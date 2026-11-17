"""The pure helpers in ``agentchatme_hermes.wizard``: key masking and the
key shape in tracked text. The flows are ``test_wizard_signup.py``."""
from __future__ import annotations

import pytest
from fake_keys import api_key, real_shaped_key

from agentchatme_hermes.wizard import _mask_key

# ─── _mask_key ─────────────────────────────────────────────────────────────


class TestMaskKey:
    def test_real_shaped_key_shows_first_8_and_last_4(self) -> None:
        key = real_shaped_key()
        assert len(key) == 46
        masked = _mask_key(key)
        assert masked == f"{key[:8]}…{key[-4:]}"
        assert masked.startswith("ac_")
        # 34 of the 43 random characters stay hidden.
        assert key[8:-4] not in masked

    def test_long_key_masked(self) -> None:
        masked = _mask_key(api_key("abcdef1234567890ghij"))
        assert masked == "ac_abcde…ghij"
        assert "1234567890" not in masked

    def test_short_value_uses_placeholder(self) -> None:
        assert _mask_key("ac_short") == "ac_…"
        assert _mask_key("") == "ac_…"

    def test_twelve_characters_are_never_shown_whole(self) -> None:
        # 8 + 4 would be the whole value.
        assert _mask_key(api_key("123456789")) == "ac_…"
        assert _mask_key(api_key("1234567890")) == "ac_12345…7890"

    def test_cli_uses_the_same_mask(self) -> None:
        from agentchatme_hermes import cli

        assert cli._mask_key is _mask_key


class TestKeyShapeText:
    def test_no_tracked_text_names_a_live_prefix(self) -> None:
        """Keys are ``ac_`` and 43 random characters; there is no ``live``
        prefix, so no prompt, help text, doc, manifest or workflow may show
        one. The CHANGELOG keeps its history, the tests build keys at run
        time, and the bundled manual is the published text verbatim."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        try:
            listed = subprocess.run(
                ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("not a git checkout")
        skipped = ("CHANGELOG.md", "tests/", "agentchatme_hermes/skills/agentchat/SKILL.md")
        needle = "ac_" + "live"
        offenders = []
        for name in listed:
            if name.startswith(skipped):
                continue
            path = root / name
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                offenders.append(name)
        assert offenders == []
