"""Snapshot dei processi: NtQuerySystemInformation (ctypes), fallback psutil.process_iter.

Una singola chiamata NtQuerySystemInformation(SystemProcessInformation) restituisce PID, PPID,
nome, create time, tempi CPU, RAM privata/working set, handle e I/O per TUTTI i processi in una
volta (~2-4 ms per 220 processi su questo PC), senza OpenProcess e quindi senza AccessDenied.
Il fallback psutil.process_iter ripete lo snapshot di sistema per ogni processo e costa ordini di
grandezza di piu' (misurato da `tslow bench`).

Layout della struct verificato confrontando nome/create_time con psutil.Process per tutti i PID
su questa macchina (Windows 11 x64): le uniche differenze osservate sono System (psutil azzera il
create_time), Secure System (psutil non legge il nome, processo protetto) e Memory Compression
(psutil usa l'alias interno "MemCompression"): vedi test_processes.py.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)

_SYSTEM_PROCESS_INFORMATION_CLASS = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_FILETIME_EPOCH_OFFSET_100NS = 116_444_736_000_000_000  # 1601-01-01 -> 1970-01-01, in unita' da 100ns


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]


_NtQuerySystemInformation = None


def _get_ntquery():
    global _NtQuerySystemInformation
    if _NtQuerySystemInformation is None:
        ntdll = ctypes.WinDLL("ntdll")
        func = ntdll.NtQuerySystemInformation
        func.restype = ctypes.c_ulong
        _NtQuerySystemInformation = func
    return _NtQuerySystemInformation


def _filetime_to_unix(filetime_100ns: int) -> float:
    if filetime_100ns <= 0:
        return 0.0
    return (filetime_100ns - _FILETIME_EPOCH_OFFSET_100NS) / 10_000_000


@dataclass
class ProcessSnapshot:
    pid: int
    ppid: int
    name: str
    create_time: float
    thread_count: int
    handle_count: int
    working_set_bytes: int
    private_bytes: int
    cpu_time_seconds: float  # UserTime + KernelTime, cumulativo dalla creazione del processo
    io_read_bytes: int
    io_write_bytes: int
    io_other_bytes: int


def snapshot_via_ntquery() -> dict[int, ProcessSnapshot]:
    """Uno snapshot di sistema con una sola syscall. Solleva OSError se la chiamata fallisce."""
    query = _get_ntquery()
    size = 1 << 20
    buf = ctypes.create_string_buffer(size)
    while True:
        return_length = wintypes.ULONG(0)
        status = query(_SYSTEM_PROCESS_INFORMATION_CLASS, buf, size, ctypes.byref(return_length))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, return_length.value + 4096)
            buf = ctypes.create_string_buffer(size)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: status={status:#x}")
        break

    results: dict[int, ProcessSnapshot] = {}
    offset = 0
    while True:
        entry = _SYSTEM_PROCESS_INFORMATION.from_buffer(buf, offset)
        pid = entry.UniqueProcessId or 0
        if pid != 0:  # PID 0 = System Idle Process, non e' un processo reale
            name = ""
            if entry.ImageName.Length and entry.ImageName.Buffer:
                name = ctypes.wstring_at(entry.ImageName.Buffer, entry.ImageName.Length // 2)
            results[pid] = ProcessSnapshot(
                pid=pid,
                ppid=entry.InheritedFromUniqueProcessId or 0,
                name=name,
                create_time=_filetime_to_unix(entry.CreateTime),
                thread_count=entry.NumberOfThreads,
                handle_count=entry.HandleCount,
                working_set_bytes=entry.WorkingSetSize,
                private_bytes=entry.PrivatePageCount,
                cpu_time_seconds=(entry.UserTime + entry.KernelTime) / 10_000_000,
                io_read_bytes=entry.ReadTransferCount,
                io_write_bytes=entry.WriteTransferCount,
                io_other_bytes=entry.OtherTransferCount,
            )
        if entry.NextEntryOffset == 0:
            break
        offset += entry.NextEntryOffset
    return results


_PSUTIL_ATTRS = ["pid", "ppid", "name", "create_time", "num_threads", "num_handles", "memory_info", "cpu_times", "io_counters"]


def snapshot_via_psutil() -> dict[int, ProcessSnapshot]:
    """Fallback se NtQuerySystemInformation non e' disponibile: piu' lento, uno snapshot per processo."""
    results: dict[int, ProcessSnapshot] = {}
    for proc in psutil.process_iter(attrs=_PSUTIL_ATTRS, ad_value=None):
        info = proc.info
        pid = info.get("pid")
        if not pid:
            continue
        mem = info.get("memory_info")
        cpu = info.get("cpu_times")
        io = info.get("io_counters")
        results[pid] = ProcessSnapshot(
            pid=pid,
            ppid=info.get("ppid") or 0,
            name=info.get("name") or "",
            create_time=info.get("create_time") or 0.0,
            thread_count=info.get("num_threads") or 0,
            handle_count=info.get("num_handles") or 0,
            working_set_bytes=getattr(mem, "wset", None) or getattr(mem, "rss", 0) if mem else 0,
            private_bytes=getattr(mem, "private", None) or 0 if mem else 0,
            cpu_time_seconds=(cpu.user + cpu.system) if cpu else 0.0,
            io_read_bytes=getattr(io, "read_bytes", 0) if io else 0,
            io_write_bytes=getattr(io, "write_bytes", 0) if io else 0,
            io_other_bytes=0,
        )
    return results


def snapshot() -> tuple[dict[int, ProcessSnapshot], str]:
    """Ritorna (snapshot, metodo): metodo e' 'ntquery' oppure 'psutil' se e' scattato il fallback."""
    try:
        return snapshot_via_ntquery(), "ntquery"
    except OSError:
        logger.warning("NtQuerySystemInformation non disponibile, uso il fallback psutil.process_iter", exc_info=True)
        return snapshot_via_psutil(), "psutil"
