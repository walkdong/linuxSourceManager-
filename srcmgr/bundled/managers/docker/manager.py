"""Docker manager plugin: registry mirrors live in ``/etc/docker/daemon.json``.

Docker's own model
------------------
Docker Hub (``https://registry-1.docker.io``) is always the implicit upstream; the
``registry-mirrors`` array only *prefixes* it.  That is why a configured mirror list
never means "Hub is broken" - the Hub source is simply reported as not-preferred
(``enabled=False`` with ``extra["note"] = "mirrors take precedence"``), and only an
empty ``registry-mirrors`` array makes it the default source.

Editing rules
-------------
* ``json.loads`` -> mutate ``registry-mirrors`` -> ``json.dumps(..., indent=2)``: every
  other key (``insecure-registries``, ``log-opts``, ``storage-driver``, ...) and the key
  order survive untouched.  The file is written with
  :func:`~srcmgr.core.paths.atomic_write_text`.
* A missing ``daemon.json`` is created as ``{}`` - but only by a *mutating* call, never
  by a read.
* ``insecure-registries`` entries are exposed as disabled informational sources; they
  are not mirrors and this plugin will not rewrite them.
* Docker has no "disabled mirror" state, so ``disable()`` normally just removes the
  entry.  Opt in (``managers.docker.park_disabled_mirrors: true``,
  ``$SRCMGR_DOCKER_ROOT`` aware) to have it parked in a Source Manager owned key
  ``x-source-manager-disabled-mirrors`` inside ``daemon.json`` instead, which keeps it
  restorable by ``enable()``.  The key is prefixed because Docker warns about unknown
  top-level keys in some versions, hence default OFF.

Writes need root (``/etc`` is system scope); reads never do.

Test hook
---------
``SRCMGR_DOCKER_ROOT`` (default ``/etc/docker``) is the directory that stands in for
``/etc/docker``: every read and write - including :meth:`DockerManager.config_files` -
resolves against it, so tests never touch the real file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from srcmgr.core import helpers
from srcmgr.core.api import (
    ManagerPlugin,
    OperationResult,
    Scope,
    Source,
    SourceKind,
    TestResult,
)
from srcmgr.core.backup import utc_now
from srcmgr.core.health import ProbeResult, probe_url
from srcmgr.core.paths import atomic_write_text

#: Directory that stands in for ``/etc/docker`` (``SRCMGR_DOCKER_ROOT``).
ROOT_ENV = "SRCMGR_DOCKER_ROOT"
DEFAULT_ROOT = Path("/etc/docker")

DAEMON_JSON = "daemon.json"
DROPIN_DIRNAME = "daemon.json.d"

MIRROR_KEY = "registry-mirrors"
INSECURE_KEY = "insecure-registries"
#: Source Manager owned key for mirrors parked by :meth:`DockerManager.disable`.
PARKED_KEY = "x-source-manager-disabled-mirrors"

DOCKER_HUB = "https://registry-1.docker.io"
HUB_NOTE = "mirrors take precedence"
RESTART_HINT = "systemctl restart docker"

#: The registry API needs auth, so 401/403 prove the endpoint is alive.
HEALTHY_HTTP = (200, 401, 403)

PARK_CONFIG_KEY = "managers.docker.park_disabled_mirrors"


def normalize(url: str) -> str:
    """Comparison key for mirror URLs (scheme/host case, trailing slash)."""
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        path = parts.path.rstrip("/")
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"
    return text.rstrip("/").lower()


def label_for(url: str) -> str:
    """Friendly short name for a mirror URL (``docker.1ms.run``)."""
    parts = urlsplit(url or "")
    if parts.netloc:
        path = parts.path.rstrip("/")
        return f"{parts.netloc}{path}"
    return (url or "").strip()


@dataclass
class Daemon:
    """Contents of ``daemon.json`` plus what we had to say about reading it."""

    data: dict[str, Any] = field(default_factory=dict)
    path: Path = DEFAULT_ROOT / DAEMON_JSON
    exists: bool = False
    error: str = ""


class DockerManager(ManagerPlugin):
    """``/etc/docker/daemon.json`` registry mirrors as Source Manager sources."""

    supports: tuple[str, ...] = (
        "detect",
        "list",
        "current",
        "test",
        "native-test",
        "add",
        "remove",
        "enable",
        "disable",
        "update",
        "backup",
        "restore",
        "refresh",
    )

    # -- paths ---------------------------------------------------------------
    def root_dir(self) -> Path:
        """``$SRCMGR_DOCKER_ROOT`` or ``/etc/docker``."""
        raw = os.environ.get(ROOT_ENV, "").strip()
        if raw:
            return Path(raw).expanduser()
        return DEFAULT_ROOT

    def daemon_path(self) -> Path:
        return self.root_dir() / DAEMON_JSON

    def dropin_dir(self) -> Path:
        return self.root_dir() / DROPIN_DIRNAME

    def dropin_files(self) -> list[Path]:
        directory = self.dropin_dir()
        if not directory.is_dir():
            return []
        return sorted(item for item in directory.glob("*.json") if item.is_file())

    def config_files(self) -> list[Path]:
        """``daemon.json`` plus any drop-ins, resolved against the current root."""
        return [self.daemon_path(), *self.dropin_files()]

    # -- metadata ------------------------------------------------------------
    def detect(self) -> bool:
        cli = helpers.which("docker")
        if cli:
            return True
        return self.daemon_path().exists() or bool(self.dropin_files())

    def detect_detail(self) -> dict[str, Any]:
        daemon = self._load()
        mirrors, mirror_error = self._string_list(daemon.data, MIRROR_KEY)
        insecure, _insecure_error = self._string_list(daemon.data, INSECURE_KEY)
        parked, _parked_error = self._string_list(daemon.data, PARKED_KEY)
        dropins = self.dropin_files()
        return {
            "available": self.detect(),
            "cli": bool(helpers.which("docker")),
            "cli_path": helpers.which("docker") or "",
            "root": str(self.root_dir()),
            "root_overridden": bool(os.environ.get(ROOT_ENV, "").strip()),
            "daemon_json": str(self.daemon_path()),
            "daemon_json_exists": daemon.exists,
            "daemon_json_error": daemon.error,
            "dropins": [str(path) for path in dropins],
            "dropins_merged": False,
            "dropin_note": (
                "drop-in files are listed but never merged or modified: Docker reads "
                "them itself and this plugin only owns daemon.json"
            ),
            "mirrors": len(mirrors),
            "mirror_error": mirror_error,
            "insecure": len(insecure),
            "parked": len(parked),
            "park_disabled_mirrors": self._park_enabled(),
            "files": [str(path) for path in self.config_files() if path.exists()],
        }

    # -- daemon.json IO ------------------------------------------------------
    def _load(self) -> Daemon:
        """Read ``daemon.json``; a missing file is an empty document, never created."""
        path = self.daemon_path()
        text = helpers.read_text(path)
        if not text.strip():
            return Daemon({}, path, path.exists())
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            return Daemon({}, path, True, error=f"{path}: invalid JSON: {exc}")
        if not isinstance(data, dict):
            return Daemon({}, path, True, error=f"{path}: top level must be a JSON object")
        return Daemon(data, path, True)

    def _dump(self, daemon: Daemon, data: Mapping[str, Any]) -> OperationResult | None:
        """Write ``data`` atomically; returns a failure result on IO errors."""
        text = json.dumps(dict(data), indent=2) + "\n"
        try:
            atomic_write_text(daemon.path, text)
        except OSError as exc:
            return helpers.fail(
                f"docker: cannot write {daemon.path}: {exc}",
                hint="re-run with sudo",
            )
        return None

    def _string_list(
        self, data: Mapping[str, Any], key: str
    ) -> tuple[list[str], str]:
        """Read a JSON string array without ever coercing foreign values."""
        value = data.get(key)
        if value is None:
            return [], ""
        if not isinstance(value, list):
            return [], f"'{key}' must be a JSON array"
        if any(not isinstance(item, str) for item in value):
            return [], f"'{key}' entries must be strings"
        return [str(item) for item in value], ""

    def _park_enabled(self) -> bool:
        """True when the user opted into parking disabled mirrors (default OFF)."""
        config = getattr(self, "config", None)
        if config is None:
            return False
        try:
            return bool(config.get(PARK_CONFIG_KEY, False))
        except Exception:  # noqa: BLE001 - configuration is always optional
            return False

    def _parked(self, data: Mapping[str, Any]) -> list[str]:
        parked, _error = self._string_list(data, PARKED_KEY)
        return parked

    # -- reads ---------------------------------------------------------------
    def _hub_source(self, *, default: bool, path: Path, note: str | None = None) -> Source:
        if note is None:
            note = "" if default else HUB_NOTE
        extra: dict[str, Any] = {
            "key": MIRROR_KEY,
            "url": DOCKER_HUB,
            "synthetic": True,
            "note": note,
        }
        return helpers.make_source(
            self.name,
            name="Docker Hub",
            url=DOCKER_HUB,
            kind=SourceKind.OFFICIAL,
            scope=Scope.SYSTEM,
            enabled=default,
            is_default=default,
            file=str(path),
            provider_key="docker-hub",
            description="The implicit upstream registry (registry-1.docker.io).",
            extra=extra,
        )

    def list_enabled(self) -> list[Source]:
        """Mirrors, Docker Hub and the informational ``insecure-registries`` entries."""
        daemon = self._load()
        data = daemon.data
        mirrors, _mirror_error = self._string_list(data, MIRROR_KEY)
        insecure, _insecure_error = self._string_list(data, INSECURE_KEY)
        parked = self._parked(data)

        out: list[Source] = []
        for index, url in enumerate(mirrors):
            out.append(
                helpers.make_source(
                    self.name,
                    name=label_for(url),
                    url=url,
                    kind=SourceKind.MIRROR,
                    scope=Scope.SYSTEM,
                    enabled=True,
                    file=str(daemon.path),
                    provider_key=label_for(url),
                    description=f"registry-mirrors[{index}]",
                    extra={"key": MIRROR_KEY, "url": url, "index": index},
                )
            )
        # Docker Hub is never "removed" - it is only outranked by the mirror list.
        for url in parked:
            if any(normalize(item) == normalize(url) for item in mirrors):
                continue
            out.append(
                helpers.make_source(
                    self.name,
                    name=label_for(url),
                    url=url,
                    kind=SourceKind.MIRROR,
                    scope=Scope.SYSTEM,
                    enabled=False,
                    file=str(daemon.path),
                    provider_key=label_for(url),
                    description="mirror parked by 'srcmgr disable docker'",
                    extra={"key": PARKED_KEY, "url": url, "parked": True},
                )
            )
        out.append(self._hub_source(default=not mirrors, path=daemon.path))
        for index, url in enumerate(insecure):
            out.append(
                helpers.make_source(
                    self.name,
                    name=f"insecure: {label_for(url)}",
                    url=url,
                    kind=SourceKind.CUSTOM,
                    scope=Scope.SYSTEM,
                    enabled=False,
                    file=str(daemon.path),
                    provider_key=f"insecure:{label_for(url)}",
                    description=(
                        "listed under insecure-registries (plain HTTP allowed); "
                        "informational only, this plugin does not modify the list"
                    ),
                    extra={
                        "key": INSECURE_KEY,
                        "url": url,
                        "index": index,
                        "informational": True,
                    },
                )
            )
        return out

    def current(self) -> Source | None:
        """The first enabled mirror, else Docker Hub."""
        items = self.list_enabled()
        for source in items:
            if source.enabled and source.extra.get("key") == MIRROR_KEY and not source.is_default:
                return source
        for source in items:
            if source.enabled:
                return source
        return None

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Docker Hub is the only built-in registry; mirrors come from providers."""
        return [self._hub_source(default=False, path=self.daemon_path(), note="")]

    # -- mutations -----------------------------------------------------------
    def _root_guard(self, daemon: Daemon) -> OperationResult | None:
        """Writes to the system scope need root; reads never call this."""
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            return None
        return helpers.fail(
            f"docker: writing {daemon.path} requires root",
            hint="re-run with sudo",
        )

    def _target_url(self, source: Source) -> tuple[str, OperationResult | None]:
        url = str(source.url or source.extra.get("url") or "").strip()
        if not url:
            return "", helpers.fail(f"docker: source {source.name!r} has no mirror URL")
        return url, None

    def _find_index(self, entries: Sequence[str], url: str) -> int:
        wanted = normalize(url)
        for index, item in enumerate(entries):
            if normalize(item) == wanted:
                return index
        return -1

    def add(self, source: Source) -> OperationResult:
        """Append the URL to ``registry-mirrors`` (deduplicated, order preserved)."""
        daemon = self._load()
        denied = self._root_guard(daemon)
        if denied is not None:
            return denied
        url, problem = self._target_url(source)
        if problem is not None:
            return problem
        if daemon.error:
            return helpers.fail(f"docker: {daemon.error}", hint="fix daemon.json first")

        data = dict(daemon.data)
        mirrors, error = self._string_list(data, MIRROR_KEY)
        if error:
            return helpers.fail(f"docker: {error}", path=str(daemon.path))
        if self._find_index(mirrors, url) >= 0:
            return helpers.ok(
                f"docker: mirror {url} is already configured",
                changed=[],
                path=str(daemon.path),
                restart_required=RESTART_HINT,
            )

        restored = False
        parked = self._parked(data)
        index = self._find_index(parked, url)
        if index >= 0:
            # The URL is being activated again: drop the parked copy so the file does
            # not claim it is both active and parked.
            parked.pop(index)
            restored = True
            if parked:
                data[PARKED_KEY] = parked
            else:
                data.pop(PARKED_KEY, None)

        mirrors.append(url)
        data[MIRROR_KEY] = mirrors
        failure = self._dump(daemon, data)
        if failure is not None:
            return failure
        return helpers.ok(
            f"docker: mirror {url} added" + (" (restored from the parked list)" if restored else ""),
            changed=[str(daemon.path)],
            path=str(daemon.path),
            mirrors=mirrors,
            created=not daemon.exists,
            restart_required=RESTART_HINT,
        )

    def remove(self, source: Source) -> OperationResult:
        """Remove exactly one entry; an empty list drops the key entirely."""
        daemon = self._load()
        denied = self._root_guard(daemon)
        if denied is not None:
            return denied
        url, problem = self._target_url(source)
        if problem is not None:
            return problem
        if daemon.error:
            return helpers.fail(f"docker: {daemon.error}", hint="fix daemon.json first")

        key = str(source.extra.get("key") or MIRROR_KEY)
        if key == INSECURE_KEY:
            return helpers.fail(
                "docker: insecure-registries are not managed by Source Manager",
                hint=f"edit {daemon.path} by hand if you really want to drop {url}",
            )

        data = dict(daemon.data)
        entries, error = self._string_list(data, key)
        if error:
            return helpers.fail(f"docker: {error}", path=str(daemon.path))
        index = self._find_index(entries, url)
        if index < 0:
            return helpers.fail(
                f"docker: {url} is not in {key}; nothing was removed",
                path=str(daemon.path),
            )
        entries.pop(index)
        if entries:
            data[key] = entries
        else:
            data.pop(key, None)
        failure = self._dump(daemon, data)
        if failure is not None:
            return failure
        return helpers.ok(
            f"docker: mirror {url} removed from {key}",
            changed=[str(daemon.path)],
            path=str(daemon.path),
            mirrors=entries,
            restart_required=RESTART_HINT,
        )

    def enable(self, source: Source) -> OperationResult:
        """Ensure the mirror is present again (also un-parks a parked mirror)."""
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Drop the mirror - Docker has no disabled-mirror state.

        With ``managers.docker.park_disabled_mirrors`` enabled the entry is moved to
        ``x-source-manager-disabled-mirrors`` instead, which :meth:`enable` restores.
        """
        daemon = self._load()
        denied = self._root_guard(daemon)
        if denied is not None:
            return denied
        url, problem = self._target_url(source)
        if problem is not None:
            return problem
        if daemon.error:
            return helpers.fail(f"docker: {daemon.error}", hint="fix daemon.json first")

        data = dict(daemon.data)
        mirrors, error = self._string_list(data, MIRROR_KEY)
        if error:
            return helpers.fail(f"docker: {error}", path=str(daemon.path))
        index = self._find_index(mirrors, url)
        if index < 0:
            parked = self._parked(data)
            if self._find_index(parked, url) >= 0:
                return helpers.ok(
                    f"docker: mirror {url} is already disabled (parked)",
                    changed=[],
                    path=str(daemon.path),
                )
            return helpers.fail(
                f"docker: {url} is not a configured mirror; nothing was disabled",
                path=str(daemon.path),
                note="Docker Hub cannot be disabled - configure a mirror instead",
            )

        mirrors.pop(index)
        if mirrors:
            data[MIRROR_KEY] = mirrors
        else:
            data.pop(MIRROR_KEY, None)

        parked_note = "the entry was removed (docker has no disabled-mirror state)"
        if self._park_enabled():
            parked = self._parked(data)
            if self._find_index(parked, url) < 0:
                parked.append(url)
            data[PARKED_KEY] = parked
            parked_note = f"parked in {PARKED_KEY} so it can be re-enabled"
        failure = self._dump(daemon, data)
        if failure is not None:
            return failure
        return helpers.ok(
            f"docker: mirror {url} disabled - {parked_note}",
            changed=[str(daemon.path)],
            path=str(daemon.path),
            mirrors=mirrors,
            parked=self._park_enabled(),
            restart_required=RESTART_HINT,
        )

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """Replace one mirror URL in place, keeping the list order."""
        daemon = self._load()
        denied = self._root_guard(daemon)
        if denied is not None:
            return denied
        new_url = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_url:
            return helpers.fail(
                "docker: only 'url' can be edited for a registry mirror",
                hint="srcmgr edit docker <source> --set url=https://mirror.example.org",
            )
        old_url, problem = self._target_url(source)
        if problem is not None:
            return problem
        if daemon.error:
            return helpers.fail(f"docker: {daemon.error}", hint="fix daemon.json first")

        data = dict(daemon.data)
        mirrors, error = self._string_list(data, MIRROR_KEY)
        if error:
            return helpers.fail(f"docker: {error}", path=str(daemon.path))
        index = self._find_index(mirrors, old_url)
        if index < 0:
            return helpers.fail(
                f"docker: {old_url} is not a configured mirror; nothing was changed",
                path=str(daemon.path),
            )
        other = self._find_index(mirrors, new_url)
        if other >= 0 and other != index:
            return helpers.fail(
                f"docker: {new_url} is already configured as a mirror",
                path=str(daemon.path),
            )
        mirrors[index] = new_url
        data[MIRROR_KEY] = mirrors
        failure = self._dump(daemon, data)
        if failure is not None:
            return failure
        return helpers.ok(
            f"docker: mirror {old_url} -> {new_url}",
            changed=[str(daemon.path)],
            path=str(daemon.path),
            mirrors=mirrors,
            restart_required=RESTART_HINT,
        )

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        """Docker's own view of the effective mirror list."""
        return ["docker", "info", "--format", "{{json .RegistryConfig.Mirrors}}"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe ``<url>/v2/``; 200, 401 and 403 all mean "registry is alive"."""
        base = str(source.url or source.extra.get("url") or "").strip().rstrip("/")
        if not base:
            return TestResult(
                source_id=source.id,
                manager=self.name,
                name=source.name,
                ok=False,
                status="no-url",
                checked_at=utc_now(),
                error="source has no URL to probe",
            )
        target = f"{base}/" if base.endswith("/v2") else f"{base}/v2/"
        try:
            result = probe_url(target, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            result = ProbeResult(ok=False, status="error", url=target, error=str(exc))
        healthy = bool(result.ok or result.http_status in HEALTHY_HTTP)
        return TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=target,
            ok=healthy,
            status=result.status or ("ok" if healthy else "error"),
            http_status=result.http_status,
            latency_ms=result.latency_ms,
            tls_ok=result.tls_ok,
            redirected_to=result.redirected_to,
            method=result.method,
            checked_at=utc_now(),
            detail="the registry API requires auth; 401/403 count as healthy",
            error="" if healthy else result.error,
            probes=[result.to_dict()],
        )

    def refresh(self) -> OperationResult:
        """Docker must be restarted by the user; this never restarts the daemon."""
        return helpers.ok(
            f"docker restart required: {RESTART_HINT}",
            restart_required=True,
            command=RESTART_HINT,
        )


__all__ = [
    "DAEMON_JSON",
    "DEFAULT_ROOT",
    "DOCKER_HUB",
    "Daemon",
    "DockerManager",
    "HUB_NOTE",
    "INSECURE_KEY",
    "MIRROR_KEY",
    "PARKED_KEY",
    "RESTART_HINT",
    "ROOT_ENV",
    "label_for",
    "normalize",
]
