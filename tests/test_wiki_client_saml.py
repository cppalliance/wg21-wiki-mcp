"""SAML/SSO and clientlogin auth tests using synthetic HTML fixtures + responses."""

from __future__ import annotations

import re
from pathlib import Path

import mwclient
import pytest
import responses
from mwclient.errors import APIError, MwClientError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.log_safety import is_safe_auth_message
from wg21_wiki_mcp.models import AuthError

_FIXTURES = Path(__file__).parent / "fixtures" / "saml"
_BASE = "https://w.example"
_PLUGGABLE = re.compile(r"https://w\.example/index\.php\?title=Special:PluggableAuthLogin")
_IDP = re.compile(r"https://idp\.example/login")
_ACS = re.compile(r"https://w\.example/index\.php/Special:PluggableAuth/Assert")


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _user_config(tmp_path: Path) -> Config:
    return Config(
        base_url=_BASE,
        bot=None,
        user=Credentials("user", "Acct", "test-password-xyz"),
        cache_dir=tmp_path / "c",
    )


def _saml_site(client: wc.WikiClient) -> mwclient.Site:
    """MediaWiki site handle without eager site_init (SAML tests mock HTTP only)."""
    return mwclient.Site(
        client._host,
        path="/",
        scheme=client._scheme,
        clients_useragent=client._config.user_agent,
        max_lag=5,
        do_init=False,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)


def _user_login_via_saml(client: wc.WikiClient, monkeypatch) -> None:
    """Run the user path with clientlogin disabled and auth check stubbed."""
    monkeypatch.setattr(client, "_new_site", lambda: _saml_site(client))
    monkeypatch.setattr(client, "_try_clientlogin", lambda _site, _cred: False)
    monkeypatch.setattr(client, "_is_authenticated", lambda _site: True)
    monkeypatch.setattr(mwclient.Site, "site_init", lambda self: None)
    cred = client._config.user
    assert cred is not None
    client._login_with(cred)
    client._active = cred


def _register_saml_happy_path() -> None:
    responses.add(
        responses.GET, _PLUGGABLE, status=302, headers={"Location": "https://idp.example/login?AuthState=abc123"}
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body=_fixture("saml_post_form.html"), status=200)
    responses.add(responses.POST, _ACS, status=302, headers={"Location": f"{_BASE}/"})
    responses.add(responses.GET, re.compile(r"https://w\.example/?$"), status=200, body="ok")


@responses.activate
def test_saml_happy_path(tmp_path, monkeypatch):
    _register_saml_happy_path()
    client = wc.WikiClient(_user_config(tmp_path))
    _user_login_via_saml(client, monkeypatch)
    assert client.active_label == "user"


@responses.activate
def test_saml_missing_form_raises_auth_error(tmp_path):
    responses.add(responses.GET, _PLUGGABLE, body=_fixture("no_form.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    with pytest.raises(AuthError, match="SAML IdP login form not found"):
        client._saml_login(site, cred)
    assert is_safe_auth_message("SAML IdP login form not found (page changed or extra step required).")


@responses.activate
def test_saml_missing_password_field_raises_auth_error(tmp_path):
    responses.add(
        responses.GET, _PLUGGABLE, status=302, headers={"Location": "https://idp.example/login?AuthState=abc123"}
    )
    responses.add(responses.GET, _IDP, body=_fixture("no_password_field.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    with pytest.raises(AuthError, match="Could not locate username/password fields"):
        client._saml_login(site, cred)


@responses.activate
def test_saml_mfa_no_samlresponse_raises_auth_error(tmp_path):
    responses.add(
        responses.GET, _PLUGGABLE, status=302, headers={"Location": "https://idp.example/login?AuthState=abc123"}
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body=_fixture("mfa_challenge.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    with pytest.raises(AuthError, match="no SAMLResponse"):
        client._saml_login(site, cred)


@responses.activate
def test_saml_auto_follow_branch(tmp_path, monkeypatch):
    """When the HTTP client auto-follows the SAML POST, _saml_login returns early."""
    responses.add(
        responses.GET, _PLUGGABLE, status=302, headers={"Location": "https://idp.example/login?AuthState=abc123"}
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body=_fixture("auto_follow_body.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    _user_login_via_saml(client, monkeypatch)
    assert client.active_label == "user"


@responses.activate
def test_auth_errors_contain_no_credentials(tmp_path):
    secret = "test-password-xyz"
    responses.add(responses.GET, _PLUGGABLE, body=_fixture("no_form.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    with pytest.raises(AuthError) as exc_info:
        client._saml_login(site, cred)
    assert secret not in str(exc_info.value)
    assert is_safe_auth_message(str(exc_info.value))


# --- clientlogin (_try_clientlogin) ---------------------------------------
class _ClientloginSite:
    def __init__(self, *, status="PASS", post_raises: Exception | None = None):
        self.status = status
        self.post_raises = post_raises
        self.connection = type("Conn", (), {"cookies": {}})()

    def get_token(self, _kind: str) -> str:
        return "login-token"

    def post(self, action: str, **_kw):
        if self.post_raises is not None:
            raise self.post_raises
        if action == "clientlogin":
            return {"clientlogin": {"status": self.status}}
        return {}

    def site_init(self) -> None:
        pass

    def api(self, action: str, **params):
        if action == "query" and params.get("meta") == "userinfo":
            return {"query": {"userinfo": {"name": "Acct"}}}
        return {}


def test_try_clientlogin_success(tmp_path):
    client = wc.WikiClient(_user_config(tmp_path))
    site = _ClientloginSite(status="PASS")
    assert client._try_clientlogin(site, client._config.user) is True  # type: ignore[arg-type]


def test_try_clientlogin_fail_status(tmp_path):
    client = wc.WikiClient(_user_config(tmp_path))
    site = _ClientloginSite(status="FAIL")
    assert client._try_clientlogin(site, client._config.user) is False  # type: ignore[arg-type]


def test_try_clientlogin_api_error_returns_false(tmp_path):
    client = wc.WikiClient(_user_config(tmp_path))
    site = _ClientloginSite(post_raises=APIError("loginfailed", "bad", {}))
    assert client._try_clientlogin(site, client._config.user) is False  # type: ignore[arg-type]


def test_try_clientlogin_mwclient_error_returns_false(tmp_path):
    client = wc.WikiClient(_user_config(tmp_path))
    site = _ClientloginSite(post_raises=MwClientError("network"))
    assert client._try_clientlogin(site, client._config.user) is False  # type: ignore[arg-type]


@responses.activate
def test_clientlogin_success_skips_saml(tmp_path, monkeypatch):
    """When clientlogin succeeds, no SAML HTTP requests are made."""
    client = wc.WikiClient(_user_config(tmp_path))
    site = _ClientloginSite(status="PASS")
    monkeypatch.setattr(client, "_new_site", lambda: site)
    client.login()
    assert client.active_label == "user"
    assert len(responses.calls) == 0
