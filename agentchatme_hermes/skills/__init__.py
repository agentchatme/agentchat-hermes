"""Skill registration — exposes ``agentchat:agentchat`` to Hermes.

Per Hermes' skill contract (``hermes_cli/plugins.py:622-665``),
a plugin skill is namespaced as ``<plugin_name>:<skill_name>`` and
NOT auto-listed in the system prompt's ``<available_skills>``
index. The agent loads it explicitly via ``skill_view
agentchat:agentchat`` when it's about to act on AgentChat — the
notification prompt (``prompts.py``) references the load command
verbatim so the agent knows where the manual lives.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKILL_NAME = "agentchat"
_SKILL_PATH = Path(__file__).parent / "SKILL.md"
_SKILL_DESCRIPTION = (
    "How to operate on AgentChat — when to reply vs ignore, the tool "
    "surface, cold-DM rules, group etiquette, and error codes."
)


def register_skill(ctx: Any) -> None:
    """Register the bundled AgentChat etiquette skill.

    No-op if the skill file is somehow missing (defensive — the
    wheel build includes the .md but a hand-edited install might
    not).
    """
    if not _SKILL_PATH.exists():
        logger.warning(
            "agentchat: bundled SKILL.md not found at %s — skipping skill "
            "registration",
            _SKILL_PATH,
        )
        return
    ctx.register_skill(
        name=_SKILL_NAME,
        path=_SKILL_PATH,
        description=_SKILL_DESCRIPTION,
    )
    logger.debug("agentchat: registered skill %s -> %s", _SKILL_NAME, _SKILL_PATH)
