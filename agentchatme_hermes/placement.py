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
3. **The manual** — the one the served texts announce
   (``https://agentchat.me/skill.md``, with its version and sha256), as the
   ``agentchat:agentchat`` skill: ``<profile home>/plugin-data/agentchat/
   manual/SKILL.md``, downloaded and checked by the SDK kit when it is not the
   announced one, the bundled copy only when nothing verified can be had
   (:mod:`.manual`). The gateway keeps it current from then on.
4. **The tools** — ``mcp_servers.agentchat`` in the profile's
   ``config.yaml`` (:mod:`.mcp_registration`), with
   ``Authorization: "Bearer ${AGENTCHAT_API_KEY}"`` as a template: the key
   never lands in ``config.yaml``. Written only once a person decided whether
   the agent may use them on its own (``trust: full`` or ``untrusted``;
   :mod:`.allow`).

Every placer is idempotent and reports what it did; the wizard prints the
report, the activation path logs it, ``doctor --fix`` runs it again.

Removing AgentChat (``logout``) takes out only what is this profile's: the
identity line goes only when the block is the one AgentChat placed for this
profile's agent (:func:`profile_identity` says who that is, read before the
key is removed; :func:`.soul_anchor.remove_soul_anchor` decides).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import manual as manual_module
from .client_identity import hermes_client_identity
from .manual import BUNDLED_MANUAL_PATH
from .mcp_registration import McpRegistrationError, install_entry
from .profile_scope import resolve_hermes_home
from .soul_anchor import AnchorError, place_soul_anchor
from .state import state_dir_for_account
from .texts import load_texts
from .turn import identity_line as render_identity_line

if TYPE_CHECKING:
    from agentchatme.types import Texts

    from .mcp_registration import Decision

logger = logging.getLogger(__name__)

# The package's copy of the manual: the fallback bytes only (see :mod:`.manual`).
BUNDLED_SKILL_PATH = BUNDLED_MANUAL_PATH

TEXTS_UNAVAILABLE_DETAIL = (
    "identity line not placed: /v1/texts is unreachable and nothing is cached — the gateway "
    "places it once the server answers (or run `hermes agentchat doctor --fix` later)"
)


@dataclass(frozen=True)
class PlacementStep:
    """One placement outcome: ``name`` ∈ anchor | manual | secrets | mcp; ``status`` ∈ ok |
    changed | warned | skipped | failed. ``warned``: done as far as it can be and
    nothing broken, but not current (a manual left at the version it has)."""

    name: str
    status: str
    detail: str
    # What the placer did, where callers branch on it (``place_mcp``: the
    # :data:`.mcp_registration.EnsureOutcome`); ``None`` otherwise.
    outcome: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def needs_attention(self) -> bool:
        """Printed as a warning by the wizard and the CLI (``failed``,
        ``warned`` or ``skipped``)."""
        return self.status in ("failed", "warned", "skipped")


# ─── the served identity line ──────────────────────────────────────────────


def resolve_identity_line(
    *,
    api_key: str,
    api_base: str,
    hermes_home: Path | None = None,
    state_dir: Path | None = None,
) -> str | None:
    """The served identity line for the account behind ``api_key``, as served for Hermes.

    :func:`resolve_texts`, then its identity line. Never raises.
    """
    texts = resolve_texts(api_key=api_key, api_base=api_base, hermes_home=hermes_home, state_dir=state_dir)
    return render_identity_line(texts) if texts is not None else None


def resolve_texts(
    *,
    api_key: str,
    api_base: str,
    hermes_home: Path | None = None,
    state_dir: Path | None = None,
) -> Texts | None:
    """The served texts for the account behind ``api_key``, as served for Hermes:
    the identity line and the manual's announcement.

    One fetch through the SDK (writing the account's texts cache), else the
    cached copy, else ``None``. For the wizard, the CLI and ``doctor``,
    which run outside the gateway's receive loop.
    Never raises.
    """
    try:
        from agentchatme import AgentChatClient
    except ImportError:
        logger.debug("placement: agentchatme not importable; no texts")
        return None
    home = hermes_home if hermes_home is not None else resolve_hermes_home()
    account_dir = state_dir_for_account(
        hermes_home=home, api_base=api_base, api_key=api_key, state_dir=state_dir
    )
    client = AgentChatClient(
        api_key=api_key, base_url=api_base, client_identity=hermes_client_identity()
    )
    try:
        return load_texts(client, state_dir=account_dir)
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("placement: client close raised", exc_info=True)


# ─── identity, manual, tools ───────────────────────────────────────────────


def place_anchor(
    *,
    handle: str,
    identity_line: str | None,
    soul_path: Path | None = None,
    template: str | None = None,
    replace_other: bool = True,
) -> PlacementStep:
    """The served identity line in SOUL.md, on request (:func:`.soul_anchor.place_soul_anchor`):
    this agent's block brought to the served line, one placed where there is
    none; a block edited by hand, or several blocks, left (``warned``, with the
    remedy); another agent's block replaced only with ``replace_other`` (login,
    the wizard), refused otherwise (``doctor --fix``). The detail says what it
    was. Never raises."""
    if identity_line is None:
        return PlacementStep("anchor", "skipped", TEXTS_UNAVAILABLE_DETAIL)
    try:
        placement = place_soul_anchor(
            identity_line, handle=handle, soul_path=soul_path, template=template, replace_other=replace_other
        )
    except (AnchorError, OSError) as exc:
        return PlacementStep("anchor", "failed", f"could not write the identity line to SOUL.md: {exc}")
    if placement.outcome == "left":
        return PlacementStep("anchor", "warned", placement.detail)
    if not placement.ok:
        return PlacementStep("anchor", "failed", placement.detail)
    return PlacementStep("anchor", "changed" if placement.wrote else "ok", placement.detail)


def place_manual(
    served: Any, *, api_base: str | None, hermes_home: Path | None = None
) -> PlacementStep:
    """The manual the served texts announce (``served``: ``texts.manual``, ``None``
    when no texts are in hand) as this profile's ``agentchat:agentchat`` file.

    One sync through the SDK kit (a download when the file is not the announced
    manual); the detail says what the file was when the sync changed it
    (``was stale (…) → 2.4.4 placed …``, ``was tampered (…) → restored: …``).
    A manual not at the announced version is a warning, never a
    failed placement: the agent reads the file it has (the old one, or the
    bundled copy) and the gateway keeps trying; only no manual at all fails.
    ``api_base`` is the API base the texts came from: it decides where a
    download may go. Never raises.
    """
    home = hermes_home if hermes_home is not None else resolve_hermes_home()
    before = manual_module.check(manual_module.manual_path(home), served)
    result = manual_module.sync_once(home=home, served=served, api_base=api_base)
    if result is None:
        return PlacementStep(
            "manual",
            "failed",
            f"the agentchatme SDK kit is not installed, so the manual cannot be placed at "
            f"{manual_module.manual_path(home)} (the agent reads the bundled copy)",
        )
    detail = manual_module.describe_repair(before, result)
    if result.state == "failed":
        return PlacementStep("manual", "failed", f"{detail} (the agent reads the bundled copy)")
    if result.up_to_date is False:
        return PlacementStep("manual", "warned", f"{detail} (the gateway keeps trying)")
    return PlacementStep("manual", "changed" if result.written else "ok", detail)


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
            "runs (used from the next start of the gateway and of any open Hermes session)",
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


def place_mcp(api_base: str, *, decision: Decision | None = None) -> PlacementStep:
    """``mcp_servers.agentchat`` as ``decision`` asks (:func:`.mcp_registration.install_entry`):
    with no decision nothing is added (``missing``). A written or allowed
    entry is recorded in the profile's own data (:func:`.allow.write_record`).

    A yes over our ``untrusted`` entry while a gateway runs for this profile
    is the first of two steps (``applying``): the decision is recorded, the
    entry removed (Hermes' watcher drops the connected server), and the
    gateway writes it with ``trust: full`` once it holds no AgentChat tools
    any more (:func:`.allow.start_completion`). With no gateway running the
    entry is changed in place (``allowed``). Never raises."""
    home = resolve_hermes_home()
    if decision == "yes":
        flip = _begin_two_step_yes(api_base, home)
        if flip is not None:
            return flip
    try:
        placed = install_entry(api_base=api_base, decision=decision)
    except McpRegistrationError as exc:
        return PlacementStep("mcp", "failed", str(exc))
    except Exception as exc:  # Hermes' config layer raised something of its own
        return PlacementStep("mcp", "failed", f"Hermes refused the config.yaml write: {exc}")
    outcome = placed.outcome
    if outcome in ("added", "allowed"):
        from .allow import write_record

        write_record(
            home,
            how="carried_over" if decision == "carried_over" else "asked",
            trust=placed.trust,
            decision=decision if decision in ("yes", "no") else None,
        )
    if outcome == "added":
        return PlacementStep(
            "mcp",
            "changed",
            f"registered the hosted tools as mcp_servers.agentchat in config.yaml with trust: {placed.trust} "
            "(a running gateway connects them within about a minute; a session on /reload-mcp)",
            outcome,
        )
    if outcome == "allowed":
        named = " and named this client (X-AgentChat-Client: hermes)" if placed.header_added else ""
        return PlacementStep(
            "mcp",
            "changed",
            f"set trust: full on mcp_servers.agentchat{named} (a running gateway uses it after /reload-mcp "
            "in a chat it serves, or hermes gateway restart)",
            outcome,
        )
    if outcome == "upgraded":
        return PlacementStep(
            "mcp",
            "changed",
            "added the client header to mcp_servers.agentchat (X-AgentChat-Client: hermes), so "
            "the hosted tools know this agent runs on Hermes (used after a gateway restart, or "
            "/reload-mcp in a session)",
            outcome,
        )
    if outcome == "present":
        return PlacementStep("mcp", "ok", "hosted tools already registered as mcp_servers.agentchat", outcome)
    if outcome == "missing":
        return PlacementStep(
            "mcp",
            "skipped",
            "hosted tools not registered: AgentChat tools are not allowed yet (the entry is added when the "
            "person allows them: /agentchat allow)",
            outcome,
        )
    if outcome == "disabled":
        return PlacementStep(
            "mcp", "skipped", "mcp_servers.agentchat exists but is disabled in config.yaml — left alone", outcome
        )
    return PlacementStep(
        "mcp", "skipped", "mcp_servers.agentchat is managed by hand in config.yaml — left alone", outcome
    )


def _begin_two_step_yes(api_base: str, home: Path) -> PlacementStep | None:
    """The first step of a yes over our connected ``untrusted`` entry
    (:func:`place_mcp`), or ``None`` when it does not apply (no such entry,
    or no gateway runs for this profile)."""
    from .allow import APPLY_WORDS, gateway_lives, write_record
    from .mcp_registration import inspect_allowance, uninstall_entry

    try:
        current = inspect_allowance(api_base=api_base)
    except Exception:
        return None
    if current.entry != "ours" or current.trust != "untrusted" or not gateway_lives(home, api_base=api_base):
        return None
    if not write_record(home, how="asked", trust="full", decision="yes", pending=True):
        return PlacementStep("mcp", "failed", "the decision could not be recorded, so nothing was changed")
    try:
        uninstall_entry(api_base=api_base)
    except Exception as exc:
        write_record(home, how="asked", trust="untrusted", decision="yes")
        return PlacementStep("mcp", "failed", f"mcp_servers.agentchat could not be changed: {exc}")
    return PlacementStep(
        "mcp",
        "changed",
        f"trust: full is applied in two steps ({APPLY_WORDS}): mcp_servers.agentchat is removed so the "
        "running gateway drops the connection it made untrusted, then the gateway writes it back with "
        "trust: full and connects it again",
        "applying",
    )


def place_identity_and_tools(
    *,
    handle: str,
    identity_line: str | None,
    api_base: str,
    soul_path: Path | None = None,
    served_manual: Any = None,
    hermes_home: Path | None = None,
    identity_template: str | None = None,
    replace_other_agent: bool = True,
    tools_decision: Decision | None = None,
) -> list[PlacementStep]:
    """Anchor, manual, the key source, tools — in that order (the tools' entry
    needs the key source). ``served_manual`` is the texts' announcement
    (``texts.manual``), ``None`` when no texts are in hand; ``identity_template``
    the served template (recognises a block an earlier release wrote);
    ``replace_other_agent`` False keeps another agent's block (``doctor --fix``);
    ``tools_decision`` what the person decided about the tools
    (:func:`.allow.decide`; ``None``: nobody asked, no entry is added).
    Never raises."""
    return [
        place_anchor(
            handle=handle,
            identity_line=identity_line,
            soul_path=soul_path,
            template=identity_template,
            replace_other=replace_other_agent,
        ),
        place_manual(served_manual, api_base=api_base, hermes_home=hermes_home),
        place_secrets(),
        place_mcp(api_base, decision=tools_decision),
    ]


# ─── removal (logout) ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProfileIdentity:
    """Who a profile is configured as, from this machine alone: what removing
    AgentChat compares SOUL.md's block with. ``handle`` ``None``: not known (no
    key file names one, no ``AGENTCHATME_HANDLE``); ``identity_line`` and
    ``template``: the served texts cached for the profile's key, as Hermes
    places them, ``None`` when nothing is cached."""

    handle: str | None
    identity_line: str | None = None
    template: str | None = None


def profile_identity(home: Path, *, api_base: str) -> ProfileIdentity:
    """Who ``home``'s profile is configured as — read BEFORE logout removes the
    key: the handle its key file names (a copied key file too: it names the
    agent this profile held the key of), else ``AGENTCHATME_HANDLE`` (a profile
    set up before 0.6); the served identity line and template cached for the
    key it holds. No network. Never raises."""
    from .config import ENV_HANDLE, resolve_key
    from .profile_scope import resolve_setting
    from .texts import cached_texts
    from .turn import identity_line_template as render_identity_line_template

    try:
        resolution = resolve_key(home)
    except Exception:
        logger.debug("placement: key resolution raised", exc_info=True)
        return ProfileIdentity(None)
    stored = resolution.store.key
    raw = (stored.handle if stored is not None else "") or ""
    if not raw.strip():
        try:
            raw = resolve_setting(ENV_HANDLE) or ""
        except Exception:
            raw = ""
    handle = raw.strip().lstrip("@").lower() or None
    api_key = resolution.held_key or (stored.api_key if stored is not None else None)
    if not api_key:
        return ProfileIdentity(handle)
    try:
        texts, _ = cached_texts(state_dir_for_account(hermes_home=home, api_base=api_base, api_key=api_key))
    except Exception:
        logger.debug("placement: cached texts unreadable", exc_info=True)
        texts = None
    if texts is None:
        return ProfileIdentity(handle)
    return ProfileIdentity(handle, render_identity_line(texts), render_identity_line_template(texts))


def gateway_still_serving(home: Path, *, api_base: str) -> str | None:
    """What to say when logout leaves a running gateway behind: read BEFORE the
    key is removed. A gateway that lives for this profile's account (its health
    record fresh, its pid alive, the leader lock held: the rule ``doctor`` and
    ``status`` use) keeps the socket it opened with the key, so the agent is
    still woken and gated, now without the hosted tools, until it restarts.
    ``None`` when no gateway lives for it. No network. Never raises."""
    from .config import resolve_key
    from .profile_scope import profile_label
    from .ws_health import live_health

    try:
        resolution = resolve_key(home)
        stored = resolution.store.key
        api_key = resolution.held_key or (stored.api_key if stored is not None else None)
        if not api_key:
            return None
        health = live_health(api_base, api_key, home)
        if health is None:
            return None
        label = profile_label(home)
    except Exception:
        logger.debug("placement: gateway liveness not read", exc_info=True)
        return None
    restart = _gateway_restart_command(home, label, health.get("pid"))
    return (
        f"The running gateway (pid {health['pid']}) still holds @{health['handle']}'s AgentChat connection, so this "
        f"profile is still woken and gated, now without the hosted tools: restart it ({restart}) so it stops "
        "serving this profile"
    )


def default_gateway_pid(home: Path) -> int | None:
    """The pid of the default profile's running gateway when ``home`` is a named
    profile (Hermes' own check, ``gateway.status.get_running_pid`` scoped to the
    root's ``gateway.pid``, read-only), else ``None``: not a named profile, no
    such gateway, or a Hermes without that check. Never raises."""
    from .profile_scope import hermes_root_of

    try:
        root = hermes_root_of(Path(home))
        if root == Path(home):
            return None
        from gateway.status import get_running_pid  # type: ignore[import-not-found]

        pid = get_running_pid(root / "gateway.pid", cleanup_stale=False)
    except Exception:
        logger.debug("placement: the default profile's gateway pid not read", exc_info=True)
        return None
    return pid if isinstance(pid, int) else None


def _gateway_restart_command(home: Path, label: str, pid: object) -> str:
    """The restart that frees the gateway holding this profile's socket. The
    default profile's gateway, and a gateway that multiplexes profiles (it is
    the default profile's: the pid the health record names), restart with
    `hermes gateway restart`; `hermes -p <name> gateway restart` on a profile
    such a gateway serves reconnects only Hermes' own adapters, not this
    socket. Only a named profile with a gateway of its own takes `-p`."""
    if label == "default":
        return "`hermes gateway restart`"
    host = default_gateway_pid(home)
    if host is not None and host == pid:
        return "`hermes gateway restart`: the default profile's gateway serves this profile"
    if host is not None:
        return f"`hermes -p {label} gateway restart`: this profile's own gateway"
    return (
        f"`hermes gateway restart` when the default profile's gateway serves this profile, "
        f"else `hermes -p {label} gateway restart`"
    )
