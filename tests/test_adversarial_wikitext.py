"""Adversarial fixtures for the deterministic wikitext slot parser."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wg21_wiki_mcp.wikitext import extract_iso_slots, has_agenda_signal


def _parse_safely(wikitext: str, title: str = "Synthetic:Page") -> tuple[list, str]:
    """Call extract_iso_slots; any outcome other than a tuple is a test failure."""
    slots, status = extract_iso_slots(wikitext, title)
    assert status in {"success", "partial", "not_found"}
    assert isinstance(slots, list)
    return slots, status


@pytest.mark.parametrize(
    "wikitext,expected_status,expect_slots",
    [
        ("", "not_found", False),
        ("<html><body>no agenda markers</body></html>", "not_found", False),
        ("<div session-start= without closing quote", "partial", False),
        ('<td session-start="2026-01-01T00:00:00Z"', "partial", False),  # missing end + close
        ('<td>nested <td session-start="a" session-end="b"></td></td>', "success", True),
        ("<" * 500 + "session-start=" + ">" * 500, "partial", False),
        ('id="agenda"' + "<div>" * 2000 + "</div>" * 2000, "not_found", False),
    ],
)
def test_adversarial_wikitext_degrades_gracefully(wikitext: str, expected_status: str, expect_slots: bool) -> None:
    """Malformed HTML and missing attributes degrade gracefully, never raise."""
    slots, status = _parse_safely(wikitext)
    assert status == expected_status
    if expect_slots:
        assert len(slots) >= 1
    else:
        assert slots == []


def test_megabyte_input_completes_without_hang() -> None:
    """Megabyte-scale nested markup must return promptly (no hang, no raise)."""
    noise = "<table>" + ("<tr><td>" * 10_000) + ("x" * 50) + ("</td></tr>" * 10_000) + "</table>"
    assert "session-start" not in noise
    slots, status = _parse_safely(noise)
    assert status == "not_found"
    assert slots == []


def test_megabyte_agenda_signal_without_slots() -> None:
    """Megabyte input with a signal substring but no well-formed slots -> partial."""
    blob = "session-start=" + ("<span>" * 50_000) + ("garbage " * 50_000)
    slots, status = _parse_safely(blob)
    assert status == "partial"
    assert slots == []


@given(
    prefix=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200),
    suffix=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200),
)
@settings(max_examples=50, deadline=None)
def test_random_wrappers_never_raise(prefix: str, suffix: str) -> None:
    """Arbitrary surrounding text never causes extract_iso_slots to raise."""
    wikitext = prefix + 'id="agenda"' + suffix
    _parse_safely(wikitext)
    assert isinstance(has_agenda_signal(wikitext), bool)


def test_has_agenda_signal_adversarial() -> None:
    """has_agenda_signal is a pure predicate that never raises."""
    assert not has_agenda_signal("")
    assert not has_agenda_signal("A" * 1_000_000)
    assert has_agenda_signal("x" * 1000 + 'session-start="' + "y" * 1000)
