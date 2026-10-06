"""Finestre Win32: primo piano, rilevamento "non risponde", chiusura pulita (WM_CLOSE).

Usato dal detector (M2) per il segnale "app non risponde" e per l'esenzione primo piano, e
dall'optimizer (M3) per l'azione Hard (WM_CLOSE prima del kill).

`win32gui` di questa versione di pywin32 non espone `IsHungAppWindow`: si chiama user32.dll
direttamente via ctypes (verificato su questa macchina).
"""

from __future__ import annotations

import ctypes

import win32con
import win32gui
import win32process

_user32 = ctypes.windll.user32
_user32.IsHungAppWindow.restype = ctypes.c_bool
_user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]


def foreground_pid() -> int | None:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid or None


def _is_main_window(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.GetWindow(hwnd, win32con.GW_OWNER) != 0:
        return False  # ha un owner: non e' una finestra principale (es. tooltip, popup)
    return True


def enumerate_main_windows() -> dict[int, list[int]]:
    """Una sola EnumWindows: mappa PID -> finestre principali (visibili, senza owner).

    Usato dal detector per il segnale "non risponde": molto piu' economico di una EnumWindows
    per ogni processo sospetto.
    """
    result: dict[int, list[int]] = {}

    def _callback(hwnd: int, _extra: object) -> bool:
        if _is_main_window(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid:
                result.setdefault(pid, []).append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    return result


def main_windows_for_pid(pid: int) -> list[int]:
    """Finestre principali (visibili, senza owner) del processo `pid`."""
    return enumerate_main_windows().get(pid, [])


def is_hung(hwnd: int) -> bool:
    return bool(_user32.IsHungAppWindow(hwnd))


def is_pid_hung(pid: int) -> bool:
    """True se il processo ha almeno una finestra principale non rispondente (>5s senza processare messaggi)."""
    windows = main_windows_for_pid(pid)
    return any(is_hung(hwnd) for hwnd in windows)


def send_wm_close(hwnd: int) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def close_windows_for_pid(pid: int) -> int:
    """Invia WM_CLOSE a tutte le finestre principali del processo. Ritorna quante ne ha trovate."""
    windows = main_windows_for_pid(pid)
    for hwnd in windows:
        send_wm_close(hwnd)
    return len(windows)
