# GramaDrama roadmap

As of **2026-10-07**. The first target of the open-source release is Linux: Ubuntu 26.04, GNOME 50,
Wayland. The set of publication conditions and the evidence that they are met is in the [release plan](docs/open-source-plan.md).

## Available features

- Correction and style modes, a preview of the changes and approval of the result.
- Choice of a primary language and an optional second one (PL/EN/DE) and changing the correction and translation
  shortcuts (Linux and macOS); on Linux also an independent keyboard layout (pl/us/de).
- Translation between the chosen languages (Linux and macOS).
- Dictation: local speech recognition or a server compatible with the OpenAI or whisper.cpp transcription API
  (Linux and macOS).
- On macOS: a transcript preview in the pill and splitting of long recordings at pauses.
- Checking while typing: local dictionary, LanguageTool and the model; badge, list of suggestions,
  underlines in supported fields and a user dictionary.
- App settings, a default correction mode per application and turning off chosen applications and pages.
- The IBus engine and the GNOME extension on Linux, the input method and Accessibility on macOS.
- Shared browser extension: the text of the active field, corrections, dictation and the position of underlines.
- On Linux: configurable service protocols, OIDC sign-in, editable prompts and a benchmark of their quality;
  one dictionary for checking, corrections and dictation, and LanguageTool rules turned off from the card.
- On macOS: the model, LanguageTool and speech recognition on this Mac (with an editable address) or on a server; a model
  server in the Ollama or OpenAI-compatible protocol, with the key in the keychain; Ollama on the Mac with model downloads;
  OIDC sign-in for remote access, as on Linux; editable prompts (the same files as on Linux, without the variant
  benchmark).
- Experimental model-only mode (Linux and macOS, the same prompt): a shared result of background checking
  and of the shortcut correction, a memory of fragment results and an indicator of a running check.
  On macOS, fragments from the caret with results as they come, rejection of a single sentence without holding back
  the rest, and a message about an unreachable server.
- Shared rules for basic correction that limit synonyms and unwanted word changes;
  a benchmark that tells missed errors apart from other changes.

This describes implemented features; it is not a statement of compatibility with every application or every service.
Limitations are in the [Linux guide](linux/README.md) and the [browser description](linux/docs/browser.md).

## Next work before the release

1. Clean the current files of data about private environments and keep automatic checking of new changes.
   Do not rewrite the history of the current repository. Before publication prepare a clean export
   and check the images, archives and generated files as well.
2. Tidy up the user documentation, installation, service instructions and bug-reporting rules.
   Interface texts must describe the configured server and the product's real capabilities.
3. Run the tests automatically and add regression cases where a bug could change text,
   breach privacy or make writing harder. Desktop tests and model measurements remain separate checks.
4. Check installing, updating and removing the app on a clean Ubuntu. Repeat the installation
   with someone outside the project who uses only the instructions.
5. Go through the two-week release-candidate trial and fix serious problems.
6. Before the announcement, define the interest indicators, the threshold for further work and where to publish.

**G3, the extended validation of backends, is deliberately postponed.** The protocol implementation
remains available; tests on further services are not part of the current clean-up of the project.

## Further development

- Verify additional model and transcription backends, including their errors, limits, cancellation and authentication.
- Extend compatibility with applications based on repeated reports and integration tests.
- Measure prompt quality on public case sets, with correct and incorrect texts.
- Check long dictation from a real microphone on macOS, with and without pauses.
- Prepare a separate release path for macOS, including distribution, signing and notarisation.
- Consider a ready-made hosted service only after measuring interest and working out the costs.
  Buying hardware or a paid service is not a condition for using the app with your own backend.
