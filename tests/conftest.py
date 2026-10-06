"""Fixture condivise. La protezione utente (M7.3) viene sostituita con la vecchia lista fissa."""

from __future__ import annotations

import pytest

from tslow import protection


@pytest.fixture(autouse=True)
def _legacy_user_protection():
    """Mantiene significativi i test scritti quando chrome/claude/code erano hardcoded."""
    apps = frozenset({"chrome", "claude", "code"})
    previous = protection.set_user_protection(
        protection.UserProtection(
            untouchable=apps,
            free_rein_parents=apps,
            untouchable_children=frozenset({("node", "claude")}),
        )
    )
    yield
    protection.set_user_protection(previous)
