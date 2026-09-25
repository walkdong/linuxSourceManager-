"""Health checking: HTTP/TLS probes, native ecosystem checks and scoring.

Design rules:

* A mirror's *reachability* is decided by an HTTP(S) probe; GPG/TLS problems are
  reported but do not by themselves declare a source dead (a broken TLS chain is still
  worth surfacing, so it is exposed as ``status="tls-error"``).
* No network dependency in tests: :func:`probe_source` and :func:`test_sources`
  accept an injectable ``probe`` callable.
* Results are cached (TTL configurable) so ``srcmgr list`` can show last-known status
  without hammering the network.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .api import Source, TestResult
from .backup import utc_now
from .paths import cache_dir, ensure_dir

DEFAULT_HEADERS = {
    "User-Agent": "source-manager/0.1 (+https://github.com/source-manager)",
    "Accept": "*/*",
    "Cache-Control": "no-cache",
}

#: Statuses that mean "the endpoint answered with usable content".
OK_HTTP = {200, 201, 204, 206, 301, 302, 303, 307, 308}

#: Statuses that still prove the *host* is alive and serving even though the exact URL
#: has no content: many repository roots legitimately answer 404 (``dl.google.com``,
#: ``repo.charm.sh``) and private registries answer 401/403 before authentication.
#: Counting these as "down" is exactly the false alarm users complain about, so they are
#: reported as ``reachable`` and count as a pass for failover purposes.
REACHABLE_HTTP = OK_HTTP | {400, 401, 403, 404, 405, 406, 410, 429}

#: 5xx means the mirror itself is broken or overloaded - a genuine failure.
BROKEN_HTTP = {500, 501, 502, 503, 504, 507, 508, 521, 522, 523, 524}

#: Statuses where a HEAD probe says "I will not answer this method" rather than
#: "nothing here", so asking again with GET is worthwhile.
HEAD_UNSUPPORTED = {401, 403, 405, 501}

_HTTP_IN_TEXT = re.compile(r"\b(?:HTTP[/ ]?\d(?:\.\d)?\s*)?(\d{3})\b")


class ProbeUnavailable(RuntimeError):
    """Raised when a check cannot even be attempted."""


@dataclass
class ProbeResult:
    ok: bool
    status: str
    url: str = ""
    http_status: int | None = None
    latency_ms: float | None = None
    tls_ok: bool | None = None
    redirected_to: str = ""
    error: str = ""
    method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "url": self.url,
            "http_status": self.http_status,
            "latency_ms": self.latency_ms,
            "tls_ok": self.tls_ok,
            "redirected_to": self.redirected_to,
            "error": self.error,
            "method": self.method,
        }


# ---------------------------------------------------------------------------
# low level probes
# ---------------------------------------------------------------------------
def probe_url(
    url: str,
    *,
    timeout: float = 8.0,
    retries: int = 0,
    check_tls: bool = True,
    method: str = "HEAD",
    headers: dict[str, str] | None = None,
) -> ProbeResult:
    """Probe one URL. Never raises for network problems."""
    method = (method or "HEAD").strip().upper() or "HEAD"
    if not url:
        return ProbeResult(ok=False, status="no-url", error="source has no URL")
    if not re.match(r"^https?://", url):
        return ProbeResult(ok=False, status="unsupported-scheme", url=url,
                           error=f"cannot probe scheme of {url!r}")

    attempt = 0
    last: ProbeResult | None = None
    while attempt <= max(0, retries):
        attempt += 1
        last = _probe_once(
            url, timeout=timeout, check_tls=check_tls, method=method, headers=headers
        )
        if last.ok:
            return last
        if method == "HEAD" and last.http_status in HEAD_UNSUPPORTED:
            # Some servers reject HEAD outright (405/501) or hide the resource from it
            # (401/403): those are worth asking again with GET.  The test must look at the
            # HTTP status, not ProbeResult.status - a 405 arrives as status="reachable".
            # A *network* error (timeout, reset, unreachable) is NOT retried with GET: a
            # server that refuses HEAD sometimes also refuses a plain GET
            # (packages.microsoft.com resets on GET), which used to turn a perfectly good
            # probe into a failure.
            fallback = _probe_once(
                url, timeout=timeout, check_tls=check_tls, method="GET", headers=headers
            )
            if fallback.ok:
                return fallback
            # Keep the HEAD answer when the GET retry failed for a network reason.
            if fallback.http_status is None:
                return last
            last = fallback
    return last or ProbeResult(ok=False, status="error", url=url)


def _probe_once(
    url: str,
    *,
    timeout: float,
    check_tls: bool,
    method: str,
    headers: dict[str, str] | None,
) -> ProbeResult:
    request = urllib.request.Request(url, method=method, headers={**DEFAULT_HEADERS, **(headers or {})})
    context = None
    if url.startswith("https://") and not check_tls:
        context = ssl._create_unverified_context()  # noqa: SLF001 - explicit opt-out
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            response.read(1024)  # touch the body so TLS/HTTP errors surface
            elapsed = (time.perf_counter() - started) * 1000
            return ProbeResult(
                ok=True,
                status="ok",
                url=url,
                http_status=response.status,
                latency_ms=round(elapsed, 1),
                tls_ok=url.startswith("https://") or None,
                redirected_to=response.url if response.url != url else "",
                method=method,
            )
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        # The server answered, which is real information: 200 means content, 401/403
        # mean "authenticate first", 404 means "no content at this exact path".
        if exc.code in OK_HTTP:
            return ProbeResult(
                ok=True,
                status="ok",
                url=url,
                http_status=exc.code,
                latency_ms=round(elapsed, 1),
                tls_ok=url.startswith("https://") or None,
                method=method,
            )
        if exc.code in REACHABLE_HTTP:
            return ProbeResult(
                ok=True,
                status="reachable",
                url=url,
                http_status=exc.code,
                latency_ms=round(elapsed, 1),
                tls_ok=url.startswith("https://") or None,
                error=f"HTTP {exc.code} {exc.reason} (host answered)",
                method=method,
            )
        return ProbeResult(
            ok=False,
            status=f"http-{exc.code}",
            url=url,
            http_status=exc.code,
            latency_ms=round(elapsed, 1),
            tls_ok=url.startswith("https://") or None,
            error=f"HTTP {exc.code} {exc.reason}",
            method=method,
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        elapsed = (time.perf_counter() - started) * 1000
        # urllib wraps a plain HTTP response error inside URLError for some schemes.
        http_status = getattr(reason, "code", None)
        if isinstance(http_status, int):
            ok = http_status in REACHABLE_HTTP
            return ProbeResult(
                ok=ok,
                status="reachable" if ok else f"http-{http_status}",
                url=url,
                http_status=http_status,
                latency_ms=round(elapsed, 1),
                tls_ok=url.startswith("https://") or None,
                error=f"HTTP {http_status} (host answered)",
                method=method,
            )
        if isinstance(reason, ssl.SSLError):
            return ProbeResult(ok=False, status="tls-error", url=url,
                               latency_ms=round(elapsed, 1), tls_ok=False,
                               error=f"TLS error: {reason}", method=method)
        if isinstance(reason, socket.timeout):
            return ProbeResult(ok=False, status="timeout", url=url,
                               latency_ms=round(elapsed, 1),
                               error=f"timed out after {timeout}s", method=method)
        return ProbeResult(ok=False, status="unreachable", url=url,
                           latency_ms=round(elapsed, 1), error=str(reason), method=method)
    except (TimeoutError, socket.timeout):
        return ProbeResult(ok=False, status="timeout", url=url,
                           latency_ms=round((time.perf_counter() - started) * 1000, 1),
                           error=f"timed out after {timeout}s", method=method)
    except (ssl.SSLError, ssl.SSLCertVerificationError) as exc:
        return ProbeResult(ok=False, status="tls-error", url=url, tls_ok=False,
                           error=f"TLS error: {exc}", method=method)
    except Exception as exc:  # noqa: BLE001 - a probe must never crash the CLI
        return ProbeResult(ok=False, status="error", url=url, error=str(exc), method=method)


def run_command_probe(
    manager: str,
    command: Sequence[str],
    *,
    timeout: float = 60.0,
    cwd: str | None = None,
) -> TestResult:
    """Run an ecosystem-native check such as ``npm ping`` and interpret the exit code."""
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - command comes from a plugin manifest
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeUnavailable(f"{command[0]}: not installed") from exc
    except subprocess.TimeoutExpired:
        return TestResult(
            manager=manager,
            source_id="native",
            name=f"{manager} native check",
            ok=False,
            status="timeout",
            method=" ".join(command),
            checked_at=utc_now(),
            error=f"timed out after {timeout}s",
        )
    elapsed = (time.perf_counter() - started) * 1000
    output = (completed.stdout or "") + (completed.stderr or "")
    http_status = parse_http_status(output)
    ok = completed.returncode == 0
    return TestResult(
        manager=manager,
        source_id="native",
        name=f"{manager} native check",
        ok=ok,
        status="ok" if ok else f"exit-{completed.returncode}",
        http_status=http_status,
        latency_ms=round(elapsed, 1),
        method=" ".join(command),
        checked_at=utc_now(),
        error="" if ok else _tail(output, 400),
        detail=_tail(output, 400) if ok else "",
    )


def parse_http_status(text: str) -> int | None:
    """Pull an HTTP status code out of command output (``npm ping``, ``apt``, ...)."""
    for match in _HTTP_IN_TEXT.finditer(text or ""):
        code = int(match.group(1))
        if 100 <= code <= 599:
            return code
    return None


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


# ---------------------------------------------------------------------------
# high level: per-source health
# ---------------------------------------------------------------------------
def source_probe_urls(source: Source, *, timeout: float = 8.0) -> list[str]:
    """URLs to probe for a source.

    Resolution order: explicit ``health.urls`` -> ``health.url`` -> ``health.paths``
    appended to the source URL (so a provider can say "prove this registry answers by
    fetching ``/v2/``") -> the source URL itself.
    """
    health = source.health or {}
    urls = [str(u) for u in (health.get("urls") or []) if u]
    if not urls and health.get("url"):
        urls = [str(health["url"])]
    if not urls:
        paths = [str(p) for p in (health.get("paths") or []) if p]
        base = str(health.get("base") or source.url or "")
        if paths and base:
            urls = [base.rstrip("/") + "/" + path.lstrip("/") for path in paths]
            if health.get("include_base", True):
                urls.insert(0, base)
    if not urls and source.url:
        urls = [source.url]
    return urls


def probe_source(
    source: Source,
    *,
    timeout: float = 8.0,
    retries: int = 0,
    check_tls: bool = True,
    check_gpg: bool = False,
    probe: Callable[..., ProbeResult] | None = None,
    native: Callable[[], TestResult] | None = None,
) -> TestResult:
    """Probe ``source`` and return a normalized :class:`TestResult`.

    A source is considered healthy when *any* of its URLs answers, or when it
    delegates to a native check that succeeds.
    """
    probe = probe or probe_url
    urls = source_probe_urls(source, timeout=timeout)
    if not urls:
        if native is not None:
            result = native()
            result.source_id = source.id
            result.name = result.name or source.name
            result.url = source.url
            return result
        return TestResult(
            source_id=source.id,
            manager=source.manager,
            name=source.name,
            url=source.url,
            ok=False,
            status="no-url",
            error="nothing to probe (no URL and no native check)",
            checked_at=utc_now(),
        )

    method = str((source.health or {}).get("method") or "HEAD").upper()
    headers = dict((source.health or {}).get("headers") or {})
    allow_missing = bool((source.health or {}).get("allow_missing", False))
    probes: list[dict[str, Any]] = []
    winner: ProbeResult | None = None
    weak: ProbeResult | None = None
    for url in urls:
        result = probe(
            url,
            timeout=timeout,
            retries=retries,
            check_tls=check_tls,
            method=method,
            headers=headers,
        )
        probes.append(result.to_dict())
        if result.status == "ok" and winner is None:
            winner = result
        elif result.ok and weak is None:
            weak = result
    if winner is None:
        winner = weak

    if winner is None:
        # All URLs failed: fall back to a native ecosystem check when the plugin has
        # one, so `apt update` can still vouch for an unreachable-from-here mirror.
        fallback_native = None
        if native is not None and not allow_missing:
            try:
                fallback_native = native()
            except ProbeUnavailable:
                fallback_native = None
        if fallback_native is not None and fallback_native.ok:
            fallback_native.source_id = source.id
            fallback_native.name = fallback_native.name or source.name
            fallback_native.url = source.url
            fallback_native.probes = probes
            fallback_native.detail = (
                fallback_native.detail + " (HTTP probe failed, native check passed)"
            ).strip()
            return fallback_native
        failed = probes[0] if probes else {}
        return TestResult(
            source_id=source.id,
            manager=source.manager,
            name=source.name,
            url=urls[0],
            ok=False,
            status=str(failed.get("status", "error")),
            http_status=failed.get("http_status"),
            latency_ms=failed.get("latency_ms"),
            tls_ok=failed.get("tls_ok"),
            redirected_to=str(failed.get("redirected_to", "")),
            method=str(failed.get("method", "")),
            error=str(failed.get("error", "")),
            checked_at=utc_now(),
            probes=probes,
        )

    ok = winner.status == "ok"
    detail_bits: list[str] = []
    if len(urls) > 1:
        detail_bits.append(f"{len(urls)} URL(s) probed")
    if winner.status == "reachable":
        detail_bits.append(
            f"host answered with HTTP {winner.http_status}; no content check at this path"
        )
    return TestResult(
        source_id=source.id,
        manager=source.manager,
        name=source.name,
        url=winner.url,
        ok=winner.ok,
        status=winner.status if ok else "reachable",
        http_status=winner.http_status,
        latency_ms=winner.latency_ms,
        tls_ok=winner.tls_ok,
        redirected_to=winner.redirected_to,
        method=winner.method,
        checked_at=utc_now(),
        detail="; ".join(detail_bits),
        probes=probes,
    )


def test_sources(
    sources: Iterable[Source],
    *,
    config: Any | None = None,
    manager: str | None = None,
    probe: Callable[..., ProbeResult] | None = None,
    max_workers: int | None = None,
    native: Callable[[], TestResult] | None = None,
    progress: Callable[[Source, TestResult], None] | None = None,
) -> list[TestResult]:
    """Probe many sources concurrently, preserving input order."""
    items = list(sources)
    if not items:
        return []
    settings = (config.health(manager) if config is not None else {}) or {}
    timeout = float(settings.get("timeout", 8.0))
    retries = int(settings.get("retries", 1))
    check_tls = bool(settings.get("check_tls", True))
    check_gpg = bool(settings.get("check_gpg", False))
    workers = int(max_workers or settings.get("max_workers", 8) or 8)

    def run(source: Source) -> TestResult:
        result = probe_source(
            source,
            timeout=timeout,
            retries=retries,
            check_tls=check_tls,
            check_gpg=check_gpg,
            probe=probe,
            native=native,
        )
        if progress is not None:
            progress(source, result)
        return result

    if len(items) == 1:
        return [run(items[0])]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(run, items))


# ---------------------------------------------------------------------------
# scoring (used by the "which mirror is fastest" view and by fallback)
# ---------------------------------------------------------------------------
def score_result(result: TestResult, source: Source | None = None) -> float:
    """Lower is better. Failed sources score ``inf``."""
    if not result.ok:
        return float("inf")
    latency = result.latency_ms if result.latency_ms is not None else 9999.0
    penalty = 0.0
    if source is not None:
        if source.url.startswith("http://"):
            penalty += 150.0
        if source.kind.value == "official":
            penalty += 25.0       # prefer mirrors when they are close in latency
        if source.priority is not None:
            penalty -= max(0, min(source.priority, 100)) * 0.5
    if result.status != "ok":
        # "reachable" (host answered 4xx) is a weak pass: heavily deprioritised but not
        # discarded, so a registry that needs auth can still be chosen when nothing else
        # answers at all.
        penalty += 500.0 if result.status == "reachable" else 1500.0
    return latency + penalty


def rank_results(pairs: Iterable[tuple[Source, TestResult]]) -> list[tuple[Source, TestResult]]:
    return sorted(pairs, key=lambda pair: (score_result(pair[1], pair[0]), pair[0].name))


def best_source(pairs: Iterable[tuple[Source, TestResult]]) -> tuple[Source, TestResult] | None:
    ranked = rank_results(pairs)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
class HealthCache:
    """Small JSON cache of the latest result per source."""

    def __init__(self, root: Path | None = None, ttl: float = 900.0) -> None:
        self.root = Path(root) if root else cache_dir()
        self.path = self.root / "health.json"
        self.ttl = ttl

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, source_id: str) -> TestResult | None:
        data = self._load().get(source_id)
        if not data:
            return None
        try:
            checked = data.get("checked_at", "")
            from datetime import datetime, timezone

            moment = datetime.fromisoformat(checked) if checked else None
            if moment is not None and self.ttl >= 0:
                age = (datetime.now(timezone.utc) - moment).total_seconds()
                if age > self.ttl:
                    return None
            return TestResult(**{k: v for k, v in data.items() if k in TestResult.__dataclass_fields__})
        except Exception:  # noqa: BLE001 - a stale cache entry must never break us
            return None

    def put(self, result: TestResult) -> None:
        data = self._load()
        data[result.source_id] = result.to_dict()
        ensure_dir(self.root)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def put_many(self, results: Iterable[TestResult]) -> None:
        data = self._load()
        for result in results:
            data[result.source_id] = result.to_dict()
        ensure_dir(self.root)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


__all__ = [
    "HealthCache",
    "ProbeResult",
    "ProbeUnavailable",
    "best_source",
    "parse_http_status",
    "probe_source",
    "probe_url",
    "rank_results",
    "run_command_probe",
    "score_result",
    "source_probe_urls",
    "test_sources",
]
