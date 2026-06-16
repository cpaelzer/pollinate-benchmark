#!/usr/bin/env python3
"""Batch summarizer for pollinate boot benchmark results.

Walks the results/ tree (virtiorng_mode / machine / release) and produces two
TSV tables paste-ready for Google Sheets:

    Table 1 — Userspace boot time:  no-pollinate vs pollinated median + stddev
  Table 2 — Pollinate CPU usage:  pollinated runs grouped by machine

Usage:
  ./summarize_results.py [results_dir] [--min-retain-fraction F]
                         [--min-retain-count N] [--out FILE]
"""

import argparse
import sys
from pathlib import Path

# Reuse existing analysis helpers — no duplication.
from analyze_boot_benchmark import (  # type: ignore
    mode_stats,
    read_mode_metadata,
)

MODE_NO_POLLINATE = "no-pollinate"
MODE_POLLINATED = "pollinated"

NA = "n/a"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_latest_run_dir(leaf: Path) -> Path | None:
    """Return the most-recent benchmark-data/run-*/ inside a leaf directory."""
    bdata = leaf / "benchmark-data"
    if not bdata.is_dir():
        return None
    runs = sorted(bdata.glob("run-*/"))
    if not runs:
        return None
    return runs[-1]


def discover_leaves(results_dir: Path):
    """Yield (virtiorng_mode, machine, release, run_dir) for every populated leaf.

    Leaves with no benchmark-data or no run directories are emitted as warnings
    and skipped.
    """
    for virt_dir in sorted(results_dir.iterdir()):
        if not virt_dir.is_dir():
            continue
        for machine_dir in sorted(virt_dir.iterdir()):
            if not machine_dir.is_dir():
                continue
            for release_dir in sorted(machine_dir.iterdir()):
                if not release_dir.is_dir():
                    continue
                run_dir = find_latest_run_dir(release_dir)
                if run_dir is None:
                    print(
                        f"WARN: no benchmark data found in {release_dir.relative_to(results_dir)} — skipping",
                        file=sys.stderr,
                    )
                    continue
                yield virt_dir.name, machine_dir.name, release_dir.name, run_dir


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _fmt_s(v) -> str:
    return f"{v:.4f}" if v is not None else NA


def _fmt_ns(v) -> str:
    return f"{int(round(v))}" if v is not None else NA


def _raw_median_stddev_count(stats: dict):
    r = stats["raw"]
    return r["median"], r["stddev"], r["count"]


def _filtered_mean(stats: dict):
    f = stats["filtered"]
    return f["mean"]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def build_userspace_rows(leaves, min_retain_fraction, min_retain_count):
    """Return list of row-dicts for Table 1 (userspace boot time)."""
    rows = []
    for virt_mode, machine, release, run_dir in leaves:
        np_rows = read_mode_metadata(run_dir / MODE_NO_POLLINATE)
        p_rows = read_mode_metadata(run_dir / MODE_POLLINATED)

        np_stats = mode_stats(np_rows, "userspace_s", min_retain_fraction, min_retain_count)
        p_stats = mode_stats(p_rows, "userspace_s", min_retain_fraction, min_retain_count)

        np_median, np_std, np_n = _raw_median_stddev_count(np_stats)
        p_median, p_std, p_n = _raw_median_stddev_count(p_stats)

        np_mean_filtered = _filtered_mean(np_stats)
        p_mean_filtered = _filtered_mean(p_stats)

        delta = (p_median - np_median) if (p_median is not None and np_median is not None) else None

        rows.append({
            "virtiorng": virt_mode,
            "machine": machine,
            "release": release,
            "np_n": np_n,
            "np_median_s": np_median,
            "np_stddev_s": np_std,
            "np_mean_filtered_s": np_mean_filtered,
            "p_n": p_n,
            "p_median_s": p_median,
            "p_stddev_s": p_std,
            "p_mean_filtered_s": p_mean_filtered,
            "delta_median_s": delta,
        })
    return rows


def build_cpu_rows(leaves, min_retain_fraction, min_retain_count):
    """Return list of row-dicts for Table 2 (pollinate CPU usage)."""
    rows = []
    for virt_mode, machine, release, run_dir in leaves:
        p_rows = read_mode_metadata(run_dir / MODE_POLLINATED)
        p_stats = mode_stats(p_rows, "pollinate_cpu_nsec", min_retain_fraction, min_retain_count)
        median, std, n = _raw_median_stddev_count(p_stats)
        mean_filtered = _filtered_mean(p_stats)
        rows.append({
            "virtiorng": virt_mode,
            "machine": machine,
            "release": release,
            "n": n,
            "median_ns": median,
            "stddev_ns": std,
            "mean_filtered_ns": mean_filtered,
        })
    # Sort by machine first so same-machine rows are adjacent (easy visual check).
    rows.sort(key=lambda r: (r["machine"], r["virtiorng"], r["release"]))
    return rows


# ---------------------------------------------------------------------------
# TSV formatting
# ---------------------------------------------------------------------------

USERSPACE_HEADERS = [
    "virtiorng", "machine", "release",
    "np_n", "np_median_s", "np_stddev_s", "np_mean_filtered_s",
    "p_n", "p_median_s", "p_stddev_s", "p_mean_filtered_s",
    "delta_median_s",
]

CPU_HEADERS = [
    "virtiorng", "machine", "release",
    "n", "median_ns", "stddev_ns", "mean_filtered_ns",
]


def row_to_tsv(row: dict, headers: list, num_keys: set) -> str:
    parts = []
    for h in headers:
        v = row.get(h)
        if v is None:
            parts.append(NA)
        elif h in num_keys:
            # numeric: format appropriately
            if "ns" in h:
                parts.append(_fmt_ns(v))
            else:
                parts.append(_fmt_s(v))
        else:
            parts.append(str(v))
    return "\t".join(parts)


def print_table(title: str, headers: list, rows: list, num_keys: set, out):
    print(f"# {title}", file=out)
    print("\t".join(headers), file=out)
    for row in rows:
        print(row_to_tsv(row, headers, num_keys), file=out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch-summarize pollinate benchmark results into TSV tables"
    )
    parser.add_argument(
        "results_dir",
        nargs="?",
        default="results",
        help="Root results directory (default: results/)",
    )
    parser.add_argument("--min-retain-fraction", type=float, default=0.70)
    parser.add_argument("--min-retain-count", type=int, default=20)
    parser.add_argument(
        "--out",
        default=None,
        help="Write TSV output to FILE instead of stdout",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        sys.exit(f"ERROR: results directory not found: {results_dir}")

    leaves = list(discover_leaves(results_dir))
    if not leaves:
        sys.exit("ERROR: no populated benchmark leaves found")

    userspace_rows = build_userspace_rows(
        leaves, args.min_retain_fraction, args.min_retain_count
    )
    cpu_rows = build_cpu_rows(
        leaves, args.min_retain_fraction, args.min_retain_count
    )

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        userspace_num_keys = {
            "np_median_s",
            "np_stddev_s",
            "np_mean_filtered_s",
            "p_median_s",
            "p_stddev_s",
            "p_mean_filtered_s",
            "delta_median_s",
        }
        print_table(
            "Userspace boot time (seconds) — raw median/stddev and filtered mean",
            USERSPACE_HEADERS,
            userspace_rows,
            userspace_num_keys,
            out,
        )

        print(file=out)

        cpu_num_keys = {"median_ns", "stddev_ns", "mean_filtered_ns"}
        print_table(
            "Pollinate CPU usage (nanoseconds) — raw median/stddev and filtered mean — pollinated runs only",
            CPU_HEADERS,
            cpu_rows,
            cpu_num_keys,
            out,
        )
    finally:
        if args.out:
            out.close()


if __name__ == "__main__":
    main()
