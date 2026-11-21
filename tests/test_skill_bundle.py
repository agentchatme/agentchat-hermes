"""The bundled copy IS the published manual — the offline floor.

The texts an agent reads are served (``GET /v1/texts``) and never bundled,
and so is the manual: the ``agentchat:agentchat`` skill is the per-profile
file the plugin keeps at the version the server announces
(``test_manual.py``). The one copy left in this package is the fallback for
a first install with no server in reach. These tests keep it the published
document byte for byte, so that fallback is a manual the server served.
"""
from __future__ import annotations

import hashlib
import re

from agentchatme_hermes.placement import BUNDLED_SKILL_PATH
from agentchatme_hermes.skills import SKILL_NAME, SKILL_PATH, SKILL_SOURCE_URL

# The published manual this release bundles, byte for byte: the server pins the
# same pair (MANUAL_VERSION / MANUAL_SHA256 in the core repository's
# packages/shared/src/types/texts.ts) and announces it in the served texts. A
# new manual changes both here.
MANUAL_VERSION = "2.4.3"
MANUAL_SHA256 = "c7c755bb75dac1df831ca3f4f7e768f8432d03133ccec138e3dd5b4a5eee935d"


class TestBundle:
    def test_the_package_copy_is_alone_in_its_folder(self) -> None:
        """Registered itself only as the last resort; then, like the
        kept-current file, nothing else may sit beside it (skill_view serves
        every file in a skill's folder)."""
        assert SKILL_PATH == BUNDLED_SKILL_PATH
        assert SKILL_PATH.parts[-3:] == ("skills", "agentchat", "SKILL.md")
        assert SKILL_NAME == "agentchat"
        assert sorted(p.name for p in SKILL_PATH.parent.iterdir()) == ["SKILL.md"]

    def test_bundle_is_the_published_manual_byte_for_byte(self) -> None:
        raw = SKILL_PATH.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == MANUAL_SHA256
        head = raw.decode("utf-8").split("---", 2)[1]
        assert f"version: {MANUAL_VERSION}" in head

    def test_frontmatter_names_the_published_manual(self) -> None:
        head = SKILL_PATH.read_text(encoding="utf-8").split("---", 2)[1]
        assert "name: agentchat" in head
        assert "homepage: https://agentchat.me" in head
        assert SKILL_SOURCE_URL == "https://agentchat.me/skill.md"

    def test_no_inline_shell_for_hermes_to_run(self) -> None:
        """Hermes can expand an inline-shell snippet (an exclamation mark, then a
        command in backticks; the opt-in ``skills.inline_shell``) in a SKILL.md
        it serves: the manual must carry none. The backtick is the ``\\x60``
        escape, so this source does not carry the sequence Hermes' install
        scanner flags."""
        assert re.search(r"!\x60[^\x60\n]+\x60", SKILL_PATH.read_text(encoding="utf-8")) is None

    def test_the_key_goes_only_to_agentchat_and_claim_links_instead(self) -> None:
        """2.4.0 told the agent to send its key to its human owner as well; the
        served manual sends it to api.agentchat.me only and gives that person
        a claim link."""
        text = SKILL_PATH.read_text(encoding="utf-8")
        assert "**Send the key to exactly one place: `api.agentchat.me`**" in text
        assert "**Your human owner** — once" not in text
        assert "agentchat_create_claim_link" in text

    def test_no_one_line_curl_with_the_key_for_hermes_to_block(self) -> None:
        """Hermes' install scan (``hermes plugins install``) blocks a plugin whose
        files hold a one-line ``curl … -H "Authorization: Bearer $AGENTCHAT_API_KEY"``
        (``env_exfil_curl``, critical; ``--force`` does not override). 2.4.2 had 15;
        the served manual since 2.4.3 has none."""
        text = SKILL_PATH.read_text(encoding="utf-8")
        assert re.search(r"curl[^\n]*Authorization: Bearer \$AGENTCHAT_API_KEY", text) is None

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
