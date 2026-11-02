"""Skill registration — exposes the published manual as ``agentchat:agentchat``.

The bundled ``skills/agentchat/SKILL.md`` is ``https://agentchat.me/skill.md``
verbatim — the one manual written in the main line for every runtime
(``docs/decisions/2026-11-main-line.md``, *Texts*). This plugin places it
where Hermes keeps skills and nothing more; a release refreshes the copy.

Per Hermes' skill contract (``hermes_cli/plugins.py``), a plugin skill is
namespaced ``<plugin_name>:<skill_name>`` and NOT auto-listed in the system
prompt's ``<available_skills>`` index. The agent loads it explicitly via
``skill_view agentchat:agentchat``; the identity line in SOUL.md and the
turn prompt say where it lives.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SKILL_NAME = "agentchat"
SKILL_PATH = Path(__file__).parent / "agentchat" / "SKILL.md"
SKILL_SOURCE_URL = "https://agentchat.me/skill.md"
SKILL_DESCRIPTION = (
    "The AgentChat manual (https://agentchat.me/skill.md, verbatim): the tray and the "
    "gate, hosted tools, the receive loop, cold-outreach rules, groups, contacts, "
    "error codes, account states."
)


def register_skill(ctx: Any) -> None:
    """Register the bundled AgentChat manual.

    No-op if the skill file is somehow missing (defensive — the wheel build
    includes the .md but a hand-edited install might not).
    """
    if not SKILL_PATH.exists():
        logger.warning(
            "agentchat: bundled SKILL.md not found at %s — skipping skill registration",
            SKILL_PATH,
        )
        return
    ctx.register_skill(
        name=SKILL_NAME,
        path=SKILL_PATH,
        description=SKILL_DESCRIPTION,
    )
    logger.debug("agentchat: registered skill %s -> %s", SKILL_NAME, SKILL_PATH)
