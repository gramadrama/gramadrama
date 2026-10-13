# Dictation on Linux

Hold the **right Ctrl** (*prawy Ctrl*), speak and release: the text goes in at the cursor.
It works in fields supported by the active GramaDrama engine or by the browser extension.
Local Whisper keeps the recording on the computer; choosing a transcription server sends the recording to it.

## Controls and language

Holding the right Ctrl for 0.3 s starts a dictation; releasing it stops recording.
Any other key that makes a shortcut cancels the recording, so that e.g. Ctrl+Shift+T still works in the application.
While a correction is active, the right Ctrl does not start dictation.

The microphone opens as soon as the right Ctrl goes down, so what you say as the indicator appears is recorded.
A microphone that PipeWire has suspended can take a moment to wake: a USB webcam's microphone may need about
2 s, while a headset or a built-in one usually starts in about 0.15 s. Until the first audio arrives the indicator says
*Budzę mikrofon…* (*Waking the mic…*) with a spinner; speak once it says *Słucham…* (*Listening…*).
Releasing the key before that gives a message instead of an empty result.

Languages are chosen in **Writing → Text languages** (*Pisanie → Języki tekstu*). With two languages, Shift switches
the current dictation to the other language of the pair, relative to the set language; with one language it does not change the language.
With one language, **Automatic** (*Automatycznie*) uses that language. With two, it leaves recognition
to Whisper, which may also detect a language outside the pair. Details: [languages](languages.md).

The recording is 16 kHz, mono. A very short clip is dropped, silence gives a message,
and recording stops by itself after 180 s. The indicator shows recording and the processing steps that follow.

## Speech recognition

In **Services → Speech recognition** (*Usługi → Rozpoznawanie mowy*) choose where the recording is processed
(the **Services…** (*Usługi…*) button of the **Dictation** (*Dyktowanie*) tab opens that group, ringed):

- **This computer** (*Ten komputer*) runs a local faster-whisper. The `linux/install_engine.py tools` command
  downloads a separate Python environment, the CUDA libraries and the Whisper large-v3-turbo model.
  This path needs a compatible NVIDIA graphics card and driver. Nothing is downloaded on its own: without the
  tools, holding the key shows „Brak modelu mowy” (*No speech model*) on the pill and, as on macOS, opens the app on
  **Services → Speech recognition** with the group ringed; its Whisper row copies the command.
- **Server** (*Serwer*) sends the recording to the chosen service. Choosing it shows the server's rows: its base
  address, the **Protocol** (*Protokół*), the model for the OpenAI API and an optional key.
  This needs no local model and no local NVIDIA card; the server must already provide a compatible API.

The **OpenAI compatible** (*Zgodny z OpenAI*) protocol uses
`POST {base_address}/audio/transcriptions` with a WAV file, the model name, the language if it was chosen
explicitly, and an optional Bearer key. **whisper.cpp** uses the native `POST /inference`
and `GET /health`; the model is set by the server's configuration. Example addresses and details:
[own services](../../docs/self-hosting.md#speech-recognition).

Switching to **This computer** does not erase the server's address, model, key or protocol.
When you choose **Server** again, the kept settings are used. Choosing a server without an address
ends with a message about the missing configuration, without falling back to local Whisper.

In `settings.json` the `whisper_backend` field takes `"local"` or `"server"`.
The default `"auto"` keeps the earlier behaviour: a non-empty `whisper_url` selects the server,
an empty one selects the local model. An explicit choice in the app replaces this rule.
`whisper_api` is `"openai"` (the default) or `"whispercpp"`.

The words of the [dictionary](live-check.md#dictionary-and-rules) help Whisper recognise names and terms:
the own words typed in **Writing → Dictionary and rules** (*Pisanie → Słownik i reguły*), then the latest words added
with **Add to dictionary** (*Dodaj do słownika*) while the list stays under 300 characters, because Whisper reads only
the end of a long prompt. The **Dictation** tab links to them with **Writing…** (*Pisanie…*), which rings that
section. The words can also affect the detected language, so check a change with a short recording in the target
language.

## Cleanup and insertion

Local rules filter out common transcription artefacts, fillers and the way times are written.
**Cleanup by the model** (*Porządkowanie modelem*) is experimental and off by default.
The **when needed** (*gdy trzeba*) and **always** (*zawsze*) modes send the transcript to the configured LLM.
A timeout or a rejected result keeps the original transcript.

The engine ties the result to the field that was active when recording started. The result must not be inserted
twice, or into another field after the focus has changed. When the text cannot be inserted safely, the interface
reports the result; in the cases provided for, the text goes to the clipboard.
The spacing is chosen to fit the text at the cursor.

## Architecture

- `engine/dictate.py`: the key's state machine and the decisions about insertion.
- `gramadrama/speech.py`: a separate process for recording through `pw-record`, recognition and cleanup.
- `gramadrama/dictation.py`: pure rules for processing the transcript.
- `gnome-extension/hud.js`: the status indicator, which does not take the focus.
- `engine/browser_text.py`: insertion into the active field of a web page through the extension.

The processes exchange JSON messages with a recording ID: start (at the key-down), confirm (the hold
became a dictation), language change, stop and cancel, and the state (first audio included), the microphone
level, the result or an error.
Late replies and duplicate results are discarded.
The local model stays in memory between recordings; the unused service exits after a period of inactivity.

## Privacy and tests

The recording is processed in memory; GramaDrama does not write it to a file.
Because the microphone opens at the key-down, a shortcut with the right Ctrl or a short tap also opens it
for a moment; GNOME may show its microphone indicator then. That clip is discarded unprocessed, and the
model's clean-up is not contacted until the hold has lasted 0.3 s.
With recognition by a server selected, it leaves the computer. Merely keeping the server's address
in the settings while **This computer** is selected does not send the recording to it.
Cleanup by the LLM sends only the text, with the same words of the dictionary as the Whisper prompt.
With recognition by a server, that prompt goes to the server with the recording.
The log contains the outcome of the operation, lengths and timings (including how long the microphone took
to give audio, `mic_ms`), the language and the application ID, not the transcript.

`tests/test_dictation.py` checks the rules, `tests/test_engine_dictate.py` the controls and insertion,
and `tests/test_speech.py` the service with stubs. Scenarios in an isolated GNOME session check
the keys and the indicator with a test service and no microphone. These tests do not measure the recognition quality
of real speech or the quality of the models for all languages.
