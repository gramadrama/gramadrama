# Checking while typing on Linux

The badge shows the number of errors in the active field. Hovering over it opens the suggestions,
and clicking a suggestion corrects the word. Clicking the badge opens the panel in **Correct** (*Popraw*) mode.
Underlines appear in applications that expose word geometry and in the browser with the extension.
A result edited in the correction panel is checked by the same spelling and rule layers, without the model
(the text came from it); the panel itself then shows the underlines and fixes.

## Standard layered mode

| Layer | When | Where it runs |
|---|---|---|
| Spelling | after the text changes | local Enchant dictionaries |
| Grammar rules | after about 0.8 s without typing | the configured LanguageTool |
| Model correction | after a longer pause, about 3 s | the configured language model |

Each server layer can be turned off separately. Turning off live checking stops all of it.
The code keeps the historical setting name `live_bielik` for the model layer, but it does not require
the Bielik model.

Checking uses the [languages chosen in the app](languages.md). Enchant needs the matching
dictionaries, and LanguageTool needs support for the given language code. With one language, the whole text is
checked as that language. With two, the text is split into sentences; masking the remaining
fragments keeps the error positions. Short and mixed sentences can be ambiguous.

Links, email addresses, mentions, code and recognised names are filtered out to limit false alarms.
The word being typed is not reported straight away. While you edit, the positions of known errors are
shifted, and results that no longer match the new text are discarded. Suggestions from overlapping
layers are merged.

## Model only — experimental mode

In **Writing → Checking** (*Pisanie → Sposób sprawdzania*), choosing **Model only** (*Tylko model*; as on macOS)
replaces the layers above during live checking; the rows of those layers disappear. The default is **Standard**
(*Standardowy*; `live_model_only: false`), and the choice requires **Check while typing** (*Sprawdzaj podczas pisania*)
to be on. The mode uses the model chosen in the **Services** (*Usługi*) tab, e.g. a configured Gemma; it does not
download or choose a model itself.
In this mode the text is not checked by LanguageTool or the local dictionary.
The switches of those layers keep their values for the return to the standard mode.
With checking on in this mode, the **Correct** shortcut and clicking the badge
use the same minimal correction as the underlines. Finished results for fragments
are reused; if a check of the same text is running, the panel waits for its
result instead of sending a second request. A new or changed fragment has to be checked.
This mode uses its own minimal-correction instruction, also in the **Correct** panel.
Custom prompts for ordinary correction, translation and the style modes remain available
in their usual paths; turning the experiment off restores the ordinary **Correct**.

After about 0.8 s without a change to the text, the service splits the checked area into fragments of up to 320 characters.
The area still has a limit of 6000 characters; IBus or the browser may provide a smaller window
or the paragraph at the caret. The model returns the corrected text, and the code compares it with the original
deterministically and works out the ranges of the suggestions. The results have the source **Model**.
A change to the text or the configuration invalidates outdated results and cancels the checks still running for them.
Repeated fragments use an in-memory cache, limited to 256 entries
and 2 MiB of stored data; this cache is not written to disk.
Reuse requires the text and the model and language configuration to match.
A configuration change invalidates earlier results.

For Ollama, the requests of this mode have the model's thinking turned off, without changing the saved setting
for the other features. A model error does not start a fallback check with LanguageTool
or the dictionary. The 0.8 s pause sets when checking starts, not when the underlines appear:
the result depends on the server, the model, the length of the text and whether the cache is hit.
The model can miss an error or propose an unnecessary change, and splitting into fragments limits the context.
Suggestions need to be reviewed before they are applied. The mode is an experiment in the Linux client.

## Reading and replacing text

The engine gets the text from IBus or, through the browser extension, from the page's active field.
An incomplete text window is not treated as the whole document. For a long field, the paragraph
at the caret may be checked; fragments of words cut off at the boundary should not be corrected.

Clicking a suggestion locates the word again and checks that the context is still current.
A replacement through IBus has to be confirmed by the field's new text. In the browser the extension
compares the expected text before editing. A change of focus or content can lead to a refusal
instead of an old suggestion being inserted somewhere else.

Checking is off in, among others, password fields, terminals, and disabled applications and sites.
It is paused during a correction or dictation, and when there is no current context.
Without a panel that can show the results, the engine does not run invisible live checking.

## Position and the suggestion card

AT-SPI provides character positions in supported GTK applications. They are read
in a separate thread; the text and the caret must match the checked field.
The browser provides the geometry through the extension, taking zoom and frames into account.

GNOME draws the underlines in the active window. Hovering opens a card with suggestions,
**Ignore** (*Ignoruj*) and **Add to dictionary** (*Dodaj do słownika*); for a LanguageTool grammar, punctuation or style
rule also **Turn off rule** (*Wyłącz regułę*). Adding a word affects the local Enchant dictionary.
Ignoring affects the word and the rule in the current service; turning a rule off lasts
(see [Dictionary and rules](#dictionary-and-rules)). The logs of these actions do not record the word.
The correction panel offers the same actions under a word of the edited result.

While the model or LanguageTool is being asked, before the number of errors is known, the badge
shows three lavender blobs without a background that merge and separate as in a lava lamp
(on hover: **Checking…** (*Sprawdzanie…*)). With the result, the blobs flow together in about 0.7 s into a brick-coloured
circle with the number of errors inside (from 10 a pill, above 99 **99+**).
When the check found nothing, the blobs merge and disappear. A number that is already known stays
during the next check and only “breathes” slightly; the new number jumps in after the result.
Neither a pause in typing alone nor reading a finished result from the cache starts the animation.
With GNOME animations turned off (Settings → Accessibility), the blobs stand still and the result
appears at once. The badge does not take the keyboard focus.

Without word geometry, the badge remains. Electron applications without the extension may not expose
the AT-SPI needed for underlines. In the browser with the extension, underlines do not depend on this
limitation. The exact scaling limitations are described in the [browser documentation](browser.md).

## Dictionary and rules

**Writing → Dictionary and rules** (*Pisanie → Słownik i reguły*) gathers one dictionary for checking, corrections
and dictation (Linux; macOS keeps its own words in the Dictation tab):

- **Own words** (*Własne słowa*): names, terms and jargon typed by hand, separated by commas.
  They are kept in `settings.json` as `dictation_vocabulary`, the name from when only dictation used them.
- **Added to dictionary** (*Dodane do słownika*): the words added with **Add to dictionary**, kept by Enchant in
  `~/.config/enchant/<language>.dic` (with `ENCHANT_CONFIG_DIR`, there). GNOME apps that use Enchant share these
  lists, so words added there count too. The list shows the latest 100 words; the bin button removes a word from
  the list file. GramaDrama does not use Enchant's own removal, which would also put the word on Enchant's
  exclusion list and mark it as an error in every app.
- **Rules turned off** (*Wyłączone reguły*): the LanguageTool rules turned off from the card, by LanguageTool's
  description of the rule and its identifier. **Enable** (*Włącz*) turns one on again.

Checking does not underline your words: an own word is accepted as written, in capitals, or with a capital first
letter when it is written in lower case, as Enchant accepts its own lists. A correction is told how to spell the
words of the dictionary that its text uses ([prompts](prompts.md#words-from-the-dictionary)), and dictation gets them
in its Whisper prompt ([dictation](dictation.md#speech-recognition)). Enchant reads its list only when the dictionary
is opened, so the checking service opens it again (about 40 ms) when another program changed the file.

A rule turned off is left out of the request to LanguageTool (`disabledRules`) and its issues disappear at once.
It stays off in `rules.json` in the configuration directory until it is turned on again. A rule of spelling is not
offered: a word belongs in the dictionary instead. If `rules.json` is broken, every rule is shown, and the next
change keeps the old file as `rules.json.bak`.

## Code and settings

`engine/live.py` manages the context and the fixes, `liveservice.py` the checking layers,
`dictionary.py` the user's words and the rules turned off, `geometry.py` reading AT-SPI, and
`gnome-extension/live.js` and `underlines.js` the interface.
The process messages are JSON: text, clear, ignore, learn a word, turn a rule off, and results.

| Setting | Default | Meaning |
|---|---|---|
| `live_check` | true | checking while typing |
| `live_model_only` | false | experimental live checking with the configured model only |
| `live_languagetool` | true | sending to LanguageTool |
| `live_bielik` | true | sending to the model |
| `live_underlines` | true | underlines where geometry is available |

## Privacy and verification

The dictionaries and the geometry reading run locally. With the server layers on, the text
of the active field is sent to the configured services after a pause in typing.
The logs keep events, timings, counts and application identifiers, without the checked content.
`rules.json` keeps the identifiers and descriptions of the rules turned off, never the text they were found in.
The **Model only** mode uses the same restrictions for passwords, disabled applications
and sites, and outdated context. The cache holds the text of the fragments and their results
in the service process; not writing to disk does not mean that the model runs locally.

Unit tests: `test_live.py`, `test_liveservice.py`, `test_engine_live.py`, `test_dictionary.py`,
`test_geometry.py`. GNOME integration scenarios check the badge, the position of the underlines,
clicking a fix and the card. Language tests and API mocks do not replace measuring the quality
of the real model and dictionaries in the target environment.
