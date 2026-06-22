"""Cache persistence + cross-instance sharing tests."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from wg21_wiki_mcp.cache import Cache, title_hash


def test_put_get_roundtrip(tmp_path):
    with Cache(tmp_path / "c") as cache:
        cache.put(
            requested_title="A B",
            title="A B",
            redirected_from=None,
            revid=7,
            timestamp="2026-06-01T00:00:00Z",
            size=3,
            content="café",
        )
        entry = cache.get("A B")
        assert entry is not None
        assert entry.content == "café"
        assert entry.revid == 7
        assert cache.count() == 1


def test_shared_across_instances(tmp_path):
    with Cache(tmp_path / "c") as a:
        a.put(requested_title="X", title="X", redirected_from=None, revid=1, timestamp=None, size=1, content="hi")
        with Cache(tmp_path / "c") as b:
            assert b.get("X").content == "hi"


def test_age_and_touch(tmp_path):
    with Cache(tmp_path / "c") as cache:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        cache.put(
            requested_title="X",
            title="X",
            redirected_from=None,
            revid=1,
            timestamp=None,
            size=1,
            content="hi",
            fetched_at=old,
        )
        assert cache.get("X").age_seconds() > 3600
        cache.touch("X")
        assert cache.get("X").age_seconds() < 60


def test_missing_returns_none(tmp_path):
    with Cache(tmp_path / "c") as cache:
        assert cache.get("nope") is None


def test_age_seconds_edge_cases():
    from wg21_wiki_mcp.cache import CacheEntry

    bad = CacheEntry("X", "X", None, 1, None, 1, "hi", "not-a-timestamp")
    assert bad.age_seconds() == float("inf")
    naive = CacheEntry("X", "X", None, 1, None, 1, "hi", "2020-01-01T00:00:00")
    assert naive.age_seconds() > 0  # naive timestamp treated as UTC, no crash


def test_lock_path_uses_hash(tmp_path):
    with Cache(tmp_path / "c") as cache:
        p = cache.lock_path("Some:Weird/Title*With?Chars")
        assert p.name == f"{title_hash('Some:Weird/Title*With?Chars')}.lock"
        assert p.parent == cache.locks_dir


def test_update_overwrites(tmp_path):
    with Cache(tmp_path / "c") as cache:
        cache.put(requested_title="X", title="X", redirected_from=None, revid=1, timestamp=None, size=1, content="v1")
        cache.put(requested_title="X", title="X", redirected_from=None, revid=2, timestamp=None, size=1, content="v2")
        assert cache.get("X").content == "v2"
        assert cache.get("X").revid == 2
        assert cache.count() == 1


def test_connect_after_close_raises(tmp_path):
    cache = Cache(tmp_path / "c")
    cache.close()
    with pytest.raises(RuntimeError, match="closed"):
        cache.get("nope")


def test_close_race_does_not_register_orphan_connection(tmp_path):
    cache = Cache(tmp_path / "c")
    gate = threading.Event()
    result: dict[str, RuntimeError | None] = {"exc": None}

    real_connect = sqlite3.connect

    def gated_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        gate.wait(timeout=5)
        return conn

    def opener() -> None:
        try:
            cache._connect()
        except RuntimeError as exc:
            result["exc"] = exc

    with patch("wg21_wiki_mcp.cache.sqlite3.connect", gated_connect):
        worker = threading.Thread(target=opener)
        worker.start()
        cache.close()
        gate.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert result["exc"] is not None
    assert "closed" in str(result["exc"]).lower()
    assert cache._all_conns == []
