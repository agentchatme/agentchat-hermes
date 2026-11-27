"""Interactive ``hermes agentchat`` wizard.

The menus and their wording come from the 0.1.x line — same arrow-key
picker (``prompt_choice``), same styled print helpers from
``hermes_cli.setup`` — so the wizard looks like Hermes' own setup.

The sign-up itself is the SDK's (:mod:`.signup`, on
``agentchatme.integration.account``): register and recover follow the
server's answer step by step, and what the person reads about a code, a
handle or an email address is the server's own sentence. One wrong code
asks for the code again; the person can also ask for a new code or cancel
at the code prompt. The wizard keeps no attempt count, limit or timing of
its own.

What the wizard owns:

* the menus (register / paste a key / recover / skip, and the edit menu of a
  configured profile);
* prompting, through Hermes' ``prompt`` / ``prompt_yes_no``;
* the one question, before the key is saved: may the agent use the
  AgentChat tools on its own (:func:`agentchatme_hermes.allow.decide`; Enter
  is a yes, end of input and Ctrl-C a no; never asked in an agent's
  terminal), and the same question in the edit menu while they are not
  allowed;
* saving: the key and the handle the server returned (never the one typed)
  in this profile's key file (:mod:`agentchatme_hermes.credentials`; never the
  profile ``.env``), then *placing the integration*
  (:mod:`agentchatme_hermes.placement`): the served identity line in
  ``SOUL.md``, the served manual as the ``agentchat:agentchat`` skill, the key
  source and the hosted MCP server in ``config.yaml``;
* logout: the key file and any ``.env`` key line removed, the identity line
  AgentChat placed for this profile's agent removed (a block edited by hand
  or another agent's is left, and said), our ``secrets.agentchat`` and
  ``mcp_servers.agentchat`` entries removed.

The non-interactive ``hermes agentchat <register|login|recover|status|logout>``
subcommands stay in ``cli.py``. This module is only the no-argument
interactive entry.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from . import allow, credentials, key_store, signup
from .config import ENV_HANDLE, cli_prefix, resolve_key
from .placement import (
    gateway_serves_profile,
    gateway_still_serving,
    place_identity_and_tools,
    profile_identity,
    resolve_texts,
)
from .profile_scope import resolve_hermes_home
from .soul_anchor import remove_soul_anchor

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentchatme.integration.account import AccountAnswer, AccountQuestion

    from .mcp_registration import Decision

logger = logging.getLogger(__name__)


# ─── public entry ──────────────────────────────────────────────────────────


def interactive_setup() -> None:
    """Run the interactive wizard. Wraps the body with ``KeyboardInterrupt``
    handling so Ctrl+C exits cleanly instead of dumping a traceback."""
    try:
        _interactive_setup_body()
    except KeyboardInterrupt:
        print()
        try:
            from hermes_cli.setup import print_info

            print_info("Cancelled.")
        except ImportError:
            print("Cancelled.")


def _step(message: str) -> None:
    """Step indicator — matches 0.1.x format verbatim."""
    print(f"  ✓ {message}")


def _interactive_setup_body() -> None:
    """Wizard core. State detection + branch into edit-menu or fresh-menu.

    Lazy-imports the Hermes ``hermes_cli`` helpers so this module imports
    cleanly outside a Hermes process (pytest, doc builds, etc.).
    """
    from hermes_cli.cli_output import prompt, prompt_yes_no
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt_choice,
        save_env_value,
    )

    print_header("AgentChat")
    print()

    resolution = resolve_key()
    existing_key = resolution.api_key or resolution.held_key or ""
    stored = resolution.store.key
    existing_handle = (
        (stored.handle if stored is not None else "")
        or (get_env_value(ENV_HANDLE) or "").strip().lstrip("@")
    )
    if resolution.refusal is not None:
        print_warning(resolution.refusal)
        print()

    if existing_key:
        _edit_menu(
            existing_key=existing_key,
            existing_handle=existing_handle,
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            get_env_value=get_env_value,
        )
        return

    _fresh_setup_menu(
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        prompt_choice=prompt_choice,
        print_info=print_info,
        print_success=print_success,
        print_warning=print_warning,
        save_env_value=save_env_value,
        get_env_value=get_env_value,
    )


# ─── menus (external UX from 0.1.x) ────────────────────────────────────────


def _edit_menu(
    *,
    existing_key: str,
    existing_handle: str,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Already-configured edit menu. Mirrors the 0.1.x version verbatim."""
    masked = _mask_key(existing_key)
    identity_line = (
        f"AgentChat: configured (@{existing_handle}) with key {masked}"
        if existing_handle
        else f"AgentChat: configured with key {masked} (handle not cached)"
    )
    print_info(identity_line)
    print()

    keep = "Keep current configuration"
    allow_tools = f"Allow {allow.who(existing_handle)} to use AgentChat tools on its own"
    replace = "Replace the API key (paste a new one, register a new agent, or recover a lost key)"
    logout = "Logout (clear the saved key)"
    choices = [keep, *([allow_tools] if _tools_not_allowed() else []), replace, logout]
    description = "ENTER to confirm a choice. ESC keeps the current configuration."
    idx = prompt_choice(
        "AgentChat is already configured. What would you like to do?",
        choices,
        default=0,
        description=description,
    )
    picked = choices[idx]
    _step(picked)

    if picked == keep:
        return
    if picked == allow_tools:
        _allow_tools_flow(existing_handle, print_info=print_info, print_warning=print_warning)
        return
    if picked == replace:
        _replace_key_branch(
            existing_key=existing_key,
            existing_handle=existing_handle,
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            get_env_value=get_env_value,
        )
        return
    if picked == logout:
        _logout_flow(prompt_yes_no, print_info, print_success, save_env_value)


def _fresh_setup_menu(
    *,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Top-level register / paste / recover menu for fresh installs.

    Recover is there because a lost or leaked key used to mean "register
    again", which spends a registration of the email's budget instead of
    re-keying the agent that exists. The 0.1.x "AGENTCHATME_ALLOW_ALL seed"
    step is NOT ported — that was specific to the platform-adapter
    gateway-authorization layer 0.2.0 doesn't go through.
    """
    choices = [
        "Register a new AgentChat agent (email + verification code)",
        "I already have an API key (paste ac_…)",
        "Recover a lost API key (email + @handle + verification code)",
        "Skip for now",
    ]
    description = "Register is recommended for new users — it mints a fresh @handle."
    idx = prompt_choice(
        "How would you like to configure AgentChat?",
        choices,
        default=0,
        description=description,
    )
    _step(choices[idx])

    if idx == 3:
        return

    # Read before a key is saved: a gateway a logout left serving keeps its key.
    serving = gateway_serves_profile(resolve_hermes_home(), api_base=_api_base())
    if idx == 0:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )
    elif idx == 1:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    else:
        ok = _recover_lost_key_flow(
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )

    if not ok:
        return

    print_info(credentials.key_in_use_line(None, resolve_key().api_key or "", serving=serving))
    print_success("AgentChat ready")


def _replace_key_branch(
    *,
    existing_key: str,
    existing_handle: str,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Replace-key sub-flow reached from the edit menu.

    The recover entry covers the "my key leaked, rotate it" case for the
    agent this profile is already configured as — ``existing_handle`` is
    offered as the default handle so the operator only has to confirm it.
    """
    print()
    print_info(
        "Replacing the saved API key. The current key will be replaced "
        "in this profile's key file."
    )
    print()

    choices = [
        "Paste a different API key (ac_…)",
        "Register a new agent (mints a brand-new @handle)",
        "Recover a lost API key (email + @handle + verification code — rotates the key)",
        "Cancel — keep the current key",
    ]
    idx = prompt_choice(
        "How would you like to replace it?",
        choices,
        default=0,
    )
    _step(choices[idx])

    if idx == 3:
        return

    serving = gateway_serves_profile(resolve_hermes_home(), api_base=_api_base())
    if idx == 0:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    elif idx == 1:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )
    else:
        ok = _recover_lost_key_flow(
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            existing_handle=existing_handle,
        )

    if ok:
        # A different key needs a restart (a running Hermes keeps the one it
        # started with); the same key pasted again does not.
        print_info(credentials.key_in_use_line(existing_key, resolve_key().api_key or "", serving=serving))


def _logout_flow(
    prompt_yes_no: Any, print_info: Any, print_success: Any, save_env_value: Any
) -> None:
    """Clear saved credentials, then remove our placements: the identity line
    AgentChat placed in SOUL.md for this profile's agent (a block edited by
    hand, another agent's or several are left, and said), the key source, the
    hosted-tools entry.

    Verbatim 0.1.x confirmation copy.
    """
    print()
    if not prompt_yes_no(
        f"Remove the AgentChat key ({key_store.store_path(resolve_hermes_home())}) "
        "from this profile? Your AgentChat agent will remain on the server — this "
        "only removes its key from THIS Hermes profile.",
        False,
    ):
        print_info("Cancelled. Existing credentials retained.")
        return

    # Who this profile is, read while its key is still here: only the block
    # AgentChat placed for that agent leaves SOUL.md.
    home = resolve_hermes_home()
    identity = profile_identity(home, api_base=_api_base())
    gateway_note = gateway_still_serving(home, api_base=_api_base())
    try:
        credentials.clear_credentials(home)
    except key_store.KeyStoreError as exc:
        print_info(f"The key was not removed: {exc}")
        return

    try:
        from .secrets_registration import uninstall_block

        if uninstall_block():
            _step("Key source secrets.agentchat removed from config.yaml")
    except Exception as exc:
        logger.warning("logout: secrets.agentchat removal failed: %s", exc)

    # The identity line, so the agent loses its AgentChat awareness in every
    # context: only the block AgentChat placed for this agent. Non-fatal (the
    # key is gone already); idempotent.
    removal = remove_soul_anchor(
        handle=identity.handle, identity_line=identity.identity_line, template=identity.template
    )
    if removal.removed:
        _step(f"SOUL.md: {removal.detail}")
    elif removal.outcome != "absent":
        print_info(f"SOUL.md: {removal.detail}.")

    # Remove our hosted-tools entry (a hand-managed one is left alone).
    try:
        from .mcp_registration import uninstall_entry

        if uninstall_entry(api_base=_api_base()):
            _step("Hosted-tools entry mcp_servers.agentchat removed from config.yaml")
        allow.clear_pending(home)  # removed on purpose: nothing to finish
    except Exception as exc:
        logger.warning("logout: mcp_servers.agentchat removal failed: %s", exc)

    if gateway_note is not None:
        print_info(f"{gateway_note}.")
    print_success(f"Logged out. Run `{cli_prefix(home)}` to reconfigure.")


# ─── flows ─────────────────────────────────────────────────────────────────


def _paste_existing_key_flow(
    prompt: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
) -> bool:
    """Save a pasted key, once the server accepts it, with the handle the
    server says it belongs to."""
    print()
    print_info(
        f"Paste your AgentChat API key. Mint one with `{cli_prefix(resolve_hermes_home())} register` "
        "or via the AgentChat docs if you don't have one yet."
    )
    # Masked typing: Hermes' own secret prompt (``prompt(password=True)``).
    api_key = str(prompt("API key (ac_…)", password=True)).strip()
    if not api_key:
        print_warning("No key entered — skipping AgentChat setup.")
        return False

    problem = signup.sdk_problem()
    if problem is not None:
        print_warning(problem)
        return False
    from agentchatme import ConnectionError as SDKConnectionError

    try:
        checked = signup.check_key(api_key, api_base=_api_base())
    except SDKConnectionError as exc:
        print_warning(f"Could not reach AgentChat ({exc}). The key was not saved.")
        return False
    if not checked.ok or not checked.handle:
        print_warning(f"The key was not saved. AgentChat answered: {checked.message or checked.code}")
        return False

    tools = allow.decide_for(checked.handle)
    if not _save_key(api_key, checked.handle, print_warning):
        return False
    _step(f"Key validated — you are @{checked.handle}")
    placed = _place_or_warn(checked.handle, api_key, print_warning, tools_decision=tools.decision)
    _show_tools(tools, print_info, placed)
    return True


def _register_new_agent_flow(
    *,
    prompt: Any,
    prompt_yes_no: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
) -> bool:
    """Register a new agent: email, @handle, optional display name, then the
    server's steps (:func:`_run_signup`) until it answers with a key."""
    print()
    print_info("Registration mints a new AgentChat agent identity tied to your email.")
    print_info("A verification code is sent to that address — check your inbox (and spam).")
    print()

    email = _ask_field(prompt, "register", "email")
    if email is None:
        return _cancelled(print_info)
    handle = _ask_field(prompt, "register", "handle")
    if handle is None:
        return _cancelled(print_info)
    display_name = str(
        prompt('Display name (optional, shown next to your @handle, e.g. "Alice")')
    ).strip()

    saved = _run_signup(
        "register",
        {"email": email, "handle": handle, "display_name": display_name or None},
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        print_info=print_info,
        print_warning=print_warning,
    )
    if saved is None:
        return False
    api_key, server_handle = saved

    tools = allow.decide_for(server_handle)
    if not _save_key(api_key, server_handle, print_warning):
        return False
    _step(f"Registered as @{server_handle} (key {_mask_key(api_key)})")
    placed = _place_or_warn(server_handle, api_key, print_warning, tools_decision=tools.decision)
    _show_tools(tools, print_info, placed)
    return True


def _recover_lost_key_flow(
    *,
    prompt: Any,
    prompt_yes_no: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    existing_handle: str = "",
) -> bool:
    """Re-key an agent whose API key is lost or leaked: its email and
    @handle, then the server's steps (:func:`_run_signup`).

    The handle is always sent (an email can back several agents);
    ``existing_handle`` (the handle this profile is configured as) is the
    default, because "my own key leaked, rotate it" is the common case.
    """
    print()
    print_info(
        "Recovery re-keys an existing AgentChat agent: prove control of its "
        "email with a verification code and a fresh API key is issued."
    )
    print_info(
        "The old key stops working the moment the new one is issued — "
        "update anything still using it."
    )
    print()

    email = _ask_field(prompt, "recover", "email")
    if email is None:
        return _cancelled(print_info)
    handle = _ask_field(prompt, "recover", "handle", default=existing_handle)
    if handle is None:
        return _cancelled(print_info)

    saved = _run_signup(
        "recover",
        {"email": email, "handle": handle},
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        print_info=print_info,
        print_warning=print_warning,
    )
    if saved is None:
        return False
    api_key, server_handle = saved

    tools = allow.decide_for(server_handle)
    if not _save_key(api_key, server_handle, print_warning):
        return False
    _step(f"Recovered @{server_handle} (new key {_mask_key(api_key)})")
    placed = _place_or_warn(server_handle, api_key, print_warning, tools_decision=tools.decision)
    _show_tools(tools, print_info, placed)
    return True


# ─── the sign-up conversation ──────────────────────────────────────────────


def _run_signup(
    kind: signup.Kind,
    input: Mapping[str, Any],
    *,
    prompt: Any,
    prompt_yes_no: Any,
    print_info: Any,
    print_warning: Any,
) -> tuple[str, str] | None:
    """Run the server's steps for ``kind``: ``(api_key, handle)`` when it
    answered with a key, else ``None`` (cancelled, stopped, or unreachable;
    said, and nothing saved)."""
    problem = signup.sdk_problem()
    if problem is not None:
        print_warning(problem)
        return None
    from agentchatme import ConnectionError as SDKConnectionError

    print()
    try:
        result = signup.run(
            kind,
            ask=_asker(prompt, prompt_yes_no, print_info),
            show=print_info,
            input=input,
            api_base=_api_base(),
        )
    except SDKConnectionError as exc:
        print_warning(f"Could not reach AgentChat ({exc}). Nothing was saved.")
        return None

    saved = signup.saved_credentials(result)
    if saved is not None:
        return saved
    if result.outcome == "cancelled":
        _cancelled(print_info)
        return None
    # Stopped: the server's message has been shown.
    if not result.message:
        print_warning(f"AgentChat answered {result.code or 'without a message'}.")
    print_warning("Nothing was saved.")
    return None


def _asker(prompt: Any, prompt_yes_no: Any, print_info: Any) -> Any:
    """The helper's ``ask``, on Hermes' prompts. ``prompt`` returns ``""`` on
    Ctrl+C, so a blank line cancels (it never sends a new code)."""

    def ask(question: AccountQuestion) -> AccountAnswer:
        if question.field == "new_code":
            return signup.new_code_answer(bool(prompt_yes_no(signup.NEW_CODE_QUESTION, False)))
        handles = signup.handles_line(question)
        if handles is not None:
            print_info(handles)
        return signup.answer_of(question, str(prompt(signup.prompt_text(question))))

    return ask


def _ask_field(
    prompt: Any, kind: signup.Kind, field: Literal["email", "handle"], *, default: str = ""
) -> str | None:
    """Ask ``field`` before the first call; ``None`` when left blank.
    ``default`` (when set) is shown by Hermes' ``prompt`` in brackets and
    returned on a bare ENTER."""
    question = signup.field_prompt(kind, field)
    raw = prompt(question, default=default) if default else prompt(question)
    return signup.typed_value(field, str(raw)) or None


def _cancelled(print_info: Any) -> bool:
    print_info("Cancelled. Nothing was saved.")
    return False


# ─── display ───────────────────────────────────────────────────────────────


def _mask_key(key: str) -> str:
    """Render a key for display without leaking the bulk of the secret.

    A key is ``ac_`` and 43 base64url characters (32 random bytes). Shown
    as its first 8 characters and its last 4 — the wizard's 0.1.x format
    and the one OpenClaw's integration uses — so 34 of the 43 random
    characters stay hidden. The wizard and the subcommands share this
    function. A value of 12 characters or fewer is no key and would be
    shown whole, so it reads ``ac_…``.
    """
    if len(key) <= 12:
        return "ac_…"
    prefix = key[:8]
    suffix = key[-4:]
    return f"{prefix}…{suffix}"


# ─── placement ─────────────────────────────────────────────────────────────


def _save_key(api_key: str, handle: str, print_warning: Any) -> bool:
    """Save the key and the handle in this profile's key file (:mod:`.credentials`).

    ``False`` when the key is saved nowhere (said). When the key file cannot
    be written, the key is kept in the profile ``.env`` instead and that is
    said: a key the server just issued must not be lost.
    """
    ready, why = credentials.key_bridge()
    try:
        outcome = credentials.save_credentials(
            api_key=api_key, handle=handle, api_base=_api_base(), use_key_file=ready, not_ready=why
        )
    except key_store.KeyStoreError as exc:
        print_warning(f"The key was not saved: {exc}")
        return False
    if outcome.fallback_reason is not None:
        print_warning(
            f"The key was saved in the profile .env, not in the key file: {outcome.fallback_reason}. "
            f"Fix that, then run `{cli_prefix(resolve_hermes_home())} doctor --fix`."
        )
    elif outcome.path is not None:
        _step(f"Key saved in {outcome.path} (readable by you only)")
    return True


def _show_tools(tools: allow.SetupDecision, print_info: Any, mcp_outcome: str | None = None) -> None:
    """The answer's line: a check mark for a yes or tools already allowed;
    nothing when the placement's warning said it (:func:`.allow.line_after`)."""
    if allow.line_after(tools, mcp_outcome) is None:
        return
    if tools.decision == "yes" or tools.line == allow.ALREADY_ALLOWED_LINE:
        _step(allow.ALLOWED_LINE)
    else:
        print_info(tools.line)


def _tools_not_allowed() -> bool:
    """Whether the edit menu offers the one question: the entry's own tier
    does not allow the tools (nobody allowed them yet, a no, or only
    approvals off / --yolo let them through)."""
    switch = allow.read_switch(resolve_hermes_home(), api_base=_api_base())
    return switch.state in ("not_allowed_yet", "not_allowed") or (switch.state == "allowed" and not switch.by_tier)


def _allow_tools_flow(handle: str, *, print_info: Any, print_warning: Any) -> None:
    """The edit menu's "Allow …": the one question, then the entry as answered."""
    from .placement import place_mcp

    tools = allow.decide_for(handle)
    if tools.decision is None:
        _show_tools(tools, print_info)
        return
    step = place_mcp(_api_base(), decision=tools.decision)
    if step.failed:
        print_warning(f"AgentChat tools: nothing changed — {step.detail}")
        return
    if step.outcome == "restart":
        print_warning(f"AgentChat tools: {step.detail}")
        return
    _show_tools(tools, print_info)
    if step.outcome == "applying":
        _step(f"The running gateway applies it: {allow.APPLY_WORDS}")
    elif step.outcome == "allowed":
        _step("An open Hermes session uses it after /reload-mcp")
    elif step.outcome == "added" and tools.decision == "yes":
        _step("A running gateway connects them within about a minute")


def _place_or_warn(
    handle: str, api_key: str, print_warning: Any, *, tools_decision: Decision | None = None
) -> str | None:
    """Place the identity line and the manual, register the hosted tools.

    The texts are fetched from the server for this key (cached copy when the
    server is unreachable). By default the identity line and the manual are
    this release's; with ``live_texts`` on they are the served ones (without
    texts the line is skipped and the manual is the bundled copy — the
    gateway places both later). Non-fatal — the credentials are persisted
    and the runtime will boot; each failed step is surfaced so the operator
    can repair it (``hermes agentchat doctor --fix`` re-runs the placement).
    Returns what the tools' placement did (:attr:`.placement.PlacementStep.outcome`).
    """
    api_base = _api_base()
    texts = resolve_texts(api_key=api_key, api_base=api_base)
    mcp_outcome: str | None = None
    for step in place_identity_and_tools(handle=handle, texts=texts, api_base=api_base, tools_decision=tools_decision):
        if step.name == "mcp":
            mcp_outcome = step.outcome
        if step.name == "mcp" and step.outcome == "missing":
            continue  # the AgentChat tools line says it
        if step.needs_attention:
            print_warning(f"{step.name}: {step.detail}")
        else:
            _step(step.detail)
    return mcp_outcome


def _api_base() -> str:
    """The API base for this profile (:func:`.config.resolve_api_base`, the one resolver)."""
    from .config import resolve_api_base, stored_api_base

    return resolve_api_base(stored_api_base(), strict=False)
