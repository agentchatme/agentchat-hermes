"""One Hermes turn, run under a time limit, and what it did on AgentChat.

Why a time limit
────────────────
``AIAgent.run_conversation`` blocks until the turn ends. Run inline on the
integration's worker, one turn that never ends (a provider stream that
stalls, a tool that hangs) stops every later wake for the account while the
socket stays connected and the health record says so. Hermes guards its own
turns the same way this module does: the gateway and the cron scheduler run
``run_conversation`` on a worker thread, watch the agent's activity, and on
overrun ask the agent to stop with ``AIAgent.hard_interrupt(message,
tool_reason=...)`` (``gateway/run_turn.py``, ``cron/scheduler.py``). Hermes
has no "run with a timeout" call a plugin can use, so the watch lives here.

How it works
────────────
* The turn runs on its own daemon thread, inside a copy of the caller's
  context: the profile's HERMES_HOME override, secret scope and terminal
  policy are ContextVars and reach the turn thread that way, as Hermes'
  own turn workers receive them.
* Two limits, both read from the monotonic clock, so a host that sleeps
  mid-turn is not charged for the time asleep:

  - **idle** — no activity for ``idle_s``. Activity is the agent's own
    ``get_activity_summary()``: any change in its last-activity stamp,
    API-call count or current tool counts. An agent without that method is
    charged its whole run against this limit.
  - **ceiling** — ``max_s`` since the start, however busy the turn is.

* On overrun (or when the integration stops) the agent is asked to stop
  with ``hard_interrupt`` (``interrupt`` on an agent without it), and the
  thread gets ``grace_s`` to end. Either way the turn is reported
  ``timeout`` — a failed turn: nothing is recorded for it.
* A thread that does not end within the grace keeps running in the
  background; :attr:`TurnReport.finished` is set only when it really ends.
  The caller keeps that turn's conversations marked as running until then
  (:class:`RunningTurns`), so no second turn starts for them while the
  first one may still send.

What the turn did on AgentChat (the reply rule's inputs)
────────────────────────────────────────────────────────
The transcript Hermes returns (``result["messages"]``) lists every tool
call; :func:`count_agentchat_calls` counts the hosted AgentChat ones and
how many of them failed. :meth:`TurnReport.turn_end` hands that to the
kit's reply rule (``agentchatme.integration.turn_outcome``), which records
nothing for a turn that made no AgentChat call, or whose calls all failed:
it could not look.
"""
from __future__ import annotations

import contextvars
import inspect
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from agentchatme.integration import AgentChatCallCount, TurnEnd

from .config import DEFAULT_TURN_IDLE_S, DEFAULT_TURN_MAX_S
from .turn import MCP_SERVER_NAME, MCP_TOOL_PREFIX

logger = logging.getLogger(__name__)

# The two limits' defaults live in :mod:`.config` (``AGENTCHATME_TURN_IDLE_TIMEOUT_S``,
# ``AGENTCHATME_TURN_MAX_S``). Time an interrupted turn gets to end before
# it is left to finish alone:
DEFAULT_INTERRUPT_GRACE_S = 30.0
DEFAULT_POLL_S = 1.0

# The fixed category Hermes shows in the interrupted tool's output and in
# its turn-exit reason (``tool_reason``); never message content.
INTERRUPT_TOOL_REASON = "agentchat turn time limit"
INTERRUPT_STOP_REASON = "agentchat integration stopping"

TurnStatus = Literal["completed", "failed", "timeout"]


@dataclass(frozen=True)
class TurnLimits:
    """How long one turn may run. ``0`` (or ``None``) switches a limit off."""

    idle_s: float | None = DEFAULT_TURN_IDLE_S
    max_s: float | None = DEFAULT_TURN_MAX_S
    grace_s: float = DEFAULT_INTERRUPT_GRACE_S
    poll_s: float = DEFAULT_POLL_S


@dataclass(frozen=True)
class TurnReport:
    """What became of one turn.

    ``finished`` is set once the turn's thread has really ended — at once
    for ``completed`` and ``failed``, possibly later (or never) for a
    ``timeout`` the interrupt did not stop within the grace.
    """

    status: TurnStatus
    session_id: str
    started_at: float
    """Wall-clock start (epoch seconds), for logs and the health record."""
    duration_s: float
    calls: AgentChatCallCount | None
    """The turn's hosted AgentChat calls (``made``, ``failed``) from its
    transcript; ``None`` when the transcript could not be read: unknown,
    not zero."""
    detail: str | None = None
    result: Any = None
    finished: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def ended(self) -> bool:
        return self.finished.is_set()

    def turn_end(self) -> TurnEnd:
        """This report in the kit's terms (the reply rule's input).

        A turn that overran and has not ended is ``still_running``: it keeps
        its conversation. One the interrupt stopped is ``failed``.
        """
        if self.status == "completed":
            return TurnEnd(status="completed", agentchat_calls=self.calls, detail=self.detail)
        if self.status == "timeout" and not self.ended:
            return TurnEnd(status="still_running", detail=self.detail)
        return TurnEnd(status="failed", agentchat_calls=self.calls, detail=self.detail)

    def calls_text(self) -> str:
        if self.calls is None:
            return "unknown AgentChat calls"
        return f"{self.calls.made} AgentChat call(s), {self.calls.failed} failed"


def run_turn(
    agent: Any,
    prompt: str,
    *,
    session_id: str,
    limits: TurnLimits | None = None,
    stopped: threading.Event | None = None,
    on_finished: Callable[[], None] | None = None,
    on_poll: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TurnReport:
    """Run ``agent.run_conversation(prompt)`` under ``limits`` and report.

    Returns when the turn ended, or when it overran (or ``stopped`` was set)
    and was interrupted — after at most ``grace_s`` more. ``on_finished``
    runs on the turn's own thread when it really ends, also after this
    function has returned ``timeout``; it must be quick and must not raise.
    ``on_poll`` runs on the caller's thread once per poll (a liveness beat).
    Never raises for what the turn does.
    """
    limits = limits or TurnLimits()
    finished = threading.Event()
    box: dict[str, Any] = {}
    started_at = time.time()
    started = clock()

    def body() -> None:
        try:
            box["result"] = agent.run_conversation(prompt, conversation_history=[])
        except BaseException as exc:
            box["error"] = exc
        finally:
            # The callback first (it releases the turn's conversations), then
            # the event: whoever sees ``finished`` sees them released.
            if on_finished is not None:
                try:
                    on_finished()
                except Exception:
                    logger.exception("AgentInvoker: turn %s end callback raised", session_id)
            finished.set()

    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run, args=(body,), name=f"agentchat-turn-{session_id[-6:]}", daemon=True
    )
    try:
        thread.start()
    except RuntimeError as exc:
        # No thread could be started (the process is out of them): a turn
        # that never ran. Whatever it would have held is released now.
        if on_finished is not None:
            on_finished()
        finished.set()
        return TurnReport(
            status="failed",
            session_id=session_id,
            started_at=started_at,
            duration_s=0.0,
            calls=None,
            detail=f"could not start the turn thread: {exc}",
            finished=finished,
        )

    watch = _ActivityWatch(agent, clock=clock)
    overrun: str | None = None
    while not finished.wait(limits.poll_s):
        if on_poll is not None:
            on_poll()
        if stopped is not None and stopped.is_set():
            overrun = "stopping"
            break
        elapsed = clock() - started
        if limits.max_s and elapsed >= limits.max_s:
            overrun = f"ran {elapsed:.0f}s, ceiling {limits.max_s:.0f}s"
            break
        idle = watch.idle_s(default=elapsed)
        if limits.idle_s and idle >= limits.idle_s:
            overrun = f"no activity for {idle:.0f}s, limit {limits.idle_s:.0f}s ({watch.describe()})"
            break

    if overrun is None:
        return _finished_report(box, session_id, started_at, clock() - started, finished)

    reason = INTERRUPT_STOP_REASON if overrun == "stopping" else INTERRUPT_TOOL_REASON
    logger.warning(
        "AgentInvoker: turn %s overran (%s) — interrupting it; it counts as failed and nothing "
        "is recorded for it",
        session_id,
        overrun,
    )
    if not interrupt_agent(agent, reason):
        logger.warning(
            "AgentInvoker: turn %s has no interrupt method — it is left to end on its own",
            session_id,
        )
    if finished.wait(limits.grace_s):
        logger.info("AgentInvoker: turn %s ended after the interrupt", session_id)
    else:
        logger.error(
            "AgentInvoker: turn %s did not end within %.0fs of the interrupt — its conversations "
            "stay marked running until it does; no other turn starts for them",
            session_id,
            limits.grace_s,
        )
    result = box.get("result")
    return TurnReport(
        status="timeout",
        session_id=session_id,
        started_at=started_at,
        duration_s=clock() - started,
        calls=count_agentchat_calls(result) if finished.is_set() else None,
        detail=overrun,
        result=result,
        finished=finished,
    )


def _finished_report(
    box: Mapping[str, Any],
    session_id: str,
    started_at: float,
    duration_s: float,
    finished: threading.Event,
) -> TurnReport:
    error = box.get("error")
    if error is not None:
        logger.error(
            "AgentInvoker: run_conversation raised for %s",
            session_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return TurnReport(
            status="failed",
            session_id=session_id,
            started_at=started_at,
            duration_s=duration_s,
            calls=None,
            detail=f"raised {type(error).__name__}: {str(error)[:300]}",
            finished=finished,
        )
    result = box.get("result")
    calls = count_agentchat_calls(result)
    failure = turn_failure(result)
    return TurnReport(
        status="failed" if failure is not None else "completed",
        session_id=session_id,
        started_at=started_at,
        duration_s=duration_s,
        calls=calls,
        detail=failure,
        result=result,
        finished=finished,
    )


def interrupt_agent(agent: Any, reason: str) -> bool:
    """Ask a running agent to stop, the way Hermes' own watchdogs do.

    ``hard_interrupt(message, tool_reason=...)`` when the agent has it (an
    explicit stop that also reaches its tools and child agents), else the
    older ``interrupt(message)``. ``False`` when it has neither.
    """
    for name in ("hard_interrupt", "interrupt"):
        try:
            # Static lookup: a dynamic ``__getattr__`` proxy is not taken as
            # really having the method (Hermes' own compat check does this).
            inspect.getattr_static(agent, name)
        except AttributeError:
            continue
        method = getattr(agent, name, None)
        if not callable(method):
            continue
        try:
            if _accepts_keyword(method, "tool_reason"):
                method(f"AgentChat: {reason}", tool_reason=reason)
            else:
                method(f"AgentChat: {reason}")
        except Exception:
            logger.exception("AgentInvoker: %s() raised", name)
            continue
        return True
    return False


def _accepts_keyword(method: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        or (p.name == name and p.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for p in parameters
    )


class _ActivityWatch:
    """Idle time from the agent's own activity record, on the monotonic clock."""

    def __init__(self, agent: Any, *, clock: Callable[[], float]) -> None:
        self._summary = getattr(agent, "get_activity_summary", None)
        self._clock = clock
        self._last: tuple[Any, ...] | None = None
        self._since = clock()
        self._desc = ""

    def idle_s(self, *, default: float) -> float:
        """Seconds since the activity record last changed; ``default`` without one."""
        if not callable(self._summary):
            return default
        try:
            summary = self._summary()
        except Exception:
            return default
        if not isinstance(summary, Mapping):
            return default
        mark = (
            summary.get("last_activity_at", summary.get("last_activity_ts")),
            summary.get("api_call_count"),
            summary.get("current_tool"),
        )
        desc = summary.get("last_activity_desc") or summary.get("last_activity_description")
        self._desc = str(desc)[:120] if desc else ""
        now = self._clock()
        if mark != self._last:
            self._last = mark
            self._since = now
        return now - self._since

    def describe(self) -> str:
        return f"last activity: {self._desc}" if self._desc else "no activity record"


# ─── reading the transcript ────────────────────────────────────────────────

_HOSTED_TOOL_MARK = f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__"
_UNTRUSTED_SOURCE_MARK = f'source="{_HOSTED_TOOL_MARK}'


def count_agentchat_calls(result: Any) -> AgentChatCallCount | None:
    """Count the hosted AgentChat tool calls in a turn's transcript.

    Hermes returns ``result["messages"]``; each tool result is a ``role:
    tool`` message with ``name`` (or ``tool_name``) and ``content``. A hosted
    tool's result is wrapped with ``source="mcp__agentchat__…"``; an error is
    a bare ``{"error": …}`` object. No ``messages`` list → ``None`` (unknown).
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return None
    calls = 0
    failed = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        name = str(message.get("name") or message.get("tool_name") or "")
        text = _content_text(message.get("content"))
        if not (name.startswith(_HOSTED_TOOL_MARK) or _UNTRUSTED_SOURCE_MARK in text):
            continue
        calls += 1
        if _is_tool_error(text):
            failed += 1
    return AgentChatCallCount(made=calls, failed=failed)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


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
    blocks, interrupts or partial turns; it returns ``{"completed": False,
    "failed": True, "error": …}``, ``{"interrupted": True, …}`` or
    ``{"partial": True, …}`` (``agent/turn_finalizer.py``). Such a turn must
    not record. A non-dict result (an older Hermes) is taken as completed.
    """
    if not isinstance(result, dict):
        return None
    failed = bool(result.get("failed"))
    partial = bool(result.get("partial"))
    interrupted = bool(result.get("interrupted"))
    completed = result.get("completed")
    if not failed and not partial and not interrupted and completed is not False:
        return None
    error = result.get("error")
    reason = result.get("failure_reason") or result.get("turn_exit_reason")
    what = (
        "failed"
        if failed
        else ("interrupted" if interrupted else ("partial" if partial else "not completed"))
    )
    detail = str(error).strip()[:300] if error else "no error text"
    return f"{what}{f', {reason}' if reason else ''}: {detail}"


# ─── turns that are running ─────────────────────────────────────────────────


class RunningTurns:
    """Which conversations (and invites) have a turn running right now.

    A key is held from the moment a turn starts until its thread has really
    ended — not until the host stopped waiting for it. Cards whose key is
    held are not gated and get no second turn. Thread-safe.
    """

    def __init__(self, *, on_released: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._held: dict[int, frozenset[str]] = {}
        self._abandoned: set[int] = set()
        self._next = 0
        self._on_released = on_released

    def hold(self, keys: set[str] | frozenset[str]) -> int:
        with self._lock:
            self._next += 1
            token = self._next
            self._held[token] = frozenset(keys)
        return token

    def release(self, token: int) -> None:
        with self._lock:
            keys = self._held.pop(token, None)
            was_abandoned = token in self._abandoned
            self._abandoned.discard(token)
        if keys is not None and was_abandoned and self._on_released is not None:
            # A turn the host had stopped waiting for has ended: whatever its
            # cards hold now was not looked at by anyone; read the tray again.
            try:
                self._on_released()
            except Exception:
                logger.exception("AgentInvoker: running-turn release callback raised")

    def abandon(self, token: int) -> None:
        """The host stopped waiting for this turn; keep it held until it ends."""
        with self._lock:
            if token in self._held:
                self._abandoned.add(token)

    def busy(self) -> set[str]:
        with self._lock:
            return set().union(*self._held.values()) if self._held else set()

    def is_busy(self, key: str) -> bool:
        with self._lock:
            return any(key in keys for keys in self._held.values())

    @property
    def hung(self) -> int:
        """Turns that overran and have not ended yet."""
        with self._lock:
            return len(self._abandoned)
