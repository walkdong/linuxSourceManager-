# Interactive terminal UI (`srcmgr --tui`)

The TUI is a **frontend for the same engine**: every line you type becomes a CLI argument
list that is executed by `srcmgr.cli.main._run_cli`.  There is one implementation of
"add an apt mirror", not two, so the TUI can never drift from the CLI.

## Starting it

```bash
srcmgr                 # bare name = interactive UI
srcmgr --tui           # same, explicit
srcmgr --tui --help    # what the three frontends are
```

`srcmgr --cli <command>` is the one-shot mode and is now **mandatory** for scripts:

```bash
srcmgr --cli managers
srcmgr --cli apt list --available --china
srcmgr --cli --json health
```

Running `srcmgr apt list` (no `--cli`) exits 2 and prints the corrected command, so a
script can never accidentally launch an interactive UI.

## What you see

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

## Commands

Inside the UI you type the **same words as the CLI**, minus `--cli` and minus the
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

1. import `srcmgr.core` only (never `srcmgr.cli`),
2. install its own frontend hooks via `srcmgr.core.interaction.interactive(ask=…,
   output=…, name="gui")`,
3. take over the bare `srcmgr` entry point, leaving `--tui`/`--cli` as they are.

That is the whole contract — see `docs/architecture.md`.
