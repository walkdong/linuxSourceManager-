"""The Source Registry: the curated, plugin-provided catalogue of sources.

A manager plugin knows *how* to operate an ecosystem; the registry knows *which*
sources exist.  Providers (``providers/*/plugin.yaml`` + ``sources/*.yaml``, or a
``provider.py``) contribute entries; the registry merges them, de-duplicates, filters
by kind/country/search and annotates entries that are already active on this machine.

::

    APT (manager plugin)
      +- Debian Official      <- provider "debian-official"
      +- China mirrors        <- provider "china-mirrors" (TUNA, USTC, Aliyun, ...)
      +- Custom              <- user added through the CLI
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .api import Source, SourceKind


def normalize_url(url: str) -> str:
    """Normalize a URL for equality checks (case, trailing slash, query kept)."""
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    if scheme and netloc:
        return f"{scheme}://{netloc}{path}{query}"
    return text.rstrip("/").lower()


def source_matches(a: Source, b: Source, *, strict_suite: bool = False) -> bool:
    """True when two source descriptions point at the same place."""
    if a.manager != b.manager:
        return False
    if normalize_url(a.url) != normalize_url(b.url):
        return False
    if strict_suite and (a.suite or "") != (b.suite or ""):
        return False
    return True


@dataclass
class ProviderGroup:
    """Sources of one provider, as shown in the CLI listing."""

    name: str
    display_name: str
    description: str = ""
    version: str = "0.0.0"
    sources: list[Source] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.sources)


class SourceRegistry:
    """Merged view over every loaded provider plugin."""

    def __init__(
        self,
        providers: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        config: Any | None = None,
    ) -> None:
        self.config = config
        self._providers: list[Any] = []
        self.load_warnings: list[str] = []
        self.set_providers(providers or {})

    # -- providers -----------------------------------------------------------
    def set_providers(self, providers: Mapping[str, Any] | Sequence[Any]) -> None:
        found: list[Any] = []
        items = providers.values() if isinstance(providers, Mapping) else providers
        disabled = set()
        if self.config is not None:
            disabled = {str(n) for n in self.config.get("registry.disabled_providers", []) or []}
        include_builtin = True
        if self.config is not None:
            include_builtin = bool(self.config.get("registry.include_builtin", True))
        for item in items:
            provider = getattr(item, "provider", item)
            manifest = getattr(item, "manifest", None) or getattr(provider, "manifest", None)
            if provider is None or manifest is None:
                error = getattr(item, "error", "")
                skipped = getattr(item, "skipped", "")
                if error or skipped:
                    self.load_warnings.append(f"{manifest.name if manifest else '?'}: {error or skipped}")
                continue
            if manifest.name in disabled:
                continue
            if manifest.bundled and not include_builtin:
                continue
            found.append(provider)
        self._providers = found

    @property
    def providers(self) -> list[Any]:
        return list(self._providers)

    def provider_names(self) -> list[str]:
        return [p.manifest.name for p in self._providers]

    def provider_manifests(self) -> list[Any]:
        return [p.manifest for p in self._providers]

    # -- queries -------------------------------------------------------------
    def all_sources(self, manager: str | None = None) -> list[Source]:
        out: list[Source] = []
        for provider in self._providers:
            try:
                out.extend(provider.list_sources(manager))
            except Exception as exc:  # noqa: BLE001 - a broken provider is skipped
                self.load_warnings.append(f"{provider.manifest.name}: {exc}")
        return _dedupe(out)

    def sources_for(self, manager: str) -> list[Source]:
        return self.all_sources(manager)

    def groups_for(self, manager: str) -> list[ProviderGroup]:
        groups: dict[str, ProviderGroup] = {}
        for provider in self._providers:
            try:
                items = provider.list_sources(manager)
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            manifest = provider.manifest
            group = groups.setdefault(
                manifest.name,
                ProviderGroup(
                    name=manifest.name,
                    display_name=manifest.display_name or manifest.name,
                    description=manifest.description,
                    version=manifest.version,
                ),
            )
            group.sources.extend(items)
        for group in groups.values():
            group.sources = _dedupe(group.sources)
        return sorted(groups.values(), key=lambda g: (-g.count, g.name))

    def get(self, manager: str, key: str) -> Source | None:
        """Look up a provider source by ``provider_key`` (``tuna``) or by id."""
        for source in self.sources_for(manager):
            if source.provider_key and source.provider_key == key:
                return source
            if source.id == key:
                return source
        # Fall back to a case-insensitive name search.
        lowered = key.strip().lower()
        for source in self.sources_for(manager):
            if source.name.lower() == lowered:
                return source
        return None

    def search(
        self,
        manager: str,
        query: str,
        *,
        kinds: Iterable[SourceKind | str] | None = None,
        country: str | None = None,
        limit: int | None = None,
    ) -> list[Source]:
        wanted_kinds = {_kind(k) for k in (kinds or [])}
        needle = (query or "").strip().lower()
        results: list[Source] = []
        for source in self.sources_for(manager):
            if wanted_kinds and source.kind not in wanted_kinds:
                continue
            if country:
                countries = [
                    str(c).lower()
                    for c in (
                        source.extra.get("countries")
                        or source.extra.get("country")
                        or ([source.extra["country"]] if source.extra.get("country") else [])
                    )
                    if c
                ]
                if countries and country.lower() not in countries:
                    continue
            if needle:
                haystack = " ".join(
                    [
                        source.name,
                        source.url,
                        source.description,
                        source.provider,
                        source.provider_key,
                        source.suite,
                    ]
                ).lower()
                if needle not in haystack and not fnmatch.fnmatch(haystack, f"*{needle}*"):
                    continue
            results.append(source)
        if limit:
            return results[:limit]
        return results

    def annotate_installed(
        self, manager: str, installed: Sequence[Source]
    ) -> list[tuple[Source, Source | None]]:
        """Pair each provider source with the installed source it matches, if any."""
        pairs: list[tuple[Source, Source | None]] = []
        for candidate in self.sources_for(manager):
            match = next(
                (item for item in installed if source_matches(candidate, item, strict_suite=False)),
                None,
            )
            pairs.append((candidate, match))
        return pairs


def _kind(value: SourceKind | str) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    try:
        return SourceKind(str(value).lower())
    except ValueError:
        return SourceKind.UNKNOWN


def _dedupe(sources: Sequence[Source]) -> list[Source]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Source] = []
    for source in sources:
        key = (source.manager, source.provider_key or source.name.lower(), normalize_url(source.url))
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return out


__all__ = [
    "ProviderGroup",
    "SourceRegistry",
    "normalize_url",
    "source_matches",
]
