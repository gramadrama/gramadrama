# Application list and launch fixes

As of 2026-10-04. The user interface is described in the [README](../README.md#applications-and-limitations).
The mechanism spans `gramadrama/apps.py`, the engine adapter and the GNOME extension.

## Identification and settings

GNOME knows the active window and sends the engine its application identifier and kind
(`wayland`, `x11`, `shell` or `none`). This information arrives before the shortcut is handled:
the synchronous key path must not make an extra IPC read.

The extension uses the `pl.gramadrama.Panel1` interface, the `Subscribe` subscription
and the `Focus` signal. Signals are sent to the authorised client.
Disabling the panel or losing the connection invalidates the remembered focus.

Preferences are in `apps.json` in the GramaDrama configuration directory.
Applications not on the list are allowed by default. Turning an application off covers correction,
dictation and live checking; the shortcut reaches the application without being intercepted by correction.
Settings can be changed without restarting the engine.

The list shows recently used and manually added applications. An entry that is turned off, has its own mode
or has a launch fix does not disappear just because it has not been used recently.
The settings for disabled browser sites are in the same file.

## Identifier privacy

An application with a `.desktop` file uses that file's identifier.
A window without such a file may have the identifier `window:<class>`.
For a Chromium web app the identifier keeps the browser and the host name
but drops the path of the page and file. Identifiers can end up in the local log;
do not present them as anonymous.

## Detecting when a fix is needed

A Chromium or Electron application running through X11 may not give IBus the text it needs.
Detection checks the application type, how it is currently launched and the result of a real correction attempt.
A running process alone does not confirm that all fields are supported.

A row's state tells apart confirmed operation, a needed fix, missing data and no support.
A browser with a working extension can provide page text regardless of IBus limitations;
details in the [browser guide](browser.md).

## Creating a launch fix

The fix adds `--ozone-platform=wayland` only after the user presses the button:

- For a standalone application launched by name, a script `~/.local/bin/<name>` may be created.
  The real program is looked up while skipping that directory, to avoid a loop.
- For an application launched by its full path, a copy of the `.desktop` file is created in the user's applications directory.
  The change covers the relevant `Exec` entries.
- A shared runtime, e.g. `electron30`, does not get a global script that would affect other applications.
- Commands wrapped in a shell, sandboxing tools and unsupported formats are rejected
  instead of guessing how to rewrite them.
- Flatpak needs instructions for changing its permissions; the app does not edit them automatically.

A file created by GramaDrama carries the marker `# GramaDrama: `.
A user's own file without the marker is not overwritten. Undoing removes only recognised
GramaDrama files. If the program's path changes, an existing fix has to be repaired.

## Lifecycle

After the fix is saved, the target application has to be restarted. GNOME may remember the old `.desktop`
entry in the app grid until you log in again; this also applies to undoing a fix.
Installing a newer GramaDrama keeps the fixes, and `install_engine.py uninstall` removes them.

## Tests

`tests/test_apps.py` covers the application catalogue, parsing of launch commands, detection,
the plan and the ownership of created files. `tests/test_install_engine.py` checks that only
GramaDrama's own integrations are removed. The engine tests check that operations are refused for a disabled application.
The GNOME integration tests need a separate environment and confirm the behaviour of real launchers.
