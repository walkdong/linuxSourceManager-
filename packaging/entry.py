#!/usr/bin/env python3
"""Entry point for the packaged (PyInstaller) builds.

Two flavours share this file, selected by the ``SRCMGR_BUILD_FLAVOR`` environment variable
that the build script bakes into each binary:

``menu``
    the normal program: a bare invocation opens the menu UI, exactly like the checkout.
``cli``
    a one-shot-only program: a bare invocation prints the command list instead of opening
    an interactive UI.  For scripts and for people who only want the CLI.

The module header also hides the console window nobody wants to see on Windows-style
"double click a binary" workflows is not needed here (Linux), so this stays a plain shim.
"""

from __future__ import annotations

import os
import sys


def _frozen_paths() -> None:
    """Make sure a packaged build can find its bundled plugins and its own data.

    PyInstaller unpacks a one-file build into a fresh temporary directory, so the bundled
    plugin catalogue is read from there while everything the user owns keeps going to the
    usual XDG locations (``~/.config/source-manager`` etc.).  Setting this before importing
    the package keeps :data:`srcmgr.core.plugins.BUNDLED_ROOT` correct.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        os.environ.setdefault("SRCMGR_BUNDLE_ROOT", str(bundle))


def main() -> int:
    _frozen_paths()
    # The build script writes _build_flavor.py next to this file (see pyinstaller.spec);
    # relying on PyInstaller's EXE(env=...) turned out not to reach the frozen process.
    try:
        from _build_flavor import FLAVOR  # type: ignore[import-not-found]
    except ImportError:  # source checkout / unsupported build
        FLAVOR = "menu"
    flavor = os.environ.get("SRCMGR_BUILD_FLAVOR", FLAVOR).strip().lower()
    argv = list(sys.argv[1:])

    from srcmgr.cli.main import main as cli_main

    if flavor == "cli" and not argv:
        from srcmgr import APP_NAME, __version__
        from srcmgr.cli.main import build_parser

        print(f"{APP_NAME} {__version__} (命令行版)\n")
        build_parser().print_help()
        print("\n提示：这个版本只执行单条命令，例如：srcmgr --cli managers")
        return 0
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
