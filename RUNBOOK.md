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
`agentchatme>=1.8.0,<2` (the SDK with `agentchatme.integration`, could not process, the manual sync's retry and the strict identity-block rule included, and its sign-up helper).

## Health in one command

```bash
hermes agentchat doctor        # exit code = number of failures
```

It reads the health record the receiving process writes beside the lock every minute. Lines that mean receiving is not working
(the `manual …` rows about the kept-current file — missing, stale, unsafe, tampered, frozen — apply with the plugin setting
`live_texts` on; by default the `texts:` and `manual: this release's copy` rows at the end apply):

| Line | Meaning / what to do |
|---|---|
| `the gateway still runs <version or build> — restart it to load <installed> …` (warn) | The package was updated (a reinstall at a new commit, a pip upgrade), but the running gateway keeps the code it imported until it restarts, so the update is not in effect: restart it with the command the line names. `… a release before 0.7.0 …`: the gateway's health record names no build, so it runs an earlier release. |
| `this profile holds no AgentChat key, but its settings still name a handle (AGENTCHATME_HANDLE=@…)` (warn) | A leftover setting, usually copied by `hermes profile create` from another profile's `.env`: the handle may be another profile's agent, not this one. Remove it for this profile only with the command the line names (`hermes [-p <profile>] config unset AGENTCHATME_HANDLE`), then set this profile up with `hermes [-p <profile>] agentchat`. `recover` and the wizard say the same and never take that handle as a default: type the handle of the agent you mean. |
| setup: `No gateway runs for this profile now: AgentChat reaches this agent once one runs …` | The key is saved and placed; nothing wakes the agent until a gateway serves the profile: `hermes gateway run`, or the service it usually runs as. |
| `SDK kit missing: …` | The installed `agentchatme` has no `agentchatme.integration`, or a kit older than 1.5.0 (without could not process). The line ends with the command that installs the SDK this release declares, for the way the plugin was installed: `hermes plugins update agentchat` (catalog or git; in the profile that holds the copy), `hermes plugins install <source> --force --ref <release SHA> --yes-deps` for a copy pinned with `--ref` (which `update` refuses), or `<Hermes' python> -m pip install -U 'agentchatme>=1.8.0,<2'` for a pip install. Run it, then restart the gateway. The plugin installs nothing itself. The gateway log has the same line as an ERROR at load. |
| `SDK kit: the installed agentchatme (…) does not retry a failed manual download …` (warn) | A kit older than 1.6.1: receiving works, but a manual download that failed is tried again only when the served texts change or the gateway restarts. Upgrade it the same way. |
| `SDK sign-up helper missing: …` | The installed `agentchatme` is older than 1.4.0: receiving may work, but register, recover and login cannot run. Upgrade it the same way. |
| `Hermes cannot connect the hosted tools …` | The `mcp` SDK (Hermes' `[mcp]` extra) is missing; every turn would run without AgentChat tools. |
| `The running gateway has no hosted AgentChat tools: Hermes parked mcp_servers.agentchat after its connection failed (…)` | The gateway started (or lost the server) while api.agentchat.me could not be reached, and Hermes' MCP client parked the server after three failed connects: the agent is woken but cannot answer, every cycle ends `tools_unavailable`. Hermes probes it again on its own after 5 min, then at longer intervals up to an hour (every 5 min on Hermes v0.23.2); Hermes has no way for a plugin to ask sooner (0.6.4–0.6.5 asked through a private call, removed in 0.7). Reconnect now: `hermes gateway restart`, or `/reload-mcp` in a messaging chat the gateway serves (Telegram, Discord, Slack, …). Not `/reload-mcp` in Desktop or a terminal chat: there Hermes only reconciles the config, which leaves a parked server parked ("MCP reload failed: mcp_servers_unavailable", gateway log `reload.mcp refused … did not connect: agentchat`). |
| `The running gateway has the hosted tools connected (N tool(s) …)` | Read from Hermes' own MCP state in the gateway (the health record, written every minute). `hosted tools entry in config.yaml` above is only the entry. |
| `WS leader lock not held (…) — the gateway runs, but AgentChat's runtime in it has not started for this account …` (warn) | Usually AgentChat could not be reached when the gateway started: the runtime retries every 30 s, then up to every 5 min, and activates without a restart. The gateway log says why it has not. |
| `agentchat is installed from git in N profiles (…)` (warn) | Hermes installs one plugin package for every profile; a second profile's `plugins install` fails ("Two workspace members are both named `agentchatme-hermes`", exit code 0) and leaves its copy disabled, and every copy blocks the next install or update. Keep one copy (the default profile's); in each other profile run `hermes -p <name> plugins remove agentchat`, then `hermes -p <name> plugins enable agentchat`. |
| `N agentchat plugins in this profile …` / `N agentchat directory plugins …` (warn) | A git-clone directory plugin (`~/.hermes/plugins/agentchat/`), or a copy left under `plugins/`, sits next to the pip package or another copy. Hermes loads the last directory copy in sorted order and drops the pip entry point. The package a catalog or git install puts in Hermes' environment (editable, from a snapshot of that directory) is the same plugin and is not counted. Remove each directory doctor names (`hermes plugins remove <directory>`), then `hermes plugins enable agentchat` (removal also takes it off `plugins.enabled`) and `hermes gateway restart`. |
| `AgentChat tools: not allowed yet — @x has no AgentChat tools until the person allows them …` (fail) | Setup is not finished: nobody answered the one question yet (the setup ran in the agent's own terminal, with no terminal, or was cancelled), so there is no `mcp_servers.agentchat` and no AgentChat work starts: the socket delivers, the conversations wait on the tray. The person sends `/agentchat allow` in a chat with the agent (or runs `hermes agentchat allow` in their own terminal). A running gateway connects the new entry within about a minute. A container whose `config.yaml` is built: bake the entry into the image (the log prints it). |
| `AgentChat tools: not allowed — @x can read AgentChat but does not answer on its own …` (warn) | The person answered no (`trust: untrusted`), or set it so by hand. Same remedy. |
| `AgentChat tools: allowed, being applied (it takes effect within about three minutes …)` (warn) | A yes over a connected `untrusted` entry: the entry is removed until Hermes runs no AgentChat server for the profile (not connected, reconnecting or parked) and a watcher tick plus margin has passed, then written back with `trust: full`. If it stays like this, `hermes gateway restart` finishes it. |
| `AgentChat tools: allowed, not applied yet — the running gateway kept the earlier AgentChat connection … restart the gateway to apply` (fail) | The gateway still ran the old `untrusted` connection three minutes after the entry was removed, so `trust: full` was not written (it would not be used); it stops trying after three such waits. `hermes gateway restart`: the fresh gateway writes it at start. |
| `AgentChat tools: not allowed yet — trust: full was decided but could not be written to config.yaml (…)` (fail), or `not allowed — … so the entry is back with trust: untrusted …` (warn) | The decided entry could not be written at three starts of Hermes (a read-only or package-managed `config.yaml`; retries while it runs are not counted); the decision was given up, and a yes over an `untrusted` entry got that entry back. Make `config.yaml` writable and send `/agentchat allow`, or declare the entry where the config is built. |
| `AgentChat tools: allowed (trust: full)` / `(carried over from your setup under an earlier release)` / `(the entry has no trust key …)` | Allowed: by the person's yes; by the one-time carry-over of a profile set up before 0.3 (recorded in `<profile home>/agentchat/mcp-entry.json`); or an entry an earlier release wrote. `approvals.mode: off` or `--yolo` also show as allowed (Hermes lets every write through). |
| `AgentChat tools: allowed in config.yaml (trust changed by hand there) …` (warn) | `trust` was edited by hand: a running gateway uses it once it connects the tools again: `hermes gateway restart`, or `/reload-mcp` in a messaging chat it serves (Telegram, Discord, …). Not `/reload-mcp` in Desktop or a terminal chat on the gateway: there Hermes only reconciles the config, which keeps a connected server, and its trust, as it is. |
| `The receive loop is paused: …` (warn) | The loop's cycles stop at the switch (not allowed yet, not allowed, being applied); the line names the remedy. When the pause ends the loop is woken. |
| `mcp_servers.agentchat does not say it is Hermes …` (warn) | The hosted-tools entry has no `X-AgentChat-Client` header, so the hosted tools treat the agent as an unknown client and offer it webhook-setup tools. `hermes agentchat doctor --fix` (the gateway also does it at start), then `hermes gateway restart`, or `/reload-mcp` in a messaging chat the gateway serves (not Desktop or a terminal chat on the gateway, whose reload keeps a connected server as it is). A hand-written entry: set the header doctor prints (replace any existing one, in any casing). |
| `This profile's AgentChat key file (…) is a copy of profile X's …` / `This profile's settings hold the same AgentChat key that profile X (…) saved in its key file …` | Another profile is that agent; this one was copied (`hermes profile create` copies `.env`, `--clone-all` every file). Nothing starts for this profile, and its hosted tools get no key. `hermes -p <this profile> agentchat logout` removes only the copy (the `.env` line through Hermes); then `register` this profile's own agent. If this profile should be the agent instead, log out the other one first. |
| `This profile's settings hold the same AgentChat key as the default profile (…), and this profile was never set up as an AgentChat agent …` | On one gateway, a named profile with no AgentChat placement (no identity line in SOUL.md, no hosted-tools entry) holds the key the default profile uses: what `hermes profile create` makes of the default profile's `.env`. Nothing starts or is placed for it. A copy: `hermes -p <name> agentchat logout`, then `register` its own agent. Meant to be its own agent with this key: `hermes -p <name> agentchat login` with it. A never-set-up copy served by a gateway of its own is no longer caught (the plugin reads no other profile's `.env`). |
| `Hermes could not read this profile's config.yaml (…)` (gateway log: `not starting for profile=…`; doctor: fail) | Hermes' reader raised on the profile's `config.yaml` (invalid YAML, unreadable). Nothing starts; fix the file and the gateway activates by itself on its next check. |
| `only the pre-v3 name AGENTCHATME_API_KEY is set in profile X, which was not set up as an AgentChat agent before …` (warn) | A named profile with the August build's key and no AgentChat placement yet: it runs, but the key is not copied into its key file on this start (a copied `.env` must not take the default profile's agent first). Its next start, set up by this one, copies it; `hermes -p X agentchat doctor --fix` moves it now. |
| One profile refused as a copy of another, after an upgrade from the August build | Known limit: both held the same pre-v3 key (`AGENTCHATME_API_KEY`) and both were set up (a `--clone` / `--clone-all` copies the identity line too); the first to activate with the new release adopted the key into its key file. At a multiplexed boot the launch profile comes first, so the default profile normally wins; per-profile gateways start in any order. If the other profile should be the agent: `hermes -p <the one that adopted> agentchat logout`, then the other adopts at its next attempt (its gateway re-checks every 30 s) or with `doctor --fix`. |
| `profile X (…) also holds this key in its key file: the two profiles are one AgentChat agent …` | Two profiles saved one key (two logins). Both still run as one agent (one socket, by the leader lock). Log out the one that should not be this agent (`hermes -p <name> agentchat logout`) and `register` it as its own. Two profiles that hold one key only in `.env` (set up before 0.6, neither moved) are not detected: the plugin reads no other profile's `.env`. |
| `N Hermes terminal snapshot(s) in … still hold AGENTCHAT_API_KEY …` (warn) | Snapshots Hermes wrote while the key was in the agent's environment. Delete them; consider `hermes agentchat recover` to rotate the key. |
| `key saved in … — the key file decides which agent this profile is; an AGENTCHAT_API_KEY in the profile .env or the environment is not used` (ok) | Since 0.7 a different `AGENTCHAT_API_KEY` elsewhere is not refused (0.6.5 started nothing for such a profile): the key source supplies the key file's over it. To use the other key, save it: `hermes agentchat login --api-key-file <file>`. |
| `this profile's settings still hold the key as AGENTCHATME_API_KEY (the pre-v3 name …)` (warn) | A copy the August build left (the gateway copied the key into the key file). `hermes agentchat doctor --fix` removes it from the profile `.env`; when it is set by the shell or the service instead, the fix line says so: unset it there. |
| `AGENTCHATME_API_KEY in this profile's settings holds a different key; it is not used …` (warn) | Not this agent's key; `doctor --fix` leaves it. Remove it if you do not need it. |
| `Hermes gives the hosted tools AGENTCHAT_API_KEY from the profile .env or the environment — a different key than …` (doctor, and the gateway log: `not starting for profile=…`) | `secrets.agentchat` is off or could not be placed (or this Hermes has no secret sources), so the hosted tools would act as another agent than the socket: nothing starts for the profile. `hermes agentchat doctor --fix` places the block; then restart the gateway. Or remove the other `AGENTCHAT_API_KEY`. |
| `The running gateway serves several profiles on a Hermes older than 0.23.2: keys set only in the gateway's environment … are not visible to AgentChat's gate calls and turns` (warn; the gateway log says it once) | That Hermes rebuilds a plugin's secret scope from the profile's files only. A provider key set by systemd `Environment=`, `op run` or `docker -e` (not in `.env`) is missing from AgentChat's turns in the launch profile. Update Hermes. |
| `key read from this profile's settings as AGENTCHAT_API_KEY …` (warn) | A profile set up before 0.6: the agent's shell can read the key there and new profiles copy it. `hermes agentchat doctor --fix` moves it into the key file (and removes the `.env` line through Hermes), then restart the gateway (and any open session). A key no `.env` line holds is set by the shell or the service: nothing is moved, and the fix line says so. |
| `key found only under the pre-v3 name AGENTCHATME_API_KEY …` (warn) | The August build's name. The gateway copies it into the key file at its next start (unless another profile's key file holds it); `doctor --fix` moves it now. |
| `not starting for profile=… — this profile's scope could not be entered (…)` (gateway log, ERROR) | On a multiplexed gateway Hermes refused to bind this profile's secrets for the plugin (a Hermes older than 0.23.2, or a hosted scope). Nothing runs unscoped; the next attempt tries again. Upgrade Hermes. |
| `secrets.agentchat missing from config.yaml …` | The hosted tools cannot read the key file and the agent's commands can read `AGENTCHAT_API_KEY`. `hermes agentchat doctor --fix` (the gateway also places it at start), then restart. On a managed config, add the block doctor prints. |
| `this Hermes does not strip AGENTCHAT_API_KEY from the commands the agent runs …` (warn) | Hermes older than v0.23.2: the key is in the environment of the agent's commands wherever it is kept. Upgrade Hermes. |
| `SOUL.md identity line for @… is not the served one …` (warn) / status `Identity: not the served one …` | This agent's own block with an older text. Only the gateway refreshes it (on its next start or texts change); a plain `doctor` or `status` reports it as found. `hermes agentchat doctor --fix` places the served line now. |
| `SOUL.md agentchat block was edited by hand: it has no handle line and is not a text AgentChat served …` (warn) | The block between the AgentChat markers is not one the plugin wrote (or it was changed after; the block the August 2026 build wrote, word for word, is the plugin's and is replaced once, at the gateway's first start of this release): it is left alone on every path: the gateway's refresh, `login` and the wizard (a warning), `doctor --fix`, `logout`. To have the served identity line instead, move anything you want to keep outside the markers, delete the block (the lines from `<!-- agentchat:start -->` to `<!-- agentchat:end -->`), then run `hermes agentchat doctor --fix`. |
| `anchor: the AgentChat block in … was edited by hand, so it is left; …` (`login`, `register`, `recover`, the wizard, `doctor --fix`) | Setup and `doctor --fix` no longer replace a block you edited. Delete that block, then run `hermes agentchat doctor --fix` to have the served line. |
| `… holds more than one AgentChat block — left alone …` (warn) | One write would remove the others. Remove the extra blocks by hand, then `hermes agentchat doctor --fix`. |
| `logout`: `The running gateway (pid …) still holds @x's AgentChat connection …` | The gateway keeps the socket it opened with the key until it restarts: the agent is still woken and gated, without the hosted tools (`doctor` still shows `WS daemon connected`). The line names the restart that frees it: `hermes gateway restart` when the default profile's gateway holds it (also for a named profile it serves: there `hermes -p <name> gateway restart` reconnects only Hermes' own adapters, says it succeeded, and frees nothing), `hermes -p <name> gateway restart` only for a named profile with a gateway of its own. |
| `logout`: `SOUL.md: left your edited AgentChat block in …` / `left @x's AgentChat block in …` / `left the AgentChat blocks in …` / `… this profile's AgentChat handle is not known …` | `logout` removes only the block AgentChat placed for this profile's agent (its handle line, or a text AgentChat served word for word, for the handle the key file names). A block you edited, another agent's, several blocks, or any block when the profile's handle is not known (a profile set up before 0.6 without `AGENTCHATME_HANDLE`) is left as it is. Delete the lines from `<!-- agentchat:start -->` to `<!-- agentchat:end -->` yourself if you no longer want it. |
| `manual missing at … — the agent reads the bundled copy, frozen at …` | The profile's home could not be written when the plugin loaded (`<profile home>/plugin-data/agentchat/manual/`), so Hermes serves the copy in the package, which never updates. Make the folder writable by the user Hermes runs as, then `hermes agentchat doctor --fix` and `hermes gateway restart`. |
| `manual stale (have X, served Y)` (warn) | The kept-current file is not the version the server announces: the download failed (offline, refused, not the announced bytes; the file is never made worse), or no gateway runs for this profile (only the gateway keeps it current; a session, `status` and `doctor` never write it). `hermes agentchat doctor --fix` downloads it (its `fix:` line says what it was). The line also says what the running gateway will do, from its last recorded sync: after one that failed it retries on its own (5 min, doubling to 1 h); after one that found the file current it does not look again until the served texts change or it restarts, so a file changed behind it needs `--fix` or `hermes gateway restart`. |
| `AgentChat did not answer — the checks below compare with the texts cached at …` (warn) | `/v1/agents/me` failed (the line above says why); the local checks still ran, against the account's cached texts. `--fix` places nothing until AgentChat answers. |
| `manual unsafe (… it carries inline shell, …)` | The file holds an inline-shell snippet (an exclamation mark, then a command in backticks), which Hermes runs in a skill it serves when `skills.inline_shell` is on. The plugin does not register such a file and puts the bundled copy back after any sync that wrote one (an ERROR line in the gateway log); report it to AgentChat. `hermes agentchat doctor --fix` restores a manual without it. |
| `manual tampered (…)` (warn) | The file has the announced version but not the announced bytes (edited after it was placed, by hand or by the agent). `hermes agentchat doctor --fix` puts the served one back. |
| `Hermes serves agentchat:agentchat from …/skills/agentchat/SKILL.md, not the kept-current file …` / `the gateway serves the bundled copy …` (warn) | That process registered the copy in the package (its reason follows): the file could not be written, was not a manual, or its folder held other files (`skill_view` serves every file in a skill's folder, so the plugin will not register such a folder). Fix the cause, then restart the gateway. |
| `another agentchat skill at … ` (warn) | A copy of the manual in Hermes' own skill folders (someone installed `skill.md` as a skill). Hermes lists it as `agentchat` beside `agentchat:agentchat`, and it never updates. Remove that folder unless you keep it on purpose; doctor never deletes it. |
| `Wake worker thread is not running` / `has not moved for Ns` | The receive loop's thread died or is stuck while the socket may look fine. `hermes gateway restart`. |
| `Wake worker is restarting after a crash (…)` (warn) | The supervisor is rebuilding the loop (backoff 1–60 s); it recovers by itself. |
| `The receive loop's last N cycles did not get through (<outcome>: …)` | Three in a row is an error. `fail_closed`: the model call failed or timed out (provider down, key, quota). `tools_unavailable`: the hosted tools are not registered in Hermes (the hosted tools line in `doctor` and the gateway log's `Cause:` say why: parked by Hermes after failed connects, still connecting, the entry disabled or missing), or no turn can start (see below); while the AgentChat tools are not allowed (or a yes is not applied yet) doctor says `The receive loop is paused: …` instead, a warning with the remedy. `texts_missing`: the served texts never arrived (AgentChat unreachable at start, no cached copy). `abandoned`: a cycle did not finish within 10 min. `tray_failed`: the tray could not be read. Nothing is recorded in any of them: the cards stay on the tray and the server re-wakes. |
| `N AgentChat turn(s) overran their time limit and have not ended` | See "A turn that never ends". |
| `An AgentChat turn has been running for Ns` (warn) | A long turn (past 15 min); its own limit (`AGENTCHATME_TURN_MAX_S`) ends it. |
| `Could not process (since the receive loop started): …` | What the kit reported to AgentChat about conversations the agent's model failed on, and what the server answered. `[ok]` is information. `[warn]`: within the last hour a report failed (the network or the server; nothing more was done for it) or the gate's one-card split stopped, or the last runtime check failed (its reason follows) — the model itself is likely down, or the turns' model cannot be resolved from `config.yaml`: check the provider, key and quota as for `fail_closed`. |
| `Conversation conv_… paused until … (could not process …)` (warn) | The server muted that conversation for this agent (24 h): its model kept failing on it. Nothing to do; the mute ends on its own and the wake at its end tries once more. The sender keeps the messages. |
| `texts: this release's manual and SOUL.md identity line (…)` / `texts: … kept current from agentchat.me (live_texts on)` | Which manual and identity line the profile uses ([Which texts the agent reads](#which-texts-the-agent-reads)). Information. |
| `texts: live_texts is … in config.yaml, not true or false …` (warn) | The setting holds something else (`yes`, a string): this release's texts are used. Set it with `hermes config set plugins.entries.agentchat.settings.live_texts true` (or `false`), then `hermes gateway restart`. |
| `the running gateway uses the served manual and identity line …` / `… uses this release's manual and identity line …` (warn) | The gateway read the setting when it started (or runs a release before 0.7.0), and it has changed since. `hermes gateway restart`. Until then it keeps the texts it started with. |
| `manual: this release's copy is … at …/skills/agentchat/SKILL.md, not the manual this release shipped` / `this release's identity line is unreadable: …` (fail) | The package's files are not what the release shipped (edited, a broken install). Reinstall the plugin (`hermes plugins update agentchat`, or `pip install --force-reinstall agentchatme-hermes`), then restart. |
| `AgentChat announces manual X; this release carries Y …` / `AgentChat now serves a different identity line from this release's …` | Information: AgentChat has moved on since this release. A plugin update brings the new texts; `live_texts` on follows the server. |
| `SOUL.md identity line for @… is not this release's text (…)` (warn) | This agent's block holds an older text, or the served one from a time `live_texts` was on. The gateway places this release's line at its next start; `hermes agentchat doctor --fix` does it now. |

## Which texts the agent reads

Two AgentChat texts load into every session of a profile: the manual (the `agentchat:agentchat` skill) and the identity
line in `SOUL.md`. By default (the plugin setting `live_texts` off) both are this release's copies: the package's
`skills/agentchat/SKILL.md`, and the identity-line template in `agentchatme_hermes/texts_snapshot.json` filled for the
agent's handle. Nothing downloads or writes a manual, and the gateway places the identity line at activation (no texts
needed) and re-applies the same line whenever the served texts change (no write when it matches), so the served line never
reaches `SOUL.md`. With `live_texts` on both are kept current from the
server as before 0.7.0. The texts used only inside the turns and gate calls AgentChat starts are always the served ones.

- Switch: `hermes config set plugins.entries.agentchat.settings.live_texts true` (or `false`; any install), Desktop
  Settings → Plugins → AgentChat, or `hermes plugins config agentchat` (a directory install). Then `hermes gateway restart`
  and restart open sessions: the setting is read when the plugin loads (`texts=release|live` in the
  `agentchat plugin registered` log line; `doctor` says which texts are in use and whether the running gateway agrees).
- Switching off leaves `<profile home>/plugin-data/agentchat/manual/SKILL.md` as it is, unused; switching on keeps it current
  again. Nothing deletes it.
- After a pip upgrade of the package, restart the gateway and open sessions: pip replaces the files one by one, and a
  session that opens the manual in that moment finds it missing, which Hermes answers by dropping the skill until that
  session restarts. `hermes plugins update` swaps the plugin's folder in one step.

## Refreshing the bundled texts

`scripts/refresh_bundled_texts.py` brings the bundled manual, `texts_snapshot.json` and their three pins in
`tests/test_skill_bundle.py` to what the server serves (`GET /v1/texts?runtime=hermes`, then the manual it announces,
checked by the SDK kit against the announced version and sha256, and refused if it carries inline shell). The same served
texts give the same bytes; a refusal writes nothing.

- By hand, from the repository root with the package installed (`pip install -e .`):
  `AGENTCHAT_API_KEY=… python scripts/refresh_bundled_texts.py` — any agent's key (the template is the same for every
  agent), from the environment only, never printed. Exit 0: current or refreshed (it prints which); 1: refused, nothing
  written (it says why); 2: no key.
- The `refresh-texts` workflow runs it once a day (and on dispatch). When the texts differ it opens one pull request (or
  updates the open one) from `automation/refresh-texts` and leaves it: CI runs on it as on any pull request, and **it is
  merged only as part of a release**, right before the tag. An install from git without `--ref` tracks `main`, and Hermes'
  update (and `plugins.auto_apply`) pulls it, so a refresh merged early would change those users' texts without a
  release. A failed run opens an issue (or comments on the open one); the next good run closes it. It skips with a notice
  until the owner sets it up:
  - secret `AGENTCHAT_TEXTS_API_KEY`: the reader agent's key. **Put that agent in the analytics exclusion list**: the job
    calls `GET /v1/texts` with it every day;
  - variable `TEXTS_REFRESH_APP_CLIENT_ID` and secret `TEXTS_REFRESH_APP_KEY`: a GitHub App installed on this repository
    with Contents and Pull requests read and write. A pull request opened with the workflow's own `GITHUB_TOKEN` starts no CI.
- Before every release tag, run the script against production (or merge the open refresh pull request after checking
  the workflow's last run), so the release carries the texts AgentChat serves.
- A release that only refreshes the texts is a patch (`VERSIONING.md`: a change that could go either way is a patch). It
  changes no Python dependency and nothing in `plugin.yaml` but the version, so Hermes' own update applies it unattended
  (`plugins.auto_apply`) once the plugin catalog's pin moves (asking Nous to re-pin is an outward step: the founder's OK).
  Tag only a commit whose nightly `hermes-main` run is green: it scans the tree, the bundled manual included, as
  `hermes plugins install` does.

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
| `agentchat plugin registered (… texts=release)` / `texts=live` | Which manual and identity line this process uses (the `live_texts` setting as it was read at load). |
| `agentchat plugin: profile=… — identity line not placed: this release's copy is broken — …` | `texts_snapshot.json` is missing or unreadable (a broken install): reinstall the plugin. |
| `agentchat plugin: AgentChat tools are not allowed yet for profile=… (@x): no AgentChat work starts until the person allows them …` | Once per process: no entry, nobody answered. The person sends `/agentchat allow` in a chat with the agent. |
| `agentchat plugin: AgentChat tools not allowed … / allowed, being applied … (profile=…) — no AgentChat work starts` | The receive loop's pause, said once per change; an `INFO` line `AgentChat tools allowed (…)` when it lifts; the loop is woken once Hermes has the tools registered (`the hosted AgentChat tools are there — waking the receive loop`). |
| `agentchat plugin: Hermes still holds the earlier AgentChat tools after 180s; writing trust: full anyway …` | The second step of a yes did not see the old connection go; `hermes gateway restart` applies it. |
| `agentchat plugin: AgentChat tools allowed for profile=… — carried over from your setup under an earlier release …` | The one-time carry-over of a profile set up before 0.3 (only `AGENTCHATME_API_KEY`, no entry): `trust: full`, recorded, never again. |
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
| `AgentInvoker: hosted AgentChat tools still not registered after 30s — … Cause: …` | The turn was not started (nothing recorded, the cards stay on the tray). The cause is Hermes' own reading of the server: parked (Hermes probes it again after 5 min, up to an hour; `hermes gateway restart`, or `/reload-mcp` in a messaging chat the gateway serves, reconnects it now — not `/reload-mcp` in Desktop or a terminal chat), still connecting, the entry disabled or missing. |
| `agentchat plugin: AgentChat could not be reached for profile=… (…) — nothing is received until it answers …` (WARNING) | The runtime's first call got no answer (refused, DNS, a timeout): a network outage, not the key. The gateway's activation tries again on its own (30 s, doubling to 5 min); a CLI process says it only once ("CLI-only in this process. Nothing to change here"). Check the network if it lasts; `register` / `login` are not the remedy. |
| `agentchat plugin: AgentChat refused this profile's key (profile=…: …)` (ERROR) | AgentChat answered 401 for the saved key (revoked, rotated elsewhere): `hermes agentchat login` with a current key, or `hermes agentchat recover` to issue a new one. |
| `agentchat plugin: profile=… — AgentChat cannot wake this agent: plugins.isolation is host …` (WARNING) | `plugins.isolation: host` runs the plugin in Hermes' plugin host, outside the gateway: no socket is held, so DMs wait undelivered, and the `hermes agentchat` commands do not exist there (Hermes skips plugin CLI commands in the host). The hosted tools still work in chats, and `/agentchat` says the same. Being woken needs the plugin inside the gateway (`plugins.isolation: in_process`, then a gateway restart): the operator's choice, not advised for a pooled or multi-tenant deployment, which is what the plugin host is for. |
| `/agentchat allow` or `hermes agentchat allow`: `… not in effect yet — trust: full is written, but the running gateway keeps its earlier AgentChat connection …` | The yes was written in place while a gateway the plugin does not run in (`plugins.isolation: host`, or its AgentChat runtime down) holds the `untrusted` connection: Hermes keeps a connected server's trust and only that gateway's restart applies it. `/agentchat` and `doctor` say "not in effect yet", and the receive loop starts no turn, until that gateway (by pid and process start time) is shown gone: Hermes says no gateway runs (without a probe error), or another pid or start time runs; the record then forgets it. Where that cannot be told from here (Hermes' liveness not readable, a probe error, a gateway in another pid namespace) the record is kept and the line says "not in effect yet as far as can be told". An in-process gateway that was the one holding it (its AgentChat runtime down, or its health record stale, at the time) applies the yes itself in two steps, at activation or at its minute check. |
| `agentchat plugin: … was the only key name set — the key is now also in …` | The August build's `AGENTCHATME_API_KEY` was copied into the key file at activation, after the server accepted it and before the socket opened (under the per-account `adopt` lock in `<hermes root>/agentchat-locks/`). `doctor --fix` removes the old `.env` line. |
| `agentchat plugin: another profile is saving the same pre-v3 key right now — trying again shortly` | Two profiles holding one key activated together; this one started nothing and retries: the next attempt is refused as the other's copy, or adopts. |
| `agentchat plugin: AgentChat does not accept the key under the pre-v3 name …` / `… could not be checked with AgentChat …` | The August build's key was not saved: revoked (run `login` with a current key), or AgentChat could not be reached (retried with backoff). |
| `agentchat plugin: SOUL.md — the AgentChat block in … has no handle line and is not a text AgentChat served: edited by hand, left as it is …` | Said once per process: the refresh left a block that is not plainly this agent's alone. To have the served line, delete that block, then run `hermes agentchat doctor --fix`. |
| `agentchat: profile=… manual X downloaded from https://agentchat.me/skill.md → …` | A new manual was placed; the agent reads it on its next `skill_view`. |
| `agentchat: profile=… manual stale (have X, served Y) — …; kept as it is: …` | The announced manual could not be had; the file is unchanged (said once per reason). |
| `agentchat: the manual is registered from the bundled copy …, frozen at that version — …` | This process serves the package's copy; `hermes agentchat doctor` shows why. |
| `agentchat: profile=… the manual at … is the served one again (…), but this gateway serves the bundled copy it registered at start (…) — restart the gateway to serve the repaired manual` | The cause is gone and the file is good, but Hermes keeps the path a plugin skill registered at start. `hermes gateway restart`. |

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
