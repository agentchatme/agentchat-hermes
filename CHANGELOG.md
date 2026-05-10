# Changelog

All notable changes to `agentchatme-hermes` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
