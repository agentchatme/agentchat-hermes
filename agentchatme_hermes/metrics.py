"""Optional Prometheus metrics for the AgentChat Hermes plugin.

Mirrors the shape of ``agentchat-openclaw/src/metrics.ts`` so an operator
running both plugins gets parallel counter/gauge/histogram families across
the two runtimes. Names are prefixed ``agentchat_hermes_`` for
unambiguous scraping.

Design contract:

* The plugin owns no Prometheus endpoint. Hermes hosts may or may not
  expose ``/metrics``; we register into the operator's existing
  registry, never our own.
* The ``prometheus_client`` library is a soft dependency. ``import
  agentchatme_hermes.metrics`` succeeds without it; only
  :func:`enable_prometheus` requires it. A noop recorder is the default
  so a plain ``pip install agentchatme-hermes`` has zero metric
  overhead.
* The recorder is module-level singleton and wired by the operator at
  startup via :func:`enable_prometheus` (auto-detects the default
  registry) or :func:`set_recorder` (custom recorder for tests / Datadog
  / OTel adapters).

Recorded signals:

* ``agentchat_hermes_connection_state`` (gauge, label=``state``) — 1 for
  the currently-active state, 0 for others. States: ``connecting``,
  ``ready``, ``disconnected``, ``failed``.
* ``agentchat_hermes_inbound_total`` (counter, label=``kind``) — every
  inbound frame dispatched, by kind: ``message_new``, ``group_message``,
  ``group_deleted``, ``ignored``.
* ``agentchat_hermes_outbound_sent_total`` (counter) — successful sends
  through the platform's ``send()`` method.
* ``agentchat_hermes_outbound_failed_total`` (counter, label=``code``) —
  send failures, labeled by the error code we returned (``RATE_LIMITED``,
  ``AWAITING_REPLY``, etc.).
* ``agentchat_hermes_send_latency_seconds`` (histogram) — end-to-end
  ``send()`` latency.
* ``agentchat_hermes_reconnect_total`` (counter, label=``reason``) —
  framework-level reconnect signals.
* ``agentchat_hermes_tool_calls_total`` (counter, labels=``tool``,
  ``outcome``) — every ``agentchat_*`` tool invocation.
* ``agentchat_hermes_tool_latency_seconds`` (histogram, label=``tool``) —
  per-tool wall-clock latency.
* ``agentchat_hermes_inflight_depth`` (gauge) — current concurrency depth
  inside the tool semaphore.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ─── Public Protocol ──────────────────────────────────────────────────────


class MetricsRecorder(Protocol):
    """Protocol every recorder implements. Keep this stable across versions —
    swapping recorders shouldn't require changes to call sites."""

    def set_connection_state(self, state: str) -> None: ...
    def inc_inbound(self, kind: str) -> None: ...
    def inc_outbound_sent(self) -> None: ...
    def inc_outbound_failed(self, code: str) -> None: ...
    def observe_send_latency(self, seconds: float) -> None: ...
    def inc_reconnect(self, reason: str) -> None: ...
    def observe_tool_call(self, tool: str, outcome: str, seconds: float) -> None: ...
    def set_inflight_depth(self, n: int) -> None: ...


# ─── Noop default ─────────────────────────────────────────────────────────


class _NoopRecorder:
    """Zero-overhead default. Every method returns ``None`` immediately."""

    __slots__ = ()

    def set_connection_state(self, state: str) -> None:
        return None

    def inc_inbound(self, kind: str) -> None:
        return None

    def inc_outbound_sent(self) -> None:
        return None

    def inc_outbound_failed(self, code: str) -> None:
        return None

    def observe_send_latency(self, seconds: float) -> None:
        return None

    def inc_reconnect(self, reason: str) -> None:
        return None

    def observe_tool_call(self, tool: str, outcome: str, seconds: float) -> None:
        return None

    def set_inflight_depth(self, n: int) -> None:
        return None


# ─── Module-level singleton ───────────────────────────────────────────────


_recorder: MetricsRecorder = _NoopRecorder()


def get_recorder() -> MetricsRecorder:
    """Return the active recorder. Defaults to a noop recorder."""
    return _recorder


def set_recorder(recorder: MetricsRecorder) -> None:
    """Install a custom recorder (used by tests, custom integrations)."""
    global _recorder
    _recorder = recorder
    logger.debug("agentchat_hermes: metrics recorder set to %s", type(recorder).__name__)


def reset_recorder() -> None:
    """Restore the noop default. Test helper."""
    global _recorder
    _recorder = _NoopRecorder()


# ─── Prometheus integration (soft dependency) ─────────────────────────────


def enable_prometheus(registry: Any | None = None) -> MetricsRecorder:
    """Wire a prometheus_client-backed recorder and install it as the active
    singleton.

    :param registry: optional ``prometheus_client.CollectorRegistry``.
        Defaults to ``prometheus_client.REGISTRY`` (the global registry the
        ``/metrics`` endpoint exposes).
    :raises ImportError: if ``prometheus_client`` is not installed.

    Idempotent: calling twice with the same registry replaces the existing
    metric family — Prometheus's CollectorRegistry rejects duplicate
    registrations otherwise. We catch and reuse on second call.
    """
    try:
        import prometheus_client  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "agentchat_hermes.metrics.enable_prometheus requires "
            "prometheus_client. Install with: pip install prometheus_client"
        ) from e

    reg = registry if registry is not None else prometheus_client.REGISTRY
    recorder = _build_prometheus_recorder(prometheus_client, reg)
    set_recorder(recorder)
    return recorder


def _build_prometheus_recorder(pc: Any, registry: Any) -> MetricsRecorder:
    """Construct the Prometheus recorder against the supplied registry.

    Safe-against-double-register pattern: each metric is wrapped in a
    try/except. If a metric with the same name is already in the registry
    (re-init in tests, hot reload), we reuse it via ``registry._names_to_collectors``
    rather than crash. Same approach prom-client uses internally.
    """

    def _metric(cls: Any, name: str, doc: str, **kwargs: Any) -> Any:
        try:
            return cls(name, doc, registry=registry, **kwargs)
        except ValueError:
            existing = registry._names_to_collectors.get(name)
            if existing is None:
                raise
            return existing

    Counter = pc.Counter
    Gauge = pc.Gauge
    Histogram = pc.Histogram

    connection_state = _metric(
        Gauge,
        "agentchat_hermes_connection_state",
        "Current adapter connection state (1 = active, 0 = otherwise).",
        labelnames=["state"],
    )
    inbound = _metric(
        Counter,
        "agentchat_hermes_inbound_total",
        "Inbound frames dispatched into Hermes, by kind.",
        labelnames=["kind"],
    )
    outbound_sent = _metric(
        Counter,
        "agentchat_hermes_outbound_sent_total",
        "Outbound messages successfully sent.",
    )
    outbound_failed = _metric(
        Counter,
        "agentchat_hermes_outbound_failed_total",
        "Outbound send failures, labeled by error code.",
        labelnames=["code"],
    )
    send_latency = _metric(
        Histogram,
        "agentchat_hermes_send_latency_seconds",
        "End-to-end send() latency in seconds.",
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    reconnect = _metric(
        Counter,
        "agentchat_hermes_reconnect_total",
        "Adapter reconnect signals to Hermes framework, by reason.",
        labelnames=["reason"],
    )
    tool_calls = _metric(
        Counter,
        "agentchat_hermes_tool_calls_total",
        "Tool invocations, labeled by tool name and outcome.",
        labelnames=["tool", "outcome"],
    )
    tool_latency = _metric(
        Histogram,
        "agentchat_hermes_tool_latency_seconds",
        "Per-tool wall-clock latency in seconds.",
        labelnames=["tool"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    inflight = _metric(
        Gauge,
        "agentchat_hermes_inflight_depth",
        "Current concurrent tool-call count (semaphore depth).",
    )

    _STATES = ("connecting", "ready", "disconnected", "failed")

    class _PrometheusRecorder:
        __slots__ = ()

        def set_connection_state(self, state: str) -> None:
            for s in _STATES:
                connection_state.labels(state=s).set(1 if s == state else 0)

        def inc_inbound(self, kind: str) -> None:
            inbound.labels(kind=kind).inc()

        def inc_outbound_sent(self) -> None:
            outbound_sent.inc()

        def inc_outbound_failed(self, code: str) -> None:
            outbound_failed.labels(code=code).inc()

        def observe_send_latency(self, seconds: float) -> None:
            send_latency.observe(seconds)

        def inc_reconnect(self, reason: str) -> None:
            reconnect.labels(reason=reason).inc()

        def observe_tool_call(self, tool: str, outcome: str, seconds: float) -> None:
            tool_calls.labels(tool=tool, outcome=outcome).inc()
            tool_latency.labels(tool=tool).observe(seconds)

        def set_inflight_depth(self, n: int) -> None:
            inflight.set(n)

    return _PrometheusRecorder()


__all__ = [
    "MetricsRecorder",
    "enable_prometheus",
    "get_recorder",
    "reset_recorder",
    "set_recorder",
]
