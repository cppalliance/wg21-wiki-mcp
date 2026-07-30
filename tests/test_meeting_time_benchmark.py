"""Meeting-time composite latency benchmarks (pytest-benchmark).

Offline fake wiki only. Run selectively::

    pytest tests/test_meeting_time_benchmark.py --benchmark-only --no-cov \\
      -o addopts= --benchmark-warmup=off --benchmark-min-rounds=5
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    FakeCalendar,
    FakeWikiClient,
    run_concurrent_meeting_sessions,
    seed_meeting_pages,
)

from wg21_wiki_mcp import tools
from wg21_wiki_mcp.context import ServerContext

pytestmark = pytest.mark.benchmark

_MEETING_CALENDAR = FakeCalendar(active=True, mode="meeting", ttl_meeting=3600)


@pytest.mark.benchmark(group="meeting-time")
def test_benchmark_meeting_sessions_warm_concurrent(benchmark, fake_client, make_ctx) -> None:
    """Cache-warm parallel get_meeting_sessions (mirrors load-test warm path)."""
    seed_meeting_pages(fake_client)
    ctx = make_ctx(fake_client, calendar=_MEETING_CALENDAR)
    tools.get_meeting_sessions(ctx)

    def burst() -> None:
        run_concurrent_meeting_sessions(ctx, join_timeout=10)
        assert fake_client.page_links_calls == 1

    benchmark.pedantic(burst, rounds=5, warmup_rounds=2)


@pytest.mark.benchmark(group="meeting-time")
def test_benchmark_meeting_sessions_cold_concurrent(benchmark, make_ctx, tmp_path: Path) -> None:
    """Cold parallel get_meeting_sessions with isolated cache per round."""
    round_no = 0

    def setup() -> tuple[tuple[ServerContext, ...], dict[str, object]]:
        nonlocal round_no
        round_no += 1
        client = FakeWikiClient()
        seed_meeting_pages(client)
        cache_parent = tmp_path / f"cold_{round_no}"
        cache_parent.mkdir(parents=True, exist_ok=True)
        ctx = make_ctx(client, calendar=_MEETING_CALENDAR, cache_parent=cache_parent)
        return (ctx,), {}

    def burst(ctx: ServerContext) -> None:
        run_concurrent_meeting_sessions(ctx, join_timeout=15)
        assert ctx.client.page_links_calls == 1  # type: ignore[attr-defined]

    benchmark.pedantic(
        burst,
        setup=setup,
        rounds=5,
        warmup_rounds=0,
    )
