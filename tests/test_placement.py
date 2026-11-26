"""Tests for ``agentchatme_hermes.placement`` — the identity line, the manual, the key source, the tools.

The key itself is :mod:`agentchatme_hermes.credentials` (``test_credentials.py``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agentchatme.types import Texts
from fake_keys import PREFIX, api_key

from agentchatme_hermes import placement

# The served manual and identity line, kept current: the plugin setting
# live_texts on (tests/conftest.py ``live_texts``). This release's texts, the
# default, are tests/test_pinned_texts.py.
pytestmark = pytest.mark.usefixtures("live_texts")

if TYPE_CHECKING:
    from pathlib import Path

LINE = "I am @alice on AgentChat (api.agentchat.me). Peers reach me there."


def _served_texts() -> Texts:
    """Served texts whose identity line is :data:`LINE`, announcing the bundled
    manual (placed from the bundled bytes: no download)."""
    import hashlib

    from agentchatme_hermes import manual

    bundle = manual.BUNDLED_MANUAL_PATH.read_bytes()
    announced = {
        "url": "https://agentchat.me/skill.md",
        "version": manual.bundled_version(),
        "sha256": hashlib.sha256(bundle).hexdigest(),
    }
    return Texts.model_validate(
        {
            "version": "1",
            "manual": announced,
            "wake_instruction": "w",
            "invite_prompt": "i",
            "identity_line": LINE,
            "identity_line_template": "I am @<handle> on AgentChat (api.agentchat.me). Peers reach me there.",
        }
    )


def read_anchor_handle(*, soul_path: Path) -> str | None:
    """The strict owner of the block in ``soul_path`` (the kit's ``anchor.owner``)."""
    from agentchatme.integration import anchor

    found = anchor.find(soul_path.read_text(encoding="utf-8"))
    return None if found is None else anchor.owner(found)


class TestAnchorAndSkill:
    def test_place_anchor_writes_the_served_identity_line(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        step = placement.place_anchor(handle="@Alice", identity_line=LINE, soul_path=soul)
        assert step.status == "changed" and not step.failed and "placed" in step.detail
        assert read_anchor_handle(soul_path=soul) == "alice"
        assert LINE in soul.read_text(encoding="utf-8")
        again = placement.place_anchor(handle="alice", identity_line=LINE, soul_path=soul)
        assert again.status == "ok" and "the served text" in again.detail

    def test_doctor_fix_never_replaces_another_agents_block(self, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        placement.place_anchor(handle="bob", identity_line="I am @bob on AgentChat.", soul_path=soul)
        before = soul.read_bytes()
        step = placement.place_anchor(handle="alice", identity_line=LINE, soul_path=soul, replace_other=False)
        assert step.failed and "@bob's" in step.detail and soul.read_bytes() == before
        login = placement.place_anchor(handle="alice", identity_line=LINE, soul_path=soul)
        assert login.status == "changed" and "was @bob's" in login.detail

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

    def test_bundled_copy_is_present_and_is_the_published_manual(self) -> None:
        text = placement.BUNDLED_SKILL_PATH.read_text(encoding="utf-8")
        assert text.startswith("---\nname: agentchat\n")
        assert "## Be reachable" in text


class TestResolveTexts:
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
        from agentchatme_hermes.texts import identity_text

        seen: dict[str, Any] = {}

        def fake_load(client: Any, *, state_dir: Path, **_kw: Any) -> Texts:
            seen["state_dir"] = state_dir
            return self._texts()

        monkeypatch.setattr(placement, "load_texts", fake_load)
        texts = placement.resolve_texts(
            api_key=api_key("k"), api_base="https://api.example.test", hermes_home=tmp_path
        )
        line = identity_text(texts, handle="alice", home=tmp_path).line
        assert line == "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first."
        assert seen["state_dir"].parent == tmp_path / "agentchat-state"

    def test_nothing_available_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(placement, "load_texts", lambda client, *, state_dir, **_kw: None)
        assert (
            placement.resolve_texts(
                api_key="k", api_base="https://api.example.test", hermes_home=tmp_path
            )
            is None
        )


def _placed(outcome: str, trust: Any = None, header_added: bool = False) -> Any:
    from agentchatme_hermes.mcp_registration import EntryPlacement

    return lambda *, api_base, decision=None: EntryPlacement(outcome, trust, header_added)  # type: ignore[arg-type]


class TestMcpStep:
    @pytest.mark.parametrize(
        ("outcome", "status"),
        [
            ("added", "changed"),
            ("allowed", "changed"),
            ("upgraded", "changed"),
            ("present", "ok"),
            ("missing", "skipped"),
            ("user_managed", "skipped"),
            ("disabled", "skipped"),
        ],
    )
    def test_outcomes_map_to_statuses(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, status: str
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", _placed(outcome, "full"))
        step = placement.place_mcp("https://api.example.test")
        assert step.name == "mcp" and step.status == status and step.outcome == outcome

    def test_an_upgraded_entry_says_what_changed_and_when_it_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "install_entry", _placed("upgraded"))
        detail = placement.place_mcp("https://api.example.test").detail
        assert "X-AgentChat-Client: hermes" in detail
        assert "gateway restart" in detail and "/reload-mcp" in detail

    def test_the_decision_reaches_the_writer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []

        def install(*, api_base: str, decision: Any = None) -> Any:
            from agentchatme_hermes.mcp_registration import EntryPlacement

            seen.append(decision)
            return EntryPlacement("missing")

        monkeypatch.setattr(placement, "install_entry", install)
        placement.place_mcp("https://api.example.test")
        placement.place_mcp("https://api.example.test", decision="yes")
        assert seen == [None, "yes"]

    def test_an_added_entry_names_its_trust_and_when_it_connects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "install_entry", _placed("added", "full"))
        detail = placement.place_mcp("https://api.example.test", decision="yes").detail
        assert "trust: full" in detail and "within about a minute" in detail

    def test_an_allowed_entry_says_how_to_apply_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "install_entry", _placed("allowed", "full", header_added=True))
        detail = placement.place_mcp("https://api.example.test", decision="yes").detail
        assert "trust: full" in detail and "/reload-mcp" in detail and "hermes gateway restart" in detail
        assert "X-AgentChat-Client: hermes" in detail

    def test_not_allowed_yet_names_the_slash_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "install_entry", _placed("missing"))
        step = placement.place_mcp("https://api.example.test")
        assert "not allowed yet" in step.detail and "/agentchat allow" in step.detail

    @pytest.mark.parametrize(
        ("outcome", "decision", "how"),
        [("added", "yes", "asked"), ("added", "no", "asked"), ("added", "carried_over", "carried_over"), ("allowed", "yes", "asked")],
    )
    def test_a_written_entry_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str, decision: str, how: str
    ) -> None:
        from agentchatme_hermes import allow

        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(placement, "install_entry", _placed(outcome, "full"))
        placement.place_mcp("https://api.example.test", decision=decision)  # type: ignore[arg-type]
        record = allow.read_record(tmp_path)
        assert record is not None and record["how"] == how and record["trust"] == "full"

    @pytest.mark.parametrize("outcome", ["present", "missing", "upgraded", "user_managed", "disabled"])
    def test_nothing_decided_nothing_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
    ) -> None:
        from agentchatme_hermes import allow

        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(placement, "install_entry", _placed(outcome))
        placement.place_mcp("https://api.example.test")
        assert allow.read_record(tmp_path) is None

    def _connected_untrusted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, gateway: bool) -> list[str]:
        from agentchatme_hermes import allow, mcp_registration
        from agentchatme_hermes.mcp_registration import Allowance, EntryPlacement

        log: list[str] = []
        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(mcp_registration, "inspect_allowance", lambda *, api_base: Allowance("ours", "untrusted", None))
        monkeypatch.setattr(allow, "gateway_lives", lambda home, *, api_base: gateway)
        monkeypatch.setattr(mcp_registration, "uninstall_entry", lambda *, api_base: log.append("removed") or True)
        monkeypatch.setattr(
            placement,
            "install_entry",
            lambda *, api_base, decision=None: log.append(f"install {decision}") or EntryPlacement("allowed", "full"),
        )
        return log

    def test_a_yes_over_a_connected_untrusted_entry_takes_two_steps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Hermes keeps a connected server's tier, and its watcher only adds and
        removes servers: the decision is recorded, the entry removed; the
        gateway writes it back full once the old connection is gone."""
        from agentchatme_hermes import allow

        log = self._connected_untrusted(monkeypatch, tmp_path, gateway=True)
        step = placement.place_mcp("https://api.example.test", decision="yes")
        assert (step.status, step.outcome) == ("changed", "applying")
        assert "within about two minutes" in step.detail
        assert log == ["removed"]
        record = allow.read_record(tmp_path)
        assert record is not None and (record["decision"], record["trust"], record["pending"]) == ("yes", "full", True)

    def test_with_no_gateway_running_it_is_changed_in_place(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import allow

        log = self._connected_untrusted(monkeypatch, tmp_path, gateway=False)
        step = placement.place_mcp("https://api.example.test", decision="yes")
        assert step.outcome == "allowed" and log == ["install yes"]
        record = allow.read_record(tmp_path)
        assert record is not None and record["pending"] is False and record["decision"] == "yes"

    def test_a_removal_that_fails_leaves_the_entry_and_no_pending_yes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import allow, mcp_registration

        self._connected_untrusted(monkeypatch, tmp_path, gateway=True)

        def refused(*, api_base: str) -> bool:
            raise mcp_registration.McpRegistrationError("managed")

        monkeypatch.setattr(mcp_registration, "uninstall_entry", refused)
        step = placement.place_mcp("https://api.example.test", decision="yes")
        assert step.failed and "managed" in step.detail
        record = allow.read_record(tmp_path)
        assert record is not None and record["pending"] is False

    @pytest.mark.parametrize("decision", [None, "no", "carried_over"])
    def test_only_a_yes_takes_two_steps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, decision: Any) -> None:
        log = self._connected_untrusted(monkeypatch, tmp_path, gateway=True)
        placement.place_mcp("https://api.example.test", decision=decision)
        assert log == [f"install {decision}"]

    def test_registration_error_is_a_failed_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*, api_base: str, decision: Any = None) -> Any:
            raise placement.McpRegistrationError("no hermes")

        monkeypatch.setattr(placement, "install_entry", boom)
        step = placement.place_mcp("https://api.example.test")
        assert step.failed and "no hermes" in step.detail

    def test_place_all_runs_every_step_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import secrets_registration

        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(placement, "install_entry", _placed("added", "full"))
        monkeypatch.setattr(secrets_registration, "install_block", lambda: "added")
        steps = placement.place_identity_and_tools(
            handle="alice",
            texts=_served_texts(),
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
            tools_decision="yes",
        )
        # The key source before the tools' entry, which authenticates through it.
        assert [s.name for s in steps] == ["anchor", "manual", "secrets", "mcp"]
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

        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(placement, "install_entry", _placed("added", "full"))
        monkeypatch.setattr(secrets_registration, "install_block", lambda: "added")
        steps: list[Any] = placement.place_identity_and_tools(
            handle="alice",
            texts=_served_texts(),
            api_base="https://api.example.test",
            soul_path=tmp_path / "SOUL.md",
        )
        assert all(PREFIX not in s.detail for s in steps)
