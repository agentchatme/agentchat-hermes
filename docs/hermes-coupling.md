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
met yet. The profile-scope, key and hosted-tools rows were re-checked against v0.23.4 and `main` of 2026-11-26
(c8f866c48f), where `agent/secret_scope.py`, `hermes_scope.py`, `agent/runtime_scope.py` and
`tui_gateway/launch_profile_policy.py` are the same; since 0.7 the plugin reads no `.env` file and imports none of
`gateway.run`, `gateway.run_turn`, `hermes_cli.env_loader`, `tools.terminal_scope`, `tui_gateway.*` or
`tools.mcp_tool_loop`.

## Loading and the plugin context — public

| Surface | Where | Why |
|---|---|---|
| Entry point group `hermes_agent.plugins`, `register(ctx)` | `pyproject.toml`, `_register.py` | How Hermes finds and loads the plugin, once per profile. |
| `ctx.register_cli_command` | `cli.py` | `hermes agentchat …` (register, login, doctor, recover). Skipped under the opt-in `plugins.isolation: host` (`HOST_SKIPPED_CTX_METHODS`), so the subcommands do not exist there. |
| `ctx.register_skill` | `skills/__init__.py` | The manual as the `agentchat:agentchat` skill: by default the package's own `skills/agentchat/SKILL.md` (this release's manual); with the plugin setting `live_texts` on, `<profile home>/plugin-data/agentchat/manual/SKILL.md` (`manual.py`), outside the package, which the plugin keeps at the version the server announces. Hermes checks only that the path exists when it registers (`hermes_cli/plugins_content.py`). The description names no version: registered rows are part of the skills-prompt cache key (`agent/prompt_builder.py` `_plugin_skill_prompt_rows`). |
| `config_schema` in `plugin.yaml`; `ctx.get_config("live_texts", None)` (**public**: `PluginContext`, `website/docs/developer-guide/plugins/index.md` § settings) | `plugin.yaml` (both copies, kept identical), `texts.read_mode`, called in `_register.register` | The one plugin setting, `live_texts` (bool, default off): whether the manual and the SOUL.md identity line are this release's copies or kept current from the server. Hermes reads `config_schema` from a plugin directory's manifest (the Desktop form, `hermes plugins config`, the settings offer at `hermes plugins install`); an entry-point (pip) install has no manifest there, so the README and `doctor` give `hermes config set plugins.entries.agentchat.settings.live_texts true`, which writes a bool. `get_config` reads `config.yaml` of the profile Hermes has bound in the calling context, so it is read once, in `register()`, where the loader binds this profile, and kept for the process (`texts.record_mode`): it applies from the next start or plugin reload. Hermes applies no schema default, so the plugin passes its own. Contract-tested: per-profile reads, the `hermes config set` round trip, the schema as Hermes parses and renders it. | This is the public hook. |
| `<profile home>/plugin-data/<plugin>/` (**public**: `plugins.plugin_storage.plugin_data_dir`) | `manual.py` | With `live_texts` on, where the kept-current manual lives: per profile, kept across `hermes plugins update` / `remove` and package upgrades, never scanned for skills. The plugin computes the folder from the profile's home itself (`plugin_data_dir()` resolves `get_hermes_home()`, which a thread the plugin starts does not have for the right profile); the contract test checks the two agree. |
| `tools.skills_tool.find_all_skills` / `skill_search_dirs` / `locate_skill` (**public**, v0.22.5 and later) | `cli.py` (`doctor`) | Finds an `agentchat` skill in Hermes' own skill folders (a copy of the manual someone installed): reported, never removed. |
| `ctx.llm.acomplete(...)` / `ctx.llm.complete(...)` | `model_call.py` | The gate call: one tool-less completion on the profile's main model (or a fallback the user declared). `acomplete` runs on one private event loop (its own thread, for the runtime's lifetime — Hermes binds its async client to a loop, so the client and its connection are reused) and is cancelled when the kit stops waiting (gate-call deadline, abandoned cycle): the request itself ends. An adapter whose async call is `asyncio.to_thread` over a sync one (the plugin-host child's `ctx.llm`, some auxiliary adapters) only stops being waited for; its worker thread runs until the provider timeout. `complete` (cannot be interrupted) where there is no `acomplete` or no cancel handle. Where `ctx.llm` exists, it replaces the rows marked *gate fallback* below. It resolves the profile from the calling thread's context, so the plugin still enters the profile's scope around it; **upstream ask:** bind `ctx.llm` to the profile whose manager created the `ctx`. |
| `ctx.on_unload(callback)` | `_register.py` | A plugin reload, disable or remove in a running process: the runtime is stopped (socket, wake worker, timers) and dropped from the registry; the leader lock is released once no turn is left (none running, none whose outcome is still being recorded) and the receive loop's thread has exited, so the reloaded code takes the socket and the lock then, never a second turn for a conversation the old one may still answer. |
| `agent.secret_scope.get_secret` (**public**) | `profile_scope.resolve_setting` | Reading `AGENTCHAT_API_KEY` and the `AGENTCHATME_*` settings for the right profile, in the scope the caller runs in. The plugin guide names this as the way plugins read secrets. |
| Top-level `__init__.py` shim | repo root | Hermes' directory (git clone) install path. |

## Process role — internal

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| argv `hermes gateway run` (and the `_HERMES_GATEWAY=1` env marker, logged only) | `leader_lock.is_hermes_gateway_process` | Only the long-lived gateway may hold the socket. argv is the only signal: every child the gateway starts inherits the marker, so a process with the marker alone is a follower (logged at DEBUG). No signal at `register()` time says what kind of process this is. Under the opt-in `plugins.isolation: host`, third-party plugins run only in a host child (`python -m hermes_cli.plugin_host_child`), which is never the gateway: nothing holds the socket and the agent is never woken (the default is `in_process`). | **Upstream ask:** `ctx.process_role` (`"gateway"`, `"cli"`, `"tui"`, …) or gateway start/stop lifecycle hooks. |
| `gateway.status.get_running_pid(<default home>/gateway.pid, cleanup_stale=False)` | `placement.default_gateway_pid` (logout's note), `cli._gateway_running` (unscoped, `doctor`) | Whether the gateway that holds a named profile's socket is the default profile's (a multiplexing gateway): then only `hermes gateway restart` frees it — `hermes -p <name> gateway restart` reconnects Hermes' adapters there, not this plugin's socket. Read-only. | Upstream ask: a served-profile restart that stops plugin runtimes too. |

## Profile scope (multiplexed gateways) — public names only

One gateway can serve several profiles. Hermes binds a profile's scope (home override, secret scope, terminal
policy — one ContextVar state, `hermes_scope.py`) around that profile's own turns; a thread a plugin starts has
none of it. As the Nous review asked (the route documented in open core PR #126738), `register(ctx)` captures the
context Hermes loaded the plugin in (`contextvars.copy_context()`, `profile_scope.ProfileScope.capture`), and every
thread and cycle the plugin runs for the profile runs in a fresh copy of it (`ProfileScope.run` / `.thread`): the
activation watcher and each of its attempts, the receive loop's thread, the gate call, the runtime check, the hold
for the hosted tools, each turn and the thread that records a late turn's outcome, the leader-retry and
lock-release threads, the follower's manual sync. The turn's own thread inherits the turn's scope
(`profile_scope.context_thread`, the shape of `agent.memory_provider.spawn_context_thread`).

On a multiplexed gateway each entry point (an activation attempt, a gate call, the runtime check, the hold, a
turn) also binds the profile's secrets for its call, as the gateway rebinds per turn:
`set_secret_scope({}, profile_home=<home>)` installs an empty mapping stamped with the home (with Hermes' runtime
scope bound for the profile — a served profile loads inside one — it passes through `legacy_switch`), and
`refresh_installed_secret_scope(<home>)` fills it in place with the mapping Hermes builds for that profile's own
turns (`owner_secret_scope`: the launch profile's environment frozen at activation under its `.env` and sources —
keys set only by systemd `Environment=` / `op run` / `docker -e` are kept — and any other profile's own `.env` and
sources only). A single-profile process binds nothing: `get_secret` reads the process environment, which is the
profile's own (Hermes' own standalone turns bind nothing either, `GatewayTurnMixin._standalone_launch_scope`).
A bind that fails runs nothing: the entry point raises `ProfileScopeError` (the activation is retried, a turn is
not started, the runtime check and the hold say "profile scope unavailable"), logged once. The `hermes agentchat`
CLI never multiplexes; a setting read outside any scope on a multiplexed gateway is a typed failure, never a read
of another source.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_constants.get_hermes_home` (**public**) | `profile_scope.resolve_hermes_home`, `soul_anchor.py` | The profile's home: SOUL.md, `config.yaml`, the key file, lock root. | Upstream ask: `ctx.profile_home` (`ctx.profile_name` exists, the path does not). |
| `contextvars.copy_context()` in `register()` (stdlib) | `profile_scope.ProfileScope` | The profile's home override and, for a served profile, Hermes' runtime scope and terminal policy as bound when it loaded the plugin, for every plugin thread. | **Upstream ask:** `ctx.profile_scope()` / `ctx.run_in_profile(fn)`: the profile's full scope (secrets and terminal policy included) for plugin work, so a plugin binds nothing itself. |
| `agent.secret_scope.is_multiplex_active` / `set_secret_scope` / `refresh_installed_secret_scope` / `reset_secret_scope` (**public**) | `ProfileScope._bind_secrets` | The profile's secrets for each entry point's call on a multiplexed gateway. `refresh_installed_secret_scope` builds through `owner_secret_scope` from v0.23.2; before (v0.23.1 and older) it rebuilt from the profile's files only, which drops the launch profile's environment-only keys. That is warned, not refused: one WARNING per process and a `doctor` line from the running gateway's health record (`worker.scope`). Hermes exposes no release number to a plugin (its distribution reports `0.0.0`; `hermes --version` reads the private `hermes_cli.version_info`), so `profile_scope.refresh_keeps_launch_env` asks the public function itself, in an empty context, whether it refills a scope stamped for another (nonexistent) home — its documented contract since 0.23.2 is that it does not. Contract-tested on a real two-profile multiplexed gateway (including a launch profile loaded before the gateway multiplexes), and mutation-checked: no bind fails it, a files-only bind loses the launch profile's environment-only key. | Same ask. |
| `hermes_constants.hermes_home_key` (**public**) | `profile_scope.hermes_home_key`, `config.copy_of` | Our registries key profiles the way Hermes' plugin managers do. | Part of the same ask. |
| `agent.secret_scope.get_secret` / `UnscopedSecretError` (**public**) | `profile_scope.resolve_setting` | Every setting, in the scope the caller runs in. An unscoped read under multiplex is a `ProfileScopeError` (no fallback reader). | — |

Known limits (said to Nous):

- **The launch profile's terminal policy.** A served profile's captured context carries the terminal policy Hermes
  bound when it loaded the plugin (a snapshot: a `terminal:` change applies after a restart). The launch profile
  loads with only its home bound (the boot's discovery thread), so its turns' commands read `TERMINAL_*` from the
  process environment, which the gateway filled from the launch profile's own config at start. There is no public
  way to bind a profile's terminal policy.
- **The loader's in-load flag.** The context captured in `register()` carries `hermes_cli.plugins_loader`'s
  `_IN_PLUGIN_LOAD`, so `in_plugin_load_worker()` is true in our threads and a `discover_plugins()` from them does
  not wait for a sweep still running (`hermes_cli/plugins.py` `discover_and_load`, `_join_background_discovery`):
  the boot sweep, or a forced reload — `discover_plugins(force=True)` from `/reload-plugins`, a plugin enable
  (`hermes_cli/plugins_activation.py`), the gateway's plugin rewire (`gateway/run_plugin_rewire.py:112`), the
  dashboard — which first unloads every plugin (their tools and hooks unregistered) and then loads them again. A
  turn of ours that builds its agent during either can find the registry without the tools of plugins not loaded
  (back) yet. A forced reload unloads this plugin too (`ctx.on_unload` stops its runtime), so no new turn of ours
  starts during one; a turn already running keeps the tool list its agent was built with. Our turns run after
  the socket connects and the gate answers, well after the boot sweep;
  `test_a_plugin_loaded_after_ours_is_visible_from_our_scope` checks that a plugin loaded after ours is visible
  from our scope once the sweep is done. With `plugins.load_timeout_seconds: 0` (inline loads) there is no flag.
- **Source hydration.** The bind uses the external-source snapshot Hermes made for the profile
  (`get_secret_source_values`); the plugin triggers no hydration itself. The gateway hydrates every served
  profile at boot (its MCP discovery) and per turn.
- **A hosted (Hermes Cloud) scope** refuses the bind (`ScopeSwitchRefused`): every entry point fails closed there.
- **`plugins.isolation: host`.** The plugin runs in a host child process started with only that profile's
  environment, which never multiplexes: nothing is bound and `get_secret` reads that environment, the profile's
  own. The key source works (Hermes proxies `fetch`); there is no socket and no `hermes agentchat` command there
  (see *Process role*).

## Turns (`run_agent`, the `AIAgent` import, is not on the public list)

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
| `tools.mcp_tool_discovery.get_mcp_status()` (rows with `name`, `status`, `error`, `tools`, `needs_auth`; private, diagnostics only) | `hosted_tools.hosted_server_state`, the health record's `worker.hosted_tools`, `doctor` | Hermes' own reading of `mcp_servers.agentchat` in this profile (what `hermes mcp list` shows): `failed` covers a server Hermes parked after its first connects failed. The tools_unavailable log line and `doctor` name that cause. Read only: cached state, never connects, changes nothing. | Same ask: an MCP-server-ready signal with its state. |
| A parked server (`failed` in `get_mcp_status`): Hermes' own self-probe after 300 s, doubling to an hour (`tools.mcp_tool._PARKED_RETRY_INTERVAL` / `_PARKED_PROBE_BACKOFF_MAX`); `/reload-mcp` (`hermes_cli.commands`, a gateway chat command) | `hosted_tools.PARKED_REMEDY`, the tools_unavailable log line, `doctor` | Hermes has no public reconnect for a plugin, so the plugin asks for none (it used `tools.mcp_tool_loop.reconnect_mcp_server` until 0.6.5): it holds the turn, and says the cause and the remedy — `/reload-mcp` in a chat this gateway serves, or `hermes gateway restart`. Regression accepted: a gateway started offline gets its hosted tools back at Hermes' next probe (5 minutes, longer while the network stays down, up to an hour), not when the plugin's socket reconnects. Contract-tested: a real park is read, held and named; the probe constants and the command are pinned. | **Upstream ask:** reconnect parked MCP servers when the network returns, or a public `ctx` reconnect. |
| Naming `mcp__<server>__<tool>` (`tools.mcp_tool_schema.MCP_TOOL_NAME_PREFIX`), toolset `mcp-<server>` | `turn.py`, `hosted_tools.py` | Tool names in prompts are spelled the way Hermes names them. Under tool search (the next row) the model sees them in the `tool_search` listing and passes them as the `name` inside `tool_call`. Shared with Claude Code, Codex, OpenCode. | A convention, not an import; the served texts can be spelled per runtime. |
| Tool search: `tools.tool_search.enabled: auto` (the default since at least v0.22.5) defers every MCP tool, the hosted AgentChat tools included, behind `tool_search` / `tool_describe` / `tool_call` (`tools/tool_search.py`) | `turn_runner.count_agentchat_calls` | The model calls `tool_call` with `{"calls": [{"name": "mcp__agentchat__…", "arguments": …}]}`. Hermes unwraps the call, so its result message carries the real tool name and is counted. A `tool_call` naming several of them is refused before anything runs (only `connectors__…` tools batch). Two refusals reach no AgentChat tool yet count differently: a bridged call Hermes refuses before dispatch (its schema probe or scope block) keeps the wrapper name `tool_call` and is not counted, while on v0.23.1 a call by a deferred tool's real name is refused ("Tool '…' does not exist") under the real name and is counted as failed; `main` reroutes that call through the bridge (`agent/turn_tool_validation.py`). So on v0.23.1 a model that tries the real name first and then succeeds through `tool_call` never has "every call succeeded", and a second silent look is deferred, not dismissed (an extra re-wake, never a wrong read). Also not counted: a bridged call cut off by the interrupt gets a stub named `tool_call` (the turn was interrupted, so nothing is recorded unless the answer went out). Not checked by the contract test yet. | Same ask as the turn rows: per-tool-call outcomes in the run's result. |
| `hermes_cli.config.read_user_config_raw` (or `read_raw_config`) / `save_config` / `is_managed` (**public**) | `mcp_registration.py` | Reads, changes and saves `config.yaml` as written: `load_config` expands `${AGENTCHAT_API_KEY}` from the `.env` a running Hermes has loaded, so read through it the plugin's own entry would carry a literal key and look hand-written. A managed install (`is_managed`) is not written. Writes `mcp_servers.agentchat` (with `${AGENTCHAT_API_KEY}` kept as a template, and the static `X-AgentChat-Client: hermes` header that tells the hosted tools this is Hermes) into the profile's `config.yaml`, and adds that header once to an entry an earlier release placed. `save_config` keeps comments, key order and quotes (a ruamel round trip, `utils.atomic_roundtrip_yaml_save`). Hermes sends configured headers on every request (`tools/mcp_tool_transport.py` `_http_endpoint`) and uses a changed entry once it reconnects: `/reload-mcp`, a gateway restart, or a reconnect (`_refresh_remote_config`). | `mcp.json` in a portable `plugin.json` package; Hermes (still on v0.23.1 and `main`) does not read it for native plugins. Upstream ask: native plugins declaring MCP servers in `plugin.yaml`. |

## The manual — internal behaviour, contract-tested

By default the skill is the package's copy (`test_this_releases_manual_is_served_from_the_package`). The
kept-current manual (`live_texts` on) rests on how Hermes serves a plugin skill, read from its code rather than a
documented contract; `test_a_skill_outside_the_package_is_read_fresh_after_an_atomic_replace` in the nightly
contract tests registers a path outside the package, replaces the file atomically and checks that `skill_view`
returns the new text and the skill stays registered.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `skill_view("agentchat:agentchat")` reads the registered file on every call (`tools/skills_tool_plugin.py` `_serve_plugin_skill`) | `manual.py` | A new manual reaches the agent on its next `skill_view`, with no re-registration and no restart. | **Upstream ask:** document it (or `ctx.update_skill(path)`). |
| A registered file found missing drops the registry entry for the rest of the process (`tools/skills_tool.py` `_resolve_plugin_skill`) | `manual.py` | The kept-current file is only ever replaced atomically (the kit's temp file + rename), never deleted. The package's copy (the default) is replaced by whatever upgrades the package: `hermes plugins update` swaps the plugin's folder with two back-to-back renames (`pm/publication.py` `StagedPlugin.publish`), a pip upgrade replaces the files one by one, so a `skill_view` in a running process during a pip upgrade drops the skill there until that process restarts (the README and the RUNBOOK say to restart after a pip upgrade). Not yet checked: whether a catalog install's editable snapshot (next section) is the path a running process registered, and when Hermes removes an old snapshot; `doctor` prints the registered path. | **Upstream ask:** re-resolve a plugin skill whose file is gone (or `ctx.update_skill(path)`). |
| `skill_view(name, file_path=…)` serves any file in the skill's folder | `manual.py` | `SKILL.md` is alone in its folder, away from the key file; a folder holding anything else is not registered (the package's copy is, and `doctor` says why). | — |
| `hermes_cli.plugins.get_plugin_manager().find_plugin_skill("agentchat:agentchat")` | `cli.py` (`doctor`, `status`) | Which file Hermes holds for the skill in this process (else what the plugin gave it). Diagnostics only. | **Upstream ask:** a public read of a plugin's registered skills. |

## Text-only releases and Hermes' unattended update — internal, nightly

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| What an unattended update (`plugins.auto_apply`) refuses: a wider surface, `hermes_cli.plugins_cmd_catalog.plugin_surface` / `surface_delta` (capabilities, tools, hooks, Python dependencies, sign-ins, a Desktop half), and new Python dependencies, `pm.plugin_declarations.read_python_declaration` (`hermes_cli/plugins_transaction.py` `_refresh_declared_dependencies`) | `scripts/refresh_bundled_texts.py`, `RUNBOOK.md` (releases) | A release that only refreshes the bundled manual and identity-line template widens none of these, so Hermes' own update applies it once the catalog pin moves, and catalog users get the new texts without a person at a terminal. `config_schema` is not a surface either, so this release's added setting does not hold an unattended update back. `test_a_release_that_changes_only_the_texts_widens_nothing` checks both against Hermes' own functions. | **Upstream ask:** a documented list of what an unattended update refuses. |
| An install from git without `--ref` is recorded unpinned (`hermes_cli/plugins_cmd_install.py`, `pinned` only with a requested revision) and tracks the branch: `hermes plugins update`, update-all and `plugins.auto_apply` pull it (`hermes_cli/plugins_auto_update.py` `update_all_unattended`: "tracked Git pulls") | `.github/workflows/refresh-texts.yml`, `RUNBOOK.md` | A commit on `main` reaches those installs without a release, so the refresh workflow opens a pull request and never merges it: the bundled texts move only at a release, when the refresh pull request is merged right before the tag. | — |

## The install-time security scan — internal, nightly

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_cli.plugins_install_scan.scan_plugin` / `should_allow_plugin_install` (older releases: `tools.plugin_guard`), `tools.skills_guard.scan_skill` / `should_allow_install` | `.github/scripts/hermes-install-scan.py`, run by `hermes-main` | What `hermes plugins install` and `hermes skills install` call on a clone or a downloaded skill: `dangerous` is refused (`--force` does not override), a plugin's `caution` asks to confirm. The nightly scans this tree as a clone holds it and the served manual, so a finding that would block or question an install shows within a day. Two findings need no Hermes and are checked on every PR (`tests/test_install_scan_hygiene.py`): invisible characters in any text file, and an inline-shell snippet in Python source (written with the `\x60` escape, this plugin's own guard included). | **Upstream ask:** a public scan entry point (`hermes plugins scan <dir>`). |

## How Hermes installs the plugin's package — internal, nightly

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| A catalog or git install (`hermes plugins install`) also installs this package into Hermes' environment, editable, from a snapshot of the plugin directory: `<generation>/workspace/plugin-sources/<pm.workspace._member_key(directory)>`, the directory name and the first 16 hex digits of the sha256 of its resolved path (seen on v0.23.2 and `main`) | `cli._is_editable_copy_of`, `cli._hermes_member_key` (`doctor`) | That install carries the `hermes_agent.plugins: agentchat` entry point, which Hermes drops next to the directory plugin. `doctor` counts it as the directory itself, not a second copy, when its `direct_url.json` (PEP 610) names the directory or its snapshot. If Hermes renames the snapshot, the false "2 agentchat plugins" warning comes back; nothing else breaks. `test_doctor_names_a_plugin_directorys_snapshot_as_hermes_does` checks the name against Hermes'. | **Upstream ask:** record the member in the install sidecar (`plugins/.install-metadata.json`), or a public read of which plugin owns an entry point. |
| Hermes' installer-owned record of each install, `plugins/.install-metadata.json`, keyed by the plugin directory's name: a catalog install carries a `catalog` block (`name`; `sha`, the commit checked out; `pin`, empty when installed with `--ref` off the catalog's pin), earlier releases the top-level `catalog_name` and `revision` (`hermes_cli.plugins_cmd_catalog.read_catalog_sidecar`; v0.23.2, v0.23.3 and `main`) | `cli._catalog_install_line` (`doctor`) | `doctor`'s directory-plugin line says a catalog install came from the catalog at that commit and that `hermes plugins update agentchat` moves it, instead of "a git clone (GitHub main unless installed with --ref)". Read from the record only, as Hermes reads it, never from the in-tree `.hermes-catalog.json` the repo could write. If Hermes changes the record, the line falls back to the git-clone wording; nothing else breaks. `test_doctor_reads_a_catalog_install_record_as_hermes_does` checks it against Hermes' reader. | **Upstream ask:** a public read of a plugin's install provenance. |

## The key

The key lives in the profile's own key file (`key_store.py`), not in `.env`: Hermes loads `.env` into its process
environment, from where it reached every command the agent runs, and `hermes profile create` copies `.env` into new
profiles. Hermes resolves `${VAR}` in an MCP header only from a profile's secrets (its `.env` and its secret sources:
`hermes_cli/config.py` `_env_ref_lookup`, `agent/secret_scope.py` `build_profile_secret_scope`), and every value there is
also in the environment Hermes builds a child from; what keeps one out of a child is Hermes' own name-based scrub. Checked
against hermes-agent v0.23.2 and `main` of 2026-11-18.

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `ctx.register_secret_source`, `agent.secret_sources.base.SecretSource` / `FetchResult` / `ErrorKind` / `SECRET_SOURCE_API_VERSION` (**public**) | `key_source.py`, `_register.register` | The `agentchat` secret source supplies `AGENTCHAT_API_KEY` from the profile's key file to Hermes' MCP client (the hosted tools' bearer), per profile home; Hermes re-pulls plugin sources after discovery (`website/docs/developer-guide/secret-source-plugin.md`). Under `plugins.isolation: host` the host child serves it (`HOST_OBJECT_BASES`). | This is the public hook. |
| `secrets.agentchat` in `config.yaml` (`enabled: true`, `api_key_env: AGENTCHAT_API_KEY`), through `read_user_config_raw` / `save_config` / `is_managed` (**public**) | `secrets_registration.py` | `enabled` turns the source on (Hermes' standard form). `api_key_env` is read by Hermes' child-environment policy (`tools/environments/local_env_policy.py` `_home_declared_key_env`, any `*_key_env` field outside `mcp_servers`; v0.23.2 and later) as naming a credential: stripped from terminal, background-process, script-runner and snapshot environments, refused by `env_passthrough`. `execute_code` already drops any `*KEY*` name. | **Upstream ask:** a declared credential name for standalone plugins (`requires_env` with `secret: true` honoured outside platform plugins, or `ctx.register_credential_env`). |
| `tools.environments.local_env_policy._is_provider_env_blocklisted` (private) | `cli._hermes_strips_key_from_commands` (doctor) | `doctor` asks Hermes' own policy whether `AGENTCHAT_API_KEY` is stripped, instead of assuming it. A Hermes without it: "could not check". | Same ask. |
| `hermes_cli.config.remove_env_value` / `get_env_path` (**public**) | `credentials.remove_key_settings`, `move_env_key`, `clear_credentials` | The plugin never opens a `.env` file. Moving a pre-0.6 key into the key file (`doctor --fix`, `login`) and logout remove our names' lines with Hermes' own writer (its lock and cache), only for the profile the process runs for (`get_env_path()` in that home) and only from the CLI, never a gateway (`remove_env_value` also clears the variable from the process). Hermes loads the profile `.env` over the shell's exports (`hermes_cli/env_loader.py` `load_hermes_dotenv`), so the key a CLI process reads is the line when there is one: a removal that finds no line means the key came from the environment, and an `AGENTCHAT_API_KEY` move is undone then. Contract-tested. | — |
| `hermes_cli.config.require_env_writable` (**public**) | `credentials._save_env_fallback` | The `.env` fallback for a key the server just issued asks Hermes' write lock first (a managed install refuses `save_env_value` without raising), instead of reading the file back. | — |

**Which key, and one key is one agent.** With a key file the key file decides: the key source supplies its key over
any `AGENTCHAT_API_KEY` set elsewhere, so a different value in `.env` or the environment is not used (0.6.5 refused
such a profile; it now runs as the key file's agent, and `doctor` says the key file decides). Except when the source
cannot carry it (`secrets.agentchat` disabled or not placeable, or no secret sources): then Hermes' MCP client takes
`AGENTCHAT_API_KEY` from the settings, and a different key there would make the hosted tools another agent than the
socket — nothing starts for the profile (`credentials.tools_key_conflict`, the same line `doctor` fails on;
`doctor --fix` places the block). A block that an activation places is used by the hosted tools once Hermes applies
secret sources again: right away when it is placed while the gateway loads plugins (Hermes re-pulls them after
discovery), else at the next gateway restart. A `config.yaml` Hermes cannot read refuses activation with its error
(said once), and `doctor` fails on it; `register()` never raises because of it. Without a key file, the key is the profile's setting as Hermes resolves it
(`get_secret`), and is refused when it is certainly or likely another profile's:

- another profile's **key file** holds it — found in each process (in `register()`, in the profile's scope) for the
  key source, which then gives the copy's hosted tools an empty key (`fetch` reads only our key files and that finding);
- a **named** profile that was never set up as an agent (no identity line in its `SOUL.md`, no hosted-tools entry in
  its `config.yaml`: `config.was_set_up_as_agent`, which reads those two files, never `.env`) holds the key the
  **default profile** resolved from its settings in this process — what `hermes profile create` makes of the default
  profile's `.env`, on one gateway. Its remedy also says `hermes -p <name> agentchat login` if it is meant to be its
  own agent. Nothing is placed for it (no identity line, no `secrets.agentchat`, no tools entry, so no hosted tools to
  mask). Without that evidence a never-set-up named profile activates and is set up as before (a key a container or
  a service exports, no wizard). Not caught any more: a never-set-up `.env` copy of a 0.5.x default profile served by
  a gateway of its own — telling it apart needed reading the default profile's `.env`, which ask 1 removes.

Profiles one process resolves with the same settings key (a multiplexed gateway, each in its own scope) are said to be
one agent, the default profile first (`shared_with`, `where: settings`). Other profiles' `.env` files are never read,
so two set-up pre-0.6 profiles holding one key only in `.env`, on gateways of their own, are not told apart until one
of them saves it (`doctor --fix`, `login`).

A key found only under the pre-v3 name `AGENTCHATME_API_KEY` (the August build) is copied into the key file — the
hosted tools read `AGENTCHAT_API_KEY` — once the server has accepted it (`GET /v1/agents/me`, the wizard's check: a
revoked key is never saved; its handle is recorded) and before the runtime opens a socket, in the default profile
and in a named profile set up as an agent before this activation, under a per-account lock (`<hermes
root>/agentchat-locks/adopt-<fingerprint>.lock`, beside the socket's leader lock, waited for 2 s at most) and only
when no other profile's key file holds it. Of two profiles holding one key, the first to adopt it is the agent; the
other is refused as the copy before it opens the account's socket (a busy lock stops the attempt, and the next one
decides). A named profile not set up before runs as before and adopts at its next start (this one sets it up), so
it does not take the default profile's agent first. At a multiplexed boot the launch profile loads first, so the default profile normally wins.
Remaining edge: a `--clone` or `--clone-all` copy of an August setup (its identity line copied too) on per-profile
gateways that start in any order — whichever adopts first is the agent, and the other one's `doctor` names the fix
(`hermes -p <the copy> agentchat logout`, then `register` it as its own agent; if the wrong one won, log that one out
and the other adopts at its next attempt).

## Settings and the wizard

| Surface | Where | Why | Public replacement |
|---|---|---|---|
| `hermes_cli.config.save_env_value` (**public**) | `cli.py`, `credentials._save_env_fallback` | The key goes to `.env` only as a fallback (the key file cannot be written, or this Hermes cannot read it for the hosted tools) so a key the server just issued is never dropped; `AGENTCHATME_API_BASE` when set. | The plugin settings schema (`type: secret`) covers Desktop; there is no public setter for a CLI wizard. |
| `hermes_cli.setup.{print_header, print_info, print_success, print_warning, prompt_choice, get_env_value, save_env_value}`, `hermes_cli.cli_output.{prompt, prompt_yes_no}` | `wizard.py` | The wizard looks and behaves like Hermes' own setup. Public: `hermes_cli.cli_output` (whole module) and `hermes_cli.setup.prompt_choice`; the other `setup` names are not. | `hermes_cli.cli_output`'s `print_*` and `hermes_cli.config`'s `get/save_env_value` would leave no non-public wizard import (`cli_output.print_header` looks different from setup's). |

## Not used yet, worth using

- `plugins.plugin_storage.plugin_data_dir(name)` (**public**): profile-scoped durable directory. The account-level state and
  leader locks live under the Hermes root on purpose (one per account across profiles), so it is not a drop-in.
