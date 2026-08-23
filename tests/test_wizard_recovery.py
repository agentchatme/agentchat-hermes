"""API-key recovery in ``wizard``: the network calls and the interactive flow.

Recovery re-keys an existing agent from its email **and** @handle — an
email can back several agents, so the handle is sent on every start
request. Step 1 (``POST /v1/agents/recover``) is posted with ``httpx``
directly; step 2 goes through ``AgentChatClient.recover_verify``. Both
are patched here so no network IO happens. The interactive flow takes
its prompt / print / persist callables as arguments, so it runs outside
a Hermes process with scripted stand-ins.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from agentchatme_hermes import wizard
from agentchatme_hermes.wizard import (
    _HANDLE_REQUIRED,
    _handle_required_lines,
    _prompt_email,
    _prompt_recovery_handle,
    _recover_lost_key_flow,
    _recover_start,
    _recover_verify,
    _RecoverError,
)


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: Any = None, *, invalid_json: bool = False
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self) -> Any:
        if self._invalid_json:
            raise ValueError("not json")
        return self._payload


def _patch_post(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


class _ScriptedPrompt:
    """Stand-in for Hermes' ``prompt(question, default=None)``: hands out
    scripted answers in order and, like Hermes, returns ``default`` on a
    bare ENTER when one was offered."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, question: str, default: Any = None, password: bool = False) -> str:
        self.calls.append((question, default))
        answer = self.answers.pop(0)
        if answer == "" and default is not None:
            return str(default)
        return answer


class _Out:
    """Collects the wizard's print_* output and persisted env values."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warning: list[str] = []
        self.env: dict[str, str] = {}

    def save_env_value(self, key: str, value: str) -> None:
        self.env[key] = value


# ─── _RecoverError ─────────────────────────────────────────────────────────


class TestRecoverError:
    def test_carries_code_and_handles(self) -> None:
        err = _RecoverError("nope", code="HANDLE_REQUIRED", handles=["a", "b"])
        assert str(err) == "nope"
        assert err.code == "HANDLE_REQUIRED"
        assert err.handles == ["a", "b"]

    def test_defaults(self) -> None:
        err = _RecoverError("x")
        assert err.code is None
        assert err.handles == []


# ─── _recover_start ────────────────────────────────────────────────────────


class TestRecoverStart:
    def test_sends_handle_with_email_to_recover_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCHATME_API_BASE", "https://api.test")
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"pending_id": "pnd_1", "message": "m"}))

        assert _recover_start(email="a@b.co", handle="alice") == "pnd_1"

        assert len(calls) == 1
        assert calls[0]["url"] == "https://api.test/v1/agents/recover"
        assert calls[0]["json"] == {"email": "a@b.co", "handle": "alice"}
        assert calls[0]["timeout"] == 15.0

    def test_client_headers_identify_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes._version import __version__

        calls = _patch_post(monkeypatch, _FakeResponse(200, {"pending_id": "pnd_1"}))
        _recover_start(email="a@b.co", handle="alice")
        assert calls[0]["headers"] == {
            "X-AgentChat-Client": "hermes",
            "X-AgentChat-Client-Version": __version__,
        }

    def test_missing_pending_id_reports_no_code_issued(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-policy servers omit pending_id when nothing matched."""
        _patch_post(monkeypatch, _FakeResponse(200, {"message": "if it exists, a code was sent"}))
        with pytest.raises(_RecoverError, match="no recovery code was issued"):
            _recover_start(email="a@b.co", handle="alice")

    def test_invalid_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(monkeypatch, _FakeResponse(200, invalid_json=True))
        with pytest.raises(_RecoverError, match="invalid server response"):
            _recover_start(email="a@b.co", handle="alice")

    def test_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(monkeypatch, _FakeResponse(429, {"code": "RATE_LIMITED", "message": "slow down"}))
        with pytest.raises(_RecoverError) as info:
            _recover_start(email="a@b.co", handle="alice")
        assert info.value.code == "RATE_LIMITED"
        assert "wait a minute" in str(info.value)

    def test_validation_error_surfaces_field_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(
            monkeypatch,
            _FakeResponse(
                400,
                {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request",
                    "details": {"fieldErrors": {"handle": ["Invalid handle"]}},
                },
            ),
        )
        with pytest.raises(_RecoverError) as info:
            _recover_start(email="a@b.co", handle="alice")
        assert info.value.code == "VALIDATION_ERROR"
        assert "handle: Invalid handle" in str(info.value)

    def test_unknown_error_without_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(monkeypatch, _FakeResponse(502, invalid_json=True))
        with pytest.raises(_RecoverError, match="HTTP 502"):
            _recover_start(email="a@b.co", handle="alice")

    def test_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("dns")

        monkeypatch.setattr(httpx, "post", boom)
        with pytest.raises(_RecoverError, match="network error"):
            _recover_start(email="a@b.co", handle="alice")


# ─── _recover_verify ───────────────────────────────────────────────────────


class TestRecoverVerify:
    def test_success_returns_key_and_handle_and_closes_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from agentchatme import AgentChatClient

        client = MagicMock()
        seen: dict[str, Any] = {}

        def fake(pending_id: str, code: str, *, base_url: str, client_identity: Any) -> Any:
            seen.update(pending_id=pending_id, code=code, base_url=base_url)
            return "alice", "ac_live_newkey_000000000000", client

        monkeypatch.setattr(AgentChatClient, "recover_verify", staticmethod(fake))

        assert _recover_verify(pending_id="pnd_1", code="123456") == (
            "ac_live_newkey_000000000000",
            "alice",
        )
        assert seen["pending_id"] == "pnd_1"
        assert seen["code"] == "123456"
        client.close.assert_called_once_with()

    def test_handle_required_carries_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme import AgentChatClient, AgentChatError

        def fake(*_: Any, **__: Any) -> Any:
            raise AgentChatError(
                {
                    "code": "HANDLE_REQUIRED",
                    "message": "This email backs more than one agent.",
                    "details": {"handles": ["alice", "alice-2"]},
                },
                409,
            )

        monkeypatch.setattr(AgentChatClient, "recover_verify", staticmethod(fake))
        with pytest.raises(_RecoverError) as info:
            _recover_verify(pending_id="pnd_1", code="123456")
        err = info.value
        assert err.code == _HANDLE_REQUIRED
        assert err.handles == ["alice", "alice-2"]
        assert str(err) == "This email backs more than one agent."

    def test_invalid_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme import AgentChatClient, AgentChatError

        def fake(*_: Any, **__: Any) -> Any:
            raise AgentChatError({"code": "INVALID_CODE", "message": "Invalid code"}, 400)

        monkeypatch.setattr(AgentChatClient, "recover_verify", staticmethod(fake))
        with pytest.raises(_RecoverError) as info:
            _recover_verify(pending_id="pnd_1", code="000000")
        assert info.value.code == "INVALID_CODE"
        assert info.value.handles == []

    def test_non_sdk_exception_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme import AgentChatClient

        def fake(*_: Any, **__: Any) -> Any:
            raise RuntimeError("socket closed")

        monkeypatch.setattr(AgentChatClient, "recover_verify", staticmethod(fake))
        with pytest.raises(_RecoverError, match="socket closed") as info:
            _recover_verify(pending_id="pnd_1", code="123456")
        assert info.value.code is None

    def test_missing_api_key_in_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from agentchatme import AgentChatClient

        def fake(*_: Any, **__: Any) -> Any:
            return "alice", "", MagicMock()

        monkeypatch.setattr(AgentChatClient, "recover_verify", staticmethod(fake))
        with pytest.raises(_RecoverError, match="missing api_key"):
            _recover_verify(pending_id="pnd_1", code="123456")


# ─── _handle_required_lines ────────────────────────────────────────────────


class TestHandleRequiredLines:
    def test_lists_handles_and_instruction(self) -> None:
        err = _RecoverError("server msg", code=_HANDLE_REQUIRED, handles=["alice", "bob"])
        lines = _handle_required_lines(err)
        assert lines[0] == "server msg"
        assert lines[1] == "Agents registered under this email: @alice, @bob"
        assert "Run recovery again" in lines[-1]

    def test_no_handles_no_empty_line(self) -> None:
        err = _RecoverError("", code=_HANDLE_REQUIRED)
        lines = _handle_required_lines(err)
        assert len(lines) == 2
        assert "more than one agent" in lines[0]


# ─── prompts ───────────────────────────────────────────────────────────────


class TestPromptRecoveryHandle:
    def test_default_accepted_on_enter(self) -> None:
        prompt = _ScriptedPrompt([""])
        warnings: list[str] = []
        assert _prompt_recovery_handle(prompt, warnings.append, default="alice") == "alice"
        assert prompt.calls == [("@handle of the agent to recover", "alice")]
        assert warnings == []

    def test_no_default_when_none_configured(self) -> None:
        prompt = _ScriptedPrompt(["@Bob"])
        assert _prompt_recovery_handle(prompt, lambda _m: None) == "bob"
        # No default offered → Hermes' prompt is called without one.
        assert prompt.calls == [("@handle of the agent to recover", None)]

    def test_override_default_with_another_handle(self) -> None:
        prompt = _ScriptedPrompt(["carol"])
        assert _prompt_recovery_handle(prompt, lambda _m: None, default="alice") == "carol"

    def test_invalid_then_valid(self) -> None:
        prompt = _ScriptedPrompt(["a", "alice"])
        warnings: list[str] = []
        assert _prompt_recovery_handle(prompt, warnings.append) == "alice"
        assert len(warnings) == 1

    def test_gives_up_after_three(self) -> None:
        prompt = _ScriptedPrompt(["!", "!", "!"])
        warnings: list[str] = []
        assert _prompt_recovery_handle(prompt, warnings.append) is None
        assert "Too many invalid handle attempts" in warnings[-1]


class TestPromptEmailDefault:
    def test_default_accepted_on_enter(self) -> None:
        prompt = _ScriptedPrompt([""])
        assert _prompt_email(prompt, lambda _m: None, default="a@b.co") == "a@b.co"
        assert prompt.calls[0][1] == "a@b.co"

    def test_without_default_unchanged(self) -> None:
        prompt = _ScriptedPrompt(["a@b.co"])
        assert _prompt_email(prompt, lambda _m: None) == "a@b.co"
        assert prompt.calls[0][1] is None


# ─── _recover_lost_key_flow ────────────────────────────────────────────────


def _run_flow(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
    *,
    start: Any,
    verify: Any,
    existing_handle: str = "",
    default_email: str = "",
) -> tuple[bool, _Out, _ScriptedPrompt, list[str]]:
    out = _Out()
    prompt = _ScriptedPrompt(answers)
    anchored: list[str] = []

    monkeypatch.setattr(wizard, "_recover_start", start)
    monkeypatch.setattr(wizard, "_recover_verify", verify)
    # Never touch the real ~/.hermes/SOUL.md from a unit test.
    monkeypatch.setattr(
        wizard, "write_soul_anchor", lambda handle: anchored.append(handle) or "/tmp/SOUL.md"
    )

    ok = _recover_lost_key_flow(
        prompt=prompt,
        print_info=out.info.append,
        print_success=out.success.append,
        print_warning=out.warning.append,
        save_env_value=out.save_env_value,
        existing_handle=existing_handle,
        default_email=default_email,
    )
    return ok, out, prompt, anchored


class TestRecoverLostKeyFlow:
    def test_happy_path_persists_new_key_and_anchor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        starts: list[dict[str, str]] = []

        def start(*, email: str, handle: str) -> str:
            starts.append({"email": email, "handle": handle})
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            assert (pending_id, code) == ("pnd_1", "123456")
            return "ac_live_newkey_000000000000", "alice"

        ok, out, _prompt, anchored = _run_flow(
            monkeypatch, ["a@b.co", "alice", "123456"], start=start, verify=verify
        )

        assert ok is True
        assert starts == [{"email": "a@b.co", "handle": "alice"}]
        assert out.env == {
            "AGENTCHATME_API_KEY": "ac_live_newkey_000000000000",
            "AGENTCHATME_HANDLE": "alice",
        }
        assert anchored == ["alice"]
        assert out.warning == []
        assert any("6-digit code is on its way" in line for line in out.info)

    def test_existing_handle_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        starts: list[str] = []

        def start(*, email: str, handle: str) -> str:
            starts.append(handle)
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            return "ac_live_newkey_000000000000", "alice"

        # Email typed, handle accepted on bare ENTER, then the code.
        ok, _out, prompt, _ = _run_flow(
            monkeypatch,
            ["a@b.co", "", "123456"],
            start=start,
            verify=verify,
            existing_handle="alice",
        )
        assert ok is True
        assert starts == ["alice"]
        assert prompt.calls[1] == ("@handle of the agent to recover", "alice")

    def test_default_email_is_offered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        starts: list[str] = []

        def start(*, email: str, handle: str) -> str:
            starts.append(email)
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            return "ac_live_newkey_000000000000", "alice"

        ok, _out, prompt, _ = _run_flow(
            monkeypatch,
            ["", "alice", "123456"],
            start=start,
            verify=verify,
            default_email="full@b.co",
        )
        assert ok is True
        assert starts == ["full@b.co"]
        assert prompt.calls[0][1] == "full@b.co"

    def test_handle_required_prints_handles_and_persists_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def start(*, email: str, handle: str) -> str:
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            raise _RecoverError(
                "This email backs more than one agent.",
                code=_HANDLE_REQUIRED,
                handles=["alice", "alice-2"],
            )

        ok, out, _prompt, anchored = _run_flow(
            monkeypatch, ["a@b.co", "alice", "123456"], start=start, verify=verify
        )
        assert ok is False
        assert out.env == {}
        assert anchored == []
        joined = "\n".join(out.warning)
        assert "@alice, @alice-2" in joined
        assert "Run recovery again" in joined

    def test_invalid_code_fails_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def start(*, email: str, handle: str) -> str:
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            raise _RecoverError("Invalid or expired code", code="INVALID_CODE")

        ok, out, _prompt, _ = _run_flow(
            monkeypatch, ["a@b.co", "alice", "123456"], start=start, verify=verify
        )
        assert ok is False
        assert out.env == {}
        assert out.warning == ["Recovery failed: Invalid or expired code"]

    def test_start_failure_stops_before_otp_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def start(*, email: str, handle: str) -> str:
            raise _RecoverError("rate-limited — wait a minute and try again", code="RATE_LIMITED")

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            raise AssertionError("verify must not be called")

        # Only two answers scripted: a third prompt would IndexError.
        ok, out, _prompt, _ = _run_flow(
            monkeypatch, ["a@b.co", "alice"], start=start, verify=verify
        )
        assert ok is False
        assert out.warning == ["Recovery request failed: rate-limited — wait a minute and try again"]

    def test_unexpected_start_exception_reported_as_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def start(*, email: str, handle: str) -> str:
            raise OSError("no route to host")

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            raise AssertionError("verify must not be called")

        ok, out, _prompt, _ = _run_flow(
            monkeypatch, ["a@b.co", "alice"], start=start, verify=verify
        )
        assert ok is False
        assert out.warning == ["Could not reach AgentChat: no route to host"]

    def test_bad_otp_three_times_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def start(*, email: str, handle: str) -> str:
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            raise AssertionError("verify must not be called")

        ok, out, _prompt, _ = _run_flow(
            monkeypatch, ["a@b.co", "alice", "12", "abcdef", "1234567"], start=start, verify=verify
        )
        assert ok is False
        assert "Too many invalid code attempts" in out.warning[-1]


# ─── menu routing ──────────────────────────────────────────────────────────


class _Menus:
    """Fixture-ish helper: drives ``_fresh_setup_menu`` / ``_replace_key_branch``
    with a fixed picker index and records which flow ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pick: int) -> None:
        self.pick = pick
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.out = _Out()

        def recover(**kwargs: Any) -> bool:
            self.ran.append(("recover", kwargs))
            return True

        def register(**kwargs: Any) -> bool:
            self.ran.append(("register", kwargs))
            return True

        def paste(*args: Any) -> bool:
            self.ran.append(("paste", {}))
            return True

        monkeypatch.setattr(wizard, "_recover_lost_key_flow", recover)
        monkeypatch.setattr(wizard, "_register_new_agent_flow", register)
        monkeypatch.setattr(wizard, "_paste_existing_key_flow", paste)

    def prompt_choice(self, question: str, choices: list[str], default: int = 0, **_: Any) -> int:
        self.choices = list(choices)
        return self.pick

    def kwargs(self) -> dict[str, Any]:
        return {
            "prompt": _ScriptedPrompt([]),
            "prompt_yes_no": lambda *_a, **_k: False,
            "prompt_choice": self.prompt_choice,
            "print_info": self.out.info.append,
            "print_success": self.out.success.append,
            "print_warning": self.out.warning.append,
            "save_env_value": self.out.save_env_value,
            "get_env_value": lambda _k: "",
        }


class TestMenuRouting:
    def test_fresh_menu_recover_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=2)
        wizard._fresh_setup_menu(**menus.kwargs())
        assert [name for name, _ in menus.ran] == ["recover"]
        assert "Recover a lost API key" in menus.choices[2]
        assert menus.out.success == ["AgentChat ready"]

    def test_fresh_menu_skip_is_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=3)
        wizard._fresh_setup_menu(**menus.kwargs())
        assert menus.ran == []
        assert menus.choices[3] == "Skip for now"

    def test_fresh_menu_register_and_paste_positions_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for pick, expected in ((0, "register"), (1, "paste")):
            menus = _Menus(monkeypatch, pick=pick)
            wizard._fresh_setup_menu(**menus.kwargs())
            assert [name for name, _ in menus.ran] == [expected]

    def test_replace_branch_recover_passes_existing_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        menus = _Menus(monkeypatch, pick=2)
        wizard._replace_key_branch(existing_handle="alice", **menus.kwargs())
        assert len(menus.ran) == 1
        name, kwargs = menus.ran[0]
        assert name == "recover"
        assert kwargs["existing_handle"] == "alice"
        assert "rotates the key" in menus.choices[2]

    def test_replace_branch_cancel_is_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=3)
        wizard._replace_key_branch(existing_handle="alice", **menus.kwargs())
        assert menus.ran == []
        assert menus.choices[3].startswith("Cancel")
