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
import contextlib
import json
import logging
import os
import time
from typing import Any, Callable

from agentchatme import (
    AsyncAgentChatClient,
    RealtimeClient,
    RealtimeOptions,
)
from agentchatme.errors import (
    AgentChatError,
    AwaitingReplyError,
    BlockedError,
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
from agentchatme.errors import (
    ConnectionError as ACConnectionError,
)

from . import metrics as _metrics_mod

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

_AdapterCls: type | None = None


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
            # Defensive __init__ contract: NEVER raise. Stash any failure
            # in `self._init_error` and let `connect()` surface it via
            # the framework's `_set_fatal_error` machinery (retryable=False).
            # An exception bubbling out of the adapter factory short-
            # circuits gateway.runner setup for EVERY platform, not just
            # AgentChat — see plugin loading at
            # `gateway/runner.py:2185`. Discovered in the v0.1.62 audit.
            self._init_error: str | None = None

            # Always-present attributes so disconnect() / repr() / metrics
            # never NPE if we bail out mid-init. The pre-population mirrors
            # the order things are first read in the rest of the adapter.
            self.api_key: str = ""
            self.api_base: str = "https://api.agentchat.me"
            self._allowed_handles_lower: set[str] = set()
            self._client: AsyncAgentChatClient | None = None
            self._realtime: RealtimeClient | None = None
            self.handle: str | None = None
            self._agent_id: str | None = None
            self._lock_key: str | None = None
            self._handler_unsubs: list[Callable[[], None]] = []
            self._MessageType = MessageType
            self._MessageEvent = MessageEvent
            self._SendResult = SendResult

            try:
                platform = Platform("agentchat")  # auto-minted by Platform._missing_
                super().__init__(config=config, platform=platform)

                extra = getattr(config, "extra", {}) or {}

                # Auth + endpoint. Env wins over config.yaml so an operator can
                # rotate via `save_env_value` without rewriting profile config.
                self.api_key = (
                    os.getenv("AGENTCHATME_API_KEY") or extra.get("api_key") or ""
                ).strip()
                self.api_base = (
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
                self._allowed_handles_lower = {
                    h.lstrip("@").lower() for h in allowed_raw if isinstance(h, str)
                }
            except Exception as e:
                # Anything during init failed — record it and bail. connect()
                # is the only consumer of `self._init_error`; it will fail-fast
                # there with retryable=False and a clear operator-facing
                # message rather than letting the framework see a raised
                # exception and possibly mark the entire platform broken.
                logger.exception("AgentChat: adapter __init__ failed")
                self._init_error = f"AgentChat adapter init failed: {e}"

        @property
        def name(self) -> str:
            return "AgentChat"

        # ── Connection lifecycle ──────────────────────────────────────────

        async def connect(self) -> bool:
            recorder = _metrics_mod.get_recorder()
            recorder.set_connection_state("connecting")

            # If __init__ recorded an error, surface it here as a clean
            # non-retryable fatal — the framework would otherwise keep
            # bouncing connect() with no useful signal. See defensive
            # __init__ contract above.
            if self._init_error:
                logger.error("AgentChat: aborting connect — %s", self._init_error)
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("init_error")
                self._set_fatal_error(
                    "init_error",
                    self._init_error,
                    retryable=False,
                )
                return False

            if not self.api_key:
                logger.error("AgentChat: AGENTCHATME_API_KEY is not set")
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("config_missing")
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
            #
            # Use the base class's `_acquire_platform_lock` wrapper
            # (`base.py:1408`) — it correctly unpacks the
            # `(bool, dict|None)` return shape, sets a `<scope>_lock`
            # fatal error on conflict with PID context, and pairs with
            # `_release_platform_lock()` for teardown. The previous
            # direct `acquire_scoped_lock(...)` call had a critical
            # bug: tuples are always truthy, so the `if not (...)`
            # never fired — silent double-connect was possible. Fixed
            # in v0.1.62 audit.
            import hashlib

            # Don't put the full key in the lock id — leaks across logs.
            # The first 16 hex chars of the SHA fingerprint is unique
            # enough for the in-process lock and won't reveal the secret.
            self._lock_key = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]

            try:
                acquired = self._acquire_platform_lock(
                    "agentchat", self._lock_key, "AgentChat API key"
                )
            except (ImportError, AttributeError):
                # Base-class lock helper unavailable (very old Hermes or
                # unit-test mocks) — proceed unlocked. The framework
                # already supervises adapter lifecycle at a higher
                # layer, so this is degraded but not broken.
                self._lock_key = None
                acquired = True

            if not acquired:
                # `_acquire_platform_lock` already called `_set_fatal_error`
                # with `agentchat_lock`. Mirror the metrics emission and
                # bail; teardown will be a no-op since we never opened
                # the SDK clients.
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("lock_conflict")
                self._lock_key = None
                return False

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
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("client_init_failed")
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
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("auth_failed")
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
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("identity_failed")
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
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("ws_connect_failed")
                self._set_fatal_error(
                    "ws_connect_failed",
                    f"AgentChat WebSocket connect failed: {e}",
                    retryable=True,
                )
                return False

            recorder.set_connection_state("ready")
            self._mark_connected()
            logger.info(
                "AgentChat: connected as @%s (api_base=%s)",
                self.handle,
                self.api_base,
            )
            return True

        async def disconnect(self) -> None:
            # Release the scope lock first so a fresh adapter (e.g. on
            # the framework's reconnect ladder) can acquire it without
            # waiting for the rest of teardown. The base-class helper
            # checks for the stored identity and is a no-op if no lock
            # was acquired.
            if self._lock_key:
                with contextlib.suppress(Exception):
                    self._release_platform_lock()
                self._lock_key = None

            self._mark_disconnected()
            _metrics_mod.get_recorder().set_connection_state("disconnected")

            await self._teardown_realtime()
            await self._cleanup_client()

            self.handle = None
            self._agent_id = None

        async def _teardown_realtime(self) -> None:
            for off in self._handler_unsubs:
                with contextlib.suppress(Exception):
                    off()
            self._handler_unsubs = []

            if self._realtime is not None:
                # Best-effort — Hermes's 5s shutdown grace already covers us.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._realtime.disconnect(), timeout=3.0)
                self._realtime = None

        async def _cleanup_client(self) -> None:
            if self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.__aexit__(None, None, None)
                self._client = None

        # ── Inbound: SDK frame → MessageEvent → handle_message ────────────

        async def _on_realtime_frame(self, frame: dict[str, Any]) -> None:
            """Dispatch a realtime frame.

            The SDK passes the decoded JSON dict; we branch on ``type``.
            Frame shapes are documented in WIRE-CONTRACT.md / the SDK's
            ``_realtime.py``.
            """
            ftype = frame.get("type")
            recorder = _metrics_mod.get_recorder()

            if ftype in ("message.new", "group.message"):
                payload = frame.get("payload") or {}
                kind = "group" if ftype == "group.message" else "direct"
                recorder.inc_inbound("group_message" if kind == "group" else "message_new")
                await self._dispatch_inbound_message(payload, kind=kind)
                return

            if ftype == "group.deleted":
                recorder.inc_inbound("group_deleted")
                await self._dispatch_group_deleted(frame.get("data") or {})
                return

            # message.read / presence.update / typing.* / rate_limit.warning —
            # framework has no direct analog. Logged at debug, dropped.
            recorder.inc_inbound("ignored")
            logger.debug("AgentChat: ignoring realtime frame type=%s", ftype)

        async def _dispatch_inbound_message(
            self, payload: dict[str, Any], *, kind: str
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

        async def _dispatch_group_deleted(self, data: dict[str, Any]) -> None:
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

        async def _on_realtime_disconnect(self, info: dict[str, Any]) -> None:
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
                recorder = _metrics_mod.get_recorder()
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("auth_revoked")
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
            reply_to: str | None = None,
            metadata: dict[str, Any] | None = None,
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
            kwargs: dict[str, Any] = {
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

            recorder = _metrics_mod.get_recorder()
            start = time.perf_counter()

            def _fail(code: str, sr: Any) -> Any:
                recorder.observe_send_latency(time.perf_counter() - start)
                recorder.inc_outbound_failed(code)
                return sr

            # ``retryable=True`` lets the framework's ``_send_with_retry``
            # (``base.py:2315``) absorb transient failures via backoff
            # instead of bouncing the error back to the agent on first
            # try. Reserve True for genuinely transient classes:
            #   * RateLimitedError — the per-second bucket drains
            #   * ServerError / ACConnectionError — network blip, 5xx
            #   * RecipientBackloggedError — peer drains as they sync
            # Everything else (auth, validation, blocked, awaiting-reply,
            # restricted, suspended, group-deleted, not-found) is a
            # decision-class error the agent must see and react to, not
            # a transient that retrying would fix.
            try:
                result = await self._client.send_message(**kwargs)
            except RateLimitedError as e:
                return _fail("RATE_LIMITED", self._SendResult(
                    success=False,
                    error=f"rate_limited: retry in {e.retry_after_ms}ms",
                    retryable=True,
                ))
            except AwaitingReplyError as e:
                return _fail("AWAITING_REPLY", self._SendResult(
                    success=False,
                    error=(
                        f"awaiting_reply: @{e.recipient_handle} hasn't replied "
                        "to your last cold DM yet — wait for them before sending another."
                    ),
                ))
            except BlockedError:
                return _fail("BLOCKED", self._SendResult(
                    success=False, error="blocked: messaging is blocked between you two"
                ))
            except SuspendedError:
                return _fail("SUSPENDED", self._SendResult(
                    success=False, error="suspended: your account is suspended"
                ))
            except RestrictedError:
                return _fail("RESTRICTED", self._SendResult(
                    success=False,
                    error="restricted: cold outreach disabled, contact existing peers only",
                ))
            except GroupDeletedError as e:
                return _fail("GROUP_DELETED", self._SendResult(
                    success=False,
                    error=f"group_deleted: by @{e.deleted_by_handle} at {e.deleted_at}",
                ))
            except NotFoundError as e:
                return _fail("NOT_FOUND", self._SendResult(success=False, error=f"not_found: {e}"))
            except RecipientBackloggedError as e:
                return _fail("RECIPIENT_BACKLOGGED", self._SendResult(
                    success=False,
                    error=(
                        f"backlogged: @{e.recipient_handle} has "
                        f"{e.undelivered_count} undelivered — try later."
                    ),
                    retryable=True,
                ))
            except ValidationError as e:
                return _fail("VALIDATION_ERROR", self._SendResult(success=False, error=f"validation: {e}"))
            except UnauthorizedError:
                recorder.set_connection_state("failed")
                recorder.inc_reconnect("auth_revoked")
                self._set_fatal_error(
                    "auth_revoked",
                    "API key rejected on send",
                    retryable=False,
                )
                return _fail("UNAUTHORIZED", self._SendResult(success=False, error="auth_revoked"))
            except (ServerError, ACConnectionError) as e:
                return _fail("SERVER_OR_NETWORK", self._SendResult(
                    success=False, error=f"server_or_network: {e}", retryable=True,
                ))
            except AgentChatError as e:
                code = getattr(e, "code", "AGENTCHAT_ERROR") or "AGENTCHAT_ERROR"
                return _fail(code, self._SendResult(success=False, error=f"{code}: {e}"))
            except Exception as e:
                logger.exception("AgentChat: unexpected send failure")
                return _fail("UNEXPECTED", self._SendResult(success=False, error=str(e)))

            # Success path. SendMessageResult.message is the raw message dict
            # from the API (Pydantic-friendly but typed as dict). Pull the id
            # field for the SendResult.message_id contract, defaulting to
            # None on a malformed payload.
            recorder.observe_send_latency(time.perf_counter() - start)
            recorder.inc_outbound_sent()
            msg = getattr(result, "message", None) or {}
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return self._SendResult(success=True, message_id=msg_id)

        async def send_typing(
            self, chat_id: str, metadata: dict[str, Any] | None = None
        ) -> None:
            # Typing indicators are still WIP server-side. No-op for now;
            # the framework's default is also no-op so this override is
            # only documentation that we considered the surface.
            return None

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
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


# ─── Out-of-process cron delivery ─────────────────────────────────────────
#
# Cron jobs configured with `deliver=agentchat` may run in a SEPARATE
# process from the long-lived `hermes gateway` (e.g. `hermes cron run` on
# its own systemd unit, or a CI matrix job firing a one-off). In that
# process the live adapter is not available, so
# `tools/send_message_tool._send_to_platform` falls through to the
# `standalone_sender_fn` registered on our `PlatformEntry`
# (`send_message_tool.py:478`). Without this hook, `deliver=agentchat`
# cron tasks fail with `No live adapter for platform 'agentchat'`.
#
# The hook opens a one-shot REST client, sends, and closes. The SDK
# routes via /v1/messages so no WebSocket is needed. Routing rules
# mirror `AgentChatAdapter.send` so the agent's outbound contract is
# identical whether running in-gateway or via cron.
#
# `thread_id` and `media_files` are accepted for signature parity with
# the framework's `_standalone_send` shape but not meaningful here:
# AgentChat threads are conversation-scoped (the `conv_*` chat_id IS
# the thread), and out-of-process media upload requires a holding
# session we don't have in this path.


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """One-shot cron-side delivery via the AgentChat REST SDK.

    Contract: returns ``{"success": True, "message_id": str|None}`` on a
    successful send, or ``{"error": str}`` on failure. The cron pipeline
    treats anything else as an invalid shape and surfaces a generic
    error to the operator (see ``tools/send_message_tool.py:494``).
    """
    extra = getattr(pconfig, "extra", {}) or {}
    api_key = (os.getenv("AGENTCHATME_API_KEY") or extra.get("api_key") or "").strip()
    api_base = (
        os.getenv("AGENTCHATME_API_BASE")
        or extra.get("api_base")
        or "https://api.agentchat.me"
    ).strip()

    if not api_key:
        return {"error": "AgentChat standalone send: AGENTCHATME_API_KEY is not set"}
    if not chat_id:
        return {"error": "AgentChat standalone send: chat_id is required"}

    cid = chat_id.strip()
    kwargs: dict[str, Any] = {
        "content": {"type": "text", "text": message or ""},
    }
    if cid.startswith("@"):
        kwargs["to"] = cid
    elif cid.startswith("conv_"):
        kwargs["conversation_id"] = cid
    elif cid and "/" not in cid and " " not in cid:
        kwargs["to"] = "@" + cid
    else:
        kwargs["conversation_id"] = cid

    # Media + thread are not supported via the cron-side path. Surface
    # the deferral so the recipient (and the operator reading the log)
    # know media wasn't delivered rather than silently dropping it.
    if media_files:
        kwargs["content"] = {
            "type": "text",
            "text": (
                (kwargs["content"]["text"] or "")
                + f"\n[{len(media_files)} attachment(s) generated; "
                "not deliverable from cron]"
            ),
        }

    client = AsyncAgentChatClient(api_key=api_key, base_url=api_base)
    try:
        await client.__aenter__()
    except Exception as e:
        return {"error": f"AgentChat standalone send: REST init failed: {e}"}

    try:
        try:
            result = await asyncio.wait_for(
                client.send_message(**kwargs), timeout=30.0
            )
        except asyncio.TimeoutError:
            return {"error": "AgentChat standalone send: timed out after 30s"}
        except UnauthorizedError:
            return {"error": "AgentChat standalone send: API key rejected"}
        except RateLimitedError as e:
            return {"error": f"AgentChat standalone send: rate_limited (retry in {e.retry_after_ms}ms)"}
        except AwaitingReplyError as e:
            return {
                "error": (
                    f"AgentChat standalone send: awaiting_reply "
                    f"(@{e.recipient_handle} hasn't replied yet)"
                )
            }
        except BlockedError:
            return {"error": "AgentChat standalone send: blocked between sender and recipient"}
        except SuspendedError:
            return {"error": "AgentChat standalone send: account suspended"}
        except RestrictedError:
            return {"error": "AgentChat standalone send: cold outreach restricted"}
        except GroupDeletedError as e:
            return {
                "error": (
                    f"AgentChat standalone send: group_deleted by "
                    f"@{e.deleted_by_handle}"
                )
            }
        except NotFoundError as e:
            return {"error": f"AgentChat standalone send: not_found: {e}"}
        except RecipientBackloggedError as e:
            return {
                "error": (
                    f"AgentChat standalone send: recipient_backlogged "
                    f"(@{e.recipient_handle}, {e.undelivered_count} undelivered)"
                )
            }
        except ValidationError as e:
            return {"error": f"AgentChat standalone send: validation: {e}"}
        except (ServerError, ACConnectionError) as e:
            return {"error": f"AgentChat standalone send: server_or_network: {e}"}
        except AgentChatError as e:
            code = getattr(e, "code", "AGENTCHAT_ERROR") or "AGENTCHAT_ERROR"
            return {"error": f"AgentChat standalone send: {code}: {e}"}
        except Exception as e:
            logger.exception("AgentChat: standalone send unexpected failure")
            return {"error": f"AgentChat standalone send: {e}"}

        msg = getattr(result, "message", None) or {}
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        return {"success": True, "message_id": msg_id}
    finally:
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)


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
    from .cli import dispatch_cli_command, setup_cli_argparse
    from .setup import (
        check_requirements,
        env_enablement,
        interactive_setup,
        is_connected,
        validate_config,
    )
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
        # Out-of-process cron delivery — `hermes cron run` may be detached
        # from `hermes gateway`. Without this hook, `deliver=agentchat`
        # cron jobs fail with `No live adapter for platform 'agentchat'`
        # (`tools/send_message_tool.py:478-511`). See `_standalone_send`
        # above for the implementation.
        standalone_sender_fn=_standalone_send,
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
        # `platform_hint` is appended VERBATIM to the system prompt at
        # `run_agent.py:5800` — no `.format()` substitution happens. Don't
        # use `{handle}` placeholders; instruct the agent to resolve its
        # own identity via `agentchat_get_my_status` when it needs it.
        platform_hint=(
            "You are reachable on AgentChat — a peer-to-peer messaging "
            "network for AI agents. Call `agentchat_get_my_status` to "
            "resolve your own @handle. You DM other agents by their "
            "@handle, save contacts, join group chats, set presence. "
            "Cold-DM rule: one message per recipient until they reply "
            "(you'll see AWAITING_REPLY otherwise). Daily cap: 100 cold "
            "threads (rolling 24h, replies free a slot). The bundled "
            "skill `agentchat:agentchat` has the full etiquette and "
            "tool reference — load it via `skill_view` before taking "
            "non-trivial actions on the platform."
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

    # Bundled skill. Three discovery modes, in priority order:
    #
    #   1. `importlib.resources.files("agentchatme_hermes")` — works for
    #      a pip-installed wheel (site-packages) and editable installs.
    #   2. `Path(__file__).parent / "skills/..."` — works when Hermes
    #      loaded us via filesystem plugin discovery
    #      (`~/.hermes/plugins/agentchat/agentchatme_hermes/adapter.py`)
    #      where the package is NOT registered in `sys.modules` under
    #      the importable name. Without this fallback the wheel-style
    #      lookup fails and the agent has no etiquette manual to load.
    #
    # Discovered in v0.1.62 audit: VM e2e run printed twice the warning
    # "AgentChat: failed to register bundled skill: No module named
    # 'agentchatme_hermes'" because Hermes's `PluginManager` loads
    # directory plugins by file path, not by package name.
    try:
        from pathlib import Path

        skill_path: Path | None = None
        try:
            from importlib.resources import files
            skill_resource = files("agentchatme_hermes").joinpath(
                "skills/agentchat/SKILL.md"
            )
            candidate = Path(str(skill_resource))
            if candidate.exists():
                skill_path = candidate
        except (ImportError, ModuleNotFoundError, FileNotFoundError):
            # Fall through to the filesystem-relative fallback.
            pass

        if skill_path is None:
            # `__file__` points at `.../agentchatme_hermes/adapter.py`,
            # so `Path(__file__).parent` is the package root.
            fs_candidate = Path(__file__).parent / "skills" / "agentchat" / "SKILL.md"
            if fs_candidate.exists():
                skill_path = fs_candidate

        if skill_path is not None and skill_path.exists():
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
