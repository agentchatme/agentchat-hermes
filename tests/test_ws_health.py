"""Durable WebSocket health-record contract."""
from __future__ import annotations

import json
import stat
import sys
from typing import TYPE_CHECKING

from agentchatme_hermes.ws_health import (
    health_age_seconds,
    read_ws_health,
    write_ws_health,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_health_round_trip_is_atomic_shape_and_private(tmp_path: Path) -> None:
    target = tmp_path / "state" / "ws-health.json"
    written = write_ws_health(
        handle="alice",
        state="connected",
        restart_attempt=2,
        last_connected_at=900.0,
        path=target,
        now=1_000.0,
    )

    assert read_ws_health(target) == written
    if sys.platform != "win32":  # chmod bits are advisory on Windows
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.glob("*.tmp")) == []


def test_error_is_bounded_and_age_clamps_clock_skew(tmp_path: Path) -> None:
    target = tmp_path / "ws-health.json"
    write_ws_health(
        handle="alice",
        state="restart_wait",
        last_error="x" * 2_000,
        path=target,
        now=100.0,
    )
    health = read_ws_health(target)
    assert health is not None
    assert health["last_error"] == "x" * 500
    assert health_age_seconds(health, now=90.0) == 0.0
    assert health_age_seconds(health, now=130.0) == 30.0


def test_invalid_or_partial_health_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "ws-health.json"
    assert read_ws_health(target) is None

    target.write_text("not-json", encoding="utf-8")
    assert read_ws_health(target) is None

    target.write_text(json.dumps({"version": 1, "state": "connected"}), encoding="utf-8")
    assert read_ws_health(target) is None

    target.write_text(
        json.dumps(
            {
                "version": 999,
                "pid": 1,
                "handle": "alice",
                "state": "connected",
                "updated_at": 1,
                "last_connected_at": None,
                "restart_attempt": 0,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )
    assert read_ws_health(target) is None


def test_health_path_sits_beside_its_lock(tmp_path: Path) -> None:
    from agentchatme_hermes.ws_health import health_path_for

    lock = tmp_path / "agentchat-locks" / "ws-0123456789abcdef.lock"
    assert health_path_for(lock) == tmp_path / "agentchat-locks" / "ws-0123456789abcdef.health.json"


def test_record_carries_the_worker_section_and_old_records_still_read(tmp_path: Path) -> None:
    path = tmp_path / "ws.health.json"
    write_ws_health(handle="me", state="connected", path=path, worker={"state": "idle", "alive": True})
    health = read_ws_health(path)
    assert health is not None and health["worker"] == {"state": "idle", "alive": True}
    write_ws_health(handle="me", state="connected", path=path)
    old = read_ws_health(path)
    assert old is not None and "worker" not in old


def test_doctor_line_for_the_worker() -> None:
    from agentchatme_hermes.cli import _describe_worker

    now = 10_000.0
    assert _describe_worker(None, now=now) is None, "a record from before the worker section"
    level, text = _describe_worker({"state": "idle", "alive": True, "heartbeat_at": now - 5, "since": now - 50, "restarts": 0, "hung_turns": 0, "last_turn": {"status": "completed", "agentchat_calls": {"made": 3, "failed": 0}}}, now=now)  # type: ignore[misc]
    assert level == "ok" and "last turn completed with 3 AgentChat call(s)" in text
    assert _describe_worker({"state": "idle", "alive": False}, now=now)[0] == "fail"  # type: ignore[index]
    assert _describe_worker({"state": "restart_wait", "alive": True, "last_error": "boom", "restarts": 2}, now=now)[0] == "warn"  # type: ignore[index]
    stale = _describe_worker({"state": "cycle", "alive": True, "heartbeat_at": now - 400}, now=now)
    assert stale is not None and stale[0] == "fail" and "has not moved" in stale[1]
    hung = _describe_worker({"state": "idle", "alive": True, "heartbeat_at": now, "hung_turns": 1}, now=now)
    assert hung is not None and hung[0] == "warn" and "overran" in hung[1]
    long_turn = _describe_worker({"state": "turn", "alive": True, "heartbeat_at": now - 1, "since": now - 1000}, now=now)
    assert long_turn is not None and long_turn[0] == "warn" and "1000s" in long_turn[1]
