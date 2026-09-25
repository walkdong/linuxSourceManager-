# PROGRESS.md — Source Manager (`srcmgr`)

> **This is the hand-off document.** A new session should read, in order:
> `CLAUDE.md` → `PROGRESS.md` → `git log` → the code named below, then continue from
> the **NEXT STEP** section. Do not re-plan the project from scratch.

*Last updated: 2026-09-25 (session 3 — M5 beginner menu UI + hard locks, 368 tests green)*
*Version: 0.1.0 — GUI deliberately not started (CLI + core only).*

---

## 1. Where the project is

**Phase: M5 complete — the beginner menu UI replaced the typed REPL as the default.**
M3 (engine/plugins/registry) and M4 (frontend split) are frozen; M5 is the
"my grandmother / a primary-school kid can use it" layer.

Status: **368 tests passing**, 14 manager plugins, 7 source providers (**144 registry
entries**), four frontends, `pip install` verified from a wheel in a clean venv, nothing
under `/etc` ever written outside the sandboxed test roots (fingerprinted before/after).

### M5 — 面向零基础用户的中文菜单界面

| Invocation | Frontend | Notes |
|---|---|---|
| `srcmgr` | **菜单界面** (Textual) | 全屏、鼠标可点、不需要输入任何命令 |
| `srcmgr --tui` | 菜单界面 | 显式 |
| `srcmgr --shell` | 打字式 REPL | M4 的界面，留给会用命令行的人 / 排错 |
| `srcmgr --cli <cmd>` | 一次性命令 | 脚本唯一入口（`--cli` 仍是强制） |
| `srcmgr --gui` | GUI | 保留位（exit 3） |
| `srcmgr --install-shortcut` | 桌面图标 | 双击即可从应用菜单打开，不用碰终端 |

Key design decisions (do not undo these casually):

* **锁是硬锁**（用户明确要求）：`l` 键或点击即可锁定一个生态；锁定后 *任何* 修改都会被
  拒绝 —— 手动切换、一键换最快、自动故障切换全部跳过；`rollback`/`restore` 仍允许（那是
  逃生通道）。锁存在 `$SRCMGR_DATA_HOME/locks.json`，不在 config 里（它是状态不是配置）。
  界面上必须**一眼看出可点、一眼看出已锁**：常驻 `🔓 点这里锁定` / `🔒 已锁定（点击解锁）`，
  锁定行变暗，标题栏显示锁定数量，点其它镜像时弹"先解锁"对话框并提供"解锁并切换"一键出路。
* **不动核心契约**：`runtime.add/enable/...` 仍然抛 `PermissionDenied`（CLI 退出码 4）；
  "需要管理员密码"这个判断在 `srcmgr/tui/data.py` 里转成 `Outcome(data={"permission": True})`，
  由界面去要密码并重试一次。每个动作**最多问一次密码**，再被拒绝就用人话解释，绝不循环弹窗。
* **密码安全**：`srcmgr/tui/sudo.py` 用临时 `SUDO_ASKPASS` 助手，密码写进 0600 临时文件、
  经 `SRCMGR_ASKPASS_FILE` 传入，用完立即删除；从不进 argv、不落盘长期保存。
* **"当前源"要跟着用户意图走**：APT 可以同时启用多个源，插件自己的优先级（官方 archive 优先）
  看不到"用户刚换到清华"这个意图，所以 `tui/data.py:last_switched_url()` 会查历史记录，
  界面显示以用户最后一次成功切换为准。
* **短名单而不是全量**：给初学者只测/只显示最有用的 12 个镜像（先用相关性排序：正在用的 →
  mirror → official → 优先级 → 国家偏好），按 `a` 才显示全部。之前测 65 个 apt 源要一分钟。
* 文案全部走 `tui/i18n.py`（中文优先，`SRCMGR_LANG=en` 可切英文，缺词条回退中文），
  TUI 渲染的字符串必须是纯文本（不要 prompt_toolkit/Textual 标记）。
* 报错必须"人话+下一步"：`friendly_error()` 把 sudo/timeout/证书/连接失败等翻译成中文，
  并给出该点哪个按钮。

### M4 — frontend model (this milestone)

| Invocation | Frontend | Notes |
|---|---|---|
| `srcmgr` | **TUI** | prompt_toolkit REPL; once a GUI exists, a bare invocation should prefer it |
| `srcmgr --tui` | TUI | explicit |
| `srcmgr --cli <cmd...>` | CLI | **mandatory** for one-shot commands |
| `srcmgr --gui` | GUI | reserved: exits 3 with "not implemented yet" |
| `srcmgr apt list` | *refused* | exits 2 with `srcmgr --cli apt list` in the hint |

* `srcmgr/tui/app.py` — REPL: banner, `:help`/`:clear`/`:refresh`/`:stats`/`:quit`,
  Ctrl-C abandons a line, Ctrl-D leaves, confirmations asked in the TUI prompt.
* `srcmgr/tui/completion.py` — context-aware Tab completion built from the CLI's own
  `COMMANDS`/`MANAGER_ACTIONS` plus live registry/source data.
* `srcmgr/core/interaction.py` — context-var seam (`ask_hook`, `output_hook`,
  `frontend`) so **one** command implementation serves every frontend; a GUI plugs in
  the same way the TUI does.
* TUI commands are the CLI's words (`apt add tuna`, `health --watch 30`), so nothing has
  to be learned twice; `--json`/`--porcelain` are neutralised inside the transcript.

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
| 11 | Tests: 368 (plugins 206 + CLI 26 + core 21 + runtime safety 13 + providers 22 + apt family 16 + shell 26 + locks 14 + menu 24) | ✅ all green | `tests/` |
| 12 | Docs: `CLAUDE.md`, `PROGRESS.md`, `README.md`, `docs/{source-providers,architecture,operations,tui}.md`, `LICENSE` | ✅ | repo root, `docs/` |
| 14 | **M4 frontends**: `--cli` gate, `--gui` reservation, interaction seam, typed REPL | ✅ | `core/interaction.py`, `tui/shell.py` |
| 15 | **M5 菜单界面**: Textual 首页/镜像页/引导/对话框/锁/鼠标/提权，中文 i18n，桌面图标 | ✅ | `tui/app.py`, `tui/data.py`, `tui/i18n.py`, `tui/sudo.py`, `core/locks.py`, `core/shortcuts.py` |
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

### ⚠️ 一次真实事故（已修复，并加了防线）

手动调试脚本忘了设置 `HOME`，于是 `srcmgr --cli -y npm add …` 写进了**真机**的
`~/.npmrc`（把 npm registry 改成 npmmirror）。测试套件本身没碰真 HOME（已用指纹对比证明），
是调试脚本的问题。处理：删掉 `~/.npmrc` 恢复默认，并在 `tests/conftest.py` 加了 autouse
守卫 `_refuse_real_home`——只要 `SRCMGR_CONFIG_HOME/DATA_HOME/CACHE_HOME` 解析到真 HOME
里面，测试立刻失败（已验证该守卫会真的拦住）。

**结论（写进规则）**：任何手动验证脚本都必须先设 `HOME`/`SRCMGR_*` 到临时目录，
绝不能依赖"反正只是读"。

### M5 bugs found and fixed (regression-tested)

20. APT 换源后界面仍显示"当前：官方源"——插件自身的优先级看不到用户意图；现在
    界面查历史记录（`last_switched_url`）再决定显示哪个。
21. 普通用户点"切换"直接弹权限错误（死路），而不是先问密码；现在会要密码并重试一次，
    且**每个动作最多问一次**（第二次被拒绝就解释，不再弹窗）。
22. `Config.set()` 只写文件、不更新内存视图，导致 `srcmgr config set` / 引导标记看起来没生效。
23. 镜像列表不排序就把 65 个 apt 源全列出来、还全部去测速（一分钟）；现在先按相关性选出
    12 个再测。
24. `App.notify()` 的第二个位置参数是 timeout 不是 title，通知标题全错。
25. 二级页面按 `q` 退不出去（绑定只在首页）；现在 `q` 在任何页面都能退出，弹窗里用 Esc。
26. pyte 之外还发现：`runtime.add()` 我一度改成"返回失败而不是抛异常"，破坏了 CLI 退出码 4
    的契约 —— 已回滚，改为在界面层捕获。

### M4 bugs found and fixed (regression-tested)

14. A bare `srcmgr` in a pipe would have launched the UI: `run()` now refuses without a
    terminal and points at `--cli`.
15. **`snap get` blocked forever** when the process had no controlling terminal, and
    completion queried it during start-up — the TUI appeared to hang on a blank screen.
    Fix: every plugin shell-out is capped (`SRCMGR_PLUGIN_TIMEOUT`, default 15 s) and
    `stdin` is `DEVNULL`.
16. Completion refresh of 14 managers ran on the main thread, delaying the first prompt;
    it now runs on a worker thread and `Session` publishes state atomically.
17. `banner_text()` called `status()` per manager (which shells out) — the banner is now
    pure formatting and takes ~0.6 s to first paint.
18. The TUI's `:help` printed literal `[banner]` markup (prompt_toolkit `HTML` tags in a
    plain `print`).
19. `KeyValueFile.set_many` used an undefined `original` when rewriting a line, and the
    TUI's completion helper leaked a comprehension variable (both silent data bugs found
    by pyflakes / tests).

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
4. 菜单界面打磨待办：底部实时健康状态条、把"修改记录"做成可展开详情、镜像页显示
   "上次测速时间"、以及把 `.desktop` 图标做成自带 SVG 图标文件。
5. **GUI**: `--gui` is reserved but unimplemented.  When it is built it must consume
   `srcmgr.core` only (never `srcmgr.cli`), install its hooks through
   `srcmgr.core.interaction.interactive(...)`, and take over the bare-`srcmgr` entry
   point from the TUI (that is the intended behaviour).
6. Optional: CI recipe (GitHub Actions running `pytest`), provider signing for the
   marketplace.

## 2. NEXT STEP (start here in a new session)

```bash
cd /home/alnilam/Downloads/compress/outdsh/sourceManager
.venv/bin/python -m pytest tests/ -q                       # 1. everything green?
.venv/bin/python bin/srcmgr --cli providers list           # 2. all providers 'ok'
.venv/bin/python bin/srcmgr --cli lock                    # 3. no locks leaked by a test run
.venv/bin/python bin/srcmgr                                # 4. 菜单界面（需要真终端）
.venv/bin/python bin/srcmgr --tui --help                   # 5. 四种前端
.venv/bin/python -m pyflakes srcmgr/ tests/                # 6. lint
```

界面的手动验收（pty 脚本在 `/tmp/tui_m5b.py` 有参考）：启动→欢迎→首页→`l` 锁定→回车进镜像页→
Esc→`q` 退出。锁定行的 🔒、`一键换最快` 跳过锁定项、普通用户提权流程都要肉眼确认一次。

Note the new invocation rule: **one-shot commands need `--cli`**.  `managers`, `sources`,
`test`, `health`, `fallback`, `history`, `backup`, `rollback`, `restore`, `plugins`,
`providers`, `config`, `doctor` are unchanged apart from that prefix.  In the TUI, type
them without `--cli` (and without `srcmgr`).

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

* **session 3** — M5：把默认界面换成 **Textual 中文全屏菜单**（首页状态列表 + 每生态镜像
  选择 + 一键体检 + 一键换最快 + 硬锁 + 备份还原 + 首次引导 + 鼠标 + 中文报错引导 + 提权），
  原 REPL 移到 `--shell`，新增 `--install-shortcut` 桌面图标；新增 `core/locks.py` 与
  `restore_backup()`；修 7 个 bug（apt 当前源、提权死路、Config 内存不同步、镜像不排序、
  notify 参数、二级页 q 不退出、核心契约被我误改）；测试 330 → **368**。
* **session 2** — M4: three-frontend entry model.  Bare `srcmgr` opens a prompt_toolkit
  TUI (banner, Tab completion, history, `:` meta commands, confirmations inside the UI),
  `--cli` became mandatory for one-shot commands (old shorthand refused with a hint),
  `--gui` reserved with a clear "not implemented" exit 3.  Added
  `srcmgr/core/interaction.py` as the frontend seam and `ManagerPlugin.registry` wiring.
  Fixed six bugs (offline/pipe guard, `snap get` blocking start-up, `KeyValueFile`
  undefined variable, completion variable leak, banner shelling out, help markup).
  Added `SRCMGR_OFFLINE` (+ `SRCMGR_PLUGIN_TIMEOUT`) which kept the suite hermetic and
  cut it from 90 s back to 52 s.  `docs/tui.md` written; 330 tests green; pyflakes clean.
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
