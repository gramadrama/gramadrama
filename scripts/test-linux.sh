#!/usr/bin/env bash
# Unit tests use temporary application directories and local stub services.
set -euo pipefail
cd "$(dirname "$0")/../linux"
test_root=$(mktemp -d "${TMPDIR:-/tmp}/gramadrama-tests.XXXXXX")
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root"/{config,data,cache,state,runtime}
chmod 700 "$test_root/runtime"
env -u DISPLAY -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS \
    XDG_CONFIG_HOME="$test_root/config" XDG_DATA_HOME="$test_root/data" \
    XDG_CACHE_HOME="$test_root/cache" XDG_STATE_HOME="$test_root/state" \
    XDG_RUNTIME_DIR="$test_root/runtime" GSETTINGS_BACKEND=memory NO_AT_BRIDGE=1 \
    /usr/bin/python3 -m unittest discover -s tests "$@"
