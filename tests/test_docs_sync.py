"""CLAUDE.md impone di documentare ogni tabella del DB e ogni comando CLI nuovo (da M2 in poi)."""

from __future__ import annotations

from pathlib import Path

from tslow import database as db
from tslow.cli.app import app

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs_obsidian"
CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def test_every_db_table_is_documented(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "docs_sync.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    }
    conn.close()

    schema_doc = (DOCS_DIR / "Database_Schema.md").read_text(encoding="utf-8")
    missing = sorted(t for t in tables if f"`{t}`" not in schema_doc)
    assert not missing, f"Tabelle create dalle migrazioni ma non documentate in Database_Schema.md: {missing}"


def test_every_cli_command_is_documented() -> None:
    commands = {
        (cmd.name or cmd.callback.__name__) for cmd in app.registered_commands if cmd.callback is not None
    }
    task_log = (DOCS_DIR / "Task_Log.md").read_text(encoding="utf-8")

    missing = sorted(c for c in commands if f"`{c}" not in task_log and c not in task_log)
    assert not missing, f"Comandi CLI non menzionati in Task_Log.md: {missing}"
