# Linux architecture

How the Linux version of GramaDrama is built: its processes, the rules that keep the user's text
safe, the contract between the IBus engine and the GNOME Shell extension, the installer and the
tests. It is written for contributors. Installation and everyday use are in the
[Linux README](../README.md); the panel's design, shared with macOS, is in
[branding/PANEL.md](../../branding/PANEL.md).

The supported target is GNOME Shell 50 on Wayland (`metadata.json` declares only `50`). Apps
running under Xwayland are reached through IBus too. Other GNOME versions, distributions and
desktops are untested. The panel design and the browser extension are shared with macOS; the IBus
engine, the GNOME Shell extension, the settings app and the installer described here are
Linux-only.

## Components

| Component | Code | Runs as | Responsibility |
|---|---|---|---|
| IBus engine | `linux/gramadrama/engine/`, launcher `linux/gramadrama-engine` | one Python process started by ibus-daemon with `--ibus` | keys, field state, correction sessions, applying and verifying text; drives every other part |
| Correction backend | `engine/backend.py` → `gramadrama/core.py`, `prompts.py`, `engine/textdiff.py` | one child process per correction, plus a short warm-up child | model request and streaming, post-processing, word diff, change list, LanguageTool reasons |
| Live-check service | `gramadrama/liveservice.py` with `live.py`, `spell.py`, `model_live.py`, `geometry.py` | long-lived child of the engine | spelling (Enchant), LanguageTool, the model layer; word boxes and caret confirmation through AT-SPI |
| Speech service | `gramadrama/speech.py`, `dictation.py` | long-lived child; in the speech environment's Python when the built-in Whisper is used | recording, transcription, transcript clean-up |
| GNOME Shell extension | `linux/gnome-extension/` | inside gnome-shell | correction panel, message toast, dictation pill, live-check badge, underlines; reports the focused app |
| IBus popup | `engine/fallback_ui.py` | inside the engine | plain-text fallback when the extension is missing or cannot show something |
| Settings app | `gramadrama/app.py`, `gramadrama/window/`, `checks.py`, `apps.py`, `oidc.py`; launcher `linux/gramadrama-app` | GTK 4 / libadwaita app, only while open | status checks, `settings.json`, `apps.json`, prompts, launch fixes, sign-in to servers |
| Browser host | `gramadrama/browserhost.py`, launcher `linux/gramadrama-browser-host` | started by the browser for each extension connection | relays native-messaging frames to the engine's socket |
| Browser extension | `browser/extension/` (shared with macOS) | inside Chromium-based browsers | text of the focused page field, word boxes, replacements |
| Installer | `linux/install_engine.py` | run by the user | copies, registers and removes everything above |

The settings app reads no text from other apps. It learns what the engine does from the engine's
log and its `Status()` method, and it calls the panel's `Ping()` to report whether the extension
is loaded. `linux/run.sh` starts the app window from the repository without installing.

### Engine modules

The engine package keeps decisions in pure modules (no `gi`) and isolates GLib/IBus in one adapter.
The engine process never imports `core.py`.

| Module | Responsibility |
|---|---|
| `main.py` | IBus/GLib adapter: vfuncs in, effects out; `PanelClient` (the extension's D-Bus client); log writer; `apps.json` and `settings.json` watchers; browser socket; diagnostics |
| `session.py` | correction session state machine and `EngineLogic`, the facade the adapter calls |
| `keys.py` | key classification for the key path |
| `shortcuts.py` | the configurable correction and translation shortcuts |
| `context.py` | the focused field: text, caret, selection, freshness, capabilities, sensitivity |
| `plan.py` | what a hotkey captures, minimal replace operations, verification of the echo |
| `editbuf.py` | the result being edited in the panel (text, caret, selection, undo) |
| `textdiff.py` | word diff and change list, shared with `core.py` |
| `backend.py` | the per-correction child process and its relay |
| `fallback_ui.py` | IBus popup rendering and the refusal messages |
| `live.py` | live checking, engine side |
| `dictate.py` | dictation state machine (right Ctrl held alone) |
| `speech.py` | clients of the long-lived speech and live-check services |
| `applist.py` | the engine's reading of `apps.json` |
| `browser.py` | hub for browser hosts on a Unix socket |
| `browser_text.py` | correction and dictation in a page field through the browser extension |
| `browser_live.py` | live checking in a page field through the browser extension |

### Channels between processes

| Between | Channel |
|---|---|
| ibus-daemon ⇄ engine | IBus: key events, surrounding text, focus, content type; `commit_text` and `delete_surrounding_text` |
| engine ⇄ extension | session bus, `pl.gramadrama.Panel1` on gnome-shell's own connection ([Panel D-Bus interface](#panel-d-bus-interface)) |
| engine → backend child | the job as JSON on stdin; JSON lines on stdout (`partial`, `result`, `changes`, `error`) |
| engine ⇄ live-check and speech services | one JSON object per line on stdin and stdout |
| engine ⇄ browser hosts | Unix socket `$XDG_RUNTIME_DIR/gramadrama/browser.sock` (directory 0700, socket 0600, peer uid checked), one JSON line per message |
| app → engine | `pl.gramadrama.Engine` at `/pl/gramadrama/Engine` on the session bus; the engine's JSONL log |

### Files

| Where (XDG defaults) | What |
|---|---|
| `~/.config/gramadrama/` | `settings.json` (services, model, languages, shortcuts, switches), `apps.json` (apps and sites where GramaDrama is off, per-app default mode), `auth.json` (sign-in tokens), `prompts/` (prompt overrides) |
| `~/.cache/gramadrama/` | `engine.log` (JSONL events, rotated to `engine.log.1` above 1 MiB), the route cache |
| `~/.local/state/gramadrama/` | what the app remembers between runs |
| `~/.local/share/gramadrama-engine/` | the installed copy of the engine, the app, the launchers and the browser extension; `install.json` |
| `~/.local/share/gramadrama-tools/` | optional speech environment and Whisper model |

Only absolute `XDG_*` values are honoured. Services and their settings are described in
[docs/self-hosting.md](../../docs/self-hosting.md), [remote-access.md](remote-access.md),
[models.md](models.md) and [prompts.md](prompts.md).

## Text safety

These rules are the reason for most of the engine's structure. A change that weakens one of them
needs a test that shows why it is still safe.

**Typing never waits.**

- The key path never waits for another process or the disk. It makes no synchronous D-Bus call
  and reads no file: the focused app is pushed by the extension in advance, `apps.json` and
  `settings.json` are read by threads, and log lines go to a writer thread. What it does send (a
  dictation command, a question to a browser page) is a short write that never waits for an
  answer.
- Network requests, the diff and the popup's line wrapping run in a child process per correction;
  pure-Python work in the engine process would hold the GIL and delay keys. Cancelling a session
  kills its child.
- UI updates, backend starts and live-check updates run from idle callbacks after the key handler
  has returned. D-Bus calls are asynchronous and never wait for a reply.
- An exception in any vfunc is logged by type only, the session ends, and the key is handed to
  `IBus.EngineSimple` as if GramaDrama were not there. The exception: when this key had already
  sent a replacement, it is consumed, so it never lands on top of a partial replace.
- A missing extension, model server, live service or speech service never stops ordinary typing.
  The installer keeps a plain keyboard layout as a second input source.

**Old answers never touch new text.**

- Session ids grow per input context. Results, partials, timers and panel clicks that carry
  another id are ignored. Transport ids for shared services are process-wide.
- Every answer from AT-SPI, a browser page or the live service is checked against a version or
  focus generation of the field taken when it was asked.

**The engine reads only what it can trust.**

- Text, caret and selection are dropped on every focus change. Offsets are code points.
- Apps that reach IBus through GNOME Shell (text-input-v3: every Wayland app) may push the state
  from before the click that focused them. The first update after a focus change or a reset is kept
  but not trusted until an update answers a key the user typed. A hotkey on such a field is refused
  with `stale` (press an arrow key and try again). In-process GTK IM modules re-send their state
  before every key, so their updates are trusted.
- ibus-daemon keeps the last text, caret and anchor it passed to each engine object, across focus
  changes, and drops an update identical to it. An app that sends its unchanged state again after
  a focus change (a window switch, or the focus moving between its widgets) therefore sends the
  engine nothing. The engine keeps the same copy. GNOME Shell turns `SURROUNDING_TEXT` on in the
  capabilities only in the call that sends the text, so when it comes back on in this focus and
  the next message from ibus-daemon is not the text, the engine takes its copy as the field's
  state, untrusted like any first update (log event `surrounding_unchanged`). Only that next
  message decides, never a timer. An empty text is never taken; a sensitive field's is never kept.
- When only the caret is in doubt on a text-input-v3 field, the engine asks AT-SPI through the
  live service where the caret is in the text IBus already has (`ConfirmCaret`, 800 ms). A matching
  answer makes the field current; any other answer keeps the refusal. AT-SPI is only a witness: it
  is never the source of the text and never the way text is written. Right after GNOME Text Editor
  opens a document it moves the caret itself without telling the input method; AT-SPI does not
  confirm that caret, so the refusal stays until an arrow key.
- A selection the app has never reported (Shift+arrows or Ctrl+A in a toolkit that does not send
  the anchor) refuses with `hidden_selection`; an unknown selection refuses with
  `selection_unknown` until something collapses it.
- IBus may provide only part of a field. text-input-v3 sends at most 4000 bytes around the caret,
  so a paragraph that touches the edge of a window of 3900 bytes or more is refused as
  `truncated`.
- The hotkey takes the selection, else the caret's paragraph. Refusals: `empty`, `too_long`
  (over 6000 characters), `embedded` (U+FFFC object), `no_field`, `no_text`.

**Some fields are never read.**

- Password and PIN fields, and fields with the private or hidden-text hints, are refused as
  `sensitive`. Their text is not stored, not passed to the IBus base class and not counted in
  logs.
- Terminals are refused (`terminal`). Apps listed as off in `apps.json` get the hotkey unchanged
  (not even a compose reset); dictation and live checking are off there too.
- Live checking sends text only when the focused app is known, `apps.json` has been read and is
  valid, the app is not off and is not the GramaDrama app itself, and the extension can show the
  result.

**Replacements are planned and checked at the last moment.**

- `plan.replace_ops` runs when the user accepts, never earlier. The field must still have the same
  client, text, caret and anchor as at capture, else `changed`.
- Results that are empty, whitespace only, contain NUL or U+FFFC, or are not valid Unicode are
  refused (`bad_result`). Line ends of apps that use CR, VT, LS or PS are mapped back; if the model
  changed the number of lines there, the replacement is refused (`line_structure`).
- A selection is replaced by a commit alone (clients replace the selection). A paragraph is
  replaced by the smallest delete and commit around the common prefix and suffix, moved to
  grapheme boundaries and widened to contain the cursor (mutter accepts only such delete
  ranges). The commit's `IBus.Text` is built before the delete is sent.
- The echo is verified within 300 ms against the expected paragraph around the new caret.
  A match shows "Applied"; a mismatch shows a warning; no echo ends the session as unverified.
- In a browser page, ranges are UTF-16 code units. The extension checks that the field still holds
  the captured text before writing word-by-word edits (at most 12; more become one span), and
  reports whether they landed.

**Logs contain no text.** The engine log holds events, counts, offsets, reason codes, timings and
app IDs. App IDs appear only in events tied to a user action (start, refusal, apply, verification,
live fixes, dictation) and are not anonymous. Outside test mode `Status()` returns only coarse
fields, without lengths, offsets or key timings.

Offsets use different units in different places:

| Where | Unit |
|---|---|
| IBus surrounding text, the engine, all panel offsets in the view JSON and `Action`/`Edit` | code points |
| Browser extension, LanguageTool | UTF-16 code units |
| Pango inside the extension (`textmap.js` converts) | UTF-8 bytes |
| text-input-v3 surrounding window | at most 4000 UTF-8 bytes |

## Request flow

A correction in an IBus field, with the default shortcuts:

1. **Ctrl+Alt+G** reaches `do_process_key_event`. `keys.classify` recognises the correction
   shortcut. If the focused app is off, the key goes to the app. Otherwise the engine resets any
   compose sequence, ends a running session and captures the selection or the caret's paragraph
   (`plan.capture`), possibly after asking AT-SPI about the caret. A refusal becomes a short
   message toast and the session never starts.
2. The session starts in the app's default mode from `apps.json` (Correct unless set);
   **Ctrl+Alt+J** always starts Translate. The effects are `StartBackend`, `ShowUI` and a
   five-minute idle timeout. The key is consumed.
3. After the key returns, an idle callback starts the backend child. It reads `settings.json`,
   sends the request to the configured model server (Ollama's API or an OpenAI-compatible chat
   API), streams partials at most ten times a second and, except for translations, asks
   LanguageTool for the reasons of the changes in parallel. The result is post-processed, diffed
   (`segments`), turned into a change list (`changes`, at most 30) and sent at once; LanguageTool's
   reasons may follow as a separate `changes` message. With the experimental model-only live mode,
   Correct uses the live service's model path and its cache instead.
4. Every partial and the result become a view sent with `Show`. Once the panel (or, without it,
   the IBus popup) is on screen, the session is modal: Enter, Esc, Ctrl+1…6 and the other session
   keys are consumed.
5. **Enter** or the Replace button plans the replacement against the current field, sends delete
   and commit, then verifies the echo. **Esc**, the × button or any unrelated key ends the session;
   an unrelated key also reaches the app.

In a browser whose GramaDrama extension is connected, the hotkey first asks the page for the
field's text (`context`, 0.8 s). The session then works on the page's text and the result goes back
through the extension (`replace`). If the page has no suitable field, does not answer, or the site
is off, the hotkey takes the IBus way. Details: [browser.md](browser.md).

Live checking sends the field's text to the live service after changes (at most every 100 ms); the
issues come back as the badge and, where word boxes are known, underlines. A click on a suggestion
fixes one word through the same planned and verified path, without opening the panel. Details:
[live-check.md](live-check.md). Dictation (right Ctrl held alone) records through the speech service
and commits the text at the caret: [dictation.md](dictation.md).

### Session keys

While a session is modal:

| Key | Effect |
|---|---|
| Enter (no modifiers) | Replace; while working: apply when ready; after an error: close |
| Esc | cancel |
| Ctrl+1 … Ctrl+6 | restart in Correct, Improve, Formal, Casual, Shorter or Translate; Ctrl+6 again in Translate reverses the direction |
| Ctrl+D | reverse the direction in Translate |
| PgUp, PgDn | page the IBus popup |
| Ctrl+E | start or end editing a finished result (panel only) |
| Ctrl+I | show or hide the change list under a finished correction (swallowed when there is none) |
| a printable key or Backspace in a finished result shown in the panel | start editing with that key |
| the correction or translation shortcut | restart from a fresh capture |
| modifiers | pass to the app |
| any other key | end the session; the key reaches the app |

A key typed after a session has started but before its panel or popup is on screen ends the
session and reaches the app. Auto-repeats and releases of consumed keys are swallowed.

While the result is edited in the panel, every key except modifiers and the two shortcuts goes to
the engine's editor: Enter replaces, Shift+Enter inserts a line break, Esc ends editing, arrows,
Home and End move (Ctrl: by word or to the text's ends; Shift: extend the selection; Up, Down, Home
and End follow the panel's visual lines), Backspace and Delete delete (Ctrl: a word), Ctrl+A
selects all, Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y undo and redo, Ctrl+C or Ctrl+Insert copy,
Ctrl+X or Shift+Delete cut, Ctrl+V or Shift+Insert paste. Other keys are consumed without effect.

## Panel D-Bus interface

The extension exports object `/pl/gramadrama/Panel`, interface `pl.gramadrama.Panel1`, on
gnome-shell's own session-bus connection (bus name `org.gnome.Shell`). Only GNOME Shell can draw a
window-less panel next to the caret that never takes keyboard focus: the app keeps focus, its IBus
input context stays active and the engine keeps handling the session keys. Without the extension,
the engine falls back to the IBus popup.

| Member | Signature | Caller check | Meaning |
|---|---|---|---|
| `Ping()` | `→ s version` | no | the extension's `metadata.json` version as a decimal string (currently `"13"`) |
| `Show(s view)` | `→ b shown` | yes | show or update the correction panel or a message toast ([View JSON](#view-json)); `true` once it is mapped and visible |
| `Hide(u session)` | `→ ()` | yes | hide the panel and the toast if `session` is the one last shown, or for `0` whatever is shown; the pill, badge and underlines are not affected |
| `Subscribe()` | `→ ()` | yes | the caller becomes the single receiver of `Focus`; the current focus is sent right after the reply |
| `Hud(s view)` | `→ b shown` | yes | show, update or hide the dictation pill ([Dictation pill view](#dictation-pill-view)) |
| `Live(s view)` | `→ b shown` | yes | show, update or hide the live-check badge ([Live badge view](#live-badge-view)) |
| `Marks(s view)` | `→ b shown` | yes | draw or remove underlines ([Underline view](#underline-view)) |
| signal `Action` | `(u session, s action)` | | a click on the panel, the badge or the underline card ([Actions](#actions)); broadcast from gnome-shell's connection |
| signal `Focus` | `(s app, s kind)` | | the focused app ([Focus](#focus)); unicast to the subscriber only |
| signal `Edit` | `(u session, s kind, s data)` | | about the result being edited ([Edit](#edit)); only to the bus name that showed the view (unicast) |

Errors:

| Error | When |
|---|---|
| `pl.gramadrama.Panel1.Error.AccessDenied` | the caller check failed (`Show`, `Hide`, `Subscribe`, `Hud`, `Live`, `Marks`) |
| `pl.gramadrama.Panel1.Error.InvalidArgs` | the view cannot be parsed or validated (`Show`, `Hud`, `Live`, `Marks`) |
| `org.freedesktop.DBus.Error.UnknownObject` | `Subscribe` when the extension was disabled (or its actors destroyed) during the caller check; GDBus answers the same for any call once the object is unexported |
| `org.freedesktop.DBus.Error.Failed` | `Subscribe` failed unexpectedly |

`Show`, `Hud`, `Live` and `Marks` return `false` instead of an error when the extension was
disabled while the caller was being checked, or when rendering threw (everything of that kind is
then hidden). A `Hide` that arrives after the extension was disabled returns normally.

### Versions

The engine reads the version from `Ping()` (a non-numeric version counts as 0) and uses newer
members only when they exist:

| Version | Adds |
|---|---|
| 4 | `Subscribe`, `Focus` |
| 5 | `Hud`, `Live` |
| 6 | `Marks` |
| 12 | `Edit`, and editing the result in the panel (`editable`, `editing`, `edit*` actions) |
| 13 | the result as an editable field: no diff, no Edit button; the change list on demand (`changes_open`, the `changes` action) and the marks of an edited text (`editing.marks`) |
| 14 | `waiting` in the dictation pill view: the microphone is open but has given no audio yet |
| 16 | a failed correction's title, sentence and buttons (`problem`, the `problem:<action>` actions); Enter does its first button; `retrying` |
| 18 | `note` in the live badge view: the „!” of Model only checking |
| 19 | `Edit` goes only to the engine that showed the session, and names the session of the view being drawn (it was broadcast, and a report made while a new view was drawn named the previous session) |

A change to the contract bumps `version` in `metadata.json`, and the engine gates the new calls on
it.

### Caller check

Every member except `Ping` is reserved for the installed engine as ibus-daemon starts it:

- The sender's PID comes from `GetConnectionUnixProcessID`. Its `/proc/PID/cmdline` must end with
  exactly two arguments: the installed launcher, `GLib.get_user_data_dir()` +
  `/gramadrama-engine/gramadrama-engine` (what the installer writes into the IBus component), and
  `--ibus`.
- Its parent (`/proc/PID/stat`) must be the session's ibus-daemon, whose PID is taken from the
  peer credentials of GNOME Shell's own IBus connection. If that PID is unknown, every caller is
  refused.
- The result is cached per unique bus name (at most 16 names). The cache is cleared when IBus
  becomes ready again (a new ibus-daemon). A failed check caused by an error is not cached.
- An engine started by hand (without `--ibus`) is refused; it uses the IBus popup and gets no
  focus information.

### Test mode

The check is relaxed only in the isolated test harness. Test mode needs all of:
`GRAMADRAMA_ENGINE_TEST=1` in gnome-shell's environment, `GD_WORK` set to a `/tmp/gd-*` work
directory, and `DBUS_SESSION_BUS_ADDRESS` pointing inside it (`policy.js`, `isTestMode`).
`GRAMADRAMA_ENGINE_TEST` alone, which a user may export to debug the engine, changes nothing.
In test mode any caller may use every member, `globalThis.gdGramaDramaPanel` exposes `state()`
(panel, toast, pill, badge, underlines, placement, caret, focus, the last actions and `Focus`
signals, the duration of the last `Show`) and `textPoint(offset)` (a screen point inside a
character of the shown text), and the shell log says that test mode is on.

### Actions

`Action(session, action)` is emitted only for controls that are visible and reactive. Panel
actions are emitted only while the session panel (not a toast) is on screen and carry its session
id. Badge and underline actions carry session `0`.

| Action | Control | Engine effect |
|---|---|---|
| `accept` | Replace (*Zastąp*) | as Enter |
| `cancel` | × in the header (*Anuluj*) | as Esc |
| `copy` | Copy (*Kopiuj*) | the extension has already put `view.text` on the clipboard and shows "Copied"; the engine ends the session and shows the "Copied to clipboard." toast for 1.5 s |
| `direction` | direction button (Translate) | as Ctrl+D |
| `mode:0` … `mode:5` | mode buttons, including the active one | as Ctrl+1 … Ctrl+6 |
| `edit` | Edit / Done of panels before version 13 | as Ctrl+E |
| `changes` | Show N changes / Hide changes (*Pokaż* / *Ukryj zmiany*) under the result | as Ctrl+I: show or hide the change list |
| `problem:<action>` | a button of a failed correction (`problem.actions`) | `retry` runs the same text in the same mode again; `open:connection`, `open:writing` and `login` start the app (`gramadrama-app --show=<tab>` or `--login`), which opens that tab and rings the group the cause is in (the language model, the text languages, remote access) for a few seconds, and end the session; only the actions the view offers |
| `edit_at:<offset>` | primary click in the result text | start editing with the caret there, or move the caret while editing; the offset is in code points of the result, which the panel shows as it is |
| `edit_fix:<issue id>:<index>` | a suggestion under the edited text | replace that word with suggestion `index` (one undo step) |
| `edit_ignore:<issue id>` | Ignore under the edited text | the live service ignores that word and rule |
| `edit_learn:<issue id>` | Add to dictionary under the edited text | the word goes to the user's dictionary |
| `revert:<id>` | Undo on a change row | only that change goes back to the original |
| `keep:<id>` | Keep and add to dictionary on a change row | as `revert`, and the original word goes to the dictionary |
| `hidden` | none: the extension hid the session panel on its own | the engine ends the session |
| `live:open` | the badge, or the header of its list | Ctrl+Alt+G in Correct mode, when no session runs |
| `live:fix:<issue id>:<n>` | a suggestion in the badge's list or the underline card | fix that word |
| `live:ignore:<issue id>` | Ignore in the underline card | the live service ignores that word and rule |
| `live:learn:<issue id>` | Add to dictionary in the underline card | the word goes to the dictionary; "Added to dictionary." toast |

`hidden` is sent when the on-screen keyboard comes up or the monitor goes away under the panel,
so that an invisible session does not stay modal (Enter would apply a result nobody sees). It is
never sent after the engine's own `Hide`, a newer `Show`, a toast replacing the panel, or when the
engine's connection is gone.

The engine subscribes to `Action` with sender `org.gnome.Shell` and hands it to the engine object
that has focus. Panel actions act only when `session` is the current session and it is modal;
`hidden` only for the current session; `live:*` actions regardless of `session`. While the focused
browser page is checked through the browser extension, `live:fix`, `live:ignore` and `live:learn`
go to `browser_live.py`.

### Focus

Only GNOME Shell knows which app has focus (every Wayland app reaches IBus as `gnome-shell`), and
the engine decides about the hotkey synchronously, so the extension pushes the focused app to it.
`Focus(app, kind)` goes to the subscriber only, right after `Subscribe` and on every change of the
pair. Sources: the display's `focus-window`, the stage's `key-focus`, and the window tracker's
`focus-app` (a window whose app is known only later, e.g. a WM_CLASS set late).

| Situation | `app` | `kind` |
|---|---|---|
| a window of an app with a `.desktop` file | its desktop ID, e.g. `org.gnome.TextEditor.desktop` | `wayland` or `x11` |
| a window-backed app (no `.desktop` file) | `window:<WM_CLASS>`, at most 100 characters of the class; a Chromium app window (`--app=URL`) keeps only the browser and the host, e.g. `window:chrome-example.com` | `wayland` or `x11` |
| a GNOME Shell entry has the keyboard (search, run dialog) | `org.gnome.Shell` | `shell` |
| no focused window | `""` | `none` |
| the extension is being disabled or its actors are gone | `""` | `""` |

After `("", "")` no focus information follows until the next `Subscribe`; the lock screen disables
user extensions, and a stale app must not keep deciding about the hotkey. The subscriber is
forgotten when its bus name goes away. Page paths and file names never reach the app ID, because
the ID is logged and stored in `apps.json`. What the app IDs are used for: [apps.md](apps.md).

### Edit

`Edit(session, kind, data)` reports about the result being edited. It is emitted only while the
session panel is on screen, only to the bus name that showed that view (unicast: a paste carries the user's text,
so no other process on the bus may receive it), and is never logged. `session` is the view's own, also for a report
made while a new view is drawn.

| `kind` | `data` | When |
|---|---|---|
| `lines` | JSON array of the code-point offsets where the visual lines of the edited text start; the first is `0` | whenever the edited text or its wrapping changes; the engine uses it for Up, Down, Home and End |
| `paste` | JSON `{"id": <int>, "text": <string>}`, the clipboard's text (`""` when empty) | once per `editing.paste` id, in answer to Ctrl+V |

The engine ignores other kinds, another session id, `lines` data over 120 000 characters or over
10 000 entries, `paste` data over 160 000 characters, an `id` that is not the pending paste, and
pasted text that is empty, over 20 000 characters or not valid Unicode.

### Engine behaviour

`PanelClient` in `engine/main.py` is the only client.

- Every call uses `NO_AUTO_START` and is asynchronous (2 s timeout for `Ping` and `Subscribe`,
  3 s for the rest); no call waits for its reply. Panel updates are sent from an idle callback after
  the key handler has returned, and only the last of the queued UI effects is drawn. (After an
  exception, `Hide` may be sent directly.)
- The engine pings at start. While the extension is missing it pings again at most every 10 s when
  it has something to show, and at most every 2 s when a field gets focus. After an extension that
  had answered went away (the lock screen), every focus change pings at once until it answers
  again: the unlock dialog's own field gets focus while the extension is still off.
- Errors that mark the extension missing: `UnknownMethod`, `UnknownObject`, `UnknownInterface`,
  `ServiceUnknown`, `NameHasNoOwner`, and any error that is not a D-Bus error (a timeout, a closed
  connection). The focus becomes unknown and the IBus popup is used.
- After a `Ping` answer of version 4 or later the engine calls `Subscribe`. If no `Focus` arrives
  within 2 s, the focus is unknown and the next focus change subscribes again. A reply to an older
  `Subscribe` is ignored. `AccessDenied` on `Subscribe` (the extension was disabled and enabled
  during the check, or GNOME Shell did not know ibus-daemon yet) is retried at the next focus
  change, except by a hand-started engine.
- The focus is unknown (`None`) without a `Focus` within 2 s, when the extension is missing, and
  after `Focus("", "")`. With an unknown focus the hotkey and dictation are allowed, and live
  checking sends nothing.
- `Show` is called for every update of a session: partials, mode switches, the result, edits.
  A new session is modal only once the reply has come. If `Show` returns `true`, the panel is in
  use; if it returns `false` or fails, the engine draws the same state in the IBus popup, and the
  session is modal there. Updates of a session already confirmed on screen keep it modal, so an
  Enter typed between an update and its reply cannot reach the app. A message toast that cannot be
  shown becomes IBus auxiliary text.
- Editing is offered (`editable`) only when the panel showing the session has version 12 or later.
- `Hide(0)` is sent on session end, cancel, focus change and failure. A refusal toast is hidden when
  the focus moves.
- `Live` and `Marks` are fire-and-forget. If `Hud` returns `false`, the dictation state is shown as
  IBus auxiliary text.
- `PanelClient` adds `ui_language` to every view it sends (`Show`, `Hud`, `Live`, `Marks`).
- The log gets `panel` when availability changes and `focus` when the subscription state changes,
  never the app IDs themselves.

The engine's own diagnostics interface, `pl.gramadrama.Engine` at `/pl/gramadrama/Engine`, has
`Status() → s` (JSON) for the app and the tests. In test mode it also has `SetDebugReply`,
`SetDebugDelay`, `SetKeyDelay` and `Snapshot`, and `Status()` adds key timings, the focused app and
the `apps.json` state; outside test mode those would let any process on the bus watch the user.

## View JSON

`Show(view)` takes one JSON object. The engine sends a session view for every update of a
correction session and a message view for toasts. The extension validates and bounds it in
`parseView` (`panel.js`); unknown fields are ignored.

A session view, as the engine sends it for a finished correction:

```json
{
  "v": 1,
  "ui_language": "en",
  "session": 7,
  "kind": "session",
  "status": "ready",
  "mode": 0,
  "target": null,
  "source_language": null,
  "scope": "paragraph",
  "source": "Text with a mistakke.",
  "text": "Text with a mistake.",
  "segments": [["same", "Text with a "], ["removed", "mistakke"], ["added", "mistake"], ["same", "."]],
  "changes": [{"from": "mistakke", "to": "mistake", "reason": "Spelling", "id": 1, "keep": true}],
  "count": 1,
  "note": "Model · 1.2 s",
  "error": "",
  "message": "",
  "can_apply": true,
  "editable": true,
  "changes_open": false
}
```

| Field | Type | What the engine sends | Extension default and bounds |
|---|---|---|---|
| `v` | integer | `1` | `1` |
| `ui_language` | string | the interface language, which follows the primary text language: currently `pl` or `en` | `pl` when missing; any value but `pl` gives English strings |
| `session` | integer | the session id; ids grow per input context and restart with the engine process | required, unsigned 32-bit |
| `kind` | string | `session` or `message` | `session` |
| `status` | string | see the status table | unknown values: `checking` (`message` for a toast) |
| `mode` | integer | 0–5: Correct, Improve, Formal, Casual, Shorter, Translate (`fix`, `improve`, `formal`, `casual`, `shorter`, `translate`) | `0` |
| `target` | string or null | in Translate, the target language once known: `pl`, `en` or `de`; otherwise `null` | `null` |
| `source_language` | string or null | the other language of the configured pair, once the target is known | `null` |
| `scope` | string | `paragraph` or `selection` (the extension can also label `field`) | at most 40 characters |
| `source` | string | the captured text | cut at 20 000 characters |
| `text` | string | the model's text so far while working; the result when finished; the edited text while editing | cut at 20 000 characters |
| `segments` | array | the word diff, `[kind, text]` pairs with kind `same`, `removed` or `added`; empty for partials, for translations and while editing | merged with same-kind neighbours; above 2000 segments or 20 000 characters the result is shown as plain `text` |
| `changes` | array | at most 30 rows `{from, to, reason}`; in a finished correction (not Translate) a row with a valid range also has `id` and `keep`; empty while editing | at most 30; `from` and `to` cut at 200, `reason` at 300; `id` an integer ≥ 0 or none; `keep` boolean |
| `count` | integer | the number of changes, which may exceed the rows sent | the number of rows |
| `note` | string | footer text built by the engine: model, direction, time | at most 300 |
| `error` | string | for status `error`: the message | at most 2000 |
| `problem` | object | for status `error` only: `group` (1 something must be set up, 2 the server does not answer, 3 the model slipped up, 4 the text cannot be corrected as it is), `title`, `text` and `actions`, a list of `{id, label}` whose first is what Enter does in a panel of version 16 or later ([engine/problems.py](../gramadrama/engine/problems.py)) | only with status `error` and a string `title`; `group` outside 1 … 4 is 2; `title` cut at 200, `text` at 2000; `actions` other than `retry`, `open:connection`, `open:writing` and `login` dropped, at most 2, labels cut at 60 |
| `message` | string | `""` | at most 2000 |
| `can_apply` | boolean | always `true` | `true`; `false` hides Replace and Edit, makes Copy the primary button and lets Copy work in `unchanged` |
| `editable` | boolean | `true` for a finished result (`ready` or `unchanged`) in a panel of version 12 or later | `false`; `true` frames the result as a field with "Result — you can edit it before you insert it" above it |
| `changes_open` | boolean | the change list is shown: Ctrl+I or the `changes` action switch it in a finished correction; every new result starts with it closed | `false` |
| `retrying` | boolean | while `checking` or `working`: the engine's quiet second try after a slip of the model (a broken stream, an empty or rejected answer) | `false`; `true` shows "Trying again…" in the header |
| `editing` | object | only while the result is edited | dropped when `text` is 20 000 characters or longer |

Text bounds in the extension count JavaScript string length (UTF-16 code units); offsets count
code points.

A change's `id` is the index of its first segment in the backend's diff. It stays valid while other
changes are reverted, and `revert:<id>` and `keep:<id>` send it back. `keep` is `true` when one word
was changed into another, so the original can be kept and added to the dictionary.

The `editing` object:

| Field | Type | Meaning | Extension handling |
|---|---|---|---|
| `cursor` | integer | the caret, code points in `text` | outside `0 … length`: the end |
| `anchor` | integer | the other end of the selection; the selection is `[min, max)` of the two | as `cursor` |
| `clipboard` | object `{id, text}` | text to put on the clipboard after Ctrl+C or Ctrl+X | applied once per `id`; ids grow |
| `paste` | integer | the id of a paste request (Ctrl+V) | the extension answers once per id with `Edit` `paste` |
| `issues` | array | what the live check found in exactly this text: `id` (string), `start` and `end` (code points), `word`, `message`, `kind`, `suggestions` (at most 3), `learn` (Add to dictionary is offered) | at most 200; entries without a string `id` or with a range outside the text are dropped; `id` and `word` cut at 200, `message` at 300, suggestions at 200 |
| `checking` | boolean | a check of the edited text is running | `false` |
| `marks` | array | `[start, end)` code point ranges of what the edited text changes against `source` (the corrections that stay and what was typed); empty in Translate and above 3000 characters | at most 2000; ranges outside the text dropped |

A message view:

```json
{"v": 1, "ui_language": "en", "session": 7, "kind": "message", "status": "message", "message": "Applied", "seconds": 1.5}
```

`seconds` is the toast's duration: 3 by default, at most 60. A message view with an empty
`message` is not shown. The engine uses 1.5 s for "Applied" and "Copied to clipboard.", 2 s for
"Added to dictionary.", 3 s for refusals and 4 s for failed replacements.

| `status` | Session state | Panel |
|---|---|---|
| `checking` | waiting, nothing from the model yet | the source, dimmed; badge "Reading the script…" |
| `working` | streaming | the text so far, dimmed; spinner |
| `apply_pending` | Enter was pressed while working; it applies when the result arrives | spinner, "Will apply when finished" |
| `ready` | finished with changes | the result as a field with what changed on a sage tint (or the translation); the change list with Undo buttons on demand |
| `unchanged` | finished, the result equals the source | badge "No drama" |
| `error` | the backend failed | the `error` text |
| `applying` | the replacement was sent and awaits verification | spinner, "Replacing…" |
| `message` | toast only | logo and message |

Rendering follows [branding/PANEL.md](../../branding/PANEL.md). Notes for the contract:

- All text is escaped before it goes into Pango markup.
- The logo is filled in `ready`, `unchanged` and `applying`, and outlined otherwise.
- Translate shows the source small and dimmed (at most three lines) above the translation once
  streamed text arrives; before that the source is the dimmed main text. A finished translation has
  no count badge and no change list.
- Replace and Copy react in `ready` and while editing; in `applying` only × reacts. Modes and the
  direction button do not react in `applying` or while editing. Edit is shown when `can_apply` is
  true and the view is `editable` or `editing`, and does not react in `applying`.
- The footer shows `note` and the scope word (paragraph, selection, whole field); while editing it
  shows the editing keys instead.
- Change rows get Undo (`revert`) and, for word changes, Keep and add to dictionary (`keep`) only
  when the status is `ready` and the row has an `id`.
- Punctuation-only changes get a tinted chip, whitespace-only changes are drawn as "␣", and a
  removed line break as "¶".
- A toast (`kind: "message"`) replaces the panel, also for the same session, and hides itself after
  `seconds`.
- An oversized view stays cheap: the `Show` handler is tested to take under 50 ms.

### Dictation pill view

`Hud(view)`, version 5. The engine sends it from the dictation state machine.

| Field | Meaning | Bounds |
|---|---|---|
| `v` | `1` | required |
| `state` | `listening`, `recognizing`, `cleaning` (the model tidies the transcript), `message` or `hidden` | required |
| `level` | microphone level for the bars, while listening | 0–1 |
| `seconds` | listening: time so far (`m:ss`); message: how long it stays (3 when 0 or missing) | 0–3600 |
| `language` | the dictation language label while listening, e.g. `PL`, `EN` | 2–4 letters, else no label |
| `waiting` | listening, but no audio yet: a spinner and *Budzę mikrofon…* instead of the bars and the time (version 14; older extensions ignore it and say *Słucham…*) | `true`, else not waiting |
| `message` | the text of a message | required and non-empty for `message`; cut at 300 |
| `copy` | text put on the clipboard before the message is shown (dictated text that could not be inserted) | at most 50 000 |
| `ui_language` | as for `Show` | |

`hidden` hides the pill and returns `false`. A pill without news hides itself after 30 s while
listening and after 200 s while recognising or cleaning up.

### Live badge view

`Live(view)`, version 5. The engine sends it from `engine/live.py` or `engine/browser_live.py`.

| Field | Meaning | Bounds |
|---|---|---|
| `v` | `1` | required |
| `count` | the number of issues; `0`, not `checking` and no `note` hides the badge | required, integer ≥ 0 |
| `checking` | a server check is running (the lava animation while the count is unknown) | boolean |
| `summary` | the count as words, for the list header and the accessible name | at most 40 |
| `issues` | up to five issues: `id`, `word`, `message`, `kind` (`spelling`, `grammar` or `style`; others become `grammar`), `source`, `suggestions` | at most 5; `id`, `word` and suggestions cut at 200, `message` at 300, `source` at 40, at most 3 suggestions |
| `more` | issues beyond those listed | integer ≥ 0 |
| `note` | Model only: why checking finds nothing it may (the model does not answer: paused for a minute; sentences left unchecked); with `count` 0 the circle says „!”, and the list shows the note (version 18) | at most 300 |
| `field` | `[x, y, width, height]`, the page field's visible box in the window's client coordinates; used only with `from: "client"` | positive size |
| `from` | `client` for a browser page | |
| `ui_language` | as for `Show` | |

### Underline view

`Marks(view)`, version 6. The boxes come from AT-SPI through the live service, or from the browser
extension for a page.

| Field | Meaning | Bounds |
|---|---|---|
| `v` | `1` | required |
| `marks` | `{id, word, message, suggestions, kind, source, learn, rects}`; `rects` are `[x, y, width, height]` boxes in the app's window coordinates; an empty list removes the underlines | at most 60 marks and 60 boxes in total; marks without a box are dropped |
| `toolkit` | `[name, version]` from AT-SPI | each cut at 40 |
| `caret` | the caret's box in app coordinates | |
| `clip` | the field's visible box in app coordinates; words whose centre lies outside it are not drawn | |
| `from` | `client` for a browser page | |
| `ui_language` | as for `Show` | |

Where the coordinates start: the window's client rectangle for `from: "client"`; the window frame
for GTK 4; otherwise the offset between IBus's caret rectangle and `caret`, kept per window relative
to its frame. Without any of these, nothing is drawn.

## Placement and focus

**The caret.** The extension listens to the IBus panel service's `set-cursor-location`
(absolute) and `set-cursor-location-relative` (window-relative, resolved against the focused window
when used) signals, the ones GNOME's own candidate popup uses. Where GNOME Shell's private panel
service is not available, it falls back to the IBus manager's public absolute signal. A rectangle of
`(0, 0, 0, 0)` clears the caret. A caret recorded for another window, or lying outside the focused
window's buffer rectangle, counts as unknown: GTK 4 over text-input-v3 sends a new caret rectangle
only after real input, not after a programmatic move, so a moved or re-laid-out window can leave a
stale one.

**The panel** (520 px wide):

- It opens 6 px below the caret, with its left edge 16 px left of the caret so its text starts
  under it. It goes above the caret when there is no room below, and to the side with more room
  when neither fits. It is clamped 8 px inside the monitor's work area.
- A correction that is still checking or working (not Translate) needs 160 px of extra room below
  for the change list that will come, else it opens above.
- Updates of the same session keep the side while the caret stays put, so the panel never flips
  while a result streams in. It is placed again when the caret moves and when monitors change.
- Without a usable caret, the panel sits inside the focused window, at the bottom centre of its
  frame, 8 px above the bottom. An update of the same session keeps its last anchor when neither a
  caret nor a focused window is known. With no anchor at all, `Show` returns `false`.
- The toast uses the same rules at its natural width.

**Stale and foreign sessions.** A `Show` is ignored (returns `false`) only if something is on
screen, the call comes from the same bus connection, and its session id is lower than the one on
screen. With nothing on screen any id is shown, and another connection is never stale: session
counters differ between engine objects and restart with the engine process.

**`Show` also returns `false`** when the on-screen keyboard is visible, the toast's message is
empty, or there is no monitor (then anything shown is hidden).

**Other overlays.** The dictation pill (230 × 40 px) stands at the bottom centre of the monitor with
the pointer, 48 px above the work area's bottom. A message is never cut: its pill is as wide as its line, up to
420 px (the width of the macOS toast that shows these messages), wraps beyond that and grows upwards.
The live badge stands inside a page field's bottom-right corner (3 px from the edges of a field lower than 34 px,
else 6 px) when the browser extension reports the field; elsewhere 8 px inside the focused window's right edge, level with the
caret's line (near the window's bottom when the caret is unknown), at least 20 px from the work
area's top and bottom. Underlines are drawn inside the focused window's actor, so they move with
the window and the app's own popups cover them. `Live` and `Marks` return `false` while the
on-screen keyboard is visible or when there is no focused window.

**Focus.** Nothing GramaDrama draws ever takes the keyboard.

- The panel, toast, pill, badge, the badge's list and the underline card are added with
  `Main.layoutManager.addTopChrome`; nothing grabs or is given key focus. The panel and toast are
  kept just above the on-screen keyboard's layer, like GNOME's candidate popup, so the screenshot
  UI stays on top.
- The panel and its buttons are reactive but `can_focus: false`, so a click never falls through to
  the app and never moves focus. The toast and the pill are click-through, and the underlines never
  take a click. The badge, its list and the underline card are reactive but not focusable. A click
  must leave `global.display.focus_window` and the app's text-input focus unchanged; the panel
  tests check this.
- Because the panel never has the keyboard, the engine is the editor while a result is edited: the
  panel draws the text, caret, selection and issues from the view, and reports visual lines and
  clipboard text with `Edit`.
- When the on-screen keyboard comes up, the panel and toast are hidden (`hidden` is sent for a
  session panel), and the badge and underlines are hidden too.

**Lifetime.** The panel never outlives its engine: the extension watches the unique bus name that
showed it and hides the panel and toast when that connection goes away. The pill, badge and
underlines are watched the same way. `enable()` undoes itself if any step throws. If GNOME Shell
destroys the actors on its own (at shutdown), callbacks stop touching them and the subscriber gets
`Focus("", "")`.

**GNOME Shell 50 constraints** met in the extension's code:

- `Clutter.Text.get_font_description()` is transfer-full in the typelib; GJS would free the actor's
  own font description. Use `St.ThemeNode.get_font()`.
- `Main.layoutManager.uiGroup` has no layout manager, so its children are relayout roots that the
  stage may allocate after they were destroyed. Call `get_allocation_box()` before `destroy()`.
- GNOME Shell destroys the IBus panel service when ibus-daemon restarts (also once when Xwayland
  starts). Forget its handler ids on its `destroy` signal instead of disconnecting later.

## Installer

`linux/install_engine.py` installs for the current user and needs a GNOME session. Run it with the
system Python from the repository: `/usr/bin/python3 linux/install_engine.py <command>`.

| Command | What it does |
|---|---|
| `status` | prints what is installed and active; changes nothing |
| `install [--layout pl\|us\|de]` | copies, registers and activates everything (below) |
| `uninstall` | removes what `install` added and restores the input sources |
| `tools` | downloads the optional local speech environment and Whisper model, then tests them on the GPU |

`install`:

- Copies the `gramadrama` package, the launchers, the icon and `browser/extension` to
  `~/.local/share/gramadrama-engine` (the package by an atomic rename). The running engine always
  uses that copy, so editing the repository changes nothing until `install` runs again.
- Writes the IBus component for `/usr/bin/python3 <copy>/gramadrama-engine --ibus` with the chosen
  keyboard layout (default: the previous installation's, initially `pl`). ibus-daemon reads
  components only from `/usr/share/ibus/component`, so when the file there differs the installer
  prints one `sudo install -m 644 …` command, exits with status 2, and asks for `install` again.
- Restarts the IBus user service when the engine is not registered or its component changed, and
  stops engine processes older than a change of the engine's code (the engine package, the
  long-lived services and the locales); the next activation starts the new code.
- Puts GramaDrama first among the input sources, keeping a plain keyboard layout as a fallback, and
  activates it. Clears GramaDrama's legacy GNOME custom shortcut for Ctrl+Alt+G, if present, since
  GNOME would take the key before the input method.
- Copies the extension to `~/.local/share/gnome-shell/extensions/gramadrama@gramadrama.local` when
  its files changed, and adds it to `org.gnome.shell enabled-extensions`. GNOME Shell loads a new or
  updated extension only after logging out and in; until then results appear in the IBus popup.
- Installs the app's `.desktop` file, icon and `~/.local/bin/gramadrama`, and registers the
  `gramadrama://` scheme used to return from a sign-in. Files it did not write are never
  overwritten.
- Registers the browser host manifest in the configuration of each Chromium-based browser that
  exists (Chrome, Chromium, Edge, Brave, Vivaldi) and prints where to load the browser extension
  unpacked. Snap and Flatpak browsers cannot start the host and are not covered.
- Saves the previous input sources, the old shortcut and the enabled extensions to
  `~/.config/gramadrama/engine-install-backup.json`, and records the version and change times in
  `install.json`, which the app reads.

`uninstall` restores the saved input sources, removes only the GramaDrama entry from the enabled
extensions (the user's other extensions stay as they are) and deletes the extension, restores the
old shortcut if the installer had cleared it, stops the engine, removes the scheme registration,
the browser host manifests and app launchers that point to the copy, and the launch fixes the app
wrote ([apps.md](apps.md)), then deletes the copy and the backup. It prints the `sudo rm` command
for the IBus component. Settings, `apps.json`, sign-in tokens, prompts, cache, state and the speech
tools stay.

`tools` needs `uv`. It creates a Python 3.13 environment in `~/.local/share/gramadrama-tools/speech`
with pinned faster-whisper, CTranslate2 and CUDA 12 libraries, downloads Whisper large-v3-turbo
(about 4.3 GB in total) and runs a GPU self-test. It is needed only for the built-in Whisper on an
NVIDIA GPU; a transcription server needs nothing local ([dictation.md](dictation.md)).

## Tests

### Unit tests

Run from the repository root:

```bash
bash scripts/test-linux.sh -v
node --test linux/tests/textmap.test.mjs linux/tests/lava.test.mjs
python3 scripts/check_repo_privacy.py
```

`scripts/test-linux.sh` runs `python3 -m unittest discover -s tests` in `linux/` with temporary XDG
directories and without the user's display or session bus. CI runs it and the Node tests on
Ubuntu.

| Area | Tests |
|---|---|
| engine core | `test_engine_context.py`, `test_engine_keys.py`, `test_engine_plan.py`, `test_engine_session.py`, `test_engine_editbuf.py`, `test_engine_fallback_ui.py`, `test_shortcuts.py` |
| engine adapter and focus | `test_engine_main.py` (`PanelClient` with a fake connection, `apps.json` reading), `test_engine_applist.py` |
| backend and services | `test_engine_backend.py`, `test_core.py`, `test_prompts.py`, `test_route.py`, `test_oidc.py` |
| live checking | `test_engine_live.py`, `test_live.py`, `test_liveservice.py`, `test_model_live.py`, `test_model_only_service.py`, `test_live_settings.py`, `test_geometry.py` |
| dictation | `test_engine_dictate.py`, `test_engine_speech.py`, `test_speech.py`, `test_dictation.py`, `test_app_speech.py` |
| browser | `test_engine_browser_text.py`, `test_browser_live.py`, `test_browserhost.py` |
| app and installer | `test_apps.py`, `test_checks.py`, `test_checks_i18n.py`, `test_app_log.py`, `test_i18n.py`, `test_install_engine.py` |
| extension, pure JS | `textmap.test.mjs` (offsets in the panel's text), `lava.test.mjs` (badge animation geometry) |

The backend and service tests talk to `tests/stub_server.py`, a stand-in on 127.0.0.1 for Ollama,
an OpenAI-compatible server, LanguageTool, a transcription server and an OpenID Connect provider,
so no text leaves the machine. Cases shared with macOS live in `tests/postprocess_cases.json`,
`tests/ime_cases.json`, `tests/prompt_profiles.json` (regenerated by `linux/tests/prompt_profiles.py`)
and `bench/prompts.golden.json`; `linux/tests/dictation_cases.json` holds the dictation cases.
Matching cases do not mean the two platforms' integrations behave the same.

### Integration suites

These run in an isolated, headless GNOME Shell 50 session and need GNOME Shell 50, Xwayland, and
for some scenarios Chrome and GNOME Text Editor. Run them from `linux/` with the system Python:

| Suite | What it covers |
|---|---|
| `tests/integration_engine.py [--only NAME,…] [--logs DIR] [--repeat N] [-v]` | the real engine as the input source: typing pass-through, captures and refusals in GTK 3, GTK 4 and Chrome fixtures, session keys, the real backend against the stub server, the panel with the real extension, the focused app and `apps.json`, dictation, live checking, browser pages; after every session the engine log is searched for fixture text |
| `tests/integration_panel.py [--only NAME,…] [--shots DIR] [--docs] [--site] [--logs DIR] [-v]` | the extension driven over D-Bus with fixture views: every status in both variants, placement at the caret, clicks (focus unchanged), Copy, Hide, stale sessions, toasts, the vanished engine, invalid and oversized views, `Focus` (unicast, Wayland, Xwayland, shell entry, no window), the on-screen keyboard, disable and enable; a second session without test mode checks the caller check with a stand-in engine; `--shots` and `--docs` refresh the screenshots in `docs/panel/`; `--site` shoots the site's animations (a correction and dictation, frame by frame) and makes them with `site/tools/anim.py` |
| `tests/integration_app.py [--only NAME,…] [--shots DIR] [--docs] [--logs DIR] [-v]` | the settings app in a session set up like a real installation: status rows, tabs, the Try field, the Apps list and launch fixes |
| `tests/integration_model_live.py` | model-only live checking and the correction shortcut against the stub server |

### Session harness

[`tests/session/README.md`](../tests/session/README.md) documents the harness the suites use: its
own D-Bus, IBus, dconf, home directory and browser profiles under `/tmp/gd-*`; real keyboard and
pointer input through Mutter's remote desktop interface; GTK and Chrome fixtures with ground-truth
logs; and a supervisor that tears everything down even when the test process dies.
`tests/session/smoke.py` is its self-test. Results apply to the environment that ran them, not to
every distribution, app or GNOME version.

### Test switches

| Variable | Effect |
|---|---|
| `GRAMADRAMA_ENGINE_TEST=1` | engine test mode: debug transforms instead of the model, verbose log, full `Status()`, test-only D-Bus methods, live checking off unless `settings.json` turns it on; backend and live service accept only servers on 127.0.0.1; with the harness, extension test mode |
| `GRAMADRAMA_BACKEND=real` | in test mode, use the real backend (against the stub server) |
| `GRAMADRAMA_DEBUG_REPLY`, `GRAMADRAMA_DEBUG_DELAY_MS`, `GRAMADRAMA_DEBUG_BURN_MS`, `GRAMADRAMA_DEBUG_SPEECH` | in test mode only: a fixed reply, a delay, CPU load in the child, a fixed transcript |
| `GRAMADRAMA_ENGINE_DEBUG=1` | verbose engine log without test mode (still no text) |
| `GRAMADRAMA_ENGINE_LOG` | another path for the engine log |

Quality of real models and dictionaries is measured separately with `tests/bench.py` and
`tests/lint_bench.py` against the user's own servers ([models.md](models.md),
[prompts.md](prompts.md)). macOS and GUI behaviour outside the harness are not covered by these
tests.

## Related documents

- [Linux README](../README.md): installation, services, shortcuts, limitations.
- [Panel design](../../branding/PANEL.md), shared with macOS.
- [Applications and launch fixes](apps.md), [browser extension](browser.md),
  [dictation](dictation.md), [checking while typing](live-check.md),
  [languages](languages.md).
- [Self-hosting the services](../../docs/self-hosting.md), [remote access](remote-access.md),
  [models](models.md), [prompts](prompts.md).
- [Session harness](../tests/session/README.md).
