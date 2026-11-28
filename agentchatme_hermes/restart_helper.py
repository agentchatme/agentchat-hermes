"""The detached helper behind :mod:`.gateway_restart`: restart a gateway, then make sure one is up.

Run as its own process, in its own session (``python -I restart_helper.py ...``), so it outlives
the gateway it restarts. Standard library only: it runs outside Hermes' plugin loader, with
Hermes' Python. Never imported by the plugin (nothing here runs in the gateway).

1. Wait ``--delay`` seconds: Hermes reacts to the allow that triggered this (it reloads the
   chat's MCP connections); a restart that starts during that reload shows the person a failed
   reload (lab, 2026-11-28).
2. Send SIGUSR1 to ``--pid``: Hermes' documented graceful restart (``systemctl reload
   hermes-gateway`` and ``hermes gateway restart`` send it). The gateway waits until no work is
   in flight, then exits for its supervisor to relaunch.
3. Wait for that process to end. A zombie its parent has not reaped yet counts as ended: a
   gateway a chat started is the chat's child, and the chat reaps it only on its next message.
4. Give a supervisor ``--relaunch-wait`` seconds (systemd and launchd relaunch the service;
   under systemd this helper is in the service's cgroup and ends with the old gateway anyway).
5. Run ``hermes gateway ensure`` (the command the desktop app runs): ready at once when a
   gateway is up, else it starts one. Exit 6 (draining or update-paused) is retried.

Every step is one line in ``--log``; nothing else is written.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

GONE_WAIT_S = 3600.0
POLL_S = 0.5
ENSURE_TIMEOUT_S = 90
ENSURE_RETRIES = 3


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} {message}", flush=True)


def ended(pid: int) -> bool:
    """Has ``pid`` exited (a zombie counts)?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    stat = f"/proc/{pid}/stat"
    if os.path.exists(stat):
        try:
            with open(stat, encoding="utf-8") as handle:
                return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False
    try:  # macOS and other POSIX systems without /proc
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    state = out.stdout.strip()
    return not state or state.startswith("Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--relaunch-wait", type=float, default=20.0)
    parser.add_argument("hermes", nargs=argparse.REMAINDER, help="-- then the hermes command to run ensure with")
    args = parser.parse_args(argv)
    hermes = [part for part in args.hermes if part != "--"]
    if not hermes:
        _log("no hermes command given")
        return 2

    time.sleep(max(0.0, args.delay))
    try:
        os.kill(args.pid, signal.SIGUSR1)
    except ProcessLookupError:
        _log(f"gateway {args.pid} already gone")
    except OSError as exc:
        _log(f"could not signal gateway {args.pid}: {exc}")
        return 1
    else:
        _log(f"asked gateway {args.pid} for its graceful restart (SIGUSR1)")

    start = time.monotonic()
    while not ended(args.pid):
        if time.monotonic() - start > GONE_WAIT_S:
            _log(f"gateway {args.pid} still running after {GONE_WAIT_S:.0f} s: left as it is")
            return 1
        time.sleep(POLL_S)
    _log(f"gateway {args.pid} ended after {time.monotonic() - start:.1f} s")

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
            return 1
        _log(f"gateway ensure exit {result.returncode}: {result.stdout.strip()[-300:]}")
        if result.returncode == 0:
            return 0
        if result.returncode != 6:
            return 1
        time.sleep(10.0 * attempt)
    return 1


if __name__ == "__main__":
    sys.exit(main())
