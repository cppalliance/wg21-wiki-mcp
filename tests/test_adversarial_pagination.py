"""Property-based and adversarial tests for pagination cursors and UTF-8 chunking."""

from __future__ import annotations

import base64
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from wg21_wiki_mcp.pagination import chunk_utf8, decode_cursor, encode_cursor


def _decodes_to_valid_dict_cursor(raw: bytes) -> bool:
    """Return True if ``raw`` is JSON that ``decode_cursor`` would accept as a dict."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict)


def _reassemble_chunks(text: str, max_bytes: int) -> str:
    """Walk every chunk from ``chunk_utf8`` and join them."""
    pieces: list[str] = []
    start = 0
    while True:
        chunk, _bs, end, _total, has_more = chunk_utf8(text, start=start, max_bytes=max_bytes)
        pieces.append(chunk)
        start = end
        if not has_more:
            break
    return "".join(pieces)


@given(
    text=st.text(),
    max_bytes=st.integers(min_value=0, max_value=4096),
)
@settings(max_examples=200, deadline=None)
def test_chunk_reassembly_property(text: str, max_bytes: int) -> None:
    """Reassembling all chunks reproduces the input byte-for-byte."""
    assert _reassemble_chunks(text, max_bytes) == text


@given(
    text=st.text(min_size=1),
    max_bytes=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=100, deadline=None)
def test_chunk_slices_are_valid_utf8(text: str, max_bytes: int) -> None:
    """Every emitted slice is valid UTF-8 and makes forward progress."""
    start = 0
    data = text.encode("utf-8")
    while True:
        chunk, byte_start, byte_end, total, has_more = chunk_utf8(text, start=start, max_bytes=max_bytes)
        assert byte_start <= byte_end <= total
        assert chunk.encode("utf-8").decode("utf-8") == chunk
        if not has_more:
            assert byte_end == total
            break
        assert byte_end > start or byte_end >= len(data)
        start = byte_end


@given(
    payload=st.dictionaries(
        keys=st.text(min_size=1, max_size=20, alphabet=st.characters(blacklist_categories=("Cs",))),
        values=st.one_of(
            st.integers(),
            st.text(max_size=50),
            st.booleans(),
            st.none(),
        ),
        max_size=10,
    )
)
@settings(max_examples=100)
def test_cursor_roundtrip_property(payload: dict) -> None:
    """encode_cursor / decode_cursor is a round-trip for JSON-serializable dicts."""
    assert decode_cursor(encode_cursor(payload)) == payload


@given(
    garbage=st.binary(min_size=1, max_size=256).filter(lambda raw: not _decodes_to_valid_dict_cursor(raw)),
)
@settings(max_examples=100)
def test_malformed_cursor_raises_invalid_params(garbage: bytes) -> None:
    """Random bytes that are not valid dict JSON must raise INVALID_PARAMS."""
    token = base64.urlsafe_b64encode(garbage).decode("ascii")
    with pytest.raises(McpError) as exc_info:
        decode_cursor(token)
    assert exc_info.value.error.code == INVALID_PARAMS


@pytest.mark.parametrize(
    "cursor",
    [
        "",  # empty
        "YQ",  # truncated base64 (valid prefix, incomplete payload)
        "!!!",  # invalid base64 alphabet
        base64.urlsafe_b64encode(b'{"not": "a dict root"}'[:3]).decode(),  # truncated JSON
        base64.urlsafe_b64encode(json.dumps("string-not-dict").encode()).decode(),
    ],
)
def test_adversarial_cursors_raise_invalid_params(cursor: str) -> None:
    """Malformed, truncated, and oversized cursors raise INVALID_PARAMS, never crash."""
    with pytest.raises(McpError) as exc:
        decode_cursor(cursor)
    assert exc.value.error.code == INVALID_PARAMS


def test_oversized_cursor_token_raises() -> None:
    """Very large opaque tokens are rejected without crashing."""
    with pytest.raises(McpError) as exc:
        decode_cursor("A" * 10_000)
    assert exc.value.error.code == INVALID_PARAMS


def test_large_valid_cursor_payload_round_trips() -> None:
    """A large but valid dict cursor encodes and decodes without error."""
    huge = {"x": "y" * 50_000}
    token = encode_cursor(huge)
    decoded = decode_cursor(token)
    assert decoded == huge
