"""Sign-up one step per command, for an agent's terminal.

``hermes agentchat register`` at a person's terminal is one interactive run:
it sends the code and waits at a prompt for it. An agent running it in its
own terminal cannot answer a prompt reliably (lab R3, E2: an empty write to
the waiting process cancelled it, after the code was sent), and wherever
stdin is not a terminal nobody can. There, sign-up runs one step per command,
the shape the coding-agent integrations use:

1. ``register --email E --handle H`` (``recover --email E --handle H``) makes
   the start call. When the server answers that a code was sent, its flow
   object is kept in this profile's ``agentchat/signup.json`` and the command
   ends.
2. ``register --code C`` (``recover --code C``) submits the code with the
   kept flow. On ``done`` the key is saved and placed exactly as the
   interactive run does, and the file is removed.

The rules stay the server's (contract v1, "Sign-up flows"): every sentence
about a code, a handle or an email is the server's ``message``, and what
comes next is the SDK's ``action_of`` (``agentchatme.integration.account``,
whose step functions are made for this: "save the returned flow between
start and submit_code"). What is Hermes's, and lives here: when a command
runs one step, where the flow waits between the two commands, and the line
naming the command to run next.

The file holds the flow (its ``pending_id`` answers the emailed code once),
the email and handle the sign-up was started with, and the API base. Owner
only (``0600`` in the ``0700`` key directory), written atomically. It is not
the key and never holds it.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import allow
from .client_identity import hermes_client_identity
from .key_store import store_dir
from .state import read_json, write_json_atomic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentchatme.integration.account import StepResult

    from .signup import Kind

logger = logging.getLogger(__name__)

PENDING_FILENAME = "signup.json"
PENDING_VERSION = 1
_KINDS = ("register", "recover")


def one_step_per_run() -> bool:
    """Does sign-up run one step per command here? In an agent's terminal
    (Hermes stamps ``HERMES_SESSION_ID`` on every command its terminal tool
    runs, a PTY included) and wherever stdin is not a terminal. A person at
    their own terminal gets the interactive run."""
    return allow.in_agent_terminal() or not allow.stdin_is_tty()


@dataclass(frozen=True)
class Pending:
    """A sign-up waiting for its emailed code in one profile."""

    kind: Kind
    flow: dict[str, Any]
    """The server's flow object from the last answer (plain JSON)."""
    api_base: str
    email: str | None = None
    handle: str | None = None
    """The handle the sign-up was started with (not yet the server's)."""
    display_name: str | None = None


def pending_path(home: Path) -> Path:
    """``<profile home>/agentchat/signup.json``, beside the key file."""
    return store_dir(home) / PENDING_FILENAME


def read_pending(home: Path) -> Pending | None:
    """The sign-up waiting in ``home``, or ``None`` (none, or a file that is
    not one: it is removed, since nothing can finish from it)."""
    path = pending_path(home)
    record = read_json(path)
    if record is None:
        return None
    pending = _parse(record)
    if pending is None:
        logger.warning("agentchat: %s is not a sign-up this release can finish; removed", path)
        clear_pending(home)
    return pending


def _parse(record: Any) -> Pending | None:
    if not isinstance(record, dict) or record.get("version") != PENDING_VERSION:
        return None
    kind, flow, api_base = record.get("kind"), record.get("flow"), record.get("api_base")
    if kind not in _KINDS or not isinstance(api_base, str) or not api_base:
        return None
    if not isinstance(flow, dict) or not isinstance(flow.get("next_step"), str):
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
    write_json_atomic(pending_path(home), record)


def clear_pending(home: Path) -> bool:
    """Remove the waiting sign-up; ``True`` when there was one."""
    path = pending_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("agentchat: %s could not be removed", path, exc_info=True)
        return False
    with suppress(OSError):
        path.parent.rmdir()  # only when empty
    return True


def start(kind: Kind, given: Mapping[str, Any], *, api_base: str) -> StepResult:
    """One start call (sends a code). Raises ``agentchatme.ConnectionError``
    when no answer came."""
    from agentchatme.integration import account

    return account.start(
        kind,
        cast("Any", dict(given)),
        client_identity=hermes_client_identity(),
        api_base=api_base,
    )


def submit(pending: Pending, code: str) -> StepResult:
    """Submit ``code`` with the kept flow. Raises ``agentchatme.ConnectionError``
    when no answer came."""
    from agentchatme.integration import account

    return account.submit_code(
        cast("Any", pending.flow),
        code.strip(),
        client_identity=hermes_client_identity(),
        api_base=pending.api_base,
    )


def waiting(pending: Pending, result: StepResult) -> Pending:
    """``pending`` carrying the flow of the answer that keeps it waiting (a
    wrong code counts a try; a wait names its seconds)."""
    return Pending(
        kind=pending.kind,
        flow=dict(result.flow),
        api_base=pending.api_base,
        email=pending.email,
        handle=pending.handle,
        display_name=pending.display_name,
    )


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
