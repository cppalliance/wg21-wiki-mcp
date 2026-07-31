# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section uses these headings when applicable: `Added`, `Changed`,
`Deprecated`, `Removed`, `Fixed`, `Security`. See [STABILITY.md](STABILITY.md)
for the pre-1.0 API stability policy and deprecation timeline.

## [Unreleased]

### Added
- CI `canary (secrets)` and `live (secrets)` jobs reach the wiki over a TorGuard
  tunnel (`scripts/ci/torguard_vpn.sh`), because Cloudflare blocks GitHub-hosted
  runner address ranges. This is the infrastructure fix for the root cause found
  in [#53](https://github.com/cppalliance/wg21-wiki-mcp/issues/53), which was
  closed on the skip-on-WAF behaviour that kept the blocked runs from being
  mistaken for credential faults. The tunnel is split: only `wiki.isocpp.org`
  crosses it, so runner traffic keeps its direct path. Runs only where secrets are readable,
  leaving fork PRs and local runs unaffected. Needs the `TORGUARD_VPN_USERNAME`
  and `TORGUARD_VPN_PASSWORD` secrets, both readable by the `live-wiki`
  environment, plus `TORGUARD_VPN_LOCATION`, which the workflow takes from a
  variable when one is set to a non-empty value and otherwise falls back to a
  secret of the same name; on failure the OpenVPN log is uploaded as an artifact.
  Rotation is documented in [docs/RUNBOOK.md](docs/RUNBOOK.md).
- `shellcheck` lint step for `scripts/ci/*.sh` in the ubuntu/py3.12 `test` job.
- Dated CI waiver for the `mwclient` 24-month PyPI release-age hard gate
  (`config/mwclient-release-age-waiver.json`, expires **2026-08-13**); tracks
  succession work in issue [#88](https://github.com/cppalliance/wg21-wiki-mcp/issues/88).
  `scripts/check_mwclient_release_age.py` accepts `--waiver-file` / `--waiver-until`.
- Meeting-time composite latency benchmarks (`tests/test_meeting_time_benchmark.py`),
  committed baseline `benchmarks/meeting-time-baseline.json`, and a CI regression gate
  in the `benchmark` job (50% mean threshold, same as cache). Regression checking uses
  `scripts/check_benchmark_regression.py`. `get_meeting_overview` is out of scope for
  this baseline; only `get_meeting_sessions` warm/cold paths.

### Changed
- CI `canary (secrets)` and `live (secrets)` jobs fail loudly on protected CI
  (`CI_REQUIRE_LIVE_CREDS=1`) when wiki credentials are missing or the live read
  path is blocked (WAF edge or network unreachable); fork PRs and local runs
  without credentials continue to skip gracefully ([#86](https://github.com/cppalliance/wg21-wiki-mcp/issues/86)).
- A protected-CI edge block now names its own cause. `tests/live_support.py`
  checks for a tun interface and reports either a tunnel that dropped mid-run or
  an exit address that is itself blocked. The HTTP status is identical in both
  cases while the remedies differ.

## [0.3.0] - 2026-07-24

### Security
- `log_safety.py`: broaden credential-pattern redaction (`Cookie`, `Set-Cookie`,
  `session=`, `api_key`, bare `Bearer`, multiline `Authorization`); recursively
  scrub nested log `args`, `exc_info`, and pre-formatted `exc_text`.
- `log.py`: `_install_package_log_safety()` inserts a `_SilentHandler` carrying
  `LogSafetyFilter` first on the `wg21_wiki_mcp` package logger, so propagated
  records from child loggers, including raw `logging.getLogger("wg21_wiki_mcp.*")`
  callers, are redacted before any host handler emits.

### Changed
- `pagination.py`: add `cursor_offset()`; malformed or negative offset `o` values
  raise `McpError(INVALID_PARAMS)` from `search_wiki`, `get_page`, and
  `list_meetings` instead of mapping to `FETCH_ERROR`.
- `wiki_client.py`: close the prior `mwclient.Site` connection on re-login;
  remove unreachable SAML retry tail and unused `_http_timeout`.
- `meetings.py`: narrow `ensure_fresh` critical section (HTTP GET outside lock).
- `config.py`: default `user_agent` built from `__version__` (no manual drift).
- `fetch.py`: use `composite_deadline()` and shared `PAGE_FETCH_TIMEOUT_MSG`;
  `dataclasses.replace` for section outcomes; shared `DEFAULT_MAX_LOCK_ENTRIES`.
- `server.py`: MCP tool byte-limit defaults reference `tools.py` constants.
- `context.py`: remove redundant `current_ttl()` wrapper.
- Documentation: trim and de-duplicate `README.md`; fix install pins and
  `@develop` branch references; update `ARCHITECTURE.md` component map;
  mark `docs/FIRST_PYPI_PUBLISH.md` as completed archive.

### Added
- `list_meetings` docstring notes offset-pagination instability when meeting
  titles change between pages.
- Tests for log-filter idempotency, stdlib logger redaction, cursor offset
  validation, and site replacement on re-login.

## [0.2.1] - 2026-07-23

### Security
- `config.py`: `Credentials.password` is now `field(repr=False)` so the plaintext
  password is excluded from `repr()`/`print()`/debugger output; because `Config`
  nests `Credentials`, `repr(Config(...))` no longer leaks it either. Complements
  the log-redaction layer, which does not cover `repr`. No runtime behavior change;
  the value remains accessible via attribute access.
- `log_safety.py`: guard the module-level `_redactions` registry with a lock;
  `sanitize_text()` snapshots under the lock so concurrent register/clear cannot
  raise or skip redactions. Structural safety under free-threaded Python 3.13+;
  CI runs CPython 3.13 (GIL), not `python3.13t`.

### Changed
- CI enforcement: cache benchmark regression gate is now blocking (removed
  `continue-on-error`); mwclient release-age check splits a 12-month advisory
  review signal (`continue-on-error`) from a blocking 24-month hard migration
  trigger aligned with `docs/DEPENDENCY-RISK.md`. Benchmark baseline means
  refreshed to honest CI measurements and the regression tolerance widened to 50%
  (µs-scale microbenchmarks have shared-runner StdDev ~= mean); the gate re-runs
  once before failing to absorb one-off runner spikes.
- SAML headless login (`_saml_login`): configurable IdP field names
  (`WIKI_SAML_USERNAME_FIELD`, `WIKI_SAML_PASSWORD_FIELD`) and per-step timeout
  (`WIKI_SAML_TIMEOUT_S`); DOM-based field detection fallback; retry on transient
  HTTP errors; diagnostic `AuthError` context (url, status, field names); DEBUG
  step logging.
- Tighten `mwclient` runtime pin to `>=0.11.0,<0.12` so unexpected minor/major
  releases cannot land silently via Dependabot.
- `docs/API.md` is now generated from source (`scripts/build_api_docs.py`) instead
  of hand-maintained per-tool tables.

### Added
- Generated MCP API reference (`scripts/build_api_docs.py`, `docs/API.md`) built from
  tool docstrings, type hints, and Pydantic models; CI fails when the committed
  reference drifts from source.
- Cache throughput benchmarks (`tests/test_benchmark.py`, `pytest-benchmark`) with
  CI JSON artifacts and a regression gate against `benchmarks/cache-baseline.json`
  (`scripts/check_cache_benchmark_regression.py`). See the `### Changed` entry above
  for the gate's blocking status and tolerance.
- Opt-in HTTP transports via `WG21_TRANSPORT` (`sse`, `streamable-http`; default
  `stdio` unchanged). Optional `WG21_HTTP_HOST` / `WG21_HTTP_PORT` for bind
  address. See `docs/TRANSPORT-EVAL.md`.
- `docs/DEPENDENCY-RISK.md`: mwclient upstream assessment, alternatives evaluation,
  abstraction boundary, and succession plan; CI PyPI release-age check
  (`scripts/check_mwclient_release_age.py`, `dependency health` job).
- `docs/FIRST_PYPI_PUBLISH.md`: one-time Trusted Publisher checklist for the first PyPI release.
- Meeting-time **latency gate** CI step on `ubuntu-latest` / Python 3.12 (`pytest -m latency_gate`).
- Thread-safe `get_context()` / lifespan shutdown via `_state_lock` in `server.py`.
- PyPI publish workflow (`.github/workflows/publish.yml`) on published GitHub
  Release via Trusted Publisher; `requirements-lock.txt` with CI lockfile
  reproducibility gate; README PyPI install instructions and PyPI project URL
  in `pyproject.toml`.
- Release supply-chain hardening: GitHub Actions and pre-commit hooks pinned to
  full SHA digests; publish workflow generates a CycloneDX SBOM, signs
  distributions with Sigstore, and attaches the SBOM plus `.sigstore.json`
  bundles to the GitHub Release; Dependabot `pin-actions` group keeps action
  SHA pins current.
- CI canary smoke gate (`canary (secrets)`) and live tier wired to the `live-wiki`
  GitHub environment for personal secrets; per-module coverage floors, coverage
  XML artifact upload, and `@pytest.mark.canary` live smoke tests.
- `CODEOWNERS`, Dependabot config (weekly pip + GitHub Actions updates), CI
  pre-commit gate, and governance/branch-protection docs in CONTRIBUTING.md.
- `STABILITY.md`: pre-1.0 API stability tiers, SemVer rules for 0.x, and
  deprecation process (minimum one minor version with warning before removal).
- `wg21_wiki_mcp.deprecation.warn_deprecated()`: `DeprecationWarning` helper
  for future deprecations.
- `Development Status :: 3 - Alpha` PyPI classifier and README versioning section.
- Meeting-time stall mitigation: cached outlink discovery for composite meeting
  tools, `PageFetcher.get_pages(max_wait_s=…)` (default 30s for
  `get_meeting_sessions`), and optional per-call `WikiClient.api(timeout=…)`.
  Documented in ARCHITECTURE.md and docs/RUNBOOK.md.
- Debug log when stale outlink index is served after lock contention or discovery
  timeout (`title_hash` only; no page titles in logs).

### Changed
- `search_wiki`: new `include_snippet` parameter (default `False`); snippets are omitted
  unless explicitly requested. `SearchHit.snippet_warning` removed; `SearchResults.include_snippet`
  documents caller opt-in. Tool and server instructions strengthened to state snippets must
  not be cited as verbatim wiki content.
- `CODEOWNERS` and `CONTRIBUTING.md`: add `@wpak-ai` as co-maintainer on all owned paths.
- `CONTRIBUTING.md`: document ruleset `wg21-wiki-mcp-protection` (replaces legacy branch-protection UI wording).

### Fixed
- In-process lock slots no longer leak their reference count when acquisition times
  out on an exhausted deadline (`PageFetcher._acquire_inproc` and the outlink lock in
  `tools.py`). Previously a timed-out waiter left `slot.users` inflated, so the slot
  could never be evicted and the lock maps grew without bound under repeated timeouts.
  The reference-counted lock map is now a single shared implementation
  (`wg21_wiki_mcp.locks.EvictableLockMap`) used by both modules, so the fix cannot
  diverge.
- `WikiClient.api()`: replace the exclusive session lock with a reader-writer lock so
  concurrent read-only ``query`` calls are not serialized across network round-trips;
  login, re-login, and close still take an exclusive writer lock.
- `WikiClient.api()`: release the RLock before retry backoff sleep so concurrent
  tool invocations are not blocked for the full sleep duration.
- `PageFetcher._resolve_network`: revalidation and batched network fetch no longer
  hold cross-process file locks for every title at once; file locks are acquired
  per-title only during cache write, reducing lock convoys in composite tools like
  `get_meeting_sessions`.

### Deprecated

_(none)_

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

[Unreleased]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cppalliance/wg21-wiki-mcp/releases/tag/v0.1.0
