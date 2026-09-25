"""npm registry manager plugin.

What this plugin owns
---------------------
``npm`` resolves packages from the registry named by the ``registry`` key of
``$HOME/.npmrc``.  Two further npmrc files (``/usr/etc/npmrc`` and ``/etc/npmrc``) are
part of npm's configured search path and are therefore *listed* by
:meth:`NpmManager.config_files` so the backup layer can record them, but this plugin
declares ``scope: user`` and never writes them.

A project-local ``./.npmrc`` in the current working directory outranks ``$HOME/.npmrc``
for npm itself.  It is deliberately never modified here - silently rewriting a
repository's committed configuration because a *user* switched registries would be
surprising behaviour - and :meth:`NpmManager.detect_detail` reports its presence so the
CLI can mention it.

Source of truth
---------------
:meth:`NpmManager.read_value` prefers the ``registry`` key of the user npmrc.  When that
key is absent the value npm itself would resolve is asked for with
``npm config get registry`` and the returned :class:`~srcmgr.core.api.Source` carries
``extra["source_of_truth"] == "npm config"``.  When neither source has a value the
ecosystem default (``https://registry.npmjs.org/``) is reported, so ``read_value()``
never returns an empty string and ``list_enabled()`` always describes the effective
registry.

Auth companions
---------------
Private registries usually need more than ``registry=``.  A caller may attach
``source.extra["accompanying"] = {"always-auth": "true", "_authToken": "..."}``; every
pair is written verbatim into the same npmrc by :meth:`NpmManager.add`, so no key name
has to be hardcoded here.  When one of those keys is a real credential (``_authToken``,
``_auth``, ``_password``, ...) the source is flagged with ``secret_fields=["extra"]`` so
the CLI and ``Source.to_dict(redact=True)`` mask it.  Sources describing a file that
already holds a token are flagged the same way.

Environment overrides (used by the test-suite to isolate a temporary HOME)
--------------------------------------------------------------------------
``SRCMGR_NPMRC`` - absolute path used instead of ``$HOME/.npmrc``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from srcmgr.core import health, helpers
from srcmgr.core.api import OperationResult, Source, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.helpers import KeyValueFile, SingleValueManager, fail, ok
from srcmgr.core.paths import atomic_write_text

#: npmrc keys that carry real credentials; only these make a source secret.
TOKEN_KEYS = ("_authToken", "_auth", "_password", "password", "token")

#: HTTP statuses an npm registry may answer with when it wants authentication; both
#: mean "the registry is alive", which is all a reachability check should decide.
AUTH_STATUSES = frozenset({401, 403})

#: ``key=value`` / ``key: value`` line, same subset npm accepts.
_KV_LINE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\-\[\]]+)\s*[:=]\s*(?P<value>.*?)\s*$")


def _set_key(path: Path, key: str, value: str, *, separator: str = "=") -> None:
    """Set ``key=value`` in an npmrc, replacing the existing line in place.

    The core :meth:`~srcmgr.core.helpers.KeyValueFile.set_many` helper cannot be used
    for a key that already exists (its in-place branch references an unbound local), so
    this plugin performs the update itself - which also lets the original key spelling,
    every comment and every unrelated line survive byte for byte.
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
    """Run ``command``, returning ``None`` when the binary is missing or hangs."""
    try:
        return helpers.run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _clean(value: str) -> str:
    """Strip whitespace and one layer of surrounding quotes from an npmrc value."""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _looks_secret(mapping: Mapping[str, Any]) -> bool:
    return any(str(key).lower() in {k.lower() for k in TOKEN_KEYS} for key in mapping)


class NpmManager(SingleValueManager):
    """Manage ``registry`` in the npm user configuration."""

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
        #: Where the last :meth:`read_value` found its answer; consumed by
        #: :meth:`describe_current` to annotate the produced source.
        self._origin: dict[str, Any] = {}

    # -- paths ---------------------------------------------------------------
    def _rc_path(self) -> Path:
        override = os.environ.get("SRCMGR_NPMRC", "").strip()
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".npmrc"

    def _project_rc(self) -> Path:
        return Path.cwd() / ".npmrc"

    def _pnpm_rc_path(self) -> Path:
        """Reproduce *pnpm*'s resolution to decide whether the file is shared."""
        override = os.environ.get("SRCMGR_PNPM_RC", "").strip()
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".npmrc"

    def _shared_with(self) -> list[str]:
        shared = []
        if self._pnpm_rc_path() == self._rc_path():
            shared.append("pnpm")
        return shared

    def config_files(self) -> list[Path]:
        paths: list[Path] = [self._rc_path()]
        for raw in self.manifest.config_paths or ["~/.npmrc", "/usr/etc/npmrc", "/etc/npmrc"]:
            candidate = Path(raw).expanduser()
            if candidate not in paths:
                paths.append(candidate)
        return paths

    # -- detection -----------------------------------------------------------
    def detect(self) -> bool:
        try:
            if helpers.which("npm"):
                return True
            return any(path.exists() for path in self.config_files())
        except Exception:  # noqa: BLE001 - detection must never raise
            return False

    def detect_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files() if p.exists()],
            "shared_with": self._shared_with(),
        }
        project = self._project_rc()
        if project.exists():
            detail["project_npmrc"] = str(project)
            detail["notes"] = [
                f"{project} is project-local and is never modified by srcmgr"
            ]
        return detail

    # -- reading -------------------------------------------------------------
    def read_value(self) -> str:
        self._origin = {}
        path = self._rc_path()
        value = _clean(KeyValueFile(path).get("registry"))
        if value:
            self._origin = {"file": str(path), "source_of_truth": "file", "key": self.value_key}
            return value

        if helpers.which("npm"):
            completed = _run(["npm", "config", "get", "registry"])
            if completed is not None and completed.returncode == 0:
                from_cli = _clean((completed.stdout or "").splitlines()[0] if completed.stdout else "")
                if from_cli and from_cli.lower() not in {"undefined", "null", "none"}:
                    self._origin = {
                        "file": "",
                        "source_of_truth": "npm config",
                        "key": self.value_key,
                    }
                    return from_cli

        self._origin = {"file": "", "source_of_truth": "default", "key": self.value_key}
        return self.default_url

    def _companions(self) -> dict[str, str]:
        """Credential-ish keys already present in the user npmrc (values included)."""
        found: dict[str, str] = {}
        for key, value in KeyValueFile(self._rc_path()).entries():
            if key.lower() in {k.lower() for k in TOKEN_KEYS}:
                found.setdefault(key, value)
        return found

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
            }
        )
        companions = self._companions()
        if companions:
            # Never copy the secret values themselves, only say that they exist - but
            # flag the source as secret so anything extra is masked by the CLI.
            source.extra["credentials_configured"] = sorted(companions)
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

    def _write_companions(self, companions: Mapping[str, Any]) -> None:
        path = self._rc_path()
        for key, value in companions.items():
            _set_key(path, str(key), str(value))

    def add(self, source: Source) -> OperationResult:
        accompanying = source.extra.get("accompanying")
        accompanying = dict(accompanying) if isinstance(accompanying, Mapping) else {}
        if accompanying and _looks_secret(accompanying):
            source.secret_fields = ["extra"]
        try:
            if accompanying:
                # Written before the main key so an unchanged registry still gets them.
                self._write_companions(accompanying)
            result = super().add(source)
        except OSError as exc:
            return fail(f"npm: cannot write {self._rc_path()}: {exc}")
        if result.ok and accompanying:
            result.message += f" (+{len(accompanying)} accompanying key(s))"
            result.details.setdefault("accompanying", sorted(accompanying))
        return result

    def remove(self, source: Source) -> OperationResult:
        try:
            return super().remove(source)
        except OSError as exc:
            return fail(f"npm: cannot write {self._rc_path()}: {exc}")

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        return ["npm", "ping"]

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


__all__ = ["NpmManager", "TOKEN_KEYS"]
