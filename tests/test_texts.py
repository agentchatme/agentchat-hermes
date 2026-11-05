"""Tests for ``agentchatme_hermes.texts`` — fetched, cached, refreshed; never bundled."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from agentchatme.types import Texts

from agentchatme_hermes import texts as texts_module
from agentchatme_hermes.texts import (
    REFRESH_INTERVAL_S,
    RETRY_INTERVAL_S,
    TextsStore,
    load_texts,
    read_cache,
    write_cache,
)

if TYPE_CHECKING:
    from pathlib import Path


def _texts(wake: str = "Wake one.", handle: str = "alice") -> Texts:
    return Texts.model_validate(
        {
            "version": "1",
            "runtime": "hermes",
            "manual": {"url": "https://agentchat.me/skill.md", "version": "2.1.2"},
            "wake_instruction": wake,
            "invite_prompt": "Invite.",
            "identity_line": f"I am @{handle} on AgentChat (api.agentchat.me).",
            "identity_line_template": "I am @<handle> on AgentChat (api.agentchat.me).",
        }
    )


class _Client:
    """``client.texts.get`` that serves, or raises, on demand."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[str] = []
        self.texts = SimpleNamespace(get=self._get)

    def _get(self, *, runtime: str) -> Texts:
        self.calls.append(runtime)
        if isinstance(self.result, Exception):
            raise self.result
        assert isinstance(self.result, Texts)
        return self.result


class TestLoadTexts:
    def test_fresh_fetch_writes_the_cache(self, tmp_path: Path) -> None:
        client = _Client(_texts())
        cache = tmp_path / "state" / "texts.json"
        loaded = load_texts(client, cache_path=cache, now=lambda: 1000.0)  # type: ignore[arg-type]
        assert loaded is not None and loaded.source == "server"
        assert client.calls == ["hermes"]
        assert cache.exists() and (cache.stat().st_mode & 0o777) == 0o600
        cached = read_cache(cache)
        assert cached is not None and cached.texts == _texts() and cached.fetched_at == 1000.0

    def test_server_down_uses_the_cache_and_says_so_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = tmp_path / "texts.json"
        write_cache(cache, _texts("Cached wake."), fetched_at=0.0)
        client = _Client(ConnectionError("offline"))
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.texts"):
            loaded = load_texts(client, cache_path=cache, now=lambda: 7200.0)  # type: ignore[arg-type]
        assert loaded is not None and loaded.source == "cache"
        assert loaded.texts.wake_instruction == "Cached wake."
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "cached copy from 2 h ago" in warnings[0]

    def test_server_down_without_cache_is_none_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _Client(ConnectionError("offline"))
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.texts"):
            assert load_texts(client, cache_path=tmp_path / "texts.json") is None  # type: ignore[arg-type]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "nothing is cached" in warnings[0]

    def test_corrupt_cache_reads_as_nothing(self, tmp_path: Path) -> None:
        cache = tmp_path / "texts.json"
        cache.write_text("{not json", encoding="utf-8")
        assert read_cache(cache) is None
        cache.write_text('{"version": 99, "fetched_at": 1, "texts": {}}', encoding="utf-8")
        assert read_cache(cache) is None

    def test_cache_write_failure_does_not_lose_the_served_texts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(texts_module, "write_cache", boom)
        loaded = load_texts(_Client(_texts()), cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
        assert loaded is not None and loaded.source == "server"


class TestStore:
    def test_start_fetches_and_schedules_the_daily_refresh(self, tmp_path: Path) -> None:
        clock = {"now": 1000.0}
        store = TextsStore(
            client=_Client(_texts()),  # type: ignore[arg-type]
            cache_path=tmp_path / "texts.json",
            now=lambda: clock["now"],
        )
        try:
            loaded = store.start()
            assert loaded is not None and loaded.source == "server"
            assert store.get() == _texts()
            assert store.next_delay() == pytest.approx(REFRESH_INTERVAL_S)
            clock["now"] += 3600.0
            assert store.next_delay() == pytest.approx(REFRESH_INTERVAL_S - 3600.0)
        finally:
            store.stop()

    def test_start_offline_with_cache_retries_sooner(self, tmp_path: Path) -> None:
        cache = tmp_path / "texts.json"
        write_cache(cache, _texts("Cached."), fetched_at=0.0)
        store = TextsStore(client=_Client(ConnectionError("down")), cache_path=cache)  # type: ignore[arg-type]
        try:
            loaded = store.start()
            assert loaded is not None and loaded.source == "cache"
            assert store.get() is not None and store.get().wake_instruction == "Cached."  # type: ignore[union-attr]
            assert store.next_delay() == pytest.approx(RETRY_INTERVAL_S)
        finally:
            store.stop()

    def test_start_offline_without_cache_starts_anyway(self, tmp_path: Path) -> None:
        store = TextsStore(client=_Client(ConnectionError("down")), cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
        try:
            assert store.start() is None
            assert store.get() is None
            assert store.next_delay() == pytest.approx(RETRY_INTERVAL_S)
            store.refresh()
            assert store.next_delay() == pytest.approx(RETRY_INTERVAL_S * 2), "backs off"
        finally:
            store.stop()

    def test_first_successful_refresh_notifies_listeners(self, tmp_path: Path) -> None:
        client = _Client(ConnectionError("down"))
        store = TextsStore(client=client, cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
        seen: list[str] = []
        try:
            store.start()
            store.on_texts(lambda t: seen.append(t.wake_instruction))
            assert seen == [], "nothing loaded yet — no callback"
            client.result = _texts("Served now.")
            loaded = store.refresh()
            assert loaded is not None and loaded.source == "server"
            assert seen == ["Served now."]
            store.refresh()
            assert seen == ["Served now."], "unchanged texts do not notify again"
            client.result = _texts("Served again.")
            store.refresh()
            assert seen == ["Served now.", "Served again."]
        finally:
            store.stop()

    def test_on_texts_calls_back_immediately_when_loaded(self, tmp_path: Path) -> None:
        store = TextsStore(client=_Client(_texts()), cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
        seen: list[Texts] = []
        try:
            store.start()
            store.on_texts(seen.append)
            assert seen == [_texts()]
        finally:
            store.stop()

    def test_refresh_failure_keeps_the_served_copy(self, tmp_path: Path) -> None:
        client = _Client(_texts("Served."))
        store = TextsStore(client=client, cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
        try:
            store.start()
            client.result = ConnectionError("blip")
            loaded = store.refresh()
            assert loaded is not None and loaded.source == "server"
            assert loaded.texts.wake_instruction == "Served."
            assert store.next_delay() == pytest.approx(RETRY_INTERVAL_S), "retry soon, not in a day"
        finally:
            store.stop()

    def test_refresh_thread_runs_and_stops(self, tmp_path: Path) -> None:
        store = TextsStore(
            client=_Client(_texts()),  # type: ignore[arg-type]
            cache_path=tmp_path / "t.json",
            refresh_interval_s=0.01,
        )
        store.start()
        deadline = time.monotonic() + 2.0
        while store.refreshes < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.refreshes >= 3
        store.stop()
        assert store._thread is not None
        store._thread.join(timeout=2.0)
        assert not store._thread.is_alive()

    def test_second_start_uses_the_cache_written_by_the_first(self, tmp_path: Path) -> None:
        cache = tmp_path / "t.json"
        first = TextsStore(client=_Client(_texts("From server.")), cache_path=cache)  # type: ignore[arg-type]
        first.start()
        first.stop()
        second = TextsStore(client=_Client(ConnectionError("down")), cache_path=cache)  # type: ignore[arg-type]
        try:
            loaded = second.start()
            assert loaded is not None and loaded.source == "cache"
            assert loaded.texts.wake_instruction == "From server."
        finally:
            second.stop()
