"""Tests for the release plumbing: packaged-run paths, self-install, entry script.

The actual binary build lives in CI (it needs an old-glibc container), so what is covered
here is everything that can go wrong *before* that: the frozen path resolution, the
self-install that makes a downloaded file stable, the "install the shortcut" flow, and the
installer script's own syntax and failure behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from srcmgr import __version__
from srcmgr.core import paths, shortcuts

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# frozen ("packaged") runs
# ---------------------------------------------------------------------------
def test_bundle_root_prefers_the_override_then_falls_back(tmp_path, monkeypatch):
    """A PyInstaller build points the override at its unpacked tree; a checkout must
    still work when the override is missing (a stale env var must not break the program)."""
    import srcmgr.core.paths as paths_mod

    real = paths_mod.bundle_root()
    assert (real / "managers").is_dir(), real

    monkeypatch.setenv("SRCMGR_BUNDLE_ROOT", "/definitely/not/here")
    assert paths_mod.bundle_root() == real, "a bad override must fall back"

    fake = tmp_path / "unpacked"
    (fake / "bundled" / "managers").mkdir(parents=True)
    monkeypatch.setenv("SRCMGR_BUNDLE_ROOT", str(fake))
    assert paths_mod.bundle_root() == fake / "bundled"


def test_is_frozen_detects_pyinstaller(monkeypatch):
    assert paths.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.is_frozen() is True


def test_plugin_loader_uses_the_bundle_root(tmp_path, monkeypatch):
    """The shipped catalogue must be found through bundle_root(), not a hard-coded path."""
    import srcmgr.core.plugins as plugins_mod

    fake = tmp_path / "unpacked"
    (fake / "bundled" / "providers" / "demo").mkdir(parents=True)
    (fake / "bundled" / "managers").mkdir(parents=True)
    monkeypatch.setenv("SRCMGR_BUNDLE_ROOT", str(fake))
    # plugins.BUNDLED_ROOT is computed at import time, which is exactly why the entry
    # script sets the variable *before* importing anything.
    loader = plugins_mod.PluginLoader()
    assert loader.bundled_root == fake / "bundled", (
        "the loader must resolve the bundle root when it is constructed, not at import"
    )
    # An empty/incomplete bundle must be *reported*, not crash: `discover()` returns the
    # directories with present=False and a reason, which is what the UI shows.
    discovered = loader.discover()
    assert all(not manifest.present for manifest in discovered)
    assert all(manifest.warnings for manifest in discovered)


# ---------------------------------------------------------------------------
# self-install (what makes a downloaded file stable)
# ---------------------------------------------------------------------------
def test_install_self_is_a_noop_outside_a_bundle(isolated_env, monkeypatch):
    """In a source checkout or a pip install there is nothing to copy."""
    monkeypatch.setattr(shortcuts, "is_frozen", lambda: False)
    result = shortcuts.install_self()
    assert result.path == shortcuts.installed_binary()
    assert not result.path.exists(), "no binary may appear when running from source"


def test_install_self_parks_the_binary_and_is_idempotent(isolated_env, monkeypatch, tmp_path):
    """A bundled run copies itself to ~/.local/bin, and does not copy twice."""
    fake_binary = tmp_path / "downloaded-srcmgr"
    fake_binary.write_bytes(b"#!/bin/sh\necho fake\n")
    fake_binary.chmod(0o755)

    monkeypatch.setattr(shortcuts, "is_frozen", lambda: True)
    monkeypatch.setattr(shortcuts.os, "sys", type("S", (), {"executable": str(fake_binary)}))

    first = shortcuts.install_self()
    assert first.path.exists()
    assert first.path.read_bytes() == fake_binary.read_bytes()
    assert os.access(first.path, os.X_OK), "the parked copy must be executable"

    mtime = first.path.stat().st_mtime_ns
    shortcuts.install_self()  # unchanged content: no rewrite
    assert first.path.stat().st_mtime_ns == mtime

    # a new version replaces the parked copy
    fake_binary.write_bytes(b"#!/bin/sh\necho new\n")
    shortcuts.install_self()
    assert first.path.read_bytes() == fake_binary.read_bytes()


def test_launcher_prefers_the_parked_copy(isolated_env, monkeypatch, tmp_path):
    """The desktop entry must not point into ~/Downloads: it uses ~/.local/bin."""
    monkeypatch.setattr(shortcuts, "is_frozen", lambda: False)
    assert shortcuts.launcher_argv()[0] != str(shortcuts.installed_binary())

    parked = shortcuts.installed_binary()
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_text("#!/bin/sh\n")
    parked.chmod(0o755)
    assert shortcuts.launcher_argv() == [str(parked)]


def test_desktop_entry_points_at_the_parked_copy(isolated_env, monkeypatch):
    parked = shortcuts.installed_binary()
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_text("#!/bin/sh\n")
    parked.chmod(0o755)
    body, _terminal = shortcuts.desktop_entry()
    assert str(parked) in body
    assert "Type=Application" in body
    assert "Name=源管理器" in body, "the menu entry must be Chinese by default"


def test_uninstall_removes_only_what_was_installed(isolated_env):
    shortcuts.install()
    path = shortcuts.applications_dir() / shortcuts.DESKTOP_NAME
    assert path.exists()
    shortcuts.install(remove=True)
    assert not path.exists()


# ---------------------------------------------------------------------------
# the installer script
# ---------------------------------------------------------------------------
def test_installer_script_has_valid_shell_syntax():
    script = REPO / "scripts" / "install.sh"
    assert script.exists()
    completed = subprocess.run(  # noqa: S603 - fixed argv
        ["sh", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_installer_refuses_without_python_310(tmp_path):
    """No Python 3.10+ must produce advice, not a half-finished install."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("python3", "python3.10", "python3.12"):
        stub = fake_bin / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "SRCMGR_PREFIX": str(tmp_path / "prefix"),
    }
    (tmp_path / "home").mkdir()
    completed = subprocess.run(  # noqa: S603
        ["sh", str(REPO / "scripts" / "install.sh")],
        capture_output=True, text=True, env=env, cwd=str(REPO), check=False,
    )
    assert completed.returncode == 1
    assert "no Python 3.10" in completed.stderr
    assert "srcmgr-linux-x86_64" in completed.stderr, "must point at the no-Python option"
    assert not (tmp_path / "prefix").exists(), "nothing may be created"


def test_installer_uninstall_leaves_the_users_config_alone(tmp_path):
    """Uninstalling removes the program, never the user's sources/backups."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "srcmgr").write_text("#!/bin/sh\n")
    state = home / ".local" / "share" / "source-manager"
    (state / "backups").mkdir(parents=True)
    (state / "backups" / "keep-me").write_text("x")
    config = home / ".config" / "source-manager"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("general: {}\n")

    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    completed = subprocess.run(  # noqa: S603
        ["sh", str(REPO / "scripts" / "install.sh"), "--uninstall"],
        capture_output=True, text=True, env=env, cwd=str(REPO), check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (home / ".local" / "bin" / "srcmgr").exists()
    assert not state.exists()
    assert (config / "config.yaml").exists(), "user configuration must survive an uninstall"
    assert "left in place" in completed.stdout


# ---------------------------------------------------------------------------
# packaging metadata
# ---------------------------------------------------------------------------
def test_packaging_files_exist_and_are_consistent():
    assert (REPO / "packaging" / "entry.py").exists()
    spec = (REPO / "packaging" / "pyinstaller.spec").read_text(encoding="utf-8")
    assert "collect_submodules(\"srcmgr\")" in spec, (
        "dynamically loaded plugins are invisible to static analysis; this line is what "
        "makes the built binary able to import srcmgr.core.helpers"
    )
    assert '"bundled"' in spec, "the plugin catalogue must be bundled"
    assert (REPO / "packaging" / "flatpak" / "org.sourcemanager.SourceManager.yml").exists()
    assert (REPO / "packaging" / "flatpak" / "org.sourcemanager.SourceManager.metainfo.xml").exists()


def test_flatpak_manifest_keeps_system_paths_read_only():
    """The sandbox must not pretend it can write /etc: stage 1 is read-only for APT/Docker."""
    manifest = (
        REPO / "packaging" / "flatpak" / "org.sourcemanager.SourceManager.yml"
    ).read_text(encoding="utf-8")
    # Only the directives matter; the file's comments discuss the stage-2 options.
    directives = [
        line.strip()[2:]  # drop the YAML list marker, keep "--filesystem=..."
        for line in manifest.splitlines()
        if line.strip().startswith("- --")
    ]
    assert "--filesystem=/etc/apt:ro" in directives
    assert "--filesystem=/etc/docker:ro" in directives
    assert "--filesystem=/etc/apt" not in directives, "a writable /etc/apt would be a lie"
    assert "--filesystem=host" not in directives, "full host access is stage 2, not shipped"
    assert "--talk-name=org.freedesktop.Flatpak" not in directives, "host spawning is stage 2"


def test_version_is_consistent_everywhere():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    metainfo = (
        REPO / "packaging" / "flatpak" / "org.sourcemanager.SourceManager.metainfo.xml"
    ).read_text(encoding="utf-8")
    assert f'version="{__version__}"' in metainfo, "Flatpak release list must match the package"


def test_workflow_covers_tests_binary_installer_and_publish():
    workflow = (REPO / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    for job in ("test:", "wheel:", "binary:", "install-script:", "release:", "pypi:"):
        assert job in workflow, job
    assert "id-token: write" in workflow, "PyPI publishing must use Trusted Publishing"
    assert "ubuntu:22.04" in workflow, "binaries must be built against an old glibc"
