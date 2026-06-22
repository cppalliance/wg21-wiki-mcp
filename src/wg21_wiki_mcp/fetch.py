"""The centralized page-fetch chokepoint.

Every tool that needs page wikitext goes through :class:`PageFetcher` - no tool
calls the wiki client directly. This guarantees uniform caching, provenance,
re-login, and load control. Cache-missing fetches are:

* **batched** - many titles per ``titles=`` query (the primary parallelization,
  cutting both latency and server load),
* **single-flighted** - a per-page file lock (cross-process) plus an in-process
  lock map prevents two fetches of the same page at once,
* **revalidated cheaply** - a stale cache entry is confirmed via a revid check
  before a full re-fetch, so unchanged pages cost almost nothing.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from .cache import Cache, CacheEntry, title_hash
from .log import get_logger
from .log_safety import safe_exception_summary
from .wiki_client import FetchedPage, WikiClient

logger = get_logger("fetch")

_LOCK_TIMEOUT_S = 60
# Idle in-process lock slots are evicted once the map exceeds this size.
_MAX_INPROC_LOCK_ENTRIES = 256
_SECTION_KEY_SEP = "\0section="


def section_cache_key(title: str, section: int) -> str:
    """Return the cache/lock key for a page section (distinct from full-page keys)."""
    return f"{title}{_SECTION_KEY_SEP}{section}"


@dataclass
class _InprocLockSlot:
    """Per-title in-process lock with a user count for safe map eviction."""

    lock: threading.Lock
    users: int = 0


@dataclass(frozen=True)
class FetchOutcome:
    """Resolved page result, whether served from cache or freshly fetched."""

    requested_title: str
    title: str
    redirected_from: str | None
    revid: int | None
    timestamp: str | None
    size: int | None
    content: str | None
    fetched_at: str
    from_cache: bool
    missing: bool


class PageFetcher:
    """Cache-first, batched, single-flight page retrieval."""

    def __init__(self, client: WikiClient, cache: Cache) -> None:
        """Wrap a wiki client and shared cache behind the single fetch chokepoint."""
        self._client = client
        self._cache = cache
        self._inproc_locks: dict[str, _InprocLockSlot] = {}
        self._inproc_guard = threading.Lock()

    def get_page(self, title: str, *, ttl_seconds: int, refresh: bool = False) -> FetchOutcome:
        """Resolve a single page (cache-first, then fetch). See :meth:`get_pages`."""
        return self.get_pages([title], ttl_seconds=ttl_seconds, refresh=refresh)[title]

    def get_page_section(
        self,
        title: str,
        section: int,
        *,
        ttl_seconds: int,
        refresh: bool = False,
    ) -> FetchOutcome:
        """Resolve one page section (cache-first, then ``rvsection`` fetch)."""
        key = section_cache_key(title, section)
        now = datetime.now(timezone.utc)
        entry = None if refresh else self._cache.get(key)
        if entry is not None and entry.age_seconds(now) < ttl_seconds:
            return _section_outcome(_from_entry(entry, from_cache=True), title)

        stale = entry
        with ExitStack() as stack:
            self._acquire_inproc(stack, [key])
            self._acquire_cross_process(stack, [key])

            if not refresh:
                entry = self._cache.get(key)
                if entry is not None and entry.age_seconds() < ttl_seconds:
                    return _section_outcome(_from_entry(entry, from_cache=True), title)
                if entry is not None:
                    stale = entry

            if stale is not None and not refresh and stale.revid is not None:
                current = self._client.page_revisions([title])
                if current.get(title) is not None and current[title] == stale.revid:
                    self._cache.touch(key)
                    refreshed = self._cache.get(key) or stale
                    return _section_outcome(_from_entry(refreshed, from_cache=True), title)

            fetched_at = datetime.now(timezone.utc).isoformat()
            page = self._client.fetch_page_section(title, section)
            if page.missing or page.content is None:
                return _section_outcome(_missing(title, page, fetched_at), title)

            entry = self._cache.put(
                requested_title=key,
                title=page.title,
                redirected_from=page.redirected_from,
                revid=page.revid,
                timestamp=page.timestamp,
                size=page.size,
                content=page.content,
                fetched_at=fetched_at,
            )
            return _section_outcome(_from_entry(entry, from_cache=False), title)

    def get_pages(self, titles: list[str], *, ttl_seconds: int, refresh: bool = False) -> dict[str, FetchOutcome]:
        """Resolve many titles at once, cache-first then batched network fetch."""
        now = datetime.now(timezone.utc)
        outcomes: dict[str, FetchOutcome] = {}
        need_network: list[str] = []
        stale: dict[str, CacheEntry] = {}

        for title in dict.fromkeys(titles):  # de-dupe, preserve order
            entry = None if refresh else self._cache.get(title)
            if entry is not None and entry.age_seconds(now) < ttl_seconds:
                outcomes[title] = _from_entry(entry, from_cache=True)
            else:
                need_network.append(title)
                if entry is not None:
                    stale[title] = entry

        if need_network:
            self._resolve_network(need_network, stale, ttl_seconds, refresh, outcomes)
        return outcomes

    def _resolve_network(
        self,
        titles: list[str],
        stale: dict[str, CacheEntry],
        ttl_seconds: int,
        refresh: bool,
        outcomes: dict[str, FetchOutcome],
    ) -> None:
        with ExitStack() as stack:
            self._acquire_inproc(stack, sorted(titles))
            self._acquire_cross_process(stack, sorted(titles))

            # Re-check the cache: another process may have filled it while we waited.
            still: list[str] = []
            for title in titles:
                if not refresh:
                    entry = self._cache.get(title)
                    if entry is not None and entry.age_seconds() < ttl_seconds:
                        outcomes[title] = _from_entry(entry, from_cache=True)
                        continue
                still.append(title)

            to_fetch = self._revalidate(still, stale, refresh, outcomes)
            self._fetch_and_store(sorted(to_fetch), outcomes)

    def _revalidate(
        self,
        titles: list[str],
        stale: dict[str, CacheEntry],
        refresh: bool,
        outcomes: dict[str, FetchOutcome],
    ) -> set[str]:
        """Cheap revid check: unchanged stale entries are touched and served."""
        to_fetch = set(titles)
        candidates = [t for t in titles if not refresh and t in stale and stale[t].revid is not None]
        if not candidates:
            return to_fetch
        current = self._client.page_revisions(candidates)
        for title in candidates:
            entry = stale[title]
            if current.get(title) is not None and current[title] == entry.revid:
                self._cache.touch(title)
                refreshed = self._cache.get(title) or entry
                outcomes[title] = _from_entry(refreshed, from_cache=True)
                to_fetch.discard(title)
        return to_fetch

    def _fetch_and_store(self, titles: list[str], outcomes: dict[str, FetchOutcome]) -> None:
        if not titles:
            return
        fetched_at = datetime.now(timezone.utc).isoformat()
        fetched = self._client.fetch_pages(titles)  # batched (<=50 per request)
        for title in titles:
            page = fetched.get(title)
            if page is None or page.missing or page.content is None:
                outcomes[title] = _missing(title, page, fetched_at)
                continue
            entry = self._cache.put(
                requested_title=title,
                title=page.title,
                redirected_from=page.redirected_from,
                revid=page.revid,
                timestamp=page.timestamp,
                size=page.size,
                content=page.content,
                fetched_at=fetched_at,
            )
            outcomes[title] = _from_entry(entry, from_cache=False)

    # -- locking helpers ----------------------------------------------------
    def _acquire_inproc(self, stack: ExitStack, titles: list[str]) -> None:
        for title in titles:
            with self._inproc_guard:
                slot = self._inproc_locks.get(title)
                if slot is None:
                    slot = _InprocLockSlot(threading.Lock())
                    self._inproc_locks[title] = slot
                slot.users += 1
                lock = slot.lock
            lock.acquire()
            stack.callback(self._release_inproc, title, lock)

    def _release_inproc(self, title: str, lock: threading.Lock) -> None:
        lock.release()
        with self._inproc_guard:
            slot = self._inproc_locks.get(title)
            if slot is None or slot.lock is not lock:
                return
            slot.users -= 1
            if slot.users == 0 and not lock.locked():
                del self._inproc_locks[title]
            self._evict_idle_inproc_locks()

    def _evict_idle_inproc_locks(self) -> None:
        """Drop unused lock slots when the map grows past its capacity bound."""
        if len(self._inproc_locks) <= _MAX_INPROC_LOCK_ENTRIES:
            return
        for key, slot in list(self._inproc_locks.items()):
            if slot.users == 0 and not slot.lock.locked():
                del self._inproc_locks[key]
            if len(self._inproc_locks) <= _MAX_INPROC_LOCK_ENTRIES:
                return

    def _acquire_cross_process(self, stack: ExitStack, titles: list[str]) -> None:
        for title in titles:
            lock = FileLock(str(self._cache.lock_path(title)))
            try:
                lock.acquire(timeout=_LOCK_TIMEOUT_S)
                stack.enter_context(_released(lock))
            except Timeout as exc:
                # Another process is taking unusually long; proceed without the
                # cross-process lock rather than hang. The re-check after this
                # still prevents redundant work in the common case.
                logger.warning(
                    "Cross-process lock timeout (title_hash=%s): %s",
                    title_hash(title),
                    safe_exception_summary(exc),
                )
                continue


class _released:
    """Context manager that releases an already-acquired FileLock on exit."""

    def __init__(self, lock: FileLock) -> None:
        self._lock = lock

    def __enter__(self) -> FileLock:
        return self._lock

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def _from_entry(entry: CacheEntry, *, from_cache: bool) -> FetchOutcome:
    return FetchOutcome(
        requested_title=entry.requested_title,
        title=entry.title,
        redirected_from=entry.redirected_from,
        revid=entry.revid,
        timestamp=entry.timestamp,
        size=entry.size,
        content=entry.content,
        fetched_at=entry.fetched_at,
        from_cache=from_cache,
        missing=False,
    )


def _section_outcome(outcome: FetchOutcome, title: str) -> FetchOutcome:
    """Restore the caller's page title (cache keys embed the section suffix)."""
    return FetchOutcome(
        requested_title=title,
        title=outcome.title,
        redirected_from=outcome.redirected_from,
        revid=outcome.revid,
        timestamp=outcome.timestamp,
        size=outcome.size,
        content=outcome.content,
        fetched_at=outcome.fetched_at,
        from_cache=outcome.from_cache,
        missing=outcome.missing,
    )


def _missing(title: str, page: FetchedPage | None, fetched_at: str) -> FetchOutcome:
    return FetchOutcome(
        requested_title=title,
        title=page.title if page else title,
        redirected_from=page.redirected_from if page else None,
        revid=None,
        timestamp=None,
        size=None,
        content=None,
        fetched_at=fetched_at,
        from_cache=False,
        missing=True,
    )
