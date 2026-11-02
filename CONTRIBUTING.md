# Contributing to agentchatme-hermes

Thanks for considering a contribution. The plugin is small and the
maintainers are responsive — most PRs land within a few days.

## Quick start

```bash
git clone https://github.com/agentchatme/agentchat-hermes
cd agentchat-hermes
python -m pip install -e '.[dev]'
python -m pytest -q       # unit suite, runs in <1s
python -m ruff check .    # lint
python -m pyright agentchatme_hermes
```

To run the live smoke suite against `https://api.agentchat.me`, set
`AGENTCHATME_LIVE_API_KEY` (or `AGENTCHAT_LIVE_API_KEY`, same as the SDK's
fixture) to a real `ac_live_…` token and run `pytest -m live`.

## Pull request expectations

* Pass `ruff check`, `pyright agentchatme_hermes`, and `pytest -q` locally
  before pushing. The `CI` workflow re-runs them on every PR; failures
  block the merge.
* Keep changes scoped — one concern per PR. A bug fix and a refactor go
  in two PRs, not one.
* Update `CHANGELOG.md` under "Unreleased" if your change is user-visible
  (new tool, new env var, behavior change, breaking removal). Internal
  refactors that the user can't see don't need an entry.
* No `Co-Authored-By: Claude` (or any AI-tooling) attribution in commit
  messages. The maintainers do not add these and asks contributors to
  match.

## Issue triage

Use the issue templates to file bugs and feature requests — they capture
the context the maintainers need to reproduce. Bug reports without
reproduction steps are usually closed with a request for more detail.

Security issues — see `SECURITY.md` and email **security@agentchat.me**
instead of filing publicly.

## Architecture notes

The adapter is a thin bridge over the official `agentchatme` Python SDK.
When the wire format changes, the SDK's typed errors update first; the
plugin then absorbs them through the `_safe()` decorator's branch tree
in `tools.py`. Don't replicate transport logic here — extend the SDK and
let it flow through.

The Hermes runtime is intentionally a *lazy* import: nothing in the
package may `import gateway.platforms.base` at module scope. Add new
framework integrations inside `_adapter_class()` or inside `register()`
itself. The unit suite verifies the package imports cleanly without
Hermes installed; that's a regression gate.

## Release process

Maintainers only. The version number is the maintainer's decision
(`VERSIONING.md`); set it in `agentchatme_hermes/_version.py` and both
`plugin.yaml` files, merge, then push a tag on main:

```bash
git tag -a v0.3.1 origin/main -m v0.3.1 && git push origin v0.3.1
```

`.github/workflows/publish.yml` refuses the release unless the tag
matches the version and the commit is on main, re-runs the full CI gate,
builds once, and waits in the `pypi` environment for the maintainer's
approval before uploading. Upload uses PyPI trusted publishing; there is
no PyPI token in the repository. "Run workflow" on the Actions tab is a
dry run of the same pipeline and never uploads.

## Code of Conduct

Be kind. Disagree on substance, not on people. Reports to
**conduct@agentchat.me**.

## Parallel branches

Several agents and people work in this checkout at once. Do not stash or
reset someone else's uncommitted files; start a worktree instead:

```bash
git worktree add .worktrees/<branch> -b <branch> origin/main
```

`.worktrees/` is ignored by git. The CI gate is `ruff check .`,
`mypy agentchatme_hermes` and `pytest -q` on Python 3.9–3.13 (Linux) plus a
macOS and a Windows smoke run; run all three locally before pushing.
