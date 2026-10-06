"""updater.py: release parsing, verifica SHA-256, zip sicuro, controllo periodico. Nessuna rete reale."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from tslow import database as db
from tslow import updater
from tslow.config import Settings

REPO = "someone/tslow"
BASE = f"https://github.com/{REPO}/releases/download/v0.2.0/"


def _payload(**over):
    p = {
        "tag_name": "v0.2.0", "draft": False, "prerelease": False, "html_url": f"https://github.com/{REPO}/releases/tag/v0.2.0",
        "assets": [
            {"name": "tslow-0.2.0.zip", "browser_download_url": BASE + "tslow-0.2.0.zip"},
            {"name": "SHA256SUMS", "browser_download_url": BASE + "SHA256SUMS"},
        ],
    }
    p.update(over)
    return p


def _settings(**updates):
    return Settings(raw={"updates": {"repository": REPO, **updates}})


def test_parse_version() -> None:
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("0.10.0") == (0, 10, 0)
    for bad in ("1.2", "v1.2.3-rc1", "latest", "", "1.2.3.4"):
        assert updater.parse_version(bad) is None


def test_configured_repo() -> None:
    assert updater.configured_repo(_settings()) == REPO
    assert updater.configured_repo(Settings(raw={})) is None  # segnaposto di default
    assert updater.configured_repo(_settings(repository="OWNER/tslow")) is None
    assert updater.configured_repo(_settings(enabled=False)) is None
    assert updater.configured_repo(_settings(enabled="yes")) is None
    for bad in ("nope", "a/b/c", "a b/c", 5):
        assert updater.configured_repo(_settings(repository=bad)) is None


def test_release_from_api_accepts_a_good_release() -> None:
    rel = updater.release_from_api(_payload(), REPO)
    assert rel and rel.version == (0, 2, 0) and rel.zip_name == "tslow-0.2.0.zip" and rel.zip_url.endswith("tslow-0.2.0.zip")


def test_release_from_api_rejects_bad_releases() -> None:
    assert updater.release_from_api(_payload(draft=True), REPO) is None
    assert updater.release_from_api(_payload(prerelease=True), REPO) is None
    assert updater.release_from_api(_payload(tag_name="nightly"), REPO) is None
    assert updater.release_from_api(_payload(assets=[]), REPO) is None
    assert updater.release_from_api([], REPO) is None
    foreign = _payload()
    foreign["assets"][0]["browser_download_url"] = "https://evil.example/tslow-0.2.0.zip"
    assert updater.release_from_api(foreign, REPO) is None
    other_repo = _payload()
    other_repo["assets"][1]["browser_download_url"] = "https://github.com/other/tslow/releases/download/v0.2.0/SHA256SUMS"
    assert updater.release_from_api(other_repo, REPO) is None


def test_is_newer() -> None:
    rel = updater.release_from_api(_payload(), REPO)
    assert updater.is_newer(rel, "0.1.0") and updater.is_newer(rel, "0.1.9")
    assert not updater.is_newer(rel, "0.2.0") and not updater.is_newer(rel, "1.0.0")
    assert not updater.is_newer(rel, "garbage")


def test_parse_sha256sums() -> None:
    h = "a" * 64
    text = f"{h}  tslow-0.2.0.zip\n{'B' * 64} *other.bin\nnot a line\n"
    assert updater.parse_sha256sums(text) == {"tslow-0.2.0.zip": h, "other.bin": "b" * 64}


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(routes: dict[str, bytes]):
    return lambda url, timeout=15.0: _FakeResp(routes[url])


def test_download_release_verifies_and_extracts(tmp_path: Path) -> None:
    rel = updater.release_from_api(_payload(), REPO)
    data = _zip_bytes({"pyproject.toml": "[project]\n", "src/tslow/__init__.py": ""})
    sums = f"{hashlib.sha256(data).hexdigest()}  tslow-0.2.0.zip\n".encode()
    root = updater.download_release(rel, tmp_path, opener=_opener({rel.zip_url: data, rel.sums_url: sums}))
    assert (root / "pyproject.toml").is_file()


def test_download_release_accepts_single_top_level_folder(tmp_path: Path) -> None:
    rel = updater.release_from_api(_payload(), REPO)
    data = _zip_bytes({"tslow-0.2.0/pyproject.toml": "[project]\n"})
    sums = f"{hashlib.sha256(data).hexdigest()}  tslow-0.2.0.zip\n".encode()
    root = updater.download_release(rel, tmp_path, opener=_opener({rel.zip_url: data, rel.sums_url: sums}))
    assert root.name == "tslow-0.2.0"


def test_download_release_rejects_hash_mismatch(tmp_path: Path) -> None:
    rel = updater.release_from_api(_payload(), REPO)
    data = _zip_bytes({"pyproject.toml": ""})
    sums = f"{'0' * 64}  tslow-0.2.0.zip\n".encode()
    with pytest.raises(updater.UpdateError, match="mismatch"):
        updater.download_release(rel, tmp_path, opener=_opener({rel.zip_url: data, rel.sums_url: sums}))
    assert not (tmp_path / "src").exists()


def test_download_release_rejects_missing_checksum_entry(tmp_path: Path) -> None:
    rel = updater.release_from_api(_payload(), REPO)
    sums = f"{'0' * 64}  something-else.zip\n".encode()
    with pytest.raises(updater.UpdateError, match="no entry"):
        updater.download_release(rel, tmp_path, opener=_opener({rel.zip_url: b"", rel.sums_url: sums}))


def test_safe_extract_rejects_zip_slip(tmp_path: Path) -> None:
    z = tmp_path / "evil.zip"
    z.write_bytes(_zip_bytes({"../escape.txt": "x", "pyproject.toml": ""}))
    (tmp_path / "out").mkdir()
    with pytest.raises(updater.UpdateError, match="unsafe"):
        updater.safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_requires_pyproject(tmp_path: Path) -> None:
    z = tmp_path / "x.zip"
    z.write_bytes(_zip_bytes({"readme.txt": "x"}))
    (tmp_path / "out").mkdir()
    with pytest.raises(updater.UpdateError, match="pyproject"):
        updater.safe_extract(z, tmp_path / "out")


def test_download_enforces_size_cap(tmp_path: Path) -> None:
    with pytest.raises(updater.UpdateError, match="larger"):
        updater.download("u", tmp_path / "f", max_bytes=10, opener=_opener({"u": b"x" * 100}))


def test_check_for_update_interval_and_single_notification(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "u.db")
    try:
        rel = updater.release_from_api(_payload(), REPO)
        calls: list[str] = []

        def fetch(repo):
            calls.append(repo)
            return rel

        h = 3_600_000
        s = _settings()
        assert updater.check_for_update(conn, s, 1000 * h, fetch=fetch, current="0.1.0") == rel
        assert db.get_setting(conn, "update_available") == "v0.2.0"
        assert updater.check_for_update(conn, s, 1010 * h, fetch=fetch, current="0.1.0") is None  # dentro le 24 h
        assert len(calls) == 1
        assert updater.check_for_update(conn, s, 1030 * h, fetch=fetch, current="0.1.0") is None  # gia' notificata
        assert len(calls) == 2 and db.get_setting(conn, "update_available") == "v0.2.0"
        assert updater.check_for_update(conn, s, 1100 * h, fetch=fetch, current="0.2.0") is None  # ormai aggiornato
        assert db.get_setting(conn, "update_available") == ""
    finally:
        conn.close()


def test_check_for_update_is_silent_when_unconfigured_or_offline(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "u2.db")
    try:
        def boom(repo):
            raise OSError("offline")

        assert updater.check_for_update(conn, Settings(raw={}), 10**12, fetch=lambda r: pytest.fail("no network")) is None
        assert updater.check_for_update(conn, _settings(), 10**12, fetch=boom, current="0.1.0") is None
        assert db.get_setting(conn, "update_last_check_ms") == str(10**12)  # riprova solo al prossimo intervallo
    finally:
        conn.close()


def test_shipped_settings_keep_updater_off_until_configured() -> None:
    from tslow.config import load_settings

    assert updater.configured_repo(load_settings()) is None
