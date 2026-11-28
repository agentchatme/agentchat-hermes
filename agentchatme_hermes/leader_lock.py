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

2. **OS-enforced singleton per account** — an exclusive, non-blocking
   OS file lock (:func:`_try_lock`) on ``<hermes root>/agentchat-locks/ws-<fingerprint>.lock``,
   where the fingerprint identifies the (server, API key) pair (see
   :func:`profile_scope.account_fingerprint`). Even if two
   ``_HERMES_GATEWAY=1`` processes for the same account ever coexist
   (concurrent restart, operator running a second ``hermes gateway run
   --replace`` while the first is still draining), only one acquires
   the lock. The other detects the condition, logs loudly, and falls
   back to CLI-mode runtime — the daemon/invoker subsystems stay dark
   in the loser, so we get exactly one live WS **per account**.

   Scoping the lock to the account rather than to the machine is what
   lets several Hermes profiles on one computer each receive for their
   own handle. Before 0.2.3111111 the lock lived at
   ``~/.hermes/agentchat-ws.lock`` — one per OS user — so the first
   profile to start took the only socket and every sibling profile went
   deaf while still being able to send. The matrix the new scheme gives:

   * different profiles, different accounts → different lock files →
     every profile receives (the September 2026 fleet failure, fixed);
   * one profile restarted / overlapping gateways → same lock file →
     one leader, the other a loud follower (the original guarantee);
   * two profiles configured with the *same* key → same lock file →
     one leader, so one account never drives two concurrent turns.

The lock file descriptor is held open for the process lifetime. The OS
releases the lock when the descriptor closes (process exit, a crash, a
kill, or explicit :func:`release_leader_lock`). No stale lock files
survive a crash; no special cleanup hook is needed.

One lock, two operating systems
───────────────────────────────
The code relies on four properties of the lock, and both primitives give
all four:

* **non-blocking**: an attempt returns at once, held or busy;
* **released by the OS** when the descriptor closes or the process ends
  in any way, so a crashed holder never leaves a lock behind;
* **owned by the open file, not by the process**: two ``os.open`` of the
  same file in one process exclude each other, which is how two profiles
  of one multiplexed gateway that share a key elect one leader;
* **not inherited**: Python opens descriptors non-inheritable (PEP 446),
  so a child process the gateway starts never keeps the lock alive.

POSIX (Linux, macOS): ``fcntl.flock(LOCK_EX | LOCK_NB)``. flock locks
belong to the open file description; contention is ``BlockingIOError``.

Windows: ``msvcrt.locking(fd, LK_NBLCK, 1)`` on byte 0, which is the C
runtime's ``_locking``, i.e. ``LockFile`` on the descriptor's handle: an
exclusive byte-range lock that fails at once when another handle holds
the range. Windows byte-range locks belong to the file handle (the file
object): a second handle opened by the same process cannot take a range
the first one holds, exactly like a second flock descriptor. The system
unlocks them when the handle is closed or the process terminates
(``LockFile``/``LockFileEx`` documentation). Contention reads as
``EACCES`` (``ERROR_LOCK_VIOLATION``) from the C runtime. The range is
locked past the end of the empty file, which Windows allows; nothing ever
reads the lock file, so the mandatory nature of Windows locks is moot.
Hermes itself locks the same way on Windows (``hermes_cli/auth.py``,
``pm/filesystem.py``: one byte at offset 0). Rejected: ``LockFileEx``
through ctypes (the same kernel lock, more code), a named mutex (owned by
a thread, re-entrant for that thread, so two profiles on one thread would
not exclude each other), ``O_EXCL`` marker files (left behind by a crash)
and third-party lock packages (``filelock``/``portalocker`` wrap the same
calls and add a dependency).

One difference is documented by Microsoft: the unlock at process exit is
done by the system "as resources allow", so a lock can outlive its holder
by a moment. A gateway that lost the election retries every
:data:`.runtime.LEADER_RETRY_INTERVAL_S` seconds, which covers it.
"""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Windows: the byte range locked (byte 0, one byte) and the C runtime's
# errno for "another handle holds it" (``ERROR_LOCK_VIOLATION`` → EACCES;
# EDEADLOCK is what the blocking mode reports after its retries; EAGAIN is
# accepted as Hermes' own lock helper accepts it, ``pm/filesystem.py``).
_WINDOWS_LOCK_BYTES = 1
_WINDOWS_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt at the exclusive lock on ``fd``'s open file.

    ``True``: this descriptor holds it now. ``False``: another open file
    holds it (this process's or another's). Raises ``OSError`` for anything
    else (a file system without locks, a bad descriptor)."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _WINDOWS_LOCK_BYTES)
        except OSError as exc:
            if exc.errno in _WINDOWS_BUSY_ERRNOS:
                return False
            raise
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _unlock(fd: int) -> None:
    """Release the lock ``fd`` holds. Raises ``OSError`` when it holds none."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _WINDOWS_LOCK_BYTES)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


GATEWAY_ENV_MARKER = "_HERMES_GATEWAY"
GATEWAY_ENV_VALUE = "1"

# Argv tokens that identify the long-lived gateway-run command. Hermes
# accepts ``hermes gateway run`` (and ``hermes gateway run --replace``);
# the ``run`` subcommand is the only one that starts a persistent
# gateway process. ``hermes gateway stop`` / ``status`` / ``list``
# must NOT be treated as gateway-class — they're short-lived CLI ops.
_GATEWAY_RUN_ARGV_TOKENS = ("gateway", "run")


def is_hermes_gateway_process() -> bool:
    """Return ``True`` iff this process is the long-lived Hermes gateway.

    The only signal is argv: the operator invoked ``hermes gateway run``
    (or ``hermes gateway`` with no subcommand, or either with flags).
    It is reliable at ``register()`` time, which matters because Hermes
    calls ``discover_plugins()`` in ``hermes_cli/main.py`` **before**
    ``gateway/run.py`` sets ``_HERMES_GATEWAY=1``.

    The env marker is deliberately *not* sufficient on its own any more.
    Every child Python the gateway spawns inherits it, and a child that
    became a leader candidate could win the lock while the real gateway
    restarted, connect, drain and acknowledge the backlog into its own
    memory, then exit — losing those messages. Field logs from a fleet
    in October 2026 showed exactly that pattern (eleven connects in
    thirty minutes from short-lived processes, deliveries never seen).
    A process that has the marker but is not ``hermes gateway run`` is
    logged at DEBUG and treated as a follower.

    A ``True`` return makes this process a *candidate*. The file-lock
    layer (:func:`try_acquire_ws_leader_lock`) is the arbiter: one live
    socket per account.
    """
    if _argv_matches_gateway_run(sys.argv):
        return True
    if os.environ.get(GATEWAY_ENV_MARKER) == GATEWAY_ENV_VALUE:
        logger.debug(
            "leader_lock: %s=1 inherited by a process that is not `hermes "
            "gateway run` (argv=%r) — follower mode",
            GATEWAY_ENV_MARKER,
            sys.argv[:4],
        )
    return False


# Flags that take a value and may sit between ``gateway`` and ``run``
# (``hermes gateway -p growth run``). Their value must not be mistaken
# for the subcommand.
_GATEWAY_VALUE_FLAGS = frozenset({"-p", "--profile", "-c", "--config", "--home"})


def _argv_matches_gateway_run(argv: list[str]) -> bool:
    """Return True iff argv is a ``hermes gateway run`` invocation.

    The token after ``gateway`` (skipping flags and their values) must be
    ``run``, or absent — ``hermes gateway`` alone is the legacy spelling
    of starting the gateway. Any other subcommand (``status``, ``stop``,
    ``restart``, ``list``, ``install``, ``migrate``, ...) is a short-lived
    CLI operation and must never become a socket owner.

    Robust against:

    * ``hermes gateway run`` (canonical)
    * ``hermes gateway run --replace`` (flag after run)
    * ``hermes gateway -p profile run`` (value-bearing flag in between)
    * ``hermes gateway --replace run`` (bare flag in between)
    * ``python -m hermes_cli.main gateway run`` (module form)
    """
    try:
        index = argv.index("gateway")
    except ValueError:
        return False
    rest = argv[index + 1 :]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _GATEWAY_VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token == "run"
    return True


LOCK_DIRNAME = "agentchat-locks"


# The per-account lock files: ``ws`` is the socket's leader lock (held for the
# process's life); ``adopt`` is held for the moment a profile moves a key
# found only under the pre-v3 name into its key file (:mod:`.credentials`).
LockPurpose = Literal["ws", "adopt"]


def lock_path_for(
    *,
    hermes_home: Path,
    account_fingerprint: str,
    lock_dir: Path | None = None,
    purpose: LockPurpose = "ws",
) -> Path:
    """Resolve a per-account lock file on this machine.

    ``<hermes root>/agentchat-locks/<purpose>-<fingerprint>.lock`` by
    default, where the root is the directory that owns every profile
    (``~/.hermes`` for ``~/.hermes/profiles/<name>``). Keeping the file
    under the root rather than under the profile is deliberate: two
    profiles sharing one key must share one lock. ``lock_dir`` overrides
    the directory (``AGENTCHATME_LOCK_DIR``) for read-only homes.
    """
    from .profile_scope import hermes_root_of

    base = lock_dir if lock_dir is not None else hermes_root_of(hermes_home) / LOCK_DIRNAME
    return base / f"{purpose}-{account_fingerprint}.lock"


def legacy_lock_path() -> Path:
    """The pre-0.2.3111111 per-machine lock file, ``~/.hermes/agentchat-ws.lock``.

    No longer acquired. Kept so :command:`hermes agentchat doctor` can
    tell operators the stale file is harmless and may be deleted.
    """
    return Path.home() / ".hermes" / "agentchat-ws.lock"


def try_acquire_ws_leader_lock(path: Path) -> int | None:
    """Attempt to grab the singleton WS leader lock.

    Returns the file descriptor on success (the caller MUST keep this
    open for the process lifetime), or ``None`` if another process
    already holds it, or no lock could be taken here (logged).

    Implementation notes:

    * :func:`_try_lock` is atomic and non-blocking on both platforms
      (``flock`` on POSIX, ``LockFile`` through ``msvcrt.locking`` on
      Windows). Two callers racing for the lock cannot both succeed.
    * We deliberately do NOT write the PID into the file. The lock
      semantics are tied to the open file, not to file content;
      PID-tracking would be a separate concern (the health record beside
      the lock carries the holder's pid).
    * We do not delete the lock file on release. Lock files are
      stateless markers — leaving the file around lets a re-acquire
      succeed without recreating it (cheaper and avoids a race where two
      processes both create-and-lock the file simultaneously). Stale
      files are harmless: they only carry "lock currently held by some
      process" semantics WHILE that process holds the OS lock.
    """
    target = path
    try:
        fd = _open_lock_file(target)
    except OSError as exc:
        _log_once(
            f"open:{target}:{exc}",
            logging.WARNING,
            "leader_lock: cannot open %s (%s) — falling back to CLI mode (no WS in this process)",
            target,
            exc,
        )
        return None

    try:
        held = _try_lock(fd)
    except OSError as exc:
        os.close(fd)
        _log_once(
            f"lock:{target}:{exc}",
            logging.WARNING,
            "leader_lock: the OS lock failed on %s (%s) — falling back to CLI mode",
            target,
            exc,
        )
        return None
    if not held:
        os.close(fd)
        _log_once(
            f"held:{target}",
            logging.INFO,
            "leader_lock: %s already held by another process — this "
            "process will run in CLI mode (no WS, no invoker)",
            target,
        )
        return None
    return fd


_logged: set[str] = set()


def _log_once(key: str, level: int, message: str, *args: object) -> None:
    """``level`` the first time for ``key`` in this process, DEBUG after: a
    gateway that lost the election retries every 15 s and must not log every
    15 s (the runtime says once that it is a follower)."""
    if key in _logged:
        logger.debug(message, *args)
        return
    _logged.add(key)
    logger.log(level, message, *args)


def _open_lock_file(target: Path) -> int:
    """Open (creating) a lock file in our lock directory; raises ``OSError``."""
    from .owner_only import make_private_dir

    # The lock directory is ours (``agentchat-locks/``), so creating it is in
    # scope — unlike the Hermes home itself — and it is kept owner-only (0700;
    # on Windows the owner-only access list, best effort): it sits in the
    # Hermes root. Any other directory (``AGENTCHATME_LOCK_DIR``, the key
    # directory for the sign-up lock) is created as ``mkdir`` creates it.
    if target.parent.name == LOCK_DIRNAME:
        make_private_dir(target.parent, enforce=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    # Open RW + create-if-missing. Mode 0600 so the key adjacent in the
    # Hermes home isn't loosened by lock-file mode bits (POSIX; on Windows
    # the file takes its directory's access list).
    return os.open(target, os.O_RDWR | os.O_CREAT, 0o600)


HeldLock = Literal["held", "busy", "unavailable"]


@contextlib.contextmanager
def held_lock(path: Path, *, timeout_s: float = 2.0, poll_s: float = 0.05) -> Iterator[HeldLock]:
    """Hold the exclusive OS lock (:func:`_try_lock`) on ``path`` for the block.

    Yields ``"held"`` while held; ``"busy"`` when another holder kept it
    past ``timeout_s`` (the caller does not do its step now);
    ``"unavailable"`` when no lock can be had here at all (the lock
    directory cannot be written, or the file system takes no locks), so
    there is nothing to exclude with and the caller decides. The lock
    belongs to the open file on both platforms, so two profiles of one
    multiplexed gateway exclude each other as two processes do.
    """
    try:
        fd = _open_lock_file(path)
    except OSError:
        logger.warning("leader_lock: cannot open %s", path, exc_info=True)
        yield "unavailable"
        return
    deadline = time.monotonic() + timeout_s
    state: HeldLock = "busy"
    try:
        while True:
            try:
                if _try_lock(fd):
                    state = "held"
                    break
            except OSError:
                logger.warning("leader_lock: the OS lock failed on %s", path, exc_info=True)
                state = "unavailable"
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
        yield state
    finally:
        if state == "held":
            with contextlib.suppress(OSError):
                _unlock(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def release_leader_lock(fd: int) -> None:
    """Release a previously-acquired leader lock.

    Idempotent on the fd: a second call after the fd is closed will
    raise ``OSError`` which we swallow at DEBUG. Production callers
    typically don't need to call this explicitly — the OS releases the
    lock on process exit — but it's exposed for clean test teardown and
    for the in-process stop()/start() cycle.
    """
    try:
        _unlock(fd)
    except OSError:
        logger.debug("leader_lock: unlock raised", exc_info=True)
    try:
        os.close(fd)
    except OSError:
        logger.debug("leader_lock: close raised", exc_info=True)


def describe_lock_holder(path: Path) -> str:
    """Best-effort: render a one-line description of who holds the lock.

    Used by :command:`hermes agentchat doctor` to tell the operator
    whether THIS process is the leader, another process is, or no one
    is. We can detect "held by someone" via a non-blocking probe, but
    neither OS gives a portable way to read the holder's PID (the health
    record beside the lock names it).

    Returns one of:

    * ``"not present"`` — file doesn't exist (no gateway has run yet)
    * ``"held"`` — lock is currently held by some open file (another
      process, or another descriptor of this one)
    * ``"free"`` — file exists but no one holds the lock
    * ``"unknown: <reason>"`` — couldn't probe
    """
    target = path
    if not target.exists():
        return "not present"

    # Open a probe fd. If we can grab and immediately release the lock,
    # nobody else has it. If we can't grab it, someone else does.
    try:
        probe_fd = os.open(target, os.O_RDONLY)
    except OSError as exc:
        return f"unknown: {exc!r}"
    try:
        if not _try_lock(probe_fd):
            return "held"
        # We grabbed it — nobody else had it. Release immediately.
        _unlock(probe_fd)
        return "free"
    except OSError as exc:
        return f"unknown: {exc!r}"
    finally:
        with contextlib.suppress(OSError):
            os.close(probe_fd)
