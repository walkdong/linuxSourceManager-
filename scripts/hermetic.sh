#!/bin/sh
# Run any command with every Source Manager root redirected to a sandbox.
#
# WHY THIS EXISTS: twice during development a manual verification script forgot to
# redirect $HOME and wrote into the developer's real home (~/.npmrc, ~/.config/...).
# The test suite has a guard for that; ad-hoc scripts did not.  Use this wrapper for
# every manual check:
#
#     scripts/hermetic.sh .venv/bin/python bin/srcmgr --cli managers
#     scripts/hermetic.sh .venv/bin/python bin/srcmgr          # the menu UI
#
# Nothing outside the sandbox directory is touched (the caller's HOME is replaced).
set -eu

SANDBOX="${SRCMGR_SANDBOX:-/tmp/srcmgr-sandbox}"
mkdir -p "$SANDBOX/home" "$SANDBOX/etc-apt"

# A fake apt root so even a root-level write stays inside the sandbox.
if [ ! -f "$SANDBOX/etc-apt/os-release" ]; then
    printf 'ID=ubuntu\nID_LIKE=debian\nVERSION_CODENAME=resolute\nVERSION_ID="26.04"\n' \
        > "$SANDBOX/etc-apt/os-release"
fi
mkdir -p "$SANDBOX/etc-apt/sources.list.d"

exec env \
    HOME="$SANDBOX/home" \
    XDG_CONFIG_HOME="$SANDBOX/home/.config" \
    XDG_DATA_HOME="$SANDBOX/home/.local/share" \
    XDG_CACHE_HOME="$SANDBOX/home/.cache" \
    SRCMGR_CONFIG_HOME="$SANDBOX/config" \
    SRCMGR_DATA_HOME="$SANDBOX/data" \
    SRCMGR_CACHE_HOME="$SANDBOX/cache" \
    SRCMGR_APT_ROOT="$SANDBOX/etc-apt" \
    SRCMGR_APT_OS_RELEASE="$SANDBOX/etc-apt/os-release" \
    SRCMGR_DOCKER_ROOT="$SANDBOX/etc-docker" \
    "$@"
