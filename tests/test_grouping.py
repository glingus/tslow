"""grouping.py su un albero di processi sintetico (stile Chrome: 1 radice + N figli/nipoti)."""

from __future__ import annotations

from tslow.collectors.processes import ProcessSnapshot
from tslow.grouping import ancestor_names, build_groups, group_root_pid


def _snap(pid: int, ppid: int, name: str, create_time: float) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        name=name,
        create_time=create_time,
        thread_count=1,
        handle_count=10,
        working_set_bytes=1_000_000,
        private_bytes=500_000,
        cpu_time_seconds=0.0,
        io_read_bytes=0,
        io_write_bytes=0,
        io_other_bytes=0,
    )


def test_chrome_tree_collapses_into_one_group() -> None:
    snapshots = {
        1: _snap(1, 0, "explorer.exe", 100.0),
        100: _snap(100, 1, "chrome.exe", 200.0),  # browser principale
        101: _snap(101, 100, "chrome.exe", 201.0),  # renderer
        102: _snap(102, 100, "chrome.exe", 202.0),  # GPU process
        103: _snap(103, 101, "chrome.exe", 203.0),  # figlio di un renderer
    }
    groups = build_groups(snapshots)
    assert set(groups["chrome"]) == {100, 101, 102, 103}
    # explorer resta un gruppo separato (nome diverso)
    assert groups["explorer"] == [1]


def test_different_named_child_is_own_group_root() -> None:
    snapshots = {
        100: _snap(100, 0, "chrome.exe", 100.0),
        101: _snap(101, 100, "helper.exe", 101.0),  # nome diverso: radice del proprio gruppo
    }
    groups = build_groups(snapshots)
    assert groups["chrome"] == [100]
    assert groups["helper"] == [101]


def test_pid_reuse_breaks_parent_link() -> None:
    # Il PID 50 era un chrome.exe, ma e' stato riassegnato a notepad.exe DOPO che il "figlio" e' nato:
    # il collegamento e' invalido, notepad diventa radice di se stesso.
    snapshots = {
        50: _snap(50, 0, "notepad.exe", 500.0),  # create_time PIU' TARDI del "figlio" sotto
        60: _snap(60, 50, "chrome.exe", 100.0),  # "figlio" nato PRIMA del presunto genitore attuale
    }
    groups = build_groups(snapshots)
    assert groups["chrome"] == [60]
    assert groups["notepad"] == [50]


def test_ancestor_names_orders_from_parent_to_root() -> None:
    snapshots = {
        1: _snap(1, 0, "explorer.exe", 100.0),
        100: _snap(100, 1, "claude.exe", 200.0),
        101: _snap(101, 100, "node.exe", 201.0),
    }
    assert ancestor_names(101, snapshots) == ["claude.exe", "explorer.exe"]


def test_group_root_pid_self_when_no_matching_parent() -> None:
    snapshots = {1: _snap(1, 0, "standalone.exe", 100.0)}
    assert group_root_pid(1, snapshots) == 1
