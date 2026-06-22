# Security Policy

## Credentials

This server authenticates to the WG21 wiki with credentials supplied through the
environment (the MCP host's launch `env` block, or a local `.env` for
development). Credentials are:

- read only from the process environment at startup,
- held in memory for the lifetime of the process,
- never written to logs, tool output, the cache, or any file,
- never embedded in source code or tests.

Use a **bot password** (`Special:BotPasswords`, Read grant) where possible: it is
scoped and revocable. User SSO credentials are supported as a fallback.

## Confidentiality of wiki content

The committee wiki is access-restricted and its contents are confidential. This
project treats all wiki content as confidential:

- No wiki page titles, namespace names, or page content are embedded in source,
  tests, fixtures, or documentation. The wiki structure is discovered at runtime.
- Only the **public** meeting schedule (from isocpp.org) and generic placeholders
  appear in the repository.
- The local cache database and any dumps are git-ignored.

Tool output returned to an authenticated user may contain wiki content (they
already have wiki access); logs and committed artifacts must not.

## Log redaction guarantee

Logging uses the stdlib `logging` module (`wg21_wiki_mcp.log`). A centralized
redaction layer (`wg21_wiki_mcp.log_safety`) scrubs every log record before it
reaches a handler:

- Credential passwords from the active configuration are registered at startup
  and replaced with `[REDACTED]` if they ever appear in a log message.
- Common credential-like patterns (`password=…`, `token=…`, etc.) are redacted
  even when not pre-registered.
- Log call sites use safe exception summaries (exception type + sanitized
  message) and never include page titles or wikitext. Lock contention logs use
  a content-free title hash, not the title itself.

`AuthError` and other domain error messages are built without upstream exception
text, so authentication failures cannot carry credential material or IdP HTML into
tool errors or logs.

Offline tests assert that registered secrets and sample wiki content never appear
in captured log output (`caplog`).

## Committed-secret scanning

Every pull request runs [Gitleaks](https://github.com/gitleaks/gitleaks) in CI
(`.github/workflows/ci.yml`) using the MIT-licensed CLI binary. We do not use the
`gitleaks-action` wrapper: that action requires a `GITLEAKS_LICENSE` repository
secret for organization-owned repos (free keys are available at
[gitleaks.io](https://gitleaks.io/products)). Running the CLI directly provides
the same scan coverage with no license key. The job fails on any detected secret
in commits reachable from the PR branch (the CLI invocation applies no confidence
filter). This complements the runtime redaction layer: secrets must neither leak at runtime nor be committed.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers via a
GitHub security advisory rather than a public issue.
