"""A first install into a running gateway: AgentChat runs there only after one restart.

Hermes installs the plugin's dependency after that gateway started and never
switches a running process's dependency environment (lab, 2026-11-27: the
enable printed "Restart them to load the plugin", the gateway logged no SDK,
and setup said "no restart needed"). The gateway records itself
(:func:`placement.note_gateway_cannot_load`); every other process compares
the record with the gateway Hermes sees (:func:`placement.gateway_cannot_load`),
and setup, ``/agentchat allow``, the setup line and ``doctor`` say the
restart is the last step, and the person's.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from key_files import profile_home

from agentchatme_hermes import allow, credentials, placement, setup_prompt
from agentchatme_hermes.key_store import store_dir

if TYPE_CHECKING:
    from pathlib import Path


def _seen(monkeypatch: pytest.MonkeyPatch, seen: Any) -> None:
    monkeypatch.setattr(allow, "gateway_seen", lambda home: seen)


def _record() -> Path:
    placement.note_gateway_cannot_load(profile_home(), "no agentchatme")
    return store_dir(profile_home()) / placement.GATEWAY_CANNOT_LOAD_FILENAME


@pytest.fixture(autouse=True)
def _not_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentchatme_hermes import leader_lock

    monkeypatch.setattr(leader_lock, "is_hermes_gateway_process", lambda: False)


class TestTheRecord:
    def test_the_gateway_that_wrote_it_still_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _record()
        _seen(monkeypatch, (os.getpid(), allow.process_start_time(os.getpid())))
        assert placement.gateway_cannot_load(profile_home()) is True
        assert path.exists()

    def test_a_restarted_gateway_forgets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _record()
        _seen(monkeypatch, (os.getpid() + 1, None))
        assert placement.gateway_cannot_load(profile_home()) is False
        assert not path.exists()

    def test_no_gateway_forgets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _record()
        _seen(monkeypatch, "stopped")
        assert placement.gateway_cannot_load(profile_home()) is False

    def test_kept_while_hermes_cannot_tell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _record()
        _seen(monkeypatch, "unknown")
        assert placement.gateway_cannot_load(profile_home()) is True

    def test_none_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seen(monkeypatch, (os.getpid(), None))
        assert placement.gateway_cannot_load(profile_home()) is False

    def test_in_the_gateway_its_own_import_says(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import kit, leader_lock

        monkeypatch.setattr(leader_lock, "is_hermes_gateway_process", lambda: True)
        monkeypatch.setattr(kit, "kit_problem", lambda: "no agentchatme")
        assert placement.gateway_cannot_load(profile_home()) is True
        monkeypatch.setattr(kit, "kit_problem", lambda: None)
        assert placement.gateway_cannot_load(profile_home()) is False


class TestRegisterInTheGateway:
    def _register(self, monkeypatch: pytest.MonkeyPatch, *, kit: bool) -> None:
        from agentchatme_hermes import _register

        monkeypatch.setattr(_register, "_is_gateway_context", lambda ctx: True)
        if not kit:
            monkeypatch.setitem(sys.modules, "agentchatme.integration", None)
        monkeypatch.setattr(_register, "ActivationWatcher", MagicMock())
        ctx = MagicMock()
        ctx.get_config.side_effect = lambda key, default=None: default
        _register.register(ctx)

    def test_a_gateway_without_the_sdk_records_itself_and_says_restart(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes"):
            self._register(monkeypatch, kit=False)
        assert (store_dir(profile_home()) / placement.GATEWAY_CANNOT_LOAD_FILENAME).exists()
        assert any("hermes gateway restart" in r.getMessage() for r in caplog.records)

    def test_a_gateway_with_the_sdk_clears_an_earlier_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _record()
        self._register(monkeypatch, kit=True)
        assert not path.exists()


class TestWhatIsSaid:
    def test_setup_says_the_restart_not_no_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == credentials.LOAD_RESTART_LINE
        assert "/restart" in line and "hermes gateway restart" in line

    @pytest.mark.parametrize("supported", [True, False])
    def test_in_an_agents_terminal_the_persons_allow_restarts_it_where_it_can(
        self, monkeypatch: pytest.MonkeyPatch, supported: bool
    ) -> None:
        from agentchatme_hermes import gateway_restart

        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allow.Switch("not_allowed_yet"))
        monkeypatch.setattr(gateway_restart, "supported", lambda: supported)
        monkeypatch.setenv("HERMES_SESSION_ID", "s")
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == (credentials.LOAD_ON_ALLOW_LINE_AGENT if supported else credentials.LOAD_RESTART_LINE_AGENT)
        assert ("Never restart it yourself" if supported else "the person's, not yours") in line

    def test_in_an_agents_terminal_allowed_already_the_restart_is_the_persons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        allowed = MagicMock(by_tier=True)
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allowed)
        monkeypatch.setattr(allow, "restart_after_yes", lambda home: pytest.fail("an agent's command restarted it"))
        monkeypatch.setenv("HERMES_SESSION_ID", "s")
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == credentials.LOAD_RESTART_LINE_AGENT

    def test_a_restart_under_way_is_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import gateway_restart

        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "restart_state", lambda home: "under_way")
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == credentials.LOAD_RESTARTING_LINE

    def test_the_line_never_starts_a_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import gateway_restart

        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: MagicMock(by_tier=True))
        monkeypatch.setattr(gateway_restart, "restart_state", lambda home: None)
        monkeypatch.setattr(gateway_restart, "request_load_restart", lambda home: pytest.fail("a line restarted it"))
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == credentials.LOAD_RESTART_LINE

    @pytest.mark.parametrize("outcome", ["changed", "allowed", "applying"])
    @pytest.mark.parametrize("started", [True, False])
    def test_allow_starts_the_restart_and_says_so_instead_of_how_the_tools_connect(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, started: bool
    ) -> None:
        from agentchatme_hermes import gateway_restart
        from agentchatme_hermes.profile_scope import ProfileScope

        step = MagicMock(failed=False, outcome=outcome)
        monkeypatch.setattr(placement, "place_mcp", lambda api_base, *, decision: step)
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allow.Switch("not_allowed_yet"))
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: None)
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "request_load_restart", lambda home: started)
        scope = ProfileScope.capture(profile_home())
        reply = allow._write_answer("yes", scope=scope, agent="@a")
        line = credentials.LOAD_RESTARTING_LINE if started else credentials.LOAD_RESTART_LINE
        assert reply == f"AgentChat tools allowed for @a. {line}"
        assert "within about a minute" not in reply

    def test_no_restart_needed_leaves_the_reply_as_it_was(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import gateway_restart
        from agentchatme_hermes.profile_scope import ProfileScope

        step = MagicMock(failed=False, outcome="changed")
        monkeypatch.setattr(placement, "place_mcp", lambda api_base, *, decision: step)
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allow.Switch("not_allowed_yet"))
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: False)
        monkeypatch.setattr(gateway_restart, "request_load_restart", lambda home: pytest.fail("restarted for nothing"))
        scope = ProfileScope.capture(profile_home())
        assert "within about a minute" in allow._write_answer("yes", scope=scope, agent="@a")

    def test_a_no_says_no_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes.profile_scope import ProfileScope

        step = MagicMock(failed=False, outcome="changed")
        monkeypatch.setattr(placement, "place_mcp", lambda api_base, *, decision: step)
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        scope = ProfileScope.capture(profile_home())
        assert allow._write_answer("no", scope=scope, agent="@a") == allow.NOT_ALLOWED_LINE.format(who="@a")

    def test_the_setup_line_asks_for_no_restart_the_allow_will_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import gateway_restart

        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "supported", lambda: True)  # POSIX; Windows: the test below
        text = setup_prompt.section_text(profile_home(), api_base="https://api.agentchat.me")
        assert text == setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat")

    def test_where_the_plugin_cannot_restart_the_setup_line_asks_for_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import gateway_restart

        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setattr(gateway_restart, "supported", lambda: False)
        text = setup_prompt.section_text(profile_home(), api_base="https://api.agentchat.me")
        assert text.startswith(setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat"))
        assert text.endswith(setup_prompt.RESTART_TO_LOAD)
        assert len(text) <= setup_prompt.MAX_CHARS
