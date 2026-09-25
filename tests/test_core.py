"""Tests for the core contracts: source registry, selection, backup store, history.

These cover the parts the plugins rely on and the parts that broke during development
(snapshot payload resolution, provider ``extra`` passthrough, selector resolution).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from srcmgr.core.api import Source, SourceKind, compute_source_id
from srcmgr.core.backup import BackupStore
from srcmgr.core.config import Config, deep_merge, parse_value
from srcmgr.core.errors import SourceNotFound
from srcmgr.core.history import HistoryStore
from srcmgr.core.plugins import parse_manifest, source_from_dict
from srcmgr.core.registry import SourceRegistry, normalize_url, source_matches
from srcmgr.core.selection import select_source


def make(**kwargs) -> Source:
    data = {"manager": "apt", "name": "Example", "url": "https://mirror.example.org/debian/"}
    data.update(kwargs)
    return Source(**data)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
def test_source_id_is_stable_and_distinct():
    a = compute_source_id("apt", "https://x/", "trixie")
    b = compute_source_id("apt", "https://x/", "trixie")
    c = compute_source_id("apt", "https://x/", "bookworm")
    assert a == b and len(a) == 8
    assert a != c
    assert compute_source_id("npm", "https://x/", "") != compute_source_id("apt", "https://x/", "")


def test_source_redaction_hides_secrets():
    source = Source(
        manager="npm",
        name="private",
        url="https://registry.corp/",
        secret_fields=["extra"],
        extra={"token": "s3cret"},
    )
    assert source.to_dict(redact=True)["extra"] == "<redacted>"
    assert source.to_dict()["extra"] == {"token": "s3cret"}


def test_normalize_url_and_matching():
    assert normalize_url("HTTPS://Mirror.Example.org/debian/") == "https://mirror.example.org/debian"
    assert source_matches(make(), make(url="https://mirror.example.org/debian"))
    assert not source_matches(make(), make(url="https://other.example.org/debian/"))
    assert not source_matches(make(), make(manager="npm"))


# ---------------------------------------------------------------------------
# provider entries -> Source
# ---------------------------------------------------------------------------
def test_source_from_dict_keeps_top_level_extras():
    """Provider authors use plain top-level keys; they must land in extra."""
    source = source_from_dict(
        {
            "key": "tuna",
            "name": "TUNA",
            "kind": "mirror",
            "url": "https://mirrors.tuna.tsinghua.edu.cn/debian/",
            "url_template": "https://mirrors.tuna.tsinghua.edu.cn/${distro}/",
            "countries": ["cn"],
            "scope": "system",
            "components": ["main"],
            "health": {"url": "https://mirrors.tuna.tsinghua.edu.cn/debian/dists/stable/Release"},
        },
        manager="apt",
        provider="china-mirrors",
    )
    assert source.extra["url_template"].endswith("${distro}/")
    assert source.extra["countries"] == ["cn"]
    assert source.kind is SourceKind.MIRROR
    assert source.provider == "china-mirrors" and source.provider_key == "tuna"
    assert source.components == ["main"]


def test_source_from_dict_rejects_unknown_kind_and_missing_url():
    with pytest.raises(Exception):
        source_from_dict({"key": "x", "url": "https://x/", "kind": "nonsense"},
                         manager="apt", provider="p")
    with pytest.raises(Exception):
        source_from_dict({"key": "x"}, manager="apt", provider="p")


def test_parse_manifest_requires_managers_for_providers(tmp_path):
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text("name: p\ntype: provider\nversion: 1.0.0\n", encoding="utf-8")
    with pytest.raises(Exception):
        parse_manifest(manifest)
    manifest.write_text("name: p\ntype: provider\nmanagers: [apt]\n", encoding="utf-8")
    parsed = parse_manifest(manifest)
    assert parsed.managers == ["apt"]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
class _FakeProvider:
    def __init__(self, name: str, sources: list[Source]) -> None:
        from srcmgr.core.api import PluginManifest, PluginType

        self.manifest = PluginManifest(
            name=name, type=PluginType.PROVIDER, display_name=name, managers=["apt"]
        )
        self._sources = sources

    def list_sources(self, manager: str | None = None) -> list[Source]:
        if manager is None:
            return list(self._sources)
        return [s for s in self._sources if s.manager == manager]

    def get_source(self, key: str):
        return next((s for s in self._sources if s.provider_key == key), None)


def test_registry_groups_and_search():
    one = make(name="TUNA", provider="china-mirrors", provider_key="tuna", extra={"countries": ["cn"]})
    two = make(name="USTC", url="https://mirrors.ustc.edu.cn/debian/", provider="china-mirrors",
               provider_key="ustc", extra={"countries": ["cn"]})
    three = make(name="Official", url="https://deb.debian.org/debian/", provider="debian-official",
                 provider_key="official", kind=SourceKind.OFFICIAL)
    registry = SourceRegistry({"china-mirrors": _FakeProvider("china-mirrors", [one, two]),
                               "debian-official": _FakeProvider("debian-official", [three])})
    assert len(registry.sources_for("apt")) == 3
    groups = registry.groups_for("apt")
    assert {g.name for g in groups} == {"china-mirrors", "debian-official"}
    assert registry.get("apt", "tuna") is one
    assert registry.search("apt", "ustc") == [two]
    assert registry.search("apt", "", kinds=[SourceKind.OFFICIAL]) == [three]
    assert registry.search("apt", "", country="cn") == [one, two]


def test_country_filter_excludes_undeclared_sources():
    """--china must not leak entries that never claimed a country."""
    declared = make(provider="p", provider_key="tuna", extra={"countries": ["cn"]})
    undeclared = make(name="Official", url="https://deb.debian.org/debian/", provider="p",
                      provider_key="official")
    registry = SourceRegistry(
        {"p": _FakeProvider("p", [declared, undeclared])}
    )
    assert registry.search("apt", "", country="cn") == [declared]
    assert len(registry.search("apt", "")) == 2


def test_registry_dedupes_identical_entries():
    """Same provider+key+url twice is a duplicate; same URL under two keys is two entries.

    Two providers may legitimately describe the same URL differently (different key,
    components, health path), so URL alone must not collapse them - only an exact
    manager+key+url duplicate is dropped.
    """
    duplicate = make(provider="p1", provider_key="tuna")
    same_url_other_key = make(provider="p1", provider_key="tuna-https")
    registry = SourceRegistry(
        {"p1": _FakeProvider("p1", [duplicate, make(provider="p1", provider_key="tuna")])}
    )
    assert len(registry.sources_for("apt")) == 1
    registry = SourceRegistry(
        {"p1": _FakeProvider("p1", [duplicate, same_url_other_key])}
    )
    assert len(registry.sources_for("apt")) == 2


def test_registry_respects_disabled_providers():
    provider = _FakeProvider("china-mirrors", [make(provider="china-mirrors", provider_key="tuna")])
    config = Config({"registry": {"disabled_providers": ["china-mirrors"]}})
    registry = SourceRegistry({"china-mirrors": provider}, config=config)
    assert registry.sources_for("apt") == []


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def test_select_source_by_index_key_id_name_and_url():
    items = [
        make(name="TUNA", provider_key="tuna"),
        make(name="USTC", url="https://mirrors.ustc.edu.cn/debian/", provider_key="ustc"),
    ]
    assert select_source("tuna", items).name == "TUNA"
    assert select_source("#2", items).name == "USTC"
    assert select_source("2", items).name == "USTC"
    assert select_source(items[0].id, items).name == "TUNA"
    assert select_source("https://mirrors.ustc.edu.cn/debian", items).name == "USTC"
    assert select_source("ustc", items).name == "USTC"


def test_select_source_errors_are_actionable():
    items = [make(name="TUNA mirror", provider_key="a"), make(name="TUNA backup", provider_key="b")]
    with pytest.raises(SourceNotFound) as excinfo:
        select_source("tuna", items)
    assert "ambiguous" in str(excinfo.value)
    with pytest.raises(SourceNotFound):
        select_source("#9", items)
    with pytest.raises(SourceNotFound):
        select_source("nothing-like-this", items)


# ---------------------------------------------------------------------------
# backup + restore
# ---------------------------------------------------------------------------
def test_backup_restore_round_trip(tmp_path):
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    first = config_dir / "sources.list"
    second = config_dir / "extra.list"
    first.write_text("deb http://original/\n")
    second.write_text("# second file\n")

    store = BackupStore(tmp_path / "backups")
    backup_id = store.snapshot("apt", [first, second, config_dir / "not-created-yet.list"])
    assert store.get(backup_id).files

    # mutate both, create a new file, then restore
    first.write_text("deb http://changed/\n")
    second.unlink()
    (config_dir / "created.sources").write_text("Types: deb\n")
    store.record_paths(backup_id, [config_dir / "*.sources"])

    result = store.restore(backup_id)
    assert result.ok, result.message
    assert first.read_text() == "deb http://original/\n"
    assert second.read_text() == "# second file\n"
    assert not (config_dir / "created.sources").exists(), "rollback must delete created files"


def test_backup_restore_is_idempotent_and_reports_errors(tmp_path):
    path = tmp_path / "sources.list"
    path.write_text("original\n")
    store = BackupStore(tmp_path / "backups")
    backup_id = store.snapshot("apt", [path])
    first = store.restore(backup_id)
    assert first.ok and not first.changed, "a no-op restore must not rewrite anything"
    path.write_text("changed\n")
    second = store.restore(backup_id)
    assert second.ok and second.changed == [str(path)]


def test_backup_prune_keeps_the_newest(tmp_path):
    path = tmp_path / "sources.list"
    store = BackupStore(tmp_path / "backups")
    ids = []
    for index in range(4):
        path.write_text(f"line {index}\n")
        # snapshot() itself prunes with the configured default; that is fine.
        ids.append(
            store.snapshot("apt", [path], notes={"index": index})
        )
    # Deterministic ordering without sleeping: rewrite created_at, which is what
    # ordering and pruning use.
    from srcmgr.core.backup import utc_now

    for index, backup_id in enumerate(ids):
        manifest = store.get(backup_id)
        manifest.created_at = f"2026-01-0{index + 1}T00:00:00+00:00"
        from srcmgr.core.paths import write_json

        write_json(store.manifest_path(backup_id), manifest.to_dict())
    store.prune(keep=2)
    remaining = [m.id for m in store.list()]
    assert len(remaining) == 2
    assert ids[-1] in remaining


def test_history_append_read_and_export(tmp_path):
    store = HistoryStore(tmp_path / "history")
    store.log("enable", manager="apt", source="TUNA", backup="b1", message="ok")
    store.log("disable", manager="npm", source="npmmirror", ok=False, message="nope")
    records = store.read()
    assert len(records) == 2
    assert store.read(manager="apt")[0].source == "TUNA"
    assert store.last_backup("apt") == "b1"
    assert store.find("b1") is not None
    exported = store.export(tmp_path / "out.jsonl")
    assert exported.read_text().count("\n") == 2


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def test_config_layering_and_env_override(monkeypatch, tmp_path):
    config = Config({"health": {"timeout": 5}, "managers": {"apt": {"health": {"timeout": 9}}}})
    assert config.health()["timeout"] == 5
    assert config.health("apt")["timeout"] == 9
    assert config.health("npm")["timeout"] == 5
    monkeypatch.setenv("SRCMGR_HEALTH__TIMEOUT", "1.5")
    assert Config({"health": {"timeout": 5}}).health()["timeout"] == 1.5


def test_deep_merge_and_parse_value():
    merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
    assert merged == {"a": {"b": 1, "c": 3}}
    assert parse_value("true") is True
    assert parse_value("3.5") == 3.5
    assert parse_value("7") == 7
    assert parse_value("[1, 2]") == [1, 2]
    assert parse_value("plain") == "plain"


def test_fallback_is_disabled_by_default():
    """Automatic failover must never be on unless the user asks for it."""
    config = Config.defaults()
    assert config.fallback()["enabled"] is False
    assert config.fallback("apt")["enabled"] is False
