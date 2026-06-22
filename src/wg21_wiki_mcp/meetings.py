"""Meeting-aware cache TTL via the PUBLIC isocpp.org meetings page.

This is the only component that parses free-form content, and it is deliberately
conservative: a misparse only changes cache freshness (how often pages are
re-fetched), never the wiki text returned to the caller. On any parse failure it
biases to the short ("meeting") TTL so data stays fresh rather than stale.

Only public data is used; nothing here is confidential.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta, timezone

import requests

from .config import Config
from .log import get_logger
from .log_safety import safe_exception_summary
from .models import CalendarStatus

logger = get_logger("meetings")

PUBLIC_MEETINGS_URL = "https://isocpp.org/std/meetings-and-participation/upcoming-meetings"

# Matches lines like "2026-06-08 to 13: Brno" or "2026-06-29 to 07-04: ..."
# or "2026-06-08 to 2026-06-13: ...". Start is a full ISO date; end is a day,
# a month-day, or a full date.
_RANGE_RE = re.compile(
    r"(?P<start>\d{4}-\d{2}-\d{2})\s+to\s+"
    r"(?P<end>(?:\d{4}-)?(?:\d{2}-)?\d{1,2})\s*:"
)

_REFRESH_INTERVAL = timedelta(days=1)
_WINDOW_BUFFER = timedelta(days=1)  # timezone safety around each meeting window


def _parse_end(start: date, end_raw: str) -> date | None:
    """Resolve the end token (day / month-day / full date) against the start."""
    parts = end_raw.split("-")
    try:
        if len(parts) == 1:  # day only -> same year/month
            return date(start.year, start.month, int(parts[0]))
        if len(parts) == 2:  # month-day -> same year
            return date(start.year, int(parts[0]), int(parts[1]))
        if len(parts) == 3:  # full date
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    return None


def parse_meeting_windows(text: str) -> list[tuple[date, date]]:
    """Extract (start, end) meeting windows from the public page text.

    Returns only well-formed, sane ranges; malformed lines are skipped.
    """
    windows: list[tuple[date, date]] = []
    for match in _RANGE_RE.finditer(text):
        try:
            start = date.fromisoformat(match.group("start"))
        except ValueError:
            continue
        end = _parse_end(start, match.group("end"))
        if end is None or end < start or (end - start) > timedelta(days=21):
            continue  # reject implausible ranges
        windows.append((start, end))
    return windows


class MeetingCalendar:
    """Fetches and caches the public meeting windows; selects the cache TTL."""

    def __init__(self, config: Config, *, session: requests.Session | None = None) -> None:
        """Initialize the calendar; an injected ``session`` is used mainly for tests."""
        self._config = config
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", config.user_agent)
        self._lock = threading.Lock()
        self._windows: list[tuple[date, date]] = []
        self._last_fetched: datetime | None = None
        self._parse_status: str = "failed"
        self._owns_session = session is None
        self._closed = False

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def ensure_fresh(self) -> None:
        """Refresh the calendar at most once per day."""
        with self._lock:
            if self._closed:
                return
            now = datetime.now(timezone.utc)
            if self._last_fetched is not None and now - self._last_fetched < _REFRESH_INTERVAL:
                return
            try:
                resp = self._session.get(PUBLIC_MEETINGS_URL, timeout=20)
                resp.raise_for_status()
                self._windows = parse_meeting_windows(resp.text)
                self._parse_status = "ok" if self._windows else "partial"
            except Exception as exc:  # noqa: BLE001 - network/parse failure -> conservative
                logger.warning(
                    "Calendar fetch/parse failed: %s",
                    safe_exception_summary(exc),
                )
                self._parse_status = "failed"
            finally:
                self._last_fetched = now

    def _effective_windows(self) -> tuple[list[tuple[date, date]], str]:
        """Override windows take precedence; otherwise the fetched windows."""
        if self._config.meeting_window_overrides:
            return self._config.meeting_window_overrides, "override"
        return self._windows, self._parse_status

    def is_meeting_active(self, when: date | None = None) -> bool:
        """Return True if ``when`` (default today, UTC) falls within a buffered window.

        On parse failure with no override, bias to True (conservative freshness).
        """
        self.ensure_fresh()
        day = when or self._today()
        windows, status = self._effective_windows()
        if status == "failed" and not windows:
            return True  # conservative: assume meeting -> short TTL
        for start, end in windows:
            if start - _WINDOW_BUFFER <= day <= end + _WINDOW_BUFFER:
                return True
        return False

    def ttl_seconds(self, when: date | None = None) -> int:
        """Return the cache TTL in seconds for ``when`` (short during meetings)."""
        return self._config.ttl_meeting_s if self.is_meeting_active(when) else self._config.ttl_normal_s

    def ttl_mode(self, when: date | None = None) -> str:
        """Return the TTL mode label: "meeting", "normal", or "conservative"."""
        windows, status = self._effective_windows()
        if status == "failed" and not windows:
            return "conservative"
        return "meeting" if self.is_meeting_active(when) else "normal"

    def window_for_meeting_title(self, title: str) -> tuple[str | None, str | None]:
        """Map a ``YYYY-MM Location`` meeting title to public-calendar ISO dates.

        Matches on the title's year-month prefix against each window's start date.
        Returns ``(None, None)`` when no window is known for that prefix.
        """
        if len(title) < 7 or title[4] != "-":
            return None, None
        ym_prefix = title[:7]
        self.ensure_fresh()
        windows, _ = self._effective_windows()
        for start, end in windows:
            if start.strftime("%Y-%m") == ym_prefix:
                return start.isoformat(), end.isoformat()
        return None, None

    def status(self) -> CalendarStatus:
        """Return the calendar's parse status and current meeting-window state."""
        self.ensure_fresh()
        windows, status = self._effective_windows()
        return CalendarStatus(
            source_url=PUBLIC_MEETINGS_URL,
            last_fetched=self._last_fetched.isoformat() if self._last_fetched else None,
            parse_status=status if status in {"ok", "partial", "failed", "override"} else "failed",  # type: ignore[arg-type]
            in_meeting_window_now=self.is_meeting_active(),
            windows=[f"{s.isoformat()}/{e.isoformat()}" for s, e in windows],
        )

    def close(self) -> None:
        """Close the HTTP session when this calendar owns it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_session and hasattr(self._session, "close"):
                self._session.close()
