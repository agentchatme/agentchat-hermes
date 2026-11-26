"""The texts: the served ones, through the SDK kit's ``TextsCache``, and the
copies this release bundles.

Every word an agent sees about AgentChat is written once, on the server, and
served as data (``GET /v1/texts?runtime=hermes``): the wake instruction, the
invite prompt, the identity line, the gate-call words. They come spelled the
way Hermes names the hosted tools. The kit's
:class:`agentchatme.integration.TextsCache` fetches them, keeps the last good
copy in a file so a gateway that starts offline is not mute, refreshes daily
(5 min doubling to 1 h after a failure), and tells listeners when they
change. This module says where that file is for Hermes and gives the CLI,
the wizard and ``doctor`` a one-shot load.

The file is ``texts-hermes.json`` in the account's state directory. Releases
before the kit kept ``texts.json`` there; that file may hold a copy from
before the server spelled the tool names for Hermes, so it is never read and
is removed on start (:func:`remove_legacy_cache`).

Two of those texts load into every session of the profile, not only into the
turns AgentChat starts: the identity line in ``SOUL.md`` and the manual (the
``agentchat:agentchat`` skill). By default they are the copies this release
bundles — the manual at ``skills/agentchat/SKILL.md`` and the served
identity-line template in ``texts_snapshot.json``, both taken from the server
by ``scripts/refresh_bundled_texts.py`` and pinned by
``tests/test_skill_bundle.py`` — so they change only when the plugin is
updated. The plugin setting ``live_texts`` (``plugins.entries.agentchat.
settings.live_texts`` in ``config.yaml``; the plugin's ``config_schema``)
keeps both current from the server instead, as releases before 0.7.0 did.
The setting is read once, in ``register()`` (:func:`read_mode`), and applies
from the next gateway start. :func:`identity_text` is the one source of the
identity line every writer and reader of ``SOUL.md`` uses, so the gateway
and ``hermes agentchat login`` always place the same line. The texts used
only inside AgentChat-started turns and gate calls are always the served
ones.
"""
from __future__ import annotations

import functools
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .profile_scope import hermes_home_key

if TYPE_CHECKING:
    from agentchatme.integration import TextsCache
    from agentchatme.types import Texts

logger = logging.getLogger(__name__)

TEXTS_RUNTIME = "hermes"
TEXTS_FILENAME = "texts-hermes.json"
LEGACY_TEXTS_FILENAME = "texts.json"

# The plugin setting (plugin.yaml ``config_schema``).
SETTING_LIVE_TEXTS = "live_texts"

# This release's copy of the served identity-line template and of the
# manual's announcement (``scripts/refresh_bundled_texts.py`` writes it).
SNAPSHOT_PATH = Path(__file__).parent / "texts_snapshot.json"
SNAPSHOT_FORMAT = 1
_HANDLE_PLACEHOLDER = "<handle>"
_SHA256 = re.compile(r"[0-9a-f]{64}")

TEXTS_UNAVAILABLE_DETAIL = (
    "identity line not placed: /v1/texts is unreachable and nothing is cached — the gateway "
    "places it once the server answers (or run `hermes agentchat doctor --fix` later)"
)


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


class _NoNetwork:
    """A client for :func:`cached_texts`: every fetch fails at once."""

    base_url = None

    class texts:  # the SDK client's attribute name
        @staticmethod
        def get(**_kw: Any) -> Any:
            raise ConnectionError("not fetched (AgentChat did not answer)")


def cached_texts(state_dir: Path) -> tuple[Texts | None, str | None]:
    """The account's cached texts and when they were served, without any
    network (``doctor`` and ``status`` when AgentChat did not answer). Through
    the kit's ``TextsCache``: its own file, read the way the cache reads it.
    ``(None, None)`` when nothing is cached. Never raises."""
    from agentchatme.integration import TextsCache

    try:
        cache = TextsCache(
            _NoNetwork(),
            runtime=TEXTS_RUNTIME,
            file_path=state_dir / TEXTS_FILENAME,
            on_error=lambda _error: None,
        )
        cache.refresh_sync()
        return cache.current(), cache.fetched_at
    except Exception as exc:
        logger.debug("texts: cached copy unreadable: %s", exc)
        return None, None


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


# ─── the setting: this release's texts, or the served ones ──────────────────


@dataclass(frozen=True)
class TextsMode:
    """Which manual and identity line a profile uses. ``live``: the served
    ones, kept current (``live_texts`` on); otherwise this release's copies.
    ``detail`` says how that was decided; ``problem`` is set when the setting
    holds something that is not true or false, or could not be read (then
    this release's copies are used, and ``doctor`` warns)."""

    live: bool
    detail: str
    problem: str | None = None


_NOT_READ = TextsMode(False, "live_texts not read in this process (off)")
_modes: dict[str, TextsMode] = {}
_modes_lock = threading.Lock()


def read_mode(ctx: Any) -> TextsMode:
    """The profile's ``live_texts`` setting, through Hermes' ``ctx.get_config``
    (``plugins.entries.<plugin>.settings.live_texts`` of the profile ``ctx``
    was loaded for). Called in ``register()``, where Hermes has bound that
    profile. Absent, false, anything that is not ``true``, a Hermes without
    plugin settings, or a read that fails: this release's texts. Never raises."""
    get_config = getattr(ctx, "get_config", None)
    if not callable(get_config):
        return TextsMode(False, "this Hermes has no plugin settings (ctx.get_config), so live_texts is off")
    try:
        value = get_config(SETTING_LIVE_TEXTS, None)
    except Exception as exc:
        problem = f"live_texts could not be read ({exc})"
        return TextsMode(False, f"{problem}, so it is off", problem)
    if value is True:
        return TextsMode(True, "live_texts on")
    if value is False:
        return TextsMode(False, "live_texts off")
    if value is None:
        return TextsMode(False, "live_texts not set, off by default")
    problem = f"live_texts is {value!r} in config.yaml, not true or false"
    return TextsMode(False, f"{problem}, so it is off", problem)


def record_mode(home: Path, mode: TextsMode) -> None:
    """What ``register()`` read for the profile at ``home``, for this process."""
    with _modes_lock:
        _modes[hermes_home_key(Path(home))] = mode


def mode_for(home: Path) -> TextsMode:
    """The mode recorded for the profile at ``home``; this release's texts when
    nothing was recorded (outside Hermes)."""
    with _modes_lock:
        return _modes.get(hermes_home_key(Path(home)), _NOT_READ)


# ─── this release's copies ───────────────────────────────────────────────────


@dataclass(frozen=True)
class BundledTexts:
    """``texts_snapshot.json``: the served ``identity_line_template`` (spelled
    for Hermes, ``<handle>`` unfilled) and the manual's announcement
    (``url``, ``version``, ``sha256``) this release bundles, with the texts
    contract and when they were taken from the server. Shaped like the served
    texts where they are used: ``manual`` is an announcement ``manual.check``
    takes, and the template is filled by the kit (:func:`fill_template`).
    There is no filled ``identity_line``: one snapshot serves every agent."""

    runtime: str
    contract: str
    fetched_at: str
    identity_line_template: str
    manual: dict[str, str]


def parse_snapshot(data: bytes) -> BundledTexts:
    """``texts_snapshot.json`` read and checked. :class:`ValueError` names
    what is wrong. The one reader of the file (the plugin and the refresh
    script)."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"not JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    if raw.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"format {raw.get('format')!r}, not {SNAPSHOT_FORMAT}")
    if raw.get("runtime") != TEXTS_RUNTIME:
        raise ValueError(f"texts for runtime {raw.get('runtime')!r}, not {TEXTS_RUNTIME}")
    fields = {name: raw.get(name) for name in ("contract", "fetched_at", "identity_line_template")}
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} missing")
    template = str(fields["identity_line_template"])
    if f"@{_HANDLE_PLACEHOLDER}" not in template:
        raise ValueError(f"identity_line_template does not name @{_HANDLE_PLACEHOLDER}")
    manual = raw.get("manual")
    if not isinstance(manual, dict):
        raise ValueError("manual missing")
    announced = {name: manual.get(name) for name in ("url", "version", "sha256")}
    if not all(isinstance(value, str) and value for value in announced.values()):
        raise ValueError("manual needs url, version and sha256")
    if not _SHA256.fullmatch(str(announced["sha256"])):
        raise ValueError("manual.sha256 is not 64 lower-case hex digits")
    return BundledTexts(
        runtime=TEXTS_RUNTIME,
        contract=str(fields["contract"]),
        fetched_at=str(fields["fetched_at"]),
        identity_line_template=template,
        manual={name: str(value) for name, value in announced.items()},
    )


def snapshot_bytes(snapshot: BundledTexts) -> bytes:
    """The file's bytes for ``snapshot``: sorted keys, two-space indent,
    UTF-8, a final line break — the same texts always give the same bytes."""
    document = {
        "contract": snapshot.contract,
        "fetched_at": snapshot.fetched_at,
        "format": SNAPSHOT_FORMAT,
        "identity_line_template": snapshot.identity_line_template,
        "manual": dict(snapshot.manual),
        "runtime": snapshot.runtime,
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


@functools.lru_cache(maxsize=4)
def _load_snapshot(path: str) -> tuple[BundledTexts | None, str | None]:
    try:
        return parse_snapshot(Path(path).read_bytes()), None
    except OSError as exc:
        return None, f"{path} cannot be read ({exc})"
    except ValueError as exc:
        return None, f"{path} is not a texts snapshot: {exc}"


def bundled_texts() -> BundledTexts | None:
    """This release's snapshot, or ``None`` (a hand-broken install:
    :func:`bundled_texts_problem` says why)."""
    return _load_snapshot(str(SNAPSHOT_PATH))[0]


def bundled_texts_problem() -> str | None:
    return _load_snapshot(str(SNAPSHOT_PATH))[1]


def fill_template(template: str, handle: str) -> str:
    """The identity line ``template`` gives ``handle``, through the SDK kit:
    ``anchor.render`` fills the template (the rule every integration shares)
    and ``anchor.find`` reads the line back out of the block, exactly as the
    kit's ``decide`` and ``follow`` compare it. :class:`ValueError` when the
    filled template does not name ``@handle``; :class:`ImportError` without
    the kit."""
    from agentchatme.integration import anchor

    block = anchor.render({"identity_line": "", "identity_line_template": template}, handle)
    found = anchor.find(block)
    if found is None:  # pragma: no cover - render always fences its block
        raise ValueError("the kit's block could not be read back")
    return str(found.body)


# ─── the identity line in use ────────────────────────────────────────────────

IdentitySource = Literal["release", "served"]


@dataclass(frozen=True)
class IdentityText:
    """The identity line a profile places in ``SOUL.md`` and checks it against.

    ``source``: ``release`` (this release's copy: ``live_texts`` off) or
    ``served`` (the served texts). ``line``: this agent's line, ``None`` when
    there is none to place (``missing`` says why, in the words a placement
    step and ``doctor`` print). ``template``: the template it was filled from
    (recognises a block an earlier release wrote without a handle line)."""

    source: IdentitySource
    line: str | None
    template: str | None
    missing: str | None = None


def identity_text(served: Texts | None, *, handle: str | None, home: Path) -> IdentityText:
    """The one source of the identity line for the profile at ``home``: the
    gateway's refresh, every placement (register, login, recover, the
    wizard, ``doctor --fix``), ``doctor``, ``status`` and ``logout`` all ask
    here, so they never disagree about which line belongs in ``SOUL.md``.

    ``live_texts`` on: ``served`` (the texts in hand) as served for Hermes;
    nothing without them. Off: this release's template filled for ``handle``
    (``served`` is not used). No network. Never raises."""
    from .turn import identity_line, identity_line_template

    if mode_for(home).live:
        if served is None:
            return IdentityText("served", None, None, TEXTS_UNAVAILABLE_DETAIL)
        return IdentityText("served", identity_line(served), identity_line_template(served))
    snapshot = bundled_texts()
    if snapshot is None:
        return IdentityText(
            "release",
            None,
            None,
            f"identity line not placed: this release's copy is broken — {bundled_texts_problem()} "
            "(reinstall agentchatme-hermes)",
        )
    template = snapshot.identity_line_template.strip()
    if not handle:
        return IdentityText("release", None, template, "this profile's AgentChat handle is not known")
    try:
        line = fill_template(template, handle)
    except (ImportError, ValueError) as exc:
        return IdentityText(
            "release", None, template, f"identity line not placed: this release's template cannot be filled ({exc})"
        )
    return IdentityText("release", line, template)
