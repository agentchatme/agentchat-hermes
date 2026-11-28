"""Sign-up one step per command, for an agent's terminal.

``hermes agentchat register`` at a person's terminal is one interactive run:
it sends the code and waits at a prompt for it. An agent running it in its
own terminal cannot answer a prompt reliably (lab R3, E2: an empty write to
the waiting process cancelled it, after the code was sent), and wherever
stdin is not a terminal nobody can. There, sign-up runs one step per command,
the shape the coding-agent integrations use:

1. ``register --email E --handle H`` (``recover --email E --handle H``) makes
   the start call (:func:`.signup.start_step`). When the server answers that a
   code was sent, its flow object is kept in this profile's
   ``agentchat/signup.json`` and the command ends.
2. ``register --code C`` (``recover --code C``) submits the code with the
   kept flow (:func:`.signup.submit_step`). On ``done`` the key is saved and
   placed exactly as the interactive run does, and the file is removed.

The rules stay the server's (contract v1, "Sign-up flows"): every sentence
about a code, a handle or an email is the server's ``message``, and what
comes next is the SDK's ``action_of`` (``agentchatme.integration.account``,
whose step functions are made for this: "save the returned flow between
start and submit_code"). A sign-up waits only while the server's last answer
names the verify step: any other answer ends the wait. What is Hermes's, and
lives here: when a command runs one step, where the flow waits between the
two commands, and the line naming the command to run next.

The file holds the flow (its ``pending_id`` answers the emailed code once),
the email and handle the sign-up was started with, the API base, and the
profile home it was saved for: a copy in another profile (``--clone-all``, a
hand copy) is not that profile's sign-up (:func:`.key_store.same_home`).
Owner only (``0600`` in the ``0700`` key directory), written atomically, read
without side effects. It is not the key and never holds it. Any key saved in
the profile ends the wait (:func:`drop_pending`, from
:func:`.credentials.save_credentials`).
"""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import allow
from .key_store import same_home, store_dir
from .state import read_json, remove_file, write_json_atomic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .signup import Kind

logger = logging.getLogger(__name__)

PENDING_FILENAME = "signup.json"
LOCK_FILENAME = "signup.lock"
PENDING_VERSION = 1
# The step that submits the code, per flow: the only step a sign-up waits on.
VERIFY_STEP: dict[str, str] = {"register": "verify", "recover": "recover_verify"}


def one_step_per_run() -> bool:
    """Does sign-up run one step per command here? In a command Hermes'
    terminal ran (Hermes stamps ``HERMES_SESSION_ID`` on what its terminal tool
    runs, a PTY included, and on shells it starts) and wherever stdin is not a
    terminal. A person at their own terminal gets the interactive run."""
    return allow.in_agent_terminal() or not allow.stdin_is_tty()


@dataclass(frozen=True)
class Pending:
    """A sign-up waiting for its emailed code in one profile."""

    kind: Kind
    flow: dict[str, Any]
    """The server's flow object from the last answer (plain JSON), naming the verify step."""
    api_base: str
    email: str | None = None
    handle: str | None = None
    """The handle the sign-up was started with (not yet the server's)."""
    display_name: str | None = None


def waits(kind: str, flow: Mapping[str, Any]) -> bool:
    """Does ``flow`` leave a sign-up of ``kind`` waiting for its code? Only
    when the server names the verify step and the pending code."""
    pending_id = flow.get("pending_id")
    return flow.get("next_step") == VERIFY_STEP.get(kind) and isinstance(pending_id, str) and bool(pending_id)


def pending_path(home: Path) -> Path:
    """``<profile home>/agentchat/signup.json``, beside the key file."""
    return store_dir(home) / PENDING_FILENAME


def lock_path(home: Path) -> Path:
    """The lock that keeps two ``--code`` commands of one profile apart."""
    return store_dir(home) / LOCK_FILENAME


def read_pending(home: Path) -> Pending | None:
    """The sign-up waiting in ``home``, or ``None``: none, one saved for another
    profile, or a file this release cannot finish from (left as it is: a later
    start replaces it). No side effects."""
    return _parse(read_json(pending_path(home)), home)


def _parse(record: Any, home: Path) -> Pending | None:
    if not isinstance(record, dict) or record.get("version") != PENDING_VERSION:
        return None
    kind, flow, api_base = record.get("kind"), record.get("flow"), record.get("api_base")
    if kind not in VERIFY_STEP or not isinstance(api_base, str) or not api_base:
        return None
    if not isinstance(flow, dict) or not waits(kind, flow):
        return None
    saved_for = record.get("profile_home")
    if not isinstance(saved_for, str) or not same_home(Path(saved_for), Path(home)):
        return None

    def text(name: str) -> str | None:
        value = record.get(name)
        return value if isinstance(value, str) and value else None

    return Pending(
        kind=cast("Kind", kind),
        flow=flow,
        api_base=api_base,
        email=text("email"),
        handle=text("handle"),
        display_name=text("display_name"),
    )


def save_pending(home: Path, pending: Pending) -> None:
    """Keep ``pending`` for the next command. Raises ``OSError`` when the file
    cannot be written (the caller says so: the code was sent, nothing waits)."""
    record: dict[str, Any] = {
        "version": PENDING_VERSION,
        "kind": pending.kind,
        "flow": pending.flow,
        "api_base": pending.api_base,
        "profile_home": str(Path(home).expanduser()),
    }
    for name in ("email", "handle", "display_name"):
        value = getattr(pending, name)
        if value:
            record[name] = value
    # The flow answers the emailed code: its folder is made private (and one
    # that cannot be is refused) before it is written.
    write_json_atomic(pending_path(home), record, root=Path(home), private_dir=True)


def drop_pending(home: Path) -> bool:
    """End the waiting sign-up (its file removed); ``True`` when there was a file."""
    path = pending_path(home)
    try:
        remove_file(path)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("agentchat: %s could not be removed", path, exc_info=True)
        return False
    with suppress(OSError):
        path.parent.rmdir()  # only when empty
    return True


# ─── the lines Hermes adds (the server's message is shown before them) ─────


def finish_line(kind: Kind, cmd: str) -> str:
    """After a code was sent: the command that finishes, and where the code is."""
    return (
        f"Then finish with: {cmd} {kind} --code <code> (the code from that email: read it if you "
        "can, or ask your person for it)."
    )


def start_line(kind: Kind, cmd: str, *, email: str | None = None, handle: str | None = None) -> str:
    """The command that starts ``kind`` (sends a code), with what is known."""
    return f"{cmd} {kind} --email {email or '<email>'} --handle {handle or '<handle>'}"


def no_pending_line(kind: Kind, cmd: str) -> str:
    what = "account recovery" if kind == "recover" else "sign-up"
    return f"No {what} is waiting for a code in this profile. Start with: {start_line(kind, cmd)}"


def other_kind_line(pending: Pending, cmd: str) -> str:
    what = "an account recovery" if pending.kind == "recover" else "a new sign-up"
    return f"The code waiting in this profile is for {what}: finish with {cmd} {pending.kind} --code <code>"


def waiting_line(pending: Pending, cmd: str) -> str:
    """doctor's and status' line while a sign-up waits for its code."""
    who = f" for @{pending.handle}" if pending.handle else ""
    what = "An account recovery" if pending.kind == "recover" else "A sign-up"
    return f"{what}{who} is waiting for its emailed code: finish with {cmd} {pending.kind} --code <code>"
