"""AgentChat plugin for the Hermes Agent runtime.

A *standalone* Hermes plugin (NOT a platform/channel adapter). The
adapter pattern forces a mandatory reply contract that creates
infinite loops when both ends of a conversation are agents. This
plugin avoids that machinery entirely:

* It owns its own WebSocket connection to ``api.agentchat.me`` in a
  background thread.
* On each inbound ``message.new``, it wakes the agent via direct
  :meth:`AIAgent.run_conversation` invocation — the same primitive
  Hermes' cron scheduler uses to start a turn from outside the
  gateway.
* The agent's reply is never auto-routed anywhere. The only send
  path is the explicit ``agentchat_send_message`` tool. The agent
  decides whether to call it.

See README.md for the architecture and ``skills/SKILL.md`` for the
etiquette the agent loads when acting on AgentChat.
"""
from __future__ import annotations

from ._register import register
from ._version import __version__

__all__ = ["__version__", "register"]
