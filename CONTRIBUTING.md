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
python scripts/build_api_docs.py --check  # API reference matches source
pytest -m "not live and not latency_gate"  # offline tests + 95% coverage gate
pytest -m latency_gate      # meeting-time load/latency gate (ubuntu/py3.12 in CI)
```

Regenerate [docs/API.md](docs/API.md) after changing MCP tool signatures,
docstrings, or public response models:

```bash
python scripts/build_api_docs.py
```

The offline suite mocks the network with synthetic fixtures and needs no
credentials. To run the live tier against the real wiki, set the env vars from
`.env.example` (or a local `.env`) and run:

```bash
pytest -m live --no-cov
```

Live tests auto-skip when credentials are absent and must never print or store
wiki content. They also skip (with a logged HTTP status) when the wiki edge
returns **403**, **429**, or **503** — typical Cloudflare/WAF blocks of CI
runner IPs, not credential faults. In CI, the canary tier (`pytest -m canary --no-cov`) runs bot login
plus one read-only tool call; the full live tier runs `pytest -m live --no-cov`.
Both jobs read credentials from the `live-wiki` GitHub environment.

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

Never embed real wiki page titles, namespace names, or page content in source,
tests, fixtures, or docs. See [SECURITY.md](SECURITY.md) for credential handling
and the full confidentiality policy.

## Start here (reading order)

1. [`config.py`](src/wg21_wiki_mcp/config.py) - what configuration exists.
2. [`wiki_client.py`](src/wg21_wiki_mcp/wiki_client.py) - auth + raw API access.
3. [`cache.py`](src/wg21_wiki_mcp/cache.py) + [`fetch.py`](src/wg21_wiki_mcp/fetch.py) - the shared cache and the central fetch chokepoint.
4. [`meetings.py`](src/wg21_wiki_mcp/meetings.py) - meeting-aware TTL.
5. [`tools.py`](src/wg21_wiki_mcp/tools.py) + [`models.py`](src/wg21_wiki_mcp/models.py) - the tools and their outputs.
6. [`server.py`](src/wg21_wiki_mcp/server.py) - how tools are registered and run.

[ARCHITECTURE.md](ARCHITECTURE.md) explains the design, the parse-vs-offload
policy, and what may break. [docs/DEPENDENCY-RISK.md](docs/DEPENDENCY-RISK.md)
covers third-party dependency risk (notably `mwclient`) and the succession plan.
[docs/TRANSPORT-EVAL.md](docs/TRANSPORT-EVAL.md) covers optional HTTP/SSE
transport evaluation and the opt-in prototype.

## Pull requests

- Keep changes focused; add tests for new behavior and keep coverage >= 95%.
- Run `pre-commit run --all-files` and the offline suite before opening a PR.
- When MCP tool signatures, docstrings, or public response models change, run
  `python scripts/build_api_docs.py` and commit the updated `docs/API.md`.
- Update `CHANGELOG.md` for user-visible changes (use `### Deprecated` when
  marking APIs for removal; see [STABILITY.md](STABILITY.md)).

## Governance

This repository is maintained by The C++ Alliance. Review expectations:

- All changes land via pull request against `develop` (see
  [Branching and releases](#branching-and-releases) for how releases are cut).
- [CODEOWNERS](CODEOWNERS) maps critical paths (`src/`, `.github/`, `pyproject.toml`)
  to `@bradjin8` and `@wpak-ai`; GitHub automatically requests review from those
  owners.
- At least **one approving review** from a code owner is required before merge.
- All CI status checks must pass (see below).
- Maintainers merge after approval; external contributors cannot self-merge.

Dependabot opens weekly update PRs for Python dependencies and GitHub Actions.
Those PRs follow the same review and CI requirements as hand-written changes.

## Branch protection

`develop` and `master` are protected by the repository ruleset
**`wg21-wiki-mcp-protection`** (Settings → Rules → Rulesets). Expected rules:

| Rule | `develop` | `master` |
|------|-----------|----------|
| Require pull request before merging | yes | yes |
| Required approving reviews | ≥ 1 (code owner) | ≥ 1 (code owner) |
| Required status checks | CI jobs (see below) | CI jobs (see below) |
| Allow force pushes | no | no |
| Allow deletions | no | no |

Required CI checks (must be green before merge). The ruleset matches checks by
their **exact job display name** (the `name:` field in the workflow), not by
logical groupings — select each check individually when editing the ruleset.

- **pre-commit**
- **lockfile reproducibility**
- **secret scan (gitleaks)**
- **canary (secrets)** — minimal live smoke (bot login + one read-only tool) when
  the `live-wiki` environment secrets are configured; auto-skips on forks
- **live (secrets)** — full live wiki test suite when the `live-wiki` environment
  secrets are configured; auto-skips on forks
- **All 12 offline matrix jobs** (3 OS × 4 Python versions) — each must be
  selected separately:
  - `offline (ubuntu-latest, py3.10)` … `offline (ubuntu-latest, py3.13)`
  - `offline (windows-latest, py3.10)` … `offline (windows-latest, py3.13)`
  - `offline (macos-latest, py3.10)` … `offline (macos-latest, py3.13)`

Each offline job runs ruff, mypy, pytest, and the coverage gate on its
OS/Python combination. The `offline (ubuntu-latest, py3.12)` job also runs a
**Meeting-time latency gate** step (`pytest -m latency_gate`) for composite
meeting-path timing regressions; failure fails that matrix job.

Configure or audit these rules under **Settings → Rules → Rulesets** in the
GitHub repository. This document is the source of truth for what those rules
should enforce.

## Dependency lockfile

Runtime dependencies are pinned in [requirements-lock.txt](requirements-lock.txt)
(generated with `pip-compile`). CI installs from that file and fails if the
lockfile is stale.

When you change runtime dependencies in `pyproject.toml`, regenerate the
lockfile on Linux (or any environment where `pip-compile` resolves the same
graph as CI) and commit the result. CI runs on `ubuntu-latest` with Python
3.12 — do not commit a Windows-generated lockfile, which may include
platform-only transitive deps (`colorama`, `pywin32`) that Linux omits.

```bash
pip install pip-tools==7.6.0
pip-compile pyproject.toml --output-file=requirements-lock.txt --strip-extras
```

## Branching and releases

- `develop` is the default branch where day-to-day work lands, and the branch
  releases are cut from. [publish.yml](.github/workflows/publish.yml) is triggered
  by `release: published`, and GitHub always loads release-triggered workflows
  from the default branch — so publishing a GitHub Release runs the workflow as
  it exists on `develop`.
- `master` is currently **stale**: it still points at the initial repository commit and
  does not contain current release work. Until the branches are reconciled, treat
  `develop` or a release tag as the install source (do not tag or release from `master`).
- To cut a release `X.Y.Z`:
  1. Bump the version in two places: `version` in [pyproject.toml](pyproject.toml)
     and `__version__` in [src/wg21_wiki_mcp/__init__.py](src/wg21_wiki_mcp/__init__.py)
     (keep them in sync).
  2. Move the `## [Unreleased]` entries in [CHANGELOG.md](CHANGELOG.md) under a
     new `## [X.Y.Z] - YYYY-MM-DD` heading and update the link references.
  3. Merge the bump to `develop` via PR; wait for CI to be green.
  4. Publish a GitHub Release for a **new** tag `vX.Y.Z` targeting `develop` — in
     the UI, or `gh release create vX.Y.Z --target develop --title vX.Y.Z --generate-notes`.
     `--target develop` only selects the commit when the release *creates* the tag:
     if `vX.Y.Z` already exists (pre-created or pushed), GitHub ignores `--target`
     and publishes from wherever that tag already points. Ensure `vX.Y.Z` does not
     yet exist so the release creates it on the merged `develop` commit. Publishing
     the release is what fires the workflow; pushing a bare tag no longer triggers a
     publish. The tag must match the bumped `version` / `__version__`, or
     [publish.yml](.github/workflows/publish.yml) fails the build.
  5. [publish.yml](.github/workflows/publish.yml) first runs the full CI suite as a
     gate (`needs: test`), then uploads the sdist and wheel to PyPI via Trusted
     Publisher (OIDC), generates a CycloneDX SBOM, signs the distributions and SBOM
     with Sigstore, and attaches those artifacts to the GitHub Release you
     published. Configure the `pypi` GitHub environment and the matching trusted
     publisher on [pypi.org/project/wg21-wiki-mcp](https://pypi.org/project/wg21-wiki-mcp/)
     before the first release. See [docs/FIRST_PYPI_PUBLISH.md](docs/FIRST_PYPI_PUBLISH.md)
     for the one-time checklist.
  6. Review the auto-generated GitHub Release notes and Sigstore bundles on the
     release assets tab; edit the release description if needed.
