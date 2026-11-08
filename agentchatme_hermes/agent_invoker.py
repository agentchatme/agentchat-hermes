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
   turn's return value is discarded — nothing is auto-routed anywhere. The
   turn runs under a time limit (:mod:`.turn_runner`): no activity for
   ``AGENTCHATME_TURN_IDLE_TIMEOUT_S`` (600 s) or a run past
   ``AGENTCHATME_TURN_MAX_S`` (3600 s) interrupts it with Hermes' own stop,
   and it counts as failed. Its conversations stay held until its thread has
   really ended, so no second turn starts for them meanwhile.
5. **Record.** When that turn has **completed** and its transcript shows it
   reached AgentChat (at least one hosted tool call that did not fail), call
   ``record()`` on every ``reply`` entry: a turn that looked counts as read
   — whether or not it chose to send. A turn with no AgentChat call, or only
   failed ones, could not look and records nothing. Except where the turn recorded
   a verdict for that conversation itself: the served texts tell the model
   to record ``reply`` after its send, or ``dismiss`` when it read and chose
   not to answer (or ``defer``). One fresh tray read after the turn shows
   it — the card has left the tray (read, or hidden by a defer), or its
   ``your_last_decision`` moved since the gate's tray — and the host then
   records nothing more, so the agent's own last verdict stands and a reply
   is counted once. If that read fails, or the card may sit past a
   truncated tray, ``reply`` is recorded as before (reading is
   forward-only, so a repeat never un-reads anything). Never earlier: recording marks
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
the earlier wake announced is covered). Because ``reply`` is recorded only
when the turn ends, the card stays on the tray for the whole turn; the
worker's serialisation is what keeps a second cycle from gating it again
mid-turn, and an explicit per-conversation in-flight set backs that up:
cards whose turn is running are dropped before the gate (never gated,
never recorded twice), the set is cleared when the turn ends — completed
or crashed — and a cycle that skipped a card re-reads the tray once the
turn is over, so a message newer than the anchor is not missed. A cycle that could not complete —
model unreachable, server error, verdicts rejected, turn not started or
crashed — re-wakes itself on a local ladder (1 m, 5 m, 30 m) because a
connected socket gets no re-wake from the server; the cards wait on the
tray meanwhile. A gate that failed closed logs once and starts nothing.

Group invites are a second wake kind: not on the tray, not gated, one turn
each with the served invite prompt, serialized on the same worker. The
:class:`.invites.InviteLedger` remembers which were offered and which had a
turn that looked, so an invite whose turn could not start, crashed or
overran is retried on the next wake and a restart does not re-run the ones
the agent already looked at.

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
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from agentchatme import gate

from .config import (
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_REPLY_GATE_ENABLED,
    DEFAULT_TURN_IDLE_S,
    DEFAULT_TURN_MAX_S,
    DEFAULT_WAKE_DEBOUNCE_S,
)
from .hosted_tools import wait_for_hosted_tools
from .invites import InviteLedger
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

    from .config import Config
    from .types import AgentIdentity, GroupInviteEvent
    from .ws_health import HealthBoard

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
# ``run_conversation`` is next. Calling it runs the turn under the time
# limit (:mod:`.turn_runner`) and reports what became of it.
StartedTurn = Callable[[], TurnReport]

# What became of the cycle's turn. Only ``completed`` may record ``reply``;
# ``could_not_look`` is a completed turn that made no AgentChat call, or
# whose calls all failed.
TurnOutcome = Literal["completed", "could_not_look", "crashed", "timeout", "not_started"]

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


class AgentInvoker:
    """Owns the wake worker. Single-use — :meth:`start` once, :meth:`stop` once."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        runtime: Any,
        ledger: InviteLedger | None = None,
        health: HealthBoard | None = None,
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

        idle_s = float(getattr(config, "turn_idle_s", DEFAULT_TURN_IDLE_S))
        max_s = float(getattr(config, "turn_max_s", DEFAULT_TURN_MAX_S))
        self._limits = TurnLimits(idle_s=idle_s or None, max_s=max_s or None)

        self._coalescer = WakeCoalescer(on_fire=self._on_wake_fired, debounce_s=debounce_s)
        self._awake = threading.Event()
        self._cycle_due = threading.Event()
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        self._invites: collections.deque[GroupInviteEvent] = collections.deque()
        self._invites_lock = threading.Lock()
        self._retry_rung = 0
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
        # ``reply`` records that failed in the last cycle's turn (server
        # error): the cards are still on the tray, the ladder re-wakes.
        self._reply_record_failures = 0

        # Counters for the heartbeat / doctor and for tests.
        self.cycles = 0
        self.turns = 0
        self.invite_turns = 0
        self.recorded_replies = 0
        # ``reply`` entries the host left alone because the turn recorded a
        # verdict for that conversation itself (see the module docstring).
        self.turn_recorded_verdicts = 0

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
                self._requeue_pending_invites()
                self._drain_invites()
                self._guarded_cycle()
            elif self._has_queued_invites():
                self._set_state("cycle")
                self._drain_invites()
            self._set_state("idle")

    def _has_queued_invites(self) -> bool:
        with self._invites_lock:
            return bool(self._invites)

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
                if self._running.is_busy(_INVITE_KEY + invite.invite_id):
                    # Its earlier turn overran and has not ended: no second
                    # turn while the first may still act. It stays offered.
                    logger.info(
                        "AgentInvoker: group invite %s still has a turn running — not starting "
                        "another",
                        invite.invite_id,
                    )
                    continue
                self._ledger.offer(invite)
                with profile_turn_scope(self._profile_home()):
                    self._run_invite_turn(invite)
            except ProfileScopeError as exc:
                # Offered, not started: the next wake retries it in scope.
                logger.warning("AgentInvoker: group-invite turn not started — %s", exc)
                self._schedule_retry("profile scope unavailable")
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
            # The tray read, the gate's model call, the hosted-tools hold and
            # the turn all run in the profile's own runtime scope — the one the
            # gateway uses for this profile's turns — so its provider key and
            # secrets are its own, never the launch profile's.
            with profile_turn_scope(self._profile_home()):
                self.run_cycle()
        except ProfileScopeError as exc:
            # Never run a cycle with another profile's credentials: nothing is
            # read or recorded, the cards stay on the tray, the ladder re-wakes.
            logger.warning("AgentInvoker: cycle not run — %s", exc)
            self._schedule_retry("profile scope unavailable")
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
            if not skipped:
                self._retry_rung = 0
            return gate.GateResult(outcome="empty")

        if not self._gate_enabled:
            # Gate off: the agent judges in-turn with the hosted tools. No
            # verdict is recorded by the integration; the turn records its own.
            logger.info(
                "AgentInvoker: %d card(s) on the tray, gate disabled — starting the turn",
                len(cards),
            )
            turn: TurnOutcome = self._run_wake_turn(
                reply=[], covering=[c["conversation_id"] for c in cards]
            )
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
        self._reply_record_failures = 0
        if result.reply:
            turn = self._run_wake_turn(reply=result.reply)

        if turn != "completed":
            self._schedule_retry(f"turn {turn.replace('_', ' ')}")
        elif result.record_failures or result.undecided or self._reply_record_failures:
            # Some cards were not read (verdict rejected by the server — the
            # gate's or this turn's — or the model skipped them). They are
            # still on the tray; look again later.
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

    def _run_wake_turn(
        self, *, reply: list[gate.GateReply], covering: Sequence[str] = ()
    ) -> TurnOutcome:
        """One turn for the cycle.

        ``reply`` entries are recorded only when the turn has **completed**
        (``run_conversation`` returned a result that is not a failure) and
        it **could look**: a turn whose transcript shows no AgentChat call,
        or only failed ones, records nothing. A turn that could not start,
        raised, failed, or overran its time limit records nothing either;
        the cards stay on the tray and the ladder brings the next wake. An
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
                "AgentInvoker: turn %s %s (%s) — reply verdict(s) for %s NOT recorded; the "
                "conversation(s) stay unread and the next wake gates them again",
                session_id,
                "timed out" if report.status == "timeout" else "did not complete",
                report.detail or "no detail",
                ", ".join(ids) or "no card",
            )
            return "timeout" if report.status == "timeout" else "crashed"
        if report.calls.could_not_look:
            logger.warning(
                "AgentInvoker: turn %s ended with %s AgentChat call(s), %s failed — it could not "
                "look; nothing is recorded, the cards stay on the tray",
                session_id,
                report.calls.calls,
                report.calls.failed,
            )
            return "could_not_look"
        self.turns += 1
        decided_in_turn = self._decided_in_turn(reply)
        for entry in reply:
            if entry.conversation_id in decided_in_turn:
                self.turn_recorded_verdicts += 1
                logger.info(
                    "AgentInvoker: conv=%s — the turn recorded its own verdict; reply not "
                    "recorded again",
                    entry.conversation_id,
                )
                continue
            try:
                entry.record()
                self.recorded_replies += 1
            except Exception as exc:
                self._reply_record_failures += 1
                logger.warning(
                    "AgentInvoker: reply verdict for conv=%s not recorded (%s) — it stays "
                    "unread and the next wake gates it again",
                    entry.conversation_id,
                    exc,
                )
        return "completed"

    def _decided_in_turn(self, reply: Sequence[gate.GateReply]) -> set[str]:
        """The ``reply`` conversations the completed turn recorded a verdict
        for itself, seen on one fresh tray read.

        A card that left the tray was read (a verdict or a mark-read) or
        hidden by a ``defer`` — either way by the turn, since the host had
        recorded nothing for it. A card still there whose
        ``your_last_decision`` differs from the gate's copy got a verdict in
        the turn (a newer message keeps it on the tray). A card still there,
        unchanged, got nothing: the host records ``reply``. When the read
        fails, or a missing card may sit past a truncated tray, nothing is
        claimed and ``reply`` is recorded as before.
        """
        if not reply:
            return set()
        try:
            tray = self._runtime.client.inbox.tray(wait_ms=0, limit=TRAY_LIMIT)
        except Exception as exc:
            logger.info(
                "AgentInvoker: tray re-read after the turn failed (%s) — recording reply "
                "for every card the gate chose",
                exc,
            )
            return set()
        cards = {card["conversation_id"]: card for card in gate.cards_of(tray)}
        truncated = _tray_truncated(tray)
        decided: set[str] = set()
        for entry in reply:
            card = cards.get(entry.conversation_id)
            if card is None:
                if not truncated:
                    decided.add(entry.conversation_id)
                continue
            if _last_decision_at(card) != _last_decision_at(entry.card):
                decided.add(entry.conversation_id)
        return decided

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
        started = self._start_turn(
            prompt=invite_turn_prompt(texts),
            session_id=session_id,
            holds=frozenset({_INVITE_KEY + invite.invite_id}),
        )
        if started is None:
            # Stays "offered" in the ledger; the next wake queues it again.
            self._schedule_retry("invite turn not started")
            return
        report = started()
        if report.status != "completed" or report.calls.could_not_look:
            # Never marked started: a turn that crashed, overran or could not
            # look did not decide the invite. It stays offered and the next
            # wake runs it again (not while this one is still running).
            logger.warning(
                "AgentInvoker: group-invite turn for %s did not decide it (%s) — it stays "
                "offered and the next wake tries again",
                invite.invite_id,
                report.detail
                or f"{report.calls.calls} AgentChat call(s), {report.calls.failed} failed",
            )
            self._schedule_retry("invite turn did not complete")
            return
        self._ledger.mark_started(invite.invite_id)
        self.invite_turns += 1

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
                    **report.calls.as_dict(),
                }
            self._set_state("cycle")
            if report.status == "completed":
                logger.info(
                    "AgentInvoker: turn complete session=%s agentchat_calls=%s failed=%s %.1fs",
                    session_id,
                    report.calls.calls,
                    report.calls.failed,
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

_INVITE_KEY = "invite:"


def _accepts_parameter(cls: Any, name: str) -> bool:
    try:
        return name in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False


def _card_data(card: Any) -> Mapping[str, Any]:
    if isinstance(card, Mapping):
        return card
    dump = getattr(card, "model_dump", None)
    if callable(dump):
        data = dump(mode="json")
        if isinstance(data, Mapping):
            return data
    return {}


def _last_decision_at(card: Any) -> str | None:
    """When the card's ``your_last_decision`` was recorded (server time), or
    ``None`` when the agent never recorded one. Compared only with another
    value from the same server field, so no clock is involved."""
    last = _card_data(card).get("your_last_decision")
    if isinstance(last, Mapping):
        at = last.get("at")
        if isinstance(at, str) and at:
            return at
    return None


def _tray_truncated(tray: Any) -> bool:
    if isinstance(tray, Mapping):
        return bool(tray.get("truncated"))
    return bool(getattr(tray, "truncated", False))


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
