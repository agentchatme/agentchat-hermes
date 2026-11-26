"""This release's texts ARE what the server serves.

Two texts load into every session of a profile: the manual (the
``agentchat:agentchat`` skill) and the identity line in ``SOUL.md``. By
default both are this release's copies (``live_texts`` off): the package's
``skills/agentchat/SKILL.md`` and the served identity-line template in
``texts_snapshot.json``. With ``live_texts`` on the package's manual is the
fallback for a first install with no server in reach (``test_manual.py``).
These tests keep both files what the server served, byte for byte.

``scripts/refresh_bundled_texts.py`` takes them from the server and rewrites
the three pins below (one line each; ``tests/test_refresh_texts.py``).
"""
from __future__ import annotations

import hashlib
import re

from agentchatme_hermes import texts
from agentchatme_hermes.placement import BUNDLED_SKILL_PATH
from agentchatme_hermes.skills import SKILL_NAME, SKILL_PATH, SKILL_SOURCE_URL

# The published manual this release bundles, byte for byte: the server pins the
# same pair and announces it in the served texts. A new manual changes both here.
MANUAL_VERSION = "2.4.23"
MANUAL_SHA256 = "adf13e4fa20af2997a5bfb122a1e2a29f9afe3f7c52740631940471b47c41bd2"
# texts_snapshot.json, byte for byte.
TEXTS_SNAPSHOT_SHA256 = "862ec76b5bbe6720f7f19025976658f03a5e44ccf86ba70c3c6f23ef97b8f7a7"


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


class TestSnapshot:
    def test_the_snapshot_is_pinned_byte_for_byte(self) -> None:
        assert hashlib.sha256(texts.SNAPSHOT_PATH.read_bytes()).hexdigest() == TEXTS_SNAPSHOT_SHA256

    def test_it_reads_and_announces_the_bundled_manual(self) -> None:
        snapshot = texts.parse_snapshot(texts.SNAPSHOT_PATH.read_bytes())
        assert snapshot == texts.bundled_texts()
        assert snapshot.runtime == "hermes"
        assert snapshot.manual == {
            "url": SKILL_SOURCE_URL,
            "version": MANUAL_VERSION,
            "sha256": MANUAL_SHA256,
        }

    def test_it_is_written_the_one_way(self) -> None:
        """The bytes the refresh script writes: the same texts, the same file."""
        raw = texts.SNAPSHOT_PATH.read_bytes()
        assert texts.snapshot_bytes(texts.parse_snapshot(raw)) == raw

    def test_the_template_fills_for_any_handle_through_the_kit(self) -> None:
        snapshot = texts.parse_snapshot(texts.SNAPSHOT_PATH.read_bytes())
        line = texts.fill_template(snapshot.identity_line_template, "@Relay-Bot")
        assert "@relay-bot " in line and "<handle>" not in line

    def test_its_folder_is_not_the_skills(self) -> None:
        """``skill_view`` serves every file in a skill's folder: the snapshot is not there."""
        assert texts.SNAPSHOT_PATH.parent == SKILL_PATH.parents[2]


class TestNoCopiedTexts:
    def test_package_carries_no_wake_instruction_or_identity_template(self) -> None:
        """Rule 3: every text an agent sees is the server's. The package may name
        the hosted tools and the manual; its code may not carry the sentences.
        The two this release bundles are the files above, as served."""
        package_dir = BUNDLED_SKILL_PATH.parents[2]
        for path in package_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "You were woken by AgentChat" not in source, path.name
            assert "I am @<handle> on AgentChat" not in source, path.name
            assert "Being invited is not a reason to join" not in source, path.name
