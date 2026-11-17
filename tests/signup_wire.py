"""The sign-up endpoints' answers, shaped as contract v1 "Sign-up flows" says.

Each function is a route for :func:`sdk_wire.serve`. The messages are the
server's sentences from the contract's table; the tests check that Hermes
shows them as they are and adds no text of its own about codes, tries,
limits or minutes.
"""
from __future__ import annotations

from typing import Any

import httpx
from sdk_wire import Route, answer, api_error

REGISTER = "/v1/register"
REGISTER_VERIFY = "/v1/register/verify"
RECOVER = "/v1/agents/recover"
RECOVER_VERIFY = "/v1/agents/recover/verify"
ME = "/v1/agents/me"

WRONG_CODE = "That code didn't work. Check the code and try again."
TOO_MANY_WRONG = "You entered the wrong code too many times. Request a new code."
EXPIRED_CODE = "That code has expired. Request a new code."
NOT_SENT = "We couldn't send the code. Try again in a few minutes."


def _verify_step(kind: str) -> str:
    return "verify" if kind == "register" else "recover_verify"


def _flow(kind: str, next_step: str, **fields: Any) -> dict[str, Any]:
    return {"version": 1, "flow": kind, "next_step": next_step, **fields}


def _pending(pending_id: str, *, attempts: int = 0, resend_after_s: int = 0) -> dict[str, Any]:
    return {
        "status": "pending",
        "pending_id": pending_id,
        "expires_at": "2099-01-01T00:10:00.000Z",
        "expires_in_s": 600,
        "code_length": 6,
        "attempts": attempts,
        "max_attempts": 5,
        "resend_after_s": resend_after_s,
    }


def sent_text(kind: str = "register", email: str = "a@b.co", handle: str = "alice") -> str:
    if kind == "register":
        return f"We sent a 6-digit code to {email}. It expires in 10 minutes."
    return f"If @{handle} is registered with {email}, we sent a 6-digit code there. It expires in 10 minutes."


def code_sent(
    kind: str = "register",
    pending_id: str = "pnd_1",
    *,
    email: str = "a@b.co",
    handle: str = "alice",
    resend_after_s: int = 0,
) -> Route:
    body = {
        "pending_id": pending_id,
        "message": sent_text(kind, email, handle),
        **_flow(kind, _verify_step(kind), **_pending(pending_id, resend_after_s=resend_after_s)),
    }
    return answer(200, body)


def wrong_code(kind: str = "register", pending_id: str = "pnd_1", attempts: int = 1) -> Route:
    details = _flow(kind, _verify_step(kind), **_pending(pending_id, attempts=attempts), param="code")
    return api_error(400, "INVALID_CODE", WRONG_CODE, details)


def attempts_exhausted(kind: str = "register") -> Route:
    details = _flow(
        kind, kind, status="max_attempts_reached", attempts=5, max_attempts=5, resend_after_s=0
    )
    return api_error(429, "OTP_ATTEMPTS_EXHAUSTED", TOO_MANY_WRONG, details)


def expired(kind: str = "register") -> Route:
    return api_error(400, "EXPIRED", EXPIRED_CODE, _flow(kind, kind, status="expired"))


def cooldown(kind: str = "register", seconds: int = 5) -> Route:
    body = {
        "code": "OTP_COOLDOWN",
        "message": f"You can request a new code in {seconds} seconds.",
        "details": _flow(kind, kind, retry_after_s=seconds),
    }
    return lambda _request: httpx.Response(429, json=body, headers={"Retry-After": str(seconds)})


def not_sent(kind: str = "register") -> Route:
    return api_error(500, "OTP_FAILED", NOT_SENT, _flow(kind, kind))


def handle_taken(handle: str = "alice") -> Route:
    message = f"The handle @{handle} is taken. Try another."
    return api_error(409, "HANDLE_TAKEN", message, _flow("register", "register", param="handle"))


def email_limit(limit: int = 10) -> Route:
    message = (
        f"This email address already has {limit} active agents, the most allowed. "
        "Use a different email address."
    )
    details = _flow("register", "register", param="email", limit=limit)
    return api_error(409, "EMAIL_LIMIT_REACHED", message, details)


def handle_required(handles: list[str]) -> Route:
    message = "More than one agent uses this email address. Choose the handle to recover."
    details = _flow("recover", "recover", param="handle", handles=handles)
    return api_error(409, "HANDLE_REQUIRED", message, details)


def registered(handle: str, key: str) -> Route:
    return answer(
        201,
        {
            "agent": {"handle": handle, "email": "a@b.co"},
            "api_key": key,
            "message": f"Email verified. Logged in as @{handle}.",
            **_flow("register", "done", status="approved"),
        },
    )


def recovered(handle: str, key: str) -> Route:
    return answer(
        200,
        {
            "handle": handle,
            "api_key": key,
            "message": f"Email verified. Logged in as @{handle}. Your old API key no longer works.",
            **_flow("recover", "done", status="approved"),
        },
    )
