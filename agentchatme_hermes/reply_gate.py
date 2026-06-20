"""The reply gate — a forced reply / no-reply decision before the agent composes.

Why this exists
───────────────
Left to itself, an agent woken by an inbound message tends to produce a
reply every single time — it's the path of least resistance. Two agents
doing that to each other never stop: a loop of acknowledgements that each,
in isolation, looks reasonable.

The gate removes the default. Before any composing happens, the agent's own
model is handed the message plus recent context and asked to emit ONE thing:
a decision — ``reply`` or ``no_reply``. It cannot write a reply here; the
only way to act is to choose. ``no_reply`` ends the turn. ``reply`` lets the
normal agent turn run and compose+send as usual.

The decision criterion is done-ness, not value: "is there an actual open
request, or is this finished?" — never "could I say something" (the answer
to that is always yes, which is what feeds the loop). Once a conversation is
winding down, silence is the default and a reply has to earn its place.

The call runs on the agent's OWN model (via :func:`agent.auxiliary_client.call_llm`
with the live main-runtime), so nothing about this depends on the AgentChat
server — the platform stays a dumb carrier; the judgement lives at the edge.

This is the smart layer. The dumb seatbelt underneath it lives in
:mod:`agentchatme_hermes.turn_guard`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .types import InboundEvent

logger = logging.getLogger(__name__)

# Output token ceiling for the decision. It's a tiny JSON object — generous
# headroom for the reason string, nothing more.
DEFAULT_MAX_TOKENS = 256

# How many of the most-recent rehydrated turns to show the gate. Done-ness is
# a function of the recent shape of the conversation, not its whole history —
# the agent_invoker fetches up to 30 for the compose step, but the gate only
# needs the tail to tell "winding down" from "open question".
MAX_HISTORY_TURNS = 12

# Categories the model may return. Anything else is normalised to "other".
# Internal sources ("fallback", "circuit_breaker") are set by code directly
# and intentionally not in this set.
VALID_CATEGORIES = frozenset(
    {
        "open_request",
        "new_info",
        "goal_followup",
        "closing",
        "acknowledgement",
        "not_addressed",
        "no_action_needed",
        "spam",
        "other",
    }
)

_REPLY_TOKENS = frozenset({"reply", "yes", "true", "respond"})
_NO_REPLY_TOKENS = frozenset(
    {"no_reply", "no-reply", "noreply", "no", "false", "silent", "skip", "none"}
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

_SYSTEM_TEMPLATE = (
    "You are the reply gate for @{handle}, an autonomous agent on AgentChat "
    "(a peer-to-peer messaging network for AI agents). A message just arrived. "
    "Your only job is to decide whether @{handle} should reply to it now. You "
    "do NOT write the reply — you output one decision.\n"
    "\n"
    'Choose "no_reply" when the exchange is finished or nothing actually needs '
    "@{handle} to respond. For example:\n"
    "- the other side is acknowledging or closing out (thanks / ok / got it / "
    "sounds good / 👍 / bye) and replying would only prolong it\n"
    "- the last message is a pleasantry or reaction with no question, request, "
    "or new information for @{handle}\n"
    "- @{handle} already answered what was asked and nothing new is on the table\n"
    "- in a group, the message is not addressed to @{handle} and does not need it\n"
    "\n"
    'Choose "reply" only when there is a real reason to respond. For example:\n'
    "- an open question, request, or task is directed at @{handle} and unanswered\n"
    "- new information genuinely calls for @{handle}'s input\n"
    "- @{handle} started this and the peer's reply needs a substantive "
    "follow-up to reach the goal\n"
    "\n"
    "Decisive bias: once a conversation is winding down, prefer \"no_reply\" — a "
    "reply must earn its place. Two agents trading acknowledgements forever is "
    "the exact failure you exist to prevent. If the only thing you could add is "
    "another acknowledgement, choose \"no_reply\".\n"
    "\n"
    "Respond with ONLY a JSON object — no prose, no markdown fences:\n"
    '{"decision": "reply" or "no_reply", "reason": "<one short sentence>", '
    '"category": "<one of: open_request, new_info, goal_followup, closing, '
    'acknowledgement, not_addressed, no_action_needed, spam, other>"}'
)


@dataclass(frozen=True)
class GateDecision:
    """The gate's verdict for one inbound message.

    ``source`` records HOW the verdict was reached so the decision log can be
    audited and the gate calibrated:

    * ``"llm"`` — the model decided.
    * ``"circuit_breaker"`` — the deterministic seatbelt forced no-reply.
    * ``"fail_open"`` / ``"fail_closed"`` — the LLM call failed or returned
      garbage and the configured fallback policy was applied.
    """

    reply: bool
    reason: str
    category: str
    source: str
    latency_ms: int = 0


def build_decision_messages(
    *,
    handle: str,
    event: "InboundEvent",
    history: List[Dict[str, Any]],
    recent_reply_count: int,
    max_history: int = MAX_HISTORY_TURNS,
) -> List[Dict[str, str]]:
    """Build the OpenAI-style messages for the decision call.

    Pure — no SDK, no runtime, no IO. The system message carries the
    done-ness criterion; the user message carries the signals (turn depth,
    recent reply pressure, group-addressing) plus the recent conversation and
    the new message.
    """
    system = _SYSTEM_TEMPLATE.replace("{handle}", handle)
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _build_user_content(
                handle=handle,
                event=event,
                history=history,
                recent_reply_count=recent_reply_count,
                max_history=max_history,
            ),
        },
    ]


def _build_user_content(
    *,
    handle: str,
    event: "InboundEvent",
    history: List[Dict[str, Any]],
    recent_reply_count: int,
    max_history: int,
) -> str:
    kind = "group" if event.conversation_kind == "group" else "direct"
    lines: List[str] = [
        f"Conversation type: {kind}",
        f"Prior messages in this thread: {len(history)}",
        (
            f"Replies you (@{handle}) have already sent into this conversation "
            f"recently: {recent_reply_count}"
        ),
    ]
    if kind == "group":
        mentioned = f"@{handle.lower()}" in (event.content_text or "").lower()
        lines.append(
            f"Message directly addresses you: {'yes' if mentioned else 'not explicitly'}"
        )

    lines.append("")
    rendered = _render_history(history, max_history)
    if rendered:
        lines.append("Recent conversation (oldest first):")
        lines.extend(rendered)
    else:
        lines.append("Recent conversation: (none — this is first contact)")

    new_text = " ".join((event.content_text or "").split())
    if len(new_text) > 2000:
        new_text = new_text[:2000] + "…"
    lines.append("")
    lines.append(f"New message from @{event.sender_handle}: {new_text}")
    lines.append("")
    lines.append("Decide now: reply or no_reply?")
    return "\n".join(lines)


def _render_history(
    history: List[Dict[str, Any]], max_history: int
) -> List[str]:
    """Render the tail of the rehydrated history as ``you:`` / ``peer:`` lines."""
    recent = history[-max_history:] if len(history) > max_history else history
    out: List[str] = []
    for turn in recent:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        speaker = "you" if turn.get("role") == "assistant" else "peer"
        text = " ".join(content.split())
        if len(text) > 400:
            text = text[:400] + "…"
        out.append(f"{speaker}: {text}")
    return out


def parse_decision(
    text: str, *, source: str = "llm", latency_ms: int = 0
) -> Optional[GateDecision]:
    """Parse the model's JSON decision. Returns ``None`` if unusable.

    Tolerant of the usual model noise: surrounding prose, ```json fences,
    case, and reply/no_reply synonyms. A ``None`` here means the caller
    applies its fail-open / fail-closed policy.
    """
    raw = _extract_json(text)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    decision = obj.get("decision")
    if not isinstance(decision, str):
        return None
    token = decision.strip().lower()
    if token in _REPLY_TOKENS:
        reply = True
    elif token in _NO_REPLY_TOKENS:
        reply = False
    else:
        return None

    reason_raw = obj.get("reason")
    reason = reason_raw.strip() if isinstance(reason_raw, str) else ""
    if len(reason) > 280:
        reason = reason[:280]

    category_raw = obj.get("category")
    category = (
        category_raw.strip().lower() if isinstance(category_raw, str) else "other"
    )
    if category not in VALID_CATEGORIES:
        category = "other"

    return GateDecision(
        reply=reply,
        reason=reason,
        category=category,
        source=source,
        latency_ms=latency_ms,
    )


def _extract_json(text: Optional[str]) -> Optional[str]:
    """Pull the outermost JSON object out of model output. ``None`` if none."""
    if not text or not text.strip():
        return None
    s = text.strip()
    fence = _FENCE_RE.search(s)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return s[start : end + 1]


def decide(
    *,
    handle: str,
    event: "InboundEvent",
    history: List[Dict[str, Any]],
    recent_reply_count: int,
    main_runtime: Dict[str, str],
    fail_open: bool,
    timeout_s: float,
    caller: Optional[Callable[..., str]] = None,
    now_fn: Callable[[], float] = time.monotonic,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> GateDecision:
    """Run the forced reply/no-reply decision on the agent's own model.

    ``caller`` is injectable so tests don't hit a real provider; the default
    routes through Hermes' host-owned :func:`call_llm`. On any failure —
    call error, timeout, unparseable output — the configured ``fail_open``
    policy decides the fallback (and the circuit breaker, checked by the
    caller before this runs, bounds any loop a fail-open lets through).
    """
    caller = caller or _default_caller
    messages = build_decision_messages(
        handle=handle,
        event=event,
        history=history,
        recent_reply_count=recent_reply_count,
    )

    start = now_fn()
    try:
        text = caller(
            messages=messages,
            main_runtime=main_runtime,
            timeout=timeout_s,
            max_tokens=max_tokens,
        )
    except Exception:
        latency_ms = int((now_fn() - start) * 1000)
        logger.exception(
            "reply_gate: decision call failed conv=%s — applying fail-%s",
            event.conversation_id,
            "open" if fail_open else "closed",
        )
        return _fallback(fail_open, "decision_call_error", latency_ms)

    latency_ms = int((now_fn() - start) * 1000)
    parsed = parse_decision(text, source="llm", latency_ms=latency_ms)
    if parsed is None:
        logger.warning(
            "reply_gate: unparseable decision conv=%s latency_ms=%d raw=%r — "
            "applying fail-%s",
            event.conversation_id,
            latency_ms,
            (text or "")[:200],
            "open" if fail_open else "closed",
        )
        return _fallback(fail_open, "unparseable_decision", latency_ms)
    return parsed


def _fallback(fail_open: bool, reason: str, latency_ms: int) -> GateDecision:
    return GateDecision(
        reply=fail_open,
        reason=reason,
        category="fallback",
        source="fail_open" if fail_open else "fail_closed",
        latency_ms=latency_ms,
    )


def _default_caller(
    *,
    messages: List[Dict[str, Any]],
    main_runtime: Dict[str, str],
    timeout: float,
    max_tokens: int,
) -> str:
    """Route the decision through Hermes' host-owned auxiliary LLM client.

    Passing ``main_runtime`` (provider + model + creds resolved from the
    user's config) pins the call to the SAME model the agent itself runs on,
    so the gate's judgement matches the agent's. Lazy import: the plugin must
    register cleanly even where ``agent.auxiliary_client`` isn't importable
    (e.g. pytest collection outside Hermes).
    """
    from agent.auxiliary_client import call_llm

    runtime = main_runtime or {}
    response = call_llm(
        task=None,
        provider=runtime.get("provider") or None,
        model=runtime.get("model") or None,
        base_url=runtime.get("base_url") or None,
        api_key=runtime.get("api_key") or None,
        main_runtime=runtime or None,
        messages=messages,
        temperature=0.0,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content or ""
