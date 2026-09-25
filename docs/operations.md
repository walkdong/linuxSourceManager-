# Operations cookbook

Practical recipes for running Source Manager on a real machine.

## 1. Find out why `apt update` started failing

```bash
srcmgr apt list              # which sources exist and which are enabled
srcmgr apt test --all        # latency, HTTP status, TLS for every candidate mirror
srcmgr apt current           # what apt is actually using right now
```

`srcmgr apt test` prints one row per source; `ok` means real content was served,
`ok*` means the host answered without content at that path (normal for a few registry
roots), and a failure row carries the HTTP status or the network error.

## 2. Switch to the fastest mirror (with a safety net)

```bash
srcmgr apt test --all            # look at the table first
srcmgr apt add tuna              # add it (snapshot is taken automatically)
srcmgr apt enable tuna           # make it active
sudo srcmgr apt refresh          # apt-get update, as root
srcmgr apt rollback              # not happy? undo in one step
```

Everything is snapshotted before it changes; `srcmgr history` shows the record and
`srcmgr backup list` the snapshots.

## 3. Same, but let Source Manager measure for you

```bash
srcmgr apt choose                # probes candidates, ranks them, asks which to enable
srcmgr docker choose --kind mirror
```

The ranking is latency plus penalties (plain HTTP, official archives, declared
priority).  `--json` on any command gives the same data to a script.

## 4. Opt-in automatic failover

```bash
srcmgr fallback --enable apt          # persist the setting for apt only
srcmgr fallback                       # show the plan for every manager
srcmgr fallback apt --apply           # switch now if the current source is failing
srcmgr fallback --disable             # back to "never touch my sources"
```

The plan is computed from live probes: the current source must actually have failed,
and the candidate must answer *and* be a registry mirror.  Nothing ever changes without
either `--enable` plus `--apply`, so a transient network blip cannot silently rewrite
your sources.

## 5. Monitor continuously with a systemd timer

`srcmgr health` exits non-zero when any source is unhealthy, which makes it a natural
timer unit.  Create `/etc/systemd/system/srcmgr-health.service`:

```ini
[Unit]
Description=Source Manager mirror health check

[Service]
Type=oneshot
Environment=SRCMGR_DATA_HOME=/var/lib/source-manager
ExecStart=/usr/local/bin/srcmgr --json health
```

and `/etc/systemd/system/srcmgr-health.timer`:

```ini
[Unit]
Description=Run the Source Manager health check every 6 hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now srcmgr-health.timer
journalctl -u srcmgr-health.service -n 50   # last results as JSON
```

For an interactive dashboard instead, run `srcmgr health --watch 60`.

## 6. Keep a machine's whole source setup in version control

```bash
srcmgr --json sources > sources.json          # every ecosystem, installed + catalogue
srcmgr backup --list
srcmgr history --export ~/srcmgr-history.jsonl
```

`srcmgr --porcelain manager list` prints tab-separated rows for diffing.  Secrets
(registry tokens) are redacted in every export and listing.

## 7. Add an internal / corporate registry

```bash
# apt (Nexus/Artifactory proxy)
sudo srcmgr apt add --url https://nexus.corp.example/repository/apt-proxy/ \
     --suite "$(lsb_release -cs)" --components main --name "Corp APT"

# npm
srcmgr npm add --url https://nexus.corp.example/repository/npm-group/

# pip
srcmgr pip add --url https://nexus.corp.example/repository/pypi/simple/
```

To share those with a team, publish them as a provider plugin
(`srcmgr plugins new corp-mirrors --type provider --managers apt,npm,pip`) — see
[`source-providers.md`](source-providers.md).  Mark them `kind: private` so they are
never mistaken for public mirrors or used by automatic failover.

## 8. Recover a broken machine

If an edit left an ecosystem unusable, `apt` itself is usually the only thing you need:

```bash
srcmgr history --manager apt      # find the change
srcmgr backup show <snapshot-id>  # inspect exactly what will be restored
sudo srcmgr restore <snapshot-id>
```

A snapshot stores the file content, its unix mode and whether it existed at all, so a
restore also removes drop-in files that a bad change created.

## 9. Run it without touching the real system (sandbox)

Every root can be redirected, which is how the test suite stays hermetic:

```bash
export SRCMGR_CONFIG_HOME=/tmp/sm/cfg SRCMGR_DATA_HOME=/tmp/sm/data SRCMGR_CACHE_HOME=/tmp/sm/cache
export SRCMGR_APT_ROOT=/tmp/sm/etc-apt SRCMGR_DOCKER_ROOT=/tmp/sm/etc-docker
export HOME=/tmp/sm/home
srcmgr apt list
```

Per-plugin roots: `SRCMGR_APT_ROOT`, `SRCMGR_APT_OS_RELEASE`, `SRCMGR_DOCKER_ROOT`,
`SRCMGR_FLATPAK_FAKE_DIR`, `SRCMGR_SNAP_FAKE_DIR`, `SRCMGR_NPMRC`, `SRCMGR_YARNRC`,
`SRCMGR_YARNRC_YML`, `SRCMGR_PNPM_RC`, `SRCMGR_PIP_CONF`, `SRCMGR_CARGO_CONFIG`,
`SRCMGR_GO_ENV_FILE`, `SRCMGR_GITCONFIG`, `SRCMGR_MAVEN_SETTINGS`, `SRCMGR_GEMRC`,
`SRCMGR_CONDA_RC`.

## 10. Troubleshooting

| Symptom | First thing to run | Likely cause |
|---|---|---|
| a plugin does not appear | `srcmgr plugins list` | untrusted user plugin, wrong platform, or an invalid `plugin.yaml` |
| everything reports "not present" | `srcmgr doctor` | the ecosystem's CLI is not installed (`requires:` unmet) |
| `permission denied` on apt/docker | `srcmgr doctor` | system scope needs root: re-run with `sudo` |
| a mirror shows `ok*` | `srcmgr <manager> test <name>` | the host answers but serves nothing at the probe path; the provider can declare `health.url`/`health.paths` |
| `srcmgr` ignores my config | `srcmgr config get` | an `SRCMGR_*` environment variable overrides the file (by design) |
| plugin changes do not load | `srcmgr plugins repo` | the plugin is not in a scanned directory, or was installed untrusted |
