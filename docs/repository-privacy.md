# Repository prepared for publication

From 2026-10-04 new changes are meant to be suitable for splitting off a future public
repository. We describe product behaviour and reproducible examples. Names of people,
own hosts and domains, account paths, private network topology, logs from everyday
work and screenshots of a private desktop stay out of the repository.

In examples we use `localhost`, `127.0.0.1`, the domains `example.com`,
`example.org`, `example.net` and documentation addresses, e.g. `192.0.2.10`.
Paths are written as `~`, `$HOME`, `${USER}` or `/home/user`. Server configuration
is passed through the app settings or environment variables.
Example tokens must be clearly artificial, e.g. `test-example-token`.

## Check before a change and a commit

From the repository directory:

```sh
python3 scripts/check_repo_privacy.py
python3 -m unittest discover -s scripts/tests -v
```

The first command checks the files tracked by Git and new non-ignored
files in the working tree. It skips deleted files. It reports the path, the line number
and the kind of problem; it does not print the address, home path or secret it found.
A file ignored by `.gitignore` but already tracked is still checked.

An optional hook checks **the content of the Git index**, also with partial staging:

```sh
git config core.hooksPath .githooks
```

This setting applies to the current clone. If the project already has its own hooks,
add `python3 scripts/check_repo_privacy.py --staged` to the existing
`pre-commit` instead of replacing its directory. You can run the same command
manually after `git add`. CI checks the tracked files after every push and in pull requests.
The CI configuration does not by itself turn on branch protection on GitHub.

## What the check detects

- Private and local IPv4/IPv6 addresses, including IPv4 written inside IPv6.
- Account paths under `/home`, `/Users` and Windows user directories.
- Local hosts in URLs and host settings; the `.lan`, `.internal` and `.home.arpa`
  domains also in plain text. An application identifier ending in `.local`
  is not a host by itself.
- Common API key formats, private keys and long literal assignments
  to fields such as `api_key`, `client_secret` or `password`.
- Files that usually contain credentials, e.g. `.env`, `auth.json`, SSH keys
  and private certificates. `.env.example` templates are allowed; their content is scanned.
- Material in `local-private/` and the former archives `linux/research/`, `bench/lab/`,
  also when someone forces them in with `git add -f`. Publishable examples
  are moved to documentation or tests after review and anonymisation.

Binary files are checked for readable strings, e.g. metadata. Symlinks
are checked as stored paths, without reading their target. A file larger than
8 MiB, an unreadable entry, an index conflict or a submodule ends the check with an error;
such resources need a separate review, not silent skipping.

## Synthetic test data

Routing tests sometimes need a real private range. In that
case the data must be made up and must not describe the author's environment. Add a comment
on the same line with the specific rule and an explanation, e.g.:

```python
endpoint = 'http://192.168.42.17'  # privacy: allow[private-ip] synthetic network routing fixture
```

Allowed exception rules: `private-ip`, `home-path`, `local-host`. You can give
several names separated by commas. An exception works on a single line only,
needs a justification and is subject to code review. We do not exclude whole directories
of tests. Secrets and private keys cannot be excluded with a comment; a fixture
should use an explicit placeholder or build an artificial pattern in the test.

## Limits and review before publication

The scanner is a heuristic filter. It does not understand private descriptions of an environment,
does not read text from screenshots, does not unpack archives, does not fetch
Git LFS content and does not detect every kind of secret or privately owned
public domain. Before adding documents, logs, images and examples,
their content and metadata must be inspected. A token found in the history or
sent earlier to a Git server needs an assessment of whether it should be revoked.

The check covers the current files or the index, **not the commit history**.
The future public repository should be created from a separately reviewed export
with a new history. The existing history of this repository must not be published.
Deleting a file today does not remove it from earlier commits.

## CI and local tests

The `.github/workflows/checks.yml` workflow runs the scanner tests, the repository scan,
the Linux unit tests, and the Swift build and tests on a macOS runner. The Linux
tests use local HTTP/socket mocks
and do not need a running model, browser, GNOME session or a real
sign-in server. They need the system Python with GI, the GTK4, libadwaita
and IBus libraries and the Enchant/Hunspell dictionaries; the packages are listed in the workflow.

```sh
bash scripts/test-linux.sh -v
```

The script puts the XDG directories in a temporary directory and detaches the graphical session so that the tests
do not use the app's real settings. The installer tests also substitute paths and system operations.

Graphical-session and integration tests are run separately on a supported
desktop. A green unit test result does not confirm the quality of the panel's
positioning, shortcuts in every application or behaviour with display scaling.
