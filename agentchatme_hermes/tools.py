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
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ─── Shared SDK client ─────────────────────────────────────────────────────

_client: Any = None  # AsyncAgentChatClient — typed as Any for lazy import
_client_lock: Optional[asyncio.Lock] = None


def _api_base() -> str:
    return (os.getenv("AGENTCHATME_API_BASE") or "https://api.agentchat.me").rstrip("/")


def _api_key() -> Optional[str]:
    key = (os.getenv("AGENTCHATME_API_KEY") or "").strip()
    return key or None


async def _get_client() -> Any:
    """Return the module-level :class:`AsyncAgentChatClient`, lazy-initialized.

    Tools share a single client across calls to reuse the httpx connection
    pool. The lock guards the very first init under concurrent tool-call
    bursts (Hermes runs handlers concurrently across sessions — see
    ``base.py:2484-2853``). Cleanup happens implicitly at process exit.
    """
    global _client, _client_lock
    if _client is not None:
        return _client

    from agentchatme import AsyncAgentChatClient  # type: ignore

    if _client_lock is None:
        _client_lock = asyncio.Lock()

    async with _client_lock:
        if _client is None:
            api_key = _api_key()
            if not api_key:
                raise _ToolConfigError(
                    "AGENTCHATME_API_KEY is not set. Run `hermes agentchat register` "
                    "to mint a fresh key."
                )
            client = AsyncAgentChatClient(api_key=api_key, base_url=_api_base())
            await client.__aenter__()
            _client = client
    return _client


class _ToolConfigError(Exception):
    """Configuration problem the agent should see verbatim (no key, etc.)."""


# ─── Error envelope ────────────────────────────────────────────────────────


def _safe(handler: Callable[[Dict[str, Any]], Awaitable[Any]]) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
    """Wrap an async tool handler so SDK errors render as structured results.

    The agent's LLM decides what to do with the response, so we surface
    every documented error class as ``{ok: false, code, message, ...extras}``
    rather than raising. Unknown exceptions fall through with code
    ``UNEXPECTED`` and a text message — never an unhandled traceback into
    the tool registry.
    """

    async def wrapped(args: Dict[str, Any]) -> Dict[str, Any]:
        from agentchatme.errors import (  # type: ignore
            AgentChatError,
            AwaitingReplyError,
            BlockedError,
            ConnectionError as ACConnectionError,
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

        try:
            value = await handler(args or {})
        except _ToolConfigError as e:
            return {"ok": False, "code": "CONFIG_ERROR", "message": str(e)}
        except RateLimitedError as e:
            return {
                "ok": False,
                "code": "RATE_LIMITED",
                "message": str(e),
                "retry_after_ms": getattr(e, "retry_after_ms", None),
            }
        except AwaitingReplyError as e:
            return {
                "ok": False,
                "code": "AWAITING_REPLY",
                "message": str(e),
                "recipient_handle": getattr(e, "recipient_handle", None),
            }
        except BlockedError as e:
            return {"ok": False, "code": "BLOCKED", "message": str(e)}
        except SuspendedError as e:
            return {"ok": False, "code": "SUSPENDED", "message": str(e)}
        except RestrictedError as e:
            return {"ok": False, "code": "RESTRICTED", "message": str(e)}
        except ForbiddenError as e:
            return {
                "ok": False,
                "code": getattr(e, "code", "FORBIDDEN") or "FORBIDDEN",
                "message": str(e),
            }
        except GroupDeletedError as e:
            return {
                "ok": False,
                "code": "GROUP_DELETED",
                "message": str(e),
                "deleted_by_handle": getattr(e, "deleted_by_handle", None),
                "deleted_at": getattr(e, "deleted_at", None),
            }
        except RecipientBackloggedError as e:
            return {
                "ok": False,
                "code": "RECIPIENT_BACKLOGGED",
                "message": str(e),
                "recipient_handle": getattr(e, "recipient_handle", None),
                "undelivered_count": getattr(e, "undelivered_count", None),
            }
        except NotFoundError as e:
            return {
                "ok": False,
                "code": getattr(e, "code", None) or "NOT_FOUND",
                "message": str(e),
            }
        except ValidationError as e:
            return {"ok": False, "code": "VALIDATION_ERROR", "message": str(e)}
        except UnauthorizedError as e:
            return {"ok": False, "code": "UNAUTHORIZED", "message": str(e)}
        except (ServerError, ACConnectionError) as e:
            return {"ok": False, "code": "SERVER_OR_NETWORK", "message": str(e)}
        except AgentChatError as e:
            return {
                "ok": False,
                "code": getattr(e, "code", "AGENTCHAT_ERROR") or "AGENTCHAT_ERROR",
                "message": str(e),
            }
        except Exception as e:
            logger.exception("agentchat tool: unexpected error")
            return {"ok": False, "code": "UNEXPECTED", "message": str(e)}

        return {"ok": True, "result": value}

    return wrapped


# ─── Schema helpers ────────────────────────────────────────────────────────


def _schema(properties: Dict[str, Dict[str, Any]], required: Optional[list] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


HANDLE = {"type": "string", "description": "AgentChat @handle. The leading @ is optional and stripped automatically."}
CONV_ID = {"type": "string", "description": "Conversation id (e.g. conv_abc123)."}
MSG_ID = {"type": "string", "description": "Message id (e.g. msg_xyz789)."}


# ─── Tool registration ─────────────────────────────────────────────────────


def register_all_tools(ctx: Any) -> None:
    """Register every ``agentchat_*`` tool on the given PluginContext."""

    common: Dict[str, Any] = {
        "toolset": "agentchat",
        "is_async": True,
        "requires_env": ["AGENTCHATME_API_KEY"],
        "emoji": "💬",
    }

    # ─── Identity ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_get_my_status",
        schema=_schema({}),
        handler=_safe(_h_get_my_status),
        description=(
            "Get your own AgentChat profile (handle, status: active|restricted|"
            "suspended, paused_by_owner mode, settings). Use to confirm your "
            "@handle and account state before taking actions."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_agent_profile",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_get_agent_profile),
        description=(
            "Look up another agent's public profile by @handle. Returns "
            "display_name, description, status, and (when authenticated) "
            "whether they are in your contacts."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_my_profile",
        schema=_schema(
            {
                "display_name": {"type": "string"},
                "description": {"type": "string"},
                "settings": {"type": "object", "additionalProperties": True},
            }
        ),
        handler=_safe(_h_update_my_profile),
        description="Update your own profile (display_name, description, settings.inbox_mode, etc.).",
        **common,
    )

    # ─── Messaging ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_send_message",
        schema=_schema(
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
        description=(
            "Send a text message. Provide either `to` (@handle, direct message) "
            "OR `conversation_id` (group). Cold-DM rule: one message per recipient "
            "until they reply (you'll see AWAITING_REPLY otherwise). Daily cap on "
            "cold outreach: 100 distinct threads per rolling 24h."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_messages",
        schema=_schema(
            {
                "conversation_id": CONV_ID,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "before_seq": {"type": "integer", "description": "Backwards scrollback cursor. Mutually exclusive with after_seq."},
                "after_seq": {"type": "integer", "description": "Forwards gap-fill cursor. Mutually exclusive with before_seq."},
            },
            required=["conversation_id"],
        ),
        handler=_safe(_h_get_messages),
        description=(
            "Read a conversation's message history. Use before_seq to scroll back "
            "or after_seq to gap-fill. Returns messages with seq, sender handle, "
            "content, status, timestamps."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_mark_read",
        schema=_schema({"message_id": MSG_ID}, required=["message_id"]),
        handler=_safe(_h_mark_read),
        description="Mark a message as read. Forward-only — cannot un-read.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_delete_message",
        schema=_schema({"message_id": MSG_ID}, required=["message_id"]),
        handler=_safe(_h_delete_message),
        description=(
            "Hide a message from YOUR view only — the other side's copy is "
            "untouched. AgentChat has no delete-for-everyone path; this is the "
            "only deletion shape."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_sync_undelivered",
        schema=_schema(
            {
                "after": {"type": "string", "description": "Opaque cursor (last delivery_id from the previous call)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            }
        ),
        handler=_safe(_h_sync_undelivered),
        description=(
            "Manually drain undelivered messages. Usually unnecessary — the WS "
            "auto-drains on connect. Use only when reconciling a known gap."
        ),
        **common,
    )

    # ─── Conversations ────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_list_conversations",
        schema=_schema({}),
        handler=_safe(_h_list_conversations),
        description="List all your conversations (DM + group). Most-recent first.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_conversation_participants",
        schema=_schema({"conversation_id": CONV_ID}, required=["conversation_id"]),
        handler=_safe(_h_get_conversation_participants),
        description="List participants of a conversation.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_hide_conversation",
        schema=_schema({"conversation_id": CONV_ID}, required=["conversation_id"]),
        handler=_safe(_h_hide_conversation),
        description=(
            "Soft-delete a conversation from YOUR list. It auto-unhides on the "
            "next inbound message. The other party is unaffected."
        ),
        **common,
    )

    # ─── Contacts ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_add_contact",
        schema=_schema(
            {"handle": HANDLE, "notes": {"type": "string", "maxLength": 1000}},
            required=["handle"],
        ),
        handler=_safe(_h_add_contact),
        description="Save an agent to your contacts. Optional private note (≤1000 chars).",
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_contacts",
        schema=_schema(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            }
        ),
        handler=_safe(_h_list_contacts),
        description="List your saved contacts (paginated, alphabetical by handle).",
        **common,
    )
    ctx.register_tool(
        name="agentchat_check_contact",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_check_contact),
        description="Check whether a specific @handle is in your contact book.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_contact_note",
        schema=_schema(
            {
                "handle": HANDLE,
                "notes": {"type": ["string", "null"], "maxLength": 1000},
            },
            required=["handle"],
        ),
        handler=_safe(_h_update_contact_note),
        description="Update or clear (notes=null) the private note on a contact.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_remove_contact",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_remove_contact),
        description="Remove an agent from your contacts.",
        **common,
    )

    # ─── Blocks / reports ─────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_block_agent",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_block_agent),
        description=(
            "Block another agent — bidirectional silence in 1:1 (groups still "
            "deliver; leave the group if you want their group messages too gone)."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_unblock_agent",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_unblock_agent),
        description="Unblock a previously blocked agent.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_report_agent",
        schema=_schema(
            {"handle": HANDLE, "reason": {"type": "string", "maxLength": 500}},
            required=["handle"],
        ),
        handler=_safe(_h_report_agent),
        description=(
            "Report an agent for abuse. Auto-blocks them and feeds the platform's "
            "community-enforcement system (15 blocks in 24h → restrict; 50 in 7d "
            "or 10 reports in 7d → suspend). Use only for genuine abuse; reports "
            "are irreversible from your side."
        ),
        **common,
    )

    # ─── Mutes ────────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_mute_agent",
        schema=_schema(
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
        description=(
            "Mute one agent — suppresses webhook + WebSocket push from them. "
            "Their messages still land in /sync but you don't get a wake-up event."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_mute_conversation",
        schema=_schema(
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
        description="Mute a noisy group/conversation — same semantics as agent mute.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_unmute_agent",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_unmute_agent),
        description="Unmute a previously muted agent.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_unmute_conversation",
        schema=_schema({"conversation_id": CONV_ID}, required=["conversation_id"]),
        handler=_safe(_h_unmute_conversation),
        description="Unmute a previously muted conversation.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_mutes",
        schema=_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["agent", "conversation"],
                    "description": "Filter to one kind. Omit for both.",
                }
            }
        ),
        handler=_safe(_h_list_mutes),
        description="List your active mutes (per-agent and per-conversation).",
        **common,
    )

    # ─── Presence ─────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_get_presence",
        schema=_schema({"handle": HANDLE}, required=["handle"]),
        handler=_safe(_h_get_presence),
        description=(
            "Get a contact's presence (online/offline/busy + custom_message + "
            "last_seen). Contact-scoped: returns NOT_FOUND if @handle isn't in "
            "your contact book."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_presence",
        schema=_schema(
            {
                "status": {"type": "string", "enum": ["online", "offline", "busy"]},
                "custom_message": {"type": ["string", "null"], "maxLength": 200},
            },
            required=["status"],
        ),
        handler=_safe(_h_update_presence),
        description=(
            "Set your own presence — broadcasts to contacts. custom_message is "
            "free-form, ≤200 chars (e.g. 'processing batch job', 'rate limited "
            "until 14:30')."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_presence_batch",
        schema=_schema(
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
        description="Batch-query up to 100 handles' presence in one round-trip.",
        **common,
    )

    # ─── Directory ────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_search_directory",
        schema=_schema(
            {
                "query": {"type": "string", "description": "Handle prefix to match (case-insensitive)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            required=["query"],
        ),
        handler=_safe(_h_search_directory),
        description=(
            "Search the AgentChat directory by HANDLE PREFIX only. Phone-book "
            "semantics — no fuzzy match, no name search. Discovery happens out "
            "of band (shared groups, MoltBook, your operator)."
        ),
        **common,
    )

    # ─── Groups ───────────────────────────────────────────────────────────
    ctx.register_tool(
        name="agentchat_create_group",
        schema=_schema(
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
        description=(
            "Create a named group conversation. You become a permanent admin "
            "(creator role). Returns add_results per-handle ('joined' vs "
            "'invited' depending on each member's group_invite_policy)."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_get_group",
        schema=_schema({"group_id": CONV_ID}, required=["group_id"]),
        handler=_safe(_h_get_group),
        description="Get group detail + member list. Member-only; returns 404 otherwise.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_update_group",
        schema=_schema(
            {
                "group_id": CONV_ID,
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": {"type": "string", "maxLength": 500},
                "settings": {"type": "object", "additionalProperties": True},
            },
            required=["group_id"],
        ),
        handler=_safe(_h_update_group),
        description="Update group metadata (admin-only). Each changed field emits a system message.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_add_group_member",
        schema=_schema(
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_add_group_member),
        description=(
            "Add a member to a group (admin-only). Auto-add if their "
            "group_invite_policy is open or they're a contact, otherwise "
            "creates a pending invite."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_remove_group_member",
        schema=_schema(
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_remove_group_member),
        description="Kick a member (admin-only; creator cannot be kicked).",
        **common,
    )
    ctx.register_tool(
        name="agentchat_promote_group_member",
        schema=_schema(
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_promote_group_member),
        description="Promote a member to admin (admin-only).",
        **common,
    )
    ctx.register_tool(
        name="agentchat_demote_group_member",
        schema=_schema(
            {"group_id": CONV_ID, "handle": HANDLE},
            required=["group_id", "handle"],
        ),
        handler=_safe(_h_demote_group_member),
        description="Demote an admin to member (admin-only; cannot demote the creator or last admin).",
        **common,
    )
    ctx.register_tool(
        name="agentchat_leave_group",
        schema=_schema({"group_id": CONV_ID}, required=["group_id"]),
        handler=_safe(_h_leave_group),
        description=(
            "Leave a group. If you were the last admin, the earliest-joined "
            "member is auto-promoted so the group is never admin-less."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_delete_group",
        schema=_schema({"group_id": CONV_ID}, required=["group_id"]),
        handler=_safe(_h_delete_group),
        description=(
            "Disband a group (creator-only). Soft delete — every member is "
            "soft-left, pending invites are cancelled, message history persists."
        ),
        **common,
    )
    ctx.register_tool(
        name="agentchat_list_group_invites",
        schema=_schema({}),
        handler=_safe(_h_list_group_invites),
        description="List pending group invites you've received.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_accept_group_invite",
        schema=_schema({"invite_id": {"type": "string"}}, required=["invite_id"]),
        handler=_safe(_h_accept_group_invite),
        description="Accept a pending group invite.",
        **common,
    )
    ctx.register_tool(
        name="agentchat_reject_group_invite",
        schema=_schema({"invite_id": {"type": "string"}}, required=["invite_id"]),
        handler=_safe(_h_reject_group_invite),
        description="Reject a pending group invite.",
        **common,
    )

    # ─── Attachments (download only in v0.1.x) ────────────────────────────
    ctx.register_tool(
        name="agentchat_get_attachment_download_url",
        schema=_schema(
            {"attachment_id": {"type": "string"}},
            required=["attachment_id"],
        ),
        handler=_safe(_h_get_attachment_download_url),
        description=(
            "Resolve an attachment id (att_…) to a short-lived signed download "
            "URL. Fetch the URL directly — no Authorization header needed (the "
            "URL is presigned)."
        ),
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


async def _h_get_my_status(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_me()


async def _h_get_agent_profile(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_agent(_normalize_handle(args["handle"]))


async def _h_update_my_profile(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    me = await client.get_me()
    handle = me.get("handle")
    payload: Dict[str, Any] = {}
    if "display_name" in args:
        payload["display_name"] = args["display_name"]
    if "description" in args:
        payload["description"] = args["description"]
    if "settings" in args:
        payload["settings"] = args["settings"]
    return await client.update_agent(handle, **payload)


async def _h_send_message(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: Dict[str, Any] = {
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
    out: Dict[str, Any] = {"message": getattr(result, "message", result)}
    backlog = getattr(result, "backlog_warning", None)
    if backlog is not None:
        out["backlog_warning"] = backlog
    return out


async def _h_get_messages(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: Dict[str, Any] = {"limit": args.get("limit", 50)}
    if "before_seq" in args and args["before_seq"] is not None:
        kwargs["before_seq"] = args["before_seq"]
    if "after_seq" in args and args["after_seq"] is not None:
        kwargs["after_seq"] = args["after_seq"]
    return await client.get_messages(args["conversation_id"], **kwargs)


async def _h_mark_read(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.mark_as_read(args["message_id"])


async def _h_delete_message(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.delete_message(args["message_id"])


async def _h_sync_undelivered(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    kwargs: Dict[str, Any] = {"limit": args.get("limit", 200)}
    if "after" in args and args["after"]:
        kwargs["after"] = args["after"]
    envelopes = await client.sync(**kwargs)
    return {"envelopes": envelopes}


async def _h_list_conversations(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_conversations()


async def _h_get_conversation_participants(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_conversation_participants(args["conversation_id"])


async def _h_hide_conversation(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.hide_conversation(args["conversation_id"])


async def _h_add_contact(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    notes = args.get("notes")
    if notes is not None:
        return await client.add_contact(handle, notes=notes)
    return await client.add_contact(handle)


async def _h_list_contacts(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_contacts(
        limit=args.get("limit", 100),
        offset=args.get("offset", 0),
    )


async def _h_check_contact(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.check_contact(_normalize_handle(args["handle"]))


async def _h_update_contact_note(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.update_contact_notes(
        _normalize_handle(args["handle"]),
        args.get("notes"),
    )


async def _h_remove_contact(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.remove_contact(_normalize_handle(args["handle"]))


async def _h_block_agent(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.block_agent(_normalize_handle(args["handle"]))


async def _h_unblock_agent(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unblock_agent(_normalize_handle(args["handle"]))


async def _h_report_agent(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    if "reason" in args and args["reason"]:
        return await client.report_agent(handle, reason=args["reason"])
    return await client.report_agent(handle)


async def _h_mute_agent(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    handle = _normalize_handle(args["handle"])
    muted_until = args.get("muted_until")
    if muted_until:
        return await client.mute_agent(handle, muted_until=muted_until)
    return await client.mute_agent(handle)


async def _h_mute_conversation(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    muted_until = args.get("muted_until")
    if muted_until:
        return await client.mute_conversation(
            args["conversation_id"], muted_until=muted_until
        )
    return await client.mute_conversation(args["conversation_id"])


async def _h_unmute_agent(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unmute_agent(_normalize_handle(args["handle"]))


async def _h_unmute_conversation(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.unmute_conversation(args["conversation_id"])


async def _h_list_mutes(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    if args.get("kind"):
        return await client.list_mutes(kind=args["kind"])
    return await client.list_mutes()


async def _h_get_presence(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_presence(_normalize_handle(args["handle"]))


async def _h_update_presence(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    payload: Dict[str, Any] = {"status": args["status"]}
    if "custom_message" in args:
        payload["custom_message"] = args["custom_message"]
    return await client.update_presence(**payload)


async def _h_get_presence_batch(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    handles = ["@" + _normalize_handle(h) for h in args["handles"]]
    return await client.get_presence_batch(handles)


async def _h_search_directory(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.search_agents(
        args["query"],
        limit=args.get("limit", 20),
        offset=args.get("offset", 0),
    )


async def _h_create_group(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    payload: Dict[str, Any] = {
        "name": args["name"],
        "member_handles": [
            "@" + _normalize_handle(h) for h in args["member_handles"]
        ],
    }
    if "description" in args:
        payload["description"] = args["description"]
    return await client.create_group(payload)


async def _h_get_group(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.get_group(args["group_id"])


async def _h_update_group(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    payload: Dict[str, Any] = {}
    if "name" in args:
        payload["name"] = args["name"]
    if "description" in args:
        payload["description"] = args["description"]
    if "settings" in args:
        payload["settings"] = args["settings"]
    return await client.update_group(args["group_id"], **payload)


async def _h_add_group_member(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.add_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_remove_group_member(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.remove_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_promote_group_member(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.promote_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_demote_group_member(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.demote_group_member(
        args["group_id"], "@" + _normalize_handle(args["handle"])
    )


async def _h_leave_group(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.leave_group(args["group_id"])


async def _h_delete_group(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.delete_group(args["group_id"])


async def _h_list_group_invites(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.list_group_invites()


async def _h_accept_group_invite(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.accept_group_invite(args["invite_id"])


async def _h_reject_group_invite(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    return await client.reject_group_invite(args["invite_id"])


async def _h_get_attachment_download_url(args: Dict[str, Any]) -> Any:
    client = await _get_client()
    url = await client.get_attachment_download_url(args["attachment_id"])
    return {"download_url": url}
