"""Regression tests for the AGENTCHATME_ALLOW_ALL=true default seeding.

Hermes's gateway-level ``_is_user_authorized`` (``gateway/run.py:3320-3324``)
denies inbound messages from any sender not on the per-platform allowlist
when no allowlist is configured. AgentChat enforces who-can-DM-you on the
server (``inbox_mode``), so the framework allowlist is redundant — and its
deny-default silently drops legitimate messages.

The wizard now seeds ``AGENTCHATME_ALLOW_ALL=true`` on first key save so
inbound works out of the box. If the operator has already chosen a
different setting (explicit ALLOW_ALL or ALLOWED_HANDLES), we leave it
alone.
"""

from __future__ import annotations


def test_seed_default_writes_true_when_unset():
    """No prior config → ALLOW_ALL=true gets written."""
    from agentchatme_hermes.setup import _seed_allow_all_default

    saved = {}
    env = {}

    def fake_save(k, v):
        saved[k] = v
        env[k] = v

    def fake_get(k):
        return env.get(k)

    result = _seed_allow_all_default(fake_save, fake_get)
    assert result is True
    assert saved == {"AGENTCHATME_ALLOW_ALL": "true"}


def test_seed_default_skips_when_allow_all_already_set():
    """Operator set ALLOW_ALL=false manually → leave it alone."""
    from agentchatme_hermes.setup import _seed_allow_all_default

    saved = {}
    env = {"AGENTCHATME_ALLOW_ALL": "false"}

    result = _seed_allow_all_default(
        lambda k, v: saved.__setitem__(k, v),
        lambda k: env.get(k),
    )
    assert result is False
    assert saved == {}


def test_seed_default_skips_when_allowlist_already_set():
    """Operator set ALLOWED_HANDLES → that's an explicit choice, leave it."""
    from agentchatme_hermes.setup import _seed_allow_all_default

    saved = {}
    env = {"AGENTCHATME_ALLOWED_HANDLES": "alice,bob"}

    result = _seed_allow_all_default(
        lambda k, v: saved.__setitem__(k, v),
        lambda k: env.get(k),
    )
    assert result is False
    assert saved == {}


def test_seed_default_idempotent_on_existing_true():
    """ALLOW_ALL=true already → don't overwrite (no-op)."""
    from agentchatme_hermes.setup import _seed_allow_all_default

    saved = {}
    env = {"AGENTCHATME_ALLOW_ALL": "true"}

    result = _seed_allow_all_default(
        lambda k, v: saved.__setitem__(k, v),
        lambda k: env.get(k),
    )
    assert result is False
    assert saved == {}


def test_seed_default_ignores_whitespace_only_existing():
    """A whitespace-only env value should NOT count as 'operator chose'."""
    from agentchatme_hermes.setup import _seed_allow_all_default

    saved = {}
    env = {"AGENTCHATME_ALLOW_ALL": "   "}

    result = _seed_allow_all_default(
        lambda k, v: saved.__setitem__(k, v),
        lambda k: env.get(k),
    )
    assert result is True
    assert saved == {"AGENTCHATME_ALLOW_ALL": "true"}
