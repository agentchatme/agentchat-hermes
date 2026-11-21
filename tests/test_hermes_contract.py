"""The Hermes surfaces this plugin depends on, checked against a real Hermes.

Skipped unless Hermes is importable (the unit suite runs without it). The
scheduled ``hermes-main`` workflow installs hermes-agent from its main branch,
and from its latest release, and runs the whole suite with each, so a Hermes
change that breaks one of these couplings shows within a day instead of at the
next release, and a break on the release users run shows too. What each one is
for, and which public hook would replace it, is ``docs/hermes-coupling.md``.

Nothing here reaches a network service: the model is a localhost stub and
``HERMES_HOME`` is a temporary directory.
"""
from __future__ import annotations

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


# ─── a localhost model ──────────────────────────────────────────────────────


class _Stub:
    """An OpenAI-compatible endpoint on localhost: answers, or hangs."""

    def __init__(self, *, hang: bool) -> None:
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "model": body.get("model"), "tools": "tools" in body})
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
    for name in ("register_cli_command", "register_skill", "on_unload"):
        assert callable(getattr(context, name, None)), name
    assert isinstance(inspect.getattr_static(context, "llm"), property)


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


def test_a_skill_outside_the_package_is_read_fresh_after_an_atomic_replace(
    _hermes_home: Path, _plugin_manager: Any
) -> None:
    """The design the manual rests on, from Hermes' code rather than a
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
    assert reg.install_entry() == "upgraded"
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
    before = config_path.read_bytes()
    assert reg.install_entry() == "present"
    assert config_path.read_bytes() == before, "nothing to change, nothing written"
    assert reg.uninstall_entry() is True
    assert "agentchat" not in (config.read_user_config_raw().get("mcp_servers") or {})


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
    from agentchatme_hermes.profile_scope import profile_context

    constants = _import("hermes_constants")
    other = _hermes_home / "profiles" / "b"
    keys = {_hermes_home: api_key("contract_profile_a"), other: api_key("contract_profile_b")}
    for home, key in keys.items():
        key_store.write_store(home, api_key=key, handle="x", api_base="https://api.agentchat.me")
        scope = constants.hermes_home_key(home)
        assert _source_registry.register_source(key_source.build_key_source(), scope=scope) is True
    for home, key in keys.items():
        environ = {"AGENTCHAT_API_KEY": api_key("stale_or_exported")}
        with profile_context(home):
            report = _source_registry.apply_all({"agentchat": key_source.secrets_section()}, home, environ=environ)
        assert environ["AGENTCHAT_API_KEY"] == key, home
        assert [s.name for s in report.sources] == ["agentchat"]


def test_a_cloned_profiles_tools_get_no_key_not_the_originals(
    _hermes_home: Path, _source_registry: Any
) -> None:
    """A profile whose .env holds a copy of the default profile's key (made by
    ``profile create``): through Hermes' orchestrator the variable comes out
    empty, so its hosted tools cannot act as the default profile's agent."""
    from fake_keys import api_key

    from agentchatme_hermes import key_source
    from agentchatme_hermes.profile_scope import profile_context

    key = api_key("contract_copied_value")
    (_hermes_home / ".env").write_text(f"AGENTCHAT_API_KEY={key}\n", encoding="utf-8")
    clone = _hermes_home / "profiles" / "t5tmp"
    clone.mkdir(parents=True)
    (clone / ".env").write_text(f"AGENTCHAT_API_KEY={key}\n", encoding="utf-8")
    scope = _import("hermes_constants").hermes_home_key(clone)
    assert _source_registry.register_source(key_source.build_key_source(), scope=scope) is True
    environ = {"AGENTCHAT_API_KEY": key}  # the copy, loaded from the clone's .env
    with profile_context(clone):
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


def test_hermes_records_which_source_supplied_a_variable() -> None:
    """``secret_source_label`` (public): how the plugin tells its own earlier
    write from an exported key."""
    assert callable(_import("agent.secret_scope").secret_source_label)
    assert callable(_import("hermes_constants").get_process_hermes_home)
    config = _import("hermes_cli.config")
    assert callable(config.remove_env_value) and callable(config.get_env_path)


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
    constants = _import("hermes_constants")
    for name in ("get_hermes_home", "set_hermes_home_override", "reset_hermes_home_override", "hermes_home_key"):
        assert callable(getattr(constants, name, None)), name
    secret_scope = _import("agent.secret_scope")
    for name in ("get_secret", "is_multiplex_active", "set_secret_scope", "reset_secret_scope"):
        assert callable(getattr(secret_scope, name, None)), name
    assert issubclass(secret_scope.UnscopedSecretError, Exception)
    assert callable(_import("gateway.run")._profile_runtime_scope)
    assert callable(_import("gateway.run_turn").GatewayTurnMixin._standalone_launch_scope)
    assert callable(_import("hermes_cli.env_loader").hydrate_profile_secret_sources)
    assert callable(_import("tools.terminal_scope").install_and_reset_profile_terminal_scope)
    assert callable(_import("tui_gateway.launch_profile_policy").owner_secret_scope)


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
    from agentchatme_hermes.profile_scope import profile_context, resolve_hermes_home

    other = tmp_path / "profiles" / "c"
    other.mkdir(parents=True)
    with profile_context(other):
        assert resolve_hermes_home().resolve() == other.resolve()


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
