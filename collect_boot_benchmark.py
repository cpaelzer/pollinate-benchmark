#!/usr/bin/env python3
import argparse
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
echo "==> Configuring APT and MOTD defaults..."
sudo mkdir -p /etc/apt/apt.conf.d/
cat << 'EOF2' | sudo tee /etc/apt/apt.conf.d/99disable-periodic > /dev/null
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF2
[ -f /etc/default/motd-news ] && sudo sed -i 's/ENABLED=1/ENABLED=0/g' /etc/default/motd-news
[ -f /etc/default/apport ] && sudo sed -i 's/enabled=1/enabled=0/g' /etc/default/apport

echo "==> Masking background services and timers..."
TARGETS=(
    apt-daily.timer apt-daily.service apt-daily-upgrade.timer apt-daily-upgrade.service unattended-upgrades.service
    motd-news.timer motd-news.service fstrim.timer plocate-updatedb.timer man-db.timer logrotate.timer
    systemd-tmpfiles-clean.timer snapd.refresh.timer snapd.service snapd.socket snapd.seeded.service
    apport.service ubuntu-advantage.service
)
for item in "${TARGETS[@]}"; do
    sudo systemctl stop "$item" 2>/dev/null
    sudo systemctl mask "$item" 2>/dev/null
done

echo "==> Halting time synchronization and potential boot warp..."
sudo timedatectl set-ntp false
sudo systemctl stop systemd-timesyncd chrony 2>/dev/null

echo "==> Base environment minimized. Ready for benchmark."
"""

DURATION_RE = re.compile(r"([0-9]*\.?[0-9]+)(us|ms|s|min|h)")


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cmd(cmd, *, input_text: Optional[str] = None, timeout: Optional[int] = None, check: bool = False) -> CmdResult:
    p = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return CmdResult(p.returncode, p.stdout, p.stderr)


def run_with_retries(fn, retries: int, delay_seconds: int, description: str):
    last_err = None
    for idx in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_err = exc
            if idx < retries:
                time.sleep(delay_seconds)
    raise RuntimeError(f"{description} failed after {retries} retries: {last_err}")


def lxc_guest_cmd(vm_name: str, guest_cmd: str, *, timeout: Optional[int] = None, check: bool = True) -> CmdResult:
    result = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-lc", guest_cmd], timeout=timeout, check=False)
    if check and result.rc != 0:
        raise RuntimeError(f"Guest command failed: {guest_cmd}\nSTDOUT:\n{result.out}\nSTDERR:\n{result.err}")
    return result


def vm_exists(vm_name: str) -> bool:
    return run_cmd(["lxc", "info", vm_name], check=False).rc == 0


def provision_vm(vm_name: str, force_recreate: bool):
    if vm_exists(vm_name):
        if not force_recreate:
            raise RuntimeError(
                f"VM '{vm_name}' already exists. Refusing to continue. Pass --force-recreate to replace it."
            )
        run_cmd(["lxc", "delete", "-f", vm_name], check=True)

    run_cmd(
        [
            "lxc",
            "launch",
            "ubuntu:26.04",
            vm_name,
            "--vm",
            "-c",
            "limits.cpu=4",
            "-c",
            "limits.memory=8GiB",
        ],
        check=True,
    )


def wait_for_guest(vm_name: str, timeout_seconds: int):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        probe = run_cmd(["lxc", "exec", vm_name, "--", "true"], check=False)
        if probe.rc == 0:
            return
        time.sleep(5)
    raise RuntimeError(f"Guest {vm_name} did not become ready within {timeout_seconds} seconds")


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_prep_stage(vm_name: str, setup_dir: Path):
    setup_dir.mkdir(parents=True, exist_ok=True)

    host = run_cmd(["bash", "-s"], input_text=PREP_SCRIPT, timeout=1800, check=False)
    write_text(setup_dir / "host-prep.stdout.log", host.out)
    write_text(setup_dir / "host-prep.stderr.log", host.err)
    if host.rc != 0:
        raise RuntimeError("Host preparation script failed")

    guest = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-s"], input_text=PREP_SCRIPT, timeout=1800, check=False)
    write_text(setup_dir / "guest-prep.stdout.log", guest.out)
    write_text(setup_dir / "guest-prep.stderr.log", guest.err)
    if guest.rc != 0:
        raise RuntimeError("Guest preparation script failed")

    reboot_cmd = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-lc", "sudo reboot"], check=False)
    write_text(setup_dir / "post-setup-reboot.stdout.log", reboot_cmd.out)
    write_text(setup_dir / "post-setup-reboot.stderr.log", reboot_cmd.err)


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


def collect_one_attempt(
    vm_name: str,
    mode: str,
    attempt_no: int,
    out_dir: Path,
    retries: int,
    retry_delay: int,
    wait_timeout: int,
    attempt_timeout: int,
) -> Dict:
    started = time.time()
    mode_dir = out_dir / ("mode_a" if mode == "a" else "mode_b") / f"attempt_{attempt_no:04d}"
    mode_dir.mkdir(parents=True, exist_ok=True)

    def check_attempt_budget():
        if (time.time() - started) > attempt_timeout:
            raise RuntimeError(f"Attempt {attempt_no} exceeded timeout of {attempt_timeout} seconds")

    if mode == "b":
        run_with_retries(
            lambda: lxc_guest_cmd(vm_name, "sudo rm -f /var/cache/pollinate/seeded", timeout=60, check=True),
            retries,
            retry_delay,
            "remove pollinate seeded marker",
        )
        check_attempt_budget()

    reboot = run_cmd(["lxc", "exec", vm_name, "--", "bash", "-lc", "sudo reboot"], check=False, timeout=30)
    write_text(mode_dir / "reboot.stdout.log", reboot.out)
    write_text(mode_dir / "reboot.stderr.log", reboot.err)

    run_with_retries(
        lambda: wait_for_guest(vm_name, wait_timeout),
        retries,
        retry_delay,
        "wait for guest readiness",
    )
    check_attempt_budget()

    analyze_time = run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "systemd-analyze time", timeout=120, check=True),
        retries,
        retry_delay,
        "systemd-analyze time",
    )
    write_text(mode_dir / "systemd-analyze-time.txt", analyze_time.out)

    analyze_blame = run_with_retries(
        lambda: lxc_guest_cmd(vm_name, "systemd-analyze blame", timeout=180, check=True),
        retries,
        retry_delay,
        "systemd-analyze blame",
    )
    write_text(mode_dir / "systemd-analyze-blame.txt", analyze_blame.out)

    cpu_nsec = None
    cpu_raw = ""
    if mode == "b":
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

    total_s, kernel_s, userspace_s = parse_systemd_analyze_time(analyze_time.out)
    check_attempt_budget()

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
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=int, default=10)
    parser.add_argument("--attempt-timeout", type=int, default=420)
    args = parser.parse_args()

    if shutil.which("lxc") is None:
        raise RuntimeError("Required command 'lxc' not found in PATH")

    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_root) / run_id
    setup_dir = out_dir / "setup"
    attempts_log = out_dir / "attempts.jsonl"

    run_cmd(["lxc", "version"], check=True)

    provision_vm(args.vm_name, args.force_recreate)
    wait_for_guest(args.vm_name, args.wait_timeout)

    if not args.skip_prep:
        run_prep_stage(args.vm_name, setup_dir)
        wait_for_guest(args.vm_name, args.wait_timeout)

    success = {"a": 0, "b": 0}
    attempt_no = 1

    while success["a"] < args.iterations or success["b"] < args.iterations:
        mode = "a" if (attempt_no % 2 == 1) else "b"

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
            )
            success[mode] += 1
            append_jsonl(attempts_log, metadata)
            print(
                f"attempt={attempt_no} mode={mode} status=success counts(a={success['a']}, b={success['b']})",
                flush=True,
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
            print(
                f"attempt={attempt_no} mode={mode} status=failed error={exc} counts(a={success['a']}, b={success['b']})",
                file=sys.stderr,
                flush=True,
            )
        attempt_no += 1

    summary = {
        "run_id": run_id,
        "vm_name": args.vm_name,
        "iterations_target_per_mode": args.iterations,
        "successful_counts": success,
        "total_attempts": attempt_no - 1,
        "finished_at": now_iso(),
    }
    write_text(out_dir / "run-summary.json", json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
