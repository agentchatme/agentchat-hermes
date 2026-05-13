"""Runtime configuration loaded from environment variables.

Configuration is read once at plugin load. Changing an env var after
Hermes has started has no effect — the user must restart Hermes (the
same constraint every other Hermes plugin has, by design).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

DEFAULT_API_BASE = "https://api.agentchat.me"

# Behavior knobs. Defaults are tuned for a single-operator agent that
# joins a handful of conversations and receives single-digit messages
# per minute. Power users can override via env vars.

# Max number of concurrent agent invocations across all conversations.
# Backpressure: once the cap is hit, new inbound messages wait in the
# per-conversation queue until a slot frees. Prevents runaway token cost
# during a thundering-herd burst (e.g., joining a busy group).
DEFAULT_MAX_INFLIGHT_TURNS = 4

# How long to wait for a single agent turn to complete (seconds) before
# the runtime marks it timed-out and frees the slot. Inactivity-based,
# mirroring Hermes' cron timeout pattern. A long-running tool call
# (e.g., a multi-step browser session) does NOT trip this — only true
# inactivity does. 0 disables the timeout entirely.
DEFAULT_TURN_INACTIVITY_TIMEOUT_S = 600.0


class ConfigError(RuntimeError):
    """Raised when configuration is malformed."""


@dataclass(frozen=True)
class Config:
    """Resolved plugin configuration.

    All fields are required and validated at construction. Use
    :func:`load_config` to construct from environment variables — it
    returns ``None`` when ``AGENTCHATME_API_KEY`` is unset, which lets
    the plugin register the CLI wizard even before the user has set
    up an account.
    """

    api_key: str
    api_base: str
    ws_url: str
    max_inflight_turns: int = DEFAULT_MAX_INFLIGHT_TURNS
    turn_inactivity_timeout_s: float = DEFAULT_TURN_INACTIVITY_TIMEOUT_S


def load_config() -> Config | None:
    """Build a :class:`Config` from environment variables.

    Returns ``None`` when ``AGENTCHATME_API_KEY`` is unset — the plugin
    runs in *CLI-only* mode (the wizard is reachable, but the WS daemon
    and tool surface stay dormant). Once the wizard persists a key and
    Hermes restarts, this returns a populated :class:`Config`.

    Raises:
        ConfigError: when a value is present but malformed (e.g.,
            ``AGENTCHATME_API_BASE`` is not a valid URL, or a numeric
            knob is not parseable).
    """
    api_key = os.environ.get("AGENTCHATME_API_KEY", "").strip()
    if not api_key:
        return None

    api_base_raw = os.environ.get("AGENTCHATME_API_BASE", DEFAULT_API_BASE).strip()
    api_base = _normalize_url(api_base_raw, scheme_allowed=("http", "https"))
    if api_base is None:
        raise ConfigError(
            f"AGENTCHATME_API_BASE={api_base_raw!r} is not a valid http(s) URL"
        )

    ws_url_raw = os.environ.get("AGENTCHATME_WS_URL", "").strip()
    if ws_url_raw:
        ws_url = _normalize_url(ws_url_raw, scheme_allowed=("ws", "wss"))
        if ws_url is None:
            raise ConfigError(
                f"AGENTCHATME_WS_URL={ws_url_raw!r} is not a valid ws(s) URL"
            )
    else:
        ws_url = _derive_ws_url(api_base)

    max_inflight = _parse_int_env(
        "AGENTCHATME_MAX_INFLIGHT_TURNS", DEFAULT_MAX_INFLIGHT_TURNS, minimum=1
    )
    inactivity_timeout = _parse_float_env(
        "AGENTCHATME_TURN_INACTIVITY_TIMEOUT_S",
        DEFAULT_TURN_INACTIVITY_TIMEOUT_S,
        minimum=0.0,
    )

    return Config(
        api_key=api_key,
        api_base=api_base,
        ws_url=ws_url,
        max_inflight_turns=max_inflight,
        turn_inactivity_timeout_s=inactivity_timeout,
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


def _parse_int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} is below the minimum of {minimum}")
    return value


def _parse_float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} is below the minimum of {minimum}")
    return value
