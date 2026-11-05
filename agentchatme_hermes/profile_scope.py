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
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

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


@contextmanager
def profile_context(home: Path) -> Iterator[None]:
    """Run the block as Hermes runs ``home``'s own work.

    Hermes keys everything per profile through a context-local ``HERMES_HOME``
    override (``hermes_constants.set_hermes_home_override``): the tool
    registry's profile overlay (where a multiplexed gateway registers the
    ``agentchat`` MCP server's tools), ``get_hermes_home()`` for SOUL.md,
    ``config.yaml`` and ``.env``, the MCP registry scope. The gateway sets it
    around each served profile's turns and carries it to its workers with
    ``copy_context()``; a thread this plugin starts has no such context, so
    under multiplex it would read the launch profile's registry (no
    ``mcp__agentchat__*`` tools → a held turn that never starts) and build
    the agent for the wrong home. Our worker and activation threads enter
    this for their lifetime. A no-op outside Hermes.
    """
    try:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except Exception:
        yield
        return
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        try:
            reset_hermes_home_override(token)
        except Exception:
            logger.debug("profile_context: reset raised", exc_info=True)


class ProfileScopeError(RuntimeError):
    """The profile's runtime scope could not be entered; the work must not run unscoped."""


@contextmanager
def profile_turn_scope(home: Path) -> Iterator[None]:
    """Run one cycle (tray, gate, turn) in the scope Hermes runs ``home``'s own turns in.

    The HERMES_HOME override (:func:`profile_context`) picks the profile's
    home, registry overlay and config; it does NOT pick its credentials.
    Under a multiplexed gateway those come from a per-profile *secret
    scope*: ``get_secret`` reads the bound mapping and never another
    profile's ``os.environ`` (``agent/secret_scope.py``). The gateway binds
    it around every served profile's turn (``gateway/run.py``
    ``_profile_runtime_scope``: override + ``set_secret_scope`` + terminal
    policy). A turn this plugin starts must do the same, or a served
    profile's provider key, ``key_env`` and other secrets resolve to the
    launch profile's values (or fail closed).

    * Multiplexing (``is_multiplex_active()``): Hermes's own
      ``_profile_runtime_scope(home)`` — the exact scope its turns use; if
      that helper is not importable, the same override + secret scope built
      by ``build_profile_secret_scope(home)``.
    * Single profile: ``GatewayTurnMixin._standalone_launch_scope()`` — what
      the gateway enters for its own turns (a no-op unless the process also
      hosts another profile).
    * Outside Hermes: nothing.

    Entered per cycle, not per thread: the multiplex flag is the gateway's,
    secrets are re-read the way the gateway re-reads them per turn, and the
    scope never outlives the work. Raises :class:`ProfileScopeError` when a
    multiplexed scope cannot be entered — the cycle must not run with
    another profile's credentials; the caller re-wakes later.
    """
    if _multiplexing():
        helper = _hermes_profile_runtime_scope()
        scope = helper(Path(home)) if helper is not None else _replicated_profile_scope(Path(home))
        try:
            scope.__enter__()
        except Exception as exc:
            raise ProfileScopeError(f"could not enter profile scope for {home}: {exc!r}") from exc
        try:
            yield
        except BaseException:
            if not scope.__exit__(*sys.exc_info()):
                raise
        else:
            scope.__exit__(None, None, None)
        return
    standalone = _hermes_standalone_launch_scope()
    if standalone is None:
        yield
        return
    with standalone():
        yield


def _multiplexing() -> bool:
    try:
        from agent.secret_scope import is_multiplex_active
    except Exception:
        return False
    try:
        return bool(is_multiplex_active())
    except Exception:
        return False


def _hermes_profile_runtime_scope() -> Callable[[Path], AbstractContextManager[None]] | None:
    try:
        from gateway.run import _profile_runtime_scope  # type: ignore[import-not-found]
    except Exception:
        return None
    return _profile_runtime_scope if callable(_profile_runtime_scope) else None


def _hermes_standalone_launch_scope() -> Callable[[], AbstractContextManager[None]] | None:
    try:
        from gateway.run_turn import GatewayTurnMixin  # type: ignore[import-not-found]
    except Exception:
        return None
    scope = getattr(GatewayTurnMixin, "_standalone_launch_scope", None)
    return scope if callable(scope) else None


@contextmanager
def _replicated_profile_scope(home: Path) -> Iterator[None]:
    """What ``_profile_runtime_scope`` enters, from Hermes's public pieces (override + secret scope)."""
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    with profile_context(home):
        token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            yield
        finally:
            reset_secret_scope(token)


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
