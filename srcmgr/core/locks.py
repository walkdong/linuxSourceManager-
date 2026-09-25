"""Per-ecosystem locks — "do not change this one".

The lock is the safety net behind the beginner UI: a user who finally got their APT
sources working can pin them, and from then on **nothing** changes them — not the
one-click "switch to the fastest mirror", not the health checker, not a bulk action.
Unlocking is an explicit, visible act.

Design notes
------------
* The lock lives in its own file (``$SRCMGR_DATA_HOME/locks.json``) rather than in the
  user config, because it is *state* about this machine, not configuration, and it must
  survive a config reset.
* Reading never raises and never creates the file; a corrupt file degrades to "nothing
  is locked" instead of blocking the UI.
* Every mutation is atomic (see :func:`srcmgr.core.paths.atomic_write_text`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import LockedError
from .paths import atomic_write_text, data_home, ensure_dir


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class LockEntry:
    """One locked ecosystem."""

    manager: str
    locked_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"manager": self.manager, "locked_at": self.locked_at, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Any) -> "LockEntry | None":
        if isinstance(data, str):  # tolerate a bare list of names
            return cls(manager=data, locked_at="")
        if not isinstance(data, dict):
            return None
        manager = str(data.get("manager") or "").strip()
        if not manager:
            return None
        return cls(
            manager=manager,
            locked_at=str(data.get("locked_at") or ""),
            reason=str(data.get("reason") or ""),
        )


class LockStore:
    """JSON-backed set of locked ecosystems."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else data_home() / "locks.json"

    # -- reading -------------------------------------------------------------
    def _read(self) -> dict[str, LockEntry]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        items = raw.get("locked", raw) if isinstance(raw, dict) else raw
        if isinstance(items, dict):  # {"apt": {...}} form
            items = [dict(value, manager=key) if isinstance(value, dict) else key
                     for key, value in items.items()]
        out: dict[str, LockEntry] = {}
        for item in items if isinstance(items, list) else []:
            entry = LockEntry.from_dict(item)
            if entry is not None:
                out[entry.manager] = entry
        return out

    def _write(self, entries: dict[str, LockEntry]) -> None:
        ensure_dir(self.path.parent)
        payload = {
            "version": 1,
            "locked": [entries[name].to_dict() for name in sorted(entries)],
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    # -- queries -------------------------------------------------------------
    def is_locked(self, manager: str) -> bool:
        return manager in self._read()

    def entry(self, manager: str) -> LockEntry | None:
        return self._read().get(manager)

    def locked(self) -> list[str]:
        return sorted(self._read())

    def count(self) -> int:
        return len(self._read())

    def __contains__(self, manager: object) -> bool:
        return isinstance(manager, str) and self.is_locked(manager)

    def __iter__(self) -> Iterator[str]:
        return iter(self.locked())

    def snapshot(self) -> dict[str, str]:
        """``{manager: locked_at}`` — handy for the UI and for ``--json``."""
        return {name: entry.locked_at for name, entry in self._read().items()}

    # -- mutations -----------------------------------------------------------
    def lock(self, manager: str, *, reason: str = "") -> LockEntry:
        entries = self._read()
        entry = entries.get(manager) or LockEntry(manager=manager)
        entry.locked_at = entry.locked_at or _now()
        if reason:
            entry.reason = reason
        entries[manager] = entry
        self._write(entries)
        return entry

    def unlock(self, manager: str) -> bool:
        entries = self._read()
        if manager not in entries:
            return False
        del entries[manager]
        self._write(entries)
        return True

    def toggle(self, manager: str, *, reason: str = "") -> bool:
        """Toggle and return the new state (``True`` = now locked)."""
        if self.is_locked(manager):
            self.unlock(manager)
            return False
        self.lock(manager, reason=reason)
        return True

    def unlock_all(self) -> int:
        count = self.count()
        if count:
            self._write({})
        return count

    def prune(self, known: Iterable[str]) -> list[str]:
        """Drop locks for managers that no longer exist; return what was dropped."""
        known_set = set(known)
        entries = self._read()
        stale = [name for name in entries if name not in known_set]
        if stale:
            for name in stale:
                del entries[name]
            self._write(entries)
        return sorted(stale)


#: Actions a lock always blocks.  Restoring a backup is deliberately *not* here: it is the
#: documented escape hatch a locked ecosystem may still need.
LOCKED_ACTIONS = frozenset({"add", "remove", "enable", "disable", "update", "fallback"})


def assert_unlocked(
    manager: str,
    *,
    action: str,
    locks: LockStore | None = None,
    display_name: str = "",
) -> None:
    """Raise :class:`LockedError` when ``manager`` is locked and ``action`` is protected."""
    if action not in LOCKED_ACTIONS:
        return
    store = locks if locks is not None else LockStore()
    if not store.is_locked(manager):
        return
    label = display_name or manager
    raise LockedError(
        f"{label} 已锁定，不能修改（动作：{action}）",
        hint=f"先解锁：源管理器里点 🔒，或运行 srcmgr --cli unlock {manager}",
    )


__all__ = [
    "LOCKED_ACTIONS",
    "LockEntry",
    "LockStore",
    "assert_unlocked",
]
