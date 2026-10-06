# why ts so slow? — project rules

System Monitor & Optimizer for Windows (CLI command: `tslow`). Full plan in memory (`tslow-piano-mvp`); this file only holds the operating rules to follow while writing code.

## Environment
- Python **3.12** only. Don't use the system PATH's `pip`: always create/use `.venv` (`py -3.12 -m venv .venv`) and invoke `.venv\Scripts\python -m pip ...`.
- The background watcher runs **elevated** (Task Scheduler); the CLI runs **non-elevated**. The only channel between them is the SQLite DB (WAL) at `data/metrics.db`.
- The watcher treats the DB as untrusted input: it always re-validates process identity (PID + create time) and protection level before acting.

## Protection whitelist (`src/tslow/protection.py`)
- Windows system processes (L0) and system maintenance processes (L2) are hardcoded and **never configurable**. Protected *apps* come only from the `[protection]` section of `settings.toml` in the install folder (admin-writable only): never from the DB, never from a user-writable location, and configuration can only add protection, never lower a hardcoded one. No exception via an 'always' user rule.
- Levels L0 (untouchable) → L3 (normal): see the plan for the full list.
- Every action that touches a process goes through `optimizer.apply()`, the only place that runs kill/priority/IO changes. No shortcuts elsewhere.

## Required documentation
- `docs_obsidian/Architecture.md`, `docs_obsidian/Database_Schema.md`, `docs_obsidian/Task_Log.md` must be updated **at every milestone and every new DB table or CLI command**.
- YAML frontmatter (`tags`, `aggiornato`, `stato`) and `[[...]]` wikilinks between notes.
- `tests/test_docs_sync.py` (from M2 onward) must fail if a DB table or CLI command isn't documented: don't disable it to make tests pass.

## Language
- Anything a user or a GitHub visitor reads — CLI output, notifications, the web dashboard, README, docs_obsidian — in **English**.
- Code identifiers (variables, functions, classes, modules) in **English**.
- Code comments and docstrings: no strict rule either way; existing Italian ones don't need to be translated.
- DB enum values and `settings.toml` keys are English too (migration `0004` and the M8 key rename); `src/tslow/labels.py` / `web/src/lib/api.ts` only turn them into display text.

## Security
- The watcher runs as `pythonw.exe -I` (isolated) from the copy in `C:\Program Files\TSlow`, never from the dev workspace.
- No destructive action (`kill`, system install/uninstall) without going through `optimizer.py`'s control pipeline, or without the explicit confirmation required for install/uninstall steps.
- The watcher stays in `--dry-run` until all of M3's action tests are green.

## Tests
- `.venv\Scripts\python -m pytest -q` must pass at the end of every milestone before considering it closed.
- Tests on actions (kill/priority) only use processes spawned by `scripts/spawn_hog.py`, never the user's real processes.
