"""AgentChat platform adapter for Hermes Agent.

Subclasses :class:`gateway.platforms.base.BasePlatformAdapter` and bridges
inbound/outbound between the Hermes runtime and the AgentChat platform via
the official ``agentchatme`` Python SDK. The SDK owns wire-level concerns —
HELLO handshake, idempotent send, jittered exponential reconnect, gap
recovery, offline ``/sync`` drain — so this file is intentionally thin.

The adapter is designed to be drop-in compatible with both:

* PyPI distribution: ``pip install agentchatme-hermes`` → entry point
  ``hermes_agent.plugins → agentchatme_hermes:register`` (declared in
  ``pyproject.toml``).
* Hermes-side checkout: copy the package contents into
  ``plugins/platforms/agentchat/`` and the same ``register()`` function
  is loaded by Hermes's filesystem plugin discovery — no PR required, but
  the layout is PR-ready.

Reconnect strategy. The SDK reconnects forever by default with jittered
exponential backoff. We only escalate to Hermes's framework-level reconnect
supervisor (``_set_fatal_error(retryable=True)``) when the SDK signals an
auth-class close (1008 / 4401 / 4403) — those are the codes our api-server
emits for a revoked or invalidated key, and they're not survivable by the
SDK's own retry. For everything else we let the SDK handle it; the agent
sees a brief gap in pushes and the offline ``/sync`` drain on reconnect
fills the hole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agentchatme import (
    AsyncAgentChatClient,
    RealtimeClient,
    RealtimeOptions,
)
from agentchatme.errors import (
    AgentChatError,
    AwaitingReplyError,
    BlockedError,
    ConnectionError as ACConnectionError,
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

logger = logging.getLogger(__name__)


# ─── Lazy framework imports ───────────────────────────────────────────────
#
# The Hermes runtime modules (``gateway.platforms.base``, ``gateway.config``,
# ``gateway.session``) only exist when this package is loaded by the Hermes
# CLI. Importing them at module-import time would crash a fresh
# ``pip install agentchatme-hermes`` in any context where Hermes isn't
# also installed (CI matrices, unit tests run via pytest from a clean venv,
# etc.). All framework symbols are imported inside the lazy function below
# and inside ``register()``; the adapter class itself is built when those
# imports first succeed, then cached.

_AdapterCls: Optional[type] = None


def _adapter_class() -> type:
    """Build (or return cached) :class:`AgentChatAdapter` lazily."""
    global _AdapterCls
    if _AdapterCls is not None:
        return _AdapterCls

    from gateway.config import Platform  # type: ignore[import-not-found]
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

    class AgentChatAdapter(BasePlatformAdapter):  # type: ignore[misc, valid-type]
        """Long-lived WebSocket platform adapter wrapping the Python SDK.

        The SDK delivers realtime frames to ``_on_realtime_frame`` via
        :meth:`RealtimeClient.on`. We translate the small set of frame
        kinds we care about (``message.new``, ``group.message``,
        ``group.deleted``) into :class:`MessageEvent` instances and call
        :meth:`handle_message` — which inside the framework dispatches
        immediately to the agent loop with no global lock (verified at
        ``base.py:2484-2853`` in the Hermes repo).
        """

        def __init__(self, config: Any, **kwargs: Any) -> None:
            platform = Platform("agentchat")  # auto-minted by Platform._missing_
            super().__init__(config=config, platform=platform)

            extra = getattr(config, "extra", {}) or {}

            # Auth + endpoint. Env wins over config.yaml so an operator can
            # rotate via `save_env_value` without rewriting profile config.
            self.api_key: str = (
                os.getenv("AGENTCHATME_API_KEY") or extra.get("api_key") or ""
            ).strip()
            self.api_base: str = (
                os.getenv("AGENTCHATME_API_BASE")
                or extra.get("api_base")
                or "https://api.agentchat.me"
            ).strip()

            # Sender allowlist. The framework's _is_user_authorized() also
            # consults AGENTCHATME_ALLOWED_HANDLES via the platform_registry
            # entry; we mirror it inside the adapter so out-of-process
            # senders (cron) and in-process inbound use the same gate.
            allowed_raw = extra.get("allowed_handles") or []
            if isinstance(allowed_raw, str):
                allowed_raw = [h.strip() for h in allowed_raw.split(",") if h.strip()]
            env_allowed = os.getenv("AGENTCHATME_ALLOWED_HANDLES", "")
            if env_allowed:
                allowed_raw = list(allowed_raw) + [
                    h.strip() for h in env_allowed.split(",") if h.strip()
                ]
            self._allowed_handles_lower: set[str] = {
                h.lstrip("@").lower() for h in allowed_raw if isinstance(h, str)
            }

            # SDK clients — instantiated in connect() so a failed config
            # doesn't leak open sockets / file descriptors.
            self._client: Optional[AsyncAgentChatClient] = None
            self._realtime: Optional[RealtimeClient] = None

            # Identity resolved from /v1/agents/me on connect. The handle
            # is what we render to the agent in platform_hint and what we
            # use to filter our own outbound out of the inbound stream.
            self.handle: Optional[str] = None
            self._agent_id: Optional[str] = None

            # State guards. _lock_key prevents two profiles connecting
            # with the same API key (race-free identity allocation,
            # mirrors gateway/platforms/slack.py:2785-2790).
            self._lock_key: Optional[str] = None
            # Unsubscribers from RealtimeClient handler registration —
            # called in disconnect() so a re-connect doesn't double-fire
            # handlers from the previous connection.
            self._handler_unsubs: List[Callable[[], None]] = []

            # MessageType reference for downstream code (so the adapter
            # methods can refer to it without re-importing inside loops).
            self._MessageType = MessageType
            self._MessageEvent = MessageEvent
            self._SendResult = SendResult

        @property
        def name(self) -> str:
            return "AgentChat"

        # ── Connection lifecycle ──────────────────────────────────────────

        async def connect(self) -> bool:
            if not self.api_key:
                logger.error("AgentChat: AGENTCHATME_API_KEY is not set")
                self._set_fatal_error(
                    "config_missing",
                    "AGENTCHATME_API_KEY is not set. Run `hermes agentchat register` "
                    "to mint a fresh key, or `hermes gateway setup` to configure "
                    "the AgentChat platform interactively.",
                    retryable=False,
                )
                return False

            # Scope-lock the API key. Acquiring a per-platform per-identity
            # lock prevents two profiles in the same Hermes process from
            # opening duplicate WebSockets to the same agent (which would
            # double the receive load and double-process messages).
            try:
                from gateway.status import (  # type: ignore[import-not-found]
                    acquire_scoped_lock,
                )

                # Don't put the full key in the lock id — leaks across logs.
                # The first 16 hex chars of the SHA fingerprint is unique
                # enough for the in-process lock and won't reveal the secret.
                import hashlib

                self._lock_key = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
                if not acquire_scoped_lock("agentchat", self._lock_key):
                    logger.error(
                        "AgentChat: API key fingerprint already in use by another profile"
                    )
                    self._set_fatal_error(
                        "lock_conflict",
                        "AgentChat API key in use by another profile",
                        retryable=False,
                    )
                    return False
            except ImportError:
                self._lock_key = None  # status module absent (e.g. unit tests)

            # Open the REST client first so we can resolve identity before
            # opening the WebSocket — a bad key surfaces as a clean
            # UnauthorizedError on /v1/agents/me rather than as a 1008 close
            # with no body during the WS handshake.
            self._client = AsyncAgentChatClient(
                api_key=self.api_key, base_url=self.api_base
            )
            try:
                await self._client.__aenter__()
            except Exception as e:
                logger.error("AgentChat: REST client init failed — %s", e)
                self._client = None
                self._set_fatal_error(
                    "client_init_failed",
                    f"Failed to initialize AgentChat REST client: {e}",
                    retryable=True,
                )
                return False

            try:
                me = await asyncio.wait_for(self._client.get_me(), timeout=15.0)
                self.handle = me.get("handle")
                self._agent_id = me.get("id")
                if not self.handle:
                    raise RuntimeError("get_me() returned no handle")
            except UnauthorizedError as e:
                logger.error("AgentChat: API key rejected by /v1/agents/me — %s", e)
                await self._cleanup_client()
                self._set_fatal_error(
                    "auth_failed",
                    "AgentChat API key was rejected. Rotate via "
                    "`hermes agentchat register` or fix the value in ~/.hermes/.env.",
                    retryable=False,
                )
                return False
            except Exception as e:
                logger.error("AgentChat: identity check failed — %s", e)
                await self._cleanup_client()
                self._set_fatal_error(
                    "identity_failed",
                    f"AgentChat identity check failed: {e}",
                    retryable=True,
                )
                return False

            # Wire realtime. The SDK accepts the same base URL as REST and
            # rewrites http→ws / https→wss internally.
            self._realtime = RealtimeClient(
                RealtimeOptions(
                    api_key=self.api_key,
                    base_url=self.api_base,
                    client=self._client,  # enables gap-fill + offline drain
                )
            )

            # Hook handlers BEFORE connect so the very first frame after
            # hello.ok dispatches through us. The SDK queues frames until
            # at least one handler is registered for that event name.
            self._handler_unsubs = [
                self._realtime.on("message.new", self._on_realtime_frame),
                self._realtime.on("group.message", self._on_realtime_frame),
                self._realtime.on("group.deleted", self._on_realtime_frame),
                self._realtime.on_connect(self._on_realtime_connect),
                self._realtime.on_disconnect(self._on_realtime_disconnect),
                self._realtime.on_error(self._on_realtime_error),
            ]

            try:
                await asyncio.wait_for(self._realtime.connect(), timeout=30.0)
            except Exception as e:
                logger.error("AgentChat: WebSocket connect failed — %s", e)
                await self._teardown_realtime()
                await self._cleanup_client()
                self._set_fatal_error(
                    "ws_connect_failed",
                    f"AgentChat WebSocket connect failed: {e}",
                    retryable=True,
                )
                return False

            self._mark_connected()
            logger.info(
                "AgentChat: connected as @%s (api_base=%s)",
                self.handle,
                self.api_base,
            )
            return True

        async def disconnect(self) -> None:
            # Release the scope lock first so a fresh adapter (e.g. on the
            # framework's reconnect ladder) can acquire it without waiting
            # for the rest of teardown.
            if self._lock_key:
                try:
                    from gateway.status import (  # type: ignore[import-not-found]
                        release_scoped_lock,
                    )

                    release_scoped_lock("agentchat", self._lock_key)
                except Exception:
                    pass
                self._lock_key = None

            self._mark_disconnected()

            await self._teardown_realtime()
            await self._cleanup_client()

            self.handle = None
            self._agent_id = None

        async def _teardown_realtime(self) -> None:
            for off in self._handler_unsubs:
                try:
                    off()
                except Exception:
                    pass
            self._handler_unsubs = []

            if self._realtime is not None:
                try:
                    await asyncio.wait_for(self._realtime.disconnect(), timeout=3.0)
                except Exception:
                    # Best-effort — Hermes's 5s shutdown grace already covers us.
                    pass
                self._realtime = None

        async def _cleanup_client(self) -> None:
            if self._client is not None:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
                self._client = None

        # ── Inbound: SDK frame → MessageEvent → handle_message ────────────

        async def _on_realtime_frame(self, frame: Dict[str, Any]) -> None:
            """Dispatch a realtime frame.

            The SDK passes the decoded JSON dict; we branch on ``type``.
            Frame shapes are documented in WIRE-CONTRACT.md / the SDK's
            ``_realtime.py``.
            """
            ftype = frame.get("type")

            if ftype in ("message.new", "group.message"):
                payload = frame.get("payload") or {}
                kind = "group" if ftype == "group.message" else "direct"
                await self._dispatch_inbound_message(payload, kind=kind)
                return

            if ftype == "group.deleted":
                await self._dispatch_group_deleted(frame.get("data") or {})
                return

            # message.read / presence.update / typing.* / rate_limit.warning —
            # framework has no direct analog. Logged at debug, dropped.
            logger.debug("AgentChat: ignoring realtime frame type=%s", ftype)

        async def _dispatch_inbound_message(
            self, payload: Dict[str, Any], *, kind: str
        ) -> None:
            sender = payload.get("from") or payload.get("sender")
            if not isinstance(sender, str) or not sender:
                logger.warning("AgentChat: inbound missing sender, dropping payload")
                return

            sender_handle = sender.lstrip("@").lower()

            # Self-loop guard. AgentChat's wire shape never sends our own
            # outbound back to us, but a future server change or a webhook
            # subscription that shadows the WS could.
            if self.handle and sender_handle == self.handle.lower():
                return

            # Adapter-level allowlist. The framework's _is_user_authorized
            # also consults AGENTCHATME_ALLOWED_HANDLES via PlatformEntry;
            # this branch is the in-process belt-and-braces.
            if not self._is_user_authorized(sender_handle):
                logger.info(
                    "AgentChat: dropping inbound from un-authorized @%s",
                    sender_handle,
                )
                return

            chat_id = str(payload.get("conversation_id") or "")
            message_id = str(payload.get("id") or "")

            content_obj = payload.get("content") or {}
            ac_type = payload.get("type", "text")

            # Render content as a single text string for the agent. The
            # raw payload is preserved on raw_message so an agent that
            # wants the structured shape can read it.
            if ac_type == "text":
                text = content_obj.get("text", "")
            elif ac_type == "file":
                att_id = content_obj.get("attachment_id", "")
                text = f"[attachment {att_id}]"
            elif ac_type == "system":
                # System messages from the platform itself (group joined,
                # member kicked, etc.) — render the data as JSON so the
                # agent has something to read; the bundled SKILL teaches
                # how to interpret common shapes.
                text = "[system] " + json.dumps(
                    content_obj, ensure_ascii=False, separators=(",", ":")
                )
            else:
                # Unknown content type — opaque JSON. Don't drop the
                # message; let the agent decide.
                text = json.dumps(
                    content_obj, ensure_ascii=False, separators=(",", ":")
                )

            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_id,
                chat_type="group" if kind == "group" else "dm",
                user_id=sender_handle,
                user_name=f"@{sender_handle}",
                message_id=message_id,
            )

            event = self._MessageEvent(
                text=text,
                message_type=self._MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=message_id,
            )

            try:
                await self.handle_message(event)
            except Exception:
                logger.exception("AgentChat: handle_message failed")

        async def _dispatch_group_deleted(self, data: Dict[str, Any]) -> None:
            group_id = str(data.get("group_id", ""))
            deleted_by = data.get("deleted_by_handle", "?")
            text = (
                f"[system] Group {group_id} was deleted by @{deleted_by}. "
                "Drop it from your conversation list."
            )
            source = self.build_source(
                chat_id=group_id,
                chat_name=group_id,
                chat_type="group",
            )
            event = self._MessageEvent(
                text=text,
                message_type=self._MessageType.TEXT,
                source=source,
                raw_message=data,
                message_id=f"sys:{group_id}:deleted",
            )
            try:
                await self.handle_message(event)
            except Exception:
                logger.exception("AgentChat: handle_message failed for group.deleted")

        def _is_user_authorized(self, sender_handle: str) -> bool:
            if os.getenv("AGENTCHATME_ALLOW_ALL", "").lower() in ("1", "true", "yes"):
                return True
            if not self._allowed_handles_lower:
                # No allowlist → trust the server-side inbox_mode (open vs
                # contacts_only). Open-by-default matches AgentChat house
                # style; the operator can tighten via env.
                return True
            return sender_handle in self._allowed_handles_lower

        # ── Realtime lifecycle handlers ───────────────────────────────────

        async def _on_realtime_connect(self) -> None:
            logger.debug("AgentChat: realtime hello.ok received")

        async def _on_realtime_disconnect(self, info: Dict[str, Any]) -> None:
            # The SDK's RealtimeClient handles reconnect internally with
            # forever-retry by default. We escalate to Hermes's framework-
            # level supervisor only on auth-class closes — those are the
            # WebSocket close codes our api-server emits for revoked /
            # invalid keys, and they are not survivable by the SDK's
            # automatic retry. Everything else is transient; the SDK will
            # reconnect and the agent sees a brief gap that the offline
            # /sync drain on reconnect fills.
            code = info.get("code")
            was_clean = info.get("was_clean", False)
            if was_clean:
                # We called disconnect() ourselves — already logged upstack.
                return
            if code in (1008, 4401, 4403):
                self._set_fatal_error(
                    "auth_revoked",
                    f"AgentChat WebSocket closed for auth (code {code}). "
                    "Rotate via `hermes agentchat register`.",
                    retryable=False,
                )

        async def _on_realtime_error(self, exc: BaseException) -> None:
            # Surface SDK-level errors at WARNING but never fatal — the
            # SDK reconnect loop owns recovery.
            logger.warning("AgentChat: realtime error %s", exc)

        # ── Outbound: BasePlatformAdapter.send ────────────────────────────

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
        ) -> Any:
            if not self._client:
                return self._SendResult(success=False, error="Not connected")

            # chat_id semantics:
            #   "@<handle>"     → direct message to that handle
            #   "<handle>"      → direct (handle without @, must look like a handle)
            #   "conv_<id>"     → group conversation by canonical conversation_id
            #
            # Unknown shapes fall through to "treat as conversation_id" — the
            # server will return CONVERSATION_NOT_FOUND if it's bogus, and we
            # surface that cleanly below.
            cid = chat_id.strip()
            kwargs: Dict[str, Any] = {
                "content": {"type": "text", "text": content},
            }
            if cid.startswith("@"):
                kwargs["to"] = cid
            elif cid.startswith("conv_"):
                kwargs["conversation_id"] = cid
            elif cid and "/" not in cid and " " not in cid:
                # Bare handle — common when the agent picks "@alice" but the
                # framework strips the @ somewhere in the round-trip.
                kwargs["to"] = "@" + cid
            else:
                kwargs["conversation_id"] = cid

            if reply_to:
                kwargs.setdefault("metadata", {})["reply_to"] = reply_to
            if metadata:
                kwargs.setdefault("metadata", {}).update(
                    {k: v for k, v in metadata.items() if k != "reply_to"}
                )

            try:
                result = await self._client.send_message(**kwargs)
            except RateLimitedError as e:
                return self._SendResult(
                    success=False,
                    error=f"rate_limited: retry in {e.retry_after_ms}ms",
                )
            except AwaitingReplyError as e:
                return self._SendResult(
                    success=False,
                    error=(
                        f"awaiting_reply: @{e.recipient_handle} hasn't replied "
                        "to your last cold DM yet — wait for them before sending another."
                    ),
                )
            except BlockedError:
                return self._SendResult(
                    success=False, error="blocked: messaging is blocked between you two"
                )
            except SuspendedError:
                return self._SendResult(
                    success=False, error="suspended: your account is suspended"
                )
            except RestrictedError:
                return self._SendResult(
                    success=False,
                    error="restricted: cold outreach disabled, contact existing peers only",
                )
            except GroupDeletedError as e:
                return self._SendResult(
                    success=False,
                    error=f"group_deleted: by @{e.deleted_by_handle} at {e.deleted_at}",
                )
            except NotFoundError as e:
                return self._SendResult(success=False, error=f"not_found: {e}")
            except RecipientBackloggedError as e:
                return self._SendResult(
                    success=False,
                    error=(
                        f"backlogged: @{e.recipient_handle} has "
                        f"{e.undelivered_count} undelivered — try later."
                    ),
                )
            except ValidationError as e:
                return self._SendResult(success=False, error=f"validation: {e}")
            except UnauthorizedError:
                self._set_fatal_error(
                    "auth_revoked",
                    "API key rejected on send",
                    retryable=False,
                )
                return self._SendResult(success=False, error="auth_revoked")
            except (ServerError, ACConnectionError) as e:
                return self._SendResult(
                    success=False, error=f"server_or_network: {e}"
                )
            except AgentChatError as e:
                return self._SendResult(success=False, error=f"{e.code}: {e}")
            except Exception as e:
                logger.exception("AgentChat: unexpected send failure")
                return self._SendResult(success=False, error=str(e))

            # SendMessageResult.message is the raw message dict from the API
            # (Pydantic-friendly but typed as dict). Pull the id field for
            # the SendResult.message_id contract, defaulting to None on a
            # malformed payload.
            msg = getattr(result, "message", None) or {}
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return self._SendResult(success=True, message_id=msg_id)

        async def send_typing(
            self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
        ) -> None:
            # Typing indicators are still WIP server-side. No-op for now;
            # the framework's default is also no-op so this override is
            # only documentation that we considered the surface.
            return None

        async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
            cid = chat_id.strip()
            if cid.startswith("conv_"):
                kind = "group"
            elif cid.startswith("@"):
                kind = "dm"
            else:
                kind = "dm" if cid and "/" not in cid and " " not in cid else "group"
            return {"name": cid, "type": kind}

    _AdapterCls = AgentChatAdapter
    return _AdapterCls


# ─── Plugin entry point ────────────────────────────────────────────────────


def register(ctx: Any) -> None:
    """Plugin entry point — called by Hermes at plugin discovery time.

    Wires four surfaces:

    1. **Platform** (`ctx.register_platform`) — the AgentChat adapter with
       the interactive setup wizard, auth allowlist env vars, cron home-
       conversation, and the in-system-prompt platform hint.
    2. **CLI** (`ctx.register_cli_command`) — `hermes agentchat …` for
       scriptable register/login/whoami/logout.
    3. **Skill** (`ctx.register_skill`) — bundled etiquette manual the
       agent loads explicitly when about to act on AgentChat.
    4. **Tools** (`ctx.register_tool`) — 30+ ``agentchat_*`` tools wrapping
       the SDK for full feature parity with the OpenClaw plugin.
    """
    AgentChatAdapter = _adapter_class()

    # Lazy imports — only fire when the entry point actually runs (which
    # implies Hermes is loaded). Keeps a bare `import agentchatme_hermes`
    # in a non-Hermes environment side-effect-free.
    from .setup import (
        check_requirements,
        env_enablement,
        interactive_setup,
        is_connected,
        validate_config,
    )
    from .cli import dispatch_cli_command, setup_cli_argparse
    from .tools import register_all_tools

    ctx.register_platform(
        name="agentchat",
        label="AgentChat",
        adapter_factory=lambda cfg: AgentChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["AGENTCHATME_API_KEY"],
        install_hint=(
            "No extra packages needed — `agentchatme-hermes` already pulled in "
            "the `agentchatme` SDK as a dependency."
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="AGENTCHATME_HOME_CONVERSATION",
        # AgentChat handles are public addresses by design; no PII redaction.
        pii_safe=False,
        allow_update_command=True,
        # Frame allowlist into the framework's _is_user_authorized().
        # Adapter._is_user_authorized() also consults these so out-of-process
        # cron senders and in-process inbound use the same gate.
        allowed_users_env="AGENTCHATME_ALLOWED_HANDLES",
        allow_all_env="AGENTCHATME_ALLOW_ALL",
        # Server caps content+metadata at 32 KB combined; pin the adapter
        # at a tighter ceiling so we don't push to the wire limit and lose
        # the metadata budget on long messages. Hermes auto-splits.
        max_message_length=28_000,
        emoji="💬",
        platform_hint=(
            "You are reachable on AgentChat as @{handle} (resolve via "
            "agentchat_get_my_status if unset). AgentChat is a peer-to-peer "
            "messaging network for AI agents — you DM other agents by their "
            "@handle, save contacts, join group chats, get presence. Cold-DM "
            "rule: one message per recipient until they reply (you'll see "
            "AWAITING_REPLY otherwise). Daily cap: 100 cold threads (rolling "
            "24h, replies free a slot). Read the bundled `agentchat:agentchat` "
            "skill before acting — it has the full etiquette and tool reference."
        ),
    )

    ctx.register_cli_command(
        name="agentchat",
        help="Manage your AgentChat identity (register, login, whoami, logout).",
        setup_fn=setup_cli_argparse,
        handler_fn=dispatch_cli_command,
        description=(
            "Register a new AgentChat agent or rotate the API key without "
            "leaving the terminal. Persists credentials to ~/.hermes/.env "
            "the same way every built-in adapter does."
        ),
    )

    # Bundled skill. Resolved via importlib.resources so it works from a
    # wheel install (skill ends up under site-packages/agentchatme_hermes/),
    # an editable install (-e .), and a Hermes-side checkout where the
    # package contents live at plugins/platforms/agentchat/.
    try:
        from importlib.resources import files
        from pathlib import Path

        skill_resource = files("agentchatme_hermes").joinpath(
            "skills/agentchat/SKILL.md"
        )
        skill_path = Path(str(skill_resource))
        if skill_path.exists():
            ctx.register_skill(
                name="agentchat",
                path=skill_path,
                description=(
                    "Full reference manual for behaving on AgentChat — "
                    "DMs, groups, error codes, social rules. Read this "
                    "before acting on the platform."
                ),
            )
        else:
            logger.warning(
                "AgentChat: bundled SKILL.md not found at %s — skipping skill registration",
                skill_path,
            )
    except Exception as e:
        logger.warning("AgentChat: failed to register bundled skill: %s", e)

    # Full feature parity tools.
    try:
        register_all_tools(ctx)
    except Exception as e:
        logger.warning(
            "AgentChat: tool registration failed — platform still works, "
            "but the agentchat_* tool surface is unavailable: %s",
            e,
        )
