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
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

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

# Window (seconds) for the cadence signal — "how many messages in the last
# minute". Tight on purpose: the loop is a rapid-fire phenomenon, so a short
# window distinguishes a live volley from a normally paced exchange.
CADENCE_WINDOW_SECONDS = 60.0

# Categories the model may return. Anything else is normalised to "other".
# The internal "fallback" source is set by code directly and intentionally
# not in this set.
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
    "another acknowledgement, choose \"no_reply\". If the Pace line shows "
    "messages flying back and forth rapidly with each only restating or "
    "acknowledging the last, that IS the loop — choose \"no_reply\".\n"
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
    * ``"fail_open"`` / ``"fail_closed"`` — the LLM call failed or returned
      garbage and the configured fallback policy was applied.
    """

    reply: bool
    reason: str
    category: str
    source: str
    latency_ms: int = 0


@dataclass(frozen=True)
class ConversationSignals:
    """Compact, deterministic context derived from the thread already in hand.

    No network call and no server-side interpretation — every field comes from
    the recent messages the gate already fetched plus the inbound message's own
    timestamp. Rendered into short phrases for the prompt (never raw dumps), so
    the per-message cost stays bounded.

    * ``first_contact`` — no prior messages (a fresh opener).
    * ``you_have_spoken`` — this agent already replied in the window (an
      established two-way thread vs. being newly approached).
    * ``messages_last_window`` — messages incl. the new one within
      :data:`CADENCE_WINDOW_SECONDS`; a high count is the loop's tempo.
    * ``seconds_since_previous`` — gap from the previous message to this one,
      or ``None`` when there is no usable prior timestamp.
    """

    first_contact: bool
    you_have_spoken: bool
    messages_last_window: int
    seconds_since_previous: float | None


def compute_conversation_signals(
    messages: list[dict[str, Any]],
    *,
    own_handle: str,
    trigger_message_id: str,
    now: datetime,
    window_seconds: float = CADENCE_WINDOW_SECONDS,
) -> ConversationSignals:
    """Derive :class:`ConversationSignals` from the recent raw messages.

    Pure and defensive: tolerates missing/malformed timestamps, non-dict rows,
    and naive datetimes (assumed UTC). ``messages`` is the raw ``get_messages``
    result; the triggering message is excluded so "now" isn't double-counted.
    """
    own = own_handle.lstrip("@").lower()
    prior = [
        m
        for m in messages
        if isinstance(m, dict) and m.get("id") != trigger_message_id
    ]
    first_contact = len(prior) == 0
    you_have_spoken = any(_message_is_own(m, own) for m in prior)

    timestamps = [
        ts
        for ts in (_parse_timestamp(m.get("created_at")) for m in prior)
        if ts is not None
    ]
    cutoff = now - timedelta(seconds=window_seconds)
    recent_in_window = sum(1 for ts in timestamps if ts >= cutoff)

    seconds_since_previous: float | None = None
    if timestamps:
        delta = (now - max(timestamps)).total_seconds()
        seconds_since_previous = delta if delta > 0 else 0.0  # clamp clock skew

    return ConversationSignals(
        first_contact=first_contact,
        you_have_spoken=you_have_spoken,
        messages_last_window=recent_in_window + 1,  # +1 for the new message
        seconds_since_previous=seconds_since_previous,
    )


def _message_is_own(msg: dict[str, Any], own_handle_norm: str) -> bool:
    """Trust server-precomputed ``is_own``; else compare sender handles.

    Mirrors the role logic in the history translator.
    """
    is_own = msg.get("is_own")
    if isinstance(is_own, bool):
        return is_own
    sender = msg.get("from") or msg.get("sender_handle") or ""
    return str(sender).lstrip("@").lower() == own_handle_norm


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO-8601 ``created_at`` to a tz-aware datetime, or ``None``.

    Naive timestamps are assumed UTC so comparisons against the (tz-aware)
    inbound time never raise.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_gap(seconds: float) -> str:
    """Humanise a seconds gap into a compact token (``8s`` / ``3m`` / ``2h``)."""
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{round(seconds / 3600)}h"


def _relationship_phrase(signals: ConversationSignals) -> str:
    if signals.first_contact:
        return "first contact — no prior messages in this thread"
    if signals.you_have_spoken:
        return "established — you have already replied in this thread"
    return "this peer is messaging you, but you have not replied yet"


def build_decision_messages(
    *,
    handle: str,
    event: InboundEvent,
    history: list[dict[str, Any]],
    signals: ConversationSignals | None = None,
    max_history: int = MAX_HISTORY_TURNS,
) -> list[dict[str, str]]:
    """Build the OpenAI-style messages for the decision call.

    Pure — no SDK, no runtime, no IO. The system message carries the
    done-ness criterion; the user message carries the signals (relationship,
    pace, turn depth, group-addressing) plus the recent conversation and the
    new message.
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
                signals=signals,
                max_history=max_history,
            ),
        },
    ]


def _build_user_content(
    *,
    handle: str,
    event: InboundEvent,
    history: list[dict[str, Any]],
    signals: ConversationSignals | None,
    max_history: int,
) -> str:
    kind = "group" if event.conversation_kind == "group" else "direct"
    lines: list[str] = [f"Conversation type: {kind}"]
    if signals is not None:
        lines.append(f"Relationship: {_relationship_phrase(signals)}")
        if signals.seconds_since_previous is not None:
            lines.append(
                f"Pace: {signals.messages_last_window} message(s) in the last "
                f"{int(CADENCE_WINDOW_SECONDS)}s; "
                f"{_format_gap(signals.seconds_since_previous)} since the "
                f"previous message"
            )
    lines.append(f"Prior messages in this thread: {len(history)}")
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
    history: list[dict[str, Any]], max_history: int
) -> list[str]:
    """Render the tail of the rehydrated history as ``you:`` / ``peer:`` lines."""
    recent = history[-max_history:] if len(history) > max_history else history
    out: list[str] = []
    for turn in recent:
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
) -> GateDecision | None:
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


def _extract_json(text: str | None) -> str | None:
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
    event: InboundEvent,
    history: list[dict[str, Any]],
    main_runtime: dict[str, str],
    fail_open: bool,
    timeout_s: float,
    signals: ConversationSignals | None = None,
    caller: Callable[..., str] | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> GateDecision:
    """Run the forced reply/no-reply decision on the agent's own model.

    ``caller`` is injectable so tests don't hit a real provider; the default
    routes through Hermes' host-owned :func:`call_llm`. On any failure —
    call error, timeout, unparseable output — the configured ``fail_open``
    policy decides the fallback; the gate simply re-runs on the next inbound.
    """
    caller = caller or _default_caller
    messages = build_decision_messages(
        handle=handle,
        event=event,
        history=history,
        signals=signals,
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
    messages: list[dict[str, Any]],
    main_runtime: dict[str, str],
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
