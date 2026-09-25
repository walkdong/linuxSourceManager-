"""Git URL-rewriting manager plugin.

Git has no registry setting.  Instead a *rule* rewrites remote URLs::

    [url "https://gitclone.com/github.com/"]
        insteadOf = https://github.com/

Any clone of ``https://github.com/owner/repo`` is then fetched from
``https://gitclone.com/github.com/owner/repo``.  One rule = one source here:
``source.url`` is the replacement base and ``extra["instead_of"]`` the prefix list
being rewritten.  A base may carry several ``insteadOf`` values (git supports
multi-value keys) and they are all kept on one source, in file order.

Reading and writing
-------------------
* Rules are read through ``git config --global --get-regexp '^url\\.'`` when the
  ``git`` binary is usable, and from the global config file otherwise.
* ``add``/``remove`` run ``git config --global ...`` (argv list, never a shell) so
  multi-value/``insteadOf`` semantics stay exactly git's.  When ``git`` is missing -
  or when ``$SRCMGR_GITCONFIG`` overrides the file, since git would write somewhere
  else - the config file is edited directly by a small, comment-preserving INI
  editor that only touches the ``[url "..."]`` sections it owns.

There is no per-rule "enabled" flag in git: a rule exists and is applied, or it does
not exist.  ``disable()`` is therefore *destructive* (it unsets the rule) and
``enable()`` re-adds it from ``source.extra`` (which every source returned by this
plugin carries).  ``supports`` advertises both, and both are documented as such.

The mirror catalogue itself belongs to providers: only the canonical GitHub host is
built in, and well-known accelerators are offered merely as a fallback for
``list_available()`` when no provider is installed.
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

MANAGER = "git"

#: The canonical host, used when nothing is configured.
DEFAULT_URL = "https://github.com/"
DEFAULT_NAME = "github.com"
#: Config override that git itself does not know about (tests, per-project configs).
CONFIG_OVERRIDE = "SRCMGR_GITCONFIG"

#: Minimal built-in catalogue; providers normally supply the real one.
WELL_KNOWN: tuple[tuple[str, str, SourceKind, str], ...] = (
    ("github.com", "https://github.com/", SourceKind.OFFICIAL, "GitHub over HTTPS"),
    (
        "gitclone.com/github.com",
        "https://gitclone.com/github.com/",
        SourceKind.MIRROR,
        "gitclone.com GitHub accelerator",
    ),
    (
        "ghproxy.com/https://github.com",
        "https://ghproxy.com/https://github.com/",
        SourceKind.MIRROR,
        "ghproxy.com GitHub proxy",
    ),
)

_SECTION_NAME = "url"


# ---------------------------------------------------------------------------
# git-config text editing (used when the git binary is unavailable)
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    """One ``url.<base>.insteadOf`` rule."""

    base: str
    prefixes: list[str] = field(default_factory=list)
    push_prefixes: list[str] = field(default_factory=list)
    file: str = ""


def _parse_header(line: str) -> tuple[str, str] | None:
    """``[url "https://x/"]`` -> ``("url", "https://x/")``."""
    text = line.strip()
    if not text.startswith("[") or text.startswith("[["):
        return None
    end = text.find("]")
    if end < 0:
        return None
    rest = text[end + 1 :].strip()
    if rest and not rest.startswith(("#", ";")):
        return None
    inner = text[1:end].strip()
    if not inner:
        return None
    quoted = re.match(
        r"""^(?P<name>[A-Za-z0-9][A-Za-z0-9.\-]*)\s+(?P<q>["'])(?P<sub>.*)(?P=q)$""",
        inner,
        re.DOTALL,
    )
    if quoted:
        return quoted.group("name").lower(), quoted.group("sub")
    if " " in inner:
        name, _, sub = inner.partition(" ")
        return name.strip().lower(), sub.strip().strip("\"'")
    return inner.lower(), ""


def _unquote_value(value: str) -> str:
    """Unquote a git config value and drop an unquoted trailing comment."""
    text = value.strip()
    if not text:
        return ""
    if text[0] in "\"'":
        quote = text[0]
        out: list[str] = []
        escaped = False
        for index, char in enumerate(text[1:], start=1):
            if escaped:
                out.append({"n": "\n", "t": "\t"}.get(char, char))
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                return "".join(out)
            out.append(char)
        return "".join(out)
    for index, char in enumerate(text):
        if char in "#;" and (index == 0 or text[index - 1].isspace()):
            return text[:index].strip()
    return text


def _parse_key_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None
    if "=" in stripped:
        key, _, value = stripped.partition("=")
        return key.strip().lower(), _unquote_value(value)
    if re.match(r"^[A-Za-z][A-Za-z0-9\-]*$", stripped):
        return stripped.lower(), "true"
    return None


def _section_span(lines: Sequence[str], name: str, subsection: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        header = _parse_header(line)
        if header is None:
            continue
        if start is None:
            if header[0] == name.lower() and header[1] == subsection:
                start = index
            continue
        return start, index
    if start is None:
        return None
    return start, len(lines)


def _parse_file(text: str, file: str = "") -> list[Rule]:
    """Read every ``[url "..."]`` rule out of a git config file."""
    rules: list[Rule] = []
    current: Rule | None = None
    for line in (text or "").splitlines():
        header = _parse_header(line)
        if header is not None:
            current = None
            if header[0] == _SECTION_NAME and header[1]:
                current = Rule(base=header[1], file=file)
                rules.append(current)
            continue
        if current is None:
            continue
        parsed = _parse_key_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == "insteadof" and value:
            current.prefixes.append(value)
        elif key == "pushinsteadof" and value:
            current.push_prefixes.append(value)
    return rules


def _prune_empty_section(lines: list[str], name: str, subsection: str) -> list[str]:
    span = _section_span(lines, name, subsection)
    if span is None:
        return lines
    start, end = span
    if any(_parse_key_line(line) for line in lines[start + 1 : end]):
        return lines
    del lines[start:end]
    if 0 < start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
        del lines[start]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _file_add(text: str, base: str, key: str, value: str) -> tuple[str, bool]:
    """Add ``key = value`` to ``[url "<base>"]``; returns ``(text, changed)``."""
    lines = (text or "").splitlines()
    span = _section_span(lines, _SECTION_NAME, base)
    if span is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f'[url "{base}"]')
        lines.append(f"\t{key} = {value}")
        return "\n".join(lines) + "\n", True
    start, end = span
    for line in lines[start + 1 : end]:
        parsed = _parse_key_line(line)
        if parsed and parsed[0] == key.lower() and parsed[1] == value:
            return text, False
    insert_at = start + 1
    for index in range(end - 1, start, -1):
        if lines[index].strip():
            insert_at = index + 1
            break
    lines.insert(insert_at, f"\t{key} = {value}")
    return "\n".join(lines) + "\n", True


def _file_unset(text: str, base: str, key: str, value: str | None = None) -> tuple[str, bool]:
    """Remove ``key`` (optionally only one ``value``) from the rule; prune if empty."""
    lines = (text or "").splitlines()
    span = _section_span(lines, _SECTION_NAME, base)
    if span is None:
        return text, False
    start, end = span
    out = lines[: start + 1]
    removed = False
    for line in lines[start + 1 : end]:
        parsed = _parse_key_line(line)
        if parsed and parsed[0] == key.lower() and (value is None or parsed[1] == value):
            removed = True
            continue
        out.append(line)
    if not removed:
        return text, False
    out.extend(lines[end:])
    out = _prune_empty_section(out, _SECTION_NAME, base)
    if not out:
        return "", True
    return "\n".join(out) + "\n", True


def _file_drop_rule(text: str, base: str) -> tuple[str, bool]:
    """Delete the whole ``[url "<base>"]`` section."""
    lines = (text or "").splitlines()
    span = _section_span(lines, _SECTION_NAME, base)
    if span is None:
        return text, False
    start, end = span
    del lines[start:end]
    if 0 < start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
        del lines[start]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "", True
    return "\n".join(lines) + "\n", True


# ---------------------------------------------------------------------------
# plugin
# ---------------------------------------------------------------------------
class GitManager(ManagerPlugin):
    """Manages ``url.<base>.insteadOf`` rules in the global git configuration."""

    #: Canonical host used when no rule is configured.
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
        env_global = os.environ.get("GIT_CONFIG_GLOBAL")
        if env_global:
            candidates.append(Path(env_global).expanduser())
        candidates.append(helpers.home() / ".gitconfig")
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else helpers.home() / ".config"
        candidates.append(base / "git" / "config")
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

    def _registry_sources(self) -> list[Source]:
        """Catalogue entries contributed by provider plugins (may be empty)."""
        registry = getattr(self, "_registry", None)
        if registry is None:
            return []
        try:
            return list(registry.sources_for(self.name))
        except Exception:  # noqa: BLE001 - the catalogue is optional
            return []

    def _use_cli(self) -> bool:
        """Run ``git`` only when it writes the very file this plugin resolves.

        With ``$SRCMGR_GITCONFIG`` set the override is invisible to git, so the file
        is edited directly instead of letting git write to the user's real config.
        """
        if os.environ.get(CONFIG_OVERRIDE):
            return False
        if helpers.which("git") is None:
            return False
        return self.config_path() == helpers.home() / ".gitconfig"

    # ---------------------------------------------------------------- detect
    def detect(self) -> bool:
        return helpers.which("git") is not None or any(p.exists() for p in self._candidates())

    def detect_detail(self) -> dict[str, Any]:
        rules = self.rules()
        return {
            "available": self.detect(),
            "files": [str(p) for p in self.config_files()],
            "binary": helpers.which("git") or "",
            "config": str(self.config_path()),
            "config_exists": self.config_path().exists(),
            "write_mode": "git config --global" if self._use_cli() else "direct file edit",
            "rules": [rule.base for rule in rules],
        }

    # ------------------------------------------------------------------ read
    def rules(self) -> list[Rule]:
        """Every configured rule, in config order."""
        rules = self._rules_from_cli()
        if rules is None:
            path = self.config_path()
            rules = _parse_file(read_text(path), str(path))
        return rules

    def _rules_from_cli(self) -> list[Rule] | None:
        if not self._use_cli():
            return None
        try:
            completed = helpers.run(
                ["git", "config", "--global", "--get-regexp", r"^url\."], timeout=15.0
            )
        except Exception:  # noqa: BLE001 - fall back to the file
            return None
        if completed.returncode != 0:
            return None
        ordered: dict[str, Rule] = {}
        for line in (completed.stdout or "").splitlines():
            key, _, value = line.partition(" ")
            lowered = key.lower()
            if not lowered.startswith("url.") or not value.strip():
                continue
            for suffix, attribute in ((".insteadof", "prefixes"), (".pushinsteadof", "push_prefixes")):
                if lowered.endswith(suffix):
                    base = key[len("url.") : -len(suffix)]
                    rule = ordered.setdefault(base, Rule(base=base, file=str(self.config_path())))
                    getattr(rule, attribute).append(_unquote_value(value))
                    break
        return list(ordered.values())

    # ------------------------------------------------------------- listings
    def _source_for(self, rule: Rule, *, is_default: bool, enabled: bool = True) -> Source:
        base = rule.base
        official = base.rstrip("/") == DEFAULT_URL.rstrip("/")
        return make_source(
            MANAGER,
            name=_host_of(base),
            url=base,
            kind=SourceKind.OFFICIAL if official else SourceKind.MIRROR,
            scope=self.manifest.scope,
            enabled=enabled,
            is_default=is_default,
            file=rule.file or str(self.config_path()),
            provider_key=base,
            description="Rewrites " + ", ".join(rule.prefixes) if rule.prefixes else "",
            health={"url": base},
            extra={
                "value": base,
                "instead_of": list(rule.prefixes),
                "push_instead_of": list(rule.push_prefixes),
                "config_key": f"url.{base}.insteadof",
            },
        )

    def list_enabled(self) -> list[Source]:
        """Every configured rule; the plain canonical host when none is."""
        rules = self.rules()
        if not rules:
            return [
                make_source(
                    MANAGER,
                    name=DEFAULT_NAME,
                    url=DEFAULT_URL,
                    kind=SourceKind.OFFICIAL,
                    scope=self.manifest.scope,
                    enabled=True,
                    is_default=True,
                    file=str(self.config_path()),
                    provider_key="github.com",
                    description="No url.*.insteadOf rule configured: remotes are used as written.",
                    health={"url": DEFAULT_URL},
                    extra={
                        "value": DEFAULT_URL,
                        "instead_of": [],
                        "config_key": f"url.{DEFAULT_URL}.insteadof",
                        "configured": False,
                    },
                )
            ]
        return [self._source_for(rule, is_default=index == 0) for index, rule in enumerate(rules)]

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Provider catalogue, plus the well-known fallback when none is installed."""
        sources = list(self._registry_sources())
        configured = {rule.base for rule in self.rules()}
        for name, base, kind, description in WELL_KNOWN:
            if any(source.url.rstrip("/") == base.rstrip("/") for source in sources):
                continue
            sources.append(
                make_source(
                    MANAGER,
                    name=name,
                    url=base,
                    kind=kind,
                    scope=self.manifest.scope,
                    enabled=base in configured,
                    provider_key=name,
                    description=description,
                    health={"url": base},
                    extra={
                        "value": base,
                        "instead_of": [DEFAULT_URL] if base != DEFAULT_URL else [],
                        "config_key": f"url.{base}.insteadof",
                    },
                )
            )
        return sources

    def current(self) -> Source | None:
        enabled = self.list_enabled()
        return enabled[0] if enabled else None

    # ------------------------------------------------------------ mutations
    def _prefixes_of(self, source: Source) -> list[str]:
        raw = source.extra.get("instead_of") or source.extra.get("prefixes") or source.extra.get("prefix")
        if isinstance(raw, str):
            raw = [raw]
        prefixes = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if not prefixes and source.extra.get("instead_of_default", True):
            if source.url.rstrip("/") != DEFAULT_URL.rstrip("/"):
                prefixes = [DEFAULT_URL]
        return prefixes

    def add(self, source: Source) -> OperationResult:
        base = (source.url or "").strip()
        if not base:
            return fail("git: source has no replacement base URL")
        prefixes = self._prefixes_of(source)
        if not prefixes:
            return fail(
                "git: no URL prefix to rewrite",
                hint='pass extra={"instead_of": ["https://github.com/"]}',
            )
        added: list[str] = []
        present: list[str] = []
        errors: list[str] = []
        if self._use_cli():
            for prefix in prefixes:
                status = self._cli_add(base, prefix)
                if status == "added":
                    added.append(f"{base} insteadOf {prefix}")
                elif status == "present":
                    present.append(prefix)
                else:
                    errors.append(prefix)
            if errors and not added:
                return fail(
                    f"git: could not write the rule for {base}",
                    failed=errors,
                    hint="check `git config --global` permissions",
                )
        else:
            path = self.config_path()
            text = read_text(path)
            for prefix in prefixes:
                text, modified = _file_add(text, base, "insteadOf", prefix)
                if modified:
                    added.append(f"{base} insteadOf {prefix}")
                else:
                    present.append(prefix)
            if added:
                ensure_dir(path.parent)
                atomic_write_text(path, text)
        if not added:
            return ok(f"git: {base} already rewrites {', '.join(prefixes)}")
        return ok(
            f"git: {base} rewrites {', '.join(prefixes)}",
            changed=[str(self.config_path())],
            added=added,
            already_present=present,
        )

    def _cli_add(self, base: str, prefix: str) -> str:
        """Add one ``insteadOf`` value; returns ``added``/``present``/``error``."""
        key = f"url.{base}.insteadOf"
        try:
            existing = helpers.run(["git", "config", "--global", "--get-all", key], timeout=15.0)
            if existing.returncode == 0 and prefix in (existing.stdout or "").splitlines():
                return False
            helpers.run(["git", "config", "--global", "--add", key, prefix], timeout=15.0)
        except Exception:  # noqa: BLE001 - fall back to direct editing
            return False
        return True

    def remove(self, source: Source) -> OperationResult:
        base = (source.url or "").strip()
        if not base:
            return fail("git: source has no base URL")
        if self._use_cli():
            key = f"url.{base}.insteadOf"
            try:
                helpers.run(["git", "config", "--global", "--unset-all", key], timeout=15.0)
                remaining = helpers.run(
                    ["git", "config", "--global", "--get-regexp", rf"^url\.{re.escape(base)}\."],
                    timeout=15.0,
                )
                if remaining.returncode != 0 and not (remaining.stdout or "").strip():
                    helpers.run(
                        ["git", "config", "--global", "--remove-section", f"url.{base}"], timeout=15.0
                    )
            except Exception as exc:  # noqa: BLE001
                return fail(f"git: could not unset the rule for {base}: {exc}")
        else:
            path = self.config_path()
            text = read_text(path)
            new_text, changed = _file_drop_rule(text, base)
            if not changed:
                return fail(f"git: no rule for {base}")
            atomic_write_text(path, new_text)
        return ok(
            f"git: removed rewrite rule for {base}",
            changed=[str(self.config_path())],
            instead_of=list(source.extra.get("instead_of") or []),
        )

    def enable(self, source: Source) -> OperationResult:
        """Re-add a rule (there is no enabled flag: enabling means writing it back)."""
        return self.add(source)

    def disable(self, source: Source) -> OperationResult:
        """Unset the rule.

        Git has no notion of a disabled rule, so this *removes* it; ``enable()`` can
        restore it later because sources returned by this plugin carry
        ``extra["instead_of"]``.
        """
        return self.remove(source)

    def set_current(self, source: Source) -> OperationResult:
        return self.add(source)

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        new_base = str(changes.get("url") or "").strip()
        if not new_base:
            return fail("git: only 'url' (the replacement base) can be updated")
        old_base = (source.url or "").strip()
        self.remove(source)
        updated = make_source(
            MANAGER,
            name=_host_of(new_base),
            url=new_base,
            kind=source.kind,
            scope=source.scope,
            provider_key=new_base,
            extra={**dict(source.extra), "value": new_base, "instead_of": list(source.extra.get("instead_of") or [])},
        )
        result = self.add(updated)
        if not result.ok:
            return result
        result.message = f"git: {old_base} -> {new_base} ({result.message})"
        result.changed = [str(self.config_path())]
        return result

    # ---------------------------------------------------------------- health
    def native_test_command(self) -> Sequence[str] | None:
        # A real fetch over the effective configuration; generous timeout by design.
        return ["git", "ls-remote", "https://github.com/git/git", "HEAD"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
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
            result.ok = False
            result.status = "no-url"
            result.error = "rule has no replacement base URL"
            return result
        probe = probe_url(url, timeout=timeout)
        result.http_status = probe.http_status
        result.latency_ms = probe.latency_ms
        result.tls_ok = probe.tls_ok
        result.redirected_to = probe.redirected_to
        result.method = probe.method or "HEAD"
        result.ok = bool(probe.ok)
        result.status = "ok" if result.ok else probe.status
        result.error = "" if result.ok else probe.error
        result.detail = f"probed the replacement base {url}"
        return result


def _host_of(url: str) -> str:
    """``gitclone.com/github.com`` for a readability-friendly source name."""
    text = re.sub(r"^[a-z]+\+", "", (url or "").strip())
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^https?://", "", text)
    return text.rstrip("/") or DEFAULT_NAME


__all__ = ["GitManager", "Rule"]
