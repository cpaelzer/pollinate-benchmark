#!/usr/bin/env python3
import argparse
import atexit
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

PREP_SCRIPT = """#!/bin/bash
set -euo pipefail
echo "==> Configuring APT and MOTD defaults..."
sudo -n mkdir -p /etc/apt/apt.conf.d/
cat << 'EOF2' | sudo -n tee /etc/apt/apt.conf.d/99disable-periodic > /dev/null
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF2
[ -f /etc/default/motd-news ] && sudo -n sed -i 's/ENABLED=1/ENABLED=0/g' /etc/default/motd-news
[ -f /etc/default/apport ] && sudo -n sed -i 's/enabled=1/enabled=0/g' /etc/default/apport

echo "==> Masking background services and timers..."
TARGETS=(
    apt-daily.timer apt-daily.service apt-daily-upgrade.timer apt-daily-upgrade.service unattended-upgrades.service
    motd-news.timer motd-news.service fstrim.timer plocate-updatedb.timer man-db.timer logrotate.timer
    systemd-tmpfiles-clean.timer snapd.refresh.timer snapd.service snapd.socket snapd.seeded.service
    apport.service ubuntu-advantage.service
)
for item in "${TARGETS[@]}"; do
    sudo -n systemctl stop "$item" 2>/dev/null || true
    sudo -n systemctl mask "$item" 2>/dev/null || true
done

echo "==> Halting time synchronization and potential boot warp..."
sudo -n timedatectl set-ntp false
sudo -n systemctl stop systemd-timesyncd chrony 2>/dev/null || true

echo "==> Base environment minimized. Ready for benchmark."
"""

DURATION_RE = re.compile(r"([0-9]*\.?[0-9]+)(us|ms|s|min|h)")
MODE_NO_POLLINATE = "no-pollinate"
MODE_POLLINATED = "pollinated"


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts} {level}] {msg}", flush=True)


def run_cmd_streaming(
    cmd: list,
    *,
    input_text: Optional[str] = None,
    timeout: Optional[int] = None,
    check: bool = False,
) -> int:
    """Run a command with stdout/stderr streamed live to the terminal.

    Returns the process exit code. No captured output; use run_cmd() when
    the output needs to be parsed.
    """
    cmd_str = " ".join(str(c) for c in cmd)
    log(f"  $ {cmd_str}")
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
    )
    try:
        proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd_str}")
    elapsed = time.monotonic() - t0
    log(f"  -> rc={proc.returncode} ({elapsed:.1f}s)")
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed (rc={proc.returncode}): {cmd_str}")
    return proc.returncode


def run_cmd(cmd, *, input_text: Optional[str] = None, timeout: Optional[int] = None, check: bool = False) -> CmdResult:
    p = subprocess.run(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return CmdResult(p.returncode, p.stdout, p.stderr)


def decode_captured_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_with_retries(fn, retries: int, delay_seconds: int, description: str):
    last_err = None
    for idx in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_err = exc
            if idx < retries:
                log(
                    f"  retry {idx}/{retries - 1} for '{description}': {exc}; "
                    f"waiting {delay_seconds}s before next attempt",
                    "WARN",
                )
                time.sleep(delay_seconds)
    raise RuntimeError(f"{description} failed after {retries} retries: {last_err}")


def lxc_guest_cmd(vm_name: str, guest_cmd: str, *, timeout: Optional[int] = None, check: bool = True) -> CmdResult:
    result = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-lc", guest_cmd], timeout=timeout, check=False)
    if check and result.rc != 0:
        raise RuntimeError(f"Guest command failed: {guest_cmd}\nSTDOUT:\n{result.out}\nSTDERR:\n{result.err}")
    return result


def vm_exists(vm_name: str) -> bool:
    return run_cmd(["lxc", "info", vm_name], check=False).rc == 0


def collect_lxd_daemon_logs(start_ts: str, end_ts: str) -> str:
    result = run_cmd(
        [
            "sudo",
            "-n",
            "journalctl",
            "-u",
            "snap.lxd.daemon",
            "--since",
            start_ts,
            "--until",
            end_ts,
            "--no-pager",
        ],
        timeout=120,
        check=False,
    )
    out = result.out.strip()
    err = result.err.strip()
    if result.rc != 0:
        return (
            "Failed to collect LXD daemon logs "
            f"(rc={result.rc})\nSTDOUT:\n{out or '<empty>'}\nSTDERR:\n{err or '<empty>'}"
        )
    return out or "<no log lines in selected window>"


def get_image_fingerprint(image_ref: str, timeout_seconds: int) -> str:
    result = run_cmd(
        ["lxc", "image", "info", image_ref],
        timeout=timeout_seconds,
        check=True,
    )
    for line in result.out.splitlines():
        if line.startswith("Fingerprint:"):
            fingerprint = line.split(":", 1)[1].strip()
            if fingerprint:
                return fingerprint
    raise RuntimeError(f"Could not determine fingerprint for image reference '{image_ref}'")


def run_lxd_step_with_retries(
    *,
    step_name: str,
    cmd: list,
    retries: int,
    timeout_seconds: int,
    first_attempt_timeout_multiplier: int = 1,
    cleanup_cmd: Optional[list] = None,
) -> None:
    last_stdout = ""
    last_stderr = ""
    last_reason = ""
    last_daemon_logs = ""

    for attempt in range(1, retries + 1):
        attempt_timeout = timeout_seconds * first_attempt_timeout_multiplier if attempt == 1 else timeout_seconds
        log(f"{step_name}... attempt {attempt}/{retries} (timeout={attempt_timeout}s)")

        launch_stdout = ""
        launch_stderr = ""
        reason = ""
        started_at = datetime.now(timezone.utc)
        started_str = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        try:
            result = run_cmd(cmd, timeout=attempt_timeout, check=False)
            launch_stdout = result.out
            launch_stderr = result.err
            if result.rc == 0:
                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                log(f"{step_name} succeeded ({elapsed:.0f}s)")
                return
            reason = f"command exited with rc={result.rc}"
        except subprocess.TimeoutExpired as exc:
            launch_stdout = decode_captured_output(exc.stdout)
            launch_stderr = decode_captured_output(exc.stderr)
            reason = f"command timed out after {attempt_timeout}s"

        ended_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        daemon_logs = collect_lxd_daemon_logs(started_str, ended_str)

        last_stdout = launch_stdout
        last_stderr = launch_stderr
        last_reason = reason
        last_daemon_logs = daemon_logs

        log(f"{step_name} failed on attempt {attempt}: {reason}", "ERROR")
        if launch_stdout:
            log("Captured command stdout:", "ERROR")
            print(launch_stdout, flush=True)
        else:
            log("Captured command stdout: <empty>", "ERROR")
        if launch_stderr:
            log("Captured command stderr:", "ERROR")
            print(launch_stderr, flush=True)
        else:
            log("Captured command stderr: <empty>", "ERROR")

        log(f"LXD daemon logs for failure window [{started_str} .. {ended_str}]", "ERROR")
        print(daemon_logs, flush=True)

        if attempt < retries and cleanup_cmd is not None:
            log("Cleaning up potentially partial instance before retry...", "WARN")
            run_cmd(cleanup_cmd, timeout=180, check=False)

    details = [f"{step_name} failed after {retries} attempts. Last error: {last_reason}"]
    if last_stdout:
        details.append(f"Last command stdout:\n{last_stdout}")
    if last_stderr:
        details.append(f"Last command stderr:\n{last_stderr}")
    if last_daemon_logs:
        details.append(f"LXD daemon logs around last failure:\n{last_daemon_logs}")
    raise RuntimeError("\n".join(details))


def provision_vm(vm_name: str, force_recreate: bool, provision_timeout: int):
    if vm_exists(vm_name):
        if not force_recreate:
            raise RuntimeError(
                f"VM '{vm_name}' already exists. Refusing to continue. Pass --force-recreate to replace it."
            )
        log(f"Deleting existing VM '{vm_name}'...")
        run_cmd(["lxc", "delete", "-f", vm_name], check=True)
        log(f"VM '{vm_name}' deleted")

    prefetch_cmd = ["lxc", "--debug", "--verbose", "image", "copy", "ubuntu:26.04", "local:"]
    run_lxd_step_with_retries(
        step_name="Prefetching LXD image 'ubuntu:26.04' into local cache",
        cmd=prefetch_cmd,
        retries=3,
        timeout_seconds=provision_timeout,
    )

    fingerprint = get_image_fingerprint("ubuntu:26.04", provision_timeout)
    local_image_ref = f"local:{fingerprint}"
    log(f"Using prefetched local image fingerprint for launch: {local_image_ref}")

    launch_cmd = [
        "lxc",
        "--debug",
        "--verbose",
        "launch",
        local_image_ref,
        vm_name,
        "--vm",
        "-c",
        "limits.cpu=2",
        "-c",
        "limits.memory=2GiB",
    ]
    run_lxd_step_with_retries(
        step_name=(
            f"Launching VM '{vm_name}' ({local_image_ref}, limits.cpu=2, limits.memory=2GiB)"
        ),
        cmd=launch_cmd,
        retries=3,
        timeout_seconds=provision_timeout,
        first_attempt_timeout_multiplier=2,
        cleanup_cmd=["lxc", "delete", "-f", vm_name],
    )


def wait_for_guest(vm_name: str, timeout_seconds: int):
    log(f"Waiting for guest '{vm_name}' to become ready (timeout={timeout_seconds}s)...")
    t0 = time.monotonic()
    last_report = t0
    deadline = t0 + timeout_seconds
    while time.monotonic() < deadline:
        probe = run_cmd(["lxc", "exec", vm_name, "--", "true"], check=False)
        if probe.rc == 0:
            log(f"Guest '{vm_name}' is ready ({time.monotonic() - t0:.0f}s elapsed)")
            return
        now = time.monotonic()
        if now - last_report >= 30:
            log(f"  still waiting for '{vm_name}' ({now - t0:.0f}s elapsed)...")
            last_report = now
        time.sleep(5)
    raise RuntimeError(f"Guest {vm_name} did not become ready within {timeout_seconds} seconds")


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_prep_stage(
    vm_name: str,
    setup_dir: Path,
    retries: int,
    retry_delay: int,
    wait_timeout: int,
    post_reboot_delay: int,
):
    setup_dir.mkdir(parents=True, exist_ok=True)

    log("=== HOST PREP: running environment minimization script on host ===")
    log("NOTE: script runs with sudo -n; requires passwordless sudo (checked at startup)")
    rc = run_cmd_streaming(["bash", "-s"], input_text=PREP_SCRIPT, timeout=300)
    if rc != 0:
        raise RuntimeError(f"Host preparation script failed (rc={rc})")
    log("=== HOST PREP COMPLETE ===")

    log(f"=== GUEST PREP: running environment minimization script on '{vm_name}' ===")
    rc = run_cmd_streaming(
        ["lxc", "exec", vm_name, "--", "bash", "-s"],
        input_text=PREP_SCRIPT,
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"Guest preparation script failed (rc={rc})")
    log("=== GUEST PREP COMPLETE ===")

    log(f"Rebooting guest '{vm_name}' after prep (reboot command may return non-zero as the connection drops)...")
    reboot_guest_and_wait(
        vm_name,
        retries=retries,
        retry_delay=retry_delay,
        wait_timeout=wait_timeout,
        post_reboot_delay=post_reboot_delay,
        phase_label="post-prep reboot",
        artifacts_dir=setup_dir,
        reboot_log_prefix="post-prep-reboot",
    )


def parse_duration_to_seconds(token: str) -> float:
    m = DURATION_RE.fullmatch(token.strip())
    if not m:
        raise ValueError(f"Invalid duration token: {token}")
    value = float(m.group(1))
    unit = m.group(2)
    if unit == "us":
        return value / 1_000_000.0
    if unit == "ms":
        return value / 1_000.0
    if unit == "s":
        return value
    if unit == "min":
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    raise ValueError(f"Unsupported unit: {unit}")


def parse_systemd_analyze_time(output: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    kernel = None
    userspace = None
    total = None

    mk = re.search(r"([0-9]*\.?[0-9]+(?:us|ms|s|min|h))\s+\(kernel\)", output)
    mu = re.search(r"([0-9]*\.?[0-9]+(?:us|ms|s|min|h))\s+\(userspace\)", output)
    mt = re.search(r"=\s*([0-9]*\.?[0-9]+(?:us|ms|s|min|h))", output)

    if mk:
        kernel = parse_duration_to_seconds(mk.group(1))
    if mu:
        userspace = parse_duration_to_seconds(mu.group(1))
    if mt:
        total = parse_duration_to_seconds(mt.group(1))

    return total, kernel, userspace


def parse_cpu_nsec(output: str) -> Optional[int]:
    m = re.search(r"CPUUsageNSec=(\d+)", output)
    if not m:
        return None
    return int(m.group(1))


def get_boot_id(vm_name: str) -> str:
    result = lxc_guest_cmd(vm_name, "cat /proc/sys/kernel/random/boot_id", timeout=30, check=True)
    boot_id = result.out.strip()
    if not boot_id:
        raise RuntimeError("Empty boot_id returned from guest")
    return boot_id


def wait_for_new_boot_id(vm_name: str, old_boot_id: str, timeout_seconds: int, retry_delay: int) -> str:
    """Wait until the guest reports a boot_id different from old_boot_id.

    This guards against measuring the previous boot when reconnect happens too
    quickly during a reboot transition.
    """
    log(f"  Verifying reboot completion via boot_id transition (old={old_boot_id})...")
    t0 = time.monotonic()
    deadline = t0 + timeout_seconds
    while time.monotonic() < deadline:
        try:
            current_boot_id = get_boot_id(vm_name)
            if current_boot_id != old_boot_id:
                log(
                    f"  boot_id changed: {old_boot_id} -> {current_boot_id} "
                    f"({time.monotonic() - t0:.0f}s elapsed)"
                )
                return current_boot_id
            log("  boot_id unchanged; still observing previous boot, waiting...")
        except Exception as exc:
            log(f"  boot_id probe failed while waiting for reboot completion: {exc}", "WARN")
        time.sleep(retry_delay)
    raise RuntimeError(
        f"boot_id did not change within {timeout_seconds}s after reboot; "
        f"still appears to be old boot {old_boot_id}"
    )


def reboot_guest_and_wait(
    vm_name: str,
    *,
    retries: int,
    retry_delay: int,
    wait_timeout: int,
    post_reboot_delay: int,
    phase_label: str,
    artifacts_dir: Optional[Path] = None,
    reboot_log_prefix: str = "reboot",
) -> str:
    old_boot_id = run_with_retries(
        lambda: get_boot_id(vm_name),
        retries,
        retry_delay,
        f"read pre-reboot boot_id ({phase_label})",
    )
    log(f"  Current boot_id before reboot ({phase_label}): {old_boot_id}")

    log(f"  Sending reboot to guest '{vm_name}' ({phase_label})...")
    reboot = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-lc", "sudo -n reboot"], check=False, timeout=30)
    if artifacts_dir is not None:
        write_text(artifacts_dir / f"{reboot_log_prefix}.stdout.log", reboot.out)
        write_text(artifacts_dir / f"{reboot_log_prefix}.stderr.log", reboot.err)
    log(
        f"  Reboot command returned rc={reboot.rc} "
        f"(non-zero is normal as the connection drops during shutdown)"
    )

    log(f"  Sleeping {post_reboot_delay}s to let shutdown/reboot transition begin...")
    time.sleep(post_reboot_delay)

    run_with_retries(
        lambda: wait_for_guest(vm_name, wait_timeout),
        retries,
        retry_delay,
        f"wait for guest readiness ({phase_label})",
    )

    new_boot_id = wait_for_new_boot_id(vm_name, old_boot_id, wait_timeout, retry_delay)
    log(f"  Reboot validated with new boot_id ({phase_label}): {new_boot_id}")
    return new_boot_id


def wait_for_boot_completion(vm_name: str, timeout_seconds: int, poll_delay: int) -> None:
    """Wait until systemd reports boot completion.

    This prevents noisy retries for commands like `systemd-analyze time` on
    slow systems where ssh/lxd-agent is already available but boot is not
    fully finished yet.
    """
    log(f"  Waiting for full system boot completion (timeout={timeout_seconds}s)...")
    t0 = time.monotonic()
    deadline = t0 + timeout_seconds
    last_report = t0

    while time.monotonic() < deadline:
        probe = lxc_guest_cmd(
            vm_name,
            "systemctl show -p FinishTimestampMonotonic --value",
            timeout=30,
            check=False,
        )
        if probe.rc == 0:
            raw_value = probe.out.strip()
            if raw_value.isdigit() and int(raw_value) > 0:
                log(f"  System boot completion confirmed ({time.monotonic() - t0:.0f}s elapsed)")
                return

        now = time.monotonic()
        if now - last_report >= 30:
            log(f"  still waiting for full boot completion ({now - t0:.0f}s elapsed)...")
            last_report = now
        time.sleep(poll_delay)

    raise RuntimeError(f"System boot did not fully complete within {timeout_seconds} seconds")


def collect_one_attempt(
    vm_name: str,
    mode: str,
    attempt_no: int,
    out_dir: Path,
    retries: int,
    retry_delay: int,
    wait_timeout: int,
    attempt_timeout: int,
    post_reboot_delay: int,
    boot_complete_timeout: int,
    boot_complete_poll_delay: int,
) -> Dict:
    mode_label = (
        "pollinate skipped (seeded file intact)"
        if mode == MODE_NO_POLLINATE
        else "pollinate active (seeded file removed)"
    )
    log(f"--- Attempt {attempt_no} mode={mode} ({mode_label}) ---")
    started = time.time()
    mode_dir = out_dir / mode / f"attempt_{attempt_no:04d}"
    mode_dir.mkdir(parents=True, exist_ok=True)
    log(f"  Artifacts: {mode_dir}")

    def check_attempt_budget():
        elapsed = time.time() - started
        if elapsed > attempt_timeout:
            raise RuntimeError(f"Attempt {attempt_no} exceeded timeout of {attempt_timeout}s ({elapsed:.0f}s elapsed)")

    if mode == MODE_POLLINATED:
        log("  Removing /var/cache/pollinate/seeded so pollinate runs on next boot...")
        run_with_retries(
            lambda: lxc_guest_cmd(vm_name, "sudo -n rm -f /var/cache/pollinate/seeded", timeout=60, check=True),
            retries,
            retry_delay,
            "remove pollinate seeded marker",
        )
        check_attempt_budget()

    log("  Resetting SSH host keys and cloud-init state for fresh boot behavior...")
    run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "sudo -n rm -f /etc/ssh/ssh_host_*", timeout=60, check=True),
        retries,
        retry_delay,
        "remove SSH host keys",
    )
    run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "sudo -n cloud-init clean", timeout=60, check=True),
        retries,
        retry_delay,
        "cloud-init clean",
    )
    check_attempt_budget()

    reboot_guest_and_wait(
        vm_name,
        retries=retries,
        retry_delay=retry_delay,
        wait_timeout=wait_timeout,
        post_reboot_delay=post_reboot_delay,
        phase_label=f"attempt {attempt_no}",
        artifacts_dir=mode_dir,
    )
    check_attempt_budget()

    wait_for_boot_completion(vm_name, boot_complete_timeout, boot_complete_poll_delay)
    check_attempt_budget()

    log("  Collecting systemd-analyze time...")
    analyze_time = run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "systemd-analyze time", timeout=120, check=True),
        retries,
        retry_delay,
        "systemd-analyze time",
    )
    write_text(mode_dir / "systemd-analyze-time.txt", analyze_time.out)
    log(f"  systemd-analyze time: {analyze_time.out.strip()}")

    log("  Collecting systemd-analyze blame...")
    analyze_blame = run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "systemd-analyze blame", timeout=180, check=True),
        retries,
        retry_delay,
        "systemd-analyze blame",
    )
    write_text(mode_dir / "systemd-analyze-blame.txt", analyze_blame.out)
    log(f"  systemd-analyze blame: {len(analyze_blame.out.splitlines())} lines saved")

    cpu_nsec = None
    cpu_raw = ""
    if mode == MODE_POLLINATED:
        log("  Collecting pollinate.service CPUUsageNSec...")
        cpu = run_with_retries(
            lambda: lxc_guest_cmd(
                vm_name,
                "systemctl show --property=CPUUsageNSec pollinate.service",
                timeout=60,
                check=True,
            ),
            retries,
            retry_delay,
            "pollinate CPU usage",
        )
        cpu_raw = cpu.out
        write_text(mode_dir / "pollinate-cpu.txt", cpu_raw)
        cpu_nsec = parse_cpu_nsec(cpu_raw)
        log(f"  pollinate CPUUsageNSec={cpu_nsec}")

    log("  Parsing systemd-analyze time output...")
    total_s, kernel_s, userspace_s = parse_systemd_analyze_time(analyze_time.out)
    check_attempt_budget()
    log(f"  Parsed: total={total_s}s  kernel={kernel_s}s  userspace={userspace_s}s")

    metadata = {
        "attempt_no": attempt_no,
        "mode": mode,
        "started_at": now_iso(),
        "total_s": total_s,
        "kernel_s": kernel_s,
        "userspace_s": userspace_s,
        "pollinate_cpu_nsec": cpu_nsec,
        "pollinate_cpu_raw": cpu_raw.strip() if cpu_raw else None,
        "status": "success",
    }
    write_text(mode_dir / "metadata.json", json.dumps(metadata, indent=2, sort_keys=True))
    return metadata


def append_jsonl(path: Path, payload: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Collect boot benchmark data for pollinate comparison")
    parser.add_argument("--vm-name", default="testvm")
    parser.add_argument("--iterations", type=int, default=100, help="Target successful runs per mode")
    parser.add_argument("--output-root", default="benchmark-data", help="Root directory for artifacts")
    parser.add_argument("--force-recreate", action="store_true", help="Delete existing VM before launch")
    parser.add_argument("--skip-prep", action="store_true", help="Skip one-time host/guest prep stage")
    parser.add_argument("--wait-timeout", type=int, default=300)
    parser.add_argument(
        "--provision-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for each provisioning step attempt (default: 600)",
    )
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=int, default=10)
    parser.add_argument("--attempt-timeout", type=int, default=420)
    parser.add_argument(
        "--post-reboot-delay",
        type=int,
        default=20,
        help="Seconds to wait after issuing reboot before reconnect checks (default: 20)",
    )
    parser.add_argument(
        "--boot-complete-timeout",
        type=int,
        default=180,
        help="Seconds to wait for full system boot completion after reboot validation (default: 180)",
    )
    parser.add_argument(
        "--boot-complete-poll-delay",
        type=int,
        default=5,
        help="Seconds between full-boot completion checks (default: 5)",
    )
    args = parser.parse_args()

    # Restore terminal on exit in case a subprocess (e.g. sudo) left it in raw mode.
    atexit.register(lambda: subprocess.run(["stty", "sane"], check=False, stderr=subprocess.DEVNULL))

    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_root) / run_id
    setup_dir = out_dir / "setup"
    attempts_log = out_dir / "attempts.jsonl"

    log("=== Pollinate boot benchmark ===")
    log(f"run_id        : {run_id}")
    log(f"output_dir    : {out_dir}")
    log(f"vm_name       : {args.vm_name}")
    log(f"iterations    : {args.iterations} per mode")
    log(f"skip_prep     : {args.skip_prep}")
    log(f"provision_timeout: {args.provision_timeout}s")
    log(f"wait_timeout  : {args.wait_timeout}s  retries={args.retries}  retry_delay={args.retry_delay}s")
    log(f"attempt_timeout: {args.attempt_timeout}s")
    log(f"post_reboot_delay: {args.post_reboot_delay}s")
    log(
        f"boot_complete_timeout: {args.boot_complete_timeout}s  "
        f"boot_complete_poll_delay={args.boot_complete_poll_delay}s"
    )

    # Preflight: verify lxc is available.
    if shutil.which("lxc") is None:
        log("Required command 'lxc' not found in PATH", "ERROR")
        sys.exit(1)
    lxc_ver = run_cmd(["lxc", "version"], check=True)
    log(f"lxc version: {lxc_ver.out.strip()}")

    # Preflight: verify passwordless sudo on host (required by PREP_SCRIPT).
    if not args.skip_prep:
        log("Checking host passwordless sudo (required for prep stage)...")
        sudo_check = run_cmd(["sudo", "-n", "true"], check=False)
        if sudo_check.rc != 0:
            log(
                "Host sudo requires a password (sudo -n true returned non-zero).\n"
                "  -> Configure passwordless sudo for this user, or pass --skip-prep to skip host prep.",
                "ERROR",
            )
            sys.exit(1)
        log("Passwordless sudo on host: OK")

    provision_vm(args.vm_name, args.force_recreate, args.provision_timeout)
    wait_for_guest(args.vm_name, args.wait_timeout)

    if not args.skip_prep:
        log("Starting one-time environment preparation stage...")
        run_prep_stage(
            args.vm_name,
            setup_dir,
            args.retries,
            args.retry_delay,
            args.wait_timeout,
            args.post_reboot_delay,
        )
        log("Prep stage complete; reboot validated. Starting measurement campaign.")
    else:
        log("Skipping prep stage (--skip-prep passed).")

    success = {MODE_NO_POLLINATE: 0, MODE_POLLINATED: 0}
    attempt_no = 1

    log(
        "=== Starting measurement campaign: target "
        f"{args.iterations} successful runs per mode ({MODE_NO_POLLINATE}, {MODE_POLLINATED}) ==="
    )

    while success[MODE_NO_POLLINATE] < args.iterations or success[MODE_POLLINATED] < args.iterations:
        mode = MODE_NO_POLLINATE if (attempt_no % 2 == 1) else MODE_POLLINATED

        if success[mode] >= args.iterations:
            append_jsonl(
                attempts_log,
                {
                    "attempt_no": attempt_no,
                    "mode": mode,
                    "status": "skipped_target_met",
                    "timestamp": now_iso(),
                },
            )
            attempt_no += 1
            continue

        try:
            metadata = collect_one_attempt(
                vm_name=args.vm_name,
                mode=mode,
                attempt_no=attempt_no,
                out_dir=out_dir,
                retries=args.retries,
                retry_delay=args.retry_delay,
                wait_timeout=args.wait_timeout,
                attempt_timeout=args.attempt_timeout,
                post_reboot_delay=args.post_reboot_delay,
                boot_complete_timeout=args.boot_complete_timeout,
                boot_complete_poll_delay=args.boot_complete_poll_delay,
            )
            success[mode] += 1
            append_jsonl(attempts_log, metadata)
            log(
                f"Attempt {attempt_no} mode={mode} SUCCESS  "
                f"[{MODE_NO_POLLINATE}: {success[MODE_NO_POLLINATE]}/{args.iterations}  "
                f"{MODE_POLLINATED}: {success[MODE_POLLINATED]}/{args.iterations}]"
            )
        except Exception as exc:
            payload = {
                "attempt_no": attempt_no,
                "mode": mode,
                "status": "failed",
                "error": str(exc),
                "timestamp": now_iso(),
            }
            append_jsonl(attempts_log, payload)
            log(
                f"Attempt {attempt_no} mode={mode} FAILED: {exc}  "
                f"[{MODE_NO_POLLINATE}: {success[MODE_NO_POLLINATE]}/{args.iterations}  "
                f"{MODE_POLLINATED}: {success[MODE_POLLINATED]}/{args.iterations}]",
                "WARN",
            )
        attempt_no += 1

    log(
        "=== Campaign complete: "
        f"{success[MODE_NO_POLLINATE]} {MODE_NO_POLLINATE} and "
        f"{success[MODE_POLLINATED]} {MODE_POLLINATED} successful runs ==="
    )

    summary = {
        "run_id": run_id,
        "vm_name": args.vm_name,
        "iterations_target_per_mode": args.iterations,
        "successful_counts": success,
        "total_attempts": attempt_no - 1,
        "finished_at": now_iso(),
    }
    write_text(out_dir / "run-summary.json", json.dumps(summary, indent=2, sort_keys=True))
    log(f"Run summary written to {out_dir / 'run-summary.json'}")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
