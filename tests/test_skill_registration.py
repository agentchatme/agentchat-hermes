"""Regression test for filesystem-relative bundled skill discovery.

Hermes's `PluginManager` loads directory-style plugins by file path
(spec_from_file_location), NOT by importable package name. So the
package is NOT registered in `sys.modules` under `agentchatme_hermes`,
and `importlib.resources.files("agentchatme_hermes")` raises
`ModuleNotFoundError`. Without the filesystem-relative fallback,
`register()` logs "AgentChat: failed to register bundled skill" and
the agent has NO etiquette manual to load via `skill_view`.

Discovered on the v0.1.62 e2e harness run.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _install_minimal_gateway_stubs():
    """Inject the framework modules `register()` reaches for. Same shape
    as `test_defensive_init.py` but copied here so the test file is
    self-contained — these are tiny tests."""
    if "gateway" in sys.modules and "gateway.platforms.base" in sys.modules:
        return

    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _Platform:
        def __init__(self, name):
            self.value = name

    class _BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform

    config_mod.Platform = _Platform
    base_mod.BasePlatformAdapter = _BasePlatformAdapter
    base_mod.MessageType = type("MT", (), {"TEXT": "text"})
    base_mod.MessageEvent = type("ME", (), {})
    base_mod.SendResult = type("SR", (), {})

    sys.modules.setdefault("gateway", gateway)
    sys.modules.setdefault("gateway.config", config_mod)
    sys.modules.setdefault("gateway.platforms", platforms_mod)
    sys.modules.setdefault("gateway.platforms.base", base_mod)


class _FakeCtx:
    """Captures register_* calls so we can assert on them."""

    def __init__(self):
        self.skills_registered: list[dict] = []
        self.platforms_registered: list[dict] = []
        self.tools_registered: list[dict] = []
        self.cli_commands: list[dict] = []

    def register_platform(self, **kw):
        self.platforms_registered.append(kw)

    def register_cli_command(self, **kw):
        self.cli_commands.append(kw)

    def register_skill(self, **kw):
        self.skills_registered.append(kw)

    def register_tool(self, **kw):
        self.tools_registered.append(kw)


def test_bundled_skill_registers_via_filesystem_fallback(monkeypatch):
    """When `importlib.resources.files` raises ModuleNotFoundError (the
    directory-plugin case), `register()` must still find the SKILL.md
    via `Path(__file__).parent` and call ctx.register_skill."""
    _install_minimal_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test")

    # Force the importlib.resources path to fail so we exercise the
    # filesystem fallback branch.
    import importlib.resources as resources_mod

    def _angry_files(*_a, **_kw):
        raise ModuleNotFoundError("No module named 'agentchatme_hermes'")

    monkeypatch.setattr(resources_mod, "files", _angry_files)

    from agentchatme_hermes import adapter as adapter_mod
    adapter_mod._AdapterCls = None  # bust cache so register() rebuilds

    ctx = _FakeCtx()
    adapter_mod.register(ctx)

    # The skill MUST have registered via the filesystem fallback.
    assert len(ctx.skills_registered) == 1, (
        f"expected exactly 1 skill registered, got {len(ctx.skills_registered)}: "
        f"{ctx.skills_registered}"
    )
    entry = ctx.skills_registered[0]
    assert entry["name"] == "agentchat"
    skill_path = entry["path"]
    assert skill_path.exists(), f"resolved path doesn't exist: {skill_path}"
    assert skill_path.name == "SKILL.md"


def test_bundled_skill_registers_via_importlib_resources(monkeypatch):
    """When `importlib.resources.files` succeeds (the wheel/pip case),
    the skill must still register exactly once — not twice (importlib +
    filesystem fallback)."""
    _install_minimal_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test")

    # Use a real fake that returns a real path so the test stays
    # close to the actual wheel-install scenario.
    from pathlib import Path

    fake_skill_path = Path(__file__).parent.parent / "agentchatme_hermes" / "skills" / "agentchat" / "SKILL.md"

    class _FakePathRef:
        def __init__(self, p):
            self._p = p

        def joinpath(self, *parts):
            return _FakePathRef(self._p.joinpath(*parts))

        def __str__(self):
            return str(self._p)

    import importlib.resources as resources_mod
    monkeypatch.setattr(
        resources_mod,
        "files",
        lambda _pkg: _FakePathRef(fake_skill_path.parent.parent),
    )

    from agentchatme_hermes import adapter as adapter_mod
    adapter_mod._AdapterCls = None

    ctx = _FakeCtx()
    adapter_mod.register(ctx)

    # Exactly one registration — the importlib path should win, the
    # filesystem fallback must NOT also fire and register a second.
    assert len(ctx.skills_registered) == 1


def test_register_does_not_raise_when_skill_file_missing(monkeypatch, tmp_path):
    """If BOTH the importlib path and the filesystem path can't find
    SKILL.md (corrupt install), `register()` must NOT raise — it should
    log a warning and continue so the rest of the plugin still loads."""
    _install_minimal_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test")

    import importlib.resources as resources_mod

    def _angry_files(*_a, **_kw):
        raise ModuleNotFoundError("nope")

    monkeypatch.setattr(resources_mod, "files", _angry_files)

    # Point Path(__file__).parent at a tmp dir with no skills/ subdir.
    from agentchatme_hermes import adapter as adapter_mod
    fake_module = MagicMock()
    fake_module.__file__ = str(tmp_path / "adapter.py")
    monkeypatch.setattr(adapter_mod, "__file__", str(tmp_path / "adapter.py"))
    adapter_mod._AdapterCls = None

    ctx = _FakeCtx()
    # The big assertion: register() must NOT raise.
    adapter_mod.register(ctx)

    # The skill MUST NOT have registered (file doesn't exist), but the
    # platform + tools MUST still be there.
    assert len(ctx.skills_registered) == 0
    assert len(ctx.platforms_registered) == 1
