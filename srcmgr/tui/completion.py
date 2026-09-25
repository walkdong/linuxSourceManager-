"""Tab completion for the interactive frontend.

Completion is deliberately built from the same tables the CLI parser uses
(:data:`srcmgr.cli.main.COMMANDS`, :data:`srcmgr.cli.main.MANAGER_ACTIONS`) plus the
live runtime, so a new command or manager shows up in the TUI without touching this
module.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Sequence

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ..cli.main import COMMANDS, MANAGER_ACTIONS, MANAGER_HINTS
from ..core.runtime import canonical_manager

#: One-line help for the top-level commands (shown as the completion's meta text).
COMMAND_HELP: dict[str, str] = {
    "managers": "list ecosystems and whether they are usable",
    "sources": "list sources of every ecosystem",
    "test": "test reachability and latency",
    "health": "health summary for every ecosystem",
    "fallback": "show, test or apply automatic failover",
    "history": "show recorded source changes",
    "backup": "create, list and restore snapshots",
    "rollback": "undo the last change of a manager",
    "restore": "restore a specific snapshot",
    "plugins": "manage manager and provider plugins",
    "providers": "manage source providers (catalogues)",
    "config": "read and write configuration",
    "doctor": "diagnose plugins, permissions and paths",
    "version": "print the version",
    "help": "show the available commands",
    "quit": "leave the interactive UI (also :q, Ctrl-D)",
    "exit": "leave the interactive UI (also :q, Ctrl-D)",
    "refresh": "reload plugins, providers and completion",
}

ACTION_HELP: dict[str, str] = {
    "list": "installed sources (add --available for the catalogue)",
    "available": "registry sources you can add",
    "current": "the source the ecosystem is using now",
    "add": "add a source by name or --url",
    "enable": "activate a source",
    "disable": "deactivate a source",
    "remove": "delete a source",
    "edit": "change fields of an existing source",
    "test": "probe reachability, TLS and latency",
    "choose": "measure mirrors and pick the fastest",
    "refresh": "run the ecosystem's native update",
    "backup": "snapshot this manager's configuration",
    "restore": "restore a snapshot",
    "rollback": "undo the last change",
}

#: Convenience words that start with ':' and are always offered first.
META_COMMANDS = (":help", ":quit", ":clear", ":refresh", ":stats")


@dataclass
class Session:
    """Cached view of the command surface, refreshed on demand.

    Refreshing shells out to the ecosystem CLIs (that is where the source lists live), so
    it runs on a worker thread at start-up.  The three containers below are therefore only
    ever *replaced* wholesale, never mutated in place, which keeps readers consistent
    without holding a lock while a plugin runs.
    """

    managers: list[str] = field(default_factory=list)
    available: dict[str, list[str]] = field(default_factory=dict)
    installed: dict[str, list[str]] = field(default_factory=dict)
    detected: list[str] = field(default_factory=list)
    loaded: bool = False
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def refresh(self, runtime: Any) -> None:
        """Re-read manager/provider state from the runtime (never raises)."""
        managers: list[str] = []
        available: dict[str, list[str]] = {}
        installed: dict[str, list[str]] = {}
        detected: list[str] = []
        error = ""
        try:
            managers = list(runtime.manager_names())
        except Exception as exc:  # noqa: BLE001 - completion must never break the prompt
            error = str(exc)
        for name in managers:
            try:
                if runtime.status(name).available:
                    detected.append(name)
            except Exception:  # noqa: BLE001 - detection is informational here
                pass
            try:
                available[name] = [_key_of(source) for source in runtime.available(name)]
            except Exception:  # noqa: BLE001 - a broken manager loses its completions only
                available[name] = []
            try:
                installed[name] = [_key_of(source) for source in runtime.installed(name)]
            except Exception:  # noqa: BLE001
                installed[name] = []
        with self._lock:
            self.managers = managers
            self.available = available
            self.installed = installed
            self.detected = detected
            self.error = error
            self.loaded = True

    def words_for(self, tokens: Sequence[str]) -> list[tuple[str, str]]:
        """``(word, help)`` pairs that make sense after ``tokens``.

        ``tokens`` is the text *before* the word being completed, so ``[]`` means "the
        user is at the start of the line" and completes the top-level commands (plus the
        ``:`` UI commands, added by the caller).
        """
        if not tokens:
            # top-level words = built-in commands + the ecosystems, which are invoked as
            # `apt list`, `npm add ...` even though they are not CLI sub-commands.
            entries = [(command, COMMAND_HELP.get(command, "")) for command in COMMANDS]
            seen = {word for word, _ in entries}
            managers = self.managers or (sorted(MANAGER_HINTS) if not self.loaded else [])
            for name in managers:
                if name in seen:
                    continue
                seen.add(name)
                entries.append((name, "ecosystem"))
            return entries
        head = canonical_manager(tokens[0])
        # A manager name completes its actions even before the runtime has been read
        # (the name is static; only *which* sources exist needs the runtime).
        is_manager = head in self.managers or (not self.loaded and head in MANAGER_HINTS)
        if is_manager:
            if len(tokens) == 1:
                return [(action, ACTION_HELP.get(action, "")) for action in MANAGER_ACTIONS]
            if len(tokens) == 2 and tokens[1] not in MANAGER_ACTIONS:
                # a half-typed action: offer the action names again so Tab can fix it
                return [(action, ACTION_HELP.get(action, "")) for action in MANAGER_ACTIONS]
            action = tokens[1]
            if action in {"enable", "disable", "remove", "edit", "test", "choose"}:
                return [(name, "installed") for name in self.installed.get(head, [])]
            if action == "add":
                return [(name, "registry") for name in self.available.get(head, [])]
            return []
        return [(command, COMMAND_HELP.get(command, "")) for command in COMMANDS]


def _key_of(source: Any) -> str:
    """Completion word for a source: the provider key when there is one, else the id."""
    return str(getattr(source, "provider_key", "") or getattr(source, "id", ""))


class SrcmgrCompleter(Completer):
    """Context-aware completer for the TUI prompt."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _tokens(self, document: Document) -> list[str]:
        text = document.text_before_cursor
        return [token for token in text.split() if token]

    def get_completions(self, document: Document, complete_event):  # noqa: ANN201
        """Yield completions for the word under the cursor.

        Position rules (see :meth:`Session.words_for`):

        * ``""`` / ``ap``                 -> top-level commands (plus ``:`` meta words)
        * ``apt `` / ``apt en``           -> that manager's actions
        * ``apt enable `` / ``... tuna``  -> installed sources of that manager
        * ``apt add ``                    -> registry sources of that manager
        * ``... --``                      -> the options that fit the action
        """
        text = document.text_before_cursor
        tokens = self._tokens(document)
        current = "" if not text or text.endswith(" ") else text.split()[-1]
        preceding = tokens if current == "" else tokens[:-1]

        if current.startswith(":"):
            candidates = [(meta, "ui command") for meta in META_COMMANDS]
        elif current.startswith("-"):
            context = preceding[1] if len(preceding) >= 2 else ""
            candidates = [(flag, "option") for flag in _FLAGS.get(context, _FLAGS[""])]
        else:
            candidates = self.session.words_for(preceding)
            if not preceding:
                candidates = [(meta, "ui command") for meta in META_COMMANDS] + candidates

        if not current:
            for word, meta in candidates:
                yield Completion(word, start_position=0, display_meta=meta)
            return
        for word, meta in candidates:
            if word.startswith(current):
                yield Completion(word, start_position=-len(current), display_meta=meta)


#: Options offered per command position (kept short: the UI is for discovery, not for
#: exhaustive flag completion - the CLI's --help remains the reference).
_FLAGS: dict[str, list[str]] = {
    "list": ["--available", "--china", "--enabled", "--disabled", "--kind", "--search", "--limit"],
    "available": ["--china", "--kind", "--search", "--limit"],
    "add": ["--url", "--suite", "--components", "--name", "--priority", "--extra"],
    "test": ["--all", "--china", "--native", "--timeout", "--json-out"],
    "choose": ["--china", "--kind", "--limit"],
    "backup": ["--list", "--reason"],
    "restore": ["--dry-run"],
    "rollback": ["--dry-run"],
    "": [
        "--json", "--porcelain", "--yes", "--dry-run", "--verbose", "--quiet",
        "--config", "--help",
    ],
}


def _flag_context(tokens: Sequence[str]) -> str:
    if len(tokens) >= 2:
        return tokens[1]
    return ""


def completion_words(session: Session, line: str) -> list[str]:
    """Plain list of completion candidates (used by tests and documentation).

    Mirrors :meth:`SrcmgrCompleter.get_completions`: the last token is treated as the
    word being completed unless the line ends with a space.
    """
    tokens = [token for token in line.split() if token]
    if line and not line.endswith(" "):
        preceding, prefix = tokens[:-1], tokens[-1]
    else:
        preceding, prefix = tokens, ""
    if line.strip().startswith(":"):
        return [meta for meta in META_COMMANDS if meta.startswith(prefix)]
    # NOTE: iterate ``(word, help)`` pairs - binding the loop variable of an earlier
    # comprehension and reusing it leaks the LAST value and silently filters everything.
    words = []
    for candidate, _help in session.words_for(preceding):
        words.append(candidate)
    if not preceding:
        words = list(META_COMMANDS) + words
    return [candidate for candidate in words if candidate.startswith(prefix)]


__all__ = [
    "ACTION_HELP",
    "COMMAND_HELP",
    "META_COMMANDS",
    "Session",
    "SrcmgrCompleter",
    "completion_words",
]
