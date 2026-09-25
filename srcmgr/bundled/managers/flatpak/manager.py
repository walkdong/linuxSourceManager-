"""Flatpak manager plugin: the remotes are the sources.

Why the CLI and never the config file
-------------------------------------
A flatpak remote lives inside an *installation*: the system one
(``/var/lib/flatpak``) or the per-user one (``~/.local/share/flatpak``).  Both keep
their state in an ostree repository config (``.../repo/config``).  Editing that file
by hand bypasses ostree's own bookkeeping, so every read and every mutation goes
through the ``flatpak`` CLI; the config paths are declared only so that
``backup``/``restore`` can snapshot them.

``list_enabled()`` reports the remotes of *both* installations, each tagged with
``extra["installation"]`` and scoped ``Scope.SYSTEM``/``Scope.USER`` accordingly, so a
user can see and manage both without running two commands.

Test hook
---------
When ``SRCMGR_FLATPAK_FAKE_DIR`` is set (tests only) remote state is read from
``<dir>/remotes.tsv`` (system) and ``<dir>/remotes-user.tsv`` (user) - tab separated
output in the same shape as ``flatpak remotes --columns=name,url,disabled,prio`` - and
would-be CLI invocations are appended to ``<dir>/calls.log`` instead of being run.
:meth:`FlatpakManager._fake_mode` is the only place that knows about the variable; the
production path never looks at it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from srcmgr.core import helpers
from srcmgr.core.api import (
    ManagerPlugin,
    OperationResult,
    Scope,
    Source,
    SourceKind,
    TestResult,
    compute_source_id,
)
from srcmgr.core.backup import utc_now
from srcmgr.core.health import ProbeResult, probe_url
from srcmgr.core.paths import atomic_write_text

#: Test-only override: read fixtures from ``$SRCMGR_FLATPAK_FAKE_DIR``.
FAKE_ENV = "SRCMGR_FLATPAK_FAKE_DIR"

#: ostree repo config of the two installations (glob-free, so backup can snapshot it).
SYSTEM_CONFIG = Path("/var/lib/flatpak/repo/config")
USER_CONFIG = Path("~/.local/share/flatpak/repo/config")

INSTALLATIONS = ("system", "user")

FLATHUB_NAME = "flathub"
FLATHUB_URL = "https://dl.flathub.org/repo/"
FLATHUB_DESCRIPTION = "The reference Flatpak repository (Flathub)."

#: Statuses that still prove a remote endpoint is alive.
HEALTHY_HTTP = (200, 201, 204, 301, 302, 303, 307, 308, 401, 403, 405)

#: ``flatpak remotes`` invocations tried in order; the first one the local flatpak
#: accepts is used.  ``disabled``/``prio`` is what this plugin prefers, ``priority``/
#: ``options`` is what upstream flatpak actually prints (the disabled state lives in
#: the OPTIONS column there), and ``--show-details`` is the oldest supported form.
STYLE_PREFERRED = "columns-disabled"
STYLE_PRIORITY = "columns-priority"
STYLE_DETAILS = "details"

_COLUMN_ATTEMPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (STYLE_PREFERRED, ("--columns=name,url,disabled,prio", "--show-disabled")),
    (STYLE_PREFERRED, ("--columns=name,url,disabled,prio",)),
    (STYLE_PRIORITY, ("--columns=name,url,priority,options", "--show-disabled")),
    (STYLE_PRIORITY, ("--columns=name,url,priority,options",)),
    (STYLE_DETAILS, ("--show-details", "--show-disabled")),
    (STYLE_DETAILS, ("--show-details",)),
)

_FALSE_TOKENS = {"", "-", "0", "false", "no", "enabled", "none"}
_TRUE_TOKENS = {"1", "true", "yes", "disabled", "on"}


# ---------------------------------------------------------------------------
# parsing helpers (module level so tests can exercise them directly)
# ---------------------------------------------------------------------------
@dataclass
class Remote:
    """One flatpak remote as printed by the CLI."""

    name: str
    url: str = ""
    title: str = ""
    disabled: bool = False
    priority: int | None = None
    options: list[str] = field(default_factory=list)


def _clean(value: str) -> str:
    """``-`` is flatpak's "no value" marker."""
    text = (value or "").strip()
    return "" if text in {"-", ""} else text


def _to_int(value: str) -> int | None:
    text = (value or "").strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _looks_disabled(value: str) -> bool:
    text = (value or "").strip().lower()
    if text in _FALSE_TOKENS:
        return False
    if text in _TRUE_TOKENS:
        return True
    # Unknown non-empty marker (a future flatpak spelling): assume disabled so we
    # never silently claim a disabled remote is usable.
    return bool(text)


def split_options(value: str) -> list[str]:
    """``"user,no-gpg-verify"`` -> ``["user", "no-gpg-verify"]``."""
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def parse_columns_row(fields: list[str]) -> Remote | None:
    """Parse one row of ``--columns=name,url,disabled,prio``.

    The DISABLED column is flatpak's own marker (``disabled`` / ``-``); priority is
    optional, which is why three-column output is accepted too.
    """
    if not fields or not fields[0].strip():
        return None
    remote = Remote(name=fields[0].strip())
    if len(fields) > 1:
        remote.url = _clean(fields[1])
    if len(fields) > 2:
        remote.disabled = _looks_disabled(fields[2])
    if len(fields) > 3:
        remote.priority = _to_int(fields[3])
    return remote


def parse_priority_row(fields: list[str]) -> Remote | None:
    """Parse one row of ``--columns=name,url,priority,options`` (upstream layout)."""
    if not fields or not fields[0].strip():
        return None
    remote = Remote(name=fields[0].strip())
    if len(fields) > 1:
        remote.url = _clean(fields[1])
    if len(fields) > 2:
        remote.priority = _to_int(fields[2])
    if len(fields) > 3:
        remote.options = split_options(fields[3])
        remote.disabled = "disabled" in remote.options
    return remote


def parse_details_row(fields: list[str]) -> Remote | None:
    """Parse one row of ``flatpak remotes --show-details``.

    Upstream order is NAME TITLE URL COLLECTION SUBSET FILTER PRIORITY OPTIONS
    COMMENT DESCRIPTION HOMEPAGE ICON; the OPTIONS column carries ``disabled``,
    ``system``, ``user`` and ``no-gpg-verify``.
    """
    if not fields or not fields[0].strip():
        return None
    remote = Remote(name=fields[0].strip())
    if len(fields) == 2:  # ancient layout: NAME URL
        remote.url = _clean(fields[1])
        return remote
    if len(fields) > 1:
        remote.title = _clean(fields[1])
    if len(fields) > 2:
        remote.url = _clean(fields[2])
    if len(fields) > 6:
        remote.priority = _to_int(fields[6])
    if len(fields) > 7:
        remote.options = split_options(fields[7])
        remote.disabled = "disabled" in remote.options
    return remote


def parse_remotes(text: str, *, style: str = STYLE_PREFERRED) -> list[Remote]:
    """Parse a full ``flatpak remotes`` output block (tab separated)."""
    rows: list[Remote] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if style == STYLE_DETAILS:
            remote = parse_details_row(fields)
        elif style == STYLE_PRIORITY:
            remote = parse_priority_row(fields)
        else:
            remote = parse_columns_row(fields)
        if remote is not None:
            rows.append(remote)
    return rows


def sniff_style(text: str) -> str:
    """Guess which output shape ``text`` is in (used for fixtures and for tests)."""
    rows = [line.split("\t") for line in (text or "").splitlines() if line.strip()]
    if any(len(row) >= 8 for row in rows):
        return STYLE_DETAILS
    four = [row for row in rows if len(row) >= 4]
    if four and all(_to_int(row[2]) is not None for row in four):
        return STYLE_PRIORITY
    return STYLE_PREFERRED


def parse_remotes_auto(text: str) -> list[Remote]:
    """Parse fixture text of any supported shape (see :func:`sniff_style`)."""
    return parse_remotes(text, style=sniff_style(text))


def kind_for(url: str, *, name: str = "") -> SourceKind:
    """Flathub is official, everything else is a mirror/vendor heuristic."""
    if name.strip().lower() == FLATHUB_NAME:
        return SourceKind.OFFICIAL
    lowered = (url or "").lower()
    if "flathub.org" in lowered:
        return SourceKind.OFFICIAL
    if any(
        token in lowered
        for token in ("mirror", "tuna", "ustc", "aliyun", "tencent", "daocloud", "nj", "sjtu")
    ):
        return SourceKind.MIRROR
    if any(token in lowered for token in ("localhost", "127.0.0.1", "internal", ".corp", ".lan")):
        return SourceKind.PRIVATE
    return SourceKind.VENDOR


def summary_url(url: str) -> str:
    """The ``/summary`` endpoint of a flatpak repo (used by :meth:`test`)."""
    base = (url or "").rstrip("/")
    return f"{base}/summary" if base else ""


def installation_config(installation: str) -> Path:
    """Path of the ostree repo config backing one installation."""
    if installation == "user":
        return Path(USER_CONFIG).expanduser()
    return SYSTEM_CONFIG


def expand_config_paths(patterns: Sequence[str]) -> list[Path]:
    """Expand ``config_paths`` manifest entries (globs and plain paths, deduped)."""
    out: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        text = os.path.expanduser(str(pattern))
        if any(char in text for char in "*?["):
            parent = Path(text).parent
            if not parent.is_dir():
                continue
            matches = sorted(item for item in parent.glob(Path(text).name) if item.is_file())
        else:
            matches = [Path(text)]
        for item in matches:
            key = str(item)
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _record_fake_call(fake_dir: Path, argv: Sequence[str]) -> None:
    """Append one would-be invocation to ``<dir>/calls.log`` (tests only)."""
    log = fake_dir / "calls.log"
    line = " ".join(str(part) for part in argv)
    text = helpers.read_text(log)
    try:
        atomic_write_text(log, f"{text}{line}\n")
    except OSError:
        pass  # a test log must never break the plugin


@dataclass
class Outcome:
    """Result of one CLI invocation; a failure is data, never an exception."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    faked: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and self.returncode == 0

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        return text if text else f"exit code {self.returncode}"


class FlatpakManager(ManagerPlugin):
    """Flatpak remotes as Source Manager sources."""

    supports: tuple[str, ...] = (
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
    )

    # -- test hook -----------------------------------------------------------
    def _fake_mode(self) -> Path | None:
        """Return the fixture root when ``SRCMGR_FLATPAK_FAKE_DIR`` is set.

        Test-only switch: reads come from ``<dir>/remotes.tsv`` /
        ``<dir>/remotes-user.tsv`` and writes are replaced by an append to
        ``<dir>/calls.log``.  Unset in production, where the CLI is used.
        """
        raw = os.environ.get(FAKE_ENV, "").strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    # -- CLI plumbing --------------------------------------------------------
    def _run_cli(self, argv: Sequence[str], *, timeout: float = 20.0) -> Outcome:
        """Run one flatpak invocation (or record it in fake mode); never raises."""
        fake = self._fake_mode()
        if fake is not None:
            _record_fake_call(fake, argv)
            return Outcome(faked=True)
        try:
            completed = helpers.run(list(argv), timeout=timeout)
        except subprocess.TimeoutExpired:
            return Outcome(returncode=124, error=f"timed out after {timeout:g}s")
        except OSError as exc:  # missing binary, permission problem, ...
            return Outcome(returncode=127, error=str(exc))
        return Outcome(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    # -- metadata ------------------------------------------------------------
    def detect(self) -> bool:
        """True when flatpak is usable here (CLI, fixture root or an installation)."""
        if helpers.which("flatpak"):
            return True
        if self._fake_mode() is not None:
            return True
        return SYSTEM_CONFIG.exists() or Path(USER_CONFIG).expanduser().exists()

    def detect_detail(self) -> dict[str, Any]:
        cli = helpers.which("flatpak")
        installations: dict[str, Any] = {}
        for installation in INSTALLATIONS:
            config = installation_config(installation)
            remotes, command, error = self._list_installation(installation)
            installations[installation] = {
                "config": str(config),
                "config_exists": config.exists(),
                "remotes": [remote.name for remote in remotes],
                "count": len(remotes),
                "command": command,
                "error": error,
            }
        return {
            "available": self.detect(),
            "cli": bool(cli),
            "cli_path": cli or "",
            "fake_mode": str(self._fake_mode() or ""),
            "files": [str(path) for path in self.config_files() if path.exists()],
            "installations": installations,
        }

    def config_files(self) -> list[Path]:
        """Expanded config paths (globs resolved, missing plain paths kept)."""
        return expand_config_paths(self.manifest.config_paths)

    # -- reads ---------------------------------------------------------------
    def _list_installation(self, installation: str) -> tuple[list[Remote], str, str]:
        """Return ``(remotes, command_used, error)`` for one installation."""
        fake = self._fake_mode()
        if fake is not None:
            name = "remotes-user.tsv" if installation == "user" else "remotes.tsv"
            path = fake / name
            return parse_remotes_auto(helpers.read_text(path)), f"fake:{path}", ""
        flag = "--user" if installation == "user" else "--system"
        errors: list[str] = []
        for style, attempt in _COLUMN_ATTEMPTS:
            argv = ["flatpak", "remotes", flag, *attempt]
            outcome = self._run_cli(argv)
            if outcome.ok:
                return parse_remotes(outcome.stdout, style=style), " ".join(argv), ""
            errors.append(f"{' '.join(argv)}: {outcome.message}")
        return [], "", "; ".join(errors)

    def _source_for(self, remote: Remote, installation: str) -> Source:
        scope = Scope.SYSTEM if installation == "system" else Scope.USER
        is_flathub = remote.name.strip().lower() == FLATHUB_NAME
        source = helpers.make_source(
            self.name,
            name=remote.name,
            url=remote.url,
            kind=kind_for(remote.url, name=remote.name),
            scope=scope,
            enabled=not remote.disabled,
            file=str(installation_config(installation)),
            provider_key=remote.name,
            description=remote.title,
            is_default=is_flathub,
            health={"url": remote.url, "summary_url": summary_url(remote.url)},
            extra={
                "installation": installation,
                "url": remote.url,
                "name": remote.name,
                "remote_name": remote.name,
                "priority": remote.priority,
                "disabled": bool(remote.disabled),
                "options": list(remote.options),
            },
        )
        source.priority = remote.priority
        # The installation is part of the identity: the same remote name may exist in
        # both installations and they are different sources.
        source.id = compute_source_id(self.name, remote.url, installation, remote.name)
        return source

    def list_enabled(self) -> list[Source]:
        """Every remote of both installations (disabled ones included)."""
        sources: list[Source] = []
        for installation in INSTALLATIONS:
            remotes, _command, _error = self._list_installation(installation)
            sources.extend(self._source_for(remote, installation) for remote in remotes)
        return sources

    def current(self) -> Source | None:
        """Flathub when it is enabled, else the first enabled remote."""
        active = [source for source in self.list_enabled() if source.enabled]
        for source in active:
            if source.is_default:
                return source
        return active[0] if active else None

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Only Flathub is built in (system + user variant).

        Mirror catalogues are contributed by provider plugins; a provider entry for
        flatpak usually carries ``extra["url"]`` (e.g. a USTC flathub mirror) and
        ``extra["gpg"]``/``extra["gpg_verify"]``, which :meth:`add` understands.
        """
        out: list[Source] = []
        for installation in INSTALLATIONS:
            scope = Scope.SYSTEM if installation == "system" else Scope.USER
            key = FLATHUB_NAME if installation == "system" else f"{FLATHUB_NAME}-user"
            name = FLATHUB_NAME if installation == "system" else f"{FLATHUB_NAME} (user)"
            source = helpers.make_source(
                self.name,
                name=name,
                url=FLATHUB_URL,
                kind=SourceKind.OFFICIAL,
                scope=scope,
                enabled=False,
                provider_key=key,
                description=FLATHUB_DESCRIPTION,
                is_default=installation == "system",
                health={"url": FLATHUB_URL, "summary_url": summary_url(FLATHUB_URL)},
                extra={
                    "installation": installation,
                    "remote_name": FLATHUB_NAME,
                    "url": FLATHUB_URL,
                    "name": key,
                },
            )
            source.id = compute_source_id(self.name, FLATHUB_URL, installation, key)
            out.append(source)
        return out

    # -- mutations -----------------------------------------------------------
    def _root_guard(self, installation: str) -> OperationResult | None:
        if installation != "system":
            return None
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            return None
        return helpers.fail(
            "flatpak: system remotes are shared by all users and need root",
            hint="re-run with sudo (or use the user installation: extra.installation=user)",
        )

    def _target(
        self, source: Source
    ) -> tuple[str, str, str, str, OperationResult | None]:
        """Resolve ``(installation, remote_name, url, flag, error_result)``."""
        installation = str(source.extra.get("installation") or "system").strip().lower()
        if installation not in INSTALLATIONS:
            return "", "", "", "", helpers.fail(
                f"flatpak: unknown installation {installation!r}",
                hint="use extra.installation = system | user",
            )
        url = str(source.url or source.extra.get("url") or "").strip()
        remote_name = self._remote_name(source)
        flag = "--user" if installation == "user" else "--system"
        return installation, remote_name, url, flag, None

    def _remote_name(self, source: Source) -> str:
        raw = str(
            source.extra.get("remote_name") or source.provider_key or source.name or ""
        ).strip()
        # flatpak remote names must not contain spaces or slashes.
        return "".join(char if (char.isalnum() or char in "-_.") else "-" for char in raw)

    def _find_remote(
        self, installation: str, remote_name: str
    ) -> tuple[Remote | None, OperationResult | None]:
        remotes, _command, error = self._list_installation(installation)
        if error:
            return None, helpers.fail(
                f"flatpak: cannot list the {installation} remotes: {error}",
                hint="is flatpak installed and readable?",
            )
        for remote in remotes:
            if remote.name == remote_name:
                return remote, None
        return None, helpers.fail(
            f"flatpak: no remote named {remote_name!r} in the {installation} installation",
            hint="srcmgr list flatpak",
        )

    def add(self, source: Source) -> OperationResult:
        """``flatpak remote-add --if-not-exists [...] <name> <url>``."""
        installation, remote_name, url, flag, error = self._target(source)
        if error is not None:
            return error
        if not url:
            return helpers.fail(f"flatpak: source {source.name!r} has no URL to add")
        if not remote_name:
            return helpers.fail(f"flatpak: source {source.name!r} has no usable remote name")
        denied = self._root_guard(installation)
        if denied is not None:
            return denied

        argv = ["flatpak", "remote-add", "--if-not-exists", flag]
        if source.extra.get("gpg_verify") is False:
            argv.append("--gpg-verify=false")
        if source.extra.get("gpg") is False:
            argv.append("--no-gpg-verify")
        priority = source.priority if source.priority is not None else source.extra.get("priority")
        if priority is not None:
            argv.append(f"--prio={int(priority)}")
        argv += [remote_name, url]

        outcome = self._run_cli(argv)
        if not outcome.ok:
            return helpers.fail(
                f"flatpak: remote-add failed for {remote_name!r}: {outcome.message}",
                command=" ".join(argv),
                stdout=outcome.stdout.strip(),
                stderr=outcome.stderr.strip(),
            )
        return helpers.ok(
            f"flatpak: remote {remote_name} -> {url} ({installation})",
            changed=[str(installation_config(installation))],
            command=" ".join(argv),
            installation=installation,
            remote=remote_name,
            url=url,
        )

    def remove(self, source: Source) -> OperationResult:
        """``flatpak remote-delete [--user] <name>`` - only remotes we can see.

        Nothing is deleted unless the remote exists in the target installation *and*
        still points at the URL the caller described, so a stale provider entry can
        never delete a remote that has since been repointed.  ``flatpak
        remote-delete`` is called without ``--force`` (which would also remove a
        remote apps still depend on); opt in per source with ``extra["force"] = True``
        or globally with ``managers.flatpak.force_delete: true``.
        """
        installation, remote_name, url, flag, error = self._target(source)
        if error is not None:
            return error
        denied = self._root_guard(installation)
        if denied is not None:
            return denied
        remote, problem = self._find_remote(installation, remote_name)
        if problem is not None or remote is None:
            return problem or helpers.fail(
                f"flatpak: no remote named {remote_name!r} in the {installation} installation"
            )
        if url and remote.url and url.rstrip("/") != remote.url.rstrip("/"):
            return helpers.fail(
                f"flatpak: remote {remote_name!r} points at {remote.url}, not at {url}; "
                "refusing to delete it",
                hint="srcmgr list flatpak --json",
            )
        argv = ["flatpak", "remote-delete", flag]
        if source.extra.get("force") is True or self._config_flag("force_delete"):
            argv.append("--force")
        argv.append(remote_name)
        outcome = self._run_cli(argv)
        if not outcome.ok:
            return helpers.fail(
                f"flatpak: remote-delete failed for {remote_name!r}: {outcome.message}",
                command=" ".join(argv),
            )
        return helpers.ok(
            f"flatpak: remote {remote_name} removed ({installation})",
            changed=[str(installation_config(installation))],
            command=" ".join(argv),
            installation=installation,
            remote=remote_name,
        )

    def _modify(
        self, source: Source, options: Sequence[str], *, action: str
    ) -> OperationResult:
        installation, remote_name, url, flag, error = self._target(source)
        if error is not None:
            return error
        denied = self._root_guard(installation)
        if denied is not None:
            return denied
        _remote, problem = self._find_remote(installation, remote_name)
        if problem is not None:
            return problem
        argv = ["flatpak", "remote-modify", flag, remote_name, *options]
        outcome = self._run_cli(argv)
        if not outcome.ok:
            return helpers.fail(
                f"flatpak: {action} failed for {remote_name!r}: {outcome.message}",
                command=" ".join(argv),
            )
        return helpers.ok(
            f"flatpak: {action} {remote_name} ({installation})",
            changed=[str(installation_config(installation))],
            command=" ".join(argv),
            installation=installation,
            remote=remote_name,
        )

    def enable(self, source: Source) -> OperationResult:
        return self._modify(source, ["--enable"], action="enabled remote")

    def disable(self, source: Source) -> OperationResult:
        return self._modify(source, ["--disable"], action="disabled remote")

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """``flatpak remote-modify [--user] <name> --url=<new> [--prio=<n>]``."""
        options: list[str] = []
        new_url = str(changes.get("url") or changes.get("value") or "").strip()
        if new_url:
            options.append(f"--url={new_url}")
        priority = changes.get("priority", changes.get("prio", None))
        if priority is not None:
            parsed = _to_int(str(priority))
            if parsed is None:
                return helpers.fail(
                    f"flatpak: priority must be an integer, got {priority!r}"
                )
            options.append(f"--prio={parsed}")
        if not options:
            return helpers.fail(
                "flatpak: nothing to update (supported fields: url, priority)",
                hint="srcmgr edit flatpak <source> --set url=...",
            )
        return self._modify(source, options, action="updated remote")

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        """Cheap proof that the configured remotes can be enumerated."""
        return ["flatpak", "remote-ls", "--app", "--columns=application"]

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """Probe ``<url>`` and ``<url>/summary``.

        A flatpak repo root often answers 404 while ``/summary`` answers 200, so any
        healthy probe counts; a failure is reported, never raised.
        """
        base = str(source.url or source.extra.get("url") or "").rstrip("/")
        candidates: list[str] = []
        if base:
            candidates.append(base)
        summary = str(source.extra.get("summary_url") or summary_url(base))
        if summary and summary not in candidates:
            candidates.append(summary)

        probes: list[dict[str, Any]] = []
        winner: ProbeResult | None = None
        for url in candidates:
            try:
                result = probe_url(url, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - a probe must never raise
                result = ProbeResult(ok=False, status="error", url=url, error=str(exc))
            probes.append(result.to_dict())
            if winner is None and (result.ok or result.http_status in HEALTHY_HTTP):
                winner = result

        if winner is None:
            failed = probes[0] if probes else {}
            return TestResult(
                source_id=source.id,
                manager=self.name,
                name=source.name,
                url=base,
                ok=False,
                status=str(failed.get("status") or "no-url"),
                http_status=failed.get("http_status"),
                latency_ms=failed.get("latency_ms"),
                tls_ok=failed.get("tls_ok"),
                error=str(failed.get("error") or ("source has no URL" if not base else "")),
                checked_at=utc_now(),
                method=str(failed.get("method") or ""),
                probes=probes,
            )
        detail = f"probed {len(candidates)} URL(s)"
        if winner.url.endswith("/summary") and probes and not probes[0].get("ok"):
            detail = "remote root did not answer, but /summary did (normal for flatpak repos)"
        return TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=winner.url,
            ok=True,
            status=winner.status,
            http_status=winner.http_status,
            latency_ms=winner.latency_ms,
            tls_ok=winner.tls_ok,
            redirected_to=winner.redirected_to,
            method=winner.method,
            checked_at=utc_now(),
            detail=detail,
            probes=probes,
        )

    def refresh(self) -> OperationResult:
        """Lightweight liveness refresh: enumerate apps, do not fetch appstream.

        ``flatpak update --appstream`` re-downloads every remote's metadata (heavy,
        network bound) so it is deliberately left to the user; this only proves the
        remotes are reachable and reports how many applications are visible.
        """
        argv = list(self.native_test_command() or [])
        if not argv:
            return helpers.ok("flatpak: nothing to refresh")
        outcome = self._run_cli(argv, timeout=60.0)
        if not outcome.ok:
            return helpers.fail(
                f"flatpak: remotes are not reachable: {outcome.message}",
                command=" ".join(argv),
            )
        count = len([line for line in outcome.stdout.splitlines() if line.strip()])
        return helpers.ok(
            f"flatpak: remotes reachable ({count} app(s) visible); "
            "appstream metadata was not refreshed (run 'flatpak update --appstream' for that)",
            command=" ".join(argv),
            apps=count,
        )

    # -- misc ----------------------------------------------------------------
    def _config_flag(self, key: str) -> bool:
        config = getattr(self, "config", None)
        if config is None:
            return False
        try:
            return bool(config.get(f"managers.{self.name}.{key}", False))
        except Exception:  # noqa: BLE001 - configuration is always optional
            return False


__all__ = [
    "FAKE_ENV",
    "FLATHUB_NAME",
    "FLATHUB_URL",
    "FlatpakManager",
    "Outcome",
    "Remote",
    "expand_config_paths",
    "installation_config",
    "kind_for",
    "parse_columns_row",
    "parse_details_row",
    "parse_remotes",
    "parse_remotes_auto",
    "summary_url",
]
