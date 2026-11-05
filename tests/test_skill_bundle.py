"""The bundled manual IS the published manual.

The texts an agent reads are served (``GET /v1/texts``) and never bundled;
the one placed copy left in this package is the manual itself, registered
as the ``agentchat:agentchat`` skill. These tests keep that copy where
Hermes expects it and recognisably the published document.
"""
from __future__ import annotations

from agentchatme_hermes.placement import BUNDLED_SKILL_PATH
from agentchatme_hermes.skills import SKILL_NAME, SKILL_PATH, SKILL_SOURCE_URL


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


class TestNoCopiedTexts:
    def test_package_carries_no_wake_instruction_or_identity_template(self) -> None:
        """Rule 3: every text an agent sees is served. The package may name
        the hosted tools and the manual; it may not carry the sentences."""
        package_dir = BUNDLED_SKILL_PATH.parents[2]
        for path in package_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "You were woken by AgentChat" not in source, path.name
            assert "I am @<handle> on AgentChat" not in source, path.name
            assert "Being invited is not a reason to join" not in source, path.name
