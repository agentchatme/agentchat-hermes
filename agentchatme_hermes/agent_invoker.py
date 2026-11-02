"""Knock → gate → turn. The only path from the socket to the agent.

What happens on a knock
───────────────────────
1. **Coalesce.** Frames inside a short window become one knock
   (:class:`.knock.KnockCoalescer`). A group waking up is one cycle, not
   twenty.
2. **Read the tray.** ``GET /v1/inbox?wait_ms=0`` — the server's briefing
   cards and its gate question, as it built them (:mod:`.main_line`).
3. **Gate.** The agent's own model answers the question per card
   (:mod:`.gate`); ``no_reply`` / ``defer`` verdicts are recorded on the
   server, which settles those conversations. ``reply`` cards stay on the
   tray.
4. **Turn.** If anything was decided ``reply``, start ONE Hermes agent turn
   with the fixed prompt (:mod:`.turn`). The agent reads and answers through
   the hosted MCP tools; the send is its verdict. The turn's return value is
   discarded — nothing is auto-routed anywhere.

One cycle at a time per account: a knock that lands while a cycle runs is
remembered and runs once the cycle ends (the tray is re-read, so whatever
the earlier knock announced is covered). A cycle that could not complete —
model unreachable, server error, verdicts rejected — re-knocks itself on a
local ladder (1 m, 5 m, 30 m) because a connected socket gets no re-ring
from the server; the cards wait on the tray meanwhile.

Group invites are a second knock kind: not on the tray, not gated, one turn
each with the fixed invite prompt, serialized on the same worker.

This mirrors the pattern Hermes' cron scheduler uses: build a transient
agent, run a turn, ignore the response. What changed from the pre-v3
invoker: no message bodies, no history rehydration, no prompt of its own,
no gate wording of its own, no native tools — all of that lives on the main
line now (``docs/decisions/2026-11-main-line.md``).
"""
from __future__ import annotations

import collections
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from . import gate
from .config import (
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_KNOCK_DEBOUNCE_S,
    DEFAULT_REPLY_GATE_ENABLED,
    DEFAULT_REPLY_GATE_FAIL_OPEN,
)
from .knock import KnockCoalescer
from .main_line import MainLineError
from .turn import invite_session_id, invite_turn_prompt, knock_session_id, knock_turn_prompt

if TYPE_CHECKING:
    from .config import Config
    from .types import AgentIdentity, GroupInviteEvent

logger = logging.getLogger(__name__)

_WORKER_THREAD_NAME = "agentchat-invoker"
_WORKER_IDLE_WAIT_S = 60.0

# Fallback model when Hermes' config helpers are unavailable.
_FALLBACK_MODEL = "claude-sonnet-4-6"

# Local re-knock ladder after a cycle that could not complete. Mirrors the
# server's webhook ladder (1m, 5m, 30m) — a connected socket never gets that
# re-ring, so the doorbell carries it. After the last rung the cycle waits
# for the next knock; the cards are still on the tray.
RETRY_LADDER_S: tuple[float, ...] = (60.0, 300.0, 1800.0)


class AgentInvoker:
    """Owns the knock worker. Single-use — :meth:`start` once, :meth:`stop` once."""

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        runtime: Any,
    ) -> None:
        self._config = config
        self._identity = identity
        # The runtime this invoker belongs to (one per profile and account).
        # Supplies the main-line client; never resolved from a registry.
        self._runtime = runtime

        # Gate settings resolved once. getattr-with-default so light
        # config doubles in tests construct cleanly.
        self._gate_enabled = bool(getattr(config, "reply_gate_enabled", DEFAULT_REPLY_GATE_ENABLED))
        self._gate_fail_open = bool(
            getattr(config, "reply_gate_fail_open", DEFAULT_REPLY_GATE_FAIL_OPEN)
        )
        self._gate_timeout_s = float(getattr(config, "gate_timeout_s", DEFAULT_GATE_TIMEOUT_S))
        debounce_s = float(getattr(config, "knock_debounce_s", DEFAULT_KNOCK_DEBOUNCE_S))

        self._coalescer = KnockCoalescer(on_fire=self._on_knock_fired, debounce_s=debounce_s)
        self._wake = threading.Event()
        self._knock_due = threading.Event()
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        self._invites: collections.deque[GroupInviteEvent] = collections.deque()
        self._invites_lock = threading.Lock()
        self._retry_rung = 0

        # Counters for the heartbeat / doctor and for tests.
        self.cycles = 0
        self.turns = 0
        self.invite_turns = 0

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
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            # A turn in flight finishes on its own; the worker checks
            # ``_stopped`` between cycles. Bounded join so a stop never hangs
            # the gateway's shutdown on a slow provider.
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.info("AgentInvoker: worker still finishing a turn at stop()")

    # -- knocks -------------------------------------------------------------

    def knock(self) -> None:
        """A knock from the socket (or any other source). Coalesced, then cycled."""
        self._coalescer.knock()

    def handle_group_invite(self, invite: GroupInviteEvent) -> None:
        """Queue one invite-decision turn. Runs on the WS thread; never blocks."""
        if self._stopped.is_set():
            logger.warning(
                "AgentInvoker: group invite dropped at shutdown (group=%s invite=%s) — it stays "
                "pending server-side and is reachable through the hosted tools",
                invite.group_id,
                invite.invite_id,
            )
            return
        with self._invites_lock:
            self._invites.append(invite)
        self._wake.set()

    def _on_knock_fired(self) -> None:
        """Timer-thread callback from the coalescer: mark a cycle due, wake the worker."""
        self._knock_due.set()
        self._wake.set()

    # -- worker -------------------------------------------------------------

    def _worker_loop(self) -> None:
        logger.info("AgentInvoker: worker started")
        try:
            while not self._stopped.is_set():
                self._wake.wait(timeout=_WORKER_IDLE_WAIT_S)
                if self._stopped.is_set():
                    return
                self._wake.clear()
                self._drain_invites()
                if self._knock_due.is_set():
                    # Clear BEFORE the cycle: a knock landing during the cycle
                    # sets it again and earns one more cycle afterwards.
                    self._knock_due.clear()
                    self._guarded_cycle()
        except Exception:
            logger.exception(
                "AgentInvoker: worker crashed — no further cycles until the gateway restarts"
            )
        finally:
            logger.info("AgentInvoker: worker exiting")

    def _drain_invites(self) -> None:
        while True:
            with self._invites_lock:
                if not self._invites:
                    return
                invite = self._invites.popleft()
            if self._stopped.is_set():
                return
            try:
                self._run_invite_turn(invite)
            except Exception:
                logger.exception(
                    "AgentInvoker: group-invite turn FAILED (outer catch) group=%s invite=%s — "
                    "the invite stays pending server-side",
                    invite.group_id,
                    invite.invite_id,
                )

    def _guarded_cycle(self) -> None:
        try:
            self.run_cycle()
        except Exception:
            # Outer catch: whatever escaped the cycle must not kill the
            # worker. Re-knock on the ladder so the tray is read again.
            logger.exception("AgentInvoker: cycle FAILED with unhandled exception (outer catch)")
            self._schedule_retry("unhandled exception")

    # -- one cycle ----------------------------------------------------------

    def run_cycle(self) -> gate.GateResult | None:
        """Read the tray, gate it, start a turn when warranted.

        Returns the gate result (``None`` when the tray could not be read).
        Public so tests and ``doctor``-style tooling can drive one cycle.
        """
        self.cycles += 1
        try:
            tray = self._runtime.main_line.inbox(wait_ms=0)
        except MainLineError as exc:
            logger.warning("AgentInvoker: tray read failed: %s", exc)
            self._schedule_retry("tray read failed")
            return None

        cards = gate.cards_of(tray)
        truncated = bool(tray.get("truncated"))
        if not cards:
            logger.info(
                "AgentInvoker: tray empty (deferred=%s) — nothing to decide",
                tray.get("deferred", 0),
            )
            self._retry_rung = 0
            return gate.GateResult(source="empty", truncated=truncated)

        if not self._gate_enabled:
            # Gate off: the agent judges in-turn with the hosted tools.
            logger.info(
                "AgentInvoker: %d card(s) on the tray, gate disabled — starting the turn",
                len(cards),
            )
            result = gate.GateResult(
                source="disabled",
                cards=len(cards),
                reply_conversations=tuple(gate.card_ids(cards)),
                truncated=truncated,
            )
        else:
            result = gate.run_gate(
                tray,
                main_line=self._runtime.main_line,
                main_runtime=self._gate_runtime(),
                fail_open=self._gate_fail_open,
                timeout_s=self._gate_timeout_s,
            )
            counts = result.counts()
            logger.info(
                "AgentInvoker: gate cards=%d reply=%d no_reply=%d defer=%d undecided=%d "
                "posted=%d post_failures=%d source=%s latency_ms=%d%s",
                result.cards,
                counts[gate.REPLY],
                counts[gate.NO_REPLY],
                counts[gate.DEFER],
                len(result.undecided),
                result.posted,
                result.post_failures,
                result.source,
                result.latency_ms,
                f" failure={result.failure!r}" if result.failure else "",
            )

        if result.source == "fail_closed":
            # Nothing posted, no turn. The cards stay on the tray; re-knock
            # on the ladder so a transient provider outage does not leave
            # peers unanswered until the next message happens to arrive.
            self._schedule_retry(result.failure or "gate failed")
            return result

        if result.start_turn:
            self._run_knock_turn(result)

        if result.post_failures or result.undecided:
            # Some cards were not settled (verdict rejected, or the model
            # skipped them). They are still on the tray; look again later.
            self._schedule_retry("cards left on the tray")
        else:
            self._retry_rung = 0

        if truncated:
            # More conversations are waiting than one tray shows.
            logger.info("AgentInvoker: tray truncated — knocking again for the rest")
            self._coalescer.knock()
        return result

    def _schedule_retry(self, reason: str) -> None:
        if self._coalescer.delayed_pending:
            # A retry is already on its way (a knock landed mid-outage and
            # failed again). One outage climbs the ladder once; climbing per
            # failed cycle would replace the sooner retry with a later one.
            logger.info("AgentInvoker: %s — a re-knock is already scheduled; not climbing the ladder", reason)
            return
        if self._retry_rung >= len(RETRY_LADDER_S):
            logger.warning(
                "AgentInvoker: %s — retry ladder exhausted; waiting for the next knock "
                "(the cards stay on the tray)",
                reason,
            )
            return
        delay = RETRY_LADDER_S[self._retry_rung]
        self._retry_rung += 1
        logger.warning(
            "AgentInvoker: %s — re-knocking in %.0fs (rung %d/%d)",
            reason,
            delay,
            self._retry_rung,
            len(RETRY_LADDER_S),
        )
        self._coalescer.knock_after(delay)

    # -- turns --------------------------------------------------------------

    def _run_knock_turn(self, result: gate.GateResult) -> None:
        session_id = knock_session_id()
        logger.info(
            "AgentInvoker: turn start session=%s reply_cards=%d source=%s",
            session_id,
            len(result.reply_conversations),
            result.source,
        )
        if self._run_turn(
            prompt=knock_turn_prompt(result.reply_conversations), session_id=session_id
        ):
            self.turns += 1

    def _run_invite_turn(self, invite: GroupInviteEvent) -> None:
        if self._stopped.is_set():
            return
        session_id = invite_session_id(invite.invite_id)
        logger.info(
            "AgentInvoker: group-invite turn start group=%s (%s) from=@%s invite=%s",
            invite.group_name,
            invite.group_id,
            invite.inviter_handle,
            invite.invite_id,
        )
        if self._run_turn(prompt=invite_turn_prompt(), session_id=session_id):
            self.invite_turns += 1

    def _run_turn(self, *, prompt: str, session_id: str) -> bool:
        """Build a transient AIAgent and run one turn. ``True`` when it ran.

        The return value of ``run_conversation`` is deliberately discarded:
        the agent's only way to say anything on AgentChat is the hosted
        ``agentchat_send_message`` tool during the turn.
        """
        self._ensure_hermes_resolved()
        agent = self._build_agent(session_id)
        if agent is None:
            logger.warning("AgentInvoker: agent construction failed for %s — turn dropped", session_id)
            return False
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


GateRunner = Callable[..., gate.GateResult]
