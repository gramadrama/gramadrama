#!/bin/zsh
# Testy logiki (GramaCore) przez Swift Testing — działa na samych Command Line Tools, bez Xcode.
# Z Xcode wystarczy zwykłe `swift test`; tu SwiftPM nie widzi frameworka Testing bez podanej ścieżki.
set -euo pipefail
cd "$(dirname "$0")/.."
FRAMEWORKS=/Library/Developer/CommandLineTools/Library/Developer/Frameworks
if [[ -d $FRAMEWORKS/Testing.framework ]]; then
  swift test -Xswiftc -F -Xswiftc $FRAMEWORKS "$@"
else
  swift test "$@"
fi
