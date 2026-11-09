"""Receiving: the SDK kit's receive loop, hosted on a supervised thread.

One daemon thread runs a private asyncio loop for one Hermes profile and
account (the leader-lock holder). On it:

* the SDK's :class:`~agentchatme.RealtimeClient` — acks on receipt, the
  ``ack``, ``heartbeat`` and ``rewake`` capabilities, the HTTP client for the
  offline drain and gap recovery; reconnect, ordering, dedupe, the connect
  budget's backoff and the heartbeat watchdog are the SDK's;
* the kit's :class:`~agentchatme.integration.TextsCache` — the served texts,
  file-backed, refreshed daily; changes go to the runtime (SOUL.md);
* the kit's :class:`~agentchatme.integration.ReceiveLoop` in gate-call mode
  (single-run mode when the gate is switched off): wakes, one cycle per
  burst, the tray with its invites, the gate call, one turn at a time, the
  D5 rule after each turn, D5 pacing, no retry ladder (the server re-wakes).
  The Hermes side — the model call, the turn — is :mod:`.agent_invoker`.

Supervision: if the loop's main coroutine dies the whole stack is rebuilt
after a backoff (1 s doubling to 60 s, reset after a stable minute). A socket
that stays disconnected for 150 s is replaced the same way — but never while
the SDK is waiting out a delay the server asked for (the connect budget) or
retrying slowly after an auth rejection. Turns that are still running when
the loop goes away keep their conversations held until their threads end
(:meth:`.agent_invoker.AgentInvoker.detach_turns`).

Health: the socket's state and the receive side's (alive, the kit's recent
cycle outcomes, turns running or waiting, overrun turns) go to the account's
health record (:mod:`.ws_health`) every minute and on every change; ``hermes
agentchat doctor`` reads it (three bad cycles in a row are an error).

Deadlines are the kit's: one gate call (60 s, or twice
``AGENTCHATME_GATE_TIMEOUT_S`` when longer; the Hermes call is cancelled) and
one cycle (10 min, not counting turn time; then abandoned, the next wake
retries).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from agentchatme import gate

from .client_identity import hermes_client_identity
from .profile_scope import profile_context
from .turn import MCP_SERVER_NAME
from .ws_health import WSHealthState, write_ws_health

if TYPE_CHECKING:
    from pathlib import Path

    from agentchatme import AsyncAgentChatClient, RealtimeClient
    from agentchatme.integration import ReceiveLoop, TextsCache
    from agentchatme.types import Texts

    from .agent_invoker import AgentInvoker
    from .config import Config
    from .types import AgentIdentity
    from .ws_health import HealthBoard

logger = logging.getLogger(__name__)

_THREAD_NAME = "agentchat-receiver"
_STOP_GRACE_SECONDS = 5.0
_HEARTBEAT_INTERVAL_S = 60.0
_DISCONNECTED_RESTART_AFTER_S = 150.0
_SUPERVISOR_BACKOFF_BASE_S = 1.0
_SUPERVISOR_BACKOFF_MAX_S = 60.0
_STABLE_CONNECTION_S = 60.0

TextsFactory = Callable[[Any], "TextsCache"]

# The kit's last cycle summaries kept in the health record (doctor alarms on
# a run of bad ones; ``ws_health.BAD_CYCLE_OUTCOMES``), each failure text cut.
_RECENT_CYCLES_KEPT = 20
_FAILURE_TEXT_MAX = 200


def _restart_after_s(rt_client: Any) -> float:
    """How long a disconnected socket may stay down before the receiver
    replaces the stack (the guard against a wedged client).

    Never while the SDK is honouring a delay the server asked for (the
    connect budget, HTTP 429 or close 1013 with a retry hint: contract v1
    D8 — a fresh client would reconnect at once, against the server's word)
    or retrying slowly after an auth rejection (``blocked``: a fresh client
    would retry at once). The SDK reconnects by itself in both.
    """
    limit = _DISCONNECTED_RESTART_AFTER_S
    state = getattr(rt_client, "state", None)
    if state == "blocked":
        return float("inf")
    budget_ms = getattr(rt_client, "rate_limited_ms", None)
    if isinstance(budget_ms, (int, float)) and not isinstance(budget_ms, bool) and budget_ms > 0:
        # The SDK waits up to 10 % past the server's delay; then one attempt.
        limit = max(limit, budget_ms / 1000.0 * 1.1 + _DISCONNECTED_RESTART_AFTER_S)
    return limit


def _quiet_window(config: Any) -> tuple[float | None, float | None]:
    """``AGENTCHATME_WAKE_DEBOUNCE_S`` as the loop's coalescing window: the
    quiet time after the last wake (default 2 s) and the longest a burst may
    hold a cycle back (three times that, at least the kit's 6 s)."""
    from agentchatme.integration import DEFAULT_MAX_MS

    seconds = getattr(config, "wake_debounce_s", None)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return None, None
    quiet_ms = float(seconds) * 1000.0
    return quiet_ms, max(float(DEFAULT_MAX_MS), quiet_ms * 3.0)


def _cycle_record(summary: Any) -> dict[str, Any]:
    """One kit ``CycleSummary`` for the health record: when, what, why."""
    failure = getattr(summary, "failure", None)
    return {
        "at": getattr(summary, "at", None),
        "outcome": getattr(summary, "outcome", None),
        "failure": None if failure is None else str(failure)[:_FAILURE_TEXT_MAX],
    }


def _gate_call_deadline_ms(gate_timeout_s: float) -> float:
    """The kit's bound on one gate call: its default (60 s), or twice the
    call's own request timeout (``AGENTCHATME_GATE_TIMEOUT_S``; Hermes
    retries a transient failure once) when that is longer — a slow model the
    operator allowed for is not cut off."""
    from agentchatme.integration.receive_loop import GATE_CALL_TIMEOUT_MS

    return max(float(GATE_CALL_TIMEOUT_MS), 2000.0 * float(gate_timeout_s))


class Receiver:
    """Owns the receive loop for one account. Single-use: :meth:`start`, :meth:`stop`."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        invoker: AgentInvoker,
        texts_factory: TextsFactory,
        on_texts: Callable[[Texts], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        hermes_home: Path | None = None,
        health: HealthBoard | None = None,
        health_path: Path | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        self._invoker = invoker
        self._texts_factory = texts_factory
        self._on_texts = on_texts
        self._on_exit = on_exit
        self._hermes_home = hermes_home
        self._health = health
        self._health_path = health_path

        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._rt: RealtimeClient | None = None
        self._receive_loop: ReceiveLoop | None = None
        self._http: AsyncAgentChatClient | None = None

        self._disconnected_since: float | None = None
        self._last_connected_at: float | None = None
        self._restart_attempt = 0
        self.restarts = 0
        self._last_error: str | None = None
        self._restarting = False
        self._restart_since: float | None = None
        self._stopped_at: float | None = None
        self._rewake_warned = False
        self._events: dict[str, int] = {}
        # The last loop's ``recent_cycles``, kept across a stack restart.
        self._recent_cycles: list[dict[str, Any]] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)
            self._thread.start()
        self._started.wait(timeout=2.0)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped_at = time.time()
        self._stopped.set()
        self._invoker.stop()
        loop = self._loop
        event = self._stop_event
        if loop is not None and event is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=_STOP_GRACE_SECONDS)
            if thread.is_alive():
                logger.warning("Receiver: thread did not exit within %.1fs", _STOP_GRACE_SECONDS)

    @property
    def alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def wait_exited(self, timeout: float) -> bool:
        """Whether the receiver's thread has exited, waiting up to ``timeout``
        (never from that thread itself)."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def wake(self) -> None:
        """A wake from outside the socket (thread-safe)."""
        loop = self._loop
        receive_loop = self._receive_loop
        if loop is None or receive_loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(receive_loop.wake)

    def worker_status(self) -> dict[str, Any]:
        """The receive side for the health record: the invoker's turn state
        plus the loop's last cycle and turn slots."""
        status = self._invoker.status()
        receive_loop = self._receive_loop
        loop_status: dict[str, Any] | None = None
        if receive_loop is not None:
            try:
                snapshot = receive_loop.status()
                cycle = snapshot.last_cycle
                loop_status = {
                    "last_cycle": None
                    if cycle is None
                    else {"at": cycle.at, "outcome": cycle.outcome, "failure": cycle.failure},
                    "turns_active": snapshot.counters.get("turns_active"),
                    "turns_waiting": snapshot.counters.get("turns_waiting"),
                    "outcomes": snapshot.counters.get("outcomes"),
                    "coalescer": snapshot.coalescer.get("state"),
                    "socket": snapshot.socket,
                }
                self._recent_cycles = [_cycle_record(c) for c in snapshot.recent_cycles][-_RECENT_CYCLES_KEPT:]
                if status["state"] == "idle" and snapshot.coalescer.get("state") == "running":
                    status["state"] = "cycle"
            except Exception:
                logger.debug("Receiver: loop status raised", exc_info=True)
        loop_status = dict(loop_status or {})
        loop_status["recent_cycles"] = list(self._recent_cycles)
        if self._restarting:
            status["state"] = "restart_wait"
            status["since"] = self._restart_since
        if self._stopped.is_set():
            status["state"] = "stopped"
            status["since"] = self._stopped_at
        status.update(
            {
                "alive": self.alive,
                "restarts": self.restarts,
                "last_error": self._last_error,
                "loop": loop_status,
            }
        )
        return status

    # -- the supervised thread -----------------------------------------------

    def _run(self) -> None:
        # Everything on this thread — the texts listeners (SOUL.md), the
        # loop's tray reads — runs in this profile's context.
        scope = profile_context(self._hermes_home) if self._hermes_home else contextlib.nullcontext()
        with scope:
            self._supervise()

    def _supervise(self) -> None:
        first = True
        while not self._stopped.is_set():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            if first:
                self._started.set()
                first = False
            started = time.monotonic()
            error: BaseException | None = None
            fatal = False
            try:
                self._write_health("starting")
                loop.run_until_complete(self._main())
                if not self._stopped.is_set():
                    error = RuntimeError("the receive loop exited unexpectedly")
            except (SystemExit, KeyboardInterrupt) as exc:
                # Not a crash to retry: stop cleanly and hand the account
                # back (the runtime releases the leader lock once turns end).
                error = exc
                fatal = True
                logger.error("Receiver: %s in the receive loop — stopping", type(exc).__name__)
            except BaseException as exc:
                # CancelledError and the like: whatever escaped, the thread must
                # not die holding the account. Rebuild the loop.
                error = exc
                if not self._stopped.is_set():
                    logger.error("Receiver: the receive loop crashed", exc_info=(type(exc), exc, exc.__traceback__))
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    with contextlib.suppress(Exception):
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._stop_event = None
                self._loop = None
                self._receive_loop = None
                asyncio.set_event_loop(None)
                with contextlib.suppress(Exception):
                    loop.close()
            if fatal:
                self._stopped_at = time.time()
                self._stopped.set()
                self._invoker.stop()
                self._last_error = f"{type(error).__name__}: {error}"
                if self._on_exit is not None:
                    try:
                        self._on_exit()
                    except Exception:
                        logger.exception("Receiver: the exit callback raised")
                break
            if self._stopped.is_set():
                break
            if time.monotonic() - started >= _STABLE_CONNECTION_S:
                self._restart_attempt = 0
            self._restart_attempt += 1
            self.restarts += 1
            ceiling = min(
                _SUPERVISOR_BACKOFF_MAX_S,
                _SUPERVISOR_BACKOFF_BASE_S * (2 ** (self._restart_attempt - 1)),
            )
            delay = random.uniform(ceiling * 0.8, ceiling * 1.2)
            self._last_error = f"{type(error).__name__}: {error}" if error is not None else None
            self._restarting = True
            self._restart_since = time.time()
            self._write_health("restart_wait", last_error=self._last_error)
            logger.warning(
                "Receiver: restarting the receive loop in %.1fs (attempt %d, %s)",
                delay,
                self._restart_attempt,
                self._last_error or "unknown",
            )
            self._stopped.wait(delay)
            self._restarting = False
        self._write_health("stopped")
        logger.info("Receiver: stopped")

    async def _main(self) -> None:
        from agentchatme import AsyncAgentChatClient
        from agentchatme.integration import ReceiveLoop

        self._stop_event = asyncio.Event()
        self._http = AsyncAgentChatClient(
            api_key=self._config.api_key,
            base_url=self._config.api_base,
            client_identity=hermes_client_identity(),
        )
        rt = self._build_realtime_client()
        self._rt = rt
        rt.on_connect(self._on_connect)
        rt.on_disconnect(self._on_disconnect)
        rt.on_error(self._on_error)

        # The texts: one cache per loop (its timer lives on this loop). Its
        # changes go to the runtime, which places the identity line.
        texts = self._texts_factory(self._http)
        if self._on_texts is not None:
            on_texts = self._on_texts
            texts.on_change(lambda served, _source: on_texts(served))

        invoker = self._invoker
        quiet_ms, max_ms = _quiet_window(self._config)
        receive_loop = ReceiveLoop(
            self._http,
            texts,
            "gate_call" if invoker.gate_enabled else "single_run",
            call_model=invoker.call_model if invoker.gate_enabled else None,
            start_turn=invoker.start_turn if invoker.gate_enabled else None,
            start_run=None if invoker.gate_enabled else invoker.start_run,
            socket=rt,
            handle=self._identity.handle,
            tool_namer=gate.mcp_tool_namer(MCP_SERVER_NAME),
            readiness=invoker.readiness,
            on_event=self._on_event,
            max_concurrent_turns=1,
            turn_tracker=invoker.tracker,
            quiet_ms=quiet_ms,
            max_ms=max_ms,
            gate_call_timeout_ms=_gate_call_deadline_ms(invoker.gate_timeout_s),
        )
        self._receive_loop = receive_loop
        invoker.set_wake(self.wake)
        self._disconnected_since = time.monotonic()
        try:
            await texts.start()
            await receive_loop.start()
            heartbeat = asyncio.ensure_future(self._heartbeat_loop())
            stop = asyncio.ensure_future(self._stop_event.wait())
            try:
                done, _ = await asyncio.wait({heartbeat, stop}, return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    await heartbeat  # the wedge guard's exception reaches the supervisor
            finally:
                heartbeat.cancel()
                stop.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(heartbeat, stop)
        finally:
            invoker.set_wake(None)
            # Turns still running keep their conversations held until their
            # threads end; the loop records nothing for them.
            invoker.detach_turns()
            with contextlib.suppress(Exception):
                await receive_loop.stop()
            texts.stop()
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._rt = None
            self._http = None

    def _build_realtime_client(self, **overrides: Any) -> RealtimeClient:
        """The SDK's socket: acks on receipt, the ``rewake`` and ``heartbeat``
        capabilities, the HTTP client for the drain and gap recovery,
        Hermes' client identity. ``overrides`` is a test seam."""
        from agentchatme import RealtimeClient

        options: dict[str, Any] = {
            "api_key": self._config.api_key,
            "base_url": self._config.ws_url,
            "client": self._http,
            "ack_on_receipt": True,
            # ``rewake``: the server's unanswered re-wake (contract v1 D3)
            # comes to this socket, so there is no local retry ladder.
            # ``heartbeat``: the server's 45 s frame feeds the SDK's watchdog.
            "capabilities": {"rewake": True, "heartbeat": True},
            "client_identity": hermes_client_identity(),
        }
        options.update(overrides)
        return RealtimeClient(**options)

    async def _heartbeat_loop(self) -> None:
        """Every minute: the health record, and the wedge guard."""
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            except asyncio.CancelledError:
                return
            self._invoker.beat()
            rt = self._rt
            try:
                connected = bool(rt.is_connected) if rt is not None else False
            except Exception:
                connected = False
            if connected:
                self._disconnected_since = None
                self._write_health("connected")
                continue
            if self._disconnected_since is None:
                self._disconnected_since = time.monotonic()
            down_for = time.monotonic() - self._disconnected_since
            self._write_health("reconnecting")
            if down_for >= _restart_after_s(rt):
                raise RuntimeError(f"the socket stayed disconnected for {down_for:.0f}s")

    # -- socket and loop events --------------------------------------------------

    def _on_connect(self) -> None:
        self._disconnected_since = None
        self._last_connected_at = time.time()
        self._write_health("connected")
        logger.info("Receiver: connected as @%s", self._identity.handle)
        self._check_rewake()

    def _check_rewake(self) -> None:
        """Say once if the server did not take the ``rewake`` capability:
        without it a cycle that could not finish leaves its cards waiting for
        the next message (there is no retry ladder here)."""
        negotiated = getattr(self._rt, "capabilities", None) if self._rt is not None else None
        if not isinstance(negotiated, (set, frozenset)) or "rewake" in negotiated:
            return
        if self._rewake_warned:
            return
        self._rewake_warned = True
        logger.warning(
            "Receiver: the server did not take the rewake capability (negotiated: %s) — cards a "
            "cycle could not finish wait for the next message instead of the server's re-wake",
            ", ".join(sorted(negotiated)) or "none",
        )

    def _on_disconnect(self, info: dict[str, Any]) -> None:
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
        reason = info.get("reason")
        self._write_health("reconnecting", last_error=str(reason) if reason else None)
        logger.info(
            "Receiver: disconnected (code=%s reason=%s initiator=%s)",
            info.get("code"),
            reason,
            info.get("initiator"),
        )

    def _on_error(self, exc: BaseException) -> None:
        logger.warning("Receiver: socket error: %s", exc)

    def _on_event(self, event: dict[str, Any]) -> None:
        """The loop's events: counted, logged without content, health on cycle ends."""
        kind = str(event.get("type", "?"))
        self._events[kind] = self._events.get(kind, 0) + 1
        if kind == "wake":
            logger.info(
                "Receiver: wake — %s%s%s",
                event.get("source"),
                f" {event['reason']}" if event.get("reason") else "",
                f" conv={event['conversation_id']}" if event.get("conversation_id") else "",
            )
        elif kind == "cycle":
            summary = event.get("summary")
            logger.info(
                "Receiver: cycle %s cards=%s invites=%s reply=%s dismiss=%s defer=%s turns=%s "
                "queued=%s refused=%s paced=%s held=%s%s",
                getattr(summary, "outcome", "?"),
                getattr(summary, "cards", 0),
                getattr(summary, "invites", 0),
                getattr(summary, "reply", 0),
                getattr(summary, "dismiss", 0),
                getattr(summary, "defer", 0),
                getattr(summary, "turns_started", 0),
                getattr(summary, "turns_queued", 0),
                getattr(summary, "turns_refused", 0),
                getattr(summary, "paced", 0),
                getattr(summary, "held_by_turns", 0),
                f" failure={getattr(summary, 'failure', None)!r}" if getattr(summary, "failure", None) else "",
            )
            self._refresh_health()
        elif kind in ("turn_outcome", "invite_outcome"):
            outcome = event.get("outcome")
            logger.info(
                "Receiver: %s %s → %s (recorded=%s)",
                kind.replace("_", " "),
                getattr(outcome, "reason", "?"),
                getattr(outcome, "action", "?"),
                event.get("recorded"),
            )
        elif kind == "turn_refused":
            logger.warning(
                "Receiver: %s turn for %s not started: %s", event.get("kind"), event.get("id"), event.get("error")
            )
        elif kind == "error":
            self._last_error = str(event.get("error"))[-500:]
            logger.warning("Receiver: %s", event.get("error"))

    # -- health ----------------------------------------------------------------

    def _write_health(self, state: WSHealthState, *, last_error: str | None = None) -> None:
        try:
            if self._health is not None:
                self._health.socket(
                    state,
                    restart_attempt=self._restart_attempt,
                    last_error=last_error,
                    last_connected_at=self._last_connected_at,
                )
                return
            write_ws_health(
                handle=self._identity.handle,
                state=state,
                restart_attempt=self._restart_attempt,
                last_error=last_error,
                last_connected_at=self._last_connected_at,
                path=self._health_path,
                worker=self.worker_status(),
            )
        except Exception:
            # Health is diagnostics; it never becomes a receive-path dependency.
            logger.debug("Receiver: health record write failed", exc_info=True)

    def _refresh_health(self) -> None:
        if self._health is None:
            return
        try:
            self._health.refresh()
        except Exception:
            logger.debug("Receiver: health refresh failed", exc_info=True)
