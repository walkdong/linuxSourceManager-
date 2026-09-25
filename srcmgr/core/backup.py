"""File snapshots and restore.

Before any manager plugin mutates a configuration file, the core takes a *snapshot*
of every file the plugin declares.  Snapshots are self-describing: the manifest
records the original path, content hash, unix mode and whether the file existed at
all, which is what makes a faithful rollback possible (including deleting files that
were created by an operation).

Layout::

    $SRCMGR_DATA_HOME/backups/
        20260924-124000-apt-a1b2c3/
            manifest.json
            files/etc/apt/sources.list.d/ubuntu.sources
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .api import FileChange, OperationResult
from .errors import BackupError
from .paths import atomic_write_bytes, backups_dir, ensure_dir, load_json, write_json


def utc_now() -> str:
    """ISO-8601 UTC timestamp with microsecond precision.

    Microseconds matter: two mutations inside the same second must still order
    correctly in the history log (they used to compare equal, so ``rollback`` could pick
    the wrong entry).
    """
    return datetime.now(timezone.utc).isoformat()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def short_hash(text: str, length: int = 6) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


@dataclass
class FileEntry:
    path: str
    existed: bool
    mode: int = 0o644
    sha256: str = ""
    size: int = 0
    stored_as: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "existed": self.existed,
            "mode": self.mode,
            "sha256": self.sha256,
            "size": self.size,
            "stored_as": self.stored_as,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileEntry":
        return cls(
            path=data["path"],
            existed=bool(data.get("existed", True)),
            mode=int(data.get("mode", 0o644)),
            sha256=data.get("sha256", ""),
            size=int(data.get("size", 0)),
            stored_as=data.get("stored_as", ""),
        )


@dataclass
class Manifest:
    id: str
    created_at: str
    manager: str
    reason: str = ""
    command: str = ""
    files: list[FileEntry] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    #: id of the backup this one replaced, for simple chaining
    parent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "manager": self.manager,
            "reason": self.reason,
            "command": self.command,
            "parent": self.parent,
            "notes": self.notes,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        return cls(
            id=data["id"],
            created_at=data.get("created_at", ""),
            manager=data.get("manager", ""),
            reason=data.get("reason", ""),
            command=data.get("command", ""),
            parent=data.get("parent", ""),
            notes=dict(data.get("notes", {})),
            files=[FileEntry.from_dict(item) for item in data.get("files", [])],
        )

    @property
    def existing(self) -> list[FileEntry]:
        return [f for f in self.files if f.existed]

    @property
    def created(self) -> list[FileEntry]:
        return [f for f in self.files if not f.existed]


class BackupStore:
    """Create, list and restore snapshots."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else backups_dir()

    # -- helpers -------------------------------------------------------------
    def _dir(self, backup_id: str) -> Path:
        return self.root / backup_id

    def _stored_path(self, backup_id: str, entry: FileEntry) -> Path:
        """Absolute path of the payload stored for ``entry`` inside ``backup_id``.

        ``stored_as`` is relative to the snapshot directory (``files/...``), so it must
        always be resolved against the snapshot that owns it - resolving it against the
        backup root silently produced "snapshot payload missing" on restore.
        """
        if not entry.stored_as:
            return self._dir(backup_id) / "missing-payload"
        return self._dir(backup_id) / entry.stored_as

    def manifest_path(self, backup_id: str) -> Path:
        return self._dir(backup_id) / "manifest.json"

    # -- create --------------------------------------------------------------
    def snapshot(
        self,
        manager: str,
        paths: Sequence[Path],
        *,
        reason: str = "",
        command: str = "",
        notes: dict[str, Any] | None = None,
    ) -> str:
        """Snapshot every existing (or to-be-created) path; return the backup id."""
        files: list[FileEntry] = []
        payloads: dict[str, tuple[bytes, int]] = {}
        seen: set[str] = set()
        expanded: list[Path] = []
        for path in paths:
            for item in _expand(path):
                key = str(item)
                if key not in seen:
                    seen.add(key)
                    expanded.append(item)

        for path in expanded:
            if path.is_dir():
                continue
            if path.exists():
                try:
                    data = path.read_bytes()
                    mode = path.stat().st_mode & 0o7777
                except OSError as exc:  # pragma: no cover - unreadable config
                    raise BackupError(f"cannot snapshot {path}: {exc}") from exc
                rel = _relative_key(path)
                payloads[rel] = (data, mode)
                files.append(
                    FileEntry(
                        path=str(path),
                        existed=True,
                        mode=mode,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size=len(data),
                        stored_as=f"files/{rel}",
                    )
                )
            else:
                files.append(FileEntry(path=str(path), existed=False))

        # The id must depend on what was captured, not only on the file names: two
        # snapshots of the same path taken in the same second (an add followed by a
        # rollback, a test suite, a rapid enable/disable pair) would otherwise collide
        # and silently overwrite each other's payload.
        fingerprint = "|".join(sorted(seen)) + "#" + "|".join(
            f"{rel}:{hashlib.sha256(data).hexdigest()[:12]}" for rel, (data, _) in sorted(payloads.items())
        )
        backup_id = f"{stamp()}-{manager}-{short_hash(fingerprint or manager)}"
        target = ensure_dir(self._dir(backup_id))
        store_root = ensure_dir(target / "files")
        for rel, (data, _mode) in payloads.items():
            destination = store_root / rel
            ensure_dir(destination.parent)
            destination.write_bytes(data)

        manifest = Manifest(
            id=backup_id,
            created_at=utc_now(),
            manager=manager,
            reason=reason,
            command=command,
            files=files,
            notes=dict(notes or {}),
        )
        write_json(self.manifest_path(backup_id), manifest.to_dict())
        self.prune()
        return backup_id

    # -- read ----------------------------------------------------------------
    def record_paths(self, backup_id: str, paths: Sequence[Path]) -> list[str]:
        """Record paths that appeared *after* a snapshot, so they can be rolled back.

        A manager plugin creates new drop-in files, and only knows their names once it
        has run.  Calling this right after the plugin returns lets the same snapshot
        own them, so ``restore`` deletes exactly what the change introduced.
        """
        manifest = self.get(backup_id)
        known = {entry.path for entry in manifest.files}
        added: list[str] = []
        for path in paths:
            for item in _expand(Path(path)):
                if str(item) in known or item.is_dir() or not item.exists():
                    continue
                data = item.read_bytes()
                rel = _relative_key(item)
                destination = self._dir(backup_id) / "files" / rel
                ensure_dir(destination.parent)
                destination.write_bytes(data)
                manifest.files.append(
                    FileEntry(
                        path=str(item),
                        existed=False,  # recorded only so rollback knows to remove it
                        mode=item.stat().st_mode & 0o7777,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size=len(data),
                        stored_as=f"files/{rel}",
                    )
                )
                known.add(str(item))
                added.append(str(item))
        if added:
            write_json(self.manifest_path(backup_id), manifest.to_dict())
        return added

    def list(self, *, manager: str | None = None, limit: int | None = None) -> list[Manifest]:
        if not self.root.exists():
            return []
        manifests: list[Manifest] = []
        for entry in sorted(self.root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            try:
                data = load_json(entry / "manifest.json")
            except Exception:  # noqa: BLE001 - a corrupt snapshot must not break listing
                continue
            if not data:
                continue
            manifest = Manifest.from_dict(data)
            if manager and manifest.manager != manager:
                continue
            manifests.append(manifest)
        manifests.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        return manifests[:limit] if limit else manifests

    def get(self, backup_id: str) -> Manifest:
        data = load_json(self.manifest_path(backup_id))
        if not data:
            raise BackupError(f"backup {backup_id!r} not found")
        return Manifest.from_dict(data)

    def last_for(self, manager: str) -> Manifest | None:
        items = self.list(manager=manager, limit=1)
        return items[0] if items else None

    # -- restore -------------------------------------------------------------
    def restore(self, backup_id: str, *, paths: Iterable[Path] | None = None) -> OperationResult:
        manifest = self.get(backup_id)
        wanted = {str(p) for p in paths} if paths else None
        changes: list[FileChange] = []
        errors: list[str] = []
        for entry in manifest.files:
            if wanted is not None and entry.path not in wanted:
                continue
            path = Path(entry.path)
            if entry.existed:
                source = self._stored_path(backup_id, entry)
                if source is None or not source.exists():
                    errors.append(f"{entry.path}: snapshot payload missing")
                    continue
                data = source.read_bytes()
                current = None
                if path.exists():
                    try:
                        current = hashlib.sha256(path.read_bytes()).hexdigest()
                    except OSError:
                        current = None
                if current == entry.sha256:
                    continue  # already identical, nothing to do
                try:
                    atomic_write_bytes(path, data, mode=entry.mode or None)
                except OSError as exc:
                    errors.append(f"{entry.path}: {exc}")
                    continue
                changes.append(FileChange(path=entry.path, action="restore", existed_before=True))
            else:
                if path.exists():
                    try:
                        path.unlink()
                    except OSError as exc:
                        errors.append(f"{entry.path}: cannot remove: {exc}")
                        continue
                    changes.append(
                        FileChange(path=entry.path, action="delete", existed_before=False)
                    )
        ok = not errors
        message = (
            f"restored {len(changes)} file(s) from {backup_id}"
            if ok
            else f"restored with {len(errors)} error(s): " + "; ".join(errors)
        )
        return OperationResult(
            ok=ok,
            message=message,
            changed=[c.path for c in changes],
            files=changes,
            backup_id=backup_id,
            details={"errors": errors},
        )

    # -- housekeeping --------------------------------------------------------
    def prune(self, keep: int | None = None) -> int:
        limit = keep if keep is not None else 50
        if limit <= 0:
            return 0
        items = self.list()
        removed = 0
        for manifest in items[limit:]:
            try:
                shutil.rmtree(self._dir(manifest.id))
                removed += 1
            except OSError:  # pragma: no cover - best effort
                continue
        return removed

    def delete(self, backup_id: str) -> None:
        target = self._dir(backup_id)
        if not target.exists():
            raise BackupError(f"backup {backup_id!r} not found")
        shutil.rmtree(target)


def _expand(path: Path) -> list[Path]:
    """Expand glob/dir specs into concrete file paths (non recursive for dirs we own).

    A directory entry means "snapshot the files inside it" (one level for
    ``*.d``-style drop-in dirs is handled by the caller passing a glob).
    """
    text = str(path)
    if any(ch in text for ch in "*?["):
        parent = Path(os.path.dirname(text) or ".")
        pattern = os.path.basename(text)
        if not parent.exists():
            return []
        return sorted(p for p in parent.glob(pattern) if p.is_file())
    return [path]


def _relative_key(path: Path) -> str:
    """Map an absolute path to a flat, collision-free key inside the snapshot."""
    return str(path).lstrip("/").replace("/", "__")


__all__ = ["BackupStore", "FileEntry", "Manifest", "short_hash", "stamp", "utc_now"]
