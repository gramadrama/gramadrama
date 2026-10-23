#!/bin/zsh
# Buduje GramaDrama.app i instaluje w ~/Applications.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/swift-env.sh

# Wersja wspólna z Linuksem: plik VERSION (podbija ją scripts/release.sh).
VERSION=${VERSION:-$(<VERSION)}
# Numer builda = commit (z „+”, gdy są niezapisane zmiany) — widoczny w Ustawieniach → Informacje.
BUILD=$(git rev-parse --short HEAD 2>/dev/null || echo dev)$([ -z "$(git status --porcelain 2>/dev/null)" ] || echo +)
# .noindex: Spotlight pomija ten katalog — w wyszukiwarce jest tylko zainstalowana kopia z ~/Applications.
APP=build.noindex/GramaDrama.app
SIGN_NAME="GramaDrama Local Signing"

swift build -c release
[ -f branding/AppIcon.icns ] || swift scripts/make-icon.swift branding

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/GramaDrama "$APP/Contents/MacOS/GramaDrama"
cp .build/release/GramaBrowserHost "$APP/Contents/MacOS/GramaBrowserHost"
cp branding/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
# Teksty interfejsu po angielsku (UI.swift czyta je z Contents/Resources/locales/en).
cp -R Resources/locales "$APP/Contents/Resources/locales"
sed -e "s/__VERSION__/$VERSION/g" -e "s/__BUILD__/$BUILD/g" Resources/Info.plist > "$APP/Contents/Info.plist"

if security find-certificate -c "$SIGN_NAME" >/dev/null 2>&1; then
  # Host wtyczki przeglądarki: najpierw on (zagnieżdżony kod), z identyfikatorem sprawdzanym przez aplikację.
  codesign --force --options runtime --identifier pl.gramadrama.browser-host --sign "$SIGN_NAME" "$APP/Contents/MacOS/GramaBrowserHost"
  codesign --force --options runtime --entitlements Resources/GramaDrama.entitlements --sign "$SIGN_NAME" "$APP"
else
  echo "⚠︎ Brak certyfikatu — podpis ad-hoc (uruchom scripts/create-signing-cert.sh, żeby nie tracić uprawnień po każdym buildzie)"
  codesign --force --sign - --identifier pl.gramadrama.browser-host "$APP/Contents/MacOS/GramaBrowserHost"
  codesign --force --sign - "$APP"
fi

mkdir -p ~/Applications
pkill -x GramaDrama 2>/dev/null || true
rm -rf ~/Applications/GramaDrama.app
cp -R "$APP" ~/Applications/
echo "✓ ~/Applications/GramaDrama.app ($VERSION, $BUILD)"

# Metoda wprowadzania — podmieniana tylko przy zmianie jej kodu (scripts/install-ime.sh).
./scripts/install-ime.sh
