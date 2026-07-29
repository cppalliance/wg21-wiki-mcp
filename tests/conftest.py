"""Shared test fixtures: a synthetic in-memory wiki and a context factory.

No real wiki content appears here. All page titles/bodies are invented and
generic so the test suite reveals nothing confidential.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from wg21_wiki_mcp import tools
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.models import CalendarStatus
from wg21_wiki_mcp.wiki_client import FetchedPage

BASE_URL = "https://wiki.example.org"

MEETING_CONCURRENT_WORKERS = 4
MEETING_TEST_TITLE = "2026-06 Alpha"
MEETING_WG_COUNT = 8


def seed_meeting_pages(fake_client: FakeWikiClient) -> None:
    """Populate a fake client with a small meeting page graph for load/benchmark tests."""
    fake_client.pages[MEETING_TEST_TITLE] = FakePage("home", 1)
    for i in range(MEETING_WG_COUNT):
        fake_client.pages[f"{MEETING_TEST_TITLE}:WG{i}"] = FakePage(f"body {i}", i + 2)
    fake_client.allpages = [{"title": MEETING_TEST_TITLE, "ns": 0}]
    fake_client.links[MEETING_TEST_TITLE] = [
        {"title": f"{MEETING_TEST_TITLE}:WG{i}", "ns": 0}
        for i in range(MEETING_WG_COUNT)
    ]


def run_concurrent_meeting_sessions(
    ctx: ServerContext,
    *,
    workers: int = MEETING_CONCURRENT_WORKERS,
    barrier_timeout: float = 5,
    join_timeout: float = 10,
) -> float:
    """Run parallel ``get_meeting_sessions`` calls; return monotonic elapsed seconds."""
    errors: list[BaseException] = []
    ready = threading.Barrier(workers)

    def worker() -> None:
        try:
            ready.wait(timeout=barrier_timeout)
            tools.get_meeting_sessions(ctx)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    start = time.monotonic()
    for thread in threads:
        thread.start()
    deadline = start + join_timeout
    for thread in threads:
        remaining = deadline - time.monotonic()
        thread.join(timeout=remaining if remaining > 0 else 0)
    elapsed = time.monotonic() - start

    if any(thread.is_alive() for thread in threads):
        raise AssertionError("worker threads still alive after join timeout")
    if errors:
        raise AssertionError(errors)
    return elapsed


@dataclass
class FakePage:
    content: str
    revid: int
    timestamp: str = "2026-06-01T00:00:00Z"


class FakeWikiClient:
    """In-memory stand-in for WikiClient with call counters for assertions."""

    def __init__(self) -> None:
        self.pages: dict[str, FakePage] = {}
        self.redirects: dict[str, str] = {}
        self.normalized: dict[str, str] = {}
        self.namespaces: dict[str, dict] = {"0": {"*": "", "canonical": ""}}
        self.search_results: list[dict] = []
        self.recent: list[dict] = []
        self.links: dict[str, list[dict]] = {}
        self.allpages: list[dict] = []
        self.fetch_calls = 0
        self.page_links_calls = 0
        self.fetch_title_batches: list[list[str]] = []
        self.section_fetch_calls = 0
        self.revision_calls = 0
        self._active = "bot"
        self._user = "TestBot@ci"
        self._closed = False

    def close(self) -> None:
        self._closed = True

    # auth surface
    @property
    def active_label(self) -> str | None:
        return self._active

    @property
    def username(self) -> str | None:
        return self._user

    def login(self) -> None:  # pragma: no cover - trivial
        pass

    # URL helpers (mirror WikiClient formulas)
    def canonical_url(self, title: str) -> str:
        return f"{BASE_URL}/index.php?title={quote(title.replace(' ', '_'), safe=':/')}"

    def oldid_url(self, title: str, revid: int | None) -> str | None:
        if revid is None:
            return None
        return f"{self.canonical_url(title)}&oldid={revid}"

    # resolution mirrors WikiClient._map_batch
    def _resolve(self, title: str) -> tuple[str, str | None]:
        redirected_from = None
        current = self.normalized.get(title, title)
        seen: set[str] = set()
        while current in self.redirects and current not in seen:
            seen.add(current)
            redirected_from = redirected_from or title
            current = self.redirects[current]
        return current, redirected_from

    def fetch_pages(
        self, titles: list[str], *, timeout: float | None = None
    ) -> dict[str, FetchedPage]:
        self.fetch_calls += 1
        self.fetch_title_batches.append(list(titles))
        out: dict[str, FetchedPage] = {}
        for req in titles:
            final, redirected_from = self._resolve(req)
            page = self.pages.get(final)
            if page is None:
                out[req] = FetchedPage(
                    req, final, redirected_from, None, None, None, None, True
                )
                continue
            out[req] = FetchedPage(
                requested_title=req,
                title=final,
                redirected_from=redirected_from,
                revid=page.revid,
                timestamp=page.timestamp,
                size=len(page.content.encode("utf-8")),
                content=page.content,
                missing=False,
            )
        return out

    def fetch_page_section(self, title: str, section: int) -> FetchedPage:
        self.section_fetch_calls += 1
        final, redirected_from = self._resolve(title)
        page = self.pages.get(final)
        if page is None:
            return FetchedPage(
                title, final, redirected_from, None, None, None, None, True
            )
        body = f"== section {section} ==\n{page.content}"
        return FetchedPage(
            requested_title=title,
            title=final,
            redirected_from=redirected_from,
            revid=page.revid,
            timestamp=page.timestamp,
            size=len(body.encode("utf-8")),
            content=body,
            missing=False,
        )

    def page_revisions(
        self, titles: list[str], *, timeout: float | None = None
    ) -> dict[str, int | None]:
        self.revision_calls += 1
        out: dict[str, int | None] = {}
        for req in titles:
            final, _ = self._resolve(req)
            page = self.pages.get(final)
            out[req] = page.revid if page else None
        return out

    def search(
        self, query: str, *, limit: int, namespace: int | None, offset: int
    ) -> dict:
        window = self.search_results[offset : offset + limit]
        resp: dict = {"query": {"search": window}}
        if offset + limit < len(self.search_results):
            resp["continue"] = {"sroffset": offset + limit}
        return resp

    def list_pages(
        self, *, namespace: int, prefix: str | None, limit: int, cont: str | None
    ) -> dict:
        start = int(cont) if cont else 0
        pool = [
            p for p in self.allpages if (not prefix or p["title"].startswith(prefix))
        ]
        window = pool[start : start + limit]
        resp: dict = {"query": {"allpages": window}}
        if start + limit < len(pool):
            resp["continue"] = {"apcontinue": str(start + limit)}
        return resp

    def list_namespaces(self) -> dict:
        return {"query": {"namespaces": self.namespaces}}

    def recent_changes(self, *, namespace, since, limit, cont) -> dict:
        start = int(cont) if cont else 0
        window = self.recent[start : start + limit]
        resp: dict = {"query": {"recentchanges": window}}
        if start + limit < len(self.recent):
            resp["continue"] = {"rccontinue": str(start + limit)}
        return resp

    def page_links(
        self, title: str, *, limit: int, cont: str | None, timeout: float | None = None
    ) -> dict:
        self.page_links_calls += 1
        pool = self.links.get(title, [])
        start = int(cont) if cont else 0
        window = pool[start : start + limit]
        resp: dict = {"query": {"pages": {"1": {"links": window}}}}
        if start + limit < len(pool):
            resp["continue"] = {"plcontinue": str(start + limit)}
        return resp


@dataclass
class FakeCalendar:
    """Deterministic calendar for tests (no network)."""

    active: bool = False
    mode: str = "normal"
    ttl_normal: int = 604800
    ttl_meeting: int = 3600
    meeting_windows: dict[str, tuple[str, str]] | None = None
    _closed: bool = False

    def is_meeting_active(self, when=None) -> bool:
        return self.active

    def ttl_seconds(self, when=None) -> int:
        return self.ttl_meeting if self.active else self.ttl_normal

    def ttl_mode(self, when=None) -> str:
        return self.mode

    def window_for_meeting_title(self, title: str) -> tuple[str | None, str | None]:
        if not self.meeting_windows or len(title) < 7 or title[4] != "-":
            return None, None
        return self.meeting_windows.get(title[:7], (None, None))

    def status(self) -> CalendarStatus:
        return CalendarStatus(
            source_url="https://example.org/meetings",
            last_fetched=datetime.now(timezone.utc).isoformat(),
            parse_status="ok",
            in_meeting_window_now=self.active,
            windows=[],
        )

    def close(self) -> None:
        self._closed = True


def make_config(tmp_path: Path) -> Config:
    return Config(
        base_url=BASE_URL,
        bot=Credentials("bot", "TestBot@ci", "secret"),
        user=None,
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def fake_client() -> FakeWikiClient:
    return FakeWikiClient()


@pytest.fixture
def make_ctx(tmp_path: Path):
    contexts: list[ServerContext] = []

    def _make(
        client: FakeWikiClient,
        *,
        calendar: FakeCalendar | None = None,
        cache_parent: Path | None = None,
    ) -> ServerContext:
        config = make_config(cache_parent or tmp_path)
        cache = Cache(config.cache_dir)
        ctx = ServerContext(
            config=config,
            client=client,  # type: ignore[arg-type]
            calendar=calendar or FakeCalendar(),  # type: ignore[arg-type]
            cache=cache,
            fetcher=PageFetcher(client, cache),  # type: ignore[arg-type]
        )
        contexts.append(ctx)
        return ctx

    yield _make
    first_error: BaseException | None = None
    for ctx in contexts:
        try:
            ctx.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error
