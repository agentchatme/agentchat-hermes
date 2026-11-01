"""Tests for ``agentchatme_hermes.turn`` — the fixed prompts and Hermes tool names."""
from __future__ import annotations

import re

from agentchatme_hermes.turn import (
    INBOX_TOOL,
    KNOCK_TURN_PROMPT,
    MCP_SERVER_NAME,
    SKILL_REF,
    invite_session_id,
    invite_turn_prompt,
    knock_session_id,
    knock_turn_prompt,
    mcp_tool_name,
)


class TestToolNames:
    def test_hermes_double_underscore_convention(self) -> None:
        # tools/mcp_tool_schema.py (Hermes v0.22.5): mcp__<server>__<tool>.
        assert mcp_tool_name("agentchat_inbox") == "mcp__agentchat__agentchat_inbox"
        assert INBOX_TOOL == "mcp__agentchat__agentchat_inbox"

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


class TestKnockPrompt:
    def test_is_the_fixed_sentence(self) -> None:
        prompt = knock_turn_prompt()
        assert prompt is KNOCK_TURN_PROMPT
        assert prompt.startswith(
            "You were woken by AgentChat. Call mcp__agentchat__agentchat_inbox first, then read "
            "the conversations you decided to answer with mcp__agentchat__agentchat_get_conversation "
            "and reply with mcp__agentchat__agentchat_send_message; silence is valid."
        )

    def test_points_at_the_bundled_manual(self) -> None:
        assert f"skill_view {SKILL_REF}" in knock_turn_prompt()

    def test_carries_no_message_content_or_card_fields(self) -> None:
        # The prompt is fixed: no sender, no preview, no conversation id.
        prompt = knock_turn_prompt()
        assert "conv_" not in prompt
        assert "preview" not in prompt
        assert "@" not in prompt.replace("agentchat_", "")


class TestInvitePrompt:
    def test_names_the_three_invite_tools_and_the_right_to_ignore(self) -> None:
        prompt = invite_turn_prompt()
        assert "mcp__agentchat__agentchat_list_group_invites" in prompt
        assert "mcp__agentchat__agentchat_accept_group_invite" in prompt
        assert "mcp__agentchat__agentchat_reject_group_invite" in prompt
        assert "not a reason to join" in prompt


class TestSessionIds:
    def test_knock_session_id_is_fresh_and_namespaced(self) -> None:
        a = knock_session_id(now=0.0)
        b = knock_session_id(now=0.0)
        assert a != b
        assert re.fullmatch(r"agentchat:knock:19700101T000000Z-[0-9a-f]{6}", a)

    def test_invite_session_id_is_per_invite(self) -> None:
        assert invite_session_id("ginv_1") == "agentchat:invite:ginv_1"
