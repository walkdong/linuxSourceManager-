"""Plugin discovery, loading, installation and trust.

Three sources of plugins are scanned, in increasing priority:

1. **bundled** - shipped inside the package (``srcmgr/bundled/managers``,
   ``srcmgr/bundled/providers``), always trusted.
2. **user plugins** - ``$SRCMGR_CONFIG_HOME/plugins/<name>/``, installed by
   ``srcmgr plugins install``.  They execute Python, so they are only imported when
   trusted (config ``plugins.trust_user_plugins`` or a ``trusted: true`` marker file
   written by ``srcmgr plugins trust``).
3. ``SRCMGR_PLUGIN_PATH`` - extra directories, handy for development.

A plugin directory always contains ``plugin.yaml``; a manager plugin also has
``manager.py`` exposing a :class:`~srcmgr.core.api.ManagerPlugin` subclass, while a
provider plugin either has ``provider.py`` exposing a
:class:`~srcmgr.core.api.SourceProviderPlugin` or is pure data (``sources/*.yaml``).
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .api import (
    ManagerPlugin,
    PluginManifest,
    PluginType,
    Scope,
    Source,
    SourceKind,
    SourceProviderPlugin,
)
from .errors import ManifestError, PluginError
from .paths import ensure_dir, load_yaml, user_plugins_dir

BUNDLED_ROOT = Path(__file__).resolve().parent.parent / "bundled"
MANIFEST_NAME = "plugin.yaml"
TRUST_MARKER = ".trusted"

_KIND_ALIASES = {
    "default": SourceKind.OFFICIAL,
    "upstream": SourceKind.OFFICIAL,
    "community": SourceKind.MIRROR,
    "mirror": SourceKind.MIRROR,
    "third-party": SourceKind.VENDOR,
    "vendor": SourceKind.VENDOR,
    "internal": SourceKind.PRIVATE,
    "corp": SourceKind.PRIVATE,
}


# ---------------------------------------------------------------------------
# platform / trust helpers
# ---------------------------------------------------------------------------
def os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    path = Path("/etc/os-release")
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip().strip('"')
    return data


def platform_ids() -> set[str]:
    """Identifiers a manifest may list in ``platforms``."""
    release = os_release()
    ids = {
        "linux",
        sys.platform,
        platform.machine(),
        release.get("ID", ""),
        release.get("ID_LIKE", ""),
        *(release.get("ID_LIKE", "").split()),
    }
    return {item.lower() for item in ids if item}


def platform_matches(manifest: PluginManifest) -> bool:
    if not manifest.platforms:
        return True
    wanted = {item.lower() for item in manifest.platforms}
    return bool(wanted & platform_ids()) or "any" in wanted or "all" in wanted


def is_trusted(plugin_dir: Path, manifest: PluginManifest) -> bool:
    if manifest.bundled:
        return True
    if (Path(plugin_dir) / TRUST_MARKER).exists():
        return True
    return False


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------
def parse_manifest(path: Path, *, bundled: bool = False) -> PluginManifest:
    raw = load_yaml(path, default=None)
    if raw is None:
        raise ManifestError(f"{path}: empty manifest")
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{path}: top level must be a mapping")

    name = str(raw.get("name") or path.parent.name).strip()
    if not name or "/" in name:
        raise ManifestError(f"{path}: invalid plugin name {name!r}")

    type_text = str(raw.get("type") or ("provider" if "provider" in path.parent.name else "manager"))
    if type_text not in {t.value for t in PluginType}:
        raise ManifestError(f"{path}: unknown type {type_text!r} (manager|provider)")

    plugin_type = PluginType(type_text)
    scope_text = str(raw.get("scope") or "user").lower()
    try:
        scope = Scope(scope_text)
    except ValueError as exc:
        raise ManifestError(f"{path}: unknown scope {scope_text!r}") from exc

    if plugin_type is PluginType.PROVIDER and not raw.get("managers"):
        raise ManifestError(f"{path}: provider plugins must declare 'managers:'")

    manifest = PluginManifest(
        name=name,
        type=plugin_type,
        display_name=str(raw.get("display_name") or name),
        version=str(raw.get("version") or "0.0.0"),
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        license=str(raw.get("license") or ""),
        homepage=str(raw.get("homepage") or ""),
        managers=[str(m) for m in (raw.get("managers") or [])],
        sources_dir=str(raw.get("sources_dir") or "sources"),
        scope=scope,
        platforms=[str(p) for p in (raw.get("platforms") or [])],
        requires=[str(p) for p in (raw.get("requires") or [])],
        supports=[str(p) for p in (raw.get("supports") or [])],
        config_paths=[str(p) for p in (raw.get("config_paths") or [])],
        present=True,
        bundled=bundled,
        base_dir=str(path.parent),
        raw=dict(raw),
    )
    if "min_core_version" in raw:
        manifest.warnings.append(f"declares min_core_version {raw['min_core_version']}")
    return manifest


def _absent_manifest(directory: Path, reason: str) -> PluginManifest:
    return PluginManifest(
        name=directory.name,
        display_name=directory.name,
        present=False,
        bundled=BUNDLED_ROOT in directory.parents,
        base_dir=str(directory),
        warnings=[reason],
    )


# ---------------------------------------------------------------------------
# dynamic import of user plugins
# ---------------------------------------------------------------------------
def import_module_from(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise PluginError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_manager_class(plugin_dir: Path) -> type[ManagerPlugin]:
    """Find the :class:`ManagerPlugin` subclass inside ``manager.py``."""
    candidates = [plugin_dir / "manager.py", plugin_dir / "plugin.py"]
    for candidate in candidates:
        if candidate.exists():
            module = import_module_from(candidate, f"srcmgr_plugin_{plugin_dir.name}")
            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, ManagerPlugin)
                    and value is not ManagerPlugin
                    and getattr(value, "__module__", "") == module.__name__
                ):
                    return value
            raise PluginError(f"{candidate}: no ManagerPlugin subclass found")
    raise PluginError(f"{plugin_dir}: manager plugin has no manager.py/plugin.py")


def load_provider_class(plugin_dir: Path) -> type[SourceProviderPlugin] | None:
    for candidate in [plugin_dir / "provider.py", plugin_dir / "plugin.py"]:
        if candidate.exists():
            module = import_module_from(candidate, f"srcmgr_provider_{plugin_dir.name}")
            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and getattr(value, "__module__", "") == module.__name__
                    and hasattr(value, "list_sources")
                ):
                    return value
            return None
    return None


# ---------------------------------------------------------------------------
# provider implementations backed by YAML data
# ---------------------------------------------------------------------------
@dataclass
class DataProvider:
    """Provider plugin whose sources live in ``sources/*.yaml`` data files."""

    manifest: PluginManifest
    base_dir: Path
    _sources: list[Source]

    def list_sources(self, manager: str | None = None) -> list[Source]:
        if manager is None:
            return list(self._sources)
        return [s for s in self._sources if s.manager == manager]

    def get_source(self, key: str) -> Source | None:
        for source in self._sources:
            if source.provider_key == key:
                return source
        return None


def source_from_dict(data: Mapping[str, Any], *, manager: str, provider: str) -> Source:
    """Build a :class:`Source` from a provider YAML entry."""
    if not isinstance(data, Mapping):
        raise ManifestError(f"{provider}: source entry must be a mapping")
    key = str(data.get("key") or data.get("name") or "").strip()
    url = str(data.get("url") or data.get("baseurl") or "").strip()
    health = dict(data.get("health") or {})
    if "health_url" in data and data["health_url"]:
        health.setdefault("url", str(data["health_url"]))
    kind_text = str(data.get("kind") or "mirror").lower()
    kind = _KIND_ALIASES.get(kind_text)
    if kind is None:
        try:
            kind = SourceKind(kind_text)
        except ValueError as exc:
            raise ManifestError(f"{provider}/{key}: unknown kind {kind_text!r}") from exc
    scope = data.get("scope")
    scope_value = Scope.USER
    if scope:
        try:
            scope_value = Scope(str(scope).lower())
        except ValueError as exc:
            raise ManifestError(f"{provider}/{key}: unknown scope {scope!r}") from exc
    suite = str(data.get("suite") or data.get("channel") or "")
    source = Source(
        manager=manager,
        name=str(data.get("name") or key),
        url=url,
        suite=suite,
        components=[str(c) for c in (data.get("components") or [])],
        types=[str(t) for t in (data.get("types") or [])],
        architectures=[str(a) for a in (data.get("architectures") or [])],
        enabled=bool(data.get("enabled", False)),
        kind=kind,
        scope=scope_value,
        priority=int(data["priority"]) if data.get("priority") is not None else None,
        provider=provider,
        provider_key=key,
        description=str(data.get("description") or ""),
        homepage=str(data.get("homepage") or ""),
        health=health,
        extra=_collect_extra(data),
        secret_fields=[str(f) for f in (data.get("secret_fields") or [])],
    )
    if not source.url and not source.extra.get("url_template"):
        raise ManifestError(f"{provider}/{key}: source needs a 'url'")
    return source


#: Entry keys handled explicitly by :func:`source_from_dict`; every *other* key is passed
#: through to ``Source.extra`` so provider authors can use plain top-level fields
#: (``url_template:``, ``countries:``, ``remote_name:`` …) exactly as documented.
KNOWN_ENTRY_KEYS = {
    "key", "name", "url", "baseurl", "kind", "scope", "suite", "channel", "components",
    "types", "architectures", "enabled", "priority", "description", "homepage", "health",
    "health_url", "extra", "secret_fields",
}


def _collect_extra(data: Mapping[str, Any]) -> dict[str, Any]:
    extra = dict(data.get("extra") or {})
    for field, value in data.items():
        if field in KNOWN_ENTRY_KEYS:
            continue
        extra.setdefault(str(field), value)
    return extra


class DataProviderLoader:
    """Loads ``sources/*.yaml`` files into :class:`Source` objects."""

    @staticmethod
    def load(manifest: PluginManifest) -> DataProvider:
        base = Path(manifest.base_dir)
        sources_dir = base / manifest.sources_dir
        sources: list[Source] = []
        if not sources_dir.exists():
            raise PluginError(f"{manifest.name}: sources dir {sources_dir} missing")
        for path in sorted(sources_dir.glob("*.y*ml")):
            data = load_yaml(path, default={}) or {}
            if not isinstance(data, Mapping):
                raise ManifestError(f"{path}: top level must be a mapping")
            managers = data.get("managers") or manifest.managers
            entries = data.get("sources") or []
            if not isinstance(entries, list):
                raise ManifestError(f"{path}: 'sources' must be a list")
            for entry in entries:
                for manager in (managers or []):
                    sources.append(
                        source_from_dict(entry, manager=str(manager), provider=manifest.name)
                    )
        return DataProvider(manifest=manifest, base_dir=base, _sources=sources)


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------
@dataclass
class LoadedManager:
    manifest: PluginManifest
    plugin: ManagerPlugin | None = None
    error: str = ""
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return self.plugin is not None


@dataclass
class LoadedProvider:
    manifest: PluginManifest
    provider: Any | None = None
    error: str = ""
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return self.provider is not None


class PluginLoader:
    """Discovers manager and provider plugins."""

    def __init__(
        self,
        *,
        extra_paths: Sequence[Path] | None = None,
        user_dir: Path | None = None,
        bundled_root: Path | None = None,
        trust_user_plugins: bool = False,
    ) -> None:
        self.user_dir = Path(user_dir) if user_dir else user_plugins_dir()
        self.bundled_root = Path(bundled_root) if bundled_root else BUNDLED_ROOT
        self.trust_user_plugins = trust_user_plugins
        extra = list(extra_paths or [])
        env_path = os.environ.get("SRCMGR_PLUGIN_PATH", "")
        extra += [Path(p) for p in env_path.split(os.pathsep) if p]
        self.extra_paths = extra
        self.warnings: list[str] = []

    # -- discovery -----------------------------------------------------------
    def _bundled_dirs(self, kind: PluginType) -> list[Path]:
        root = self.bundled_root / ("managers" if kind is PluginType.MANAGER else "providers")
        if not root.exists():
            return []
        return sorted(d for d in root.iterdir() if d.is_dir())

    def _user_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for root in [self.user_dir, *self.extra_paths]:
            if root.exists():
                dirs += sorted(d for d in root.iterdir() if d.is_dir())
        return dirs

    def discover(self, kind: PluginType | None = None) -> list[PluginManifest]:
        """Return manifests for every installed plugin (without importing code)."""
        out: list[PluginManifest] = []
        bundled_kinds = [kind] if kind else [PluginType.MANAGER, PluginType.PROVIDER]
        user_kinds = [kind] if kind else [PluginType.MANAGER, PluginType.PROVIDER]
        for plugin_kind in bundled_kinds:
            for directory in self._bundled_dirs(plugin_kind):
                out.append(self._read_manifest(directory, bundled=True))
        for plugin_kind in user_kinds:
            for directory in self._user_dirs():
                manifest = self._read_manifest(directory, bundled=False)
                if manifest.type is not plugin_kind:
                    continue
                out.append(manifest)
        return out

    def _read_manifest(self, directory: Path, *, bundled: bool) -> PluginManifest:
        path = directory / MANIFEST_NAME
        if not path.exists():
            return _absent_manifest(directory, f"missing {MANIFEST_NAME}")
        try:
            manifest = parse_manifest(path, bundled=bundled)
        except ManifestError as exc:
            return _absent_manifest(directory, str(exc))
        if not platform_matches(manifest):
            manifest.warnings.append(
                "not applicable to this platform (" + ", ".join(manifest.platforms) + ")"
            )
        return manifest

    # -- loading -------------------------------------------------------------
    def load_managers(self, *, only: Iterable[str] | None = None) -> dict[str, LoadedManager]:
        wanted = {name.lower() for name in only} if only else None
        result: dict[str, LoadedManager] = {}
        bundled = self._bundled_dirs(PluginType.MANAGER)
        for directory in [*bundled, *self._user_dirs()]:
            manifest = self._read_manifest(directory, bundled=directory in bundled)
            if manifest.type is not PluginType.MANAGER:
                continue
            if wanted and manifest.name.lower() not in wanted:
                continue
            result[manifest.name] = self._load_manager(manifest, directory)
        return result

    def _load_manager(self, manifest: PluginManifest, directory: Path) -> LoadedManager:
        if not manifest.present:
            return LoadedManager(manifest=manifest, error=manifest.warnings[0] if manifest.warnings else "invalid")
        if not platform_matches(manifest):
            return LoadedManager(manifest=manifest, skipped="platform")
        if not manifest.bundled and not (self.trust_user_plugins or (directory / TRUST_MARKER).exists()):
            return LoadedManager(
                manifest=manifest,
                skipped=f"untrusted (run: srcmgr plugins trust {manifest.name})",
            )
        try:
            cls = load_manager_class(directory)
            plugin = cls()
            plugin.manifest = manifest
            plugin.base_dir = directory
            declared = list(manifest.supports or [])
            if declared:
                plugin.supports = tuple(declared)
            return LoadedManager(manifest=manifest, plugin=plugin)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not kill the CLI
            self.warnings.append(f"{manifest.name}: {exc}")
            return LoadedManager(manifest=manifest, error=str(exc))

    def load_providers(self, *, only: Iterable[str] | None = None) -> dict[str, LoadedProvider]:
        wanted = {name.lower() for name in only} if only else None
        result: dict[str, LoadedProvider] = {}
        bundled = self._bundled_dirs(PluginType.PROVIDER)
        for directory in [*bundled, *self._user_dirs()]:
            manifest = self._read_manifest(directory, bundled=directory in bundled)
            if manifest.type is not PluginType.PROVIDER:
                continue
            if wanted and manifest.name.lower() not in wanted:
                continue
            result[manifest.name] = self._load_provider(manifest, directory)
        return result

    def _load_provider(self, manifest: PluginManifest, directory: Path) -> LoadedProvider:
        if not manifest.present:
            return LoadedProvider(manifest=manifest, error=manifest.warnings[0] if manifest.warnings else "invalid")
        if not manifest.bundled and not (self.trust_user_plugins or (directory / TRUST_MARKER).exists()):
            return LoadedProvider(
                manifest=manifest,
                skipped=f"untrusted (run: srcmgr plugins trust {manifest.name})",
            )
        try:
            cls = load_provider_class(directory)
            if cls is not None:
                provider = cls()
                if getattr(provider, "manifest", None) is None:
                    provider.manifest = manifest
                if getattr(provider, "base_dir", None) is None:
                    provider.base_dir = directory
                return LoadedProvider(manifest=manifest, provider=provider)
            return LoadedProvider(
                manifest=manifest, provider=DataProviderLoader.load(manifest)
            )
        except Exception as exc:  # noqa: BLE001 - never let a plugin break the CLI
            self.warnings.append(f"{manifest.name}: {exc}")
            return LoadedProvider(manifest=manifest, error=str(exc))


# ---------------------------------------------------------------------------
# installation / trust management
# ---------------------------------------------------------------------------
def install_plugin(
    source: str,
    *,
    config_home: Path | None = None,
    trust: bool = False,
    force: bool = False,
    name: str | None = None,
) -> Path:
    """Install a plugin from a local directory, a ``.tar.gz``/``.zip`` or a URL.

    Remote installs are only allowed when ``plugins.allow_remote_install`` is true
    (checked by the caller); the plugin is *not* trusted implicitly - it will show up
    as ``skipped: untrusted`` until ``srcmgr plugins trust <name>`` is run.
    """
    target_root = ensure_dir(config_home or user_plugins_dir())
    with _Staging(source, name=name) as staging:
        manifest_path = staging / MANIFEST_NAME
        if not manifest_path.exists():
            raise PluginError(f"{source}: no {MANIFEST_NAME} found")
        manifest = parse_manifest(manifest_path)
        destination = target_root / manifest.name
        if destination.exists() and not force:
            raise PluginError(
                f"plugin {manifest.name!r} already installed",
                hint="use --force to overwrite",
            )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging, destination)
        if trust:
            (destination / TRUST_MARKER).write_text(
                "trusted by srcmgr plugins install --trust\n", encoding="utf-8"
            )
        return destination


class _Staging:
    """Context manager yielding a local directory for a plugin spec."""

    def __init__(self, source: str, name: str | None = None) -> None:
        self.source = source
        self.name = name
        self.temp: Path | None = None

    def __enter__(self) -> Path:
        import tempfile

        source = self.source
        if source.startswith(("http://", "https://")):
            import urllib.request

            tmpdir = Path(tempfile.mkdtemp(prefix="srcmgr-plugin-"))
            self.temp = tmpdir
            archive = tmpdir / (self.name or os.path.basename(source.rstrip("/")) or "plugin")
            with urllib.request.urlopen(source, timeout=30) as response:  # noqa: S310
                archive.write_bytes(response.read())
            return self._extract(archive, tmpdir)
        path = Path(source).expanduser()
        if not path.exists():
            raise PluginError(f"{source}: not found")
        if path.is_file():
            tmpdir = Path(tempfile.mkdtemp(prefix="srcmgr-plugin-"))
            self.temp = tmpdir
            return self._extract(path, tmpdir)
        return path

    def _extract(self, archive: Path, tmpdir: Path) -> Path:
        import tarfile
        import zipfile

        destination = ensure_dir(tmpdir / "unpacked")
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, destination)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tf:
                _safe_extract_tar(tf, destination)
        else:
            raise PluginError(f"{archive}: unsupported archive format")
        # Support archives with a single top-level directory.
        entries = [p for p in destination.iterdir()]
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return destination

    def __exit__(self, *exc_info: object) -> None:
        if self.temp is not None:
            shutil.rmtree(self.temp, ignore_errors=True)


def _safe_extract_tar(tar: Any, destination: Path) -> None:
    for member in tar.getmembers():
        target = (destination / member.name).resolve()
        if not str(target).startswith(str(destination.resolve())):
            raise PluginError(f"archive member escapes destination: {member.name}")
        if member.issym() or member.islnk():
            raise PluginError(f"archive contains a link, refusing: {member.name}")
    tar.extractall(destination)  # noqa: S202 - validated above


def _safe_extract_zip(zf: Any, destination: Path) -> None:
    for info in zf.infolist():
        target = (destination / info.filename).resolve()
        if not str(target).startswith(str(destination.resolve())):
            raise PluginError(f"archive member escapes destination: {info.filename}")
    zf.extractall(destination)  # noqa: S202 - validated above


def trust_plugin(name: str, *, config_home: Path | None = None, untrust: bool = False) -> Path:
    root = config_home or user_plugins_dir()
    directory = root / name
    if not directory.exists():
        raise PluginError(f"plugin {name!r} is not installed in {root}")
    marker = directory / TRUST_MARKER
    if untrust:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text("trusted by srcmgr plugins trust\n", encoding="utf-8")
    return directory


def uninstall_plugin(name: str, *, config_home: Path | None = None) -> Path:
    root = config_home or user_plugins_dir()
    directory = root / name
    if not directory.exists():
        raise PluginError(f"plugin {name!r} is not installed in {root}")
    shutil.rmtree(directory)
    return directory


def scaffold_plugin(
    name: str,
    *,
    kind: PluginType = PluginType.MANAGER,
    target: Path | None = None,
    managers: Sequence[str] = (),
) -> Path:
    """Generate a starter plugin directory (``srcmgr plugins new``)."""
    destination = (Path(target or Path.cwd()) / name).resolve()
    if destination.exists():
        raise PluginError(f"{destination} already exists")
    ensure_dir(destination)
    if kind is PluginType.MANAGER:
        (destination / MANIFEST_NAME).write_text(
            f"""name: {name}
display_name: {name.upper()}
type: manager
version: 0.1.0
description: Describe what {name} manages.
scope: user
supports: [detect, list, test, enable, disable, add, remove, backup, restore]
config_paths: []
""",
            encoding="utf-8",
        )
        (destination / "manager.py").write_text(
            '''"""Starter manager plugin."""
from __future__ import annotations

from typing import Any

from srcmgr.core.api import ManagerPlugin, OperationResult, Source


class MyManager(ManagerPlugin):
    def detect(self) -> bool:
        return True

    def list_enabled(self) -> list[Source]:
        return []

    def list_available(self, manager: str | None = None) -> list[Source]:
        return []

    def add(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def remove(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def enable(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def disable(self, source: Source) -> OperationResult:
        raise NotImplementedError

    def update(self, source: Source, changes: dict[str, Any]) -> OperationResult:
        raise NotImplementedError
''',
            encoding="utf-8",
        )
    else:
        manager_list = ", ".join(managers) if managers else "apt"
        (destination / MANIFEST_NAME).write_text(
            f"""name: {name}
display_name: {name}
type: provider
version: 0.1.0
description: Source provider for {manager_list}.
managers: [{manager_list}]
sources_dir: sources
""",
            encoding="utf-8",
        )
        ensure_dir(destination / "sources")
        (destination / "sources" / f"{name}.yaml").write_text(
            f"""managers: [{manager_list}]
sources:
  - key: example
    name: Example mirror
    kind: mirror
    url: https://mirror.example.org/
    description: Replace me.
    health:
      url: https://mirror.example.org/
""",
            encoding="utf-8",
        )
    (destination / "README.md").write_text(
        f"# {name} plugin\n\nGenerated by `srcmgr plugins new`. Install with:\n\n"
        f"    srcmgr plugins install ./{name} --trust\n",
        encoding="utf-8",
    )
    return destination


__all__ = [
    "BUNDLED_ROOT",
    "DataProvider",
    "LoadedManager",
    "LoadedProvider",
    "MANIFEST_NAME",
    "PluginLoader",
    "TRUST_MARKER",
    "install_plugin",
    "load_manager_class",
    "load_provider_class",
    "os_release",
    "parse_manifest",
    "platform_ids",
    "platform_matches",
    "scaffold_plugin",
    "source_from_dict",
    "trust_plugin",
    "uninstall_plugin",
]
