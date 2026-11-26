"""Which Hermes profile this code runs for: its home, its settings, its scope.

Why this module exists
──────────────────────
Hermes runs several *profiles* on one machine, each with its own home
(``~/.hermes`` for the default profile, ``~/.hermes/profiles/<name>`` for
every other one), its own settings and its own ``SOUL.md``. Since Hermes
0.22 a single gateway process can serve every profile at once
("multiplexing"): Hermes keeps one plugin manager per profile home and
calls our ``register(ctx)`` once per profile, in a context that points
``hermes_constants.get_hermes_home()`` at that profile's directory.

Versions of this plugin before 0.2.3111111 ignored that. They read the key
straight from ``os.environ`` (under multiplexing the environment of
whichever profile launched the gateway), kept one runtime for the whole
process, and derived the WebSocket leader lock from ``Path.home()``. On
real fleets (September 2026) only the first profile to start received, and
a second profile could send as the first profile's handle.

The profile's scope: :class:`ProfileScope`
──────────────────────────────────────────
``register(ctx)`` captures the context Hermes loaded the plugin in
(``contextvars.copy_context()``: the profile's home override and, for a
profile a multiplexed gateway serves beside its launch profile, the runtime
scope Hermes bound for it). Every thread this plugin starts and every cycle
it runs (an activation attempt, a gate call, the runtime check, the wait
for the hosted tools, a turn) runs in a fresh copy of that context through
:meth:`ProfileScope.run`. On a multiplexed gateway ``run`` also binds the
profile's secrets for the call, with Hermes' public secret-scope API only
(``agent.secret_scope``): an empty mapping stamped with the profile home
(``set_secret_scope``), filled in place by
``refresh_installed_secret_scope``, which builds it the way Hermes builds
a turn's (the launch profile: the environment frozen when the gateway began
multiplexing, so keys set only by systemd ``Environment=`` or ``op run``
are kept, under its ``.env``; any other profile: its own ``.env`` and secret
sources only). The mapping is rebuilt on every call, as the gateway rebinds
per turn. A single-profile process binds nothing: ``get_secret`` reads the
process environment, which is that profile's own.

Settings are read only through Hermes: :func:`resolve_setting` is
``agent.secret_scope.get_secret`` in the scope the caller runs in. This
plugin never opens a ``.env`` file.

Everything that needs to answer *which profile am I* goes through this
module, so the answer is computed one way and is unit-testable without
Hermes installed.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

DEFAULT_HOME_DIRNAME = ".hermes"
PROFILES_DIRNAME = "profiles"

SettingResolver = Callable[[str], Optional[str]]

# Test seam: when set, every setting read goes through this callable
# instead of Hermes / the process environment.
_resolver_override: SettingResolver | None = None


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


class ProfileScopeError(RuntimeError):
    """The profile's scope could not be entered; the work must not run unscoped."""


_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


class ProfileScope:
    """The scope of the Hermes profile that loaded this plugin, for every
    thread and cycle the plugin runs (module docstring).

    :meth:`capture` is called in ``register(ctx)``. :meth:`run` is an
    entry point (an activation attempt, a gate call, the runtime check, the
    hold for the hosted tools, a turn): one call in a fresh copy of the
    captured context (a ``Context`` cannot be entered by two threads at
    once, and nothing one call sets leaks into the next) with, on a
    multiplexed gateway, the profile's secrets bound for that call.
    :meth:`thread` is a daemon thread that runs in a fresh copy of the
    captured context — the profile's home, Hermes' runtime scope for it, its
    log routing — and binds no secrets itself: the entry points it calls
    do, each when it runs, so a binding that fails fails that entry point,
    never the thread. A worker inside a call that already runs in the scope
    (the turn's own thread) inherits it with :func:`context_thread`.
    """

    __slots__ = ("_context", "_home")

    def __init__(self, home: Path, context: contextvars.Context) -> None:
        self._home = Path(home)
        self._context = context

    @classmethod
    def capture(cls, home: Path) -> ProfileScope:
        """The scope of the current context, for profile ``home``."""
        return cls(Path(home), contextvars.copy_context())

    @property
    def home(self) -> Path:
        return self._home

    def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """``fn(*args, **kwargs)`` in this profile's scope.

        Raises :class:`ProfileScopeError` (and does not call ``fn``) when a
        multiplexed gateway's secret scope cannot be bound for the profile.
        """
        return self._context.copy().run(self._call, fn, args, kwargs)

    def thread(self, target: Callable[[], object], *, name: str) -> threading.Thread:
        """An unstarted daemon thread that runs ``target`` in a fresh copy of the
        captured context (no secrets bound: see the class docstring)."""
        return threading.Thread(target=self._context.copy().run, args=(target,), name=name, daemon=True)

    def _call(self, fn: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
        token, reset = self._bind_secrets()
        try:
            return fn(*args, **kwargs)
        finally:
            if token is not None and reset is not None:
                try:
                    reset(token)
                except Exception:
                    logger.debug("ProfileScope: resetting the secret scope raised", exc_info=True)

    def _bind_secrets(self) -> tuple[Any, Callable[[Any], None] | None]:
        """Bind this profile's secrets when the process multiplexes; ``(token, reset)``.

        ``set_secret_scope`` installs an empty mapping stamped with the home
        (with Hermes' runtime scope bound for this profile it passes the
        mapping through, ``agent.runtime_scope.legacy_switch``); Hermes holds
        it by reference, and ``refresh_installed_secret_scope`` fills it with
        the mapping Hermes builds for the profile's own turns
        (``owner_secret_scope``). Outside Hermes, or in a process that does
        not multiplex, nothing is bound: ``get_secret`` reads the process
        environment, which is the profile's own.
        """
        try:
            from agent import secret_scope
        except ImportError:
            return None, None
        try:
            multiplexed = bool(secret_scope.is_multiplex_active())
        except Exception as exc:
            raise ProfileScopeError(f"Hermes' multiplex state is not readable ({exc!r})") from exc
        if not multiplexed:
            return None, None
        home = str(self._home)
        set_scope = getattr(secret_scope, "set_secret_scope", None)
        refresh = getattr(secret_scope, "refresh_installed_secret_scope", None)
        reset = getattr(secret_scope, "reset_secret_scope", None)
        if not (callable(set_scope) and callable(refresh) and callable(reset)):
            raise ProfileScopeError(
                "this Hermes has no public way to bind a profile's secrets for a plugin's work "
                "(agent.secret_scope.set_secret_scope / refresh_installed_secret_scope) — upgrade "
                "Hermes to 0.23.2 or later"
            )
        try:
            token = set_scope({}, profile_home=home)
        except Exception as exc:
            raise ProfileScopeError(f"Hermes refused this profile's secret scope ({exc!r})") from exc
        try:
            filled = refresh(self._home)
        except Exception as exc:
            reset(token)
            raise ProfileScopeError(f"Hermes could not build this profile's secrets ({exc!r})") from exc
        if not filled:
            reset(token)
            raise ProfileScopeError("Hermes did not fill this profile's secret scope")
        if refresh_keeps_launch_env() is False:
            _warn_once(
                "files_only_refresh",
                "agentchat: this Hermes (older than 0.23.2) rebuilds a plugin's secret scope from "
                "the profile's files only, so on this gateway serving several profiles, keys set "
                "only in the gateway's environment (systemd Environment=, op run, docker -e) are "
                "not visible to AgentChat's gate calls and turns — update Hermes",
            )
        return token, reset


# The probe's answer, once per process (``None``: not known yet / not Hermes).
_keeps_launch_env: list[bool | None] = []


def refresh_keeps_launch_env() -> bool | None:
    """Does this Hermes fill a plugin-installed secret scope the way it builds a
    profile's own turns — keeping the launch profile's environment-only keys
    (Hermes 0.23.2 and later)? ``None`` outside Hermes or when it cannot tell.

    Hermes exposes no release number to a plugin (its distribution reports
    ``0.0.0``; ``hermes --version`` reads a private module), so this asks the
    public function itself, in an empty context: since 0.23.2
    ``refresh_installed_secret_scope`` builds through ``owner_secret_scope`` and
    refuses to refill a scope stamped for another home (its documented
    contract); before, it rebuilt any installed scope from the named home's
    files alone. Two homes that do not exist are named, so nothing is read for
    a real profile.
    """
    if _keeps_launch_env:
        return _keeps_launch_env[0]
    try:
        from agent import secret_scope
    except ImportError:
        return None

    def probe() -> bool:
        base = Path(tempfile.gettempdir()) / "agentchat-scope-probe"
        token = secret_scope.set_secret_scope({}, profile_home=str(base / "a"))
        try:
            return not secret_scope.refresh_installed_secret_scope(base / "b")
        finally:
            secret_scope.reset_secret_scope(token)

    try:
        answer: bool | None = bool(contextvars.Context().run(probe))
    except Exception:
        logger.debug("profile_scope: the secret-scope refresh probe raised", exc_info=True)
        answer = None
    _keeps_launch_env.append(answer)
    return answer


def scope_status() -> dict[str, bool | None]:
    """For the health record: does this process multiplex, and does its Hermes keep
    the launch profile's environment-only keys in a plugin's scope?"""
    try:
        from agent import secret_scope
    except ImportError:
        return {"multiplexed": None, "keeps_launch_env": None}
    try:
        multiplexed: bool | None = bool(secret_scope.is_multiplex_active())
    except Exception:
        multiplexed = None
    return {"multiplexed": multiplexed, "keeps_launch_env": refresh_keeps_launch_env() if multiplexed else None}


def context_thread(target: Callable[[], object], *, name: str) -> threading.Thread:
    """An unstarted daemon thread that runs ``target`` in a copy of the caller's
    context: a worker inside a call already in the profile's scope (the shape
    of Hermes' ``agent.memory_provider.spawn_context_thread``)."""
    context = contextvars.copy_context()
    return threading.Thread(target=context.run, args=(target,), name=name, daemon=True)


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
    """One ``AGENTCHAT*`` setting for the profile this code runs for.

    Hermes' ``agent.secret_scope.get_secret``, the profile-scoped resolver:
    in a single-profile process the process environment (Hermes loaded the
    profile's ``.env`` into it, over the shell's exports); on a multiplexed
    gateway the secrets :class:`ProfileScope` bound for the call, never
    another profile's. Without Hermes importable (tests, inspection outside
    Hermes): ``os.environ``. ``None`` when unset.

    Raises :class:`ProfileScopeError` when Hermes reports a read outside any
    profile's scope while the gateway multiplexes: a plugin path that did not
    enter the scope, which must not be answered from anywhere else.
    """
    if _resolver_override is not None:
        return _resolver_override(name)
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:
        return os.environ.get(name)
    try:
        value = get_secret(name)
    except UnscopedSecretError as exc:
        _warn_once(
            f"unscoped:{name}",
            "agentchat: %s was read outside this profile's scope while the gateway multiplexes "
            "profiles — the work that read it does not run (please report this)",
            name,
        )
        raise ProfileScopeError(f"{name} was read outside this profile's scope") from exc
    if value is None:
        return None
    return str(value)


def set_setting_resolver_for_tests(resolver: SettingResolver | None) -> None:
    """Install (or clear with ``None``) a resolver override. Test-only."""
    global _resolver_override
    _resolver_override = resolver


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
