"""Helpers for the live/canary wiki test tier (no confidential content)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
import requests

from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AuthError, ConfigError

_log = logging.getLogger(__name__)

WAF_EDGE_HTTP_CODES = frozenset({403, 429, 503})

_HTTP_STATUS_RE = re.compile(r"status=(\d{3})")
_HTTP_STATUS_LEGACY_RE = re.compile(r"HTTP (\d{3})")

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

_NET_INTERFACES = Path("/sys/class/net")
# Anchored on purpose. The kernel creates a `tunl0` ipip device as soon as that
# module loads, and reading it as a live tunnel would tell an operator to rotate
# the VPN location when the tunnel is in fact down.
_TUN_INTERFACE_RE = re.compile(r"tun\d+")


def live_creds_required() -> bool:
    """True when protected CI expects wiki credentials (``CI_REQUIRE_LIVE_CREDS``)."""
    raw = os.environ.get("CI_REQUIRE_LIVE_CREDS", "").strip().lower()
    return raw in {"1", "true", "yes"}


def live_credentials_configured() -> bool:
    """True when credentials are available (honors local ``.env`` like :meth:`Config.from_env`)."""
    try:
        Config.from_env()
        return True
    except ConfigError:
        return False


def assert_live_requirements_met() -> None:
    """Fail fast when protected CI is missing wiki credentials."""
    if live_creds_required() and not live_credentials_configured():
        pytest.fail(
            "CI_REQUIRE_LIVE_CREDS is set but wiki credentials are not configured. "
            "Set WIKI_BOT_USERNAME/WIKI_BOT_PASSWORD and/or "
            "WIKI_USER_USERNAME/WIKI_USER_PASSWORD in the live-wiki GitHub environment."
        )


def live_tier_should_skip() -> bool:
    """True when live/canary tests should skip for missing credentials (fork/local only)."""
    return not live_credentials_configured() and not live_creds_required()


def auth_error_waf_status(exc: AuthError) -> int | None:
    """Return a WAF/edge block status code embedded in ``exc``, if any."""
    message = str(exc)
    for pattern in (_HTTP_STATUS_RE, _HTTP_STATUS_LEGACY_RE):
        for match in pattern.finditer(message):
            status = int(match.group(1))
            if status in WAF_EDGE_HTTP_CODES:
                return status
    return None


def auth_error_is_unreachable(exc: AuthError) -> bool:
    """True when every failed auth path hit a network error (not bad credentials)."""
    failure_types = _AUTH_PATH_FAILURE_RE.findall(str(exc))
    return bool(failure_types) and all(name in _NETWORK_AUTH_FAILURE_TYPES for name in failure_types)


def probe_wiki_waf_block(config: Config) -> tuple[int, str] | None:
    """Probe the wiki edge; return ``(status, url)`` when a WAF/CDN block is seen."""
    probes = (
        f"{config.base_url}/api.php?action=query&meta=siteinfo&siprop=general&format=json",
        f"{config.base_url}/index.php?title=Special:PluggableAuthLogin",
    )
    headers = {"User-Agent": config.user_agent}
    for url in probes:
        try:
            resp = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
        except requests.RequestException:
            continue
        if resp.status_code in WAF_EDGE_HTTP_CODES:
            return resp.status_code, resp.url
    return None


def vpn_tunnel_present() -> bool | None:
    """True/False when a tun interface can be enumerated, ``None`` when it cannot.

    The generator is consumed inside the ``try`` on purpose: :meth:`Path.iterdir`
    defers its error to first iteration, so hoisting the call out would let the
    ``OSError`` escape.
    """
    try:
        return any(_TUN_INTERFACE_RE.fullmatch(entry.name) for entry in _NET_INTERFACES.iterdir())
    except OSError:
        return None


def vpn_state_hint() -> str:
    """Name the likely cause of an edge block on CI, where a tunnel is expected.

    Protected CI routes wiki traffic over TorGuard, so a block there means one of
    two unrelated things: this exit address is blocked too, or the tunnel dropped
    after the workflow verified it. The remedies differ, and the HTTP status alone
    cannot tell them apart.
    """
    tunnel = vpn_tunnel_present()
    if tunnel is None:
        return "Could not tell whether the CI VPN is up (no readable interface list)."
    if tunnel:
        return (
            "A tun interface is up, so the VPN connected and this exit address is "
            "blocked as well: rotate TORGUARD_VPN_LOCATION to another location."
        )
    # The workflow's own connect and verify steps have to have passed for pytest to
    # run at all, so this is a mid-run drop rather than a setup fault.
    return (
        "No tun interface is present, so the VPN dropped after the 'Verify split "
        "tunnel' step passed: see the uploaded VPN log and re-run the job."
    )


def log_waf_edge_skip(status: int, *, url: str) -> None:
    _log.warning(
        "Skipping live/canary tests: wiki edge returned HTTP %s for %s "
        "(WAF/CDN block of this runner; not a credential fault)",
        status,
        url,
    )


def skip_reason_waf_edge(status: int, *, url: str) -> str:
    return f"wiki edge returned HTTP {status} for {url} (WAF/CDN block of this runner; not a credential fault)."


def _fail_or_skip_live_block(reason: str, *, waf_status: int | None = None, url: str | None = None) -> None:
    """Fail on protected CI; skip gracefully on fork PRs and local runs."""
    if live_creds_required():
        # The hint reads the tunnel to explain an edge block, so it only belongs on
        # protected CI (nothing else runs behind the VPN) and only on the WAF path.
        # Appended to a network fault it would claim a block that never happened.
        if waf_status is not None:
            reason = f"{reason} {vpn_state_hint()}"
        pytest.fail(reason)
    if waf_status is not None and url is not None:
        log_waf_edge_skip(waf_status, url=url)
    pytest.skip(reason)


def ensure_wiki_login(ctx: ServerContext) -> None:
    """Log in, or skip/fail live tests on WAF blocks / network faults (not bad credentials)."""
    blocked = probe_wiki_waf_block(ctx.config)
    if blocked is not None:
        status, url = blocked
        _fail_or_skip_live_block(skip_reason_waf_edge(status, url=url), waf_status=status, url=url)

    try:
        ctx.login()
    except AuthError as exc:
        waf_status = auth_error_waf_status(exc)
        if waf_status is not None:
            _fail_or_skip_live_block(
                skip_reason_waf_edge(waf_status, url=ctx.config.base_url),
                waf_status=waf_status,
                url=ctx.config.base_url,
            )
        if auth_error_is_unreachable(exc):
            _fail_or_skip_live_block(
                "wiki.isocpp.org is unreachable from this shell (network error on all auth paths); "
                "credentials loaded but TCP/TLS failed. Retry when the wiki is reachable; "
                "on CI, check whether the VPN dropped mid-run."
            )
        raise
