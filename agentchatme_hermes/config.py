"""Runtime configuration, resolved per Hermes profile.

Configuration is read once at plugin load, for the profile whose
``register(ctx)`` is running. Every value goes through
:func:`profile_scope.resolve_setting`, so a multiplexed gateway serving
several profiles gets each profile's own key — never the process
environment of whichever profile launched the gateway. Changing a value
after Hermes has started has no effect — the user must restart Hermes
(the same constraint every other Hermes plugin has, by design).

The key
───────
The key lives in this profile's AgentChat key file
(``<profile home>/agentchat/credentials.json``, :mod:`.key_store`), read
only by this plugin; Hermes' MCP client gets it as ``AGENTCHAT_API_KEY``
through the plugin's secret source (:mod:`.key_source`). When the key file
exists it decides which agent this profile is: an ``AGENTCHAT_API_KEY`` set
anywhere else is not used (the source supplies the key file's over it).

A profile set up before 0.6 has no key file: its key is one of its settings
as Hermes resolves them (``agent.secret_scope.get_secret``: the profile's
``.env`` or the environment), ``AGENTCHAT_API_KEY`` or the pre-v3
``AGENTCHATME_API_KEY``. This plugin never reads a ``.env`` file itself.
``hermes agentchat doctor --fix`` (or a login) moves the key into the key
file; a key found only under the pre-v3 name is moved at activation, once
the server has accepted it (:func:`.credentials.adopt_legacy_key`), because
the hosted tools read ``AGENTCHAT_API_KEY``. :func:`resolve_key` refuses a
key this profile must not act as — one another profile's key file holds, or
the default profile's key in a named profile never set up as an agent
(``hermes profile create`` copies ``.env``) — with a sentence that says what
to do.
Behaviour knobs keep their ``AGENTCHATME_`` prefix and are read as before.

Two knobs were renamed with the words (integration, wake); the old
spellings keep working and win nothing — the new name is read first, the
old one when the new one is unset:

* ``AGENTCHATME_INTEGRATION`` (``on`` | ``off``) ← ``AGENTCHATME_DOORBELL``
  (``v3`` | ``off``);
* ``AGENTCHATME_WAKE_DEBOUNCE_S`` ← ``AGENTCHATME_KNOCK_DEBOUNCE_S``.

``AGENTCHATME_REPLY_GATE_FAIL_OPEN`` is retired: the SDK's gate fails
closed and nothing starts a turn on a failed gate. A value set for it is
accepted, ignored, and logged once.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse, urlunparse

from . import key_store, profile_scope
from .profile_scope import (
    account_fingerprint,
    hermes_root_of,
    profile_label,
    resolve_hermes_home,
    resolve_setting,
)

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.agentchat.me"

ENV_API_KEY = "AGENTCHAT_API_KEY"
ENV_API_KEY_LEGACY = "AGENTCHATME_API_KEY"
ENV_HANDLE = "AGENTCHATME_HANDLE"
ENV_API_BASE = "AGENTCHATME_API_BASE"
ENV_WS_URL = "AGENTCHATME_WS_URL"
ENV_INTEGRATION = "AGENTCHATME_INTEGRATION"
ENV_INTEGRATION_LEGACY = "AGENTCHATME_DOORBELL"
ENV_WAKE_DEBOUNCE_S = "AGENTCHATME_WAKE_DEBOUNCE_S"
ENV_WAKE_DEBOUNCE_S_LEGACY = "AGENTCHATME_KNOCK_DEBOUNCE_S"
ENV_REPLY_GATE_ENABLED = "AGENTCHATME_REPLY_GATE_ENABLED"
ENV_REPLY_GATE_FAIL_OPEN = "AGENTCHATME_REPLY_GATE_FAIL_OPEN"  # retired, see module docstring
ENV_GATE_TIMEOUT_S = "AGENTCHATME_GATE_TIMEOUT_S"
ENV_TURN_IDLE_S = "AGENTCHATME_TURN_IDLE_TIMEOUT_S"
ENV_TURN_MAX_S = "AGENTCHATME_TURN_MAX_S"
ENV_LOCK_DIR = "AGENTCHATME_LOCK_DIR"
ENV_STATE_DIR = "AGENTCHATME_STATE_DIR"

# --- the integration -----------------------------------------------------------
# ``on`` (default): hold the socket, wake, gate on the server's card, start a
# turn on the hosted tools. ``off``: hold nothing — the agent still has the
# hosted tools and the manual, and checks AgentChat only when its human runs
# it. The pre-rename key took ``v3`` for on; ``v2`` named the removed native
# tool layer (pin ``agentchatme-hermes==0.2.3111111`` to stay on it).
INTEGRATION_ON = "on"
INTEGRATION_OFF = "off"
DEFAULT_INTEGRATION = INTEGRATION_ON
_INTEGRATION_VALUES = frozenset({INTEGRATION_ON, INTEGRATION_OFF})
_INTEGRATION_LEGACY_VALUES = {"v3": INTEGRATION_ON, "off": INTEGRATION_OFF}

# A burst of frames (a group waking up) becomes one wake: the first frame
# arms a timer, frames inside the window are absorbed, the timer fires once
# and the cycle reads the whole tray. The server bundles its wakes on the same
# scale (2 s) for group chatter.
DEFAULT_WAKE_DEBOUNCE_S = 2.0

# --- reply gate ------------------------------------------------------------
# On a wake the agent's own model answers the server's gate question for
# every card on the tray before any turn starts. On by default; off means
# every wake starts a turn and the agent judges in-turn with the hosted
# ``agentchat_inbox`` / ``agentchat_decide`` tools.
DEFAULT_REPLY_GATE_ENABLED = True

# Timeout (seconds) for one decision call. It judges up to 10 cards in one
# answer (the kit gates a larger tray in calls of 10), not one message.
DEFAULT_GATE_TIMEOUT_S = 30.0

# --- turn time limit ---------------------------------------------------------
# A turn with no activity for this long (no model call, no tool step) is
# interrupted and counts as failed: nothing is recorded, the cards stay on
# the tray. Hermes' cron scheduler uses the same 600 s for its unattended
# turns. The ceiling bounds a turn that stays busy. ``0`` switches a limit
# off (a turn that never ends then stops this account's wakes again).
DEFAULT_TURN_IDLE_S = 600.0
DEFAULT_TURN_MAX_S = 3600.0

_fail_open_warned = False


class ConfigError(RuntimeError):
    """Raised when configuration is malformed."""


class KeyRefused(ConfigError):
    """This profile holds a key it must not act as; the message says what to do."""


# Where the key came from (``Config.api_key_source``, ``KeyResolution.source``):
# the key file, or the name of the setting it was read from.
KEY_SOURCE_STORE = "key file"


@dataclass(frozen=True)
class Config:
    """Resolved plugin configuration.

    All fields are required and validated at construction. Use
    :func:`load_config` to construct from the profile's key and settings —
    it returns ``None`` when the profile has no key, which lets the plugin
    register the CLI wizard even before the user has set up an account.
    """

    api_key: str
    api_base: str
    ws_url: str
    # Where the key came from: ``KEY_SOURCE_STORE`` (the key file) or the
    # variable it was read from (a profile not moved to the key file yet).
    api_key_source: str = KEY_SOURCE_STORE
    integration: str = DEFAULT_INTEGRATION
    wake_debounce_s: float = DEFAULT_WAKE_DEBOUNCE_S
    reply_gate_enabled: bool = DEFAULT_REPLY_GATE_ENABLED
    gate_timeout_s: float = DEFAULT_GATE_TIMEOUT_S
    turn_idle_s: float = DEFAULT_TURN_IDLE_S
    turn_max_s: float = DEFAULT_TURN_MAX_S
    # The Hermes profile this configuration belongs to. Drives where the
    # SOUL.md anchor lives, how the runtime registry keys this profile,
    # and which root directory holds the leader locks and the state.
    hermes_home: Path = field(default_factory=resolve_hermes_home)
    # Optional overrides for the leader-lock and state directories
    # (containers with a read-only home, shared volumes). ``None`` →
    # ``<hermes root>/agentchat-locks`` and ``<hermes root>/agentchat-state``.
    lock_dir: Path | None = None
    state_dir: Path | None = None
    # What status and doctor say about the key that does not stop it (another
    # profile's key file holds the same key, a leftover pre-v3 setting);
    # logged at activation.
    key_notes: tuple[str, ...] = ()

    @property
    def account_fingerprint(self) -> str:
        """Non-reversible (server, account) identifier — see :mod:`profile_scope`."""
        return account_fingerprint(self.api_base, self.api_key)

    @property
    def integration_enabled(self) -> bool:
        return self.integration != INTEGRATION_OFF


def _setting(name: str, default: str = "") -> str:
    value = resolve_setting(name)
    if value is None:
        return default
    return value.strip()


def _setting_compat(name: str, legacy_name: str) -> tuple[str, str]:
    """``(value, variable name)``: the new name when set, else the old one.

    Returns ``("", name)`` when neither is set, so callers report the new
    name in errors and documentation.
    """
    value = _setting(name)
    if value:
        return value, name
    legacy = _setting(legacy_name)
    if legacy:
        return legacy, legacy_name
    return "", name


def settings_key() -> tuple[str, str] | None:
    """``(key, setting name)`` from the settings of the profile in scope, or ``None``.

    ``AGENTCHAT_API_KEY`` first, then the pre-v3 ``AGENTCHATME_API_KEY``,
    as every release before 0.6 read the key, through Hermes'
    ``get_secret`` (:func:`profile_scope.resolve_setting`). With a key file
    in use, ``AGENTCHAT_API_KEY`` there is the key file's own (the secret
    source supplies it), or empty for a masked copy.
    """
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        value = _setting(name)
        if value:
            return value, name
    return None


@dataclass(frozen=True)
class KeyResolution:
    """Which key this profile acts as, where it is, and what is wrong with it."""

    # The key to use; ``None`` when there is none or it is refused.
    api_key: str | None
    # ``KEY_SOURCE_STORE``, ``ENV_API_KEY``, ``ENV_API_KEY_LEGACY``, or ``""``.
    source: str
    store: key_store.StoreRead
    # A plain sentence when the key must not be used here (copied, unsafe).
    refusal: str | None = None
    # Warnings that do not stop it (a leftover pre-v3 setting, a moved key file).
    notes: tuple[str, ...] = ()
    # The key this profile holds, refused or not (doctor, logout); ``None``
    # when it is not known (a copy whose setting Hermes now masks). Never shown.
    held_key: str | None = None
    # Refused because the key is another profile's: that profile's home.
    copy_of: Path | None = None
    # Other profiles on this machine whose key files hold the same key as
    # this profile's key file (not refused: the same agent in two profiles,
    # said by status and doctor).
    shared_with: tuple[key_store.Holder, ...] = ()


def shared_note(holder: key_store.Holder) -> str:
    """One sentence about another profile holding this profile's key."""
    return (
        f"profile {profile_label(holder.home)} ({holder.home}) also holds this key in its "
        f"{holder.where}: the two profiles are one AgentChat agent. Run `{cli_prefix(holder.home)} "
        "logout` in the one that should not be this agent, then `register` it as its own"
    )


class ProfileHandles(NamedTuple):
    """:func:`profile_handles`: ``own`` is the handle this profile is
    configured as; ``leftover`` a handle that is not this profile's, never
    presented or pre-filled as its own; ``copy_of`` the profile whose key this
    one holds a refused copy of, when that is why; ``in_settings`` whether the
    leftover comes from the settings (``AGENTCHATME_HANDLE``) rather than a
    copied key file."""

    own: str | None
    leftover: str | None = None
    copy_of: Path | None = None
    in_settings: bool = True


def profile_handles(home: Path | None = None) -> ProfileHandles:
    """The handle this profile is configured as — its key file's, else
    ``AGENTCHATME_HANDLE`` when the profile holds a key of its own (a profile
    set up before 0.6) — and a leftover one: a handle the settings name while
    the profile holds no key, or the handle a refused copy of another
    profile's key names (a ``--clone-all`` key file, a copied ``.env``). A
    leftover is never presented or pre-filled as this profile's: it may be,
    or is, another profile's agent (recovering it here would rotate that
    profile's key) (:func:`leftover_handle_line`). Handles as typed: without
    ``@``, lower case."""
    target = home if home is not None else resolve_hermes_home()

    def typed(raw: str | None) -> str | None:
        return (raw or "").strip().lstrip("@").lower() or None

    read = key_store.read_store(target)
    in_file = typed(read.key.handle if read.key is not None else "")
    if read.usable and in_file is not None:
        return ProfileHandles(in_file)
    setting = typed(resolve_setting(ENV_HANDLE))
    resolution = resolve_key(target)
    if resolution.copy_of is not None:
        copied = in_file or setting
        return ProfileHandles(None, copied, resolution.copy_of, in_file is None) if copied else ProfileHandles(None)
    if setting is None:
        return ProfileHandles(None)
    if resolution.api_key or resolution.held_key:
        return ProfileHandles(setting)
    return ProfileHandles(None, setting)


def leftover_handle_line(handles: ProfileHandles, home: Path) -> str:
    """What is said of a leftover handle (:func:`profile_handles`), with the
    cleanup for this profile only."""
    label = profile_label(home)
    flag = "" if label == "default" else f"-p {label} "
    unset = f"`hermes {flag}config unset {ENV_HANDLE}`"
    if handles.copy_of is not None:
        where = f"its settings ({ENV_HANDLE})" if handles.in_settings else "the copied key file"
        return (
            f"this profile holds a refused copy of profile {profile_label(handles.copy_of)}'s AgentChat key, so the "
            f"handle {where} name (@{handles.leftover}) is that profile's agent, not this profile's: never recover "
            f"or log in as it here (that would act on that profile's agent). `{cli_prefix(home)} logout` removes "
            f"the copy{f', and {unset} the handle' if handles.in_settings else ''}; then `{cli_prefix(home)} register` "
            "makes this profile its own agent"
        )
    return (
        f"this profile holds no AgentChat key, but its settings still name a handle ({ENV_HANDLE}=@{handles.leftover}): "
        "a leftover setting — `hermes profile create` copies another profile's .env — and it may be another "
        f"profile's agent, not this profile's. Remove it for this profile only: {unset}"
    )


def cli_prefix(home: Path) -> str:
    """``hermes agentchat`` spelled for ``home``'s profile (``hermes -p NAME agentchat``)."""
    label = profile_label(home)
    return "hermes agentchat" if label == "default" else f"hermes -p {label} agentchat"


def resolve_key(home: Path | None = None) -> KeyResolution:
    """Which key ``home``'s profile acts as. Never raises for a key problem.

    1. The key file, when it belongs to this profile (or was moved here):
       it decides, whatever ``AGENTCHAT_API_KEY`` is set to elsewhere. A copy
       of another profile's key file is refused.
    2. Else this profile's settings (:func:`settings_key`), read only for the
       profile in scope (another home's settings are not this process's to
       read). A key another profile's key file holds is that profile's agent:
       refused, and remembered for the key source (:func:`copy_of`), which
       then gives this profile's hosted tools no key. A named profile that
       was never set up as an agent (:func:`was_set_up_as_agent`) holding
       the key the default profile resolved from its settings in this
       process is that profile's copy: refused. Profiles of this process
       resolving the same key are said to share it (``shared_with``), the
       root profile first.

    Raises :class:`~.profile_scope.ProfileScopeError` when called outside
    the profile's scope on a multiplexed gateway.
    """
    home = Path(home) if home is not None else profile_scope.resolve_hermes_home()
    cmd = cli_prefix(home)
    store = key_store.read_store(home)

    if store.status == "unsafe" and store.repairable:
        return KeyResolution(
            None,
            KEY_SOURCE_STORE,
            store,
            refusal=(
                f"{store.detail}. It is yours: `{cmd} doctor --fix` makes it yours only and keeps the key "
                f"(if you did not save that key yourself, `{cmd} recover` gets a new one instead)."
            ),
        )
    if store.status in ("unsafe", "unreadable"):
        return KeyResolution(
            None,
            KEY_SOURCE_STORE,
            store,
            refusal=f"{store.detail}. Fix or remove it, then run `{cmd} login`.",
        )
    if store.status == "copied" and store.key is not None:
        origin = store.origin_home or home
        return KeyResolution(
            None,
            KEY_SOURCE_STORE,
            store,
            refusal=(
                f"This profile's AgentChat key file ({store.path}) is a copy of profile "
                f"{profile_label(origin)}'s ({origin}). One key is one agent, and profile "
                f"{profile_label(origin)} is that agent. Run `{cmd} logout` to remove the copy from "
                f"this profile (profile {profile_label(origin)} keeps its key), then `{cmd} register` "
                "to give this profile its own agent."
            ),
            held_key=store.key.api_key,
            copy_of=origin,
        )
    if store.usable and store.key is not None:
        _forget_copy(home)
        _forget_settings_key(home)
        return _resolve_from_store(home, store, cmd)

    found = settings_key() if _in_scope(home) else None
    if found is None:
        _forget_settings_key(home)
        # Unset, or the copy's setting that the key source now supplies empty
        # (this process already found it): still refused while that profile
        # holds the key.
        found_owner = copy_of(home)
        if found_owner is not None:
            return _copy_refusal(home, store, found_owner, None)
        return KeyResolution(None, "", store)
    value, name = found
    root = hermes_root_of(home)
    holders = key_store.other_holders(home, value, root=root)
    if holders:
        owner = holders[0].home
        _remember_copy(home, owner, value)
        _forget_settings_key(home)
        return _copy_refusal(home, store, owner, value, source=name)
    _forget_copy(home)
    if (
        not key_store.same_home(home, root)
        and _settings_key_of(root) == key_store.fingerprint(value)
        and not was_set_up_as_agent(home)
    ):
        # The default profile resolved the same key in this process, and this
        # named profile was never set up as an agent: what `hermes profile
        # create` makes of the default profile's .env. Refused, nothing placed.
        _forget_settings_key(home)
        return _default_copy_refusal(home, store, value, root, source=name)
    notes: list[str] = []
    if name == ENV_API_KEY:
        legacy = _setting(ENV_API_KEY_LEGACY)
        if legacy and legacy != value:
            notes.append(
                f"{ENV_API_KEY} and {ENV_API_KEY_LEGACY} in this profile's settings hold different "
                f"keys — the plugin uses {ENV_API_KEY}; remove the wrong one"
            )
    # Profiles of this process holding the same key in their settings (a
    # multiplexed gateway resolves each in its own scope): one agent, said.
    shared = tuple(key_store.Holder(other, "settings") for other in _remember_settings_key(home, value))
    return KeyResolution(value, name, store, notes=tuple(notes), held_key=value, shared_with=shared)


def _default_copy_refusal(
    home: Path, store: key_store.StoreRead, value: str, root: Path, *, source: str
) -> KeyResolution:
    cmd = cli_prefix(home)
    return KeyResolution(
        None,
        source,
        store,
        refusal=(
            f"This profile's settings hold the same AgentChat key as the default profile ({root}), and "
            "this profile was never set up as an AgentChat agent (no identity line in its SOUL.md, no "
            "hosted-tools entry in its config.yaml) — `hermes profile create` copies .env into new "
            "profiles. One key is one agent, and the default profile is that agent. Run "
            f"`{cmd} logout` to remove the copy from this profile (the default profile keeps its key), "
            f"then `{cmd} register` to give this profile its own agent. If this profile is meant to be "
            f"its own agent with this key, run `{cmd} login` with it."
        ),
        held_key=value,
        copy_of=root,
    )


def was_set_up_as_agent(home: Path) -> bool:
    """Has this profile ever had the integration placed: an identity line in its
    SOUL.md, or a hosted-tools entry in its ``config.yaml``? (Never its ``.env``.)"""
    from .soul_anchor import ANCHOR_START

    for path, markers in (
        (home / "SOUL.md", (ANCHOR_START,)),
        (home / "config.yaml", ("${AGENTCHAT_API_KEY}", "${env:AGENTCHAT_API_KEY}")),
    ):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in text for marker in markers):
            return True
    return False


def _copy_refusal(
    home: Path, store: key_store.StoreRead, owner: Path, value: str | None, *, source: str = ""
) -> KeyResolution:
    cmd, owner_cmd = cli_prefix(home), cli_prefix(owner)
    return KeyResolution(
        None,
        source,
        store,
        refusal=(
            f"This profile's settings hold the same AgentChat key that profile {profile_label(owner)} "
            f"({owner}) saved in its key file — `hermes profile create` copies .env into new "
            f"profiles. One key is one agent, and profile {profile_label(owner)} is that agent. Run "
            f"`{cmd} logout` to remove the copy from this profile (profile {profile_label(owner)} "
            f"keeps its key), then `{cmd} register` to give this profile its own agent. If this "
            f"profile should be that agent instead, run `{owner_cmd} logout` first."
        ),
        held_key=value,
        copy_of=owner,
    )


def _in_scope(home: Path) -> bool:
    """Is ``home`` the profile this code runs for (whose settings ``get_secret`` answers)?"""
    return key_store.same_home(home, profile_scope.resolve_hermes_home())


# ─── copies found in this process ────────────────────────────────────────────
#
# A profile whose settings hold a key another profile's key file holds (a
# ``.env`` copied by ``hermes profile create``) is that other profile's agent.
# :func:`resolve_key` finds it in a scoped run — every process that loads the
# plugin resolves the key in ``register()``, before Hermes asks the key source
# — and the key source then supplies this profile's hosted tools an empty key
# (:mod:`.key_source`), from what is recorded here and the key files alone.
# Kept in the process, never written: each process finds it again.


@dataclass(frozen=True)
class _Copy:
    origin: Path
    key_fingerprint: str


_copies: dict[str, _Copy] = {}
_copies_lock = threading.Lock()


def _copy_key(home: Path) -> str:
    return profile_scope.hermes_home_key(Path(home))


def _remember_copy(home: Path, origin: Path, api_key: str) -> None:
    with _copies_lock:
        _copies[_copy_key(home)] = _Copy(Path(origin), key_store.fingerprint(api_key))


def _forget_copy(home: Path) -> None:
    with _copies_lock:
        _copies.pop(_copy_key(home), None)


def forget_copy(home: Path) -> None:
    """Drop what this process found about ``home``'s key (logout)."""
    _forget_copy(home)
    _forget_settings_key(home)


def copy_of(home: Path) -> Path | None:
    """The profile whose key ``home``'s settings hold a copy of, or ``None``.

    As :func:`resolve_key` last found it in this process, and only while
    that profile's key file still holds the key. Reads no settings: the key
    source calls it while Hermes resolves secrets.
    """
    with _copies_lock:
        found = _copies.get(_copy_key(home))
    if found is None:
        return None
    if not key_store.holds_fingerprint(found.origin, found.key_fingerprint):
        _forget_copy(home)
        return None
    return found.origin


# The key each profile of this process resolved from its settings (its
# fingerprint), so two profiles one multiplexed gateway serves with the same
# key are told they are one agent, the root profile first.
_settings_keys: dict[str, tuple[Path, str]] = {}


def _remember_settings_key(home: Path, api_key: str) -> list[Path]:
    """Record ``home``'s settings key; return the other homes of this process holding it,
    the root profile first."""
    fp = key_store.fingerprint(api_key)
    with _copies_lock:
        _settings_keys[_copy_key(home)] = (Path(home), fp)
        others = [h for key, (h, other_fp) in _settings_keys.items() if other_fp == fp and key != _copy_key(home)]
    return sorted(others, key=lambda h: (not key_store.same_home(h, hermes_root_of(h)), str(h)))


def _settings_key_of(home: Path) -> str | None:
    """The fingerprint of the key ``home`` resolved from its settings in this process."""
    with _copies_lock:
        found = _settings_keys.get(_copy_key(home))
    return found[1] if found is not None else None


def _forget_settings_key(home: Path) -> None:
    with _copies_lock:
        _settings_keys.pop(_copy_key(home), None)


def reset_copies_for_tests() -> None:
    with _copies_lock:
        _copies.clear()
        _settings_keys.clear()


def _resolve_from_store(home: Path, store: key_store.StoreRead, cmd: str) -> KeyResolution:
    assert store.key is not None
    key = store.key.api_key
    notes: list[str] = []
    # The pre-v3 name is never supplied by the key source, so Hermes' value
    # for it is the profile's own setting: a copy an earlier release left.
    legacy = _setting(ENV_API_KEY_LEGACY) if _in_scope(home) else ""
    if legacy == key:
        notes.append(
            f"this profile's settings still hold the key as {ENV_API_KEY_LEGACY} (the pre-v3 name, "
            "from the profile .env or the environment), where the agent's commands can read it: "
            f"`{cmd} doctor --fix` removes it from the profile .env"
        )
    elif legacy:
        notes.append(
            f"{ENV_API_KEY_LEGACY} in this profile's settings holds a different key; it is not "
            "used (the key file decides) — remove it if you do not need it"
        )
    if store.status == "moved":
        notes.append(
            f"the key file was saved for {store.origin_home}, which no longer holds it (the profile "
            f"was renamed or moved): `{cmd} doctor --fix` records this profile in it"
        )
    holders = tuple(key_store.other_holders(home, key, root=hermes_root_of(home)))
    return KeyResolution(
        key, KEY_SOURCE_STORE, store, notes=tuple(notes), held_key=key, shared_with=holders
    )


def resolve_api_key(home: Path | None = None) -> tuple[str, str] | None:
    """``(key, source)`` for the current profile, or ``None`` when there is none or it is refused."""
    resolved = resolve_key(home)
    if resolved.api_key is None:
        return None
    return resolved.api_key, resolved.source


def load_config(*, hermes_home: Path | None = None) -> Config | None:
    """Build a :class:`Config` for the current (or given) Hermes profile.

    Returns ``None`` when the profile has no key — the plugin runs in
    *CLI-only* mode (the wizard is reachable, nothing connects). Once the
    wizard saves a key the gateway's activation watcher picks it up without
    a restart.

    Args:
        hermes_home: the profile home to bind the configuration to.
            Defaults to :func:`profile_scope.resolve_hermes_home`, which
            inside Hermes is the profile whose ``register(ctx)`` is
            running.

    Raises:
        KeyRefused: the profile holds a key it must not act as (see
            :func:`resolve_key`); the message says what to do.
        ConfigError: when a value is present but malformed (e.g.,
            ``AGENTCHATME_API_BASE`` is not a valid URL, or a numeric
            knob is not parseable).
    """
    home = hermes_home if hermes_home is not None else profile_scope.resolve_hermes_home()
    resolution = resolve_key(home)
    if resolution.refusal is not None:
        raise KeyRefused(resolution.refusal)
    if resolution.api_key is None:
        return None
    api_key, api_key_source = resolution.api_key, resolution.source

    stored_base = resolution.store.key.api_base if resolution.store.key is not None else ""
    api_base = resolve_api_base(stored_base)

    ws_url_raw = _setting(ENV_WS_URL)
    if ws_url_raw:
        ws_url = _normalize_url(ws_url_raw, scheme_allowed=("ws", "wss"))
        if ws_url is None:
            raise ConfigError(f"{ENV_WS_URL}={ws_url_raw!r} is not a valid ws(s) URL")
    else:
        ws_url = _derive_ws_url(api_base)

    integration = _parse_integration()
    wake_debounce_s = _parse_float_compat(
        ENV_WAKE_DEBOUNCE_S, ENV_WAKE_DEBOUNCE_S_LEGACY, DEFAULT_WAKE_DEBOUNCE_S, minimum=0.0
    )
    reply_gate_enabled = _parse_bool_env(ENV_REPLY_GATE_ENABLED, DEFAULT_REPLY_GATE_ENABLED)
    _warn_retired_fail_open()
    gate_timeout_s = _parse_float_env(ENV_GATE_TIMEOUT_S, DEFAULT_GATE_TIMEOUT_S, minimum=1.0)
    turn_idle_s = _parse_float_env(ENV_TURN_IDLE_S, DEFAULT_TURN_IDLE_S, minimum=0.0)
    turn_max_s = _parse_float_env(ENV_TURN_MAX_S, DEFAULT_TURN_MAX_S, minimum=0.0)
    if not turn_idle_s and not turn_max_s:
        logger.warning(
            "agentchat: %s=0 and %s=0 — turns have no time limit; one turn that never ends "
            "stops this account's wakes until the gateway restarts",
            ENV_TURN_IDLE_S,
            ENV_TURN_MAX_S,
        )

    lock_dir_raw = _setting(ENV_LOCK_DIR)
    lock_dir = Path(lock_dir_raw).expanduser() if lock_dir_raw else None
    state_dir_raw = _setting(ENV_STATE_DIR)
    state_dir = Path(state_dir_raw).expanduser() if state_dir_raw else None

    return Config(
        api_key=api_key,
        api_base=api_base,
        ws_url=ws_url,
        api_key_source=api_key_source,
        integration=integration,
        wake_debounce_s=wake_debounce_s,
        reply_gate_enabled=reply_gate_enabled,
        gate_timeout_s=gate_timeout_s,
        turn_idle_s=turn_idle_s,
        turn_max_s=turn_max_s,
        hermes_home=home,
        lock_dir=lock_dir,
        state_dir=state_dir,
        key_notes=(*resolution.notes, *(shared_note(h) for h in resolution.shared_with)),
    )


def resolve_api_base(stored: str = "", *, strict: bool = True) -> str:
    """The AgentChat API base for the profile in scope: the one resolver.

    ``AGENTCHATME_API_BASE`` when set, else the base the key file was saved
    with (``stored``), else the default; normalised (no trailing slash).
    ``strict``: an invalid URL raises :class:`ConfigError` (``load_config``);
    otherwise it is returned as given, stripped, so a CLI command still runs
    and its request says what is wrong.
    """
    raw = _setting(ENV_API_BASE) or stored.strip() or DEFAULT_API_BASE
    normalized = _normalize_url(raw, scheme_allowed=("http", "https"))
    if normalized is not None:
        return normalized
    if strict:
        raise ConfigError(f"{ENV_API_BASE}={raw!r} is not a valid http(s) URL")
    return raw.rstrip("/")


def stored_api_base(home: Path | None = None) -> str:
    """The base ``home``'s key file was saved with (``""`` without a usable one)."""
    read = key_store.read_store(Path(home) if home is not None else profile_scope.resolve_hermes_home())
    return read.key.api_base if read.usable and read.key is not None else ""


def _parse_integration() -> str:
    raw, source = _setting_compat(ENV_INTEGRATION, ENV_INTEGRATION_LEGACY)
    value = raw.lower()
    if not value:
        return DEFAULT_INTEGRATION
    if source == ENV_INTEGRATION_LEGACY:
        mapped = _INTEGRATION_LEGACY_VALUES.get(value)
        if mapped is not None:
            return mapped
        hint = (
            " The native tool layer and per-message gate were removed in 0.3.0; "
            "pin agentchatme-hermes==0.2.3111111 to stay on them."
            if value == "v2"
            else ""
        )
        raise ConfigError(
            f"{ENV_INTEGRATION_LEGACY}={value!r} is not one of: "
            f"{', '.join(sorted(_INTEGRATION_LEGACY_VALUES))} "
            f"(the current name is {ENV_INTEGRATION}=on|off).{hint}"
        )
    if value in _INTEGRATION_VALUES:
        return value
    raise ConfigError(
        f"{ENV_INTEGRATION}={value!r} is not one of: {', '.join(sorted(_INTEGRATION_VALUES))}."
    )


def _warn_retired_fail_open() -> None:
    global _fail_open_warned
    if _fail_open_warned or not _setting(ENV_REPLY_GATE_FAIL_OPEN):
        return
    _fail_open_warned = True
    logger.warning(
        "agentchat: %s is set but no longer does anything — the gate fails closed: when the "
        "model cannot be reached nothing is recorded, no turn starts, the cards stay on the "
        "tray and the server wakes the agent again. Remove the setting.",
        ENV_REPLY_GATE_FAIL_OPEN,
    )


def _normalize_url(raw: str, *, scheme_allowed: tuple[str, ...]) -> str | None:
    """Validate and canonicalize a URL.

    Returns ``None`` if invalid. Strips trailing slashes from the path
    so downstream string concatenation produces consistent URLs.
    """
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in scheme_allowed or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    return urlunparse(parsed._replace(path=path))


def _derive_ws_url(api_base: str) -> str:
    """Map ``https://host`` → ``wss://host``, ``http://`` → ``ws://``."""
    parsed = urlparse(api_base)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(parsed._replace(scheme=ws_scheme))


def _parse_float_env(name: str, default: float, *, minimum: float) -> float:
    return _parse_float(name, _setting(name), default, minimum=minimum)


def _parse_float_compat(name: str, legacy_name: str, default: float, *, minimum: float) -> float:
    raw, source = _setting_compat(name, legacy_name)
    return _parse_float(source, raw, default, minimum=minimum)


def _parse_float(name: str, raw: str, default: float, *, minimum: float) -> float:
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} is below the minimum of {minimum}")
    return value


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = _setting(name).lower()
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    raise ConfigError(
        f"{name}={raw!r} is not a boolean (use one of: "
        f"{', '.join(sorted(_TRUE_TOKENS | _FALSE_TOKENS))})"
    )

