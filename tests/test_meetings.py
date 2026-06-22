"""Meeting calendar parsing + conservative TTL tests (no real network)."""

from __future__ import annotations

from datetime import date

from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.meetings import MeetingCalendar, parse_meeting_windows

_SAMPLE = """
- 2026-06-08 to 13: Brno, Czechia
- 2026-11-16 to 21: Buzios, Brazil
- 2026-12-29 to 2027-01-03: Somewhere
- 2027-06-28 to 07-02: Month-day end form
- 2026-06-08 to 99: invalid end day (rejected)
- 2099-01-01 to 12-31: implausible (rejected, too long)
- garbage line with no range
"""


def test_parse_windows_various_formats():
    windows = parse_meeting_windows(_SAMPLE)
    assert (date(2026, 6, 8), date(2026, 6, 13)) in windows
    assert (date(2026, 11, 16), date(2026, 11, 21)) in windows
    assert (date(2026, 12, 29), date(2027, 1, 3)) in windows
    assert (date(2027, 6, 28), date(2027, 7, 2)) in windows  # month-day end form
    # Implausible/invalid ranges are rejected.
    assert all((e - s).days <= 21 for s, e in windows)
    assert all(e >= s for s, e in windows)


def _config(tmp_path, overrides=None) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "a@b", "p"),
        user=None,
        cache_dir=tmp_path / "c",
        meeting_window_overrides=overrides or [],
    )


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


class _FakeSession:
    def __init__(self, text: str | None) -> None:
        self._text = text
        self.headers: dict[str, str] = {}

    def get(self, *_a, **_k):
        if self._text is None:
            raise RuntimeError("network down")
        return _FakeResp(self._text)


def test_active_during_window(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    assert cal.is_meeting_active(date(2026, 6, 10)) is True
    assert cal.ttl_mode(date(2026, 6, 10)) == "meeting"
    assert cal.ttl_seconds(date(2026, 6, 10)) == cal._config.ttl_meeting_s


def test_inactive_outside_window(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    assert cal.is_meeting_active(date(2026, 7, 15)) is False
    assert cal.ttl_mode(date(2026, 7, 15)) == "normal"


def test_buffer_includes_adjacent_day(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    assert cal.is_meeting_active(date(2026, 6, 7)) is True  # one day before, buffered


def test_conservative_on_parse_failure(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(None))
    assert cal.is_meeting_active(date(2026, 7, 15)) is True  # bias to short TTL
    assert cal.ttl_mode(date(2026, 7, 15)) == "conservative"


def test_overrides_take_precedence(tmp_path):
    overrides = [(date(2030, 1, 1), date(2030, 1, 5))]
    cal = MeetingCalendar(_config(tmp_path, overrides), session=_FakeSession(None))
    assert cal.is_meeting_active(date(2030, 1, 3)) is True
    assert cal.is_meeting_active(date(2031, 1, 3)) is False  # override governs, not conservative
    status = cal.status()
    assert status.parse_status == "override"


def test_window_for_meeting_title(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    assert cal.window_for_meeting_title("2026-06 Alpha") == ("2026-06-08", "2026-06-13")
    assert cal.window_for_meeting_title("2026-11 Beta") == ("2026-11-16", "2026-11-21")
    assert cal.window_for_meeting_title("2026-03 Unknown") == (None, None)
    assert cal.window_for_meeting_title("Not A Meeting") == (None, None)


def test_ensure_fresh_skips_when_closed(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    cal.close()
    cal.ensure_fresh()
    assert cal._last_fetched is None


def test_close_is_idempotent(tmp_path):
    cal = MeetingCalendar(_config(tmp_path), session=_FakeSession(_SAMPLE))
    cal.close()
    cal.close()
    assert cal._closed is True
