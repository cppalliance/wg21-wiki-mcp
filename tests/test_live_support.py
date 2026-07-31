"""Unit tests for live/canary WAF-edge skip helpers."""

from __future__ import annotations

import logging
import re

import pytest
import responses
from live_support import (
    assert_live_requirements_met,
    auth_error_is_unreachable,
    auth_error_waf_status,
    ensure_wiki_login,
    live_credentials_configured,
    live_creds_required,
    live_tier_should_skip,
    probe_wiki_waf_block,
    vpn_state_hint,
    vpn_tunnel_present,
)

from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AuthError


@pytest.fixture(autouse=True)
def _isolate_interface_list(tmp_path, monkeypatch):
    """Keep the composed failure messages off the host's real interface list.

    Any test that sets ``CI_REQUIRE_LIVE_CREDS`` reaches ``vpn_state_hint()``, so
    without this the message would differ between a developer on a VPN, one not on
    a VPN, and Windows.
    """
    root = tmp_path / "default-net"
    root.mkdir()
    (root / "eth0").mkdir()
    monkeypatch.setattr("live_support._NET_INTERFACES", root)


def _config(tmp_path) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "Acct@bot", "secret"),
        user=None,
        cache_dir=tmp_path / "c",
    )


@pytest.mark.parametrize("status", [403, 429, 503])
def test_auth_error_waf_status_detects_diagnostic_status(status: int):
    exc = AuthError(f"SAML SSO entry point returned HTTP error.; url=https://w.example; status={status}")
    assert auth_error_waf_status(exc) == status


def test_auth_error_waf_status_ignores_non_waf_codes():
    exc = AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=500")
    assert auth_error_waf_status(exc) is None


def test_auth_error_waf_status_detects_legacy_http_phrase():
    exc = AuthError("SAML SSO entry point returned HTTP 403.")
    assert auth_error_waf_status(exc) == 403


def test_auth_error_waf_status_prefers_waf_code_in_aggregated_message():
    exc = AuthError(
        "clientlogin unavailable; SAML SSO entry point returned HTTP error.; "
        "url=https://w.example; status=500; status=403"
    )
    assert auth_error_waf_status(exc) == 403


def test_auth_error_is_unreachable_true_for_network_failures():
    exc = AuthError("Authentication failed (bot: ConnectionError, user: Timeout); verify wiki credentials.")
    assert auth_error_is_unreachable(exc) is True


def test_auth_error_is_unreachable_false_for_login_error():
    exc = AuthError("Authentication failed (bot: LoginError); verify wiki credentials.")
    assert auth_error_is_unreachable(exc) is False


@responses.activate
def test_probe_wiki_waf_block_detects_api_edge_block(tmp_path):
    config = _config(tmp_path)
    responses.add(
        responses.GET,
        re.compile(r"https://w\.example/api\.php"),
        status=403,
        body="blocked",
    )
    blocked = probe_wiki_waf_block(config)
    assert blocked is not None
    assert blocked[0] == 403
    assert blocked[1].startswith("https://w.example/api.php")


@responses.activate
def test_probe_wiki_waf_block_checks_pluggable_auth_when_api_ok(tmp_path):
    config = _config(tmp_path)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=200, body="{}")
    responses.add(
        responses.GET,
        re.compile(r"https://w\.example/index\.php"),
        status=503,
        body="unavailable",
    )
    blocked = probe_wiki_waf_block(config)
    assert blocked is not None
    assert blocked[0] == 503


@responses.activate
def test_ensure_wiki_login_skips_on_waf_probe(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=429, body="rate limited")
    caplog.set_level(logging.WARNING)
    try:
        with pytest.raises(pytest.skip.Exception, match="HTTP 429"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "HTTP 429" in caplog.text


def test_ensure_wiki_login_skips_on_waf_auth_error(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _blocked_login() -> None:
        raise AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=403")

    ctx.client.login = _blocked_login  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING)
    try:
        with pytest.raises(pytest.skip.Exception, match="HTTP 403"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "HTTP 403" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        ("0", False),
        ("", False),
        ("no", False),
    ],
)
def test_live_creds_required_truthy_parsing(monkeypatch, value: str, expected: bool):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", value)
    assert live_creds_required() is expected


def test_live_creds_required_false_when_unset(monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    assert live_creds_required() is False


def test_assert_live_requirements_met_noop_when_not_required(monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    monkeypatch.delenv("WIKI_BOT_USERNAME", raising=False)
    monkeypatch.delenv("WIKI_BOT_PASSWORD", raising=False)
    assert_live_requirements_met()


def test_assert_live_requirements_met_fails_when_required_and_missing(monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support.live_credentials_configured", lambda: False)
    with pytest.raises(pytest.fail.Exception, match="CI_REQUIRE_LIVE_CREDS"):
        assert_live_requirements_met()


def test_live_credentials_configured_true_with_bot_creds(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "cache"))
    assert live_credentials_configured() is True


@pytest.mark.parametrize(
    ("configured", "required", "expected"),
    [
        (True, True, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_live_tier_should_skip_all_combinations(monkeypatch, configured: bool, required: bool, expected: bool):
    monkeypatch.setattr("live_support.live_credentials_configured", lambda: configured)
    if required:
        monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    else:
        monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    assert live_tier_should_skip() is expected


@responses.activate
def test_ensure_wiki_login_fails_on_waf_probe_when_required(tmp_path, monkeypatch):
    # The default fixture leaves eth0 only, so the composed message must carry the
    # dropped-tunnel half of the hint. Asserting the status alone would let the
    # two halves swap places unnoticed.
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=403, body="blocked")
    try:
        with pytest.raises(pytest.fail.Exception) as excinfo:
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "the VPN dropped" in message
    assert "rotate TORGUARD_VPN_LOCATION" not in message


def test_ensure_wiki_login_fails_on_waf_auth_error_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _blocked_login() -> None:
        raise AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=429")

    ctx.client.login = _blocked_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.fail.Exception, match="HTTP 429"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def test_ensure_wiki_login_fails_on_network_unreachable_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _unreachable_login() -> None:
        raise AuthError("Authentication failed (bot: ConnectionError); verify wiki credentials.")

    ctx.client.login = _unreachable_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.fail.Exception, match="unreachable"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def _net_dir(tmp_path, *interfaces: str):
    """Stand in for /sys/class/net with the given interface names."""
    root = tmp_path / "net"
    root.mkdir()
    for name in interfaces:
        (root / name).mkdir()
    return root


def test_vpn_tunnel_present_true_when_tun_interface_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "lo", "eth0", "tun0"))
    assert vpn_tunnel_present() is True


def test_vpn_tunnel_present_false_without_tun_interface(tmp_path, monkeypatch):
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "lo", "eth0"))
    assert vpn_tunnel_present() is False


def test_vpn_tunnel_present_false_for_the_kernel_ipip_device(tmp_path, monkeypatch):
    # The kernel creates tunl0 as soon as the ipip module loads. Counting it would
    # send an operator off to rotate the VPN location while the tunnel is down.
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "lo", "eth0", "tunl0"))
    assert vpn_tunnel_present() is False


def test_vpn_tunnel_present_none_when_interface_list_is_absent(tmp_path, monkeypatch):
    # Windows and macOS have no /sys/class/net, and "cannot tell" must not be
    # reported as "the VPN is down".
    monkeypatch.setattr("live_support._NET_INTERFACES", tmp_path / "absent")
    assert vpn_tunnel_present() is None


def test_vpn_state_hint_blames_the_exit_ip_when_the_tunnel_is_up(tmp_path, monkeypatch):
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "tun0"))
    hint = vpn_state_hint()
    assert "rotate TORGUARD_VPN_LOCATION" in hint
    # An interface name is weak evidence: it shows a tun device exists, not that
    # the wiki routes still cross it. The hint has to keep saying so, or a reader
    # who rotates and stays blocked has nowhere to go next.
    assert "does not prove the wiki routes survived" in hint


def test_vpn_state_hint_reports_a_mid_run_drop_when_the_tunnel_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "eth0"))
    hint = vpn_state_hint()
    assert "dropped" in hint
    assert "rotate" not in hint


def test_vpn_state_hint_admits_uncertainty_when_it_cannot_look(tmp_path, monkeypatch):
    monkeypatch.setattr("live_support._NET_INTERFACES", tmp_path / "absent")
    assert "Could not tell" in vpn_state_hint()


@responses.activate
def test_ensure_wiki_login_failure_names_the_vpn_cause_on_protected_ci(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "tun0"))
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=403, body="blocked")
    try:
        with pytest.raises(pytest.fail.Exception, match="rotate TORGUARD_VPN_LOCATION"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def _login_blocked_by_the_edge(ctx) -> None:
    """Make login fail the way a WAF block reaches the AuthError path."""

    def _blocked_login() -> None:
        raise AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=429")

    ctx.client.login = _blocked_login  # type: ignore[method-assign]


def test_auth_error_failure_blames_the_exit_ip_when_the_tunnel_is_up(tmp_path, monkeypatch):
    # The edge blocks either at the probe or at login, and the two arrive by
    # different routes through ensure_wiki_login. The hint has to read the same
    # on both, or the remedy would depend on which one happened to fire first.
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "tun0"))
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)
    _login_blocked_by_the_edge(ctx)
    try:
        with pytest.raises(pytest.fail.Exception) as excinfo:
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    message = str(excinfo.value)
    assert "HTTP 429" in message
    assert "rotate TORGUARD_VPN_LOCATION" in message


def test_auth_error_failure_reports_a_mid_run_drop_when_the_tunnel_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "eth0"))
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)
    _login_blocked_by_the_edge(ctx)
    try:
        with pytest.raises(pytest.fail.Exception) as excinfo:
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    message = str(excinfo.value)
    assert "HTTP 429" in message
    assert "the VPN dropped" in message
    assert "rotate TORGUARD_VPN_LOCATION" not in message


def test_skip_message_stays_free_of_vpn_advice_off_protected_ci(tmp_path, monkeypatch):
    # A contributor running the suite locally has no VPN and needs no advice
    # about one, so the hint must not leak into the skip path. The tun0 interface
    # is what makes this bite: without it the hint would be the "cannot tell"
    # variant, which contains neither token and would pass vacuously.
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "tun0"))
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: (403, "https://w.example"))
    try:
        with pytest.raises(pytest.skip.Exception) as excinfo:
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "TORGUARD" not in str(excinfo.value)
    assert "rotate" not in str(excinfo.value)


def test_unreachable_failure_carries_no_waf_claim(tmp_path, monkeypatch):
    # A network fault produced no HTTP response at all, so appending the edge-block
    # hint would assert a block that was never observed and offer the wrong remedy.
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support._NET_INTERFACES", _net_dir(tmp_path, "tun0"))
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _unreachable_login() -> None:
        raise AuthError("Authentication failed (bot: ConnectionError); verify wiki credentials.")

    ctx.client.login = _unreachable_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.fail.Exception) as excinfo:
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "unreachable" in str(excinfo.value)
    assert "rotate TORGUARD_VPN_LOCATION" not in str(excinfo.value)


def test_ensure_wiki_login_skips_on_network_unreachable_when_not_required(tmp_path, monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _unreachable_login() -> None:
        raise AuthError("Authentication failed (bot: ConnectionError); verify wiki credentials.")

    ctx.client.login = _unreachable_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.skip.Exception, match="unreachable"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
