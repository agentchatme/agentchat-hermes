"""The Hermes side of receiving: how this runtime asks its model and runs a turn.

Receiving itself is the SDK kit's :class:`agentchatme.integration.ReceiveLoop`
(gate-call mode, contract v1): the socket as a wake, one cycle per burst, the
tray (with its invites), the gate call, one turn per ``reply`` card and per
pending invite (one at a time), what is recorded after each turn (the D5
rule), D5 pacing, and no retry ladder (the server re-wakes). :mod:`.receiver`
hosts it. This module is what only Hermes can say, as the loop's callbacks:

* :meth:`AgentInvoker.call_model` — the gate's one tool-less completion on
  the agent's own model: Hermes' ``ctx.llm`` (else its auxiliary client
  pinned to the profile's model), inside the profile's runtime scope;
  cancelled when the loop stops waiting, where Hermes can (:mod:`.model_call`).
* :meth:`AgentInvoker.readiness` — are the hosted ``mcp__agentchat__*``
  tools registered in Hermes; a turn without them cannot look. Holds up to
  30 s (:mod:`.hosted_tools`).
* :meth:`AgentInvoker.start_turn` — one turn: a transient ``AIAgent`` (the
  pattern Hermes' cron scheduler uses) on the kit's prompt plus where the
  manual is, in the profile's scope, under the time limit
  (:mod:`.turn_runner`), with its AgentChat call counts. The admission's
  ``done`` resolves when the turn's thread has ended.
* :meth:`AgentInvoker.start_run` — the same turn for single-run mode (the
  gate switched off: the turn reads the tray and records its own verdicts).

A turn that overran and does not end after Hermes' own interrupt
─────────────────────────────────────────────────────────────────
Its conversation (or invite) stays held until its thread really ends, while
the loop moves on: in one step on the loop's thread, with no await in
between, the shared :class:`~agentchatme.integration.TurnTracker` releases
the loop's entry — the loop's wait on it ends with nothing recorded and its
turn slot is freed at once (the next waiting turn starts) — and begins one
of this module's for the same conversation. When the thread ends, this
module applies the kit's rule on a fresh tray read (an answer it sent late is
recorded as ``reply``), then ends its entry and wakes the loop. So no second
turn starts for a conversation while the first may still send, and one stuck
turn does not stop the others. With
:data:`MAX_HUNG_TURNS` such turns still running, no new turn is admitted
(the cards stay on the tray; the server re-wakes), and :meth:`readiness`
says not-ready, so no gate call is spent while no turn can start.

A turn still running when its receive loop goes away (a supervisor rebuild)
is detached the same way: the loop records nothing for it, its conversation
stays held, and when its thread ends this module applies the kit's rule itself
(``turn_outcome`` / ``invite_outcome`` on a fresh tray read through the
runtime's client) before letting go — so a reply it sent is recorded and the
card is not gated into a second turn.

:meth:`AgentInvoker.when_idle` lets the runtime keep the leader lock until the
last turn thread has ended (a plugin reload while a turn runs: the reloaded
instance stays a follower meanwhile, so no second turn starts for it).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentchatme.integration import (
    Readiness,
    RunEnd,
    TrackedTurn,
    TurnAdmission,
    TurnEnd,
    TurnTracker,
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
)
from .hosted_tools import wait_for_hosted_tools
from .model_call import GateCallCancelled, GateLoop, build_call_model
from .profile_scope import (
    ProfileScopeError,
    profile_context,
    profile_turn_scope,
    resolve_hermes_home,
)
from .turn import invite_session_id, wake_session_id, with_manual
from .turn_runner import TurnLimits, TurnReport, run_turn

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentchatme.integration import RunRequest, TurnHandle, TurnRequest

    from .config import Config
    from .types import AgentIdentity

logger = logging.getLogger(__name__)

# Fallback model when Hermes' config helpers are unavailable.
_FALLBACK_MODEL = "claude-sonnet-4-6"

# The largest tray the server builds (the loop's own read is the same).
_TRAY_LIMIT = 50

# Turns that overran and did not end after the interrupt keep their thread.
# With this many still running, no new turn is admitted (the cards stay on
# the tray) until one of them ends: a wedged provider must not pile up threads.
MAX_HUNG_TURNS = 2


@dataclass
class _Turn:
    """One admitted turn, from admission until its thread has ended."""

    key: str
    tracked: TrackedTurn
    session_id: str
    loop: asyncio.AbstractEventLoop
    done: asyncio.Future[TurnEnd]
    lock: threading.Lock = field(default_factory=threading.Lock)
    reported: bool = False
    """``done`` has its result."""
    thread_done: bool = False
    """The turn's thread has ended (or never ran)."""
    host_handle: TurnHandle | None = None
    """Set while this module holds the conversation for a turn that overran
    (or was detached from its loop)."""
    detached: bool = False
    """This module records its outcome, once its thread has ended: its loop
    went away before the turn ended, or it overran and the loop let it go."""
    request: Any = None
    """The loop's :class:`TurnRequest` (card, anchor, category, invite)."""
    started_at: float = field(default_factory=time.time)
    final: tuple[TurnReport | None] | None = None
    """``(report,)`` once the turn's runner has returned."""
    finishing: bool = False
    """An owned turn's outcome is being recorded (exactly once)."""


class AgentInvoker:
    """The receive loop's runtime callbacks for one Hermes profile and account."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        runtime: Any,
        host_llm: Callable[[], Any] | None = None,
        tracker: TurnTracker | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        # The runtime this invoker belongs to (one per profile and account):
        # supplies the profile home; never resolved from a registry.
        self._runtime = runtime
        self._host_llm = host_llm
        self._host_llm_warned = False

        # getattr-with-default so light config doubles in tests construct cleanly.
        self._gate_enabled = bool(getattr(config, "reply_gate_enabled", DEFAULT_REPLY_GATE_ENABLED))
        self._gate_timeout_s = float(getattr(config, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S))
        idle_s = float(getattr(config, "turn_idle_s", DEFAULT_TURN_IDLE_S))
        max_s = float(getattr(config, "turn_max_s", DEFAULT_TURN_MAX_S))
        self._limits = TurnLimits(idle_s=idle_s or None, max_s=max_s or None)

        # Shared with the receive loop: its turns and this module's holds for
        # turns that overran, in one place (it survives a loop restart).
        self.tracker = tracker if tracker is not None else TurnTracker()
        self._stopped = threading.Event()
        # The gate's async model calls: one private loop for this invoker's life.
        self._gate_loop = GateLoop()
        self._turns_lock = threading.Lock()
        self._turns: dict[str, _Turn] = {}
        self._wake: Callable[[], None] | None = None
        self._run_finished: threading.Event | None = None
        self._idle_lock = threading.Lock()
        self._idle_callbacks: list[Callable[[], None]] = []

        self._state_lock = threading.Lock()
        self._heartbeat_at = time.time()
        self._current_turn: dict[str, Any] | None = None
        self._idle_since = time.time()
        self._last_turn: dict[str, Any] | None = None
        self.turns = 0
        self.runs = 0
        self.hung_total = 0

        # Hermes-side handles, resolved lazily on the first turn.
        self._session_db: Any = None
        self._resolved_hermes = False

    # -- wiring ---------------------------------------------------------------

    @property
    def gate_enabled(self) -> bool:
        """Gate-call mode (default) or, switched off, single-run mode."""
        return self._gate_enabled

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        """How to wake the current receive loop from any thread (set by the receiver)."""
        self._wake = wake

    def stop(self) -> None:
        """No new turns; a running one is interrupted by its watch; a gate
        call in flight is cancelled and the gate-call loop stopped."""
        self._stopped.set()
        self._gate_loop.close()

    @property
    def stopping(self) -> threading.Event:
        return self._stopped

    @property
    def hung_turns(self) -> int:
        with self._turns_lock:
            turns = list(self._turns.values())
        count = 0
        for turn in turns:
            with turn.lock:
                if turn.host_handle is not None and not turn.thread_done:
                    count += 1
        return count

    def status(self) -> dict[str, Any]:
        """The turn side of the health record (the receiver adds the loop's)."""
        with self._state_lock:
            current = self._current_turn
            return {
                "state": "turn" if current else "idle",
                # When the current state began: the running turn's start, else
                # when the last one ended (doctor's long-turn warning reads it).
                "since": current["started_at"] if current else self._idle_since,
                "heartbeat_at": self._heartbeat_at,
                "turn": dict(self._current_turn) if self._current_turn else None,
                "last_turn": dict(self._last_turn) if self._last_turn else None,
                "hung_turns": self.hung_turns,
                "max_hung_turns": MAX_HUNG_TURNS,
                "live_turns": self.live_turns(),
                "turns": self.turns,
                "runs": self.runs,
                "hung_total": self.hung_total,
            }

    def beat(self) -> None:
        with self._state_lock:
            self._heartbeat_at = time.time()

    def live_turns(self) -> int:
        """Turns not finished with (reply, invite, or a single-run run): a
        thread still running, a report the loop has not taken yet, or an
        owned turn whose outcome is not recorded yet — the runtime keeps the
        client and the leader lock until there are none."""
        with self._turns_lock:
            turns = list(self._turns.values())
        count = 0
        for turn in turns:
            with turn.lock:
                if not turn.thread_done or not turn.reported or turn.detached:
                    count += 1
        run = self._run_finished
        if run is not None and not run.is_set():
            count += 1
        return count

    def when_idle(self, callback: Callable[[], None]) -> bool:
        """Call ``callback`` once no turn thread is running: now (``True``)
        or when the last one ends (``False``; on that thread)."""
        with self._idle_lock:
            if self.live_turns() > 0:
                self._idle_callbacks.append(callback)
                return False
        callback()
        return True

    def _maybe_idle(self) -> None:
        with self._idle_lock:
            if not self._idle_callbacks or self.live_turns() > 0:
                return
            callbacks = list(self._idle_callbacks)
            self._idle_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("AgentInvoker: an idle callback raised")

    # -- the loop's callbacks ---------------------------------------------------

    @property
    def gate_timeout_s(self) -> float:
        """The gate call's own request timeout (``AGENTCHATME_GATE_TIMEOUT_S``)."""
        return self._gate_timeout_s

    def call_model(self, prompt: str, *, cancel: threading.Event | None = None) -> str:
        """The gate's tool-less completion on the agent's own model (a worker
        thread). ``cancel`` is the receive loop's handle, set when it stops
        waiting: with Hermes' async ``ctx.llm`` the request is then cancelled
        (:mod:`.model_call`)."""
        if self._stopped.is_set():
            raise GateCallCancelled("stopping")
        with self._scope():
            host_llm = self._host_llm_facade()
            model_call = build_call_model(
                main_runtime=self._gate_runtime() if host_llm is None else {},
                timeout_s=self._gate_timeout_s,
                host_llm=host_llm,
                gate_loop=self._gate_loop,
            )
            return model_call(prompt, cancel=cancel)

    def readiness(self) -> Readiness:
        """Could a turn started now reach AgentChat? (a worker thread)"""
        hung = self.hung_turns
        if hung >= MAX_HUNG_TURNS:
            # No turn could be admitted: do not spend a gate call on it.
            return Readiness(
                ready=False,
                reason=f"{hung} earlier turns overran and have not ended — no turn can start",
            )
        with profile_context(self._home()):
            held = wait_for_hosted_tools(label="turn", stopped=self._stopped)
        if held is None:
            return Readiness(ready=False, reason="stopping")
        if not held:
            return Readiness(
                ready=False, reason="the hosted AgentChat tools are not registered in Hermes yet"
            )
        return Readiness(ready=True)

    async def start_turn(self, request: TurnRequest) -> TurnAdmission:
        """Admit one reply or invite turn; ``done`` resolves when its thread has ended."""
        if self._stopped.is_set():
            return TurnAdmission(started=False, error="stopping")
        hung = self.hung_turns
        if hung >= MAX_HUNG_TURNS:
            logger.error(
                "AgentInvoker: %d earlier turn(s) overran and have not ended — no new turn until "
                "one does (the cards stay on the tray)",
                hung,
            )
            return TurnAdmission(started=False, error=f"{hung} earlier turns are still running")
        tracked = _tracked(request)
        loop = asyncio.get_running_loop()
        turn = _Turn(
            key=tracked.key,
            tracked=tracked,
            session_id=(
                invite_session_id(str(tracked.invite_id))
                if tracked.kind == "invite"
                else wake_session_id()
            ),
            loop=loop,
            done=loop.create_future(),
            request=request,
        )
        with self._turns_lock:
            self._turns[turn.key] = turn
        thread = threading.Thread(
            target=self._turn_thread,
            args=(with_manual(request.prompt), turn),
            name=f"agentchat-turn-start-{turn.session_id[-6:]}",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            self._forget(turn)
            return TurnAdmission(started=False, error=f"could not start a thread: {exc}")
        logger.info("AgentInvoker: %s turn start session=%s (%s)", tracked.kind, turn.session_id, turn.key)
        return TurnAdmission(started=True, done=turn.done)

    def start_run(self, request: RunRequest) -> RunEnd:
        """Single-run mode (the gate off): one turn that reads the tray and
        records its own verdicts. Runs in a worker thread until the turn
        ends or overruns; never a second while the first's thread lives."""
        previous = self._run_finished
        if previous is not None and not previous.is_set():
            return RunEnd(status="not_started", detail="the previous run is still running")
        finished = threading.Event()
        self._run_finished = finished
        def run_ended() -> None:
            finished.set()
            self._maybe_idle()

        report, reason = self._run_blocking(
            prompt=with_manual(request.prompt), session_id=wake_session_id(), on_finished=run_ended
        )
        if report is None:
            run_ended()
            return RunEnd(status="not_started", detail=reason)
        self.runs += 1
        if report.status == "completed":
            return RunEnd(status="completed")
        return RunEnd(status="failed", detail=report.detail)

    # -- turns --------------------------------------------------------------------

    def _turn_thread(self, prompt: str, turn: _Turn) -> None:
        report, reason = self._run_blocking(
            prompt=prompt, session_id=turn.session_id, on_finished=lambda: self._thread_ended(turn)
        )
        with turn.lock:
            if report is None:
                turn.thread_done = True
            turn.final = (report,)
            # Already ours to record (detached): nobody else will.
            owned = turn.detached
            finish_here = owned and self._claim_finish(turn)
        if report is None:
            self._maybe_idle()
        if owned:
            if finish_here:
                self._finish_detached(turn, report)
            return
        try:
            turn.loop.call_soon_threadsafe(self._report_turn, turn, report, reason)
        except RuntimeError:
            # The loop that admitted the turn is gone (it restarted or stopped).
            self._report_without_loop(turn, report)

    @staticmethod
    def _claim_finish(turn: _Turn) -> bool:
        """Under ``turn.lock``: may the caller record this owned turn's
        outcome now? Once its runner has returned and its thread has really
        ended — exactly once."""
        if turn.finishing or turn.final is None or not turn.thread_done:
            return False
        turn.finishing = True
        return True

    def _report_turn(self, turn: _Turn, report: TurnReport | None, reason: str | None) -> None:
        """On the loop's thread: hand the loop what became of the turn."""
        with turn.lock:
            owned = turn.detached
            go = owned and self._claim_finish(turn)
        if owned:
            # Detached while the report was on its way: record it ourselves,
            # off the loop's thread (it reads the tray and records over HTTP).
            if go:
                self._finish_in_thread(turn, report)
            return
        with turn.lock:
            if turn.reported:
                return
            turn.reported = True
            if report is None:
                end = TurnEnd(status="failed", detail=f"not started: {reason}")
            elif report.status == "timeout" and not turn.thread_done:
                # Overran and still running: from the loop's entry to ours, in
                # one step with no await in between, so no cycle sees the
                # conversation free. The release ends the loop's wait (nothing
                # recorded) and frees its slot; this module records the
                # outcome once the thread has really ended (it may still send).
                self.tracker.release(turn.key)
                turn.host_handle = self.tracker.begin(turn.tracked)
                turn.detached = True
                self.hung_total += 1
                end = TurnEnd(
                    status="failed",
                    agentchat_calls=report.calls,
                    detail=f"overran ({report.detail}); its thread is still running",
                )
            elif report.status == "timeout":
                # Overran, and its thread has ended since: a failed turn. (Not
                # ``report.turn_end()``: the thread's end callback runs a moment
                # before the report's ``finished`` is set.)
                end = TurnEnd(status="failed", agentchat_calls=report.calls, detail=report.detail)
            else:
                end = report.turn_end()
            finished = turn.thread_done and not turn.detached
        if not turn.done.done():
            turn.done.set_result(end)
        if finished:
            self._forget(turn)
            self._maybe_idle()

    def _thread_ended(self, turn: _Turn) -> None:
        """On the turn's own thread, when it really ends."""
        with turn.lock:
            turn.thread_done = True
            owned = turn.detached
            go = owned and self._claim_finish(turn)
            reported = turn.reported
        if owned:
            # Ours to record; the hold — and the runtime's client and leader
            # lock (``live_turns``) — stay until then: idle is told only by
            # :meth:`_finish_detached`, once the outcome is recorded.
            if go:
                self._finish_in_thread(turn, turn.final[0] if turn.final else None)
            return
        if reported:
            self._forget(turn)
        self._maybe_idle()

    def detach_turns(self) -> None:
        """The receive loop is going away (restart or stop), on its thread.

        Every turn not yet reported is reported now as failed, so the loop
        records nothing for it, and this module takes over its hold: the
        conversation stays held until the turn's thread has ended and its
        outcome has been recorded here (:meth:`_finish_detached`), so a reply
        it sent is recorded and the card is not gated into a second turn.
        """
        with self._turns_lock:
            turns = list(self._turns.values())
        for turn in turns:
            with turn.lock:
                if turn.reported:
                    continue
                turn.reported = True
                turn.detached = True
                self.tracker.release(turn.key)
                turn.host_handle = self.tracker.begin(turn.tracked)
                # Its runner may have returned already, its report never to
                # reach the loop (closing): record it now if it has ended.
                go = self._claim_finish(turn)
                final = turn.final
            if not turn.done.done():
                turn.done.set_result(
                    TurnEnd(status="failed", detail="the receive loop stopped before the turn ended")
                )
            if go and final is not None:
                self._finish_in_thread(turn, final[0])

    def _report_without_loop(self, turn: _Turn, report: TurnReport | None) -> None:
        """The loop that admitted the turn is closed: record its outcome here."""
        with turn.lock:
            if not turn.reported:
                # Not detached (the loop vanished without its teardown): take
                # over its entry so the outcome is recorded before letting go.
                turn.reported = True
                turn.detached = True
                self.tracker.release(turn.key)
                turn.host_handle = self.tracker.begin(turn.tracked)
            owned = turn.detached
            go = owned and self._claim_finish(turn)
        if go:
            self._finish_detached(turn, report)
        elif not owned:
            self._wake_loop()

    def _finish_in_thread(self, turn: _Turn, report: TurnReport | None) -> None:
        threading.Thread(
            target=self._finish_detached, args=(turn, report), name="agentchat-turn-record", daemon=True
        ).start()

    def _finish_detached(self, turn: _Turn, report: TurnReport | None) -> None:
        """An owned turn's thread has ended: apply the kit's rule through the
        runtime's client (a fresh tray read) — a turn that failed after
        sending its answer records ``reply`` too — then let go of the
        conversation."""
        try:
            if report is not None:
                self._record_detached(turn, report)
            else:
                logger.info("AgentInvoker: detached turn %s never started — nothing recorded", turn.session_id)
        except Exception as exc:
            logger.warning(
                "AgentInvoker: the outcome of detached turn %s was not recorded (%s) — the card "
                "stays on the tray and the server re-wakes",
                turn.session_id,
                exc,
            )
        finally:
            with turn.lock:
                handle = turn.host_handle
                turn.host_handle = None
            if handle is not None:
                self.tracker.end(handle)
            self._forget(turn)
            self._wake_loop()
            self._maybe_idle()

    def _record_detached(self, turn: _Turn, report: TurnReport) -> None:
        client = getattr(self._runtime, "client", None)
        request = turn.request
        if client is None or request is None:
            return
        fresh = client.inbox.tray(wait_ms=0, limit=_TRAY_LIMIT, include_invites=True)
        end = TurnEnd(
            status="completed" if report.status == "completed" else "failed",
            agentchat_calls=report.calls,
            detail=report.detail,
        )
        if request.kind == "invite":
            invited = invite_outcome(turn=end, invite=request.invite or {}, fresh_tray=fresh)
            recorded = apply_invite_outcome(client, invited)
            reason: str = invited.reason
            action: str = invited.action
        else:
            replied = turn_outcome(
                turn=end,
                turn_started_at=datetime.fromtimestamp(turn.started_at, tz=timezone.utc),
                gate_card=request.card or {},
                category=request.category,
                fresh_tray=fresh,
            )
            recorded = apply_outcome(client, replied)
            reason, action = replied.reason, replied.action
        logger.info(
            "AgentInvoker: detached turn %s (%s): %s → %s (recorded=%s)",
            turn.session_id,
            end.status,
            reason,
            action,
            recorded,
        )

    def _forget(self, turn: _Turn) -> None:
        with self._turns_lock:
            if self._turns.get(turn.key) is turn:
                del self._turns[turn.key]

    def _wake_loop(self) -> None:
        wake = self._wake
        if wake is None:
            return
        try:
            wake()
        except Exception:
            logger.debug("AgentInvoker: waking the receive loop raised", exc_info=True)

    def _run_blocking(
        self, *, prompt: str, session_id: str, on_finished: Callable[[], None]
    ) -> tuple[TurnReport | None, str | None]:
        """Build the agent and run the turn, in the profile's scope (a worker
        thread). ``(None, reason)`` when it could not start."""
        if self._stopped.is_set():
            return None, "stopping"
        try:
            with self._scope():
                self._ensure_hermes_resolved()
                agent = self._build_agent(session_id)
                if agent is None:
                    return None, "the Hermes agent could not be built"
                with self._state_lock:
                    self._current_turn = {"session_id": session_id, "started_at": time.time()}
                try:
                    report = run_turn(
                        agent,
                        prompt,
                        session_id=session_id,
                        limits=self._limits,
                        stopped=self._stopped,
                        on_finished=on_finished,
                        on_poll=self.beat,
                    )
                finally:
                    with self._state_lock:
                        self._current_turn = None
                        self._idle_since = time.time()
        except ProfileScopeError as exc:
            logger.warning("AgentInvoker: turn %s not started — %s", session_id, exc)
            return None, f"profile scope unavailable: {exc}"
        except Exception as exc:
            logger.exception("AgentInvoker: turn %s could not run", session_id)
            return None, f"{type(exc).__name__}: {exc}"
        self.turns += 1
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
        logger.info(
            "AgentInvoker: turn %s %s (%s, %.1fs)%s",
            session_id,
            report.status,
            report.calls_text(),
            report.duration_s,
            f" — {report.detail}" if report.detail and report.status != "completed" else "",
        )
        return report, None

    # -- Hermes integration (lazy imports) ----------------------------------

    def _home(self) -> Path:
        home = getattr(self._runtime, "hermes_home", None)
        if home is None:
            home = getattr(self._config, "hermes_home", None)
        return Path(home) if home is not None else resolve_hermes_home()

    @contextlib.contextmanager
    def _scope(self) -> Iterator[None]:
        """The profile's home and runtime scope — the one the gateway uses for
        this profile's own turns — so its provider key and secrets are its
        own, never the launch profile's. :class:`ProfileScopeError` when a
        multiplexed scope cannot be entered: nothing then runs unscoped."""
        home = self._home()
        with profile_context(home), profile_turn_scope(home):
            yield

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


# ─── pure helpers ───────────────────────────────────────────────────────────


def _tracked(request: TurnRequest) -> TrackedTurn:
    """The tracker entry the loop began for this request."""
    if request.kind == "invite":
        invite = request.invite or {}
        return TrackedTurn.invite(str(invite.get("invite_id", "")))
    return TrackedTurn.reply(str(request.conversation_id or ""), request.anchor)


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
