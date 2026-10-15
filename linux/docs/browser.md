# Browser extension (Chrome, Chromium, Edge, Brave)

The shortcuts Ctrl+Alt+G and Ctrl+Alt+J in the examples are the defaults. The current shortcuts
are set in **Home → Shortcuts** (*Start → Skróty*), and the languages in **Writing** (*Pisanie*);
see [text languages](languages.md).

The GramaDrama extension is **one and the same on the Mac and on Linux**: the `browser/extension` directory in the
repository, the same ID (`hlnpnkiancoohmafifihcjndfmhilkfe`, from the key in the manifest), the same protocol. Nothing
in it is Linux- or macOS-specific: it chooses system-dependent text (the shortcut ⌥⌘G or Ctrl+Alt+G) at run time.
Behind the extension there is a different host and a different app: on the Mac `GramaBrowserHost` and the app,
on Linux `gramadrama-browser-host` and the engine.

The extension corrects nothing by itself. It is the engine's interface in the browser: when the engine asks, it provides
the text of the page's active field and the positions of words, and it performs replacements. Without a running engine
it does nothing.

```
extension ⇄ native messaging ⇄ gramadrama-browser-host ⇄ Unix socket ⇄ engine (engine/browser.py)
            (4-byte length + JSON)                       (one JSON line per message)
```

- **Host** (`gramadrama/browserhost.py`, started by the browser for as long as the extension is connected) understands
  nothing: it turns the browser's frames into JSON lines and back, and when the engine is not there, it waits for it and
  tells the extension that it is missing. When the connection to the engine ends (a restart), the host ends too, and the
  extension starts a new one straight away. It accepts only the extension with the right ID (the origin from the first
  argument; the host's manifest is the second check).
- **Socket** `$XDG_RUNTIME_DIR/gramadrama/browser.sock`: directory 0700, socket 0600, and the engine checks the uid of
  every peer. Another user of the computer cannot connect.
- **Engine** (`gramadrama/engine/browser.py`) knows the connected browsers (name and extension version: in the log
  `browser_connected`, in the status `browsers`; never the text or the page address) and sends the extension requests
  of the same types as the Mac (`hello`, `context`, `live`, `rects`, `replace`, `insert`): live checking, Ctrl+Alt+G and
  dictation in web pages.

## Installation

1. `/usr/bin/python3 linux/install_engine.py install` copies the extension to `~/.local/share/gramadrama-engine/browser-extension`
   (a fixed path, refreshed on every installation) and registers the host in the browsers that have a configuration
   directory in `~/.config` (`google-chrome`, `chromium`, `microsoft-edge`, `BraveSoftware/Brave-Browser`, `vivaldi`):
   the file `NativeMessagingHosts/pl.gramadrama.browser.json`. Browsers from a snap or Flatpak package run in a sandbox
   and cannot start a host outside it, so these instructions do not cover them.
2. Load the extension by hand once: `chrome://extensions` → **Developer mode** (*Tryb dewelopera*) → **Load unpacked**
   (*Załaduj rozpakowane*) → the folder from step 1 (in the app window: the **Apps** (*Aplikacje*) tab, the **Browser
   extension** (*Wtyczka przeglądarki*) row in the **Integrations** (*Integracje*) group; while the extension is not
   connected, its **Open folder** (*Otwórz folder*) button opens the folder and copies its path to the clipboard).
   Chrome does not allow loading an unpacked extension with a command-line flag (`--load-extension` is blocked in it),
   and the Chrome Web Store is a separate matter.
3. After the extension's files change, the installer copies them again; then click the extension's reload button
   in `chrome://extensions`.

`install_engine.py uninstall` removes the registered host files (only ours).

With the [package](../README.md#the-package), **Switch on for this account** (*Włącz na tym koncie*) registers the
host in the same browsers, pointing to `/usr/lib/gramadrama/gramadrama-browser-host`, and the folder to load is
`/usr/lib/gramadrama/browser-extension`, updated with the package. **Switch GramaDrama off for this account…**
(*Wyłącz GramaDramę na tym koncie…*) removes the host files again.

## Icon and popup

The same as on the Mac. Lavender icon: GramaDrama works on this page. Grey with “off”: the site is disabled. Grey with
a pink “!”: the app is not running. Clicking the icon shows the status and the **Check text on this site**
(*Sprawdzaj na tej stronie*) switch. The engine saves disabled sites in `~/.config/gramadrama/apps.json` (the `"sites"`
key, host names in lower case without `www.`), and the **Apps** tab shows them in the **Disabled sites**
(*Wyłączone strony*) group, each with an **Enable** (*Włącz*) button. Every change to the file (from the extension or from the window) reaches all connected
extensions. The engine does not overwrite a damaged `apps.json` on behalf of the extension: the app repairs it.

## Tests

- Unit tests, without a browser: `tests/test_browserhost.py` (frames, the host and the hub on real sockets).
- With a real Chrome in an isolated session: `integration_engine.py --only browser_extension_connects` and
  `integration_app.py --only browser`. Chrome (the stable version) ignores `--load-extension`, so
  `tests/session/chrome_ext.py` loads the extension through the DevTools protocol over a pipe (`Extensions.loadUnpacked`,
  as Puppeteer does) and can run code in the extension's service worker, so that the tests can do what the extension's
  popup does.
- The extension's test pages, which need no browser with the extension, are the same as on the Mac and unchanged here:
  `browser/test/` (see `browser/README.md`).

## Live checking in web pages (underlines, badge, fixes)

When the focused window is a browser with a connected extension, **the extension, not IBus, is the source of the text**
for live checking (`gramadrama/engine/browser_live.py`, like `BrowserChannel` and `LiveMonitor` on the Mac). In the
browser, IBus provides only a text window or nothing (X11), and it knows nothing about the positions of words.

- Every 0.35 s the engine asks the extension for `live`: the whole text of the page's active field and the caret. The text
  goes to the same checking service (Enchant, LanguageTool, the model) as text from other applications, so the errors,
  the badge with the count and the list of suggestions are the same. A longer field is checked by the paragraph at the
  caret. A site disabled in the extension, a password field and a field without focus send nothing.
- When there are errors, the engine asks for `rects`: the rectangles of the words in page pixels. GNOME Shell draws the
  underlines in window pixels, so each rectangle is `page origin in the window + zoom × rectangle`. The page origin
  follows from the pointer position, which the page knows in both coordinate systems (the Mac does the same), and without
  the pointer from the browser's toolbar (`outerHeight − innerHeight × zoom`). The second way is exact in a maximised
  window; in a window that is not maximised it includes the window's bottom shadow (Chrome on X11: the page comes out
  ~25 px too low), so it is used only until the pointer first moves over the page.
- **Nested frames** (an editor in an `<iframe>`, also from another domain): the frame asks its parent through
  `postMessage` where its `<iframe>` element lies (with the border and padding), the parent adds its own position and
  asks further up, up to the main frame, which adds its size and the last pointer position (`geometry.frame`: `origin`,
  `inner`, `mouse`). The engine computes the origin of the main page as above and adds the origin of the frame to it.
  So the pointer does not need to be over the frame itself, and scrolling the main page does not break the position
  (after such a scroll, the pointer position over a nested frame is outdated). A parent that does not answer within
  150 ms breaks nothing: the old way (the pointer over the frame) remains. On the Mac the `frame` field is extra and unused.
  `background.js` adds the tab zoom (`chrome.tabs.getZoom`) to the answer: this is the only change in the shared extension
  for this feature, and it has no effect on the Mac (an extra field). The underlines are drawn by the same GNOME Shell
  extension as for GTK applications: with a card on hover, **Ignore** (*Ignoruj*) and **Add to dictionary**
  (*Dodaj do słownika*).
- **Where the window counts from.** Chrome's coordinates (`screenX − window.screenX`) start at the window's *client rect*
  (`frame_rect_to_client_rect` in Mutter), not at its frame: a window that is not maximised has Chrome's shadow around the
  frame — on Wayland 12 px on the left and 10 px at the top, on X11 22 and 19 — and on X11 with the system title bar the
  page counts from below Mutter's frame. That is why the underline view has `from: 'client'` and the Shell extension
  (from version 8) counts from the client rect. Version 7 and older do not know this field and count from the frame,
  as for GTK 4: correct only in a maximised window; in any other window the underlines are shifted right and down by the
  width of the shadow.
- The **badge** sits in the bottom-right corner of the field, inside it (3 px from the edges of a low field, 6 px from
  those of a taller one), as on the Mac: the answer to `live` gives the visible part of the field, the engine converts it
  like the words and adds it to the badge view (`field`, `from: 'client'`), and when the field moves in the window
  (scrolling, resizing), it sends the view again. Without the page position (a nested frame whose parents did not answer,
  and no mouse movement over it), the badge sits as for other applications: at the right edge of the window, at the
  height of the caret.
- Clicking a suggestion is a `replace` in that page field: the extension checks that the word is still as it was and
  inserts the fix with an edit command (editors such as ProseMirror and Gmail see it as typing). When the page has
  changed, the fix does not go through and the engine says so in a message.
- None of this goes to the log: only events (`live_fix` with the result, `via: extension`), never the text or the word.

How we know it works: `integration_engine.py --only panel_live_browser` opens a real Chrome with the extension and a page
with a green marker at a known place, takes a screenshot and checks by pixels that the brick-coloured underline lies
under the word (within 4 px) — first in a window that is not maximised (with the shadow), then in a maximised one — and
that the badge sits in the corner of the field; it clicks a suggestion in the card and reads the page's text back. With
version 7 of the Shell extension this test fails on the window with the shadow. A measurement on Wayland showed that
`event.screenX` is relative to the window here (to its client rect), and that `outerHeight − innerHeight` is the
browser's toolbar (87 px) also at 150% zoom — in a maximised window; in a window that is not maximised the shadow above
and below the window is added. The same measurement at 133% scaling (Wayland) gave the same in logical pixels. Chrome on
X11 with fractional scaling and `xwayland-native-scaling` counts in X11 pixels (2× the logical ones in that measurement):
the engine does not convert this yet.

## Ctrl+Alt+G and dictation in web pages

As on the Mac (`TextBridge.captureViaBrowser`, `replaceViaBrowser`, `insertViaBrowser`), in a browser with a connected
extension **the extension, not IBus, provides the text to correct** (`gramadrama/engine/browser_text.py`). In Chrome
on X11, IBus gives only the refusal “This app does not share its text” (*Ta aplikacja nie udostępnia tekstu*), and on
Wayland a text window around the caret, from which the fix goes in blindly through `delete_surrounding_text` and
`commit_text`. The extension gives the whole field and confirms the write.

- **Ctrl+Alt+G** (and clicking the badge) asks the extension for `context`. From the answer the engine picks what it
  always picks on Linux: the selection or the paragraph with the caret (shared Mac cases in `tests/ime_cases.json`,
  ranges in UTF-16 units). A password field in a page is a refusal, as in applications. When the extension has no field
  (the address bar, a page of the browser itself), the site is disabled in the extension's popup, or the answer did not
  come within 0.8 s, Ctrl+Alt+G goes the IBus way as before (in the log `browser_fallback`). The panel looks the same;
  nothing is shown until the page answers (a few ms).
- **Enter** sends `replace`: the fixes as words, from the end of the text (each leaves the positions of the earlier ones
  unchanged), with a check that the field still holds exactly the text that was read. Short replacements keep links,
  mentions and formatting between the corrected words; more than 12 changes go in as one span from the first to the last.
  A model that changed the number of lines gets the refusal “The model changed line breaks that this app handles itself”
  (*Model zmienił podział na linie*): in a rich field the extension replaces only within a line. The extension reports
  the result: “Applied” (*Zastosowano*),
  “The text changed. Nothing was modified.” (*Tekst zmienił się. Nic nie zmieniono.*; it wrote nothing)
  or “The change could not be verified” (*Nie udało się potwierdzić zmiany*; it may have written,
  but the answer did not come). The extension has 1.2 s to start writing and the engine waits 2.5 s: the write
  never arrives after the engine has stopped waiting. The focus change that IBus sees when the extension sets the focus
  in the field does not interrupt the write.
- **Dictation** in such a browser asks for `context` (the character before the caret: a space before the dictated text,
  as in applications), then sends `insert` to the same field. A password field gets only the clipboard, and so does a
  field other than the one from `context`. When the extension wrote nothing (there is no field, it refused), the text
  goes through IBus as before; when it is not known whether it wrote, the text also goes to the clipboard with the
  message “Insertion could not be verified” (*Nie wiem, czy tekst trafił do pola*). On a site disabled in the
  extension's popup, dictation, like Ctrl+Alt+G, goes the IBus way.
- Log: `browser_request` with the kind (`hotkey`, `apply`, `dictation`, `insert`), the result and the time;
  `apply`/`verified`/`dictation` with `via: extension`. Never the text or the page address.
- An answer that did not come (`timeout`) or a broken connection (`disconnected`) is reported by the hub without
  `attempted`: for a write this means “unknown”, not a refusal.

How we know it works: `integration_engine.py --only browser_page_hotkey_dictation` opens a real Chrome on Xwayland (IBus
gives no text there) with the extension: it corrects a paragraph in a `textarea`, corrects two words in a rich field with
a link between them (the link stays), dictates at the end of the field (with a space) and checks the underline of a word
in a field inside a frame from another domain, over which the pointer has never been. Unit tests:
`tests/test_engine_browser_text.py`.

## Work status

Done: hosting the extension (host, socket, engine, installer, window, site switch), live checking in web pages (text,
badge, underlines, fixing with a suggestion, also in nested frames), Ctrl+Alt+G and dictation through the extension.
**Open**: the page position without any pointer movement over it in a window that is not maximised (the window shadow;
see above), Chrome on X11 with fractional scaling.
