"""The one restart a first install needs, started by the person's Allow.

Hermes never switches a running process's dependency environment, so a gateway that was running
when this plugin was first installed runs AgentChat only after a restart
(:func:`.placement.gateway_cannot_load`). In Hermes every chat runs in the gateway, so an agent
that installs the plugin itself always meets this. The person's yes (``/agentchat allow`` in a
chat, or the question at their own terminal) is the moment the restart is theirs to want: it is
started then (:func:`.allow.restart_after_yes`), never from an agent's command and never by the
reminder.

Hermes has no plugin call for a restart. Its documented graceful restart is SIGUSR1 (what
``systemctl reload hermes-gateway`` and ``hermes gateway restart`` send): the gateway waits until
no work is in flight, then exits for its supervisor to relaunch. :mod:`.restart_helper`, detached,
sends it after a short delay, waits for that gateway to end, and where nothing supervises it runs
``hermes gateway ensure`` (what the desktop app runs); a gateway a chat started is left to that
chat, which starts the next one with its next message. Lab, Hermes v0.23.4 (2026-11-28): under a
systemd user service AgentChat was live 22 s after the Allow; a desktop-style gateway about 40 s
(the 20 s supervisor wait included); a chat-started gateway with the chat's next message; in each
the chat stayed the same session, its history kept.

One request per gateway process, whichever profile of a multiplexed gateway asks: the record is
``<hermes root>/agentchat-locks/load-restart-<pid>.json``, written and read under a lock, rewritten
by the helper at each step (:func:`restart_state`). POSIX only (Windows has no SIGUSR1: the person
restarts), and never from a root process (``ensure`` would start the user's gateway as root).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

HELPER = Path(__file__).with_name("restart_helper.py")
RECORD_PREFIX = "load-restart-"
LOCK_FILENAME = "load-restart.lock"
LOG_FILENAME = "load-restart.log"
# Hermes reacts to the allow (it reloads the chat's MCP connections); the restart waits for that.
DELAY_S = 5.0
# How long a supervisor (systemd, launchd, a container's policy) gets to relaunch before `ensure`.
RELAUNCH_WAIT_S = 20.0

State = Literal["under_way", "failed"]
_FAILED = "failed"


def supported() -> bool:
    """Can a restart be requested from here at all (POSIX, with SIGUSR1, not as root)?"""
    if os.name == "nt" or not hasattr(signal, "SIGUSR1"):
        return False
    return os.geteuid() != 0


def request_load_restart(home: Path) -> bool:
    """Start the restart that loads the plugin into the gateway serving ``home``'s profile.

    ``True`` when it is under way (started now, or by an earlier request for the same gateway
    process that has not failed), ``False`` when nothing was started: not supported here, no
    gateway found, or the helper could not be started (the caller then says the restart is the
    person's). Never raises."""
    if not supported():
        return False
    try:
        from .leader_lock import held_lock

        target = _gateway(home)
        if target is None:
            return False
        directory = _record_dir(home)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with held_lock(directory / LOCK_FILENAME, timeout_s=5.0) as lock:
            if lock == "busy":
                return True  # another process of this machine is requesting it right now
            record = _read(directory, target[0])
            if record is not None and _same(record, target) and record.get("state") != _FAILED:
                return True
            path = _record_path(directory, target[0])
            _write(path, {"pid": target[0], "start_time": target[1], "state": "requested", "at": time.time()})
            _spawn_helper(home, target[0], path)
        return True
    except Exception:
        logger.warning("agentchat plugin: the gateway restart was not started", exc_info=True)
        return False


def restart_state(home: Path) -> State | None:
    """The restart requested for the gateway serving ``home``'s profile now: ``under_way``,
    ``failed`` (the helper's record says why), or ``None`` (none requested for this process).
    Never raises."""
    try:
        target = _gateway(home)
        if target is None:
            return None
        record = _read(_record_dir(home), target[0])
        if record is None or not _same(record, target):
            return None
        return "failed" if record.get("state") == _FAILED else "under_way"
    except Exception:
        logger.debug("agentchat plugin: the restart record was not read", exc_info=True)
        return None


def restart_detail(home: Path) -> str:
    """Why the restart requested for this gateway failed (the helper's words), or ``""``."""
    try:
        target = _gateway(home)
        record = _read(_record_dir(home), target[0]) if target is not None else None
        if record is None or target is None or not _same(record, target):
            return ""
        detail = record.get("detail")
        return detail if isinstance(detail, str) else ""
    except Exception:
        return ""


def log_path(home: Path) -> Path:
    """Where the helper writes one line per step."""
    return _record_dir(home) / LOG_FILENAME


def _gateway(home: Path) -> tuple[int, int | None] | None:
    """The gateway serving ``home``'s profile: this process when it is that gateway, else the one
    Hermes sees running (:func:`.allow.gateway_seen`)."""
    from .allow import gateway_seen, process_start_time
    from .leader_lock import is_hermes_gateway_process

    if is_hermes_gateway_process():
        pid = os.getpid()
        return pid, process_start_time(pid)
    seen = gateway_seen(home)
    return seen if isinstance(seen, tuple) else None


def _same(record: dict[str, Any], target: tuple[int, int | None]) -> bool:
    """The record names this gateway process (Hermes' start-time drift tolerance)."""
    from .allow import same_gateway

    pid, start = record.get("pid"), record.get("start_time")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    return same_gateway((pid, start if isinstance(start, int) and not isinstance(start, bool) else None), target)


def _record_dir(home: Path) -> Path:
    """``<hermes root>/agentchat-locks/``: one per machine root, so every profile a multiplexed
    gateway serves finds the same record."""
    from .leader_lock import LOCK_DIRNAME
    from .profile_scope import hermes_root_of

    return hermes_root_of(Path(home)) / LOCK_DIRNAME


def _record_path(directory: Path, pid: int) -> Path:
    return directory / f"{RECORD_PREFIX}{pid}.json"


def _read(directory: Path, pid: int) -> dict[str, Any] | None:
    from .state import read_json

    record = read_json(_record_path(directory, pid))
    return record if isinstance(record, dict) else None


def _write(path: Path, record: dict[str, Any]) -> None:
    from .state import write_json_atomic

    write_json_atomic(path, record)


def _hermes_command(home: Path) -> tuple[list[str], dict[str, str]]:
    """``hermes`` as Hermes spawns its own gateway (``hermes_cli/gateway_runtime_start.py``): its
    Python, the module form, ``--profile default`` for a root home (else the sticky
    ``active_profile`` would pick another profile) and the profile home as ``HERMES_HOME`` for a
    ``profiles/<name>`` home; ``ensure`` maps a profile a multiplexer serves to the multiplexer."""
    resolved = Path(home).expanduser().resolve()
    selector = [] if resolved.parent.name == "profiles" else ["--profile", "default"]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(resolved)
    return [sys.executable, "-m", "hermes_cli.main", *selector], env


def _spawn_helper(home: Path, pid: int, status: Path) -> None:
    command, env = _hermes_command(home)
    log_path = status.parent / LOG_FILENAME
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} restart of gateway {pid} requested\n")
        log.flush()
        subprocess.Popen(  # its own session: it outlives the gateway it restarts
            [
                sys.executable, "-I", str(HELPER),
                "--pid", str(pid), "--delay", str(DELAY_S), "--relaunch-wait", str(RELAUNCH_WAIT_S),
                "--status", str(status), "--", *command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
