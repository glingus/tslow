"""rules.py: apprendimento nega/consenti-una-volta/consenti-sempre."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import database as db
from tslow import rules
from tslow.config import load_settings


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_app_identity_prefers_exe_path() -> None:
    assert rules.app_identity(r"C:\Program Files\Chrome\chrome.exe", "chrome.exe") == r"C:\Program Files\Chrome\chrome.exe"
    assert rules.app_identity(None, "Chrome.EXE") == "chrome"


def test_not_ignored_by_default(conn) -> None:
    assert rules.is_ignored(conn, "chrome", now_ms=1000) is False
    assert rules.auto_action_for(conn, "chrome") is None


def test_three_consecutive_denies_trigger_ignore_learned(conn) -> None:
    settings = load_settings()
    rules.apply_decision(conn, settings, "chrome", "deny", now_ms=1000)
    assert rules.is_ignored(conn, "chrome", now_ms=1000) is False
    rules.apply_decision(conn, settings, "chrome", "deny", now_ms=2000)
    assert rules.is_ignored(conn, "chrome", now_ms=2000) is False
    rules.apply_decision(conn, settings, "chrome", "deny", now_ms=3000)
    assert rules.is_ignored(conn, "chrome", now_ms=3000) is True


def test_ignore_learned_expires_after_30_days(conn) -> None:
    settings = load_settings()
    for i in range(3):
        rules.apply_decision(conn, settings, "chrome", "deny", now_ms=1000 + i)
    assert rules.is_ignored(conn, "chrome", now_ms=1000) is True
    thirty_one_days_later = 1000 + 31 * 86_400_000
    assert rules.is_ignored(conn, "chrome", now_ms=thirty_one_days_later) is False


def test_allow_once_resets_consecutive_denies(conn) -> None:
    settings = load_settings()
    rules.apply_decision(conn, settings, "chrome", "deny", now_ms=1000)
    rules.apply_decision(conn, settings, "chrome", "deny", now_ms=2000)
    rule = rules.apply_decision(conn, settings, "chrome", "allow_once", now_ms=3000)
    assert rule.consecutive_denies == 0
    assert rule.allow_once_count == 1


def test_allow_always_sets_soft_auto_action(conn) -> None:
    settings = load_settings()
    rules.apply_decision(conn, settings, "chrome", "allow_always", now_ms=1000)
    assert rules.auto_action_for(conn, "chrome") == "soft"


def test_allow_always_clears_previous_ignore_learned(conn) -> None:
    settings = load_settings()
    for i in range(3):
        rules.apply_decision(conn, settings, "chrome", "deny", now_ms=1000 + i)
    assert rules.is_ignored(conn, "chrome", now_ms=1000) is True
    rules.apply_decision(conn, settings, "chrome", "allow_always", now_ms=5000)
    assert rules.is_ignored(conn, "chrome", now_ms=5000) is False
