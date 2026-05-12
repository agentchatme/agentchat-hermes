"""Tests for the defensive `__init__` contract on AgentChatAdapter.

The adapter MUST NOT raise from `__init__`. Any error is stashed in
`self._init_error` and surfaced by `connect()` via `_set_fatal_error`
with `retryable=False`. An exception bubbling out of the adapter
factory short-circuits gateway.runner setup for EVERY platform, not
just AgentChat (`gateway/runner.py:2185`).

These tests don't require a real Hermes runtime — they install minimal
stubs into `sys.modules` for `gateway.config` / `gateway.platforms.base`
and exercise the adapter class returned by `_adapter_class()`.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_gateway_stubs():
    """Inject minimum-viable stubs for the framework modules.

    The adapter only needs:
      * `gateway.config.Platform(name)` — any callable that returns a
        value with `.value` attribute is fine.
      * `gateway.platforms.base.BasePlatformAdapter` — a class with a
        `__init__(self, config, platform)` that doesn't barf.
      * `gateway.platforms.base.MessageEvent / MessageType / SendResult`
        — opaque references the adapter stashes for later use.
    """
    if "gateway" in sys.modules:
        return  # Real Hermes already loaded — nothing to stub.

    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _Platform:
        def __init__(self, name):
            self.value = name

        def __eq__(self, other):
            return getattr(other, "value", other) == self.value

    class _BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform

        def _set_fatal_error(self, *_args, **_kwargs):
            self._fatal_error_called = True

        def _mark_connected(self):
            self._connected = True

        def _mark_disconnected(self):
            self._connected = False

        def _acquire_platform_lock(self, *_args, **_kwargs):
            return True

        def _release_platform_lock(self, *_args, **_kwargs):
            pass

        def build_source(self, **_kwargs):
            return MagicMock()

    class _MessageType:
        TEXT = "text"

    class _MessageEvent:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _SendResult:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.success = kw.get("success", False)
            self.message_id = kw.get("message_id")
            self.error = kw.get("error")

    config_mod.Platform = _Platform
    base_mod.BasePlatformAdapter = _BasePlatformAdapter
    base_mod.MessageType = _MessageType
    base_mod.MessageEvent = _MessageEvent
    base_mod.SendResult = _SendResult

    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_mod
    sys.modules["gateway.platforms.base"] = base_mod


def _make_adapter(config):
    """Force-rebuild the adapter class and instantiate it."""
    _install_gateway_stubs()

    from agentchatme_hermes import adapter as adapter_mod

    # Reset the cached class so each test gets a fresh build that
    # picks up the (stub) framework modules we just installed.
    adapter_mod._AdapterCls = None
    AdapterCls = adapter_mod._adapter_class()
    return AdapterCls(config)


def test_init_does_not_raise_on_normal_config(monkeypatch):
    """Sanity: with a normal config, __init__ records no error."""
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")
    cfg = SimpleNamespace(extra={"api_base": "https://api.test"})
    inst = _make_adapter(cfg)
    assert inst._init_error is None
    assert inst.api_key == "ac_test_key_123"
    assert inst.api_base == "https://api.test"


def test_init_handles_missing_extra_gracefully(monkeypatch):
    """A config without `.extra` must not raise."""
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_456")
    cfg = SimpleNamespace()  # no `.extra` attribute
    inst = _make_adapter(cfg)
    # getattr(config, "extra", {}) handles the absence — error stays None.
    assert inst._init_error is None


def test_init_handles_weird_allowed_handles_payload(monkeypatch):
    """Non-string, non-list payload for allowed_handles must not crash."""
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_789")
    # The current code calls `extra.get("allowed_handles") or []` which
    # coerces None / 0 / "" / False to []. A list containing non-str
    # values is filtered by isinstance() — should be safe.
    cfg = SimpleNamespace(extra={"allowed_handles": [None, 42, "@alice", "bob"]})
    inst = _make_adapter(cfg)
    assert inst._init_error is None
    assert inst._allowed_handles_lower == {"alice", "bob"}


def test_init_accepts_comma_string_allowlist(monkeypatch):
    """allowed_handles as a comma-separated string must parse."""
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_aaa")
    cfg = SimpleNamespace(extra={"allowed_handles": "@alice, bob ,@charlie"})
    inst = _make_adapter(cfg)
    assert inst._init_error is None
    assert inst._allowed_handles_lower == {"alice", "bob", "charlie"}


def test_init_merges_env_allowlist(monkeypatch):
    """AGENTCHATME_ALLOWED_HANDLES env var must merge with extra."""
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_bbb")
    monkeypatch.setenv("AGENTCHATME_ALLOWED_HANDLES", "@dave,erin")
    cfg = SimpleNamespace(extra={"allowed_handles": ["@alice"]})
    inst = _make_adapter(cfg)
    assert inst._init_error is None
    assert inst._allowed_handles_lower == {"alice", "dave", "erin"}


def test_init_stashes_error_when_super_init_raises(monkeypatch):
    """If `super().__init__` raises, the error is captured, not re-raised."""
    _install_gateway_stubs()

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_ccc")

    # Force BasePlatformAdapter.__init__ to raise.
    from gateway.platforms import base as base_mod  # type: ignore[import-not-found]

    original_init = base_mod.BasePlatformAdapter.__init__

    def _angry_init(self, *_args, **_kwargs):
        raise RuntimeError("BasePlatformAdapter is angry today")

    base_mod.BasePlatformAdapter.__init__ = _angry_init

    try:
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None
        AdapterCls = adapter_mod._adapter_class()
        # The big assertion: __init__ MUST NOT raise.
        inst = AdapterCls(SimpleNamespace(extra={}))
        assert inst._init_error is not None
        assert "BasePlatformAdapter is angry today" in inst._init_error
    finally:
        base_mod.BasePlatformAdapter.__init__ = original_init
        # Bust the cache so a subsequent test rebuilds against the
        # restored base class.
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None


def test_init_pre_populates_all_attributes_even_on_error(monkeypatch):
    """Even when init bails, downstream attrs must be present so
    `disconnect()` / `repr()` don't NPE on a partially-built adapter."""
    _install_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_ddd")

    from gateway.platforms import base as base_mod  # type: ignore[import-not-found]

    original_init = base_mod.BasePlatformAdapter.__init__

    def _angry_init(self, *_args, **_kwargs):
        raise RuntimeError("nope")

    base_mod.BasePlatformAdapter.__init__ = _angry_init

    try:
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None
        AdapterCls = adapter_mod._adapter_class()
        inst = AdapterCls(SimpleNamespace(extra={}))
        # Every attribute the rest of the adapter assumes exists.
        assert inst.api_key == ""
        assert inst.api_base == "https://api.agentchat.me"
        assert inst._allowed_handles_lower == set()
        assert inst._client is None
        assert inst._realtime is None
        assert inst.handle is None
        assert inst._lock_key is None
        assert inst._handler_unsubs == []
    finally:
        base_mod.BasePlatformAdapter.__init__ = original_init
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None


@pytest.mark.asyncio
async def test_connect_surfaces_init_error_as_fatal(monkeypatch):
    """When `_init_error` is set, `connect()` must call _set_fatal_error
    with retryable=False and return False — not try to use the broken
    client and crash on a None attribute access."""
    _install_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_eee")

    from gateway.platforms import base as base_mod  # type: ignore[import-not-found]

    original_init = base_mod.BasePlatformAdapter.__init__
    set_fatal_calls = []

    def _angry_init(self, *_args, **_kwargs):
        raise RuntimeError("super blew up")

    def _capture_fatal(self, code, msg, **kw):
        set_fatal_calls.append({"code": code, "msg": msg, **kw})

    base_mod.BasePlatformAdapter.__init__ = _angry_init
    base_mod.BasePlatformAdapter._set_fatal_error = _capture_fatal

    try:
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None
        AdapterCls = adapter_mod._adapter_class()
        inst = AdapterCls(SimpleNamespace(extra={}))

        result = await inst.connect()
        assert result is False
        assert len(set_fatal_calls) == 1
        assert set_fatal_calls[0]["code"] == "init_error"
        assert set_fatal_calls[0]["retryable"] is False
        assert "super blew up" in set_fatal_calls[0]["msg"]
    finally:
        base_mod.BasePlatformAdapter.__init__ = original_init
        from agentchatme_hermes import adapter as adapter_mod

        adapter_mod._AdapterCls = None
