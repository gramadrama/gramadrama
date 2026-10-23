#!/bin/zsh
# Pełna seria testów przed wydaniem. Raport w build.noindex/reports/<wersja>-<czas>/ (summary.md + logi + JSON).
#   scripts/release-check.sh            poziomy 1–3
#   scripts/release-check.sh --level=1  tylko logika (bez zewnętrznego serwera, bez pulpitu)
#   scripts/release-check.sh --level=2  logika + modele (skonfigurowany serwer)
# Poziom 3 instaluje sprawdzaną wersję w ~/Applications i przez ~1 min steruje Makiem (ręce z dala).
# Przed nim pauza: w terminalu Enter, bez terminala jednorazowy plik zgody wskazany przez GRAMADRAMA_LEVEL3_GO.
# Kod wyjścia 0 = wszystko zielone. Progi: bench/release.json.
set -uo pipefail
# Zapobiega uśpieniu podczas testów: -i utrzymuje działanie, -d włączony ekran potrzebny w poziomie 3.
if [[ -z ${GRAMADRAMA_CAFFEINATED:-} ]] && command -v caffeinate >/dev/null; then
  GRAMADRAMA_CAFFEINATED=1 exec caffeinate -di "${0:A}" "$@"
fi
cd "$(dirname "$0")/.."
source scripts/swift-env.sh

LEVEL=3
for arg in "$@"; do
  case $arg in
    --level=*) LEVEL=${arg#--level=} ;;
    *) echo "Nieznany argument: $arg"; exit 2 ;;
  esac
done

COMMIT=$(git rev-parse --short HEAD)$([ -z "$(git status --porcelain)" ] || echo +)
VERSION=$(<VERSION)
REPORTS=build.noindex/reports
DIR=$REPORTS/$VERSION-$COMMIT-$(date +%Y%m%d-%H%M%S)
mkdir -p $DIR
# Najnowsze zielone sprawdzenie według czasu (nazwy zaczynają się od wersji i commitu, więc nie sortują się w czasie).
PREVIOUS=$(ls -td $REPORTS/*/ 2>/dev/null | while read d; do [ -f $d/passed ] && { echo ${d%/}; break; }; done)
BIN=.build/release/GramaDrama
# Benchmark ma osobną domenę ustawień; przekazujemy model wybrany w aplikacji albo jej domyślny model.
MODEL=$(defaults read pl.gramadrama.app model 2>/dev/null || sed -n '/^[[:space:]]*model = /s/.*?? "\([^"]*\)".*/\1/p' Sources/GramaBackend/Settings.swift)
# …i serwer modelu z aplikacji: „Ten Mac” (Ollama na 127.0.0.1) albo adres i protokół serwera. Ustawienia sprzed
# wyboru „Ten Mac / Serwer” (sam host w serverHost) aplikacja przenosi przy starcie — tu czytamy je tak samo.
# Klucza API z pęku kluczy benchmark nie dostaje: serwer wymagający klucza trzeba sprawdzić w aplikacji.
model_server() {
  local location=$(defaults read pl.gramadrama.app modelLocation 2>/dev/null || true)
  local host=$(defaults read pl.gramadrama.app serverHost 2>/dev/null || true)
  SERVER_URL="http://127.0.0.1:11434"
  SERVER_API=ollama
  if [[ $location == server ]]; then
    SERVER_URL=$(defaults read pl.gramadrama.app modelServerURL 2>/dev/null || true)
    SERVER_API=$(defaults read pl.gramadrama.app modelServerAPI 2>/dev/null || echo ollama)
  elif [[ -z $location && -n $host ]]; then
    host=${host#*://}; host=${host%%/*}
    # IPv6 w nawiasach; port z dawnego pola pomijamy (Ollama zawsze na 11434).
    if [[ $host == *:*:* ]]; then host="[${host//[\[\]]/}]"; else host=${host%%:*}; fi
    [[ $host == (127.0.0.1|localhost|\[::1\]) ]] || SERVER_URL="http://$host:11434"
  fi
  while [[ $SERVER_URL == */ ]]; do SERVER_URL=${SERVER_URL%/}; done
}
model_server
typeset -a NAMES STATUS TIMES
FAILED=0

# step <nazwa> <polecenie…> — log w $DIR/<nazwa>.log, wynik w tabeli.
step() {
  local name=$1; shift
  local started=$SECONDS
  printf '▶ %-22s' "$name"
  if "$@" > $DIR/$name.log 2>&1; then
    STATUS+=("✅"); printf '✅ %3ds\n' $((SECONDS - started))
  else
    STATUS+=("❌"); FAILED=$((FAILED + 1)); printf '❌ %3ds  (log: %s)\n' $((SECONDS - started)) $DIR/$name.log
    tail -5 $DIR/$name.log | sed 's/^/    /'
  fi
  NAMES+=("$name"); TIMES+=($((SECONDS - started)))
}

# Strony testowe rozszerzenia w Chrome bez okna (świeży profil — inaczej Chrome bierze stare pliki z cache).
browser_pages() {
  local chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  [ -x "$chrome" ] || chrome="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
  local port=8765 ok=0
  python3 -m http.server $port --bind 127.0.0.1 >/dev/null 2>&1 &
  local server=$!
  sleep 1
  for page in harness prosemirror; do
    rm -rf $DIR/chrome-profile
    local dom=$(perl -e 'alarm 45; exec @ARGV' "$chrome" --headless=new --disable-gpu --no-first-run \
      --user-data-dir=$DIR/chrome-profile --virtual-time-budget=8000 --dump-dom \
      "http://127.0.0.1:$port/browser/test/$page.html" 2>/dev/null)
    echo "$dom" | sed -n '/<pre id="result">/,/<\/pre>/p' > $DIR/browser-$page.json
    if echo "$dom" | grep -q '<title>PASS</title>'; then echo "$page: PASS"; else echo "$page: FAIL"; cat $DIR/browser-$page.json; ok=1; fi
  done
  kill $server 2>/dev/null
  rm -rf $DIR/chrome-profile
  return $ok
}

model_server_up() {
  [ -n "$SERVER_URL" ] || { echo "Ustaw serwer modelu w aplikacji przed benchmarkiem." >&2; return 1; }
  [ -n "$MODEL" ] || { echo "Wybierz model w aplikacji przed benchmarkiem." >&2; return 1; }
  echo "Sprawdzanie serwera modelu ($SERVER_API)"
  # Lista modeli: Ollama /api/tags, API zgodne z OpenAI /models (z /v1 dopisanym do adresu bez ścieżki).
  local list=$SERVER_URL/api/tags
  if [[ $SERVER_API == openai ]]; then
    list=$SERVER_URL
    [[ ${list#*://} == */* ]] || list=$list/v1
    list=$list/models
  fi
  # Model aplikacji musi być na serwerze — inaczej bench kończy się samymi błędami bez wyjaśnienia.
  curl -sf --max-time 5 "$list" | python3 -c "
import json, sys
try:
    reply = json.load(sys.stdin)
    names = [m.get('name') or m.get('id') for m in reply.get('models') or reply.get('data') or []]
except (ValueError, TypeError, AttributeError):
    print('Nie udało się odczytać listy modeli serwera.'); sys.exit(1)
model = sys.argv[1]
print('Wybrany model jest dostępny.' if model in names else 'Wybranego modelu nie ma na serwerze.')
sys.exit(0 if model in names else 1)
" "$MODEL"
}

# check_bench <plik JSON> <klucz w release.json>
check_bench() {
  python3 - "$1" "$2" <<'EOF'
import json, sys
summary = json.load(open(sys.argv[1]))[0]
limits = json.load(open("bench/release.json"))[sys.argv[2]]
problems = []
ratio = summary["exact"] / summary["total"] if summary["total"] else 0
# Odsetek zachowuje sens progu, gdy rośnie zestaw benchmarku.
if ratio < limits.get("minExactRatio", 0): problems.append(f"dokładne {ratio:.0%} < {limits['minExactRatio']:.0%}")
if summary["exact"] < limits.get("minExact", 0): problems.append(f"dokładne {summary['exact']} < {limits['minExact']}")
if summary["falseAlarms"] > limits.get("maxFalseAlarms", 99): problems.append(f"fałszywe alarmy {summary['falseAlarms']} > {limits['maxFalseAlarms']}")
# „Inne zmiany”: model zmienił coś innego, niż trzeba (wymyślone słowo, synonim) — groźniejsze niż pominięcie.
if summary.get("wrongEdits", 0) > limits.get("maxWrongEdits", 99): problems.append(f"inne zmiany {summary['wrongEdits']} > {limits['maxWrongEdits']}")
if summary["medianSeconds"] > limits.get("maxMedianSeconds", 99): problems.append(f"mediana {summary['medianSeconds']:.1f} s > {limits['maxMedianSeconds']}")
if summary["errors"] > limits.get("maxErrors", 0): problems.append(f"błędy {summary['errors']}")
line = f"{summary['exact']}/{summary['total']} dokładnych ({ratio:.0%}), fałszywe alarmy {summary['falseAlarms']}/{summary['cleanCases']}, mediana {summary['medianSeconds']:.1f} s"
if sys.argv[2] == "fix": line += f", pominięte {summary.get('missed', '?')}, inne zmiany {summary.get('wrongEdits', '?')}"
print(line)
print("\n".join(problems) or "w progach")
sys.exit(1 if problems else 0)
EOF
}

bench_fix() { $BIN --bench bench/cases.json "$MODEL" --server="$SERVER_URL" --api=$SERVER_API --json=$DIR/bench-fix.json && check_bench $DIR/bench-fix.json fix; }
bench_translate() { $BIN --bench bench/translate.json "$MODEL" --server="$SERVER_URL" --api=$SERVER_API --mode=translate --json=$DIR/bench-translate.json && check_bench $DIR/bench-translate.json translate; }

dictation() {
  $BIN --dictation-bench bench/dictation.json --json=$DIR/dictation.json || return 1
  python3 - $DIR/dictation.json <<'EOF'
import json, sys
r = json.load(open(sys.argv[1])); limits = json.load(open("bench/release.json"))["dictation"]
problems = []
if r["meanWER"] > limits["maxMeanWER"]: problems.append(f"średni WER {r['meanWER']:.2f} > {limits['maxMeanWER']}")
if r["medianAsrMs"] > limits["maxMedianAsrMs"]: problems.append(f"mediana {r['medianAsrMs']} ms > {limits['maxMedianAsrMs']}")
print("\n".join(problems) or "w progach"); sys.exit(1 if problems else 0)
EOF
}

# Poziom 3 wkleja ⌘V do okna na wierzchu — rusza dopiero, gdy użytkownik odszedł od Maca.
hands_off() {
  if [ -n "${GRAMADRAMA_LEVEL3_GO:-}" ]; then
    local waited=0
    until [ -f "$GRAMADRAMA_LEVEL3_GO" ]; do
      sleep 1
      (( ++waited > 1800 )) && { echo "Brak zgody przez 30 min — poziom 3 pominięty"; return 1; }
    done
    rm -f -- "$GRAMADRAMA_LEVEL3_GO" || return 1  # zgoda jednorazowa
    echo "Zgoda po $waited s"
  elif [ -t 0 ]; then
    printf '⏸  Poziom 3 przez ok. 2 min steruje Makiem (⌘V do okna na wierzchu). Odejdź od klawiatury i naciśnij Enter… ' > /dev/tty || return 1
    read -r < /dev/tty || { echo "Nie potwierdzono zgody — poziom 3 pominięty"; return 1; }
    echo "Potwierdzono zgodę w terminalu"
  else
    echo "Poziom 3 wymaga potwierdzenia w terminalu albo pliku zgody GRAMADRAMA_LEVEL3_GO." >&2
    return 1
  fi
}

install_app() { ./scripts/build-app.sh; }
# open -W zwraca 0 niezależnie od kodu wyjścia aplikacji — wynik bierzemy z pliku (ostatnia linia „Wszystko OK”).
e2e_result() { [ -s "$1" ] && cat "$1" && [[ $(grep -v '^[[:space:]]*$' "$1" | tail -1) == "Wszystko OK" ]]; }
e2e() { rm -f $DIR/e2e.txt; open -n -W ~/Applications/GramaDrama.app --args --e2e $PWD/$DIR/e2e.txt; e2e_result $DIR/e2e.txt; }
e2e_ime() { rm -f $DIR/e2e-ime.txt; open -n -W ~/Applications/GramaDrama.app --args --e2e-ime $PWD/$DIR/e2e-ime.txt; e2e_result $DIR/e2e-ime.txt; }

echo "GramaDrama $VERSION ($COMMIT) · poziom $LEVEL · model $MODEL · raport: $DIR"
[ -n "$PREVIOUS" ] && echo "Poprzednie zielone sprawdzenie: $PREVIOUS"
echo

echo "— Poziom 1: logika"
step build          swift build -c release
step swift-tests    ./scripts/test.sh
step selftest       $BIN --selftest
step browser-pages  browser_pages

if (( LEVEL >= 2 )); then
  echo "— Poziom 2: modele"
  step model-server    model_server_up
  if [[ ${STATUS[-1]} == "✅" ]]; then
    step bench-fix       bench_fix
    step bench-translate bench_translate
  fi
  step dictation       dictation
fi

if (( LEVEL >= 3 )); then
  echo "— Poziom 3: na żywo (instaluje tę wersję)"
  if (( FAILED > 0 )); then
    echo "  pominięty: wcześniejsze kroki nie przeszły (nie instaluję wersji z błędami)"
  else
    if [ -n "${GRAMADRAMA_LEVEL3_GO:-}" ]; then
      echo "⏸  Czekam na plik zgody wskazany przez GRAMADRAMA_LEVEL3_GO (najdłużej 30 min)"
    fi
    step hands-off hands_off
    [[ ${STATUS[-1]} == "✅" ]] && step install install_app
    if [[ ${STATUS[-1]} == "✅" ]]; then
      echo "  ⚠︎  Ręce z dala od klawiatury i myszy przez ok. 1 min (test wkleja ⌘V do okna na wierzchu)."
      for i in 5 4 3 2 1; do printf '\r  start za %d s ' $i; sleep 1; done; echo
      step e2e     e2e
      step e2e-ime e2e_ime
      # Zakończ testowy host przed uruchomieniem zwykłego procesu aplikacji z tej samej paczki.
      pkill -f "MacOS/GramaDrama --e2e-webhost" 2>/dev/null
      open -g ~/Applications/GramaDrama.app --args --background
      sleep 3
      pgrep -qf "MacOS/GramaDrama --background" || echo "  ⚠︎  GramaDrama nie wystartowała — uruchom ją ręcznie"
    fi
  fi
fi

{
  echo "# GramaDrama $VERSION ($COMMIT), poziom $LEVEL, $(date '+%Y-%m-%d %H:%M')"
  echo
  echo "Model: \`$MODEL\`"
  echo
  echo "| Krok | Wynik | Czas |"
  echo "|---|---|---|"
  for i in {1..${#NAMES}}; do echo "| ${NAMES[$i]} | ${STATUS[$i]} | ${TIMES[$i]} s |"; done
  for f in bench-fix bench-translate; do
    [ -f $DIR/$f.log ] && echo "\n**$f:** $(tail -2 $DIR/$f.log | head -1)"
    [ -n "$PREVIOUS" ] && [ -f $PREVIOUS/$f.log ] && echo "(poprzednio: $(tail -2 $PREVIOUS/$f.log | head -1))"
  done
  [ -f $DIR/dictation.log ] && echo "\n**dyktowanie:** $(grep '^==' $DIR/dictation.log | sed 's/^== //')"
  [ -n "$PREVIOUS" ] && [ -f $PREVIOUS/dictation.log ] && echo "(poprzednio: $(grep '^==' $PREVIOUS/dictation.log | sed 's/^== //'))"
} > $DIR/summary.md

echo
if (( FAILED == 0 )); then
  echo "$COMMIT $LEVEL" > $DIR/passed
  echo "✅ Wszystko zielone ($DIR/summary.md)"
  exit 0
fi
echo "❌ Nie przeszło: $FAILED krok(ów) — $DIR/summary.md"
exit 1
