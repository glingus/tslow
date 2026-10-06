"""Livelli di protezione dei processi: L0 (intoccabile) -> L3 (normale).

I processi di sistema Windows (L0) e di manutenzione (L2) sono hardcoded, MAI configurabili. Le
*applicazioni* protette dall'utente arrivano solo dalla sezione [protection] di settings.toml (in
Program Files: scrivibile solo da un amministratore), mai dal DB; la configurazione puo' solo
AGGIUNGERE protezione. Il controllo avviene in tre punti: il detector (proposta), la CLI
(visualizzazione) e soprattutto optimizer.apply() (M3), unico punto che esegue azioni.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum

# --- L0: intoccabile -------------------------------------------------------------
# Processi core di Windows. Se uno di questi nomi gira da un percorso fuori da
# %SystemRoot%, resta comunque L0 ma viene marcato "sospetto" (probabile malware in
# maschera): vedi classify().
_L0_SYSTEM_NAMES = frozenset(
    {
        "system",
        "registry",
        "secure system",
        "memory compression",
        "smss",
        "csrss",
        "wininit",
        "winlogon",
        "services",
        "lsass",
        "lsaiso",
        "svchost",
        "dwm",
        "fontdrvhost",
        "sihost",
        "taskhostw",
        "ctfmon",
        "runtimebroker",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "searchhost",
        "textinputhost",
        "applicationframehost",
        "dllhost",
        "conhost",
        "audiodg",
        "spoolsv",
        "wudfhost",
        "wmiprvse",
        "smartscreen",
        "securityhealthservice",
        "msmpeng",
        "nissrv",
        "lockapp",
        "logonui",
        "explorer",
    }
)

# --- L2: solo soft ------------------------------------------------------------------
# Terminarli puo' corrompere aggiornamenti, installazioni o macchine virtuali: mai kill, solo
# priorita' bassa.
_L2_NAMES = frozenset(
    {
        "searchindexer",
        "searchprotocolhost",
        "tiworker",
        "trustedinstaller",
        "msiexec",
        "mousocoreworker",
        "compattelrunner",
        "vmmem",
        "vmmemwsl",
    }
)


class ProtectionLevel(IntEnum):
    L0_UNTOUCHABLE = 0
    L1_FREE_REIN = 1
    L2_SOFT_ONLY = 2
    L3_NORMAL = 3


@dataclass(frozen=True)
class ProtectionResult:
    level: ProtectionLevel
    suspicious: bool = False
    reason: str = ""


def normalize_name(name: str) -> str:
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def is_own_process(pid: int) -> bool:
    """Il demone TSlow stesso non e' mai un bersaglio, indipendentemente dal nome del suo eseguibile.

    Va controllato SEMPRE prima di classify(): il demone gira come pythonw.exe/python.exe, un nome
    generico che nessuna regola di classify() puo' riconoscere in modo affidabile.
    """
    return pid == os.getpid()


def _under_system_root(exe_path: str) -> bool:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    try:
        root = os.path.normcase(os.path.normpath(system_root))
        path = os.path.normcase(os.path.normpath(exe_path))
        return path.startswith(root)
    except (TypeError, ValueError):
        return False


class ProtectionConfigError(ValueError):
    """La sezione [protection] di settings.toml non e' valida."""


@dataclass(frozen=True)
class UserProtection:
    """Applicazioni protette dall'utente. Nomi normalizzati con normalize_name()."""

    untouchable: frozenset[str] = frozenset()
    free_rein_parents: frozenset[str] = frozenset()
    untouchable_children: frozenset[tuple[str, str]] = frozenset()  # (figlio, antenato)


def _string_list(section: dict, key: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProtectionConfigError(f"[protection] {key}: expected a list of strings")
    return value


def parse_user_protection(section: object) -> UserProtection:
    """Valida e normalizza la sezione [protection]. Sezione assente (None) -> nessuna protezione extra."""
    if section is None:
        return UserProtection()
    if not isinstance(section, dict):
        raise ProtectionConfigError("[protection] must be a table")
    pairs = section.get("untouchable_children", [])
    if not isinstance(pairs, list) or not all(
        isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in pairs
    ):
        raise ProtectionConfigError("[protection] untouchable_children: expected a list of [child, ancestor] pairs")
    return UserProtection(
        untouchable=frozenset(normalize_name(n) for n in _string_list(section, "untouchable")),
        free_rein_parents=frozenset(normalize_name(n) for n in _string_list(section, "free_rein_parents")),
        untouchable_children=frozenset((normalize_name(c), normalize_name(a)) for c, a in pairs),
    )


_user_protection: UserProtection | None = None


def get_user_protection() -> UserProtection:
    """Carica (una sola volta, poi in cache) la sezione [protection] di settings.toml."""
    global _user_protection
    if _user_protection is None:
        from tslow.config import load_settings

        _user_protection = parse_user_protection(load_settings().raw.get("protection"))
    return _user_protection


def set_user_protection(user: UserProtection | None) -> UserProtection | None:
    """Imposta (o, con None, azzera la cache di) la protezione utente. Ritorna il valore precedente."""
    global _user_protection
    previous = _user_protection
    _user_protection = user
    return previous


def classify(
    name: str, exe_path: str | None, ancestor_names: list[str], user: UserProtection | None = None
) -> ProtectionResult:
    """Classifica un processo. `ancestor_names`: dal genitore diretto verso la radice.

    Ordine: sistema L0 -> app L0 dell'utente -> figli L0 dell'utente -> L2 hardcoded -> L1 dell'utente -> L3.
    L2 viene prima dell'L1 utente di proposito: la configurazione puo' solo aggiungere protezione.
    """
    if user is None:
        user = get_user_protection()
    n = normalize_name(name)
    ancestors = [normalize_name(a) for a in ancestor_names]

    if n in _L0_SYSTEM_NAMES:
        suspicious = bool(exe_path) and not _under_system_root(exe_path)
        return ProtectionResult(ProtectionLevel.L0_UNTOUCHABLE, suspicious=suspicious, reason="processo di sistema")

    if n in user.untouchable:
        return ProtectionResult(ProtectionLevel.L0_UNTOUCHABLE, reason=f"applicazione protetta ({n})")

    for child, ancestor in user.untouchable_children:
        if n == child and ancestor in ancestors:
            return ProtectionResult(ProtectionLevel.L0_UNTOUCHABLE, reason=f"{n} figlio di {ancestor}")

    if n in _L2_NAMES:
        return ProtectionResult(ProtectionLevel.L2_SOFT_ONLY, reason="manutenzione di sistema")

    if any(a in user.free_rein_parents for a in ancestors):
        return ProtectionResult(ProtectionLevel.L1_FREE_REIN, reason="discendente di un'applicazione protetta")

    return ProtectionResult(ProtectionLevel.L3_NORMAL)
