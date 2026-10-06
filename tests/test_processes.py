"""NtQuerySystemInformation confrontato con psutil come sorgente di verita' (richiesto dal piano M1)."""

from __future__ import annotations

import sys

import psutil
import pytest

from tslow.collectors import processes

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="collector specifico per Windows")

# Differenze note e accettate tra ntquery e psutil (non sono errori di parsing):
# - "System" (PID 4): psutil azzera create_time per questo processo speciale.
# - "Secure System": processo protetto (VBS), psutil non riesce a leggerne il nome.
# - "Memory Compression": psutil usa l'alias interno "MemCompression".
_KNOWN_EXCEPTIONS = {"System", "Secure System", "Memory Compression", "MemCompression"}


def test_ntquery_pid_set_matches_psutil() -> None:
    nt_snapshot = processes.snapshot_via_ntquery()
    psutil_pids = set(psutil.pids())
    nt_pids = set(nt_snapshot.keys())

    missing_from_nt = psutil_pids - nt_pids
    assert missing_from_nt <= {0}, f"PID in psutil ma non in ntquery: {missing_from_nt}"


def test_ntquery_name_and_create_time_match_psutil() -> None:
    nt_snapshot = processes.snapshot_via_ntquery()
    psutil_pids = set(psutil.pids())
    nt_pids = set(nt_snapshot.keys())

    mismatches = []
    for pid in psutil_pids & nt_pids:
        try:
            p = psutil.Process(pid)
            p_name = p.name()
            p_ct = p.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        nt = nt_snapshot[pid]
        if nt.name in _KNOWN_EXCEPTIONS or p_name in _KNOWN_EXCEPTIONS:
            continue
        name_ok = nt.name.lower() == p_name.lower() or nt.name == ""
        ct_ok = abs(nt.create_time - p_ct) < 0.01
        if not (name_ok and ct_ok):
            mismatches.append((pid, nt.name, p_name, nt.create_time, p_ct))

    assert not mismatches, f"Mismatch inattesi (primi 5): {mismatches[:5]}"


def test_snapshot_uses_ntquery_and_has_current_process() -> None:
    result, method = processes.snapshot()
    assert method == "ntquery"
    assert psutil.Process().pid in result
    assert len(result) > 10


def test_psutil_fallback_has_current_process() -> None:
    result = processes.snapshot_via_psutil()
    assert psutil.Process().pid in result
    own = result[psutil.Process().pid]
    assert own.name.lower().startswith("python")
