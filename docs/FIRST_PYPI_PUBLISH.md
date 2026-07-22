# First PyPI publish (one-time setup)

The [publish workflow](../.github/workflows/publish.yml) runs automatically when
a GitHub Release is **published** (`release: published`) after this one-time
configuration. Until the first successful publish, `pip install wg21-wiki-mcp`
will not resolve on PyPI even though the workflow and README install
instructions are in place.

## Prerequisites

- Merge rights on `cppalliance/wg21-wiki-mcp`
- PyPI account with permission to create or claim the `wg21-wiki-mcp` project
- GitHub admin on the repository (for environments and rulesets)

## 1. Configure Trusted Publishing on PyPI

1. Open [pypi.org/manage/project/wg21-wiki-mcp/settings/publishing/](https://pypi.org/manage/project/wg21-wiki-mcp/settings/publishing/)
   (create the project name first if it does not exist).
2. Add a **pending** trusted publisher:
   - **PyPI project name:** `wg21-wiki-mcp`
   - **Owner:** `cppalliance`
   - **Repository:** `wg21-wiki-mcp`
   - **Workflow name:** `Publish` (file `publish.yml`)
   - **Environment name:** `pypi`

## 2. Create the GitHub `pypi` environment

1. Repository **Settings → Environments → New environment** → name `pypi`.
2. Restrict the environment's deployment branches/tags to the `v*` **tag** pattern
   ("Deployment branches and tags" → "Selected branches and tags" → add a tag rule
   `v*`). The workflow is triggered by a published Release, so the `pypi` deployment
   runs against the release tag ref: a branch-only policy (e.g. `master`) would
   block it, and an unrestricted policy removes an environment-level guardrail
   against accidental non-release deployments. Optionally also require reviewers on
   the `pypi` environment as an extra approval gate.
3. No long-lived PyPI password is required — OIDC supplies the token at publish time.

## 3. Publish a GitHub Release that matches package version

The publish job **fails** if the tag does not match `version` in
`pyproject.toml` and `__version__` in `src/wg21_wiki_mcp/__init__.py`.

Follow [CONTRIBUTING.md](../CONTRIBUTING.md#branching-and-releases):

1. Bump version on `develop`, update `CHANGELOG.md`.
2. Merge the bump to `develop`; wait for CI to be green.
3. Publish a GitHub Release for a **new** tag `vX.Y.Z` targeting `develop`:
   `gh release create vX.Y.Z --target develop --title vX.Y.Z --generate-notes`
   (or use the Releases UI). `--target develop` only selects the commit when the
   release *creates* the tag; if `vX.Y.Z` already exists (pre-created or pushed),
   GitHub ignores `--target` and publishes from that tag's existing commit. Make
   sure `vX.Y.Z` does not yet exist so the release creates it on the merged
   `develop` commit. Publishing the Release is what triggers the workflow —
   pushing a bare tag does not.

The release tag's commit must contain `requirements-lock.txt` and the version
files (`pyproject.toml`, `src/wg21_wiki_mcp/__init__.py`) — the job checks out the
tag and builds, verifies, and generates the SBOM from it. `publish.yml` itself is
loaded from the default branch (`develop`), so it need not exist at the tag
commit. Cutting from current `develop` satisfies both.

## 4. Verify the publish workflow

When the Release is published, the **Publish** workflow should:

1. Run the CI suite as a gate (`needs: test`)
2. Build sdist + wheel
3. Generate CycloneDX SBOM and Sigstore bundles
4. Upload to PyPI via Trusted Publisher
5. Attach artifacts to the GitHub Release

Confirm:

```bash
pip index versions wg21-wiki-mcp
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Workflow did not run | Triggers on **published Releases** (not tag pushes) and loads from default branch `develop`; publish a Release, don't just push a tag |
| Trusted publisher rejected | Owner/repo/workflow/environment must match PyPI settings exactly |
| Version mismatch error | Tag `vX.Y.Z` must equal `pyproject.toml` and `__init__.py` |
| Environment missing | Create GitHub environment `pypi` |
