"""Tests for the safety-critical runtime behaviours.

* mutations always snapshot first and are always undoable
* automatic failover never acts unless it was explicitly enabled
* a system-scope manager refuses to act without root (and does so before writing)
* the plugin marketplace never executes untrusted code
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from srcmgr.core.api import PluginType, Source, Scope
from srcmgr.core.config import Config
from srcmgr.core.errors import PermissionDenied
from srcmgr.core.health import ProbeResult
from srcmgr.core.plugins import PluginLoader, install_plugin, scaffold_plugin, trust_plugin
from srcmgr.core.runtime import SourceManagerRuntime

UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: resolute
Components: main restricted universe multiverse
"""


def apt_tree(tmp_path: Path) -> Path:
    root = tmp_path / "etc-apt"
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True)
    (dropins / "ubuntu.sources").write_text(UBUNTU_SOURCES, encoding="utf-8")
    (root / "sources.list").write_text("# empty\n", encoding="utf-8")
    return root


def runtime(**config: object) -> SourceManagerRuntime:
    return SourceManagerRuntime(config=Config(dict(config)))


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


# ---------------------------------------------------------------------------
# snapshot-before-change and rollback
# ---------------------------------------------------------------------------
def test_successful_mutation_creates_a_snapshot_and_rolls_back(isolated_env, tmp_path, as_root):
    root = apt_tree(tmp_path)
    rt = runtime()
    before = (root / "sources.list.d" / "ubuntu.sources").read_text()

    result = rt.add(
        "apt", source=Source(manager="apt", name="Example",
                             url="https://mirror.example.org/ubuntu/", suite="resolute")
    )
    assert result.ok, result.message
    assert result.backup_id, "a mutation must always be backed by a snapshot"
    created = [p for p in (root / "sources.list.d").iterdir() if "example" in p.name]
    assert created, "the source file was not created"
    assert (root / "sources.list.d" / "ubuntu.sources").read_text() == before

    records = rt.history_records(manager="apt")
    assert records[0].action == "add" and records[0].backup == result.backup_id

    undone = rt.rollback("apt")
    assert undone.ok, undone.message
    assert not created[0].exists(), "rollback must remove files the change created"
    assert (root / "sources.list.d" / "ubuntu.sources").read_text() == before


def test_backup_failure_aborts_before_writing(isolated_env, tmp_path, as_root, monkeypatch):
    root = apt_tree(tmp_path)
    rt = runtime(backup={"enabled": True, "before_change": True})
    calls: list[str] = []

    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(rt.backups, "snapshot", explode)
    plugin = rt.plugin("apt")
    monkeypatch.setattr(plugin, "add", lambda source: calls.append("add"))

    result = rt.add("apt", source=Source(manager="apt", name="X", url="https://x.example/"))
    assert not result.ok
    assert "backup failed" in result.message
    assert calls == [], "the plugin must not be called when the snapshot failed"
    assert sorted(p.name for p in (root / "sources.list.d").iterdir()) == ["ubuntu.sources"]


def test_dry_run_never_touches_anything(isolated_env, tmp_path, as_root):
    root = apt_tree(tmp_path)
    rt = runtime()
    before = {p.name: p.read_text() for p in root.rglob("*") if p.is_file()}
    result = rt.add(
        "apt",
        source=Source(manager="apt", name="X", url="https://x.example/ubuntu/"),
        dry_run=True,
    )
    assert result.ok and result.details.get("dry_run") is True
    assert {p.name: p.read_text() for p in root.rglob("*") if p.is_file()} == before
    assert rt.history_records() == [], "a dry run must not appear in history"


def test_history_records_every_mutation(isolated_env, tmp_path, as_root):
    apt_tree(tmp_path)
    rt = runtime()
    rt.add("apt", source=Source(manager="apt", name="One", url="https://one.example/ubuntu/",
                                suite="resolute"))
    rt.remove("apt", "one")
    actions = [record.action for record in rt.history_records(manager="apt")]
    assert actions == ["remove", "add"]
    assert all(record.backup for record in rt.history_records(manager="apt"))


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------
def test_system_scope_manager_refuses_without_root(isolated_env, tmp_path, monkeypatch):
    root = apt_tree(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    rt = runtime()
    with pytest.raises(PermissionDenied) as excinfo:
        rt.add("apt", source=Source(manager="apt", name="X", url="https://x.example/",
                                    scope=Scope.SYSTEM))
    assert "sudo" in str(excinfo.value)
    assert sorted(p.name for p in (root / "sources.list.d").iterdir()) == ["ubuntu.sources"]


def test_user_scope_manager_works_without_root(isolated_env, as_root=None):
    rt = runtime()
    result = rt.add("npm", source=Source(manager="npm", name="npmmirror",
                                         url="https://registry.npmmirror.com"))
    assert result.ok, result.message
    assert rt.current("npm").url == "https://registry.npmmirror.com"


# ---------------------------------------------------------------------------
# fallback
# ---------------------------------------------------------------------------
def test_fallback_plan_is_inert_when_disabled(isolated_env, tmp_path, monkeypatch):
    apt_tree(tmp_path)
    rt = runtime()
    plan = rt.fallback_plan("apt")
    assert plan.to_source == ""
    assert "disabled" in plan.reason


def test_fallback_switches_to_a_reachable_source(isolated_env, tmp_path, as_root, monkeypatch):
    root = apt_tree(tmp_path)
    (root / "sources.list.d" / "ubuntu.sources").write_text(
        "Types: deb\nURIs: http://127.0.0.1:9/ubuntu/\nSuites: resolute\nComponents: main\n",
        encoding="utf-8",
    )
    rt = runtime(managers={"apt": {"fallback": {"enabled": True}}})

    def fake_probe(url, **kwargs):
        if "127.0.0.1" in url:
            return ProbeResult(ok=False, status="unreachable", url=url, error="Connection refused")
        return ProbeResult(ok=True, status="ok", url=url, http_status=200, latency_ms=42.0)

    monkeypatch.setattr("srcmgr.core.health.probe_url", fake_probe)
    plan = rt.fallback_plan("apt")
    assert plan.to_source, plan.reason
    assert "127.0.0.1" in plan.from_source

    result = rt.apply_fallback("apt", plan)
    assert result.ok, result.message
    assert any("fallback" == r.action for r in rt.history_records(manager="apt"))
    written = (root / "sources.list.d").glob("*.sources")
    assert any("127.0.0.1" not in p.read_text() for p in written if "ubuntu" not in p.name)


def test_fallback_does_nothing_when_the_current_source_is_healthy(
    isolated_env, tmp_path, as_root, monkeypatch
):
    apt_tree(tmp_path)
    rt = runtime(managers={"apt": {"fallback": {"enabled": True}}})
    monkeypatch.setattr(
        "srcmgr.core.health.probe_url",
        lambda url, **kwargs: ProbeResult(ok=True, status="ok", url=url, http_status=200,
                                          latency_ms=10.0),
    )
    plan = rt.fallback_plan("apt")
    assert plan.to_source == ""
    assert "healthy" in plan.reason


# ---------------------------------------------------------------------------
# plugin marketplace trust
# ---------------------------------------------------------------------------
def test_scaffold_install_trust_and_uninstall(isolated_env, tmp_path):
    plugins_dir = Path(isolated_env["SRCMGR_CONFIG_HOME"]) / "plugins"
    scaffold = scaffold_plugin("mymirrors", kind=PluginType.PROVIDER, target=tmp_path,
                               managers=["apt"])
    assert (scaffold / "plugin.yaml").exists()
    assert list((scaffold / "sources").glob("*.yaml"))

    installed = install_plugin(str(scaffold), trust=False)
    assert installed == plugins_dir / "mymirrors"

    loader = PluginLoader()
    untrusted = loader.load_providers()["mymirrors"]
    assert not untrusted.ok and "untrusted" in untrusted.skipped

    trust_plugin("mymirrors")
    trusted = PluginLoader().load_providers()["mymirrors"]
    assert trusted.ok, trusted.error
    assert trusted.provider.list_sources("apt"), "a trusted provider must expose its sources"

    from srcmgr.core.plugins import uninstall_plugin

    uninstall_plugin("mymirrors")
    assert not (plugins_dir / "mymirrors").exists()


def test_scaffolded_manager_plugin_loads_and_is_usable(isolated_env, tmp_path):
    scaffold = scaffold_plugin("demo", kind=PluginType.MANAGER, target=tmp_path)
    install_plugin(str(scaffold), trust=True)
    loaded = PluginLoader().load_managers()["demo"]
    assert loaded.ok, loaded.error
    assert loaded.plugin.detect() is True
    assert loaded.plugin.list_enabled() == []


def test_install_refuses_to_overwrite_without_force(isolated_env, tmp_path):
    scaffold = scaffold_plugin("demo", kind=PluginType.MANAGER, target=tmp_path)
    install_plugin(str(scaffold), trust=True)
    with pytest.raises(Exception):
        install_plugin(str(scaffold), trust=True)
    install_plugin(str(scaffold), trust=True, force=True)  # --force is allowed


def test_plugin_archive_path_traversal_is_rejected(isolated_env, tmp_path):
    import tarfile

    archive = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "plugin.yaml").write_text("name: evil\ntype: manager\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload / "plugin.yaml", arcname="evil/plugin.yaml")
        info = tarfile.TarInfo("evil/../../escaped.txt")
        info.size = 3
        import io

        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(Exception) as excinfo:
        install_plugin(str(archive))
    assert "escape" in str(excinfo.value).lower()
    assert not (tmp_path / "escaped.txt").exists()
