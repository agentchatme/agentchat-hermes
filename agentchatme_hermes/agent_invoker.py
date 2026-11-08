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
   read — whether or not it chose to send. Except where the turn recorded
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
:class:`.invites.InviteLedger` remembers which were offered and which got a
turn, so an invite whose turn could not start is retried on the next wake
and a restart does not re-run the ones the agent already looked at.

This mirrors the pattern Hermes' cron scheduler uses: build a transient
agent, run a turn, ignore the response.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
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
from .profile_scope import (
    ProfileScopeError,
    profile_context,
    profile_turn_scope,
    resolve_hermes_home,
)
from .turn import (
    MCP_SERVER_NAME,
    MCP_TOOL_PREFIX,
    invite_session_id,
    invite_turn_prompt,
    wake_session_id,
    wake_turn_prompt,
)
from .wake import WakeCoalescer

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        # Conversations whose turn is running right now. See the module
        # docstring: the record happens when the turn ends, so until then
        # the card is still on the tray and must not be gated again.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
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

    def in_flight(self) -> set[str]:
        """Conversation ids whose turn is running right now (diagnostics, tests)."""
        with self._in_flight_lock:
            return set(self._in_flight)

    def _skip_in_flight(self, tray: InboxTray) -> tuple[InboxTray, list[str]]:
        """``(tray without the cards whose turn is running, the ids dropped)``."""
        with self._in_flight_lock:
            busy = set(self._in_flight)
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
        (``run_conversation`` returned): a completed turn looked, so the
        conversation is read — whether or not it sent. A turn that could
        not start or that raised records nothing; the cards stay on the
        tray and the ladder brings the next wake. An entry whose record
        fails is logged: it stays unread and the next wake gates it again.

        While the turn runs its conversations (``reply``, plus ``covering``
        for a gate-less turn) are in flight: a cycle that reads the tray
        meanwhile does not gate them again.
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
        busy = set(ids) | {cid for cid in covering if isinstance(cid, str) and cid}
        with self._in_flight_lock:
            self._in_flight |= busy
        try:
            completed = started()
        finally:
            with self._in_flight_lock:
                self._in_flight -= busy
        if not completed:
            logger.warning(
                "AgentInvoker: turn %s did not complete — reply verdict(s) for %s NOT recorded; "
                "the conversation(s) stay unread and the next wake gates them again",
                session_id,
                ", ".join(ids) or "no card",
            )
            return "crashed"
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
        held = wait_for_hosted_tools(label=f"turn {session_id}", stopped=self._stopped)
        if not held:
            # ``False``: not registered within the bound; ``None``: stopping.
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
            failure = turn_failure(result)
            if failure is not None:
                # Hermes reports provider errors, usage limits, content-policy
                # blocks and partial turns in the result, not as exceptions.
                # Such a turn did not look: it is a crash, nothing is recorded.
                logger.warning(
                    "AgentInvoker: turn %s did not complete (%s)", session_id, failure
                )
                return False
            if turn_looked(result) is False:
                # The turn ran to the end, but every AgentChat tool call in it
                # failed (transport down, server error): it could not look.
                logger.warning(
                    "AgentInvoker: turn %s ended but every AgentChat tool call in it failed — "
                    "it could not look; nothing is recorded, the cards stay on the tray",
                    session_id,
                )
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

_HOSTED_TOOL_MARK = f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__"
_UNTRUSTED_SOURCE_MARK = f'source="{_HOSTED_TOOL_MARK}'


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


def turn_looked(result: Any) -> bool | None:
    """Did a turn that ran to the end actually reach AgentChat?

    Reads the transcript Hermes returns (``result["messages"]``: ``role:
    tool`` messages carry ``name`` and ``content``; a hosted tool's result is
    wrapped with ``source="mcp__agentchat__…"``, an error is a bare
    ``{"error": …}`` object). ``False`` only on positive evidence: AgentChat
    tools were called and every call errored (the MCP transport was down, the
    server refused) — such a turn could not look and must not record.
    ``True`` when at least one succeeded; ``None`` when the transcript shows
    no AgentChat tool call at all (the model's own choice, or a shape this
    code does not know), which is trusted as before.
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return None
    calls = 0
    succeeded = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        name = str(message.get("name") or message.get("tool_name") or "")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            try:
                text = json.dumps(content)
            except (TypeError, ValueError):
                text = str(content)
        if not (name.startswith(_HOSTED_TOOL_MARK) or _UNTRUSTED_SOURCE_MARK in text):
            continue
        calls += 1
        if not _is_tool_error(text):
            succeeded += 1
    if calls == 0:
        return None
    return succeeded > 0


def _is_tool_error(text: str) -> bool:
    """A Hermes tool result that is an error: a bare ``{"error": …}`` payload
    (unwrapped, or the only object inside the untrusted wrapper)."""
    body = text.strip()
    if _UNTRUSTED_SOURCE_MARK in body:
        # Wrapped data: the payload follows the wrapper's blank line.
        start = body.find("{")
        body = body[start:] if start >= 0 else ""
    if not body.startswith("{"):
        return False
    try:
        payload = json.loads(body[: body.rfind("}") + 1])
    except ValueError:
        return body.startswith('{"error"')
    return isinstance(payload, dict) and "error" in payload and "result" not in payload


def turn_failure(result: Any) -> str | None:
    """Why a ``run_conversation`` result is NOT a completed turn, or ``None``.

    Hermes does not raise for provider errors, usage limits, content-policy
    blocks or partial turns; it returns ``{"completed": False, "failed":
    True, "error": …}`` or ``{"partial": True, …}`` (``agent/
    conversation_loop.py``). Such a turn read nothing and must not record.
    A non-dict result (an older Hermes) is taken as completed.
    """
    if not isinstance(result, dict):
        return None
    failed = bool(result.get("failed"))
    partial = bool(result.get("partial"))
    completed = result.get("completed")
    if not failed and not partial and completed is not False:
        return None
    error = result.get("error")
    reason = result.get("failure_reason")
    what = "failed" if failed else ("partial" if partial else "not completed")
    detail = str(error).strip()[:300] if error else "no error text"
    return f"{what}{f', {reason}' if reason else ''}: {detail}"


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
