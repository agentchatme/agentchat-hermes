"""Tests for ``agentchatme_hermes.profile_scope``.

Hermes itself is not importable in the test environment, so the module's
no-Hermes path is what runs by default. The Hermes-present paths are
exercised with a stand-in ``agent.secret_scope`` (``tests/hermes_doubles.py``);
the real one is ``test_hermes_contract.py``'s.
"""
from __future__ import annotations

import contextvars
import sys
import threading
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from hermes_doubles import FakeSecretScope

from agentchatme_hermes.profile_scope import (
    ProfileScope,
    ProfileScopeError,
    account_fingerprint,
    context_thread,
    hermes_root_of,
    profile_label,
    resolve_hermes_home,
    resolve_setting,
    set_setting_resolver_for_tests,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_override() -> Any:
    set_setting_resolver_for_tests(None)
    yield
    set_setting_resolver_for_tests(None)


class TestResolveHermesHome:
    def test_env_var_wins_without_hermes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "growth"))
        assert resolve_hermes_home() == tmp_path / "profiles" / "growth"

    def test_default_is_the_platform_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``~/.hermes`` on POSIX, ``%LOCALAPPDATA%\\hermes`` on Windows, as Hermes
        computes it (tests/test_owner_only.py covers both on every platform)."""
        import pathlib
        import sys

        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        expected = tmp_path / "Local" / "hermes" if sys.platform == "win32" else tmp_path / ".hermes"
        assert resolve_hermes_home() == expected


class TestRootAndLabel:
    def test_profile_home_rolls_up_to_root(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        assert hermes_root_of(root / "profiles" / "growth") == root
        assert profile_label(root / "profiles" / "growth") == "growth"

    def test_default_home_is_its_own_root(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        assert hermes_root_of(root) == root
        assert profile_label(root) == "default"

    def test_custom_root_without_profiles_dir(self, tmp_path: Path) -> None:
        custom = tmp_path / "hermes-data"
        assert hermes_root_of(custom) == custom
        assert profile_label(custom) == "default"


class TestAccountFingerprint:
    def test_stable_and_short(self) -> None:
        a = account_fingerprint("https://api.agentchat.me", api_key("abc"))
        assert a == account_fingerprint("https://api.agentchat.me", api_key("abc"))
        assert len(a) == 16
        int(a, 16)  # hex

    def test_differs_by_key_and_by_server(self) -> None:
        base = "https://api.agentchat.me"
        assert account_fingerprint(base, "k1") != account_fingerprint(base, "k2")
        assert account_fingerprint(base, "k1") != account_fingerprint(
            "https://staging.example", "k1"
        )

    def test_trailing_slash_on_base_is_ignored(self) -> None:
        assert account_fingerprint("https://api.agentchat.me/", "k") == account_fingerprint(
            "https://api.agentchat.me", "k"
        )

    def test_not_the_raw_key(self) -> None:
        assert api_key("secret") not in account_fingerprint("https://x", api_key("secret"))


class TestResolveSetting:
    def test_falls_back_to_environ_without_hermes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
        monkeypatch.delitem(sys.modules, "agent", raising=False)
        monkeypatch.setenv("AGENTCHATME_HANDLE", "from-env")
        assert resolve_setting("AGENTCHATME_HANDLE") == "from-env"
        monkeypatch.delenv("AGENTCHATME_HANDLE")
        assert resolve_setting("AGENTCHATME_HANDLE") is None

    def test_hermes_scope_wins_over_process_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The multiplexing fix: the bound profile's value, never os.environ."""
        monkeypatch.setenv("AGENTCHATME_HANDLE", "launch-profile")
        fake = FakeSecretScope(multiplex=True, by_home={tmp_path: {"AGENTCHATME_HANDLE": "growth"}})
        fake.install(monkeypatch)
        assert ProfileScope.capture(tmp_path).run(resolve_setting, "AGENTCHATME_HANDLE") == "growth"

    def test_unscoped_under_multiplexing_is_a_typed_failure_never_a_file_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No fallback reader: a read outside the scope while the gateway
        multiplexes is a plugin path that did not enter it, and fails."""
        FakeSecretScope(multiplex=True).install(monkeypatch)
        monkeypatch.setenv("AGENTCHATME_HANDLE", "must-not-leak")
        with pytest.raises(ProfileScopeError, match="outside this profile's scope"):
            resolve_setting("AGENTCHATME_HANDLE")

    def test_override_resolver_for_tests(self) -> None:
        set_setting_resolver_for_tests(lambda name: {"AGENTCHATME_HANDLE": "o"}.get(name))
        assert resolve_setting("AGENTCHATME_HANDLE") == "o"
        assert resolve_setting("OTHER") is None


_PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")


class TestProfileScope:
    def test_runs_in_the_captured_context(self, tmp_path: Path) -> None:
        token = _PROBE.set("register's")
        try:
            scope = ProfileScope.capture(tmp_path)
        finally:
            _PROBE.reset(token)
        assert _PROBE.get() == "unset"
        assert scope.run(_PROBE.get) == "register's"
        assert scope.home == tmp_path

    def test_a_fresh_copy_per_call(self, tmp_path: Path) -> None:
        """Nothing one call sets leaks into the next (each runs in its own copy)."""
        scope = ProfileScope.capture(tmp_path)
        scope.run(_PROBE.set, "first call")
        assert scope.run(_PROBE.get) == "unset"

    def test_two_threads_run_at_once(self, tmp_path: Path) -> None:
        """A Context cannot be entered twice at once: each call copies it."""
        scope = ProfileScope.capture(tmp_path)
        inside = threading.Barrier(2, timeout=5.0)
        seen: list[str] = []

        def body(name: str) -> None:
            _PROBE.set(name)
            inside.wait()
            seen.append(_PROBE.get())

        threads = [scope.thread(lambda n=n: body(n), name=f"agentchat-test-{n}") for n in ("a", "b")]
        for thread in threads:
            assert thread.daemon
            thread.start()
        for thread in threads:
            thread.join(5.0)
        assert sorted(seen) == ["a", "b"]

    def test_outside_hermes_nothing_is_bound(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
        monkeypatch.delitem(sys.modules, "agent", raising=False)
        assert ProfileScope.capture(tmp_path).run(lambda: 42) == 42

    def test_a_single_profile_process_binds_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """As Hermes' own standalone gateway turns: the process env is the profile's."""
        fake = FakeSecretScope(multiplex=False)
        fake.install(monkeypatch)
        monkeypatch.setenv("AGENTCHATME_HANDLE", "own")
        assert ProfileScope.capture(tmp_path).run(resolve_setting, "AGENTCHATME_HANDLE") == "own"
        assert fake.binds == [] and fake.refreshed == []

    def test_multiplexed_binds_the_profiles_secrets_for_the_call_and_resets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = FakeSecretScope(multiplex=True, by_home={tmp_path: {"OPENAI_API_KEY": "own-key"}})
        fake.install(monkeypatch)
        scope = ProfileScope.capture(tmp_path)
        seen = scope.run(lambda: fake.get_secret("OPENAI_API_KEY"))
        assert seen == "own-key"
        # One empty mapping stamped with the home, filled by Hermes, reset after.
        assert fake.binds == [str(tmp_path)]
        assert fake.refreshed == [str(tmp_path)]
        assert fake.resets == 1
        assert fake.bound.get() is None
        # A miss is a miss: never the process environment.
        monkeypatch.setenv("OTHER_KEY", "launch-only")
        assert scope.run(lambda: fake.get_secret("OTHER_KEY")) is None

    def test_rebound_on_every_call(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A rotated key is seen at the next call, as the gateway rebinds per turn."""
        fake = FakeSecretScope(multiplex=True, by_home={tmp_path: {"K": "old"}})
        fake.install(monkeypatch)
        scope = ProfileScope.capture(tmp_path)
        assert scope.run(lambda: fake.get_secret("K")) == "old"
        fake.by_home[fake._key(tmp_path)]["K"] = "new"
        assert scope.run(lambda: fake.get_secret("K")) == "new"

    def test_reset_even_when_the_call_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake = FakeSecretScope(multiplex=True)
        fake.install(monkeypatch)

        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            ProfileScope.capture(tmp_path).run(boom)
        assert fake.resets == 1

    @pytest.mark.parametrize("failure", ["set_raises", "refresh_empty", "missing_api"])
    def test_a_scope_that_cannot_be_bound_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
    ) -> None:
        fake = FakeSecretScope(multiplex=True)
        module = fake.install(monkeypatch)
        if failure == "set_raises":
            fake.fail_set = RuntimeError("hosted scope")
        elif failure == "refresh_empty":
            fake.refresh_fills = False
        else:
            monkeypatch.delattr(module, "refresh_installed_secret_scope")
        ran: list[bool] = []
        with pytest.raises(ProfileScopeError):
            ProfileScope.capture(tmp_path).run(lambda: ran.append(True))
        assert ran == []
        assert fake.bound.get() is None
        if failure == "refresh_empty":
            assert fake.resets == 1

    def test_a_thread_runs_in_the_captured_context_and_binds_nothing_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failing bind fails the entry point the thread calls, never the thread."""
        fake = FakeSecretScope(multiplex=True)
        fake.install(monkeypatch)
        fake.fail_set = RuntimeError("hosted scope")
        token = _PROBE.set("register's")
        try:
            scope = ProfileScope.capture(tmp_path)
        finally:
            _PROBE.reset(token)
        seen: list[Any] = []

        def body() -> None:
            seen.append(_PROBE.get())
            try:
                scope.run(lambda: None)
            except ProfileScopeError:
                seen.append("entry point refused")

        thread = scope.thread(body, name="agentchat-test-thread")
        thread.start()
        thread.join(5.0)
        assert seen == ["register's", "entry point refused"]
        assert fake.binds == []

    def test_context_thread_inherits_the_callers_context(self) -> None:
        token = _PROBE.set("the turn's scope")
        try:
            seen: list[str] = []
            thread = context_thread(lambda: seen.append(_PROBE.get()), name="agentchat-test-worker")
        finally:
            _PROBE.reset(token)
        thread.start()
        thread.join(5.0)
        assert seen == ["the turn's scope"] and thread.daemon


class TestLaunchProfileLoadedBeforeMultiplexing:
    def test_a_scope_captured_before_activation_binds_once_the_gateway_multiplexes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The launch profile's plugins load while the gateway does not multiplex
        yet; the scope follows the process's state at each call, not at capture."""
        fake = FakeSecretScope(multiplex=False, by_home={tmp_path: {"K": "launch"}})
        fake.install(monkeypatch)
        scope = ProfileScope.capture(tmp_path)  # register(), before activation
        monkeypatch.setenv("K", "process-env")
        assert scope.run(lambda: fake.get_secret("K")) == "process-env" and fake.binds == []
        fake.multiplex = True  # the gateway started hosting a second profile
        assert scope.run(lambda: fake.get_secret("K")) == "launch"
        assert fake.binds == [str(tmp_path)]


class TestAnOlderHermes:
    """Hermes before 0.23.2 rebuilds a plugin's scope from the profile's files
    only: warned (once per process), never refused."""

    def test_the_probe_reads_the_refresh_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import profile_scope

        FakeSecretScope().install(monkeypatch)
        assert profile_scope.refresh_keeps_launch_env() is True
        profile_scope._keeps_launch_env.clear()
        FakeSecretScope(files_only=True).install(monkeypatch)
        assert profile_scope.refresh_keeps_launch_env() is False
        profile_scope._keeps_launch_env.clear()
        monkeypatch.setitem(sys.modules, "agent", None)  # not importable: not Hermes
        assert profile_scope.refresh_keeps_launch_env() is None

    def test_it_is_warned_once_and_the_call_still_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from agentchatme_hermes import profile_scope

        monkeypatch.setattr(profile_scope, "_warned", set())
        fake = FakeSecretScope(multiplex=True, files_only=True, by_home={tmp_path: {"K": "own"}})
        fake.install(monkeypatch)
        scope = ProfileScope.capture(tmp_path)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.profile_scope"):
            assert scope.run(lambda: fake.get_secret("K")) == "own"
            assert scope.run(lambda: fake.get_secret("K")) == "own"
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "older than 0.23.2" in warnings[0] and "update Hermes" in warnings[0]
        assert profile_scope.scope_status() == {"multiplexed": True, "keeps_launch_env": False}

    def test_doctor_warns_from_the_running_gateways_record(self) -> None:
        from agentchatme_hermes.cli import _describe_scope

        line = _describe_scope({"scope": {"multiplexed": True, "keeps_launch_env": False}})
        assert line is not None and "older than 0.23.2" in line and "update Hermes" in line
        assert _describe_scope({"scope": {"multiplexed": True, "keeps_launch_env": True}}) is None
        assert _describe_scope({"scope": {"multiplexed": False, "keeps_launch_env": None}}) is None
        assert _describe_scope({}) is None
