"""Albero dei processi -> gruppi-app: stesso eseguibile risalendo l'albero dei processi.

Il gruppo e' identificato dal NOME del processo (economico: nessuna chiamata OpenProcess per
processo). L'arricchimento con il percorso completo dell'exe avviene solo sul colpevole
(detector.py/rules.py), mai per tutti i processi a ogni tick. Il PPID viene validato con il
create_time: un genitore "piu' giovane" del figlio significa che il PID e' stato riassegnato a un
processo non imparentato, e il collegamento viene ignorato.
"""

from __future__ import annotations

from tslow.collectors.processes import ProcessSnapshot


def _valid_ppid(pid: int, snapshots: dict[int, ProcessSnapshot]) -> int | None:
    snap = snapshots.get(pid)
    if snap is None or snap.ppid == 0:
        return None
    parent = snapshots.get(snap.ppid)
    if parent is None:
        return None
    if parent.create_time > snap.create_time:
        return None  # PID del genitore riassegnato dopo la creazione del figlio: link non valido
    return snap.ppid


def ancestor_names(pid: int, snapshots: dict[int, ProcessSnapshot], max_depth: int = 32) -> list[str]:
    """Nomi dei processi antenati dal genitore diretto verso la radice (per protection.classify)."""
    names: list[str] = []
    current = pid
    seen = {current}
    for _ in range(max_depth):
        parent_pid = _valid_ppid(current, snapshots)
        if parent_pid is None or parent_pid in seen:
            break
        names.append(snapshots[parent_pid].name)
        current = parent_pid
        seen.add(current)
    return names


def group_root_pid(pid: int, snapshots: dict[int, ProcessSnapshot], max_depth: int = 32) -> int:
    """Risale la catena dei genitori finche' il nome resta lo stesso; ritorna il PID piu' in alto trovato."""
    from tslow.protection import normalize_name

    current = pid
    name = normalize_name(snapshots[pid].name)
    seen = {current}
    for _ in range(max_depth):
        parent_pid = _valid_ppid(current, snapshots)
        if parent_pid is None or parent_pid in seen:
            break
        if normalize_name(snapshots[parent_pid].name) != name:
            break
        current = parent_pid
        seen.add(current)
    return current


def build_groups(snapshots: dict[int, ProcessSnapshot]) -> dict[str, list[int]]:
    """Raggruppa i PID per app: chiave = nome normalizzato del processo radice del gruppo."""
    from tslow.protection import normalize_name

    groups: dict[str, list[int]] = {}
    for pid in snapshots:
        root_pid = group_root_pid(pid, snapshots)
        key = normalize_name(snapshots[root_pid].name)
        groups.setdefault(key, []).append(pid)
    return groups
