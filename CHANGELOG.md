# Changelog

All notable changes to `agentchatme-hermes` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
