"""Conda (``channels:``) manager plugin.

Conda resolves packages from the **ordered** ``channels:`` list in ``.condarc``: the
first entry wins, the rest are fallbacks, and ``channel_alias`` turns a short channel
name into a URL (``conda-forge`` -> ``https://conda.anaconda.org/conda-forge``).  So
the first channel is "the current source" and every entry is modelled as a source.

Reading and writing
-------------------
* The document is **read** with :func:`yaml.safe_load`, because ``.condarc`` is YAML
  with nesting (``custom_channels`` is a mapping) and a partial hand-rolled parser
  would mis-read it.
* It is **written textually**: only the ``channels:`` block and the ``channel_alias:``
  scalar are rewritten, so comments, quoting style and unrelated keys
  (``ssl_verify``, ``custom_channels``, ``default_channels``, ``pkgs_dirs``, ...)
  survive byte-for-byte.  Dumping the parsed document with ``yaml.safe_dump`` would
  have been simpler but would reflow the whole file and delete comments.

Configuration files: ``$SRCMGR_CONDA_RC`` wins, then ``~/.condarc``.  An environment's
``$CONDA_PREFIX/.condarc`` is read as a *fallback* (and reported by
:meth:`CondaManager.detect_detail`); it is never written, because it belongs to the
environment, not to the user.

``native_test_command()`` is the cheap ``conda config --show channels``: it validates
the configuration only (no network).  ``test()`` probes ``repodata.json`` over HTTP.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from srcmgr.core import helpers
from srcmgr.core.api import ManagerPlugin, OperationResult, Source, SourceKind, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.health import probe_url
from srcmgr.core.helpers import fail, make_source, ok, read_text
from srcmgr.core.paths import atomic_write_text, ensure_dir

MANAGER = "conda"

#: What ``defaults`` expands to first.
DEFAULT_URL = "https://repo.anaconda.com/pkgs/main"
DEFAULT_NAME = "defaults"
#: The community channel conda users switch to most often.
CONDA_FORGE_NAME = "conda-forge"
CONDA_FORGE_URL = "https://conda.anaconda.org/conda-forge"
#: Where short channel names are resolved.
DEFAULT_ALIAS = "https://conda.anaconda.org"
CONFIG_OVERRIDE = "SRCMGR_CONDA_RC"
#: Channel index file.
REPODATA = "repodata.json"
#: 403/404 mean "a server answered", which is all a reachability check needs.
REACHABLE_HTTP = {200, 403, 404}

#: conda platform sub-directory for the local machine.
_ARCH_SUBDIRS = {
    "x86_64": "linux-64",
    "amd64": "linux-64",
    "aarch64": "linux-aarch64",
    "arm64": "linux-aarch64",
    "ppc64le": "linux-ppc64le",
    "s390x": "linux-s390x",
    "i686": "linux-32",
    "i386": "linux-32",
    "armv7l": "linux-armv7l",
}

_LIST_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<rest>.*)$")
_ITEM = re.compile(r"^(?P<indent>[ \t]*)-(?P<item>.*)$")


@dataclass
class Block:
    """Location of a top-level ``key:`` and its block sequence."""

    key_index: int
    key: str
    indent: str
    item_indent: str = ""
    item_indexes: list[int] = field(default_factory=list)
    inline: str = ""
    end: int = 0


def find_block(lines: Sequence[str], key: str) -> Block | None:
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _LIST_KEY.match(line)
        if not match or match.group("key") != key:
            continue
        block = Block(
            key_index=index,
            key=key,
            indent=match.group("indent"),
            inline=match.group("rest").strip(),
            end=index + 1,
        )
        if block.inline:
            return block
        for offset in range(index + 1, len(lines)):
            raw = lines[offset]
            if not raw.strip():
                continue
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


def _scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_list(text: str, key: str) -> list[str]:
    """Read a top-level block/flow list such as ``channels:``."""
    lines = (text or "").splitlines()
    block = find_block(lines, key)
    if block is None:
        return []
    if block.inline:
        inline = block.inline
        if inline.startswith("["):
            inner = inline.strip().strip("[]")
            return [item for item in (_scalar(part) for part in inner.split(",")) if item]
        return [value for value in [_scalar(inline)] if value]
    return [
        value
        for value in (_scalar(_ITEM.match(lines[i]).group("item")) for i in block.item_indexes)
        if value
    ]


def write_list(text: str, key: str, values: Sequence[str]) -> str:
    """Rewrite one top-level list key, leaving every other line untouched."""
    lines = (text or "").splitlines()
    block = find_block(lines, key)
    if block is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f"{key}:")
        lines.extend(f"  - {value}" for value in values)
        return "\n".join(lines) + "\n"
    if block.inline.startswith("["):
        head = f"{block.indent}{key}: [{', '.join(values)}]"
        new_lines = [*lines[: block.key_index], head, *lines[block.end :]]
    else:
        item_indent = block.item_indent
        if not item_indent and block.item_indexes:
            item_indent = _ITEM.match(lines[block.item_indexes[0]]).group("indent")
        rendered = [f"{item_indent}- {value}" for value in values]
        new_lines = [*lines[: block.key_index + 1], *rendered, *lines[block.end :]]
    return "\n".join(new_lines) + "\n"


def write_scalar(text: str, key: str, value: str) -> str:
    """Set one top-level ``key: value`` pair, replacing it in place when present."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _LIST_KEY.match(line)
        if match and match.group("key") == key:
            lines[index] = f"{match.group('indent')}{key}: {value}"
            return "\n".join(lines) + "\n"
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def parse_scalar(text: str, key: str) -> str:
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LIST_KEY.match(line)
        if match and match.group("key") == key:
            return _scalar(match.group("rest"))
    return ""


def _platform_subdir() -> str:
    machine = platform.machine().lower()
    return _ARCH_SUBDIRS.get(machine, f"linux-{machine}" if machine else "linux-64")


class CondaManager(ManagerPlugin):
    """Manages the ordered ``channels:`` list conda resolves against."""

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
    def config_path(self) -> Path:
        override = os.environ.get(CONFIG_OVERRIDE)
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".condarc"

    def env_config_path(self) -> Path | None:
        """``$CONDA_PREFIX/.condarc`` - the active environment's own file."""
        prefix = os.environ.get("CONDA_PREFIX")
        if not prefix:
            return None
        return Path(prefix).expanduser() / ".condarc"

    def config_files(self) -> list[Path]:
        files = [self.config_path()]
        env_path = self.env_config_path()
        if env_path is not None and env_path.exists():
            files.append(env_path)
        return files

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        return helpers.which("conda") is not None or self.config_path().exists()

    def detect_detail(self) -> dict[str, Any]:
        env_path = self.env_config_path()
        detail: dict[str, Any] = {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": helpers.which("conda") or "",
            "config": str(self.config_path()),
            "config_exists": self.config_path().exists(),
            "channels": self.channels(),
            "channel_alias": self.channel_alias(),
            "default_channels": self.extra_keys().get("default_channels", []),
            "custom_channels": self.extra_keys().get("custom_channels", {}),
            "ssl_verify": self.extra_keys().get("ssl_verify"),
            "platform_subdir": _platform_subdir(),
        }
        if env_path is not None:
            detail["env_config"] = str(env_path)
            detail["env_config_exists"] = env_path.exists()
        return detail

    # ------------------------------------------------------------------ read
    def _document(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load(read_text(self.config_path()))
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    def extra_keys(self) -> dict[str, Any]:
        """The unrelated keys this plugin must never lose (used by detect_detail)."""
        document = self._document()
        return {
            key: value
            for key, value in document.items()
            if key not in ("channels", "channel_alias")
        }

    def channels(self) -> list[str]:
        """Ordered channel names.

        An explicit ``channels:`` key (even ``channels: []``) is authoritative; when
        the user file does not define one, ``$CONDA_PREFIX/.condarc`` is consulted and
        finally conda's implicit ``defaults``.
        """
        text = read_text(self.config_path())
        if find_block(text.splitlines(), "channels") is not None:
            return parse_list(text, "channels")
        env_path = self.env_config_path()
        if env_path is not None and env_path.exists():
            values = parse_list(read_text(env_path), "channels")
            if values:
                return values
        return [DEFAULT_NAME]

    def channel_alias(self) -> str:
        alias = parse_scalar(read_text(self.config_path()), "channel_alias")
        if alias:
            return alias
        document = self._document()
        value = document.get("channel_alias")
        return str(value) if value else DEFAULT_ALIAS

    def channel_url(self, name: str) -> str:
        """Resolve a channel entry to a URL (full URLs pass through)."""
        text = (name or "").strip()
        if text.startswith(("http://", "https://", "file://")):
            return text
        if text == DEFAULT_NAME:
            return self.default_url
        return f"{self.channel_alias().rstrip('/')}/{text}"

    def _source_for(self, name: str, position: int) -> Source:
        url = self.channel_url(name)
        return make_source(
            MANAGER,
            name=name,
            url=url,
            kind=SourceKind.OFFICIAL if url.rstrip("/") == self.default_url.rstrip("/") else SourceKind.MIRROR,
            scope=self.manifest.scope,
            enabled=True,
            is_default=position == 0,
            file=str(self.config_path()),
            provider_key=name,
            description=(
                "highest priority channel" if position == 0 else f"fallback channel #{position + 1}"
            ),
            health={"url": _probe_target(url)},
            extra={
                "value": url,
                "key": "channels",
                "channel": name,
                "position": position,
                "channel_alias": self.channel_alias(),
            },
        )

    def list_enabled(self) -> list[Source]:
        return [self._source_for(name, index) for index, name in enumerate(self.channels())]

    def list_available(self, manager: str | None = None) -> list[Source]:
        sources = list(self._registry_sources())
        known = {source.name for source in sources} | {source.url.rstrip("/") for source in sources}
        for name, url, kind, description in (
            (CONDA_FORGE_NAME, CONDA_FORGE_URL, SourceKind.MIRROR, "Community-maintained conda channel"),
            (DEFAULT_NAME, DEFAULT_URL, SourceKind.OFFICIAL, "Anaconda's defaults channel (pkgs/main)"),
        ):
            if name in known or url.rstrip("/") in known:
                continue
            sources.append(
                make_source(
                    MANAGER,
                    name=name,
                    url=url,
                    kind=kind,
                    scope=self.manifest.scope,
                    enabled=False,
                    provider_key=name,
                    description=description,
                    health={"url": _probe_target(url)},
                    extra={
                        "value": url,
                        "key": "channels",
                        "channel": name,
                        "channel_alias": self.channel_alias(),
                    },
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
    def _write_channels(self, channels: Sequence[str]) -> None:
        path = self.config_path()
        ensure_dir(path.parent)
        atomic_write_text(path, write_list(read_text(path), "channels", channels))

    def _channel_for_url(self, url: str) -> str:
        """Shortest channel entry conda accepts for ``url``."""
        text = (url or "").strip()
        if not text:
            return ""
        if text.rstrip("/") == self.default_url.rstrip("/"):
            return DEFAULT_NAME
        alias = self.channel_alias().rstrip("/")
        if alias and text.rstrip("/").startswith(alias + "/"):
            return text.rstrip("/")[len(alias) + 1 :]
        return text

    def _channel_of(self, source: Source) -> str:
        """How ``source`` should be written into ``channels:``."""
        explicit = str(source.extra.get("channel") or "").strip()
        if explicit:
            return explicit
        return self._channel_for_url(source.url or "")

    def _locate(self, source: Source, current: Sequence[str]) -> str | None:
        """Find the ``channels:`` entry a source refers to.

        The recorded ``extra["channel"]`` wins, then an exact URL match, then the
        channel name derived from the URL.  Deriving first would be wrong when
        ``channel_alias`` changed after the entry was written.
        """
        explicit = str(source.extra.get("channel") or "").strip()
        if explicit and explicit in current:
            return explicit
        url = (source.url or "").strip().rstrip("/")
        if url:
            for entry in current:
                if self.channel_url(entry).rstrip("/") == url:
                    return entry
            derived = self._channel_for_url(url)
            if derived in current:
                return derived
        return explicit or None

    def add(self, source: Source) -> OperationResult:
        channel = self._channel_of(source)
        if not channel:
            return fail("conda: source has no channel name or URL")
        current = self.channels()
        if channel in current:
            return ok(f"conda: {channel} is already a channel")
        highest = str(source.extra.get("priority") or "").lower() == "highest"
        updated = [channel, *current] if highest else [*current, channel]
        self._write_channels(updated)
        return ok(
            f"conda: added channel {channel}" + (" (highest priority)" if highest else ""),
            changed=[str(self.config_path())],
            position=0 if highest else len(current),
        )

    def remove(self, source: Source) -> OperationResult:
        current = self.channels()
        channel = self._locate(source, current)
        if channel is None or channel not in current:
            return fail(f"conda: {self._channel_of(source) or source.name} is not a configured channel")
        remaining = list(current)
        remaining.remove(channel)
        self._write_channels(remaining)
        return ok(
            f"conda: removed channel {channel}",
            changed=[str(self.config_path())],
            remaining=remaining,
        )

    def enable(self, source: Source) -> OperationResult:
        """Add the channel back (conda has no per-channel enabled flag)."""
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Remove the channel.

        Conda has no "disabled channel" state - a channel is either listed or it is
        not - so disabling removes the entry and stores nothing.
        """
        result = self.remove(source)
        if result.ok:
            result.message = result.message.replace("removed", "disabled (removed)")
        return result

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """Replace the channel in place and/or set ``channel_alias``."""
        messages: list[str] = []
        alias = changes.get("channel_alias")
        if alias:
            path = self.config_path()
            ensure_dir(path.parent)
            atomic_write_text(path, write_scalar(read_text(path), "channel_alias", str(alias)))
            messages.append(f"channel_alias -> {alias}")
        new_url = changes.get("url") or changes.get("value")
        if new_url:
            current = self.channels()
            old = self._locate(source, current)
            if old is None:
                return fail(f"conda: {self._channel_of(source) or source.name} is not a configured channel")
            new_channel = self._channel_for_url(str(new_url))
            updated = list(current)
            updated[updated.index(old)] = new_channel
            self._write_channels(updated)
            messages.append(f"{old} -> {new_channel}")
        if not messages:
            return fail("conda: nothing to update (use 'url' or 'channel_alias')")
        return ok(
            "conda: " + "; ".join(messages),
            changed=[str(self.config_path())],
        )

    # ---------------------------------------------------------------- health
    def native_test_command(self) -> Sequence[str] | None:
        # Cheap and config-only on purpose: it prints the effective channel list
        # without touching the network (a real `conda search` is very heavy).
        return ["conda", "config", "--show", "channels"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe ``<channel>/<arch>/repodata.json`` for a channel root."""
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
            result.error = "channel has no URL"
            return result
        target, note = _probe_target_for(url)
        if not target:
            result.status = "unsupported-url"
            result.method = "none"
            result.error = note
            result.detail = note
            return result
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
                f"probed {target}: HTTP 404 - the host answered but has no repodata.json "
                "there (wrong platform subdir or not a channel root)"
            )
        elif probe.http_status == 403:
            result.detail = f"probed {target}: HTTP 403 (host answered; access filter)"
        else:
            result.detail = f"probed {target}" + (f" ({note})" if note else "")
        return result


def _probe_target(url: str) -> str:
    return _probe_target_for(url)[0]


def _probe_target_for(url: str) -> tuple[str, str]:
    """``(probe_url, note)`` for a channel URL; ``("", reason)`` when unprobeable."""
    text = (url or "").strip()
    if not text:
        return "", "channel has no URL"
    if not text.startswith(("http://", "https://")):
        return "", f"channel {text!r} is not an HTTP(S) URL"
    if text.endswith(".json"):
        return text, "repodata URL"
    last = text.rstrip("/").rsplit("/", 1)[-1]
    if last in set(_ARCH_SUBDIRS.values()) | {"noarch"}:
        return f"{text.rstrip('/')}/{REPODATA}", "platform subdir"
    return f"{text.rstrip('/')}/{_platform_subdir()}/{REPODATA}", "channel root"


__all__ = ["CondaManager", "find_block", "parse_list", "parse_scalar", "write_list", "write_scalar"]
