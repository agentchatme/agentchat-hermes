"""The pure helpers in ``agentchatme_hermes.wizard``: key masking and the
key shape in tracked text. The flows are ``test_wizard_signup.py``."""
from __future__ import annotations

import pytest
from fake_keys import api_key, real_shaped_key

from agentchatme_hermes.wizard import _mask_key

# ─── _mask_key ─────────────────────────────────────────────────────────────


class TestMaskKey:
    def test_real_shaped_key_shows_first_8_and_last_4(self) -> None:
        key = real_shaped_key()
        assert len(key) == 46
        masked = _mask_key(key)
        assert masked == f"{key[:8]}…{key[-4:]}"
        assert masked.startswith("ac_")
        # 34 of the 43 random characters stay hidden.
        assert key[8:-4] not in masked

    def test_long_key_masked(self) -> None:
        masked = _mask_key(api_key("abcdef1234567890ghij"))
        assert masked == "ac_abcde…ghij"
        assert "1234567890" not in masked

    def test_short_value_uses_placeholder(self) -> None:
        assert _mask_key("ac_short") == "ac_…"
        assert _mask_key("") == "ac_…"

    def test_twelve_characters_are_never_shown_whole(self) -> None:
        # 8 + 4 would be the whole value.
        assert _mask_key(api_key("123456789")) == "ac_…"
        assert _mask_key(api_key("1234567890")) == "ac_12345…7890"

    def test_cli_uses_the_same_mask(self) -> None:
        from agentchatme_hermes import cli

        assert cli._mask_key is _mask_key


class TestKeyShapeText:
    def test_no_tracked_text_names_a_live_prefix(self) -> None:
        """Keys are ``ac_`` and 43 random characters; there is no ``live``
        prefix, so no prompt, help text, doc, manifest or workflow may show
        one. The CHANGELOG keeps its history, the tests build keys at run
        time, and the bundled manual is the published text verbatim."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        try:
            listed = subprocess.run(
                ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("not a git checkout")
        skipped = ("CHANGELOG.md", "tests/", "agentchatme_hermes/skills/agentchat/SKILL.md")
        needle = "ac_" + "live"
        offenders = []
        for name in listed:
            if name.startswith(skipped):
                continue
            path = root / name
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                offenders.append(name)
        assert offenders == []


# ─── the edit menu's one question ──────────────────────────────────────────


class TestEditMenuAllow:
    def _menu(self, monkeypatch: pytest.MonkeyPatch, *, not_allowed: bool, pick: str) -> tuple[list[str], list[str]]:
        from agentchatme_hermes import wizard

        shown: list[str] = []
        flows: list[str] = []
        monkeypatch.setattr(wizard, "_tools_not_allowed", lambda: not_allowed)
        monkeypatch.setattr(wizard, "_allow_tools_flow", lambda handle, **kw: flows.append(f"allow {handle}"))
        monkeypatch.setattr(wizard, "_logout_flow", lambda *a: flows.append("logout"))

        def prompt_choice(question: str, choices: list[str], default: int = 0, description: str = "") -> int:
            shown.extend(choices)
            return next(i for i, c in enumerate(choices) if c.startswith(pick))

        wizard._edit_menu(
            existing_key=api_key("menu_key_00000000"),
            existing_handle="alice",
            prompt=None,
            prompt_yes_no=None,
            prompt_choice=prompt_choice,
            print_info=lambda text: None,
            print_success=lambda text: None,
            print_warning=lambda text: None,
            save_env_value=None,
            get_env_value=None,
        )
        return shown, flows

    def test_offered_while_not_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown, flows = self._menu(monkeypatch, not_allowed=True, pick="Allow")
        assert shown[1] == "Allow @alice to use AgentChat tools on its own"
        assert flows == ["allow alice"]

    def test_not_offered_once_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown, flows = self._menu(monkeypatch, not_allowed=False, pick="Logout")
        assert not any(c.startswith("Allow") for c in shown)
        assert flows == ["logout"]

    def test_the_flow_writes_the_answer(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from agentchatme_hermes import allow, placement, wizard
        from agentchatme_hermes.placement import PlacementStep

        placed: list[object] = []
        monkeypatch.setattr(wizard, "_tools_decision", lambda handle: allow.SetupDecision("yes", allow.ALLOWED_LINE))
        monkeypatch.setattr(
            placement, "place_mcp", lambda api_base, decision=None: placed.append(decision) or PlacementStep("mcp", "changed", "d", "allowed")
        )
        wizard._allow_tools_flow("alice", print_info=lambda t: None, print_warning=lambda t: None)
        out = capsys.readouterr().out
        assert placed == ["yes"]
        assert "✓ AgentChat tools allowed" in out and "/reload-mcp" in out

    def test_the_flow_asks_nothing_when_nobody_can_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import allow, placement, wizard

        said: list[str] = []
        monkeypatch.setattr(
            wizard, "_tools_decision", lambda handle: allow.SetupDecision(None, allow.AGENT_TERMINAL_LINE.format(who="@alice"))
        )
        monkeypatch.setattr(placement, "place_mcp", lambda *a, **k: pytest.fail("nothing to write"))
        wizard._allow_tools_flow("alice", print_info=said.append, print_warning=said.append)
        assert said and "/agentchat allow" in said[0]

    def test_not_allowed_reads_the_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import mcp_registration, wizard
        from agentchatme_hermes.mcp_registration import Allowance

        for allowance, offered in (
            (Allowance("missing", None, None), True),
            (Allowance("ours", "untrusted", None), True),
            (Allowance("ours", "full", "trust"), False),
            (Allowance("user_managed", "untrusted", None), True),
        ):
            monkeypatch.setattr(mcp_registration, "inspect_allowance", lambda *, api_base, a=allowance: a)
            assert wizard._tools_not_allowed() is offered
