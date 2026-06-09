# Pollinate Boot Benchmark

Compare boot performance with and without pollinate activity on an LXD VM, then summarize results with robust statistics.
This was done long ago, but recent CPUs have randomness as instruction and kernels no more block as much without entropy.
There was a chance this would no more be useful and then needs to be reconsidered to be default installed.

## Purpose

This benchmark measures two scenarios repeatedly:
- `no-pollinate`: normal boot path where pollinate is not forced.
- `pollinated`: `/var/cache/pollinate/seeded` is removed before reboot so pollinate runs again.

Primary reported metrics:
- Userspace boot time (`systemd-analyze time`)
- Pollinate CPU usage (`CPUUsageNSec`, pollinated mode only)

## Scripts

- `collect_boot_benchmark.py`: provisions VM, prepares environment, runs alternating boot attempts, stores artifacts.
- `analyze_boot_benchmark.py`: reads collected artifacts, applies IQR outlier filtering, reports mean + stddev.

## Quick Start

1. Collect data (small smoke run):

```bash
./collect_boot_benchmark.py --iterations 3 --force-recreate
```

2. Analyze results:

```bash
./analyze_boot_benchmark.py benchmark-data/run-YYYYMMDDTHHMMSSZ
```

## Collector Behavior (Design Basics)

- Creates VM from scratch (`testvm`) unless it exists and `--force-recreate` is provided.
- Runs one-time host + guest prep (can be skipped with `--skip-prep`).
- Alternates attempts between `no-pollinate` and `pollinated` until each reaches target successful count.
- Before each measured reboot:
  - removes SSH host keys (`/etc/ssh/ssh_host_*`)
  - runs `cloud-init clean`
- Reboot hardening:
  - waits `--post-reboot-delay` seconds (default: 20)
  - waits for guest readiness
  - verifies `boot_id` changed before collecting metrics
- Retries transient failures and logs all stages with timestamps.

## Output Layout

Inside each run directory:
- `no-pollinate/attempt_XXXX/`
- `pollinated/attempt_XXXX/`
- `attempts.jsonl`
- `run-summary.json`
- `setup/` (prep logs)

Each attempt directory includes raw command outputs and `metadata.json`.

## Key Options

Collector:
- `--iterations` successful runs required per mode (default: 100)
- `--force-recreate` recreate VM if it already exists
- `--skip-prep` skip one-time prep stage
- `--post-reboot-delay` reboot settle delay in seconds (default: 20)

Analyzer:
- `--min-retain-fraction` IQR guardrail fraction (default: 0.70)
- `--min-retain-count` IQR guardrail minimum count (default: 20)

## Notes

- Host prep uses `sudo -n`; passwordless sudo is required unless prep is skipped.
- If a run is interrupted, rerun with `--force-recreate` for a clean VM baseline.
