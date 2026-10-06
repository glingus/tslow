"""Test delle tabelle di M2: baselines (EWMA), incidents, notifications."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import database as db


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_migration_0002_applied(conn) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"baselines", "incidents", "notifications"}.issubset(tables)


def test_baseline_created_on_first_sample(conn) -> None:
    b = db.update_baseline(conn, "cpu_percent", hour_of_day=10, value=20.0, alpha=0.1, now_ms=1000)
    assert b.ewma_mean == 20.0
    assert b.ewma_var == 0.0
    assert b.sample_count == 1


def test_baseline_converges_toward_stable_value(conn) -> None:
    for _ in range(200):
        db.update_baseline(conn, "cpu_percent", hour_of_day=10, value=30.0, alpha=0.1, now_ms=1000)
    b = db.get_baseline(conn, "cpu_percent", hour_of_day=10)
    assert b.ewma_mean == pytest.approx(30.0, abs=0.01)
    assert b.ewma_var == pytest.approx(0.0, abs=0.01)
    assert b.sample_count == 200


def test_baseline_separate_per_hour(conn) -> None:
    db.update_baseline(conn, "cpu_percent", hour_of_day=9, value=10.0, alpha=0.1, now_ms=1000)
    db.update_baseline(conn, "cpu_percent", hour_of_day=20, value=90.0, alpha=0.1, now_ms=1000)
    assert db.get_baseline(conn, "cpu_percent", 9).ewma_mean == 10.0
    assert db.get_baseline(conn, "cpu_percent", 20).ewma_mean == 90.0


def test_incident_lifecycle(conn) -> None:
    assert db.get_open_incident(conn, "chrome", "cpu") is None

    incident_id = db.create_incident(
        conn,
        opened_at_ms=1000,
        resource="cpu",
        incident_type="anomaly",
        severity="anomaly",
        group_key="chrome",
        proposed_action="soft",
    )
    assert incident_id > 0

    open_incident = db.get_open_incident(conn, "chrome", "cpu")
    assert open_incident is not None
    assert open_incident.status == "open"

    # non deve aprirsi un secondo incidente per la stessa coppia (gruppo, risorsa): la logica di
    # detector.py controlla get_open_incident prima di chiamare create_incident.

    db.close_incident(conn, incident_id, closed_at_ms=2000)
    assert db.get_open_incident(conn, "chrome", "cpu") is None
    closed = db.get_incident(conn, incident_id)
    assert closed.status == "resolved"
    assert closed.closed_at_ms == 2000


def test_incident_system_wide_has_null_group(conn) -> None:
    incident_id = db.create_incident(
        conn, opened_at_ms=1000, resource="system", incident_type="throttling", severity="critical", proposed_action="none"
    )
    assert db.get_open_incident(conn, None, "system").id == incident_id


def test_notifications_and_cooldown(conn) -> None:
    incident_id = db.create_incident(
        conn, opened_at_ms=1000, resource="cpu", incident_type="anomaly", severity="anomaly",
        group_key="chrome", proposed_action="soft",
    )
    assert db.last_notification_for_group(conn, "chrome") is None
    db.record_notification(conn, incident_id, sent_at_ms=1500, channel="toast", success=True)
    assert db.last_notification_for_group(conn, "chrome") == 1500
    assert db.notifications_count_since(conn, since_ms=0) == 1
    assert db.notifications_count_since(conn, since_ms=2000) == 0


def _mk_incident(conn, opened_at_ms: int, action: str = "none") -> int:
    return db.create_incident(
        conn, opened_at_ms=opened_at_ms, resource="system", incident_type="throttling", severity="critical", proposed_action=action
    )


def test_expire_stale_incidents(conn) -> None:
    ttl = 30 * 60_000
    now = 10 * ttl
    old = _mk_incident(conn, now - ttl - 1)
    recent = _mk_incident(conn, now - 1000)
    pending = _mk_incident(conn, now - ttl - 1, action="soft")
    db.create_decision(conn, pending, "allow_once", now_ms=now - 10)

    assert db.expire_stale_incidents(conn, now, ttl) == 1
    assert db.get_incident(conn, old).status == "expired"
    assert db.get_incident(conn, old).closed_at_ms == now
    assert db.get_incident(conn, recent).status == "open"
    assert db.get_incident(conn, pending).status == "open"
    assert db.expire_stale_incidents(conn, now, ttl) == 0


def test_has_actionable_open_incident(conn) -> None:
    assert not db.has_actionable_open_incident(conn)
    _mk_incident(conn, 1000)
    assert not db.has_actionable_open_incident(conn)
    _mk_incident(conn, 2000, action="soft")
    assert db.has_actionable_open_incident(conn)
