# GramaDrama

**Write without the drama.** GramaDrama corrects and translates text in the text fields of your
applications. It shows the proposed changes before inserting them, checks spelling while you type
and lets you dictate. The language model runs at an address you choose: on your computer,
on your own server or with an external service.

The project is being prepared for its first open-source release on **Linux**. The target environment
is **Ubuntu 26.04, GNOME 50, Wayland**. The macOS code lives in the same repository, but its public
distribution needs separate validation. Other environments have no confirmed support yet.

The interface follows the primary text language on Linux and on macOS: Polish gives a Polish interface,
any other language an English one. This documentation uses the English labels and gives the Polish ones
in parentheses where they first appear. The macOS permission prompts stay in Polish.

## Get started on Linux

1. Build the package and install it. There is no published release yet, and a fresh Ubuntu desktop lacks only
   `git` for the build; apt installs everything else, the Polish and English dictionaries included:

   ```bash
   sudo apt install git
   git clone https://github.com/gramadrama/gramadrama.git
   cd gramadrama
   scripts/build-deb.sh
   sudo apt install ./build/gramadrama_*_all.deb
   ```

2. Start **GramaDrama** from your applications, click **Switch on for this account** (*Włącz na tym koncie*) in
   **Home → First steps** (*Start → Pierwsze kroki*), then log out and log in again so that GNOME loads the panel.
3. In **GramaDrama → Services** (*Usługi*) choose the model: Ollama on this computer, or a server
   with an address, a protocol and a model name (the same choice as on macOS).
   [Service setup](docs/self-hosting.md) covers the LLM, the optional LanguageTool and speech recognition.
4. In **Home → Try it** (*Start → Wypróbuj*) type some text, press **Ctrl+Alt+G**, check the proposal
   and accept it with **Enter**.

Installation, updates, removal and limitations are covered by the [Linux guide](linux/README.md#installation),
which also describes the installer for working on the code. The package was tested on a clean Ubuntu 26.04 in a
virtual machine; an installation by someone outside the project is still a release condition.

## What to install

GramaDrama downloads and installs nothing on its own: every download below starts with a click.

Required:

- **The app.** Linux: the [package](linux/README.md#the-package). macOS: there is no signed download yet, so build it
  from this repository ([building for macOS](#building-for-macos-developers)); it needs macOS 14 or later, Apple's
  developer tools and the Accessibility permission.
- **A language model.** [Ollama](https://ollama.com/download) on this computer, or a server with the Ollama protocol
  or an OpenAI-style chat API ([service setup](docs/self-hosting.md)). With Ollama, the **Pull a model**
  (*Pobierz model*) row in **Services → Language model** (*Usługi → Model językowy*) downloads a model; the recommended
  one is `gemma4:e4b` (about 6.6 GB), and on macOS **Get Ollama…** (*Pobierz Ollamę…*) opens Ollama's download page.

Optional:

- **LanguageTool**, for grammar rules while typing and the explanations in the list of changes: a LanguageTool server
  ([setup](docs/self-hosting.md#languagetool)). Without it everything else works; see
  [services](#services-prompts-and-privacy).
- **Dictation**, one of two ways, chosen in **Services → Speech recognition** (*Usługi → Rozpoznawanie mowy*);
  on macOS both need the microphone permission:
  - **On this computer** (*Ten Mac*; on Linux *Ten komputer*): Whisper runs here, so its model is downloaded here. On
    macOS with **Download (1.6 GB)** (*Pobierz (1,6 GB)*) in that group or in **Dictation** (*Dyktowanie*); on Linux
    with the `install_engine.py tools` command, which the app copies from that group; it needs an NVIDIA graphics
    card.
  - **On a speech server** (*Serwer*): Whisper, or another speech model, runs on a server you set up, and the model is
    installed there; nothing is downloaded on this computer. You enter the server's address and protocol
    ([speech recognition](docs/self-hosting.md#speech-recognition)).
- **The GramaDrama keyboard** (macOS, recommended): the input source *Polski (GramaDrama)*, which
  `./scripts/build-app.sh` installs with the app when the signing certificate exists
  (`./scripts/create-signing-cert.sh`). Turn it on in **Home → First steps** (*Start → Pierwsze kroki*) or
  **Apps → Integrations** (*Aplikacje → Integracje*) with **Turn on…** (*Włącz…*) and allow it in System Settings;
  it types in the layout you had when you turned it on. While it is selected, ⌥⌘G reads and replaces text without
  Accessibility or the clipboard in macOS apps, Mail and the new Outlook, and dictation inserts text through it almost
  everywhere, browsers and Slack included, never in terminals. Without it both work through Accessibility and the
  clipboard. Apps that were open before the installation may not see it until they are restarted.
- **The browser extension**, for text fields in web pages: [installation](browser/README.md#installation).

## Features

| Feature | Linux — default shortcuts | macOS — default shortcuts |
|---|---|---|
| Correct, Improve, Formal, Casual, Shorter | Ctrl+Alt+G | ⌥⌘G |
| Translation | Ctrl+Alt+J, between the chosen languages | ⌥⌘T, between the chosen languages |
| Dictation | hold the right Ctrl | hold Fn / 🌐 |
| Checking while typing | local dictionary, LanguageTool and the model | local dictionary, LanguageTool and the model |
| Editing fields on web pages | shared browser extension | shared browser extension |

On both systems you choose a primary language and an optional second one from Polish, English and German,
e.g. English only, or German + English. With a single language, translation is off.
You change the [text languages](linux/docs/languages.md) in the **Writing** (*Pisanie*) tab.
On both systems the correction and translation shortcuts change in **Home → Shortcuts → Change shortcuts**
(*Start → Skróty → Zmień skróty*): on Linux Ctrl+Alt, Ctrl+Shift or Ctrl+Alt+Shift with a letter, on macOS at least
two of ⌃ ⌥ ⌘ (⇧ optional) with a letter, recorded from the keyboard.
**Enter** applies the proposal, **Esc** cancels it. The model can change the meaning or miss an error;
read the result before applying it. Compatibility depends on the application and on how it exposes text.
Password fields, terminals and applications turned off in the settings are skipped.

On Linux the IBus engine and the GNOME panel handle supported fields without the clipboard. The browser
extension provides the page's text, its position and replacements. See [browser support](linux/docs/browser.md).
On macOS the app uses the input method, the extension, Accessibility or the clipboard, depending on the field.
Some editors may lose formatting when the text is replaced.

**Writing → Dictionary and rules** (*Pisanie → Słownik i reguły*) keeps one dictionary for checking,
corrections and dictation: your own words and the words added with **Add to dictionary**. It also lists the
LanguageTool rules turned off with **Turn off rule** on the card under an underlined word, and turns them on again.
On Linux the added words are Enchant's lists, shared with GNOME apps; on macOS GramaDrama keeps its own list of the words
it added to the macOS dictionary and also reads the words the system lists as learned in other apps.

Experimental model-only checking (Linux and macOS) shares its result with the **Correct** mode:
the shortcut uses a finished correction or waits for a running check of the same text.
The error badge shows an indicator while the server is being asked. Both platforms use the same
minimal-correction prompt. On macOS the model checks sentences from the caret and shows results as they
come, a rejected answer for one sentence does not hold back the others, and an unreachable server is
shown on the badge. Linux details: [checking while typing](linux/docs/live-check.md).

On macOS the dictation pill shows a partial transcript and a word count when Whisper on the Mac
recognises speech. The full recognition goes into the field when you release the key. Long recordings
are split at pauses to lose less text at the boundaries of the recognition windows.

## Services, prompts and privacy

Linux and macOS support the Ollama protocol and the OpenAI-style chat API. LanguageTool is optional.
Dictation can use a local Whisper (on Linux with an NVIDIA graphics card, on macOS through WhisperKit)
or a server of your choice with the OpenAI or whisper.cpp API. The choice and its settings are in
**Services → Speech recognition** (*Usługi → Rozpoznawanie mowy*).

**Only the language model is required.** The recommended one is `gemma4:e4b` (Gemma 4, about 6.6 GB through
Ollama); any model the app can reach works. Without LanguageTool:

- correcting and translating work the same, but the list of changes has no explanations, which come from
  LanguageTool's rules;
- checking while typing uses the system's spelling dictionary at once and the model after a pause of about 3 s
  (texts up to 1500 characters) instead of grammar rules after 0.8 s; the experimental **Model only** mode
  (*Tylko model*) leaves the model alone.

Dictation is optional as well, and recognition runs one of two ways. The recommended speech model on both is Whisper
large-v3-turbo.

- **On this computer:** always Whisper large-v3-turbo, with its model on this computer. On macOS it runs through
  WhisperKit; the model (about 1.6 GB) downloads only with **Download**, then loads in the background while dictation
  is on, and its first load prepares it for the Neural Engine, which takes a few minutes. On Linux it needs an NVIDIA
  graphics card.
- **On a speech server:** the model is installed on the server, and nothing is downloaded on this computer. The server
  can run Whisper large-v3-turbo or another Whisper version or speech model, as you prefer: with the OpenAI API you
  enter the model's name in the settings, and whisper.cpp uses the model the server was started with.

On both systems the **Services** tab offers the same choice for the model, LanguageTool and speech
recognition: **This computer** (*Ten komputer*; on macOS *Ten Mac*) or **Server** (*Serwer*) with an address,
a protocol and an optional API key (on macOS in the keychain, on Linux in a settings file readable only
by its owner). The model on this computer is Ollama: the app shows whether it runs and downloads the chosen
model with a progress bar. On both systems the address of the service on this computer can be changed
(e.g. another port); on macOS an empty LanguageTool address means the first of the ports 8010 and 8081 that
answers. The preview in the pill works only with speech recognition on the Mac. There is no default project
server: without settings, both versions use services on this computer.

Text goes to the configured model and LanguageTool when you correct it and while background checking is on.
The local dictionary sends nothing. On Linux a correction names the dictionary words its text already contains,
and with recognition by a server the Whisper prompt with your dictionary words goes to that server. With local speech recognition the recording stays on the computer;
with recognition by a server the recording is sent to that server. The operator of the chosen service
and any proxy that terminates TLS may have access to the data sent.

The app sends no telemetry. Local logs contain events, timings, lengths and application identifiers,
not the corrected text or API keys. Details and where data is kept:
[privacy on Linux](linux/README.md#privacy).

On both systems the prompts can be changed with files: **Services → Language model → Advanced → Prompts**
(*Usługi → Model językowy → Zaawansowane → Prompty*) opens their folder (Linux: the configuration directory,
macOS: `~/Library/Application Support/GramaDrama/prompts/`). Updates do not overwrite them. On Linux a benchmark
compares the effect of a change: [prompts guide](linux/docs/prompts.md).

## Development and tests

- [Reporting bugs and proposing changes](CONTRIBUTING.md).
- [Reporting security issues](SECURITY.md).
- [Roadmap](ROADMAP.md) and [first release conditions](docs/open-source-plan.md).
- [Changelog](CHANGELOG.md); Linux and macOS share the version number in `VERSION`.
- [Running the Linux tests](linux/README.md#documentation-and-tests).
- [Building the Linux package](linux/docs/architecture.md#package): `scripts/build-deb.sh`.
- [Browser extension tests](browser/README.md#tests).
- [Shared panel design](branding/PANEL.md).

The macOS interface code is in `Sources/GramaDrama`, the logic in `Sources/GramaCore`, the service client
in `Sources/GramaBackend`, and the input method and its protocol in `Sources/GramaIME*`.
The Linux version is in `linux/gramadrama`; shared test cases are in `tests/` and `bench/`.

### Building for macOS (developers)

You need macOS 14 or later and Apple's developer tools with Swift package support.
These commands get the repository and install a local build of the app and the input method:

```bash
git clone https://github.com/gramadrama/gramadrama.git
cd gramadrama
./scripts/create-signing-cert.sh
./scripts/build-app.sh
open ~/Applications/GramaDrama.app
```

Give GramaDrama the **Accessibility** permission in macOS Privacy & Security settings. Dictation needs
microphone access and the setting “Press 🌐 key to: Do Nothing”. `./scripts/test.sh` runs the logic tests.
The `--e2e` and `--e2e-ime` tests drive the desktop and need a separate test environment.

`scripts/release-check.sh --level=1` checks the logic, level 2 adds the configured models,
and level 3 installs the app and drives the desktop after a separate confirmation. The correction benchmark
tells missed errors apart from unwanted word changes; its thresholds are in `bench/release.json`.
`--transcribe <file.wav> --stream` checks the dictation preview with an audio file.

The input method is managed with the `--ime-enable`, `--ime-status`, `--ime-disable`
and `--ime-uninstall` options of `~/Applications/GramaDrama.app/Contents/MacOS/GramaDrama`.
`--ime-disable` switches to the plain layout, and `--ime-uninstall` removes the input method bundle.
macOS changes must be checked on macOS; the Linux tests do not cover them.

## License

GramaDrama is licensed under **GPL-3.0-or-later**: the GNU General Public License version 3
or, at your option, any later version. The full license text is in [LICENSE](LICENSE).
Dependencies, services and models have their own terms ([list](THIRD_PARTY.md)). Model weights are not
part of the repository; you download them and check their license yourself.

The license covers the code, not the name and the logo. You may use them to refer to this project; a modified
version that you distribute needs another name and logo, so that people can tell it from the original.
