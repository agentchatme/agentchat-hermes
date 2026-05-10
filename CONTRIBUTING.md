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

Maintainers only. See `.github/workflows/publish.yml` — push a `v*` tag,
the workflow gates on tests, builds, validates, and uploads to PyPI via
the token in repo secrets. Patch increments only (0.1.0 → 0.1.1 → 0.1.2)
until enough real-fleet signal motivates a minor bump.

## Code of Conduct

Be kind. Disagree on substance, not on people. Reports to
**conduct@agentchat.me**.
