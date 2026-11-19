"""Durable WebSocket health-record contract."""
from __future__ import annotations

import json
import stat
import sys
import time
from typing import TYPE_CHECKING

from agentchatme_hermes.ws_health import (
    health_age_seconds,
    health_path_for,
    read_ws_health,
    write_ws_health,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


def test_doctor_line_for_a_stuck_cap_and_the_loop() -> None:
    from agentchatme_hermes.cli import _describe_worker
    from agentchatme_hermes.ws_health import bad_cycle_run

    now = 10_000.0
    base: dict[str, object] = {"state": "idle", "alive": True, "heartbeat_at": now - 1, "max_hung_turns": 2}
    capped = _describe_worker({**base, "hung_turns": 2}, now=now)
    assert capped is not None and capped[0] == "fail" and "no new turn" in capped[1], "no turn can start: an error"

    def cycles(*outcomes: str) -> dict[str, object]:
        recent = [{"at": "t", "outcome": o, "failure": "why" if o != "decided" else None} for o in outcomes]
        return {**base, "hung_turns": 0, "loop": {"recent_cycles": recent}}

    for bad in ("fail_closed", "tools_unavailable", "texts_missing", "abandoned", "tray_failed"):
        line = _describe_worker(cycles("decided", bad, bad, bad), now=now)
        assert line is not None and line[0] == "fail" and bad in line[1], bad
    one = _describe_worker(cycles("decided", "fail_closed"), now=now)
    assert one is not None and one[0] == "warn"
    healed = _describe_worker(cycles("fail_closed", "fail_closed", "fail_closed", "empty"), now=now)
    assert healed is not None and healed[0] == "ok", "a good cycle ends the run"
    assert bad_cycle_run([{"outcome": "abandoned"}, {"outcome": "turns_started"}]) == (0, None)
    assert bad_cycle_run(None) == (0, None)


def test_doctor_lines_for_could_not_process() -> None:
    from agentchatme_hermes.cli import _could_not_process_summary, _describe_could_not_process
    from agentchatme_hermes.ws_health import pause_end

    now = 1_800_000_000.0
    assert _describe_could_not_process(None, now=now) == []
    assert _describe_could_not_process({"loop": {}}, now=now) == [], "a record from before could not process"

    def worker(**section: object) -> dict[str, object]:
        return {"loop": {"could_not_process": section}}

    assert _describe_could_not_process(worker(failures_reported=0, paused=[]), now=now) == [], "nothing to say"
    counts = worker(
        failures_reported=2, failure_actions={"retry": 1, "paused": 1}, reruns=1,
        runtime_checks_ok=1, failures_waiting=0, paused=[
            {"conversation_id": "conv_bad", "until": "2027-02-01T08:00:00.000Z"},
            {"conversation_id": "conv_old", "until": "2020-01-01T00:00:00.000Z"},
        ],
    )
    assert _describe_could_not_process(counts, now=now) == [
        ("ok", "Could not process (since the receive loop started): 2 reported (retry 1, paused 1), 1 re-run, runtime checks 1 ok"),
        ("warn", "Conversation conv_bad paused until 2027-02-01T08:00:00.000Z (could not process; a 24 h mute that ends on its own)"),
    ]
    trouble = worker(failure_report_errors=1, runtime_checks_failed=2, splits_stopped=1)
    assert _describe_could_not_process(trouble, now=now) == [
        ("warn", "Could not process (since the receive loop started): 1 report error, runtime checks 2 failed, gate split stopped 1 time"),
    ]
    assert _could_not_process_summary({}) == "nothing reported"
    assert pause_end("2027-01-15T08:00:00.000Z") == pause_end("2027-01-15T08:00:00+00:00")
    assert pause_end("not a time") is None and pause_end(None) is None and pause_end("2027-01-15T08:00:00") is None


def test_status_shows_could_not_process_from_this_accounts_health_record(capsys: pytest.CaptureFixture[str]) -> None:
    """``hermes agentchat status`` reads the running gateway's record for
    this account (beside its leader lock) and prints the counts and the
    paused conversations; no record, a stale one, or one without the section
    prints nothing more."""
    from fake_keys import api_key

    from agentchatme_hermes import cli
    from agentchatme_hermes.leader_lock import lock_path_for
    from agentchatme_hermes.profile_scope import account_fingerprint, resolve_hermes_home

    base, key = "https://api.example.test", api_key("status")
    assert cli._health_worker(base, key) is None, "no gateway has written a record"
    lock = lock_path_for(hermes_home=resolve_hermes_home(), account_fingerprint=account_fingerprint(base, key))
    worker = {
        "state": "idle",
        "loop": {"could_not_process": {
            "failures_reported": 1, "failure_actions": {"paused": 1},
            "paused": [{"conversation_id": "conv_bad", "until": "2099-01-01T00:00:00.000Z"}],
        }},
    }
    write_ws_health(handle="me", state="connected", path=health_path_for(lock), worker=worker)
    found = cli._health_worker(base, key)
    assert found == worker
    cli._print_status({"handle": "me", "status": "active"}, worker=found)
    out = capsys.readouterr().out
    assert "  Could not process: 1 reported (paused 1)" in out
    assert "  Paused chat:   conv_bad paused until 2099-01-01T00:00:00.000Z (could not process; a 24 h mute that ends on its own)" in out
    cli._print_status({"handle": "me", "status": "active"}, worker={"state": "idle", "loop": {}})
    assert "Could not process" not in capsys.readouterr().out, "a record from an older release"
    write_ws_health(handle="me", state="connected", path=health_path_for(lock), worker=worker, now=time.time() - 3600)
    assert cli._health_worker(base, key) is None, "a stale record is not this gateway's word"
