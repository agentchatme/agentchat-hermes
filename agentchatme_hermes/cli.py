"""`hermes agentchat [...]` CLI subcommand.

Surfaces:

* **``hermes agentchat``** (no subcommand) — registers a fresh
  agent via the interactive wizard if no key is configured, or
  shows status if one is.
* **``register``** — OTP registration: email → handle → 6-digit
  code → minted API key persisted to ``~/.hermes/.env``.
* **``login``** — paste an existing ``ac_…`` key. Validates
  via ``GET /v1/agents/me`` before persisting so we never save a
  key that doesn't authenticate.
* **``recover``** — re-key a lost or leaked API key: email + @handle
  → 6-digit code → a fresh key persisted to ``~/.hermes/.env`` (the
  old key dies). The handle is required because an email can back
  several agents; it defaults to the locally configured one.
* **``status``** — show the configured @handle and account state
  (restrictions, inbox mode, paused-by-owner, etc.).
* **``logout``** — wipe the saved key from ``~/.hermes/.env``, strip the
  identity line from SOUL.md, remove our ``mcp_servers.agentchat`` entry.
* **``doctor``** — check every placement (key, identity line, manual,
  hosted tools) plus the lock and the daemon's health; ``--fix`` re-places.

Every successful register / login / recover *places* the integration: the
key in the profile ``.env`` as ``AGENTCHAT_API_KEY``, the served identity
line in SOUL.md, and the hosted MCP server in ``config.yaml`` — see
:mod:`.placement`.

The wizard is the only path users need. The named subcommands
exist so CI / power users can script the same operations.

Persistence: every successful flow writes through Hermes's
``save_env_value`` (``hermes_cli/config.py``), which mirrors the
gateway and auth flows so users get one well-known location for
their secrets.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ._version import __version__
from .client_identity import hermes_client_identity
from .config import ENV_API_BASE, ENV_API_KEY, ENV_API_KEY_LEGACY, ENV_HANDLE
from .placement import (
    PlacementStep,
    clear_api_key,
    persist_api_key,
    place_identity_and_tools,
    resolve_identity_line,
)
from .soul_anchor import remove_soul_anchor
from .wizard import _mask_key

if TYPE_CHECKING:
    import argparse

logger = logging.getLogger(__name__)

# Server-side regex (project_agentchat_handle_rules). Mirrored client-side
# so the user gets immediate feedback on bad input instead of an opaque
# 400 from the server.
_HANDLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Env var keys we persist (canonical names live in config.py). The key is
# ``AGENTCHAT_API_KEY`` — the name the manual and the hosted-MCP header
# use; ``AGENTCHATME_API_KEY`` is read as a fallback and kept in step while
# it exists.
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
        "are scriptable equivalents. Configuration is persisted to "
        "~/.hermes/.env."
    )
    parser.set_defaults(func=_dispatch_wizard)

    sub = parser.add_subparsers(
        dest="action",
        metavar="<action>",
        help="Action to run. Omit to launch the interactive wizard.",
    )

    p_register = sub.add_parser(
        "register",
        help="Register a new AgentChat agent (email + OTP)",
    )
    p_register.add_argument("--email", help="Email address for OTP verification.")
    p_register.add_argument(
        "--handle",
        help="Desired @handle (3-30 chars, lowercase letters/digits/hyphens, must start with a letter).",
    )
    p_register.add_argument(
        "--display-name",
        dest="display_name",
        help="Optional display name shown next to your @handle.",
    )
    p_register.set_defaults(func=_dispatch_register)

    p_login = sub.add_parser(
        "login",
        help="Paste an existing AgentChat API key",
    )
    p_login.add_argument(
        "--api-key",
        dest="api_key",
        help="ac_… key. Prompts (masked input) if omitted.",
    )
    p_login.set_defaults(func=_dispatch_login)

    p_recover = sub.add_parser(
        "recover",
        help="Recover a lost or leaked API key (email + @handle + OTP; rotates the key)",
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

    p_status = sub.add_parser(
        "status",
        help="Show the currently configured @handle and account state",
    )
    p_status.set_defaults(func=_dispatch_status)

    p_logout = sub.add_parser(
        "logout",
        help="Clear the saved AgentChat key from ~/.hermes/.env",
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
            "Re-place what is missing or outdated: the identity line in SOUL.md and the "
            "hosted-tools entry in config.yaml (never touches a hand-managed entry)."
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
    try:
        email = _prompt_email(getattr(args, "email", None))
        handle = _prompt_handle(getattr(args, "handle", None))
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    api_base = _api_base()

    try:
        from agentchatme import AgentChatClient, AgentChatError
        from agentchatme import ConnectionError as SDKConnectionError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    from .wizard import _EMAIL_POLICY_CODES, _register_start, _RegisterError

    display_name = getattr(args, "display_name", None) or ""

    _printline(f"Sending verification code to {email}…")
    # Route through wizard._register_start so both entry points share the
    # same SDK call and the same error texts; the wizard adds field-scoped
    # retry logic on top, which the named subcommand doesn't need (the
    # operator either succeeds or runs the command again).
    try:
        pending_id = _register_start(
            email=email, handle=handle, display_name=display_name
        )
    except _RegisterError as exc:
        lines = [f"Registration request failed: {exc}"]
        if exc.code in _EMAIL_POLICY_CODES:
            # The wizard offers this as a menu choice; the scriptable
            # path gets the equivalent hint. The common mistake behind a
            # per-email rejection is registering again to replace a
            # lost key — which burns a lifetime slot instead of
            # re-keying the agent that already exists.
            lines.append(
                "Trying to replace the key of an agent already on this email? "
                "Re-key it instead: hermes agentchat recover --handle <handle> "
                f"--email {email}"
            )
        return _exit_with("\n".join(lines))

    _printline("Check your email — a 6-digit code will arrive shortly.")
    try:
        code = _prompt_code()
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    try:
        _agent, api_key, auth_client = AgentChatClient.verify(
            pending_id,
            code,
            base_url=api_base,
            client_identity=hermes_client_identity(),
        )
    except (AgentChatError, SDKConnectionError) as exc:
        # A connection error too: the code is spent, so say so instead of a traceback.
        return _exit_with(f"Verification failed: {exc}")

    if not isinstance(api_key, str) or not api_key:
        return _exit_with(
            "Verification server response did not include an api_key"
        )

    # verify() hands back an authenticated AgentChatClient we never
    # use — close it so we don't leak the underlying httpx connection
    # pool. The plugin's long-lived clients are constructed lazily
    # inside the runtime once register persists the key.
    try:
        auth_client.close()
    except Exception:
        logger.debug("auth_client close raised", exc_info=True)

    _persist_credentials(api_key=api_key, handle=handle, api_base=api_base)
    _install_placement(handle, api_base=api_base, api_key=api_key)
    _print_registration_success(handle=handle, api_key=api_key)
    return _EXIT_OK


def _dispatch_login(args: argparse.Namespace) -> int:
    raw_key = getattr(args, "api_key", None)
    if not raw_key:
        try:
            raw_key = _prompt_api_key()
        except KeyboardInterrupt:
            return _exit_cancel()
        except _UserAbort:
            return _EXIT_USER_CANCEL

    api_key = raw_key.strip()
    if not api_key:
        return _exit_with("No API key provided.")

    api_base = _api_base()

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
        from agentchatme import ConnectionError as SDKConnectionError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    client = AgentChatClient(
        api_key=api_key,
        base_url=api_base,
        client_identity=hermes_client_identity(),
    )
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            return _exit_with(
                "That key did not authenticate against /v1/agents/me. "
                "Check that you copied the full ac_… key."
            )
        except (AgentChatError, SDKConnectionError) as exc:
            return _exit_with(f"Key validation failed: {exc}")
    finally:
        client.close()

    handle = me.get("handle") if isinstance(me, dict) else None
    if not isinstance(handle, str):
        return _exit_with(
            "Server response missing handle — refusing to persist a key we "
            "can't identify."
        )

    _persist_credentials(api_key=api_key, handle=handle, api_base=api_base)
    _install_placement(handle, api_base=api_base, api_key=api_key)
    _printline(f"Saved AgentChat key for @{handle}.")
    return _EXIT_OK


def _dispatch_recover(args: argparse.Namespace) -> int:
    """``hermes agentchat recover [--handle H] [--email E]`` — re-key a
    lost or leaked API key.

    Recovery needs the @handle as well as the email because an email can
    back several agents. When ``--handle`` is omitted the handle comes
    from, in order: the locally configured ``AGENTCHATME_HANDLE`` (the
    "rotate my own key" case, by far the most common), an interactive
    prompt when stdin is a TTY, otherwise a hard failure that names the
    flag — a script must fail fast, not hang on a prompt nobody will
    answer. ``--email`` follows the same TTY rule (there is no stored
    email to default to).

    The first server response is deliberately the same whether or not
    the pair exists (no email-existence oracle); a wrong pair surfaces
    as ``INVALID_CODE`` at verify, exactly like a mistyped code.
    """
    try:
        email = _resolve_recovery_email(getattr(args, "email", None))
        handle = _resolve_recovery_handle(getattr(args, "handle", None))
    except (_MissingFlag, ValueError) as exc:
        # ValueError: a --handle / --email flag that failed validation.
        return _exit_usage(str(exc))
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    api_base = _api_base()

    # The verify step goes through the SDK; check it is importable before
    # spending the operator's OTP on a request we could not complete.
    if not _sdk_importable():
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    from .wizard import (
        _HANDLE_REQUIRED,
        _handle_required_lines,
        _recover_start,
        _recover_verify,
        _RecoverError,
    )

    _printline(f"Requesting a recovery code for @{handle} ({email})…")
    try:
        pending_id = _recover_start(email=email, handle=handle)
    except _RecoverError as exc:
        return _exit_with(f"Recovery request failed: {exc}")

    _printline(
        "If that @handle is registered under that email, a 6-digit code is on "
        "its way (valid ~10 minutes). Check your inbox (and spam)."
    )
    _printline(
        "Completing recovery rotates the API key — anything still using the "
        "old key stops working."
    )
    try:
        code = _prompt_code()
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    try:
        api_key, resolved_handle = _recover_verify(pending_id=pending_id, code=code)
    except _RecoverError as exc:
        if exc.code == _HANDLE_REQUIRED:
            lines = _handle_required_lines(exc)
            lines.append(
                "Re-run with: hermes agentchat recover --handle <handle> "
                f"--email {email}"
            )
            return _exit_with("\n".join(lines))
        return _exit_with(f"Recovery failed: {exc}")

    _persist_credentials(api_key=api_key, handle=resolved_handle, api_base=api_base)
    _install_placement(resolved_handle, api_base=api_base, api_key=api_key)
    _print_recovery_success(handle=resolved_handle, api_key=api_key)
    return _EXIT_OK


def _dispatch_status(_args: argparse.Namespace) -> int:
    saved_key = _read_saved_key()
    if not saved_key:
        _printline(
            "No AgentChat key configured. Run `hermes agentchat register` "
            "or `hermes agentchat login`."
        )
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
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            return _exit_with(
                "Saved key no longer authenticates. Run "
                "`hermes agentchat login` with a fresh key."
            )
        except (AgentChatError, SDKConnectionError) as exc:
            return _exit_with(f"Status fetch failed: {exc}")
    finally:
        client.close()

    _print_status(me)
    return _EXIT_OK


def _dispatch_doctor(args: argparse.Namespace) -> int:
    """Health-check the integration end-to-end.

    Surfaces in a single checklist:

    * the key (which variable name holds it), handle hint, api base
    * SDK importability
    * API reachability + auth (``GET /v1/agents/me``)
    * Account-side flags that would silently suppress live delivery
      (paused-by-owner, inbox mode = strict, etc.)
    * the four placements: identity line in SOUL.md (present, current,
      matching the key), the bundled manual, the hosted-tools entry in
      ``config.yaml``, the key under the name that entry references
    * Gateway process detection (informational only — the doctor
      itself is a CLI process, so we can only check whether *another*
      process matching ``hermes gateway`` is running)
    * WS leader ownership plus the daemon's heartbeat record (the lock
      alone cannot distinguish a healthy receiver from a dead thread)

    ``--fix`` re-places the identity line and the hosted-tools entry
    before the checks run (a hand-managed entry is never touched).

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
        clone = (
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

    from .config import ENV_INTEGRATION, ENV_INTEGRATION_LEGACY, INTEGRATION_OFF

    for env_name in (ENV_INTEGRATION, ENV_INTEGRATION_LEGACY):
        if (resolve_setting(env_name) or "").strip().lower() == INTEGRATION_OFF:
            warn(
                f"{env_name}=off — this profile holds no socket and is never woken; "
                "the hosted tools and the manual still work when the agent is run"
            )
            break

    saved = _read_saved_key_source()
    saved_key = saved[0] if saved else None
    if saved is not None:
        key, source = saved
        if source == _ENV_API_KEY:
            ok(f"{_ENV_API_KEY} set ({_mask_key(key)})")
        else:
            warn(
                f"key found only under the pre-v3 name {source} ({_mask_key(key)}) — the "
                f"hosted tools authenticate with ${{{_ENV_API_KEY}}}; the gateway mirrors "
                f"it at activation, or run `hermes agentchat login` to write {_ENV_API_KEY}"
            )
        legacy = (resolve_setting(_ENV_API_KEY_LEGACY) or "").strip()
        canonical = (resolve_setting(_ENV_API_KEY) or "").strip()
        if legacy and canonical and legacy != canonical:
            fail(
                f"{_ENV_API_KEY} and {_ENV_API_KEY_LEGACY} hold different keys — the plugin "
                f"uses {_ENV_API_KEY}; remove or fix the other"
            )
    else:
        fail(
            f"{_ENV_API_KEY} not set — run "
            "`hermes agentchat register` or `hermes agentchat login`"
        )

    api_base = _api_base()
    if api_base == _DEFAULT_API_BASE:
        ok(f"API base: {api_base} (default)")
    else:
        ok(f"API base: {api_base} (overridden via AGENTCHATME_API_BASE)")

    saved_handle = (resolve_setting(_ENV_HANDLE) or "").strip()
    if saved_handle:
        ok(f"Local handle hint: @{saved_handle}")
    else:
        warn(
            "AGENTCHATME_HANDLE not set (informational — the runtime "
            "resolves the handle from /v1/agents/me at startup)"
        )

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
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            fail(
                "API key did NOT authenticate against /v1/agents/me — "
                "rotate via `hermes agentchat login`"
            )
            return _doctor_finalize(failures, warnings_count)
        except (AgentChatError, SDKConnectionError) as exc:
            # Offline is what doctor must report, not a traceback.
            fail(f"Could not reach /v1/agents/me: {exc}")
            return _doctor_finalize(failures, warnings_count)

        handle = me.get("handle") if isinstance(me, dict) else None
        if isinstance(handle, str) and handle:
            authenticated_handle = handle.lstrip("@").lower()
            ok(f"Authenticated as @{authenticated_handle}")
        else:
            fail("Server response missing handle — refusing to trust")
            return _doctor_finalize(failures, warnings_count)

        # The served texts: the identity line the anchor check compares
        # against, and proof that /v1/texts answers for this key.
        identity_line, identity_template = _doctor_identity_texts(
            client, api_key=saved_key, api_base=api_base, hermes_home=hermes_home
        )
        if identity_line is not None:
            ok("served texts available (/v1/texts, or the cached copy)")
        else:
            warn(
                "/v1/texts unreachable and nothing cached — the identity line cannot be "
                "placed or verified until the server answers"
            )

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

        paused = (
            me.get("paused_by_owner") if isinstance(me, dict) else None
        )
        if paused and paused != "none":
            warn(
                f"Account paused by owner ({paused}) — agent will "
                "not receive new inbound until un-paused"
            )

        # NOTE: deliberately do NOT call /v1/messages/sync from the
        # doctor. The gateway drains sync on every (re)connect, and a
        # doctor invocation while the gateway is up would race the
        # drain — the SDK has no peek/count variant, only "pull and
        # ack." We instead rely on the receiver heartbeat in the
        # gateway log for queue-depth visibility.
    finally:
        client.close()

    if getattr(args, "fix", False) and authenticated_handle is not None:
        for step in place_identity_and_tools(
            handle=authenticated_handle, identity_line=identity_line, api_base=api_base
        ):
            (fail if step.failed else ok)(f"fix: {step.name} {step.status} — {step.detail}")

    try:
        from .soul_anchor import (
            anchor_is_current,
            has_anchor,
            read_anchor_handle,
            resolve_soul_path,
        )

        if has_anchor():
            anchored = read_anchor_handle(template=identity_template)
            if (
                anchored is not None
                and authenticated_handle is not None
                and anchored != authenticated_handle
            ):
                fail(
                    f"IDENTITY MISMATCH: {resolve_soul_path()} says @{anchored} "
                    f"but the configured key authenticates as "
                    f"@{authenticated_handle}. This profile holds another "
                    "agent's key or another agent's SOUL.md — run "
                    "`hermes agentchat login` here with the right key, or "
                    "re-register."
                )
            elif anchored is not None and identity_line is None:
                warn(
                    f"SOUL.md identity line present (@{anchored}) — cannot verify its text "
                    "while /v1/texts is unreachable"
                )
            elif (
                anchored is not None
                and identity_line is not None
                and anchor_is_current(identity_line, template=identity_template) is False
            ):
                warn(
                    "SOUL.md identity line differs from the served text — run "
                    "`hermes agentchat doctor --fix` to place the served line"
                )
            elif anchored is None:
                warn(
                    "SOUL.md agentchat block was hand-edited and names no handle this plugin "
                    "recognises — it is yours, left alone; the served identity line is at "
                    "https://agentchat.me/skill.md (Be reachable)"
                )
            else:
                ok(f"SOUL.md identity line present (@{anchored}, served text)")
        else:
            warn(
                "SOUL.md identity line missing — run "
                "`hermes agentchat doctor --fix` (or `hermes agentchat login`) to place it"
            )
    except ImportError:
        warn("soul_anchor module not importable — skipping anchor check")

    from .placement import BUNDLED_SKILL_PATH

    if BUNDLED_SKILL_PATH.exists():
        ok("manual bundled as the agentchat:agentchat skill (https://agentchat.me/skill.md)")
    else:
        fail(f"bundled manual missing at {BUNDLED_SKILL_PATH} — reinstall agentchatme-hermes")

    try:
        from .mcp_registration import (
            McpRegistrationError,
            client_header_lines,
            hosted_mcp_url,
            inspect_entry_identity,
            manual_snippet,
        )

        try:
            entry_state, identity = inspect_entry_identity(api_base=api_base)
        except McpRegistrationError as exc:
            warn(f"hosted-tools entry not checked — {exc}")
        else:
            set_headers = (
                "set these two headers under its `headers:` (replace any existing "
                "X-AgentChat-Client lines; a key may appear only once, in any casing), then "
                "restart the gateway or /reload-mcp:\n" + client_header_lines("      ")
            )
            if entry_state == "ours":
                ok(
                    f"hosted tools registered: mcp_servers.agentchat → {hosted_mcp_url(api_base)} "
                    "(tools appear as mcp__agentchat__<tool>; `hermes mcp test agentchat` probes it)"
                )
                if identity == "missing":
                    warn(
                        "mcp_servers.agentchat does not say it is Hermes (no X-AgentChat-Client "
                        "header), so the hosted tools treat this agent as an unknown client and "
                        "offer it webhook-setup tools it does not need — `hermes agentchat doctor "
                        "--fix` adds the header (the gateway also does at its next start)"
                    )
                elif identity == "stale":
                    ok(
                        "client header names another plugin version — `hermes agentchat doctor "
                        "--fix` refreshes it (the gateway also does at its next start)"
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
            else:
                fail(
                    "hosted tools NOT registered — mcp_servers.agentchat missing from "
                    "config.yaml. Run `hermes agentchat doctor --fix`, or add:\n"
                    + manual_snippet(api_base)
                )
    except ImportError:
        warn("mcp_registration module not importable — skipping tool-source check")

    if _other_gateway_running():
        ok("Hermes gateway process detected (live inbound active)")
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
    if bad and newest is not None:
        what = str(newest.get("outcome") or "?")
        why = f": {newest['failure']}" if newest.get("failure") else ""
        if bad >= _WORKER_BAD_CYCLES:
            return "fail", (
                f"The receive loop's last {bad} cycles did not get through ({what}{why}) — "
                "AgentChat messages are not being answered"
            )
        return "warn", f"The receive loop's last cycle did not get through ({what}{why})"
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


def _other_gateway_running() -> bool:
    """Best-effort: is a *different* ``hermes gateway`` process alive?

    The doctor itself is a CLI process, so we exclude our own pid.
    Cross-platform via ``psutil`` when available, falls back to
    ``False`` (informational warning) when not.
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
                if "hermes" in joined and "gateway" in cmdline:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        logger.debug("doctor: process iteration failed", exc_info=True)
    return False


def _dispatch_logout(_args: argparse.Namespace) -> int:
    if not _read_saved_key():
        _printline("No AgentChat key was configured. Nothing to clear.")
        return _EXIT_OK

    try:
        from hermes_cli.config import save_env_value
    except ImportError:
        return _exit_with(
            "Cannot reach Hermes' env-config helper — is this running "
            "inside a Hermes install?"
        )

    clear_api_key(save_env_value)
    save_env_value(_ENV_HANDLE, "")
    _uninstall_soul_anchor()
    _uninstall_mcp_registration()
    _printline(
        "AgentChat key cleared from the profile .env. Your account on the "
        "server is unchanged."
    )
    return _EXIT_OK


def _install_placement(handle: str, *, api_base: str, api_key: str) -> list[PlacementStep]:
    """Place the identity line, verify the manual, register the hosted tools.

    The identity line is fetched from the server for this key (the cached
    copy when the server is unreachable; skipped when neither exists — the
    gateway places it later). Failure is non-fatal: credentials are already
    persisted and the account is fully operational. Each failed step is
    printed with its reason so the operator can repair it
    (``hermes agentchat doctor --fix`` re-runs this).
    """
    identity_line = resolve_identity_line(api_key=api_key, api_base=api_base)
    steps = place_identity_and_tools(
        handle=handle, identity_line=identity_line, api_base=api_base
    )
    for step in steps:
        if step.failed or step.status == "skipped":
            sys.stderr.write(f"Warning: {step.name}: {step.detail}\n")
            sys.stderr.flush()
        else:
            _printline(f"{step.name}: {step.detail}")
    return steps


def _doctor_identity_texts(
    client: Any, *, api_key: str, api_base: str, hermes_home: Path
) -> tuple[str | None, str | None]:
    """``(identity line, template)`` for ``doctor``: one fetch on the open client, else the cache."""
    try:
        from .state import state_dir_for_account
        from .texts import load_texts
        from .turn import identity_line as render_identity_line
        from .turn import identity_line_template as render_identity_line_template
    except ImportError:
        return None, None
    state_dir = state_dir_for_account(hermes_home=hermes_home, api_base=api_base, api_key=api_key)
    try:
        texts = load_texts(client, state_dir=state_dir)
    except Exception as exc:
        logger.debug("doctor: texts load raised: %s", exc)
        return None, None
    if texts is None:
        return None, None
    return render_identity_line(texts), render_identity_line_template(texts)


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
    if removed:
        _printline("Hosted-tools entry mcp_servers.agentchat removed from config.yaml")


def _uninstall_soul_anchor() -> None:
    """Strip the AgentChat identity block from SOUL.md (idempotent)."""
    try:
        removed = remove_soul_anchor()
    except OSError as exc:
        sys.stderr.write(
            f"Warning: could not remove the AgentChat block from SOUL.md "
            f"({exc}). You can delete the block between "
            "`<!-- agentchat:start -->` and `<!-- agentchat:end -->` "
            "manually if needed.\n"
        )
        sys.stderr.flush()
        return
    if removed:
        _printline("Identity anchor removed from ~/.hermes/SOUL.md")


# ───────────────────────── prompts ─────────────────────────


class _UserAbort(Exception):
    """Raised when the user explicitly types 'q' / 'quit' / 'cancel'."""


class _MissingFlag(Exception):
    """Raised when a required value was neither passed as a flag nor
    obtainable interactively (stdin is not a TTY). The message names the
    flag to pass."""


_PICK_HANDLE_INTRO = (
    "Pick a handle (3-30 chars, lowercase letters/digits/hyphens, "
    "must start with a letter)."
)


def _prompt_email(provided: str | None) -> str:
    if provided:
        return _validate_email(provided)
    while True:
        raw = _input("Email address: ").strip()
        _check_abort(raw)
        try:
            return _validate_email(raw)
        except ValueError as exc:
            _printline(f"  {exc}")


def _prompt_handle(provided: str | None, *, intro: str = _PICK_HANDLE_INTRO) -> str:
    if provided:
        return _validate_handle(provided)
    _printline(intro)
    while True:
        raw = _input("@").strip().lstrip("@").lower()
        _check_abort(raw)
        try:
            return _validate_handle(raw)
        except ValueError as exc:
            _printline(f"  {exc}")


def _resolve_recovery_email(provided: str | None) -> str:
    """``--email`` → validated; else prompt on a TTY; else fail naming the flag."""
    if provided:
        return _validate_email(provided)
    if not _stdin_is_tty():
        raise _MissingFlag(
            "No email given and stdin is not a terminal. Re-run with: "
            "hermes agentchat recover --handle <handle> --email <email>"
        )
    return _prompt_email(None)


def _resolve_recovery_handle(provided: str | None) -> str:
    """Which agent to recover.

    ``--handle`` wins (``@`` prefix and case are forgiven, people type
    handles the way they see them). Otherwise the locally configured
    ``AGENTCHATME_HANDLE`` — the operator is told which one and how to
    override it. Otherwise prompt on a TTY. Otherwise fail naming the
    flag: an email can back several agents, so the server cannot pick
    one for us, and a script must not hang on a prompt.
    """
    if provided:
        return _validate_handle(provided.strip().lstrip("@").lower())
    saved = _read_saved_handle()
    if saved:
        _printline(
            f"Recovering the locally configured agent @{saved} "
            "(pass --handle to recover a different one)."
        )
        return saved
    if not _stdin_is_tty():
        raise _MissingFlag(
            "No @handle given and none is saved in ~/.hermes/.env. Recovery "
            "needs the agent's handle as well as its email — re-run with: "
            "hermes agentchat recover --handle <handle> --email <email>"
        )
    return _prompt_handle(
        None,
        intro="Enter the @handle of the agent to recover (the one it was registered with).",
    )


def _prompt_code() -> str:
    while True:
        raw = _input("Verification code (6 digits): ").strip()
        _check_abort(raw)
        if re.fullmatch(r"\d{6}", raw):
            return raw
        _printline("  Code must be exactly 6 digits.")


def _prompt_api_key() -> str:
    return getpass("API key (ac_…): ").strip()


# ───────────────────────── validators ─────────────────────────


def _validate_email(value: str) -> str:
    value = value.strip()
    if not _EMAIL_RE.match(value):
        raise ValueError(f"{value!r} is not a valid email address.")
    if len(value) > 254:
        raise ValueError("Email is too long.")
    return value


def _validate_handle(value: str) -> str:
    if len(value) < 3 or len(value) > 30:
        raise ValueError("Handle must be 3-30 characters.")
    if not _HANDLE_RE.match(value):
        raise ValueError(
            "Handle must be lowercase letters/digits/hyphens, start with a "
            "letter, and not contain doubled or trailing hyphens."
        )
    return value


# ───────────────────────── helpers ─────────────────────────


def _read_saved_key_source() -> tuple[str, str] | None:
    """``(key, variable name)`` — canonical name first, legacy name as fallback."""
    from .config import resolve_api_key

    return resolve_api_key()


def _read_saved_key() -> str | None:
    resolved = _read_saved_key_source()
    return resolved[0] if resolved else None


def _read_saved_handle() -> str | None:
    """The handle this profile is configured as, or ``None``.

    ``AGENTCHATME_HANDLE`` is written by every successful register /
    login / recover, so it is normally well-formed. A hand-edited
    ``.env`` with garbage in it must not silently drive a recovery at
    the wrong handle — malformed values are ignored (with a log line)
    and the caller falls through to the prompt / flag path.
    """
    from .profile_scope import resolve_setting

    value = (resolve_setting(_ENV_HANDLE) or "").strip().lstrip("@").lower()
    if not value:
        return None
    try:
        return _validate_handle(value)
    except ValueError:
        logger.warning("ignoring malformed %s=%r", _ENV_HANDLE, value)
        return None


def _stdin_is_tty() -> bool:
    """Is stdin an interactive terminal? ``False`` for pipes, files, a
    closed or replaced stdin — anywhere a prompt would never be answered."""
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


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


def _sdk_importable() -> bool:
    try:
        import agentchatme  # noqa: F401
    except ImportError:
        return False
    return True


def _api_base() -> str:
    from .profile_scope import resolve_setting

    raw = (resolve_setting(_ENV_API_BASE) or "").strip()
    return raw.rstrip("/") if raw else _DEFAULT_API_BASE


def _persist_credentials(*, api_key: str, handle: str, api_base: str) -> None:
    """Write through Hermes's env-config so subsequent processes see it.

    The key lands under ``AGENTCHAT_API_KEY`` (the name the hosted-MCP header
    references); a pre-v3 ``AGENTCHATME_API_KEY`` line is kept in step so a
    downgrade still finds the key.
    """
    from hermes_cli.config import get_env_value, save_env_value

    persist_api_key(save_env_value, api_key=api_key, get_env_value=get_env_value)
    save_env_value(_ENV_HANDLE, handle)
    # Only persist the API base when the user overrode the default —
    # leaves the default unchanged for everyone else.
    if api_base != _DEFAULT_API_BASE:
        save_env_value(_ENV_API_BASE, api_base)


def _print_registration_success(*, handle: str, api_key: str) -> None:
    masked = _mask_key(api_key)
    _printline("")
    _printline(f"  Registered @{handle}")
    _printline(f"  Key:   {masked}   (stored in the profile .env as {_ENV_API_KEY})")
    _printline("")
    _printline(
        "Restart Hermes (or any running `hermes gateway`) so the new key "
        "is picked up; in an open session run /reload-mcp so the AgentChat "
        "tools connect. Then your agent is on the network."
    )


def _print_recovery_success(*, handle: str, api_key: str) -> None:
    masked = _mask_key(api_key)
    _printline("")
    _printline(f"  Recovered @{handle}")
    _printline(f"  New key:   {masked}   (stored in the profile .env as {_ENV_API_KEY})")
    _printline("")
    _printline(
        "The previous key is now dead. Restart Hermes (or any running "
        "`hermes gateway`) so the new key is picked up."
    )


def _print_status(me: dict[str, Any]) -> None:
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
    _printline("")


# ───────────────────────── I/O glue ─────────────────────────


_MANIFEST_NAME = re.compile(r"^name:\s*['\"]?([^'\"#\s]+)['\"]?\s*(?:#.*)?$", re.MULTILINE)


def _plugin_copies(hermes_home: Path) -> tuple[list[Path], str | None]:
    """The ``agentchat`` plugins Hermes can find for this profile.

    Returns the directory plugins named ``agentchat`` under the profile's
    ``plugins/`` (a ``hermes plugins install`` git clone, or a copy left
    there), sorted the way Hermes scans them (the last one is the one it
    loads), and the version of the pip distribution when its ``agentchat``
    entry point is installed (else ``None``). A directory's name is its
    manifest's ``name:`` (``plugin.yaml`` or ``plugin.yml``), or the
    directory name when the manifest has none, as in Hermes. Hermes keeps a
    directory plugin and drops an entry point with the same name, so with
    both present the pip package never runs. Never raises.
    """
    directories: list[Path] = []
    plugins_dir = hermes_home / "plugins"
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

    from importlib import metadata

    try:
        found: Any = metadata.entry_points()
        select = getattr(found, "select", None)
        group = select(group="hermes_agent.plugins") if callable(select) else found.get("hermes_agent.plugins", ())
        has_entry_point = any(getattr(ep, "name", None) == "agentchat" for ep in group)
    except Exception:
        has_entry_point = False
    if not has_entry_point:
        return directories, None
    try:
        return directories, metadata.version("agentchatme-hermes")
    except metadata.PackageNotFoundError:
        return directories, "(version unknown)"


def _printline(message: str) -> None:
    """Send to stdout. Hermes captures stdout for the CLI surface."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _input(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise _UserAbort("EOF on stdin")
    return line.rstrip("\n")


def _check_abort(value: str) -> None:
    if value.lower() in {"q", "quit", "cancel", "exit"}:
        raise _UserAbort("user cancelled")


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
        help="Manage your AgentChat identity (register, login, recover, status, logout, doctor)",
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
