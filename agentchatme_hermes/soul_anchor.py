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

Recognising our own block
─────────────────────────
The block's text and its parsing are the SDK kit's
(``agentchatme.integration.anchor``, one copy for every integration, pinned by
the shared conformance cases): the fences, a one-line handle comment
(``<!-- agentchat:handle ops -->``) so a block is recognised whatever the
served wording becomes, recognition of unmarked blocks by the served
``identity_line_template`` and by the wordings earlier releases wrote, and
the legacy ``agentchat-skill`` markers. A block in none of these shapes was
hand-edited and is the user's: left alone, and ``doctor`` says so. This
module only picks the file (the profile's ``SOUL.md``) and reads and writes it.

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
from pathlib import Path
from typing import TYPE_CHECKING

from agentchatme.integration import anchor

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# The kit's markers (shared with every integration, so a workspace that ends
# up with two integrations sees one canonical block, not two).
ANCHOR_START = anchor.START_MARKER
ANCHOR_END = anchor.END_MARKER


class AnchorError(RuntimeError):
    """The identity block could not be written as asked.

    The line given does not name this agent (or the handle is empty): fail
    loud at wizard time so the operator can fix it before the agent reads a
    wrong identity on every turn.
    """


normalize_handle = anchor.normalize_handle
render_handle_marker = anchor.handle_line


def render_anchor_block(identity_line: str, *, handle: str) -> str:
    """The fenced block for SOUL.md: markers, the handle line, the served line.

    Raises:
        AnchorError: the handle is empty or the line does not name ``@handle``.
    """
    try:
        return anchor.render({"identity_line": identity_line, "identity_line_template": ""}, handle)
    except ValueError as exc:
        raise AnchorError(str(exc)) from exc


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
    * Refuses a line that does not name ``@handle`` before writing.
    * Creates the file (and parent dirs) if missing.
    * Replaces the first block in place and removes any other (current or
      legacy); appends the block after a blank line when there is none
      (``agentchatme.integration.anchor.replace``).
    * Reads the file back and checks the block names ``@<handle>``.

    Returns:
        The :class:`Path` of the file that was written.

    Raises:
        AnchorError: handle or line empty, or post-write verify failed.
        OSError: filesystem write failed (caller decides whether to
            surface as user-facing failure or log-and-continue).
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    trimmed = normalize_handle(handle)
    # Checked before anything is written: a line that names nobody (or
    # somebody else) must not reach the system prompt.
    block = render_anchor_block(identity_line, handle=trimmed)

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(anchor.replace(existing, block), encoding="utf-8")

    found = anchor.find(target.read_text(encoding="utf-8"))
    if found is None or found.handle != trimmed or f"@{trimmed}" not in found.body:
        raise AnchorError(
            f"write_soul_anchor: handle @{trimmed} did not land in {target} — remove the "
            "agentchat block manually and re-run.",
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
    content = _read(target)
    return content is not None and anchor.find(content) is not None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_anchor_handle(
    *,
    soul_path: Path | None = None,
    template: str | None = None,
    templates: Iterable[str] = (),
) -> str | None:
    """Return the handle named inside the fenced block, or ``None``.

    The block's own handle line wins; without one (a block written by an
    earlier release) the served ``template`` (and any further ``templates``)
    and the wordings earlier releases wrote are tried. ``None`` when the
    file or the block is absent, or the block was hand-edited into a shape
    not recognised. Used at plugin start to detect a profile whose SOUL.md
    says one identity while its API key authenticates as another — the
    mix-up behind the September 2026 fleet incident, where a profile
    labelled *Growth* was sending as *Operations*.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    content = _read(target)
    if content is None:
        return None
    # The kit always tries the handle line and the earlier wordings; each
    # served template is one more way to recognise an unmarked block.
    candidates: list[str | None] = [t for t in (template, *templates) if t]
    for candidate in candidates or [None]:
        found = anchor.find(content, template=candidate)
        if found is None:
            return None
        if found.handle is not None:
            return found.handle
    return None


def anchor_is_current(
    identity_line: str, *, soul_path: Path | None = None, template: str | None = None
) -> bool | None:
    """``True`` when the block's text is exactly ``identity_line``.

    ``False`` for a recognised block with other text (safe to refresh);
    ``None`` when there is no block, or the block names no handle we
    recognise (a hand-edited block is the user's, and is left alone).
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    content = _read(target)
    found = anchor.find(content, template=template) if content is not None else None
    if found is None or found.handle is None:
        return None
    return found.body.strip() == identity_line.strip()


def remove_soul_anchor(*, soul_path: Path | None = None) -> bool:
    """Idempotently strip the AgentChat identity block from SOUL.md.

    Returns:
        ``True`` if a block was found and removed, ``False`` if the file or
        the block was absent.

    Notes:
        If the file becomes empty after stripping, it is left as an empty
        file (not deleted) — Hermes treats absent and empty the same way.
        Removing the file might surprise a user who hand-wrote other
        content there; an empty file is the safer default.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    existing = _read(target)
    if existing is None:
        return False
    stripped = anchor.remove(existing)
    if stripped == existing:
        return False
    target.write_text(stripped, encoding="utf-8")
    logger.info("SOUL.md anchor removed from %s", target)
    return True
