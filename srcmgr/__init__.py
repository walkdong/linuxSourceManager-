"""Source Manager (``srcmgr``).

A plugin-based, cross-ecosystem *software source / registry / mirror* manager for
Linux.

The project deliberately does **not** try to be yet another "universal package
manager" (install/uninstall).  It manages the layer *below* that: the repositories,
registries and mirrors that every ecosystem (APT, npm, PyPI, Cargo, Flatpak, Docker,
...) resolves packages from.

Public API surface lives in :mod:`srcmgr.core`.
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME"]

__version__ = "0.2.0"

#: Human readable application name, used by the CLI and the config/state dirs.
APP_NAME = "source-manager"
