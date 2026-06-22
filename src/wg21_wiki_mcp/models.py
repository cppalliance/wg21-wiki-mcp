"""Structured response models and error types shared across tools.

These Pydantic models are the MCP tools' structured outputs. They carry only
data that is API-provided or deterministically extracted; verbatim wiki text is
returned in dedicated ``content``/``wikitext`` fields and never transformed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Error hierarchy re-exported from errors.py for backward compatibility.
from .errors import AuthError, FetchError, PageNotFound, WikiMcpError

__all__ = [
    # Model classes
    "BundledPage",
    "CalendarStatus",
    "Chunk",
    "IsoSlot",
    "MeetingList",
    "MeetingOverview",
    "MeetingRef",
    "NamespaceInfo",
    "PageContent",
    "PageList",
    "PageRef",
    "Provenance",
    "RecentChange",
    "RecentChanges",
    "SearchHit",
    "SearchResults",
    "SessionBundle",
    "WikiStatus",
    # Error types (re-exported for backward compatibility)
    "AuthError",
    "FetchError",
    "PageNotFound",
    "WikiMcpError",
]


class Provenance(BaseModel):
    """Identity and verifiable source links for a wiki page revision."""

    requested_title: str = Field(description="Title as requested by the caller.")
    title: str = Field(description="Resolved (normalized) page title from the API.")
    redirected_from: str | None = Field(default=None, description="Original title if the request followed a redirect.")
    revid: int | None = Field(default=None, description="Revision id of the returned content.")
    last_modified: str | None = Field(default=None, description="ISO 8601 timestamp of the revision.")
    fetched_at: str = Field(description="ISO 8601 time the content was fetched from the wiki.")
    url: str = Field(description="Canonical clickable page URL.")
    oldid_url: str | None = Field(default=None, description="Permanent URL pinned to this exact revision.")
    from_cache: bool = Field(description="True if served from the local cache.")


class Chunk(BaseModel):
    """Pagination state for chunked page content (UTF-8-boundary safe)."""

    byte_start: int
    byte_end: int
    total_bytes: int
    has_more: bool
    next_cursor: str | None = None


class PageContent(BaseModel):
    """Verbatim wikitext for a page (or a chunk of it) plus provenance."""

    provenance: Provenance
    section: int | None = Field(default=None, description="Section index if a section was requested.")
    content: str = Field(description="Exact wikitext, byte-for-byte as stored on the wiki.")
    chunk: Chunk


class SearchHit(BaseModel):
    """A single CirrusSearch result. The snippet is API-generated, not verbatim."""

    title: str
    namespace: int
    size: int | None = None
    wordcount: int | None = None
    timestamp: str | None = None
    snippet: str | None = None
    snippet_warning: str = "mediawiki_generated_not_verbatim"
    url: str


class SearchResults(BaseModel):
    """A page of search hits for a query, with an optional pagination cursor."""

    query: str
    hits: list[SearchHit]
    next_cursor: str | None = None


class PageRef(BaseModel):
    """A lightweight title + URL reference (no content)."""

    title: str
    namespace: int | None = None
    url: str


class PageList(BaseModel):
    """A page of page references within a namespace, with a pagination cursor."""

    namespace_id: int | None = None
    namespace_name: str | None = None
    pages: list[PageRef]
    next_cursor: str | None = None


class NamespaceInfo(BaseModel):
    """A MediaWiki content namespace: its numeric id and names."""

    id: int
    name: str
    canonical: str | None = None


class MeetingRef(BaseModel):
    """A discovered meeting namespace, with public-calendar status if known."""

    title: str
    url: str
    is_active: bool = False
    window_start: str | None = None
    window_end: str | None = None


class MeetingList(BaseModel):
    """A page of discovered meetings plus the currently active one, if any."""

    meetings: list[MeetingRef]
    active_meeting: str | None = None
    next_cursor: str | None = None


class RecentChange(BaseModel):
    """A single entry from the wiki's recent-changes feed (API-provided)."""

    type: str
    title: str
    revid: int | None = None
    old_revid: int | None = None
    timestamp: str | None = None
    user: str | None = None
    comment: str | None = None
    url: str


class RecentChanges(BaseModel):
    """A page of recent-changes entries, with an optional pagination cursor."""

    changes: list[RecentChange]
    next_cursor: str | None = None


class MeetingOverview(BaseModel):
    """A meeting's landing page (verbatim) plus a deterministic outlink index."""

    meeting: str
    home: PageContent
    outlinks: list[PageRef]
    next_cursor: str | None = None


class IsoSlot(BaseModel):
    """A deterministically-extracted agenda time boundary (no interpretation)."""

    start: str
    end: str
    source: str
    label_hint: str | None = None


class BundledPage(BaseModel):
    """One raw page in a session bundle (verbatim, with provenance)."""

    title: str
    role: Literal["agenda", "rooms", "evening", "working_group", "other"]
    provenance: Provenance
    wikitext: str | None = None
    size_bytes: int = 0
    truncated: bool = False
    next_cursor: str | None = None


class SessionBundle(BaseModel):
    """Raw materials for the LLM to compose a meeting schedule. Not a schedule."""

    meeting: str
    composition_disclaimer: str = (
        "No schedule is computed by the server. Compose the answer from the bundled "
        "verbatim pages; iso_slots are time boundaries only, not group/room assignments."
    )
    iso_slots: list[IsoSlot] = Field(default_factory=list)
    iso_slots_extraction: Literal["success", "partial", "not_found"] = "not_found"
    pages: list[BundledPage] = Field(default_factory=list)
    missing_pages: list[str] = Field(default_factory=list)


class CalendarStatus(BaseModel):
    """Status of the public meeting-calendar parse used for cache TTL selection."""

    source_url: str
    last_fetched: str | None = None
    parse_status: Literal["ok", "partial", "failed", "override"] = "failed"
    in_meeting_window_now: bool = False
    windows: list[str] = Field(default_factory=list)


class WikiStatus(BaseModel):
    """Operational status. Contains no confidential wiki content."""

    base_url: str
    authenticated: bool
    auth_mode: str | None = None
    user: str | None = None
    ttl_normal_s: int
    ttl_meeting_s: int
    current_ttl_s: int
    ttl_mode: Literal["normal", "meeting", "conservative"]
    calendar: CalendarStatus
    cache_entries: int | None = None
