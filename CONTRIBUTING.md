# Contributing to GramaDrama

The project is preparing its first open-source release on Linux. The current scope is in the
[README](README.md) and the priorities are in the [roadmap](ROADMAP.md).

## Reporting a bug

Open an issue in the repository you use. Include:

- the GramaDrama version or the commit hash;
- the system, the GNOME version and the session type (Wayland/X11);
- the application and the type of field where the bug happened;
- the steps to reproduce, the expected result and the actual result;
- a short, made-up text example if it is needed to reproduce the bug.

Do not attach whole configuration directories, tokens, API keys or private correspondence.
Check logs and screenshots before sending them: they may contain application names, local paths,
addresses or data visible in open windows. Report security issues as described in [SECURITY.md](SECURITY.md).

## Code changes

1. Prepare a change that solves one specific problem.
2. For a bug fix, add a regression case that reproduces the bug before the change.
3. Update the guide when you change behaviour visible to the user.
4. Run the tests relevant to the change and describe the result in the pull request. List the checks
   you could not run and what they need.

Interface texts describe what a feature does and how to fix a problem. Use names such as
“model server” or the name of the model the user chose. Do not assume specific hardware,
networks or deployments. Network examples use loopback or domains reserved for documentation.

## Local checks

From the repository root:

```bash
python3 scripts/check_repo_privacy.py
python3 scripts/check_doc_links.py
bash scripts/test-linux.sh -v
```

The scanner checks the files meant for the repository. The `--staged` option checks the versions staged
for commit. Scope, limitations and examples: [repository protection](docs/repository-privacy.md).
The link check confirms that relative links between documents lead to existing files and headings.

The runner uses the system Python and separate temporary XDG directories, with no connection to the real session.
Unit tests use service stubs and do not need an external model. Some tests need
local sockets, and a sandbox may restrict creating them.
Desktop tests have a separate [GNOME harness](linux/tests/session/README.md); run them in an environment
with the required libraries, without using private browser profiles.

For browser extension changes, run the [test pages](browser/README.md#tests).
For Swift changes, run `./scripts/test.sh` on macOS; the Linux tests do not check the macOS integration.
The prompt benchmark sends cases to the chosen server, so run it deliberately:
[guide](linux/docs/prompts.md#measure-the-change).

## Shared contracts and privacy

The cases in `tests/` and `bench/prompts.golden.json` tie the Swift and Python implementations together.
Changes to the formats and to the extension protocol need to be checked on both sides.
Do not write user content or credentials to logs. Use synthetic data in tests;
do not copy working sessions or dumps of a real configuration.

The project is licensed under GPL-3.0-or-later ([LICENSE](LICENSE)).
Dependency licences are listed in [THIRD_PARTY.md](THIRD_PARTY.md). Propose new dependencies together with
their licence and how they are distributed, and update the list in the same change.
