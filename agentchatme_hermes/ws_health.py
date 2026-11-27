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

import functools
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

# Cycle outcomes (the kit's ``CycleSummary.outcome``) that mean the receive
# loop could not do its job that time: the tray unreadable, the model
# unreachable or its answer unusable (``fail_closed``), no served texts, no
# hosted tools or no turn can start (``tools_unavailable``), or a cycle that
# never finished (``abandoned``). A run of them is what doctor alarms on.
BAD_CYCLE_OUTCOMES = frozenset(
    {"tray_failed", "fail_closed", "texts_missing", "tools_unavailable", "abandoned"}
)


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
    # The plugin build the writing process runs (:func:`plugin_build`).
    # Absent in records written by releases before 0.7.0.
    plugin: dict[str, str]


@functools.lru_cache(maxsize=1)
def plugin_build() -> dict[str, str]:
    """The plugin this process runs: its version, and a fingerprint of its
    code — sha256 over the package's ``.py`` files (their paths and bytes),
    the first 12 hex digits. Taken once per process, first at ``register()``,
    so a gateway's health record names the code it loaded: Hermes keeps
    imported code until the process restarts, while an update replaces the
    files. ``doctor`` takes the installed files' the same way; a same-version
    commit (a ``--ref`` install between releases) differs by its build."""
    from ._version import __version__

    return {"version": __version__, "build": code_fingerprint(Path(__file__).resolve().parent)}


def code_fingerprint(root: Path) -> str:
    """sha256 over the ``.py`` files under ``root`` (each one's relative path
    and bytes, in path order), the first 12 hex digits; ``unknown`` when a
    file cannot be read."""
    import hashlib

    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    except OSError:
        return "unknown"
    return digest.hexdigest()[:12]


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
    record["plugin"] = dict(plugin_build())

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


def pause_end(value: Any) -> float | None:
    """A paused conversation's ``until`` (``worker.loop.could_not_process.
    paused[].until``: the server's ``paused_until``, ISO 8601) as epoch
    seconds; ``None`` when unreadable."""
    if not isinstance(value, str) or not value:
        return None
    import re
    from datetime import datetime

    text = value.strip().replace("Z", "+00:00")
    # Python 3.9 / 3.10 read only 3 or 6 fraction digits; Postgres drops
    # trailing zeros (``.12345+00:00``): pad or cut to 6.
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def bad_cycle_run(recent: Any) -> tuple[int, dict[str, Any] | None]:
    """How many of the newest cycles in ``recent`` (the health record's
    ``worker.loop.recent_cycles``, oldest first) were bad in a row, and the
    newest of them."""
    if not isinstance(recent, list):
        return 0, None
    run = 0
    newest: dict[str, Any] | None = None
    for cycle in reversed(recent):
        if not isinstance(cycle, dict) or cycle.get("outcome") not in BAD_CYCLE_OUTCOMES:
            break
        run += 1
        newest = newest or cycle
    return run, newest


# ─── whether the gateway behind a record lives (doctor, status, logout) ─────


def live_health(api_base: str, api_key: str, hermes_home: Path | None = None) -> Any:
    """This account's health record, only while the gateway that writes it
    lives: fresh (≤ 150 s), not ``stopped``, its pid alive, and the leader
    lock beside it held (where the lock can be probed). A gateway stopped or
    killed leaves a fresh record saying ``connected`` behind it (Hermes has no
    shutdown hook for a plugin; the receiver marks ``stopped`` at exit only
    when the interpreter exits cleanly), so freshness alone is not enough.
    ``None`` otherwise. Never raises."""
    try:
        from .leader_lock import describe_lock_holder, lock_path_for
        from .profile_scope import account_fingerprint, resolve_hermes_home, resolve_setting

        lock_dir_raw = (resolve_setting("AGENTCHATME_LOCK_DIR") or "").strip()
        lock_path = lock_path_for(
            hermes_home=hermes_home if hermes_home is not None else resolve_hermes_home(),
            account_fingerprint=account_fingerprint(api_base, api_key),
            lock_dir=Path(lock_dir_raw).expanduser() if lock_dir_raw else None,
        )
        health = read_ws_health(health_path_for(lock_path))
        if health is None or health["state"] == "stopped":
            return None
        if health_age_seconds(health) > HEALTH_STALE_AFTER_SECONDS:
            return None
        if pid_alive(health["pid"]) is not True:
            return None
        lock = describe_lock_holder(lock_path)
        if lock in ("free", "not present"):
            return None
        return health
    except Exception:
        return None


def pid_alive(pid: int) -> bool | None:
    """Whether a process with ``pid`` exists here; ``None`` when that cannot be
    told (Windows without ``psutil``: ``os.kill`` there would end the process)."""
    try:
        import importlib

        psutil: Any = importlib.import_module("psutil")
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, owned by another user
    except OSError:
        return None
    return True
