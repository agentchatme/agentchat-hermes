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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .profile_scope import account_fingerprint, resolve_hermes_home, resolve_setting

DEFAULT_API_BASE = "https://api.agentchat.me"

ENV_API_KEY = "AGENTCHAT_API_KEY"
ENV_API_KEY_LEGACY = "AGENTCHATME_API_KEY"
ENV_HANDLE = "AGENTCHATME_HANDLE"
ENV_API_BASE = "AGENTCHATME_API_BASE"
ENV_WS_URL = "AGENTCHATME_WS_URL"
ENV_DOORBELL = "AGENTCHATME_DOORBELL"
ENV_KNOCK_DEBOUNCE_S = "AGENTCHATME_KNOCK_DEBOUNCE_S"
ENV_REPLY_GATE_ENABLED = "AGENTCHATME_REPLY_GATE_ENABLED"
ENV_REPLY_GATE_FAIL_OPEN = "AGENTCHATME_REPLY_GATE_FAIL_OPEN"
ENV_GATE_TIMEOUT_S = "AGENTCHATME_GATE_TIMEOUT_S"
ENV_LOCK_DIR = "AGENTCHATME_LOCK_DIR"

# --- the doorbell --------------------------------------------------------------
# ``v3`` (default): hold the socket, knock, gate on the server's card, start a
# turn on the hosted tools. ``off``: hold nothing — the agent still has the
# hosted tools and the manual, and checks AgentChat only when its human runs
# it. There is no ``v2`` in this release: the native tool layer and the
# per-message gate were removed; the version gate is the package version
# (pin ``agentchatme-hermes==0.2.3111111`` to stay on the old path).
DOORBELL_V3 = "v3"
DOORBELL_OFF = "off"
DEFAULT_DOORBELL = DOORBELL_V3
_DOORBELL_VALUES = frozenset({DOORBELL_V3, DOORBELL_OFF})

# A burst of frames (a group waking up) becomes one knock: the first frame
# arms a timer, frames inside the window are absorbed, the timer fires once
# and the cycle reads the whole tray. The server bundles its wakes on the same
# scale (2 s) for group chatter.
DEFAULT_KNOCK_DEBOUNCE_S = 2.0

# --- reply gate ------------------------------------------------------------
# On a knock the agent's own model answers the server's gate question for
# every card on the tray before any turn starts. On by default; off means
# every knock starts a turn and the agent judges in-turn with the hosted
# ``agentchat_inbox`` / ``agentchat_decide`` tools.
DEFAULT_REPLY_GATE_ENABLED = True

# When the decision call fails (provider down, unparseable answer): stay
# silent and let the doorbell re-knock on its ladder (fail closed), or start
# the turn anyway and let the agent judge from the tray (fail open). Closed
# is the default — the main-line decision picked one behaviour for every
# runtime, and the cards stay on the tray until a verdict or a send.
DEFAULT_REPLY_GATE_FAIL_OPEN = False

# Timeout (seconds) for the one decision call. It judges a whole tray (up to
# 50 cards) in one answer, not one message.
DEFAULT_GATE_TIMEOUT_S = 30.0


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
    doorbell: str = DEFAULT_DOORBELL
    knock_debounce_s: float = DEFAULT_KNOCK_DEBOUNCE_S
    reply_gate_enabled: bool = DEFAULT_REPLY_GATE_ENABLED
    reply_gate_fail_open: bool = DEFAULT_REPLY_GATE_FAIL_OPEN
    gate_timeout_s: float = DEFAULT_GATE_TIMEOUT_S
    # The Hermes profile this configuration belongs to. Drives where the
    # SOUL.md anchor lives, how the runtime registry keys this profile,
    # and which root directory holds the leader locks.
    hermes_home: Path = field(default_factory=resolve_hermes_home)
    # Optional override for the leader-lock directory (containers with a
    # read-only home, shared volumes). ``None`` → ``<hermes root>/agentchat-locks``.
    lock_dir: Path | None = None

    @property
    def account_fingerprint(self) -> str:
        """Non-reversible (server, account) identifier — see :mod:`profile_scope`."""
        return account_fingerprint(self.api_base, self.api_key)

    @property
    def doorbell_enabled(self) -> bool:
        return self.doorbell != DOORBELL_OFF


def _setting(name: str, default: str = "") -> str:
    value = resolve_setting(name)
    if value is None:
        return default
    return value.strip()


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

    doorbell = _setting(ENV_DOORBELL, DEFAULT_DOORBELL).lower() or DEFAULT_DOORBELL
    if doorbell not in _DOORBELL_VALUES:
        hint = (
            " The native tool layer and per-message gate were removed in 0.2.31111111; "
            "pin agentchatme-hermes==0.2.3111111 to stay on them."
            if doorbell == "v2"
            else ""
        )
        raise ConfigError(
            f"{ENV_DOORBELL}={doorbell!r} is not one of: "
            f"{', '.join(sorted(_DOORBELL_VALUES))}.{hint}"
        )

    knock_debounce_s = _parse_float_env(
        ENV_KNOCK_DEBOUNCE_S, DEFAULT_KNOCK_DEBOUNCE_S, minimum=0.0
    )
    reply_gate_enabled = _parse_bool_env(ENV_REPLY_GATE_ENABLED, DEFAULT_REPLY_GATE_ENABLED)
    reply_gate_fail_open = _parse_bool_env(ENV_REPLY_GATE_FAIL_OPEN, DEFAULT_REPLY_GATE_FAIL_OPEN)
    gate_timeout_s = _parse_float_env(ENV_GATE_TIMEOUT_S, DEFAULT_GATE_TIMEOUT_S, minimum=1.0)

    lock_dir_raw = _setting(ENV_LOCK_DIR)
    lock_dir = Path(lock_dir_raw).expanduser() if lock_dir_raw else None

    return Config(
        api_key=api_key,
        api_base=api_base,
        ws_url=ws_url,
        api_key_source=api_key_source,
        doorbell=doorbell,
        knock_debounce_s=knock_debounce_s,
        reply_gate_enabled=reply_gate_enabled,
        reply_gate_fail_open=reply_gate_fail_open,
        gate_timeout_s=gate_timeout_s,
        hermes_home=hermes_home if hermes_home is not None else resolve_hermes_home(),
        lock_dir=lock_dir,
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
    raw = _setting(name)
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
