#!/bin/zsh
# Wydanie nowej wersji: scripts/release.sh 0.6.0 ["krótki opis wersji"]
# Podbija wersję w pliku VERSION (commit; wspólna z Linuksem), uruchamia pełne scripts/release-check.sh (poziomy 1–3),
# a tag v<wersja> i push na origin/main robi tylko wtedy, gdy wszystko jest zielone.
set -euo pipefail
cd "$(dirname "$0")/.."

NEW=${1:-}
SUMMARY=${2:-}
[[ $NEW =~ '^[0-9]+\.[0-9]+\.[0-9]+$' ]] || { echo "Użycie: scripts/release.sh <X.Y.Z> [\"opis wersji\"]"; exit 2; }
[[ $(git branch --show-current) == main ]] || { echo "Wydania tylko z main."; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Są niezapisane zmiany — najpierw commit."; exit 1; }
git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null && { echo "Tag v$NEW już istnieje."; exit 1; }

# Treść tagu: opis jak w poprzednich wersjach + zmiany na main od poprzedniego tagu (--first-parent:
# gałęzie wchodzą jednym wpisem „Merge branch…”, bez ich pojedynczych commitów).
PREV=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || true)
NOTES=$(mktemp)
{
  echo "v$NEW${SUMMARY:+: $SUMMARY}"
  if [ -n "$PREV" ]; then
    echo; echo "Zmiany od $PREV:"
    # Bez commitu z samą wersją (po nieudanej próbie już istnieje).
    git log --first-parent --format='- %s' "$PREV..HEAD" | grep -vxF -- "- v$NEW" || true
  fi
} > $NOTES

CURRENT=$(<VERSION)
if [[ $CURRENT != $NEW ]]; then
  print -r -- $NEW > VERSION
  git commit -q -am "v$NEW"
  echo "Wersja $CURRENT → $NEW (commit $(git rev-parse --short HEAD), jeszcze bez tagu)"
fi

if ! ./scripts/release-check.sh; then
  echo "\n⛔ Wydanie v$NEW zablokowane — popraw i uruchom scripts/release.sh $NEW ponownie (commit z wersją zostaje)."
  exit 1
fi

git tag -a "v$NEW" -F $NOTES
rm -f $NOTES
git push origin main "v$NEW"
echo "✅ v$NEW otagowane i wypchnięte."
