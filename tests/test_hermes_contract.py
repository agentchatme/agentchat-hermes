"""The Hermes surfaces this plugin depends on, checked against a real Hermes.

Skipped unless Hermes is importable (the unit suite runs without it). The
scheduled ``hermes-main`` workflow installs hermes-agent from its main branch
and runs the whole suite with it, so a Hermes change that breaks one of these
couplings shows within a day instead of at the next release. What each one is
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


def test_plugin_llm_async_call_reaches_the_main_model(answering_model: _Stub, _hermes_home: Path) -> None:
    """With the receive loop's cancel handle the gate call is
    ``ctx.llm.acomplete`` on a private loop per call (Hermes rebinds its async
    client to each loop): same model, same answer, twice in a row."""
    from agentchatme_hermes.model_call import build_call_model

    _main_model_config(_hermes_home, answering_model.base_url)
    plugin_llm = _import("agent.plugin_llm")
    assert inspect.iscoroutinefunction(plugin_llm.PluginLlm.acomplete)
    assert {"messages", "temperature", "max_tokens", "timeout", "purpose"} <= _params(plugin_llm.PluginLlm.acomplete)
    call_model = build_call_model(main_runtime={}, timeout_s=20.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat"))
    for _ in range(2):
        assert call_model("THE PROMPT", cancel=threading.Event()) == "done"
        assert answering_model.requests[-1] == {"path": "/v1/chat/completions", "model": "contract-main-model", "tools": False}


def test_plugin_llm_async_call_is_cancelled_when_the_loop_stops_waiting(hanging_model: _Stub, _hermes_home: Path) -> None:
    from agentchatme_hermes.model_call import GateCallCancelled, build_call_model

    _main_model_config(_hermes_home, hanging_model.base_url)
    plugin_llm = _import("agent.plugin_llm")
    call_model = build_call_model(main_runtime={}, timeout_s=30.0, host_llm=plugin_llm.PluginLlm(plugin_id="agentchat"))
    cancel = threading.Event()

    def cancel_once_asked() -> None:
        deadline = time.monotonic() + 15.0
        while not hanging_model.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    threading.Thread(target=cancel_once_asked, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(GateCallCancelled):
        call_model("THE PROMPT", cancel=cancel)
    assert hanging_model.requests, "the request reached the model"
    assert time.monotonic() - started < 20.0, "ended by the cancel, not by its 30 s timeout"


def test_auxiliary_call_llm_takes_what_the_fallback_passes() -> None:
    accepted = _params(_import("agent.auxiliary_client").call_llm)
    assert {"task", "provider", "model", "base_url", "api_key", "main_runtime", "messages", "temperature", "max_tokens", "timeout"} <= accepted


def test_config_and_provider_helpers() -> None:
    config = _import("hermes_cli.config")
    for name in ("load_config", "save_config", "get_env_value", "save_env_value"):
        assert callable(getattr(config, name, None)), name
    assert any(callable(getattr(config, n, None)) for n in ("read_user_config_raw", "read_raw_config", "load_config"))
    assert "target_model" in _params(_import("hermes_cli.runtime_provider").resolve_runtime_provider)


def test_wizard_helpers() -> None:
    setup = _import("hermes_cli.setup")
    for name in ("get_env_value", "print_header", "print_info", "print_success", "print_warning", "prompt_choice", "save_env_value"):
        assert callable(getattr(setup, name, None)), name
    output = _import("hermes_cli.cli_output")
    assert callable(output.prompt) and callable(output.prompt_yes_no)


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


def test_gateway_marks_its_process() -> None:
    """``leader_lock.is_hermes_gateway_process`` reads the marker gateway/run.py sets."""
    spec = importlib.util.find_spec("gateway.run")
    assert spec is not None and spec.origin is not None
    assert "_HERMES_GATEWAY" in Path(spec.origin).read_text(encoding="utf-8")
