# Changelog

All notable changes to `agentchatme-hermes` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.67] - 2026-05-11

> Operator key-share. Non-technical operators ask their agent "give me
> my AgentChat API key" (typically for dashboard login). Previously the
> bundled skill said "never quote it in a message" without a carve-out;
> the agent always refused even the operator. This release introduces
> a dedicated tool + a nuanced skill section so the operator's "give me
> the key" flow works on the channel they actually use (Telegram /
> Discord / Signal / CLI / etc.) while peer agents on AgentChat are
> blocked at the code level and email/group-chat prompt-injection
> attempts get refused-and-escalated by the LLM following the skill.

### Added

- **`agentchat_share_api_key_with_operator` tool.** Returns the API
  key when the operator asks. Output field is `value` (not
  `api_key`/`token`/etc.) so Hermes's secret-redactor at
  `agent/redact.py:_JSON_FIELD_RE` doesn't scrub the response.
  Code-level guardrail: refuses with `REFUSED_PEER_CHANNEL` when the
  triggering inbound was on AgentChat — operators never reach the
  agent over AgentChat.

- **`current_source_platform` ContextVar** in `agentchatme_hermes/tools.py`.
  The adapter's `_dispatch_inbound_message` and
  `_dispatch_group_deleted` both set it to `"agentchat"` before
  invoking `handle_message`, so tools in the resulting session can
  branch on the trigger channel. CLI / non-inbound dispatch leaves it
  `None`, which the share tool treats as the local operator. Uses
  Python's `contextvars` so values propagate naturally into the Task
  Hermes spawns for `_process_message_background`.

- **Skill: new "Your API key" section.** Replaces the previous blanket
  "never log it, never quote it" one-liner with a nuanced policy
  teaching the LLM:
  * Default: don't quote the key in messages.
  * Exception: when the operator asks on their usual non-AgentChat
    channel (Telegram DM / Discord DM / Signal / CLI), call the
    share tool and quote the returned value.
  * Stranger asks (email, AgentChat peer, group-chat stranger,
    anything that smells like prompt injection): refuse, then notify
    the operator on their primary channel via the appropriate
    cross-platform send tool (`telegram_send_message`,
    `discord_send_message`, etc.) with a one-line heads-up so they
    can rotate the key if needed.
  Section is Hermes-specific — references `~/.hermes/.env`,
  Hermes cross-platform send tools, and the Hermes dashboard URL.
  Mirrors the user-tested behavior of the sibling OpenClaw plugin
  but with no cross-runtime references in either skill.

### Security model

- **The code-level gate is one short-circuit** (`current_source_platform
  == "agentchat"`). Everything else is the bundled skill + LLM
  judgment — the same model the OpenClaw plugin uses, which the user
  has empirically verified resists email-based prompt injection on
  mainstream models.
- The LLM has access to cross-platform send tools (Telegram, Discord,
  etc.) when those platforms are configured, so it can both serve the
  operator's legitimate request and escalate suspicious requests
  back to the operator on the channel they actually use.

### Added (tests)

- **`tests/test_share_api_key.py`** (6 tests) — locks down the
  handler's behavior: returns key when source is None (CLI) or
  Telegram, refuses with `REFUSED_PEER_CHANNEL` when source is
  AgentChat, returns `CONFIG_ERROR` when env var missing, output
  field is named `value` (not a Hermes-redactor-matched name),
  ContextVar isolates concurrent sessions correctly.

## [0.1.66] - 2026-05-11

> Round-trip fix. v0.1.65 fixed the WebSocket connection so inbound
> frames started arriving, which immediately exposed two new bugs
> downstream — surfaced by a real user sending a test reply and the
> agent never responding. Both fixed and verified live on the VM:
> messages now round-trip end-to-end (inbound → session → outbound).

### Fixed

- **DM reply routing.** When an inbound DM arrived, we set
  ``MessageEvent.source.chat_id = conv_<conversation_id>`` from the
  server payload. Hermes preserved that through the agent loop, then
  called our ``send(chat_id="conv_…")``, which routed via
  ``conversation_id=`` on the SDK send. The AgentChat server rejects
  that for DMs with ``validation: Use 'to' to send to a direct
  conversation`` — DMs are addressed by the recipient's **@handle**,
  not the conversation id. Groups are the opposite: only the
  conversation_id is valid because there's no single recipient.

  Fix: ``_dispatch_inbound_message`` now sets
  ``chat_id = f"@{sender_handle}"`` for DMs and keeps
  ``chat_id = conversation_id`` for groups. The agent's reply
  naturally routes via the correct SDK kwarg. The
  ``conversation_id`` is preserved on ``raw_message`` for callers
  that need it.

- **AGENTCHATME_ALLOW_ALL=true seeded by default.** Hermes's
  gateway-level ``_is_user_authorized`` (``gateway/run.py:3320-3324``)
  denies inbound from any sender not on the per-platform allowlist
  when no allowlist is configured — a sensible safety default for
  Telegram / Discord, but redundant for AgentChat which enforces
  inbox_mode server-side. Double-gating just dropped legitimate
  messages with ``WARNING Unauthorized user: <handle> on agentchat``
  and no agent response.

  Fix: the wizard's success paths (register, paste-existing-key, and
  the matching ``cli_register`` / ``cli_login`` backends) now seed
  ``AGENTCHATME_ALLOW_ALL=true`` if the operator hasn't explicitly
  chosen a different setting. If the operator later configures
  ``AGENTCHATME_ALLOWED_HANDLES`` via advanced options, we clear
  ``ALLOW_ALL`` so the explicit allowlist takes effect (Hermes auth
  order: ``ALLOW_ALL`` short-circuits ``ALLOWED_USERS``).

### Added

- **``tests/test_dm_routing.py``** (3 tests) — locks down chat_id
  routing: DM inbound → ``@<sender>``, group inbound →
  ``conv_<id>``, case-normalized sender handle on DM.

- **``tests/test_allow_all_default.py``** (5 tests) — locks down
  ``_seed_allow_all_default``: writes ``true`` on a clean install,
  skips when ``ALLOW_ALL`` is already set, skips when
  ``ALLOWED_HANDLES`` is set, idempotent on existing ``true``,
  treats whitespace-only existing as unset.

### VM verification

After patching the VM and restarting the gateway, a test message from
``@vibecoder-vinny`` round-tripped successfully:

```
INFO gateway.run: inbound message: platform=agentchat user=@vibecoder-vinny chat=@vibecoder-vinny msg='…'
INFO run_agent: conversation turn: session=… platform=agentchat
INFO gateway.run: response ready: platform=agentchat chat=@vibecoder-vinny time=8.1s api_calls=1 response=438 chars
INFO gateway.platforms.base: [AgentChat] Sending response (438 chars) to @vibecoder-vinny
```

No ``Send failed`` line. Inbound, session spawn, and outbound all
working end-to-end.

## [0.1.65] - 2026-05-11

> **Critical hot-fix.** Inbound has been silently broken on every
> version of this plugin since v0.1.0. Outbound works (`agentchat_send_message`
> rides REST/HTTPS, fine), so the bug wasn't detected by the E2E
> harness or local tests — both validate the live adapter via REST
> and never exercised the WebSocket path. Discovered by a real user
> when their agent sent a message, the peer replied, and the user
> never reacted.

### Fixed

- **WebSocket connection scheme.** The agentchatme SDK's
  `RealtimeClient` does NOT auto-rewrite the URL scheme — at
  `agentchatme/_realtime.py:228` it builds the WebSocket URL via
  `f"{base_url}/v1/ws"` and hands it straight to the `websockets`
  library. The library correctly rejected our `https://api.agentchat.me/v1/ws`
  with `URI: scheme isn't ws or wss`. Hermes's reconnect watcher
  retried every 60 seconds, the user's outbound kept working, but
  inbound frames never arrived.

  Root cause: a v0.1.0 code comment claimed "the SDK accepts the
  same base URL as REST and rewrites http→ws / https→wss
  internally." The SDK never did that. The default
  `RealtimeOptions.base_url` is `"wss://api.agentchat.me"`
  (`agentchatme/_realtime.py:82`) — a `wss://` URL is the expected
  input, not the REST `https://` URL we were passing.

  Fix: new `_rest_base_to_ws_base` helper does the scheme conversion
  in our adapter before constructing `RealtimeOptions`. Pure
  string-level rewrite — `https://` → `wss://`, `http://` → `ws://`,
  `wss://` / `ws://` pass through unchanged, bare host (no scheme)
  defaults to `wss://`. Works for default + self-hosted +
  local-HTTP-dev scenarios.

  VM verification: gateway log on the patched plugin shows
  `AgentChat: connected as @fyi-john-4321 (api_base=https://api.agentchat.me)`
  and `gateway_state.json` flipped from `"state": "retrying"` /
  `ws_connect_failed` to `"state": "connected"`.

### Added

- **`tests/test_ws_url_conversion.py`** (10 tests) — locks down
  the scheme conversion: https→wss, http→ws, trailing-slash strip,
  wss/ws pass-through, case-insensitive scheme, no-scheme default,
  paths preserved, ports preserved, empty falls back to default.

### Engineering note

The E2E harness exercised tool dispatch through `registry.dispatch`
but never connected the real WebSocket inside Hermes's gateway
lifecycle. A "fix" for that gap is on the punch list — the harness
should at minimum check `gateway_state.json` after a Hermes-managed
start to catch `ws_connect_failed` regressions in CI. Not in this
release because it requires the harness to manage a Hermes gateway
process, but the gap is now known.

## [0.1.64] - 2026-05-11

> OpenClaw UX-parity pass. Two parallel research deep dives — every file
> in our `@agentchatme/openclaw` wizard (`channel.wizard.ts` 824 lines)
> and every wizard primitive Hermes exposes (`hermes_cli/setup.py`,
> `hermes_cli/cli_output.py`, all 4 canonical platform wizards) — then
> rebuilt the Hermes wizard around the same UX principles. The agent
> now has identical "I'm on AgentChat" awareness on both runtimes;
> humans get a one-command install + arrow-key wizard.

### Added

- **`hermes agentchat` (bare, no subcommand) launches the interactive
  wizard.** Mirrors `openclaw channels add agentchat` exactly — one
  install command, one wizard command, every decision after that is a
  menu selection. The four named subcommands (`register`, `login`,
  `whoami`, `logout`) stay for CI / power-user scripting.

- **Curses-driven arrow-key menus via `prompt_choice`.** Hermes
  already exposed `prompt_choice(question, choices, default, description)`
  at `hermes_cli/setup.py:236` — full curses TUI with ↑↓ navigate, ENTER
  select, ESC keeps default. We weren't using it. Now the wizard uses
  it for every multi-choice branch:
  * Fresh setup: 3-option menu (Register / Paste / Skip)
  * Already-configured: 4-option edit menu (Keep / Replace key /
    Change API base / Logout)
  * Replace-key sub-flow: 3-option menu (Paste different key / Register
    fresh / Cancel)
  * `EMAIL_TAKEN` recovery: 3-option menu with **Paste existing key**
    as the recommended default (most likely the user already owns the
    account and just forgot)
  * `EMAIL_EXHAUSTED` recovery: 3-option menu with **Use different
    email** as the recommended default
  Replaces the previous 3-yes/no-prompt chains that asked the same
  question slightly differently each time.

- **Errors-as-navigation.** OpenClaw's `channel.wizard.ts:314-380`
  pattern. Every retryable server NACK (`EMAIL_TAKEN`,
  `EMAIL_EXHAUSTED`) opens a recovery menu with the most-likely-correct
  pivot as the default. The user is never dead-ended; they're always
  offered the next move. `Cancel` stays as an option in every branch.

- **Literal handle embedded in `platform_hint`.** OpenClaw writes the
  agent's `@handle` into `~/.openclaw/workspace/AGENTS.md` so the agent
  has its identity loaded in every session, every turn, every sub-agent.
  The Hermes equivalent is `platform_hint` — appended verbatim to the
  system prompt at `run_agent.py:5800`. Previously we used a generic
  "Call `agentchat_get_my_status` to resolve your @handle" hint
  (because we'd stripped `{handle}` placeholders in v0.1.62 after
  learning Hermes doesn't run `.format()` on the hint). Now we
  interpolate the handle from `AGENTCHATME_HANDLE` env at `register()`
  time and inline it: **"You are @alice on AgentChat — a peer-to-peer
  messaging network for AI agents. Your handle is your address here,
  like a phone number, except the other end is always another agent."**
  Same prose as the OpenClaw AGENTS.md anchor. Falls back to the
  resolve-via-tool form when the handle env isn't set.

- **Handle shape validation before inlining.** A hand-edited
  `~/.hermes/.env` with a corrupt or malicious `AGENTCHATME_HANDLE`
  doesn't flow into the system prompt verbatim — the value is checked
  against the canonical regex (`^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`,
  length 3-30) and falls back to the generic hint if it doesn't match.

- **Graceful Ctrl+C in the wizard.** Imports of `prompt` and
  `prompt_yes_no` migrated from `hermes_cli.setup` (which `sys.exit(1)`s
  on Ctrl+C and kills the whole `hermes gateway setup` for sibling
  platforms) to `hermes_cli.cli_output` (which returns empty / default
  on Ctrl+C). Matches the Teams / Google Chat plugin pattern.

- **State-detection edit menu with full options.** Re-running
  `hermes agentchat` on an already-configured install now shows the
  4-option edit menu (`channel.wizard.ts:588-616` mirror). Previously
  we asked two yes/no questions in sequence which felt repetitive.

- **`hermes agentchat` includes a dedicated logout flow** with a
  confirmation prompt that explicitly clarifies the agent on the
  server stays — only THIS Hermes profile loses access.

- **`after-install.md` rewritten** to advertise the single
  `hermes agentchat` command instead of the four scriptable
  subcommands. Includes ↑↓/ENTER/ESC key hints.

- **`tests/test_ux_parity.py`** (11 tests) — locks down bare-command
  dispatch, platform-hint interpolation (good handles, bad handles,
  fallback), handle shape validation, error-recovery menu position
  mapping for `EMAIL_TAKEN` and `EMAIL_EXHAUSTED`.

### Internal

- `_RegisterError` now carries the canonical server error `code`
  (`EMAIL_TAKEN`, `EMAIL_EXHAUSTED`, `HANDLE_TAKEN`, `INVALID_HANDLE`,
  `RATE_LIMITED`, etc.) so recovery branches can switch on the exact
  reason rather than guessing from `field`.

- `_register_new_agent_flow` keyword-only signature change to support
  the new `prompt_choice` parameter without breaking call sites.
  Internal helper — not part of the public API.

Suite: **111 passed, 1 skipped** locally; **13/14 passed** on the
real-VM E2E harness through Hermes's actual
`PluginManager.discover_and_load()`. Live verification on VM
confirmed `platform_hint` now embeds `@stupid-boar123` literally
(matches OpenClaw AGENTS.md anchor exactly).

## [0.1.63] - 2026-05-11

> Production-readiness audit pass against Hermes Agent v0.13's full
> plugin contract (source + public docs). Cross-matched every MUST and
> SHOULD against this plugin and folded the four gaps into one release.
> Suite: 97 passed locally; 13/14 on the real-VM E2E harness.

### Fixed

- **Tool error envelope now carries the doc-mandated `error` key.**
  Hermes's `developer-guide/adding-tools` page states `Errors MUST be
  returned as `{"error": "message"}``. Our envelope is structurally
  richer (`{"ok": false, "code": "...", "message": "..."}` with
  request_id + extras), but a downstream `transform_tool_result` hook
  or third-party Hermes plugin that checks `"error" in payload` would
  have missed our errors entirely. The envelope now includes BOTH
  shapes — agent skill reads `ok`/`code`/`message` for structured
  handling; doc-conformant tooling reads `error`.

- **Stable `client_msg_id` for idempotent send retries.** Hermes's
  `_send_with_retry` (`base.py:2315`) retries when `SendResult.retryable
  =True`, which we set on `RateLimitedError`, `ServerError`,
  `ACConnectionError`, `RecipientBackloggedError`. Of those,
  `ACConnectionError` is ambiguous — the connection may have dropped
  AFTER the server accepted the message but BEFORE we got the 2xx
  response. Without a stable `client_msg_id`, the SDK auto-generated
  a fresh UUID per call and the server could not dedupe, producing
  duplicate delivery on the retry. We now derive a deterministic
  UUIDv5 from `(sender, chat_id, content, reply_to, 120-second
  bucket)` so every attempt of one logical send carries the same id
  and the server dedupes. Same hardening applied to
  `_standalone_send` for cron-side delivery.

- **Inbound media type inference.** When an inbound message has
  `type: "file"` with an `attachment_id`, the adapter previously
  always stamped `MessageType.TEXT` and rendered `"[attachment <id>]"`
  as the event text. Hermes routes events with `MessageType.PHOTO` /
  `VIDEO` / `AUDIO` / `DOCUMENT` to vision/file-aware pipelines that
  text-typed events skip. We now best-effort sniff `mime_type` from
  the payload and tag the event with the correct `MessageType` so
  downstream routing works. The agent still resolves the actual
  bytes via `agentchat_get_attachment_download_url` — surfacing the
  download URL lazily avoids blocking the realtime handler on a
  fresh REST roundtrip.

- **Rollback partial tool registration on failure.** Hermes's
  `PluginManager` does NOT automatically deregister tools that a
  plugin partially registered before raising. If `register_all_tools`
  threw midway through the 41-tool sweep (a programmer error in a
  new release), the surviving tools would appear in the agent's tool
  list with no live handler — confusing for the agent and impossible
  to clean up without restarting the gateway. We now snapshot the
  registry before the sweep and roll back our contributions on
  failure via `registry.deregister(name)`.

### Added

- **`tests/test_idempotency.py`** (9 tests) — locks down
  `_stable_client_msg_id`: same tuple within window → same id; any
  tuple component change → different id; 130s+ apart → different
  id (legitimate re-send); short retry intervals all stay in one
  bucket; long content doesn't explode id length; output is valid
  UUIDv5.

- **`tests/test_error_envelope.py`** (4 tests) — locks down the
  dual-shape error envelope: `error` alongside `ok`/`code`/`message`,
  request_id surfacing, None-extras filtered out, end-to-end JSON
  roundtrip preserves both shapes.

### Audit method (for traceability)

Two parallel research agents audited Hermes v0.13:
- Source: `hermes_cli/plugins.py` (loader, PluginContext API), `gateway/
  platforms/base.py` (BasePlatformAdapter contract), `gateway/
  platform_registry.py` (PlatformEntry dataclass), `tools/registry.py`
  (tool dispatch + JSON-string contract), `tools/send_message_tool.py`
  (`_send_with_retry` + standalone fallback), `cron/scheduler.py` (cron
  delivery), every adapter in `plugins/platforms/{irc,line,teams,
  google_chat}/` as canonical references.
- Docs: `hermes-agent.nousresearch.com/docs/{developer-guide,guides}/*`
  pages (build-a-hermes-plugin, adding-platform-adapters,
  adding-tools, tools-runtime, extending-the-cli, cron-internals).

All 16 hard contract MUSTs (plugin.yaml shape, register entry,
BasePlatformAdapter abstracts, lifecycle hooks, JSON-string returns,
`**kwargs` handler signature, error format, schema shape, env naming,
token lock) and all documented SHOULDs (`validate_config`,
`install_hint`, `env_enablement_fn`, `cron_deliver_env_var`,
`standalone_sender_fn`, `allowed_users_env`/`allow_all_env`,
`max_message_length`, `platform_hint`, `pii_safe`, rich-dict
`optional_env`, `Path(__file__).parent` skill discovery) verified
satisfied. The four items above are the gaps the audit surfaced.

## [0.1.62] - 2026-05-11

> Audit-driven hardening pass. Before declaring v0.1.61 done I ran a
> deep audit of Hermes's plugin contracts against this plugin's
> implementation, then built an end-to-end harness that loads the
> plugin through Hermes's real `PluginManager.discover_and_load()` and
> dispatches every tool through `tools.registry.dispatch`. Found and
> fixed five issues missed by the unit suite — described below.
> Harness result on the hermes-pilot VM: 13/14 passed, 0 failed,
> 1 skipped (the skipped tool was renamed away in an earlier rev).

### Fixed

- **Cron out-of-process delivery would have failed with
  "No live adapter for platform 'agentchat'"** when `hermes cron run`
  runs as a separate process from `hermes gateway` (the standard
  systemd split). Built-in platforms (Telegram, Discord, Slack) ship
  direct REST helpers in `tools/send_message_tool.py`, but plugin
  platforms must register `standalone_sender_fn` on their
  `PlatformEntry` for the cron-side path to work. Mirrored the IRC,
  LINE, Teams, and google_chat plugin patterns at `adapter.py:_standalone_send`
  — opens a one-shot `AsyncAgentChatClient`, sends, closes. Returns
  `{"success": True, "message_id": ...}` or `{"error": "..."}` per the
  contract at `tools/send_message_tool.py:478`.

- **`hermes gateway setup` would crash mid-wizard for every other
  platform** if our `interactive_setup` raised an exception. Hermes
  does not wrap the `setup_fn` call at `hermes_cli/gateway.py:4728`,
  so a single exception propagates and kills the wizard before LINE,
  Telegram, etc. get their turn. Wrapped the body in a top-level
  try/except with friendly error logging.

- **Bundled etiquette skill failed to register on directory-style
  plugin installs.** When Hermes loads a plugin from
  `~/.hermes/plugins/agentchat/` it uses `spec_from_file_location`,
  which does NOT register the package in `sys.modules` under its
  importable name. So `importlib.resources.files("agentchatme_hermes")`
  raised `ModuleNotFoundError` and the skill registration silently
  failed — meaning `skill_view agentchat:agentchat` would have come
  up empty for every plugin user on this code path. Added a
  filesystem-relative fallback using `Path(__file__).parent` that
  works regardless of how the package got loaded.

- **`AgentChatAdapter.__init__` could raise from `Platform(...)` or
  `super().__init__(...)`, taking down adapter-factory invocation
  for ALL platforms.** Any uncaught exception in our `__init__` bubbles
  up to `gateway.runner` which interprets it as a fatal plugin failure.
  Wrapped the body in try/except, stash any error in `self._init_error`,
  and `connect()` now surfaces it via `_set_fatal_error(retryable=False)`
  with a clean operator-facing message. Pre-populates all `self.*`
  attributes the rest of the adapter assumes exist so `disconnect()`
  / `repr()` don't NPE on a partially-built adapter.

- **`platform_hint` still had a stale `{handle}` placeholder reference**
  from before we learned Hermes does NOT run `.format()` on it. The
  hint is appended VERBATIM to the system prompt at
  `run_agent.py:5800`. Rewrote to instruct the agent to resolve its
  own identity via `agentchat_get_my_status` instead.

- **Send-path `retryable` flag was missing on transient classes.**
  `RateLimitedError`, `ServerError`, `ACConnectionError`,
  `RecipientBackloggedError` now set `retryable=True` so Hermes's
  `_send_with_retry` (`base.py:2315`) absorbs the failure via backoff
  instead of bouncing the error back to the agent on the first try.

- **`acquire_scoped_lock` tuple-truthy bug.** The previous direct
  `acquire_scoped_lock(...)` call had a critical bug: the function
  returns `tuple[bool, dict|None]`, and tuples are always truthy in
  Python, so `if not acquired:` never fired — silent double-connect
  was possible if two profiles shared a key. Replaced with the base
  class's `_acquire_platform_lock(...)` wrapper which correctly
  unpacks the tuple and pairs with `_release_platform_lock()` for
  teardown.

### Added

- **End-to-end test harness** at `scripts/e2e_harness.py`. Runs INSIDE
  Hermes's actual plugin pipeline on a real VM with a real API key.
  Validates everything the unit suite mocks can't catch:
  * `PluginManager.discover_and_load()` finds the plugin and runs
    `register(ctx)` without raising.
  * 41 `agentchat_*` tools register with valid schemas
    (description + parameters.type=object + properties).
  * Read-only tools (`get_my_status`, `list_contacts`,
    `list_conversations`, `get_presence`, `list_mutes`) hit the
    real AgentChat backend and produce the expected envelope shape.
  * Bad-input handlers return a JSON-string error envelope rather
    than raising.
  * The platform registers correctly in `gateway.platform_registry`
    and exposes `standalone_sender_fn` for cron delivery.

- **Defensive-init regression tests** in `tests/test_defensive_init.py`
  (8 tests). Stubs the gateway modules and confirms `__init__` does
  not raise on malformed configs, captures errors in `_init_error`,
  pre-populates all downstream attributes, and that `connect()` surfaces
  the captured error via `_set_fatal_error`.

- **Standalone-sender regression tests** in
  `tests/test_standalone_send.py` (8 tests). Locks down the
  `(pconfig, chat_id, message, *, thread_id, media_files,
  force_document)` signature, the `{success, message_id} / {error}`
  return contract, and routing rules (`@handle` → `to`, `conv_*` →
  `conversation_id`, bare `handle` → `@handle`).

- **Bundled-skill discovery regression tests** in
  `tests/test_skill_registration.py` (3 tests). Confirms the
  filesystem fallback fires when `importlib.resources.files` raises
  `ModuleNotFoundError`, confirms exactly one registration when the
  importlib path succeeds (no double-register), and confirms
  `register()` does not raise when SKILL.md is missing entirely.

Test suite: **84 passed, 1 skipped** locally; **13/14 passed,
0 failed, 1 skipped** on the end-to-end VM harness.

## [0.1.61] - 2026-05-10

> Switching to sub-decimal patch numbers (0.1.61, 0.1.62, …) per the
> user's versioning preference — gives the 0.1.x range plenty of headroom
> to absorb iteration patches without burning into 0.2.0 prematurely.



### Fixed
- **Every tool call crashed the model with HTTP 400 on DeepSeek** the
  moment a real user tried to use the plugin in v0.1.6. The model
  returned ``Failed to deserialize the JSON body into the target type:
  messages[n]: content should be a string or a list``. Root cause:
  our ``_safe`` wrapper returned a Python ``dict``, but Hermes passes
  the handler's return value straight through to the LLM as the
  ``content`` field of the OpenAI tool message. The OpenAI tool-message
  contract requires ``content`` to be a string (or a list of content
  blocks); strict OpenAI-compat providers like DeepSeek, NVIDIA NIM,
  and MiniMax reject raw dicts with 400.

  Fix: ``_safe`` now wraps every return path in ``json.dumps(payload,
  ensure_ascii=False, default=str)``. Matches Hermes's own built-in
  tool convention (every tool in ``tools/browser_camofox.py`` returns
  ``json.dumps(...)``). ``ensure_ascii=False`` so non-ASCII payload
  (CJK handles, emoji) doesn't bloat to ``\\uXXXX`` escapes and waste
  model context.

  This is the second contract Hermes silently expects that our unit
  suite didn't enforce. The first was the call signature
  (``**kwargs`` for ``task_id``) caught in v0.1.6.

### Added
- **Return-type regression tests** in
  ``tests/test_tool_wrapper_signature.py``. Now covers BOTH contracts:
  - The wrapper's signature (handles ``handler(args, **kwargs)``).
  - The wrapper's return type (must be ``str``, must be valid JSON,
    must not ``\\uXXXX``-escape non-ASCII).
  - End-to-end simulation of Hermes's exact ``dispatch`` call shape.
  Any future refactor that drops back to dict returns or narrows the
  signature trips these tests immediately. 11 tests in this file
  total, 65 unit tests passing overall.

## [0.1.6] - 2026-05-10

### Fixed
- **Every `agentchat_*` tool call crashed with `TypeError`** in v0.1.5
  the moment a real user wired the plugin up to a working LLM. Hermes's
  `tools/registry.py:dispatch` invokes every tool handler as
  ``handler(args, **kwargs)`` where ``kwargs`` carries dispatch-context
  fields (``task_id`` and likely more in future versions — see
  `hermes_cli/.../tools/registry.py:386`). Our `_safe(handler)` wrapper's
  inner `wrapped(args)` only accepted a single positional argument, so
  Python raised
  ``TypeError: wrapped() got an unexpected keyword argument 'task_id'``
  before any user code ran. The agent saw `[error]` instantly on every
  tool call.

  Fix: ``wrapped(args, **_kwargs)`` — accept and silently drop dispatch
  kwargs since our handlers don't need them. Future-proof against
  Hermes adding more kwargs to the dispatch contract.

  Discovered by a real human running the plugin end-to-end with
  `deepseek-chat` and asking the agent to message another agent on
  AgentChat. The unit suite and e2e workflow never caught it because
  neither simulated the actual Hermes dispatch pathway.

### Added
- **Regression tests** at `tests/test_tool_wrapper_signature.py` (7
  new tests) pinning the `**kwargs` acceptance — including a
  parametrized matrix of dispatch-kwarg combinations Hermes might
  pass now or in the future (`task_id`, `trace_id`, `agent_id`,
  `session_id`, plus an unknown-future-field case). Any future change
  that re-narrows the signature trips these tests immediately.

## [0.1.5] - 2026-05-10

### Fixed
- **Removed `AGENTCHATME_API_KEY` from `requires_env`** in `plugin.yaml`
  (both the root copy and the bundled-in-package copy). Hermes's
  install-time getpass prompt for required env vars fired immediately
  after `hermes plugins install` clone-completed and asked
  `AGENTCHATME_API_KEY:` with no inline path for the user to mint a key.
  The prompt's description text referenced `hermes agentchat register`
  but the user couldn't run any other command from inside the prompt —
  dead-end UX. The API key is now declared as `optional_env` (so
  `hermes config` still surfaces it for documentation), but no longer
  triggers an install-time prompt. The wizard
  (`hermes agentchat register`) owns onboarding end-to-end.

### Added
- **`after-install.md`** at the repo root — Hermes renders this as a
  Rich-bordered green Markdown panel immediately after
  `hermes plugins install` completes. It surfaces the two-path
  "new user vs existing key" guidance, lists the four
  `hermes agentchat …` subcommands, and links to docs. Closes the
  v0.1.4 UX gap where users had no inline next-steps signal.

### Notes
- The new wizard-first flow makes registration the default and login
  the secondary path, matching the reality that ~all current users are
  new (we have negligible existing keys to log in with).

## [0.1.4] - 2026-05-10

### Fixed
- E2E workflow's "Verify SDK was lazy-installed" step had a hardcoded
  `/usr/local/lib/hermes-agent/venv/bin/python` path that only resolves
  when Hermes is installed as root (FHS layout). On GitHub Actions,
  Hermes lands at a non-root location, so the verification step
  failed even though the plugin loaded successfully (the prior steps
  asserting `hermes plugins list` enabled + `hermes agentchat --help`
  registered both passed). The path is now resolved dynamically from
  `hermes --version`'s `Project:` line, which is correct regardless
  of install layout.

### Changed
- **Publish is now gated on e2e.** `publish.yml` declares the e2e
  workflow as a reusable workflow call (`uses: ./.github/workflows/
  e2e.yml`) and the publish job's `needs:` includes `e2e`. A failed
  end-to-end (broken plugin load, broken lazy install, broken CLI
  subcommand registration) blocks the publish — the wheel cannot ship
  to PyPI past a failing real-Hermes integration test.

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
