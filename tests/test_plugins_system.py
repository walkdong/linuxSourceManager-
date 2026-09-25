"""Tests for the flatpak, snap and docker manager plugins.

Every test runs against temporary roots, never against the real system:

* ``SRCMGR_FLATPAK_FAKE_DIR`` -> fixture TSVs plus a ``calls.log`` of would-be argv
* ``SRCMGR_SNAP_FAKE_DIR``    -> ``proxy.json`` / ``snapd.conf`` plus ``calls.log``
* ``SRCMGR_DOCKER_ROOT``      -> a temp stand-in for ``/etc/docker``
* ``SRCMGR_{CONFIG,DATA,CACHE}_HOME`` -> temp XDG roots

``test_no_test_writes_outside_temp_root`` re-checks that claim by hashing the real
``/etc/docker/daemon.json`` (and its directory mtimes) before and after a full
mutation sweep, and by asserting the fake ``calls.log`` holds exactly the expected
argv.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from srcmgr.core import helpers
from srcmgr.core.api import Scope, Source, SourceKind
from srcmgr.core.config import Config
from srcmgr.core.health import ProbeResult
from srcmgr.core.plugins import PluginLoader

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "system"
REAL_DAEMON_JSON = Path("/etc/docker/daemon.json")
REAL_DOCKER_DIR = Path("/etc/docker")
REAL_ETC = Path("/etc")

REQUIRED_OPERATIONS = (
    "detect",
    "list_enabled",
    "list_available",
    "add",
    "remove",
    "enable",
    "disable",
    "update",
)


# ---------------------------------------------------------------------------
# sandbox + helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point all three plugins at temp roots so no test can touch the real system."""
    for name in ("SRCMGR_CONFIG_HOME", "SRCMGR_DATA_HOME", "SRCMGR_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.delenv("SRCMGR_PLUGIN_PATH", raising=False)

    roots = {}
    for kind in ("flatpak", "snap", "docker"):
        root = tmp_path / f"fake-{kind}"
        root.mkdir()
        roots[kind] = root
    monkeypatch.setenv("SRCMGR_FLATPAK_FAKE_DIR", str(roots["flatpak"]))
    monkeypatch.setenv("SRCMGR_SNAP_FAKE_DIR", str(roots["snap"]))
    monkeypatch.setenv("SRCMGR_DOCKER_ROOT", str(roots["docker"]))
    return SimpleNamespace(tmp=tmp_path, **roots)


@pytest.fixture
def load(sandbox: SimpleNamespace):
    """Load one bundled manager plugin through the real loader."""

    def _load(name: str):
        loader = PluginLoader(user_dir=sandbox.tmp / "user-plugins", extra_paths=[])
        loaded = loader.load_managers(only=[name])[name]
        assert loaded.ok, f"{name} did not load: {loaded.error or loaded.skipped}"
        assert not loaded.error and not loaded.skipped
        return loaded.plugin

    return _load


def stage(sandbox: SimpleNamespace, kind: str, *names: str) -> Path:
    """Copy fixture files into the temp fake root of one plugin."""
    target = getattr(sandbox, kind)
    for name in names:
        shutil.copy(FIXTURES / kind / name, target / name)
    return target


def fake_calls(root: Path) -> list[str]:
    """argv lines the plugin would have executed (fake mode ``calls.log``)."""
    path = root / "calls.log"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def module_of(plugin: Any) -> Any:
    """The imported ``manager.py`` module of a loaded plugin (for monkeypatching)."""
    return sys.modules[type(plugin).__module__]


def as_root(monkeypatch: pytest.MonkeyPatch, uid: int = 0) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)


def source_for(name: str, url: str, **kwargs: Any) -> Source:
    return Source(manager=name, name=kwargs.pop("label", "test-source"), url=url, **kwargs)


def flatpak_source(sources: list[Source], name: str, installation: str) -> Source:
    for source in sources:
        if source.name == name and source.extra.get("installation") == installation:
            return source
    raise AssertionError(f"no remote {name!r} in the {installation} installation")


def write_docker(sandbox: SimpleNamespace, data: dict[str, Any]) -> Path:
    path = sandbox.docker / "daemon.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# flatpak
# ---------------------------------------------------------------------------
def test_flatpak_lists_both_installations(load, sandbox):
    stage(sandbox, "flatpak", "remotes.tsv", "remotes-user.tsv")
    plugin = load("flatpak")

    sources = plugin.list_enabled()
    assert [s.extra.get("installation") for s in sources] == [
        "system",
        "system",
        "system",
        "user",
        "user",
    ]

    flathub = flatpak_source(sources, "flathub", "system")
    assert flathub.url == "https://dl.flathub.org/repo/"
    assert flathub.scope is Scope.SYSTEM
    assert flathub.kind is SourceKind.OFFICIAL
    assert flathub.enabled is True and flathub.is_default is True
    assert flathub.priority == 1
    assert flathub.file == "/var/lib/flatpak/repo/config"
    assert flathub.extra["url"] == flathub.url
    assert flathub.extra["name"] == "flathub"
    assert flathub.extra["remote_name"] == "flathub"

    mirror = flatpak_source(sources, "ustc-flathub", "system")
    assert mirror.kind is SourceKind.MIRROR
    assert mirror.enabled is True and mirror.priority == 5

    disabled = flatpak_source(sources, "legacy-vendor", "system")
    assert disabled.enabled is False
    assert disabled.extra["disabled"] is True
    assert disabled.priority == 20

    user_flathub = flatpak_source(sources, "flathub", "user")
    assert user_flathub.scope is Scope.USER
    assert user_flathub.url == "https://dl.flathub.org/repo/"
    assert user_flathub.enabled is True
    assert user_flathub.file.endswith(".local/share/flatpak/repo/config")
    assert user_flathub.id != flathub.id, "installations must not share an id"

    kicad = flatpak_source(sources, "kicad", "user")
    assert kicad.scope is Scope.USER and kicad.priority is None and kicad.enabled is True

    current = plugin.current()
    assert current is not None
    assert (current.name, current.extra["installation"]) == ("flathub", "system")
    assert fake_calls(sandbox.flatpak) == [], "reads must not shell out in fake mode"


def test_flatpak_parses_show_details_shape(load, sandbox):
    details = (FIXTURES / "flatpak" / "remotes-show-details.tsv").read_text(encoding="utf-8")
    shutil.copy(FIXTURES / "flatpak" / "remotes-show-details.tsv", sandbox.flatpak / "remotes.tsv")
    plugin = load("flatpak")
    mod = module_of(plugin)

    remotes = mod.parse_remotes_auto(details)
    assert [(r.name, r.url, r.disabled, r.priority) for r in remotes] == [
        ("flathub", "https://dl.flathub.org/repo/", False, 1),
        ("legacy-vendor", "https://flatpak.example.com/repo", True, 20),
        ("ustc-flathub", "https://mirrors.ustc.edu.cn/flathub", False, 5),
    ]
    assert remotes[2].options == ["user", "no-gpg-verify"]

    sources = plugin.list_enabled()
    assert flatpak_source(sources, "legacy-vendor", "system").enabled is False
    assert flatpak_source(sources, "ustc-flathub", "system").priority == 5


def test_flatpak_falls_back_when_columns_unsupported(load, sandbox, monkeypatch):
    monkeypatch.delenv("SRCMGR_FLATPAK_FAKE_DIR")
    plugin = load("flatpak")
    mod = module_of(plugin)
    details = (FIXTURES / "flatpak" / "remotes-show-details.tsv").read_text(encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(argv, *, timeout: float = 20.0):
        calls.append(list(argv))
        if "--columns=" in " ".join(argv):
            return mod.Outcome(returncode=1, stderr="error: Unknown column disabled")
        return mod.Outcome(returncode=0, stdout=details)

    monkeypatch.setattr(plugin, "_run_cli", fake_run)
    sources = plugin.list_enabled()

    system_calls = [call for call in calls if "--system" in call]
    assert system_calls[0] == [
        "flatpak",
        "remotes",
        "--system",
        "--columns=name,url,disabled,prio",
        "--show-disabled",
    ]
    assert system_calls[1] == ["flatpak", "remotes", "--system", "--columns=name,url,disabled,prio"]
    assert system_calls[2][3] == "--columns=name,url,priority,options"
    assert system_calls[-1][:4] == ["flatpak", "remotes", "--system", "--show-details"]
    assert any("--user" in call for call in calls), "both installations must be queried"
    assert flatpak_source(sources, "legacy-vendor", "system").enabled is False
    assert flatpak_source(sources, "ustc-flathub", "system").priority == 5


def test_flatpak_add_list_remove_roundtrip(load, sandbox):
    stage(sandbox, "flatpak", "remotes-user.tsv")
    plugin = load("flatpak")
    source = Source(
        manager="flatpak",
        name="ustc",
        url="https://mirrors.ustc.edu.cn/flathub",
        priority=5,
        scope=Scope.USER,
        extra={
            "installation": "user",
            "remote_name": "ustc-flathub",
            "gpg_verify": False,
            "gpg": False,
        },
    )
    added = plugin.add(source)
    assert added.ok, added.message
    assert added.changed == [str(Path("~/.local/share/flatpak/repo/config").expanduser())]
    assert fake_calls(sandbox.flatpak) == [
        "flatpak remote-add --if-not-exists --user --gpg-verify=false --no-gpg-verify "
        "--prio=5 ustc-flathub https://mirrors.ustc.edu.cn/flathub"
    ]

    # the fake CLI does not mutate state: the test plays flatpak by appending the line
    with (sandbox.flatpak / "remotes-user.tsv").open("a", encoding="utf-8") as handle:
        handle.write("ustc-flathub\thttps://mirrors.ustc.edu.cn/flathub\t-\t5\n")
    listed = flatpak_source(plugin.list_enabled(), "ustc-flathub", "user")
    assert listed.enabled is True and listed.priority == 5

    removed = plugin.remove(listed)
    assert removed.ok, removed.message
    assert fake_calls(sandbox.flatpak)[-1] == "flatpak remote-delete --user ustc-flathub"

    # the test plays flatpak again: the remote really is gone now
    (sandbox.flatpak / "remotes-user.tsv").write_text(
        "flathub-user\thttps://dl.flathub.org/repo/\t-\t1\n", encoding="utf-8"
    )
    refused = plugin.remove(listed)
    assert refused.ok is False
    assert "no remote named 'ustc-flathub'" in refused.message
    assert len(fake_calls(sandbox.flatpak)) == 2, "a refused removal must not run flatpak"


def test_flatpak_system_mutations_need_root(load, sandbox, monkeypatch):
    as_root(monkeypatch, uid=1000)
    stage(sandbox, "flatpak", "remotes.tsv")
    plugin = load("flatpak")
    target = flatpak_source(plugin.list_enabled(), "ustc-flathub", "system")

    for result in (
        plugin.add(source_for("flatpak", "https://x.example.org/repo", extra={"installation": "system", "remote_name": "x"})),
        plugin.remove(target),
        plugin.enable(target),
        plugin.disable(target),
        plugin.update(target, {"url": "https://y.example.org/repo"}),
    ):
        assert result.ok is False
        assert "root" in result.message
        assert "sudo" in str(result.details.get("hint", ""))
    assert fake_calls(sandbox.flatpak) == [], "no CLI call may happen without root"

    # the user installation still works unprivileged
    assert plugin.add(source_for("flatpak", "https://x.example.org/repo", extra={"installation": "user", "remote_name": "x"})).ok


def test_flatpak_remove_refuses_sources_it_does_not_own(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    stage(sandbox, "flatpak", "remotes.tsv")
    plugin = load("flatpak")

    unknown = source_for(
        "flatpak", "https://ghost.example.org/repo", extra={"installation": "system", "remote_name": "ghost"}
    )
    result = plugin.remove(unknown)
    assert result.ok is False and "no remote named 'ghost'" in result.message

    mismatch = Source(
        manager="flatpak",
        name="flathub",
        url="https://evil.example.com/repo",
        extra={"installation": "system", "remote_name": "flathub"},
    )
    result = plugin.remove(mismatch)
    assert result.ok is False and "refusing to delete" in result.message
    assert fake_calls(sandbox.flatpak) == []


def test_flatpak_enable_disable_update_argv(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    stage(sandbox, "flatpak", "remotes.tsv")
    plugin = load("flatpak")
    target = flatpak_source(plugin.list_enabled(), "ustc-flathub", "system")

    assert plugin.disable(target).ok
    assert plugin.enable(target).ok
    updated = plugin.update(
        target, {"url": "https://mirrors.tuna.tsinghua.edu.cn/flathub", "priority": 3}
    )
    assert updated.ok, updated.message
    assert fake_calls(sandbox.flatpak) == [
        "flatpak remote-modify --system ustc-flathub --disable",
        "flatpak remote-modify --system ustc-flathub --enable",
        "flatpak remote-modify --system ustc-flathub "
        "--url=https://mirrors.tuna.tsinghua.edu.cn/flathub --prio=3",
    ]
    assert plugin.update(target, {}).ok is False
    assert plugin.update(target, {"priority": "abc"}).ok is False

    # the remote name is sanitised when neither extra nor provider_key supplies one
    (sandbox.flatpak / "remotes-user.tsv").write_text(
        "my-mirror-repo\thttps://m.example.org/repo\t-\t-\n", encoding="utf-8"
    )
    messy = Source(
        manager="flatpak",
        name="my mirror/repo",
        url="https://m.example.org/repo",
        extra={"installation": "user"},
    )
    assert plugin.enable(messy).ok
    assert fake_calls(sandbox.flatpak)[-1] == (
        "flatpak remote-modify --user my-mirror-repo --enable"
    )


def test_flatpak_test_treats_summary_200_as_healthy(load, monkeypatch):
    plugin = load("flatpak")
    mod = module_of(plugin)
    seen: list[str] = []

    def fake_probe(url, *, timeout: float = 8.0, **kwargs):
        seen.append(url)
        if url.endswith("/summary"):
            return ProbeResult(ok=True, status="ok", url=url, http_status=200, latency_ms=3.5)
        return ProbeResult(ok=False, status="http-404", url=url, http_status=404)

    monkeypatch.setattr(mod, "probe_url", fake_probe)
    source = source_for("flatpak", "https://dl.flathub.org/repo")
    result = plugin.test(source)
    assert seen == ["https://dl.flathub.org/repo", "https://dl.flathub.org/repo/summary"]
    assert result.ok is True and result.http_status == 200
    assert "summary" in result.detail and result.probes

    monkeypatch.setattr(
        mod, "probe_url", lambda url, **kw: ProbeResult(ok=False, status="timeout", url=url, error="timed out")
    )
    failed = plugin.test(source)
    assert failed.ok is False and failed.status == "timeout" and failed.error == "timed out"

    def boom(url, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(mod, "probe_url", boom)
    contained = plugin.test(source)
    assert contained.ok is False and contained.status == "error"
    assert plugin.test(source_for("flatpak", "")).ok is False


def test_flatpak_list_available_and_config_files(load, sandbox, monkeypatch):
    plugin = load("flatpak")
    available = plugin.list_available()
    assert [s.extra["installation"] for s in available] == ["system", "user"]
    assert {s.extra["remote_name"] for s in available} == {"flathub"}
    assert all(s.url == "https://dl.flathub.org/repo/" for s in available)
    assert all(s.kind is SourceKind.OFFICIAL for s in available)
    assert len({s.id for s in available}) == 2, "system and user Flathub need distinct ids"

    remotes_dir = sandbox.tmp / "remotes.d"
    remotes_dir.mkdir()
    (remotes_dir / "a.flatpakrepo").write_text("[Flatpak Repo]\n", encoding="utf-8")
    (remotes_dir / "b.flatpakrepo").write_text("[Flatpak Repo]\n", encoding="utf-8")
    monkeypatch.setattr(
        plugin.manifest,
        "config_paths",
        [str(remotes_dir / "*.flatpakrepo"), str(sandbox.tmp / "missing.conf")],
    )
    files = plugin.config_files()
    assert [path.name for path in files] == ["a.flatpakrepo", "b.flatpakrepo", "missing.conf"]
    assert all(isinstance(path, Path) for path in files)
    assert files[-1].exists() is False


def test_flatpak_detect_without_cli(load, sandbox, monkeypatch):
    plugin = load("flatpak")
    mod = module_of(plugin)
    monkeypatch.delenv("SRCMGR_FLATPAK_FAKE_DIR")
    monkeypatch.setattr(mod.helpers, "which", lambda name: None)
    monkeypatch.setattr(mod, "SYSTEM_CONFIG", sandbox.tmp / "system-repo-config")
    monkeypatch.setattr(mod, "USER_CONFIG", sandbox.tmp / "user-repo-config")
    monkeypatch.setattr(plugin, "_run_cli", lambda argv, *, timeout=20.0: mod.Outcome(returncode=127, error="not found"))

    assert plugin.detect() is False
    (sandbox.tmp / "system-repo-config").write_text('[remote "flathub"]\n', encoding="utf-8")
    assert plugin.detect() is True
    detail = plugin.detect_detail()
    assert detail["cli"] is False and detail["available"] is True
    assert detail["installations"]["system"]["count"] == 0


def test_flatpak_refresh_reports_reachability(load, monkeypatch):
    plugin = load("flatpak")
    mod = module_of(plugin)
    monkeypatch.setattr(
        plugin,
        "_run_cli",
        lambda argv, *, timeout=20.0: mod.Outcome(returncode=0, stdout="org.example.App\norg.example.Other\n"),
    )
    result = plugin.refresh()
    assert result.ok and result.details["apps"] == 2
    assert "appstream" in result.message

    monkeypatch.setattr(
        plugin, "_run_cli", lambda argv, *, timeout=20.0: mod.Outcome(returncode=1, stderr="boom")
    )
    assert plugin.refresh().ok is False


# ---------------------------------------------------------------------------
# snap
# ---------------------------------------------------------------------------
def test_snap_default_store_when_nothing_is_configured(load, sandbox):
    plugin = load("snap")
    sources = plugin.list_enabled()
    assert len(sources) == 1
    source = sources[0]
    assert source.url == "https://api.snapcraft.io"
    assert source.kind is SourceKind.OFFICIAL
    assert source.scope is Scope.SYSTEM
    assert source.enabled is True and source.is_default is True
    assert source.extra["value"] == source.url
    assert source.extra["origin"] == "default"
    assert plugin.current().id == source.id
    assert fake_calls(sandbox.snap) == []


def test_snap_reads_the_configured_proxy(load, sandbox):
    stage(sandbox, "snap", "proxy.json")
    plugin = load("snap")
    source = plugin.list_enabled()[0]
    assert source.url == "http://proxy.example.com:3128"
    assert source.name == "proxy.example.com:3128"
    assert source.kind is SourceKind.MIRROR
    assert source.enabled is True and source.is_default is False
    assert source.extra["origin"] == "proxy"
    assert source.extra["origin_detail"] == "snap get system proxy"
    assert source.extra["proxy"] == {
        "https": "http://proxy.example.com:3128",
        "http": "http://proxy.example.com:3128",
    }


def test_snap_reads_store_from_snapd_conf(load, sandbox):
    stage(sandbox, "snap", "snapd.conf")
    plugin = load("snap")
    source = plugin.list_enabled()[0]
    assert source.url == "https://store.example.com"
    assert source.extra["origin"] == "store"
    assert source.extra["store"] == "https://store.example.com"
    assert source.file == str(sandbox.snap / "snapd.conf")


def test_snap_parses_conf_dialects(load):
    mod = module_of(load("snap"))
    values = mod.parse_conf(
        "# comment\nstore: https://s.example.com\n[proxy]\nhttps = http://p.example.com:3128\n"
    )
    assert values["store"] == "https://s.example.com"
    assert values["proxy.https"] == "http://p.example.com:3128"


def test_snap_tolerates_non_json_answers(load, sandbox):
    (sandbox.snap / "proxy.json").write_text("access denied (try with sudo)\n", encoding="utf-8")
    plugin = load("snap")
    source = plugin.list_enabled()[0]
    assert source.url == "https://api.snapcraft.io"
    assert source.extra["proxy"] == {}


def test_snap_set_and_unset_roundtrip(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    plugin = load("snap")
    proxy = source_for("snap", "https://proxy.example.com:3128", label="corp-proxy")
    added = plugin.add(proxy)
    assert added.ok, added.message
    assert added.details["url"] == "https://proxy.example.com:3128"
    assert fake_calls(sandbox.snap) == [
        "snap set system proxy.https=https://proxy.example.com:3128 "
        "proxy.http=https://proxy.example.com:3128"
    ]

    # the test plays snapd: the write is now visible through `snap get system proxy`
    (sandbox.snap / "proxy.json").write_text(
        json.dumps(
            {"proxy": {"https": "https://proxy.example.com:3128", "http": "https://proxy.example.com:3128"}}
        ),
        encoding="utf-8",
    )
    assert plugin.enable(proxy).ok
    assert plugin.list_enabled()[0].url == "https://proxy.example.com:3128"

    removed = plugin.remove(proxy)
    assert removed.ok, removed.message
    assert fake_calls(sandbox.snap)[-1] == "snap unset system proxy.https proxy.http"

    before = list(fake_calls(sandbox.snap))
    refused = plugin.disable(source_for("snap", "http://other.example.com:8080", label="other"))
    assert refused.ok is False
    assert "not the configured snap proxy" in refused.message
    assert fake_calls(sandbox.snap) == before, "a refused removal must not run snap"


def test_snap_http_target_only_sets_its_own_scheme(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    plugin = load("snap")
    assert plugin.add(source_for("snap", "http://proxy.example.com:3128")).ok
    assert fake_calls(sandbox.snap) == ["snap set system proxy.http=http://proxy.example.com:3128"]

    assert plugin.update(
        source_for("snap", "http://proxy.example.com:3128"), {"url": "https://new.example.com:3128"}
    ).ok
    assert fake_calls(sandbox.snap)[-1] == (
        "snap set system proxy.https=https://new.example.com:3128 "
        "proxy.http=https://new.example.com:3128"
    )
    assert plugin.update(source_for("snap", "https://new.example.com:3128"), {}).ok is False


def test_snap_rejects_unsupported_schemes(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    plugin = load("snap")
    for url in ("ftp://proxy.example.com", "proxy.example.com:3128"):
        result = plugin.add(source_for("snap", url))
        assert result.ok is False and "unsupported scheme" in result.message
    assert fake_calls(sandbox.snap) == []


def test_snap_mutations_require_root(load, sandbox, monkeypatch):
    as_root(monkeypatch, uid=1000)
    plugin = load("snap")
    proxy = source_for("snap", "https://proxy.example.com:3128")
    results = [
        plugin.add(proxy),
        plugin.enable(proxy),
        plugin.disable(proxy),
        plugin.remove(proxy),
        plugin.update(proxy, {"url": "https://other.example.com:3128"}),
    ]
    for result in results:
        assert result.ok is False
        assert result.message == "snap store configuration requires root"
        assert result.details.get("hint") == "re-run with sudo"
    assert fake_calls(sandbox.snap) == []
    assert plugin.list_enabled(), "reads must keep working without root"


def test_snap_refuses_to_unset_a_value_it_does_not_own(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    # the proxy lives in snapd.conf, so `snap unset` would not touch it
    (sandbox.snap / "snapd.conf").write_text(
        "proxy.https: http://conf-owned.example.com:3128\n", encoding="utf-8"
    )
    plugin = load("snap")
    configured = plugin.list_enabled()[0]
    assert configured.url == "http://conf-owned.example.com:3128"
    assert configured.file == str(sandbox.snap / "snapd.conf")

    result = plugin.remove(source_for("snap", "http://conf-owned.example.com:3128"))
    assert result.ok is False
    assert "comes from" in result.message
    assert "snapd.conf" in str(result.details.get("hint", ""))
    assert fake_calls(sandbox.snap) == []


def test_snap_test_probes_store_then_proxy(load, sandbox, monkeypatch):
    plugin = load("snap")
    mod = module_of(plugin)
    seen: list[str] = []

    def fake_probe(url, *, timeout: float = 8.0, **kwargs):
        seen.append(url)
        return ProbeResult(ok=True, status="ok", url=url, http_status=200, latency_ms=1.0)

    monkeypatch.setattr(mod, "probe_url", fake_probe)
    store = plugin.list_available()[0]
    result = plugin.test(store)
    assert result.ok is True
    assert seen == ["https://api.snapcraft.io/v2/snaps/info/core"]

    stage(sandbox, "snap", "proxy.json")
    configured = plugin.list_enabled()[0]
    assert plugin.test(configured).ok is True
    assert seen[-1] == "http://proxy.example.com:3128"

    def boom(url, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(mod, "probe_url", boom)
    assert plugin.test(configured).ok is False


def test_snap_detect_detail_and_refresh_schedule(load, monkeypatch):
    plugin = load("snap")
    mod = module_of(plugin)
    calls: list[list[str]] = []

    def fake_run(argv, *, timeout: float = 20.0):
        calls.append(list(argv))
        return mod.Outcome(returncode=0, stdout="last refresh: today at 13:00\nnext: tomorrow 06:00\n")

    monkeypatch.setattr(plugin, "_run_cli", fake_run)
    detail = plugin.detect_detail()
    assert calls == [["snap", "refresh", "--time"]]
    assert "last refresh" in detail["refresh_schedule"]
    assert detail["store"] == "" and detail["origin"] == "default"

    refreshed = plugin.refresh()
    assert refreshed.ok and "snapd refresh schedule" in refreshed.message
    assert calls[-1] == ["snap", "refresh", "--time"]


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------
def test_docker_lists_mirrors_hub_and_insecure(load, sandbox):
    stage(sandbox, "docker", "daemon.json")
    plugin = load("docker")
    sources = plugin.list_enabled()

    mirrors = [
        s
        for s in sources
        if s.extra.get("key") == "registry-mirrors" and not s.extra.get("synthetic")
    ]
    assert [s.url for s in mirrors] == ["https://docker.1ms.run", "https://mirror.example.org"]
    assert all(s.enabled and s.kind is SourceKind.MIRROR and s.scope is Scope.SYSTEM for s in mirrors)
    assert mirrors[0].name == "docker.1ms.run"
    assert mirrors[0].extra["index"] == 0

    hub = next(s for s in sources if s.name == "Docker Hub")
    assert hub.url == "https://registry-1.docker.io"
    assert hub.enabled is False and hub.is_default is False
    assert hub.extra["note"] == "mirrors take precedence"
    assert hub.kind is SourceKind.OFFICIAL

    insecure = [s for s in sources if s.extra.get("key") == "insecure-registries"]
    assert [s.url for s in insecure] == ["registry.local:5000"]
    assert insecure[0].enabled is False and insecure[0].extra["informational"] is True

    assert plugin.current().url == "https://docker.1ms.run"
    assert {s.file for s in sources} == {str(sandbox.docker / "daemon.json")}


def test_docker_empty_or_missing_mirrors_promote_hub(load, sandbox):
    plugin = load("docker")
    sources = plugin.list_enabled()
    assert [s.name for s in sources] == ["Docker Hub"]
    assert sources[0].enabled is True and sources[0].is_default is True
    assert sources[0].extra["note"] == ""
    assert plugin.current().url == "https://registry-1.docker.io"
    assert (sandbox.docker / "daemon.json").exists() is False, "reads must not create the file"

    write_docker(sandbox, {})
    source = plugin.list_enabled()[0]
    assert source.enabled is True and source.is_default is True


def test_docker_add_remove_roundtrip_preserves_json(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    before_text = path.read_text(encoding="utf-8")
    before_data = json.loads(before_text)
    plugin = load("docker")

    added = plugin.add(source_for("docker", "https://docker.m.daocloud.io", label="daocloud"))
    assert added.ok, added.message
    assert added.changed == [str(path)]
    assert added.details["restart_required"] == "systemctl restart docker"
    assert added.details["created"] is False

    after_text = path.read_text(encoding="utf-8")
    after_data = json.loads(after_text)
    assert after_data["registry-mirrors"] == [
        *before_data["registry-mirrors"],
        "https://docker.m.daocloud.io",
    ]
    for key, value in before_data.items():
        if key != "registry-mirrors":
            assert after_data[key] == value, f"{key} was not preserved"
    assert list(after_data) == list(before_data), "key order must survive"
    assert after_text == json.dumps(after_data, indent=2) + "\n", "formatting style must survive"

    listed = plugin.list_enabled()
    assert "https://docker.m.daocloud.io" in [s.url for s in listed]
    assert [s.url for s in listed if s.enabled and s.extra.get("key") == "registry-mirrors"] == [
        "https://docker.1ms.run",
        "https://mirror.example.org",
        "https://docker.m.daocloud.io",
    ]

    removed = plugin.remove(source_for("docker", "https://docker.m.daocloud.io", label="daocloud"))
    assert removed.ok, removed.message
    assert path.read_text(encoding="utf-8") == before_text, "add+remove must be a no-op"


def test_docker_add_creates_a_missing_file_and_dedupes(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    plugin = load("docker")
    path = sandbox.docker / "daemon.json"
    assert not path.exists()
    first = plugin.add(source_for("docker", "https://mirror.example.org"))
    assert first.ok and first.details["created"] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "registry-mirrors": ["https://mirror.example.org"]
    }

    second = plugin.add(source_for("docker", "https://mirror.example.org/"))
    assert second.ok and second.changed == []
    assert "already configured" in second.message
    assert json.loads(path.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://mirror.example.org"
    ]


def test_docker_remove_last_mirror_drops_the_key(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = write_docker(
        sandbox, {"registry-mirrors": ["https://only.example.org"], "log-driver": "json-file"}
    )
    plugin = load("docker")
    assert plugin.remove(source_for("docker", "https://only.example.org")).ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "registry-mirrors" not in data
    assert data["log-driver"] == "json-file"


def test_docker_remove_refuses_what_it_does_not_own(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    before = path.read_bytes()
    plugin = load("docker")

    ghost = plugin.remove(source_for("docker", "https://ghost.example.org"))
    assert ghost.ok is False and "nothing was removed" in ghost.message

    insecure = plugin.remove(
        source_for("docker", "registry.local:5000", extra={"key": "insecure-registries"})
    )
    assert insecure.ok is False and "not managed" in insecure.message

    hub = plugin.disable(source_for("docker", "https://registry-1.docker.io", label="hub"))
    assert hub.ok is False and "not a configured mirror" in hub.message
    assert path.read_bytes() == before


def test_docker_disable_removes_when_parking_is_off(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    plugin = load("docker")

    assert plugin._park_enabled() is False
    disabled = plugin.disable(source_for("docker", "https://docker.1ms.run", label="1ms"))
    assert disabled.ok and disabled.details["parked"] is False
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://mirror.example.org"]
    assert "x-source-manager-disabled-mirrors" not in data

    assert plugin.enable(source_for("docker", "https://docker.1ms.run", label="1ms")).ok
    assert json.loads(path.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://mirror.example.org",
        "https://docker.1ms.run",
    ]


def test_docker_park_disabled_mirrors_opt_in(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    plugin = load("docker")
    plugin.config = Config({"managers": {"docker": {"park_disabled_mirrors": True}}})
    assert plugin._park_enabled() is True

    disabled = plugin.disable(source_for("docker", "https://docker.1ms.run", label="1ms"))
    assert disabled.ok and disabled.details["parked"] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://mirror.example.org"]
    assert data["x-source-manager-disabled-mirrors"] == ["https://docker.1ms.run"]

    parked = [
        s
        for s in plugin.list_enabled()
        if s.extra.get("key") == "x-source-manager-disabled-mirrors"
    ]
    assert len(parked) == 1
    assert parked[0].enabled is False and parked[0].url == "https://docker.1ms.run"
    assert parked[0].extra["parked"] is True
    again = plugin.disable(parked[0])
    assert again.ok and "already disabled" in again.message

    assert plugin.enable(parked[0]).ok
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["registry-mirrors"] == ["https://mirror.example.org", "https://docker.1ms.run"]
    assert "x-source-manager-disabled-mirrors" not in data, "an empty parked list is dropped"

    # a direct-test Config that is a plain mapping must not explode
    plugin.config = {"managers": {"docker": {"park_disabled_mirrors": True}}}
    assert plugin._park_enabled() is False


def test_docker_writes_require_root(load, sandbox, monkeypatch):
    as_root(monkeypatch, uid=1000)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    before = path.read_bytes()
    plugin = load("docker")
    results = [
        plugin.add(source_for("docker", "https://new.example.org")),
        plugin.remove(source_for("docker", "https://docker.1ms.run")),
        plugin.disable(source_for("docker", "https://docker.1ms.run")),
        plugin.update(source_for("docker", "https://docker.1ms.run"), {"url": "https://new.example.org"}),
    ]
    for result in results:
        assert result.ok is False
        assert "requires root" in result.message
        assert result.details.get("hint") == "re-run with sudo"
    assert path.read_bytes() == before
    assert plugin.list_enabled(), "reads must work without root"


def test_docker_update_replaces_in_place(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    shutil.copy(FIXTURES / "docker" / "daemon.json", path)
    plugin = load("docker")

    updated = plugin.update(
        source_for("docker", "https://docker.1ms.run", label="1ms"), {"url": "https://new.example.org"}
    )
    assert updated.ok, updated.message
    assert json.loads(path.read_text(encoding="utf-8"))["registry-mirrors"] == [
        "https://new.example.org",
        "https://mirror.example.org",
    ]

    clash = plugin.update(
        source_for("docker", "https://new.example.org", label="new"), {"url": "https://mirror.example.org"}
    )
    assert clash.ok is False and "already configured" in clash.message
    ghost = plugin.update(
        source_for("docker", "https://ghost.example.org", label="ghost"), {"url": "https://x.example.org"}
    )
    assert ghost.ok is False and "nothing was changed" in ghost.message
    assert plugin.update(source_for("docker", "https://new.example.org"), {}).ok is False


def test_docker_test_accepts_registry_auth_statuses(load, monkeypatch):
    plugin = load("docker")
    mod = module_of(plugin)
    seen: list[str] = []

    def probe(url, **kwargs):
        seen.append(url)
        return ProbeResult(ok=True, status="ok", url=url, http_status=401)

    monkeypatch.setattr(mod, "probe_url", probe)
    source = source_for("docker", "https://mirror.example.org")
    result = plugin.test(source)
    assert result.ok is True and result.http_status == 401
    assert seen == ["https://mirror.example.org/v2/"]
    assert "auth" in result.detail

    assert plugin.test(source_for("docker", "https://mirror.example.org/v2")).ok is True
    assert seen[-1] == "https://mirror.example.org/v2/"

    monkeypatch.setattr(
        mod, "probe_url", lambda url, **kw: ProbeResult(ok=False, status="http-404", url=url, http_status=404)
    )
    assert plugin.test(source).ok is False

    def boom(url, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(mod, "probe_url", boom)
    assert plugin.test(source).ok is False
    assert plugin.test(source_for("docker", "")).status == "no-url"


def test_docker_detect_detail_and_dropins(load, sandbox):
    plugin = load("docker")
    detail = plugin.detect_detail()
    assert detail["dropins"] == [] and detail["dropins_merged"] is False
    assert "never merged" in detail["dropin_note"]
    assert detail["root"] == str(sandbox.docker)
    assert detail["root_overridden"] is True
    assert detail["daemon_json_exists"] is False and detail["mirrors"] == 0
    assert detail["park_disabled_mirrors"] is False

    dropin_dir = sandbox.docker / "daemon.json.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "10-mirrors.json"
    dropin.write_text('{"registry-mirrors": ["https://dropin.example.org"]}\n', encoding="utf-8")

    plugin = load("docker")
    detail = plugin.detect_detail()
    assert detail["dropins"] == [str(dropin)]
    assert str(dropin) in detail["files"]
    assert detail["dropin_note"]
    assert plugin.detect() is True
    assert "https://dropin.example.org" not in [s.url for s in plugin.list_enabled()]
    assert plugin.config_files() == [sandbox.docker / "daemon.json", dropin]


def test_docker_broken_json_is_reported_not_fatal(load, sandbox, monkeypatch):
    as_root(monkeypatch)
    path = sandbox.docker / "daemon.json"
    path.write_text("{ this is not json\n", encoding="utf-8")
    plugin = load("docker")
    assert plugin.list_enabled()  # still lists the implicit Hub source
    assert "invalid JSON" in plugin.detect_detail()["daemon_json_error"]
    result = plugin.add(source_for("docker", "https://mirror.example.org"))
    assert result.ok is False and "invalid JSON" in result.message
    assert path.read_text(encoding="utf-8") == "{ this is not json\n"


def test_docker_root_defaults_to_etc(load, monkeypatch):
    monkeypatch.delenv("SRCMGR_DOCKER_ROOT")
    plugin = load("docker")
    assert plugin.daemon_path() == Path("/etc/docker/daemon.json")
    assert plugin.config_files() == [Path("/etc/docker/daemon.json")]


# ---------------------------------------------------------------------------
# cross-cutting
# ---------------------------------------------------------------------------
def test_loader_loads_the_three_managers(sandbox):
    loader = PluginLoader(user_dir=sandbox.tmp / "user-plugins", extra_paths=[])
    loaded = loader.load_managers(only=["flatpak", "snap", "docker"])
    assert set(loaded) == {"flatpak", "snap", "docker"}
    for name, item in loaded.items():
        assert item.ok, f"{name}: {item.error}"
        assert not item.error and not item.skipped
        plugin = item.plugin
        for operation in REQUIRED_OPERATIONS:
            assert callable(getattr(plugin, operation)), f"{name}.{operation} missing"
        assert "list" in plugin.supports and "refresh" in plugin.supports
        assert plugin.manifest.scope is Scope.SYSTEM
        assert "linux" in plugin.manifest.platforms
        assert plugin.manifest.config_paths
        assert plugin.base_dir and Path(plugin.base_dir).is_dir()
        assert plugin.info.name == name
        command = plugin.native_test_command()
        assert isinstance(command, list) and all(isinstance(part, str) for part in command)
        assert isinstance(plugin.detect_detail(), dict)
        assert all(isinstance(path, Path) for path in plugin.config_files())
        available = plugin.list_available()
        assert available and all(source.manager == name for source in available)


def test_commands_that_fail_never_raise(load, sandbox, monkeypatch):
    class ExplodingHelpers:
        """Forward everything to the real helpers, but make ``run`` explode."""

        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def run(self, *args: Any, **kwargs: Any):
            raise subprocess.TimeoutExpired(cmd=list(args[0]), timeout=1)

    as_root(monkeypatch)
    monkeypatch.delenv("SRCMGR_FLATPAK_FAKE_DIR")
    monkeypatch.delenv("SRCMGR_SNAP_FAKE_DIR")

    flatpak = load("flatpak")
    monkeypatch.setattr(module_of(flatpak), "helpers", ExplodingHelpers(helpers))
    assert flatpak.list_enabled() == []
    assert flatpak.current() is None
    result = flatpak.add(
        source_for("flatpak", "https://x.example.org/repo", extra={"installation": "system", "remote_name": "x"})
    )
    assert result.ok is False and "timed out" in result.message
    assert flatpak.remove(
        source_for("flatpak", "https://x.example.org/repo", extra={"installation": "system", "remote_name": "x"})
    ).ok is False
    assert flatpak.refresh().ok is False

    snap = load("snap")
    monkeypatch.setattr(module_of(snap), "helpers", ExplodingHelpers(helpers))
    assert snap.list_enabled()[0].url == "https://api.snapcraft.io"
    added = snap.add(source_for("snap", "https://proxy.example.com:3128"))
    assert added.ok is False and "timed out" in added.message
    assert snap.remove(source_for("snap", "https://proxy.example.com:3128")).ok is False


def test_runtime_reads_all_three_managers(sandbox):
    stage(sandbox, "flatpak", "remotes.tsv", "remotes-user.tsv")
    stage(sandbox, "snap", "proxy.json")
    stage(sandbox, "docker", "daemon.json")

    from srcmgr.core.runtime import SourceManagerRuntime

    runtime = SourceManagerRuntime(config=Config.defaults())
    for name in ("flatpak", "snap", "docker"):
        status = runtime.status(name)
        assert status.available is True, name
        assert status.error == ""
        assert status.installed_count >= 1, name
        assert runtime.installed(name)
        assert all(isinstance(path, str) for path in status.config_files)
    assert str(sandbox.docker) in runtime.status("docker").config_files[0]
    warnings = [w for w in runtime.warnings() if w.split(":")[0] in {"flatpak", "snap", "docker"}]
    assert warnings == []


# ---------------------------------------------------------------------------
# the guard: nothing outside the temp roots may change
# ---------------------------------------------------------------------------
def _snapshot_real_system() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "etc_mtime_ns": REAL_ETC.stat().st_mtime_ns,
        "docker_dir_exists": REAL_DOCKER_DIR.exists(),
        "daemon_exists": REAL_DAEMON_JSON.exists(),
    }
    if snapshot["docker_dir_exists"]:
        snapshot["docker_dir_mtime_ns"] = REAL_DOCKER_DIR.stat().st_mtime_ns
    if snapshot["daemon_exists"]:
        info = REAL_DAEMON_JSON.stat()
        snapshot["daemon_mtime_ns"] = info.st_mtime_ns
        snapshot["daemon_size"] = info.st_size
        snapshot["daemon_sha256"] = hashlib.sha256(REAL_DAEMON_JSON.read_bytes()).hexdigest()
    return snapshot


def _assert_real_system_unchanged(before: dict[str, Any]) -> None:
    assert REAL_ETC.stat().st_mtime_ns == before["etc_mtime_ns"], "/etc was modified by a test"
    assert REAL_DOCKER_DIR.exists() == before["docker_dir_exists"]
    if before["docker_dir_exists"]:
        assert REAL_DOCKER_DIR.stat().st_mtime_ns == before["docker_dir_mtime_ns"]
    assert REAL_DAEMON_JSON.exists() == before["daemon_exists"]
    if before["daemon_exists"]:
        info = REAL_DAEMON_JSON.stat()
        assert info.st_mtime_ns == before["daemon_mtime_ns"], "real daemon.json mtime changed"
        assert info.st_size == before["daemon_size"]
        assert (
            hashlib.sha256(REAL_DAEMON_JSON.read_bytes()).hexdigest() == before["daemon_sha256"]
        ), "real daemon.json content changed"


def test_no_test_writes_outside_its_temp_root(load, sandbox, monkeypatch):
    for name in ("SRCMGR_FLATPAK_FAKE_DIR", "SRCMGR_SNAP_FAKE_DIR", "SRCMGR_DOCKER_ROOT"):
        assert os.environ[name].startswith(str(sandbox.tmp)), f"{name} escaped the temp root"

    before = _snapshot_real_system()
    as_root(monkeypatch)

    stage(sandbox, "flatpak", "remotes.tsv", "remotes-user.tsv")
    flatpak = load("flatpak")
    added = flatpak.add(
        source_for(
            "flatpak",
            "https://mirrors.ustc.edu.cn/flathub",
            label="ustc",
            extra={"installation": "system", "remote_name": "ustc-flathub"},
        )
    )
    assert added.ok, added.message
    assert flatpak.remove(flatpak_source(flatpak.list_enabled(), "ustc-flathub", "system")).ok
    assert fake_calls(sandbox.flatpak) == [
        "flatpak remote-add --if-not-exists --system ustc-flathub "
        "https://mirrors.ustc.edu.cn/flathub",
        "flatpak remote-delete --system ustc-flathub",
    ]

    snap = load("snap")
    proxy = source_for("snap", "http://proxy.example.com:3128", label="proxy")
    assert snap.add(proxy).ok
    (sandbox.snap / "proxy.json").write_text(
        json.dumps({"proxy": {"http": "http://proxy.example.com:3128"}}), encoding="utf-8"
    )
    assert snap.remove(proxy).ok
    assert fake_calls(sandbox.snap) == [
        "snap set system proxy.http=http://proxy.example.com:3128",
        "snap unset system proxy.http",
    ]

    docker = load("docker")
    daemon = sandbox.docker / "daemon.json"
    assert daemon.exists() is False
    assert docker.add(source_for("docker", "https://mirror.example.org")).ok
    assert daemon.exists() is True
    assert docker.remove(source_for("docker", "https://mirror.example.org")).ok
    assert json.loads(daemon.read_text(encoding="utf-8")) == {}
    assert not (sandbox.docker / "calls.log").exists(), "docker never shells out"

    _assert_real_system_unchanged(before)
