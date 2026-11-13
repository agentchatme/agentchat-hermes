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
# starting a cycle and anchors a card without a preview at its last message.
KIT_MIN_VERSION = "1.3.3"
KIT_REQUIREMENT = f"agentchatme>={KIT_MIN_VERSION}"


def kit_problem() -> str | None:
    """``None`` when the kit imports; otherwise what to tell the operator."""
    try:
        importlib.import_module("agentchatme.integration")
    except ImportError as exc:
        return (
            f"the installed agentchatme ({installed_sdk_version()}) has no agentchatme.integration "
            f"— agentchatme-hermes needs {KIT_REQUIREMENT} to receive. Upgrade it in the Hermes "
            f"environment (pip install -U '{KIT_REQUIREMENT}') and restart the gateway. ({exc})"
        )
    return None


def installed_sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("agentchatme")
    except Exception:
        return "not installed"
