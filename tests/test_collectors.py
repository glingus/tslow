"""Smoke test dei collector su questa macchina reale (Windows 11, i5-8250U, MX130+UHD620)."""

from __future__ import annotations

import sys
import time

import pytest

from tslow.collectors import gpu, pdh, system, wmi_info

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="collector specifico per Windows")


def test_system_collect_returns_plausible_values() -> None:
    snap = system.collect()
    assert 0.0 <= snap.cpu_percent <= 800.0  # 8 core logici, percentuale psutil puo' superare 100
    assert 0.0 <= snap.ram_percent <= 100.0
    assert snap.ram_total_mb > 1024  # almeno 1 GB, sanity check
    assert snap.disk_free_gb >= 0


def test_rate_tracker_computes_rate_between_two_updates() -> None:
    tracker = system.RateTracker()
    assert tracker.update(1000.0, ts=0.0) is None  # prima lettura: nessun tasso ancora
    rate = tracker.update(2000.0, ts=1.0)
    assert rate == pytest.approx(1000.0)


def test_net_link_speed_returns_positive_or_none() -> None:
    speed = system.net_link_speed_mbps()
    assert speed is None or speed > 0


def test_pdh_collector_samples_without_crashing() -> None:
    with pdh.PdhCollector() as collector:
        assert collector.unavailable_counters() == set()  # tutti i contatori validati su questa macchina
        collector.sample()
        time.sleep(0.3)
        snap = collector.sample()
    assert snap.ram_commit_percent is not None
    assert 0.0 <= snap.ram_commit_percent <= 100.0


def test_gpu_collector_available_and_samples() -> None:
    with gpu.GpuCollector() as collector:
        assert collector.available()
        collector.sample_raw()
        time.sleep(0.3)
        raw = collector.sample_raw()
    assert isinstance(raw, dict)
    per_pid = gpu.aggregate_per_pid(raw)
    assert isinstance(per_pid, dict)
    peak = gpu.max_engine_percent(raw)
    assert peak is None or 0.0 <= peak <= 100.0


def test_wmi_inventory_is_well_formed() -> None:
    inventory = wmi_info.collect_inventory()
    assert isinstance(inventory["cpu_name"], str) and inventory["cpu_name"].strip()
    assert inventory["cpu_cores_logical"] >= inventory["cpu_cores_physical"] >= 1
    assert inventory["gpu_names"] is None or isinstance(inventory["gpu_names"], str)


def test_wmi_acpi_temperature_is_float_or_none() -> None:
    temp = wmi_info.acpi_temperature_celsius()
    assert temp is None or isinstance(temp, float)


def test_timed_cache_does_not_call_function_inside_its_window() -> None:
    from tslow.collectors.system import TimedCache

    now = [0.0]
    calls: list[int] = []
    cache = TimedCache(lambda: calls.append(1) or len(calls), ttl_s=60.0, clock=lambda: now[0])
    assert cache.get() == 1
    now[0] = 59.0
    assert cache.get() == 1 and len(calls) == 1
    now[0] = 60.0
    assert cache.get() == 2 and len(calls) == 2
