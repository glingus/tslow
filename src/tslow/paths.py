"""Percorsi condivisi del progetto: workspace di sviluppo vs installazione in Program Files."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# Radice fissa dell'installazione (M5, install.py): il pacchetto finisce annidato dentro
# runtime\Lib\site-packages\tslow, quindi risalire da PACKAGE_DIR coi ".parent" non e'
# affidabile — la radice va conosciuta a priori, non dedotta dalla profondita' del pacchetto.
INSTALL_DIR = Path(r"C:\Program Files\TSlow")


def _is_installed() -> bool:
    """True se il runtime gira dalla copia in Program Files (M5)."""
    return "Program Files" in str(PACKAGE_DIR)


def _installed_root() -> Path:
    """Radice di dati e log della copia installata: %LOCALAPPDATA%/TSlow (scrivibile dall'utente)."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "TSlow"


def data_dir() -> Path:
    if _is_installed():
        base = _installed_root() / "data"
    else:
        override = os.environ.get("TSLOW_DATA_DIR")  # solo sviluppo: l'installata lo ignora
        base = Path(override) if override else PACKAGE_DIR.parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    if _is_installed():
        base = _installed_root() / "logs"
    else:
        base = PACKAGE_DIR.parent.parent / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    if _is_installed():
        return INSTALL_DIR / "settings.toml"
    return PACKAGE_DIR.parent.parent / "config" / "settings.toml"


def db_path() -> Path:
    return data_dir() / "metrics.db"


def web_dist_dir() -> Path | None:
    """Cartella della build del frontend (M6, `npm run build` in `web/`), se esiste.

    Installata: `install.py` la copia sotto INSTALL_DIR (vedi `_copy_web_dist`). In sviluppo
    e' la build accanto al workspace. None se non e' mai stata compilata: `tslow web` serve
    comunque le API, con un messaggio invece della pagina.
    """
    base = INSTALL_DIR / "web" / "dist" if _is_installed() else PACKAGE_DIR.parent.parent / "web" / "dist"
    return base if (base / "index.html").is_file() else None
