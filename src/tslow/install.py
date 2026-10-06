"""`tslow install` / `tslow uninstall` (M5): copia in Program Files, attivita' pianificata,
protocollo `tslow://`, fragment Windows Terminal.

Va eseguito da un terminale ELEVATO. Le funzioni che costruiscono XML/JSON/voci di registro sono
pure e testate senza toccare il sistema reale; le funzioni che eseguono davvero le modifiche
(copia file, `schtasks`, `winreg`, scrittura del fragment) sono isolate cosi' da poterle invocare
una alla volta e verificarne l'esito. Nessuna di queste funzioni va chiamata senza che l'utente
abbia dato conferma esplicita per ogni passo (regola del piano e di CLAUDE.md).
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

from tslow.cli.theme import WINDOWS_TERMINAL_COLOR_SCHEME
from tslow.paths import INSTALL_DIR

RUNTIME_DIR = INSTALL_DIR / "runtime"
TASK_NAME = "TSlow Monitor"
AUMID = "TSlow.Monitor"
PROTOCOL_SCHEME = "tslow"
WT_FRAGMENT_DIR = Path(r"C:\ProgramData\Microsoft\Windows Terminal\Fragments\TSlow")
WT_FRAGMENT_PATH = WT_FRAGMENT_DIR / "tslow.json"
WT_PROFILE_NAME = "TSlow"


def is_elevated() -> bool:
    """True se il processo corrente gira con privilegi di amministratore."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def build_scheduled_task_xml(python_exe: Path, working_dir: Path) -> str:
    """XML dell'attivita' pianificata (Task Scheduler 2.0). Pura: nessuna chiamata a `schtasks`."""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>why ts so slow? background watcher, started at logon.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>5</Priority>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>3</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{python_exe}</Command>\n"
        "      <Arguments>-I -m tslow daemon</Arguments>\n"
        f"      <WorkingDirectory>{working_dir}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def build_windows_terminal_fragment(pythonw_exe: Path) -> dict:
    """Contenuto del fragment Windows Terminal (profilo "TSlow" + schema "TSlow Blueprint"). Pura."""
    commandline = f'"{pythonw_exe}" -I -m tslow resolve'
    return {
        "profiles": [
            {
                "name": WT_PROFILE_NAME,
                "commandline": commandline,
                "colorScheme": WINDOWS_TERMINAL_COLOR_SCHEME["name"],
                "startingDirectory": str(INSTALL_DIR),
                "hidden": False,
            }
        ],
        "schemes": [WINDOWS_TERMINAL_COLOR_SCHEME],
    }


def protocol_registry_entries(pythonw_exe: Path) -> list[tuple[str, str, str]]:
    """Voci (sottochiave, nome valore, dato) da scrivere sotto `HKCU\\Software\\Classes`. Pura.

    Copre sia l'AUMID "TSlow.Monitor" (richiesto dal toast) sia il protocollo `tslow:`.
    """
    command = f'"{pythonw_exe}" -I -m tslow.launch "%1"'
    return [
        (f"AppUserModelId\\{AUMID}", "DisplayName", "TSlow Monitor"),
        (PROTOCOL_SCHEME, "", f"URL:{PROTOCOL_SCHEME} Protocol"),
        (PROTOCOL_SCHEME, "URL Protocol", ""),
        (f"{PROTOCOL_SCHEME}\\shell\\open\\command", "", command),
    ]


def _copy_runtime(dest: Path) -> None:
    """Copia l'interprete base (`sys.base_prefix`) in `dest`, escludendo `Lib/site-packages`."""
    base = Path(sys.base_prefix)

    def _ignore(dir_: str, names: list[str]) -> list[str]:
        if Path(dir_) == base / "Lib" / "site-packages":
            return names
        return []

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base, dest, ignore=_ignore)


def _install_dependencies(runtime_python: Path, project_root: Path) -> None:
    """Installa pip + dipendenze nel runtime copiato, in modalita' isolata (`-I`).

    Senza `-I`, il sito utente (`%APPDATA%\\Python\\Python312\\site-packages`) e' condiviso da
    QUALSIASI interprete Python 3.12 sulla macchina, non solo da questo runtime: un `pip list` da
    qui vedrebbe (ed eseguirebbe codice da) pacchetti installati dall'utente non elevato, la stessa
    via di escalation per cui il demone gira con `-I` (vedi "Limiti noti" in Architettura.md).
    """
    lock_file = project_root / "requirements.lock"
    subprocess.run([str(runtime_python), "-I", "-m", "ensurepip", "--upgrade"], check=True)
    subprocess.run(
        [str(runtime_python), "-I", "-m", "pip", "install", "--quiet", "--no-warn-script-location", "-r", str(lock_file)],
        check=True,
    )
    subprocess.run(
        [str(runtime_python), "-I", "-m", "pip", "install", "--quiet", "--no-warn-script-location", "--no-deps", str(project_root)],
        check=True,
    )


def _write_settings(project_root: Path) -> None:
    """Non sovrascrive mai un settings.toml esistente (contiene la lista [protection] dell'utente).

    I default distribuiti finiscono sempre accanto, in settings.default.toml, come riferimento.
    Ogni `settings.get(...)` ha un default nel codice, quindi un file piu' vecchio continua a funzionare.
    """
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shipped = project_root / "config" / "settings.toml"
    shutil.copyfile(shipped, INSTALL_DIR / "settings.default.toml")
    if not (INSTALL_DIR / "settings.toml").exists():
        shutil.copyfile(shipped, INSTALL_DIR / "settings.toml")


def build_cli_shim(python_exe: Path) -> str:
    """Contenuto di `tslow.cmd`: una riga che lancia il runtime installato. Pura."""
    return f'@"{python_exe}" -I -m tslow %*\r\n'

def _cli_shim_path() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    apps = Path(local) / "Microsoft" / "WindowsApps"  # nel PATH di default
    return apps / "tslow.cmd" if apps.is_dir() else None


def _write_cli_shim(python_exe: Path) -> None:
    path = _cli_shim_path()
    if path is not None:
        path.write_bytes(build_cli_shim(python_exe).encode("ascii"))  # bytes: nessuna traduzione dei newline



def _remove_cli_shim() -> None:
    path = _cli_shim_path()
    if path is not None and path.exists():
        path.unlink()


def _copy_web_dist(project_root: Path) -> None:
    """Copia la build del frontend (M6, `npm run build` in `web/`) se esiste.

    Non obbligatoria: se `web/dist` non c'e' ancora (frontend mai compilato), `tslow web`
    installato serve comunque le API con un messaggio al posto della pagina (vedi
    `paths.web_dist_dir` e `web/api.py`) — l'installazione non deve fallire per questo.
    """
    src = project_root / "web" / "dist"
    if not (src / "index.html").is_file():
        return
    dest = INSTALL_DIR / "web" / "dist"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _register_scheduled_task(python_exe: Path, working_dir: Path) -> None:
    xml_text = build_scheduled_task_xml(python_exe, working_dir)
    xml_path = INSTALL_DIR / "tslow_task.xml"
    xml_path.write_text(xml_text, encoding="utf-16")
    subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
        check=True,
    )


def _unregister_scheduled_task() -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)


def _register_protocol(pythonw_exe: Path) -> None:
    for subkey, value_name, data in protocol_registry_entries(pythonw_exe):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{subkey}") as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, data)


def _unregister_protocol() -> None:
    for subkey in (f"AppUserModelId\\{AUMID}", f"{PROTOCOL_SCHEME}\\shell\\open\\command", f"{PROTOCOL_SCHEME}\\shell\\open", f"{PROTOCOL_SCHEME}\\shell", PROTOCOL_SCHEME):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{subkey}")
        except OSError:
            pass


def _write_windows_terminal_fragment(pythonw_exe: Path) -> None:
    WT_FRAGMENT_DIR.mkdir(parents=True, exist_ok=True)
    fragment = build_windows_terminal_fragment(pythonw_exe)
    WT_FRAGMENT_PATH.write_text(json.dumps(fragment, indent=2), encoding="utf-8")


def _remove_windows_terminal_fragment() -> None:
    if WT_FRAGMENT_PATH.exists():
        WT_FRAGMENT_PATH.unlink()


def _end_scheduled_task() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], check=False)


def _runtime_processes() -> list:
    """Processi il cui eseguibile sta nel runtime installato (il watcher e, a volte, una `tslow` in corso)."""
    import os

    import psutil

    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "exe"]):
        exe = proc.info.get("exe")
        if exe and proc.info["pid"] != me and Path(exe).is_relative_to(RUNTIME_DIR):
            found.append(proc)
    return found


def _stop_watcher(timeout_s: float = 10.0) -> None:
    """Ferma il task e ogni processo del runtime: finche' girano tengono aperte le DLL e impediscono di sostituirlo."""
    import psutil

    _end_scheduled_task()
    procs = _runtime_processes()
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=timeout_s)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=timeout_s)


def _run_scheduled_task() -> None:
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)


def is_installed_runtime() -> bool:
    """True se questo processo gira dalla copia installata (e quindi `update` ha qualcosa da aggiornare)."""
    return (RUNTIME_DIR / "python.exe").is_file() and "Program Files" in str(Path(__file__).resolve())


def update(project_root: Path) -> None:
    """Aggiorna la copia installata con il progetto estratto in `project_root`. Richiede un terminale elevato.

    Ferma il task (altrimenti pywin32 tiene i DLL aperti), reinstalla dipendenze e pacchetto nel runtime
    esistente, rinfresca settings.default.toml (settings.toml dell'utente non si tocca), la build web e lo
    shim, e fa ripartire il task anche se l'installazione fallisce (l'eccezione risale comunque).
    Task pianificato, registro e profilo Windows Terminal restano quelli gia' installati.
    """
    if not is_elevated():
        raise PermissionError("tslow update needs an administrator terminal.")
    python_exe = RUNTIME_DIR / "python.exe"
    _stop_watcher()
    try:
        _install_dependencies(python_exe, project_root)
        _write_settings(project_root)
        _copy_web_dist(project_root)
        _write_cli_shim(python_exe)
    finally:
        _run_scheduled_task()


def install(project_root: Path) -> None:
    """Esegue TUTTI i passi di installazione. Richiede un terminale elevato."""
    if not is_elevated():
        raise PermissionError("tslow install needs an administrator terminal.")

    python_exe = RUNTIME_DIR / "python.exe"
    pythonw_exe = RUNTIME_DIR / "pythonw.exe"

    _stop_watcher()
    _copy_runtime(RUNTIME_DIR)
    _install_dependencies(python_exe, project_root)
    _write_settings(project_root)
    _copy_web_dist(project_root)
    _write_cli_shim(python_exe)
    _register_scheduled_task(pythonw_exe, INSTALL_DIR)
    _register_protocol(pythonw_exe)
    _write_windows_terminal_fragment(pythonw_exe)


def uninstall() -> None:
    """Rimuove attivita', registro e fragment, poi la copia in Program Files. I dati restano in %LOCALAPPDATA%/TSlow."""
    if not is_elevated():
        raise PermissionError("tslow uninstall needs an administrator terminal.")

    _unregister_scheduled_task()
    _stop_watcher()
    _unregister_protocol()
    _remove_windows_terminal_fragment()
    _remove_cli_shim()
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
