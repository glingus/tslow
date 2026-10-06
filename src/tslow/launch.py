"""Handler del protocollo `tslow://` (registrato in HKCU da install.py, M5).

Il click sul toast passa dalla shell, quindi arriva qui NON elevato: si interpreta l'URI
(`tslow://resolve?incident=N`) e si apre `tslow resolve --incident N` in un nuovo terminale, mai
nello stesso processo. Il profilo Windows Terminal "TSlow" (creato da install.py in M5) viene
usato se disponibile; altrimenti si apre una console qualunque.
"""

from __future__ import annotations

import subprocess
import sys
from urllib.parse import parse_qs, urlparse

_CREATE_NEW_CONSOLE = 0x00000010  # subprocess.CREATE_NEW_CONSOLE, solo su Windows


def parse_incident_id(uri: str) -> int | None:
    """Estrae l'ID incidente da un URI `tslow://resolve?incident=N`. None se assente o malformato."""
    parsed = urlparse(uri)
    if parsed.scheme != "tslow":
        return None
    values = parse_qs(parsed.query).get("incident")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def open_resolve_window(incident_id: int | None) -> None:
    args = [sys.executable, "-m", "tslow", "resolve"]
    if incident_id is not None:
        args += ["--incident", str(incident_id)]

    try:
        subprocess.Popen(["wt.exe", "-p", "TSlow", "--"] + args, creationflags=_CREATE_NEW_CONSOLE)
    except (FileNotFoundError, OSError):
        subprocess.Popen(args, creationflags=_CREATE_NEW_CONSOLE)


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    uri = args[0] if args else ""
    open_resolve_window(parse_incident_id(uri))


if __name__ == "__main__":
    main()
