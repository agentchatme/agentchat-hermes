"""Is the SDK kit (``agentchatme.integration``) installed?

Receiving is the kit's receive loop, so an ``agentchatme`` older than the
release that ships it leaves the plugin unable to receive. That must be
loud: ``register()`` logs one ERROR naming the requirement and keeps the CLI
(``hermes agentchat doctor`` reports the same) instead of failing quietly.
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# The agentchatme release this package needs: the floor of the one declaration
# in pyproject.toml, which Hermes installs. ``agentchatme.integration`` (contract
# v1) shipped in 1.3.0; 1.3.3 is the kit that acks a system notice without
# starting a cycle and anchors a card without a preview at its last message;
# 1.4.0 adds ``agentchatme.integration.account``, the sign-up helper the
# wizard and the register / login / recover subcommands run on; 1.5.0 is the
# kit with could not process (the gate-chunk split, the failure report,
# ``check_runtime``, ``TurnEnd`` / ``RunEnd`` ``timed_out``), which the
# receiver passes and the invoker builds: an older kit would refuse them and
# the receive loop could not start (:func:`kit_problem` checks that by
# capability); 1.6.1 is the ``ManualSync`` that retries while the manual is not
# the served one (5 min doubling to 1 h), so a failed download does not wait
# for the next texts change (:func:`manual_retry_problem`, a doctor warning);
# 1.8.0 is the strict identity-block rule (``anchor.owner`` / ``decide`` /
# ``follow``) the SOUL.md refresh and ``doctor`` run on
# (:func:`.soul_anchor.anchor_kit_problem`, a doctor warning).
KIT_MIN_VERSION = "1.8.0"
KIT_REQUIREMENT = f"agentchatme>={KIT_MIN_VERSION}"
# pyproject.toml's ``[project].dependencies`` entry, exactly (a test keeps the two
# identical): what the pip command below installs.
SDK_REQUIREMENT = f"{KIT_REQUIREMENT},<2"
_MANUAL_RETRY_FLOOR = (1, 6, 1)
# The repo a git install clones, when Hermes' install record names no source.
_REPO_URL = "https://github.com/agentchatme/agentchat-hermes.git"


def kit_problem() -> str | None:
    """``None`` when the kit imports; otherwise what to tell the operator."""
    try:
        integration = importlib.import_module("agentchatme.integration")
    except ImportError as exc:
        return (
            f"the installed agentchatme ({installed_sdk_version()}) has no agentchatme.integration "
            f"— agentchatme-hermes needs {KIT_REQUIREMENT} to receive. {sdk_update_sentence()} ({exc})"
        )
    if not _has_could_not_process(integration):
        return (
            f"the installed agentchatme ({installed_sdk_version()}) has a kit without could not "
            f"process (no check_runtime on its ReceiveLoop) — agentchatme-hermes needs "
            f"{KIT_REQUIREMENT} to receive. {sdk_update_sentence()}"
        )
    return None


def sdk_update_sentence(then: str = "restart the gateway") -> str:
    """``Run `<command>` and <then>.``: :func:`sdk_update_command` in a sentence."""
    return f"Run `{sdk_update_command()}` and {then}."


def sdk_update_command() -> str:
    """The one command that installs :data:`SDK_REQUIREMENT` the way this copy
    of the plugin was installed. Never raises.

    - A directory install (``hermes plugins install``, from Hermes' plugin
      catalog or from git): Hermes installs the dependency pyproject.toml
      declares, at install, reinstall and update, after a yes (``--yes-deps``),
      and leaves the plugin disabled on a no; the plugin installs nothing
      itself. ``hermes plugins update <directory>``, in the profile whose
      ``plugins/`` holds the directory; a copy pinned with ``--ref``, which
      ``update`` refuses, is installed again at a release's commit.
    - A pip install: ``python -m pip install -U`` in the environment that runs
      Hermes (:data:`sys.executable`).
    """
    try:
        from_hermes, directory = _install_source()
    except Exception:
        from_hermes, directory = False, None
    if not from_hermes:
        return f"{_quoted(sys.executable)} -m pip install -U {_quoted(SDK_REQUIREMENT, always=True)}"
    if directory is None:
        return "hermes plugins update agentchat"
    from .profile_scope import profile_label

    label = profile_label(directory.parent.parent)
    flag = "" if label == "default" else f" -p {label}"
    record = _install_record(directory)
    if record.get("pinned") is True:
        source = record.get("source")
        source = source if isinstance(source, str) and source else _REPO_URL
        return (
            f"hermes{flag} plugins install {_quoted(source)} --force "
            "--ref <release SHA> --yes-deps"
        )
    return f"hermes{flag} plugins update {directory.name}"


def _quoted(arg: str, *, always: bool = False) -> str:
    """``arg`` as one word for the person's shell: POSIX quoting, or on
    Windows double quotes (``cmd`` and PowerShell both take them; a
    requirement's ``<`` / ``>`` must never reach ``cmd`` bare)."""
    if os.name != "nt":
        return shlex.quote(arg)
    return f'"{arg}"' if always or any(char in arg for char in " \t&()^|<>") else arg


def _install_source() -> tuple[bool, Path | None]:
    """Whether Hermes installed this code (``hermes plugins install``), and
    from which plugin directory (``None`` when it cannot be found).

    Hermes imports a directory plugin as ``hermes_plugins.<slug>``
    (``hermes_cli/plugins_loader.py``), so this module's name says it was
    loaded from ``<home>/plugins/<directory>/``. A profile that enables the
    installed plugin without its own directory runs the package Hermes'
    package manager installed, editable, from a snapshot of the directory
    (``…/plugin-sources/<member key>/``): the directory is the one whose
    member key matches, in the default profile or one under ``profiles/``.
    Anything else is a pip install."""
    package_root = Path(__file__).resolve().parent.parent
    if __name__.startswith("hermes_plugins."):
        return True, package_root
    if package_root.parent.name != "plugin-sources":
        return False, None
    from .cli import _agentchat_directories, _hermes_member_key, _profiles_with_a_git_copy
    from .profile_scope import resolve_hermes_home

    for home in _profiles_with_a_git_copy(resolve_hermes_home()):
        for directory in _agentchat_directories(home / "plugins"):
            if _hermes_member_key(directory) == package_root.name:
                return True, directory
    return True, None


def _install_record(directory: Path) -> dict[str, Any]:
    """Hermes' installer-owned record of ``directory``
    (``plugins/.install-metadata.json``, keyed by the directory's name);
    ``{}`` when it cannot be read."""
    try:
        record = json.loads((directory.parent / ".install-metadata.json").read_text(encoding="utf-8-sig"))[
            directory.name
        ]
    except Exception:
        return {}
    return record if isinstance(record, dict) else {}


def _has_could_not_process(integration: object) -> bool:
    """Whether the kit's ``ReceiveLoop`` takes ``check_runtime`` (1.5.0):
    the receiver passes it, so an older kit's loop would refuse to start."""
    import inspect

    loop = getattr(integration, "ReceiveLoop", None)
    if loop is None:
        return False
    try:
        return "check_runtime" in inspect.signature(loop).parameters
    except (TypeError, ValueError):
        return False


def manual_retry_problem() -> str | None:
    """``None`` when the installed kit's ``ManualSync`` retries while the
    manual is stale (1.6.1); otherwise what ``doctor`` says. Not a reason to
    stop receiving: the manual is still synced at start and on every texts
    change."""
    installed = installed_sdk_version()
    parsed = _release(installed)
    if parsed is None or parsed >= _MANUAL_RETRY_FLOOR:
        return None
    return (
        f"the installed agentchatme ({installed}) does not retry a failed manual download: the "
        f"gateway tries again only when the served texts change or it restarts. {sdk_update_sentence()}"
    )


def _release(version: str) -> tuple[int, int, int] | None:
    """``"1.6.0"`` → ``(1, 6, 0)``; a pre-release counts as its release; ``None`` when unreadable."""
    parts: list[int] = []
    for piece in version.split(".")[:3]:
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parts.append(int(digits))
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def installed_sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("agentchatme")
    except Exception:
        return "not installed"
