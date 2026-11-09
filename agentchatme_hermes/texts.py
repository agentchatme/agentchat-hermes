"""The served texts, through the SDK kit's ``TextsCache``.

Every word an agent sees about AgentChat is written once, on the server, and
served as data (``GET /v1/texts?runtime=hermes``): the wake instruction, the
invite prompt, the identity line, the gate-call words. They come spelled the
way Hermes names the hosted tools. The kit's
:class:`agentchatme.integration.TextsCache` fetches them, keeps the last good
copy in a file so a gateway that starts offline is not mute, refreshes daily
(5 min doubling to 1 h after a failure), and tells listeners when they
change. This module only says where that file is for Hermes and gives the
CLI, the wizard and ``doctor`` a one-shot load.

The file is ``texts-hermes.json`` in the account's state directory. Releases
before the kit kept ``texts.json`` there; that file may hold a copy from
before the server spelled the tool names for Hermes, so it is never read and
is removed on start (:func:`remove_legacy_cache`).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from agentchatme.integration import TextsCache
    from agentchatme.types import Texts

logger = logging.getLogger(__name__)

TEXTS_RUNTIME = "hermes"
TEXTS_FILENAME = "texts-hermes.json"
LEGACY_TEXTS_FILENAME = "texts.json"


def texts_cache(client: Any, state_dir: Path | None) -> TextsCache:
    """The kit's texts cache for one account, its file in ``state_dir``."""
    from agentchatme.integration import TextsCache

    return TextsCache(
        client,
        runtime=TEXTS_RUNTIME,
        file_path=state_dir / TEXTS_FILENAME if state_dir is not None else None,
        on_error=_log_texts_error,
    )


def load_texts(client: Any, *, state_dir: Path | None) -> Texts | None:
    """One attempt from sync code: the server, else the cached copy, else ``None``.

    For the wizard, the CLI and ``doctor``, which run outside the gateway's
    receive loop. Never raises.
    """
    cache = texts_cache(client, state_dir)
    try:
        cache.refresh_sync()
    except Exception as exc:
        logger.debug("texts: one-shot load raised: %s", exc)
    return cache.current()


def remove_legacy_cache(state_dir: Path) -> None:
    """Remove the pre-kit ``texts.json`` (never read; see the module docstring)."""
    path = state_dir / LEGACY_TEXTS_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("texts: could not remove %s: %s", path, exc)
        return
    logger.info("texts: removed the old cache %s (the kit keeps %s)", path, TEXTS_FILENAME)


def _log_texts_error(error: Exception) -> None:
    # Called once per new failure condition, not once per retry.
    logger.warning("texts: %s — using the copy in hand, if any; retrying", error)
