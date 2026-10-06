"""Test di paths.py: soprattutto config_path(), che in modalita' installata NON deve dipendere
dalla profondita' di PACKAGE_DIR (dopo `pip install` il pacchetto finisce annidato dentro
runtime\\Lib\\site-packages\\tslow, molto piu' in profondita' che nel workspace di sviluppo)."""

from __future__ import annotations

from pathlib import Path

from tslow import paths


def test_config_path_dev_points_to_workspace_config(monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", Path(r"C:\dev\tslow\src\tslow"))
    assert paths.config_path() == Path(r"C:\dev\tslow\config\settings.toml")


def test_config_path_installed_points_to_install_dir_regardless_of_nesting(monkeypatch) -> None:
    monkeypatch.setattr(
        paths,
        "PACKAGE_DIR",
        Path(r"C:\Program Files\TSlow\runtime\Lib\site-packages\tslow"),
    )
    assert paths.config_path() == paths.INSTALL_DIR / "settings.toml"
    assert paths.config_path() == Path(r"C:\Program Files\TSlow\settings.toml")


def test_is_installed_detects_program_files_anywhere_in_path(monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", Path(r"C:\Program Files\TSlow\runtime\Lib\site-packages\tslow"))
    assert paths._is_installed() is True

    monkeypatch.setattr(paths, "PACKAGE_DIR", Path(r"C:\dev\tslow\src\tslow"))
    assert paths._is_installed() is False


def test_web_dist_dir_dev_finds_build_next_to_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", tmp_path / "src" / "tslow")
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")

    assert paths.web_dist_dir() == dist


def test_web_dist_dir_installed_looks_under_install_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", Path(r"C:\Program Files\TSlow\runtime\Lib\site-packages\tslow"))
    monkeypatch.setattr(paths, "INSTALL_DIR", tmp_path)
    (tmp_path / "web" / "dist").mkdir(parents=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("<html></html>")

    assert paths.web_dist_dir() == tmp_path / "web" / "dist"


def test_web_dist_dir_none_when_never_built(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", tmp_path / "src" / "tslow")
    assert paths.web_dist_dir() is None


_INSTALLED_PKG = Path(r"C:\Program Files\TSlow\runtime\Lib\site-packages\tslow")


def test_data_and_logs_installed_under_localappdata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", _INSTALLED_PKG)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.data_dir() == tmp_path / "TSlow" / "data"
    assert paths.logs_dir() == tmp_path / "TSlow" / "logs"
    assert paths.db_path() == tmp_path / "TSlow" / "data" / "metrics.db"


def test_installed_root_falls_back_without_localappdata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", _INSTALLED_PKG)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.data_dir() == tmp_path / "AppData" / "Local" / "TSlow" / "data"


def test_data_dir_dev_is_workspace_and_honors_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", tmp_path / "src" / "tslow")
    monkeypatch.delenv("TSLOW_DATA_DIR", raising=False)
    assert paths.data_dir() == tmp_path / "data"
    monkeypatch.setenv("TSLOW_DATA_DIR", str(tmp_path / "other"))
    assert paths.data_dir() == tmp_path / "other"


def test_data_dir_installed_ignores_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PACKAGE_DIR", _INSTALLED_PKG)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("TSLOW_DATA_DIR", str(tmp_path / "evil"))
    assert paths.data_dir() == tmp_path / "TSlow" / "data"
