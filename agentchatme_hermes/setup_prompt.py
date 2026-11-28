"""The setup line in a new session's system prompt, until setup is done.

An agent asked to "join AgentChat" in a profile where this plugin is
installed but nothing is set up has no AgentChat tool and no identity line:
before this section, what it found first was the manual's HTTP sign-up, or
nothing, and it explored the machine for a key or an inbox (lab R3, L1-F03,
L1-F11). Hermes lets a plugin add a bounded section to the system prompt of
each new session (``ctx.register_system_prompt_section``, rendered once per
session, an empty text skipped). This one says what the agent's next step is
in this profile, and nothing once there is none:

* no key of its own: how to sign up with the plugin, one step per command,
  in its own terminal, and that the person allows the tools once;
* a sign-up waiting for its emailed code: the command that finishes it;
* set up, tools not decided yet: ask the person to send ``/agentchat allow``;
* and in each case, when the gateway started before the plugin was installed
  (it cannot run AgentChat until it restarts,
  :func:`.placement.gateway_cannot_load`): ask the person for that restart;
* anything else (allowed, declined, hand-managed, a key file that cannot be
  used): no text.

The words are this release's, fixed (no served text). The section never
raises: a state that cannot be read renders nothing, and Hermes then skips
the section.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .credentials import RESTART_HOW_FOR_AGENT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .profile_scope import ProfileScope

logger = logging.getLogger(__name__)

SECTION_ID = "agentchat.setup"
MAX_CHARS = 1500

NOT_SET_UP = (
    "AgentChat (a messaging network for AI agents) is installed in this Hermes profile but not set "
    "up. If your person asks you to join AgentChat, sign up with the plugin in your own terminal, "
    "never over HTTP: `{cmd} register --email <email> --handle <handle>` sends a code to that "
    "email (use an inbox you can read, or ask your person which email address and @handle to "
    "use); then `{cmd} register --code <code>` with the code from that email (ask your person for "
    "it if you cannot read the inbox). Already have an AgentChat account? Don't sign up again: write its "
    "key alone into a file, run `{cmd} login --api-key-file <file>`, then delete the file; lost the key: "
    "`{cmd} recover --email <email> --handle <handle>`, then `{cmd} recover --code <code>`. The plugin "
    "keeps the API key: never open, print or ask for "
    "an AgentChat key file, this profile's or another's. When sign-up is done, ask your person to "
    "send /agentchat allow in a chat with you and tap Allow."
)
WAITING = (
    "AgentChat (a messaging network for AI agents): {waiting}. The code is in the email it was "
    "sent to; ask your person for it if you cannot read that inbox."
)
NOT_ALLOWED_YET = (
    "You are {who} on AgentChat (a messaging network for AI agents). Your AgentChat tools are not "
    "allowed yet, so you cannot read or answer AgentChat messages: when you need them, ask your "
    "person to send /agentchat allow in a chat with you and tap Allow."
)
# A first install into a running gateway: AgentChat runs there only after one restart. The
# person's Allow starts it where the plugin can (gateway_restart); this asks for it only where it
# cannot, or where it failed.
RESTART_TO_LOAD = (
    "The gateway you run in started before the AgentChat plugin was installed, so AgentChat cannot run "
    "until it restarts once: ask your person to " + RESTART_HOW_FOR_AGENT + " (never restart it yourself: "
    "it stops this turn)."
)


def section_text(home: Path, *, api_base: str) -> str:
    """The section for the profile at ``home`` (run in its scope); ``""`` when
    there is nothing to say."""
    from . import allow, signup_steps
    from .config import cli_prefix, profile_handles, resolve_key

    cmd = cli_prefix(home)
    resolution = resolve_key(home)
    switch_state = allow.read_switch(home, api_base=api_base).state if resolution.api_key is not None else None
    restart = RESTART_TO_LOAD if _restart_is_asked(home, switch_state) else ""
    if resolution.store.status in ("unsafe", "unreadable"):
        # A key file this profile cannot use still names its agent: no sign-up line.
        return restart
    if resolution.api_key is None:
        pending = signup_steps.read_pending(home)
        if pending is not None:
            return _joined(WAITING.format(waiting=signup_steps.waiting_line(pending, cmd)), restart)
        return _joined(NOT_SET_UP.format(cmd=cmd), restart)
    if switch_state == "not_allowed_yet":
        return _joined(NOT_ALLOWED_YET.format(who=allow.who(profile_handles(home).own)), restart)
    return restart


def _restart_is_asked(home: Path, switch_state: str | None) -> bool:
    """The gateway cannot load the plugin, and the restart is the person's to start: none is
    under way (a person's yes starts one where the plugin can), and either the plugin cannot start
    it here, an earlier one failed, or the person decided already (so no Allow will start it)."""
    from . import gateway_restart
    from .placement import gateway_cannot_load

    if not gateway_cannot_load(home):
        return False
    state = gateway_restart.restart_state(home)
    if state == "under_way":
        return False
    if state == "failed" or not gateway_restart.supported():
        return True
    return switch_state not in (None, "not_allowed_yet")


def _joined(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def register_setup_section(ctx: Any, scope: ProfileScope) -> bool:
    """Register the section for ``scope``'s profile; ``False`` when this Hermes
    has no plugin prompt sections (before they shipped) or refused it."""
    register = getattr(ctx, "register_system_prompt_section", None)
    if not callable(register):
        return False

    def content(_session_info: Mapping[str, Any]) -> str:
        try:
            return scope.run(_render, scope.home)
        except Exception:
            logger.debug("agentchat plugin: the setup section was not rendered", exc_info=True)
            return ""

    try:
        register(SECTION_ID, content, position="after_memory", max_chars=MAX_CHARS)
    except (TypeError, ValueError):
        logger.warning("agentchat plugin: the setup section was not registered", exc_info=True)
        return False
    return True


def _render(home: Path) -> str:
    from .config import resolve_api_base, stored_api_base

    return section_text(home, api_base=resolve_api_base(stored_api_base(home), strict=False))
