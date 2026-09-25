"""Shared pytest fixtures.

Every Source Manager test must be hermetic: no writes to ``$HOME``, ``/etc`` or the
network.  This module is the single place that guarantees it, so individual test
modules do not each have to remember the full list of environment overrides.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Directories that must never be touched by a test run.
PROTECTED_PATHS = (
    Path("/etc/apt"),
    Path("/etc/docker"),
    Path("/etc/flatpak"),
    Path("/etc/pip.conf"),
    Path("/etc/npmrc"),
)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch) -> dict[str, str]:
    """Point every Source Manager directory and every plugin root at ``tmp_path``.

    Returns the environment mapping so tests can pass it to subprocesses.
    """
    home = tmp_path / "home"
    config = tmp_path / "config"
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    for directory in (home, config, data, cache):
        directory.mkdir(parents=True, exist_ok=True)

    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SRCMGR_CONFIG_HOME": str(config),
        "SRCMGR_DATA_HOME": str(data),
        "SRCMGR_CACHE_HOME": str(cache),
        # per-plugin roots (system-scope managers must never see the real /etc)
        "SRCMGR_APT_ROOT": str(tmp_path / "etc-apt"),
        "SRCMGR_APT_OS_RELEASE": str(tmp_path / "etc-apt" / "os-release"),
        "SRCMGR_DOCKER_ROOT": str(tmp_path / "etc-docker"),
        "SRCMGR_FLATPAK_FAKE_DIR": str(tmp_path / "flatpak"),
        "SRCMGR_SNAP_FAKE_DIR": str(tmp_path / "snap"),
        "SRCMGR_NPMRC": str(home / ".npmrc"),
        "SRCMGR_YARNRC": str(home / ".yarnrc"),
        "SRCMGR_YARNRC_YML": str(home / ".yarnrc.yml"),
        "SRCMGR_PNPM_RC": str(home / ".npmrc"),
        "SRCMGR_PIP_CONF": str(home / ".config" / "pip" / "pip.conf"),
        "SRCMGR_CARGO_CONFIG": str(home / ".cargo" / "config.toml"),
        "SRCMGR_GO_ENV_FILE": str(home / ".config" / "go" / "env"),
        "SRCMGR_GITCONFIG": str(home / ".gitconfig"),
        "SRCMGR_MAVEN_SETTINGS": str(home / ".m2" / "settings.xml"),
        "SRCMGR_GEMRC": str(home / ".gemrc"),
        "SRCMGR_CONDA_RC": str(home / ".condarc"),
        # NO_COLOR keeps assertions on plain text simple; the health timeout is NOT set
        # here so that tests can exercise `config set health.timeout` without fighting
        # an environment override (env beats the config file by design).
        "NO_COLOR": "1",
    }
    # a fake os-release so the apt plugin has a deterministic codename
    (tmp_path / "etc-apt").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc-apt" / "os-release").write_text(
        'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n'
        'PRETTY_NAME="Test Ubuntu"\n',
        encoding="utf-8",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def cli(isolated_env):
    """Run ``srcmgr`` in-process and capture its exit code and stdout."""

    def run(*args: str, expect: int | None = None):
        from srcmgr.cli.main import main

        try:
            code = main([*args])
        except SystemExit as exc:  # argparse exits for --version/--help
            code = int(exc.code or 0)
        if expect is not None:
            assert code == expect, f"srcmgr {' '.join(args)} exited {code}, expected {expect}"
        return code

    return run


def snapshot_protected() -> dict[str, str]:
    """Cheap fingerprint of the real system config trees (for regression guards)."""
    import hashlib

    digest = hashlib.sha256()
    for root in PROTECTED_PATHS:
        if not root.exists():
            digest.update(f"{root}:missing".encode())
            continue
        if root.is_file():
            digest.update(root.read_bytes())
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                digest.update(str(path).encode())
                digest.update(path.read_bytes())
    return {"fingerprint": digest.hexdigest(), "pid": str(os.getpid())}
