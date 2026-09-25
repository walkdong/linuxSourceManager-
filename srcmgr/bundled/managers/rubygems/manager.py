"""RubyGems (``gem sources``) manager plugin.

RubyGems keeps an **ordered** list of sources in ``~/.gemrc``::

    ---
    :backtrace: false
    :verbose: true
    :sources:
    - https://rubygems.org/
    gem: --no-document

The *first* source in that list is the one ``gem install`` uses by default, the rest
are additional fallbacks - so "the current source" is simply the first entry.

Two ways to write it, both supported
------------------------------------
* ``gem sources --add/--remove`` is preferred when the ``gem`` binary exists, because
  RubyGems then owns its own file (locking, ordering, future format changes).
* Otherwise the file is edited directly.  The editor is *textual*: it locates the
  ``:sources:`` key and rewrites only its block sequence, leaving comments, quoting,
  key order and every unrelated key (``:verbose``, ``:backtrace``,
  ``gem: --no-document``, ...) byte-for-byte intact.  ``yaml.safe_dump`` was
  deliberately **not** used: it would reflow the whole document and drop comments.

The CLI is skipped when ``$SRCMGR_GEMRC`` is set or ``$GEMRC`` is in play, because
``gem`` would then write a *different* file than the one this plugin manages.

There is no "disabled source" state in RubyGems: a source is either listed or it is
not.  ``disable()`` therefore removes the entry and stores nothing; ``enable()`` adds
it back (sources produced by this plugin carry ``extra["position"]``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from srcmgr.core import helpers
from srcmgr.core.api import ManagerPlugin, OperationResult, Source, SourceKind, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.health import probe_url
from srcmgr.core.helpers import fail, make_source, ok, read_text
from srcmgr.core.paths import atomic_write_text, ensure_dir

MANAGER = "rubygems"

DEFAULT_URL = "https://rubygems.org"
DEFAULT_NAME = "rubygems.org"
CONFIG_OVERRIDE = "SRCMGR_GEMRC"
#: RubyGems' machine-readable index; its presence proves the source serves gems.
PROBE_PATH = "/specs.4.8.gz"
#: 403 (bot filter) and 404 (answered, path differs) still prove the host is alive.
REACHABLE_HTTP = {200, 403, 404}

_SOURCES_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>:?sources)\s*:\s*(?P<rest>.*)$")
_ITEM = re.compile(r"^(?P<indent>[ \t]*)-(?P<item>.*)$")


@dataclass
class SourcesBlock:
    """Location of the ``:sources:`` key and its block sequence."""

    key_index: int
    key: str
    key_indent: str
    item_indent: str = ""
    item_indexes: list[int] = field(default_factory=list)
    inline: str = ""
    end: int = 0


def _unquote_scalar(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def find_sources_block(lines: Sequence[str]) -> SourcesBlock | None:
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _SOURCES_KEY.match(line)
        if not match:
            continue
        block = SourcesBlock(
            key_index=index,
            key=match.group("key"),
            key_indent=match.group("indent"),
            inline=match.group("rest").strip(),
            end=index + 1,
        )
        if block.inline:
            return block
        for offset in range(index + 1, len(lines)):
            raw = lines[offset]
            if not raw.strip():
                continue  # blank lines between items are allowed in YAML
            item = _ITEM.match(raw)
            if item is None:
                break
            if not block.item_indexes:
                block.item_indent = item.group("indent")
            block.item_indexes.append(offset)
            block.end = offset + 1
        if not block.item_indexes:
            block.end = index + 1
        return block
    return None


def parse_sources(text: str) -> list[str]:
    """Read the ordered source list out of a ``.gemrc`` document."""
    lines = (text or "").splitlines()
    block = find_sources_block(lines)
    if block is None:
        return []
    if block.inline:
        inline = block.inline
        if inline.startswith("["):
            inner = inline.strip().strip("[]")
            return [item for item in (_unquote_scalar(part) for part in inner.split(",")) if item]
        return [value for value in [_unquote_scalar(inline)] if value]
    return [
        value
        for value in (_unquote_scalar(_ITEM.match(lines[index]).group("item")) for index in block.item_indexes)
        if value
    ]


def write_sources(text: str, sources: Sequence[str]) -> str:
    """Rewrite only the ``:sources:`` block; everything else is copied through."""
    lines = (text or "").splitlines()
    block = find_sources_block(lines)
    if block is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(":sources:")
        lines.extend(f"- {url}" for url in sources)
        return "\n".join(lines) + "\n"
    if block.inline.startswith("["):
        rendered = [f"{block.key_indent}{block.key}: [{', '.join(sources)}]"]
        new_lines = [*lines[: block.key_index], *rendered, *lines[block.end :]]
    else:
        item_indent = block.item_indent
        if not item_indent and block.item_indexes:
            item_indent = _ITEM.match(lines[block.item_indexes[0]]).group("indent")
        rendered = [f"{item_indent}- {url}" for url in sources]
        new_lines = [*lines[: block.key_index + 1], *rendered, *lines[block.end :]]
    return "\n".join(new_lines) + "\n"


class RubygemsManager(ManagerPlugin):
    """Manages the ordered ``:sources:`` list RubyGems reads."""

    default_url = DEFAULT_URL
    default_name = DEFAULT_NAME
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
    )

    # ------------------------------------------------------------------ paths
    def _candidates(self) -> list[Path]:
        override = os.environ.get(CONFIG_OVERRIDE)
        if override:
            return [Path(override).expanduser()]
        candidates: list[Path] = []
        gemrc = os.environ.get("GEMRC")
        if gemrc:
            candidates.extend(Path(entry).expanduser() for entry in gemrc.split(os.pathsep) if entry.strip())
        candidates.append(helpers.home() / ".gemrc")
        return candidates

    def config_path(self) -> Path:
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

    def _use_cli(self) -> bool:
        """``gem sources`` only when it edits the file this plugin resolves."""
        if os.environ.get(CONFIG_OVERRIDE) or os.environ.get("GEMRC"):
            return False
        if helpers.which("gem") is None:
            return False
        return self.config_path() == helpers.home() / ".gemrc"

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        return helpers.which("gem") is not None or any(p.exists() for p in self._candidates())

    def detect_detail(self) -> dict[str, Any]:
        return {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": helpers.which("gem") or "",
            "config": str(self.config_path()),
            "config_exists": self.config_path().exists(),
            "write_mode": "gem sources --add/--remove" if self._use_cli() else "direct .gemrc edit",
            "sources": self._sources(),
        }

    # ------------------------------------------------------------------ read
    def _sources(self) -> list[str]:
        """Ordered sources: local file first, then ``gem sources``, then the default."""
        values = parse_sources(read_text(self.config_path()))
        if values:
            return values
        from_cli = self._sources_from_cli()
        if from_cli:
            return from_cli
        return [self.default_url]

    def _sources_from_cli(self) -> list[str]:
        if helpers.which("gem") is None:
            return []
        try:
            completed = helpers.run(["gem", "sources"], timeout=20.0)
        except Exception:  # noqa: BLE001 - never fail a listing because of the CLI
            return []
        if completed.returncode != 0:
            return []
        out: list[str] = []
        for line in (completed.stdout or "").splitlines():
            text = line.strip()
            if text.startswith(("http://", "https://")):
                out.append(text)
        return out

    def _source_for(self, url: str, position: int) -> Source:
        return make_source(
            MANAGER,
            name=_host_of(url),
            url=url,
            kind=SourceKind.OFFICIAL if _same_url(url, self.default_url) else SourceKind.MIRROR,
            scope=self.manifest.scope,
            enabled=True,
            is_default=position == 0,
            file=str(self.config_path()),
            provider_key=_host_of(url),
            description="primary source" if position == 0 else f"fallback source #{position + 1}",
            health={"url": _probe_target(url)},
            extra={"value": url, "key": "sources", "position": position},
        )

    def list_enabled(self) -> list[Source]:
        """Every configured source, primary first."""
        return [self._source_for(url, index) for index, url in enumerate(self._sources())]

    def list_available(self, manager: str | None = None) -> list[Source]:
        sources = list(self._registry_sources())
        if not any(_same_url(source.url, DEFAULT_URL) for source in sources):
            sources.append(
                make_source(
                    MANAGER,
                    name=DEFAULT_NAME,
                    url=DEFAULT_URL,
                    kind=SourceKind.OFFICIAL,
                    scope=self.manifest.scope,
                    enabled=False,
                    provider_key=DEFAULT_NAME,
                    description="The canonical RubyGems source.",
                    health={"url": _probe_target(DEFAULT_URL)},
                    extra={"value": DEFAULT_URL, "key": "sources", "position": 0},
                )
            )
        return sources

    def _registry_sources(self) -> list[Source]:
        registry = getattr(self, "_registry", None)
        if registry is None:
            return []
        try:
            return list(registry.sources_for(self.name))
        except Exception:  # noqa: BLE001 - catalogue is optional
            return []

    def current(self) -> Source | None:
        sources = self.list_enabled()
        return sources[0] if sources else None

    # ------------------------------------------------------------ mutations
    def _write_sources(self, sources: Sequence[str]) -> None:
        if self._use_cli():
            # The CLI is incremental; callers use _cli_add/_cli_remove instead.
            return
        path = self.config_path()
        ensure_dir(path.parent)
        atomic_write_text(path, write_sources(read_text(path), sources))

    def _cli(self, *args: str) -> bool:
        try:
            completed = helpers.run(["gem", "sources", *args], timeout=60.0)
        except Exception:  # noqa: BLE001
            return False
        return completed.returncode == 0

    def add(self, source: Source) -> OperationResult:
        url = (source.url or "").strip()
        if not url:
            return fail("rubygems: source has no URL")
        current = self._sources()
        if any(_same_url(item, url) for item in current):
            return ok(f"rubygems: {url} is already a source")
        primary = bool(source.extra.get("primary"))
        if self._use_cli():
            if not self._cli("--add", url):
                return fail(f"rubygems: 'gem sources --add {url}' failed")
        else:
            updated = [url, *current] if primary else [*current, url]
            self._write_sources(updated)
        position = 0 if primary else len(current)
        return ok(
            f"rubygems: added {url}" + (" (primary)" if primary else ""),
            changed=[str(self.config_path())],
            position=position,
        )

    def remove(self, source: Source) -> OperationResult:
        url = (source.url or "").strip()
        current = self._sources()
        match = next((item for item in current if _same_url(item, url)), None)
        if match is None:
            return fail(f"rubygems: {url or source.name} is not a configured source")
        if self._use_cli():
            if not self._cli("--remove", match):
                return fail(f"rubygems: 'gem sources --remove {match}' failed")
        else:
            remaining = list(current)
            remaining.remove(match)
            self._write_sources(remaining)
        return ok(
            f"rubygems: removed {match}",
            changed=[str(self.config_path())],
            remaining=[item for item in current if item != match],
        )

    def enable(self, source: Source) -> OperationResult:
        """Add a source back (there is no separate enabled state)."""
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Remove exactly one entry.

        RubyGems has no "disabled source" concept: a source is either in ``:sources:``
        or it is not, so disabling removes the line and stores nothing.  ``enable()``
        adds it back from the source object.
        """
        result = self.remove(source)
        if result.ok:
            result.message = result.message.replace("removed", "disabled (removed)")
        return result

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        new_url = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_url:
            return fail("rubygems: only 'url' can be updated for a source")
        current = self._sources()
        old_url = (source.url or "").strip()
        position = next((i for i, item in enumerate(current) if _same_url(item, old_url)), None)
        if position is None:
            position = source.extra.get("position") if isinstance(source.extra.get("position"), int) else None
        if position is None or position >= len(current):
            return fail(f"rubygems: {old_url or source.name} is not a configured source")
        updated = list(current)
        updated[position] = new_url
        if self._use_cli():
            if not self._cli("--remove", current[position]) or not self._cli("--add", new_url):
                return fail(f"rubygems: could not replace {current[position]} with {new_url}")
        else:
            self._write_sources(updated)
        return ok(
            f"rubygems: {current[position]} -> {new_url} (position {position})",
            changed=[str(self.config_path())],
        )

    # ---------------------------------------------------------------- health
    def native_test_command(self) -> Sequence[str] | None:
        return ["gem", "search", "--remote", "rake"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe ``<source>/specs.4.8.gz`` (the compact index RubyGems fetches)."""
        url = (source.url or "").strip()
        result = TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=url,
            method="HEAD",
            checked_at=utc_now(),
        )
        if not url:
            result.status = "no-url"
            result.error = "source has no URL"
            return result
        target = _probe_target(url)
        probe = probe_url(target, timeout=timeout)
        result.http_status = probe.http_status
        result.latency_ms = probe.latency_ms
        result.tls_ok = probe.tls_ok
        result.redirected_to = probe.redirected_to
        result.method = probe.method or "HEAD"
        result.ok = probe.http_status in REACHABLE_HTTP
        result.status = "ok" if result.ok else probe.status
        result.error = "" if result.ok else probe.error
        if probe.http_status == 404:
            result.detail = (
                f"probed {target}: HTTP 404 - the host answered but does not serve "
                "specs.4.8.gz (source may be a private/partial index)"
            )
        elif probe.http_status == 403:
            result.detail = f"probed {target}: HTTP 403 (host answered; access filter)"
        else:
            result.detail = f"probed {target}"
        return result


def _same_url(left: str, right: str) -> bool:
    return (left or "").strip().rstrip("/") == (right or "").strip().rstrip("/")


def _probe_target(url: str) -> str:
    return f"{(url or '').strip().rstrip('/')}{PROBE_PATH}"


def _host_of(url: str) -> str:
    text = re.sub(r"^https?://", "", (url or "").strip()).rstrip("/")
    return text or DEFAULT_NAME


__all__ = ["RubygemsManager", "find_sources_block", "parse_sources", "write_sources"]
