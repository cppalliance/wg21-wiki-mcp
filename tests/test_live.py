"""Live tier: exercises the real wiki using configured secrets.

These tests are skipped automatically when credentials are absent (forks/local
stay green). They assert structurally and NEVER print or store wiki content, so
nothing confidential leaks into logs or CI artifacts.
"""

from __future__ import annotations

import multiprocessing
import os
import re
from typing import TYPE_CHECKING

import pytest

from wg21_wiki_mcp import tools
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AuthError, ConfigError
from wg21_wiki_mcp.pagination import decode_cursor

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier as BarrierType

pytestmark = pytest.mark.live


def _live_credentials_configured() -> bool:
    """True when credentials are available (honors local ``.env`` like :meth:`Config.from_env`)."""
    try:
        Config.from_env()
        return True
    except ConfigError:
        return False


_HAS_CREDS = _live_credentials_configured()

skip_no_creds = pytest.mark.skipif(not _HAS_CREDS, reason="no wiki credentials configured")

# Generic MediaWiki landing page — not a confidential committee title.
_LIVE_PAGE = "Main Page"


def _resolve_live_meeting_title(ctx: ServerContext) -> str:
    """Discover a meeting title at runtime (no hardcoded confidential titles)."""
    override = os.environ.get("LIVE_MEETING_TITLE")
    if override:
        return override
    listed = tools.list_meetings(ctx, limit=1)
    assert listed.meetings, "expected at least one meeting namespace on the wiki"
    return listed.meetings[0].title


def _live_chunk_page() -> str:
    """Page title for chunk-pagination live test (override when Main Page is too small)."""
    return os.environ.get("LIVE_LARGE_PAGE") or _LIVE_PAGE


def _assert_provenance(page, ctx: ServerContext) -> None:
    prov = page.provenance
    assert prov.revid is not None
    assert prov.url.startswith(ctx.config.base_url)
    assert prov.oldid_url and "oldid=" in prov.oldid_url
    assert isinstance(prov.fetched_at, str) and prov.fetched_at


def _cross_process_fetch_worker(cache_dir: str, title: str, ready: BarrierType) -> bool:
    """Fetch one page in a child process; return whether the result was cache-served."""
    os.environ["ISOCPP_WIKI_CACHE_DIR"] = cache_dir
    ctx = ServerContext.create(Config.from_env())
    try:
        ctx.login()
        ready.wait()
        outcome = ctx.fetcher.get_page(title, ttl_seconds=604800)
        return outcome.from_cache
    finally:
        ctx.close()


# Exception type names emitted by auth_path_failure_label for network/TLS failures.
_NETWORK_AUTH_FAILURE_TYPES = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectTimeout",
        "NewConnectionError",
        "OSError",
        "ProtocolError",
        "ReadTimeout",
        "SSLError",
        "Timeout",
        "TimeoutError",
    }
)
_AUTH_PATH_FAILURE_RE = re.compile(r"\b(?:bot|user): (\w+)")


def _auth_error_is_unreachable(exc: AuthError) -> bool:
    """True when every failed auth path hit a network error (not bad credentials).

    AuthError carries only sanitized type names, not exception chains, so this
    matches the labels produced by auth_path_failure_label in wiki_client.login.
    """
    failure_types = _AUTH_PATH_FAILURE_RE.findall(str(exc))
    return bool(failure_types) and all(name in _NETWORK_AUTH_FAILURE_TYPES for name in failure_types)


def _ensure_wiki_login(ctx: ServerContext) -> None:
    """Log in, or skip live tests when the wiki cannot be reached (not a credential fault)."""
    try:
        ctx.login()
    except AuthError as exc:
        if _auth_error_is_unreachable(exc):
            pytest.skip(
                "wiki.isocpp.org is unreachable from this shell (network error on all auth paths); "
                "credentials loaded but TCP/TLS failed — retry when the wiki is reachable or check VPN/proxy"
            )
        raise


@pytest.fixture(scope="module")
def live_ctx(tmp_path_factory):
    cache_dir = tmp_path_factory.mktemp("live-cache")
    os.environ.setdefault("ISOCPP_WIKI_CACHE_DIR", str(cache_dir))
    ctx = ServerContext.create(Config.from_env())
    try:
        _ensure_wiki_login(ctx)
        yield ctx
    finally:
        ctx.close()


# --- wiki_status -----------------------------------------------------------
@skip_no_creds
def test_login_succeeds(live_ctx):
    status = tools.wiki_status(live_ctx)
    assert status.authenticated is True
    assert status.auth_mode in {"bot", "user"}
    assert status.base_url.startswith("https://")
    assert status.ttl_normal_s > 0
    assert status.ttl_meeting_s > 0
    assert status.current_ttl_s > 0
    assert status.ttl_mode in {"normal", "meeting", "conservative"}
    assert status.calendar.source_url.startswith("https://")


# --- get_page --------------------------------------------------------------
@skip_no_creds
def test_fetch_main_page_has_provenance(live_ctx):
    page = tools.get_page(live_ctx, _LIVE_PAGE)
    assert isinstance(page.content, str) and page.content != ""
    _assert_provenance(page, live_ctx)
    assert page.chunk.has_more is False


@skip_no_creds
def test_get_page_chunk_pagination(live_ctx):
    title = _live_chunk_page()
    page1 = tools.get_page(live_ctx, title, max_bytes=1024)
    assert page1.chunk.total_bytes > 0
    assert page1.chunk.byte_end <= page1.chunk.total_bytes
    if not page1.chunk.has_more:
        pytest.skip(
            f"{title!r} fits in 1 KiB — set LIVE_LARGE_PAGE to a larger page title "
            "to exercise get_page chunk cursor round-trip"
        )
    assert page1.chunk.next_cursor is not None
    decoded = decode_cursor(page1.chunk.next_cursor)
    assert decoded.get("o") == page1.chunk.byte_end
    page2 = tools.get_page(live_ctx, title, max_bytes=1024, cursor=page1.chunk.next_cursor)
    assert page2.chunk.byte_start == page1.chunk.byte_end
    assert isinstance(page2.content, str)
    _assert_provenance(page2, live_ctx)


@skip_no_creds
def test_get_page_section(live_ctx):
    page = tools.get_page(live_ctx, _LIVE_PAGE, section=0)
    assert page.section == 0
    # Section 0 (lead) may legitimately be empty on some pages; provenance is the signal.
    assert isinstance(page.content, str)
    _assert_provenance(page, live_ctx)


@skip_no_creds
def test_get_page_refresh(live_ctx):
    first = tools.get_page(live_ctx, _LIVE_PAGE)
    refreshed = tools.get_page(live_ctx, _LIVE_PAGE, refresh=True)
    assert isinstance(refreshed.content, str) and refreshed.content != ""
    _assert_provenance(refreshed, live_ctx)
    assert refreshed.provenance.revid is not None
    assert first.provenance.revid is not None


# --- search_wiki -----------------------------------------------------------
@skip_no_creds
def test_search_wiki(live_ctx):
    res = tools.search_wiki(live_ctx, "ISO", limit=3)
    assert res.query == "ISO"
    assert isinstance(res.hits, list)
    if res.hits:
        hit = res.hits[0]
        assert hit.title
        assert hit.url.startswith(live_ctx.config.base_url)
        assert hit.snippet_warning == "mediawiki_generated_not_verbatim"


@skip_no_creds
def test_search_wiki_pagination(live_ctx):
    page1 = tools.search_wiki(live_ctx, "C++", limit=2)
    if not page1.next_cursor:
        pytest.skip("not enough search hits to exercise pagination")
    decoded = decode_cursor(page1.next_cursor)
    assert "o" in decoded
    page2 = tools.search_wiki(live_ctx, "C++", limit=2, cursor=page1.next_cursor)
    assert isinstance(page2.hits, list)
    if page1.hits and page2.hits:
        assert page1.hits[0].title != page2.hits[0].title


# --- list_pages / list_namespaces ------------------------------------------
@skip_no_creds
def test_list_pages(live_ctx):
    res = tools.list_pages(live_ctx, 0, limit=2)
    assert res.namespace_id == 0
    assert isinstance(res.pages, list)
    if res.pages:
        assert res.pages[0].title
        assert res.pages[0].url.startswith(live_ctx.config.base_url)


@skip_no_creds
def test_list_pages_pagination(live_ctx):
    page1 = tools.list_pages(live_ctx, 0, limit=1)
    if not page1.next_cursor:
        pytest.skip("not enough pages to exercise pagination")
    decoded = decode_cursor(page1.next_cursor)
    assert "c" in decoded
    page2 = tools.list_pages(live_ctx, 0, limit=1, cursor=page1.next_cursor)
    assert isinstance(page2.pages, list)
    if page1.pages and page2.pages:
        assert page1.pages[0].title != page2.pages[0].title


@skip_no_creds
def test_list_namespaces(live_ctx):
    namespaces = tools.list_namespaces(live_ctx)
    assert isinstance(namespaces, list) and namespaces
    ids = {ns.id for ns in namespaces}
    assert 0 in ids
    assert all(ns.id >= 0 for ns in namespaces)


# --- list_meetings ---------------------------------------------------------
@skip_no_creds
def test_list_meetings(live_ctx):
    res = tools.list_meetings(live_ctx, limit=3)
    assert isinstance(res.meetings, list)
    if res.meetings:
        meeting = res.meetings[0]
        assert meeting.title
        assert meeting.url.startswith(live_ctx.config.base_url)


@skip_no_creds
def test_list_meetings_pagination(live_ctx):
    page1 = tools.list_meetings(live_ctx, limit=1)
    if not page1.next_cursor:
        pytest.skip("not enough meetings to exercise pagination")
    decoded = decode_cursor(page1.next_cursor)
    assert "o" in decoded
    page2 = tools.list_meetings(live_ctx, limit=1, cursor=page1.next_cursor)
    assert isinstance(page2.meetings, list)
    if page1.meetings and page2.meetings:
        assert page1.meetings[0].title != page2.meetings[0].title


# --- get_recent_changes ----------------------------------------------------
@skip_no_creds
def test_recent_changes_reachable(live_ctx):
    changes = tools.get_recent_changes(live_ctx, limit=1)
    assert isinstance(changes.changes, list)
    if changes.changes:
        entry = changes.changes[0]
        assert entry.title
        assert entry.url.startswith(live_ctx.config.base_url)


# --- meeting overview / sessions -------------------------------------------
@skip_no_creds
def test_meeting_overview(live_ctx):
    title = _resolve_live_meeting_title(live_ctx)
    overview = tools.get_meeting_overview(live_ctx, title)
    assert overview.meeting == title
    assert isinstance(overview.home.content, str) and overview.home.content != ""
    _assert_provenance(overview.home, live_ctx)
    assert isinstance(overview.outlinks, list)


@skip_no_creds
def test_meeting_sessions(live_ctx):
    title = _resolve_live_meeting_title(live_ctx)
    bundle = tools.get_meeting_sessions(live_ctx, title, include_wikitext=False)
    assert bundle.meeting == title
    assert bundle.iso_slots_extraction in {"success", "partial", "not_found"}
    assert isinstance(bundle.pages, list)
    assert isinstance(bundle.missing_pages, list)
    if bundle.pages:
        page = bundle.pages[0]
        assert page.title
        assert page.provenance.url.startswith(live_ctx.config.base_url)
        assert page.wikitext is None


# --- cross-process single-flight -------------------------------------------
@skip_no_creds
def test_cross_process_single_flight(tmp_path_factory):
    """Two processes on a cold cache: file lock coalesces to one network fetch."""
    probe = ServerContext.create(Config.from_env())
    try:
        _ensure_wiki_login(probe)
    finally:
        probe.close()

    cache_dir = tmp_path_factory.mktemp("cross-proc-cache")
    os.environ["ISOCPP_WIKI_CACHE_DIR"] = str(cache_dir)
    # Initialize schema once in the parent so spawned workers do not race on SQLite creation.
    warm = Cache(cache_dir)
    warm.close()

    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        ready = manager.Barrier(2)
        with ctx.Pool(2) as pool:
            async_results = [
                pool.apply_async(_cross_process_fetch_worker, (str(cache_dir), _LIVE_PAGE, ready)) for _ in range(2)
            ]
            from_cache_flags = [r.get(timeout=120) for r in async_results]

    assert sorted(from_cache_flags) == [False, True]
