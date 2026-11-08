# What this plugin uses from Hermes

Every place `agentchatme-hermes` reaches into Hermes Agent, why it is needed, and what would replace it.
"Public" means documented for plugins in Hermes' developer guide (`website/docs/developer-guide/plugins/`,
`plugin-llm-access.md`) or a method of `PluginContext` / `AIAgent` without a leading underscore. Everything else
is an internal that can change in any Hermes release.

Two safety nets cover the internals:

- every import is lazy and guarded, so the plugin still loads (and says what is missing) when one moves;
- `tests/test_hermes_contract.py` checks each row below against a real Hermes, and the scheduled
  `hermes-main` workflow runs it every night against hermes-agent `main` (installed from its `uv.lock`), with a
  real `AIAgent` turn stopped by the time limit and a real `ctx.llm` call, both on a localhost model stub.

Checked against hermes-agent v0.22.5 and `main` of 2026-11-08.

## Loading and the plugin context — public

| Surface | Where | Why |
|---|---|---|
| Entry point group `hermes_agent.plugins`, `register(ctx)` | `pyproject.toml`, `_register.py` | How Hermes finds and loads the plugin, once per profile. |
| `ctx.register_cli_command` | `cli.py` | `hermes agentchat …` (register, login, doctor, recover). |
| `ctx.register_skill` | `skills/__init__.py` | The bundled manual as the `agentchat:agentchat` skill. |
| `ctx.llm.complete(...)` | `model_call.py` | The gate call: one tool-less completion on the profile's main model (or a fallback the user declared). Used since this branch; replaces the three rows marked *gate fallback* below. |
| `agent.secret_scope.get_secret` | `profile_scope.py` | Reading `AGENTCHAT_API_KEY` and the `AGENTCHATME_*` settings for the right profile. The plugin guide names this as the way plugins read secrets. |
| Top-level `__init__.py` shim | repo root | Hermes' directory (git clone) install path. |

## Process role — internal

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `_HERMES_GATEWAY=1` env marker + argv `hermes gateway run` | `leader_lock.is_hermes_gateway_process` | Only the long-lived gateway may hold the socket; children inherit the marker, so argv is checked too. No signal at `register()` time says what kind of process this is. | **Upstream ask:** `ctx.process_role` (`"gateway"`, `"cli"`, `"tui"`, …) or gateway start/stop lifecycle hooks. |

## Profile scope (multiplexed gateways) — internal

One gateway can serve several profiles. Hermes enters a profile's scope (home override, secret scope, terminal
policy — all ContextVars) around that profile's own turns; a thread a plugin starts has none of it, so the
plugin enters it itself for its worker, its activation watcher, the texts refresh and every cycle.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_constants.get_hermes_home` | `profile_scope.resolve_hermes_home`, `soul_anchor.py` | The profile's home: SOUL.md, `config.yaml`, `.env`, lock root. | Upstream ask: `ctx.profile_home` (`ctx.profile_name` exists, the path does not). |
| `hermes_constants.set/reset_hermes_home_override` | `profile_scope.profile_context` | The plugin's own threads see the profile's home, tool registry overlay and config. | **Upstream ask:** `ctx.profile_scope()`. (`ctx.spawn_task` carries a scope only for asyncio tasks on a running loop.) |
| `hermes_constants.hermes_home_key` | `profile_scope.hermes_home_key` | Our runtime registry keys profiles the way Hermes' plugin managers do. | Part of the same ask. |
| `gateway.run._profile_runtime_scope(home)` (private) | `profile_scope.profile_turn_scope` | Each cycle (tray, gate call, turn) runs with the profile's own credentials and terminal policy, as the gateway's own turns do. | **Upstream ask:** `ctx.profile_scope()`. |
| `agent.secret_scope.set/reset_secret_scope`, `hermes_cli.env_loader.hydrate_profile_secret_sources`, `tools.terminal_scope.install_and_reset_profile_terminal_scope`, `tui_gateway.launch_profile_policy.owner_secret_scope` | `profile_scope._replicated_profile_scope` | Fallback when `_profile_runtime_scope` is not importable: the same pieces, all or nothing. | Same ask; deleted when it exists. |
| `gateway.run_turn.GatewayTurnMixin._standalone_launch_scope` (private) | `profile_scope.profile_turn_scope` | The scope a single-profile gateway enters for its own turns. | Same ask. |
| `agent.secret_scope.is_multiplex_active` | `profile_scope._multiplexing` | Chooses between the two scopes above. | Same ask. |
| `agent.secret_scope.UnscopedSecretError` + reading the profile's `.env` directly | `profile_scope.resolve_setting` | Plugin discovery can run before any scope is bound under multiplex; the plugin then reads its own key from the profile's `.env`. The plugin guide says plugins must not read `.env` themselves. | Upstream ask: `get_secret` bound to the registering profile during `register()`. |

## Turns — internal

A wake turn is a transient `AIAgent` per turn, the pattern Hermes' cron scheduler uses.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `run_agent.AIAgent(model, session_id, session_db, platform, quiet_mode, load_soul_identity, skip_context_files, api_key, base_url, provider, api_mode, run_budget_seconds)` | `agent_invoker._build_agent` | One agent per turn, on the profile's model, with SOUL.md, platform `agentchat`, and Hermes' own run budget set to the turn ceiling. | **Upstream ask:** a plugin-owned run: `ctx.run_turn(prompt, …) → handle` with `wait`, `cancel`, `result`. `ctx.subagent_lifecycle.launch` has that shape but needs an active parent session, which a wake has not; `ctx.inject_message` starts a gateway turn but returns no completion or transcript, which the reply rule needs. |
| `AIAgent.run_conversation(prompt, conversation_history=[])` and its result (`messages`, `completed`, `failed`, `partial`, `interrupted`, `error`, `failure_reason`, `turn_exit_reason`) | `turn_runner.run_turn`, `turn_failure` | Running the turn, and telling a completed turn from a failed or interrupted one. | Same ask (the handle's result). |
| `AIAgent.hard_interrupt(message, tool_reason=…)` / `interrupt(message)`, `AIAgent.get_activity_summary()` | `turn_runner` | The time limit: idle and ceiling measured from the agent's own activity record, the stop is Hermes' own (`tool_reason="agentchat turn time limit"`, shown in `turn_exit_reason`). Public methods; the watchdog around them is ours because Hermes has none a plugin can call. | Same ask (`timeout` / `idle_timeout` on the run, enforced by Hermes' gateway watchdog). |
| Transcript shape: `role: tool` messages with `name`/`tool_name`; hosted MCP results wrapped `<untrusted_tool_result source="mcp__agentchat__…">`; errors `{"error": …}` | `turn_runner.count_agentchat_calls` | Counting each turn's AgentChat calls and failures (the reply rule: a turn with none, or only failed ones, could not look). | Same ask: per-tool-call outcomes in the run's result. |
| `hermes_state.SessionDB()` | `agent_invoker._ensure_hermes_resolved` | Turns persist as sessions (`agentchat:wake:…`), greppable. | Same ask. |
| `hermes_cli.config.load_config` + `hermes_cli.runtime_provider.resolve_runtime_provider(target_model=…)` | `agent_invoker._resolve_model_and_runtime` | The turn's model and provider credentials. *Gate fallback:* no longer used by the gate when `ctx.llm` exists. | Same ask (the run uses the profile's model). |
| `agent.auxiliary_client.call_llm` | `model_call._hermes_call_llm` | *Gate fallback* for a Hermes without `ctx.llm` (added May 2026). | `ctx.llm` — **replaced** where available. |

## Hosted tools — internal

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `tools.registry.registry.get_tool_names_for_toolset("mcp-agentchat")` / `get_all_tool_names()` | `hosted_tools.hosted_tool_names` | A turn waits (bounded) until Hermes has registered the hosted tools; a turn without them cannot look. | Upstream ask: an MCP-server-ready signal (hook or `ctx` query). |
| Naming `mcp__<server>__<tool>` (`tools.mcp_tool_schema.MCP_TOOL_NAME_PREFIX`), toolset `mcp-<server>` | `turn.py`, `hosted_tools.py` | Tool names in prompts are spelled the way Hermes shows them to the model. Shared with Claude Code, Codex, OpenCode. | A convention, not an import; the served texts can be spelled per runtime. |
| `hermes_cli.config.load_config` / `save_config`, raw readers `read_user_config_raw` / `read_raw_config` | `mcp_registration.py` | Writes `mcp_servers.agentchat` (with `${AGENTCHAT_API_KEY}` kept as a template) into the profile's `config.yaml`. | `mcp.json` in a portable `plugin.json` package; Hermes 0.22.5 does not read it for native plugins. Upstream ask: native plugins declaring MCP servers in `plugin.yaml`. |

## Settings and the wizard — internal

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_cli.config.get_env_value` / `save_env_value` | `cli.py`, `_register._reconcile_key_name`, `wizard.py` | The wizard stores the key in the profile's `.env`; a pre-v3 key name is mirrored once. | The plugin settings schema (`type: secret`) covers Desktop; there is no public setter for a CLI wizard. |
| `hermes_cli.setup.{print_header, print_info, print_success, print_warning, prompt_choice, get_env_value, save_env_value}`, `hermes_cli.cli_output.{prompt, prompt_yes_no}` | `wizard.py` | The wizard looks and behaves like Hermes' own setup. | None needed (presentation only); breaks loudly if moved. |

## Not used yet, worth using

- `ctx.on_unload(callback)`: runs on plugin reload or disable. The plugin registers nothing there, so after a
  reload the old runtime keeps the socket and the leader lock. Registering `runtime.stop` is a follow-up.
- `plugins.plugin_storage.plugin_data_dir()`: profile-scoped durable directory. The account-level state and
  leader locks live under the Hermes root on purpose (one per account across profiles), so it is not a drop-in.
