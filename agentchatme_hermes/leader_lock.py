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

2. **OS-enforced singleton per account** — ``fcntl.flock(LOCK_EX |
   LOCK_NB)`` on ``<hermes root>/agentchat-locks/ws-<fingerprint>.lock``,
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


def lock_path_for(
    *,
    hermes_home: Path,
    account_fingerprint: str,
    lock_dir: Path | None = None,
) -> Path:
    """Resolve the leader-lock file for one account on this machine.

    ``<hermes root>/agentchat-locks/ws-<fingerprint>.lock`` by default,
    where the root is the directory that owns every profile
    (``~/.hermes`` for ``~/.hermes/profiles/<name>``). Keeping the file
    under the root rather than under the profile is deliberate: two
    profiles sharing one key must share one lock. ``lock_dir`` overrides
    the directory (``AGENTCHATME_LOCK_DIR``) for read-only homes.
    """
    from .profile_scope import hermes_root_of

    base = lock_dir if lock_dir is not None else hermes_root_of(hermes_home) / LOCK_DIRNAME
    return base / f"ws-{account_fingerprint}.lock"


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

    target = path
    try:
        # The lock directory is ours (``agentchat-locks/``), so creating
        # it is in scope — unlike the Hermes home itself. 0700: the
        # directory sits beside profile ``.env`` files.
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.name == LOCK_DIRNAME:
            os.chmod(target.parent, 0o700)
        # Open RW + create-if-missing. Mode 0600 so the API key
        # adjacent in the Hermes home isn't loosened by lock-file mode bits.
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        logger.exception(
            "leader_lock: cannot open %s — falling back to CLI mode "
            "(no WS in this process)",
            target,
        )
        return None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined, unused-ignore]
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

        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined, unused-ignore]
    except (ImportError, OSError):
        logger.debug("leader_lock: LOCK_UN raised", exc_info=True)
    try:
        os.close(fd)
    except OSError:
        logger.debug("leader_lock: close raised", exc_info=True)


def describe_lock_holder(path: Path) -> str:
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
    target = path
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
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined, unused-ignore]
        except BlockingIOError:
            return "held"
        # We grabbed it — nobody else had it. Release immediately.
        fcntl.flock(probe_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined, unused-ignore]
        return "free"
    except OSError as exc:
        return f"unknown: {exc!r}"
    finally:
        with contextlib.suppress(OSError):
            os.close(probe_fd)
