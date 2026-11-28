"""The detached helper behind :mod:`.gateway_restart`: restart a gateway, then make sure one is up.

Run as its own process, in its own session (``python -I restart_helper.py ...``), so it outlives
the gateway it restarts. Standard library only: it runs outside Hermes' plugin loader, with
Hermes' Python. Never imported by the plugin (nothing here runs in the gateway).

1. Note the target (its start-time fingerprint, so a reused pid is told apart, and whether a chat
   started it: ``--idle-exit`` on its command line).
2. Wait ``--delay`` seconds: Hermes reacts to the allow that triggered this (it reloads the
   chat's MCP connections); a restart that starts during that reload shows the person a failed
   reload (lab, 2026-11-28).
3. Send SIGUSR1: Hermes' documented graceful restart (``systemctl reload hermes-gateway`` and
   ``hermes gateway restart`` send it). The gateway waits until no work is in flight, with no
   limit (a long background job holds it), then exits for its supervisor to relaunch.
4. Wait, for as long as that same process lives, for it to end. A zombie its parent has not
   reaped yet counts as ended: a gateway a chat started is the chat's child.
5. A chat-started gateway (``--idle-exit``) is left to the chat: its next message starts the next
   one, as before (lab, 2026-11-28), and nothing outlives the chat that the person did not have.
   Otherwise give a supervisor ``--relaunch-wait`` seconds (systemd and launchd relaunch the
   service; under systemd this helper is in the service's cgroup and ends with the old gateway),
   then run ``hermes gateway ensure`` (what the desktop app runs): ready at once when a gateway is
   up, else it starts one. Exit 6 (draining or update-paused) is retried.

``--status`` is a JSON file each step rewrites (``state``: signalled, waiting, ended, left_to_chat,
ensured or failed, with ``detail``): what doctor, setup and the reminder read. One line per step
goes to stdout (the plugin's log file).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

FAST_POLL_S = 0.5
FAST_FOR_S = 3600.0
SLOW_POLL_S = 30.0
ENSURE_TIMEOUT_S = 90
ENSURE_RETRIES = 3


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} {message}", flush=True)


def _status(path: str | None, pid: int, state: str, detail: str = "") -> None:
    """Rewrite the status record atomically (mode 0600); never raises."""
    if not path:
        return
    record = {"pid": pid, "state": state, "detail": detail, "at": time.time()}
    directory = os.path.dirname(path) or "."
    try:
        fd, temp = tempfile.mkstemp(prefix=".restart-status.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        pass


def _ps(pid: int, field: str) -> str:
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def fingerprint(pid: int) -> str:
    """When ``pid`` started (``/proc`` start ticks, else ``ps`` lstart); ``""`` when unknown."""
    stat = f"/proc/{pid}/stat"
    if os.path.exists(stat):
        try:
            with open(stat, encoding="utf-8") as handle:
                return handle.read().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return ""
    return _ps(pid, "lstart")


def started_by_a_chat(pid: int) -> bool:
    """Does ``pid``'s command line carry ``--idle-exit`` (a gateway a chat or the TUI started)?"""
    cmdline = f"/proc/{pid}/cmdline"
    if os.path.exists(cmdline):
        try:
            with open(cmdline, "rb") as handle:
                return b"--idle-exit" in handle.read().split(b"\0")
        except OSError:
            return False
    return "--idle-exit" in _ps(pid, "command").split()


def ended(pid: int, started: str = "") -> bool:
    """Has the process ``started`` names exited (a zombie counts; another process on that pid too)?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if started and fingerprint(pid) not in ("", started):
        return True  # the pid was reused
    stat = f"/proc/{pid}/stat"
    if os.path.exists(stat):
        try:
            with open(stat, encoding="utf-8") as handle:
                return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False
    state = _ps(pid, "stat")
    return not state or state.startswith("Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--relaunch-wait", type=float, default=20.0)
    parser.add_argument("--status", default=None)
    parser.add_argument("hermes", nargs=argparse.REMAINDER, help="-- then the hermes command to run ensure with")
    args = parser.parse_args(argv)
    hermes = [part for part in args.hermes if part != "--"]
    pid = args.pid
    if not hermes:
        _log("no hermes command given")
        _status(args.status, pid, "failed", "no hermes command given")
        return 2

    started = fingerprint(pid)
    chat_started = started_by_a_chat(pid)
    time.sleep(max(0.0, args.delay))
    if ended(pid, started):
        _log(f"gateway {pid} already ended")
    else:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError as exc:
            _log(f"could not signal gateway {pid}: {exc}")
            _status(args.status, pid, "failed", f"could not signal the gateway: {exc}")
            return 1
        _log(f"asked gateway {pid} for its graceful restart (SIGUSR1)")
        _status(args.status, pid, "signalled")

    begin = time.monotonic()
    waiting_said = False
    while not ended(pid, started):
        waited = time.monotonic() - begin
        if waited > FAST_FOR_S and not waiting_said:
            _log(f"gateway {pid} still finishing its work after {waited:.0f} s: waiting as long as it does")
            _status(args.status, pid, "waiting", "the gateway is still finishing its work")
            waiting_said = True
        time.sleep(FAST_POLL_S if waited <= FAST_FOR_S else SLOW_POLL_S)
    _log(f"gateway {pid} ended after {time.monotonic() - begin:.1f} s")
    _status(args.status, pid, "ended")

    if chat_started:
        _log("a chat started that gateway: the chat starts the next one with its next message")
        _status(args.status, pid, "left_to_chat")
        return 0

    time.sleep(max(0.0, args.relaunch_wait))
    for attempt in range(1, ENSURE_RETRIES + 1):
        try:
            result = subprocess.run(
                [*hermes, "gateway", "ensure", "--timeout", str(ENSURE_TIMEOUT_S)],
                capture_output=True,
                text=True,
                timeout=ENSURE_TIMEOUT_S + 30,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log(f"gateway ensure could not run: {exc}")
            _status(args.status, pid, "failed", f"`hermes gateway ensure` could not run: {exc}")
            return 1
        _log(f"gateway ensure exit {result.returncode}: {result.stdout.strip()[-300:]}")
        if result.returncode == 0:
            _status(args.status, pid, "ensured")
            return 0
        if result.returncode != 6 or attempt == ENSURE_RETRIES:
            break
        time.sleep(10.0 * attempt)
    _status(args.status, pid, "failed", f"`hermes gateway ensure` exited {result.returncode}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
