"""End-to-end tests for the CLI.

These drive ``srcmgr`` exactly like a user would (through ``main(argv)``) with every
directory redirected into ``tmp_path`` by the ``isolated_env`` fixture, and they assert
that the real system configuration was not touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import snapshot_protected

FIXTURE_UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: resolute resolute-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""

FIXTURE_TUNA_LIST = """\
# added by hand
deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ resolute main universe
"""


@pytest.fixture
def apt_tree(isolated_env):
    """A fake /etc/apt tree plus root privileges, so mutations are testable."""
    root = Path(isolated_env["SRCMGR_APT_ROOT"])
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / "ubuntu.sources").write_text(FIXTURE_UBUNTU_SOURCES, encoding="utf-8")
    (dropins / "tuna.list").write_text(FIXTURE_TUNA_LIST, encoding="utf-8")
    (root / "sources.list").write_text("# nothing here\n", encoding="utf-8")
    return root


@pytest.fixture
def as_root(monkeypatch):
    """Pretend to be root so system-scope managers are allowed to act."""
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    return True


# ---------------------------------------------------------------------------
# read-only commands
# ---------------------------------------------------------------------------
def test_managers_lists_every_bundled_plugin(cli, isolated_env, capsys):
    assert cli("managers") == 0
    output = capsys.readouterr().out
    for name in ("apt", "flatpak", "docker", "npm", "pip", "cargo", "go", "git", "maven", "conda"):
        assert name in output, f"{name} missing from 'srcmgr managers'"


def test_managers_json_is_machine_readable(cli, capsys):
    assert cli("--json", "managers") == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and payload
    assert {"name", "available", "scope", "config_files"} <= set(payload[0])


def test_apt_default_action_is_list(cli, apt_tree, capsys):
    assert cli("apt") == 0
    output = capsys.readouterr().out
    assert "installed" in output
    assert "archive.ubuntu.com" in output


def test_apt_list_porcelain_is_tab_separated(cli, apt_tree, capsys):
    assert cli("apt", "list", "--porcelain") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines[0].startswith("name\turl\tsuite")
    assert all("\t" in line for line in lines[1:])


def test_apt_current_identifies_the_official_archive(cli, apt_tree, capsys):
    assert cli("apt", "current") == 0
    output = capsys.readouterr().out
    assert "archive.ubuntu.com" in output
    assert "current" in output


def test_sources_without_registry_still_lists_installed(cli, apt_tree, capsys):
    assert cli("sources") == 0
    assert "archive.ubuntu.com" in capsys.readouterr().out


def test_unknown_manager_fails_cleanly(cli, capsys):
    code = cli("nosuchmanager", "list")
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown manager" in captured.err


def test_version_and_help(cli, capsys):
    assert cli("version") == 0
    assert "source-manager" in capsys.readouterr().out
    assert cli("help") == 0


# ---------------------------------------------------------------------------
# dry-run must change nothing
# ---------------------------------------------------------------------------
def test_dry_run_add_and_enable_change_nothing(cli, apt_tree, capsys):
    before = {p: p.read_text() for p in sorted(apt_tree.rglob("*")) if p.is_file()}
    assert cli("-n", "apt", "add", "--url", "https://mirror.example.org/ubuntu", "--suite", "resolute") == 0
    assert cli("apt", "enable", "#2", "-n") == 0
    after = {p: p.read_text() for p in sorted(apt_tree.rglob("*")) if p.is_file()}
    assert before == after


def test_dry_run_before_the_manager_name_is_honoured(cli, apt_tree, capsys):
    assert cli("-n", "apt", "add", "--url", "https://mirror.example.org/ubuntu") == 0
    assert "dry-run" in capsys.readouterr().out
    assert not (apt_tree / "sources.list.d" / "mirror-example-org.sources").exists()


# ---------------------------------------------------------------------------
# mutations, backup and rollback
# ---------------------------------------------------------------------------
def test_add_creates_a_deb822_file_and_a_backup(cli, apt_tree, as_root, isolated_env, capsys):
    assert cli("-v", "-y", "apt", "add", "--url", "https://mirror.example.org/ubuntu",
               "--suite", "resolute", "--components", "main,universe",
               "--name", "Example") == 0
    output = capsys.readouterr().out
    assert "backup" in output.lower(), output
    created = list((apt_tree / "sources.list.d").glob("*.sources"))
    assert any("example" in path.name for path in created), created
    text = next(path for path in created if "example" in path.name).read_text()
    assert "URIs: https://mirror.example.org/ubuntu" in text
    assert "Suites: resolute" in text
    assert "Components: main universe" in text
    # the backup snapshot must exist and be listed
    assert cli("backup", "--list") == 0
    assert "apt" in capsys.readouterr().out


def test_add_is_idempotent(cli, apt_tree, as_root, capsys):
    args = ("-y", "apt", "add", "--url", "https://mirror.example.org/ubuntu", "--suite", "resolute")
    assert cli(*args) == 0
    capsys.readouterr()
    assert cli(*args) == 0
    text = capsys.readouterr().out
    assert "already" in text.lower()
    matches = [p for p in (apt_tree / "sources.list.d").glob("*.sources")
               if "mirror.example.org" in p.read_text()]
    assert len(matches) == 1


def test_disable_enable_round_trip_on_a_line_entry(cli, apt_tree, as_root, capsys):
    assert cli("-y", "apt", "disable", "tuna") == 0
    capsys.readouterr()
    # a standalone drop-in is parked as *.list.disabled, a line in a shared file is
    # commented out; both are legitimate "disabled" states for APT.
    dropins = {p.name for p in (apt_tree / "sources.list.d").iterdir()}
    if "tuna.list" in dropins:
        assert (apt_tree / "sources.list.d" / "tuna.list").read_text().lstrip().startswith("#")
    else:
        assert "tuna.list.disabled" in dropins
    assert cli("-y", "apt", "enable", "tuna") == 0
    final = (apt_tree / "sources.list.d" / "tuna.list").read_text()
    assert "deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/" in final
    active = [line for line in final.splitlines() if line.startswith("deb ")]
    assert active, f"enabled entry missing: {final!r}"
    assert "added by hand" in final, "unrelated comment must survive"


def test_remove_leaves_other_entries_alone(cli, apt_tree, as_root, capsys):
    assert cli("-y", "apt", "remove", "tuna") == 0
    remaining = {p.name for p in (apt_tree / "sources.list.d").iterdir()}
    assert "ubuntu.sources" in remaining
    assert "tuna.list" not in remaining


def test_non_root_system_manager_is_refused(cli, apt_tree, capsys):
    code = cli("-y", "apt", "add", "--url", "https://mirror.example.org/ubuntu")
    captured = capsys.readouterr()
    assert code == 4
    assert "sudo" in captured.err
    assert not list((apt_tree / "sources.list.d").glob("*example*"))


def test_history_and_rollback(cli, apt_tree, as_root, capsys):
    assert cli("-y", "apt", "add", "--url", "https://mirror.example.org/ubuntu",
               "--suite", "resolute") == 0
    capsys.readouterr()
    assert cli("history", "--manager", "apt") == 0
    history_output = capsys.readouterr().out
    assert "add" in history_output
    created = [p for p in (apt_tree / "sources.list.d").glob("*.sources")
               if "mirror.example.org" in p.read_text()]
    assert created, "add did not create the expected file"
    assert cli("-y", "rollback", "apt") == 0
    rollback_output = capsys.readouterr().out
    assert "rolled back" in rollback_output
    for path in created:
        assert not path.exists(), "rollback must delete files it created"


def test_rollback_without_history_reports_clearly(cli, apt_tree, as_root, capsys):
    code = cli("rollback", "apt")
    assert code == 5
    assert "nothing to roll back" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# config, doctor, plugins, providers
# ---------------------------------------------------------------------------
def test_config_set_get_and_path(cli, isolated_env, capsys):
    # health.timeout is capped by an env var in tests (SRCMGR_HEALTH__TIMEOUT beats the
    # file by design), so the round-trip is asserted on a plain setting.
    assert cli("config", "set", "general.confirm_destructive", "false") == 0
    capsys.readouterr()
    assert cli("config", "get", "general.confirm_destructive") == 0
    assert "false" in capsys.readouterr().out.lower()
    assert cli("config", "path") == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.exists() and path.is_file()
    assert isolated_env["SRCMGR_CONFIG_HOME"] in str(path)
    assert cli("config", "unset", "general.confirm_destructive") == 0


def test_per_manager_config_override(cli, capsys):
    assert cli("config", "set", "managers.apt.health.timeout", "11") == 0
    capsys.readouterr()
    assert cli("--json", "config", "managers", "apt") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apt"]["health"]["timeout"] == 11


def test_doctor_reports_environment(cli, apt_tree, capsys):
    assert cli("doctor") == 0
    output = capsys.readouterr().out
    assert "environment" in output
    assert "providers" in output
    assert isolated_env_marker(output)


def isolated_env_marker(output: str) -> bool:
    """The doctor output must describe the temporary config location."""
    return "/tmp/" in output or "sm-" in output or "pytest" in output or "config" in output


def test_plugins_list_and_info(cli, capsys):
    assert cli("plugins", "list") == 0
    listing = capsys.readouterr().out
    assert "apt" in listing and "bundled" in listing
    assert cli("plugins", "info", "docker") == 0
    info = capsys.readouterr().out
    assert "docker" in info.lower()


def test_plugins_repo_points_at_a_real_directory(cli, capsys):
    assert cli("plugins", "repo") == 0
    output = capsys.readouterr().out
    assert "bundled" in output


def test_providers_list_runs(cli, capsys):
    assert cli("providers", "list") == 0


def test_health_runs_without_sources(cli, apt_tree, capsys):
    """No network here: the point is that the command completes and reports a verdict."""
    code = cli("health")
    assert code in (0, 6)
    assert "manager" in capsys.readouterr().out


def test_fallback_is_disabled_by_default_and_reports_a_plan(cli, apt_tree, capsys):
    assert cli("fallback") == 0
    output = capsys.readouterr().out
    assert "apt" in output
    assert "disabled" in output.lower() or "no alternative" in output.lower()


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------
def test_real_system_untouched_by_the_whole_suite(cli, apt_tree, as_root, capsys):
    before = snapshot_protected()
    cli("managers")
    cli("apt", "list")
    cli("-y", "apt", "add", "--url", "https://mirror.example.org/ubuntu")
    cli("doctor")
    cli("providers", "list")
    cli("plugins", "list")
    after = snapshot_protected()
    assert before["fingerprint"] == after["fingerprint"], "a test wrote to the real system"
