"""Tests for ``agentchatme_hermes.turn`` — the served texts, used as served for Hermes."""
from __future__ import annotations

import re

from agentchatme import gate
from agentchatme.types import Texts

from agentchatme_hermes.turn import (
    INBOX_TOOL,
    MANUAL_POINTER,
    MCP_SERVER_NAME,
    SKILL_REF,
    identity_line,
    invite_session_id,
    mcp_tool_name,
    wake_session_id,
    with_manual,
)

# Shapes like the server's, as ``?runtime=hermes`` serves them: hosted tool
# names already spelled the way Hermes shows them. Test data, not product text.
TEXTS = Texts.model_validate(
    {
        "version": "1",
        "runtime": "hermes",
        "manual": {"url": "https://agentchat.me/skill.md", "version": "2.1.2"},
        "wake_instruction": (
            "You were woken by AgentChat. Call mcp__agentchat__agentchat_inbox first, then record "
            "mcp__agentchat__agentchat_decide or read with mcp__agentchat__agentchat_get_conversation and reply with "
            "mcp__agentchat__agentchat_send_message."
        ),
        "invite_prompt": (
            "A group invitation is pending. Call mcp__agentchat__agentchat_list_group_invites first; "
            "accept with mcp__agentchat__agentchat_accept_group_invite or decline with "
            "mcp__agentchat__agentchat_reject_group_invite."
        ),
        "identity_line": (
            "I am @alice on AgentChat (api.agentchat.me). On any AgentChat wake I call\n"
            "mcp__agentchat__agentchat_inbox first and decide per card."
        ),
        "identity_line_template": "I am @<handle> on AgentChat (api.agentchat.me).",
    }
)


class TestToolNames:
    def test_hermes_double_underscore_convention(self) -> None:
        # tools/mcp_tool_schema.py (Hermes v0.22.5): mcp__<server>__<tool>.
        assert mcp_tool_name("agentchat_inbox") == "mcp__agentchat__agentchat_inbox"
        assert INBOX_TOOL == "mcp__agentchat__agentchat_inbox"

    def test_matches_the_sdk_namer(self) -> None:
        namer = gate.mcp_tool_namer(MCP_SERVER_NAME)
        for tool in ("agentchat_inbox", "agentchat_decide", "agentchat_send_file"):
            assert mcp_tool_name(tool) == namer(tool)

    def test_server_name_is_the_config_yaml_entry(self) -> None:
        assert MCP_SERVER_NAME == "agentchat"

    def test_names_stay_under_the_64_char_provider_cap(self) -> None:
        for tool in (
            "agentchat_inbox",
            "agentchat_get_conversation",
            "agentchat_send_message",
            "agentchat_list_group_invites",
            "agentchat_accept_group_invite",
            "agentchat_reject_group_invite",
        ):
            assert len(mcp_tool_name(tool)) <= 64


class TestAsServed:
    def test_the_identity_line_is_used_verbatim(self) -> None:
        """No re-spelling, no text of our own inside the served words."""
        assert identity_line(TEXTS) == TEXTS.identity_line.strip()


class TestTurnPrompt:
    """The kit builds a turn's prompt from the served texts (the wake
    instruction and the conversation id, or the invite prompt and the
    invite id); this runtime adds only where its manual is."""

    def test_adds_only_the_manual_pointer(self) -> None:
        prompt = f"{TEXTS.wake_instruction}\n\nconversation_id: conv_a"
        assert with_manual(prompt) == f"{prompt}\n\n{MANUAL_POINTER}"
        assert f"skill_view {SKILL_REF}" in with_manual(prompt)

    def test_strips_the_kits_prompt_once(self) -> None:
        assert with_manual("  hi \n") == f"hi\n\n{MANUAL_POINTER}"


class TestIdentityLine:
    def test_is_the_served_line_spelled_for_hermes(self) -> None:
        line = identity_line(TEXTS)
        assert line.startswith("I am @alice on AgentChat")
        assert "mcp__agentchat__agentchat_inbox first" in line
        assert "\nagentchat_inbox" not in line


class TestSessionIds:
    def test_wake_session_id_is_fresh_and_namespaced(self) -> None:
        a = wake_session_id(now=0.0)
        b = wake_session_id(now=0.0)
        assert a != b
        assert re.fullmatch(r"agentchat:wake:19700101T000000Z-[0-9a-f]{6}", a)

    def test_invite_session_id_is_per_invite(self) -> None:
        assert invite_session_id("ginv_1") == "agentchat:invite:ginv_1"
