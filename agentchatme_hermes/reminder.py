"""The reminder while the person has not decided: once a day, in a turn the person started.

An agent that joins AgentChat cannot use it until its person allows the tools (``/agentchat
allow``), and nobody writes to an agent that has never written to anyone: a reminder tied to
incoming messages would never fire. People forget, are busy, or were away when the agent joined.
So the plugin asks the agent to remind its person, in the next turn the person starts and at most
once a day, until the person decides: Allow (it stops: the tools work) or Don't allow (it stops:
the decision is respected). Where the gateway still needs the one restart a first install needs and
no restart is under way (:mod:`.gateway_restart`: Windows, a failed one, or the person allowed
before the install), the reminder asks the person for it. The reminder itself never starts a
restart: only a person's yes does. It is not given where the person cannot act on it (a
package-managed or hand-written tools entry, a yes that could not be written).

Mechanism: Hermes' ``pre_llm_call`` hook (public, in every Hermes the plugin accepts): once per
turn, before the model runs, a returned string is added to the turn's user message. The hook adds
the reminder only to a turn started on a chat platform a person uses (an allowlist: a new kind of
turn gets no reminder rather than using up the day), never to a delegated subagent's. The day is
checked first, from one small file, before anything else is read; it is counted per profile, in
``<profile home>/agentchat/reminder.json``, claimed under a lock so two turns at once remind once.
The words are this release's, fixed. The hook never raises: Hermes skips a failing hook.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .credentials import RESTART_HOW_FOR_AGENT

if TYPE_CHECKING:
    from pathlib import Path

    from .profile_scope import ProfileScope

logger = logging.getLogger(__name__)

RECORD_FILENAME = "reminder.json"
LOCK_FILENAME = "reminder.lock"
REMIND_EVERY_S = 24 * 3600.0
# Where a person talks to the agent: the classic CLI and Hermes' local windows (``tui``, ``desktop``,
# ``local``), the chat platforms of gateway/config.py ``Platform`` and the person-chat platform
# plugins (plugins/platforms/). Not: cron, subagent, curator, gateway, the API server, webhooks, the
# relay, AgentChat's own turns, nor the agent and community relays (a2a, raft, buzz), where the
# speaker may not be the agent's person.
PERSON_PLATFORMS = frozenset({
    "cli", "local", "tui", "desktop",
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "mattermost", "matrix",
    "email", "sms", "dingtalk", "feishu", "wecom", "weixin", "bluebubbles", "qqbot",
    "google_chat", "irc", "line", "simplex", "teams", "photon", "yuanbao", "ntfy",
})

REMIND_ALLOW = (
    "AgentChat note for this turn (from the AgentChat plugin, at most once a day): before anything else, "
    "tell your person in one short line that you are {who} on AgentChat (messaging between AI agents) but "
    "cannot use it until they send /agentchat allow in a chat with you and tap Allow; tapping Don't allow "
    "stops this reminder. Then go on with their message as usual."
)
REMIND_RESTART = (
    "AgentChat note for this turn (from the AgentChat plugin, at most once a day): before anything else, "
    "tell your person in one short line that you are {who} on AgentChat but it cannot run until Hermes' "
    "gateway restarts once (it started before the plugin was installed): ask them to "
    + RESTART_HOW_FOR_AGENT
    + ". Never restart it yourself. Then go on with their message as usual."
)


def reminder_text(home: Path, *, api_base: str) -> str | None:
    """The reminder this profile needs now (run in its scope), or ``None``: not set up (the setup
    line in the system prompt covers sign-up), allowed and running or restarting, declined, or a
    step the person cannot take here. Reads only: it never starts a restart."""
    from . import allow, gateway_restart
    from .config import profile_handles, resolve_key
    from .mcp_registration import config_is_managed
    from .placement import gateway_cannot_load

    if resolve_key(home).api_key is None:
        return None
    switch = allow.read_switch(home, api_base=api_base)
    if switch.gave_up:
        return None
    who = allow.who(profile_handles(home).own)
    if switch.state == "not_allowed_yet":
        entry = switch.allowance.entry if switch.allowance is not None else None
        if config_is_managed() or entry == "user_managed":
            return None  # /agentchat allow cannot ask here: the operator writes the entry
        return REMIND_ALLOW.format(who=who)
    if switch.by_tier and gateway_cannot_load(home) and gateway_restart.restart_state(home) != "under_way":
        return REMIND_RESTART.format(who=who)
    return None


def register_reminder(ctx: Any, scope: ProfileScope) -> bool:
    """Register the ``pre_llm_call`` hook for ``scope``'s profile; ``False`` when this Hermes has
    no ``register_hook`` or refused it."""
    register = getattr(ctx, "register_hook", None)
    if not callable(register):
        return False

    def hook(**kwargs: Any) -> str | None:
        try:
            if str(kwargs.get("platform") or "") not in PERSON_PLATFORMS or kwargs.get("parent_session_id"):
                return None
            return scope.run(_due, scope.home)
        except Exception:
            logger.debug("agentchat plugin: the reminder was not prepared", exc_info=True)
            return None

    try:
        register("pre_llm_call", hook)
    except (TypeError, ValueError):
        logger.warning("agentchat plugin: the reminder hook was not registered", exc_info=True)
        return False
    return True


def _due(home: Path) -> str | None:
    from .config import resolve_api_base, stored_api_base

    if _reminded_recently(home):
        return None  # the cheap check first: one small file, nothing else read
    text = reminder_text(home, api_base=resolve_api_base(stored_api_base(home), strict=False))
    if text is None or not _claim(home):
        return None
    return text


def _reminded_recently(home: Path, *, now: float | None = None) -> bool:
    from .key_store import store_dir
    from .state import read_json

    record: Any = read_json(store_dir(home) / RECORD_FILENAME)
    last = record.get("at") if isinstance(record, dict) else None
    stamp = time.time() if now is None else now
    return isinstance(last, (int, float)) and 0 <= stamp - last < REMIND_EVERY_S


def _claim(home: Path, *, now: float | None = None) -> bool:
    """Take today's reminder for this profile: ``True`` once per :data:`REMIND_EVERY_S`."""
    from .key_store import store_dir
    from .leader_lock import held_lock
    from .state import read_json, write_json_atomic

    directory = store_dir(home)
    stamp = time.time() if now is None else now
    with held_lock(directory / LOCK_FILENAME, timeout_s=0.5) as lock:
        if lock == "busy":
            return False  # another turn is claiming it right now
        record: Any = read_json(directory / RECORD_FILENAME)
        last = record.get("at") if isinstance(record, dict) else None
        if isinstance(last, (int, float)) and 0 <= stamp - last < REMIND_EVERY_S:
            return False
        try:
            write_json_atomic(directory / RECORD_FILENAME, {"at": stamp})
        except OSError:
            return False  # nothing recorded: never remind on every turn
        return True
