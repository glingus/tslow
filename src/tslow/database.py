"""Connessione SQLite, migrazioni, repository e manutenzione (rollup/retention).

Il DB (`data/metrics.db`, WAL) e' l'unico canale tra demone e CLI: vedi CLAUDE.md.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tslow.config import load_settings
from tslow.paths import PACKAGE_DIR, db_path

MIGRATIONS_DIR = PACKAGE_DIR / "migrations"

MINUTE_MS = 60_000
HOUR_MS = 3_600_000


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def connect(path: Path | None = None, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Apre (o crea) il DB, imposta i PRAGMA e applica le migrazioni mancanti.

    `check_same_thread=False` serve solo all'API web (M6, `tslow.web.api`): FastAPI esegue
    le dipendenze sync e l'endpoint nel pool di thread di anyio, non necessariamente sullo
    stesso thread che ha aperto la connessione. La connessione resta comunque usata da un
    solo thread alla volta (aperta e chiusa nello stesso ciclo di richiesta, mai in
    concorrenza), quindi disattivare il controllo di sqlite3 qui e' sicuro.
    """
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    run_migrations(conn)
    return conn


def _backup_before_migration(conn: sqlite3.Connection, version: int) -> None:
    """Copia di sicurezza (VACUUM INTO) del DB esistente prima di applicare migrazioni nuove.

    Una sola copia per versione di partenza (`metrics.db.bak-v<N>`); se esiste gia', non la riscrive.
    """
    row = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()
    if not row or not row[0]:
        return  # DB in memoria
    target = Path(f"{row[0]}.bak-v{version}")
    if target.exists():
        return
    conn.execute("VACUUM INTO ?", (str(target),))


def run_migrations(conn: sqlite3.Connection) -> int:
    """Applica le migrazioni .sql non ancora applicate, tracciate via PRAGMA user_version. Ritorna la versione finale."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = _migration_files()
    version = current
    if 0 < current < len(files):
        _backup_before_migration(conn, current)
    for index, path in enumerate(files, start=1):
        if index <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        version = index
        conn.execute(f"PRAGMA user_version = {version}")
    return version


# --- metrics_raw ------------------------------------------------------------


@dataclass
class MetricSample:
    ts_ms: int
    sampler_state: str
    period_ms: int
    cpu_percent: float | None = None
    cpu_queue_per_core: float | None = None
    cpu_perf_percent: float | None = None
    cpu_perf_limit_percent: float | None = None
    ram_percent: float | None = None
    ram_available_mb: float | None = None
    ram_commit_percent: float | None = None
    page_reads_sec: float | None = None
    disk_read_bytes_sec: float | None = None
    disk_write_bytes_sec: float | None = None
    disk_latency_ms: float | None = None
    disk_queue_length: float | None = None
    disk_free_gb: float | None = None
    net_rx_bytes_sec: float | None = None
    net_tx_bytes_sec: float | None = None
    gpu_percent: float | None = None
    battery_percent: float | None = None


_METRIC_COLUMNS = [f.name for f in dataclasses.fields(MetricSample)]


def insert_metrics_raw(conn: sqlite3.Connection, samples: list[MetricSample]) -> None:
    if not samples:
        return
    placeholders = ", ".join("?" for _ in _METRIC_COLUMNS)
    sql = f"INSERT INTO metrics_raw ({', '.join(_METRIC_COLUMNS)}) VALUES ({placeholders})"
    rows = [tuple(getattr(s, c) for c in _METRIC_COLUMNS) for s in samples]
    conn.executemany(sql, rows)


# --- process_minutes ---------------------------------------------------------


@dataclass
class ProcessMinuteSample:
    minute_ts: int
    group_key: str
    display_name: str
    cpu_percent_avg: float
    cpu_percent_max: float
    ram_private_mb_max: float
    io_bytes_sec: float
    gpu_percent: float
    instance_count: int


_PROCESS_MINUTE_COLUMNS = [f.name for f in dataclasses.fields(ProcessMinuteSample)]


def insert_process_minutes(conn: sqlite3.Connection, rows: list[ProcessMinuteSample]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in _PROCESS_MINUTE_COLUMNS)
    sql = f"INSERT INTO process_minutes ({', '.join(_PROCESS_MINUTE_COLUMNS)}) VALUES ({placeholders})"
    values = [tuple(getattr(r, c) for c in _PROCESS_MINUTE_COLUMNS) for r in rows]
    conn.executemany(sql, values)


# --- rollup -------------------------------------------------------------------


def rollup_1m(conn: sqlite3.Connection, now_ms: int) -> int:
    """Aggrega metrics_raw in metrics_1m per ogni minuto completo non ancora aggregato."""
    current_minute_start = (now_ms // MINUTE_MS) * MINUTE_MS
    last = conn.execute("SELECT MAX(minute_ts) FROM metrics_1m").fetchone()[0]
    if last is not None:
        start_from = last + MINUTE_MS
    else:
        earliest = conn.execute("SELECT MIN(ts_ms) FROM metrics_raw").fetchone()[0]
        if earliest is None:
            return 0
        start_from = (earliest // MINUTE_MS) * MINUTE_MS

    count = 0
    minute = start_from
    while minute < current_minute_start:
        end = minute + MINUTE_MS
        row = conn.execute(
            """
            SELECT AVG(cpu_percent), MAX(cpu_percent), AVG(ram_percent), MAX(ram_percent),
                   AVG(disk_latency_ms), MAX(disk_latency_ms),
                   AVG(net_rx_bytes_sec), AVG(net_tx_bytes_sec),
                   AVG(gpu_percent), MAX(gpu_percent), COUNT(*)
            FROM metrics_raw WHERE ts_ms >= ? AND ts_ms < ?
            """,
            (minute, end),
        ).fetchone()
        if row is not None and row[10]:
            conn.execute(
                """
                INSERT OR REPLACE INTO metrics_1m
                (minute_ts, cpu_percent_avg, cpu_percent_max, ram_percent_avg, ram_percent_max,
                 disk_latency_ms_avg, disk_latency_ms_max, net_rx_bytes_sec_avg, net_tx_bytes_sec_avg,
                 gpu_percent_avg, gpu_percent_max, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (minute, *row),
            )
            count += 1
        minute = end
    return count


def rollup_1h(conn: sqlite3.Connection, now_ms: int) -> int:
    """Aggrega metrics_1m in metrics_1h per ogni ora completa non ancora aggregata."""
    current_hour_start = (now_ms // HOUR_MS) * HOUR_MS
    last = conn.execute("SELECT MAX(hour_ts) FROM metrics_1h").fetchone()[0]
    if last is not None:
        start_from = last + HOUR_MS
    else:
        earliest = conn.execute("SELECT MIN(minute_ts) FROM metrics_1m").fetchone()[0]
        if earliest is None:
            return 0
        start_from = (earliest // HOUR_MS) * HOUR_MS

    count = 0
    hour = start_from
    while hour < current_hour_start:
        end = hour + HOUR_MS
        row = conn.execute(
            """
            SELECT AVG(cpu_percent_avg), MAX(cpu_percent_max), AVG(ram_percent_avg), MAX(ram_percent_max),
                   AVG(disk_latency_ms_avg), MAX(disk_latency_ms_max),
                   AVG(net_rx_bytes_sec_avg), AVG(net_tx_bytes_sec_avg),
                   AVG(gpu_percent_avg), MAX(gpu_percent_max), SUM(sample_count)
            FROM metrics_1m WHERE minute_ts >= ? AND minute_ts < ?
            """,
            (hour, end),
        ).fetchone()
        if row is not None and row[10]:
            conn.execute(
                """
                INSERT OR REPLACE INTO metrics_1h
                (hour_ts, cpu_percent_avg, cpu_percent_max, ram_percent_avg, ram_percent_max,
                 disk_latency_ms_avg, disk_latency_ms_max, net_rx_bytes_sec_avg, net_tx_bytes_sec_avg,
                 gpu_percent_avg, gpu_percent_max, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hour, *row),
            )
            count += 1
        hour = end
    return count


# --- retention ------------------------------------------------------------


def apply_retention(conn: sqlite3.Connection, now_ms: int) -> None:
    settings = load_settings()

    def days(key: str, default: float) -> int:
        return int(settings.get("retention", key, default=default) * 86_400_000)

    def hours(key: str, default: float) -> int:
        return int(settings.get("retention", key, default=default) * 3_600_000)

    conn.execute("DELETE FROM metrics_raw WHERE ts_ms < ?", (now_ms - hours("metrics_raw_hours", 48),))
    conn.execute("DELETE FROM metrics_1m WHERE minute_ts < ?", (now_ms - days("metrics_1m_days", 30),))
    conn.execute("DELETE FROM metrics_1h WHERE hour_ts < ?", (now_ms - days("metrics_1h_days", 365),))
    conn.execute("DELETE FROM process_minutes WHERE minute_ts < ?", (now_ms - days("process_minutes_days", 7),))
    conn.execute("DELETE FROM monitor_health WHERE ts_ms < ?", (now_ms - days("monitor_health_days", 7),))


# --- monitor_health ---------------------------------------------------------


def record_monitor_health(
    conn: sqlite3.Connection,
    ts_ms: int,
    daemon_cpu_percent: float,
    daemon_rss_mb: float,
    sampler_state: str,
    period_multiplier: float,
) -> None:
    conn.execute(
        """
        INSERT INTO monitor_health (ts_ms, daemon_cpu_percent, daemon_rss_mb, sampler_state, period_multiplier)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts_ms, daemon_cpu_percent, daemon_rss_mb, sampler_state, period_multiplier),
    )


# --- app_settings -------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


# --- system_inventory ----------------------------------------------------------


def insert_system_inventory(
    conn: sqlite3.Connection,
    *,
    collected_at_ms: int,
    cpu_name: str | None = None,
    cpu_cores_physical: int | None = None,
    cpu_cores_logical: int | None = None,
    ram_total_mb: float | None = None,
    gpu_names: str | None = None,
    disk_model: str | None = None,
    os_version: str | None = None,
    os_language: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO system_inventory
        (collected_at_ms, cpu_name, cpu_cores_physical, cpu_cores_logical, ram_total_mb,
         gpu_names, disk_model, os_version, os_language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            collected_at_ms,
            cpu_name,
            cpu_cores_physical,
            cpu_cores_logical,
            ram_total_mb,
            gpu_names,
            disk_model,
            os_version,
            os_language,
        ),
    )


# --- baselines (M2) --------------------------------------------------------------


@dataclass
class Baseline:
    metric: str
    hour_of_day: int
    ewma_mean: float
    ewma_var: float
    sample_count: int


def get_baseline(conn: sqlite3.Connection, metric: str, hour_of_day: int) -> Baseline | None:
    row = conn.execute(
        "SELECT metric, hour_of_day, ewma_mean, ewma_var, sample_count FROM baselines WHERE metric = ? AND hour_of_day = ?",
        (metric, hour_of_day),
    ).fetchone()
    return Baseline(*row) if row is not None else None


def update_baseline(conn: sqlite3.Connection, metric: str, hour_of_day: int, value: float, alpha: float, now_ms: int) -> Baseline:
    """Aggiorna media e varianza EWMA per (metrica, ora del giorno). Crea la riga al primo campione."""
    existing = get_baseline(conn, metric, hour_of_day)
    if existing is None:
        mean, var, count = value, 0.0, 1
    else:
        delta = value - existing.ewma_mean
        mean = existing.ewma_mean + alpha * delta
        var = (1 - alpha) * (existing.ewma_var + alpha * delta * delta)
        count = existing.sample_count + 1
    conn.execute(
        """
        INSERT INTO baselines (metric, hour_of_day, ewma_mean, ewma_var, sample_count, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(metric, hour_of_day) DO UPDATE SET
            ewma_mean = excluded.ewma_mean, ewma_var = excluded.ewma_var,
            sample_count = excluded.sample_count, updated_at_ms = excluded.updated_at_ms
        """,
        (metric, hour_of_day, mean, var, count, now_ms),
    )
    return Baseline(metric, hour_of_day, mean, var, count)


# --- incidents (M2) ----------------------------------------------------------------


@dataclass
class Incident:
    id: int
    opened_at_ms: int
    closed_at_ms: int | None
    resource: str
    incident_type: str
    severity: str
    group_key: str | None
    exe_path: str | None
    culprit_pids_json: str | None
    quota_percent: float | None
    protection_level: int | None
    proposed_action: str
    status: str
    detail_json: str | None


def _incident_from_row(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        opened_at_ms=row["opened_at_ms"],
        closed_at_ms=row["closed_at_ms"],
        resource=row["resource"],
        incident_type=row["incident_type"],
        severity=row["severity"],
        group_key=row["group_key"],
        exe_path=row["exe_path"],
        culprit_pids_json=row["culprit_pids_json"],
        quota_percent=row["quota_percent"],
        protection_level=row["protection_level"],
        proposed_action=row["proposed_action"],
        status=row["status"],
        detail_json=row["detail_json"],
    )


def get_open_incident(conn: sqlite3.Connection, group_key: str | None, resource: str) -> Incident | None:
    """Un solo incidente aperto per coppia (gruppo, risorsa): va controllato prima di aprirne uno nuovo."""
    if group_key is None:
        row = conn.execute(
            "SELECT * FROM incidents WHERE group_key IS NULL AND resource = ? AND status = 'open' "
            "ORDER BY opened_at_ms DESC LIMIT 1",
            (resource,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM incidents WHERE group_key = ? AND resource = ? AND status = 'open' "
            "ORDER BY opened_at_ms DESC LIMIT 1",
            (group_key, resource),
        ).fetchone()
    return _incident_from_row(row) if row is not None else None


def create_incident(
    conn: sqlite3.Connection,
    *,
    opened_at_ms: int,
    resource: str,
    incident_type: str,
    severity: str,
    group_key: str | None = None,
    exe_path: str | None = None,
    culprit_pids_json: str | None = None,
    quota_percent: float | None = None,
    protection_level: int | None = None,
    proposed_action: str = "none",
    detail_json: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO incidents
        (opened_at_ms, resource, incident_type, severity, group_key, exe_path, culprit_pids_json,
         quota_percent, protection_level, proposed_action, status, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            opened_at_ms,
            resource,
            incident_type,
            severity,
            group_key,
            exe_path,
            culprit_pids_json,
            quota_percent,
            protection_level,
            proposed_action,
            detail_json,
        ),
    )
    return cur.lastrowid


def close_incident(conn: sqlite3.Connection, incident_id: int, closed_at_ms: int, status: str = "resolved") -> None:
    conn.execute("UPDATE incidents SET status = ?, closed_at_ms = ? WHERE id = ?", (status, closed_at_ms, incident_id))


def list_open_incidents(conn: sqlite3.Connection) -> list[Incident]:
    rows = conn.execute("SELECT * FROM incidents WHERE status = 'open' ORDER BY opened_at_ms DESC").fetchall()
    return [_incident_from_row(r) for r in rows]


def expire_stale_incidents(conn: sqlite3.Connection, now_ms: int, ttl_ms: int) -> int:
    """Chiude come 'expired' gli incidenti aperti da piu' di `ttl_ms` e senza decisioni pending."""
    cur = conn.execute(
        "UPDATE incidents SET status = 'expired', closed_at_ms = ? "
        "WHERE status = 'open' AND opened_at_ms < ? "
        "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.incident_id = incidents.id AND d.execution_status = 'pending')",
        (now_ms, now_ms - ttl_ms),
    )
    return cur.rowcount


def has_actionable_open_incident(conn: sqlite3.Connection) -> bool:
    """True se esiste un incidente aperto a cui l'utente puo' ancora rispondere con una decisione."""
    row = conn.execute("SELECT 1 FROM incidents WHERE status = 'open' AND proposed_action != 'none' LIMIT 1").fetchone()
    return row is not None


def get_incident(conn: sqlite3.Connection, incident_id: int) -> Incident | None:
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return _incident_from_row(row) if row is not None else None


# --- notifications (M2) ------------------------------------------------------------


def record_notification(
    conn: sqlite3.Connection, incident_id: int, sent_at_ms: int, channel: str, success: bool, error: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO notifications (incident_id, sent_at_ms, channel, success, error) VALUES (?, ?, ?, ?, ?)",
        (incident_id, sent_at_ms, channel, 1 if success else 0, error),
    )


def last_notification_for_group(conn: sqlite3.Connection, group_key: str) -> int | None:
    """ts_ms dell'ultima notifica riuscita per un gruppo (join su incidents): usato per il cooldown."""
    row = conn.execute(
        """
        SELECT MAX(n.sent_at_ms) FROM notifications n
        JOIN incidents i ON i.id = n.incident_id
        WHERE i.group_key = ? AND n.success = 1
        """,
        (group_key,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def notifications_count_since(conn: sqlite3.Connection, since_ms: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE sent_at_ms >= ? AND success = 1", (since_ms,)
    ).fetchone()
    return row[0]


# --- user_rules (M3) ------------------------------------------------------------


@dataclass
class UserRule:
    id: int
    app_identity: str
    status: str
    consecutive_denies: int
    allow_once_count: int
    max_auto_action: str | None
    source: str
    expires_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int


def _rule_from_row(row: sqlite3.Row) -> UserRule:
    return UserRule(
        id=row["id"],
        app_identity=row["app_identity"],
        status=row["status"],
        consecutive_denies=row["consecutive_denies"],
        allow_once_count=row["allow_once_count"],
        max_auto_action=row["max_auto_action"],
        source=row["source"],
        expires_at_ms=row["expires_at_ms"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def get_user_rule(conn: sqlite3.Connection, app_identity: str) -> UserRule | None:
    row = conn.execute("SELECT * FROM user_rules WHERE app_identity = ?", (app_identity,)).fetchone()
    return _rule_from_row(row) if row is not None else None


def upsert_user_rule(
    conn: sqlite3.Connection,
    app_identity: str,
    *,
    consecutive_denies: int,
    allow_once_count: int,
    max_auto_action: str | None,
    source: str,
    expires_at_ms: int | None,
    now_ms: int,
) -> UserRule:
    conn.execute(
        """
        INSERT INTO user_rules
        (app_identity, status, consecutive_denies, allow_once_count, max_auto_action, source, expires_at_ms, created_at_ms, updated_at_ms)
        VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_identity) DO UPDATE SET
            status = 'active', consecutive_denies = excluded.consecutive_denies,
            allow_once_count = excluded.allow_once_count, max_auto_action = excluded.max_auto_action,
            source = excluded.source, expires_at_ms = excluded.expires_at_ms, updated_at_ms = excluded.updated_at_ms
        """,
        (app_identity, consecutive_denies, allow_once_count, max_auto_action, source, expires_at_ms, now_ms, now_ms),
    )
    return get_user_rule(conn, app_identity)


def revoke_user_rule(conn: sqlite3.Connection, app_identity: str, now_ms: int) -> None:
    conn.execute(
        "UPDATE user_rules SET status = 'revoked', updated_at_ms = ? WHERE app_identity = ?", (now_ms, app_identity)
    )


def get_user_rule_by_id(conn: sqlite3.Connection, rule_id: int) -> UserRule | None:
    row = conn.execute("SELECT * FROM user_rules WHERE id = ?", (rule_id,)).fetchone()
    return _rule_from_row(row) if row is not None else None


def revoke_user_rule_by_id(conn: sqlite3.Connection, rule_id: int, now_ms: int) -> bool:
    cur = conn.execute("UPDATE user_rules SET status = 'revoked', updated_at_ms = ? WHERE id = ?", (now_ms, rule_id))
    return cur.rowcount > 0


def reset_learning(conn: sqlite3.Connection) -> int:
    """Cancella tutte le regole apprese (`tslow rules reset-learning`). Ritorna quante righe rimosse."""
    cur = conn.execute("DELETE FROM user_rules")
    return cur.rowcount


def list_user_rules(conn: sqlite3.Connection, *, only_active: bool = True) -> list[UserRule]:
    if only_active:
        rows = conn.execute("SELECT * FROM user_rules WHERE status = 'active' ORDER BY updated_at_ms DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM user_rules ORDER BY updated_at_ms DESC").fetchall()
    return [_rule_from_row(r) for r in rows]


# --- decisions (M3) ----------------------------------------------------------------


@dataclass
class Decision:
    id: int
    incident_id: int
    created_at_ms: int
    choice: str
    source: str
    execution_status: str
    executed_at_ms: int | None


def _decision_from_row(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        incident_id=row["incident_id"],
        created_at_ms=row["created_at_ms"],
        choice=row["choice"],
        source=row["source"],
        execution_status=row["execution_status"],
        executed_at_ms=row["executed_at_ms"],
    )


def create_decision(conn: sqlite3.Connection, incident_id: int, choice: str, *, source: str = "user", now_ms: int) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (incident_id, created_at_ms, choice, source, execution_status) VALUES (?, ?, ?, ?, 'pending')",
        (incident_id, now_ms, choice, source),
    )
    return cur.lastrowid


def update_decision_status(conn: sqlite3.Connection, decision_id: int, status: str, executed_at_ms: int) -> None:
    conn.execute(
        "UPDATE decisions SET execution_status = ?, executed_at_ms = ? WHERE id = ?", (status, executed_at_ms, decision_id)
    )


def get_decision(conn: sqlite3.Connection, decision_id: int) -> Decision | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return _decision_from_row(row) if row is not None else None


def list_pending_decisions(conn: sqlite3.Connection) -> list[Decision]:
    rows = conn.execute("SELECT * FROM decisions WHERE execution_status = 'pending' ORDER BY created_at_ms").fetchall()
    return [_decision_from_row(r) for r in rows]


def list_decisions_for_incident(conn: sqlite3.Connection, incident_id: int) -> list[Decision]:
    rows = conn.execute("SELECT * FROM decisions WHERE incident_id = ? ORDER BY created_at_ms", (incident_id,)).fetchall()
    return [_decision_from_row(r) for r in rows]


# --- actions_log (M3) ---------------------------------------------------------------


@dataclass
class ActionLogRow:
    id: int
    decision_id: int | None
    incident_id: int
    executed_at_ms: int
    action: str
    pid: int
    previous_priority: int | None
    previous_io_priority: int | None
    outcome: str


def _action_from_row(row: sqlite3.Row) -> ActionLogRow:
    return ActionLogRow(
        id=row["id"],
        decision_id=row["decision_id"],
        incident_id=row["incident_id"],
        executed_at_ms=row["executed_at_ms"],
        action=row["action"],
        pid=row["pid"],
        previous_priority=row["previous_priority"],
        previous_io_priority=row["previous_io_priority"],
        outcome=row["outcome"],
    )


def record_action(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    incident_id: int,
    executed_at_ms: int,
    action: str,
    pid: int,
    previous_priority: int | None,
    previous_io_priority: int | None,
    outcome: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO actions_log
        (decision_id, incident_id, executed_at_ms, action, pid, previous_priority, previous_io_priority, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (decision_id, incident_id, executed_at_ms, action, pid, previous_priority, previous_io_priority, outcome),
    )
    return cur.lastrowid


def list_actions_for_decision(conn: sqlite3.Connection, decision_id: int) -> list[ActionLogRow]:
    rows = conn.execute("SELECT * FROM actions_log WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()
    return [_action_from_row(r) for r in rows]


def get_action(conn: sqlite3.Connection, action_id: int) -> ActionLogRow | None:
    row = conn.execute("SELECT * FROM actions_log WHERE id = ?", (action_id,)).fetchone()
    return _action_from_row(row) if row is not None else None
