"""The bundled manual IS the published manual, and the identity line IS its anchor text.

Four texts carry the product; all four are written in the main line and only
placed here (``docs/decisions/2026-11-main-line.md``, *Texts*). These tests
keep the two placed copies — the skill file and the SOUL.md anchor template
— in lockstep with each other, so a release that refreshes one cannot forget
the other.
"""
from __future__ import annotations

import re

from agentchatme_hermes.placement import BUNDLED_SKILL_PATH
from agentchatme_hermes.skills import SKILL_NAME, SKILL_PATH, SKILL_SOURCE_URL
from agentchatme_hermes.soul_anchor import ANCHOR_LINE_TEMPLATE, ANCHOR_TEXT_SOURCE

_ANCHOR_SECTION = "### 1. The anchor line (identity)"


def _published_anchor_text() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    start = text.index(_ANCHOR_SECTION)
    match = re.search(r"```text\n(.*?)\n```", text[start:], re.DOTALL)
    assert match, "the manual's anchor section must carry a ```text block"
    return match.group(1)


class TestBundle:
    def test_skill_lives_where_hermes_expects(self) -> None:
        assert SKILL_PATH == BUNDLED_SKILL_PATH
        assert SKILL_PATH.parts[-3:] == ("skills", "agentchat", "SKILL.md")
        assert SKILL_NAME == "agentchat"

    def test_frontmatter_names_the_published_manual(self) -> None:
        head = SKILL_PATH.read_text(encoding="utf-8").split("---", 2)[1]
        assert "name: agentchat" in head
        assert "homepage: https://agentchat.me" in head
        assert SKILL_SOURCE_URL == "https://agentchat.me/skill.md"

    def test_manual_covers_the_tray_the_gate_and_the_hosted_tools(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        for needle in (
            "agentchat_inbox",
            "agentchat_decide",
            "https://api.agentchat.me/mcp",
            "## Be reachable",
            "Hermes",
        ):
            assert needle in text


class TestAnchorLockstep:
    def test_identity_line_is_the_manuals_anchor_text_verbatim(self) -> None:
        assert _published_anchor_text() == ANCHOR_LINE_TEMPLATE

    def test_source_is_recorded(self) -> None:
        assert "agentchat.me/skill.md" in ANCHOR_TEXT_SOURCE
        assert "Be reachable" in ANCHOR_TEXT_SOURCE
