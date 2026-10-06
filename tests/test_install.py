"""Test delle parti pure di install.py (M5): XML dell'attivita', fragment WT, voci di registro.

Le funzioni che toccano davvero il sistema (copia in Program Files, schtasks, winreg, fragment su
disco) non sono chiamate qui: richiedono un terminale elevato e la conferma esplicita dell'utente
prevista dal piano, quindi vengono verificate dal vivo separatamente, non nella suite automatica.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from tslow import install

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _task_xml_root(python_exe: Path, working_dir: Path) -> ET.Element:
    xml_text = install.build_scheduled_task_xml(python_exe, working_dir)
    return ET.fromstring(xml_text)


def test_scheduled_task_xml_has_logon_trigger_highest_available() -> None:
    root = _task_xml_root(Path(r"C:\Program Files\TSlow\runtime\pythonw.exe"), Path(r"C:\Program Files\TSlow"))

    assert root.find("t:Triggers/t:LogonTrigger", _NS) is not None
    principal = root.find("t:Principals/t:Principal", _NS)
    assert principal.find("t:LogonType", _NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", _NS).text == "HighestAvailable"


def test_scheduled_task_xml_settings_match_plan() -> None:
    root = _task_xml_root(Path(r"C:\Program Files\TSlow\runtime\pythonw.exe"), Path(r"C:\Program Files\TSlow"))
    settings = root.find("t:Settings", _NS)

    assert settings.find("t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"
    assert settings.find("t:DisallowStartIfOnBatteries", _NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", _NS).text == "false"
    assert settings.find("t:ExecutionTimeLimit", _NS).text == "PT0S"
    assert settings.find("t:Priority", _NS).text == "5"
    restart = settings.find("t:RestartOnFailure", _NS)
    assert restart.find("t:Interval", _NS).text == "PT1M"
    assert restart.find("t:Count", _NS).text == "3"


def test_scheduled_task_xml_action_runs_daemon_with_isolated_flag() -> None:
    python_exe = Path(r"C:\Program Files\TSlow\runtime\pythonw.exe")
    working_dir = Path(r"C:\Program Files\TSlow")
    root = _task_xml_root(python_exe, working_dir)

    exec_ = root.find("t:Actions/t:Exec", _NS)
    assert exec_.find("t:Command", _NS).text == str(python_exe)
    assert exec_.find("t:Arguments", _NS).text == "-I -m tslow daemon"
    assert exec_.find("t:WorkingDirectory", _NS).text == str(working_dir)


def test_windows_terminal_fragment_has_tslow_profile_and_blueprint_scheme() -> None:
    pythonw_exe = Path(r"C:\Program Files\TSlow\runtime\pythonw.exe")
    fragment = install.build_windows_terminal_fragment(pythonw_exe)

    profiles = fragment["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["name"] == "TSlow"
    assert str(pythonw_exe) in profiles[0]["commandline"]
    assert "tslow resolve" in profiles[0]["commandline"]
    assert profiles[0]["colorScheme"] == "TSlow Blueprint"

    schemes = fragment["schemes"]
    assert len(schemes) == 1
    assert schemes[0]["name"] == "TSlow Blueprint"
    assert schemes[0]["background"] == "#000000"
    assert schemes[0]["foreground"] == "#FFFFFF"


def test_protocol_registry_entries_cover_aumid_and_url_protocol() -> None:
    pythonw_exe = Path(r"C:\Program Files\TSlow\runtime\pythonw.exe")
    entries = install.protocol_registry_entries(pythonw_exe)
    by_subkey = {(subkey, name): data for subkey, name, data in entries}

    assert by_subkey[("AppUserModelId\\TSlow.Monitor", "DisplayName")] == "TSlow Monitor"
    assert by_subkey[("tslow", "URL Protocol")] == ""
    command = by_subkey[("tslow\\shell\\open\\command", "")]
    assert str(pythonw_exe) in command
    assert "tslow.launch" in command
    assert '"%1"' in command


def test_is_elevated_reflects_shell32_isuseranadmin(monkeypatch) -> None:
    monkeypatch.setattr(install.ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    assert install.is_elevated() is True

    monkeypatch.setattr(install.ctypes.windll.shell32, "IsUserAnAdmin", lambda: 0)
    assert install.is_elevated() is False


def test_cli_shim_is_a_single_line_calling_the_runtime_isolated() -> None:
    shim = install.build_cli_shim(Path(r"C:\Program Files\TSlow\runtime\python.exe"))
    assert shim.strip() == r'@"C:\Program Files\TSlow\runtime\python.exe" -I -m tslow %*'
    assert len(shim.strip().splitlines()) == 1


def test_write_settings_preserves_existing_and_writes_default_copy(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "settings.toml").write_text("shipped = true\n", encoding="utf-8")
    target = tmp_path / "install"
    monkeypatch.setattr(install, "INSTALL_DIR", target)

    install._write_settings(project)  # prima installazione
    assert (target / "settings.toml").read_text(encoding="utf-8") == "shipped = true\n"

    (target / "settings.toml").write_text("[protection]\nuntouchable = ['mine']\n", encoding="utf-8")
    (project / "config" / "settings.toml").write_text("shipped = 2\n", encoding="utf-8")
    install._write_settings(project)  # reinstallazione
    assert "mine" in (target / "settings.toml").read_text(encoding="utf-8")
    assert (target / "settings.default.toml").read_text(encoding="utf-8") == "shipped = 2\n"
