"""Operation history and rollback.

Every mutating operation appends one JSON line to ``history/operations.jsonl``::

    {"ts": "...", "action": "enable", "manager": "apt", "source": "Tsinghua",
     "source_id": "9f1c2b3d", "backup": "20260924-124000-apt-a1b2c3", "ok": true, ...}

The log is append-only (so it is cheap, atomic and never rewrites user data) and is
what powers ``srcmgr history`` and ``srcmgr rollback``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .backup import utc_now
from .errors import HistoryError
from .paths import ensure_dir, history_dir


@dataclass
class Record:
    ts: str
    action: str
    manager: str = ""
    source: str = ""
    source_id: str = ""
    url: str = ""
    backup: str = ""
    ok: bool = True
    message: str = ""
    command: str = ""
    user: str = ""
    host: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "action": self.action,
            "manager": self.manager,
            "source": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "backup": self.backup,
            "ok": self.ok,
            "message": self.message,
            "command": self.command,
            "user": self.user,
            "host": self.host,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        return cls(
            ts=data.get("ts", ""),
            action=data.get("action", ""),
            manager=data.get("manager", ""),
            source=data.get("source", ""),
            source_id=data.get("source_id", ""),
            url=data.get("url", ""),
            backup=data.get("backup", ""),
            ok=bool(data.get("ok", True)),
            message=data.get("message", ""),
            command=data.get("command", ""),
            user=data.get("user", ""),
            host=data.get("host", ""),
            details=dict(data.get("details", {})),
        )

    @property
    def when(self) -> str:
        """``YYYY-MM-DD HH:MM:SS`` (local time) for display."""
        try:
            moment = datetime.fromisoformat(self.ts)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self.ts


class HistoryStore:
    """Append-only operation log."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else history_dir()
        self.path = self.root / "operations.jsonl"

    # -- write ---------------------------------------------------------------
    def append(self, record: Record) -> Record:
        if not record.ts:
            record.ts = utc_now()
        if not record.user:
            record.user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
        if not record.host:
            try:
                import socket

                record.host = socket.gethostname()
            except Exception:  # noqa: BLE001 - hostname is cosmetic
                record.host = ""
        ensure_dir(self.root)
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return record

    def log(
        self,
        action: str,
        *,
        manager: str = "",
        source: str = "",
        source_id: str = "",
        url: str = "",
        backup: str = "",
        ok: bool = True,
        message: str = "",
        command: str = "",
        details: dict[str, Any] | None = None,
    ) -> Record:
        return self.append(
            Record(
                ts="",
                action=action,
                manager=manager,
                source=source,
                source_id=source_id,
                url=url,
                backup=backup,
                ok=ok,
                message=message,
                command=command,
                details=dict(details or {}),
            )
        )

    # -- read ----------------------------------------------------------------
    def read(self, *, manager: str | None = None, limit: int | None = None) -> list[Record]:
        if not self.path.exists():
            return []
        records: list[Record] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn last line must not break the history
                    if manager and data.get("manager") != manager:
                        continue
                    records.append(Record.from_dict(data))
        except OSError as exc:  # pragma: no cover - unreadable log
            raise HistoryError(f"cannot read history: {exc}") from exc
        # Newest first; a stable reverse keeps insertion order for identical timestamps
        # (old log files written before microseconds were recorded).
        records.sort(key=lambda r: r.ts)
        records.reverse()
        return records[:limit] if limit else records

    def last_backup(self, manager: str | None = None) -> str:
        """Most recent record that has an associated backup snapshot."""
        for record in self.read(manager=manager):
            if record.backup:
                return record.backup
        return ""

    def find(self, needle: str) -> Record | None:
        needle = needle.lower()
        for record in self.read():
            if needle in record.ts.lower() or needle == record.backup.lower():
                return record
        return None

    def clear(self) -> int:
        count = len(self.read())
        if self.path.exists():
            self.path.unlink()
        return count

    def export(self, destination: Path) -> Path:
        destination = Path(destination)
        ensure_dir(destination.parent)
        destination.write_text(
            "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in self.read())
            + ("\n" if self.read() else ""),
            encoding="utf-8",
        )
        return destination


def summarize(records: Iterable[Record]) -> dict[str, int]:
    out: dict[str, int] = {}
    for record in records:
        out[record.manager or "-"] = out.get(record.manager or "-", 0) + 1
    return out


__all__ = ["HistoryStore", "Record", "summarize"]
