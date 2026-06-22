"""Cross-process shared page cache (SQLite, WAL) in ``~/.isocpp.wiki/``.

Multiple MCP instances (one per local agent) share one cache file. SQLite WAL
allows concurrent readers; writes are short and serialized by SQLite. A per-page
file lock (see :mod:`wg21_wiki_mcp.fetch`) provides cross-process single-flight
so the same page is never fetched twice at once.

Stored content is the exact wikitext, byte-for-byte; it is never mutated.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    requested_title TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    redirected_from TEXT,
    revid           INTEGER,
    timestamp       TEXT,
    size            INTEGER,
    content         TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CacheEntry:
    """One cached page revision: verbatim content plus identity/freshness metadata."""

    requested_title: str
    title: str
    redirected_from: str | None
    revid: int | None
    timestamp: str | None
    size: int | None
    content: str
    fetched_at: str

    def age_seconds(self, now: datetime | None = None) -> float:
        """Return seconds since this entry was fetched (inf if the time is unparseable)."""
        now = now or datetime.now(timezone.utc)
        try:
            fetched = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            return float("inf")
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (now - fetched).total_seconds()


def title_hash(title: str) -> str:
    """Stable filesystem-safe hash of a title (for lock filenames)."""
    return hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]


class Cache:
    """SQLite-backed page cache. Safe for concurrent processes and threads."""

    def __init__(self, cache_dir: Path) -> None:
        """Open (creating if needed) the WAL cache database under ``cache_dir``."""
        self.cache_dir = cache_dir
        self.locks_dir = cache_dir / "locks"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = cache_dir / "cache.sqlite"
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._closed = False
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Return this thread's SQLite connection, opening one on first use."""
        if self._closed:
            raise RuntimeError("Cache is closed")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            with self._all_conns_lock:
                if self._closed:
                    conn.close()
                    raise RuntimeError("Cache is closed")
                self._all_conns.append(conn)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close all thread-local SQLite connections opened by this cache."""
        if self._closed:
            return
        self._closed = True
        with self._all_conns_lock:
            for conn in self._all_conns:
                conn.close()
            self._all_conns.clear()
        if hasattr(self._local, "conn"):
            del self._local.conn

    def __enter__(self) -> Cache:
        """Enter a context that closes this cache on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the cache when leaving the context."""
        self.close()

    def get(self, requested_title: str) -> CacheEntry | None:
        """Return the cached entry for ``requested_title``, or None if absent."""
        row = self._connect().execute("SELECT * FROM pages WHERE requested_title = ?", (requested_title,)).fetchone()
        if row is None:
            return None
        return CacheEntry(
            requested_title=row["requested_title"],
            title=row["title"],
            redirected_from=row["redirected_from"],
            revid=row["revid"],
            timestamp=row["timestamp"],
            size=row["size"],
            content=row["content"],
            fetched_at=row["fetched_at"],
        )

    def put(
        self,
        *,
        requested_title: str,
        title: str,
        redirected_from: str | None,
        revid: int | None,
        timestamp: str | None,
        size: int | None,
        content: str,
        fetched_at: str | None = None,
    ) -> CacheEntry:
        """Insert or replace the cached entry for ``requested_title`` and return it."""
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO pages
                    (requested_title, title, redirected_from, revid, timestamp, size, content, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(requested_title) DO UPDATE SET
                    title=excluded.title,
                    redirected_from=excluded.redirected_from,
                    revid=excluded.revid,
                    timestamp=excluded.timestamp,
                    size=excluded.size,
                    content=excluded.content,
                    fetched_at=excluded.fetched_at
                """,
                (requested_title, title, redirected_from, revid, timestamp, size, content, fetched_at),
            )
        return CacheEntry(requested_title, title, redirected_from, revid, timestamp, size, content, fetched_at)

    def touch(self, requested_title: str, fetched_at: str | None = None) -> None:
        """Mark an entry as freshly validated without changing its content."""
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self._connect().execute(
                "UPDATE pages SET fetched_at = ? WHERE requested_title = ?",
                (fetched_at, requested_title),
            )

    def count(self) -> int:
        """Return the number of cached pages."""
        return int(self._connect().execute("SELECT COUNT(*) FROM pages").fetchone()[0])

    def lock_path(self, requested_title: str) -> Path:
        """Return the cross-process lock file path for a title (hashed filename)."""
        return self.locks_dir / f"{title_hash(requested_title)}.lock"
