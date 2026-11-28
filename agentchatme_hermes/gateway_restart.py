"""The one restart a first install needs, started by the person's Allow.

Hermes never switches a running process's dependency environment, so a gateway that was running
when this plugin was first installed runs AgentChat only after a restart
(:func:`.placement.gateway_cannot_load`). In Hermes every chat runs in the gateway, so an agent
that installs the plugin itself always meets this. The person's Allow (``/agentchat allow`` in a
chat, or the question at their own terminal) is the moment the restart is theirs to want: it is
started then, never by the agent.

Hermes has no plugin call for a restart. Its documented graceful restart is SIGUSR1 (what
``systemctl reload hermes-gateway`` and ``hermes gateway restart`` send): the gateway waits until
no work is in flight, then exits for its supervisor to relaunch. A gateway nobody supervises (the
desktop app's, one a chat started, a hand-started ``gateway run``) is relaunched by nobody, so
:mod:`.restart_helper`, detached, runs ``hermes gateway ensure`` (what the desktop app runs) once
the old one has ended. Lab, Hermes v0.23.4 (2026-11-28): under a systemd user service the new
gateway was up 8 s after the Allow and the tools 17 s; a chat-started gateway came back for the
chat's next message; in both the chat stayed the same session, its history kept.

POSIX only: Windows has no SIGUSR1, and there the person restarts as before. Requested once per
gateway process (a second Allow, or the terminal question after the chat's, starts nothing new).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RECORD_FILENAME = "load-restart.json"
LOG_FILENAME = "load-restart.log"
HELPER = Path(__file__).with_name("restart_helper.py")
# Hermes reacts to the allow (it reloads the chat's MCP connections); the restart waits for that.
DELAY_S = 5.0
# How long a supervisor (systemd, launchd, a container) gets to relaunch before `gateway ensure`.
RELAUNCH_WAIT_S = 20.0
# A request for the same gateway process within this long is the same request.
SAME_REQUEST_S = 3600.0


def supported() -> bool:
    """Can a restart be requested from here at all (POSIX, with SIGUSR1)?"""
    return os.name != "nt" and hasattr(signal, "SIGUSR1")


def request_load_restart(home: Path) -> bool:
    """Start the restart that loads the plugin into the gateway serving ``home``'s profile.

    ``True`` when it is under way (now or by an earlier request for the same gateway process),
    ``False`` when nothing was started: not POSIX, no gateway found, or the helper could not be
    started (the caller then says the restart is the person's). Never raises."""
    if not supported():
        return False
    try:
        target = _gateway(home)
        if target is None:
            return False
        pid, start_time = target
        record = _record_path(home)
        if _already_requested(record, pid, start_time):
            return True
        _spawn_helper(home, pid)
        _write_record(record, pid, start_time)
        return True
    except Exception:
        logger.warning("agentchat plugin: the gateway restart was not started", exc_info=True)
        return False


def _gateway(home: Path) -> tuple[int, int | None] | None:
    """The gateway serving ``home``'s profile: this process when it is that gateway, else the one
    Hermes sees running (:func:`.allow.gateway_seen`)."""
    from .allow import gateway_seen, process_start_time
    from .leader_lock import is_hermes_gateway_process

    if is_hermes_gateway_process():
        pid = os.getpid()
        return pid, process_start_time(pid)
    seen = gateway_seen(home)
    if isinstance(seen, tuple):
        return seen
    return None


def _hermes_command(home: Path) -> list[str]:
    """``hermes`` as this process runs it (Hermes' own Python), for the profile whose gateway it is:
    the default profile's gateway serves the profiles it multiplexes."""
    from .placement import default_gateway_pid
    from .profile_scope import profile_label

    command = [sys.executable, "-m", "hermes_cli.main"]
    label = profile_label(home)
    if label != "default" and default_gateway_pid(home) is None:
        command += ["-p", label]
    return command


def _spawn_helper(home: Path, pid: int) -> None:
    from .key_store import store_dir

    directory = store_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / LOG_FILENAME, "a", encoding="utf-8") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} restart of gateway {pid} requested\n")
        log.flush()
        subprocess.Popen(  # its own session: it outlives the gateway it restarts
            [
                sys.executable, "-I", str(HELPER),
                "--pid", str(pid), "--delay", str(DELAY_S), "--relaunch-wait", str(RELAUNCH_WAIT_S),
                "--", *_hermes_command(home),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )


def _record_path(home: Path) -> Path:
    from .key_store import store_dir

    return store_dir(home) / RECORD_FILENAME


def _already_requested(path: Path, pid: int, start_time: int | None) -> bool:
    from .state import read_json

    record: Any = read_json(path)
    if not isinstance(record, dict) or record.get("pid") != pid:
        return False
    if start_time is not None and record.get("start_time") not in (None, start_time):
        return False  # the same pid, another process
    requested = record.get("requested_at")
    return isinstance(requested, (int, float)) and time.time() - requested < SAME_REQUEST_S


def _write_record(path: Path, pid: int, start_time: int | None) -> None:
    from .state import write_json_atomic

    try:
        write_json_atomic(path, {"pid": pid, "start_time": start_time, "requested_at": time.time()})
    except OSError:
        logger.debug("agentchat plugin: the restart request was not recorded", exc_info=True)
