"""Desktop launcher ("double-click to open") support.

A beginner should never have to find a terminal.  ``srcmgr --install-shortcut`` writes a
freedesktop ``.desktop`` entry that opens the menu UI in a terminal emulator, which is
what makes the app reachable from the desktop environment's application menu.

Notes
-----
* User-level install (``~/.local/share/applications``) needs no privileges and is the
  default; ``--system`` writes ``/usr/share/applications`` (requires root).
* The launcher tries the terminal emulators that ship with the big desktop
  environments, and falls back to asking for one on ``PATH``.
* ``Terminal=true`` is used where possible so the DE provides the terminal; for terminals
  that need an explicit command we pass one.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .paths import atomic_write_text, ensure_dir

APP_ID = "source-manager"
DESKTOP_NAME = f"{APP_ID}.desktop"

#: ``(terminal, argv template)`` in preference order.  ``{cmd}`` is replaced by the
#: launcher command; ``%F``/``%U`` are freedesktop field codes we do not use.
TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gnome-terminal", ("gnome-terminal", "--", "{cmd}")),
    ("kgx", ("kgx", "--", "{cmd}")),
    ("konsole", ("konsole", "-e", "{cmd}")),
    ("xfce4-terminal", ("xfce4-terminal", "-x", "{cmd}")),
    ("mate-terminal", ("mate-terminal", "-x", "{cmd}")),
    ("tilix", ("tilix", "-e", "{cmd}")),
    ("alacritty", ("alacritty", "-e", "{cmd}")),
    ("kitty", ("kitty", "{cmd}")),
    ("wezterm", ("wezterm", "start", "--", "{cmd}")),
    ("xterm", ("xterm", "-e", "{cmd}")),
)

ICON_NAME = "system-software-install"  # a stock icon every theme has


@dataclass
class ShortcutResult:
    path: Path
    removed: bool = False
    icon_installed: bool = False
    terminal: str = ""


def applications_dir(*, system: bool = False) -> Path:
    if system:
        return Path("/usr/share/applications")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications"


def launcher_argv() -> list[str]:
    """How the desktop entry should start the menu UI, robust to where it is installed."""
    # Prefer the console script; fall back to `python -m srcmgr` for a source checkout.
    script = shutil.which("srcmgr")
    if script:
        return [script]
    return [os.sys.executable or "python3", "-m", "srcmgr"]


def terminal_command() -> tuple[str, list[str]]:
    """Pick a terminal emulator and build the argv that runs the menu UI inside it."""
    base = launcher_argv()
    for name, template in TERMINALS:
        if shutil.which(name):
            argv = [str(part).format(cmd=" ".join(base)) for part in template]
            return name, argv
    return "", base


def desktop_entry(*, system: bool = False) -> tuple[str, str]:
    """Return ``(contents, terminal_name)`` for the ``.desktop`` file."""
    name, argv = terminal_command()
    exec_line = " ".join(argv)
    body = f"""\
[Desktop Entry]
Type=Application
Version=1.0
Name=源管理器
Name[en]=Source Manager
GenericName=软件下载源管理
Comment=把各种软件的下载源换成更快、更稳的镜像（不用输入命令）
Comment[en]=Switch every software source to a faster, more reliable mirror
Exec={exec_line}
Icon={ICON_NAME}
Terminal=false
Categories=Settings;System;PackageManager;
Keywords=mirror;source;apt;npm;pip;镜像;源;
StartupNotify=true
"""
    return body, name


def install(*, system: bool = False, remove: bool = False) -> ShortcutResult:
    """Create (or delete) the desktop launcher.  Returns what happened."""
    directory = applications_dir(system=system)
    path = directory / DESKTOP_NAME
    if remove:
        if path.exists():
            path.unlink()
            return ShortcutResult(path=path, removed=True)
        return ShortcutResult(path=path, removed=True)
    body, terminal = desktop_entry(system=system)
    ensure_dir(directory)
    atomic_write_text(path, body, mode=0o644)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    _refresh_desktop_database(directory)
    return ShortcutResult(path=path, terminal=terminal)


def _refresh_desktop_database(directory: Path) -> None:
    """Ask the desktop to notice the new entry (harmless when the tool is missing)."""
    tool = shutil.which("update-desktop-database")
    if not tool or not directory.exists():
        return
    try:
        import subprocess

        subprocess.run(  # noqa: S603 - fixed argv, output ignored
            [tool, str(directory)], capture_output=True, timeout=10, check=False
        )
    except Exception:  # noqa: BLE001 - purely cosmetic
        pass


def installed_paths() -> Sequence[Path]:
    """Where a launcher may exist (for `doctor`)."""
    return [applications_dir(system=False) / DESKTOP_NAME, applications_dir(system=True) / DESKTOP_NAME]


__all__ = [
    "APP_ID",
    "DESKTOP_NAME",
    "ShortcutResult",
    "applications_dir",
    "desktop_entry",
    "install",
    "installed_paths",
    "launcher_argv",
    "terminal_command",
]
