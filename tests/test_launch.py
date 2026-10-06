"""launch.py: parsing dell'URI tslow:// (la parte pura e testabile senza aprire finestre)."""

from __future__ import annotations

from tslow.launch import parse_incident_id


def test_parses_incident_id() -> None:
    assert parse_incident_id("tslow://resolve?incident=42") == 42


def test_returns_none_for_wrong_scheme() -> None:
    assert parse_incident_id("http://resolve?incident=42") is None


def test_returns_none_when_missing() -> None:
    assert parse_incident_id("tslow://resolve") is None


def test_returns_none_for_malformed_value() -> None:
    assert parse_incident_id("tslow://resolve?incident=abc") is None
