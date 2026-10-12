# GramaDrama browser extension

The extension, shared by Linux and macOS, gives the app the text of the page's active field and
performs replacements. It also provides word positions for underlines and lets you turn off chosen sites.
Without a running app or engine it does not correct text on its own.

## Installation

### Linux

Follow the [Linux instructions](../linux/docs/browser.md). The installer registers the native messaging
host and copies the extension to a fixed directory. In the browser, turn on developer mode,
choose **Load unpacked** (*Załaduj rozpakowane*) and select the folder given by the app.

### macOS

1. Build the app with `./scripts/build-app.sh` and run it. It registers the host
   `pl.gramadrama.browser` in the supported browsers.
2. Open `edge://extensions` or `chrome://extensions`, turn on **Developer mode** (*Tryb dewelopera*) and choose
   **Load unpacked** → the `browser/extension` directory.
3. The extension ID must be `hlnpnkiancoohmafifihcjndfmhilkfe`; it follows from the key in the manifest.

After the files change, reload the extension in the browser's extension manager.
Support for other browsers and for sandboxed packages depends on whether the native messaging host is available.

## Icon and popup

- Lavender icon: GramaDrama works on the page.
- Grey with “off”: checking is turned off for the site.
- Grey with a pink exclamation mark: no connection to the app.

The icons are made from the app's logo (`linux/icon.svg`) with `python3 browser/tools/icons.py`.
The colours of the popup (`popup.css`) and of the icon's badge (`background.js`) come from the palette in `branding/BRAND.md`.

Clicking the icon shows the status and the **Check text on this site** (*Sprawdzaj na tej stronie*) switch.
The list of disabled sites is kept by the app.
A preview of the interface is in `test/popup-preview.html`, also with `?disabled=1` and `?connected=0`.

## Tests

From the repository root:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 -d browser
```

Open the [editing tests](http://127.0.0.1:8765/test/harness.html) and
the [ProseMirror tests](http://127.0.0.1:8765/test/prosemirror.html). The page title shows PASS or FAIL.
These pages check editing behaviour without installing the extension. The host connection, permissions,
focus handling and desktop integration have to be checked with separate integration tests.
