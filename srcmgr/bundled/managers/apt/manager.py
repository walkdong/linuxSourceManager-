"""APT manager plugin: read and edit Debian/Ubuntu sources safely.

Two on-disk formats are supported, both ways:

* **one-line** - ``/etc/apt/sources.list`` and ``/etc/apt/sources.list.d/*.list``::

      deb [arch=amd64 signed-by=/usr/share/keyrings/x.gpg] https://url trixie main

  plus ``deb-src`` entries and ``*.list.disabled`` files (reported as disabled).
  Commented-out ``deb`` lines are reported as *disabled* entries too, because that is
  how ``apt`` users park a repository and how ``disable()`` stores one.

* **deb822** - ``/etc/apt/sources.list.d/*.sources``::

      Types: deb
      URIs: https://url/debian
      Suites: trixie
      Components: main contrib
      Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----
       ...
      Enabled: no

  Multi-line continuation fields (including inline ASCII-armoured PGP keys) are kept
  verbatim: a stanza is only rewritten when it is the *target* of an operation, and
  even then only the touched field lines are replaced.

Safety rules that the rest of the code depends on:

* every read and write goes through :meth:`AptManager.root`, which defaults to
  ``/etc/apt`` and can be redirected with ``SRCMGR_APT_ROOT`` (used by the tests);
* ``_write``/``_unlink``/``_rename`` refuse to touch anything outside that root;
* a file is never rewritten unless one of its entries is the operation's target, so
  unrelated content stays byte-identical.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from srcmgr.core import helpers
from srcmgr.core.api import (
    ManagerPlugin,
    OperationResult,
    Scope,
    Source,
    SourceKind,
    TestResult,
)
from srcmgr.core.backup import utc_now
from srcmgr.core.health import ProbeResult, probe_url
from srcmgr.core.paths import atomic_write_text, ensure_dir
from srcmgr.core.registry import normalize_url

DEFAULT_APT_ROOT = Path("/etc/apt")
ROOT_ENV = "SRCMGR_APT_ROOT"
OS_RELEASE_ENV = "SRCMGR_APT_OS_RELEASE"
DISABLED_SUFFIX = ".disabled"

#: fallback patterns when the manifest does not declare ``config_paths``
CONFIG_PATTERNS: tuple[str, ...] = (
    "/etc/apt/sources.list",
    "/etc/apt/sources.list.d/*.list",
    "/etc/apt/sources.list.d/*.sources",
    "/etc/apt/sources.list.d/*.list.disabled",
    "/etc/apt/sources.list.d/*.sources.disabled",
)
#: canonical drop-ins we may create, listed even when absent so a backup records
#: "did not exist before" for the files a later operation creates
CANONICAL_NEW_FILES: tuple[str, ...] = (
    "sources.list.d/ubuntu.sources",
    "sources.list.d/debian.sources",
)

OFFICIAL_HOSTS = {
    "deb.debian.org",
    "ftp.debian.org",
    "security.debian.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ports.ubuntu.com",
}
OFFICIAL_DOMAINS = tuple(sorted(OFFICIAL_HOSTS))

#: third-party repositories that ship their own package (not mirrors of a distro)
VENDOR_HOSTS = {
    "ppa.launchpadcontent.net",
    "ppa.launchpad.net",
    "packages.microsoft.com",
    "download.docker.com",
    "packages.cloud.google.com",
    "dl.google.com",
    "packages.sury.org",
    "deb.nodesource.com",
    "apt.postgresql.org",
    "packages.gitlab.com",
    "repo.mongodb.org",
    "artifacts.elastic.co",
    "packages.elastic.co",
    "cli.github.com",
    "packages.nvidia.com",
    "developer.download.nvidia.com",
    "repo.steampowered.com",
    "apt.releases.hashicorp.com",
    "packages.jetbrains.team",
    "repository.spotify.com",
}
VENDOR_HINTS = (
    "launchpad",
    "packages.microsoft",
    "download.docker",
    "packages.cloud.google",
    "nvidia",
    "steampowered",
    "spotify",
    "gitlab",
    "jetbrains",
    "nodesource",
    "postgresql",
    "mongodb",
    "elastic",
    "hashicorp",
)
#: a generic hint only - the curated mirror catalogue belongs to provider plugins
MIRROR_HINTS = ("mirror",)
_PRIVATE_RE = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|.*\.local|.*\.lan|.*\.internal|.*\.corp)$"
)
#: Debian releases that ship the ``non-free-firmware`` component
DEBIAN_NONFREE_FIRMWARE = {"bookworm", "trixie", "forky", "sid", "testing", "unstable"}
#: Debian suites that have neither ``-updates`` nor ``-security``
DEBIAN_ROLLING = {"sid", "unstable"}
UBUNTU_IDS = {"ubuntu", "linuxmint", "pop", "pop-os", "elementary", "zorin", "neon", "kde-neon"}
DEBIAN_IDS = {"debian", "lmde", "raspbian", "kali", "devuan", "pureos", "parrot", "mx", "antix"}

#: Debian-family systems whose packages do NOT come from deb.debian.org.  They must never
#: be silently mapped onto the Debian archive when a provider template is expanded.
DISTROS_WITH_OWN_ARCHIVE = {"kali", "raspbian", "parrot"}

FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*:\s?(.*)$")
ONE_LINE_RE = re.compile(r"^(deb-src|deb)\s+(.*)$")
_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")
_UNCOMMENT_RE = re.compile(r"^(\s*)#\s?")
_ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "i386",
    "i486": "i386",
    "i586": "i386",
    "i686": "i386",
    "x86": "i386",
    "armv7l": "armhf",
    "armv6l": "armhf",
    "armv8l": "arm64",
    "ppc64le": "ppc64el",
    "ppc64": "ppc64",
    "s390x": "s390x",
    "riscv64": "riscv64",
    "loongarch64": "loong64",
    "mips64el": "mips64el",
    "mipsel": "mipsel",
}


class AptError(RuntimeError):
    """Internal, user-facing failure (converted to a failing result by the API)."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _as_list(value: Any) -> list[str]:
    """Normalize ``"main contrib"`` / ``"main,contrib"`` / ``["main"]`` to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in re.split(r"[,\s]+", value.strip()) if part]
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[str] = []
        for item in value:
            out.extend(_as_list(str(item)))
        return out
    return [str(value)]


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_int(value: Any) -> int | None:
    """Tolerant int conversion for caller supplied location hints."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm(url: str) -> str:
    return normalize_url(url or "")


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:  # pragma: no cover - malformed URL
        return ""


def _is_official_host(host: str) -> bool:
    if not host:
        return False
    if host in OFFICIAL_HOSTS:
        return True
    return any(host.endswith("." + base) for base in OFFICIAL_DOMAINS)


def _append_block(existing: str, block: str, *, blank: bool = True) -> str:
    """Append ``block`` to ``existing`` keeping one separating blank line."""
    block = block.strip("\n") + "\n"
    if not existing.strip():
        return block
    text = existing.rstrip("\n") + "\n"
    if blank and not text.endswith("\n\n"):
        text += "\n"
    return text + block


def _cut_lines(lines: Sequence[str], start: int, end: int) -> list[str]:
    """Drop ``lines[start:end]`` and tidy up the blank lines around the hole."""
    new = list(lines[:start]) + list(lines[end:])
    before_blank = start > 0 and not lines[start - 1].strip()
    after_blank = end < len(lines) and not lines[end].strip()
    if before_blank and after_blank:
        del new[start - 1]
    while new and not new[0].strip():
        new.pop(0)
    while new and not new[-1].strip():
        new.pop()
    return new


def _field_lines(name: str, value: str) -> list[str]:
    """Render a deb822 field, using continuation lines for multi-line values."""
    parts = str(value).splitlines() or [""]
    if len(parts) == 1:
        return [f"{name}: {parts[0]}".rstrip()]
    out = [f"{name}:"]
    out.extend(" ." if part == "" else f" {part}" for part in parts)
    return out


def _set_field(lines: Sequence[str], name: str, value: str) -> list[str]:
    """Set ``name`` in a deb822 block, preserving field order and spelling."""
    lower = name.lower()
    out = list(lines)
    header: int | None = None
    end = len(out)
    for index, raw in enumerate(out):
        match = FIELD_RE.match(raw)
        if match and match.group(1).lower() == lower:
            header = index
            cursor = index + 1
            while cursor < len(out) and out[cursor][:1] in (" ", "\t"):
                cursor += 1
            end = cursor
            name = match.group(1)
            break
    if header is None:
        insert = len(out)
        for index, raw in enumerate(out):
            if FIELD_RE.match(raw) or raw[:1] in (" ", "\t"):
                insert = index + 1
        return out[:insert] + _field_lines(name, value) + out[insert:]
    return out[:header] + _field_lines(name, value) + out[end:]


def _remove_field(lines: Sequence[str], name: str) -> list[str]:
    """Remove ``name`` (and its continuation lines) from a deb822 block."""
    lower = name.lower()
    out = list(lines)
    for index, raw in enumerate(out):
        match = FIELD_RE.match(raw)
        if not match or match.group(1).lower() != lower:
            continue
        cursor = index + 1
        while cursor < len(out) and out[cursor][:1] in (" ", "\t"):
            cursor += 1
        del out[index:cursor]
        break
    return out


def _comment(line: str) -> str:
    return re.sub(r"^(\s*)", r"\1# ", line, count=1)


def _uncomment(line: str) -> str:
    return _UNCOMMENT_RE.sub(r"\1", line, count=1)


def _join_value(chunks: Sequence[str]) -> str:
    """Join a deb822 field's first value plus continuation lines.

    A continuation line consisting of a single ``.`` means "empty line", which is how
    inline PGP keys are embedded.
    """
    parts = [chunks[0].strip() if chunks else ""]
    for raw in chunks[1:]:
        if raw.strip() == ".":
            parts.append("")
        elif raw[:1] in (" ", "\t"):
            parts.append(raw[1:].rstrip())
        else:
            parts.append(raw.strip())
    return "\n".join(parts).strip()


def _split_blocks(lines: Sequence[str]) -> list[tuple[int, int, list[str]]]:
    """Split a file into blank-line separated blocks: ``(start, end, lines)``."""
    blocks: list[tuple[int, int, list[str]]] = []
    start: int | None = None
    for index, raw in enumerate(lines):
        if not raw.strip():
            if start is not None:
                blocks.append((start, index, list(lines[start:index])))
                start = None
            continue
        if start is None:
            start = index
    if start is not None:
        blocks.append((start, len(lines), list(lines[start:])))
    return blocks


# ---------------------------------------------------------------------------
# parsed entries
# ---------------------------------------------------------------------------
@dataclass
class OneLineEntry:
    """One ``deb``/``deb-src`` line of a ``sources.list``-style file."""

    file: Path
    line: int
    raw: str
    types: list[str]
    url: str
    suite: str
    components: list[str]
    options: dict[str, str] = field(default_factory=dict)
    commented: bool = False
    indent: str = ""

    @property
    def enabled(self) -> bool:
        return not self.commented and not self.file.name.endswith(DISABLED_SUFFIX)

    @property
    def architectures(self) -> list[str]:
        return _as_list(self.options.get("arch") or self.options.get("architectures") or "")

    @property
    def signed_by(self) -> str:
        return self.options.get("signed-by", "") or self.options.get("signedby", "")

    @property
    def inline_key(self) -> bool:
        return False

    @property
    def describe(self) -> str:
        return f"{self.file}:{self.line}"

    def render(
        self,
        *,
        types: Sequence[str] | None = None,
        url: str | None = None,
        suite: str | None = None,
        components: Sequence[str] | None = None,
        architectures: Sequence[str] | None = None,
        options: Mapping[str, str] | None = None,
        enabled: bool | None = None,
    ) -> str:
        """Render this entry back to a one-line definition (uncommented parts)."""
        kind = (types or self.types or ["deb"])[0]
        opts = dict(self.options if options is None else options)
        if architectures is not None:
            opts.pop("architectures", None)
            if architectures:
                opts["arch"] = ",".join(architectures)
            else:
                opts.pop("arch", None)
        body_parts = [kind]
        if opts:
            body_parts.append("[" + " ".join(f"{key}={value}" for key, value in opts.items()) + "]")
        body_parts.append((url or self.url).strip())
        body_parts.append((suite if suite is not None else self.suite).strip())
        body_parts.extend(components if components is not None else self.components)
        commented = self.commented if enabled is None else not enabled
        prefix = "# " if commented else ""
        return f"{self.indent}{prefix}{' '.join(part for part in body_parts if part)}"


@dataclass
class Deb822Stanza:
    """One stanza of a deb822 ``.sources`` file, kept verbatim in :attr:`lines`."""

    file: Path
    index: int
    line_start: int
    lines: list[str]
    fields: dict[str, list[int]] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)

    @property
    def line(self) -> int:
        return self.line_start + 1

    @property
    def line_end(self) -> int:
        """Exclusive end line of this stanza inside the file (0-based)."""
        return self.line_start + len(self.lines)

    @property
    def enabled(self) -> bool:
        if self.file.name.endswith(DISABLED_SUFFIX):
            return False
        return self.text("enabled").strip().lower() not in {"no", "false", "0", "off"}

    @property
    def describe(self) -> str:
        return f"{self.file}:{self.line}"

    def has(self, name: str) -> bool:
        return name.lower() in self.values

    def text(self, name: str) -> str:
        return self.values.get(name.lower(), "")

    def flat(self, name: str) -> str:
        return " ".join(self.text(name).split())

    def tokens(self, name: str) -> list[str]:
        return self.text(name).split()

    def _either(self, *names: str) -> str:
        for name in names:
            if self.has(name):
                return self.text(name)
        return ""

    @property
    def types(self) -> list[str]:
        return self._either("types", "type").split()

    @property
    def uris(self) -> list[str]:
        return self._either("uris", "uri").split()

    @property
    def suites(self) -> list[str]:
        return self._either("suites", "suite").split()

    @property
    def components(self) -> list[str]:
        return self.tokens("components")

    @property
    def architectures(self) -> list[str]:
        return _as_list(self._either("architectures", "architecture"))

    @property
    def signed_by(self) -> str:
        return self.text("signed-by")

    @property
    def inline_key(self) -> bool:
        return self.signed_by.lstrip().startswith("-----BEGIN")


Entry = OneLineEntry | Deb822Stanza


def parse_one_line_entry(line: str, path: Path, number: int) -> OneLineEntry | None:
    """Parse one ``deb``/``deb-src`` line; return ``None`` for anything else."""
    stripped = line.strip()
    if not stripped:
        return None
    commented = stripped.startswith("#")
    body = stripped.lstrip("#").strip() if commented else stripped
    match = ONE_LINE_RE.match(body)
    if not match:
        return None
    rest = match.group(2).strip()
    options: dict[str, str] = {}
    if rest.startswith("["):
        closing = rest.find("]")
        if closing < 0:
            return None
        for token in rest[1:closing].split():
            key, _, value = token.partition("=")
            options[key.strip().lower()] = value.strip() or "yes"
        rest = rest[closing + 1:].strip()
    parts = rest.split()
    if len(parts) < 2:
        return None
    return OneLineEntry(
        file=path,
        line=number,
        raw=line,
        types=[match.group(1)],
        url=parts[0],
        suite=parts[1],
        components=parts[2:],
        options=options,
        commented=commented,
        indent=line[: len(line) - len(line.lstrip())],
    )


def parse_deb822_stanzas(text: str, path: Path) -> list[Deb822Stanza]:
    """Parse ``.sources`` text into stanzas, byte-faithfully."""
    lines = text.splitlines()
    stanzas: list[Deb822Stanza] = []
    for start, end, block in _split_blocks(lines):
        has_field = any(
            FIELD_RE.match(raw) and not raw.lstrip().startswith("#") for raw in block
        )
        if not has_field:
            continue
        field_index: dict[str, list[int]] = {}
        chunks: dict[str, list[str]] = {}
        current: str | None = None
        for offset, raw in enumerate(block):
            if raw.lstrip().startswith("#"):
                continue
            if raw[:1] in (" ", "\t"):
                if current is not None:
                    field_index[current].append(offset)
                    chunks[current].append(raw)
                continue
            match = FIELD_RE.match(raw)
            if not match:
                current = None
                continue
            current = match.group(1).lower()
            field_index.setdefault(current, []).append(offset)
            chunks.setdefault(current, []).append(match.group(2))
        values = {name: _join_value(items) for name, items in chunks.items()}
        stanzas.append(
            Deb822Stanza(
                file=path,
                index=len(stanzas),
                line_start=start,
                lines=list(block),
                fields=field_index,
                values=values,
            )
        )
    return stanzas


def _same_entry(left: Entry, right: Entry) -> bool:
    return type(left) is type(right) and left.line == right.line


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------
class AptManager(ManagerPlugin):
    """Debian/Ubuntu APT sources manager (one-line + deb822)."""

    supports: tuple[str, ...] = (
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
        #: explicit root override (used by tests and by ``SRCMGR_APT_ROOT``)
        self._root_override: Path | None = None

    # ------------------------------------------------------------------ root
    @property
    def root(self) -> Path:
        """Directory holding ``sources.list`` and ``sources.list.d``."""
        if self._root_override is not None:
            return self._root_override
        override = os.environ.get(ROOT_ENV)
        return Path(override).expanduser() if override else DEFAULT_APT_ROOT

    @root.setter
    def root(self, value: Path | str | None) -> None:
        self._root_override = Path(value).expanduser() if value else None

    def sources_list(self) -> Path:
        return self.root / "sources.list"

    def sources_dir(self) -> Path:
        return self.root / "sources.list.d"

    def _inside_root(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.root.resolve())
        except (OSError, ValueError):  # pragma: no cover - unreadable path
            return False

    def _guard(self, path: Path) -> Path:
        if not self._inside_root(path):
            raise AptError(f"apt: refusing to touch {path} outside {self.root}")
        return path

    def _write(self, path: Path, text: str) -> None:
        self._guard(path)
        ensure_dir(path.parent)
        atomic_write_text(path, text)

    def _unlink(self, path: Path) -> None:
        self._guard(path)
        path.unlink(missing_ok=True)

    def _rename(self, source: Path, destination: Path) -> None:
        self._guard(source)
        self._guard(destination)
        source.rename(destination)

    # ----------------------------------------------------------- os metadata
    def os_release_path(self) -> Path:
        override = os.environ.get(OS_RELEASE_ENV)
        if override:
            return Path(override)
        candidate = self.root.parent / "os-release"
        return candidate if candidate.exists() else Path("/etc/os-release")

    def os_release(self) -> dict[str, str]:
        """Parsed ``os-release`` for this machine (or the test root)."""
        data: dict[str, str] = {}
        try:
            text = helpers.read_text(self.os_release_path())
        except OSError:  # pragma: no cover - defensive
            return data
        for line in text.splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
        return data

    def distro_id(self) -> str:
        """``debian``, ``ubuntu``, ``linuxmint``, ... (``""`` when unknown)."""
        release = self.os_release()
        ident = (release.get("ID") or "").strip().lower()
        if ident:
            return ident
        like = (release.get("ID_LIKE") or "").split()
        return like[0].lower() if like else ""

    def id_like(self) -> str:
        return self.os_release().get("ID_LIKE", "")

    def version_id(self) -> str:
        return self.os_release().get("VERSION_ID", "")

    def codename(self) -> str:
        """Release codename: ``os-release``, then ``lsb_release -cs``, else ``""``."""
        release = self.os_release()
        for key in ("VERSION_CODENAME", "UBUNTU_CODENAME"):
            value = (release.get(key) or "").strip()
            if value:
                return value
        if helpers.which("lsb_release"):
            try:
                completed = helpers.run(["lsb_release", "-cs"], timeout=5.0)
            except (OSError, subprocess.SubprocessError):
                return ""
            if completed.returncode == 0:
                return (completed.stdout or "").strip()
        return ""

    def architectures(self) -> list[str]:
        """Native architectures as apt spells them (``amd64``, ``arm64``, ...)."""
        if helpers.which("dpkg"):
            try:
                completed = helpers.run(["dpkg", "--print-architecture"], timeout=5.0)
            except (OSError, subprocess.SubprocessError):
                completed = None
            if completed is not None and completed.returncode == 0:
                value = (completed.stdout or "").strip()
                if value:
                    return [value]
        machine = platform.machine().lower()
        return [_ARCH_ALIASES.get(machine, machine or "amd64")]

    def archive_family(self) -> str:
        """The upstream archive a mirror actually serves for this system.

        ``${distro}`` must be the *archive* name, not the local ``ID``: mirrors serve
        ``/ubuntu/`` and ``/debian/`` and nothing else, so a derivative has to be mapped
        to its parent archive or the generated URL 404s.  Non-Debian-family systems keep
        their own id (Kali, Devuan, Parrot and friends run their own archives, and
        guessing "debian" for them would be worse than letting the add fail loudly).
        """
        ident = self.distro_id()
        if ident in UBUNTU_IDS:
            return "ubuntu"
        if ident in DEBIAN_IDS:
            if ident in DISTROS_WITH_OWN_ARCHIVE:
                # Kali and Raspbian run their own archives: pointing them at
                # deb.debian.org would produce a plausible-looking but wrong source, so
                # leave the id alone and let a templated add fail with a clear message.
                return ident
            return "debian"
        return ident

    def template_values(self) -> dict[str, str]:
        """Values for provider ``url_template`` expansion (``${codename}`` ...).

        Public API: the core calls this when the user adds a templated source, e.g.
        ``https://mirrors.tuna.tsinghua.edu.cn/${distro}/``.

        * ``${distro}`` is the archive family (``ubuntu``/``debian``), so Mint, Pop!_OS
          and friends resolve to the archive their mirrors really host.
        * ``${id}`` is the raw os-release ``ID`` when a provider wants the local identity.
        """
        codename = self.codename()
        arch = self.architectures()
        return {
            "codename": codename,
            "distro": self.archive_family(),
            "id": self.distro_id(),
            "id_like": self.id_like(),
            "version_id": self.version_id(),
            "arch": arch[0] if arch else "",
            "suite": codename,
        }

    def _is_ubuntu_family(self) -> bool:
        release = self.os_release()
        ident = (release.get("ID") or "").strip().lower()
        like = set((release.get("ID_LIKE") or "").lower().split())
        if ident in UBUNTU_IDS or "ubuntu" in like:
            return True
        if ident in DEBIAN_IDS or "debian" in like:
            return False
        return False

    def _default_components(self) -> list[str]:
        if self._is_ubuntu_family():
            return ["main", "restricted", "universe", "multiverse"]
        components = ["main", "contrib", "non-free"]
        codename = self.codename()
        if not codename or codename in DEBIAN_NONFREE_FIRMWARE:
            components.append("non-free-firmware")
        return components

    # ----------------------------------------------------------- config files
    def list_files(self) -> list[Path]:
        """Existing apt source files: ``sources.list`` first, then drop-ins."""
        out: list[Path] = []
        if self.sources_list().is_file():
            out.append(self.sources_list())
        directory = self.sources_dir()
        if directory.is_dir():
            for pattern in ("*.list", "*.sources", "*.list.disabled", "*.sources.disabled"):
                for path in sorted(directory.glob(pattern)):
                    if path.is_file() and path not in out:
                        out.append(path)
        return out

    def sources_files(self) -> list[Path]:
        """Alias of :meth:`list_files` kept for plugin-internal callers."""
        return self.list_files()

    def config_files(self) -> list[Path]:
        """Expanded config paths *plus* canonical files that may not exist yet."""
        out: list[Path] = []
        seen: set[str] = set()

        def add(path: Path) -> None:
            key = str(path)
            if key not in seen:
                seen.add(key)
                out.append(path)

        manifest = getattr(self, "manifest", None)
        patterns = list(manifest.config_paths) if manifest and manifest.config_paths else list(CONFIG_PATTERNS)
        prefix = str(self.root).rstrip("/")
        for raw in patterns:
            text = str(raw)
            if text.startswith(str(DEFAULT_APT_ROOT)):
                text = prefix + text[len(str(DEFAULT_APT_ROOT)):]
            if any(char in text for char in "*?["):
                directory = Path(os.path.dirname(text) or ".")
                if directory.is_dir():
                    for match in sorted(directory.glob(os.path.basename(text))):
                        if match.is_file():
                            add(match)
                continue
            add(Path(text).expanduser())
        for canonical in CANONICAL_NEW_FILES:
            add(self.root / canonical)
        return out

    # -------------------------------------------------------------- detection
    def detect(self) -> bool:
        if self.root.is_dir():
            return True
        if helpers.which("apt-get") or helpers.which("apt"):
            return True
        release = self.os_release()
        ident = (release.get("ID") or "").strip().lower()
        like = (release.get("ID_LIKE") or "").lower().split()
        return ident in DEBIAN_IDS or ident in UBUNTU_IDS or bool({"debian", "ubuntu"} & set(like))

    def detect_detail(self) -> dict[str, Any]:
        detail = super().detect_detail()
        detail.update(
            {
                "codename": self.codename(),
                "distro": self.distro_id(),
                "id_like": self.id_like(),
                "arch": self.architectures(),
                "root": str(self.root),
            }
        )
        return detail

    # ------------------------------------------------------------------ parse
    def entries_in(self, path: Path) -> list[Entry]:
        """Every source entry defined in ``path`` (deb822 or one-line)."""
        text = helpers.read_text(path)
        if not text.strip():
            return []
        name = path.name
        if name.endswith(".sources") or name.endswith(".sources" + DISABLED_SUFFIX):
            return parse_deb822_stanzas(text, path)
        entries: list[Entry] = []
        for number, line in enumerate(text.splitlines(), start=1):
            entry = parse_one_line_entry(line, path, number)
            if entry is not None:
                entries.append(entry)
        return entries

    def list_entries(self) -> list[Entry]:
        """Entries of every source file, in file/line order."""
        out: list[Entry] = []
        for path in self.list_files():
            out.extend(self.entries_in(path))
        return out

    # ---------------------------------------------------------------- queries
    def list_enabled(self) -> list[Source]:
        """All configured entries; disabled ones are marked ``enabled=False``.

        Identical ``(types, url, suite, components)`` tuples are de-duplicated with the
        enabled occurrence winning.
        """
        sources: list[Source] = []
        for entry in self.list_entries():
            source = self._source_from_entry(entry)
            if source.url:
                sources.append(source)
        return _dedupe_sources(sources)

    def current(self) -> Source | None:
        """The enabled source apt resolves packages from right now.

        Precedence follows apt's own reading order: an official archive that covers this
        release wins, then the first enabled entry.  ``list_enabled()`` marks such entries
        ``is_default``.  The menu UI additionally consults the change history so that a
        mirror the user just switched to is reported as the active one, which the plugin
        alone cannot know (APT happily keeps several sources enabled at once).
        """
        enabled = [source for source in self.list_enabled() if source.enabled]
        if not enabled:
            return None
        preferred = [source for source in enabled if source.is_default]
        if preferred:
            return preferred[0]
        codename = self.codename()
        official = [source for source in enabled if _is_official_host(_host(source.url))]
        if official:
            for source in official:
                if codename and source.suite == codename:
                    return source
            for source in official:
                if codename and source.suite.startswith(codename):
                    return source
            return official[0]
        return enabled[0]

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Official Debian/Ubuntu archives for *this* machine's release.

        Mirror catalogues come from provider plugins; the core merges both.
        """
        return self._official_sources()

    def _official_sources(self) -> list[Source]:
        values = self.template_values()
        codename = values["codename"]
        arch = values["arch"]
        plans: list[tuple[str, str, str]] = []
        if self._is_ubuntu_family():
            components = ["main", "restricted", "universe", "multiverse"]
            host = "archive.ubuntu.com" if arch in {"amd64", "i386", ""} else "ports.ubuntu.com"
            base = f"http://{host}/{'ubuntu' if host.startswith('archive') else 'ubuntu-ports'}"
            if codename:
                plans.append((base, codename, "Ubuntu official"))
                plans.append((base, f"{codename}-updates", "Ubuntu updates"))
                plans.append((base, f"{codename}-backports", "Ubuntu backports"))
                plans.append(("http://security.ubuntu.com/ubuntu", f"{codename}-security", "Ubuntu security"))
            else:
                plans.append((base, "", "Ubuntu official"))
        else:
            components = self._default_components()
            if codename and codename not in DEBIAN_ROLLING:
                plans.append(("http://deb.debian.org/debian", codename, "Debian official"))
                plans.append(("http://deb.debian.org/debian", f"{codename}-updates", "Debian updates"))
                plans.append(
                    ("http://security.debian.org/debian-security", f"{codename}-security", "Debian security")
                )
            else:
                plans.append(("http://deb.debian.org/debian", codename or "sid", "Debian official"))

        installed = [source for source in self.list_enabled() if source.enabled]
        matches: set[int] = set()
        for position, (url, suite, _name) in enumerate(plans):
            for source in installed:
                suites = {source.suite, *_as_list((source.extra or {}).get("suites"))} - {""}
                if suite and suites and suite not in suites:
                    continue
                same_url = _norm(source.url) == _norm(url)
                # an official archive alias (cn.archive.ubuntu.com) counts as a match
                alias = _is_official_host(_host(source.url)) and _is_official_host(_host(url))
                if same_url or alias:
                    matches.add(position)
                    break
        if not matches:
            matches.add(0)  # nothing configured yet: the base suite is the default

        prefix = f"{values['distro'] or 'debian'}"
        out: list[Source] = []
        for position, (url, suite, name) in enumerate(plans):
            degraded = not codename
            out.append(
                Source(
                    manager="apt",
                    name=name,
                    url=url,
                    suite=suite,
                    components=list(components),
                    types=["deb"],
                    enabled=True,
                    kind=SourceKind.OFFICIAL,
                    scope=Scope.SYSTEM,
                    is_default=position in matches,
                    provider_key=f"{prefix}-{suite or 'stable'}",
                    description=f"Official {prefix} archive for {codename or 'this release'}",
                    health={"url": url},
                    degraded=degraded,
                    warnings=[] if codename else ["cannot determine the release codename"],
                    extra={"origin": "apt-official", "suite_type": _suite_type(suite, codename)},
                )
            )
        return out

    def _source_from_entry(self, entry: Entry) -> Source:
        urls = entry.uris if isinstance(entry, Deb822Stanza) else [entry.url]
        suites = entry.suites if isinstance(entry, Deb822Stanza) else [entry.suite]
        url = urls[0] if urls else ""
        suite = suites[0] if suites else ""
        kind = self._classify_kind(url)
        extra: dict[str, Any] = {
            "format": "deb822" if isinstance(entry, Deb822Stanza) else "list",
            "line": entry.line,
            "commented": bool(getattr(entry, "commented", False)),
            "urls": list(urls),
            "suites": list(suites),
            "options": dict(getattr(entry, "options", {}) or {}),
            "signed_by": entry.signed_by,
        }
        if isinstance(entry, Deb822Stanza):
            extra["stanza_index"] = entry.index
        if entry.inline_key:
            extra["inline_key"] = True
        return Source(
            manager="apt",
            name=self._name_for(url, suite, kind),
            url=url,
            suite=suite,
            components=list(entry.components),
            types=list(entry.types),
            architectures=list(entry.architectures),
            enabled=entry.enabled,
            kind=kind,
            scope=Scope.SYSTEM,
            is_default=entry.enabled and self._is_default_candidate(url, suites),
            file=str(entry.file),
            description=f"{entry.describe} ({extra['format']})",
            health={"url": url},
            extra=extra,
        )

    def _name_for(self, url: str, suite: str, kind: SourceKind) -> str:
        host = _host(url)
        if host == "security.ubuntu.com":
            return "Ubuntu security"
        if host.endswith("archive.ubuntu.com") or host == "ports.ubuntu.com":
            return "Ubuntu official"
        if host == "security.debian.org":
            return "Debian security"
        if host in {"deb.debian.org", "ftp.debian.org"}:
            return "Debian official"
        if kind is SourceKind.VENDOR:
            if "launchpad" in host:
                parts = [part for part in urlsplit(url).path.split("/") if part]
                if len(parts) >= 2:
                    return f"PPA {parts[0]}/{parts[1]}"
            if host == "packages.microsoft.com":
                return "Microsoft"
            if host == "download.docker.com":
                return "Docker"
        return host or url or suite or "apt source"

    def _classify_kind(self, url: str) -> SourceKind:
        if not url:
            return SourceKind.UNKNOWN
        scheme = urlsplit(url).scheme.lower()
        if scheme and scheme not in {"http", "https", "ftp"}:
            return SourceKind.CUSTOM if scheme in {"file", "cdrom", "copy"} else SourceKind.UNKNOWN
        host = _host(url)
        if not host:
            return SourceKind.UNKNOWN
        if _is_official_host(host):
            return SourceKind.OFFICIAL
        if host in VENDOR_HOSTS or any(hint in host for hint in VENDOR_HINTS):
            return SourceKind.VENDOR
        if _PRIVATE_RE.match(host):
            return SourceKind.PRIVATE
        if any(hint in host for hint in MIRROR_HINTS):
            return SourceKind.MIRROR
        return SourceKind.CUSTOM

    def _is_default_candidate(self, url: str, suites: Sequence[str]) -> bool:
        if not _is_official_host(_host(url)):
            return False
        codename = self.codename()
        if not codename:
            return True
        return any(suite == codename for suite in suites if suite)

    def _describe(self, path: Path, entry: Entry) -> str:
        return f"{path}:{entry.line}"

    # -------------------------------------------------------------- mutation 1
    def _prepare(self, source: Source) -> Source:
        """Expand templates/``${...}`` and fill in sensible defaults."""
        values = self.template_values()
        extra = dict(source.extra or {})
        raw_url = str(source.url or extra.get("url_template") or "").strip()
        if not raw_url:
            raise AptError("apt: source has no URL")

        # allow a full "deb [opts] url suite components" line as the URL
        if raw_url.startswith(("deb ", "deb-src ")):
            parsed = parse_one_line_entry(raw_url, Path("<spec>"), 1)
            if parsed is None:
                raise AptError(f"apt: cannot parse source line {raw_url!r}")
            source = replace(
                source,
                url=parsed.url,
                suite=parsed.suite,
                components=list(parsed.components),
                types=list(parsed.types),
                architectures=list(source.architectures) or list(parsed.architectures),
            )
            extra["options"] = dict(parsed.options)
            if parsed.signed_by:
                extra.setdefault("signed_by", parsed.signed_by)
            raw_url = parsed.url

        url = helpers.expand_template(raw_url, values)
        unresolved = sorted(set(_TEMPLATE_RE.findall(url)))
        if unresolved:
            raise AptError(f"apt: cannot expand {', '.join(unresolved)} in {raw_url!r}")
        suite = helpers.expand_template(str(source.suite or ""), values).strip()
        components = [helpers.expand_template(str(item), values) for item in (source.components or [])]
        architectures = [helpers.expand_template(str(item), values) for item in (source.architectures or [])]

        if url.lower().startswith("ppa:"):
            spec = url.split(":", 1)[1].strip().strip("/")
            parts = [part for part in spec.split("/") if part]
            if not parts:
                raise AptError("apt: ppa: sources need a 'user/ppa' suffix")
            owner = parts[0]
            archive = parts[1] if len(parts) > 1 else "ppa"
            url = f"https://ppa.launchpadcontent.net/{owner}/{archive}/ubuntu"
            suite = suite or values["codename"]
            components = components or ["main"]

        suite = suite or values["suite"] or values["codename"]
        components = components or self._default_components()
        if "signed_by" in extra:
            extra["signed_by"] = helpers.expand_template(str(extra["signed_by"]), values)
        return replace(
            source,
            url=url,
            suite=suite,
            components=components,
            architectures=architectures,
            extra=extra,
        )

    def _slug(self, source: Source) -> str:
        extra = source.extra or {}
        raw = str(extra.get("slug") or source.provider_key or source.name or "")
        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", _host(source.url)).strip("-")
        return (slug or "srcmgr")[:48]

    def _signed_by(self, source: Source) -> str:
        value = str((source.extra or {}).get("signed_by") or "").strip()
        if value.lower() in {"true", "yes", "1"}:
            return f"/usr/share/keyrings/{self._slug(source)}.gpg"
        return value

    def _render_deb822(self, source: Source) -> str:
        extra = source.extra or {}
        components = list(source.components) or self._default_components()
        types = list(source.types) or ["deb"]
        lines = [f"# Added by source-manager: {source.name or source.url}"]
        lines.append(f"Types: {' '.join(types)}")
        lines.append(f"URIs: {source.url}")
        lines.append(f"Suites: {source.suite}")
        lines.append(f"Components: {' '.join(components)}")
        signed_by = self._signed_by(source)
        if signed_by:
            lines.extend(_field_lines("Signed-By", signed_by))
        if source.architectures:
            lines.append(f"Architectures: {' '.join(source.architectures)}")
        if extra.get("enabled") is False:
            lines.append("Enabled: no")
        return "\n".join(lines) + "\n"

    def _render_oneline(self, source: Source) -> str:
        extra = source.extra or {}
        types = list(source.types) or ["deb"]
        bad = [item for item in types if item not in {"deb", "deb-src"}]
        if bad:
            raise AptError(f"apt: one-line format supports only deb/deb-src, not {', '.join(bad)}")
        options = dict(extra.get("options") or {})
        if source.architectures:
            options["arch"] = ",".join(source.architectures)
        signed_by = self._signed_by(source)
        if signed_by:
            if "\n" in signed_by or signed_by.startswith("-----BEGIN"):
                raise AptError(
                    "apt: one-line format cannot carry an inline PGP key; use format 'deb822'"
                )
            options["signed-by"] = signed_by
        components = list(source.components) or self._default_components()
        disabled = extra.get("enabled") is False
        rendered = []
        for kind in types:
            entry = OneLineEntry(
                file=Path("<new>"),
                line=1,
                raw="",
                types=[kind],
                url=source.url,
                suite=source.suite,
                components=components,
                options=options,
            )
            rendered.append(entry.render(enabled=not disabled))
        return "\n".join(rendered) + "\n"

    def _new_file(self, source: Source) -> tuple[Path, str, str]:
        extra = source.extra or {}
        filename = str(extra.get("filename") or "").strip()
        fmt = str(extra.get("format") or "").strip().lower()
        if filename:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", filename):
                raise AptError(f"apt: refusing unsafe filename {filename!r}")
            if not filename.endswith((".list", ".sources")):
                raise AptError("apt: filename must end with .list or .sources")
            fmt = "list" if filename.endswith(".list") else "deb822"
        elif fmt in {"list", "one-line", "oneline", "sources.list"}:
            filename = f"{self._slug(source)}.list"
            fmt = "list"
        else:
            filename = f"{self._slug(source)}.sources"
            fmt = "deb822"
        path = self.sources_dir() / filename
        self._guard(path)
        block = self._render_oneline(source) if fmt == "list" else self._render_deb822(source)
        return path, block, fmt

    def add(self, source: Source) -> OperationResult:
        """Enable ``source``: reuse an existing (even disabled) entry or write a drop-in."""
        target: Path | None = None
        try:
            prepared = self._prepare(source)
            existing = self._find_installed(prepared)
            if existing is not None:
                path, entry = existing
                where = self._describe(path, entry)
                if entry.enabled:
                    return helpers.ok(
                        f"apt: already enabled ({where})",
                        file=str(path),
                        url=prepared.url,
                        suite=prepared.suite,
                        action="none",
                    )
                result = self._set_entry_enabled(path, entry, True)
                if result.ok:
                    result.message = f"apt: enabled existing entry ({where})"
                    result.details.setdefault("url", prepared.url)
                    result.details.setdefault("suite", prepared.suite)
                return result
            if not prepared.url:
                return helpers.fail("apt: source has no URL")
            target, block, fmt = self._new_file(prepared)
            existed = target.exists()
            text = _append_block(
                helpers.read_text(target), block, blank=fmt == "deb822"
            )
            self._write(target, text)
        except AptError as exc:
            return helpers.fail(str(exc))
        except OSError as exc:
            return helpers.fail(f"apt: cannot write {target or self.sources_dir()}: {exc}")
        return helpers.ok(
            f"apt: added {prepared.name} -> {target}",
            changed=[str(target)],
            file=str(target),
            url=prepared.url,
            suite=prepared.suite,
            components=list(prepared.components),
            format=fmt,
            action="append" if existed else "create",
            existed_before=existed,
        )

    def remove(self, source: Source) -> OperationResult:
        """Remove exactly one entry (a line, a stanza or a standalone drop-in file)."""
        try:
            found = self._find_installed(source)
            if found is None:
                return helpers.fail(self._not_installed(source))
            path, entry = found
            where = self._describe(path, entry)
            changed, notes = self._strip_entry(path, entry)
        except AptError as exc:
            return helpers.fail(str(exc))
        except OSError as exc:
            return helpers.fail(f"apt: cannot remove {source.name or source.url}: {exc}")
        return helpers.ok(
            f"apt: removed {source.name or source.url} ({where}: {'; '.join(notes)})",
            changed=changed,
            file=str(path),
            action="delete" if str(path) in changed and not Path(path).exists() else "rewrite",
            notes=notes,
        )

    def enable(self, source: Source) -> OperationResult:
        return self._flip(source, True)

    def disable(self, source: Source) -> OperationResult:
        return self._flip(source, False)

    def _flip(self, source: Source, enabled: bool) -> OperationResult:
        try:
            found = self._find_installed(source)
            if found is None:
                return helpers.fail(self._not_installed(source))
            path, entry = found
            where = self._describe(path, entry)
            if entry.enabled == enabled:
                return helpers.ok(
                    f"apt: already {'enabled' if enabled else 'disabled'} ({where})",
                    file=str(path),
                    action="none",
                )
            return self._set_entry_enabled(path, entry, enabled)
        except AptError as exc:
            return helpers.fail(str(exc))
        except OSError as exc:
            return helpers.fail(f"apt: cannot {'enable' if enabled else 'disable'} {source.name}: {exc}")

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """Rewrite only the targeted entry, preserving the rest of the file verbatim."""
        try:
            wanted = self._normalize_changes(changes)
            if not wanted:
                return helpers.fail(
                    "apt: nothing to update (supported keys: url, suite, components, "
                    "types, architectures, enabled, signed_by)"
                )
            found = self._find_installed(source)
            if found is None:
                return helpers.fail(self._not_installed(source))
            path, entry = found
            where = self._describe(path, entry)
            lines = helpers.read_text(path).splitlines()
            if isinstance(entry, Deb822Stanza):
                block = list(entry.lines)
                if "url" in wanted:
                    block = _set_field(block, "URIs", " ".join(wanted["url"]))
                if "suites" in wanted:
                    block = _set_field(block, "Suites", " ".join(wanted["suites"]))
                if "components" in wanted:
                    block = _set_field(block, "Components", " ".join(wanted["components"]))
                if "types" in wanted:
                    block = _set_field(block, "Types", " ".join(wanted["types"]))
                if "architectures" in wanted:
                    block = (
                        _set_field(block, "Architectures", " ".join(wanted["architectures"]))
                        if wanted["architectures"]
                        else _remove_field(block, "architectures")
                    )
                if "signed_by" in wanted:
                    block = (
                        _set_field(block, "Signed-By", wanted["signed_by"])
                        if wanted["signed_by"]
                        else _remove_field(block, "signed-by")
                    )
                if "enabled" in wanted:
                    block = (
                        _remove_field(block, "enabled")
                        if wanted["enabled"]
                        else _set_field(block, "Enabled", "no")
                    )
                if block == entry.lines:
                    return helpers.ok(f"apt: no changes ({where})", file=str(path), action="none")
                new_lines = lines[: entry.line_start] + block + lines[entry.line_end:]
            else:
                if "types" in wanted and len(wanted["types"]) != 1:
                    raise AptError(
                        "apt: a one-line entry carries exactly one type (deb or deb-src); "
                        "use format 'deb822' for multi-type entries"
                    )
                new_raw = entry.render(
                    types=wanted.get("types"),
                    url=wanted["url"][0] if "url" in wanted else None,
                    suite=wanted["suites"][0] if "suites" in wanted else None,
                    components=wanted.get("components"),
                    architectures=wanted.get("architectures") if "architectures" in wanted else None,
                    enabled=wanted.get("enabled"),
                )
                if new_raw == entry.raw:
                    return helpers.ok(f"apt: no changes ({where})", file=str(path), action="none")
                new_lines = lines[: entry.line - 1] + [new_raw] + lines[entry.line:]
            self._write(path, "\n".join(new_lines) + "\n")
        except AptError as exc:
            return helpers.fail(str(exc))
        except OSError as exc:
            return helpers.fail(f"apt: cannot update {source.name or source.url}: {exc}")
        return helpers.ok(
            f"apt: updated {where}",
            changed=[str(path)],
            file=str(path),
            changes=wanted,
            action="rewrite",
        )

    # ------------------------------------------------------ mutation helpers
    def _normalize_changes(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if _first(changes, "url", "uri", "uris") is not None:
            urls = _as_list(_first(changes, "url", "uri", "uris"))
            if not urls:
                raise AptError("apt: 'url' cannot be empty")
            out["url"] = urls
        if _first(changes, "suite", "suites") is not None:
            suites = _as_list(_first(changes, "suite", "suites"))
            if not suites:
                raise AptError("apt: 'suite' cannot be empty")
            out["suites"] = suites
        if changes.get("components") is not None:
            components = _as_list(changes.get("components"))
            if not components:
                raise AptError("apt: 'components' cannot be empty")
            out["components"] = components
        types = _first(changes, "types", "type")
        if types is not None:
            values = _as_list(types)
            if not values:
                raise AptError("apt: 'types' cannot be empty")
            unknown = [item for item in values if item not in {"deb", "deb-src"}]
            if unknown:
                raise AptError(f"apt: unsupported type(s): {', '.join(unknown)}")
            out["types"] = values
        if _first(changes, "architectures", "arch") is not None:
            out["architectures"] = _as_list(_first(changes, "architectures", "arch"))
        if "enabled" in changes:
            out["enabled"] = bool(changes["enabled"])
        if _first(changes, "signed_by", "signed-by") is not None:
            out["signed_by"] = str(_first(changes, "signed_by", "signed-by") or "")
        return out

    def _not_installed(self, source: Source) -> str:
        label = source.name or source.url or source.id or "source"
        return f"apt: {label} is not configured under {self.root}"

    def _find_installed(self, source: Source) -> tuple[Path, Entry] | None:
        """Locate the installed entry matching ``source`` (hints first, then identity)."""
        if source.file:
            hinted = Path(source.file)
            self._guard(hinted)  # raises when a caller points us outside our root
            if hinted.is_file():
                entry = self._entry_in(hinted, source)
                if entry is not None:
                    return hinted, entry
        for path in self.list_files():
            entry = self._entry_in(path, source)
            if entry is not None:
                return path, entry
        return None

    def _entry_in(self, path: Path, source: Source) -> Entry | None:
        entries = self.entries_in(path)
        hint_line = _as_int(source.extra.get("line"))
        hint_index = _as_int(source.extra.get("stanza_index"))
        same_file = bool(source.file) and _norm(str(Path(source.file))) == _norm(str(path))
        if same_file and hint_line is not None:
            for entry in entries:
                if entry.line == hint_line:
                    return entry
        if same_file and hint_index is not None:
            for entry in entries:
                if isinstance(entry, Deb822Stanza) and entry.index == hint_index:
                    return entry
        for entry in entries:
            if self._entry_matches(entry, source):
                return entry
        return None

    def _entry_matches(self, entry: Entry, source: Source) -> bool:
        if not source.url:
            return False
        urls = entry.uris if isinstance(entry, Deb822Stanza) else [entry.url]
        if _norm(source.url) not in {_norm(url) for url in urls}:
            return False
        suites = entry.suites if isinstance(entry, Deb822Stanza) else [entry.suite]
        wanted = (source.suite or "").strip()
        if wanted and any(suite.strip() for suite in suites) and wanted not in suites:
            return False
        if source.types and entry.types and not set(source.types) & set(entry.types):
            return False
        return True

    def _is_dropin(self, path: Path) -> bool:
        return _norm(str(path.parent)) == _norm(str(self.sources_dir()))

    def _strip_entry(self, path: Path, entry: Entry) -> tuple[list[str], list[str]]:
        """Delete ``entry``; drop standalone drop-ins, rewrite shared files."""
        remaining = [item for item in self.entries_in(path) if not _same_entry(item, entry)]
        if not remaining and self._is_dropin(path):
            self._unlink(path)
            return [str(path)], [f"deleted {path}"]
        lines = helpers.read_text(path).splitlines()
        if isinstance(entry, Deb822Stanza):
            start, end = entry.line_start, entry.line_end
        else:
            start, end = entry.line - 1, entry.line
        self._write(path, "\n".join(_cut_lines(lines, start, end)) + "\n")
        return [str(path)], [f"rewrote {path}"]

    def _rewrite_line(self, path: Path, entry: OneLineEntry, new_raw: str) -> OperationResult:
        lines = helpers.read_text(path).splitlines()
        lines[entry.line - 1] = new_raw
        self._write(path, "\n".join(lines) + "\n")
        return helpers.ok(
            f"apt: {'enabled' if not new_raw.lstrip().startswith('#') else 'disabled'} "
            f"{self._describe(path, entry)}",
            changed=[str(path)],
            file=str(path),
            action="rewrite",
        )

    def _replace_block(
        self, path: Path, entry: Deb822Stanza, block: Sequence[str], message: str
    ) -> OperationResult:
        lines = helpers.read_text(path).splitlines()
        new_lines = lines[: entry.line_start] + list(block) + lines[entry.line_end:]
        self._write(path, "\n".join(new_lines) + "\n")
        return helpers.ok(message, changed=[str(path)], file=str(path), action="rewrite")

    def _render_entry(self, entry: Entry, *, enabled: bool) -> str:
        if isinstance(entry, OneLineEntry):
            return entry.render(enabled=enabled) + "\n"
        block = list(entry.lines)
        block = _remove_field(block, "enabled") if enabled else _set_field(block, "Enabled", "no")
        return "\n".join(block) + "\n"

    def _move_entry(self, path: Path, entry: Entry, destination: Path) -> OperationResult:
        where = self._describe(path, entry)
        text = _append_block(
            helpers.read_text(destination) if destination.exists() else "",
            self._render_entry(entry, enabled=True),
            blank=isinstance(entry, Deb822Stanza),
        )
        self._write(destination, text)
        changed, _notes = self._strip_entry(path, entry)
        return helpers.ok(
            f"apt: enabled {where} -> {destination}",
            changed=[str(destination), *changed],
            file=str(destination),
            action="move",
        )

    def _rename_variant(self, path: Path, *, enable: bool) -> OperationResult:
        destination = (
            path.with_name(path.name[: -len(DISABLED_SUFFIX)])
            if enable
            else path.with_name(path.name + DISABLED_SUFFIX)
        )
        self._guard(destination)
        if destination.exists():
            merged = _append_block(
                helpers.read_text(destination), helpers.read_text(path), blank=True
            )
            self._write(destination, merged)
            self._unlink(path)
            return helpers.ok(
                f"apt: {'enabled' if enable else 'disabled'} {path} (merged into {destination})",
                changed=[str(path), str(destination)],
                file=str(destination),
                action="merge",
            )
        self._rename(path, destination)
        return helpers.ok(
            f"apt: {'enabled' if enable else 'disabled'} {path} (renamed to {destination})",
            changed=[str(path), str(destination)],
            file=str(destination),
            action="rename",
        )

    def _set_entry_enabled(self, path: Path, entry: Entry, enabled: bool) -> OperationResult:
        where = self._describe(path, entry)
        single = len(self.entries_in(path)) == 1
        disabled_file = path.name.endswith(DISABLED_SUFFIX)
        if enabled:
            if disabled_file:
                if single and not getattr(entry, "commented", False):
                    return self._rename_variant(path, enable=True)
                return self._move_entry(path, entry, path.with_name(path.name[: -len(DISABLED_SUFFIX)]))
            if isinstance(entry, OneLineEntry):
                if not entry.commented:
                    return helpers.ok(
                        f"apt: already enabled ({where})", file=str(path), action="none"
                    )
                return self._rewrite_line(path, entry, _uncomment(entry.raw))
            if entry.enabled:
                return helpers.ok(f"apt: already enabled ({where})", file=str(path), action="none")
            return self._replace_block(
                path,
                entry,
                _remove_field(list(entry.lines), "enabled"),
                f"apt: enabled {where} (removed 'Enabled' field)",
            )
        if disabled_file:
            return helpers.ok(f"apt: already disabled ({where})", file=str(path), action="none")
        if isinstance(entry, OneLineEntry):
            if entry.commented:
                return helpers.ok(f"apt: already disabled ({where})", file=str(path), action="none")
            if single and self._is_dropin(path):
                return self._rename_variant(path, enable=False)
            return self._rewrite_line(path, entry, _comment(entry.raw))
        if not entry.enabled:
            return helpers.ok(f"apt: already disabled ({where})", file=str(path), action="none")
        if isinstance(entry, Deb822Stanza) and single and self._is_dropin(path):
            # a standalone drop-in is parked by renaming it, exactly like *.list.disabled
            return self._rename_variant(path, enable=False)
        return self._replace_block(
            path,
            entry,
            _set_field(list(entry.lines), "Enabled", "no"),
            f"apt: disabled {where} (set 'Enabled: no')",
        )

    # ------------------------------------------------------------------ health
    def native_test_command(self) -> list[str] | None:
        """``apt-get update`` is only usable as root (it writes to /var/lib/apt)."""
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            return None
        return ["apt-get", "update", "-o", "Acquire::Retries=1"]

    def refresh(self) -> OperationResult:
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            return helpers.ok(
                "apt: skipped: apt-get update needs root",
                command=["apt-get", "update", "-o", "Acquire::Retries=1"],
                skipped=True,
            )
        command = self.native_test_command() or ["apt-get", "update", "-o", "Acquire::Retries=1"]
        try:
            completed = helpers.run(command, timeout=300.0)
        except FileNotFoundError:
            return helpers.fail("apt: apt-get not found")
        except subprocess.TimeoutExpired:
            return helpers.fail("apt: apt-get update timed out", command=command)
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        details = {"command": command, "output": output[-1200:]}
        if completed.returncode == 0:
            return helpers.ok("apt: apt-get update completed", **details)
        return helpers.fail(f"apt: apt-get update failed (exit {completed.returncode})", **details)

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe the archive root *and* the suite's ``Release``/``InRelease`` file."""
        base = (source.url or "").rstrip("/")
        result = TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=source.url,
            checked_at=utc_now(),
            method="apt-dists",
        )
        if not base:
            result.status = "no-url"
            result.error = "source has no URL"
            return result
        if not base.lower().startswith(("http://", "https://")):
            result.ok = True
            result.status = "skipped"
            result.detail = f"non-HTTP source ({base.split(':', 1)[0]}), nothing to probe"
            return result
        try:
            suites = self._probe_suites(source)
            base_probe = probe_url(base, timeout=timeout)
            result.probes.append(base_probe.to_dict())
            releases: list[ProbeResult] = []
            for suite in suites:
                for kind in ("Release", "InRelease"):
                    probe = probe_url(f"{base}/dists/{suite}/{kind}", timeout=timeout)
                    releases.append(probe)
                    result.probes.append(probe.to_dict())
            detail = [
                f"{probe.url[len(base) + 1:] if probe.url.startswith(base + '/') else probe.url}: "
                + (
                    f"{probe.http_status or 200} ({probe.latency_ms:.0f} ms)"
                    if probe.ok and probe.latency_ms is not None
                    else ("200" if probe.ok else probe.status)
                )
                for probe in releases
            ]
            winner = next((probe for probe in releases if probe.ok), None)
            if winner is not None or base_probe.ok:
                chosen = winner or base_probe
                result.ok = True
                result.status = "ok"
                result.http_status = chosen.http_status
                result.latency_ms = chosen.latency_ms
                result.tls_ok = chosen.tls_ok
                result.redirected_to = chosen.redirected_to
                if winner is not None and not base_probe.ok:
                    detail.append(f"base URL {base_probe.status} but a Release file answered")
                elif winner is None:
                    detail.append("no dists/<suite>/Release file found")
                    if not suites:
                        detail = [f"{base}: {base_probe.http_status or base_probe.status}"]
            else:
                result.ok = False
                result.status = base_probe.status
                result.http_status = base_probe.http_status
                result.latency_ms = base_probe.latency_ms
                result.tls_ok = base_probe.tls_ok
                result.error = base_probe.error or "unreachable"
            result.detail = "; ".join(item for item in detail if item)
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            result.ok = False
            result.status = "error"
            result.error = str(exc)
        return result

    def _probe_suites(self, source: Source) -> list[str]:
        out: list[str] = []
        candidates = [source.suite, *_as_list((source.extra or {}).get("suites"))]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text not in out:
                out.append(text)
        return out[:2]


def _dedupe_sources(sources: Iterable[Source]) -> list[Source]:
    """De-duplicate identical entries, preferring the enabled occurrence."""
    index: dict[tuple[Any, ...], int] = {}
    out: list[Source] = []
    for source in sources:
        key = (
            tuple(sorted(source.types)),
            _norm(source.url),
            source.suite,
            tuple(source.components),
        )
        position = index.get(key)
        if position is None:
            index[key] = len(out)
            out.append(source)
        elif source.enabled and not out[position].enabled:
            out[position] = source
    return out


def _suite_type(suite: str, codename: str) -> str:
    if codename and suite == codename:
        return "base"
    if suite.endswith("-security"):
        return "security"
    if suite.endswith("-updates"):
        return "updates"
    if suite.endswith("-backports"):
        return "backports"
    return "other"


__all__ = ["AptManager", "Deb822Stanza", "OneLineEntry"]
