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
import hashlib
import json
import logging
import os
import time
import uuid
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
    SystemAgentProtectedError,
    UnauthorizedError,
    ValidationError,
)
from agentchatme.errors import (
    ConnectionError as ACConnectionError,
)

from . import metrics as _metrics_mod

logger = logging.getLogger(__name__)


# ── Idempotency helper ──────────────────────────────────────────────────────
#
# Stable `client_msg_id` derived from the send tuple + a coarse time bucket.
# See `AgentChatAdapter.send` for the contract rationale.
#
# Window of 120 seconds = 5x Hermes's worst-case retry ladder (1s/2s/4s).
# Every attempt of one logical send lands in the same window → same id →
# server dedupes. A legitimate re-send 2+ minutes later gets a new id and
# is treated as a fresh message. UUIDv5 over a stable namespace yields a
# canonical 36-char UUID string the SDK accepts as-is.

_IDEMPOTENCY_WINDOW_SECONDS = 120
# Derived from `uuid.uuid5(NAMESPACE_DNS, "agentchatme-hermes.agentchat.me")`.
# Stable across releases; rotating it would change every existing id, which
# is fine because ids are ephemeral (only meaningful inside one send
# attempt's retry window).
_IDEMPOTENCY_NAMESPACE = uuid.UUID("870db470-9f32-5e78-897e-d7c626fb60b0")


def _rest_base_to_ws_base(rest_base: str) -> str:
    """Convert a REST base URL (``https://…`` / ``http://…``) into a
    WebSocket base URL (``wss://…`` / ``ws://…``).

    The agentchatme SDK's ``RealtimeClient`` constructs the WebSocket
    URL via ``f"{base_url}/v1/ws"`` (``agentchatme/_realtime.py:228``)
    without any scheme normalization. Passing the same URL the REST
    client uses (``https://…``) makes the underlying ``websockets``
    library reject the URI with ``scheme isn't ws or wss``.

    Pass-through for already-WS schemes so an operator who sets
    ``AGENTCHATME_API_BASE`` to a ``wss://…`` value still works.
    Falls back to the default if the input is empty.
    """
    if not rest_base:
        return "wss://api.agentchat.me"
    s = rest_base.rstrip("/")
    lowered = s.lower()
    if lowered.startswith("https://"):
        return "wss://" + s[len("https://"):]
    if lowered.startswith("http://"):
        return "ws://" + s[len("http://"):]
    if lowered.startswith("wss://") or lowered.startswith("ws://"):
        return s
    # No scheme at all — assume the operator meant the secure variant.
    return "wss://" + s


def _stable_client_msg_id(
    sender_handle: str,
    chat_id: str,
    content: str,
    reply_to: str | None,
) -> str:
    """Stable per-attempt-window UUID for SDK send dedup.

    Hashing inputs before UUIDv5 keeps content out of the namespace
    string (avoids accidentally putting the message text in any log
    that prints the namespace value).
    """
    bucket = int(time.time()) // _IDEMPOTENCY_WINDOW_SECONDS
    digest = hashlib.sha256(
        f"{sender_handle}|{chat_id}|{reply_to or ''}|{bucket}|{content}".encode()
    ).hexdigest()
    return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, digest))


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

        # Tells Hermes's stream consumer not to set itself up for this
        # platform (`gateway/run.py:14363`). Streaming-text delivery on
        # other platforms works by sending a partial message, then
        # editing it as more tokens arrive. AgentChat's message-tool-only
        # contract (see `send` and `set_message_handler` below) means we
        # never want mid-turn text to land in the chat at all — the
        # agent must explicitly call `agentchat_send_message`. Setting
        # this flag false skips the consumer entirely; the no-op `send`
        # would catch the deltas regardless, but skipping the setup
        # avoids the upstream cost (buffering, edit-interval scheduling)
        # of producing chunks that would just be dropped.
        SUPPORTS_MESSAGE_EDITING = False

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
            #
            # `on_backlog_warning` lets us observe when a peer recipient is
            # piling up (5000-10000 undelivered) BEFORE the hard
            # RECIPIENT_BACKLOGGED 429 fires, so the operator gets a log
            # heads-up rather than discovering it via failed sends.
            self._client = AsyncAgentChatClient(
                api_key=self.api_key,
                base_url=self.api_base,
                on_backlog_warning=self._on_backlog_warning,
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
                # 30s outer cap matches the SDK's default request timeout
                # plus retry headroom (was 15s before v0.1.71, which
                # truncated the SDK's own retry ladder).
                me = await asyncio.wait_for(self._client.get_me(), timeout=30.0)
                self.handle = me.get("handle")
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

            # Wire realtime.
            #
            # CRITICAL: the agentchatme SDK's `RealtimeClient` does NOT
            # rewrite the scheme. At `agentchatme/_realtime.py:228` it
            # constructs the WebSocket URL as `f"{base_url}/v1/ws"`
            # via raw string concatenation, then hands it straight to
            # the `websockets` library. Pass it `https://…` and the WS
            # connect dies with `URI: scheme isn't ws or wss`.
            #
            # The default `base_url` on `RealtimeOptions` is
            # `"wss://api.agentchat.me"` (verified at
            # `agentchatme/_realtime.py:82`). REST and realtime live on
            # the same host but use different schemes, so we convert
            # https→wss / http→ws here. ws/wss are passed through
            # untouched so an operator who already configured
            # `AGENTCHATME_API_BASE=wss://…` (unlikely but defensible)
            # still works.
            #
            # Inbound has been silently broken on every version of this
            # plugin since v0.1.0 because the original comment claimed
            # the SDK auto-rewrites. It does not. Discovered when a
            # real user noticed their agent never received replies.
            self._realtime = RealtimeClient(
                RealtimeOptions(
                    api_key=self.api_key,
                    base_url=_rest_base_to_ws_base(self.api_base),
                    client=self._client,  # enables gap-fill + offline drain
                    on_sequence_gap=self._on_sequence_gap,
                )
            )

            # Hook handlers BEFORE connect so the very first frame after
            # hello.ok dispatches through us. The SDK queues frames until
            # at least one handler is registered for that event name.
            #
            # `group.message` is NOT registered — the SDK never emits it.
            # All inbound (DM + group) flows through `message.new` and we
            # branch on conversation_id prefix. See _on_realtime_frame.
            self._handler_unsubs = [
                self._realtime.on("message.new", self._on_realtime_frame),
                self._realtime.on("group.deleted", self._on_realtime_frame),
                self._realtime.on("group.invite.received", self._on_realtime_frame),
                self._realtime.on("rate_limit.warning", self._on_realtime_frame),
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
            Frame shapes are defined in the SDK's ``_realtime.py`` and the
            server-side emitter in ``apps/api-server/src/services/``.
            Currently subscribed: ``message.new`` (DMs + group messages),
            ``group.deleted``, ``group.invite.received``,
            ``rate_limit.warning``.
            """
            ftype = frame.get("type")
            recorder = _metrics_mod.get_recorder()

            # The SDK's contract (`agentchatme/_realtime.py:563`) is:
            # **every inbound message — DM or group — arrives as a
            # `message.new` frame**. The `group.message` frame type we
            # used to listen for does NOT exist in the SDK and never
            # fires. To distinguish DM from group, we inspect the
            # payload's `conversation_id` prefix: `grp_*` = group,
            # anything else (`conv_*`, `dir_*`, etc.) = direct.
            #
            # Verified against `/v1/conversations` shapes:
            #   DM:    {"id": "conv_IcwG…",  "type": "direct"}
            #   Group: {"id": "grp_HtQb…",   "type": "group"}
            #
            # Before this fix every group message was misclassified as
            # a DM, the agent's reply was routed via `to=@<sender>`
            # back to the sender's private DM instead of the group, and
            # the group appeared silent to everyone else.
            if ftype == "message.new":
                payload = frame.get("payload") or {}
                conv_id = str(payload.get("conversation_id") or "")
                kind = "group" if conv_id.startswith("grp_") else "direct"
                recorder.inc_inbound("group_message" if kind == "group" else "message_new")
                await self._dispatch_inbound_message(payload, kind=kind)
                return

            if ftype == "group.deleted":
                # Server emits the deleted-group info under `payload`, not
                # `data` (fixed in v0.1.71). The SDK's WsMessage model
                # declares `payload: dict[str, Any]` and the server emits
                # via `payload: ctx.deletedPayload` (api-server's
                # group-deletion-fanout-worker). The previous `data` read
                # always returned None → handler showed empty group_id /
                # "@?" deletor in the system message.
                recorder.inc_inbound("group_deleted")
                await self._dispatch_group_deleted(frame.get("payload") or {})
                return

            if ftype == "group.invite.received":
                # Realtime invite event — server pushes when someone
                # invites this agent to a group. Without subscribing,
                # the agent only learns about invites by polling
                # `agentchat_list_group_invites` or on reconnect drain.
                recorder.inc_inbound("group_invite_received")
                await self._dispatch_group_invite_received(frame.get("payload") or {})
                return

            if ftype == "rate_limit.warning":
                # Early-warning signal before the server starts firing
                # hard 429s. The agent gets a chance to throttle
                # proactively; the operator gets a log line.
                recorder.inc_inbound("rate_limit_warning")
                payload = frame.get("payload") or {}
                logger.warning(
                    "AgentChat: rate_limit.warning — %s",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
                return

            # message.read / presence.update / typing.* / rate_limit.warning —
            # framework has no direct analog. Logged at debug, dropped.
            recorder.inc_inbound("ignored")
            logger.debug("AgentChat: ignoring realtime frame type=%s", ftype)

        async def _dispatch_inbound_message(
            self, payload: dict[str, Any], *, kind: str
        ) -> None:
            # SDK and server use `sender` exclusively; the legacy `from`
            # fallback was dead code (verified against `types/message.py`
            # and api-server in v0.1.71).
            sender = payload.get("sender")
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

            # Address Hermes will use when the agent replies. The
            # AgentChat server rejects `conversation_id=conv_...` for
            # DMs with `validation: Use 'to' to send to a direct
            # conversation` — DMs are addressed by the recipient's
            # @handle, not the conversation id. Groups still use the
            # conversation_id because that's the only handle a group
            # has. Discovered when an agent's reply to an inbound DM
            # silently failed in the v0.1.65 hot-fix verification.
            conversation_id = str(payload.get("conversation_id") or "")
            # DM → route Hermes's reply via `to=@sender` (server only
            # accepts `to=` for direct conversations).
            # Group → keep the conversation_id (groups have no single
            # recipient handle, only the conv id).
            chat_id = conversation_id if kind == "group" else f"@{sender_handle}"
            message_id = str(payload.get("id") or "")

            content_obj = payload.get("content") or {}
            ac_type = payload.get("type", "text")

            # System messages from the AgentChat server (member_joined,
            # member_left, settings changed, group avatar updated, etc.)
            # are SERVER-SIDE NOTIFICATIONS, not user input.
            #
            # Previously (v0.1.71 and earlier) we stringified them as
            # `[system] {json}` and dispatched them through
            # `handle_message`. Hermes saw a "user message", spawned an
            # agent session, the agent ran 7 tool calls trying to make
            # sense of the JSON, and then BOTH:
            #   (a) called `agentchat_send_message` to react ("welcome!")
            #   (b) emitted a final text response, which Hermes
            #       auto-routed back to the same conversation
            # That's 2 messages per system event. When the operator
            # added the bot to "The Vibe Council", multiple
            # `member_joined` events fired back-to-back; the agent
            # spammed the group with welcomes + thought-narrations.
            #
            # The right behavior: drop. Agents don't need to react to
            # server-side state changes. When they DO need to know the
            # group's state (e.g., before composing a message), they
            # poll `agentchat_get_conversation_participants` or
            # `agentchat_list_group_invites`. Fixed in v0.1.72.
            if ac_type == "system":
                event_kind = (
                    content_obj.get("data", {}).get("event")
                    if isinstance(content_obj.get("data"), dict)
                    else None
                ) or "unknown"
                logger.info(
                    "AgentChat: dropped system event '%s' in %s (server-side notification, not user input)",
                    event_kind, conversation_id or "?",
                )
                _metrics_mod.get_recorder().inc_inbound("system_event_dropped")
                return

            # Render content as a single text string for the agent. The
            # raw payload is preserved on raw_message so an agent that
            # wants the structured shape can read it.
            #
            # MessageType inference: Hermes routes media-typed events to
            # vision/file-aware pipelines downstream. Even without
            # downloading the attachment (deferred — needs a fresh REST
            # roundtrip we don't want to block the realtime handler on),
            # tagging the event with the correct `MessageType` lets the
            # framework apply the right session policy. The agent
            # resolves the actual content via
            # `agentchat_get_attachment_download_url`.
            message_type = self._MessageType.TEXT
            if ac_type == "text":
                text = content_obj.get("text", "")
            elif ac_type == "file":
                att_id = content_obj.get("attachment_id", "")
                # Best-effort MIME inference. AgentChat servers populate
                # `mime_type` on `content` when available; older payloads
                # may not. Default to DOCUMENT so the agent at least
                # knows to call `agentchat_get_attachment_download_url`.
                mime = (content_obj.get("mime_type") or "").lower()
                if mime.startswith("image/"):
                    message_type = self._MessageType.PHOTO
                    text = f"[image attachment {att_id}]"
                elif mime.startswith("video/"):
                    message_type = self._MessageType.VIDEO
                    text = f"[video attachment {att_id}]"
                elif mime.startswith("audio/"):
                    message_type = self._MessageType.AUDIO
                    text = f"[audio attachment {att_id}]"
                else:
                    message_type = self._MessageType.DOCUMENT
                    text = f"[attachment {att_id}]"
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
                message_type=message_type,
                source=source,
                raw_message=payload,
                message_id=message_id,
            )

            # Set the source platform context so the
            # `agentchat_share_api_key_with_operator` tool's
            # AgentChat-peer short-circuit fires for any tool call
            # inside this inbound's session. ContextVar values
            # propagate into the Task that `handle_message` spawns
            # (`_process_message_background`), so the agent's whole
            # turn sees "this turn was triggered by AgentChat".
            from . import tools as _tools_mod

            _tools_mod.current_source_platform.set("agentchat")

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
            # Same AgentChat-source tagging as the regular dispatch path
            # so the operator-key tool refuses on this turn too.
            from . import tools as _tools_mod

            _tools_mod.current_source_platform.set("agentchat")

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

        async def _on_sequence_gap(
            self,
            conversation_id: str,
            recovered: bool,
            reason: str,
            from_seq: int,
            to_seq: int,
        ) -> None:
            """SDK callback when a per-conversation seq gap is detected.

            When the SDK can't fill the gap (network down too long, buffer
            overflowed past 500 messages, gap_fill endpoint failed), it
            advances ``next_expected_seq`` past the hole and the agent
            never sees the missing messages. Without this handler the
            silent loss was invisible to operators.

            We log at WARNING when ``recovered=False`` so the gap is
            permanent and an operator can decide if a manual sync is
            warranted.
            """
            recorder = _metrics_mod.get_recorder()
            recorder.inc_inbound(
                f"seq_gap_{'recovered' if recovered else 'unrecovered'}"
            )
            level = logger.info if recovered else logger.warning
            level(
                "AgentChat: sequence gap in %s — seqs %d..%d, recovered=%s, reason=%s",
                conversation_id, from_seq, to_seq, recovered, reason,
            )

        async def _on_backlog_warning(self, warning: Any) -> None:
            """SDK callback when the server sends X-Backlog-Warning header.

            Recipient has 5000-10000 undelivered messages pending; the
            next send will likely fail with RECIPIENT_BACKLOGGED. Surface
            as a metric + log line so the operator sees the gathering
            storm before the hard 429s start.
            """
            recipient = getattr(warning, "recipient_handle", "?")
            count = getattr(warning, "undelivered_count", "?")
            logger.warning(
                "AgentChat: backlog warning — @%s has %s undelivered messages pending",
                recipient, count,
            )
            _metrics_mod.get_recorder().inc_inbound("backlog_warning")

        async def _dispatch_group_invite_received(
            self, payload: dict[str, Any]
        ) -> None:
            """A peer invited us to a group. Surface as a system MessageEvent so
            the agent can decide to accept/reject via the existing tools."""
            group_id = str(payload.get("group_id") or payload.get("conversation_id") or "")
            inviter = str(payload.get("inviter_handle") or "?").lstrip("@")
            group_name = str(payload.get("group_name") or group_id or "(unnamed)")
            text = (
                f"[system] Group invite from @{inviter}: \"{group_name}\" "
                f"({group_id}). Use agentchat_accept_group_invite or "
                f"agentchat_reject_group_invite to act."
            )
            source = self.build_source(
                chat_id=group_id,
                chat_name=group_name,
                chat_type="group",
            )
            event = self._MessageEvent(
                text=text,
                message_type=self._MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=f"sys:{group_id}:invite",
            )
            try:
                from . import tools as _tools_mod
                _tools_mod.current_source_platform.set("agentchat")
                await self.handle_message(event)
            except Exception:
                logger.exception("AgentChat: handle_message failed for group.invite.received")

        # ── Message-tool-only mode: silence-by-default contract ───────────
        #
        # AgentChat is a peer-to-peer agent network. Hermes's default reply
        # mechanic — "spawn session, LLM produces a final_response text,
        # framework auto-routes it to the source chat" — is the wrong model
        # for this platform. The same mechanic that makes Telegram bots
        # responsive turns two Hermes agents into an infinite ping-pong
        # because neither side has a "be silent" option: any text the LLM
        # emits at end-of-turn becomes a chat message. Worse, every Hermes
        # side-channel that's purely informational on Telegram/Slack
        # (mid-turn streaming deltas, "📬 set a home channel" hint, interim
        # assistant commentary) becomes brain-thought leakage on AgentChat.
        #
        # The fix is structural and runs in three layers, so a regression
        # at any single layer doesn't reopen the leak:
        #
        #   1. ``SUPPORTS_MESSAGE_EDITING = False`` at the class level
        #      (above) — Hermes's stream consumer (`run.py:14329-14409`)
        #      skips its own setup for editing-incapable adapters, so
        #      mid-turn token deltas never get buffered for delivery.
        #   2. ``send`` is a no-op — every framework-internal path that
        #      reaches ``adapter.send(...)`` (final-response delivery from
        #      ``base.py:2868``, ``_deliver_platform_notice``, status /
        #      interim callbacks, ``_send_with_retry``) lands here, gets
        #      logged at DEBUG, and returns a synthetic success. Nothing
        #      reaches the wire unless an agent tool explicitly calls
        #      ``client.send_message(...)``.
        #   3. ``set_message_handler`` wraps whatever Hermes registers so
        #      the real handler still runs (LLM, tools, session lifecycle
        #      all execute normally) but its return value — the LLM's
        #      wrap-up text — is discarded before the framework can route
        #      it. Defense-in-depth against any future Hermes change that
        #      bypasses ``adapter.send`` for the handler's return path.
        #
        # The bundled skill and ``platform_hint`` teach the LLM that this
        # is the contract — "your end-of-turn text is private; speak only
        # by calling ``agentchat_send_message``." Without those nudges
        # the LLM would keep producing wrap-up text it expects to be sent
        # and the agent would appear silent. With them, the LLM treats
        # AgentChat the way a thoughtful human treats Slack: read, think,
        # occasionally chime in.
        #
        # The agent's only delivery path is ``agentchat_send_message``,
        # which goes directly to ``client.send_message(...)`` on the SDK
        # (see ``tools._h_send_message``) — it does NOT route through
        # ``adapter.send`` and is therefore unaffected by the no-op
        # above. Cron-side delivery uses ``_standalone_send`` below,
        # also direct-to-SDK.

        def set_message_handler(self, handler):  # type: ignore[override]
            """Wrap Hermes's handler so its return value never auto-replies.

            See the block comment above for the architectural rationale.
            """
            async def message_tool_only_wrapper(event):
                # Run Hermes's real handler so the LLM runs, tools fire,
                # session lifecycle completes normally. Catch (and log)
                # exceptions to keep the wrapper transparent — without
                # this, an exception in the real handler would surface
                # as if the wrapper itself raised, and Hermes's session
                # supervisor would treat the adapter as misbehaving.
                try:
                    await handler(event)
                except Exception:
                    logger.exception(
                        "AgentChat: wrapped message handler raised"
                    )
                # ALWAYS None. Suppresses the framework auto-reply.
                # The agent must call `agentchat_send_message` explicitly
                # to put a message into the chat.
                return None

            # Preserve the original handler's identity so Hermes-side
            # introspection / unregister logic still works against the
            # wrapped reference if needed.
            message_tool_only_wrapper.__wrapped__ = handler  # type: ignore[attr-defined]
            super().set_message_handler(message_tool_only_wrapper)

        # ── Outbound: BasePlatformAdapter.send is a silent no-op ──────────

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> Any:
            """Drop every framework-internal send — message-tool-only contract.

            On Telegram/Slack-style platforms, Hermes calls ``adapter.send``
            from several proactive paths: the message handler's return
            value (``base.py:2868``), ``_deliver_platform_notice`` for
            setup hints like "📬 set a home channel" (``run.py:7096``),
            ``_status_adapter.send`` for interim assistant commentary
            (``run.py:14430``), the stream consumer's per-delta calls
            (``run.py:14400``), and ``_send_with_retry`` shims around all
            of the above. Each one is benign-to-helpful when the chat is
            a human ↔ bot conversation. On AgentChat — a peer-to-peer
            fabric of autonomous agents — every one of those becomes
            ambient "brain thought" leakage: one agent's internal
            reasoning, status text, or framework prompts arriving as
            chat messages to other agents in the room.

            The fix is to make this method a no-op. The agent's only
            sanctioned delivery path is the ``agentchat_send_message``
            tool, which routes directly to ``client.send_message`` on
            the SDK (``tools._h_send_message``) and bypasses this method
            entirely. Cron-driven delivery (``_standalone_send`` below)
            also goes direct-to-SDK.

            Return a synthetic ``success=True`` ``SendResult`` so the
            framework's ``_send_with_retry`` (``base.py:2315``) doesn't
            treat the no-op as a transient failure and retry it forever.
            Log at DEBUG so operators inspecting traffic can see exactly
            what the framework tried to send and why it didn't land.

            See the ``set_message_handler`` block-comment above for the
            full three-layer silence-contract rationale.
            """
            preview = content if isinstance(content, str) else "<non-text>"
            if len(preview) > 120:
                preview = preview[:117] + "…"
            logger.debug(
                "AgentChat: dropped framework-internal send "
                "(chat_id=%r, %d chars, reply_to=%r, has_metadata=%s): %r",
                chat_id,
                len(content) if isinstance(content, str) else 0,
                reply_to,
                bool(metadata),
                preview,
            )
            return self._SendResult(success=True, message_id=None)

        async def send_typing(
            self, chat_id: str, metadata: dict[str, Any] | None = None
        ) -> None:
            # Typing indicators are still WIP server-side. No-op for now;
            # the framework's default is also no-op so this override is
            # only documentation that we considered the surface.
            return None

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
            # Classification rules (server convention):
            #   grp_*        → group
            #   conv_*/dir_* → dm
            #   @handle      → dm
            #   bare handle  → dm
            #   anything else → fallback to group (best-effort)
            cid = chat_id.strip()
            if cid.startswith("grp_"):
                kind = "group"
            elif (
                cid.startswith(("conv_", "@"))
                or (cid and "/" not in cid and " " not in cid)
            ):
                kind = "dm"
            else:
                kind = "group"
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
    elif cid.startswith(("grp_", "conv_")):
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

    # Idempotency. Cron has its own retry-on-failure ladder
    # (`cron/scheduler.py:_run_with_retries`). Without a stable
    # `client_msg_id`, a retry after partial-success risks duplicate
    # delivery. Same windowed-hash strategy as the in-gateway path.
    # Sender handle isn't resolvable here (we'd need an extra REST
    # roundtrip), so we use the api_key prefix as a stand-in
    # discriminator — same key always produces same id contribution.
    kwargs["client_msg_id"] = _stable_client_msg_id(
        api_key[:16],
        cid,
        kwargs["content"].get("text", "") if isinstance(kwargs.get("content"), dict) else "",
        kwargs.get("metadata", {}).get("reply_to") if isinstance(kwargs.get("metadata"), dict) else None,
    )

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
        except SystemAgentProtectedError as e:
            return {"error": f"AgentChat standalone send: system_agent_protected: {e}"}
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


# ─── Persistent-identity platform hint ────────────────────────────────────
#
# Hermes appends `platform_hint` to the system prompt verbatim, every turn,
# every session, every sub-agent — same coverage as OpenClaw's `AGENTS.md`
# anchor. The OpenClaw plugin writes the literal handle into AGENTS.md at
# wizard finalize; we do the equivalent by interpolating the handle into
# the platform_hint at register() time. Effect: in any context (a Twitter
# task, a sub-agent run, a cron job firing in a non-AgentChat surface),
# the agent reads "You are @alice on AgentChat" as part of its system
# prompt — no tool call needed to remember its own address.

_PLATFORM_HINT_TEMPLATE_WITH_HANDLE = (
    "You are @{handle} on AgentChat — a peer-to-peer messaging network "
    "for AI agents. Your handle is your address here, like a phone number, "
    "except the other end is always another agent.\n"
    "\n"
    "- Other agents can DM you. You can DM them — any time, for anything "
    "that fits your context.\n"
    "- Share @{handle} freely. Whenever and wherever you think you should "
    "share. It's how peers reach you.\n"
    "- Cold-DM rule: one message per recipient until they reply (you'll "
    "see AWAITING_REPLY otherwise). Daily cap: 100 cold threads (rolling "
    "24h, replies free a slot).\n"
    "- When you need to send, search, or handle an error, the bundled "
    "skill `agentchat:agentchat` has the full reference — load it via "
    "`skill_view` before taking non-trivial actions on the platform.\n"
    "\n"
    "**HOW SPEAKING WORKS ON AGENTCHAT.** AgentChat is peer-to-peer "
    "between agents — like Slack between humans, not like Telegram with "
    "a bot. The default is **silence**. Your turn-end reasoning text is "
    "**internal** — it never reaches any chat. The ONLY way to send a "
    "message is to explicitly call the `agentchat_send_message` tool "
    "with a real recipient and a real text body. If you have nothing "
    "worth saying, say nothing — that's correct behavior, not a bug. "
    "Read the inbound, think, and either choose to call "
    "`agentchat_send_message` once with a deliberate message, or let "
    "the turn end with no tool call. Don't narrate your reasoning, "
    "don't summarize what you found, don't ask polite follow-ups — "
    "those land nowhere on this platform.\n"
    "\n"
    "This is an identity, not a tool you reach for occasionally."
)

_PLATFORM_HINT_TEMPLATE_NO_HANDLE = (
    "You are reachable on AgentChat — a peer-to-peer messaging network "
    "for AI agents. Call `agentchat_get_my_status` to resolve your own "
    "@handle. You DM other agents by their @handle, save contacts, join "
    "group chats, set presence. Cold-DM rule: one message per recipient "
    "until they reply (you'll see AWAITING_REPLY otherwise). Daily cap: "
    "100 cold threads (rolling 24h, replies free a slot). "
    "\n\n"
    "**HOW SPEAKING WORKS ON AGENTCHAT.** AgentChat is peer-to-peer "
    "between agents. The default is silence. Your turn-end reasoning "
    "text is internal — it never reaches any chat. The only way to "
    "send a message is to explicitly call `agentchat_send_message`. "
    "If you have nothing worth saying, say nothing. "
    "\n\n"
    "The bundled skill `agentchat:agentchat` has the full etiquette "
    "and tool reference — load it via `skill_view` before taking "
    "non-trivial actions on the platform."
)


def _build_platform_hint() -> str:
    """Interpolate the handle from env at register() time, or fall back.

    `AGENTCHATME_HANDLE` is written to `~/.hermes/.env` by the wizard
    after a successful register/paste-and-validate. Hermes loads `.env`
    into the process environment before our `register()` runs, so we
    can read it here directly.

    Two correctness guards:

    * Sanity-check the handle against the canonical regex so a corrupted
      .env doesn't inject garbage into the system prompt. Falls back to
      the no-handle template if the env value doesn't look like a real
      AgentChat handle (lowercase letters/digits/hyphens, 3-30 chars,
      starts with a letter).
    * Cap the env read at register() time — the handle is captured into
      a stable string when the platform registers. If the wizard changes
      it later in the same process, the new hint only takes effect on
      the next gateway restart. This is intentional: re-registering
      changes the agent's identity, which is a thing the operator should
      notice as a restart event.
    """
    import re as _re

    # NB: no `.lower()` here. The wizard already canonicalizes the handle
    # to lowercase before persisting; a hand-edited .env that has mixed
    # case or other invalid shape gets the fallback template instead of
    # being silently normalized. Strict validation prevents corrupt env
    # values from injecting unexpected content into the system prompt.
    handle = (os.getenv("AGENTCHATME_HANDLE") or "").strip().lstrip("@")
    if (
        handle
        and _re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", handle)
        and 3 <= len(handle) <= 30
    ):
        return _PLATFORM_HINT_TEMPLATE_WITH_HANDLE.format(handle=handle)
    return _PLATFORM_HINT_TEMPLATE_NO_HANDLE


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
        # `run_agent.py:5800` — no `.format()` substitution happens. We
        # interpolate the handle ourselves here at register() time so the
        # agent sees its literal identity ("You are @alice on AgentChat")
        # in every session, every turn, every sub-agent.
        #
        # This is the Hermes equivalent of the OpenClaw plugin's
        # `AGENTS.md` anchor write (`agents-anchor.ts:126-140`). Same
        # prose — "Your handle is your address here, like a phone
        # number, except the other end is always another agent" — so an
        # agent installed on both runtimes has identical situational
        # awareness about being on AgentChat.
        #
        # Falls back to the resolve-via-tool form when the handle env
        # isn't set (fresh install, wizard hasn't run yet, etc.). The
        # `env_enablement_fn` and the `connect()` identity probe both
        # backfill identity from the API key when reached.
        platform_hint=_build_platform_hint(),
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
    #
    # Hermes's `PluginManager` does NOT automatically deregister tools
    # that a plugin partially-registered before raising
    # (`hermes_cli/plugins.py:1069-1136` — the failure path leaves
    # tool entries in the global `tools.registry` registry). So if
    # `register_all_tools` raises midway through the 41-tool sweep
    # (a programmer error in a new release), the surviving tools
    # appear in the agent's tool list with no live handler implementing
    # them — confusing for the agent and impossible to clean up
    # without restarting the gateway.
    #
    # We snapshot the registry before the call and roll back our
    # contributions on failure. This is the "defensive plugin"
    # pattern documented at v0.13 in the upstream contracts research.
    try:
        from tools.registry import registry as _tool_registry  # type: ignore[import-not-found]
        before = set(_tool_registry.get_all_tool_names())
    except Exception:
        _tool_registry = None  # type: ignore[assignment]
        before = set()

    try:
        register_all_tools(ctx)
    except Exception as e:
        logger.warning(
            "AgentChat: tool registration failed — platform still works, "
            "but the agentchat_* tool surface is unavailable: %s",
            e,
        )
        # Roll back partial state so the agent never sees orphaned
        # tool names. The registry's `deregister` is exposed at
        # `tools/registry.py:303` (verified in the v0.13 audit).
        if _tool_registry is not None:
            try:
                after = set(_tool_registry.get_all_tool_names())
                orphans = sorted(t for t in (after - before) if t.startswith("agentchat_"))
                for name in orphans:
                    with contextlib.suppress(Exception):
                        _tool_registry.deregister(name)
                if orphans:
                    logger.info(
                        "AgentChat: deregistered %d partially-loaded tools after failure",
                        len(orphans),
                    )
            except Exception:
                logger.exception("AgentChat: rollback of partial tool registration failed")
