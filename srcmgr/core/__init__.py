"""Core engine of Source Manager.

The core is intentionally ecosystem-agnostic: everything that knows how APT or npm
actually work lives in a *manager plugin* (see :mod:`srcmgr.plugins`).

Layering::

    cli  ->  runtime (SourceManagerRuntime)
               |-- plugins     : discovery + loading of manager/provider plugins
               |-- registry    : the "Source Registry" (curated source databases)
               |-- health      : probing (HTTP/TLS/native checks) + scoring
               |-- backup      : file snapshots + restore
               |-- history     : operation log + rollback
               |-- config      : layered configuration
"""

from __future__ import annotations

from .api import (
    ManagerPlugin,
    PluginManifest,
    PluginType,
    Source,
    SourceKind,
    SourceProviderPlugin,
    TestResult,
)
from .errors import (
    BackupError,
    ConfigError,
    ManagerNotFound,
    PluginError,
    SourceManagerError,
    SourceNotFound,
    UnsupportedOperation,
)
from .runtime import SourceManagerRuntime
from .selection import select_source, selection_key

__all__ = [
    "BackupError",
    "ConfigError",
    "ManagerNotFound",
    "ManagerPlugin",
    "PluginError",
    "PluginManifest",
    "PluginType",
    "Source",
    "SourceKind",
    "SourceManagerError",
    "SourceManagerRuntime",
    "SourceNotFound",
    "SourceProviderPlugin",
    "TestResult",
    "UnsupportedOperation",
    "select_source",
    "selection_key",
]
