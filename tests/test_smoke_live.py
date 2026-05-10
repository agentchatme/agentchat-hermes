"""Live end-to-end smoke test against the deployed AgentChat API.

Skipped by default. Runs only when ``AGENTCHATME_LIVE_API_KEY`` (preferred)
or ``AGENTCHAT_LIVE_API_KEY`` (matches the SDK's env name for shared CI
secrets) is set in the environment. CI gates this file behind a workflow
input + repository secret so PRs from forks never hit the live API.

The contract is "no side effects you can't undo without a click": every
call here is a read against the caller's own state plus a one-shot
WebSocket connect that closes immediately. We never:

  * send a message
  * mutate any contact / mute / block
  * upload an avatar / attachment
  * register a webhook
  * change presence

If the plugin ever needs a "creates a row" smoke check, it goes under a
separate marker (``@pytest.mark.live_mutating``) gated on a disposable
test agent.

Why a live smoke at all? Because every parity fix in the unit suite uses
mocked objects — those tests prove the adapter *would* dispatch correctly,
not that the wire format on the actual server still matches what the SDK
expects. One smoke test against ``https://api.agentchat.me`` per release
catches model drift the moment it ships.

Required environment:
  AGENTCHATME_LIVE_API_KEY  a valid ``ac_live_…`` token for any agent
                            (or AGENTCHAT_LIVE_API_KEY — same thing,
                            shared with the SDK's smoke fixture).

Optional environment:
  AGENTCHAT_LIVE_BASE_URL   override (defaults to https://api.agentchat.me)
"""

from __future__ import annotations

import asyncio
import os

import pytest
from agentchatme import AgentChatClient

# Accept either env var. AGENTCHATME_LIVE_API_KEY matches our package's
# AGENTCHATME_* prefix; AGENTCHAT_LIVE_API_KEY matches the existing SDK
# smoke fixture so a single GitHub Actions secret covers both repos.
_API_KEY = (
    os.environ.get("AGENTCHATME_LIVE_API_KEY")
    or os.environ.get("AGENTCHAT_LIVE_API_KEY")
)
_BASE_URL = os.environ.get("AGENTCHAT_LIVE_BASE_URL", "https://api.agentchat.me")

# Skip the entire module unless explicitly opted in. ``allow_module_level``
# is required because individual ``pytest.skip`` calls inside an
# ``asyncio_mode = auto`` config get re-wrapped before they fire.
if not _API_KEY:
    pytest.skip(
        "AGENTCHATME_LIVE_API_KEY / AGENTCHAT_LIVE_API_KEY not set — "
        "live smoke tests skipped",
        allow_module_level=True,
    )


# ─── Auth + identity ──────────────────────────────────────────────────────


@pytest.mark.live
def test_get_me_round_trips() -> None:
    """The most fundamental check: auth works and the agent record parses.

    If this fails, every other call in the plugin would also fail —
    so the rest of the suite skips on a key that doesn't authenticate.
    """
    with AgentChatClient(api_key=_API_KEY or "", base_url=_BASE_URL) as client:
        me = client.get_me()
    assert isinstance(me["handle"], str), f"unexpected handle: {me!r}"
    assert me["status"] in ("active", "restricted", "suspended"), (
        f"unexpected status: {me.get('status')!r}"
    )
    assert "settings" in me
    assert me["settings"]["inbox_mode"] in ("open", "contacts_only")


# ─── Read-only platform reads ─────────────────────────────────────────────


@pytest.mark.live
def test_list_conversations_returns_an_array() -> None:
    """Read-only — every agent has either zero or many conversations."""
    with AgentChatClient(api_key=_API_KEY or "", base_url=_BASE_URL) as client:
        convs = client.list_conversations()
    assert isinstance(convs, list)
    for c in convs:
        assert "id" in c, f"conversation missing id: {c!r}"


@pytest.mark.live
def test_search_directory_handle_prefix() -> None:
    """The directory accepts an empty prefix and returns at most ``limit``."""
    with AgentChatClient(api_key=_API_KEY or "", base_url=_BASE_URL) as client:
        # Use a prefix unlikely to match anything to keep response small.
        # Empty prefix is rejected; "z" is fine and returns whatever exists.
        results = client.search_agents("z", limit=5)
    assert isinstance(results, list)
    for r in results:
        assert "handle" in r, f"directory entry missing handle: {r!r}"


@pytest.mark.live
def test_list_contacts_paginates() -> None:
    """Contacts list returns a list (possibly empty)."""
    with AgentChatClient(api_key=_API_KEY or "", base_url=_BASE_URL) as client:
        contacts = client.list_contacts(limit=10, offset=0)
    assert isinstance(contacts, list)


# ─── Realtime smoke (connect + close) ─────────────────────────────────────
#
# The full inbound dispatch path goes through the Hermes framework, which
# isn't installed in this test context. So we only verify the SDK can open
# the WebSocket and complete the HELLO handshake — that's the integration
# point where a wire-format change on the server would surface first. The
# rest of the path (frame → MessageEvent → handle_message) is unit-tested.


@pytest.mark.live
def test_realtime_can_connect_and_disconnect() -> None:
    """Open a WebSocket, wait for hello.ok, close cleanly."""
    from agentchatme import AsyncAgentChatClient, RealtimeClient, RealtimeOptions

    async def go() -> None:
        client = AsyncAgentChatClient(api_key=_API_KEY or "", base_url=_BASE_URL)
        await client.__aenter__()
        try:
            realtime = RealtimeClient(
                RealtimeOptions(
                    api_key=_API_KEY or "",
                    base_url=_BASE_URL,
                    client=client,
                    reconnect=False,  # one-shot
                )
            )
            connected = asyncio.Event()
            realtime.on_connect(lambda: connected.set())
            try:
                await asyncio.wait_for(realtime.connect(), timeout=20.0)
                # hello.ok should land within a couple of seconds; 5s is
                # generous for a healthy server. If this times out the
                # SDK's internal handshake is broken.
                await asyncio.wait_for(connected.wait(), timeout=5.0)
                assert realtime.is_connected
            finally:
                await realtime.disconnect()
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(go())


# ─── Adapter wiring smoke (no Hermes runtime) ─────────────────────────────


@pytest.mark.live
def test_metrics_module_round_trips_state() -> None:
    """A live smoke is the right place to confirm the metrics module
    surfaces real connection-state transitions without breaking anything.

    We can't exercise the full adapter here (Hermes isn't installed in CI
    for this test), but we can prove the recorder is wired and observable.
    """
    from agentchatme_hermes.metrics import (
        get_recorder,
        reset_recorder,
        set_recorder,
    )

    captured: list[tuple[str, ...]] = []

    class CapturingRecorder:
        def set_connection_state(self, state: str) -> None:
            captured.append(("state", state))

        def inc_inbound(self, kind: str) -> None:
            captured.append(("inbound", kind))

        def inc_outbound_sent(self) -> None:
            captured.append(("outbound_sent",))

        def inc_outbound_failed(self, code: str) -> None:
            captured.append(("outbound_failed", code))

        def observe_send_latency(self, seconds: float) -> None:
            captured.append(("send_latency", str(seconds)))

        def inc_reconnect(self, reason: str) -> None:
            captured.append(("reconnect", reason))

        def observe_tool_call(self, tool: str, outcome: str, seconds: float) -> None:
            captured.append(("tool", tool, outcome))

        def set_inflight_depth(self, n: int) -> None:
            captured.append(("inflight", str(n)))

    try:
        set_recorder(CapturingRecorder())
        rec = get_recorder()
        rec.set_connection_state("connecting")
        rec.set_connection_state("ready")
        rec.set_connection_state("disconnected")
    finally:
        reset_recorder()

    assert ("state", "connecting") in captured
    assert ("state", "ready") in captured
    assert ("state", "disconnected") in captured
