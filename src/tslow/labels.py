"""Display labels for the Italian words stored as DB enum values.

The stored values (`incidents.resource`, `.status`, `.severity`, `.proposed_action`,
`metrics_raw.sampler_state`) are historical and touching them means a real migration across
76k+ production rows and every `==` comparison in detector.py/optimizer.py/monitor.py/rules.py.
Not worth the risk just to relabel text — instead, every place that shows one of these values
to a person (CLI, web) goes through here, same pattern already used for `decisions.choice`
(stored `deny`/`allow_once`/`allow_always`, shown via a label map) in cli/resolve.py.
"""

from __future__ import annotations

RESOURCE_LABELS: dict[str, str] = {
    "cpu": "CPU",
    "ram": "RAM",
    "disk": "disk",
    "network": "network",
    "gpu": "GPU",
    "system": "system",
    "app": "app",
}

INCIDENT_TYPE_LABELS: dict[str, str] = {
    "anomaly": "anomaly",
    "critical": "critical",
    "leak": "leak",
    "hung": "hung",
    "throttling": "throttling",
}

SEVERITY_LABELS: dict[str, str] = {
    "anomaly": "anomaly",
    "critical": "critical",
}

STATUS_LABELS: dict[str, str] = {
    "open": "open",
    "resolved": "resolved",
    "expired": "expired",
    "ignored": "ignored",
}

PROPOSED_ACTION_LABELS: dict[str, str] = {
    "soft": "soft",
    "hard": "hard",
    "none": "none",
}

SAMPLER_STATE_LABELS: dict[str, str] = {
    "idle": "idle",
    "watching": "watching",
    "investigating": "investigating",
}


def label(mapping: dict[str, str], value: str | None) -> str:
    """`mapping.get(value, value)` with a `None` passthrough, so a future unmapped value
    still shows something instead of crashing."""
    if value is None:
        return "n/a"
    return mapping.get(value, value)
