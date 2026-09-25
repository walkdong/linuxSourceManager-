# Architecture

Source Manager is built around one idea: **the component that knows an ecosystem's file
formats must not be the component that decides which mirrors exist.**  Everything else
follows from that split.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ CLI  (srcmgr/cli)                                                            │
│   main.py      argparse tree, global flags accepted anywhere                  │
│   commands.py  one function per command (thin)                                │
│   output.py    tables / colour / JSON / prompts, no third-party deps          │
└───────────────────────────┬──────────────────────────────────────────────────┘
                            │ every command calls the runtime
┌───────────────────────────▼──────────────────────────────────────────────────┐
│ SourceManagerRuntime  (srcmgr/core/runtime.py)                                │
│   the only façade; a GUI or a script would use exactly this                   │
│                                                                              │
│   PluginLoader ──── discovers bundled + user plugins, enforces trust          │
│   SourceRegistry ── merges source providers into one catalogue                │
│   health ────────── HTTP/TLS probes, native checks, scoring, TTL cache         │
│   BackupStore ───── snapshot before change, faithful restore                  │
│   HistoryStore ──── append-only JSONL, powers rollback                        │
│   Config ────────── DEFAULTS < config.yaml < managers.<name> < SRCMGR_* env    │
└───────────────────────────┬──────────────────────────────────────────────────┘
                            │ ManagerPlugin contract (srcmgr/core/api.py)
┌───────────────────────────▼──────────────────────────────────────────────────┐
│ Manager plugins (bundled/managers/<name>/{plugin.yaml,manager.py})            │
│   the ONLY code that knows file formats and native commands                   │
│   apt flatpak snap docker npm yarn pnpm pip cargo go git maven rubygems conda │
└──────────────────────────────────────────────────────────────────────────────┘
                            ▲
                            │ SourceProviderPlugin / sources/*.yaml (data only)
┌───────────────────────────┴──────────────────────────────────────────────────┐
│ Source providers (bundled/providers/<name>/{plugin.yaml,sources/*.yaml})       │
│   debian-official ubuntu-official china-mirrors europe-mirrors cloud-vendor …  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## The two plugin levels

| | Manager plugin | Source provider |
|---|---|---|
| Answers | *how* do I operate this ecosystem? | *which* sources exist? |
| Language | Python (`manager.py`) | YAML data (`sources/*.yaml`) |
| Knows about | file formats, native commands, scope | URLs, mirror metadata, health paths |
| Must not | invent mirror URLs | touch the filesystem |
| Publishable by | anyone (runs code → needs trust) | anyone (data → still trust-gated) |

Splitting them is what makes third-party contribution cheap: a university can publish
its own mirror list without writing (or reviewing) a single line of Python, and a new
ecosystem can be added without touching any provider.

## Mutation pipeline

Every change goes through `SourceManagerRuntime._mutate`, in this order:

```
1. dry-run?     → return a plan, touch nothing, log nothing
2. guard        → system scope without root → PermissionDenied (before any write)
3. snapshot     → BackupStore.snapshot(manager, config_files()) → backup_id
                  (a failing snapshot aborts the operation: nothing is written)
4. plugin call  → plugin.add/remove/enable/disable/update(source) → OperationResult
5. record       → BackupStore.record_paths(backup_id, config_files())
                  so files the change created can be deleted on rollback
6. history      → append one JSON line: action, manager, source, backup, ok, details
7. refresh      → optional, explicit (`--refresh`) or after restore
```

`srcmgr rollback <manager>` reads step 6 and replays step 3's snapshot in reverse:
existing files are restored byte-for-byte with their original mode, files that did not
exist before are deleted.

## Health model

A probe answers one HTTP(S) request (HEAD, falling back to GET) and classifies the
result:

| Outcome | Status | Counts as healthy? |
|---|---|---|
| 2xx, 3xx | `ok` | yes |
| 400, 401, 403, 404, 405, 406, 410, 429 | `reachable` | yes, but heavily deprioritised |
| 5xx | `http-5xx` | no - the mirror itself is broken |
| timeout / DNS / connection refused | `timeout` / `unreachable` | no |
| certificate failure | `tls-error` | no |

The `reachable` bucket exists because repository *roots* legitimately 404
(`dl.google.com`, `repo.charm.sh`) and private registries answer 401 before auth;
calling those "down" is the false alarm that makes people stop trusting a health view.
Where a provider knows a better probe path it declares `health.url` or `health.paths`
(APPENDED to the URL), and the apt plugin additionally validates
`dists/<suite>/Release`.

Ranking (`health.score_result`) is latency plus penalties: plain HTTP +150 ms, official
archives +25 ms (mirrors win ties), declared `priority` helps, `reachable` +500 ms.

## Trust model

User plugins are Python that runs inside `srcmgr`.  They are discovered and *listed*
immediately but never imported until trusted, via `srcmgr plugins trust <name>` (a
`.trusted` marker file) or `plugins.trust_user_plugins: true`.  Archives are extracted
with path-traversal and symlink checks, and `plugins install` warns loudly before
trusting.  Remote installs can be disabled entirely (`plugins.allow_remote_install`).

## What is deliberately *not* here

* No package install/uninstall.  That layer belongs to apt/npm/pip/…; Source Manager
  changes which source they resolve from.
* No GUI (yet).  `srcmgr.core` is UI-agnostic precisely so one can be added later.
* No daemon.  `health --watch` is a loop; run it from a systemd timer if you want
  continuous monitoring.
* No silent rewriting.  Automatic failover is opt-in and always produces a plan first.
