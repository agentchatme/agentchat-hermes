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


def elevated() -> bool:
    """An administrator's full token (GitHub's Windows runners: UAC off)."""
    if not WINDOWS:
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined, unused-ignore]


elevated_only = pytest.mark.skipif(not elevated(), reason="needs an administrator's full token")


def junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


# ─── make_private_dir / ensure_private ──────────────────────────────────────


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

    def test_ensure_private_sets_the_mode_once_and_leaves_a_link(self, tmp_path: Path) -> None:
        made = tmp_path / "credentials.json"
        made.write_text("{}", encoding="utf-8")
        os.chmod(made, 0o644)
        assert owner_only.ensure_private(made) is True
        assert stat.S_IMODE(made.stat().st_mode) == owner_only.FILE_MODE
        assert owner_only.ensure_private(made) is False
        link = tmp_path / "link.json"
        link.symlink_to(made)
        assert owner_only.ensure_private(link) is False

    def test_no_access_list_is_read_off_windows(self, tmp_path: Path) -> None:
        assert owner_only.inspect(tmp_path) is None
        assert owner_only.restrict_children(tmp_path) == 0


@windows_only
class TestWindowsAccessList:
    def test_a_new_directory_is_made_private_in_the_call_that_creates_it(self, tmp_path: Path) -> None:
        """CreateDirectoryW with the descriptor: owned by this user, the three
        entries, protected, from its first moment."""
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)", f"*{USERS}:(OI)(CI)(M)")
        target = tmp_path / "agentchat"
        owner_only.make_private_dir(target)
        access = owner_only.inspect(target)
        assert access is not None
        assert access.settled, access
        assert access.owner == access.user == owner_only.current_user_sid()
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

    def test_an_open_parent_is_seen(self, tmp_path: Path) -> None:
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)", f"*{USERS}:(OI)(CI)(M)")
        plain = tmp_path / "plain"
        plain.mkdir()
        before = owner_only.inspect(plain)
        assert before is not None and EVERYONE in before.others_read and USERS in before.others_write
        assert not before.private

    def test_an_existing_directory_is_made_private_only_when_enforced(self, tmp_path: Path) -> None:
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        target = tmp_path / "d"
        target.mkdir()
        owner_only.make_private_dir(target)
        access = owner_only.inspect(target)
        assert access is not None and not access.private
        owner_only.make_private_dir(target, enforce=True)
        access = owner_only.inspect(target)
        assert access is not None and access.settled

    def test_inherit_only_entries_count(self, tmp_path: Path) -> None:
        """An inherit-only entry gives the directory's new files to Everyone: the
        directory is not private, and ensure_private replaces it."""
        target = tmp_path / "agentchat"
        owner_only.make_private_dir(target)
        icacls(str(target), "/grant", f"*{EVERYONE}:(OI)(IO)(R)")
        access = owner_only.inspect(target)
        assert access is not None and EVERYONE in access.others_read and not access.private
        assert owner_only.ensure_private(target) is True
        made = target / "credentials.json"
        made.write_text("{}", encoding="utf-8")
        after = owner_only.inspect(made)
        assert after is not None and after.private

    def test_nothing_propagates_to_existing_children_until_they_are_set(self, tmp_path: Path) -> None:
        """The list is written on the directory's own handle: a file already in
        it keeps what it had (a junction planted inside is never walked);
        ``restrict_children`` sets each file."""
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        target = tmp_path / "d"
        target.mkdir()
        child = target / "signup.json"
        child.write_text("{}", encoding="utf-8")
        assert owner_only.ensure_private(target) is True
        kept = owner_only.inspect(child)
        assert kept is not None and EVERYONE in kept.others_read
        assert owner_only.restrict_children(target) == 1
        fixed = owner_only.inspect(child)
        assert fixed is not None and fixed.settled
        assert owner_only.restrict_children(target) == 0, "already private: left alone"

    def test_a_list_without_the_user_is_not_private(self, tmp_path: Path) -> None:
        """What Python 3.13's ``mkdir(0o700)`` leaves when run elevated (SYSTEM,
        Administrators, OWNER RIGHTS = Administrators): no entry for the user.
        An administrator's full token still reads it: not private, and set
        again with the user's own entry and ownership (the elevated remedy).
        A standard account cannot even open it: an error, never "private"."""
        target = tmp_path / "d"
        target.mkdir()
        user = owner_only.current_user_sid()
        icacls(str(target), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
        # Any entry naming this user, or OWNER RIGHTS (which is this user while
        # it owns the folder; a fresh profile's Temp leaves one): what
        # remains names only SYSTEM and Administrators.
        icacls(str(target), "/remove", f"*{user}", f"*{owner_only.OWNER_RIGHTS_SID}")
        if not elevated():
            with pytest.raises(PermissionError):
                owner_only.inspect(target)
            with pytest.raises(PermissionError):
                owner_only.make_private_dir(target, enforce=True, strict=True)
            return
        access = owner_only.inspect(target)
        assert access is not None and not access.user_granted and not access.private, access
        owner_only.make_private_dir(target, enforce=True)
        access = owner_only.inspect(target)
        assert access is not None and access.settled

    def test_a_junction_at_the_path_is_refused_everywhere(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        link = tmp_path / "link"
        junction(link, real)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.inspect(link)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.restrict(link)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.make_private_dir(link)  # not even written into
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.make_private_dir(link, enforce=True, strict=True)
        untouched = owner_only.inspect(real)
        assert untouched is not None and EVERYONE in untouched.others_read

    def test_a_junction_between_the_root_and_the_path_is_refused(self, tmp_path: Path) -> None:
        """``root\\agentchat`` a junction to another folder: the file below it
        resolves outside ``root``, which GetFinalPathNameByHandleW shows."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "credentials.json").write_text("{}", encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        junction(home / "agentchat", elsewhere)
        target = home / "agentchat" / "credentials.json"
        with pytest.raises(owner_only.OwnerOnlyError, match="resolves to"):
            owner_only.inspect(target, root=home)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.restrict(target, root=home)
        assert owner_only.inspect(elsewhere / "credentials.json") is not None, "the real file, by its own name"

    def test_a_root_that_is_itself_a_link_is_the_callers_choice(self, tmp_path: Path) -> None:
        real = tmp_path / "real-home"
        real.mkdir()
        home = tmp_path / "home"
        junction(home, real)
        owner_only.make_private_dir(home / "agentchat", root=home)
        made = home / "agentchat" / "credentials.json"
        made.write_text("{}", encoding="utf-8")
        access = owner_only.inspect(made, root=home)
        assert access is not None and access.private

    def test_a_hard_linked_file_is_refused(self, tmp_path: Path) -> None:
        first = tmp_path / "a.json"
        first.write_text("{}", encoding="utf-8")
        os.link(first, tmp_path / "b.json")
        with pytest.raises(owner_only.OwnerOnlyError, match="hard link"):
            owner_only.inspect(first)
        with pytest.raises(owner_only.OwnerOnlyError):
            owner_only.restrict(first)

    def test_a_volume_without_access_lists_has_nothing_to_judge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAT / exFAT (no FILE_PERSISTENT_ACLS): ``None``, and nothing to set;
        the only case that is not an error."""
        real_open = owner_only._open

        def no_acls(path: Path, access: int, root: Path | None) -> owner_only._Opened:
            opened = real_open(path, access, root)
            return owner_only._Opened(opened.handle, opened.is_dir, False)

        made = tmp_path / "x.json"
        made.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(owner_only, "_open", no_acls)
        assert owner_only.inspect(made) is None
        assert owner_only.ensure_private(made) is False

    def test_a_failure_is_raised_only_when_strict_and_warned_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        def refuse(path: Path, *, root: Path | None = None) -> None:
            raise OSError("no access lists on this file system")

        icacls(str(tmp_path), "/grant", f"*{EVERYONE}:(OI)(CI)(R)")
        lax = tmp_path / "lax"
        lax.mkdir()
        monkeypatch.setattr(owner_only, "restrict", refuse)
        caplog.set_level(logging.DEBUG, logger="agentchatme_hermes.owner_only")
        owner_only.make_private_dir(lax, enforce=True)  # logged, used as it is
        owner_only.make_private_dir(lax, enforce=True)  # every 15 s on a follower
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and str(lax) in r.getMessage()]
        assert len(warnings) == 1, "warned once, then DEBUG"
        strict = tmp_path / "strict"
        strict.mkdir()
        with pytest.raises(OSError):
            owner_only.make_private_dir(strict, enforce=True, strict=True)

    @elevated_only
    def test_another_owner_is_never_taken_over_or_written_into(self, tmp_path: Path) -> None:
        """A directory another account made (and opened to us): refused by every
        writer, as a POSIX directory of another uid is. Users stands for the
        other account; setting another owner needs an administrator."""
        theirs = tmp_path / "agentchat"
        theirs.mkdir()
        icacls(str(theirs), "/grant", f"*{USERS}:(OI)(CI)F")
        icacls(str(theirs), "/setowner", f"*{USERS}")
        access = owner_only.inspect(theirs)
        assert access is not None and not access.owner_trusted
        with pytest.raises(owner_only.OwnerOnlyError, match="another account"):
            owner_only.make_private_dir(theirs)
        with pytest.raises(owner_only.OwnerOnlyError, match="another account"):
            owner_only.make_private_dir(theirs, enforce=True)
        with pytest.raises(owner_only.OwnerOnlyError, match="not taken over"):
            owner_only.ensure_private(theirs)
        still = owner_only.inspect(theirs)
        assert still is not None and still.owner == USERS


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


def test_remove_file_retries_while_a_reader_has_the_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``logout`` while another process reads the key file: on Windows the
    removal fails with a sharing violation for that moment and is retried;
    here the reader closes during the pause."""
    import time

    from agentchatme_hermes.state import remove_file

    target = tmp_path / "credentials.json"
    target.write_text("{}", encoding="utf-8")
    reader = open(target, encoding="utf-8")  # noqa: SIM115 - held across the removal on purpose
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        reader.close()

    monkeypatch.setattr(time, "sleep", pause)
    try:
        if WINDOWS:
            with pytest.raises(PermissionError):
                os.remove(target)
        remove_file(target)
    finally:
        reader.close()
    assert not target.exists()
    assert bool(pauses) is WINDOWS
    with pytest.raises(FileNotFoundError):
        remove_file(target)


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
