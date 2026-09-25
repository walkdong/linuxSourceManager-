# CLAUDE.md — working agreement for this repository

This file is the **durable instruction set** for any AI agent (or human) working on
Source Manager.  Read it before touching code, then read `PROGRESS.md`.

---

## 1. What this project is

**Source Manager** (`srcmgr`) is a plugin-based, cross-ecosystem **software source /
registry / mirror manager** for Linux.

It is explicitly **not** another "universal package manager" (install/uninstall).
It manages the layer *below* that: the repositories, registries and mirrors that every
ecosystem resolves packages from.

```
Source Manager  (manages the SOURCES)
  ├── APT      → sources.list / sources.list.d/*.sources        (TUNA, USTC, official…)
  ├── npm/yarn/pnpm → registry= in .npmrc / .yarnrc(.yml)       (npmjs, npmmirror…)
  ├── pip      → index-url in pip.conf                          (PyPI, TUNA, Aliyun…)
  ├── cargo    → [source.crates-io] replace-with                (crates.io, rsproxy…)
  ├── go       → GOPROXY list                                   (proxy.golang.org, goproxy.cn…)
  ├── flatpak  → remotes                                        (Flathub, USTC…)
  ├── docker   → registry-mirrors in daemon.json                (Docker Hub accelerators…)
  ├── git      → url.<base>.insteadOf rules                     (ghproxy, gitclone…)
  ├── maven / rubygems / conda / snap                           (…)
  └── …        (plugins)
```

Value proposition, in priority order:

1. **One place** to see and switch the source of every ecosystem (no more memorising
   `sources.list`, `npm config`, `pip.conf`, `daemon.json`, `config.toml`, …).
2. **Mirror health**: latency/TLS/HTTP probing, "which mirror is fastest right now",
   and detection of the "Chinese mirror died again" class of failure.
3. **Safe by construction**: snapshot before every change, full history, rollback, and
   an automatic-fallback mode that is **opt-in** and never silently rewrites sources.
4. **Two-level plugin market**: *manager* plugins (how to operate an ecosystem) and
   *source providers* (which sources exist), independently publishable.

Four frontends share the engine: a full-screen Chinese menu UI (bare `srcmgr`), the CLI
(`srcmgr --cli …`), a typing shell (`srcmgr --shell`) and a reserved GUI slot
(`srcmgr --gui`).  The menu UI is the product's face for people who have never used a
terminal - it must never require typing.

## 2. Repository map

```
srcmgr/
  tui/             interactive frontends
    app.py         full-screen menu UI (Textual) - the default, beginner-facing
    data.py        view models + actions for the menu (lock is enforced here too)
    i18n.py        Chinese-first message table + friendly_error()
    sudo.py        admin prompt via SUDO_ASKPASS (no password on argv or disk)
    shell.py       the typing REPL (srcmgr --shell)
    completion.py  completion for the shell
  core/            engine — ecosystem agnostic, no CLI concerns
    api.py         Source/TestResult/OperationResult + ManagerPlugin/Provider contracts
    helpers.py     shared plugin building blocks (SingleValueManager, KeyValueFile, ini/…)
    plugins.py     discovery, loading, trust, install/scaffold of plugins
    registry.py    merges provider plugins into the "Source Registry"
    health.py      HTTP/TLS probes, native checks, scoring, cache
    backup.py      snapshots + restore        history.py  operation log + rollback
    config.py      layered config             paths.py    XDG dirs, atomic writes
    runtime.py     SourceManagerRuntime — the facade every frontend uses
    interaction.py frontend seam: ask/output hooks + current frontend name
    selection.py   "tuna" | "#3" | id | url → a Source
  cli/
    main.py        entry dispatch (TUI/CLI/GUI) + argparse tree for --cli
    commands.py    one function per command
    output.py      tables/colour/JSON/prompts (no rich/click dependency)
  bundled/
    managers/<name>/{plugin.yaml,manager.py}     14 manager plugins
    providers/<name>/{plugin.yaml,sources/*.yaml} source catalogues
tests/             pytest suite, hermetic (temp HOME, no root, no network)
docs/              design notes, plugin authoring guide, source-provider schema
bin/srcmgr         run straight from a checkout
```

## 3. Non-negotiable engineering rules

1. **Never write to the real system in tests.** Tests set `HOME`, `XDG_*`,
   `SRCMGR_CONFIG_HOME/DATA_HOME/CACHE_HOME` and the per-plugin roots
   (`SRCMGR_APT_ROOT`, `SRCMGR_DOCKER_ROOT`, `SRCMGR_*_FAKE_DIR`, `SRCMGR_*_RC`, …)
   to temp directories. Never `sudo`. Never touch `/etc` from a test.
2. **Atomic writes only** (`paths.atomic_write_text` / `atomic_write_bytes`): a crash
   must never leave half a sources file behind.
3. **Backup before every mutation.** `SourceManagerRuntime._mutate` snapshots first and
   refuses to change anything when the snapshot fails.
4. **Append-only history.** Every mutation writes one JSON line to
   `history/operations.jsonl`; rollback reads it. Never rewrite that file in place.
5. **No shell strings, ever.** `subprocess.run([...])` with an argument list; no
   `shell=True`; validate anything that ends up in a path (see the apt plugin's
   `_guard`).
6. **Plugins must not raise on environment problems** (missing binary, unreachable
   host, malformed user config): return a failed `OperationResult`/`TestResult` with a
   message a human can act on.
7. **Secrets** (npm tokens, registry passwords) are declared in
   `Source.secret_fields` and redacted by `Source.to_dict(redact=True)`.
8. **Preserve user files.** Editors must keep comments, ordering and unrelated keys;
   never reserialise a whole file when a targeted line edit will do.
9. **Accuracy of the registry.** A published mirror URL must have been verified with a
   real request. Dead mirrors are the exact problem this project exists to solve.
10. **Opt-in destructive behaviour.** Automatic fallback and any "replace my sources"
    action are off by default and always show a plan first.

## 4. How to work here

* Python ≥ 3.10, dependencies: `PyYAML`, `prompt_toolkit` (shell) and `textual` (menu
  UI); `pytest` for dev.
  Use the checkout's virtualenv: `.venv/bin/python` (created with `python3 -m venv .venv`).
* Run the suite: `.venv/bin/python -m pytest tests/ -q` (it sets `SRCMGR_OFFLINE=1`, so it
  never touches the network; keep it that way — a real-network test cost 70 s).
* Run the CLI from the checkout: `.venv/bin/python bin/srcmgr --cli <args>`
  (or `pip install -e .` for the `srcmgr` console script).  A bare `bin/srcmgr` opens the
  menu UI and therefore needs a real terminal; `/tmp/tui_m5b.py`-style pty scripts are how
  the UI is verified without one.
* Hermetic CLI run:
  `SRCMGR_CONFIG_HOME=/tmp/sm-cfg SRCMGR_DATA_HOME=/tmp/sm-data SRCMGR_CACHE_HOME=/tmp/sm-cache .venv/bin/python bin/srcmgr --cli …`
* **Keep `PROGRESS.md` current**: it is the hand-off document. Update its
  "Current status" and "Next step" sections *before* you stop working.
* Commit in small, described steps (`git commit`), because the next session starts by
  reading `git log`.

## 5. Adding things

**A new ecosystem** = a manager plugin directory:

```
srcmgr/bundled/managers/<name>/
  plugin.yaml     name, type: manager, scope, platforms, requires, supports, config_paths
  manager.py      class <Name>Manager(ManagerPlugin)   (or SingleValueManager)
```
Implement `detect/list_enabled/list_available/add/remove/enable/disable/update`
(`template_values()` too when provider URLs are distribution dependent), declare
`supports`, and add a hermetic test module.

**A new source catalogue** = a provider plugin directory with `plugin.yaml`
(`type: provider`, `managers: [...]`) and `sources/*.yaml`.  No Python needed.  See
`docs/source-providers.md` for the schema and the accuracy policy.

**A new CLI command** = a `_cmd_*` function in `cli/commands.py` + a parser branch in
`cli/main.py` (keep the handler thin; put logic in `core/runtime.py`).  The TUI picks it
up automatically — never duplicate a command there.

## 5a. Frontends: one engine, four entry points

```
srcmgr                 → 菜单界面 (tui/app.py)   裸命令 = 给零基础用户的全屏菜单
srcmgr --tui           → 菜单界面                显式
srcmgr --shell         → REPL    (tui/shell.py)  会打字的人的界面
srcmgr --cli <args>    → CLI                     一次性命令的唯一入口
srcmgr --gui           → GUI                     保留位，不许半成品实现
srcmgr --install-shortcut → 桌面图标（双击打开，不用终端）
```

Rules:

1. **`--cli` is mandatory** for one-shot commands; the pre-M4 shorthand (`srcmgr apt
   list`) must keep failing with a hint that shows the corrected command. Never make the
   gate lenient "just for one case" — scripts rely on a bare name never going interactive.
2. Handlers live in `cli/commands.py` and are reached through
   `cli/main.py:_run_cli(argv, out=…)`. The TUI calls exactly that; it must not call
   `plugin`/`runtime` methods directly for anything the CLI can do.
3. Anything that needs user input goes through `Output.confirm()`, which consults
   `core/interaction.ask_hook`; a frontend installs the hook with
   `core/interaction.interactive(...)`. Never call `input()` from a handler.
4. A frontend must never import another frontend. `srcmgr.core` must not import
   `srcmgr.cli` or `srcmgr.tui`.
5. New user-visible strings that a frontend renders must be **plain text** (no
   prompt_toolkit/Textual markup) — the transcript is printed, not parsed.
6. **The menu UI must stay typeless.** Every action is a key or a click; the only text
   input allowed is the optional "type a URL" entry. If a feature needs typing, it belongs
   in the CLI.
7. **All user-facing strings go through `tui/i18n.py`** (`tr("key", **values)`), Chinese
   first. Never hardcode a sentence in a widget.
8. **A lock is a hard lock** (`core/locks.py`): while an ecosystem is locked, source
   changes are refused everywhere - menu, one-click actions, automatic fallback. The only
   allowed writes are `rollback`/`restore`. The UI must show the lock state constantly and
   offer a one-click way to unlock (never a dead end).
9. **Ask for the admin password at most once per run** (`tui/sudo.py`): retry the action
   once after a successful login; if the system still refuses, explain in plain words.
   Never put the password on a command line or keep it on disk.

## 6. Definition of done for any change

* `.venv/bin/python -m pytest tests/ -q` passes, and `.venv/bin/python -m pyflakes
  srcmgr/ tests/` is clean (it has already caught two silent bugs).
* The affected CLI paths were exercised manually against a hermetic env and the real
  system was proven untouched (hash the files before/after).
* `PROGRESS.md` updated, `git log` tells the story, no debug prints left behind.
