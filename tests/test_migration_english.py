"""Migrazione 0004: enum italiani -> inglesi su un DB v3 con dati reali e righe figlie (FK)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tslow import database as db


def _make_v3_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for sql_file in sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:3]:
        conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 3")
    return conn


def test_migration_converts_values_keeps_children_and_ids(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    conn = _make_v3_db(path)
    for i, state in enumerate(["calmo", "attenzione", "indagine"]):
        conn.execute("INSERT INTO metrics_raw (ts_ms, sampler_state, period_ms, cpu_percent) VALUES (?, ?, 1000, ?)", (i, state, 10.0 + i))
    conn.execute("INSERT INTO monitor_health (ts_ms, sampler_state) VALUES (1, 'indagine')")
    rows = [("disco", "critico", "critico", "hard", "aperto"), ("rete", "anomalia", "anomalia", "soft", "risolto"),
            ("sistema", "throttling", "critico", "nessuna", "scaduto"), ("cpu", "anomalia", "anomalia", "soft", "ignorato")]
    for i, (res, typ, sev, act, st) in enumerate(rows):
        conn.execute(
            "INSERT INTO incidents (opened_at_ms, resource, incident_type, severity, proposed_action, status, group_key) VALUES (?, ?, ?, ?, ?, ?, 'x')",
            (i, res, typ, sev, act, st),
        )
    conn.execute("INSERT INTO decisions (incident_id, created_at_ms, choice, source, execution_status) VALUES (2, 5, 'deny', 'user', 'pending')")
    conn.execute("INSERT INTO notifications (incident_id, sent_at_ms, channel, success) VALUES (1, 5, 'log', 1)")
    conn.execute("DELETE FROM incidents WHERE id = 4")  # il contatore AUTOINCREMENT deve restare a 4
    conn.close()

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [r[0] for r in conn.execute("SELECT sampler_state FROM metrics_raw ORDER BY id")] == ["idle", "watching", "investigating"]
        assert conn.execute("SELECT sampler_state FROM monitor_health").fetchone()[0] == "investigating"
        got = [tuple(r) for r in conn.execute("SELECT resource, incident_type, severity, proposed_action, status FROM incidents ORDER BY id")]
        assert got == [("disk", "critical", "critical", "hard", "open"), ("network", "anomaly", "anomaly", "soft", "resolved"),
                       ("system", "throttling", "critical", "none", "expired")]
        assert conn.execute("SELECT incident_id FROM decisions").fetchone()[0] == 2
        assert conn.execute("SELECT incident_id FROM notifications").fetchone()[0] == 1
        new_id = db.create_incident(conn, opened_at_ms=9, resource="cpu", incident_type="anomaly", severity="anomaly")
        assert new_id == 5  # id non riutilizzato
        assert db.get_incident(conn, new_id).status == "open"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    assert (tmp_path / "v3.db.bak-v3").exists()  # copia di sicurezza prima della migrazione


def test_old_values_are_rejected_after_migration(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "fresh.db")
    try:
        try:
            conn.execute("INSERT INTO incidents (opened_at_ms, resource, incident_type, severity, proposed_action) VALUES (1, 'disco', 'anomaly', 'anomaly', 'soft')")
        except sqlite3.IntegrityError:
            return
        raise AssertionError("old Italian enum value accepted")
    finally:
        conn.close()
