"""Meeting-time load tests: concurrent session bundles without deadlock."""

from __future__ import annotations

import pytest
from conftest import FakeCalendar, run_concurrent_meeting_sessions, seed_meeting_pages

from wg21_wiki_mcp import tools

pytestmark = pytest.mark.latency_gate


def test_concurrent_meeting_sessions_no_deadlock(fake_client, make_ctx):
    """Parallel get_meeting_sessions calls complete; outlinks enumerated once."""
    seed_meeting_pages(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting", ttl_meeting=3600))
    tools.get_meeting_sessions(ctx)  # warm outlink cache + page cache
    assert fake_client.page_links_calls == 1

    elapsed = run_concurrent_meeting_sessions(ctx, join_timeout=10)

    assert fake_client.page_links_calls == 1  # no re-enumeration under meeting TTL
    assert elapsed < 5  # cache-warm path should not serialize excessively


def test_concurrent_meeting_sessions_cold_miss(fake_client, make_ctx):
    """Concurrent cold get_meeting_sessions calls single-flight outlink discovery."""
    seed_meeting_pages(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting", ttl_meeting=3600))

    elapsed = run_concurrent_meeting_sessions(ctx, join_timeout=15)

    assert fake_client.page_links_calls == 1
    assert elapsed < 10


def test_meeting_sessions_warm_path_skips_outlinks(fake_client, make_ctx):
    """After the first call, a second call within TTL does not hit page_links."""
    seed_meeting_pages(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting"))
    tools.get_meeting_sessions(ctx, include_wikitext=False)
    links_after_first = fake_client.page_links_calls
    tools.get_meeting_sessions(ctx, include_wikitext=False)
    assert fake_client.page_links_calls == links_after_first
