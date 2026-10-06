"""detector.py su serie sintetiche: picchi ignorati, finestra sostenuta, leak con R², colpevole L0.

PID sintetici molto alti (900000+) per non collidere per caso con processi reali della macchina
durante il controllo "non risponde" (che fa una vera EnumWindows sulla sessione desktop).
"""

from __future__ import annotations

import pytest

from tslow import database as db
from tslow import detector
from tslow.collectors.processes import ProcessSnapshot
from tslow.config import load_settings


def _snap(pid: int, ppid: int, name: str, create_time: float, cpu_time: float = 0.0, private_bytes: int = 0) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        name=name,
        create_time=create_time,
        thread_count=1,
        handle_count=10,
        working_set_bytes=private_bytes,
        private_bytes=private_bytes,
        cpu_time_seconds=cpu_time,
        io_read_bytes=0,
        io_write_bytes=0,
        io_other_bytes=0,
    )


def _tick(now_ms: int, cpu_percent: float = 5.0, snapshots=None, prev_snapshots=None, dt_s: float = 1.0, **overrides) -> detector.TickInput:
    defaults = dict(
        now_ms=now_ms,
        cpu_percent=cpu_percent,
        cpu_perf_percent=100.0,
        cpu_perf_limit_percent=100.0,
        ram_available_mb=4000.0,
        ram_total_mb=8000.0,
        ram_commit_percent=40.0,
        page_reads_sec=0.0,
        disk_latency_ms=1.0,
        disk_queue_length=0.0,
        disk_iops=0.0,
        disk_free_gb=50.0,
        net_rx_bytes_sec=0.0,
        net_tx_bytes_sec=0.0,
        gpu_percent=0.0,
        link_speed_mbps=1000,
        on_battery=False,
        snapshots=snapshots or {},
        prev_snapshots=prev_snapshots or {},
        dt_s=dt_s,
        gpu_raw={},
        foreground_pid=None,
    )
    defaults.update(overrides)
    return detector.TickInput(**defaults)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_single_spike_is_ignored(conn) -> None:
    det = detector.Detector(load_settings(), cores=8)
    snap = {900100: _snap(900100, 0, "hog.exe", 0.0)}
    events = det.evaluate(conn, _tick(1000, cpu_percent=99.0, snapshots=snap))
    assert events == []


def test_sustained_cpu_creates_incident_with_culprit(conn) -> None:
    det = detector.Detector(load_settings(), cores=8)
    now_ms = 1000
    prev_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=0.0)}
    events: list = []
    for i in range(25):
        curr_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=(i + 1) * 5.0)}
        tick = _tick(now_ms, cpu_percent=97.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        events = det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
        if events:
            break
    assert events, "doveva aprirsi un incidente CPU dopo la finestra sostenuta"
    e = events[0]
    assert e.resource == "cpu"
    assert e.group_key == "hog"
    assert e.severity == "critical"


def test_duplicate_incident_not_reopened(conn) -> None:
    det = detector.Detector(load_settings(), cores=8)
    now_ms = 1000
    prev_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=0.0)}
    all_events: list = []
    for i in range(30):
        curr_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=(i + 1) * 5.0)}
        tick = _tick(now_ms, cpu_percent=97.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        all_events += det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
    cpu_events = [e for e in all_events if e.resource == "cpu"]
    assert len(cpu_events) == 1


def test_l0_culprit_gets_no_action_proposed(conn) -> None:
    det = detector.Detector(load_settings(), cores=8)
    now_ms = 1000
    prev_snap = {900200: _snap(900200, 0, "chrome.exe", 0.0, cpu_time=0.0)}
    events: list = []
    for i in range(25):
        curr_snap = {900200: _snap(900200, 0, "chrome.exe", 0.0, cpu_time=(i + 1) * 5.0)}
        tick = _tick(now_ms, cpu_percent=97.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        events = det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
        if events:
            break
    assert events
    e = events[0]
    assert e.protection_level == 0  # ProtectionLevel.L0_UNTOUCHABLE
    incident = db.get_incident(conn, e.incident_id)
    assert incident.proposed_action == "none"


def test_leak_detected_with_sustained_ram_growth(conn) -> None:
    """Tick a intervalli NON allineati ai minuti (come nella realta', dove il sampler adattivo non
    campiona mai su bordi puliti): regressione per un bug trovato dal vivo dove la finestra dello
    _LeakTracker coincideva esattamente con la durata minima richiesta, rendendo duration_s >= soglia
    quasi impossibile da soddisfare con timestamp non perfettamente allineati (vedi Detector.__init__)."""
    det = detector.Detector(load_settings(), cores=8)
    now_ms = 0
    events: list = []
    tick_s = 23.0  # non divide 60 in modo esatto, cosi' i bordi della finestra non sono mai "puliti"
    mb_per_tick = 40.0 * tick_s / 60.0
    private_mb = 700.0
    for _ in range(40):  # 40 * 23s ≈ 15 minuti
        private_mb += mb_per_tick
        snap = {900300: _snap(900300, 0, "leaky.exe", 0.0, private_bytes=int(private_mb * 1024 * 1024))}
        tick = _tick(now_ms, cpu_percent=5.0, snapshots=snap, prev_snapshots=snap, dt_s=tick_s)
        events = det.evaluate(conn, tick)
        now_ms += int(tick_s * 1000)
        if any(e.incident_type == "leak" for e in events):
            break
    leak_events = [e for e in events if e.incident_type == "leak"]
    assert leak_events, "doveva rilevare il leak dopo 10+ minuti di crescita costante"
    assert leak_events[0].group_key == "leaky"


def test_calibration_free_baseline_does_not_block_absolute_threshold(conn) -> None:
    """Prima che la baseline abbia abbastanza campioni, la sola soglia assoluta deve bastare."""
    det = detector.Detector(load_settings(), cores=8)
    now_ms = 1000
    prev_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=0.0)}
    events: list = []
    for i in range(25):
        curr_snap = {900100: _snap(900100, 0, "hog.exe", 0.0, cpu_time=(i + 1) * 4.0)}
        tick = _tick(now_ms, cpu_percent=88.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)  # solo anomalia, non critico
        events = det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
        if events:
            break
    assert events
    assert events[0].severity == "anomaly"


def _patch_hung_window(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    """Sostituisce l'enumerazione reale delle finestre con una finestra sintetica sempre "hung" per `pid`."""
    monkeypatch.setattr(detector.windows_collector, "enumerate_main_windows", lambda: {pid: [12345]})
    monkeypatch.setattr(detector.windows_collector, "is_hung", lambda hwnd: True)


def test_hung_incident_created_for_normal_process(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    det = detector.Detector(load_settings(), cores=8)
    pid = 900400
    _patch_hung_window(monkeypatch, pid)
    now_ms = 0
    prev_snap = {pid: _snap(pid, 0, "stuck.exe", 0.0, cpu_time=0.0, private_bytes=100_000_000)}
    events: list = []
    for i in range(35):  # 35 tick da 1s: supera la soglia di 30s
        curr_snap = {pid: _snap(pid, 0, "stuck.exe", 0.0, cpu_time=(i + 1) * 0.5, private_bytes=100_000_000 + i * 10_000_000)}
        tick = _tick(now_ms, cpu_percent=5.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        events = det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
        if any(e.incident_type == "hung" for e in events):
            break
    hung_events = [e for e in events if e.incident_type == "hung"]
    assert hung_events, "doveva rilevare 'non risponde' dopo 30+ secondi con RAM in crescita"
    assert hung_events[0].group_key == "stuck"


def test_hung_cpu_threshold_is_per_core_not_per_system(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regressione: un busy-loop a un thread satura UN core (100%) ma su 8 core logici cio'
    equivale al 12.5% del sistema. La soglia hung ("25% di un core", come Gestione attivita' per
    processo) non va divisa per il numero di core, altrimenti un processo davvero impazzito su un
    solo thread non la raggiunge mai. RAM e I/O restano piatti qui: solo la CPU deve far scattare
    l'incidente."""
    det = detector.Detector(load_settings(), cores=8)
    pid = 900600
    _patch_hung_window(monkeypatch, pid)
    now_ms = 0
    prev_snap = {pid: _snap(pid, 0, "busyloop.exe", 0.0, cpu_time=0.0, private_bytes=50_000_000)}
    events: list = []
    for i in range(35):
        # 0.5s di CPU per 1s di tempo reale = 50% di UN core, sopra la soglia 25%, ma solo 6.25% se
        # (erroneamente) diviso per 8 core.
        curr_snap = {pid: _snap(pid, 0, "busyloop.exe", 0.0, cpu_time=(i + 1) * 0.5, private_bytes=50_000_000)}
        tick = _tick(now_ms, cpu_percent=15.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        events = det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
        if any(e.incident_type == "hung" for e in events):
            break
    hung_events = [e for e in events if e.incident_type == "hung"]
    assert hung_events, "un busy-loop al 50% di un core doveva far scattare 'non risponde'"


def test_hung_not_reported_for_l0_process(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regressione: durante il test dal vivo M2, una finestra transitoria di dwm.exe (L0) e' stata
    segnalata come "non risponde" da IsHungAppWindow. I processi L0 non sono mai un bersaglio."""
    det = detector.Detector(load_settings(), cores=8)
    pid = 900500
    _patch_hung_window(monkeypatch, pid)
    now_ms = 0
    prev_snap = {pid: _snap(pid, 0, "dwm.exe", 0.0, cpu_time=0.0, private_bytes=100_000_000)}
    events: list = []
    for i in range(35):
        curr_snap = {pid: _snap(pid, 0, "dwm.exe", 0.0, cpu_time=(i + 1) * 0.5, private_bytes=100_000_000 + i * 10_000_000)}
        tick = _tick(now_ms, cpu_percent=5.0, snapshots=curr_snap, prev_snapshots=prev_snap, dt_s=1.0)
        events += det.evaluate(conn, tick)
        prev_snap = curr_snap
        now_ms += 1000
    assert not any(e.incident_type == "hung" for e in events)
