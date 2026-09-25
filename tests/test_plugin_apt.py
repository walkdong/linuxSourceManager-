"""pytest suite for the bundled apt manager plugin.

Isolation contract: the plugin resolves ``/etc/apt`` through ``SRCMGR_APT_ROOT``, which
this module sets **at import time** to a throw-away directory.  Every test that mutates
anything works on a fresh copy of ``tests/fixtures/apt`` below ``tmp_path``; the real
``/etc/apt`` is only ever read by the explicit integration check documented in the task,
never by this module.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "apt"

OS_RELEASE_UBUNTU = """\
PRETTY_NAME="Ubuntu 24.04 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION_CODENAME=noble
UBUNTU_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
"""
OS_RELEASE_DEBIAN = """\
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
NAME="Debian GNU/Linux"
VERSION_ID="13"
VERSION_CODENAME=trixie
ID=debian
ID_LIKE=debian
"""

# ---------------------------------------------------------------------------
# import-time isolation: point the plugin at a temp tree before anything loads
# ---------------------------------------------------------------------------
_IMPORT_TREE = Path(tempfile.mkdtemp(prefix="srcmgr-apt-import-"))
(_IMPORT_TREE / "apt").mkdir(parents=True, exist_ok=True)
(_IMPORT_TREE / "os-release").write_text(OS_RELEASE_UBUNTU, encoding="utf-8")
os.environ["SRCMGR_APT_ROOT"] = str(_IMPORT_TREE / "apt")
os.environ.pop("SRCMGR_APT_OS_RELEASE", None)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from srcmgr.core.api import Scope, Source, SourceKind  # noqa: E402
from srcmgr.core.health import ProbeResult  # noqa: E402
from srcmgr.core.plugins import PluginLoader  # noqa: E402

#: fixture files, for readability in assertions
SOURCES_LIST = "sources.list"
TUNA = "tuna.list"
MULTI = "multi.sources"
NASTY = "nasty.list"
UBUNTU = "ubuntu.sources"
DOCKER_DISABLED = "docker.list.disabled"
EXTRAS_DISABLED = "extras.sources.disabled"

#: expected parse results for the fixture tree
TOTAL_ENTRIES = 18          # after de-duplicating the disabled tuna duplicate
ENABLED_ENTRIES = 11
DISABLED_ENTRIES = 7


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@dataclass
class Apt:
    """A loaded plugin plus the throw-away tree it operates on."""

    plugin: Any
    root: Path
    tmp: Path

    @property
    def dropins(self) -> Path:
        return self.root / "sources.list.d"

    def path(self, name: str) -> Path:
        return self.dropins / name if name != SOURCES_LIST else self.root / SOURCES_LIST

    def read(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8")

    def entries(self, *, enabled: bool | None = None) -> list[Source]:
        items = self.plugin.list_enabled()
        if enabled is None:
            return items
        return [source for source in items if source.enabled is enabled]

    def find(self, **attributes: Any) -> Source:
        """The single entry matching every given attribute (url, suite, file suffix...)."""
        matches = []
        for source in self.plugin.list_enabled():
            if all(_matches(source, key, value) for key, value in attributes.items()):
                matches.append(source)
        assert len(matches) == 1, f"expected exactly one entry for {attributes}, got {matches}"
        return matches[0]


def _matches(source: Source, key: str, value: Any) -> bool:
    if key == "file_ends":
        return source.file.endswith(value)
    if key == "contains":
        return value in source.url
    return getattr(source, key) == value


def load_plugin():
    """Load the bundled apt plugin exactly like the CLI does."""
    loader = PluginLoader(bundled_root=PROJECT_ROOT / "srcmgr" / "bundled")
    loaded = loader.load_managers(only=["apt"])["apt"]
    assert loaded.ok, f"apt plugin failed to load: {loaded.error or loaded.skipped}"
    return loaded.plugin


def plugin_module():
    """The imported ``manager.py`` module (patched by a few tests)."""
    module = sys.modules.get("srcmgr_plugin_apt")
    assert module is not None, "the plugin module has not been imported yet"
    return module


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def stanza_blocks(path: Path) -> list[str]:
    """Raw text of every deb822 stanza in ``path`` (parsed by the plugin itself)."""
    text = path.read_text(encoding="utf-8")
    parsed = plugin_module().parse_deb822_stanzas(text, path)
    return ["\n".join(stanza.lines) for stanza in parsed]


def make_source(url: str, **kwargs: Any) -> Source:
    payload: dict[str, Any] = {"manager": "apt", "scope": Scope.SYSTEM, "url": url}
    payload.update(kwargs)
    payload.setdefault("name", kwargs.get("provider_key") or url)
    return Source(**payload)


@pytest.fixture(autouse=True)
def _isolated_root_guard() -> Iterator[None]:
    """Fail loudly if anything ever points the plugin at the real /etc/apt."""
    root = Path(os.environ["SRCMGR_APT_ROOT"]).resolve()
    assert not str(root).startswith("/etc"), f"tests must not use {root}"
    yield


@pytest.fixture()
def apt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Apt:
    """A plugin instance directed at a private copy of the fixture tree."""
    root = tmp_path / "etc" / "apt"
    shutil.copytree(FIXTURE_ROOT, root)
    (tmp_path / "etc" / "os-release").write_text(OS_RELEASE_UBUNTU, encoding="utf-8")
    monkeypatch.setenv("SRCMGR_APT_ROOT", str(root))
    plugin = load_plugin()
    plugin.root = root
    return Apt(plugin=plugin, root=root, tmp=tmp_path)


# ---------------------------------------------------------------------------
# manifest / detection
# ---------------------------------------------------------------------------
def test_manifest_declares_expected_capabilities() -> None:
    plugin = load_plugin()
    manifest = plugin.manifest
    assert manifest.name == "apt"
    assert manifest.type.value == "manager"
    assert manifest.scope is Scope.SYSTEM
    assert "apt-get" in manifest.requires
    assert any("*" in path for path in manifest.config_paths)
    for operation in (
        "detect", "list", "current", "test", "native-test", "add", "remove",
        "enable", "disable", "update", "backup", "restore", "refresh",
    ):
        assert operation in manifest.supports
        assert operation in plugin.supports


def test_detect_true_on_debian_family(apt: Apt) -> None:
    assert apt.plugin.detect() is True
    detail = apt.plugin.detect_detail()
    assert detail["available"] is True
    assert detail["codename"] == "noble"
    assert detail["distro"] == "ubuntu"
    assert detail["id_like"] == "debian"
    assert detail["root"] == str(apt.root)
    assert any(path.endswith("sources.list") for path in detail["files"])
    assert all(Path(path).is_file() for path in detail["files"])


def test_detect_false_on_non_debian(apt: Apt, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()
    monkeypatch.setattr(module.helpers, "which", lambda _name: None)
    elsewhere = tmp_path / "nowhere"
    elsewhere.mkdir()
    (elsewhere / "os-release").write_text('ID=fedora\nID_LIKE="rhel"\n', encoding="utf-8")
    apt.plugin.root = elsewhere / "apt"
    assert apt.plugin.detect() is False


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_list_enabled_counts_formats_and_dedup(apt: Apt) -> None:
    sources = apt.plugin.list_enabled()
    assert len(sources) == TOTAL_ENTRIES
    assert len([s for s in sources if s.enabled]) == ENABLED_ENTRIES
    assert len([s for s in sources if not s.enabled]) == DISABLED_ENTRIES
    assert {s.extra["format"] for s in sources} == {"list", "deb822"}
    assert all(s.manager == "apt" for s in sources)
    assert all(s.scope is Scope.SYSTEM for s in sources)
    assert all(Path(s.file).is_absolute() for s in sources)
    assert all(Path(s.file).exists() for s in sources)

    # the duplicate inside dupe.list.disabled collapses into the enabled tuna.list line
    duplicates = [
        s for s in sources
        if s.url == "https://mirrors.tuna.tsinghua.edu.cn/debian"
        and s.suite == "trixie"
        and s.types == ["deb"]
    ]
    assert len(duplicates) == 1
    assert duplicates[0].enabled is True
    assert duplicates[0].file.endswith(TUNA)


def test_parse_one_line_sources_list(apt: Apt) -> None:
    entries = [s for s in apt.plugin.list_enabled() if s.file == str(apt.path(SOURCES_LIST))]
    assert len(entries) == 4
    deb, deb_src, parked, vendor = entries
    assert deb.types == ["deb"] and deb.url == "http://deb.debian.org/debian"
    assert deb.suite == "trixie"
    assert deb.components == ["main", "contrib", "non-free", "non-free-firmware"]
    assert deb.kind is SourceKind.OFFICIAL and deb.enabled is True
    assert deb.file == str(apt.root / SOURCES_LIST) and deb.extra["line"] == 4
    assert deb_src.types == ["deb-src"] and deb_src.enabled is True
    # a commented-out entry is reported as disabled so enable() can flip it
    assert parked.enabled is False and parked.extra["commented"] is True
    assert parked.components == ["universe"] and parked.extra["line"] == 8
    # bracket options survive
    assert vendor.architectures == ["amd64"]
    assert vendor.extra["options"]["signed-by"] == "/usr/share/keyrings/vendor.gpg"
    assert vendor.kind is SourceKind.VENDOR


def test_parse_one_line_disabled_file_and_dropin(apt: Apt) -> None:
    docker = apt.find(contains="download.docker.com")
    assert docker.enabled is False
    assert docker.file.endswith(DOCKER_DISABLED)
    assert docker.architectures == ["amd64"]
    assert docker.kind is SourceKind.VENDOR
    assert docker.suite == "trixie" and docker.components == ["stable"]
    assert docker.extra["line"] == 1

    tuna = [s for s in apt.plugin.list_enabled() if s.file.endswith(TUNA)]
    assert len(tuna) == 3
    assert [s.enabled for s in tuna] == [True, True, False]
    assert tuna[2].extra["commented"] is True and tuna[2].suite == "trixie"


def test_parse_nasty_file_tabs_and_spacing(apt: Apt) -> None:
    nasty = {s.url: s for s in apt.plugin.list_enabled() if s.file.endswith(NASTY)}
    assert set(nasty) == {
        "https://mirror.example.net/ubuntu",
        "https://nasty.example.com/apt",
        "https://commented.example.com/apt",
    }
    assert nasty["https://mirror.example.net/ubuntu"].components == ["main", "universe", "multiverse"]
    assert nasty["https://mirror.example.net/ubuntu"].enabled is True
    odd = nasty["https://nasty.example.com/apt"]
    assert odd.architectures == ["amd64", "arm64"]
    assert odd.extra["options"]["signed-by"] == "/usr/share/keyrings/nasty.gpg"
    assert odd.components == ["main"] and odd.enabled is True
    assert nasty["https://commented.example.com/apt"].enabled is False
    assert nasty["https://commented.example.com/apt"].extra["commented"] is True


def test_parse_deb822_multi_stanza_with_inline_key(apt: Apt) -> None:
    path = apt.path(MULTI)
    blocks = stanza_blocks(path)
    assert len(blocks) == 3
    stanzas = [s for s in apt.plugin.list_enabled() if s.file == str(path)]
    assert [s.extra["stanza_index"] for s in stanzas] == [0, 1, 2]

    first, inline, disabled = stanzas
    assert first.extra["suites"] == ["trixie", "trixie-updates"]
    assert first.suite == "trixie" and first.extra["line"] == 3
    assert first.architectures == ["amd64", "arm64"]
    assert first.components == ["main", "contrib"] and first.enabled is True

    assert inline.url == "https://repo.example.net/apt"
    assert inline.types == ["deb", "deb-src"]
    assert inline.enabled is True
    assert inline.extra["inline_key"] is True
    assert inline.extra["signed_by"].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert inline.extra["signed_by"].rstrip().endswith("-----END PGP PUBLIC KEY BLOCK-----")
    assert "\n\n" in inline.extra["signed_by"]          # the "." continuation survived

    # Enabled: no, even when it is the last field of the stanza
    assert disabled.enabled is False and disabled.extra["stanza_index"] == 2
    parsed_disabled = plugin_module().parse_deb822_stanzas(path.read_text(), path)[2]
    assert parsed_disabled.text("enabled").strip().lower() == "no"


def test_disabled_source_files_are_reported_disabled(apt: Apt) -> None:
    for name in (DOCKER_DISABLED, EXTRAS_DISABLED):
        entries = [s for s in apt.plugin.list_enabled() if s.file.endswith(name)]
        assert entries, f"{name} produced no entries"
        assert all(s.enabled is False for s in entries)


def test_sources_files_helpers(apt: Apt) -> None:
    files = apt.plugin.list_files()
    assert files == apt.plugin.sources_files()
    assert files == apt.plugin.list_files()               # deterministic ordering
    assert files[0] == apt.root / SOURCES_LIST
    names = [path.name for path in files]
    assert len(names) == len(set(names))
    for expected in (TUNA, MULTI, NASTY, UBUNTU, DOCKER_DISABLED, EXTRAS_DISABLED):
        assert expected in names
    assert all(path.is_file() for path in files)
    assert all(path.parent == apt.dropins or path == apt.root / SOURCES_LIST for path in files)


def test_config_files_expands_globs_and_lists_canonical_paths(apt: Apt) -> None:
    files = {str(path) for path in apt.plugin.config_files()}
    assert str(apt.root / SOURCES_LIST) in files
    assert str(apt.dropins / UBUNTU) in files
    assert str(apt.dropins / DOCKER_DISABLED) in files
    # canonical drop-ins are listed even though they do not exist yet
    assert str(apt.dropins / "debian.sources") in files
    assert not (apt.dropins / "debian.sources").exists()
    assert all(str(path).startswith(str(apt.root)) for path in map(Path, files))


# ---------------------------------------------------------------------------
# machine metadata
# ---------------------------------------------------------------------------
def test_template_values_keys(apt: Apt) -> None:
    values = apt.plugin.template_values()
    assert set(values) == {"codename", "distro", "id", "id_like", "version_id", "arch", "suite"}
    assert values["codename"] == "noble"
    assert values["suite"] == "noble"
    assert values["distro"] == values["id"] == "ubuntu"
    assert values["id_like"] == "debian"
    assert values["version_id"] == "24.04"
    assert values["arch"] == apt.plugin.architectures()[0] != ""


def test_template_values_follow_os_release(apt: Apt) -> None:
    (apt.tmp / "etc" / "os-release").write_text(OS_RELEASE_DEBIAN, encoding="utf-8")
    values = apt.plugin.template_values()
    assert values["distro"] == "debian" and values["codename"] == "trixie"
    assert values["suite"] == "trixie" and values["version_id"] == "13"
    available = apt.plugin.list_available()
    assert {s.suite for s in available} == {"trixie", "trixie-updates", "trixie-security"}
    assert all("non-free-firmware" in s.components for s in available)
    assert all(s.url.startswith("http://deb.debian.org") or s.url.startswith("http://security.debian.org")
               for s in available)


def test_current_prefers_official_suite(apt: Apt) -> None:
    current = apt.plugin.current()
    assert current is not None
    assert current.url == "http://archive.ubuntu.com/ubuntu"
    assert current.suite == "noble"
    assert current.kind is SourceKind.OFFICIAL
    assert current.is_default is True
    assert current.scope is Scope.SYSTEM


def test_list_available_returns_official_entries(apt: Apt) -> None:
    available = apt.plugin.list_available()
    assert {s.name for s in available} == {
        "Ubuntu official", "Ubuntu updates", "Ubuntu backports", "Ubuntu security",
    }
    assert all(s.manager == "apt" for s in available)
    assert all(s.kind is SourceKind.OFFICIAL for s in available)
    assert all(s.scope is Scope.SYSTEM for s in available)
    assert {s.suite for s in available} == {
        "noble", "noble-updates", "noble-backports", "noble-security",
    }
    assert all(s.url.startswith(("http://archive.ubuntu.com", "http://security.ubuntu.com"))
               for s in available)
    # the fixture's ubuntu.sources stanzas are already configured, so they are defaults
    defaults = {s.suite for s in available if s.is_default}
    assert defaults == {"noble", "noble-updates", "noble-backports", "noble-security"}
    assert all(s.provider_key for s in available)


def test_list_available_without_official_config_marks_base_default(apt: Apt) -> None:
    (apt.dropins / UBUNTU).unlink()
    available = apt.plugin.list_available()
    defaults = [s for s in available if s.is_default]
    assert [s.suite for s in defaults] == ["noble"]


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
def test_add_writes_deb822_and_is_idempotent(apt: Apt) -> None:
    plugin = apt.plugin
    source = make_source(
        "https://mirrors.example.com/debian", provider_key="example-mirror", name="Example mirror"
    )
    before = snapshot_tree(apt.root)

    result = plugin.add(source)
    assert result.ok
    assert result.details["action"] == "create"
    target = apt.dropins / "example-mirror.sources"
    assert result.changed == [str(target)]
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "Types: deb" in text
    assert "URIs: https://mirrors.example.com/debian" in text
    assert "Suites: noble" in text                       # codename filled in
    assert "Components: main restricted universe multiverse" in text
    assert "Signed-By" not in text                       # only when requested

    after_first = snapshot_tree(apt.root)
    second = plugin.add(source)
    assert second.ok and second.details["action"] == "none"
    assert "already enabled" in second.message
    assert snapshot_tree(apt.root) == after_first        # nothing was rewritten

    # unrelated fixture files were never touched by either call
    assert before[SOURCES_LIST] == (apt.root / SOURCES_LIST).read_bytes()
    assert before[f"sources.list.d/{MULTI}"] == (apt.dropins / MULTI).read_bytes()
    assert before[f"sources.list.d/{TUNA}"] == (apt.dropins / TUNA).read_bytes()

    removed = plugin.remove(source)
    assert removed.ok and str(target) in removed.changed
    assert not target.exists()


def test_add_signed_by_architectures_and_one_line_format(apt: Apt) -> None:
    plugin = apt.plugin
    signed = make_source(
        "https://repo.example.com/apt",
        provider_key="repo",
        name="Repo",
        suite="stable",
        components=["main"],
        architectures=["amd64", "arm64"],
        extra={"signed_by": True},
    )
    assert plugin.add(signed).ok
    text = apt.read("repo.sources")
    assert "Signed-By: /usr/share/keyrings/repo.gpg" in text
    assert "Architectures: amd64 arm64" in text

    one_line = make_source(
        "https://mirrors.ustc.edu.cn/debian-ports",
        provider_key="ustc-ports",
        name="USTC ports",
        suite="trixie",
        components=["main"],
        types=["deb", "deb-src"],
        extra={"format": "list"},
    )
    result = plugin.add(one_line)
    assert result.ok and result.details["format"] == "list"
    assert apt.read("ustc-ports.list").splitlines() == [
        "deb https://mirrors.ustc.edu.cn/debian-ports trixie main",
        "deb-src https://mirrors.ustc.edu.cn/debian-ports trixie main",
    ]

    inline = make_source(
        "https://repo.example.com/inline",
        provider_key="inline",
        name="Inline key",
        suite="stable",
        components=["main"],
        extra={"format": "list", "signed_by": "-----BEGIN PGP PUBLIC KEY BLOCK-----"},
    )
    failed = plugin.add(inline)
    assert failed.ok is False and "inline PGP key" in failed.message


def test_add_expands_provider_url_template(apt: Apt) -> None:
    plugin = apt.plugin
    templated = make_source(
        "https://mirrors.tuna.tsinghua.edu.cn/${distro}/",
        provider_key="tuna",
        name="TUNA",
        extra={"url_template": "https://mirrors.tuna.tsinghua.edu.cn/${distro}/"},
    )
    result = plugin.add(templated)
    assert result.ok
    assert result.details["url"] == "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/"
    assert result.details["suite"] == "noble"
    assert "URIs: https://mirrors.tuna.tsinghua.edu.cn/ubuntu/" in apt.read("tuna.sources")

    unresolved = make_source("https://example.com/${unknown}/", provider_key="broken", name="Broken")
    failed = plugin.add(unresolved)
    assert failed.ok is False and "cannot expand" in failed.message


def test_add_accepts_deb_line_and_ppa_shorthand(apt: Apt) -> None:
    plugin = apt.plugin
    from_line = make_source(
        "deb [arch=arm64 signed-by=/usr/share/keyrings/x.gpg] https://line.example.com/apt trixie contrib",
        provider_key="from-line",
        name="From line",
    )
    assert plugin.add(from_line).ok
    text = apt.read("from-line.sources")
    assert "URIs: https://line.example.com/apt" in text
    assert "Suites: trixie" in text and "Components: contrib" in text
    assert "Architectures: arm64" in text
    assert "Signed-By: /usr/share/keyrings/x.gpg" in text
    assert apt.find(contains="line.example.com").types == ["deb"]

    ppa = make_source("ppa:deadsnakes/ppa", provider_key="deadsnakes", name="deadsnakes PPA")
    assert plugin.add(ppa).ok
    ppa_text = apt.read("deadsnakes.sources")
    assert "URIs: https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu" in ppa_text
    assert "Suites: noble" in ppa_text and "Components: main" in ppa_text
    assert apt.find(contains="ppa.launchpadcontent.net").kind is SourceKind.VENDOR


def test_add_of_commented_entry_uncomments_instead_of_duplicating(apt: Apt) -> None:
    plugin = apt.plugin
    before = apt.read(TUNA)
    source = make_source(
        "https://mirrors.ustc.edu.cn/debian",
        name="USTC",
        suite="trixie",
        components=["main"],
        types=["deb"],
        file=str(apt.dropins / TUNA),
    )
    result = plugin.add(source)
    assert result.ok and result.details["action"] == "rewrite"
    after = apt.read(TUNA)
    assert after == before.replace(
        "# deb https://mirrors.ustc.edu.cn/debian",
        "deb https://mirrors.ustc.edu.cn/debian",
    )
    assert not any(path.name.startswith("ustc") for path in apt.dropins.iterdir())


def test_add_enables_existing_disabled_entry(apt: Apt) -> None:
    plugin = apt.plugin
    docker = apt.find(contains="download.docker.com")
    result = plugin.add(docker)
    assert result.ok
    assert result.details["action"] == "rename"
    assert (apt.dropins / "docker.list").exists()
    assert not (apt.dropins / DOCKER_DISABLED).exists()
    assert apt.find(contains="download.docker.com").enabled is True


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------
def test_enable_disable_commented_line_is_byte_identical(apt: Apt) -> None:
    plugin = apt.plugin
    original = (apt.root / SOURCES_LIST).read_bytes()
    parked = apt.find(url="http://archive.ubuntu.com/ubuntu", enabled=False)
    assert parked.extra["commented"] is True

    enabled = plugin.enable(parked)
    assert enabled.ok and enabled.changed == [str(apt.root / SOURCES_LIST)]
    text = (apt.root / SOURCES_LIST).read_text(encoding="utf-8")
    assert "\ndeb http://archive.ubuntu.com/ubuntu noble universe\n" in text
    assert "# deb http://archive.ubuntu.com/ubuntu" not in text
    assert plugin.enable(parked).details["action"] == "none"

    disabled = plugin.disable(parked)
    assert disabled.ok
    assert (apt.root / SOURCES_LIST).read_bytes() == original


def test_enable_disable_standalone_disabled_file(apt: Apt) -> None:
    plugin = apt.plugin
    original = (apt.dropins / DOCKER_DISABLED).read_bytes()
    docker = apt.find(contains="download.docker.com")

    enabled = plugin.enable(docker)
    assert enabled.ok and enabled.details["action"] == "rename"
    assert set(enabled.changed) == {str(apt.dropins / DOCKER_DISABLED), str(apt.dropins / "docker.list")}
    assert (apt.dropins / "docker.list").read_bytes() == original
    assert not (apt.dropins / DOCKER_DISABLED).exists()
    refreshed = apt.find(contains="download.docker.com")
    assert refreshed.enabled is True and refreshed.file.endswith("docker.list")

    disabled = plugin.disable(refreshed)
    assert disabled.ok and disabled.details["action"] == "rename"
    assert (apt.dropins / DOCKER_DISABLED).read_bytes() == original
    assert apt.find(contains="download.docker.com").enabled is False
    assert plugin.disable(apt.find(contains="download.docker.com")).details["action"] == "none"


def test_enable_disable_deb822_stanza_uses_enabled_field(apt: Apt) -> None:
    plugin = apt.plugin
    inline_block = stanza_blocks(apt.path(MULTI))[1]
    stanza = apt.find(url="https://disabled.example.net/apt")
    assert stanza.enabled is False

    enabled = plugin.enable(stanza)
    assert enabled.ok and "Enabled" in enabled.message
    assert apt.find(url="https://disabled.example.net/apt").enabled is True
    assert "Enabled" not in apt.read(MULTI)
    assert inline_block in apt.read(MULTI)               # untouched stanza, byte-identical

    disabled = plugin.disable(apt.find(url="https://disabled.example.net/apt"))
    assert disabled.ok and "Enabled: no" in apt.read(MULTI)
    assert apt.find(url="https://disabled.example.net/apt").enabled is False
    assert inline_block in apt.read(MULTI)


def test_disable_single_entry_dropin_renames_file(apt: Apt) -> None:
    plugin = apt.plugin
    (apt.dropins / "single.list").write_text(
        "deb https://single.example.com/apt stable main\n", encoding="utf-8"
    )
    single = apt.find(contains="single.example.com")
    result = plugin.disable(single)
    assert result.ok and result.details["action"] == "rename"
    assert not (apt.dropins / "single.list").exists()
    assert (apt.dropins / "single.list.disabled").exists()
    assert apt.find(contains="single.example.com").enabled is False
    assert plugin.enable(apt.find(contains="single.example.com")).details["action"] == "rename"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------
def test_remove_refuses_to_delete_shared_file(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.dropins / EXTRAS_DISABLED
    blocks = stanza_blocks(path)
    assert len(blocks) == 2
    target = apt.find(url="https://extra-a.example.org/debian")

    result = plugin.remove(target)
    assert result.ok and path.exists()
    remaining = path.read_text(encoding="utf-8")
    assert "extra-a.example.org" not in remaining
    assert blocks[1] in remaining                        # the other stanza survives verbatim
    assert not any(s.url.endswith("extra-a.example.org/debian") for s in plugin.list_enabled())

    last = apt.find(url="https://extra-b.example.org/debian")
    final = plugin.remove(last)
    assert final.ok and str(path) in final.changed
    assert not path.exists()                             # only now may the file go away


def test_remove_line_only_removes_that_line(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.path(NASTY)
    before = path.read_text(encoding="utf-8").splitlines()
    target = apt.find(url="https://nasty.example.com/apt")

    result = plugin.remove(target)
    assert result.ok and result.changed == [str(path)]
    after = path.read_text(encoding="utf-8").splitlines()
    expected = [line for index, line in enumerate(before) if index != 1]
    assert after == expected


def test_remove_single_stanza_dropin_deletes_file(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.dropins / "solo.sources"
    path.write_text(
        "# a hand written note\nTypes: deb\nURIs: https://solo.example.com/apt\n"
        "Suites: stable\nComponents: main\n",
        encoding="utf-8",
    )
    solo = apt.find(contains="solo.example.com")
    assert plugin.remove(solo).ok
    assert not path.exists()


def test_enable_moves_one_stanza_out_of_a_shared_disabled_file(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.dropins / EXTRAS_DISABLED
    target = apt.find(url="https://extra-a.example.org/debian")

    result = plugin.enable(target)
    assert result.ok and result.details["action"] == "move"
    moved = apt.dropins / "extras.sources"
    assert str(moved) in result.changed
    text = moved.read_text(encoding="utf-8")
    assert "extra-a.example.org" in text and "extra-b.example.org" not in text
    assert apt.find(url="https://extra-a.example.org/debian").enabled is True
    # the other stanza stays parked in the .disabled file
    assert path.exists()
    assert apt.entries(enabled=False) and apt.find(url="https://extra-b.example.org/debian").enabled is False
    assert "Enabled: no" not in path.read_text(encoding="utf-8")


def test_enable_disable_standalone_deb822_file(apt: Apt) -> None:
    plugin = apt.plugin
    added = make_source(
        "https://repo.example.com/solo", provider_key="solo", name="Solo", suite="stable", components=["main"]
    )
    assert plugin.add(added).ok
    original = (apt.dropins / "solo.sources").read_bytes()

    disabled = plugin.disable(apt.find(contains="repo.example.com/solo"))
    assert disabled.ok and disabled.details["action"] == "rename"
    assert not (apt.dropins / "solo.sources").exists()
    assert (apt.dropins / "solo.sources.disabled").read_bytes() == original
    assert apt.find(contains="repo.example.com/solo").enabled is False

    enabled = plugin.enable(apt.find(contains="repo.example.com/solo"))
    assert enabled.ok and enabled.details["action"] == "rename"
    assert (apt.dropins / "solo.sources").read_bytes() == original
    assert apt.find(contains="repo.example.com/solo").enabled is True


def test_add_creates_sources_list_d_when_missing(apt: Apt) -> None:
    plugin = apt.plugin
    shutil.rmtree(apt.dropins)
    (apt.root / SOURCES_LIST).unlink()
    assert not apt.dropins.exists()
    assert plugin.list_enabled() == []
    assert plugin.list_files() == []

    result = plugin.add(make_source("https://mirrors.example.com/debian", provider_key="fresh", name="Fresh"))
    assert result.ok and result.details["action"] == "create"
    assert (apt.dropins / "fresh.sources").exists()
    assert [(s.url, s.enabled) for s in apt.entries()] == [
        ("https://mirrors.example.com/debian", True)
    ]


def test_remove_unknown_source_fails_cleanly(apt: Apt) -> None:
    result = apt.plugin.remove(make_source("https://nowhere.example.com/apt", name="nowhere"))
    assert result.ok is False and "not configured" in result.message


def test_full_cycle_keeps_unrelated_content_byte_identical(apt: Apt) -> None:
    """add/disable/enable/update/remove must not disturb anything they do not target."""
    plugin = apt.plugin
    original = {
        str(path.relative_to(FIXTURE_ROOT)): path.read_bytes()
        for path in sorted(FIXTURE_ROOT.rglob("*"))
        if path.is_file()
    }
    inline_blocks = stanza_blocks(apt.path(MULTI))

    # 1. a brand new drop-in: create, no-op add, park, unpark, edit, delete
    added = make_source("https://mirrors.example.org/trixie", provider_key="cycle", name="Cycle")
    assert plugin.add(added).ok
    assert plugin.add(added).details["action"] == "none"
    assert plugin.disable(apt.find(contains="mirrors.example.org/trixie")).ok
    assert plugin.enable(apt.find(contains="mirrors.example.org/trixie")).ok
    changed = plugin.update(
        apt.find(contains="mirrors.example.org/trixie"), {"suite": "stable", "components": ["main"]}
    )
    assert changed.ok and apt.find(contains="mirrors.example.org/trixie").suite == "stable"
    assert plugin.remove(apt.find(contains="mirrors.example.org/trixie")).ok

    # 2. the parked one-line entry in sources.list: uncomment then re-comment
    parked = apt.find(url="http://archive.ubuntu.com/ubuntu", enabled=False)
    assert plugin.enable(parked).ok
    assert plugin.disable(parked).ok

    # 3. the standalone .list.disabled drop-in: rename both ways
    assert plugin.enable(apt.find(contains="download.docker.com")).ok
    assert plugin.disable(apt.find(contains="download.docker.com")).ok

    # 4. a deb822 stanza: Enabled toggling plus a field edit that is then undone
    assert plugin.enable(apt.find(url="https://disabled.example.net/apt")).ok
    assert plugin.disable(apt.find(url="https://disabled.example.net/apt")).ok
    assert plugin.update(apt.find(url="https://mirrors.example.org/debian"), {"components": ["main"]}).ok
    assert plugin.update(
        apt.find(url="https://mirrors.example.org/debian"), {"components": ["main", "contrib"]}
    ).ok

    # 5. one stanza out of a shared .disabled file
    assert plugin.remove(apt.find(url="https://extra-a.example.org/debian")).ok

    after = {
        str(path.relative_to(apt.root)): path.read_bytes()
        for path in sorted(apt.root.rglob("*"))
        if path.is_file()
    }
    # the created drop-in is gone again, everything else is still there
    assert set(after) == set(original)

    # files we deliberately edited keep their exact bytes for the untouched stanzas
    assert after[f"sources.list.d/{MULTI}"] != original[f"sources.list.d/{MULTI}"]
    assert inline_blocks[1].encode() in after[f"sources.list.d/{MULTI}"]
    # the re-enabled/re-disabled stanza keeps every other field line; 'Enabled: no' is
    # appended at the end of the stanza because deb822 field order is not significant
    for line in inline_blocks[2].splitlines():
        if not line.startswith("Enabled:"):
            assert line.encode() in after[f"sources.list.d/{MULTI}"]
    assert b"Enabled: no" in after[f"sources.list.d/{MULTI}"]

    # the shared file lost exactly one stanza (and the blank line after it)
    fixture_lines = original[f"sources.list.d/{EXTRAS_DISABLED}"].decode().splitlines()
    assert after[f"sources.list.d/{EXTRAS_DISABLED}"].decode() == "\n".join(fixture_lines[5:]) + "\n"

    # every file we did not target is byte-identical
    for name, payload in original.items():
        if name in {f"sources.list.d/{MULTI}", f"sources.list.d/{EXTRAS_DISABLED}"}:
            continue
        assert after[name] == payload, f"{name} changed unexpectedly"

    final = [(s.url, s.suite, tuple(s.components), s.enabled) for s in apt.entries()
             if s.file.endswith(MULTI)]
    assert final == [
        ("https://mirrors.example.org/debian", "trixie", ("main", "contrib"), True),
        ("https://repo.example.net/apt", "stable", ("main",), True),
        ("https://disabled.example.net/apt", "testing", ("main",), False),
    ]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------
def test_update_only_changes_target_entry(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.path(MULTI)
    before = path.read_text(encoding="utf-8").splitlines()
    blocks = stanza_blocks(path)

    target = apt.find(url="https://mirrors.example.org/debian")
    result = plugin.update(target, {"suite": "trixie", "components": ["main"]})
    assert result.ok and result.changed == [str(path)]

    after = path.read_text(encoding="utf-8").splitlines()
    diff = list(difflib.unified_diff(before, after, lineterm="", n=0))
    changed = [
        line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert set(changed) == {
        "-Suites: trixie trixie-updates",
        "+Suites: trixie",
        "-Components: main contrib",
        "+Components: main",
    }
    # stanzas 1 (inline PGP key) and 2 (Enabled: no) are untouched byte for byte
    text = path.read_text(encoding="utf-8")
    assert blocks[1] in text and blocks[2] in text
    updated = apt.find(url="https://mirrors.example.org/debian")
    assert updated.suite == "trixie" and updated.components == ["main"]
    assert updated.extra["suites"] == ["trixie"]


def test_update_line_entry_keeps_options_and_file(apt: Apt) -> None:
    plugin = apt.plugin
    path = apt.path(SOURCES_LIST)
    before = path.read_text(encoding="utf-8").splitlines()
    target = apt.find(url="https://packages.microsoft.com/repos/code")

    result = plugin.update(target, {"suite": "stable", "components": ["main", "universe"]})
    assert result.ok
    after = path.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before)
    assert after[9] == (
        "deb [arch=amd64 signed-by=/usr/share/keyrings/vendor.gpg] "
        "https://packages.microsoft.com/repos/code stable main universe"
    )
    assert after[:9] == before[:9]

    url_change = plugin.update(apt.find(contains="packages.microsoft.com"), {"url": "https://packages.microsoft.com/repos/edge"})
    assert url_change.ok
    assert "https://packages.microsoft.com/repos/edge stable main universe" in apt.read(SOURCES_LIST)
    assert "arch=amd64" in apt.read(SOURCES_LIST)


def test_update_enabled_toggle_and_noop(apt: Apt) -> None:
    plugin = apt.plugin
    disabled = apt.find(url="https://disabled.example.net/apt")
    assert plugin.update(disabled, {"enabled": True}).ok
    assert apt.find(url="https://disabled.example.net/apt").enabled is True

    again = apt.find(url="https://disabled.example.net/apt")
    assert plugin.update(again, {"enabled": True}).details["action"] == "none"
    assert plugin.update(again, {"suite": "testing", "components": ["main"]}).details["action"] == "none"
    assert plugin.update(again, {"nonsense": 1}).ok is False


def test_update_validates_input(apt: Apt) -> None:
    plugin = apt.plugin
    target = apt.find(url="https://mirrors.tuna.tsinghua.edu.cn/debian", enabled=True, types=["deb"])
    assert plugin.update(target, {"types": ["rpm"]}).ok is False
    assert plugin.update(target, {"components": []}).ok is False
    assert plugin.update(target, {"suite": "   "}).ok is False
    assert plugin.update(target, {}).ok is False
    two_types = make_source(target.url, name="two", types=["deb", "deb-src"])
    assert plugin.update(two_types, {"types": ["deb", "deb-src"]}).ok is False


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_test_probes_release_files(apt: Apt, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()
    probed: list[str] = []

    def fake_probe(url: str, *, timeout: float = 8.0, **_: Any) -> ProbeResult:
        probed.append(url)
        if url.endswith("/dists/noble/Release"):
            return ProbeResult(ok=True, status="ok", url=url, http_status=200, latency_ms=31.7, tls_ok=True)
        return ProbeResult(ok=False, status="unreachable", url=url, error="no route to host")

    monkeypatch.setattr(module, "probe_url", fake_probe)
    source = apt.find(url="http://archive.ubuntu.com/ubuntu", file_ends=UBUNTU)
    result = apt.plugin.test(source, timeout=1.0)
    assert result.ok is True and result.status == "ok"
    assert result.http_status == 200 and result.latency_ms == 31.7
    assert "dists/noble/Release: 200 (32 ms)" in result.detail
    assert "base URL unreachable but a Release file answered" in result.detail
    assert result.method == "apt-dists" and result.checked_at
    assert "http://archive.ubuntu.com/ubuntu/dists/noble/Release" in probed
    assert len(result.probes) == len(probed)


def test_test_reports_failure_without_raising(apt: Apt, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()

    def fake_probe(url: str, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=False, status="timeout", url=url, error="timed out after 1s")

    monkeypatch.setattr(module, "probe_url", fake_probe)
    live = lambda: apt.find(url="http://archive.ubuntu.com/ubuntu", file_ends=UBUNTU)  # noqa: E731
    result = apt.plugin.test(live(), timeout=1.0)
    assert result.ok is False and result.status == "timeout"
    assert "timed out" in result.error

    def exploding_probe(url: str, **_kwargs: Any) -> ProbeResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "probe_url", exploding_probe)
    assert apt.plugin.test(live()).ok is False


def test_test_skips_non_http_sources(apt: Apt) -> None:
    result = apt.plugin.test(make_source("cdrom:[Debian]/", name="cdrom"))
    assert result.ok is True and result.status == "skipped"
    assert "nothing to probe" in result.detail
    assert apt.plugin.test(make_source("", name="empty")).status == "no-url"


def test_native_test_command_requires_root(apt: Apt, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)
    assert apt.plugin.native_test_command() is None
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    assert apt.plugin.native_test_command() == ["apt-get", "update", "-o", "Acquire::Retries=1"]


def test_refresh_skips_without_root_and_runs_as_root(apt: Apt, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)
    skipped = apt.plugin.refresh()
    assert skipped.ok is True and skipped.details["skipped"] is True
    assert "needs root" in skipped.message

    recorded: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        recorded.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="Hit:1 http://archive.ubuntu.com\n", stderr="")

    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module.helpers, "run", fake_run)
    refreshed = apt.plugin.refresh()
    assert refreshed.ok is True
    assert recorded == [["apt-get", "update", "-o", "Acquire::Retries=1"]]


# ---------------------------------------------------------------------------
# safety: the plugin must never write outside its root
# ---------------------------------------------------------------------------
def test_guard_refuses_paths_outside_root(apt: Apt) -> None:
    plugin = apt.plugin
    outside = apt.tmp / "outside"
    outside.mkdir()
    evil = outside / "sources.list"
    evil.write_text("deb https://evil.example.com/apt stable main\n", encoding="utf-8")
    original = evil.read_bytes()
    source = make_source(
        "https://evil.example.com/apt",
        name="evil",
        suite="stable",
        components=["main"],
        types=["deb"],
        file=str(evil),
    )
    results = [
        plugin.remove(source),
        plugin.enable(source),
        plugin.disable(source),
        plugin.update(source, {"suite": "other"}),
    ]
    for result in results:
        assert result.ok is False
        assert "outside" in result.message
    assert evil.read_bytes() == original
    assert not (outside / "sources.list.disabled").exists()


def test_every_write_stays_inside_root(apt: Apt, monkeypatch: pytest.MonkeyPatch) -> None:
    module = plugin_module()
    written: list[str] = []
    renamed: list[tuple[str, str]] = []
    real_write = module.atomic_write_text
    real_rename = Path.rename

    def spy_write(path: Any, text: str, **kwargs: Any) -> None:
        written.append(str(path))
        real_write(path, text, **kwargs)

    def spy_rename(self: Path, target: Any) -> None:
        renamed.append((str(self), str(target)))
        real_rename(self, target)

    monkeypatch.setattr(module, "atomic_write_text", spy_write)
    monkeypatch.setattr(Path, "rename", spy_rename)

    plugin = apt.plugin
    added = make_source("https://mirrors.example.org/trixie", provider_key="cycle", name="Cycle")
    assert plugin.add(added).ok
    installed = apt.find(contains="mirrors.example.org/trixie")
    assert plugin.disable(installed).ok
    assert plugin.enable(apt.find(contains="mirrors.example.org/trixie")).ok
    assert plugin.update(apt.find(contains="mirrors.example.org/trixie"), {"suite": "stable"}).ok
    assert plugin.disable(apt.find(contains="mirrors.example.org/trixie")).ok
    assert plugin.remove(apt.find(contains="mirrors.example.org/trixie")).ok
    assert plugin.enable(apt.find(contains="download.docker.com")).ok
    assert plugin.disable(apt.find(contains="download.docker.com")).ok

    assert written, "the cycle should have written something"
    inside = str(apt.root.resolve())
    assert all(str(Path(path).resolve()).startswith(inside) for path in written)
    assert renamed, "the cycle should have renamed a .disabled file"
    for source_path, destination in renamed:
        assert str(Path(source_path).resolve()).startswith(inside)
        assert str(Path(destination).resolve()).startswith(inside)
    assert not (apt.tmp / "outside").exists()
