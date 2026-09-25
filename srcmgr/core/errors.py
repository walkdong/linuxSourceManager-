"""Exception hierarchy for Source Manager.

Every error raised on purpose by the tool derives from
:class:`SourceManagerError` so the CLI can render it as a clean message instead of
a traceback (``srcmgr --debug`` re-raises).
"""

from __future__ import annotations


class SourceManagerError(Exception):
    """Base class for all Source Manager errors."""

    #: Short machine-friendly code, useful for scripting.
    code = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ConfigError(SourceManagerError):
    code = "config"


class PluginError(SourceManagerError):
    code = "plugin"


class ManifestError(PluginError):
    """A ``plugin.yaml`` is missing, malformed or inconsistent."""

    code = "manifest"


class ManagerNotFound(SourceManagerError):
    code = "manager-not-found"


class SourceNotFound(SourceManagerError):
    code = "source-not-found"


class SourceExists(SourceManagerError):
    code = "source-exists"


class UnsupportedOperation(SourceManagerError):
    """The manager plugin does not implement the requested capability."""

    code = "unsupported"


class BackupError(SourceManagerError):
    code = "backup"


class HistoryError(SourceManagerError):
    code = "history"


class PermissionDenied(SourceManagerError):
    """A system-level manager was used without sufficient privileges."""

    code = "permission"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint or "re-run the command with sudo (system-level managers write to /etc).",
        )
