"""Demone: sampler adattivo, governor dell'overhead, buffer di scrittura, mutex single-instance.

Il rilevamento vero (baseline, isteresi, finestra sostenuta, attribuzione del colpevole) e' in
detector.py (M2). Qui la classificazione dello stato del sampler e' volutamente grezza (soglie
assolute di "anomaly"/"critical" lette da settings.toml): serve solo a decidere quanto campionare
piu' spesso, non a generare incidenti. Vedi Architettura.md per il diagramma degli stati.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

import psutil
import win32api
import win32event
import winerror

from tslow import database as db
from tslow import detector as detector_module
from tslow import grouping
from tslow import notifier
from tslow import optimizer
from tslow import protection as protection_module
from tslow import rules
from tslow import updater
from tslow.collectors import gpu as gpu_collector
from tslow.collectors import pdh as pdh_collector
from tslow.collectors import processes as processes_collector
from tslow.collectors import system as system_collector
from tslow.collectors import windows as windows_collector
from tslow.config import Settings, legacy_keys, load_settings
from tslow.paths import logs_dir

logger = logging.getLogger(__name__)

_MUTEX_NAME = "TSlowDaemonSingleInstance"


class SamplerState(str, Enum):
    IDLE = "idle"
    WATCHING = "watching"
    INVESTIGATING = "investigating"


# --- single-instance ----------------------------------------------------------


class SingleInstanceLock:
    """Mutex nominato (Local, sessione dell'utente): impedisce a due copie del demone di girare insieme."""

    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self._name = name
        self._handle = None

    def acquire(self) -> bool:
        self._handle = win32event.CreateMutex(None, False, self._name)
        already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
        if already_running:
            self.release()
        return not already_running

    def release(self) -> None:
        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None


def is_daemon_running() -> bool:
    handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    win32api.CloseHandle(handle)
    return exists


# --- sampler adattivo -----------------------------------------------------------


@dataclass
class RawMetrics:
    cpu_percent: float
    ram_available_mb: float
    ram_total_mb: float
    ram_commit_percent: float | None
    disk_latency_ms: float | None


class AdaptiveSampler:
    """Decide lo stato calmo/attenzione/indagine con soglie assolute; isteresi di discesa a 60s."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.state = SamplerState.IDLE
        self._below_since: float | None = time.monotonic()

    def evaluate(self, metrics: RawMetrics) -> SamplerState:
        level = self._raw_level(metrics)
        now = time.monotonic()
        if level != SamplerState.IDLE:
            self.state = level
            self._below_since = None
            return self.state

        if self._below_since is None:
            self._below_since = now
        isteresi = self._settings.get("sampler", "hysteresis_drop_seconds", default=60)
        if now - self._below_since >= isteresi:
            self.state = SamplerState.IDLE
        return self.state

    def _raw_level(self, m: RawMetrics) -> SamplerState:
        t = self._settings
        ram_avail_pct = (m.ram_available_mb / m.ram_total_mb * 100) if m.ram_total_mb else 100.0

        if (
            m.cpu_percent >= t.get("thresholds", "cpu", "critical_percent", default=95)
            or ram_avail_pct <= t.get("thresholds", "ram", "critical_available_percent_max", default=5)
            or (m.ram_commit_percent or 0) >= t.get("thresholds", "ram", "critical_commit_percent", default=95)
            or (m.disk_latency_ms or 0) >= t.get("thresholds", "disk", "critical_latency_ms", default=200)
        ):
            return SamplerState.INVESTIGATING

        if (
            m.cpu_percent >= t.get("thresholds", "cpu", "anomaly_percent", default=85)
            or ram_avail_pct <= t.get("thresholds", "ram", "anomaly_available_percent_max", default=10)
            or (m.ram_commit_percent or 0) >= t.get("thresholds", "ram", "anomaly_commit_percent_min", default=90)
            or (m.disk_latency_ms or 0) >= t.get("thresholds", "disk", "anomaly_latency_ms", default=50)
        ):
            return SamplerState.WATCHING

        return SamplerState.IDLE


def on_battery(snap: system_collector.SystemSnapshot) -> bool:
    """True se c'e' una batteria e non e' collegata all'alimentazione (letta una sola volta in collect)."""
    return snap.battery_percent is not None and not snap.battery_plugged


def tick_globale_period_s(settings: Settings, state: SamplerState, on_battery: bool = False) -> float:
    if state is SamplerState.IDLE:
        key = "idle_tick_battery" if on_battery else "idle_tick"
        return settings.get("sampler", key, default=5)
    if state is SamplerState.WATCHING:
        return settings.get("sampler", "watching_tick", default=2)
    return settings.get("sampler", "investigating_tick", default=1)


def gpu_period_s(settings: Settings, state: SamplerState) -> float:
    if state is SamplerState.IDLE:
        return settings.get("sampler", "idle_gpu", default=60)
    if state is SamplerState.WATCHING:
        return settings.get("sampler", "watching_gpu", default=10)
    return settings.get("sampler", "investigating_gpu", default=5)


# --- governor dell'overhead ---------------------------------------------------


class OverheadGovernor:
    """Misura CPU/RAM del demone (media mobile) e regola il moltiplicatore dei periodi."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._process = psutil.Process()
        self._process.cpu_percent(interval=None)  # priming: il primo valore reale e' inaffidabile
        self._cpu_samples: deque[tuple[float, float]] = deque()
        self.multiplier = 1.0

    def sample(self) -> tuple[float, float]:
        cores = psutil.cpu_count(logical=True) or 1
        cpu = self._process.cpu_percent(interval=None) / cores
        rss_mb = self._process.memory_info().rss / (1024 * 1024)
        now = time.monotonic()
        self._cpu_samples.append((now, cpu))
        window_s = self._settings.get("governor", "average_minutes", default=5) * 60
        while self._cpu_samples and now - self._cpu_samples[0][0] > window_s:
            self._cpu_samples.popleft()
        avg_cpu = sum(c for _, c in self._cpu_samples) / len(self._cpu_samples)
        self._adjust(avg_cpu)
        return avg_cpu, rss_mb

    def _adjust(self, avg_cpu: float) -> None:
        budget = self._settings.get("governor", "budget_cpu_percent", default=0.25)
        max_mult = self._settings.get("governor", "max_factor", default=4.0)
        low_frac = self._settings.get("governor", "reduction_threshold_fraction", default=0.33)
        if avg_cpu > budget:
            old = self.multiplier
            self.multiplier = min(max_mult, self.multiplier * 1.5)
            if self.multiplier != old:
                logger.warning(
                    "Overhead sopra budget (%.3f%% > %.3f%%): periodi x%.2f", avg_cpu, budget, self.multiplier
                )
        elif avg_cpu < budget * low_frac and self.multiplier > 1.0:
            self.multiplier = max(1.0, self.multiplier / 1.2)


# --- logging --------------------------------------------------------------------


def configure_logging(*, foreground: bool, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if foreground:
        logging.basicConfig(level=level, format=fmt)
        return
    handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "tslow.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


# --- rilevamento (M2) --------------------------------------------------------------


def _is_foreground_group(tick: detector_module.TickInput, group_key: str | None) -> bool:
    if group_key is None or tick.foreground_pid is None or tick.foreground_pid not in tick.snapshots:
        return False
    from tslow import grouping
    from tslow.protection import normalize_name

    root_pid = grouping.group_root_pid(tick.foreground_pid, tick.snapshots)
    return normalize_name(tick.snapshots[root_pid].name) == group_key


def _handle_new_incidents(
    conn, settings: Settings, events: list[detector_module.NewIncident], tick: detector_module.TickInput
) -> None:
    for event in events:
        incident = db.get_incident(conn, event.incident_id)

        if event.group_key is not None and incident.proposed_action == "soft":
            identity = rules.app_identity(incident.exe_path, event.group_key)
            if rules.auto_action_for(conn, identity) == "soft":
                db.create_decision(conn, incident.id, "allow_once", source="auto", now_ms=tick.now_ms)
                logger.info(
                    "Applicazione automatica (regola 'consenti sempre') per l'incidente #%s (%s)", incident.id, identity
                )

        is_fg = _is_foreground_group(tick, event.group_key)
        ok, reason = notifier.should_notify(conn, incident, settings, now_ms=tick.now_ms, is_foreground=is_fg)
        if not ok:
            logger.debug("Notifica soppressa per l'incidente #%s: %s", event.incident_id, reason)
            continue
        app_name = event.group_key or "system"
        sent = notifier.send(conn, incident, app_name=app_name, now_ms=tick.now_ms)
        logger.info(
            "Notifica %s per l'incidente #%s (%s, %s/%s)",
            "inviata" if sent else "FALLITA", event.incident_id, app_name, event.resource, event.severity,
        )


def _process_pending_decisions(conn, settings: Settings, snapshots: dict, dry_run: bool) -> None:
    """Valida ed esegue le decisioni in attesa (optimizer.py). Poll indicizzato, costo trascurabile."""
    pending = db.list_pending_decisions(conn)
    if not pending:
        return
    groups = grouping.build_groups(snapshots)
    for decision in pending:
        now_ms = int(time.time() * 1000)
        incident = db.get_incident(conn, decision.incident_id)
        if incident is None:
            db.update_decision_status(conn, decision.id, "no_such_process", now_ms)
            continue
        group_pids = groups.get(incident.group_key, []) if incident.group_key else []
        outcome = optimizer.apply_decision(conn, settings, decision, incident, snapshots, group_pids, dry_run=dry_run)
        if incident.status == "open":
            db.close_incident(conn, incident.id, now_ms, status="resolved")
        logger.info(
            "Decisione #%s (%s, %s) sull'incidente #%s eseguita: %s", decision.id, decision.choice, decision.source, incident.id, outcome
        )


# --- attesa tra un tick e il successivo ---------------------------------------------

_DECISION_POLL_SLICE_S = 1.0


def wait_for_next_tick(
    conn,
    duration_s: float,
    on_pending_decisions,
    *,
    sleep=time.sleep,
    clock=time.monotonic,
) -> None:
    """Dorme `duration_s`. Con un incidente "azionabile" aperto sveglia ogni <=1s per leggere le decisioni.

    Il periodo di campionamento NON dipende dagli incidenti: qui si legge soltanto la tabella
    `decisions` (query indicizzata); solo se c'e' qualcosa in attesa si chiama `on_pending_decisions`.
    """
    if duration_s <= 0:
        return
    if not db.has_actionable_open_incident(conn):
        sleep(duration_s)
        return
    deadline = clock() + duration_s
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return
        sleep(min(_DECISION_POLL_SLICE_S, remaining))
        if db.list_pending_decisions(conn):
            on_pending_decisions()


# --- process_minutes (aggregazione per gruppo-app) --------------------------------


_PROCESS_MINUTES_TOP_N = 10


class ProcessGroupAccumulator:
    """Accumula per gruppo-app le metriche di ogni tick, per produrre le righe di `process_minutes` al flush.

    Nello schema esiste solo `process_minutes` (nessuna tabella "raw" per processo): l'accumulo vive
    quindi solo in memoria tra un flush e l'altro, azzerato ad ogni `flush()`. Gruppi e formule per-gruppo
    sono gli stessi gia' usati da detector.py (create_time per scartare i PID riusati, CPU come quota
    della capacita' TOTALE del sistema) applicati agli stessi snapshot (gia' filtrati del processo del
    demone stesso) che il detector ha appena usato per costruire i propri gruppi.
    """

    def __init__(self) -> None:
        self._cpu: dict[str, list[float]] = {}
        self._io: dict[str, list[float]] = {}
        self._gpu: dict[str, list[float]] = {}
        self._ram_max: dict[str, float] = {}
        self._instances_max: dict[str, int] = {}
        self._display_name: dict[str, str] = {}

    def record(
        self,
        snapshots: dict[int, processes_collector.ProcessSnapshot],
        prev_snapshots: dict[int, processes_collector.ProcessSnapshot],
        dt_s: float,
        cores: int,
        gpu_per_pid: dict[int, float],
        groups: dict[str, list[int]] | None = None,
    ) -> None:
        if dt_s <= 0:
            return
        if groups is None:
            groups = grouping.build_groups(snapshots)
        for group_key, pids in groups.items():
            self._cpu.setdefault(group_key, []).append(
                detector_module.group_cpu_percent(pids, prev_snapshots, snapshots, dt_s, cores)
            )
            self._io.setdefault(group_key, []).append(
                detector_module.group_io_bytes_sec(pids, prev_snapshots, snapshots, dt_s)
            )
            self._gpu.setdefault(group_key, []).append(detector_module.group_gpu_percent(pids, gpu_per_pid))
            ram_mb = detector_module.group_ram_private_mb(pids, snapshots)
            self._ram_max[group_key] = max(self._ram_max.get(group_key, 0.0), ram_mb)
            self._instances_max[group_key] = max(self._instances_max.get(group_key, 0), len(pids))
            self._display_name[group_key] = snapshots[pids[0]].name

    def flush(self, minute_ts: int, top_n: int = _PROCESS_MINUTES_TOP_N) -> list[db.ProcessMinuteSample]:
        """Righe dei `top_n` gruppi per CPU media nel periodo accumulato; azzera l'accumulo."""
        rows = [
            db.ProcessMinuteSample(
                minute_ts=minute_ts,
                group_key=group_key,
                display_name=self._display_name[group_key],
                cpu_percent_avg=sum(cpu_samples) / len(cpu_samples),
                cpu_percent_max=max(cpu_samples),
                ram_private_mb_max=self._ram_max[group_key],
                io_bytes_sec=sum(self._io[group_key]) / len(self._io[group_key]),
                gpu_percent=sum(self._gpu[group_key]) / len(self._gpu[group_key]),
                instance_count=self._instances_max[group_key],
            )
            for group_key, cpu_samples in self._cpu.items()
        ]
        rows.sort(key=lambda r: r.cpu_percent_avg, reverse=True)
        self.reset()
        return rows[:top_n]

    def reset(self) -> None:
        self._cpu.clear()
        self._io.clear()
        self._gpu.clear()
        self._ram_max.clear()
        self._instances_max.clear()
        self._display_name.clear()


# --- flush ------------------------------------------------------------------------


def _flush(
    conn,
    buffer: list[db.MetricSample],
    state: SamplerState,
    multiplier: float,
    daemon_cpu_percent: float,
    daemon_rss_mb: float,
    process_groups: ProcessGroupAccumulator,
    incident_ttl_ms: int | None = None,
) -> None:
    if buffer:
        db.insert_metrics_raw(conn, buffer)
    now_ms = int(time.time() * 1000)
    db.rollup_1m(conn, now_ms)
    db.rollup_1h(conn, now_ms)
    db.record_monitor_health(conn, now_ms, daemon_cpu_percent, daemon_rss_mb, state.value, multiplier)
    minute_ts = (now_ms // db.MINUTE_MS) * db.MINUTE_MS
    db.insert_process_minutes(conn, process_groups.flush(minute_ts))
    if incident_ttl_ms is not None:
        expired = db.expire_stale_incidents(conn, now_ms, incident_ttl_ms)
        if expired:
            logger.info("%d incidenti scaduti (TTL %d min)", expired, incident_ttl_ms // 60_000)


# --- loop principale --------------------------------------------------------------


def _ensure_system_inventory(conn) -> None:
    """Registra l'inventario hardware una tantum (WMI, fuori dal loop di campionamento)."""
    count = conn.execute("SELECT COUNT(*) FROM system_inventory").fetchone()[0]
    if count > 0:
        return
    try:
        from tslow.collectors import wmi_info

        inventory = wmi_info.collect_inventory()
        db.insert_system_inventory(conn, collected_at_ms=int(time.time() * 1000), **inventory)
        logger.info("Inventario di sistema registrato: %s", inventory.get("cpu_name"))
    except Exception:
        logger.warning("Impossibile registrare l'inventario di sistema", exc_info=True)


def resolve_dry_run(cli_value: bool | None, settings: Settings) -> bool:
    """Dry-run effettivo: il flag esplicito della CLI vince; altrimenti `[watcher] dry_run` di settings.toml.

    Installato, settings.toml e' scrivibile solo da un amministratore: e' li' che si abilitano le azioni
    reali. Qualunque valore diverso da `false` (assente, stringa, numero) resta dry-run.
    """
    if cli_value is not None:
        return cli_value
    return settings.get("watcher", "dry_run", default=True) is not False


def init_protection(dry_run: bool) -> bool:
    """Carica la protezione utente all'avvio. Se il file e' rotto: ERROR, protezione vuota e dry_run forzato.

    Ritorna il dry_run effettivo. Una configurazione di protezione illeggibile non deve mai
    portare a un'azione reale.
    """
    try:
        protection_module.get_user_protection()
    except protection_module.ProtectionConfigError as exc:
        logger.error("Configurazione [protection] non valida (%s): dry_run forzato per questa esecuzione", exc)
        protection_module.set_user_protection(protection_module.UserProtection())
        return True
    return dry_run


def _update_check_worker(settings: Settings) -> None:
    """Un controllo release in un thread a parte (rete lenta = nessun rallentamento del campionamento)."""
    conn = None
    try:
        conn = db.connect()
        release = updater.check_for_update(conn, settings, int(time.time() * 1000))
        if release is not None:
            logger.info("Aggiornamento disponibile: %s", release.tag)
            notifier.send_text(
                f"why ts so slow? — {release.tag} is available",
                "Run `tslow update` from an administrator terminal to install it.",
            )
    except Exception:
        logger.debug("update check worker failed", exc_info=True)
    finally:
        if conn is not None:
            conn.close()


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt()


def run_daemon(*, foreground: bool = True, dry_run: bool | None = None, verbose: bool = False) -> None:
    configure_logging(foreground=foreground, verbose=verbose)

    # Un arresto "gentile" (SIGTERM, es. dall'Utilita' di pianificazione) deve fare lo stesso flush
    # finale di Ctrl+C, altrimenti si perde il buffer in memoria non ancora scritto (fino a 60s di
    # metrics_raw). Un TerminateProcess forzato (kill -9 equivalente) resta intercettabile da nessun
    # processo per progettazione del sistema operativo: quel caso limite resta un rischio residuo.
    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        logger.debug("Impossibile installare l'handler SIGTERM (piattaforma non supportata?)", exc_info=True)

    lock = SingleInstanceLock()
    if not lock.acquire():
        logger.error("Un'altra istanza del demone TSlow e' gia' in esecuzione. Uscita.")
        raise SystemExit(1)

    settings = load_settings()
    dry_run = init_protection(resolve_dry_run(dry_run, settings))
    old_keys = legacy_keys(settings)
    if old_keys:
        logger.warning(
            "settings.toml still has pre-English keys (%s): they are ignored and defaults apply. "
            "Compare it with settings.default.toml.", ", ".join(old_keys),
        )
    if not dry_run:
        logger.warning(
            "dry_run=False: il demone eseguira' azioni REALI (kill/priorita') sui processi che risultano colpevoli."
        )
    conn = db.connect()
    _ensure_system_inventory(conn)
    sampler = AdaptiveSampler(settings)
    governor = OverheadGovernor(settings)
    cores = psutil.cpu_count(logical=True) or 1
    det = detector_module.Detector(settings, cores=cores)

    disk_read_tracker = system_collector.RateTracker()
    disk_write_tracker = system_collector.RateTracker()
    net_rx_tracker = system_collector.RateTracker()
    net_tx_tracker = system_collector.RateTracker()

    incident_ttl_ms = int(settings.get("incidents", "expire_after_minutes", default=30) * 60_000)
    flush_interval_s = 60.0
    retention_interval_s = 6 * 3600.0
    buffer: list[db.MetricSample] = []
    process_groups = ProcessGroupAccumulator()
    last_flush = time.monotonic()
    last_retention = time.monotonic()
    next_gpu_due = 0.0
    next_update_poll = time.monotonic() + 120.0  # non nel burst dell'avvio; l'intervallo vero e' in check_for_update
    last_gpu_percent: float | None = None
    last_gpu_raw: dict[str, float] = {}
    prev_process_snapshots: dict[int, processes_collector.ProcessSnapshot] = {}
    prev_process_ts: float | None = None

    with pdh_collector.PdhCollector() as pdh, gpu_collector.GpuCollector() as gpu:
        if pdh.unavailable_counters():
            logger.warning("Contatori PDH non disponibili: %s", pdh.unavailable_counters())
        logger.info("TSlow daemon avviato (dry_run=%s, gpu_disponibile=%s)", dry_run, gpu.available())

        # priming dei contatori stateful (psutil.cpu_percent, il primo campione PDH rate-based)
        system_collector.sample_cpu_percent()
        pdh.sample()

        try:
            while True:
                loop_start = time.monotonic()

                snap = system_collector.collect()
                pdh_snap = pdh.sample()

                if loop_start >= next_gpu_due:
                    last_gpu_raw = gpu.sample_raw()
                    last_gpu_percent = gpu_collector.max_engine_percent(last_gpu_raw)
                    period = gpu_period_s(settings, sampler.state) * governor.multiplier
                    next_gpu_due = loop_start + period

                metrics = RawMetrics(
                    cpu_percent=snap.cpu_percent,
                    ram_available_mb=snap.ram_available_mb,
                    ram_total_mb=snap.ram_total_mb,
                    ram_commit_percent=pdh_snap.ram_commit_percent,
                    disk_latency_ms=pdh_snap.disk_latency_ms,
                )
                state = sampler.evaluate(metrics)
                tick_period = tick_globale_period_s(settings, state, on_battery(snap)) * governor.multiplier

                disk_read_rate = disk_read_tracker.update(snap.disk_read_bytes)
                disk_write_rate = disk_write_tracker.update(snap.disk_write_bytes)
                net_rx_rate = net_rx_tracker.update(snap.net_rx_bytes)
                net_tx_rate = net_tx_tracker.update(snap.net_tx_bytes)
                now_ms = int(time.time() * 1000)

                sample = db.MetricSample(
                    ts_ms=now_ms,
                    sampler_state=state.value,
                    period_ms=int(tick_period * 1000),
                    cpu_percent=snap.cpu_percent,
                    cpu_queue_per_core=pdh_snap.cpu_queue_per_core,
                    cpu_perf_percent=pdh_snap.cpu_perf_percent,
                    cpu_perf_limit_percent=pdh_snap.cpu_perf_limit_percent,
                    ram_percent=snap.ram_percent,
                    ram_available_mb=snap.ram_available_mb,
                    ram_commit_percent=pdh_snap.ram_commit_percent,
                    page_reads_sec=pdh_snap.page_reads_sec,
                    disk_read_bytes_sec=disk_read_rate,
                    disk_write_bytes_sec=disk_write_rate,
                    disk_latency_ms=pdh_snap.disk_latency_ms,
                    disk_queue_length=pdh_snap.disk_queue_length,
                    disk_free_gb=snap.disk_free_gb,
                    net_rx_bytes_sec=net_rx_rate,
                    net_tx_bytes_sec=net_tx_rate,
                    gpu_percent=last_gpu_percent,
                    battery_percent=snap.battery_percent,
                )
                buffer.append(sample)

                curr_process_snapshots, _snapshot_method = processes_collector.snapshot()
                dt_s = (loop_start - prev_process_ts) if prev_process_ts is not None else 0.0
                tick = detector_module.TickInput(
                    now_ms=now_ms,
                    cpu_percent=snap.cpu_percent,
                    cpu_perf_percent=pdh_snap.cpu_perf_percent,
                    cpu_perf_limit_percent=pdh_snap.cpu_perf_limit_percent,
                    ram_available_mb=snap.ram_available_mb,
                    ram_total_mb=snap.ram_total_mb,
                    ram_commit_percent=pdh_snap.ram_commit_percent,
                    page_reads_sec=pdh_snap.page_reads_sec,
                    disk_latency_ms=pdh_snap.disk_latency_ms,
                    disk_queue_length=pdh_snap.disk_queue_length,
                    disk_iops=pdh_snap.disk_iops,
                    disk_free_gb=snap.disk_free_gb,
                    net_rx_bytes_sec=net_rx_rate,
                    net_tx_bytes_sec=net_tx_rate,
                    gpu_percent=last_gpu_percent,
                    link_speed_mbps=system_collector.net_link_speed_mbps(),
                    on_battery=on_battery(snap),
                    snapshots=curr_process_snapshots,
                    prev_snapshots=prev_process_snapshots,
                    dt_s=dt_s,
                    gpu_raw=last_gpu_raw,
                    foreground_pid=windows_collector.foreground_pid(),
                )
                if dt_s > 0:
                    events = det.evaluate(conn, tick)
                    if events:
                        _handle_new_incidents(conn, settings, events, tick)
                    gpu_per_pid = gpu_collector.aggregate_per_pid(tick.gpu_raw)
                    process_groups.record(tick.snapshots, tick.prev_snapshots, tick.dt_s, cores, gpu_per_pid, tick.groups)

                _process_pending_decisions(conn, settings, curr_process_snapshots, dry_run)
                prev_process_snapshots = curr_process_snapshots
                prev_process_ts = loop_start

                now = time.monotonic()
                if now - last_flush >= flush_interval_s:
                    avg_cpu, rss_mb = governor.sample()
                    _flush(conn, buffer, state, governor.multiplier, avg_cpu, rss_mb, process_groups, incident_ttl_ms)
                    buffer = []
                    last_flush = now
                    logger.debug(
                        "flush: stato=%s x%.2f cpu_demone=%.3f%% rss=%.1fMB", state.value, governor.multiplier, avg_cpu, rss_mb
                    )

                if now >= next_update_poll:
                    next_update_poll = now + 3600.0
                    if updater.configured_repo(settings) is not None:
                        threading.Thread(target=_update_check_worker, args=(settings,), daemon=True).start()

                if now - last_retention >= retention_interval_s:
                    db.apply_retention(conn, int(time.time() * 1000))
                    last_retention = now
                    logger.debug("retention applicata")

                elapsed = time.monotonic() - loop_start

                def _on_pending(snap_fn=processes_collector.snapshot) -> None:
                    # snapshot fresco usato solo per questa decisione: prev_process_snapshots/ts
                    # restano intatti perche' i delta del detector dipendono da essi.
                    fresh, _method = snap_fn()
                    _process_pending_decisions(conn, settings, fresh, dry_run)

                wait_for_next_tick(conn, tick_period - elapsed, _on_pending)
        except KeyboardInterrupt:
            logger.info("Interruzione richiesta, chiusura in corso...")
        finally:
            avg_cpu, rss_mb = governor.sample()
            _flush(conn, buffer, sampler.state, governor.multiplier, avg_cpu, rss_mb, process_groups, incident_ttl_ms)
            conn.close()
            lock.release()
            logger.info("TSlow daemon terminato.")
