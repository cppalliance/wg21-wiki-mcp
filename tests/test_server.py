"""Server wiring tests (no network)."""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeCalendar, FakePage, FakeWikiClient, make_config

from wg21_wiki_mcp import server
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.fetch import PageFetcher


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "search_wiki",
        "get_page",
        "list_pages",
        "list_namespaces",
        "list_meetings",
        "get_meeting_overview",
        "get_meeting_sessions",
        "get_recent_changes",
        "wiki_status",
    }


def test_get_context_builds_from_env(monkeypatch, tmp_path):
    server._state.pop("ctx", None)
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))
    try:
        ctx = server.get_context()
        assert ctx.config.base_url == "https://wiki.isocpp.org"
        assert server.get_context() is ctx  # cached
    finally:
        ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()


def _fake_ctx(tmp_path) -> ServerContext:
    client = FakeWikiClient()
    client.pages["2026-06 Alpha"] = FakePage("home", 1)
    client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    client.search_results = [{"title": "Hit", "ns": 0}]
    config = make_config(tmp_path)
    cache = Cache(config.cache_dir)
    return ServerContext(
        config=config,
        client=client,
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        cache=cache,
        fetcher=PageFetcher(client, cache),  # type: ignore[arg-type]
    )


def test_tool_wrappers_delegate(monkeypatch, tmp_path):
    ctx = _fake_ctx(tmp_path)
    monkeypatch.setattr(server, "get_context", lambda: ctx)
    try:
        assert server.search_wiki("q").hits[0].title == "Hit"
        assert server.get_page("2026-06 Alpha").content == "home"
        assert server.list_pages(0).pages[0].title == "2026-06 Alpha"
        assert isinstance(server.list_namespaces(), list)
        assert server.list_meetings().meetings[0].title == "2026-06 Alpha"
        assert server.get_meeting_overview().meeting == "2026-06 Alpha"
        assert server.get_meeting_sessions().meeting == "2026-06 Alpha"
        assert server.get_recent_changes().changes == []
        assert server.wiki_status().authenticated is True
    finally:
        ctx.close()


def test_lifespan_runs(monkeypatch, tmp_path):
    ctx = _fake_ctx(tmp_path)

    def _get_ctx() -> ServerContext:
        server._state["ctx"] = ctx
        return ctx

    monkeypatch.setattr(server, "get_context", _get_ctx)

    async def run():
        async with server._lifespan(server.mcp):
            return True

    assert asyncio.run(run()) is True


def test_wrap_passthrough_mcp_error():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    from wg21_wiki_mcp.server import _wrap

    def _raise_mcp() -> None:
        raise McpError(ErrorData(code=-32602, message="bad params"))

    with pytest.raises(McpError):
        _wrap(_raise_mcp)


def test_wrap_converts_unexpected_exception():
    from mcp.shared.exceptions import McpError

    from wg21_wiki_mcp.server import _wrap

    def _boom() -> None:
        raise ValueError("unexpected")

    with pytest.raises(McpError):
        _wrap(_boom)


def test_main_runs_mcp(monkeypatch):
    called = False

    def _run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(server.mcp, "run", _run)
    server.main()
    assert called is True
