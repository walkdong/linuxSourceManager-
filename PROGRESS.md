# PROGRESS.md — Source Manager (`srcmgr`)

> **This is the hand-off document.** A new session should read, in order:
> `CLAUDE.md` → `PROGRESS.md` → `git log` → the code named below, then continue from
> the **NEXT STEP** section. Do not re-plan the project from scratch.

*Last updated: 2026-09-25 (session 1, final pass — registry complete, 304 tests green)*
*Version: 0.1.0 — GUI deliberately not started (CLI + core only).*

---

## 1. Where the project is

**Phase: M3 complete — verified, documented, committed.** Every milestone from the
original brief is implemented; the remaining work is listed under *Known gaps* and is
optional polish.

Status: **304 tests passing**, 14 manager plugins, 7 source providers (**144 registry
entries**, every URL HTTP-verified by the provider author), CLI working end to end,
`pip install` verified from a wheel in a clean venv, nothing under `/etc` ever written
outside the sandboxed test roots (fingerprinted before/after every session).

### Done

| # | Deliverable | State | Where |
|---|---|---|---|
| 1 | Core engine: config, paths, atomic IO, history, backup, health, plugin loader, registry, runtime | ✅ | `srcmgr/core/*.py` |
| 2 | Plugin contracts: `ManagerPlugin`, `SourceProviderPlugin`, `Source`, stable ids, redaction | ✅ | `srcmgr/core/api.py` |
| 3 | Plugin discovery/load/trust/install/scaffold + manifest validation | ✅ | `srcmgr/core/plugins.py` |
| 4 | Health engine: HTTP/TLS probes, `ok` vs `reachable` classification, scoring, cache, native checks | ✅ | `srcmgr/core/health.py` |
| 5 | Snapshots + faithful restore (incl. deleting files the change created) | ✅ | `srcmgr/core/backup.py` |
| 6 | Append-only history, rollback, opt-in fallback planner | ✅ | `core/history.py`, `core/runtime.py` |
| 7 | Full CLI; global flags accepted anywhere (`--json/--porcelain/-n/-y/-v/-q/--config/--color`) | ✅ | `srcmgr/cli/*` |
| 8 | 14 manager plugins: apt, flatpak, snap, docker, npm, yarn, pnpm, pip, cargo, go, git, maven, rubygems, conda | ✅ | `srcmgr/bundled/managers/*` |
| 9 | Source registry: 7 providers, 144 entries, `${codename}`/`${distro}` templating, verified mirror URLs | ✅ | `srcmgr/bundled/providers/*` |
| 10 | Plugin/source-provider marketplace flow (install/trust/uninstall/scaffold) | ✅ | `core/plugins.py`, `cli/commands.py` |
| 11 | Tests: 304 (plugins 206 + CLI 26 + core 21 + runtime safety 13 + providers 22 + apt archive family 16) | ✅ all green | `tests/` |
| 12 | Docs: `CLAUDE.md`, `PROGRESS.md`, `README.md`, `docs/{source-providers,architecture,operations}.md`, `LICENSE` | ✅ | repo root, `docs/` |
| 13 | `pip install -e .` + console scripts `srcmgr` / `source-manager` | ✅ verified | `pyproject.toml` |

### Bugs found and fixed this session (regression-tested)

1. Backup restore resolved payloads against the wrong root → "snapshot payload missing";
   now resolved against the owning snapshot.
2. Rollback left behind files the change had created → `BackupStore.record_paths()`
   records post-change files so restore deletes exactly what was introduced.
3. Two snapshots in the same second with different content collided on one id and
   overwrote each other → the id now fingerprints file contents.
4. Provider top-level keys (`url_template`, `countries`, …) were dropped → folded into
   `Source.extra`.
5. `${codename}`/`${distro}` expansion wired through `_expand_templates` (verified:
   `tuna` → `https://mirrors.tuna.tsinghua.edu.cn/ubuntu/` on this box).
6. 404 was treated as "mirror down" → `reachable` classification (fixes false alarms for
   `dl.google.com`, Docker Hub, `repo.charm.sh`); health also supports `health.paths`.
7. Global flags were only accepted before the sub-command (argparse ambiguity) → single
   central splitter (`_parse_flexible` + `_split_globals`).
8. `PermissionDenied` fired before the `--dry-run` short-circuit.
9. `--china` leaked entries that never declared a country → country metadata is now
   authoritative.

### Bugs found and fixed this session (regression-tested)

1. Backup restore resolved payloads against the wrong root → "snapshot payload missing".
2. Rollback left behind files the change had created → `BackupStore.record_paths()`.
3. Two snapshots in the same second with different content collided on one id and
   overwrote each other → the id fingerprints file contents.
4. Provider top-level keys (`url_template`, `countries`, …) were dropped → folded into
   `Source.extra`.
5. `${codename}`/`${distro}` expansion was not wired through `_expand_templates`.
6. 404 was treated as "mirror down" → `reachable` classification (`dl.google.com`,
   Docker Hub, `repo.charm.sh` were false alarms).
7. An unresolvable placeholder was silently accepted when `url_template == url`, so a
   literal `${codename}` could be written into a sources file.
8. `apt`'s `${distro}` was the raw os-release `ID`, so Mint/Pop!_OS produced paths no
   mirror serves → `archive_family()` maps derivatives to the archive that exists, while
   Kali/Raspbian/Parrot keep their own id (loud failure instead of a wrong source).
9. A HEAD network error used to trigger a GET retry, turning a good probe into a failure
   when the host resets on GET (`packages.microsoft.com`); the retry now happens only for
   real "wrong method" statuses and inspects the HTTP code, not the verdict string.
10. Global flags were only accepted before the sub-command (argparse ambiguity) → one
    central splitter (`_parse_flexible` + `_split_globals`).
11. `PermissionDenied` fired before the `--dry-run` short-circuit.
12. `--china` leaked entries that never declared a country; state column could print
    `enabled,disabled,current`.
13. History timestamps had second resolution, so rapid changes compared equal and
    `rollback` could pick the wrong entry.

### Known gaps / TODO (optional polish, in order)

1. **`snap` has no provider catalogue** — by design: snapd has a single hardcoded store,
   which the snap manager plugin already exposes via `list_available()` (visible through
   `srcmgr snap available`).  Nothing to add unless snap ever grows mirrors.
2. **Microsoft's apt repo** (`packages.microsoft.com`) resets connections on any probe
   from this machine, for `urllib` and `curl` alike (OpenSSL 3.5 `unexpected eof`).  It is
   reported as a failure; this is an environment/TLS-stack issue, not a probe bug.  If it
   matters, add a per-source `health.allow_missing: true` override or probe a different
   path.
3. `srcmgr apt choose` was exercised non-interactively (`--yes` picks the fastest); one
   human interactive run is still worth doing for the transcript.
4. Optional: CI recipe (GitHub Actions running `pytest`), a `srcmgr health` systemd timer
   example is already in `docs/operations.md`, and provider signing for the marketplace.

## 2. NEXT STEP (start here in a new session)

```bash
cd /home/alnilam/Downloads/compress/outdsh/sourceManager
.venv/bin/python -m pytest tests/ -q                       # 1. everything green?
SRCMGR_CONFIG_HOME=/tmp/sm-cfg SRCMGR_DATA_HOME=/tmp/sm-data SRCMGR_CACHE_HOME=/tmp/sm-cache \
  .venv/bin/python bin/srcmgr providers list               # 2. all providers 'ok'
SRCMGR_CONFIG_HOME=/tmp/sm-cfg SRCMGR_DATA_HOME=/tmp/sm-data SRCMGR_CACHE_HOME=/tmp/sm-cache \
  .venv/bin/python bin/srcmgr apt available --china | head # 3. mirrors listed
.venv/bin/python -m pytest tests/test_cli.py -q            # 4. CLI end-to-end
```

All of the above should be green (304 tests, 7 providers `ok`, 144 sources).  Then pick
the first item from *Known gaps* that matters to you.  Verify at the end that the real
system is untouched:

```bash
find /etc/apt /etc/docker -type f -exec md5sum {} + | sort | md5sum
```

---

## 3. Architecture in one screen

```
CLI (cli/main.py → cli/commands.py)
        │  thin handlers, one per command
        ▼
SourceManagerRuntime (core/runtime.py)          ← the only façade; a GUI would use this
   ├── PluginLoader        bundled + user plugins, trust model, install/scaffold
   ├── SourceRegistry      merges provider plugins into the catalogue
   ├── health.test_sources HTTP/TLS + native probes, scoring, cache
   ├── BackupStore         snapshot(paths) → id ; restore(id) ; record_paths(id, …)
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

Mutation pipeline (always this order): `guard → snapshot → plugin call → record created
files → history → (optional) refresh`, so `srcmgr rollback <manager>` can always undo the
last change and a failed snapshot aborts before anything is written.

## 4. State the tool keeps

| Path | Contents |
|---|---|
| `$SRCMGR_CONFIG_HOME` (`~/.config/source-manager`) | `config.yaml`, `plugins/` (user plugins + `.trusted` markers) |
| `$SRCMGR_DATA_HOME` (`~/.local/share/source-manager`) | `backups/<id>/{manifest.json,files/…}`, `history/operations.jsonl` |
| `$SRCMGR_CACHE_HOME` (`~/.cache/source-manager`) | `health.json` (last probe per source, TTL from config) |

Environment overrides: `SRCMGR_CONFIG_HOME`, `SRCMGR_DATA_HOME`, `SRCMGR_CACHE_HOME`,
`SRCMGR_PLUGIN_PATH`, `SRCMGR_<SECTION>__<KEY>` (e.g. `SRCMGR_HEALTH__TIMEOUT=4`), plus
per-plugin roots used by the tests (`SRCMGR_APT_ROOT`, `SRCMGR_DOCKER_ROOT`,
`SRCMGR_FLATPAK_FAKE_DIR`, `SRCMGR_SNAP_FAKE_DIR`, `SRCMGR_*_RC`, …).

## 5. Session log (newest first)

* **session 1 (final)** — registry finished and verified by its author (144 sources, all
  URLs curl-checked, dead mirrors removed: Ruby China's broken TLS, TUNA/USTC/163 docker
  accelerators, USTC/TUNA/NJU Go proxies, conda substitutions); fixed the derivative
  `${distro}` mapping and the placeholder/HEAD-probe bugs; `apt choose` exercised end to
  end; docs completed (`architecture.md`, `operations.md`, `LICENSE`); 304 tests.
* **session 1 (continued)** — integrated the source registry (7 providers, ≈144 entries);
  fixed 9 bugs (backup restore/id collisions, created-file rollback, provider extras,
  template expansion, `reachable` health classification, global-flag parsing, dry-run
  ordering, country filtering); added `tests/test_cli.py` (26) and `tests/test_core.py`
  (19) with a real-system fingerprint guard; verified `pip install -e .`; committed
  twice (`7f8e1ce`, `9273620`).
* **session 1** — project created from an empty directory: engine, CLI, 14 manager
  plugins, docs/hand-off files, plugin test-suites (206 tests).
