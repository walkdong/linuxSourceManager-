"""Behavioural tests for the npm / yarn / pnpm / pip manager plugins.

Everything runs against a throw-away ``$HOME``: the ``home`` fixture points ``HOME`` at
``tmp_path``, sets the per-plugin ``SRCMGR_*`` overrides into the same tree and hides
every CLI binary, so the plugins must work from the files alone.  The final guard test
proves that no plugin wrote anything outside that temporary directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from srcmgr.core import health, helpers
from srcmgr.core.api import Scope, Source
from srcmgr.core.plugins import PluginLoader, load_manager_class, parse_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGERS = PROJECT_ROOT / "srcmgr" / "bundled" / "managers"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "js_python"
PLUGIN_NAMES = ("npm", "yarn", "pnpm", "pip")

#: The *real* HOME, captured before any test monkeypatches the variable.
REAL_HOME = Path(os.environ.get("HOME", "~")).expanduser()

DEFAULTS = {
    "npm": "https://registry.npmjs.org/",
    "yarn": "https://registry.yarnpkg.com",
    "pnpm": "https://registry.npmjs.org/",
    "pip": "https://pypi.org/simple",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def make_plugin(name: str):
    """Build a plugin exactly like :class:`PluginLoader` does."""
    directory = MANAGERS / name
    manifest = parse_manifest(directory / "plugin.yaml", bundled=True)
    plugin = load_manager_class(directory)()
    plugin.manifest = manifest
    plugin.base_dir = directory
    return plugin


def plugin_module(plugin):
    """The module the plugin class was defined in (for monkeypatching globals)."""
    return sys.modules[type(plugin).__module__]


def top_level_key(line: str, key: str) -> bool:
    """True for an unindented line whose key is ``key`` (case-insensitive)."""
    return line[:1] not in (" ", "\t") and line.strip().lower().startswith(key.lower())


def drop_lines(text: str, predicate) -> list[str]:
    return [line for line in text.splitlines() if not predicate(line)]


def hide_binaries(monkeypatch) -> None:
    monkeypatch.setattr(helpers, "which", lambda program: None)
    monkeypatch.setattr(shutil, "which", lambda program: None)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """Isolated HOME plus the per-plugin path overrides; no CLI binaries in sight."""
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SRCMGR_NPMRC", str(home_dir / ".npmrc"))
    monkeypatch.setenv("SRCMGR_YARNRC", str(home_dir / ".yarnrc"))
    monkeypatch.setenv("SRCMGR_YARNRC_YML", str(home_dir / ".yarnrc.yml"))
    monkeypatch.setenv("SRCMGR_PNPM_RC", str(home_dir / ".npmrc"))
    monkeypatch.setenv("SRCMGR_PIP_CONF", str(home_dir / "pip-override.conf"))
    hide_binaries(monkeypatch)
    return home_dir


@pytest.fixture
def outside_snapshot() -> dict[Path, tuple[bool, int | None, int | None]]:
    """Existence/mtime/size of files the plugins must never touch."""
    candidates = [
        Path("/etc/pip.conf"),
        Path("/etc/npmrc"),
        Path("/etc/yarnrc"),
        Path("/etc/yarnrc.yml"),
        REAL_HOME / ".npmrc",
        REAL_HOME / ".yarnrc",
        REAL_HOME / ".yarnrc.yml",
        REAL_HOME / ".config" / "pip" / "pip.conf",
        REAL_HOME / ".pip" / "pip.conf",
    ]
    snapshot: dict[Path, tuple[bool, int | None, int | None]] = {}
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            snapshot[path] = (False, None, None)
        else:
            snapshot[path] = (True, stat.st_mtime_ns, stat.st_size)
    return snapshot


# ---------------------------------------------------------------------------
# generic behaviour
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", PLUGIN_NAMES)
def test_missing_config_falls_back_to_ecosystem_default(home, monkeypatch, name):
    plugin = make_plugin(name)
    if name == "pip":
        # a real /etc/pip.conf must not leak into the test
        monkeypatch.setattr(plugin, "_system_path", lambda: home / "absent-etc-pip.conf")
    assert plugin.read_value() == DEFAULTS[name]
    current = plugin.current()
    assert current is not None and current.url == DEFAULTS[name] and current.is_default
    assert plugin.list_enabled()


@pytest.mark.parametrize("name", PLUGIN_NAMES)
def test_detect_returns_bool_without_binaries(home, monkeypatch, name):
    plugin = make_plugin(name)
    if name == "pip":
        monkeypatch.setattr(plugin, "_system_path", lambda: home / "absent-etc-pip.conf")
    assert plugin.detect() is False  # no binaries, no config files anywhere
    detail = plugin.detect_detail()
    assert detail["available"] is False
    assert isinstance(detail, dict)
    assert plugin.list_enabled()  # must not raise either


@pytest.mark.parametrize("name", PLUGIN_NAMES)
def test_config_files_are_absolute_and_home_relative(home, name):
    plugin = make_plugin(name)
    files = plugin.config_files()
    assert files
    assert all(path.is_absolute() for path in files)


def test_home_is_read_at_call_time(tmp_path, home, monkeypatch):
    """Nothing may cache HOME at import time."""
    plugin = make_plugin("npm")
    assert plugin._rc_path() == home / ".npmrc"

    other = tmp_path / "other-home"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.delenv("SRCMGR_NPMRC")
    assert plugin._rc_path() == other / ".npmrc"
    assert other / ".npmrc" in plugin.config_files()
    assert home / ".npmrc" not in plugin.config_files()


# ---------------------------------------------------------------------------
# npm
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("npmrc_quoted", "https://registry.npmmirror.com/"),
        ("npmrc_case", "https://registry.example.test/npm/"),
    ],
)
def test_npm_read_value_parses_fixtures(home, fixture, expected):
    plugin = make_plugin("npm")
    (home / ".npmrc").write_text(fixture_text("npm", fixture), encoding="utf-8")
    assert plugin.read_value() == expected

    current = plugin.current()
    assert current.url == expected
    assert current.file == str(home / ".npmrc")
    assert current.extra["source_of_truth"] == "file"
    assert current.extra["key"] == "registry"


def test_npm_project_npmrc_is_only_reported(home, monkeypatch, tmp_path):
    plugin = make_plugin("npm")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".npmrc").write_text("registry=https://project.example.test/\n", encoding="utf-8")
    monkeypatch.chdir(project)
    detail = plugin.detect_detail()
    assert detail["project_npmrc"] == str(project / ".npmrc")
    assert any("never modified" in note for note in detail["notes"])
    # the project file was neither read nor written
    assert plugin.read_value() == DEFAULTS["npm"]
    assert (project / ".npmrc").read_text(encoding="utf-8") == "registry=https://project.example.test/\n"


def test_npm_read_value_cli_fallback(home, monkeypatch):
    plugin = make_plugin("npm")
    (home / ".npmrc").write_text(fixture_text("npm", "npmrc_no_registry"), encoding="utf-8")
    monkeypatch.setattr(
        helpers, "which", lambda program: "/usr/bin/npm" if program == "npm" else None
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "https://registry.corp.example/\n", "")

    monkeypatch.setattr(helpers, "run", fake_run)
    assert plugin.read_value() == "https://registry.corp.example/"
    assert calls == [["npm", "config", "get", "registry"]]
    assert plugin.current().extra["source_of_truth"] == "npm config"

    # a failing CLI (stderr only) must not raise: ecosystem default instead
    monkeypatch.setattr(
        helpers,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            list(command), 1, "", "npm ERR! network failure\n"
        ),
    )
    assert plugin.read_value() == DEFAULTS["npm"]
    assert plugin.current().extra["source_of_truth"] == "default"


def test_npm_round_trip_preserves_unrelated_lines(home):
    plugin = make_plugin("npm")
    rc = home / ".npmrc"
    original = fixture_text("npm", "npmrc_quoted")
    rc.write_text(original, encoding="utf-8")
    unrelated = lambda line: top_level_key(line, "registry")  # noqa: E731

    source = Source(manager="npm", name="ustc", url="https://mirrors.ustc.edu.cn/npm/")
    assert plugin.add(source).ok
    assert "registry=https://mirrors.ustc.edu.cn/npm/" in rc.read_text(encoding="utf-8")
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm/"
    assert drop_lines(rc.read_text(encoding="utf-8"), unrelated) == drop_lines(original, unrelated)

    enabled = plugin.list_enabled()
    assert [item.url for item in enabled] == ["https://mirrors.ustc.edu.cn/npm/"]
    assert enabled[0].is_default and enabled[0].enabled

    assert plugin.disable(enabled[0]).ok
    assert plugin.read_value() == plugin.default_url
    assert plugin.enable(source).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm/"

    assert plugin.update(source, {"url": "https://mirrors.ustc.edu.cn/npm2/"}).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm2/"
    assert drop_lines(rc.read_text(encoding="utf-8"), unrelated) == drop_lines(original, unrelated)

    removed = plugin.remove(
        Source(manager="npm", name="ustc", url="https://mirrors.ustc.edu.cn/npm2/")
    )
    assert removed.ok
    after = rc.read_text(encoding="utf-8")
    assert "registry=" not in after.replace("@myscope:registry=", "")
    assert drop_lines(after, unrelated) == drop_lines(original, unrelated)
    assert "# npm user configuration" in after  # comments survive the clear
    assert "@myscope:registry=https://npm.pkg.github.com/" in after
    assert not list(home.rglob(".*.tmp"))  # atomic writes leave no droppings


def test_npm_remove_deletes_an_emptied_file(home):
    plugin = make_plugin("npm")
    rc = home / ".npmrc"
    rc.write_text("registry=https://registry.example.test/\n", encoding="utf-8")
    assert plugin.remove(Source(manager="npm", name="x", url="https://registry.example.test/")).ok
    assert not rc.exists()


def test_npm_accompanying_keys_are_written_and_redacted(home):
    plugin = make_plugin("npm")
    source = Source(
        manager="npm",
        name="corp",
        url="https://npm.corp.example/",
        extra={"accompanying": {"always-auth": "true", "_authToken": "s3cr3t"}},
    )
    result = plugin.add(source)
    assert result.ok, result.message
    assert "+2 accompanying key(s)" in result.message
    text = (home / ".npmrc").read_text(encoding="utf-8")
    assert "registry=https://npm.corp.example/" in text
    assert "always-auth=true" in text
    assert "_authToken=s3cr3t" in text

    # a token is involved -> the source (and its extra) is marked secret
    assert source.secret_fields == ["extra"]
    assert source.is_secret
    assert source.to_dict(redact=True)["extra"] == "<redacted>"

    # ... and so is the source describing the file that now holds the token
    current = plugin.current()
    assert current.secret_fields == ["extra"]
    assert "_authToken" in current.extra["credentials_configured"]
    assert "s3cr3t" not in str(current.to_dict(redact=True))


def test_npm_native_and_refresh(home):
    plugin = make_plugin("npm")
    assert list(plugin.native_test_command()) == ["npm", "ping"]
    assert plugin.refresh().ok


def test_npm_test_treats_401_and_403_as_reachable(home, monkeypatch):
    plugin = make_plugin("npm")
    source = Source(manager="npm", name="private", url="https://npm.corp.example/")

    monkeypatch.setattr(
        health,
        "probe_url",
        lambda url, **kwargs: health.ProbeResult(
            ok=False, status="http-401", url=url, http_status=401, latency_ms=1.0, method="HEAD"
        ),
    )
    result = plugin.test(source)
    assert result.ok and result.status == "ok"
    assert result.http_status == 401 and "authentication" in result.detail

    monkeypatch.setattr(
        health,
        "probe_url",
        lambda url, **kwargs: health.ProbeResult(
            ok=False, status="http-404", url=url, http_status=404, error="HTTP 404", method="GET"
        ),
    )
    result = plugin.test(source)
    assert not result.ok and result.status == "http-404"

    monkeypatch.setattr(
        health,
        "probe_url",
        lambda url, **kwargs: health.ProbeResult(
            ok=True, status="ok", url=url, http_status=200, latency_ms=2.0, method="HEAD"
        ),
    )
    assert plugin.test(source).ok


# ---------------------------------------------------------------------------
# yarn
# ---------------------------------------------------------------------------
def test_yarn_detects_berry_and_reads_scopes(home):
    plugin = make_plugin("yarn")
    yml = home / ".yarnrc.yml"
    yml.write_text(fixture_text("yarn", "yarnrc.yml"), encoding="utf-8")
    assert plugin.detect() is True
    detail = plugin.detect_detail()
    assert detail["flavour"] == "berry"
    assert detail["files"] == [str(yml)]
    assert plugin.read_value() == "https://registry.npmmirror.com"

    sources = plugin.list_enabled()
    main = sources[0]
    assert main.url == "https://registry.npmmirror.com" and main.is_default
    assert main.extra["flavour"] == "berry" and main.extra["key"] == "npmRegistryServer"
    scoped = {item.extra["scope"]: item.url for item in sources if item.extra.get("scope")}
    assert scoped == {
        "myco": "https://npm.myco.example/",
        "other": "https://npm.other.example",
    }
    assert all(item.extra["read_only"] for item in sources if item.extra.get("scope"))

    blocked = plugin.remove(next(item for item in sources if item.extra.get("scope") == "myco"))
    assert not blocked.ok and "yarnrc.yml" in blocked.message
    assert "read-only" in blocked.details.get("hint", "")


def test_yarn_berry_write_preserves_comments_and_nested_yaml(home):
    plugin = make_plugin("yarn")
    yml = home / ".yarnrc.yml"
    original = fixture_text("yarn", "yarnrc.yml")
    yml.write_text(original, encoding="utf-8")
    unrelated = lambda line: top_level_key(line, "npmRegistryServer")  # noqa: E731

    assert plugin.add(
        Source(manager="yarn", name="ustc", url="https://mirrors.ustc.edu.cn/npm/")
    ).ok
    after = yml.read_text(encoding="utf-8")
    assert 'npmRegistryServer: "https://mirrors.ustc.edu.cn/npm/"' in after
    assert drop_lines(after, unrelated) == drop_lines(original, unrelated)
    for needle in (
        "# Yarn 2+ configuration",
        "yarnPath: .yarn/releases/yarn-4.1.1.cjs",
        "npmScopes:",
        '    npmRegistryServer: "https://npm.myco.example/"',
        "    npmAlwaysAuth: true",
        "unsafeHttpWhitelist:",
        '  - "npm.myco.example"',
        "# trailing comment",
    ):
        assert needle in after, needle
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm/"

    assert plugin.update(
        Source(manager="yarn", name="ustc", url="https://mirrors.ustc.edu.cn/npm/"),
        {"url": "https://mirrors.ustc.edu.cn/npm2/"},
    ).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm2/"
    assert drop_lines(yml.read_text(encoding="utf-8"), unrelated) == drop_lines(original, unrelated)


def test_yarn_berry_appends_when_the_key_is_absent(home):
    plugin = make_plugin("yarn")
    yml = home / ".yarnrc.yml"
    yml.write_text("nodeLinker: node-modules\n", encoding="utf-8")
    assert plugin.add(Source(manager="yarn", name="tuna", url="https://npmmirror.com/")).ok
    assert yml.read_text(encoding="utf-8") == (
        'nodeLinker: node-modules\nnpmRegistryServer: "https://npmmirror.com/"\n'
    )


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("yarnrc_classic", "https://registry.npmmirror.com"),
        ("yarnrc_classic_eq", "https://registry.example.test/"),
        ("yarnrc_classic_dash", "https://dashed.example.test/"),
    ],
)
def test_yarn_classic_reads_both_syntaxes(home, fixture, expected):
    plugin = make_plugin("yarn")
    (home / ".yarnrc").write_text(fixture_text("yarn", fixture), encoding="utf-8")
    assert plugin.read_value() == expected
    detail = plugin.detect_detail()
    assert detail["flavour"] == "classic"
    assert plugin.current().extra["key"] == "registry"


def test_yarn_classic_write_keeps_the_line_style(home):
    plugin = make_plugin("yarn")
    rc = home / ".yarnrc"
    original = fixture_text("yarn", "yarnrc_classic_dash")
    rc.write_text(original, encoding="utf-8")
    unrelated = lambda line: top_level_key(line, "registry") or line.strip().startswith(  # noqa: E731
        "--registry"
    )

    assert plugin.add(Source(manager="yarn", name="ustc", url="https://mirrors.ustc.edu.cn/npm/")).ok
    after = rc.read_text(encoding="utf-8")
    assert '--registry "https://mirrors.ustc.edu.cn/npm/"' in after
    assert drop_lines(after, unrelated) == drop_lines(original, unrelated)

    assert plugin.clear_value() is True
    cleared = rc.read_text(encoding="utf-8")
    assert '--registry "' not in cleared
    assert drop_lines(cleared, unrelated) == drop_lines(original, unrelated)
    assert "yarn-offline-mirror" in cleared


def test_yarn_classic_add_disable_round_trip(home):
    plugin = make_plugin("yarn")
    rc = home / ".yarnrc"
    original = fixture_text("yarn", "yarnrc_classic")
    rc.write_text(original, encoding="utf-8")
    unrelated = lambda line: top_level_key(line, "registry")  # noqa: E731

    source = Source(manager="yarn", name="ustc", url="https://mirrors.ustc.edu.cn/npm/")
    assert plugin.add(source).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm/"
    assert plugin.disable(source).ok
    assert plugin.read_value() == plugin.default_url
    assert plugin.enable(source).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm/"
    assert plugin.remove(source).ok
    assert drop_lines(rc.read_text(encoding="utf-8"), unrelated) == drop_lines(original, unrelated)


def test_yarn_unknown_flavour_prefers_classic_and_berry_for_yarn_two_plus(home, monkeypatch):
    plugin = make_plugin("yarn")
    assert plugin.detect() is False
    assert plugin.detect_detail()["flavour"] == "unknown"
    plugin.write_value("https://registry.example.test/")
    assert (home / ".yarnrc").exists() and not (home / ".yarnrc.yml").exists()

    (home / ".yarnrc").unlink()
    monkeypatch.setattr(
        helpers, "which", lambda program: "/usr/bin/yarn" if program == "yarn" else None
    )
    monkeypatch.setattr(
        helpers,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(list(command), 0, "4.1.1\n", ""),
    )
    fresh = make_plugin("yarn")
    assert fresh.detect() is True
    assert fresh.detect_detail()["flavour"] == "berry"
    fresh.write_value("https://registry.example.test/")
    assert (home / ".yarnrc.yml").exists()
    assert fresh.read_value() == "https://registry.example.test/"


def test_yarn_cli_fallback_for_both_generations(home, monkeypatch):
    plugin = make_plugin("yarn")
    monkeypatch.setattr(
        helpers, "which", lambda program: "/usr/bin/yarn" if program == "yarn" else None
    )
    responses = {"npmRegistryServer": "https://berry.example.test\n", "registry": ""}

    def fake_run(command, **kwargs):
        value = responses.get(command[-1], "")
        return subprocess.CompletedProcess(list(command), 0 if value else 1, value, "")

    monkeypatch.setattr(helpers, "run", fake_run)
    assert plugin.read_value() == "https://berry.example.test"
    assert plugin.current().extra["source_of_truth"] == "yarn config"

    responses["npmRegistryServer"] = ""
    responses["registry"] = "https://classic.example.test\n"
    fresh = make_plugin("yarn")
    assert fresh.read_value() == "https://classic.example.test"
    assert fresh.current().extra["flavour"] == "classic"


def test_yarn_has_no_native_connectivity_command(home):
    plugin = make_plugin("yarn")
    assert plugin.native_test_command() is None
    assert "native-test" not in plugin.supports


# ---------------------------------------------------------------------------
# pnpm
# ---------------------------------------------------------------------------
def test_pnpm_reads_and_flags_the_shared_npmrc(home):
    plugin = make_plugin("pnpm")
    rc = home / ".npmrc"
    rc.write_text(fixture_text("npm", "npmrc_quoted"), encoding="utf-8")
    assert plugin.read_value() == "https://registry.npmmirror.com/"
    assert plugin.detect_detail()["shared_with"] == ["npm"]

    result = plugin.add(Source(manager="pnpm", name="ustc", url="https://mirrors.ustc.edu.cn/npm/"))
    assert result.ok and "(shared with npm)" in result.message
    assert result.details["shared_with"] == ["npm"]
    assert rc.read_text(encoding="utf-8").count(
        "registry=https://mirrors.ustc.edu.cn/npm/"
    ) == 1
    assert plugin.current().extra["shared_with"] == ["npm"]


def test_pnpm_separate_rc_is_isolated_from_npm(home, monkeypatch):
    plugin = make_plugin("pnpm")
    separate = home / "pnpm-rc"
    monkeypatch.setenv("SRCMGR_PNPM_RC", str(separate))
    npmrc = home / ".npmrc"
    original = fixture_text("npm", "npmrc_quoted")
    npmrc.write_text(original, encoding="utf-8")

    assert plugin.detect_detail()["shared_with"] == []
    assert plugin.read_value() == DEFAULTS["pnpm"]  # npm's file is not consulted
    result = plugin.add(Source(manager="pnpm", name="ustc", url="https://mirrors.ustc.edu.cn/npm/"))
    assert result.ok and "(shared with npm)" not in result.message
    assert separate.read_text(encoding="utf-8") == "registry=https://mirrors.ustc.edu.cn/npm/\n"
    assert npmrc.read_text(encoding="utf-8") == original  # untouched


def test_pnpm_cli_fallback_and_native_command(home, monkeypatch):
    plugin = make_plugin("pnpm")
    assert plugin.native_test_command() is None  # pnpm is not installed in this test

    monkeypatch.setattr(
        helpers, "which", lambda program: "/usr/bin/pnpm" if program == "pnpm" else None
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "https://registry.corp.example/\n", "")

    monkeypatch.setattr(helpers, "run", fake_run)
    assert plugin.read_value() == "https://registry.corp.example/"
    assert calls == [["pnpm", "config", "get", "registry"]]
    assert plugin.current().extra["source_of_truth"] == "pnpm config"
    assert list(plugin.native_test_command()) == ["pnpm", "ping"]


def test_pnpm_update_and_remove(home):
    plugin = make_plugin("pnpm")
    rc = home / ".npmrc"
    original = fixture_text("npm", "npmrc_quoted")
    rc.write_text(original, encoding="utf-8")
    unrelated = lambda line: top_level_key(line, "registry")  # noqa: E731

    source = Source(manager="pnpm", name="ustc", url="https://mirrors.ustc.edu.cn/npm/")
    assert plugin.add(source).ok
    assert plugin.update(source, {"url": "https://mirrors.ustc.edu.cn/npm2/"}).ok
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/npm2/"
    assert plugin.remove(
        Source(manager="pnpm", name="ustc", url="https://mirrors.ustc.edu.cn/npm2/")
    ).ok
    assert drop_lines(rc.read_text(encoding="utf-8"), unrelated) == drop_lines(original, unrelated)


# ---------------------------------------------------------------------------
# pip
# ---------------------------------------------------------------------------
def test_pip_reads_index_url_and_extra_indexes(home):
    plugin = make_plugin("pip")
    conf = home / ".config" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(fixture_text("pip", "pip.conf"), encoding="utf-8")

    assert plugin.read_value() == "https://pypi.tuna.tsinghua.edu.cn/simple"
    current = plugin.current()
    assert current.is_default and current.file == str(conf)
    assert current.extra["config_file"] == str(conf)
    assert current.extra["key"] == "index-url"

    listed = plugin.list_enabled()
    assert listed[0].url == "https://pypi.tuna.tsinghua.edu.cn/simple"
    extras = [item for item in listed if item.extra.get("key") == "extra-index-url"]
    assert [item.url for item in extras] == [
        "https://pypi.example.test/simple",
        "https://extra.example.test/simple",
    ]
    assert all(item.enabled and not item.is_default for item in extras)
    assert {item.file for item in extras} == {str(conf)}


def test_pip_config_precedence(home, monkeypatch):
    plugin = make_plugin("pip")
    override = home / "pip-override.conf"
    xdg = home / ".config" / "pip" / "pip.conf"
    legacy = home / ".pip" / "pip.conf"
    xdg.parent.mkdir(parents=True, exist_ok=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)

    legacy.write_text(fixture_text("pip", "pip_legacy.conf"), encoding="utf-8")
    assert plugin.read_value() == "https://legacy.example.test/simple"

    xdg.write_text(fixture_text("pip", "pip.conf"), encoding="utf-8")
    assert plugin.read_value() == "https://pypi.tuna.tsinghua.edu.cn/simple"

    override.write_text("[global]\nindex-url = https://override.example.test/simple\n", encoding="utf-8")
    assert plugin.read_value() == "https://override.example.test/simple"
    assert plugin.current().file == str(override)
    assert plugin._write_target() == override


def test_pip_extra_index_round_trip_is_byte_identical(home):
    plugin = make_plugin("pip")
    conf = home / ".config" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    original = fixture_text("pip", "pip.conf")
    conf.write_text(original, encoding="utf-8")

    extra = Source(
        manager="pip",
        name="corp",
        url="https://pypi.corp.example/simple",
        extra={"key": "extra-index-url"},
    )
    assert plugin.add(extra).ok
    after_add = conf.read_text(encoding="utf-8")
    assert "\n    https://pypi.corp.example/simple\n" in after_add
    assert plugin.read_value() == "https://pypi.tuna.tsinghua.edu.cn/simple"

    added = next(
        item
        for item in plugin.list_enabled()
        if item.url == "https://pypi.corp.example/simple"
    )
    assert added.extra["key"] == "extra-index-url" and not added.is_default

    # disabling an extra index removes exactly that line ...
    assert plugin.disable(added).ok
    assert conf.read_text(encoding="utf-8") == original

    # ... and it can be added and removed again without touching anything else
    assert plugin.enable(added).ok
    assert "https://pypi.corp.example/simple" in conf.read_text(encoding="utf-8")
    assert plugin.remove(added).ok
    assert conf.read_text(encoding="utf-8") == original


def test_pip_update_changes_urls_in_place(home):
    plugin = make_plugin("pip")
    conf = home / ".config" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(fixture_text("pip", "pip.conf"), encoding="utf-8")

    new_index = "https://mirrors.ustc.edu.cn/pypi/simple"
    assert plugin.update(plugin.current(), {"url": new_index}).ok
    assert plugin.read_value() == new_index
    assert f"index-url = {new_index}" in conf.read_text(encoding="utf-8")

    extra = next(
        item for item in plugin.list_enabled() if item.url == "https://pypi.example.test/simple"
    )
    new_extra = "https://pypi2.example.test/simple"
    assert plugin.update(extra, {"url": new_extra}).ok
    assert f"    {new_extra}\n" in conf.read_text(encoding="utf-8")
    assert new_index in conf.read_text(encoding="utf-8")  # primary untouched

    # disabling the primary index falls back to the ecosystem default
    assert plugin.disable(plugin.current()).ok
    assert plugin.read_value() == DEFAULTS["pip"]


def test_pip_http_index_manages_trusted_host(home):
    plugin = make_plugin("pip")
    conf = home / ".config" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    original = fixture_text("pip", "pip.conf")
    conf.write_text(original, encoding="utf-8")

    extra = Source(
        manager="pip",
        name="lab",
        url="http://lab.example.test/simple",
        extra={"key": "extra-index-url"},
    )
    assert plugin.add(extra).ok
    text = conf.read_text(encoding="utf-8")
    assert "\n    http://lab.example.test/simple\n" in text
    assert "\n    lab.example.test\n" in text

    assert plugin.remove(extra).ok
    assert conf.read_text(encoding="utf-8") == original


def test_pip_never_writes_the_system_file(home, monkeypatch):
    plugin = make_plugin("pip")
    system = home / "etc-pip.conf"  # stands in for /etc/pip.conf
    system.write_text(fixture_text("pip", "pip_system.conf"), encoding="utf-8")
    monkeypatch.setattr(plugin, "_system_path", lambda: system)

    assert plugin.read_value() == "https://system.example.test/simple"
    detail = plugin.detect_detail()
    assert detail["system_file"] == str(system)
    assert detail["write_target"] == str(home / "pip-override.conf")  # not root -> user file
    assert plugin.clear_value() is False  # refuses to touch the system file

    before = system.read_text(encoding="utf-8")
    assert plugin.add(
        Source(manager="pip", name="ustc", url="https://mirrors.ustc.edu.cn/pypi/simple")
    ).ok
    assert system.read_text(encoding="utf-8") == before
    assert (home / "pip-override.conf").exists()

    # only root, with the system file as the sole config, may write it
    (home / "pip-override.conf").unlink()
    monkeypatch.setattr(plugin_module(plugin), "is_root", lambda: True)
    assert plugin._write_target() == system


def test_pip_cli_fallback_and_native_command(home, monkeypatch):
    plugin = make_plugin("pip")
    monkeypatch.setattr(plugin, "_system_path", lambda: home / "absent-etc-pip.conf")
    assert list(plugin.native_test_command()) == ["pip", "index", "versions", "pip"]

    monkeypatch.setattr(
        helpers, "which", lambda program: "/usr/bin/pip" if program == "pip" else None
    )
    monkeypatch.setattr(
        helpers,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            list(command), 0, "https://mirrors.ustc.edu.cn/pypi/simple\n", ""
        ),
    )
    assert plugin.read_value() == "https://mirrors.ustc.edu.cn/pypi/simple"
    assert plugin.current().extra["source_of_truth"] == "pip config"


def test_pip_test_accepts_auth_statuses(home, monkeypatch):
    plugin = make_plugin("pip")
    source = Source(manager="pip", name="private", url="https://pypi.corp.example/simple")
    monkeypatch.setattr(
        health,
        "probe_url",
        lambda url, **kwargs: health.ProbeResult(
            ok=False, status="http-403", url=url, http_status=403, latency_ms=1.0
        ),
    )
    result = plugin.test(source)
    assert result.ok and result.status == "ok" and result.http_status == 403


def test_add_without_url_fails_cleanly(home):
    for name in PLUGIN_NAMES:
        plugin = make_plugin(name)
        result = plugin.add(Source(manager=name, name="broken", url=""))
        assert result.ok is False, name
        assert "no URL" in result.message or "index" in result.message


# ---------------------------------------------------------------------------
# integration: the real plugin loader
# ---------------------------------------------------------------------------
def test_plugin_loader_loads_all_four(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("SRCMGR_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("SRCMGR_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SRCMGR_CACHE_HOME", str(tmp_path / "cache"))
    for key in (
        "SRCMGR_NPMRC",
        "SRCMGR_YARNRC",
        "SRCMGR_YARNRC_YML",
        "SRCMGR_PNPM_RC",
        "SRCMGR_PIP_CONF",
    ):
        monkeypatch.delenv(key, raising=False)
    # hide the binaries as well: a real `npm config get registry` would write to the
    # developer's own npm cache (and the harness exports npm_config_cache), which would
    # make this test neither hermetic nor deterministic.
    hide_binaries(monkeypatch)

    loader = PluginLoader()
    assert loader.user_dir == tmp_path / "cfg" / "plugins"
    loaded = loader.load_managers(only=list(PLUGIN_NAMES))
    assert set(loaded) == set(PLUGIN_NAMES)

    for name, item in sorted(loaded.items()):
        assert item.ok, f"{name}: error={item.error!r} skipped={item.skipped!r}"
        assert item.manifest.bundled is True
        assert item.manifest.scope is Scope.USER
        assert list(item.manifest.supports) == list(item.plugin.supports)
        canonical = {
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
        }
        assert set(item.manifest.supports) <= canonical
        # yarn has no reliable native connectivity command (see its docstring)
        assert ("native-test" in item.manifest.supports) is (name != "yarn")
        assert isinstance(item.plugin.detect(), bool)
        assert item.plugin.list_enabled()  # the ecosystem default is always reported
        assert all(path.is_absolute() for path in item.plugin.config_files())

    # loading and listing must not create a single file inside the temporary HOME
    assert [path for path in home_dir.rglob("*") if path.is_file()] == []


# ---------------------------------------------------------------------------
# guard: nothing may be written outside the temporary HOME
# ---------------------------------------------------------------------------
def test_nothing_is_written_outside_the_temp_home(outside_snapshot, home, tmp_path, monkeypatch):
    # no network in the guard test: the point is where files are written
    monkeypatch.setattr(
        health,
        "probe_url",
        lambda url, **kwargs: health.ProbeResult(
            ok=True, status="ok", url=url, http_status=200, latency_ms=0.1
        ),
    )
    for name in PLUGIN_NAMES:
        plugin = make_plugin(name)
        url = f"https://guard.example.test/{name}/"
        source = Source(manager=name, name="guard", url=url)
        assert plugin.add(source).ok, name
        assert plugin.disable(source).ok, name
        assert plugin.enable(source).ok, name
        assert plugin.remove(Source(manager=name, name="guard", url=url)).ok, name
        plugin.read_value()
        plugin.current()
        plugin.list_enabled()
        plugin.detect()
        plugin.detect_detail()
        plugin.test(source)

    created = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert created, "the plugins should have written something inside the temp HOME"
    for path in created:
        assert path.resolve().is_relative_to(tmp_path.resolve())
    assert not list(tmp_path.rglob(".*.tmp"))  # no half-written leftovers

    for path, (existed, mtime, size) in outside_snapshot.items():
        if existed:
            stat = path.stat()
            assert (stat.st_mtime_ns, stat.st_size) == (mtime, size), f"{path} was modified"
        else:
            assert not path.exists(), f"{path} was created"
