# Interactive UIs

Two interactive frontends ship, both driving the **same engine**:

| Frontend | Opened by | For |
|---|---|---|
| 菜单界面 (menu UI) | `srcmgr`, `srcmgr --tui` | people who have never used a terminal — keys and mouse only |
| 打字式 REPL (shell) | `srcmgr --shell` | people who prefer typing / debugging |

Neither re-implements a command: the shell executes lines through
`srcmgr.cli.main._run_cli` (exactly what `--cli` runs), and the menu calls the same
runtime actions through `srcmgr/tui/data.py`. A command therefore behaves identically in
all three places.

## The menu UI (default)

```bash
srcmgr                 # 打开菜单界面
srcmgr --install-shortcut   # 可选：创建桌面图标，以后双击打开
```

```
 软件                    现在用的是            状态              锁定
 ⚫  APT（系统软件）        Ubuntu official      正常 · 32ms      🔓 点这里锁定
 🔒  npm（Node.js）        npmmirror            已锁定           🔒 已锁定（点击解锁）

 🩺 一键体检   ⚡ 一键换到最快源   ↩️ 撤销上一次修改   🗂 备份与还原   🚪 退出
 ↑↓ 选择   Enter 进入   鼠标可点击   L 锁定/解锁   Q 退出
```

* `Enter`/click opens the mirror chooser for that ecosystem: the current source, a
  speed-ranked shortlist (12 by default, `a` shows all), one-click switching, a
  "type a URL" entry, rescan and undo.
* `L` (or a click on the lock cell) pins an ecosystem. **A lock is a hard lock**: while it
  is on, no source change happens — not by hand, not by 一键换最快, not by automatic
  failover. `rollback`/`restore` still work (that is the escape hatch). The lock is stored
  in `$SRCMGR_DATA_HOME/locks.json` and survives restarts.
* 一键体检 probes the active source of every ecosystem; 一键换到最快源 measures the
  shortlists, shows a confirmation listing exactly what will change (and what is skipped
  because it is locked), then applies it — asking for the admin password when needed.
* The admin password is requested **inside the UI** (UAC style) through a temporary
  `SUDO_ASKPASS` helper: it never appears on a command line and is not kept on disk. It is
  asked **at most once per run**; a second refusal is explained instead of re-prompting.
* Failures are translated into plain Chinese with the action that fixes them
  (`tui/i18n.py:friendly_error`). Strings come from the message table; `SRCMGR_LANG=en`
  switches to English where a translation exists.
* The first run shows a short welcome that is never shown again
  (`general.seen_onboarding`).

## The shell (`--shell`)

```bash
srcmgr --shell         # 打字式界面，命令和 --cli 完全一样（去掉 srcmgr 前缀）
srcmgr --shell --help
```

`srcmgr --cli <command>` remains the one-shot mode and is **mandatory** for scripts:

```bash
srcmgr --cli managers
srcmgr --cli apt list --available --china
srcmgr --cli --json health
```

Running `srcmgr apt list` (no `--cli`) exits 2 and prints the corrected command, so a
script can never accidentally launch an interactive UI.

## What you see in the shell

```
Source Manager 0.1.0 - one place for every ecosystem's sources
managing: apt, flatpak, snap, docker, npm, pnpm, pip, conda, git
config:   ~/.config/source-manager/config.yaml
state:    ~/.local/share/source-manager
type :help for commands, Tab to complete, :q to leave

srcmgr ❯ apt list
APT sources  (14 installed, 40 known)
…
srcmgr ❯ apt add tuna
OK apt: added TUNA (Tsinghua) -> /etc/apt/sources.list.d/tuna.sources
backup: 20260925-061312-apt-f9cb11 (undo with 'srcmgr rollback <manager>')
```

## Shell commands

Inside the shell you type the **same words as the CLI**, minus `--cli` and minus the
program name:

| Line | Meaning |
|---|---|
| `managers` | what exists on this machine and what it uses |
| `sources --available` | every ecosystem's sources in one table |
| `apt` | shortcut for `apt list` |
| `apt list --available --china` | mirrors you could switch to |
| `apt test --all` | latency / HTTP / TLS of every candidate |
| `apt choose` | measure mirrors, then switch to the fastest |
| `apt add tuna` / `enable` / `disable` / `remove` | change sources (snapshot is automatic) |
| `apt rollback` / `health` / `history` / `doctor` | inspect and undo |
| `plugins install ./x --trust`, `providers list` | the two plugin levels |

UI-only commands start with `:` (so they can never collide with a manager name):

| Command | Effect |
|---|---|
| `:help` | the full command reference |
| `:clear` | clear the screen and redraw the banner |
| `:refresh` | re-read managers, providers and sources |
| `:stats` | commands / failures / uptime for this session |
| `:quit`, `:q`, Ctrl-D | leave |
| Ctrl-C | abandon the current line (never exits the UI) |
| Tab | complete commands, manager actions, source names and flags |
| Up / Down, Ctrl-R | history and incremental search |

## Behaviour details

* **Confirmations happen in the UI.**  Destructive operations ask `remove tuna from apt?
  [y/N]` in the TUI prompt, not on stdin (that is what
  `srcmgr.core.interaction.ask_hook` is for).
* **Machine output is neutralised.**  `--json`/`--porcelain` still work but are rendered
  as plain text: the transcript is human-facing.  Use `--cli` when you want to parse.
* **It is not a shell.**  Pipes, redirects and `;` are refused with an explanation; a
  missing program name is assumed (`apt list`, not `srcmgr --cli apt list`).
* **Nothing blocks the prompt.**  Start-up completion runs on a worker thread, and every
  plugin shell-out is capped (`SRCMGR_PLUGIN_TIMEOUT`, default 15 s) with `stdin`
  detached — `snap get` blocks forever without a controlling terminal, which used to
  freeze the UI before the first paint.
* **Errors never kill the REPL.**  A failing command prints its error and returns to the
  prompt; `:stats` counts the failures.

## Future GUI

`srcmgr --gui` is reserved and currently exits 3 with "not implemented yet".  The GUI is
expected to:

1. import `srcmgr.core` (and may reuse `srcmgr.tui.data` for view models) but never
   `srcmgr.cli` internals,
2. install its own frontend hooks via `srcmgr.core.interaction.interactive(ask=…,
   output=…, name="gui")`,
3. take over the bare `srcmgr` entry point when a graphical session is available, leaving
   `--tui` (menu) / `--shell` / `--cli` as they are.

That is the whole contract — see `docs/architecture.md`.
