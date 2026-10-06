"""Apprendimento dalle decisioni dell'utente: [1] nega / [2] consenti una volta / [3] consenti sempre.

Una riga per app in `user_rules`, chiave `app_identity` (percorso dell'exe se leggibile, altrimenti
nome normalizzato). Regole dal piano:
- **[1] Nega**: vale una volta; `consecutive_denies += 1`. A quota 3 la regola diventa
  `ignore_learned` per 30 giorni; il livello critico la scavalca comunque (va controllato dal
  chiamante PRIMA di usare `is_ignored`, mai qui dentro).
- **[2] Consenti una volta**: esegue l'azione, `allow_once_count += 1`, azzera i dinieghi consecutivi.
- **[3] Consenti sempre**: esegue l'azione e salva `max_auto_action='soft'`. I prossimi incidenti
  applicano Soft da soli; Hard richiede sempre il prompt (mai automatico).
"""

from __future__ import annotations

from tslow import database as db
from tslow.config import Settings
from tslow.protection import normalize_name


def app_identity(exe_path: str | None, name: str) -> str:
    """Percorso dell'exe se leggibile, altrimenti nome normalizzato."""
    return exe_path if exe_path else normalize_name(name)


def is_ignored(conn, identity: str, now_ms: int) -> bool:
    """True se l'app ha 'ignora appresa' attiva e non scaduta.

    Il livello critico scavalca SEMPRE questa regola: il chiamante deve controllare la severita'
    dell'incidente prima di usare questo risultato, non e' compito di questa funzione.
    """
    rule = db.get_user_rule(conn, identity)
    if rule is None or rule.status != "active" or rule.source != "ignore_learned":
        return False
    if rule.expires_at_ms is not None and now_ms >= rule.expires_at_ms:
        return False
    return True


def auto_action_for(conn, identity: str) -> str | None:
    """'soft' se l'app ha 'consenti sempre' attivo, altrimenti None. Mai 'hard' automatico."""
    rule = db.get_user_rule(conn, identity)
    if rule is None or rule.status != "active":
        return None
    return rule.max_auto_action


def apply_decision(conn, settings: Settings, identity: str, choice: str, now_ms: int) -> db.UserRule:
    """Aggiorna user_rules in base alla scelta dell'utente. `choice`: deny | allow_once | allow_always."""
    rule = db.get_user_rule(conn, identity)
    denies = rule.consecutive_denies if rule else 0
    allow_once = rule.allow_once_count if rule else 0
    max_auto = rule.max_auto_action if rule else None
    source = "user"
    expires_at_ms = None

    if choice == "deny":
        denies += 1
        threshold = settings.get("actions", "consecutive_denies_to_ignore", default=3)
        if denies >= threshold:
            days = settings.get("actions", "learned_ignore_days", default=30)
            source = "ignore_learned"
            expires_at_ms = now_ms + int(days * 86_400_000)
    elif choice == "allow_once":
        allow_once += 1
        denies = 0
    elif choice == "allow_always":
        allow_once += 1
        denies = 0
        max_auto = "soft"
    else:
        raise ValueError(f"unknown choice: {choice!r}")

    return db.upsert_user_rule(
        conn, identity, consecutive_denies=denies, allow_once_count=allow_once,
        max_auto_action=max_auto, source=source, expires_at_ms=expires_at_ms, now_ms=now_ms,
    )
