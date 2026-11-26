"""``scripts/refresh_bundled_texts.py``: this release's texts brought to what the server serves.

What must hold: the manual comes through the SDK kit (announced version and
sha256) and the plugin's inline-shell guard; the snapshot is written by the
plugin's writer and read back by its reader; the three pins in
``tests/test_skill_bundle.py`` follow; the same texts give the same bytes;
anything refused writes nothing. The files are copies under a temporary root;
the server is the fake API at the httpx transport (``sdk_wire``): nothing
reaches a network.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fake_keys import api_key
from sdk_wire import Wire, answer, connect_error, raw, serve

from agentchatme_hermes import texts
from agentchatme_hermes.manual import BUNDLED_MANUAL_PATH

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.agentchat.me"
NOW = datetime(2026, 12, 1, 9, 30, 0, tzinfo=timezone.utc)
BANG_TICK = "!" + "\x60"  # never side by side in this source: Hermes' install scanner reads it


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("refresh_bundled_texts_under_test", ROOT / "scripts" / "refresh_bundled_texts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


script = _load()
SNAPSHOT = texts.parse_snapshot(texts.SNAPSHOT_PATH.read_bytes())
BUNDLE = BUNDLED_MANUAL_PATH.read_bytes()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for relative in (script.MANUAL_REL, script.SNAPSHOT_REL, script.PINS_REL):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, tmp_path / relative)
    return tmp_path


def _files(root: Path) -> dict[Path, bytes]:
    return {relative: (root / relative).read_bytes() for relative in (script.MANUAL_REL, script.SNAPSHOT_REL, script.PINS_REL)}


def _body(version: str, text: str = "the manual") -> bytes:
    return f"---\nname: agentchat\nversion: {version}\ndescription: x\n---\n{text}\n".encode()


def _payload(
    *,
    manual: bytes = BUNDLE,
    version: str | None = None,
    sha: str | None = "auto",
    template: str = SNAPSHOT.identity_line_template,
    runtime: str = "hermes",
) -> dict[str, Any]:
    announced: dict[str, Any] = {
        "url": "https://agentchat.me/skill.md",
        "version": version or SNAPSHOT.manual["version"],
    }
    if sha == "auto":
        announced["sha256"] = hashlib.sha256(manual).hexdigest()
    elif sha is not None:
        announced["sha256"] = sha
    return {
        "version": "1",
        "runtime": runtime,
        "manual": announced,
        "wake_instruction": "You were woken.",
        "invite_prompt": "An invite is pending.",
        "identity_line": template.replace("<handle>", "someone"),
        "identity_line_template": template,
    }


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> tuple[Any, Wire]:
    from agentchatme import AgentChatClient

    wire = serve(monkeypatch, routes)
    client = AgentChatClient(api_key=api_key("refresh"), base_url=API)
    try:
        return script.refresh(client=client, api_base=API, root=root, now=NOW), wire
    finally:
        client.close()


def _refused(root: Path, monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any], why: str) -> None:
    before = _files(root)
    with pytest.raises(script.RefreshError, match=why):
        _run(root, monkeypatch, routes)
    assert _files(root) == before, "a refusal writes nothing"


def test_the_paths_are_the_packages() -> None:
    assert ROOT / script.MANUAL_REL == BUNDLED_MANUAL_PATH
    assert ROOT / script.SNAPSHOT_REL == texts.SNAPSHOT_PATH
    assert (ROOT / script.PINS_REL).is_file()


def test_current_texts_write_nothing_and_download_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _files(root)
    outcome, wire = _run(root, monkeypatch, {"/v1/texts": answer(200, _payload())})
    assert outcome.written == () and not outcome.changed
    assert _files(root) == before
    assert [path for _method, path in wire.calls()] == ["/v1/texts"]
    assert "current" in outcome.summary()


def test_a_new_manual_refreshes_all_three_files(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body("9.9.9")
    outcome, wire = _run(
        root,
        monkeypatch,
        {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")},
    )
    assert outcome.written == (script.MANUAL_REL, script.SNAPSHOT_REL, script.PINS_REL)
    assert outcome.manual_before == SNAPSHOT.manual["version"] and outcome.manual_after == "9.9.9"
    assert not outcome.template_changed
    assert (root / script.MANUAL_REL).read_bytes() == body
    snapshot_raw = (root / script.SNAPSHOT_REL).read_bytes()
    snapshot = texts.parse_snapshot(snapshot_raw)
    assert snapshot.manual == {"url": "https://agentchat.me/skill.md", "version": "9.9.9", "sha256": hashlib.sha256(body).hexdigest()}
    assert snapshot.fetched_at == "2026-12-01T09:30:00Z"
    pins = (root / script.PINS_REL).read_text(encoding="utf-8")
    assert 'MANUAL_VERSION = "9.9.9"' in pins
    assert f'MANUAL_SHA256 = "{hashlib.sha256(body).hexdigest()}"' in pins
    assert f'TEXTS_SNAPSHOT_SHA256 = "{hashlib.sha256(snapshot_raw).hexdigest()}"' in pins
    assert ("GET", "/skill.md") in wire.calls()
    # No temp file of the kit's left anywhere under the root.
    assert sorted(p.name for p in (root / script.MANUAL_REL).parent.iterdir()) == ["SKILL.md"]


def test_a_pins_file_with_crlf_line_breaks_keeps_them(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Windows checkout converts the test file's line breaks to CRLF."""
    pins = root / script.PINS_REL
    pins.write_bytes(pins.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    body = _body("9.9.9")
    _run(
        root,
        monkeypatch,
        {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")},
    )
    data = pins.read_bytes()
    assert b'MANUAL_VERSION = "9.9.9"\r\n' in data
    assert b"\n" not in data.replace(b"\r\n", b""), "every line break is still CRLF"


def test_the_same_texts_give_the_same_bytes(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body("9.9.9")
    routes = {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")}
    _run(root, monkeypatch, routes)
    first = _files(root)
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    from agentchatme import AgentChatClient

    serve(monkeypatch, routes)
    client = AgentChatClient(api_key=api_key("refresh"), base_url=API)
    try:
        again = script.refresh(client=client, api_base=API, root=root, now=later)
    finally:
        client.close()
    assert again.written == () and _files(root) == first, "fetched_at moves only with the texts"


def test_a_new_identity_line_changes_the_snapshot_and_its_pin_only(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = "I am @<handle> on AgentChat, reworded. On any AgentChat wake I call mcp__agentchat__agentchat_inbox first."
    before = _files(root)
    outcome, _ = _run(root, monkeypatch, {"/v1/texts": answer(200, _payload(template=template))})
    assert outcome.written == (script.SNAPSHOT_REL, script.PINS_REL) and outcome.template_changed
    assert (root / script.MANUAL_REL).read_bytes() == before[script.MANUAL_REL]
    assert texts.parse_snapshot((root / script.SNAPSHOT_REL).read_bytes()).identity_line_template == template


@pytest.mark.parametrize(
    ("route", "why"),
    [
        (raw(503, b"down"), "could not be had"),
        (raw(200, _body("9.9.9", "NOT THE ANNOUNCED BYTES"), "text/markdown"), "could not be had"),
        (connect_error(), "could not be had"),
    ],
)
def test_a_manual_that_cannot_be_had_writes_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, route: Any, why: str
) -> None:
    body = _body("9.9.9")
    _refused(root, monkeypatch, {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": route}, why)


def test_a_manual_carrying_inline_shell_is_not_bundled(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body("9.9.9", f"run {BANG_TICK}id\x60 now")
    _refused(
        root,
        monkeypatch,
        {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")},
        "inline shell",
    )


def test_an_announcement_without_a_sha256_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refused(root, monkeypatch, {"/v1/texts": answer(200, _payload(sha=None))}, "without a sha256")


def test_texts_that_do_not_answer_are_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refused(root, monkeypatch, {"/v1/texts": connect_error()}, "did not answer")


def test_texts_for_another_runtime_are_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refused(root, monkeypatch, {"/v1/texts": answer(200, _payload(runtime="openclaw"))}, "did not answer|runtime hermes")


def test_a_template_that_names_no_handle_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refused(
        root, monkeypatch, {"/v1/texts": answer(200, _payload(template="I am an agent on AgentChat."))}, "usable snapshot"
    )


def test_a_pin_line_that_moved_refuses_before_any_write(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pins = root / script.PINS_REL
    pins.write_text(pins.read_text(encoding="utf-8").replace("MANUAL_SHA256 = ", "MANUAL_SHA = "), encoding="utf-8")
    body = _body("9.9.9")
    _refused(
        root,
        monkeypatch,
        {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")},
        "MANUAL_SHA256",
    )


def test_main_needs_the_key_in_the_environment(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv(script.ENV_API_KEY, raising=False)
    assert script.main([]) == 2
    assert script.ENV_API_KEY in capsys.readouterr().err


def test_main_says_what_it_did_for_the_workflow(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = api_key("refresh")
    monkeypatch.setenv(script.ENV_API_KEY, key)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(script, "ROOT", root)
    serve(monkeypatch, {"/v1/texts": answer(200, _payload())})
    assert script.main(["--api-base", API]) == 0
    printed = capsys.readouterr()
    assert "bundled texts current" in printed.out
    assert key not in printed.out + printed.err
    assert output.read_text(encoding="utf-8") == (
        f"changed=false\nmanual={SNAPSHOT.manual['version']}\nsummary=bundled texts current: manual "
        f"{SNAPSHOT.manual['version']}, identity line as served\n"
    )


def test_main_says_a_refusal_and_exits_1(root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(script.ENV_API_KEY, api_key("refresh"))
    monkeypatch.setattr(script, "ROOT", root)
    serve(monkeypatch, {"/v1/texts": answer(200, _payload(sha=None))})
    assert script.main(["--api-base", API]) == 1
    assert "nothing written" in capsys.readouterr().err


def _failing_writes(monkeypatch: pytest.MonkeyPatch, fail: Any) -> None:
    """The kit's ``write_atomic`` as the script calls it, raising where ``fail(path, data)`` says."""
    import agentchatme.integration as integration

    real = integration.write_atomic

    def write_atomic(path: Any, data: Any, *, mode: int = 0o644) -> None:
        if fail(Path(path), data):
            raise OSError(28, "No space left on device")
        real(path, data, mode=mode)

    monkeypatch.setattr(integration, "write_atomic", write_atomic)


def _new_manual_routes() -> dict[str, Any]:
    body = _body("9.9.9")
    return {"/v1/texts": answer(200, _payload(manual=body, version="9.9.9")), "/skill.md": raw(200, body, "text/markdown")}


def test_a_failed_write_puts_back_what_the_run_wrote(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pins cannot be written: the manual goes back to its bytes, and the
    snapshot, which did not exist before the run, is removed again."""
    (root / script.SNAPSHOT_REL).unlink()
    before = {relative: (root / relative).read_bytes() for relative in (script.MANUAL_REL, script.PINS_REL)}
    _failing_writes(monkeypatch, lambda path, _data: path == root / script.PINS_REL)
    with pytest.raises(script.RefreshError, match="could not write") as caught:
        _run(root, monkeypatch, _new_manual_routes())
    assert caught.value.left == ()
    assert not (root / script.SNAPSHOT_REL).exists()
    assert {relative: (root / relative).read_bytes() for relative in before} == before


def test_a_put_back_that_fails_names_the_file_left(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pins cannot be written, and the manual cannot be put back either:
    the refusal names the manual as holding this run's bytes."""
    manual = root / script.MANUAL_REL
    original = manual.read_bytes()
    _failing_writes(
        monkeypatch, lambda path, data: path == root / script.PINS_REL or (path == manual and data == original)
    )
    with pytest.raises(script.RefreshError) as caught:
        _run(root, monkeypatch, _new_manual_routes())
    (left,) = caught.value.left
    assert str(manual) in left and "No space left" in left
    assert manual.read_bytes() == _body("9.9.9"), "the file named is the one left as the run wrote it"

    monkeypatch.setenv(script.ENV_API_KEY, api_key("refresh"))
    monkeypatch.setattr(script, "ROOT", root)
    manual.write_bytes(original)
    (root / script.SNAPSHOT_REL).write_bytes((ROOT / script.SNAPSHOT_REL).read_bytes())
    serve(monkeypatch, _new_manual_routes())
    assert script.main(["--api-base", API]) == 1
    err = capsys.readouterr().err
    assert "could not be put back" in err and str(manual) in err and "nothing written" not in err
