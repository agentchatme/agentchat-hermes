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

    def test_in_an_agents_terminal_the_restart_is_the_persons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        monkeypatch.setenv("HERMES_SESSION_ID", "s")
        line = credentials.key_in_use_line(None, "ac_x", serving=False, runs=True, home=profile_home())
        assert line == credentials.LOAD_RESTART_LINE_AGENT
        assert "the person's, not yours" in line

    def test_allow_names_the_restart_after_a_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes.profile_scope import ProfileScope

        monkeypatch.setattr(allow, "_written_answer", lambda decision, *, scope, agent: f"{decision} written.")
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        scope = ProfileScope.capture(profile_home())
        assert allow._write_answer("yes", scope=scope, agent="@a") == f"yes written. {credentials.LOAD_RESTART_LINE}"
        assert allow._write_answer("no", scope=scope, agent="@a") == "no written."

    def test_the_setup_line_asks_for_the_restart_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "gateway_cannot_load", lambda home: True)
        text = setup_prompt.section_text(profile_home(), api_base="https://api.agentchat.me")
        assert text.startswith(setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat"))
        assert text.endswith(setup_prompt.RESTART_TO_LOAD)
        assert len(text) <= setup_prompt.MAX_CHARS
