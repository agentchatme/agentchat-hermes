"""Runtime configuration, resolved per Hermes profile.

Configuration is read once at plugin load, for the profile whose
``register(ctx)`` is running. Every value goes through
:func:`profile_scope.resolve_setting`, so a multiplexed gateway serving
several profiles gets each profile's own key — never the process
environment of whichever profile launched the gateway. Changing a value
after Hermes has started has no effect — the user must restart Hermes
(the same constraint every other Hermes plugin has, by design).

Variable names
──────────────
The key is ``AGENTCHAT_API_KEY`` — the name the published manual and the
hosted-MCP declaration use (``Bearer ${AGENTCHAT_API_KEY}``). The pre-v3
name ``AGENTCHATME_API_KEY`` is still honoured as a fallback and mirrored
on first activation. Behaviour knobs keep their ``AGENTCHATME_`` prefix.

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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .profile_scope import account_fingerprint, resolve_hermes_home, resolve_setting

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

# Timeout (seconds) for the one decision call. It judges a whole tray (up to
# 50 cards) in one answer, not one message.
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


@dataclass(frozen=True)
class Config:
    """Resolved plugin configuration.

    All fields are required and validated at construction. Use
    :func:`load_config` to construct from the profile's settings — it
    returns ``None`` when no API key is set, which lets the plugin
    register the CLI wizard even before the user has set up an account.
    """

    api_key: str
    api_base: str
    ws_url: str
    # Which variable the key came from (``AGENTCHAT_API_KEY`` or the legacy
    # name). Drives the one-time mirror at activation and ``doctor``.
    api_key_source: str = ENV_API_KEY
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


def resolve_api_key() -> tuple[str, str] | None:
    """``(key, variable name)`` for the current profile, or ``None`` when unset.

    ``AGENTCHAT_API_KEY`` wins; ``AGENTCHATME_API_KEY`` is the fallback for
    profiles configured before v3.
    """
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY):
        value = _setting(name)
        if value:
            return value, name
    return None


def load_config(*, hermes_home: Path | None = None) -> Config | None:
    """Build a :class:`Config` for the current (or given) Hermes profile.

    Returns ``None`` when no API key is set for that profile — the plugin
    runs in *CLI-only* mode (the wizard is reachable, nothing connects).
    Once the wizard persists a key the gateway's activation watcher picks
    it up without a restart.

    Args:
        hermes_home: the profile home to bind the configuration to.
            Defaults to :func:`profile_scope.resolve_hermes_home`, which
            inside Hermes is the profile whose ``register(ctx)`` is
            running.

    Raises:
        ConfigError: when a value is present but malformed (e.g.,
            ``AGENTCHATME_API_BASE`` is not a valid URL, or a numeric
            knob is not parseable).
    """
    resolved = resolve_api_key()
    if resolved is None:
        return None
    api_key, api_key_source = resolved

    api_base_raw = _setting(ENV_API_BASE, DEFAULT_API_BASE) or DEFAULT_API_BASE
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
        hermes_home=hermes_home if hermes_home is not None else resolve_hermes_home(),
        lock_dir=lock_dir,
        state_dir=state_dir,
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
