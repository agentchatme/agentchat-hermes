"""Regression tests for the REST→WS base URL conversion.

The agentchatme SDK's `RealtimeClient` does NOT auto-rewrite the
scheme — it concatenates `/v1/ws` to whatever `base_url` we pass.
Passing `https://api.agentchat.me` made the websockets library
reject the URI with `scheme isn't ws or wss`, silently breaking
inbound message delivery on every version of this plugin since
v0.1.0. This locks down the fix.
"""

from __future__ import annotations

from agentchatme_hermes.adapter import _rest_base_to_ws_base


def test_https_becomes_wss():
    assert _rest_base_to_ws_base("https://api.agentchat.me") == "wss://api.agentchat.me"


def test_http_becomes_ws():
    """For local dev / self-hosted on plain HTTP."""
    assert _rest_base_to_ws_base("http://localhost:8000") == "ws://localhost:8000"


def test_trailing_slash_stripped():
    assert _rest_base_to_ws_base("https://api.agentchat.me/") == "wss://api.agentchat.me"


def test_already_wss_passthrough():
    assert _rest_base_to_ws_base("wss://api.agentchat.me") == "wss://api.agentchat.me"


def test_already_ws_passthrough():
    assert _rest_base_to_ws_base("ws://localhost:8000") == "ws://localhost:8000"


def test_empty_falls_back_to_default():
    assert _rest_base_to_ws_base("") == "wss://api.agentchat.me"


def test_case_insensitive_scheme():
    """Operators might accidentally type HTTPS instead of https."""
    assert _rest_base_to_ws_base("HTTPS://api.agentchat.me") == "wss://api.agentchat.me"


def test_no_scheme_assumes_wss():
    """A bare host (operator forgot the scheme) defaults to secure."""
    assert _rest_base_to_ws_base("api.agentchat.me") == "wss://api.agentchat.me"


def test_https_with_path_segment():
    """Some self-hosted deployments mount under a prefix."""
    assert _rest_base_to_ws_base("https://example.com/agentchat") == "wss://example.com/agentchat"


def test_https_with_port():
    assert _rest_base_to_ws_base("https://example.com:8443") == "wss://example.com:8443"
