"""Yarn registry manager plugin - supports both incompatible generations.

Yarn 1 ("classic") and Yarn 2+ ("berry") do not share a configuration format, so this
plugin detects which one is in use and edits the matching file:

======================  =========================  ==================================
flavour                 file                       key
======================  =========================  ==================================
classic (yarn 1)        ``$HOME/.yarnrc``          ``registry "https://..."``
berry (yarn >= 2)       ``$HOME/.yarnrc.yml``      ``npmRegistryServer: "https://..."``
======================  =========================  ==================================

Detection order: an existing ``.yarnrc.yml`` means berry, an existing ``.yarnrc`` means
classic; when neither file exists ``yarn --version`` decides (``>= 2`` -> berry), and
when yarn is not installed at all the flavour stays ``"unknown"`` and new configuration
is written to ``.yarnrc`` (the format every yarn understands when it cannot tell us
which generation it is).  ``detect_detail()`` reports ``{"flavour": ..., "files": [...]}``.

Format preservation
-------------------
Berry files are edited *line by line* with :mod:`re`: comments, ``yarnPath``,
``npmScopes:`` blocks, ``unsafeHttpWhitelist:`` and every unrelated YAML key survive
byte for byte.  The file is never parsed and re-serialised with PyYAML, which would
reorder keys, drop comments and rewrite quoting.  Classic files are equally line-based,
because ``.yarnrc`` is a Yarn-specific ``key value`` format (not INI, not YAML).

Scoped registries (``npmScopes.<scope>.npmRegistryServer``) are reported by
``list_enabled()`` as additional, read-only sources so users can see them, but they are
never rewritten: synthesising nested YAML blocks reliably is not worth the risk of
corrupting a hand-maintained file.  Mutating one returns a clear failure with a hint.

Why ``native_test_command()`` returns ``None``
----------------------------------------------
There is no cheap, side-effect-free yarn command that proves *connectivity* to the
configured registry: ``yarn config get registry`` only echoes local configuration (and
is spelled ``npmRegistryServer`` on berry), while ``yarn info``/``yarn npm info``
resolve a real package - a metadata request that hits the registry and the network
hard, takes seconds, and can fail for reasons unrelated to the registry (offline cache,
package rename, private package scope).  A health check must answer "is this registry
reachable", so this plugin relies on the core HTTP probe (which accepts 200/401/403)
instead of shelling out to yarn.

Environment overrides (used by the test-suite to isolate a temporary HOME)
--------------------------------------------------------------------------
``SRCMGR_YARNRC``     - path used instead of ``$HOME/.yarnrc``; a path ending in
                        ``.yml`` is treated as the berry file and its classic sibling
                        (same name without ``.yml``) as ``.yarnrc``.
``SRCMGR_YARNRC_YML`` - path used instead of ``$HOME/.yarnrc.yml`` (wins over the
                        derivation from ``SRCMGR_YARNRC``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from srcmgr.core import helpers
from srcmgr.core.api import OperationResult, Source, SourceKind
from srcmgr.core.helpers import SingleValueManager, fail, make_source, ok
from srcmgr.core.paths import atomic_write_text

#: ``key: value`` / ``key:`` at any indentation (enough for yarn's own YAML subset).
_YAML_KV = re.compile(r"^(?P<key>[A-Za-z0-9_.@/-]+)\s*:\s*(?P<value>.*)$")

#: classic ``.yarnrc`` registry lines: ``registry "url"``, ``registry=url``,
#: ``--registry "url"`` and ``registry url`` all exist in the wild.
_CLASSIC_REGISTRY = re.compile(r"^\s*(?P<dash>--)?registry\s*(?:=|\s)\s*(?P<value>.+?)\s*$")


def _run(command: Sequence[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    try:
        return helpers.run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _strip_inline_comment(text: str) -> str:
    """Drop a trailing ``# comment`` unless it sits inside quotes."""
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text.rstrip()


def _scalar(text: str) -> str:
    """Turn a YAML/rc scalar into a bare value (quotes and comments removed)."""
    value = _strip_inline_comment(text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _parse_berry(text: str) -> tuple[str, list[tuple[str, str, int]]]:
    """Return ``(top_level_registry, [(scope, url, line_index), ...])``."""
    top = ""
    scoped: list[tuple[str, str, int]] = []
    in_scopes = False
    current_scope = ""
    current_scope_indent = 0
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _YAML_KV.match(stripped)
        if not match:
            continue
        key, raw_value = match.group("key"), match.group("value")
        indent = _indent_of(line)
        if indent == 0:
            in_scopes = False
            current_scope = ""
            if key == "npmRegistryServer" and raw_value.strip():
                top = _scalar(raw_value)
            elif key == "npmScopes" and not raw_value.strip():
                in_scopes = True
            continue
        if not in_scopes:
            continue
        if not raw_value.strip():
            current_scope = key
            current_scope_indent = indent
        elif key == "npmRegistryServer" and current_scope and indent > current_scope_indent:
            scoped.append((current_scope, _scalar(raw_value), index))
    return top, scoped


def _classic_registry(path: Path) -> str:
    for line in helpers.read_text(path).splitlines():
        match = _CLASSIC_REGISTRY.match(line)
        if match:
            return _scalar(match.group("value"))
    return ""


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    text = "\n".join(lines).strip("\n")
    atomic_write_text(path, text + "\n" if text else "")


class YarnManager(SingleValueManager):
    """Manage the registry of Yarn classic and Yarn berry."""

    value_key = "npmRegistryServer"
    default_name = "yarnpkg"
    default_url = "https://registry.yarnpkg.com"
    supports = (
        "detect",
        "list",
        "current",
        "test",
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
        self._cached_version: int | None = None
        self._version_checked = False

    # -- paths ---------------------------------------------------------------
    def _rc_paths(self) -> tuple[Path, Path]:
        """Return ``(classic, berry)`` after applying the environment overrides."""
        classic_env = os.environ.get("SRCMGR_YARNRC", "").strip()
        berry_env = os.environ.get("SRCMGR_YARNRC_YML", "").strip()
        if classic_env:
            candidate = Path(classic_env).expanduser()
            if candidate.name.endswith(".yml"):
                berry = candidate
                classic = candidate.with_name(candidate.name[: -len(".yml")])
            else:
                classic = candidate
                berry = candidate.with_name(candidate.name + ".yml")
        else:
            classic = helpers.home() / ".yarnrc"
            berry = helpers.home() / ".yarnrc.yml"
        if berry_env:
            berry = Path(berry_env).expanduser()
        return classic, berry

    def config_files(self) -> list[Path]:
        classic, berry = self._rc_paths()
        paths: list[Path] = [classic, berry]
        for raw in self.manifest.config_paths or ["~/.yarnrc", "~/.yarnrc.yml"]:
            candidate = Path(raw).expanduser()
            if candidate not in paths:
                paths.append(candidate)
        return paths

    # -- detection -----------------------------------------------------------
    def _yarn_version(self) -> int | None:
        if self._version_checked:
            return self._cached_version
        self._version_checked = True
        self._cached_version = None
        if not helpers.which("yarn"):
            return None
        completed = _run(["yarn", "--version"])
        if completed is None or completed.returncode != 0:
            return None
        match = re.match(r"\s*(\d+)", completed.stdout or "")
        if match:
            self._cached_version = int(match.group(1))
        return self._cached_version

    def flavour(self) -> str:
        classic, berry = self._rc_paths()
        try:
            if berry.exists():
                return "berry"
            if classic.exists():
                return "classic"
            version = self._yarn_version()
        except Exception:  # noqa: BLE001 - detection must never raise
            return "unknown"
        if version is None:
            return "unknown"
        return "berry" if version >= 2 else "classic"

    def detect(self) -> bool:
        try:
            classic, berry = self._rc_paths()
            return bool(helpers.which("yarn")) or classic.exists() or berry.exists()
        except Exception:  # noqa: BLE001 - detection must never raise
            return False

    def detect_detail(self) -> dict[str, Any]:
        return {
            "available": self.detect(),
            "flavour": self.flavour(),
            "files": [str(p) for p in self.config_files() if p.exists()],
            "yarn_major": self._yarn_version(),
        }

    def _target_flavour(self) -> str:
        """Flavour used for writes; unknown falls back to the detected generation."""
        flavour = self.flavour()
        if flavour != "unknown":
            return flavour
        version = self._yarn_version()
        return "berry" if (version is not None and version >= 2) else "classic"

    def _target_path(self, flavour: str | None = None) -> Path:
        classic, berry = self._rc_paths()
        return berry if (flavour or self._target_flavour()) == "berry" else classic

    # -- reading -------------------------------------------------------------
    def read_value(self) -> str:
        self._origin = {}
        classic, berry = self._rc_paths()
        if berry.exists():
            top, _ = _parse_berry(helpers.read_text(berry))
            if top:
                self._origin = {
                    "file": str(berry),
                    "source_of_truth": "file",
                    "key": "npmRegistryServer",
                    "flavour": "berry",
                }
                return top
        if classic.exists():
            value = _classic_registry(classic)
            if value:
                self._origin = {
                    "file": str(classic),
                    "source_of_truth": "file",
                    "key": "registry",
                    "flavour": "classic",
                }
                return value

        if helpers.which("yarn"):
            for key in ("npmRegistryServer", "registry"):
                completed = _run(["yarn", "config", "get", key])
                if completed is None or completed.returncode != 0:
                    continue
                output = (completed.stdout or "").strip().splitlines()
                value = _scalar(output[0]) if output else ""
                if value and value.lower() not in {"undefined", "null", "none"}:
                    self._origin = {
                        "file": "",
                        "source_of_truth": "yarn config",
                        "key": key,
                        "flavour": "berry" if key == "npmRegistryServer" else "classic",
                    }
                    return value

        self._origin = {
            "file": "",
            "source_of_truth": "default",
            "key": "npmRegistryServer" if self._target_flavour() == "berry" else "registry",
            "flavour": self._target_flavour(),
        }
        return self.default_url

    def _scoped_sources(self) -> list[Source]:
        _, berry = self._rc_paths()
        if not berry.exists():
            return []
        out: list[Source] = []
        for scope, url, _ in _parse_berry(helpers.read_text(berry))[1]:
            source = make_source(
                self.name,
                name=f"@{scope}",
                url=url,
                kind=SourceKind.MIRROR if url != self.default_url else SourceKind.OFFICIAL,
                scope=self.manifest.scope,
                enabled=True,
                file=str(berry),
                is_default=False,
                description=f"scoped registry for @{scope} (edit .yarnrc.yml by hand)",
                extra={
                    "key": f"npmScopes.{scope}.npmRegistryServer",
                    "scope": scope,
                    "config_file": str(berry),
                    "source_of_truth": "file",
                    "read_only": True,
                },
            )
            out.append(source)
        return out

    def list_enabled(self) -> list[Source]:
        enabled = super().list_enabled()
        return enabled + self._scoped_sources()

    def describe_current(self, value: str) -> Source:
        source = super().describe_current(value)
        origin = dict(self._origin or {})
        flavour = origin.get("flavour") or self._target_flavour()
        file = origin.get("file") or str(self._target_path(flavour))
        source.file = file
        source.extra.update(
            {
                "key": origin.get("key")
                or ("npmRegistryServer" if flavour == "berry" else "registry"),
                "config_file": file,
                "flavour": flavour,
                "source_of_truth": origin.get("source_of_truth", "file"),
            }
        )
        return source

    # -- writing -------------------------------------------------------------
    def write_value(self, value: str) -> None:
        flavour = self._target_flavour()
        path = self._target_path(flavour)
        self.value_key = "npmRegistryServer" if flavour == "berry" else "registry"
        if flavour == "berry":
            self._write_berry(path, value)
        else:
            self._write_classic(path, value)

    @staticmethod
    def _write_berry(path: Path, value: str) -> None:
        lines = helpers.read_text(path).splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = _YAML_KV.match(stripped)
            if match and _indent_of(line) == 0 and match.group("key") == "npmRegistryServer":
                lines[index] = f'npmRegistryServer: "{value}"'
                break
        else:
            lines.append(f'npmRegistryServer: "{value}"')
        _write_lines(path, lines)

    @staticmethod
    def _write_classic(path: Path, value: str) -> None:
        lines = helpers.read_text(path).splitlines()
        for index, line in enumerate(lines):
            match = _CLASSIC_REGISTRY.match(line)
            if match:
                indent = line[: len(line) - len(line.lstrip())]
                dash = match.group("dash") or ""
                lines[index] = f'{indent}{dash}registry "{value}"'
                break
        else:
            lines.append(f'registry "{value}"')
        _write_lines(path, lines)

    def clear_value(self) -> bool:
        removed = False
        classic, berry = self._rc_paths()
        if berry.exists():
            lines = helpers.read_text(berry).splitlines()
            kept = [
                line
                for line in lines
                if not (
                    _indent_of(line) == 0
                    and (match := _YAML_KV.match(line.strip()))
                    and match.group("key") == "npmRegistryServer"
                )
            ]
            if len(kept) != len(lines):
                removed = True
                _write_lines(berry, kept)
                if not helpers.read_text(berry).strip():
                    berry.unlink(missing_ok=True)
        if classic.exists():
            lines = helpers.read_text(classic).splitlines()
            kept = [line for line in lines if not _CLASSIC_REGISTRY.match(line)]
            if len(kept) != len(lines):
                removed = True
                _write_lines(classic, kept)
                if not helpers.read_text(classic).strip():
                    classic.unlink(missing_ok=True)
        return removed

    # -- mutations -----------------------------------------------------------
    @staticmethod
    def _is_scoped(source: Source) -> bool:
        """True for a ``npmScopes`` entry, which this plugin only reports."""
        return bool(
            source.extra.get("read_only")
            or source.extra.get("scope")
            or str(source.extra.get("key", "")).startswith("npmScopes.")
        )

    def _scoped_refusal(self, source: Source) -> OperationResult:
        _, berry = self._rc_paths()
        return fail(
            f"yarn: scoped registry @{source.extra.get('scope')} lives in {berry}",
            hint="scoped registries are read-only here; edit the file directly",
        )

    def add(self, source: Source) -> OperationResult:
        if self._is_scoped(source):
            return self._scoped_refusal(source)
        try:
            return super().add(source)
        except OSError as exc:
            return fail(f"yarn: cannot write {self._target_path()}: {exc}")

    def remove(self, source: Source) -> OperationResult:
        if self._is_scoped(source):
            return self._scoped_refusal(source)
        try:
            return super().remove(source)
        except OSError as exc:
            return fail(f"yarn: cannot write {self._target_path()}: {exc}")

    def disable(self, source: Source) -> OperationResult:
        if self._is_scoped(source):
            return self.remove(source)
        try:
            return super().disable(source)
        except OSError as exc:
            return fail(f"yarn: cannot write {self._target_path()}: {exc}")

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        """No reliable yarn connectivity check exists; see the module docstring."""
        return None

    def refresh(self) -> OperationResult:
        return ok("nothing to refresh")


__all__ = ["YarnManager"]
