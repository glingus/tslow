"""Pipeline di sicurezza per le azioni sui processi: UNICO punto che tocca kill/priorita/IO/WM_CLOSE.

`apply_decision()` e' l'unica funzione che esegue azioni reali (CLAUDE.md): nessun'altra parte del
codice deve chiamare psutil.Process.kill()/nice()/ionice() o inviare WM_CLOSE. Ogni chiamata passa
da: controllo del livello di protezione -> identita' del processo (PID+create_time, evita PID
riusati) -> validita' della decisione (azione consentita dall'incidente) -> controllo dry-run ->
esecuzione -> riga in actions_log con i valori precedenti (per l'undo).
"""

from __future__ import annotations

import logging
import time

import psutil

from tslow import database as db
from tslow import grouping, protection
from tslow.collectors import windows as windows_collector
from tslow.collectors.processes import ProcessSnapshot
from tslow.config import Settings

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def apply_decision(
    conn,
    settings: Settings,
    decision: db.Decision,
    incident: db.Incident,
    snapshots: dict[int, ProcessSnapshot],
    group_pids: list[int],
    *,
    dry_run: bool,
) -> str:
    """Esegue (o simula, se dry_run) l'azione per una decisione. Aggiorna decisions.execution_status."""
    now = _now_ms()

    if decision.choice == "deny":
        db.update_decision_status(conn, decision.id, "ok", now)
        return "ok"

    if incident.group_key is None or incident.proposed_action == "none":
        db.update_decision_status(conn, decision.id, "protected_refused", now)
        return "protected_refused"

    action_kind = incident.proposed_action  # 'soft' | 'hard'
    verified, worst_outcome = _verify_group(action_kind, snapshots, group_pids)

    if not verified:
        db.update_decision_status(conn, decision.id, worst_outcome, now)
        return worst_outcome

    if dry_run:
        for pid, _proc, _snap in verified:
            db.record_action(
                conn, decision_id=decision.id, incident_id=incident.id, executed_at_ms=_now_ms(),
                action="soft_priority" if action_kind == "soft" else "hard_close", pid=pid,
                previous_priority=None, previous_io_priority=None, outcome="dry_run",
            )
        db.update_decision_status(conn, decision.id, "dry_run", _now_ms())
        return "dry_run"

    outcome = _apply_soft(conn, decision.id, incident, verified) if action_kind == "soft" else _apply_hard(
        conn, settings, decision.id, incident, verified
    )
    db.update_decision_status(conn, decision.id, outcome, _now_ms())
    return outcome


def _verify_group(
    action_kind: str, snapshots: dict[int, ProcessSnapshot], group_pids: list[int]
) -> tuple[list[tuple[int, "psutil.Process", ProcessSnapshot]], str]:
    """Controllo di protezione + identita' per OGNI PID del gruppo, prima di agire su chiunque."""
    verified: list[tuple[int, psutil.Process, ProcessSnapshot]] = []
    worst_outcome = "ok"
    for pid in group_pids:
        snap = snapshots.get(pid)
        if snap is None:
            continue
        if protection.is_own_process(pid):
            worst_outcome = "protected_refused"
            continue
        ancestors = grouping.ancestor_names(pid, snapshots)
        result = protection.classify(snap.name, None, ancestors)
        if result.level == protection.ProtectionLevel.L0_UNTOUCHABLE:
            worst_outcome = "protected_refused"
            continue
        if action_kind == "hard" and result.level == protection.ProtectionLevel.L2_SOFT_ONLY:
            worst_outcome = "protected_refused"  # L2: mai kill, solo priorita' bassa
            continue
        try:
            proc = psutil.Process(pid)
            if abs(proc.create_time() - snap.create_time) > 1.0:
                worst_outcome = "identity_mismatch"
                continue
        except psutil.NoSuchProcess:
            worst_outcome = "no_such_process"
            continue
        verified.append((pid, proc, snap))
    return verified, worst_outcome


def _apply_soft(conn, decision_id: int, incident: db.Incident, verified: list) -> str:
    outcome = "ok"
    for pid, proc, _snap in verified:
        try:
            previous = proc.nice()
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            db.record_action(
                conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(),
                action="soft_priority", pid=pid, previous_priority=int(previous), previous_io_priority=None, outcome="ok",
            )
        except psutil.NoSuchProcess:
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="soft_priority", pid=pid, previous_priority=None, previous_io_priority=None, outcome="no_such_process")
            outcome = "no_such_process"
            continue
        except psutil.AccessDenied:
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="soft_priority", pid=pid, previous_priority=None, previous_io_priority=None, outcome="access_denied")
            outcome = "access_denied"
            continue

        if incident.resource == "disk":
            try:
                prev_io = proc.ionice()
                proc.ionice(psutil.IOPRIO_LOW)
                db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="soft_ionice", pid=pid, previous_priority=None, previous_io_priority=int(prev_io), outcome="ok")
            except (psutil.Error, OSError):
                logger.debug("ionice non disponibile per PID %s", pid, exc_info=True)
    return outcome


def _apply_hard(conn, settings: Settings, decision_id: int, incident: db.Incident, verified: list) -> str:
    hung = incident.incident_type == "hung"
    ram_critical = incident.resource == "ram" and incident.severity == "critical"
    wait_s = (
        settings.get("actions", "hard_reduced_wait_seconds", default=3)
        if (hung or ram_critical)
        else settings.get("actions", "hard_wm_close_wait_seconds", default=10)
    )

    had_windows = False
    for pid, _proc, _snap in verified:
        closed = windows_collector.close_windows_for_pid(pid)
        if closed > 0:
            had_windows = True
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="hard_close", pid=pid, previous_priority=None, previous_io_priority=None, outcome="ok")

    if had_windows:
        time.sleep(wait_s)

    outcome = "ok"
    # i figli (create_time piu' recente) prima della radice.
    ordered = sorted(verified, key=lambda item: item[2].create_time, reverse=True)
    for pid, proc, _snap in ordered:
        try:
            if not proc.is_running():
                continue
            proc.kill()
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="hard_kill", pid=pid, previous_priority=None, previous_io_priority=None, outcome="ok")
        except psutil.NoSuchProcess:
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="hard_kill", pid=pid, previous_priority=None, previous_io_priority=None, outcome="no_such_process")
        except psutil.AccessDenied:
            db.record_action(conn, decision_id=decision_id, incident_id=incident.id, executed_at_ms=_now_ms(), action="hard_kill", pid=pid, previous_priority=None, previous_io_priority=None, outcome="access_denied")
            outcome = "access_denied"
    return outcome


def undo(conn, decision_id: int) -> str:
    """Ripristina priorita' CPU/IO delle azioni Soft di una decisione. Hard non e' reversibile."""
    actions = db.list_actions_for_decision(conn, decision_id)
    restored = 0
    for a in actions:
        if a.action not in ("soft_priority", "soft_ionice") or a.outcome != "ok":
            continue
        try:
            proc = psutil.Process(a.pid)
            if a.action == "soft_priority" and a.previous_priority is not None:
                proc.nice(a.previous_priority)
                restored += 1
            elif a.action == "soft_ionice" and a.previous_io_priority is not None:
                proc.ionice(a.previous_io_priority)
                restored += 1
            db.record_action(conn, decision_id=decision_id, incident_id=a.incident_id, executed_at_ms=_now_ms(), action="undo", pid=a.pid, previous_priority=None, previous_io_priority=None, outcome="ok")
        except psutil.NoSuchProcess:
            db.record_action(conn, decision_id=decision_id, incident_id=a.incident_id, executed_at_ms=_now_ms(), action="undo", pid=a.pid, previous_priority=None, previous_io_priority=None, outcome="no_such_process")
        except psutil.AccessDenied:
            db.record_action(conn, decision_id=decision_id, incident_id=a.incident_id, executed_at_ms=_now_ms(), action="undo", pid=a.pid, previous_priority=None, previous_io_priority=None, outcome="access_denied")
    return "ok" if restored else "no_such_process"
