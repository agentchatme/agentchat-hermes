"""The AgentChat manual, as the ``agentchat:agentchat`` skill.

The server owns the manual (``https://agentchat.me/skill.md``) and announces the
current one in the served texts (``GET /v1/texts?runtime=hermes`` →
``manual: {url, version, sha256}``).

By default the skill is this release's copy, the package's
``skills/agentchat/SKILL.md`` (the published manual byte for byte, refreshed
by ``scripts/refresh_bundled_texts.py`` and pinned by
``tests/test_skill_bundle.py``): :func:`prepare` registers it, nothing here
downloads or writes a manual, and it changes only when the plugin is updated.
A file in plugin storage (below) from a time ``live_texts`` was on is left as
it is and not used. A pip upgrade replaces the package's files one by one: a
``skill_view`` in a running process at that moment finds the file missing and
Hermes drops the skill for that process until it restarts (Hermes' own
``hermes plugins update`` swaps the plugin's folder with two renames).

With the plugin setting ``live_texts`` on (:mod:`.texts`), the rest of this
docstring applies. Hermes reads a plugin skill's file fresh from its
registered path on every ``skill_view``, so the plugin registers a file it
keeps current, not the copy in the package::

    <profile home>/plugin-data/agentchat/manual/SKILL.md

* ``<profile home>/plugin-data/<plugin>`` is Hermes' per-plugin storage
  (``plugins/plugin_storage.py``): one per profile (``~/.hermes``,
  ``~/.hermes/profiles/<name>``, ``/opt/data`` in Docker), kept across
  upgrades (site-packages is replaced; ``/opt/hermes`` is read-only in Docker),
  never scanned for skills, untouched by ``skill_manage`` and the curator. The
  path is computed from the home of the profile being served, never from
  ``get_hermes_home()`` on a thread that was not given that profile.
* ``SKILL.md`` is alone in its folder: ``skill_view(name, file_path=...)``
  serves any file in a skill's folder. Nothing else is written there, the key
  file (``<profile home>/agentchat/credentials.json``) is elsewhere, and a
  folder that holds anything else is not registered.
* The file is only ever replaced atomically (the SDK kit's write: a temp file
  beside it, then a rename), never deleted: a ``skill_view`` that finds the
  registered file missing drops the skill for the rest of the process.

Who writes it, always through the kit's ``sync_manual`` / ``ManualSync`` (the
host pin, the redirect checks, the size cap, front matter ``name: agentchat``
at the announced version, the announced sha256, never worse than before,
atomic, mode 0644) — and only these; every other Hermes process (a chat or
TUI session, a one-shot, ``hermes agentchat status``, ``doctor`` without
``--fix``, any other command) reads the file and never writes it, so what
``status`` and ``doctor`` report is the file as found:

* the gateway (``hermes gateway run``), at ``register()``, locally
  (:func:`prepare` with ``place=True``): the bundled copy when there is no file
  yet — the offline floor for a first install. Then the file is registered.
  When it cannot be written (or is not a manual) the bundled copy itself is
  registered, and ``doctor`` says the manual is frozen at that version.
* the gateway leader: the kit's ``ManualSync`` on its receive loop, following
  the texts (at start, on every change, and while the file is not the served
  one again after 5 min doubling to 1 h; :func:`manual_sync_for`);
* a gateway that is not the leader (another gateway holds this account's
  socket, or the integration is off): one sync on a daemon thread after its
  texts load (:func:`sync_in_background`);
* the placement of ``hermes agentchat register`` / ``login`` / ``recover`` /
  the wizard, and ``hermes agentchat doctor --fix``: one sync, saying what it
  repaired (:func:`describe_repair`).

A process that registered the bundled copy keeps serving it until it restarts,
even once its sync has put a good file in place: Hermes takes a plugin skill's
path at ``register()`` and has no supported way to change it later. The
gateway says so in its log (:func:`note_registration_stale`).

With ``live_texts`` on, the package's ``skills/agentchat/SKILL.md`` is only
those fallback bytes. This module imports nothing from the SDK at import time:
the CLI must load where the kit is missing.
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from .profile_scope import context_thread, hermes_home_key
from .texts import mode_for

if TYPE_CHECKING:
    from agentchatme.integration import ManualSync, ManualSyncResult

    from .profile_scope import ProfileScope

logger = logging.getLogger(__name__)

MANUAL_URL = "https://agentchat.me/skill.md"
MANUAL_SKILL_NAME = "agentchat"
PLUGIN_DATA_DIRNAME = "plugin-data"
# The plugin's name (plugin.yaml ``name:``): Hermes' per-plugin storage folder.
PLUGIN_NAME = "agentchat"
MANUAL_DIRNAME = "manual"
MANUAL_FILENAME = "SKILL.md"
# The package's copy: this release's manual (``live_texts`` off), else the
# fallback bytes only (the offline floor for a first install).
BUNDLED_MANUAL_PATH = Path(__file__).parent / "skills" / "agentchat" / MANUAL_FILENAME

# The kit's temp name for an atomic write of ``SKILL.md``
# (``agentchatme/integration/_files.py``): ``.SKILL.md.<pid>.<hex>.tmp``.
_KIT_TMP_NAME = re.compile(r"^\.SKILL\.md\.\d+\.[0-9a-f]+\.tmp$")
# A temp file older than this was left by a write that never finished (the
# process died between the temp file and the rename); a live write takes ms.
_STALE_TMP_S = 600.0
# What a desktop drops into any folder it shows; harmless, never ours.
_DESKTOP_DROPPINGS = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# How long a process that is exiting waits for a sync still in flight (a CLI
# command can end before its download does).
_EXIT_WAIT_S = 2.0
# Hermes runs every inline-shell snippet (an exclamation mark, then a command in
# backticks) in a SKILL.md it serves when ``skills.inline_shell`` is on
# (``agent/skill_preprocessing.py`` ``_INLINE_SHELL_RE``, the same pattern): a
# manual that carries one is never registered or kept. The backtick is written
# as the escape ``\x60`` here and below, never next to the exclamation mark:
# Hermes' install scanner (``hermes plugins install``) reads this source text
# and flags that two-character sequence wherever it appears, this guard included.
_TICK = "\x60"
INLINE_SHELL = re.compile(r"!\x60([^\x60\n]+)\x60")
_SNIPPET = "!" + _TICK + "..." + _TICK
_INLINE_SHELL_REFUSED = (
    f"the served manual carries inline shell ({_SNIPPET}), which Hermes runs when "
    "skills.inline_shell is on — the bundled copy was put back"
)



def carries_inline_shell(data: bytes | str) -> bool:
    """Whether a manual's text carries an inline-shell snippet (:data:`INLINE_SHELL`)."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return INLINE_SHELL.search(text) is not None


ManualCheckState = Literal["current", "stale", "tampered", "unsafe", "unchecked", "missing"]


# ─── where ───────────────────────────────────────────────────────────────────


def manual_dir(home: Path) -> Path:
    """``<profile home>/plugin-data/agentchat/manual``: the skill's folder."""
    return Path(home) / PLUGIN_DATA_DIRNAME / PLUGIN_NAME / MANUAL_DIRNAME


def manual_path(home: Path) -> Path:
    """The kept-current manual for the profile at ``home``."""
    return manual_dir(home) / MANUAL_FILENAME


def bundled_bytes() -> bytes | None:
    """The package's copy, or ``None`` when it is missing (a hand-broken install)."""
    try:
        return BUNDLED_MANUAL_PATH.read_bytes()
    except OSError:
        return None


def bundled_version() -> str | None:
    data = bundled_bytes()
    return _front_matter(data).get("version") if data is not None else None


def read_version(path: Path) -> str | None:
    """The ``version:`` in the front matter of the file at ``path`` (``None``
    when unreadable, without one, or without the kit)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return _front_matter(data).get("version")


def other_files(folder: Path) -> list[str]:
    """Names in the skill's folder that are not ``SKILL.md`` (nor a write of
    ours in flight, nor a desktop's dropping). ``skill_view`` would serve them."""
    try:
        names = sorted(entry.name for entry in folder.iterdir())
    except OSError:
        return []
    return [
        name
        for name in names
        if name != MANUAL_FILENAME and name not in _DESKTOP_DROPPINGS and not _KIT_TMP_NAME.match(name)
    ]


def stray_writes(folder: Path, *, now: float | None = None) -> list[Path]:
    """Temp files of the kit's atomic write that no write is still using."""
    moment = time.time() if now is None else now
    found: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not _KIT_TMP_NAME.match(entry.name):
            continue
        try:
            if moment - entry.lstat().st_mtime > _STALE_TMP_S:
                found.append(entry)
        except OSError:
            continue
    return found


# ─── what the plugin registered ──────────────────────────────────────────────


@dataclass(frozen=True)
class ManualRegistration:
    """The file this process gave Hermes as ``agentchat:agentchat``."""

    path: Path
    target: Path
    reason: str | None = None
    """Why the bundled copy was registered instead of ``target``."""
    routine: bool = False
    """The bundled copy because this process places nothing and the file is
    not there yet (not a fault: the gateway, a login or ``doctor --fix``
    places it)."""
    pinned: bool = False
    """The bundled copy because the profile uses this release's texts
    (``live_texts`` off, the default): not a fault, nothing is kept current."""

    @property
    def kept_current(self) -> bool:
        return self.path == self.target

    def as_record(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kept_current": self.kept_current,
            "reason": self.reason,
            "pinned": self.pinned,
        }


_registrations: dict[str, ManualRegistration] = {}
_registrations_lock = threading.Lock()


def record_registration(home: Path, registration: ManualRegistration) -> None:
    with _registrations_lock:
        _registrations[hermes_home_key(Path(home))] = registration


def registration_for(home: Path) -> ManualRegistration | None:
    """What this process registered for the profile at ``home``, if anything."""
    with _registrations_lock:
        return _registrations.get(hermes_home_key(Path(home)))


def prepare(home: Path, *, place: bool = True) -> ManualRegistration | None:
    """The file to register for the profile at ``home``. Local only — no network.

    ``live_texts`` off (the default): the package's copy, whatever ``place``
    says; nothing is written and the plugin-storage file, if any, is left as it
    is (``pinned``; ``None`` when the package's copy is missing). With it on:
    ``place`` (the gateway): creates the kept-current file from the bundled
    bytes when there is none (the kit's ``sync_manual`` with nothing announced
    writes its fallback only then, atomically) and removes a temp file a dead
    write left. Without ``place`` nothing is written: the file is registered
    as it is, or the bundled copy when there is none yet. Returns the bundled
    copy, with the reason, when the file cannot be written, is not the
    agentchat manual, or shares its folder with anything else; ``None`` when
    there is no manual at all. Never raises.
    """
    target = manual_path(home)
    bundled = BUNDLED_MANUAL_PATH if BUNDLED_MANUAL_PATH.is_file() else None

    def frozen(reason: str) -> ManualRegistration | None:
        if bundled is None:
            logger.warning(
                "agentchat: no manual to register — %s, and the package's copy is missing at %s "
                "(reinstall agentchatme-hermes)",
                reason,
                BUNDLED_MANUAL_PATH,
            )
            return None
        return ManualRegistration(path=bundled, target=target, reason=reason)

    if not mode_for(home).live:
        if bundled is None:
            return frozen("live_texts is off, so the manual is this release's copy")
        return ManualRegistration(path=bundled, target=target, pinned=True)

    try:
        from agentchatme.integration import sync_manual
    except ImportError:
        return frozen("the agentchatme SDK kit is not installed, so the manual cannot be kept current")
    try:
        if not place:
            return _look(home, target, bundled, frozen)
        return _prepare(home, target, sync_manual, frozen)
    except Exception as exc:  # EACCES on a folder this user cannot traverse, and any surprise
        return frozen(f"{target} could not be checked: {exc}")


def _look(
    home: Path,
    target: Path,
    bundled: Path | None,
    frozen: Callable[[str], ManualRegistration | None],
) -> ManualRegistration | None:
    """:func:`prepare` without writing: the file as it is, else the bundled copy."""
    if not (target.exists() or target.is_symlink()):
        if bundled is None:
            return frozen(f"{target} does not exist")
        return ManualRegistration(
            path=bundled,
            target=target,
            reason=f"{target} is not placed yet (the gateway, a login or `doctor --fix` places it)",
            routine=True,
        )
    problem = _unusable(target, home)
    if problem is not None:
        return frozen(problem)
    return ManualRegistration(path=target, target=target)


def _prepare(
    home: Path,
    target: Path,
    sync_manual: Callable[..., ManualSyncResult],
    frozen: Callable[[str], ManualRegistration | None],
) -> ManualRegistration | None:
    for leftover in stray_writes(target.parent):
        try:
            leftover.unlink()
            logger.info("agentchat: removed %s, left by a manual write that never finished", leftover)
        except OSError:
            logger.debug("agentchat: could not remove %s", leftover, exc_info=True)

    problem = _unusable(target, home)
    if problem is not None and (target.exists() or target.is_symlink()):
        return frozen(problem)
    try:
        result = sync_manual(path=target, served=None, api_base=None, fallback=bundled_bytes())
    except Exception as exc:  # the kit never raises; a surprise must not cost the skill
        return frozen(f"{target} could not be placed: {exc}")
    if result.state == "failed":
        return frozen(f"{target} cannot be written: {result.error}")
    problem = _unusable(target, home)
    if problem is not None:
        return frozen(problem)
    if result.state == "fallback":
        logger.info(
            "agentchat: placed the bundled manual %s at %s; it is kept current from the server",
            result.version,
            target,
        )
    return ManualRegistration(path=target, target=target)


def _unusable(target: Path, home: Path) -> str | None:
    """Why ``target`` must not be registered, or ``None``."""
    if target.is_symlink():
        return f"{target} is a symbolic link, not the plugin's own file (not followed; the next sync with the server replaces the link)"
    if not target.exists():
        return f"{target} does not exist"
    if not target.is_file():
        return f"{target} is not a file (left as it is)"
    folder = target.parent
    try:
        credentials_dir = (Path(home) / "agentchat").resolve()
        if folder.resolve() == credentials_dir:
            return f"{folder} is the key's folder"
    except OSError:
        pass
    others = other_files(folder)
    if others:
        return (
            f"{folder} holds other files ({', '.join(others[:5])}); skill_view serves every file in a "
            "skill's folder, so it is not registered until they are moved out"
        )
    try:
        data = target.read_bytes()
    except OSError as exc:
        return f"{target} cannot be read: {exc}"
    if _front_matter(data).get("name") != MANUAL_SKILL_NAME:
        return f"{target} is not the agentchat manual (left as it is; the next sync with the server replaces it)"
    if carries_inline_shell(data):
        return f"{target} carries inline shell ({_SNIPPET}), which Hermes runs when skills.inline_shell is on"
    return None


# ─── syncing ──────────────────────────────────────────────────────────────────


def sync_once(
    *, home: Path, served: Any, api_base: str | None, http_client: Any = None
) -> ManualSyncResult | None:
    """One blocking sync of the profile's manual to ``served`` (``texts.manual``).
    ``None`` when the kit is missing. Never raises."""
    try:
        from agentchatme.integration import sync_manual
    except ImportError:
        return None
    try:
        result = sync_manual(
            path=manual_path(home),
            served=served,
            api_base=api_base,
            fallback=bundled_bytes(),
            http_client=http_client,
        )
    except Exception as exc:  # EACCES on a folder this user cannot traverse (the kit's own checks)
        logger.debug("agentchat: the manual sync raised", exc_info=True)
        return _failed_result(manual_path(home), served, exc)
    return refuse_inline_shell(home, result)


def refuse_inline_shell(home: Path, result: ManualSyncResult) -> ManualSyncResult:
    """After a sync that wrote: a manual that carries inline shell
    (:data:`INLINE_SHELL`) is replaced by the bundled copy (through the kit,
    atomically, no network), and the result says so. Otherwise ``result``."""
    if not result.written:
        return result
    try:
        data = manual_path(home).read_bytes()
    except OSError:
        return result
    if not carries_inline_shell(data):
        return result
    logger.error("agentchat: %s (%s)", _INLINE_SHELL_REFUSED, manual_path(home))
    restored = _restore_bundled(home)
    base = restored if restored is not None else result
    return dataclasses.replace(base, up_to_date=False, error=_INLINE_SHELL_REFUSED)


def _restore_bundled(home: Path) -> ManualSyncResult | None:
    """The bundled copy written over the file, through the kit (the bundled
    bytes announced as themselves: no download)."""
    bundle = bundled_bytes()
    version = bundled_version()
    if bundle is None or version is None or carries_inline_shell(bundle):
        return None
    try:
        from agentchatme.integration import ServedManual, sync_manual

        return sync_manual(
            path=manual_path(home),
            served=ServedManual(url=MANUAL_URL, version=version, sha256=hashlib.sha256(bundle).hexdigest()),
            api_base="https://api.agentchat.me",
            fallback=bundle,
        )
    except Exception:
        logger.debug("agentchat: the bundled copy could not be put back", exc_info=True)
        return None


def _failed_result(path: Path, served: Any, exc: BaseException) -> ManualSyncResult:
    from agentchatme.integration import ManualSyncResult

    version, _sha = _announcement(served)
    url = served.get("url") if isinstance(served, dict) else getattr(served, "url", None)
    return ManualSyncResult(
        state="failed", path=path, version=None, previous=None, tampered=False, served=version,
        url=url if isinstance(url, str) else None, source=None, written=False,
        up_to_date=None if version is None else False, error=f"{path} could not be checked: {exc}",
    )


_inflight: set[threading.Thread] = set()
_inflight_lock = threading.Lock()
_exit_hook_installed = False


def sync_in_background(
    *, home: Path, served: Any, api_base: str | None, label: str, scope: ProfileScope | None = None
) -> threading.Thread | None:
    """One sync on a daemon thread (a gateway that holds no receive loop for
    this account: another gateway is the leader, or the integration is off),
    in the profile's scope (``scope``; the caller's context without one).
    ``None`` when nothing is announced (there is nothing to sync to), or the
    profile uses this release's manual (``live_texts`` off)."""
    if served is None or not mode_for(home).live:
        return None
    global _exit_hook_installed

    def run() -> None:
        try:
            result = sync_once(home=home, served=served, api_base=api_base)
            if result is not None:
                # A gateway that is not the leader: a manual it could not
                # bring current is said at INFO (doctor says it louder); a
                # write that failed is a warning.
                log_result(result, label=label, quiet=True)
                note_registration_stale(home, result, label=label)
        finally:
            with _inflight_lock:
                _inflight.discard(threading.current_thread())

    thread = (
        scope.thread(run, name="agentchat-manual-sync")
        if scope is not None
        else context_thread(run, name="agentchat-manual-sync")
    )
    with _inflight_lock:
        _inflight.add(thread)
        if not _exit_hook_installed:
            atexit.register(_wait_for_inflight)
            _exit_hook_installed = True
    thread.start()
    return thread


def _wait_for_inflight() -> None:
    """At exit: give a sync still in flight a moment to finish its write."""
    deadline = time.monotonic() + _EXIT_WAIT_S
    with _inflight_lock:
        threads = list(_inflight)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def manual_sync_for(
    texts: Any, *, home: Path, on_result: Callable[[ManualSyncResult], Any] | None = None
) -> ManualSync | None:
    """The kit's ``ManualSync`` for the gateway leader's texts cache; ``None``
    when the kit is missing, or the profile uses this release's manual
    (``live_texts`` off: the receive loop then keeps no manual current)."""
    if not mode_for(home).live:
        return None
    try:
        from agentchatme.integration import ManualSync
    except ImportError:
        return None
    return ManualSync(
        texts=texts, target_path=manual_path(home), fallback=bundled_bytes(), on_result=on_result
    )


def log_result(result: ManualSyncResult, *, label: str, quiet: bool = False) -> None:
    """One sync's outcome, at the level it deserves (``quiet``: a manual left
    at the version it has is INFO, not a warning)."""
    line = describe_result(result)
    if result.error == _INLINE_SHELL_REFUSED or result.state == "failed":
        logger.warning("agentchat: profile=%s manual %s", label, line)
    elif result.state == "updated":
        logger.info("agentchat: profile=%s manual %s", label, line)
    elif result.up_to_date is False:
        (logger.info if quiet else logger.warning)("agentchat: profile=%s manual %s", label, line)
    else:
        logger.debug("agentchat: profile=%s manual %s", label, line)


def result_record(result: ManualSyncResult, *, at: float | None = None) -> dict[str, Any]:
    """A sync's outcome for the health record."""
    return {
        "at": time.time() if at is None else at,
        "state": result.state,
        "version": result.version,
        "served": result.served,
        "up_to_date": result.up_to_date,
        "tampered": result.tampered,
        "error": None if result.error is None else str(result.error)[:300],
        "detail": describe_result(result)[:500],
    }


def describe_result(result: ManualSyncResult) -> str:
    """One sync in one line (agent-core's ``describeManualSync``)."""
    at = result.path
    have = (
        f"{result.previous} with a sha256 that is not the served one"
        if result.tampered
        else (result.previous or "an unversioned file")
    )
    if result.error == _INLINE_SHELL_REFUSED:
        return f"refused: {result.error} ({result.version}) → {at}"
    if result.state == "current":
        return f"current ({result.version}) — {at}"
    if result.state == "updated":
        was = f"was {have} → " if result.previous is not None or result.tampered else ""
        return f"{was}{result.version} {_how(result)} → {at}"
    if result.state == "fallback":
        if result.served is None:
            return f"bundled copy {result.version} → {at} (no served version in hand to check it against)"
        return f"bundled copy {result.version} → {at}; served {result.served} could not be fetched: {result.error}"
    if result.state == "kept":
        if result.served is None:
            return f"unchecked (have {have}; no served version in hand) — {at}"
        return f"stale (have {have}, served {result.served}) — {at}; kept as it is: {result.error}"
    return f"could not write {at}: {result.error}"


def _how(result: ManualSyncResult) -> str:
    if result.source == "download":
        return f"downloaded from {result.url}"
    return "placed from the bundled copy (the served version)"


def describe_repair(before: ManualCheck, result: ManualSyncResult) -> str:
    """One sync by the wizard's placement or ``doctor --fix``, saying what the
    file was before when the sync changed it: ``was stale (have 2.4.0, served
    2.4.4) → 2.4.4 placed from the bundled copy …``, ``was tampered (…) →
    restored: …``, ``was missing → …``."""
    if result.error == _INLINE_SHELL_REFUSED or not result.written:
        return describe_result(result)
    if result.state == "fallback":
        return f"was {before.summary} → {describe_result(result)}"
    verb = "restored: " if before.state in ("tampered", "unsafe") else ""
    return f"was {before.summary} → {verb}{result.version} {_how(result)} → {result.path}"


_said_stale_registration: set[str] = set()
_said_lock = threading.Lock()


def note_registration_stale(home: Path, result: ManualSyncResult, *, label: str) -> None:
    """A gateway that registered the bundled copy at start, whose sync has now
    put a usable manual at the kept-current path: Hermes keeps serving the
    path it was given at ``register()``, so the gateway says, once, to restart."""
    registration = registration_for(home)
    if registration is None or registration.kept_current or result.up_to_date is not True:
        return
    target = manual_path(home)
    try:
        if _unusable(target, home) is not None:
            return
    except OSError:
        return
    key = hermes_home_key(Path(home))
    with _said_lock:
        if key in _said_stale_registration:
            return
        _said_stale_registration.add(key)
    logger.warning(
        "agentchat: profile=%s the manual at %s is the served one again (%s), but this gateway "
        "serves the bundled copy it registered at start (%s) — restart the gateway to serve the "
        "repaired manual",
        label,
        target,
        result.version,
        registration.reason or "it could not use the file then",
    )


# ─── checking (status, doctor) ───────────────────────────────────────────────


@dataclass(frozen=True)
class ManualCheck:
    state: ManualCheckState
    path: Path
    version: str | None
    served: str | None
    summary: str

    @property
    def detail(self) -> str:
        return f"{self.summary} — {self.path}"


def check(path: Path, served: Any) -> ManualCheck:
    """Is the file at ``path`` the announced manual (``served``: ``texts.manual``
    or ``None``)? Read-only (agent-core's ``checkManual``)."""
    served_version, served_sha = _announcement(served)

    def result(state: ManualCheckState, summary: str, version: str | None = None) -> ManualCheck:
        return ManualCheck(state=state, path=path, version=version, served=served_version, summary=summary)

    try:
        if path.is_symlink() or not path.is_file():
            return result("missing", "missing")
        data = path.read_bytes()
    except OSError as exc:
        return result("missing", f"unreadable ({exc})")
    if not _kit_available():
        return result("unchecked", "unchecked (the agentchatme SDK kit is not installed)")
    fields = _front_matter(data)
    if fields.get("name") != MANUAL_SKILL_NAME:
        return result("tampered", "not the agentchat manual (its front matter does not name agentchat)")
    version = fields.get("version")
    have = version or "an unversioned file"
    if carries_inline_shell(data):
        return result("unsafe", f"unsafe (have {have}: it carries inline shell, {_SNIPPET})", version)
    if served_version is None:
        return result("unchecked", f"unchecked (have {have}; no served version in hand)", version)
    if served_sha is not None and not re.fullmatch(r"[0-9a-f]{64}", served_sha):
        return result("unchecked", f"unchecked (have {have}; the announced sha256 is malformed)", version)
    if version == served_version:
        if served_sha is None or hashlib.sha256(data).hexdigest() == served_sha:
            return result("current", f"current ({version})", version)
        return result(
            "tampered", f"tampered (have {version}, but its sha256 is not the served text's)", version
        )
    return result("stale", f"stale (have {have}, served {served_version})", version)


def _announcement(served: Any) -> tuple[str | None, str | None]:
    """``(version, sha256)`` of ``texts.manual`` (a model or a mapping)."""
    if served is None:
        return None, None
    data: Any = served.model_dump(mode="json") if hasattr(served, "model_dump") else served
    if not isinstance(data, dict):
        data = getattr(served, "__dict__", {})
    version = data.get("version")
    sha = data.get("sha256")
    return (
        version if isinstance(version, str) and version else None,
        sha.strip().lower() if isinstance(sha, str) and sha.strip() else None,
    )


def _kit_available() -> bool:
    try:
        from agentchatme.integration import manual_front_matter  # noqa: F401
    except ImportError:
        return False
    return True


def _front_matter(data: bytes) -> dict[str, str]:
    """The front matter's ``key: value`` fields, through the kit's reader
    (``{}`` without the kit, for text that is not UTF-8, or without front matter)."""
    try:
        from agentchatme.integration import manual_front_matter
    except ImportError:
        return {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return manual_front_matter(text) or {}


__all__ = [
    "BUNDLED_MANUAL_PATH",
    "INLINE_SHELL",
    "MANUAL_URL",
    "ManualCheck",
    "ManualRegistration",
    "bundled_bytes",
    "bundled_version",
    "carries_inline_shell",
    "check",
    "describe_repair",
    "describe_result",
    "log_result",
    "manual_dir",
    "manual_path",
    "manual_sync_for",
    "note_registration_stale",
    "other_files",
    "prepare",
    "read_version",
    "record_registration",
    "refuse_inline_shell",
    "registration_for",
    "result_record",
    "stray_writes",
    "sync_in_background",
    "sync_once",
]
