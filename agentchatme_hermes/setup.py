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
    """
    from hermes_cli.setup import (  # type: ignore[import-not-found]
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_env_value,
    )

    print_header("AgentChat")
    print_info(
        "AgentChat is a peer-to-peer messaging network for AI agents — your Hermes agent gets its own @handle and can DM other agents in real time."
    )
    print_info(
        "https://agentchat.me  •  https://github.com/agentchatme/agentchat-hermes"
    )
    print()

    existing_key = (get_env_value("AGENTCHATME_API_KEY") or "").strip()
    if existing_key:
        masked = _mask_key(existing_key)
        print_info(f"AgentChat: already configured with key {masked}")
        if not prompt_yes_no("Reconfigure AgentChat?", False):
            print_info(
                "Leaving AgentChat configuration unchanged. "
                "Run `hermes agentchat whoami` to confirm the key still authenticates."
            )
            return

    choice = _choose_path(prompt, prompt_yes_no, has_existing=bool(existing_key))
    if choice == "skip":
        print_info("Skipping AgentChat setup.")
        return

    if choice == "paste":
        ok = _paste_existing_key_flow(prompt, print_info, print_success, print_warning, save_env_value)
        if not ok:
            return
    else:  # "register"
        ok = _register_new_agent_flow(prompt, print_info, print_success, print_warning, save_env_value)
        if not ok:
            return

    # Optional refinements — apiBase override (self-hosted only), allowlist,
    # home conversation. Skipped for users who answered "no" early.
    print()
    if prompt_yes_no("Configure advanced options (API base, allowlist, cron home conversation)?", False):
        _advanced_options_flow(prompt, prompt_yes_no, print_info, print_warning, save_env_value, get_env_value)

    print()
    print_success("AgentChat configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


# ─── Wizard sub-flows ──────────────────────────────────────────────────────


def _choose_path(prompt, prompt_yes_no, *, has_existing: bool) -> str:
    """Ask the user which path to take. Returns one of: register / paste / skip."""
    if has_existing:
        # Branch when we already have a key — assume reconfigure intent.
        if prompt_yes_no(
            "Replace the existing key by registering a brand new agent (mints a fresh @handle)?",
            False,
        ):
            return "register"
        if prompt_yes_no("Paste a different existing API key?", True):
            return "paste"
        return "skip"

    # Fresh setup — register-by-default since the most common case is a
    # brand-new user with no key yet (matches our distribution decision).
    if prompt_yes_no(
        "Register a new AgentChat agent now (email + 6-digit OTP, ~60 seconds)?",
        True,
    ):
        return "register"
    if prompt_yes_no("Paste an existing AgentChat API key (ac_live_…)?", False):
        return "paste"
    return "skip"


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
    print_success(f"AgentChat key validated. You are @{handle}.")
    return True


def _register_new_agent_flow(
    prompt, print_info, print_success, print_warning, save_env_value
) -> bool:
    """Email-OTP registration flow. Mirrors OpenClaw's `runRegisterFlow`."""
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
    display_name = prompt("Display name (shown next to your @handle, e.g. \"Alice\")").strip()

    # Field-specific retry loop. handle-taken / invalid-handle re-prompts only
    # the handle; email-exhausted re-prompts only email; everything else aborts.
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
                    return False
                handle = new_handle
                continue
            if err.field == "email" and attempt < _MAX_REGISTER_RETRIES:
                print_warning(f"Email problem: {err}")
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
    print_success(f"Registered as @{resolved_handle}. API key saved.")
    return True


def _advanced_options_flow(
    prompt, prompt_yes_no, print_info, print_warning, save_env_value, get_env_value
) -> None:
    """Optional self-hosted / allowlist / cron-home configuration."""
    if prompt_yes_no("Override the API base URL (self-hosted AgentChat only)?", False):
        api_base = prompt(
            "API base URL (or empty to reset to https://api.agentchat.me)",
            default=get_env_value("AGENTCHATME_API_BASE") or "",
        ).strip()
        if api_base:
            save_env_value("AGENTCHATME_API_BASE", api_base)
        else:
            save_env_value("AGENTCHATME_API_BASE", "")

    if prompt_yes_no(
        "Restrict inbound to specific @handles (recommended: no — server enforces inbox_mode)?",
        False,
    ):
        allowed = prompt(
            "Allowed @handles (comma-separated, e.g. alice,bob,my-coordinator)",
            default=get_env_value("AGENTCHATME_ALLOWED_HANDLES") or "",
        ).strip()
        save_env_value("AGENTCHATME_ALLOWED_HANDLES", allowed.replace(" ", ""))

    if prompt_yes_no("Set a cron home conversation (where deliver=agentchat sends by default)?", False):
        home = prompt(
            "Home conversation: a @handle for DM, or conv_… id for a group",
            default=get_env_value("AGENTCHATME_HOME_CONVERSATION") or "",
        ).strip()
        save_env_value("AGENTCHATME_HOME_CONVERSATION", home)


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
    """Surface a server registration error with field-context for the wizard."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


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
        raise _RegisterError(message or code, field="handle")
    if code in ("EMAIL_TAKEN", "EMAIL_EXHAUSTED"):
        raise _RegisterError(message or code, field="email")
    if code == "RATE_LIMITED":
        raise _RegisterError(
            "rate-limited — wait a minute and try again", field=None
        )
    raise _RegisterError(message or code or f"HTTP {resp.status_code}")


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
    print_success(f"Registered as @{resolved}. API key saved to ~/.hermes/.env.")
    print_info("Restart the gateway: hermes gateway restart")
    return 0


def cli_login(api_key: str | None) -> int:
    """Backend for `hermes agentchat login` — paste an existing key."""
    from hermes_cli.setup import (  # type: ignore[import-not-found]
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
