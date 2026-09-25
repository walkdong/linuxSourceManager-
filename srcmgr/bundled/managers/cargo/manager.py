"""Cargo (crates.io) manager plugin.

How Cargo is redirected
-----------------------
Cargo has no single ``registry =`` setting: a registry is *replaced* by another
named source table::

    [source.crates-io]
    replace-with = "tuna"

    [source.tuna]
    registry = "sparse+https://mirrors.ustc.edu.cn/crates.io-index/"

Alternate registries (``cargo publish --registry``) are declared separately::

    [registries.my-registry]
    index = "sparse+https://my.registry/index/"

Both shapes are exposed as sources.  The *active* source is the ``registry`` URL of
the table named by ``[source.crates-io] replace-with`` (or crates.io itself when no
replacement is configured).

Why not ``tomllib``/``tomli_w``?
-------------------------------
Re-serializing the document would destroy the user's comments, key order and
formatting, and Cargo configuration files are hand-written.  This module therefore
ships a small, deliberately narrow TOML *text* editor that only understands table
headers (``[a.b]``), dotted/quoted table names, ``key = "value"`` lines inside a
table and section removal.  Everything else in the file is copied through
byte-for-byte.  The documented limitation: a multi-line array value whose
continuation lines start with ``[`` is not supported (Cargo does not use those in
the tables this plugin touches).

Environment resolution happens at call time (never at import), so tests can point
``HOME``/``CARGO_HOME``/``SRCMGR_CARGO_CONFIG`` at a temporary directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from srcmgr.core import helpers
from srcmgr.core.api import OperationResult, Source, SourceKind, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.health import probe_url
from srcmgr.core.helpers import SingleValueManager, fail, make_source, ok, read_text
from srcmgr.core.paths import atomic_write_text, ensure_dir

MANAGER = "cargo"

#: crates.io sparse index; what Cargo uses when nothing is configured.
DEFAULT_INDEX = "https://index.crates.io/"
#: URL suffix that proves a *sparse* index is alive.
SPARSE_HEALTH_SUFFIX = "/config.json"
#: Accepted by a healthy index (GitHub raw answers 403 to unknown user agents).
OK_HTTP = {200, 403}

CARGO_HOME_ENV = "CARGO_HOME"
CONFIG_OVERRIDE_ENV = "SRCMGR_CARGO_CONFIG"


# ---------------------------------------------------------------------------
# tiny TOML *text* editor
#
# Only the subset Cargo uses is implemented: `[table]` headers, dotted/quoted
# table names and `key = "value"` pairs.  Nothing is re-serialized, so comments,
# blank lines, ordering and unrelated tables survive untouched.
# ---------------------------------------------------------------------------
_HEADER_RE = re.compile(r"^\s*\[\s*(?P<name>[^\[\]]+?)\s*\]\s*(?:#.*)?$")
_KEY_RE = re.compile(
    r"""^\s*(?P<key>[A-Za-z0-9_\-]+|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    r"""\s*=\s*(?P<value>.*?)\s*$"""
)


def _is_header(line: str) -> bool:
    """True for ``[table]`` / ``[[array-of-table]]`` lines (not array values)."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return False
    if stripped.startswith("[["):
        return True
    return _HEADER_RE.match(line) is not None


def _strip_comment(value: str) -> str:
    """Drop a trailing ``# comment`` that is not inside a quoted string."""
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#":
            return value[:index].strip()
    return value.strip()


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _table_segments(raw: str) -> list[str]:
    """Split ``source."my-mirror"`` into ``["source", "my-mirror"]``."""
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in raw:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ".":
            segments.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    segments.append("".join(current).strip())
    return [segment for segment in segments if segment]


def _section_span(lines: Sequence[str], table: str) -> tuple[int, int] | None:
    """Return ``(header_index, end_index)`` of ``[table]`` or ``None``."""
    wanted = _table_segments(table)
    start: int | None = None
    for index, line in enumerate(lines):
        if not _is_header(line):
            continue
        if start is None:
            match = _HEADER_RE.match(line)
            if match and _table_segments(match.group("name")) == wanted:
                start = index
            continue
        return start, index
    if start is None:
        return None
    return start, len(lines)


def _section_key(lines: Sequence[str], table: str, key: str) -> str | None:
    span = _section_span(lines, table)
    if span is None:
        return None
    start, end = span
    for line in lines[start + 1 : end]:
        match = _KEY_RE.match(line)
        if match and _unquote(match.group("key")) == key:
            return _unquote(_strip_comment(match.group("value")))
    return None


def _set_section_keys(text: str, table: str, values: Mapping[str, str]) -> str:
    """Set ``key = "value"`` pairs inside ``[table]``, creating the table if needed."""
    lines = text.splitlines()
    span = _section_span(lines, table)
    if span is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f"[{table}]")
        lines.extend(f'{key} = "{value}"' for key, value in values.items())
        return "\n".join(lines) + "\n"

    start, end = span
    remaining = dict(values)
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if not match:
            continue
        key = _unquote(match.group("key"))
        if key in remaining:
            lines[index] = f'{key} = "{remaining.pop(key)}"'
    insert_at = start + 1
    for index in range(end - 1, start, -1):
        if lines[index].strip():
            insert_at = index + 1
            break
    for key, value in remaining.items():
        lines.insert(insert_at, f'{key} = "{value}"')
        insert_at += 1
    return "\n".join(lines) + "\n"


def _unset_section_keys(text: str, table: str, keys: Sequence[str]) -> str:
    """Remove keys from ``[table]``; the header itself stays (even if empty)."""
    span = _section_span(text.splitlines(), table)
    if span is None:
        return text
    start, end = span
    lowered = set(keys)
    lines = text.splitlines()
    out = lines[: start + 1]
    for line in lines[start + 1 : end]:
        match = _KEY_RE.match(line)
        if match and _unquote(match.group("key")) in lowered:
            continue
        out.append(line)
    out.extend(lines[end:])
    return "\n".join(out) + "\n"


def _drop_section(text: str, table: str) -> str:
    """Delete ``[table]`` and its body, keeping the rest of the file intact."""
    span = _section_span(text.splitlines(), table)
    if span is None:
        return text
    start, end = span
    lines = text.splitlines()
    del lines[start:end]
    # Collapse the blank separator line the section may have left behind.
    if 0 < start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
        del lines[start]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _sanitize(text: str) -> str:
    """Turn a display name into a Cargo-legal source table name."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]+", "-", (text or "").strip().lower()).strip("-.")
    return cleaned or "mirror"


# ---------------------------------------------------------------------------
# plugin
# ---------------------------------------------------------------------------
class CargoManager(SingleValueManager):
    """Manages ``[source.crates-io]``/``[registries.*]`` in Cargo's config.toml."""

    #: The value this manager switches is Cargo's source replacement.
    value_key = "replace-with"
    default_name = "crates-io"
    default_url = DEFAULT_INDEX
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

    # ------------------------------------------------------------------ paths
    def _candidates(self) -> list[Path]:
        """Config candidates in Cargo's own precedence order."""
        override = os.environ.get(CONFIG_OVERRIDE_ENV)
        if override:
            return [Path(override).expanduser()]
        cargo_home = os.environ.get(CARGO_HOME_ENV)
        base = Path(cargo_home).expanduser() if cargo_home else helpers.home() / ".cargo"
        return [base / "config.toml", base / "config"]

    def config_path(self) -> Path:
        """The file that is read *and* written (first existing candidate)."""
        candidates = self._candidates()
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def config_files(self) -> list[Path]:
        primary = self.config_path()
        files = [primary]
        for candidate in self._candidates():
            if candidate != primary and candidate.exists():
                files.append(candidate)
        return files

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        # A hand-written config is enough to manage sources even without the binary.
        return helpers.which("cargo") is not None or self.config_path().exists()

    def detect_detail(self) -> dict[str, Any]:
        path = self.config_path()
        return {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": helpers.which("cargo") or "",
            "config": str(path),
            "config_exists": path.exists(),
            "replace_with": self._replace_with(),
            "registries": [name for name, _ in self._registries()],
        }

    # ------------------------------------------------------------- TOML read
    def _read_document(self) -> str:
        return read_text(self.config_path())

    def _write_document(self, text: str) -> None:
        path = self.config_path()
        ensure_dir(path.parent)
        atomic_write_text(path, text)

    def _replace_with(self) -> str:
        """Name of the source table ``[source.crates-io]`` points at."""
        return _section_key(self._read_document().splitlines(), "source.crates-io", "replace-with") or ""

    def _source_url(self, table_name: str) -> str:
        """``registry`` URL declared by ``[source.<table_name>]``."""
        if not table_name:
            return ""
        lines = self._read_document().splitlines()
        return _section_key(lines, f"source.{table_name}", "registry") or ""

    def _registries(self) -> list[tuple[str, str]]:
        """Every ``[registries.<name>] index = ...`` entry, in file order."""
        lines = self._read_document().splitlines()
        out: list[tuple[str, str]] = []
        for line in lines:
            match = _HEADER_RE.match(line)
            if not match:
                continue
            segments = _table_segments(match.group("name"))
            if len(segments) == 2 and segments[0] == "registries":
                index = _section_key(lines, f"registries.{segments[1]}", "index")
                out.append((segments[1], index or ""))
        return out

    # ---------------------------------------------------------- value access
    def read_value(self) -> str:
        """Active crates.io index URL (via ``replace-with``), else crates.io itself."""
        table = self._replace_with()
        if not table or table == self.default_name:
            return self.default_url
        return self._source_url(table)

    def write_value(self, value: str, *, table: str | None = None) -> None:
        """Install ``value`` as the crates.io replacement under ``[source.<table>]``.

        The table name comes from ``table``, the pending provider key set by
        :meth:`add`, or is derived from the URL host.  Both blocks are written
        idempotently: an existing ``[source.<table>]`` keeps its other keys and
        comments, only ``registry`` is refreshed.
        """
        name = table or getattr(self, "_pending_table", "") or self._table_for_url(value)
        text = self._read_document()
        text = _set_section_keys(
            text, f"source.{name}", {"registry": value}
        )
        text = _set_section_keys(text, "source.crates-io", {"replace-with": name})
        self._write_document(text)

    def clear_value(self) -> bool:
        """Remove ``replace-with`` and drop the mirror table it pointed at."""
        text = self._read_document()
        if not text.strip():
            return False
        table = self._replace_with()
        new_text = _unset_section_keys(text, "source.crates-io", ["replace-with"])
        if table and table != self.default_name:
            new_text = _drop_section(new_text, f"source.{table}")
        if new_text == text:
            return False
        self._write_document(new_text)
        return True

    # ------------------------------------------------------------- listings
    def describe_current(self, value: str) -> Source:
        table = self._replace_with() or self.default_name
        return self._make_source(
            name=self._name_for(value),
            url=value,
            table=table,
            key="source",
            is_default=True,
            enabled=True,
        )

    def list_enabled(self) -> list[Source]:
        """The active index plus every declared ``[registries.*]`` entry."""
        out: list[Source] = []
        value = self.read_value()
        if value:
            out.append(self.describe_current(value))
        elif not self._replace_with():
            # No replacement configured: crates.io itself is in effect.
            out.append(self.describe_current(self.default_url))
        for name, index in self._registries():
            if not index:
                continue
            out.append(
                self._make_source(
                    name=name,
                    url=index,
                    table=f"registries.{name}",
                    key="registries",
                    registry_name=name,
                    is_default=False,
                    enabled=True,
                )
            )
        return out

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Provider catalogues, with crates.io itself as the built-in fallback."""
        sources = list(self._registry_sources())
        if not any(source.url.rstrip("/") == self.default_url.rstrip("/") for source in sources):
            sources.append(
                self._make_source(
                    name="crates.io",
                    url=self.default_url,
                    table=self.default_name,
                    key="source",
                    kind=SourceKind.OFFICIAL,
                    enabled=False,
                    is_default=True,
                    provider_key="crates-io",
                )
            )
        return sources

    def current(self) -> Source | None:
        for source in self.list_enabled():
            if source.is_default:
                return source
        return None

    def _make_source(
        self,
        *,
        name: str,
        url: str,
        table: str,
        key: str,
        kind: SourceKind = SourceKind.MIRROR,
        enabled: bool = True,
        is_default: bool = False,
        registry_name: str = "",
        provider_key: str = "",
    ) -> Source:
        if kind is SourceKind.MIRROR and url.rstrip("/") == self.default_url.rstrip("/"):
            kind = SourceKind.OFFICIAL
        source = make_source(
            MANAGER,
            name=name,
            url=url,
            kind=kind,
            scope=self.manifest.scope,
            enabled=enabled,
            is_default=is_default,
            file=str(self.config_path()),
            provider_key=provider_key,
            health={"url": _probe_target(url)[0], "mode": "http"},
            extra={
                "value": url,
                "key": key,
                "table": table,
                "sparse": url.startswith("sparse+"),
                "registries_key": registry_name,
            },
        )
        return source

    def _name_for(self, value: str) -> str:
        if value.rstrip("/") == self.default_url.rstrip("/"):
            return self.default_name
        provider = self.registry_lookup(value)
        if provider is not None and provider.name:
            return provider.name
        # The user's own table name (``[source.ustc]``) is the friendliest label.
        table = self._replace_with()
        if table and table != self.default_name:
            return table
        return super()._name_for(value)

    def _table_for_url(self, value: str) -> str:
        host = re.sub(r"^[a-z]+\+", "", (value or "").strip())
        host = re.sub(r"^https?://", "", host).split("/")[0]
        return _sanitize(host or "mirror")

    def _table_name_for(self, source: Source) -> str:
        for candidate in (
            source.provider_key,
            str(source.extra.get("table") or ""),
            str(source.extra.get("source_name") or ""),
            source.name,
        ):
            if candidate:
                return _sanitize(str(candidate))
        return self._table_for_url(source.url)

    # ------------------------------------------------------------ mutations
    def add(self, source: Source) -> OperationResult:
        value = self._value_of(source)
        if not value:
            return fail("cargo: source has no index URL")
        table = self._table_name_for(source)
        previous = self.read_value()
        if previous.rstrip("/") == value.rstrip("/") and self._replace_with() in (table, ""):
            return ok(f"cargo: already using {source.name}")
        self._pending_table = table
        try:
            self.write_value(value, table=table)
        finally:
            self._pending_table = ""
        return ok(
            f"cargo: crates.io -> {source.name} ([source.{table}])",
            changed=[str(self.config_path())],
            previous=previous,
            table=table,
        )

    def remove(self, source: Source) -> OperationResult:
        path = self.config_path()
        if source.extra.get("key") == "registries":
            name = str(source.extra.get("registries_key") or source.extra.get("table", "")).split(".")[-1]
            if not name:
                return fail("cargo: cannot determine the registry name to remove")
            text = self._read_document()
            new_text = _drop_section(text, f"registries.{name}")
            if new_text == text:
                return fail(f"cargo: registry {name} is not configured")
            self._write_document(new_text)
            return ok(f"cargo: removed registry {name}", changed=[str(path)])
        current = self.read_value()
        if not current:
            return fail("cargo: no crates.io replacement configured")
        if not self.clear_value():
            return fail("cargo: nothing to remove")
        return ok(
            f"cargo: crates.io -> {self.default_url} (replacement removed)",
            changed=[str(path)],
        )

    def enable(self, source: Source) -> OperationResult:
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Stop using ``source``; Cargo falls back to crates.io itself."""
        if source.extra.get("key") == "registries":
            return self.remove(source)
        value = self._value_of(source)
        current = self.read_value()
        if value and current and value.rstrip("/") != current.rstrip("/"):
            return fail(f"cargo: {source.name} is not the active replacement")
        if not self.clear_value():
            return ok("cargo: replacement already disabled")
        return ok(
            f"cargo: crates.io replacement disabled ({self.default_url})",
            changed=[str(self.config_path())],
        )

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        new_value = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_value:
            return fail("cargo: only 'url' can be updated for a cargo source")
        if source.extra.get("key") == "registries":
            name = str(source.extra.get("registries_key") or source.extra.get("table", "")).split(".")[-1]
            if not name:
                return fail("cargo: cannot determine the registry name to update")
            text = _set_section_keys(self._read_document(), f"registries.{name}", {"index": new_value})
            self._write_document(text)
            return ok(f"cargo: registry {name} -> {new_value}", changed=[str(self.config_path())])
        table = str(source.extra.get("table") or "").split(".")[-1] or self._replace_with()
        if not table or table == self.default_name:
            table = self._table_name_for(source)
        self.write_value(new_value, table=table)
        return ok(
            f"cargo: [source.{table}] registry -> {new_value}",
            changed=[str(self.config_path())],
        )

    # ---------------------------------------------------------------- health
    def native_test_command(self) -> Sequence[str] | None:
        return ["cargo", "search", "serde", "--limit", "1"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe the index endpoint.

        * ``sparse+https://...`` -> ``<url>/config.json`` (the sparse marker file)
        * plain ``http(s)://...`` -> the URL itself (a git index has no marker file)
        * anything else (``file://``, ``path+...``) is not probeable over HTTP.
        """
        url = source.url or ""
        target, note = _probe_target(url)
        result = TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=url,
            method="HEAD",
            checked_at=utc_now(),
        )
        if not target:
            result.ok = False
            result.status = "unsupported-index"
            result.detail = note
            result.error = note
            return result
        probe = probe_url(target, timeout=timeout)
        result.http_status = probe.http_status
        result.latency_ms = probe.latency_ms
        result.tls_ok = probe.tls_ok
        result.redirected_to = probe.redirected_to
        result.method = probe.method or "HEAD"
        result.ok = bool(probe.ok or probe.http_status in OK_HTTP)
        result.status = "ok" if result.ok else probe.status
        result.error = "" if result.ok else probe.error
        if result.ok and probe.http_status == 403:
            result.detail = f"probed {target}: HTTP 403 is expected for HEAD/without a UA; the host answered"
        else:
            result.detail = f"probed {target}" + (f" ({note})" if note else "")
        return result


def _probe_target(url: str) -> tuple[str, str]:
    """Return ``(probe_url, note)`` for an index URL (``("", reason)`` if not HTTP)."""
    value = (url or "").strip()
    if not value:
        return "", "source has no index URL"
    if value.startswith("sparse+"):
        base = value[len("sparse+") :].rstrip("/")
        if not base.startswith(("http://", "https://")):
            return "", f"sparse index {value!r} is not an HTTP(S) URL"
        return f"{base}{SPARSE_HEALTH_SUFFIX}", "sparse index config.json"
    if value.startswith(("http://", "https://")):
        return value, "git index"
    return "", f"index scheme of {value!r} cannot be probed over HTTP"


__all__ = ["CargoManager"]
