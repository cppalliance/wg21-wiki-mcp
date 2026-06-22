# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-22

### Added
- `errors.py`: centralized error hierarchy (`WikiMcpError`, `AuthError`,
  `PageNotFound`, `FetchError`, `ConfigError`) with documented application
  error codes (`PAGE_NOT_FOUND=1`, `AUTH_ERROR=2`, `FETCH_ERROR=3`,
  `CONFIG_ERROR=4`) and a `to_mcp_error()` mapping function.
- `server._wrap()`: converts every domain / transport exception to a structured
  `McpError` / `ErrorData` at the tool boundary; all nine tools go through it.
- ARCHITECTURE.md "Error contract" section enumerating codes and safety invariants.
- Adversarial and property-based offline tests (Hypothesis) for pagination
  cursors, UTF-8 chunking, wikitext slot parsing, and `WikiClient` HTTP/API
  edge paths; `hypothesis` added to the `dev` extra.
- `log.py`: library-style stdlib logging with a package `NullHandler`.
- `log_safety.py`: centralized log redaction (`register_redactions`,
  `sanitize_text`, `LogSafetyFilter`) and safe auth-error message helpers.
- Resource lifecycle: `Cache.close()` (context-manager supported),
  `WikiClient.close()`, `MeetingCalendar.close()`, and
  `ServerContext.close()` orchestrating teardown; wired into the FastMCP
  lifespan `finally` block.
- WARNING-level observability for calendar fetch/parse failure, `wiki_status`
  cache-count failure, and cross-process lock timeout (logs use `title_hash`,
  not page titles).
- `tests/test_lifecycle.py`: shutdown, lock-map, and swallowed-path log coverage.
- `PageFetcher.get_page_section()`: section reads use section-aware cache keys
  and the same cache-first / single-flight path as full-page fetches.
- SAML/SSO offline tests (`tests/test_wiki_client_saml.py`) with synthetic HTML
  fixtures under `tests/fixtures/saml/` and `responses`-mocked HTTP flows.
- CONTRIBUTING.md section on testing bot-password vs user SSO auth paths.
- `docs/RUNBOOK.md`: operator and agent-consumer troubleshooting (MCP host
  config, common failures, tool-selection guidance).
- `MeetingCalendar.window_for_meeting_title()`: maps meeting titles to public
  calendar ISO date windows.

### Changed
- `models.py` and `config.py` re-export their error types from `errors.py`;
  existing import paths are unchanged.
- `PageFetcher` in-process single-flight locks: per-title user refcount with
  capacity-bounded eviction of idle slots (fixes premature delete-on-release
  under concurrent fetches of the same title).
- `WikiClient.close()` is terminal; `login()` / `api()` reject use-after-close.
- `Cache.close()` guards against a connect/close race that could orphan SQLite
  connections.
- `ServerContext.close()` attempts all resource teardown even when one step fails.
- `get_page(section=...)` routes through `PageFetcher` instead of calling the
  wiki client directly, restoring the architectural fetch chokepoint invariant.
- `types-requests` added to the `dev` extra for mypy parity with CI.
- `list_pages` populates `PageList.namespace_name` from the namespaces API.
- `list_meetings` populates `MeetingRef.window_start` / `window_end` from the
  public meeting calendar when a year-month match exists.
- CHANGELOG 0.1.0 tool list corrected to include `list_namespaces`.

## [0.1.0] - 2026-06-12

### Added
- Initial release: local stdio MCP server for the WG21 wiki.
- Auto-selecting authentication: bot password preferred, headless user SSO
  (SimpleSAMLphp) fallback, with the working path pinned for re-login.
- Cross-process shared SQLite cache in `~/.isocpp.wiki/` with per-page locking
  and a meeting-aware TTL driven by the public meeting calendar.
- Centralized `PageFetcher` with batched (`titles=`) coalescing, single-flight
  de-duplication, and bounded concurrency.
- Tools: `search_wiki`, `get_page`, `list_pages`, `list_namespaces`,
  `list_meetings`, `get_meeting_overview`, `get_meeting_sessions`,
  `get_recent_changes`, `wiki_status`, with opaque cursor pagination.
- Verbatim, provenance-bearing responses (canonical + `oldid` URLs, `revid`).

[Unreleased]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cppalliance/wg21-wiki-mcp/releases/tag/v0.1.0
