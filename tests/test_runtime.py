"""Tests for ``agentchatme_hermes.runtime``.

Three modes need to behave correctly:

* **Leader** (gateway_mode=True, lock acquired) — full runtime: WS
  daemon (the wake) + invoker (gate → turn) + SDK client + texts + identity.
* **Follower-gateway** (gateway_mode=True, lock NOT acquired) — light
  runtime, loud warning, no WS, no invoker. This is the path a
  second concurrent gateway hits.
* **Follower-cli** (gateway_mode=False) — light runtime, info log,
  no WS, no invoker. TUI / named CLI / one-shot.

We patch the heavy components (HTTP client, WS daemon, invoker) so
the tests can exercise the wiring without real network IO. The
leader-lock acquire path is patched per-test so we can drive both
"won the race" and "lost the race" without touching the filesystem.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_runtime_singleton() -> Any:
    """Each test gets a fresh module-level singleton."""
    from agentchatme_hermes import runtime

    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


def _config(
    *,
    api_key: str = "ac_live_test",
    hermes_home: Path | None = None,
    lock_dir: Path | None = None,
) -> Any:
    return SimpleNamespace(
        api_key=api_key,
        api_base="https://api.example.test",
        ws_url="wss://api.example.test/v1/ws",
        max_inflight_turns=2,
        hermes_home=hermes_home,
        lock_dir=lock_dir,
    )


@contextlib.contextmanager
def _patched_runtime(*, lock_winner: bool = True) -> Any:
    """Yield a context where Runtime's heavy components are mocked.

    ``lock_winner`` controls whether the patched ``try_acquire_ws_leader_lock``
    returns a fake fd (won the race → leader path) or ``None`` (lost
    the race → follower-gateway path).
    """
    from agentchatme_hermes.runtime import Runtime

    fake_client = MagicMock()
    fake_client.get_me.return_value = {"handle": "alice"}

    ws_cls = MagicMock()
    invoker_cls = MagicMock()
    acquire = MagicMock(return_value=99 if lock_winner else None)
    release = MagicMock()
    texts_cls = MagicMock()
    texts_cls.return_value.get.return_value = None

    with patch(
        "agentchatme_hermes.runtime.WSDaemon", ws_cls
    ), patch(
        "agentchatme_hermes.runtime.AgentInvoker", invoker_cls
    ), patch(
        "agentchatme_hermes.runtime.TextsStore", texts_cls
    ), patch(
        "agentchatme_hermes.runtime.try_acquire_ws_leader_lock", acquire
    ), patch(
        "agentchatme_hermes.runtime.release_leader_lock", release
    ), patch.object(
        Runtime, "_build_sync_client", return_value=fake_client
    ):
        yield ws_cls, invoker_cls, fake_client, acquire, release


class TestRuntimeStartLeaderMode:
    def test_leader_starts_ws_and_invoker(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            ws_cls, invoker_cls, fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()

            assert acquire.called, (
                "leader election must be attempted when gateway_mode=True"
            )
            assert ws_cls.called
            assert invoker_cls.called
            ws_cls.return_value.start.assert_called_once()
            invoker_cls.return_value.start.assert_called_once()

            assert rt.identity.handle == "alice"
            assert rt.client is fake_client
            assert rt.is_leader is True


class TestRuntimeStartFollowerGateway:
    """Gateway-class process that LOST the leader-lock race."""

    def test_lost_lock_skips_ws_and_invoker(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False) as (
            ws_cls, invoker_cls, _fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()

            assert acquire.called, (
                "we should still attempt the acquire so we know we lost"
            )
            assert not ws_cls.called, (
                "follower-gateway must NOT open a WS — fixes the "
                "multi-WS-per-machine bug"
            )
            assert not invoker_cls.called
            assert rt.is_leader is False
            assert rt.identity.handle == "alice"


class TestRuntimeStartFollowerCli:
    def test_cli_skips_ws_and_invoker_and_lock(self) -> None:
        """gateway_mode=False (TUI/CLI/oneshot): no WS, no lock attempt."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            ws_cls, invoker_cls, _fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()

            assert not acquire.called, (
                "CLI mode must NOT touch the leader lock — saves an "
                "open/flock syscall pair per short-lived invocation"
            )
            assert not ws_cls.called
            assert not invoker_cls.called
            assert rt.is_leader is False


class TestRuntimeStopReleasesLock:
    def test_leader_stop_releases_lock(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            _ws, _inv, _client, _acquire, release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            assert rt.is_leader is True
            rt.stop()

            # Should have released exactly the fd we acquired (99).
            release.assert_called_once_with(99)
            assert rt.is_leader is False

    def test_follower_stop_does_not_release(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False) as (
            _ws, _inv, _client, _acquire, release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            rt.stop()
            assert not release.called, (
                "follower never held the lock — releasing the wrong fd "
                "would be a bug"
            )


class TestGetRuntimeIdempotency:
    def test_returns_same_instance(self) -> None:
        from agentchatme_hermes.runtime import get_runtime

        with _patched_runtime():
            r1 = get_runtime(_config(), gateway_mode=True)
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r1 is r2

    def test_first_construction_wins(self) -> None:
        """``gateway_mode`` is honored on first construct only."""
        from agentchatme_hermes.runtime import get_runtime

        with _patched_runtime() as (ws_cls, _invoker_cls, _fake_client, _acq, _rel):
            r1 = get_runtime(_config(), gateway_mode=False)
            r1.start()
            assert not ws_cls.called

            # Second call says gateway_mode=True but we already have a
            # CLI runtime — that's intentional, the mode is fixed for
            # the process.
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r2 is r1
            assert not ws_cls.called


class TestPerAccountLockWiring:
    def test_acquire_uses_the_per_account_lock_path(self, tmp_path: Path) -> None:
        from agentchatme_hermes.leader_lock import LOCK_DIRNAME
        from agentchatme_hermes.runtime import Runtime

        root = tmp_path / ".hermes"
        with _patched_runtime(lock_winner=True) as (_ws, _inv, _client, acquire, _rel):
            rt = Runtime(_config(hermes_home=root / "profiles" / "growth"), gateway_mode=True)
            rt.start()
            acquire.assert_called_once_with(rt.lock_path)
            assert rt.lock_path.parent == root / LOCK_DIRNAME
            assert rt.hermes_home == root / "profiles" / "growth"

    def test_invoker_receives_its_runtime_and_daemon_wakes_it(self) -> None:
        """No singleton lookup inside a turn — the invoker is told who owns it,
        and the daemon's wake callback is the invoker's."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (ws_cls, invoker_cls, _client, _acq, _rel):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            assert invoker_cls.call_args.kwargs["runtime"] is rt
            assert ws_cls.call_args.kwargs["on_wake"] is invoker_cls.return_value.wake
            assert "on_group_invite" not in ws_cls.call_args.kwargs, "invites are on the tray"


class TestRuntimeRegistry:
    """One runtime per (profile, account) — the multiplexed-gateway contract."""

    def test_two_accounts_get_two_runtimes(self, tmp_path: Path) -> None:
        from agentchatme_hermes.runtime import get_runtime

        home = tmp_path / ".hermes"
        with _patched_runtime():
            growth = get_runtime(
                _config(api_key="ac_live_growth", hermes_home=home / "profiles" / "growth"),
                gateway_mode=True,
            )
            ops = get_runtime(
                _config(api_key="ac_live_ops", hermes_home=home / "profiles" / "ops"),
                gateway_mode=True,
            )
            assert growth is not ops
            assert growth.lock_path != ops.lock_path

    def test_same_account_two_profiles_two_runtimes_one_lock(self, tmp_path: Path) -> None:
        from agentchatme_hermes.runtime import get_runtime

        home = tmp_path / ".hermes"
        with _patched_runtime():
            a = get_runtime(_config(hermes_home=home / "profiles" / "a"), gateway_mode=True)
            b = get_runtime(_config(hermes_home=home / "profiles" / "b"), gateway_mode=True)
            assert a is not b, "each profile has its own SOUL.md and tools"
            assert a.lock_path == b.lock_path, "but one account elects one leader"

    def test_mode_mismatch_is_logged_not_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agentchatme_hermes.runtime import get_runtime

        with _patched_runtime(), caplog.at_level(logging.WARNING, logger="agentchatme_hermes.runtime"):
            r1 = get_runtime(_config(), gateway_mode=False)
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r1 is r2
            assert any("gateway_mode" in rec.getMessage() for rec in caplog.records)

    def test_get_existing_runtime_exact_and_ambiguous(self, tmp_path: Path) -> None:
        from agentchatme_hermes.runtime import get_existing_runtime, get_runtime

        home = tmp_path / ".hermes"
        with _patched_runtime():
            assert get_existing_runtime() is None
            cfg_a = _config(api_key="k-a", hermes_home=home / "profiles" / "a")
            a = get_runtime(cfg_a, gateway_mode=False)
            assert get_existing_runtime() is a, "single runtime is unambiguous"
            cfg_b = _config(api_key="k-b", hermes_home=home / "profiles" / "b")
            b = get_runtime(cfg_b, gateway_mode=False)
            assert get_existing_runtime(cfg_a) is a
            assert get_existing_runtime(cfg_b) is b
            assert get_existing_runtime() is None, "never guess between profiles"

    def test_reset_stops_every_runtime(self, tmp_path: Path) -> None:
        from agentchatme_hermes import runtime as runtime_module

        home = tmp_path / ".hermes"
        with _patched_runtime(lock_winner=True) as (_ws, _inv, _client, _acq, release):
            a = runtime_module.get_runtime(
                _config(api_key="k-a", hermes_home=home / "profiles" / "a"), gateway_mode=True
            )
            b = runtime_module.get_runtime(
                _config(api_key="k-b", hermes_home=home / "profiles" / "b"), gateway_mode=True
            )
            a.start()
            b.start()
            runtime_module.reset_for_tests()
            assert release.call_count == 2
            assert runtime_module.registered_runtimes() == []


class TestIntegrationOff:
    def test_gateway_with_integration_off_never_contests_the_lock(self) -> None:
        """AGENTCHATME_INTEGRATION=off: identity resolves, no socket, no invoker, no lock."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (ws_cls, invoker_cls, _client, acquire, _rel):
            cfg = _config()
            cfg.integration_enabled = False
            rt = Runtime(cfg, gateway_mode=True)
            rt.start()
            assert rt.identity.handle == "alice"
            assert rt.is_leader is False
            assert not acquire.called
            assert not ws_cls.called
            assert not invoker_cls.called
            assert rt._leader_retry_thread is None


class TestClientAndTexts:
    def test_runtime_hands_the_sdk_client_and_texts_to_the_invoker(self, tmp_path: Path) -> None:
        """One sync SDK client serves identity, tray, verdicts and texts; the
        texts store lives in the account's state dir and starts with the runtime."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (_ws, invoker_cls, fake_client, _acq, _rel), patch(
            "agentchatme_hermes.runtime.TextsStore"
        ) as texts_cls:
            texts_cls.return_value.get.return_value = None
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=True)
            rt.start()
            try:
                assert rt.client is fake_client
                assert invoker_cls.call_args.kwargs["runtime"] is rt
                assert "ledger" not in invoker_cls.call_args.kwargs
                texts_cls.assert_called_once()
                assert not (rt.state_dir / "invites.json").exists()
                assert texts_cls.call_args.kwargs["client"] is fake_client
                assert texts_cls.call_args.kwargs["cache_path"] == rt.state_dir / "texts.json"
                from agentchatme_hermes.profile_scope import account_fingerprint

                fingerprint = account_fingerprint(rt.config.api_base, rt.config.api_key)
                assert rt.state_dir == tmp_path / ".hermes" / "agentchat-state" / fingerprint
                texts_cls.return_value.start.assert_called_once()
                assert rt.texts is None and rt.identity_line() is None
            finally:
                rt.stop()
            texts_cls.return_value.stop.assert_called_once()

    def test_texts_refresh_thread_enters_this_profiles_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The texts store's refresh thread runs in this profile's HERMES_HOME
        override, so the SOUL.md reconcile it triggers reads this profile's file."""
        import contextvars
        import sys
        import types

        from agentchatme_hermes.runtime import Runtime

        override: contextvars.ContextVar[str | None] = contextvars.ContextVar("override", default=None)
        constants = types.ModuleType("hermes_constants")
        constants.set_hermes_home_override = lambda path: override.set(str(path))  # type: ignore[attr-defined]
        constants.reset_hermes_home_override = lambda token: override.reset(token)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
        home = tmp_path / ".hermes" / "profiles" / "up"
        with _patched_runtime(lock_winner=False), patch("agentchatme_hermes.runtime.TextsStore") as texts_cls:
            texts_cls.return_value.get.return_value = None
            rt = Runtime(_config(hermes_home=home), gateway_mode=False)
            rt.start()
            try:
                thread_scope = texts_cls.call_args.kwargs["thread_scope"]
                assert override.get() is None
                with thread_scope():
                    assert override.get() == str(home)
                assert override.get() is None
            finally:
                rt.stop()

    def test_stop_ends_every_thread_the_runtime_started(self, tmp_path: Path) -> None:
        """A real texts store (no network: the fake client's answer is not a
        ``Texts``, so nothing loads) starts its refresh thread with the
        runtime; ``stop()`` must end it, leaving no ``agentchat-*`` thread."""
        from agentchatme_hermes.runtime import Runtime

        fake_client = MagicMock()
        fake_client.get_me.return_value = {"handle": "alice"}
        before = {t.name for t in threading.enumerate()}
        with patch.object(Runtime, "_build_sync_client", return_value=fake_client):
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=False)
            rt.start()
            thread = rt.texts_store._thread
            assert thread is not None and thread.is_alive()
            assert rt.texts is None
            rt.stop()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        leaked = [
            t.name for t in threading.enumerate() if t.name.startswith("agentchat-") and t.name not in before
        ]
        assert leaked == []
        fake_client.close.assert_called_once()

    def test_identity_line_is_the_served_line_spelled_for_hermes(self, tmp_path: Path) -> None:
        from agentchatme.types import Texts

        from agentchatme_hermes.runtime import Runtime

        served = Texts.model_validate(
            {
                "version": "1",
                "manual": {"url": "u", "version": "v"},
                "wake_instruction": "w",
                "invite_prompt": "i",
                "identity_line": "I am @alice on AgentChat. I call agentchat_inbox first.",
                "identity_line_template": "I am @<handle> on AgentChat.",
            }
        )
        with _patched_runtime(lock_winner=False), patch(
            "agentchatme_hermes.runtime.TextsStore"
        ) as texts_cls:
            texts_cls.return_value.get.return_value = served
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=False)
            rt.start()
            try:
                assert rt.texts is served
                assert rt.identity_line() == (
                    "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first."
                )
                assert rt.identity_line_template() == "I am @<handle> on AgentChat."
                callback = MagicMock()
                rt.on_texts(callback)
                texts_cls.return_value.on_texts.assert_called_once_with(callback)
            finally:
                rt.stop()


class TestFollowerTakeover:
    def test_follower_gateway_takes_over_when_lock_frees(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leader exits; the waiting gateway wins the next election and
        starts the socket without a restart."""
        import time

        from agentchatme_hermes import runtime as runtime_module
        from agentchatme_hermes.runtime import Runtime

        monkeypatch.setattr(runtime_module, "LEADER_RETRY_INTERVAL_S", 0.01)
        with _patched_runtime(lock_winner=False) as (ws_cls, invoker_cls, _client, acquire, _rel):
            acquire.side_effect = [None, None, 99]
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            assert rt.is_leader is False
            assert not ws_cls.called

            deadline = time.monotonic() + 2.0
            while not rt.is_leader and time.monotonic() < deadline:
                time.sleep(0.01)

            assert rt.is_leader is True
            ws_cls.return_value.start.assert_called_once()
            invoker_cls.return_value.start.assert_called_once()
            assert acquire.call_count == 3
            rt.stop()

    def test_cli_follower_never_retries(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False) as (_ws, _inv, _client, acquire, _rel):
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()
            assert rt._leader_retry_thread is None
            assert not acquire.called

    def test_health_record_lives_beside_the_lock(self, tmp_path: Path) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (ws_cls, _inv, _client, _acq, _rel):
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=True)
            rt.start()
            health_path = ws_cls.call_args.kwargs["health_path"]
            assert health_path.parent == rt.lock_path.parent
            assert health_path.name == rt.lock_path.stem + ".health.json"
