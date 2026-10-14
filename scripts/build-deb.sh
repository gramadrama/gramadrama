#!/usr/bin/env bash
# The Debian package for Linux: build/gramadrama_<version>_all.deb (linux/build_deb.py; --out DIR elsewhere).
#   scripts/build-deb.sh && sudo apt install ./build/gramadrama_*_all.deb
set -euo pipefail
exec /usr/bin/python3 "$(dirname "$0")/../linux/build_deb.py" "$@"
