"""The public model + plugin contracts.

Two kinds of plugins exist (the project's "two-level plugin market"):

Level 1 - **manager plugins** (:class:`ManagerPlugin`)
    Teach Source Manager *how* to operate one ecosystem's sources:
    APT, npm, Yarn, pnpm, pip, Cargo, Flatpak, Docker, Go, Git, Maven, RubyGems, ...

Level 2 - **source providers** (:class:`SourceProviderPlugin`)
    Declare *which* sources exist for one or more managers: "Official",
    "China mirrors", "USTC", "Tsinghua", "corp-internal", ...

A manager plugin never hardcodes mirror URLs and a source provider never touches the
filesystem, so third parties can publish either level independently.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class PluginType(str, enum.Enum):
    """Kind of plugin."""

    MANAGER = "manager"
    PROVIDER = "provider"


class SourceKind(str, enum.Enum):
    """What the source ultimately comes from, used for filtering/scoring."""

    OFFICIAL = "official"
    MIRROR = "mirror"
    VENDOR = "vendor"          # third-party repo such as a PPA / vendor apt repo
    PRIVATE = "private"        # corporate / self-hosted
    CUSTOM = "custom"          # user added by hand
    UNKNOWN = "unknown"


class Scope(str, enum.Enum):
    """Where a source lives / who it applies to."""

    SYSTEM = "system"          # /etc, needs root
    USER = "user"              # $HOME only
    PROJECT = "project"        # repository-local config


@dataclass
class Source:
    """An ecosystem-agnostic description of one software source.

    Plugins may attach arbitrary ecosystem specific data in :attr:`extra`; the core
    only relies on the fields declared here.
    """

    manager: str
    #: Stable synthetic id (``sha1(manager + kind + url + qualifier)[:8]``).
    id: str = ""
    #: Short human name, e.g. ``"Tsinghua"`` or ``"registry.npmjs.org"``.
    name: str = ""
    url: str = ""
    #: Repository suite / channel / distribution (APT: ``trixie``; Flatpak: branch).
    suite: str = ""
    #: APT components (``main contrib``) or an equivalent list.
    components: list[str] = field(default_factory=list)
    #: e.g. ``"deb"`` / ``"deb-src"`` for APT, ``"registry"`` for npm.
    types: list[str] = field(default_factory=list)
    architectures: list[str] = field(default_factory=list)
    #: Whether this entry is currently active for the ecosystem.
    enabled: bool = True
    kind: SourceKind = SourceKind.UNKNOWN
    scope: Scope = Scope.USER
    priority: int | None = None
    #: True for the entry that the ecosystem currently resolves packages from.
    is_default: bool = False
    #: Where the entry is defined, e.g. ``/etc/apt/sources.list.d/ubuntu.sources``.
    file: str = ""
    #: Provider plugin that declared this source, if it came from the registry.
    provider: str = ""
    #: Provider-specific key (``"tuna"``) used for enable/install actions.
    provider_key: str = ""
    description: str = ""
    homepage: str = ""
    #: True when the core had to degrade to a partial parse.
    degraded: bool = False
    #: Field that must be masked when printed (tokens, passwords).
    secret_fields: list[str] = field(default_factory=list)
    #: Health-check hints declared by the provider or manager.
    health: dict[str, Any] = field(default_factory=dict)
    #: Keys that participate in "is this the same source?" comparisons.
    identity: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.url or self.suite or "unnamed"
        if not self.id:
            self.id = compute_source_id(
                self.manager, self.url, self.suite, self.provider_key or self.name
            )

    # -- convenience ---------------------------------------------------------
    @property
    def label(self) -> str:
        """``name`` plus the default/enabled markers, for one-line listings."""
        mark = "*" if self.enabled else ""
        return f"{self.name}{mark}"

    @property
    def is_secret(self) -> bool:
        return bool(self.secret_fields)

    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, enum.Enum):
                value = value.value
            if redact and (f.name in self.secret_fields or f.name == "extra"):
                value = "<redacted>" if value not in ("", None, [], {}) else value
            out[f.name] = value
        return out


def compute_source_id(manager: str, url: str, suite: str = "", qualifier: str = "") -> str:
    """Return the stable 8-char id used to address a source from the CLI.

    Stable means: the same logical source keeps the same id across runs, machines and
    provider reloads, so it is safe to script ``srcmgr enable <id>``.
    """
    raw = "\x1f".join(
        part.strip().lower() for part in (manager, url or "", suite or "", qualifier or "")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


@dataclass
class TestResult:
    """Outcome of a health check against one source."""

    source_id: str
    manager: str
    name: str = ""
    url: str = ""
    ok: bool = False
    #: Short verdict: ``ok``, ``timeout``, ``http-503``, ``tls-error``, ``offline``, ...
    status: str = "unknown"
    http_status: int | None = None
    latency_ms: float | None = None
    tls_ok: bool | None = None
    gpg_ok: bool | None = None
    redirected_to: str = ""
    checked_at: str = ""
    method: str = ""
    detail: str = ""
    error: str = ""
    #: Individual probes (mirror may have several URLs).
    probes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def symbol(self) -> str:
        return "OK" if self.ok else "FAIL"


@dataclass
class ManagerInfo:
    """Static metadata about a manager plugin, as declared in ``plugin.yaml``."""

    name: str
    display_name: str = ""
    version: str = "0.0.0"
    description: str = ""
    scope: Scope = Scope.USER
    platforms: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    config_paths: list[str] = field(default_factory=list)
    supports: list[str] = field(default_factory=list)
    docs_url: str = ""
    plugin_dir: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name.upper()


@dataclass
class PluginManifest:
    """Parsed ``plugin.yaml`` of either plugin type."""

    name: str
    type: PluginType = PluginType.MANAGER
    display_name: str = ""
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    license: str = ""
    homepage: str = ""
    #: Provider only: manager names it contributes sources for.
    managers: list[str] = field(default_factory=list)
    #: Provider only: directory holding ``*.yaml`` source files, relative to the plugin.
    sources_dir: str = "sources"
    #: Manager only: trust level hints, e.g. ``{"writes": ["/etc/apt"]}``.
    scope: Scope = Scope.USER
    platforms: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    supports: list[str] = field(default_factory=list)
    config_paths: list[str] = field(default_factory=list)
    present: bool = False
    bundled: bool = False
    base_dir: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name

    @property
    def plugin_type(self) -> PluginType:
        return self.type


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------
@runtime_checkable
class SourceProviderPlugin(Protocol):
    """Level-2 plugin: declares sources, never touches the machine."""

    manifest: PluginManifest

    def list_sources(self, manager: str | None = None) -> list[Source]:
        """Return the sources this provider knows about."""

    def get_source(self, key: str) -> Source | None:
        """Look up one source by its provider key (``tuna``)."""


# ---------------------------------------------------------------------------
# Manager contract
# ---------------------------------------------------------------------------
@dataclass
class FileChange:
    """One file the manager wants to touch, recorded for backup/history."""

    path: str
    action: str = "modify"      # write | create | delete | rename | chmod
    existed_before: bool = True


@dataclass
class OperationResult:
    """What a mutating manager operation actually did."""

    ok: bool = True
    message: str = ""
    changed: list[str] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    backup_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ManagerPlugin:
    """Level-1 plugin: knows how to read and modify one ecosystem's sources.

    Capability flags tell the core/CLI what is implemented; unsupported operations
    raise :class:`~srcmgr.core.errors.UnsupportedOperation`.
    """

    #: Filled in by the loader; ``plugin.yaml`` data.
    manifest: PluginManifest
    #: Directory the plugin was loaded from.
    base_dir: Path
    #: Set by :class:`~srcmgr.core.runtime.SourceManagerRuntime` so a plugin can look
    #: things up in the source registry (friendly names, which mirror is which).
    runtime: Any | None = None

    #: Operations this plugin implements (subset of the canonical list).
    supports: tuple[str, ...] = (
        "detect",
        "list",
        "test",
        "backup",
        "restore",
    )

    # -- metadata ------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def registry(self) -> Any | None:
        """The source registry, when the plugin was loaded through a runtime."""
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return None
        try:
            return runtime.registry
        except Exception:  # noqa: BLE001 - metadata lookups must never break a plugin
            return None

    @property
    def info(self) -> ManagerInfo:
        return ManagerInfo(
            name=self.manifest.name,
            display_name=self.manifest.display_name,
            version=self.manifest.version,
            description=self.manifest.description,
            scope=self.manifest.scope,
            platforms=list(self.manifest.platforms),
            requires=list(self.manifest.requires),
            config_paths=list(self.manifest.config_paths),
            supports=list(self.manifest.supports or self.supports),
            plugin_dir=str(getattr(self, "base_dir", "")),
        )

    # -- lifecycle -----------------------------------------------------------
    def detect(self) -> bool:  # pragma: no cover - plugins override
        """Return True when this ecosystem is usable on this machine."""
        raise NotImplementedError

    def detect_detail(self) -> dict[str, Any]:
        """Optional richer detection info (version, config files found, ...)."""
        files = [str(p) for p in self.config_files() if p.exists()]
        return {"available": self.detect(), "files": files}

    def config_files(self) -> list[Path]:
        """Files/dirs this plugin considers its configuration (after expansion)."""
        return [Path(p).expanduser() for p in self.manifest.config_paths]

    # -- queries -------------------------------------------------------------
    def list_enabled(self) -> list[Source]:
        """Sources currently active for this ecosystem."""
        raise NotImplementedError

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Sources known to the registry, optionally filtered by manager name.

        ``manager`` is passed through because a provider may serve several managers.
        """
        raise NotImplementedError

    def current(self) -> Source | None:
        """The source the ecosystem resolves packages from right now, if knowable."""
        enabled = self.list_enabled()
        if not enabled:
            return None
        for source in enabled:
            if source.is_default:
                return source
        return enabled[0]

    # -- mutations -----------------------------------------------------------
    def add(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def remove(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def enable(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def disable(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        raise NotImplementedError

    def set_current(self, source: Source) -> OperationResult:
        """Make ``source`` the one used by default (alias of add+enable for most)."""
        if source.enabled:
            return OperationResult(ok=True, message="already current")
        return self.enable(source)

    # -- health --------------------------------------------------------------
    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe one source. Implementations should never raise for network errors."""
        from .health import probe_source

        return probe_source(source, timeout=timeout)

    def native_test(self, *, timeout: float = 60.0) -> TestResult:
        """Ecosystem-level check (``npm ping``, ``apt update``, ...)."""
        from .health import ProbeUnavailable, run_command_probe

        cmd = self.native_test_command()
        if not cmd:
            raise ProbeUnavailable(f"{self.name} has no native test command")
        return run_command_probe(self.name, cmd, timeout=timeout)

    def native_test_command(self) -> Sequence[str] | None:
        """Command used by :meth:`native_test`, e.g. ``["npm", "ping"]``."""
        return None

    # -- backup --------------------------------------------------------------
    def backup(self) -> str:
        """Snapshot the ecosystem's config files; return the backup id."""
        from .backup import BackupStore

        store = BackupStore()
        return store.snapshot(self.name, [p for p in self.config_files()])

    def restore(self, backup_id: str) -> OperationResult:
        from .backup import BackupStore

        store = BackupStore()
        return store.restore(backup_id)

    def refresh(self) -> OperationResult:
        """Post-change ecosystem refresh (``apt update``, ``flatpak update --appstream``)."""
        return OperationResult(ok=True, message="no refresh required")


__all__ = [
    "FileChange",
    "ManagerInfo",
    "ManagerPlugin",
    "OperationResult",
    "PluginManifest",
    "PluginType",
    "Scope",
    "Source",
    "SourceKind",
    "SourceProviderPlugin",
    "TestResult",
    "compute_source_id",
]
