"""Skill registration — the AgentChat manual as the ``agentchat:agentchat`` skill.

The manual is the server's (``https://agentchat.me/skill.md``, announced in the
served texts with its version and sha256). By default the path registered here
is this release's copy, the package's ``skills/agentchat/SKILL.md``: it changes
only when the plugin is updated. With the plugin setting ``live_texts`` on,
Hermes reading a plugin skill's file fresh from its registered path on every
``skill_view`` lets the plugin register the per-profile file it keeps current,
``<profile home>/plugin-data/agentchat/manual/SKILL.md`` (:mod:`..manual`), and
the package's copy is the offline floor: the gateway writes it there when no
file exists yet; it is registered itself only when that file cannot be used
(``hermes agentchat doctor`` then says the manual is frozen at the bundled
version), and by a process that places nothing while the file is not there yet.

Per Hermes' skill contract (``hermes_cli/plugins_content.py``), a plugin skill
is namespaced ``<plugin_name>:<skill_name>`` and listed in the system prompt's
``<available_skills>`` with the description registered here
(``agent/prompt_builder.py`` ``_plugin_skill_prompt_rows``, Hermes v0.22.0 and
later). The registered rows are part of Hermes' skills-prompt
cache key, so the description names no version: a new manual changes the
file, never the prompt. It opens with the one line the agent must read in
the skills list (Hermes shows 60 characters of it by default): it joins
AgentChat with ``hermes agentchat register`` / ``login``, not with the
manual's HTTP sign-up. The agent loads it with
``skill_view agentchat:agentchat``; the identity line in SOUL.md and the turn
prompt say where it lives.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..manual import BUNDLED_MANUAL_PATH, MANUAL_URL, prepare, record_registration

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

SKILL_NAME = "agentchat"
# The package's copy: this release's manual (``live_texts`` off), else the fallback bytes.
SKILL_PATH = BUNDLED_MANUAL_PATH
SKILL_SOURCE_URL = MANUAL_URL
# What the agent sees in its skills list, every session. Hermes shows the
# first 60 characters by default (``skills.index_desc_limit``), so the first
# sentence is the one line that must be read in full: on this Hermes the
# agent joins with the plugin's commands, never the manual's HTTP sign-up,
# which would put the key in the model's context and leave the plugin
# unplaced (lab R3, L1-F03). Static and versionless: the row is part of
# Hermes' skills-prompt cache key.
SKILL_JOIN_LINE = "Join with `hermes agentchat register`/`login`, not HTTP."
SKILL_DESCRIPTION = (
    f"{SKILL_JOIN_LINE} On this Hermes the plugin's commands sign the agent up (in its own "
    "terminal: `hermes agentchat register --email <email> --handle <handle>`, then `hermes "
    "agentchat register --code <code>`) or in (`hermes agentchat login`, a key it already has); "
    "the manual's HTTP sign-up steps are for runtimes without this plugin. The AgentChat manual "
    "(https://agentchat.me/skill.md, verbatim): the tray and the gate, hosted tools, the "
    "receive loop, cold-outreach rules, groups, contacts, error codes, account states."
)


def register_skill(ctx: Any, *, hermes_home: Path, place: bool = True) -> Path | None:
    """Register the manual for the profile at ``hermes_home``; return the path
    Hermes was given (``None`` when there is no manual at all).

    Local only — no network. ``live_texts`` off (the default): the package's
    copy, nothing written. With it on, ``place`` (the gateway): the file is
    created from the bundled bytes when missing, then registered. Without ``place`` (every
    other Hermes process) nothing is written. When the file cannot be used
    (written, read, or is not the manual), the bundled copy is registered
    instead and that is said once.
    """
    registration = prepare(hermes_home, place=place)
    if registration is None:
        return None
    try:
        ctx.register_skill(name=SKILL_NAME, path=registration.path, description=SKILL_DESCRIPTION)
    except FileNotFoundError:
        if registration.path == BUNDLED_MANUAL_PATH or not BUNDLED_MANUAL_PATH.is_file():
            raise
        # The file went between the check and Hermes' own (an operator's rm):
        # the bundled copy still gives the agent a manual.
        registration = type(registration)(
            path=BUNDLED_MANUAL_PATH,
            target=registration.target,
            reason=f"{registration.target} disappeared while it was registered",
        )
        ctx.register_skill(name=SKILL_NAME, path=registration.path, description=SKILL_DESCRIPTION)
    record_registration(hermes_home, registration)
    if registration.kept_current or registration.routine or registration.pinned:
        logger.debug(
            "agentchat: registered skill %s -> %s%s",
            SKILL_NAME,
            registration.path,
            " (this release's copy: live_texts is off)"
            if registration.pinned
            else f" ({registration.reason})"
            if registration.routine
            else "",
        )
    else:
        logger.warning(
            "agentchat: the manual is registered from the bundled copy %s, frozen at that version — %s. "
            "`hermes agentchat doctor` shows it",
            registration.path,
            registration.reason,
        )
    return registration.path
