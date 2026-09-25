"""Go module proxy (``GOPROXY``) manager plugin.

Where the value lives
---------------------
``go env -w GOPROXY=...`` writes one ``KEY=value`` line into the *Go environment
file* (``go env GOENV``, by default ``$XDG_CONFIG_HOME/go/env``).  Writing that line
directly is exactly equivalent to running ``go env -w`` - the command only validates
the value and appends/replaces the line - so this plugin edits the file instead of
shelling out to ``go`` (faster, works when the toolchain is broken, and keeps the
change atomic).  The resolution order is:

1. ``$SRCMGR_GO_ENV_FILE`` (explicit override, used by tests)
2. ``$(go env GOENV)``
3. ``$XDG_CONFIG_HOME/go/env`` (default ``~/.config/go/env``)
4. ``$HOME/.config/go/env``

If ``go`` is missing, :meth:`GoManager.detect_detail` says so; reading and writing the
file still works, which is the point.

GOPROXY is a *fallback list*
---------------------------
Elements are separated by ``,`` (try the next proxy on 404/410) or ``|`` (stop at the
first error).  Each element is modelled as its own :class:`Source` with
``extra["position"]``/``extra["separator"]``; order is therefore meaningful and this
plugin never reorders elements the user did not touch.  ``direct`` and ``off`` are
special values: they carry no URL, are not probeable
(``health={"mode": "none"}``) and are reported as skipped by :meth:`test`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from srcmgr.core import helpers
from srcmgr.core.api import OperationResult, Source, SourceKind, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.health import probe_url
from srcmgr.core.helpers import SingleValueManager, fail, make_source, ok, read_text
from srcmgr.core.paths import atomic_write_text, ensure_dir

MANAGER = "go"

#: What the Go toolchain uses when GOPROXY is unset.
DEFAULT_GOPROXY = "https://proxy.golang.org,direct"
#: Env variable that overrides the environment file location (tests/dev).
ENV_FILE_OVERRIDE = "SRCMGR_GO_ENV_FILE"
#: A tiny always-available module used to ask a proxy "are you there?".
PROBE_MODULE = "github.com/pkg/errors"
#: 404/410 means the proxy answered but has no such module: the host is reachable.
REACHABLE_HTTP = {200, 404, 410}
#: Values that are not proxies at all.
SPECIAL_VALUES = {"direct", "off"}
DEFAULT_PROXY_HOST = "https://proxy.golang.org"


@dataclass(frozen=True)
class ProxyEntry:
    """One element of a GOPROXY list."""

    value: str
    #: Separator that *precedes* this element (``,``/``|``); empty for the first.
    separator: str = ""
    position: int = 0

    @property
    def is_special(self) -> bool:
        return self.value in SPECIAL_VALUES


def split_chain(value: str) -> list[ProxyEntry]:
    """Split a GOPROXY value into its elements, remembering the separators."""
    entries: list[ProxyEntry] = []
    current: list[str] = []
    separator = ""
    for char in value or "":
        if char in ",|":
            text = "".join(current).strip()
            if text:
                entries.append(ProxyEntry(text, separator, len(entries)))
            separator = char
            current = []
            continue
        current.append(char)
    text = "".join(current).strip()
    if text:
        entries.append(ProxyEntry(text, separator, len(entries)))
    return entries


def join_chain(entries: Sequence[ProxyEntry]) -> str:
    """Rebuild a GOPROXY value; the first element's separator is not emitted."""
    out = ""
    for index, entry in enumerate(entries):
        if index == 0:
            out = entry.value
            continue
        out += (entry.separator or ",") + entry.value
    return out


# ---------------------------------------------------------------------------
# `go env` file line editor (see the note in GoManager.read_value)
# ---------------------------------------------------------------------------
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$")


def _unquote_env(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _env_get(text: str, key: str) -> str:
    """Value of ``key`` in a ``go env`` file ("" when absent)."""
    found = ""
    for line in (text or "").splitlines():
        match = ENV_LINE_RE.match(line)
        if match and match.group("key") == key:
            found = _unquote_env(match.group("value"))
    return found


def _env_set(text: str, key: str, value: str) -> str:
    """Replace (or append) the ``KEY=value`` line, preserving every other line."""
    lines = (text or "").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        match = ENV_LINE_RE.match(line)
        if match and match.group("key") == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue  # collapse duplicates of the managed key
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _env_unset(text: str, key: str) -> str:
    """Delete every ``KEY=`` line (``go env -u``)."""
    lines = [
        line
        for line in (text or "").splitlines()
        if not (ENV_LINE_RE.match(line) and ENV_LINE_RE.match(line).group("key") == key)
    ]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


class GoManager(SingleValueManager):
    """Manages the GOPROXY fallback list in Go's environment file."""

    value_key = "GOPROXY"
    default_name = "proxy.golang.org"
    default_url = DEFAULT_GOPROXY
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
    def env_file(self) -> Path:
        """Resolve the Go environment file at call time (never cached)."""
        override = os.environ.get(ENV_FILE_OVERRIDE)
        if override:
            return Path(override).expanduser()
        go = helpers.which("go")
        if go:
            try:
                completed = helpers.run([go, "env", "GOENV"], timeout=10.0)
            except Exception:  # noqa: BLE001 - a broken go must not break detection
                completed = None
            if completed is not None and completed.returncode == 0:
                text = (completed.stdout or "").strip()
                if text and text.lower() != "off":
                    return Path(text).expanduser()
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else helpers.home() / ".config"
        return base / "go" / "env"

    def config_files(self) -> list[Path]:
        return [self.env_file()]

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        return helpers.which("go") is not None or self.env_file().exists()

    def detect_detail(self) -> dict[str, Any]:
        binary = helpers.which("go")
        path = self.env_file()
        detail: dict[str, Any] = {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": binary or "",
            "env_file": str(path),
            "env_file_exists": path.exists(),
            "goproxy": self.read_value(),
        }
        if binary is None:
            detail["note"] = (
                "the 'go' binary is not installed; GOPROXY is still read/written "
                "directly in the environment file (equivalent to 'go env -w')"
            )
        return detail

    # ---------------------------------------------------------- value access
    #
    # The environment file is edited line by line: `go env -w KEY=value` replaces the
    # existing line (or appends one) and `go env -u KEY` deletes it, so writing the
    # line directly is equivalent.  Only the managed key is touched; every other line
    # is copied through byte-for-byte.  Duplicate lines of the *managed* key are
    # collapsed, because Go applies the file in order and the last duplicate would
    # otherwise win over the value we just wrote.
    def read_value(self) -> str:
        return _env_get(read_text(self.env_file()), self.value_key)

    def write_value(self, value: str) -> None:
        """Write the ``GOPROXY=`` line (what ``go env -w GOPROXY=`` does)."""
        path = self.env_file()
        new_text = _env_set(read_text(path), self.value_key, value)
        ensure_dir(path.parent)
        atomic_write_text(path, new_text)

    def clear_value(self) -> bool:
        """Remove the line, i.e. ``go env -u GOPROXY``."""
        path = self.env_file()
        text = read_text(path)
        new_text = _env_unset(text, self.value_key)
        if new_text == text:
            return False
        atomic_write_text(path, new_text)
        return True

    # ------------------------------------------------------------- listings
    def _entries(self) -> list[ProxyEntry]:
        value = self.read_value()
        if value:
            return split_chain(value)
        return split_chain(self.default_url)

    def _source_for(self, entry: ProxyEntry) -> Source:
        special = entry.is_special
        kind = SourceKind.UNKNOWN
        if not special:
            kind = (
                SourceKind.OFFICIAL
                if entry.value == DEFAULT_PROXY_HOST
                else SourceKind.MIRROR
            )
        health: dict[str, Any] = (
            {"mode": "none"} if special else {"mode": "http", "url": _probe_url(entry.value)}
        )
        return make_source(
            MANAGER,
            name=entry.value,
            url=entry.value,
            kind=kind,
            scope=self.manifest.scope,
            enabled=True,
            is_default=entry.position == 0,
            file=str(self.env_file()),
            health=health,
            extra={
                "value": entry.value,
                "key": "GOPROXY",
                "position": entry.position,
                "separator": entry.separator,
                "special": special,
                "probeable": not special,
            },
        )

    def list_enabled(self) -> list[Source]:
        """Every element of the list, in resolution order."""
        return [self._source_for(entry) for entry in self._entries()]

    def list_available(self, manager: str | None = None) -> list[Source]:
        sources = list(self._registry_sources())
        if not sources:
            sources = [
                make_source(
                    MANAGER,
                    name="proxy.golang.org",
                    url=DEFAULT_PROXY_HOST,
                    kind=SourceKind.OFFICIAL,
                    scope=self.manifest.scope,
                    enabled=False,
                    provider_key="proxy.golang.org",
                    health={"url": _probe_url(DEFAULT_PROXY_HOST)},
                    extra={"value": DEFAULT_PROXY_HOST, "key": "GOPROXY"},
                ),
                make_source(
                    MANAGER,
                    name="direct",
                    url="direct",
                    kind=SourceKind.UNKNOWN,
                    scope=self.manifest.scope,
                    enabled=False,
                    provider_key="direct",
                    health={"mode": "none"},
                    description="Fetch modules directly from their version control system.",
                    extra={"value": "direct", "key": "GOPROXY", "special": True},
                ),
            ]
        return sources

    def current(self) -> Source | None:
        entries = self._entries()
        return self._source_for(entries[0]) if entries else None

    # ------------------------------------------------------------ mutations
    def _value_of(self, source: Source) -> str:
        return str(source.extra.get("value") or source.url or "").strip()

    def add(self, source: Source) -> OperationResult:
        """Point GOPROXY at ``source`` (``<url>,direct`` unless ``extra["no_direct"]``).

        An explicit ``off`` in the current list is preserved as the *last* element:
        the user asked for downloads to stay disabled and adding a proxy must not
        silently re-enable them.
        """
        value = self._value_of(source)
        if not value:
            return fail("go: source has no proxy URL")
        if value in SPECIAL_VALUES:
            return fail(f"go: {value!r} cannot be added as a proxy")
        parts = [value] if source.extra.get("no_direct") else [value, "direct"]
        current = self.read_value()
        if any(entry.value == "off" for entry in split_chain(current or "")):
            parts.append("off")
        new_value = ",".join(parts)
        if new_value == current:
            return ok(f"go: GOPROXY already {new_value}")
        self.write_value(new_value)
        return ok(
            f"go: GOPROXY -> {new_value}",
            changed=[str(self.env_file())],
            previous=current,
        )

    def enable(self, source: Source) -> OperationResult:
        """Insert one element back into the list at its remembered position."""
        value = self._value_of(source)
        if not value:
            return fail("go: source has no proxy URL")
        entries = self._entries()
        if any(entry.value == value for entry in entries):
            return ok(f"go: {value} is already in GOPROXY")
        position = source.extra.get("position")
        index = len(entries)
        if isinstance(position, int) and 0 <= position <= len(entries):
            index = position
        separator = str(source.extra.get("separator") or "")
        if not separator:
            separator = entries[index - 1].separator if index > 0 else ","
        if not separator:
            separator = ","
        entries.insert(index, ProxyEntry(value, separator, index))
        for new_position, entry in enumerate(entries):
            entries[new_position] = ProxyEntry(entry.value, entry.separator, new_position)
        self.write_value(join_chain(entries))
        return ok(
            f"go: GOPROXY -> {join_chain(entries)}",
            changed=[str(self.env_file())],
            position=index,
        )

    def disable(self, source: Source) -> OperationResult:
        """Remove one element from the list.

        GOPROXY has no per-element "disabled" flag - an element is either in the
        fallback list or it is not - so disabling means removing it.  The plugin
        keeps nothing on disk; ``enable()`` re-adds it from the source object (a
        provider entry or ``extra``) when the user wants it back.
        """
        value = self._value_of(source)
        entries = self._entries()
        kept = [entry for entry in entries if entry.value != value]
        if len(kept) == len(entries):
            return fail(f"go: {value or source.name} is not in GOPROXY")
        for position, entry in enumerate(kept):
            kept[position] = ProxyEntry(entry.value, entry.separator, position)
        self.write_value(join_chain(kept))
        return ok(
            f"go: removed {value} from GOPROXY",
            changed=[str(self.env_file())],
            remaining=[entry.value for entry in kept],
        )

    def remove(self, source: Source) -> OperationResult:
        """Take exactly one element out of the list."""
        value = self._value_of(source)
        result = self.disable(source)
        if not result.ok:
            return result
        return ok(
            f"go: {value} removed from GOPROXY",
            changed=[str(self.env_file())],
            remaining=result.details.get("remaining", []),
        )

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """Replace one element in place, keeping its position and separator."""
        new_value = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_value:
            return fail("go: only 'url' can be updated for a GOPROXY element")
        old_value = self._value_of(source)
        entries = self._entries()
        for index, entry in enumerate(entries):
            if entry.value == old_value:
                entries[index] = ProxyEntry(new_value, entry.separator, index)
                self.write_value(join_chain(entries))
                return ok(
                    f"go: GOPROXY[{index}] {old_value} -> {new_value}",
                    changed=[str(self.env_file())],
                )
        return fail(f"go: {old_value or source.name} is not in GOPROXY")

    # ---------------------------------------------------------------- health
    def native_test_command(self) -> Sequence[str] | None:
        return ["go", "list", "-m", "-versions", PROBE_MODULE]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Ask a proxy for one module's version list and interpret the answer."""
        value = self._value_of(source)
        result = TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=value,
            method="GET",
            checked_at=utc_now(),
        )
        if value in SPECIAL_VALUES:
            result.ok = True
            result.status = "skipped"
            result.method = "none"
            result.detail = (
                f"'{value}' is a GOPROXY keyword, not a proxy: nothing to probe "
                "(direct = fetch from VCS, off = downloads disabled)"
            )
            return result
        target = _probe_url(value)
        if not target:
            result.ok = False
            result.status = "unsupported-url"
            result.method = "none"
            result.error = f"GOPROXY element {value!r} is not a scheme://host URL"
            result.detail = result.error
            return result
        probe = probe_url(target, timeout=timeout)
        result.http_status = probe.http_status
        result.latency_ms = probe.latency_ms
        result.tls_ok = probe.tls_ok
        result.redirected_to = probe.redirected_to
        result.method = probe.method or "GET"
        if probe.http_status in REACHABLE_HTTP:
            result.ok = True
            result.status = "ok"
            if probe.http_status in (404, 410):
                result.detail = (
                    f"probed {target}: HTTP {probe.http_status} - the proxy answered but "
                    f"has no {PROBE_MODULE}@v/list (still reachable)"
                )
            else:
                result.detail = f"probed {target}"
        else:
            result.ok = False
            result.status = probe.status
            result.error = probe.error
            result.detail = f"probed {target}"
        return result


def _probe_url(value: str) -> str:
    """``<proxy>/<module>/@v/list`` for a proxy base URL ("" when not HTTP)."""
    parts = urlsplit((value or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{(value or '').strip().rstrip('/')}/{PROBE_MODULE}/@v/list"


__all__ = ["GoManager", "ProxyEntry", "join_chain", "split_chain"]
