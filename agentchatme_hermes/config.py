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
through the plugin's secret source (:mod:`.key_source`). A profile set up
before 0.6 still has it in the profile ``.env`` as ``AGENTCHAT_API_KEY`` (or
the pre-v3 ``AGENTCHATME_API_KEY``): that is read as a fallback until
``hermes agentchat doctor --fix`` (or a login) moves it. :func:`resolve_key`
also refuses a key this profile must not act as — a copy of another
profile's, or an ``AGENTCHAT_API_KEY`` that disagrees with the saved key —
with a sentence that says what to do. Behaviour knobs keep their
``AGENTCHATME_`` prefix and are read as before.

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
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import key_store, profile_scope
from .profile_scope import (
    account_fingerprint,
    dotenv_value,
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
# the key file, or the name of the variable it was read from.
KEY_SOURCE_STORE = "key file"

# ``AGENTCHAT_API_KEY`` as an *export* (the shell, a service's ``Environment=``)
# when the plugin was first imported: before Hermes re-pulls the plugin's
# secret source after discovery (which overwrites it with the saved key), so an
# exported key that disagrees with the saved one is still seen, and refused,
# for the process's whole life. A value equal to the process profile's ``.env``
# line is Hermes' own dotenv load, not an export, and is not kept: the file is
# read as it is now instead, so removing the line takes effect without a restart.
_PROCESS_ENV_AT_IMPORT: dict[str, str | None] = {}


def _snapshot_process_env() -> None:
    env_file = _process_home() / ".env"
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        if name in _PROCESS_ENV_AT_IMPORT:
            continue
        value = (os.environ.get(name) or "").strip()
        if value and value == (dotenv_value(env_file, name) or "").strip():
            value = ""
        _PROCESS_ENV_AT_IMPORT[name] = value or None


def reset_process_env_snapshot_for_tests() -> None:
    _PROCESS_ENV_AT_IMPORT.clear()
    _snapshot_process_env()


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
    # profile holds the same key, a leftover .env copy); logged at activation.
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


def _env_api_key(home: Path) -> tuple[str, str, str] | None:
    """``(key, variable name, where)`` for a profile not moved to the key file yet.

    ``home``'s own ``.env`` file first (``where`` = ``".env"``), then the
    profile's settings as Hermes resolves them for the running profile (its
    secret scope, the process environment: ``"environment"``), as every
    release before 0.6 read the key.
    """
    env_file = home / ".env"
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        value = (dotenv_value(env_file, name) or "").strip()
        if value:
            return value, name, ".env"
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        value = _setting(name)
        if value:
            where = ".env" if (dotenv_value(env_file, name) or "").strip() == value else "environment"
            return value, name, where
    return None


@dataclass(frozen=True)
class KeyResolution:
    """Which key this profile acts as, where it is, and what is wrong with it."""

    # The key to use; ``None`` when there is none or it is refused.
    api_key: str | None
    # ``KEY_SOURCE_STORE``, ``ENV_API_KEY``, ``ENV_API_KEY_LEGACY``, or ``""``.
    source: str
    store: key_store.StoreRead
    # A plain sentence when the key must not be used here (copied, conflicting, unsafe).
    refusal: str | None = None
    # Warnings that do not stop it (another profile holds it too, a leftover .env copy).
    notes: tuple[str, ...] = ()
    # The key this profile holds, refused or not (doctor, logout). Never shown.
    held_key: str | None = None
    # For a key read from the settings: ``".env"`` (the profile's file) or ``"environment"``.
    env_location: str = ""
    # Refused because the key is another profile's: that profile's home.
    copy_of: Path | None = None
    # Other profiles on this machine holding the same key (not refused: the
    # same agent in two profiles, said by status and doctor).
    shared_with: tuple[key_store.Holder, ...] = ()


def shared_note(holder: key_store.Holder) -> str:
    """One sentence about another profile holding this profile's key."""
    return (
        f"profile {profile_label(holder.home)} ({holder.home}) also holds this key in its "
        f"{holder.where}: the two profiles are one AgentChat agent. Run `{cli_prefix(holder.home)} "
        "logout` in the one that should not be this agent, then `register` it as its own"
    )


def cli_prefix(home: Path) -> str:
    """``hermes agentchat`` spelled for ``home``'s profile (``hermes -p NAME agentchat``)."""
    label = profile_label(home)
    return "hermes agentchat" if label == "default" else f"hermes -p {label} agentchat"


def resolve_key(home: Path | None = None) -> KeyResolution:
    """Which key ``home``'s profile acts as. Never raises.

    1. The key file, when it belongs to this profile (or was moved here). A
       copy of another profile's key file is refused. Any other value of
       ``AGENTCHAT_API_KEY`` this profile sees — a line in its ``.env``, or the
       process environment as it was when the plugin loaded (an export, a
       service's ``Environment=``) — that disagrees with it is refused: the
       saved key decides which agent this is, and a second key is never
       quietly preferred or ignored.
    2. Else the settings (``.env``, then the environment), as before 0.6. A
       ``.env`` key that is certainly a copy of another profile's (see
       :func:`_dotenv_copy_owner`) is refused; one merely shared is kept and
       said (``shared_with``).
    """
    home = Path(home) if home is not None else profile_scope.resolve_hermes_home()
    cmd = cli_prefix(home)
    store = key_store.read_store(home)

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
        return _resolve_from_store(home, store, cmd)

    found = _env_api_key(home)
    if found is None:
        return KeyResolution(None, "", store)
    value, name, location = found
    holders = tuple(
        key_store.other_holders(
            home, value, root=hermes_root_of(home), env_names=(ENV_API_KEY, ENV_API_KEY_LEGACY)
        )
    )
    owner = _dotenv_copy_owner(home, holders) if location == ".env" else None
    if owner is not None:
        owner_cmd = cli_prefix(owner)
        return KeyResolution(
            None,
            name,
            store,
            refusal=(
                f"This profile's .env holds the same AgentChat key as profile {profile_label(owner)} "
                f"({owner}) — `hermes profile create` copies .env into new profiles. One key is one "
                f"agent, and profile {profile_label(owner)} is that agent. Run `{cmd} logout` to "
                f"remove the copy from this profile (profile {profile_label(owner)} keeps its key), "
                f"then `{cmd} register` to give this profile its own agent. If this profile should "
                f"be that agent instead, run `{owner_cmd} logout` first."
            ),
            held_key=value,
            env_location=location,
            copy_of=owner,
        )
    notes: list[str] = []
    if location == ".env":
        canonical = (dotenv_value(home / ".env", ENV_API_KEY) or "").strip()
        legacy = (dotenv_value(home / ".env", ENV_API_KEY_LEGACY) or "").strip()
        if canonical and legacy and canonical != legacy:
            notes.append(
                f"{ENV_API_KEY} and {ENV_API_KEY_LEGACY} in the profile .env hold different keys — "
                f"the plugin uses {ENV_API_KEY}; remove the wrong line"
            )
    return KeyResolution(
        value,
        name,
        store,
        notes=tuple(notes),
        held_key=value,
        env_location=location,
        shared_with=holders,
    )


def _dotenv_copy_owner(home: Path, holders: tuple[key_store.Holder, ...]) -> Path | None:
    """The profile whose key this profile's ``.env`` holds a copy of, or ``None``.

    Before 0.6 the key was in ``.env`` and ``hermes profile create`` copied
    it, so two profiles holding one key cannot always be told apart from
    their files. Refused as a copy only where it is certain:

    * another profile saved the key in its key file (an explicit claim by
      ``login`` / ``register`` / ``recover`` / ``doctor --fix``);
    * the default profile holds it and this profile was never set up as an
      AgentChat agent: no identity line in its SOUL.md and no hosted-tools
      entry in its ``config.yaml`` (a plain ``profile create`` copies only
      ``.env``).

    Anything else (a ``--clone`` copies SOUL.md and ``config.yaml`` too, and
    two set-up profiles may each be the one in use) is the same agent in two
    profiles: it keeps working as before, and status and doctor say so.
    """
    for holder in holders:
        if holder.where == "key file":
            return holder.home
    root = hermes_root_of(home)
    if key_store.same_home(home, root):
        return None
    if not any(key_store.same_home(h.home, root) for h in holders):
        return None
    return None if _was_set_up_as_agent(home) else root


def _was_set_up_as_agent(home: Path) -> bool:
    """Has this profile ever had the integration placed (identity line or tools entry)?"""
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


def copy_owner(home: Path) -> Path | None:
    """The profile this profile's key is a copy of, or ``None`` — from the files alone.

    For the key source, which must hand Hermes no key for a copied profile;
    cheaper than :func:`resolve_key` (no settings, no environment).
    """
    store = key_store.read_store(home)
    if store.status == "copied":
        return store.origin_home
    if store.status != "missing":
        return None
    env_file = home / ".env"
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        value = (dotenv_value(env_file, name) or "").strip()
        if value:
            holders = tuple(
                key_store.other_holders(
                    home, value, root=hermes_root_of(home), env_names=(ENV_API_KEY, ENV_API_KEY_LEGACY)
                )
            )
            return _dotenv_copy_owner(home, holders)
    return None


def _resolve_from_store(home: Path, store: key_store.StoreRead, cmd: str) -> KeyResolution:
    assert store.key is not None
    key = store.key.api_key
    env_file = home / ".env"
    conflicts: list[str] = []
    leftovers: list[str] = []
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        in_file = (dotenv_value(env_file, name) or "").strip()
        if in_file and in_file != key:
            conflicts.append(f"{name} in {env_file}")
        elif in_file:
            leftovers.append(name)
        exported = _process_env_value(home, name)
        if exported and exported != key and exported != in_file:
            conflicts.append(f"{name} in this process's environment (exported in the shell or set by the service)")
    if conflicts:
        handle = f" (@{store.key.handle})" if store.key.handle else ""
        return KeyResolution(
            None,
            KEY_SOURCE_STORE,
            store,
            refusal=(
                f"{' and '.join(conflicts)} is not the key this profile saved in {store.path}"
                f"{handle}. The saved key decides which agent this profile is, and another key is "
                f"never used in its place: remove or unset the other one, or save it for this "
                f"profile instead with `{cmd} login --api-key-file <file>`."
            ),
            held_key=key,
        )
    notes: list[str] = []
    if leftovers:
        notes.append(
            f"the profile .env still holds a copy of the key ({', '.join(leftovers)}): "
            f"`{cmd} doctor --fix` removes it"
        )
    if store.status == "moved":
        notes.append(
            f"the key file was saved for {store.origin_home}, which no longer holds it (the profile "
            f"was renamed or moved): `{cmd} doctor --fix` records this profile in it"
        )
    holders = tuple(
        key_store.other_holders(
            home, key, root=hermes_root_of(home), env_names=(ENV_API_KEY, ENV_API_KEY_LEGACY)
        )
    )
    return KeyResolution(
        key, KEY_SOURCE_STORE, store, notes=tuple(notes), held_key=key, shared_with=holders
    )


def _process_env_value(home: Path, name: str) -> str:
    """``name`` in the process environment as the plugin first saw it — for the process's own profile.

    A profile a multiplexed gateway serves for another home is not the
    process's own; the launch profile's environment says nothing about it.
    """
    if not _is_process_home(home):
        return ""
    _snapshot_process_env()
    value = (_PROCESS_ENV_AT_IMPORT.get(name) or "").strip()
    if not value:
        return ""
    from .key_source import supplied_by_us

    current = (os.environ.get(name) or "").strip()
    if current == value and supplied_by_us(name):
        return ""  # our own source's value from an earlier load in this process
    return value


def _process_home() -> Path:
    """The profile home this process was started for (Hermes' launch home)."""
    try:
        from hermes_constants import (  # type: ignore[import-not-found]
            get_process_hermes_home,
        )

        return Path(get_process_hermes_home())
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _is_process_home(home: Path) -> bool:
    return key_store.same_home(_process_home(), home)


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
    api_base_raw = _setting(ENV_API_BASE) or stored_base or DEFAULT_API_BASE
    api_base = _normalize_url(api_base_raw, scheme_allowed=("http", "https"))
    if api_base is None:
        raise ConfigError(f"{ENV_API_BASE}={api_base_raw!r} is not a valid http(s) URL")

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


_snapshot_process_env()
