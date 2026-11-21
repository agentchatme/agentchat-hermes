"""What Hermes' install scanner would flag in this tree, checked on every PR.

``hermes plugins install`` clones the repository and scans every file before
it installs (``hermes_cli/plugins_install_scan.py`` on ``tools/skills_guard.py``'s
patterns): a ``dangerous`` verdict blocks the install even with ``--force``, a
``caution`` verdict asks the user to confirm, after listing the findings. The
nightly ``hermes-main`` workflow runs that scanner itself on this tree; these
checks catch two findings that need no Hermes to see, on every PR:

* an invisible character (zero-width, BOM, bidi controls) in any tracked text
  file (``invisible_unicode``, high);
* an inline-shell snippet (an exclamation mark, then a command in backticks) in
  Python source (``inline_shell_exec``, high), the code that refuses one included.

The bundled manual is checked by ``test_skill_bundle.py``.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# tools/skills_guard.py ``_INVISIBLE_CHAR_NAMES``, written as escapes.
INVISIBLE = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
# tools/skills_guard.py ``inline_shell_exec``, with the backtick as ``\x60``.
INLINE_SHELL = re.compile(r"!\x60[^\x60\s][^\x60\n]*\x60")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.stdout.splitlines()


def _text(name: str) -> str | None:
    try:
        return (ROOT / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def test_no_invisible_characters_in_any_tracked_text_file() -> None:
    offenders = []
    for name in _tracked():
        text = _text(name)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            hidden = sorted({f"U+{ord(c):04X}" for c in line if c in INVISIBLE})
            if hidden:
                offenders.append(f"{name}:{number} {', '.join(hidden)}")
    assert offenders == [], "write them as escapes (\\ufeff): " + "; ".join(offenders)


def test_no_inline_shell_snippet_in_python_source() -> None:
    offenders = []
    for name in _tracked():
        if not name.endswith(".py"):
            continue
        text = _text(name) or ""
        for number, line in enumerate(text.splitlines(), start=1):
            if INLINE_SHELL.search(line):
                offenders.append(f"{name}:{number}")
    assert offenders == [], "build the backtick from \\x60: " + "; ".join(offenders)


def test_the_patterns_catch_what_they_guard() -> None:
    assert INLINE_SHELL.search("run " + "!" + chr(0x60) + "id" + chr(0x60)) is not None
    assert INLINE_SHELL.search("!" + chr(0x60) + " " + chr(0x60)) is None, "Hermes skips an empty marker"
    assert chr(0xFEFF) in INVISIBLE
