"""Tests for ``agentchatme_hermes.placement`` — the identity line, the manual, the key source, the tools.

The key itself is :mod:`agentchatme_hermes.credentials` (``test_credentials.py``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agentchatme.types import Texts
from fake_keys import PREFIX, api_key

from agentchatme_hermes import placement
from agentchatme_hermes.soul_anchor import read_anchor_handle

if TYPE_CHECKING:
    from pathlib import Path

LINE = "I am @alice on AgentChat (api.agentchat.me). Peers reach me there."


class TestAnchorAndSkill:
    def test_place_anchor_writes_the_served_identity_line(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        step = placement.place_anchor(handle="@Alice", identity_line=LINE, soul_path=soul)
        assert step.status == "ok" and not step.failed
        assert read_anchor_handle(soul_path=soul) == "alice"
        assert LINE in soul.read_text(encoding="utf-8")

    def test_place_anchor_without_texts_is_skipped_not_failed(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        step = placement.place_anchor(handle="alice", identity_line=None, soul_path=soul)
        assert step.status == "skipped" and not step.failed
        assert "/v1/texts" in step.detail
        assert not soul.exists()

    def test_place_anchor_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        step = placement.place_anchor(handle="", identity_line=LINE, soul_path=tmp_path / "SOUL.md")
        assert step.failed and step.name == "anchor"

    def test_line_naming_another_agent_is_a_failure(self, tmp_path: Path) -> None:
        step = placement.place_anchor(
            handle="alice", identity_line="I am @bob on AgentChat.", soul_path=tmp_path / "SOUL.md"
        )
        assert step.failed and "@alice" in step.detail

    def test_bundled_skill_is_present_and_is_the_published_manual(self) -> None:
        step = placement.check_skill()
        assert step.status == "ok"
        text = placement.BUNDLED_SKILL_PATH.read_text(encoding="utf-8")
        assert text.startswith("---\nname: agentchat\n")
        assert "## Be reachable" in text

    def test_missing_skill_is_a_failure(self, tmp_path: Path) -> None:
        assert placement.check_skill(tmp_path / "nope.md").failed


class TestResolveIdentityLine:
    def _texts(self) -> Texts:
        return Texts.model_validate(
            {
                "version": "1",
                "manual": {"url": "https://agentchat.me/skill.md", "version": "2.1.2"},
                "wake_instruction": "w",
                "invite_prompt": "i",
                "identity_line": "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first.",
                "identity_line_template": "I am @<handle> on AgentChat.",
            }
        )

    def test_served_line_is_placed_as_served(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_load(client: Any, *, state_dir: Path, **_kw: Any) -> Texts:
            seen["state_dir"] = state_dir
            return self._texts()

        monkeypatch.setattr(placement, "load_texts", fake_load)
        line = placement.resolve_identity_line(
            api_key=api_key("k"), api_base="https://api.example.test", hermes_home=tmp_path
        )
        assert line == "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first."
        assert seen["state_dir"].parent == tmp_path / "agentchat-state"

    def test_nothing_available_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(placement, "load_texts", lambda client, *, state_dir, **_kw: None)
        assert (
            placement.resolve_identity_line(
                api_key="k", api_base="https://api.example.test", hermes_home=tmp_path
            )
            is None
        )


class TestMcpStep:
    @pytest.mark.parametrize(
        ("outcome", "status"),
        [
            ("added", "changed"),
            ("upgraded", "changed"),
            ("present", "ok"),
            ("user_managed", "skipped"),
            ("disabled", "skipped"),
        ],
    )
    def test_outcomes_map_to_statuses(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, status: str
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: outcome)
        step = placement.place_mcp("https://api.example.test")
        assert step.name == "mcp" and step.status == status

    def test_an_upgraded_entry_says_what_changed_and_when_it_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: "upgraded")
        detail = placement.place_mcp("https://api.example.test").detail
        assert "X-AgentChat-Client: hermes" in detail
        assert "gateway restart" in detail and "/reload-mcp" in detail

    def test_registration_error_is_a_failed_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*, api_base: str) -> str:
            raise placement.McpRegistrationError("no hermes")

        monkeypatch.setattr(placement, "install_entry", boom)
        step = placement.place_mcp("https://api.example.test")
        assert step.failed and "no hermes" in step.detail

    def test_place_all_runs_every_step_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import secrets_registration

        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: "added")
        monkeypatch.setattr(secrets_registration, "install_block", lambda: "added")
        steps = placement.place_identity_and_tools(
            handle="alice",
            identity_line=LINE,
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
        )
        # The key source before the tools' entry, which authenticates through it.
        assert [s.name for s in steps] == ["anchor", "skill", "secrets", "mcp"]
        assert not any(s.failed for s in steps)


class TestSecretsStep:
    @pytest.mark.parametrize(
        ("outcome", "status"),
        [
            ("added", "changed"),
            ("completed", "changed"),
            ("present", "ok"),
            ("disabled", "skipped"),
            ("user_managed", "skipped"),
        ],
    )
    def test_outcomes_map_to_statuses(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, status: str
    ) -> None:
        from agentchatme_hermes import secrets_registration

        monkeypatch.setattr(secrets_registration, "install_block", lambda: outcome)
        step = placement.place_secrets()
        assert step.name == "secrets" and step.status == status

    def test_added_says_what_it_does(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import secrets_registration

        monkeypatch.setattr(secrets_registration, "install_block", lambda: "added")
        detail = placement.place_secrets().detail
        assert "keeps AGENTCHAT_API_KEY out of every command the agent runs" in detail

    def test_a_refused_write_is_a_failed_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import secrets_registration

        def boom() -> str:
            raise placement.McpRegistrationError("managed install")

        monkeypatch.setattr(secrets_registration, "install_block", boom)
        step = placement.place_secrets()
        assert step.failed and "managed install" in step.detail

    def test_without_hermes_it_fails_with_the_block_to_add(self) -> None:
        step = placement.place_secrets()  # no Hermes in the unit suite
        if step.failed:
            assert "secrets:\n  agentchat:\n    enabled: true\n    api_key_env: AGENTCHAT_API_KEY\n" in step.detail


class TestNothingSecretLeaks:
    def test_steps_never_carry_the_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import secrets_registration

        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: "added")
        monkeypatch.setattr(secrets_registration, "install_block", lambda: "added")
        steps: list[Any] = placement.place_identity_and_tools(
            handle="alice",
            identity_line=LINE,
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
        )
        assert all(PREFIX not in s.detail for s in steps)
