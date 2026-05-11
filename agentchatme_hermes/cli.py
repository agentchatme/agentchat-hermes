"""`hermes agentchat [<subcommand>]` argparse wiring.

Two surfaces:

* **Bare ``hermes agentchat``** — launches the interactive wizard
  (state-detection + arrow-key menu). This is the recommended entry point
  for humans, mirrors ``openclaw channels add agentchat``'s UX.
* **``hermes agentchat <subcommand>``** — scriptable shortcuts for CI /
  power users:
  * ``register`` — email-OTP registration, mints a fresh key
  * ``login`` — paste an existing ``ac_live_…`` key
  * ``whoami`` — confirm the saved key still authenticates
  * ``logout`` — clear the key from ``~/.hermes/.env``

The argparse setup is wired by Hermes's plugin context via
``register_cli_command`` (see ``adapter.register``); the dispatch handler
returns an integer exit code that propagates as the process status.
"""

from __future__ import annotations

import argparse
from typing import Any


def setup_cli_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes agentchat`` subcommand tree.

    Hermes calls this once at CLI construction with the parser scoped to
    our subcommand. We attach a sub-subparsers tree mirroring the
    google_meet plugin's pattern (``plugins/google_meet/cli.py:35-100``).
    """
    parser.description = (
        "Manage your AgentChat identity. Run `hermes agentchat` with no "
        "subcommand to launch the interactive wizard (state-detection + "
        "arrow-key menu). The named subcommands are scriptable shortcuts "
        "for CI and power users. Configuration is persisted to ~/.hermes/.env."
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>")

    # register
    p_register = sub.add_parser(
        "register",
        help="Register a new AgentChat agent (email + OTP)",
        description=(
            "Mint a fresh AgentChat agent. Prompts for email, picks a "
            "@handle, sends a 6-digit code, and persists the minted key "
            "to ~/.hermes/.env. Same flow as the gateway-setup wizard, "
            "scriptable from the terminal."
        ),
    )
    p_register.add_argument(
        "--email",
        help="Email address for verification (will prompt if omitted).",
    )
    p_register.add_argument(
        "--handle",
        help="Desired @handle (3-30 chars, lowercase letters/digits/hyphens; will prompt if omitted).",
    )
    p_register.add_argument(
        "--display-name",
        dest="display_name",
        default=None,
        help="Display name shown next to your @handle.",
    )

    # login
    p_login = sub.add_parser(
        "login",
        help="Paste an existing AgentChat API key",
        description=(
            "Paste an existing ac_live_… key. Validates against "
            "GET /v1/agents/me before persisting so you can't save a key "
            "that won't authenticate."
        ),
    )
    p_login.add_argument(
        "--api-key",
        dest="api_key",
        help="API key (ac_live_…). Will prompt with input masked if omitted.",
    )

    # whoami
    sub.add_parser(
        "whoami",
        help="Show the @handle of the currently configured key",
        description=(
            "Calls GET /v1/agents/me with the saved key and prints the "
            "resolved @handle. Use this to confirm you haven't accidentally "
            "rotated the key out from under the running gateway."
        ),
    )

    # logout
    sub.add_parser(
        "logout",
        help="Clear the saved AgentChat key from ~/.hermes/.env",
        description=(
            "Wipes AGENTCHATME_API_KEY and AGENTCHATME_HANDLE from "
            "~/.hermes/.env. The agent on the AgentChat server is "
            "unaffected — only this Hermes profile loses access."
        ),
    )


def dispatch_cli_command(args: Any) -> int:
    """Route the parsed argparse Namespace to the right backend.

    The handler returns the desired process exit code (0 success, 1
    server-side failure, 2 user input failure). Hermes's main CLI
    surfaces this as the shell return code.

    **Bare ``hermes agentchat``** (no subcommand) launches the interactive
    wizard — mirrors ``openclaw channels add agentchat``. The wizard
    handles state detection internally, so this is the only command a
    new user needs to know.
    """
    action = getattr(args, "action", None)

    # Lazy-import the backends so the argparse wiring stays fast.
    if action == "register":
        from .setup import cli_register

        return cli_register(
            email=getattr(args, "email", None),
            handle=getattr(args, "handle", None),
            display_name=getattr(args, "display_name", None),
        )

    if action == "login":
        from .setup import cli_login

        return cli_login(api_key=getattr(args, "api_key", None))

    if action == "whoami":
        from .setup import cli_whoami

        return cli_whoami()

    if action == "logout":
        from .setup import cli_logout

        return cli_logout()

    # No subcommand → launch the interactive wizard. This is the canonical
    # human entry point — `interactive_setup` handles state detection
    # (configured vs fresh) and routes to the right sub-flow.
    from .setup import interactive_setup

    interactive_setup()
    return 0
