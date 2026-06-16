from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILENAME = "agentchat-closed-threads.json"


@dataclass(frozen=True)
class ClosedThreadRecord:
    conversation_id: str
    closed_at: str
    reason: str | None = None


def resolve_thread_closures_path() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        return Path.home() / ".hermes" / _STATE_FILENAME

    hermes_home: Path = get_hermes_home()
    return hermes_home / _STATE_FILENAME


class ThreadClosures:
    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path if path is not None else resolve_thread_closures_path()
        self._lock = threading.Lock()
        self._closed: dict[str, ClosedThreadRecord] = {}
        self._load()

    def is_closed(self, conversation_id: str) -> bool:
        with self._lock:
            return conversation_id in self._closed

    def close(self, conversation_id: str, *, reason: str | None = None) -> ClosedThreadRecord:
        record = ClosedThreadRecord(
            conversation_id=conversation_id,
            closed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            reason=reason,
        )
        with self._lock:
            self._closed[conversation_id] = record
            self._save_locked()
        logger.info(
            "ThreadClosures: locally closed conversation %s reason=%s",
            conversation_id,
            reason or "<none>",
        )
        return record

    def reopen(self, conversation_id: str) -> bool:
        with self._lock:
            existed = self._closed.pop(conversation_id, None) is not None
            if existed:
                self._save_locked()
        if existed:
            logger.info("ThreadClosures: reopened conversation %s", conversation_id)
        return existed

    def list_closed(self) -> list[ClosedThreadRecord]:
        with self._lock:
            return sorted(
                self._closed.values(),
                key=lambda item: item.closed_at,
                reverse=True,
            )

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "ThreadClosures: could not read %s; starting with empty state",
                self._path,
                exc_info=True,
            )
            return

        if not isinstance(raw, dict):
            logger.warning(
                "ThreadClosures: unexpected top-level shape in %s; ignoring",
                self._path,
            )
            return

        loaded: dict[str, ClosedThreadRecord] = {}
        for conversation_id, payload in raw.items():
            if not isinstance(conversation_id, str) or not isinstance(payload, dict):
                continue
            closed_at = payload.get("closed_at")
            reason = payload.get("reason")
            if not isinstance(closed_at, str):
                continue
            if reason is not None and not isinstance(reason, str):
                reason = None
            loaded[conversation_id] = ClosedThreadRecord(
                conversation_id=conversation_id,
                closed_at=closed_at,
                reason=reason,
            )
        self._closed = loaded

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            conversation_id: {
                "closed_at": record.closed_at,
                **({"reason": record.reason} if record.reason else {}),
            }
            for conversation_id, record in self._closed.items()
        }
        tmp = self._path.with_name(f"{self._path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
