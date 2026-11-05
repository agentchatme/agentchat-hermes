"""The invite ledger: which group invites were offered to the agent, and
whether a turn ran for each. Survives restarts.

A group invite is not on the tray and is never gated; it is a one-shot
decision the agent makes in its own turn (accept, decline, or leave it
pending by doing nothing). Two failure shapes need memory:

* **The turn could not start** (Hermes runtime unavailable, texts not loaded
  yet, agent construction failed). The invite must not be lost: it stays
  *offered* in the ledger and the invoker re-queues it on the next wake.
* **The gateway restarted.** The connect-time catch-up lists every invite
  still pending server-side — including ones the agent already looked at
  and deliberately left pending. Those are *started* in the ledger and are
  not run again, so a restart never costs one turn per pending invite.

The file is ``invites.json`` in the account's state directory
(:mod:`.state`). Entries expire after thirty days and the ledger is capped,
so it never grows without bound. With ``path=None`` the ledger is
in-memory only (tests, and light runtimes that never persist).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from .state import read_json, write_json_atomic
from .types import GroupInviteEvent

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_VERSION = 1
MAX_ENTRIES = 200
EXPIRE_AFTER_S = 30 * 24 * 60 * 60.0


class InviteLedger:
    """Offered and started invites, keyed by invite id. Thread-safe."""

    def __init__(self, path: Path | None, *, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # -- queries ----------------------------------------------------------------

    def is_known(self, invite_id: str) -> bool:
        with self._lock:
            return invite_id in self._entries

    def is_started(self, invite_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(invite_id)
            return bool(entry and entry.get("started_at") is not None)

    def pending(self) -> list[GroupInviteEvent]:
        """Invites offered but never started — the ones to retry on a wake."""
        with self._lock:
            rows = [e for e in self._entries.values() if e.get("started_at") is None]
        rows.sort(key=lambda e: float(e.get("offered_at") or 0.0))
        return [event for event in (_event_of(row) for row in rows) if event is not None]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- writes -----------------------------------------------------------------

    def offer(self, invite: GroupInviteEvent) -> bool:
        """Record an offered invite. ``True`` when it was not in the ledger."""
        with self._lock:
            if invite.invite_id in self._entries:
                return False
            self._entries[invite.invite_id] = {
                "invite_id": invite.invite_id,
                "group_id": invite.group_id,
                "group_name": invite.group_name,
                "inviter_handle": invite.inviter_handle,
                "received_at": invite.received_at.isoformat(),
                "offered_at": self._now(),
                "started_at": None,
            }
            self._prune_locked()
            self._save_locked()
        return True

    def mark_started(self, invite_id: str) -> None:
        with self._lock:
            entry = self._entries.get(invite_id)
            if entry is None:
                return
            entry["started_at"] = self._now()
            self._save_locked()

    def forget(self, invite_id: str) -> None:
        with self._lock:
            if self._entries.pop(invite_id, None) is not None:
                self._save_locked()

    # -- persistence ------------------------------------------------------------

    def _load(self) -> None:
        if self._path is None:
            return
        raw: Any = read_json(self._path)
        if not isinstance(raw, dict) or raw.get("version") != LEDGER_VERSION:
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        with self._lock:
            for invite_id, entry in entries.items():
                if isinstance(invite_id, str) and isinstance(entry, dict):
                    self._entries[invite_id] = dict(entry)
            self._prune_locked()

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            write_json_atomic(
                self._path, {"version": LEDGER_VERSION, "entries": self._entries}
            )
        except OSError as exc:
            # The in-memory ledger is the one in use; the file is for the
            # next start. Say so once per write, never raise into a turn.
            logger.warning("invites: could not write the ledger at %s: %s", self._path, exc)

    def _prune_locked(self) -> None:
        now = self._now()
        stale = [
            invite_id
            for invite_id, entry in self._entries.items()
            if now - float(entry.get("offered_at") or 0.0) > EXPIRE_AFTER_S
        ]
        for invite_id in stale:
            del self._entries[invite_id]
        if len(self._entries) > MAX_ENTRIES:
            oldest = sorted(
                self._entries.items(), key=lambda item: float(item[1].get("offered_at") or 0.0)
            )
            for invite_id, _entry in oldest[: len(self._entries) - MAX_ENTRIES]:
                del self._entries[invite_id]


def _event_of(entry: dict[str, Any]) -> GroupInviteEvent | None:
    invite_id = entry.get("invite_id")
    group_id = entry.get("group_id")
    if not isinstance(invite_id, str) or not isinstance(group_id, str):
        return None
    received_raw = entry.get("received_at")
    try:
        received_at = (
            datetime.fromisoformat(received_raw)
            if isinstance(received_raw, str)
            else datetime.now(timezone.utc)
        )
    except ValueError:
        received_at = datetime.now(timezone.utc)
    group_name = entry.get("group_name")
    inviter = entry.get("inviter_handle")
    return GroupInviteEvent(
        invite_id=invite_id,
        group_id=group_id,
        group_name=group_name if isinstance(group_name, str) and group_name else group_id,
        inviter_handle=inviter if isinstance(inviter, str) and inviter else "unknown",
        received_at=received_at,
    )
