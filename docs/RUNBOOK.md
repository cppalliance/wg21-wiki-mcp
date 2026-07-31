# Runbook

Operator and agent-consumer guide for `wg21-wiki-mcp`. This document stays
generic: no confidential wiki titles or page content appear here. See
[README.md](../README.md) for install and tool summaries,
[ARCHITECTURE.md](../ARCHITECTURE.md) for design, and [SECURITY.md](../SECURITY.md)
for credential and confidentiality rules.

## MCP host configuration

The server is a **stdio** MCP process. The host launches it as a subprocess and
passes configuration through the launch `env` block (not via tool arguments).

### Cursor (recommended)

Add a server entry under **Settings → MCP** (or edit your MCP config JSON):

```json
{
  "mcpServers": {
    "wg21-wiki": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.3.0",
        "wg21-wiki-mcp"
      ],
      "env": {
        "WIKI_BOT_USERNAME": "YourAccount@yourbot",
        "WIKI_BOT_PASSWORD": "the-bot-password"
      }
    }
  }
}
```

Pin a release tag (`@v0.3.0`) for reproducible behavior. To track the latest
development branch, use `@develop` and add `"--refresh"` as the first `args` entry.

If you installed the console script locally (`pip install -e ".[dev]"`), use
`"command": "wg21-wiki-mcp"` with no `args`.

### Required environment variables

| Variable | Purpose |
| --- | --- |
| `WIKI_BOT_USERNAME` / `WIKI_BOT_PASSWORD` | Bot password (preferred). Create at `Special:BotPasswords` with the Read grant. |
| `WIKI_USER_USERNAME` / `WIKI_USER_PASSWORD` | Normal account SSO fallback. Used when bot credentials are absent or fail. MFA must be off. |

If both bot and user credentials are set, the bot path is tried first. See
[`.env.example`](../.env.example) for optional tuning.

### Startup behavior

On launch the server builds `ServerContext` from the environment, logs in once
during lifespan startup, and keeps the authenticated session for the process
lifetime. Both configuration and login failures surface at startup rather than
on the first tool call, but the codes differ:

- **Code `4` / `CONFIG_ERROR`** — no credentials are configured (missing env vars).
- **Code `2` / `AUTH_ERROR`** — credentials are present but login fails (wrong
  password, MFA, SSO drift, etc.) during startup login.

## Common failures and recovery

### Authentication / MFA (`AUTH_ERROR`, code `2`)

**Symptoms:** Tools fail immediately or after a long session; `wiki_status` shows
`authenticated: false` or an unexpected `auth_mode`.

**Causes and fixes:**

- **Wrong or expired bot password** — Regenerate at `Special:BotPasswords` and
  update the MCP host `env` block. Restart the MCP server (reload MCP in Cursor).
- **Bot path unavailable, user SSO required** — Set `WIKI_USER_USERNAME` and
  `WIKI_USER_PASSWORD`. The server tries bot login first; remove bot vars if you
  intend SSO only.
- **MFA enabled on the user account** — SSO fallback cannot complete an MFA
  challenge headlessly. Use a bot password instead.
- **SSO form drift** — The headless SimpleSAMLphp parser may break after an IdP
  markup change. Bot passwords are unaffected; switch to bot auth or report the
  regression.

Error messages are fixed strings with no credential material. Check host logs
only if your environment attaches a handler to the package logger; the server
never logs passwords or page content.

### Session drop (`readapidenied`)

**Symptoms:** Intermittent failures mid-session; often self-heals on the next call.

**Behavior:** `WikiClient.api()` detects `readapidenied`, re-authenticates via
the pinned login path, and retries. No operator action is usually needed unless
re-login also fails (then see auth/MFA above).

### Cache directory issues

**Default location:** `~/.isocpp.wiki/` (override with `ISOCPP_WIKI_CACHE_DIR`).

**Symptoms:**

- Permission errors creating or writing the cache directory.
- Stale content when you expect a fresh revision.

**Fixes:**

- Ensure the directory is writable by the user running the MCP server.
- Point `ISOCPP_WIKI_CACHE_DIR` to a writable path if the home directory is
  restricted (containers, shared profiles).
- Force a live re-fetch for one page with `get_page(..., refresh=True)` (see
  Agent usage below). This bypasses the cache for that title only.
- Delete the cache directory to reset all entries (local confidential data only;
  do not commit cache files).

### Lock timeout (cross-process single-flight)

**Symptoms:** Slower fetches under heavy parallel use; a WARNING log with a
`title_hash` (not the page title) when the per-page file lock cannot be acquired
within 60 seconds.

**Behavior:** The fetch proceeds **without** the cross-process lock rather than
blocking forever. Duplicate upstream fetches are possible but harmless; content
and provenance remain correct.

**Fixes:**

- Reduce parallel `get_page` calls for the same title across multiple MCP
  processes.
- Check for a stuck process holding locks in the cache directory.
- Increase patience: contention usually clears when the holder finishes.

### Calendar / meeting TTL (`wiki_status`)

**Symptoms:** `wiki_status` shows `parse_status: failed` or
`ttl_mode: conservative`; cache refreshes more often than expected.

**Behavior:** On public calendar fetch/parse failure the server biases to the
short (meeting) TTL so content stays fresh rather than stale. This does not
change returned wikitext.

**Fixes:**

- Verify outbound HTTPS to `isocpp.org` from the host machine.
- Override windows with `ISOCPP_WIKI_MEETING_WINDOWS` (comma-separated
  `YYYY-MM-DD/YYYY-MM-DD` pairs) when autodetection is wrong.
- Tune `ISOCPP_WIKI_TTL_NORMAL` / `ISOCPP_WIKI_TTL_MEETING` if needed.

### Meeting-time load (`get_meeting_sessions`)

**Symptoms:** Slow first call at the start of a meeting hour; `FETCH_ERROR` after
~30s under wiki lag; multiple agents calling `get_meeting_sessions` at once.

**Behavior:**

- Outlink discovery (which subpages exist) is cached with the same meeting-aware
  TTL as page bodies. Repeated calls within that window skip re-enumerating links.
- The page bundle fetch is bounded to **30 seconds** total wait; if locks or
  upstream API calls exceed that, the tool raises `FETCH_ERROR` (code 3) instead
  of blocking other tools indefinitely.
- `WikiClient.api()` accepts an optional per-call timeout, applies it to the
  underlying mwclient HTTP request, and releases the client lock before retry
  backoff sleep. Read-only ``query`` calls share a reader lock and may run
  concurrently; login, re-login, and non-``query`` actions take an exclusive
  writer lock.

**Expected latency:** See the "Meeting-time performance" table in
[ARCHITECTURE.md](../ARCHITECTURE.md). Cache-warm repeats are typically sub-second;
cache-cold first calls for a ~40-page meeting are often a few seconds on a healthy wiki.

**Fixes:**

- Retry after a `FETCH_ERROR` once wiki lag clears (`wiki_status` / upstream health).
- Warm the cache with one `get_meeting_sessions` call before parallel agent use.
- Ensure multiple MCP processes share one cache directory (default) so outlink
  indexes and page bodies are reused.

### Malformed pagination cursor (`INVALID_PARAMS`, code `-32602`)

**Symptoms:** `next_cursor` from a prior response no longer works.

**Causes:** Cursor tampering, version skew, or passing a cursor to the wrong
tool.

**Fix:** Restart pagination from the first page (omit `cursor`). Cursors are
opaque and tool-specific.

### CI live/canary jobs blocked at the wiki edge

**Symptoms:** The `live (secrets)` or `canary (secrets)` job fails with HTTP 403,
429, or 503 from the wiki edge, on a push to `develop`/`master` or on a same-repo
pull request, while the same tests pass from a maintainer's machine.

**Cause:** Cloudflare blocks GitHub-hosted runner address ranges. Both jobs
therefore bring up a TorGuard tunnel first on any run that sets `VPN_REQUIRED`
(pushes, same-repo pull requests, and manual dispatches), carrying wiki traffic
only, and the tunnel's exit address can itself land on the blocklist over time.
Fork pull requests set neither `VPN_REQUIRED` nor `CI_REQUIRE_LIVE_CREDS`, so
they build no tunnel and skip on a block rather than reaching this symptom.

**Which failure is it?** The step name says so. `Connect VPN` failing means the
tunnel never came up, and its message separates a rejected login from a download
failure from a handshake timeout. `Verify split tunnel` failing means the tunnel
came up but something downstream is wrong, and its message separates a broken
split from a blocked exit address. If the block appears later, during the tests
themselves, the pytest failure carries the same distinction: a message naming
`TORGUARD_VPN_LOCATION` means the exit address is blocked, while one saying the
VPN dropped means the tunnel died after `Verify split tunnel` had passed.

**Fix, for a blocked exit address:** Rotate to another location. Set
`TORGUARD_VPN_LOCATION` to a different bundle basename, then re-run the job.
Confirm the new location first, from a machine connected to it:

```bash
curl --max-time 30 -s -o /dev/null -w '%{http_code}\n' \
  'https://wiki.isocpp.org/api.php?action=query&meta=siteinfo&siprop=general&format=json'
```

200 means that exit address is clear. `000` means no HTTP response arrived
within the timeout, which is a stalled or dead route rather than a block, so
check the connection itself before rejecting the location. The authoritative
list of basenames is whatever `OpenVPN-UDP-Linux.zip` currently contains; unzip
it to read them, or set `TORGUARD_VPN_LOCATION` to a name that is not in it and
let the resulting `Connect VPN` failure print the full list.

Prefer a repository or environment **variable** over a secret for this one. The
location is not a credential, and as a secret the runner masks it, which blanks
out the value in exactly the message you would be reading. The workflow accepts
either, preferring `vars`.

**Diagnosis:** On failure both jobs upload the OpenVPN log as the `vpn-log-live`
or `vpn-log-canary` artifact, on the same `VPN_REQUIRED` runs that build the
tunnel; a run without it has no tunnel and so no log to upload. It is written at
`--verb 3`, which records the handshake and the negotiated cipher but no
credentials.

**If TorGuard itself is down:** every run that sets `VPN_REQUIRED` goes red for
reasons unrelated to the change under test, because `Connect VPN` cannot fetch
the bundle or complete a handshake. There is no automatic fallback, since running the live tier unrouted
would just fail at Cloudflare instead. Wait for service to come back, then use
**Re-run failed jobs** on the original run and merge only once `live (secrets)`
and `canary (secrets)` are green. Re-run that way rather than starting a fresh
**Run workflow** dispatch: the re-run keeps the original event, which is what
sets `CI_REQUIRE_LIVE_CREDS`, while a dispatch leaves it unset and a still-blocked
edge would then skip the live assertions instead of failing on them.

Both jobs are required status checks and this repository documents no bypass, so
a long outage blocks every merge to `develop` and `master`. Relaxing that is a
ruleset change, which belongs to a maintainer with admin rights on the
repository.

## Agent usage notes

### Which tool when

| Goal | Tool | Notes |
| --- | --- | --- |
| Find pages by keyword | `search_wiki` | Snippets are **not** verbatim; always follow with `get_page`. |
| Read authoritative text | `get_page` | Byte-faithful wikitext + provenance (`url`, `oldid_url`, `revid`). |
| Browse a namespace | `list_namespaces` → `list_pages` | Resolve numeric namespace ids first; `list_pages` includes `namespace_name`. |
| Discover meetings | `list_meetings` | Newest first; `is_active` and `window_start`/`window_end` when the public calendar matches. |
| Meeting landing + index | `get_meeting_overview` | Verbatim home page + outlink index. |
| Compose a schedule | `get_meeting_sessions` | Returns a **bundle** (time slots + pages); the agent composes the schedule. |
| Track recent edits | `get_recent_changes` | High value during meetings. |
| Health check | `wiki_status` | Auth path, TTL mode, cache stats — no wiki content. |

The server **never composes meeting schedules**. Use `get_meeting_sessions` for
raw materials and build the answer yourself.

### Chunk reassembly (`get_page`)

Large pages are split on **UTF-8 byte boundaries** (default ~48 KiB per chunk).

1. Call `get_page(title)` — read `content` and check `chunk.has_more`.
2. While `chunk.has_more`, call `get_page(title, cursor=chunk.next_cursor)`.
3. Concatenate all `content` strings in order.

Reassembled text is byte-for-byte identical to the wiki revision. Provenance
(`revid`, URLs) is the same on every chunk. For sections, pass the same
`section` index on every chunk request.

### `refresh=True`

Pass `refresh=True` to `get_page` when you need the **latest live revision**
and cannot rely on cache freshness (e.g. active meeting, suspected stale
`revid`). This forces a network fetch for that title (or section) only; other
cached pages are untouched.

During meetings the cache TTL is short (default one hour); outside meetings it
is long (default one week). `wiki_status` reports the active TTL mode.

### Provenance and citations

Every `get_page` result includes:

- `provenance.url` — canonical page link
- `provenance.oldid_url` — permanent link pinned to the exact `revid`
- `provenance.from_cache` — whether the body was served from SQLite

Quote from `content` and cite `oldid_url` when the answer must be auditable.

### Error branching

| Code | When | Agent action |
| --- | --- | --- |
| `1` | Page or section missing | Try `search_wiki` or `list_pages`; do not invent content. |
| `2` | Auth failure | Ask the operator to check credentials / MFA / bot password. |
| `3` | Fetch / network error | Retry; check connectivity. |
| `4` | Config missing | Ask the operator to set credential env vars in the MCP host. |
| `-32602` | Bad cursor | Restart pagination without `cursor`. |

See [ARCHITECTURE.md#error-contract](../ARCHITECTURE.md#error-contract) for the
full contract.

## Quick diagnostics checklist

1. Call `wiki_status` — confirm `authenticated`, `auth_mode`, `ttl_mode`, and
   `calendar.parse_status`.
2. Call `get_page` on a known small page without `refresh` — confirm provenance
   fields populate.
3. If auth looks good but content is wrong, retry with `refresh=True`.
4. If startup fails, check the error code: `4` means set credential env vars;
   `2` means vars are set but login failed (see Authentication / MFA above).
   Restart the server process after fixing the MCP host config.

## Related documentation

- [README.md](../README.md) — install, tool table, error codes summary
- [ARCHITECTURE.md](../ARCHITECTURE.md) — data flow, parse-vs-offload, what may break
- [CONTRIBUTING.md](../CONTRIBUTING.md) — dev setup, tests, confidentiality rules
- [SECURITY.md](../SECURITY.md) — credentials and log redaction
