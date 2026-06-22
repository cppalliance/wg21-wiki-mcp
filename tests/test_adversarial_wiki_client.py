"""Adversarial redirect resolution and HTTP-level retry tests for WikiClient."""

from __future__ import annotations

import pytest
import requests
import responses
from mwclient.errors import MwClientError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)


def _config(tmp_path) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "Acct@bot", "secret"),
        user=None,
        cache_dir=tmp_path / "c",
    )


class _HttpApiSite:
    """Minimal site whose api() hits a real requests.Session (mocked via responses)."""

    def __init__(self) -> None:
        self.connection = requests.Session()
        self._url = "https://w.example/api.php"

    def api(self, action: str, **params: object) -> dict:
        resp = self.connection.post(self._url, data={"action": action, "format": "json", **params}, timeout=5)
        if resp.status_code >= 500:
            raise ConnectionError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise MwClientError("Invalid JSON in API response") from exc
        if "error" in data:
            from mwclient.errors import APIError

            err = data["error"]
            raise APIError(err.get("code", "unknown"), err.get("info", ""), data)
        return data


@pytest.fixture
def http_api_client(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path))
    site = _HttpApiSite()
    monkeypatch.setattr(client, "_new_site", lambda: site)
    client._site = site
    client._active = client._config.bot
    yield client
    client.close()


def _make_client(tmp_path, monkeypatch, api_func):
    class _Site:
        def api(self, action, **params):
            return api_func(action, params)

    client = wc.WikiClient(_config(tmp_path))
    site = _Site()
    monkeypatch.setattr(client, "_new_site", lambda: site)
    client._site = site
    client._active = client._config.bot
    return client


def test_redirect_loop_does_not_hang(tmp_path, monkeypatch):
    """A redirect cycle terminates and returns a stable resolved title."""

    def api_func(action, params):
        return {
            "query": {
                "redirects": [
                    {"from": "Requested", "to": "LoopA"},
                    {"from": "LoopA", "to": "Requested"},
                ],
                "pages": {
                    "1": {
                        "title": "Requested",
                        "revisions": [{"revid": 1, "slots": {"main": {"*": "body"}}}],
                    }
                },
            }
        }

    client = _make_client(tmp_path, monkeypatch, api_func)
    out = client.fetch_pages(["Requested"])
    page = out["Requested"]
    assert page.title == "Requested"
    assert page.redirected_from == "Requested"
    assert page.content == "body"
    assert page.missing is False


def test_self_redirect_does_not_hang(tmp_path, monkeypatch):
    """Self-redirect (A -> A) terminates without infinite recursion."""

    def api_func(action, params):
        return {
            "query": {
                "redirects": [{"from": "Self", "to": "Self"}],
                "pages": {
                    "1": {
                        "title": "Self",
                        "revisions": [{"revid": 2, "slots": {"main": {"*": "self-body"}}}],
                    }
                },
            }
        }

    client = _make_client(tmp_path, monkeypatch, api_func)
    out = client.fetch_pages(["Self"])
    page = out["Self"]
    assert page.title == "Self"
    assert page.redirected_from == "Self"
    assert page.content == "self-body"
    assert page.missing is False


def test_long_redirect_chain_terminates(tmp_path, monkeypatch):
    """A long but acyclic chain resolves to the terminal page."""
    chain = [f"Hop{i}" for i in range(20)]
    redirects = [{"from": "Requested", "to": chain[0]}]
    redirects.extend({"from": chain[i], "to": chain[i + 1]} for i in range(len(chain) - 1))
    terminal = chain[-1]

    def api_func(action, params):
        return {
            "query": {
                "redirects": redirects,
                "pages": {
                    "1": {
                        "title": terminal,
                        "revisions": [{"revid": 9, "slots": {"main": {"*": "end"}}}],
                    }
                },
            }
        }

    client = _make_client(tmp_path, monkeypatch, api_func)
    out = client.fetch_pages(["Requested"])
    assert out["Requested"].title == terminal
    assert out["Requested"].content == "end"
    assert out["Requested"].redirected_from == "Requested"


# --- responses-backed HTTP adversarial ------------------------------------
@responses.activate
def test_api_retries_http_503_sequence(http_api_client):
    """HTTP 5xx responses trigger retry; a later 200 succeeds."""
    client = http_api_client
    url = "https://w.example/api.php"
    responses.add(responses.POST, url, status=503)
    responses.add(responses.POST, url, status=502)
    responses.add(responses.POST, url, json={"ok": 1}, status=200)
    assert client.api("query") == {"ok": 1}
    assert len(responses.calls) == 3


@responses.activate
def test_api_retries_on_timeout(http_api_client):
    """Simulated timeout (connection error) is retried deterministically."""
    client = http_api_client
    url = "https://w.example/api.php"
    responses.add(responses.POST, url, body=requests.exceptions.Timeout("timed out"))
    responses.add(responses.POST, url, json={"query": {"pages": {}}}, status=200)
    assert "query" in client.api("query")
    assert len(responses.calls) == 2


@responses.activate
def test_api_retries_truncated_json(http_api_client):
    """Truncated/garbage API JSON triggers retry via MwClientError."""
    client = http_api_client
    url = "https://w.example/api.php"
    responses.add(responses.POST, url, body='{"query": {"pa', status=200)
    responses.add(
        responses.POST,
        url,
        json={"query": {"search": []}},
        status=200,
    )
    result = client.api("query", list="search")
    assert result == {"query": {"search": []}}
    assert len(responses.calls) == 2


@responses.activate
def test_api_garbage_json_body(http_api_client):
    """Non-JSON garbage body is retried and eventually succeeds."""
    client = http_api_client
    url = "https://w.example/api.php"
    responses.add(responses.POST, url, body="not json at all", status=200)
    responses.add(responses.POST, url, json={"done": True}, status=200)
    assert client.api("query") == {"done": True}
    assert len(responses.calls) == 2
