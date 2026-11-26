"""Tests for ``agentchatme_hermes._register``.

The load-bearing logic that needs coverage:

1. ``_is_gateway_context`` must reliably distinguish gateway vs every
   non-gateway process class (TUI, named CLI, one-shot, gateway-
   spawned subprocess). The 0.2.0 detector misclassified the TUI as
   gateway and opened a second WS, causing duplicate-delivery and
   lock contention. 0.2.1 switched to the canonical
   ``_HERMES_GATEWAY=1`` env marker — these tests pin that down.
2. The module must NOT register an ``on_session_end`` hook —
   regression guard against re-introducing the hook that fired on
   every per-session ending and killed the runtime mid-flight.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fake_keys import api_key

from agentchatme_hermes._register import _is_gateway_context
from agentchatme_hermes.leader_lock import GATEWAY_ENV_MARKER

# The served manual and identity line, kept current: the plugin setting
# live_texts on (tests/conftest.py ``live_texts``). This release's texts, the
# default, are tests/test_pinned_texts.py.
pytestmark = pytest.mark.usefixtures("live_texts")

if TYPE_CHECKING:
    from pathlib import Path


class TestIsGatewayContext:
    def test_gateway_when_argv_is_gateway_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        assert _is_gateway_context(ctx=object()) is True

    def test_env_marker_alone_is_not_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "agentchat", "doctor"])
        assert _is_gateway_context(ctx=object()) is False

    def test_not_gateway_when_env_marker_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TUI / named CLI / one-shot — none of these set the marker.
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        assert _is_gateway_context(ctx=object()) is False

    def test_not_gateway_when_marker_is_other_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: only literal "1" counts. A future Hermes that
        # ever sets it to "true" or anything else should NOT be
        # silently classified as gateway by us.
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "true")
        assert _is_gateway_context(ctx=object()) is False

    def test_ctx_value_does_not_change_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ctx arg is intentionally unused.

        Earlier 0.2.0 logic inspected ``ctx._manager._cli_ref`` and
        misfired. The 0.2.1 detector is process-level only — any ctx
        with the marker set returns True; any ctx without it returns
        False.
        """
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        assert _is_gateway_context(ctx=None) is True
        assert _is_gateway_context(ctx=MagicMock()) is True
        assert _is_gateway_context(ctx="anything") is True


class TestRegisterNoSessionEndHook:
    """Regression guard against re-introducing the on_session_end hook.

    The first spike's primary bug: Hermes fires ``on_session_end`` on
    every individual session ending (TUI sessions, cron jobs, adapter
    chats), not just process shutdown. Wiring our ``runtime.stop()``
    to that hook killed the WS daemon mid-conversation.
    """

    def test_no_on_session_end_registration(self) -> None:
        from agentchatme_hermes import _register

        ctx = MagicMock()
        # CLI registration is unconditional; everything else short-circuits
        # because load_config will return None when AGENTCHATME_API_KEY
        # is absent in the test env.
        _register.register(ctx)

        # The forbidden call.
        forbidden = any(
            call.args == ("on_session_end",)
            or (call.args and call.args[0] == "on_session_end")
            or "on_session_end" in str(call)
            for call in ctx.method_calls
        )
        assert not forbidden, (
            "register() must NOT wire an on_session_end hook — Hermes "
            "fires that per-session, not per-process-shutdown."
        )


def _unused_param_silencer(_: Any) -> None:
    """Silences linter complaints about unused-imports in adapted tests."""
    return None


class TestProfileScopedRegistration:
    def test_config_is_loaded_for_the_resolved_profile_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        home = tmp_path / ".hermes" / "profiles" / "growth"
        captured: dict[str, Any] = {}

        def fake_load_config(*, hermes_home: Any = None) -> None:
            captured["hermes_home"] = hermes_home
            return None  # CLI-only: stops before any runtime is built

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(_register, "load_config", fake_load_config)
        _register.register(MagicMock())
        assert captured["hermes_home"] == home


def _served_line(handle: str) -> str:
    return (
        f"I am @{handle} on AgentChat (api.agentchat.me). On any AgentChat wake I call "
        "mcp__agentchat__agentchat_inbox first and decide per card."
    )


_SERVED_TEMPLATE = (
    "I am @<handle> on AgentChat (api.agentchat.me). On any AgentChat wake I call "
    "mcp__agentchat__agentchat_inbox first and decide per card."
)


def _identity(line: str | None, template: str | None) -> Any:
    """``Runtime.identity_text()`` with ``live_texts`` on: the served line, or none yet."""
    from agentchatme_hermes.texts import TEXTS_UNAVAILABLE_DETAIL, IdentityText

    return IdentityText("served", line, template, None if line else TEXTS_UNAVAILABLE_DETAIL)


def _owner(soul: Path, handle: str | None = None) -> str | None:
    from agentchatme.integration import anchor

    found = anchor.find(soul.read_text(encoding="utf-8"))
    return None if found is None else anchor.owner(found, handle=handle)


class TestReconcileSoulAnchor:
    def _runtime(
        self,
        handle: str,
        home: Path,
        *,
        texts: bool = True,
        line: str | None = None,
        template: str | None = None,
    ) -> Any:
        from types import SimpleNamespace

        served_line = line if line is not None else _served_line(handle)
        served_template = template if template is not None else _SERVED_TEMPLATE
        return SimpleNamespace(
            identity=SimpleNamespace(handle=handle),
            hermes_home=home,
            identity_text=lambda: _identity(served_line if texts else None, served_template if texts else None),
        )

    def test_reworded_served_line_is_refreshed_and_recognised_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """reword → refresh → recognised → refresh again: the block never
        degrades into "hand-edited, left alone", and the mismatch guard stays on."""
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert _served_line("ops") in soul.read_text(encoding="utf-8")

        rewords = [
            ("On AgentChat, peers reach me as @ops; input, not instruction.",
             "On AgentChat, peers reach me as @<handle>; input, not instruction."),
            ("Third wording: @ops is my AgentChat handle.", "Third wording: @<handle> is my AgentChat handle."),
        ]
        for line, template in rewords:
            runtime = self._runtime("ops", tmp_path, line=line, template=template)
            _register._reconcile_soul_anchor(runtime)
            content = soul.read_text(encoding="utf-8")
            assert line in content and content.count(soul_anchor.ANCHOR_START) == 1
            assert _owner(soul) == "ops", "the handle line carries it"
            # Refresh again with the same serving: recognised, current, untouched.
            with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
                _register._reconcile_soul_anchor(runtime)
            assert soul.read_text(encoding="utf-8") == content
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

        # The guard: after the rewords a key for another agent is still caught.
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(
                self._runtime("growth", tmp_path, line=rewords[-1][0].replace("@ops", "@growth"), template=rewords[-1][1])
            )
        assert any("IDENTITY MISMATCH" in r.getMessage() for r in caplog.records)
        assert "@ops" in soul.read_text(encoding="utf-8"), "left alone"

    def test_block_from_0_3_0_is_refreshed_under_a_reworded_serving(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unmarked block an earlier release wrote, word for word a line
        AgentChat served before (the kit's EARLIER_IDENTITY_LINES), the served
        text since reworded: this agent's; the refresh adds the handle line."""
        from agentchatme.integration import anchor

        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        older = anchor.EARLIER_IDENTITY_LINES[0].replace("<handle>", "ops")
        soul.write_text(f"{ANCHOR_START}\n{older}\n{ANCHOR_END}\n", encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        line = "New wording: @ops, reachable on AgentChat."
        _register._reconcile_soul_anchor(
            self._runtime("ops", tmp_path, line=line, template="New wording: @<handle>, reachable on AgentChat.")
        )
        content = soul.read_text(encoding="utf-8")
        assert line in content and older not in content
        assert soul_anchor.render_handle_marker("ops") in content

    def test_backfills_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert _owner(soul) == "ops"
        assert _served_line("ops") in soul.read_text(encoding="utf-8")

    def test_without_texts_places_nothing_and_says_so(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path, texts=False))
        assert not soul.exists()
        assert any("identity line not placed yet" in r.getMessage() for r in caplog.records)

    def test_reconciles_when_the_texts_arrive(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The runtime calls back when texts load; the anchor is placed then."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        listeners: list[Any] = []
        state = {"line": None}
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            identity_text=lambda: _identity(state["line"], _SERVED_TEMPLATE if state["line"] else None),
            on_texts=listeners.append,
        )
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        assert len(listeners) == 1 and not soul.exists()
        state["line"] = _served_line("ops")
        listeners[0](object())
        assert _owner(soul) == "ops"

    def test_mismatch_is_an_error_and_file_is_left_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SOUL.md says Growth, the key says Operations: shout, don't rewrite."""
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        soul_anchor.write_soul_anchor(_served_line("growth"), handle="growth", soul_path=soul)
        before = soul.read_text()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("operations", tmp_path))
        assert soul.read_text() == before
        messages = [rec.getMessage() for rec in caplog.records if "IDENTITY MISMATCH" in rec.getMessage()]
        assert len(messages) == 1
        said = messages[0]
        assert "@growth's AgentChat block" in said and "authenticates as @operations" in said
        # The key has been in the key file since 0.5.4, and either side may be the wrong one.
        assert "AGENTCHAT_API_KEY" not in said
        assert "Fix the key (`hermes agentchat login`) or remove that block" in said
        assert "`hermes agentchat doctor --fix` places @operations's" in said

    def test_mismatch_names_the_profile_flag_for_a_named_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        home = tmp_path / ".hermes" / "profiles" / "growth"
        home.mkdir(parents=True)
        soul = home / "SOUL.md"
        soul_anchor.write_soul_anchor(_served_line("ops"), handle="ops", soul_path=soul)
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("growth", home))
        (said,) = [r.getMessage() for r in caplog.records if "IDENTITY MISMATCH" in r.getMessage()]
        assert "profile=growth" in said
        assert "Fix the key (`hermes -p growth agentchat login`) or remove that block" in said
        assert "`hermes -p growth agentchat doctor --fix`" in said

    def test_matching_anchor_is_quiet(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        soul_anchor.write_soul_anchor(_served_line("ops"), handle="ops", soul_path=soul)
        before = soul.read_text()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        # Quiet = no warning. (An idempotent rewrite logs at INFO, which shows
        # when Hermes is installed and has raised the logging level.)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert soul.read_text() == before

    def test_the_users_words_naming_the_handle_are_left_alone_and_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The lab repro: a block with no handle line whose words name @ops but
        are not a text AgentChat served is the user's. Before, the loose
        reading found @ops in it and replaced it with the served line."""
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        mine = (
            f"# Mine\n\n{ANCHOR_START}\n## On AgentChat\n\nYou are **@ops** on AgentChat — my own "
            f"notes about how I like to answer.\n{ANCHOR_END}\n"
        )
        soul.write_text(mine, encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul.read_text(encoding="utf-8") == mine
        said = [r.getMessage() for r in caplog.records if "edited by hand" in r.getMessage()]
        assert len(said) == 1, "said once, not on every texts change"
        assert said[0].endswith(
            "It is yours: not refreshed; to have the served line, delete that block, then run "
            "`hermes agentchat doctor --fix`."
        )

    def test_one_word_changed_is_left_alone(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        edited = _served_line("ops").replace("decide per card", "decide per thread")
        soul.write_text(f"{ANCHOR_START}\n{edited}\n{ANCHOR_END}\n", encoding="utf-8")
        before = soul.read_bytes()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul.read_bytes() == before

    def test_the_exact_served_text_without_a_handle_line_is_reshaped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        soul.write_text(f"# Mine\n\n{ANCHOR_START}\n{_served_line('ops')}\n{ANCHOR_END}\n", encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        content = soul.read_text(encoding="utf-8")
        assert soul_anchor.render_handle_marker("ops") in content and content.startswith("# Mine\n\n")

    def test_hand_edited_block_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        soul.write_text(f"{ANCHOR_START}\nI am a teapot.\n{ANCHOR_END}\n", encoding="utf-8")
        before = soul.read_text(encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("ending", ["\r\n", ""], ids=["crlf", "no-final-newline"])
    def test_a_refresh_keeps_line_breaks_and_the_end_of_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        eol = "\r\n" if ending else "\n"
        old = eol.join([ANCHOR_START, soul_anchor.render_handle_marker("ops"), "an older served line for @ops", ANCHOR_END])
        soul.write_bytes(f"# Mine{eol}{eol}{old}{eol}{eol}after{ending}".encode())
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        data = soul.read_bytes().decode("utf-8")
        assert _served_line("ops") in data and data.endswith(f"after{ending}")
        if ending:
            assert "\n" not in data.replace("\r\n", "")

    def test_several_blocks_are_left_alone_and_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        block = soul_anchor.render_anchor_block("an older served line for @ops", handle="ops")
        soul.write_text(f"{block}\n\n{ANCHOR_START}\nmine\n{ANCHOR_END}\n", encoding="utf-8")
        before = soul.read_bytes()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul.read_bytes() == before
        assert any("more than one AgentChat block" in r.getMessage() for r in caplog.records)


class TestReconcileKeyStore:
    """At activation the key file is kept in shape; ``.env`` is never written."""

    def _config(self, home: Path, key: str, source: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            hermes_home=home, api_key=key, api_key_source=source, api_base="https://api.agentchat.me"
        )

    def test_a_legacy_only_profile_gets_its_key_into_the_key_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentchatme_hermes import _register, key_source, key_store
        from agentchatme_hermes.config import ENV_API_KEY_LEGACY

        monkeypatch.setattr(key_source, "source_supported", lambda: True)

        home = tmp_path / ".hermes"
        home.mkdir()
        env = home / ".env"
        env.write_text(f"{ENV_API_KEY_LEGACY}={api_key('old')}\n", encoding="utf-8")
        _register._reconcile_key_store(self._config(home, api_key("old"), ENV_API_KEY_LEGACY))
        read = key_store.read_store(home)
        assert read.usable and read.key is not None and read.key.api_key == api_key("old")
        # Not mirrored into .env any more (0.5.3 wrote AGENTCHAT_API_KEY there).
        assert env.read_text(encoding="utf-8") == f"{ENV_API_KEY_LEGACY}={api_key('old')}\n"

    def test_a_dotenv_key_is_left_where_it_is(self, tmp_path: Path) -> None:
        from agentchatme_hermes import _register, key_store
        from agentchatme_hermes.config import ENV_API_KEY

        home = tmp_path / ".hermes"
        home.mkdir()
        _register._reconcile_key_store(self._config(home, api_key("k"), ENV_API_KEY))
        assert key_store.read_store(home).status == "missing", "moved by doctor --fix or login only"

    def test_a_moved_key_file_records_this_profile(self, tmp_path: Path) -> None:
        from agentchatme_hermes import _register, key_store
        from agentchatme_hermes.config import KEY_SOURCE_STORE

        old_home, home = tmp_path / "old", tmp_path / "new"
        key_store.write_store(old_home, api_key=api_key("k"), handle="alice", api_base="https://a.test")
        home.mkdir()
        key_store.store_dir(old_home).rename(key_store.store_dir(home))  # a rename, not a copy
        assert key_store.read_store(home).status == "moved"
        _register._reconcile_key_store(self._config(home, api_key("k"), KEY_SOURCE_STORE))
        read = key_store.read_store(home)
        assert read.status == "ok" and read.key is not None and read.key.handle == "alice"


class TestReconcileMcpRegistration:
    def test_backfills_through_placement(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, placement
        from agentchatme_hermes.placement import PlacementStep

        calls: list[str] = []
        monkeypatch.setattr(
            placement,
            "place_mcp",
            lambda api_base: calls.append(api_base) or PlacementStep("mcp", "changed", "added"),
        )
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            config=SimpleNamespace(api_base="https://api.example.test"),
        )
        _register._reconcile_mcp_registration(runtime)
        assert calls == ["https://api.example.test"]

    def test_failure_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, placement
        from agentchatme_hermes.placement import PlacementStep

        monkeypatch.setattr(
            placement, "place_mcp", lambda api_base: PlacementStep("mcp", "failed", "no hermes")
        )
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            config=SimpleNamespace(api_base="https://api.example.test"),
        )
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_mcp_registration(runtime)
        assert any("hosted tools not registered" in r.getMessage() for r in caplog.records)


def _bundled_bytes() -> bytes:
    from agentchatme_hermes.manual import BUNDLED_MANUAL_PATH

    return BUNDLED_MANUAL_PATH.read_bytes()


class TestSurface:
    def test_skill_registered_and_no_native_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The manual is a skill from the first turn; tools are the hosted MCP
        server declared in config.yaml, never ``register_tool`` calls."""
        from agentchatme_hermes import _register

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])  # CLI process: no watcher
        ctx = MagicMock()
        _register.register(ctx)

        assert ctx.register_skill.called
        skill_kwargs = ctx.register_skill.call_args.kwargs
        assert skill_kwargs["name"] == "agentchat"
        # A process that is not the gateway writes nothing: with no file placed
        # yet it registers the package's copy.
        target = tmp_path / ".hermes" / "plugin-data" / "agentchat" / "manual" / "SKILL.md"
        assert skill_kwargs["path"].read_bytes() == _bundled_bytes()
        assert skill_kwargs["path"] != target and not target.exists()
        assert not ctx.register_tool.called, "no native tools — the hosted MCP server is the tool source"

    def test_the_gateway_places_the_manual_and_registers_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        monkeypatch.setattr(_register.ActivationWatcher, "start", lambda self: None)
        ctx = MagicMock()
        ctx.get_config.side_effect = lambda key, default=None: True if key == "live_texts" else default
        _register.register(ctx)
        target = tmp_path / ".hermes" / "plugin-data" / "agentchat" / "manual" / "SKILL.md"
        assert ctx.register_skill.call_args.kwargs["path"] == target
        assert target.read_bytes() == _bundled_bytes()


class TestActivationWatcher:
    def _watcher(self, tmp_path: Path, *, gateway_mode: bool = True) -> Any:
        from agentchatme_hermes._register import ActivationWatcher

        return ActivationWatcher(
            hermes_home=tmp_path / ".hermes",
            profile="default",
            gateway_mode=gateway_mode,
        )

    def test_unconfigured_then_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Key appears after the gateway booted: the next check activates."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        configs: list[Any] = [None, SimpleNamespace(api_base="https://api.example.test")]
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: configs.pop(0))
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path)
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_store", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_secrets_block", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

        watcher = self._watcher(tmp_path)
        assert watcher.try_activate() == "unconfigured"
        assert watcher.next_delay() == _register.ACTIVATION_RETRY_S
        assert watcher.try_activate() == "active"
        assert watcher.runtime is runtime
        assert watcher.try_activate() == "active", "idempotent once bound"

    def test_start_failure_backs_off_then_recovers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Key present but the server unreachable at boot: retry with backoff."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        monkeypatch.setattr(
            _register, "load_config",
            lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.example.test"),
        )
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path)
        outcomes: list[Any] = [None, None, runtime]
        monkeypatch.setattr(
            _register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: outcomes.pop(0)
        )
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_store", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_secrets_block", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

        watcher = self._watcher(tmp_path)
        assert watcher.try_activate() == "failed"
        first = watcher.next_delay()
        assert watcher.try_activate() == "failed"
        second = watcher.next_delay()
        assert second > first, "exponential backoff after repeated failures"
        assert second <= _register.ACTIVATION_RETRY_MAX_S
        assert watcher.try_activate() == "active"
        assert watcher.next_delay() == _register.ACTIVATION_RETRY_S

    def test_gateway_without_key_starts_the_watcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        started: list[Any] = []
        monkeypatch.setattr(_register.ActivationWatcher, "start", lambda self: started.append(self))
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        _register.register(MagicMock())
        assert len(started) == 1

    def test_cli_process_without_key_does_not_start_a_watcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        started: list[Any] = []
        monkeypatch.setattr(_register.ActivationWatcher, "start", lambda self: started.append(self))
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
        _register.register(MagicMock())
        assert started == []


class _UnloadCtx:
    """A plugin context with Hermes' ``on_unload``: records the callbacks."""

    def __init__(self) -> None:
        self.unloads: list[Any] = []

    def on_unload(self, callback: Any) -> None:
        self.unloads.append(callback)

    def register_cli_command(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def register_skill(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class TestUnload:
    """``ctx.on_unload`` (a plugin reload, disable or remove in a running
    process): the runtime is stopped — socket, worker, timers, leader lock —
    and forgotten, so the reloaded code takes the socket and the lock."""

    def _watched(self, monkeypatch: pytest.MonkeyPatch, runtime: Any) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        monkeypatch.setattr(
            _register, "load_config", lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.example.test")
        )
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_store", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_secrets_block", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

    def test_unload_stops_the_runtime_and_forgets_it(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes import runtime as runtime_module

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        forgotten: list[Any] = []
        monkeypatch.setattr(runtime_module, "forget_runtime", lambda rt: forgotten.append(rt) or True)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        ctx = _UnloadCtx()
        _register.register(ctx)
        assert len(ctx.unloads) == 1
        runtime.stop.assert_not_called()

        ctx.unloads[0]()
        runtime.stop.assert_called_once()
        assert forgotten == [runtime]
        ctx.unloads[0]()  # a second unload changes nothing
        runtime.stop.assert_called_once()

    def test_a_runtime_started_during_the_unload_is_stopped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes._register import ActivationWatcher

        watcher = ActivationWatcher(hermes_home=tmp_path / ".hermes", profile="default", gateway_mode=True)
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())

        def start_while_unloading(cfg: Any, *, gateway_mode: bool, host_llm: Any = None) -> Any:
            watcher.close()  # the unload lands while this attempt starts the runtime
            return runtime

        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "_try_start_runtime", start_while_unloading)
        assert watcher.try_activate() == "closed"
        runtime.stop.assert_called_once()
        assert watcher.runtime is None and watcher.closed
        assert watcher.try_activate() == "closed", "no further attempts"

    def test_hermes_without_on_unload_still_loads(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        class _OldCtx:
            def register_cli_command(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def register_skill(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        _register.register(_OldCtx())
        runtime.stop.assert_not_called()

    def test_a_raising_on_unload_is_a_warning(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        class _BrokenCtx(_UnloadCtx):
            def on_unload(self, callback: Any) -> None:
                raise TypeError("on_unload callback must be callable")

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register.register(_BrokenCtx())
        assert any("unload hook" in r.getMessage() for r in caplog.records)



class TestWithoutTheKit:
    """An agentchatme without ``agentchatme.integration`` cannot receive: the
    plugin says so once, loudly, keeps the CLI (doctor says it too) and the
    manual, starts nothing, and never raises."""

    def test_register_logs_one_error_and_keeps_the_cli(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agentchatme_hermes import _register
        from agentchatme_hermes.kit import KIT_REQUIREMENT

        monkeypatch.setitem(sys.modules, "agentchatme.integration", None)

        def no_watcher(**_kw: Any) -> Any:
            raise AssertionError("nothing starts without the kit")

        monkeypatch.setattr(_register, "ActivationWatcher", no_watcher)
        ctx = MagicMock()
        with caplog.at_level(logging.DEBUG, logger="agentchatme_hermes"):
            _register.register(ctx)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, [r.getMessage() for r in errors]
        assert KIT_REQUIREMENT in errors[0].getMessage() and "not receiving" in errors[0].getMessage()
        assert ctx.register_cli_command.called, "`hermes agentchat doctor` stays reachable"
        assert ctx.register_skill.called

    def test_a_kit_without_could_not_process_is_a_problem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An agentchatme older than 1.5.0 has the kit but its ReceiveLoop
        takes no check_runtime: the receiver could not start, so it is
        reported like a missing kit."""
        import agentchatme.integration as integration

        from agentchatme_hermes.kit import KIT_REQUIREMENT, kit_problem

        assert kit_problem() is None, "the installed kit has could not process"

        class OldReceiveLoop:
            def __init__(self, client: object, texts: object, mode: str, *, call_model: object = None) -> None:
                pass

        monkeypatch.setattr(integration, "ReceiveLoop", OldReceiveLoop)
        problem = kit_problem()
        assert problem is not None and "without could not process" in problem and KIT_REQUIREMENT in problem
        assert KIT_REQUIREMENT == "agentchatme>=1.8.0"

    @pytest.mark.parametrize(
        ("installed", "said"),
        [("1.6.0", True), ("1.5.1", True), ("1.6.1", False), ("1.6.2rc1", False), ("1.7.0", False), ("not installed", False)],
    )
    def test_a_kit_whose_manual_sync_does_not_retry_is_a_doctor_warning(
        self, monkeypatch: pytest.MonkeyPatch, installed: str, said: bool
    ) -> None:
        """1.6.1's ManualSync retries while the manual is stale; an older kit
        still receives, so it is a warning, never a reason to stop."""
        from agentchatme_hermes import kit

        monkeypatch.setattr(kit, "installed_sdk_version", lambda: installed)
        problem = kit.manual_retry_problem()
        assert (problem is not None) is said
        if said:
            assert installed in problem and "agentchatme>=1.8.0" in problem

    def test_the_plugin_and_its_cli_import_without_the_kit(self) -> None:
        import os
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        script = (
            "import sys\n"
            "sys.modules['agentchatme.integration'] = None\n"
            "import agentchatme_hermes, agentchatme_hermes.cli\n"
            "from agentchatme_hermes.kit import kit_problem\n"
            "print('problem:', kit_problem())\n"
        )
        env = {**os.environ, "PYTHONPATH": str(root)}
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=root, timeout=60)
        assert done.returncode == 0, done.stderr
        assert "problem: the installed agentchatme" in done.stdout and "pip install -U" in done.stdout

    def test_doctor_fails_on_it(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        from agentchatme_hermes import cli
        from agentchatme_hermes.kit import KIT_REQUIREMENT

        monkeypatch.setitem(sys.modules, "agentchatme.integration", None)
        monkeypatch.delenv("AGENTCHAT_API_KEY", raising=False)
        monkeypatch.delenv("AGENTCHATME_API_KEY", raising=False)
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        out = capsys.readouterr().out
        assert rc >= 1 and "SDK kit missing" in out and KIT_REQUIREMENT in out


class TestKeyReachesTheHostedToolsOnly:
    """The key file reaches Hermes' MCP client through the plugin's secret
    source, registered in every Hermes process (gateway, CLI session, cron);
    the profile's ``config.yaml`` gets ``secrets.agentchat`` at activation,
    which also tells Hermes to keep ``AGENTCHAT_API_KEY`` out of the agent's
    commands — for a profile still keeping the key in ``.env`` too."""

    def test_register_hands_hermes_the_key_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from hermes_doubles import install_secret_source_base

        from agentchatme_hermes import _register

        install_secret_source_base(monkeypatch)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        sources: list[Any] = []

        class _Ctx(_UnloadCtx):
            def register_secret_source(self, source: Any) -> bool:
                sources.append(source)
                return True

        for argv in (["hermes", "chat"], ["hermes", "agentchat", "doctor"]):
            monkeypatch.setattr(sys, "argv", argv)
            _register.register(_Ctx())
        assert [s.name for s in sources] == ["agentchat", "agentchat"]

    def test_activation_places_the_block_before_the_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, placement

        order: list[str] = []
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            config=SimpleNamespace(api_base="https://api.agentchat.me"),
        )
        monkeypatch.setattr(
            _register, "load_config", lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.agentchat.me")
        )
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_store", lambda cfg: None)
        monkeypatch.setattr(
            placement, "place_secrets", lambda: order.append("secrets") or placement.PlacementStep("secrets", "ok", "")
        )
        monkeypatch.setattr(
            placement, "place_mcp", lambda api_base: order.append("mcp") or placement.PlacementStep("mcp", "ok", "")
        )
        watcher = _register.ActivationWatcher(hermes_home=tmp_path, profile="default", gateway_mode=True)
        assert watcher.try_activate() == "active"
        assert order == ["secrets", "mcp"]


class TestGatewayOnlyReconcile:
    """SOUL.md follows the manual's rule (``runtime.keeps_placements_current``):
    only a gateway-mode process refreshes or backfills it by itself. A plain
    ``doctor`` or ``status`` (any process that is not the gateway) reports it
    as found and writes nothing; placement on request stays the CLI's."""

    def _activate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, soul: Path, *, gateway: bool) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, soul_anchor

        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            gateway_mode_requested=gateway,
            identity_text=lambda: _identity(_served_line("ops"), _SERVED_TEMPLATE),
        )
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.example.test"))
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_key_store", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_secrets_block", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)
        watcher = _register.ActivationWatcher(hermes_home=tmp_path, profile="default", gateway_mode=gateway)
        assert watcher.try_activate() == "active"

    def test_a_stale_block_of_ours_is_reported_not_rewritten_then_refreshed_by_the_gateway(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import cli, soul_anchor

        soul = tmp_path / "SOUL.md"
        soul_anchor.write_soul_anchor("an older served line for @ops", handle="ops", soul_path=soul)
        before = soul.read_bytes()

        # `hermes agentchat doctor` / `status`: the activation in that process writes nothing ...
        self._activate(monkeypatch, tmp_path, soul, gateway=False)
        assert soul.read_bytes() == before
        lines: list[tuple[str, str]] = []
        cli._doctor_soul_checks(
            handle="ops", identity_line=_served_line("ops"), template=_SERVED_TEMPLATE, cmd="hermes agentchat",
            ok=lambda line: lines.append(("ok", line)), warn=lambda line: lines.append(("warn", line)),
            fail=lambda line: lines.append(("fail", line)),
        )
        assert lines and lines[0][0] == "warn" and "is not the served one" in lines[0][1]
        status = cli._identity_line_status(handle="ops", identity_line=_served_line("ops"), template=_SERVED_TEMPLATE, home=tmp_path)
        assert status is not None and status.startswith("not the served one (@ops, an older text)")
        assert soul.read_bytes() == before, "... and reports it as found"

        # The gateway keeps it current.
        self._activate(monkeypatch, tmp_path, soul, gateway=True)
        assert _served_line("ops") in soul.read_text(encoding="utf-8")
        status = cli._identity_line_status(handle="ops", identity_line=_served_line("ops"), template=_SERVED_TEMPLATE, home=tmp_path)
        assert status is not None and status.startswith("served (@ops)")

    def test_no_backfill_outside_the_gateway(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        soul = tmp_path / "SOUL.md"
        self._activate(monkeypatch, tmp_path, soul, gateway=False)
        assert not soul.exists()
        self._activate(monkeypatch, tmp_path, soul, gateway=True)
        assert _owner(soul) == "ops"

    def test_the_manual_and_soul_md_share_one_rule(self) -> None:
        """runtime.keeps_placements_current gates both: the follower's manual
        sync and the SOUL.md reconcile. A runtime that does not say is a reader."""
        import inspect as _inspect
        from types import SimpleNamespace

        from agentchatme_hermes import _register, runtime
        from agentchatme_hermes.runtime import keeps_placements_current

        assert keeps_placements_current(SimpleNamespace(gateway_mode_requested=True)) is True
        assert keeps_placements_current(SimpleNamespace(gateway_mode_requested=False)) is False
        assert keeps_placements_current(SimpleNamespace()) is False
        assert "keeps_placements_current(self)" in _inspect.getsource(runtime.Runtime.start)
        assert "keeps_placements_current(runtime)" in _inspect.getsource(_register.ActivationWatcher.try_activate)
