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
   completion) with the served gate-call words, parses the verdicts, and
   records ``dismiss`` / ``defer`` at once. ``reply`` entries come back to
   the host.
4. **Turn.** If anything was decided ``reply``, start ONE Hermes agent turn
   with the served wake instruction (:mod:`.turn`) naming the ids the gate
   chose. The agent reads and answers through the hosted MCP tools; the
   turn's return value is discarded — nothing is auto-routed anywhere. The
   turn runs under a time limit (:mod:`.turn_runner`): no activity for
   ``AGENTCHATME_TURN_IDLE_TIMEOUT_S`` (600 s) or a run past
   ``AGENTCHATME_TURN_MAX_S`` (3600 s) interrupts it with Hermes' own stop,
   and it counts as failed. Its conversations stay held until its thread has
   really ended, so no second turn starts for them meanwhile.
5. **Record.** After the turn, what is recorded for each ``reply`` card is
   the kit's reply rule (``agentchatme.integration.turn_outcome``), decided
   from one fresh tray read and the turn's AgentChat call counts — never
   from "the turn ran". A turn that failed, overran or is still running →
   nothing. The turn recorded its own verdict (the served texts tell the
   model to record ``reply`` after its send, or ``dismiss`` / ``defer``) →
   nothing more, so the agent's own verdict stands. The answer went out →
   ``reply`` through the anchor the gate saw. No AgentChat call, or only
   failed ones → nothing (it could not look). It looked and did nothing →
   nothing the first time, ``dismiss`` (``no_action_needed``) the second
   time for the same anchor. A failed tray read records nothing. Never
   before the turn ends: recording marks the conversation read and takes
   the card off the tray, so a turn that records first finds an empty tray
   (the 0.4.0 live-fire: 25 s parked in ``agentchat_inbox`` for nothing). A
   turn that could not start (Hermes runtime not importable, texts not
   loaded, agent construction failed, the hosted tools not registered in
   Hermes) records nothing: the card stays on the tray and the next wake
   gates it again.

The hosted tools must be there before a turn starts. Hermes registers the
``agentchat`` MCP server's tools some seconds after the gateway starts
(longer on a cold tool cache, or never without the ``mcp`` SDK); a turn
without them cannot look. :mod:`.hosted_tools` asks Hermes' registry and
the turn is held, bounded, until they are registered — after the bound it
is a turn not started.

One cycle at a time per account: a wake that lands while a cycle runs is
remembered and runs once the cycle ends (the tray is re-read, so whatever
the earlier wake announced is covered). Because ``reply`` is recorded only
when the turn ends, the card stays on the tray for the whole turn; the
worker's serialisation is what keeps a second cycle from gating it again
mid-turn, and an explicit per-conversation in-flight set backs that up:
cards whose turn is running are dropped before the gate (never gated,
never recorded twice), the set is cleared when the turn ends — completed
or crashed — and a cycle that skipped a card re-reads the tray once the
turn is over, so a message newer than the anchor is not missed. A cycle that could not complete —
model unreachable, server error, verdicts rejected, turn not started or
crashed — leaves the cards on the tray, and the server re-wakes this socket
while they stay unanswered (contract v1 D3, the ``rewake`` capability); the
integration keeps no retry ladder. A gate that failed closed logs once and
starts nothing.

Group invites come on the same tray (contract v1 D4: ``tray.invites``,
pending and not dismissed by this agent). After the cards, each invite gets
one turn of its own with the served invite prompt (a few per cycle; the
rest on the next). What the host records after it is the kit's rule
(``agentchatme.integration.invite_outcome``): answered in the turn →
nothing; looked and left it pending → nothing the first time, ``dismiss``
(seen, left pending, not shown again) the second time; a turn that failed
or could not look → nothing, and the server re-wakes while the invite is
pending. Nothing is remembered on disk: the tray is the truth.

The worker is supervised: a worker loop that dies is restarted with backoff
(1 s doubling to 60 s) and reads the tray again; its state — alive, idle /
cycle / turn / restart_wait, a heartbeat, restarts, overrun turns still
running — is in the account's health record beside the socket's, and
``hermes agentchat doctor`` reports it.

This mirrors the pattern Hermes' cron scheduler uses: build a transient
agent, run a turn, ignore the response.
"""
from __future__ import annotations

import collections
import inspect
import logging
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from agentchatme import gate
from agentchatme.integration import (
    apply_invite_outcome,
    apply_outcome,
    invite_outcome,
    turn_outcome,
)

from .config import (
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_REPLY_GATE_ENABLED,
    DEFAULT_TURN_IDLE_S,
    DEFAULT_TURN_MAX_S,
    DEFAULT_WAKE_DEBOUNCE_S,
)
from .hosted_tools import wait_for_hosted_tools
from .model_call import build_call_model
from .profile_scope import (
    ProfileScopeError,
    profile_context,
    profile_turn_scope,
    resolve_hermes_home,
)
from .turn import (
    MCP_SERVER_NAME,
    invite_session_id,
    invite_turn_prompt,
    wake_session_id,
    wake_turn_prompt,
)
from .turn_runner import RunningTurns, TurnLimits, TurnReport, run_turn
from .wake import WakeCoalescer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentchatme.types import InboxTray, Texts
    from agentchatme.types.inbox import TrayInvite

    from .config import Config
    from .types import AgentIdentity
    from .ws_health import HealthBoard

logger = logging.getLogger(__name__)

_WORKER_THREAD_NAME = "agentchat-invoker"
_WORKER_IDLE_WAIT_S = 60.0

# Fallback model when Hermes' config helpers are unavailable.
_FALLBACK_MODEL = "claude-sonnet-4-6"

# Largest tray the server builds (``INBOX_TRAY_MAX_LIMIT``). The integration
# always asks for all of it and never parks (``wait_ms=0``).
TRAY_LIMIT = 50

# A turn ready to run: the hosted tools are registered, the agent is built,
# ``run_conversation`` is next. Calling it runs the turn under the time
# limit (:mod:`.turn_runner`) and reports what became of it.
StartedTurn = Callable[[], TurnReport]

# What became of the cycle's turn. What is recorded after a ``completed``
# one is the kit's reply rule (``agentchatme.integration.turn_outcome``).
TurnRun = Literal["completed", "crashed", "timeout", "not_started"]

# Turns per (conversation, anchor) that looked and did nothing, for the
# reply rule's second look (``silent_before``). Bounded; lost on restart,
# which gives the agent one more look, never a wrong verdict.
_SILENT_MEMORY = 512

# Worker supervision: a worker loop that dies is restarted after a backoff
# (1 s doubling to 60 s, ±20 % jitter); a loop that ran this long before it
# died starts the backoff over.
WORKER_BACKOFF_BASE_S = 1.0
WORKER_BACKOFF_MAX_S = 60.0
WORKER_STABLE_S = 300.0

# Turns that overran and did not end after the interrupt keep their thread.
# With this many still running, no new turn starts (the cards stay on the
# tray) until one of them ends: a wedged provider must not pile up threads.
MAX_HUNG_TURNS = 2

WorkerState = Literal["starting", "idle", "cycle", "turn", "restart_wait", "stopped"]

# Invite turns per cycle; the rest on the next cycle, so a burst of invites
# never holds the account's messages back for long.
MAX_INVITE_TURNS_PER_CYCLE = 3


class AgentInvoker:
    """Owns the wake worker. Single-use — :meth:`start` once, :meth:`stop` once."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        runtime: Any,
        health: HealthBoard | None = None,
        host_llm: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        # The runtime this invoker belongs to (one per profile and account).
        # Supplies the SDK client and the served texts; never resolved from
        # a registry.
        self._runtime = runtime

        # Gate settings resolved once. getattr-with-default so light
        # config doubles in tests construct cleanly.
        self._gate_enabled = bool(getattr(config, "reply_gate_enabled", DEFAULT_REPLY_GATE_ENABLED))
        self._gate_timeout_s = float(getattr(config, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S))
        debounce_s = float(getattr(config, "wake_debounce_s", DEFAULT_WAKE_DEBOUNCE_S))

        idle_s = float(getattr(config, "turn_idle_s", DEFAULT_TURN_IDLE_S))
        max_s = float(getattr(config, "turn_max_s", DEFAULT_TURN_MAX_S))
        self._limits = TurnLimits(idle_s=idle_s or None, max_s=max_s or None)

        self._coalescer = WakeCoalescer(on_fire=self._on_wake_fired, debounce_s=debounce_s)
        self._awake = threading.Event()
        self._cycle_due = threading.Event()
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        # Cycles that ended with cards still on the tray (diagnostics, tests).
        # Nothing here re-wakes for them: the server does (contract v1 D3).
        self.left_on_tray = 0
        self.last_left_reason: str | None = None
        # Conversations (and invites, ``invite:<id>``) whose turn is running.
        # The record happens when the turn ends, so until then the card is
        # still on the tray and must not be gated again. A turn that overran
        # stays here until its thread has really ended; then the tray is
        # read again.
        self._running = RunningTurns(on_released=self._coalescer.wake)

        # Worker state for the health record (``hermes agentchat doctor``).
        self._health = health
        self._state_lock = threading.Lock()
        self._state: WorkerState = "starting"
        self._state_since = time.time()
        self._heartbeat_at = time.time()
        self._last_error: str | None = None
        self._current_turn: dict[str, Any] | None = None
        self._last_turn: dict[str, Any] | None = None
        self.worker_restarts = 0
        # ``reply`` cards the last cycle's turn left on the tray (the reply
        # rule recorded nothing, or its record failed): the server re-wakes.
        self._cards_left_after_turn = 0
        self._silent_turns: collections.OrderedDict[tuple[str, str], int] = collections.OrderedDict()

        # Counters for the heartbeat / doctor and for tests.
        self.cycles = 0
        self.turns = 0
        self.invite_turns = 0
        self.recorded_replies = 0
        self.recorded_dismisses = 0
        # How many times each reply-rule reason came up (doctor, tests).
        self.outcomes: collections.Counter[str] = collections.Counter()

        # Hermes' public plugin LLM (``ctx.llm``), read when the gate needs it;
        # ``None`` on a Hermes without it (the gate then resolves the model
        # and credentials itself, :meth:`_gate_runtime`).
        self._host_llm = host_llm
        self._host_llm_warned = False

        # Hermes-side handles, resolved lazily on first turn so the plugin
        # registers cleanly where Hermes' runtime isn't importable yet.
        self._session_db: Any = None
        self._resolved_hermes = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._supervise, name=_WORKER_THREAD_NAME, daemon=True
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
            # A running turn is asked to stop (the turn runner watches
            # ``_stopped`` and interrupts it; it records nothing). Bounded
            # join so a stop never hangs the gateway's shutdown on a slow
            # provider.
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.info("AgentInvoker: worker still ending a turn at stop()")

    def in_flight(self) -> set[str]:
        """Conversation ids whose turn is running right now (diagnostics, tests)."""
        return {key for key in self._running.busy() if not key.startswith(_INVITE_KEY)}

    def status(self) -> dict[str, Any]:
        """The worker's state for the health record and ``doctor``.

        ``alive`` is the worker thread itself; ``heartbeat_at`` moves while
        the worker loop (or a turn's watch) is responsive, so a worker stuck
        anywhere shows a heartbeat that stops moving while the socket's own
        record stays fresh.
        """
        worker = self._worker
        with self._state_lock:
            return {
                "state": self._state,
                "since": self._state_since,
                "alive": bool(worker is not None and worker.is_alive()),
                "heartbeat_at": self._heartbeat_at,
                "restarts": self.worker_restarts,
                "last_error": self._last_error,
                "turn": dict(self._current_turn) if self._current_turn else None,
                "hung_turns": self._running.hung,
                "last_turn": dict(self._last_turn) if self._last_turn else None,
                "cycles": self.cycles,
                "turns": self.turns,
                "recorded_replies": self.recorded_replies,
                "recorded_dismisses": self.recorded_dismisses,
            }

    def _set_state(self, state: WorkerState, *, error: str | None = None) -> None:
        with self._state_lock:
            changed = state != self._state or error is not None
            if state != self._state:
                self._state = state
                self._state_since = time.time()
            self._heartbeat_at = time.time()
            if error is not None:
                self._last_error = error[-500:]
        if changed:
            self._publish_health()

    def _beat(self) -> None:
        with self._state_lock:
            self._heartbeat_at = time.time()

    def _publish_health(self) -> None:
        board = self._health
        if board is None:
            return
        try:
            board.refresh()
        except Exception:
            # Health is diagnostics; it never becomes a receive-path dependency.
            logger.debug("AgentInvoker: health record write failed", exc_info=True)

    def _skip_in_flight(self, tray: InboxTray) -> tuple[InboxTray, list[str]]:
        """``(tray without the cards whose turn is running, the ids dropped)``."""
        busy = self.in_flight()
        if not busy:
            return tray, []
        skipped = [c.conversation_id for c in tray.conversations if c.conversation_id in busy]
        if not skipped:
            return tray, []
        kept = [c for c in tray.conversations if c.conversation_id not in busy]
        return tray.model_copy(update={"conversations": kept}), skipped

    # -- wakes --------------------------------------------------------------

    def wake(self) -> None:
        """A wake from the socket (or any other source). Coalesced, then cycled."""
        self._coalescer.wake()

    def _on_wake_fired(self) -> None:
        """Timer-thread callback from the coalescer: mark a cycle due, wake the worker."""
        self._cycle_due.set()
        self._awake.set()

    # -- worker -------------------------------------------------------------

    def _supervise(self) -> None:
        """Run the worker loop; restart it with backoff whenever it dies.

        A worker that died used to stop every later cycle until the gateway
        restarted, while this process kept the leader lock and the socket
        stayed connected. Now the loop is restarted after 1 s, 2 s, 4 s …
        (capped at 60 s, reset after a loop that lived five minutes), the
        restart reads the tray again, and the health record says
        ``restart_wait`` with the error meanwhile.
        """
        in_a_row = 0
        while not self._stopped.is_set():
            started = time.monotonic()
            error: BaseException | None = None
            try:
                self._worker_loop()
            except BaseException as exc:
                error = exc
            if self._stopped.is_set():
                break
            if time.monotonic() - started >= WORKER_STABLE_S:
                in_a_row = 0
            in_a_row += 1
            self.worker_restarts += 1
            ceiling = min(WORKER_BACKOFF_MAX_S, WORKER_BACKOFF_BASE_S * (2 ** (in_a_row - 1)))
            delay = random.uniform(ceiling * 0.8, ceiling * 1.2)
            what = f"{type(error).__name__}: {error}" if error is not None else "worker loop returned"
            logger.error(
                "AgentInvoker: worker loop died (%s) — restarting in %.1fs (restart %d); the "
                "cards stay on the tray and the restart reads it again",
                what,
                delay,
                self.worker_restarts,
                exc_info=(type(error), error, error.__traceback__) if error is not None else None,
            )
            self._set_state("restart_wait", error=what)
            if self._stopped.wait(delay):
                break
            # Whatever woke the dead worker is not lost: read the tray again.
            self._cycle_due.set()
            self._awake.set()
        self._set_state("stopped")
        logger.info("AgentInvoker: worker exiting")

    def _worker_loop(self) -> None:
        # The worker serves one profile for its lifetime: everything it does
        # — the hosted-tools probe, the agent it builds, the home it reads —
        # runs inside that profile's context, as Hermes runs the profile's
        # own turns. Essential under a multiplexed gateway.
        with profile_context(self._profile_home()):
            self._worker_body()

    def _profile_home(self) -> Path:
        home = getattr(self._runtime, "hermes_home", None)
        if home is None:
            home = getattr(self._config, "hermes_home", None)
        return Path(home) if home is not None else resolve_hermes_home()

    def _worker_body(self) -> None:
        """The worker loop. Whatever escapes it is the supervisor's (:meth:`_supervise`)."""
        logger.info("AgentInvoker: worker started")
        self._set_state("idle")
        while not self._stopped.is_set():
            self._awake.wait(timeout=_WORKER_IDLE_WAIT_S)
            self._beat()
            if self._stopped.is_set():
                return
            self._awake.clear()
            if self._cycle_due.is_set():
                # Clear BEFORE the cycle: a wake landing during the cycle
                # sets it again and earns one more cycle afterwards.
                self._cycle_due.clear()
                self._set_state("cycle")
                self._guarded_cycle()
            self._set_state("idle")

    def _guarded_cycle(self) -> None:
        try:
            # The tray read, the gate's model call, the hosted-tools hold and
            # the turn all run in the profile's own runtime scope — the one the
            # gateway uses for this profile's turns — so its provider key and
            # secrets are its own, never the launch profile's.
            with profile_turn_scope(self._profile_home()):
                self.run_cycle()
        except ProfileScopeError as exc:
            # Never run a cycle with another profile's credentials: nothing is
            # read or recorded, the cards stay on the tray, the server re-wakes.
            logger.warning("AgentInvoker: cycle not run — %s", exc)
            self._schedule_retry("profile scope unavailable")
        except Exception:
            # Outer catch: whatever escaped the cycle must not kill the
            # worker. The cards stay on the tray; the server re-wakes.
            logger.exception("AgentInvoker: cycle FAILED with unhandled exception (outer catch)")
            self._schedule_retry("unhandled exception")

    # -- one cycle ----------------------------------------------------------

    def run_cycle(self) -> gate.GateResult[gate.GateReply] | None:
        """Read the tray, gate its cards, start a turn when warranted, then
        one turn per pending invite.

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
        result = self._decide_cards(tray)
        self._run_invite_turns(tray)
        return result

    def _decide_cards(self, tray: InboxTray) -> gate.GateResult[gate.GateReply]:
        """Gate the tray's cards and run the reply turn (steps 3 to 5 of the module docstring)."""
        client = self._runtime.client
        tray, skipped = self._skip_in_flight(tray)
        if skipped:
            # A turn is running for these conversations (a concurrent caller
            # of this method; the worker itself never overlaps). They are not
            # gated again; the tray is re-read once that turn has ended, so a
            # message newer than its anchor is seen.
            logger.info(
                "AgentInvoker: %d card(s) already have a turn in flight (%s) — not gated "
                "again; the tray is re-read when it ends",
                len(skipped),
                ", ".join(skipped),
            )
            self._coalescer.wake()
        cards = gate.cards_of(tray)
        truncated = bool(tray.truncated)
        if not cards:
            logger.info(
                "AgentInvoker: tray empty (deferred=%s) — nothing to decide",
                tray.deferred,
            )
            return gate.GateResult(outcome="empty")

        if not self._gate_enabled:
            # Gate off: the agent judges in-turn with the hosted tools. No
            # verdict is recorded by the integration; the turn records its own.
            logger.info(
                "AgentInvoker: %d card(s) on the tray, gate disabled — starting the turn",
                len(cards),
            )
            turn: TurnRun = self._run_wake_turn(
                reply=[], covering=[c["conversation_id"] for c in cards]
            )
            if turn != "completed":
                self._schedule_retry(f"turn {turn.replace('_', ' ')}")
            if truncated:
                self._coalescer.wake()
            return gate.GateResult(outcome="decided", undecided=[c["conversation_id"] for c in cards])

        host_llm = self._host_llm_facade()
        call_model = build_call_model(
            main_runtime=self._gate_runtime() if host_llm is None else {},
            timeout_s=self._gate_timeout_s,
            host_llm=host_llm,
        )
        # The gate prompt's own words are served (``gate_call_preamble``,
        # ``gate_call_answer_format``); without texts the gate fails closed.
        # The tray's question names the hosted tools bare: the namer spells
        # them the way Hermes shows them (served texts already are).
        result = gate.run(
            tray,
            call_model,
            client,
            texts=getattr(self._runtime, "texts", None),
            tool_namer=gate.mcp_tool_namer(MCP_SERVER_NAME),
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
            # on the tray; the server re-wakes while they are unanswered, so a
            # transient provider outage does not leave peers waiting for the
            # next message to arrive.
            self._schedule_retry(result.failure or "gate failed closed")
            return result

        turn = "completed"
        self._cards_left_after_turn = 0
        if result.reply:
            turn = self._run_wake_turn(reply=result.reply)

        if turn != "completed":
            self._schedule_retry(f"turn {turn.replace('_', ' ')}")
        elif result.record_failures or result.undecided or self._cards_left_after_turn:
            # Some cards are still on the tray: a verdict the server
            # rejected, a card the model skipped, or a reply card the turn
            # left (it could not look, or looked once and did nothing — the
            # second look dismisses it). Look again later.
            self._schedule_retry("cards left on the tray")

        if truncated:
            # More conversations are waiting than one tray shows.
            logger.info("AgentInvoker: tray truncated — waking again for the rest")
            self._coalescer.wake()
        return result

    def _schedule_retry(self, reason: str) -> None:
        """A cycle ended with cards still on the tray. Nothing is scheduled.

        The server re-wakes an agent whose tray holds unanswered cards (or a
        pending invite) on its own ladder — +1 m, +5 m, +30 m, +3 h after the
        newest message, restarting on a newer one — and sends that wake to
        this socket because it negotiated ``rewake`` (contract v1 D3; see
        :mod:`.ws_daemon`). The integration keeps no ladder of its own.
        """
        self.left_on_tray += 1
        self.last_left_reason = reason
        logger.warning(
            "AgentInvoker: %s — the cards stay on the tray; the server re-wakes while they are "
            "unanswered",
            reason,
        )

    # -- turns --------------------------------------------------------------

    def _texts(self) -> Texts | None:
        texts: Texts | None = getattr(self._runtime, "texts", None)
        if texts is None:
            logger.warning(
                "AgentInvoker: no served texts yet (/v1/texts unreachable and nothing cached) "
                "— the turn cannot start; the cards stay on the tray"
            )
        return texts

    def _run_wake_turn(
        self, *, reply: list[gate.GateReply], covering: Sequence[str] = ()
    ) -> TurnRun:
        """One turn for the cycle.

        ``reply`` entries are recorded only when the turn has **completed**
        (``run_conversation`` returned a result that is not a failure) and
        it **could look**: a turn whose transcript shows no AgentChat call,
        or only failed ones, records nothing. A turn that could not start,
        raised, failed, or overran its time limit records nothing either;
        the cards stay on the tray and the server re-wakes. An
        entry whose record fails is logged: it stays unread and the next
        wake gates it again.

        While the turn runs its conversations (``reply``, plus ``covering``
        for a gate-less turn) are held: a cycle that reads the tray
        meanwhile does not gate them again, and a turn that overran keeps
        them held until its thread has really ended.
        """
        texts = self._texts()
        if texts is None:
            return "not_started"
        session_id = wake_session_id()
        ids = [entry.conversation_id for entry in reply]
        busy = frozenset(ids) | {cid for cid in covering if isinstance(cid, str) and cid}
        logger.info(
            "AgentInvoker: turn start session=%s reply_cards=%d",
            session_id,
            len(ids),
        )
        started = self._start_turn(
            prompt=wake_turn_prompt(texts, ids), session_id=session_id, holds=busy
        )
        if started is None:
            return "not_started"
        report = started()
        if report.status != "completed":
            logger.warning(
                "AgentInvoker: turn %s %s (%s) — nothing recorded for %s; the conversation(s) "
                "stay unread and the next wake gates them again",
                session_id,
                "timed out" if report.status == "timeout" else "did not complete",
                report.detail or "no detail",
                ", ".join(ids) or "no card",
            )
            return "timeout" if report.status == "timeout" else "crashed"
        self.turns += 1
        if reply:
            self._record_after_turn(reply, report)
        return "completed"

    def _record_after_turn(self, reply: Sequence[gate.GateReply], report: TurnReport) -> None:
        """Record what the turn's outcome calls for, per ``reply`` card.

        The kit's reply rule (``agentchatme.integration.turn_outcome``)
        decides from one fresh tray read and the turn's AgentChat call
        counts, never from "the turn ran": the turn recorded its own verdict
        → nothing; the answer went out → ``reply`` through the anchor the
        gate saw; no AgentChat call, or only failed ones → nothing; looked
        and did nothing → nothing the first time, ``dismiss`` the second
        time for the same anchor. A failed tray read records nothing (the
        card stays; the next wake looks again).
        """
        client = self._runtime.client
        try:
            fresh: Any = client.inbox.tray(wait_ms=0, limit=TRAY_LIMIT)
        except Exception as exc:
            logger.info("AgentInvoker: tray re-read after the turn failed (%s) — nothing recorded", exc)
            fresh = None
        end = report.turn_end()
        started_at = datetime.fromtimestamp(report.started_at, tz=timezone.utc)
        for entry in reply:
            key = (entry.conversation_id, entry.anchor_message_id or "")
            outcome = turn_outcome(
                turn=end,
                turn_started_at=started_at,
                gate_card=entry.card,
                category=entry.category,
                fresh_tray=fresh,
                silent_before=self._silent_turns.get(key, 0),
            )
            self._remember_silence(key, outcome.reason)
            self.outcomes[outcome.reason] += 1
            logger.info(
                "AgentInvoker: conv=%s after turn %s: %s → %s (%s)",
                entry.conversation_id,
                report.session_id,
                outcome.reason,
                outcome.action,
                report.calls_text(),
            )
            if outcome.action == "none":
                if outcome.reason not in ("turn_recorded_verdict", "card_left_tray"):
                    self._cards_left_after_turn += 1
                continue
            try:
                apply_outcome(client, outcome)
            except Exception as exc:
                self._cards_left_after_turn += 1
                logger.warning(
                    "AgentInvoker: %s for conv=%s not recorded (%s) — it stays unread and the "
                    "next wake gates it again",
                    outcome.action,
                    entry.conversation_id,
                    exc,
                )
                continue
            if outcome.action == "record_reply":
                self.recorded_replies += 1
            else:
                self.recorded_dismisses += 1

    def _remember_silence(self, key: tuple[str, str], reason: str) -> None:
        if reason == "silent_first":
            self._silent_turns[key] = self._silent_turns.get(key, 0) + 1
            self._silent_turns.move_to_end(key)
            while len(self._silent_turns) > _SILENT_MEMORY:
                self._silent_turns.popitem(last=False)
        elif reason not in (
            "turn_failed",
            "turn_running",
            "card_unknown",
            "invite_unknown",
            "could_not_look",
        ):
            # Recorded, answered, decided by the turn, or gone: start over.
            self._silent_turns.pop(key, None)

    def _run_invite_turns(self, tray: InboxTray) -> None:
        """One turn per pending invite on the tray, a few per cycle.

        An invite whose earlier turn overran and has not ended gets no second
        turn. Past the per-cycle cap, or a truncated invite list, the tray is
        read again in a new cycle, so messages are gated in between.
        """
        invites: list[TrayInvite] = list(getattr(tray, "invites", None) or [])
        started = 0
        for invite in invites:
            if self._stopped.is_set():
                return
            if self._running.is_busy(_INVITE_KEY + invite.invite_id):
                logger.info(
                    "AgentInvoker: group invite %s still has a turn running — not starting another",
                    invite.invite_id,
                )
                continue
            if started >= MAX_INVITE_TURNS_PER_CYCLE:
                logger.info(
                    "AgentInvoker: %d invite turn(s) this cycle — the rest on the next", started
                )
                self._coalescer.wake()
                return
            started += 1
            self._run_invite_turn(invite)
        if getattr(tray, "invites_truncated", False):
            self._coalescer.wake()

    def _run_invite_turn(self, invite: TrayInvite) -> None:
        texts = self._texts()
        if texts is None:
            self._schedule_retry("invite turn not started")
            return
        session_id = invite_session_id(invite.invite_id)
        logger.info(
            "AgentInvoker: group-invite turn start invite=%s group=%s from=%s",
            invite.invite_id,
            invite.group.id,
            invite.invited_by,
        )
        started = self._start_turn(
            prompt=invite_turn_prompt(texts, invite.invite_id),
            session_id=session_id,
            holds=frozenset({_INVITE_KEY + invite.invite_id}),
        )
        if started is None:
            self._schedule_retry("invite turn not started")
            return
        report = started()
        if report.status == "completed":
            self.invite_turns += 1
        client = self._runtime.client
        try:
            fresh: Any = client.inbox.tray(wait_ms=0, limit=TRAY_LIMIT)
        except Exception as exc:
            logger.info("AgentInvoker: tray re-read after the invite turn failed (%s)", exc)
            fresh = None
        key = ("invite", invite.invite_id)
        outcome = invite_outcome(
            turn=report.turn_end(),
            invite=invite,
            fresh_tray=fresh,
            silent_before=self._silent_turns.get(key, 0),
        )
        self._remember_silence(key, outcome.reason)
        self.outcomes[f"invite_{outcome.reason}"] += 1
        logger.info(
            "AgentInvoker: invite=%s after turn %s: %s → %s (%s)",
            invite.invite_id,
            session_id,
            outcome.reason,
            outcome.action,
            report.calls_text(),
        )
        if outcome.action == "dismiss_invite":
            try:
                apply_invite_outcome(client, outcome)
            except Exception as exc:
                logger.warning("AgentInvoker: invite %s not dismissed (%s)", invite.invite_id, exc)
                self._schedule_retry("invite dismiss failed")
            return
        if outcome.reason != "invite_answered":
            self._schedule_retry(f"invite {outcome.reason.replace('_', ' ')}")

    def _start_turn(
        self, *, prompt: str, session_id: str, holds: frozenset[str] = frozenset()
    ) -> StartedTurn | None:
        """Build a transient AIAgent for one turn. ``None`` when it cannot start.

        First holds, bounded, until Hermes has registered the hosted
        AgentChat tools (:mod:`.hosted_tools`): a turn without them cannot
        look and must not count as started. Returns a callable that runs
        the turn under the time limit (:func:`.turn_runner.run_turn`) and
        reports it. ``holds`` are the keys (conversation ids, invite keys)
        marked running from the turn's start until its thread has ended.
        The return value of ``run_conversation`` is deliberately discarded:
        the agent's only way to say anything on AgentChat is the hosted
        ``agentchat_send_message`` tool during the turn.
        """
        hung = self._running.hung
        if hung >= MAX_HUNG_TURNS:
            logger.error(
                "AgentInvoker: %d earlier turn(s) overran and have not ended — %s not started "
                "until one does (the cards stay on the tray)",
                hung,
                session_id,
            )
            return None
        held = wait_for_hosted_tools(label=f"turn {session_id}", stopped=self._stopped)
        if not held:
            # ``False``: not registered within the bound; ``None``: stopping.
            return None
        self._ensure_hermes_resolved()
        agent = self._build_agent(session_id)
        if agent is None:
            logger.warning("AgentInvoker: agent construction failed for %s — turn not started", session_id)
            return None

        def run() -> TurnReport:
            token = self._running.hold(holds)
            with self._state_lock:
                self._current_turn = {
                    "session_id": session_id,
                    "started_at": time.time(),
                    "conversations": len([k for k in holds if not k.startswith(_INVITE_KEY)]),
                }
            self._set_state("turn")
            try:
                report = run_turn(
                    agent,
                    prompt,
                    session_id=session_id,
                    limits=self._limits,
                    stopped=self._stopped,
                    on_finished=lambda: self._running.release(token),
                    on_poll=self._beat,
                )
            except BaseException:
                # Not expected (the runner reports, it does not raise). If it
                # does, the turn's thread may be running: keep its
                # conversations held until that thread ends, never release them.
                self._running.abandon(token)
                raise
            finally:
                with self._state_lock:
                    self._current_turn = None
            if not report.ended:
                self._running.abandon(token)
            with self._state_lock:
                self._last_turn = {
                    "session_id": session_id,
                    "status": report.status,
                    "duration_s": round(report.duration_s, 1),
                    "ended": report.ended,
                    "agentchat_calls": None
                    if report.calls is None
                    else {"made": report.calls.made, "failed": report.calls.failed},
                }
            self._set_state("cycle")
            if report.status == "completed":
                logger.info(
                    "AgentInvoker: turn complete session=%s %s %.1fs",
                    session_id,
                    report.calls_text(),
                    report.duration_s,
                )
            return report

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
        budget: dict[str, Any] = {}
        if self._limits.max_s and _accepts_parameter(AIAgent, "run_budget_seconds"):
            # Hermes' own cooperative run budget: past 80 % the model is told to
            # wrap up, and a stalled provider call is capped at half the time
            # left — so the turn usually ends itself before the ceiling's
            # interrupt (:mod:`.turn_runner`). Older Hermes: the interrupt alone.
            budget["run_budget_seconds"] = self._limits.max_s
        try:
            return AIAgent(
                model=model,
                session_id=session_id,
                session_db=self._session_db,
                platform="agentchat",
                quiet_mode=True,
                load_soul_identity=True,
                skip_context_files=True,
                **budget,
                **runtime_kwargs,
            )
        except Exception:
            # A constructor failure (provider config, session DB, a Hermes
            # change) is a turn that did not start: nothing recorded, the
            # cards stay on the tray and the server re-wakes.
            logger.exception(
                "AgentInvoker: AIAgent construction raised for %s — turn not started", session_id
            )
            return None

    def _host_llm_facade(self) -> Any:
        """Hermes' ``ctx.llm`` for the gate call, or ``None`` to use the fallback path."""
        if self._host_llm is None:
            return None
        try:
            return self._host_llm()
        except Exception as exc:
            if not self._host_llm_warned:
                self._host_llm_warned = True
                logger.warning(
                    "AgentInvoker: Hermes' plugin LLM (ctx.llm) is unavailable (%r) — the gate "
                    "calls the agent's model through the auxiliary client instead",
                    exc,
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

_INVITE_KEY = "invite:"


def _accepts_parameter(cls: Any, name: str) -> bool:
    try:
        return name in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False


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
