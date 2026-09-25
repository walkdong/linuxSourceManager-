"""pip (PyPI) index manager plugin - the one plugin with real multi-source handling.

Unlike npm/yarn/pnpm, one pip configuration file can name several indexes at once, so
this plugin exposes more than a single "current" value:

* ``index-url``       - the primary index, reported as the *default* enabled source;
* ``extra-index-url`` - zero or more additional indexes, each reported as its own
  enabled source with ``extra["key"] == "extra-index-url"`` and ``is_default=False``.

``add()`` decides where a new source goes via ``source.extra.get("key", "index-url")``,
and ``remove()``/``disable()`` of an extra index removes exactly that entry (its own
physical line, or one continuation line of the ``extra-index-url`` value) without
touching the primary index or any unrelated setting.

Config file precedence
----------------------
#. ``$SRCMGR_PIP_CONF`` (environment override used by the test-suite)
#. ``$XDG_CONFIG_HOME/pip/pip.conf`` (``~/.config/pip/pip.conf``)
#. ``~/.pip/pip.conf`` (legacy per-user location)
#. ``/etc/pip.conf`` (system-wide)

Reading follows that order: the first existing file that defines ``index-url`` wins,
then ``pip config get global.index-url`` is tried, then the ecosystem default
(``https://pypi.org/simple``).  The file that answered is reported through
``Source.file`` and ``extra["config_file"]``.

Writing never touches ``/etc/pip.conf`` while a user file can be used instead: if only
the system file exists, is writable and we are running as root the system file is
edited, otherwise the highest-precedence *user* file is created/updated.  pip gives the
user file precedence over the system one, so the effective configuration still changes -
without srcmgr silently rewriting a machine-wide default for every user.

INI editing
-----------
pip parses ``pip.conf`` with :mod:`configparser`, which rejects a repeated option name
(``extra-index-url = a`` on two lines is a hard error: "option already exists"), so the
list form pip documents and accepts is used instead::

    [global]
    index-url = https://pypi.org/simple
    extra-index-url =
        https://a.example/simple
        https://b.example/simple

The core :func:`~srcmgr.core.helpers.ini_get` / :func:`~srcmgr.core.helpers.ini_set` /
:func:`~srcmgr.core.helpers.ini_unset` helpers cover the simple one-line case, while
:func:`_entries` and its friends below handle repeated/indented (continuation) lines so
that removing one index never orphans a URL or merges two keys into one.

Plain-HTTP indexes
------------------
Writing an ``http://`` index also adds its host to ``trusted-host`` (pip refuses
plain-HTTP indexes otherwise).  Removing that index drops the ``trusted-host`` entry
again when it was the one we added.

Environment overrides (used by the test-suite to isolate a temporary HOME)
--------------------------------------------------------------------------
``SRCMGR_PIP_CONF`` - absolute path inserted at the front of the candidate list.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from srcmgr.core import health, helpers
from srcmgr.core.api import OperationResult, Source, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.helpers import SingleValueManager, fail, make_source, ok
from srcmgr.core.paths import atomic_write_text, is_root, is_writable

PRIMARY_KEY = "index-url"
EXTRA_KEY = "extra-index-url"
TRUSTED_HOST_KEY = "trusted-host"

AUTH_STATUSES = frozenset({401, 403})

#: ``key = value`` / ``key: value`` inside a section.
_KEY_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\-\[\]]+)\s*[:=]\s*(?P<value>.*?)\s*$")


# ---------------------------------------------------------------------------
# tiny process helper
# ---------------------------------------------------------------------------
def _run(command: Sequence[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess | None:
    try:
        return helpers.run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    text = "\n".join(lines).strip("\n")
    atomic_write_text(path, text + "\n" if text else "")


# ---------------------------------------------------------------------------
# line-level INI helpers (continuation / repeated-key aware)
# ---------------------------------------------------------------------------
def _section_bounds(lines: Sequence[str], section: str) -> tuple[int | None, int]:
    """Return ``(header_index_or_None, end_index)`` for ``[section]``."""
    header = f"[{section}]"
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == header.lower():
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def _entries(lines: Sequence[str], section: str) -> list[dict[str, Any]]:
    """Parse ``[section]`` into ``{key, header, lines, tokens}`` records.

    Indented lines after a key are treated as further values of that key
    (configparser's multi-line form, which is how pip stores several extra indexes);
    a non-indented line or a comment ends a value, while blank lines in between are
    tolerated but are *not* claimed by the record so that rewriting one key never
    reformats the rest of the file.
    """
    start, end = _section_bounds(lines, section)
    records: list[dict[str, Any]] = []
    if start is None:
        return records
    index = start + 1
    while index < end:
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            index += 1
            continue
        match = _KEY_RE.match(line)
        if not match or line[match.end("key") :].lstrip().startswith("://"):
            # Not a key line (a bare URL continuation line matches the pattern too).
            index += 1
            continue
        key = match.group("key").lower()
        record: dict[str, Any] = {
            "key": key,
            "header": index,
            "lines": [index],
            "tokens": list(match.group("value").split()),
        }
        index += 1
        pending_blanks: list[int] = []
        while index < end:
            nxt = lines[index]
            if not nxt.strip():
                pending_blanks.append(index)
                index += 1
                continue
            if nxt.strip().startswith(("#", ";")) or not nxt[:1].isspace():
                break
            record["lines"].extend(pending_blanks)
            pending_blanks.clear()
            record["tokens"].extend(nxt.split())
            record["lines"].append(index)
            index += 1
        records.append(record)
    return records


def _key_entries(lines: Sequence[str], section: str, key: str) -> list[dict[str, Any]]:
    return [entry for entry in _entries(lines, section) if entry["key"] == key]


def _format_block(key: str, values: Sequence[str]) -> list[str]:
    if not values:
        return []
    if len(values) == 1:
        return [f"{key} = {values[0]}"]
    return [f"{key} =", *[f"    {value}" for value in values]]


def _ini_get_all(path: Path, section: str, key: str) -> list[str]:
    """Every value configured for ``key`` (repeated keys and continuations)."""
    text = helpers.read_text(path)
    if not text:
        return []
    out: list[str] = []
    for entry in _key_entries(text.splitlines(), section, key):
        for token in entry["tokens"]:
            if token not in out:
                out.append(token)
    return out


def _ini_set_key(path: Path, section: str, key: str, values: Sequence[str]) -> None:
    """Write ``key`` as a well-formed single/continuation block, preserving all else."""
    text = helpers.read_text(path)
    lines = text.splitlines()
    matches = _key_entries(lines, section, key)
    block = _format_block(key, values)
    if matches:
        first = matches[0]["header"]
        drop = {idx for entry in matches for idx in entry["lines"]}
        out: list[str] = []
        for index, line in enumerate(lines):
            if index == first:
                out.extend(block)
            elif index in drop:
                continue
            else:
                out.append(line)
        _write_lines(path, out)
        return
    start, end = _section_bounds(lines, section)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.extend(block)
    else:
        lines = [*lines[:end], *block, *lines[end:]]
    _write_lines(path, lines)


def _ini_remove_key(path: Path, section: str, key: str) -> bool:
    """Remove ``key`` and all of its continuation lines from ``section``."""
    lines = helpers.read_text(path).splitlines()
    matches = _key_entries(lines, section, key)
    if not matches:
        return False
    drop = {idx for entry in matches for idx in entry["lines"]}
    _write_lines(path, [line for index, line in enumerate(lines) if index not in drop])
    return True


def _ini_remove_value(path: Path, section: str, key: str, value: str) -> bool:
    """Remove one value of ``key``; drops the line when it was the last one."""
    lines = helpers.read_text(path).splitlines()
    matches = [entry for entry in _key_entries(lines, section, key) if value in entry["tokens"]]
    if not matches:
        return False
    replace: dict[int, list[str]] = {}
    drop: set[int] = set()
    for entry in matches:
        remaining = [token for token in entry["tokens"] if token != value]
        replace[entry["header"]] = _format_block(key, remaining)
        drop.update(idx for idx in entry["lines"] if idx != entry["header"])
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in replace:
            out.extend(replace[index])
        elif index in drop:
            continue
        else:
            out.append(line)
    _write_lines(path, out)
    return True


def _ini_replace_value(path: Path, section: str, key: str, old: str, new: str) -> bool:
    """Swap one value of ``key`` for another, keeping its line/position."""
    lines = helpers.read_text(path).splitlines()
    matches = [entry for entry in _key_entries(lines, section, key) if old in entry["tokens"]]
    if not matches:
        return False
    replace: dict[int, list[str]] = {}
    drop: set[int] = set()
    for entry in matches:
        tokens = [new if token == old else token for token in entry["tokens"]]
        replace[entry["header"]] = _format_block(key, tokens)
        drop.update(idx for idx in entry["lines"] if idx != entry["header"])
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in replace:
            out.extend(replace[index])
        elif index in drop:
            continue
        else:
            out.append(line)
    _write_lines(path, out)
    return True


# ---------------------------------------------------------------------------
# the plugin
# ---------------------------------------------------------------------------
class PipManager(SingleValueManager):
    """Manage ``index-url`` / ``extra-index-url`` in pip configuration."""

    value_key = PRIMARY_KEY
    default_name = "pypi"
    default_url = "https://pypi.org/simple"
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
    def _system_path(self) -> Path:
        """System-wide config; overridable in tests to simulate ``/etc/pip.conf``."""
        return Path("/etc/pip.conf")

    def _candidate_paths(self) -> list[Path]:
        paths: list[Path] = []
        override = os.environ.get("SRCMGR_PIP_CONF", "").strip()
        if override:
            paths.append(Path(override).expanduser())
        paths.append(helpers.xdg_config_home() / "pip" / "pip.conf")
        paths.append(helpers.home() / ".pip" / "pip.conf")
        system = self._system_path()
        out: list[Path] = []
        for path in paths:
            if path not in out:
                out.append(path)
        if system not in out:
            out.append(system)
        return out

    def _user_paths(self) -> list[Path]:
        return [path for path in self._candidate_paths() if path != self._system_path()]

    def config_files(self) -> list[Path]:
        paths = self._candidate_paths()
        declared = self.manifest.config_paths or [
            "~/.config/pip/pip.conf",
            "~/.pip/pip.conf",
            "/etc/pip.conf",
        ]
        for raw in declared:
            candidate = Path(raw).expanduser()
            if candidate not in paths:
                paths.append(candidate)
        return paths

    def _write_target(self) -> Path:
        """Highest-precedence writable user file; the system file only for root."""
        user = self._user_paths()
        for path in user:
            if path.exists() and is_writable(path):
                return path
        system = self._system_path()
        only_system = system.exists() and not any(path.exists() for path in user)
        if only_system and is_root() and is_writable(system):
            return system
        return user[0]

    # -- detection -----------------------------------------------------------
    def detect(self) -> bool:
        try:
            if helpers.which("pip") or helpers.which("pip3"):
                return True
            return any(path.exists() for path in self._candidate_paths())
        except Exception:  # noqa: BLE001 - detection must never raise
            return False

    def detect_detail(self) -> dict[str, Any]:
        return {
            "available": self.detect(),
            "files": [str(p) for p in self._candidate_paths() if p.exists()],
            "write_target": str(self._write_target()),
            "system_file": str(self._system_path()),
        }

    # -- reading -------------------------------------------------------------
    def _configured_index(self) -> tuple[Path | None, str]:
        """First existing candidate that defines ``index-url``."""
        for path in self._candidate_paths():
            if not path.exists():
                continue
            value = _clean(helpers.ini_get(path, "global", PRIMARY_KEY))
            if value:
                return path, value
        return None, ""

    def _configured_extras(self) -> tuple[Path | None, list[str]]:
        """Effective ``extra-index-url`` list (pip does not merge across files)."""
        for path in self._candidate_paths():
            if not path.exists():
                continue
            values = [_clean(v) for v in _ini_get_all(path, "global", EXTRA_KEY) if _clean(v)]
            if values:
                return path, values
        return None, []

    def read_value(self) -> str:
        self._origin = {}
        path, value = self._configured_index()
        if value:
            self._origin = {"file": str(path), "source_of_truth": "file", "key": PRIMARY_KEY}
            return value

        binary = helpers.which("pip") or helpers.which("pip3")
        if binary:
            completed = _run([binary, "config", "get", "global.index-url"])
            if completed is not None and completed.returncode == 0:
                lines = (completed.stdout or "").strip().splitlines()
                from_cli = _clean(lines[0]) if lines else ""
                if from_cli and from_cli.lower() not in {"undefined", "null", "none"}:
                    self._origin = {
                        "file": "",
                        "source_of_truth": "pip config",
                        "key": PRIMARY_KEY,
                    }
                    return from_cli

        self._origin = {"file": "", "source_of_truth": "default", "key": PRIMARY_KEY}
        return self.default_url

    def _make_extra_source(self, url: str, path: Path | None) -> Source:
        file = str(path or self._write_target())
        return make_source(
            self.name,
            name=self._name_for(url),
            url=url,
            scope=self.manifest.scope,
            enabled=True,
            file=file,
            is_default=False,
            description="additional index (extra-index-url)",
            extra={
                "key": EXTRA_KEY,
                "config_file": file,
                "source_of_truth": "file",
            },
        )

    def list_enabled(self) -> list[Source]:
        sources = [self.describe_current(self.read_value())]
        path, extras = self._configured_extras()
        known = {source.url.rstrip("/") for source in sources}
        for url in extras:
            if url.rstrip("/") in known:
                continue
            sources.append(self._make_extra_source(url, path))
        return sources

    def current(self) -> Source | None:
        items = self.list_enabled()
        for source in items:
            if source.is_default:
                return source
        return items[0] if items else None

    def describe_current(self, value: str) -> Source:
        source = super().describe_current(value)
        origin = dict(self._origin or {})
        file = origin.get("file") or str(self._write_target())
        source.file = file
        source.extra.update(
            {
                "key": PRIMARY_KEY,
                "config_file": file,
                "source_of_truth": origin.get("source_of_truth", "file"),
            }
        )
        return source

    # -- writing -------------------------------------------------------------
    def _add_trusted_host(self, path: Path, url: str) -> None:
        if not url.startswith("http://"):
            return
        host = _host_of(url)
        if not host:
            return
        values = _ini_get_all(path, "global", TRUSTED_HOST_KEY)
        if host not in values:
            _ini_set_key(path, "global", TRUSTED_HOST_KEY, [*values, host])

    def _remove_trusted_host(self, path: Path, url: str) -> None:
        if not url.startswith("http://"):
            return
        host = _host_of(url)
        if host:
            _ini_remove_value(path, "global", TRUSTED_HOST_KEY, host)

    def write_value(self, value: str) -> None:
        path = self._write_target()
        _ini_set_key(path, "global", PRIMARY_KEY, [value.strip()])
        self._add_trusted_host(path, value.strip())

    def clear_value(self) -> bool:
        path, current = self._configured_index()
        if path is None or not current:
            return False
        if path == self._system_path() and not (is_root() and is_writable(path)):
            return False
        removed = _ini_remove_key(path, "global", PRIMARY_KEY)
        if removed:
            self._remove_trusted_host(path, current)
            if not helpers.read_text(path).strip():
                path.unlink(missing_ok=True)
        return removed

    # -- mutations -----------------------------------------------------------
    def add(self, source: Source) -> OperationResult:
        key = str(source.extra.get("key") or PRIMARY_KEY).strip().lower()
        value = self._value_of(source).strip()
        if not value:
            return fail("pip: source has no URL to use as an index")
        if key not in {PRIMARY_KEY, EXTRA_KEY}:
            return fail(
                f"pip: unsupported key {key!r}",
                hint=f"use {PRIMARY_KEY} or {EXTRA_KEY}",
            )
        try:
            if key == PRIMARY_KEY:
                previous = self.read_value()
                if previous.rstrip("/") == value.rstrip("/"):
                    return ok(f"pip: already using {source.name}", file=str(self._write_target()))
                self.write_value(value)
                return ok(
                    f"pip: {PRIMARY_KEY} -> {value}",
                    changed=[value],
                    previous=previous,
                    key=key,
                    file=str(self._write_target()),
                )

            winning, existing = self._configured_extras()
            if value.rstrip("/") in {item.rstrip("/") for item in existing}:
                return ok(f"pip: {value} is already an extra index", file=str(winning or ""))
            target = winning if (winning and is_writable(winning)) else self._write_target()
            _ini_set_key(target, "global", EXTRA_KEY, [*existing, value])
            self._add_trusted_host(target, value)
            return ok(
                f"pip: {EXTRA_KEY} += {value}",
                changed=[value],
                key=key,
                file=str(target),
                extras=[*existing, value],
            )
        except OSError as exc:
            return fail(f"pip: cannot write pip configuration: {exc}")

    def _file_for_extra(self, source: Source, value: str) -> Path | None:
        explicit = source.file or str(source.extra.get("config_file") or "")
        if explicit:
            candidate = Path(explicit)
            if candidate.exists() and value in _ini_get_all(candidate, "global", EXTRA_KEY):
                return candidate
        for path in self._candidate_paths():
            if path.exists() and value in _ini_get_all(path, "global", EXTRA_KEY):
                return path
        winning, existing = self._configured_extras()
        if winning is not None and value in existing:
            return winning
        return Path(explicit) if explicit else None

    def remove(self, source: Source) -> OperationResult:
        key = str(source.extra.get("key") or PRIMARY_KEY).strip().lower()
        value = self._value_of(source).strip()
        if key == PRIMARY_KEY:
            current = self.read_value()
            if value and current and value.rstrip("/") != current.rstrip("/"):
                return fail(f"pip: {source.name} is not the active {PRIMARY_KEY}")
            try:
                if self.clear_value():
                    return ok(f"pip: cleared {PRIMARY_KEY}", changed=[value])
            except OSError as exc:
                return fail(f"pip: cannot write pip configuration: {exc}")
            return ok("pip: nothing to remove")

        try:
            path = self._file_for_extra(source, value)
            if path is None:
                return fail(f"pip: {value} is not configured as an {EXTRA_KEY}")
            if not is_writable(path):
                return fail(
                    f"pip: {path} is not writable",
                    hint="run as root or edit the file by hand",
                )
            removed = _ini_remove_value(path, "global", EXTRA_KEY, value)
            if not removed:
                return fail(f"pip: {value} is not configured as an {EXTRA_KEY}")
            self._remove_trusted_host(path, value)
            if not helpers.read_text(path).strip():
                path.unlink(missing_ok=True)
            return ok(f"pip: {EXTRA_KEY} -= {value}", changed=[value], key=key, file=str(path))
        except OSError as exc:
            return fail(f"pip: cannot write pip configuration: {exc}")

    def enable(self, source: Source) -> OperationResult:
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        key = str(source.extra.get("key") or PRIMARY_KEY).strip().lower()
        if key == EXTRA_KEY:
            # An extra index has no "off" state other than being absent.
            return self.remove(source)
        current = self.read_value()
        value = self._value_of(source).strip()
        if current and value and current.rstrip("/") != value.rstrip("/"):
            return fail(f"pip: {source.name} is not active")
        try:
            self.write_value(self.default_url)
        except OSError as exc:
            return fail(f"pip: cannot write pip configuration: {exc}")
        return ok(f"pip: {PRIMARY_KEY} -> {self.default_url} (ecosystem default)")

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        new_value = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_value:
            return fail("pip: only 'url' can be edited for this manager")
        key = str(source.extra.get("key") or PRIMARY_KEY).strip().lower()
        if key != EXTRA_KEY:
            try:
                self.write_value(new_value)
            except OSError as exc:
                return fail(f"pip: cannot write pip configuration: {exc}")
            return ok(f"pip: {PRIMARY_KEY} -> {new_value}", changed=[new_value])

        old = self._value_of(source).strip()
        try:
            path = self._file_for_extra(source, old)
            if path is None or not is_writable(path):
                return fail(f"pip: {old} is not a writable {EXTRA_KEY}")
            if not _ini_replace_value(path, "global", EXTRA_KEY, old, new_value):
                return fail(f"pip: {old} is not configured as an {EXTRA_KEY}")
            self._remove_trusted_host(path, old)
            self._add_trusted_host(path, new_value)
        except OSError as exc:
            return fail(f"pip: cannot write pip configuration: {exc}")
        return ok(f"pip: {EXTRA_KEY} {old} -> {new_value}", changed=[new_value], file=str(path))

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        return ["pip", "index", "versions", "pip"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """HTTP-probe the index; 401/403 count as reachable (private indexes)."""
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


__all__ = ["PipManager"]
