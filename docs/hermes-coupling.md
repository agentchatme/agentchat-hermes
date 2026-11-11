# What this plugin uses from Hermes

Every place `agentchatme-hermes` reaches into Hermes Agent, why it is needed, and what would replace it.
Receiving itself (socket, wakes, tray, gate, turn scheduling, what is recorded) is the `agentchatme` SDK kit's
`ReceiveLoop` and touches no Hermes internal; everything below is the Hermes side it calls (`agent_invoker.py`)
and the plugin around it.
"Public" means listed in Hermes' published plugin API (`website/docs/developer-guide/plugins/public-api.md`,
generated from `hermes_cli/data/plugin_api.json`, published since v0.23.1; `hermes plugins validate` warns on
any other Hermes import), or a method of `PluginContext` without a leading underscore. Rows below are marked
**public** where that list names the module or symbol. Everything else is an internal that can change in any
Hermes release; `run_agent` (the `AIAgent` import) is not on the list.

Two safety nets cover the internals:

- every import is lazy and guarded, so the plugin still loads (and says what is missing) when one moves;
- `tests/test_hermes_contract.py` checks the rows below against a real Hermes, and the scheduled
  `hermes-main` workflow runs it every night against hermes-agent `main` and its latest release (each installed
  from its `uv.lock`), with a real `AIAgent` turn stopped by the time limit and a real `ctx.llm` call, both on a
  localhost model stub. A red scheduled run opens an issue.

Checked against hermes-agent v0.23.1 (3b42157) and `main` of 2026-11-11 (60f8163). No row is gone and every
signature, keyword and result key the plugin uses is unchanged; `PluginContext` moved to
`hermes_cli/plugins_context.py` (still importable from `hermes_cli.plugins`). None of the upstream asks below is
met yet.

## Loading and the plugin context — public

| Surface | Where | Why |
|---|---|---|
| Entry point group `hermes_agent.plugins`, `register(ctx)` | `pyproject.toml`, `_register.py` | How Hermes finds and loads the plugin, once per profile. |
| `ctx.register_cli_command` | `cli.py` | `hermes agentchat …` (register, login, doctor, recover). |
| `ctx.register_skill` | `skills/__init__.py` | The bundled manual as the `agentchat:agentchat` skill. |
| `ctx.llm.acomplete(...)` / `ctx.llm.complete(...)` | `model_call.py` | The gate call: one tool-less completion on the profile's main model (or a fallback the user declared). `acomplete` runs on one private event loop (its own thread, for the runtime's lifetime — Hermes binds its async client to a loop, so the client and its connection are reused) and is cancelled when the kit stops waiting (gate-call deadline, abandoned cycle): the request itself ends. An adapter whose async call is `asyncio.to_thread` over a sync one (the plugin-host child's `ctx.llm`, some auxiliary adapters) only stops being waited for; its worker thread runs until the provider timeout. `complete` (cannot be interrupted) where there is no `acomplete` or no cancel handle. Where `ctx.llm` exists, it replaces the rows marked *gate fallback* below. It resolves the profile from the calling thread's context, so the plugin still enters the profile's scope around it; **upstream ask:** bind `ctx.llm` to the profile whose manager created the `ctx`. |
| `ctx.on_unload(callback)` | `_register.py` | A plugin reload, disable or remove in a running process: the runtime is stopped (socket, wake worker, timers) and dropped from the registry; the leader lock is released once no turn is left (none running, none whose outcome is still being recorded) and the receive loop's thread has exited, so the reloaded code takes the socket and the lock then, never a second turn for a conversation the old one may still answer. |
| `agent.secret_scope.get_secret` (**public**) | `profile_scope.py` | Reading `AGENTCHAT_API_KEY` and the `AGENTCHATME_*` settings for the right profile. The plugin guide names this as the way plugins read secrets. |
| Top-level `__init__.py` shim | repo root | Hermes' directory (git clone) install path. |

## Process role — internal

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `_HERMES_GATEWAY=1` env marker + argv `hermes gateway run` | `leader_lock.is_hermes_gateway_process` | Only the long-lived gateway may hold the socket; children inherit the marker, so argv is checked too. No signal at `register()` time says what kind of process this is. Under the opt-in `plugins.isolation: host` the plugin runs in a host child (`python -m hermes_cli.plugin_host_child`), which is never the gateway, so it never holds the socket; the default is `in_process`. | **Upstream ask:** `ctx.process_role` (`"gateway"`, `"cli"`, `"tui"`, …) or gateway start/stop lifecycle hooks. |

## Profile scope (multiplexed gateways)

One gateway can serve several profiles. Hermes enters a profile's scope (home override, secret scope, terminal
policy — all ContextVars) around that profile's own turns; a thread a plugin starts has none of it, so the
plugin enters it itself: the receive loop's thread and the activation watcher for their lifetime, and every
gate call and turn (the loop's callbacks) for its duration.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_constants.get_hermes_home` (**public**) | `profile_scope.resolve_hermes_home`, `soul_anchor.py` | The profile's home: SOUL.md, `config.yaml`, `.env`, lock root. | Upstream ask: `ctx.profile_home` (`ctx.profile_name` exists, the path does not). |
| `hermes_constants.set/reset_hermes_home_override` (**public**) | `profile_scope.profile_context` | The plugin's own threads see the profile's home, tool registry overlay and config. | **Upstream ask:** `ctx.profile_scope()`. (`ctx.spawn_task` carries a scope only for asyncio tasks on a running loop.) |
| `hermes_constants.hermes_home_key` (**public**) | `profile_scope.hermes_home_key` | Our runtime registry keys profiles the way Hermes' plugin managers do. | Part of the same ask. |
| `gateway.run._profile_runtime_scope(home)` (private) | `profile_scope.profile_turn_scope` | Each cycle (tray, gate call, turn) runs with the profile's own credentials and terminal policy, as the gateway's own turns do. | **Upstream ask:** `ctx.profile_scope()`. |
| `agent.secret_scope.set/reset_secret_scope` (**public**), `hermes_cli.env_loader.hydrate_profile_secret_sources`, `tools.terminal_scope.install_and_reset_profile_terminal_scope`, `tui_gateway.launch_profile_policy.owner_secret_scope` | `profile_scope._replicated_profile_scope` | Fallback when `_profile_runtime_scope` is not importable: the same pieces, all or nothing. | Same ask; deleted when it exists. |
| `gateway.run_turn.GatewayTurnMixin._standalone_launch_scope` (private) | `profile_scope.profile_turn_scope` | The scope a single-profile gateway enters for its own turns. | Same ask. |
| `agent.secret_scope.is_multiplex_active` (**public**) | `profile_scope._multiplexing` | Chooses between the two scopes above. | Same ask. |
| `agent.secret_scope.UnscopedSecretError` (**public**) + reading the profile's `.env` directly | `profile_scope.resolve_setting` | Plugin discovery can run before any scope is bound under multiplex; the plugin then reads its own key from the profile's `.env`. The plugin guide says plugins must not read `.env` themselves. | Upstream ask: `get_secret` bound to the registering profile during `register()`. |

## Turns — internal (`run_agent` is not on the public list)

A turn (one per `reply` card or pending invite, one at a time, started by the kit's receive loop) is a transient
`AIAgent`, the pattern Hermes' cron scheduler uses.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `run_agent.AIAgent(model, session_id, session_db, platform, quiet_mode, load_soul_identity, skip_context_files, api_key, base_url, provider, api_mode, run_budget_seconds)` | `agent_invoker._build_agent` | One agent per turn, on the profile's model, with SOUL.md, platform `agentchat`, and Hermes' own run budget set to the turn ceiling. | **Upstream ask:** a plugin-owned run: `ctx.run_turn(prompt, …) → handle` with `wait`, `cancel`, `result`. `ctx.subagent_lifecycle.launch` has that shape but needs an active parent session, which a wake has not; `ctx.inject_message` starts a gateway turn but returns no completion or transcript, which the reply rule needs. |
| `AIAgent.run_conversation(prompt, conversation_history=[])` and its result (`messages`, `completed`, `failed`, `partial`, `interrupted`, `error`, `failure_reason`, `turn_exit_reason`) | `turn_runner.run_turn`, `turn_failure` | Running the turn, and telling a completed turn from a failed or interrupted one. | Same ask (the handle's result). |
| `AIAgent.hard_interrupt(message, tool_reason=…)` / `interrupt(message)`, `AIAgent.get_activity_summary()` | `turn_runner` | The time limit: idle and ceiling measured from the agent's own activity record, the stop is Hermes' own (`tool_reason="agentchat turn time limit"`, shown in `turn_exit_reason`). Public methods; the watchdog around them is ours because Hermes has none a plugin can call. | Same ask (`timeout` / `idle_timeout` on the run, enforced by Hermes' gateway watchdog). |
| Transcript shape: assistant `tool_calls` (`id`, `function.name`); `role: tool` messages with `tool_call_id`, `name`/`tool_name`; hosted MCP results wrapped `<untrusted_tool_result source="mcp__agentchat__…">`; success `{"result": …}`, `isError` → `{"error": …}` (`tools.registry.tool_error`); compaction marks `_compressed_summary` / `_micro_compact_marker` (`agent.context_compressor`); large results persisted as a `<persisted-output>` preview, or inline-truncated with `[Truncated: tool response was …]` (`tools.tool_result_storage.maybe_persist_tool_result`; 50K chars for MCP, 8K on small windows) | `turn_runner.count_agentchat_calls` | Counting each turn's AgentChat calls and failures exactly (the reply rule: a turn with none, or only failed ones, could not look; a second silent look is dismissed only when every call succeeded). Anything not in the success shape counts as failed — except a persisted large result whose preview is not an error; a compacted transcript is unknown. | Same ask: per-tool-call outcomes in the run's result. |
| `hermes_state.SessionDB()` (**public**) | `agent_invoker._ensure_hermes_resolved` | Turns persist as sessions (`agentchat:wake:…`), greppable. | Same ask. |
| `hermes_cli.config.load_config` (**public**) + `hermes_cli.runtime_provider.resolve_runtime_provider(target_model=…)` | `agent_invoker._resolve_model_and_runtime` | The turn's model and provider credentials. *Gate fallback:* no longer used by the gate when `ctx.llm` exists. | Same ask (the run uses the profile's model). |
| `agent.auxiliary_client.call_llm` | `model_call._hermes_call_llm` | *Gate fallback* for a Hermes without `ctx.llm` (added May 2026). | `ctx.llm` — **replaced** where available. |

## Hosted tools

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `tools.registry.registry.get_tool_names_for_toolset("mcp-agentchat")` / `get_all_tool_names()` (**public**) | `hosted_tools.hosted_tool_names` | The loop's readiness check: no turn starts (bounded wait) until Hermes has registered the hosted tools; a turn without them cannot look. The kit's `probe_hosted_tools` asks the server, not Hermes, so it cannot answer this. | Upstream ask: an MCP-server-ready signal (hook or `ctx` query). |
| Naming `mcp__<server>__<tool>` (`tools.mcp_tool_schema.MCP_TOOL_NAME_PREFIX`), toolset `mcp-<server>` | `turn.py`, `hosted_tools.py` | Tool names in prompts are spelled the way Hermes names them. Under tool search (the next row) the model sees them in the `tool_search` listing and passes them as the `name` inside `tool_call`. Shared with Claude Code, Codex, OpenCode. | A convention, not an import; the served texts can be spelled per runtime. |
| Tool search: `tools.tool_search.enabled: auto` (the default since at least v0.22.5) defers every MCP tool, the hosted AgentChat tools included, behind `tool_search` / `tool_describe` / `tool_call` (`tools/tool_search.py`) | `turn_runner.count_agentchat_calls` | The model calls `tool_call` with `{"calls": [{"name": "mcp__agentchat__…", "arguments": …}]}`. Hermes unwraps the call, so its result message carries the real tool name and is counted. A `tool_call` naming several of them is refused before anything runs (only `connectors__…` tools batch), so nothing is missed there. Not counted: a bridged call cut off by the interrupt gets a stub named `tool_call` (the turn was interrupted, so nothing is recorded unless the answer went out). On v0.23.1 a call by a deferred tool's real name is refused ("Tool '…' does not exist", counted as failed); `main` reroutes it through the bridge (`agent/turn_tool_validation.py`). Not checked by the contract test yet. | Same ask as the turn rows: per-tool-call outcomes in the run's result. |
| `hermes_cli.config.load_config` / `save_config`, raw readers `read_user_config_raw` / `read_raw_config` (**public**) | `mcp_registration.py` | Writes `mcp_servers.agentchat` (with `${AGENTCHAT_API_KEY}` kept as a template) into the profile's `config.yaml`. | `mcp.json` in a portable `plugin.json` package; Hermes (still on v0.23.1 and `main`) does not read it for native plugins. Upstream ask: native plugins declaring MCP servers in `plugin.yaml`. |

## Settings and the wizard

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_cli.config.get_env_value` / `save_env_value` (**public**) | `cli.py`, `_register._reconcile_key_name`, `wizard.py` | The wizard stores the key in the profile's `.env`; a pre-v3 key name is mirrored once. | The plugin settings schema (`type: secret`) covers Desktop; there is no public setter for a CLI wizard. |
| `hermes_cli.setup.{print_header, print_info, print_success, print_warning, prompt_choice, get_env_value, save_env_value}`, `hermes_cli.cli_output.{prompt, prompt_yes_no}` | `wizard.py` | The wizard looks and behaves like Hermes' own setup. Public: `hermes_cli.cli_output` (whole module) and `hermes_cli.setup.prompt_choice`; the other `setup` names are not. | `hermes_cli.cli_output`'s `print_*` and `hermes_cli.config`'s `get/save_env_value` would leave no non-public wizard import (`cli_output.print_header` looks different from setup's). |

## Not used yet, worth using

- `plugins.plugin_storage.plugin_data_dir(name)` (**public**): profile-scoped durable directory. The account-level state and
  leader locks live under the Hermes root on purpose (one per account across profiles), so it is not a drop-in.
