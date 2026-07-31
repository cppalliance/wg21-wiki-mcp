"""Runtime configuration, read from the process environment.

The MCP host injects these via the server's launch ``env`` block (the canonical
way to configure a stdio MCP); a local ``.env`` is also honored for development.
Credential values are held in memory only and never logged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, cast

# ConfigError re-exported from errors.py; import here so callers that do
# ``from wg21_wiki_mcp.config import ConfigError`` continue to work.
from .errors import ConfigError
from .log import get_logger

__all__ = [
    # Configuration classes
    "Config",
    "Credentials",
    # Constants
    "DEFAULT_CACHE_DIR_NAME",
    "DEFAULT_HTTP_HOST",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_SAML_TIMEOUT_S",
    "DEFAULT_TTL_MEETING_S",
    "DEFAULT_TTL_NORMAL_S",
    "DEFAULT_TRANSPORT",
    "VALID_TRANSPORTS",
    "WIKI_BASE_URL",
    # Error type (re-exported for backward compatibility)
    "ConfigError",
]

_load_dotenv: Callable[..., bool] | None = None
try:  # python-dotenv is optional; absence simply means "no .env convenience".
    from dotenv import load_dotenv as _load_dotenv_impl

    _load_dotenv = _load_dotenv_impl
except ImportError:  # pragma: no cover - dotenv is a normal dependency
    pass


# The WG21 committee wiki has a single canonical base URL; it is centralized
# here rather than configured per-deployment.
WIKI_BASE_URL = "https://wiki.isocpp.org"

DEFAULT_TTL_NORMAL_S = 7 * 24 * 60 * 60  # one week
DEFAULT_TTL_MEETING_S = 60 * 60  # one hour
DEFAULT_CACHE_DIR_NAME = ".isocpp.wiki"
DEFAULT_SAML_TIMEOUT_S = 30
TransportName = Literal["stdio", "sse", "streamable-http"]
DEFAULT_TRANSPORT: TransportName = "stdio"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
VALID_TRANSPORTS = frozenset({"stdio", "sse", "streamable-http"})
_log = get_logger("config")


def _env_first(*names: str) -> str:
    """Return the first non-empty environment value among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _parse_meeting_windows(raw: str) -> list[tuple[date, date]]:
    """Parse ``YYYY-MM-DD/YYYY-MM-DD`` comma-separated override windows.

    Invalid entries are skipped (overrides must never crash startup).
    """
    windows: list[tuple[date, date]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "/" not in chunk:
            continue
        start_s, _, end_s = chunk.partition("/")
        try:
            start = date.fromisoformat(start_s.strip())
            end = date.fromisoformat(end_s.strip())
        except ValueError:
            continue
        if end >= start:
            windows.append((start, end))
    return windows


@dataclass(frozen=True)
class Credentials:
    """A single username/password pair plus a label for diagnostics."""

    label: str  # "bot" or "user"
    username: str
    password: str = field(repr=False)  # keep secrets out of repr/print/debug output


def default_user_agent() -> str:
    """Return the User-Agent every wiki request carries.

    Public so that out-of-process callers can send the same one. The CI tunnel
    check in ``scripts/ci/torguard_vpn.sh`` reads it to probe the edge exactly as
    the tests do, which a UA-specific rule at the edge would otherwise split.
    """
    from . import __version__

    return f"wg21-wiki-mcp/{__version__} (+https://github.com/cppalliance/wg21-wiki-mcp)"


@dataclass(frozen=True)
class Config:
    """Immutable, validated configuration for the server.

    Build with :meth:`from_env`. Credentials are present only when configured;
    the auth path is auto-selected at login time (bot preferred, user fallback).
    """

    base_url: str
    bot: Credentials | None
    user: Credentials | None
    cache_dir: Path
    ttl_normal_s: int = DEFAULT_TTL_NORMAL_S
    ttl_meeting_s: int = DEFAULT_TTL_MEETING_S
    meeting_window_overrides: list[tuple[date, date]] = field(default_factory=list)
    user_agent: str = field(default_factory=default_user_agent)
    saml_username_field: str | None = None
    saml_password_field: str | None = None
    saml_timeout_s: int = DEFAULT_SAML_TIMEOUT_S
    transport: TransportName = DEFAULT_TRANSPORT
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT

    @classmethod
    def from_env(cls, *, load_env_file: bool = True) -> Config:
        """Construct configuration from environment variables.

        The wiki base URL is fixed (:data:`WIKI_BASE_URL`); only credentials and
        optional tuning come from the environment.

        Raises:
            ConfigError: if no credentials are configured.
        """
        if load_env_file and _load_dotenv is not None:
            _load_dotenv()

        bot_user = _env_first("WIKI_BOT_USERNAME")
        bot_pass = _env_first("WIKI_BOT_PASSWORD")
        bot = Credentials("bot", bot_user, bot_pass) if (bot_user and bot_pass) else None

        usr_user = _env_first("WIKI_USER_USERNAME")
        usr_pass = _env_first("WIKI_USER_PASSWORD")
        user = Credentials("user", usr_user, usr_pass) if (usr_user and usr_pass) else None

        if bot is None and user is None:
            raise ConfigError(
                "No credentials configured: set WIKI_BOT_USERNAME/WIKI_BOT_PASSWORD "
                "and/or WIKI_USER_USERNAME/WIKI_USER_PASSWORD."
            )

        cache_dir_raw = _env_first("ISOCPP_WIKI_CACHE_DIR")
        cache_dir = Path(cache_dir_raw) if cache_dir_raw else Path.home() / DEFAULT_CACHE_DIR_NAME

        saml_user_field = _env_first("WIKI_SAML_USERNAME_FIELD") or None
        saml_pass_field = _env_first("WIKI_SAML_PASSWORD_FIELD") or None

        return cls(
            base_url=WIKI_BASE_URL,
            bot=bot,
            user=user,
            cache_dir=cache_dir,
            ttl_normal_s=_env_int("ISOCPP_WIKI_TTL_NORMAL", DEFAULT_TTL_NORMAL_S),
            ttl_meeting_s=_env_int("ISOCPP_WIKI_TTL_MEETING", DEFAULT_TTL_MEETING_S),
            meeting_window_overrides=_parse_meeting_windows(_env_first("ISOCPP_WIKI_MEETING_WINDOWS")),
            saml_username_field=saml_user_field,
            saml_password_field=saml_pass_field,
            saml_timeout_s=_env_int("WIKI_SAML_TIMEOUT_S", DEFAULT_SAML_TIMEOUT_S),
            transport=_parse_transport(_env_first("WG21_TRANSPORT")),
            http_host=_env_first("WG21_HTTP_HOST") or DEFAULT_HTTP_HOST,
            http_port=_env_port("WG21_HTTP_PORT", DEFAULT_HTTP_PORT),
        )

    @property
    def api_url(self) -> str:
        """Absolute URL of the MediaWiki Action API endpoint."""
        return f"{self.base_url}/api.php"

    @property
    def ordered_credentials(self) -> list[Credentials]:
        """Credentials to try at login, bot first (the default path)."""
        return [c for c in (self.bot, self.user) if c is not None]


def _env_int_raw(name: str) -> int | None:
    """Parse an integer environment variable, or return ``None`` if unset/invalid."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _env_int(name: str, default: int) -> int:
    value = _env_int_raw(name)
    if value is None:
        return default
    return value if value > 0 else default


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    value = _env_int_raw(name)
    if value is None:
        _log.warning("Invalid %s=%r: using default %d", name, raw.strip(), default)
        return default
    if not (1 <= value <= 65535):
        _log.warning(
            "Invalid %s=%r: port must be 1-65535; using default %d",
            name,
            raw.strip(),
            default,
        )
        return default
    return value


def _parse_transport(raw: str) -> TransportName:
    """Return a validated MCP transport name from ``WG21_TRANSPORT``."""
    value = (raw or DEFAULT_TRANSPORT).strip().lower()
    if value not in VALID_TRANSPORTS:
        allowed = ", ".join(sorted(VALID_TRANSPORTS))
        raise ConfigError(f"Invalid WG21_TRANSPORT={raw!r}: must be one of {allowed}.")
    return cast(TransportName, value)
