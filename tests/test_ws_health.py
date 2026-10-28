"""Durable WebSocket health-record contract."""
from __future__ import annotations

import json
import stat
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
