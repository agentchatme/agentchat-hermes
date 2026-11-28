"""The Hermes surfaces this plugin depends on, checked against a real Hermes.

Skipped unless Hermes is importable (the unit suite runs without it). The
scheduled ``hermes-main`` workflow installs hermes-agent from its main branch,
its latest release and the floor release (``requires_hermes``), and runs the
whole suite with each, so a Hermes change that breaks one of these couplings
shows within a day instead of at the next release, and a break on the release
users run, or on the oldest one the plugin accepts, shows too. What each one is
for, and which public hook would replace it, is ``docs/hermes-coupling.md``.

Nothing here reaches a network service: the model is a localhost stub and
``HERMES_HOME`` is a temporary directory.
"""
from __future__ import annotations

import contextlib
import contextvars
import importlib
import importlib.util
import inspect
import json
import os
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agentchatme_hermes import turn_runner

if TYPE_CHECKING:
    from collections.abc import Iterator

if importlib.util.find_spec("run_agent") is None:
    pytest.skip("Hermes is not installed (the hermes-main workflow runs these)", allow_module_level=True)

# Hermes' own resource hygiene is not what these tests judge: a real AIAgent
# may leave a helper subprocess or socket for the garbage collector, which the
# suite's ``filterwarnings = error`` would turn into a failure here.
pytestmark = pytest.mark.filterwarnings(
    "ignore::ResourceWarning",
    "ignore::pytest.PytestUnraisableExceptionWarning",
    "ignore::DeprecationWarning",
)


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway profile home: never the real ``~/.hermes``."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _import(name: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return importlib.import_module(name)


def _params(fn: Any) -> set[str]:
    return set(inspect.signature(fn).parameters)


@contextlib.contextmanager
def _home_override(home: Path) -> Iterator[None]:
    """Hermes' context-local profile home, as its loader binds it around ``register()``."""
    constants = _import("hermes_constants")
    token = constants.set_hermes_home_override(str(home))
    try:
        yield
    finally:
        constants.reset_hermes_home_override(token)


# ─── a localhost model ──────────────────────────────────────────────────────


class _Stub:
    """An OpenAI-compatible endpoint on localhost: answers, or hangs."""

    def __init__(self, *, hang: bool) -> None:
        self.requests: list[dict[str, Any]] = []
        # The bearer each request carried: which provider key the call used.
        self.auth: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "model": body.get("model"), "tools": "tools" in body})
                stub.auth.append(str(self.headers.get("authorization") or ""))
                if hang:
                    time.sleep(60)
                    return
                if body.get("stream"):
                    self._stream()
                    return
                data = json.dumps(
                    {
                        "id": "c1",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body.get("model"),
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for chunk in (
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ):
                    payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", **chunk}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hanging_model() -> Iterator[_Stub]:
    stub = _Stub(hang=True)
    yield stub
    stub.close()


@pytest.fixture
def answering_model() -> Iterator[_Stub]:
    stub = _Stub(hang=False)
    yield stub
    stub.close()


def _agent(base_url: str) -> Any:
    run_agent = _import("run_agent")
    return run_agent.AIAgent(
        model="contract-model",
        base_url=base_url,
        api_key="contract-key",
        provider="custom",
        session_id="agentchat:contract",
        platform="agentchat",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        enabled_toolsets=[],
        run_budget_seconds=120,
    )


# ─── turns (run_agent.AIAgent) ─────────────────────────────────────────────


def test_aiagent_constructor_takes_what_the_invoker_passes() -> None:
    accepted = _params(_import("run_agent").AIAgent)
    needed = {
        "model", "session_id", "session_db", "platform", "quiet_mode", "load_soul_identity",
        "skip_context_files", "api_key", "base_url", "provider", "api_mode", "run_budget_seconds",
    }
    assert needed <= accepted, f"AIAgent no longer takes {sorted(needed - accepted)}"


def test_aiagent_turn_surface() -> None:
    agent_cls = _import("run_agent").AIAgent
    assert {"conversation_history"} <= _params(agent_cls.run_conversation)
    assert "tool_reason" in _params(agent_cls.hard_interrupt)
    assert callable(agent_cls.interrupt) and callable(agent_cls.get_activity_summary)


def test_a_hung_turn_is_stopped_by_the_time_limit(hanging_model: _Stub) -> None:
    """The real thing: Hermes' own hard_interrupt ends a turn whose provider call hangs."""
    agent = _agent(hanging_model.base_url)
    limits = turn_runner.TurnLimits(idle_s=3.0, max_s=60.0, grace_s=30.0, poll_s=0.25)
    report = turn_runner.run_turn(agent, "Say hi.", session_id="agentchat:contract", limits=limits)
    assert report.status == "timeout" and report.ended, report.detail
    assert isinstance(report.result, dict) and report.result.get("interrupted") is True
    assert "agentchat_turn_time_limit" in str(report.result.get("turn_exit_reason")), "Hermes attributes the stop to us"
    assert turn_runner.turn_failure(report.result) is not None
    assert report.calls is not None and report.calls.made == 0, "the transcript is a messages list"
    assert report.turn_end().status == "failed"


def test_a_completed_turn_reports_its_transcript(answering_model: _Stub) -> None:
    agent = _agent(answering_model.base_url)
    report = turn_runner.run_turn(agent, "Say hi.", session_id="agentchat:contract", limits=turn_runner.TurnLimits(idle_s=30.0, max_s=60.0, poll_s=0.25))
    assert report.status == "completed", report.detail
    assert isinstance(report.result, dict) and isinstance(report.result.get("messages"), list)
    assert turn_runner.turn_failure(report.result) is None
    assert answering_model.requests and answering_model.requests[-1]["model"] == "contract-model"


def test_session_db() -> None:
    assert callable(_import("hermes_state").SessionDB)


# ─── the gate call (ctx.llm, else the auxiliary client) ───────────────────


def test_plugin_context_surface() -> None:
    context = _import("hermes_cli.plugins").PluginContext
    for name in ("register_cli_command", "register_skill", "on_unload", "register_command", "ask_choice"):
        assert callable(getattr(context, name, None)), name
    assert isinstance(inspect.getattr_static(context, "llm"), property)
    # /agentchat allow: the handler's raw arguments, its description, the hint.
    assert {"name", "handler", "description", "args_hint"} <= _params(context.register_command)
    assert list(inspect.signature(context.ask_choice).parameters) == ["self", "question", "choices"]


# ─── the setup line: a plugin section in each new session's system prompt ──


def test_the_setup_line_is_rendered_into_a_new_sessions_prompt_until_setup_is_done(
    _hermes_home: Path, _plugin_manager: Any
) -> None:
    """``ctx.register_system_prompt_section`` takes a callable, renders it for a
    new session, and skips it when it returns no text (set up: nothing to
    say). A Hermes without plugin prompt sections is skipped by the plugin."""
    from agentchatme_hermes import setup_prompt
    from agentchatme_hermes.profile_scope import ProfileScope

    plugins = _import("hermes_cli.plugins")
    if not callable(getattr(plugins.PluginContext, "register_system_prompt_section", None)):
        pytest.skip("this Hermes has no plugin system prompt sections (the plugin skips the line)")
    params = inspect.signature(plugins.PluginContext.register_system_prompt_section).parameters
    assert {"id", "content", "position", "max_chars"} <= set(params)
    manifest = _import("hermes_cli.plugins_manifest").PluginManifest(name="agentchat", source="entrypoint")
    ctx = plugins.PluginContext(manifest, _plugin_manager)
    sections = _plugin_manager._system_prompt_sections
    try:
        assert setup_prompt.register_setup_section(ctx, ProfileScope.capture(_hermes_home)) is True
        rendered = _plugin_manager.render_system_prompt_sections({"session_id": "s", "platform": "cli"})
        texts = {item.id: item.content for item in rendered}
        assert texts.get(setup_prompt.SECTION_ID) == setup_prompt.NOT_SET_UP.format(cmd="hermes agentchat")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(setup_prompt, "section_text", lambda home, *, api_base: "")
            rendered = _plugin_manager.render_system_prompt_sections({"session_id": "t", "platform": "cli"})
        assert setup_prompt.SECTION_ID not in {item.id for item in rendered}, "an empty line is skipped"
    finally:
        sections.pop(setup_prompt.SECTION_ID, None)


def test_the_reminder_hook_is_one_hermes_runs_before_each_turn() -> None:
    """The reminder is a ``pre_llm_call`` hook: a valid hook name here, registered with
    ``ctx.register_hook(name, callback)``, called with the turn's platform and
    ``parent_session_id`` (how the reminder skips turns no person started)."""
    plugins = _import("hermes_cli.plugins")
    assert "pre_llm_call" in plugins.VALID_HOOKS
    assert {"hook_name", "callback"} <= _params(plugins.PluginContext.register_hook)
    source = inspect.getsource(_import("agent.turn_context"))
    assert '"pre_llm_call"' in source and "parent_session_id=" in source and "platform=" in source


def test_the_graceful_restart_is_sigusr1_and_ensure_exists() -> None:
    """The restart the person's Allow starts (gateway_restart): Hermes' gateway handles SIGUSR1 as
    its drain-first restart, and ``hermes gateway ensure`` is a command (what the helper runs when no
    supervisor relaunches)."""
    import signal

    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("no SIGUSR1 here (Windows): the person restarts")
    handler = inspect.getsource(_import("gateway.run")._start_gateway_make_restart_signal_handler)
    assert "request_restart(" in handler, "SIGUSR1 is the drain-first restart"
    gateway_cli = inspect.getsource(_import("hermes_cli.subcommands.gateway"))
    assert "Pending startup is not readiness" in gateway_cli, "`hermes gateway ensure` and its exit codes"


# ─── the manual: a plugin skill whose file the plugin keeps current ────────


@pytest.fixture
def _plugin_manager(_hermes_home: Path) -> Iterator[Any]:
    """This profile's real plugin manager, discovered first (as at boot, so the
    ``discover_plugins()`` inside ``skill_view`` finds a finished sweep), with
    our skill entry removed again afterwards."""
    plugins = _import("hermes_cli.plugins")
    plugins.discover_plugins()
    manager = plugins.get_plugin_manager()
    try:
        yield manager
    finally:
        manager.remove_plugin_skill("agentchat:agentchat")


@pytest.mark.usefixtures("live_texts")
def test_a_skill_outside_the_package_is_read_fresh_after_an_atomic_replace(
    _hermes_home: Path, _plugin_manager: Any
) -> None:
    """With ``live_texts`` on, the design the manual rests on, from Hermes' code rather than a
    documented contract: ``register_skill`` takes any existing path (here
    ``<home>/plugin-data/agentchat/manual/SKILL.md``, outside the package),
    ``skill_view`` reads that file fresh on every call, and a file replaced
    atomically (temp + rename) is served at once and stays registered."""
    import hashlib

    import httpx

    from agentchatme_hermes import manual
    from agentchatme_hermes.skills import SKILL_DESCRIPTION, register_skill

    plugins = _import("hermes_cli.plugins")
    skills_tool = _import("tools.skills_tool")
    manifest = _import("hermes_cli.plugins_manifest").PluginManifest(name="agentchat", source="entrypoint")
    ctx = plugins.PluginContext(manifest, _plugin_manager)

    path = register_skill(ctx, hermes_home=_hermes_home)
    assert path == manual.manual_path(_hermes_home)
    assert manual.BUNDLED_MANUAL_PATH.parent not in path.parents, "outside the package"
    assert Path(_plugin_manager.find_plugin_skill("agentchat:agentchat")) == path
    rows = {meta["name"]: meta for meta in _plugin_manager.list_plugin_skill_metadata()}
    assert rows["agentchat:agentchat"]["description"] == SKILL_DESCRIPTION, "the index row names no version"
    # The skills list shows the description clipped to Hermes' default width
    # (60): the join line (lab R3, L1-F03) is read in full there.
    from agentchatme_hermes.skills import SKILL_JOIN_LINE

    skill_utils = _import("agent.skill_utils")
    assert skill_utils.SKILL_PROMPT_DESC_LIMIT == 60
    assert skill_utils.clip_skill_description(SKILL_DESCRIPTION).startswith(SKILL_JOIN_LINE.rstrip("."))

    first = json.loads(skills_tool.skill_view("agentchat:agentchat"))
    assert first["success"] is True, first
    assert manual.BUNDLED_MANUAL_PATH.read_text(encoding="utf-8").split("\n", 3)[2] in first["content"]
    assert Path(first["skill_dir"]) == path.parent

    body = b"---\nname: agentchat\nversion: 9.9.9\ndescription: x\n---\nTHE NEW MANUAL\n"
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)))
    served = {"url": "https://agentchat.me/skill.md", "version": "9.9.9", "sha256": hashlib.sha256(body).hexdigest()}
    try:
        result = manual.sync_once(home=_hermes_home, served=served, api_base="https://api.agentchat.me", http_client=client)
    finally:
        client.close()
    assert result is not None and result.state == "updated" and path.read_bytes() == body

    second = json.loads(skills_tool.skill_view("agentchat:agentchat"))
    assert second["success"] is True and "THE NEW MANUAL" in second["content"], second
    assert Path(_plugin_manager.find_plugin_skill("agentchat:agentchat")) == path, "still registered"
    # The folder holds only SKILL.md: skill_view(file_path=...) has nothing else to serve.
    assert sorted(p.name for p in path.parent.iterdir()) == ["SKILL.md"]
    other = json.loads(skills_tool.skill_view("agentchat:agentchat", file_path="credentials.json"))
    assert other["success"] is False


def test_this_releases_manual_is_served_from_the_package(_hermes_home: Path, _plugin_manager: Any) -> None:
    """``live_texts`` off (the default): the skill Hermes serves is the
    package's copy, read by ``skill_view`` as it is, alone in its folder; the
    plugin's storage is not touched."""
    from agentchatme_hermes import manual
    from agentchatme_hermes.skills import register_skill

    plugins = _import("hermes_cli.plugins")
    skills_tool = _import("tools.skills_tool")
    manifest = _import("hermes_cli.plugins_manifest").PluginManifest(name="agentchat", source="entrypoint")
    ctx = plugins.PluginContext(manifest, _plugin_manager)

    path = register_skill(ctx, hermes_home=_hermes_home, place=True)
    assert path == manual.BUNDLED_MANUAL_PATH
    assert Path(_plugin_manager.find_plugin_skill("agentchat:agentchat")) == path
    served = json.loads(skills_tool.skill_view("agentchat:agentchat"))
    assert served["success"] is True, served
    assert f"version: {manual.bundled_version()}" in served["content"]
    assert sorted(p.name for p in path.parent.iterdir()) == ["SKILL.md"]
    assert not (_hermes_home / "plugin-data" / "agentchat").exists()


# ─── the setting: live_texts (config_schema, ctx.get_config) ───────────────

_REPO = Path(__file__).resolve().parents[1]


def test_live_texts_reaches_each_profile_through_ctx_get_config(
    _hermes_home: Path, _plugin_manager: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ctx.get_config`` reads ``plugins.entries.agentchat.settings`` of the
    profile Hermes has bound (``register()`` runs bound to its profile); the
    command doctor and the README give (``hermes config set …``) writes a
    bool it reads back."""
    from agentchatme_hermes.texts import read_mode

    plugins = _import("hermes_cli.plugins")
    constants = _import("hermes_constants")
    config = _import("hermes_cli.config")
    manifest = _import("hermes_cli.plugins_manifest").PluginManifest(name="agentchat", source="entrypoint")
    ctx = plugins.PluginContext(manifest, _plugin_manager)

    unset = read_mode(ctx)
    assert unset.live is False and unset.problem is None and "not set" in unset.detail
    config.set_config_value("plugins.entries.agentchat.settings.live_texts", "true")
    capsys.readouterr()
    assert read_mode(ctx).live is True

    other = tmp_path / "profiles" / "b"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text(
        "plugins:\n  entries:\n    agentchat:\n      settings:\n        live_texts: false\n", encoding="utf-8"
    )
    token = constants.set_hermes_home_override(str(other))
    try:
        bound = read_mode(ctx)
        assert bound.live is False and bound.detail == "live_texts off", "the bound profile's own setting"
    finally:
        constants.reset_hermes_home_override(token)
    assert read_mode(ctx).live is True


def test_our_config_schema_is_one_boolean_setting_hermes_shows() -> None:
    """The manifest's ``config_schema`` as Hermes parses, validates and renders
    it (the Desktop form, ``hermes plugins config``, the settings offer at
    ``hermes plugins install``). ``hermes_cli.plugins_settings`` names the
    form's fields on every release (a module up to v0.23.3, a package
    re-exporting ``fields`` since v0.23.4)."""
    manifest_mod = _import("hermes_cli.plugins_manifest")
    fields_mod = _import("hermes_cli.plugins_settings")

    parsed = manifest_mod.parse_manifest_file(_REPO / "plugin.yaml", _REPO, "user", "")
    assert parsed is not None and set(parsed.config_schema) == {"live_texts"}
    assert manifest_mod.validate_config_schema("agentchat", parsed.config_schema, {"live_texts": True}) == []
    assert manifest_mod.validate_config_schema("agentchat", parsed.config_schema, {"live_texts": "yes"}) != []
    (field,) = fields_mod.plugin_settings_fields("agentchat", _REPO)
    assert field["key"] == "live_texts" and field["type"] == "boolean"
    assert field["default"] is False and field["value"] is False
    assert field["label"] == "Keep the AgentChat manual and identity line current"
    assert field["description"].startswith("Off: your agent reads the AgentChat manual")


def _pyproject_sdk_requirement() -> str:
    import re

    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*"(agentchatme[^"]*)",?\s*$', pyproject, re.MULTILINE)
    assert match, "pyproject.toml declares agentchatme"
    return match.group(1)


def test_hermes_installs_the_sdk_pyproject_declares_and_asks_first() -> None:
    """The plugin installs nothing: Hermes reads this tree's one Python
    dependency from pyproject.toml (``pm.plugin_declarations``), shows it and
    asks before installing it; a headless install without ``--yes-deps`` is
    refused (Hermes leaves the plugin installed but disabled) and
    ``--yes-deps`` is the yes."""
    import io

    from rich.console import Console

    declarations = _import("pm.plugin_declarations")
    install = _import("hermes_cli.plugins_cmd_install")
    plugins_cmd = _import("hermes_cli.plugins_cmd")

    declaration = declarations.read_python_declaration(_REPO)
    assert declaration.pyproject == _REPO / "pyproject.toml"
    assert declaration.install_requirements == (_pyproject_sdk_requirement(),)
    manifest = plugins_cmd._read_manifest_for_install(_REPO)
    shown = io.StringIO()
    consented, reason = install._install_plugin_python_deps(manifest, _REPO, Console(file=shown, width=200))
    assert consented is False and getattr(reason, "failure_class", None) == "non_interactive"
    assert f"- {_pyproject_sdk_requirement()}" in shown.getvalue()
    assert install._install_plugin_python_deps(manifest, _REPO, Console(file=io.StringIO()), assume_yes=True) == (
        True,
        None,
    )


def test_hermes_reads_our_floor_and_skips_the_plugin_below_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``requires_hermes`` as Hermes parses it from our manifest, validates it
    (``hermes plugins validate``) and gates the load on it."""
    manifest_mod = _import("hermes_cli.plugins_manifest")
    validate = _import("hermes_cli.plugin_validate")

    parsed = manifest_mod.parse_manifest_file(_REPO / "plugin.yaml", _REPO, "user", "")
    assert parsed is not None and parsed.requires_hermes == ">=0.23.2"
    assert validate._requires_hermes_spec_valid(parsed.requires_hermes)
    monkeypatch.setattr(manifest_mod, "running_hermes_version", lambda: "0.23.1")
    assert manifest_mod.requires_hermes_error(parsed) == "requires hermes >=0.23.2, running 0.23.1"
    for running in ("0.23.2", "0.23.4", "0.24.0", "unknown"):
        monkeypatch.setattr(manifest_mod, "running_hermes_version", lambda running=running: running)
        assert manifest_mod.requires_hermes_error(parsed) is None, running


def test_a_directory_plugin_is_imported_under_the_namespace_the_kit_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kit._install_source`` tells a directory install (``hermes plugins
    install``) from a pip install by the module name Hermes' directory loader
    gives the plugin, ``hermes_plugins.<slug>``."""
    loader = _import("hermes_cli.plugins_loader")
    plugins = _import("hermes_cli.plugins")
    manifest_mod = _import("hermes_cli.plugins_manifest")

    assert loader._NS_PARENT == "hermes_plugins"
    monkeypatch.setattr(loader, "_BARE_MODULE_SCOPE", {})  # which scope claimed the bare name: not this test's
    parsed = manifest_mod.parse_manifest_file(_REPO / "plugin.yaml", _REPO, "user", "")
    manager = plugins.PluginManager()
    assert manager._directory_module_name(parsed).startswith("hermes_plugins.agentchat")


def test_dropping_the_manifest_copy_of_the_sdk_widens_nothing(tmp_path: Path) -> None:
    """0.6.5's manifests listed the SDK under ``pip_dependencies``; this
    release's do not and name a Hermes floor instead. Hermes' unattended update
    (``plugins.auto_apply``) sees no wider surface and no new dependency."""
    catalog = _import("hermes_cli.plugins_cmd_catalog")
    declarations = _import("pm.plugin_declarations")
    plugins_cmd = _import("hermes_cli.plugins_cmd")

    manifest = (_REPO / "plugin.yaml").read_text(encoding="utf-8")
    floor = 'requires_hermes: ">=0.23.2"\n'
    assert floor in manifest
    previous = manifest.replace(floor, f"pip_dependencies:\n  - {_pyproject_sdk_requirement()}\n")
    trees = {}
    for name, text in (("old", previous), ("new", manifest)):
        tree = tmp_path / name
        tree.mkdir()
        (tree / "plugin.yaml").write_text(text, encoding="utf-8")
        (tree / "pyproject.toml").write_bytes((_REPO / "pyproject.toml").read_bytes())
        trees[name] = tree
    old = catalog.plugin_surface(plugins_cmd._read_manifest(trees["old"]), trees["old"])
    new = catalog.plugin_surface(plugins_cmd._read_manifest(trees["new"]), trees["new"])
    assert catalog.surface_delta(old, new) == {}
    assert (
        declarations.read_python_declaration(trees["new"]).install_requirements
        == declarations.read_python_declaration(trees["old"]).install_requirements
    )


def test_a_release_that_changes_only_the_texts_widens_nothing(tmp_path: Path) -> None:
    """What Hermes' unattended update refuses (``plugins.auto_apply``): a wider
    surface (``plugin_surface``: capabilities, tools, hooks, Python
    dependencies, sign-ins, a Desktop half) or new Python dependencies. A
    release that only refreshes the bundled texts, and this release's added
    ``config_schema``, are neither, so Hermes' own update applies them."""
    catalog = _import("hermes_cli.plugins_cmd_catalog")
    declarations = _import("pm.plugin_declarations")
    plugins_cmd = _import("hermes_cli.plugins_cmd")

    manifest = (_REPO / "plugin.yaml").read_text(encoding="utf-8")
    before_schema = manifest[: manifest.index("config_schema:")]
    trees = {}
    for name, text, manual_text in (
        ("old", before_schema, "---\nname: agentchat\nversion: 2.4.4\n---\nold\n"),
        ("new", manifest, "---\nname: agentchat\nversion: 2.4.99\n---\nnew\n"),
    ):
        tree = tmp_path / name
        (tree / "agentchatme_hermes" / "skills" / "agentchat").mkdir(parents=True)
        (tree / "plugin.yaml").write_text(text, encoding="utf-8")
        (tree / "pyproject.toml").write_bytes((_REPO / "pyproject.toml").read_bytes())
        (tree / "agentchatme_hermes" / "skills" / "agentchat" / "SKILL.md").write_text(manual_text, encoding="utf-8")
        (tree / "agentchatme_hermes" / "texts_snapshot.json").write_text(f'{{"name": "{name}"}}', encoding="utf-8")
        trees[name] = tree
    old = catalog.plugin_surface(plugins_cmd._read_manifest(trees["old"]), trees["old"])
    new = catalog.plugin_surface(plugins_cmd._read_manifest(trees["new"]), trees["new"])
    assert catalog.surface_delta(old, new) == {}
    assert (
        declarations.read_python_declaration(trees["new"]).install_requirements
        == declarations.read_python_declaration(trees["old"]).install_requirements
    )


def test_the_manual_lives_in_hermes_plugin_storage(_hermes_home: Path) -> None:
    """``plugins/plugin_storage.plugin_data_dir`` is Hermes' per-plugin storage;
    the plugin computes the same folder from the profile's home (never from
    ``get_hermes_home()`` on a thread without the profile)."""
    from agentchatme_hermes import manual

    storage = _import("plugins.plugin_storage")
    assert Path(storage.plugin_data_dir("agentchat")).resolve() == manual.manual_dir(_hermes_home).parent.resolve()


def test_doctor_finds_a_stray_agentchat_skill_through_hermes(_hermes_home: Path) -> None:
    """``doctor`` reports an ``agentchat`` skill in Hermes' own skill folders
    through the public skill-catalog helpers (``tools.skills_tool``:
    ``find_all_skills``, ``skill_search_dirs``, ``locate_skill``) and never
    removes it."""
    from agentchatme_hermes import cli

    skills_tool = _import("tools.skills_tool")
    for name in ("find_all_skills", "skill_search_dirs", "locate_skill"):
        assert callable(getattr(skills_tool, name, None)), name
    stray = _hermes_home / "skills" / "agentchat" / "SKILL.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("---\nname: agentchat\nversion: 2.3.1\n---\nold\n", encoding="utf-8")
    found = cli._stray_agentchat_skills()
    assert [(p.resolve(), v) for p, v in found] == [(stray.resolve(), "2.3.1")]
    assert stray.exists()


def test_doctor_names_a_plugin_directorys_snapshot_as_hermes_does(_hermes_home: Path) -> None:
    """A catalog or git install also installs the plugin's package, editable,
    from a snapshot Hermes' package manager takes of the directory, named
    ``pm.workspace._member_key(directory)``. ``doctor`` tells that install from
    a second copy by this name."""
    from agentchatme_hermes import cli

    member_key = _import("pm.workspace")._member_key
    for name in ("agentchat", "AgentChat copy.bak"):
        directory = _hermes_home / "plugins" / name
        directory.mkdir(parents=True)
        assert cli._hermes_member_key(directory) == member_key(directory)


@pytest.mark.parametrize("shape", ["at-pin", "with-ref", "top-level"])
def test_doctor_reads_a_catalog_install_record_as_hermes_does(_hermes_home: Path, shape: str) -> None:
    """``doctor`` names a catalog install from Hermes' installer-owned record
    (``plugins/.install-metadata.json``); Hermes' own reader of that record
    (``plugins_cmd_catalog.read_catalog_sidecar``) must see the same entry and
    commit. ``top-level`` is the earlier record, which Hermes still reads."""
    from agentchatme_hermes import cli

    sha = "0002d3991addc89d6018a366a23f61b17a12b4dd"
    repo = "https://github.com/agentchatme/agentchat-hermes"
    record: dict[str, Any] = {"pinned": True, "revision": sha, "source": repo}
    if shape == "top-level":
        record.update(catalog_name="agentchat", catalog_tier="community")
    else:
        pin = sha if shape == "at-pin" else ""
        record["catalog"] = {"name": "agentchat", "repo": repo, "tier": "community", "sha": sha, "pin": pin}
    directory = _hermes_home / "plugins" / "agentchat"
    directory.mkdir(parents=True)
    (_hermes_home / "plugins" / ".install-metadata.json").write_text(json.dumps({"agentchat": record}))

    line = cli._catalog_install_line(directory, _hermes_home)
    hermes = _import("hermes_cli.plugins_cmd_catalog").read_catalog_sidecar(directory)
    assert hermes is not None
    assert hermes["catalog_name"] == "agentchat"
    assert hermes["sha"] == sha
    assert line is not None
    assert line.startswith(f"from the Hermes plugin catalog at {sha[:8]}")
    assert ("off the catalog's pin" in line) is (hermes["pin"] == "")
    assert "`hermes plugins update agentchat`" in line


def test_hermes_unload_stops_our_runtime(tmp_path: Path) -> None:
    """A plugin reload/disable runs ``ctx.on_unload``: our hook stops the
    runtime (socket, worker, lock) and forgets it."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agentchatme_hermes._register import ActivationWatcher, _close_on_unload

    plugins = _import("hermes_cli.plugins")
    manifest = _import("hermes_cli.plugins_manifest").PluginManifest(name="agentchat", source="entrypoint")
    manager = plugins.PluginManager()
    ctx = plugins.PluginContext(manifest, manager)
    watcher = ActivationWatcher(hermes_home=tmp_path, profile="default", gateway_mode=True)
    runtime = SimpleNamespace(stop=MagicMock())
    assert watcher._adopt(runtime)  # type: ignore[arg-type]
    _close_on_unload(ctx, watcher)
    runtime.stop.assert_not_called()
    assert manager.unload("agentchat") is True
    runtime.stop.assert_called_once()
    assert watcher.closed


def test_plugin_llm_reaches_the_profiles_main_model(answering_model: _Stub, _hermes_home: Path) -> None:
    from agentchatme_hermes.model_call import build_call_model

    (_hermes_home / "config.yaml").write_text(
        f"model:\n  default: contract-main-model\n  provider: custom\n  base_url: {answering_model.base_url}\n  api_key: contract-key\n",
        encoding="utf-8",
    )
    plugin_llm = _import("agent.plugin_llm")
    assert {"temperature", "max_tokens", "timeout", "purpose"} <= _params(plugin_llm.PluginLlm.complete)
    call_model = build_call_model(main_runtime={}, timeout_s=20.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat"))
    assert call_model("THE PROMPT") == "done"
    assert answering_model.requests[-1] == {"path": "/v1/chat/completions", "model": "contract-main-model", "tools": False}


def _main_model_config(home: Path, base_url: str) -> None:
    (home / "config.yaml").write_text(
        f"model:\n  default: contract-main-model\n  provider: custom\n  base_url: {base_url}\n  api_key: contract-key\n",
        encoding="utf-8",
    )


def _in_a_worker(fn: Any) -> Any:
    """As the receive loop runs ``call_model``: on a worker thread."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(60.0)
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_plugin_llm_async_call_reaches_the_main_model(answering_model: _Stub, _hermes_home: Path) -> None:
    """With the receive loop's cancel handle the gate call is
    ``ctx.llm.acomplete`` on the invoker's one gate loop: same model, same
    answer, twice in a row on the same loop (Hermes' async client bound to it
    is reused)."""
    from agentchatme_hermes.model_call import GateLoop, build_call_model

    _main_model_config(_hermes_home, answering_model.base_url)
    plugin_llm = _import("agent.plugin_llm")
    assert inspect.iscoroutinefunction(plugin_llm.PluginLlm.acomplete)
    assert {"messages", "temperature", "max_tokens", "timeout", "purpose"} <= _params(plugin_llm.PluginLlm.acomplete)
    gate_loop = GateLoop()
    try:
        call_model = build_call_model(
            main_runtime={}, timeout_s=20.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat"), gate_loop=gate_loop
        )
        for _ in range(2):
            assert _in_a_worker(lambda: call_model("THE PROMPT", cancel=threading.Event())) == "done"
            assert answering_model.requests[-1] == {"path": "/v1/chat/completions", "model": "contract-main-model", "tools": False}
    finally:
        gate_loop.close()


def test_plugin_llm_async_call_is_cancelled_when_the_loop_stops_waiting(hanging_model: _Stub, _hermes_home: Path) -> None:
    from agentchatme_hermes.model_call import GateCallCancelled, GateLoop, build_call_model

    _main_model_config(_hermes_home, hanging_model.base_url)
    plugin_llm = _import("agent.plugin_llm")
    gate_loop = GateLoop()
    call_model = build_call_model(
        main_runtime={}, timeout_s=30.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat"), gate_loop=gate_loop
    )
    cancel = threading.Event()

    def cancel_once_asked() -> None:
        deadline = time.monotonic() + 15.0
        while not hanging_model.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    threading.Thread(target=cancel_once_asked, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(GateCallCancelled):
            _in_a_worker(lambda: call_model("THE PROMPT", cancel=cancel))
        assert hanging_model.requests, "the request reached the model"
        assert time.monotonic() - started < 20.0, "ended by the cancel, not by its 30 s timeout"
    finally:
        gate_loop.close()


def test_auxiliary_call_llm_takes_what_the_fallback_passes() -> None:
    accepted = _params(_import("agent.auxiliary_client").call_llm)
    assert {"task", "provider", "model", "base_url", "api_key", "main_runtime", "messages", "temperature", "max_tokens", "timeout"} <= accepted


def test_config_and_provider_helpers() -> None:
    config = _import("hermes_cli.config")
    for name in ("load_config", "save_config", "get_env_value", "save_env_value", "is_managed"):
        assert callable(getattr(config, name, None)), name
    # The hosted-tools entry is read as written (load_config expands ${VAR}).
    assert any(callable(getattr(config, n, None)) for n in ("read_user_config_raw", "read_raw_config"))
    assert "target_model" in _params(_import("hermes_cli.runtime_provider").resolve_runtime_provider)


def test_the_hosted_tools_entry_round_trips_through_hermes_config(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through Hermes' own reader and saver: an earlier release's entry gains
    the client header and keeps the keys the user added and the file's
    comments, the key stays a template in config.yaml with the key loaded, a
    second pass writes nothing, and logout removes the entry."""
    from fake_keys import api_key

    from agentchatme_hermes import mcp_registration as reg

    key = api_key("contract_round_trip_value")
    monkeypatch.setenv("AGENTCHAT_API_KEY", key)
    config_path = _hermes_home / "config.yaml"
    config_path.write_text(
        "# the operator's own note\n"
        "mcp_servers:\n"
        "  agentchat:  # hosted tools\n"
        '    url: "https://api.agentchat.me/mcp"\n'
        "    headers:\n"
        '      Authorization: "Bearer ${AGENTCHAT_API_KEY}"\n'
        "    timeout: 120\n",
        encoding="utf-8",
    )
    config = _import("hermes_cli.config")

    assert reg.inspect_entry_identity() == ("ours", "missing")
    assert reg.install_entry().outcome == "upgraded"
    text = config_path.read_text(encoding="utf-8")
    assert key not in text, "the key must never be written into config.yaml"
    assert "# the operator's own note" in text and "# hosted tools" in text, "Hermes keeps comments"
    entry = config.read_user_config_raw()["mcp_servers"]["agentchat"]
    assert entry["headers"] == {
        "Authorization": "Bearer ${AGENTCHAT_API_KEY}",
        "X-AgentChat-Client": "hermes",
    }
    assert entry["timeout"] == 120
    assert reg.inspect_entry_identity() == ("ours", "current")
    assert "trust" not in entry, "an earlier release's entry keeps no trust key"
    before = config_path.read_bytes()
    assert reg.install_entry().outcome == "present"
    assert reg.install_entry(decision="no").outcome == "present", "a no never narrows an existing entry"
    assert config_path.read_bytes() == before, "nothing to change, nothing written"
    assert reg.uninstall_entry() is True
    assert "agentchat" not in (config.read_user_config_raw().get("mcp_servers") or {})


def test_the_tools_entry_carries_its_trust_through_hermes_config(_hermes_home: Path) -> None:
    """Nobody asked: nothing written. A no writes ``trust: untrusted``, a later
    yes turns it ``full``; Hermes' own MCP config loader reads what was
    written, and the operator's comments survive."""
    from agentchatme_hermes import mcp_registration as reg

    config_path = _hermes_home / "config.yaml"
    config_path.write_text("# the operator's own note\nmodel:\n  default: x\n", encoding="utf-8")
    loader = _import("tools.mcp_tool_config")._load_mcp_config
    assert reg.install_entry().outcome == "missing"
    assert "mcp_servers" not in config_path.read_text(encoding="utf-8")
    assert reg.install_entry(decision="no") == reg.EntryPlacement("added", "untrusted")
    assert loader()["agentchat"]["trust"] == "untrusted"
    assert reg.inspect_allowance().allowed is False
    assert reg.install_entry(decision="yes") == reg.EntryPlacement("allowed", "full")
    assert loader()["agentchat"]["trust"] == "full"
    assert reg.inspect_allowance().via == "trust"
    assert "# the operator's own note" in config_path.read_text(encoding="utf-8")


# ─── the one switch: Hermes' approval gate for the hosted tools ────────────


def test_trust_reads_as_hermes_reads_it() -> None:
    from agentchatme_hermes import mcp_registration as reg

    normalize = _import("tools.mcp_tool_registration")._normalize_server_trust
    for raw in (None, "full", "FULL", " full ", "untrusted", " Untrusted", "", "yes", "trusted", True, False, 1, 0):
        entry: dict[str, Any] = {} if raw is None else {"trust": raw}
        assert reg.effective_trust(reg.entry_trust(entry)) == normalize(raw), raw


def test_approvals_mode_reads_as_hermes_reads_it() -> None:
    from agentchatme_hermes import mcp_registration as reg

    normalize = _import("tools.approval_context")._normalize_approval_mode
    for raw in (False, True, "off", "OFF", " off ", "manual", "smart", "auto", "", None, 0):
        assert reg._approval_mode(raw) == normalize(raw), raw


_GATE_CASES = [
    (trust, mode, yolo)
    for trust in (None, "full", "FULL", "untrusted", "bogus")
    for mode in (None, "off", "manual")
    for yolo in (False, True)
]


def test_the_allowance_is_hermes_gate(_hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """For every combination of the entry's trust, approvals.mode and the
    process --yolo, the plugin's allowance says what Hermes' own call-time
    gate does with an AgentChat write tool: runs it, or asks (a declined
    consent here). Read-only tools run either way."""
    from types import SimpleNamespace

    from agentchatme_hermes import mcp_registration as reg

    handlers = _import("tools.mcp_tool_handlers")
    registration = _import("tools.mcp_tool_registration")
    approval = _import("tools.approval")
    prompt = _import("tools.approval_prompt")
    asked: list[str] = []

    def decline(message: str, description: str, **_kw: Any) -> str:
        asked.append(message)
        return "decline"

    monkeypatch.setattr(prompt, "request_elicitation_consent", decline)
    config_path = _hermes_home / "config.yaml"
    for n, (trust, mode, yolo) in enumerate(_GATE_CASES):
        entry_lines = '    url: "https://api.agentchat.me/mcp"\n    headers:\n      Authorization: "Bearer ${AGENTCHAT_API_KEY}"\n'
        if trust is not None:
            entry_lines += f"    trust: {trust}\n"
        text = "mcp_servers:\n  agentchat:\n" + entry_lines
        if mode is not None:
            text += f"approvals:\n  mode: {mode}\n"  # a bare off: YAML reads False, as a person writes it
        text += f"# case {n:04d}\n"
        config_path.write_text(text, encoding="utf-8")
        os.utime(config_path, ns=(time.time_ns(), time.time_ns() + n * 1_000_000))
        monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", yolo)
        hermes_entry = {"url": "https://api.agentchat.me/mcp"} if trust is None else {"url": "x", "trust": trust}
        registration._record_tool_trust_metadata(
            "agentchat", hermes_entry, [SimpleNamespace(name="agentchat_send_message", annotations=None)]
        )
        asked.clear()
        hermes_runs = handlers._trust_gate_check("agentchat", "agentchat_send_message") is None
        ours = reg.tools_allowance(reg._hermes_raw_config(), environ={"HERMES_YOLO_MODE": "1" if yolo else ""})
        assert ours.allowed is hermes_runs, (trust, mode, yolo, ours)
        assert bool(asked) is not hermes_runs, "a gated call asks (declined here)"


def test_hermes_marks_the_commands_its_terminal_tool_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hermes agentchat`` in an agent's terminal: Hermes puts the session id in
    the process environment of every agent session and in every command its
    terminal tool spawns; a ``hermes`` process sets only the harness markers
    (AI_AGENT, HERMES_AGENT), never a session id."""
    from agentchatme_hermes import allow

    for name in ("HERMES_SESSION_ID", "AI_AGENT", "HERMES_AGENT", "HERMES_DESKTOP_TERMINAL"):
        monkeypatch.delenv(name, raising=False)
    main = _import("hermes_cli.main")
    main._advertise_agent_env()
    assert os.environ.get("HERMES_AGENT") == "true" and "HERMES_SESSION_ID" not in os.environ
    assert allow.in_agent_terminal() is False, "a person's terminal running hermes"

    session_context = _import("gateway.session_context")
    local = _import("tools.environments.local")
    monkeypatch.setenv("HERMES_SESSION_ID", "")

    def in_a_session() -> dict[str, str]:
        session_context.set_current_session_id("contract-session")
        child: dict[str, str] = {}
        local._inject_session_context_env(child)
        return child

    child = contextvars.copy_context().run(in_a_session)
    assert os.environ.get("HERMES_SESSION_ID") == "contract-session"
    assert child.get("HERMES_SESSION_ID") == "contract-session"
    assert allow.in_agent_terminal(child) is True


def test_the_line_reader_tells_the_end_of_input_from_an_empty_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    from agentchatme_hermes import allow

    cli_output = _import("hermes_cli.cli_output")
    assert callable(cli_output.line_input) and callable(cli_output.color) and hasattr(cli_output.Colors, "YELLOW")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(EOFError):
        cli_output.line_input("q? ")
    assert allow.ask_in_terminal("q?") == "end_of_input", "the end of input decides nothing"
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert allow.ask_in_terminal("q?") == "yes", "Enter is a yes"
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    assert allow.ask_in_terminal("q?") == "no"


def test_ask_choice_answers_only_where_someone_can(monkeypatch: pytest.MonkeyPatch) -> None:
    choices = _import("hermes_cli.plugin_choices")
    options = [{"value": "allow", "label": "Allow"}, {"value": "no", "label": "Don't allow"}]
    assert choices.ask_choice("Allow @alice to use AgentChat tools on its own?", options) is None, "no asker: None"
    with choices.bind_asker(lambda question, labels: labels[0]):
        assert choices.ask_choice("q", options) == "allow"
    with choices.bind_asker(lambda question, labels: None):
        assert choices.ask_choice("q", options) is None


_ALLOW_PROBE = """    from agentchatme_hermes.allow import register_allow_command
    from agentchatme_hermes.profile_scope import ProfileScope, resolve_hermes_home
    register_allow_command(ctx, ProfileScope.capture(resolve_hermes_home()))"""


@pytest.mark.parametrize("surface", ["classic_cli", "gateway"])
def test_allow_is_asked_through_the_asker_the_surface_binds(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    """``/agentchat allow`` registered through a real ``PluginContext`` (its
    scope captured in ``register()``, before any asker exists), dispatched as
    Hermes dispatches it: the classic CLI's own ``_run_plugin_slash_command``
    (``bind_asker(clarify_asker(...))`` around ``invoke_plugin_command``), and
    the gateway's (``invoke_plugin_command`` on its executor, the caller's
    context carried by ``_run_in_executor_with_context``). The question
    reaches the surface's asker; the switch is written in the profile's scope.
    Inside that scope Hermes' ``ask_choice`` finds no asker — where the
    question was asked before this fix."""
    import asyncio
    import functools
    import types as _types

    from fake_keys import api_key
    from key_files import save_key

    from agentchatme_hermes import allow
    from agentchatme_hermes import mcp_registration as reg
    from agentchatme_hermes.profile_scope import ProfileScope

    monkeypatch.setattr(allow, "inspect_allowance", reg.inspect_allowance)  # the real file
    monkeypatch.setattr(allow, "gateway_lives", lambda home, *, api_base: False)
    save_key(api_key("contract_" + "0" * 32), handle="alice", home=_hermes_home)
    (_hermes_home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    _probe_plugin(_hermes_home, "agentchat_allow_probe", _ALLOW_PROBE)
    manager = _load_plugins(_hermes_home, routed=False)
    entry = manager._plugin_commands.get("agentchat")
    assert entry is not None, f"registered through Hermes: {sorted(manager._plugin_commands)}"
    handler = entry["handler"]
    choices = _import("hermes_cli.plugin_choices")
    dispatch = _import("hermes_cli.plugin_command_dispatch")
    asked: list[tuple[str, list[str]]] = []

    def clarify(questions: list[dict[str, Any]]) -> dict[str, Any]:
        asked.append((questions[0]["question"], list(questions[0]["choices"])))
        return {"answers": {"q0": "Allow"}, "outcome": "answered"}

    if surface == "classic_cli":
        cli = _import("cli")
        printed: list[str] = []
        monkeypatch.setattr(_import("hermes_cli.gateway_plugin_commands"), "run", lambda *a, **k: None)
        monkeypatch.setattr(_import("hermes_cli.plugins"), "get_plugin_command_handler", lambda name: handler)
        monkeypatch.setattr(cli, "_cprint", lambda text, *a, **k: printed.append(str(text)))
        shell = _types.SimpleNamespace(_clarify_callback=clarify, session_id="cli-session")
        cli.HermesCLI._run_plugin_slash_command(shell, "/agentchat", "allow")
        reply = "\n".join(printed)
    else:
        runner_cls = _import("gateway.run").GatewayRunner
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=1)
        runner = _types.SimpleNamespace(_get_executor=lambda: pool)
        call = functools.partial(
            dispatch.invoke_plugin_command, handler, "allow", source=None, session_id="tg", session_key="tg",
            platform="telegram",
        )

        async def dispatched() -> Any:
            with choices.bind_asker(choices.clarify_asker(clarify)):
                return await runner_cls._run_in_executor_with_context(runner, call)

        try:
            reply = str(asyncio.run(dispatched()))
        finally:
            pool.shutdown(wait=True)
    assert asked == [("Allow @alice to use AgentChat tools on its own?", ["Allow", "Don't allow"])], reply
    assert "AgentChat tools allowed for @alice" in reply
    assert reg.inspect_allowance(api_base=allow._api_base(_hermes_home)).trust == "full"

    # Why it is asked outside the scope: a scope captured before the surface
    # binds its asker (as register() captures it) does not see the asker.
    scope = ProfileScope.capture(_hermes_home)
    with choices.bind_asker(lambda question, labels: labels[0]):
        assert choices.ask_choice("q", ["a"]) == "a"
        assert scope.run(choices.ask_choice, "q", ["a"]) is None


def test_hermes_shows_after_install_md() -> None:
    install = _import("hermes_cli.plugins_cmd_install")
    assert "after-install.md" in inspect.getsource(install._display_after_install)
    root = Path(__file__).resolve().parents[1]
    assert (root / "after-install.md").is_file(), "the clone's root is the plugin directory Hermes reads"


def test_the_gateway_reconciles_mcp_config_every_minute() -> None:
    """What "a running gateway connects them within about a minute" rests on:
    the housekeeping thread's default tick is 60 s and the MCP config
    reconcile runs every tick."""
    run = _import("gateway.run")
    housekeeping = run._start_gateway_housekeeping
    assert inspect.signature(housekeeping).parameters["interval"].default == 60
    assert '(1, "MCP config reconcile", _mcp_config_reconciler(runner))' in inspect.getsource(housekeeping)


# ─── the key: out of the agent's reach, still in the hosted tools' ─────────


_SECRETS_BLOCK = "secrets:\n  agentchat:\n    enabled: true\n    api_key_env: AGENTCHAT_API_KEY\n"


@pytest.fixture
def _source_registry() -> Iterator[Any]:
    """Hermes' secret-source registry, emptied again after the test."""
    registry = _import("agent.secret_sources.registry")
    env_loader = _import("hermes_cli.env_loader")
    try:
        yield registry
    finally:
        registry._reset_registry_for_tests()
        env_loader.reset_secret_source_cache()


def test_the_key_source_is_a_hermes_secret_source(_source_registry: Any) -> None:
    from agentchatme_hermes import key_source

    base = _import("agent.secret_sources.base")
    source = key_source.build_key_source()
    assert isinstance(source, base.SecretSource)
    assert source.api_version == base.SECRET_SOURCE_API_VERSION
    assert _source_registry.register_source(source) is True
    assert callable(getattr(_import("hermes_cli.plugins").PluginContext, "register_secret_source", None))
    assert key_source.source_supported() is True


def test_each_profile_gets_its_own_key_through_hermes_orchestrator(
    _hermes_home: Path, _source_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apply_all`` per profile home, the way a multiplexed gateway hydrates
    each served profile: profile b's hosted tools get b's key, never a's, and
    the saved key beats a stale value already in the environment."""
    from fake_keys import api_key

    from agentchatme_hermes import key_source, key_store

    constants = _import("hermes_constants")
    other = _hermes_home / "profiles" / "b"
    keys = {_hermes_home: api_key("contract_profile_a"), other: api_key("contract_profile_b")}
    for home, key in keys.items():
        key_store.write_store(home, api_key=key, handle="x", api_base="https://api.agentchat.me")
        scope = constants.hermes_home_key(home)
        assert _source_registry.register_source(key_source.build_key_source(), scope=scope) is True
    for home, key in keys.items():
        environ = {"AGENTCHAT_API_KEY": api_key("stale_or_exported")}
        with _home_override(home):
            report = _source_registry.apply_all({"agentchat": key_source.secrets_section()}, home, environ=environ)
        assert environ["AGENTCHAT_API_KEY"] == key, home
        assert [s.name for s in report.sources] == ["agentchat"]


def test_a_cloned_profiles_tools_get_no_key_not_the_originals(
    _hermes_home: Path, _source_registry: Any, _multiplexed: Any
) -> None:
    """A profile whose .env holds a copy of the default profile's key (made by
    ``profile create``) while the default saved it in its key file. The copy is
    found in the clone's own scope — its settings through Hermes' ``get_secret``
    on a multiplexed gateway, never by reading a file — and through Hermes'
    orchestrator the clone's variable then comes out empty, so its hosted tools
    cannot act as the default profile's agent."""
    from fake_keys import api_key

    from agentchatme_hermes import config, key_source, key_store
    from agentchatme_hermes.profile_scope import ProfileScope

    key = api_key("contract_copied_value")
    key_store.write_store(_hermes_home, api_key=key, handle="x", api_base="https://api.agentchat.me")
    clone = _hermes_home / "profiles" / "t5tmp"
    clone.mkdir(parents=True)
    (clone / ".env").write_text(f"AGENTCHAT_API_KEY={key}\n", encoding="utf-8")
    _multiplexed()
    with _home_override(clone):
        scope = ProfileScope.capture(clone)
    resolved = scope.run(config.resolve_key, clone)
    assert resolved.refusal is not None and resolved.copy_of == _hermes_home
    scope_key = _import("hermes_constants").hermes_home_key(clone)
    assert _source_registry.register_source(key_source.build_key_source(), scope=scope_key) is True
    environ = {"AGENTCHAT_API_KEY": key}  # the copy, as Hermes would load it
    with _home_override(clone):
        _source_registry.apply_all({"agentchat": key_source.secrets_section()}, clone, environ=environ)
    assert environ["AGENTCHAT_API_KEY"] == ""


def test_the_startup_path_puts_the_key_where_the_tools_header_reads_it(
    _hermes_home: Path, _source_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the key only in the key file (none in .env), Hermes' own dotenv
    load applies the source and ``Bearer ${AGENTCHAT_API_KEY}`` resolves."""
    from fake_keys import api_key

    from agentchatme_hermes import key_source, key_store

    key = api_key("contract_startup_value")
    # Recorded, so the value Hermes publishes into os.environ is removed afterwards.
    monkeypatch.setenv("AGENTCHAT_API_KEY", "")
    monkeypatch.delenv("AGENTCHAT_API_KEY")
    (_hermes_home / "config.yaml").write_text(_SECRETS_BLOCK, encoding="utf-8")
    key_store.write_store(_hermes_home, api_key=key, handle="x", api_base="https://api.agentchat.me")
    constants = _import("hermes_constants")
    _source_registry.register_source(key_source.build_key_source(), scope=constants.hermes_home_key(_hermes_home))
    env_loader = _import("hermes_cli.env_loader")
    env_loader.reset_secret_source_cache(_hermes_home)
    env_loader.load_hermes_dotenv(hermes_home=_hermes_home)
    assert _import("hermes_cli.config")._expand_env_vars("Bearer ${AGENTCHAT_API_KEY}") == f"Bearer {key}"
    assert not (_hermes_home / ".env").exists() or key not in (_hermes_home / ".env").read_text(encoding="utf-8")


def test_hermes_keeps_the_key_out_of_the_agents_commands(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply turn's shell had AGENTCHAT_API_KEY in its environment (0.5.3).
    With ``secrets.agentchat`` in config.yaml, Hermes' child-environment
    builder drops it (terminal, background processes, script runners) and a
    skill cannot ask for it back; without the block it is passed through."""
    from fake_keys import api_key

    from agentchatme_hermes import cli

    local = _import("tools.environments.local")
    passthrough = _import("tools.env_passthrough")
    probe = api_key("contract_child_env_value")
    monkeypatch.setenv("AGENTCHAT_API_KEY", probe)
    config_path = _hermes_home / "config.yaml"

    config_path.write_text("model:\n  default: stub\n", encoding="utf-8")
    assert local.build_subprocess_env().get("AGENTCHAT_API_KEY") == probe, "without the block: the leak"
    assert cli._hermes_strips_key_from_commands() is False

    config_path.write_text(_SECRETS_BLOCK, encoding="utf-8")
    assert "AGENTCHAT_API_KEY" not in local.build_subprocess_env()
    assert "AGENTCHAT_API_KEY" not in local._sanitize_subprocess_env(dict(os.environ))
    assert passthrough.register_env_passthrough(["AGENTCHAT_API_KEY"]) == ["AGENTCHAT_API_KEY"], "refused"
    assert cli._hermes_strips_key_from_commands() is True


def test_the_secrets_block_round_trips_through_hermes_config(_hermes_home: Path) -> None:
    from agentchatme_hermes import secrets_registration as reg

    config_path = _hermes_home / "config.yaml"
    config_path.write_text("# mine\nsecrets:\n  bitwarden:\n    enabled: false\n", encoding="utf-8")
    assert reg.inspect_block() == "missing"
    assert reg.install_block() == "added"
    text = config_path.read_text(encoding="utf-8")
    assert "# mine" in text and "bitwarden" in text
    assert reg.inspect_block() == "ours" and reg.inspect_source_enabled() is True
    assert reg.install_block() == "present"
    assert reg.uninstall_block() is True
    assert "agentchat" not in (_import("hermes_cli.config").read_user_config_raw().get("secrets") or {})


def test_hermes_env_routes_for_this_profile(_hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the key migration relies on (``credentials``): Hermes loads the
    profile .env over an exported value, so a key setting with a .env line
    reads as that line; ``remove_env_value`` removes every line of a name,
    clears it from the environment and says whether a line was there; its
    writers edit the active profile's .env (``get_env_path``); its write lock
    can be asked first (``require_env_writable``)."""
    from fake_keys import api_key

    config = _import("hermes_cli.config")
    env_loader = _import("hermes_cli.env_loader")
    for name in ("remove_env_value", "save_env_value", "get_env_path", "require_env_writable"):
        assert callable(getattr(config, name, None)), name
    assert Path(config.get_env_path()) == _hermes_home / ".env"
    other = _hermes_home / "profiles" / "b"
    other.mkdir(parents=True)
    with _home_override(other):
        assert Path(config.get_env_path()) == other / ".env", "the profile in scope"

    line = api_key("contract_env_line")
    (_hermes_home / ".env").write_text(f"KEEP=1\nAGENTCHAT_API_KEY={line}\nAGENTCHAT_API_KEY={line}\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("contract_exported"))
    env_loader.load_hermes_dotenv(hermes_home=_hermes_home)
    assert os.environ["AGENTCHAT_API_KEY"] == line, "the profile .env wins over the shell's export"
    config.require_env_writable("AGENTCHAT_API_KEY", "remove")
    assert config.remove_env_value("AGENTCHAT_API_KEY") is True
    assert "AGENTCHAT_API_KEY" not in os.environ
    assert (_hermes_home / ".env").read_text(encoding="utf-8").splitlines() == ["KEEP=1"]
    assert config.remove_env_value("AGENTCHAT_API_KEY") is False, "no line: nothing found"


def test_wizard_helpers() -> None:
    setup = _import("hermes_cli.setup")
    for name in ("get_env_value", "print_header", "print_info", "print_success", "print_warning", "prompt_choice", "save_env_value"):
        assert callable(getattr(setup, name, None)), name
    output = _import("hermes_cli.cli_output")
    assert callable(output.prompt) and callable(output.prompt_yes_no)
    # The wizard reads a pasted API key with Hermes' masked prompt.
    import inspect

    assert "password" in inspect.signature(output.prompt).parameters


# ─── profile scope ─────────────────────────────────────────────────────────


def test_profile_scope_pieces() -> None:
    """Every Hermes name ``ProfileScope`` uses is a public one (``plugin_api.json``)."""
    constants = _import("hermes_constants")
    for name in ("get_hermes_home", "set_hermes_home_override", "reset_hermes_home_override", "hermes_home_key"):
        assert callable(getattr(constants, name, None)), name
    secret_scope = _import("agent.secret_scope")
    for name in ("get_secret", "is_multiplex_active", "set_secret_scope", "reset_secret_scope", "refresh_installed_secret_scope"):
        assert callable(getattr(secret_scope, name, None)), name
    assert issubclass(secret_scope.UnscopedSecretError, Exception)
    spec = importlib.util.find_spec("hermes_cli")
    assert spec is not None and spec.submodule_search_locations
    api = json.loads((Path(next(iter(spec.submodule_search_locations))) / "data" / "plugin_api.json").read_text(encoding="utf-8"))
    public = {entry for group in api["groups"] for entry in group["entries"]}
    assert {"agent.secret_scope", "hermes_constants", "hermes_cli.config", "agent.secret_sources.base"} <= public


def test_the_profile_home_reaches_a_turn_thread(tmp_path: Path) -> None:
    """The turn runner starts its thread in a copy of the caller's context;
    Hermes' HERMES_HOME override must be a ContextVar for that to carry it."""
    constants = _import("hermes_constants")
    other = tmp_path / "profiles" / "b"
    other.mkdir(parents=True)
    token = constants.set_hermes_home_override(str(other))
    seen: list[Path] = []
    try:
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(lambda: seen.append(Path(constants.get_hermes_home())),))
        thread.start()
        thread.join(5.0)
    finally:
        constants.reset_hermes_home_override(token)
    assert seen and seen[0].resolve() == other.resolve()


def test_the_plugin_resolves_its_home_through_hermes(tmp_path: Path) -> None:
    """A scope captured where Hermes bound profile c's home runs every call there."""
    from agentchatme_hermes.profile_scope import ProfileScope, resolve_hermes_home

    other = tmp_path / "profiles" / "c"
    other.mkdir(parents=True)
    with _home_override(other):
        scope = ProfileScope.capture(other)
    assert resolve_hermes_home().resolve() != other.resolve()
    assert _in_a_worker(lambda: scope.run(resolve_hermes_home)).resolve() == other.resolve()


@pytest.fixture
def _multiplexed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Make this process a multiplexed gateway, as Hermes does when it starts
    hosting a second profile home (``activate_multi_profile_hosting``: the
    environment frozen as the launch profile's, ``get_secret`` fail-closed).
    Undone afterwards."""
    policy = _import("tui_gateway.launch_profile_policy")
    secret_scope = _import("agent.secret_scope")
    monkeypatch.setattr(policy, "_snapshot", None)
    monkeypatch.setattr(policy, "_source_writes", {})
    active: list[bool] = []

    def activate() -> None:
        policy.activate_multi_profile_hosting()
        active.append(True)

    try:
        yield activate
    finally:
        if active:
            secret_scope.set_multiplex_active(False)


def _probe_plugin(home: Path, name: str, body: str) -> None:
    """A directory plugin in ``home``, enabled there, whose ``register(ctx)`` runs ``body``."""
    plugin = home / "plugins" / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text(f"name: {name}\nversion: 0.1.0\ndescription: contract probe\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(f"def register(ctx):\n{body}\n", encoding="utf-8")
    config = home / "config.yaml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if "plugins:" not in text:
        text += "plugins:\n  enabled: []\n"
    text = text.replace("  enabled: [", f"  enabled: [{name}, ", 1).replace(", ]", "]")
    config.write_text(text, encoding="utf-8")


_CAPTURE = """    import sys
    from agentchatme_hermes.profile_scope import ProfileScope, resolve_hermes_home
    home = resolve_hermes_home()
    sys.modules["agentchat_contract_probe"].captured[str(home)] = ProfileScope.capture(home)"""


@pytest.fixture
def _probe_captures(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import types as _types

    module = _types.ModuleType("agentchat_contract_probe")
    module.captured = {}  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "agentchat_contract_probe", module)
    return module.captured  # type: ignore[attr-defined,no-any-return]


def _load_plugins(home: Path, *, routed: bool) -> Any:
    """Load ``home``'s plugins as a multiplexed gateway does, on a thread of its
    own: the launch profile by a plain discovery (the boot's background
    thread: only the home is bound), a served profile inside the runtime
    scope the gateway binds for it (``gateway.run._profile_runtime_scope``)."""
    plugins = _import("hermes_cli.plugins")
    constants = _import("hermes_constants")
    # As the release's own ``get_plugin_manager`` builds a profile's manager:
    # with the home it was named by since v0.23.4 (``owner_home``), from the
    # scope key alone before.
    owner = {"owner_home": home} if "owner_home" in _params(plugins.PluginManager) else {}
    manager = plugins.PluginManager(scope_key=constants.hermes_home_key(home), **owner)

    def load() -> None:
        if routed:
            with _import("gateway.run")._profile_runtime_scope(home):
                manager.discover_and_load()
        else:
            manager.discover_and_load()

    thread = threading.Thread(target=contextvars.Context().run, args=(load,))
    thread.start()
    thread.join(60.0)
    return manager


def _two_profiles(root: Path, monkeypatch: pytest.MonkeyPatch, *, model_base: str | None = None) -> Path:
    """The default profile (the launch profile) and profile ``s``, each with its
    own settings in its own .env; one key only in the launch environment (as
    systemd ``Environment=`` sets it)."""
    served = root / "profiles" / "s"
    served.mkdir(parents=True)
    (root / ".env").write_text("AGENTCHATME_HANDLE=root-h\nCONTRACT_PROVIDER_KEY=root-file\n", encoding="utf-8")
    (served / ".env").write_text("AGENTCHATME_HANDLE=s-h\nCONTRACT_PROVIDER_KEY=s-key\n", encoding="utf-8")
    monkeypatch.setenv("CONTRACT_ENV_ONLY_KEY", "launch-only")
    monkeypatch.delenv("AGENTCHATME_HANDLE", raising=False)
    monkeypatch.delenv("CONTRACT_PROVIDER_KEY", raising=False)
    if model_base is not None:
        for home, ref in ((root, "CONTRACT_ENV_ONLY_KEY"), (served, "CONTRACT_PROVIDER_KEY")):
            (home / "config.yaml").write_text(
                f"model:\n  default: contract-main-model\n  provider: custom\n  base_url: {model_base}\n"
                f"  api_key: ${{{ref}}}\n",
                encoding="utf-8",
            )
    for home in (root, served):
        _probe_plugin(home, "agentchat_scope_probe", _CAPTURE)
    return served


def test_profile_scope_on_a_real_two_profile_multiplexed_gateway(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch, _multiplexed: Any, _probe_captures: dict[str, Any]
) -> None:
    """Each profile's plugin work, run in the scope ``register()`` captured, sees
    that profile's home and secrets — bound per call with public names only
    (``set_secret_scope`` + ``refresh_installed_secret_scope``):

    * the launch profile its own .env AND the keys only its process
      environment has (``owner_secret_scope`` → the environment frozen at
      activation), never the served profile's;
    * the served profile its own .env only — never a launch-only key — and the
      terminal policy Hermes bound for it when it loaded the plugin;
    * nothing left bound after a call: an unscoped read still fails closed.
    """
    from agentchatme_hermes.profile_scope import resolve_setting

    root = _hermes_home
    served = _two_profiles(root, monkeypatch)
    _multiplexed()
    _load_plugins(root, routed=False)
    _load_plugins(served, routed=True)
    key = _import("hermes_constants").hermes_home_key
    scopes = {key(Path(home)): scope for home, scope in _probe_captures.items()}
    assert set(scopes) == {key(root), key(served)}, f"the probe loaded in both profiles: {list(_probe_captures)}"
    secret_scope = _import("agent.secret_scope")
    terminal_scope = _import("tools.terminal_scope")

    def probe() -> dict[str, Any]:
        get = secret_scope.get_secret
        return {
            "home": key(_import("hermes_constants").get_hermes_home()),
            "handle": resolve_setting("AGENTCHATME_HANDLE"),
            "provider": get("CONTRACT_PROVIDER_KEY"),
            "env_only": get("CONTRACT_ENV_ONLY_KEY"),
            "terminal": terminal_scope.get_terminal_scope() is not None,
        }

    seen: dict[str, dict[str, Any]] = {}
    barrier = threading.Barrier(2, timeout=30.0)

    def run(home_key: str) -> None:
        barrier.wait()
        seen[home_key] = scopes[home_key].run(probe)

    threads = [threading.Thread(target=run, args=(k,)) for k in scopes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60.0)
    assert seen[key(root)] == {
        "home": key(root), "handle": "root-h", "provider": "root-file", "env_only": "launch-only",
        "terminal": seen[key(root)]["terminal"],
    }
    assert seen[key(served)] == {
        "home": key(served), "handle": "s-h", "provider": "s-key", "env_only": None, "terminal": True,
    }
    with pytest.raises(secret_scope.UnscopedSecretError):
        _in_a_worker(lambda: secret_scope.get_secret("CONTRACT_PROVIDER_KEY"))
    left = _in_a_worker(lambda: (scopes[key(served)].run(lambda: None), secret_scope.current_secret_scope())[1])
    assert left is None, "the call's binding is reset"


def test_the_launch_profile_loaded_before_multiplexing_gets_its_scope_once_it_starts(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch, _multiplexed: Any, _probe_captures: dict[str, Any]
) -> None:
    """A gateway loads the launch profile's plugins before it starts hosting a
    second profile (the eager activation is its last boot step). The scope
    captured then binds the launch profile's secrets — its environment-only key
    included — once the gateway multiplexes."""
    root = _hermes_home
    _two_profiles(root, monkeypatch)
    _load_plugins(root, routed=False)  # before activation: single-profile semantics
    key = _import("hermes_constants").hermes_home_key
    (scope,) = [s for home, s in _probe_captures.items() if key(Path(home)) == key(root)]
    secret_scope = _import("agent.secret_scope")
    assert not secret_scope.is_multiplex_active()
    _multiplexed()
    seen = _in_a_worker(
        lambda: scope.run(lambda: (secret_scope.get_secret("AGENTCHATME_HANDLE"), secret_scope.get_secret("CONTRACT_ENV_ONLY_KEY")))
    )
    assert seen == ("root-h", "launch-only")


def test_the_refresh_contract_the_version_probe_reads() -> None:
    """``profile_scope.refresh_keeps_launch_env`` asks Hermes' public refresh
    whether it refills a scope stamped for another home: since 0.23.2 it does
    not (it builds through ``owner_secret_scope``), so this Hermes keeps the
    launch profile's environment-only keys and no warning is said."""
    from agentchatme_hermes import profile_scope

    profile_scope._keeps_launch_env.clear()
    assert profile_scope.refresh_keeps_launch_env() is True


def test_ctx_llm_in_each_profiles_scope_uses_its_own_key(
    _hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _multiplexed: Any,
    _probe_captures: dict[str, Any],
    answering_model: _Stub,
) -> None:
    """The gate's ``ctx.llm`` (``PluginLlm``) resolves the calling thread's
    profile: in each profile's scope it calls that profile's model with that
    profile's key — the launch profile's from its environment only."""
    from agentchatme_hermes.model_call import build_call_model

    root = _hermes_home
    served = _two_profiles(root, monkeypatch, model_base=answering_model.base_url)
    _multiplexed()
    _load_plugins(root, routed=False)
    _load_plugins(served, routed=True)
    key = _import("hermes_constants").hermes_home_key
    scopes = {key(Path(home)): scope for home, scope in _probe_captures.items()}
    plugin_llm = _import("agent.plugin_llm")
    for home, bearer in ((root, "Bearer launch-only"), (served, "Bearer s-key")):
        call_model = build_call_model(
            main_runtime={}, timeout_s=20.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat_scope_probe")
        )
        assert _in_a_worker(lambda s=scopes[key(home)], c=call_model: s.run(c, "THE PROMPT")) == "done"
        assert answering_model.auth[-1] == bearer, home


def test_a_plugin_loaded_after_ours_is_visible_from_our_scope(
    _hermes_home: Path, _probe_captures: dict[str, Any]
) -> None:
    """The context ``register()`` captures carries Hermes' plugin-load flag
    (``in_plugin_load_worker``): a discovery from our threads does not wait for
    a sweep still running. Once the sweep is done, what a plugin loaded after
    ours registered is visible from our scope, as a turn needs it."""
    _probe_plugin(_hermes_home, "aaa_agentchat_probe", _CAPTURE)
    _probe_plugin(
        _hermes_home,
        "zzz_late_probe",
        "    ctx.register_tool(name='zzz_late_probe_tool', toolset='zzz_late_probe', "
        "schema={'name': 'zzz_late_probe_tool', 'description': 'probe', 'parameters': {'type': 'object', 'properties': {}}}, "
        "handler=lambda args, **kw: '{}')",
    )
    manager = _load_plugins(_hermes_home, routed=False)
    try:
        (scope,) = _probe_captures.values()
        registry = _import("tools.registry").registry
        loader = _import("hermes_cli.plugins_loader")

        def look() -> tuple[bool, bool]:
            names = registry.get_all_tool_names()
            return "zzz_late_probe_tool" in names, loader.in_plugin_load_worker()

        visible, _in_load_flag = _in_a_worker(lambda: scope.run(look))
        assert visible, "the later plugin's tool is in the registry our turn reads"
    finally:
        manager.unload()


# ─── hosted tools and names ─────────────────────────────────────────────────


def test_tool_registry_and_mcp_names() -> None:
    registry = _import("tools.registry").registry
    assert callable(registry.get_tool_names_for_toolset) and callable(registry.get_all_tool_names)
    from agentchatme_hermes.hosted_tools import hosted_tool_names

    assert hosted_tool_names() == [], "Hermes is here and no agentchat MCP server is connected"
    assert _import("tools.mcp_tool_schema").MCP_TOOL_NAME_PREFIX == "mcp__"


def test_hosted_tool_result_wrapper() -> None:
    """``count_agentchat_calls`` recognises a hosted result by this wrapper."""
    source = Path(_import("agent.tool_dispatch_helpers").__file__).read_text(encoding="utf-8")
    assert '<untrusted_tool_result source="{name}">' in source


def test_compaction_marks_what_it_writes() -> None:
    """``count_agentchat_calls`` returns ``None`` (unknown) for a transcript
    carrying these keys: earlier tool calls may be gone from it."""
    from agentchatme_hermes.turn_runner import _COMPACTION_KEYS

    compressor = _import("agent.context_compressor")
    assert (compressor.COMPRESSED_SUMMARY_METADATA_KEY, compressor.MICRO_COMPACT_MARKER_KEY) == _COMPACTION_KEYS


def test_hosted_tool_results_have_the_counted_shapes() -> None:
    """A hosted call succeeded only as ``{"result": …}``; an ``isError``
    result is ``{"error": …}`` (``count_agentchat_calls``)."""
    import json

    assert json.loads(_import("tools.registry").tool_error("AWAITING_REPLY")) == {"error": "AWAITING_REPLY"}
    source = Path(_import("tools.mcp_tool_handlers").__file__).read_text(encoding="utf-8")
    assert 'json.dumps({"result": text_result}' in source
    assert 'mcp_field(result, "is_error", "isError", False)' in source


def test_a_large_hosted_result_hermes_persists_counts_as_a_success(_hermes_home: Path) -> None:
    """Hermes keeps an oversized result out of the transcript
    (``maybe_persist_tool_result``: 50K chars for MCP tools, less on small
    context windows) and leaves a ``<persisted-output>`` preview, wrapped as
    untrusted data like any hosted result; the turn budget can persist a
    whole tool message, an error too."""
    import json

    storage = _import("tools.tool_result_storage")
    wrap = _import("agent.tool_dispatch_helpers")._maybe_wrap_untrusted
    name = "mcp__agentchat__agentchat_get_conversation"
    assert (storage.PERSISTED_OUTPUT_TAG, storage.PERSISTED_OUTPUT_CLOSING_TAG) == (
        turn_runner.PERSISTED_OUTPUT_OPEN,
        turn_runner.PERSISTED_OUTPUT_CLOSE,
    )
    big = json.dumps({"result": json.dumps({"messages": [{"id": f"msg_{i}", "text": "x" * 200} for i in range(400)]})})
    persisted = storage.maybe_persist_tool_result(content=big, tool_name=name, tool_use_id="call_big")
    assert persisted.startswith(storage.PERSISTED_OUTPUT_TAG) and turn_runner.PERSISTED_PREVIEW_LABEL in persisted
    error = _import("tools.registry").tool_error("AWAITING_REPLY: wait for their reply")
    whole_error = storage.maybe_persist_tool_result(content=wrap(name, error), tool_name=name, tool_use_id="call_err", threshold=0)
    assert whole_error.startswith(storage.PERSISTED_OUTPUT_TAG)
    messages = [
        {"role": "tool", "tool_call_id": "call_big", "name": name, "content": wrap(name, persisted)},
        {"role": "tool", "tool_call_id": "call_err", "name": name, "content": whole_error},
    ]
    calls = turn_runner.count_agentchat_calls({"messages": messages})
    assert calls is not None and (calls.made, calls.failed) == (2, 1)
    source = Path(storage.__file__).read_text(encoding="utf-8")
    assert turn_runner.INLINE_TRUNCATED_NOTE.strip("\n") in source, "the fallback when the file cannot be saved"


def test_gateway_marks_its_process() -> None:
    """``leader_lock.is_hermes_gateway_process`` reads the marker gateway/run.py sets."""
    spec = importlib.util.find_spec("gateway.run")
    assert spec is not None and spec.origin is not None
    assert "_HERMES_GATEWAY" in Path(spec.origin).read_text(encoding="utf-8")


# ─── a parked hosted tools server (MS-F19) ──────────────────────────────────


def test_hermes_mcp_status_surface_and_the_parked_remedy() -> None:
    """``hosted_tools`` reads ``get_mcp_status()`` rows (read only, diagnostics).
    Its remedy for a parked server is true of this Hermes: a parked server
    probes itself after 300 s — again every 300 s on v0.23.2, backing off to
    an hour since v0.23.3 — and ``/reload-mcp`` is a command a chat on the
    gateway can send (which chats it reconnects a parked server from:
    :func:`test_reload_mcp_reconnects_a_parked_server_only_from_a_messaging_chat`)."""
    from agentchatme_hermes import hosted_tools

    discovery = _import("tools.mcp_tool_discovery")
    assert {"configured"} <= _params(discovery.get_mcp_status)
    mcp_tool = _import("tools.mcp_tool")
    assert mcp_tool._PARKED_RETRY_INTERVAL == 300
    remedy = hosted_tools.PARKED_REMEDY
    assert "after 5 minutes" in remedy and "`hermes gateway restart`" in remedy
    assert "/reload-mcp in a messaging chat" in remedy and "not in Desktop or a terminal chat" in remedy
    if hasattr(mcp_tool, "_PARKED_PROBE_BACKOFF_MAX"):
        assert mcp_tool._PARKED_PROBE_BACKOFF_MAX == 3600
        assert "up to an hour" in remedy
    else:
        run = _import("tools.mcp_tool_server_run")
        assert "timeout=_core._PARKED_RETRY_INTERVAL" in inspect.getsource(run), "a fixed 5-minute probe"
        assert "every 5 minutes on Hermes v0.23.2" in remedy
    command = _import("hermes_cli.commands").resolve_command("reload-mcp")
    assert command is not None and not getattr(command, "cli_only", False)


class _McpStub:
    """A Streamable HTTP MCP server on localhost: ``initialize``, ``tools/list``
    with one ``agentchat_inbox`` tool, ``ping``. JSON answers, no SSE stream
    (``GET`` is 405, which the client allows)."""

    def __init__(self, port: int, tools: tuple[str, ...] = ("agentchat_inbox",)) -> None:
        stub = self
        self.methods: list[str] = []
        self.tools = tools
        # Set: every request is answered 503 and the connection closed (the
        # server went away for a live client: its keepalive fails).
        self.down = False

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                if body is not None:
                    self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_HEAD(self) -> None:
                self._send(405)

            def do_GET(self) -> None:
                self._send(405)

            def do_DELETE(self) -> None:
                self._send(200)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                message = json.loads(self.rfile.read(length) or b"{}")
                method = message.get("method")
                stub.methods.append(str(method))
                if stub.down:
                    self.close_connection = True
                    self._send(503, {"error": "down"}, {"connection": "close"})
                    return
                if "id" not in message:
                    self._send(202)
                    return
                if method == "initialize":
                    result: Any = {
                        "protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "agentchat-stub", "version": "1"},
                    }
                    self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, {"mcp-session-id": "stub"})
                    return
                if method == "tools/list":
                    listed = [
                        {"name": name, "description": "A hosted tool.", "inputSchema": {"type": "object", "properties": {}}}
                        for name in stub.tools
                    ]
                    self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": listed}})
                    return
                if method == "ping":
                    self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": {}})
                    return
                self._send(200, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "not here"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="mcp-stub", daemon=True)

    def __enter__(self) -> _McpStub:
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_parked_hosted_tools_server_is_held_and_named(_hermes_home: Path) -> None:
    """The gateway starts while the hosted tools server cannot be reached:
    Hermes' own MCP client tries three times and parks it (tools dropped).
    The plugin reads that state, holds the turn, and names the cause and the
    remedy; it asks Hermes for no reconnect (there is no public one)."""
    pytest.importorskip("mcp.client.streamable_http")
    from agentchatme_hermes import hosted_tools

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    port = _free_port()
    (_hermes_home / "config.yaml").write_text(
        f"mcp_servers:\n  agentchat:\n    url: http://127.0.0.1:{port}/mcp\n    connect_timeout: 30\n",
        encoding="utf-8",
    )
    try:
        discovery.discover_mcp_tools()  # nothing listens: three refused attempts, then parked
        state = hosted_tools.hosted_server_state()
        assert state is not None and state.parked, f"Hermes parked the server: {state}"
        assert hosted_tools.hosted_tool_names() == []
        cause = hosted_tools.describe_cause(state)
        assert cause.startswith("Hermes parked mcp_servers.agentchat") and "/reload-mcp" in cause
        assert hosted_tools.wait_for_hosted_tools(timeout_s=0.2, poll_s=0.05) is False
    finally:
        lifecycle.shutdown_mcp_servers()



def _park(home: Path, port: int) -> Any:
    """``mcp_servers.agentchat`` on ``port``, where nothing listens: Hermes'
    discovery tries three times and parks it. Its state."""
    from agentchatme_hermes import hosted_tools

    (home / "config.yaml").write_text(
        f"mcp_servers:\n  agentchat:\n    url: http://127.0.0.1:{port}/mcp\n    connect_timeout: 30\n",
        encoding="utf-8",
    )
    _import("tools.mcp_tool_discovery").discover_mcp_tools()
    state = hosted_tools.hosted_server_state()
    assert state is not None and state.parked, f"Hermes parked the server: {state}"
    return state


def test_reload_mcp_reconnects_a_parked_server_only_from_a_messaging_chat(_hermes_home: Path) -> None:
    """Lab R3 (L1-F07): ``PARKED_REMEDY`` names ``/reload-mcp`` only for a
    messaging chat. Both of the gateway's reload paths, run for real against a
    parked server that answers again (the network is back):

    * a messaging chat (``/reload-mcp`` in Telegram, Discord, …) runs
      ``GatewayTurnMixin._execute_mcp_reload``: the profile's servers torn down
      and discovered again — the parked server is connected;
    * Desktop and a terminal chat on the gateway send the ``reload_mcp``
      request (``gateway/session_ancillary.py``), a config reconcile
      (``reconcile_mcp_servers_with_config``): the parked server stays parked,
      so Hermes refuses the reload (``mcp_servers_unavailable``).

    When either changes, ``PARKED_REMEDY`` and the README / RUNBOOK change."""
    pytest.importorskip("mcp.client.streamable_http")
    import asyncio
    from types import SimpleNamespace

    from agentchatme_hermes import hosted_tools

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    ancillary = _import("gateway.session_ancillary")
    source = inspect.getsource(ancillary.reload_mcp)
    assert "reconcile_mcp_servers_with_config()" in source and "mcp_servers_unavailable" in source
    assert "discover_mcp_tools" not in source and "shutdown_mcp_servers" not in source

    # Desktop / a terminal chat: the reconcile, with the server answering again.
    port = _free_port()
    try:
        _park(_hermes_home, port)
        with _McpStub(port):
            result = discovery.reconcile_mcp_servers_with_config()
            assert not result.get("added") and not result.get("removed"), result
            time.sleep(1.0)
            state = hosted_tools.hosted_server_state()
            assert state is not None and state.parked, f"the reconcile left it parked: {state}"
            assert "agentchat" in discovery.mcp_servers_not_connected(), "so the request is refused"
            assert hosted_tools.hosted_tool_names() == []
    finally:
        lifecycle.shutdown_mcp_servers()

    # A messaging chat: the gateway's own reload, the server answering again.
    run_turn = _import("gateway.run_turn")

    async def in_executor(fn: Any) -> Any:
        return await asyncio.to_thread(fn)

    class _Sessions:
        async def get_or_create_session(self, _source: Any) -> Any:
            raise RuntimeError("no session store here")  # the transcript note is best effort

    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        _run_in_executor_with_context=in_executor,
        _mcp_reload_refresh_cached_agents=lambda *_a: None,
        async_session_store=_Sessions(),
    )
    port = _free_port()
    try:
        _park(_hermes_home, port)
        with _McpStub(port):
            said = asyncio.run(
                run_turn.GatewayTurnMixin._execute_mcp_reload(runner, SimpleNamespace(source=SimpleNamespace(profile=None)))
            )
            state = hosted_tools.hosted_server_state()
            assert state is not None and state.status == "connected", f"{state}: {said}"
            assert hosted_tools.hosted_tool_names() == ["mcp__agentchat__agentchat_inbox"]
    finally:
        lifecycle.shutdown_mcp_servers()


# ─── the one switch on a real connection ───────────────────────────────────


def _gate(name: str = "agentchat_send_message") -> bool:
    """Whether Hermes' call-time gate runs the write tool without asking."""
    return _import("tools.mcp_tool_handlers")._trust_gate_check("agentchat", name) is None


def test_a_new_entry_connects_with_the_trust_it_was_written_with(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's config reconcile (what its housekeeping runs every
    minute) connects an entry added after start with the tier it was written
    with: a yes needs no /reload-mcp when there was no entry."""
    pytest.importorskip("mcp.client.streamable_http")
    from agentchatme_hermes import hosted_tools
    from agentchatme_hermes import mcp_registration as reg

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    monkeypatch.setattr(_import("tools.approval_prompt"), "request_elicitation_consent", lambda *a, **k: "decline")
    from fake_keys import api_key

    monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("contract_" + "0" * 32))
    port = _free_port()
    api_base = f"http://127.0.0.1:{port}"
    (_hermes_home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    try:
        with _McpStub(port, tools=("agentchat_inbox", "agentchat_send_message")):
            discovery.discover_mcp_tools()  # gateway start: nothing to connect
            assert hosted_tools.hosted_tool_names() == []
            assert reg.install_entry(api_base=api_base, decision="yes").outcome == "added"
            assert discovery.reconcile_mcp_servers_with_config()["added"] == ["agentchat"]
            assert hosted_tools.wait_for_hosted_tools(timeout_s=30.0) is True
            assert _gate() is True, "connected full: the reply tool runs without asking"
    finally:
        lifecycle.shutdown_mcp_servers()


def _eventually(check: Any, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return bool(check())


def test_a_yes_over_a_connected_untrusted_server_is_applied_in_two_steps(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no, connected untrusted; then a yes while the gateway runs. Hermes
    keeps a connected server's tier and its config reconcile never re-reads
    it, so the plugin removes the entry (the reconcile, one housekeeping tick,
    drops the server), and the gateway's second step — waiting on its own
    thread — writes it back full only once Hermes runs no agentchat server for
    the profile (its own status and reconnect state for the name, not "no
    tools registered") and a watcher tick plus margin has passed since the
    removal; the next tick connects it fresh, with the new tier: 90 to 150 s.
    The real discovery path, a localhost server."""
    pytest.importorskip("mcp.client.streamable_http")
    from fake_keys import api_key

    from agentchatme_hermes import allow, hosted_tools, placement
    from agentchatme_hermes import mcp_registration as reg

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    monkeypatch.setattr(_import("tools.approval_prompt"), "request_elicitation_consent", lambda *a, **k: "decline")
    monkeypatch.setattr(allow, "inspect_allowance", reg.inspect_allowance)  # the real file, not the unit suite's stand-in
    monkeypatch.setattr(allow, "gateway_lives", lambda home, *, api_base: True)  # this process is the gateway
    monkeypatch.setattr(allow, "POLL_S", 0.05)
    monkeypatch.setattr(allow, "TICK_WAIT_S", 1.0)  # the watcher's tick is driven by hand here
    monkeypatch.setattr(placement, "resolve_hermes_home", lambda: _hermes_home)
    monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("contract_" + "0" * 32))
    port = _free_port()
    api_base = f"http://127.0.0.1:{port}"
    (_hermes_home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    worker: threading.Thread | None = None
    try:
        with _McpStub(port, tools=("agentchat_inbox", "agentchat_send_message")):
            assert placement.place_mcp(api_base, decision="no").outcome == "added"
            discovery.discover_mcp_tools()
            assert hosted_tools.wait_for_hosted_tools(timeout_s=30.0) is True
            assert _gate() is False, "connected untrusted: the write asks"
            assert allow.read_switch(_hermes_home, api_base=api_base).state == "not_allowed"

            # A yes: step one.
            step = placement.place_mcp(api_base, decision="yes")
            assert step.outcome == "applying"
            assert reg.inspect_entry(api_base=api_base) == "missing"
            assert allow.read_switch(_hermes_home, api_base=api_base).state == "applying"
            assert _gate() is False, "until the gateway connects it again, Hermes keeps the old tier"
            assert allow.server_runtime(api_base) == "held"

            # Step two, the gateway's, on its own thread: nothing written while Hermes runs the server.
            done: list[bool] = []
            worker = threading.Thread(
                target=lambda: done.append(allow.complete_pending(_hermes_home, api_base=api_base, wait=True))
            )
            worker.start()
            time.sleep(0.5)
            assert reg.inspect_entry(api_base=api_base) == "missing", "never written while the old connection lives"

            # Hermes' watcher, one tick: the removed server is dropped; step two writes trust: full.
            assert discovery.reconcile_mcp_servers_with_config()["removed"] == ["agentchat"]
            assert allow.server_runtime(api_base) == "gone"
            assert _eventually(lambda: reg.inspect_entry(api_base=api_base) == "ours")
            assert reg.inspect_allowance(api_base=api_base).trust == "full"

            # The next tick: connected fresh, with the new tier; step two ends once the tools are back.
            assert discovery.reconcile_mcp_servers_with_config()["added"] == ["agentchat"]
            worker.join(30.0)
            assert done == [True]
            record = allow.read_record(_hermes_home)
            assert record is not None and record["pending"] is False and record["decision"] == "yes"
            assert hosted_tools.wait_for_hosted_tools(timeout_s=30.0) is True
            assert _gate() is True
            assert allow.read_switch(_hermes_home, api_base=api_base).state == "allowed"
    finally:
        if worker is not None:
            allow.clear_pending(_hermes_home)
            worker.join(10.0)
        lifecycle.shutdown_mcp_servers()


def test_a_parked_server_is_waited_for_like_a_connected_one(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked server has no tools registered, but Hermes still runs it (it
    probes itself and would come back with its old tier). The second step
    never reads it as dropped: this one parked on its first connect and keeps
    that error, which Hermes' status shows like a first connect that left
    nothing, so the reading is "cannot tell" and the wait is one watcher tick
    plus margin — by when the tick tore it down, as here."""
    pytest.importorskip("mcp.client.streamable_http")
    from fake_keys import api_key

    from agentchatme_hermes import allow, hosted_tools
    from agentchatme_hermes import mcp_registration as reg

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("contract_" + "0" * 32))
    api_base = f"http://127.0.0.1:{_free_port()}"  # nothing listens
    (_hermes_home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    try:
        assert reg.install_entry(api_base=api_base, decision="no").outcome == "added"
        discovery.discover_mcp_tools()  # three refused attempts, then parked
        state = hosted_tools.hosted_server_state()
        assert state is not None and state.parked, state
        assert hosted_tools.hosted_tool_names() == [], "no tools: what the earlier wait took for dropped"
        assert reg.uninstall_entry(api_base=api_base)
        assert allow.server_runtime(api_base) in ("held", "unknown"), "never read as dropped"
        assert discovery.reconcile_mcp_servers_with_config()["removed"] == ["agentchat"]
        assert allow.server_runtime(api_base) == "gone"
        assert allow.TICK_WAIT_S > 60, "one housekeeping tick (contract-tested: 60 s) plus margin"
    finally:
        lifecycle.shutdown_mcp_servers()


def test_a_server_between_reconnects_is_not_taken_for_dropped(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live connection whose server goes away: Hermes' keepalive fails and
    the task reconnects with the config it began with (session gone, not
    parked, not connecting, no connect error) — its status reads
    ``configured``, as if dropped. Its reconnect state says it is not: held
    until the watcher's tick tears it down. The real transport, a localhost
    server that starts answering 503."""
    pytest.importorskip("mcp.client.streamable_http")
    from agentchatme_hermes import allow, hosted_tools

    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    monkeypatch.setattr(_import("tools.approval_prompt"), "request_elicitation_consent", lambda *a, **k: "decline")
    assert callable(getattr(discovery, "mcp_server_reconnecting", None)), "the guarded read is there"
    port = _free_port()
    api_base = f"http://127.0.0.1:{port}"
    entry = f"mcp_servers:\n  agentchat:\n    url: {api_base}/mcp\n    trust: untrusted\n    keepalive_interval: 5\n"
    (_hermes_home / "config.yaml").write_text("model:\n  default: x\n" + entry, encoding="utf-8")
    try:
        with _McpStub(port) as stub:
            discovery.discover_mcp_tools()
            assert hosted_tools.wait_for_hosted_tools(timeout_s=30.0) is True
            assert allow.server_runtime(api_base) == "held"
            stub.down = True  # the next keepalive fails: Hermes reconnects

            def mid_reconnect() -> bool:
                state = hosted_tools.hosted_server_state(entry={"url": f"{api_base}/mcp"})
                return state is not None and state.status == "configured" and hosted_tools.hosted_server_reconnecting() is True

            assert _eventually(mid_reconnect, timeout=30.0), "Hermes' status reads configured while it reconnects"
            (_hermes_home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")  # step one
            assert allow.server_runtime(api_base) == "held", "still Hermes' task, with the untrusted config"
            assert discovery.reconcile_mcp_servers_with_config()["removed"] == ["agentchat"]
            assert hosted_tools.hosted_server_reconnecting() is False
            assert allow.server_runtime(api_base) == "gone"
    finally:
        lifecycle.shutdown_mcp_servers()


def test_each_profile_waits_only_on_its_own_server(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch, _multiplexed: Any, _probe_captures: dict[str, Any]
) -> None:
    """A gateway serving two profiles runs an ``agentchat`` server for each,
    keyed by the profile's home. Read in a profile's scope (the one its
    plugin captured in ``register()``), Hermes' status shows that profile's
    server only: once the launch profile's watcher tick drops its server, its
    second step reads it gone while the served profile's same-named server is
    still connected — and the other way round."""
    pytest.importorskip("mcp.client.streamable_http")
    discovery = _import("tools.mcp_tool_discovery")
    lifecycle = _import("tools.mcp_tool_lifecycle")
    monkeypatch.setattr(_import("tools.approval_prompt"), "request_elicitation_consent", lambda *a, **k: "decline")
    from agentchatme_hermes import allow

    root = _hermes_home
    served = _two_profiles(root, monkeypatch)
    ports = {root: _free_port(), served: _free_port()}
    base = {home: (home / "config.yaml").read_text(encoding="utf-8") for home in ports}
    for home, port in ports.items():
        (home / "config.yaml").write_text(
            base[home] + f"mcp_servers:\n  agentchat:\n    url: http://127.0.0.1:{port}/mcp\n    trust: untrusted\n",
            encoding="utf-8",
        )
    _multiplexed()
    _load_plugins(root, routed=False)
    _load_plugins(served, routed=True)
    key = _import("hermes_constants").hermes_home_key
    scopes = {key(Path(home)): scope for home, scope in _probe_captures.items()}
    assert set(scopes) == {key(root), key(served)}

    def runtime(home: Path) -> str:
        return str(_in_a_worker(lambda: scopes[key(home)].run(allow.server_runtime, "https://api.agentchat.me")))

    def in_scope(home: Path, fn: Any) -> Any:
        return _in_a_worker(lambda: scopes[key(home)].run(fn))

    try:
        with _McpStub(ports[root]), _McpStub(ports[served]):
            for home in ports:
                in_scope(home, discovery.discover_mcp_tools)
            assert runtime(root) == "held" and runtime(served) == "held"

            (root / "config.yaml").write_text(base[root], encoding="utf-8")  # the launch profile's step one
            assert runtime(root) == "held"
            assert in_scope(root, discovery.reconcile_mcp_servers_with_config)["removed"] == ["agentchat"]
            assert runtime(root) == "gone", "the served profile's agentchat server is not the launch profile's"
            assert runtime(served) == "held"

            (served / "config.yaml").write_text(base[served], encoding="utf-8")
            assert in_scope(served, discovery.reconcile_mcp_servers_with_config)["removed"] == ["agentchat"]
            assert runtime(served) == "gone"
    finally:
        lifecycle.shutdown_mcp_servers()


# ─── plugins.isolation: host (lab R3, L2-F05 / L2-F06) ──────────────────────


@pytest.mark.parametrize(
    ("written", "host"),
    [(None, False), ("host", True), ("HOST", True), (" host ", True), ("in_process", False), ("in-process", False), ("sandbox", False)],
)
def test_plugins_isolation_is_read_as_hermes_reads_it(
    _hermes_home: Path, monkeypatch: pytest.MonkeyPatch, written: str | None, host: bool
) -> None:
    """``allow.plugin_host_isolation`` reads ``plugins.isolation`` from the
    profile's config.yaml the way Hermes' loader decides (``isolation_mode``),
    and has to read it itself: inside the plugin host Hermes answers
    ``in_process`` (its ``HERMES_PLUGIN_HOST_PROCESS`` marker). There Hermes
    skips ``register_cli_command``, so no ``hermes agentchat`` command exists."""
    from agentchatme_hermes import allow

    isolation = _import("hermes_cli.plugin_isolation")
    monkeypatch.delenv(isolation.HOST_PROCESS_ENV, raising=False)
    config = {"plugins": {"isolation": written}} if written is not None else {}
    (_hermes_home / "config.yaml").write_text(
        "plugins:\n  isolation: '" + written + "'\n" if written is not None else "model: {}\n", encoding="utf-8"
    )
    assert (isolation.isolation_mode(config) == isolation.ISOLATION_HOST) is host
    assert allow.plugin_host_isolation() is host
    monkeypatch.setenv(isolation.HOST_PROCESS_ENV, "1")
    assert isolation.isolation_mode(config) == isolation.ISOLATION_IN_PROCESS, "why the plugin reads the file"
    assert allow.plugin_host_isolation() is host
    assert "register_cli_command" in isolation.HOST_SKIPPED_CTX_METHODS


def test_hermes_says_whether_a_gateway_serves_the_profile(_hermes_home: Path) -> None:
    """``allow.hermes_gateway`` asks Hermes' one liveness answer for a
    profile (``gateway.status.resolve_gateway_liveness``: pid file and lock,
    runtime status, a multiplexed gateway serving it) — no plugin code needs
    to run in that gateway, as under ``plugins.isolation: host``."""
    from agentchatme_hermes import allow

    status = _import("gateway.status")
    assert {"profile_dir", "use_cache"} <= _params(status.resolve_gateway_liveness)
    liveness = status.resolve_gateway_liveness(profile_dir=_hermes_home, use_cache=False)
    assert liveness.running is False and liveness.pid is None
    # ``probe_error`` tells "down" from "cannot tell": only a clean "down" clears a record.
    assert liveness.probe_error is False
    assert allow.gateway_seen(_hermes_home) == "stopped"
    assert allow.hermes_gateway(_hermes_home) is None
    # The pid-reuse guard: Hermes' start-time fingerprint and its comparator.
    import os

    assert {"pid"} <= _params(status.get_process_start_time)
    start = allow.process_start_time(os.getpid())
    assert isinstance(start, int) and start == status.get_process_start_time(os.getpid())
    assert status.start_time_fingerprints_match(start, start) is True
    assert status.start_time_fingerprints_match(start, start + 10_000) is False
    assert allow.same_gateway((os.getpid(), start), (os.getpid(), allow.process_start_time(os.getpid())))


# ─── pre-0.7.0 lab fixes ────────────────────────────────────────────────────


def test_a_replacement_install_loads_nothing_into_a_running_gateway() -> None:
    """Lab R3 (L1-F10): ``hermes plugins install --force`` over an enabled
    plugin changes no selection and activates nothing, so a running gateway
    keeps the code it imported: after-install.md says to restart it, and
    doctor compares the build the gateway's health record names."""
    install = _import("hermes_cli.plugins_cmd_install")
    source = inspect.getsource(install)
    assert "Replacement installed; plugin selection was not changed." in source
    assert "if activate and not is_memory_provider:" in source and "report_activation(" in source


def test_config_unset_removes_a_leftover_handle_from_this_profiles_env(
    _hermes_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cleanup doctor names for a copied AGENTCHATME_HANDLE (R2-F04):
    ``hermes config unset AGENTCHATME_HANDLE`` removes it from the profile's
    own ``.env`` (any UPPER_SNAKE name is an env setting there)."""
    routing = _import("hermes_cli.config_env_routing")
    assert routing.is_env_setting_key("AGENTCHATME_HANDLE") is True
    env = _hermes_home / ".env"
    env.write_text("AGENTCHATME_HANDLE=fixtest-lab-he-1\nOTHER=keep\n", encoding="utf-8")
    _import("hermes_cli.config").unset_config_value("AGENTCHATME_HANDLE")
    text = env.read_text(encoding="utf-8")
    assert "AGENTCHATME_HANDLE" not in text and "OTHER=keep" in text
