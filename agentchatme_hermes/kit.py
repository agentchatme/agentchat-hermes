"""Is the SDK kit (``agentchatme.integration``) installed?

Receiving is the kit's receive loop, so an ``agentchatme`` older than the
release that ships it leaves the plugin unable to receive. That must be
loud: ``register()`` logs one ERROR naming the requirement and keeps the CLI
(``hermes agentchat doctor`` reports the same) instead of failing quietly.
"""
from __future__ import annotations

import importlib

# The agentchatme release this package needs: the floor in pyproject.toml,
# plugin.yaml and the git-clone shim. ``agentchatme.integration`` (contract
# v1) shipped in 1.3.0; 1.3.3 is the kit that acks a system notice without
# starting a cycle and anchors a card without a preview at its last message;
# 1.4.0 adds ``agentchatme.integration.account``, the sign-up helper the
# wizard and the register / login / recover subcommands run on; 1.5.0 is the
# kit with could not process (the gate-chunk split, the failure report,
# ``check_runtime``, ``TurnEnd`` / ``RunEnd`` ``timed_out``), which the
# receiver passes and the invoker builds: an older kit would refuse them and
# the receive loop could not start.
KIT_MIN_VERSION = "1.5.0"
KIT_REQUIREMENT = f"agentchatme>={KIT_MIN_VERSION}"


def kit_problem() -> str | None:
    """``None`` when the kit imports; otherwise what to tell the operator."""
    try:
        integration = importlib.import_module("agentchatme.integration")
    except ImportError as exc:
        return (
            f"the installed agentchatme ({installed_sdk_version()}) has no agentchatme.integration "
            f"— agentchatme-hermes needs {KIT_REQUIREMENT} to receive. Upgrade it in the Hermes "
            f"environment (pip install -U '{KIT_REQUIREMENT}') and restart the gateway. ({exc})"
        )
    if not _has_could_not_process(integration):
        return (
            f"the installed agentchatme ({installed_sdk_version()}) has a kit without could not "
            f"process (no check_runtime on its ReceiveLoop) — agentchatme-hermes needs "
            f"{KIT_REQUIREMENT} to receive. Upgrade it in the Hermes environment "
            f"(pip install -U '{KIT_REQUIREMENT}') and restart the gateway."
        )
    return None


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


def installed_sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("agentchatme")
    except Exception:
        return "not installed"
