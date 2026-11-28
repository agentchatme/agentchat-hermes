#!/usr/bin/env python3
"""The probes of the native-Windows check (.github/workflows/windows-native.yml).

Run with a Python that has this plugin installed (``pip install .``), never
Hermes' own: the probes are a second process beside the gateway, as
``hermes agentchat doctor`` is.

    windows-check.py stub <port> <request log>
        A stand-in for the AgentChat API on 127.0.0.1, for the leg that runs
        without a key: ``GET /v1/agents/me`` answers a handle (so ``login``
        and the runtime's start succeed), everything else 404, the socket
        upgrade included. Each request's method and path (never a header) is
        appended to the log. Proves the gateway's plugin takes the leader
        lock and its receive loop starts and tries the socket; it cannot
        prove a connect.

    windows-check.py leader <hermes home> <timeout s> [connected]
        Waits for this account's health record beside its leader lock, written
        by a live process (the gateway), then checks from here that the lock
        is held: the probe reads "held" and an acquire from this process is
        refused. With ``connected``, also waits for the record's state to be
        ``connected``. Prints the record (no key is in it).

    windows-check.py free <hermes home> <timeout s>
        After the gateway stopped or was killed: the lock is free again (the
        OS released it with the process), taken and given back from here.

    windows-check.py write-refused <home> <text>
        Saving a made-up key into ``<home>`` must be refused with an error
        that contains ``<text>`` (a key directory of another account).

    windows-check.py read-detail <home> <text>
        Reading ``<home>``'s key file must not be usable, and what it says
        must contain ``<text>`` (the message a person sees).

Exit 0 when the check holds, 1 when it does not.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _stub(port: int, log: Path) -> int:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            with log.open("a", encoding="utf-8") as out:
                out.write(f"{self.command} {self.path}\n")
            if self.command == "GET" and self.path.split("?", 1)[0] == "/v1/agents/me":
                status, body = 200, {"handle": "windows-check", "display_name": "Windows check"}
            else:
                status, body = 404, {"code": "NOT_FOUND", "message": "not served by the Windows check stub"}
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()
    return 0


def _records(home: Path) -> list[Path]:
    return sorted((home / "agentchat-locks").glob("ws-*.health.json"))


def _lock_of(record: Path) -> Path:
    return record.with_name(record.name.replace(".health.json", ".lock"))


def _leader(home: Path, timeout_s: float, want_connected: bool) -> int:
    from agentchatme_hermes.leader_lock import (
        describe_lock_holder,
        release_leader_lock,
        try_acquire_ws_leader_lock,
    )
    from agentchatme_hermes.ws_health import pid_alive, read_ws_health

    deadline = time.monotonic() + timeout_s
    seen: dict[str, object] | None = None
    while time.monotonic() < deadline:
        for record in _records(home):
            health = read_ws_health(record)
            if health is None or pid_alive(health["pid"]) is not True:
                continue
            seen = dict(health)
            if want_connected and health["state"] != "connected":
                continue
            lock = _lock_of(record)
            state = describe_lock_holder(lock)
            fd = try_acquire_ws_leader_lock(lock)
            if fd is not None:
                release_leader_lock(fd)
            print(json.dumps({"lock": str(lock), "probe": state, "acquired_here": fd is not None, "health": seen}, indent=2))
            if state != "held" or fd is not None:
                print(f"::error::the gateway's leader lock is not held against another process ({state})")
                return 1
            return 0
        time.sleep(2)
    print(json.dumps({"last_record_seen": seen}, indent=2))
    want = "a connected health record" if want_connected else "a health record from a live gateway"
    print(f"::error::no {want} under {home / 'agentchat-locks'} within {timeout_s:.0f}s")
    return 1


def _free(home: Path, timeout_s: float) -> int:
    from agentchatme_hermes.leader_lock import (
        describe_lock_holder,
        release_leader_lock,
        try_acquire_ws_leader_lock,
    )

    records = _records(home)
    if not records:
        print(f"::error::no health record under {home / 'agentchat-locks'}")
        return 1
    deadline = time.monotonic() + timeout_s
    for record in records:
        lock = _lock_of(record)
        while True:
            fd = try_acquire_ws_leader_lock(lock)
            if fd is not None:
                release_leader_lock(fd)
                print(f"{lock}: free, taken and released by this process; probe now {describe_lock_holder(lock)}")
                break
            if time.monotonic() >= deadline:
                print(f"::error::{lock} still held {timeout_s:.0f}s after the gateway ended")
                return 1
            time.sleep(0.5)
    return 0


def _write_refused(home: Path, text: str) -> int:
    import secrets

    from agentchatme_hermes import key_store

    try:
        key_store.write_store(home, api_key="ac_" + secrets.token_urlsafe(32), handle="windows-check", api_base="http://127.0.0.1")
    except key_store.KeyStoreError as exc:
        print(f"refused: {exc}")
        if text in str(exc):
            return 0
        print(f"::error::refused, but not with {text!r}")
        return 1
    print(f"::error::a key was saved into {home}")
    return 1


def _read_detail(home: Path, text: str) -> int:
    from agentchatme_hermes import key_store

    read = key_store.read_store(home)
    print(f"status={read.status} detail={read.detail}")
    if read.usable or text not in read.detail:
        print(f"::error::expected an unusable key file saying {text!r}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "stub":
        return _stub(int(argv[2]), Path(argv[3]))
    if len(argv) in (4, 5) and argv[1] == "leader":
        return _leader(Path(argv[2]), float(argv[3]), want_connected=argv[4:] == ["connected"])
    if len(argv) == 4 and argv[1] == "free":
        return _free(Path(argv[2]), float(argv[3]))
    if len(argv) == 4 and argv[1] == "write-refused":
        return _write_refused(Path(argv[2]), argv[3])
    if len(argv) == 4 and argv[1] == "read-detail":
        return _read_detail(Path(argv[2]), argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
