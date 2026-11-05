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

## 安装（三种方式，选一个）

**① 一行命令（推荐，任何发行版）**

```sh
curl -fsSL https://raw.githubusercontent.com/source-manager/source-manager/main/scripts/install.sh | sh
```

装完从**应用菜单**点"源管理器"即可（会自动创建图标）。它只往 `~/.local/bin` 和
`~/.local/share/source-manager` 写东西，不动系统 Python、不装到 `/usr`。

**② 单文件可执行（不需要 Python）**

从 [Releases](https://github.com/source-manager/source-manager/releases) 下载
`srcmgr-linux-x86_64`：

```sh
chmod +x srcmgr-linux-x86_64 && ./srcmgr-linux-x86_64
```

首次运行会把自己装到 `~/.local/bin` 并创建菜单图标（支持 Ubuntu 22.04+/Debian 12+）。

**③ Flatpak**（软件中心；受沙箱限制，只能改用户级源，系统源为只读展示）

详细的安装/校验/卸载/排查见 [`docs/安装与发布.md`](docs/安装与发布.md)。

## 从源码运行

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

## Ways in

| Command | What you get |
|---|---|
| `srcmgr` | **全屏中文菜单界面** — 只用方向键和鼠标，不需要输入任何命令（给零基础用户） |
| `srcmgr --shell` | 打字式交互界面（会用命令行的人用） |
| `srcmgr --cli <command>` | 一次性命令，脚本用 |
| `srcmgr --install-shortcut` | 在应用菜单创建"源管理器"图标，双击即可打开 |
| `srcmgr --gui` | 图形界面，保留位（尚未实现） |

```bash
srcmgr                 # 打开菜单界面（第一次会有一段欢迎说明）
srcmgr --install-shortcut   # 可选：创建桌面图标，以后双击打开
srcmgr --cli managers  # 一次性命令
srcmgr --cli --json sources --available     # 机器可读
```

菜单界面长这样（真实终端输出，中文；颜色在终端里显示，这里只有文字）：

```
 源管理器  把软件的下载源换成更快、更稳的镜像              语言  中 EN
 管理员权限：需要时询问

 你想管理哪个软件的下载源？
 ↑↓ 选择   Enter 进入   鼠标可点击   L 锁定/解锁   Q 退出
 ┌──────────────────────────────────────────────────────────────────┐
 │   软件                 现在用的是         状态               锁定    │
 │ ● APT（系统软件）      清华 TUNA       ● 正常 · 108ms      ▢ 锁定   │
 │ ● Flatpak（应用商店）  Flathub (USTC)  ● 2 个备用源失效      ▢ 锁定   │
 │                                        · 不影响使用                 │
 │ ● Docker（容器镜像）   docker.1ms.run  ● 正在用的源打不开   ▢ 锁定   │
 │ ○ npm（Node.js）       npmmirror       ○ 未检测            ▢ 锁定   │
 │ ▣ Python (pip)         清华 TUNA       ● 正常 · 174ms      ▣ 已锁定 │
 └──────────────────────────────────────────────────────────────────┘
  测速   换到最快   撤销   备份   记录   权限   退出
```

设计上刻意克制：只有**一个强调色**（默认青，可换琥珀/靛蓝），状态用
**四种低饱和颜色 + 文字**表达，动作是纯文字而不是大色块按钮，默认深色、可切浅色。
右上角 `语言 中 EN` 点一下就切换界面语言（会记住）。

进去之后：选择某个软件 → 看到按网速排好序的镜像列表 → 回车即切换（切换前自动备份，
随时可以"撤销"）。觉得某个软件已经调好了，就点它的 🔓 锁上：**锁定后任何自动操作
（包括"一键换到最快源"）都不会再碰它**，想换的话界面会提示先解锁，并给一个"解锁并切换"
的按钮。系统软件（APT/Docker）需要管理员密码时，界面会像 Windows 的 UAC 一样弹框询问，
只问一次。

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
> means "open the menu UI".

## How it is built

```
menu UI (tui/app.py)  ┐
shell   (tui/shell.py)┼→  SourceManagerRuntime (srcmgr/core/runtime.py)
CLI     (srcmgr/cli)  ┘
                       ├── PluginLoader    bundled + user plugins, trust model
                       ├── SourceRegistry  merges source providers into a catalogue
                       ├── health          HTTP/TLS/native probes, scoring, cache
                       ├── BackupStore     snapshot / restore
                       ├── HistoryStore    append-only log / rollback
                       └── Config          defaults < config.yaml < per-manager < env
                     →  ManagerPlugin implementations (srcmgr/bundled/managers/*)

Every frontend calls the same command handlers, and both route user questions through
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

Alpha.  The engine, the Chinese menu UI, the shell, the CLI, the
health/backup/history/rollback pipeline, 14 ecosystem manager plugins and 7 source
providers (144 verified sources) are implemented and tested (**368 tests**).  A GUI is
planned and already reserved at `srcmgr --gui`; it will consume `srcmgr.core` like the
other frontends do.  See `PROGRESS.md` for the current milestone and what is next.

## License

GPL-3.0 — 见 [`LICENSE`](LICENSE)。

