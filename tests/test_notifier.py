"""should_notify(): calibrazione, protezione L1, esenzione primo piano, cooldown, limiti orari."""

from __future__ import annotations

from pathlib import Path

import pytest

from tslow import database as db
from tslow import notifier
from tslow.config import load_settings
from tslow.protection import ProtectionLevel


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _make_incident(conn, **overrides) -> db.Incident:
    defaults = dict(
        opened_at_ms=10_000,
        resource="cpu",
        incident_type="anomaly",
        severity="anomaly",
        group_key="chrome",
        proposed_action="soft",
        protection_level=int(ProtectionLevel.L3_NORMAL),
    )
    defaults.update(overrides)
    incident_id = db.create_incident(conn, **defaults)
    return db.get_incident(conn, incident_id)


def test_blocked_during_calibration_for_non_critical(conn) -> None:
    settings = load_settings()
    incident = _make_incident(conn, severity="anomaly")
    ok, reason = notifier.should_notify(conn, incident, settings, now_ms=10_000)
    assert ok is False
    assert "calibrazione" in reason


def test_critico_bypasses_calibration(conn) -> None:
    settings = load_settings()
    incident = _make_incident(conn, severity="critical")
    ok, _ = notifier.should_notify(conn, incident, settings, now_ms=10_000)
    assert ok is True


def test_l1_anomalia_suppressed_even_after_calibration(conn) -> None:
    settings = load_settings()
    # forza la fine della calibrazione impostando l'inizio molto nel passato
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="anomaly", protection_level=int(ProtectionLevel.L1_FREE_REIN))
    ok, reason = notifier.should_notify(conn, incident, settings, now_ms=10_000_000_000)
    assert ok is False
    assert "L1" in reason


def test_l1_critico_still_notifies(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="critical", protection_level=int(ProtectionLevel.L1_FREE_REIN))
    ok, _ = notifier.should_notify(conn, incident, settings, now_ms=10_000_000_000)
    assert ok is True


def test_foreground_exemption_for_cpu_anomalia(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="anomaly", resource="cpu")
    ok, reason = notifier.should_notify(conn, incident, settings, now_ms=10_000_000_000, is_foreground=True)
    assert ok is False
    assert "primo piano" in reason


def test_foreground_does_not_exempt_ram(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="anomaly", resource="ram")
    ok, _ = notifier.should_notify(conn, incident, settings, now_ms=10_000_000_000, is_foreground=True)
    assert ok is True


def test_cooldown_blocks_repeated_notification(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="critical")
    db.record_notification(conn, incident.id, sent_at_ms=10_000_000_000, channel="toast", success=True)
    ok, reason = notifier.should_notify(conn, incident, settings, now_ms=10_000_000_000 + 60_000)  # 1 min dopo
    assert ok is False
    assert "cooldown" in reason


def test_cooldown_expires_after_window(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    incident = _make_incident(conn, severity="critical")
    db.record_notification(conn, incident.id, sent_at_ms=10_000_000_000, channel="toast", success=True)
    sixteen_min_later = 10_000_000_000 + 16 * 60_000
    ok, _ = notifier.should_notify(conn, incident, settings, now_ms=sixteen_min_later)
    assert ok is True


def test_hourly_rate_limit(conn) -> None:
    settings = load_settings()
    db.set_setting(conn, "calibration_started_at_ms", "0")
    now_ms = 10_000_000_000
    limit = settings.get("notifications", "hourly_limit_total", default=4)
    for i in range(limit):
        inc = _make_incident(conn, severity="critical", group_key=f"app{i}")
        db.record_notification(conn, inc.id, sent_at_ms=now_ms, channel="toast", success=True)

    new_incident = _make_incident(conn, severity="critical", group_key="overflow")
    ok, reason = notifier.should_notify(conn, new_incident, settings, now_ms=now_ms + 1000)
    assert ok is False
    assert "orario" in reason
