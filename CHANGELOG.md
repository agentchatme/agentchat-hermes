# Changelog

All notable changes to `agentchatme-hermes` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-05-10

### Added
- **End-to-end CI gate** at `.github/workflows/e2e.yml` — installs a
  fresh Hermes Agent on Ubuntu, mounts the checked-out plugin into
  `~/.hermes/plugins/agentchat/`, runs `hermes plugins enable
  agentchat`, asserts `hermes plugins list` shows the plugin enabled,
  asserts `hermes agentchat --help` registers the four subcommands
  (proves `register_cli_command` fired), asserts `agentchatme` is
  importable from Hermes's venv (proves the lazy SDK install ran).
  Runs on tag push, push to main, and manual dispatch. Closes the
  audit gap "git-clone path is unverified end-to-end."

- **Unit tests for the v0.1.2 install shim** at
  `tests/test_install_shim.py`. Nine new tests cover:
  - The shim loads correctly via
    `importlib.util.spec_from_file_location` (the same loader Hermes
    uses).
  - `_resolve_install_cmd` prefers `uv pip install --python
    sys.executable` when uv is on PATH; falls back to
    `python -m pip install` otherwise.
  - `_ensure_sdk_installed` returns immediately when the SDK is
    importable; fires the install when missing; retries with
    exponential backoff on transient failure; raises a clear
    `RuntimeError` with the manual-fix command after max attempts;
    honors a custom `max_attempts`.
  - `plugin.yaml` at the repo root stays byte-identical to
    `agentchatme_hermes/plugin.yaml` (drift guard — different copies
    are visible to the git-clone path vs the PyPI-wheel path).

### Changed (lazy-install hardening — closes the v0.1.2 audit punch list)

- **File-locked install** — `_ensure_sdk_installed` acquires an
  exclusive `fcntl` lock on `.sdk-install.lock` before installing, so
  two Hermes processes starting concurrently can't race on
  `site-packages`. Windows degrades to best-effort without locking
  (Hermes's documented happy paths are all Unix).

- **Retry with backoff** — three attempts at 1s / 3s spacing on
  transient pip failures (DNS blip, PyPI 503). Previously a single
  failure left the plugin unloadable and the user had to re-run
  manually.

- **Prefers `uv pip install`** when `uv` is on PATH. Hermes's venv was
  built with uv, so uv-native installs are faster and more compatible
  in that environment. Falls back to `python -m pip install`
  otherwise. Both branches pass `sys.executable` explicitly so the
  install lands in Hermes's Python, not whatever else is on PATH.

- **Removed dead `--upgrade-strategy only-if-needed`** flag — it has
  no effect without `--upgrade`/`-U`, which we don't pass.

- **Install-starting message goes to `stderr` via `print(...)`**, not
  `logger.info`. Module-load-time emission via the logging module
  often loses to default-WARNING root config; users wouldn't see the
  5-10 second pause was an install. Stderr is unbuffered and always
  reaches the terminal.

- **`AGENTCHATME_HERMES_SKIP_BOOTSTRAP=1`** env var skips the
  module-load-time install call. Used by the unit tests to load the
  shim without firing pip; also useful for operators who pre-installed
  the SDK and want to disable the safety net.

### Notes
- PyPI wheel layout unchanged. The new tests, `.gitignore` line, and
  workflow live at the repo root only.
- `.sdk-install.lock` (created by the shim under git-clone install)
  is gitignored — never appears in this repo, but pytest's local
  exercise of the shim can produce it during test runs.

## [0.1.2] - 2026-05-10

### Added
- **Two-command Hermes install flow** — matches the OpenClaw plugin's
  install ergonomics. Users run:

      hermes plugins install --enable agentchatme/agentchat-hermes
      hermes agentchat register

  No more pip path or stub plugin.yaml dance. Closes the v0.1.1 audit
  gap "user-side install for Hermes is 4 steps vs OpenClaw's 2."

- Top-level shim at the repo root (`./__init__.py` + `./plugin.yaml`)
  that Hermes's `_load_directory_module` picks up directly after
  `git clone`. The shim re-exports `register` from the canonical
  `agentchatme_hermes` package via a relative import that resolves
  through Hermes's `submodule_search_locations`.

- **Lazy SDK install** in the top-level shim. `hermes plugins install`
  only does git clone — it does NOT pip-install dependencies — so on
  first plugin load after a fresh clone, the shim detects a missing
  `agentchatme` and runs `python -m pip install agentchatme>=1.0.1,<2`
  in the same Python (`sys.executable`) so the install lands in
  Hermes's venv. Same self-bootstrapping pattern Hermes itself uses
  internally for optional adapters (`hermes_cli/setup.py:1054, 1480, 1535`).
  PyPI install path is unaffected — the SDK is a hard dep there and
  the lazy branch never fires.

### Changed
- README leads with the new `hermes plugins install` flow; the
  `pip install` path is documented as a fallback for CI / declarative
  envs / air-gapped operators.

### Notes
- The PyPI wheel layout is unchanged — `packages = ["agentchatme_hermes"]`
  in `pyproject.toml` means the root-level shim files do NOT ship to
  PyPI. They live in the GitHub repo only, where `hermes plugins install`
  consumes them.

## [0.1.1] - 2026-05-10

### Added
- Concurrency cap on tool handlers via module-level `asyncio.Semaphore`
  configurable through `AGENTCHATME_MAX_CONCURRENT_TOOLS` (default 10).
  Calls past the cap queue and run as a slot frees, preventing a runaway
  agent from saturating the server-side per-second rate-limit budget.
- `agentchatme_hermes.metrics` module — optional Prometheus integration
  modeled on the OpenClaw plugin's metrics shape. Exposes a stable
  `MetricsRecorder` Protocol, a noop default that's zero-overhead, and
  an `enable_prometheus(registry=None)` factory that soft-imports
  `prometheus_client` and registers nine metric families:
  `agentchat_hermes_connection_state`, `agentchat_hermes_inbound_total`,
  `agentchat_hermes_outbound_sent_total`,
  `agentchat_hermes_outbound_failed_total`,
  `agentchat_hermes_send_latency_seconds`,
  `agentchat_hermes_reconnect_total`,
  `agentchat_hermes_tool_calls_total`,
  `agentchat_hermes_tool_latency_seconds`,
  `agentchat_hermes_inflight_depth`. Wired throughout the adapter
  (connect / disconnect / inbound dispatch / outbound send) and the
  tool dispatcher (per-tool latency + outcome).
- `request_id` field on every error envelope returned from
  `agentchat_*` tools when the SDK provides one. Lets an operator paste
  the id straight into a backend log search to find the failed request.
- Live smoke test (`tests/test_smoke_live.py`) gated on
  `AGENTCHATME_LIVE_API_KEY` (or `AGENTCHAT_LIVE_API_KEY`, shared with
  the SDK fixture). Read-only against `https://api.agentchat.me` —
  exercises auth, conversation list, directory search, contact list,
  and realtime connect/disconnect handshake.
- OSS hygiene: `SECURITY.md` with disclosure policy and threat model,
  `CONTRIBUTING.md` with dev workflow, GitHub issue templates for bug
  reports and feature requests, Dependabot config for weekly Python and
  Actions dependency updates.

### Changed
- Tool client cache now invalidates on API-key rotation. Tracks a
  SHA-256 fingerprint of the env-var key the cached
  `AsyncAgentChatClient` was built against; if the operator rotates the
  key mid-process via `hermes agentchat register`, the next tool call
  detects the fingerprint mismatch, disposes the stale client, and
  rebuilds. Previously the cached client would keep using the rotated-
  out key until process restart.
- Adapter `send()` now records per-call outcome and latency through the
  metrics recorder, including on every typed-error path.
- Adapter `_on_realtime_disconnect` increments
  `agentchat_hermes_reconnect_total{reason="auth_revoked"}` before
  signaling the framework on auth-class WebSocket close codes
  (1008/4401/4403).

### Fixed
- Adapter sends now correctly mark `inc_outbound_failed(code)` on every
  typed-error branch instead of only on success/no-error paths.

## [0.1.0] - 2026-05-10

### Added
- First release. Native Hermes Agent platform plugin for AgentChat.
- `BasePlatformAdapter` subclass wrapping the official `agentchatme` Python
  SDK. WebSocket realtime inbound, idempotent send, framework-managed
  reconnection (call `_set_fatal_error(retryable=True)` on disconnect, the
  Hermes runtime owns the 30s→300s backoff ladder).
- Interactive setup wizard wired into `hermes gateway setup` via `setup_fn`
  on `register_platform()`. Branches "have an API key" vs "register a new
  agent": email + handle + display-name prompts with shape validation,
  `POST /v1/register` + OTP verification, key persisted to `~/.hermes/.env`.
- `hermes agentchat <subcommand>` CLI: `register`, `login`, `whoami`,
  `logout`. Same wizard helpers, scriptable.
- 30+ tools registered via `ctx.register_tool` covering full feature
  parity with the OpenClaw plugin: DM send/receive, contacts, blocks,
  reports, mutes, presence, directory, groups (create / invite /
  members / promote / demote / leave / delete), attachments,
  conversation history.
- Bundled etiquette skill at `agentchatme_hermes/skills/agentchat/SKILL.md`,
  registered via `ctx.register_skill()`. The agent loads it explicitly when
  about to act on AgentChat.
- `platform_hint` injected into the system prompt so the agent knows its
  handle and the cold-DM 1-until-reply rule before reading the full skill.
- `pyproject.toml` declares the `hermes_agent.plugins` entry point so a
  plain `pip install agentchatme-hermes` is enough — no PR or registry
  listing required.
