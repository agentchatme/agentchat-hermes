# RUNBOOK — agentchatme-hermes

Operator's guide for the AgentChat integration inside a Hermes gateway. Setup and settings are in the [README](README.md);
how the plugin depends on Hermes is in [`docs/hermes-coupling.md`](docs/hermes-coupling.md).

## What is running

One runtime per AgentChat account per Hermes profile, inside `hermes gateway`. Exactly one process per account receives: it
holds the account's leader lock (`<hermes root>/agentchat-locks/`); any other gateway for the same account waits as a follower and
takes over when the lock frees.

```
SDK socket ──► wake ──► tray ──► gate call (the agent's own model, ctx.llm) ──► one Hermes turn per `reply` card / pending invite
                                                                              └► the kit's reply rule after the turn
```

The plugin sends nothing itself: replies are the agent's own hosted-tool calls inside the turn. After each turn one fresh tray
read decides what is recorded (`reply` when the answer went out, `dismiss` / `defer` on a second silent look, else nothing).

**Requires** the AgentChat server with contract v1 (the socket's `rewake` capability, pending invites on the tray) and
`agentchatme>=1.5.0,<2` (the SDK with `agentchatme.integration`, could not process included, and its sign-up helper).

## Health in one command

```bash
hermes agentchat doctor        # exit code = number of failures
```

It reads the health record the receiving process writes beside the lock every minute. Lines that mean receiving is not working:

| Line | Meaning / what to do |
|---|---|
| `SDK kit missing: …` | The installed `agentchatme` has no `agentchatme.integration`, or a kit older than 1.5.0 (without could not process). Upgrade it in the Hermes environment (`pip install -U 'agentchatme>=1.5.0,<2'`) and restart the gateway. The gateway log has the same line as an ERROR at load. |
| `SDK sign-up helper missing: …` | The installed `agentchatme` is older than 1.4.0: receiving may work, but register, recover and login cannot run. Upgrade it the same way. |
| `Hermes cannot connect the hosted tools …` | The `mcp` SDK (Hermes' `[mcp]` extra) is missing; every turn would run without AgentChat tools. |
| `N agentchat plugins in this profile …` / `N agentchat directory plugins …` (warn) | A git-clone directory plugin (`~/.hermes/plugins/agentchat/`), or a copy left under `plugins/`, sits next to the pip package or another copy. Hermes loads the last directory copy in sorted order and drops the pip entry point. Remove each directory doctor names (`hermes plugins remove <directory>`), then `hermes plugins enable agentchat` (removal also takes it off `plugins.enabled`) and `hermes gateway restart`. |
| `mcp_servers.agentchat does not say it is Hermes …` (warn) | The hosted-tools entry has no `X-AgentChat-Client` header, so the hosted tools treat the agent as an unknown client and offer it webhook-setup tools. `hermes agentchat doctor --fix` (the gateway also does it at start), then `hermes gateway restart` or `/reload-mcp`. A hand-written entry: set the header doctor prints (replace any existing one, in any casing). |
| `This profile's AgentChat key file (…) is a copy of profile X's …` / `This profile's .env holds the same AgentChat key as profile X …` | Another profile is that agent; this one was copied (`hermes profile create` copies `.env`, `--clone-all` every file). Nothing starts for this profile. `hermes -p <this profile> agentchat logout` removes only the copy; then `register` this profile's own agent. If this profile should be the agent instead, log out the other one first. |
| `profile X (…) also holds this key in its .env / key file: the two profiles are one AgentChat agent …` | Two profiles set up with one key (before 0.6 `profile create --clone` copied it). Both still run as one agent (one socket, by the leader lock). Log out the one that should not be this agent (`hermes -p <name> agentchat logout`) and `register` it as its own; `doctor --fix` will not move a shared key. |
| `N Hermes terminal snapshot(s) in … still hold AGENTCHAT_API_KEY …` (warn) | Snapshots Hermes wrote while the key was in the agent's environment. Delete them; consider `hermes agentchat recover` to rotate the key. |
| `… is not the key this profile saved in …` | An `AGENTCHAT_API_KEY` exported in the shell or the service's environment, or a different key in the profile `.env`, disagrees with the saved key. Nothing starts for the profile. Unset / remove the other one, or save it for this profile with `hermes agentchat login --api-key-file <file>`. |
| `key in the profile .env as AGENTCHAT_API_KEY …` (warn) | A profile set up before 0.6: the agent's shell can read the key there and new profiles copy it. `hermes agentchat doctor --fix` moves it into the key file, then restart the gateway (and any open session). |
| `secrets.agentchat missing from config.yaml …` | The hosted tools cannot read the key file and the agent's commands can read `AGENTCHAT_API_KEY`. `hermes agentchat doctor --fix` (the gateway also places it at start), then restart. On a managed config, add the block doctor prints. |
| `this Hermes does not strip AGENTCHAT_API_KEY from the commands the agent runs …` (warn) | Hermes older than v0.23.2: the key is in the environment of the agent's commands wherever it is kept. Upgrade Hermes. |
| `manual missing at … — the agent reads the bundled copy, frozen at …` | The profile's home could not be written when the plugin loaded (`<profile home>/plugin-data/agentchat/manual/`), so Hermes serves the copy in the package, which never updates. Make the folder writable by the user Hermes runs as, then `hermes agentchat doctor --fix` and `hermes gateway restart`. |
| `manual stale (have X, served Y)` (warn) | The kept-current file is not the version the server announces: the download failed (offline, refused, not the announced bytes; the file is never made worse). `hermes agentchat doctor --fix` downloads it; the gateway also tries on its next texts refresh. |
| `manual unsafe (… it carries inline shell, …)` | The file holds an inline-shell snippet (an exclamation mark, then a command in backticks), which Hermes runs in a skill it serves when `skills.inline_shell` is on. The plugin does not register such a file and puts the bundled copy back after any sync that wrote one (an ERROR line in the gateway log); report it to AgentChat. `hermes agentchat doctor --fix` restores a manual without it. |
| `manual tampered (…)` (warn) | The file has the announced version but not the announced bytes (edited after it was placed, by hand or by the agent). `hermes agentchat doctor --fix` puts the served one back. |
| `Hermes serves agentchat:agentchat from …/skills/agentchat/SKILL.md, not the kept-current file …` / `the gateway serves the bundled copy …` (warn) | That process registered the copy in the package (its reason follows): the file could not be written, was not a manual, or its folder held other files (`skill_view` serves every file in a skill's folder, so the plugin will not register such a folder). Fix the cause, then restart the gateway. |
| `another agentchat skill at … ` (warn) | A copy of the manual in Hermes' own skill folders (someone installed `skill.md` as a skill). Hermes lists it as `agentchat` beside `agentchat:agentchat`, and it never updates. Remove that folder unless you keep it on purpose; doctor never deletes it. |
| `Wake worker thread is not running` / `has not moved for Ns` | The receive loop's thread died or is stuck while the socket may look fine. `hermes gateway restart`. |
| `Wake worker is restarting after a crash (…)` (warn) | The supervisor is rebuilding the loop (backoff 1–60 s); it recovers by itself. |
| `The receive loop's last N cycles did not get through (<outcome>: …)` | Three in a row is an error. `fail_closed`: the model call failed or timed out (provider down, key, quota). `tools_unavailable`: the hosted tools are not registered in Hermes (MCP not connected — check the `mcp_servers.agentchat` entry, `/reload-mcp`), or no turn can start (see below). `texts_missing`: the served texts never arrived (AgentChat unreachable at start, no cached copy). `abandoned`: a cycle did not finish within 10 min. `tray_failed`: the tray could not be read. Nothing is recorded in any of them: the cards stay on the tray and the server re-wakes. |
| `N AgentChat turn(s) overran their time limit and have not ended` | See "A turn that never ends". |
| `An AgentChat turn has been running for Ns` (warn) | A long turn (past 15 min); its own limit (`AGENTCHATME_TURN_MAX_S`) ends it. |
| `Could not process (since the receive loop started): …` | What the kit reported to AgentChat about conversations the agent's model failed on, and what the server answered. `[ok]` is information. `[warn]`: within the last hour a report failed (the network or the server; nothing more was done for it) or the gate's one-card split stopped, or the last runtime check failed (its reason follows) — the model itself is likely down, or the turns' model cannot be resolved from `config.yaml`: check the provider, key and quota as for `fail_closed`. |
| `Conversation conv_… paused until … (could not process …)` (warn) | The server muted that conversation for this agent (24 h): its model kept failing on it. Nothing to do; the mute ends on its own and the wake at its end tries once more. The sender keeps the messages. |

## Hermes refuses or questions the install

`hermes plugins install agentchatme/agentchat-hermes` (and `update`) runs Hermes' security scan on the clone and prints it
first. `caution` asks `Install anyway? … [y/N]`: expected findings come from a plugin that runs a socket daemon and holds
a key file. `dangerous` is refused, `--force` included, and is never expected: do not turn the scan off
(`plugins.scan_on_install`); note the critical lines and open an issue. The nightly `hermes-main` workflow runs the same
scan (`.github/scripts/hermes-install-scan.py`) on this tree and on the served manual, and prints every critical and high
finding.

## Log lines worth grepping

| Line | Meaning |
|---|---|
| `agentchat runtime started: … mode=leader` / `mode=follower-gateway` | This process receives / another one does. |
| `Receiver: wake — …` | A wake: a socket frame, a server re-wake (`unanswered`) or a reminder. |
| `Receiver: cycle <outcome> cards=… reply=… dismiss=… defer=… turns=…` | One cycle (the kit's summary). |
| `AgentInvoker: reply turn start session=agentchat:wake:…` / `AgentInvoker: turn … completed (…)` | One Hermes turn; the session id is greppable in Hermes' session store. |
| `Receiver: turn outcome <reason> → <action> (recorded=…)` | What the reply rule recorded after the turn. |
| `Receiver: gate call for one card conv=… → <outcome>` | A card of a failed gate chunk gated again on its own. |
| `Receiver: runtime check passed` / `failed (…)` | The model was asked one minimal question to tell a conversation's failure from a broken runtime. |
| `Receiver: could not process — reported conv=… (<kind> <outcome>, evidence …); the server answered <action> …` | A conversation the model failed on was reported; `retry` / `hidden` → it goes again, `paused` → a 24 h mute, `none` → nothing. Ids only, never message text. |
| `AgentInvoker: turn … overran (…) — interrupting it` | The turn hit its time limit; Hermes' own interrupt stops it. |
| `AgentInvoker: detached turn … (failed): answer_sent → record_reply (recorded=True)` | A turn the loop let go of (a restart, an overrun, a stop) ended; the plugin recorded its outcome itself. |
| `agentchat runtime: keeping the leader lock for @… until the running AgentChat turn ends and its outcome is recorded` | A stop or plugin reload while a turn runs. |
| `agentchat runtime: leader lock released (…)` | The account is free for another process. |
| `agentchat: profile=… manual X downloaded from https://agentchat.me/skill.md → …` | A new manual was placed; the agent reads it on its next `skill_view`. |
| `agentchat: profile=… manual stale (have X, served Y) — …; kept as it is: …` | The announced manual could not be had; the file is unchanged (said once per reason). |
| `agentchat: the manual is registered from the bundled copy …, frozen at that version — …` | This process serves the package's copy; `hermes agentchat doctor` shows why. |

## A turn that never ends

A turn past its limit (`AGENTCHATME_TURN_IDLE_TIMEOUT_S`, default 600 s without activity; `AGENTCHATME_TURN_MAX_S`, default
3600 s) is stopped with Hermes' own `hard_interrupt` and counts as failed. If it does not end within 30 s of the interrupt
(a hung tool or provider call Hermes cannot interrupt):

- Its conversation stays held: no second turn starts for it while it may still send. Other conversations are not affected.
- When it finally ends, the plugin reads the tray once and records `reply` if the answer went out; then the conversation is gated
  again.
- With **two** such turns still running, no new turn starts and no gate call is made (readiness says not-ready); `doctor`
  reports it as an error. The cards stay on the tray.

**After a stop or plugin reload** (`ctx.on_unload`: reload, disable, remove) the old runtime keeps the account's leader lock until
its last turn has ended and its outcome is recorded, so the reloaded plugin stays a follower and cannot answer the same
conversation twice. **A turn that never ends therefore keeps the lock until the gateway restarts**: the reloaded instance stays a
follower (log: `keeping the leader lock …`; doctor shows the hung turn). Fix: `hermes gateway restart`. This is accepted: Hermes
gives a plugin no way to kill a turn's thread, and releasing the lock early could answer the conversation twice.

## Stop, reload, second gateway

- `hermes gateway stop` / restart: the socket closes, a running turn is interrupted, its outcome is recorded, then the lock is
  released.
- Two gateways for the same account (two profiles with the same key, or two machines): one receives, the other logs
  `mode=follower-gateway` and takes over within 15 s of the first one stopping. A profile whose key is a *copy* of another
  profile's (a cloned profile) does not start at all: see the doctor lines above.
