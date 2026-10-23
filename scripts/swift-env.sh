# Źródłowany przez skrypty, które budują kod Swift (build-app.sh, test.sh, release-check.sh).
# Same Command Line Tools 27 (bez Xcode): w SDK macOS 27 @State ze SwiftUI jest makrem, a narzędzia nie mają
# jego wtyczki (SwiftUIMacros) — budujemy więc z SDK 26, które instalują obok. Z Xcode, z własnym SDKROOT
# albo gdy wtyczka jest, nic się nie zmienia.
SWIFT_TOOLS=${DEVELOPER_DIR:-$(xcode-select -p 2>/dev/null || true)}
if [[ $SWIFT_TOOLS == */CommandLineTools && -z ${SDKROOT:-} && -d $SWIFT_TOOLS/SDKs/MacOSX26.sdk &&
      ! -e $SWIFT_TOOLS/usr/lib/swift/host/plugins/libSwiftUIMacros.dylib ]]; then
  export SDKROOT=$SWIFT_TOOLS/SDKs/MacOSX26.sdk
  echo "ℹ︎ Command Line Tools bez wtyczki SwiftUIMacros — budowanie z ${SDKROOT:A:t}" >&2
fi
