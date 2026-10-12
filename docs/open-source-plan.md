# Preparing the open-source release

As of **2026-10-12**. The first release covers Linux (Ubuntu 26.04, GNOME 50, Wayland).
The project lets you choose your own services and change the prompts; a ready-made hosted service is a possible later stage.

## Gate status

| Gate | Status | What remains |
|---|---|---|
| G1. Rights and licence | GPL-3.0-or-later in `LICENSE`, dependencies in `THIRD_PARTY.md` | keep the list current when dependencies change |
| G2. Repository privacy | clean history since 2026-10-12 (a reviewed export, images checked by eye); checks in CI and in local hooks | make the repository public |
| G3. Independent backends | **deliberately postponed** | separate validation of at least three backends; does not block the current clean-up work |
| G4. Self-service installation | installer and guide available | clean Ubuntu, test by someone outside the project, installation/update/removal |
| G5. Editable prompts | **implemented on Linux and macOS** (the same files; variant benchmark on Linux only) | maintain the tests and documentation |
| G6. Documentation and collaboration | documentation in English, licence list, changelog | instructions checked by a new user |
| G7. Release quality | tests and CI running remotely (confirmed 2026-10-07) | two-week trial of a release candidate |
| G8. Measuring interest | to be decided before the release | indicators, threshold for the next stage, announcement channels |

## G2. Scope of the clean-up

We remove private addresses, accounts, host names, deployment configurations,
research logs and descriptions of the owner's environments from the current files. Documentation uses `localhost`,
loopback addresses and example domains. Application and extension identifiers stay stable.

`python3 scripts/check_repo_privacy.py` scans the current files; the `--staged` mode
checks the version prepared for the commit. Scope and limits are described in
[repository protection](repository-privacy.md).

This repository started on 2026-10-12 from a reviewed export of the earlier development repository, with
a new history; the exported images were checked by eye. Removing text from the current files does not remove it
from older commits, so the hooks check every commit and its message before it leaves the computer. Text scans
are complemented by a manual review of images, archives, attachments and every new file.

## G3. Backends — postponed

Linux lets you set an LLM in the Ollama or OpenAI protocol, LanguageTool and a transcription server
in the OpenAI or whisper.cpp protocol. It needs no default addresses or a particular model.
Trials of Ollama and llama.cpp are documented;
compatibility of other services does not follow from the compatibility of their API names alone.

Returning to this gate needs a test matrix: correction, translation, model list, streaming,
API key, cancellation, context-limit errors and transcription. Postponing it does not mean it has passed.

## G4. Installation

A person on a clean Ubuntu is to make a first correction using only the README. The trial also covers
an update, logging in again after a change to the GNOME extension, no server, and uninstalling.
It must be described what uninstalling removes and which settings and downloaded models remain.
We do not declare this trial finished without a recorded result and feedback from someone outside the project.

## G5. Prompts

Your own files in the configuration directory replace the default prompt fragments. Updates do not
overwrite them; a faulty file gives diagnostic information and a safe fallback to the default fragment.
The benchmark on Linux takes a prompts directory or the default variant. macOS reads the same files
(`PromptFiles.swift`) from its own directory. Guide: [prompts](../linux/docs/prompts.md).

## G6. Documentation

The README is to describe capabilities, limitations and data flow. Instructions are required for installation,
[service setup](self-hosting.md), prompts, bug reports, security and contributing.
Licences of dependencies and of the proposed models are listed in [THIRD_PARTY.md](../THIRD_PARTY.md); models are not part of the repository.

## G7. Tests and release trial

Automatic tests should run without access to private services. Coverage includes the logic,
the safety of text replacement, configuration, the installer and the service protocols against mocks.
Desktop integration tests and model benchmarks have separate requirements and are not replaced by unit tests.
A CI result does not prove compatibility with the whole desktop. A release candidate needs two weeks of daily
use without serious problems.

The `.github/workflows/checks.yml` configuration runs the privacy check, the link check between documents,
the Linux tests and the Swift tests on macOS. Remote runs on GitHub work: on 2026-10-07 all three jobs
(`privacy`, `linux-unit`, `macos-build-and-unit`) succeeded.
The local Linux suite is run with `bash scripts/test-linux.sh -v` in isolated XDG directories.

## G8. Decision on further work

Before the release, decide what we measure (e.g. reports from new users, repository stars,
interest in a ready-made service), the threshold for the next stage and where to announce it. After the release, set aside
4–6 weeks for observation. A paid service and additional hardware need a separate decision.
