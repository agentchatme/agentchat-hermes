"""Tests for ``agentchatme_hermes.main_line`` against a stubbed server.

The two calls the doorbell makes onto the main line: the tray and the
verdict. Nothing is interpreted, so the tests pin the wire — paths, query,
headers, body — and the error mapping.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentchatme_hermes.main_line import (
    INBOX_MAX_LIMIT,
    INBOX_WAIT_MAX_MS,
    MainLineClient,
    MainLineError,
)

_TRAY = {
    "you": "@alice",
    "question": "Decide ONE thing per card.",
    "conversations": [{"conversation_id": "conv_1"}],
    "deferred": 0,
    "truncated": False,
    "waited_ms": None,
}


class _Server:
    """Records requests; answers from a queue of (status, body) or a callable."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[tuple[int, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0) if self.responses else (200, _TRAY)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    def client(self) -> MainLineClient:
        return MainLineClient(
            api_key="ac_live_k",
            api_base="https://api.example.test/",
            transport=httpx.MockTransport(self.handler),
        )


class TestInbox:
    def test_get_inbox_wire(self) -> None:
        server = _Server()
        client = server.client()
        tray = client.inbox(wait_ms=0, limit=20)
        assert tray == _TRAY
        req = server.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/v1/inbox"
        assert dict(req.url.params) == {"wait_ms": "0", "limit": "20"}
        assert req.headers["authorization"] == "Bearer ac_live_k"
        assert req.headers["x-agentchat-client"] == "hermes"

    def test_defaults_ask_for_the_largest_tray_without_parking(self) -> None:
        server = _Server()
        server.client().inbox()
        assert dict(server.requests[0].url.params) == {
            "wait_ms": "0",
            "limit": str(INBOX_MAX_LIMIT),
        }

    def test_tray_is_passed_through_verbatim_including_unknown_fields(self) -> None:
        server = _Server()
        server.responses.append((200, {**_TRAY, "categories": ["x"], "new_field": 1}))
        tray = server.client().inbox()
        assert tray["categories"] == ["x"]
        assert tray["new_field"] == 1

    @pytest.mark.parametrize("wait_ms", [-1, INBOX_WAIT_MAX_MS + 1])
    def test_rejects_out_of_range_wait(self, wait_ms: int) -> None:
        with pytest.raises(ValueError):
            _Server().client().inbox(wait_ms=wait_ms)

    @pytest.mark.parametrize("limit", [0, INBOX_MAX_LIMIT + 1])
    def test_rejects_out_of_range_limit(self, limit: int) -> None:
        with pytest.raises(ValueError):
            _Server().client().inbox(limit=limit)

    def test_non_2xx_maps_to_error_with_code(self) -> None:
        server = _Server()
        server.responses.append((429, {"code": "RATE_LIMITED", "message": "slow down"}))
        with pytest.raises(MainLineError) as info:
            server.client().inbox()
        assert info.value.status == 429
        assert info.value.code == "RATE_LIMITED"
        assert "slow down" in str(info.value)

    def test_non_object_body_is_an_error(self) -> None:
        server = _Server()
        server.responses.append((200, [1, 2]))
        with pytest.raises(MainLineError, match="expected a JSON object"):
            server.client().inbox()

    def test_transport_failure_is_an_error(self) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = MainLineClient(
            api_key="k", api_base="https://api.example.test", transport=httpx.MockTransport(boom)
        )
        with pytest.raises(MainLineError, match="refused") as info:
            client.inbox()
        assert info.value.status is None


class TestRecordDecision:
    def test_post_wire_full_body(self) -> None:
        server = _Server()
        server.responses.append((201, {"decision": {"id": "dec_1"}}))
        result = server.client().record_decision(
            "conv_1",
            decision="defer",
            category="goal_followup",
            through_message_id="msg_9",
            defer_seconds=900,
        )
        assert result == {"decision": {"id": "dec_1"}}
        req = server.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/v1/conversations/conv_1/decisions"
        assert json.loads(req.content) == {
            "decision": "defer",
            "category": "goal_followup",
            "through_message_id": "msg_9",
            "defer_seconds": 900,
        }

    def test_optional_fields_are_omitted_not_nulled(self) -> None:
        server = _Server()
        server.responses.append((201, {}))
        server.client().record_decision("conv_1", decision="no_reply")
        assert json.loads(server.requests[0].content) == {"decision": "no_reply"}

    def test_validation_error_is_surfaced_with_status(self) -> None:
        server = _Server()
        server.responses.append(
            (400, {"code": "VALIDATION_ERROR", "message": "Invalid request"})
        )
        with pytest.raises(MainLineError) as info:
            server.client().record_decision("conv_1", decision="no_reply", category="bogus")
        assert info.value.status == 400
        assert info.value.code == "VALIDATION_ERROR"

    def test_empty_conversation_id_rejected_locally(self) -> None:
        with pytest.raises(ValueError):
            _Server().client().record_decision("", decision="no_reply")

    def test_empty_2xx_body_is_fine(self) -> None:
        server = _Server()
        server.responses.append((201, b""))
        assert server.client().record_decision("conv_1", decision="no_reply") == {}
