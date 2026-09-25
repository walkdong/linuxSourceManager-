# Source providers (Level-2 plugins)

*Which* sources exist is data, not code.  This document is the authoring guide and the
schema reference for **source providers** — the Level-2 half of Source Manager's plugin
market — plus the accuracy policy every bundled catalogue follows.

---

## 1. The two-level plugin market

```
Level 1  manager plugin      srcmgr/bundled/managers/<name>/{plugin.yaml,manager.py}
         "how do I operate this ecosystem?"      APT, npm, pip, Cargo, Docker, ...

Level 2  source provider     srcmgr/bundled/providers/<name>/{plugin.yaml,sources/*.yaml}
         "which sources exist for it?"           debian-official, china-mirrors, ...
```

A manager plugin never hardcodes a mirror URL; a provider never touches the filesystem
or the machine.  They meet in the **Source Registry**
(`srcmgr/core/registry.py`), which merges every loaded provider into one catalogue per
manager and de-duplicates it.  The CLI reads that registry for
`srcmgr <manager> available`, `srcmgr <manager> add <key>`, `providers search`, the
`--china` filter and the automatic-fallback candidate list.

Provider plugins are pure data unless they ship a `provider.py`; providers that only
contain YAML are always treated as trusted data (they execute nothing).

```
srcmgr/bundled/providers/china-mirrors/
├── plugin.yaml          manifest: identity + which managers are served
└── sources/
    ├── apt.yaml         one file per manager (or per manager group)
    ├── npm.yaml
    ├── pypi.yaml
    └── ...
```

Two rules shape the layout:

* **One `managers:` list per file.**  Every entry in a YAML file is materialised once
  per manager listed in that file (`managers:` in the file, falling back to `managers:`
  in `plugin.yaml`).  Entries cannot declare their own manager, so a mixed file is
  impossible — put `managers: [npm, yarn, pnpm]` in one file to serve three managers
  with one URL, and use separate files for different ecosystems.
* **One bad entry kills the whole provider.**  `DataProviderLoader` raises on the first
  invalid entry and the loader then reports the provider as `ERROR ...`, contributing
  nothing at all.  `tests/test_providers.py` exists to catch that before release.

---

## 2. Provider manifest (`plugin.yaml`)

| key | type | required | meaning |
|---|---|---|---|
| `name` | string, kebab-case | yes | unique plugin id, also the `provider` shown in listings |
| `display_name` | string | no | human title used as the group heading |
| `type` | `provider` | yes | must be `provider` |
| `version` | semver string | no | shown in group headings |
| `description` | string | no | one line printed under the group heading |
| `author`, `license`, `homepage` | string | no | provenance metadata |
| `managers` | list of manager names | **yes** | which managers this provider serves |
| `sources_dir` | string | no | directory holding `*.yaml`, default `sources` |
| `platforms` | list | no | restrict the plugin to e.g. `[debian, ubuntu]` |

```yaml
name: china-mirrors
display_name: China mirrors
type: provider
version: 0.1.0
description: Mainland-China public mirrors for apt, npm, pip, cargo, Go, ...
author: Source Manager contributors
license: MIT
homepage: https://mirrors.cernet.edu.cn/
managers: [apt, npm, yarn, pnpm, pip, cargo, go, docker, flatpak, git, maven, rubygems, conda]
sources_dir: sources
```

## 3. Source file schema

```yaml
managers: [apt]            # optional here: overrides the manifest's managers list
sources:                   # required, a list of entries
  - key: tuna
    ...
```

### 3.1 Entry keys

| key | type | required | meaning |
|---|---|---|---|
| `key` | string `[a-z0-9-]+` | **yes** | unique inside the provider *per manager*; what `srcmgr apt add tuna` accepts |
| `name` | string | yes in practice | short label for tables (`TUNA (Tsinghua)`) |
| `url` | string | **yes** unless `url_template` is set | the address written into the ecosystem's configuration |
| `url_template` | string | no | `url` variant with `${...}` placeholders, expanded when the source is *added* |
| `suite` / `channel` | string | no | APT suite, conda channel, Flatpak branch; may itself contain `${codename}` |
| `components` | list | no | APT components. **Omit it on cross-family entries** so apt fills in the family default (Debian `main contrib non-free non-free-firmware`, Ubuntu `main restricted universe multiverse`) |
| `types` | list | no | APT `deb` / `deb-src`; defaults to `deb` |
| `architectures` | list | no | restrict/complement APT architectures |
| `enabled` | bool | no | registry entries default to `false`; `srcmgr <mgr> add` flips them on |
| `kind` | `official` \| `mirror` \| `vendor` \| `private` \| `custom` | no | provenance, used by `--kind` filters and scoring (default `mirror`) |
| `scope` | `system` \| `user` \| `project` | no | where the configuration lives; `system` means "needs root" (default `user`) |
| `priority` | int 0–100 | no | editorial preference: **lower** score (= better) by 0.5 ms per point |
| `description` | string | yes in practice | shown in listings and search hits |
| `homepage` | string | no | documentation link for the source |
| `health` | mapping | yes in practice | how to prove the source is alive — see §3.3 |
| `extra` | mapping | no | manager-specific fields — see §5 |
| `health_url` | string | no | legacy shorthand for `health.url` |
| `baseurl` | string | no | accepted alias for `url` |
| `secret_fields` | list | no | field names the CLI must redact when printing |

Every *unrecognised* top-level key is folded into `Source.extra`, so `url_template:`,
`countries:` or `remote_name:` work both at top level and inside `extra:`.  Keys inside
`extra:` win over nothing — top level is merged on top of `extra:`.

### 3.2 A complete example

```yaml
managers: [apt]
sources:
  - key: tuna
    name: TUNA (Tsinghua)
    kind: mirror
    url_template: https://mirrors.tuna.tsinghua.edu.cn/${distro}/
    url: https://mirrors.tuna.tsinghua.edu.cn/ubuntu/
    types: [deb]
    scope: system
    priority: 20
    description: Tsinghua University TUNA mirror.
    homepage: https://mirrors.tuna.tsinghua.edu.cn/
    extra:
      countries: [cn]
    health:
      urls:
        - https://mirrors.tuna.tsinghua.edu.cn/debian/dists/stable/Release
        - https://mirrors.tuna.tsinghua.edu.cn/ubuntu/dists/noble/Release
```

### 3.3 `health`

`srcmgr <manager> test` probes these URLs; the source is healthy when **any** of them
answers.

| key | type | meaning |
|---|---|---|
| `url` | string | the single probe URL (a `Release` file, `config.json`, `/v2/`, package metadata, …) |
| `urls` | list | several probe URLs; a source passes when one answers (used for entries that serve both the Debian and the Ubuntu tree) |
| `paths` | list | paths appended to `health.base` (or to the source URL) when no `url`/`urls` is given |
| `base` | string | base used with `paths` |
| `include_base` | bool | also probe the bare base (default `true`) |
| `method` | `HEAD` \| `GET` | request method, default `HEAD` with an automatic `GET` retry |
| `headers` | mapping | extra request headers |
| `allow_missing` | bool | the endpoint is *expected* to be unreachable (internal/example hosts): it disables the fallback to the ecosystem's own native check, so an `example.invalid` entry is not accidentally vouched for by a successful `npm ping` |

A 401/403 is reported as a weak **`reachable`** pass (the host answered and wants
credentials), which is how registry endpoints such as Docker's `/v2/` are handled.  A
404 in a health URL is a broken entry.

---

## 4. Placeholders (`${...}`)

`url_template` exists so that one entry can serve every distribution family, instead of
shipping one entry per codename:

```yaml
url_template: https://mirrors.tuna.tsinghua.edu.cn/${distro}/
# on Debian  -> https://mirrors.tuna.tsinghua.edu.cn/debian/
# on Ubuntu  -> https://mirrors.tuna.tsinghua.edu.cn/ubuntu/
```

Expansion happens in `SourceManagerRuntime._expand_templates()` **at add time**, using
the values the manager plugin returns from `template_values()`:

| placeholder | value (apt) | example |
|---|---|---|
| `${codename}` | release codename | `trixie`, `noble`, `resolute` |
| `${distro}` | distribution id | `debian`, `ubuntu` |
| `${id}` | same as `${distro}` | `ubuntu` |
| `${id_like}` | `ID_LIKE` from `/etc/os-release` | `debian` |
| `${version_id}` | `VERSION_ID` | `26.04` |
| `${arch}` | native architecture as apt spells it | `amd64`, `arm64` |
| `${suite}` | suite alias for the codename | `trixie` |

Rules that follow from the implementation:

* **Only these placeholders.**  An unknown `${...}` makes the add fail with
  `unresolved placeholder(s) ...` instead of writing a broken URL.
* **The template replaces `url`.**  `url` is still required as the *concrete* fallback
  used by listings, URL matching and search before anything is expanded, so make it the
  most likely real path.  For mirrors that carry both trees, the bundled catalogues use
  the Ubuntu tree (`.../ubuntu/`), because the machines running this tool are far more
  often Ubuntu than Debian; `add` rewrites it from the template anyway.
* **`suite` may be templated too** (`${codename}-security`) — the apt plugin expands
  suites and components with the same table.
* **Health URLs must be concrete.**  `srcmgr <manager> test --all` probes the registry
  listing *without* expanding templates, so `${...}` inside `health.url`/`health.urls`
  would always fail.  Use a file that really exists (`dists/stable/Release` for Debian,
  a long-lived LTS codename such as `dists/noble/Release` for Ubuntu — Ubuntu has no
  `stable` suite alias; it was verified 404 on every mirror).
* **`extra["value"]`** is set to the expanded URL, which is what single-value managers
  (npm, pip, cargo, go, …) read.

---

## 5. Manager-specific `extra` fields

Only the fields a manager actually reads are listed here; check the manager's
`manager.py` before adding anything else — invented keys are silently ignored.

| manager | field | effect |
|---|---|---|
| apt | `signed_by` | path to the keyring for `Signed-By` (`"true"` derives `/usr/share/keyrings/<slug>.gpg`) |
| apt | `countries`, `slug`, `filename`, `format` | `countries` for the China filter; the rest are user-supplied at add time and should **not** be set by providers |
| docker | — | the entry `url` goes straight into `registry-mirrors`; there is no per-entry field |
| flatpak | `installation` | `system` (default) or `user` |
| flatpak | `remote_name` | remote name, defaults to the provider key |
| flatpak | `gpg`, `gpg_verify` | disable GPG verification (`--no-gpg-verify` / `--gpg-verify=false`) |
| flatpak | `priority` | `--prio=` value |
| git | `instead_of` | list of URL prefixes to rewrite (defaults to `https://github.com/`) |
| conda | `channel` | write this name instead of deriving it from the URL |
| conda | `priority` | `highest` prepends the channel |
| maven | `mirror_id`, `mirror_of` | `<id>` (defaults to the key) and `<mirrorOf>` (defaults to `*`) |
| pip | `key` | `index-url` (default) or `extra-index-url` |
| cargo | `table`, `source_name` | name of the `[source.<name>]` table (defaults to the key) |
| npm | `accompanying` | extra keys written next to `registry=` (e.g. `always-auth`) |
| all | `countries` | see §6.4 |

---

## 6. How the core uses the fields

### 6.1 `kind`

`official` sources get a **+25 ms** penalty in `score_result()`, so a nearby mirror wins
when latencies are close ("prefer the mirror when it is not slower"); `mirror` is the
normal case; `vendor` is for third-party repositories (PPAs, vendor apt repos),
`private` for corporate/self-hosted endpoints and `custom` for hand-added URLs.

### 6.2 `scope`

`system` marks configuration that lives in `/etc` (or a system-wide installation):
`apt`, `docker` and `flatpak` entries use it.  The runtime refuses to touch a
`system` source unless it is root (`general.require_root_for_system`), which is why
`srcmgr apt add tuna` asks for `sudo`.  Everything else (`npm`, `yarn`, `pnpm`, `pip`,
`cargo`, `go`, `git`, `maven`, `rubygems`, `conda`) is `user`.

### 6.3 `priority`

Optional 0–100 editor's choice: every point subtracts 0.5 ms from the score
(`score = latency + penalties - priority * 0.5`), i.e. priority 20 buys 10 ms.  The
bundled China catalogue uses it sparingly (TUNA/USTC 20, npmmirror/goproxy.cn 20, the
big clouds 10) so that a 5 ms difference in measured latency still decides.

### 6.4 `countries` and `--china`

`srcmgr <manager> available --china` keeps entries whose `extra.countries` contains
`cn` (or whose host matches a China hint such as `.cn`, `tuna.tsinghua`, `aliyun`,
`ustc.edu.cn`).  Every China entry in the bundled catalogue sets
`extra: {countries: [cn]}` explicitly, including the ones whose host gives nothing away
(`goproxy.io`, `rsproxy.cn` is fine but `goproxy.io` is not).

---

## 7. Accuracy policy

> A published mirror URL must have been verified with a real request.  Dead mirrors are
> the exact problem this project exists to solve, so an unverified entry is worse than a
> missing one.

The rules the bundled providers follow — and that a contributed provider is expected to
follow:

1. **Probe everything you publish** (`url`, `url_template` expansions, `health.url`,
   `health.urls`) with a real request, e.g.
   `curl -sS -o /dev/null -w '%{http_code}' -m 12 -I <url>`, falling back to
   `curl -r 0-0 <url>` when a server refuses HEAD.
2. **Ship only 200s** — plus documented 401/403 for endpoints that require
   authentication (a Docker registry's `/v2/` answers 401 with a bearer realm; that is a
   *live* registry).
3. **Verify function, not just liveness**, where the ecosystem has a protocol: a Go
   proxy is checked with a real module version list, a Git mirror with
   `info/refs?service=git-upload-pack`, a Docker accelerator with an anonymous
   `library/alpine:latest` manifest pull, a conda channel with `noarch/repodata.json`,
   RubyGems with `specs.4.8.gz`, cargo with `config.json`.
4. **Check freshness**, not only the status code: a mirror that still answers for
   `dists/stable/Release` while its `Date:` header is months old is a dead mirror with a
   pulse.  (All shipped APT mirrors were checked against the upstream `Release` dates.)
5. **Delete or fix anything that 404s**, and write down *why* a well-known mirror is
   missing so the next contributor does not re-add it — every exclusions list in the
   bundled YAML files is there for that reason (see §9).
6. **Never publish an account-specific or private URL** (Aliyun's
   `<id>.mirror.aliyuncs.com` accelerator, GitHub Packages) as if it were public.

Some `url` values are configuration values rather than fetchable documents — a GOPROXY
prefix (`https://mirrors.aliyun.com/goproxy/`), an npm registry root
(`https://mirrors.cloud.tencent.com/npm/`), a PEP 503 index root, a conda channel URL or
a cargo `sparse+https://...` index.  Those paths legitimately answer 404 to a plain
`GET /`; their *health* URL is the functional probe that must return 200.

---

## 8. Working with providers

```bash
# what is installed, and how many sources each provider contributes
.venv/bin/python bin/srcmgr providers list

# one provider in detail (including every key and URL)
.venv/bin/python bin/srcmgr providers info china-mirrors

# everything a manager can offer, grouped by provider
.venv/bin/python bin/srcmgr apt list --available
.venv/bin/python bin/srcmgr apt available --china          # only China-tagged entries
.venv/bin/python bin/srcmgr apt available --kind official  # only upstream archives

# search across providers/managers
.venv/bin/python bin/srcmgr providers search nju
.venv/bin/python bin/srcmgr providers search --china --manager cargo

# health-check every registry entry of one ecosystem (no root, no changes)
.venv/bin/python bin/srcmgr apt test --all
.venv/bin/python bin/srcmgr conda test --all --json-out /tmp/conda-health.json

# turn a provider off without uninstalling it
.venv/bin/python bin/srcmgr providers disable corporate-template
```

Writing your own provider:

```bash
.venv/bin/python bin/srcmgr plugins new acme-internal --type provider --managers apt,npm,pip
# edit acme-internal/plugin.yaml + acme-internal/sources/*.yaml
.venv/bin/python bin/srcmgr plugins install ./acme-internal --trust
.venv/bin/python bin/srcmgr providers list
```

Run the schema and template regression suite before publishing:

```bash
.venv/bin/python -m pytest tests/test_providers.py -q -o addopts=""
```

`tests/test_providers.py` loads every provider through `PluginLoader`, checks the schema
invariants (key/url/kind/health, unique keys, known placeholders, concrete health URLs,
scope per ecosystem, `.invalid` hosts marked private + `allow_missing`) and exercises
`_expand_templates()` with a fake `template_values()`.  It never touches the network.

---

## 9. Shipped providers

| provider | sources | apt | npm | yarn | pnpm | pip | cargo | go | docker | flatpak | git | maven | rubygems | conda |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `china-mirrors` | 88 | 28 | 3 | 3 | 3 | 8 | 7 | 4 | 5 | 1 | 4 | 3 | 3 | 16 |
| `cloud-vendor` | 11 | – | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | – | 1 | 1 | 1 |
| `corporate-template` | 8 | 1 | 1 | 1 | 1 | 1 | 1 | – | 1 | – | – | – | – | 1 |
| `debian-official` | 4 | 4 | – | – | – | – | – | – | – | – | – | – | – | – |
| `europe-mirrors` | 15 | 14 | – | – | – | 1 | – | – | – | – | – | – | – | – |
| `selfhosted` | 10 | 10 | – | – | – | – | – | – | – | – | – | – | – | – |
| `ubuntu-official` | 8 | 8 | – | – | – | – | – | – | – | – | – | – | – | – |
| **total** | **144** | 65 | 5 | 5 | 5 | 11 | 9 | 5 | 7 | 2 | 4 | 4 | 4 | 18 |

What each one is for:

* **`debian-official` / `ubuntu-official`** — the stock upstream archives, split by
  distribution family: `deb.debian.org` (base, updates, backports), the separate
  `security.debian.org/debian-security` archive, and for Ubuntu
  `archive.ubuntu.com`, `security.ubuntu.com`, Canonical's China CDN
  (`cn.archive.ubuntu.com`) and `ports.ubuntu.com` for non-x86.  `kind: official`.
* **`china-mirrors`** — the flagship China catalogue: apt on 14 mirrors (each with a
  matching `*-debian-security` entry), npm/yarn/pnpm, pip, cargo, Go, Docker
  accelerators, Flatpak, Git proxies, Maven, RubyGems and conda.
* **`europe-mirrors`** — large non-China mirror sites (LeaseWeb, FAU, kernel.org,
  Rackspace, NetCologne, dotsrc, Funet, xTom) plus the one verified non-China PyPI
  index; `deb.debian.org` is *not* repeated here because `debian-official` already
  ships it (avoiding the same URL twice in `apt available`).
* **`cloud-vendor`** — the upstream of every ecosystem (npmjs, PyPI, crates.io,
  proxy.golang.org, Maven Central, RubyGems, conda-forge, Flathub, Docker Hub);
  `kind: official`, the fallbacks the mirrors are compared against.
* **`corporate-template`** — documentation-as-data: an example internal Nexus /
  Artifactory / Verdaccio / devpi / Harbor setup.  Every host is under the reserved
  `.invalid` TLD, `kind: private` and `health.allow_missing: true`, so a health run
  never mistakes the example for a broken public mirror.  Disable it with
  `srcmgr providers disable corporate-template` if the FAIL lines bother you.
* **`selfhosted`** — community and university mirrors (OCF Berkeley, Princeton, MIT,
  Michigan Tech, AARNet) for Debian/Ubuntu, including non-x86 ports.

### Verification status

The catalogues above were verified with real requests at authoring time:

* Every `health.url`/`health.urls` (159 probes) and every `${distro}` expansion
  (25 × 2 trees) answers **200**, except the Docker registries that answer **401** with a
  bearer realm (the registry protocol's "authenticate first", reported as a weak
  `reachable` pass) and the `.invalid` example hosts.
* `srcmgr <manager> test --all` over all 13 ecosystems reports **136/136 healthy**
  (132 `ok`, 4 documented 401 `reachable`) once the example provider is disabled with
  `srcmgr providers disable corporate-template`.  With it enabled the only failures are
  its 8 intentionally unresolvable entries.
* APT mirrors were additionally checked for freshness (upstream `Release` `Date:`
  headers) and for every suite an entry can produce (`trixie`, `trixie-updates`,
  `trixie-backports`, `trixie-security`, `noble`, `noble-updates`, `noble-backports`,
  `noble-security`, `resolute`).
* Some `url` values are configuration values rather than documents and answer 404/403
  to a plain fetch — GOPROXY prefixes (`mirrors.aliyun.com/goproxy/`,
  `mirrors.cloud.tencent.com/go/`), npm registry roots, PEP 503 index roots, conda
  channel roots, cargo `sparse+…` values (a scheme curl cannot speak at all) and the Git
  rewrite bases.  Their functional probes are the health URLs, and the bundled Git
  entries therefore use `health: {method: GET, url: …info/refs?service=git-upload-pack}`
  so the check performs a real smart-HTTP handshake instead of a HEAD that proxies
  answer with 405.
* A handful of hosts (dotsrc, ghproxy, Huawei's directory indexes) answer 403/429 to
  rapid-fire tooling; re-checking them individually returns 200.  When contributing,
  space out bulk probes rather than trusting a single rate-limited run.

### Verified exclusions

These were tested and deliberately left out; re-adding them without new evidence would
regress the catalogue:

| candidate | verified result |
|---|---|
| `gems.ruby-china.com` | TLS certificate is a Tencent CDN wildcard (`CN=*.cdn.myqcloud.com`) that does not cover the host name — every HTTPS request fails verification; plain HTTP answers 418 |
| `docker.mirrors.tuna.tsinghua.edu.cn`, `docker.mirrors.ustc.edu.cn`, `hub-mirror.c.163.com` | connection/TLS failure (accelerators retired) |
| `docker.1panel.live`, `docker.nju.edu.cn` | 403 from the edge |
| `dockerproxy.net` | `/v2/` answers 200 but a manifest pull returns 404 |
| `mirror.ccs.tencentyun.com` | only resolves inside Tencent Cloud VPCs |
| `*.mirror.aliyuncs.com` (Aliyun Docker accelerator) | account-specific host, cannot be verified or shared |
| `mirrors.{ustc,tuna,nju,bfsu}.edu.cn/goproxy/…` | 404 — those mirrors no longer publish a Go module proxy |
| `mirrors.aliyun.com/anaconda/…` | 404 — no conda mirror |
| `mirrors.{ustc,bfsu}.edu.cn/anaconda/…` | 302 redirects to NJU/TUNA, so not an independent mirror |
| `mirrors.aliyun.com/crates.io-index` + `mirrors.cloud.tencent.com/crates.io-index` | the Tencent path 404s; the Aliyun path is shipped |
| TUNA/SJTU/BFSU/NJU `flathub` | 404; SJTU only republishes the upstream `.flatpakrepo` whose `Url=` still points at `dl.flathub.org` |
| FAU/netCologne/dotsrc/OCF/Princeton/AARNet PyPI | `/pypi/web/simple` returns 404 today; only xTom's index verified |
| `deb.debian.org/debian` `<codename>-security` | 404 — Debian security lives on `security.debian.org/debian-security`, which is why it is a separate entry |
| `mirrors.163.com` as a CLI key | the key is `netease`: a purely numeric selector such as `163` is parsed as a list index by `srcmgr apt add 163` |
