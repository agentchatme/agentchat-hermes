"""The served texts: fetched from ``GET /v1/texts``, cached on disk, refreshed daily.

Every word an agent sees about AgentChat is written once, on the server,
and served as data (``docs/architecture.md``, rule 3): the wake instruction
a turn starts with, the invite prompt, the identity line placed in SOUL.md,
and where the manual is. This package carries no copy of any of them. It
fetches them through the SDK (``client.texts.get(runtime="hermes")``) at
start and every 24 hours, and keeps the last copy in the plugin's state
directory so a gateway that starts offline is not mute.

Start-up policy
───────────────
* server reachable → the served texts, cache rewritten;
* server unreachable, cache present → the cached texts, one log line saying
  how old they are, retried every five minutes until the server answers;
* server unreachable, no cache → start anyway, one log line; nothing can be
  placed and no turn can start until a fetch succeeds (the retry loop keeps
  trying; the first success fires :meth:`TextsStore.on_texts` callbacks so
  the identity line is placed late rather than never).

Nothing here spells tool names: :mod:`.turn` renders the served texts the
way Hermes names the hosted tools.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from agentchatme.types import Texts

from .state import read_json, write_json_atomic

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from agentchatme import AgentChatClient

logger = logging.getLogger(__name__)

TEXTS_RUNTIME = "hermes"
REFRESH_INTERVAL_S = 24 * 60 * 60.0
RETRY_INTERVAL_S = 5 * 60.0
RETRY_MAX_S = 60 * 60.0
CACHE_VERSION = 1

TextsSource = Literal["server", "cache"]
TextsCallback = Callable[["Texts"], None]


@dataclass(frozen=True)
class LoadedTexts:
    """Texts in use, and where they came from."""

    texts: Texts
    fetched_at: float
    """When the server served them (epoch seconds), also for a cached copy."""
    source: TextsSource

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.fetched_at)


# ─── one-shot: fetch, else cache ───────────────────────────────────────────


def fetch_texts(client: AgentChatClient, *, runtime: str = TEXTS_RUNTIME) -> Texts:
    """``GET /v1/texts?runtime=hermes`` through the SDK. Raises what the SDK raises.

    The result must be the SDK's :class:`Texts`; anything else (a stubbed
    client, an SDK that changed shape) is treated as a failed fetch rather
    than cached.
    """
    texts = client.texts.get(runtime=runtime)
    if not isinstance(texts, Texts):
        raise TypeError(f"texts.get returned {type(texts).__name__}, not Texts")
    return texts


def read_cache(path: Path) -> LoadedTexts | None:
    """The cached texts, or ``None`` when absent, corrupt or of another version."""
    raw: Any = read_json(path)
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return None
    fetched_at = raw.get("fetched_at")
    body = raw.get("texts")
    if not isinstance(fetched_at, (int, float)) or not isinstance(body, dict):
        return None
    try:
        texts = Texts.model_validate(body)
    except Exception:
        return None
    return LoadedTexts(texts=texts, fetched_at=float(fetched_at), source="cache")


def write_cache(path: Path, texts: Texts, *, fetched_at: float, runtime: str = TEXTS_RUNTIME) -> None:
    write_json_atomic(
        path,
        {
            "version": CACHE_VERSION,
            "runtime": runtime,
            "fetched_at": fetched_at,
            "texts": texts.model_dump(mode="json"),
        },
    )


def load_texts(
    client: AgentChatClient,
    *,
    cache_path: Path | None,
    runtime: str = TEXTS_RUNTIME,
    now: Callable[[], float] = time.time,
    quiet: bool = False,
) -> LoadedTexts | None:
    """Fetch the texts; on success rewrite the cache, on failure fall back to it.

    Never raises. ``None`` means neither the server nor the cache had
    anything. The wizard, ``doctor`` and the runtime's attempts all go
    through here so the policy is written once. ``quiet`` logs a failure at
    DEBUG instead of WARNING: the store sets it once an outage has been
    reported, so a retry every few minutes does not repeat the line.
    """
    try:
        texts = fetch_texts(client, runtime=runtime)
    except Exception as exc:
        level = logging.DEBUG if quiet else logging.WARNING
        cached = read_cache(cache_path) if cache_path is not None else None
        if cached is not None:
            logger.log(
                level,
                "texts: /v1/texts unreachable (%s) — using the cached copy from %.0f h ago",
                exc,
                cached.age_s(now()) / 3600.0,
            )
            return cached
        logger.log(
            level,
            "texts: /v1/texts unreachable (%s) and nothing is cached%s — no wake instruction, "
            "invite prompt or identity line until the server answers",
            exc,
            f" at {cache_path}" if cache_path is not None else "",
        )
        return None
    fetched_at = now()
    if cache_path is not None:
        try:
            write_cache(cache_path, texts, fetched_at=fetched_at, runtime=runtime)
        except OSError as exc:
            logger.warning("texts: could not write the cache at %s: %s", cache_path, exc)
    return LoadedTexts(texts=texts, fetched_at=fetched_at, source="server")


# ─── the store: start, refresh on a timer, tell listeners ──────────────────


class TextsStore:
    """Holds the texts in use for one account and keeps them fresh.

    :meth:`start` makes one synchronous attempt (so the caller knows what it
    has) and then runs :meth:`refresh` on a daemon thread: every 24 h after
    a served copy, every 5 min (backing off to 1 h) while the texts in use
    are cached or absent. Listeners registered with :meth:`on_texts` are
    called with the current texts immediately when some are loaded, and
    again whenever a refresh brings different ones.
    """

    def __init__(
        self,
        *,
        client: AgentChatClient,
        cache_path: Path | None,
        runtime: str = TEXTS_RUNTIME,
        refresh_interval_s: float = REFRESH_INTERVAL_S,
        retry_interval_s: float = RETRY_INTERVAL_S,
        retry_max_s: float = RETRY_MAX_S,
        now: Callable[[], float] = time.time,
        thread_scope: Callable[[], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._client = client
        # Entered by the refresh thread for its lifetime. A served-texts change
        # fires listeners there, and the SOUL.md reconcile they run resolves its
        # file through Hermes's context-local HERMES_HOME override: without the
        # profile's context a multiplexed gateway's refresh would read and write
        # the LAUNCH profile's SOUL.md. The runtime passes ``profile_context``.
        self._thread_scope = thread_scope
        self._cache_path = cache_path
        self._runtime = runtime
        self._refresh_interval_s = refresh_interval_s
        self._retry_interval_s = retry_interval_s
        self._retry_max_s = retry_max_s
        self._now = now

        self._lock = threading.Lock()
        self._loaded: LoadedTexts | None = None
        self._listeners: list[TextsCallback] = []
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.refreshes = 0

    # -- state ------------------------------------------------------------------

    @property
    def loaded(self) -> LoadedTexts | None:
        with self._lock:
            return self._loaded

    def get(self) -> Texts | None:
        """The texts in use, or ``None`` while nothing could be loaded."""
        loaded = self.loaded
        return loaded.texts if loaded is not None else None

    def on_texts(self, callback: TextsCallback) -> None:
        """Call ``callback(texts)`` now if texts are loaded, and on every change."""
        with self._lock:
            self._listeners.append(callback)
            current = self._loaded
        if current is not None:
            self._notify(callback, current.texts)

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> LoadedTexts | None:
        """One synchronous attempt, then the refresh thread. Returns what is in use."""
        loaded = self.refresh()
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="agentchat-texts-refresh", daemon=True
            )
            self._thread.start()
        return loaded

    def stop(self) -> None:
        self._stop.set()

    def refresh(self) -> LoadedTexts | None:
        """Fetch now (falling back to the cache only while nothing is loaded).

        A failure while served texts are already in use keeps them: the last
        served copy is fresher than anything the cache holds.
        """
        self.refreshes += 1
        with self._lock:
            previous = self._loaded
        loaded: LoadedTexts | None
        if previous is not None and previous.source == "server":
            loaded, served = self._fetch_or_keep(previous)
        else:
            # One warning per outage: ``_failures`` is 0 until the first
            # failed attempt and resets only when the server answers.
            loaded = load_texts(
                self._client,
                cache_path=self._cache_path,
                runtime=self._runtime,
                now=self._now,
                quiet=self._failures > 0,
            )
            served = loaded is not None and loaded.source == "server"
            if loaded is None:
                loaded = previous
        changed = False
        with self._lock:
            if loaded is not None:
                changed = self._loaded is None or self._loaded.texts != loaded.texts
                self._loaded = loaded
            if served:
                self._failures = 0
            else:
                self._failures += 1
            listeners = list(self._listeners)
        if changed and loaded is not None:
            for listener in listeners:
                self._notify(listener, loaded.texts)
        return loaded

    def _fetch_or_keep(self, previous: LoadedTexts) -> tuple[LoadedTexts, bool]:
        """``(texts in use, whether this attempt was served)``."""
        try:
            texts = fetch_texts(self._client, runtime=self._runtime)
        except Exception as exc:
            level = logging.WARNING if self._failures == 0 else logging.DEBUG
            logger.log(
                level,
                "texts: refresh failed (%s) — keeping the copy served %.0f h ago",
                exc,
                previous.age_s(self._now()) / 3600.0,
            )
            return previous, False
        fetched_at = self._now()
        if self._cache_path is not None:
            try:
                write_cache(self._cache_path, texts, fetched_at=fetched_at, runtime=self._runtime)
            except OSError as exc:
                logger.warning("texts: could not write the cache at %s: %s", self._cache_path, exc)
        return LoadedTexts(texts=texts, fetched_at=fetched_at, source="server"), True

    def next_delay(self) -> float:
        """Seconds until the next refresh attempt, from the current state."""
        with self._lock:
            loaded = self._loaded
            failures = self._failures
        if loaded is not None and loaded.source == "server" and failures == 0:
            return max(0.0, self._refresh_interval_s - loaded.age_s(self._now()))
        backoff = self._retry_interval_s * (2.0 ** max(0, failures - 1))
        return min(self._retry_max_s, backoff)

    def _loop(self) -> None:
        if self._thread_scope is None:
            self._refresh_until_stopped()
            return
        with self._thread_scope():
            self._refresh_until_stopped()

    def _refresh_until_stopped(self) -> None:
        while not self._stop.wait(self.next_delay()):
            try:
                self.refresh()
            except Exception:
                # The refresh thread must outlive any surprise.
                logger.exception("texts: refresh raised")
                with self._lock:
                    self._failures += 1

    @staticmethod
    def _notify(callback: TextsCallback, texts: Texts) -> None:
        try:
            callback(texts)
        except Exception:
            logger.exception("texts: listener raised")
