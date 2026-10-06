"""CLI Typer di TSlow. I comandi reali arrivano nelle milestone successive (M1+)."""

import sys

import typer

if sys.platform == "win32":
    # La console di Windows puo' avere una code page legacy (es. cp1252) che non
    # rappresenta lettere accentate o l'em dash usati nei testi in italiano.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    name="tslow",
    help='"why ts so slow?" — a background watcher for Windows that catches whatever app is choking your PC, tells you, and lets you deal with it.',
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """why ts so slow? — System Monitor & Optimizer for Windows."""


@app.command()
def version() -> None:
    """Show the tslow version."""
    from tslow.updater import current_version

    typer.echo(f'tslow {current_version()} — "why ts so slow?" (System Monitor & Optimizer for Windows)')


@app.command()
def daemon(
    foreground: bool = typer.Option(
        False, "--foreground", help="Log to this console instead of a file; stays attached to the terminal."
    ),
    dry_run: bool | None = typer.Option(
        None,
        "--dry-run/--no-dry-run",
        help="Just watch and report, never touch a process. Default: the [watcher] dry_run setting (true unless changed).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug-level logging."),
) -> None:
    """Start the background watcher. In foreground mode, Ctrl+C stops it."""
    from tslow.monitor import run_daemon

    run_daemon(foreground=foreground, dry_run=dry_run, verbose=verbose)


@app.command()
def status() -> None:
    """Show whether the background watcher is running, its latest sample, and its overhead."""
    import datetime

    from tslow import analytics, labels
    from tslow.monitor import is_daemon_running

    from tslow import database as db

    running = is_daemon_running()
    typer.echo(f"Background watcher running: {'yes' if running else 'no'}")
    from tslow.config import load_settings
    from tslow.monitor import resolve_dry_run

    mode = "dry-run (detects and notifies, never touches a process)" if resolve_dry_run(None, load_settings()) else "ACTIONS ENABLED"
    typer.echo(f"Mode set in settings.toml: {mode}")

    conn = db.connect()
    try:
        from tslow import updater

        pending = db.get_setting(conn, "update_available")
        pending_v = updater.parse_version(pending) if pending else None
        if pending_v and pending_v > (updater.parse_version(updater.current_version()) or (0, 0, 0)):
            typer.echo(f"Update available: {pending} (run `tslow update` from an administrator terminal)")
        row = conn.execute(
            "SELECT ts_ms, sampler_state, cpu_percent, ram_percent, disk_latency_ms FROM metrics_raw ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if row:
            ts = datetime.datetime.fromtimestamp(row["ts_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            cpu = f"{row['cpu_percent']:.1f}%" if row["cpu_percent"] is not None else "n/a"
            ram = f"{row['ram_percent']:.1f}%" if row["ram_percent"] is not None else "n/a"
            state = labels.label(labels.SAMPLER_STATE_LABELS, row["sampler_state"])
            typer.echo(f"Latest sample: {ts}  state={state}  CPU={cpu}  RAM={ram}")

            nasa = analytics.nasa_comparison(row["cpu_percent"], row["ram_percent"], row["disk_latency_ms"])
            typer.echo(nasa["line"])
        else:
            typer.echo("No samples recorded yet.")

        health = conn.execute(
            "SELECT daemon_cpu_percent, daemon_rss_mb, period_multiplier FROM monitor_health ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if health:
            typer.echo(
                f"Watcher overhead: CPU={health['daemon_cpu_percent']:.3f}%  "
                f"RSS={health['daemon_rss_mb']:.1f}MB  period multiplier=x{health['period_multiplier']:.2f}"
            )
        else:
            typer.echo("No overhead measurement yet (the first flush happens after 60s).")

        count = conn.execute("SELECT COUNT(*) FROM metrics_raw").fetchone()[0]
        typer.echo(f"Rows in metrics_raw: {count}")
    finally:
        conn.close()


@app.command()
def bench() -> None:
    """Measure the real cost of each collector on this PC and list available PDH counters."""
    import time

    from tslow.collectors import gpu as gpu_collector
    from tslow.collectors import pdh as pdh_collector
    from tslow.collectors import processes, system, wmi_info

    def timeit_ms(fn, n: int = 5) -> list[float]:
        return [_time_call_ms(fn) for _ in range(n)]

    def _time_call_ms(fn) -> float:
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000

    def report(label: str, times: list[float]) -> None:
        avg = sum(times) / len(times)
        typer.echo(f"  {label:<32} avg {avg:6.2f} ms  (min {min(times):.2f}, max {max(times):.2f})")

    typer.echo("Collector cost on this machine (5 samples each):")

    system.sample_cpu_percent()
    report("system.collect", timeit_ms(system.collect))

    with pdh_collector.PdhCollector() as pdh:
        pdh.sample()
        report("pdh.sample", timeit_ms(pdh.sample))
        if pdh.unavailable_counters():
            typer.echo(f"  WARNING missing PDH counters: {sorted(pdh.unavailable_counters())}")
        else:
            typer.echo("  all expected PDH counters are available.")

    with gpu_collector.GpuCollector() as gpu:
        if gpu.available():
            gpu.sample_raw()
            report("gpu.sample_raw", timeit_ms(gpu.sample_raw))
        else:
            typer.echo("  GPU Engine not available on this system.")

    report("processes.snapshot_via_ntquery", timeit_ms(processes.snapshot_via_ntquery))
    report("processes.snapshot_via_psutil (fallback)", timeit_ms(processes.snapshot_via_psutil, n=2))

    report("wmi_info.collect_inventory", [_time_call_ms(wmi_info.collect_inventory)])


@app.command()
def web(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Interface to listen on. The API has no authentication: don't expose it beyond localhost."
    ),
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser automatically."),
) -> None:
    """Start the web dashboard (FastAPI, read-only over analytics.py) and open a browser."""
    import threading
    import webbrowser

    import uvicorn

    from tslow.web.api import app as web_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo(f"WARNING: {host} is not a local interface and the API has no authentication.")

    url = f"http://{host}:{port}"
    typer.echo(f"Web dashboard on {url} (Ctrl+C to stop it).")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def resolve(
    incident: int | None = typer.Option(None, "--incident", help="Incident ID (default: the most recent open one)."),
) -> None:
    """Show an incident and ask how to handle it: [1] deny, [2] allow once, [3] always allow."""
    from tslow.cli.resolve import show_and_resolve

    show_and_resolve(incident)


@app.command()
def undo(decision_id: int = typer.Argument(..., help="Decision ID to undo (shown by 'tslow resolve').")) -> None:
    """Restore the CPU/IO priority from a previous Soft action. A Hard action can't be undone."""
    from tslow import database as db
    from tslow import optimizer

    conn = db.connect()
    try:
        result = optimizer.undo(conn, decision_id)
        typer.echo(f"Result: {result}")
    finally:
        conn.close()


@app.command()
def protection() -> None:
    """Show which processes tslow will never touch (read-only)."""
    from tslow import protection as prot
    from tslow.paths import config_path

    typer.echo(f"Settings file: {config_path()}")
    typer.echo("\nWindows system processes (L0, built in, not configurable):")
    typer.echo("  " + ", ".join(sorted(prot._L0_SYSTEM_NAMES)))
    typer.echo("\nSystem maintenance (L2, built in, soft only, never killed):")
    typer.echo("  " + ", ".join(sorted(prot._L2_NAMES)))
    try:
        user = prot.get_user_protection()
    except prot.ProtectionConfigError as exc:
        typer.echo(f"\nYour [protection] section is INVALID: {exc}")
        typer.echo("Until it is fixed the watcher runs in dry-run mode and takes no action.")
    else:
        typer.echo("\nYour apps ([protection] in the settings file):")
        typer.echo(f"  untouchable:          {', '.join(sorted(user.untouchable)) or '(none)'}")
        typer.echo(f"  free_rein_parents:    {', '.join(sorted(user.free_rein_parents)) or '(none)'}")
        pairs = ", ".join(f"{c} under {a}" for c, a in sorted(user.untouchable_children)) or "(none)"
        typer.echo(f"  untouchable_children: {pairs}")
    typer.echo("\nTo change your list, open the settings file as administrator and edit [protection].")


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Only look for a newer version and print it; installs nothing, no admin needed."),
) -> None:
    """Look for a newer release on GitHub and, from an administrator terminal, install it."""
    import tempfile
    from pathlib import Path

    from tslow import install as install_mod
    from tslow import updater
    from tslow.config import load_settings

    repo = updater.configured_repo(load_settings())
    if repo is None:
        typer.echo("Updates are not configured: set [updates] repository = \"owner/name\" in settings.toml (and enabled = true).")
        raise typer.Exit(code=1)
    try:
        release = updater.fetch_latest(repo)
    except Exception as exc:
        typer.echo(f"Could not reach GitHub: {exc}")
        raise typer.Exit(code=1)
    current = updater.current_version()
    if release is None:
        typer.echo(f"No usable release found in {repo} (needs a vX.Y.Z tag with tslow-X.Y.Z.zip and SHA256SUMS).")
        raise typer.Exit(code=1)
    if not updater.is_newer(release, current):
        typer.echo(f"You are up to date (installed {current}, latest {release.tag}).")
        return
    typer.echo(f"New version available: {release.tag} (installed {current}). Release notes: {release.page_url}")
    if check:
        typer.echo("Run `tslow update` from an administrator terminal to install it.")
        return
    if not install_mod.is_installed_runtime():
        typer.echo("This is not the installed copy (Program Files); update a dev checkout with git instead.")
        raise typer.Exit(code=1)
    if not install_mod.is_elevated():
        typer.echo("This needs an administrator terminal. Restart as admin and try again.")
        raise typer.Exit(code=1)
    typer.echo(f"This will download {release.zip_name} from {repo}, verify its SHA-256 against the release's SHA256SUMS,")
    typer.echo("stop the watcher, reinstall the dependencies and tslow in Program Files, and start the watcher again.")
    typer.echo("Your settings.toml (including [protection]) and your data are kept.")
    if not typer.confirm("Proceed with the update?"):
        raise typer.Abort()
    with tempfile.TemporaryDirectory(prefix="tslow-update-") as tmp:
        try:
            root = updater.download_release(release, Path(tmp))
        except updater.UpdateError as exc:
            typer.echo(f"Update aborted, nothing was installed: {exc}")
            raise typer.Exit(code=1)
        install_mod.update(root)
    typer.echo(f"Updated to {release.tag}. Check the new default settings in {install_mod.INSTALL_DIR / 'settings.default.toml'}.")


@app.command()
def install() -> None:
    """Install tslow (Program Files, scheduled task, tslow:// protocol, Windows Terminal). Needs admin."""
    from pathlib import Path

    from tslow import install as install_mod

    if not install_mod.is_elevated():
        typer.echo("This needs an administrator terminal. Restart as admin and try again.")
        raise typer.Exit(code=1)

    typer.echo(f"This will install into {install_mod.INSTALL_DIR}:")
    typer.echo("  - a copy of the Python interpreter (separate from your dev workspace)")
    typer.echo("  - a scheduled task ('TSlow Monitor') that starts at logon")
    typer.echo("  - the 'tslow://' protocol and an AUMID under HKCU\\Software\\Classes")
    typer.echo(f"  - a Windows Terminal profile ('TSlow') in {install_mod.WT_FRAGMENT_PATH}")
    typer.echo("  - a 'tslow.cmd' shortcut in %LOCALAPPDATA%\\Microsoft\\WindowsApps (so you can type 'tslow')")
    typer.echo("  - settings.toml is kept if it already exists (your [protection] list survives a reinstall)")
    if not typer.confirm("Proceed with installation?"):
        raise typer.Abort()

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    install_mod.install(project_root)
    typer.echo("Install complete. The watcher will start at next logon (or start it now from Task Scheduler).")


@app.command()
def uninstall() -> None:
    """Remove the tslow install (scheduled task, registry, fragment, Program Files copy). Needs admin."""
    from tslow import install as install_mod

    if not install_mod.is_elevated():
        typer.echo("This needs an administrator terminal. Restart as admin and try again.")
        raise typer.Exit(code=1)

    typer.echo(f"This will remove: the scheduled task, the HKCU registry entries, the Windows Terminal fragment, and {install_mod.INSTALL_DIR}.")
    typer.echo("Your database and logs in %LOCALAPPDATA%\\TSlow are left untouched.")
    if not typer.confirm("Proceed with uninstall?"):
        raise typer.Abort()

    install_mod.uninstall()
    typer.echo("Uninstall complete.")


rules_app = typer.Typer(help="Manage learned rules (auto deny/allow per app).")
app.add_typer(rules_app, name="rules")


@rules_app.command("list")
def rules_list() -> None:
    """List the active learned rules."""
    from tslow import database as db

    conn = db.connect()
    try:
        active_rules = db.list_user_rules(conn, only_active=True)
        if not active_rules:
            typer.echo("No active rules.")
            return
        for rule in active_rules:
            expiry = ""
            if rule.expires_at_ms is not None:
                import datetime

                expiry = f"  expires={datetime.datetime.fromtimestamp(rule.expires_at_ms / 1000):%Y-%m-%d}"
            typer.echo(
                f"#{rule.id}  {rule.app_identity}  source={rule.source}  "
                f"consecutive_denies={rule.consecutive_denies}  always_allow={rule.max_auto_action or 'no'}{expiry}"
            )
    finally:
        conn.close()


@rules_app.command("revoke")
def rules_revoke(rule_id: int = typer.Argument(..., help="Rule ID (from 'tslow rules list').")) -> None:
    """Revoke a learned rule."""
    import time as _time

    from tslow import database as db

    conn = db.connect()
    try:
        ok = db.revoke_user_rule_by_id(conn, rule_id, now_ms=int(_time.time() * 1000))
        typer.echo("Rule revoked." if ok else f"No rule with id {rule_id}.")
    finally:
        conn.close()


@rules_app.command("reset-learning")
def rules_reset_learning() -> None:
    """Delete ALL learned rules (asks for confirmation)."""
    from tslow import database as db

    if not typer.confirm("Delete every learned rule (deny/always-allow for every app)?"):
        raise typer.Abort()
    conn = db.connect()
    try:
        removed = db.reset_learning(conn)
        typer.echo(f"Removed {removed} rules.")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
