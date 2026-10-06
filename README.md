# why ts so slow?

A tiny background watcher for Windows that notices **which app is choking your PC**, tells you with a
native notification, and lets you deal with it. CLI command: `tslow`.

It is built around one rule: **maximum lightness**. The watcher should cost less than the thing it
is trying to find.

## What it is, and what it is not

- **It is**: a watcher that samples CPU, RAM, disk, network, GPU and per-process usage, learns what
  "normal" looks like on your machine, finds the app (or app group) responsible when something goes
  wrong, and shows you a history (web dashboard).
- **It is not**: a cleaner, a registry tweaker, an antivirus, or a cloud service. It never phones
  home: everything stays on your PC.
- **Actions are opt-in.** The installed watcher starts in *dry-run*: it detects, notifies and records
  your decisions, but does not change any process. To let it act, see [Turning real actions on](#turning-real-actions-on).

## How light is it?

Measured on two real laptops/desktops while idle (percentages are of the *whole* machine):

| | Older laptop (i5-8250U, 8 GB) | Newer PC |
|---|---|---|
| Watcher CPU, average | ~0.4 % | ~0.13 % |
| Watcher memory (RSS) | ~22 MB average, 56 MB peak | ~27–47 MB, 66 MB peak |

It samples every 5 s when idle (10 s on battery) and speeds up only while something is wrong. It also
measures its own overhead and slows itself down if it goes over its budget. `tslow bench` shows what
each collector costs on your machine.

## Requirements

- Windows 10 or 11
- Python **3.12**
- Node.js — to build the web dashboard (`tslow web`) yourself; there is no prebuilt copy

## Install

From a clone of this repository, in a normal (non-administrator) terminal:

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[daemon,cli,web]"
```

Optional, for the web dashboard (`tslow web`):

```bash
cd web
npm ci
npm run build
cd ..
```

Then, from an **administrator** terminal in the same folder:

```bash
.venv\Scripts\tslow.exe install
```

It lists what it will change and asks for confirmation. It:

- copies a private Python runtime and tslow into `C:\Program Files\TSlow` (the watcher never runs
  from your working copy),
- registers a scheduled task, **TSlow Monitor**, that starts the watcher at logon with elevated
  rights,
- registers the `tslow://` link used by the notification and an app id for toasts (current user
  only),
- adds a "TSlow" profile to Windows Terminal,
- adds a `tslow.cmd` shortcut to `%LOCALAPPDATA%\Microsoft\WindowsApps` so you can type `tslow`
  from any terminal,
- keeps an existing `C:\Program Files\TSlow\settings.toml` on reinstall (your protection list
  survives); the shipped defaults are always written next to it as `settings.default.toml`.

Your database and logs live in `%LOCALAPPDATA%\TSlow` (`data\metrics.db`, `logs\`).

## Daily use

Run these from a normal terminal.

| Command | What it does |
|---|---|
| `tslow status` | Is the watcher running? Latest sample and its own overhead. |
| `tslow web` | Web dashboard at `http://127.0.0.1:8765` (local only). |
| `tslow resolve` | Look at the latest open incident and choose what to do. |
| `tslow undo <id>` | Undo a priority change made by a decision. |
| `tslow rules list` | Show learned rules (`revoke <id>`, `reset-learning` to remove). |
| `tslow protection` | Show what tslow will never touch. |
| `tslow update` | Install a newer release (administrator terminal). `--check` just looks. |
| `tslow bench` | Measure the cost of each collector on this PC. |

When a slowdown is detected you get a Windows notification; clicking it opens a terminal on
`tslow resolve` so you can choose: **[1] Deny**, **[2] Allow once**, **[3] Always allow**.

## Protecting your own apps

Windows system processes (`svchost`, `lsass`, `explorer`, …) and system-maintenance processes
(Windows Update, Search Indexer, …) are built in and can **never** be touched or configured away.

The apps *you* care about are listed in `C:\Program Files\TSlow\settings.toml`, which only an
administrator can edit. Open it as administrator and fill in:

```toml
[protection]
# Never touched, whatever they do:
untouchable = ["chrome", "firefox", "code"]
# Every descendant of these apps gets "free rein": only a critical slowdown asks you.
free_rein_parents = ["code"]
# [child, ancestor] pairs: the child is untouchable only when it runs under that ancestor.
untouchable_children = [["node", "code"]]
```

Names are process names without `.exe`. Check the result with `tslow protection`. If the section is
invalid, the watcher logs an error and runs in dry-run mode until you fix it. The shipped default
list is empty.

## Turning real actions on

By default the watcher only detects, notifies and records your choices. To let it carry out the
choices you confirm (lower priority, close or kill an app), open
`C:\Program Files\TSlow\settings.toml` **as administrator**, set

```toml
[watcher]
dry_run = false
```

and restart the **TSlow Monitor** task (Task Scheduler: End, then Run). `tslow status` shows the
configured mode. Fill in `[protection]` first if there are apps you never want touched. Set it back
to `true` any time to return to watch-only. For a one-off run from a terminal,
`tslow daemon --foreground --no-dry-run` overrides the setting.

## Safety model

1. **Only the watcher acts**, and only through one code path (`optimizer.py`). The CLI and the web
   dashboard just write a *proposed* decision into the database.
2. The watcher treats the database as untrusted input: before acting it re-checks the process
   identity (PID and creation time) and its protection level.
3. **Dry-run is the default** (`[watcher] dry_run = true` in the admin-only `settings.toml`), so a
   fresh install never touches a process until you change that line.
4. Everything is local. The web dashboard listens on `127.0.0.1` only and has no authentication
   for that reason.
5. The system protection lists are hardcoded; your app list only comes from the admin-writable
   settings file, and configuration can only add protection, never remove it.

## Updating

The watcher looks for a newer release on GitHub (`[updates] repository`, shipped as
`glingus/tslow`; set `enabled = false` to turn it off, or point it at your fork) once a day and shows a notification. It never installs anything by itself. To update, from an
**administrator** terminal:

```bash
tslow update
```

It shows the new version, asks for confirmation, downloads the release, checks its SHA-256 against
the release's `SHA256SUMS`, stops the watcher, reinstalls tslow in `C:\Program Files\TSlow`, and
starts the watcher again. Your `settings.toml` and your data are kept; the new defaults land in
`settings.default.toml`. `tslow update --check` only looks, without admin rights. There is no code
signature: the check protects against corrupted downloads, not against a compromised GitHub account.

## Uninstall

From an administrator terminal:

```bash
.venv\Scripts\tslow.exe uninstall
```

Removes the scheduled task, the registry entries, the Windows Terminal profile, the `tslow.cmd`
shortcut and `C:\Program Files\TSlow`. Your database and logs in `%LOCALAPPDATA%\TSlow` are left
untouched; delete that folder yourself if you want them gone.

## Upgrading from an earlier install

Early installs kept the database on the Desktop: `Desktop\tslow\data\metrics.db`. The current
version uses `%LOCALAPPDATA%\TSlow\data`. To keep your history, stop the watcher (end the **TSlow
Monitor** task), copy `metrics.db` (and any `metrics.db-wal` / `metrics.db-shm` next to it) into
`%LOCALAPPDATA%\TSlow\data`, then reinstall with `tslow install`. Re-add your apps to `[protection]`
after a reinstall if you had relied on the old built-in list.

Versions before the English rename used Italian setting names and database values. A new version
converts the database automatically the first time it opens it (keeping a `metrics.db.bak-v3`
copy next to it), but a database converted this way can no longer be read by an old installed
watcher, so reinstall first. Old `settings.toml` files keep working, except that renamed keys are
ignored (the watcher logs which ones); compare yours with `settings.default.toml`.

## Releasing (maintainers)

Bump `version` in `pyproject.toml`, commit, then push a tag `vX.Y.Z` with the same number. The
**Release** workflow runs the tests, builds the web dashboard and publishes `tslow-X.Y.Z.zip` and
`SHA256SUMS` on a GitHub Release, which is what `tslow update` downloads.

## License

MIT, see [LICENSE](LICENSE).

## Development

- Python 3.12 only, in a `.venv`; run the tests with `.venv\Scripts\python -m pytest -q`.
- Set `TSLOW_DATA_DIR` to point a development checkout at another data folder (ignored by the
  installed copy).
- Rules for contributors are in [CLAUDE.md](CLAUDE.md); design notes and the decision log are in
  [docs_obsidian/](docs_obsidian/Architecture.md).
