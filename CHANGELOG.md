# Changelog

Notable changes to GramaDrama. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/). Linux and macOS share one
version number, kept in the `VERSION` file.

## [Unreleased]

The first public release. Its target is Linux: Ubuntu 26.04, GNOME 50, Wayland. The macOS code is in the same
repository, but its public distribution (signing, notarisation) needs separate validation. Compatibility depends
on the application and on how it exposes text; see the [Linux guide](linux/README.md).

### Linux

- Correction in five modes (Correct, Improve, Formal, Casual, Shorter) and translation between the chosen text
  languages. The panel shows the corrected text itself as an editable field, with what changed on a light green
  background; the list of changes, with the reason for each, opens on demand (Ctrl+I).
- Editing the result in the panel before it goes in, just by typing or clicking in it: undo one change, keep a word
  and add it to the dictionary, copy, cut and paste; the edited result is checked again.
- An IBus engine and a GNOME Shell extension that read and replace text in supported fields without the clipboard.
- Checking while typing: local dictionary, LanguageTool and the model, with a badge, a list of suggestions,
  underlines in supported fields and a user dictionary. Model-only checking is experimental.
- Dictation while holding the right Ctrl: local Whisper, or a server with the OpenAI or whisper.cpp
  transcription API. The microphone opens at the key-down, and the indicator says when it is actually
  hearing, so the first words are not lost while a suspended USB microphone wakes.
- Services: Ollama on this computer, or a server with the Ollama protocol or the OpenAI-style chat API and an
  optional API key. LanguageTool is optional. There is no default project server.
- A failed correction says what happened and what to do: a title and one sentence, and buttons by the cause (open
  the app on the right tab, try again, sign in); Enter does the first one. A slip of the model (a broken stream, an
  empty or rejected answer) is retried once on its own first. A new install is told whether Ollama is missing, not
  running or has no model chosen.
- Remote access to your own servers through an OpenID Connect sign-in and a gateway.
- Prompts as editable files, and a benchmark that compares prompt variants on your own server.
- Text languages: a primary and an optional second one from Polish, English and German. The interface is Polish
  or English, following the primary language. The correction and translation shortcuts can be changed, and the
  keyboard layout (pl/us/de) is independent of the text languages.
- One dictionary for checking, corrections and dictation (Writing → Dictionary and rules): the own words and the
  words added with "Add to dictionary" (Enchant's lists, shared with GNOME apps). Checking does not underline them,
  a correction is told how to spell the ones its text uses, and dictation puts them in the Whisper prompt within its
  length limit. Words can be removed from the list.
- "Turn off rule" on the card of a LanguageTool grammar rule, in the underlines and in the correction panel: the rule
  stays off until it is turned on again in the same section.
- GramaDrama can be turned off for chosen applications and web pages, and each application can have its own
  default correction mode.
- A browser extension, shared with macOS, for the text of fields on web pages, its position and replacement.
- A settings window (Home, Writing, Dictation, Apps, Services, Privacy) that checks each component and says how
  to fix what is missing.

### macOS

- The same correction modes, translation, text languages, editable shortcuts and editing in the panel, through the
  input method, the browser extension, Accessibility or the clipboard, depending on the field. The panel shows the
  corrected text itself as an editable field, as on Linux; the list of changes opens on demand (⌘I).
- A failed correction says what happened and what to do, with the same causes and buttons as on Linux (open the app on
  the right tab, try again, sign in; ↩ does the first one), and a slip of the model is retried once on its own first.
  When Ollama on this Mac does not answer, the panel says whether it is missing or not running.
- Dictation while holding Fn / 🌐: Whisper on the Mac with a live preview in the pill and long recordings split at
  pauses, or a server.
- Services on this Mac or on a server, Ollama with model download, the API key in the keychain, remote access
  sign-in and editable prompts (the same files as on Linux).

### Notes

- No telemetry. Text goes only to the services you configure; see the privacy section of the [README](README.md).
- Versions up to 0.5.2 were private development builds and have no public release notes.
