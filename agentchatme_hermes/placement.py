"""Placement: the four things the doorbell puts where Hermes loads them.

1. **The key** — the profile's ``.env`` as ``AGENTCHAT_API_KEY``, the name
   the manual and the MCP declaration use (``Bearer ${AGENTCHAT_API_KEY}``).
   The pre-v3 name ``AGENTCHATME_API_KEY`` is still read, and is kept in
   step while it exists so a downgrade keeps working.
2. **The identity line** — the published anchor text into ``SOUL.md``
   (:mod:`.soul_anchor`), the file Hermes loads into every turn.
3. **The manual** — ``https://agentchat.me/skill.md`` verbatim, bundled as
   the ``agentchat:agentchat`` skill. Nothing to write; verified present.
4. **The tools** — ``mcp_servers.agentchat`` in the profile's
   ``config.yaml`` (:mod:`.mcp_registration`).

Every placer is idempotent and reports what it did; the wizard prints the
report, the activation path logs it, ``doctor --fix`` runs it again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import ENV_API_KEY, ENV_API_KEY_LEGACY
from .mcp_registration import McpRegistrationError, install_entry
from .soul_anchor import AnchorError, write_soul_anchor

logger = logging.getLogger(__name__)

EnvWriter = Callable[[str, str], None]
EnvReader = Callable[[str], Optional[str]]

BUNDLED_SKILL_PATH = Path(__file__).parent / "skills" / "agentchat" / "SKILL.md"


@dataclass(frozen=True)
class PlacementStep:
    """One placement outcome: ``name`` ∈ anchor | skill | mcp; ``status`` ∈ ok | changed | skipped | failed."""

    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "failed"


# ─── the key ───────────────────────────────────────────────────────────────


def persist_api_key(
    save_env_value: EnvWriter, *, api_key: str, get_env_value: EnvReader | None = None
) -> list[str]:
    """Write the key under its canonical name; keep the legacy name in step while present.

    Returns the variable names written.
    """
    save_env_value(ENV_API_KEY, api_key)
    written = [ENV_API_KEY]
    legacy_present = bool((get_env_value(ENV_API_KEY_LEGACY) or "").strip()) if get_env_value else False
    if legacy_present:
        save_env_value(ENV_API_KEY_LEGACY, api_key)
        written.append(ENV_API_KEY_LEGACY)
    return written


def clear_api_key(save_env_value: EnvWriter) -> None:
    """Blank both names (Hermes' ``save_env_value`` keeps the line, empties the value)."""
    save_env_value(ENV_API_KEY, "")
    save_env_value(ENV_API_KEY_LEGACY, "")


def migrate_legacy_key(save_env_value: EnvWriter, get_env_value: EnvReader) -> bool:
    """Copy ``AGENTCHATME_API_KEY`` to ``AGENTCHAT_API_KEY`` when only the legacy name is set.

    The hosted MCP header is ``Bearer ${AGENTCHAT_API_KEY}``; without this an
    upgraded install would authenticate the socket and fail the tools.
    Returns ``True`` when a value was written.
    """
    canonical = (get_env_value(ENV_API_KEY) or "").strip()
    if canonical:
        return False
    legacy = (get_env_value(ENV_API_KEY_LEGACY) or "").strip()
    if not legacy:
        return False
    save_env_value(ENV_API_KEY, legacy)
    logger.info("placement: mirrored %s into %s in the profile .env", ENV_API_KEY_LEGACY, ENV_API_KEY)
    return True


# ─── identity, manual, tools ───────────────────────────────────────────────


def place_anchor(handle: str, *, soul_path: Path | None = None) -> PlacementStep:
    try:
        path = write_soul_anchor(handle, soul_path=soul_path)
    except (AnchorError, OSError) as exc:
        return PlacementStep("anchor", "failed", f"could not write the identity line to SOUL.md: {exc}")
    return PlacementStep("anchor", "ok", f"identity line for @{handle.strip().lstrip('@')} in {path}")


def check_skill(path: Path = BUNDLED_SKILL_PATH) -> PlacementStep:
    if not path.exists():
        return PlacementStep("skill", "failed", f"bundled manual missing at {path} — reinstall the package")
    return PlacementStep("skill", "ok", f"manual bundled as the agentchat:agentchat skill ({path.name})")


def place_mcp(api_base: str) -> PlacementStep:
    try:
        outcome = install_entry(api_base=api_base)
    except McpRegistrationError as exc:
        return PlacementStep("mcp", "failed", str(exc))
    except Exception as exc:  # Hermes' config layer raised something of its own
        return PlacementStep("mcp", "failed", f"Hermes refused the config.yaml write: {exc}")
    if outcome == "added":
        return PlacementStep(
            "mcp",
            "changed",
            "registered the hosted tools as mcp_servers.agentchat in config.yaml "
            "(a running gateway connects it within about a minute; a session on /reload-mcp)",
        )
    if outcome == "present":
        return PlacementStep("mcp", "ok", "hosted tools already registered as mcp_servers.agentchat")
    if outcome == "disabled":
        return PlacementStep("mcp", "skipped", "mcp_servers.agentchat exists but is disabled in config.yaml — left alone")
    return PlacementStep(
        "mcp", "skipped", "mcp_servers.agentchat is managed by hand in config.yaml — left alone"
    )


def place_identity_and_tools(
    *, handle: str, api_base: str, soul_path: Path | None = None
) -> list[PlacementStep]:
    """Anchor, skill, tools — in that order. Never raises."""
    return [place_anchor(handle, soul_path=soul_path), check_skill(), place_mcp(api_base)]
