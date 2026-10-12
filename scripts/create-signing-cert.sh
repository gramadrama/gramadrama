#!/bin/zsh
# Tworzy lokalny, samopodpisany certyfikat do podpisywania GramaDramy.
# Dzięki stałemu podpisowi macOS pamięta uprawnienie „Dostępność" między kolejnymi buildami.
set -euo pipefail
NAME="GramaDrama Local Signing"
if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  echo "✓ Certyfikat „$NAME” już istnieje"; exit 0
fi
TMP=$(mktemp -d)
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $NAME
[ext]
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
basicConstraints = critical, CA:false
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$TMP/cert.cnf" -extensions ext \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -out "$TMP/cert.p12" \
  -passout pass:gramadrama 2>/dev/null || \
openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -out "$TMP/cert.p12" -passout pass:gramadrama
security import "$TMP/cert.p12" -k ~/Library/Keychains/login.keychain-db -P gramadrama -T /usr/bin/codesign
rm -rf "$TMP"
echo "✓ Utworzono certyfikat „$NAME”"
