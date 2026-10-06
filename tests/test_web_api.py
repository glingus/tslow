"""API web (M6): TestClient su un DB temporaneo iniettato via dependency override.

Stesso principio dei test di cli/dashboard.py: nessuno stato condiviso, ogni test isola il
proprio file DB. L'API e' sola lettura sopra analytics.py, quindi non serve mai testare
azioni sui processi qui (gia' coperto da test_optimizer.py).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tslow import database as db
from tslow.web.api import app, get_connection


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "web_test.db"
    # check_same_thread=False: la connessione e' creata nel thread del test ma usata dagli
    # endpoint nel threadpool di FastAPI/anyio (vedi commento su database.connect).
    conn = db.connect(db_path, check_same_thread=False)

    def _override():
        yield conn

    app.dependency_overrides[get_connection] = _override
    yield TestClient(app), conn
    app.dependency_overrides.clear()
    conn.close()


def test_health(client) -> None:
    test_client, _ = client
    resp = test_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_rejects_unknown_range(client) -> None:
    test_client, _ = client
    resp = test_client.get("/api/metrics", params={"range": "9999y"})
    assert resp.status_code == 400


def test_metrics_returns_raw_samples_at_1h(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    for i in range(5):
        db.insert_metrics_raw(
            conn,
            [db.MetricSample(ts_ms=now_ms - (5 - i) * 1000, sampler_state="idle", period_ms=1000, cpu_percent=float(i * 10))],
        )

    resp = test_client.get("/api/metrics", params={"range": "1h"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "1h"
    assert body["columns"]["cpu"] == "cpu_percent"
    assert len(body["samples"]) == 5
    assert body["samples"][-1]["cpu_percent"] == pytest.approx(40.0)
    assert body["trend"]["cpu"] == pytest.approx(36000.0)  # 10%/s di crescita -> 36000%/h


def test_metrics_resolves_rollup_columns_at_24h(client) -> None:
    # Stessa regressione di test_analytics.py::test_metric_column_falls_back_to_rollup_suffix,
    # ma verificata end-to-end attraverso l'endpoint HTTP: senza analytics.metric_column
    # l'API restituirebbe colonne "cpu_percent" assenti (rimane solo cpu_percent_avg) e un
    # frontend che le legge per nome diretto vedrebbe un grafico vuoto nonostante dati reali.
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    db.insert_metrics_raw(conn, [db.MetricSample(ts_ms=now_ms - 5 * 60_000, sampler_state="idle", period_ms=5000, cpu_percent=55.0)])
    db.rollup_1m(conn, now_ms)

    resp = test_client.get("/api/metrics", params={"range": "24h"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns"]["cpu"] == "cpu_percent_avg"
    assert len(body["samples"]) == 1
    assert body["samples"][0]["cpu_percent_avg"] == pytest.approx(55.0)


def test_metrics_samples_are_json_safe_with_nulls(client) -> None:
    # cpu_percent non impostato (None) deve arrivare come null JSON, non far fallire la serializzazione.
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    db.insert_metrics_raw(conn, [db.MetricSample(ts_ms=now_ms, sampler_state="idle", period_ms=1000)])

    resp = test_client.get("/api/metrics", params={"range": "1h"})
    assert resp.status_code == 200
    assert resp.json()["samples"][0]["cpu_percent"] is None


def test_top_culprits_orders_by_incident_count(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    for _ in range(2):
        db.create_incident(conn, opened_at_ms=now_ms, resource="cpu", incident_type="anomaly", severity="anomaly", group_key="chrome", proposed_action="soft")
    db.create_incident(conn, opened_at_ms=now_ms, resource="ram", incident_type="critical", severity="critical", group_key="code", proposed_action="hard")

    resp = test_client.get("/api/top-culprits", params={"range": "24h"})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["group_key"] == "chrome"
    assert body[0]["incidenti"] == 2


def test_decision_stats(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(conn, opened_at_ms=now_ms, resource="cpu", incident_type="anomaly", severity="anomaly", group_key="chrome", proposed_action="soft")
    db.create_decision(conn, incident_id, "allow_once", source="user", now_ms=now_ms)

    resp = test_client.get("/api/decisions/stats", params={"range": "24h"})
    assert resp.status_code == 200
    assert resp.json() == [{"choice": "allow_once", "source": "user", "conteggio": 1}]


def test_incidents_history(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    db.create_incident(conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical", group_key="chrome", proposed_action="hard")

    resp = test_client.get("/api/incidents", params={"range": "24h"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["resource"] == "cpu"


def test_overhead_empty_when_no_data(client) -> None:
    test_client, _ = client
    resp = test_client.get("/api/overhead", params={"range": "24h"})
    assert resp.status_code == 200
    assert resp.json()["samples"] == 0


def test_overhead_series_returns_samples(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    db.record_monitor_health(conn, now_ms, daemon_cpu_percent=0.2, daemon_rss_mb=42.0, sampler_state="idle", period_multiplier=1.0)

    resp = test_client.get("/api/overhead/series", params={"range": "24h"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["daemon_cpu_percent"] == pytest.approx(0.2)


def test_post_decision_writes_pending_decision(client) -> None:
    # Stessa scrittura di `tslow resolve` (rules.apply_decision + db.create_decision), solo
    # senza il prompt: verifica end-to-end che il demone troverebbe la decisione da eseguire.
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="chrome", exe_path=r"C:\chrome.exe", proposed_action="soft", protection_level=3,
    )

    resp = test_client.post(f"/api/incidents/{incident_id}/decision", json={"choice": "allow_once"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_status"] == "pending"

    decision = db.get_decision(conn, body["decision_id"])
    assert decision.choice == "allow_once"
    assert decision.incident_id == incident_id


def test_post_decision_404_for_missing_incident(client) -> None:
    test_client, _ = client
    resp = test_client.post("/api/incidents/999/decision", json={"choice": "deny"})
    assert resp.status_code == 404


def test_post_decision_409_when_already_resolved(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="chrome", proposed_action="soft", protection_level=3,
    )
    db.close_incident(conn, incident_id, now_ms, status="resolved")

    resp = test_client.post(f"/api/incidents/{incident_id}/decision", json={"choice": "deny"})
    assert resp.status_code == 409


def test_post_decision_409_for_l0_process(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="explorer", proposed_action="none", protection_level=0,
    )

    resp = test_client.post(f"/api/incidents/{incident_id}/decision", json={"choice": "allow_once"})
    assert resp.status_code == 409


def test_post_decision_409_for_system_incident(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="system", incident_type="throttling", severity="anomaly",
        group_key=None, proposed_action="none",
    )

    resp = test_client.post(f"/api/incidents/{incident_id}/decision", json={"choice": "allow_once"})
    assert resp.status_code == 409


def test_post_decision_rejects_invalid_choice(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="chrome", proposed_action="soft", protection_level=3,
    )
    resp = test_client.post(f"/api/incidents/{incident_id}/decision", json={"choice": "yolo"})
    assert resp.status_code == 422


def test_get_decision_status(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    incident_id = db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="chrome", proposed_action="soft", protection_level=3,
    )
    decision_id = db.create_decision(conn, incident_id, "allow_once", now_ms=now_ms)

    resp = test_client.get(f"/api/decisions/{decision_id}")
    assert resp.status_code == 200
    assert resp.json()["execution_status"] == "pending"


def test_get_decision_status_404(client) -> None:
    test_client, _ = client
    resp = test_client.get("/api/decisions/999")
    assert resp.status_code == 404


def test_nasa_comparison_no_data(client) -> None:
    test_client, _ = client
    resp = test_client.get("/api/nasa")
    assert resp.status_code == 200
    assert resp.json()["worse_percent"] == 0


def test_nasa_comparison_uses_latest_sample(client) -> None:
    test_client, conn = client
    now_ms = int(time.time() * 1000)
    db.insert_metrics_raw(conn, [db.MetricSample(ts_ms=now_ms, sampler_state="idle", period_ms=1000, cpu_percent=90.0, ram_percent=90.0)])

    resp = test_client.get("/api/nasa")
    assert resp.status_code == 200
    body = resp.json()
    assert body["worse_percent"] > 0
    assert "line" in body
