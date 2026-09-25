"""Command line interface for Source Manager.

Usage is deliberately git-like::

    srcmgr                          # overview of every detected ecosystem
    srcmgr managers                 # which ecosystems exist here
    srcmgr sources [--available]    # all sources, all ecosystems
    srcmgr apt                      # == srcmgr apt list
    srcmgr apt list --available --china
    srcmgr apt add tuna             # add/enable a registry mirror
    srcmgr apt enable tuna          # make it the active source
    srcmgr apt disable #2
    srcmgr apt remove tuna
    srcmgr apt test --all           # latency/健康 check of every mirror
    srcmgr apt refresh              # apt-get update
    srcmgr health [--watch]         # health of every ecosystem
    srcmgr fallback --enable apt    # opt-in automatic failover
    srcmgr history [--manager apt]  # what changed, when
    srcmgr backup list
    srcmgr rollback apt             # undo the last change
    srcmgr plugins list|install|trust|new
    srcmgr providers list|search
    srcmgr config get|set|edit|path
    srcmgr doctor [--json]
"""

from __future__ import annotations

import argparse
import io
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import APP_NAME, __version__
from ..core import runtime as runtime_module
from ..core.api import PluginType, Source, SourceKind
from ..core.config import Config, parse_value
from ..core.errors import SourceManagerError
from ..core.paths import config_file, describe_path
from ..core.runtime import ChangePlan, SourceManagerRuntime, canonical_manager
from ..core.selection import selection_key
from . import commands as cmd
from .output import Output, human_latency, resolve_color

PROGRAM = "srcmgr"

MANAGER_ACTIONS = (
    "list",
    "available",
    "current",
    "add",
    "enable",
    "disable",
    "remove",
    "edit",
    "test",
    "choose",
    "refresh",
    "backup",
    "restore",
    "rollback",
)

COMMANDS = (
    "managers",
    "sources",
    "test",
    "health",
    "fallback",
    "history",
    "backup",
    "rollback",
    "restore",
    "plugins",
    "providers",
    "config",
    "doctor",
    "version",
    "help",
)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Source Manager - manage the software sources, registries and mirrors of "
            "every ecosystem on this machine (APT, npm, PyPI, Cargo, Flatpak, Docker, ...)."
        ),
        epilog=(
            "Run 'srcmgr managers' for the ecosystems detected here, 'srcmgr apt list "
            "--available' to see mirrors, or 'srcmgr doctor' when something looks wrong."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_globals(parser)
    add_commands(parser, COMMANDS)
    return parser


def _add_globals(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", metavar="FILE", help="use an alternate config.yaml")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine readable output")
    parser.add_argument(
        "--porcelain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="tab separated, no headers (for scripts)",
    )
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default=argparse.SUPPRESS, help="colourise output"
    )
    parser.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="same as --color never"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="assume yes for prompts"
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", default=argparse.SUPPRESS, help="show what would change"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more detail"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="less output"
    )
    parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help="show tracebacks")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")


#: Flags accepted both before and after the sub-command (git-style convenience).
INHERITED_FLAGS = ("yes", "dry_run", "verbose", "quiet")


def _inherit_flags(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Re-declare the common global flags on a sub-parser.

    ``argparse`` sub-parsers do not inherit the parent's optional arguments, and users
    reasonably type ``srcmgr apt add -y`` as well as ``srcmgr -y apt add``. ``default=None``
    keeps the parent value when the flag is only given globally.
    """
    parser.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="assume yes")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", default=argparse.SUPPRESS, help="show what would change"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more detail"
    )
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="less output")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine readable output")
    parser.add_argument(
        "--porcelain", action="store_true", default=argparse.SUPPRESS, help="tab separated output"
    )
    parser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="disable colour")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
        help="colourise output",
    )
    return parser


def add_commands(parser: argparse.ArgumentParser, commands: Sequence[str]) -> None:
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # managers ---------------------------------------------------------------
    p = subparsers.add_parser("managers", help="list ecosystems and whether they are usable")
    p.add_argument("--available-only", action="store_true", help="hide ecosystems not present here")
    p.add_argument("--all", action="store_true", help="include plugins skipped on this platform")
    p.set_defaults(handler=_cmd_managers)

    # sources ----------------------------------------------------------------
    p = subparsers.add_parser("sources", help="list sources of every ecosystem")
    p.add_argument("--available", "-a", action="store_true", help="show registry entries too")
    p.add_argument("--manager", "-m", action="append", help="limit to one manager (repeatable)")
    p.add_argument("--china", action="store_true", help="only China mirrors")
    p.add_argument("--kind", action="append", choices=[k.value for k in SourceKind])
    p.add_argument("--search", "-s", metavar="TEXT", help="filter by name/url")
    p.set_defaults(handler=_cmd_sources)

    # test -------------------------------------------------------------------
    p = subparsers.add_parser("test", help="test reachability and latency of sources")
    p.add_argument("what", nargs="*", help="manager and/or source selector")
    p.add_argument("--all", "-a", action="store_true", help="include every available source")
    p.add_argument("--native", action="store_true", help="also run the ecosystem's own check")
    p.add_argument("--timeout", type=float, default=None, help="per-probe timeout in seconds")
    p.add_argument("--json-out", metavar="FILE", help="write raw results to a file")
    p.set_defaults(handler=_cmd_test)

    # health -----------------------------------------------------------------
    p = subparsers.add_parser("health", help="health summary for every ecosystem")
    p.add_argument("--watch", nargs="?", const=60.0, type=float, metavar="SECONDS",
                   help="refresh continuously (default 60s)")
    p.add_argument("--all", "-a", action="store_true", help="probe every registry entry, not just enabled")
    p.add_argument("--fallback", action="store_true", help="also show the fallback plan")
    p.set_defaults(handler=_cmd_health)

    # fallback ---------------------------------------------------------------
    p = subparsers.add_parser("fallback", help="show, test or apply automatic failover")
    p.add_argument("manager", nargs="?", help="manager name (default: all)")
    p.add_argument("--enable", action="store_true", help="turn automatic fallback on")
    p.add_argument("--disable", action="store_true", help="turn automatic fallback off")
    p.add_argument("--apply", action="store_true", help="switch to the best reachable mirror")
    p.set_defaults(handler=_cmd_fallback)

    # history ----------------------------------------------------------------
    p = subparsers.add_parser("history", help="show recorded source changes")
    p.add_argument("--manager", "-m", help="only this manager")
    p.add_argument("--limit", "-l", type=int, default=25, help="how many entries (default 25)")
    p.add_argument("--all", action="store_true", help="no limit")
    p.add_argument("--export", metavar="FILE", help="write the log to a file")
    p.add_argument("--show", metavar="ID", help="show one entry's diff/details")
    p.set_defaults(handler=_cmd_history)

    # backup -----------------------------------------------------------------
    p = subparsers.add_parser("backup", help="create, list and restore configuration snapshots")
    p.add_argument("manager", nargs="?", help="manager to snapshot")
    p.add_argument("--list", "-l", action="store_true", help="list snapshots")
    p.add_argument("--show", metavar="ID", help="show one snapshot's manifest")
    p.add_argument("--delete", metavar="ID", help="delete a snapshot")
    p.add_argument("--prune", type=int, metavar="N", help="keep only N snapshots")
    p.set_defaults(handler=_cmd_backup)

    # rollback / restore -----------------------------------------------------
    p = subparsers.add_parser("rollback", help="undo the last change of a manager")
    p.add_argument("manager", help="manager name (or 'all')")
    p.set_defaults(handler=_cmd_rollback)

    p = subparsers.add_parser("restore", help="restore a specific snapshot")
    p.add_argument("backup_id", help="snapshot id")
    p.add_argument("manager", nargs="?", help="manager name (optional, for refresh)")
    p.set_defaults(handler=_cmd_restore)

    # plugins ----------------------------------------------------------------
    p = subparsers.add_parser("plugins", help="manage manager and provider plugins")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="plugins_action", metavar="ACTION")
    sp = sub.add_parser("list", help="list installed plugins")
    sp.add_argument("--type", choices=("manager", "provider"))
    sp.set_defaults(handler=_cmd_plugins_list)
    sp = sub.add_parser("info", help="show details about one plugin")
    sp.add_argument("name")
    sp.set_defaults(handler=_cmd_plugins_info)
    sp = sub.add_parser("install", help="install a plugin from a path, archive or URL")
    sp.add_argument("source")
    sp.add_argument("--trust", action="store_true", help="execute its code immediately")
    sp.add_argument("--force", action="store_true", help="overwrite an existing plugin")
    sp.add_argument("--name", help="override the plugin name")
    sp.set_defaults(handler=_cmd_plugins_install)
    sp = sub.add_parser("uninstall", help="remove an installed plugin")
    sp.add_argument("name")
    sp.set_defaults(handler=_cmd_plugins_uninstall)
    sp = sub.add_parser("trust", help="allow a user plugin's code to run")
    sp.add_argument("name")
    sp.add_argument("--untrust", action="store_true", help="revoke trust")
    sp.set_defaults(handler=_cmd_plugins_trust)
    sp = sub.add_parser("new", help="scaffold a new plugin")
    sp.add_argument("name")
    sp.add_argument("--type", choices=("manager", "provider"), default="manager")
    sp.add_argument("--target", help="directory to create it in (default: .)")
    sp.add_argument("--managers", default="apt", help="for providers: managers it serves")
    sp.set_defaults(handler=_cmd_plugins_new)
    sp = sub.add_parser("repo", help="show where plugins are loaded from")
    sp.set_defaults(handler=_cmd_plugins_repo)
    p.set_defaults(handler=_cmd_plugins_list)

    # providers --------------------------------------------------------------
    p = subparsers.add_parser("providers", help="manage source providers (source registries)")
    sub = p.add_subparsers(dest="providers_action", metavar="ACTION")
    sp = sub.add_parser("list", help="list source providers")
    sp.set_defaults(handler=_cmd_providers_list)
    sp = sub.add_parser("search", help="search sources across providers")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--manager", "-m", help="limit to one manager")
    sp.add_argument("--china", action="store_true", help="only sources tagged for China")
    sp.set_defaults(handler=_cmd_providers_search)
    sp = sub.add_parser("enable", help="enable a provider")
    sp.add_argument("name")
    sp.set_defaults(handler=_cmd_providers_enable)
    sp = sub.add_parser("disable", help="disable a provider")
    sp.add_argument("name")
    sp.set_defaults(handler=_cmd_providers_disable)
    sp = sub.add_parser("info", help="show a provider and its sources")
    sp.add_argument("name")
    sp.set_defaults(handler=_cmd_providers_info)
    p.set_defaults(handler=_cmd_providers_list)

    # config -----------------------------------------------------------------
    p = subparsers.add_parser("config", help="read and write Source Manager configuration")
    sub = p.add_subparsers(dest="config_action", metavar="ACTION")
    sp = sub.add_parser("get", help="print a value (or the whole config)")
    sp.add_argument("key", nargs="?", help="dotted key, e.g. health.timeout")
    sp.set_defaults(handler=_cmd_config_get)
    sp = sub.add_parser("set", help="write a value")
    sp.add_argument("key")
    sp.add_argument("value")
    sp.set_defaults(handler=_cmd_config_set)
    sp = sub.add_parser("unset", help="remove a key from the user config")
    sp.add_argument("key")
    sp.set_defaults(handler=_cmd_config_unset)
    sp = sub.add_parser("path", help="print the config file path")
    sp.set_defaults(handler=_cmd_config_path)
    sp = sub.add_parser("edit", help="open the config in $EDITOR")
    sp.set_defaults(handler=_cmd_config_edit)
    sp = sub.add_parser("managers", help="show effective per-manager settings")
    sp.add_argument("manager", nargs="?")
    sp.set_defaults(handler=_cmd_config_managers)
    p.set_defaults(handler=_cmd_config_get)

    # doctor -----------------------------------------------------------------
    p = subparsers.add_parser("doctor", help="diagnose plugins, permissions and paths")
    p.add_argument("--health", action="store_true", help="also probe every enabled source")
    p.set_defaults(handler=_cmd_doctor)

    # version / help ---------------------------------------------------------
    p = subparsers.add_parser("version", help="print the version")
    p.set_defaults(handler=_cmd_version)
    p = subparsers.add_parser("help", help="show help")
    p.set_defaults(handler=_cmd_help)


def build_manager_parser(manager: str, actions: Sequence[str] = MANAGER_ACTIONS) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROGRAM} {manager}",
        description=f"Manage {manager} software sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_globals(parser)
    sub = parser.add_subparsers(dest="action", metavar="ACTION")

    def action(name: str, help_text: str) -> argparse.ArgumentParser:
        # NOTE: the common flags (-y/--json/--porcelain/…) are handled centrally by
        # :func:`_parse_flexible`, which accepts them before *or* after the action name.
        # Declaring them here as well makes argparse ambiguous (same dest declared twice).
        return sub.add_parser(name, help=help_text)

    p = action("list", "list installed (and available) sources")
    p.add_argument("--available", "-a", action="store_true", help="include registry sources")
    p.add_argument("--enabled", action="store_true", help="only enabled sources")
    p.add_argument("--disabled", action="store_true", help="only disabled sources")
    p.add_argument("--china", action="store_true", help="only China mirrors")
    p.add_argument("--kind", action="append", choices=[k.value for k in SourceKind])
    p.add_argument("--search", "-s", metavar="TEXT")
    p.add_argument("--limit", "-l", type=int)
    p.set_defaults(handler=_cmd_manager_list)

    p = action("available", "list registry sources (mirrors you can add)")
    p.add_argument("--china", action="store_true")
    p.add_argument("--kind", action="append", choices=[k.value for k in SourceKind])
    p.add_argument("--search", "-s", metavar="TEXT")
    p.add_argument("--limit", "-l", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=_cmd_manager_available)

    p = action("current", "show the active source")
    p.set_defaults(handler=_cmd_manager_current)

    p = action("add", "add a source from the registry, a URL or a file")
    p.add_argument("spec", nargs="?", help="registry name (tuna) or URL")
    p.add_argument("--url", help="explicit URL (with --suite/--components as needed)")
    p.add_argument("--suite", help="suite/channel/branch (APT: trixie)")
    p.add_argument("--components", help="comma separated components (APT: main,contrib)")
    p.add_argument("--name", help="display name for a custom source")
    p.add_argument("--priority", type=int)
    p.add_argument("--extra", action="append", default=[], metavar="K=V",
                   help="manager specific extra field (repeatable)")
    p.set_defaults(handler=_cmd_manager_add)

    for action_name, help_text in (
        ("enable", "activate a source"),
        ("disable", "deactivate a source"),
        ("remove", "delete a source"),
    ):
        p = action(action_name, help_text)
        p.add_argument("spec", help="source name, id, #index or URL")
        p.set_defaults(
            handler=_cmd_manager_enable
            if action_name == "enable"
            else (_cmd_manager_disable if action_name == "disable" else _cmd_manager_remove)
        )

    p = action("edit", "change fields of an existing source")
    p.add_argument("spec")
    p.add_argument("--url")
    p.add_argument("--suite")
    p.add_argument("--components")
    p.add_argument("--name")
    p.set_defaults(handler=_cmd_manager_edit)

    p = action("test", "probe reachability, TLS and latency")
    p.add_argument("spec", nargs="*", help="source selectors (default: all enabled)")
    p.add_argument("--all", "-a", action="store_true", help="every registry source")
    p.add_argument("--china", action="store_true", help="limit to China mirrors")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--native", action="store_true", help="run the ecosystem's own check too")
    p.add_argument("--json-out", metavar="FILE")
    p.set_defaults(handler=_cmd_manager_test)

    p = action("choose", "measure all mirrors, then pick the best")
    p.add_argument("--china", action="store_true", help="restrict to China mirrors")
    p.add_argument("--kind", action="append", choices=[k.value for k in SourceKind])
    p.add_argument("--limit", type=int, default=12, help="how many candidates to measure")
    p.set_defaults(handler=_cmd_manager_choose)

    p = action("refresh", "run the ecosystem's update command")
    p.set_defaults(handler=_cmd_manager_refresh)

    p = action("backup", "snapshot this manager's configuration")
    p.add_argument("--reason", default="manual")
    p.add_argument("--list", "-l", action="store_true", help="list snapshots instead")
    p.set_defaults(handler=_cmd_manager_backup)

    p = action("restore", "restore a snapshot")
    p.add_argument("backup_id", nargs="?", help="snapshot id (default: the latest)")
    p.set_defaults(handler=_cmd_manager_restore)

    p = action("rollback", "undo the last change")
    p.set_defaults(handler=_cmd_manager_rollback)
    return parser


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # git-style manager shortcuts: `srcmgr apt list` / `srcmgr -n apt add ...`
    manager_token = _first_positional(argv)
    if manager_token is not None and manager_token not in COMMANDS:
        candidate = canonical_manager(manager_token)
        index = argv.index(manager_token)
        head = argv[:index]
        tail = argv[index + 1:]
        # `srcmgr apt` with no action means `srcmgr apt list`.
        action = tail if tail and not tail[0].startswith("-") else ["list", *tail]
        manager_parser = build_manager_parser(candidate)
        args = _parse_flexible(manager_parser, [*head, *action])
        return _run(manager_parser, args, manager=candidate, raw_argv=argv)

    parser = build_parser()
    if not argv:
        argv = ["managers"]
    elif argv[0] in {"--version", "-h", "--help"}:
        parser.parse_args(argv)  # let argparse handle it (and exit)
    args = _parse_flexible(parser, argv)
    if not getattr(args, "handler", None):
        args = _parse_flexible(parser, ["managers"])
    return _run(parser, args, manager=None, raw_argv=argv)


def _parse_flexible(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
) -> argparse.Namespace:
    """Parse a command line in which the common flags may appear anywhere.

    ``argparse`` sub-parsers do not inherit the parent's optionals, and users type both
    ``srcmgr -n apt add …`` and ``srcmgr apt add … -n``.  This helper splits the common
    flags out wherever they are, parses the remaining action arguments with the action
    parser, and finally fills in any attribute the action did not set from the global
    parser.  No destination is ever declared twice, so there is no default ambiguity.

    ``argv`` must be the *complete* command line: splitting a partial slice would lose
    flags that appear before the action name (``srcmgr --json managers``).
    """
    tokens = [str(a) for a in argv]
    values, action = _split_globals(tokens)
    args = parser.parse_args(action)
    if values:
        global_args = build_parser().parse_args(values)
        for key, value in vars(global_args).items():
            # The action parser is authoritative: only fill in what it left unset
            # (argparse writes ``None`` for optional positionals we never declared).
            if key not in vars(args) or vars(args)[key] is None:
                setattr(args, key, value)
    return args


def _split_globals(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate the global flags from the action arguments (order independent)."""
    values: list[str] = []
    action: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in GLOBAL_FLAG_ARITY:
            values.append(token)
            arity = GLOBAL_FLAG_ARITY[token]
            if arity and index + 1 < len(argv):
                values.append(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        if "=" in token:
            name, _, _ = token.partition("=")
            if name in GLOBAL_FLAG_ARITY:
                values.append(token)
                index += 1
                continue
        action.append(token)
        index += 1
    return values, action


#: Global flags and how many values they consume, so they can be recognised in any
#: position.  Keep in sync with :func:`_add_globals`.
GLOBAL_FLAG_ARITY: dict[str, int] = {
    "--config": 1,
    "--json": 0,
    "--porcelain": 0,
    "--color": 1,
    "--no-color": 0,
    "-y": 0,
    "--yes": 0,
    "-n": 0,
    "--dry-run": 0,
    "-v": 0,
    "--verbose": 0,
    "-q": 0,
    "--quiet": 0,
    "--debug": 0,
    "--version": 0,
}


def _positional_index(argv: Sequence[str]) -> int | None:
    """Index of the first non-flag token (skipping values of global flags)."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in GLOBAL_FLAG_ARITY:
            index += 1 + GLOBAL_FLAG_ARITY[token]
            continue
        if token.startswith("-") and "=" in token and token.split("=", 1)[0] in GLOBAL_FLAG_ARITY:
            index += 1
            continue
        return index
    return None


def _first_positional(argv: Sequence[str]) -> str | None:
    """First non-flag token, used to detect the git-style ``srcmgr <manager> ...`` form."""
    index = _positional_index(argv)
    return argv[index] if index is not None else None



def _run(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    manager: str | None,
    raw_argv: Sequence[str],
) -> int:
    from ..core.errors import ManifestError, PermissionDenied

    color_mode = "never" if getattr(args, "no_color", False) else (getattr(args, "color", None) or "auto")
    out = Output(
        json_mode=bool(getattr(args, "json", False)),
        porcelain=bool(getattr(args, "porcelain", False)),
        color=resolve_color(color_mode),
        verbose=bool(getattr(args, "verbose", False)),
        quiet=bool(getattr(args, "quiet", False)),
    )
    try:
        config_path = getattr(args, "config", None)
        config = Config.load(Path(config_path).expanduser() if config_path else None)
        if getattr(args, "color", None):
            # session-only override; `srcmgr config set general.color` persists it
            config.set("general.color", args.color)
        runtime = SourceManagerRuntime(
            config=config, command=f"{PROGRAM} " + " ".join(shlex.quote(a) for a in raw_argv)
        )
        setattr(args, "manager", manager or getattr(args, "manager", None))
        return int(args.handler(runtime, args, out) or 0)
    except KeyboardInterrupt:
        out.error("interrupted")
        return 130
    except PermissionDenied as exc:
        out.error(str(exc))
        return 4
    except SourceManagerError as exc:
        out.error(exc.message, exc.hint)
        return 2
    except ManifestError as exc:  # pragma: no cover - defensive
        out.error(f"plugin manifest problem: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - last resort, hide tracebacks unless --debug
        if getattr(args, "debug", False):
            raise
        out.error(f"unexpected error: {exc}", hint="re-run with --debug for a traceback")
        return 1


# ---------------------------------------------------------------------------
# handlers (thin wrappers around srcmgr.cli.commands)
# ---------------------------------------------------------------------------
def _cmd_managers(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.managers(runtime, args, out)


def _cmd_sources(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.sources(runtime, args, out)


def _cmd_test(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.test(runtime, args, out)


def _cmd_health(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.health(runtime, args, out)


def _cmd_fallback(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.fallback(runtime, args, out)


def _cmd_history(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.history(runtime, args, out)


def _cmd_backup(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.backup(runtime, args, out)


def _cmd_rollback(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.rollback(runtime, args, out)


def _cmd_restore(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.restore(runtime, args, out)


def _cmd_plugins_list(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_list(runtime, args, out)


def _cmd_plugins_info(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_info(runtime, args, out)


def _cmd_plugins_install(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_install(runtime, args, out)


def _cmd_plugins_uninstall(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_uninstall(runtime, args, out)


def _cmd_plugins_trust(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_trust(runtime, args, out)


def _cmd_plugins_new(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_new(runtime, args, out)


def _cmd_plugins_repo(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.plugins_repo(runtime, args, out)


def _cmd_providers_list(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.providers_list(runtime, args, out)


def _cmd_providers_search(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.providers_search(runtime, args, out)


def _cmd_providers_info(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.providers_info(runtime, args, out)


def _cmd_providers_enable(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.providers_toggle(runtime, args, out, enable=True)


def _cmd_providers_disable(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.providers_toggle(runtime, args, out, enable=False)


def _cmd_config_get(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_get(runtime, args, out)


def _cmd_config_set(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_set(runtime, args, out)


def _cmd_config_unset(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_unset(runtime, args, out)


def _cmd_config_path(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_path(runtime, args, out)


def _cmd_config_edit(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_edit(runtime, args, out)


def _cmd_config_managers(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.config_managers(runtime, args, out)


def _cmd_doctor(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.doctor(runtime, args, out)


def _cmd_version(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    out.print(f"{APP_NAME} {__version__}")
    return 0


def _cmd_help(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    build_parser().print_help()
    return 0


# ---------------------------------------------------------------------------
# manager handlers
# ---------------------------------------------------------------------------
def _cmd_manager_list(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_list(runtime, args, out)


def _cmd_manager_available(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_available(runtime, args, out)


def _cmd_manager_current(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_current(runtime, args, out)


def _cmd_manager_add(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_add(runtime, args, out)


def _cmd_manager_enable(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_enable(runtime, args, out)


def _cmd_manager_disable(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_disable(runtime, args, out)


def _cmd_manager_remove(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_remove(runtime, args, out)


def _cmd_manager_edit(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_edit(runtime, args, out)


def _cmd_manager_test(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_test(runtime, args, out)


def _cmd_manager_choose(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_choose(runtime, args, out)


def _cmd_manager_refresh(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_refresh(runtime, args, out)


def _cmd_manager_backup(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_backup(runtime, args, out)


def _cmd_manager_restore(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_restore(runtime, args, out)


def _cmd_manager_rollback(runtime: SourceManagerRuntime, args: argparse.Namespace, out: Output) -> int:
    return cmd.manager_rollback(runtime, args, out)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
