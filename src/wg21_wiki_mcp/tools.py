"""Tool implementations.

Each function takes a :class:`~wg21_wiki_mcp.context.ServerContext` and returns
a structured Pydantic model. All page content flows through the centralized
``PageFetcher``; nothing here transforms wiki text. List tools paginate with
opaque cursors; ``get_page`` chunks long pages on UTF-8 boundaries.
"""

from __future__ import annotations

import re

from .context import ServerContext
from .log import get_logger
from .log_safety import safe_exception_summary
from .models import (
    BundledPage,
    Chunk,
    IsoSlot,
    MeetingList,
    MeetingOverview,
    MeetingRef,
    NamespaceInfo,
    PageContent,
    PageList,
    PageNotFound,
    PageRef,
    RecentChange,
    RecentChanges,
    SearchHit,
    SearchResults,
    SessionBundle,
    WikiStatus,
)
from .pagination import chunk_utf8, decode_cursor, encode_cursor
from .wikitext import extract_iso_slots, has_agenda_signal

logger = get_logger("tools")

_MEETING_TITLE_RE = re.compile(r"^\d{4}-\d{2} .+$")
_DEFAULT_PAGE_MAX_BYTES = 48 * 1024
_DEFAULT_BUNDLE_PAGE_MAX_BYTES = 8 * 1024
_MAX_LIST_LIMIT = 50
_MAX_NS_PAGE_LIMIT = 500


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def _lookup_namespace_name(ctx: ServerContext, namespace_id: int) -> str | None:
    """Resolve a namespace id to its API-provided display name, if known."""
    try:
        resp = ctx.client.list_namespaces()
    except Exception:  # noqa: BLE001 - optional enrichment; list_pages must not fail
        return None
    for ns_id_str, ns in resp.get("query", {}).get("namespaces", {}).items():
        if int(ns_id_str) == namespace_id:
            name = ns.get("*")
            return name if name is not None else None
    return None


# --------------------------------------------------------------------------- #
# search_wiki
# --------------------------------------------------------------------------- #
def search_wiki(
    ctx: ServerContext,
    query: str,
    *,
    limit: int = 10,
    namespace: int | None = None,
    cursor: str | None = None,
) -> SearchResults:
    """Search the wiki's full text via CirrusSearch.

    Snippets are API-generated excerpts, not authoritative text; use ``get_page``
    for verbatim content.
    """
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    offset = int(decode_cursor(cursor).get("o", 0))
    resp = ctx.client.search(query, limit=limit, namespace=namespace, offset=offset)
    search = resp.get("query", {}).get("search", [])
    hits = [
        SearchHit(
            title=item["title"],
            namespace=item.get("ns", 0),
            size=item.get("size"),
            wordcount=item.get("wordcount"),
            timestamp=item.get("timestamp"),
            snippet=item.get("snippet"),
            url=ctx.client.canonical_url(item["title"]),
        )
        for item in search
    ]
    next_offset = resp.get("continue", {}).get("sroffset")
    next_cursor = encode_cursor({"o": next_offset}) if next_offset is not None else None
    return SearchResults(query=query, hits=hits, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_page
# --------------------------------------------------------------------------- #
def get_page(
    ctx: ServerContext,
    title: str,
    *,
    section: int | None = None,
    max_bytes: int = _DEFAULT_PAGE_MAX_BYTES,
    cursor: str | None = None,
    refresh: bool = False,
) -> PageContent:
    """Return verbatim wikitext for a page (or one section), chunked if large.

    Raises:
        PageNotFound: if the page does not exist.
    """
    max_bytes = _clamp(max_bytes, 1024, 256 * 1024)
    start = int(decode_cursor(cursor).get("o", 0))

    if section is not None:
        outcome = ctx.fetcher.get_page_section(
            title,
            section,
            ttl_seconds=ctx.current_ttl(),
            refresh=refresh,
        )
    else:
        outcome = ctx.fetcher.get_page(title, ttl_seconds=ctx.current_ttl(), refresh=refresh)
    if outcome.missing or outcome.content is None:
        if section is not None:
            raise PageNotFound(f"Page or section not found: {title!r} section {section}")
        raise PageNotFound(f"Page not found: {title!r}")
    prov = ctx.provenance(outcome)
    content = outcome.content

    chunk_text, byte_start, byte_end, total, has_more = chunk_utf8(content, start=start, max_bytes=max_bytes)
    next_cursor = encode_cursor({"o": byte_end}) if has_more else None
    return PageContent(
        provenance=prov,
        section=section,
        content=chunk_text,
        chunk=Chunk(
            byte_start=byte_start,
            byte_end=byte_end,
            total_bytes=total,
            has_more=has_more,
            next_cursor=next_cursor,
        ),
    )


# --------------------------------------------------------------------------- #
# list_pages
# --------------------------------------------------------------------------- #
def list_pages(
    ctx: ServerContext,
    namespace: int,
    *,
    prefix: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> PageList:
    """Enumerate page titles in a namespace (API-provided; no content)."""
    limit = _clamp(limit, 1, _MAX_NS_PAGE_LIMIT)
    cont = decode_cursor(cursor).get("c")
    resp = ctx.client.list_pages(namespace=namespace, prefix=prefix, limit=limit, cont=cont)
    pages = [
        PageRef(title=p["title"], namespace=p.get("ns", namespace), url=ctx.client.canonical_url(p["title"]))
        for p in resp.get("query", {}).get("allpages", [])
    ]
    next_cont = resp.get("continue", {}).get("apcontinue")
    next_cursor = encode_cursor({"c": next_cont}) if next_cont is not None else None
    return PageList(
        namespace_id=namespace,
        namespace_name=_lookup_namespace_name(ctx, namespace),
        pages=pages,
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# list_namespaces
# --------------------------------------------------------------------------- #
def list_namespaces(ctx: ServerContext) -> list[NamespaceInfo]:
    """List all content namespaces (API-provided)."""
    resp = ctx.client.list_namespaces()
    out: list[NamespaceInfo] = []
    for ns_id_str, ns in resp.get("query", {}).get("namespaces", {}).items():
        ns_id = int(ns_id_str)
        if ns_id < 0:
            continue
        out.append(NamespaceInfo(id=ns_id, name=ns.get("*") or "", canonical=ns.get("canonical")))
    return out


# --------------------------------------------------------------------------- #
# meeting discovery
# --------------------------------------------------------------------------- #
def _discover_meetings(ctx: ServerContext) -> list[str]:
    """All ns0 titles that look like meetings (``YYYY-MM Location``), newest first."""
    titles: list[str] = []
    cont: str | None = None
    while True:
        resp = ctx.client.list_pages(namespace=0, prefix=None, limit=_MAX_NS_PAGE_LIMIT, cont=cont)
        for page in resp.get("query", {}).get("allpages", []):
            if _MEETING_TITLE_RE.match(page["title"]):
                titles.append(page["title"])
        cont = resp.get("continue", {}).get("apcontinue")
        if not cont:
            break
    return sorted(set(titles), reverse=True)


def _resolve_meeting(ctx: ServerContext, meeting: str | None) -> str:
    """Return the requested meeting, or the most recent discovered one."""
    if meeting:
        return meeting
    meetings = _discover_meetings(ctx)
    if not meetings:
        raise PageNotFound("No meeting namespaces were found on the wiki.")
    return meetings[0]


def list_meetings(
    ctx: ServerContext,
    *,
    limit: int = 10,
    cursor: str | None = None,
) -> MeetingList:
    """List discovered meetings (newest first); flags the active meeting."""
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    offset = int(decode_cursor(cursor).get("o", 0))
    all_meetings = _discover_meetings(ctx)
    active = all_meetings[0] if (all_meetings and ctx.calendar.is_meeting_active()) else None
    window = all_meetings[offset : offset + limit]
    refs = []
    for t in window:
        window_start, window_end = ctx.calendar.window_for_meeting_title(t)
        refs.append(
            MeetingRef(
                title=t,
                url=ctx.client.canonical_url(t),
                is_active=(t == active),
                window_start=window_start,
                window_end=window_end,
            )
        )
    next_offset = offset + limit
    next_cursor = encode_cursor({"o": next_offset}) if next_offset < len(all_meetings) else None
    return MeetingList(meetings=refs, active_meeting=active, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_recent_changes
# --------------------------------------------------------------------------- #
def get_recent_changes(
    ctx: ServerContext,
    *,
    namespace: int | None = None,
    since: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> RecentChanges:
    """Recent edits/new pages (API-provided); high value during meetings."""
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    cont = decode_cursor(cursor).get("c")
    resp = ctx.client.recent_changes(namespace=namespace, since=since, limit=limit, cont=cont)
    changes = [
        RecentChange(
            type=c.get("type", "edit"),
            title=c["title"],
            revid=c.get("revid"),
            old_revid=c.get("old_revid"),
            timestamp=c.get("timestamp"),
            user=c.get("user"),
            comment=c.get("comment"),
            url=ctx.client.canonical_url(c["title"]),
        )
        for c in resp.get("query", {}).get("recentchanges", [])
    ]
    next_cont = resp.get("continue", {}).get("rccontinue")
    next_cursor = encode_cursor({"c": next_cont}) if next_cont is not None else None
    return RecentChanges(changes=changes, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_meeting_overview
# --------------------------------------------------------------------------- #
def _page_outlinks(ctx: ServerContext, title: str, *, cap: int = 500) -> list[str]:
    """All internal links on a page (paginated up to ``cap``)."""
    links: list[str] = []
    cont: str | None = None
    while len(links) < cap:
        resp = ctx.client.page_links(title, limit=500, cont=cont)
        for page in resp.get("query", {}).get("pages", {}).values():
            for link in page.get("links", []):
                if link.get("ns", 0) >= 0:
                    links.append(link["title"])
        cont = resp.get("continue", {}).get("plcontinue")
        if not cont:
            break
    return links


def get_meeting_overview(ctx: ServerContext, meeting: str | None = None) -> MeetingOverview:
    """Return a meeting's landing page (verbatim) plus its deterministic outlink index."""
    title = _resolve_meeting(ctx, meeting)
    home = get_page(ctx, title)
    outlinks = [
        PageRef(title=t, url=ctx.client.canonical_url(t)) for t in _page_outlinks(ctx, title) if t.startswith(title)
    ]
    return MeetingOverview(meeting=title, home=home, outlinks=outlinks)


# --------------------------------------------------------------------------- #
# get_meeting_sessions  (session BUNDLE, not a parsed schedule)
# --------------------------------------------------------------------------- #
def get_meeting_sessions(
    ctx: ServerContext,
    meeting: str | None = None,
    *,
    groups: list[str] | None = None,
    include_wikitext: bool = True,
    max_page_bytes: int = _DEFAULT_BUNDLE_PAGE_MAX_BYTES,
) -> SessionBundle:
    """Bundle the raw pages needed to compose a meeting's schedule.

    The server does NOT compose a schedule (room/day tables are not reliably
    parseable). It returns deterministic ``iso_slots`` (agenda time boundaries,
    when present) plus the relevant pages verbatim; the calling LLM composes.
    """
    title = _resolve_meeting(ctx, meeting)
    max_page_bytes = _clamp(max_page_bytes, 512, 64 * 1024)
    group_tokens = [g.lower() for g in (groups or [])]

    candidates = [t for t in _page_outlinks(ctx, title) if t.startswith(title)]
    fetched = ctx.fetcher.get_pages(candidates, ttl_seconds=ctx.current_ttl()) if candidates else {}

    iso_slots: list[IsoSlot] = []
    iso_status = "not_found"
    pages: list[BundledPage] = []
    missing: list[str] = []

    for cand in candidates:
        outcome = fetched.get(cand)
        if outcome is None or outcome.missing or outcome.content is None:
            missing.append(cand)
            continue
        content = outcome.content
        is_agenda = has_agenda_signal(content)
        if is_agenda and iso_status == "not_found":
            iso_slots, iso_status = extract_iso_slots(content, outcome.title)
        matched_group = any(tok in cand.lower() for tok in group_tokens)
        role = "agenda" if is_agenda else ("working_group" if matched_group else "other")

        include_body = include_wikitext and (is_agenda or matched_group)
        body, body_cursor, truncated = _bundle_body(content, include_body, max_page_bytes)
        pages.append(
            BundledPage(
                title=outcome.title,
                role=role,  # type: ignore[arg-type]
                provenance=ctx.provenance(outcome),
                wikitext=body,
                size_bytes=outcome.size or len(content.encode("utf-8")),
                truncated=truncated,
                next_cursor=body_cursor,
            )
        )

    return SessionBundle(
        meeting=title,
        iso_slots=iso_slots,
        iso_slots_extraction=iso_status,  # type: ignore[arg-type]
        pages=pages,
        missing_pages=missing,
    )


def _bundle_body(content: str, include: bool, max_bytes: int) -> tuple[str | None, str | None, bool]:
    if not include:
        return None, None, False
    chunk, _start, end, total, has_more = chunk_utf8(content, start=0, max_bytes=max_bytes)
    cursor = encode_cursor({"o": end}) if has_more else None
    return chunk, cursor, has_more


# --------------------------------------------------------------------------- #
# wiki_status
# --------------------------------------------------------------------------- #
def wiki_status(ctx: ServerContext) -> WikiStatus:
    """Operational status. Contains no confidential wiki content."""
    calendar = ctx.calendar.status()
    try:
        cache_entries: int | None = ctx.cache.count()
    except Exception as exc:  # noqa: BLE001 - status must never fail hard
        logger.warning(
            "Cache count failed: %s",
            safe_exception_summary(exc),
        )
        cache_entries = None
    return WikiStatus(
        base_url=ctx.config.base_url,
        authenticated=ctx.client.active_label is not None,
        auth_mode=ctx.client.active_label,
        user=ctx.client.username,
        ttl_normal_s=ctx.config.ttl_normal_s,
        ttl_meeting_s=ctx.config.ttl_meeting_s,
        current_ttl_s=ctx.calendar.ttl_seconds(),
        ttl_mode=ctx.calendar.ttl_mode(),  # type: ignore[arg-type]
        calendar=calendar,
        cache_entries=cache_entries,
    )
