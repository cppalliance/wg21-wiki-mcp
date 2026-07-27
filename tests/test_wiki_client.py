"""WikiClient tests: auth selection, re-login, and batch resolution."""

from __future__ import annotations

import threading
import time
import types

import pytest
from mwclient.errors import APIError, LoginError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.models import AuthError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)


def _config(tmp_path, *, bot=True, user=False) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "Acct@bot", "secret") if bot else None,
        user=Credentials("user", "Acct", "pw") if user else None,
        cache_dir=tmp_path / "c",
    )


class FakeSite:
    def __init__(self, *, login_fails=False, clientlogin_status="FAIL", saml_fails=False, api_func=None):
        self.login_fails = login_fails
        self.clientlogin_status = clientlogin_status
        self._api_func = api_func
        self.login_count = 0
        self.requests: dict = {}

        def _conn_get(*_a, **_k):
            if saml_fails:
                raise ConnectionError("no network in test")
            raise AssertionError("SAML path not expected in this test")

        self.connection = types.SimpleNamespace(
            get=_conn_get,
            post=lambda *a, **k: None,
            cookies={},
            close=lambda: None,
        )

    def login(self, _u, _p):
        self.login_count += 1
        if self.login_fails:
            raise LoginError(self, "Failed", "bad creds")

    def get_token(self, _t):
        return "tok"

    def post(self, action, **_kw):
        if action == "clientlogin":
            return {"clientlogin": {"status": self.clientlogin_status}}
        return {}

    def site_init(self):
        pass

    def api(self, action, **params):
        if action == "query" and params.get("meta") == "userinfo":
            return {"query": {"userinfo": {"name": "Acct"}}}
        if self._api_func is not None:
            return self._api_func(action, params)
        return {"ok": 1}


def _patch_sites(monkeypatch, client, sites):
    seq = iter(sites)
    monkeypatch.setattr(client, "_new_site", lambda: next(seq))


# --- url helpers ----------------------------------------------------------
def test_url_helpers(tmp_path):
    client = wc.WikiClient(_config(tmp_path))
    assert client.canonical_url("A B").endswith("title=A_B")
    assert client.canonical_url("Ns:Page").endswith("title=Ns:Page")
    assert client.oldid_url("A B", 7).endswith("&oldid=7")
    assert client.oldid_url("A", None) is None


# --- auth selection -------------------------------------------------------
def test_bot_login_succeeds(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True))
    _patch_sites(monkeypatch, client, [FakeSite()])
    client.login()
    assert client.active_label == "bot"


def test_replace_site_closes_previous_connection(tmp_path):
    client = wc.WikiClient(_config(tmp_path))
    closed: list[object] = []

    def trackable_site() -> FakeSite:
        site = FakeSite()
        conn = site.connection
        site.connection = types.SimpleNamespace(
            get=conn.get,
            post=conn.post,
            cookies=conn.cookies,
            close=lambda s=site: closed.append(s),
        )
        return site

    first = trackable_site()
    second = trackable_site()
    with client._lock.write():
        client._replace_site(first)
    with client._lock.write():
        client._replace_site(second)
    assert closed == [first]


def test_falls_back_to_user_clientlogin(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True, user=True))
    _patch_sites(
        monkeypatch,
        client,
        [
            FakeSite(login_fails=True),  # bot attempt
            FakeSite(clientlogin_status="PASS"),  # user attempt via clientlogin
        ],
    )
    client.login()
    assert client.active_label == "user"


def test_all_paths_fail_raises(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True, user=True))
    _patch_sites(
        monkeypatch,
        client,
        [
            FakeSite(login_fails=True),  # bot
            FakeSite(clientlogin_status="FAIL", saml_fails=True),  # user clientlogin fail + SAML fail
        ],
    )
    with pytest.raises(AuthError):
        client.login()


def test_api_query_logs_in_when_not_authenticated(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=lambda _a, _p: {"ok": 1})])
    assert client.active_label is None
    assert client.api("query") == {"ok": 1}
    assert client.active_label == "bot"


def test_api_non_query_uses_write_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=lambda _a, _p: {"ok": "mutate"})])
    client.login()

    read_calls: list[int] = []
    write_calls: list[int] = []
    real_read = client._lock.read
    real_write = client._lock.write

    @contextmanager
    def tracked_read():
        read_calls.append(1)
        with real_read():
            yield

    @contextmanager
    def tracked_write():
        write_calls.append(1)
        with real_write():
            yield

    monkeypatch.setattr(client._lock, "read", tracked_read)
    monkeypatch.setattr(client._lock, "write", tracked_write)

    assert client.api("edit") == {"ok": "mutate"}
    assert write_calls == [1]
    assert read_calls == []


def test_api_timed_query_uses_read_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=lambda _a, _p: {"ok": 1})])
    client.login()

    read_calls: list[int] = []
    write_calls: list[int] = []
    real_read = client._lock.read
    real_write = client._lock.write

    @contextmanager
    def tracked_read():
        read_calls.append(1)
        with real_read():
            yield

    @contextmanager
    def tracked_write():
        write_calls.append(1)
        with real_write():
            yield

    monkeypatch.setattr(client._lock, "read", tracked_read)
    monkeypatch.setattr(client._lock, "write", tracked_write)

    assert client.api("query", timeout=0.5) == {"ok": 1}
    assert read_calls == [1]
    assert write_calls == []


def test_relogin_on_readapidenied(tmp_path, monkeypatch):
    calls = {"n": 0}

    def api_func(action, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIError("readapidenied", "need read", {})
        return {"ok": "after-relogin"}

    good = FakeSite(api_func=api_func)
    client = wc.WikiClient(_config(tmp_path, bot=True))
    _patch_sites(monkeypatch, client, [good, FakeSite(api_func=api_func)])
    client.login()
    result = client.api("query")
    assert result == {"ok": "after-relogin"}
    assert calls["n"] == 2  # failed once, retried after re-login


def test_concurrent_query_api_calls_do_not_serialize(tmp_path, monkeypatch):
    """Two concurrent query api() calls overlap instead of serializing on the lock."""
    from concurrent.futures import ThreadPoolExecutor

    api_delay = 0.2
    entered = threading.Barrier(2, timeout=5)

    def api_func(action, params):
        entered.wait()
        time.sleep(api_delay)
        return {"ok": 1}

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.api, "query") for _ in range(2)]
        for fut in futures:
            assert fut.result(timeout=5) == {"ok": 1}
    elapsed = time.monotonic() - t0
    assert elapsed < api_delay * 1.75


def test_concurrent_timed_query_api_calls_do_not_serialize(tmp_path, monkeypatch):
    """Two concurrent timed query api() calls overlap instead of serializing on the lock."""
    from concurrent.futures import ThreadPoolExecutor

    api_delay = 0.2
    entered = threading.Barrier(2, timeout=5)

    def api_func(action, params):
        entered.wait()
        time.sleep(api_delay)
        return {"ok": 1}

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.api, "query", timeout=5.0) for _ in range(2)]
        for fut in futures:
            assert fut.result(timeout=5) == {"ok": 1}
    elapsed = time.monotonic() - t0
    assert elapsed < api_delay * 1.75


def test_timed_query_request_timeout_isolated_per_call(tmp_path, monkeypatch):
    """Per-call timeouts reach session.request without mutating site.requests."""
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2, timeout=5)
    timeouts_by_thread: dict[int, float] = {}
    record_lock = threading.Lock()

    def api_func(action, params):
        site.connection.request("GET", "http://test.example/")
        return {"ok": 1}

    site = FakeSite(api_func=api_func)

    def recording_request(method, url, **kwargs):
        barrier.wait()
        timeout = kwargs.get("timeout")
        assert timeout is not None
        with record_lock:
            timeouts_by_thread[threading.get_ident()] = float(timeout)
        return types.SimpleNamespace(ok=True)

    site.connection.request = recording_request  # type: ignore[attr-defined]
    wc._install_per_call_request_timeout(site.connection)  # type: ignore[arg-type]

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [site])
    client.login()
    assert "timeout" not in site.requests

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(client.api, "query", timeout=3.0)
        f2 = pool.submit(client.api, "query", timeout=7.0)
        assert f1.result(timeout=5) == {"ok": 1}
        assert f2.result(timeout=5) == {"ok": 1}

    assert len(timeouts_by_thread) == 2
    recorded = sorted(timeouts_by_thread.values())
    assert recorded[0] == pytest.approx(3.0, rel=0.25)
    assert recorded[1] == pytest.approx(7.0, rel=0.25)
    assert "timeout" not in site.requests


def test_rwlock_blocks_new_readers_while_writer_waits():
    """Writer-preferring: pending writers prevent new readers from entering."""
    lock = wc._RWLock()
    release_first_read = threading.Event()
    writer_acquired = threading.Event()
    new_read_blocked = threading.Event()

    def wait_for_lock_state(
        *,
        readers: int | None = None,
        writers_waiting: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock._cond:
                if readers is not None and lock._readers != readers:
                    pass
                elif writers_waiting is not None and lock._writers_waiting < writers_waiting:
                    pass
                else:
                    return
            time.sleep(0.001)
        raise AssertionError("lock state not reached in time")

    def hold_first_read():
        with lock.read():
            release_first_read.wait(timeout=5)

    t_read = threading.Thread(target=hold_first_read)
    t_read.start()
    wait_for_lock_state(readers=1)

    def queue_writer():
        with lock.write():
            writer_acquired.set()

    t_write = threading.Thread(target=queue_writer)
    t_write.start()
    wait_for_lock_state(readers=1, writers_waiting=1)

    def probe_read():
        with lock.read():
            new_read_blocked.set()

    t_probe = threading.Thread(target=probe_read)
    t_probe.start()
    t_probe.join(timeout=0.1)
    assert not new_read_blocked.is_set()

    release_first_read.set()
    t_read.join(timeout=5)
    t_write.join(timeout=5)
    t_probe.join(timeout=5)
    assert writer_acquired.is_set()


def test_api_sleep_releases_lock_for_concurrent_calls(tmp_path, monkeypatch):
    """A second api() call can proceed while the first sleeps between retries."""
    first_at_sleep = threading.Event()
    second_may_proceed = threading.Event()
    calls = {"n": 0}

    def api_func(action, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIError("maxlag", "lag", {})
        return {"ok": 1}

    def controlled_sleep(_duration):
        first_at_sleep.set()
        if not second_may_proceed.wait(timeout=5):
            raise AssertionError("second api() did not proceed during retry sleep")

    monkeypatch.setattr(wc.time, "sleep", controlled_sleep)

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()

    errors: list[BaseException] = []

    def first_call():
        try:
            assert client.api("query") == {"ok": 1}
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    def second_call():
        try:
            assert first_at_sleep.wait(timeout=5)
            assert client.api("query") == {"ok": 1}
            second_may_proceed.set()
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    t1 = threading.Thread(target=first_call)
    t2 = threading.Thread(target=second_call)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not errors, errors
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert calls["n"] >= 2


# --- batch resolution -----------------------------------------------------
def test_fetch_pages_maps_normalized_redirects_missing(tmp_path, monkeypatch):
    def api_func(action, params):
        return {
            "query": {
                "normalized": [{"from": "foo_bar", "to": "Foo bar"}],
                "redirects": [{"from": "Foo bar", "to": "Target"}],
                "pages": {
                    "1": {
                        "title": "Target",
                        "revisions": [
                            {
                                "revid": 11,
                                "timestamp": "2026-06-01T00:00:00Z",
                                "size": 4,
                                "slots": {"main": {"*": "body"}},
                            }
                        ],
                    },
                    "2": {"title": "Gone", "missing": ""},
                },
            }
        }

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()
    out = client.fetch_pages(["foo_bar", "Gone"])
    assert out["foo_bar"].title == "Target"
    assert out["foo_bar"].redirected_from == "foo_bar"
    assert out["foo_bar"].content == "body"
    assert out["Gone"].missing is True


def test_page_revisions(tmp_path, monkeypatch):
    def api_func(action, params):
        return {"query": {"pages": {"1": {"title": "P", "revisions": [{"revid": 99}]}}}}

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()
    assert client.page_revisions(["P"]) == {"P": 99}


def test_api_timeout_raises_fetch_error_after_retry(tmp_path, monkeypatch):
    """Per-call timeout bounds retries and raises FetchError once the budget is spent."""
    from wg21_wiki_mcp.models import FetchError

    calls = {"n": 0}

    def api_func(action, params):
        calls["n"] += 1
        raise APIError("maxlag", "lag", {})

    base = time.monotonic()
    ticks = {"n": 0}

    def fake_monotonic():
        ticks["n"] += 1
        if ticks["n"] <= 4:
            return base
        return base + 100.0

    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(wc.time, "monotonic", fake_monotonic)
    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()
    with pytest.raises(FetchError, match="timed out"):
        client.api("query", timeout=0.5)
    assert calls["n"] >= 1


def test_api_timeout_restores_absent_request_timeout(tmp_path, monkeypatch):
    site = FakeSite(api_func=lambda _a, _p: {"ok": 1})
    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [site])
    client.login()
    assert "timeout" not in site.requests
    client.api("query", timeout=0.5)
    assert "timeout" not in site.requests


def test_api_timeout_skips_relogin_when_budget_exhausted(tmp_path, monkeypatch):
    from wg21_wiki_mcp.models import FetchError

    calls = {"api": 0, "relogin": 0}

    def api_func(action, params):
        calls["api"] += 1
        raise APIError("readapidenied", "need read", {})

    site = FakeSite(api_func=api_func)
    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [site, FakeSite(api_func=api_func)])

    real_relogin = client._relogin_locked

    def tracked_relogin(*, deadline=None):
        calls["relogin"] += 1
        return real_relogin(deadline=deadline)

    monkeypatch.setattr(client, "_relogin_locked", tracked_relogin)

    base = time.monotonic()
    ticks = {"n": 0}

    def fake_monotonic():
        ticks["n"] += 1
        if ticks["n"] <= 4:
            return base
        return base + 100.0

    monkeypatch.setattr(wc.time, "monotonic", fake_monotonic)
    client.login()
    with pytest.raises(FetchError, match="timed out"):
        client.api("query", timeout=0.5)
    assert calls["api"] >= 1
    assert calls["relogin"] == 0


def test_login_raises_fetch_error_on_timeout(tmp_path, monkeypatch):
    from wg21_wiki_mcp.models import FetchError

    client = wc.WikiClient(_config(tmp_path, bot=True))
    base = time.monotonic()
    ticks = {"n": 0}

    def fake_monotonic():
        ticks["n"] += 1
        return base + 100.0 if ticks["n"] > 1 else base

    monkeypatch.setattr(wc.time, "monotonic", fake_monotonic)
    with pytest.raises(FetchError, match="timed out"):
        client.login(deadline=base + 0.5)


def test_bot_login_applies_site_request_timeout(tmp_path, monkeypatch):
    site = FakeSite()
    seen: list[object] = []
    real_login = site.login

    def tracked_login(u, p):
        seen.append(site.requests.get("timeout"))
        return real_login(u, p)

    site.login = tracked_login  # type: ignore[method-assign]
    client = wc.WikiClient(_config(tmp_path))
    monkeypatch.setattr(client, "_new_site", lambda: site)
    cred = client._config.bot
    assert cred is not None
    client._bot_login(cred, deadline=time.monotonic() + 5.0)
    assert seen and seen[0] is not None
    assert "timeout" not in site.requests
