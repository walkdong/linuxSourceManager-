"""Reusable building blocks for manager plugins.

Several ecosystems share the same *shape* of configuration, so the tedious parts are
factored out here instead of being re-implemented (and re-broken) per plugin:

* :class:`SingleValueManager` - ecosystems whose "current source" is one value in a
  key/value file: npm/Yarn/pnpm ``registry``, pip ``index-url``, Cargo
  ``[source.crates-io] replace-with``, RubyGems first ``source`` line, Go ``GOPROXY``.
* :func:`upsert_marked_block` - append/replace a clearly delimited block in a text
  file (used by managers that must not clobber foreign content).
* :func:`replace_xml_element` - Maven ``<mirror>`` handling without reserializing the
  whole document (comments and formatting survive).
* :class:`FileManager` - path resolution + snapshot list + safe read helpers.

Everything writes atomically and preserves the original file mode.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .api import ManagerPlugin, OperationResult, Scope, Source, SourceKind, compute_source_id
from .paths import atomic_write_text, ensure_dir

MARKER_BEGIN = "# >>> source-manager >>>"
MARKER_END = "# <<< source-manager <<<"

ENV_GUARD = "SRCMGR_SAFE_ENV"


# ---------------------------------------------------------------------------
# tiny helpers shared by all plugins
# ---------------------------------------------------------------------------
def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home()).expanduser()


def reads_only() -> bool:
    """True when the environment forbids writes (used by tests/dry-run harnesses)."""
    return os.environ.get(ENV_GUARD, "").strip().lower() in {"1", "true", "yes"}


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home() / ".config").expanduser()


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local/share").expanduser()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def which(program: str) -> str | None:
    return shutil.which(program)


def run(
    command: Sequence[str],
    *,
    timeout: float = 30.0,
    check: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, capturing output; never raises for a non-zero exit."""
    return subprocess.run(  # noqa: S603 - argv is built by plugins, never a shell string
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        input=input_text,
        env=os.environ.copy(),
    )


def make_source(
    manager: str,
    *,
    name: str,
    url: str,
    suite: str = "",
    components: Sequence[str] = (),
    kind: SourceKind = SourceKind.UNKNOWN,
    scope: Scope = Scope.USER,
    enabled: bool = True,
    file: str = "",
    provider: str = "",
    provider_key: str = "",
    description: str = "",
    extra: Mapping[str, Any] | None = None,
    types: Sequence[str] = (),
    architectures: Sequence[str] = (),
    health: Mapping[str, Any] | None = None,
    secret_fields: Sequence[str] = (),
    is_default: bool = False,
) -> Source:
    payload: dict[str, Any] = dict(extra or {})
    source = Source(
        manager=manager,
        name=name,
        url=url,
        suite=suite,
        components=list(components),
        types=list(types),
        architectures=list(architectures),
        kind=kind,
        scope=scope,
        enabled=enabled,
        file=file,
        provider=provider,
        provider_key=provider_key,
        description=description,
        extra=payload,
        health=dict(health or {}),
        secret_fields=list(secret_fields),
        is_default=is_default,
    )
    if not source.id:
        source.id = compute_source_id(manager, url, suite, provider_key or name)
    return source


def ok(message: str, changed: Iterable[str] = (), **details: Any) -> OperationResult:
    return OperationResult(
        ok=True, message=message, changed=list(changed), details=dict(details)
    )


def fail(message: str, **details: Any) -> OperationResult:
    return OperationResult(ok=False, message=message, details=dict(details))


def expand_template(text: str, values: Mapping[str, str]) -> str:
    """``${codename}`` style substitution used by provider template URLs."""
    out = text
    for key, value in values.items():
        out = out.replace("${" + key + "}", value)
    return out


# ---------------------------------------------------------------------------
# marked blocks
# ---------------------------------------------------------------------------
def find_block(text: str, marker_begin: str = MARKER_BEGIN) -> tuple[int, int] | None:
    start = text.find(marker_begin)
    if start < 0:
        return None
    end = text.find(MARKER_END, start)
    if end < 0:
        return None
    return start, end + len(MARKER_END)


def upsert_marked_block(
    path: Path,
    body: str,
    *,
    marker_begin: str = MARKER_BEGIN,
    marker_end: str = MARKER_END,
) -> None:
    """Insert or replace a delimited block in ``path`` (creating it if needed)."""
    text = read_text(path)
    block = f"{marker_begin}\n{body.rstrip()}\n{marker_end}\n"
    found = find_block(text, marker_begin)
    if found is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        new_text = f"{text}{separator}{block}"
    else:
        start, end = found
        new_text = text[:start] + block.rstrip("\n") + text[end:]
        if not new_text.endswith("\n"):
            new_text += "\n"
    ensure_dir(path.parent)
    atomic_write_text(path, new_text)


def remove_marked_block(
    path: Path, *, marker_begin: str = MARKER_BEGIN, marker_end: str = MARKER_END
) -> bool:
    text = read_text(path)
    found = find_block(text, marker_begin)
    if found is None:
        return False
    start, end = found
    new_text = (text[:start] + text[end:]).strip("\n")
    if new_text:
        new_text += "\n"
    if new_text.strip():
        atomic_write_text(path, new_text)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return True


def get_marked_block(
    path: Path, *, marker_begin: str = MARKER_BEGIN, marker_end: str = MARKER_END
) -> str:
    text = read_text(path)
    found = find_block(text, marker_begin)
    if found is None:
        return ""
    start, end = found
    return text[start + len(marker_begin): end - len(marker_end)].strip("\n")


# ---------------------------------------------------------------------------
# key=value files (.npmrc, .yarnrc, pip.conf, .gemrc-ish, go env file)
# ---------------------------------------------------------------------------
KV_PATTERN = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*[:=]\s*(.*?)\s*$")


@dataclass
class KeyValueFile:
    """A naive but format-preserving ``key=value`` / ``key: value`` file editor."""

    path: Path
    separator: str = "="

    def entries(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for line in read_text(self.path).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";", "[")):
                continue
            match = KV_PATTERN.match(line)
            if match:
                out.append((match.group(1), match.group(2)))
        return out

    def get(self, key: str, default: str = "") -> str:
        lowered = key.lower()
        for name, value in self.entries():
            if name.lower() == lowered:
                return value
        return default

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    def set_many(self, values: Mapping[str, str]) -> None:
        text = read_text(self.path)
        lines = text.splitlines()
        remaining = {k.lower(): (k, v) for k, v in values.items()}
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            match = KV_PATTERN.match(line)
            if not match:
                continue
            name = match.group(1).lower()
            if name in remaining:
                _, value = remaining.pop(name)
                if value is None or value == "":
                    lines[index] = f"{original}{self.separator}"
                else:
                    lines[index] = f"{original}{self.separator}{value}"
        for original, value in remaining.values():
            if value is None or value == "":
                continue
            lines.append(f"{original}{self.separator}{value}")
        new_text = "\n".join(lines).strip("\n") + "\n"
        ensure_dir(self.path.parent)
        atomic_write_text(self.path, new_text)

    def unset(self, key: str) -> bool:
        text = read_text(self.path)
        if not text:
            return False
        lowered = key.lower()
        lines = []
        removed = False
        for line in text.splitlines():
            match = KV_PATTERN.match(line)
            if match and match.group(1).lower() == lowered:
                removed = True
                continue
            lines.append(line)
        if removed:
            atomic_write_text(self.path, "\n".join(lines).strip("\n") + "\n")
        return removed


def ini_get(path: Path, section: str, key: str, default: str = "") -> str:
    """Minimal INI reader (pip.conf and friends); enough for one-section files."""
    text = read_text(path)
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().lower()
            continue
        if current != section.lower():
            continue
        match = KV_PATTERN.match(line)
        if match and match.group(1).lower() == key.lower():
            return match.group(2)
    return default


def ini_set(path: Path, section: str, values: Mapping[str, str]) -> None:
    """Set keys inside ``[section]`` of an INI file, preserving everything else."""
    text = read_text(path)
    lines = text.splitlines()
    header = f"[{section}]"
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == header.lower():
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    remaining = {k.lower(): (k, v) for k, v in values.items()}
    if start is None:
        lines.append(header)
        start = len(lines) - 1
        end = len(lines)
    for index in range(start + 1, end):
        match = KV_PATTERN.match(lines[index])
        if not match:
            continue
        name = match.group(1).lower()
        if name in remaining:
            original, value = remaining.pop(name)
            lines[index] = f"{original} = {value}"
    insert_at = end
    for original, value in remaining.values():
        lines.insert(insert_at, f"{original} = {value}")
        insert_at += 1
    ensure_dir(path.parent)
    atomic_write_text(path, "\n".join(lines).strip("\n") + "\n")


def ini_unset(path: Path, section: str, keys: Sequence[str]) -> int:
    text = read_text(path)
    if not text:
        return 0
    lowered = {k.lower() for k in keys}
    current = ""
    removed = 0
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().lower()
        if current == section.lower():
            match = KV_PATTERN.match(line)
            if match and match.group(1).lower() in lowered:
                removed += 1
                continue
        out.append(line)
    if removed:
        atomic_write_text(path, "\n".join(out).strip("\n") + "\n")
    return removed


# ---------------------------------------------------------------------------
# XML (Maven settings.xml)
# ---------------------------------------------------------------------------
def replace_xml_element(text: str, wrapper: str, inner: str) -> str:
    """Replace the first ``<wrapper>...</wrapper>`` (or append it if absent)."""
    pattern = re.compile(rf"<{wrapper}\b.*?</{wrapper}>", re.DOTALL)
    new = f"<{wrapper}>{inner}</{wrapper}>"
    if pattern.search(text):
        return pattern.sub(new, text, count=1)
    close = re.search(rf"</{re.escape('mirrors')}>", text)
    if wrapper == "mirror" and close:
        return text[: close.start()] + new + text[close.start():]
    return text


def xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# SingleValueManager
# ---------------------------------------------------------------------------
class SingleValueManager(ManagerPlugin):
    """Base class for ecosystems whose current source is a single setting.

    Subclasses provide :meth:`describe_current` and the read/write plumbing
    (:meth:`read_value` / :meth:`write_value`); everything else - listing enabled
    sources, switching, testing, backups - is handled here.
    """

    #: key used by the manager, e.g. ``registry`` or ``index-url``
    value_key: str = "source"
    #: synthetic name used when the user has no explicit configuration
    default_name: str = "default"
    #: the ecosystem default URL, used when nothing is configured
    default_url: str = ""
    #: extra keys written/removed together with the main one
    companion_keys: Mapping[str, str] = {}

    # -- to be implemented by subclasses ------------------------------------
    def read_value(self) -> str:
        raise NotImplementedError

    def write_value(self, value: str) -> None:
        raise NotImplementedError

    def clear_value(self) -> bool:
        raise NotImplementedError

    # -- overridable ---------------------------------------------------------
    def describe_current(self, value: str) -> Source:
        """Build the :class:`Source` representing the active value."""
        kind = SourceKind.MIRROR if value != self.default_url else SourceKind.OFFICIAL
        name = self._name_for(value)
        return make_source(
            self.name,
            name=name,
            url=value,
            kind=kind,
            scope=self.manifest.scope,
            enabled=True,
            is_default=True,
            health={"url": value},
            extra={"value": value},
        )

    def _name_for(self, value: str) -> str:
        if value == self.default_url:
            return self.default_name
        provider = self.registry_lookup(value)
        if provider:
            return provider.name
        stripped = re.sub(r"^https?://", "", value or "").rstrip("/")
        return stripped or self.default_name

    def registry_lookup(self, value: str) -> Source | None:
        """Look the value up in the registry to get a friendly name."""
        try:
            from .registry import SourceRegistry

            registry = getattr(self, "_registry", None)
            if registry is None:
                return None
            return registry.get(self.name, value) or next(
                (
                    s
                    for s in registry.sources_for(self.name)
                    if s.url.rstrip("/") == (value or "").rstrip("/")
                ),
                None,
            )
        except Exception:  # noqa: BLE001 - naming is cosmetic
            return None

    # -- ManagerPlugin -------------------------------------------------------
    def detect(self) -> bool:
        if self.manifest.requires and not any(which(cmd) for cmd in self.manifest.requires):
            return False
        if not self.manifest.requires:
            return True
        return any(which(cmd) for cmd in self.manifest.requires)

    def list_enabled(self) -> list[Source]:
        value = self.read_value()
        if not value:
            if not self.default_url:
                return []
            return [self.describe_current(self.default_url)]
        return [self.describe_current(value)]

    def list_available(self, manager: str | None = None) -> list[Source]:
        return list(self._registry_sources())

    def _registry_sources(self) -> list[Source]:
        registry = getattr(self, "_registry", None)
        if registry is None:
            return []
        return registry.sources_for(self.name)

    def current(self) -> Source | None:
        items = self.list_enabled()
        return items[0] if items else None

    def add(self, source: Source) -> OperationResult:
        value = self._value_of(source)
        if not value:
            return fail(f"{self.name}: source has no URL to use as {self.value_key}")
        previous = self.read_value()
        if previous == value:
            return ok(f"{self.name}: already using {source.name}")
        self.write_value(value)
        return ok(
            f"{self.name}: {self.value_key} -> {value}",
            changed=[value],
            previous=previous,
        )

    def remove(self, source: Source) -> OperationResult:
        value = self._value_of(source)
        current = self.read_value()
        if value and current and value.rstrip("/") != current.rstrip("/"):
            return fail(f"{self.name}: {source.name} is not the active {self.value_key}")
        if self.clear_value():
            return ok(f"{self.name}: cleared {self.value_key}", changed=[value])
        return ok(f"{self.name}: nothing to remove")

    def enable(self, source: Source) -> OperationResult:
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        value = self._value_of(source)
        current = self.read_value()
        if current and value and current.rstrip("/") != value.rstrip("/"):
            return fail(f"{self.name}: {source.name} is not active")
        if not self.default_url:
            return fail(
                f"{self.name}: cannot disable the only {self.value_key}",
                hint="add another source instead",
            )
        self.write_value(self.default_url)
        return ok(f"{self.name}: {self.value_key} -> {self.default_url} (ecosystem default)")

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        new_value = str(changes.get("url") or changes.get("value") or "")
        if not new_value:
            return fail(f"{self.name}: only 'url' can be edited for this manager")
        self.write_value(new_value)
        return ok(f"{self.name}: {self.value_key} -> {new_value}")

    def set_current(self, source: Source) -> OperationResult:
        return self.add(source)

    def _value_of(self, source: Source) -> str:
        return str(source.extra.get("value") or source.url or "")

    # -- backups -------------------------------------------------------------
    def config_files(self) -> list[Path]:
        return [Path(p).expanduser() for p in self.manifest.config_paths]


__all__ = [
    "ENV_GUARD",
    "KeyValueFile",
    "MARKER_BEGIN",
    "MARKER_END",
    "SingleValueManager",
    "expand_template",
    "fail",
    "find_block",
    "get_marked_block",
    "home",
    "ini_get",
    "ini_set",
    "ini_unset",
    "make_source",
    "ok",
    "read_text",
    "reads_only",
    "remove_marked_block",
    "replace_xml_element",
    "run",
    "upsert_marked_block",
    "which",
    "xdg_config_home",
    "xdg_data_home",
    "xml_escape",
]
