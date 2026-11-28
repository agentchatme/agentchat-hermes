"""The once-a-day reminder while the person has not decided (:mod:`agentchatme_hermes.reminder`).

It is added only to a turn a person started, at most once a day per profile, says the step only
the person can take, and stops once they decide (Allow, or Don't allow). It never raises.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fake_keys import api_key
from key_files import profile_home, save_key

from agentchatme_hermes import allow, gateway_restart, placement, reminder
from agentchatme_hermes.profile_scope import ProfileScope

_BASE = "https://api.agentchat.me"


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTCHATME_HANDLE", "AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: False)


def _switch(monkeypatch: pytest.MonkeyPatch, state: str, *, by_tier: bool = False) -> None:
    switch = MagicMock(state=state, by_tier=by_tier)
    monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: switch)


def _hook(monkeypatch: pytest.MonkeyPatch) -> Any:
    ctx = MagicMock()
    assert reminder.register_reminder(ctx, ProfileScope.capture(profile_home())) is True
    name, hook = ctx.register_hook.call_args.args
    assert name == "pre_llm_call"
    return hook


class TestWhatItSays:
    def test_not_set_up_says_nothing(self) -> None:
        assert reminder.reminder_text(profile_home(), api_base=_BASE) is None

    def test_tools_not_decided_asks_for_the_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("remind_000000000000000"), handle="alice")
        _switch(monkeypatch, "not_allowed_yet")
        text = reminder.reminder_text(profile_home(), api_base=_BASE)
        assert text == reminder.REMIND_ALLOW.format(who="@alice")
        assert "/agentchat allow" in text and "Don't allow" in text

    @pytest.mark.parametrize("state", ["not_allowed", "disabled", "unknown"])
    def test_a_decision_or_an_unreadable_switch_says_nothing(self, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
        save_key(api_key("remind_000000000000000"))
        _switch(monkeypatch, state)
        assert reminder.reminder_text(profile_home(), api_base=_BASE) is None

    def test_allowed_and_running_says_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("remind_000000000000000"))
        _switch(monkeypatch, "allowed", by_tier=True)
        assert reminder.reminder_text(profile_home(), api_base=_BASE) is None

    def test_allowed_but_the_gateway_cannot_load_it_restarts_where_it_can(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("remind_000000000000000"))
        _switch(monkeypatch, "allowed", by_tier=True)
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "request_load_restart", lambda home: True)
        assert reminder.reminder_text(profile_home(), api_base=_BASE) is None

    def test_where_it_cannot_restart_it_asks_the_person(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("remind_000000000000000"), handle="alice")
        _switch(monkeypatch, "allowed", by_tier=True)
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "request_load_restart", lambda home: False)
        text = reminder.reminder_text(profile_home(), api_base=_BASE)
        assert text == reminder.REMIND_RESTART.format(who="@alice")
        assert "Never restart it yourself" in text


class TestWhenItIsAdded:
    @pytest.fixture(autouse=True)
    def _undecided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(api_key("remind_000000000000000"), handle="alice")
        _switch(monkeypatch, "not_allowed_yet")

    def test_once_a_day_in_a_persons_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = _hook(monkeypatch)
        first = hook(platform="telegram", session_id="s", user_message="hi")
        assert first == reminder.REMIND_ALLOW.format(who="@alice")
        assert hook(platform="telegram", session_id="s", user_message="again") is None
        assert hook(platform="cli", session_id="t", user_message="another chat") is None, "once a day per profile"

    def test_again_after_a_day(self) -> None:
        home = profile_home()
        assert reminder._claim(home, now=1_000_000.0) is True
        assert reminder._claim(home, now=1_000_000.0 + reminder.REMIND_EVERY_S - 1) is False
        assert reminder._claim(home, now=1_000_000.0 + reminder.REMIND_EVERY_S) is True

    @pytest.mark.parametrize("platform", sorted(reminder.SKIP_PLATFORMS))
    def test_never_in_a_turn_no_person_started(self, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
        hook = _hook(monkeypatch)
        assert hook(platform=platform, session_id="s", user_message="job") is None
        assert hook(platform="telegram", session_id="s", user_message="hi") is not None, "the day was not used up"

    def test_never_in_a_delegated_subagent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = _hook(monkeypatch)
        assert hook(platform="cli", parent_session_id="parent", session_id="child", user_message="task") is None

    def test_a_failure_is_no_reminder_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = _hook(monkeypatch)

        def broken(home: Any, *, api_base: str) -> str:
            raise RuntimeError("config.yaml unreadable")

        monkeypatch.setattr(reminder, "reminder_text", broken)
        assert hook(platform="telegram", session_id="s", user_message="hi") is None


def test_a_hermes_without_hooks_is_skipped() -> None:
    assert reminder.register_reminder(object(), ProfileScope.capture(profile_home())) is False
