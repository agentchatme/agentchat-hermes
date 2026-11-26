"""The ``.env`` guard (``conftest._dotenv_guard``) itself: an open of a ``.env``
file from the plugin's own code is blocked and recorded, so the test that did it
fails at teardown; tests and test doubles may still write and read one."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

import agentchatme_hermes

if TYPE_CHECKING:
    from pathlib import Path

_PACKAGE_DIR = os.path.dirname(os.path.abspath(agentchatme_hermes.__file__))

# Each way a module can open a file: the builtin, pathlib (through io.open), os.open.
_OPENS = {
    "open": "def go(path):\n    with open(path, encoding='utf-8') as f:\n        return f.read()\n",
    "read_text": "from pathlib import Path\ndef go(path):\n    return Path(path).read_text(encoding='utf-8')\n",
    "os.open": "import os\ndef go(path):\n    fd = os.open(path, os.O_RDONLY)\n    os.close(fd)\n",
}


def _plugin_function(source: str) -> Any:
    """``go`` compiled as if it were in a module of the plugin package."""
    namespace: dict[str, Any] = {}
    exec(compile(source, os.path.join(_PACKAGE_DIR, "_dotenv_guard_probe.py"), "exec"), namespace)
    return namespace["go"]


@pytest.mark.parametrize("how", sorted(_OPENS))
def test_a_dotenv_opened_by_the_plugin_is_blocked_and_fails_the_test(
    tmp_path: Path, _dotenv_guard: list[str], how: str
) -> None:
    env = tmp_path / ".env"
    env.write_text("AGENTCHAT_API_KEY=x\n", encoding="utf-8")  # a test may write it
    with pytest.raises(OSError, match=r"the plugin opened a \.env file"):
        _plugin_function(_OPENS[how])(str(env))
    assert _dotenv_guard == [str(env)], "recorded: the guard fails the test at teardown"
    _dotenv_guard.clear()  # this test is the guard's own: do not fail it


@pytest.mark.parametrize("how", sorted(_OPENS))
def test_other_files_and_test_code_are_not_blocked(tmp_path: Path, _dotenv_guard: list[str], how: str) -> None:
    other = tmp_path / "config.yaml"
    other.write_text("x: 1\n", encoding="utf-8")
    _plugin_function(_OPENS[how])(str(other))
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    assert env.read_text(encoding="utf-8") == "A=1\n", "a test (or a Hermes double) may read it"
    assert _dotenv_guard == []
