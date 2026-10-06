"""Caricamento di config/settings.toml (tomllib, stdlib Python 3.12)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from tslow.paths import config_path


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


_LEGACY_MARKERS = (("notifiche",), ("azioni",), ("thresholds", "disco"), ("thresholds", "rete"), ("sampler", "calmo_tick"))


def legacy_keys(settings: Settings) -> list[str]:
    """Chiavi italiane di prima della M8 ancora presenti (un settings.toml vecchio le ignora in silenzio)."""
    return [".".join(m) for m in _LEGACY_MARKERS if settings.get(*m) is not None]


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    path = config_path()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(raw=raw)
