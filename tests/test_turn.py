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
    invite_turn_prompt,
    mcp_tool_name,
    wake_session_id,
    wake_turn_prompt,
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
    def test_texts_are_used_verbatim(self) -> None:
        """No re-spelling, no text of our own inside the served words."""
        assert wake_turn_prompt(TEXTS).startswith(TEXTS.wake_instruction.strip())
        assert invite_turn_prompt(TEXTS).startswith(TEXTS.invite_prompt.strip())
        assert identity_line(TEXTS) == TEXTS.identity_line.strip()


class TestWakePrompt:
    def test_is_the_served_instruction_with_hermes_tool_names(self) -> None:
        prompt = wake_turn_prompt(TEXTS)
        assert prompt.startswith("You were woken by AgentChat.")
        for tool in (
            "mcp__agentchat__agentchat_inbox",
            "mcp__agentchat__agentchat_decide",
            "mcp__agentchat__agentchat_get_conversation",
            "mcp__agentchat__agentchat_send_message",
        ):
            assert tool in prompt
        assert " agentchat_inbox" not in prompt, "bare hosted name — Hermes would not find it"

    def test_states_the_gates_reply_ids_and_nothing_else(self) -> None:
        prompt = wake_turn_prompt(TEXTS, ("conv_a", "conv_b"))
        assert "decided to answer: conv_a, conv_b." in prompt
        assert prompt.startswith(TEXTS.wake_instruction.strip())
        assert prompt.endswith(MANUAL_POINTER)
        assert "preview" not in prompt
        assert wake_turn_prompt(TEXTS, ()) == wake_turn_prompt(TEXTS)
        assert wake_turn_prompt(TEXTS, ("", "conv_c")) == wake_turn_prompt(TEXTS, ("conv_c",))

    def test_points_at_the_bundled_manual(self) -> None:
        assert f"skill_view {SKILL_REF}" in wake_turn_prompt(TEXTS)

    def test_no_text_of_our_own_beyond_the_two_facts(self) -> None:
        prompt = wake_turn_prompt(TEXTS, ("conv_a",))
        ours = prompt.replace(TEXTS.wake_instruction.strip(), "", 1)
        ours = ours.replace(MANUAL_POINTER, "", 1)
        assert ours.strip() == (
            "The gate step before this turn decided to answer: conv_a. Any other card on the "
            "tray was left undecided; decide it with the tray's question."
        )


class TestInvitePrompt:
    def test_is_the_served_invite_prompt_with_hermes_tool_names(self) -> None:
        prompt = invite_turn_prompt(TEXTS)
        assert prompt.startswith("A group invitation is pending.")
        assert "mcp__agentchat__agentchat_list_group_invites" in prompt
        assert "mcp__agentchat__agentchat_accept_group_invite" in prompt
        assert "mcp__agentchat__agentchat_reject_group_invite" in prompt
        assert prompt.endswith(MANUAL_POINTER)


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
