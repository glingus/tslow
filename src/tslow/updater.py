"""Updater: controllo automatico delle release GitHub + download verificato (M9).

Il watcher controlla da solo (al massimo una volta ogni `[updates] check_interval_hours`) se esiste una
release piu' nuova e avvisa con una notifica. NON installa nulla da solo: l'installazione e' `tslow update`,
da un terminale amministratore, dopo conferma esplicita (vedi install.update). Il watcher gira elevato,
quindi codice nuovo non deve mai arrivare in Program Files senza un OK dell'utente.

Fiducia: il repository viene da `settings.toml` (scrivibile solo da un amministratore), mai dal DB. Gli
asset devono stare sotto `https://github.com/<repo>/releases/download/`; lo zip e' accettato solo se il suo
SHA-256 coincide con quello in `SHA256SUMS` della stessa release. Questo protegge da download corrotti o
troncati e da asset sostituiti a meta': NON protegge da un account GitHub compromesso (nessuna firma).
Solo libreria standard: nessuna dipendenza nuova.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable

from tslow import database as db
from tslow.config import Settings

logger = logging.getLogger(__name__)

PLACEHOLDER_REPO = "OWNER/tslow"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
API_URL = "https://api.github.com/repos/{repo}/releases/latest"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_USER_AGENT = "tslow-updater"

Version = tuple[int, int, int]


class UpdateError(RuntimeError):
    """Download o verifica falliti: nulla e' stato installato."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: Version
    zip_name: str
    zip_url: str
    sums_url: str
    page_url: str


def parse_version(text: str) -> Version | None:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Pre-release ('1.2.3-rc1'), 'latest' ecc. -> None."""
    m = _VERSION_RE.fullmatch((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def current_version() -> str:
    try:
        return metadata.version("tslow")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def configured_repo(settings: Settings) -> str | None:
    """Repository da cui aggiornare, o None se l'updater e' spento / non configurato (segnaposto)."""
    if settings.get("updates", "enabled", default=True) is not True:
        return None
    repo = settings.get("updates", "repository", default=PLACEHOLDER_REPO)
    if not isinstance(repo, str) or repo == PLACEHOLDER_REPO or not _REPO_RE.fullmatch(repo):
        return None
    return repo


def release_from_api(payload: object, repo: str) -> Release | None:
    """Estrae la release utilizzabile dalla risposta di `releases/latest`; None se non e' valida.

    Valida: non bozza, non pre-release, tag 'vX.Y.Z', asset `tslow-X.Y.Z.zip` e `SHA256SUMS` entrambi
    scaricabili da github.com/<repo>/releases/download/.
    """
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    version = parse_version(tag) if isinstance(tag, str) else None
    if version is None:
        return None
    zip_name = f"tslow-{'.'.join(map(str, version))}.zip"
    prefix = f"https://github.com/{repo}/releases/download/"
    urls: dict[str, str] = {}
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            url = asset.get("browser_download_url")
            if isinstance(url, str) and url.startswith(prefix):
                urls[asset["name"]] = url
    if zip_name not in urls or "SHA256SUMS" not in urls:
        return None
    page = payload.get("html_url")
    return Release(
        tag=tag,
        version=version,
        zip_name=zip_name,
        zip_url=urls[zip_name],
        sums_url=urls["SHA256SUMS"],
        page_url=page if isinstance(page, str) else f"https://github.com/{repo}/releases",
    )


def is_newer(release: Release, current: str) -> bool:
    cur = parse_version(current)
    return cur is not None and release.version > cur


def _open(url: str, timeout: float = 15.0):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}), timeout=timeout)


def fetch_latest(repo: str, opener: Callable = _open) -> Release | None:
    """Chiede a GitHub l'ultima release. Alza in caso di errore di rete (il chiamante decide cosa farne)."""
    with opener(API_URL.format(repo=repo)) as resp:
        payload = json.loads(resp.read(2_000_000))
    return release_from_api(payload, repo)


def parse_sha256sums(text: str) -> dict[str, str]:
    """Formato di `sha256sum`/`Get-FileHash`: '<hex64>  <nome>' (asterisco opzionale davanti al nome)."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*", line)
        if m:
            sums[m.group(2)] = m.group(1).lower()
    return sums


def download(url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES, opener: Callable = _open) -> str:
    """Scarica `url` in `dest` (tetto di dimensione) e ritorna lo SHA-256 esadecimale."""
    digest = hashlib.sha256()
    size = 0
    with opener(url, 60.0) as resp, dest.open("wb") as out:
        while chunk := resp.read(1 << 16):
            size += len(chunk)
            if size > max_bytes:
                raise UpdateError(f"download larger than {max_bytes} bytes: aborted")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def safe_extract(zip_path: Path, dest: Path) -> Path:
    """Estrae lo zip rifiutando percorsi assoluti o che escono da `dest`; ritorna la cartella con pyproject.toml."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if name.startswith(("/", "\\")) or not target.is_relative_to(dest):
                raise UpdateError(f"unsafe path in the archive: {name!r}")
        zf.extractall(dest)
    candidates = [dest] + [p for p in dest.iterdir() if p.is_dir()]
    for root in candidates:
        if (root / "pyproject.toml").is_file():
            return root
    raise UpdateError("the archive has no pyproject.toml: not a tslow release")


def download_release(release: Release, workdir: Path, opener: Callable = _open) -> Path:
    """Scarica e verifica la release in `workdir`; ritorna la radice del progetto estratto."""
    sums_path = workdir / "SHA256SUMS"
    download(release.sums_url, sums_path, max_bytes=1_000_000, opener=opener)
    expected = parse_sha256sums(sums_path.read_text(encoding="utf-8", errors="replace")).get(release.zip_name)
    if expected is None:
        raise UpdateError(f"SHA256SUMS has no entry for {release.zip_name}")
    zip_path = workdir / release.zip_name
    actual = download(release.zip_url, zip_path, opener=opener)
    if actual != expected:
        zip_path.unlink(missing_ok=True)
        raise UpdateError(f"SHA-256 mismatch for {release.zip_name}: expected {expected}, got {actual}")
    extracted = workdir / "src"
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.mkdir()
    return safe_extract(zip_path, extracted)


# --- controllo periodico (watcher) ---------------------------------------------------------


def check_for_update(
    conn, settings: Settings, now_ms: int, *, fetch: Callable[[str], Release | None] = fetch_latest, current: str | None = None
) -> Release | None:
    """Controllo a intervallo. Ritorna la release SOLO la prima volta che la vede (per notificare una volta).

    Stato in `settings` (tabella chiave/valore): `update_last_check_ms`, `update_available`, `update_notified`.
    Il DB e' solo cache/promemoria: `tslow update` riscarica sempre tutto da GitHub e non si fida di questi valori.
    """
    repo = configured_repo(settings)
    if repo is None:
        return None
    interval_ms = int(settings.get("updates", "check_interval_hours", default=24) * 3_600_000)
    last = db.get_setting(conn, "update_last_check_ms")
    if last is not None and now_ms - int(last) < interval_ms:
        return None
    db.set_setting(conn, "update_last_check_ms", str(now_ms))
    try:
        release = fetch(repo)
    except Exception as exc:  # rete assente, rate limit, JSON strano: riprova al prossimo giro
        logger.debug("update check failed: %s", exc)
        return None
    if release is None or not is_newer(release, current or current_version()):
        db.set_setting(conn, "update_available", "")
        return None
    db.set_setting(conn, "update_available", release.tag)
    if db.get_setting(conn, "update_notified") == release.tag:
        return None
    db.set_setting(conn, "update_notified", release.tag)
    return release
