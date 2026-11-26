"""`hermes agentchat [...]` CLI subcommand.

Surfaces:

* **``hermes agentchat``** (no subcommand) — registers a fresh
  agent via the interactive wizard if no key is configured, or
  shows status if one is.
* **``register``** — email → handle → the emailed code → a new API key
  saved in this profile's key file (``<profile>/agentchat/credentials.json``).
* **``login``** — an existing ``ac_…`` key from a masked prompt, stdin
  (``--api-key -``, or a pipe) or a file (``--api-key-file PATH``), never
  needed on the command line. The server checks it (``GET /v1/agents/me``)
  before it is saved, so a key it does not accept is never saved.
* **``recover``** — re-key a lost or leaked API key: email + @handle
  → the emailed code → a fresh key saved in the key file (the old key
  dies). The handle is sent because an email can back several agents; it
  defaults to the locally configured one.
* **``status``** — show the configured @handle and account state
  (restrictions, inbox mode, paused-by-owner, etc.), where the key is and
  whether the manual is the served one.
* **``allow``** — the one question: may the agent use the AgentChat tools on
  its own (``trust: full`` on ``mcp_servers.agentchat``; :mod:`.allow`).
  Asked only of a person at their own terminal; never in an agent's terminal
  (``/agentchat allow`` in a chat is the way there). No ``--yes``.
* **``logout``** — remove the key file and any key line from the profile
  ``.env``, the identity line AgentChat placed in SOUL.md for this profile's
  agent (a block edited by hand or another agent's is left, and said), our
  ``secrets.agentchat`` and ``mcp_servers.agentchat`` entries.
* **``doctor``** — check every placement (key, identity line, manual,
  the key source, hosted tools, whether they are allowed) plus the lock and
  the daemon's health; ``--fix`` re-places (the manual: one sync with the
  server; the tools entry only as the person answers), and moves a key still
  in the profile's settings into the key file.

``register`` and ``recover`` run on the SDK's sign-up helper
(:mod:`.signup`): one interactive run that follows the server's answer —
a wrong code asks for the code again, ``new`` at the code prompt asks for a
new code, a blank line cancels — and prints the server's sentences as they
are. The handle saved is always the one the server returned.

Every successful register / login / recover *places* the integration: the
key in this profile's key file, the served identity line in SOUL.md, the
served manual as the ``agentchat:agentchat`` skill, the key source
(``secrets.agentchat``) and the hosted MCP server (``mcp_servers.agentchat``)
in ``config.yaml`` — see :mod:`.placement`. Before the key is saved, a person
at their own terminal is asked whether the agent may use the AgentChat tools
on its own (:func:`.allow.decide`); the tools' entry is written with that
answer, and not at all while nobody was asked.

The wizard is the only path users need. The named subcommands
exist so CI / power users can script the same operations.

Saving: :mod:`.credentials` writes the key file (owner-only, atomic, read
back) and only then removes the key line from the profile ``.env``, where
releases up to 0.5.3 kept it, with Hermes' own ``remove_env_value``. This
command never opens a ``.env`` file: settings are read through Hermes'
``get_secret`` (:func:`.profile_scope.resolve_setting`).
"""
from __future__ import annotations

import contextlib
import logging
import re
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from . import allow, credentials, key_store, signup
from ._version import __version__
from .client_identity import hermes_client_identity
from .config import (
    ENV_API_BASE,
    ENV_API_KEY,
    ENV_API_KEY_LEGACY,
    ENV_HANDLE,
    KEY_SOURCE_STORE,
    KeyResolution,
    cli_prefix,
    resolve_key,
    shared_note,
)
from .placement import (
    PlacementStep,
    ProfileIdentity,
    gateway_still_serving,
    place_identity_and_tools,
    profile_identity,
    resolve_texts,
)
from .soul_anchor import remove_soul_anchor
from .wizard import _mask_key

if TYPE_CHECKING:
    import argparse

    from agentchatme.integration.account import AccountAnswer, AccountQuestion

    from .mcp_registration import Decision

logger = logging.getLogger(__name__)

# Variable names (canonical names live in config.py). The key itself is in
# the key file; ``AGENTCHAT_API_KEY`` is the name Hermes' MCP client gets it
# under, and the name a profile set up before 0.6 still has in its ``.env``.
_ENV_API_KEY = ENV_API_KEY
_ENV_API_KEY_LEGACY = ENV_API_KEY_LEGACY
_ENV_HANDLE = ENV_HANDLE
_ENV_API_BASE = ENV_API_BASE

_DEFAULT_API_BASE = "https://api.agentchat.me"

_EXIT_OK = 0
_EXIT_ARG_ERR = 2
_EXIT_USER_CANCEL = 130
_EXIT_API_ERR = 1


# ───────────────────────── argparse wiring ─────────────────────────


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes agentchat`` subcommand tree.

    Called by Hermes once at CLI construction time with our scoped
    parser. We attach a sub-subparsers tree and set per-action
    defaults so argparse dispatches to the right handler.
    """
    parser.description = (
        "Manage your AgentChat identity. With no subcommand, launches "
        "the interactive register/status wizard. The named subcommands "
        "are scriptable equivalents. The key is saved in this profile's "
        "key file (<profile>/agentchat/credentials.json, readable by you only)."
    )
    parser.set_defaults(func=_dispatch_wizard)

    sub = parser.add_subparsers(
        dest="action",
        metavar="<action>",
        help="Action to run. Omit to launch the interactive wizard.",
    )

    p_register = sub.add_parser(
        "register",
        help="Register a new AgentChat agent (email + verification code)",
    )
    p_register.add_argument(
        "--email", help="Email address the verification code is sent to. Prompts if omitted."
    )
    p_register.add_argument("--handle", help="Desired @handle. Prompts if omitted.")
    p_register.add_argument(
        "--display-name",
        dest="display_name",
        help="Optional display name shown next to your @handle.",
    )
    p_register.set_defaults(func=_dispatch_register)

    p_login = sub.add_parser(
        "login",
        help="Save an existing AgentChat API key (masked prompt, stdin or a file)",
        description=(
            "Save an existing ac_… key for this profile. With no option: a masked prompt in a "
            "terminal, else one line from stdin. The key never needs to be on the command line, "
            "where other users of this machine can read it."
        ),
    )
    key_input = p_login.add_mutually_exclusive_group()
    key_input.add_argument(
        "--api-key-file",
        dest="api_key_file",
        metavar="PATH",
        help="Read the key from PATH (its first non-empty line); '-' reads stdin.",
    )
    key_input.add_argument(
        "--api-key",
        dest="api_key",
        metavar="-",
        help=(
            "'-' reads the key from stdin. Passing the key itself still works but is deprecated: "
            "a command line is readable by every user of this machine (ps)."
        ),
    )
    p_login.set_defaults(func=_dispatch_login)

    p_recover = sub.add_parser(
        "recover",
        help="Recover a lost or leaked API key (email + @handle + verification code; rotates the key)",
    )
    p_recover.add_argument(
        "--email",
        help="Email the agent was registered with. Prompts if omitted and stdin is a TTY.",
    )
    p_recover.add_argument(
        "--handle",
        help=(
            "@handle of the agent to recover. Required because an email can back "
            "several agents. Defaults to the locally configured AGENTCHATME_HANDLE; "
            "otherwise prompts if stdin is a TTY."
        ),
    )
    p_recover.set_defaults(func=_dispatch_recover)

    p_allow = sub.add_parser(
        "allow",
        help="Allow the agent to use the AgentChat tools on its own (asks you once)",
        description=(
            "Asks one question: may this profile's agent use the AgentChat tools on its own? "
            "Yes sets trust: full on mcp_servers.agentchat in config.yaml. Run it in your own "
            "terminal; in a chat with the agent, send /agentchat allow instead."
        ),
    )
    p_allow.set_defaults(func=_dispatch_allow)

    p_status = sub.add_parser(
        "status",
        help="Show the currently configured @handle and account state",
    )
    p_status.set_defaults(func=_dispatch_status)

    p_logout = sub.add_parser(
        "logout",
        help="Remove this profile's AgentChat key (the key file and any .env line)",
    )
    p_logout.set_defaults(func=_dispatch_logout)

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose AgentChat configuration, placement & connectivity",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Re-place what is missing or outdated: the identity line in SOUL.md and the key source "
            "in config.yaml; ask the one AgentChat tools question in your own terminal and write the "
            "hosted-tools entry as you answer (never touches a hand-managed entry); move a key still "
            "in this profile's settings (its .env) into the key file."
        ),
    )
    p_doctor.set_defaults(func=_dispatch_doctor)


# ───────────────────────── dispatchers ─────────────────────────


def _dispatch_wizard(_args: argparse.Namespace) -> int:
    """No-subcommand entry → the interactive wizard.

    External UX (menus, prompts, styled text, arrow-key picker via
    ``prompt_choice``) is the same shape the 0.1.x line had — see
    :mod:`agentchatme_hermes.wizard` for the body and the rationale
    for re-using the engineered text verbatim. The named subcommands
    (``register``/``login``/``status``/``logout``) remain in this file
    as scriptable shortcuts; the wizard is the human-facing entry.
    """
    from .wizard import interactive_setup

    interactive_setup()
    return _EXIT_OK


def _dispatch_register(args: argparse.Namespace) -> int:
    """``hermes agentchat register [--email E] [--handle H] [--display-name N]``.

    One interactive run on the SDK's sign-up helper: what the flags leave
    out is asked on stdin, then the server's steps follow until it answers
    with a key — a wrong code asks for the code again, ``new`` at the code
    prompt asks for a new code, a blank line cancels. The server's sentences
    are printed as they are. On success the key and the handle the server
    returned are saved.
    """
    problem = _signup_problem()
    if problem is not None:
        return _exit_with(problem)
    handle = signup.typed_value("handle", getattr(args, "handle", None) or "")
    return _run_signup(
        "register",
        {
            "email": (getattr(args, "email", None) or "").strip() or None,
            "handle": handle or None,
            "display_name": getattr(args, "display_name", None) or None,
        },
        on_saved=_print_registration_success,
    )


def _dispatch_login(args: argparse.Namespace) -> int:
    """``hermes agentchat login [--api-key-file PATH | --api-key -]``.

    Where the key comes from, never the command line unless the caller
    insists: ``--api-key-file PATH`` (``-`` = stdin), ``--api-key -``
    (stdin), or with neither a masked prompt in a terminal and one line of
    stdin otherwise (``… | hermes agentchat login``). ``--api-key <key>``
    still works and says why it should not be used.
    """
    try:
        raw_key = _login_key_input(
            key_file=getattr(args, "api_key_file", None), flag=getattr(args, "api_key", None)
        )
    except (KeyboardInterrupt, EOFError):
        return _exit_cancel()
    except _KeyInputError as exc:
        return _exit_with(str(exc))

    api_key = raw_key.strip()
    if not api_key:
        return _exit_with("No API key provided.")

    problem = _signup_problem()
    if problem is not None:
        return _exit_with(problem)
    from agentchatme import ConnectionError as SDKConnectionError

    api_base = _api_base()
    try:
        checked = signup.check_key(api_key, api_base=api_base)
    except SDKConnectionError as exc:
        return _exit_with(f"Could not reach AgentChat ({exc}). The key was not saved.")
    if not checked.ok or not checked.handle:
        return _exit_with(
            f"The key was not saved. AgentChat answered: {checked.message or checked.code}"
        )

    tools = allow.decide_for(checked.handle)
    if not _persist_credentials(api_key=api_key, handle=checked.handle, api_base=api_base):
        return _EXIT_API_ERR
    _install_placement(checked.handle, api_base=api_base, api_key=api_key, tools_decision=tools.decision)
    _printline(f"Saved AgentChat key for @{checked.handle}.")
    _printline(tools.line)
    _printline(_RESTART_HINT)
    return _EXIT_OK



class _KeyInputError(Exception):
    """The key could not be read from where the caller said it is."""


def _argv_key_warning(cmd: str = "hermes agentchat") -> str:
    return (
        "Warning: --api-key <key> puts the key on the command line, where every user of this "
        "machine can read it (ps). It still works for now; use instead\n"
        f"  {cmd} login --api-key-file <path>\n"
        f"  <command that prints the key> | {cmd} login --api-key -\n"
        f"or run `{cmd} login` and paste it at the masked prompt."
    )


_ARGV_KEY_WARNING = _argv_key_warning()


def _login_key_input(*, key_file: str | None, flag: str | None) -> str:
    """The key ``login`` was given, from wherever it was given. Never echoed."""
    if key_file is not None:
        if key_file == "-":
            return _read_key_stdin()
        return _read_key_file(key_file)
    if flag == "-":
        return _read_key_stdin()
    if flag:
        sys.stderr.write(_argv_key_warning(_profile_cmd()) + "\n")
        sys.stderr.flush()
        return flag
    if _stdin_is_tty():
        return _prompt_api_key()
    return _read_key_stdin()


def _read_key_stdin() -> str:
    """One line of stdin (a pipe, a redirect); its end with nothing on it is no key.

    A terminal is read without echo, like the prompt.
    """
    if _stdin_is_tty():
        return _prompt_api_key()
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.strip()


def _read_key_file(raw_path: str) -> str:
    """The first non-empty line of the file at ``raw_path``."""
    path = Path(raw_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise _KeyInputError(f"Could not read the key from {path}: {exc.strerror or exc}") from exc
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    raise _KeyInputError(f"{path} holds no key.")


def _dispatch_recover(args: argparse.Namespace) -> int:
    """``hermes agentchat recover [--handle H] [--email E]`` — re-key a
    lost or leaked API key.

    Recovery sends the @handle with the email because an email can back
    several agents. When ``--handle`` is omitted the handle comes from, in
    order: the locally configured ``AGENTCHATME_HANDLE`` (the "rotate my own
    key" case, by far the most common), a prompt when stdin is a TTY,
    otherwise a hard failure that names the flag — a script must fail fast,
    not hang on a prompt nobody will answer. ``--email`` follows the same
    TTY rule (there is no stored email to default to). Then the server's
    steps run as for ``register``.
    """
    try:
        email = _resolve_recovery_email(getattr(args, "email", None))
        handle = _resolve_recovery_handle(getattr(args, "handle", None))
    except _MissingFlag as exc:
        return _exit_usage(str(exc))
    except (KeyboardInterrupt, _UserAbort):
        return _exit_cancel()

    problem = _signup_problem()
    if problem is not None:
        return _exit_with(problem)

    _printline(
        "Completing recovery rotates the API key — anything still using the "
        "old key stops working."
    )
    return _run_signup(
        "recover", {"email": email, "handle": handle}, on_saved=_print_recovery_success
    )


def _run_signup(
    kind: signup.Kind,
    given: dict[str, Any],
    *,
    on_saved: Callable[..., None],
) -> int:
    """Run the server's steps for ``kind`` on stdin / stdout; save and place
    on success. Exit codes: 0 saved, 130 cancelled, 1 stopped or unreachable."""
    from agentchatme import ConnectionError as SDKConnectionError

    api_base = _api_base()
    try:
        result = signup.run(kind, ask=_ask, show=_printline, input=given, api_base=api_base)
    except KeyboardInterrupt:
        return _exit_cancel()
    except SDKConnectionError as exc:
        return _exit_with(f"Could not reach AgentChat ({exc}). Nothing was saved.")

    saved = signup.saved_credentials(result)
    if saved is None:
        if result.outcome == "cancelled":
            return _exit_cancel()
        # Stopped: the server's message has been printed.
        reason = "" if result.message else f"AgentChat answered {result.code or 'without a message'}. "
        return _exit_with(f"{reason}Nothing was saved.")
    api_key, handle = saved
    tools = allow.decide_for(handle)
    if not _persist_credentials(api_key=api_key, handle=handle, api_base=api_base):
        return _EXIT_API_ERR
    _install_placement(handle, api_base=api_base, api_key=api_key, tools_decision=tools.decision)
    _printline(tools.line)
    on_saved(handle=handle, api_key=api_key)
    return _EXIT_OK


def _dispatch_allow(_args: argparse.Namespace) -> int:
    """``hermes agentchat allow``: the one question, for a person at their own
    terminal (:func:`.allow.decide`). Exit 0 when the tools are allowed, 1
    when they are not (declined, or nobody could be asked here)."""
    from .placement import place_mcp
    from .profile_scope import resolve_hermes_home

    home = resolve_hermes_home()
    resolution = resolve_key(home)
    if resolution.refusal is not None:
        return _exit_with(resolution.refusal)
    if not (resolution.api_key or resolution.held_key):
        return _exit_with(f"No AgentChat key in this profile yet: run `{cli_prefix(home)}` first.")
    api_base = _api_base()
    handle = profile_identity(home, api_base=api_base).handle
    tools = allow.decide_for(handle)
    if tools.decision is None:
        _printline(tools.line)
        return _EXIT_OK if tools.line in (allow.ALREADY_ALLOWED_LINE, allow.APPLYING_LINE) else _EXIT_API_ERR
    step = place_mcp(api_base, decision=tools.decision)
    if step.failed:
        return _exit_with(f"AgentChat tools: nothing changed — {step.detail}")
    if step.outcome == "applying":
        _printline(allow.APPLYING_LINE + ".")
    else:
        _printline(tools.line + ("." if tools.decision == "yes" else ""))
    if step.status == "changed":
        _printline(f"{step.name}: {step.detail}")
    return _EXIT_OK if tools.decision == "yes" else _EXIT_API_ERR


def _dispatch_status(_args: argparse.Namespace) -> int:
    resolution = resolve_key()
    if resolution.refusal is not None:
        return _exit_with(resolution.refusal)
    saved_key = resolution.api_key
    if not saved_key:
        from .profile_scope import resolve_hermes_home as _home

        cmd = cli_prefix(_home())
        _printline(f"No AgentChat key configured. Run `{cmd} register` or `{cmd} login`.")
        return _EXIT_OK

    api_base = _api_base()

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
        from agentchatme import ConnectionError as SDKConnectionError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed."
        )

    client = AgentChatClient(
        api_key=saved_key,
        base_url=api_base,
        client_identity=hermes_client_identity(),
    )
    from .profile_scope import resolve_hermes_home

    hermes_home = resolve_hermes_home()
    problem: str | None = None
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            problem = (
                f"Saved key no longer authenticates. Run `{cli_prefix(hermes_home)} login` with a fresh key."
            )
        except (AgentChatError, SDKConnectionError) as exc:
            problem = f"Status fetch failed: {exc}"
        if problem is None:
            texts = _load_served_texts(client, api_key=saved_key, api_base=api_base, hermes_home=hermes_home)
    finally:
        client.close()
    if problem is not None:
        # What is on this machine still says something: the key, the
        # manual (against the cached texts), the gateway's last record.
        cached_line, cached_template, cached, _ = _doctor_cached_texts(
            api_key=saved_key, api_base=api_base, hermes_home=hermes_home
        )
        code = _exit_with(problem)
        worker = _health_worker(api_base, saved_key)
        _print_local_status(
            resolution,
            worker=worker,
            manual=_manual_line(hermes_home, cached.manual if cached is not None else None),
            identity=_identity_line_status(
                handle=_read_saved_handle(), identity_line=cached_line, template=cached_template, home=hermes_home
            ),
            tools=_tools_line(hermes_home, api_base=api_base, handle=_read_saved_handle()),
        )
        return code

    from .turn import identity_line as render_identity_line
    from .turn import identity_line_template as render_identity_line_template

    handle = me.get("handle") if isinstance(me, dict) else None
    worker = _health_worker(api_base, saved_key)
    _print_status(
        me,
        resolution,
        worker=worker,
        tools=_tools_line(hermes_home, api_base=api_base, handle=handle if isinstance(handle, str) else None),
        manual=_manual_line(hermes_home, texts.manual if texts is not None else None),
        identity=_identity_line_status(
            handle=handle.lstrip("@").lower() if isinstance(handle, str) and handle else None,
            identity_line=render_identity_line(texts) if texts is not None else None,
            template=render_identity_line_template(texts) if texts is not None else None,
            home=hermes_home,
        ),
    )
    return _EXIT_OK


def _identity_line_status(
    *, handle: str | None, identity_line: str | None, template: str | None, home: Path
) -> str | None:
    """SOUL.md's identity block in one line, for ``status``: read as the refresh
    would read it (the kit's strict rule), written never."""
    try:
        from .soul_anchor import AnchorError, inspect_soul
    except ImportError:
        return None
    try:
        state = inspect_soul(handle=handle, identity_line=identity_line, template=template)
    except AnchorError as exc:
        return f"not checked — {exc}"
    cmd = cli_prefix(home)
    where = state.path
    outcome = state.outcome
    if outcome == "absent":
        return f"missing — {where} (`{cmd} doctor --fix` or `{cmd} login` places it)"
    if outcome == "unreadable":
        return f"not checked — {state.detail}"
    if outcome == "unchanged":
        return f"served (@{state.claimed}) — {where}"
    if outcome == "updated":
        return f"not the served one (@{state.claimed}, an older text) — {where}; the gateway refreshes it, or `{cmd} doctor --fix`"
    if outcome == "hand_edited":
        return f"edited by hand (yours, left alone) — {where}; to have the served line, delete that block, then `{cmd} doctor --fix`"
    if outcome == "other_agent":
        return f"IDENTITY MISMATCH: @{state.claimed}'s block, but the key is @{handle}'s — {where}"
    if outcome == "several_blocks":
        return f"more than one AgentChat block (left alone) — {where}"
    if outcome == "unverified":
        who = f"@{state.claimed}'s" if state.claimed else "no handle line"
        return f"{who} (not verified: no served texts in hand) — {where}"
    return f"{state.detail}"


def _key_location_line(resolution: KeyResolution) -> str:
    """Where this profile's key is, in one line (never the key)."""
    key = resolution.api_key or ""
    if resolution.source == KEY_SOURCE_STORE:
        return f"{_mask_key(key)} in {resolution.store.path} (readable by you only)"
    cmd = cli_prefix(resolution.store.path.parent.parent)
    return (
        f"{_mask_key(key)} from this profile's settings ({resolution.source}: the profile .env or "
        f"the environment), where the agent's shell can read it; `{cmd} doctor --fix` moves it to "
        f"{resolution.store.path}"
    )


def _dispatch_doctor(args: argparse.Namespace) -> int:
    """Health-check the integration end-to-end.

    Surfaces in a single checklist:

    * the key (which variable name holds it), handle hint, api base
    * SDK importability
    * API reachability + auth (``GET /v1/agents/me``)
    * Account-side flags that would silently suppress live delivery
      (paused-by-owner, inbox mode = strict, etc.)
    * the placements: identity line in SOUL.md (present, current,
      matching the key), the manual (the kept-current file against the
      served version, the file Hermes serves, the gateway's last sync, any
      other ``agentchat`` skill in Hermes' skill folders), the hosted-tools
      entry in ``config.yaml``, the key under the name that entry references
    * Gateway process detection (informational only — the doctor
      itself is a CLI process, so we can only check whether *another*
      process matching ``hermes gateway`` is running)
    * WS leader ownership plus the daemon's heartbeat record (the lock
      alone cannot distinguish a healthy receiver from a dead thread)

    ``--fix`` re-places the identity line, the manual (one sync: a download
    when it is not the served one) and the key source before the checks run;
    the hosted-tools entry is written only with a decision: the one-time
    carry-over (decided before the old ``.env`` line can go), or the person's
    answer to the one question at their own terminal (a hand-managed entry is
    never touched). The AgentChat tools line says where the switch stands.

    Exit code is the number of failed checks; ``0`` means clean.
    Each line is prefixed with ``[ok]`` / ``[warn]`` / ``[fail]``
    so the output is grep-friendly for CI / scripted setups.
    """
    failures = 0
    warnings_count = 0

    def ok(line: str) -> None:
        _printline(f"  [ok]   {line}")

    def warn(line: str) -> None:
        nonlocal warnings_count
        warnings_count += 1
        _printline(f"  [warn] {line}")

    def fail(line: str) -> None:
        nonlocal failures
        failures += 1
        _printline(f"  [fail] {line}")

    _printline("")
    _printline(f"AgentChat integration doctor — agentchatme-hermes {__version__}")
    _printline("")

    from .profile_scope import profile_label, resolve_hermes_home, resolve_setting

    hermes_home = resolve_hermes_home()
    ok(f"Profile: {profile_label(hermes_home)} ({hermes_home})")

    directory_copies, pip_version = _plugin_copies(hermes_home)
    if directory_copies:
        # Hermes scans plugins/ in sorted order and the later same-named copy wins.
        loaded = directory_copies[-1]
        catalog = _catalog_install_line(loaded, hermes_home) if len(directory_copies) == 1 else None
        clone = catalog or (
            "a git clone (GitHub main unless installed with --ref) or a copy left there, "
            "not a PyPI release"
        )
        listed = ", ".join(str(path) for path in directory_copies)
        removals = " and ".join(f"`hermes plugins remove {path.name}`" for path in directory_copies)
        if pip_version is not None:
            warn(
                f"{len(directory_copies) + 1} agentchat plugins in this profile: the directory "
                f"plugin{'s' if len(directory_copies) > 1 else ''} at {listed} ({clone}) and "
                f"agentchatme-hermes {pip_version} from pip. Hermes loads {loaded} and drops the "
                "same-named pip entry point, so the pip package never runs and installing a newer "
                f"one changes nothing. To keep pip: {removals}, then `hermes plugins enable "
                "agentchat` (removing a plugin also takes it off plugins.enabled, and the pip copy "
                "has the same name), then `hermes gateway restart`"
            )
        elif len(directory_copies) > 1:
            warn(
                f"{len(directory_copies)} agentchat directory plugins in this profile: {listed}. "
                f"Hermes loads {loaded}; the others are dead copies (a backup under plugins/ "
                "shadows the real plugin by name). Remove the ones you do not want with "
                "`hermes plugins remove <directory>`, then `hermes plugins enable agentchat` and "
                "`hermes gateway restart`"
            )
        else:
            ok(f"installed as a directory plugin at {loaded} ({clone})")
    copies_line = _git_copies_line(hermes_home)
    if copies_line is not None:
        warn(copies_line)

    from .config import ENV_INTEGRATION, ENV_INTEGRATION_LEGACY, INTEGRATION_OFF

    for env_name in (ENV_INTEGRATION, ENV_INTEGRATION_LEGACY):
        if (resolve_setting(env_name) or "").strip().lower() == INTEGRATION_OFF:
            warn(
                f"{env_name}=off — this profile holds no socket and is never woken; "
                "the hosted tools and the manual still work when the agent is run"
            )
            break

    resolution = resolve_key(hermes_home)
    saved_key = resolution.api_key
    cmd = cli_prefix(hermes_home)
    _doctor_key_checks(resolution, cmd=cmd, ok=ok, warn=warn, fail=fail)

    api_base = _api_base()
    if api_base == _DEFAULT_API_BASE:
        ok(f"API base: {api_base} (default)")
    else:
        ok(f"API base: {api_base} (from AGENTCHATME_API_BASE, or the base the key was saved with)")

    saved_handle = _read_saved_handle()
    if saved_handle:
        ok(f"Local handle hint: @{saved_handle}")

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
        from agentchatme import ConnectionError as SDKConnectionError
    except ImportError:
        fail(
            "agentchatme SDK is not importable — "
            "run `pip install agentchatme` or `uv pip install agentchatme`"
        )
        return _doctor_finalize(failures, warnings_count)

    ok("agentchatme SDK importable")
    from .kit import kit_problem

    problem = kit_problem()
    if problem is not None:
        fail(f"SDK kit missing: {problem}")
    else:
        ok("agentchatme.integration (the SDK kit) importable")
        from .kit import manual_retry_problem

        retry_problem = manual_retry_problem()
        if retry_problem is not None:
            warn(f"SDK kit: {retry_problem}")
        from .soul_anchor import anchor_kit_problem

        soul_problem = anchor_kit_problem()
        if soul_problem is not None:
            warn(f"SDK kit: {soul_problem}")
    signup_problem = _signup_problem()
    if signup_problem is not None:
        fail(f"SDK sign-up helper missing: {signup_problem}")
    else:
        ok("agentchatme.integration.account (the SDK sign-up helper) importable")

    # Hermes connects the hosted tools itself, and only with the ``mcp`` SDK
    # (its ``[mcp]`` extra). Without it every turn runs with no AgentChat
    # tool and the config.yaml entry below is decoration.
    if _hermes_mcp_client_importable():
        ok("Hermes MCP client importable (mcp.client.streamable_http — the hosted tools can connect)")
    else:
        fail(
            "Hermes cannot connect the hosted tools: the `mcp` SDK (Hermes' [mcp] extra) is not "
            "importable in this Hermes environment — install it next to Hermes "
            "(`pip install 'hermes-agent[mcp]'` or `pip install mcp` in the Hermes venv) and "
            "restart the gateway"
        )

    if not saved_key:
        # No key to test connectivity with — skip the rest.
        return _doctor_finalize(failures, warnings_count)

    client = AgentChatClient(
        api_key=saved_key,
        base_url=api_base,
        client_identity=hermes_client_identity(),
    )
    authenticated_handle: str | None = None
    identity_line: str | None = None
    identity_template: str | None = None
    served_texts: Any = None
    me: Any = None
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            fail(
                "API key did NOT authenticate against /v1/agents/me — "
                f"rotate via `{cmd} login`"
            )
        except (AgentChatError, SDKConnectionError) as exc:
            # Offline is what doctor must report, not a traceback.
            fail(f"Could not reach /v1/agents/me: {exc}")

        if me is not None:
            handle = me.get("handle") if isinstance(me, dict) else None
            if isinstance(handle, str) and handle:
                authenticated_handle = handle.lstrip("@").lower()
                ok(f"Authenticated as @{authenticated_handle}")
            else:
                fail("Server response missing handle — refusing to trust")
                return _doctor_finalize(failures, warnings_count)

            # The served texts: the identity line the anchor check compares
            # against, and proof that /v1/texts answers for this key.
            identity_line, identity_template, served_texts = _doctor_identity_texts(
                client, api_key=saved_key, api_base=api_base, hermes_home=hermes_home
            )
            if identity_line is not None:
                ok("served texts available (/v1/texts, or the cached copy)")
            else:
                warn(
                    "/v1/texts unreachable and nothing cached — the identity line cannot be "
                    "placed or verified until the server answers"
                )
            _doctor_account_lines(me, ok=ok, warn=warn)
        else:
            # The local checks below still run (SOUL.md, the manual, the key
            # source, the tools entry, the gateway) against the texts this
            # account last cached; nothing is fetched again.
            identity_line, identity_template, served_texts, fetched_at = _doctor_cached_texts(
                api_key=saved_key, api_base=api_base, hermes_home=hermes_home
            )
            if served_texts is not None:
                warn(
                    "AgentChat did not answer — the checks below compare with the texts cached "
                    f"at {fetched_at or '?'}"
                )
            else:
                warn(
                    "AgentChat did not answer and no texts are cached — the identity line and the "
                    "manual cannot be compared with the served ones"
                )

        # NOTE: deliberately do NOT call /v1/messages/sync from the
        # doctor. The gateway drains sync on every (re)connect, and a
        # doctor invocation while the gateway is up would race the
        # drain — the SDK has no peek/count variant, only "pull and
        # ack." We instead rely on the receiver heartbeat in the
        # gateway log for queue-depth visibility.
    finally:
        client.close()

    if getattr(args, "fix", False) and authenticated_handle is None:
        warn("--fix skipped: nothing is placed until AgentChat answers for this key")

    if getattr(args, "fix", False) and authenticated_handle is not None:
        # Decided, recorded and placed before the key's old .env line can go:
        # the one-time carry-over reads the pre-0.3 name.
        tools = _doctor_tools_decision(authenticated_handle, api_base=api_base, home=hermes_home)
        carry_failed = False
        if tools.decision == "carried_over":
            from .placement import place_mcp

            carried = place_mcp(api_base, decision="carried_over")
            carry_failed = carried.failed
            (fail if carried.failed else ok)(f"fix: {carried.name} {carried.status} — {carried.detail}")
        if carry_failed:
            warn("fix: the key's old .env line is kept: the carried-over tools entry did not land")
        else:
            for level, line in _fix_key(
                resolution, handle=authenticated_handle, api_base=api_base, home=hermes_home
            ):
                {"ok": ok, "warn": warn, "fail": fail}[level](line)
        for step in place_identity_and_tools(
            handle=authenticated_handle,
            identity_line=identity_line,
            api_base=api_base,
            served_manual=served_texts.manual if served_texts is not None else None,
            hermes_home=hermes_home,
            identity_template=identity_template,
            # Another agent's block is a mismatch to fix (the key, or the
            # block), never something `--fix` decides by replacing it.
            replace_other_agent=False,
            tools_decision=tools.decision if tools.decision in ("yes", "no") else None,
        ):
            if step.name == "mcp" and step.outcome == "missing":
                continue  # the AgentChat tools line below says it
            report = fail if step.failed else warn if step.status == "warned" else ok
            report(f"fix: {step.name} {step.status} — {step.detail}")
        if tools.decision in ("yes", "no"):
            ok(f"fix: {tools.line}")

    # Offline, the block is read against the cached texts for the handle this
    # profile saved (as `status` does), not left "not verified".
    soul_handle = authenticated_handle
    if soul_handle is None and identity_line is not None:
        soul_handle = _read_saved_handle()
    _doctor_soul_checks(
        handle=soul_handle,
        identity_line=identity_line,
        template=identity_template,
        cmd=cmd,
        ok=ok,
        warn=warn,
        fail=fail,
    )

    _doctor_manual_checks(
        hermes_home,
        served=served_texts.manual if served_texts is not None else None,
        worker=_health_worker(api_base, saved_key),
        cmd=cmd,
        ok=ok,
        warn=warn,
        fail=fail,
    )

    _doctor_secrets_checks(resolution, cmd=cmd, ok=ok, warn=warn, fail=fail)

    try:
        from .mcp_registration import (
            McpRegistrationError,
            client_header_lines,
            hosted_mcp_url,
            inspect_entry_identity,
        )

        try:
            entry_state, identity = inspect_entry_identity(api_base=api_base)
        except McpRegistrationError as exc:
            warn(f"hosted-tools entry not checked — {exc}")
        else:
            set_headers = (
                "set this header under its `headers:` (replace any existing X-AgentChat-Client "
                "line; a key may appear only once, in any casing), then restart the gateway or "
                "/reload-mcp:\n" + client_header_lines("      ")
            )
            if entry_state == "ours":
                ok(
                    f"hosted tools entry in config.yaml: mcp_servers.agentchat → {hosted_mcp_url(api_base)} "
                    "(tools appear as mcp__agentchat__<tool>; `hermes mcp test agentchat` probes it; "
                    "whether the running gateway has them connected is the WS daemon section below)"
                )
                if identity == "missing":
                    warn(
                        "mcp_servers.agentchat does not say it is Hermes (no X-AgentChat-Client "
                        "header), so the hosted tools treat this agent as an unknown client and "
                        f"offer it webhook-setup tools it does not need — `{cmd} doctor "
                        "--fix` adds the header (the gateway also does at its next start)"
                    )
                elif identity == "other":
                    warn(
                        "X-AgentChat-Client in mcp_servers.agentchat names another client, not "
                        "hermes — left alone; the hosted tools treat this agent as that client"
                    )
            elif entry_state == "user_managed":
                ok("mcp_servers.agentchat is managed by hand in config.yaml — left alone")
                if identity == "other":
                    warn(
                        "X-AgentChat-Client in the hand-written mcp_servers.agentchat names another "
                        "client, so the hosted tools treat this agent as that client; to say it is "
                        "Hermes, " + set_headers
                    )
                elif identity == "missing":
                    warn(
                        "the hand-written mcp_servers.agentchat does not say it is Hermes, so the "
                        "hosted tools treat this agent as an unknown client and offer it "
                        "webhook-setup tools it does not need; " + set_headers
                    )
            elif entry_state == "disabled":
                warn(
                    "mcp_servers.agentchat is disabled in config.yaml — the agent has no "
                    "AgentChat tools"
                )
    except ImportError:
        warn("mcp_registration module not importable — skipping tool-source check")
    _doctor_tools_switch(
        hermes_home,
        api_base=api_base,
        handle=authenticated_handle or saved_handle,
        cmd=cmd,
        ok=ok,
        warn=warn,
        fail=fail,
    )

    gateway = _gateway_running(api_base, saved_key, hermes_home)
    if gateway is not None:
        ok(f"Hermes gateway running ({gateway})")
    else:
        warn(
            "No `hermes gateway` process detected — start one "
            "(`hermes gateway`) for live inbound delivery"
        )

    # Report the WS leader-lock state for THIS account. The doctor
    # itself never holds the lock (it's a one-shot CLI invocation), so
    # the meaningful states are "held" (gateway running — good), "free"
    # (no leader — bad if a gateway should be running), or "not present"
    # (file never created — gateway hasn't run since last cleanup).
    try:
        from .leader_lock import describe_lock_holder, legacy_lock_path, lock_path_for
        from .profile_scope import account_fingerprint

        lock_dir_raw = (resolve_setting("AGENTCHATME_LOCK_DIR") or "").strip()
        lock_path = lock_path_for(
            hermes_home=hermes_home,
            account_fingerprint=account_fingerprint(api_base, saved_key),
            lock_dir=Path(lock_dir_raw).expanduser() if lock_dir_raw else None,
        )
        state = describe_lock_holder(lock_path)
        if legacy_lock_path().exists():
            warn(
                f"Legacy lock file {legacy_lock_path()} is from a version "
                "before 0.2.3111111 and is no longer used — safe to delete"
            )
        if state == "held":
            ok(f"WS leader lock held for this account ({lock_path})")
            # The lock proves a process owns the receive role for this
            # account; the heartbeat record beside it proves the daemon
            # thread is alive and connected. Both are needed to say
            # "receiving".
            from .ws_health import (
                HEALTH_STALE_AFTER_SECONDS,
                health_age_seconds,
                health_path_for,
                read_ws_health,
            )

            health = read_ws_health(health_path_for(lock_path))
            if health is None:
                fail(
                    "WS leader lock is held but the daemon health record is "
                    "missing or invalid — restart `hermes gateway`"
                )
            else:
                age = health_age_seconds(health)
                health_state = health["state"]
                if age > HEALTH_STALE_AFTER_SECONDS:
                    fail(
                        "WS leader lock is held but daemon health is stale "
                        f"({age:.0f}s old, pid={health['pid']}) — restart `hermes gateway`"
                    )
                elif health_state == "connected":
                    ok(
                        "WS daemon connected "
                        f"(@{health['handle']}, pid={health['pid']}, heartbeat {age:.0f}s ago)"
                    )
                elif health_state in {"starting", "reconnecting", "restart_wait"}:
                    warn(
                        "WS daemon is recovering "
                        f"(state={health_state}, attempt={health['restart_attempt']}, "
                        f"heartbeat {age:.0f}s ago)"
                    )
                else:
                    fail(
                        "WS leader lock is held but the daemon reports stopped — "
                        "restart `hermes gateway`"
                    )
                worker_report = _describe_worker(health.get("worker"), now=time.time())
                if worker_report is not None:
                    level, text = worker_report
                    {"ok": ok, "warn": warn, "fail": fail}[level](text)
                tools_report = _describe_hosted_tools(health.get("worker"), now=time.time())
                if tools_report is not None:
                    level, text = tools_report
                    {"ok": ok, "warn": warn, "fail": fail}[level](text)
                scope_line = _describe_scope(health.get("worker"))
                if scope_line is not None:
                    warn(scope_line)
                for level, text in _describe_could_not_process(health.get("worker"), now=time.time()):
                    {"ok": ok, "warn": warn, "fail": fail}[level](text)
        elif state in ("free", "not present") and gateway is not None:
            # The gateway runs; AgentChat's runtime in it does not hold this
            # account's socket (it retries while AgentChat cannot be reached,
            # or its start failed: the gateway log says which).
            warn(
                f"WS leader lock not held ({lock_path}) — the gateway runs, but AgentChat's runtime in it "
                "has not started for this account: it retries on its own while AgentChat cannot be "
                "reached (the gateway log says why); no wakes until it does"
            )
        elif state == "free":
            warn(
                f"WS leader lock file present but no holder ({lock_path}) — "
                "gateway is not running"
            )
        elif state == "not present":
            warn(
                f"WS leader lock file missing ({lock_path}) — gateway "
                "has not run since the last cleanup"
            )
        else:
            warn(f"WS leader lock state: {state}")
    except ImportError:
        warn("leader_lock module not importable — skipping lock check")

    return _doctor_finalize(failures, warnings_count)


# A worker turn that runs this long is reported, though its own time limit
# (``AGENTCHATME_TURN_MAX_S``) is what ends it.
_WORKER_LONG_TURN_S = 900.0
# This many cycles in a row that did not get through (``recent_cycles``: the
# model or the tray unreachable, no texts, no hosted tools, no turn can
# start, a cycle abandoned) is an error.
_WORKER_BAD_CYCLES = 3


# What to do about a cycle that did not get through, by its outcome.
_CYCLE_REMEDIES = {
    "tools_unavailable": (
        "; the hosted tools line below says why — /reload-mcp in a chat this gateway serves, or "
        "`hermes gateway restart`, reconnects them"
    ),
}


def _doctor_tools_switch(
    hermes_home: Path,
    *,
    api_base: str,
    handle: str | None,
    cmd: str,
    ok: Callable[[str], None],
    warn: Callable[[str], None],
    fail: Callable[[str], None],
) -> None:
    """The one switch (:func:`.allow.read_switch`): not decided yet is a
    failure (setup is not finished: the agent has no AgentChat tools); a no,
    a yes being applied, or a trust changed by hand is a warning."""
    switch = allow.read_switch(hermes_home, api_base=api_base)
    if switch.state == "disabled":
        return  # said above
    if switch.state == "unknown":
        warn("AgentChat tools: not checked — this Hermes' config.yaml could not be read here")
        return
    text = f"AgentChat tools: {allow.describe(switch, handle, cmd=cmd)}"
    if switch.state == "not_allowed_yet":
        fail(text)
    elif switch.state == "allowed" and not switch.hand_edited:
        ok(text)
    else:
        warn(text)


def _doctor_tools_decision(handle: str, *, api_base: str, home: Path) -> allow.SetupDecision:
    """``doctor --fix``'s decision about the tools: the one-time carry-over
    when it still applies (the pre-0.3 name set, no entry, never placed — the
    same rule as at activation, recorded before the old ``.env`` line can
    go), else the one question for a person at their own terminal."""
    from .mcp_registration import McpRegistrationError, inspect_entry

    try:
        entry = inspect_entry(api_base=api_base)
    except McpRegistrationError:
        entry = None
    if entry is not None and allow.first_placement(
        home, entry=entry, legacy_key_set=allow.legacy_key_set()
    ) == "carried_over":
        return allow.SetupDecision(
            "carried_over", "AgentChat tools allowed: carried over from your setup under an earlier release"
        )
    return allow.decide_for(handle)


def _describe_scope(worker: Any) -> str | None:
    """The warn line when the running gateway multiplexes on a Hermes that rebuilds a
    plugin's secret scope from the profile's files only (older than 0.23.2)."""
    section = worker.get("scope") if isinstance(worker, dict) else None
    if not isinstance(section, dict):
        return None
    if section.get("multiplexed") is True and section.get("keeps_launch_env") is False:
        return (
            "The running gateway serves several profiles on a Hermes older than 0.23.2: keys set only "
            "in the gateway's environment (systemd Environment=, op run, docker -e) are not visible to "
            "AgentChat's gate calls and turns — update Hermes"
        )
    return None


def _describe_hosted_tools(worker: Any, *, now: float) -> tuple[str, str] | None:
    """``(ok|warn|fail, line)`` for the running gateway's hosted tools server,
    as Hermes reads it there (the health record's ``worker.hosted_tools``,
    written by the gateway every minute). ``None`` when the record has none
    (an older plugin, or a Hermes whose MCP state is not readable)."""
    from .hosted_tools import PARKED_REMEDY, ServerState, describe_cause

    section = worker.get("hosted_tools") if isinstance(worker, dict) else None
    if not isinstance(section, dict) or not isinstance(section.get("status"), str):
        return None
    status = section["status"]
    error = section.get("error") if isinstance(section.get("error"), str) else None
    tools = section.get("tools") if isinstance(section.get("tools"), int) else 0
    at = section.get("at")
    age = f", read {now - float(at):.0f}s ago" if isinstance(at, (int, float)) else ""
    if status == "connected":
        return "ok", f"The running gateway has the hosted tools connected ({tools} tool(s){age})"
    if status == "lazy":
        return "ok", f"The running gateway has the hosted tools registered ({tools} tool(s); they connect on first use{age})"
    if status == "connecting":
        return "warn", f"The running gateway is still connecting the hosted tools{age}"
    if status == "configured":
        return "warn", f"The running gateway has not connected the hosted tools yet{age}"
    if status == "failed":
        why = f" ({error})" if error else ""
        return "fail", (
            f"The running gateway has no hosted AgentChat tools: Hermes parked mcp_servers.agentchat after its "
            f"connection failed{why}, so the agent is woken but cannot answer. {PARKED_REMEDY[0].upper()}"
            f"{PARKED_REMEDY[1:]}"
        )
    return "fail", f"The running gateway has no hosted AgentChat tools: {describe_cause(ServerState(status=status, error=error))}"


def _describe_worker(worker: Any, *, now: float) -> tuple[str, str] | None:
    """``(ok|warn|fail, line)`` for the health record's ``worker`` section.

    ``None`` when the record has none (written by a version before the
    worker was supervised). The socket can be connected while the worker
    that turns wakes into turns is dead or stuck; this is how that shows.
    """
    from .ws_health import HEALTH_STALE_AFTER_SECONDS, bad_cycle_run

    if not isinstance(worker, dict):
        return None
    state = str(worker.get("state") or "unknown")
    restarts = worker.get("restarts") if isinstance(worker.get("restarts"), int) else 0
    hung = worker.get("hung_turns") if isinstance(worker.get("hung_turns"), int) else 0
    beat = worker.get("heartbeat_at")
    beat_age = now - float(beat) if isinstance(beat, (int, float)) else None
    since = worker.get("since")
    in_state = now - float(since) if isinstance(since, (int, float)) else None
    extra = f", restarts={restarts}" if restarts else ""
    last_error = worker.get("last_error")
    if worker.get("alive") is False and state != "stopped":
        return "fail", f"Wake worker thread is not running (state={state}) — restart `hermes gateway`"
    if state == "restart_wait":
        return "warn", f"Wake worker is restarting after a crash ({last_error or 'no error text'}{extra})"
    if beat_age is not None and beat_age > HEALTH_STALE_AFTER_SECONDS:
        # The worker loop beats on every idle tick (60 s) and a running turn's
        # watch every second; only a worker stuck outside both stops it.
        return "fail", (
            f"Wake worker has not moved for {beat_age:.0f}s (state={state}) while the socket is up "
            "— restart `hermes gateway`"
        )
    max_hung = worker.get("max_hung_turns") if isinstance(worker.get("max_hung_turns"), int) else None
    if hung and max_hung is not None and hung >= max_hung:
        return "fail", (
            f"{hung} AgentChat turns overran their time limit and have not ended — no new turn "
            "can start until one does (restart `hermes gateway` if they never end)"
        )
    if hung:
        return "warn", (
            f"{hung} AgentChat turn(s) overran their time limit and have not ended; their "
            "conversations get no new turn until they do"
        )
    loop_raw = worker.get("loop")
    loop: dict[str, Any] = loop_raw if isinstance(loop_raw, dict) else {}
    bad, newest = bad_cycle_run(loop.get("recent_cycles"))
    switch = worker.get("tools_allowed")
    paused = switch.get("state") if isinstance(switch, dict) else None
    if bad and newest is not None and newest.get("outcome") == "tools_unavailable" and paused in allow.PAUSED_STATES:
        # Paused by the switch (the person's choice, or a yes being applied):
        # said with its remedy, not alarmed on.
        return "warn", (
            f"The receive loop is paused: {allow.reason(paused)} — AgentChat messages wait on the tray"
        )
    if bad and newest is not None:
        what = str(newest.get("outcome") or "?")
        why = f": {newest['failure']}" if newest.get("failure") else ""
        remedy = _CYCLE_REMEDIES.get(what, "")
        if bad >= _WORKER_BAD_CYCLES:
            return "fail", (
                f"The receive loop's last {bad} cycles did not get through ({what}{why}) — "
                f"AgentChat messages are not being answered{remedy}"
            )
        return "warn", f"The receive loop's last cycle did not get through ({what}{why}){remedy}"
    if state == "turn" and in_state is not None and in_state > _WORKER_LONG_TURN_S:
        return "warn", f"An AgentChat turn has been running for {in_state:.0f}s"
    last_turn = worker.get("last_turn")
    tail = ""
    if isinstance(last_turn, dict) and last_turn.get("status"):
        tail = f", last turn {last_turn['status']}"
        calls = last_turn.get("agentchat_calls")
        if isinstance(calls, dict) and isinstance(calls.get("made"), int):
            tail += f" with {calls['made']} AgentChat call(s)"
    return "ok", f"Wake worker {state}{extra}{tail}"


def _doctor_finalize(failures: int, warnings_count: int) -> int:
    """Print the doctor footer and return the exit code.

    Exit code is the failure count so CI can gate on a clean
    doctor run. Warnings are surfaced but don't fail the check.
    """
    _printline("")
    if failures == 0 and warnings_count == 0:
        _printline("All checks passed.")
    elif failures == 0:
        _printline(f"{warnings_count} warning(s); no failures.")
    else:
        _printline(
            f"{failures} failure(s), {warnings_count} warning(s). "
            "Resolve the [fail] lines above."
        )
    _printline("")
    return failures


def _doctor_key_checks(
    resolution: KeyResolution,
    *,
    cmd: str,
    ok: Callable[[str], None],
    warn: Callable[[str], None],
    fail: Callable[[str], None],
) -> None:
    """Where this profile's key is, and whether this profile may act as it."""
    store = resolution.store
    if resolution.refusal is not None:
        fail(resolution.refusal)
    elif resolution.source == KEY_SOURCE_STORE and resolution.api_key:
        decides = (
            "the key file decides which agent this profile is; an AGENTCHAT_API_KEY in the "
            "profile .env or the environment is not used"
        )
        ok(f"key saved in {store.path} ({_mask_key(resolution.api_key)}, readable by you only) — {decides}"
           if not store.loose_mode else
           f"key saved in {store.path} ({_mask_key(resolution.api_key)}) — {decides}")
        if store.loose_mode:
            warn(f"{store.path} can be read by its group or others — `{cmd} doctor --fix` makes it "
                 "readable by you only")
    elif resolution.api_key:
        masked = _mask_key(resolution.api_key)
        if resolution.source == _ENV_API_KEY_LEGACY:
            warn(
                f"key found only under the pre-v3 name {_ENV_API_KEY_LEGACY} ({masked}) — the "
                f"gateway copies it into {store.path} when it next starts; `{cmd} doctor --fix` "
                "moves it now"
            )
        else:
            warn(
                f"key read from this profile's settings as {resolution.source} ({masked}) — Hermes "
                "reads it from the profile .env or the environment, where the agent's shell can "
                "read it and `hermes profile create` copies it into every new profile; "
                f"`{cmd} doctor --fix` moves it to {store.path}"
            )
    else:
        fail(f"no AgentChat key in this profile — run `{cmd} register` or `{cmd} login`")
    for note in resolution.notes:
        warn(note)
    for holder in resolution.shared_with:
        fail(shared_note(holder))
    if resolution.held_key:
        snapshots = _snapshots_holding_the_key(store.path.parent.parent)
        if snapshots:
            warn(
                f"{len(snapshots)} Hermes terminal snapshot(s) in {snapshots[0].parent} still hold "
                f"{_ENV_API_KEY} from before it was kept out of the agent's commands — delete them "
                f"(`rm {snapshots[0].parent}/hermes-snap-*.sh`); the agent's commands could read the "
                f"key then, so consider `{cmd} recover` to rotate it"
            )


def _snapshots_holding_the_key(home: Path) -> list[Path]:
    """Hermes' terminal env snapshots (``cache/scratch/hermes-snap-*.sh``) that export the key.

    Hermes wrote the agent's shell environment there; up to 0.5.3 that
    included ``AGENTCHAT_API_KEY``. Only the variable's name is looked for.
    """
    found: list[Path] = []
    try:
        candidates = sorted((home / "cache" / "scratch").glob("hermes-snap-*.sh"))
    except OSError:
        return found
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"{_ENV_API_KEY}=" in text or f"{_ENV_API_KEY_LEGACY}=" in text:
            found.append(path)
    return found


def _doctor_secrets_checks(
    resolution: KeyResolution,
    *,
    cmd: str,
    ok: Callable[[str], None],
    warn: Callable[[str], None],
    fail: Callable[[str], None],
) -> None:
    """``secrets.agentchat`` (the key source, and the key kept out of the agent's commands)."""
    from .key_source import source_supported
    from .mcp_registration import McpRegistrationError
    from .secrets_registration import inspect_block, manual_snippet

    has_key = resolution.api_key is not None
    in_store = resolution.source == KEY_SOURCE_STORE and has_key
    if has_key and not source_supported():
        (fail if in_store else warn)(
            "this Hermes has no secret-source support (agent.secret_sources), so the hosted tools "
            "cannot read the key file — upgrade Hermes"
        )
    try:
        state = inspect_block()
    except McpRegistrationError as exc:
        warn(f"secrets.agentchat not checked — {exc}")
        return
    except Exception as exc:  # Hermes' reader raised: an unreadable or invalid config.yaml
        fail(
            f"Hermes could not read this profile's config.yaml ({type(exc).__name__}: {exc}): "
            "secrets.agentchat and the hosted tools entry cannot be checked — fix the file"
        )
        return
    if state == "ours":
        ok("secrets.agentchat in config.yaml: Hermes reads the key from the key file for the hosted "
           "tools and is told to keep AGENTCHAT_API_KEY out of the agent's commands")
    elif state == "disabled":
        (fail if in_store else warn)(
            "secrets.agentchat is disabled in config.yaml — the hosted tools cannot read the key file"
        )
    elif state == "user_managed":
        warn("secrets.agentchat in config.yaml is written by hand and is not the plugin's — left "
             "alone; the plugin's is:\n" + manual_snippet())
    elif has_key:
        (fail if in_store else warn)(
            "secrets.agentchat missing from config.yaml — the hosted tools cannot read the key file "
            f"and the agent's commands can read AGENTCHAT_API_KEY; `{cmd} doctor --fix` places it"
        )
    if not has_key:
        return
    if in_store and resolution.api_key:
        conflict = credentials.tools_key_conflict(resolution.store.path.parent.parent, resolution.api_key)
        if conflict is not None:
            fail(conflict)
    stripped = _hermes_strips_key_from_commands()
    if stripped is True:
        ok("Hermes strips AGENTCHAT_API_KEY from the commands the agent runs (checked)")
    elif stripped is False:
        warn(
            "this Hermes does not strip AGENTCHAT_API_KEY from the commands the agent runs: the key "
            "is in their environment. Needs Hermes v0.23.2 or later with secrets.agentchat in "
            "config.yaml"
        )


def _hermes_strips_key_from_commands() -> bool | None:
    """Would Hermes drop ``AGENTCHAT_API_KEY`` from a command the agent runs, for this profile?

    Asks Hermes' own child-environment policy (``tools.environments.
    local_env_policy._is_provider_env_blocklisted``: what its terminal,
    background-process and snapshot paths consult). ``None`` when this Hermes
    has no such policy to ask.
    """
    try:
        from tools.environments.local_env_policy import _is_provider_env_blocklisted
    except Exception:
        return None
    try:
        return bool(_is_provider_env_blocklisted(_ENV_API_KEY))
    except Exception:
        logger.debug("doctor: Hermes' env policy raised", exc_info=True)
        return None


def _fix_key(
    resolution: KeyResolution, *, handle: str, api_base: str, home: Path
) -> list[tuple[str, str]]:
    """``doctor --fix`` for the key: move it out of the profile's settings, record
    this profile, tighten modes, remove a leftover pre-v3 line."""
    lines: list[tuple[str, str]] = []
    if resolution.refusal is not None or not resolution.api_key:
        return lines
    store = resolution.store
    if resolution.source == KEY_SOURCE_STORE:
        try:
            if store.status == "moved":
                key_store.write_store(
                    home, api_key=resolution.api_key, handle=handle, api_base=api_base
                )
                lines.append(("ok", f"fix: {store.path} now records this profile"))
            elif store.loose_mode and key_store.tighten(home):
                lines.append(("ok", f"fix: {store.path} made readable by you only"))
        except key_store.KeyStoreError as exc:
            lines.append(("fail", f"fix: key file not updated — {exc}"))
        lines.extend(_fix_legacy_leftover(resolution.api_key, home=home))
        return lines
    ready, why = credentials.key_bridge()
    if not ready:
        lines.append(("fail", f"fix: the key stays in this profile's settings — {why}"))
        return lines
    moved = credentials.move_env_key(handle=handle, api_base=api_base, home=home, lock_dir=_lock_dir())
    if moved.moved:
        lines.append((
            "ok",
            f"fix: moved the key from this profile's settings to {moved.path} (readable by you only); "
            "restart the gateway (and any open Hermes session) so the hosted tools read it there",
        ))
        if moved.detail:
            lines.append(("warn", f"fix: {moved.detail}"))
    else:
        lines.append(("fail", f"fix: key not moved — {moved.detail}"))
    return lines


def _fix_legacy_leftover(api_key: str, *, home: Path) -> list[tuple[str, str]]:
    """The pre-v3 setting still holding the saved key: removed from the profile .env."""
    from .profile_scope import ProfileScopeError, resolve_setting

    try:
        legacy = (resolve_setting(_ENV_API_KEY_LEGACY) or "").strip()
    except ProfileScopeError:
        return []
    if legacy != api_key:
        return []  # unset, or another key: not this fix's to remove
    if credentials.remove_key_settings(home, (_ENV_API_KEY_LEGACY,)):
        return [("ok", f"fix: removed {_ENV_API_KEY_LEGACY} from the profile .env")]
    return [(
        "warn",
        f"fix: {_ENV_API_KEY_LEGACY} is not in the profile .env — it is set in the environment "
        "(the shell or the service): unset it there",
    )]


def _lock_dir() -> Path | None:
    """``AGENTCHATME_LOCK_DIR`` for the profile in scope (``None``: the default)."""
    from .profile_scope import resolve_setting

    raw = (resolve_setting("AGENTCHATME_LOCK_DIR") or "").strip()
    return Path(raw).expanduser() if raw else None


def _gateway_running(api_base: str, api_key: str, hermes_home: Path) -> str | None:
    """How ``doctor`` knows a Hermes gateway is running, or ``None``. In order:

    1. this account's health record, while its gateway lives (:func:`_live_health`:
       fresh, its pid alive, the leader lock held);
    2. Hermes' own answer for this profile, ``gateway.status.get_running_pid``
       (public; read-only with ``cleanup_stale=False``): its lock and pid file
       checked against the live process, however it was launched;
    3. a process whose command line runs ``hermes gateway`` (argv, also a
       ``sys.argv = [..., 'gateway', 'run']`` launcher).
    """
    health = _live_health(api_base, api_key, hermes_home)
    if health is not None:
        return f"pid {health['pid']}, holding this account's socket"
    try:
        from gateway.status import get_running_pid  # type: ignore[import-not-found]

        pid = get_running_pid(cleanup_stale=False)
        if pid is not None:
            return f"pid {pid}, Hermes' gateway lock for this profile"
    except Exception:
        logger.debug("doctor: Hermes' gateway status not read", exc_info=True)
    if _other_gateway_running():
        return "found by its command line"
    return None


_ARGV_GATEWAY_RUN = re.compile(r"""['"]gateway['"]\s*,\s*['"]run['"]""")


def _other_gateway_running() -> bool:
    """Best-effort: is a *different* ``hermes gateway`` process alive?

    The doctor itself is a CLI process, so we exclude our own pid. A
    ``gateway`` argument, or a launcher that sets ``sys.argv = [...,
    'gateway', 'run']`` in its ``-c`` code. Cross-platform via ``psutil`` when
    available, falls back to ``False`` (informational warning) when not.
    """
    try:
        import os

        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return False

    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == own_pid:
                    continue
                cmdline = proc.info.get("cmdline") or []
                if not cmdline:
                    continue
                joined = " ".join(str(arg) for arg in cmdline)
                if "hermes" in joined and ("gateway" in cmdline or _ARGV_GATEWAY_RUN.search(joined)):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        logger.debug("doctor: process iteration failed", exc_info=True)
    return False


def _dispatch_logout(_args: argparse.Namespace) -> int:
    """Remove this profile's AgentChat key — the key file and any ``.env`` line — and our placements.

    Only this profile: a copy of the key another profile holds is that
    profile's and stays.
    """
    from .config import settings_key
    from .profile_scope import resolve_hermes_home

    home = resolve_hermes_home()
    resolution = resolve_key(home)
    has_file = resolution.store.status != "missing"
    # What this profile's settings hold, read before anything is removed (Hermes
    # removes a line and clears the variable from this process together).
    in_settings = settings_key()
    if not has_file and resolution.held_key is None and in_settings is None:
        _printline("No AgentChat key was configured. Nothing to clear.")
        return _EXIT_OK
    # Read while the key is still here: who this profile is (only the block
    # AgentChat placed for that agent leaves SOUL.md), and whether a running
    # gateway holds the account's socket (it keeps it until it restarts).
    identity = profile_identity(home, api_base=_api_base())
    gateway_note = gateway_still_serving(home, api_base=_api_base())
    try:
        removed_file, removed_lines = credentials.clear_credentials(home)
    except key_store.KeyStoreError as exc:
        return _exit_with(f"The key was not removed: {exc}")
    _uninstall_soul_anchor(identity)
    _uninstall_secrets_registration()
    _uninstall_mcp_registration()
    where = []
    if removed_file:
        where.append(str(resolution.store.path))
    if removed_lines:
        where.append(f"the profile .env ({', '.join(removed_lines)})")
    _printline(
        f"AgentChat key removed from this profile ({' and '.join(where) or 'nothing was left'}). "
        "Your account on the server is unchanged."
    )
    # A setting no .env line held is set by the shell or the service. With a
    # key file, AGENTCHAT_API_KEY here was the key file's own (the key source).
    if in_settings is not None:
        value, name = in_settings
        ours = has_file and name == _ENV_API_KEY and value == (resolution.held_key or "")
        if name not in removed_lines and not ours:
            _printline(
                f"{name} is still set in this process's environment (the shell or the "
                "service): unset it there too."
            )
    if gateway_note is not None:
        _printline(f"{gateway_note}.")
    return _EXIT_OK


def _install_placement(
    handle: str, *, api_base: str, api_key: str, tools_decision: Decision | None = None
) -> list[PlacementStep]:
    """Place the identity line and the manual, register the hosted tools.

    The texts (the identity line, the manual's announcement) are fetched from
    the server for this key (the cached copy when the server is unreachable;
    without either the line is skipped and the manual is the bundled copy —
    the gateway places both later). ``tools_decision`` is the person's answer
    about the tools (``None``: nobody asked, so no tools entry is added).
    Failure is non-fatal: credentials are already persisted and the account is
    fully operational. Each failed step is printed with its reason so the
    operator can repair it (``hermes agentchat doctor --fix`` re-runs this).
    """
    from .turn import identity_line as render_identity_line

    texts = resolve_texts(api_key=api_key, api_base=api_base)
    from .turn import identity_line_template as render_identity_line_template

    steps = place_identity_and_tools(
        handle=handle,
        identity_line=render_identity_line(texts) if texts is not None else None,
        api_base=api_base,
        served_manual=texts.manual if texts is not None else None,
        identity_template=render_identity_line_template(texts) if texts is not None else None,
        tools_decision=tools_decision,
    )
    for step in steps:
        if step.name == "mcp" and step.outcome == "missing":
            continue  # the AgentChat tools line after this says it
        if step.needs_attention:
            sys.stderr.write(f"Warning: {step.name}: {step.detail}\n")
            sys.stderr.flush()
        else:
            _printline(f"{step.name}: {step.detail}")
    return steps


def _doctor_soul_checks(
    *,
    handle: str | None,
    identity_line: str | None,
    template: str | None,
    cmd: str,
    ok: Callable[[str], None],
    warn: Callable[[str], None],
    fail: Callable[[str], None],
) -> None:
    """The identity block in SOUL.md, read the way a refresh would: the kit's
    ``decide`` with the served line in hand, else the strict ``owner``. A
    block's words naming ``@handle`` do not make it this agent's (only its
    handle line, or a text AgentChat served word for word, do)."""
    try:
        from .soul_anchor import AnchorError, inspect_soul
    except ImportError:
        warn("soul_anchor module not importable — skipping anchor check")
        return
    try:
        state = inspect_soul(handle=handle, identity_line=identity_line, template=template)
    except AnchorError as exc:
        warn(f"SOUL.md identity line not checked — {exc}")
        return
    where = state.path
    outcome = state.outcome
    if outcome == "absent":
        warn(f"SOUL.md identity line missing — run `{cmd} doctor --fix` (or `{cmd} login`) to place it")
    elif outcome == "unreadable":
        warn(f"SOUL.md identity line not checked — {state.detail}")
    elif outcome == "unchanged":
        ok(f"SOUL.md identity line present (@{state.claimed}, served text)")
    elif outcome == "updated":
        warn(
            f"SOUL.md identity line for @{state.claimed} is not the served one (older text, or the served text "
            f"in an older shape) — `{cmd} doctor --fix` places it (a running gateway also does when the texts change)"
        )
    elif outcome == "other_agent" or (
        outcome == "unverified" and state.claimed is not None and handle is not None and state.claimed != handle
    ):
        fail(
            f"IDENTITY MISMATCH: {where} holds @{state.claimed}'s AgentChat block, but this profile's saved "
            f"key authenticates as @{handle}: the key or that block is another agent's. Fix the key "
            f"(`{cmd} login`) or remove that block (then `{cmd} doctor --fix` places @{handle}'s)"
        )
    elif outcome == "hand_edited":
        names = f"; its words name @{state.named}" if state.named else ""
        warn(
            f"SOUL.md agentchat block was edited by hand: it has no handle line and is not a text AgentChat "
            f"served{names}. It is yours, left alone (also by `{cmd} doctor --fix` and `{cmd} login`) — to have "
            f"the served identity line, delete that block, then run `{cmd} doctor --fix`"
        )
    elif outcome == "several_blocks":
        warn(
            f"{where} holds more than one AgentChat block — left alone (one write would remove the others); "
            f"remove the extra ones by hand, then `{cmd} doctor --fix`"
        )
    elif outcome == "unverified" and state.claimed is not None:
        warn(f"SOUL.md identity line present (@{state.claimed}) — cannot verify its text while /v1/texts is unreachable")
    elif outcome == "unverified":
        warn(
            "SOUL.md agentchat block has no handle line; whether it is a text AgentChat served is checked "
            "when /v1/texts answers"
        )
    else:
        warn(f"SOUL.md identity line: {state.detail}")


def _doctor_account_lines(me: Any, *, ok: Callable[[str], None], warn: Callable[[str], None]) -> None:
    """``doctor``'s lines from ``/v1/agents/me``: status, inbox mode, an owner's pause."""
    status = me.get("status") if isinstance(me, dict) else None
    if status == "active":
        ok(f"Account status: {status}")
    elif status:
        warn(f"Account status: {status} — may suppress live delivery")
    else:
        warn("Account status field missing from /v1/agents/me response")

    settings = me.get("settings") if isinstance(me, dict) else None
    if isinstance(settings, dict):
        inbox_mode = settings.get("inbox_mode")
        if inbox_mode == "open":
            ok(f"Inbox mode: {inbox_mode}")
        elif inbox_mode:
            warn(
                f"Inbox mode: {inbox_mode} — non-contact senders "
                "may be blocked. Adjust on the AgentChat dashboard "
                "if you expect cold inbound."
            )

    paused = me.get("paused_by_owner") if isinstance(me, dict) else None
    if paused and paused != "none":
        warn(
            f"Account paused by owner ({paused}) — agent will "
            "not receive new inbound until un-paused"
        )


def _doctor_cached_texts(
    *, api_key: str, api_base: str, hermes_home: Path
) -> tuple[str | None, str | None, Any, str | None]:
    """``(identity line, template, texts, cached at)`` from the account's cached
    texts only — AgentChat did not answer, so nothing is fetched."""
    try:
        from .state import state_dir_for_account
        from .texts import cached_texts
        from .turn import identity_line as render_identity_line
        from .turn import identity_line_template as render_identity_line_template
    except ImportError:
        return None, None, None, None
    state_dir = state_dir_for_account(hermes_home=hermes_home, api_base=api_base, api_key=api_key)
    texts, fetched_at = cached_texts(state_dir)
    if texts is None:
        return None, None, None, None
    return render_identity_line(texts), render_identity_line_template(texts), texts, fetched_at


def _doctor_identity_texts(
    client: Any, *, api_key: str, api_base: str, hermes_home: Path
) -> tuple[str | None, str | None, Any]:
    """``(identity line, template, texts)`` for ``doctor``: one fetch on the
    open client, else the cache; ``(None, None, None)`` without either."""
    texts = _load_served_texts(client, api_key=api_key, api_base=api_base, hermes_home=hermes_home)
    if texts is None:
        return None, None, None
    from .turn import identity_line as render_identity_line
    from .turn import identity_line_template as render_identity_line_template

    return render_identity_line(texts), render_identity_line_template(texts), texts


def _load_served_texts(client: Any, *, api_key: str, api_base: str, hermes_home: Path) -> Any:
    """The served texts for ``doctor`` and ``status``: one fetch on the open
    client, else the account's cached copy, else ``None``. Never raises."""
    try:
        from .state import state_dir_for_account
        from .texts import load_texts
    except ImportError:
        return None
    state_dir = state_dir_for_account(hermes_home=hermes_home, api_base=api_base, api_key=api_key)
    try:
        return load_texts(client, state_dir=state_dir)
    except Exception as exc:
        logger.debug("cli: texts load raised: %s", exc)
        return None


# ───────────────────────── the manual ─────────────────────────


def _registered_manual(hermes_home: Path) -> tuple[Path | None, str | None]:
    """``(path, reason)``: the file Hermes holds as ``agentchat:agentchat`` in
    this process — Hermes' own registry when it can be asked, else what this
    plugin's ``register()`` gave it — and, for the bundled copy, why.
    ``(None, None)`` when neither knows (``doctor`` run outside Hermes)."""
    from .manual import registration_for

    ours = registration_for(hermes_home)
    reason = ours.reason if ours is not None else None
    held: Path | None = None
    try:
        from hermes_cli.plugins import get_plugin_manager

        found = get_plugin_manager().find_plugin_skill("agentchat:agentchat")
        held = Path(found) if found is not None else None
    except Exception:
        held = None
    if held is None and ours is not None:
        held = ours.path
    if held is not None and ours is not None and held != ours.path:
        reason = None  # Hermes holds a path this process did not give it
    return held, reason


def _routine_registration(hermes_home: Path) -> bool:
    """This process registered the package's copy only because nothing was
    placed yet when it started (it places nothing itself): not a fault to report."""
    from .manual import registration_for

    registration = registration_for(hermes_home)
    return registration is not None and registration.routine


def _manual_line(hermes_home: Path, served: Any) -> str:
    """The manual in one line, for ``status``."""
    from .manual import bundled_version, check, manual_path

    target = manual_path(hermes_home)
    state = check(target, served)
    held, reason = _registered_manual(hermes_home)
    if _routine_registration(hermes_home) and state.state == "missing":
        return (
            f"missing — {target} is not placed yet; Hermes sessions read the bundled copy "
            f"{bundled_version() or '?'} until the gateway, a login or `doctor --fix` places it"
        )
    if held is not None and held != target:
        return (
            f"frozen at the bundled copy {bundled_version() or '?'} ({held}){f': {reason}' if reason else ''} "
            f"— {target}: {state.summary}"
        )
    return state.detail


def _doctor_manual_checks(
    hermes_home: Path,
    *,
    served: Any,
    worker: Any,
    cmd: str,
    ok: Callable[[str], None],
    warn: Callable[[str], None],
    fail: Callable[[str], None],
) -> None:
    """The manual: the kept-current file against the texts' announcement, the
    file Hermes serves as ``agentchat:agentchat``, the gateway's last sync,
    the skill's folder, and any other ``agentchat`` skill in Hermes' own
    skill folders (reported, never removed)."""
    from .manual import MANUAL_URL, bundled_version, check, manual_path, other_files

    target = manual_path(hermes_home)
    bundled = bundled_version() or "?"
    state = check(target, served)
    if state.state == "current":
        ok(f"manual {state.detail} (kept current from {MANUAL_URL})")
    elif state.state == "missing" and _routine_registration(hermes_home):
        fail(
            f"manual {state.summary} at {target} — not placed yet: Hermes sessions read the bundled "
            f"copy {bundled} until the gateway, a login or `{cmd} doctor --fix` places it"
        )
    elif state.state == "missing":
        fail(
            f"manual {state.summary} at {target} — the agent reads the bundled copy, frozen at "
            f"{bundled}, until this profile's home lets the plugin write it (`{cmd} doctor --fix` "
            "tries again)"
        )
    elif state.state == "stale":
        warn(
            f"manual {state.detail} — `{cmd} doctor --fix` downloads the served one; "
            f"{_gateway_manual_outlook(worker, cmd)}"
        )
    elif state.state == "tampered":
        warn(
            f"manual {state.detail} — not the text the server serves (edited after it was "
            f"placed); `{cmd} doctor --fix` puts the served one back"
        )
    elif state.state == "unsafe":
        fail(
            f"manual {state.detail} — Hermes runs inline shell in a skill it serves when "
            f"skills.inline_shell is on, so the plugin does not register this file; `{cmd} doctor "
            "--fix` puts a manual without it back"
        )
    else:
        warn(f"manual {state.detail} — it cannot be compared until /v1/texts answers")

    held, reason = _registered_manual(hermes_home)
    if held is not None and not _routine_registration(hermes_home):
        if held == target:
            ok(f"Hermes serves agentchat:agentchat from {target}")
        else:
            warn(
                f"Hermes serves agentchat:agentchat from {held}, not the kept-current file — frozen "
                f"at the bundled {bundled}{f': {reason}' if reason else ''}"
            )

    section = worker.get("manual") if isinstance(worker, dict) else None
    if isinstance(section, dict):
        registered = section.get("registered")
        if isinstance(registered, dict) and registered.get("kept_current") is False:
            why = registered.get("reason")
            warn(
                f"the gateway serves the bundled copy {registered.get('path')}, frozen at {bundled}"
                + (f": {why}" if why else "")
                + f" — restart the gateway once {target} is in place"
            )
        last = section.get("last_sync")
        if isinstance(last, dict):
            at = last.get("at")
            age = f", {time.time() - float(at):.0f}s ago" if isinstance(at, (int, float)) else ""
            detail = last.get("detail") or last.get("state")
            if last.get("up_to_date") is False or last.get("state") == "failed":
                warn(f"the gateway's last manual sync{age}: {detail}")
            else:
                ok(f"the gateway keeps the manual current (last sync{age}: {detail})")

    others = other_files(target.parent)
    if others:
        warn(
            f"{target.parent} holds other files ({', '.join(others[:5])}): skill_view serves every "
            "file in a skill's folder, so the plugin registers the bundled copy until they are moved out"
        )

    for path, version in _stray_agentchat_skills(exclude=(target,)):
        warn(
            f"another agentchat skill at {path} ({version or 'no version'}): Hermes lists it as "
            "`agentchat` beside agentchat:agentchat, and that copy never updates — remove its folder "
            "unless you keep it on purpose (doctor never deletes it)"
        )


def _gateway_manual_outlook(worker: Any, cmd: str) -> str:
    """What the running gateway will do about a stale manual, from its last
    recorded sync (the health record's ``worker.manual.last_sync``). The kit
    tries again by itself only after a sync that left the file not the served
    one; after a current one it looks again only when the served texts change
    or the gateway restarts, so a file changed behind it stays as it is."""
    section = worker.get("manual") if isinstance(worker, dict) else None
    last = section.get("last_sync") if isinstance(section, dict) else None
    if worker is None:
        return "no running gateway keeps it current for this account"
    if not isinstance(last, dict):
        return "the running gateway has recorded no sync of it yet"
    at = last.get("at")
    ago = f" {time.time() - float(at):.0f}s ago" if isinstance(at, (int, float)) else ""
    if last.get("up_to_date") is False or last.get("state") == "failed":
        from .kit import manual_retry_problem

        if manual_retry_problem() is None:
            return (
                f"the gateway's last sync{ago} did not bring it current, so it tries again on its own "
                "(after 5 min, doubling to 1 h)"
            )
        return f"the gateway's last sync{ago} did not bring it current; it tries again when the served texts change"
    if last.get("up_to_date") is True:
        return (
            f"the gateway's last sync{ago} found it current, so the gateway does not look at it again until "
            f"the served texts change or it restarts: run `{cmd} doctor --fix` or `hermes gateway restart`"
        )
    return "the gateway had no served version to compare with at its last sync; it syncs once the texts arrive"


def _stray_agentchat_skills(*, exclude: tuple[Path, ...] = ()) -> list[tuple[Path, str | None]]:
    """``(SKILL.md, version)`` of a skill Hermes loads as ``agentchat`` from its
    own skill folders (the profile's ``skills/``, ``create_dir``, external and
    trusted project folders) — a copy of the manual someone installed, which
    never updates. Through Hermes' public skill-catalog helpers
    (``tools.skills_tool``: ``find_all_skills``, ``skill_search_dirs``,
    ``locate_skill``; Hermes v0.22.5 and later): the copy ``skill_view
    agentchat`` would load, not every file on disk. ``[]`` outside Hermes or on
    an older one. Never raises."""
    try:
        from tools.skills_tool import find_all_skills, locate_skill, skill_search_dirs
    except Exception:
        return []
    from .manual import read_version

    skip: set[Path] = set()
    for path in exclude:
        try:
            skip.add(path.resolve())
        except OSError:
            continue
    found: list[tuple[Path, str | None]] = []
    try:
        names = [
            str(entry["name"])
            for entry in find_all_skills()
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and (entry["name"] == "agentchat" or entry["name"].endswith("/agentchat"))
        ]
        roots, _active = skill_search_dirs()
    except Exception:
        logger.debug("doctor: Hermes' skill catalog could not be read", exc_info=True)
        return []
    for name in names:
        try:
            error, _skill_dir, skill_md = locate_skill(name, None, roots)
            if error is not None or skill_md is None:
                continue
            path = Path(skill_md)
            if path.resolve() in skip:
                continue
            version = read_version(path)
        except Exception:
            continue
        found.append((path, version))
    return found


def _uninstall_secrets_registration() -> None:
    """Remove our ``secrets.agentchat`` block (a hand-written one is left alone)."""
    try:
        from .mcp_registration import McpRegistrationError
        from .secrets_registration import uninstall_block

        removed = uninstall_block()
    except McpRegistrationError as exc:
        sys.stderr.write(f"Warning: could not remove secrets.agentchat from config.yaml ({exc}).\n")
        sys.stderr.flush()
        return
    except Exception as exc:
        sys.stderr.write(f"Warning: Hermes refused the config.yaml write ({exc}).\n")
        sys.stderr.flush()
        return
    if removed:
        _printline("Key source secrets.agentchat removed from config.yaml")


def _uninstall_mcp_registration() -> None:
    """Remove our ``mcp_servers.agentchat`` entry (a hand-managed one is left alone)."""
    try:
        from .mcp_registration import McpRegistrationError, uninstall_entry

        removed = uninstall_entry(api_base=_api_base())
    except McpRegistrationError as exc:
        sys.stderr.write(
            f"Warning: could not remove mcp_servers.agentchat from config.yaml ({exc}).\n"
        )
        sys.stderr.flush()
        return
    except Exception as exc:
        sys.stderr.write(f"Warning: Hermes refused the config.yaml write ({exc}).\n")
        sys.stderr.flush()
        return
    from .profile_scope import resolve_hermes_home

    allow.clear_pending(resolve_hermes_home())  # removed on purpose: nothing to finish
    if removed:
        _printline("Hosted-tools entry mcp_servers.agentchat removed from config.yaml")


def _uninstall_soul_anchor(identity: ProfileIdentity) -> None:
    """Remove the identity block AgentChat placed for this profile's agent from
    SOUL.md; any other block (edited by hand, another agent's, several) is left
    and said. Idempotent."""
    removal = remove_soul_anchor(
        handle=identity.handle, identity_line=identity.identity_line, template=identity.template
    )
    if removal.outcome == "absent":
        return
    if removal.outcome == "failed":
        sys.stderr.write(f"Warning: {removal.detail}.\n")
        sys.stderr.flush()
        return
    _printline(f"SOUL.md: {removal.detail}.")


# ───────────────────────── prompts ─────────────────────────


class _UserAbort(Exception):
    """The person left a prompt blank (or stdin ended): cancel."""


class _MissingFlag(Exception):
    """Raised when a required value was neither passed as a flag nor
    obtainable interactively (stdin is not a TTY). The message names the
    flag to pass."""


def _ask(question: AccountQuestion) -> AccountAnswer:
    """The sign-up helper's ``ask`` on stdin: a blank line (or the end of
    stdin) cancels; ``new`` at the code prompt asks for a new code."""
    if question.field == "new_code":
        reply = _input(f"{signup.NEW_CODE_QUESTION} [y/N]: ").strip().lower()
        return signup.new_code_answer(reply in {"y", "yes"})
    handles = signup.handles_line(question)
    if handles is not None:
        _printline(handles)
    return signup.answer_of(question, _input(f"{signup.prompt_text(question)}: "))


def _ask_recovery_field(field: Literal["email", "handle"]) -> str:
    """Prompt for ``field`` before recovery starts; blank cancels."""
    value = signup.typed_value(field, _input(f"{signup.field_prompt('recover', field)}: "))
    if not value:
        raise _UserAbort("left blank")
    return value


def _resolve_recovery_email(provided: str | None) -> str:
    """``--email``; else prompt on a TTY; else fail naming the flag."""
    if provided and provided.strip():
        return provided.strip()
    if not _stdin_is_tty():
        raise _MissingFlag(
            "No email given and stdin is not a terminal. Re-run with: "
            "hermes agentchat recover --handle <handle> --email <email>"
        )
    return _ask_recovery_field("email")


def _resolve_recovery_handle(provided: str | None) -> str:
    """Which agent to recover.

    ``--handle`` wins (``@`` prefix and case are forgiven, people type
    handles the way they see them). Otherwise the locally configured
    ``AGENTCHATME_HANDLE`` — the operator is told which one and how to
    override it. Otherwise prompt on a TTY. Otherwise fail naming the
    flag: an email can back several agents, so the server cannot pick
    one for us, and a script must not hang on a prompt.
    """
    handle = signup.typed_value("handle", provided or "")
    if handle:
        return handle
    saved = _read_saved_handle()
    if saved:
        _printline(
            f"Recovering the locally configured agent @{saved} "
            "(pass --handle to recover a different one)."
        )
        return saved
    if not _stdin_is_tty():
        raise _MissingFlag(
            "No @handle given and none is saved for this profile. Recovery "
            "needs the agent's handle as well as its email — re-run with: "
            "hermes agentchat recover --handle <handle> --email <email>"
        )
    return _ask_recovery_field("handle")


def _prompt_api_key() -> str:
    return getpass("API key (ac_…): ").strip()


# ───────────────────────── helpers ─────────────────────────


def _read_saved_handle() -> str | None:
    """The handle this profile is configured as, or ``None``.

    The key file holds the handle the server returned at register / login /
    recover; a profile set up before 0.6 has it as ``AGENTCHATME_HANDLE``.
    Its form is the server's to judge: a hand-edited value it does not accept
    is answered with the server's message, and the handle is asked for again.
    """
    from .profile_scope import resolve_hermes_home, resolve_setting

    read = key_store.read_store(resolve_hermes_home())
    stored = read.key.handle if read.usable and read.key is not None else ""
    return signup.typed_value("handle", stored or resolve_setting(_ENV_HANDLE) or "") or None


def _stdin_is_tty() -> bool:
    """Is stdin an interactive terminal? (:func:`.allow.stdin_is_tty`)"""
    return allow.stdin_is_tty()


def _hermes_mcp_client_importable() -> bool:
    """Can Hermes' MCP layer import the ``mcp`` SDK's HTTP client?

    ``tools/mcp_tool.py`` imports ``mcp.client.streamable_http`` lazily when
    it connects an HTTP server; when that import fails the server is parked
    and no ``mcp__agentchat__*`` tool ever exists. Same check, up front.
    """
    try:
        import importlib

        importlib.import_module("mcp.client.streamable_http")
    except Exception:
        return False
    return True


def _signup_problem() -> str | None:
    """What to say when the SDK's sign-up helper cannot be imported, else ``None``."""
    return signup.sdk_problem()


def _api_base() -> str:
    """The API base for this profile (:func:`.config.resolve_api_base`, the one resolver)."""
    from .config import resolve_api_base, stored_api_base

    return resolve_api_base(stored_api_base(), strict=False)


_RESTART_HINT = (
    "Restart the gateway (`hermes gateway restart`) and any open Hermes session so they use the "
    "key: a running Hermes keeps the key it started with for the AgentChat tools."
)

# Where the last successful save put the key, for the success lines.
_last_saved_path: Path | None = None


def _persist_credentials(*, api_key: str, handle: str, api_base: str) -> bool:
    """Save the key in this profile's key file; ``False`` (said on stderr) when it is saved nowhere.

    The key file is written and read back first; only then is any key line
    removed from the profile ``.env``. When the key file cannot be written,
    the key is kept in the profile ``.env`` instead (a key the server just
    issued exists nowhere else) and that is said.
    """
    global _last_saved_path
    ready, why = credentials.key_bridge()
    try:
        outcome = credentials.save_credentials(
            api_key=api_key, handle=handle, api_base=api_base, use_key_file=ready, not_ready=why
        )
    except key_store.KeyStoreError as exc:
        _exit_with(f"The key was not saved: {exc}")
        return False
    _last_saved_path = outcome.path
    if outcome.tools_need_block:
        from .secrets_registration import manual_snippet

        sys.stderr.write(
            f"Warning: {outcome.fallback_reason}. Add this to the profile's config.yaml so the "
            "AgentChat tools can read it:\n" + manual_snippet()
        )
        sys.stderr.flush()
    elif outcome.fallback_reason is not None:
        sys.stderr.write(
            f"Warning: the key was saved in the profile .env as {_ENV_API_KEY}, not in the key "
            f"file: {outcome.fallback_reason}. Fix that, then run `{_profile_cmd()} doctor --fix` "
            "to move it.\n"
        )
        sys.stderr.flush()
    elif outcome.env_lines_removed:
        _printline(
            f"Moved out of the profile .env: {', '.join(outcome.env_lines_removed)} "
            "(the key is now only in the key file)."
        )
    for holder in resolve_key().shared_with:
        sys.stderr.write(f"Warning: {shared_note(holder)}.\n")
        sys.stderr.flush()
    # Only persist the API base when the user overrode the default —
    # leaves the default unchanged for everyone else. It is not a secret.
    if api_base != _DEFAULT_API_BASE:
        try:
            from hermes_cli.config import save_env_value

            save_env_value(_ENV_API_BASE, api_base)
        except ImportError:
            logger.debug("hermes_cli.config not importable; AGENTCHATME_API_BASE not saved")
    return True


def _saved_where() -> str:
    if _last_saved_path is None:
        return f"saved in the profile .env as {_ENV_API_KEY}"
    return f"saved in {_last_saved_path}, readable by you only"


def _print_registration_success(*, handle: str, api_key: str) -> None:
    masked = _mask_key(api_key)
    _printline("")
    _printline(f"  Registered @{handle}")
    _printline(f"  Key:   {masked}   ({_saved_where()})")
    _printline("")
    _printline(f"{_RESTART_HINT} Then your agent is on the network.")


def _print_recovery_success(*, handle: str, api_key: str) -> None:
    masked = _mask_key(api_key)
    _printline("")
    _printline(f"  Recovered @{handle}")
    _printline(f"  New key:   {masked}   ({_saved_where()})")
    _printline("")
    _printline(_RESTART_HINT)


def _could_not_process_section(worker: Any) -> dict[str, Any] | None:
    """The health record's ``worker.loop.could_not_process`` (the kit's
    counts and the paused conversations), or ``None`` when the record has
    none (written by a release before could not process)."""
    if not isinstance(worker, dict):
        return None
    loop = worker.get("loop")
    section = loop.get("could_not_process") if isinstance(loop, dict) else None
    return section if isinstance(section, dict) else None


def _count(section: dict[str, Any], name: str) -> int:
    value = section.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _could_not_process_summary(section: dict[str, Any]) -> str:
    """One line of the kit's could-not-process counts since the receive loop
    started: ``2 reported (retry 1, hidden 1), 1 re-run, runtime checks 1 ok``;
    ``nothing reported`` when every count is zero."""
    parts: list[str] = []
    actions_raw = section.get("failure_actions")
    actions = actions_raw if isinstance(actions_raw, dict) else {}
    answered = ", ".join(
        f"{name} {actions[name]}"
        for name in ("retry", "hidden", "paused", "none")
        if isinstance(actions.get(name), int) and actions[name] > 0
    )
    reported = _count(section, "failures_reported")
    if reported:
        parts.append(f"{reported} reported" + (f" ({answered})" if answered else ""))
    errors = _count(section, "failure_report_errors")
    if errors:
        parts.append(f"{errors} report error{'s' if errors != 1 else ''}")
    reruns = _count(section, "reruns")
    if reruns:
        parts.append(f"{reruns} re-run{'s' if reruns != 1 else ''}")
    waiting = _count(section, "failures_waiting")
    if waiting:
        parts.append(f"{waiting} waiting for evidence")
    checks_ok, checks_failed = _count(section, "runtime_checks_ok"), _count(section, "runtime_checks_failed")
    if checks_ok or checks_failed:
        parts.append(
            "runtime checks " + ", ".join(
                text for n, text in ((checks_ok, f"{checks_ok} ok"), (checks_failed, f"{checks_failed} failed")) if n
            )
        )
    stopped = _count(section, "splits_stopped")
    if stopped:
        parts.append(f"gate split stopped {stopped} time{'s' if stopped != 1 else ''}")
    return ", ".join(parts) if parts else "nothing reported"


def _paused_lines(section: dict[str, Any], *, now: float) -> list[str]:
    """One line per conversation the server paused for this agent whose
    pause has not ended."""
    from .ws_health import pause_end

    paused = section.get("paused")
    lines: list[str] = []
    for entry in paused if isinstance(paused, list) else []:
        if not isinstance(entry, dict):
            continue
        conversation_id, until = entry.get("conversation_id"), entry.get("until")
        if not isinstance(conversation_id, str) or not isinstance(until, str):
            continue
        end = pause_end(until)
        if end is not None and end <= now:
            continue
        lines.append(f"{conversation_id} paused until {until} (could not process; the mute ends on its own)")
    return lines


# A failed report or a stopped split this recent makes doctor's counts line a
# warning; older ones are history (the counts still show them).
_RECENT_TROUBLE_S = 3600.0


def _describe_could_not_process(worker: Any, *, now: float) -> list[tuple[str, str]]:
    """``(ok|warn, line)`` pairs for doctor: the kit's counts once anything
    was reported or checked (a warning when, within the last hour, a report
    failed or the gate's split stopped, or when the last runtime check
    failed), and a warning per paused conversation. Nothing when the record
    has no such section."""
    section = _could_not_process_section(worker)
    if section is None:
        return []
    out: list[tuple[str, str]] = []
    summary = _could_not_process_summary(section)
    last_check = section.get("last_runtime_check")
    check_failed = isinstance(last_check, dict) and last_check.get("ok") is False
    if check_failed and isinstance(last_check, dict):
        detail = last_check.get("detail")
        summary += f"; the last runtime check failed{f' ({detail})' if detail else ''}"

    def recent(name: str) -> bool:
        at = section.get(name)
        return isinstance(at, (int, float)) and not isinstance(at, bool) and now - float(at) <= _RECENT_TROUBLE_S

    if summary != "nothing reported":
        attention = check_failed or recent("last_report_error_at") or recent("last_split_stopped_at")
        out.append(("warn" if attention else "ok", f"Could not process (since the receive loop started): {summary}"))
    for line in _paused_lines(section, now=now):
        out.append(("warn", f"Conversation {line}"))
    return out


def _health_worker(api_base: str, api_key: str) -> Any:
    """The health record's ``worker`` section for this profile's account
    (what the running gateway last wrote), or ``None`` (see
    :func:`_live_health`). Never raises: status prints what it can."""
    health = _live_health(api_base, api_key)
    return health.get("worker") if health is not None else None


def _live_health(api_base: str, api_key: str, hermes_home: Path | None = None) -> Any:
    """This account's health record while its gateway lives (:func:`.ws_health.live_health`)."""
    from .ws_health import live_health

    return live_health(api_base, api_key, hermes_home)


def _tools_line(hermes_home: Path, *, api_base: str, handle: str | None) -> str:
    """``status``'s one "AgentChat tools:" line (:func:`.allow.read_switch`)."""
    return allow.describe(allow.read_switch(hermes_home, api_base=api_base), handle, cmd=cli_prefix(hermes_home))


def _print_local_status(
    resolution: KeyResolution, *, worker: Any, manual: str, identity: str | None = None, tools: str | None = None
) -> None:
    """``status`` when AgentChat did not answer: what this machine holds."""
    saved = _read_saved_handle()
    _printline("")
    _printline(f"  Handle:        {f'@{saved}' if saved else 'unknown'} (saved here; AgentChat did not answer)")
    _printline(f"  Plugin:        agentchatme-hermes {__version__} (integration)")
    _printline(f"  Key:           {_key_location_line(resolution)}")
    for note in (*resolution.notes, *(shared_note(h) for h in resolution.shared_with)):
        _printline(f"  Note:          {note}")
    _printline(f"  Manual:        {manual}")
    if identity is not None:
        _printline(f"  Identity:      {identity}")
    if tools is not None:
        _printline(f"  AgentChat tools: {tools}")
    section = _could_not_process_section(worker)
    if section is not None:
        _printline(f"  Could not process: {_could_not_process_summary(section)}")
    _printline("")


def _print_status(
    me: dict[str, Any],
    resolution: KeyResolution | None = None,
    *,
    worker: Any = None,
    manual: str | None = None,
    identity: str | None = None,
    tools: str | None = None,
) -> None:
    handle = me.get("handle", "unknown")
    status = me.get("status", "unknown")
    settings = me.get("settings") or {}
    inbox_mode = settings.get("inbox_mode", "unknown") if isinstance(settings, dict) else "unknown"
    display_name = me.get("display_name") or ""
    paused_by_owner = me.get("paused_by_owner") or "none"

    _printline("")
    _printline(f"  Handle:        @{handle}")
    if display_name:
        _printline(f"  Display name:  {display_name}")
    _printline(f"  Status:        {status}")
    _printline(f"  Inbox mode:    {inbox_mode}")
    if paused_by_owner != "none":
        _printline(f"  Paused:        {paused_by_owner}  (owner-paused)")
    _printline(f"  Plugin:        agentchatme-hermes {__version__} (integration)")
    if resolution is not None:
        _printline(f"  Key:           {_key_location_line(resolution)}")
        for note in (*resolution.notes, *(shared_note(h) for h in resolution.shared_with)):
            _printline(f"  Note:          {note}")
    if manual is not None:
        _printline(f"  Manual:        {manual}")
    if identity is not None:
        _printline(f"  Identity:      {identity}")
    if tools is not None:
        _printline(f"  AgentChat tools: {tools}")
    # Could not process, from the running gateway's health record (absent
    # when no gateway receives for this account, or from an older release).
    section = _could_not_process_section(worker)
    if section is not None:
        _printline(f"  Could not process: {_could_not_process_summary(section)}")
        for line in _paused_lines(section, now=time.time()):
            _printline(f"  Paused chat:   {line}")
    _printline("")


# ───────────────────────── I/O glue ─────────────────────────


_MANIFEST_NAME = re.compile(r"^name:\s*['\"]?([^'\"#\s]+)['\"]?\s*(?:#.*)?$", re.MULTILINE)


def _agentchat_directories(plugins_dir: Path) -> list[Path]:
    """The directory plugins named ``agentchat`` under ``plugins_dir``, sorted
    the way Hermes scans them. Never raises."""
    directories: list[Path] = []
    try:
        candidates = sorted(path for path in plugins_dir.iterdir() if path.is_dir())
    except OSError:
        candidates = []
    for candidate in candidates:
        manifest_file = next(
            (f for f in (candidate / "plugin.yaml", candidate / "plugin.yml") if f.is_file()), None
        )
        if manifest_file is None:
            continue
        try:
            manifest = manifest_file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        named = _MANIFEST_NAME.search(manifest)
        if (named[1] if named else candidate.name) == "agentchat":
            directories.append(candidate)
    return directories


def _profiles_with_a_git_copy(hermes_home: Path) -> list[Path]:
    """Every profile home on this machine (the default and each under
    ``profiles/``) with its own ``agentchat`` directory plugin. Never raises."""
    from .profile_scope import PROFILES_DIRNAME, hermes_root_of

    root = hermes_root_of(hermes_home)
    homes = [root]
    with contextlib.suppress(OSError):
        homes += sorted(path for path in (root / PROFILES_DIRNAME).iterdir() if path.is_dir())
    return [home for home in homes if _agentchat_directories(home / "plugins")]


def _git_copies_line(hermes_home: Path) -> str | None:
    """T5-F07: two profiles each with a git install of this plugin. Hermes
    installs one plugin package for every profile (its plugin venv is a uv
    workspace), so the second install fails to resolve and leaves that copy
    disabled, and every copy blocks the next install or update. ``None`` when
    at most one profile has a copy."""
    from .profile_scope import profile_label

    homes = _profiles_with_a_git_copy(hermes_home)
    if len(homes) < 2:
        return None
    # The default profile's copy is the first install on most machines; else this profile's.
    keep = next(
        (h for h in homes if profile_label(h) == "default"),
        next((h for h in homes if key_store.same_home(h, hermes_home)), homes[0]),
    )
    others = [h for h in homes if h is not keep]

    def steps(home: Path) -> str:
        label = profile_label(home)
        flag = "" if label == "default" else f" -p {label}"
        return f"`hermes{flag} plugins remove agentchat`, then `hermes{flag} plugins enable agentchat`"

    return (
        f"agentchat is installed from git in {len(homes)} profiles "
        f"({', '.join(profile_label(h) for h in homes)}). Hermes installs one plugin package for every "
        "profile, so a second `plugins install` fails (\"Two workspace members are both named "
        "`agentchatme-hermes`\", yet exits 0), leaves that copy disabled, and blocks the next install or "
        f"update. Keep the copy in {profile_label(keep)}; in "
        + "; in ".join(f"{profile_label(h)}: {steps(h)}" for h in others)
        + " (enable uses the package already installed)"
    )


def _plugin_copies(hermes_home: Path) -> tuple[list[Path], str | None]:
    """The ``agentchat`` plugins Hermes can find for this profile.

    Returns the directory plugins named ``agentchat`` under the profile's
    ``plugins/`` (a ``hermes plugins install`` git clone, or a copy left
    there), sorted the way Hermes scans them (the last one is the one it
    loads), and the version of the pip distribution when its ``agentchat``
    entry point is installed from a separate copy (else ``None``). A
    directory's name is its manifest's ``name:`` (``plugin.yaml`` or
    ``plugin.yml``), or the directory name when the manifest has none, as in
    Hermes. Hermes keeps a directory plugin and drops an entry point with the
    same name, so with both present the pip package never runs. An entry
    point installed editable from one of these directories is that directory,
    not a second copy (``_is_editable_copy_of``). Never raises.
    """
    directories = _agentchat_directories(hermes_home / "plugins")

    from importlib import metadata

    try:
        found: Any = metadata.entry_points()
        select = getattr(found, "select", None)
        group = select(group="hermes_agent.plugins") if callable(select) else found.get("hermes_agent.plugins", ())
        separate = [
            ep
            for ep in group
            if getattr(ep, "name", None) == "agentchat" and not _is_editable_copy_of(ep, directories)
        ]
    except Exception:
        separate = []
    if not separate:
        return directories, None
    try:
        return directories, metadata.version("agentchatme-hermes")
    except metadata.PackageNotFoundError:
        return directories, "(version unknown)"


def _is_editable_copy_of(entry_point: Any, directories: list[Path]) -> bool:
    """Whether the entry point's distribution is an editable install of one of
    ``directories``. A catalog or git install (``hermes plugins install``) also
    installs this package into Hermes' environment, editable, from a snapshot
    Hermes' package manager takes of the directory
    (``<generation>/workspace/plugin-sources/<_hermes_member_key(directory)>``);
    an editable install of the directory itself counts too. Read from the
    distribution's ``direct_url.json`` (PEP 610). ``False`` when that cannot be
    read, so a separate install (PyPI, another path) still counts as a second
    copy. Never raises."""
    import json
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    try:
        direct_url = json.loads(entry_point.dist.read_text("direct_url.json") or "{}")
        if not direct_url.get("dir_info", {}).get("editable"):
            return False
        url = urlparse(direct_url["url"])
        if url.scheme != "file":
            return False
        source = Path(url2pathname(url.path)).resolve()
        return any(
            source == directory.resolve() or source.name == _hermes_member_key(directory)
            for directory in directories
        )
    except Exception:
        return False


def _hermes_member_key(directory: Path) -> str:
    """The name Hermes' package manager gives the snapshot of a plugin
    directory (``pm/workspace.py`` ``_member_key``; v0.23.2 and ``main``): the
    directory name lowercased, runs of other characters as ``-``, then the
    first 16 hex digits of the sha256 of its resolved path. The contract suite
    checks it against Hermes'."""
    import hashlib

    resolved = directory.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    name = re.sub(r"[^a-z0-9._-]+", "-", resolved.name.lower()).strip("-.") or "plugin"
    return f"{name}-{digest}"


_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def _catalog_install_line(directory: Path, hermes_home: Path) -> str | None:
    """How doctor names a directory plugin Hermes installed from its plugin
    catalog (``hermes plugins install agentchat``), or ``None`` for any other
    install. Read, as Hermes reads it, from the installer-owned record
    (``plugins/.install-metadata.json``, keyed by the directory's name): its
    ``catalog`` block (``name``; ``sha``, the commit checked out; ``pin``,
    empty when installed with ``--ref`` off the catalog's pin), or the
    top-level ``catalog_name`` and ``revision`` of earlier releases. Never
    from the tree, which the repo controls. ``hermes plugins update`` re-pins
    a catalog install to the catalog's current pin. Never raises."""
    import json

    from .profile_scope import profile_label

    try:
        record = json.loads((directory.parent / ".install-metadata.json").read_text(encoding="utf-8-sig"))[
            directory.name
        ]
        block = record.get("catalog")
        if isinstance(block, dict):
            name, sha, off_pin = block.get("name"), block.get("sha"), block.get("pin") == ""
        else:
            name, sha, off_pin = record.get("catalog_name"), record.get("revision"), False
    except Exception:
        return None
    if not isinstance(name, str) or not name:
        return None
    sha = sha.lower() if isinstance(sha, str) else ""
    label = profile_label(hermes_home)
    flag = "" if label == "default" else f" -p {label}"
    return (
        "from the Hermes plugin catalog"
        + (f" at {sha[:8]}" if _COMMIT_SHA.fullmatch(sha) else "")
        + (", installed with --ref off the catalog's pin" if off_pin else "")
        + f", not a PyPI release; `hermes{flag} plugins update {name}` moves it to the catalog's current pin"
    )


def _profile_cmd() -> str:
    """``hermes agentchat`` spelled for the current profile (``hermes -p NAME agentchat``)."""
    from .profile_scope import resolve_hermes_home

    return cli_prefix(resolve_hermes_home())


def _printline(message: str) -> None:
    """Send to stdout. Hermes captures stdout for the CLI surface."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _input(prompt: str) -> str:
    """One line from stdin; ``""`` at its end, which reads as a blank line (cancel)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")


def _exit_with(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()
    return _EXIT_API_ERR


def _exit_usage(message: str) -> int:
    """Bad or missing arguments — exit 2, argparse's own usage-error code,
    so scripts can tell "fix the invocation" from "the server said no"."""
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()
    return _EXIT_ARG_ERR


def _exit_cancel() -> int:
    sys.stderr.write("\nCancelled.\n")
    sys.stderr.flush()
    return _EXIT_USER_CANCEL


# ───────────────────────── Hermes plugin entry ─────────────────────────


def register_cli(ctx: Any) -> None:
    """Wire ``hermes agentchat …`` into Hermes' top-level CLI.

    Per ``PluginContext.register_cli_command`` (``hermes_cli/plugins_context.py``) the ``setup_fn`` receives a
    pre-built argparse subparser scoped under our subcommand and we
    attach our subtree there. ``handler_fn`` is set via per-action
    ``set_defaults(func=...)`` so this top-level handler only fires
    when no subcommand is given — it routes to the wizard.
    """
    ctx.register_cli_command(
        name="agentchat",
        help="Manage your AgentChat identity (register, login, recover, allow, status, logout, doctor)",
        setup_fn=_setup_with_signature(setup_argparse),
        handler_fn=_dispatch_top,
        description=(
            "Manage your AgentChat identity. Run with no subcommand for "
            "the interactive register/status wizard."
        ),
    )


def _dispatch_top(args: argparse.Namespace) -> int:
    """Top-level dispatcher when argparse sees no subcommand.

    Argparse's ``set_defaults(func=...)`` calls this with the parsed
    namespace; we fall through to whichever action's func was set, or
    the wizard if nothing matched.
    """
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is _dispatch_top or func is None:
        return _dispatch_wizard(args)
    return func(args)


def _setup_with_signature(setup_fn: Callable[[argparse.ArgumentParser], None]) -> Callable[[argparse.ArgumentParser], None]:
    """Identity wrapper — placeholder for future shimming.

    Lets us interpose on the argparse wiring (e.g., to inject global
    flags, attach an environment-version banner) without rewriting
    callsites in the future. Right now it's a pass-through.
    """
    return setup_fn
