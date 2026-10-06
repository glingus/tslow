"""Analytics: funzioni pure su sqlite3 + stdlib, usate da TUI e web.

Nessuno stato, nessuna scrittura: prendono una connessione DB (sola lettura) e restituiscono
liste di dict o valori semplici (i valori mancanti sono `None`, mai NaN). `load_metrics` sceglie la tabella giusta in base all'intervallo
richiesto, per non leggere mai piu' righe del necessario.
"""

from __future__ import annotations

import sqlite3
from collections import deque

_RANGE_TABLES: dict[str, tuple[str, str]] = {
    "1h": ("metrics_raw", "ts_ms"),
    "24h": ("metrics_1m", "minute_ts"),
    "7d": ("metrics_1m", "minute_ts"),
    "30d": ("metrics_1h", "hour_ts"),
}

_RANGE_MS: dict[str, int] = {
    "1h": 3_600_000,
    "24h": 86_400_000,
    "7d": 7 * 86_400_000,
    "30d": 30 * 86_400_000,
}

RANGES = tuple(_RANGE_TABLES)


def range_since_ms(range_key: str, now_ms: int) -> int:
    """`now_ms` meno l'ampiezza dell'intervallo (per i `since_ms` di top_culprits/decision_stats/...)."""
    if range_key not in _RANGE_MS:
        raise ValueError(f"unknown range: {range_key!r} (valid: {sorted(_RANGE_MS)})")
    return now_ms - _RANGE_MS[range_key]


class MetricRows(list):
    """Righe (dict) di una query piu' i nomi delle colonne, che restano noti anche con zero righe."""

    columns: list[str]

    def __init__(self, rows=(), columns=()) -> None:
        super().__init__(rows)
        self.columns = list(columns)


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> MetricRows:
    cur = conn.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return MetricRows((dict(zip(columns, row)) for row in cur.fetchall()), columns)


def load_metrics(conn: sqlite3.Connection, range_key: str, now_ms: int) -> MetricRows:
    """Righe di metriche per l'intervallo richiesto, dalla tabella piu' economica che lo copre."""
    if range_key not in _RANGE_TABLES:
        raise ValueError(f"unknown range: {range_key!r} (valid: {sorted(_RANGE_TABLES)})")
    table, ts_col = _RANGE_TABLES[range_key]
    since = range_since_ms(range_key, now_ms)
    rows = _query(conn, f"SELECT * FROM {table} WHERE {ts_col} >= ? ORDER BY {ts_col}", (since,))
    if ts_col != "ts_ms":  # la colonna temporale dei rollup si chiama `minute_ts`/`hour_ts`
        rows = MetricRows(
            ({("ts_ms" if k == ts_col else k): v for k, v in row.items()} for row in rows),
            ["ts_ms" if c == ts_col else c for c in rows.columns],
        )
    return rows


def metric_column(rows: MetricRows, base: str, stat: str = "avg") -> str | None:
    """Nome reale della colonna per la metrica `base` nel risultato di `load_metrics`.

    `metrics_raw` (intervallo 1h) usa il nome diretto (es. `cpu_percent`); i rollup
    `metrics_1m`/`metrics_1h` (24h/7d/30d) lo espongono solo come `<nome>_avg`/`<nome>_max`
    (vedi migrazione 0001). Senza questa risoluzione, un chiamante che indicizza sempre col
    nome diretto vede "colonna assente" per ogni intervallo diverso da 1h anche con dati reali
    presenti — bug trovato dal vivo sul DB di produzione mentre si scriveva M6, che affliggeva
    silenziosamente anche `cli/dashboard.py` fin da M4 (vedi Task_Log).
    """
    if base in rows.columns:
        return base
    candidate = f"{base}_{stat}"
    return candidate if candidate in rows.columns else None


def values(rows: list[dict], column: str) -> list[float]:
    """Valori non nulli di una colonna, nell'ordine delle righe."""
    return [row[column] for row in rows if row.get(column) is not None]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def with_moving_averages(rows: list[dict], column: str, windows_ms: dict[str, int]) -> list[dict]:
    """Aggiunge a ogni riga `<column>_ma_<label>`: media dei valori non nulli in (ts_ms - finestra, ts_ms].

    `windows_ms`: es. {'15m': 900_000, '1h': 3_600_000}. Le righe vanno ordinate per `ts_ms`.
    `None` se nella finestra non c'e' nessun valore. Modifica le righe in place e le restituisce.
    """
    if not rows or column not in rows[0]:
        return rows
    for label, window_ms in windows_ms.items():
        key = f"{column}_ma_{label}"
        window: deque[tuple[int, float]] = deque()
        total = 0.0
        for row in rows:
            ts = row["ts_ms"]
            if row[column] is not None:
                window.append((ts, row[column]))
                total += row[column]
            while window and window[0][0] <= ts - window_ms:
                total -= window.popleft()[1]
            row[key] = total / len(window) if window else None
    return rows


def trend(rows: list[dict], column: str) -> float | None:
    """Pendenza per ora (unita' della colonna / ora) via regressione lineare ai minimi quadrati sul tempo."""
    if len(rows) < 2 or column not in rows[0]:
        return None
    t0 = rows[0]["ts_ms"]
    points = [((row["ts_ms"] - t0) / 3_600_000.0, float(row[column])) for row in rows if row[column] is not None]
    if len(points) < 2:
        return None
    mx = mean([x for x, _ in points])
    my = mean([y for _, y in points])
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / sxx


def top_culprits(conn: sqlite3.Connection, since_ms: int, limit: int = 10) -> list[dict]:
    """Gruppi-app con piu' incidenti nell'intervallo."""
    query = """
        SELECT group_key, resource, COUNT(*) AS incidenti, MAX(opened_at_ms) AS ultimo_ms
        FROM incidents
        WHERE opened_at_ms >= ? AND group_key IS NOT NULL
        GROUP BY group_key, resource
        ORDER BY incidenti DESC
        LIMIT ?
    """
    return list(_query(conn, query, (since_ms, limit)))


def decision_stats(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """Conteggio delle decisioni per scelta e origine ([1]/[2]/[3] utente o automatiche)."""
    query = """
        SELECT choice, source, COUNT(*) AS conteggio
        FROM decisions
        WHERE created_at_ms >= ?
        GROUP BY choice, source
        ORDER BY conteggio DESC
    """
    return list(_query(conn, query, (since_ms,)))


def incident_history(conn: sqlite3.Connection, since_ms: int, limit: int = 50) -> list[dict]:
    """Ultimi incidenti nell'intervallo, piu' recenti prima.

    Include `protection_level`/`proposed_action`/`quota_percent` (non solo i campi mostrati
    dalla TUI) perche' la dashboard web li usa per decidere se un incidente e' azionabile
    dal browser (vedi web/api.py::post_incident_decision), esattamente come cli/resolve.py.
    """
    query = """
        SELECT id, opened_at_ms, resource, incident_type, severity, group_key, status,
               protection_level, proposed_action, quota_percent
        FROM incidents
        WHERE opened_at_ms >= ?
        ORDER BY opened_at_ms DESC
        LIMIT ?
    """
    return list(_query(conn, query, (since_ms, limit)))


def overhead_summary(conn: sqlite3.Connection, since_ms: int) -> dict:
    """Media/max di CPU e RSS del demone nell'intervallo."""
    rows = _query(conn, "SELECT daemon_cpu_percent, daemon_rss_mb FROM monitor_health WHERE ts_ms >= ?", (since_ms,))
    if not rows:
        return {"cpu_avg": None, "cpu_max": None, "rss_avg": None, "rss_max": None, "samples": 0}
    cpu = values(rows, "daemon_cpu_percent")
    rss = values(rows, "daemon_rss_mb")
    return {
        "cpu_avg": mean(cpu) if cpu else None,
        "cpu_max": max(cpu) if cpu else None,
        "rss_avg": mean(rss) if rss else None,
        "rss_max": max(rss) if rss else None,
        "samples": len(rows),
    }


def overhead_series(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """Serie temporale grezza di CPU/RSS del demone (vedi overhead_summary per le sole medie/max)."""
    query = "SELECT ts_ms, daemon_cpu_percent, daemon_rss_mb FROM monitor_health WHERE ts_ms >= ? ORDER BY ts_ms"
    return list(_query(conn, query, (since_ms,)))


# Non e' un benchmark vero: e' una battuta. Il Guidance Computer dell'Apollo (~0.043 MHz
# effettivi, 2KB di RAM) e' un riferimento reale ma comicamente debole apposta — la battuta e'
# che un laptop del 2018 sotto stress "perde" comunque contro un computer del genere.
_NASA_STRESS_SCALE = 2.4


def nasa_comparison(cpu_percent: float | None, ram_percent: float | None, disk_latency_ms: float | None) -> dict:
    """Trasforma il carico attuale in una percentuale scherzosa 'peggio di un computer NASA'."""
    cpu = cpu_percent or 0.0
    ram = ram_percent or 0.0
    disk_component = min(50.0, (disk_latency_ms or 0.0) / 4)
    stress = cpu * 0.5 + ram * 0.4 + disk_component
    worse_percent = round(stress * _NASA_STRESS_SCALE)
    return {"worse_percent": worse_percent, "line": _nasa_line(worse_percent)}


def _nasa_line(worse_percent: int) -> str:
    if worse_percent <= 0:
        return "somehow your pc is beating a nasa computer right now. weird flex but ok"
    if worse_percent < 50:
        return f"ur pc is only {worse_percent}% worse than a nasa computer. not terrible"
    if worse_percent < 150:
        return f"ur pc is {worse_percent}% worse than a nasa computer. that sucks"
    return f"ur pc is {worse_percent}% worse than a nasa computer. apollo 11 called, it's concerned"
