#!/bin/zsh
# Buduje metodę wprowadzania GramaDramaIME.app i instaluje ją w ~/Library/Input Methods.
#
# Podmienia ją tylko wtedy, gdy zmienił się jej kod (wersja = skrót źródeł IME): macOS liczy każde wyjście
# procesu IME — także pkill — jako awarię i po serii (~11 w 20 min) blokuje IME we wszystkich działających
# aplikacjach. Zwykły build aplikacji IME więc nie rusza. Podmiana atomowa (nowy pakiet obok, potem zamiana).
# Włączenie źródła: ~/Applications/GramaDrama.app/Contents/MacOS/GramaDrama --ime-enable
set -euo pipefail
cd "$(dirname "$0")/.."

SIGN_NAME="GramaDrama Local Signing"
IME=build.noindex/GramaDramaIME.app
DEST="$HOME/Library/Input Methods/GramaDramaIME.app"

IME_VERSION=$(cat Package.swift Sources/GramaIME/*.swift Sources/GramaIPC/*.swift Sources/GramaIMEProtocol/*.swift \
  Sources/GramaIMEShim/*.m Sources/GramaIMEShim/include/*.h Resources/IME-Info.plist | shasum | cut -c1-12)
INSTALLED=none
if [ -f "$DEST/Contents/Info.plist" ]; then
  INSTALLED=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$DEST/Contents/Info.plist" 2>/dev/null || echo none)
fi
if [ "$IME_VERSION" = "$INSTALLED" ] && [ "${FORCE_IME:-0}" != 1 ]; then
  echo "✓ IME bez zmian ($IME_VERSION)"
  exit 0
fi
if ! security find-certificate -c "$SIGN_NAME" >/dev/null 2>&1; then
  # Aplikacja i IME rozmawiają tylko z procesem podpisanym tym samym certyfikatem — ad hoc nie zadziała.
  echo "⚠︎ Brak certyfikatu „$SIGN_NAME” — IME nie instaluję (scripts/create-signing-cert.sh)"
  exit 0
fi

swift build -c release --product GramaIME
[ -f branding/ime-icon.tiff ] || swift scripts/make-ime-icon.swift branding/ime-icon.tiff
rm -rf "$IME"
mkdir -p "$IME/Contents/MacOS" "$IME/Contents/Resources"
cp .build/release/GramaIME "$IME/Contents/MacOS/GramaIME"
cp branding/ime-icon.tiff "$IME/Contents/Resources/icon.tiff"
sed "s/__IME_VERSION__/$IME_VERSION/" Resources/IME-Info.plist > "$IME/Contents/Info.plist"
codesign --force --options runtime --sign "$SIGN_NAME" "$IME"

mkdir -p "$HOME/Library/Input Methods"
rm -rf "$DEST.new" "$DEST.old"
ditto "$IME" "$DEST.new"
[ -d "$DEST" ] && mv "$DEST" "$DEST.old"
mv "$DEST.new" "$DEST"
rm -rf "$DEST.old"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST"
# Stara kopia działa dalej ze starym kodem — kończymy ją raz (system uruchomi nową przy następnym polu).
if [ "$INSTALLED" != none ]; then pkill -x GramaIME 2>/dev/null || true; sleep 0.5; fi
# Data pierwszej instalacji: aplikacje uruchomione wcześniej mogą nie widzieć IME do restartu (--ime-status).
if [ "$INSTALLED" = none ] && ! defaults read pl.gramadrama.app imeFirstInstalledAt >/dev/null 2>&1; then
  defaults write pl.gramadrama.app imeFirstInstalledAt -date "$(date -u '+%Y-%m-%d %H:%M:%S +0000')"
fi
# Pierwsze uruchomienie przez LaunchServices zdejmuje z pakietu flagę „launch-disabled” — bez tego Ustawienia
# nie pokażą prośby o zgodę na nowe źródło klawiatury.
open -g "$DEST"
echo "✓ $DEST ($INSTALLED → $IME_VERSION)"
