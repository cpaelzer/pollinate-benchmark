#!/usr/bin/env python3
import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Tuple

MODE_NO_POLLINATE = "no-pollinate"
MODE_POLLINATED = "pollinated"


def read_mode_metadata(mode_dir: Path) -> List[Dict]:
    rows = []
    if not mode_dir.exists():
        return rows
    for attempt_dir in sorted(mode_dir.glob("attempt_*")):
        meta = attempt_dir / "metadata.json"
        if not meta.exists():
            continue
        try:
            rows.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:
            continue
    return rows


def percentile(values: List[float], p: float) -> float:
    if not values:
        raise ValueError("No data")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def iqr_filter(values: List[float], min_retain_fraction: float, min_retain_count: int) -> Tuple[List[float], Dict]:
    n = len(values)
    if n < 4:
        return values[:], {
            "applied": False,
            "reason": "too_few_samples",
            "removed": 0,
            "lower": None,
            "upper": None,
        }

    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    filtered = [v for v in values if lower <= v <= upper]

    min_keep = max(min_retain_count, math.ceil(min_retain_fraction * n))
    if len(filtered) < min_keep:
        return values[:], {
            "applied": False,
            "reason": "guardrail_not_met",
            "removed": 0,
            "lower": lower,
            "upper": upper,
        }

    return filtered, {
        "applied": True,
        "reason": "ok",
        "removed": n - len(filtered),
        "lower": lower,
        "upper": upper,
    }


def summarize(values: List[float]) -> Dict:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "stddev": None,
        }
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def mode_stats(rows: List[Dict], metric_key: str, min_retain_fraction: float, min_retain_count: int) -> Dict:
    values = [float(r[metric_key]) for r in rows if r.get(metric_key) is not None]
    base = summarize(values)
    filtered, filter_info = iqr_filter(values, min_retain_fraction, min_retain_count)
    final = summarize(filtered)
    return {
        "raw": base,
        "filtered": final,
        "filter": filter_info,
    }


def fmt_seconds(v: float) -> str:
    return f"{v:.6f} s"


def fmt_nsec(v: float) -> str:
    return f"{int(round(v))} ns"


def print_metric_block(title: str, a_stats: Dict, b_stats: Dict, formatter):
    print(title)
    print(
        f"  {MODE_NO_POLLINATE} raw_count={a_stats['raw']['count']} "
        f"filtered_count={a_stats['filtered']['count']} removed={a_stats['filter']['removed']}"
    )
    print(
        f"    {MODE_NO_POLLINATE} mean="
        f"{formatter(a_stats['filtered']['mean']) if a_stats['filtered']['mean'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_NO_POLLINATE} median="
        f"{formatter(a_stats['filtered']['median']) if a_stats['filtered']['median'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_NO_POLLINATE} stddev="
        f"{formatter(a_stats['filtered']['stddev']) if a_stats['filtered']['stddev'] is not None else 'n/a'}"
    )
    print(
        f"  {MODE_POLLINATED} raw_count={b_stats['raw']['count']} "
        f"filtered_count={b_stats['filtered']['count']} removed={b_stats['filter']['removed']}"
    )
    print(
        f"    {MODE_POLLINATED} mean="
        f"{formatter(b_stats['filtered']['mean']) if b_stats['filtered']['mean'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_POLLINATED} median="
        f"{formatter(b_stats['filtered']['median']) if b_stats['filtered']['median'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_POLLINATED} stddev="
        f"{formatter(b_stats['filtered']['stddev']) if b_stats['filtered']['stddev'] is not None else 'n/a'}"
    )


def main():
    parser = argparse.ArgumentParser(description="Analyze pollinate boot benchmark artifacts")
    parser.add_argument(
        "run_dir",
        help="Path to a collector run directory containing no-pollinate and pollinated",
    )
    parser.add_argument("--min-retain-fraction", type=float, default=0.70)
    parser.add_argument("--min-retain-count", type=int, default=20)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    no_pollinate_dir = run_dir / MODE_NO_POLLINATE
    pollinated_dir = run_dir / MODE_POLLINATED

    if not no_pollinate_dir.exists() or not pollinated_dir.exists():
        raise SystemExit(
            "Expected both 'no-pollinate' and 'pollinated' directories in run_dir"
        )

    no_pollinate_rows = read_mode_metadata(no_pollinate_dir)
    pollinated_rows = read_mode_metadata(pollinated_dir)

    total_no_pollinate = mode_stats(no_pollinate_rows, "total_s", args.min_retain_fraction, args.min_retain_count)
    total_pollinated = mode_stats(pollinated_rows, "total_s", args.min_retain_fraction, args.min_retain_count)

    kernel_no_pollinate = mode_stats(no_pollinate_rows, "kernel_s", args.min_retain_fraction, args.min_retain_count)
    kernel_pollinated = mode_stats(pollinated_rows, "kernel_s", args.min_retain_fraction, args.min_retain_count)

    user_no_pollinate = mode_stats(no_pollinate_rows, "userspace_s", args.min_retain_fraction, args.min_retain_count)
    user_pollinated = mode_stats(pollinated_rows, "userspace_s", args.min_retain_fraction, args.min_retain_count)

    pollinate_cpu_pollinated = mode_stats(
        pollinated_rows,
        "pollinate_cpu_nsec",
        args.min_retain_fraction,
        args.min_retain_count,
    )

    print(f"run_dir={run_dir}")
    print("Boot statistics after IQR filtering (guardrails applied)")
    print_metric_block("Total boot time", total_no_pollinate, total_pollinated, fmt_seconds)
    print_metric_block("Kernel boot time", kernel_no_pollinate, kernel_pollinated, fmt_seconds)
    print_metric_block("Userspace boot time", user_no_pollinate, user_pollinated, fmt_seconds)

    print(f"Pollinate CPU usage ({MODE_POLLINATED} only)")
    print(
        f"  {MODE_POLLINATED} raw_count={pollinate_cpu_pollinated['raw']['count']} "
        f"filtered_count={pollinate_cpu_pollinated['filtered']['count']} "
        f"removed={pollinate_cpu_pollinated['filter']['removed']}"
    )
    print(
        f"    {MODE_POLLINATED} mean="
        f"{fmt_nsec(pollinate_cpu_pollinated['filtered']['mean']) if pollinate_cpu_pollinated['filtered']['mean'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_POLLINATED} median="
        f"{fmt_nsec(pollinate_cpu_pollinated['filtered']['median']) if pollinate_cpu_pollinated['filtered']['median'] is not None else 'n/a'}"
    )
    print(
        f"    {MODE_POLLINATED} stddev="
        f"{fmt_nsec(pollinate_cpu_pollinated['filtered']['stddev']) if pollinate_cpu_pollinated['filtered']['stddev'] is not None else 'n/a'}"
    )


if __name__ == "__main__":
    main()
