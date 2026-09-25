"""Terminal output helpers: tables, colours, JSON, prompts.

Kept dependency-free on purpose (no rich/click): the CLI must work on a bare
Debian/Ubuntu box with nothing but the standard library and PyYAML.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


@dataclass
class Output:
    """Handles one CLI invocation's formatting decisions."""

    json_mode: bool = False
    porcelain: bool = False
    color: bool = True
    verbose: bool = False
    quiet: bool = False

    # -- colour --------------------------------------------------------------
    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(styles) + text + RESET

    def ok(self, text: str) -> str:
        return self.paint(text, GREEN)

    def warn(self, text: str) -> str:
        return self.paint(text, YELLOW)

    def err(self, text: str) -> str:
        return self.paint(text, RED)

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)

    def strong(self, text: str) -> str:
        return self.paint(text, BOLD)

    def accent(self, text: str) -> str:
        return self.paint(text, CYAN)

    # -- printing ------------------------------------------------------------
    def print(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def info(self, text: str) -> None:
        self.print(text)

    def success(self, text: str) -> None:
        self.print(f"{self.ok('OK')} {text}" if not self.porcelain else text)

    def failure(self, text: str) -> None:
        self.print(f"{self.err('FAIL')} {text}" if not self.porcelain else text)

    def warning(self, text: str) -> None:
        self.print(f"{self.warn('WARN')} {text}" if not self.porcelain else text)

    def note(self, text: str) -> None:
        if self.verbose:
            self.print(self.dim(text))

    def error(self, text: str, hint: str | None = None) -> None:
        stream = sys.stderr
        print(f"{self.err('error')}: {text}", file=stream)
        if hint:
            print(f"  {self.dim('hint:')} {hint}", file=stream)

    # -- data ----------------------------------------------------------------
    def json(self, payload: Any) -> None:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    def emit(self, payload: Any, *, text_renderer=None, porcelain_renderer=None) -> None:
        """Emit a payload in the format the user asked for."""
        if self.json_mode:
            self.json(payload)
        elif self.porcelain and porcelain_renderer is not None:
            for line in porcelain_renderer(payload):
                print(line)
        elif text_renderer is not None:
            text_renderer(payload)
        else:
            print(payload)

    # -- tables --------------------------------------------------------------
    def table(
        self,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        aligns: Sequence[str] | None = None,
        empty: str = "(nothing to show)",
    ) -> None:
        if self.porcelain:
            for row in rows:
                print("\t".join(_plain(item) for item in row))
            return
        materialized = [[_plain(item) for item in row] for row in rows]
        if not materialized:
            self.print(self.dim(empty))
            return
        widths = [display_width(h) for h in headers]
        for row in materialized:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], display_width(cell))
        aligns = list(aligns or ["left"] * len(headers))
        head = "  ".join(
            _pad(str(header), widths[index], aligns[index] if index < len(aligns) else "left")
            for index, header in enumerate(headers)
        )
        print(self.strong(head))
        print(self.dim("  ".join("-" * width for width in widths)))
        for row in materialized:
            print(
                "  ".join(
                    _pad(cell, widths[index], aligns[index] if index < len(aligns) else "left")
                    for index, cell in enumerate(row)
                    if index < len(widths)
                )
            )

    # -- prompts -------------------------------------------------------------
    def confirm(self, question: str, *, default: bool = False, assume_yes: bool = False) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            answer = input(self.strong(question) + suffix).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        return answer in {"y", "yes"}


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)


def _pad(text: str, width: int, align: str) -> str:
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    if align == "center":
        left = padding // 2
        return " " * left + text + " " * (padding - left)
    return text + " " * padding


def _plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def term_supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        return False
    return os.environ.get("TERM", "") not in {"", "dumb"}


def human_latency(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1:
        return "<1 ms"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.2f} s"


def human_size(count: int) -> str:
    return f"{count}"


def fit(text: str, width: int) -> str:
    """Truncate to ``width`` display columns with an ellipsis."""
    if display_width(text) <= width:
        return text
    out = ""
    used = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "…"


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001
        return default


def health_badge(ok: bool | None, status: str = "") -> str:
    if ok is None:
        return "?"
    if ok:
        return "OK"
    return status.upper() if status else "FAIL"


def resolve_color(mode: str) -> bool:
    """``auto`` | ``always`` | ``never`` -> whether to colourise output."""
    mode = (mode or "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return term_supports_color()


def shorten_url(url: str, width: int = 46) -> str:
    return fit(url, width)


__all__ = [
    "Output",
    "resolve_color",
    "display_width",
    "fit",
    "health_badge",
    "human_latency",
    "human_size",
    "shorten_url",
    "term_supports_color",
    "terminal_width",
]
