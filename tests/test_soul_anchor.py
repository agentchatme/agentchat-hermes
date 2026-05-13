"""Tests for ``agentchatme_hermes.soul_anchor``.

Covers the identity-anchor mechanism that mirrors OpenClaw's
``agents-anchor.ts``: fenced-block upsert into SOUL.md, idempotent
strip on logout, post-write handle-verify defense, and preservation of
user content outside the fenced block.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentchatme_hermes.soul_anchor import (
    ANCHOR_END,
    ANCHOR_START,
    AnchorError,
    _strip_anchor_block,
    _upsert_anchor_block,
    remove_soul_anchor,
    render_anchor_block,
    write_soul_anchor,
)

if TYPE_CHECKING:
    from pathlib import Path


# ───────────────────────── render_anchor_block ─────────────────────────


class TestRenderAnchorBlock:
    def test_handle_substituted_twice(self) -> None:
        block = render_anchor_block("alice")
        # The OpenClaw template substitutes the handle into TWO slots —
        # the bold heading reference AND the backtick share-instruction.
        assert "**@alice**" in block
        assert "`@alice`" in block

    def test_starts_with_anchor_start(self) -> None:
        block = render_anchor_block("alice")
        assert block.startswith(ANCHOR_START)

    def test_ends_with_anchor_end(self) -> None:
        block = render_anchor_block("alice")
        assert block.endswith(ANCHOR_END)

    def test_hermes_skill_idiom_present(self) -> None:
        """Hermes-specific adaptation: the skill-load command is
        included as a verbatim shell command the agent can copy."""
        block = render_anchor_block("alice")
        assert "skill_view agentchat:agentchat" in block

    def test_strips_at_prefix(self) -> None:
        block = render_anchor_block("@alice")
        assert "**@alice**" in block
        assert "**@@alice**" not in block

    def test_strips_whitespace(self) -> None:
        block = render_anchor_block("  alice  ")
        assert "**@alice**" in block

    def test_empty_handle_raises(self) -> None:
        with pytest.raises(AnchorError, match="empty"):
            render_anchor_block("")

    def test_whitespace_only_handle_raises(self) -> None:
        with pytest.raises(AnchorError, match="empty"):
            render_anchor_block("   ")

    def test_at_only_handle_raises(self) -> None:
        with pytest.raises(AnchorError, match="empty"):
            render_anchor_block("@")


# ───────────────────────── write_soul_anchor ─────────────────────────


class TestWriteSoulAnchor:
    def test_creates_file_when_absent(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        assert not soul.exists()

        result_path = write_soul_anchor("alice", soul_path=soul)

        assert result_path == soul
        assert soul.exists()
        content = soul.read_text(encoding="utf-8")
        assert "**@alice**" in content
        assert ANCHOR_START in content
        assert ANCHOR_END in content

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        soul = tmp_path / "nested" / "subdir" / "SOUL.md"
        write_soul_anchor("alice", soul_path=soul)
        assert soul.exists()

    def test_preserves_existing_user_content(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text(
            "# My agent\n\nYou are a friendly research assistant.\n",
            encoding="utf-8",
        )

        write_soul_anchor("alice", soul_path=soul)

        content = soul.read_text(encoding="utf-8")
        assert "# My agent" in content
        assert "You are a friendly research assistant." in content
        assert "**@alice**" in content

    def test_idempotent_same_handle(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text("# Existing\n\nThe agent is curious.\n", encoding="utf-8")

        write_soul_anchor("alice", soul_path=soul)
        first = soul.read_text(encoding="utf-8")

        write_soul_anchor("alice", soul_path=soul)
        second = soul.read_text(encoding="utf-8")

        assert first == second
        # Exactly one start marker and one end marker (no duplication)
        assert second.count(ANCHOR_START) == 1
        assert second.count(ANCHOR_END) == 1

    def test_idempotent_handle_change(self, tmp_path: Path) -> None:
        """Re-running with a different handle replaces in place."""
        soul = tmp_path / "SOUL.md"

        write_soul_anchor("alice", soul_path=soul)
        write_soul_anchor("bob", soul_path=soul)

        content = soul.read_text(encoding="utf-8")
        assert "**@bob**" in content
        assert "**@alice**" not in content
        # Still exactly one block, not two competing ones
        assert content.count(ANCHOR_START) == 1
        assert content.count(ANCHOR_END) == 1

    def test_no_blank_line_accumulation(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text("Existing.\n", encoding="utf-8")

        for _ in range(5):
            write_soul_anchor("alice", soul_path=soul)

        content = soul.read_text(encoding="utf-8")
        # Re-running 5 times should not leave 4 extra blank lines around
        # the block. We assert no runs of 3+ consecutive newlines.
        assert "\n\n\n" not in content

    def test_returns_target_path(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        result = write_soul_anchor("alice", soul_path=soul)
        assert result == soul

    def test_empty_handle_raises_before_touching_file(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        with pytest.raises(AnchorError):
            write_soul_anchor("", soul_path=soul)
        assert not soul.exists()


# ───────────────────────── remove_soul_anchor ─────────────────────────


class TestRemoveSoulAnchor:
    def test_strips_block_preserves_other_content(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text(
            "# My agent\n\nYou are friendly.\n",
            encoding="utf-8",
        )
        write_soul_anchor("alice", soul_path=soul)
        assert "**@alice**" in soul.read_text(encoding="utf-8")

        removed = remove_soul_anchor(soul_path=soul)

        assert removed is True
        content = soul.read_text(encoding="utf-8")
        assert "**@alice**" not in content
        assert ANCHOR_START not in content
        assert ANCHOR_END not in content
        assert "# My agent" in content
        assert "You are friendly." in content

    def test_returns_false_when_file_absent(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        assert remove_soul_anchor(soul_path=soul) is False

    def test_returns_false_when_block_absent(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text("# Just user content, no markers.\n", encoding="utf-8")
        assert remove_soul_anchor(soul_path=soul) is False
        # Untouched
        assert soul.read_text(encoding="utf-8") == "# Just user content, no markers.\n"

    def test_idempotent(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        soul.write_text("User content.\n", encoding="utf-8")
        write_soul_anchor("alice", soul_path=soul)

        first = remove_soul_anchor(soul_path=soul)
        second = remove_soul_anchor(soul_path=soul)

        assert first is True
        assert second is False
        assert "User content." in soul.read_text(encoding="utf-8")

    def test_strips_only_block_leaves_empty_file(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        write_soul_anchor("alice", soul_path=soul)

        removed = remove_soul_anchor(soul_path=soul)

        assert removed is True
        # File still exists but is empty — we don't delete the file
        # because the user might have other tooling that expects it to
        # be present.
        assert soul.exists()
        assert soul.read_text(encoding="utf-8") == ""


# ───────────────────────── _upsert_anchor_block (pure helper) ─────────────────────────


class TestUpsertAnchorBlock:
    def _block(self, handle: str = "alice") -> str:
        return render_anchor_block(handle)

    def test_appends_to_empty_string(self) -> None:
        block = self._block()
        result = _upsert_anchor_block("", block)
        assert result == block + "\n"

    def test_appends_to_existing(self) -> None:
        block = self._block()
        result = _upsert_anchor_block("Pre-existing content.\n", block)
        assert result.startswith("Pre-existing content.")
        assert block in result

    def test_replaces_in_place(self) -> None:
        first = self._block("alice")
        second = self._block("bob")
        intermediate = _upsert_anchor_block("Header.\n", first)
        result = _upsert_anchor_block(intermediate, second)
        assert "**@bob**" in result
        assert "**@alice**" not in result
        assert "Header." in result

    def test_separates_with_blank_line(self) -> None:
        block = self._block()
        result = _upsert_anchor_block("Top content.\n", block)
        # One blank line between user content and our block
        assert "Top content.\n\n" + ANCHOR_START in result

    def test_does_not_accumulate_trailing_newlines(self) -> None:
        block = self._block()
        result = _upsert_anchor_block("Stuff.\n\n\n\n", block)
        assert "\n\n\n" not in result


# ───────────────────────── _strip_anchor_block (pure helper) ─────────────────────────


class TestStripAnchorBlock:
    def _block(self) -> str:
        return render_anchor_block("alice")

    def test_no_op_on_string_without_markers(self) -> None:
        assert _strip_anchor_block("just text") == "just text"

    def test_strips_only_block(self) -> None:
        block = self._block()
        # File contains only our block
        full = block + "\n"
        assert _strip_anchor_block(full) == ""

    def test_strips_leading_block(self) -> None:
        block = self._block()
        full = block + "\n\nUser content below.\n"
        result = _strip_anchor_block(full)
        assert ANCHOR_START not in result
        assert "User content below." in result

    def test_strips_trailing_block(self) -> None:
        block = self._block()
        full = "User content above.\n\n" + block + "\n"
        result = _strip_anchor_block(full)
        assert ANCHOR_START not in result
        assert "User content above." in result

    def test_strips_sandwiched_block(self) -> None:
        block = self._block()
        full = "Above.\n\n" + block + "\n\nBelow.\n"
        result = _strip_anchor_block(full)
        assert ANCHOR_START not in result
        assert "Above." in result
        assert "Below." in result

    def test_no_blank_line_accumulation_after_strip(self) -> None:
        block = self._block()
        full = "Above.\n\n" + block + "\n\nBelow.\n"
        result = _strip_anchor_block(full)
        # No 3+ consecutive newlines
        assert "\n\n\n" not in result


# ───────────────────────── handle-verify defense ─────────────────────────


class TestVerifyDefense:
    def test_substitution_works_in_normal_path(self, tmp_path: Path) -> None:
        """Happy path: real handle lands in file, no exception."""
        soul = tmp_path / "SOUL.md"
        write_soul_anchor("test-agent-42", soul_path=soul)
        assert "@test-agent-42" in soul.read_text(encoding="utf-8")

    def test_verify_throws_if_handle_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If a future refactor of render_anchor_block drops the handle
        substitution, the post-write verify should fail loud.

        We simulate by monkey-patching render_anchor_block to return a
        block that doesn't contain the handle.
        """
        from agentchatme_hermes import soul_anchor

        def broken_render(handle: str) -> str:
            _ = handle  # deliberately ignored — simulating a buggy refactor
            return (
                f"{soul_anchor.ANCHOR_START}\n"
                "## On AgentChat\n"
                "You are an agent. (handle dropped by a buggy refactor)\n"
                f"{soul_anchor.ANCHOR_END}"
            )

        monkeypatch.setattr(soul_anchor, "render_anchor_block", broken_render)

        soul = tmp_path / "SOUL.md"
        with pytest.raises(AnchorError, match="did not land"):
            soul_anchor.write_soul_anchor("alice", soul_path=soul)
