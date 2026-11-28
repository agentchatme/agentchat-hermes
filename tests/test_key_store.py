"""The key file: one per profile, owner-only, written atomically, bound to its profile."""
from __future__ import annotations

import json
import os
import shutil
import stat
from typing import TYPE_CHECKING

import pytest
from fake_keys import api_key, real_shaped_key

from agentchatme_hermes import key_store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

POSIX = os.name != "nt"


def _home(tmp_path: Path, name: str = ".hermes") -> Path:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home


class TestWriteAndRead:
    def test_round_trip(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        key = real_shaped_key()
        path = key_store.write_store(home, api_key=f"  {key}\n", handle="@alice", api_base="https://a.test/")
        assert path == home / "agentchat" / "credentials.json"
        read = key_store.read_store(home)
        assert read.status == "ok" and read.usable
        assert read.key is not None
        assert (read.key.api_key, read.key.handle, read.key.api_base) == (key, "alice", "https://a.test")

    def test_owner_only_from_the_first_byte(
        self, tmp_path: Path, assert_private_file: Callable[[Path], None]
    ) -> None:
        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        assert_private_file(path)
        if POSIX:
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_no_temporary_file_is_left(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        key_store.write_store(home, api_key=api_key("a"), handle="a", api_base="https://a.test")
        key_store.write_store(home, api_key=api_key("b"), handle="b", api_base="https://a.test")
        assert sorted(p.name for p in (home / "agentchat").iterdir()) == ["credentials.json"]
        assert key_store.read_store(home).key.api_key == api_key("b")  # type: ignore[union-attr]

    def test_a_failed_write_leaves_the_old_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _home(tmp_path)
        key_store.write_store(home, api_key=api_key("old"), handle="a", api_base="https://a.test")

        def fail(*_a: object, **_k: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(key_store.os, "replace", fail)
        with pytest.raises(key_store.KeyStoreError, match="No space left"):
            key_store.write_store(home, api_key=api_key("new"), handle="a", api_base="https://a.test")
        assert key_store.read_store(home).key.api_key == api_key("old")  # type: ignore[union-attr]
        assert sorted(p.name for p in (home / "agentchat").iterdir()) == ["credentials.json"]

    def test_an_empty_key_is_not_saved(self, tmp_path: Path) -> None:
        with pytest.raises(key_store.KeyStoreError):
            key_store.write_store(_home(tmp_path), api_key="  ", handle="a", api_base="https://a.test")

    def test_the_file_records_its_profile(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["profile_home"] == str(home)
        assert record["version"] == key_store.STORE_VERSION


class TestUnusable:
    def test_missing(self, tmp_path: Path) -> None:
        read = key_store.read_store(_home(tmp_path))
        assert read.status == "missing" and not read.usable

    def test_not_json(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        key_store.store_path(home).write_text("{not json", encoding="utf-8")
        read = key_store.read_store(home)
        assert read.status == "unreadable" and "not valid JSON" in read.detail

    def test_no_key_in_it(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        key_store.store_path(home).write_text(json.dumps({"handle": "a"}), encoding="utf-8")
        assert key_store.read_store(home).status == "unreadable"

    @pytest.mark.skipif(not POSIX, reason="POSIX modes and links")
    def test_a_symlink_is_refused(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        elsewhere = _home(tmp_path, "elsewhere")
        target = key_store.write_store(elsewhere, api_key=api_key("k"), handle="a", api_base="https://a.test")
        key_store.store_dir(home).mkdir(mode=0o700)
        key_store.store_path(home).symlink_to(target)
        read = key_store.read_store(home)
        assert read.status == "unsafe" and "symbolic link" in read.detail and not read.usable

    @pytest.mark.skipif(not POSIX, reason="POSIX modes")
    def test_writable_by_others_is_refused(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        os.chmod(path, 0o622)
        read = key_store.read_store(home)
        assert read.status == "unsafe" and "written by its group or by others" in read.detail

    @pytest.mark.skipif(not POSIX, reason="POSIX modes")
    def test_readable_by_others_is_used_and_flagged(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        os.chmod(path, 0o644)
        read = key_store.read_store(home)
        assert read.usable and read.loose_mode
        assert key_store.tighten(home) is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not key_store.read_store(home).loose_mode


@pytest.mark.skipif(POSIX, reason="Windows access lists")
class TestWindowsAccessList:
    """The POSIX mode checks, read from the access list (``owner_only``)."""

    @staticmethod
    def _icacls(*args: str) -> None:
        import subprocess

        subprocess.run(["icacls", *args], check=True, capture_output=True)

    def test_the_directory_is_owner_only_and_the_file_inherits_it(self, tmp_path: Path) -> None:
        from agentchatme_hermes import owner_only

        home = _home(tmp_path)
        # A Hermes home outside the user profile: its parent lets everyone read.
        self._icacls(str(home), "/grant", "*S-1-1-0:(OI)(CI)(R)")
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        directory = owner_only.inspect(path.parent)
        assert directory is not None and directory.private and directory.protected
        made = owner_only.inspect(path)
        assert made is not None and made.private
        read = key_store.read_store(home)
        assert read.status == "ok" and not read.loose_mode

    def test_readable_by_others_is_used_flagged_and_tightened(self, tmp_path: Path) -> None:
        from agentchatme_hermes import owner_only

        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        self._icacls(str(path), "/grant", "*S-1-1-0:(R)")
        read = key_store.read_store(home)
        assert read.usable and read.loose_mode
        assert key_store.tighten(home) is True
        after = owner_only.inspect(path)
        assert after is not None and after.private
        assert not key_store.read_store(home).loose_mode
        assert key_store.tighten(home) is False, "nothing left to change"

    def test_writable_by_others_is_refused(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        self._icacls(str(path), "/grant", "*S-1-5-32-545:(M)")
        read = key_store.read_store(home)
        assert read.status == "unsafe" and not read.usable
        assert "can be changed by other accounts on this computer (Users)" in read.detail

    def test_a_key_directory_closed_to_the_user_is_given_back(self, tmp_path: Path) -> None:
        """What an elevated run of an earlier release could leave on Python 3.13+
        (SYSTEM and Administrators only): loose, and tightening names the user."""
        from agentchatme_hermes import owner_only

        home = _home(tmp_path)
        path = key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        for target, inherit in ((path.parent, "(OI)(CI)"), (path, "")):
            self._icacls(
                str(target), "/inheritance:r", "/grant:r",
                f"*S-1-5-18:{inherit}F", f"*S-1-5-32-544:{inherit}F",
            )
        if owner_only.inspect(path) is None:
            pytest.skip("this account cannot read the file without its own entry")
        read = key_store.read_store(home)
        assert read.loose_mode
        assert key_store.tighten(home) is True
        after = owner_only.inspect(path)
        assert after is not None and after.user_granted and after.private


class TestBoundToItsProfile:
    """A copied profile must not become the same agent."""

    def test_a_copy_of_another_profiles_file_is_copied(self, tmp_path: Path) -> None:
        root = _home(tmp_path)
        key_store.write_store(root, api_key=api_key("k"), handle="alice", api_base="https://a.test")
        clone = root / "profiles" / "he2"
        clone.mkdir(parents=True)
        shutil.copytree(key_store.store_dir(root), key_store.store_dir(clone))  # `--clone-all`
        read = key_store.read_store(clone)
        assert read.status == "copied" and not read.usable
        assert read.origin_home == root
        assert key_store.read_store(root).status == "ok", "the original stays the agent"

    def test_a_moved_file_is_usable_and_says_so(self, tmp_path: Path) -> None:
        old = _home(tmp_path, "old")
        key_store.write_store(old, api_key=api_key("k"), handle="alice", api_base="https://a.test")
        new = _home(tmp_path, "new")
        key_store.store_dir(old).rename(key_store.store_dir(new))  # `hermes profile rename`
        read = key_store.read_store(new)
        assert read.status == "moved" and read.usable and read.origin_home == old

    def test_a_copy_whose_original_changed_key_is_moved(self, tmp_path: Path) -> None:
        root = _home(tmp_path)
        key_store.write_store(root, api_key=api_key("one"), handle="a", api_base="https://a.test")
        clone = _home(tmp_path, "clone")
        shutil.copytree(key_store.store_dir(root), key_store.store_dir(clone))
        key_store.write_store(root, api_key=api_key("two"), handle="a", api_base="https://a.test")
        assert key_store.read_store(clone).status == "moved"


class TestDelete:
    def test_removes_the_file_and_the_empty_directory(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        key_store.write_store(home, api_key=api_key("k"), handle="a", api_base="https://a.test")
        assert key_store.delete_store(home) is True
        assert not key_store.store_dir(home).exists()
        assert key_store.delete_store(home) is False

    @pytest.mark.skipif(not POSIX, reason="links")
    def test_a_link_is_removed_not_followed(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        elsewhere = _home(tmp_path, "elsewhere")
        target = key_store.write_store(elsewhere, api_key=api_key("k"), handle="a", api_base="https://a.test")
        key_store.store_dir(home).mkdir(mode=0o700)
        key_store.store_path(home).symlink_to(target)
        assert key_store.delete_store(home) is True
        assert target.exists()


class TestOtherHolders:
    def test_finds_key_files_only_never_itself(self, tmp_path: Path) -> None:
        """Another profile's settings or ``.env`` are never read: only this
        plugin's own key files say which profile holds a key."""
        root = _home(tmp_path)
        a, b, c = (root / "profiles" / n for n in ("a", "b", "c"))
        for p in (a, b, c):
            p.mkdir(parents=True)
        key = api_key("shared")
        key_store.write_store(root, api_key=key, handle="x", api_base="https://a.test")
        key_store.write_store(b, api_key=api_key("different"), handle="y", api_base="https://a.test")
        (a / ".env").write_text(f"OTHER=1\nAGENTCHAT_API_KEY={key}\n", encoding="utf-8")
        found = key_store.other_holders(c, key, root=root)
        assert [(h.home, h.where) for h in found] == [(root, "key file")]
        assert key_store.other_holders(root, key, root=root) == []

    def test_holds_fingerprint(self, tmp_path: Path) -> None:
        root = _home(tmp_path)
        key_store.write_store(root, api_key=api_key("k"), handle="x", api_base="https://a.test")
        assert key_store.holds_fingerprint(root, key_store.fingerprint(api_key("k")))
        assert not key_store.holds_fingerprint(root, key_store.fingerprint(api_key("other")))
        assert not key_store.holds_fingerprint(tmp_path / "none", key_store.fingerprint(api_key("k")))
