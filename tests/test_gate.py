"""Tests for the gate step against a stubbed server and a stubbed model.

What must hold (``docs/decisions/2026-11-main-line.md``):

* the model sees the server's question and cards VERBATIM, plus the JSON
  wrapper and nothing else;
* ``no_reply`` / ``defer`` are posted with the card's newest message id;
  ``reply`` is never posted (the send is the verdict);
* fail closed by default: a failed model call posts nothing and starts no
  turn; fail open starts the turn;
* no local category list: categories are asked for only when the server
  serves them, and a rejected optional field is retried bare.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentchatme_hermes import gate
from agentchatme_hermes.main_line import MainLineClient

QUESTION = "For each conversation on this tray decide ONE thing: reply, no_reply, or defer."


def _card(cid: str, **extra: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "conversation_id": cid,
        "type": "direct",
        "group": None,
        "counterparty": {"handle": "bob", "display_name": None},
        "relationship": "first_contact",
        "you_owe_reply": True,
        "mentioned_you": False,
        "unread": 1,
        "oldest_unread_at": "2026-11-01T10:00:00Z",
        "newest_unread_at": "2026-11-01T10:00:00Z",
        "last_sender": "@bob",
        "last_message_at": "2026-11-01T10:00:00Z",
        "your_last_message_at": None,
        "pace": {"last_60s": 1, "last_10m": 1},
        "preview": {"message_id": f"msg_{cid}", "sender": "@bob", "text": "hi", "at": "x"},
        "your_last_decision": None,
    }
    card.update(extra)
    return card


def _tray(*cards: dict[str, Any], **extra: Any) -> dict[str, Any]:
    tray: dict[str, Any] = {
        "you": "@alice",
        "question": QUESTION,
        "conversations": list(cards),
        "deferred": 0,
        "truncated": False,
        "waited_ms": None,
    }
    tray.update(extra)
    return tray


class _Server:
    def __init__(self, *, fail_statuses: dict[str, list[int]] | None = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail_statuses or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        cid = request.url.path.split("/")[3]
        body = json.loads(request.content)
        self.posts.append((cid, body))
        queue = self._fail.get(cid)
        if queue:
            status = queue.pop(0)
            return httpx.Response(status, json={"code": "VALIDATION_ERROR", "message": "bad"})
        return httpx.Response(201, json={"decision": {"id": "dec"}})

    def client(self) -> MainLineClient:
        return MainLineClient(
            api_key="k", api_base="https://api.example.test", transport=httpx.MockTransport(self.handler)
        )


def _answer(*items: dict[str, Any]) -> str:
    return json.dumps({"decisions": list(items)})


def _caller_returning(text: str) -> Any:
    captured: dict[str, Any] = {}

    def caller(**kwargs: Any) -> str:
        captured.update(kwargs)
        return text

    caller.captured = captured  # type: ignore[attr-defined]
    return caller


# ─── building the call ─────────────────────────────────────────────────────


class TestBuildGateMessages:
    def test_question_and_cards_are_verbatim(self) -> None:
        tray = _tray(_card("conv_1"), _card("conv_2"))
        messages = gate.build_gate_messages(tray)
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"].startswith(QUESTION)
        payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert payload["conversations"] == tray["conversations"]
        assert payload["you"] == "@alice"

    def test_wrapper_asks_for_strict_json_and_nothing_product_like(self) -> None:
        system = gate.build_gate_messages(_tray(_card("conv_1")))[0]["content"]
        wrapper = system[len(QUESTION) :]
        assert "ONLY a JSON object" in wrapper
        assert '"decision": "reply" | "no_reply" | "defer"' in wrapper
        # No local judgement criteria sneak into the wrapper.
        for forbidden in ("acknowledg", "pace", "relationship", "loop", "silence"):
            assert forbidden not in wrapper.lower()

    def test_no_category_asked_unless_the_server_serves_them(self) -> None:
        without = gate.build_gate_messages(_tray(_card("conv_1")))[0]["content"]
        assert "category" not in without
        with_cats = gate.build_gate_messages(
            _tray(_card("conv_1"), categories=["closing", "spam"])
        )[0]["content"]
        assert '"category": "<one of: closing, spam>"' in with_cats

    def test_missing_question_is_a_contract_break(self) -> None:
        with pytest.raises(ValueError):
            gate.build_gate_messages({"conversations": [_card("conv_1")]})


# ─── parsing ───────────────────────────────────────────────────────────────


class TestParseVerdicts:
    def test_parses_fenced_json_and_synonyms(self) -> None:
        text = "Sure:\n```json\n" + _answer(
            {"conversation_id": "a", "decision": "Reply"},
            {"conversation_id": "b", "decision": "no-reply"},
            {"conversation_id": "c", "decision": "later", "defer_seconds": 600},
        ) + "\n```"
        verdicts = gate.parse_verdicts(text, known_ids=["a", "b", "c"])
        assert verdicts is not None
        assert [(v.conversation_id, v.decision) for v in verdicts] == [
            ("a", "reply"),
            ("b", "no_reply"),
            ("c", "defer"),
        ]
        assert verdicts[2].defer_seconds == 600

    def test_unknown_ids_unknown_decisions_and_duplicates_are_dropped(self) -> None:
        text = _answer(
            {"conversation_id": "zzz", "decision": "reply"},
            {"conversation_id": "a", "decision": "maybe"},
            {"conversation_id": "b", "decision": "reply"},
            {"conversation_id": "b", "decision": "no_reply"},
        )
        verdicts = gate.parse_verdicts(text, known_ids=["a", "b"])
        assert verdicts is not None
        assert [(v.conversation_id, v.decision) for v in verdicts] == [("b", "reply")]

    def test_category_kept_only_from_the_served_list(self) -> None:
        text = _answer(
            {"conversation_id": "a", "decision": "no_reply", "category": "Closing"},
            {"conversation_id": "b", "decision": "no_reply", "category": "made_up"},
        )
        verdicts = gate.parse_verdicts(text, known_ids=["a", "b"], categories=["closing"])
        assert verdicts is not None
        assert verdicts[0].category == "closing"
        assert verdicts[1].category is None
        # Without a served list no category survives, whatever the model wrote.
        bare = gate.parse_verdicts(text, known_ids=["a", "b"])
        assert bare is not None and all(v.category is None for v in bare)

    def test_defer_seconds_only_with_defer(self) -> None:
        text = _answer({"conversation_id": "a", "decision": "no_reply", "defer_seconds": 60})
        verdicts = gate.parse_verdicts(text, known_ids=["a"])
        assert verdicts is not None and verdicts[0].defer_seconds is None

    def test_bare_list_and_single_object_shapes(self) -> None:
        as_list = json.dumps([{"conversation_id": "a", "decision": "reply"}])
        single = json.dumps({"conversation_id": "a", "decision": "defer"})
        assert gate.parse_verdicts(as_list, known_ids=["a"]) is not None
        assert gate.parse_verdicts(single, known_ids=["a"]) is not None

    @pytest.mark.parametrize("text", ["", None, "no json here", "{broken", '{"decisions": 3}'])
    def test_unusable_text_is_none(self, text: str | None) -> None:
        assert gate.parse_verdicts(text, known_ids=["a"]) is None

    def test_json_with_no_usable_entries_is_empty(self) -> None:
        # Parsed, but nothing in it decides a card: run_gate treats it as a failure too.
        assert gate.parse_verdicts("[1, 2]", known_ids=["a"]) == []


# ─── the step ──────────────────────────────────────────────────────────────


class TestRunGate:
    def test_posts_no_reply_and_defer_never_reply(self) -> None:
        server = _Server()
        tray = _tray(_card("conv_r"), _card("conv_n"), _card("conv_d"))
        caller = _caller_returning(
            _answer(
                {"conversation_id": "conv_r", "decision": "reply"},
                {"conversation_id": "conv_n", "decision": "no_reply"},
                {"conversation_id": "conv_d", "decision": "defer", "defer_seconds": 900},
            )
        )
        result = gate.run_gate(
            tray,
            main_line=server.client(),
            main_runtime={"model": "m"},
            fail_open=False,
            timeout_s=5.0,
            caller=caller,
        )
        assert result.source == "llm"
        assert result.reply_conversations == ("conv_r",)
        assert result.posted == 2
        assert result.post_failures == 0
        assert result.undecided == ()
        assert result.start_turn is True
        assert result.counts() == {"reply": 1, "no_reply": 1, "defer": 1}
        posted = dict(server.posts)
        assert set(posted) == {"conv_n", "conv_d"}
        assert posted["conv_n"] == {"decision": "no_reply", "through_message_id": "msg_conv_n"}
        assert posted["conv_d"] == {
            "decision": "defer",
            "through_message_id": "msg_conv_d",
            "defer_seconds": 900,
        }

    def test_model_sees_exactly_question_and_cards(self) -> None:
        tray = _tray(_card("conv_1"))
        caller = _caller_returning(_answer({"conversation_id": "conv_1", "decision": "reply"}))
        gate.run_gate(
            tray,
            main_line=_Server().client(),
            main_runtime={"model": "m", "provider": "p"},
            fail_open=False,
            timeout_s=7.0,
            caller=caller,
        )
        captured = caller.captured
        assert captured["timeout"] == 7.0
        assert captured["main_runtime"] == {"model": "m", "provider": "p"}
        assert captured["messages"] == gate.build_gate_messages(tray)

    def test_fail_closed_posts_nothing_and_starts_no_turn(self) -> None:
        server = _Server()

        def broken(**_kwargs: Any) -> str:
            raise TimeoutError("provider down")

        result = gate.run_gate(
            _tray(_card("conv_1"), _card("conv_2")),
            main_line=server.client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=broken,
        )
        assert result.source == "fail_closed"
        assert result.start_turn is False
        assert result.undecided == ("conv_1", "conv_2")
        assert result.failure is not None and "provider down" in result.failure
        assert server.posts == []

    def test_fail_open_starts_the_turn_for_every_card(self) -> None:
        result = gate.run_gate(
            _tray(_card("conv_1"), _card("conv_2")),
            main_line=_Server().client(),
            main_runtime={},
            fail_open=True,
            timeout_s=1.0,
            caller=_caller_returning("garbage"),
        )
        assert result.source == "fail_open"
        assert result.start_turn is True
        assert result.reply_conversations == ("conv_1", "conv_2")

    def test_unusable_answer_is_a_failure(self) -> None:
        server = _Server()
        result = gate.run_gate(
            _tray(_card("conv_1")),
            main_line=server.client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=_caller_returning('{"decisions": []}'),
        )
        assert result.source == "fail_closed"
        assert server.posts == []

    def test_empty_tray_is_not_a_decision(self) -> None:
        def must_not_run(**_kwargs: Any) -> str:
            raise AssertionError("the model must not be asked about an empty tray")

        result = gate.run_gate(
            _tray(truncated=True),
            main_line=_Server().client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=must_not_run,
        )
        assert result.source == "empty"
        assert result.truncated is True
        assert result.start_turn is False

    def test_rejected_optional_field_is_retried_bare(self) -> None:
        server = _Server(fail_statuses={"conv_1": [400]})
        tray = _tray(_card("conv_1"), categories=["closing"])
        caller = _caller_returning(
            _answer({"conversation_id": "conv_1", "decision": "no_reply", "category": "closing"})
        )
        result = gate.run_gate(
            tray, main_line=server.client(), main_runtime={}, fail_open=False, timeout_s=1.0, caller=caller
        )
        assert result.posted == 1 and result.post_failures == 0
        assert [body for _cid, body in server.posts] == [
            {"decision": "no_reply", "category": "closing", "through_message_id": "msg_conv_1"},
            {"decision": "no_reply", "through_message_id": "msg_conv_1"},
        ]

    def test_post_failure_is_counted_not_raised(self) -> None:
        server = _Server(fail_statuses={"conv_1": [500, 500]})
        result = gate.run_gate(
            _tray(_card("conv_1")),
            main_line=server.client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=_caller_returning(_answer({"conversation_id": "conv_1", "decision": "no_reply"})),
        )
        assert result.post_failures == 1
        assert result.posted == 0

    def test_undecided_cards_are_reported(self) -> None:
        result = gate.run_gate(
            _tray(_card("conv_1"), _card("conv_2")),
            main_line=_Server().client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=_caller_returning(_answer({"conversation_id": "conv_1", "decision": "reply"})),
        )
        assert result.undecided == ("conv_2",)
        assert result.reply_conversations == ("conv_1",)

    def test_card_without_preview_posts_without_through_id(self) -> None:
        server = _Server()
        card = _card("conv_1", preview=None)
        gate.run_gate(
            _tray(card),
            main_line=server.client(),
            main_runtime={},
            fail_open=False,
            timeout_s=1.0,
            caller=_caller_returning(_answer({"conversation_id": "conv_1", "decision": "no_reply"})),
        )
        assert server.posts == [("conv_1", {"decision": "no_reply"})]
