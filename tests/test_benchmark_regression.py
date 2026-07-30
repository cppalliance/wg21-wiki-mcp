"""Tests for scripts/check_benchmark_regression.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_benchmark_regression.py"
_spec = importlib.util.spec_from_file_location("check_benchmark_regression", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
main = _mod.main

_BENCH_CACHE_COUNT = "tests/test_benchmark.py::test_benchmark_cache_count"
_BENCH_MEETING_WARM = "tests/test_meeting_time_benchmark.py::test_benchmark_meeting_sessions_warm_concurrent"
_BENCH_MEETING_COLD = "tests/test_meeting_time_benchmark.py::test_benchmark_meeting_sessions_cold_concurrent"
_BENCH_KEYS = [_BENCH_CACHE_COUNT, _BENCH_MEETING_WARM, _BENCH_MEETING_COLD]


def _bench_json(means: dict[str, float]) -> dict:
    benchmarks = []
    for fullname, mean in means.items():
        benchmarks.append(
            {
                "fullname": fullname,
                "stats": {"mean": mean},
            },
        )
    return {"benchmarks": benchmarks}


@pytest.mark.parametrize("bench_key", _BENCH_KEYS)
def test_regression_gate_passes_within_threshold(tmp_path: Path, bench_key: str) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({bench_key: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({bench_key: 11.0})), encoding="utf-8")

    assert main([str(current), str(baseline), "--max-regression", "0.20"]) == 0


@pytest.mark.parametrize("bench_key", _BENCH_KEYS)
def test_regression_gate_fails_beyond_threshold(tmp_path: Path, bench_key: str) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({bench_key: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({bench_key: 13.0})), encoding="utf-8")

    assert main([str(current), str(baseline), "--max-regression", "0.20"]) == 1


@pytest.mark.parametrize("bench_key", _BENCH_KEYS)
def test_regression_gate_fails_on_missing_benchmark(tmp_path: Path, bench_key: str) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_bench_json({bench_key: 10.0})), encoding="utf-8")
    current.write_text(json.dumps(_bench_json({})), encoding="utf-8")

    assert main([str(current), str(baseline)]) == 1


def test_regression_gate_exits_on_missing_baseline(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_bench_json({_BENCH_CACHE_COUNT: 10.0})), encoding="utf-8")

    with pytest.raises(SystemExit, match="could not read baseline"):
        main([str(current), str(tmp_path / "missing.json")])


def test_regression_gate_exits_on_invalid_json(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text("{not json", encoding="utf-8")
    current.write_text(json.dumps(_bench_json({_BENCH_CACHE_COUNT: 10.0})), encoding="utf-8")

    with pytest.raises(SystemExit, match="could not parse baseline"):
        main([str(current), str(baseline)])


@pytest.mark.parametrize(
    "relative_path",
    [
        "benchmarks/cache-baseline.json",
        "benchmarks/meeting-time-baseline.json",
    ],
)
def test_committed_baseline_matches_ci_runner(relative_path: str) -> None:
    """Guardrail: committed baselines must be from ubuntu-latest / Python 3.12 CI."""
    path = Path(__file__).resolve().parents[1] / relative_path
    data = json.loads(path.read_text(encoding="utf-8"))
    machine = data["machine_info"]
    commit = data["commit_info"]
    assert machine["system"] == "Linux"
    assert machine["python_version"].startswith("3.12")
    assert commit["dirty"] is False

    run_at = datetime.fromisoformat(data["datetime"])
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=UTC)
    commit_at = datetime.fromisoformat(commit["time"].replace("Z", "+00:00"))
    assert abs(run_at - commit_at) < timedelta(days=1)
