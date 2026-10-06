"""`tslow resolve`: mostra un incidente e chiede [1] Nega [2] Consenti una volta [3] Consenti sempre.

La CLI NON esegue mai azioni: scrive la decisione nel DB, il demone la valida e la esegue, qui si
aspetta l'esito e lo si mostra (vedi Architettura.md, modello di sicurezza).
"""

from __future__ import annotations

import time

import typer
from rich.console import Console
from rich.table import Table

from tslow import database as db
from tslow import labels
from tslow import rules
from tslow.config import load_settings
from tslow.protection import ProtectionLevel

console = Console()

_CHOICE_MAP = {"1": "deny", "2": "allow_once", "3": "allow_always"}
_CHOICE_LABELS = {"deny": "Deny", "allow_once": "Allow once", "allow_always": "Always allow"}


def _format_duration(opened_at_ms: int) -> str:
    seconds = max(0, int(time.time() - opened_at_ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60} min"


def _print_incident(incident: db.Incident) -> None:
    table = Table(show_header=False, title=f"Incident #{incident.id}")
    table.add_row("Resource", labels.label(labels.RESOURCE_LABELS, incident.resource))
    table.add_row("Type", labels.label(labels.INCIDENT_TYPE_LABELS, incident.incident_type))
    table.add_row("Severity", labels.label(labels.SEVERITY_LABELS, incident.severity))
    table.add_row("App / group", incident.group_key or "(system event)")
    table.add_row("Usage", f"{incident.quota_percent:.1f}" if incident.quota_percent is not None else "n/a")
    table.add_row("Duration", _format_duration(incident.opened_at_ms))
    table.add_row("Protection level", str(incident.protection_level) if incident.protection_level is not None else "n/a")
    table.add_row("Proposed action", labels.label(labels.PROPOSED_ACTION_LABELS, incident.proposed_action))
    table.add_row("Status", labels.label(labels.STATUS_LABELS, incident.status))
    console.print(table)


def show_and_resolve(incident_id: int | None) -> None:
    conn = db.connect()
    try:
        if incident_id is not None:
            incident = db.get_incident(conn, incident_id)
        else:
            open_incidents = db.list_open_incidents(conn)
            incident = open_incidents[0] if open_incidents else None

        if incident is None:
            console.print("[bold]Nothing to resolve.[/bold]")
            return

        _print_incident(incident)

        is_l0 = incident.protection_level == int(ProtectionLevel.L0_UNTOUCHABLE)
        is_system = incident.group_key is None
        if is_l0 or is_system:
            console.print("[dim]Informational only: no action possible (protected process or system event).[/dim]")
            return

        if incident.status != "open":
            console.print(f"[dim]This incident is already '{labels.label(labels.STATUS_LABELS, incident.status)}': nothing to do.[/dim]")
            return

        console.print("\n[1] Deny   [2] Allow once   [3] Always allow")
        raw_choice = typer.prompt("Choice", default="1").strip()
        choice = _CHOICE_MAP.get(raw_choice)
        if choice is None:
            console.print(f"[red]Invalid choice: {raw_choice!r}[/red]")
            raise typer.Exit(code=1)

        now_ms = int(time.time() * 1000)
        settings = load_settings()
        identity = rules.app_identity(incident.exe_path, incident.group_key)
        rules.apply_decision(conn, settings, identity, choice, now_ms)
        decision_id = db.create_decision(conn, incident.id, choice, now_ms=now_ms)

        console.print(f"Recorded: {_CHOICE_LABELS[choice]}. Waiting for the watcher to act on it...")
        deadline = time.monotonic() + 15
        decision = db.get_decision(conn, decision_id)
        while decision.execution_status == "pending" and time.monotonic() < deadline:
            time.sleep(0.5)
            decision = db.get_decision(conn, decision_id)

        if decision.execution_status == "pending":
            console.print("[yellow]The watcher hasn't executed it yet (is it running?).[/yellow]")
        else:
            console.print(f"Result: [bold]{decision.execution_status}[/bold]  (decision #{decision.id})")
    finally:
        conn.close()
