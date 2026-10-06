"""Test di database.py: migrazioni idempotenti, insert, rollup, retention."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import database as db


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_migrations_create_expected_tables(conn) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    expected = {
        "app_settings",
        "system_inventory",
        "metrics_raw",
        "metrics_1m",
        "metrics_1h",
        "process_minutes",
        "monitor_health",
    }
    assert expected.issubset(tables)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_migrations_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    conn1 = db.connect(path)
    version_after_first_connect = conn1.execute("PRAGMA user_version").fetchone()[0]
    conn1.close()
    conn2 = db.connect(path)  # riapertura non deve fallire ne' riapplicare le migrazioni
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == version_after_first_connect
    conn2.close()


def test_insert_and_rollup(conn) -> None:
    base_ts = 1_000_000_000  # ms, minuto arbitrario allineato
    base_ts = (base_ts // db.MINUTE_MS) * db.MINUTE_MS
    samples = [
        db.MetricSample(
            ts_ms=base_ts + i * 5_000,
            sampler_state="idle",
            period_ms=5000,
            cpu_percent=10.0 + i,
            ram_percent=50.0,
        )
        for i in range(6)  # 6 campioni nello stesso minuto (30s di dati)
    ]
    db.insert_metrics_raw(conn, samples)
    assert conn.execute("SELECT COUNT(*) FROM metrics_raw").fetchone()[0] == 6

    now_ms = base_ts + db.MINUTE_MS + 1  # un istante dopo la fine del minuto
    rolled = db.rollup_1m(conn, now_ms)
    assert rolled == 1
    row = conn.execute("SELECT cpu_percent_avg, cpu_percent_max, sample_count FROM metrics_1m").fetchone()
    assert row[2] == 6
    assert row[1] == pytest.approx(15.0)
    assert row[0] == pytest.approx(12.5)

    # rollup 1h: serve un'ora intera di metrics_1m, con un solo minuto non produce nulla.
    rolled_h = db.rollup_1h(conn, now_ms)
    assert rolled_h == 0


def test_retention_deletes_old_rows(conn) -> None:
    now_ms = 10_000_000_000
    old_ts = now_ms - 100 * 3_600_000  # molto piu' vecchio di 48h
    db.insert_metrics_raw(
        conn,
        [db.MetricSample(ts_ms=old_ts, sampler_state="idle", period_ms=5000, cpu_percent=1.0)],
    )
    db.insert_metrics_raw(
        conn,
        [db.MetricSample(ts_ms=now_ms, sampler_state="idle", period_ms=5000, cpu_percent=1.0)],
    )
    assert conn.execute("SELECT COUNT(*) FROM metrics_raw").fetchone()[0] == 2
    db.apply_retention(conn, now_ms)
    remaining = conn.execute("SELECT ts_ms FROM metrics_raw").fetchall()
    assert [r[0] for r in remaining] == [now_ms]


def test_app_settings_roundtrip(conn) -> None:
    assert db.get_setting(conn, "missing", default="x") == "x"
    db.set_setting(conn, "calibration_started_at", "2026-09-13T00:00:00")
    assert db.get_setting(conn, "calibration_started_at") == "2026-09-13T00:00:00"
    db.set_setting(conn, "calibration_started_at", "2026-09-14T00:00:00")
    assert db.get_setting(conn, "calibration_started_at") == "2026-09-14T00:00:00"
