"""Collector psutil: tick globale a bassissimo costo (<1 ms).

I contatori cumulativi (disco, rete) vanno convertiti in tassi al secondo con RateTracker,
confrontando due letture consecutive: una singola lettura non basta.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import psutil


class RateTracker:
    """Converte un contatore cumulativo in un tasso al secondo tra due letture consecutive."""

    def __init__(self) -> None:
        self._last_value: float | None = None
        self._last_ts: float | None = None

    def update(self, value: float, ts: float | None = None) -> float | None:
        ts = ts if ts is not None else time.monotonic()
        rate = None
        if self._last_value is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt > 0:
                rate = max(0.0, (value - self._last_value) / dt)
        self._last_value = value
        self._last_ts = ts
        return rate


@dataclass
class SystemSnapshot:
    cpu_percent: float
    ram_percent: float
    ram_available_mb: float
    ram_total_mb: float
    disk_read_bytes: int
    disk_write_bytes: int
    net_rx_bytes: int
    net_tx_bytes: int
    disk_free_gb: float
    battery_percent: float | None
    battery_plugged: bool | None


def sample_cpu_percent(interval: float | None = None) -> float:
    """psutil.cpu_percent e' stateful: senza interval usa la finestra dall'ultima chiamata (non bloccante)."""
    return psutil.cpu_percent(interval=interval)


SLOW_CHANGING_TTL_S = 60.0


class TimedCache:
    """Cache a tempo di una funzione senza argomenti: per letture costose che cambiano lentamente."""

    def __init__(self, fn, ttl_s: float = SLOW_CHANGING_TTL_S, clock=time.monotonic) -> None:
        self._fn = fn
        self._ttl_s = ttl_s
        self._clock = clock
        self._value = None
        self._expires = float("-inf")

    def get(self):
        now = self._clock()
        if now >= self._expires:
            self._value = self._fn()
            self._expires = now + self._ttl_s
        return self._value


_disk_free_caches: dict[str, TimedCache] = {}


def _disk_free_gb(disk_path: str) -> float:
    cache = _disk_free_caches.get(disk_path)
    if cache is None:
        cache = _disk_free_caches[disk_path] = TimedCache(lambda: psutil.disk_usage(disk_path).free / (1024**3))
    return cache.get()


def collect(disk_path: str = "C:\\") -> SystemSnapshot:
    vm = psutil.virtual_memory()
    disk_io = psutil.disk_io_counters()
    net_io = psutil.net_io_counters()
    battery = psutil.sensors_battery()
    return SystemSnapshot(
        cpu_percent=sample_cpu_percent(),
        ram_percent=vm.percent,
        ram_available_mb=vm.available / (1024 * 1024),
        ram_total_mb=vm.total / (1024 * 1024),
        disk_read_bytes=disk_io.read_bytes if disk_io else 0,
        disk_write_bytes=disk_io.write_bytes if disk_io else 0,
        net_rx_bytes=net_io.bytes_recv if net_io else 0,
        net_tx_bytes=net_io.bytes_sent if net_io else 0,
        disk_free_gb=_disk_free_gb(disk_path),
        battery_percent=battery.percent if battery else None,
        battery_plugged=battery.power_plugged if battery else None,
    )


def _read_link_speed_mbps() -> int | None:
    stats = psutil.net_if_stats()
    speeds = [s.speed for s in stats.values() if s.isup and s.speed > 0]
    return max(speeds) if speeds else None


_link_speed_cache = TimedCache(_read_link_speed_mbps)


def net_link_speed_mbps() -> int | None:
    """Velocita' massima (Mbps) tra le interfacce di rete attive, per la soglia '% della velocita' del link'.

    Cambia di rado: riletta al piu' ogni SLOW_CHANGING_TTL_S secondi.
    """
    return _link_speed_cache.get()
