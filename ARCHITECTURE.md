# Architecture

`wg21-wiki-mcp` is a local stdio MCP server that serves the WG21 committee
MediaWiki as a verifiable source of truth. This document explains how it is
built, the load/correctness policies it follows, what can break, and where it
could go next.

## Component map

```
server.py        FastMCP stdio server; registers tools; lifespan logs in.
  tools.py       Tool logic (structured outputs). All page content via the fetcher.
    context.py   ServerContext: wires config + client + calendar + cache + fetcher.
      fetch.py       PageFetcher: cache-first, batched, single-flight retrieval.
        cache.py     SQLite (WAL) shared cache in ~/.isocpp.wiki/ + per-page lock paths.
        wiki_client.py  Authenticated MediaWiki client (bot/user SSO, re-login, batch).
      meetings.py  MeetingCalendar: public-calendar -> meeting-aware cache TTL.
  models.py      Pydantic response models; re-exports error types from errors.py.
  errors.py      Error hierarchy, documented codes, and to_mcp_error() mapping.
  pagination.py  Opaque cursors + UTF-8-safe chunking.
  wikitext.py    Deterministic agenda time-slot extraction (the only content parse).
  config.py      Environment-driven configuration; re-exports ConfigError from errors.py.
```

## Request data flow

1. A tool is called (e.g. `get_page`).
2. It asks `PageFetcher` for the title(s) with the current meeting-aware TTL.
3. The fetcher serves fresh cache entries directly. For misses it takes a
   per-page lock (cross-process + in-process), re-checks the cache, cheaply
   revalidates stale-but-unchanged pages by `revid`, and batch-fetches the rest
   via the `WikiClient` (`titles=A|B|...`, up to 50 per request).
4. Results are stored verbatim and returned with provenance (URL, `oldid` URL,
   `revid`, timestamps). The tool chunks long content on UTF-8 boundaries.

## Authentication

`WikiClient` auto-selects the credential path: it tries the **bot password**
first (default), and on login failure falls back to **user SSO**
(`clientlogin`, else the headless SimpleSAMLphp form-flow). If neither works it
raises immediately. The successful path is **pinned** and reused for every later
re-login. `api()` retries transient errors and, on `readapidenied` (a dropped
session), transparently re-logs-in via the pinned path.

## Correctness: source of truth

- Page content is returned **byte-for-byte**; nothing in the path
  (fetch -> cache -> tool) transforms it.
- Every result carries verifiable provenance; redirects and title normalization
  are surfaced so content is never misattributed.
- Long pages are chunked only on UTF-8 boundaries; partiality is always signaled
  (`has_more`/`next_cursor`), and reassembling chunks reproduces the page exactly.
- Missing pages, fetch failures, and auth failures are distinct, explicit
  outcomes; nothing is fabricated, and `refresh=True` forces a live re-fetch.

## Parse-vs-offload policy

The server emits **structured data only when it is API-provided or mechanically
deterministic (~100%)**; everything else is returned verbatim for the calling
LLM to interpret. This was chosen after surveying the wiki's real formats across
many meetings (agendas, room tables, and page roles vary widely by year).

- Structured (safe): search results, `allpages`, namespaces, recent changes,
  page links, revision metadata. Search snippets are flagged non-verbatim.
- Deterministic extraction (only when the exact signal is present): agenda
  `session-start`/`session-end` ISO slots; meeting-title detection
  (`^\d{4}-\d{2} .+$`). Each reports an extraction status and degrades to "not
  found" rather than guessing.
- Never parsed into truth: room/day tables, composed schedules, slot<->group
  <->paper mapping, working-group and evening-session bodies. `get_meeting_sessions`
  therefore returns a **bundle** (deterministic time slots + relevant pages
  verbatim + provenance), and the LLM composes the schedule.
- The one sanctioned content parser is the public meeting-calendar TTL parser,
  because a misparse only changes cache freshness, never returned content.

## Shared cache and TTL

A single SQLite (WAL) database in `~/.isocpp.wiki/` is shared by all of the
user's local agents. WAL gives concurrent readers; a per-page `filelock`
provides cross-process single-flight so the same page is never fetched twice at
once. The TTL is **meeting-aware**: pages live for a week normally and are
re-checked hourly during the three-times-a-year meetings, detected from the
public meetings calendar (with a conservative bias to the short TTL on any parse
failure).

## What may break

- **SSO form drift.** Headless user login parses the SimpleSAMLphp login form;
  a markup change there breaks the user path (the bot path is unaffected). It is
  isolated in `WikiClient._saml_login`.
- **Session drops.** The wiki drops sessions on long runs (`readapidenied`);
  handled by automatic re-login, but heavy concurrent use re-authenticates more.
- **Agendas without `session-start`.** Most historical meetings lack the
  machine-readable agenda; `iso_slots` is then empty (`extraction: not_found`)
  and the raw page is bundled instead - by design.
- **Wiki format drift across years.** Room/agenda/evening formats vary; the
  server never depends on them beyond the agenda signal.
- **Public meetings-page format change.** Would degrade TTL accuracy only
  (conservative fallback keeps data fresh); override via `ISOCPP_WIKI_MEETING_WINDOWS`.

## Error contract

Every domain error is converted to a structured `McpError` / `ErrorData` with a
distinct code **before** it crosses the tool boundary. The single mapping
function lives in `errors.py`; `server._wrap()` calls it around every tool
invocation.

| Code | Name | Meaning |
|------|------|---------|
| `1` | `PAGE_NOT_FOUND` | The requested page does not exist on the wiki. |
| `2` | `AUTH_ERROR` | Authentication failed for every configured credential path. |
| `3` | `FETCH_ERROR` | A network or API error prevented retrieval after all retries. Raw `mwclient.APIError` that escapes the client layer is wrapped here too. |
| `4` | `CONFIG_ERROR` | Required server configuration is missing or invalid (credentials env vars not set). |
| `-32602` | `INVALID_PARAMS` | A pagination cursor is malformed or expired. Defined by the MCP / JSON-RPC protocol layer in `pagination.py`. |

**Safety invariants:**
- Auth-error messages are fixed strings; they never reflect the underlying
  login-exception text, which could carry credential-adjacent information.
- All messages are actionable and contain no wiki page content.
- `McpError` instances (including `INVALID_PARAMS`) pass through `_wrap` unchanged.

## Future work

- Attachment/file (PDF) retrieval (currently wikitext pages only).
- Richer deterministic extraction if/when meeting formats stabilize.
- SAML ECP profile for user auth if the IdP enables it (more robust than form-flow).
- Optional negative caching of missing pages.
