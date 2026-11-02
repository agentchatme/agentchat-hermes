"""The gate step: the agent's own model answers the server's question about the server's cards.

Code we keep, brain we don't
────────────────────────────
Every word the model reads here comes from the main line: the gate question
(``tray["question"]``) and the briefing cards (``tray["conversations"]``) are
passed through exactly as ``GET /v1/inbox`` returned them. This module adds a
two-line wrapper asking for strict JSON, parses the answer, and records the
verdicts with ``POST /v1/conversations/{id}/decisions``. No wording of its own,
no category list of its own, no pace or relationship arithmetic — the server
computes, the agent's model judges, the doorbell carries.

What gets recorded
──────────────────
``no_reply`` and ``defer`` are posted; a recorded verdict settles delivery for
what it covers, so the sender's tick is honest and the conversation leaves the
tray. ``reply`` is deliberately *not* posted: the send is the reply verdict
(``WIRE-CONTRACT.md``, *Reply-gate verdicts*), and posting it would settle the
envelopes and take the card off the tray before the agent's turn has read it.
Cards decided ``reply`` stay on the tray; the turn that follows reads exactly
those with the hosted ``agentchat_inbox`` tool.

Failure policy
──────────────
Fail closed by default: when the model cannot be reached or answers nothing
usable, nothing is posted and no turn starts. The envelopes stay ``stored``,
the tray keeps the cards, and the invoker re-knocks on its own ladder. With
``AGENTCHATME_REPLY_GATE_FAIL_OPEN=1`` a failed gate starts the turn anyway and
the agent judges from the tray inside the turn.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from .main_line import MainLineClient, MainLineError

logger = logging.getLogger(__name__)

# Output ceiling for the verdict JSON: a few dozen tokens per card. The tray
# is capped at 50 cards server-side.
DEFAULT_MAX_TOKENS = 2048

REPLY = "reply"
NO_REPLY = "no_reply"
DEFER = "defer"
DECISIONS = (REPLY, NO_REPLY, DEFER)

# Tolerated spellings. Anything else leaves the card undecided.
_REPLY_TOKENS = frozenset({"reply", "respond", "answer", "yes", "true"})
_NO_REPLY_TOKENS = frozenset(
    {"no_reply", "no-reply", "noreply", "silent", "silence", "skip", "ignore", "no", "false"}
)
_DEFER_TOKENS = frozenset({"defer", "deferred", "later", "wait", "postpone"})

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

GateCaller = Callable[..., str]


@dataclass(frozen=True)
class Verdict:
    """One parsed answer for one card."""

    conversation_id: str
    decision: str
    category: str | None = None
    defer_seconds: int | None = None


@dataclass(frozen=True)
class GateResult:
    """What one gate cycle did, for the log line and for the invoker.

    ``source``:

    * ``"llm"`` — the model answered; ``verdicts`` is what it said.
    * ``"empty"`` — the tray had no cards; nothing to decide.
    * ``"fail_closed"`` — the model call failed or was unusable; nothing
      posted, no turn (default policy).
    * ``"fail_open"`` — same failure, but the configured policy starts the
      turn and lets the agent judge in-turn.
    """

    source: str
    cards: int = 0
    verdicts: tuple[Verdict, ...] = ()
    undecided: tuple[str, ...] = ()
    reply_conversations: tuple[str, ...] = ()
    posted: int = 0
    post_failures: int = 0
    truncated: bool = False
    latency_ms: int = 0
    failure: str | None = None

    @property
    def start_turn(self) -> bool:
        return bool(self.reply_conversations) or self.source == "fail_open"

    def counts(self) -> dict[str, int]:
        counts = {decision: 0 for decision in DECISIONS}
        for verdict in self.verdicts:
            counts[verdict.decision] += 1
        return counts


# ─── building the call ─────────────────────────────────────────────────────


def cards_of(tray: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tray's cards, exactly as served (non-object entries dropped)."""
    raw = tray.get("conversations")
    if not isinstance(raw, list):
        return []
    return [card for card in raw if isinstance(card, dict)]


def card_ids(cards: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for card in cards:
        cid = card.get("conversation_id")
        if isinstance(cid, str) and cid:
            out.append(cid)
    return out


def categories_of(tray: Mapping[str, Any]) -> list[str]:
    """The verdict categories, when the server serves them with the tray.

    The taxonomy is the server's (``GATE_CATEGORIES`` in the shared package,
    validated on POST). The tray does not carry it today; when it does,
    the model is asked to pick from it. Until then verdicts are posted
    without a category, never with a locally invented one.
    """
    raw = tray.get("categories")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def build_gate_messages(tray: Mapping[str, Any]) -> list[dict[str, str]]:
    """OpenAI-style messages for the decision call.

    System: the server's question, verbatim, then the two-line JSON wrapper.
    User: the server's cards, verbatim, as JSON. Pure — no IO.

    Raises ``ValueError`` when the tray carries no question (a server
    contract break: judging without the question is exactly the drift this
    design removes, so the gate fails closed instead).
    """
    question = tray.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("tray carries no gate question")
    cards = cards_of(tray)
    categories = categories_of(tray)
    you = tray.get("you")

    shape = (
        '{"decisions": [{"conversation_id": "<conversation_id from a card>", '
        '"decision": "reply" | "no_reply" | "defer"'
    )
    if categories:
        shape += ', "category": "<one of: ' + ", ".join(categories) + '>"'
    shape += ', "defer_seconds": <seconds until you look again, only with defer>}]}'

    wrapper = (
        "Answer with ONLY a JSON object, no prose and no code fences, in this shape: "
        + shape
        + " — one entry per card, every card decided."
    )
    system = question.strip() + "\n\n" + wrapper

    payload: dict[str, Any] = {"conversations": cards}
    if isinstance(you, str) and you:
        payload = {"you": you, **payload}
    user = "Your tray, as the server built it:\n" + json.dumps(payload, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ─── parsing the answer ────────────────────────────────────────────────────


def parse_verdicts(
    text: str | None,
    *,
    known_ids: Sequence[str],
    categories: Sequence[str] = (),
) -> list[Verdict] | None:
    """Parse the model's JSON into verdicts for cards we showed it.

    Returns ``None`` when the text holds no JSON object or list at all (the
    caller applies the failure policy). Entries for unknown conversations,
    unknown decisions, or duplicates are dropped; the caller sees those cards
    as undecided. Tolerant of fences, surrounding prose and spelling.
    """
    raw = _extract_json(text)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None

    items: Any
    if isinstance(obj, dict):
        items = obj.get("decisions", obj.get("verdicts"))
        if items is None and "conversation_id" in obj:
            items = [obj]  # a single verdict object
    else:
        items = obj
    if not isinstance(items, list):
        return None

    known = set(known_ids)
    allowed = {c.lower() for c in categories}
    seen: set[str] = set()
    out: list[Verdict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get("conversation_id")
        if not isinstance(cid, str) or cid not in known or cid in seen:
            continue
        decision = _normalize_decision(item.get("decision"))
        if decision is None:
            continue
        category = _normalize_category(item.get("category"), allowed)
        defer_seconds = _normalize_defer_seconds(item.get("defer_seconds"))
        seen.add(cid)
        out.append(
            Verdict(
                conversation_id=cid,
                decision=decision,
                category=category,
                defer_seconds=defer_seconds if decision == DEFER else None,
            )
        )
    return out


def _extract_json(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    s = text.strip()
    fence = _FENCE_RE.search(s)
    if fence:
        s = fence.group(1).strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    closer = "}" if s[start] == "{" else "]"
    end = s.rfind(closer)
    if end <= start:
        return None
    return s[start : end + 1]


def _normalize_decision(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace(" ", "_")
    if token in _REPLY_TOKENS:
        return REPLY
    if token in _NO_REPLY_TOKENS:
        return NO_REPLY
    if token in _DEFER_TOKENS:
        return DEFER
    return None


def _normalize_category(value: Any, allowed: set[str]) -> str | None:
    if not allowed or not isinstance(value, str):
        return None
    token = value.strip().lower()
    return token if token in allowed else None


def _normalize_defer_seconds(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(value)
    return seconds if seconds > 0 else None


# ─── the step ──────────────────────────────────────────────────────────────


def run_gate(
    tray: Mapping[str, Any],
    *,
    main_line: MainLineClient,
    main_runtime: Mapping[str, Any],
    fail_open: bool,
    timeout_s: float,
    caller: GateCaller | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    now_fn: Callable[[], float] = time.monotonic,
) -> GateResult:
    """Decide every card on ``tray`` and record the verdicts.

    ``caller`` is injectable so tests never reach a provider; the default is
    Hermes' auxiliary client pinned to the agent's own model. ``main_runtime``
    is what :func:`agent_invoker.AgentInvoker._resolve_model_and_runtime`
    produced (model + provider credentials).
    """
    cards = cards_of(tray)
    ids = card_ids(cards)
    truncated = bool(tray.get("truncated"))
    if not ids:
        return GateResult(source="empty", truncated=truncated)

    caller = caller or default_caller
    start = now_fn()
    try:
        messages = build_gate_messages(tray)
        text = caller(
            messages=messages,
            main_runtime=dict(main_runtime),
            timeout=timeout_s,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        latency_ms = int((now_fn() - start) * 1000)
        logger.exception("gate: decision call failed (%d cards)", len(ids))
        return _failed(ids, fail_open, f"decision call failed: {exc}", latency_ms, truncated)
    latency_ms = int((now_fn() - start) * 1000)

    verdicts = parse_verdicts(text, known_ids=ids, categories=categories_of(tray))
    if not verdicts:
        logger.warning(
            "gate: unusable answer for %d cards latency_ms=%d raw=%r",
            len(ids),
            latency_ms,
            (text or "")[:200],
        )
        return _failed(ids, fail_open, "no usable verdicts in the answer", latency_ms, truncated)

    by_id = {card["conversation_id"]: card for card in cards if "conversation_id" in card}
    decided = {verdict.conversation_id for verdict in verdicts}
    undecided = tuple(cid for cid in ids if cid not in decided)
    reply: list[str] = []
    posted = 0
    failures = 0
    for verdict in verdicts:
        if verdict.decision == REPLY:
            reply.append(verdict.conversation_id)
            continue
        if _post_verdict(main_line, verdict, by_id.get(verdict.conversation_id)):
            posted += 1
        else:
            failures += 1

    if undecided:
        logger.warning(
            "gate: %d of %d cards left undecided by the model: %s",
            len(undecided),
            len(ids),
            ", ".join(undecided),
        )
    return GateResult(
        source="llm",
        cards=len(ids),
        verdicts=tuple(verdicts),
        undecided=undecided,
        reply_conversations=tuple(reply),
        posted=posted,
        post_failures=failures,
        truncated=truncated,
        latency_ms=latency_ms,
    )


def _failed(
    ids: Sequence[str], fail_open: bool, reason: str, latency_ms: int, truncated: bool
) -> GateResult:
    return GateResult(
        source="fail_open" if fail_open else "fail_closed",
        cards=len(ids),
        undecided=tuple(ids),
        reply_conversations=tuple(ids) if fail_open else (),
        truncated=truncated,
        latency_ms=latency_ms,
        failure=reason,
    )


def _post_verdict(
    main_line: MainLineClient, verdict: Verdict, card: Mapping[str, Any] | None
) -> bool:
    """Record one ``no_reply``/``defer`` verdict. ``True`` when the server took it.

    ``through_message_id`` is the card's newest stored message, so only what
    the model actually saw is settled; anything that landed since stays
    ``stored`` and knocks again. A ``400`` for an optional field (category,
    defer_seconds) is retried once with the bare verdict — a verdict the
    server can take is worth more than its label.
    """
    through = _preview_message_id(card)
    try:
        main_line.record_decision(
            verdict.conversation_id,
            decision=verdict.decision,
            category=verdict.category,
            through_message_id=through,
            defer_seconds=verdict.defer_seconds,
        )
        return True
    except MainLineError as exc:
        retryable = exc.status == 400 and (
            verdict.category is not None or verdict.defer_seconds is not None
        )
        if not retryable:
            logger.warning(
                "gate: verdict %s for conv=%s not recorded: %s",
                verdict.decision,
                verdict.conversation_id,
                exc,
            )
            return False
        logger.warning(
            "gate: server rejected optional fields for conv=%s (%s) — retrying bare verdict",
            verdict.conversation_id,
            exc,
        )
    try:
        main_line.record_decision(
            verdict.conversation_id,
            decision=verdict.decision,
            through_message_id=through,
        )
        return True
    except MainLineError as exc:
        logger.warning(
            "gate: verdict %s for conv=%s not recorded: %s",
            verdict.decision,
            verdict.conversation_id,
            exc,
        )
        return False


def _preview_message_id(card: Mapping[str, Any] | None) -> str | None:
    if card is None:
        return None
    preview = card.get("preview")
    if not isinstance(preview, Mapping):
        return None
    message_id = preview.get("message_id")
    return message_id if isinstance(message_id, str) and message_id else None


# ─── the model call ────────────────────────────────────────────────────────


def default_caller(
    *,
    messages: list[dict[str, str]],
    main_runtime: Mapping[str, Any],
    timeout: float,
    max_tokens: int,
) -> str:
    """Route the decision through Hermes' host-owned auxiliary LLM client.

    ``main_runtime`` (provider, model, credentials resolved from the user's
    Hermes config) pins the call to the same model the agent itself runs on,
    so the judgement is the agent's. Lazy import: the plugin must register
    cleanly where ``agent.auxiliary_client`` is not importable (pytest
    outside Hermes).
    """
    from agent.auxiliary_client import call_llm

    runtime = dict(main_runtime)
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
    return str(content) if content else ""
