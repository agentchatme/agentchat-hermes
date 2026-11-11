"""A fake AgentChat API at the httpx transport, under the real ``agentchatme`` SDK.

The wizard's network calls are the SDK's. Patching the SDK's methods would
test nothing the SDK does with the request or the answer, so :func:`serve`
replaces ``httpx.HTTPTransport.handle_request`` — the last step before a
socket — instead: the SDK builds and sends the request, and the canned
answer is parsed into the SDK's own result or exception, exactly as an
answer from the server would be. No byte leaves the process (the network
guard in ``conftest.py`` fails any test that tries).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Union

import httpx

if TYPE_CHECKING:
    import pytest

Answer = Union[httpx.Response, BaseException]
Route = Callable[[httpx.Request], Answer]


@dataclass
class Wire:
    """What reached the fake API, in order."""

    requests: list[httpx.Request] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    def bodies(self) -> list[Any]:
        return [json.loads(request.content) if request.content else None for request in self.requests]


def serve(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Route | list[Route]]) -> Wire:
    """Answer each request by its URL path.

    A list of routes is used in order, the last one repeating. A route
    returns the response, or an exception to raise from the transport (a
    connect error, a timeout).
    """
    wire = Wire()
    queues = {path: list(r) if isinstance(r, list) else [r] for path, r in routes.items()}

    def handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        wire.requests.append(request)
        queue = queues.get(request.url.path)
        if not queue:
            wire.unexpected.append(f"{request.method} {request.url}")
            return httpx.Response(599, text="no route in the fake API")
        route = queue.pop(0) if len(queue) > 1 else queue[0]
        answer = route(request)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return wire


def answer(status: int, payload: Any) -> Route:
    """A JSON response."""
    return lambda _request: httpx.Response(status, json=payload)


def api_error(status: int, code: str, message: str, details: Any = None) -> Route:
    """The server's error shape: ``{code, message, details?}``."""
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return answer(status, body)


def raw(status: int, content: bytes, content_type: str = "text/html") -> Route:
    """A response that is not JSON (a proxy's error page, an empty body)."""
    return lambda _request: httpx.Response(status, content=content, headers={"content-type": content_type})


def connect_error(text: str = "dns") -> Route:
    return lambda request: httpx.ConnectError(text, request=request)


def read_timeout(text: str = "timed out") -> Route:
    return lambda request: httpx.ReadTimeout(text, request=request)
