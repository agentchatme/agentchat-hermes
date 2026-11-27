"""This release's texts, the default: the manual and the identity line never change from the server.

What must hold with the plugin setting ``live_texts`` off (absent, false,
anything that is not ``true``):

* the setting is read once, in ``register()``, through ``ctx.get_config``,
  for the profile that registered (``texts.read_mode`` / ``record_mode``);
* the ``agentchat:agentchat`` skill is the package's copy, in every process,
  with or without the kit; nothing downloads or writes a manual, and a file
  left in plugin storage from a time the setting was on is left as it is;
* the SOUL.md identity line is this release's template filled for the
  agent's handle by the kit — placed by the gateway at activation (no texts
  needed), re-applied on each texts change (a no-op when it matches, never
  the served line), and by every setup path, so the gateway
  and ``login`` place the same line;
* ``doctor`` and ``status`` say which texts are in use, how they compare with
  what AgentChat serves now, and whether the running gateway uses the same
  setting.

With ``live_texts`` on everything is as before 0.7.0: the modules marked with
the ``live_texts`` fixture (``test_manual.py``, ``test_soul_anchor.py``, …).
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from agentchatme.types import Texts
from fake_keys import api_key

from agentchatme_hermes import _register, cli, manual, placement, soul_anchor, texts
from agentchatme_hermes.manual import BUNDLED_MANUAL_PATH, manual_path
from agentchatme_hermes.skills import register_skill
from agentchatme_hermes.texts import IdentityText, TextsMode, identity_text, mode_for, record_mode

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = texts.parse_snapshot(texts.SNAPSHOT_PATH.read_bytes())
BUNDLE = BUNDLED_MANUAL_PATH.read_bytes()
BUNDLE_VERSION = SNAPSHOT.manual["version"]
SERVED_TEMPLATE = "I am @<handle> on AgentChat, as served today. I call mcp__agentchat__agentchat_inbox first."
OLD_MANUAL = b"---\nname: agentchat\nversion: 2.4.0\n---\nkept current while live_texts was on\n"


def _release_line(handle: str) -> str:
    """The server's rule for the line (``<handle>`` replaced), to check the kit's fill against."""
    return SNAPSHOT.identity_line_template.strip().replace("<handle>", handle)


def _served(*, template: str = SERVED_TEMPLATE, handle: str = "ops", version: str = "9.9.9") -> Texts:
    body = f"---\nname: agentchat\nversion: {version}\n---\nnewer\n".encode()
    return Texts.model_validate(
        {
            "version": "1",
            "runtime": "hermes",
            "manual": {"url": "https://agentchat.me/skill.md", "version": version, "sha256": hashlib.sha256(body).hexdigest()},
            "wake_instruction": "You were woken.",
            "invite_prompt": "An invite is pending.",
            "identity_line": template.replace("<handle>", handle),
            "identity_line_template": template,
        }
    )


class _Ctx:
    """Hermes' ``ctx`` as far as these tests use it: plugin settings and ``register_skill``."""

    def __init__(self, settings: dict[str, Any] | None = None, *, raises: Exception | None = None) -> None:
        self.settings = settings or {}
        self.raises = raises
        self.skills: list[dict[str, Any]] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.settings.get(key, default)

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        if not Path(path).exists():
            raise FileNotFoundError(path)
        self.skills.append({"name": name, "path": Path(path)})


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".hermes"
    path.mkdir()
    monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: path / "SOUL.md")
    return path


@pytest.fixture
def broken_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "texts_snapshot.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(texts, "SNAPSHOT_PATH", path)
    return path


class _Lines:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def ok(self, line: str) -> None:
        self.lines.append(("ok", line))

    def warn(self, line: str) -> None:
        self.lines.append(("warn", line))

    def fail(self, line: str) -> None:
        self.lines.append(("fail", line))

    def of(self, level: str) -> list[str]:
        return [line for lv, line in self.lines if lv == level]

    def texts(self, home: Path, *, served: Any = None, worker: Any = None) -> _Lines:
        cli._doctor_texts_checks(home, served=served, worker=worker, ok=self.ok, warn=self.warn, fail=self.fail)
        return self

    def manual(self, home: Path, *, served: Any = None) -> _Lines:
        cli._doctor_manual_checks(
            home, served=served, worker=None, cmd="hermes agentchat", ok=self.ok, warn=self.warn, fail=self.fail
        )
        return self


# ─── the setting ─────────────────────────────────────────────────────────────


class TestSetting:
    @pytest.mark.parametrize(
        ("settings", "live", "said", "problem"),
        [
            ({"live_texts": True}, True, "live_texts on", False),
            ({"live_texts": False}, False, "live_texts off", False),
            ({}, False, "not set, off by default", False),
            ({"live_texts": "yes"}, False, "'yes'", True),
            ({"live_texts": 1}, False, "not true or false", True),
        ],
    )
    def test_only_true_turns_it_on(self, settings: dict[str, Any], live: bool, said: str, problem: bool) -> None:
        mode = texts.read_mode(_Ctx(settings))
        assert mode.live is live and said in mode.detail
        assert (mode.problem is not None) is problem

    def test_a_hermes_without_plugin_settings_uses_this_release(self) -> None:
        mode = texts.read_mode(SimpleNamespace())
        assert mode.live is False and mode.problem is None and "ctx.get_config" in mode.detail

    def test_a_read_that_fails_uses_this_release_and_says_why(self) -> None:
        mode = texts.read_mode(_Ctx(raises=RuntimeError("config.yaml unreadable")))
        assert mode.live is False and mode.problem is not None and "config.yaml unreadable" in mode.problem

    def test_each_profile_keeps_its_own(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        record_mode(a, TextsMode(True, "on"))
        record_mode(b, TextsMode(False, "off"))
        assert mode_for(a).live is True and mode_for(b).live is False
        assert mode_for(tmp_path / "c").live is False, "nothing recorded: this release's texts"

    def test_register_reads_it_for_the_profile_it_registers(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
        ctx = MagicMock()
        ctx.get_config.side_effect = lambda key, default=None: True if key == "live_texts" else default
        _register.register(ctx)
        assert mode_for(home).live is True
        ctx.get_config.assert_any_call("live_texts", None)

    def test_register_warns_once_about_a_value_it_cannot_use(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
        ctx = MagicMock()
        ctx.get_config.side_effect = lambda key, default=None: "true" if key == "live_texts" else default
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes"):
            _register.register(ctx)
        said = [r.getMessage() for r in caplog.records if "live_texts" in r.getMessage()]
        assert len(said) == 1 and "'true'" in said[0] and "this release's manual and identity line" in said[0]
        assert mode_for(home).live is False

    def test_both_manifests_declare_it_the_same_way(self) -> None:
        root = (ROOT / "plugin.yaml").read_bytes()
        assert root == (ROOT / "agentchatme_hermes" / "plugin.yaml").read_bytes()
        text = root.decode("utf-8").replace("\r\n", "\n")  # a Windows checkout has CRLF
        assert (
            "config_schema:\n"
            "  live_texts:\n"
            "    type: bool\n"
            "    default: false\n"
            "    label: Keep the AgentChat manual and identity line current\n"
            "    description: >-\n"
            "      Off: your agent reads the AgentChat manual and the SOUL.md identity line\n"
            "      exactly as this plugin release ships them; a plugin update changes them.\n"
            "      On: the plugin keeps both current from agentchat.me, so they can change\n"
            "      between releases. Applies when the gateway restarts.\n"
        ) in text


# ─── the snapshot ────────────────────────────────────────────────────────────


class TestSnapshot:
    def _doc(self, **change: Any) -> bytes:
        import json

        document = json.loads(texts.SNAPSHOT_PATH.read_bytes())
        document.update(change)
        return json.dumps(document).encode()

    @pytest.mark.parametrize(
        ("change", "why"),
        [
            ({"format": 2}, "format"),
            ({"runtime": "openclaw"}, "runtime"),
            ({"identity_line_template": "I am an agent."}, "@<handle>"),
            ({"contract": ""}, "contract missing"),
            ({"manual": {"url": "u", "version": "1"}}, "sha256"),
            ({"manual": {"url": "u", "version": "1", "sha256": "ABC"}}, "64"),
        ],
    )
    def test_what_is_not_a_snapshot_is_refused(self, change: dict[str, Any], why: str) -> None:
        with pytest.raises(ValueError, match=why):
            texts.parse_snapshot(self._doc(**change))

    def test_not_json_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not JSON"):
            texts.parse_snapshot(b"{")

    def test_a_broken_snapshot_is_none_and_says_why(self, broken_snapshot: Path) -> None:
        assert texts.bundled_texts() is None
        problem = texts.bundled_texts_problem()
        assert problem is not None and str(broken_snapshot) in problem and "not JSON" in problem

    def test_a_missing_snapshot_is_none_and_says_why(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(texts, "SNAPSHOT_PATH", tmp_path / "gone.json")
        assert texts.bundled_texts() is None
        assert "cannot be read" in (texts.bundled_texts_problem() or "")


# ─── the identity line in use ────────────────────────────────────────────────


class TestIdentityText:
    def test_this_release_fills_its_template_for_the_handle_through_the_kit(self, home: Path) -> None:
        identity = identity_text(None, handle="@Ops", home=home)
        assert identity == IdentityText("release", _release_line("ops"), SNAPSHOT.identity_line_template.strip())
        assert "\n" in identity.line, "the line keeps the template's line breaks"

    def test_the_served_texts_are_not_used(self, home: Path) -> None:
        assert identity_text(_served(), handle="ops", home=home).line == _release_line("ops")

    def test_without_a_handle_there_is_no_line_but_the_template(self, home: Path) -> None:
        identity = identity_text(None, handle=None, home=home)
        assert identity.line is None and identity.template == SNAPSHOT.identity_line_template.strip()
        assert identity.missing is not None and "handle" in identity.missing

    def test_a_broken_snapshot_places_nothing_and_says_reinstall(self, home: Path, broken_snapshot: Path) -> None:
        identity = identity_text(None, handle="ops", home=home)
        assert identity.line is None and identity.missing is not None and "reinstall" in identity.missing

    @pytest.mark.usefixtures("live_texts")
    def test_live_texts_on_is_the_served_line_as_served(self, home: Path) -> None:
        served = _served()
        assert identity_text(served, handle="ops", home=home) == IdentityText(
            "served", served.identity_line, SERVED_TEMPLATE
        )
        nothing = identity_text(None, handle="ops", home=home)
        assert nothing.line is None and nothing.missing == texts.TEXTS_UNAVAILABLE_DETAIL


# ─── the manual ──────────────────────────────────────────────────────────────


class TestManual:
    @pytest.mark.parametrize("place", [True, False])
    def test_the_skill_is_the_packages_copy_and_nothing_is_written(self, home: Path, place: bool) -> None:
        ctx = _Ctx()
        assert register_skill(ctx, hermes_home=home, place=place) == BUNDLED_MANUAL_PATH
        assert ctx.skills == [{"name": "agentchat", "path": BUNDLED_MANUAL_PATH}]
        assert not (home / "plugin-data").exists()
        registration = manual.registration_for(home)
        assert registration is not None and registration.pinned and not registration.kept_current
        assert registration.as_record() == {
            "path": str(BUNDLED_MANUAL_PATH),
            "kept_current": False,
            "reason": None,
            "pinned": True,
        }

    def test_a_file_from_live_texts_on_is_left_as_it_is(self, home: Path) -> None:
        target = manual_path(home)
        target.parent.mkdir(parents=True)
        target.write_bytes(OLD_MANUAL)
        assert register_skill(_Ctx(), hermes_home=home, place=True) == BUNDLED_MANUAL_PATH
        assert target.read_bytes() == OLD_MANUAL
        assert sorted(p.name for p in target.parent.iterdir()) == ["SKILL.md"]

    def test_switching_back_on_keeps_that_file_current_again(self, home: Path) -> None:
        target = manual_path(home)
        target.parent.mkdir(parents=True)
        target.write_bytes(OLD_MANUAL)
        record_mode(home, TextsMode(True, "live_texts on"))
        assert register_skill(_Ctx(), hermes_home=home, place=True) == target, "the kept-current file, as before"

    def test_it_needs_no_kit(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "agentchatme.integration", None)
        assert register_skill(_Ctx(), hermes_home=home, place=True) == BUNDLED_MANUAL_PATH

    def test_it_is_not_a_warning(self, home: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="agentchatme_hermes"):
            register_skill(_Ctx(), hermes_home=home)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("this release's copy" in r.getMessage() for r in caplog.records)

    def test_a_missing_package_copy_registers_nothing_and_says_reinstall(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(manual, "BUNDLED_MANUAL_PATH", tmp_path / "gone" / "SKILL.md")
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes"):
            assert manual.prepare(home, place=True) is None
        assert "reinstall" in caplog.text

    @pytest.mark.usefixtures("manual_sync_on")
    def test_nothing_keeps_a_manual_current(self, home: Path) -> None:
        served = _served().manual
        assert manual.manual_sync_for(object(), home=home) is None
        assert manual.sync_in_background(home=home, served=served, api_base="https://api.agentchat.me", label="t") is None
        assert not (home / "plugin-data").exists()

    def test_the_placement_step_names_the_copy_and_writes_nothing(self, home: Path) -> None:
        step = placement.place_manual(_served().manual, api_base="https://api.agentchat.me", hermes_home=home)
        assert step.status == "ok" and BUNDLE_VERSION in step.detail and str(BUNDLED_MANUAL_PATH) in step.detail
        assert not (home / "plugin-data").exists()

    def test_the_placement_step_fails_when_the_copy_is_missing(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "BUNDLED_MANUAL_PATH", tmp_path / "gone.md")
        step = placement.place_manual(None, api_base="https://api.agentchat.me", hermes_home=home)
        assert step.failed and "reinstall" in step.detail

    @pytest.mark.usefixtures("manual_sync_on")
    def test_the_leaders_health_record_says_what_it_serves(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agentchatme

        from agentchatme_hermes.agent_invoker import AgentInvoker
        from agentchatme_hermes.receiver import Receiver
        from agentchatme_hermes.texts import texts_cache
        from agentchatme_hermes.types import AgentIdentity

        class _Api:
            base_url = "https://api.agentchat.me"

            def __init__(self, **_kw: Any) -> None:
                self.texts = SimpleNamespace(get=self._texts)
                self.inbox = SimpleNamespace(tray=self._tray)

            async def _texts(self, *, runtime: str, opts: Any = None) -> Any:
                return _served()

            async def _tray(self, **_kw: Any) -> dict[str, Any]:
                return {"version": 1, "you": "@me", "question": "q", "categories": [], "conversations": [], "deferred": 0, "truncated": False, "invites": [], "invites_truncated": False}

            async def aclose(self) -> None:
                return None

        register_skill(_Ctx(), hermes_home=home, place=True)
        monkeypatch.setattr(agentchatme, "AsyncAgentChatClient", _Api)
        invoker = AgentInvoker(config=SimpleNamespace(), identity=AgentIdentity(handle="me"), runtime=SimpleNamespace())
        receiver = Receiver(
            config=SimpleNamespace(api_key=api_key("leader"), api_base="https://api.agentchat.me", ws_url="wss://api.agentchat.me"),
            identity=AgentIdentity(handle="me"),
            invoker=invoker,
            texts_factory=lambda client: texts_cache(client, tmp_path / "state"),
            hermes_home=home,
            manual_sync_factory=lambda served, on_result: manual.manual_sync_for(served, home=home, on_result=on_result),
        )

        async def never(*_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("no network in tests")

        real_build = receiver._build_realtime_client
        monkeypatch.setattr(receiver, "_build_realtime_client", lambda: real_build(websocket_connect=never, reconnect=False))
        receiver.start()
        try:
            # The loop's texts cache has loaded the texts announcing 9.9.9.
            assert _wait(lambda: (tmp_path / "state" / texts.TEXTS_FILENAME).is_file())
            section = receiver.worker_status()["manual"]
        finally:
            receiver.stop()
        assert section["registered"]["pinned"] is True and section["last_sync"] is None
        assert not (home / "plugin-data").exists(), "the served 9.9.9 is never downloaded"


def _wait(predicate: Any, timeout: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ─── the gateway ─────────────────────────────────────────────────────────────


def _runtime(home: Path, handle: str = "ops") -> Any:
    from agentchatme_hermes.runtime import Runtime
    from agentchatme_hermes.types import AgentIdentity

    config = SimpleNamespace(
        api_key=api_key("rt"),
        api_base="https://api.agentchat.me",
        hermes_home=home,
        lock_dir=home / "locks",
        state_dir=home / "state",
    )
    runtime = Runtime(config, gateway_mode=True)  # type: ignore[arg-type]
    runtime._identity = AgentIdentity(handle=handle)
    return runtime


class TestGateway:
    def test_the_line_is_placed_at_activation_without_any_texts(self, home: Path) -> None:
        runtime = _runtime(home)
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        assert _release_line("ops") in (home / "SOUL.md").read_text(encoding="utf-8")
        assert len(runtime._texts_listeners) == 1, "re-applied on every texts change"

    def test_texts_already_loaded_place_it_once(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = _runtime(home)
        runtime._set_texts(_served())
        calls: list[Any] = []
        real = _register._reconcile_soul_anchor
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: calls.append(rt) or real(rt))
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        assert len(calls) == 1
        assert _release_line("ops") in (home / "SOUL.md").read_text(encoding="utf-8")

    def test_a_change_of_the_served_texts_never_writes_the_served_line(self, home: Path) -> None:
        runtime = _runtime(home)
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        before = (home / "SOUL.md").read_bytes()
        runtime._set_texts(_served())
        runtime._set_texts(_served(template="I am @<handle>, reworded again."))
        assert (home / "SOUL.md").read_bytes() == before, "the same line re-applied: no write"

    def test_a_texts_change_retries_a_placement_that_did_not_land(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first write met an edit in flight (the kit's ``edited_meanwhile``):
        the next texts change places this release's line."""
        runtime = _runtime(home)
        soul_anchor.place_soul_anchor(_served().identity_line, handle="ops", template=SERVED_TEMPLATE)
        real_follow = soul_anchor.follow_soul
        busy = SimpleNamespace(outcome="edited_meanwhile", path=home / "SOUL.md", claimed=None, detail="changed meanwhile")
        monkeypatch.setattr(soul_anchor, "follow_soul", lambda **_kw: busy)
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        assert _release_line("ops") not in (home / "SOUL.md").read_text(encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "follow_soul", real_follow)
        runtime._set_texts(_served())
        text = (home / "SOUL.md").read_text(encoding="utf-8")
        assert _release_line("ops") in text and _served().identity_line not in text

    def test_the_served_line_from_live_texts_on_is_brought_to_this_releases(self, home: Path) -> None:
        soul_anchor.place_soul_anchor(_served().identity_line, handle="ops", template=SERVED_TEMPLATE)
        _register._reconcile_soul_anchor(_runtime(home))
        text = (home / "SOUL.md").read_text(encoding="utf-8")
        assert _release_line("ops") in text and _served().identity_line not in text

    def test_a_block_edited_by_hand_is_left_alone(self, home: Path) -> None:
        soul = home / "SOUL.md"
        soul.write_text("<!-- agentchat:start -->\nI am @ops here, my own words.\n<!-- agentchat:end -->\n", encoding="utf-8")
        before = soul.read_bytes()
        _register._reconcile_soul_anchor(_runtime(home))
        assert soul.read_bytes() == before

    def test_the_august_2026_block_is_brought_to_this_releases_line_without_a_warning(
        self, home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lab R3 (L2-F01): a profile updated from the August 2026 build was warned
        "edited by hand" at every gateway start and kept that build's text for good."""
        from test_soul_anchor import AUGUST_BLOCK_ALICE

        soul = home / "SOUL.md"
        soul.write_text("# Persona\n\n" + AUGUST_BLOCK_ALICE.replace("@alice", "@ops") + "\n", encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes"):
            _register._reconcile_soul_anchor(_runtime(home))
        text = soul.read_text(encoding="utf-8")
        assert text.startswith("# Persona\n\n<!-- agentchat:start -->\n<!-- agentchat:handle ops -->\n")
        assert _release_line("ops") in text and "## On AgentChat" not in text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert "the block the August 2026 build wrote is replaced" in caplog.text
        caplog.clear()
        _register._reconcile_soul_anchor(_runtime(home))
        assert soul.read_text(encoding="utf-8") == text and "August" not in caplog.text, "once"

    def test_a_broken_snapshot_places_nothing_and_says_so(
        self, home: Path, broken_snapshot: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes"):
            _register._reconcile_soul_anchor(_runtime(home))
        assert not (home / "SOUL.md").exists()
        assert "reinstall" in caplog.text


class TestOneLine:
    """The gateway and every setup path place the same line, so they never undo each other."""

    def test_login_then_the_gateway_then_doctor_fix_leave_it_as_login_placed_it(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(placement, "place_secrets", lambda: placement.PlacementStep("secrets", "ok", "x"))
        monkeypatch.setattr(placement, "place_mcp", lambda api_base, decision=None: placement.PlacementStep("mcp", "ok", "x"))
        monkeypatch.setattr(cli, "resolve_texts", lambda **_kw: _served())
        cli._install_placement("ops", api_base="https://api.agentchat.me", api_key=api_key("k"))
        placed = (home / "SOUL.md").read_bytes()
        assert _release_line("ops").encode() in placed
        _register._reconcile_soul_anchor(_runtime(home))
        steps = placement.place_identity_and_tools(
            handle="ops", texts=_served(), api_base="https://api.agentchat.me", hermes_home=home, replace_other_agent=False
        )
        assert [s.status for s in steps if s.name == "anchor"] == ["ok"]
        assert (home / "SOUL.md").read_bytes() == placed

    @pytest.mark.usefixtures("live_texts")
    def test_with_live_texts_on_they_place_the_same_served_line(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = _runtime(home)
        runtime._set_texts(_served())
        step = placement.place_anchor(
            handle="ops",
            identity_line=_served().identity_line,
            template=SERVED_TEMPLATE,
        )
        assert step.status == "changed"
        placed = (home / "SOUL.md").read_bytes()
        _register._reconcile_soul_anchor(runtime)
        assert (home / "SOUL.md").read_bytes() == placed


# ─── doctor and status ───────────────────────────────────────────────────────


class TestDoctor:
    def test_the_default_says_which_texts_and_how_to_change_it(self, home: Path) -> None:
        lines = _Lines().texts(home)
        assert lines.of("warn") == [] and lines.of("fail") == []
        (said,) = lines.of("ok")
        assert "this release's manual and SOUL.md identity line" in said
        assert "hermes config set plugins.entries.agentchat.settings.live_texts true" in said

    def test_in_a_named_profile_the_command_names_the_profile(self, tmp_path: Path) -> None:
        """Lab R3 (L1-F05): without ``-p`` the command sets the default profile's setting."""
        named = tmp_path / ".hermes" / "profiles" / "t5tmp"
        named.mkdir(parents=True)
        (said,) = _Lines().texts(named).of("ok")
        assert "`hermes -p t5tmp config set plugins.entries.agentchat.settings.live_texts true`" in said
        record_mode(named, texts.read_mode(_Ctx({"live_texts": "yes"})))
        (warned,) = _Lines().texts(named).of("warn")
        assert "`hermes -p t5tmp config set plugins.entries.agentchat.settings.live_texts true`" in warned

    def test_on_says_kept_current(self, home: Path) -> None:
        record_mode(home, TextsMode(True, "live_texts on"))
        assert _Lines().texts(home).of("ok") == [
            "texts: the manual and the SOUL.md identity line are kept current from agentchat.me (live_texts on)"
        ]

    def test_a_value_that_is_not_true_or_false_is_a_warning(self, home: Path) -> None:
        record_mode(home, texts.read_mode(_Ctx({"live_texts": "yes"})))
        (said,) = _Lines().texts(home).of("warn")
        assert "'yes'" in said and "this release's" in said

    def test_a_different_served_identity_line_is_said_not_warned(self, home: Path) -> None:
        lines = _Lines().texts(home, served=_served())
        assert lines.of("warn") == []
        assert any("serves a different identity line" in line for line in lines.of("ok"))
        quiet = _Lines().texts(home, served=_served(template=SNAPSHOT.identity_line_template))
        assert not any("different identity line" in line for line in quiet.of("ok"))

    def test_a_broken_snapshot_fails(self, home: Path, broken_snapshot: Path) -> None:
        (said,) = _Lines().texts(home).of("fail")
        assert "reinstall" in said

    @pytest.mark.parametrize(
        ("registered", "live", "warned"),
        [
            ({"pinned": False, "kept_current": True}, False, "uses the served manual"),
            ({"kept_current": True}, False, "uses the served manual"),  # a gateway before 0.7.0
            ({"pinned": True, "kept_current": False}, True, "uses this release's"),
            ({"pinned": True, "kept_current": False}, False, None),
            ({"pinned": False, "kept_current": True}, True, None),
        ],
    )
    def test_a_gateway_on_the_other_setting_is_told_to_restart(
        self, home: Path, registered: dict[str, Any], live: bool, warned: str | None
    ) -> None:
        record_mode(home, TextsMode(live, "test"))
        lines = _Lines().texts(home, worker={"manual": {"registered": registered}})
        if warned is None:
            assert lines.of("warn") == []
        else:
            (said,) = lines.of("warn")
            assert warned in said and "hermes gateway restart" in said
            assert lines.of("ok") == [], "one line for one cause (lab R3, L1-F06)"

    def test_the_manual_is_this_releases_copy(self, home: Path) -> None:
        register_skill(_Ctx(), hermes_home=home)
        lines = _Lines().manual(home)
        assert lines.of("warn") == [] and lines.of("fail") == []
        assert lines.of("ok") == [f"manual: this release's copy {BUNDLE_VERSION} — {BUNDLED_MANUAL_PATH}"]

    def test_a_newer_announced_manual_is_said_not_warned(self, home: Path) -> None:
        lines = _Lines().manual(home, served=_served().manual)
        assert lines.of("warn") == []
        assert any(f"AgentChat announces manual 9.9.9; this release carries {BUNDLE_VERSION}" in line for line in lines.of("ok"))

    def test_a_changed_copy_fails_with_reinstall(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changed = tmp_path / "SKILL.md"
        changed.write_bytes(BUNDLE.replace(b"AgentChat", b"AgentChaT", 1))
        monkeypatch.setattr(manual, "BUNDLED_MANUAL_PATH", changed)
        (said,) = _Lines().manual(home).of("fail")
        assert "tampered" in said and "reinstall" in said

    def test_a_kept_current_file_left_behind_is_named_as_unused(self, home: Path) -> None:
        target = manual_path(home)
        target.parent.mkdir(parents=True)
        target.write_bytes(OLD_MANUAL)
        assert any(f"{target} (kept current while live_texts was on)" in line for line in _Lines().manual(home).of("ok"))

    def test_the_identity_line_is_checked_against_this_releases(self, home: Path) -> None:
        identity = identity_text(None, handle="ops", home=home)
        assert identity.line is not None
        soul_anchor.place_soul_anchor(identity.line, handle="ops", template=identity.template)
        lines = _Lines()
        cli._doctor_soul_checks(
            handle="ops", identity_line=identity.line, template=identity.template, source=identity.source,
            cmd="hermes agentchat", ok=lines.ok, warn=lines.warn, fail=lines.fail,
        )
        assert lines.of("ok") == ["SOUL.md identity line present (@ops, this release text)"]

    def test_the_served_line_in_soul_md_is_not_this_releases(self, home: Path) -> None:
        soul_anchor.place_soul_anchor(_served().identity_line, handle="ops", template=SERVED_TEMPLATE)
        identity = identity_text(_served(), handle="ops", home=home)
        lines = _Lines()
        cli._doctor_soul_checks(
            handle="ops", identity_line=identity.line, template=identity.template, source=identity.source,
            cmd="hermes agentchat", ok=lines.ok, warn=lines.warn, fail=lines.fail,
        )
        (said,) = lines.of("warn")
        assert "not this release's text" in said and "doctor --fix" in said

    def test_status_says_the_same(self, home: Path) -> None:
        assert cli._manual_line(home, _served().manual) == (
            f"this release's copy {BUNDLE_VERSION} — {BUNDLED_MANUAL_PATH}; AgentChat announces 9.9.9"
        )
        assert cli._identity_status_for(None, handle="ops", home=home) == f"missing — {home / 'SOUL.md'} (`hermes agentchat doctor --fix` or `hermes agentchat login` places it)"
        identity = identity_text(None, handle="ops", home=home)
        assert identity.line is not None
        soul_anchor.place_soul_anchor(identity.line, handle="ops", template=identity.template)
        assert cli._identity_status_for(_served(), handle="ops", home=home) == f"this release (@ops) — {home / 'SOUL.md'}"


def test_doctor_on_a_clean_profile_passes_with_this_releases_texts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``hermes agentchat doctor`` end to end, the default setting: Hermes ran
    ``register()`` first (it read the setting and registered the manual)."""
    import argparse
    import types as _types

    from key_files import save_key

    from agentchatme_hermes import key_source, kit, mcp_registration, secrets_registration
    from agentchatme_hermes.profile_scope import resolve_hermes_home

    save_key(api_key("xyz"))
    fake_client = MagicMock()
    fake_client.get_me.return_value = {
        "handle": "alice",
        "status": "active",
        "settings": {"inbox_mode": "open"},
        "paused_by_owner": "none",
    }
    fake_sdk = _types.ModuleType("agentchatme")
    fake_sdk.AgentChatClient = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
    fake_sdk.AgentChatClientIdentity = MagicMock  # type: ignore[attr-defined]
    fake_sdk.AgentChatError = Exception  # type: ignore[attr-defined]
    fake_sdk.UnauthorizedError = Exception  # type: ignore[attr-defined]
    fake_sdk.ConnectionError = type("ConnectionError", (Exception,), {})  # type: ignore[attr-defined]
    home = resolve_hermes_home()
    record_mode(home, texts.read_mode(_Ctx()))
    register_skill(_Ctx(), hermes_home=home)
    monkeypatch.setitem(sys.modules, "agentchatme", fake_sdk)
    monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)
    monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
    monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: ("ours", "current"))
    monkeypatch.setattr(secrets_registration, "inspect_block", lambda: "ours")
    monkeypatch.setattr(key_source, "source_supported", lambda: True)
    monkeypatch.setattr(kit, "kit_problem", lambda: None)

    rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
    out = capsys.readouterr().out
    assert "[ok]   texts: this release's manual and SOUL.md identity line (live_texts not set, off by default)" in out
    assert f"[ok]   manual: this release's copy {BUNDLE_VERSION} — {BUNDLED_MANUAL_PATH}" in out
    assert "Hermes serves agentchat:agentchat from" not in out, "said only when it is not this release's copy"
    assert "[fail]" not in out
    assert rc == 0, out


class TestLogout:
    """``hermes agentchat logout`` with the default setting: the block holding
    this release's line is the one AgentChat placed, so it goes."""

    def _profile(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from key_files import save_key

        from agentchatme_hermes import mcp_registration, secrets_registration

        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(cli, "_uninstall_secrets_registration", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_mcp_registration", lambda: None)
        monkeypatch.setattr(secrets_registration, "uninstall_block", lambda *a, **k: False)
        monkeypatch.setattr(mcp_registration, "uninstall_entry", lambda *a, **k: False)
        save_key(api_key("logout-release"), handle="alice", home=home)
        soul = home / "SOUL.md"
        soul.write_text("# Persona\n", encoding="utf-8")
        return soul

    def _logout(self) -> int:
        import argparse

        return cli._dispatch_logout(argparse.Namespace())

    def test_the_block_login_placed_is_removed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        soul = self._profile(home, monkeypatch)
        identity = identity_text(None, handle="alice", home=home)
        assert identity.line is not None
        soul_anchor.write_soul_anchor(identity.line, handle="alice", soul_path=soul)
        assert self._logout() == 0
        assert f"SOUL.md: removed the identity line for @alice from {soul}." in capsys.readouterr().out
        assert soul.read_text(encoding="utf-8") == "# Persona\n"

    def test_this_releases_text_without_a_handle_line_is_recognised(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No handle line: the block is AgentChat's because its text is exactly
        this release's template filled in (``profile_identity`` hands the kit
        that template)."""
        soul = self._profile(home, monkeypatch)
        soul.write_text(
            f"# Persona\n\n<!-- agentchat:start -->\n{_release_line('alice')}\n<!-- agentchat:end -->\n", encoding="utf-8"
        )
        identity = placement.profile_identity(home, api_base="https://api.agentchat.me")
        assert identity.template == SNAPSHOT.identity_line_template.strip()
        assert self._logout() == 0
        assert "removed the identity line for @alice" in capsys.readouterr().out
        assert "agentchat:start" not in soul.read_text(encoding="utf-8")
