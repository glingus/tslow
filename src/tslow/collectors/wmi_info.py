"""Collector WMI (win32com): SOLO fuori dal loop di campionamento.

Ogni query WMI carica `WmiPrvSE.exe`, cioe' l'Effetto Osservatore che il monitor deve evitare
durante il campionamento normale. Qui viene usato solo per:
- inventario hardware una tantum (`collect_inventory`, M1);
- arricchimento del colpevole: servizi ospitati da uno svchost specifico (`services_for_pid`, M2+);
- temperatura ACPI quando si sospetta throttling (`acpi_temperature_celsius`, M2+).

`Win32_Service` su questa macchina costa ~1.1s: mai chiamarlo nel tick del sampler.
La temperatura ACPI richiede tipicamente privilegi elevati: se il chiamante non e' elevato torna
None invece di sollevare un'eccezione (verificato: "Accesso negato" da processo non elevato).
"""

from __future__ import annotations

import logging

import win32com.client

logger = logging.getLogger(__name__)


def _connect(namespace: str = "winmgmts:"):
    return win32com.client.GetObject(namespace)


def collect_inventory() -> dict:
    """Inventario hardware/OS una tantum, per `system_inventory`. Le query costano <0.1s ciascuna."""
    wmi = _connect()

    cpu_name = None
    cpu_cores_physical = None
    cpu_cores_logical = None
    for cpu in wmi.ExecQuery("SELECT Name, NumberOfCores, NumberOfLogicalProcessors FROM Win32_Processor"):
        cpu_name = cpu.Name
        cpu_cores_physical = cpu.NumberOfCores
        cpu_cores_logical = cpu.NumberOfLogicalProcessors
        break

    os_version = None
    os_language = None
    for os_ in wmi.ExecQuery("SELECT Caption, Version, OSLanguage FROM Win32_OperatingSystem"):
        os_version = f"{os_.Caption} ({os_.Version})"
        os_language = str(os_.OSLanguage)
        break

    gpu_names = [gpu.Name for gpu in wmi.ExecQuery("SELECT Name FROM Win32_VideoController") if gpu.Name]

    disk_model = None
    for disk in wmi.ExecQuery("SELECT Model FROM Win32_DiskDrive"):
        disk_model = disk.Model
        break

    return {
        "cpu_name": cpu_name,
        "cpu_cores_physical": cpu_cores_physical,
        "cpu_cores_logical": cpu_cores_logical,
        "gpu_names": ", ".join(gpu_names) if gpu_names else None,
        "disk_model": disk_model,
        "os_version": os_version,
        "os_language": os_language,
    }


def services_for_pid(pid: int) -> list[str]:
    """Nomi visualizzati dei servizi Windows attivi ospitati dal processo `pid` (tipicamente uno svchost)."""
    wmi = _connect()
    query = f"SELECT DisplayName FROM Win32_Service WHERE ProcessId={int(pid)} AND State='Running'"
    try:
        return [row.DisplayName for row in wmi.ExecQuery(query) if row.DisplayName]
    except Exception:
        logger.warning("Interrogazione Win32_Service fallita per PID %s", pid, exc_info=True)
        return []


def acpi_temperature_celsius() -> float | None:
    """Temperatura media delle zone termiche ACPI, o None se non disponibile (richiede privilegi elevati)."""
    try:
        wmi_acpi = _connect(r"winmgmts:\\.\root\wmi")
        readings = [
            (row.CurrentTemperature / 10.0) - 273.15
            for row in wmi_acpi.ExecQuery("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
        ]
    except Exception:
        logger.debug("Temperatura ACPI non disponibile (richiede privilegi elevati)", exc_info=True)
        return None
    if not readings:
        return None
    return sum(readings) / len(readings)
