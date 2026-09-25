# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Source Manager (both flavours).

Built by ``scripts/build_binary.sh`` inside an old-glibc container so the result runs on
Ubuntu 20.04 and newer.  ``SPEC_FLAVOR`` (``menu`` or ``cli``) selects the binary name and
bakes the flavour into the program.
"""

import os

# PyInstaller injects SPECPATH / DISTPATH into the namespace, but neither is the project
# root, so the build script passes absolute paths in (env vars keep this relocatable).
FLAVOR = os.environ.get("SPEC_FLAVOR", "menu")
EXE_NAME = "srcmgr" if FLAVOR == "menu" else "srcmgr-cli"
ROOT = os.environ.get("SRCMGR_PACK_ROOT") or os.getcwd()
if not os.path.isfile(os.path.join(ROOT, "pyproject.toml")):
    raise SystemExit(f"SRCMGR_PACK_ROOT does not look like the project root: {ROOT}")

# The flavour has to be baked into the file list, not the environment: EXE(env=...) did not
# reach the frozen process, so a "--flavor cli" build behaved like the menu one.
FLAVOR_MODULE = os.path.join(SPECPATH, "_build_flavor.py")  # noqa: F821
with open(FLAVOR_MODULE, "w", encoding="utf-8") as handle:
    handle.write(f"FLAVOR = {FLAVOR!r}\n")

datas = [
    # the shipped plugin catalogue (manager plugins + source providers)
    (os.path.join(ROOT, "srcmgr", "bundled"), "bundled"),
    (os.path.join(ROOT, "LICENSE"), "."),
    (FLAVOR_MODULE, "."),
]
if os.path.isdir(os.path.join(ROOT, "docs")):
    datas.append((os.path.join(ROOT, "docs"), "docs"))

# The manager plugins are loaded with importlib from the bundled directory, so PyInstaller's
# static analysis cannot see their `from srcmgr.core import helpers` style imports.  Collect
# every first-party module explicitly - the package is small and correctness beats cleverness
# (this was caught by running the built binary: apt reported "cannot import name 'helpers'").
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

hiddenimports = [
    # Textual pulls in widgets dynamically as well.
    "textual.widgets",
    "textual.widgets._data_table",
    "textual.widgets._list_view",
    "textual.widgets._rich_log",
    *collect_submodules("srcmgr"),
]

a = Analysis(  # noqa: F821 - provided by PyInstaller
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    env={"SRCMGR_BUILD_FLAVOR": FLAVOR},
)
