"""PageFetcher tests: cache-first, batching, single-flight, revalidation."""

from __future__ import annotations

import threading

import pytest
from conftest import FakePage, FakeWikiClient

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.fetch import PageFetcher


@pytest.fixture
def fetcher_stack(tmp_path):
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    yield fetcher, client, cache
    cache.close()


def test_cache_miss_then_hit(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    first = fetcher.get_page("P", ttl_seconds=1000)
    assert first.content == "body" and first.from_cache is False
    second = fetcher.get_page("P", ttl_seconds=1000)
    assert second.from_cache is True
    assert client.fetch_calls == 1  # second served from cache


def test_batched_coalescing(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    for i in range(5):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)
    out = fetcher.get_pages([f"P{i}" for i in range(5)], ttl_seconds=1000)
    assert len(out) == 5
    assert client.fetch_calls == 1  # one batched request, not five
    assert client.fetch_title_batches[0] == [f"P{i}" for i in range(5)]


def test_single_flight_dedup(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    results = []

    def worker():
        results.append(fetcher.get_page("P", ttl_seconds=1000).content)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["body"] * 8
    assert client.fetch_calls == 1  # concurrent callers coalesced into one fetch


def test_refresh_bypasses_cache(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    client.pages["P"] = FakePage("v2", 2)
    out = fetcher.get_page("P", ttl_seconds=1000, refresh=True)
    assert out.content == "v2" and out.from_cache is False


def test_revalidation_unchanged_revid(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=0)  # store
    # ttl=0 forces staleness; revid unchanged -> revalidate, no content refetch.
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.from_cache is True
    assert client.revision_calls >= 1
    assert client.fetch_calls == before  # no extra content fetch


def test_revalidation_changed_revid_refetches(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=0)
    client.pages["P"] = FakePage("v2", 2)
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.content == "v2" and out.from_cache is False
    assert client.fetch_calls == before + 1


def test_missing_page(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    out = fetcher.get_page("Ghost", ttl_seconds=1000)
    assert out.missing is True and out.content is None


def test_section_cache_miss_then_hit(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("whole", 3)

    first = fetcher.get_page_section("P", 1, ttl_seconds=1000)
    assert first.content == "== section 1 ==\nwhole"
    assert first.from_cache is False
    assert first.requested_title == "P"

    second = fetcher.get_page_section("P", 1, ttl_seconds=1000)
    assert second.from_cache is True
    assert client.section_fetch_calls == 1


def test_section_full_page_cached_separately(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    fetcher.get_page("P", ttl_seconds=1000)
    fetcher.get_page_section("P", 2, ttl_seconds=1000)
    assert client.fetch_calls == 1
    assert client.section_fetch_calls == 1


def test_inproc_lock_map_bounded(tmp_path, monkeypatch):
    from wg21_wiki_mcp.fetch import PageFetcher

    monkeypatch.setattr("wg21_wiki_mcp.fetch._MAX_INPROC_LOCK_ENTRIES", 2)
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    try:
        for i in range(4):
            client.pages[f"P{i}"] = FakePage(f"b{i}", i)
            fetcher.get_page(f"P{i}", ttl_seconds=1000)
        assert len(fetcher._inproc_locks) <= 2
    finally:
        cache.close()
