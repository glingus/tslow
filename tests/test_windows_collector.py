"""Smoke test di collectors/windows.py sulla sessione desktop reale."""

from __future__ import annotations

import sys

import pytest

from tslow.collectors import windows

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="collector specifico per Windows")


def test_foreground_pid_returns_int_or_none() -> None:
    pid = windows.foreground_pid()
    assert pid is None or (isinstance(pid, int) and pid > 0)


def test_main_windows_for_current_process_does_not_crash() -> None:
    import os

    result = windows.main_windows_for_pid(os.getpid())
    assert isinstance(result, list)  # pytest non ha finestre: lista vuota attesa


def test_is_pid_hung_on_foreground_process() -> None:
    pid = windows.foreground_pid()
    if pid is None:
        pytest.skip("nessuna finestra in primo piano in questo momento")
    assert isinstance(windows.is_pid_hung(pid), bool)
