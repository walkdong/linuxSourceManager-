# Source Manager (`srcmgr`)

**A plugin-based, cross-ecosystem software source / registry / mirror manager for Linux.**

Source Manager is *not* another "universal package manager": it does not install
packages.  It manages the layer below that — the **repositories, registries and mirrors**
that every ecosystem resolves packages from:

| Ecosystem | What it manages | Typical mirrors |
|---|---|---|
| APT | `/etc/apt/sources.list`, `sources.list.d/*.list`, `*.sources` (deb822) | Debian/Ubuntu official, TUNA, USTC, Aliyun, Tencent, SJTU… |
| npm / Yarn / pnpm | `registry=` in `.npmrc`, `.yarnrc`, `.yarnrc.yml` | registry.npmjs.org, npmmirror… |
| pip / PyPI | `index-url`, `extra-index-url` in `pip.conf` | PyPI, TUNA, USTC, Aliyun… |
| Cargo | `[source.crates-io] replace-with` in `config.toml` | crates.io, rsproxy, TUNA… |
| Go | the `GOPROXY` fallback list | proxy.golang.org, goproxy.cn… |
| Flatpak | remotes (system + user) | Flathub, USTC… |
| Docker | `registry-mirrors` in `daemon.json` | Docker Hub accelerators… |
| Git | `url.<base>.insteadOf` rules | ghproxy, gitclone… |
| Maven / RubyGems / Conda / Snap | `settings.xml` mirrors, `:sources:`, `channels:`, store proxy | Aliyun, Ruby China, TUNA… |

Why it exists:

1. **One place** for every source on the machine — no more remembering where
   `sources.list`, `pip.conf`, `daemon.json` and `config.toml` keep their mirrors.
2. **Mirror health** — latency/TLS/HTTP probing per source, "which mirror is fastest
   right now", so the classic "the Chinese mirror died again" problem is visible
   immediately instead of as a mysterious `apt update` failure.
3. **Safe by construction** — every change is snapshotted first, logged, and can be
   rolled back with one command.  Automatic failover exists but is **opt-in**.
4. **Two-level plugin market** — *manager* plugins (how to operate an ecosystem) and
   *source providers* (which mirrors exist) are independent, hot-installable plugins.

---

## Install / run

No installation is required to try it (only PyYAML):

```bash
git clone <this repo> sourceManager && cd sourceManager
python3 -m venv .venv && .venv/bin/pip install pyyaml
.venv/bin/python bin/srcmgr managers           # or: .venv/bin/python -m srcmgr managers
```

or install the `srcmgr` console script:

```bash
.venv/bin/pip install -e .
srcmgr managers
```

`srcmgr` needs root **only** for system-wide ecosystems (APT, Docker, system Flatpak,
Snap); every user-level ecosystem works unprivileged.

## Three ways in

| Command | What you get |
|---|---|
| `srcmgr` | **interactive terminal UI** — a Claude-Code-style REPL with Tab completion, history and `:` commands |
| `srcmgr --cli <command>` | **one-shot commands** for scripts and muscle memory |
| `srcmgr --gui` | graphical UI — reserved for later, reports "not implemented" |

```bash
srcmgr                                     # open the TUI (type :help inside)
srcmgr --tui                               # same, explicit
srcmgr --cli managers                      # one-shot: what exists here
srcmgr --cli --json sources --available    # machine readable
```

Inside the TUI you type the CLI's words without the prefix:

```
srcmgr ❯ apt list --available --china      mirrors you could switch to
srcmgr ❯ apt add tuna                      add it (snapshot taken automatically)
srcmgr ❯ apt test --all                    latency / HTTP / TLS per mirror
srcmgr ❯ apt choose                        measure, rank, pick the fastest
srcmgr ❯ apt rollback                      undo the last change
srcmgr ❯ health                            every ecosystem at a glance
srcmgr ❯ :help                             full command reference
```

The equivalent one-shot forms (use these in scripts):

```bash
srcmgr --cli apt list --available --china
srcmgr --cli apt add tuna
srcmgr --cli apt test --all
srcmgr --cli apt choose --china
srcmgr --cli apt rollback
srcmgr --cli health --watch 30          # live health of every ecosystem
srcmgr --cli fallback --enable apt      # opt-in automatic failover
srcmgr --cli history --manager apt      # what changed, when, with which backup
srcmgr --cli backup list                # snapshots; srcmgr --cli restore <id>
srcmgr --cli doctor                     # diagnose plugins, permissions, providers
```

Useful global flags, accepted **anywhere** after `--cli`:
`--json`, `--porcelain` (tab separated, for scripts), `--dry-run/-n`, `-y`,
`-v/-q`, `--no-color`, `--config FILE`.

> **Note:** one-shot commands require `--cli`.  The pre-0.2 shorthand
> `srcmgr apt list` now exits with an error explaining this, because a bare `srcmgr`
> means "open the interactive UI".

## How it is built

```
TUI (srcmgr/tui)  ┐
CLI (srcmgr/cli)  ┼→  SourceManagerRuntime (srcmgr/core/runtime.py)
                       ├── PluginLoader    bundled + user plugins, trust model
                       ├── SourceRegistry  merges source providers into a catalogue
                       ├── health          HTTP/TLS/native probes, scoring, cache
                       ├── BackupStore     snapshot / restore
                       ├── HistoryStore    append-only log / rollback
                       └── Config          defaults < config.yaml < per-manager < env
                     →  ManagerPlugin implementations (srcmgr/bundled/managers/*)

Both frontends call the same command handlers, and both route user questions through
`srcmgr.core.interaction`, so a future GUI is a third peer rather than a rewrite.
See [`docs/tui.md`](docs/tui.md) and [`docs/architecture.md`](docs/architecture.md).

Every mutation follows the same pipeline:

```
guard (root/scope) → snapshot → plugin call → history → (optional) refresh
```

so `srcmgr rollback <manager>` can always undo the last change, and a failed snapshot
aborts the change before anything is written.

### Writing your own plugin

```bash
srcmgr --cli plugins new mymanager            # manager plugin skeleton
srcmgr --cli plugins new mymirrors --type provider --managers apt,npm
srcmgr --cli plugins install ./mymirrors --trust
```

* `srcmgr/bundled/managers/<name>/{plugin.yaml,manager.py}` — how to operate an ecosystem.
* `srcmgr/bundled/providers/<name>/{plugin.yaml,sources/*.yaml}` — which mirrors exist.

See [`docs/source-providers.md`](docs/source-providers.md) for the provider schema and
[`CLAUDE.md`](CLAUDE.md) for the engineering rules.  **User plugins execute Python** —
they are never run until you `srcmgr plugins trust <name>`.

### Where state lives

| Path | Contents |
|---|---|
| `~/.config/source-manager/config.yaml` | your configuration |
| `~/.local/share/source-manager/backups/<id>/` | configuration snapshots |
| `~/.local/share/source-manager/history/operations.jsonl` | append-only change log |
| `~/.cache/source-manager/health.json` | last probe result per source |

All three roots are overridable: `SRCMGR_CONFIG_HOME`, `SRCMGR_DATA_HOME`,
`SRCMGR_CACHE_HOME` (plus `SRCMGR_PLUGIN_PATH` for development plugins).

## Safety model

* Snapshot before every change; rollback restores files byte-for-byte and deletes files
  the change created.
* Automatic failover is off by default; `srcmgr fallback` only *plans* until you either
  enable it or run `--apply`.
* System ecosystems refuse to act without root and tell you so.
* Secrets (registry tokens) are declared by the plugins and redacted in every listing
  and JSON export.
* Nothing is ever written with a shell command; editors preserve comments, ordering and
  unrelated keys of your configuration files.

## Development

```bash
.venv/bin/python -m pytest tests/ -q          # hermetic: temp HOME, no root, no network
```

`PROGRESS.md` is the hand-off document (what is done, what is in progress, what is next);
`CLAUDE.md` holds the working agreement.

## Status

Alpha.  The engine, interactive TUI, CLI, the health/backup/history/rollback pipeline,
14 ecosystem manager plugins and 7 source providers (144 verified sources) are
implemented and tested (330 tests).  A GUI is planned and already reserved at
`srcmgr --gui`; it will consume `srcmgr.core` like the other frontends do.  See
`PROGRESS.md` for the current milestone and what is next.

## License

MIT.
