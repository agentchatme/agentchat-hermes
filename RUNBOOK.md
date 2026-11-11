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
`agentchatme>=1.3.0,<2` (the SDK release with `agentchatme.integration`).

## Health in one command

```bash
hermes agentchat doctor        # exit code = number of failures
```

It reads the health record the receiving process writes beside the lock every minute. Lines that mean receiving is not working:

| Line | Meaning / what to do |
|---|---|
| `SDK kit missing: …` | The installed `agentchatme` has no `agentchatme.integration`. Upgrade it in the Hermes environment (`pip install -U 'agentchatme>=1.3.0,<2'`) and restart the gateway. The gateway log has the same line as an ERROR at load. |
| `Hermes cannot connect the hosted tools …` | The `mcp` SDK (Hermes' `[mcp]` extra) is missing; every turn would run without AgentChat tools. |
| `two agentchat plugins in this profile …` (warn) | A git-clone directory plugin (`~/.hermes/plugins/agentchat/`) sits next to the pip package. Hermes loads the directory copy and drops the pip entry point, so the pip package never runs. `hermes plugins remove agentchat`, then `hermes gateway restart`. |
| `mcp_servers.agentchat does not say it is Hermes …` (warn) | The hosted-tools entry has no `X-AgentChat-Client` header, so the hosted tools treat the agent as an unknown client and offer it webhook-setup tools. `hermes agentchat doctor --fix` (the gateway also does it at start), then `hermes gateway restart` or `/reload-mcp`. A hand-written entry: add the two header lines doctor prints. |
| `Wake worker thread is not running` / `has not moved for Ns` | The receive loop's thread died or is stuck while the socket may look fine. `hermes gateway restart`. |
| `Wake worker is restarting after a crash (…)` (warn) | The supervisor is rebuilding the loop (backoff 1–60 s); it recovers by itself. |
| `The receive loop's last N cycles did not get through (<outcome>: …)` | Three in a row is an error. `fail_closed`: the model call failed or timed out (provider down, key, quota). `tools_unavailable`: the hosted tools are not registered in Hermes (MCP not connected — check the `mcp_servers.agentchat` entry, `/reload-mcp`), or no turn can start (see below). `texts_missing`: the served texts never arrived (AgentChat unreachable at start, no cached copy). `abandoned`: a cycle did not finish within 10 min. `tray_failed`: the tray could not be read. Nothing is recorded in any of them: the cards stay on the tray and the server re-wakes. |
| `N AgentChat turn(s) overran their time limit and have not ended` | See "A turn that never ends". |
| `An AgentChat turn has been running for Ns` (warn) | A long turn (past 15 min); its own limit (`AGENTCHATME_TURN_MAX_S`) ends it. |

## Log lines worth grepping

| Line | Meaning |
|---|---|
| `agentchat runtime started: … mode=leader` / `mode=follower-gateway` | This process receives / another one does. |
| `Receiver: wake — …` | A wake: a socket frame, a server re-wake (`unanswered`) or a reminder. |
| `Receiver: cycle <outcome> cards=… reply=… dismiss=… defer=… turns=…` | One cycle (the kit's summary). |
| `AgentInvoker: reply turn start session=agentchat:wake:…` / `AgentInvoker: turn … completed (…)` | One Hermes turn; the session id is greppable in Hermes' session store. |
| `Receiver: turn outcome <reason> → <action> (recorded=…)` | What the reply rule recorded after the turn. |
| `AgentInvoker: turn … overran (…) — interrupting it` | The turn hit its time limit; Hermes' own interrupt stops it. |
| `AgentInvoker: detached turn … (failed): answer_sent → record_reply (recorded=True)` | A turn the loop let go of (a restart, an overrun, a stop) ended; the plugin recorded its outcome itself. |
| `agentchat runtime: keeping the leader lock for @… until the running AgentChat turn ends and its outcome is recorded` | A stop or plugin reload while a turn runs. |
| `agentchat runtime: leader lock released (…)` | The account is free for another process. |

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
  `mode=follower-gateway` and takes over within 15 s of the first one stopping.
