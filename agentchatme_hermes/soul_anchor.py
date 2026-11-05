"""SOUL.md identity-anchor management — the identity line, placed where Hermes loads it.

Why this exists
───────────────
Being on AgentChat starts with knowing who you are there: which ``@handle``,
and that the other side of every message is a peer agent, not your human.
Hermes loads ``SOUL.md`` from the profile home as slot #1 of the system
prompt on every agent turn (``prompt_builder.py``), so that is where the
identity line goes — TUI sessions, cron jobs, every channel see it.

The text is not ours. It is served by ``GET /v1/texts`` as
``identity_line`` with this agent's handle filled in (:mod:`.texts`), and
spelled for Hermes' tool names by :mod:`.turn`. This module only places
whatever line it is given; it carries no copy and no template. When the
texts are not available (server unreachable, nothing cached) nothing is
placed, and the runtime places the line as soon as a fetch succeeds.

We can't take SOUL.md over (it's the user's file), but we can manage a
fenced block within it, mirroring the OpenClaw plugin's AGENTS.md approach.
No official "plugin → SOUL.md" API exists in Hermes, so we write directly,
with markers that scope what we own — the pattern nvm, conda and pyenv use
for their blocks in ``~/.bashrc``: scoped, idempotent, removable,
user-content-preserving.

Lifecycle
─────────
write   — the wizard and ``cli`` after the API key has been validated
          against ``/v1/agents/me``; the activation path backfills it.
refresh — an anchor from an older release (different text, same handle)
          is rewritten to the served text by the activation path and
          ``doctor --fix``; a hand-edited block is left alone.
remove  — ``hermes agentchat logout``.
orphan  — if the user uninstalls the package without running ``logout``,
          the block stays in SOUL.md. Documented in the README.
"""
from __future__ import annotations

import logging
import re
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
    literal ``@<handle>`` did not land in the file. That means the line
    we were given does not name this agent — fail loud at wizard time so
    the operator can fix it before the agent reads a wrong identity on
    every turn forever.
    """


def normalize_handle(handle: str) -> str:
    """Handles are canonical lowercase on the wire; strip ``@`` and whitespace."""
    return handle.strip().lstrip("@").lower()


def render_anchor_block(identity_line: str) -> str:
    """The fenced block to inject into SOUL.md: markers around the served line.

    Raises:
        AnchorError: if the line is empty.
    """
    line = identity_line.strip()
    if not line:
        raise AnchorError("render_anchor_block: identity line is empty")
    return "\n".join([ANCHOR_START, line, ANCHOR_END])


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


def write_soul_anchor(identity_line: str, *, handle: str, soul_path: Path | None = None) -> Path:
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
        AnchorError: handle or line empty, or post-write verify failed.
        OSError: filesystem write failed (caller decides whether to
            surface as user-facing failure or log-and-continue).
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    trimmed = normalize_handle(handle)
    if not trimmed:
        raise AnchorError("write_soul_anchor: handle is empty")

    block = render_anchor_block(identity_line)

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    next_content = _upsert_anchor_block(existing, block)
    target.write_text(next_content, encoding="utf-8")

    # Post-write verify — the identity defense. The served line carries
    # the handle the key authenticates as; a line that names nobody (or
    # somebody else) must not sit in the system prompt forever.
    verify = target.read_text(encoding="utf-8")
    if f"@{trimmed}" not in verify:
        raise AnchorError(
            f"write_soul_anchor: handle @{trimmed} did not land in "
            f"{target} — the identity line does not name this agent; remove the "
            "agentchat anchor manually and re-run.",
        )

    logger.info("SOUL.md anchor upserted for @%s at %s", trimmed, target)
    return target


def has_anchor(*, soul_path: Path | None = None) -> bool:
    """Return ``True`` if the AgentChat fenced block is present in SOUL.md.

    Used by the non-interactive activation path
    (:func:`_register._reconcile_soul_anchor`) to decide whether to
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


# The shapes of blocks this plugin has written, so an anchor from any
# earlier release is still recognised (and refreshed, see
# :func:`anchor_is_current`): the served line ("I am @handle on AgentChat"),
# then the pre-v3 block ("You are **@handle** on AgentChat"). A block in
# neither shape was hand-edited and is the user's.
_ANCHOR_HANDLE_RES = (
    re.compile(r"I am @([a-z0-9][a-z0-9-]*) on AgentChat"),
    re.compile(r"You are \*\*@([a-z0-9][a-z0-9-]*)\*\* on AgentChat"),
)
_HANDLE_PLACEHOLDER = "<handle>"


def _template_pattern(template: str | None) -> re.Pattern[str] | None:
    """A recogniser built from the served ``identity_line_template``, when given."""
    if not template or _HANDLE_PLACEHOLDER not in template:
        return None
    head, _sep, _tail = template.partition(_HANDLE_PLACEHOLDER)
    # The first line up to the placeholder is enough to recognise the shape;
    # whitespace is normalised so a reflowed block still matches.
    prefix = " ".join(head.split())
    if not prefix:
        return None
    return re.compile(re.escape(prefix) + r"\s*@?([a-z0-9][a-z0-9-]*)")


def read_anchor_handle(
    *, soul_path: Path | None = None, template: str | None = None
) -> str | None:
    """Return the handle named inside the fenced block, or ``None``.

    ``None`` when the file or the markers are absent, or when the block
    was hand-edited into a shape we do not recognise (the served template
    when ``template`` is given, the current and pre-v3 shapes always). Used
    at plugin start to detect a profile whose SOUL.md says one identity
    while its API key authenticates as another — the mix-up behind the
    September 2026 fleet incident, where a profile labelled *Growth* was
    sending as *Operations*.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return None
    start_idx = content.find(ANCHOR_START)
    end_idx = content.find(ANCHOR_END)
    if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
        return None
    block = content[start_idx:end_idx]
    patterns: list[re.Pattern[str]] = []
    from_template = _template_pattern(template)
    if from_template is not None:
        patterns.append(from_template)
    patterns.extend(_ANCHOR_HANDLE_RES)
    normalised = " ".join(block.split())
    for pattern in patterns:
        match = pattern.search(normalised) or pattern.search(block)
        if match:
            return match.group(1)
    return None


def anchor_is_current(identity_line: str, *, soul_path: Path | None = None) -> bool | None:
    """``True`` when the block holds exactly ``identity_line``.

    ``False`` for a recognised block with other text (safe to refresh);
    ``None`` when there is no block, or the block names no handle we
    recognise (a hand-edited block is the user's, and is left alone).
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    handle = read_anchor_handle(soul_path=target)
    if handle is None:
        return None
    content = target.read_text(encoding="utf-8")
    start_idx = content.find(ANCHOR_START)
    end_idx = content.find(ANCHOR_END)
    block = content[start_idx + len(ANCHOR_START) : end_idx].strip()
    return block == identity_line.strip()


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
