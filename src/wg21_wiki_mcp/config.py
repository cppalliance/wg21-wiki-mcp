"""Runtime configuration, read from the process environment.

The MCP host injects these via the server's launch ``env`` block (the canonical
way to configure a stdio MCP); a local ``.env`` is also honored for development.
Credential values are held in memory only and never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# ConfigError re-exported from errors.py; import here so callers that do
# ``from wg21_wiki_mcp.config import ConfigError`` continue to work.
from .errors import ConfigError

__all__ = [
    # Configuration classes
    "Config",
    "Credentials",
    # Constants
    "DEFAULT_CACHE_DIR_NAME",
    "DEFAULT_TTL_MEETING_S",
    "DEFAULT_TTL_NORMAL_S",
    "WIKI_BASE_URL",
    # Error type (re-exported for backward compatibility)
    "ConfigError",
]

try:  # python-dotenv is optional; absence simply means "no .env convenience".
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - dotenv is a normal dependency
    _load_dotenv = None  # type: ignore[assignment]


# The WG21 committee wiki has a single canonical base URL; it is centralized
# here rather than configured per-deployment.
WIKI_BASE_URL = "https://wiki.isocpp.org"

DEFAULT_TTL_NORMAL_S = 7 * 24 * 60 * 60  # one week
DEFAULT_TTL_MEETING_S = 60 * 60  # one hour
DEFAULT_CACHE_DIR_NAME = ".isocpp.wiki"


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
    password: str


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
    user_agent: str = "wg21-wiki-mcp/0.1 (+https://github.com/cppalliance/wg21-wiki-mcp)"

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

        return cls(
            base_url=WIKI_BASE_URL,
            bot=bot,
            user=user,
            cache_dir=cache_dir,
            ttl_normal_s=_env_int("ISOCPP_WIKI_TTL_NORMAL", DEFAULT_TTL_NORMAL_S),
            ttl_meeting_s=_env_int("ISOCPP_WIKI_TTL_MEETING", DEFAULT_TTL_MEETING_S),
            meeting_window_overrides=_parse_meeting_windows(_env_first("ISOCPP_WIKI_MEETING_WINDOWS")),
        )

    @property
    def api_url(self) -> str:
        """Absolute URL of the MediaWiki Action API endpoint."""
        return f"{self.base_url}/api.php"

    @property
    def ordered_credentials(self) -> list[Credentials]:
        """Credentials to try at login, bot first (the default path)."""
        return [c for c in (self.bot, self.user) if c is not None]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default
