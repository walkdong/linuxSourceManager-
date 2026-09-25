"""Snap manager plugin: the store (or its proxy) is the source.

snapd has no per-source list - a machine either talks to the SNAPSHOT store or to a
brand store, optionally through an HTTP(S) proxy.  This plugin therefore models snap as
a *single value* manager:

* ``list_enabled()`` returns exactly one source describing the active store/proxy:
  ``https://api.snapcraft.io`` when nothing is configured, else the ``store`` or
  ``proxy.https``/``proxy.http`` value read from ``snap get system proxy`` (and, as a
  fallback, from ``snapd.conf``).
* ``add()``/``enable()``/``update()`` write the proxy with
  ``snap set system proxy.<scheme>=<url>``; ``disable()``/``remove()`` undo it with
  ``snap unset`` - and only when the configured value really is the one being removed.
* everything is ``Scope.SYSTEM`` and snapd only accepts those writes from root, so a
  non-root caller gets ``OperationResult(ok=False, ...)`` with a sudo hint instead of a
  command that fails halfway.

``snap refresh --time`` is read for the diagnostic output of :meth:`detect_detail`;
the refresh schedule itself is never modified.  ``/etc/environment`` is declared as a
config path for backup purposes only - it is not parsed for a store URL.

Test hook
---------
When ``SRCMGR_SNAP_FAKE_DIR`` is set (tests only) ``snap get system proxy`` is answered
from ``<dir>/proxy.json`` (``<dir>/core-proxy.json`` for ``snap get core proxy``),
``<dir>/snapd.conf`` stands in for the snapd configuration file, and would-be CLI
invocations are appended to ``<dir>/calls.log`` instead of being executed.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from srcmgr.core import helpers
from srcmgr.core.api import (
    ManagerPlugin,
    OperationResult,
    Scope,
    Source,
    SourceKind,
    TestResult,
)
from srcmgr.core.backup import utc_now
from srcmgr.core.health import ProbeResult, probe_url
from srcmgr.core.paths import atomic_write_text

#: Test-only override: read fixtures from ``$SRCMGR_SNAP_FAKE_DIR``.
FAKE_ENV = "SRCMGR_SNAP_FAKE_DIR"

DEFAULT_STORE = "https://api.snapcraft.io"
#: The endpoint that actually answers for the default store (SNAPSHOT API).
DEFAULT_STORE_ENDPOINT = "https://api.snapcraft.io/v2/snaps/info/core"
STORE_API_PATH = "/v2/snaps/info/core"

#: snapd configuration files, in increasing priority (also declared in plugin.yaml).
CONFIG_CANDIDATES = (Path("/var/lib/snapd/snapd.conf"), Path("/etc/snapd.conf"))

PROXY_KEYS = ("https", "http")

HEALTHY_HTTP = (200, 201, 204, 301, 302, 303, 307, 308, 401, 403, 405)

ROOT_HINT = "re-run with sudo"


def _loads(text: str) -> dict[str, Any]:
    """Parse ``snap get`` output; non-JSON and empty answers are tolerated."""
    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = value
    return out


def _proxy_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).lower(): str(item).strip()
        for key, item in value.items()
        if isinstance(item, (str, int, float)) and str(item).strip()
    }


def _norm(url: str) -> str:
    """Case/trailing-slash insensitive comparison key for URLs."""
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        path = parts.path.rstrip("/")
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"
    return text.rstrip("/").lower()


def parse_conf(text: str) -> dict[str, str]:
    """Tolerantly parse ``snapd.conf`` / ``key=value`` style configuration.

    Handles ``key: value``, ``key=value``, dotted keys (``proxy.https: ...``),
    ``[section]`` headers (``[proxy]`` + ``https = ...``) and quoted values.
    """
    values: dict[str, str] = {}
    section = ""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            continue
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if section and not key.startswith(f"{section}."):
            key = f"{section}.{key}"
        values[key] = value
    return values


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


@dataclass
class SnapState:
    """What snapd currently says about the store and its proxies."""

    url: str = DEFAULT_STORE
    store: str = ""
    proxy: dict[str, str] = field(default_factory=dict)
    proxy_cli: dict[str, str] = field(default_factory=dict)
    proxy_conf: dict[str, str] = field(default_factory=dict)
    origin: str = "default"          # store | proxy | default
    origin_detail: str = ""          # which command/file supplied the value
    config_file: str = ""
    refresh_schedule: str = ""
    errors: list[str] = field(default_factory=list)


class SnapManager(ManagerPlugin):
    """The snap store / proxy as a single Source."""

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
        """Return the fixture root when ``SRCMGR_SNAP_FAKE_DIR`` is set.

        Test-only switch: ``snap get ...`` answers come from ``<dir>/proxy.json`` and
        writes are replaced by an append to ``<dir>/calls.log``.
        """
        raw = os.environ.get(FAKE_ENV, "").strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    # -- CLI plumbing --------------------------------------------------------
    def _run_cli(self, argv: Sequence[str], *, timeout: float = 20.0) -> Outcome:
        """Run one snap invocation (or record it in fake mode); never raises."""
        fake = self._fake_mode()
        if fake is not None:
            _record_fake_call(fake, argv)
            return Outcome(faked=True)
        try:
            completed = helpers.run(list(argv), timeout=timeout)
        except subprocess.TimeoutExpired:
            return Outcome(returncode=124, error=f"timed out after {timeout:g}s")
        except OSError as exc:
            return Outcome(returncode=127, error=str(exc))
        return Outcome(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def _snap_get(self, scope: str, key: str) -> tuple[str, str]:
        """``snap get <scope> <key>``; returns ``(output, error)``."""
        fake = self._fake_mode()
        if fake is not None:
            name = "proxy.json" if scope == "system" else f"{scope}-proxy.json"
            return helpers.read_text(fake / name), ""
        outcome = self._run_cli(["snap", "get", scope, key])
        if not outcome.ok:
            return "", outcome.message
        return outcome.stdout, ""

    # -- metadata ------------------------------------------------------------
    def detect(self) -> bool:
        """True when snapd is installed, configured here, or faked by the tests."""
        if helpers.which("snap"):
            return True
        if self._fake_mode() is not None:
            return True
        return any(path.exists() for path in CONFIG_CANDIDATES)

    def config_files(self) -> list[Path]:
        """Expanded snapd config paths (globs resolved, missing plain paths kept)."""
        out: list[Path] = []
        seen: set[str] = set()
        for pattern in self.manifest.config_paths:
            text = os.path.expanduser(str(pattern))
            if any(char in text for char in "*?["):
                parent = Path(text).parent
                matches = (
                    sorted(item for item in parent.glob(Path(text).name) if item.is_file())
                    if parent.is_dir()
                    else []
                )
            else:
                matches = [Path(text)]
            for item in matches:
                key = str(item)
                if key not in seen:
                    seen.add(key)
                    out.append(item)
        return out

    def detect_detail(self) -> dict[str, Any]:
        state = self._read_state(schedule=True)
        cli = helpers.which("snap")
        return {
            "available": self.detect(),
            "cli": bool(cli),
            "cli_path": cli or "",
            "fake_mode": str(self._fake_mode() or ""),
            "files": [str(path) for path in self.config_files() if path.exists()],
            "store": state.store,
            "proxy": dict(state.proxy),
            "origin": state.origin,
            "origin_detail": state.origin_detail,
            "config_file": state.config_file,
            "refresh_schedule": state.refresh_schedule,
            "read_errors": list(state.errors),
        }

    # -- reads ---------------------------------------------------------------
    def _conf_candidates(self) -> list[Path]:
        """Config files read for a store/proxy value (fixture last, so it wins)."""
        out: list[Path] = list(CONFIG_CANDIDATES)
        fake = self._fake_mode()
        if fake is not None:
            out.append(fake / "snapd.conf")
        return out

    def _conf_values(self) -> tuple[dict[str, str], str]:
        merged: dict[str, str] = {}
        used = ""
        for path in self._conf_candidates():
            text = helpers.read_text(path)
            if not text.strip():
                continue
            values = parse_conf(text)
            if values:
                merged.update(values)
                used = str(path)
        return merged, used

    def _read_state(self, *, schedule: bool = False) -> SnapState:
        state = SnapState()
        raw_cli: dict[str, Any] = {}
        for scope in ("system", "core"):
            text, error = self._snap_get(scope, "proxy")
            if error:
                state.errors.append(f"snap get {scope} proxy: {error}")
                continue
            data = _loads(text)
            if data:
                raw_cli = _deep_merge(raw_cli, data)

        conf, conf_file = self._conf_values()
        state.proxy_cli = _proxy_map(raw_cli.get("proxy"))
        state.proxy_conf = {
            key: str(conf[f"proxy.{key}"])
            for key in PROXY_KEYS
            if conf.get(f"proxy.{key}")
        }
        state.proxy = {**state.proxy_conf, **state.proxy_cli}
        state.store = str(raw_cli.get("store") or conf.get("store") or "")

        cli_source = "snap get system proxy"
        store_cli = str(raw_cli.get("store") or "")
        if store_cli:
            state.url, state.origin, state.origin_detail = store_cli, "store", cli_source
        elif conf.get("store"):
            state.url, state.origin, state.origin_detail = (
                str(conf["store"]),
                "store",
                conf_file,
            )
        elif state.proxy_cli.get("https") or state.proxy_cli.get("http"):
            state.url = state.proxy_cli.get("https") or state.proxy_cli["http"]
            state.origin, state.origin_detail = "proxy", cli_source
        elif state.proxy_conf.get("https") or state.proxy_conf.get("http"):
            state.url = state.proxy_conf.get("https") or state.proxy_conf["http"]
            state.origin, state.origin_detail = "proxy", conf_file
        else:
            state.url, state.origin, state.origin_detail = (
                DEFAULT_STORE,
                "default",
                "built-in default",
            )
        state.config_file = state.origin_detail if state.origin_detail.endswith(".conf") else ""
        if schedule:
            state.refresh_schedule = self._refresh_schedule()
        return state

    def _refresh_schedule(self) -> str:
        outcome = self._run_cli(["snap", "refresh", "--time"], timeout=20.0)
        if not outcome.ok:
            return ""
        text = " ".join(line.strip() for line in outcome.stdout.splitlines() if line.strip())
        return text[:400]

    def _describe(self, state: SnapState) -> Source:
        is_default = _norm(state.url) == _norm(DEFAULT_STORE)
        host = urlsplit(state.url).netloc or state.url
        source = helpers.make_source(
            self.name,
            name="snap-store" if is_default else host,
            url=state.url,
            kind=SourceKind.OFFICIAL if is_default else SourceKind.MIRROR,
            scope=Scope.SYSTEM,
            enabled=True,
            is_default=is_default,
            file=state.config_file,
            provider_key="store",
            description=(
                "The SNAPSHOT store snapd uses by default."
                if is_default
                else f"snapd store/proxy configured in {state.origin_detail}."
            ),
            health={"url": self._probe_target(state, is_proxy=state.origin == "proxy")},
            extra={
                "value": state.url,
                "url": state.url,
                "store": state.store,
                "proxy": dict(state.proxy),
                "proxy_cli": dict(state.proxy_cli),
                "origin": state.origin,
                "origin_detail": state.origin_detail,
                "config_file": state.config_file,
            },
        )
        return source

    def list_enabled(self) -> list[Source]:
        """The one store/proxy snapd currently uses (empty when snap is absent)."""
        if not self.detect():
            return []
        return [self._describe(self._read_state())]

    def current(self) -> Source | None:
        items = self.list_enabled()
        return items[0] if items else None

    def list_available(self, manager: str | None = None) -> list[Source]:
        """Only the default SNAPSHOT store: snapd knows no other catalogue."""
        source = helpers.make_source(
            self.name,
            name="snap-store",
            url=DEFAULT_STORE,
            kind=SourceKind.OFFICIAL,
            scope=Scope.SYSTEM,
            enabled=False,
            is_default=True,
            provider_key="snap-store",
            description="The SNAPSHOT store endpoint snapd uses by default.",
            health={"url": DEFAULT_STORE_ENDPOINT},
            extra={
                "value": DEFAULT_STORE,
                "url": DEFAULT_STORE,
                "store": DEFAULT_STORE,
                "origin": "default",
                "endpoint": DEFAULT_STORE_ENDPOINT,
            },
        )
        return [source]

    # -- mutations -----------------------------------------------------------
    def _root_guard(self) -> OperationResult | None:
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            return None
        return helpers.fail(
            "snap store configuration requires root",
            hint=ROOT_HINT,
        )

    def _target_url(self, source: Source) -> tuple[str, OperationResult | None]:
        url = str(
            source.url or source.extra.get("value") or source.extra.get("url") or ""
        ).strip()
        if not url:
            return "", helpers.fail(f"snap: source {source.name!r} has no store/proxy URL")
        scheme = urlsplit(url).scheme.lower()
        if scheme not in PROXY_KEYS:
            return "", helpers.fail(
                f"snap: unsupported scheme {scheme or '?'} for {url!r}",
                hint="snapd proxies must be http:// or https://",
            )
        return url, None

    def _set_argv(self, url: str) -> list[str]:
        """``snap set system proxy.https=... proxy.http=...`` for an https URL."""
        scheme = urlsplit(url).scheme.lower()
        settings = [f"proxy.{scheme}={url}"]
        if scheme == "https":
            # Keep the plain-http proxy in sync with the same endpoint.
            settings.append(f"proxy.http={url}")
        return ["snap", "set", "system", *settings]

    def _set_proxy(self, source: Source, *, action: str) -> OperationResult:
        denied = self._root_guard()
        if denied is not None:
            return denied
        url, problem = self._target_url(source)
        if problem is not None:
            return problem
        argv = self._set_argv(url)
        outcome = self._run_cli(argv)
        if not outcome.ok:
            return helpers.fail(
                f"snap: could not {action} the store proxy: {outcome.message}",
                command=" ".join(argv),
                stdout=outcome.stdout.strip(),
                stderr=outcome.stderr.strip(),
            )
        return helpers.ok(
            f"snap: store proxy -> {url}",
            command=" ".join(argv),
            url=url,
            state_changed="snapd system configuration (snap set)",
            config_files=[str(path) for path in self._existing_config_files()],
            note="snapd applies proxy changes immediately; no refresh is needed",
        )

    def _existing_config_files(self) -> list[Path]:
        """Config files that exist here (informational; snapd state is not a file)."""
        return [path for path in self.config_files() if path.exists()]

    def add(self, source: Source) -> OperationResult:
        return self._set_proxy(source, action="set")

    def enable(self, source: Source) -> OperationResult:
        return self._set_proxy(source, action="set")

    def update(self, source: Source, changes: Mapping[str, Any]) -> OperationResult:
        """Same as :meth:`add` with the new URL (snap has a single value)."""
        new_url = str(changes.get("url") or changes.get("value") or "").strip()
        if not new_url:
            return helpers.fail(
                "snap: only 'url' can be edited for this manager",
                hint="srcmgr edit snap <source> --set url=http://proxy:3128",
            )
        replacement = Source(
            manager=self.name,
            name=source.name,
            url=new_url,
            kind=source.kind,
            scope=source.scope,
            extra=dict(source.extra),
        )
        return self._set_proxy(replacement, action="update")

    def _unset(self, source: Source, *, action: str) -> OperationResult:
        denied = self._root_guard()
        if denied is not None:
            return denied
        target, problem = self._target_url(source)
        if problem is not None:
            return problem
        state = self._read_state()
        matches = [
            key
            for key in PROXY_KEYS
            if _norm(state.proxy_cli.get(key, "")) == _norm(target)
        ]
        if not matches:
            if any(_norm(state.proxy_conf.get(key, "")) == _norm(target) for key in PROXY_KEYS):
                return helpers.fail(
                    f"snap: {target} comes from {state.origin_detail}, not from 'snap set'",
                    hint=f"edit {state.origin_detail} by hand (source manager does not rewrite it)",
                )
            return helpers.fail(
                f"snap: {target} is not the configured snap proxy; nothing was changed",
                hint="srcmgr list snap",
            )
        argv = ["snap", "unset", "system", *[f"proxy.{key}" for key in matches]]
        outcome = self._run_cli(argv)
        if not outcome.ok:
            return helpers.fail(
                f"snap: could not {action} the store proxy: {outcome.message}",
                command=" ".join(argv),
            )
        return helpers.ok(
            f"snap: store proxy {target} removed ({', '.join('proxy.' + key for key in matches)})",
            command=" ".join(argv),
            url=target,
            state_changed="snapd system configuration (snap unset)",
            config_files=[str(path) for path in self._existing_config_files()],
        )

    def disable(self, source: Source) -> OperationResult:
        return self._unset(source, action="unset")

    def remove(self, source: Source) -> OperationResult:
        return self._unset(source, action="unset")

    # -- health --------------------------------------------------------------
    def native_test_command(self) -> Sequence[str] | None:
        """Cheap, offline-safe proof that snapd answers."""
        return ["snap", "list"]

    def _probe_target(self, state: SnapState, *, is_proxy: bool) -> str:
        """Proxy URLs are probed directly; store URLs through the SNAPSHOT API."""
        base = (state.url or "").rstrip("/")
        if is_proxy:
            return base
        return f"{base}{STORE_API_PATH}"

    def test(self, source: Source, *, timeout: float = 8.0) -> TestResult:
        """HTTP probe of the store's SNAPSHOT endpoint (or of the configured proxy)."""
        state = self._read_state()
        origin = str(source.extra.get("origin") or state.origin)
        is_proxy = origin == "proxy"
        url = str(source.url or source.extra.get("value") or state.url or DEFAULT_STORE)
        if _norm(url) == _norm(DEFAULT_STORE):
            url = DEFAULT_STORE
        target = self._probe_target(
            SnapState(url=url, origin=origin), is_proxy=is_proxy
        )
        try:
            result = probe_url(target, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            result = ProbeResult(ok=False, status="error", url=target, error=str(exc))
        healthy = bool(result.ok or result.http_status in HEALTHY_HTTP)
        return TestResult(
            source_id=source.id,
            manager=self.name,
            name=source.name,
            url=target,
            ok=healthy,
            status=result.status if healthy else (result.status or "error"),
            http_status=result.http_status,
            latency_ms=result.latency_ms,
            tls_ok=result.tls_ok,
            redirected_to=result.redirected_to,
            method=result.method,
            checked_at=utc_now(),
            detail=(
                "probed the configured proxy"
                if is_proxy
                else "probed the SNAPSHOT API endpoint of the store"
            ),
            error="" if healthy else result.error,
            probes=[result.to_dict()],
        )

    def refresh(self) -> OperationResult:
        """Report snapd's refresh schedule; nothing is changed and nothing restarts."""
        schedule = self._refresh_schedule()
        if schedule:
            return helpers.ok(
                f"snap: nothing to refresh (store proxy changes apply immediately); "
                f"snapd refresh schedule: {schedule}",
                refresh_schedule=schedule,
            )
        return helpers.ok(
            "snap: nothing to refresh (store proxy changes apply immediately)"
        )


__all__ = [
    "CONFIG_CANDIDATES",
    "DEFAULT_STORE",
    "DEFAULT_STORE_ENDPOINT",
    "FAKE_ENV",
    "Outcome",
    "SnapManager",
    "SnapState",
    "parse_conf",
]
