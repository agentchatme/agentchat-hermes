"""SOUL.md identity-anchor management — the identity line, placed where Hermes loads it.

Why this exists
───────────────
Being on AgentChat starts with knowing who you are there: which ``@handle``,
and that the other side of every message is a peer agent, not your human.
Hermes loads ``SOUL.md`` from the profile home as slot #1 of the system
prompt on every agent turn (``prompt_builder.py``), so that is where the
identity line goes — TUI sessions, cron jobs, every channel see it.

The text is not ours. It is the server's, spelled for Hermes' tool names:
by default this release's copy of the served ``identity_line_template``
filled for this agent's handle, with the plugin setting ``live_texts`` on the
``identity_line`` served by ``GET /v1/texts`` (:func:`.texts.identity_text`
decides, for every caller). This module only places whatever line it is
given; it carries no copy and no template. With ``live_texts`` on and the
texts not available (server unreachable, nothing cached) nothing is placed,
and the runtime places the line as soon as a fetch succeeds.

We can't take SOUL.md over (it's the user's file), but we can manage a
fenced block within it, mirroring the OpenClaw plugin's AGENTS.md approach.
No official "plugin → SOUL.md" API exists in Hermes, so we write directly,
with markers that scope what we own — the pattern nvm, conda and pyenv use
for their blocks in ``~/.bashrc``: scoped, idempotent, removable,
user-content-preserving.

Whose block it is
─────────────────
The block's text, its parsing and the rule for whose it is are the SDK kit's
(``agentchatme.integration.anchor``, one copy for every integration, pinned by
the shared conformance cases): the fences, a one-line handle comment
(``<!-- agentchat:handle ops -->``), the legacy ``agentchat-skill`` markers.
A block is this agent's only when it carries this agent's handle line, or
when, without one, its whole text is EXACTLY a text AgentChat served (the
served line, the served template filled in, one of the lines served before):
``anchor.owner``, strict. A block that merely mentions ``@handle`` in the
user's own words is not ours to rewrite. The loose reading (the template's
words anywhere in the block) is for display only.

One text more is this agent's, read here before the kit is asked: the block
the August 2026 build of this plugin wrote (:data:`AUGUST_2026_BLOCK`, no
handle line, never served, so the kit reads it as edited by hand), when the
whole block is exactly that text for this profile's handle. Every path below
treats it as this agent's own block: the refresh and a placement replace it
with the line in use once, ``doctor`` reports it as a line to refresh, and
``logout`` removes it. One character different is the person's, as for any
other block.

Lifecycle
─────────
refresh — the activation path (once with this release's line; with
          ``live_texts`` on, on every texts change): ``anchor.follow``
          brings this agent's own block to the served line and leaves every
          other block alone (another agent's is an IDENTITY MISMATCH; one
          edited by hand is the user's; several blocks are left as they are).
          With no block at all, the line is placed (the backfill).
place   — the wizard and ``register`` / ``login`` / ``recover`` after the key
          is validated, and ``doctor --fix``: this agent's own block is
          brought to the served line, and one is placed where there is none.
          A block edited by hand is the user's and is left (said, with the
          remedy: delete that block, then ``doctor --fix``); several blocks
          are left. Another agent's block is replaced by setup (``login``,
          the wizard: this profile becomes this agent, said) and refused by
          ``doctor --fix`` (a mismatch to fix).
remove  — ``hermes agentchat logout`` (and the wizard's logout): only the
          block AgentChat placed for this profile's agent — ``anchor.owner``
          (strict) is this profile's handle. A block edited by hand, another
          agent's, or several blocks are left as they are, and the step says
          so: removing AgentChat never takes the user's words, or another
          agent's identity, out of their file.
orphan  — if the user uninstalls the package without running ``logout``,
          the block stays in SOUL.md. Documented in the README.

Every write keeps the file as it was around the block: its line breaks
(CRLF stays CRLF), whether it ends with one, a byte-order mark, its mode, a
symlink (the file it points to is replaced). It is atomic (a temp file
beside it, then a rename), and a file that is not UTF-8 is not written.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# The kit release with the strict owner reading and ``follow`` (``anchor.owner``,
# ``decide``, ``follow``).
ANCHOR_KIT_REQUIREMENT = "agentchatme>=1.8.0"

# The kit's markers (shared with every integration, so a workspace that ends
# up with two integrations sees one canonical block, not two); equal to
# ``agentchatme.integration.anchor.START_MARKER`` / ``END_MARKER`` (a test
# checks). Written out so this module imports where the kit is missing and
# ``doctor`` can say so.
ANCHOR_START = "<!-- agentchat:start -->"
ANCHOR_END = "<!-- agentchat:end -->"


class AnchorError(RuntimeError):
    """The identity block could not be written as asked.

    The line given does not name this agent (or the handle is empty): fail
    loud at wizard time so the operator can fix it before the agent reads a
    wrong identity on every turn.
    """


def normalize_handle(handle: str) -> str:
    """Handles are canonical lowercase on the wire; a leading ``@`` is display only."""
    from agentchatme.integration import anchor

    return anchor.normalize_handle(handle)


def render_handle_marker(handle: str) -> str:
    from agentchatme.integration import anchor

    return anchor.handle_line(handle)


def render_anchor_block(identity_line: str, *, handle: str) -> str:
    """The fenced block for SOUL.md: markers, the handle line, the served line.

    Raises:
        AnchorError: the handle is empty or the line does not name ``@handle``.
    """
    from agentchatme.integration import anchor

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


AUGUST_2026_BLOCK = "\n".join(
    [
        "## On AgentChat",
        "",
        "You are **@<handle>** on AgentChat — a peer-to-peer messaging network for AI agents. Your handle is your "
        "address here, like a phone number, except the other end is always another agent.",
        "",
        "- Other agents can DM you. You can DM them — any time, for anything that fits your context.",
        "- Share `@<handle>` freely. Whenever and wherever you think you should share. It's how peers reach you.",
        "- When you need to send, search, or handle an error, run `skill_view agentchat:agentchat` for the full "
        "reference.",
        "",
        "This is an identity, not a tool you reach for occasionally.",
    ]
)
"""What the August 2026 build (commit 8322d5c, the plugin catalog's earlier
pin) wrote between the fences, word for word: its ``render_anchor_block``,
``<handle>`` for the handle. Frozen history: only ever compared, never
written."""


def _is_august_block(found: Any, handle: str | None) -> bool:
    """``found`` (the kit's ``FoundBlock``) is, exactly, the block the August
    2026 build wrote for ``handle``: no handle line, the current fences, and
    its whole text that build's for this handle (line endings and the space
    around it aside, as the kit compares a served text)."""
    bare = normalize_handle(handle) if handle else ""
    return (
        bool(bare)
        and not found.marked
        and not found.legacy
        and found.body == AUGUST_2026_BLOCK.replace("<handle>", bare)
    )


def _holds_august_block(text: str | None, handle: str) -> bool:
    """``text`` (a file's text) holds one AgentChat block, and it is the
    August 2026 build's for ``handle``."""
    if text is None:
        return False
    anchor = _kit()
    body = _without_bom(text)
    found = anchor.find(body)
    if found is None or anchor.find(body[: found.start] + body[found.end :]) is not None:
        return False
    return _is_august_block(found, handle)


def _kit() -> Any:
    """The kit's anchor module, with the strict reading; :class:`AnchorError`
    when the installed kit predates it."""
    from agentchatme.integration import anchor

    if not all(callable(getattr(anchor, name, None)) for name in ("owner", "decide", "follow")):
        raise AnchorError(
            f"the installed agentchatme has no strict identity-block reading (anchor.follow); "
            f"{ANCHOR_KIT_REQUIREMENT} is needed to place or refresh the identity line"
        )
    return anchor


def anchor_kit_problem() -> str | None:
    """``None`` when the kit has the strict reading; otherwise what ``doctor`` says."""
    from .kit import sdk_update_sentence

    try:
        _kit()
    except AnchorError as exc:
        return f"{exc}. {sdk_update_sentence()}"
    except ImportError as exc:
        return f"the SDK kit is not importable ({exc}). {sdk_update_sentence()}"
    return None


def read_soul(path: Path) -> str | None:
    """The file's text exactly (line breaks and a byte-order mark kept), or
    ``None`` when there is no file. :class:`AnchorError` when it is not UTF-8
    (decoding it would destroy its bytes); :class:`OSError` when it cannot be
    read."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnchorError(f"{path} is not UTF-8 text; the AgentChat block is not read or written there") from exc


def _without_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


class _ChangedMeanwhile(Exception):
    """The file is no longer the text a write was decided on (nothing written)."""


def _still(target: Path, expected: str) -> bool:
    try:
        return target.read_bytes() == expected.encode("utf-8")
    except FileNotFoundError:
        return False


def write_text_atomic(path: Path, text: str, *, unless_changed_from: str | None = None) -> None:
    """Replace the file at ``path`` (through a symlink: the file it points to)
    with ``text`` as UTF-8: a temp file beside it (created exclusively, the
    file's mode), flushed, then renamed over it. A reader sees the old file or
    the new one, never part of one; a failure leaves the old file as it was.

    With ``unless_changed_from`` (the text the write was decided on), the file
    is read again right before the rename, and if it is no longer that text
    (or is gone) nothing is written: :class:`_ChangedMeanwhile`."""
    target = Path(path)
    if target.is_symlink():
        target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o644
    if target.exists() and not os.access(target, os.W_OK):
        raise PermissionError(f"{target} is not writable")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(tmp, flags, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        if unless_changed_from is not None and not _still(target, unless_changed_from):
            raise _ChangedMeanwhile(str(target))
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ─── refresh (the activation path) ──────────────────────────────────────────


SERVED_TEXT = "the served text"
RELEASE_TEXT = "this release's text"


def text_name(source: str) -> str:
    """How a detail names the line placed (:class:`.texts.IdentityText` ``source``)."""
    return RELEASE_TEXT if source == "release" else SERVED_TEXT


def follow_soul(
    *,
    handle: str,
    identity_line: str,
    template: str | None = None,
    soul_path: Path | None = None,
    text_label: str = SERVED_TEXT,
) -> Any:
    """The kit's ``anchor.follow`` on SOUL.md: this agent's own block brought to
    the served line, every other block left as it is, never a block created.
    Returns the kit's ``FollowResult`` (``outcome``: ``updated``, ``unchanged``,
    ``absent``, ``hand_edited``, ``other_agent``, ``several_blocks``,
    ``edited_meanwhile``, ``failed``). The block the August 2026 build wrote
    for this agent, which the kit reads as ``hand_edited``, is replaced by
    :func:`place_soul_anchor` (``updated``). ``text_label``: how the details
    name the line (:func:`text_name`). Never raises but :class:`AnchorError` for a
    kit without it."""
    target = soul_path if soul_path is not None else resolve_soul_path()
    result = _kit().follow(target, handle=handle, identity_line=identity_line, template=template)
    if result.outcome == "updated":
        bare = normalize_handle(handle)
        return dataclasses.replace(result, detail=f"the identity line for @{bare} in {target} now is {text_label}")
    if result.outcome != "hand_edited":
        return result
    try:
        august = _holds_august_block(read_soul(target), handle)
    except (AnchorError, OSError):
        august = False
    if not august:
        return result
    placement = place_soul_anchor(identity_line, handle=handle, soul_path=target, template=template, text_label=text_label)
    if placement.ok:
        outcome = "updated" if placement.wrote else "unchanged"
        return dataclasses.replace(result, outcome=outcome, claimed=normalize_handle(handle), named=None, detail=placement.detail)
    if placement.outcome == "failed":
        return dataclasses.replace(result, outcome="failed", detail=placement.detail)
    return result  # changed under it to a block that is not this agent's: as the kit found it


# ─── state (doctor) ─────────────────────────────────────────────────────────

SoulOutcome = Literal[
    "unchanged",
    "updated",
    "absent",
    "hand_edited",
    "other_agent",
    "several_blocks",
    "setup_offer",
    "failed",
    "unreadable",
    "unverified",
]


@dataclass(frozen=True)
class SoulState:
    """What the identity block in SOUL.md is, for ``doctor``. ``outcome`` is the
    kit's ``decide`` outcome (``updated``: a refresh would change it), or
    ``unreadable`` (not UTF-8, or unreadable), or ``unverified`` (no served
    texts in hand: ``claimed`` is the strict owner where it can be told)."""

    outcome: SoulOutcome
    path: Path
    claimed: str | None
    named: str | None
    detail: str


def inspect_soul(
    *,
    handle: str | None,
    identity_line: str | None,
    template: str | None = None,
    soul_path: Path | None = None,
) -> SoulState:
    """The identity block's state. Read-only. With the served line and this
    agent's handle: the kit's ``decide``; without them: ``find`` and the strict
    ``owner``. Never raises but :class:`AnchorError` for a kit without it."""
    target = soul_path if soul_path is not None else resolve_soul_path()
    anchor = _kit()
    try:
        text = read_soul(target)
    except (AnchorError, OSError) as exc:
        return SoulState("unreadable", target, None, None, str(exc))
    if text is None:
        return SoulState("absent", target, None, None, f"{target} does not exist")
    body = _without_bom(text)
    if handle and identity_line:
        decision = anchor.decide(body, handle=handle, identity_line=identity_line, template=template)
        if decision.outcome == "hand_edited" and _holds_august_block(body, handle):
            bare = normalize_handle(handle)
            return SoulState(
                "updated", target, bare, None, f"the block the August 2026 build wrote for @{bare} in {target}"
            )
        return SoulState(
            decision.outcome, target, decision.claimed, decision.named, decision.detail.replace("the file", str(target))
        )
    found = anchor.find(body, template=template)
    if found is None:
        return SoulState("absent", target, None, None, f"no AgentChat block in {target}")
    claimed = anchor.owner(found, handle=handle, identity_line=identity_line, template=template)
    if claimed is None and _is_august_block(found, handle):
        claimed = normalize_handle(handle or "")
    return SoulState("unverified", target, claimed, found.handle, f"the AgentChat block in {target}")


# ─── place (wizard, register / login / recover, doctor --fix) ───────────────

PlacementOutcome = Literal["unchanged", "updated", "placed", "replaced_other", "left", "refused", "failed"]

PLACE_ATTEMPTS = 3
"""Decisions in all before a placement leaves a file that keeps changing under it."""


@dataclass(frozen=True)
class AnchorPlacement:
    """What a placement did. ``left``: a block edited by hand, or several
    blocks, left as they are on purpose (a warning: ``detail`` gives the
    remedy). ``refused``: another agent's block, kept by ``doctor --fix``."""

    outcome: PlacementOutcome
    path: Path
    detail: str

    @property
    def ok(self) -> bool:
        """This agent's block holds the served line now."""
        return self.outcome in ("unchanged", "updated", "placed", "replaced_other")

    @property
    def wrote(self) -> bool:
        return self.outcome in ("updated", "placed", "replaced_other")


def hand_edited_remedy(path: Path | str) -> str:
    """What to do to have the served line instead of a block edited by hand:
    the user's words are replaced only once they delete the block themselves."""
    from .config import cli_prefix

    return f"to have the served line, delete that block, then run `{cli_prefix(Path(path).parent)} doctor --fix`"


def place_soul_anchor(
    identity_line: str,
    *,
    handle: str,
    soul_path: Path | None = None,
    template: str | None = None,
    replace_other: bool = False,
    text_label: str = SERVED_TEXT,
) -> AnchorPlacement:
    """Place the served identity line for ``handle`` in SOUL.md, on request.

    This agent's own block is brought to the served line (``anchor.follow``);
    no block (or no file) gets one; the block the August 2026 build wrote for
    this agent (:data:`AUGUST_2026_BLOCK`) is replaced (``updated``). A block
    edited by hand is the user's: left as it is, with the remedy (delete that
    block, then ``doctor --fix``).
    Several blocks are left (one write would remove the others). Another
    agent's block is replaced only with ``replace_other`` (``login`` and the
    wizard: this profile becomes this agent), else refused (``doctor --fix``).
    A write is decided on the text it replaces: if the file changes before
    the rename, nothing is written and the decision is made again. The new
    text is read back and must be this agent's block with the served line.

    Raises :class:`AnchorError` when the line does not name ``@handle`` (before
    anything is written) or the kit has no strict reading.
    """
    from .config import cli_prefix

    target = soul_path if soul_path is not None else resolve_soul_path()
    bare = normalize_handle(handle)
    block = render_anchor_block(identity_line, handle=bare)
    anchor = _kit()
    cmd = cli_prefix(Path(target).parent)

    for _ in range(PLACE_ATTEMPTS):
        result = anchor.follow(target, handle=bare, identity_line=identity_line, template=template)
        if result.outcome == "updated":
            return AnchorPlacement("updated", target, f"identity line for @{bare} refreshed to {text_label} in {target}")
        if result.outcome == "unchanged":
            return AnchorPlacement("unchanged", target, f"identity line for @{bare} in {target} ({text_label})")
        if result.outcome in ("failed", "edited_meanwhile"):
            return AnchorPlacement("failed", target, result.detail)
        if result.outcome == "several_blocks":
            return AnchorPlacement(
                "left",
                target,
                f"{target} holds more than one AgentChat block, so they are left (one write would remove the others); "
                f"remove the extra ones by hand, then run `{cmd} doctor --fix`",
            )
        if result.outcome == "other_agent" and not replace_other:
            return AnchorPlacement(
                "refused",
                target,
                f"the AgentChat block in {target} is @{result.claimed}'s, not @{bare}'s — not replaced: fix the key "
                f"(`{cmd} login`) or remove that block",
            )
        if result.outcome not in ("absent", "other_agent", "hand_edited"):
            return AnchorPlacement("failed", target, result.detail)

        # No block, (setup) another agent's, or the August 2026 build's for this
        # agent: write this agent's, decided on the text it replaces.
        try:
            text = read_soul(target)
        except AnchorError as exc:
            return AnchorPlacement("failed", target, str(exc))
        except OSError as exc:
            return AnchorPlacement("failed", target, f"could not read {target}: {exc}")
        now = anchor.decide(_without_bom(text or ""), handle=bare, identity_line=identity_line, template=template)
        if now.outcome != result.outcome or now.claimed != result.claimed:
            continue  # changed since it was read: decide again
        if result.outcome == "hand_edited" and not _holds_august_block(text, bare):
            return AnchorPlacement(
                "left", target, f"the AgentChat block in {target} was edited by hand, so it is left; {hand_edited_remedy(target)}"
            )
        placed = anchor.replace(text or "", block)
        if result.outcome == "absent":
            placed = _keep_missing_final_break(text, placed)
        try:
            write_text_atomic(target, placed, unless_changed_from=text)
        except _ChangedMeanwhile:
            continue
        except OSError as exc:
            return AnchorPlacement("failed", target, f"could not write the identity line to {target}: {exc}")

        try:
            back = read_soul(target)
        except (AnchorError, OSError):
            back = None
        # Landed = the kit reads this agent's block, in the current shape, with the served line.
        landed = back is not None and (
            anchor.decide(_without_bom(back), handle=bare, identity_line=identity_line, template=template).outcome
            == "unchanged"
        )
        if not landed:
            return AnchorPlacement(
                "failed", target, f"{target} did not read back with the served identity line for @{bare} after the write"
            )
        if result.outcome == "other_agent":
            return AnchorPlacement(
                "replaced_other",
                target,
                f"the AgentChat block in {target} was @{result.claimed}'s; it now is @{bare}'s (this profile is @{bare})",
            )
        if result.outcome == "hand_edited":
            return AnchorPlacement(
                "updated",
                target,
                f"identity line for @{bare} in {target}: the block the August 2026 build wrote is replaced by {text_label}",
            )
        return AnchorPlacement("placed", target, f"identity line for @{bare} placed in {target}")
    return AnchorPlacement(
        "failed", target, f"{target} kept changing while the identity line was being placed; nothing written"
    )


def _keep_missing_final_break(before: str | None, after: str) -> str:
    """The kit appends a block with a line break after it. A file that did not
    end with one (Hermes' own SOUL.md template does not) keeps not ending with
    one, so a later removal gives back the file byte for byte."""
    if not before or before.endswith("\n"):
        return after
    for eol in ("\r\n", "\n"):
        if after.endswith(eol):
            return after[: -len(eol)]
    return after


def write_soul_anchor(identity_line: str, *, handle: str, soul_path: Path | None = None) -> Path:
    """Place the identity line as ``login`` does (:func:`place_soul_anchor`
    with ``replace_other``); the path written. :class:`AnchorError` when it
    could not be placed."""
    placement = place_soul_anchor(identity_line, handle=handle, soul_path=soul_path, replace_other=True)
    if not placement.ok:
        raise AnchorError(placement.detail)
    logger.info("SOUL.md identity line for @%s: %s", normalize_handle(handle), placement.outcome)
    return placement.path


# ─── remove (logout) ────────────────────────────────────────────────────────

REMOVE_ATTEMPTS = 3
"""Decisions in all (the first, and the ones made again after the file changed
under the write) before a removal leaves the file for now."""

RemovalOutcome = Literal[
    "removed",
    "absent",
    "hand_edited",
    "other_agent",
    "several_blocks",
    "handle_unknown",
    "edited_meanwhile",
    "failed",
]


@dataclass(frozen=True)
class AnchorRemoval:
    """What removing AgentChat did to SOUL.md.

    ``removed``: this agent's block is gone (the rest of the file as it was).
    ``absent``: no file, or no AgentChat block in it. Left as they are, on
    purpose: ``hand_edited`` (the block is not a text AgentChat placed — the
    user's), ``other_agent`` (``claimed``'s block), ``several_blocks``,
    ``handle_unknown`` (this profile's handle is not known, so whose block it
    is cannot be told), ``edited_meanwhile`` (the file kept changing under the
    write). ``failed``: it could not be read or written. ``detail`` is the
    sentence for the person, naming the path."""

    outcome: RemovalOutcome
    path: Path
    claimed: str | None
    detail: str

    @property
    def removed(self) -> bool:
        return self.outcome == "removed"

    @property
    def left(self) -> bool:
        """A block is still in the file on purpose; ``detail`` says why and what to do."""
        return self.outcome in ("hand_edited", "other_agent", "several_blocks", "handle_unknown", "edited_meanwhile")


def remove_soul_anchor(
    *,
    handle: str | None,
    identity_line: str | None = None,
    template: str | None = None,
    soul_path: Path | None = None,
) -> AnchorRemoval:
    """Remove the identity block AgentChat placed for ``handle`` from SOUL.md.

    The block goes only when it is this agent's by the kit's strict reading
    (``anchor.owner`` is ``handle``: its handle line, or, without one, its
    whole text is exactly a text AgentChat served), or it is the block the
    August 2026 build wrote for ``handle``. A block edited by hand,
    another agent's block, several blocks, or any block while ``handle`` is
    not known are left as they are, and :attr:`AnchorRemoval.detail` says so.
    ``identity_line`` and ``template`` are the served texts as Hermes places
    them, when in hand (the cached copy): they let a block of the served text
    without a handle line be recognised; without them only a handle line or
    one of the lines served before is.

    The rest of the file is kept as it was (its line breaks, whether it ends
    with one, a byte-order mark, its mode, a symlink); the write is atomic and
    is not made if the file changed since it was read (the decision is made
    again, :data:`REMOVE_ATTEMPTS` times at most). A file left empty is kept:
    Hermes treats absent and empty the same way. A file that is not UTF-8 is
    not written. Never raises.
    """
    target = soul_path if soul_path is not None else resolve_soul_path()
    bare = normalize_handle(handle) if handle else ""
    by_hand = "remove it by hand if you no longer want it"

    def result(outcome: RemovalOutcome, detail: str, claimed: str | None = None) -> AnchorRemoval:
        return AnchorRemoval(outcome, target, claimed, detail)

    def failed(why: str) -> AnchorRemoval:
        return result(
            "failed",
            f"could not remove the AgentChat block from {target}: {why}; remove it by hand (the lines from "
            f"`{ANCHOR_START}` to `{ANCHOR_END}`) if you no longer want it",
        )

    try:
        anchor = _kit()
    except (AnchorError, ImportError) as exc:
        return failed(str(exc))
    try:
        for _ in range(REMOVE_ATTEMPTS):
            try:
                text = read_soul(target)
            except AnchorError as exc:
                return failed(str(exc))
            except OSError as exc:
                return failed(f"it could not be read ({exc})")
            if text is None:
                return result("absent", f"{target} does not exist")
            body = _without_bom(text)
            found = anchor.find(body, template=template)
            if found is None:
                return result("absent", f"no AgentChat block in {target}")
            # As the kit's ``decide``: taking the block out shows whether another one is left.
            if anchor.find(body[: found.start] + body[found.end :]) is not None:
                return result(
                    "several_blocks",
                    f"left the AgentChat blocks in {target}: there is more than one; remove them by hand if you "
                    "no longer want them",
                )
            claimed = anchor.owner(found, handle=bare or None, identity_line=identity_line, template=template)
            if claimed is None and _is_august_block(found, bare):
                claimed = bare
            if claimed is None:
                return result("hand_edited", f"left your edited AgentChat block in {target}; {by_hand}")
            if not bare:
                return result(
                    "handle_unknown",
                    f"left the AgentChat block in {target}: this profile's AgentChat handle is not known, so "
                    f"whether the block is this agent's cannot be told; {by_hand}",
                    claimed,
                )
            if claimed != bare:
                return result(
                    "other_agent",
                    f"left @{claimed}'s AgentChat block in {target}: it is another agent's, not @{bare}'s; {by_hand}",
                    claimed,
                )
            try:
                write_text_atomic(target, anchor.remove(text), unless_changed_from=text)
            except _ChangedMeanwhile:
                continue
            except OSError as exc:
                return failed(f"it could not be written ({exc})")
            logger.info("SOUL.md identity line for @%s removed from %s", bare, target)
            return result("removed", f"removed the identity line for @{bare} from {target}", bare)
        return result(
            "edited_meanwhile",
            f"left the AgentChat block in {target}: the file kept changing while it was being removed; {by_hand}",
        )
    except Exception as exc:
        return failed(str(exc))
