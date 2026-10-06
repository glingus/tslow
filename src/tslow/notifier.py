"""Notifiche: Windows-Toasts -> plyer -> solo log. Applica i filtri (calibrazione, protezione,
primo piano, limiti/cooldown) PRIMA di inviare: detector.py crea sempre l'incidente per lo
storico, should_notify() decide se disturbare davvero l'utente.

`win10toast` non funziona su Python >=3.11 (errore "WPARAM is simple") ed e' abbandonata: si usa
Windows-Toasts (verificato: show_toast() non solleva eccezioni su questa macchina).
"""

from __future__ import annotations

import logging
import time

from tslow import database as db
from tslow.config import Settings
from tslow.protection import ProtectionLevel

logger = logging.getLogger(__name__)

_TOASTER = None


def _get_toaster():
    global _TOASTER
    if _TOASTER is None:
        from windows_toasts import WindowsToaster

        _TOASTER = WindowsToaster("TSlow Monitor")
    return _TOASTER


def _in_calibration(conn, settings: Settings, now_ms: int) -> bool:
    started = db.get_setting(conn, "calibration_started_at_ms")
    if started is None:
        db.set_setting(conn, "calibration_started_at_ms", str(now_ms))
        return True
    duration_ms = int(settings.get("calibration", "duration_hours", default=24) * 3_600_000)
    return (now_ms - int(started)) < duration_ms


def should_notify(
    conn,
    incident: db.Incident,
    settings: Settings,
    *,
    now_ms: int,
    is_foreground: bool = False,
) -> tuple[bool, str]:
    """Decide se inviare una notifica per `incident`. Ritorna (invia, motivo)."""
    if _in_calibration(conn, settings, now_ms) and incident.severity != "critical":
        return False, "in calibrazione (24h): solo critico/leak/hung/throttling notificano"

    if incident.protection_level == ProtectionLevel.L1_FREE_REIN and incident.severity != "critical":
        return False, "processo L1 (carta bianca): solo il livello critico notifica"

    if is_foreground and incident.resource in ("cpu", "gpu") and incident.severity != "critical":
        return False, "app in primo piano: esente dalle anomalie non critiche di CPU/GPU"

    cooldown_minutes = settings.get("notifications", "protected_cooldown_minutes", default=60)
    if incident.protection_level != ProtectionLevel.L0_UNTOUCHABLE and incident.group_key is not None:
        cooldown_minutes = settings.get("notifications", "app_cooldown_minutes", default=15)

    if incident.group_key is not None:
        last = db.last_notification_for_group(conn, incident.group_key)
        if last is not None and (now_ms - last) < cooldown_minutes * 60_000:
            return False, f"cooldown attivo ({cooldown_minutes} min)"

    limit_per_hour = settings.get("notifications", "hourly_limit_total", default=4)
    count = db.notifications_count_since(conn, now_ms - 3_600_000)
    if count >= limit_per_hour:
        return False, f"limite orario raggiunto ({limit_per_hour}/h)"

    return True, "ok"


def _toast_text(incident: db.Incident, app_name: str) -> tuple[str, str]:
    if incident.resource == "system":
        title = "why ts so slow? — throttling detected" if incident.incident_type == "throttling" else "why ts so slow? — system event"
        body = "Your CPU is throttling itself (overheating or a power-saving mode)."
        return title, body

    if incident.protection_level == ProtectionLevel.L0_UNTOUCHABLE:
        title = f"why ts so slow? — {app_name} is protected"
        body = f"{app_name} is using a lot of resources, but it's a protected process: no automatic action."
        return title, body

    if incident.incident_type == "leak":
        title = f"why ts so slow? — {app_name} might have a memory leak"
        body = "Its RAM has been climbing steadily for several minutes. Open the CLI to deal with it."
        return title, body

    if incident.incident_type == "hung":
        title = f"why ts so slow? — {app_name} isn't responding"
        body = "It's been frozen for at least 30 seconds. Open the CLI to deal with it."
        return title, body

    title = f"why ts so slow? — {app_name} is slowing your PC down"
    body = f"{app_name} is why ts so slow right now. Open the CLI to deal with it."
    return title, body


def send_text(title: str, body: str) -> bool:
    """Toast di sola informazione (nessun incidente, nessuna riga in `notifications`), es. "aggiornamento disponibile"."""
    try:
        from windows_toasts import Toast

        toast = Toast()
        toast.text_fields = [title, body]
        toast.group = "tslow"
        _get_toaster().show_toast(toast)
        return True
    except Exception:
        logger.warning("Toast informativo fallito: %s - %s", title, body, exc_info=True)
        return False


def send(conn, incident: db.Incident, *, app_name: str, now_ms: int | None = None) -> bool:
    """Invia la notifica per un incidente gia' approvato da should_notify(). Registra l'esito in `notifications`."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    title, body = _toast_text(incident, app_name)

    try:
        from windows_toasts import Toast

        toaster = _get_toaster()
        toast = Toast()
        toast.text_fields = [title, body]
        toast.launch_action = f"tslow://resolve?incident={incident.id}"
        toast.group = "tslow"
        toast.tag = f"incident-{incident.id}"
        toaster.show_toast(toast)
        db.record_notification(conn, incident.id, now_ms, channel="toast", success=True)
        return True
    except Exception as exc:
        logger.warning("Toast fallito per l'incidente %s, provo plyer", incident.id, exc_info=True)
        try:
            from plyer import notification as plyer_notification

            plyer_notification.notify(title=title, message=body, app_name="TSlow", timeout=10)
            db.record_notification(conn, incident.id, now_ms, channel="plyer", success=True)
            return True
        except Exception as exc2:
            logger.error("Anche plyer e' fallito per l'incidente %s: %s / %s", incident.id, exc, exc2)
            db.record_notification(conn, incident.id, now_ms, channel="log", success=False, error=str(exc2))
            logger.warning("NOTIFICA (solo log): %s - %s", title, body)
            return False
