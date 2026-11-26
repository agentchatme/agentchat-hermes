"""Saving, moving and clearing the key — and that it never lands in the profile ``.env``.

Up to 0.5.3 every register / login / recover wrote ``AGENTCHAT_API_KEY``
into the profile ``.env``. Hermes loads that file into its process environment,
so the key was in the environment of every command a reply turn ran (and in
Hermes' terminal env snapshots), and ``hermes profile create`` copied it into
every new profile.

The plugin never opens a ``.env`` file (the conftest guard fails any test in
which it does): the key is read from the profile's settings through Hermes'
``get_secret`` (here the process environment, as in a single-profile Hermes
process), and lines are removed and written with Hermes' own
``remove_env_value`` / ``save_env_value`` (``FakeHermesEnv``).
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from hermes_doubles import FakeHermesEnv
from key_files import profile_home, saved
from sdk_wire import answer, serve

from agentchatme_hermes import cli, credentials, key_source, key_store

if TYPE_CHECKING:
    from pathlib import Path

BASE = "https://api.agentchat.me"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_HANDLE", "AGENTCHATME_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(key_source, "source_supported", lambda: True)


def _hermes(monkeypatch: pytest.MonkeyPatch, home: Path | None = None, **lines: str) -> FakeHermesEnv:
    """Hermes' .env helpers for the profile ``home`` (made the active one), its
    ``.env`` holding ``lines``, loaded into the environment as Hermes does at start."""
    home = home if home is not None else profile_home()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes = FakeHermesEnv(monkeypatch)
    hermes.install()
    hermes.write(home, **lines)
    hermes.load()
    return hermes


class TestSave:
    def test_the_key_goes_to_the_key_file_and_leaves_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, OPENAI_API_KEY="x", AGENTCHAT_API_KEY=api_key("old"), AGENTCHATME_HANDLE="alice")
        outcome = credentials.save_credentials(api_key=api_key("new"), handle="alice", api_base=BASE, home=home)
        assert outcome.path == key_store.store_path(home) and outcome.fallback_reason is None
        assert outcome.env_lines_removed == ("AGENTCHAT_API_KEY",)
        # Removed with Hermes' own remover; every other line is Hermes' to keep.
        assert hermes.file(home) == {"OPENAI_API_KEY": "x", "AGENTCHATME_HANDLE": "alice"}
        assert hermes.saved == [], "nothing written to .env"
        assert saved(home).api_key == api_key("new")  # type: ignore[union-attr]

    def test_both_names_leave_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, AGENTCHATME_API_KEY=api_key("a"), AGENTCHAT_API_KEY=api_key("a"))
        outcome = credentials.save_credentials(api_key=api_key("a"), handle="a", api_base=BASE, home=home)
        assert set(outcome.env_lines_removed) == {"AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY"}
        assert hermes.file(home) == {}

    def test_the_dotenv_line_goes_only_after_the_key_file_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never a moment with no saved copy: a key file that cannot be
        written keeps the key in .env (where Hermes keeps secrets) and says so."""
        home = profile_home()
        hermes = _hermes(monkeypatch, AGENTCHAT_API_KEY=api_key("old"))

        def no_disk(*_a: Any, **_k: Any) -> Path:
            raise key_store.KeyStoreError("cannot write: read-only file system")

        monkeypatch.setattr(key_store, "write_store", no_disk)
        outcome = credentials.save_credentials(api_key=api_key("new"), handle="a", api_base=BASE, home=home)
        assert outcome.path is None and "read-only" in str(outcome.fallback_reason)
        assert hermes.saved == [("AGENTCHAT_API_KEY", api_key("new"))]
        assert hermes.removed == []

    def test_another_profiles_dotenv_is_never_edited(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Hermes' writers edit the active profile's .env: saving for another
        home writes its key file and removes nothing."""
        other = tmp_path / "profiles" / "b"
        hermes = _hermes(monkeypatch, AGENTCHAT_API_KEY=api_key("this_profiles"))
        outcome = credentials.save_credentials(api_key=api_key("b"), handle="b", api_base=BASE, home=other)
        assert outcome.env_lines_removed == () and hermes.removed == []
        assert saved(other).api_key == api_key("b")  # type: ignore[union-attr]


class TestMove:
    def test_doctor_fix_moves_the_dotenv_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, X="1", AGENTCHAT_API_KEY=api_key("k"))
        moved = credentials.move_env_key(handle="alice", api_base=BASE, home=home)
        assert moved.moved and moved.env_lines_removed == ("AGENTCHAT_API_KEY",) and moved.detail == ""
        assert hermes.file(home) == {"X": "1"}
        assert saved(home).handle == "alice"  # type: ignore[union-attr]

    def test_a_legacy_only_key_moves_and_its_line_goes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, AGENTCHATME_API_KEY=api_key("aug"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert moved.moved and moved.env_lines_removed == ("AGENTCHATME_API_KEY",)
        assert hermes.file(home) == {} and saved(home).api_key == api_key("aug")  # type: ignore[union-attr]

    def test_an_exported_key_is_not_moved_the_write_is_undone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No .env line held it: the shell or the service sets it, and a key the
        environment sets keeps working from there (R2: undo the key-file write)."""
        home = profile_home()
        hermes = _hermes(monkeypatch)
        monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("exported"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert not moved.moved and "set in this process's environment" in moved.detail
        assert "login --api-key-file" in moved.detail
        assert hermes.removed == ["AGENTCHAT_API_KEY"]
        assert key_store.read_store(home).status == "missing"

    def test_an_exported_legacy_key_stays_in_the_key_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hosted tools read AGENTCHAT_API_KEY, which only the key file supplies."""
        home = profile_home()
        _hermes(monkeypatch)
        monkeypatch.setenv("AGENTCHATME_API_KEY", api_key("exported_aug"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert moved.moved and moved.env_lines_removed == ()
        assert "AGENTCHATME_API_KEY is set in the environment" in moved.detail and "unset it there" in moved.detail
        assert saved(home).api_key == api_key("exported_aug")  # type: ignore[union-attr]

    def test_two_different_keys_move_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Moving one would leave the other behind as a disagreeing key."""
        home = profile_home()
        hermes = _hermes(monkeypatch, AGENTCHAT_API_KEY=api_key("k"), AGENTCHATME_API_KEY=api_key("other"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert not moved.moved and "two different AgentChat keys" in moved.detail
        assert hermes.removed == [] and key_store.read_store(home).status == "missing"

    def test_a_key_another_profiles_key_file_holds_is_not_claimed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The other profile is that agent: the move is refused and says what to do."""
        root = tmp_path / ".hermes"
        a, b = root / "profiles" / "a", root / "profiles" / "b"
        key_store.write_store(a, api_key=api_key("shared"), handle="a", api_base=BASE)
        hermes = _hermes(monkeypatch, b, AGENTCHAT_API_KEY=api_key("shared"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=b)
        assert not moved.moved
        assert "profile a" in moved.detail and "hermes -p a agentchat logout" in moved.detail
        assert key_store.read_store(b).status == "missing" and hermes.removed == []

    def test_nothing_to_move(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _hermes(monkeypatch)
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=profile_home())
        assert not moved.moved and "no key" in moved.detail

    def test_without_hermes_nothing_moves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "hermes_cli.config", None)  # not importable
        monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("k"))
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=profile_home())
        assert not moved.moved and "cannot edit this profile's .env" in moved.detail
        assert key_store.read_store(profile_home()).status == "missing"

    def test_a_failed_write_moves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, AGENTCHAT_API_KEY=api_key("k"))

        def no_disk(*_a: Any, **_k: Any) -> Path:
            raise key_store.KeyStoreError("disk full")

        monkeypatch.setattr(key_store, "write_store", no_disk)
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert not moved.moved and "stays where it is" in moved.detail
        assert hermes.removed == []

    def test_a_busy_lock_moves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import contextlib

        from agentchatme_hermes import leader_lock

        home = profile_home()
        _hermes(monkeypatch, AGENTCHAT_API_KEY=api_key("k"))

        @contextlib.contextmanager
        def busy(_path: Path, **_kw: Any) -> Any:
            yield "busy"

        monkeypatch.setattr(leader_lock, "held_lock", busy)
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert not moved.moved and "right now" in moved.detail
        assert key_store.read_store(home).status == "missing"


class TestAdoptLegacyKey:
    def test_adopted_when_no_other_key_file_holds_it(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        outcome = credentials.adopt_legacy_key(home=home, api_key=api_key("aug"), api_base=BASE)
        assert outcome.status == "adopted" and outcome.path == key_store.store_path(home)
        assert saved(home).api_key == api_key("aug")  # type: ignore[union-attr]

    def test_the_second_profile_holding_it_is_its_copy(self, tmp_path: Path) -> None:
        """Two profiles with the same key (a copied .env): the first to adopt it is
        the agent; the second finds its key file and adopts nothing."""
        root = tmp_path / ".hermes"
        named = root / "profiles" / "clone"
        assert credentials.adopt_legacy_key(home=root, api_key=api_key("aug"), api_base=BASE).status == "adopted"
        second = credentials.adopt_legacy_key(home=named, api_key=api_key("aug"), api_base=BASE)
        assert second.status == "copy" and second.owner == root
        assert key_store.read_store(named).status == "missing"

    @pytest.mark.skipif(sys.platform == "win32", reason="the per-account lock is fcntl, POSIX-only (as the leader lock)")
    def test_two_profiles_adopting_at_once_cannot_both_take_it(self, tmp_path: Path) -> None:
        """At one multiplexed boot: the per-account lock makes check-and-write
        one step, so exactly one adopts and the other is refused as its copy."""
        import threading

        root = tmp_path / ".hermes"
        homes = [root / "profiles" / f"p{i}" for i in range(6)]
        start = threading.Barrier(len(homes), timeout=5.0)
        outcomes: dict[str, str] = {}

        def adopt(home: Path) -> None:
            start.wait()
            outcomes[home.name] = credentials.adopt_legacy_key(
                home=home, api_key=api_key("aug"), api_base=BASE
            ).status

        threads = [threading.Thread(target=adopt, args=(home,)) for home in homes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        assert sorted(outcomes.values()) == ["adopted"] + ["copy"] * (len(homes) - 1)
        assert sum(key_store.read_store(h).status != "missing" for h in homes) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="the per-account lock is fcntl, POSIX-only (as the leader lock)")
    def test_the_lock_beside_the_leader_lock(self, tmp_path: Path) -> None:
        from agentchatme_hermes.leader_lock import lock_path_for
        from agentchatme_hermes.profile_scope import account_fingerprint

        root = tmp_path / ".hermes"
        credentials.adopt_legacy_key(home=root, api_key=api_key("aug"), api_base=BASE)
        fingerprint = account_fingerprint(BASE, api_key("aug"))
        adopt = lock_path_for(hermes_home=root, account_fingerprint=fingerprint, purpose="adopt")
        assert adopt.exists() and adopt.parent == lock_path_for(hermes_home=root, account_fingerprint=fingerprint).parent

    def test_without_secret_sources_nothing_is_adopted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(key_source, "source_supported", lambda: False)
        outcome = credentials.adopt_legacy_key(home=tmp_path, api_key=api_key("aug"), api_base=BASE)
        assert outcome.status == "unsupported" and key_store.read_store(tmp_path).status == "missing"


class TestTheFallbackNeverDropsOrSplitsTheKey:
    def test_falling_back_to_dotenv_removes_the_old_key_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A login (or recover) while Hermes cannot read the key file: the new
        key goes to .env, and the old key file — another key — goes, so the
        two cannot disagree."""
        from agentchatme_hermes import config

        home = profile_home()
        key_store.write_store(home, api_key=api_key("old"), handle="a", api_base=BASE)
        _hermes(monkeypatch)
        outcome = credentials.save_credentials(
            api_key=api_key("new"), handle="a", api_base=BASE, home=home, use_key_file=False, not_ready="x"
        )
        assert outcome.path is None and outcome.fallback_reason == "x"
        assert key_store.read_store(home).status == "missing"
        resolved = config.resolve_key(home)
        assert (resolved.api_key, resolved.refusal) == (api_key("new"), None)

    def test_a_managed_dotenv_keeps_the_key_in_the_key_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hermes refuses .env writes on a managed install without raising; its
        write lock says so first: the key must not be reported saved there and lost."""
        home = profile_home()
        monkeypatch.setenv("HERMES_HOME", str(home))
        hermes = FakeHermesEnv(monkeypatch, managed=True)
        hermes.install()
        outcome = credentials.save_credentials(
            api_key=api_key("new"), handle="a", api_base=BASE, home=home, use_key_file=False,
            not_ready="the managed config has no secrets.agentchat",
        )
        assert outcome.tools_need_block and outcome.path == key_store.store_path(home)
        assert "managed config" in str(outcome.fallback_reason)
        assert hermes.saved == [], "refused by the write lock before any write"
        assert saved(home).api_key == api_key("new")  # type: ignore[union-attr]

    def test_saved_nowhere_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        monkeypatch.setenv("HERMES_HOME", str(home))
        FakeHermesEnv(monkeypatch, managed=True).install()

        def no_disk(*_a: Any, **_k: Any) -> Path:
            raise key_store.KeyStoreError("read-only file system")

        monkeypatch.setattr(key_store, "write_store", no_disk)
        with pytest.raises(key_store.KeyStoreError, match="saved neither"):
            credentials.save_credentials(api_key=api_key("new"), handle="a", api_base=BASE, home=home)


class TestClear:
    def test_logout_removes_the_file_and_the_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        hermes = _hermes(monkeypatch, KEEP="1", AGENTCHAT_API_KEY=api_key("k"), AGENTCHATME_HANDLE="alice")
        key_store.write_store(home, api_key=api_key("k"), handle="alice", api_base=BASE)
        removed_file, removed_lines = credentials.clear_credentials(home)
        assert removed_file is True
        assert set(removed_lines) == {"AGENTCHAT_API_KEY", "AGENTCHATME_HANDLE"}
        assert hermes.file(home) == {"KEEP": "1"}
        assert key_store.read_store(home).status == "missing"


class TestKeyBridge:
    """The key leaves ``.env`` only when Hermes' MCP client can read the key file."""

    def test_without_secret_sources_the_key_stays_in_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(key_source, "source_supported", lambda: False)
        ready, why = credentials.key_bridge()
        assert ready is False and "no secret sources" in why
        home = profile_home()
        hermes = _hermes(monkeypatch)
        outcome = credentials.save_credentials(
            api_key=api_key("k"), handle="a", api_base=BASE, home=home, use_key_file=ready, not_ready=why
        )
        assert outcome.path is None and "no secret sources" in str(outcome.fallback_reason)
        assert hermes.saved == [("AGENTCHAT_API_KEY", api_key("k"))]
        assert key_store.read_store(home).status == "missing"

    def test_ready_when_the_block_turns_the_source_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import placement, secrets_registration

        monkeypatch.setattr(placement, "place_secrets", lambda: placement.PlacementStep("secrets", "ok", ""))
        monkeypatch.setattr(secrets_registration, "inspect_source_enabled", lambda: True)
        assert credentials.key_bridge() == (True, "")
        monkeypatch.setattr(secrets_registration, "inspect_source_enabled", lambda: False)
        monkeypatch.setattr(
            placement, "place_secrets", lambda: placement.PlacementStep("secrets", "skipped", "disabled by hand")
        )
        assert credentials.key_bridge() == (False, "disabled by hand")


class TestLoginNeverWritesTheKeyToDotenv:
    """End to end through ``hermes agentchat login``: the key lands in the
    key file (owner-only) and the profile ``.env`` holds no key afterwards."""

    def test_login(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        assert_private_file: Any,
    ) -> None:
        import argparse
        import io

        home = profile_home()
        hermes = _hermes(monkeypatch, OPENAI_API_KEY="sk-other", AGENTCHAT_API_KEY=api_key("old"))
        serve(monkeypatch, {"/v1/agents/me": answer(200, {"handle": "alice", "status": "active"})})
        monkeypatch.setattr(cli, "_install_placement", lambda handle, *, api_base, api_key, tools_decision=None: [])
        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{api_key('new')}\n"))
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

        rc = cli._dispatch_login(argparse.Namespace(api_key=None, api_key_file=None))
        out = capsys.readouterr().out

        assert rc == 0
        assert hermes.file(home) == {"OPENAI_API_KEY": "sk-other"}
        assert hermes.saved == []
        record = saved(home)
        assert record is not None and (record.api_key, record.handle) == (api_key("new"), "alice")
        assert_private_file(key_store.store_path(home))
        assert api_key("new") not in out
