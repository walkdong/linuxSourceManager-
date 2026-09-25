"""Tests for ecosystem locks and restoring a specific snapshot.

The lock is the safety net for beginners: once pinned, **nothing** may change that
ecosystem — not the menu, not the one-click "fastest mirror", not automatic failover.
These tests pin the behaviour down (including the escape hatches).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from srcmgr.core.api import Source
from srcmgr.core.config import Config
from srcmgr.core.errors import LockedError
from srcmgr.core.locks import LockStore
from srcmgr.core.runtime import SourceManagerRuntime

UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: resolute
Components: main restricted universe multiverse
"""


def build_apt_tree(env) -> Path:
    """Populate the sandboxed apt root (a helper, not a fixture: tests that only need the
    side effect must not be turned into tests that assert on its return value)."""
    root = Path(env["SRCMGR_APT_ROOT"])
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / "ubuntu.sources").write_text(UBUNTU_SOURCES, encoding="utf-8")
    (root / "sources.list").write_text("# empty\n", encoding="utf-8")
    return root


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


def runtime() -> SourceManagerRuntime:
    return SourceManagerRuntime(config=Config.defaults())


# ---------------------------------------------------------------------------
# store level
# ---------------------------------------------------------------------------
def test_lock_store_round_trip_and_persistence(tmp_path):
    path = tmp_path / "locks.json"
    store = LockStore(path)
    assert store.locked() == [] and store.count() == 0
    assert not store.is_locked("apt")

    store.lock("apt", reason="works now")
    assert store.is_locked("apt")
    assert "apt" in store
    assert store.snapshot()["apt"], "the lock time must be recorded"

    # a fresh store sees the same state (it is on disk, not in memory)
    assert LockStore(path).locked() == ["apt"]
    assert LockStore(path).entry("apt").reason == "works now"

    assert store.unlock("apt") is True
    assert store.unlock("apt") is False
    assert store.locked() == []


def test_lock_store_tolerates_corrupt_and_legacy_files(tmp_path):
    path = tmp_path / "locks.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert LockStore(path).locked() == [], "a corrupt file must not break the UI"

    path.write_text(json.dumps(["apt", "npm"]), encoding="utf-8")  # bare list
    assert LockStore(path).locked() == ["apt", "npm"]

    path.write_text(json.dumps({"locked": {"apt": {"locked_at": "x"}}}), encoding="utf-8")
    entry = LockStore(path).entry("apt")
    assert entry is not None and entry.locked_at == "x"


def test_lock_store_prunes_unknown_managers(tmp_path):
    store = LockStore(tmp_path / "locks.json")
    store.lock("apt")
    store.lock("nosuchmanager")
    assert store.prune(["apt", "npm"]) == ["nosuchmanager"]
    assert store.locked() == ["apt"]


def test_assert_unlocked_only_protects_source_changes():
    store = LockStore(Path("/nonexistent/never-written.json"))
    store.lock = lambda *a, **k: None  # type: ignore[assignment]
    # a lock blocks source changes ...
    for action in ("add", "remove", "enable", "disable", "update", "fallback"):
        assert action in __import__("srcmgr.core.locks", fromlist=["LOCKED_ACTIONS"]).LOCKED_ACTIONS
    # ... but restoring a backup is the documented escape hatch
    assert "restore" not in __import__("srcmgr.core.locks", fromlist=["LOCKED_ACTIONS"]).LOCKED_ACTIONS


# ---------------------------------------------------------------------------
# runtime level
# ---------------------------------------------------------------------------
def test_lock_blocks_every_source_change(isolated_env, tmp_path, as_root):
    root = build_apt_tree(isolated_env)
    rt = runtime()
    rt.lock("apt")
    assert rt.is_locked("apt") and rt.locked_managers() == ["apt"]

    before = (root / "sources.list.d" / "ubuntu.sources").read_text()
    source = Source(manager="apt", name="Example", url="https://mirror.example.org/ubuntu/",
                    suite="resolute")

    with pytest.raises(LockedError):
        rt.add("apt", source=source)
    with pytest.raises(LockedError):
        rt.remove("apt", "ubuntu")
    with pytest.raises(LockedError):
        rt.disable("apt", "ubuntu")
    with pytest.raises(LockedError):
        rt.edit("apt", "ubuntu", {"suite": "other"})

    assert (root / "sources.list.d" / "ubuntu.sources").read_text() == before
    assert not [p for p in (root / "sources.list.d").iterdir() if "example" in p.name]
    assert rt.history_records(manager="apt") == [] or all(
        r.action in {"lock"} for r in rt.history_records(manager="apt")
    ), "a refused change must not appear as a change"


def test_lock_blocks_dry_run_too(isolated_env, tmp_path, as_root):
    """Even a dry run is refused, so the UI can never imply it would work."""
    build_apt_tree(isolated_env)
    rt = runtime()
    rt.lock("apt")
    result = rt.add(
        "apt",
        source=Source(manager="apt", name="X", url="https://x.example/ubuntu/"),
        dry_run=True,
    )
    assert result.ok and result.details.get("dry_run"), "dry-run stays harmless"
    with pytest.raises(LockedError):
        rt.add("apt", source=Source(manager="apt", name="X", url="https://x.example/ubuntu/"))


def test_unlock_restores_the_ability_to_change(isolated_env, tmp_path, as_root):
    build_apt_tree(isolated_env)
    rt = runtime()
    rt.lock("apt")
    assert rt.toggle_lock("apt") is False  # unlock
    result = rt.add("apt", source=Source(manager="apt", name="Example",
                                         url="https://mirror.example.org/ubuntu/", suite="resolute"))
    assert result.ok, result.message


def test_locked_ecosystem_is_skipped_by_automatic_fallback(isolated_env, tmp_path, as_root, monkeypatch):
    build_apt_tree(isolated_env)
    rt = runtime()
    rt.lock("apt")

    def fail(*_args, **_kwargs):
        raise AssertionError("a locked ecosystem must not even be probed for fallback")

    monkeypatch.setattr(rt, "fallback_plan", fail)
    result = rt.apply_fallback("apt")
    assert not result.ok
    assert "锁定" in result.message
    assert result.details.get("locked") is True


def test_lock_and_unlock_are_recorded_in_history(isolated_env, tmp_path):
    rt = runtime()
    rt.lock("npm", reason="cli")
    rt.unlock("npm")
    actions = [record.action for record in rt.history_records(manager="npm")]
    assert actions == ["unlock", "lock"]


def test_locks_are_written_under_the_data_home(isolated_env):
    rt = runtime()
    rt.lock("npm")
    path = Path(isolated_env["SRCMGR_DATA_HOME"]) / "locks.json"
    assert path.exists(), "the lock must live in the data dir, not the config"
    assert json.loads(path.read_text())["locked"][0]["manager"] == "npm"


# ---------------------------------------------------------------------------
# restoring one specific snapshot (the UI's 备份与还原 action)
# ---------------------------------------------------------------------------
def test_restore_backup_by_id(isolated_env, tmp_path, as_root):
    root = build_apt_tree(isolated_env)
    rt = runtime()
    created = rt.add("apt", source=Source(manager="apt", name="First",
                                          url="https://first.example.org/ubuntu/", suite="resolute"))
    assert created.ok
    first_files = sorted(p.name for p in (root / "sources.list.d").iterdir())

    second = rt.add("apt", source=Source(manager="apt", name="Second",
                                         url="https://second.example.org/ubuntu/", suite="resolute"))
    assert second.ok
    uid = rt.backups.last_for("apt").id

    result = rt.restore_backup(uid)
    assert result.ok, result.message
    assert sorted(p.name for p in (root / "sources.list.d").iterdir()) == first_files


def test_restore_backup_works_even_when_locked(isolated_env, tmp_path, as_root):
    build_apt_tree(isolated_env)
    rt = runtime()
    rt.add("apt", source=Source(manager="apt", name="First",
                                url="https://first.example.org/ubuntu/", suite="resolute"))
    uid = rt.backups.last_for("apt").id
    rt.lock("apt")
    result = rt.restore_backup(uid)
    assert result.ok, "restoring must stay possible while locked: " + result.message


def test_restore_backup_refuses_a_foreign_snapshot(isolated_env, tmp_path, as_root):
    build_apt_tree(isolated_env)
    rt = runtime()
    rt.add("apt", source=Source(manager="apt", name="First",
                                url="https://first.example.org/ubuntu/", suite="resolute"))
    uid = rt.backups.last_for("apt").id
    with pytest.raises(Exception):
        rt.restore_backup(uid, manager="npm")


def test_restore_backup_unknown_id_fails_cleanly(isolated_env):
    rt = runtime()
    with pytest.raises(Exception):
        rt.restore_backup("20200101-000000-apt-deadbeef")
