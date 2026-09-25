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

#: The developer's real home, captured before any fixture can monkeypatch it.  A test or a
#: plugin that writes here would silently change the machine running the suite (it has
#: happened: a manual debug script that forgot to redirect $HOME left a ~/.npmrc behind).
REAL_HOME = Path(os.path.expanduser("~")).resolve()


@pytest.fixture(autouse=True)
def _refuse_real_home():
    """Fail loudly when a test would write to the developer's real home.

    The check is one-directional on purpose: ``$HOME`` is normally already the real home
    when the suite starts (that is the ambient value, not a mistake) — what must never
    happen is a *test* leaving it that way and then running something that writes.  The
    tool's own roots are the ones that matter, so if they resolve inside the real home the
    test is about to touch the machine.
    """
    for key in ("SRCMGR_CONFIG_HOME", "SRCMGR_DATA_HOME", "SRCMGR_CACHE_HOME"):
        value = os.environ.get(key)
        if not value:
            continue
        try:
            resolved = Path(value).resolve()
        except OSError:  # pragma: no cover
            continue
        assert REAL_HOME not in resolved.parents and resolved != REAL_HOME, (
            f"{key}={resolved} is inside the real home ({REAL_HOME}); this test would "
            "modify the machine.  Use the isolated_env fixture (it redirects every root)."
        )
    yield


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
        # NO_COLOR keeps assertions on plain text simple.
        "NO_COLOR": "1",
        # Cap probe timeouts hard and forbid real network access: tests must never sit on
        # a dead mirror (8s each) or run `git ls-remote` (60s).  Both turned a 21s suite
        # into a 90s one; with SRCMGR_OFFLINE the same sweep takes ~1s while still
        # exercising every code path (probes report a deterministic "offline").
        "SRCMGR_HEALTH__TIMEOUT": "1",
        "SRCMGR_OFFLINE": "1",
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
        """Run one CLI command.  ``--cli`` is inserted for you: it is mandatory now that a
        bare ``srcmgr`` invocation means "open the TUI"."""
        from srcmgr.cli.main import main

        try:
            code = main(["--cli", *args])
        except SystemExit as exc:  # argparse exits for --version/--help
            code = int(exc.code or 0)
        if expect is not None:
            assert code == expect, f"srcmgr --cli {' '.join(args)} exited {code}, expected {expect}"
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
