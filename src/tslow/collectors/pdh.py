"""Collector PDH (win32pdh): contatori in inglese, indipendenti dalla lingua di Windows.

Windows in italiano traduce i nomi visualizzati dei contatori PDH; `AddEnglishCounter` usa i nomi
canonici in inglese e funziona indipendentemente dalla lingua del sistema (verificato su questa
macchina in it-IT). La query resta aperta per tutta la vita del demone (vedi PdhCollector): i
contatori rate-based (es. Page Reads/sec) richiedono due CollectQueryData per restituire un
valore, quindi il primo campione dopo l'apertura puo' avere alcuni valori a None.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psutil
import win32pdh

logger = logging.getLogger(__name__)

_COUNTER_PATHS = {
    "cpu_queue_length": r"\System\Processor Queue Length",
    "cpu_perf_percent": r"\Processor Information(_Total)\% Processor Performance",
    "cpu_perf_limit_percent": r"\Processor Information(_Total)\% Performance Limit",
    "ram_commit_percent": r"\Memory\% Committed Bytes In Use",
    "page_reads_sec": r"\Memory\Page Reads/sec",
    "disk_latency_sec": r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer",
    "disk_queue_length": r"\PhysicalDisk(_Total)\Current Disk Queue Length",
    "disk_iops": r"\PhysicalDisk(_Total)\Disk Transfers/sec",
}


@dataclass
class PdhSnapshot:
    cpu_queue_per_core: float | None
    cpu_perf_percent: float | None
    cpu_perf_limit_percent: float | None
    ram_commit_percent: float | None
    page_reads_sec: float | None
    disk_latency_ms: float | None
    disk_queue_length: float | None
    disk_iops: float | None


class PdhCollector:
    """Query PDH persistente: un'istanza per demone, chiudere con close() allo spegnimento."""

    def __init__(self) -> None:
        self._query = win32pdh.OpenQuery()
        self._handles: dict[str, int] = {}
        self._unavailable: set[str] = set()
        for name, path in _COUNTER_PATHS.items():
            try:
                self._handles[name] = win32pdh.AddEnglishCounter(self._query, path)
            except Exception:
                logger.warning("Contatore PDH non disponibile, disattivato: %s", path)
                self._unavailable.add(name)

    def unavailable_counters(self) -> set[str]:
        return set(self._unavailable)

    def sample(self) -> PdhSnapshot:
        win32pdh.CollectQueryData(self._query)
        values: dict[str, float | None] = {}
        for name, handle in self._handles.items():
            try:
                _, val = win32pdh.GetFormattedCounterValue(handle, win32pdh.PDH_FMT_DOUBLE)
                values[name] = val
            except Exception:
                # Puo' capitare al primo campione dopo l'apertura (contatori rate-based).
                values[name] = None

        cores = psutil.cpu_count(logical=True) or 1
        cpu_queue = values.get("cpu_queue_length")
        disk_latency_sec = values.get("disk_latency_sec")
        return PdhSnapshot(
            cpu_queue_per_core=(cpu_queue / cores) if cpu_queue is not None else None,
            cpu_perf_percent=values.get("cpu_perf_percent"),
            cpu_perf_limit_percent=values.get("cpu_perf_limit_percent"),
            ram_commit_percent=values.get("ram_commit_percent"),
            page_reads_sec=values.get("page_reads_sec"),
            disk_latency_ms=(disk_latency_sec * 1000) if disk_latency_sec is not None else None,
            disk_queue_length=values.get("disk_queue_length"),
            disk_iops=values.get("disk_iops"),
        )

    def close(self) -> None:
        win32pdh.CloseQuery(self._query)

    def __enter__(self) -> "PdhCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
