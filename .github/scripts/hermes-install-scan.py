#!/usr/bin/env python3
"""Hermes' own install-time security scan, on this plugin or on the served manual.

``hermes plugins install`` clones a plugin and scans the clone before it
installs (``hermes_cli/plugins_cmd.py`` ``_scan_plugin_tree``:
``hermes_cli.plugins_install_scan.scan_plugin`` and
``should_allow_plugin_install``; ``tools.plugin_guard`` on releases that keep
the old name). ``hermes skills install <url>`` scans the skill it downloads
(``tools.skills_guard.scan_skill`` and ``should_allow_install``). A
``dangerous`` verdict blocks either install, ``--force`` included; a plugin's
``caution`` verdict asks the user to confirm after listing the findings, and a
community skill's is blocked without ``--force``.

Usage, with the Python of the Hermes environment (Hermes has no public entry
point for the scan, so this calls the functions its install commands call)::

    hermes-install-scan.py plugin <cloned tree> <source>
    hermes-install-scan.py skill <skill dir or SKILL.md> <source>

Prints the verdict, the counts by severity, the install decision with and
without ``--force``, and every critical and high finding. Exit 1 on a
dangerous verdict, 2 on bad usage, 0 otherwise (a caution verdict is a
warning annotation). Run by ``.github/workflows/hermes-main.yml``. Reads only.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import Any, Callable


def _plugin_scanner() -> tuple[Callable[..., Any], Callable[..., Any]]:
    try:
        from hermes_cli.plugins_install_scan import scan_plugin, should_allow_plugin_install
    except ImportError:
        from tools.plugin_guard import scan_plugin, should_allow_plugin_install
    return scan_plugin, should_allow_plugin_install


def _skill_scanner() -> tuple[Callable[..., Any], Callable[..., Any]]:
    from tools.skills_guard import scan_skill, should_allow_install

    return scan_skill, should_allow_install


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("plugin", "skill"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, target, source = argv[1], Path(argv[2]), argv[3]
    if not target.exists():
        print(f"::error::{target} does not exist")
        return 2
    scan, allow = _plugin_scanner() if kind == "plugin" else _skill_scanner()
    result = scan(target, source=source)
    counts = collections.Counter(f.severity for f in result.findings)
    by_severity = ", ".join(f"{counts[s]} {s}" for s in ("critical", "high", "medium", "low") if counts[s])
    print(f"{kind} {target} ({source}): verdict {result.verdict}, {len(result.findings)} findings ({by_severity or 'none'})")
    print(f"  install: {allow(result, force=False)[1]}")
    print(f"  with --force: {allow(result, force=True)[1]}")
    for severity in ("critical", "high"):
        for f in sorted((f for f in result.findings if f.severity == severity), key=lambda f: (f.file, f.line)):
            print(f"  {severity.upper():8} {f.pattern_id:26} {f.file}:{f.line}  {f.match[:110]!r}")
    if result.verdict == "dangerous":
        print(f"::error::Hermes' install scan rates {target} dangerous: the install is refused, --force included")
        return 1
    if result.verdict == "caution":
        print(f"::warning::Hermes' install scan rates {target} caution ({counts['high']} high): the user is asked to confirm")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
