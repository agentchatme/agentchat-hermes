from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from agentchatme_hermes.lookup_cache import LookupCache
from agentchatme_hermes.thread_closures import ThreadClosures
from agentchatme_hermes.tools.contacts import (
    _build_add_contact,
    _build_check_contact,
    _build_list_contacts,
    _build_remove_contact,
    _build_update_contact_notes,
)
from agentchatme_hermes.tools.directory import _build_search_directory


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        client=MagicMock(),
        lookup_cache=LookupCache(),
        thread_closures=ThreadClosures(path=None),
    )


class TestDirectoryCaching:
    def test_search_directory_uses_cache_for_same_query(self) -> None:
        runtime = _runtime()
        runtime.client.search_agents.return_value = [{"handle": "alice"}]
        handler = _build_search_directory(runtime)

        first = json.loads(handler({"q": "ali", "limit": 5}))
        second = json.loads(handler({"q": "ali", "limit": 5}))

        assert first["ok"] is True
        assert second["ok"] is True
        runtime.client.search_agents.assert_called_once_with("ali", limit=5)


class TestContactCaching:
    def test_list_contacts_uses_cache_for_same_page(self) -> None:
        runtime = _runtime()
        runtime.client.list_contacts.return_value = [{"handle": "alice"}]
        handler = _build_list_contacts(runtime)

        json.loads(handler({"limit": 20}))
        json.loads(handler({"limit": 20}))

        runtime.client.list_contacts.assert_called_once_with(limit=20)

    def test_check_contact_uses_cache_for_same_handle(self) -> None:
        runtime = _runtime()
        runtime.client.check_contact.return_value = {"handle": "alice", "notes": None}
        handler = _build_check_contact(runtime)

        json.loads(handler({"handle": "@alice"}))
        json.loads(handler({"handle": "alice"}))

        runtime.client.check_contact.assert_called_once_with("alice")

    def test_add_contact_invalidates_contact_cache(self) -> None:
        runtime = _runtime()
        runtime.client.list_contacts.return_value = [{"handle": "alice"}]
        runtime.client.add_contact.return_value = {"handle": "bob"}
        list_handler = _build_list_contacts(runtime)
        add_handler = _build_add_contact(runtime)

        json.loads(list_handler({"limit": 20}))
        json.loads(list_handler({"limit": 20}))
        json.loads(add_handler({"handle": "bob"}))
        json.loads(list_handler({"limit": 20}))

        assert runtime.client.list_contacts.call_count == 2

    def test_update_notes_invalidates_check_cache(self) -> None:
        runtime = _runtime()
        runtime.client.check_contact.return_value = {"handle": "alice", "notes": None}
        runtime.client.update_contact_notes.return_value = {
            "handle": "alice",
            "notes": "prefers async",
        }
        check_handler = _build_check_contact(runtime)
        update_handler = _build_update_contact_notes(runtime)

        json.loads(check_handler({"handle": "alice"}))
        json.loads(check_handler({"handle": "alice"}))
        json.loads(update_handler({"handle": "alice", "notes": "prefers async"}))
        json.loads(check_handler({"handle": "alice"}))

        assert runtime.client.check_contact.call_count == 2

    def test_remove_contact_invalidates_check_cache(self) -> None:
        runtime = _runtime()
        runtime.client.check_contact.return_value = {"handle": "alice", "notes": None}
        runtime.client.remove_contact.return_value = None
        check_handler = _build_check_contact(runtime)
        remove_handler = _build_remove_contact(runtime)

        json.loads(check_handler({"handle": "alice"}))
        json.loads(remove_handler({"handle": "alice"}))
        json.loads(check_handler({"handle": "alice"}))

        assert runtime.client.check_contact.call_count == 2
