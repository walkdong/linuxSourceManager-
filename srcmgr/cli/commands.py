"""Implementations of every CLI command.

Handlers receive ``(runtime, args, out)`` and return a process exit code.  Keeping them
here (instead of inside :mod:`srcmgr.cli.main`) makes the parser readable and lets the
commands be reused by a future GUI or by tests.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ..core.api import PluginType, Source, SourceKind
from ..core.config import Config, parse_value
from ..core.errors import (
    PluginError,
    SourceManagerError,
    SourceNotFound,
    UnsupportedOperation,
)
from ..core.health import best_source, probe_url, test_sources
from ..core.paths import config_file, describe_path
from ..core.plugins import (
    MANIFEST_NAME,
    install_plugin,
    scaffold_plugin,
    trust_plugin,
    uninstall_plugin,
)
from ..core.registry import normalize_url, source_matches
from ..core.runtime import SourceManagerRuntime, canonical_manager
from ..core.selection import selection_key, select_source
from .output import Output, fit, human_latency, shorten_url

CHINA_HOST_HINTS = (
    ".cn",
    "tuna.tsinghua",
    "ustc.edu.cn",
    "aliyun",
    "tencent",
    "huaweicloud",
    "sjtug",
    "nju.",
    "bfsu",
    "163.com",
    "daocloud",
    "1panel",
    "xuanyuan",
    "1ms.run",
)


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------
def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def _is_china(source: Source) -> bool:
    """Best-effort "is this a China-focused source?" test for the ``--china`` filter.

    The authoritative signal is the ``countries`` metadata a provider declares (which
    the registry also understands through ``registry.search(country="cn")``); the host
    hints are only a fallback for entries that omit that metadata.
    """
    declared = source.extra.get("countries") or source.extra.get("country") or []
    if isinstance(declared, str):
        declared = [declared]
    if declared and any(str(c).lower() in {"cn", "china", "zh"} for c in declared):
        return True
    text = " ".join(
        [
            source.url,
            source.name,
            source.provider,
            " ".join(str(v) for v in source.extra.values() if isinstance(v, (str, int))),
        ]
    ).lower()
    return any(hint in text for hint in CHINA_HOST_HINTS)


def _kind_of(source: Source) -> SourceKind:
    return source.kind if isinstance(source.kind, SourceKind) else SourceKind(str(source.kind))


def _ref(source: Source, index: int | None = None, installed: bool = False) -> str:
    """Short id shown in listings and accepted by enable/disable/remove."""
    if installed:
        prefix = f"#{index}" if index is not None else "#"
    else:
        return selection_key(source)
    return f"{prefix}:{source.id}"


def _state_label(marks: Sequence[str]) -> str:
    """Join state markers without duplicates, keeping the most specific last.

    ``state`` for an enabled source that is also the ecosystem default must read
    ``enabled,current`` - not ``enabled,disabled,current``.
    """
    seen: list[str] = []
    for mark in marks:
        if mark and mark not in seen:
            seen.append(mark)
    if "enabled" in seen:
        seen = [m for m in seen if m != "disabled"]
    return ",".join(seen) or "-"


def _source_rows(runtime: SourceManagerRuntime, sources: Sequence[Source]) -> list[list[str]]:
    manager = sources[0].manager if sources else ""
    installed = []
    if manager:
        try:
            installed = runtime.installed(manager)
        except SourceManagerError:
            installed = []
    current = None
    if manager:
        try:
            current = runtime.current(manager)
        except SourceManagerError:
            current = None
    rows: list[list[str]] = []
    for index, source in enumerate(sources, start=1):
        marks = []
        match = next((s for s in installed if source_matches(source, s)), None)
        if match is not None and match.enabled:
            marks.append("enabled")
        if not source.enabled:
            marks.append("disabled")
        if current is not None and source_matches(source, current):
            marks.append("current")
        rows.append(
            [
                f"#{index}",
                source.name,
                _host(source.url),
                source.suite or "-",
                _kind_of(source).value,
                _state_label(marks),
                source.id,
            ]
        )
    return rows


SOURCE_HEADERS = ["#", "name", "host", "suite", "kind", "state", "id"]
SOURCE_ALIGNS = ["right", "left", "left", "left", "left", "left", "left"]


def _print_sources(
    runtime: SourceManagerRuntime,
    out: Output,
    sources: Sequence[Source],
    *,
    title: str = "",
    show_url: bool = False,
) -> None:
    if title:
        out.print(out.strong(title))
    if not sources:
        out.print(out.dim("(no sources)"))
        return
    headers = list(SOURCE_HEADERS)
    rows = _source_rows(runtime, sources)
    if show_url:
        headers = ["#", "name", "url", "suite", "kind", "state", "id"]
        for row, source in zip(rows, sources):
            row[2] = shorten_url(source.url, 52)
    out.table(headers, rows, aligns=SOURCE_ALIGNS)


def _porcelain_sources(runtime: SourceManagerRuntime, sources: Sequence[Source]) -> list[str]:
    installed = []
    if sources:
        try:
            installed = runtime.installed(sources[0].manager)
        except SourceManagerError:
            installed = []
    lines = ["name\turl\tsuite\tkind\tenabled\tid\tprovider\tfile"]
    for source in sources:
        enabled = source.enabled or any(source_matches(source, s) and s.enabled for s in installed)
        lines.append(
            "\t".join(
                [
                    source.name,
                    source.url,
                    source.suite,
                    _kind_of(source).value,
                    "yes" if enabled else "no",
                    source.id,
                    source.provider,
                    source.file,
                ]
            )
        )
    return lines


def _source_detail(runtime: SourceManagerRuntime, out: Output, source: Source) -> None:
    current = None
    try:
        current = runtime.current(source.manager)
    except SourceManagerError:
        current = None
    health = source.health or {}
    fields = [
        ("manager", source.manager),
        ("name", source.name),
        ("id", source.id),
        ("url", source.url),
        ("suite", source.suite or "-"),
        ("components", " ".join(source.components) or "-"),
        ("types", " ".join(source.types) or "-"),
        ("architectures", " ".join(source.architectures) or "-"),
        ("kind", _kind_of(source).value),
        ("scope", source.scope.value if hasattr(source.scope, "value") else str(source.scope)),
        ("enabled", "yes" if source.enabled else "no"),
        ("current", "yes" if current and source_matches(source, current) else "no"),
        ("provider", source.provider or "-"),
        ("provider key", source.provider_key or "-"),
        ("file", describe_path(Path(source.file)) if source.file else "-"),
        ("health url", ", ".join(str(u) for u in (health.get("urls") or [])) or health.get("url", source.url)),
        ("description", source.description or "-"),
        ("homepage", source.homepage or "-"),
    ]
    extras = {k: v for k, v in source.extra.items() if k not in {"value"}}
    if extras:
        fields.append(("extra", json.dumps(extras, ensure_ascii=False, default=str)))
    for name, value in fields:
        out.print(f"{out.dim(name.ljust(13))} {value}")
    if source.secret_fields:
        out.print(out.dim("note          ") + "contains secret material; values are redacted in exports")


def _verdict(item: Mapping[str, Any]) -> str:
    """Status cell for a health result: ``ok``, ``ok*`` (host answered, no content) or the
    failure reason."""
    if not item.get("ok"):
        return str(item.get("status", "fail"))
    return "ok*" if item.get("status") == "reachable" else "ok"


def _confirm_change(out: Output, args: Any, question: str) -> bool:
    """Ask before a destructive change; ``--dry-run`` never prompts."""
    if getattr(args, "dry_run", False):
        return True
    return out.confirm(question, assume_yes=bool(getattr(args, "yes", False)))


def _report_operation(out: Output, result: Any, *, dry_run: bool = False) -> int:
    if result.ok:
        out.success(result.message)
        if result.backup_id:
            out.note(f"backup: {result.backup_id} (undo with 'srcmgr rollback <manager>')")
        for path in result.changed[:10]:
            out.note(f"changed: {describe_path(Path(path))}")
        return 0
    out.failure(result.message)
    if result.details.get("hint"):
        out.error(str(result.details["hint"]))
    return 5


def _manager_name(args: Any) -> str:
    name = getattr(args, "manager", None)
    if not name:
        raise SourceManagerError("internal error: no manager given")
    return canonical_manager(name)


def _add_extra_kwargs(args: Any) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for item in getattr(args, "extra", []) or []:
        if "=" not in item:
            raise SourceManagerError(f"--extra expects key=value, got {item!r}")
        key, _, value = item.partition("=")
        extra[key.strip()] = parse_value(value)
    return extra


def _kind_filter(args: Any) -> list[SourceKind]:
    kinds = []
    for value in getattr(args, "kind", None) or []:
        try:
            kinds.append(SourceKind(str(value).lower()))
        except ValueError:
            continue
    return kinds


def _filter_sources(
    sources: Sequence[Source],
    args: Any,
    *,
    china: bool | None = None,
) -> list[Source]:
    out = list(sources)
    kinds = _kind_filter(args)
    if kinds:
        out = [s for s in out if _kind_of(s) in kinds]
    if china if china is not None else getattr(args, "china", False):
        out = [s for s in out if _is_china(s)]
    query = (getattr(args, "search", None) or "").strip().lower()
    if query:
        out = [
            s
            for s in out
            if query in s.name.lower() or query in s.url.lower() or query in s.provider.lower()
        ]
    if getattr(args, "enabled", False):
        out = [s for s in out if s.enabled]
    if getattr(args, "disabled", False):
        out = [s for s in out if not s.enabled]
    limit = getattr(args, "limit", None)
    if limit:
        out = out[:limit]
    return out


# ---------------------------------------------------------------------------
# top-level commands
# ---------------------------------------------------------------------------
def managers(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    statuses = runtime.statuses()
    if getattr(args, "available_only", False):
        statuses = [s for s in statuses if s.available]
    payload = [s.to_dict() for s in statuses]

    def render(items: list[dict[str, Any]]) -> None:
        out.table(
            ["manager", "status", "scope", "sources", "plugin", "description"],
            [
                [
                    item["name"],
                    "ready" if item["available"] else f"- ({item['reason'] or 'off'})",
                    item["scope"],
                    item["installed_count"] if item["available"] else "-",
                    item["version"],
                    fit(item["description"], 48),
                ]
                for item in items
            ],
            aligns=["left", "left", "left", "right", "left", "left"],
            empty="no managers found - is the plugins directory intact?",
        )
        broken = [item for item in items if item["error"]]
        for item in broken:
            out.warning(f"{item['name']}: {item['error']}")

    out.emit(payload, text_renderer=render)
    return 0


def sources(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    managers_filter = [canonical_manager(m) for m in (getattr(args, "manager", None) or [])]
    names = managers_filter or runtime.manager_names()
    payload: dict[str, Any] = {}
    entries: list[tuple[str, Source]] = []
    for name in names:
        try:
            runtime.plugin(name)
        except SourceManagerError as exc:
            out.note(f"skipping {name}: {exc.message}")
            continue
        try:
            items = runtime.installed(name)
        except SourceManagerError as exc:
            out.warning(f"{name}: {exc.message}")
            items = []
        items = _filter_sources(items, args)
        available: list[Source] = []
        if getattr(args, "available", False):
            available = _filter_sources(runtime.available(name), args)
        payload[name] = {
            "installed": [s.to_dict() for s in items],
            "available": [s.to_dict(redact=True) for s in available],
        }
        entries += [(name, s) for s in items]
        entries += [(name, s) for s in available]

    def render(_: Any) -> None:
        if not entries:
            out.print(out.dim("(no sources matched)"))
            return
        out.table(
            ["manager", "name", "host", "suite", "kind", "state", "id"],
            [
                [
                    name,
                    source.name,
                    _host(source.url),
                    source.suite or "-",
                    _kind_of(source).value,
                    "enabled" if source.enabled else "available",
                    source.id,
                ]
                for name, source in entries
            ],
            aligns=["left", "left", "left", "left", "left", "left", "left"],
        )

    out.emit(payload, text_renderer=render)
    return 0


def test(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    what = list(getattr(args, "what", []) or [])
    manager = _manager_name(args) if getattr(args, "manager", None) else None
    if what and manager is None:
        candidate = canonical_manager(what[0])
        if candidate in runtime.manager_names() or candidate in runtime.loaded_managers():
            manager = candidate
            what = what[1:]
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        runtime.config.set("health.timeout", float(timeout))

    results = []
    if manager:
        if what:
            for spec in what:
                results += runtime.test(manager, spec)
        else:
            results += runtime.test(manager, scope="available" if args.all else "enabled")
        if getattr(args, "native", False):
            try:
                results.append(runtime.native_test(manager))
            except Exception as exc:  # noqa: BLE001 - native check is optional
                out.warning(f"{manager}: native check unavailable ({exc})")
    else:
        names = [s.name for s in runtime.statuses(only_detected=True)]
        for name in names:
            try:
                results += runtime.test(name, scope="available" if args.all else "enabled")
            except SourceManagerError as exc:
                out.warning(f"{name}: {exc.message}")

    if getattr(args, "json_out", None):
        Path(args.json_out).expanduser().write_text(
            json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        out.note(f"wrote {args.json_out}")

    def render(items: list[dict[str, Any]]) -> None:
        out.table(
            ["status", "manager", "source", "latency", "http", "tls", "url"],
            [
                [
                    _verdict(item),
                    item["manager"],
                    item["name"],
                    human_latency(item["latency_ms"]),
                    item["http_status"] or "-",
                    "-" if item["tls_ok"] is None else ("yes" if item["tls_ok"] else "no"),
                    shorten_url(item["url"], 44),
                ]
                for item in items
            ],
            aligns=["left", "left", "left", "right", "right", "left", "left"],
            empty="nothing to test",
        )
        failed = [item for item in items if not item["ok"]]
        if failed:
            out.print("")
            for item in failed:
                out.warning(f"{item['manager']}/{item['name']}: {item['error'] or item['status']}")
        weak = [item for item in items if item["ok"] and item["status"] == "reachable"]
        if weak:
            out.note(
                "some hosts answered without content at the probed path - normal for "
                "registry roots such as dl.google.com"
            )

    payload = [r.to_dict() for r in results]
    out.emit(payload, text_renderer=render)
    return 0 if all(r.ok for r in results) or not results else 6


def health(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    interval = getattr(args, "watch", None)
    while True:
        code = _health_once(runtime, args, out)
        if not interval:
            return code
        try:
            time.sleep(float(interval))
        except KeyboardInterrupt:
            out.print("")
            return code


def _health_once(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    payload: dict[str, Any] = {"generated_at": None, "managers": []}
    from ..core.backup import utc_now

    payload["generated_at"] = utc_now()
    failures: list[str] = []
    fallbacks: list[ChangePlan] = []
    rows: list[list[str]] = []
    details: list[tuple[str, list[Any]]] = []

    for status in runtime.statuses():
        if not status.available:
            continue
        scope = "available" if getattr(args, "all", False) else "enabled"
        try:
            results = runtime.test(status.name, scope=scope)
        except SourceManagerError as exc:
            rows.append([status.name, "error", "-", "-", exc.message])
            continue
        ok_count = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        latencies = [r.latency_ms for r in results if r.ok and r.latency_ms is not None]
        average = round(sum(latencies) / len(latencies), 1) if latencies else None
        verdict = "ok" if not bad else ("degraded" if ok_count else "down")
        weak_count = sum(1 for r in results if r.ok and r.status == "reachable")
        if verdict == "ok" and weak_count:
            verdict = f"ok ({weak_count} weak)"
        rows.append(
            [
                status.name,
                verdict,
                f"{ok_count}/{len(results)}",
                human_latency(average),
                ", ".join(f"{r.name} ({r.status})" for r in bad[:3]) or "-",
            ]
        )
        payload["managers"].append(
            {
                "manager": status.name,
                "verdict": verdict,
                "ok": ok_count,
                "total": len(results),
                "average_latency_ms": average,
                "failures": [r.to_dict() for r in bad],
                "results": [r.to_dict() for r in results],
            }
        )
        failures += [f"{status.name}/{r.name}" for r in bad]
        details.append((status.name, results))
        if getattr(args, "fallback", False):
            plan = runtime.fallback_plan(status.name, results=results)
            fallbacks.append(plan)
            payload["managers"][-1]["fallback"] = plan.to_dict()

    def render(_: Any) -> None:
        out.table(
            ["manager", "status", "healthy", "avg latency", "problems"],
            rows,
            aligns=["left", "left", "right", "right", "left"],
        )
        for name, results in details:
            bad = [r for r in results if not r.ok]
            if bad:
                out.print("")
                out.print(out.strong(f"{name}: failed sources"))
                out.table(
                    ["source", "status", "http", "error"],
                    [[r.name, r.status, r.http_status or "-", fit(r.error, 60)] for r in bad],
                    aligns=["left", "left", "right", "left"],
                )
        if fallbacks:
            out.print("")
            out.print(out.strong("fallback plan"))
            for plan in fallbacks:
                if plan.to_source:
                    out.print(f"  {plan.manager}: {plan.from_source} -> {plan.to_source} ({plan.reason})")
                else:
                    out.print(out.dim(f"  {plan.manager}: {plan.reason}"))

    out.emit(payload, text_renderer=render)
    return 0 if not failures else 6


def fallback(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = getattr(args, "manager", None)
    if getattr(args, "enable", False):
        if manager:
            runtime.config.set(f"managers.{canonical_manager(manager)}.fallback.enabled", True)
            out.success(f"automatic fallback enabled for {canonical_manager(manager)}")
        else:
            runtime.config.set("fallback.enabled", True)
            out.success("automatic fallback enabled for all managers")
        out.note("it only acts when you run 'srcmgr health --fallback' or 'srcmgr fallback --apply'")
        return 0
    if getattr(args, "disable", False):
        if manager:
            runtime.config.set(f"managers.{canonical_manager(manager)}.fallback.enabled", False)
            out.success(f"automatic fallback disabled for {canonical_manager(manager)}")
        else:
            runtime.config.set("fallback.enabled", False)
            out.success("automatic fallback disabled")
        return 0

    names = [canonical_manager(manager)] if manager else [
        s.name for s in runtime.statuses(only_detected=True)
    ]
    plans: list[dict[str, Any]] = []
    exit_code = 0
    for name in names:
        plan = runtime.fallback_plan(name)
        plans.append(plan.to_dict())
        if getattr(args, "apply", False):
            if plan.to_source:
                result = runtime.apply_fallback(name, plan, dry_run=bool(getattr(args, "dry_run", False)))
                _report_operation(out, result)
                exit_code = exit_code or (0 if result.ok else 5)
            else:
                out.note(f"{name}: {plan.reason}")
    if not getattr(args, "apply", False):
        def render(items: list[dict[str, Any]]) -> None:
            out.table(
                ["manager", "current", "candidate", "reason"],
                [
                    [item["manager"], item["from"] or "-", item["to"] or "-", fit(item["reason"], 64)]
                    for item in items
                ],
                aligns=["left", "left", "left", "left"],
            )

        out.emit(plans, text_renderer=render)
    return exit_code


def history(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = canonical_manager(args.manager) if getattr(args, "manager", None) else None
    if getattr(args, "export", None):
        records = runtime.history_records(manager=manager, limit=None)
        target = runtime.history.export(Path(args.export).expanduser())
        out.success(f"exported {len(records)} record(s) to {describe_path(target)}")
        return 0
    if getattr(args, "show", None):
        record = runtime.history.find(args.show)
        if record is None:
            raise SourceManagerError(f"no history entry matching {args.show!r}")
        payload = record.to_dict()
        if out.json_mode:
            out.json(payload)
            return 0
        for key, value in payload.items():
            if key == "details":
                out.print(out.dim("details"))
                out.print(json.dumps(value, indent=2, ensure_ascii=False))
            else:
                out.print(f"{out.dim(str(key).ljust(10))} {value}")
        return 0

    limit = None if getattr(args, "all", False) else int(getattr(args, "limit", 25) or 25)
    records = runtime.history_records(manager=manager, limit=limit)
    payload = [r.to_dict() for r in records]

    def render(items: list[dict[str, Any]]) -> None:
        out.table(
            ["when", "action", "manager", "source", "backup", "ok"],
            [
                [
                    _fmt_ts(item["ts"]),
                    item["action"],
                    item["manager"] or "-",
                    item["source"] or "-",
                    item["backup"] or "-",
                    "yes" if item["ok"] else "no",
                ]
                for item in items
            ],
            aligns=["left", "left", "left", "left", "left", "left"],
            empty="no changes recorded yet",
        )

    out.emit(payload, text_renderer=render)
    return 0


def _fmt_ts(value: str) -> str:
    try:
        from datetime import datetime, timezone

        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return value


def backup(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = canonical_manager(args.manager) if getattr(args, "manager", None) else None
    if getattr(args, "prune", None) is not None:
        removed = runtime.backups.prune(keep=int(args.prune))
        out.success(f"pruned {removed} snapshot(s)")
        return 0
    if getattr(args, "delete", None):
        runtime.backups.delete(args.delete)
        out.success(f"deleted snapshot {args.delete}")
        return 0
    if getattr(args, "show", None):
        manifest = runtime.backups.get(args.show)
        payload = manifest.to_dict()
        if out.json_mode:
            out.json(payload)
            return 0
        out.print(f"{out.dim('id'.ljust(12))} {manifest.id}")
        out.print(f"{out.dim('manager'.ljust(12))} {manifest.manager}")
        out.print(f"{out.dim('created'.ljust(12))} {manifest.created_at}")
        out.print(f"{out.dim('reason'.ljust(12))} {manifest.reason or '-'}")
        out.print(f"{out.dim('command'.ljust(12))} {manifest.command or '-'}")
        out.table(
            ["file", "existed", "mode", "size", "sha256"],
            [
                [
                    describe_path(Path(entry.path)),
                    "yes" if entry.existed else "no",
                    oct(entry.mode),
                    entry.size,
                    entry.sha256[:12],
                ]
                for entry in manifest.files
            ],
            aligns=["left", "left", "left", "right", "left"],
            empty="snapshot contains no files",
        )
        return 0
    if getattr(args, "list", False) or not manager:
        manifests = runtime.backups_list(manager)
        payload = [m.to_dict() for m in manifests]

        def render(items: list[dict[str, Any]]) -> None:
            out.table(
                ["snapshot", "created", "manager", "files", "reason"],
                [
                    [
                        item["id"],
                        _fmt_ts(item["created_at"]),
                        item["manager"],
                        len(item["files"]),
                        fit(item.get("reason", ""), 40),
                    ]
                    for item in items
                ],
                aligns=["left", "left", "left", "right", "left"],
                empty="no snapshots yet",
            )

        out.emit(payload, text_renderer=render)
        return 0
    backup_id = runtime.backup(manager, reason=args.reason if hasattr(args, "reason") else "manual")
    if out.json_mode:
        out.json({"backup_id": backup_id, "manager": manager})
    else:
        out.success(f"snapshot {backup_id} created for {manager}")
    return 0


def rollback(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = canonical_manager(args.manager)
    if manager == "all":
        exit_code = 0
        for name in runtime.manager_names():
            try:
                result = runtime.rollback(name, dry_run=bool(getattr(args, "dry_run", False)))
            except SourceManagerError as exc:
                out.note(f"{name}: {exc.message}")
                continue
            if result.ok:
                out.success(f"{name}: {result.message}")
            else:
                out.note(f"{name}: {result.message}")
        return exit_code
    result = runtime.rollback(manager, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


def restore(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = canonical_manager(args.manager) if getattr(args, "manager", None) else ""
    if not manager:
        manifest = runtime.backups.get(args.backup_id)
        manager = manifest.manager
    result = runtime.restore(manager, args.backup_id)
    return _report_operation(out, result)


def doctor(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    report = runtime.doctor()
    if getattr(args, "health", False):
        health_report = []
        for status in runtime.statuses(only_detected=True):
            try:
                results = runtime.test(status.name, scope="enabled")
            except SourceManagerError:
                continue
            health_report.append(
                {
                    "manager": status.name,
                    "ok": sum(1 for r in results if r.ok),
                    "total": len(results),
                    "failures": [r.name for r in results if not r.ok],
                }
            )
        report["health"] = health_report

    def render(data: dict[str, Any]) -> None:
        out.print(out.strong("environment"))
        out.table(
            ["key", "value"],
            [
                ["python", data["python"]],
                ["platform", fit(data["platform"], 60)],
                ["running as root", "yes" if data["root"] else "no"],
                ["config file", describe_path(Path(data["config_file"]))],
                ["managers loaded", len(data["managers"])],
                ["providers loaded", len(data["providers"])],
            ],
            aligns=["left", "left"],
        )
        out.print("")
        out.print(out.strong("managers"))
        out.table(
            ["manager", "usable", "writable", "sources", "config paths"],
            [
                [
                    item["name"],
                    "yes" if item["available"] else "no",
                    "yes" if item["writable"] else "no",
                    item["installed_count"],
                    fit(", ".join(describe_path(Path(p)) for p in item["config_files"]) or "-", 60),
                ]
                for item in data["managers"]
            ],
            aligns=["left", "left", "left", "right", "left"],
        )
        out.print("")
        out.print(out.strong("providers"))
        out.table(
            ["provider", "sources", "managers", "bundled"],
            [
                [
                    item["display_name"],
                    item["sources"],
                    ", ".join(item["managers"]),
                    "yes" if item["bundled"] else "no",
                ]
                for item in data["providers"]
            ],
            aligns=["left", "right", "left", "left"],
        )
        if data.get("missing_commands"):
            out.print("")
            out.print(out.strong("missing commands"))
            for item in data["missing_commands"]:
                out.warning(f"{item['manager']}: {item['command']} not on PATH")
        if data.get("unwritable_paths"):
            out.print("")
            out.print(out.strong("paths needing root"))
            for path in sorted(set(data["unwritable_paths"])):
                out.print(f"  {describe_path(Path(path))}")
        if data.get("health"):
            out.print("")
            out.print(out.strong("health"))
            out.table(
                ["manager", "healthy", "failing"],
                [
                    [item["manager"], f"{item['ok']}/{item['total']}", ", ".join(item["failures"]) or "-"]
                    for item in data["health"]
                ],
                aligns=["left", "right", "left"],
            )
        if data.get("warnings"):
            out.print("")
            out.print(out.strong("warnings"))
            for warning in data["warnings"]:
                out.warning(warning)

    out.emit(report, text_renderer=render)
    return 0


# ---------------------------------------------------------------------------
# per-manager commands
# ---------------------------------------------------------------------------
def manager_list(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    plugin = runtime.plugin(manager)
    installed = runtime.installed(manager)
    available = runtime.available(manager) if getattr(args, "available", False) else []

    # `srcmgr apt list tuna` behaves like `srcmgr apt list --search tuna`
    specs = list(getattr(args, "spec", []) or [])
    if specs:
        query = " ".join(specs)
        setattr(args, "search", query)

    filtered_installed = _filter_sources(installed, args)
    filtered_available = _filter_sources(available, args) if available else []
    if out.json_mode:
        out.json(
            {
                "manager": manager,
                "config_files": [str(p) for p in plugin.config_files()],
                "installed": [s.to_dict(redact=True) for s in filtered_installed],
                "available": [s.to_dict(redact=True) for s in filtered_available],
            }
        )
        return 0
    if out.porcelain:
        for line in _porcelain_sources(runtime, filtered_installed):
            print(line)
        return 0

    status = runtime.status(manager)
    out.print(
        out.strong(f"{status.display_name} sources")
        + out.dim(f"  ({len(installed)} installed, {len(available) or 'catalog'} known)")
    )
    for path in status.config_files:
        out.print(out.dim(f"  config: {describe_path(Path(path))}"))
    if not status.available:
        out.warning(f"not detected on this machine ({status.reason})")
    out.print("")
    _print_sources(runtime, out, filtered_installed, title="installed")
    if available:
        out.print("")
        _print_sources(runtime, out, filtered_available, title="available (not installed)")
    current = runtime.current(manager)
    if current:
        out.print("")
        out.print(f"current: {out.accent(current.name)} {out.dim(current.url)}")
    return 0


def manager_available(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    available = _filter_sources(runtime.available(manager), args)
    if out.json_mode:
        out.json([s.to_dict(redact=True) for s in available])
        return 0
    if out.porcelain:
        for line in _porcelain_sources(runtime, available):
            print(line)
        return 0
    groups = runtime.registry.groups_for(manager)
    if not groups:
        # Managers without a mirror catalogue (snap) still expose their upstream store
        # through the plugin, so show that instead of an empty "no providers" line.
        if available:
            out.print(
                out.strong(f"{manager} upstream")
                + out.dim("  [declared by the manager plugin, no provider needed]")
            )
            _print_sources(runtime, out, available)
        else:
            out.print(out.dim("no source providers for this manager"))
        return 0
    for group in groups:
        items = _filter_sources(group.sources, args)
        if not items:
            continue
        out.print(out.strong(f"{group.display_name}") + out.dim(f"  [{group.name} {group.version}]"))
        if group.description:
            out.print(out.dim(f"  {group.description}"))
        _print_sources(runtime, out, items)
        out.print("")
    return 0


def manager_current(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    current = runtime.current(manager)
    if current is None:
        out.print(out.dim(f"{manager}: no active source detected"))
        return 0
    if out.json_mode:
        out.json(current.to_dict(redact=True))
        return 0
    _source_detail(runtime, out, current)
    return 0


def manager_add(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    spec = getattr(args, "spec", None)
    if not spec and not getattr(args, "url", None):
        raise SourceManagerError("nothing to add", hint=f"try: srcmgr {manager} available")

    extra = _add_extra_kwargs(args)
    components = None
    if getattr(args, "components", None):
        components = [c.strip() for c in str(args.components).split(",") if c.strip()]
    source: Source | None = None
    if spec and not getattr(args, "url", None):
        try:
            source = runtime.resolve_available(manager, spec)
            out.note(f"resolved {spec!r} to {source.name} ({source.url})")
        except SourceNotFound:
            if spec.startswith(("http://", "https://", "ppa:")):
                setattr(args, "url", spec)
            else:
                raise
    if source is None and not getattr(args, "dry_run", False):
        custom = str(getattr(args, "url"))
        if not _confirm_change(out, args, f"add custom source {custom} to {manager}?"):
            out.print("aborted")
            return 0
    result = runtime.add(
        manager,
        source=source,
        url=None if source else str(getattr(args, "url")),
        suite=getattr(args, "suite", None),
        components=components,
        name=getattr(args, "name", None),
        priority=getattr(args, "priority", None),
        extra=extra,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    return _report_operation(out, result)


def manager_enable(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    spec = args.spec
    if not _confirm_change(out, args, f"enable {spec} for {manager}?"):
        out.print("aborted")
        return 0
    result = runtime.enable(
        manager,
        spec,
        dry_run=bool(getattr(args, "dry_run", False)),
        refresh=bool(getattr(args, "refresh", False)),
    )
    return _report_operation(out, result)


def manager_disable(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    if not _confirm_change(out, args, f"disable {args.spec} for {manager}?"):
        out.print("aborted")
        return 0
    result = runtime.disable(manager, args.spec, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


def manager_remove(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    if not _confirm_change(out, args, f"remove {args.spec} from {manager}?"):
        out.print("aborted")
        return 0
    result = runtime.remove(manager, args.spec, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


def manager_edit(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    changes: dict[str, Any] = {}
    if getattr(args, "url", None):
        changes["url"] = args.url
    if getattr(args, "suite", None):
        changes["suite"] = args.suite
    if getattr(args, "components", None):
        changes["components"] = [c.strip() for c in str(args.components).split(",") if c.strip()]
    if getattr(args, "name", None):
        changes["name"] = args.name
    if not changes:
        raise SourceManagerError("nothing to change", hint="pass --url/--suite/--components/--name")
    result = runtime.edit(manager, args.spec, changes, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


def manager_test(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    if getattr(args, "timeout", None) is not None:
        runtime.config.set("health.timeout", float(args.timeout))
    specs = list(getattr(args, "spec", []) or [])
    results = []
    sources: list[Source] = []
    try:
        if specs:
            for spec in specs:
                found = runtime.test(manager, spec)
                results += found
        else:
            scope = "available" if getattr(args, "all", False) else "enabled"
            results = runtime.test(manager, scope=scope)
            sources = runtime.installed(manager) if scope == "enabled" else runtime.available(manager)
    except UnsupportedOperation:
        raise
    if getattr(args, "china", False):
        results = [
            r
            for r in results
            if any(_is_china(s) for s in sources if s.id == r.source_id) or not sources
        ]
    if getattr(args, "native", False):
        try:
            results.append(runtime.native_test(manager))
        except Exception as exc:  # noqa: BLE001
            out.warning(f"native check unavailable: {exc}")
    if getattr(args, "json_out", None):
        Path(args.json_out).expanduser().write_text(
            json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False), encoding="utf-8"
        )
    payload = [r.to_dict() for r in results]

    def render(items: list[dict[str, Any]]) -> None:
        out.table(
            ["status", "source", "latency", "http", "tls", "checked", "detail"],
            [
                [
                    _verdict(item),
                    item["name"],
                    human_latency(item["latency_ms"]),
                    item["http_status"] or "-",
                    "-" if item["tls_ok"] is None else ("yes" if item["tls_ok"] else "no"),
                    _fmt_ts(item["checked_at"]) if item["checked_at"] else "-",
                    fit(item["error"] or item["detail"], 40),
                ]
                for item in items
            ],
            aligns=["left", "left", "right", "right", "left", "left", "left"],
            empty="no sources to test",
        )
        if any(item["ok"] and item["status"] == "reachable" for item in items):
            out.note("ok* = the host answered but served no content at the probed path")

    out.emit(payload, text_renderer=render)
    return 0 if all(r.ok for r in results) else 6


def manager_choose(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    candidates = _filter_sources(
        runtime.available(manager),
        args,
    )
    if getattr(args, "china", False) is False and not _kind_filter(args):
        pass
    limit = int(getattr(args, "limit", 12) or 12)
    candidates = candidates[:limit]
    if not candidates:
        out.print(out.dim(f"{manager}: no candidate mirrors in the registry"))
        return 0
    out.print(f"measuring {len(candidates)} {manager} mirror(s)…")
    results = test_sources(candidates, config=runtime.config, manager=manager)
    ranked = runtime.rank(results, candidates)
    if out.json_mode:
        out.json(ranked)
        return 0
    out.table(
        ["#", "mirror", "latency", "status", "http", "tls", "url"],
        [
            [
                item["rank"],
                item["name"],
                human_latency(item["latency_ms"]),
                item["status"],
                item["http_status"] or "-",
                "-" if item["tls_ok"] is None else ("yes" if item["tls_ok"] else "no"),
                shorten_url(item["url"], 40),
            ]
            for item in ranked
        ],
        aligns=["right", "left", "right", "left", "right", "left", "left"],
    )
    reachable = [item for item in ranked if item["ok"]]
    if not reachable:
        out.failure("no mirror answered")
        return 6
    if not sys.stdin.isatty() or getattr(args, "yes", False):
        chosen = reachable[0]
        out.note(f"selecting the fastest reachable mirror: {chosen['name']}")
    else:
        try:
            answer = input(f"enable which mirror? [1-{len(ranked)}] (default 1, empty to abort) ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if not answer:
            out.print("aborted")
            return 0
        try:
            index = int(answer)
        except ValueError:
            out.error("expected a number")
            return 2
        match = next((item for item in ranked if item["rank"] == index), None)
        if match is None:
            out.error(f"no such entry: {index}")
            return 2
        chosen = match
    if not chosen["ok"]:
        if not _confirm_change(out, args, f"{chosen['name']} did not answer - enable anyway?"):
            out.print("aborted")
            return 0
    result = runtime.enable(manager, chosen["id"])
    return _report_operation(out, result)


def manager_refresh(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    result = runtime.refresh(manager, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


def manager_backup(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    if getattr(args, "list", False):
        manifests = runtime.backups_list(manager)
        if out.json_mode:
            out.json([m.to_dict() for m in manifests])
            return 0
        out.table(
            ["snapshot", "created", "files", "reason"],
            [
                [m.id, _fmt_ts(m.created_at), len(m.files), fit(m.reason, 40)]
                for m in manifests
            ],
            aligns=["left", "left", "right", "left"],
            empty=f"no snapshots for {manager}",
        )
        return 0
    backup_id = runtime.backup(manager, reason=getattr(args, "reason", "manual"))
    if out.json_mode:
        out.json({"backup_id": backup_id, "manager": manager})
    else:
        out.success(f"snapshot {backup_id} created for {manager}")
    return 0


def manager_restore(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    backup_id = args.backup_id
    if not backup_id:
        manifest = runtime.backups.last_for(manager)
        if manifest is None:
            raise SourceManagerError(f"no snapshot available for {manager}")
        backup_id = manifest.id
    if not _confirm_change(out, args, f"restore {manager} configuration from {backup_id}?"):
        out.print("aborted")
        return 0
    result = runtime.restore(manager, backup_id)
    return _report_operation(out, result)


def manager_rollback(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = _manager_name(args)
    result = runtime.rollback(manager, dry_run=bool(getattr(args, "dry_run", False)))
    return _report_operation(out, result)


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------
def plugins_list(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    wanted = getattr(args, "type", None)
    manifests = runtime.all_manifests()
    if wanted:
        manifests = [m for m in manifests if m.type.value == wanted]
    loaded_managers = runtime.loaded_managers()
    loaded_providers = runtime.loaded_providers()
    rows = []
    payload = []
    for manifest in sorted(manifests, key=lambda m: (m.type.value, m.name)):
        loaded = (
            loaded_managers.get(manifest.name)
            if manifest.type is PluginType.MANAGER
            else loaded_providers.get(manifest.name)
        )
        state = "broken" if (manifest.present and loaded is None) else "installed"
        if not manifest.present:
            state = "invalid"
        elif loaded is not None and loaded.error:
            state = "error"
        elif loaded is not None and loaded.skipped:
            state = f"skipped ({loaded.skipped})"
        elif not manifest.bundled:
            state = "installed" if (loaded is not None and loaded.ok) else "untrusted"
        origin = "bundled" if manifest.bundled else "user"
        rows.append(
            [
                manifest.name,
                manifest.type.value,
                manifest.version,
                origin,
                state,
                fit(manifest.description, 46),
            ]
        )
        payload.append(
            {
                "name": manifest.name,
                "type": manifest.type.value,
                "version": manifest.version,
                "bundled": manifest.bundled,
                "state": state,
                "managers": manifest.managers,
                "platforms": manifest.platforms,
                "config_paths": manifest.config_paths,
                "base_dir": manifest.base_dir,
                "description": manifest.description,
            }
        )

    def render(_: Any) -> None:
        out.table(
            ["plugin", "type", "version", "origin", "state", "description"],
            rows,
            aligns=["left", "left", "left", "left", "left", "left"],
        )
        untrusted = [item for item in payload if item["state"] == "untrusted"]
        if untrusted:
            out.print("")
            out.warning(
                "untrusted plugins are not executed: "
                + ", ".join(item["name"] for item in untrusted)
            )
            out.note("enable one with: srcmgr plugins trust <name>")

    out.emit(payload, text_renderer=render)
    return 0


def plugins_info(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    name = args.name
    matches = [m for m in runtime.all_manifests() if m.name == name or m.display_name == name]
    if not matches:
        raise PluginError(f"plugin {name!r} not found")
    manifest = matches[0]
    loaded_managers = runtime.loaded_managers()
    loaded_providers = runtime.loaded_providers()
    loaded = loaded_managers.get(name) or loaded_providers.get(name)
    payload = {
        "name": manifest.name,
        "type": manifest.type.value,
        "display_name": manifest.display_name,
        "version": manifest.version,
        "description": manifest.description,
        "author": manifest.author,
        "license": manifest.license,
        "homepage": manifest.homepage,
        "bundled": manifest.bundled,
        "platforms": manifest.platforms,
        "requires": manifest.requires,
        "supports": manifest.supports,
        "config_paths": manifest.config_paths,
        "managers": manifest.managers,
        "base_dir": manifest.base_dir,
        "present": manifest.present,
        "loaded": bool(loaded and loaded.ok),
        "error": (loaded.error if loaded else "") or "",
        "skipped": (loaded.skipped if loaded else "") or "",
    }
    if getattr(args, "json", False) or out.json_mode:
        out.json(payload)
        return 0
    for key, value in payload.items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) or "-"
        out.print(f"{out.dim(str(key).ljust(13))} {value}")
    if manifest.type is PluginType.PROVIDER and loaded and loaded.provider is not None:
        sources = loaded.provider.list_sources()
        out.print("")
        out.print(out.strong(f"sources ({len(sources)})"))
        out.table(
            ["manager", "key", "name", "url", "kind"],
            [
                [s.manager, s.provider_key, s.name, shorten_url(s.url, 44), _kind_of(s).value]
                for s in sources
            ],
            aligns=["left", "left", "left", "left", "left"],
        )
    return 0


def plugins_install(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    source = args.source
    if source.startswith(("http://", "https://")) and not runtime.config.get(
        "plugins.allow_remote_install", True
    ):
        raise PluginError("remote plugin installation is disabled (plugins.allow_remote_install)")
    out.print(f"installing plugin from {source}…")
    path = install_plugin(
        source, trust=bool(getattr(args, "trust", False)), force=bool(getattr(args, "force", False)),
        name=getattr(args, "name", None),
    )
    manifest_path = path / MANIFEST_NAME
    from ..core.plugins import parse_manifest

    manifest = parse_manifest(manifest_path)
    out.success(f"installed {manifest.type.value} plugin {manifest.name} {manifest.version} -> {path}")
    if not getattr(args, "trust", False) and not manifest.bundled:
        out.warning("it is NOT trusted yet, so its code will not run")
        out.note(
            "plugins execute Python inside srcmgr. Review the code, then run:\n"
            f"  srcmgr plugins trust {manifest.name}"
        )
    if manifest.requires:
        out.note("requires: " + ", ".join(manifest.requires))
    return 0


def plugins_uninstall(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    path = uninstall_plugin(args.name)
    out.success(f"removed {path}")
    return 0


def plugins_trust(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    path = trust_plugin(args.name, untrust=bool(getattr(args, "untrust", False)))
    verb = "revoked trust for" if getattr(args, "untrust", False) else "trusted"
    out.success(f"{verb} {path}")
    if not getattr(args, "untrust", False):
        out.warning("user plugins run with your privileges; only trust code you have reviewed")
    return 0


def plugins_new(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    kind = PluginType.PROVIDER if args.type == "provider" else PluginType.MANAGER
    managers = [m.strip() for m in str(getattr(args, "managers", "apt")).split(",") if m.strip()]
    path = scaffold_plugin(
        args.name, kind=kind, target=Path(args.target).expanduser() if args.target else None,
        managers=managers,
    )
    out.success(f"created {kind.value} plugin skeleton in {path}")
    out.note(f"install it with: srcmgr plugins install {path} --trust")
    return 0


def plugins_repo(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    paths = runtime.plugin_paths()
    payload = {
        "paths": paths,
        "manifests": [m.to_dict() if hasattr(m, "to_dict") else vars(m) for m in runtime.all_manifests()],
    }
    if out.json_mode:
        out.json(
            {
                "paths": paths,
                "plugins": [m.name for m in runtime.all_manifests()],
            }
        )
        return 0
    out.table(
        ["location", "path"],
        [[key, describe_path(Path(value)) if value else "-"] for key, value in paths.items()],
        aligns=["left", "left"],
    )
    out.print("")
    out.note("set SRCMGR_PLUGIN_PATH to load plugins from extra directories (colon separated)")
    return 0


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def providers_list(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    registry = runtime.registry
    loaded = runtime.loaded_providers()
    disabled = {str(n) for n in runtime.config.get("registry.disabled_providers", []) or []}
    rows = []
    payload = []
    for manifest in sorted(registry.provider_manifests(), key=lambda m: m.name):
        sources = []
        provider = loaded.get(manifest.name)
        if provider and provider.provider is not None:
            try:
                sources = provider.provider.list_sources()
            except Exception:  # noqa: BLE001
                sources = []
        managers = sorted({s.manager for s in sources}) or manifest.managers
        state = "disabled" if manifest.name in disabled else "enabled"
        rows.append(
            [
                manifest.name,
                manifest.display_name,
                len(sources),
                ", ".join(managers),
                state,
                "bundled" if manifest.bundled else "user",
            ]
        )
        payload.append(
            {
                "name": manifest.name,
                "display_name": manifest.display_name,
                "version": manifest.version,
                "sources": len(sources),
                "managers": managers,
                "state": state,
                "bundled": manifest.bundled,
                "description": manifest.description,
            }
        )
    for name, item in loaded.items():
        if item.ok:
            continue
        rows.append([name, name, 0, ", ".join(item.manifest.managers), "unavailable", "user"])
        payload.append(
            {"name": name, "display_name": name, "sources": 0, "state": "unavailable",
             "error": item.error or item.skipped}
        )

    def render(_: Any) -> None:
        out.table(
            ["provider", "display name", "sources", "managers", "state", "origin"],
            rows,
            aligns=["left", "left", "right", "left", "left", "left"],
        )
        out.print("")
        out.note(
            "disable one with: srcmgr providers disable <name>   "
            "(or set registry.disabled_providers in the config)"
        )

    out.emit(payload, text_renderer=render)
    return 0


def providers_search(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    manager = canonical_manager(args.manager) if getattr(args, "manager", None) else None
    names = [manager] if manager else runtime.manager_names()
    query = getattr(args, "query", "") or ""
    rows: list[list[str]] = []
    payload: dict[str, Any] = {}
    for name in names:
        try:
            runtime.plugin(name)
        except SourceManagerError:
            continue
        found = runtime.registry.search(
            name, query, country="cn" if getattr(args, "china", False) else None
        )
        if getattr(args, "china", False):
            found = [s for s in found if _is_china(s)]
        if not found:
            continue
        payload[name] = [s.to_dict(redact=True) for s in found]
        rows += [
            [name, s.provider, s.provider_key, s.name, shorten_url(s.url, 46), _kind_of(s).value]
            for s in found
        ]
    if out.json_mode:
        out.json(payload)
        return 0
    if out.porcelain:
        for row in rows:
            print("\t".join(row))
        return 0
    out.table(
        ["manager", "provider", "key", "name", "url", "kind"],
        rows,
        aligns=["left", "left", "left", "left", "left", "left"],
        empty="no matching sources",
    )
    return 0


def providers_info(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    name = args.name
    loaded = runtime.loaded_providers().get(name)
    if loaded is None:
        raise PluginError(f"provider {name!r} not found")
    manifest = loaded.manifest
    sources = []
    if loaded.provider is not None:
        sources = loaded.provider.list_sources()
    payload = {
        "name": manifest.name,
        "display_name": manifest.display_name,
        "version": manifest.version,
        "description": manifest.description,
        "bundled": manifest.bundled,
        "managers": manifest.managers,
        "base_dir": manifest.base_dir,
        "sources": [s.to_dict(redact=True) for s in sources],
    }
    if out.json_mode:
        out.json(payload)
        return 0
    for key in ("name", "display_name", "version", "description", "bundled", "managers", "base_dir"):
        value = payload[key]
        if isinstance(value, list):
            value = ", ".join(value) or "-"
        out.print(f"{out.dim(key.ljust(13))} {value}")
    out.print("")
    out.table(
        ["manager", "key", "name", "url", "kind", "health"],
        [
            [
                s.manager,
                s.provider_key,
                s.name,
                shorten_url(s.url, 42),
                _kind_of(s).value,
                shorten_url(str((s.health or {}).get("url", "-")), 30),
            ]
            for s in sources
        ],
        aligns=["left", "left", "left", "left", "left", "left"],
        empty="this provider declares no sources",
    )
    return 0


def providers_toggle(runtime: SourceManagerRuntime, args: Any, out: Output, *, enable: bool) -> int:
    name = args.name
    known = {m.name for m in runtime.registry.provider_manifests()}
    if name not in known and name not in runtime.loaded_providers():
        raise PluginError(f"provider {name!r} not found")
    current = [str(n) for n in runtime.config.get("registry.disabled_providers", []) or []]
    if enable:
        updated = [n for n in current if n != name]
    else:
        updated = sorted({*current, name})
    runtime.config.set("registry.disabled_providers", updated)
    out.success(f"provider {name} {'enabled' if enable else 'disabled'}")
    out.note(f"config: registry.disabled_providers = {updated}")
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def config_get(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    key = getattr(args, "key", None)
    if key:
        value = runtime.config.get(key, None)
        if value is None:
            out.error(f"{key} is not set")
            return 2
        if out.json_mode:
            out.json({key: value})
        else:
            out.print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return 0
    data = runtime.config.as_dict(redact=False)
    if out.json_mode:
        out.json(runtime.config.as_dict(redact=True))
        return 0
    out.print(out.strong(f"config file: {describe_path(config_file())}"))
    _print_config_tree(out, data)
    return 0


def _print_config_tree(out: Output, data: Mapping[str, Any], prefix: str = "") -> None:
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            if not value:
                out.print(f"  {dotted} = {{}}")
                continue
            out.print(out.strong(dotted))
            _print_config_tree(out, value, prefix=f"{dotted}.")
        else:
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
            out.print(f"  {out.dim(dotted.ljust(42))} {rendered}")


def config_set(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    value = parse_value(args.value)
    runtime.config.set(args.key, value)
    out.success(f"{args.key} = {value!r}")
    return 0


def config_unset(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    from ..core.paths import load_yaml, dump_yaml

    path = config_file()
    raw = load_yaml(path, default={}) or {}
    if not isinstance(raw, Mapping):
        raw = {}
    raw = json.loads(json.dumps(raw))
    parts = args.key.split(".")
    node: Any = raw
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            out.error(f"{args.key} is not set")
            return 2
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        out.error(f"{args.key} is not set")
        return 2
    node.pop(parts[-1], None)
    dump_yaml(raw, path, header="Source Manager user configuration")
    out.success(f"removed {args.key}")
    return 0


def config_path(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    out.print(str(config_file()))
    return 0


def config_edit(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    path = config_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Source Manager user configuration\n", encoding="utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    out.note(f"launching {editor} {path}")
    return subprocess.call([*shlex.split(editor), str(path)])  # noqa: S603


def config_managers(runtime: SourceManagerRuntime, args: Any, out: Output) -> int:
    names = [canonical_manager(args.manager)] if getattr(args, "manager", None) else runtime.manager_names()
    payload: dict[str, Any] = {}
    for name in names:
        try:
            runtime.plugin(name)
        except SourceManagerError:
            continue
        payload[name] = {
            "health": runtime.config.health(name),
            "fallback": runtime.config.fallback(name),
            "backup": runtime.config.backup(name),
            "overrides": runtime.config.get(f"managers.{name}", {}) or {},
        }
    if out.json_mode:
        out.json(payload)
        return 0
    for name, sections in payload.items():
        out.print(out.strong(name))
        for section, values in sections.items():
            rendered = json.dumps(values, ensure_ascii=False)
            out.print(f"  {out.dim(section.ljust(10))} {rendered}")
    return 0


__all__ = [
    "backup",
    "config_get",
    "config_managers",
    "config_path",
    "config_set",
    "config_unset",
    "config_edit",
    "doctor",
    "fallback",
    "health",
    "history",
    "manager_add",
    "manager_available",
    "manager_backup",
    "manager_choose",
    "manager_current",
    "manager_disable",
    "manager_edit",
    "manager_enable",
    "manager_list",
    "manager_refresh",
    "manager_remove",
    "manager_restore",
    "manager_rollback",
    "manager_test",
    "managers",
    "plugins_info",
    "plugins_install",
    "plugins_list",
    "plugins_new",
    "plugins_repo",
    "plugins_trust",
    "plugins_uninstall",
    "providers_info",
    "providers_list",
    "providers_search",
    "providers_toggle",
    "restore",
    "rollback",
    "sources",
    "test",
]
