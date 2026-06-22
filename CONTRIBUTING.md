# Contributing

Thanks for helping improve `wg21-wiki-mcp`. This is a small, focused project;
the bar is production-grade quality and strict confidentiality.

## Dev setup

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Requires Python 3.10+. The project is pure-Python and runs on Windows, macOS,
and Linux.

Install the git hooks once so checks run automatically on commit:

```bash
pre-commit install
```

Run all hooks on the whole tree at any time:

```bash
pre-commit run --all-files
```

The hooks are lint/format/type only (ruff, ruff-format, mypy, plus basic file
hygiene); the test suite runs via `pytest`/CI, not in the commit hook.

## Running checks

```bash
ruff check src tests        # lint
mypy src                    # type-check
pytest -m "not live"        # offline tests + 90% coverage gate
```

The offline suite mocks the network with synthetic fixtures and needs no
credentials. To run the live tier against the real wiki, set the env vars from
`.env.example` (or a local `.env`) and run:

```bash
pytest -m live --no-cov
```

Live tests auto-skip when credentials are absent and must never print or store
wiki content.

## Testing authentication paths

The wiki client supports two credential paths: **bot password** (default) and
**user SSO** (``clientlogin``, then headless SimpleSAMLphp form-flow). Offline
tests cover both without real credentials:

| Path | Test module | What it exercises |
|------|-------------|-------------------|
| Bot password | ``tests/test_wiki_client.py`` | Login selection, re-login on ``readapidenied``, batch fetch |
| ``clientlogin`` | ``tests/test_wiki_client_saml.py`` | ``_try_clientlogin`` success/failure (API errors return ``False``) |
| SAML/SSO HTTP | ``tests/test_wiki_client_saml.py`` | Synthetic HTML fixtures under ``tests/fixtures/saml/`` mocked with ``responses``: happy path, missing form, missing fields, MFA/no-``SAMLResponse``, auto-follow branch |

SAML fixtures are generic (no real IdP markup). Auth failures must raise
:class:`~wg21_wiki_mcp.models.AuthError` with fixed, safe messages — see
``tests/test_wiki_client_saml.py::test_auth_errors_contain_no_credentials`` and
``tests/test_log_safety.py``.

To run only the auth tests:

```bash
pytest tests/test_wiki_client.py tests/test_wiki_client_more.py tests/test_wiki_client_saml.py -m "not live"
```

## Confidentiality rules (important)

- Never embed real wiki page titles, namespace names, or page content in source,
  tests, fixtures, or docs. The wiki structure is discovered at runtime.
- Only the public meeting schedule and invented/generic placeholders belong in
  the repo. Tests use synthetic data (e.g. `"2026-06 Alpha"`).
- The cache DB and any dumps are git-ignored; do not commit them.
- See [SECURITY.md](SECURITY.md) for credential handling.

## Start here (reading order)

1. [`config.py`](src/wg21_wiki_mcp/config.py) - what configuration exists.
2. [`wiki_client.py`](src/wg21_wiki_mcp/wiki_client.py) - auth + raw API access.
3. [`cache.py`](src/wg21_wiki_mcp/cache.py) + [`fetch.py`](src/wg21_wiki_mcp/fetch.py) - the shared cache and the central fetch chokepoint.
4. [`meetings.py`](src/wg21_wiki_mcp/meetings.py) - meeting-aware TTL.
5. [`tools.py`](src/wg21_wiki_mcp/tools.py) + [`models.py`](src/wg21_wiki_mcp/models.py) - the tools and their outputs.
6. [`server.py`](src/wg21_wiki_mcp/server.py) - how tools are registered and run.

[ARCHITECTURE.md](ARCHITECTURE.md) explains the design, the parse-vs-offload
policy, and what may break.

## Pull requests

- Keep changes focused; add tests for new behavior and keep coverage >= 90%.
- Run ruff, mypy, and the offline suite before opening a PR.
- Update `CHANGELOG.md` for user-visible changes.

## Branching and releases

- `develop` is the default branch where day-to-day work lands.
- `master` is the release branch and always points at the latest released
  commit; users installing `@master` get the latest release.
- To cut a release `X.Y.Z`:
  1. Bump the version in two places: `version` in [pyproject.toml](pyproject.toml)
     and `__version__` in [src/wg21_wiki_mcp/__init__.py](src/wg21_wiki_mcp/__init__.py)
     (keep them in sync).
  2. Move the `## [Unreleased]` entries in [CHANGELOG.md](CHANGELOG.md) under a
     new `## [X.Y.Z] - YYYY-MM-DD` heading and update the link references.
  3. Open a PR from `develop` to `master`; merge once CI is green.
  4. Tag the merge commit on `master` and push the tag:
     `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
  5. Create the GitHub Release from that tag.
