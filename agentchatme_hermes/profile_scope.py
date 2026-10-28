"""Profile-scoped resolution of the Hermes home, settings, and secrets.

Why this module exists
──────────────────────
Hermes runs several *profiles* on one machine, each with its own home
(``~/.hermes`` for the default profile, ``~/.hermes/profiles/<name>`` for
every other one), its own ``.env`` and its own ``SOUL.md``. Since Hermes
0.22 a single gateway process can serve every profile at once
("multiplexing"): Hermes keeps one plugin manager per profile home and
calls our ``register(ctx)`` once per profile, inside a context that
points ``hermes_constants.get_hermes_home()`` at that profile's directory
and resolves that profile's secrets through
``agent.secret_scope.get_secret``.

Versions of this plugin before 0.2.3111111 ignored both signals. They read
``AGENTCHATME_API_KEY`` straight from ``os.environ`` (the *process*
environment, which under multiplexing belongs to whichever profile
launched the gateway), kept one module-global runtime for the whole
process, and derived the WebSocket leader lock from ``Path.home()``.
The observable failures on real fleets (September 2026 investigation):
only the first profile to start ever received live messages, and a
second profile could send as the first profile's handle.

Everything that needs to answer *which profile am I* goes through this
module, so the answer is computed one way, with one fallback chain, and
is unit-testable without Hermes installed.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOME_DIRNAME = ".hermes"
PROFILES_DIRNAME = "profiles"

SettingResolver = Callable[[str], Optional[str]]

# Test seam: when set, every setting read goes through this callable
# instead of Hermes / the process environment.
_resolver_override: SettingResolver | None = None

# Names for which we already logged an "unscoped under multiplexing"
# warning — the condition repeats on every read, the log line should not.
_unscoped_warned: set[str] = set()


# ─── Hermes home ───────────────────────────────────────────────────────────


def resolve_hermes_home() -> Path:
    """Return the home of the profile this code is running for.

    Order: Hermes' own ``get_hermes_home()`` (context-local override →
    ``HERMES_HOME`` → platform default) when Hermes is importable; else
    ``HERMES_HOME``; else ``~/.hermes``. Inside a Hermes process the first
    branch always wins, and under multiplexing it is the *only* correct
    answer — the process environment may name a different profile.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        raw = os.environ.get("HERMES_HOME", "").strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / DEFAULT_HOME_DIRNAME
    home: Path = get_hermes_home()
    return Path(home).expanduser()


def hermes_root_of(home: Path) -> Path:
    """The Hermes *root* that owns ``home``.

    ``~/.hermes/profiles/growth`` → ``~/.hermes``; ``~/.hermes`` →
    itself; a Docker-style custom root behaves the same way. The root is
    where per-machine, cross-profile state lives (the leader locks).
    """
    parent = home.parent
    if parent.name == PROFILES_DIRNAME:
        return parent.parent
    return home


def profile_label(home: Path) -> str:
    """Human-readable profile name for logs and ``doctor`` output."""
    if hermes_root_of(home) == home:
        return "default"
    return home.name


def hermes_home_key(home: Path) -> str:
    """Stable registry key for a profile home.

    Delegates to Hermes' ``hermes_home_key`` when available so our
    runtime registry agrees with Hermes' own per-profile plugin
    managers about which paths are the same profile.
    """
    try:
        from hermes_constants import hermes_home_key as _hermes_key
    except ImportError:
        return str(home.expanduser().resolve(strict=False))
    return str(_hermes_key(home))


# ─── settings / secrets ────────────────────────────────────────────────────


def resolve_setting(name: str) -> str | None:
    """Resolve one ``AGENTCHATME_*`` setting for the current profile.

    Chain:

    1. Hermes' ``agent.secret_scope.get_secret`` — the profile-scoped
       resolver. Outside multiplexing it reads the profile ``.env``
       overlay and falls back to ``os.environ``; under multiplexing it
       serves the bound profile's secrets and never another profile's.
    2. If Hermes reports *no scope bound while multiplexing* (plugin
       discovery can run before a turn scope exists), read the current
       profile's own ``.env`` file. That is exactly the value the scope
       would have served, and strictly safer than the process env.
    3. Without Hermes importable (tests, inspection outside Hermes):
       ``os.environ``.

    Returns ``None`` when unset. Never raises for a missing value.
    """
    if _resolver_override is not None:
        return _resolver_override(name)
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return _environ_value(name)

    try:
        value = get_secret(name)
    except UnscopedSecretError:
        if name not in _unscoped_warned:
            _unscoped_warned.add(name)
            logger.warning(
                "agentchat: %s read outside a profile secret scope while the "
                "gateway multiplexes profiles — falling back to this profile's "
                ".env file (never the process environment).",
                name,
            )
        return _dotenv_value(resolve_hermes_home() / ".env", name)
    if value is None:
        return None
    return str(value)


def set_setting_resolver_for_tests(resolver: SettingResolver | None) -> None:
    """Install (or clear with ``None``) a resolver override. Test-only."""
    global _resolver_override
    _resolver_override = resolver


def _environ_value(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None else None


def _dotenv_value(path: Path, name: str) -> str | None:
    """Minimal ``KEY=VALUE`` reader for one key of a dotenv file.

    Supports ``export KEY=value``, single/double quotes, blank lines and
    ``#`` comments. Deliberately tiny: we only ever look up our own
    ``AGENTCHATME_*`` names in the profile's own file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value.split(" #", 1)[0].strip()
    return None


# ─── account identity ──────────────────────────────────────────────────────


def account_fingerprint(api_base: str, api_key: str) -> str:
    """Short, non-reversible identifier for (server, account).

    Used to name per-account artifacts such as the leader lock. Two
    profiles holding the *same* key get the same fingerprint (and will
    coordinate one live socket); different keys never collide in
    practice (16 hex chars of SHA-256 over a high-entropy key).
    """
    digest = hashlib.sha256(f"{api_base.rstrip('/')}\n{api_key}".encode())
    return digest.hexdigest()[:16]
