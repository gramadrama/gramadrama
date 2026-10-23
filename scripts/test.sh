#!/bin/zsh
# Testy logiki (GramaCore) przez Swift Testing — działa na samych Command Line Tools, bez Xcode.
# Z Xcode wystarczy zwykłe `swift test`; tu SwiftPM nie widzi frameworka Testing bez podanej ścieżki.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/swift-env.sh
FRAMEWORKS=/Library/Developer/CommandLineTools/Library/Developer/Frameworks
TESTING_PLUGINS=$SWIFT_TOOLS/usr/lib/swift/host/plugins/testing
args=()
if [[ -d $FRAMEWORKS/Testing.framework ]]; then
  args+=(-Xswiftc -F -Xswiftc $FRAMEWORKS)
fi
# Od CLT 27 (Swift 6.4) domyślny system budowania SwiftPM nie ładuje wtyczki makr Swift Testing z plugins/testing.
if [[ $SWIFT_TOOLS == */CommandLineTools && -f $TESTING_PLUGINS/libTestingMacros.dylib ]]; then
  args+=(-Xswiftc -plugin-path -Xswiftc $TESTING_PLUGINS)
fi
swift test "${args[@]}" "$@"
