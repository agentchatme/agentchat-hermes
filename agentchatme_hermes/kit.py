"""Is the SDK kit (``agentchatme.integration``) installed?

Receiving is the kit's receive loop, so an ``agentchatme`` older than the
release that ships it leaves the plugin unable to receive. That must be
loud: ``register()`` logs one ERROR naming the requirement and keeps the CLI
(``hermes agentchat doctor`` reports the same) instead of failing quietly.
"""
from __future__ import annotations

import importlib

# The first agentchatme release with ``agentchatme.integration`` (contract
# v1). Set to that SDK release's number at release time — the founder sets
# it; 1.3.0 is the expected value (VERSIONING.md: added behaviour → minor
# +1 from 1.2.x) — and the floor in pyproject.toml moves to it then.
KIT_MIN_VERSION = "1.3.0"
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
