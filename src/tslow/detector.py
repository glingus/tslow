"""Rilevamento: baseline EWMA, finestra sostenuta, attribuzione del colpevole, leak, hung,
throttling. Crea SOLO righe in `incidents`: nessuna azione sui processi (quella e' optimizer.py,
M3). Il demone resta in --dry-run per tutto M2.

Le soglie "Anomalia"/"Critico" e le finestre vengono da settings.toml (tabella concordata nel
piano). Una condizione deve restare sopra soglia per almeno l'80% dei campioni della finestra
("picco ignorato" altrimenti). A livello Anomalia serve anche la conferma della baseline (z >= 3
rispetto a media/varianza EWMA per ora del giorno), MA solo dopo che la baseline ha abbastanza
campioni (MIN_BASELINE_SAMPLES): prima di allora si usa solo la soglia assoluta, coerente con la
calibrazione di 24h.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from tslow import database as db
from tslow import grouping, protection
from tslow.collectors import gpu as gpu_collector
from tslow.collectors import windows as windows_collector
from tslow.collectors.processes import ProcessSnapshot
from tslow.config import Settings

logger = logging.getLogger(__name__)

BASELINE_ALPHA = 0.1
MIN_BASELINE_SAMPLES = 30
Z_SCORE_THRESHOLD = 3.0
SUSTAINED_FRACTION = 0.8


@dataclass
class NewIncident:
    incident_id: int
    resource: str
    incident_type: str
    severity: str
    group_key: str | None
    protection_level: int | None
    quota_percent: float | None
    detail: dict


@dataclass
class TickInput:
    now_ms: int
    cpu_percent: float
    cpu_perf_percent: float | None
    cpu_perf_limit_percent: float | None
    ram_available_mb: float
    ram_total_mb: float
    ram_commit_percent: float | None
    page_reads_sec: float | None
    disk_latency_ms: float | None
    disk_queue_length: float | None
    disk_iops: float | None
    disk_free_gb: float | None
    net_rx_bytes_sec: float | None
    net_tx_bytes_sec: float | None
    gpu_percent: float | None
    link_speed_mbps: int | None
    on_battery: bool
    snapshots: dict[int, ProcessSnapshot]
    prev_snapshots: dict[int, ProcessSnapshot]
    dt_s: float
    gpu_raw: dict[str, float]
    foreground_pid: int | None
    groups: dict[str, list[int]] | None = None  # riempito da Detector.evaluate (build_groups una volta per tick)


class _SustainedWindow:
    """Frazione di campioni "sopra soglia" negli ultimi `window_s` secondi."""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, bool]] = deque()

    def record(self, ts_s: float, over: bool, window_s: float) -> float:
        self._samples.append((ts_s, over))
        while self._samples and ts_s - self._samples[0][0] > window_s:
            self._samples.popleft()
        if not self._samples:
            return 0.0
        return sum(1 for _, flag in self._samples if flag) / len(self._samples)


class _LeakTracker:
    """RAM privata di un gruppo negli ultimi `window_s` secondi, per la regressione lineare."""

    def __init__(self, window_s: float = 600.0) -> None:
        self._window_s = window_s
        self._series: dict[str, deque[tuple[float, float]]] = {}

    def record(self, group_key: str, ts_s: float, private_mb: float) -> None:
        series = self._series.setdefault(group_key, deque())
        series.append((ts_s, private_mb))
        while series and ts_s - series[0][0] > self._window_s:
            series.popleft()
        stale = [k for k, s in self._series.items() if s and ts_s - s[-1][0] > self._window_s]
        for k in stale:
            del self._series[k]

    def trend(self, group_key: str) -> tuple[float, float, float] | None:
        """(crescita_mb_al_minuto, r2, durata_secondi) o None se non ci sono abbastanza dati."""
        series = self._series.get(group_key)
        if series is None or len(series) < 10:
            return None
        t0 = series[0][0]
        xs = [(t - t0) / 60.0 for t, _ in series]
        ys = [v for _, v in series]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        ss_xx = sum((x - mean_x) ** 2 for x in xs)
        ss_yy = sum((y - mean_y) ** 2 for y in ys)
        if ss_xx == 0 or ss_yy == 0:
            return None
        slope = ss_xy / ss_xx
        r2 = (ss_xy**2) / (ss_xx * ss_yy)
        duration_s = series[-1][0] - series[0][0]
        return slope, r2, duration_s


def group_cpu_percent(
    pids: list[int], prev: dict[int, ProcessSnapshot], curr: dict[int, ProcessSnapshot], dt_s: float, cores: int
) -> float:
    """% della capacita' TOTALE del sistema (0-100) usata dal gruppo nell'intervallo."""
    if dt_s <= 0:
        return 0.0
    delta_cpu_s = 0.0
    for pid in pids:
        c = curr.get(pid)
        p = prev.get(pid)
        if c is None:
            continue
        if p is not None and p.create_time == c.create_time:
            delta = c.cpu_time_seconds - p.cpu_time_seconds
            if delta > 0:
                delta_cpu_s += delta
    return (delta_cpu_s / dt_s) / cores * 100.0


def group_ram_private_mb(pids: list[int], curr: dict[int, ProcessSnapshot]) -> float:
    return sum(curr[pid].private_bytes for pid in pids if pid in curr) / (1024 * 1024)


def group_io_bytes_sec(pids: list[int], prev: dict[int, ProcessSnapshot], curr: dict[int, ProcessSnapshot], dt_s: float) -> float:
    if dt_s <= 0:
        return 0.0
    delta = 0.0
    for pid in pids:
        c = curr.get(pid)
        p = prev.get(pid)
        if c is None:
            continue
        if p is not None and p.create_time == c.create_time:
            d = (c.io_read_bytes + c.io_write_bytes) - (p.io_read_bytes + p.io_write_bytes)
            if d > 0:
                delta += d
    return delta / dt_s


def group_gpu_percent(pids: list[int], gpu_per_pid: dict[int, float]) -> float:
    return sum(gpu_per_pid.get(pid, 0.0) for pid in pids)


class Detector:
    def __init__(self, settings: Settings, cores: int) -> None:
        self._settings = settings
        self._cores = cores or 1
        self._windows: dict[str, _SustainedWindow] = {}
        # La finestra dello _LeakTracker deve essere PIU' LARGA della durata minima richiesta
        # (thresholds.leak.durata_min): altrimenti, con i due valori uguali, il campione piu'
        # vecchio del deque viene scartato proprio mentre si avvicina alla soglia e duration_s
        # non raggiunge mai il minimo (trovato dal vivo con spawn_hog.py leak: 880MB allocati in
        # 11 minuti, mai rilevato). Il margine 1.5x lascia spazio perche' la finestra osservata
        # possa davvero coprire l'intera durata minima.
        leak_duration_min = settings.get("thresholds", "leak", "duration_minutes", default=10)
        self._leak_tracker = _LeakTracker(window_s=leak_duration_min * 60 * 1.5)
        self._hung_since: dict[int, float] = {}

    def _window(self, name: str) -> _SustainedWindow:
        return self._windows.setdefault(name, _SustainedWindow())

    def _z_score_ok(self, baseline: db.Baseline | None, value: float) -> bool:
        if baseline is None or baseline.sample_count < MIN_BASELINE_SAMPLES:
            return True  # baseline non ancora affidabile: si usa solo la soglia assoluta
        std = baseline.ewma_var**0.5
        if std <= 0:
            return True
        z = abs(value - baseline.ewma_mean) / std
        return z >= Z_SCORE_THRESHOLD

    def evaluate(self, conn, tick: TickInput) -> list[NewIncident]:
        snapshots = {pid: s for pid, s in tick.snapshots.items() if not protection.is_own_process(pid)}
        tick.snapshots = snapshots
        groups = grouping.build_groups(snapshots)
        tick.groups = groups
        gpu_per_pid = gpu_collector.aggregate_per_pid(tick.gpu_raw)
        hour = datetime.fromtimestamp(tick.now_ms / 1000).hour

        events: list[NewIncident] = []
        events += self._check_cpu(conn, tick, hour, groups)
        events += self._check_ram(conn, tick, hour, groups)
        events += self._check_disco(conn, tick, hour, groups)
        events += self._check_rete(conn, tick, hour, groups)
        events += self._check_gpu(conn, tick, hour, groups, gpu_per_pid)
        events += self._check_throttling(conn, tick, hour)
        events += self._check_leak(conn, tick, groups)
        events += self._check_hung(conn, tick, groups)
        return events

    # --- CPU ---------------------------------------------------------------------

    def _check_cpu(self, conn, tick: TickInput, hour: int, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        is_critical = tick.cpu_percent >= t.get("thresholds", "cpu", "critical_percent", default=95)
        is_anomaly = tick.cpu_percent >= t.get("thresholds", "cpu", "anomaly_percent", default=85)

        frac_c = self._window("cpu_critico").record(
            tick.now_ms / 1000, is_critical, t.get("thresholds", "cpu", "critical_window_seconds", default=20)
        )
        frac_a = self._window("cpu_anomalia").record(
            tick.now_ms / 1000, is_anomaly, t.get("thresholds", "cpu", "anomaly_window_seconds", default=30)
        )
        baseline = db.update_baseline(conn, "cpu_percent", hour, tick.cpu_percent, BASELINE_ALPHA, tick.now_ms)

        severity = None
        if frac_c >= SUSTAINED_FRACTION:
            severity = "critical"
        elif frac_a >= SUSTAINED_FRACTION and self._z_score_ok(baseline, tick.cpu_percent):
            severity = "anomaly"
        if severity is None:
            return []

        threshold = t.get("thresholds", "cpu", "culprit_group_percent", default=35)
        best_key, best_value = None, 0.0
        for group_key, pids in groups.items():
            value = group_cpu_percent(pids, tick.prev_snapshots, tick.snapshots, tick.dt_s, self._cores)
            if value > best_value:
                best_key, best_value = group_key, value
        if best_key is None or best_value < threshold:
            return []

        return self._open_incident(
            conn, tick, groups, resource="cpu", severity=severity, group_key=best_key,
            quota_percent=best_value, proposed_action="soft",
        )

    # --- RAM ---------------------------------------------------------------------

    def _check_ram(self, conn, tick: TickInput, hour: int, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        ram_avail_pct = (tick.ram_available_mb / tick.ram_total_mb * 100) if tick.ram_total_mb else 100.0
        commit = tick.ram_commit_percent or 0.0
        page_reads = tick.page_reads_sec or 0.0

        is_critical = commit >= t.get("thresholds", "ram", "critical_commit_percent", default=95) or (
            ram_avail_pct <= t.get("thresholds", "ram", "critical_available_percent_max", default=5)
            and page_reads >= t.get("thresholds", "ram", "critical_page_reads_sec", default=500)
        )
        is_anomaly = (
            ram_avail_pct <= t.get("thresholds", "ram", "anomaly_available_percent_max", default=10)
            or commit >= t.get("thresholds", "ram", "anomaly_commit_percent_min", default=90)
        ) and page_reads >= t.get("thresholds", "ram", "anomaly_page_reads_sec", default=100)

        frac_c = self._window("ram_critico").record(tick.now_ms / 1000, is_critical, 20)
        frac_a = self._window("ram_anomalia").record(tick.now_ms / 1000, is_anomaly, 30)
        baseline = db.update_baseline(conn, "ram_available_pct", hour, ram_avail_pct, BASELINE_ALPHA, tick.now_ms)

        severity = None
        if frac_c >= SUSTAINED_FRACTION:
            severity = "critical"
        elif frac_a >= SUSTAINED_FRACTION and self._z_score_ok(baseline, ram_avail_pct):
            severity = "anomaly"
        if severity is None:
            return []

        threshold_pct = t.get("thresholds", "ram", "culprit_group_percent", default=25)
        best_key, best_pct = None, 0.0
        for group_key, pids in groups.items():
            mb = group_ram_private_mb(pids, tick.snapshots)
            pct = (mb / tick.ram_total_mb * 100) if tick.ram_total_mb else 0.0
            if pct > best_pct:
                best_key, best_pct = group_key, pct
        if best_key is None or best_pct < threshold_pct:
            return []

        return self._open_incident(
            conn, tick, groups, resource="ram", severity=severity, group_key=best_key,
            quota_percent=best_pct, proposed_action="hard" if severity == "critical" else "soft",
        )

    # --- Disco ---------------------------------------------------------------------

    def _check_disco(self, conn, tick: TickInput, hour: int, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        latency = tick.disk_latency_ms or 0.0
        iops = tick.disk_iops or 0.0
        queue = tick.disk_queue_length or 0.0
        free_gb = tick.disk_free_gb if tick.disk_free_gb is not None else 999.0

        is_critical = latency >= t.get("thresholds", "disk", "critical_latency_ms", default=200) or (
            free_gb < t.get("thresholds", "disk", "critical_free_space_gb_max", default=2)
        )
        is_anomaly = (
            latency >= t.get("thresholds", "disk", "anomaly_latency_ms", default=50)
            and iops >= t.get("thresholds", "disk", "anomaly_iops_min", default=20)
            and queue >= t.get("thresholds", "disk", "anomaly_queue_min", default=2)
        )

        frac_c = self._window("disco_critico").record(tick.now_ms / 1000, is_critical, 20)
        frac_a = self._window("disco_anomalia").record(tick.now_ms / 1000, is_anomaly, 30)
        baseline = db.update_baseline(conn, "disk_latency_ms", hour, latency, BASELINE_ALPHA, tick.now_ms)

        severity = None
        if frac_c >= SUSTAINED_FRACTION:
            severity = "critical"
        elif frac_a >= SUSTAINED_FRACTION and self._z_score_ok(baseline, latency):
            severity = "anomaly"
        if severity is None:
            return []

        threshold_pct = t.get("thresholds", "disk", "culprit_group_percent_bytes", default=50)
        totals = {gk: group_io_bytes_sec(pids, tick.prev_snapshots, tick.snapshots, tick.dt_s) for gk, pids in groups.items()}
        total_all = sum(totals.values())
        if total_all <= 0:
            return []
        best_key, best_pct = max(totals.items(), key=lambda kv: kv[1])
        best_pct = (best_pct / total_all) * 100
        if best_pct < threshold_pct:
            return []

        return self._open_incident(
            conn, tick, groups, resource="disk", severity=severity, group_key=best_key,
            quota_percent=best_pct, proposed_action="soft",
        )

    # --- Rete (solo anomalia: euristica, confidenza bassa) ----------------------------

    def _check_rete(self, conn, tick: TickInput, hour: int, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        rate_bytes_s = (tick.net_rx_bytes_sec or 0.0) + (tick.net_tx_bytes_sec or 0.0)
        rate_mbit_s = rate_bytes_s * 8 / 1_000_000

        link_pct = None
        if tick.link_speed_mbps:
            link_pct = rate_mbit_s / tick.link_speed_mbps * 100

        baseline = db.update_baseline(conn, "net_total_mbit_s", hour, rate_mbit_s, BASELINE_ALPHA, tick.now_ms)
        sigma_hit = False
        if baseline is not None and baseline.sample_count >= MIN_BASELINE_SAMPLES:
            std = baseline.ewma_var**0.5
            sigma = t.get("thresholds", "network", "anomaly_sigma", default=4)
            sigma_hit = std > 0 and (rate_mbit_s - baseline.ewma_mean) / std >= sigma

        is_anomaly = (link_pct is not None and link_pct >= t.get("thresholds", "network", "anomaly_percent_link", default=80)) or (
            sigma_hit and rate_mbit_s >= t.get("thresholds", "network", "anomaly_mbit_s_min", default=5)
        )
        frac = self._window("rete_anomalia").record(tick.now_ms / 1000, is_anomaly, t.get("thresholds", "network", "window_seconds", default=60))
        if frac < SUSTAINED_FRACTION:
            return []

        # Attribuzione euristica: stesso contatore I/O del disco (somma disco+rete, vedi ADR).
        totals = {gk: group_io_bytes_sec(pids, tick.prev_snapshots, tick.snapshots, tick.dt_s) for gk, pids in groups.items()}
        if not totals or max(totals.values(), default=0) <= 0:
            return []
        best_key, best_value = max(totals.items(), key=lambda kv: kv[1])

        return self._open_incident(
            conn, tick, groups, resource="network", severity="anomaly", group_key=best_key,
            quota_percent=rate_mbit_s, proposed_action="soft",
            detail={"confidenza": "bassa", "euristica": "I/O combinato disco+rete"},
        )

    # --- GPU (solo anomalia) ---------------------------------------------------------

    def _check_gpu(self, conn, tick: TickInput, hour: int, groups: dict[str, list[int]], gpu_per_pid: dict[int, float]) -> list[NewIncident]:
        t = self._settings
        value = tick.gpu_percent or 0.0
        is_anomaly = value >= t.get("thresholds", "gpu", "anomaly_engine_percent", default=90)
        frac = self._window("gpu_anomalia").record(tick.now_ms / 1000, is_anomaly, t.get("thresholds", "gpu", "window_seconds", default=60))
        if frac < SUSTAINED_FRACTION:
            return []

        threshold = t.get("thresholds", "gpu", "culprit_group_engine_percent", default=60)
        best_key, best_value = None, 0.0
        for group_key, pids in groups.items():
            v = group_gpu_percent(pids, gpu_per_pid)
            if v > best_value:
                best_key, best_value = group_key, v
        if best_key is None or best_value < threshold:
            return []

        return self._open_incident(
            conn, tick, groups, resource="gpu", severity="anomaly", group_key=best_key,
            quota_percent=best_value, proposed_action="soft",
        )

    # --- Throttling (sistema, nessun colpevole) ---------------------------------------

    def _check_throttling(self, conn, tick: TickInput, hour: int) -> list[NewIncident]:
        t = self._settings
        carico_min = t.get("thresholds", "throttling", "load_percent_min", default=60)
        perf_frac = t.get("thresholds", "throttling", "performance_percent_max", default=60) / 100.0
        duration_s = t.get("thresholds", "throttling", "duration_seconds", default=60)

        under_load = tick.cpu_percent >= carico_min
        if under_load and tick.cpu_perf_percent is not None:
            baseline = db.update_baseline(conn, "cpu_perf_percent_under_load", hour, tick.cpu_perf_percent, BASELINE_ALPHA, tick.now_ms)
        else:
            baseline = db.get_baseline(conn, "cpu_perf_percent_under_load", hour)

        is_throttling = False
        if under_load and tick.cpu_perf_percent is not None and baseline is not None and baseline.sample_count >= MIN_BASELINE_SAMPLES:
            is_throttling = tick.cpu_perf_percent < baseline.ewma_mean * perf_frac

        frac = self._window("throttling").record(tick.now_ms / 1000, is_throttling, duration_s)
        if frac < SUSTAINED_FRACTION:
            return []

        label = "limite energetico" if tick.on_battery else "throttling termico"
        return self._open_incident(
            conn, tick, {}, resource="system", severity="critical", group_key=None,
            quota_percent=tick.cpu_perf_percent, proposed_action="none", incident_type="throttling",
            detail={"etichetta": label, "cpu_perf_limit_percent": tick.cpu_perf_limit_percent},
        )

    # --- Leak (RAM di un gruppo, crescita costante) -----------------------------------

    def _check_leak(self, conn, tick: TickInput, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        growth_min = t.get("thresholds", "leak", "growth_mb_per_min", default=30)
        duration_min = t.get("thresholds", "leak", "duration_minutes", default=10)
        r2_min = t.get("thresholds", "leak", "r2_min", default=0.8)
        soglia_mb = t.get("thresholds", "leak", "threshold_mb", default=500)

        events: list[NewIncident] = []
        for group_key, pids in groups.items():
            private_mb = group_ram_private_mb(pids, tick.snapshots)
            self._leak_tracker.record(group_key, tick.now_ms / 1000, private_mb)
            if private_mb < soglia_mb:
                continue
            trend = self._leak_tracker.trend(group_key)
            if trend is None:
                continue
            slope, r2, duration_s = trend
            if slope >= growth_min and r2 >= r2_min and duration_s >= duration_min * 60:
                events += self._open_incident(
                    conn, tick, groups, resource="ram", severity="critical", group_key=group_key,
                    quota_percent=private_mb, proposed_action="hard", incident_type="leak",
                    detail={"growth_mb_per_min": slope, "r2": r2, "durata_s": duration_s},
                )
        return events

    # --- Hung (finestra non risponde + attivita' delle risorse) ----------------------

    def _check_hung(self, conn, tick: TickInput, groups: dict[str, list[int]]) -> list[NewIncident]:
        t = self._settings
        duration_s = t.get("thresholds", "hung", "duration_seconds", default=30)
        cpu_min = t.get("thresholds", "hung", "cpu_percent_min", default=25)
        io_min_mb_s = t.get("thresholds", "hung", "io_mb_s_min", default=1)

        window_map = windows_collector.enumerate_main_windows()
        now_s = tick.now_ms / 1000

        hung_now = set()
        for pid, hwnds in window_map.items():
            if pid not in tick.snapshots:
                continue
            if any(windows_collector.is_hung(h) for h in hwnds):
                hung_now.add(pid)

        for pid in list(self._hung_since):
            if pid not in hung_now:
                del self._hung_since[pid]
        for pid in hung_now:
            self._hung_since.setdefault(pid, now_s)

        events: list[NewIncident] = []
        for pid in hung_now:
            if now_s - self._hung_since[pid] < duration_s:
                continue
            curr = tick.snapshots[pid]
            prev = tick.prev_snapshots.get(pid)
            cpu_pct = 0.0
            io_mb_s = 0.0
            ram_growing = False
            if prev is not None and tick.dt_s > 0 and prev.create_time == curr.create_time:
                # Soglia "25% di UN core" (come Gestione attivita' per-processo): NON va divisa per
                # self._cores, a differenza delle quote di gruppo (quelle sono % della capacita'
                # totale del sistema). Bug trovato dal vivo: un busy-loop a un thread saturava un
                # intero core (100%) ma il calcolo diviso per 8 core dava 12.5%, sotto la soglia 25%.
                cpu_pct = (curr.cpu_time_seconds - prev.cpu_time_seconds) / tick.dt_s * 100.0
                io_mb_s = ((curr.io_read_bytes + curr.io_write_bytes) - (prev.io_read_bytes + prev.io_write_bytes)) / tick.dt_s / (1024 * 1024)
                ram_growing = curr.private_bytes > prev.private_bytes

            if not (cpu_pct >= cpu_min or ram_growing or io_mb_s >= io_min_mb_s):
                continue

            # I processi L0 (System, dwm, ...) non sono mai un bersaglio azionabile per "non
            # risponde": DWM in particolare puo' avere finestre principali transitorie durante le
            # animazioni (apertura/chiusura di un'altra finestra) segnalate erroneamente come
            # "hung" da IsHungAppWindow per un singolo tick. Verificato dal vivo in M2.
            own_ancestors = grouping.ancestor_names(pid, tick.snapshots)
            if protection.classify(curr.name, None, own_ancestors).level == protection.ProtectionLevel.L0_UNTOUCHABLE:
                continue

            root_pid = grouping.group_root_pid(pid, tick.snapshots)
            group_key = protection.normalize_name(tick.snapshots[root_pid].name)
            events += self._open_incident(
                conn, tick, groups, resource="app", severity="critical", group_key=group_key,
                quota_percent=None, proposed_action="hard", incident_type="hung",
                detail={"pid": pid, "cpu_percent": cpu_pct, "io_mb_s": io_mb_s, "ram_in_crescita": ram_growing},
            )
        return events

    # --- creazione incidente (comune) ---------------------------------------------

    def _open_incident(
        self,
        conn,
        tick: TickInput,
        groups: dict[str, list[int]],
        *,
        resource: str,
        severity: str,
        group_key: str | None,
        quota_percent: float | None,
        proposed_action: str,
        incident_type: str | None = None,
        detail: dict | None = None,
    ) -> list[NewIncident]:
        incident_type = incident_type or severity
        if db.get_open_incident(conn, group_key, resource) is not None:
            return []  # un solo incidente aperto per coppia (gruppo, risorsa)

        level = None
        if group_key is not None:
            pids = groups.get(group_key, [])
            if pids:
                rep_pid = min(pids, key=lambda p: tick.snapshots[p].create_time)
                rep = tick.snapshots[rep_pid]
                ancestors = grouping.ancestor_names(rep_pid, tick.snapshots)
                result = protection.classify(rep.name, None, ancestors)
                level = int(result.level)
                if result.level == protection.ProtectionLevel.L0_UNTOUCHABLE:
                    proposed_action = "none"

        incident_id = db.create_incident(
            conn,
            opened_at_ms=tick.now_ms,
            resource=resource,
            incident_type=incident_type,
            severity=severity,
            group_key=group_key,
            quota_percent=quota_percent,
            protection_level=level,
            proposed_action=proposed_action,
            detail_json=json.dumps(detail) if detail else None,
        )
        logger.info(
            "Incidente #%s aperto: risorsa=%s tipo=%s gruppo=%s severita=%s livello_protezione=%s",
            incident_id, resource, incident_type, group_key, severity, level,
        )
        return [NewIncident(incident_id, resource, incident_type, severity, group_key, level, quota_percent, detail or {})]
