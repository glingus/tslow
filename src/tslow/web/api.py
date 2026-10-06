"""Backend FastAPI di M6: cruscotto storico sopra `analytics.py`, piu' un canale per le
decisioni [1]/[2]/[3] (M6.1) che scrive nel DB esattamente come `cli/resolve.py`.

Ogni richiesta apre e chiude la propria connessione SQLite (stessa politica della CLI e della
dashboard TUI): nessuno stato condiviso. Il comando `tslow web` fa il bind di default su
127.0.0.1: non c'e' autenticazione, quindi questo processo non deve MAI ascoltare su
un'interfaccia diversa da localhost (vedi ADR in Architettura.md). Anche l'endpoint di
decisione NON esegue mai nulla sui processi: scrive in `decisions` con la stessa
`rules.apply_decision`/`db.create_decision` gia' usata da `tslow resolve`, e' il demone
elevato (`optimizer.apply()`) a rivalidare tutto (protezione, identita', dry-run) e ad
agire — vedi Modello di sicurezza in Architettura.md. Il canale resta unico: DB SQLite.
"""

from __future__ import annotations

import math
import sqlite3
import time
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tslow import analytics
from tslow import database as db
from tslow import paths
from tslow import rules
from tslow.config import load_settings
from tslow.protection import ProtectionLevel

# Metriche mostrate dal cruscotto, nello stesso ordine di cli/dashboard.py::_METRICS.
METRICS: dict[str, str] = {
    "cpu": "cpu_percent",
    "ram": "ram_percent",
    "disk": "disk_latency_ms",
    "network": "net_rx_bytes_sec",
    "gpu": "gpu_percent",
}
_MA_WINDOWS_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000}

app = FastAPI(title="why ts so slow?", docs_url="/api/docs", openapi_url="/api/openapi.json")

# Necessario solo per lo sviluppo del frontend (Vite su una porta diversa da quella dell'API);
# a chi non gira su localhost non arriva comunque nulla, dato che il bind e' su 127.0.0.1.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_connection() -> Iterator[sqlite3.Connection]:
    conn = db.connect(check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _range_param(range_: str) -> str:
    if range_ not in analytics.RANGES:
        raise HTTPException(status_code=400, detail=f"invalid range: {range_!r} (valid: {', '.join(analytics.RANGES)})")
    return range_


def _json_safe(value: Any) -> Any:
    """Float non finiti -> null; float arrotondati a 10 decimali."""
    if isinstance(value, float):
        return round(value, 10) if math.isfinite(value) else None
    return value


def _records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _json_safe(v) for k, v in row.items() if k != "ts"} for row in rows]


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/metrics")
def get_metrics(range_: str = Query("24h", alias="range"), conn: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Serie storica delle 5 metriche principali (con medie mobili 15m/1h) piu' la pendenza oraria di ciascuna."""
    range_ = _range_param(range_)
    now_ms = int(time.time() * 1000)
    rows = analytics.load_metrics(conn, range_, now_ms)

    trends: dict[str, float | None] = {}
    for key, base in METRICS.items():
        resolved = analytics.metric_column(rows, base)
        if resolved is None:
            trends[key] = None
            continue
        analytics.with_moving_averages(rows, resolved, _MA_WINDOWS_MS)
        trends[key] = _json_safe(analytics.trend(rows, resolved))

    columns = {key: analytics.metric_column(rows, base) for key, base in METRICS.items()}
    return {"range": range_, "columns": columns, "trend": trends, "samples": _records(rows)}


@app.get("/api/top-culprits")
def get_top_culprits(
    range_: str = Query("24h", alias="range"), limit: int = 10, conn: sqlite3.Connection = Depends(get_connection)
) -> list[dict[str, Any]]:
    range_ = _range_param(range_)
    since_ms = analytics.range_since_ms(range_, int(time.time() * 1000))
    return _records(analytics.top_culprits(conn, since_ms, limit=limit))


@app.get("/api/decisions/stats")
def get_decision_stats(range_: str = Query("24h", alias="range"), conn: sqlite3.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    range_ = _range_param(range_)
    since_ms = analytics.range_since_ms(range_, int(time.time() * 1000))
    return _records(analytics.decision_stats(conn, since_ms))


@app.get("/api/incidents")
def get_incidents(
    range_: str = Query("24h", alias="range"), limit: int = 50, conn: sqlite3.Connection = Depends(get_connection)
) -> list[dict[str, Any]]:
    range_ = _range_param(range_)
    since_ms = analytics.range_since_ms(range_, int(time.time() * 1000))
    return _records(analytics.incident_history(conn, since_ms, limit=limit))


@app.get("/api/overhead")
def get_overhead(range_: str = Query("24h", alias="range"), conn: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
    range_ = _range_param(range_)
    since_ms = analytics.range_since_ms(range_, int(time.time() * 1000))
    return {k: _json_safe(v) for k, v in analytics.overhead_summary(conn, since_ms).items()}


@app.get("/api/overhead/series")
def get_overhead_series(range_: str = Query("24h", alias="range"), conn: sqlite3.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    range_ = _range_param(range_)
    since_ms = analytics.range_since_ms(range_, int(time.time() * 1000))
    return _records(analytics.overhead_series(conn, since_ms))


class DecisionRequest(BaseModel):
    choice: Literal["deny", "allow_once", "allow_always"]


@app.post("/api/incidents/{incident_id}/decision")
def post_incident_decision(
    incident_id: int, body: DecisionRequest, conn: sqlite3.Connection = Depends(get_connection)
) -> dict[str, Any]:
    """Stessa validazione e le stesse scritture di `cli/resolve.py::show_and_resolve`, solo
    senza il prompt interattivo. Il demone la rilegge dalla tabella `decisions` (poll ad ogni
    tick) e la esegue solo dopo aver rivalidato protezione e identita' del processo."""
    incident = db.get_incident(conn, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    if incident.status != "open":
        raise HTTPException(status_code=409, detail=f"incident is already '{incident.status}': nothing to do")

    is_l0 = incident.protection_level == int(ProtectionLevel.L0_UNTOUCHABLE)
    is_system = incident.group_key is None
    if is_l0 or is_system:
        raise HTTPException(status_code=409, detail="informational only: protected process or system event")

    now_ms = int(time.time() * 1000)
    settings = load_settings()
    identity = rules.app_identity(incident.exe_path, incident.group_key)
    rules.apply_decision(conn, settings, identity, body.choice, now_ms)
    decision_id = db.create_decision(conn, incident.id, body.choice, now_ms=now_ms)
    return {"decision_id": decision_id, "execution_status": "pending"}


@app.get("/api/decisions/{decision_id}")
def get_decision_status(decision_id: int, conn: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
    decision = db.get_decision(conn, decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return {"id": decision.id, "execution_status": decision.execution_status, "executed_at_ms": decision.executed_at_ms}


@app.get("/api/nasa")
def get_nasa_comparison(conn: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Not a real benchmark — see analytics.nasa_comparison. Uses the latest raw sample."""
    row = conn.execute(
        "SELECT cpu_percent, ram_percent, disk_latency_ms FROM metrics_raw ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"worse_percent": 0, "line": "no data yet — too early to embarrass this pc"}
    return analytics.nasa_comparison(row["cpu_percent"], row["ram_percent"], row["disk_latency_ms"])


# Registrato per ultimo, dopo ogni /api/*: Starlette prova le route nell'ordine in cui sono
# state dichiarate, quindi questo mount su "/" (che altrimenti intercetterebbe tutto) non
# le puo' mai adombrare. Nessuna route SPA di fallback: il frontend e' una pagina sola,
# senza routing lato client.
_dist_dir = paths.web_dist_dir()
if _dist_dir is not None:
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="frontend")
else:

    @app.get("/", response_class=HTMLResponse)
    def frontend_not_built() -> str:
        return (
            "<pre>Frontend not built yet.\n"
            "From web/: npm install &amp;&amp; npm run build\n"
            "The API is still available at /api/* (schema: /api/docs).</pre>"
        )
