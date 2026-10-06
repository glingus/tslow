"""Test delle tabelle di M3: user_rules, decisions, actions_log."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import database as db


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _make_incident(conn, group_key: str = "chrome") -> int:
    return db.create_incident(
        conn, opened_at_ms=1000, resource="cpu", incident_type="anomaly", severity="anomaly",
        group_key=group_key, proposed_action="soft",
    )


def test_migration_0003_applied(conn) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 3
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"user_rules", "decisions", "actions_log"}.issubset(tables)


def test_user_rule_upsert_and_get(conn) -> None:
    assert db.get_user_rule(conn, "chrome") is None
    db.upsert_user_rule(
        conn, "chrome", consecutive_denies=1, allow_once_count=0, max_auto_action=None, source="user",
        expires_at_ms=None, now_ms=1000,
    )
    rule = db.get_user_rule(conn, "chrome")
    assert rule.consecutive_denies == 1
    assert rule.status == "active"

    db.upsert_user_rule(
        conn, "chrome", consecutive_denies=0, allow_once_count=1, max_auto_action=None, source="user",
        expires_at_ms=None, now_ms=2000,
    )
    rule2 = db.get_user_rule(conn, "chrome")
    assert rule2.allow_once_count == 1
    assert rule2.consecutive_denies == 0  # sovrascritto, non sommato


def test_revoke_and_reset_learning(conn) -> None:
    db.upsert_user_rule(conn, "chrome", consecutive_denies=3, allow_once_count=0, max_auto_action=None, source="ignore_learned", expires_at_ms=999999, now_ms=1000)
    db.revoke_user_rule(conn, "chrome", now_ms=2000)
    assert db.get_user_rule(conn, "chrome").status == "revoked"

    db.upsert_user_rule(conn, "code", consecutive_denies=0, allow_once_count=2, max_auto_action="soft", source="user", expires_at_ms=None, now_ms=1000)
    removed = db.reset_learning(conn)
    assert removed >= 1
    assert db.get_user_rule(conn, "code") is None


def test_decision_lifecycle(conn) -> None:
    incident_id = _make_incident(conn)
    decision_id = db.create_decision(conn, incident_id, "allow_once", now_ms=1000)
    decision = db.get_decision(conn, decision_id)
    assert decision.execution_status == "pending"
    assert decision.choice == "allow_once"

    pending = db.list_pending_decisions(conn)
    assert any(d.id == decision_id for d in pending)

    db.update_decision_status(conn, decision_id, "ok", executed_at_ms=2000)
    updated = db.get_decision(conn, decision_id)
    assert updated.execution_status == "ok"
    assert updated.executed_at_ms == 2000
    assert not any(d.id == decision_id for d in db.list_pending_decisions(conn))


def test_actions_log_records_and_lists(conn) -> None:
    incident_id = _make_incident(conn)
    decision_id = db.create_decision(conn, incident_id, "allow_once", now_ms=1000)
    db.record_action(
        conn, decision_id=decision_id, incident_id=incident_id, executed_at_ms=1500, action="soft_priority",
        pid=1234, previous_priority=32, previous_io_priority=None, outcome="ok",
    )
    actions = db.list_actions_for_decision(conn, decision_id)
    assert len(actions) == 1
    assert actions[0].pid == 1234
    assert actions[0].previous_priority == 32
    assert actions[0].outcome == "ok"
