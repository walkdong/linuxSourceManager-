"""Behavioural tests for the cargo/go/git/maven/rubygems/conda manager plugins.

Everything runs against a temporary ``$HOME``: the fixture config files are copied
in, the plugins are loaded through the real :class:`~srcmgr.core.plugins.PluginLoader`
and the assertions check both the parsed model *and* the exact bytes of the files that
must not change.  No test touches the network: ``probe_url`` is replaced by a fake.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `pytest tests/...` without an installed package
    sys.path.insert(0, str(ROOT))

from srcmgr.core import helpers  # noqa: E402
from srcmgr.core.health import ProbeResult  # noqa: E402
from srcmgr.core.helpers import make_source  # noqa: E402
from srcmgr.core.plugins import PluginLoader  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "misc"
MANAGERS = ("cargo", "go", "git", "maven", "rubygems", "conda")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ``$HOME`` with every manager override pointing inside it."""
    root = tmp_path / "home"
    root.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("CARGO_HOME", str(root / ".cargo"))
    monkeypatch.setenv("SRCMGR_CARGO_CONFIG", str(root / ".cargo" / "config.toml"))
    monkeypatch.setenv("SRCMGR_GO_ENV_FILE", str(root / ".config" / "go" / "env"))
    monkeypatch.setenv("SRCMGR_GITCONFIG", str(root / ".gitconfig"))
    monkeypatch.setenv("SRCMGR_MAVEN_SETTINGS", str(root / ".m2" / "settings.xml"))
    monkeypatch.setenv("SRCMGR_GEMRC", str(root / ".gemrc"))
    monkeypatch.setenv("SRCMGR_CONDA_RC", str(root / ".condarc"))
    monkeypatch.setenv("SRCMGR_CONFIG_HOME", str(config / "source-manager"))
    monkeypatch.setenv("SRCMGR_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SRCMGR_CACHE_HOME", str(tmp_path / "cache"))
    for variable in ("GEMRC", "GIT_CONFIG_GLOBAL", "CONDA_PREFIX", "MAVEN_HOME", "M2_HOME", "SRCMGR_PLUGIN_PATH"):
        monkeypatch.delenv(variable, raising=False)
    return root


def install(root: Path, fixture: str, relative: str) -> Path:
    """Copy a fixture into the temp HOME and return the destination path."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def fixture_text(fixture: str) -> str:
    return (FIXTURES / fixture).read_text(encoding="utf-8")


def load(name: str):
    """Load one bundled manager through the real loader."""
    loaded = PluginLoader().load_managers(only=[name])[name]
    assert loaded.ok, f"{name} did not load: {loaded.error or loaded.skipped}"
    assert loaded.plugin is not None
    return loaded.plugin


def source_for(plugin, **kwargs):
    return make_source(plugin.name, **kwargs)


class FakeProbe:
    """Records probe targets and returns a canned HTTP status."""

    def __init__(self, http_status: int = 200) -> None:
        self.http_status = http_status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs) -> ProbeResult:
        self.calls.append((url, kwargs))
        ok = self.http_status in (200, 201, 204, 206, 301, 302, 303, 307, 308, 401, 403, 405)
        return ProbeResult(
            ok=ok,
            status="ok" if ok else f"http-{self.http_status}",
            url=url,
            http_status=self.http_status,
            latency_ms=1.5,
            tls_ok=url.startswith("https://"),
            method="HEAD",
        )


def patch_probe(monkeypatch: pytest.MonkeyPatch, plugin, http_status: int = 200) -> FakeProbe:
    """Replace the ``probe_url`` used by the plugin's module."""
    module = sys.modules[type(plugin).__module__]
    fake = FakeProbe(http_status=http_status)
    monkeypatch.setattr(module, "probe_url", fake)
    return fake


def tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


# ---------------------------------------------------------------------------
# cargo
# ---------------------------------------------------------------------------
class TestCargo:
    def test_parses_active_index_and_registries(self, home):
        install(home, "cargo_config.toml", ".cargo/config.toml")
        plugin = load("cargo")

        sources = plugin.list_enabled()
        assert [s.name for s in sources] == ["ustc", "insecure-corp"]

        active, registry = sources
        assert active.url == "sparse+https://mirrors.ustc.edu.cn/crates.io-index/"
        assert active.is_default is True
        assert active.extra["key"] == "source"
        assert active.extra["table"] == "ustc"
        assert active.extra["sparse"] is True

        assert registry.url == "sparse+https://crates.corp.example/index/"
        assert registry.is_default is False
        assert registry.extra["key"] == "registries"
        assert registry.extra["registries_key"] == "insecure-corp"

        assert plugin.read_value() == active.url
        assert plugin.current().name == "ustc"

    def test_defaults_to_crates_io_without_a_config(self, home):
        plugin = load("cargo")
        sources = plugin.list_enabled()
        assert len(sources) == 1
        assert sources[0].url == "https://index.crates.io/"
        assert sources[0].kind.value == "official"

    def test_add_is_exact_and_idempotent(self, home):
        path = install(home, "cargo_config.toml", ".cargo/config.toml")
        before = path.read_text(encoding="utf-8")
        plugin = load("cargo")
        source = source_for(
            plugin,
            name="TUNA",
            url="sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/",
            provider_key="tuna",
        )

        first = plugin.add(source)
        assert first.ok and "[source.tuna]" in first.message
        expected = before.replace('replace-with = "ustc"', 'replace-with = "tuna"') + (
            '\n[source.tuna]\nregistry = "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"\n'
        )
        assert path.read_text(encoding="utf-8") == expected

        second = plugin.add(source)
        assert second.ok and "already" in second.message
        assert path.read_text(encoding="utf-8") == expected
        assert path.read_text(encoding="utf-8").count("[source.tuna]") == 1

    def test_disable_removes_only_the_managed_table(self, home):
        path = install(home, "cargo_config.toml", ".cargo/config.toml")
        plugin = load("cargo")
        source = plugin.current()
        assert source is not None

        result = plugin.disable(source)
        assert result.ok
        after = path.read_text(encoding="utf-8")

        assert "# Cargo configuration for this workstation -- the comments matter, keep them.\n" in after
        assert "[net]\nretry = 3\n" in after
        assert "[build]\njobs = 4\n" in after
        assert "[registries.insecure-corp]\nindex = \"sparse+https://crates.corp.example/index/\"\n" in after
        assert "replace-with" not in after
        assert "[source.ustc]" not in after
        assert plugin.read_value() == "https://index.crates.io/"

    def test_detect_and_list_survive_missing_binaries(self, home, monkeypatch):
        install(home, "cargo_config.toml", ".cargo/config.toml")
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("cargo")
        assert plugin.detect() is True  # a hand-written config is enough
        detail = plugin.detect_detail()
        assert detail["binary"] == ""
        assert [s.name for s in plugin.list_enabled()] == ["ustc", "insecure-corp"]

    def test_detect_is_false_and_silent_without_config_or_binary(self, home, monkeypatch):
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("cargo")
        assert plugin.detect() is False
        assert plugin.list_enabled()[0].url == "https://index.crates.io/"

    def test_sparse_index_is_probed_at_config_json(self, home, monkeypatch):
        install(home, "cargo_config.toml", ".cargo/config.toml")
        plugin = load("cargo")
        probe = patch_probe(monkeypatch, plugin, http_status=200)

        result = plugin.test(plugin.current())
        assert result.ok and result.status == "ok"
        assert probe.calls[0][0] == "https://mirrors.ustc.edu.cn/crates.io-index/config.json"
        assert "probed" in result.detail

    def test_git_index_is_probed_directly_and_403_is_accepted(self, home, monkeypatch):
        plugin = load("cargo")
        probe = patch_probe(monkeypatch, plugin, http_status=403)
        source = source_for(plugin, name="git-index", url="https://github.com/rust-lang/crates.io-index")

        result = plugin.test(source)
        assert result.ok is True
        assert probe.calls[0][0] == "https://github.com/rust-lang/crates.io-index"
        assert "403" in result.detail

    def test_unprobeable_index_is_reported_not_raised(self, home, monkeypatch):
        plugin = load("cargo")
        patch_probe(monkeypatch, plugin, http_status=200)
        result = plugin.test(source_for(plugin, name="local", url="file:///srv/crates-index"))
        assert result.ok is False
        assert result.status == "unsupported-index"
        assert "cannot be probed" in result.error

    def test_remove_a_registry_entry_leaves_the_active_source_alone(self, home):
        path = install(home, "cargo_config.toml", ".cargo/config.toml")
        plugin = load("cargo")
        registry = plugin.list_enabled()[1]

        assert plugin.remove(registry).ok
        after = path.read_text(encoding="utf-8")
        assert "[registries.insecure-corp]" not in after
        assert '[source.crates-io]\nreplace-with = "ustc"\n' in after
        assert "[net]\nretry = 3\n" in after

        missing = plugin.remove(registry)
        assert missing.ok is False

    def test_list_available_offers_crates_io(self, home):
        plugin = load("cargo")
        available = plugin.list_available()
        assert [s.url for s in available] == ["https://index.crates.io/"]
        assert available[0].kind.value == "official"
        assert all(s.manager == "cargo" for s in available)

    def test_native_test_command(self, home):
        plugin = load("cargo")
        assert plugin.native_test_command() == ["cargo", "search", "serde", "--limit", "1"]


# ---------------------------------------------------------------------------
# go
# ---------------------------------------------------------------------------
class TestGo:
    def test_parses_the_fallback_list_in_order(self, home):
        path = install(home, "go_env", ".config/go/env")
        plugin = load("go")
        assert Path(plugin.env_file()) == path

        sources = plugin.list_enabled()
        assert [s.url for s in sources] == [
            "https://goproxy.cn",
            "https://proxy.golang.org",
            "direct",
        ]
        assert [s.extra["position"] for s in sources] == [0, 1, 2]
        assert [s.extra["separator"] for s in sources] == ["", "|", ","]
        assert [s.is_default for s in sources] == [True, False, False]

        proxy_golang, direct = sources[1], sources[2]
        assert proxy_golang.kind.value == "official"
        assert direct.kind.value == "unknown"
        assert direct.extra["probeable"] is False
        assert direct.health == {"mode": "none"}

    def test_add_sets_proxy_then_direct_and_keeps_other_lines(self, home):
        path = install(home, "go_env", ".config/go/env")
        plugin = load("go")
        source = source_for(plugin, name="TUNA", url="https://mirrors.tuna.tsinghua.edu.cn/goproxy/")

        result = plugin.add(source)
        assert result.ok
        assert path.read_text(encoding="utf-8") == (
            "GOFLAGS=-mod=mod\n"
            "GOPROXY=https://mirrors.tuna.tsinghua.edu.cn/goproxy/,direct\n"
            "GOSUMDB=sum.golang.org\n"
            "GOPRIVATE=*.corp.example\n"
            "GOTOOLCHAIN=local\n"
        )

        again = plugin.add(source)
        assert again.ok and "already" in again.message
        assert path.read_text(encoding="utf-8").count("TUNA") == 0
        assert path.read_text(encoding="utf-8").count("GOPROXY=") == 1

    def test_add_can_skip_direct(self, home):
        path = install(home, "go_env", ".config/go/env")
        plugin = load("go")
        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/goproxy/",
            extra={"no_direct": True},
        )
        assert plugin.add(source).ok
        assert "GOPROXY=https://mirrors.tuna.tsinghua.edu.cn/goproxy/\n" in path.read_text(
            encoding="utf-8"
        )

    def test_off_semantics_are_preserved(self, home):
        path = install(home, "go_env", ".config/go/env")
        path.write_text("GOPROXY=off\nGOFLAGS=-mod=mod\n", encoding="utf-8")
        plugin = load("go")

        result = plugin.add(source_for(plugin, name="TUNA", url="https://goproxy.cn"))
        assert result.ok
        assert path.read_text(encoding="utf-8").startswith("GOPROXY=https://goproxy.cn,direct,off\n")
        assert "GOFLAGS=-mod=mod\n" in path.read_text(encoding="utf-8")

    def test_enable_disable_remove_and_update_keep_order(self, home):
        path = install(home, "go_env", ".config/go/env")
        plugin = load("go")
        middle = plugin.list_enabled()[1]  # https://proxy.golang.org, separator '|'

        assert plugin.disable(middle).ok
        assert plugin.read_value() == "https://goproxy.cn,direct"
        assert "GOSUMDB=sum.golang.org\n" in path.read_text(encoding="utf-8")

        assert plugin.enable(middle).ok
        assert plugin.read_value() == "https://goproxy.cn|https://proxy.golang.org,direct"

        direct = [s for s in plugin.list_enabled() if s.url == "direct"][0]
        assert plugin.remove(direct).ok
        assert plugin.read_value() == "https://goproxy.cn|https://proxy.golang.org"

        first = plugin.list_enabled()[0]
        assert plugin.update(first, {"url": "https://goproxy.io"}).ok
        assert plugin.read_value() == "https://goproxy.io|https://proxy.golang.org"

        missing = plugin.remove(source_for(plugin, name="nope", url="https://nope.example"))
        assert missing.ok is False
        assert plugin.read_value() == "https://goproxy.io|https://proxy.golang.org"

    def test_default_when_unset_and_detect_without_binary(self, home, monkeypatch):
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("go")
        assert plugin.detect() is False
        detail = plugin.detect_detail()
        assert "not installed" in detail["note"]
        assert [s.url for s in plugin.list_enabled()] == ["https://proxy.golang.org", "direct"]

    def test_probe_accepts_404_as_reachable(self, home, monkeypatch):
        install(home, "go_env", ".config/go/env")
        plugin = load("go")
        probe = patch_probe(monkeypatch, plugin, http_status=404)

        result = plugin.test(plugin.list_enabled()[0])
        assert result.ok is True
        assert result.detail.count("404") == 1
        assert probe.calls[0][0] == "https://goproxy.cn/github.com/pkg/errors/@v/list"

    def test_direct_and_off_are_skipped(self, home, monkeypatch):
        install(home, "go_env", ".config/go/env")
        plugin = load("go")
        probe = patch_probe(monkeypatch, plugin, http_status=200)
        direct = [s for s in plugin.list_enabled() if s.url == "direct"][0]

        result = plugin.test(direct)
        assert result.ok is True
        assert result.status == "skipped"
        assert result.method == "none"
        assert "not a proxy" in result.detail
        assert probe.calls == []

    def test_native_test_command(self, home):
        plugin = load("go")
        assert plugin.native_test_command() == [
            "go",
            "list",
            "-m",
            "-versions",
            "github.com/pkg/errors",
        ]

    def test_env_file_resolution_order(self, home, monkeypatch):
        import subprocess

        plugin = load("go")
        assert Path(plugin.env_file()) == home / ".config" / "go" / "env"

        # 2. `go env GOENV` wins over the XDG default ...
        monkeypatch.delenv("SRCMGR_GO_ENV_FILE")
        target = home / "custom" / "goenv"
        monkeypatch.setattr(helpers, "which", lambda _program: "/usr/bin/go")
        monkeypatch.setattr(
            helpers,
            "run",
            lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, f"{target}\n", ""
            ),
        )
        assert Path(plugin.env_file()) == target

        # ... 3./4. and without a usable `go` binary the XDG default is used.
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        assert Path(plugin.env_file()) == Path(os.environ["XDG_CONFIG_HOME"]) / "go" / "env"

    def test_list_available_offers_the_go_defaults(self, home):
        plugin = load("go")
        urls = [s.url for s in plugin.list_available()]
        assert urls == ["https://proxy.golang.org", "direct"]


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
class TestGit:
    def test_parses_insteadof_rules(self, home):
        install(home, "gitconfig", ".gitconfig")
        plugin = load("git")

        sources = plugin.list_enabled()
        assert [s.url for s in sources] == [
            "https://gitclone.com/github.com/",
            "https://ghproxy.com/https://github.com/",
        ]
        first, second = sources
        assert first.is_default is True
        assert first.extra["instead_of"] == ["https://github.com/"]
        assert first.extra["config_key"] == "url.https://gitclone.com/github.com/.insteadof"
        assert second.extra["instead_of"] == ["https://raw.githubusercontent.com/"]
        assert second.extra["push_instead_of"] == ["https://github.com/"]
        assert second.is_default is False
        assert plugin.current().url == first.url

    def test_canonical_host_when_nothing_is_configured(self, home):
        plugin = load("git")
        sources = plugin.list_enabled()
        assert len(sources) == 1
        assert sources[0].url == "https://github.com/"
        assert sources[0].kind.value == "official"
        assert sources[0].is_default is True
        assert sources[0].extra["configured"] is False
        assert plugin.current().url == "https://github.com/"

    def test_add_writes_a_rule_and_keeps_foreign_sections(self, home):
        path = install(home, "gitconfig", ".gitconfig")
        before = path.read_text(encoding="utf-8")
        plugin = load("git")
        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/git/github.com/",
            extra={"instead_of": ["https://github.com/"]},
        )

        result = plugin.add(source)
        assert result.ok
        after = path.read_text(encoding="utf-8")
        assert after.startswith(before)
        assert '[url "https://mirrors.tuna.tsinghua.edu.cn/git/github.com/"]\n\tinsteadOf = https://github.com/\n' in after
        assert "[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n" in after

        assert plugin.add(source).ok
        assert after == path.read_text(encoding="utf-8")
        assert path.read_text(encoding="utf-8").count("mirrors.tuna.tsinghua.edu.cn") == 1

    def test_disable_drops_only_that_rule(self, home):
        path = install(home, "gitconfig", ".gitconfig")
        plugin = load("git")
        first = plugin.list_enabled()[0]

        result = plugin.disable(first)
        assert result.ok
        after = path.read_text(encoding="utf-8")
        assert "gitclone.com" not in after
        assert '[url "https://ghproxy.com/https://github.com/"]\n\tpushInsteadOf = https://github.com/\n\tinsteadOf = https://raw.githubusercontent.com/\n' in after
        assert "[core]\n\teditor = vim\n\tautocrlf = input\n" in after

    def test_update_replaces_the_base_url(self, home):
        path = install(home, "gitconfig", ".gitconfig")
        plugin = load("git")
        first = plugin.list_enabled()[0]

        result = plugin.update(first, {"url": "https://mirror.example.org/github.com/"})
        assert result.ok
        after = path.read_text(encoding="utf-8")
        assert "gitclone.com" not in after
        assert '[url "https://mirror.example.org/github.com/"]\n\tinsteadOf = https://github.com/\n' in after
        assert "[user]\n" in after

    def test_detect_without_binary_still_reads_and_writes_the_file(self, home, monkeypatch):
        path = install(home, "gitconfig", ".gitconfig")
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("git")
        assert plugin.detect() is True
        assert plugin.detect_detail()["write_mode"] == "direct file edit"

        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirror.example.org/github.com/",
            extra={"instead_of": ["https://github.com/"]},
        )
        assert plugin.add(source).ok
        assert '[url "https://mirror.example.org/github.com/"]' in path.read_text(encoding="utf-8")

    def test_probe_and_native_command(self, home, monkeypatch):
        install(home, "gitconfig", ".gitconfig")
        plugin = load("git")
        probe = patch_probe(monkeypatch, plugin, http_status=200)

        result = plugin.test(plugin.list_enabled()[0])
        assert result.ok is True
        assert probe.calls[0][0] == "https://gitclone.com/github.com/"
        assert plugin.native_test_command() == [
            "git",
            "ls-remote",
            "https://github.com/git/git",
            "HEAD",
        ]

    def test_disable_then_enable_restores_the_rule(self, home):
        path = install(home, "gitconfig", ".gitconfig")
        plugin = load("git")
        rule = plugin.list_enabled()[0]

        assert plugin.disable(rule).ok
        assert "gitclone.com" not in path.read_text(encoding="utf-8")

        assert plugin.enable(rule).ok
        after = path.read_text(encoding="utf-8")
        assert '[url "https://gitclone.com/github.com/"]\n\tinsteadOf = https://github.com/\n' in after
        assert "[user]\n\tname = Ada Lovelace\n" in after

    def test_list_available_offers_well_known_hosts(self, home):
        plugin = load("git")
        urls = [s.url for s in plugin.list_available()]
        assert urls[0] == "https://github.com/"
        assert "https://gitclone.com/github.com/" in urls
        assert "https://ghproxy.com/https://github.com/" in urls


# ---------------------------------------------------------------------------
# maven
# ---------------------------------------------------------------------------
class TestMaven:
    def test_parses_mirrors_and_default(self, home):
        install(home, "maven_settings.xml", ".m2/settings.xml")
        plugin = load("maven")

        sources = plugin.list_enabled()
        assert [s.extra["mirror_id"] for s in sources] == ["corp"]
        corp = sources[0]
        assert corp.url == "https://nexus.corp.example/repository/maven-public/"
        assert corp.extra["mirror_of"] == "central"
        assert corp.is_default is True
        assert plugin.current().extra["mirror_id"] == "corp"

    def test_mirror_of_matching(self):
        from srcmgr.bundled.managers.maven.manager import covers_central

        assert covers_central("central") is True
        assert covers_central("*") is True
        assert covers_central("external:*") is True
        assert covers_central("central,!corp") is True
        assert covers_central("*,!central") is False
        assert covers_central("repo1,repo2") is False
        assert covers_central("external:http:*") is True

    def test_add_keeps_servers_block_and_is_idempotent(self, home):
        path = install(home, "maven_settings.xml", ".m2/settings.xml")
        plugin = load("maven")
        servers_block = (
            "  <servers>\n"
            "    <server>\n"
            "      <id>corp-releases</id>\n"
            "      <username>ada</username>\n"
            "      <password>s3cret</password>\n"
            "    </server>\n"
            "  </servers>\n"
        )
        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/maven/",
            provider_key="tuna",
            extra={"mirror_id": "tuna", "mirror_of": "central"},
        )

        result = plugin.add(source)
        assert result.ok and "inserted" in result.message
        after = path.read_text(encoding="utf-8")
        assert servers_block in after
        assert "<!-- Hand-maintained settings; comments and extra sections must survive edits. -->" in after
        assert after.count("<id>tuna</id>") == 1
        assert "<mirrorOf>central</mirrorOf>" in after

        assert plugin.add(source).ok
        assert path.read_text(encoding="utf-8").count("<id>tuna</id>") == 1
        assert servers_block in path.read_text(encoding="utf-8")

    def test_add_replaces_an_existing_mirror_by_id(self, home):
        path = install(home, "maven_settings.xml", ".m2/settings.xml")
        plugin = load("maven")
        replacement = source_for(
            plugin,
            name="Corp Central (new)",
            url="https://nexus2.corp.example/repository/maven-public/",
            extra={"mirror_id": "corp", "mirror_of": "*"},
        )

        result = plugin.add(replacement)
        assert result.ok and "updated" in result.message
        after = path.read_text(encoding="utf-8")
        assert after.count("<id>corp</id>") == 1
        assert "https://nexus2.corp.example/repository/maven-public/" in after
        assert "https://nexus.corp.example/repository/maven-public/" not in after
        assert "<mirrorOf>*</mirrorOf>" in after
        assert "<password>s3cret</password>" in after

    def test_update_and_disable(self, home):
        path = install(home, "maven_settings.xml", ".m2/settings.xml")
        plugin = load("maven")
        corp = plugin.current()

        updated = plugin.update(corp, {"url": "https://mirror.example.org/maven2", "mirror_of": "*"})
        assert updated.ok
        after = path.read_text(encoding="utf-8")
        assert "<url>https://mirror.example.org/maven2</url>" in after
        assert "<mirrorOf>*</mirrorOf>" in after
        assert "<id>corp</id>" in after

        disabled = plugin.disable(corp)
        assert disabled.ok
        after = path.read_text(encoding="utf-8")
        assert "<id>corp</id>" not in after
        assert "<servers>" in after and "<password>s3cret</password>" in after
        assert "<!-- the corporate mirror is mandatory here -->" in after

    def test_skeleton_is_created_on_write_only(self, home):
        plugin = load("maven")
        path = Path(plugin.settings_path())
        assert path.exists() is False

        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/maven/",
            extra={"mirror_id": "tuna", "mirror_of": "central"},
        )
        assert plugin.add(source).ok
        text = path.read_text(encoding="utf-8")
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<mirrors>" in text and text.rstrip().endswith("</settings>")
        assert plugin.list_enabled()[0].extra["mirror_id"] == "tuna"

    def test_test_accepts_404_and_native_command_is_absent(self, home, monkeypatch):
        install(home, "maven_settings.xml", ".m2/settings.xml")
        plugin = load("maven")
        probe = patch_probe(monkeypatch, plugin, http_status=404)

        result = plugin.test(plugin.current())
        assert result.ok is True
        assert probe.calls[0][0].endswith(
            "/org/apache/maven/maven-core/maven-metadata.xml"
        )
        assert "404" in result.detail
        assert plugin.native_test_command() is None

    def test_detect_without_maven_binary(self, home, monkeypatch):
        install(home, "maven_settings.xml", ".m2/settings.xml")
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("maven")
        assert plugin.detect() is True
        assert plugin.detect_detail()["binary"] == ""

    def test_list_available_is_only_maven_central(self, home):
        plugin = load("maven")
        available = plugin.list_available()
        assert [s.url for s in available] == ["https://repo.maven.apache.org/maven2"]
        assert available[0].kind.value == "official"

    def test_update_inserts_missing_tags_with_the_block_indentation(self, home):
        path = install(home, "maven_settings.xml", ".m2/settings.xml")
        path.write_text(
            "<settings>\n  <mirrors>\n    <mirror>\n      <id>corp</id>\n"
            "      <url>https://nexus.corp.example/maven/</url>\n    </mirror>\n"
            "  </mirrors>\n</settings>\n",
            encoding="utf-8",
        )
        plugin = load("maven")

        assert plugin.update(plugin.current(), {"mirror_of": "*", "name": "Corp Central"}).ok
        assert path.read_text(encoding="utf-8") == (
            "<settings>\n  <mirrors>\n    <mirror>\n      <id>corp</id>\n"
            "      <url>https://nexus.corp.example/maven/</url>\n"
            "      <mirrorOf>*</mirrorOf>\n      <name>Corp Central</name>\n"
            "    </mirror>\n  </mirrors>\n</settings>\n"
        )

    def test_self_closing_mirrors_element_is_expanded(self, home):
        path = install(home, "maven_settings.xml", ".m2/settings.xml")
        path.write_text("<settings>\n  <mirrors/>\n</settings>\n", encoding="utf-8")
        plugin = load("maven")
        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/maven/",
            extra={"mirror_id": "tuna", "mirror_of": "central"},
        )

        assert plugin.add(source).ok
        assert path.read_text(encoding="utf-8") == (
            "<settings>\n"
            "  <mirrors>\n"
            "    <mirror>\n"
            "      <id>tuna</id>\n"
            "      <name>TUNA</name>\n"
            "      <url>https://mirrors.tuna.tsinghua.edu.cn/maven/</url>\n"
            "      <mirrorOf>central</mirrorOf>\n"
            "    </mirror>\n"
            "  </mirrors>\n"
            "</settings>\n"
        )


# ---------------------------------------------------------------------------
# rubygems
# ---------------------------------------------------------------------------
class TestRubygems:
    def test_parses_ordered_sources(self, home):
        install(home, "gemrc", ".gemrc")
        plugin = load("rubygems")

        sources = plugin.list_enabled()
        assert [s.url for s in sources] == ["https://rubygems.org/", "https://gems.example.org/"]
        assert [s.is_default for s in sources] == [True, False]
        assert sources[0].kind.value == "official"
        assert sources[1].kind.value == "mirror"
        assert plugin.current().url == "https://rubygems.org/"

    def test_add_appends_and_keeps_unrelated_yaml(self, home):
        path = install(home, "gemrc", ".gemrc")
        plugin = load("rubygems")
        source = source_for(plugin, name="TUNA", url="https://mirrors.tuna.tsinghua.edu.cn/rubygems/")

        assert plugin.add(source).ok
        text = path.read_text(encoding="utf-8")
        assert text == (
            "---\n"
            ":backtrace: false\n"
            ":verbose: true\n"
            ":sources:\n"
            "- https://rubygems.org/\n"
            "- https://gems.example.org/\n"
            "- https://mirrors.tuna.tsinghua.edu.cn/rubygems/\n"
            "gem: --no-document\n"
        )

        assert plugin.add(source).ok
        assert path.read_text(encoding="utf-8") == text
        assert text.count("mirrors.tuna.tsinghua.edu.cn") == 1

    def test_primary_source_is_prepended(self, home):
        path = install(home, "gemrc", ".gemrc")
        plugin = load("rubygems")
        source = source_for(
            plugin,
            name="TUNA",
            url="https://mirrors.tuna.tsinghua.edu.cn/rubygems/",
            extra={"primary": True},
        )

        assert plugin.add(source).ok
        assert plugin.current().url == "https://mirrors.tuna.tsinghua.edu.cn/rubygems/"
        assert path.read_text(encoding="utf-8").index("tuna") < path.read_text(encoding="utf-8").index(
            "rubygems.org"
        )

    def test_remove_disable_enable_and_update(self, home):
        path = install(home, "gemrc", ".gemrc")
        plugin = load("rubygems")
        second = plugin.list_enabled()[1]

        assert plugin.disable(second).ok
        assert "- https://gems.example.org/\n" not in path.read_text(encoding="utf-8")
        assert ":verbose: true\n" in path.read_text(encoding="utf-8")

        assert plugin.enable(second).ok
        assert plugin.list_enabled()[1].url == "https://gems.example.org/"

        assert plugin.update(plugin.list_enabled()[1], {"url": "https://gems2.example.org/"}).ok
        assert [s.url for s in plugin.list_enabled()] == [
            "https://rubygems.org/",
            "https://gems2.example.org/",
        ]
        assert ":backtrace: false\n" in path.read_text(encoding="utf-8")

        assert plugin.remove(plugin.list_enabled()[1]).ok
        assert len(plugin.list_enabled()) == 1
        assert "gem: --no-document\n" in path.read_text(encoding="utf-8")

    def test_flow_style_sources_are_preserved_as_flow(self, home):
        path = install(home, "gemrc", ".gemrc")
        path.write_text("---\n:verbose: false\n:sources: [https://rubygems.org/]\ngem: --no-document\n", encoding="utf-8")
        plugin = load("rubygems")
        assert [s.url for s in plugin.list_enabled()] == ["https://rubygems.org/"]

        source = source_for(plugin, name="TUNA", url="https://mirrors.tuna.tsinghua.edu.cn/rubygems/")
        assert plugin.add(source).ok
        assert path.read_text(encoding="utf-8") == (
            "---\n"
            ":verbose: false\n"
            ":sources: [https://rubygems.org/, https://mirrors.tuna.tsinghua.edu.cn/rubygems/]\n"
            "gem: --no-document\n"
        )

    def test_test_accepts_404_and_without_gem_binary(self, home, monkeypatch):
        install(home, "gemrc", ".gemrc")
        plugin = load("rubygems")
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        assert plugin.detect() is True
        probe = patch_probe(monkeypatch, plugin)  # 200

        result = plugin.test(plugin.current())
        assert result.ok is True
        assert probe.calls[0][0] == "https://rubygems.org/specs.4.8.gz"

        patch_probe(monkeypatch, plugin, http_status=404)
        assert plugin.test(plugin.current()).ok is True

        assert plugin.native_test_command() == ["gem", "search", "--remote", "rake"]


# ---------------------------------------------------------------------------
# conda
# ---------------------------------------------------------------------------
class TestConda:
    def test_parses_channels_and_unrelated_keys(self, home):
        install(home, "condarc", ".condarc")
        plugin = load("conda")

        sources = plugin.list_enabled()
        assert [s.name for s in sources] == ["conda-forge", "defaults"]
        assert [s.is_default for s in sources] == [True, False]
        assert sources[0].url == "https://conda.anaconda.org/conda-forge"
        assert sources[1].url == "https://repo.anaconda.com/pkgs/main"
        assert sources[0].extra["channel_alias"] == "https://conda.anaconda.org"
        assert plugin.current().name == "conda-forge"

        detail = plugin.detect_detail()
        assert detail["ssl_verify"] is True
        assert detail["custom_channels"] == {"bioconda": "https://conda.anaconda.org"}
        assert detail["default_channels"] == [
            "https://repo.anaconda.com/pkgs/main",
            "https://repo.anaconda.com/pkgs/r",
        ]

    def test_add_appends_and_keeps_the_rest_of_the_file(self, home):
        path = install(home, "condarc", ".condarc")
        plugin = load("conda")
        source = source_for(
            plugin,
            name="tuna",
            url="https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge",
            extra={"channel": "tuna-conda-forge"},
        )

        assert plugin.add(source).ok
        text = path.read_text(encoding="utf-8")
        assert text.startswith(
            "# conda configuration for the workstation -- unrelated keys must survive edits.\n"
            "channels:\n"
            "  - conda-forge\n"
            "  - defaults\n"
            "  - tuna-conda-forge\n"
        )
        assert "ssl_verify: true\n" in text
        assert "custom_channels:\n  bioconda: https://conda.anaconda.org\n" in text
        assert "pkgs_dirs:\n  - /opt/conda/pkgs\n" in text

        assert plugin.add(source).ok
        assert path.read_text(encoding="utf-8") == text
        assert text.count("tuna-conda-forge") == 1

    def test_highest_priority_is_prepended(self, home):
        path = install(home, "condarc", ".condarc")
        plugin = load("conda")
        source = source_for(
            plugin,
            name="tuna",
            url="https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge",
            extra={"channel": "tuna", "priority": "highest"},
        )

        assert plugin.add(source).ok
        assert plugin.current().name == "tuna"
        assert path.read_text(encoding="utf-8").splitlines()[2:4] == [
            "  - tuna",
            "  - conda-forge",
        ]

    def test_update_channel_alias_and_url(self, home):
        path = install(home, "condarc", ".condarc")
        plugin = load("conda")
        forge = plugin.list_enabled()[0]

        assert plugin.update(forge, {"channel_alias": "https://mirrors.ustc.edu.cn/anaconda"}).ok
        assert "channel_alias: https://mirrors.ustc.edu.cn/anaconda\n" in path.read_text(encoding="utf-8")
        assert plugin.channel_alias() == "https://mirrors.ustc.edu.cn/anaconda"
        assert "ssl_verify: true\n" in path.read_text(encoding="utf-8")

        assert plugin.update(forge, {"url": "https://mirror.example.org/conda-forge"}).ok
        assert [s.url for s in plugin.list_enabled()] == [
            "https://mirror.example.org/conda-forge",
            "https://repo.anaconda.com/pkgs/main",
        ]

    def test_disable_removes_exactly_one_channel(self, home):
        path = install(home, "condarc", ".condarc")
        plugin = load("conda")
        defaults = plugin.list_enabled()[1]

        assert plugin.disable(defaults).ok
        assert [s.name for s in plugin.list_enabled()] == ["conda-forge"]
        assert "default_channels:\n  - https://repo.anaconda.com/pkgs/main\n" in path.read_text(encoding="utf-8")

        missing = plugin.remove(defaults)
        assert missing.ok is False

    def test_test_probes_repodata_json(self, home, monkeypatch):
        install(home, "condarc", ".condarc")
        plugin = load("conda")
        probe = patch_probe(monkeypatch, plugin, http_status=404)
        subdir = plugin.detect_detail()["platform_subdir"]

        result = plugin.test(plugin.list_enabled()[0])
        assert result.ok is True
        assert probe.calls[0][0] == (
            f"https://conda.anaconda.org/conda-forge/{subdir}/repodata.json"
        )
        assert "404" in result.detail
        assert plugin.native_test_command() == ["conda", "config", "--show", "channels"]

    def test_detect_without_conda_binary(self, home, monkeypatch):
        install(home, "condarc", ".condarc")
        monkeypatch.setattr(helpers, "which", lambda _program: None)
        plugin = load("conda")
        assert plugin.detect() is True
        assert plugin.detect_detail()["binary"] == ""

    def test_list_available_offers_conda_forge_and_defaults(self, home):
        plugin = load("conda")
        available = {s.name: s for s in plugin.list_available()}
        assert available["conda-forge"].url == "https://conda.anaconda.org/conda-forge"
        assert available["defaults"].url == "https://repo.anaconda.com/pkgs/main"
        assert available["defaults"].kind.value == "official"

    def test_channel_names_resolve_through_channel_alias(self, home):
        install(home, "condarc", ".condarc")
        plugin = load("conda")
        assert plugin.channel_url("conda-forge") == "https://conda.anaconda.org/conda-forge"
        assert plugin.channel_url("defaults") == "https://repo.anaconda.com/pkgs/main"
        assert (
            plugin.channel_url("https://mirror.example.org/channel")
            == "https://mirror.example.org/channel"
        )


# ---------------------------------------------------------------------------
# cross-cutting: detection without binaries, isolation, loader
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", MANAGERS)
def test_detect_never_raises_without_binaries(home, monkeypatch, name):
    monkeypatch.setattr(helpers, "which", lambda _program: None)
    plugin = load(name)
    assert plugin.detect() in (True, False)
    detail = plugin.detect_detail()
    assert detail["available"] in (True, False)
    assert isinstance(plugin.list_enabled(), list)
    assert isinstance(plugin.list_available(), list)
    assert plugin.current() is None or plugin.current().manager == name
    assert isinstance(plugin.config_files(), list)


@pytest.mark.parametrize("name", MANAGERS)
def test_test_without_a_url_reports_instead_of_raising(home, monkeypatch, name):
    plugin = load(name)
    probe = patch_probe(monkeypatch, plugin, http_status=200)

    result = plugin.test(source_for(plugin, name="broken", url=""))
    assert result.ok is False
    assert result.error
    assert probe.calls == []  # nothing to probe, so nothing was probed


def test_all_managers_load_and_touch_nothing(home, tmp_path):
    """The loader must load all six plugins and detection must not write anything."""
    for fixture, relative in (
        ("cargo_config.toml", ".cargo/config.toml"),
        ("go_env", ".config/go/env"),
        ("gitconfig", ".gitconfig"),
        ("maven_settings.xml", ".m2/settings.xml"),
        ("gemrc", ".gemrc"),
        ("condarc", ".condarc"),
    ):
        install(home, fixture, relative)

    before = tree(tmp_path)
    loaded = PluginLoader().load_managers(only=list(MANAGERS))
    assert set(loaded) == set(MANAGERS)
    for name in MANAGERS:
        assert loaded[name].ok is True, f"{name}: {loaded[name].error or loaded[name].skipped}"
        plugin = loaded[name].plugin
        assert plugin is not None
        assert plugin.manifest.name == name
        assert plugin.manifest.bundled is True
        assert "detect" in plugin.supports and "list" in plugin.supports
        assert plugin.detect() in (True, False)
        plugin.detect_detail()
        plugin.list_enabled()
        plugin.list_available()
        plugin.current()
        for path in plugin.config_files():
            assert str(path).startswith(str(tmp_path)), f"{name} would touch {path}"
    assert tree(tmp_path) == before


def test_loader_loads_every_manifest_field(home):
    loaded = PluginLoader().load_managers(only=list(MANAGERS))
    expected = {
        "cargo": (["cargo"], "user"),
        "go": (["go"], "user"),
        "git": (["git"], "user"),
        "maven": (["mvn"], "user"),
        "rubygems": (["gem"], "user"),
        "conda": (["conda"], "user"),
    }
    for name, (requires, scope) in expected.items():
        manifest = loaded[name].manifest
        assert manifest.type.value == "manager"
        assert manifest.requires == requires
        assert manifest.scope.value == scope
        assert manifest.config_paths
        assert manifest.supports
