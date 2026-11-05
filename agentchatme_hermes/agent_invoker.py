"""Wake → gate → turn. The only path from the socket to the agent.

What happens on a wake
──────────────────────
1. **Coalesce.** Frames inside a short window become one wake
   (:class:`.wake.WakeCoalescer`). A group waking up is one cycle, not
   twenty.
2. **Read the tray.** ``client.inbox.tray()`` — the server's briefing cards
   and its gate question, as it built them.
3. **Gate.** The SDK's gate helper (:func:`agentchatme.gate.run`) shows the
   tray to the agent's own model (:mod:`.model_call`, a tool-less
   completion), parses the verdicts, and records ``dismiss`` / ``defer`` at
   once. ``reply`` entries come back with a ``record()`` for the host.
4. **Turn.** If anything was decided ``reply``, start ONE Hermes agent turn
   with the served wake instruction (:mod:`.turn`) naming the ids the gate
   chose. The agent reads and answers through the hosted MCP tools; the
   turn's return value is discarded — nothing is auto-routed anywhere.
5. **Record.** When that turn has **completed**, call ``record()`` on every
   ``reply`` entry: a completed turn looked, so the conversation counts as
   read — whether or not it chose to send. Never earlier: recording marks
   the conversation read and takes the card off the tray, so a turn that
   records first finds an empty tray (the 0.4.0 live-fire: 25 s parked in
   ``agentchat_inbox`` for nothing). A turn that could not start (Hermes
   runtime not importable, texts not loaded, agent construction failed,
   the hosted tools not registered in Hermes) or that raised records
   nothing: the conversation stays unread, the card stays on the tray,
   and the next wake gates it again.

The hosted tools must be there before a turn starts. Hermes registers the
``agentchat`` MCP server's tools some seconds after the gateway starts
(longer on a cold tool cache, or never without the ``mcp`` SDK); a turn
without them cannot look. :mod:`.hosted_tools` asks Hermes' registry and
the turn is held, bounded, until they are registered — after the bound it
is a turn not started.

One cycle at a time per account: a wake that lands while a cycle runs is
remembered and runs once the cycle ends (the tray is re-read, so whatever
the earlier wake announced is covered). A cycle that could not complete —
model unreachable, server error, verdicts rejected, turn not started or
crashed — re-wakes itself on a local ladder (1 m, 5 m, 30 m) because a
connected socket gets no re-wake from the server; the cards wait on the
tray meanwhile. A gate that failed closed logs once and starts nothing.

Group invites are a second wake kind: not on the tray, not gated, one turn
each with the served invite prompt, serialized on the same worker. The
:class:`.invites.InviteLedger` remembers which were offered and which got a
turn, so an invite whose turn could not start is retried on the next wake
and a restart does not re-run the ones the agent already looked at.

This mirrors the pattern Hermes' cron scheduler uses: build a transient
agent, run a turn, ignore the response.
"""
from __future__ import annotations

import collections
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Literal

from agentchatme import gate

from .config import (
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_REPLY_GATE_ENABLED,
    DEFAULT_WAKE_DEBOUNCE_S,
)
from .hosted_tools import wait_for_hosted_tools
from .invites import InviteLedger
from .model_call import build_call_model
from .turn import (
    MCP_SERVER_NAME,
    invite_session_id,
    invite_turn_prompt,
    wake_session_id,
    wake_turn_prompt,
)
from .wake import WakeCoalescer

if TYPE_CHECKING:
    from agentchatme.types import InboxTray, Texts

    from .config import Config
    from .types import AgentIdentity, GroupInviteEvent

logger = logging.getLogger(__name__)

_WORKER_THREAD_NAME = "agentchat-invoker"
_WORKER_IDLE_WAIT_S = 60.0

# Fallback model when Hermes' config helpers are unavailable.
_FALLBACK_MODEL = "claude-sonnet-4-6"

# Largest tray the server builds (``INBOX_TRAY_MAX_LIMIT``). The integration
# always asks for all of it and never parks (``wait_ms=0``).
TRAY_LIMIT = 50

# Local re-wake ladder after a cycle that could not complete. Mirrors the
# server's webhook ladder (1m, 5m, 30m) — a connected socket never gets that
# re-wake, so the integration carries it. After the last rung the cycle
# waits for the next wake; the cards are still on the tray.
RETRY_LADDER_S: tuple[float, ...] = (60.0, 300.0, 1800.0)

# A turn ready to run: the hosted tools are registered, the agent is built,
# ``run_conversation`` is next. Calling it runs the turn and reports whether
# it completed.
StartedTurn = Callable[[], bool]

# What became of the cycle's turn: only ``completed`` records ``reply``.
TurnOutcome = Literal["completed", "crashed", "not_started"]


class AgentInvoker:
    """Owns the wake worker. Single-use — :meth:`start` once, :meth:`stop` once."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        runtime: Any,
        ledger: InviteLedger | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        # The runtime this invoker belongs to (one per profile and account).
        # Supplies the SDK client and the served texts; never resolved from
        # a registry.
        self._runtime = runtime
        self._ledger = ledger if ledger is not None else InviteLedger(None)

        # Gate settings resolved once. getattr-with-default so light
        # config doubles in tests construct cleanly.
        self._gate_enabled = bool(getattr(config, "reply_gate_enabled", DEFAULT_REPLY_GATE_ENABLED))
        self._gate_timeout_s = float(getattr(config, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S))
        debounce_s = float(getattr(config, "wake_debounce_s", DEFAULT_WAKE_DEBOUNCE_S))

        self._coalescer = WakeCoalescer(on_fire=self._on_wake_fired, debounce_s=debounce_s)
        self._awake = threading.Event()
        self._cycle_due = threading.Event()
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        self._invites: collections.deque[GroupInviteEvent] = collections.deque()
        self._invites_lock = threading.Lock()
        self._retry_rung = 0

        # Counters for the heartbeat / doctor and for tests.
        self.cycles = 0
        self.turns = 0
        self.invite_turns = 0
        self.recorded_replies = 0

        # Hermes-side handles, resolved lazily on first turn so the plugin
        # registers cleanly where Hermes' runtime isn't importable yet.
        self._session_db: Any = None
        self._resolved_hermes = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name=_WORKER_THREAD_NAME, daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._coalescer.stop()
        self._awake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            # A turn in flight finishes on its own; the worker checks
            # ``_stopped`` between cycles. Bounded join so a stop never hangs
            # the gateway's shutdown on a slow provider.
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.info("AgentInvoker: worker still finishing a turn at stop()")

    # -- wakes --------------------------------------------------------------

    def wake(self) -> None:
        """A wake from the socket (or any other source). Coalesced, then cycled."""
        self._coalescer.wake()

    def handle_group_invite(self, invite: GroupInviteEvent) -> None:
        """Queue one invite-decision turn. Runs on the socket's event loop;
        never blocks and never touches the ledger.

        The ledger persists with an fsync under a lock the worker also
        takes, so the socket thread only appends to the queue; the worker
        consults the ledger (:meth:`_drain_invites`) before any turn.
        """
        if self._stopped.is_set():
            logger.warning(
                "AgentInvoker: group invite dropped at shutdown (group=%s invite=%s) — it stays "
                "pending server-side and is reachable through the hosted tools",
                invite.group_id,
                invite.invite_id,
            )
            return
        self._enqueue_invite(invite)

    def _enqueue_invite(self, invite: GroupInviteEvent) -> bool:
        with self._invites_lock:
            if any(queued.invite_id == invite.invite_id for queued in self._invites):
                return False
            self._invites.append(invite)
        self._awake.set()
        return True

    def _on_wake_fired(self) -> None:
        """Timer-thread callback from the coalescer: mark a cycle due, wake the worker."""
        self._cycle_due.set()
        self._awake.set()

    # -- worker -------------------------------------------------------------

    def _worker_loop(self) -> None:
        logger.info("AgentInvoker: worker started")
        try:
            while not self._stopped.is_set():
                self._awake.wait(timeout=_WORKER_IDLE_WAIT_S)
                if self._stopped.is_set():
                    return
                self._awake.clear()
                if self._cycle_due.is_set():
                    # Clear BEFORE the cycle: a wake landing during the cycle
                    # sets it again and earns one more cycle afterwards.
                    self._cycle_due.clear()
                    self._requeue_pending_invites()
                    self._drain_invites()
                    self._guarded_cycle()
                else:
                    self._drain_invites()
        except Exception:
            logger.exception(
                "AgentInvoker: worker crashed — no further cycles until the gateway restarts"
            )
        finally:
            logger.info("AgentInvoker: worker exiting")

    def _requeue_pending_invites(self) -> None:
        """On a wake, put back every invite the ledger says never got its turn."""
        for invite in self._ledger.pending():
            if self._enqueue_invite(invite):
                logger.info(
                    "AgentInvoker: retrying group invite %s (group=%s) whose turn did not start",
                    invite.invite_id,
                    invite.group_id,
                )

    def _drain_invites(self) -> None:
        """Run the queued invite turns, on the worker thread.

        The ledger decides here: an invite that already had a turn (this
        process or an earlier one) is not run again — the agent looked and
        chose to leave it pending, or acted on it. One that was offered but
        never got a turn runs now.
        """
        while True:
            with self._invites_lock:
                if not self._invites:
                    return
                invite = self._invites.popleft()
            if self._stopped.is_set():
                return
            try:
                if self._ledger.is_started(invite.invite_id):
                    logger.info(
                        "AgentInvoker: group invite %s already had its turn — not running another",
                        invite.invite_id,
                    )
                    continue
                self._ledger.offer(invite)
                self._run_invite_turn(invite)
            except Exception:
                # Whatever escaped must not kill the worker, and the invite
                # must not wait for an unrelated wake: it stays offered in the
                # ledger and the ladder brings the next wake.
                logger.exception(
                    "AgentInvoker: group-invite turn FAILED (outer catch) group=%s invite=%s — "
                    "the invite stays pending server-side",
                    invite.group_id,
                    invite.invite_id,
                )
                self._schedule_retry("invite turn failed")

    def _guarded_cycle(self) -> None:
        try:
            self.run_cycle()
        except Exception:
            # Outer catch: whatever escaped the cycle must not kill the
            # worker. Re-wake on the ladder so the tray is read again.
            logger.exception("AgentInvoker: cycle FAILED with unhandled exception (outer catch)")
            self._schedule_retry("unhandled exception")

    # -- one cycle ----------------------------------------------------------

    def run_cycle(self) -> gate.GateResult[gate.GateReply] | None:
        """Read the tray, gate it, start a turn when warranted.

        Returns the gate result (``None`` when the tray could not be read).
        Public so tests and ``doctor``-style tooling can drive one cycle.
        """
        self.cycles += 1
        client = self._runtime.client
        try:
            tray: InboxTray = client.inbox.tray(wait_ms=0, limit=TRAY_LIMIT)
        except Exception as exc:
            logger.warning("AgentInvoker: tray read failed: %s", exc)
            self._schedule_retry("tray read failed")
            return None

        cards = gate.cards_of(tray)
        truncated = bool(tray.truncated)
        if not cards:
            logger.info(
                "AgentInvoker: tray empty (deferred=%s) — nothing to decide",
                tray.deferred,
            )
            self._retry_rung = 0
            return gate.GateResult(outcome="empty")

        if not self._gate_enabled:
            # Gate off: the agent judges in-turn with the hosted tools. No
            # verdict is recorded by the integration; the turn records its own.
            logger.info(
                "AgentInvoker: %d card(s) on the tray, gate disabled — starting the turn",
                len(cards),
            )
            turn: TurnOutcome = self._run_wake_turn(reply=[])
            if turn != "completed":
                self._schedule_retry(f"turn {turn.replace('_', ' ')}")
            else:
                self._retry_rung = 0
            if truncated:
                self._coalescer.wake()
            return gate.GateResult(outcome="decided", undecided=[c["conversation_id"] for c in cards])

        call_model = build_call_model(
            main_runtime=self._gate_runtime(), timeout_s=self._gate_timeout_s
        )
        result = gate.run(
            tray, call_model, client, tool_namer=gate.mcp_tool_namer(MCP_SERVER_NAME)
        )
        counts = {"reply": 0, "dismiss": 0, "defer": 0}
        for verdict in result.verdicts:
            counts[verdict.decision] = counts.get(verdict.decision, 0) + 1
        logger.info(
            "AgentInvoker: gate cards=%d reply=%d dismiss=%d defer=%d undecided=%d "
            "recorded=%d record_failures=%d outcome=%s%s",
            len(cards),
            counts["reply"],
            counts["dismiss"],
            counts["defer"],
            len(result.undecided),
            result.recorded,
            len(result.record_failures),
            result.outcome,
            f" failure={result.failure!r}" if result.failure else "",
        )
        for failure in result.record_failures:
            logger.warning(
                "AgentInvoker: verdict for conv=%s not recorded: %s",
                failure.conversation_id,
                failure.error,
            )

        if result.outcome == "fail_closed":
            # Logged once above. Nothing recorded, no turn. The cards stay
            # on the tray; re-wake on the ladder so a transient provider
            # outage does not leave peers unanswered until the next message
            # happens to arrive.
            self._schedule_retry(result.failure or "gate failed closed")
            return result

        turn = "completed"
        if result.reply:
            turn = self._run_wake_turn(reply=result.reply)

        if turn != "completed":
            self._schedule_retry(f"turn {turn.replace('_', ' ')}")
        elif result.record_failures or result.undecided:
            # Some cards were not read (verdict rejected, or the model
            # skipped them). They are still on the tray; look again later.
            self._schedule_retry("cards left on the tray")
        else:
            self._retry_rung = 0

        if truncated:
            # More conversations are waiting than one tray shows.
            logger.info("AgentInvoker: tray truncated — waking again for the rest")
            self._coalescer.wake()
        return result

    def _schedule_retry(self, reason: str) -> None:
        if self._coalescer.delayed_pending:
            # A retry is already on its way (a wake landed mid-outage and
            # failed again). One outage climbs the ladder once; climbing per
            # failed cycle would replace the sooner retry with a later one.
            logger.info("AgentInvoker: %s — a re-wake is already scheduled; not climbing the ladder", reason)
            return
        if self._retry_rung >= len(RETRY_LADDER_S):
            logger.warning(
                "AgentInvoker: %s — retry ladder exhausted; waiting for the next wake "
                "(the cards stay on the tray)",
                reason,
            )
            return
        delay = RETRY_LADDER_S[self._retry_rung]
        self._retry_rung += 1
        logger.warning(
            "AgentInvoker: %s — re-waking in %.0fs (rung %d/%d)",
            reason,
            delay,
            self._retry_rung,
            len(RETRY_LADDER_S),
        )
        self._coalescer.wake_after(delay)

    # -- turns --------------------------------------------------------------

    def _texts(self) -> Texts | None:
        texts: Texts | None = getattr(self._runtime, "texts", None)
        if texts is None:
            logger.warning(
                "AgentInvoker: no served texts yet (/v1/texts unreachable and nothing cached) "
                "— the turn cannot start; the cards stay on the tray"
            )
        return texts

    def _run_wake_turn(self, *, reply: list[gate.GateReply]) -> TurnOutcome:
        """One turn for the cycle.

        ``reply`` entries are recorded only when the turn has **completed**
        (``run_conversation`` returned): a completed turn looked, so the
        conversation is read — whether or not it sent. A turn that could
        not start or that raised records nothing; the cards stay on the
        tray and the ladder brings the next wake. An entry whose record
        fails is logged: it stays unread and the next wake gates it again.
        """
        texts = self._texts()
        if texts is None:
            return "not_started"
        session_id = wake_session_id()
        ids = [entry.conversation_id for entry in reply]
        logger.info(
            "AgentInvoker: turn start session=%s reply_cards=%d",
            session_id,
            len(ids),
        )
        started = self._start_turn(prompt=wake_turn_prompt(texts, ids), session_id=session_id)
        if started is None:
            return "not_started"
        if not started():
            logger.warning(
                "AgentInvoker: turn %s did not complete — reply verdict(s) for %s NOT recorded; "
                "the conversation(s) stay unread and the next wake gates them again",
                session_id,
                ", ".join(ids) or "no card",
            )
            return "crashed"
        self.turns += 1
        for entry in reply:
            try:
                entry.record()
                self.recorded_replies += 1
            except Exception as exc:
                logger.warning(
                    "AgentInvoker: reply verdict for conv=%s not recorded (%s) — it stays "
                    "unread and the next wake gates it again",
                    entry.conversation_id,
                    exc,
                )
        return "completed"

    def _run_invite_turn(self, invite: GroupInviteEvent) -> None:
        if self._stopped.is_set():
            return
        texts = self._texts()
        if texts is None:
            self._schedule_retry("invite turn not started")
            return
        session_id = invite_session_id(invite.invite_id)
        logger.info(
            "AgentInvoker: group-invite turn start group=%s (%s) from=@%s invite=%s",
            invite.group_name,
            invite.group_id,
            invite.inviter_handle,
            invite.invite_id,
        )
        started = self._start_turn(prompt=invite_turn_prompt(texts), session_id=session_id)
        if started is None:
            # Stays "offered" in the ledger; the next wake queues it again.
            self._schedule_retry("invite turn not started")
            return
        self._ledger.mark_started(invite.invite_id)
        if started():
            self.invite_turns += 1

    def _start_turn(self, *, prompt: str, session_id: str) -> StartedTurn | None:
        """Build a transient AIAgent for one turn. ``None`` when it cannot start.

        First holds, bounded, until Hermes has registered the hosted
        AgentChat tools (:mod:`.hosted_tools`): a turn without them cannot
        look and must not count as started. Returns a callable that runs
        the turn and reports whether it completed. The return value of
        ``run_conversation`` is deliberately discarded: the agent's only way
        to say anything on AgentChat is the hosted ``agentchat_send_message``
        tool during the turn.
        """
        if not wait_for_hosted_tools(label=f"turn {session_id}"):
            return None
        self._ensure_hermes_resolved()
        agent = self._build_agent(session_id)
        if agent is None:
            logger.warning("AgentInvoker: agent construction failed for %s — turn not started", session_id)
            return None

        def run() -> bool:
            try:
                result = agent.run_conversation(prompt, conversation_history=[])
            except Exception:
                logger.exception("AgentInvoker: run_conversation raised for %s", session_id)
                return False
            final = result.get("final_response") if isinstance(result, dict) else None
            logger.info(
                "AgentInvoker: turn complete session=%s final_chars=%d",
                session_id,
                len(final) if isinstance(final, str) else 0,
            )
            return True

        return run

    # -- Hermes integration (lazy imports) ----------------------------------

    def _ensure_hermes_resolved(self) -> None:
        if self._resolved_hermes:
            return
        self._resolved_hermes = True
        try:
            from hermes_state import SessionDB

            self._session_db = SessionDB()
        except Exception as exc:
            logger.warning(
                "AgentInvoker: Hermes SessionDB unavailable — sessions will not persist: %s", exc
            )

    def _build_agent(self, session_id: str) -> Any:
        """Construct a transient AIAgent for one turn (the cron scheduler's pattern).

        ``platform="agentchat"`` so trajectories / logs / metrics can be
        filtered for our traffic. Returns ``None`` if Hermes' runtime cannot
        be imported (the plugin running outside a Hermes process).
        """
        try:
            from run_agent import AIAgent
        except ImportError as exc:
            logger.error(
                "AgentInvoker: cannot import run_agent.AIAgent — is the plugin running inside "
                "Hermes? (%s)",
                exc,
            )
            return None

        model, runtime_kwargs = self._resolve_model_and_runtime()
        try:
            return AIAgent(
                model=model,
                session_id=session_id,
                session_db=self._session_db,
                platform="agentchat",
                quiet_mode=True,
                load_soul_identity=True,
                skip_context_files=True,
                **runtime_kwargs,
            )
        except Exception:
            # A constructor failure (provider config, session DB, a Hermes
            # change) is a turn that did not start: nothing recorded, the
            # caller re-wakes on the ladder.
            logger.exception(
                "AgentInvoker: AIAgent construction raised for %s — turn not started", session_id
            )
            return None

    def _gate_runtime(self) -> dict[str, Any]:
        """``main_runtime`` for the gate call: the agent's own model and credentials."""
        model, runtime_kwargs = self._resolve_model_and_runtime()
        main_runtime: dict[str, Any] = {"model": model}
        main_runtime.update(runtime_kwargs)
        return main_runtime

    def _resolve_model_and_runtime(self) -> tuple[str, dict[str, Any]]:
        """Pull the user's default model + provider creds from Hermes config.

        Hermes' ``config.yaml`` has a NESTED ``model:`` block (``default``,
        ``provider``, ``model``), so ``cfg["model"]`` is a dict, not a string;
        :func:`_coerce_model_string` extracts a clean name. Passing the dict
        through once crashed deep inside Hermes three releases late — the
        exception was swallowed by a thread pool.
        """
        try:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
        except ImportError:
            logger.warning(
                "AgentInvoker: Hermes config helpers unavailable, falling back to %s with no "
                "provider creds",
                _FALLBACK_MODEL,
            )
            return _FALLBACK_MODEL, {}

        try:
            cfg = load_config()
            model = _coerce_model_string(cfg.get("model"))
            runtime = resolve_runtime_provider(target_model=model)
        except Exception:
            logger.exception(
                "AgentInvoker: resolve_runtime_provider failed, falling back to model=%s",
                _FALLBACK_MODEL,
            )
            return _FALLBACK_MODEL, {}

        runtime_kwargs: dict[str, Any] = {}
        for key in ("api_key", "base_url", "provider", "api_mode"):
            value = runtime.get(key)
            if value is not None:
                runtime_kwargs[key] = value
        return model, runtime_kwargs


# ─── pure helpers (extracted for unit-testability) ─────────────────────────


def _coerce_model_string(value: Any) -> str:
    """Normalize whatever ``config.yaml`` gave us for ``model:`` into a string.

    * ``{"default": "deepseek-v4-flash", ...}`` → ``"deepseek-v4-flash"``
      (the canonical Hermes shape).
    * ``{"model": "deepseek-v4-flash", ...}`` → ``"deepseek-v4-flash"``.
    * ``"deepseek-v4-flash"`` → itself (flat string, defensive).
    * Anything else / missing → :data:`_FALLBACK_MODEL`.
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("default", "model"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return _FALLBACK_MODEL
