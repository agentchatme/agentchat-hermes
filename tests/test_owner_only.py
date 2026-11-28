"""Owner-only directories and files (``agentchatme_hermes.owner_only``), the
Windows retrying rename (``state.replace_file``), the process probe
(``ws_health.pid_alive``) and the platform default home.

The access-list tests run on Windows only (the CI matrix's Windows job):
they set lists with ``icacls``, the tool a person would use, and read them
back through the module. Everything else runs everywhere.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentchatme_hermes import owner_only

if TYPE_CHECKING:
    from pathlib import Path

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows access lists")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX modes")

EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"


def icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], check=True, capture_output=True)


# ─── make_private_dir ───────────────────────────────────────────────────────


@posix_only
class TestPosixModes:
    def test_created_0700(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"
        owner_only.make_private_dir(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_an_existing_directory_is_left_unless_enforced(self, tmp_path: Path) -> None:
        target = tmp_path / "d"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        owner_only.make_private_dir(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        owner_only.make_private_dir(target, enforce=True)
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_no_access_list_is_read_off_windows(self, tmp_path: Path) -> None:
        assert owner_only.inspect(tmp_path) is None
        assert owner_only.restrict_children(tmp_path) == 0


@windows_only
class TestWindowsAccessList:
    def test_a_new_directory_is_private_and_protected(self, tmp_path: Path) -> None:
        target = tmp_path / "agentchat"
        owner_only.make_private_dir(target)
        access = owner_only.inspect(target)
        assert access is not None
        assert access.private and access.protected
        assert access.user == owner_only.current_user_sid()
        assert not access.others_read and not access.others_write

    def test_a_file_made_inside_inherits_it_from_its_first_byte(self, tmp_path: Path) -> None:
        target = tmp_path / "agentchat"
        owner_only.make_private_dir(target)
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")  # the parent opens up
        made = target / "credentials.json"
        made.write_text("{}", encoding="utf-8")
        access = owner_only.inspect(made)
        assert access is not None and access.private, access
        assert not access.protected, "inherited from the directory, not set on the file"

    def test_an_open_parent_is_not_inherited(self, tmp_path: Path) -> None:
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)", f"*{USERS}:(OI)(CI)(M)")
        plain = tmp_path / "plain"
        plain.mkdir()
        before = owner_only.inspect(plain)
        assert before is not None and EVERYONE in before.others_read and USERS in before.others_write
        assert before.writable_by_others
        private = tmp_path / "private"
        owner_only.make_private_dir(private)
        after = owner_only.inspect(private)
        assert after is not None and after.private and not after.writable_by_others

    def test_an_existing_directory_is_set_only_when_enforced(self, tmp_path: Path) -> None:
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        target = tmp_path / "d"
        target.mkdir()
        owner_only.make_private_dir(target)
        access = owner_only.inspect(target)
        assert access is not None and not access.private
        owner_only.make_private_dir(target, enforce=True)
        access = owner_only.inspect(target)
        assert access is not None and access.private and access.protected

    def test_nothing_propagates_to_existing_children_until_they_are_set(self, tmp_path: Path) -> None:
        """The list is written on the directory's own handle: a file already in
        it keeps what it had (a junction planted inside is never walked);
        ``restrict_children`` sets each file."""
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        target = tmp_path / "d"
        target.mkdir()
        child = target / "signup.json"
        child.write_text("{}", encoding="utf-8")
        owner_only.restrict(target)
        kept = owner_only.inspect(child)
        assert kept is not None and EVERYONE in kept.others_read
        assert owner_only.restrict_children(target) == 1
        fixed = owner_only.inspect(child)
        assert fixed is not None and fixed.private and fixed.protected
        assert owner_only.restrict_children(target) == 0, "already private: left alone"

    def test_a_list_without_the_user_is_not_private(self, tmp_path: Path) -> None:
        """What Python 3.13's ``mkdir(0o700)`` leaves when run elevated (SYSTEM,
        Administrators, OWNER RIGHTS = Administrators): no entry for the user,
        so the user unelevated is locked out. Not private: it is set again."""
        target = tmp_path / "d"
        target.mkdir()
        icacls(
            str(target), "/inheritance:r", "/grant:r",
            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
        )
        access = owner_only.inspect(target)
        assert access is not None
        assert not access.user_granted and not access.private and not access.writable_by_others
        owner_only.make_private_dir(target, enforce=True)
        access = owner_only.inspect(target)
        assert access is not None and access.user_granted and access.private

    def test_a_junction_is_never_followed(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        link = tmp_path / "link"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.restrict(link)
        with pytest.raises(OSError):
            owner_only.make_private_dir(link, enforce=True, strict=True)
        untouched = owner_only.inspect(real)
        assert untouched is not None and EVERYONE in untouched.others_read
        assert owner_only.inspect(link) is None, "a link is not read through either"

    def test_a_hard_linked_file_is_never_set(self, tmp_path: Path) -> None:
        first = tmp_path / "a.json"
        first.write_text("{}", encoding="utf-8")
        os.link(first, tmp_path / "b.json")
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.restrict(first)

    def test_a_failure_is_only_raised_when_strict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(path: Path) -> None:
            raise OSError("no access lists on this file system")

        monkeypatch.setattr(owner_only, "restrict", refuse)
        owner_only.make_private_dir(tmp_path / "lax")  # logged, used as it is
        assert (tmp_path / "lax").is_dir()
        with pytest.raises(OSError):
            owner_only.make_private_dir(tmp_path / "strict", strict=True)


def test_describe_sid() -> None:
    assert owner_only.describe_sid(EVERYONE) == "Everyone"
    assert owner_only.describe_sid("S-1-5-21-1-2-3-1001") == "S-1-5-21-1-2-3-1001"


# ─── replace_file: Windows refuses a rename onto an open file ──────────────


def test_replace_file_retries_while_a_reader_has_the_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows ``os.replace`` onto a file another handle has open (Python
    opens without FILE_SHARE_DELETE) fails with a sharing violation; the
    rename is retried after a pause, and here the reader closes during that
    pause. Elsewhere the first rename succeeds."""
    import time

    from agentchatme_hermes.state import replace_file

    target = tmp_path / "health.json"
    target.write_text("old", encoding="utf-8")
    source = tmp_path / "health.json.tmp"
    source.write_text("new", encoding="utf-8")
    reader = open(target, encoding="utf-8")  # noqa: SIM115 - held across the rename on purpose
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        reader.close()

    monkeypatch.setattr(time, "sleep", pause)
    try:
        if WINDOWS:
            with pytest.raises(PermissionError):
                os.replace(source, target)
        replace_file(source, target)
    finally:
        reader.close()
    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()
    assert bool(pauses) is WINDOWS


# ─── pid_alive without psutil ──────────────────────────────────────────────


def test_pid_alive_without_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: OpenProcess and a zero wait (never ``os.kill(pid, 0)``, a
    CTRL+C to the target's console group there); POSIX: ``os.kill(pid, 0)``."""
    import importlib

    from agentchatme_hermes import ws_health

    real = importlib.import_module

    def no_psutil(name: str, package: str | None = None) -> object:
        if name == "psutil":
            raise ImportError("no psutil here")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", no_psutil)
    assert ws_health.pid_alive(os.getpid()) is True
    with subprocess.Popen([sys.executable, "-c", "pass"]) as gone:
        gone.wait(timeout=30)
        # The Popen object still holds the exited process (Windows: its handle).
        assert ws_health.pid_alive(gone.pid) is False


# ─── the platform default home ─────────────────────────────────────────────


class TestPlatformDefaultHome:
    def test_posix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import pathlib

        from agentchatme_hermes.profile_scope import platform_default_home

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
        assert platform_default_home() == tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", "-dev")
        assert platform_default_home() == tmp_path / ".hermes-dev"

    def test_windows_is_localappdata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """As Hermes computes it (``hermes_constants._platform_default_hermes_home``)."""
        import pathlib

        from agentchatme_hermes.profile_scope import platform_default_home

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert platform_default_home() == tmp_path / "Local" / "hermes"
        monkeypatch.delenv("LOCALAPPDATA")
        assert platform_default_home() == tmp_path / "AppData" / "Local" / "hermes"
