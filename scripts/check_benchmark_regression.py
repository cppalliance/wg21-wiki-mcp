#!/usr/bin/env python3
"""Fail when benchmark means regress more than a threshold vs a committed baseline.

Compares pytest-benchmark ``--benchmark-json`` output against a committed baseline
(e.g. ``benchmarks/cache-baseline.json`` or ``benchmarks/meeting-time-baseline.json``).
Used in CI (see the ``benchmark`` job).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_MAX_REGRESSION = 0.20


def _load_benchmark_json(path: Path, label: str) -> dict:
    """Load a pytest-benchmark JSON file, raising ``SystemExit`` on I/O or parse errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"could not read {label} benchmark JSON at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {label} benchmark JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{label} benchmark JSON at {path} must be a JSON object")
    return data


def _means_by_fullname(data: dict) -> dict[str, float]:
    benchmarks = data.get("benchmarks") or []
    means: dict[str, float] = {}
    for index, entry in enumerate(benchmarks):
        if not isinstance(entry, dict):
            raise SystemExit(f"benchmark entry at index {index} must be a JSON object")
        try:
            fullname = entry["fullname"]
            mean = float(entry["stats"]["mean"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"benchmark entry at index {index} is missing fullname/stats.mean: {exc}") from exc
        means[str(fullname)] = mean
    return means


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path, help="pytest-benchmark JSON from the current run")
    parser.add_argument("baseline", type=Path, help="committed baseline JSON")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=_DEFAULT_MAX_REGRESSION,
        help=f"max allowed mean slowdown as a fraction (default: {_DEFAULT_MAX_REGRESSION})",
    )
    args = parser.parse_args(argv)

    current = _load_benchmark_json(args.current, "current")
    baseline = _load_benchmark_json(args.baseline, "baseline")
    cur_means = _means_by_fullname(current)
    base_means = _means_by_fullname(baseline)

    regressions: list[str] = []
    for name, base_mean in sorted(base_means.items()):
        cur_mean = cur_means.get(name)
        if cur_mean is None:
            regressions.append(f"{name}: missing from current run")
            continue
        if base_mean <= 0:
            continue
        delta = (cur_mean - base_mean) / base_mean
        if delta > args.max_regression:
            regressions.append(
                f"{name}: mean {cur_mean:.6f}s vs baseline {base_mean:.6f}s (+{delta * 100:.1f}%)",
            )

    if regressions:
        for line in regressions:
            print(f"REGRESSION: {line}", file=sys.stderr)
        return 1

    print(f"OK: all benchmarks within {args.max_regression * 100:.0f}% mean regression threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
