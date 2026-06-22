# wg21-wiki-mcp

A local [Model Context Protocol](https://modelcontextprotocol.io) (stdio) server
that gives an LLM agent read access to the **WG21 (ISO C++) committee wiki** at
`wiki.isocpp.org` as a **verifiable source of truth**.

The committee wiki requires a login even to read, and its agendas, straw polls,
documents, and subgroup pages are otherwise hard to reach from an agent. This
server authenticates with your credentials, fetches pages live over the
MediaWiki API, and caches them - returning the **exact** wiki text with a
clickable URL and revision id so every answer can be verified.

> Access requires WG21 membership. This tool stores nothing confidential in its
> source and never logs page content. See [SECURITY.md](SECURITY.md).

## What it does

- **Verifiable content.** Tools return wikitext byte-for-byte with provenance
  (canonical URL, permanent `oldid` URL, `revid`, last-edit time). Nothing is
  summarized or reformatted by the server.
- **Authentication, no browser.** Prefers a MediaWiki **bot password**; falls
  back to your **normal account via headless SSO** (SimpleSAMLphp) if no bot
  password is set. The working path is pinned and reused; sessions that drop are
  transparently re-established.
- **Shared, meeting-aware cache.** A cross-process SQLite cache in
  `~/.isocpp.wiki/` is shared by all your local agents. Pages are cached for a
  week normally and re-checked hourly during the three-times-a-year meetings
  (detected from the public meetings calendar).
- **Token-lean tools** with opaque-cursor pagination and UTF-8-safe chunking of
  long pages.

## Install

This package is not published to PyPI yet; install it from the GitHub source.
Pin a released version (recommended for reproducibility) with `@v0.2.0`, or
track the latest release with `@master` (the release branch). The simplest
option is [`uv`](https://docs.astral.sh/uv/), which can run it without a manual
install:

```bash
# pinned release (reproducible)
uvx --from git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.2.0 wg21-wiki-mcp

# or always the latest release (master tracks releases; --refresh bypasses uv's cache)
uvx --refresh --from git+https://github.com/cppalliance/wg21-wiki-mcp.git@master wg21-wiki-mcp
```

Or install from git with pip/pipx:

```bash
pipx install "git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.2.0"
# or, into a venv:
pip install "git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.2.0"
```

Or from a local clone (for development):

```bash
git clone https://github.com/cppalliance/wg21-wiki-mcp
cd wg21-wiki-mcp
pip install -e ".[dev]"
```

Requires Python 3.10+. Works on Windows, macOS, and Linux.

## Configure

The wiki base URL is fixed (`https://wiki.isocpp.org`), so only credentials are
needed. With an MCP host, pass them in the server's launch `env` block. Since the
package is not on PyPI, run it from GitHub with `uvx`:

```json
{
  "mcpServers": {
    "wg21-wiki": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.2.0",
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

To always run the newest release instead of a pinned one, replace `@v0.2.0`
with `@master` and add `"--refresh"` as the first entry in `args` (so `uv`
re-resolves the branch rather than reusing its cache). Pinning a tag is
recommended for a source-of-truth tool so behavior is reproducible.

If you installed the console script (via pipx/pip), use `"command": "wg21-wiki-mcp"`
with no `args` instead. To use your normal account instead of a bot password,
supply `WIKI_USER_USERNAME` / `WIKI_USER_PASSWORD` (requires that MFA is not
enabled). If both are present, the bot password is used by default. See
[`.env.example`](.env.example) for optional tuning (cache directory, TTLs,
meeting overrides). A bot password is created at `Special:BotPasswords` with the
Read grant and is the recommended, revocable option.

## Tools

| Tool | Purpose |
| --- | --- |
| `search_wiki` | Full-text search; returns titles, API snippets (non-verbatim), and URLs. |
| `get_page` | Verbatim wikitext for a page or section, with provenance; chunked if large. |
| `list_pages` | Enumerate page titles in a namespace. |
| `list_namespaces` | List content namespaces and their numeric ids. |
| `list_meetings` | List discovered meetings (newest first); flags the active one. |
| `get_meeting_overview` | A meeting's landing page plus its subpage index. |
| `get_meeting_sessions` | Raw materials (agenda time slots + relevant pages) to compose a schedule. |
| `get_recent_changes` | Recent edits/new pages, optionally by namespace or since a time. |
| `wiki_status` | Auth path, meeting-aware TTL state, cache stats (no wiki content). |

## Quickstart (programmatic)

```python
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp import tools

ctx = ServerContext.create(Config.from_env())

# Search, then read a page verbatim with a verifiable URL.
hits = tools.search_wiki(ctx, "some topic", limit=5)
page = tools.get_page(ctx, hits.hits[0].title)
print(page.provenance.url, page.provenance.revid)
print(page.content)  # exact wikitext
```

## Dependencies

- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) - the MCP/FastMCP server framework.
- [`requests`](https://requests.readthedocs.io/) + [`mwclient`](https://mwclient.readthedocs.io/) - MediaWiki API access (re-login, batched fetch).
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) + [`lxml`](https://lxml.de/) - parse the SSO login form for headless user auth.
- [`pydantic`](https://docs.pydantic.dev/) - structured tool outputs.
- [`python-dotenv`](https://pypi.org/project/python-dotenv/) - optional `.env` loading for local dev.
- [`filelock`](https://py-filelock.readthedocs.io/) - cross-process single-flight page locking.

## Error contract

Every tool error surfaces as a structured `McpError` with a distinct,
documented code. Messages are actionable and contain no credentials or wiki
page content.

| Code | Name | Meaning |
| --- | --- | --- |
| `1` | `PAGE_NOT_FOUND` | The requested page does not exist on the wiki. |
| `2` | `AUTH_ERROR` | Authentication failed; check credentials in the server config. |
| `3` | `FETCH_ERROR` | Network or API error after retries; check connectivity. |
| `4` | `CONFIG_ERROR` | Credential env vars not set (`WIKI_BOT_USERNAME` / `WIKI_BOT_PASSWORD`). |
| `-32602` | `INVALID_PARAMS` | Malformed or expired pagination cursor. |

See [ARCHITECTURE.md](ARCHITECTURE.md#error-contract) for the full invariants.

## Documentation

- [docs/RUNBOOK.md](docs/RUNBOOK.md) - MCP host setup, troubleshooting, and agent tool-selection guide.
- [ARCHITECTURE.md](ARCHITECTURE.md) - design, data flow, parse-vs-offload policy, what may break, future work.
- [CONTRIBUTING.md](CONTRIBUTING.md) - dev setup, tests, confidentiality rules, where to start reading.
- [SECURITY.md](SECURITY.md) - credential handling and confidentiality.

## License

Boost Software License 1.0 - see [LICENSE](LICENSE).
