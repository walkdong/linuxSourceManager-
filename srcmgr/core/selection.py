"""Resolving user-supplied source selectors.

``srcmgr`` accepts several selector shapes everywhere a source must be named, so
scripts can be stable while humans stay lazy::

    srcmgr apt enable tuna            # provider key (China mirror "tuna")
    srcmgr apt enable 3               # index from the last `srcmgr apt list`
    srcmgr apt enable #3              # same, explicit
    srcmgr apt enable 9f1c2b3d        # stable source id
    srcmgr apt enable tsinghua        # case-insensitive name / URL substring
    srcmgr apt enable https://mirrors.ustc.edu.cn/debian

Ambiguity is an error listing the candidates rather than a silent guess.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .api import Source
from .errors import SourceNotFound
from .registry import normalize_url

_INDEX = re.compile(r"^#?(\d+)$")
_ID = re.compile(r"^[0-9a-f]{6,40}$")


def selection_key(source: Source) -> str:
    """Canonical, script-friendly key: provider key, else id."""
    return source.provider_key or source.id


def select_source(
    spec: str,
    sources: Sequence[Source],
    *,
    what: str = "source",
    allow_empty: bool = False,
) -> Source:
    """Resolve ``spec`` against ``sources`` (in listing order)."""
    if not spec or not spec.strip():
        raise SourceNotFound(f"empty {what} selector")
    text = spec.strip()
    items = list(sources)

    match = _INDEX.match(text)
    if match:
        index = int(match.group(1))
        if 1 <= index <= len(items):
            return items[index - 1]
        raise SourceNotFound(
            f"{what} #{index} out of range (1..{len(items)})" if items else f"no {what}s available"
        )

    lowered = text.lower()
    for source in items:
        if source.id == lowered:
            return source
    for source in items:
        if source.provider_key and source.provider_key.lower() == lowered:
            return source
    for source in items:
        if source.name.lower() == lowered:
            return source
    for source in items:
        if normalize_url(source.url) == normalize_url(text):
            return source

    substring = [s for s in items if lowered in s.name.lower() or lowered in s.url.lower()]
    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        listing = ", ".join(f"{s.name} ({selection_key(s)})" for s in substring[:8])
        raise SourceNotFound(
            f"{what} {spec!r} is ambiguous, matches {len(substring)}: {listing}",
            hint="use the provider key or the #index shown by 'list'",
        )
    if not items and allow_empty:
        raise SourceNotFound(f"no {what}s available")
    raise SourceNotFound(
        f"no {what} matching {spec!r}",
        hint="run 'srcmgr <manager> list --available' to see what exists",
    )


def select_many(specs: Iterable[str], sources: Sequence[Source], *, what: str = "source") -> list[Source]:
    out: list[Source] = []
    for spec in specs:
        source = select_source(spec, sources, what=what)
        if source not in out:
            out.append(source)
    return out


__all__ = ["select_many", "select_source", "selection_key"]
