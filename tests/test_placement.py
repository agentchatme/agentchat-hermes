"""Tests for ``agentchatme_hermes.placement`` — the key, the identity line, the manual, the tools."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agentchatme.types import Texts

from agentchatme_hermes import placement
from agentchatme_hermes.config import ENV_API_KEY, ENV_API_KEY_LEGACY
from agentchatme_hermes.soul_anchor import read_anchor_handle
from agentchatme_hermes.texts import LoadedTexts

if TYPE_CHECKING:
    from pathlib import Path

LINE = "I am @alice on AgentChat (api.agentchat.me). Peers reach me there."


class _Env:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)
        self.writes: list[tuple[str, str]] = []

    def save(self, key: str, value: str) -> None:
        self.writes.append((key, value))
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


class TestKey:
    def test_persist_writes_the_canonical_name_only_when_no_legacy(self) -> None:
        env = _Env()
        written = placement.persist_api_key(env.save, api_key="ac_live_1", get_env_value=env.get)
        assert written == [ENV_API_KEY]
        assert env.values == {ENV_API_KEY: "ac_live_1"}

    def test_persist_keeps_a_legacy_line_in_step(self) -> None:
        env = _Env(**{ENV_API_KEY_LEGACY: "ac_live_old"})
        written = placement.persist_api_key(env.save, api_key="ac_live_new", get_env_value=env.get)
        assert written == [ENV_API_KEY, ENV_API_KEY_LEGACY]
        assert env.values[ENV_API_KEY] == "ac_live_new"
        assert env.values[ENV_API_KEY_LEGACY] == "ac_live_new"

    def test_persist_without_reader_writes_canonical_only(self) -> None:
        env = _Env()
        assert placement.persist_api_key(env.save, api_key="k") == [ENV_API_KEY]

    def test_clear_blanks_both_names(self) -> None:
        env = _Env()
        placement.clear_api_key(env.save)
        assert env.values == {ENV_API_KEY: "", ENV_API_KEY_LEGACY: ""}

    def test_migrate_mirrors_legacy_once(self) -> None:
        env = _Env(**{ENV_API_KEY_LEGACY: "ac_live_old"})
        assert placement.migrate_legacy_key(env.save, env.get) is True
        assert env.values[ENV_API_KEY] == "ac_live_old"
        assert placement.migrate_legacy_key(env.save, env.get) is False, "canonical now present"

    def test_migrate_is_a_noop_without_a_legacy_value(self) -> None:
        env = _Env()
        assert placement.migrate_legacy_key(env.save, env.get) is False
        assert env.writes == []


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
                "identity_line": "I am @alice on AgentChat. I call agentchat_inbox first.",
                "identity_line_template": "I am @<handle> on AgentChat.",
            }
        )

    def test_served_line_is_spelled_for_hermes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_load(client: Any, *, cache_path: Path, **_kw: Any) -> LoadedTexts:
            seen["cache_path"] = cache_path
            return LoadedTexts(texts=self._texts(), fetched_at=0.0, source="server")

        monkeypatch.setattr(placement, "load_texts", fake_load)
        line = placement.resolve_identity_line(
            api_key="ac_live_k", api_base="https://api.example.test", hermes_home=tmp_path
        )
        assert line == "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first."
        assert seen["cache_path"].parent.parent == tmp_path / "agentchat-state"
        assert seen["cache_path"].name == "texts.json"

    def test_nothing_available_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(placement, "load_texts", lambda client, *, cache_path, **_kw: None)
        assert (
            placement.resolve_identity_line(
                api_key="k", api_base="https://api.example.test", hermes_home=tmp_path
            )
            is None
        )


class TestMcpStep:
    @pytest.mark.parametrize(
        ("outcome", "status"),
        [("added", "changed"), ("present", "ok"), ("user_managed", "skipped"), ("disabled", "skipped")],
    )
    def test_outcomes_map_to_statuses(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, status: str
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: outcome)
        step = placement.place_mcp("https://api.example.test")
        assert step.name == "mcp" and step.status == status

    def test_registration_error_is_a_failed_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*, api_base: str) -> str:
            raise placement.McpRegistrationError("no hermes")

        monkeypatch.setattr(placement, "install_entry", boom)
        step = placement.place_mcp("https://api.example.test")
        assert step.failed and "no hermes" in step.detail

    def test_place_all_runs_every_step_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: "added")
        steps = placement.place_identity_and_tools(
            handle="alice",
            identity_line=LINE,
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
        )
        assert [s.name for s in steps] == ["anchor", "skill", "mcp"]
        assert not any(s.failed for s in steps)


class TestNothingSecretLeaks:
    def test_steps_never_carry_the_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(placement, "install_entry", lambda *, api_base: "added")
        steps: list[Any] = placement.place_identity_and_tools(
            handle="alice",
            identity_line=LINE,
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
        )
        assert all("ac_live" not in s.detail for s in steps)
