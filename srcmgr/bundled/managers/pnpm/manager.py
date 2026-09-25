"""pnpm registry manager plugin.

Shared configuration - read this before changing anything
---------------------------------------------------------
**npm, pnpm and Yarn classic all read the same ``registry`` key from the same
``$HOME/.npmrc``.**  pnpm has no private registry file of its own by default: it reads
npm's user config, so this plugin writes exactly the same key as the npm plugin and
changing the registry here changes it for npm (and for yarn 1) too, unless a per-tool
configuration is used instead:

* ``SRCMGR_PNPM_RC`` (or ``pnpm --config-dir``/``PNPM_HOME`` style setups, and newer
  pnpm versions that keep an XDG rc at ``~/.config/pnpm/rc``) - point the environment
  override at a separate file to manage pnpm alone;
* a project-local ``./.npmrc`` (never touched by this plugin) - it outranks the user
  file for the project it belongs to.

Because of that, :meth:`PnpmManager.detect_detail` reports
``{"shared_with": ["npm"]}`` when this plugin's file resolves to the same path as the
npm plugin's file, and the ``add()`` message is suffixed with ``(shared with npm)`` so
the CLI can warn the user that one change affects several tools.

The npm path is *re-derived* here (``SRCMGR_NPMRC`` else ``$HOME/.npmrc``) rather than
imported from the npm plugin: plugins are independent, may be loaded separately, and a
load-order dependency between two bundled plugins would be a bug waiting to happen.

Reading, writing and health checking mirror the npm plugin: the ``registry`` key is the
source of truth, ``pnpm config get registry`` is the fallback, and ``pnpm ping`` is the
native connectivity check when the binary is installed.

Environment overrides (used by the test-suite to isolate a temporary HOME)
--------------------------------------------------------------------------
``SRCMGR_PNPM_RC`` - absolute path used instead of ``$HOME/.npmrc``.
``SRCMGR_NPMRC``   - only read, to detect the shared-file situation.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from srcmgr.core import health, helpers
from srcmgr.core.api import OperationResult, Source, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.helpers import KeyValueFile, SingleValueManager, fail, ok
from srcmgr.core.paths import atomic_write_text

AUTH_STATUSES = frozenset({401, 403})

#: ``key=value`` / ``key: value`` line, same subset npm/pnpm accept.
_KV_LINE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\-\[\]]+)\s*[:=]\s*(?P<value>.*?)\s*$")


def _set_key(path: Path, key: str, value: str, *, separator: str = "=") -> None:
    """Set ``key=value`` in the shared npmrc, replacing the existing line in place.

    The core :meth:`~srcmgr.core.helpers.KeyValueFile.set_many` helper cannot be used
    for a key that already exists (its in-place branch references an unbound local), so
    the update is done here, keeping every comment and unrelated line byte for byte.
    """
    lines = helpers.read_text(path).splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = _KV_LINE.match(line)
        if match and match.group("key").lower() == key.lower():
            lines[index] = f"{match.group('key')}{separator}{value}"
            break
    else:
        lines.append(f"{key}{separator}{value}")
    text = "\n".join(lines).rstrip("\n")
    atomic_write_text(path, text + "\n" if text else "")


def _run(command: Sequence[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    try:
        return helpers.run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


class PnpmManager(SingleValueManager):
    """Manage ``registry`` in the npm-compatible user configuration."""

    value_key = "registry"
    default_name = "npmjs"
    default_url = "https://registry.npmjs.org/"
    supports = (
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

    def __init__(self) -> None:
        self._origin: dict[str, Any] = {}

    # -- paths ---------------------------------------------------------------
    def _rc_path(self) -> Path:
        override = os.environ.get("SRCMGR_PNPM_RC", "").strip()
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".npmrc"

    def _npm_rc_path(self) -> Path:
        override = os.environ.get("SRCMGR_NPMRC", "").strip()
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".npmrc"

    def shared_with(self) -> list[str]:
        """Other managers that write this exact file (only npm, by design)."""
        try:
            return ["npm"] if self._rc_path() == self._npm_rc_path() else []
        except Exception:  # noqa: BLE001 - cosmetic metadata
            return []

    def config_files(self) -> list[Path]:
        paths: list[Path] = [self._rc_path()]
        for raw in self.manifest.config_paths or ["~/.npmrc", "/etc/npmrc"]:
            candidate = Path(raw).expanduser()
            if candidate not in paths:
                paths.append(candidate)
        return paths

    # -- detection -----------------------------------------------------------
    def detect(self) -> bool:
        try:
            if helpers.which("pnpm"):
                return True
            return any(path.exists() for path in self.config_files())
        except Exception:  # noqa: BLE001 - detection must never raise
            return False

    def detect_detail(self) -> dict[str, Any]:
        return {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files() if p.exists()],
            "shared_with": self.shared_with(),
        }

    # -- reading -------------------------------------------------------------
    def read_value(self) -> str:
        self._origin = {}
        path = self._rc_path()
        value = _clean(KeyValueFile(path).get("registry"))
        if value:
            self._origin = {"file": str(path), "source_of_truth": "file", "key": self.value_key}
            return value

        if helpers.which("pnpm"):
            completed = _run(["pnpm", "config", "get", "registry"])
            if completed is not None and completed.returncode == 0:
                lines = (completed.stdout or "").strip().splitlines()
                from_cli = _clean(lines[0]) if lines else ""
                if from_cli and from_cli.lower() not in {"undefined", "null", "none"}:
                    self._origin = {
                        "file": "",
                        "source_of_truth": "pnpm config",
                        "key": self.value_key,
                    }
                    return from_cli

        self._origin = {"file": "", "source_of_truth": "default", "key": self.value_key}
        return self.default_url

    def describe_current(self, value: str) -> Source:
        source = super().describe_current(value)
        origin = dict(self._origin or {})
        file = origin.get("file") or str(self._rc_path())
        source.file = file
        source.extra.update(
            {
                "key": self.value_key,
                "config_file": file,
                "source_of_truth": origin.get("source_of_truth", "file"),
                "shared_with": self.shared_with(),
            }
        )
        if KeyValueFile(self._rc_path()).get("_authToken") or KeyValueFile(self._rc_path()).get(
            "_auth"
        ):
            source.secret_fields = ["extra"]
        return source

    # -- writing -------------------------------------------------------------
    def write_value(self, value: str) -> None:
        _set_key(self._rc_path(), self.value_key, value.strip())

    def clear_value(self) -> bool:
        path = self._rc_path()
        if not path.exists():
            return False
        removed = KeyValueFile(path).unset(self.value_key)
        if removed and not helpers.read_text(path).strip():
            path.unlink(missing_ok=True)
        return removed

    def add(self, source: Source) -> OperationResult:
        try:
            result = super().add(source)
        except OSError as exc:
            return fail(f"pnpm: cannot write {self._rc_path()}: {exc}")
        shared = self.shared_with()
        if result.ok and shared:
            result.message += f" (shared with {', '.join(shared)})"
            result.details.setdefault("shared_with", shared)
        return result

    def remove(self, source: Source) -> OperationResult:
        try:
            return super().remove(source)
        except OSError as exc:
            return fail(f"pnpm: cannot write {self._rc_path()}: {exc}")

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        return ["pnpm", "ping"] if helpers.which("pnpm") else None

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """HTTP-probe the registry; 401/403 count as reachable (private registries)."""
        url = (source.url or self.read_value() or self.default_url).strip()
        probe = health.probe_url(url, timeout=timeout)
        private = probe.http_status in AUTH_STATUSES
        healthy = bool(probe.ok) or private
        detail = ""
        if private:
            detail = f"HTTP {probe.http_status}: authentication required, endpoint reachable"
        return TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=url,
            ok=healthy,
            status="ok" if healthy else probe.status,
            http_status=probe.http_status,
            latency_ms=probe.latency_ms,
            tls_ok=probe.tls_ok,
            redirected_to=probe.redirected_to,
            checked_at=utc_now(),
            method=probe.method,
            detail=detail,
            error="" if healthy else probe.error,
            probes=[probe.to_dict()],
        )

    def refresh(self) -> OperationResult:
        return ok("nothing to refresh")


__all__ = ["PnpmManager"]
