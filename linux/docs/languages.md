# Text languages and keyboard layout

Text languages are chosen the same way on Linux and on macOS (**Writing → Text languages**, *Pisanie → Języki tekstu*).
Polish as the primary language gives the Polish GramaDrama interface, any other language the English one, on Linux
and on macOS. The keyboard layout (at the end) applies to Linux only.
The text language settings change neither the keys nor the language of the whole system.

## Primary and second language

In **Writing → Text languages** choose the primary language and, optionally, a second one.
The supported values are Polish (`pl`), English (`en`) and German (`de`).
The default set is Polish + English.

| Example | Settings |
|---|---|
| English only | primary `en`, second off |
| German and English | primary `de`, second `en` |
| German only | primary `de`, second off |

In `settings.json` these are `primary_language` and `secondary_language`.
An empty second language (`""`) means one active language. The two chosen languages must differ.

With one language, text is checked in that language. With two, language detection picks
one language of the pair, and short or ambiguous fragments may need the user's judgement.
LanguageTool and the live-checking dictionaries receive the chosen languages.
Local spell checking needs an available Enchant dictionary for `pl_PL`, `en_US` or `de_DE`.
On macOS the system dictionary checks spelling, and the choice is stored as `primaryLanguage` and `secondaryLanguage`
in the app's settings; NaturalLanguage detects the language within the chosen pair, so the result for short
or mixed sentences may differ from Linux.

## Correction, translation and prompts

The default instructions and prompt examples adapt to the active languages.
The Polish + English variant keeps the prompt patterns shared with macOS.
Your own prompt files take precedence: after changing the languages, check that they still fit
the chosen set. The program does not overwrite them.

Translation works between the two configured languages. With one language it is off;
the app says that a second one needs to be added. Reversing the direction does not bring in a third language.

Configuring a language does not mean that any model knows it well. German has support,
rules and test cases, but there is no quality measurement with a real model yet.
Measure [model quality](models.md) and [prompt changes](prompts.md) separately.

## Dictation

The dictation language setting uses the active set of languages.
With one language, the automatic choice also resolves to that language, and Shift
does not switch the recording to another language.

With two languages, Shift picks the other language of the pair relative to the dictation language set:
if the primary one is set, it uses the second; if the second one is set, it goes back to the primary.
In automatic mode, Shift picks the second language of the configured pair.
Automatic mode leaves language detection to Whisper; automatic detection cannot be
limited to the chosen pair. If you need a specific language, choose it explicitly.

## Keyboard layout

The engine can use the Polish, US or German layout.
The choice made at installation is independent of the settings above:

```bash
/usr/bin/python3 linux/install_engine.py install --layout de
```

`--layout` options: `pl`, `us`, `de`. The sources are called
**Polski (GramaDrama)**, **English (GramaDrama)** and **Deutsch (GramaDrama)** respectively.
A new installation without the option uses `pl`; an update keeps the current layout.

Changing the layout requires an update of the IBus description. The installer may print
a `sudo install …` command, after which the installation has to be run again.
See the [installation guide](../README.md#installation).

The [package](../README.md#the-package) registers all three sources for the whole computer. **Switch on for this
account** (*Włącz na tym koncie*) picks the one with the account's first keyboard layout (`pl`, `us` or `de`;
any other layout gets the US one).
