"""Maven (``settings.xml`` mirrors) manager plugin.

Model
-----
A ``<mirror>`` is ``<id>`` + ``<name>`` + ``<url>`` + ``<mirrorOf>``.  ``<mirrorOf>``
says which repositories the mirror serves (``central``, ``*``, ``external:*``,
``repo,!other``); Maven uses the **first** mirror whose ``<mirrorOf>`` matches a
repository, and one mirror per id is meaningful, so each ``<mirror>`` is modelled as a
source and the first one covering ``central`` is marked ``is_default``.

Editing strategy
----------------
``settings.xml`` is hand-written and full of comments, so it is never re-serialized
through ElementTree.  This module locates ``<mirror>`` blocks with a regex, replaces
tags *inside* the block it owns (unknown tags such as ``<blocked>`` therefore survive)
and inserts new blocks just before ``</mirrors>``.  The standard skeleton is only
created when the file has to be written and does not exist yet
(:data:`SKELETON`), never on read.

The Maven installation's own ``conf/settings.xml`` is read-only and is only reported
by :meth:`MavenManager.detect_detail` - it is the distribution default, not something
a user manager should rewrite.

``native_test_command()`` is ``None`` on purpose: ``mvn -o help:evaluate
-Dexpression=maven.version`` is a *local* check (``-o`` = offline) that proves nothing
about a mirror, while a real online resolution boots the whole Maven runtime and is far
too heavy for a health check.  :meth:`MavenManager.test` probes the mirror's metadata
file over HTTP instead.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import unescape as xml_unescape

from srcmgr.core import helpers
from srcmgr.core.api import ManagerPlugin, OperationResult, Source, SourceKind, TestResult
from srcmgr.core.backup import utc_now
from srcmgr.core.health import probe_url
from srcmgr.core.helpers import fail, make_source, ok, read_text, xml_escape
from srcmgr.core.paths import atomic_write_text, ensure_dir

MANAGER = "maven"

#: Maven Central; the repository mirrors usually replace.
CENTRAL_URL = "https://repo.maven.apache.org/maven2"
CENTRAL_NAME = "Maven Central"
#: A small, stable artifact used to prove a mirror serves metadata.
PROBE_PATH = "/org/apache/maven/maven-core/maven-metadata.xml"
#: HTTP codes that prove the host answered (403 = bot filter, 404 = no such path).
REACHABLE_HTTP = {200, 403, 404}
SETTINGS_OVERRIDE = "SRCMGR_MAVEN_SETTINGS"

#: Written only when the user has no settings.xml yet.
SKELETON = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">
  <mirrors>
  </mirrors>
</settings>
"""

_MIRROR_RE = re.compile(r"(?P<open>[ \t]*)<mirror(\s[^>]*)?>(?P<body>.*?)</mirror\s*>", re.DOTALL)


@dataclass
class Mirror:
    """One ``<mirror>`` element."""

    id: str
    name: str
    url: str
    mirror_of: str
    start: int
    end: int
    raw: str
    indent: str
    file: str = ""


def _tag_value(body: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>", body, re.DOTALL)
    return xml_unescape(match.group(1).strip()) if match else ""


def _parse_mirrors(text: str, file: str = "") -> list[Mirror]:
    out: list[Mirror] = []
    for match in _MIRROR_RE.finditer(text or ""):
        body = match.group("body")
        out.append(
            Mirror(
                id=_tag_value(body, "id"),
                name=_tag_value(body, "name"),
                url=_tag_value(body, "url"),
                mirror_of=_tag_value(body, "mirrorOf") or "*",
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                indent=match.group("open") or "  ",
                file=file,
            )
        )
    return out


def covers_central(mirror_of: str) -> bool:
    """True when a ``<mirrorOf>`` expression serves the ``central`` repository.

    Handles the documented forms: ``*``, ``central``, ``external:*``,
    ``external:http:*`` and ``!``-exclusions (``*,!central`` excludes central).
    """
    covered = False
    excluded = False
    for token in (mirror_of or "").split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("!"):
            rest = token[1:].strip()
            if rest in ("central", "*"):
                excluded = True
            continue
        if token in ("central", "*") or token.startswith("external:"):
            covered = True
    return covered and not excluded


def _set_tag(block: str, tag: str, value: str) -> str:
    """Set ``<tag>value</tag>`` inside a mirror block, keeping its layout."""
    pattern = re.compile(rf"<{tag}(\s[^>]*)?>.*?</{tag}\s*>", re.DOTALL)
    if pattern.search(block):
        return pattern.sub(lambda _m: f"<{tag}>{xml_escape(value)}</{tag}>", block, count=1)
    if not value:
        return block
    closing = re.search(r"(?P<indent>[ \t]*)</mirror\s*>", block)
    close_indent = closing.group("indent") if closing else ""
    opening = re.match(r"(?P<indent>[ \t]*)<mirror", block)
    inner_indent = (opening.group("indent") if opening else close_indent) + "  "
    if "\n" not in block:  # a one-line <mirror>...</mirror> stays on one line
        return re.sub(
            r"</mirror\s*>",
            lambda _m: f"<{tag}>{xml_escape(value)}</{tag}></mirror>",
            block,
            count=1,
        )
    return re.sub(
        r"[ \t]*</mirror\s*>",
        lambda _m: f"{inner_indent}<{tag}>{xml_escape(value)}</{tag}>\n{close_indent}</mirror>",
        block,
        count=1,
    )


def _render_mirror(spec: "_MirrorSpec", indent: str) -> str:
    inner = indent + "  "
    lines = [
        f"{indent}<mirror>",
        f"{inner}<id>{xml_escape(spec.id)}</id>",
    ]
    if spec.name:
        lines.append(f"{inner}<name>{xml_escape(spec.name)}</name>")
    lines.append(f"{inner}<url>{xml_escape(spec.url)}</url>")
    lines.append(f"{inner}<mirrorOf>{xml_escape(spec.mirror_of)}</mirrorOf>")
    lines.append(f"{indent}</mirror>")
    return "\n".join(lines)


@dataclass
class _MirrorSpec:
    id: str
    name: str
    url: str
    mirror_of: str


def _mirrors_open(text: str) -> re.Match[str] | None:
    return re.search(r"<mirrors(\s[^>]*)?>", text)


def _mirrors_close(text: str) -> re.Match[str] | None:
    return re.search(r"</mirrors\s*>", text)


def _upsert_mirror(text: str, spec: _MirrorSpec) -> tuple[str, str]:
    """Insert or update ``spec`` inside ``<mirrors>``; returns ``(text, action)``."""
    for mirror in _parse_mirrors(text):
        if mirror.id == spec.id:
            block = mirror.raw
            block = _set_tag(block, "id", spec.id)
            block = _set_tag(block, "name", spec.name)
            block = _set_tag(block, "url", spec.url)
            block = _set_tag(block, "mirrorOf", spec.mirror_of)
            return text[: mirror.start] + block + text[mirror.end :], "updated"

    close = _mirrors_close(text)
    if close is None:
        # `<mirrors/>` is legal XML and easy to produce by hand.
        self_closing = re.search(r"<mirrors(\s[^>]*)?/>", text)
        if self_closing:
            indent = _line_indent(text, self_closing.start()) or "  "
            block = _render_mirror(spec, indent + "  ")
            replacement = f"<mirrors>\n{block}\n{indent}</mirrors>"
            return text[: self_closing.start()] + replacement + text[self_closing.end() :], "inserted"
        # No <mirrors> container at all: add one just before </settings>.
        settings_close = re.search(r"</settings\s*>", text)
        if settings_close is None:
            raise ValueError("settings.xml has no <settings> element to extend")
        block = _render_mirror(spec, "    ")
        replacement = f"  <mirrors>\n{block}\n  </mirrors>\n"
        return text[: settings_close.start()] + replacement + text[settings_close.start() :], "inserted"

    open_tag = _mirrors_open(text)
    open_indent = _line_indent(text, open_tag.start()) if open_tag is not None else ""
    prefix = text[: close.start()]
    suffix = text[close.start() :]
    line_start = prefix.rfind("\n") + 1
    leading = prefix[line_start:]
    if leading.strip() == "":
        # The closing tag sits alone on its line: keep that indentation for it.
        close_indent = leading
        prefix = prefix[:line_start]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
    else:
        # The closing tag shares a line (e.g. `<mirrors>`/`</mirrors>` inline).
        close_indent = open_indent
        prefix = prefix.rstrip() + "\n"
    block = _render_mirror(spec, open_indent + "  ")
    return f"{prefix}{block}\n{close_indent}{suffix}", "inserted"


def _line_indent(text: str, position: int) -> str:
    """Leading whitespace of the line containing ``position`` ("" if not alone)."""
    start = text.rfind("\n", 0, position) + 1
    leading = text[start:position]
    if leading.strip():
        return ""
    return leading


def _drop_mirror(text: str, mirror_id: str) -> tuple[str, bool]:
    """Delete the first ``<mirror>`` with ``mirror_id`` including its own line."""
    for mirror in _parse_mirrors(text):
        if mirror.id != mirror_id:
            continue
        start, end = mirror.start, mirror.end
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip() == "":
            start = line_start
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        elif text[end:line_end].strip() == "":
            line_end += 1
        else:
            line_end = end
        return text[:start] + text[line_end:], True
    return text, False


def _sanitize_id(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]+", "-", (text or "").strip()).strip("-.")
    return cleaned or "mirror"


class MavenManager(ManagerPlugin):
    """Manages ``<mirror>`` entries in the user's Maven ``settings.xml``."""

    default_url = CENTRAL_URL
    default_name = CENTRAL_NAME
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
    )

    # ------------------------------------------------------------------ paths
    def settings_path(self) -> Path:
        override = os.environ.get(SETTINGS_OVERRIDE)
        if override:
            return Path(override).expanduser()
        return helpers.home() / ".m2" / "settings.xml"

    def global_settings_path(self) -> Path | None:
        """The Maven installation's ``conf/settings.xml`` (read-only), if findable."""
        for variable in ("MAVEN_HOME", "M2_HOME"):
            root = os.environ.get(variable)
            if root:
                candidate = Path(root).expanduser() / "conf" / "settings.xml"
                if candidate.exists():
                    return candidate
        binary = helpers.which("mvn")
        if not binary:
            return None
        resolved = Path(os.path.realpath(binary))
        for parent in resolved.parents:
            candidate = parent / "conf" / "settings.xml"
            if candidate.exists():
                return candidate
        return None

    def config_files(self) -> list[Path]:
        return [self.settings_path()]

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        return helpers.which("mvn") is not None or self.settings_path().exists()

    def detect_detail(self) -> dict[str, Any]:
        global_settings = self.global_settings_path()
        detail: dict[str, Any] = {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": helpers.which("mvn") or "",
            "settings": str(self.settings_path()),
            "settings_exists": self.settings_path().exists(),
            "mirrors": [mirror.id for mirror in self._mirrors()],
        }
        if global_settings is not None:
            detail["global_settings"] = str(global_settings)
            detail["global_settings_note"] = (
                "the Maven installation's conf/settings.xml is read-only here: "
                "mirrors are managed in the user settings file"
            )
        else:
            detail["global_settings"] = ""
        return detail

    # ------------------------------------------------------------------ read
    def _mirrors(self) -> list[Mirror]:
        path = self.settings_path()
        seen: set[str] = set()
        out: list[Mirror] = []
        for mirror in _parse_mirrors(read_text(path), str(path)):
            if not mirror.id or mirror.id in seen:
                continue  # Maven uses the first mirror per id
            seen.add(mirror.id)
            out.append(mirror)
        return out

    def _source_for(self, mirror: Mirror, *, is_default: bool) -> Source:
        kind = SourceKind.OFFICIAL if mirror.url.rstrip("/") == CENTRAL_URL.rstrip("/") else SourceKind.MIRROR
        return make_source(
            MANAGER,
            name=mirror.name or mirror.id,
            url=mirror.url,
            kind=kind,
            scope=self.manifest.scope,
            enabled=True,
            is_default=is_default,
            file=mirror.file or str(self.settings_path()),
            provider_key=mirror.id,
            description=f"mirrorOf: {mirror.mirror_of}",
            health={"url": mirror.url},
            extra={
                "value": mirror.url,
                "key": "mirrors",
                "mirror_id": mirror.id,
                "mirror_of": mirror.mirror_of,
            },
        )

    def list_enabled(self) -> list[Source]:
        """Every configured mirror; the first one covering central is the default."""
        mirrors = self._mirrors()
        default_id = next((m.id for m in mirrors if covers_central(m.mirror_of)), "")
        return [self._source_for(mirror, is_default=mirror.id == default_id) for mirror in mirrors]

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Maven Central (plus anything a provider contributes)."""
        sources = list(self._registry_sources())
        if not any(source.url.rstrip("/") == CENTRAL_URL for source in sources):
            sources.append(
                make_source(
                    MANAGER,
                    name=CENTRAL_NAME,
                    url=CENTRAL_URL,
                    kind=SourceKind.OFFICIAL,
                    scope=self.manifest.scope,
                    enabled=False,
                    provider_key="central",
                    description="The default Maven Central repository.",
                    health={"url": CENTRAL_URL},
                    extra={"value": CENTRAL_URL, "key": "mirrors", "mirror_id": "central", "mirror_of": "central"},
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
        for source in self.list_enabled():
            if source.is_default:
                return source
        return None

    # ------------------------------------------------------------ mutations
    def _spec_from(self, source: Source) -> _MirrorSpec:
        mirror_id = str(source.extra.get("mirror_id") or source.provider_key or _sanitize_id(source.name))
        mirror_of = str(source.extra.get("mirror_of") or "*")
        return _MirrorSpec(
            id=_sanitize_id(mirror_id),
            name=source.name or mirror_id,
            url=source.url or "",
            mirror_of=mirror_of,
        )

    def _write(self, text: str) -> None:
        path = self.settings_path()
        ensure_dir(path.parent)
        atomic_write_text(path, text)

    def _document(self) -> str:
        text = read_text(self.settings_path())
        if not text.strip():
            return SKELETON
        return text

    def add(self, source: Source) -> OperationResult:
        spec = self._spec_from(source)
        if not spec.url:
            return fail("maven: mirror has no URL")
        existing = next((m for m in self._mirrors() if m.id == spec.id), None)
        if (
            existing is not None
            and existing.url == spec.url
            and existing.mirror_of == spec.mirror_of
            and (existing.name or spec.id) == spec.name
        ):
            return ok(f"maven: mirror {spec.id} already configured")
        try:
            new_text, action = _upsert_mirror(self._document(), spec)
        except ValueError as exc:
            return fail(f"maven: {exc}")
        self._write(new_text)
        return ok(
            f"maven: mirror {spec.id} {action} ({spec.url}, mirrorOf={spec.mirror_of})",
            changed=[str(self.settings_path())],
            mirror_id=spec.id,
        )

    def remove(self, source: Source) -> OperationResult:
        mirror_id = str(source.extra.get("mirror_id") or source.provider_key or "").strip()
        if not mirror_id:
            return fail("maven: cannot determine which mirror to remove")
        new_text, removed = _drop_mirror(self._document(), mirror_id)
        if not removed:
            return fail(f"maven: mirror {mirror_id} is not configured")
        self._write(new_text)
        return ok(f"maven: mirror {mirror_id} removed", changed=[str(self.settings_path())])

    def enable(self, source: Source) -> OperationResult:
        """Maven has no disabled mirrors: enabling (re-)adds the mirror."""
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Maven has no ``enabled`` flag for mirrors, so disabling removes it.

        ``enable()`` can restore it because every source produced by this plugin
        carries the full mirror definition in ``extra``.
        """
        result = self.remove(source)
        if result.ok:
            result.message = result.message.replace("removed", "disabled (removed)")
        return result

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        mirror_id = str(source.extra.get("mirror_id") or source.provider_key or "").strip()
        if not mirror_id:
            return fail("maven: cannot determine which mirror to update")
        current = next((m for m in self._mirrors() if m.id == mirror_id), None)
        if current is None:
            return fail(f"maven: mirror {mirror_id} is not configured")
        url = str(changes.get("url") or current.url).strip()
        mirror_of = str(changes.get("mirror_of") or changes.get("mirrorOf") or current.mirror_of).strip()
        name = str(changes.get("name") or current.name or mirror_id)
        if not url:
            return fail("maven: a mirror needs a URL")
        text = self._document()
        target = next((m for m in _parse_mirrors(text) if m.id == mirror_id), None)
        if target is None:
            return fail(f"maven: mirror {mirror_id} disappeared from {self.settings_path()}")
        block = _set_tag(target.raw, "url", url)
        block = _set_tag(block, "mirrorOf", mirror_of)
        block = _set_tag(block, "name", name)
        self._write(text[: target.start] + block + text[target.end :])
        return ok(
            f"maven: mirror {mirror_id} -> {url} (mirrorOf={mirror_of})",
            changed=[str(self.settings_path())],
        )

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe ``<url>/…/maven-metadata.xml`` (404 still proves the host answered)."""
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
            result.error = "mirror has no URL"
            return result
        target = url.rstrip("/") + PROBE_PATH
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
                "maven-metadata.xml at that path (mirror may proxy other repositories)"
            )
        elif probe.http_status == 403:
            result.detail = f"probed {target}: HTTP 403 (host answered; bot filter)"
        else:
            result.detail = f"probed {target}"
        return result

    def native_test_command(self) -> Sequence[str] | None:
        """No native command: ``mvn -o help:evaluate`` is offline/local only.

        A meaningful online check would resolve an artifact through the configured
        mirror, i.e. boot the full Maven runtime - far too heavy for a health check.
        """
        return None


__all__ = ["MavenManager", "Mirror", "covers_central"]
