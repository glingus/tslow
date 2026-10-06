"""protection.py su dati sintetici: nessun nome L0 deve mai essere scavalcabile."""

from __future__ import annotations

import os

from tslow.protection import ProtectionLevel, classify, is_own_process


def test_l0_system_names() -> None:
    for name in ["System", "svchost.exe", "lsass.exe", "dwm.exe", "csrss.exe", "MsMpEng.exe"]:
        result = classify(name, exe_path=r"C:\Windows\System32\x.exe", ancestor_names=[])
        assert result.level == ProtectionLevel.L0_UNTOUCHABLE, name


def test_l0_app_names_any_ancestor() -> None:
    for name in ["chrome.exe", "claude.exe", "Code.exe", "explorer.exe"]:
        result = classify(name, exe_path=None, ancestor_names=["explorer.exe"])
        assert result.level == ProtectionLevel.L0_UNTOUCHABLE, name


def test_node_child_of_claude_is_l0() -> None:
    result = classify("node.exe", exe_path=None, ancestor_names=["claude.exe", "explorer.exe"])
    assert result.level == ProtectionLevel.L0_UNTOUCHABLE


def test_node_without_claude_ancestor_is_not_l0() -> None:
    result = classify("node.exe", exe_path=None, ancestor_names=["cmd.exe", "explorer.exe"])
    assert result.level != ProtectionLevel.L0_UNTOUCHABLE


def test_l1_descendant_of_protected_app() -> None:
    result = classify("helper.exe", exe_path=None, ancestor_names=["chrome.exe", "explorer.exe"])
    assert result.level == ProtectionLevel.L1_FREE_REIN


def test_l2_maintenance_processes() -> None:
    for name in ["SearchIndexer.exe", "TrustedInstaller.exe", "vmmem"]:
        result = classify(name, exe_path=None, ancestor_names=[])
        assert result.level == ProtectionLevel.L2_SOFT_ONLY, name


def test_l3_default_for_unknown_process() -> None:
    result = classify("mygame.exe", exe_path=None, ancestor_names=["explorer.exe"])
    assert result.level == ProtectionLevel.L3_NORMAL


def test_suspicious_when_system_name_outside_system_root() -> None:
    result = classify("svchost.exe", exe_path=r"C:\Users\Public\svchost.exe", ancestor_names=[])
    assert result.level == ProtectionLevel.L0_UNTOUCHABLE
    assert result.suspicious is True


def test_not_suspicious_when_system_name_under_system_root() -> None:
    result = classify("svchost.exe", exe_path=r"C:\Windows\System32\svchost.exe", ancestor_names=[])
    assert result.suspicious is False


def test_is_own_process() -> None:
    assert is_own_process(os.getpid()) is True
    assert is_own_process(os.getpid() + 1) is False


def test_case_insensitive_and_no_exe_suffix() -> None:
    assert classify("SVCHOST", exe_path=None, ancestor_names=[]).level == ProtectionLevel.L0_UNTOUCHABLE
    assert classify("CHROME.EXE", exe_path=None, ancestor_names=[]).level == ProtectionLevel.L0_UNTOUCHABLE


# --- configurazione utente (M7.3) ---------------------------------------------------

import pytest

from tslow import protection
from tslow.protection import ProtectionConfigError, UserProtection, parse_user_protection


def test_empty_config_makes_chrome_normal() -> None:
    result = classify("chrome.exe", None, ["explorer.exe"], user=UserProtection())
    assert result.level == ProtectionLevel.L3_NORMAL


def test_user_untouchable_is_l0() -> None:
    user = UserProtection(untouchable=frozenset({"firefox"}))
    assert classify("Firefox.exe", None, [], user=user).level == ProtectionLevel.L0_UNTOUCHABLE


def test_user_untouchable_children_only_under_ancestor() -> None:
    user = UserProtection(untouchable_children=frozenset({("node", "code")}))
    assert classify("node.exe", None, ["code.exe"], user=user).level == ProtectionLevel.L0_UNTOUCHABLE
    assert classify("node.exe", None, ["cmd.exe"], user=user).level == ProtectionLevel.L3_NORMAL


def test_system_names_stay_l0_whatever_the_config() -> None:
    for user in (UserProtection(), UserProtection(free_rein_parents=frozenset({"svchost", "explorer"}))):
        for name in ("svchost.exe", "lsass.exe", "explorer.exe"):
            assert classify(name, None, ["svchost.exe"], user=user).level == ProtectionLevel.L0_UNTOUCHABLE


def test_l2_name_under_free_rein_ancestor_stays_l2() -> None:
    user = UserProtection(free_rein_parents=frozenset({"chrome"}))
    assert classify("TrustedInstaller.exe", None, ["chrome.exe"], user=user).level == ProtectionLevel.L2_SOFT_ONLY


def test_free_rein_descendant_is_l1() -> None:
    user = UserProtection(free_rein_parents=frozenset({"chrome"}))
    assert classify("helper.exe", None, ["chrome.exe"], user=user).level == ProtectionLevel.L1_FREE_REIN


def test_parse_documented_format() -> None:
    user = parse_user_protection(
        {"untouchable": ["Chrome.exe", "code"], "free_rein_parents": ["chrome"], "untouchable_children": [["node", "Code.exe"]]}
    )
    assert user.untouchable == {"chrome", "code"}
    assert user.free_rein_parents == {"chrome"}
    assert user.untouchable_children == {("node", "code")}


def test_parse_missing_section_is_empty() -> None:
    assert parse_user_protection(None) == UserProtection()
    assert parse_user_protection({}) == UserProtection()


@pytest.mark.parametrize(
    "section",
    [
        {"untouchable": "chrome"},
        {"untouchable": [1, 2]},
        {"free_rein_parents": {"a": 1}},
        {"untouchable_children": ["node"]},
        {"untouchable_children": [["node", "code", "x"]]},
        {"untouchable_children": [[1, 2]]},
        "nope",
    ],
)
def test_parse_wrong_types_raise(section) -> None:
    with pytest.raises(ProtectionConfigError):
        parse_user_protection(section)


def test_parse_error_names_the_key() -> None:
    with pytest.raises(ProtectionConfigError, match="free_rein_parents"):
        parse_user_protection({"free_rein_parents": 5})


def test_init_protection_forces_dry_run_on_config_error(monkeypatch) -> None:
    from tslow import monitor

    def boom():
        raise ProtectionConfigError("bad")

    monkeypatch.setattr(protection, "get_user_protection", boom)
    assert monitor.init_protection(dry_run=False) is True
    assert protection._user_protection == UserProtection()  # protezione vuota installata


def test_init_protection_keeps_dry_run_flag_when_valid() -> None:
    from tslow import monitor

    assert monitor.init_protection(dry_run=False) is False
    assert monitor.init_protection(dry_run=True) is True
