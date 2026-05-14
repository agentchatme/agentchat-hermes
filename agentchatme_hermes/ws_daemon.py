"""Background thread that owns the AgentChat WebSocket.

A single daemon thread runs a private asyncio event loop and hosts
the SDK's :class:`agentchatme.RealtimeClient`. Lifecycle:

* :meth:`start` spins up the thread, schedules ``connect()`` on the
  loop, returns immediately. Initial WS handshake runs in the
  background — does not block plugin registration.
* The SDK owns reconnect, HELLO handshake, per-conversation seq
  ordering, gap-fill, and offline ``/sync`` drain on reconnect. We
  don't reimplement any of that.
* :meth:`stop` schedules a clean disconnect and joins the thread
  within a bounded grace period.

Inbound ``message.new`` frames go through :meth:`_on_message_frame`
which:

1. Skips frames where ``sender == own_handle`` — our own outbound
   echoes back through the WS (server-side fan-out) and would
   otherwise wake the agent with its own reply.
2. Maps the payload into an :class:`InboundEvent`.
3. Pushes to the :class:`MessageQueue`.
4. Notifies the invoker that there's work to do.

Pattern mirrors ``plugins/memory/hindsight/__init__.py:_get_loop``:
one long-lived loop, one daemon thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from agentchatme import AsyncAgentChatClient, RealtimeClient

    from .config import Config
    from .message_queue import MessageQueue
    from .types import AgentIdentity

logger = logging.getLogger(__name__)

_THREAD_NAME = "agentchat-ws-daemon"
_STOP_GRACE_SECONDS = 5.0

# Heartbeat interval. Long enough to not spam the log, short enough
# that "is the daemon alive?" is a question with an answer ≤60s old.
_HEARTBEAT_INTERVAL_S = 60.0


class WSDaemon:
    """Owns the long-lived AgentChat WebSocket.

    Single-use — call :meth:`start` once, :meth:`stop` once. Re-use
    requires a new instance. Each individual call is idempotent
    (double-start / double-stop are safe no-ops).
    """

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        queue: MessageQueue,
        on_new_event: Callable[[], None],
    ) -> None:
        self._config = config
        self._identity = identity
        self._queue = queue
        self._on_new_event = on_new_event

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._rt_client: RealtimeClient | None = None
        self._http_client: AsyncAgentChatClient | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()

        # Set by ``_main`` once the loop is running. ``_shutdown``
        # signals this to cleanly unblock ``_main``'s park-forever
        # await, replacing the older ``await asyncio.Future()`` pattern
        # that needed task cancellation to unwind.
        self._stop_event: asyncio.Event | None = None

        # Operational counters for the periodic heartbeat log. Bumped
        # atomically inside :meth:`_on_message_frame` so the heartbeat
        # can report what's flowing without coordinating locks.
        self._frames_seen = 0
        self._frames_self_filtered = 0
        self._frames_queued = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return

            self._thread = threading.Thread(
                target=self._run_loop,
                name=_THREAD_NAME,
                daemon=True,
            )
            self._thread.start()

        # Block briefly so callers can rely on the loop being ready
        # to schedule work. WS handshake runs on the loop itself.
        self._started.wait(timeout=2.0)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()

        loop = self._loop
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop)

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_GRACE_SECONDS)
            if thread.is_alive():
                logger.warning(
                    "WSDaemon: thread did not exit within %.1fs grace",
                    _STOP_GRACE_SECONDS,
                )

    # -- internals ----------------------------------------------------------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._started.set()

        try:
            loop.run_until_complete(self._main())
        except Exception:
            logger.exception("WSDaemon: event loop crashed")
        finally:
            try:
                loop.close()
            except Exception:
                logger.debug("WSDaemon: loop close raised", exc_info=True)

    async def _main(self) -> None:
        from agentchatme import AsyncAgentChatClient, RealtimeClient

        # AsyncAgentChatClient on the RealtimeClient enables two
        # SDK-provided guarantees we depend on:
        #   1. Auto-drain of offline envelopes on (re)connect via
        #      `GET /v1/messages/sync` — no inbound is lost across
        #      a disconnect.
        #   2. Per-conversation seq-gap recovery via `GET
        #      /v1/messages/{conv_id}` — eliminates duplicate /
        #      out-of-order delivery during fan-out.
        self._http_client = AsyncAgentChatClient(
            api_key=self._config.api_key,
            base_url=self._config.api_base,
        )

        self._rt_client = RealtimeClient(
            api_key=self._config.api_key,
            base_url=self._config.ws_url,
            client=self._http_client,
        )
        self._rt_client.on("message.new", self._on_message_frame)
        self._rt_client.on_connect(self._on_connect)
        self._rt_client.on_disconnect(self._on_disconnect)
        self._rt_client.on_error(self._on_error)

        # Stop-event lives on this loop. ``_shutdown`` (scheduled
        # cross-thread) sets it, which unblocks the park-forever wait
        # below — cleaner than the old "await never-resolved Future
        # + cancel" pattern, which left a "thread did not exit within
        # 5.0s grace" warning on most shutdowns.
        self._stop_event = asyncio.Event()

        try:
            await self._rt_client.connect()
        except Exception:
            logger.exception("WSDaemon: initial connect failed")
            return

        # Start the heartbeat alongside the park-forever wait so the
        # daemon emits a "still alive" signal every minute even when
        # no traffic is flowing. Both run concurrently; either ending
        # unblocks the main coroutine.
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await self._stop_event.wait()
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

    async def _heartbeat_loop(self) -> None:
        """Periodic 'WSDaemon alive' signal with counters.

        Bridges the visibility gap that the first spike exposed: a
        silent daemon could be alive-and-idle or dead-and-stuck, and
        from outside the process there was no way to tell. The
        heartbeat distinguishes the two: while this log line is
        appearing every 60s the daemon thread is alive; if it stops
        appearing while the gateway is still running, the daemon is
        wedged.
        """
        rt_client = self._rt_client
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            except asyncio.CancelledError:
                return
            try:
                connected = bool(rt_client.is_connected) if rt_client else False
                logger.info(
                    "WSDaemon heartbeat: connected=%s handle=@%s "
                    "frames=%d self_filtered=%d queued=%d pending_convs=%d",
                    connected,
                    self._identity.handle,
                    self._frames_seen,
                    self._frames_self_filtered,
                    self._frames_queued,
                    self._queue.pending_count(),
                )
            except Exception:
                # The heartbeat must never crash the daemon. Log and
                # keep ticking — diagnostic noise is far less harmful
                # than a silent dead loop.
                logger.exception("WSDaemon: heartbeat-log raised")

    async def _shutdown(self) -> None:
        # Signal the park-forever wait in ``_main`` to unblock cleanly.
        # The Event-based approach (vs the old task-cancellation dance)
        # lets the loop drain pending tasks and exit on its own without
        # the 5.0s grace timeout firing.
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()

        rt = self._rt_client
        if rt is not None:
            try:
                await rt.disconnect()
            except Exception:
                logger.debug("WSDaemon: rt disconnect raised", exc_info=True)

        http = self._http_client
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                logger.debug("WSDaemon: http aclose raised", exc_info=True)

        loop = self._loop
        if loop is not None:
            # Cancel any background tasks the SDK might still have
            # (reconnect timers, hello-ack watchdogs) so the loop can
            # exit. Don't cancel the current task — that's us.
            for task in asyncio.all_tasks(loop):
                if task is not asyncio.current_task():
                    task.cancel()
            loop.call_soon_threadsafe(loop.stop)

    # -- SDK callbacks ------------------------------------------------------

    def _on_connect(self) -> None:
        logger.info("WSDaemon: connected as @%s", self._identity.handle)

    def _on_disconnect(self, info: dict[str, Any]) -> None:
        logger.info(
            "WSDaemon: disconnected (code=%s reason=%s clean=%s)",
            info.get("code"),
            info.get("reason"),
            info.get("was_clean"),
        )

    def _on_error(self, exc: BaseException) -> None:
        # SDK-emitted error — does not necessarily indicate a fatal
        # state (transient socket errors fire here while the SDK's
        # internal reconnect handles recovery). We log at warning
        # level and let the SDK's machinery decide what to do.
        logger.warning("WSDaemon: error from SDK: %s", exc)

    def _on_message_frame(self, frame: dict[str, Any]) -> None:
        """Inbound ``message.new`` callback.

        Receives the full envelope ``{type: "message.new", payload:
        {...}}``. The SDK guarantees seq-ordered, deduplicated
        delivery — we don't need to defend against either at this
        layer.

        Every frame is logged at INFO. The first spike had us
        unable to tell whether a peer's message had reached our
        plugin or not, because the only log signal was a turn
        completing at the far end of the pipeline. With per-frame
        logging the receipt is visible the instant it arrives.

        Defensive try/except around the queue push and the wake
        callback — a single bad frame must NOT take down the daemon.
        """
        from .types import InboundEvent

        self._frames_seen += 1

        payload = frame.get("payload")
        if not isinstance(payload, dict):
            logger.warning(
                "WSDaemon: received message.new without dict payload "
                "(seen=%d, type=%s) — dropping",
                self._frames_seen,
                type(payload).__name__,
            )
            return

        sender = (payload.get("from") or payload.get("sender") or "").lstrip("@").lower()
        msg_id = payload.get("id", "<no-id>")
        conv_id = payload.get("conversation_id", "<no-conv>")

        if sender == self._identity.handle:
            # Own outbound, echoed back by server-side fan-out. Suppress
            # so we don't wake the agent on its own reply.
            self._frames_self_filtered += 1
            logger.debug(
                "WSDaemon: filtered own echo conv=%s msg=%s",
                conv_id,
                msg_id,
            )
            return

        event = InboundEvent.from_ws_message(payload)
        if event is None:
            logger.warning(
                "WSDaemon: dropping malformed payload conv=%s msg=%s keys=%s",
                conv_id,
                msg_id,
                sorted(payload.keys()),
            )
            return

        logger.info(
            "WSDaemon: received message.new conv=%s msg=%s from=@%s "
            "kind=%s text_chars=%d",
            event.conversation_id,
            event.message_id,
            event.sender_handle,
            event.conversation_kind,
            len(event.content_text),
        )

        try:
            self._queue.push(event)
            self._frames_queued += 1
        except Exception:
            logger.exception(
                "WSDaemon: queue.push raised for conv=%s msg=%s — "
                "dropping (daemon survives, future frames continue)",
                event.conversation_id,
                event.message_id,
            )
            return

        try:
            self._on_new_event()
        except Exception:
            logger.exception(
                "WSDaemon: on_new_event callback raised for conv=%s — "
                "daemon survives, but the invoker may not have woken; "
                "the next frame's wake signal will pick up this event "
                "from the queue.",
                event.conversation_id,
            )
