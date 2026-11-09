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
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from fake_keys import api_key

# The tests that take the real leader lock: fcntl is POSIX-only, as in
# test_leader_lock.py (the plugin receives only where Hermes' gateway runs).
_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the real leader lock is fcntl, POSIX-only")

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
    api_key: str = api_key("test"),
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

    ws_cls = MagicMock()  # the receiver (socket + receive loop)
    invoker_cls = MagicMock()
    # No turn running: an idle invoker runs the callback at once.
    invoker_cls.return_value.when_idle.side_effect = lambda callback: callback() or True
    acquire = MagicMock(return_value=99 if lock_winner else None)
    release = MagicMock()

    with patch(
        "agentchatme_hermes.runtime.Receiver", ws_cls
    ), patch(
        "agentchatme_hermes.runtime.AgentInvoker", invoker_cls
    ), patch(
        "agentchatme_hermes.runtime.load_texts", MagicMock(return_value=None)
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
            invoker_cls.return_value.start.assert_not_called()  # the receiver runs it; it has no thread

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
            try:
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
            finally:
                rt.stop()  # ends the leader-retry thread while the lock is still mocked


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
            assert ws_cls.call_args.kwargs["invoker"] is invoker_cls.return_value
            assert "on_group_invite" not in ws_cls.call_args.kwargs, "invites are on the tray"
            assert ws_cls.call_args.kwargs["hermes_home"] == rt.hermes_home


class TestRuntimeRegistry:
    """One runtime per (profile, account) — the multiplexed-gateway contract."""

    def test_two_accounts_get_two_runtimes(self, tmp_path: Path) -> None:
        from agentchatme_hermes.runtime import get_runtime

        home = tmp_path / ".hermes"
        with _patched_runtime():
            growth = get_runtime(
                _config(api_key=api_key("growth"), hermes_home=home / "profiles" / "growth"),
                gateway_mode=True,
            )
            ops = get_runtime(
                _config(api_key=api_key("ops"), hermes_home=home / "profiles" / "ops"),
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


_SERVED = {
    "version": "1",
    "manual": {"url": "u", "version": "v"},
    "wake_instruction": "w",
    "invite_prompt": "i",
    "identity_line": "I am @alice on AgentChat. I call mcp__agentchat__agentchat_inbox first.",
    "identity_line_template": "I am @<handle> on AgentChat.",
}


class TestClientAndTexts:
    def test_runtime_hands_the_sdk_client_and_the_texts_cache_to_the_receiver(self, tmp_path: Path) -> None:
        """One sync SDK client serves identity and the start-time texts load;
        the receiver gets a texts cache in the account's state dir and the
        runtime's hub for changes; the old state files are removed."""
        from agentchatme_hermes.runtime import Runtime

        home = tmp_path / ".hermes"
        with _patched_runtime(lock_winner=True) as (ws_cls, invoker_cls, fake_client, _acq, _rel):
            rt = Runtime(_config(hermes_home=home), gateway_mode=True)
            rt.state_dir.mkdir(parents=True)
            (rt.state_dir / "texts.json").write_text("{}", encoding="utf-8")
            (rt.state_dir / "invites.json").write_text("{}", encoding="utf-8")
            rt.start()
            try:
                assert rt.client is fake_client
                assert invoker_cls.call_args.kwargs["runtime"] is rt
                assert "ledger" not in invoker_cls.call_args.kwargs
                assert not (rt.state_dir / "invites.json").exists()
                assert not (rt.state_dir / "texts.json").exists()
                cache = ws_cls.call_args.kwargs["texts_factory"](fake_client)
                assert cache.runtime == "hermes" and cache.file_path == rt.state_dir / "texts-hermes.json"
                assert ws_cls.call_args.kwargs["on_texts"] == rt._set_texts
                from agentchatme_hermes.profile_scope import account_fingerprint

                fingerprint = account_fingerprint(rt.config.api_base, rt.config.api_key)
                assert rt.state_dir == home / "agentchat-state" / fingerprint
                assert rt.texts is None and rt.identity_line() is None
            finally:
                rt.stop()
            ws_cls.return_value.stop.assert_called_once()

    def test_texts_hub_tells_listeners_of_real_changes(self, tmp_path: Path) -> None:
        from agentchatme.types import Texts

        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False):
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=False)
            rt.start()
            try:
                seen: list[str] = []
                rt.on_texts(lambda texts: seen.append(texts.identity_line))
                assert seen == [], "nothing loaded yet"
                served = Texts.model_validate(_SERVED)
                rt._set_texts(served)
                rt._set_texts(Texts.model_validate(_SERVED))  # the same again: no news
                rt._set_texts(None)  # a failed refresh changes nothing
                assert seen == [_SERVED["identity_line"]]
                late: list[str] = []
                rt.on_texts(lambda texts: late.append(texts.identity_line))
                assert late == [_SERVED["identity_line"]], "a late listener hears the current texts at once"
                rt._set_texts(Texts.model_validate({**_SERVED, "identity_line": "I am @alice, reworded."}))
                assert seen[-1] == late[-1] == "I am @alice, reworded."
            finally:
                rt.stop()

    def test_stop_ends_every_thread_the_runtime_started(self, tmp_path: Path) -> None:
        """A follower starts no thread of its own; ``stop()`` closes the client."""
        from agentchatme_hermes.runtime import Runtime

        fake_client = MagicMock()
        fake_client.get_me.return_value = {"handle": "alice"}
        before = {t.name for t in threading.enumerate()}
        with patch.object(Runtime, "_build_sync_client", return_value=fake_client):
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=False)
            rt.start()
            assert rt.texts is None, "the fake client serves no Texts"
            rt.stop()
        leaked = [
            t.name for t in threading.enumerate() if t.name.startswith("agentchat-") and t.name not in before
        ]
        assert leaked == []
        fake_client.close.assert_called_once()

    def test_identity_line_is_the_served_line_as_served(self, tmp_path: Path) -> None:
        from agentchatme.types import Texts

        from agentchatme_hermes.runtime import Runtime

        served = Texts.model_validate(_SERVED)
        with _patched_runtime(lock_winner=False), patch(
            "agentchatme_hermes.runtime.load_texts", MagicMock(return_value=served)
        ):
            rt = Runtime(_config(hermes_home=tmp_path / ".hermes"), gateway_mode=False)
            rt.start()
            try:
                assert rt.texts is served
                assert rt.identity_line() == _SERVED["identity_line"]
                assert rt.identity_line_template() == "I am @<handle> on AgentChat."
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
            invoker_cls.return_value.start.assert_not_called()  # the receiver runs it; it has no thread
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


class TestUnloadedRuntime:
    def test_a_stopped_leader_releases_everything_and_is_forgotten(self) -> None:
        """What the plugin's unload does to a real leader runtime: the socket
        and the worker are stopped, the leader lock is released, and the
        registry builds a fresh runtime for the reloaded code."""
        from agentchatme_hermes import runtime as runtime_module

        with _patched_runtime(lock_winner=True) as (ws_cls, invoker_cls, fake_client, _acquire, release):
            rt = runtime_module.get_runtime(_config(), gateway_mode=True)
            rt.start()
            assert rt.is_leader
            from agentchatme_hermes._register import _shutdown_runtime

            _shutdown_runtime(rt, profile="default")
            ws_cls.return_value.stop.assert_called_once()
            invoker_cls.return_value.stop.assert_called_once()
            fake_client.close.assert_called_once()
            release.assert_called_once_with(99)
            assert not rt.is_leader
            assert runtime_module.forget_runtime(rt) is False, "already forgotten"
            fresh = runtime_module.get_runtime(_config(), gateway_mode=True)
            assert fresh is not rt
            runtime_module.forget_runtime(fresh)


@_posix_only
class TestReloadWithALiveTurn:
    """A plugin reload (or stop) while an AgentChat turn runs: the old
    instance keeps the account's leader lock until that turn's thread has
    ended, so the reloaded instance stays a follower — no receiver, no second
    turn for the conversation the old turn may still answer — and takes over
    the moment it ends. Real lock file, real invoker, a turn that ignores
    Hermes' interrupt."""

    def test_the_new_instance_waits_for_the_old_turn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        import time

        from agentchatme.integration import TurnRequest

        from agentchatme_hermes import hosted_tools as hosted_tools_module
        from agentchatme_hermes import runtime as runtime_module
        from agentchatme_hermes import turn_runner as turn_runner_module
        from agentchatme_hermes.leader_lock import try_acquire_ws_leader_lock
        from agentchatme_hermes.runtime import Runtime

        monkeypatch.setattr(runtime_module, "LEADER_RETRY_INTERVAL_S", 0.01)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])
        released = threading.Event()
        started = threading.Event()

        class _StuckAgent:
            def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
                started.set()
                released.wait(10.0)
                return {"final_response": "", "messages": []}

            def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
                return None  # it does not stop

        fake_client = MagicMock()
        fake_client.get_me.return_value = {"handle": "alice"}
        receivers: list[Any] = []

        def make_receiver(**_kw: Any) -> Any:
            receivers.append(MagicMock())
            return receivers[-1]

        config = _config(hermes_home=tmp_path / ".hermes", lock_dir=tmp_path / "locks")
        loop = asyncio.new_event_loop()
        started_runtimes: list[Any] = []
        try:
            with patch("agentchatme_hermes.runtime.Receiver", side_effect=make_receiver), patch(
                "agentchatme_hermes.runtime.load_texts", MagicMock(return_value=None)
            ), patch.object(Runtime, "_build_sync_client", return_value=fake_client):
                old = Runtime(config, gateway_mode=True)
                started_runtimes.append(old)
                old.start()
                assert old.is_leader and len(receivers) == 1
                inv = old._invoker
                assert inv is not None
                inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=0.05, poll_s=0.01)
                monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
                monkeypatch.setattr(inv, "_build_agent", lambda _sid: _StuckAgent())
                async def start() -> Any:
                    # Built on the loop: Python 3.9 binds an asyncio.Event when it is made.
                    request = TurnRequest(
                        kind="reply", prompt="p", texts=MagicMock(), stopping=asyncio.Event(), conversation_id="conv_a",
                        card={"conversation_id": "conv_a"}, anchor="msg_a", category="open_request", invite=None,
                    )
                    return await inv.start_turn(request)

                admission = loop.run_until_complete(start())
                assert admission.started and started.wait(5.0)
                # What the real receiver's teardown does with a running turn.
                receivers[0].stop.side_effect = inv.detach_turns

                old.stop()  # the reload: the old code is unloaded mid-turn
                assert try_acquire_ws_leader_lock(old.lock_path) is None, "the lock is still held"
                new = Runtime(config, gateway_mode=True)
                started_runtimes.append(new)
                new.start()
                time.sleep(0.2)
                assert not new.is_leader and len(receivers) == 1, "the new instance stays a follower: no second turn"
                fake_client.close.assert_not_called()

                released.set()  # the old turn finally ends
                deadline = time.monotonic() + 5.0
                while not new.is_leader and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert new.is_leader and len(receivers) == 2, "it takes over at once"
                receivers[1].start.assert_called_once()
                fake_client.close.assert_called_once()
                new.stop()
        finally:
            released.set()
            for runtime in reversed(started_runtimes):
                runtime.stop()  # idempotent; on a failed assert it ends the retry thread too
            loop.close()

    def test_the_lock_waits_for_a_closing_receive_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The loop's thread may still be recording a verdict through its own
        client after the stop's grace: the lock waits for it to exit."""
        import time

        from agentchatme_hermes.runtime import Runtime

        exited = threading.Event()
        with _patched_runtime(lock_winner=True) as (ws_cls, _inv, _client, _acquire, release):
            ws_cls.return_value.wait_exited.side_effect = lambda timeout: exited.wait(timeout)
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            rt.stop()
            time.sleep(0.1)
            release.assert_not_called()
            exited.set()
            deadline = time.monotonic() + 5.0
            while not release.called and time.monotonic() < deadline:
                time.sleep(0.01)
            release.assert_called_once_with(99)

    def test_a_receiver_that_gave_up_hands_the_account_back(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (_ws, _inv, _client, _acquire, release):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            assert rt.is_leader
            rt._receiver_gave_up()
            release.assert_called_once_with(99)
            assert not rt.is_leader
            rt.stop()
            release.assert_called_once_with(99)


class _FakeApi:
    """AgentChat's HTTP API on loopback, for the real SDK clients: the tray
    (one card), the texts, ``/v1/agents/me`` and verdicts."""

    TEXTS: ClassVar[dict[str, Any]] = {
        "version": "1",
        "runtime": "hermes",
        "manual": {"url": "https://agentchat.me/skill.md", "version": "2.2.2"},
        "wake_instruction": "You were woken. Call mcp__agentchat__agentchat_inbox first.",
        "invite_prompt": "An invite is pending.",
        "identity_line": "I am @alice on AgentChat.",
        "identity_line_template": "I am @<handle> on AgentChat.",
    }

    def __init__(self) -> None:
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.lock = threading.Lock()
        self.answered = False
        self.recorded: list[tuple[str, str, str | None]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/v1/agents/me":
                    self._send(200, {"handle": "alice", "status": "active"})
                elif path == "/v1/texts":
                    self._send(200, api.TEXTS)
                elif path == "/v1/inbox":
                    self._send(200, api.tray())
                else:
                    self._send(404, {"code": "NOT_FOUND", "message": path})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.split("/")
                if len(parts) == 5 and parts[2] == "conversations" and parts[4] == "decisions":
                    with api.lock:
                        api.recorded.append((parts[3], body.get("decision"), body.get("through_message_id")))
                    self._send(201, {"decision": {"conversation_id": parts[3], "decision": body.get("decision")}})
                else:
                    self._send(404, {"code": "NOT_FOUND", "message": self.path})

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tray(self) -> dict[str, Any]:
        with self.lock:
            read = any(cid == "conv_a" and decision in ("reply", "dismiss") for cid, decision, _a in self.recorded)
            answered = self.answered
        cards = [] if read else [
            {
                "conversation_id": "conv_a",
                "type": "direct",
                "counterparty": {"handle": "@peer"},
                "relationship": "established",
                "you_owe_reply": not answered,
                "mentioned_you": False,
                "unread": 1,
                "pace": {"last_60s": 0, "last_10m": 1},
                "preview": {"message_id": "msg_a", "sender": "@peer", "text": "hello", "at": "2026-11-08T10:00:00.000Z"},
            }
        ]
        return {
            "version": 1, "you": "@alice", "question": "Decide per card.", "categories": ["open_request"],
            "conversations": cards, "deferred": 0, "truncated": False, "invites": [], "invites_truncated": False,
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@_posix_only
class TestStopRecordsBeforeLettingGo:
    """The real receiver, invoker, SDK clients and leader lock against
    AgentChat's API on loopback (only Hermes is stood in for): a turn that
    has sent its answer is stopped by the unload and ends on Hermes'
    interrupt after the loop has let go of it. Its ``reply`` must be recorded
    — through the runtime's client — before the client is closed and the
    leader lock is freed for the reloaded instance."""

    def test_the_reply_is_recorded_before_the_lock_is_released(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json
        import socket
        import time

        from agentchatme_hermes import agent_invoker as invoker_module
        from agentchatme_hermes import hosted_tools as hosted_tools_module
        from agentchatme_hermes import runtime as runtime_module
        from agentchatme_hermes.agent_invoker import AgentInvoker
        from agentchatme_hermes.leader_lock import try_acquire_ws_leader_lock
        from agentchatme_hermes.runtime import Runtime

        api = _FakeApi()
        interrupted = threading.Event()
        started = threading.Event()

        class _Agent:
            """Sends its answer, then runs until Hermes' interrupt; it ends a
            moment after the stop, once the loop has let go of the turn."""

            def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
                with api.lock:
                    api.answered = True  # the answer went out
                started.set()
                interrupted.wait(10.0)
                time.sleep(0.5)
                return {"completed": False, "interrupted": True, "messages": [
                    {"role": "tool", "name": "mcp__agentchat__agentchat_send_message", "content": '{"result": "{}"}'},
                ]}

            def get_activity_summary(self) -> dict[str, Any]:
                return {"last_activity_at": time.time(), "api_call_count": 1, "current_tool": None}

            def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
                interrupted.set()

        def gate_answer(**_kw: Any) -> Any:
            return lambda prompt, **_: json.dumps({"decisions": [{"conversation_id": "conv_a", "decision": "reply"}]})

        monkeypatch.setattr(AgentInvoker, "_build_agent", lambda self, _sid: _Agent())
        monkeypatch.setattr(AgentInvoker, "_ensure_hermes_resolved", lambda self: None)
        monkeypatch.setattr(AgentInvoker, "_resolve_model_and_runtime", lambda self: ("m", {}))
        monkeypatch.setattr(invoker_module, "build_call_model", gate_answer)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])
        at_release: list[list[tuple[str, str, str | None]]] = []
        real_release = runtime_module.release_leader_lock

        def spy_release(fd: int) -> None:
            with api.lock:
                at_release.append(list(api.recorded))
            real_release(fd)

        monkeypatch.setattr(runtime_module, "release_leader_lock", spy_release)

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        ws_port = closed.getsockname()[1]
        closed.close()  # nothing listens there: the socket stays down, wakes are manual
        config = _config(hermes_home=tmp_path / ".hermes", lock_dir=tmp_path / "locks")
        config.api_base = api.base_url
        config.ws_url = f"ws://127.0.0.1:{ws_port}/v1/ws"
        config.state_dir = tmp_path / "state"
        config.wake_debounce_s = 0.01
        rt = Runtime(config, gateway_mode=True)
        try:
            rt.start()
            assert rt.is_leader
            deadline = time.monotonic() + 15.0
            while not started.is_set() and time.monotonic() < deadline:
                receiver = rt._receiver
                if receiver is not None:
                    receiver.wake()
                time.sleep(0.05)
            assert started.is_set(), "the gate decided reply and the turn started"

            rt.stop()  # the unload, mid-turn
            fd = None
            while fd is None and time.monotonic() < deadline:
                fd = try_acquire_ws_leader_lock(rt.lock_path)
                if fd is None:
                    time.sleep(0.02)
            assert fd is not None, "the lock is released once the turn is done with"
            real_release(fd)
            assert at_release and ("conv_a", "reply", "msg_a") in at_release[0], (
                f"the reply was recorded before the lock was released: {at_release}"
            )
            with api.lock:
                assert api.recorded.count(("conv_a", "reply", "msg_a")) == 1
        finally:
            interrupted.set()
            rt.stop()
            api.close()
