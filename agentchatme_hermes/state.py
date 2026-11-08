"""Per-account state on disk: what the integration must remember across restarts.

One small JSON file lives here today: the last served texts
(``texts-hermes.json``, written by the SDK kit's ``TextsCache``), so a
gateway that starts while ``GET /v1/texts`` is unreachable still has the
wake instruction, the invite prompt and the identity line it used last
time. Releases before contract v1 also kept ``texts.json`` and an invite
ledger, ``invites.json``; the runtime removes both.

The directory is per (Hermes root, account): ``<hermes root>/agentchat-state/
<fingerprint>/``, beside ``agentchat-locks/`` and for the same reason — two
profiles that share one key share one account, and the texts carry that
account's handle. ``AGENTCHATME_STATE_DIR`` overrides the base directory for
read-only homes, the way ``AGENTCHATME_LOCK_DIR`` does for the locks.

Writes are atomic (temp file + ``os.replace``) with mode ``0600``; a crash
mid-write leaves the previous file intact. Reads never raise: a missing or
corrupt file reads as "nothing stored".
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .profile_scope import account_fingerprint, hermes_root_of

STATE_DIRNAME = "agentchat-state"
# Written by releases before contract v1 (the invite ledger); removed on start.
LEGACY_INVITES_FILENAME = "invites.json"


def state_dir_for(
    *,
    hermes_home: Path,
    account_fingerprint: str,
    state_dir: Path | None = None,
) -> Path:
    """The state directory for one account on this machine.

    ``<hermes root>/agentchat-state/<fingerprint>`` by default, where the
    root is the directory that owns every profile. ``state_dir`` replaces
    the base directory (``AGENTCHATME_STATE_DIR``).
    """
    base = state_dir if state_dir is not None else hermes_root_of(hermes_home) / STATE_DIRNAME
    return base / account_fingerprint


def state_dir_for_account(
    *,
    hermes_home: Path,
    api_base: str,
    api_key: str,
    state_dir: Path | None = None,
) -> Path:
    """:func:`state_dir_for` from the raw account facts (wizard and CLI, which
    run before a :class:`~agentchatme_hermes.config.Config` exists)."""
    return state_dir_for(
        hermes_home=hermes_home,
        account_fingerprint=account_fingerprint(api_base, api_key),
        state_dir=state_dir,
    )


def write_json_atomic(path: Path, record: Any) -> None:
    """Replace ``path`` with ``record`` as JSON, atomically, mode ``0600``.

    Raises ``OSError`` when the directory cannot be written; callers decide
    whether that is fatal (it never is for state — the in-memory copy is
    the one in use, the file is for the next start).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp: Path | None = None
    try:
        # Exclusive creation: no stale-temp collisions after a crash and no
        # predictable-name symlink attacks.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp = Path(file.name)
            os.chmod(temp, 0o600)
            json.dump(record, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp is not None:
            with suppress(FileNotFoundError):
                temp.unlink()


def read_json(path: Path) -> Any:
    """The decoded JSON at ``path``, or ``None`` when absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
