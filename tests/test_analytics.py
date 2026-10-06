"""analytics.py su dati sintetici: scelta della tabella, medie mobili, trend, aggregazioni."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import analytics
from tslow import database as db


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_load_metrics_picks_table_by_range(conn) -> None:
    now_ms = 10_000_000_000
    db.insert_metrics_raw(conn, [db.MetricSample(ts_ms=now_ms - 1000, sampler_state="idle", period_ms=5000, cpu_percent=10.0)])
    rows_1h = analytics.load_metrics(conn, "1h", now_ms)
    assert len(rows_1h) == 1
    rows_24h = analytics.load_metrics(conn, "24h", now_ms)  # legge metrics_1m, ancora vuota
    assert not rows_24h
    assert "cpu_percent_avg" in rows_24h.columns and "ts_ms" in rows_24h.columns  # colonne note anche a vuoto


def test_load_metrics_rejects_unknown_range(conn) -> None:
    with pytest.raises(ValueError):
        analytics.load_metrics(conn, "9999y", 0)


def test_with_moving_averages_is_a_time_window_mean_that_skips_none(conn) -> None:
    rows = [
        {"ts_ms": 0, "v": 10.0},
        {"ts_ms": 60_000, "v": None},
        {"ts_ms": 120_000, "v": 30.0},
        {"ts_ms": 600_000, "v": 50.0},
    ]
    out = analytics.with_moving_averages(rows, "v", {"3m": 180_000})
    # finestra (t - 3m, t]: il campione a t-3m esatto esce, i None non contano
    assert [r["v_ma_3m"] for r in out] == [10.0, 10.0, 20.0, 50.0]
    assert out is rows


def test_with_moving_averages_none_when_window_has_no_values() -> None:
    rows = [{"ts_ms": 0, "v": None}, {"ts_ms": 1000, "v": 4.0}]
    out = analytics.with_moving_averages(rows, "v", {"1m": 60_000})
    assert [r["v_ma_1m"] for r in out] == [None, 4.0]


def test_with_moving_averages_ignores_missing_column() -> None:
    rows = [{"ts_ms": 0, "v": 1.0}]
    assert analytics.with_moving_averages(rows, "nope", {"1m": 60_000}) == [{"ts_ms": 0, "v": 1.0}]
    assert analytics.with_moving_averages([], "v", {"1m": 60_000}) == []


def test_trend_detects_positive_slope() -> None:
    rows = [{"ts_ms": hour * 3_600_000, "cpu_percent": float(hour * 20)} for hour in range(5)]
    assert analytics.trend(rows, "cpu_percent") == pytest.approx(20.0)


def test_trend_skips_none_values() -> None:
    rows = [
        {"ts_ms": 0, "v": None},
        {"ts_ms": 3_600_000, "v": 10.0},
        {"ts_ms": 7_200_000, "v": 20.0},
    ]
    assert analytics.trend(rows, "v") == pytest.approx(10.0)


def test_trend_none_when_not_enough_data() -> None:
    assert analytics.trend([], "cpu_percent") is None
    assert analytics.trend([{"ts_ms": 0, "cpu_percent": 1.0}], "cpu_percent") is None
    assert analytics.trend([{"ts_ms": 0, "v": None}, {"ts_ms": 1, "v": 2.0}], "v") is None


def test_top_culprits_orders_by_incident_count(conn) -> None:
    now_ms = 10_000_000_000
    for _ in range(3):
        db.create_incident(conn, opened_at_ms=now_ms, resource="cpu", incident_type="anomaly", severity="anomaly", group_key="chrome", proposed_action="soft")
    db.create_incident(conn, opened_at_ms=now_ms, resource="ram", incident_type="critical", severity="critical", group_key="code", proposed_action="hard")

    top = analytics.top_culprits(conn, since_ms=0)
    assert top[0]["group_key"] == "chrome"
    assert top[0]["incidenti"] == 3


def test_decision_stats_groups_by_choice_and_source(conn) -> None:
    now_ms = 10_000_000_000
    incident_id = db.create_incident(conn, opened_at_ms=now_ms, resource="cpu", incident_type="anomaly", severity="anomaly", group_key="chrome", proposed_action="soft")
    db.create_decision(conn, incident_id, "allow_once", source="user", now_ms=now_ms)
    db.create_decision(conn, incident_id, "allow_once", source="auto", now_ms=now_ms)

    stats = analytics.decision_stats(conn, since_ms=0)
    assert len(stats) == 2


def test_overhead_summary_computes_avg_and_max(conn) -> None:
    now_ms = 10_000_000_000
    db.record_monitor_health(conn, now_ms, daemon_cpu_percent=0.1, daemon_rss_mb=40.0, sampler_state="idle", period_multiplier=1.0)
    db.record_monitor_health(conn, now_ms + 60_000, daemon_cpu_percent=0.3, daemon_rss_mb=50.0, sampler_state="idle", period_multiplier=1.0)

    summary = analytics.overhead_summary(conn, since_ms=0)
    assert summary["cpu_avg"] == pytest.approx(0.2)
    assert summary["cpu_max"] == pytest.approx(0.3)
    assert summary["rss_max"] == pytest.approx(50.0)
    assert summary["samples"] == 2


def test_overhead_summary_empty(conn) -> None:
    summary = analytics.overhead_summary(conn, since_ms=0)
    assert summary["samples"] == 0
    assert summary["cpu_avg"] is None


def test_range_since_ms_subtracts_range_width() -> None:
    assert analytics.range_since_ms("1h", 10_000_000) == 10_000_000 - 3_600_000


def test_range_since_ms_rejects_unknown_range() -> None:
    with pytest.raises(ValueError):
        analytics.range_since_ms("9999y", 0)


def test_overhead_series_returns_ordered_samples(conn) -> None:
    now_ms = 10_000_000_000
    db.record_monitor_health(conn, now_ms + 60_000, daemon_cpu_percent=0.3, daemon_rss_mb=50.0, sampler_state="idle", period_multiplier=1.0)
    db.record_monitor_health(conn, now_ms, daemon_cpu_percent=0.1, daemon_rss_mb=40.0, sampler_state="idle", period_multiplier=1.0)

    series = analytics.overhead_series(conn, since_ms=0)
    assert [r["daemon_cpu_percent"] for r in series] == [0.1, 0.3]
    assert [r["ts_ms"] for r in series] == [now_ms, now_ms + 60_000]


def test_overhead_series_empty(conn) -> None:
    assert analytics.overhead_series(conn, since_ms=0) == []


def test_incident_history_includes_fields_needed_for_web_actions(conn) -> None:
    # La dashboard web decide se un incidente e' azionabile da questi campi (vedi
    # web/api.py::post_incident_decision): senza protection_level non potrebbe distinguere
    # un incidente L0 (solo informativo) da uno su cui proporre [1]/[2]/[3].
    now_ms = 10_000_000_000
    db.create_incident(
        conn, opened_at_ms=now_ms, resource="cpu", incident_type="critical", severity="critical",
        group_key="chrome", proposed_action="hard", protection_level=3, quota_percent=42.5,
    )
    history = analytics.incident_history(conn, since_ms=0)
    assert history[0]["protection_level"] == 3
    assert history[0]["proposed_action"] == "hard"
    assert history[0]["quota_percent"] == pytest.approx(42.5)


def test_metric_column_prefers_direct_name() -> None:
    rows = analytics.MetricRows([{"cpu_percent": 1.0, "cpu_percent_avg": 2.0}], ["cpu_percent", "cpu_percent_avg"])
    assert analytics.metric_column(rows, "cpu_percent") == "cpu_percent"


def test_metric_column_falls_back_to_rollup_suffix(conn) -> None:
    # metrics_1m (24h/7d/30d) espone solo <nome>_avg/_max, mai il nome diretto: bug reale
    # trovato dal vivo che rendeva "n/d" ogni indicatore della dashboard fuori dall'intervallo 1h.
    now_ms = 10_000_000_000
    db.insert_metrics_raw(conn, [db.MetricSample(ts_ms=now_ms - 120_000, sampler_state="idle", period_ms=5000, cpu_percent=42.0)])
    db.rollup_1m(conn, now_ms)
    rows = analytics.load_metrics(conn, "24h", now_ms)
    assert "cpu_percent" not in rows.columns
    resolved = analytics.metric_column(rows, "cpu_percent")
    assert resolved == "cpu_percent_avg"
    assert rows[0][resolved] == pytest.approx(42.0)


def test_metric_column_none_when_absent(conn) -> None:
    rows = analytics.load_metrics(conn, "1h", 10_000_000_000)
    assert analytics.metric_column(rows, "non_esiste") is None


def test_nasa_comparison_zero_load() -> None:
    result = analytics.nasa_comparison(0.0, 0.0, 0.0)
    assert result["worse_percent"] == 0
    assert "nasa" in result["line"]


def test_nasa_comparison_scales_with_load() -> None:
    low = analytics.nasa_comparison(10.0, 10.0, 0.0)
    high = analytics.nasa_comparison(95.0, 95.0, 200.0)
    assert high["worse_percent"] > low["worse_percent"]


def test_nasa_comparison_handles_missing_values() -> None:
    result = analytics.nasa_comparison(None, None, None)
    assert result["worse_percent"] == 0
