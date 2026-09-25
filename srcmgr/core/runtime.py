"""The runtime: the single place where plugins, registry, health, backup and history meet.

The CLI is a thin wrapper around this object, and so would be a GUI::

    runtime = SourceManagerRuntime()
    runtime.managers()                     # detected ecosystems
    runtime.available("apt")               # registry entries
    runtime.installed("apt")               # currently active sources
    runtime.enable("apt", "tuna")          # backup -> plugin call -> history
    runtime.test("apt", scope="enabled")   # health results
    runtime.rollback("apt")                # restore last snapshot
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .api import ManagerPlugin, OperationResult, Scope, Source, TestResult
from .backup import BackupStore, utc_now
from .config import Config
from .errors import ManagerNotFound, PermissionDenied, SourceManagerError, SourceNotFound
from .health import (
    HealthCache,
    ProbeUnavailable,
    ProbeResult,
    best_source,
    probe_source,
    rank_results,
    run_command_probe,
    test_sources,
)
from .history import HistoryStore, Record
from .paths import cache_dir, is_root, is_writable
from .plugins import LoadedManager, LoadedProvider, PluginLoader
from .registry import SourceRegistry, normalize_url, source_matches
from .selection import select_source

#: Aliases accepted on the command line.
MANAGER_ALIASES = {
    "apt-get": "apt",
    "deb": "apt",
    "debian": "apt",
    "ubuntu": "apt",
    "pip3": "pip",
    "pypi": "pip",
    "python": "pip",
    "node": "npm",
    "nodejs": "npm",
    "crates": "cargo",
    "crates.io": "cargo",
    "golang": "go",
    "goproxy": "go",
    "gem": "rubygems",
    "gems": "rubygems",
    "mvn": "maven",
    "docker-ce": "docker",
    "container": "docker",
    "conda-forge": "conda",
}

#: ``${...}`` placeholders used by provider ``url_template`` entries.
_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")

MANAGER_ORDER = [
    "apt",
    "flatpak",
    "snap",
    "docker",
    "npm",
    "yarn",
    "pnpm",
    "pip",
    "conda",
    "cargo",
    "go",
    "git",
    "maven",
    "rubygems",
]


def canonical_manager(name: str) -> str:
    text = (name or "").strip().lower()
    return MANAGER_ALIASES.get(text, text)


@dataclass
class ManagerStatus:
    """Detected state of one manager on this machine."""

    name: str
    display_name: str
    version: str = ""
    description: str = ""
    scope: str = "user"
    available: bool = False
    reason: str = ""
    supports: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    writable: bool = True
    installed_count: int = 0
    error: str = ""
    bundled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "scope": self.scope,
            "available": self.available,
            "reason": self.reason,
            "supports": self.supports,
            "config_files": self.config_files,
            "writable": self.writable,
            "installed_count": self.installed_count,
            "error": self.error,
            "bundled": self.bundled,
        }


@dataclass
class ChangePlan:
    """What a rollback/fallback would do, used for ``--dry-run`` output."""

    action: str
    manager: str
    from_source: str = ""
    to_source: str = ""
    reason: str = ""
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "manager": self.manager,
            "from": self.from_source,
            "to": self.to_source,
            "reason": self.reason,
            "steps": self.steps,
        }


class SourceManagerRuntime:
    """Facade used by the CLI (and future GUI)."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        loader: PluginLoader | None = None,
        registry: SourceRegistry | None = None,
        backups: BackupStore | None = None,
        history: HistoryStore | None = None,
        cache: HealthCache | None = None,
        command: str = "",
    ) -> None:
        self.config = config or Config.load()
        self.command = command
        self._loader = loader or PluginLoader(
            trust_user_plugins=bool(self.config.get("plugins.trust_user_plugins", False))
        )
        self._managers: dict[str, LoadedManager] | None = None
        self._providers: dict[str, LoadedProvider] | None = None
        self._registry = registry
        self.backups = backups or BackupStore()
        self.history = history or HistoryStore()
        health_settings = self.config.health()
        self.cache = cache or HealthCache(ttl=float(health_settings.get("cache_ttl", 900)))

    # ------------------------------------------------------------------ setup
    @property
    def loader(self) -> PluginLoader:
        return self._loader

    def loaded_managers(self) -> dict[str, LoadedManager]:
        if self._managers is None:
            self._managers = self._loader.load_managers()
        return self._managers

    def loaded_providers(self) -> dict[str, LoadedProvider]:
        if self._providers is None:
            self._providers = self._loader.load_providers()
        return self._providers

    @property
    def registry(self) -> SourceRegistry:
        if self._registry is None:
            self._registry = SourceRegistry(self.loaded_providers(), config=self.config)
        return self._registry

    def warnings(self) -> list[str]:
        out = list(self._loader.warnings) + list(self.registry.load_warnings)
        for loaded in [*self.loaded_managers().values(), *self.loaded_providers().values()]:
            for warning in loaded.manifest.warnings:
                out.append(f"{loaded.manifest.name}: {warning}")
            if loaded.error:
                out.append(f"{loaded.manifest.name}: {loaded.error}")
            if loaded.skipped:
                out.append(f"{loaded.manifest.name}: {loaded.skipped}")
        return out

    # --------------------------------------------------------------- managers
    def manager_names(self) -> list[str]:
        names = list(self.loaded_managers())
        ordered = [n for n in MANAGER_ORDER if n in names]
        return ordered + sorted(n for n in names if n not in ordered)

    def plugin(self, name: str) -> ManagerPlugin:
        """Return a loaded manager plugin by (alias-aware) name."""
        wanted = canonical_manager(name)
        managers = self.loaded_managers()
        loaded = managers.get(wanted) or managers.get(name)
        if loaded is None:
            available = ", ".join(self.manager_names()) or "none"
            raise ManagerNotFound(
                f"unknown manager {name!r}",
                hint=f"installed managers: {available}",
            )
        if loaded.plugin is None:
            reason = loaded.error or loaded.skipped or "plugin could not be loaded"
            raise ManagerNotFound(f"manager {wanted!r} is not usable: {reason}")
        return loaded.plugin

    def status(self, name: str) -> ManagerStatus:
        loaded = self.loaded_managers()[canonical_manager(name)]
        manifest = loaded.manifest
        info = manifest
        status = ManagerStatus(
            name=manifest.name,
            display_name=manifest.display_name,
            version=manifest.version,
            description=manifest.description,
            scope=manifest.scope.value,
            supports=list(manifest.supports),
            bundled=manifest.bundled,
            error=loaded.error,
        )
        if loaded.plugin is None:
            status.reason = loaded.error or loaded.skipped or "unavailable"
            return status
        try:
            status.available = bool(loaded.plugin.detect())
        except Exception as exc:  # noqa: BLE001 - detection must never crash the listing
            status.available = False
            status.error = str(exc)
            status.reason = f"detect() failed: {exc}"
        if not status.reason and not status.available:
            status.reason = "not present on this machine"
        try:
            files = loaded.plugin.config_files()
            status.config_files = [str(p) for p in files]
            status.writable = all(is_writable(p) for p in files) if files else True
        except Exception:  # noqa: BLE001
            status.config_files = list(manifest.config_paths)
        if status.available:
            try:
                status.installed_count = len(loaded.plugin.list_enabled())
            except Exception:  # noqa: BLE001
                status.installed_count = 0
        return status

    def statuses(self, *, only_detected: bool = False) -> list[ManagerStatus]:
        out = []
        for name in self.manager_names():
            try:
                status = self.status(name)
            except KeyError:  # pragma: no cover - race with reload
                continue
            if only_detected and not status.available:
                continue
            out.append(status)
        return out

    # ---------------------------------------------------------------- sources
    def installed(self, manager: str) -> list[Source]:
        """Sources currently active for ``manager`` (may be empty)."""
        plugin = self.plugin(manager)
        try:
            return list(plugin.list_enabled())
        except Exception as exc:  # noqa: BLE001 - report, never crash
            raise SourceManagerError(f"{plugin.name}: cannot read sources: {exc}") from exc

    def available(
        self,
        manager: str,
        *,
        query: str = "",
        kinds: Sequence[str] | None = None,
        country: str | None = None,
    ) -> list[Source]:
        plugin = self.plugin(manager)
        if query or kinds or country:
            return self.registry.search(
                plugin.name, query, kinds=kinds or [], country=country
            )
        sources = self.registry.sources_for(plugin.name)
        if sources:
            return sources
        # A manager plugin may declare its own catalogue (e.g. discovered from the OS).
        return list(plugin.list_available(plugin.name))

    def resolve_available(self, manager: str, spec: str) -> Source:
        return select_source(spec, self.available(manager), what=f"{manager} source")

    def resolve_installed(self, manager: str, spec: str) -> Source:
        return select_source(spec, self.installed(manager), what=f"{manager} source")

    def current(self, manager: str) -> Source | None:
        return self.plugin(manager).current()

    def refresh_installed_flags(self, manager: str) -> list[tuple[Source, Source | None]]:
        """Registry entries paired with the installed entry they correspond to."""
        installed = []
        try:
            installed = self.installed(manager)
        except SourceManagerError:
            installed = []
        return self.registry.annotate_installed(manager, installed)

    def refresh(
        self,
        manager: str,
        *,
        dry_run: bool = False,
        runner: Callable[[Sequence[str]], OperationResult] | None = None,
    ) -> OperationResult:
        plugin = self.plugin(manager)
        if dry_run:
            return OperationResult(ok=True, message=f"would refresh {manager}")
        if runner is not None:
            result = runner(plugin)
            result.details.setdefault("manager", manager)
        else:
            try:
                result = plugin.refresh()
            except Exception as exc:  # noqa: BLE001
                result = OperationResult(ok=False, message=f"refresh failed: {exc}")
        self.history.log(
            "refresh",
            manager=manager,
            ok=result.ok,
            message=result.message,
            command=self.command,
        )
        return result

    # -------------------------------------------------------------- mutations
    def _guard(self, plugin: ManagerPlugin, source: Source | None = None) -> None:
        scope = source.scope if source is not None else plugin.manifest.scope
        if scope is Scope.SYSTEM and self.config.get("general.require_root_for_system", True):
            if not is_root():
                raise PermissionDenied(
                    f"{plugin.name} uses system-wide configuration ({scope.value})",
                )

    def _snapshot(
        self,
        plugin: ManagerPlugin,
        *,
        reason: str,
        extra_paths: Iterable[Path] = (),
        notes: Mapping[str, Any] | None = None,
    ) -> str:
        settings = self.config.backup(plugin.name)
        if not settings.get("enabled", True):
            return ""
        paths = list(plugin.config_files()) + list(extra_paths)
        if not paths:
            return ""
        backup_id = self.backups.snapshot(
            plugin.name,
            paths,
            reason=reason,
            command=self.command,
            notes=dict(notes or {}),
        )
        keep = int(settings.get("keep", 50) or 0)
        if keep:
            self.backups.prune(keep=keep)
        return backup_id

    def _record(
        self,
        action: str,
        plugin: ManagerPlugin,
        source: Source | None,
        result: OperationResult,
        backup_id: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> OperationResult:
        result.backup_id = backup_id or result.backup_id
        payload = dict(details or {})
        payload.update(result.details)
        if result.changed:
            payload.setdefault("changed", result.changed)
        self.history.log(
            action,
            manager=plugin.name,
            source=source.name if source else "",
            source_id=source.id if source else "",
            url=source.url if source else "",
            backup=result.backup_id,
            ok=result.ok,
            message=result.message,
            command=self.command,
            details=payload,
        )
        return result

    def _mutate(
        self,
        action: str,
        manager: str,
        source: Source | None,
        operation: Callable[[ManagerPlugin, Source | None], OperationResult],
        *,
        dry_run: bool = False,
        extra_paths: Iterable[Path] = (),
        details: Mapping[str, Any] | None = None,
    ) -> OperationResult:
        plugin = self.plugin(manager)
        if dry_run:
            target = f" {source.name}" if source else ""
            return OperationResult(
                ok=True,
                message=f"dry-run: would {action}{target} for {plugin.name}",
                details={"dry_run": True},
            )
        self._guard(plugin, source)
        backup_id = ""
        if self.config.backup(plugin.name).get("before_change", True):
            try:
                backup_id = self._snapshot(
                    plugin,
                    reason=f"{action} {source.name}".strip(),
                    extra_paths=extra_paths,
                    notes={"source_id": source.id if source else "", "url": source.url if source else ""},
                )
            except Exception as exc:  # noqa: BLE001 - backup failure is fatal by design
                return self._record(
                    action,
                    plugin,
                    source,
                    OperationResult(ok=False, message=f"backup failed, nothing changed: {exc}"),
                    "",
                )
        try:
            result = operation(plugin, source)
        except Exception as exc:  # noqa: BLE001 - surface plugin errors cleanly
            result = OperationResult(ok=False, message=f"{action} failed: {exc}")
        if not isinstance(result, OperationResult):  # pragma: no cover - plugin contract
            result = OperationResult(ok=True, message=str(result))
        if result.ok and backup_id:
            # Pick up files the plugin created (a new apt drop-in, a fresh .npmrc, ...)
            # so rollback can delete exactly what this change introduced.
            try:
                added = self.backups.record_paths(backup_id, plugin.config_files())
                if added:
                    result.details.setdefault("created_files", added)
            except Exception as exc:  # noqa: BLE001 - never fail an operation over bookkeeping
                result.details.setdefault("backup_warning", str(exc))
        return self._record(action, plugin, source, result, backup_id, details=details)

    def add(
        self,
        manager: str,
        spec: str | None = None,
        *,
        source: Source | None = None,
        dry_run: bool = False,
        url: str | None = None,
        suite: str | None = None,
        components: Sequence[str] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> OperationResult:
        selected = source or self._prepare_new_source(
            manager, spec, url=url, suite=suite, components=components, name=name, **kwargs
        )
        return self._mutate("add", manager, selected, lambda p, s: p.add(s), dry_run=dry_run)

    def _prepare_new_source(
        self,
        manager: str,
        spec: str | None,
        *,
        url: str | None = None,
        suite: str | None = None,
        components: Sequence[str] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> Source:
        plugin = self.plugin(manager)
        from .api import Source as SourceModel

        if spec and not url:
            try:
                selected = self.resolve_available(manager, spec)
            except SourceNotFound:
                if spec.startswith(("http://", "https://", "ppa:", "deb ")):
                    url = spec
                else:
                    raise
            else:
                return self._expand_templates(plugin, selected)
        if not url:
            raise SourceNotFound("adding a source needs either a registry name or --url")
        source = SourceModel(
            manager=plugin.name,
            name=name or url,
            url=url,
            suite=suite or "",
            components=list(components or []),
            kind=kwargs.pop("kind", None) or _infer_kind(url),
            scope=kwargs.pop("scope", None) or plugin.manifest.scope,
            extra=dict(kwargs.pop("extra", {}) or {}),
        )
        for key, value in kwargs.items():
            if hasattr(source, key) and value is not None:
                setattr(source, key, value)
        return self._expand_templates(plugin, source)

    def _expand_templates(self, plugin: ManagerPlugin, source: Source) -> Source:
        """Expand ``${codename}`` style placeholders using the manager's own values.

        Providers ship one entry per distribution family (for example
        ``url_template: https://mirrors.tuna.tsinghua.edu.cn/${distro}/``) instead of
        dozens of static URLs; the plugin is the only component that knows the local
        codename, so it supplies the substitution table through ``template_values()``.
        """
        template = str(source.extra.get("url_template") or "")
        values: dict[str, Any] = {}
        supplier = getattr(plugin, "template_values", None)
        if callable(supplier):
            try:
                values = {str(k): v for k, v in dict(supplier()).items()}
            except Exception:  # noqa: BLE001 - templating is optional
                values = {}
        expanded = template or source.url
        for key, value in values.items():
            expanded = expanded.replace("${" + key + "}", str(value))
        if expanded != source.url:
            unresolved = sorted(set(_PLACEHOLDER.findall(expanded)))
            if unresolved:
                raise SourceNotFound(
                    f"{source.name}: unresolved placeholder(s) {', '.join(unresolved)}",
                    hint="this provider targets a different distribution",
                )
            source.url = expanded
        source.extra.pop("url_template", None)
        # Managers whose active source is a single value read it from extra["value"].
        source.extra["value"] = source.url
        health_url = str(source.health.get("url", ""))
        if not health_url and not source.health.get("urls"):
            source.health["url"] = source.url
        elif health_url:
            for key, value in values.items():
                health_url = health_url.replace("${" + key + "}", str(value))
            source.health["url"] = health_url
        return source

    def remove(self, manager: str, spec: str, *, dry_run: bool = False) -> OperationResult:
        source = self.resolve_installed(manager, spec)
        return self._mutate("remove", manager, source, lambda p, s: p.remove(s), dry_run=dry_run)

    def enable(
        self,
        manager: str,
        spec: str,
        *,
        dry_run: bool = False,
        refresh: bool = False,
    ) -> OperationResult:
        source = self._resolve_for_enable(manager, spec)
        result = self._mutate("enable", manager, source, lambda p, s: p.add(s), dry_run=dry_run)
        if result.ok and refresh and not dry_run:
            refresh_result = self.refresh(manager)
            result.details["refresh"] = refresh_result.message
            if not refresh_result.ok:
                result.message += f" (refresh: {refresh_result.message})"
        return result

    def _resolve_for_enable(self, manager: str, spec: str) -> Source:
        installed = self.installed(manager)
        try:
            return select_source(spec, installed, what=f"{manager} source")
        except SourceNotFound:
            return self.resolve_available(manager, spec)

    def disable(self, manager: str, spec: str, *, dry_run: bool = False) -> OperationResult:
        source = self.resolve_installed(manager, spec)
        return self._mutate("disable", manager, source, lambda p, s: p.disable(s), dry_run=dry_run)

    def edit(
        self,
        manager: str,
        spec: str,
        changes: Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> OperationResult:
        source = self.resolve_installed(manager, spec)
        return self._mutate(
            "update",
            manager,
            source,
            lambda p, s: p.update(s, changes),
            dry_run=dry_run,
            details={"changes": dict(changes)},
        )

    def set_current(self, manager: str, spec: str, *, dry_run: bool = False) -> OperationResult:
        """Make one source the active one, disabling its installed siblings."""
        plugin = self.plugin(manager)
        target = self._resolve_for_enable(manager, spec)
        if dry_run:
            siblings = [
                s for s in self.installed(manager)
                if s.id != target.id and not source_matches(s, target)
            ]
            plan = [f"enable {target.name}"]
            plan += [f"disable {s.name}" for s in siblings]
            return OperationResult(
                ok=True, message="dry-run: " + "; ".join(plan), details={"dry_run": True, "plan": plan}
            )
        self._guard(plugin, target)
        siblings = [
            s for s in self.installed(manager)
            if s.id != target.id and not source_matches(s, target)
        ]
        results: list[str] = []
        ok = True
        result = self._mutate("enable", manager, target, lambda p, s: p.add(s))
        results.append(result.message)
        ok = ok and result.ok
        for sibling in siblings:
            outcome = self._mutate("disable", manager, sibling, lambda p, s: p.disable(s))
            results.append(outcome.message)
            ok = ok and outcome.ok
        return OperationResult(
            ok=ok,
            message="; ".join(item for item in results if item),
            backup_id=result.backup_id,
            changed=[target.url],
            details={"disabled": [s.name for s in siblings]},
        )

    # ----------------------------------------------------------------- health
    def test_source(
        self,
        manager: str,
        source: Source,
        *,
        cache: bool = True,
        native: bool = True,
    ) -> TestResult:
        plugin = self.plugin(manager)
        settings = self.config.health(manager)
        native_callable = None
        if native and plugin.native_test_command():
            native_callable = lambda: run_command_probe(  # noqa: E731
                plugin.name,
                plugin.native_test_command() or [],
                timeout=float(settings.get("native_timeout", 60.0)),
            )
        result = probe_source(
            source,
            timeout=float(settings.get("timeout", 8.0)),
            retries=int(settings.get("retries", 1)),
            check_tls=bool(settings.get("check_tls", True)),
            check_gpg=bool(settings.get("check_gpg", False)),
            native=native_callable,
        )
        if cache:
            try:
                self.cache.put(result)
            except Exception:  # noqa: BLE001 - cache is best effort
                pass
        return result

    def test(
        self,
        manager: str,
        spec: str | None = None,
        *,
        scope: str = "enabled",
        query: str = "",
        cache: bool = True,
    ) -> list[TestResult]:
        """Health check one source, the enabled set, or every registry entry."""
        plugin = self.plugin(manager)
        if spec:
            try:
                source = self.resolve_installed(manager, spec)
            except SourceNotFound:
                source = self.resolve_available(manager, spec)
            return [self.test_source(manager, source, cache=cache)]
        if scope == "available":
            sources = self.available(manager, query=query)
        else:
            sources = self.installed(manager)
        results = test_sources(
            sources,
            config=self.config,
            manager=plugin.name,
            native=self._native_probe(plugin),
        )
        if cache:
            try:
                self.cache.put_many(results)
            except Exception:  # noqa: BLE001
                pass
        return results

    def _native_probe(self, plugin: ManagerPlugin) -> Callable[[], TestResult] | None:
        command = plugin.native_test_command()
        if not command:
            return None
        return lambda: run_command_probe(plugin.name, command)

    def native_test(self, manager: str) -> TestResult:
        plugin = self.plugin(manager)
        command = plugin.native_test_command()
        if not command:
            raise ProbeUnavailable(f"{plugin.name} has no native test command")
        settings = self.config.health(manager)
        result = run_command_probe(
            plugin.name, command, timeout=float(settings.get("native_timeout", 120.0))
        )
        self.history.log(
            "test-native",
            manager=plugin.name,
            ok=result.ok,
            message=result.error or "ok",
            command=self.command,
        )
        return result

    def rank(self, results: Sequence[TestResult], sources: Sequence[Source]) -> list[dict[str, Any]]:
        by_id = {s.id: s for s in sources}
        pairs = [(by_id.get(r.source_id), r) for r in results]
        pairs = [(s, r) for s, r in pairs if s is not None]
        ranked = rank_results(pairs)  # type: ignore[arg-type]
        out = []
        for position, (source, result) in enumerate(ranked, start=1):
            out.append(
                {
                    "rank": position,
                    "id": source.id,
                    "name": source.name,
                    "url": source.url,
                    "ok": result.ok,
                    "latency_ms": result.latency_ms,
                    "status": result.status,
                    "http_status": result.http_status,
                    "tls_ok": result.tls_ok,
                    "enabled": source.enabled,
                }
            )
        return out

    # ------------------------------------------------------- backup / history
    def backup(self, manager: str, *, reason: str = "manual") -> str:
        plugin = self.plugin(manager)
        backup_id = self._snapshot(plugin, reason=reason)
        self.history.log(
            "backup",
            manager=plugin.name,
            backup=backup_id,
            message=f"manual backup {backup_id}",
            command=self.command,
        )
        return backup_id

    def restore(self, manager: str, backup_id: str) -> OperationResult:
        plugin = self.plugin(manager)
        self._guard(plugin)
        result = self.backups.restore(backup_id)
        self._record("restore", plugin, None, result, backup_id)
        if result.ok:
            refresh = plugin.refresh()
            if not refresh.ok:
                result.details["refresh"] = refresh.message
        return result

    def rollback(self, manager: str, *, dry_run: bool = False) -> OperationResult:
        """Undo the most recent change for ``manager``."""
        plugin = self.plugin(manager)
        record = next(
            (r for r in self.history.read(manager=plugin.name) if r.backup and r.action != "rollback"),
            None,
        )
        if record is None:
            return OperationResult(
                ok=False, message=f"nothing to roll back for {plugin.name} (no recorded change)"
            )
        if dry_run:
            manifest = self.backups.get(record.backup)
            steps = [f"restore {f.path}" for f in manifest.files[:20]]
            return OperationResult(
                ok=True,
                message=f"dry-run: would restore {record.backup} ({record.action} {record.source})",
                backup_id=record.backup,
                details={"dry_run": True, "plan": steps, "record": record.to_dict()},
            )
        self._guard(plugin)
        result = self.backups.restore(record.backup)
        result.message = (
            f"rolled back {record.action} {record.source} ({record.when}) using {record.backup}: "
            + result.message
        )
        self._record(
            "rollback",
            plugin,
            None,
            result,
            record.backup,
            details={"undone": record.to_dict()},
        )
        return result

    def history_records(
        self, *, manager: str | None = None, limit: int | None = None
    ) -> list[Record]:
        return self.history.read(manager=manager, limit=limit)

    def backups_list(self, manager: str | None = None) -> list[Any]:
        return self.backups.list(manager=manager)

    # --------------------------------------------------------------- fallback
    def fallback_plan(
        self,
        manager: str,
        *,
        results: Sequence[TestResult] | None = None,
        allow: bool | None = None,
    ) -> ChangePlan:
        """Work out whether the active source should be replaced, and by what."""
        plugin = self.plugin(manager)
        settings = self.config.fallback(manager)
        enabled = settings.get("enabled", False) if allow is None else allow
        installed = self.installed(manager)
        current = plugin.current()
        if current is None:
            return ChangePlan("fallback", manager, reason="no active source detected")
        if not enabled:
            return ChangePlan(
                "fallback",
                manager,
                from_source=current.name,
                reason="automatic fallback is disabled (config: fallback.enabled)",
            )

        checks = list(results) if results is not None else self.test(manager, scope="enabled")
        primary = next((r for r in checks if r.source_id == current.id), None)
        if primary is None and checks:
            primary = checks[0]
        if primary is not None and primary.ok:
            return ChangePlan(
                "fallback",
                manager,
                from_source=current.name,
                reason=f"current source is healthy ({primary.latency_ms} ms)",
            )

        candidates = [
            source for source in self.available(manager)
            if not any(source_matches(source, item) for item in installed)
        ]
        order = [str(item) for item in (settings.get("order") or [])]
        if order:
            ordered: list[Source] = []
            for key in order:
                match = next(
                    (c for c in candidates if c.provider_key == key or c.name.lower() == key.lower()),
                    None,
                )
                if match is not None:
                    ordered.append(match)
            candidates = ordered + [c for c in candidates if c not in ordered]
        else:
            country = self.config.get("registry.prefer_country", "")
            if country:
                candidates = self.registry.search(plugin.name, "", country=str(country)) or candidates

        if not candidates:
            return ChangePlan(
                "fallback",
                manager,
                from_source=current.name,
                reason="no alternative source available in the registry",
            )
        measured = test_sources(
            candidates[:12],
            config=self.config,
            manager=plugin.name,
            native=self._native_probe(plugin),
        )
        pairs = list(zip(candidates[:12], measured))
        best = best_source([(s, r) for s, r in pairs if r.ok])
        failure_reason = primary.error or primary.status if primary else "current source did not answer"
        if best is None:
            return ChangePlan(
                "fallback",
                manager,
                from_source=current.name,
                reason=f"no reachable alternative ({failure_reason})",
            )
        source, result = best
        return ChangePlan(
            "fallback",
            manager,
            from_source=current.name,
            to_source=source.name,
            reason=(
                f"{current.name} failed ({failure_reason}); "
                f"{source.name} answered in {result.latency_ms} ms"
            ),
            steps=[f"switch {plugin.name} -> {source.name} ({source.url})"],
        )

    def apply_fallback(
        self, manager: str, plan: ChangePlan | None = None, *, dry_run: bool = False
    ) -> OperationResult:
        plan = plan or self.fallback_plan(manager)
        if not plan.to_source:
            return OperationResult(ok=False, message=f"no fallback: {plan.reason}")
        if dry_run:
            return OperationResult(
                ok=True,
                message=f"dry-run: would switch {manager} from {plan.from_source} to {plan.to_source}",
                details={"dry_run": True, "plan": plan.to_dict()},
            )
        result = self.enable(manager, plan.to_source)
        if result.ok:
            result.message = (
                f"fallback: {plan.from_source} -> {plan.to_source} ({plan.reason})"
            )
            self.history.log(
                "fallback",
                manager=manager,
                source=plan.to_source,
                ok=True,
                message=result.message,
                command=self.command,
                details=plan.to_dict(),
            )
        return result

    # ----------------------------------------------------------------- doctor
    def doctor(self) -> dict[str, Any]:
        """Static self-check: plugins, config paths, permissions, providers."""
        report: dict[str, Any] = {
            "generated_at": utc_now(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "root": is_root(),
            "config_file": str(self.config.path),
            "cache_dir": str(cache_dir()),
            "managers": [],
            "providers": [
                {
                    "name": m.name,
                    "display_name": m.display_name,
                    "version": m.version,
                    "managers": m.managers,
                    "bundled": m.bundled,
                    "present": m.present,
                    "sources": len(getattr(p, "list_sources", lambda *_: [])()),
                }
                for m, p in (
                    (loaded.manifest, loaded.provider)
                    for loaded in self.loaded_providers().values()
                )
            ],
            "warnings": self.warnings(),
            "missing_commands": [],
            "unwritable_paths": [],
        }
        for name in self.manager_names():
            status = self.status(name)
            report["managers"].append(status.to_dict())
            if status.available and not status.writable:
                report["unwritable_paths"] += [p for p in status.config_files]
        for name in self.manager_names():
            loaded = self.loaded_managers()[name]
            for command in loaded.manifest.requires:
                program = command.split()[0]
                if shutil.which(program) is None:
                    report["missing_commands"].append({"manager": name, "command": program})
        return report

    # ------------------------------------------------------------------ misc
    def plugin_paths(self) -> dict[str, str]:
        """Where plugins are looked for (used by ``srcmgr plugins info``)."""
        return {
            "bundled": str(self._loader.bundled_root),
            "user": str(self._loader.user_dir),
            "extra": os.pathsep.join(str(p) for p in self._loader.extra_paths),
        }

    def all_manifests(self) -> list[Any]:
        """Every discovered plugin manifest (manager + provider, present or broken)."""
        return self._loader.discover()

    def export_state(self, manager: str | None = None) -> dict[str, Any]:
        """Machine-readable dump of managers/sources, used by ``--json``."""
        names = [canonical_manager(manager)] if manager else self.manager_names()
        out: dict[str, Any] = {"generated_at": utc_now(), "managers": {}}
        for name in names:
            try:
                plugin = self.plugin(name)
            except ManagerNotFound:
                continue
            entry: dict[str, Any] = {
                "display_name": plugin.manifest.display_name,
                "scope": plugin.manifest.scope.value,
                "config_files": [str(p) for p in plugin.config_files()],
            }
            try:
                entry["enabled"] = [s.to_dict() for s in self.installed(name)]
            except SourceManagerError as exc:
                entry["enabled"] = []
                entry["error"] = str(exc)
            out["managers"][name] = entry
        return out


def _infer_kind(url: str):
    from .api import SourceKind

    lowered = (url or "").lower()
    if "ppa.launchpadcontent.net" in lowered or "launchpad" in lowered:
        return SourceKind.VENDOR
    if any(token in lowered for token in ("mirror", "mirrors", "tuna", "ustc", "aliyun", "tencent")):
        return SourceKind.MIRROR
    if any(token in lowered for token in ("localhost", "127.0.0.1", "internal", ".corp", ".lan")):
        return SourceKind.PRIVATE
    return SourceKind.CUSTOM


__all__ = [
    "ChangePlan",
    "MANAGER_ALIASES",
    "MANAGER_ORDER",
    "ManagerStatus",
    "SourceManagerRuntime",
    "canonical_manager",
]
