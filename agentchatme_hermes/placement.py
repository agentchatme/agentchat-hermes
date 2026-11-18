"""Placement: what the integration puts where Hermes loads it.

1. **The key** — this profile's AgentChat key file
   (``<profile home>/agentchat/credentials.json``, owner-only), written by
   :mod:`.credentials` at register / login / recover; never the profile
   ``.env`` (:mod:`.key_store` says why). Hermes' MCP client gets it as
   ``AGENTCHAT_API_KEY`` through the plugin's secret source, which
   ``secrets.agentchat`` in ``config.yaml`` turns on; the same block tells
   Hermes to keep that variable out of every command the agent runs
   (:mod:`.secrets_registration`, :mod:`.key_source`).
2. **The identity line** — the served ``identity_line`` (``GET /v1/texts``,
   served spelled for Hermes) into ``SOUL.md`` (:mod:`.soul_anchor`),
   the file Hermes loads into every turn. Nothing is placed when the texts
   cannot be fetched and nothing is cached; the gateway places the line as
   soon as a fetch succeeds.
3. **The manual** — ``https://agentchat.me/skill.md`` verbatim, bundled as
   the ``agentchat:agentchat`` skill. Nothing to write; verified present.
4. **The tools** — ``mcp_servers.agentchat`` in the profile's
   ``config.yaml`` (:mod:`.mcp_registration`), with
   ``Authorization: "Bearer ${AGENTCHAT_API_KEY}"`` as a template: the key
   never lands in ``config.yaml``.

Every placer is idempotent and reports what it did; the wizard prints the
report, the activation path logs it, ``doctor --fix`` runs it again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .client_identity import hermes_client_identity
from .mcp_registration import McpRegistrationError, install_entry
from .profile_scope import resolve_hermes_home
from .soul_anchor import AnchorError, normalize_handle, write_soul_anchor
from .state import state_dir_for_account
from .texts import load_texts
from .turn import identity_line as render_identity_line

logger = logging.getLogger(__name__)

BUNDLED_SKILL_PATH = Path(__file__).parent / "skills" / "agentchat" / "SKILL.md"

TEXTS_UNAVAILABLE_DETAIL = (
    "identity line not placed: /v1/texts is unreachable and nothing is cached — the gateway "
    "places it once the server answers (or run `hermes agentchat doctor --fix` later)"
)


@dataclass(frozen=True)
class PlacementStep:
    """One placement outcome: ``name`` ∈ anchor | skill | secrets | mcp; ``status`` ∈ ok | changed | skipped | failed."""

    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "failed"


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
    which run outside the gateway's receive loop.
    Never raises.
    """
    try:
        from agentchatme import AgentChatClient
    except ImportError:
        logger.debug("placement: agentchatme not importable; no identity line")
        return None
    home = hermes_home if hermes_home is not None else resolve_hermes_home()
    account_dir = state_dir_for_account(
        hermes_home=home, api_base=api_base, api_key=api_key, state_dir=state_dir
    )
    client = AgentChatClient(
        api_key=api_key, base_url=api_base, client_identity=hermes_client_identity()
    )
    try:
        texts = load_texts(client, state_dir=account_dir)
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("placement: client close raised", exc_info=True)
    return render_identity_line(texts) if texts is not None else None


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


def place_secrets() -> PlacementStep:
    """``secrets.agentchat``: the key source on, ``AGENTCHAT_API_KEY`` kept out of the agent's commands."""
    from .secrets_registration import install_block

    try:
        outcome = install_block()
    except McpRegistrationError as exc:
        return PlacementStep("secrets", "failed", str(exc))
    except Exception as exc:  # Hermes' config layer raised something of its own
        return PlacementStep("secrets", "failed", f"Hermes refused the config.yaml write: {exc}")
    if outcome in ("added", "completed"):
        return PlacementStep(
            "secrets",
            "changed",
            "added secrets.agentchat to config.yaml: Hermes reads the key from this profile's key "
            "file for the hosted tools and keeps AGENTCHAT_API_KEY out of every command the agent "
            "runs (used from the next gateway restart, or /reload-mcp in a session)",
        )
    if outcome == "present":
        return PlacementStep("secrets", "ok", "secrets.agentchat present in config.yaml")
    if outcome == "disabled":
        return PlacementStep(
            "secrets",
            "skipped",
            "secrets.agentchat is disabled in config.yaml — left alone; the hosted tools cannot "
            "read the key file",
        )
    return PlacementStep(
        "secrets", "skipped", "secrets.agentchat is managed by hand in config.yaml — left alone"
    )


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
    if outcome == "upgraded":
        return PlacementStep(
            "mcp",
            "changed",
            "added the client header to mcp_servers.agentchat (X-AgentChat-Client: hermes), so "
            "the hosted tools know this agent runs on Hermes (used after a gateway restart, or "
            "/reload-mcp in a session)",
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
    """Anchor, skill, the key source, tools — in that order (the tools' entry needs the key source). Never raises."""
    return [
        place_anchor(handle=handle, identity_line=identity_line, soul_path=soul_path),
        check_skill(),
        place_secrets(),
        place_mcp(api_base),
    ]
