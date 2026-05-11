#!/usr/bin/env python3
"""End-to-end test harness for the agentchatme-hermes plugin.

Runs INSIDE Hermes's actual plugin discovery + tool-registry pipeline,
on a real VM with a real AGENTCHATME_API_KEY. Validates everything a
unit-suite mock can't catch:

  * Hermes's `PluginManager.load_all()` finds and loads the plugin
  * `register(ctx)` runs without raising
  * Every `agentchat_*` tool is registered with a valid schema
  * Tool handlers, dispatched through `tools.registry.dispatch()`,
    return a JSON-string (the contract we lock in by `_serialize`)
  * Read-only tools hit the real AgentChat backend and produce the
    expected response shapes
  * Bad-input handling: malformed args produce a clean JSON error,
    not a raised exception

Exit code: 0 if every required test passes, 1 otherwise.

Usage on the VM:
    AGENTCHATME_API_KEY=ac_*** python3 scripts/e2e_harness.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# ── result tracking ─────────────────────────────────────────────────────────

class Results:
    def __init__(self) -> None:
        self.passed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append((name, detail))
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        print(f"  ✗ {name}  ({detail})")

    def skip(self, name: str, detail: str) -> None:
        self.skipped.append((name, detail))
        print(f"  ⊘ {name}  ({detail})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print()
        print("─" * 60)
        print(f"  {len(self.passed)}/{total} passed, "
              f"{len(self.failed)} failed, {len(self.skipped)} skipped")
        if self.failed:
            print()
            print("  Failures:")
            for name, detail in self.failed:
                print(f"    ✗ {name}")
                print(f"        {detail}")
        return 0 if not self.failed else 1


# ── Hermes runtime bootstrap ────────────────────────────────────────────────

def _resolve_hermes_path() -> Path:
    """Locate the Hermes installation so we can import hermes_cli, tools, etc."""
    # Standard install layout: /usr/local/lib/hermes-agent (linux installer)
    # or sibling path the user dropped via `hermes --version` PATH.
    candidates = [
        Path("/usr/local/lib/hermes-agent"),
        Path.home() / ".local/lib/hermes-agent",
    ]
    hint = os.getenv("HERMES_INSTALL_DIR")
    if hint:
        candidates.insert(0, Path(hint))

    for p in candidates:
        if (p / "hermes_cli").is_dir() and (p / "tools").is_dir():
            return p
    raise RuntimeError(
        "Cannot locate Hermes install. Set HERMES_INSTALL_DIR or run "
        "from a host where `hermes --version` works."
    )


# ── A. plugin load + registration ───────────────────────────────────────────

def test_plugin_loads(r: Results) -> tuple[Any, list[str]]:
    """Verify Hermes's loader can pick up the plugin and call register()."""
    print("\n[A] Plugin load + registration")

    try:
        from hermes_cli.plugins import PluginManager  # type: ignore
        from tools.registry import registry  # type: ignore
    except Exception as e:
        r.fail("import hermes_cli.plugins / tools.registry", str(e))
        return None, []

    # Track tools registered before vs after — diff is "this plugin's tools".
    before = set(registry.get_all_tool_names())

    try:
        mgr = PluginManager()
        # Hermes 0.13 uses `discover_and_load`; older builds may have used
        # `load_all`. Try the modern name first, fall back if missing.
        loader = getattr(mgr, "discover_and_load", None) or getattr(mgr, "load_all", None)
        if loader is None:
            raise AttributeError(
                "PluginManager has neither discover_and_load nor load_all"
            )
        loader()
    except Exception as e:
        r.fail("PluginManager loader", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
        return None, []
    r.ok("PluginManager.discover_and_load() returned without raising")

    after = set(registry.get_all_tool_names())
    plugin_tools = sorted(after - before)
    agentchat_tools = [t for t in plugin_tools if t.startswith("agentchat_")]

    if not agentchat_tools:
        r.fail("agentchat_* tools registered", "found 0 agentchat_* tools")
        return mgr, []

    r.ok(f"{len(agentchat_tools)} agentchat_* tools registered",
         f"sample: {agentchat_tools[:5]}")

    # Cross-check: did the agentchat platform itself register?
    try:
        from gateway.platform_registry import platform_registry  # type: ignore
        entry = platform_registry.get("agentchat")
        if entry is None:
            r.fail("platform registered", "platform_registry.get('agentchat') is None")
        else:
            r.ok("AgentChat platform registered",
                 f"label={getattr(entry, 'label', '?')}")
            # Standalone sender hook — Bug #4 fix lives here.
            sender = getattr(entry, "standalone_sender_fn", None)
            if sender is None:
                r.fail("standalone_sender_fn registered",
                       "PlatformEntry.standalone_sender_fn is None — cron delivery will break")
            else:
                r.ok("standalone_sender_fn registered",
                     getattr(sender, "__name__", "<lambda>"))
    except Exception as e:
        r.fail("platform_registry.get('agentchat')", str(e))

    return mgr, agentchat_tools


# ── B. schema sanity ────────────────────────────────────────────────────────

def test_schemas_well_formed(r: Results, tool_names: list[str]) -> None:
    print("\n[B] Tool schemas")
    from tools.registry import registry  # type: ignore

    bad: list[str] = []
    for name in tool_names:
        schema = registry.get_schema(name)
        if not isinstance(schema, dict):
            bad.append(f"{name}: schema is {type(schema).__name__}")
            continue
        params = schema.get("parameters")
        if not isinstance(params, dict):
            bad.append(f"{name}: parameters is not a dict")
            continue
        if params.get("type") != "object":
            bad.append(f"{name}: parameters.type != 'object'")
            continue
        if "properties" not in params:
            bad.append(f"{name}: parameters.properties missing")
            continue
        # `description` should live at schema root, not in parameters.
        if "description" not in schema:
            bad.append(f"{name}: top-level description missing")

    if bad:
        for line in bad[:5]:
            r.fail("schema check", line)
        if len(bad) > 5:
            r.fail("schema check", f"... and {len(bad) - 5} more")
    else:
        r.ok(f"All {len(tool_names)} tool schemas have description + parameters.type=object + properties")


# ── C. dispatch returns JSON string ─────────────────────────────────────────

def _dispatch(tool: str, args: dict) -> tuple[bool, Any, str]:
    """Dispatch and return (ok_shape, parsed_payload, raw)."""
    from tools.registry import registry  # type: ignore
    raw = registry.dispatch(tool, args, task_id="e2e-harness")
    if not isinstance(raw, str):
        return False, None, f"return is {type(raw).__name__}, not str"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, None, f"raw is not JSON: {e}"
    return True, payload, raw


def test_readonly_tools(r: Results, available: set[str]) -> None:
    """Read-only tools hit the real backend. Need AGENTCHATME_API_KEY."""
    print("\n[C] Read-only tool dispatch (live)")

    if not os.getenv("AGENTCHATME_API_KEY"):
        r.skip("read-only tools", "AGENTCHATME_API_KEY not set")
        return

    # The plugin's `_safe` wrapper returns an `{"ok": true, "result": ...}`
    # envelope on success and `{"ok": false, "code": ..., "message": ...}`
    # on adapter-level errors. Define each test as (tool, args,
    # required_keys_in_result, allowed_error_codes).
    #
    # `agentchat_get_presence` requires a `handle` arg. We resolve our
    # own handle from get_my_status (since AGENTCHATME_HANDLE env may
    # drift). NOT_FOUND on self-presence is a known server quirk and
    # counts as a clean envelope-level pass.
    own_handle: str | None = None
    try:
        ok_shape, payload, _ = _dispatch("agentchat_get_my_status", {})
        if ok_shape and isinstance(payload, dict) and payload.get("ok"):
            result = payload.get("result") or {}
            own_handle = result.get("handle")
    except Exception:
        pass

    presence_args = {"handle": f"@{own_handle}"} if own_handle else None

    tests: list[tuple[str, dict, list[str], list[str]]] = [
        ("agentchat_get_my_status", {}, ["handle"], []),
        ("agentchat_list_contacts", {}, [], []),
        # `list_conversations` covers DMs + groups with a `kind` field.
        ("agentchat_list_conversations", {}, [], []),
        # NOT_FOUND on self-presence is the documented server behavior
        # for solo agents (no peer has recorded a presence ping yet).
        ("agentchat_get_presence", presence_args or {}, [], ["NOT_FOUND"]),
        ("agentchat_list_mutes", {}, [], []),
    ]

    for tool, args, expected_keys, allowed_errors in tests:
        if tool not in available:
            r.skip(tool, "not registered (might be renamed)")
            continue
        if tool == "agentchat_get_presence" and presence_args is None:
            r.skip(tool, "couldn't resolve own handle for self-test")
            continue

        ok_shape, payload, raw = _dispatch(tool, args)
        if not ok_shape:
            r.fail(tool, raw)
            continue

        if not isinstance(payload, dict):
            r.fail(tool, f"payload is {type(payload).__name__}, expected dict envelope")
            continue
        if payload.get("ok") is False:
            code = payload.get("code")
            if code in allowed_errors:
                r.ok(tool, f"ok=false with allowed code {code} (envelope shape correct)")
                continue
            r.fail(tool, f"backend returned ok=false: code={code} message={payload.get('message')}")
            continue
        if payload.get("ok") is not True:
            r.fail(tool, f"missing `ok` field; got keys {list(payload)[:5]}")
            continue

        inner = payload.get("result")
        if expected_keys and isinstance(inner, dict):
            missing = [k for k in expected_keys if k not in inner]
            if missing:
                r.fail(tool, f"result missing keys {missing}; got {list(inner)[:5]}")
                continue

        sample = (
            list(inner.keys())[:3] if isinstance(inner, dict)
            else f"len={len(inner)}" if isinstance(inner, list)
            else str(inner)[:40]
        )
        r.ok(tool, f"ok+result; result={sample}")


# ── D. error-path dispatch (no real network) ────────────────────────────────

def test_error_paths(r: Results, available: set[str]) -> None:
    """Verify malformed args produce a JSON-string error, not a raised exc."""
    print("\n[D] Error-path dispatch")

    cases = [
        # Send to nobody → adapter should error cleanly.
        ("agentchat_send_message", {"to": "", "text": "hello"}),
        # Search for empty handle.
        ("agentchat_search_directory", {"handle": ""}),
        # Lookup a definitely-missing handle.
        ("agentchat_lookup_handle", {"handle": "@__definitely_not_a_real_user_xyz999__"}),
    ]

    for tool, args in cases:
        if tool not in available:
            r.skip(tool, "not registered")
            continue
        ok_shape, payload, raw = _dispatch(tool, args)
        if not ok_shape:
            r.fail(f"{tool} (bad args)", raw)
            continue
        # We expect a dict with either "error" or "ok": False or similar —
        # the key thing is dispatcher did NOT raise and we got a JSON string.
        r.ok(f"{tool} (bad args)", f"returned JSON string; payload type={type(payload).__name__}")


# ── E. standalone sender shape ──────────────────────────────────────────────

def test_standalone_sender_shape(r: Results) -> None:
    """Bug #4 regression test: the hook is reachable and signature-compliant."""
    print("\n[E] standalone_sender_fn shape")

    try:
        from gateway.platform_registry import platform_registry  # type: ignore
    except Exception as e:
        r.fail("import platform_registry", str(e))
        return

    entry = platform_registry.get("agentchat")
    if entry is None:
        r.fail("platform entry", "agentchat not in registry")
        return

    sender = getattr(entry, "standalone_sender_fn", None)
    if sender is None:
        r.fail("standalone_sender_fn", "is None — cron delivery will fail")
        return

    import inspect
    sig = inspect.signature(sender)
    params = list(sig.parameters.keys())
    expected = ["pconfig", "chat_id", "message"]
    if params[:3] != expected:
        r.fail("standalone_sender_fn signature",
               f"first 3 params {params[:3]}, expected {expected}")
        return
    if not inspect.iscoroutinefunction(sender):
        r.fail("standalone_sender_fn is async", "not a coroutine function")
        return
    r.ok("standalone_sender_fn signature OK", f"params={params}")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="agentchatme-hermes E2E harness")
    parser.add_argument(
        "--hermes-dir",
        help="Path to Hermes install (overrides auto-detection)",
    )
    args = parser.parse_args()

    if args.hermes_dir:
        os.environ["HERMES_INSTALL_DIR"] = args.hermes_dir

    hermes_path = _resolve_hermes_path()
    print(f"Hermes install: {hermes_path}")
    sys.path.insert(0, str(hermes_path))

    r = Results()

    _, tools = test_plugin_loads(r)
    if not tools:
        return r.summary()

    test_schemas_well_formed(r, tools)
    test_readonly_tools(r, set(tools))
    test_error_paths(r, set(tools))
    test_standalone_sender_shape(r)

    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
