# PROGRESS.md — Source Manager (`srcmgr`)

> **This is the hand-off document.** A new session should read, in order:
> `CLAUDE.md` → `PROGRESS.md` → `git log` → the code named below, then continue from
> the **NEXT STEP** section. Do not re-plan the project from scratch.

*Last updated: 2026-09-24 (session 1)*
*Version: 0.1.0 — GUI deliberately not started (CLI + core only).*

---

## 1. Where the project is

**Phase: IN PROGRESS — milestone M2 (source registry) → M3 (verification, docs, polish).**

The whole engine, the whole CLI and all 14 manager plugins are written and working on
this machine (Ubuntu 26.04, Python 3.14, no root needed for user-scope managers).
The curated mirror catalogue (provider plugins) is being written in parallel.

### Done

| # | Deliverable | State | Where |
|---|---|---|---|
| 1 | Core engine (config, paths, atomic IO, history, backup, health, plugin loader, registry, runtime) | ✅ working | `srcmgr/core/*.py` |
| 2 | `ManagerPlugin` / `SourceProviderPlugin` contracts, `Source` model, stable source ids | ✅ | `srcmgr/core/api.py` |
| 3 | Plugin discovery/load/trust/install/scaffold + `plugin.yaml` validation | ✅ | `srcmgr/core/plugins.py` |
| 4 | Health engine: HTTP/TLS probes, `ok` vs `reachable` classification, scoring, cache | ✅ (fixed 404=false-failure bug) | `srcmgr/core/health.py` |
| 5 | Backup snapshots (self-describing manifests, faithful restore incl. "delete what we created") | ✅ | `srcmgr/core/backup.py` |
| 6 | Operation history (append-only JSONL) + rollback + fallback planner | ✅ | `core/history.py`, `core/runtime.py` |
| 7 | Full CLI incl. git-style `srcmgr <manager> …`, `--json/--porcelain/--dry-run/-y` | ✅ | `srcmgr/cli/*` |
| 8 | Manager plugins: apt (1706 lines, 45 tests), flatpak, snap, docker, npm, yarn, pnpm, pip, cargo, go, git, maven, rubygems, conda | ✅ all 14 load & detect | `srcmgr/bundled/managers/*` |
| 9 | `template_values()` + `${codename}` expansion in `_expand_templates()` | ✅ | `core/runtime.py`, apt plugin |
| 10 | Test suite: 45 (apt) + 41 (flatpak/snap/docker) + peer modules (~180 in total at last full run) | ✅ green for those modules | `tests/` |
| 11 | Persistent project memory (CLAUDE.md, PROGRESS.md, README) | ✅ in this session | repo root |

### In progress right now

* **Provider plugins / Source Registry** (subagent): `debian-official`, `china-mirrors`,
  `europe-mirrors`, `cloud-vendor`, `corporate-template` under
  `srcmgr/bundled/providers/`, plus `docs/source-providers.md` and
  `tests/test_providers.py`. Every published mirror URL is HTTP-verified before it is
  committed (accuracy policy in CLAUDE.md §3.9).

### Known gaps / TODO (ordered)

1. Wait for the provider subagent; verify `srcmgr providers list` + `srcmgr apt
   available --china` show the catalogue and that **every** provider loads (`ok`).
2. Full-suite green: `.venv/bin/python -m pytest tests/ -q` (the `tests/test_plugins_misc.py`
   module had 8 failures mid-session from a peer agent that was still writing; re-run and
   fix the owner's regressions if any survive).
3. Add repo-level integration tests (`tests/test_cli.py`): end-to-end `srcmgr managers /
   sources / apt list / apt add --dry-run / backup / history / rollback / plugins /
   providers / config / doctor` against a hermetic env, asserting exit codes and that
   nothing outside the temp dirs was written.
4. `srcmgr apt test --all` end-to-end against the real registry (latency table) and a
   real `srcmgr apt choose` run; document the output in the README.
5. `pip install -e .` smoke test (console script `srcmgr` + `source-manager`).
6. The Microsoft apt repo probe reports `error: Remote end closed connection without
   response` (HEAD is refused). Confirm whether a GET-with-Range probe fixes it; if the
   host simply refuses, classify it as `reachable`/informational rather than failed.
7. Optional polish: `srcmgr snap` needs root for real mutations (documented), Docker
   mirror accelerator list validity varies over time (providers note it).
8. Later (not requested yet): GUI (Tauri/Qt/GTK) consuming `srcmgr.core`, plugin
   marketplace over HTTP, signed providers, `snap`/`pacman`/`dnf`/`nix` managers.

---

## 2. NEXT STEP (start here in a new session)

```bash
cd /home/alnilam/Downloads/compress/outdsh/sourceManager
.venv/bin/python -m pytest tests/ -q                       # 1. find what is red
SRCMGR_CONFIG_HOME=/tmp/sm-cfg SRCMGR_DATA_HOME=/tmp/sm-data SRCMGR_CACHE_HOME=/tmp/sm-cache \
  .venv/bin/python bin/srcmgr providers list               # 2. registry sanity
SRCMGR_CONFIG_HOME=/tmp/sm-cfg SRCMGR_DATA_HOME=/tmp/sm-data SRCMGR_CACHE_HOME=/tmp/sm-cache \
  .venv/bin/python bin/srcmgr apt available --china        # 3. mirrors visible?
```

Then do **TODO 2 → 3 → 4 → 5** above in order, updating this file after each.

---

## 3. Architecture in one screen

```
CLI (cli/main.py → cli/commands.py)
        │  every command is a thin wrapper
        ▼
SourceManagerRuntime (core/runtime.py)          ← the only façade; a GUI would use this
   ├── PluginLoader        discovers bundled + user plugins, enforces trust
   ├── SourceRegistry      merges provider plugins into the catalogue
   ├── health.test_sources probes HTTP/TLS + native checks, scores, caches
   ├── BackupStore         snapshot(paths) → backup_id ; restore(id)
   ├── HistoryStore        append-only JSONL ; rollback(id)
   └── Config              DEFAULTS < config.yaml < managers.<name> < SRCMGR_* env
        │
        ▼
ManagerPlugin implementations (bundled/managers/*) — the ONLY code that knows an
ecosystem's file formats and commands.
```

Two-level plugin model:

* **manager plugin** = "how do I operate this ecosystem" (`plugin.yaml` + `manager.py`).
* **source provider**  = "which sources exist" (`plugin.yaml` + `sources/*.yaml`, data only).

Mutation pipeline (always in this order): `guard → snapshot → plugin call → history →
(optional) refresh`, so `srcmgr rollback <manager>` can always undo the last change.

## 4. Layout of the state the tool keeps

| Path | Contents |
|---|---|
| `$SRCMGR_CONFIG_HOME` (`~/.config/source-manager`) | `config.yaml`, `plugins/` (user plugins + `.trusted` markers) |
| `$SRCMGR_DATA_HOME` (`~/.local/share/source-manager`) | `backups/<id>/{manifest.json,files/…}`, `history/operations.jsonl` |
| `$SRCMGR_CACHE_HOME` (`~/.cache/source-manager`) | `health.json` (last probe per source, TTL from config) |

Environment overrides: `SRCMGR_CONFIG_HOME`, `SRCMGR_DATA_HOME`, `SRCMGR_CACHE_HOME`,
`SRCMGR_PLUGIN_PATH`, `SRCMGR_HEALTH__TIMEOUT` (env → config, `__` splits sections).

## 5. How to verify nothing bad happened (used in every session)

```bash
find /etc/apt /etc/docker -type f -exec md5sum {} + | sort | md5sum   # before
# … run srcmgr commands with the hermetic env …
find /etc/apt /etc/docker -type f -exec md5sum {} + | sort | md5sum   # after: identical
```

## 6. Session log (newest first)

* **session 1** — project created from an empty directory.
  Engine + CLI + 14 manager plugins + health/backup/history/rollback/fallback + tests.
  Fixed: 404 wrongly counted as a dead mirror; `-n` before the manager name
  (`srcmgr -n apt add …`) ignored; dry-run prompting for confirmation; template
  expansion for provider `url_template` entries; `PermissionDenied` raised before the
  dry-run short-circuit. Started the provider/registry subagent.
  Not yet done: repo-level CLI tests, full-suite green, `pip install -e .` check,
  README/docs polish beyond `docs/source-providers.md`, first `git commit` series.
