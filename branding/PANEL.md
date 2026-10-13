# GramaDrama — correction panel (shared by macOS and Linux)

The single source of truth for how the panel looks and behaves. Implementations:
- **macOS:** `Sources/GramaDrama/PanelView.swift` (SwiftUI in a non-activating `NSPanel`),
- **Linux:** the GNOME Shell extension `linux/gnome-extension/` (St), drawn by GNOME Shell itself,
  because only then does the target application keep the focus. The IBus engine sends it the panel state.

The interface code cannot be shared (SwiftUI vs GNOME Shell), so both versions follow
this document: the layout, colours, texts and shortcuts. A change in one version = a change here.

## Layout (from the top)

```
┌────────────────────────────────────────────────────────── 520 px ─┐
│ [masks] GramaDrama              [PL → EN ⇄]   [3 poprawki]    [×] │  header
│ ┌ Popraw ┬ Ulepsz ┬ Formalnie ┬ Luźniej ┬ Krócej ┬ Tłumacz ┐       │  modes
│ ✎ Wynik — możesz go poprawić, zanim wstawisz                      │  field label
│ ┌───────────────────────────────────────────────────────────────┐ │
│ │ Tekst z [błędem] i dodanym przecinkiem[,] który…               │ │  the result, a field (≤ 220 px)
│ └───────────────────────────────────────────────────────────────┘ │
│ ┌───────────────────────────────────────────────────────┐         │
│ │ ~~bledem~~ → błędem · Polskie znaki                    │         │  list of changes, on demand (≤ 130 px)
│ │ → , · Brakujący znak interpunkcyjny                    │         │
│ └───────────────────────────────────────────────────────┘         │
│ Bielik 11B · 2,9 s · akapit   [Ukryj zmiany ▴] [Kopiuj] [Zastąp]  │  footer
└───────────────────────────────────────────────────────────────────┘
```

- Width 520 px, margins 16 px, spacing between sections 12 px, corner radius 16 px,
  surface background from `BRAND.md` (`#FFFEFC` light, `#242827` dark, following the theme),
  border `#DDD8D0` / `#505852`, shadow.
- The panel sits below the text field (Linux: below the text cursor), or above it when there is not enough room.
  Its height follows the content.

## Header

- **Logo:** two masks (happy and sad), neutral (`#4D4B4F` in light, `#B9BDB8` in dark), without
  the square, 20 px. Filled when the correction has finished, outlined while it runs.
- **“GramaDrama”** — bold.
- **Translation direction** (only in the **Translate** (*Tłumacz*) mode): a small button `PL → EN ⇄` / `EN → PL ⇄`;
  a click or the shortcut reverses the direction.
- **Status badge:**

| State | Content |
|---|---|
| checking rules | „Czytam scenariusz…” (“Reading the script…”) |
| stream from the model | activity indicator + „Poprawiam…” (“Correcting…”) / „Tłumaczę…” (“Translating…”) |
| done, with changes | „N poprawka / poprawki / poprawek” (“N change / changes”) on a sage capsule |
| done, no changes | „Zero dramatu” (“No drama”) in sage |
| error | warning triangle in the error colour (rose) |
| Enter pressed while working | activity indicator + „Zastosuję po zakończeniu” (“Will apply when finished”) |
| replacing the text | activity indicator + „Zastępuję…” (“Replacing…”) |

## Modes

Segmented control: **Correct · Improve · Formal · Casual · Shorter · Translate**
(*Popraw · Ulepsz · Formalnie · Luźniej · Krócej · Tłumacz*). The active
segment is neutral: bold text on a light background (`#F0EDE7` / `#303631`), because lavender is
reserved for actions on the text. Changing the mode immediately runs the correction of the same text again.

## Content

- **Result** (modes other than Translate): the corrected text alone, with no struck-through text in it: with many
  corrections a diff is hard to read. What the correction changed (inserted or replaced text, an added comma) sits on
  the sage background, in the normal text colour; deleted text is not shown (the list of changes, on demand, says
  what went). The label shows exactly the result, so a click or a caret in it is a place in the result. On macOS,
  changes separated only by spaces share one mark, without spaces at its ends („wyślę cały”), whichever way the word
  diff splits them; a space added alone („w cale”) stays marked.
- **The result is a field:** a finished result that can be inserted is framed (`#DDD8D0` / `#505852`, corner radius
  8 px, padding 8/10 px) with „Wynik — możesz go poprawić, zanim wstawisz” (“Result — you can edit it before you
  insert it”) and a pencil above it, small and dimmed. Typing or a click edits it at once (Editing); the frame turns
  lavender while it is being edited. macOS frames the copy-only result too, with „Wynik — możesz go poprawić, zanim
  skopiujesz” (“Result — you can edit it before you copy it”).
- **Translation:** the original small and dimmed (max. 3 lines), a rule, and below it the translation as the
  main content.
- **While streaming:** the model's text so far in the secondary colour.
- **Error:** grouped by what the user can do about it (`linux/gramadrama/engine/problems.py` and, the same table on
  macOS, `Sources/GramaCore/Problems.swift` map the cause's code to the group, the title, the sentence and the buttons). The title in the error colour and one sentence on a light
  rose tint (`rgba(196, 86, 106, 0.16)`, corner radius 12 px); the footer says „tekst bez zmian” (“text unchanged”) after
  the scope, and the error's buttons take the place of Copy and Replace: at most two, the first one is what ↩ does,
  lavender, last, with its key („Enter”; macOS „↩”).
  - **Something must be set up** (no model or Ollama on a new install — macOS tells a missing Ollama from a stopped one
    when „Ten Mac” does not answer —, a model the server does not have, a refused key, no second language for Translate): „Otwórz Usługi” (“Open Services”) or „Otwórz Pisanie” (“Open Writing”) opens the
    app on that tab, with the group the cause is in ringed for a few seconds, and closes the panel; no „Spróbuj ponownie” (“Try again”), which would not help.
  - **The server does not answer:** „Spróbuj ponownie” runs the same text in the same mode again, „Otwórz Usługi” next
    to it; away from home without the sign-in the first button is „Zaloguj się…” (“Sign in…”).
  - **The model slipped up** (the connection broke, an empty answer, an answer that changed a link or wrote another
    script; on macOS also a connection dropped while the answer streams): the app tries once more on its own, with „Ponawiam…” (“Trying again…”) in the header; only a second
    slip is shown, with „Spróbuj ponownie”. A cut-short answer is not retried and has no button: only a shorter
    selection helps.
  - **The text cannot be corrected as it is** (empty, too long, an attachment): no panel, a toast with the message,
    as when the shortcut refuses such a text (macOS refuses such texts before the panel opens).
  - Linux with an extension before version 16: the message in the secondary colour, and ↩ closes.
- **Fallback mode:** removed on both platforms (macOS on 29 September, Linux on 1 October): when the model server
  cannot be reached, the panel shows an error in every mode. LanguageTool stays in the panel for the reasons
  for changes; its result does not replace a full correction by the model.
- The content scrolls above 220 px.

## List of changes

On demand: **Show N changes ▾** (*Pokaż N poprawek ▾*) in the footer or Ctrl+I shows it below the result,
**Hide changes ▴** (*Ukryj zmiany ▴*) or Ctrl+I again hides it; every new result starts with it hidden, and there is
no list while editing. In a frame with a subtle background (corner radius 12 px, max. 130 px, up to 30 items):
`old` (struck through, secondary) `→` `new` (sage, bold) `· reason` (small, secondary).
The reasons are computed the same way on both platforms (Mac: `Diff.changes`, Linux: `core.changes`):
Spacing (*Odstęp*) · Word spacing (*Pisownia łączna / rozdzielna*) ·
Missing / Unnecessary punctuation (*Brakujący / Zbędny znak interpunkcyjny*) ·
Capitalization (*Wielka / mała litera*) · Polish diacritics (*Polskie znaki*; also with word spacing, and with punctuation) ·
Punctuation (*Interpunkcja*) · the LanguageTool message (without MORFOLOGIK rules) · Typo (*Literówka*) ·
Grammar / style (*Gramatyka / styl*). In the Translate mode the list is empty.

For each change: **Undo** (*Cofnij*; back arrow) — only this change returns to the original, the other
corrections stay; when one word is changed into another, also **Keep “…” and add it to the dictionary**
(*Zostaw „…” i dodaj do słownika*; book) — the word from the original stays and goes into the user dictionary
(macOS: the system dictionary and GramaDrama's list of added words; Linux: the Enchant dictionary, the same one as
for checking while typing).
A change includes adjacent insertions and deletions, so a comma added right after a word is undone together
with it (then there is only Undo). The diff and the counter are recomputed.

## Editing

Clicking the result text turns the preview into a text field with the cursor at the clicked place, and typing without
clicking does the same with the cursor at the end and the typed character inserted. ⌘E / Ctrl+E does the same, and ends
editing again like ⎋; the diff and the list of changes are then computed from the edited text. While editing, what the
edited text changes against the original (the corrections that stay and what was typed) keeps the sage background.
There is no Edit button on either platform: the result is the field. The badge says „Edycja” (macOS „Edycja wyniku”,
“Editing”) and the footer note gives the keys: „⇧↩ nowa linia · ⎋ kończy edycję”. GramaDrama checks the field just
like text in other applications: the dictionary on the Mac (in the chosen languages) immediately, LanguageTool after a
short pause in typing if it is turned on; no model in the background. An error has a brick wave (the system spell
checking is turned off), and the cursor on an underlined word shows below the field the word, clickable suggestions,
the message, **Ignore** (*Ignoruj*) and **Add to dictionary** (*Dodaj do słownika*), or **Turn off rule** (*Wyłącz
regułę*) for a LanguageTool grammar rule (no message in the panel; Writing › Dictionary and rules turns it on again);
otherwise the number of issues,
„Sprawdzam…” (“Checking…”) until LanguageTool answers, or „Bez uwag.” (“No issues.”), always two lines high.
No autocorrection and no quote substitution. While editing, ↩ inserts the result (like **Replace** (*Zastąp*)),
⇧↩ gives a new line, ⎋ ends editing, and only the next ⎋ closes the panel. The modes and the translation direction
are then inactive (they would recompute the result and discard the edits); in Translate the original stays above.

Linux: Ctrl+E, a letter or Backspace in a finished result, or a click in the result (the cursor at the clicked place;
while editing a click moves the cursor; no selecting by dragging with the mouse). The panel does not take the keyboard
(the text goes back to the application's field through IBus), so the engine does the editing and the panel draws the text,
the selection and the cursor. Supported: typing (also letters with AltGr), arrows, Ctrl+arrows by word, Home/End within
the panel line, Ctrl+Home/End, Backspace and Delete (with Ctrl by word), selecting with Shift, Ctrl+A, Ctrl+Z and
Ctrl+Shift+Z (Ctrl+Y), the clipboard (Ctrl+C, Ctrl+X, Ctrl+V; Ctrl+Insert, Shift+Delete, Shift+Insert — through the panel,
because the engine has no clipboard on Wayland), ⇧↩, ↩ and Esc as above. After editing, the diff and the list of changes
are recomputed, keeping the reasons of the changes that stayed the same. While editing, the text is checked by the
checking-while-typing service (the dictionary immediately, LanguageTool after a pause, without the model, also in the
“model only” (*tylko model*) mode): a brick wave under an error, and below the text, with the cursor on it,
the word, up to three suggestions, the message, Ignore and Add to dictionary; otherwise the number of issues (always two lines).

## Footer

- On the left, in small dimmed text: model · direction (translation) · time „2,9 s” · scope:
  „akapit” (“paragraph”), „zaznaczenie” (“selection”), „całe pole” (“whole field”).
- On the right: **Show N changes ▾** / **Hide changes ▴** (quiet text button, only with a list of changes) ·
  **Copy** (*Kopiuj*; outline) · **Replace** (Enter; lavender; inactive until there is
  a finished result with changes). When the text cannot be replaced, the default Enter action is Copy
  (then in lavender), and Replace disappears. **Copy** copies the result to the clipboard and closes the panel
  (message „Skopiowano do schowka.” (“Copied to clipboard.”)).
- **Cancel** (*Anuluj*) is the “×” in the right corner of the header (Esc on the keyboard); the footer is kept for actions on the text.

## Keyboard

The first two shortcuts are defaults; they are changed in **Home → Shortcuts → Change shortcuts**
(*Start → Skróty → Zmień skróty*).

| Action | macOS | Linux |
|---|---|---|
| Open the panel for the selection / paragraph (always in Correct mode) | ⌥⌘G | Ctrl+Alt+G |
| Open directly in Translate mode | ⌥⌘T | Ctrl+Alt+J (Ctrl+Alt+T opens a terminal in GNOME) |
| Replace | ↩ | Enter |
| Cancel | ⎋ | Esc |
| Modes | ⌘1–⌘6 | Ctrl+1–6 |
| Reverse the translation direction | ⌘D | Ctrl+D (and Ctrl+6 again) |
| Copy the result | ⌘⇧C | Copy button |
| Edit the result / Done | ⌘E, click in the text or typing | Ctrl+E, click in the text or typing |
| Show / hide the list of changes | ⌘I | Ctrl+I |
| While editing: new line / end editing | ⇧↩ / ⎋ | — |
| ↩ when there is nothing to apply („Zero dramatu”, error) | closes the panel; on an error with buttons, the first button | closes the panel; on an error with buttons, the first button |

## Colours and microcopy

Colours from `BRAND.md`: lavender — app actions, brick — what checking found, sage —
corrected text, rose — app error, on warm neutral backgrounds.
Texts: „Czytam scenariusz…” (“Reading the script…”), „Zero dramatu” (“No drama”); errors say what happened without the
theatre, e.g. „Serwer nie odpowiada” (“The server does not answer”) and what failed in the connection (Error, above). Refusals (e.g. a password field) are shown
by the same panel as a short message without buttons; it disappears by itself after a few seconds and does not capture keys.

## Checking-while-typing badge

- The colour says what is happening: lavender dots — the app is checking; a brick circle with a number —
  errors found (`BRAND.md`).
- Checking without a known number yet: three lavender dots without a background merge and separate
  like in a lava lamp, in a 24 × 18 px area.
- Result: in 0.52 s the dots merge into a circle 18 px in diameter, lavender turns into
  brick `#AD4E2C` in 0.2 s, and the number pops in (0.24 s). From 10 — a pill, above 99 — “99+”.
  A light 35% outline separates the circle from a dark application background.
- No errors: the dots merge and disappear (0.45 s).
- Another check with a known number: the circle stays and gently “breathes”; the new number pops in
  after the result, without merging again.
- Animations turned off (GNOME: Accessibility → Animations; macOS: Accessibility → Reduce motion): the dots stand
  still, the result appears at once.
- The dots run only while a server is asked (LanguageTool, the model in the background, model-only checking); a result
  from the cache and a pause in typing alone do not start them.
- macOS: a message about checking with no errors (the server does not answer, sentences left unchecked) is a brick
  circle with „!”.
- Geometry: `linux/gnome-extension/lava.js` (test: `linux/tests/lava.test.mjs`); macOS: `Sources/GramaCore/Lava.swift`
  with the same numbers and the badge's states (test: `tests/GramaCoreTests/LavaTests.swift`), drawn by `LiveBadge`.

## Scope and refusals

- With no selection: macOS corrects the paragraph with the cursor in native fields (TextEdit, Notes). In web
  fields (Slack, Outlook, Mail) — the whole field, unless it contains quoted correspondence or exceeds
  the limit: then only the new message above the quote (footer: „nowa wiadomość (bez cytatów)” (“new message (without quotes)”)),
  and when the cursor is in the quote — the paragraph with the cursor. Quoted emails stay untouched. Linux: always the paragraph.
- The model server cannot be reached: the error „Serwer nie odpowiada” with what failed in the connection, in every mode,
  with no answer from LanguageTool alone (fallback mode removed). Other errors (a wrong model name, an error in the stream,
  a text that is too long) are also shown as an error.
- The result after repairs (`PostProcess.checked`, `core.clean`): an empty model response is the error „Model zwrócił pustą
  odpowiedź” (“The model returned an empty answer”), after one quiet retry (a comment alone or „[Your name]” alone
  is still „Zero dramatu”). A result in which a link, email address, mention, channel or code still changed, the author's emoji
  got lost or (only in Correct) a new emoji was added is rejected, also after one quiet retry: „Wynik odrzucono” (“Result
  rejected”) with „Model zmienił link, wzmiankę, kod lub emoji. Wynik odrzucono; spróbuj ponownie.” (“The model changed a
  link, mention, code or emoji. The result was rejected; try again.”)
  Shared cases: `tests/result_check_cases.json`.
- Password fields: refusal without reading the text („To pole hasła — GramaDrama go nie czyta.” (“This is a password field —
  GramaDrama does not read it.”); Linux: „Pole hasła lub prywatne — GramaDrama go nie czyta.” (“Password or private field —
  GramaDrama does not read it.”), because IBus reports passwords and private fields the same way).
- Before replacing, both versions check that the text in the field has not changed; if it has, the correction stays
  on the clipboard and a message is shown. After pasting into a native field, macOS also checks that the correction
  really landed there.

## Shared test files

The prompts and the cleanup of model responses are the same on both platforms, which the tests
on Linux check with files written by macOS: `bench/prompts.golden.json` (`--dump-prompts`,
messages to the character) and `tests/postprocess_cases.json` (`--selftest --dump-cases`). After a change
of a prompt or of the post-processing on macOS they have to be generated again; the Linux test shows
what to port.

## Differences still to be aligned

- macOS: read-only text (Slack history, terminal) gives a “Copy only” panel; Linux refuses,
  because IBus gets text only from edit fields, and the terminal does not expose it.
- macOS: the panel can be moved with the mouse; Linux: the panel stays at the text cursor.
