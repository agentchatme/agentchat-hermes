"""Tests for ``agentchatme_hermes.texts`` — the kit's TextsCache, for Hermes.

The cache itself (fetch, file, daily refresh, listeners) is the SDK kit's and
tested there. What is Hermes' own:

* the runtime name and the file name in the account's state directory;
* the one-shot load the CLI, the wizard and ``doctor`` use: the server, else
  the cached copy, else ``None`` — never raising;
* the pre-kit ``texts.json`` is never read (it may name the tools bare) and
  is removed on start.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from agentchatme.types import Texts

from agentchatme_hermes.texts import (
    LEGACY_TEXTS_FILENAME,
    TEXTS_FILENAME,
    TEXTS_RUNTIME,
    load_texts,
    remove_legacy_cache,
    texts_cache,
)

if TYPE_CHECKING:
    from pathlib import Path

SERVED = {
    "version": "1",
    "runtime": "hermes",
    "manual": {"url": "https://agentchat.me/skill.md", "version": "2.3.1"},
    "wake_instruction": "You were woken. Call mcp__agentchat__agentchat_inbox first.",
    "invite_prompt": "An invite is pending. Call mcp__agentchat__agentchat_list_group_invites first.",
    "identity_line": "I am @me on AgentChat.",
    "identity_line_template": "I am @<handle> on AgentChat.",
}


class _Client:
    """A sync SDK client double: ``texts.get(runtime=...)``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.texts = SimpleNamespace(get=self._get)

    def _get(self, *, runtime: str, opts: Any = None) -> Texts:
        self.calls.append(runtime)
        if self.fail:
            raise ConnectionError("offline")
        return Texts.model_validate(SERVED)


def test_the_cache_is_for_hermes_in_the_account_directory(tmp_path: Path) -> None:
    cache = texts_cache(_Client(), tmp_path)
    assert cache.runtime == TEXTS_RUNTIME == "hermes"
    assert cache.file_path == tmp_path / TEXTS_FILENAME
    assert TEXTS_FILENAME != LEGACY_TEXTS_FILENAME


def test_one_shot_load_from_the_server_writes_the_file(tmp_path: Path) -> None:
    client = _Client()
    texts = load_texts(client, state_dir=tmp_path)
    assert texts is not None and texts.wake_instruction == SERVED["wake_instruction"]
    assert client.calls == ["hermes"]
    saved = json.loads((tmp_path / TEXTS_FILENAME).read_text(encoding="utf-8"))
    assert saved["runtime"] == "hermes" and saved["texts"]["identity_line"] == SERVED["identity_line"]


def test_offline_the_cached_copy_is_used(tmp_path: Path) -> None:
    load_texts(_Client(), state_dir=tmp_path)
    texts = load_texts(_Client(fail=True), state_dir=tmp_path)
    assert texts is not None and texts.identity_line == SERVED["identity_line"]


def test_offline_with_nothing_cached_is_none(tmp_path: Path) -> None:
    assert load_texts(_Client(fail=True), state_dir=tmp_path) is None
    assert load_texts(_Client(fail=True), state_dir=None) is None


def test_the_pre_kit_file_is_never_read_and_is_removed(tmp_path: Path) -> None:
    legacy = tmp_path / LEGACY_TEXTS_FILENAME
    bare = {**SERVED, "wake_instruction": "Call agentchat_inbox first."}
    legacy.write_text(
        json.dumps({"version": 1, "runtime": "hermes", "fetched_at": 1.0, "texts": bare}),
        encoding="utf-8",
    )
    assert load_texts(_Client(fail=True), state_dir=tmp_path) is None, "a copy with bare tool names never reaches the model"
    remove_legacy_cache(tmp_path)
    assert not legacy.exists()
    remove_legacy_cache(tmp_path)  # idempotent
