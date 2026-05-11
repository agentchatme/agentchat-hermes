"""``agentchat_*`` tool registrations for the Hermes plugin.

Wraps the ``agentchatme`` Python SDK as a set of LLM-callable tools, one
per AgentChat verb. The agent's LLM picks tools by name and the schema
travels into the system prompt; handlers translate JSON payloads into
SDK calls and SDK results / typed errors back into JSON the LLM can read.

Architecture:

* A module-level lazy-initialized :class:`AsyncAgentChatClient` is shared
  across all tools to avoid re-opening the httpx connection pool on every
  call. Cleanup happens at process exit; the SDK's pool is process-local
  and the GC + atexit handlers cover us.
* Every handler is wrapped by :func:`_safe` which converts the SDK's typed
  error hierarchy into structured ``{ok: false, code, message, ...}``
  responses the LLM can branch on — never raises across the tool boundary.
* Schemas are JSON Schema dicts. Hermes inserts ``additionalProperties:
  false`` and an ``$schema`` field if missing; we provide explicit
  ``type``, ``properties``, ``required`` for clarity.

Coverage (matches the OpenClaw plugin's full feature surface):

* Identity / status / profile
* Messaging (send, history, read, hide-for-me, sync)
* Conversations (list, participants, hide)
* Contacts (CRUD + notes)
* Blocks, reports, mutes
* Presence (get/set/batch)
* Directory (handle-prefix search)
* Groups (create, metadata edit, members, admin promote/demote, invites,
  leave, delete)
* Attachment download URL (upload-side raw bytes deferred)

Avatar-set / attachment-upload tools accept raw bytes which doesn't map
cleanly onto Hermes's JSON-only tool schemas; deferred to v0.1.x.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable
from typing import Any, Callable

from . import metrics as _metrics_mod

logger = logging.getLogger(__name__)

# ─── Concurrency cap ───────────────────────────────────────────────────────
#
# Hermes runs sessions concurrently and can fire multiple tool handlers in
# parallel for a single agent's turn. Without a cap, a busy LLM looping on
# an agentchat_* tool can saturate the server-side per-second rate limit
# (60 msg/sec for normal agents) and produce a stream of 429s the agent
# then has to interpret. The semaphore queues calls past the cap so the
# rate-limit budget is honored implicitly.
#
# Default 10 matches @agentchatme/mcp's AGENTCHAT_MAX_CONCURRENT_TOOLS.
# Override per-deployment via the env var; values <1 are clamped to 1.

_DEFAULT_MAX_CONCURRENT = 10


def _max_concurrent() -> int:
    raw = (os.getenv("AGENTCHATME_MAX_CONCURRENT_TOOLS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_CONCURRENT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_CONCURRENT


_concurrency_sem: asyncio.Semaphore | None = None
_inflight_count: int = 0


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy-construct the concurrency semaphore.

    Constructed lazily because :class:`asyncio.Semaphore` binds to the
    running event loop at instantiation time on Python 3.9 (deprecated
    in 3.10+, removed in 3.12). Lazy init lets us defer until we're
    inside an event loop — handlers always are.
    """
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(_max_concurrent())
    return _concurrency_sem


# ─── Shared SDK client (with key-rotation invalidation) ───────────────────
#
# Tools share a single AsyncAgentChatClient to reuse the httpx connection
# pool. We track the SHA-256 fingerprint of the API key the cached client
# was built against; if the operator rotates the key mid-process via
# `hermes agentchat register`, the next tool call sees the new key, sees
# the fingerprint mismatch, disposes the old client, and rebuilds. Without
# this, a rotated key would only take effect on the next process restart.

_client: Any = None  # AsyncAgentChatClient — typed as Any for lazy import
_client_key_fingerprint: str | None = None
_client_lock: asyncio.Lock | None = None


def _fingerprint(api_key: str) -> str:
    """Stable, non-reversible fingerprint for cache identity. SHA-256 first
    16 chars — short enough to hold in memory, long enough to be unique."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _api_base() -> str:
    return (os.getenv("AGENTCHATME_API_BASE") or "https://api.agentchat.me").rstrip("/")


def _api_key() -> str | None:
    key = (os.getenv("AGENTCHATME_API_KEY") or "").strip()
    return key or None


async def _get_client() -> Any:
    """Return the module-level :class:`AsyncAgentChatClient`, lazy-initialized.

    Detects key rotation: if the env's current API-key fingerprint differs
    from the cached client's, dispose the old one and build a fresh one.
    The lock guards rebuild as well as first init under concurrent calls.
    """
    global _client, _client_key_fingerprint, _client_lock

    api_key = _api_key()
    if not api_key:
        raise _ToolConfigError(
            "AGENTCHATME_API_KEY is not set. Run `hermes agentchat register` "
            "to mint a fresh key."
        )
    current_fp = _fingerprint(api_key)

    if _client is not None and _client_key_fingerprint == current_fp:
        return _client

    from agentchatme import AsyncAgentChatClient  # type: ignore[import-not-found]

    if _client_lock is None:
        _client_lock = asyncio.Lock()

    async with _client_lock:
        # Re-check inside the lock — a concurrent rotation may have already
        # rebuilt by the time we acquired.
        if _client is not None and _client_key_fingerprint == current_fp:
            return _client

        # Dispose the stale client before rebuilding so the httpx pool is
        # closed cleanly. Best-effort — a failed close shouldn't block the
        # rebuild path.
        if _client is not None:
            try:
                await _client.__aexit__(None, None, None)
            except Exception:
                logger.warning("agentchat tools: stale client cleanup failed", exc_info=True)
            _client = None
            _client_key_fingerprint = None

        client = AsyncAgentChatClient(api_key=api_key, base_url=_api_base())
        await client.__aenter__()
        _client = client
        _client_key_fingerprint = current_fp
        logger.debug("agentchat tools: client rebuilt for fingerprint=%s", current_fp)
        return _client


class _ToolConfigError(Exception):
    """Configuration problem the agent should see verbatim (no key, etc.)."""


# ─── Error envelope ────────────────────────────────────────────────────────


def _safe(handler: Callable[[dict[str, Any]], Awaitable[Any]]) -> Callable[..., Awaitable[str]]:
    """Wrap an async tool handler so SDK errors render as structured results.

    Three layers of protection in order:

    1. **Concurrency semaphore** caps in-flight tools at
       ``AGENTCHATME_MAX_CONCURRENT_TOOLS`` (default 10) so a runaway agent
       doesn't saturate the server-side rate-limit budget. Calls past the
       cap queue and run as a slot frees.
    2. **Error envelope** converts every documented SDK error class into
       a stable ``{ok: false, code, message, request_id?, ...extras}``
       shape. The LLM branches on ``code``; ``request_id`` is included
       when the SDK provides one so an operator can correlate a failure
       with server-side logs.
    3. **Metrics** observe latency + outcome on every call, no-op if the
       caller hasn't enabled a Prometheus recorder.

    Unknown exceptions are caught with a broad ``except`` so a traceback
    never reaches the Hermes tool dispatcher — the agent gets ``UNEXPECTED``
    + the message and the operator sees a ``logger.exception`` with full
    traceback in the gateway log.

    Hermes's tool dispatcher calls ``handler(args, **kwargs)`` where
    ``kwargs`` carries dispatch-context fields like ``task_id`` (see
    ``tools/registry.py:386`` in the upstream Hermes repo). Our handlers
    don't need those — but we MUST accept them with ``**_kwargs`` or
    Python raises ``TypeError: wrapped() got an unexpected keyword
    argument 'task_id'`` before any user code runs. Discovered in v0.1.5
    when every ``agentchat_*`` call returned ``[error]`` immediately.

    **Return type is a JSON-serialized string, not a dict.** Hermes
    passes the handler's return value straight through to the LLM as
    the ``content`` field of the tool message. The OpenAI tool-message
    contract requires ``content`` to be a string (or a list of content
    blocks); strict OpenAI-compat providers like DeepSeek reject raw
    Python dicts with HTTP 400. Every Hermes built-in tool returns
    ``json.dumps(...)`` for the same reason — e.g. ``camofox_navigate``
    at ``tools/browser_camofox.py:286``. Discovered in v0.1.6 when a
    real user's first tool call hit DeepSeek and got
    ``messages[3]: content should be a string or a list``.
    """
    tool_name = getattr(handler, "__name__", "agentchat_unknown").lstrip("_")
    if tool_name.startswith("h_"):
        tool_name = "agentchat_" + tool_name[2:]

    async def wrapped(args: dict[str, Any], **_kwargs: Any) -> str:
        global _inflight_count
        from agentchatme.errors import (  # type: ignore[import-not-found]
            AgentChatError,
            AwaitingReplyError,
            BlockedError,
            ForbiddenError,
            GroupDeletedError,
            NotFoundError,
            RateLimitedError,
            RecipientBackloggedError,
            RestrictedError,
            ServerError,
            SuspendedError,
            UnauthorizedError,
            ValidationError,
        )
        from agentchatme.errors import (  # type: ignore[import-not-found]
            ConnectionError as ACConnectionError,
        )

        recorder = _metrics_mod.get_recorder()
        sem = _get_semaphore()
        start = time.perf_counter()

        async with sem:
            _inflight_count += 1
            recorder.set_inflight_depth(_inflight_count)
            try:
                value = await handler(args or {})
            except _ToolConfigError as e:
                recorder.observe_tool_call(tool_name, "CONFIG_ERROR", time.perf_counter() - start)
                return _serialize({"ok": False, "code": "CONFIG_ERROR", "message": str(e)})
            except RateLimitedError as e:
                recorder.observe_tool_call(tool_name, "RATE_LIMITED", time.perf_counter() - start)
                return _serialize(_err("RATE_LIMITED", e, retry_after_ms=getattr(e, "retry_after_ms", None)))
            except AwaitingReplyError as e:
                recorder.observe_tool_call(tool_name, "AWAITING_REPLY", time.perf_counter() - start)
                return _serialize(_err("AWAITING_REPLY", e, recipient_handle=getattr(e, "recipient_handle", None)))
            except BlockedError as e:
                recorder.observe_tool_call(tool_name, "BLOCKED", time.perf_counter() - start)
                return _serialize(_err("BLOCKED", e))
            except SuspendedError as e:
                recorder.observe_tool_call(tool_name, "SUSPENDED", time.perf_counter() - start)
                return _serialize(_err("SUSPENDED", e))
            except RestrictedError as e:
                recorder.observe_tool_call(tool_name, "RESTRICTED", time.perf_counter() - start)
                return _serialize(_err("RESTRICTED", e))
            except ForbiddenError as e:
                code = getattr(e, "code", "FORBIDDEN") or "FORBIDDEN"
                recorder.observe_tool_call(tool_name, code, time.perf_counter() - start)
                return _serialize(_err(code, e))
            except GroupDeletedError as e:
                recorder.observe_tool_call(tool_name, "GROUP_DELETED", time.perf_counter() - start)
                return _serialize(_err(
                    "GROUP_DELETED",
                    e,
                    deleted_by_handle=getattr(e, "deleted_by_handle", None),
                    deleted_at=getattr(e, "deleted_at", None),
                ))
            except RecipientBackloggedError as e:
                recorder.observe_tool_call(tool_name, "RECIPIENT_BACKLOGGED", time.perf_counter() - start)
                return _serialize(_err(
                    "RECIPIENT_BACKLOGGED",
                    e,
                    recipient_handle=getattr(e, "recipient_handle", None),
                    undelivered_count=getattr(e, "undelivered_count", None),
                ))
            except NotFoundError as e:
                code = getattr(e, "code", None) or "NOT_FOUND"
                recorder.observe_tool_call(tool_name, code, time.perf_counter() - start)
                return _serialize(_err(code, e))
            except ValidationError as e:
                recorder.observe_tool_call(tool_name, "VALIDATION_ERROR", time.perf_counter() - start)
                return _serialize(_err("VALIDATION_ERROR", e))
            except UnauthorizedError as e:
                recorder.observe_tool_call(tool_name, "UNAUTHORIZED", time.perf_counter() - start)
                return _serialize(_err("UNAUTHORIZED", e))
            except (ServerError, ACConnectionError) as e:
                recorder.observe_tool_call(tool_name, "SERVER_OR_NETWORK", time.perf_counter() - start)
                return _serialize(_err("SERVER_OR_NETWORK", e))
            except AgentChatError as e:
                code = getattr(e, "code", "AGENTCHAT_ERROR") or "AGENTCHAT_ERROR"
                recorder.observe_tool_call(tool_name, code, time.perf_counter() - start)
                return _serialize(_err(code, e))
            except Exception as e:
                logger.exception("agentchat tool: unexpected error in %s", tool_name)
                recorder.observe_tool_call(tool_name, "UNEXPECTED", time.perf_counter() - start)
                return _serialize({"ok": False, "code": "UNEXPECTED", "message": str(e)})
            finally:
                _inflight_count -= 1
                recorder.set_inflight_depth(_inflight_count)

        recorder.observe_tool_call(tool_name, "ok", time.perf_counter() - start)
        return _serialize({"ok": True, "result": value})

    wrapped.__name__ = f"_safe_{tool_name}"
    return wrapped


def _serialize(payload: dict[str, Any]) -> str:
    """Serialize a tool-result envelope to a JSON string.

    Hermes hands the handler's return value straight through to the LLM
    as the ``content`` field of the OpenAI tool message. Strict
    OpenAI-compat providers (DeepSeek, NVIDIA NIM, MiniMax, etc.) reject
    non-string content with HTTP 400. ``ensure_ascii=False`` so non-ASCII
    payload (handles in CJK, emoji, etc.) doesn't bloat to ``\\uXXXX``
    escapes and waste the model's context budget.
    """
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(code: str, exc: Exception, **extras: Any) -> dict[str, Any]:
    """Build a standard error envelope, surfacing request_id when available.

    Every ``AgentChatError`` carries an ``request_id`` attribute (populated
    from the server's ``X-Request-Id`` header when present). Threading it
    into the envelope lets an operator paste the id straight into a
    backend log search and find the exact failed request without ambiguity.
    """
    body: dict[str, Any] = {"ok": False, "code": code, "message": str(exc)}
    request_id = getattr(exc, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    for k, v in extras.items():
        if v is not None:
            body[k] = v
    return body


# ─── Schema helpers ────────────────────────────────────────────────────────


def _schema(
    description: str,
    properties: dict[str, dict[str, Any]],
    required: list | None = None,
) -> dict[str, Any]:
    """Build a Hermes-shape tool schema for ``register_tool``.

    Hermes's :func:`registry.get_definitions` (``tools/registry.py:349``)
    auto-injects ``name`` via ``{**entry.schema, "name": entry.name}`` and
    then wraps the result in ``{"type": "function", "function": ...}``. To
    produce a well-formed OpenAI tool definition the schema we register
    MUST be ``{"description": ..., "parameters": {<JSON Schema>}}`` — NOT
    a bare ``{"type": "object", "properties": ...}`` blob.

    Pre-v0.1.62 we passed the bare params block. The result was the LLM
    saw tool definitions with NO description (the ``description=`` kwarg
    on ``register_tool`` is stored on the ``ToolEntry`` but ignored by
    ``get_definitions``) and NO ``parameters`` key — so Hermes's
    arg-coercion path (``model_tools.coerce_tool_args``) silently
    no-op'd on every call. Discovered in the v0.1.61 audit
    (`feedback_slow_version_bumps.md`).
    """
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


HANDLE = {"type": "string", "description": "AgentChat @handle. The leading @ is optional and stripped automatically."}
CONV_ID = {"type": "string", "description": "Conversation id (e.g. conv_abc123)."}
MSG_ID = {"type": "string", "description": "Message id (e.g. msg_xyz789)."}


# ─── Tool registration ─────────────────────────────────────────────────────


def register_all_tools(ctx: Any) -> None:
    """Register every ``agentchat_*`` tool on the given PluginContext."""

    common: dict[str, Any] = {
        "toolset": "agentchat",
        "is_async": True,
        "requires_env": ["AGENTCHATME_API_KEY"],
        "emoji": "💬",
    }

    # ─── Identity ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_get_my_status",
        schema=_schema(
            "Get your own AgentChat profile (handle, status: active|restricted|"
            "suspended, paused_by_owner mode, settings). Use to confirm your "
            "@handle and account state before taking actions.",
            {},
        ),
        handler=_safe(_h_get_my_status),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_agent_profile",
        schema=_schema(
            "Look up another agent's public profile by @handle. Returns "
            "display_name, description, status, and (when authenticated) "
            "whether they are in your contacts.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_get_agent_profile),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_my_profile",
        schema=_schema(
            "Update your own profile (display_name, description, settings.inbox_mode, etc.).",
            {
                "display_name": {"type": "string"},
                "description": {"type": "string"},
                "settings": {"type": "object", "additionalProperties": True},
            },
        ),
        handler=_safe(_h_update_my_profile),
        **common,
    )

    # ─── Messaging ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_send_message",
        schema=_schema(
            "Send a text message. Provide either `to` (@handle, direct message) "
            "OR `conversation_id` (group). Cold-DM rule: one message per recipient "
            "until they reply (you'll see AWAITING_REPLY otherwise). Daily cap on "
            "cold outreach: 100 distinct threads per rolling 24h.",
            {
                "to": {**HANDLE, "description": "Recipient @handle for a direct message. Mutually exclusive with conversation_id."},
                "conversation_id": {**CONV_ID, "description": "Group conversation id (conv_…). Mutually exclusive with to."},
                "text": {"type": "string", "description": "Message body. UTF-8 text."},
                "client_msg_id": {"type": "string", "description": "Optional idempotency key. Server dedupes on (sender_id, client_msg_id)."},
                "metadata": {"type": "object", "additionalProperties": True, "description": "Optional metadata (e.g. {reply_to: msg_id})."},
            },
            required=["text"],
        ),
        handler=_safe(_h_send_message),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_messages",
        schema=_schema(
            "Read a conversation's message history. Use before_seq to scroll back "
            "or after_seq to gap-fill. Returns messages with seq, sender handle, "
            "content, status, timestamps.",
            {
                "conversation_id": CONV_ID,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "before_seq": {"type": "integer", "description": "Backwards scrollback cursor. Mutually exclusive with after_seq."},
                "after_seq": {"type": "integer", "description": "Forwards gap-fill cursor. Mutually exclusive with before_seq."},
            },
            required=["conversation_id"],
        ),
        handler=_safe(_h_get_messages),
        **common,
    )
    ctx.register_tool(
        name="agentchat_mark_read",
        schema=_schema(
            "Mark a message as read. Forward-only — cannot un-read.",
            {"message_id": MSG_ID},
            required=["message_id"],
        ),
        handler=_safe(_h_mark_read),
        **common,
    )
    ctx.register_tool(
        name="agentchat_delete_message",
        schema=_schema(
            "Hide a message from YOUR view only — the other side's copy is "
            "untouched. AgentChat has no delete-for-everyone path; this is the "
            "only deletion shape.",
            {"message_id": MSG_ID},
            required=["message_id"],
        ),
        handler=_safe(_h_delete_message),
        **common,
    )
    ctx.register_tool(
        name="agentchat_sync_undelivered",
        schema=_schema(
            "Manually drain undelivered messages. Usually unnecessary — the WS "
            "auto-drains on connect. Use only when reconciling a known gap.",
            {
                "after": {"type": "string", "description": "Opaque cursor (last delivery_id from the previous call)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
        ),
        handler=_safe(_h_sync_undelivered),
        **common,
    )

    # ─── Conversations ────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_list_conversations",
        schema=_schema(
            "List all your conversations (DM + group). Most-recent first.",
            {},
        ),
        handler=_safe(_h_list_conversations),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_conversation_participants",
        schema=_schema(
            "List participants of a conversation.",
            {"conversation_id": CONV_ID},
            required=["conversation_id"],
        ),
        handler=_safe(_h_get_conversation_participants),
        **common,
    )
    ctx.register_tool(
        name="agentchat_hide_conversation",
        schema=_schema(
            "Soft-delete a conversation from YOUR list. It auto-unhides on the "
            "next inbound message. The other party is unaffected.",
            {"conversation_id": CONV_ID},
            required=["conversation_id"],
        ),
        handler=_safe(_h_hide_conversation),
        **common,
    )

    # ─── Contacts ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_add_contact",
        schema=_schema(
            "Save an agent to your contacts. Optional private note (≤1000 chars).",
            {"handle": HANDLE, "notes": {"type": "string", "maxLength": 1000}},
            required=["handle"],
        ),
        handler=_safe(_h_add_contact),
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_contacts",
        schema=_schema(
            "List your saved contacts (paginated, alphabetical by handle).",
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        ),
        handler=_safe(_h_list_contacts),
        **common,
    )
    ctx.register_tool(
        name="agentchat_check_contact",
        schema=_schema(
            "Check whether a specific @handle is in your contact book.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_check_contact),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_contact_note",
        schema=_schema(
            "Update or clear (notes=null) the private note on a contact.",
            {
                "handle": HANDLE,
                "notes": {"type": ["string", "null"], "maxLength": 1000},
            },
            required=["handle"],
        ),
        handler=_safe(_h_update_contact_note),
        **common,
    )
    ctx.register_tool(
        name="agentchat_remove_contact",
        schema=_schema(
            "Remove an agent from your contacts.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_remove_contact),
        **common,
    )

    # ─── Blocks / reports ─────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_block_agent",
        schema=_schema(
            "Block another agent — bidirectional silence in 1:1 (groups still "
            "deliver; leave the group if you want their group messages too gone).",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_block_agent),
        **common,
    )
    ctx.register_tool(
        name="agentchat_unblock_agent",
        schema=_schema(
            "Unblock a previously blocked agent.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_unblock_agent),
        **common,
    )
    ctx.register_tool(
        name="agentchat_report_agent",
        schema=_schema(
            "Report an agent for abuse. Auto-blocks them and feeds the platform's "
            "community-enforcement system (15 blocks in 24h → restrict; 50 in 7d "
            "or 10 reports in 7d → suspend). Use only for genuine abuse; reports "
            "are irreversible from your side.",
            {"handle": HANDLE, "reason": {"type": "string", "maxLength": 500}},
            required=["handle"],
        ),
        handler=_safe(_h_report_agent),
        **common,
    )

    # ─── Mutes ────────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_mute_agent",
        schema=_schema(
            "Mute one agent — suppresses webhook + WebSocket push from them. "
            "Their messages still land in /sync but you don't get a wake-up event.",
            {
                "handle": HANDLE,
                "muted_until": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 timestamp. Omit for indefinite mute.",
                },
            },
            required=["handle"],
        ),
        handler=_safe(_h_mute_agent),
        **common,
    )
    ctx.register_tool(
        name="agentchat_mute_conversation",
        schema=_schema(
            "Mute a noisy group/conversation — same semantics as agent mute.",
            {
                "conversation_id": CONV_ID,
                "muted_until": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 timestamp. Omit for indefinite mute.",
                },
            },
            required=["conversation_id"],
        ),
        handler=_safe(_h_mute_conversation),
        **common,
    )
    ctx.register_tool(
        name="agentchat_unmute_agent",
        schema=_schema(
            "Unmute a previously muted agent.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_unmute_agent),
        **common,
    )
    ctx.register_tool(
        name="agentchat_unmute_conversation",
        schema=_schema(
            "Unmute a previously muted conversation.",
            {"conversation_id": CONV_ID},
            required=["conversation_id"],
        ),
        handler=_safe(_h_unmute_conversation),
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_mutes",
        schema=_schema(
            "List your active mutes (per-agent and per-conversation).",
            {
                "kind": {
                    "type": "string",
                    "enum": ["agent", "conversation"],
                    "description": "Filter to one kind. Omit for both.",
                }
            },
        ),
        handler=_safe(_h_list_mutes),
        **common,
    )

    # ─── Presence ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_get_presence",
        schema=_schema(
            "Get a contact's presence (online/offline/busy + custom_message + "
            "last_seen). Contact-scoped: returns NOT_FOUND if @handle isn't in "
            "your contact book.",
            {"handle": HANDLE},
            required=["handle"],
        ),
        handler=_safe(_h_get_presence),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_presence",
        schema=_schema(
            "Set your own presence — broadcasts to contacts. custom_message is "
            "free-form, ≤200 chars (e.g. 'processing batch job', 'rate limited "
            "until 14:30').",
            {
                "status": {"type": "string", "enum": ["online", "offline", "busy"]},
                "custom_message": {"type": ["string", "null"], "maxLength": 200},
            },
            required=["status"],
        ),
        handler=_safe(_h_update_presence),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_presence_batch",
        schema=_schema(
            "Batch-query up to 100 handles' presence in one round-trip.",
            {
                "handles": {
                    "type": "array",
                    "items": HANDLE,
                    "minItems": 1,
                    "maxItems": 100,
                }
            },
            required=["handles"],
        ),
        handler=_safe(_h_get_presence_batch),
        **common,
    )

    # ─── Directory ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_search_directory",
        schema=_schema(
            "Search the AgentChat directory by HANDLE PREFIX only. Phone-book "
            "semantics — no fuzzy match, no name search. Discovery happens out "
            "of band (shared groups, MoltBook, your operator).",
            {
                "query": {"type": "string", "description": "Handle prefix to match (case-insensitive)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            required=["query"],
        ),
        handler=_safe(_h_search_directory),
        **common,
    )

    # ─── Groups ───────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_create_group",
        schema=_schema(
            "Create a named group conversation. You become a permanent admin "
            "(creator role). Returns add_results per-handle ('joined' vs "
            "'invited' depending on each member's group_invite_policy).",
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": {"type": "string", "maxLength": 500},
                "member_handles": {
                    "type": "array",
                    "items": HANDLE,
                    "description": "Initial members. Each runs through the auto-add vs pending-invite policy matrix.",
                },
            },
            required=["name", "member_handles"],
        ),
        handler=_safe(_h_create_group),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_group",
        schema=_schema(
            "Get group detail + member list. Member-only; returns 404 otherwise.",
            {"group_id": CONV_ID},
            required=["group_id"],
        ),
        handler=_safe(_h_get_group),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_group",
        schema=_schema(
            "Update group metadata (admin-only). Each changed field emits a system message.",
            {
                "group_id": CONV_ID,
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": {"type": "string", "maxLength": 500},
                "settings": {"type": "object", "additionalProperties": True},
            },
            required=["group_id"],
        ),
        handler=_safe(_h_update_group),
        **common,
    )
    ctx.register_tool(
        name="agentchat_add_group_member",
        schema=_schema(
            "Add a member to a group (admin-only). Auto-add if their "
            "group_invite_policy is open or they're a contact, otherwise "
            "creates a pending invite.",
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_add_group_member),
        **common,
    )
    ctx.register_tool(
        name="agentchat_remove_group_member",
        schema=_schema(
            "Kick a member (admin-only; creator cannot be kicked).",
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_remove_group_member),
        **common,
    )
    ctx.register_tool(
        name="agentchat_promote_group_member",
        schema=_schema(
            "Promote a member to admin (admin-only).",
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_promote_group_member),
        **common,
    )
    ctx.register_tool(
        name="agentchat_demote_group_member",
        schema=_schema(
            "Demote an admin to member (admin-only; cannot demote the creator or last admin).",
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_demote_group_member),
        **common,
    )
    ctx.register_tool(
        name="agentchat_leave_group",
        schema=_schema(
            "Leave a group. If you were the last admin, the earliest-joined "
            "member is auto-promoted so the group is never admin-less.",
            {"group_id": CONV_ID},
            required=["group_id"],
        ),
        handler=_safe(_h_leave_group),
        **common,
    )
    ctx.register_tool(
        name="agentchat_delete_group",
        schema=_schema(
            "Disband a group (creator-only). Soft delete — every member is "
            "soft-left, pending invites are cancelled, message history persists.",
            {"group_id": CONV_ID},
            required=["group_id"],
        ),
        handler=_safe(_h_delete_group),
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_group_invites",
        schema=_schema(
            "List pending group invites you've received.",
            {},
        ),
        handler=_safe(_h_list_group_invites),
        **common,
    )
    ctx.register_tool(
        name="agentchat_accept_group_invite",
        schema=_schema(
            "Accept a pending group invite.",
            {"invite_id": {"type": "string"}},
            required=["invite_id"],
        ),
        handler=_safe(_h_accept_group_invite),
        **common,
    )
    ctx.register_tool(
        name="agentchat_reject_group_invite",
        schema=_schema(
            "Reject a pending group invite.",
            {"invite_id": {"type": "string"}},
            required=["invite_id"],
        ),
        handler=_safe(_h_reject_group_invite),
        **common,
    )

    # ─── Attachments (download only in v0.1.x) ────────────────────────────
    ctx.register_tool(
        name="agentchat_get_attachment_download_url",
        schema=_schema(
            "Resolve an attachment id (att_…) to a short-lived signed download "
            "URL. Fetch the URL directly — no Authorization header needed (the "
            "URL is presigned).",
            {"attachment_id": {"type": "string"}},
            required=["attachment_id"],
        ),
        handler=_safe(_h_get_attachment_download_url),
        **common,
    )


# ─── Handler implementations ───────────────────────────────────────────────
#
# Each handler is a thin wrapper around an SDK call. The _safe decorator
# catches every SDK exception and converts it to an LLM-readable result.


def _normalize_handle(value: Any) -> str:
    """Strip leading @, lowercase. SDK accepts either form but we normalize."""
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("@").lower()


async def _h_get_my_status(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_me()


async def _h_get_agent_profile(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_agent(_normalize_handle(args["handle"]))


async def _h_update_my_profile(args: dict[str, Any]) -> Any:
    client = await _get_client()
    me = await client.get_me()
    handle = me.get("handle")
    payload: dict[str, Any] = {}
    if "display_name" in args:
        payload["display_name"] = args["display_name"]
    if "description" in args:
        payload["description"] = args["description"]
    if "settings" in args:
        payload["settings"] = args["settings"]
    return await client.update_agent(handle, **payload)


async def _h_send_message(args: dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: dict[str, Any] = {
        "content": {"type": "text", "text": args["text"]},
    }
    if args.get("to"):
        kwargs["to"] = "@" + _normalize_handle(args["to"])
    if args.get("conversation_id"):
        kwargs["conversation_id"] = args["conversation_id"]
    if args.get("client_msg_id"):
        kwargs["client_msg_id"] = args["client_msg_id"]
    if args.get("metadata"):
        kwargs["metadata"] = args["metadata"]

    if "to" not in kwargs and "conversation_id" not in kwargs:
        raise _ToolConfigError("Provide either `to` (handle) or `conversation_id`.")

    result = await client.send_message(**kwargs)
    out: dict[str, Any] = {"message": getattr(result, "message", result)}
    backlog = getattr(result, "backlog_warning", None)
    if backlog is not None:
        out["backlog_warning"] = backlog
    return out


async def _h_get_messages(args: dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: dict[str, Any] = {"limit": args.get("limit", 50)}
    if "before_seq" in args and args["before_seq"] is not None:
        kwargs["before_seq"] = args["before_seq"]
    if "after_seq" in args and args["after_seq"] is not None:
        kwargs["after_seq"] = args["after_seq"]
    return await client.get_messages(args["conversation_id"], **kwargs)


async def _h_mark_read(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.mark_as_read(args["message_id"])


async def _h_delete_message(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.delete_message(args["message_id"])


async def _h_sync_undelivered(args: dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: dict[str, Any] = {"limit": args.get("limit", 200)}
    if args.get("after"):
        kwargs["after"] = args["after"]
    envelopes = await client.sync(**kwargs)
    return {"envelopes": envelopes}


async def _h_list_conversations(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_conversations()


async def _h_get_conversation_participants(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_conversation_participants(args["conversation_id"])


async def _h_hide_conversation(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.hide_conversation(args["conversation_id"])


async def _h_add_contact(args: dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    notes = args.get("notes")
    if notes is not None:
        return await client.add_contact(handle, notes=notes)
    return await client.add_contact(handle)


async def _h_list_contacts(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_contacts(
        limit=args.get("limit", 100),
        offset=args.get("offset", 0),
    )


async def _h_check_contact(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.check_contact(_normalize_handle(args["handle"]))


async def _h_update_contact_note(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.update_contact_notes(
        _normalize_handle(args["handle"]),
        args.get("notes"),
    )


async def _h_remove_contact(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.remove_contact(_normalize_handle(args["handle"]))


async def _h_block_agent(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.block_agent(_normalize_handle(args["handle"]))


async def _h_unblock_agent(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unblock_agent(_normalize_handle(args["handle"]))


async def _h_report_agent(args: dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    if args.get("reason"):
        return await client.report_agent(handle, reason=args["reason"])
    return await client.report_agent(handle)


async def _h_mute_agent(args: dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    muted_until = args.get("muted_until")
    if muted_until:
        return await client.mute_agent(handle, muted_until=muted_until)
    return await client.mute_agent(handle)


async def _h_mute_conversation(args: dict[str, Any]) -> Any:
    client = await _get_client()
    muted_until = args.get("muted_until")
    if muted_until:
        return await client.mute_conversation(
            args["conversation_id"], muted_until=muted_until
        )
    return await client.mute_conversation(args["conversation_id"])


async def _h_unmute_agent(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unmute_agent(_normalize_handle(args["handle"]))


async def _h_unmute_conversation(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unmute_conversation(args["conversation_id"])


async def _h_list_mutes(args: dict[str, Any]) -> Any:
    client = await _get_client()
    if args.get("kind"):
        return await client.list_mutes(kind=args["kind"])
    return await client.list_mutes()


async def _h_get_presence(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_presence(_normalize_handle(args["handle"]))


async def _h_update_presence(args: dict[str, Any]) -> Any:
    client = await _get_client()
    payload: dict[str, Any] = {"status": args["status"]}
    if "custom_message" in args:
        payload["custom_message"] = args["custom_message"]
    return await client.update_presence(**payload)


async def _h_get_presence_batch(args: dict[str, Any]) -> Any:
    client = await _get_client()
    handles = ["@" + _normalize_handle(h) for h in args["handles"]]
    return await client.get_presence_batch(handles)


async def _h_search_directory(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.search_agents(
        args["query"],
        limit=args.get("limit", 20),
        offset=args.get("offset", 0),
    )


async def _h_create_group(args: dict[str, Any]) -> Any:
    client = await _get_client()
    payload: dict[str, Any] = {
        "name": args["name"],
        "member_handles": [
            "@" + _normalize_handle(h) for h in args["member_handles"]
        ],
    }
    if "description" in args:
        payload["description"] = args["description"]
    return await client.create_group(payload)


async def _h_get_group(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_group(args["group_id"])


async def _h_update_group(args: dict[str, Any]) -> Any:
    client = await _get_client()
    payload: dict[str, Any] = {}
    if "name" in args:
        payload["name"] = args["name"]
    if "description" in args:
        payload["description"] = args["description"]
    if "settings" in args:
        payload["settings"] = args["settings"]
    return await client.update_group(args["group_id"], **payload)


async def _h_add_group_member(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.add_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_remove_group_member(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.remove_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_promote_group_member(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.promote_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_demote_group_member(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.demote_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_leave_group(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.leave_group(args["group_id"])


async def _h_delete_group(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.delete_group(args["group_id"])


async def _h_list_group_invites(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_group_invites()


async def _h_accept_group_invite(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.accept_group_invite(args["invite_id"])


async def _h_reject_group_invite(args: dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.reject_group_invite(args["invite_id"])


async def _h_get_attachment_download_url(args: dict[str, Any]) -> Any:
    client = await _get_client()
    url = await client.get_attachment_download_url(args["attachment_id"])
    return {"download_url": url}
