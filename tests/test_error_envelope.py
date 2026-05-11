"""Tests for the dual-shape tool error envelope.

Hermes docs (`developer-guide/adding-tools`) state:
> "Errors MUST be returned as `{"error": "message"}`, never raised as
> exceptions."

Our envelope is structurally richer — `{"ok": false, "code": "...",
"message": "...", "error": "..."}` — so the agent skill teaches the LLM
to read `ok`/`code`/`message` for structured handling AND any
doc-conformant tooling (`transform_tool_result` hooks, ops inspectors,
third-party Hermes plugins) can match on `error` to detect failures.
"""

from __future__ import annotations


def test_err_envelope_carries_error_key():
    from agentchatme_hermes.tools import _err

    exc = ValueError("something broke")
    body = _err("BLOCKED", exc)
    assert body["ok"] is False
    assert body["code"] == "BLOCKED"
    assert body["message"] == "something broke"
    # Doc-conformant alias.
    assert body["error"] == "something broke"


def test_err_envelope_with_request_id_and_extras():
    from agentchatme_hermes.tools import _err

    class _Exc(Exception):
        request_id = "req_abc123"

    body = _err("AWAITING_REPLY", _Exc("peer hasn't replied"), recipient_handle="@alice")
    assert body["ok"] is False
    assert body["code"] == "AWAITING_REPLY"
    assert body["error"] == "peer hasn't replied"
    assert body["message"] == "peer hasn't replied"
    assert body["request_id"] == "req_abc123"
    assert body["recipient_handle"] == "@alice"


def test_err_envelope_drops_none_extras():
    """Extras with value None must not appear in the body."""
    from agentchatme_hermes.tools import _err

    body = _err("X", ValueError("boom"), foo=None, bar="present")
    assert "foo" not in body
    assert body["bar"] == "present"


def test_serialized_envelope_is_valid_json_with_error_key():
    """End-to-end: serialize an error envelope and confirm both shapes
    survive the JSON roundtrip."""
    import json

    from agentchatme_hermes.tools import _err, _serialize

    raw = _serialize(_err("RATE_LIMITED", ValueError("slow down"), retry_after_ms=5000))
    payload = json.loads(raw)
    # Structured shape (our agent skill reads this).
    assert payload["ok"] is False
    assert payload["code"] == "RATE_LIMITED"
    assert payload["message"] == "slow down"
    assert payload["retry_after_ms"] == 5000
    # Doc-conformant shape (third-party tooling reads this).
    assert payload["error"] == "slow down"
