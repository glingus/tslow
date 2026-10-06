"""optimizer.py: test con processi reali usa-e-getta (mai su processi dell'utente, CLAUDE.md).

Ogni test crea un python.exe disposable che dorme e lo termina sempre in teardown, a prescindere
da cosa il test gli abbia fatto.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from tslow import database as db
from tslow import optimizer
from tslow.collectors.processes import ProcessSnapshot
from tslow.config import load_settings


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def dummy_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.3)  # tempo di avviarsi
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _snapshot_for(pid: int) -> ProcessSnapshot:
    p = psutil.Process(pid)
    return ProcessSnapshot(
        pid=pid, ppid=p.ppid(), name=p.name(), create_time=p.create_time(), thread_count=1,
        handle_count=10, working_set_bytes=0, private_bytes=0, cpu_time_seconds=0.0,
        io_read_bytes=0, io_write_bytes=0, io_other_bytes=0,
    )


def _make_incident(conn, *, resource="cpu", severity="critical", action="soft", incident_type=None) -> db.Incident:
    incident_id = db.create_incident(
        conn, opened_at_ms=1000, resource=resource, incident_type=incident_type or severity, severity=severity,
        group_key="dummy", proposed_action=action,
    )
    return db.get_incident(conn, incident_id)


def _decide(conn, incident: db.Incident, choice: str) -> db.Decision:
    decision_id = db.create_decision(conn, incident.id, choice, now_ms=1000)
    return db.get_decision(conn, decision_id)


def test_dry_run_never_touches_the_process(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")
    original_priority = psutil.Process(dummy_process.pid).nice()

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=True
    )

    assert outcome == "dry_run"
    assert psutil.Process(dummy_process.pid).nice() == original_priority
    assert dummy_process.poll() is None


def test_soft_lowers_priority_of_real_process(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )

    assert outcome == "ok"
    assert psutil.Process(dummy_process.pid).nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
    assert dummy_process.poll() is None  # Soft non termina il processo
    actions = db.list_actions_for_decision(conn, decision.id)
    assert any(a.action == "soft_priority" and a.outcome == "ok" for a in actions)


def test_hard_kills_the_real_process(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    incident = _make_incident(conn, resource="ram", severity="critical", action="hard", incident_type="leak")
    decision = _decide(conn, incident, "allow_once")

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )
    dummy_process.wait(timeout=5)

    assert outcome == "ok"
    assert dummy_process.poll() is not None
    actions = db.list_actions_for_decision(conn, decision.id)
    assert any(a.action == "hard_kill" and a.outcome == "ok" for a in actions)


def test_deny_does_not_touch_the_process(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    incident = _make_incident(conn, action="hard")
    decision = _decide(conn, incident, "deny")

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )

    assert outcome == "ok"
    assert dummy_process.poll() is None


def test_identity_mismatch_when_create_time_differs(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    snap.create_time -= 999  # simula un PID riusato da un processo diverso
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )

    assert outcome == "identity_mismatch"
    assert dummy_process.poll() is None


def test_l0_name_is_refused_even_if_incident_said_soft(conn, dummy_process) -> None:
    """optimizer.py non si fida del proposed_action salvato nell'incidente: riclassifica sempre."""
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    snap.name = "explorer.exe"
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )

    assert outcome == "protected_refused"
    assert dummy_process.poll() is None


def test_undo_restores_priority_after_soft(conn, dummy_process) -> None:
    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")
    original_priority = psutil.Process(dummy_process.pid).nice()

    optimizer.apply_decision(conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False)
    assert psutil.Process(dummy_process.pid).nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS

    result = optimizer.undo(conn, decision.id)
    assert result == "ok"
    assert psutil.Process(dummy_process.pid).nice() == original_priority


def test_app_listed_only_in_user_config_is_refused(conn, dummy_process) -> None:
    from tslow import protection

    settings = load_settings()
    snap = _snapshot_for(dummy_process.pid)
    snap.name = "myeditor.exe"
    incident = _make_incident(conn, action="soft")
    decision = _decide(conn, incident, "allow_once")
    protection.set_user_protection(protection.UserProtection(untouchable=frozenset({"myeditor"})))

    outcome = optimizer.apply_decision(
        conn, settings, decision, incident, {dummy_process.pid: snap}, [dummy_process.pid], dry_run=False
    )

    assert outcome == "protected_refused"
    assert dummy_process.poll() is None
