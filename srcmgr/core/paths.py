"""XDG-aware filesystem locations and small IO helpers.

Everything the tool writes about *itself* (config, plugins, backups, history, cache)
lives under three roots which are overridable through environment variables.  That is
what makes the whole project safely testable without root:

===========================  ================================  ==========================
variable                     default                           contents
===========================  ================================  ==========================
``SRCMGR_CONFIG_HOME``       ``$XDG_CONFIG_HOME/source-manager``  config.yaml, plugins/
``SRCMGR_DATA_HOME``         ``$XDG_DATA_HOME/source-manager``    backups/, history/, installed plugins
``SRCMGR_CACHE_HOME``        ``$XDG_CACHE_HOME/source-manager``   health/latency cache
===========================  ================================  ==========================
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

APP_DIRNAME = "source-manager"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value or None


def config_home() -> Path:
    root = _env("SRCMGR_CONFIG_HOME") or os.path.join(
        _env("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP_DIRNAME
    )
    return Path(root).expanduser()


def data_home() -> Path:
    root = _env("SRCMGR_DATA_HOME") or os.path.join(
        _env("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP_DIRNAME
    )
    return Path(root).expanduser()


def cache_home() -> Path:
    root = _env("SRCMGR_CACHE_HOME") or os.path.join(
        _env("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP_DIRNAME
    )
    return Path(root).expanduser()


def state_home() -> Path:
    """Alias kept for symmetry; state lives in the data dir."""
    return data_home()


def user_plugins_dir() -> Path:
    return config_home() / "plugins"


def backups_dir() -> Path:
    return data_home() / "backups"


def history_dir() -> Path:
    return data_home() / "history"


def cache_dir() -> Path:
    return cache_home()


def config_file() -> Path:
    return config_home() / "config.yaml"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def is_writable(path: Path) -> bool:
    """True when we could write ``path`` (existing file, or its closest parent)."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    if target.is_dir():
        return os.access(target, os.W_OK | os.X_OK)
    return os.access(target, os.W_OK)


# ---------------------------------------------------------------------------
# YAML / JSON helpers
# ---------------------------------------------------------------------------
def load_yaml(path: Path, *, default: Any = None) -> Any:
    """Load a YAML document; ``default`` when the file does not exist."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:  # pragma: no cover - depends on malformed input
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return default if data is None else data


def dump_yaml(data: Any, path: Path, *, header: str | None = None) -> None:
    ensure_dir(path.parent)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if header:
        text = f"# {header}\n{text}"
    path.write_text(text, encoding="utf-8")


def load_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Atomic write, used for every file Source Manager modifies
# ---------------------------------------------------------------------------
def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically, preserving the original mode.

    Never leaves a half-written config file behind: the data goes to a temporary file
    in the same directory which is then ``os.replace``d over the target.
    """
    path = Path(path)
    ensure_dir(path.parent)
    if mode is None:
        if path.exists():
            mode = path.stat().st_mode & 0o7777
        else:
            mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    if mode is None:
        mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def which(program: str) -> str | None:
    return shutil.which(program)


def describe_path(path: Path) -> str:
    """Render a path for display, collapsing ``$HOME``."""
    text = str(path)
    home = os.path.expanduser("~")
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text
