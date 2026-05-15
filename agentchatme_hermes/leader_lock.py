"""Singleton lock for the AgentChat WebSocket leader role.

A Hermes machine may host more than one process that has our plugin
loaded — the long-lived ``hermes gateway`` is one, but the TUI/REPL
(``hermes`` with no subcommand), one-shot agents (``hermes oneshot``),
and any future Hermes-spawned child Python that calls
``discover_plugins()`` will also reach our :func:`register`.

The first cut of 0.2.0 used ``ctx._manager._cli_ref is None`` as the
gateway marker. That turned out to be wrong: ``_cli_ref`` is set by
``HermesCLI.__init__`` *after* plugin discovery, so at the moment
``register()`` runs it's ``None`` in **every** process. The detector
silently misclassified the TUI as gateway, and we ended up with two
WebSockets — both receiving the same ``message.new`` and both racing
to drive a turn for the same conversation.

This module is the production-grade fix. Two layered signals decide
whether *this* process should own the live WebSocket:

1. **Hermes-canonical env marker** — ``gateway/run.py:370`` sets
   ``os.environ["_HERMES_GATEWAY"] = "1"`` at gateway-process module
   load, before any plugin discovery. Hermes' own ``cli.py:538``
   uses exactly this check internally. It's the authoritative
   "this process is the gateway." A non-set env value means TUI,
   one-shot, or cron-spawned Python — none of which should host
   a live inbound stream.

2. **OS-enforced singleton** — ``fcntl.flock(LOCK_EX | LOCK_NB)`` on
   ``~/.hermes/agentchat-ws.lock``. Even if two ``_HERMES_GATEWAY=1``
   processes ever coexist (concurrent restart, operator running a
   second ``hermes gateway run --replace`` while the first is still
   draining), only one acquires the lock. The other detects the
   condition, logs loudly, and falls back to CLI-mode runtime —
   the daemon/invoker subsystems stay dark in the loser, so we get
   exactly one WS per machine for any inbound delivery.

The lock file descriptor is held open for the process lifetime. POSIX
``flock`` is released automatically when the fd closes (process exit,
SIGKILL, or explicit :func:`release_leader_lock`). No stale lock
files survive a crash; no special cleanup hook is needed.

POSIX-only by design. ``fcntl`` is Unix-only — and the only Hermes
deployment targets are Linux and macOS, so this matches reality.
We don't import ``fcntl`` at module level so that **Windows dev
machines** can still import the package for tests / type-checks /
local exploration. On Windows the acquire path returns ``None``
(no leader) and the runtime falls back to follower mode — safe
because no production gateway ever runs on Windows.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Module-level import is purely for the type-checker — gives mypy
    # the ``flock``/``LOCK_*`` symbols without running the real import
    # on Windows. Runtime callsites still lazy-import below.
    import fcntl  # noqa: F401

logger = logging.getLogger(__name__)

_IS_POSIX = sys.platform != "win32"

GATEWAY_ENV_MARKER = "_HERMES_GATEWAY"
GATEWAY_ENV_VALUE = "1"


def is_hermes_gateway_process() -> bool:
    """Return ``True`` iff this process is the long-lived Hermes gateway.

    Checks ``_HERMES_GATEWAY=1`` in the environment — the marker set
    inside :mod:`gateway.run` at module import. Reliable across every
    Hermes process class because:

    * Gateway sets it before plugin discovery.
    * TUI / one-shot / named CLI subcommands never set it.
    * Subprocesses spawned by the gateway DO inherit it — those are
      then filtered by the file-lock layer (see
      :func:`try_acquire_ws_leader_lock`), which they will fail to
      acquire because the parent already holds it.
    """
    return os.environ.get(GATEWAY_ENV_MARKER) == GATEWAY_ENV_VALUE


def default_lock_path() -> Path:
    """Resolve the canonical lock-file path for the current Hermes home.

    ``~/.hermes/agentchat-ws.lock``. Per-user by construction — gateway
    runs as ``root`` → ``/root/.hermes/agentchat-ws.lock``; a user-side
    Hermes install lives in their own home dir. No cross-user collisions.

    The directory must already exist (Hermes creates ``~/.hermes`` on
    first launch). We don't create it here — if it doesn't exist, that's
    a deeper Hermes-install problem and we'd rather surface the OSError
    than mask it.
    """
    return Path.home() / ".hermes" / "agentchat-ws.lock"


def try_acquire_ws_leader_lock(path: Path | None = None) -> int | None:
    """Attempt to grab the singleton WS leader lock.

    Returns the file descriptor on success (the caller MUST keep this
    open for the process lifetime), or ``None`` if another process
    already holds it.

    Implementation notes:

    * ``fcntl.flock(LOCK_EX | LOCK_NB)`` is atomic and non-blocking.
      Two callers racing for the lock cannot both succeed — POSIX
      guarantees one wins, the other gets ``BlockingIOError``.
    * We deliberately do NOT write the PID into the file. The lock
      semantics are tied to the fd, not to file content; PID-tracking
      would be a separate concern (and the kernel's flock state is
      observable via ``lslocks`` for operators who need it).
    * We do not delete the lock file on release. Lock files are
      stateless markers — leaving the file around lets a re-acquire
      succeed without recreating the inode (cheaper and avoids a
      race where two processes both create-and-lock the file
      simultaneously). Stale files are harmless: they only carry
      "lock currently held by some process" semantics WHILE that
      process holds the fcntl lock.
    """
    try:
        import fcntl
    except ImportError:
        # Windows / non-POSIX: no leader election possible. Caller
        # treats this as "lost the race" and runs in follower mode.
        logger.warning(
            "leader_lock: fcntl unavailable on this platform — "
            "running in follower mode (no WS in this process). "
            "Production gateways must run on POSIX."
        )
        return None

    target = path or default_lock_path()
    try:
        # Open RW + create-if-missing. Mode 0600 so the API key
        # adjacent in ~/.hermes isn't loosened by lock-file mode bits.
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        logger.exception(
            "leader_lock: cannot open %s — falling back to CLI mode "
            "(no WS in this process)",
            target,
        )
        return None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except BlockingIOError:
        os.close(fd)
        logger.info(
            "leader_lock: %s already held by another process — this "
            "process will run in CLI mode (no WS, no invoker)",
            target,
        )
        return None
    except OSError:
        os.close(fd)
        logger.exception(
            "leader_lock: flock failed on %s — falling back to CLI mode",
            target,
        )
        return None

    return fd


def release_leader_lock(fd: int) -> None:
    """Release a previously-acquired leader lock.

    Idempotent on the fd: a second call after the fd is closed will
    raise ``OSError`` which we swallow at DEBUG. Production callers
    typically don't need to call this explicitly — the kernel
    releases the lock on process exit — but it's exposed for clean
    test teardown and for the in-process stop()/start() cycle.
    """
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except (ImportError, OSError):
        logger.debug("leader_lock: LOCK_UN raised", exc_info=True)
    try:
        os.close(fd)
    except OSError:
        logger.debug("leader_lock: close raised", exc_info=True)


def describe_lock_holder(path: Path | None = None) -> str:
    """Best-effort: render a one-line description of who holds the lock.

    Used by :command:`hermes agentchat doctor` to tell the operator
    whether THIS process is the leader, another process is, or no one
    is. We can detect "held by someone" via a non-blocking probe, but
    POSIX gives us no portable way to read the holder's PID without
    parsing ``/proc/locks`` (Linux-specific).

    Returns one of:

    * ``"not present"`` — file doesn't exist (no gateway has run yet)
    * ``"held"`` — lock is currently held by some process
    * ``"free"`` — file exists but no one holds the lock
    * ``"unknown: <reason>"`` — couldn't probe
    """
    target = path or default_lock_path()
    if not target.exists():
        return "not present"

    try:
        import fcntl
    except ImportError:
        return "unknown: fcntl unavailable (non-POSIX platform)"

    # Open a probe fd. If we can grab and immediately release the lock,
    # nobody else has it. If we can't grab it (BlockingIOError), someone
    # else does.
    try:
        probe_fd = os.open(target, os.O_RDONLY)
    except OSError as exc:
        return f"unknown: {exc!r}"
    try:
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except BlockingIOError:
            return "held"
        # We grabbed it — nobody else had it. Release immediately.
        fcntl.flock(probe_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        return "free"
    except OSError as exc:
        return f"unknown: {exc!r}"
    finally:
        with contextlib.suppress(OSError):
            os.close(probe_fd)
