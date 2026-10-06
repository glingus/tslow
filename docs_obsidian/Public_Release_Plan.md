---
tags: [tslow, plan, release]
aggiornato: 2026-10-06
stato: "M7 done except the LICENSE file (checkpoint 1: waiting for the user's choice); live verification and publishing wait for the user"
---

# Public Release Plan (M7)

Goal: make "why ts so slow?" ready to be published on GitHub for everyone, without betraying its
philosophy — **maximum lightness**. See [[Architecture]], [[Database_Schema]], [[Task_Log]].

This file is the work order. It is written to be executed start to finish by a coding session with
no other context.

## How to run this plan

1. Read `CLAUDE.md` first: its rules win over this file wherever they conflict.
2. Do the steps **in order**, one at a time. Start from the first unticked box in "Progress".
3. After each step: run `.venv\Scripts\python -m pytest -q` (must be green), update the docs as
   listed in the step, tick the box in "Progress", then go straight to the next step.
4. Do not ask for permission between steps. Stop only at the points listed in "Checkpoints".
5. If a step turns out to be wrong against the real code (a file moved, a function is named
   differently), adapt to the code and note the deviation in [[Task_Log]]. Don't stop for that.

## Progress

- [x] M7.0 — Green baseline on any machine
- [x] M7.1 — Incident lifecycle and decision polling (the 1 Hz bug)
- [x] M7.2 — Data and log location
- [x] M7.3 — Configurable protection list
- [x] M7.4 — Per-tick waste and dead settings
- [x] M7.5 — Installer fixes
- [x] M7.6 — Drop pandas and numpy
- [x] M7.7 (everything except the LICENSE file, which waits for the user's choice) — README, license, pre-publish cleanup

## Hard rules for the whole plan

- Never run `tslow install` or `tslow uninstall`, never stop or restart the scheduled task
  "TSlow Monitor", never touch `C:\Program Files\TSlow`. The user does those, from an admin
  terminal, when they decide to.
- Never kill or re-prioritize a real process. Action tests use `scripts/spawn_hog.py` only.
- Every process action stays inside `optimizer.py`. No new call to `kill()`, `nice()`, `ionice()`
  or `WM_CLOSE` anywhere else.
- No `git init`, commit or push unless the user asks.
- The installed watcher is live on this PC and holds the single-instance mutex: a second watcher
  started from the dev workspace exits immediately. Don't work around it; unit-test with fakes
  and an isolated temp DB (`db.connect(tmp_path / "x.db")`).
- Never write to the live DBs. `data/metrics.db` in the workspace and
  `%USERPROFILE%\Desktop\tslow\data\metrics.db` may be opened **read-only** for inspection
  (`sqlite3.connect("file:...?mode=ro", uri=True)`).
- User-facing text (CLI, notifications, web, README, docs) in English. New `settings.toml` keys in
  English. Leave the existing Italian keys, Italian DB enum values and Italian comments alone.
- Don't add dependencies. This plan only removes them.
- Keep changes small and in the existing style. No refactors beyond what a step asks for.

## Measured facts this plan is based on (2026-10-06)

| | Old PC (i5-8250U) | New PC |
|---|---|---|
| Watcher CPU, average (% of whole machine) | 0.38 (budget 0.25) | 0.13 |
| Watcher RSS | 22 MB avg, 56 max | 27–47 MB, 66 max |
| Real gap between samples in the idle state | ~1.0 s | ~1.0 s |
| Governor multiplier | ~x4 (pinned at max) | x1 |

- Idle tick is configured at 5 s (10 s on battery) but really runs at 1 s, on both machines.
- Baseline test run: 158 passed, 1 failed (`test_wmi_inventory_matches_known_hardware`, which
  asserts the old PC's hardware).

---

## M7.0 — Green baseline on any machine

**Why.** One test hardcodes the old laptop's CPU and GPU. It fails here and would fail for every
contributor.

**Do.**
- `tests/test_collectors.py::test_wmi_inventory_matches_known_hardware`: replace the hardware
  literals with machine-independent checks — `cpu_name` is a non-empty string,
  `cpu_cores_logical >= cpu_cores_physical >= 1`, `gpu_names` is a string or `None`. Rename the test
  to say what it now checks.
- `tests/test_paths.py`: replace the `C:\Users\<old-username>\Desktop\tslow` fixture paths with a neutral
  one (e.g. `C:\dev\tslow`). They are only fixtures; behavior doesn't change.

**Done when.** The suite is fully green.

---

## M7.1 — Incident lifecycle and decision polling

**Why.** This is the single biggest lightness bug. In `monitor.py::run_daemon`, any open incident
forces `tick_period = min(tick_period, 1.0)` so decisions are read quickly. But incidents only ever
close in `_process_pending_decisions`, after a decision. Incidents with `proposed_action='nessuna'`
(L0 processes, throttling, system events) can't receive a decision, so they stay `'aperto'` forever
and the watcher does full sampling at 1 Hz forever. It also neutralizes the overhead governor and
the battery tick, and `detector._open_incident` never reports that (group, resource) pair again.

**Do.**
1. **Expiry.** Add `db.expire_stale_incidents(conn, now_ms, ttl_ms) -> int` in `database.py`:
   one `UPDATE` that sets `status='scaduto'`, `closed_at_ms=now_ms` on rows with
   `status='aperto'`, `opened_at_ms < now_ms - ttl_ms`, and no decision with
   `execution_status='pending'`. `'scaduto'` already exists in the `CHECK` constraint and in both
   label maps (shown as "expired"): no migration.
2. Call it from `monitor._flush` (already runs every 60 s). TTL from a new setting
   `[incidents] expire_after_minutes = 30` in `config/settings.toml`, read with the same default
   in code. If the slowdown is still there after expiry the detector opens a new incident; the
   notifier's existing cooldowns prevent spam.
3. **Decouple decision polling from sampling.** Remove the `has_open_incidents` /
   `tick_period = min(tick_period, 1.0)` block. Replace the final `time.sleep(...)` with a small
   helper that:
   - checks once per tick whether an *actionable* open incident exists — new
     `db.has_actionable_open_incident(conn)`:
     `SELECT 1 FROM incidents WHERE status='aperto' AND proposed_action != 'nessuna' LIMIT 1`;
   - if none: one plain sleep for the whole remaining period;
   - if any: sleeps in slices of at most 1 s, and after each slice calls
     `db.list_pending_decisions(conn)`; only when that returns something, takes a fresh
     `processes_collector.snapshot()` and calls `_process_pending_decisions`.
   The fresh snapshot is used for that decision only: do not overwrite `prev_process_snapshots` /
   `prev_process_ts`, the detector's deltas depend on them.
4. Keep the existing `_process_pending_decisions` call inside the tick (auto-decisions created by
   `_handle_new_incidents` in the same tick rely on it).

**Tests.** `test_database_detection.py`: expiry closes old open incidents, leaves recent ones,
leaves ones with a pending decision, returns the count. `test_monitor.py`: the sleep helper —
with injected fake `sleep`/clock — does one long sleep with no actionable incident, slices with
one, and processes a decision that appears mid-wait. Structure the helper so it can be tested
without running `run_daemon`.

**Docs.** [[Architecture]]: new ADR section "M7.1" (the bug, the measured 1 Hz, the TTL choice
over tracking each condition). [[Database_Schema]]: note that `status='scaduto'` is now written
by the watcher. [[Task_Log]]: entry.

**Done when.** No code path makes the sampling period depend on open incidents; suite green.

---

## M7.2 — Data and log location

**Why.** `paths.data_dir()` / `logs_dir()` hardcode `~/Desktop/tslow/...` for the installed copy:
the first developer's folder layout. On any other PC the watcher silently creates a `tslow` folder
on the Desktop, and the dev CLI reads a different DB than the watcher writes.

**Do.**
- `paths.py`, installed mode: root is `%LOCALAPPDATA%\TSlow` (fallback
  `Path.home() / "AppData" / "Local" / "TSlow"` if the variable is missing), with `data\` and
  `logs\` under it. The scheduled task runs as the logged-on user, so the elevated watcher and the
  non-elevated CLI resolve the same folder and both can write it — which the DB channel needs.
- Dev mode: unchanged (workspace `data/`, `logs/`), plus an optional `TSLOW_DATA_DIR` environment
  variable that overrides `data_dir()` **only when not installed**, so a developer can point the
  dev CLI at the live DB. The installed runtime must ignore it.
- Fix the strings that mention the Desktop: `cli/app.py` (uninstall message), `install.py`
  (`uninstall` docstring).
- No automatic migration of old data. Add a short "Upgrading from an earlier install" note for
  the README step (M7.7): the old DB is at `Desktop\tslow\data\metrics.db` and can be copied by
  hand while the watcher is stopped.

**Tests.** `test_paths.py`: installed → under `LOCALAPPDATA`; fallback when the variable is unset;
dev → workspace; `TSLOW_DATA_DIR` honored in dev and ignored when installed.

**Docs.** [[Architecture]] (Q&A "Install" row, security model: the data folder is user-writable
by design, the DB is untrusted input), [[Task_Log]].

---

## M7.3 — Configurable protection list

**Why.** `protection.py` hardcodes the first developer's own apps (`chrome`, `claude`, `code`,
`node` under `claude`) as untouchable for everybody. The user decided on 2026-10-06: the list of
protected **apps** becomes configurable; the list of **Windows system** processes stays in code.

**Design.**
- Stay hardcoded, never configurable: `_L0_SYSTEM_NAMES` (move `explorer` into it) and
  `_L2_NAMES`.
- Remove `_L0_APP_NAMES`, `_L1_PARENT_NAMES` and the `node`/`claude` special case. Replace them
  with a frozen dataclass `UserProtection(untouchable, free_rein_parents, untouchable_children)`
  (frozensets; `untouchable_children` holds `(child, ancestor)` pairs), names normalized with
  `normalize_name`.
- Source: a `[protection]` section in `settings.toml`. Installed, that file is
  `C:\Program Files\TSlow\settings.toml`, writable only by an administrator — this is what keeps
  the security property. Never read protection from the DB or from any user-writable location.
- Shipped default in `config/settings.toml` — empty lists plus commented examples:
  ```toml
  [protection]
  # Apps tslow must never touch, whatever they do (level L0). Names without ".exe".
  # Example: untouchable = ["chrome", "firefox", "code"]
  untouchable = []
  # Every descendant of these apps gets "free rein" (L1): only a critical slowdown asks you.
  free_rein_parents = []
  # [child, ancestor] pairs: the child is untouchable only when it runs under that ancestor.
  # Example: untouchable_children = [["node", "code"]]
  untouchable_children = []
  ```
  Also update the header comment of `config/settings.toml`, which currently says the list does
  not live there.
- `classify(name, exe_path, ancestor_names, user=None)`: when `user` is `None` it uses
  `protection.get_user_protection()`, which lazily parses the settings section once and caches
  it; `protection.set_user_protection(...)` overrides it (tests, fail-safe). Call sites in
  `detector.py` and `optimizer.py` don't need to change.
- Order inside `classify`: system L0 → user `untouchable` → `untouchable_children` → hardcoded
  L2 → user `free_rein_parents` (L1) → L3. L2 is checked **before** the user's L1 rule on
  purpose: configuration may only add protection, never make a hardcoded entry killable.
- Validation: each key must be a list of strings (pairs for `untouchable_children`). Missing
  section → empty protection. Wrong types → `ProtectionConfigError` with a message naming the
  key.
- Fail-safe in `monitor.run_daemon`: load the protection eagerly at startup; on
  `ProtectionConfigError` log an ERROR, install an empty `UserProtection`, and **force
  `dry_run=True`** for that run. A broken protection file must never lead to a real action.
- New CLI command `tslow protection`: prints the settings file path, the built-in system and
  maintenance lists, and the user's three lists (or the config error). Read-only. Editing is done
  by opening the settings file as administrator — say so in the output.

**Tests.**
- Add `tests/conftest.py` with an autouse fixture that installs the legacy lists
  (`untouchable={"chrome","claude","code"}`, same three as `free_rein_parents`,
  `untouchable_children={("node","claude")}`) and restores the previous value afterwards. This
  keeps every existing test meaningful without rewriting them.
- `test_protection.py`, new cases that override the fixture: with empty config `chrome` is L3;
  user `untouchable` → L0; a system name stays L0 even when config tries nothing or anything;
  an L2 name under a `free_rein_parents` ancestor stays L2; parsing accepts the documented
  format; wrong types raise `ProtectionConfigError`; missing section → empty.
- `test_monitor.py` or `test_optimizer.py`: a config error at startup results in `dry_run=True`
  (extract the startup logic into a small testable function).
- `test_optimizer.py`: an app listed only in the user config is refused with
  `protected_refused`.

**Docs.**
- `CLAUDE.md`, section "Protection whitelist": replace the first bullet with —
  "Windows system processes (L0) and system maintenance processes (L2) are hardcoded and **never
  configurable**. Protected *apps* come only from the `[protection]` section of `settings.toml`
  in the install folder (admin-writable only): never from the DB, never from a user-writable
  location, and configuration can only add protection, never lower a hardcoded one. No exception
  via an 'always' user rule." Keep the other two bullets.
- [[Architecture]]: "Protection levels" section and a new ADR (why configurable, why the file in
  Program Files preserves the model, the L2-before-L1 order, the dry-run fail-safe).
- [[Task_Log]]: entry that names the new command `` `protection` `` (required by
  `tests/test_docs_sync.py`).

---

## M7.4 — Per-tick waste and dead settings

**Why.** After M7.1 the idle cost is already ~5x lower; this removes what's left that is plainly
redundant, and stops `settings.toml` from advertising knobs that do nothing.

**Do.**
- Measure first: run `tslow bench` and record `system.collect`'s time in [[Task_Log]].
- `psutil.sensors_battery()` is called up to three times per tick (`system.collect`,
  `monitor._on_battery` via `tick_globale_period_s`, and again for `TickInput.on_battery`). Read
  it once in `collect` and pass `snap.battery_plugged` down; `tick_globale_period_s` takes an
  `on_battery` argument instead of calling psutil.
- `system_collector.net_link_speed_mbps()` (`psutil.net_if_stats`) and `psutil.disk_usage` run
  every tick but change slowly: refresh them at most every 60 s (a tiny time-based cache; keep
  `collect()` testable).
- `grouping.build_groups` runs twice per tick (in `Detector.evaluate` and in
  `ProcessGroupAccumulator.record`). Compute it once and pass it to the accumulator.
- Delete from `config/settings.toml` the keys no code reads: `calmo_processi`,
  `attenzione_processi`, `indagine_processi`, `budget_ram_mb`, `minuti_per_riduzione`. Grep
  `src/` first to confirm each is unread. Don't implement them: with a 5 s idle tick the process
  snapshot (~3 ms) costs nothing worth a second schedule.
- Measure again with `tslow bench`; record before/after.

**Tests.** Adjust `test_monitor.py` / `test_collectors.py` for the changed signatures; add one
test that the cache does not call the slow function again inside its window.

**Docs.** [[Task_Log]] entry with the two measurements.

---

## M7.5 — Installer fixes

**Why.** A fresh clone cannot install today, and a reinstall would wipe the user's protection
list from M7.3.

**Do.**
- `.gitignore`: remove the `requirements.lock` line. `install._install_dependencies` reads that
  file, so it must be in the repository.
- `install._write_settings`: if `INSTALL_DIR / "settings.toml"` already exists, leave it; always
  (re)write the shipped defaults next to it as `settings.default.toml` for reference. Every
  `settings.get(...)` already has a code default, so an older file keeps working.
- CLI shim so people can type `tslow` after installing: pure function
  `build_cli_shim(python_exe) -> str` returning a one-line `tslow.cmd`
  (`@"<runtime>\python.exe" -I -m tslow %*`); `_write_cli_shim` puts it in
  `%LOCALAPPDATA%\Microsoft\WindowsApps` (on PATH by default) and skips silently if that folder
  doesn't exist; `_remove_cli_shim` in `uninstall`. List it in the confirmation text of
  `tslow install` in `cli/app.py`.
- `uninstall` message: say the data in `%LOCALAPPDATA%\TSlow` is left untouched.

**Tests.** `test_install.py`: `build_cli_shim` content; `_write_settings` preserves an existing
file and writes the default copy (use `monkeypatch` on `INSTALL_DIR` with `tmp_path`). The
functions that touch the real system stay untested and are **not run**.

**Docs.** [[Architecture]] ADR, [[Task_Log]].

---

## M7.6 — Drop pandas and numpy

**Why.** They are the heaviest dependencies by far, used only to read a few thousand rows and
average them. Removing them shrinks the install, speeds up `pip`, and makes `tslow status` and
`tslow web` start faster.

**Do.**
- Before changing anything, seed a temp DB and save the JSON of every `web/api.py` endpoint
  (reuse the seeding helpers in `tests/test_web_api.py`). The same requests must return the same
  JSON afterwards — the React frontend is not touched in this step.
- Rewrite `analytics.py` on `sqlite3` + stdlib: functions return `list[dict]` (or plain
  dicts/floats) instead of DataFrames. `trend` becomes a plain least-squares slope (the same
  arithmetic already exists in `detector._LeakTracker.trend`); `with_moving_averages` becomes a
  time-window rolling mean over the sorted rows. Missing values are `None`, never `NaN`.
- Update the two consumers, `cli/dashboard.py` and `web/api.py`, and
  `tests/test_analytics.py` / `test_dashboard.py` / `test_web_api.py`.
- `pyproject.toml`: remove `pandas` from the `cli` and `web` extras.
- Regenerate `requirements.lock` the way the M5 ADR describes: a clean temporary venv (in the
  scratch/temp folder, not in the repo) with only `.[daemon,cli,web]` installed, then
  `pip freeze` without the `tslow` line itself.

**Done when.** `pandas` and `numpy` appear nowhere in `src/`, `tests/`, `pyproject.toml` or
`requirements.lock`; endpoint JSON is unchanged; suite green.

**Docs.** [[Architecture]] ADR, [[Task_Log]].

---

## M7.7 — README, license, pre-publish cleanup

**Do.**
- Rewrite `README.md` for a stranger: what it is and what it is not; the lightness numbers
  measured above; requirements (Windows 10/11, Python 3.12); install from a clone (venv,
  `pip install -e .[daemon,cli,web]`, optional `npm ci && npm run build` in `web/` for the web
  dashboard, then `tslow install` from an admin terminal); what the installer changes on the PC;
  daily use; how to protect your own apps (M7.3, with the `[protection]` example); safety model
  in five lines (the watcher is the only thing that acts, dry-run by default, everything is
  local, nothing leaves the PC); uninstall; "Upgrading from an earlier install" (M7.2). No
  personal paths.
- Replace personal usernames in tracked files with a neutral placeholder
  (`C:\Users\<you>\...`): grep for the developer's usernames across the repo, excluding `.venv`,
  `web/node_modules`, `data`, `logs`.
- `CLAUDE.md`: delete the "Pending work" section (the trigger phrase) — it must not ship.
- Set this file's `stato` to done and update `aggiornato` here and in the other three notes.
- **License:** see Checkpoints.

---

## Checkpoints — the only places to stop and ask the user

1. **License (M7.7).** Ask which license to use; recommend MIT. Don't create `LICENSE` before
   the answer. Everything else in M7.7 can be done first.
2. **Live verification.** Unit tests are enough to close each step. Anything that needs the real
   watcher — reinstalling, restarting the task, a `spawn_hog.py` end-to-end run against the
   installed copy — is the user's call: at the end, list what should be verified live and wait.
3. **Publishing.** `git init`, the first commit, creating the GitHub repository and pushing all
   wait for an explicit request.

## Final report to the user

When all boxes are ticked, tell the user in plain language:
- what changed, step by step, and the test count;
- that the installed copy is still the old one until they re-run `tslow install`;
- the snippet to paste into `C:\Program Files\TSlow\settings.toml` (as administrator) to keep
  their own apps protected after reinstalling:
  ```toml
  [protection]
  untouchable = ["chrome", "claude", "code"]
  free_rein_parents = ["chrome", "claude", "code"]
  untouchable_children = [["node", "claude"]]
  ```
- the open decisions below.

## Resolved in M8 — see [[Task_Log]]
Real actions on the installed watcher (`[watcher] dry_run`), English enums and keys, one dashboard (web). Kept as decided: no prebuilt `web/dist`.

## Done after M8 — see [[Task_Log]]
MIT `LICENSE`, GitHub preparation (CI, release workflow), automatic update check + `tslow update` (M9).

## Not in this plan — do not start these

They are real, but each needs a decision from the user first:
- How an installed user turns real actions on: the scheduled task runs `tslow daemon` with its
  default `--dry-run`, so the installed watcher never acts.
- Migrating the Italian DB enum values and the Italian `settings.toml` keys to English before
  other people have data.
- Keeping one dashboard instead of two (web vs. TUI).
- Shipping a prebuilt `web/dist` or a release package, so users don't need Node.
- New actions: automatic restore of a Soft priority when an incident closes, Windows 11
  efficiency mode (EcoQoS), a real action for RAM incidents, a low-memory system notification
  instead of polling.
