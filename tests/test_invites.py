"""Tests for ``agentchatme_hermes.invites`` — the persisted invite ledger."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentchatme_hermes import invites as invites_module
from agentchatme_hermes.invites import InviteLedger
from agentchatme_hermes.types import GroupInviteEvent

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _invite(invite_id: str = "ginv_1", group_id: str = "grp_1") -> GroupInviteEvent:
    return GroupInviteEvent(
        invite_id=invite_id,
        group_id=group_id,
        group_name="Council",
        inviter_handle="carol",
        received_at=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
    )


class TestLedger:
    def test_offer_then_start(self, tmp_path: Path) -> None:
        ledger = InviteLedger(tmp_path / "invites.json")
        assert ledger.offer(_invite()) is True
        assert ledger.offer(_invite()) is False, "already known"
        assert ledger.is_known("ginv_1") and not ledger.is_started("ginv_1")
        assert [i.invite_id for i in ledger.pending()] == ["ginv_1"]
        ledger.mark_started("ginv_1")
        assert ledger.is_started("ginv_1")
        assert ledger.pending() == []

    def test_survives_a_restart(
        self, tmp_path: Path, assert_private_file: Callable[[Path], None]
    ) -> None:
        path = tmp_path / "invites.json"
        first = InviteLedger(path)
        first.offer(_invite("ginv_started"))
        first.mark_started("ginv_started")
        first.offer(_invite("ginv_pending", group_id="grp_2"))
        assert_private_file(path)

        second = InviteLedger(path)
        assert second.is_started("ginv_started")
        pending = second.pending()
        assert [i.invite_id for i in pending] == ["ginv_pending"]
        assert pending[0].group_id == "grp_2"
        assert pending[0].group_name == "Council"
        assert pending[0].inviter_handle == "carol"
        assert pending[0].received_at == _invite().received_at

    def test_in_memory_without_a_path(self) -> None:
        ledger = InviteLedger(None)
        ledger.offer(_invite())
        assert len(ledger) == 1
        ledger.forget("ginv_1")
        assert len(ledger) == 0

    def test_corrupt_file_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "invites.json"
        path.write_text("nope", encoding="utf-8")
        assert len(InviteLedger(path)) == 0

    def test_expired_entries_are_pruned(self, tmp_path: Path) -> None:
        clock = {"now": 1_000_000.0}
        ledger = InviteLedger(tmp_path / "invites.json", now=lambda: clock["now"])
        ledger.offer(_invite("old"))
        clock["now"] += invites_module.EXPIRE_AFTER_S + 1
        ledger.offer(_invite("new"))
        assert not ledger.is_known("old") and ledger.is_known("new")

    def test_ledger_is_capped(self, tmp_path: Path, monkeypatch: object) -> None:
        clock = {"now": 0.0}
        ledger = InviteLedger(tmp_path / "invites.json", now=lambda: clock["now"])
        for index in range(invites_module.MAX_ENTRIES + 5):
            clock["now"] += 1.0
            ledger.offer(_invite(f"ginv_{index}"))
        assert len(ledger) == invites_module.MAX_ENTRIES
        assert not ledger.is_known("ginv_0"), "oldest dropped first"
        assert ledger.is_known(f"ginv_{invites_module.MAX_ENTRIES + 4}")
