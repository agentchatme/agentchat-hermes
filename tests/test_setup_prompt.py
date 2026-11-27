"""The setup line in a new session's system prompt (:mod:`agentchatme_hermes.setup_prompt`).

It says the agent's next step in this profile and nothing once there is
none: how to sign up (no key of its own), the command that finishes a
sign-up waiting for its code, or ``/agentchat allow`` (set up, nobody
decided yet). It never raises: Hermes skips a section that renders empty.
"""
from __future__ import annotations

from typing import Any

import pytest
from fake_keys import api_key
from key_files import profile_home, save_key

from agentchatme_hermes import allow, setup_prompt, signup_steps
from agentchatme_hermes.profile_scope import ProfileScope

_BASE = "https://api.agentchat.me"


def _switch(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allow.Switch(state))  # type: ignore[arg-type]


def _waiting(handle: str = "alice") -> None:
    signup_steps.save_pending(
        profile_home(),
        signup_steps.Pending(
            kind="register",
            flow={"version": 1, "flow": "register", "next_step": "verify", "pending_id": "pnd_1"},
            api_base=_BASE,
            email="a@b.co",
            handle=handle,
        ),
    )


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTCHATME_HANDLE", "AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
        monkeypatch.delenv(name, raising=False)


class TestText:
    def test_not_set_up_says_how_to_sign_up_with_the_plugin(self) -> None:
        text = setup_prompt.section_text(profile_home(), api_base=_BASE)
        assert text == setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat")
        assert "`hermes agentchat register --email <email> --handle <handle>`" in text
        assert "`hermes agentchat register --code <code>`" in text
        assert "never over HTTP" in text and "/agentchat allow" in text
        assert "never open, print or ask for an AgentChat key file" in text

    def test_a_waiting_sign_up_names_the_command_that_finishes_it(self) -> None:
        _waiting()
        text = setup_prompt.section_text(profile_home(), api_base=_BASE)
        assert "A sign-up for @alice is waiting for its emailed code" in text
        assert "hermes agentchat register --code <code>" in text
        assert "a@b.co" not in text, "the email address stays out of the prompt"

    def test_set_up_with_the_tools_undecided_asks_for_the_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("prompt_000000000000000"), handle="alice")
        _switch(monkeypatch, "not_allowed_yet")
        text = setup_prompt.section_text(profile_home(), api_base=_BASE)
        assert text == setup_prompt.NOT_ALLOWED_YET.format(who="@alice")

    @pytest.mark.parametrize("state", ["allowed", "not_allowed", "applying", "disabled", "unknown"])
    def test_nothing_to_say_is_no_text(self, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
        save_key(api_key("prompt_000000000000000"), handle="alice")
        _switch(monkeypatch, state)
        assert setup_prompt.section_text(profile_home(), api_base=_BASE) == ""

    def test_every_text_fits_its_budget(self) -> None:
        longest = max(
            (
                setup_prompt.NOT_SET_UP.format(cmd="hermes -p a-long-profile-name agentchat"),
                setup_prompt.WAITING.format(waiting="x" * 200),
                setup_prompt.NOT_ALLOWED_YET.format(who="@" + "h" * 30),
            ),
            key=len,
        )
        assert len(longest) <= setup_prompt.MAX_CHARS


class _Ctx:
    def __init__(self) -> None:
        self.sections: list[dict[str, Any]] = []

    def register_system_prompt_section(self, id: str, content: Any, *, position: str, max_chars: int) -> None:
        self.sections.append({"id": id, "content": content, "position": position, "max_chars": max_chars})


class TestRegistration:
    def test_registered_once_per_profile_with_its_budget(self) -> None:
        ctx = _Ctx()
        assert setup_prompt.register_setup_section(ctx, ProfileScope.capture(profile_home())) is True
        assert [(s["id"], s["position"], s["max_chars"]) for s in ctx.sections] == [
            ("agentchat.setup", "after_memory", setup_prompt.MAX_CHARS)
        ]
        assert ctx.sections[0]["content"]({}) == setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat")

    def test_a_hermes_without_prompt_sections_is_skipped(self) -> None:
        assert setup_prompt.register_setup_section(object(), ProfileScope.capture(profile_home())) is False

    def test_a_state_that_cannot_be_read_renders_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx()
        setup_prompt.register_setup_section(ctx, ProfileScope.capture(profile_home()))

        def broken(home: Any, *, api_base: str) -> str:
            raise RuntimeError("config.yaml unreadable")

        monkeypatch.setattr(setup_prompt, "section_text", broken)
        assert ctx.sections[0]["content"]({}) == ""

    def test_a_refusal_is_logged_not_raised(self) -> None:
        class Refusing:
            def register_system_prompt_section(self, *args: Any, **kwargs: Any) -> None:
                raise ValueError("system prompt section 'agentchat.setup' is already registered")

        assert setup_prompt.register_setup_section(Refusing(), ProfileScope.capture(profile_home())) is False
