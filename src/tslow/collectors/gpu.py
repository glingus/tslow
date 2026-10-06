"""Collector GPU per processo: PDH `\\GPU Engine(*)\\Utilization Percentage` aggregato per PID.

Non esiste un contatore GPU "per processo" diretto in psutil/WMI: ogni istanza di questo contatore
rappresenta un motore GPU (3D, VideoDecode, GDI Render, ...) di un processo, con il PID codificato
nel nome dell'istanza (`pid_<PID>_luid_..._eng_<N>_engtype_<Tipo>`). Il valore per processo e' la
somma di tutti i suoi motori. Verificato su questa macchina (GPU MX130 + UHD 620): 349-358 istanze
attive, aggregazione per PID corretta.
"""

from __future__ import annotations

import logging
import re

import win32pdh

logger = logging.getLogger(__name__)

_COUNTER_PATH = r"\GPU Engine(*)\Utilization Percentage"
_PID_RE = re.compile(r"pid_(\d+)_")


class GpuCollector:
    """Query PDH persistente per il motore GPU. `available()` e' False se manca un driver compatibile."""

    def __init__(self) -> None:
        self._query = win32pdh.OpenQuery()
        self._handle = None
        self._available = True
        try:
            self._handle = win32pdh.AddCounter(self._query, _COUNTER_PATH)
        except Exception:
            logger.warning("Contatore PDH 'GPU Engine' non disponibile su questo sistema")
            self._available = False

    def available(self) -> bool:
        return self._available

    def sample_raw(self) -> dict[str, float]:
        """Un CollectQueryData: valore per istanza (motore GPU), chiave = nome istanza PDH."""
        if not self._available:
            return {}
        win32pdh.CollectQueryData(self._query)
        try:
            return dict(win32pdh.GetFormattedCounterArray(self._handle, win32pdh.PDH_FMT_DOUBLE))
        except Exception:
            # Puo' capitare al primo campione dopo l'apertura (contatore rate-based).
            return {}

    def close(self) -> None:
        win32pdh.CloseQuery(self._query)

    def __enter__(self) -> "GpuCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def aggregate_per_pid(raw: dict[str, float]) -> dict[int, float]:
    """Somma i motori GPU di ogni processo (per l'attribuzione del colpevole in detector.py, M2)."""
    totals: dict[int, float] = {}
    for instance_name, value in raw.items():
        match = _PID_RE.search(instance_name)
        if not match:
            continue
        pid = int(match.group(1))
        totals[pid] = totals.get(pid, 0.0) + value
    return totals


def max_engine_percent(raw: dict[str, float]) -> float | None:
    """Motore GPU piu' occupato in questo istante: e' quello che metrics_raw.gpu_percent registra.

    Sommare tutti i motori (come fa aggregate_per_pid per processo) sovrastimerebbe l'utilizzo
    complessivo della GPU, perche' piu' motori possono lavorare in parallelo sullo stesso chip;
    il massimo per singolo motore si avvicina di piu' a quello che Gestione attivita' mostra come
    "GPU" ed e' coerente con la soglia di anomalia (per motore, non per somma).
    """
    if not raw:
        return None
    return max(raw.values())
