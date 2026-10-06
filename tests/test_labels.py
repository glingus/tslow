"""labels.py: le mappe di visualizzazione inglesi per i valori enum italiani nel DB."""

from __future__ import annotations

from tslow import labels


def test_label_maps_known_value() -> None:
    assert labels.label(labels.RESOURCE_LABELS, "disk") == "disk"
    assert labels.label(labels.STATUS_LABELS, "open") == "open"
    assert labels.label(labels.SAMPLER_STATE_LABELS, "idle") == "idle"


def test_label_passes_through_unknown_value() -> None:
    assert labels.label(labels.RESOURCE_LABELS, "mystery") == "mystery"


def test_label_none_is_not_available() -> None:
    assert labels.label(labels.STATUS_LABELS, None) == "n/a"
