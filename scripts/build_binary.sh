#!/usr/bin/env bash
# Build the self-contained single-file binaries.
#
# WHY A CONTAINER: the binary links against the *build* host's glibc.  Building on a modern
# distro (glibc 2.39+) produces a file that refuses to start on older releases.  We build
# inside Ubuntu 22.04 (glibc 2.35, Python 3.10 - the oldest combination this project
# supports), so the result runs on Ubuntu 22.04/24.04/26.04, Debian 12/13 and derivatives.
# Ubuntu 20.04 is NOT a target: it ships Python 3.8 and this project requires 3.10+.
#
#   scripts/build_binary.sh                 # both flavours, containerised (default)
#   scripts/build_binary.sh --native        # build on this host (fast, NOT portable)
#   scripts/build_binary.sh --flavor cli    # only the CLI flavour
#   scripts/build_binary.sh --test          # also smoke-test the result in a bare container
#
# Output: dist/srcmgr-linux-x86_64  and  dist/srcmgr-cli-linux-x86_64  (+ .sha256)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

BASE_IMAGE="${SRCMGR_BUILD_IMAGE:-ubuntu:22.04}"
FLAVORS="menu cli"
MODE="container"
DO_TEST=0
OUT_DIR="${SRCMGR_DIST_DIR:-$REPO_ROOT/dist}"

while [ $# -gt 0 ]; do
    case "$1" in
        --native) MODE="native" ;;
        --container) MODE="container" ;;
        --flavor) FLAVORS="$2"; shift ;;
        --test) DO_TEST=1 ;;
        --image) BASE_IMAGE="$2"; shift ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

command -v docker >/dev/null 2>&1 || { echo "docker is required (or use --native)" >&2; exit 1; }

run_in_container() {
    # Everything happens in a throwaway container; only $OUT_DIR is bind-mounted.
    docker run --rm \
        -v "$REPO_ROOT":/src:ro \
        -v "$OUT_DIR":/out \
        -e SPEC_FLAVOR="$1" \
        -e FLAVOR="$1" \
        "$BASE_IMAGE" \
        bash -lc '
            set -euo pipefail
            export DEBIAN_FRONTEND=noninteractive
            PY=python3
            python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || {
                echo "this image has Python < 3.10, which the project does not support" >&2
                exit 1
            }
            apt-get update -qq
            apt-get install -y -qq binutils >/dev/null
            cp -r /src /build
            cd /build
            "$PY" -m venv /venv
            /venv/bin/pip install -q --upgrade pip
            /venv/bin/pip install -q -e . pyinstaller
            mkdir -p /build/dist
            cd /build
            /venv/bin/pyinstaller --clean --noconfirm \
                --distpath /build/dist --workpath /build/build/pyi \
                --specpath /build/packaging packaging/pyinstaller.spec
            NAME=srcmgr; [ "$FLAVOR" = "cli" ] && NAME=srcmgr-cli
            cp "/build/dist/$NAME" "/out/${NAME}-linux-x86_64"
            chmod +x "/out/${NAME}-linux-x86_64"
            "/out/${NAME}-linux-x86_64" --version
        '
}

build_native() {
    local flavor="$1" name
    name=srcmgr; [ "$flavor" = "cli" ] && name=srcmgr-cli
    (.venv/bin/pip install -q pyinstaller 2>/dev/null || true)
    SPEC_FLAVOR="$flavor" .venv/bin/pyinstaller --clean --noconfirm \
        --distpath "$OUT_DIR" --workpath build/pyi packaging/pyinstaller.spec
    mv "$OUT_DIR/$name" "$OUT_DIR/$name-linux-$(uname -m)"
}

mkdir -p "$OUT_DIR"
for flavor in $FLAVORS; do
    echo "==> building flavour: $flavor ($MODE, base $BASE_IMAGE)"
    if [ "$MODE" = "container" ]; then
        run_in_container "$flavor"
    else
        build_native "$flavor"
    fi
done

( cd "$OUT_DIR" && sha256sum ./*-linux-* > SHA256SUMS 2>/dev/null || true )
echo
echo "artifacts in $OUT_DIR:"
ls -lh "$OUT_DIR" | sed 's/^/  /'

if [ "$DO_TEST" = 1 ]; then
    echo
    echo "==> smoke-testing in a bare container (no Python installed)"
    docker run --rm -v "$OUT_DIR":/out:ro ubuntu:22.04 bash -lc '
        set -e
        /out/srcmgr-linux-x86_64 --version
        /out/srcmgr-linux-x86_64 --cli managers | head -5
        /out/srcmgr-linux-x86_64 --cli --json managers >/dev/null
        /out/srcmgr-cli-linux-x86_64 | head -3
        echo "SMOKE TEST PASSED"
    '
fi
