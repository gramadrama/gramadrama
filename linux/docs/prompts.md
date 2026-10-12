# Prompts: change them without touching the code

A request to the model consists of instructions, the mode's task, rules, examples and a message
template with the user's text. Your own files in `~/.config/gramadrama/prompts/` replace
selected pieces. With `XDG_CONFIG_HOME` set, the directory is under it.

The default prompts depend on the [text languages](languages.md). Polish + English keeps
the templates shared with macOS, checked against `bench/prompts.golden.json`.
Other profiles have instructions and examples suited to the active languages.
This consistency does not cover your own files or the output the model generates.

On macOS the same files are in `~/Library/Application Support/GramaDrama/prompts/` (reached the same way in the app,
with **This Mac** (*Ten Mac*) instead of **This computer** (*Ten komputer*)), and the commands are
`GramaDrama --prompts export|status [directory]` and `--prompts list`.
macOS has no language profiles: the files adjust the PL+EN prompts.

## Rules

- A valid file that differs from the base template takes precedence over the matching default piece.
  An unchanged base file is not treated as an override, so the language profile is still chosen.
- An app update does not overwrite the user's files.
- Deleting a file restores the default piece for the selected language profile.
- An invalid file, e.g. invalid UTF-8, empty text or a missing required placeholder,
  is skipped with a diagnostic message. The correction then uses the default piece.
- A single trailing newline added by an editor is ignored.
- Changing the languages does not rewrite the user's files. Check that your own instructions and examples
  still fit the new profile.

## Getting started

In **Services → Language model → Advanced → Prompts → Open folder**
(*Usługi → Model językowy → Zaawansowane → Prompty → Otwórz folder*) the app writes the missing files for editing,
without overwriting existing ones. The export writes the base PL/EN templates that match macOS.
Leaving them unedited does not block the DE profile or any other set of languages.
From the `linux/` directory these commands are available:

```bash
python3 -m gramadrama.prompts export
python3 -m gramadrama.prompts list
python3 -m gramadrama.prompts status
```

Keep only the files you want to adjust. The other pieces will use the current
defaults. The engine reads the files at each correction, so no restart is needed.
`status` shows your own and skipped files; when there is a problem it returns exit code 1.

## Pieces

| File | Use | Required placeholders |
|---|---|---|
| `intro.txt` | the model's role | — |
| `task-fix.txt`, `task-improve.txt`, `task-formal.txt`, `task-casual.txt`, `task-shorter.txt` | the task of each mode | — |
| `rules.txt` | shared rules, including protecting links and code | — |
| `last-rule-fix.txt`, `last-rule-rewrite.txt` | the final rules of a mode | — |
| `user.txt` | the message with the text; optionally `{language}` | `{text}` |
| `examples.json` | correction examples: `text`, `language`, `output`, up to 40 entries | — |
| `translate.txt` | the translation instruction | `{target}` |
| `translate-user.txt` | the text to translate | `{target}`, `{text}` |
| `translate-examples.json` | translation examples; `language` means the target | — |
| `names.json` | names of the languages and translation targets | — |
| `cleanup.txt`, `cleanup-user.txt`, `cleanup-examples.json` | dictation cleanup | `{text}` in `cleanup-user.txt` |

The example codes are `pl`, `en`, `de`, and `mixed` for correcting mixed-language text.
In `names.json` the `language` group contains `pl`, `en`, `mixed`, and the `target` group contains `pl`, `en`.
Both may also contain `de`; without it `German` is used, to stay compatible with older files.
An exported file shows the full supported shape.

Placeholders are replaced before the user's text is inserted. A `{language}`
in the message being corrected stays text, not another placeholder to expand.
German is chosen in the language settings; there is no need to replace the `pl` code with a German name.

## Words from the dictionary

On Linux a correction is told how to spell the words of your [dictionary](live-check.md#dictionary-and-rules)
that its text uses: a line `Spell these names and terms exactly like this: …` comes before the last user message,
after the examples, so the part of the request the model server keeps cached stays the same. Only words that are
already in the text are named, up to 20; a translation gets none, because it may translate them. The experimental
model-only checking adds the same kind of line, in Polish, before its fragment. The dictation cleanup has always
had the own words in its system prompt; it now gets the words added to the dictionary too.
These lines are not pieces of the prompt files. macOS does not add them yet.

## Measure the change

From the `linux/` directory:

```bash
python3 tests/bench.py ../bench/cases.json --prompts default
python3 tests/bench.py ../bench/cases.json --prompts ~/prompt-variant
python3 tests/bench.py ../bench/cases.json
python3 tests/bench.py ../bench/translate.json --mode=translate --prompts ~/prompt-variant
```

`default` ignores your own files, a directory points to a separate variant, and without the option the current files are used.
The report gives exact matches, false alarms, time and tokens per second.
Do not change the model, the case set and the prompts at the same time if you want to judge the prompt alone.

Look at the particular sentences whose result changed. Your own set has the form
`[{"lang": "de", "input": "...", "expected": "..."}]`.
For a new language, also prepare correct sentences that the model should not rewrite.
The existing PL/EN sets are no proof of the quality of the German profile.

The benchmark sends only the cases from the file to the configured server. The results may contain
the server's address and the text of the cases: remove private data before publishing a report.
More on comparing models: [benchmarks](models.md).
