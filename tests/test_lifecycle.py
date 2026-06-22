"""Resource lifecycle, shutdown, and observability tests."""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from unittest.mock import MagicMock, patch

import pytest
from conftest import FakeCalendar, FakePage, FakeWikiClient, make_config
from filelock import Timeout

from wg21_wiki_mcp import server
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.meetings import MeetingCalendar
from wg21_wiki_mcp.tools import wiki_status
from wg21_wiki_mcp.wiki_client import WikiClient


def _ctx(tmp_path) -> ServerContext:
    client = FakeWikiClient()
    config = make_config(tmp_path)
    cache = Cache(config.cache_dir)
    return ServerContext(
        config=config,
        client=client,  # type: ignore[arg-type]
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        cache=cache,
        fetcher=PageFetcher(client, cache),  # type: ignore[arg-type]
    )


def test_cache_close_no_resource_warning(tmp_path):
    cache = Cache(tmp_path / "c")
    cache.put(
        requested_title="X",
        title="X",
        redirected_from=None,
        revid=1,
        timestamp=None,
        size=1,
        content="hi",
    )
    cache.get("X")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        cache.close()
        cache.close()
    assert not any(issubclass(w.category, ResourceWarning) for w in caught)


def test_cache_context_manager(tmp_path):
    with Cache(tmp_path / "c") as cache:
        cache.put(
            requested_title="X",
            title="X",
            redirected_from=None,
            revid=1,
            timestamp=None,
            size=1,
            content="hi",
        )
    with pytest.raises(RuntimeError, match="closed"):
        cache.get("X")


def test_server_context_close(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.cache.get("probe")  # open sqlite on this thread
    ctx.close()
    assert ctx.cache._closed is True
    assert ctx.calendar._closed is True
    assert ctx.client._closed is True


def test_server_context_close_continues_after_failure(tmp_path):
    ctx = _ctx(tmp_path)

    def _boom() -> None:
        raise RuntimeError("cache close failed")

    ctx.cache.close = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cache close failed"):
        ctx.close()
    assert ctx.calendar._closed is True
    assert ctx.client._closed is True


def test_server_context_close_raises_first_calendar_failure(tmp_path):
    ctx = _ctx(tmp_path)

    def _calendar_boom() -> None:
        raise RuntimeError("calendar close failed")

    ctx.calendar.close = _calendar_boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="calendar close failed"):
        ctx.close()
    assert ctx.cache._closed is True
    assert ctx.client._closed is True


def test_server_context_close_raises_first_client_failure(tmp_path):
    ctx = _ctx(tmp_path)

    def _client_boom() -> None:
        raise RuntimeError("client close failed")

    ctx.client.close = _client_boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="client close failed"):
        ctx.close()
    assert ctx.cache._closed is True
    assert ctx.calendar._closed is True


def test_lifespan_closes_context(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    closed = False
    real_close = ctx.close

    def _tracked_close() -> None:
        nonlocal closed
        closed = True
        real_close()

    monkeypatch.setattr(ctx, "close", _tracked_close)

    def _get_ctx() -> ServerContext:
        server._state["ctx"] = ctx
        return ctx

    monkeypatch.setattr(server, "get_context", _get_ctx)

    async def run() -> None:
        async with server._lifespan(server.mcp):
            pass

    asyncio.run(run())
    assert closed is True
    assert "ctx" not in server._state


def test_inproc_locks_evicted_after_fetch(tmp_path):
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]
    for i in range(50):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)
        fetcher.get_page(f"P{i}", ttl_seconds=1000)
    assert len(fetcher._inproc_locks) == 0
    cache.close()


def test_inproc_lock_capacity_eviction(tmp_path, monkeypatch):
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]
    monkeypatch.setattr("wg21_wiki_mcp.fetch._MAX_INPROC_LOCK_ENTRIES", 1)

    for title in ("P0", "P1"):
        client.pages[title] = FakePage(f"body-{title}", 1)
        fetcher.get_page(title, ttl_seconds=1000)

    assert len(fetcher._inproc_locks) <= 1
    cache.close()


def test_inproc_lock_single_flight_under_slow_fetch(tmp_path):
    """Concurrent get_page on one title during a slow fetch coalesces to one network call."""
    client = FakeWikiClient()
    client.pages["P"] = FakePage("body", 1)
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]

    inside_fetch = threading.Event()
    allow_finish = threading.Event()
    real_fetch = client.fetch_pages

    def gated_fetch(titles: list[str]):
        inside_fetch.set()
        assert allow_finish.wait(timeout=5)
        return real_fetch(titles)

    client.fetch_pages = gated_fetch  # type: ignore[method-assign]

    holder_error: list[BaseException] = []
    waiter_error: list[BaseException] = []

    def holder() -> None:
        try:
            fetcher.get_page("P", ttl_seconds=1000)
        except BaseException as exc:
            holder_error.append(exc)

    def waiter() -> None:
        try:
            fetcher.get_page("P", ttl_seconds=1000)
        except BaseException as exc:
            waiter_error.append(exc)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert inside_fetch.wait(timeout=5)

    with fetcher._inproc_guard:
        slot = fetcher._inproc_locks["P"]
        lock_while_held = slot.lock
        assert slot.users >= 1

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()

    for _ in range(100):
        with fetcher._inproc_guard:
            slot = fetcher._inproc_locks.get("P")
            if slot is not None and slot.users >= 2 and slot.lock is lock_while_held:
                break
        threading.Event().wait(0.01)
    else:
        raise AssertionError("waiter never joined the in-process lock slot")

    allow_finish.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)

    assert not holder_error
    assert not waiter_error
    assert client.fetch_calls == 1
    cache.close()


def test_lock_timeout_logs_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.fetch")
    client = FakeWikiClient()
    client.pages["P"] = FakePage("body", 1)
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]

    with patch("wg21_wiki_mcp.fetch.FileLock") as mock_fl:
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = Timeout("filelock")
        mock_fl.return_value = mock_lock
        fetcher.get_page("P", ttl_seconds=1000)

    assert any("lock timeout" in r.message.lower() for r in caplog.records)
    cache.close()


def test_calendar_fetch_failure_logs(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.meetings")

    class _BrokenSession:
        headers: dict[str, str] = {}

        def get(self, *_a, **_k):
            raise RuntimeError("network down")

    cal = MeetingCalendar(make_config(tmp_path), session=_BrokenSession())
    cal.is_meeting_active()
    assert any("calendar fetch" in r.message.lower() for r in caplog.records)


def test_wiki_status_cache_count_failure_logs(make_ctx, fake_client, caplog):
    caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.tools")
    ctx = make_ctx(fake_client)

    def _boom() -> int:
        raise RuntimeError("db broken")

    ctx.cache.count = _boom  # type: ignore[method-assign]
    status = wiki_status(ctx)
    assert status.cache_entries is None
    assert any("cache count" in r.message.lower() for r in caplog.records)


def test_meeting_calendar_close_owned_session(tmp_path):
    with patch("wg21_wiki_mcp.meetings.requests.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session
        cal = MeetingCalendar(make_config(tmp_path))
        cal.close()
        session.close.assert_called_once()


def test_wiki_client_close(tmp_path):
    client = WikiClient(make_config(tmp_path))
    site = MagicMock()
    client._site = site  # type: ignore[attr-defined]
    client.close()
    site.connection.close.assert_called_once()
    assert client._site is None


def test_wiki_client_close_clears_state_when_connection_close_fails(tmp_path):
    client = WikiClient(make_config(tmp_path))
    site = MagicMock()
    site.connection.close.side_effect = RuntimeError("close failed")
    client._site = site  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="close failed"):
        client.close()

    assert client._site is None
    assert client._active is None
    assert client._closed is True
    client.close()  # idempotent after failed close


def test_wiki_client_use_after_close_raises(tmp_path):
    client = WikiClient(make_config(tmp_path))
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.login()
    with pytest.raises(RuntimeError, match="closed"):
        client.api("query", meta="siteinfo")


def test_wiki_client_username_before_login(tmp_path):
    client = WikiClient(make_config(tmp_path))
    assert client.username is None
    client.close()


def test_wiki_client_close_idempotent(tmp_path):
    client = WikiClient(make_config(tmp_path))
    client.close()
    client.close()
    assert client._closed is True


def test_get_logger_package_name():
    from wg21_wiki_mcp.log import get_logger

    assert get_logger("wg21_wiki_mcp.fetch").name == "wg21_wiki_mcp.fetch"
    assert get_logger("fetch").name == "wg21_wiki_mcp.fetch"
    assert get_logger("wg21_wiki_mcp_other").name == "wg21_wiki_mcp.wg21_wiki_mcp_other"
