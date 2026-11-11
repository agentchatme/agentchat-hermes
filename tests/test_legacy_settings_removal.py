"""The old setting names leave on the date contract v1 set for them.

Contract v1, section 7, "Old integration settings"
(https://agentchat.me/contract/v1/#deprecations): Hermes reads
``AGENTCHATME_DOORBELL`` (``v3`` | ``off``) and ``AGENTCHATME_KNOCK_DEBOUNCE_S``
as fallbacks for ``AGENTCHATME_INTEGRATION`` and
``AGENTCHATME_WAKE_DEBOUNCE_S``, and accepts ``AGENTCHATME_REPLY_GATE_FAIL_OPEN``
(ignored, with a warning), until **2027-02-01**.

This test holds both sides of that promise. Before the date the
compatibility readers must still be in the package. From the date on it
fails, in CI and in the nightly, until they are removed from the package
code, the README, the RUNBOOK and ``docs/`` — so the removal cannot be
forgotten. The CHANGELOG keeps its history; the readers' own tests in
``test_config.py`` go with the readers.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REMOVAL_DATE = date(2027, 2, 1)
LEGACY_NAMES = (
    "AGENTCHATME_DOORBELL",
    "AGENTCHATME_KNOCK_DEBOUNCE_S",
    "AGENTCHATME_REPLY_GATE_FAIL_OPEN",
)
_ROOT = Path(__file__).resolve().parents[1]


def _mentions(root: Path) -> dict[str, list[str]]:
    """Every file that still names a legacy setting, by name."""
    files = sorted((root / "agentchatme_hermes").rglob("*.py"))
    files += [root / "README.md", root / "RUNBOOK.md", *sorted((root / "docs").glob("*.md"))]
    found: dict[str, list[str]] = {}
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name in LEGACY_NAMES:
            if re.search(rf"\b{name}\b", text):
                found.setdefault(name, []).append(str(path.relative_to(root)))
    return found


def _verdict(today: date, mentions: dict[str, list[str]]) -> str | None:
    """``None`` when the package keeps the contract on ``today``, else why not."""
    if today >= REMOVAL_DATE:
        if not mentions:
            return None
        listed = "; ".join(f"{name}: {', '.join(paths)}" for name, paths in sorted(mentions.items()))
        return (
            f"contract v1 removed these settings on {REMOVAL_DATE.isoformat()}: delete the "
            f"compatibility readers, their docs and their tests, then this test ({listed})"
        )
    missing = [name for name in LEGACY_NAMES if name not in mentions]
    if missing:
        return (
            f"contract v1 keeps {', '.join(missing)} working until {REMOVAL_DATE.isoformat()}; "
            "the compatibility reader is gone early"
        )
    return None


def test_legacy_settings_follow_the_contract_date() -> None:
    today = datetime.now(timezone.utc).date()
    problem = _verdict(today, _mentions(_ROOT))
    assert problem is None, problem


def test_the_package_reads_them_today() -> None:
    """Each name is read in ``config.py`` (the doctor names the integration
    one too), so the scan above is looking at the readers."""
    mentions = _mentions(_ROOT)
    for name in LEGACY_NAMES:
        assert "agentchatme_hermes/config.py" in [p.replace("\\", "/") for p in mentions.get(name, [])], name


def test_the_check_fails_from_the_removal_date() -> None:
    present = {name: ["agentchatme_hermes/config.py"] for name in LEGACY_NAMES}
    day_before = REMOVAL_DATE - timedelta(days=1)

    assert _verdict(day_before, present) is None
    on_the_day = _verdict(REMOVAL_DATE, present)
    assert on_the_day is not None and "delete the compatibility readers" in on_the_day
    assert _verdict(REMOVAL_DATE + timedelta(days=400), present) is not None
    assert _verdict(REMOVAL_DATE, {}) is None

    early = _verdict(day_before, {"AGENTCHATME_DOORBELL": ["x.py"]})
    assert early is not None and "AGENTCHATME_KNOCK_DEBOUNCE_S" in early and "gone early" in early
