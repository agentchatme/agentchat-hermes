#!/usr/bin/env python3
"""Refresh the texts this release bundles from the AgentChat server.

By default a Hermes profile reads the AgentChat manual and the SOUL.md
identity line as the plugin release ships them (the plugin setting
``live_texts`` off). This script brings the release's copies to what the
server serves now, so a release made after it carries them:

* ``agentchatme_hermes/skills/agentchat/SKILL.md`` — the manual the served
  texts announce (``GET /v1/texts?runtime=hermes`` → ``manual``), downloaded
  and checked by the SDK kit's ``sync_manual`` (agentchat.me hosts only, the
  size cap, front matter ``name: agentchat`` at the announced version, the
  announced sha256), then refused if it carries inline shell (the plugin's own
  guard, ``manual.carries_inline_shell``);
* ``agentchatme_hermes/texts_snapshot.json`` — the served
  ``identity_line_template`` and the manual's announcement, written by the
  plugin's own writer (``texts.snapshot_bytes``) and read back by its reader;
* the three pins in ``tests/test_skill_bundle.py`` (``MANUAL_VERSION``,
  ``MANUAL_SHA256``, ``TEXTS_SNAPSHOT_SHA256``), one line each.

Deterministic: the same served texts give the same bytes (``fetched_at``
changes only with the texts). Everything is checked before anything is
written; a refusal writes nothing. Run from the repository root with this
package installed (``pip install -e .``)::

    AGENTCHAT_API_KEY=... python scripts/refresh_bundled_texts.py [--api-base URL]

The key is any agent's (the texts are the same for every agent; the filled
``identity_line`` is not kept). It is read from the environment only and never
printed. Exit 0: the files are current (``changed=true|false``, ``manual=`` and
``summary=`` go to ``$GITHUB_OUTPUT`` when set); 1: refused, nothing written;
2: no key.
Run by ``.github/workflows/refresh-texts.yml``.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENV_API_KEY = "AGENTCHAT_API_KEY"
DEFAULT_API_BASE = "https://api.agentchat.me"

MANUAL_REL = Path("agentchatme_hermes") / "skills" / "agentchat" / "SKILL.md"
SNAPSHOT_REL = Path("agentchatme_hermes") / "texts_snapshot.json"
PINS_REL = Path("tests") / "test_skill_bundle.py"

_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")
# A handle the template is filled for to prove it fills (never written).
_PROBE_HANDLE = "refresh-check"


class RefreshError(Exception):
    """Why the bundled texts were not refreshed (nothing was written)."""


@dataclass(frozen=True)
class Refresh:
    """What a refresh did: ``written`` the files it changed (relative to the root)."""

    manual_before: str | None
    manual_after: str
    template_changed: bool
    written: tuple[Path, ...]

    @property
    def changed(self) -> bool:
        return bool(self.written)

    def summary(self) -> str:
        if not self.changed:
            return f"bundled texts current: manual {self.manual_after}, identity line as served"
        manual = (
            f"manual {self.manual_before or '?'} → {self.manual_after}"
            if self.manual_before != self.manual_after
            else f"manual {self.manual_after}"
        )
        line = "identity line changed" if self.template_changed else "identity line unchanged"
        return f"bundled texts refreshed: {manual}, {line} ({', '.join(str(p) for p in self.written)})"


def refresh(
    *,
    client: Any,
    api_base: str,
    root: Path | None = None,
    http_client: Any = None,
    now: datetime | None = None,
) -> Refresh:
    """Bring the three files under ``root`` (this repository by default) to the
    texts ``client`` is served. :class:`RefreshError` when anything is refused
    (nothing written)."""
    from agentchatme.integration import sync_manual, write_atomic

    from agentchatme_hermes import manual, texts

    root = ROOT if root is None else root

    served = _served_texts(client, texts)
    announced = served.manual
    sha = (announced.sha256 or "").strip().lower()
    if not sha:
        raise RefreshError("the served texts announce the manual without a sha256; nothing unverified is bundled")
    if not _VERSION.fullmatch(announced.version or ""):
        raise RefreshError(f"the announced manual version {announced.version!r} is not a version")

    manual_path = root / MANUAL_REL
    snapshot_path = root / SNAPSHOT_REL
    pins_path = root / PINS_REL
    before = {path: _read(path) for path in (manual_path, snapshot_path, pins_path)}
    manual_before = manual.read_version(manual_path)

    # The manual, through the kit, into a copy of the current one (the kit
    # writes only when it is not the announced manual).
    with tempfile.TemporaryDirectory() as folder:
        staged = Path(folder) / "SKILL.md"
        if before[manual_path] is not None:
            staged.write_bytes(before[manual_path] or b"")
        result = sync_manual(path=staged, served=announced, api_base=api_base, fallback=None, http_client=http_client)
        if result.up_to_date is not True:
            raise RefreshError(f"the announced manual could not be had: {manual.describe_result(result)}")
        new_manual = staged.read_bytes()
    if manual.carries_inline_shell(new_manual):
        raise RefreshError(
            "the served manual carries inline shell, which Hermes runs when skills.inline_shell is on; not bundled"
        )
    if hashlib.sha256(new_manual).hexdigest() != sha:  # the kit checked it; said twice is cheap
        raise RefreshError("the manual's bytes are not the announced sha256")

    # The snapshot: fetched_at moves only with the texts.
    old = _parse_or_none(texts, before[snapshot_path])
    candidate = texts.BundledTexts(
        runtime=texts.TEXTS_RUNTIME,
        contract=str(served.version),
        fetched_at=_stamp(now),
        identity_line_template=served.identity_line_template,
        manual={"url": announced.url, "version": announced.version, "sha256": sha},
    )
    if old is not None and _same_texts(old, candidate):
        candidate = old
    new_snapshot = texts.snapshot_bytes(candidate)
    try:
        checked = texts.parse_snapshot(new_snapshot)
        texts.fill_template(checked.identity_line_template, _PROBE_HANDLE)
    except ValueError as exc:
        raise RefreshError(f"the served texts do not make a usable snapshot: {exc}") from exc

    new_pins = _pinned(
        before[pins_path],
        pins_path,
        {
            "MANUAL_VERSION": announced.version,
            "MANUAL_SHA256": sha,
            "TEXTS_SNAPSHOT_SHA256": hashlib.sha256(new_snapshot).hexdigest(),
        },
    )

    planned = [(manual_path, new_manual), (snapshot_path, new_snapshot), (pins_path, new_pins)]
    written: list[Path] = []
    for target, data in planned:
        if before[target] == data:
            continue
        try:
            write_atomic(target, data, mode=0o644)
        except OSError as exc:
            for done in written:  # put back what this run already wrote
                original = before[done]
                if original is not None:
                    write_atomic(done, original, mode=0o644)
            raise RefreshError(f"could not write {target}: {exc}") from exc
        written.append(target)
    return Refresh(
        manual_before=manual_before,
        manual_after=announced.version,
        template_changed=old is None or old.identity_line_template != candidate.identity_line_template,
        written=tuple(path.relative_to(root) for path in written),
    )


def _served_texts(client: Any, texts: Any) -> Any:
    cache = texts.texts_cache(client, None)
    if not cache.refresh_sync():
        raise RefreshError("GET /v1/texts?runtime=hermes did not answer (the warning above says why)")
    served = cache.current()
    if served is None or served.runtime != texts.TEXTS_RUNTIME:
        raise RefreshError(f"the served texts are not for runtime {texts.TEXTS_RUNTIME}")
    return served


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse_or_none(texts: Any, data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return texts.parse_snapshot(data)
    except ValueError:
        return None  # a broken snapshot is replaced


def _same_texts(a: Any, b: Any) -> bool:
    return (a.runtime, a.contract, a.identity_line_template, a.manual) == (
        b.runtime,
        b.contract,
        b.identity_line_template,
        b.manual,
    )


def _stamp(now: datetime | None) -> str:
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pinned(data: bytes | None, path: Path, pins: dict[str, str]) -> bytes:
    if data is None:
        raise RefreshError(f"{path} is missing")
    text = data.decode("utf-8")
    for name, value in pins.items():
        pattern = re.compile(rf'^{name} = "[^"\n]*"$', re.MULTILINE)
        if len(pattern.findall(text)) != 1:
            raise RefreshError(f'{path}: `{name} = "…"` is not on exactly one line')
        text = pattern.sub(f'{name} = "{value}"', text)
    return text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the manual and identity line this release bundles.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"AgentChat API base (default {DEFAULT_API_BASE})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    key = (os.environ.get(ENV_API_KEY) or "").strip()
    if not key:
        print(f"set {ENV_API_KEY} in the environment (any agent's key; never on the command line)", file=sys.stderr)
        return 2

    from agentchatme import AgentChatClient

    from agentchatme_hermes.client_identity import hermes_client_identity

    api_base = args.api_base.rstrip("/")
    client = AgentChatClient(api_key=key, base_url=api_base, client_identity=hermes_client_identity())
    try:
        outcome = refresh(client=client, api_base=api_base)
    except RefreshError as exc:
        print(f"refresh refused, nothing written: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(outcome.summary())
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(
                f"changed={'true' if outcome.changed else 'false'}\nmanual={outcome.manual_after}\n"
                f"summary={outcome.summary()}\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
