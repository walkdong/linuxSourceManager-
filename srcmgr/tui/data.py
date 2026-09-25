"""View models and actions for the menu UI.

The UI never talks to plugins directly: it asks this module for plain data (rows, mirrors,
reports) and calls plain actions (switch, scan, undo).  Two consequences that matter for
the beginner experience:

* **The lock is enforced here as well as in the core.**  :func:`switch` and
  :func:`auto_fastest` refuse to touch a locked ecosystem, so no future UI code can
  accidentally bypass the user's pin.
* **Every action returns a small, translatable result object** (:class:`Outcome`) rather
  than raising; the UI shows ``outcome.message`` and, on failure,
  :func:`srcmgr.tui.i18n.friendly_error` guidance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..core.api import Source, TestResult
from ..core.errors import LockedError, PermissionDenied, SourceManagerError
from ..core.health import test_sources
from ..core.runtime import SourceManagerRuntime, canonical_manager
from ..core.paths import which
from .i18n import action_label, friendly_error, tr

#: Friendly Chinese names for the ecosystems we ship.  A plugin we do not know yet is
#: shown with its manifest display name instead of being hidden.
MANAGER_NAMES: dict[str, str] = {
    "apt": "APT（系统软件）",
    "flatpak": "Flatpak（应用商店）",
    "snap": "Snap（应用商店）",
    "docker": "Docker（容器镜像）",
    "npm": "npm（Node.js）",
    "yarn": "Yarn（Node.js）",
    "pnpm": "pnpm（Node.js）",
    "pip": "Python (pip)",
    "conda": "Conda（Python 环境）",
    "cargo": "Rust (Cargo)",
    "go": "Go (GOPROXY)",
    "git": "Git（代码下载）",
    "maven": "Java (Maven)",
    "rubygems": "Ruby (RubyGems)",
}

#: Order shown on the home screen: system-wide first, then the languages people expect.
MANAGER_ORDER: tuple[str, ...] = (
    "apt", "flatpak", "snap", "docker", "npm", "yarn", "pnpm", "pip", "conda",
    "cargo", "go", "git", "maven", "rubygems",
)

#: How many mirrors the chooser shows.  A beginner should not have to scroll through 60
#: entries; everything beyond this is a keystroke away (``显示全部``).
MAX_VISIBLE_MIRRORS = 12

#: Health phases, ordered by what they mean for the user (not by colour):
#:
#: * ``ok``           - the active source answers
#: * ``spares_down``  - the active source is fine, some *other* enabled source is dead.
#:                      Downloads still work, so this must NOT look like a failure.
#: * ``slow``         - the active source answers but is slow
#: * ``current_down`` - the active source itself is unreachable: this is a real problem
#: * ``unknown``      - never measured
HEALTH_OK = "ok"
HEALTH_SPARES_DOWN = "spares_down"
HEALTH_SLOW = "slow"
HEALTH_CURRENT_DOWN = "current_down"
HEALTH_UNKNOWN = "unknown"

#: Latency above which a reachable source is called slow.
SLOW_MS = 1500.0


@dataclass
class Outcome:
    """Result of a UI action: enough to render a sentence and maybe a next step."""

    ok: bool
    message: str = ""
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, message: str = "", **data: Any) -> "Outcome":
        return cls(ok=True, message=message, data=data)

    @classmethod
    def failure(cls, message: str, detail: str = "", **data: Any) -> "Outcome":
        what, todo = friendly_error(detail or message)
        return cls(ok=False, message=what, detail=todo, data=data)


@dataclass
class Ecosystem:
    """One row of the home screen."""

    manager: str
    name: str
    available: bool
    detected: bool = True
    reason: str = ""
    current: str = ""
    current_url: str = ""
    #: health phase of the *active* source plus how many spares are dead
    health: str = HEALTH_UNKNOWN
    latency_ms: float | None = None
    bad_sources: int = 0
    total_sources: int = 0
    measured: int = 0
    locked: bool = False
    needs_admin: bool = False
    error: str = ""

    @property
    def key(self) -> str:
        return self.manager

    def health_text(self, translate: Any = None) -> str:
        """Words only: colour is decoration, the sentence carries the meaning.

        ``translate`` lets a frontend pass its own translator so the text follows the
        language *selected in the UI*, not the process default.
        """
        t = translate or tr
        if not self.available:
            return t("home.not_detected")
        if self.health == HEALTH_UNKNOWN or not self.measured:
            return t("home.health_unknown")
        if self.health == HEALTH_CURRENT_DOWN:
            return t("home.current_down")
        if self.health == HEALTH_SPARES_DOWN:
            return t("home.spares_down", count=self.bad_sources)
        if self.health == HEALTH_SLOW:
            extra = f" · {t('home.spares_down', count=self.bad_sources)}" if self.bad_sources else ""
            return f"{t('home.health_warn')} · {_ms(self.latency_ms)}{extra}"
        if self.bad_sources:
            return f"{t('home.health_ok')} · {_ms(self.latency_ms)} · {t('home.spares_down', count=self.bad_sources)}"
        return f"{t('home.health_ok')} · {_ms(self.latency_ms)}"

    def current_text(self) -> str:
        if not self.available:
            return "—"
        return self.current or tr("home.no_source")


@dataclass
class Mirror:
    """One candidate source inside an ecosystem screen."""

    source: Source
    latency_ms: float | None = None
    ok: bool | None = None
    status: str = ""
    is_current: bool = False
    error: str = ""
    detail: str = ""

    @property
    def title(self) -> str:
        return self.source.name or self.source.url

    @property
    def speed_text(self) -> str:
        if self.ok is None:
            return tr("eco.no_health")
        if not self.ok:
            return f"{'✗'} {tr('eco.probe_failed')}"
        return _ms(self.latency_ms)

    @property
    def selectable(self) -> bool:
        """A broken mirror can still be selected (the user may know better than us)."""
        return True


@dataclass
class SwitchPlan:
    """What "switch to the fastest" intends to do, for the confirmation dialog."""

    changes: list[tuple[str, str, str, float | None]] = field(default_factory=list)  # manager, name, url, ms
    skipped_locked: list[str] = field(default_factory=list)
    already_best: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.changes)


def _ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1:
        return "<1ms"
    if value < 1000:
        return f"{value:.0f}ms"
    return f"{value / 1000:.1f}s"


def display_name(runtime: SourceManagerRuntime, manager: str) -> str:
    """Chinese name when we know one, otherwise the plugin's own display name."""
    if manager in MANAGER_NAMES:
        return MANAGER_NAMES[manager]
    try:
        return runtime.plugin(manager).manifest.display_name or manager
    except SourceManagerError:
        return manager


def needs_admin(runtime: SourceManagerRuntime, manager: str) -> bool:
    """True when writing to this ecosystem requires root."""
    try:
        plugin = runtime.plugin(manager)
    except SourceManagerError:
        return False
    return getattr(plugin.manifest.scope, "value", str(plugin.manifest.scope)) == "system"


def command_present(manager: str) -> bool:
    """Whether the ecosystem's own CLI exists (used for the "not installed" note)."""
    programs = {
        "apt": "apt-get",
        "flatpak": "flatpak",
        "snap": "snap",
        "docker": "docker",
        "npm": "npm",
        "yarn": "yarn",
        "pnpm": "pnpm",
        "pip": "pip",
        "conda": "conda",
        "cargo": "cargo",
        "go": "go",
        "git": "git",
        "maven": "mvn",
        "rubygems": "gem",
    }
    program = programs.get(manager)
    return True if program is None else which(program) is not None


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def ordered_managers(runtime: SourceManagerRuntime) -> list[str]:
    try:
        names = list(runtime.manager_names())
    except SourceManagerError:
        return []
    ordered = [name for name in MANAGER_ORDER if name in names]
    return ordered + sorted(name for name in names if name not in ordered)


def load_ecosystems(
    runtime: SourceManagerRuntime,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[Ecosystem]:
    """Read every ecosystem's state.  Slow (shells out per ecosystem) → run in a worker."""
    out: list[Ecosystem] = []
    for manager in ordered_managers(runtime):
        if progress is not None:
            progress(manager)
        out.append(load_ecosystem(runtime, manager))
    return out


def last_switched_url(runtime: SourceManagerRuntime, manager: str) -> str:
    """URL the user most recently switched to, straight from the history log.

    Needed because an ecosystem can have several active sources and its own idea of which
    one "wins" (APT keeps using its official archive for the release while a mirror
    drop-in is added).  After an explicit switch the user's intent is unambiguous, so the
    UI reports what the history says rather than what the plugin guesses.
    """
    try:
        records = runtime.history_records(manager=manager, limit=20)
    except Exception:  # noqa: BLE001 - history is a nicety, never a failure
        return ""
    for record in records:
        if record.action in {"enable", "add", "fallback"} and record.ok and record.url:
            return record.url
        if record.action in {"rollback", "restore"} and record.ok:
            return ""  # an undo invalidates the previous intent
    return ""


def load_ecosystem(runtime: SourceManagerRuntime, manager: str) -> Ecosystem:
    manager = canonical_manager(manager)
    row = Ecosystem(
        manager=manager,
        name=display_name(runtime, manager),
        available=False,
        locked=runtime.is_locked(manager),
        needs_admin=needs_admin(runtime, manager),
    )
    try:
        status = runtime.status(manager)
        row.detected = status.available or command_present(manager)
        row.available = status.available
        row.reason = status.reason
        row.error = status.error
        if status.available:
            installed = runtime.installed(manager)
            row.total_sources = len(installed)
            current = runtime.current(manager)
            intended = last_switched_url(runtime, manager)
            if intended:
                match = next(
                    (s for s in installed if s.url == intended or _same_mirror(s.url, intended)),
                    None,
                )
                if match is not None:
                    current = match
            if current is not None:
                row.current = current.name
                row.current_url = current.url

            # Judge by impact: a dead *spare* source must not look like a failure.
            measured = 0
            bad_spares = 0
            current_result = None
            for source in installed:
                cached = _cached_health(runtime, source.id)
                if cached is None:
                    continue
                measured += 1
                is_active = current is not None and (
                    source.id == current.id or _same_mirror(source.url, current.url)
                )
                if is_active:
                    current_result = cached
                elif not cached.ok:
                    bad_spares += 1
            row.measured = measured
            row.bad_sources = bad_spares
            if current_result is not None:
                if not current_result.ok:
                    row.health = HEALTH_CURRENT_DOWN
                elif (current_result.latency_ms or 0) > SLOW_MS:
                    row.health = HEALTH_SLOW
                elif bad_spares:
                    row.health = HEALTH_SPARES_DOWN
                else:
                    row.health = HEALTH_OK
                row.latency_ms = current_result.latency_ms
            elif measured:
                # only spares were measured (the active entry is not in the cache)
                row.health = HEALTH_SPARES_DOWN if bad_spares else HEALTH_UNKNOWN
            else:
                row.health = HEALTH_UNKNOWN
    except SourceManagerError as exc:
        row.error = exc.message
    return row


def _cached_health(runtime: SourceManagerRuntime, source_id: str) -> TestResult | None:
    try:
        return runtime.cache.get(source_id)
    except Exception:  # noqa: BLE001 - a stale cache must never break the listing
        return None


# ---------------------------------------------------------------------------
# mirrors and switching
# ---------------------------------------------------------------------------
def load_mirrors(runtime: SourceManagerRuntime, manager: str) -> list[Mirror]:
    """Registry mirrors for ``manager``, marking the one in use and last known speeds."""
    manager = canonical_manager(manager)
    installed = []
    try:
        installed = runtime.installed(manager)
    except SourceManagerError:
        installed = []
    current = None
    try:
        current = runtime.current(manager)
    except SourceManagerError:
        current = None
    mirrors: list[Mirror] = []
    for source in runtime.available(manager):
        cached = _cached_health(runtime, source.id)
        mirrors.append(
            Mirror(
                source=source,
                latency_ms=cached.latency_ms if cached else None,
                ok=cached.ok if cached else None,
                status=cached.status if cached else "",
                error=cached.error if cached else "",
                detail=cached.detail if cached else "",
                is_current=_is_current(source, current, installed),
            )
        )
    mirrors.sort(key=lambda m: _sort_key(m))
    return mirrors


def _is_current(source: Source, current: Source | None, installed: Sequence[Source]) -> bool:
    """Mark the mirror the ecosystem is actually using.

    Exact matching misses the common cases (the machine uses ``cn.archive.ubuntu.com``
    while the registry lists ``archive.ubuntu.com``; apt stores several suites), so a
    same-host-with-same-path-prefix match is also accepted.
    """
    from ..core.registry import source_matches

    candidates = [item for item in (current, *installed) if item is not None]
    for item in candidates:
        if source_matches(source, item):
            return True
    # Country/alias prefixes must not hide the match: a machine on
    # cn.archive.ubuntu.com is using the official archive.ubuntu.com mirror.
    for item in candidates:
        if _same_mirror(source.url, item.url):
            return True
    return False


def _same_mirror(left: str, right: str) -> bool:
    """Loose "same mirror" test: same host (allowing an alias prefix) and same path root."""
    from urllib.parse import urlsplit

    if not left or not right:
        return False
    a, b = urlsplit(left), urlsplit(right)
    if not a.netloc or not b.netloc:
        return False
    host_a, host_b = a.netloc.lower(), b.netloc.lower()
    if not (host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a)):
        return False
    path_a = a.path.rstrip("/").lower()
    path_b = b.path.rstrip("/").lower()
    if path_a == path_b:
        return True
    shorter, longer = sorted((path_a, path_b), key=len)
    return bool(shorter) and len(shorter) >= 4 and longer.startswith(shorter)


def _relevance(runtime: SourceManagerRuntime, mirror: Mirror) -> tuple[int, int, int, int, str]:
    """Lower is better: which mirrors deserve a probe slot."""
    source = mirror.source
    country = str(runtime.config.get("registry.prefer_country", "") or "").lower()
    declared = source.extra.get("countries") or source.extra.get("country") or []
    if isinstance(declared, str):
        declared = [declared]
    declared = {str(item).lower() for item in declared}
    kind = _kind_value(source)
    return (
        0 if mirror.is_current else 1,
        0 if kind == "mirror" else (1 if kind == "official" else 2),
        0 if (country and country in declared) else 1,
        -(source.priority or 0),
        source.name,
    )


def _kind_value(source: Source) -> str:
    return getattr(source.kind, "value", str(source.kind))


def _sort_key(mirror: Mirror) -> tuple[int, float, str]:
    """Reachable first, then fastest, then name - never hide a source."""
    if mirror.ok is None:
        return (1, 0.0, mirror.title)
    if mirror.ok:
        return (0, mirror.latency_ms or 9999.0, mirror.title)
    return (2, 0.0, mirror.title)


def measure_mirrors(
    runtime: SourceManagerRuntime,
    manager: str,
    *,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Mirror]:
    """Probe the mirrors of one ecosystem and return them re-sorted by speed.

    Defaults to the first :data:`MAX_VISIBLE_MIRRORS` candidates (a user who wants the
    full list can pass ``limit=0``): probing 65 apt mirrors to show 3 useful ones wastes
    a minute of the user's life.
    """
    mirrors = load_mirrors(runtime, manager)
    if limit is None:
        limit = MAX_VISIBLE_MIRRORS
    if limit:
        # Pick the shortlist *before* probing: measuring 65 apt mirrors to display 12
        # wastes a minute of the user's time.  Ranked by usefulness (in use, mirrors,
        # declared priority, the country this machine already prefers, https), so the
        # dozen we test are the dozen that could plausibly be chosen.
        mirrors = sorted(mirrors, key=lambda m: _relevance(runtime, m))
        mirrors = mirrors[:limit]
    sources = [m.source for m in mirrors]
    if progress is not None:
        progress(0, len(sources))
    results = test_sources(sources, config=runtime.config, manager=manager)
    by_id = {result.source_id: result for result in results}
    for mirror in mirrors:
        result = by_id.get(mirror.source.id)
        if result is None:
            continue
        mirror.ok = result.ok
        mirror.latency_ms = result.latency_ms
        mirror.status = result.status
        mirror.error = result.error
        mirror.detail = result.detail
    if progress is not None:
        progress(len(sources), len(sources))
    mirrors.sort(key=_sort_key)
    return mirrors


def switch(runtime: SourceManagerRuntime, manager: str, source: Source, *, refresh: bool = False) -> Outcome:
    """Activate ``source`` - refused for a locked ecosystem (hard lock)."""
    manager = canonical_manager(manager)
    if runtime.is_locked(manager):
        raise LockedError(
            f"{display_name(runtime, manager)} {tr('lock.locked_row')}",
            hint=tr("lock.blocked_body").replace("\n", " "),
        )
    try:
        # Pass the Source itself: it works for registry entries and for a URL the user
        # typed by hand (which has no provider key to resolve).
        result = runtime.add(manager, source=source)
        if result.ok and refresh:
            refresh_result = runtime.refresh(manager)
            if not refresh_result.ok:
                result.message = f"{result.message}（刷新失败：{refresh_result.message}）"
    except PermissionDenied as exc:
        # A frontend must be able to tell "needs the admin password" from a real error, so
        # it can ask and retry instead of showing a dead end.
        return Outcome(
            ok=False,
            message=exc.message,
            detail=getattr(exc, "hint", "") or "",
            data={"permission": True},
        )
    except SourceManagerError as exc:
        return Outcome.failure(str(exc), detail=getattr(exc, "hint", "") or "")
    if not result.ok:
        return Outcome.failure(result.message or tr("common.failed"))
    return Outcome.success(
        tr("eco.switched", name=source.name, url=source.url),
        backup=result.backup_id,
    )


def custom_source(runtime: SourceManagerRuntime, manager: str, url: str, *, name: str = "") -> Source:
    """Build a user-supplied source (the "自己输入网址" entry)."""
    manager = canonical_manager(manager)
    return Source(
        manager=manager,
        name=name or url,
        url=url,
        kind=_infer_kind(url),
        scope=runtime.plugin(manager).manifest.scope,
        extra={"value": url},
    )


def _infer_kind(url: str):
    from ..core.api import SourceKind

    lowered = (url or "").lower()
    if any(token in lowered for token in ("mirror", "tuna", "ustc", "aliyun", "tencent")):
        return SourceKind.MIRROR
    if any(token in lowered for token in ("localhost", "127.0.0.1", "internal", ".corp", ".lan")):
        return SourceKind.PRIVATE
    return SourceKind.CUSTOM


def valid_url(text: str) -> bool:
    candidate = (text or "").strip()
    return candidate.startswith(("http://", "https://")) and "." in candidate.split("//", 1)[-1]


# ---------------------------------------------------------------------------
# bulk actions
# ---------------------------------------------------------------------------
def plan_fastest(
    runtime: SourceManagerRuntime,
    managers: Iterable[str] | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> SwitchPlan:
    """Work out which ecosystems could be switched to a faster mirror.

    Locked ecosystems are reported in :attr:`SwitchPlan.skipped_locked` and are not even
    probed: the pin means "do not touch", not "touch and then revert".
    """
    plan = SwitchPlan()
    for manager in list(managers) if managers is not None else ordered_managers(runtime):
        manager = canonical_manager(manager)
        if runtime.is_locked(manager):
            plan.skipped_locked.append(display_name(runtime, manager))
            continue
        try:
            if not runtime.status(manager).available:
                continue
        except SourceManagerError:
            continue
        if progress is not None:
            progress(manager)
        try:
            mirrors = measure_mirrors(runtime, manager, limit=8)
        except SourceManagerError:
            continue
        best = next((m for m in mirrors if m.ok), None)
        if best is None:
            continue
        current = None
        try:
            current = runtime.current(manager)
        except SourceManagerError:
            current = None
        if current is not None and current.url.rstrip("/") == best.source.url.rstrip("/"):
            plan.already_best.append(display_name(runtime, manager))
            continue
        plan.changes.append(
            (manager, best.title, best.source.url, best.latency_ms)
        )
    return plan


def apply_fastest(runtime: SourceManagerRuntime, plan: SwitchPlan) -> Outcome:
    """Execute a :func:`plan_fastest` result.  Never touches a locked ecosystem."""
    switched: list[str] = []
    failed: list[str] = []
    for manager, _name, _url, _ms in plan.changes:
        if runtime.is_locked(manager):
            plan.skipped_locked.append(display_name(runtime, manager))
            continue
        mirrors = load_mirrors(runtime, manager)
        target = next(
            (m for m in mirrors if m.source.url.rstrip("/") == _url.rstrip("/")), None
        )
        if target is None:
            failed.append(display_name(runtime, manager))
            continue
        try:
            outcome = switch(runtime, manager, target.source)
        except LockedError:
            plan.skipped_locked.append(display_name(runtime, manager))
            continue
        (switched if outcome.ok else failed).append(display_name(runtime, manager))
    message = tr("fastest.done", count=len(switched), skipped=len(plan.skipped_locked))
    detail_parts = []
    if plan.skipped_locked:
        detail_parts.append(tr("fastest.skipped_locked", names="、".join(plan.skipped_locked)))
    if failed:
        detail_parts.append(tr("fastest.failed", count=len(failed), names="、".join(failed)))
    return Outcome(ok=not failed, message=message, detail="　".join(detail_parts), data={
        "switched": switched, "failed": failed, "skipped": plan.skipped_locked,
    })


def scan_all(
    runtime: SourceManagerRuntime,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Health-check the active source of every ecosystem (the "一键体检" button)."""
    report: dict[str, Any] = {"managers": {}, "ok": 0, "bad": 0, "checked": 0}
    for manager in ordered_managers(runtime):
        try:
            if not runtime.status(manager).available:
                continue
        except SourceManagerError:
            continue
        results = runtime.test(manager, scope="enabled")
        good = sum(1 for r in results if r.ok)
        bad = len(results) - good
        report["managers"][manager] = {
            "ok": good,
            "bad": bad,
            "results": [
                {"name": r.name, "ok": r.ok, "latency_ms": r.latency_ms, "status": r.status}
                for r in results
            ],
        }
        report["ok"] += good
        report["bad"] += bad
        report["checked"] += len(results)
        if progress is not None:
            progress(manager, good, bad)
    return report


# ---------------------------------------------------------------------------
# history / backups
# ---------------------------------------------------------------------------
def history_rows(runtime: SourceManagerRuntime, *, limit: int = 50) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in runtime.history_records(limit=limit):
        detail = record.message or record.url or ""
        rows.append(
            {
                "when": record.when,
                "action": action_label(record.action),
                "manager": display_name(runtime, record.manager) if record.manager else "-",
                "detail": detail,
                "ok": "✓" if record.ok else "✗",
            }
        )
    return rows


def backup_rows(runtime: SourceManagerRuntime, *, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest in runtime.backups_list()[:limit]:
        rows.append(
            {
                "id": manifest.id,
                "when": _pretty_time(manifest.created_at),
                "manager": display_name(runtime, manifest.manager) if manifest.manager else "-",
                "files": len(manifest.files),
                "reason": manifest.reason,
            }
        )
    return rows


def _pretty_time(value: str) -> str:
    try:
        from datetime import datetime, timezone

        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return value


def undo_last(runtime: SourceManagerRuntime, manager: str) -> Outcome:
    """Undo the most recent change of one ecosystem (works while locked)."""
    try:
        result = runtime.rollback(canonical_manager(manager))
    except SourceManagerError as exc:
        return Outcome.failure(str(exc))
    if not result.ok:
        return Outcome.failure(result.message or tr("undo.nothing"))
    return Outcome.success(tr("undo.done", message=result.message), backup=result.backup_id)


def restore_backup(runtime: SourceManagerRuntime, backup_id: str) -> Outcome:
    try:
        result = runtime.restore_backup(backup_id)
    except SourceManagerError as exc:
        return Outcome.failure(str(exc))
    if not result.ok:
        return Outcome.failure(result.message or tr("common.failed"))
    return Outcome.success(tr("backups.done", message=result.message))


__all__ = [
    "Ecosystem",
    "HEALTH_CURRENT_DOWN",
    "HEALTH_SPARES_DOWN",
    "HEALTH_OK",
    "HEALTH_SLOW",
    "HEALTH_UNKNOWN",
    "MANAGER_NAMES",
    "MANAGER_ORDER",
    "Mirror",
    "Outcome",
    "SLOW_MS",
    "SwitchPlan",
    "apply_fastest",
    "backup_rows",
    "command_present",
    "custom_source",
    "display_name",
    "history_rows",
    "load_ecosystem",
    "load_ecosystems",
    "load_mirrors",
    "measure_mirrors",
    "needs_admin",
    "ordered_managers",
    "plan_fastest",
    "restore_backup",
    "scan_all",
    "switch",
    "undo_last",
    "valid_url",
]
