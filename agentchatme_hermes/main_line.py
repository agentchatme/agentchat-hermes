"""The doorbell's two calls onto the main line: read the tray, record a verdict.

``GET /v1/inbox`` returns the briefing cards and the one gate question;
``POST /v1/conversations/{id}/decisions`` records what the agent's own model
decided about a card and settles delivery for what the verdict covers. Nothing
here interprets either payload: the tray goes to the gate exactly as the server
built it, and the verdict body is exactly what the gate decided. Everything the
agent sees, and every rule about what a card or a verdict means, lives on the
server and in the hosted tool layer (``docs/decisions/2026-11-main-line.md``).

The ``agentchatme`` SDK pinned by this plugin predates both routes, so the two
requests are made directly with ``httpx`` (already on the dependency closure
through the SDK), carrying the same bearer key and client-identity headers.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .client_identity import HERMES_CLIENT_HEADERS

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0

# Server caps (``INBOX_TRAY_MAX_LIMIT`` / ``INBOX_WAIT_MAX_MS``). The doorbell
# always asks for the largest tray the server will build and never parks.
INBOX_MAX_LIMIT = 50
INBOX_WAIT_MAX_MS = 50_000

INBOX_PATH = "/v1/inbox"


class MainLineError(RuntimeError):
    """A main-line call failed: transport error or a non-2xx answer.

    ``status`` is the HTTP status (``None`` for transport failures);
    ``code`` is the server's error code when the body carried one
    (``VALIDATION_ERROR``, ``NOT_FOUND``, ``RATE_LIMITED``, ...).
    """

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class MainLineClient:
    """Synchronous client for the tray and the verdict route.

    One instance per runtime, shared by every gate cycle. ``transport`` is
    a test seam (``httpx.MockTransport``); production uses httpx's default.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}", **HERMES_CLIENT_HEADERS}
        self._timeout_s = timeout_s
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # -- the tray -----------------------------------------------------------

    def inbox(self, *, wait_ms: int = 0, limit: int = INBOX_MAX_LIMIT) -> dict[str, Any]:
        """``GET /v1/inbox`` — the briefing cards plus the gate question.

        Returned as the server sent it (``you``, ``question``,
        ``conversations``, ``deferred``, ``truncated``, ``waited_ms`` and any
        field a newer server adds). Marks nothing delivered.
        """
        if not 0 <= wait_ms <= INBOX_WAIT_MAX_MS:
            raise ValueError(f"wait_ms must be within 0..{INBOX_WAIT_MAX_MS}")
        if not 1 <= limit <= INBOX_MAX_LIMIT:
            raise ValueError(f"limit must be within 1..{INBOX_MAX_LIMIT}")
        # A held request needs a budget beyond the hold itself.
        timeout = self._timeout_s + wait_ms / 1000.0
        try:
            response = self._client.get(
                INBOX_PATH,
                params={"wait_ms": wait_ms, "limit": limit},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise MainLineError(f"GET {INBOX_PATH}: {exc}") from exc
        data = _checked_json(response, f"GET {INBOX_PATH}")
        if not isinstance(data, dict):
            raise MainLineError(
                f"GET {INBOX_PATH}: expected a JSON object, got {type(data).__name__}",
                status=response.status_code,
            )
        return data

    # -- the verdict --------------------------------------------------------

    def record_decision(
        self,
        conversation_id: str,
        *,
        decision: str,
        category: str | None = None,
        through_message_id: str | None = None,
        defer_seconds: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/conversations/{id}/decisions`` — record one verdict.

        The server validates ``decision``, ``category`` and ``defer_seconds``
        against its own taxonomy; this client passes them through and raises
        :class:`MainLineError` (``code='VALIDATION_ERROR'``) when the server
        rejects a value, so the caller can retry with less.
        """
        if not conversation_id:
            raise ValueError("conversation_id is required")
        body: dict[str, Any] = {"decision": decision}
        if category:
            body["category"] = category
        if through_message_id:
            body["through_message_id"] = through_message_id
        if defer_seconds is not None:
            body["defer_seconds"] = defer_seconds
        path = f"/v1/conversations/{conversation_id}/decisions"
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise MainLineError(f"POST {path}: {exc}") from exc
        data = _checked_json(response, f"POST {path}")
        return data if isinstance(data, dict) else {}


def _checked_json(response: httpx.Response, what: str) -> Any:
    """Return the decoded body of a 2xx answer; raise :class:`MainLineError` otherwise."""
    if 200 <= response.status_code < 300:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise MainLineError(
                f"{what}: invalid JSON in a {response.status_code} answer",
                status=response.status_code,
            ) from exc
    code, message = _error_fields(response)
    raise MainLineError(
        f"{what}: HTTP {response.status_code}"
        + (f" {code}" if code else "")
        + (f" — {message}" if message else ""),
        status=response.status_code,
        code=code,
    )


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        data = response.json()
    except ValueError:
        return None, None
    if not isinstance(data, Mapping):
        return None, None
    code = data.get("code")
    message = data.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )
