"""Test di monitor.py su dati sintetici: sampler adattivo, governor, mutex single-instance,
accumulo per gruppo-app di process_minutes."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from tslow import database as db
from tslow import monitor
from tslow.collectors import processes as processes_collector
from tslow.config import load_settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="mutex/win32 specifici per Windows")


def _proc_snap(
    pid: int,
    ppid: int,
    name: str,
    create_time: float,
    *,
    cpu_time: float = 0.0,
    private_mb: float = 0.0,
    io_read: int = 0,
    io_write: int = 0,
) -> processes_collector.ProcessSnapshot:
    return processes_collector.ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        name=name,
        create_time=create_time,
        thread_count=1,
        handle_count=10,
        working_set_bytes=0,
        private_bytes=int(private_mb * 1024 * 1024),
        cpu_time_seconds=cpu_time,
        io_read_bytes=io_read,
        io_write_bytes=io_write,
        io_other_bytes=0,
    )


def _metrics(cpu: float = 10.0, ram_avail_mb: float = 4000.0, ram_total_mb: float = 8000.0, ram_commit: float = 40.0, disk_latency: float = 5.0) -> monitor.RawMetrics:
    return monitor.RawMetrics(
        cpu_percent=cpu,
        ram_available_mb=ram_avail_mb,
        ram_total_mb=ram_total_mb,
        ram_commit_percent=ram_commit,
        disk_latency_ms=disk_latency,
    )


def test_sampler_stays_calmo_under_thresholds() -> None:
    sampler = monitor.AdaptiveSampler(load_settings())
    assert sampler.evaluate(_metrics()) == monitor.SamplerState.IDLE


def test_sampler_escalates_to_attenzione_on_cpu() -> None:
    sampler = monitor.AdaptiveSampler(load_settings())
    assert sampler.evaluate(_metrics(cpu=90.0)) == monitor.SamplerState.WATCHING


def test_sampler_escalates_to_indagine_on_critical_cpu() -> None:
    sampler = monitor.AdaptiveSampler(load_settings())
    assert sampler.evaluate(_metrics(cpu=97.0)) == monitor.SamplerState.INVESTIGATING


def test_sampler_escalates_on_ram_disponibile() -> None:
    sampler = monitor.AdaptiveSampler(load_settings())
    # 3% disponibile su 8000MB totali -> sotto la soglia critica (5%)
    state = sampler.evaluate(_metrics(ram_avail_mb=240.0, ram_total_mb=8000.0))
    assert state == monitor.SamplerState.INVESTIGATING


def test_sampler_does_not_drop_immediately_without_hysteresis_wait() -> None:
    settings = load_settings()
    sampler = monitor.AdaptiveSampler(settings)
    assert sampler.evaluate(_metrics(cpu=97.0)) == monitor.SamplerState.INVESTIGATING
    # subito dopo, anche se la metrica e' rientrata, l'isteresi (60s) non e' ancora passata
    assert sampler.evaluate(_metrics()) == monitor.SamplerState.INVESTIGATING


def test_sampler_drops_to_calmo_after_hysteresis(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    sampler = monitor.AdaptiveSampler(settings)
    sampler.evaluate(_metrics(cpu=97.0))
    assert sampler.state == monitor.SamplerState.INVESTIGATING

    # la metrica rientra: qui parte il conteggio dell'isteresi (60s di letture "idle" continue)
    assert sampler.evaluate(_metrics()) == monitor.SamplerState.INVESTIGATING

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 61)
    assert sampler.evaluate(_metrics()) == monitor.SamplerState.IDLE


def test_governor_increases_multiplier_over_budget() -> None:
    settings = load_settings()
    governor = monitor.OverheadGovernor(settings)
    governor._cpu_samples.append((time.monotonic(), 5.0))  # ben sopra il budget 0.25%
    governor._adjust(avg_cpu=5.0)
    assert governor.multiplier > 1.0


def test_governor_stays_at_one_under_budget() -> None:
    settings = load_settings()
    governor = monitor.OverheadGovernor(settings)
    governor._adjust(avg_cpu=0.01)
    assert governor.multiplier == 1.0


def test_governor_multiplier_capped_at_max() -> None:
    settings = load_settings()
    governor = monitor.OverheadGovernor(settings)
    for _ in range(50):
        governor._adjust(avg_cpu=100.0)
    max_mult = settings.get("governor", "max_factor", default=4.0)
    assert governor.multiplier == max_mult


def test_single_instance_lock_blocks_second_acquire() -> None:
    lock1 = monitor.SingleInstanceLock(name="TSlowTestMutex_pytest")
    lock2 = monitor.SingleInstanceLock(name="TSlowTestMutex_pytest")
    try:
        assert lock1.acquire() is True
        assert lock2.acquire() is False
    finally:
        lock1.release()

    lock3 = monitor.SingleInstanceLock(name="TSlowTestMutex_pytest")
    try:
        assert lock3.acquire() is True
    finally:
        lock3.release()


# --- ProcessGroupAccumulator (process_minutes) -------------------------------------


def test_process_group_accumulator_aggregates_avg_max_over_the_minute() -> None:
    acc = monitor.ProcessGroupAccumulator()

    baseline = {
        100: _proc_snap(100, 0, "alpha.exe", 1000.0, private_mb=100.0),
        101: _proc_snap(101, 100, "alpha.exe", 1001.0, private_mb=50.0),
    }
    tick1 = {
        100: _proc_snap(100, 0, "alpha.exe", 1000.0, cpu_time=0.4, private_mb=120.0, io_read=100_000, io_write=50_000),
        101: _proc_snap(101, 100, "alpha.exe", 1001.0, cpu_time=0.2, private_mb=60.0, io_read=50_000),
    }
    acc.record(tick1, baseline, dt_s=1.0, cores=4, gpu_per_pid={100: 3.0, 101: 1.0})

    # secondo tick: CPU/IO piu' alti ma RAM piu' bassa (deve restare il MASSIMO del minuto, non l'ultimo
    # valore), piu' un terzo processo dello stesso gruppo comparso solo ora (instance_count sale a 3).
    tick2 = {
        100: _proc_snap(100, 0, "alpha.exe", 1000.0, cpu_time=1.6, private_mb=90.0, io_read=140_000, io_write=50_000),
        101: _proc_snap(101, 100, "alpha.exe", 1001.0, cpu_time=0.6, private_mb=30.0, io_read=50_000, io_write=10_000),
        102: _proc_snap(102, 100, "alpha.exe", 1002.0, private_mb=10.0),
    }
    acc.record(tick2, tick1, dt_s=2.0, cores=4, gpu_per_pid={100: 0.5, 101: 0.5})

    rows = acc.flush(minute_ts=123_000)
    assert len(rows) == 1
    row = rows[0]
    assert row.minute_ts == 123_000
    assert row.group_key == "alpha"
    assert row.display_name == "alpha.exe"
    assert row.cpu_percent_avg == pytest.approx(17.5)  # (15.0 + 20.0) / 2
    assert row.cpu_percent_max == pytest.approx(20.0)
    assert row.ram_private_mb_max == pytest.approx(180.0)  # tick1 (120+60), non tick2 (90+30+10=130)
    assert row.io_bytes_sec == pytest.approx(112_500.0)  # (200_000 + 25_000) / 2
    assert row.gpu_percent == pytest.approx(2.5)  # (4.0 + 1.0) / 2
    assert row.instance_count == 3

    # flush azzera l'accumulo: il prossimo minuto senza nuovi tick non produce righe.
    assert acc.flush(minute_ts=456_000) == []


def test_process_group_accumulator_flush_keeps_only_top_n_by_cpu_avg() -> None:
    acc = monitor.ProcessGroupAccumulator()
    baseline: dict[int, processes_collector.ProcessSnapshot] = {}
    curr: dict[int, processes_collector.ProcessSnapshot] = {}
    for i in range(12):  # piu' gruppi del limite (10): ognuno e' un processo isolato, proprio gruppo
        pid = i + 1
        baseline[pid] = _proc_snap(pid, 0, f"app{i}.exe", 100.0 + i)
        curr[pid] = _proc_snap(pid, 0, f"app{i}.exe", 100.0 + i, cpu_time=float(i))
    acc.record(curr, baseline, dt_s=1.0, cores=1, gpu_per_pid={})

    rows = acc.flush(minute_ts=1)
    assert len(rows) == monitor._PROCESS_MINUTES_TOP_N
    # cpu_percent_avg cresce con i (delta cpu_time = i, cores=1): i piu' alti (11..2) sopravvivono, in ordine.
    assert [r.group_key for r in rows] == [f"app{i}" for i in range(11, 1, -1)]
    assert rows[0].cpu_percent_avg > rows[-1].cpu_percent_avg


def test_process_group_accumulator_ignores_non_positive_dt() -> None:
    acc = monitor.ProcessGroupAccumulator()
    snaps = {1: _proc_snap(1, 0, "solo.exe", 1.0, cpu_time=5.0)}
    acc.record(snaps, {}, dt_s=0.0, cores=4, gpu_per_pid={})
    assert acc.flush(minute_ts=1) == []


def test_flush_writes_top_process_minutes_rows(tmp_path: Path) -> None:
    """Copre l'aggancio reale: monitor._flush deve chiamare db.insert_process_minutes con le righe accumulate."""
    conn = db.connect(tmp_path / "flush_process_minutes.db")
    try:
        acc = monitor.ProcessGroupAccumulator()
        baseline = {1: _proc_snap(1, 0, "solo.exe", 1.0, private_mb=10.0)}
        curr = {1: _proc_snap(1, 0, "solo.exe", 1.0, cpu_time=1.0, private_mb=20.0, io_read=1000)}
        acc.record(curr, baseline, dt_s=1.0, cores=1, gpu_per_pid={1: 5.0})

        now_ms = int(time.time() * 1000)
        monitor._flush(conn, [], monitor.SamplerState.IDLE, 1.0, 0.0, 0.0, acc)

        rows = conn.execute(
            "SELECT group_key, display_name, cpu_percent_avg, ram_private_mb_max, gpu_percent, "
            "instance_count, minute_ts FROM process_minutes"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["group_key"] == "solo"
        assert row["display_name"] == "solo.exe"
        assert row["cpu_percent_avg"] == pytest.approx(100.0)
        assert row["ram_private_mb_max"] == pytest.approx(20.0)
        assert row["gpu_percent"] == pytest.approx(5.0)
        assert row["instance_count"] == 1
        assert row["minute_ts"] % db.MINUTE_MS == 0
        assert abs(row["minute_ts"] - (now_ms // db.MINUTE_MS) * db.MINUTE_MS) <= db.MINUTE_MS

        # un flush successivo senza nuovi tick non deve riscrivere righe (l'accumulo e' stato azzerato).
        monitor._flush(conn, [], monitor.SamplerState.IDLE, 1.0, 0.0, 0.0, acc)
        assert conn.execute("SELECT COUNT(*) FROM process_minutes").fetchone()[0] == 1
    finally:
        conn.close()


# --- wait_for_next_tick (M7.1) ----------------------------------------------------


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def clock(self) -> float:
        return self.now


def test_wait_for_next_tick_single_sleep_without_actionable_incident(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "wait1.db")
    try:
        db.create_incident(conn, opened_at_ms=1, resource="system", incident_type="throttling", severity="critical")
        ft = _FakeTime()
        monitor.wait_for_next_tick(conn, 5.0, lambda: pytest.fail("no decisions expected"), sleep=ft.sleep, clock=ft.clock)
        assert ft.sleeps == [5.0]
    finally:
        conn.close()


def test_wait_for_next_tick_slices_and_processes_decision(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "wait2.db")
    try:
        inc = db.create_incident(
            conn, opened_at_ms=1, resource="cpu", incident_type="anomaly", severity="critical", group_key="x", proposed_action="soft"
        )
        ft = _FakeTime()
        calls: list[float] = []

        def sleep(s: float) -> None:
            ft.sleep(s)
            if len(ft.sleeps) == 2:  # una decisione compare a meta' attesa
                db.create_decision(conn, inc, "allow_once", now_ms=1)

        monitor.wait_for_next_tick(conn, 3.5, lambda: calls.append(ft.now), sleep=sleep, clock=ft.clock)
        assert ft.sleeps == [1.0, 1.0, 1.0, 0.5]
        assert calls == [2.0, 3.0, 3.5]  # la decisione resta pending nel test: viene riletta a ogni fetta
    finally:
        conn.close()


# --- M7.4 ---------------------------------------------------------------------------


def _sys_snap(percent, plugged):
    from tslow.collectors import system

    return system.SystemSnapshot(1, 1, 1, 1, 0, 0, 0, 0, 1.0, percent, plugged)


def test_on_battery_uses_the_snapshot_not_psutil() -> None:
    assert monitor.on_battery(_sys_snap(80.0, False)) is True
    assert monitor.on_battery(_sys_snap(80.0, True)) is False
    assert monitor.on_battery(_sys_snap(None, None)) is False  # desktop senza batteria


def test_calmo_tick_uses_battery_period_when_on_battery() -> None:
    settings = load_settings()
    plugged = monitor.tick_globale_period_s(settings, monitor.SamplerState.IDLE, False)
    battery = monitor.tick_globale_period_s(settings, monitor.SamplerState.IDLE, True)
    assert battery > plugged


def test_accumulator_uses_precomputed_groups() -> None:
    acc = monitor.ProcessGroupAccumulator()
    snaps = {1: _proc_snap(1, 0, "solo.exe", 1.0, cpu_time=1.0)}
    acc.record(snaps, {1: _proc_snap(1, 0, "solo.exe", 1.0)}, dt_s=1.0, cores=1, gpu_per_pid={}, groups={"fake": [1]})
    assert [r.group_key for r in acc.flush(1)] == ["fake"]


# --- dry-run da settings.toml ----------------------------------------------------------


def test_resolve_dry_run_defaults_to_true_and_follows_setting() -> None:
    from tslow.config import Settings

    assert monitor.resolve_dry_run(None, Settings(raw={})) is True
    assert monitor.resolve_dry_run(None, Settings(raw={"watcher": {"dry_run": False}})) is False
    assert monitor.resolve_dry_run(None, Settings(raw={"watcher": {"dry_run": True}})) is True


def test_resolve_dry_run_cli_flag_wins() -> None:
    from tslow.config import Settings

    off = Settings(raw={"watcher": {"dry_run": False}})
    assert monitor.resolve_dry_run(True, off) is True
    assert monitor.resolve_dry_run(False, Settings(raw={})) is False


def test_resolve_dry_run_garbage_value_stays_dry() -> None:
    from tslow.config import Settings

    for bad in ("false", 0, None, "no"):
        assert monitor.resolve_dry_run(None, Settings(raw={"watcher": {"dry_run": bad}})) is True


def test_shipped_settings_are_dry_run() -> None:
    assert monitor.resolve_dry_run(None, load_settings()) is True


def test_legacy_italian_settings_keys_are_detected() -> None:
    from tslow.config import Settings, legacy_keys

    assert legacy_keys(load_settings()) == []
    old = Settings(raw={"notifiche": {"x": 1}, "thresholds": {"disco": {}}, "sampler": {"calmo_tick": 5}})
    assert legacy_keys(old) == ["notifiche", "thresholds.disco", "sampler.calmo_tick"]
