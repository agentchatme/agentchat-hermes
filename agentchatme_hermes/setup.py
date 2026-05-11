"""Setup wizard + helper functions for the AgentChat Hermes plugin.

Exports:

* :func:`check_requirements` — pre-flight check for `register_platform()`.
* :func:`validate_config` — config-schema validation for the platform-status UI.
* :func:`is_connected` — "is this profile minimally configured?" predicate.
* :func:`env_enablement` — seed ``PlatformConfig.extra`` from env vars.
* :func:`interactive_setup` — the multi-step ``hermes gateway setup`` wizard.
* :func:`register_via_otp` / :func:`login_via_paste` / :func:`whoami` /
  :func:`logout` — building blocks reused by the ``hermes agentchat …`` CLI.

The wizard flow mirrors the OpenClaw plugin's ``channels.add`` wizard
(``agentchat-openclaw/src/channel.wizard.ts``) — branch on "have key vs
register", multi-step prompts with shape validation, field-specific retry
loops, and a final ``GET /v1/agents/me`` confirmation. All Hermes UX
helpers come from ``hermes_cli.setup`` so the prompts feel identical to
the built-in adapters.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


# ─── Handle / email shapes ─────────────────────────────────────────────────
# Mirrors packages/shared/src/validation/handles.ts on the server. Keeping a
# client-side fast-fail saves a round-trip on obvious typos and matches the
# error vocabulary the user would see anyway. The server is authoritative.

_HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_HANDLE_MIN = 3
_HANDLE_MAX = 30
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_OTP_PATTERN = re.compile(r"^\d{6}$")
# Hard cap on retries for invalid-handle / email-taken loops before we offer
# the "paste an existing key or cancel" off-ramp. 5 is enough to converge on
# a fresh handle without punishing a confused user; matches the OpenClaw
# wizard's ``MAX_START_RETRIES`` constant.
_MAX_REGISTER_RETRIES = 5


# ─── PlatformEntry callbacks ───────────────────────────────────────────────


def check_requirements() -> bool:
    """Pre-flight: verify the SDK is importable.

    The framework calls this BEFORE adapter construction; returning False
    means "this platform's optional deps are missing, hide it from the
    gateway-setup picker." Since `agentchatme` is declared as a hard
    dependency in our ``pyproject.toml``, the only way this fails is if
    someone vendored our wheel without resolving deps, which is a setup
    bug we want to surface loudly.
    """
    try:
        import agentchatme  # noqa: F401
    except ImportError as e:
        logger.error("AgentChat: agentchatme SDK not importable — %s", e)
        return False
    return True


def validate_config(config: Any) -> bool:
    """Validate the PlatformConfig before adapter construction.

    Returning False suppresses the platform from auto-enable and surfaces
    "config invalid" in `gateway status`. The api_key shape check is
    intentionally permissive — server enforces the real format
    (`ac_(live|test)_<base62>`) and a near-but-not-quite key should fail
    loudly at /v1/agents/me, not silently here.
    """
    extra = getattr(config, "extra", {}) or {}
    api_key = (os.getenv("AGENTCHATME_API_KEY") or extra.get("api_key") or "").strip()
    # Even loosely-shaped keys exceed 20 chars. Anything shorter is a paste
    # error — fail closed. Empty string also falls through this check.
    return len(api_key) >= 20


def is_connected(config: Any) -> bool:
    """Return True iff the AgentChat platform has the minimum env to boot.

    Mirrors IRC's `is_connected` (`adapter.py:643-648`) — used by `gateway
    status` to draw a per-platform check/cross without instantiating the
    adapter. The strict source-of-truth is whether the WS opens, but the
    UI needs a synchronous predicate.
    """
    extra = getattr(config, "extra", {}) or {}
    api_key = (os.getenv("AGENTCHATME_API_KEY") or extra.get("api_key") or "").strip()
    return bool(api_key)


def env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars at gateway-config load.

    Returns None when env-only minimum (just AGENTCHATME_API_KEY) isn't
    met — the caller skips auto-enable. Mirrors IRC's `_env_enablement`
    (`adapter.py:651-699`). Note that `home_channel`, when seeded, gets
    promoted to a `HomeChannel` dataclass by the framework's hook layer
    rather than living inside `extra`.
    """
    api_key = os.getenv("AGENTCHATME_API_KEY", "").strip()
    if not api_key:
        return None

    seed: dict = {"api_key": api_key}

    api_base = os.getenv("AGENTCHATME_API_BASE", "").strip()
    if api_base:
        seed["api_base"] = api_base

    handle = os.getenv("AGENTCHATME_HANDLE", "").strip()
    if handle:
        seed["handle"] = handle.lstrip("@").lower()

    allowed = os.getenv("AGENTCHATME_ALLOWED_HANDLES", "").strip()
    if allowed:
        seed["allowed_handles"] = [
            h.strip().lstrip("@").lower() for h in allowed.split(",") if h.strip()
        ]

    home = os.getenv("AGENTCHATME_HOME_CONVERSATION", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": home,
        }

    return seed


# ─── Interactive setup wizard (hermes gateway setup) ───────────────────────


def interactive_setup() -> None:
    """Multi-step setup wizard called from `hermes gateway setup`.

    Flow:

    1. Check existing key. If present, offer reconfigure / leave alone /
       paste new / re-register.
    2. Branch on user intent: register a new agent or paste an existing key.
    3. On register: collect email + handle + display name, OTP roundtrip
       with field-specific retries, persist minted key.
    4. On paste: prompt for the key, validate via /v1/agents/me, persist.
    5. Optional: API base override (self-hosted), allowlist, home conv.

    All persistence goes through `save_env_value` so the state lives in
    `~/.hermes/.env` alongside every other adapter's tokens.

    Wrapped in a top-level try/except so an unexpected exception in our
    wizard does NOT kill the entire `hermes gateway setup` command for
    every other platform too. Hermes does not wrap the `setup_fn` call
    site at `hermes_cli/gateway.py:4728`. Discovered in the v0.1.62 audit.
    """
    try:
        _interactive_setup_body()
    except KeyboardInterrupt:
        # Ctrl+C is a deliberate cancel — let it propagate cleanly so
        # the gateway-setup wizard moves on, but avoid the full
        # traceback display.
        print()
        print("AgentChat setup cancelled.")
    except Exception as exc:
        # Anything else is an unexpected failure inside our wizard.
        # Log full traceback for ops, surface a friendly one-liner to
        # the operator. The next platform in `hermes gateway setup`
        # still gets its turn.
        logger.exception("AgentChat: interactive_setup failed")
        try:
            from hermes_cli.setup import print_warning  # type: ignore[import-not-found]

            print_warning(
                f"AgentChat setup failed: {exc}. "
                "Skipping — run `hermes agentchat register` from a fresh "
                "terminal to try again. The other platform setup steps "
                "below will continue normally."
            )
        except Exception:
            # Even the friendly-error path itself failed (probably
            # because hermes_cli isn't importable). Fall back to stderr.
            import sys

            print(f"AgentChat setup failed: {exc}", file=sys.stderr)


def _step(message: str) -> None:
    """Print a breadcrumb step in the wizard trail.

    Mirrors clack/prompts' ``◇  step result`` style so the user can
    glance at the terminal scrollback and see every decision they made
    accumulating in order. Hermes's ``prompt_choice`` is curses-based and
    its on-screen panel disappears when the user makes a selection — the
    only way to preserve "what you just chose" in the final output is to
    print our own one-line summary right after each step returns.

    Style: cyan ``◇`` glyph + dim text body. Uses ANSI escapes directly
    so we don't depend on a styled-print helper that adds the
    ``print_info`` two-space indent (the trail looks cleaner flush-left).
    """
    import sys

    if not sys.stdout.isatty():
        print(f"  {message}")
        return
    # Cyan ◇ + dim body; reset at end.
    print(f"\033[36m◇\033[0m  \033[2m{message}\033[0m")


def _interactive_setup_body() -> None:
    """Actual wizard. Separated so `interactive_setup` can wrap it.

    Two import sources, deliberately split:

    * ``hermes_cli.cli_output`` — ``prompt`` / ``prompt_yes_no`` from this
      module are **graceful on Ctrl+C** (return empty / default). The
      versions in ``hermes_cli.setup`` ``sys.exit(1)`` instead, which
      kills the entire ``hermes gateway setup`` flow for sibling
      platforms. Matches the Teams / Google Chat plugin pattern
      (``plugins/platforms/{teams,google_chat}/adapter.py``).
    * ``hermes_cli.setup`` — ``prompt_choice`` is the curses-driven
      arrow-key menu primitive (``setup.py:236``). Plus the styled
      print helpers and ``save_env_value`` / ``get_env_value`` which
      only live there.
    """
    from hermes_cli.cli_output import prompt, prompt_yes_no  # type: ignore[import-not-found]
    from hermes_cli.setup import (  # type: ignore[import-not-found]
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

    existing_key = (get_env_value("AGENTCHATME_API_KEY") or "").strip()
    existing_handle = (get_env_value("AGENTCHATME_HANDLE") or "").strip().lstrip("@")

    # Branch on detected state. The OpenClaw wizard distinguishes four
    # states (`channel.wizard.ts:588-616`); we collapse "no handle"
    # into "configured" because the gateway adapter resolves identity
    # on connect via ``GET /v1/agents/me`` regardless.
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


def _edit_menu(
    *,
    existing_key: str,
    existing_handle: str,
    prompt,
    prompt_yes_no,
    prompt_choice,
    print_info,
    print_success,
    print_warning,
    save_env_value,
    get_env_value,
) -> None:
    """Already-configured edit menu. Mirrors OpenClaw's
    ``channel.wizard.ts:588-616`` 4-option select."""
    masked = _mask_key(existing_key)
    identity_line = (
        f"AgentChat: configured (@{existing_handle}) with key {masked}"
        if existing_handle
        else f"AgentChat: configured with key {masked} (handle not cached)"
    )
    print_info(identity_line)
    print()

    choices = [
        "Keep current configuration",
        "Replace the API key (paste a new one, or register a new agent)",
        "Logout (clear the saved key)",
    ]
    description = (
        "ENTER to confirm a choice. ESC keeps the current configuration."
    )
    idx = prompt_choice(
        "AgentChat is already configured. What would you like to do?",
        choices,
        default=0,
        description=description,
    )
    _step(choices[idx])

    if idx == 0:
        return
    if idx == 1:
        _replace_key_branch(
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
    if idx == 2:
        _logout_flow(prompt_yes_no, print_info, print_success, save_env_value)
        return


def _fresh_setup_menu(
    *,
    prompt,
    prompt_yes_no,
    prompt_choice,
    print_info,
    print_success,
    print_warning,
    save_env_value,
    get_env_value,
) -> None:
    """Top-level register-or-paste menu for fresh installs. Mirrors
    OpenClaw's ``channel.wizard.ts:618-633`` 3-option select."""
    choices = [
        "Register a new AgentChat agent (email + 6-digit OTP, ~60s)",
        "I already have an API key (paste ac_live_…)",
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

    if idx == 2:
        return

    if idx == 0:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )
    else:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )

    if not ok:
        return

    # Default ALLOW_ALL=true so inbound DMs aren't dropped by Hermes's
    # gateway-level deny-all. AgentChat enforces inbox_mode server-side
    # so the framework allowlist is redundant — see `_seed_allow_all_default`.
    if _seed_allow_all_default(save_env_value, get_env_value):
        _step("Inbox open (server enforces inbox_mode)")

    _step("Restart the gateway: hermes gateway restart")
    print_success("AgentChat ready")


def _replace_key_branch(
    *,
    prompt,
    prompt_yes_no,
    prompt_choice,
    print_info,
    print_success,
    print_warning,
    save_env_value,
    get_env_value,
) -> None:
    """Replace-key sub-flow reached from the edit menu. Same register-or-paste
    menu as a fresh install, but with the explicit "going to overwrite"
    framing so the user knows the current key will be replaced."""
    print()
    print_info(
        "Replacing the saved API key. The current key will be overwritten "
        "in ~/.hermes/.env."
    )
    print()

    choices = [
        "Paste a different API key (ac_live_…)",
        "Register a new agent (mints a brand-new @handle)",
        "Cancel — keep the current key",
    ]
    idx = prompt_choice(
        "How would you like to replace it?",
        choices,
        default=0,
    )
    _step(choices[idx])

    if idx == 2:
        return

    if idx == 0:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    else:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )

    if ok:
        if _seed_allow_all_default(save_env_value, get_env_value):
            _step("Inbox open (server enforces inbox_mode)")
        _step("Restart the gateway: hermes gateway restart")


def _logout_flow(prompt_yes_no, print_info, print_success, save_env_value) -> None:
    """Clear the saved key + handle. The agent on the AgentChat server is
    untouched — only this Hermes profile loses access."""
    print()
    if not prompt_yes_no(
        "Clear AGENTCHATME_API_KEY and AGENTCHATME_HANDLE from ~/.hermes/.env? "
        "Your AgentChat agent will remain on the server — this only removes "
        "credentials from THIS Hermes profile.",
        False,
    ):
        print_info("Cancelled. Existing credentials retained.")
        return
    save_env_value("AGENTCHATME_API_KEY", "")
    save_env_value("AGENTCHATME_HANDLE", "")
    print_success("Logged out. Run `hermes agentchat` to reconfigure.")


# ─── Wizard sub-flows ──────────────────────────────────────────────────────


def _seed_allow_all_default(save_env_value, get_env_value) -> bool:
    """Set ``AGENTCHATME_ALLOW_ALL=true`` if the operator hasn't already
    chosen a different setting.

    Why this default exists: Hermes's gateway-level ``_is_user_authorized``
    (``gateway/run.py:3320-3324``) defaults to DENY when no allowlist is
    configured — a sensible safety default for platforms like Telegram
    where strangers can DM your bot. But AgentChat already enforces
    who-can-DM-you on the server side via the agent's ``inbox_mode``
    (open / contacts_only). Double-gating just blocks legitimate messages.

    We default ALLOW_ALL=true on first key save so the agent actually
    receives inbound DMs. If the operator has manually set
    ``AGENTCHATME_ALLOW_ALL`` or ``AGENTCHATME_ALLOWED_HANDLES`` to a
    different value, we leave it alone — they've made a choice.

    Returns True if we seeded the default, False if we left an existing
    setting in place.
    """
    existing_allow_all = (get_env_value("AGENTCHATME_ALLOW_ALL") or "").strip().lower()
    existing_allowlist = (get_env_value("AGENTCHATME_ALLOWED_HANDLES") or "").strip()
    if existing_allow_all or existing_allowlist:
        return False
    save_env_value("AGENTCHATME_ALLOW_ALL", "true")
    return True


def _paste_existing_key_flow(prompt, print_info, print_success, print_warning, save_env_value) -> bool:
    print()
    print_info("Paste your AgentChat API key. Mint one with `hermes agentchat register` or via the AgentChat docs if you don't have one yet.")
    api_key = prompt("API key (ac_live_…)", password=True).strip()
    if not api_key:
        print_warning("No key entered — skipping AgentChat setup.")
        return False
    if len(api_key) < 20:
        print_warning(f"That key is too short ({len(api_key)} chars) — refusing to save it.")
        return False

    handle = _validate_key_remote(api_key, print_warning)
    if not handle:
        print_warning("Key validation failed — not persisted. Try again with a fresh key.")
        return False

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", handle)
    _step(f"Key validated — you are @{handle}")
    return True


def _register_new_agent_flow(
    *,
    prompt,
    prompt_choice,
    print_info,
    print_success,
    print_warning,
    save_env_value,
) -> bool:
    """Email-OTP registration flow. Mirrors OpenClaw's ``runRegisterFlow``
    (``channel.wizard.ts:250-474``).

    Two layers of recovery:

    * **Field-scoped retry.** ``HANDLE_TAKEN`` / ``INVALID_HANDLE`` re-prompts
      only the handle; ``EMAIL_TAKEN`` / ``EMAIL_EXHAUSTED`` re-prompts only
      the offending field — email + display_name aren't touched by a handle
      collision and vice versa.
    * **Errors-as-navigation.** ``EMAIL_TAKEN`` and ``EMAIL_EXHAUSTED`` open
      a 3-option ``prompt_choice`` recovery menu (the most-likely-correct
      action defaults: paste-key when an account already exists, swap-email
      when the quota is hit) instead of a flat retry. Cancel is always an
      option.
    """
    print()
    print_info("Registration mints a new AgentChat agent identity tied to your email.")
    print_info("You will receive a 6-digit code to verify — check your inbox (and spam).")
    print()

    email = _prompt_email(prompt, print_warning)
    if email is None:
        return False
    handle = _prompt_handle(prompt, print_warning)
    if handle is None:
        return False
    display_name = prompt(
        "Display name (shown next to your @handle, e.g. \"Alice\")"
    ).strip()

    pending_id = None
    for attempt in range(1, _MAX_REGISTER_RETRIES + 1):
        try:
            pending_id = _register_start(
                email=email, handle=handle, display_name=display_name
            )
            break
        except _RegisterError as err:
            if err.field == "handle" and attempt < _MAX_REGISTER_RETRIES:
                print_warning(f"Handle problem: {err}")
                new_handle = _prompt_handle(prompt, print_warning)
                if new_handle is None:
                    return False
                handle = new_handle
                continue

            if err.field == "email" and attempt < _MAX_REGISTER_RETRIES:
                next_step = _email_error_recovery(
                    code=err.code or "",
                    message=str(err),
                    prompt=prompt,
                    prompt_choice=prompt_choice,
                    print_info=print_info,
                    print_warning=print_warning,
                )
                if next_step == "cancel":
                    return False
                if next_step == "paste":
                    # User chose to switch paths — defer to paste flow with a
                    # one-line break before that wizard's own intro.
                    print()
                    return _paste_existing_key_flow(
                        prompt, print_info, print_success, print_warning, save_env_value
                    )
                # next_step == "retry-email" — re-prompt and loop.
                new_email = _prompt_email(prompt, print_warning)
                if new_email is None:
                    return False
                email = new_email
                continue

            print_warning(f"Registration failed: {err}")
            return False
        except Exception as e:
            print_warning(f"Could not reach AgentChat: {e}")
            return False

    if not pending_id:
        return False

    print()
    print_info(f"Verification code sent to {email}. Check your inbox.")
    code = _prompt_otp(prompt, print_warning)
    if not code:
        return False

    try:
        api_key, resolved_handle = _register_verify(pending_id=pending_id, code=code)
    except _RegisterError as err:
        print_warning(f"Verification failed: {err}")
        return False
    except Exception as e:
        print_warning(f"Verification request failed: {e}")
        return False

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", resolved_handle)
    masked = _mask_key(api_key)
    _step(f"Registered as @{resolved_handle} (key {masked})")
    return True


def _email_error_recovery(
    *,
    code: str,
    message: str,
    prompt,
    prompt_choice,
    print_info,
    print_warning,
) -> str:
    """Render the OpenClaw-style errors-as-navigation menu for email-class
    server NACKs. Returns one of ``"paste"``, ``"retry-email"``, ``"cancel"``.

    ``EMAIL_TAKEN`` defaults to **paste-existing-key** because the most-likely
    user is someone who registered before and forgot. ``EMAIL_EXHAUSTED``
    defaults to **use-different-email** because the user has hit the quota
    for that address — a different email is more likely to succeed than
    digging up a key.
    """
    print_warning(message)

    if code == "EMAIL_TAKEN":
        choices = [
            "Paste the existing API key for this agent (recommended if you own it)",
            "Use a different email address",
            "Cancel registration",
        ]
        default = 0
        question = "That email is already registered. What now?"
    elif code == "EMAIL_EXHAUSTED":
        choices = [
            "Use a different email address",
            "Paste an existing API key instead",
            "Cancel registration",
        ]
        default = 0
        question = "Too many recent registrations for that email. What now?"
    else:
        # Generic email-class error (validation, unreachable, etc.) — same
        # menu but no recommended-default leans either way.
        choices = [
            "Use a different email address",
            "Paste an existing API key instead",
            "Cancel registration",
        ]
        default = 0
        question = "Couldn't register with that email. What now?"

    idx = prompt_choice(question, choices, default=default)

    # Map by position because the order of "paste" vs "retry-email" flips
    # between EMAIL_TAKEN (paste first) and EMAIL_EXHAUSTED (retry first).
    if code == "EMAIL_TAKEN":
        return ["paste", "retry-email", "cancel"][idx]
    return ["retry-email", "paste", "cancel"][idx]


# ─── Prompt helpers ────────────────────────────────────────────────────────


def _prompt_email(prompt, print_warning) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = prompt("Email — receives the 6-digit verification code (e.g. you@example.com)").strip()
        if not value:
            print_warning("Email is required.")
            continue
        if not _EMAIL_PATTERN.match(value):
            print_warning("That doesn't look like a valid email. Try again.")
            continue
        return value
    print_warning("Too many invalid email attempts — aborting.")
    return None


def _prompt_handle(prompt, print_warning) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = prompt(
            "Choose a @handle (3-30 chars, lowercase letters/digits/hyphens, must start with a letter)"
        ).strip().lstrip("@").lower()
        err = _validate_handle(value)
        if err:
            print_warning(err)
            continue
        return value
    print_warning("Too many invalid handle attempts — aborting.")
    return None


def _prompt_otp(prompt, print_warning) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = prompt("Enter the 6-digit code from your inbox").strip()
        if _OTP_PATTERN.match(value):
            return value
        print_warning("Codes are 6 digits. Try again.")
    print_warning("Too many invalid code attempts — aborting.")
    return None


def _validate_handle(value: str) -> str | None:
    """Return error message on shape failure, None on success."""
    if not value:
        return "Handle is required."
    if len(value) < _HANDLE_MIN or len(value) > _HANDLE_MAX:
        return f"Length must be {_HANDLE_MIN}-{_HANDLE_MAX} chars (you entered {len(value)})."
    if not value[0].isalpha():
        return "Must start with a lowercase letter."
    if re.search(r"[^a-z0-9-]", value):
        return "Only lowercase letters, digits, and hyphens — no underscores, dots, or symbols."
    if "--" in value:
        return "No consecutive hyphens."
    if value.endswith("-"):
        return "Cannot end with a hyphen."
    if not _HANDLE_PATTERN.match(value):
        return "Invalid handle shape."
    return None


def _mask_key(key: str) -> str:
    """Render a key for display without leaking the bulk of the secret."""
    if len(key) < 12:
        return "ac_…"
    prefix = key[:8]
    suffix = key[-4:]
    return f"{prefix}…{suffix}"


# ─── Network helpers (re-used by CLI subcommands) ──────────────────────────


class _RegisterError(Exception):
    """Surface a server registration error with field-context for the wizard.

    ``field`` lets the wizard re-prompt only the offending input
    (``handle``/``email``). ``code`` carries the canonical server error
    code (``EMAIL_TAKEN``, ``EMAIL_EXHAUSTED``, ``HANDLE_TAKEN``,
    ``INVALID_HANDLE``, ``RATE_LIMITED``, etc.) so the wizard can branch
    into the OpenClaw-style errors-as-navigation recovery menus rather
    than treating every server NACK as a flat retry.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


def _api_base() -> str:
    return (os.getenv("AGENTCHATME_API_BASE") or "https://api.agentchat.me").rstrip("/")


def _register_start(*, email: str, handle: str, display_name: str) -> str:
    """POST /v1/register — returns the pending_id for the OTP verify step.

    Done synchronously via httpx (the SDK ships it as a hard dep so we get
    it transitively). Sync calls are fine inside the wizard — there is no
    parallelism to lose, and it keeps the CLI integration trivial.
    """
    import httpx

    body: dict = {"email": email, "handle": handle}
    if display_name:
        body["display_name"] = display_name
    try:
        resp = httpx.post(
            f"{_api_base()}/v1/register",
            json=body,
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        raise _RegisterError(f"network error: {e}") from e

    if resp.status_code == 200 or resp.status_code == 201:
        try:
            data = resp.json()
        except Exception:
            raise _RegisterError("invalid server response") from None
        pending_id = data.get("pending_id")
        if not pending_id:
            raise _RegisterError("server did not return a pending_id")
        return str(pending_id)

    # Map server error codes onto field hints so the wizard's retry loop
    # can re-prompt the right field. The server emits these as JSON
    # `{ code, message }` shapes — see `apps/api-server/src/routes/register.ts`.
    code, message = _parse_error(resp)
    if code in ("HANDLE_TAKEN", "INVALID_HANDLE", "RESERVED_HANDLE"):
        raise _RegisterError(message or code, field="handle", code=code)
    if code in ("EMAIL_TAKEN", "EMAIL_EXHAUSTED"):
        raise _RegisterError(message or code, field="email", code=code)
    if code == "RATE_LIMITED":
        raise _RegisterError(
            "rate-limited — wait a minute and try again", field=None, code=code
        )
    raise _RegisterError(
        message or code or f"HTTP {resp.status_code}", code=code
    )


def _register_verify(*, pending_id: str, code: str) -> tuple[str, str]:
    """POST /v1/register/verify → returns (api_key, handle)."""
    import httpx

    try:
        resp = httpx.post(
            f"{_api_base()}/v1/register/verify",
            json={"pending_id": pending_id, "code": code},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        raise _RegisterError(f"network error: {e}") from e

    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except Exception:
            raise _RegisterError("invalid server response") from None
        api_key = data.get("api_key")
        agent = data.get("agent") or {}
        handle = agent.get("handle")
        if not api_key or not handle:
            raise _RegisterError("server response missing api_key or handle")
        return str(api_key), str(handle)

    err_code, message = _parse_error(resp)
    raise _RegisterError(message or err_code or f"HTTP {resp.status_code}")


def _validate_key_remote(api_key: str, print_warning) -> str | None:
    """Call GET /v1/agents/me and return the resolved handle on success.

    Used by the paste-existing-key path and by `hermes agentchat whoami`.
    Network errors are surfaced via print_warning; an invalid key returns
    None so the caller can re-prompt or abort.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{_api_base()}/v1/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        print_warning(f"Could not reach AgentChat: {e}")
        return None

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            print_warning("Server returned an invalid response.")
            return None
        handle = data.get("handle")
        return handle if isinstance(handle, str) else None

    if resp.status_code in (401, 403):
        print_warning("Key was rejected by the server (401/403). Double-check it and try again.")
        return None

    print_warning(f"Unexpected response from server: HTTP {resp.status_code}")
    return None


def _parse_error(resp) -> tuple[str | None, str | None]:
    """Pull `{code, message}` from a JSON error response. Returns (code, message)."""
    try:
        data = resp.json()
    except Exception:
        return None, None
    code = data.get("code")
    message = data.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


# ─── CLI subcommand backends ───────────────────────────────────────────────


def cli_register(email: str | None, handle: str | None, display_name: str | None) -> int:
    """Backend for `hermes agentchat register`. Returns shell exit code."""
    from hermes_cli.setup import (  # type: ignore[import-not-found]
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        save_env_value,
    )

    print_header("AgentChat — register a new agent")

    if not email:
        eml = _prompt_email(prompt, print_warning)
        if eml is None:
            return 2
        email = eml
    if not handle:
        h = _prompt_handle(prompt, print_warning)
        if h is None:
            return 2
        handle = h
    display_name = (display_name or "").strip()

    # Run the same retry loop the wizard uses, surfacing field-specific
    # prompts so a piped invocation (e.g. `echo y | hermes agentchat register`)
    # still has a sensible failure mode — the loop bails after the first
    # field error if the next prompt yields nothing.
    pending_id = None
    for attempt in range(1, _MAX_REGISTER_RETRIES + 1):
        try:
            pending_id = _register_start(email=email, handle=handle, display_name=display_name)
            break
        except _RegisterError as err:
            if err.field == "handle" and attempt < _MAX_REGISTER_RETRIES:
                print_warning(f"Handle problem: {err}")
                new_handle = _prompt_handle(prompt, print_warning)
                if new_handle is None:
                    return 2
                handle = new_handle
                continue
            if err.field == "email" and attempt < _MAX_REGISTER_RETRIES:
                print_warning(f"Email problem: {err}")
                new_email = _prompt_email(prompt, print_warning)
                if new_email is None:
                    return 2
                email = new_email
                continue
            print_warning(f"Registration failed: {err}")
            return 1
        except Exception as e:
            print_warning(f"Could not reach AgentChat: {e}")
            return 1

    if not pending_id:
        return 1

    print_info(f"Verification code sent to {email}.")
    code = _prompt_otp(prompt, print_warning)
    if not code:
        return 2

    try:
        api_key, resolved = _register_verify(pending_id=pending_id, code=code)
    except _RegisterError as err:
        print_warning(f"Verification failed: {err}")
        return 1

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", resolved)
    if _seed_allow_all_default(save_env_value, get_env_value):
        print_info(
            "AGENTCHATME_ALLOW_ALL=true (server enforces inbox_mode; framework allowlist disabled)."
        )
    print_success(f"Registered as @{resolved}. API key saved to ~/.hermes/.env.")
    print_info("Restart the gateway: hermes gateway restart")
    return 0


def cli_login(api_key: str | None) -> int:
    """Backend for `hermes agentchat login` — paste an existing key."""
    from hermes_cli.setup import (  # type: ignore[import-not-found]
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        save_env_value,
    )

    print_header("AgentChat — paste an existing API key")

    if not api_key:
        api_key = prompt("AgentChat API key (ac_live_…)", password=True).strip()
    if not api_key:
        print_warning("No key entered.")
        return 2
    if len(api_key) < 20:
        print_warning(f"Key is too short ({len(api_key)} chars). Refusing to save.")
        return 2

    handle = _validate_key_remote(api_key, print_warning)
    if not handle:
        return 1

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", handle)
    if _seed_allow_all_default(save_env_value, get_env_value):
        print_info(
            "AGENTCHATME_ALLOW_ALL=true (server enforces inbox_mode; framework allowlist disabled)."
        )
    print_success(f"Key validated. You are @{handle}.")
    print_info("Restart the gateway: hermes gateway restart")
    return 0


def cli_whoami() -> int:
    """Backend for `hermes agentchat whoami`."""
    from hermes_cli.setup import (  # type: ignore[import-not-found]
        get_env_value,
        print_info,
        print_warning,
    )

    api_key = (get_env_value("AGENTCHATME_API_KEY") or "").strip()
    if not api_key:
        print_warning("No AgentChat API key configured. Run `hermes agentchat register` or `hermes agentchat login`.")
        return 2

    handle = _validate_key_remote(api_key, print_warning)
    if not handle:
        return 1
    print_info(f"You are @{handle}.")
    return 0


def cli_logout() -> int:
    """Backend for `hermes agentchat logout` — clears the key from .env."""
    from hermes_cli.setup import (  # type: ignore[import-not-found]
        print_info,
        print_success,
        prompt_yes_no,
        save_env_value,
    )

    if not prompt_yes_no(
        "Clear the AgentChat API key from ~/.hermes/.env? (You'll need to re-paste or re-register to use AgentChat again.)",
        False,
    ):
        print_info("Cancelled.")
        return 0

    save_env_value("AGENTCHATME_API_KEY", "")
    save_env_value("AGENTCHATME_HANDLE", "")
    print_success("AgentChat key cleared.")
    return 0
