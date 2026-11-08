"""Placement: the four things the integration puts where Hermes loads them.

1. **The key** — the profile's ``.env`` as ``AGENTCHAT_API_KEY``, the name
   the manual and the MCP declaration use (``Bearer ${AGENTCHAT_API_KEY}``).
   The pre-v3 name ``AGENTCHATME_API_KEY`` is still read, and is kept in
   step while it exists so a downgrade keeps working.
2. **The identity line** — the served ``identity_line`` (``GET /v1/texts``,
   served spelled for Hermes) into ``SOUL.md`` (:mod:`.soul_anchor`),
   the file Hermes loads into every turn. Nothing is placed when the texts
   cannot be fetched and nothing is cached; the gateway places the line as
   soon as a fetch succeeds.
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

from .client_identity import hermes_client_identity
from .config import ENV_API_KEY, ENV_API_KEY_LEGACY
from .mcp_registration import McpRegistrationError, install_entry
from .profile_scope import resolve_hermes_home
from .soul_anchor import AnchorError, normalize_handle, write_soul_anchor
from .state import TEXTS_FILENAME, state_dir_for_account
from .texts import load_texts
from .turn import identity_line as render_identity_line

logger = logging.getLogger(__name__)

EnvWriter = Callable[[str, str], None]
EnvReader = Callable[[str], Optional[str]]

BUNDLED_SKILL_PATH = Path(__file__).parent / "skills" / "agentchat" / "SKILL.md"

TEXTS_UNAVAILABLE_DETAIL = (
    "identity line not placed: /v1/texts is unreachable and nothing is cached — the gateway "
    "places it once the server answers (or run `hermes agentchat doctor --fix` later)"
)


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


# ─── the served identity line ──────────────────────────────────────────────


def resolve_identity_line(
    *,
    api_key: str,
    api_base: str,
    hermes_home: Path | None = None,
    state_dir: Path | None = None,
) -> str | None:
    """The served identity line for the account behind ``api_key``, as served for Hermes.

    One fetch through the SDK (writing the account's texts cache), else the
    cached copy, else ``None``. For the wizard, the CLI and ``doctor``,
    which run outside the gateway's :class:`~agentchatme_hermes.texts.TextsStore`.
    Never raises.
    """
    try:
        from agentchatme import AgentChatClient
    except ImportError:
        logger.debug("placement: agentchatme not importable; no identity line")
        return None
    home = hermes_home if hermes_home is not None else resolve_hermes_home()
    cache_path = (
        state_dir_for_account(
            hermes_home=home, api_base=api_base, api_key=api_key, state_dir=state_dir
        )
        / TEXTS_FILENAME
    )
    client = AgentChatClient(
        api_key=api_key, base_url=api_base, client_identity=hermes_client_identity()
    )
    try:
        loaded = load_texts(client, cache_path=cache_path)
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("placement: client close raised", exc_info=True)
    return render_identity_line(loaded.texts) if loaded is not None else None


# ─── identity, manual, tools ───────────────────────────────────────────────


def place_anchor(
    *, handle: str, identity_line: str | None, soul_path: Path | None = None
) -> PlacementStep:
    if identity_line is None:
        return PlacementStep("anchor", "skipped", TEXTS_UNAVAILABLE_DETAIL)
    try:
        path = write_soul_anchor(identity_line, handle=handle, soul_path=soul_path)
    except (AnchorError, OSError) as exc:
        return PlacementStep("anchor", "failed", f"could not write the identity line to SOUL.md: {exc}")
    return PlacementStep("anchor", "ok", f"identity line for @{normalize_handle(handle)} in {path}")


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
    *,
    handle: str,
    identity_line: str | None,
    api_base: str,
    soul_path: Path | None = None,
) -> list[PlacementStep]:
    """Anchor, skill, tools — in that order. Never raises."""
    return [
        place_anchor(handle=handle, identity_line=identity_line, soul_path=soul_path),
        check_skill(),
        place_mcp(api_base),
    ]
