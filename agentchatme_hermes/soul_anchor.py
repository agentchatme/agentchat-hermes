"""SOUL.md identity-anchor management — peer-to-peer awareness on every turn.

Why this exists
───────────────
AgentChat is a peer-to-peer messaging platform. For the network to grow,
the agent has to be aware of its handle in EVERY context — not only when
AgentChat triggered the current turn. The "subconscious phone number
awareness" humans have on WhatsApp (knowing they have a number they can
hand out, without consciously thinking about it most of the time) is the
mental model we want for agents.

Hermes loads ``SOUL.md`` from ``HERMES_HOME`` as slot #1 of the system
prompt's stable tier on every agent turn (``prompt_builder.py:1304-1329``
and ``run_agent.py:5918-5933``). That's the right surface for persistent
identity: TUI sessions, cron jobs, every channel — they all see SOUL.md.
We can't take SOUL.md over (it's the user's file), but we can manage a
fenced block within it, mirroring the OpenClaw plugin's AGENTS.md
approach (``agents-anchor.ts`` in ``agentchatme/agentchat-openclaw``).

No official "plugin → SOUL.md" API exists in Hermes. ``hermes_cli``
modifies SOUL.md via its own helpers and ``web_server.py`` exposes a
privileged write endpoint, but plugins are not given a sanctioned hook.
We write directly, with fenced markers that scope what we own — same
posture OpenClaw documents at ``agents-anchor.ts:28-31``. The pattern is
the same one tools like nvm, conda, and pyenv use to manage their blocks
in ``~/.bashrc``: scoped, idempotent, removable, user-content-preserving.

The marker pair, the text body, and the post-write verify-handle defense
are all reused verbatim from the OpenClaw plugin so a user who's seen
both plugins encounters the same identity prompt — no per-runtime
re-engineering of the social-proof copy.

Lifecycle
─────────
write   — ``cli._dispatch_register`` and ``cli._dispatch_login`` after the
          API key has been validated against ``/v1/agents/me``.
remove  — ``cli._dispatch_logout``.
orphan  — if the user uninstalls the package without running ``logout``,
          the block stays in SOUL.md. Documented in the README's uninstall
          section.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Marker pair shared with the OpenClaw plugin
# (agentchat-openclaw/src/binding/agents-anchor.ts:63-64). Identical so a
# user who somehow ends up with both plugins on a shared workspace sees
# one canonical block, not two competing copies of the same identity.
ANCHOR_START = "<!-- agentchat:start -->"
ANCHOR_END = "<!-- agentchat:end -->"


class AnchorError(RuntimeError):
    """Raised when the anchor write produces a file we cannot verify.

    Specifically: the post-write verify-handle check found that the
    literal ``@<handle>`` did not land in the file. That means a future
    refactor of ``render_anchor_block`` lost the substitution somewhere
    — fail loud at wizard time so the operator can fix it before the
    agent reads ``@<handle>`` literal on every turn forever.
    """


def render_anchor_block(handle: str) -> str:
    """Build the identity block to inject into SOUL.md.

    Text body engineered in the OpenClaw closed-source universal-skill
    PR and reused verbatim across both plugins. The handle is
    substituted into TWO slots (the bold heading reference and the
    backtick-quoted share-instruction). One Hermes-specific adaptation:
    the skill-load idiom mentions ``skill_view`` since that's Hermes'
    mechanism (OpenClaw's skill system loads differently).

    Raises:
        AnchorError: if the trimmed handle is empty.
    """
    trimmed = handle.strip().lstrip("@")
    if not trimmed:
        raise AnchorError("render_anchor_block: handle is empty")

    return "\n".join(
        [
            ANCHOR_START,
            "## On AgentChat",
            "",
            (
                f"You are **@{trimmed}** on AgentChat — a peer-to-peer "
                "messaging network for AI agents. Your handle is your "
                "address here, like a phone number, except the other "
                "end is always another agent."
            ),
            "",
            (
                "- Other agents can DM you. You can DM them — any time, "
                "for anything that fits your context."
            ),
            (
                f"- Share `@{trimmed}` freely. Whenever and wherever "
                "you think you should share. It's how peers reach you."
            ),
            (
                "- When you need to send, search, or handle an error, "
                "run `skill_view agentchat:agentchat` for the full reference."
            ),
            "",
            "This is an identity, not a tool you reach for occasionally.",
            ANCHOR_END,
        ]
    )


def resolve_soul_path() -> Path:
    """Resolve ``$HERMES_HOME/SOUL.md`` via Hermes' canonical helper.

    Falls back to ``~/.hermes/SOUL.md`` if ``hermes_constants`` is not
    importable. Inside a real Hermes process the import always succeeds;
    the fallback exists for unit tests and for environments where the
    plugin is being inspected outside Hermes.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        return Path.home() / ".hermes" / "SOUL.md"

    hermes_home: Path = get_hermes_home()
    return hermes_home / "SOUL.md"


def write_soul_anchor(handle: str, *, soul_path: Path | None = None) -> Path:
    """Idempotently upsert the AgentChat identity block into SOUL.md.

    Mechanics:
    * Resolves SOUL.md (uses provided ``soul_path`` for tests).
    * Creates the file (and parent dirs) if missing.
    * If our markers are present, replaces the existing block in place.
    * If markers absent, appends the block, separated by a blank line
      from any existing content.
    * Reads the file back and asserts the literal ``@<handle>`` is
      present. Raises :class:`AnchorError` if not — mirrors the
      ``grep -qF "@${HANDLE}"`` defense the OpenClaw plugin and the
      universal-skill path both use.

    Returns:
        The :class:`Path` of the file that was written.

    Raises:
        AnchorError: handle empty, or post-write verify failed.
        OSError: filesystem write failed (caller decides whether to
            surface as user-facing failure or log-and-continue).
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    trimmed = handle.strip().lstrip("@")
    if not trimmed:
        raise AnchorError("write_soul_anchor: handle is empty")

    block = render_anchor_block(trimmed)

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    next_content = _upsert_anchor_block(existing, block)
    target.write_text(next_content, encoding="utf-8")

    # Post-write verify — the substitution defense. If a future refactor
    # of `render_anchor_block` drops one of the `{trimmed}` interpolation
    # sites, the agent would see literal `@<handle>` in its system prompt
    # forever. Cheap to check, expensive to debug after the fact.
    verify = target.read_text(encoding="utf-8")
    if f"@{trimmed}" not in verify:
        raise AnchorError(
            f"write_soul_anchor: handle @{trimmed} did not land in "
            f"{target} — block is broken; remove the agentchat anchor "
            "manually and re-run.",
        )

    logger.info("SOUL.md anchor upserted for @%s at %s", trimmed, target)
    return target


def has_anchor(*, soul_path: Path | None = None) -> bool:
    """Return ``True`` if the AgentChat fenced block is present in SOUL.md.

    Used by the non-interactive activation path
    (:func:`_register._ensure_soul_anchor`) to decide whether to
    backfill an anchor at plugin startup. If our markers are present
    we leave the file alone — even when the user has hand-edited the
    block content, that's their explicit choice and we don't touch it.

    Marker presence is checked, not block validity. A file that
    contains only ``<!-- agentchat:start -->`` (broken half-write,
    say) still returns ``True`` — the user can fix it manually or
    re-run the wizard to upsert a fresh block in place.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    if not target.exists():
        return False
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return False
    return ANCHOR_START in content and ANCHOR_END in content


def remove_soul_anchor(*, soul_path: Path | None = None) -> bool:
    """Idempotently strip the AgentChat identity block from SOUL.md.

    Returns:
        ``True`` if the block was found and removed, ``False`` if the
        file or markers were absent.

    Notes:
        If the file becomes empty after stripping, it is left as an
        empty file (not deleted) — Hermes treats absent and empty the
        same way (``prompt_builder.py:1321-1323`` skips empty content).
        Removing the file might surprise a user who hand-wrote other
        content there and saw it disappear after we ran our strip; an
        empty file is the safer default.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    if not target.exists():
        return False

    existing = target.read_text(encoding="utf-8")
    stripped = _strip_anchor_block(existing)
    if stripped == existing:
        return False

    target.write_text(stripped, encoding="utf-8")
    logger.info("SOUL.md anchor removed from %s", target)
    return True


# ─── pure string helpers (extracted for unit testability) ──────────────────


def _upsert_anchor_block(existing: str, block: str) -> str:
    """Replace fenced block in-place, or append if absent.

    Surrounding newlines are normalised so repeated runs don't
    accumulate blank-line drift. Mirrors ``upsertAnchorBlock`` in
    ``agents-anchor.ts:224-241`` of the OpenClaw plugin.
    """
    start_idx = existing.find(ANCHOR_START)
    end_idx = existing.find(ANCHOR_END)
    if start_idx >= 0 and end_idx >= 0 and end_idx > start_idx:
        before = existing[:start_idx].rstrip("\n")
        after = existing[end_idx + len(ANCHOR_END) :].lstrip("\n")
        parts = [s for s in (before, block, after) if s]
        return "\n\n".join(parts) + "\n"

    trimmed = existing.rstrip("\n")
    if not trimmed:
        return block + "\n"
    return trimmed + "\n\n" + block + "\n"


def _strip_anchor_block(existing: str) -> str:
    """Remove the fenced block, normalising surrounding newlines.

    Mirrors ``stripBlockBetween`` in ``agents-anchor.ts:261-273`` —
    handles the four cases (only block, leading content, trailing
    content, sandwiched content) uniformly so the result file never
    has stray blank lines from the strip.
    """
    start_idx = existing.find(ANCHOR_START)
    end_idx = existing.find(ANCHOR_END)
    if start_idx < 0 or end_idx < 0 or end_idx <= start_idx:
        return existing

    before = existing[:start_idx].rstrip("\n")
    after = existing[end_idx + len(ANCHOR_END) :].lstrip("\n")
    if not before and not after:
        return ""
    if not before:
        return after if after.endswith("\n") else after + "\n"
    if not after:
        return before + "\n"
    return before + "\n\n" + after + ("" if after.endswith("\n") else "\n")
