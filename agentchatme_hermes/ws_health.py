"""Durable liveness record for the gateway's AgentChat receiving.

The leader lock proves that a process owns the receive role; it does not prove
that the daemon thread or its asyncio loop is still healthy, nor that the
worker that turns wakes into turns is. The record beside the lock carries
both: the socket's state (written by the daemon, refreshed every minute) and,
under ``worker``, the worker's (state, whether its thread is alive, a
heartbeat that moves while it is responsive, restarts, turns that overran).
``hermes agentchat doctor`` reads it to tell a live receiver from a connected
socket whose worker is dead or stuck.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

WSHealthState = Literal[
    "starting", "connected", "reconnecting", "restart_wait", "stopped"
]

_VALID_STATES = {"starting", "connected", "reconnecting", "restart_wait", "stopped"}
_VERSION = 1
HEALTH_STALE_AFTER_SECONDS = 150.0


class _WSHealthRequired(TypedDict):
    version: int
    pid: int
    handle: str
    state: WSHealthState
    updated_at: float
    last_connected_at: float | None
    restart_attempt: int
    last_error: str | None


class WSHealth(_WSHealthRequired, total=False):
    # The invoker's :meth:`~.agent_invoker.AgentInvoker.status`. Absent in
    # records written by versions before the worker was supervised.
    worker: dict[str, Any]


def default_health_path() -> Path:
    """Pre-0.2.3111111 per-machine location. Kept for stale-file diagnostics."""
    return Path.home() / ".hermes" / "agentchat-ws-health.json"


def health_path_for(lock_path: Path) -> Path:
    """The health record that belongs to one account's leader lock.

    ``.../agentchat-locks/ws-<fingerprint>.lock`` →
    ``.../agentchat-locks/ws-<fingerprint>.health.json``. One record per
    account, beside the lock it describes, so ``doctor`` for profile A
    never reads profile B's heartbeat.
    """
    return lock_path.with_name(f"{lock_path.stem}.health.json")


def write_ws_health(
    *,
    handle: str,
    state: WSHealthState,
    restart_attempt: int = 0,
    last_error: str | None = None,
    last_connected_at: float | None = None,
    path: Path | None = None,
    now: float | None = None,
    worker: Mapping[str, Any] | None = None,
) -> WSHealth:
    """Atomically replace the health record with mode 0600.

    ``last_error`` is bounded because exception text can contain large HTTP
    response bodies. Health is diagnostics, not an unbounded log sink.
    """
    target = path or default_health_path()
    timestamp = time.time() if now is None else now
    record: WSHealth = {
        "version": _VERSION,
        "pid": os.getpid(),
        "handle": handle,
        "state": state,
        "updated_at": timestamp,
        "last_connected_at": last_connected_at,
        "restart_attempt": max(0, int(restart_attempt)),
        "last_error": last_error[-500:] if last_error else None,
    }
    if worker is not None:
        record["worker"] = dict(worker)

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp: Path | None = None
    try:
        # NamedTemporaryFile uses exclusive creation, avoiding both stale-temp
        # collisions after a crash and predictable-name symlink attacks.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp = Path(file.name)
            os.chmod(temp, 0o600)
            json.dump(record, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
    finally:
        if temp is not None:
            with suppress(FileNotFoundError):
                temp.unlink()
    return record


def read_ws_health(path: Path | None = None) -> WSHealth | None:
    target = path or default_health_path()
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None

    if not isinstance(raw, dict):
        return None
    if raw.get("version") != _VERSION or raw.get("state") not in _VALID_STATES:
        return None
    if not isinstance(raw.get("pid"), int) or not isinstance(raw.get("handle"), str):
        return None
    if not isinstance(raw.get("updated_at"), (int, float)):
        return None
    if not isinstance(raw.get("restart_attempt"), int):
        return None
    last_connected = raw.get("last_connected_at")
    if last_connected is not None and not isinstance(last_connected, (int, float)):
        return None
    last_error = raw.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        return None
    worker = raw.get("worker")
    if worker is not None and not isinstance(worker, dict):
        return None
    return cast("WSHealth", raw)


class HealthBoard:
    """One account's health record, written from two sources.

    The socket daemon reports its state with :meth:`socket`; the worker's
    state is read from ``worker_status`` at every write, and the worker calls
    :meth:`refresh` when it changes. Each write composes the latest of both
    under one lock, so neither source overwrites the other with stale data.
    Never raises into a caller that does not want it: the daemon and the
    worker both swallow write errors (health is diagnostics only).
    """

    def __init__(
        self,
        *,
        handle: str,
        path: Path | None,
        worker_status: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._handle = handle
        self._path = path
        self._worker_status = worker_status
        self._lock = threading.Lock()
        self._state: WSHealthState = "starting"
        self._restart_attempt = 0
        self._last_error: str | None = None
        self._last_connected_at: float | None = None

    @property
    def path(self) -> Path:
        return self._path or default_health_path()

    def set_worker_status(self, worker_status: Callable[[], Mapping[str, Any]] | None) -> None:
        with self._lock:
            self._worker_status = worker_status

    def socket(
        self,
        state: WSHealthState,
        *,
        restart_attempt: int = 0,
        last_error: str | None = None,
        last_connected_at: float | None = None,
    ) -> WSHealth:
        with self._lock:
            self._state = state
            self._restart_attempt = restart_attempt
            self._last_error = last_error
            self._last_connected_at = last_connected_at
            return self._write_locked()

    def refresh(self) -> WSHealth:
        """Rewrite the record with the last socket state and the worker's state now."""
        with self._lock:
            return self._write_locked()

    def _write_locked(self) -> WSHealth:
        worker: Mapping[str, Any] | None = None
        if self._worker_status is not None:
            try:
                worker = self._worker_status()
            except Exception:
                worker = {"state": "unknown", "alive": False, "last_error": "status raised"}
        return write_ws_health(
            handle=self._handle,
            state=self._state,
            restart_attempt=self._restart_attempt,
            last_error=self._last_error,
            last_connected_at=self._last_connected_at,
            path=self._path,
            worker=worker,
        )


def health_age_seconds(health: WSHealth, *, now: float | None = None) -> float:
    timestamp = time.time() if now is None else now
    return max(0.0, timestamp - float(health["updated_at"]))
