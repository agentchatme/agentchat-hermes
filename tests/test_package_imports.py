"""Smoke test: package imports cleanly without the Hermes runtime present.

The plugin must be importable from a fresh `pip install agentchatme-hermes`
in a venv that does NOT have Hermes Agent on the path. The Hermes-specific
imports (gateway.platforms.base, gateway.session, hermes_cli.setup) live
inside lazy code paths — register(), interactive_setup(), tool handlers —
that only fire when the plugin is actually loaded by Hermes.

A regression here means we accidentally hoisted a Hermes import into module
scope and broke clean-environment installs.
"""

from __future__ import annotations


def test_top_level_import_is_side_effect_free():
    """`import agentchatme_hermes` must succeed even without Hermes installed."""
    import agentchatme_hermes

    assert hasattr(agentchatme_hermes, "register")
    assert hasattr(agentchatme_hermes, "__version__")
    assert isinstance(agentchatme_hermes.__version__, str)


def test_setup_helpers_are_importable():
    """The pure-Python helpers in setup.py must not transitively pull Hermes."""
    from agentchatme_hermes.setup import (
        _EMAIL_PATTERN,
        _HANDLE_PATTERN,
        _OTP_PATTERN,
        check_requirements,
        env_enablement,
        is_connected,
        validate_config,
    )

    # The functions exist and are callable. We don't actually invoke them
    # here — env_enablement / is_connected read os.environ which is fine,
    # and check_requirements imports `agentchatme` which is a hard dep.
    assert callable(check_requirements)
    assert callable(validate_config)
    assert callable(is_connected)
    assert callable(env_enablement)
    assert _EMAIL_PATTERN is not None
    assert _HANDLE_PATTERN is not None
    assert _OTP_PATTERN is not None


def test_check_requirements_passes_with_sdk_installed():
    """The SDK is a hard dep; check_requirements must return True."""
    from agentchatme_hermes.setup import check_requirements

    assert check_requirements() is True


def test_env_enablement_returns_none_when_no_key():
    """Without AGENTCHATME_API_KEY, env_enablement should refuse to seed."""
    import os

    from agentchatme_hermes.setup import env_enablement

    saved = os.environ.pop("AGENTCHATME_API_KEY", None)
    try:
        assert env_enablement() is None
    finally:
        if saved is not None:
            os.environ["AGENTCHATME_API_KEY"] = saved


def test_is_connected_false_with_blank_config():
    """is_connected must return False when no key is configured."""
    import os
    from types import SimpleNamespace

    from agentchatme_hermes.setup import is_connected

    saved = os.environ.pop("AGENTCHATME_API_KEY", None)
    try:
        # Use a stub config object — real PlatformConfig has more fields.
        cfg = SimpleNamespace(extra={})
        assert is_connected(cfg) is False
    finally:
        if saved is not None:
            os.environ["AGENTCHATME_API_KEY"] = saved


def test_bundled_skill_present_in_package():
    """The bundled SKILL.md must be shipped with the wheel.

    register() resolves it via importlib.resources; if this test fails,
    the wheel build is missing the data files (typically a hatchling
    build target oversight in pyproject.toml).
    """
    from importlib.resources import files

    skill = files("agentchatme_hermes").joinpath("skills/agentchat/SKILL.md")
    assert skill.is_file(), f"bundled skill not found at {skill}"
    body = skill.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "skill is missing YAML frontmatter"
    assert "name: agentchat" in body
    assert "platforms:" in body


def test_bundled_plugin_yaml_present_in_package():
    """plugin.yaml must be shipped — Hermes reads it for the config UI."""
    from importlib.resources import files

    manifest = files("agentchatme_hermes").joinpath("plugin.yaml")
    assert manifest.is_file(), f"bundled plugin.yaml not found at {manifest}"
    body = manifest.read_text(encoding="utf-8")
    assert "name: agentchat" in body
    assert "kind: platform" in body
    assert "AGENTCHATME_API_KEY" in body
