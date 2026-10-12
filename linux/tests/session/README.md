# Isolated GNOME session harness

Runs a headless GNOME Shell 50 (Wayland + Xwayland) with its own D-Bus, IBus, dconf and
home directory, so IBus engines and apps can be driven with real keyboard and pointer
input without touching the desktop you are logged in to.

```sh
cd linux
/usr/bin/python3 tests/session/smoke.py            # harness self-test (about 10-15 s)
/usr/bin/python3 tests/session/smoke.py --repeat 2 --logs /tmp/smoke-logs -v
```

`smoke.py` runs two sessions:

- **stock**, with sources `[('xkb', 'pl')]`: types "Zażółć gęślą jaźń ĄŚĆ é č" into the
  GTK4 test window and checks the result, then types into the Chrome fixture and checks it
  through the console log.
- **component**, with sources `[('ibus', 'gramadrama')]` and the real component
  (`data/gramadrama.xml.in`, exec `gramadrama-engine --ibus`): checks that the
  `IBUS_COMPONENT_PATH` registration works, that typing passes through the engine and that its
  diagnostics (`pl.gramadrama.Engine.Status`) answer.

After each session it checks that nothing is left behind.

## Use from a test

```python
import sys
sys.path.insert(0, '<linux>/tests/session')
from harness import Session, wait_until

with Session(sources=[('ibus', 'gramadrama')],       # component from data/gramadrama.xml.in
             env={'GRAMADRAMA_ENGINE_TEST': '1'},     # exported before gnome-shell starts
             copy_logs_to='/some/dir') as s:
    win = s.spawn_gtk(4)                              # TestWin: D-Bus control + JSONL ground truth
    win.set_text('tv', 'Ala ma kota.')
    s.combo('ctrl', 'end')
    s.type_text(' Zażółć')                            # evdev keycodes on the pl layout
    win.wait_text('tv', 'Ala ma kota. Zażółć')
    s.combo('ctrl', 'alt', 'g')
    print(s.engine_status())                          # pl.gramadrama.Engine Status()
```

### `Session(...)` parameters

| parameter | default | meaning |
|---|---|---|
| `sources` | `[('xkb','pl')]` | GNOME input sources. The first one must become active; readiness waits for it in `ibus engine`. |
| `engine` | `None` | `True` registers the GramaDrama component, `False` never does. `None` registers it when `('ibus','gramadrama')` is among the sources. |
| `component_template` | `linux/data/gramadrama.xml.in` | Template with `@EXEC@`. It is written to `<work>/component/gramadrama.xml`, and `IBUS_COMPONENT_PATH=/usr/share/ibus/component:<work>/component` is exported before gnome-shell starts. |
| `engine_exec` | `/usr/bin/python3 <linux>/gramadrama-engine --ibus` | Value substituted for `@EXEC@` (XML-escaped). |
| `env` | `{}` | Extra environment for the whole session: gnome-shell, ibus-daemon, the engine and apps. Harness-owned keys are rejected: XDG dirs, HOME, DBUS_*, DISPLAY, WAYLAND_DISPLAY, XAUTHORITY, IBUS_* and the GD_* control keys. `GRAMADRAMA_ENGINE_LOG` defaults to `<logs>/engine.log`. |
| `name` | `''` | Label added to the Wayland display name `gdwl-<name>-<random>`. |
| `logs_dir` | `<work>/logs` | Where session, shell, ctl, app, testwin and engine logs are written. |
| `copy_logs_to` | `None` | Directory the logs are copied into during teardown, after all processes are stopped and before the work dir is removed. |
| `size` | `1280x800` | Size of the virtual monitor. |
| `max_seconds` | `1800` | Hard limit on the session's lifetime, enforced by the supervisor. |
| `system_bus` | `False` | `False` gives the session a private, empty system bus. `True` uses the host system bus (logind, GDM, NetworkManager), which you should normally not need. |
| `xwayland` | `True` | `False` starts gnome-shell with `--no-x11`. |
| `start_timeout`, `verbose` | `90`, `False` | Startup limit in seconds, and progress messages on stderr. |
| `extensions` | `()` | Directories of extra GNOME Shell extensions, e.g. `[<linux>/gnome-extension]`. Each is copied (not linked) to `<work>/data/gnome-shell/extensions/<uuid>` (uuid from its `metadata.json`) and enabled next to the harness's own `gd-harness@local`. |

Useful attributes: `work` (`/tmp/gd-XXXXXXXX`), `display`, `logs`, `env`, `bus_address`, `procs`,
and after teardown `report` and `leftovers`. `leftovers` is a dict of processes, mounts,
`work_dir` and `x_files`; all of them are empty or false when teardown was clean.

### Methods

- **Input** (Mutter RemoteDesktop; the session is created and warmed up with a Shift tap during startup):
  - `keys(seq, gap_ms=8)` sends a raw sequence of `['code', evdev, 1|0]`, `['sym', keysym, 1|0]` and `['sleep', ms]` items.
  - `tap(key, mods=())` taps one key, e.g. `s.tap('ret')` or `s.tap('right', ['lshift'])`.
  - `combo('ctrl', 'alt', 'g')` holds the modifiers and taps the last key.
  - `type_text(text, mode='keycodes'|'keysyms')`. The default keycode mode uses real keys of the
    pl layout: Shift for capitals, AltGr for ąćęłńóśźż, and dead keys for é/č and similar
    (`keyseq.COMPOSED`). Keysym mode uses `NotifyKeyboardKeysym`, but mutter only resolves keysyms on
    layout levels 1–3. Uppercase Polish letters (Shift+AltGr), é and emoji are dropped or degraded,
    so keysym mode is only useful for ASCII and lowercase Polish.
  - Key names: `keyseq.KC`, with aliases `ctrl`, `shift`, `alt`, `altgr` (= `ralt`), `super`,
    `esc`, `ret`, `pgup`, `pgdn`, ...
- **Pointer** (relative motion: the pointer is pushed into the top-left corner and then moved,
  and the result is pixel-exact):
  - `pointer_to(x, y)`, `pointer_move(dx, dy)`, `button(pressed, 'left')`
  - `click(x, y, n=1)`: `n=2` is a double click.
  - `drag((x0, y0), (x1, y1))`
  - Screen coordinates for fixture text come from `TestWin.locate(offset)` and `TestWin.rect(widget)`.
- **Apps.** Everything is started inside the session by `ctl.py`. Each app gets the session
  environment plus `DISPLAY`/`XAUTHORITY` of this session's own Xwayland, as in a real GNOME
  session. Before `DISPLAY` is handed out, ctl checks that the display's lock file holds our
  gnome-shell PID.
  - `spawn(argv, env=None, unset=(), log=None, x11=True, name=None)` and `spawn_app(argv, env, name)`
    return a `Proc` with `pid`, `log`, `alive()`, `kill(sig)` and `read_log()`.
    `kill` signals the process group and first verifies that the process belongs to the session.
    For Electron apps such as ChatGPT, pass `--user-data-dir=<s.work>/<name>`. HOME and the XDG
    dirs are isolated anyway.
  - `spawn_gtk(3|4, env=None, name=None)` returns a `TestWin` (see Fixtures). It waits until the
    window is active and the TextView has focus. Pass `env={'GTK_IM_MODULE': 'ibus'}` for the
    direct IBus module path.
  - `spawn_text_editor(path, text=None)` returns a `TextEditor` (gnome-text-editor
    `--standalone`). The file must be inside the work dir; a relative path is taken under
    `<work>/home`, and `text` is written to it first. Its ground truth is `save()`, which presses
    Ctrl+S and returns the file content.
  - `spawn_chrome(url=None, x11=False, extra_args=(), env=None, name=None, app=True)` returns a
    `Chrome`. It uses `--user-data-dir=<work>/chrome-<name>`, `--enable-logging=stderr` and
    `--ozone-platform=wayland|x11`, and opens `--app=<url>` (default: the fixture). For the
    fixture it waits for the page's `loaded` event and for window focus. `s.fixture_url(focus='ta')`
    focuses a field at load with the caret at the end.
- **IBus and the engine.** All commands run inside the session.
  - `ibus_engine_status()` returns `{'engine', 'rc', 'out'}` from `ibus engine`.
  - `ibus_engines()` lists engines (`ibus list-engine --name-only`).
  - `wait_ibus_engine(name)` waits until `ibus engine` reports `name`.
  - `input_source()` returns GNOME's current source (`type`, `id`, `xkbId`, `name`).
  - `set_sources([...])` runs `gsettings` on the isolated dconf. Switching away and back re-activates a killed engine.
  - `engine_status()` calls `pl.gramadrama.Engine.Status` and returns the decoded JSON.
  - `engine_call(dest, path, iface, method, sig=None, args=None)` (same as `call`) makes a generic
    D-Bus call on the session bus and returns the unpacked reply tuple.
  - `engine_pids()`, `pids(regex)` and `kill_pids(pids, sig)` only ever see and signal processes of this session.
- **Shell.**
  - `eval(js)` runs `org.gnome.Shell.Eval` and returns the decoded value; `raw=True` returns
    `(ok, str)`. The test extension exposes `gdMain` (ui/main.js) and `gdKbd` (status/keyboard.js).
  - `shot(path)` takes a PNG screenshot.
  - `windows()`, `window(title)`, `focused_window()` and `activate_window(title)` use
    `buffer`/`frame` rects in screen coordinates. `place_window(title, x, y, w, h)` moves and
    resizes a window (e.g. side by side, so a pointer test never clicks through another window).
- **Misc.**
  - `sh(cmd)` runs a shell command inside the session.
  - `ctl(req)` sends a raw ctl.py request.
  - `wait_until(pred, timeout=5)` polls `pred` and returns its truthy value or raises `TimeoutError`.
  - `tail(logname)` returns the end of a log file.

### Fixtures

**`testwin.py`** (GTK 3 or 4) contains a TextView `tv` with 3 paragraphs, an Entry `entry` and
a password entry `pw` (a GTK4 PasswordEntry, or on GTK3 an invisible Entry with purpose PASSWORD).

It logs JSONL to `<logs>/testwin-<name>.jsonl`: `ready`, `changed` and `sel` (with the full text and
`sel=[cursor, bound]`), `focus`, and `key`. A `key` event has phase `capture`, meaning it reached
the window, or `bubble`, meaning nobody handled it, plus type `press`/`release`, the key name,
keyval, keycode and state.

The `TestWin` handle provides:

- `focus(w)`, `select(w, start, end)`, `set_text(w, text)`
- `get(w)` returns `{'text', 'sel'}`; `text(w)` returns just the text
- `info()`, `present()`
- `locate(offset)` gives screen x/y of a TextView character; `rect(w)` gives a widget's screen rect
- `events(since)`, `mark()`, `keys_since(mark, phase='capture')`
- `wait_event(pred, since)`, `wait_text(w, text)`

The window is driven over D-Bus as `org.gramadrama.TestWin.<name>`.

**`fixture.html`** contains a textarea `ta`, an input `in`, a password field `pw`, a
contenteditable `ce` with `<p>` blocks, and a **rich** contenteditable `rich` whose paragraph
holds `<b>` and `<a>`.

Every `input` event logs `GDLOG {json}` with the state of all fields: values, selections and
`innerHTML`. `keydown`/`keyup` events log key names. `beforeinput`, `composition*`, `focus`,
`blur` and `selectionchange` are logged too. The JSON is pure ASCII, and offsets are Unicode code points,
as in IBus (an emoji counts as 1).

F-key macros (F-keys pass through the input method):

- F9 resets every field; Shift+F9 fills `ta` with 60 paragraphs.
- F10 dumps the state (`Chrome.state()`).
- F2/F3 select "KOT" / put the caret before "słowo" in `ta`; F4/F5 do the same in `in`.
- F6 focuses `pw`.
- F7/F8 do the same as F2/F3 in `ce`; Shift+F2/Shift+F3 in `rich`.
- Shift+F4/Shift+F6 put the caret at the end of `ta`/`in`.
- Shift+F7 logs `geom`: client rects of test words in `ce`, `rich` and `ta` (the textarea through a
  mirror element) and of every field. `mousedown`/`mouseup` are logged with client coordinates,
  so one click on the page background gives the client-to-screen offset (see
  `integration_engine.py`, `Run.calibrate`).

Chrome 154 re-creates its text input (IBus sees a focus change) when F7/F8 or Shift+F2/Shift+F3
replace the selection in an already focused contenteditable, and it sends nothing when a macro
changes nothing. The GramaDrama engine trusts only the update that answers a key after a focus
change, so `integration_engine.py` follows such a macro with one arrow key there and one back
(`Run.nudge_page`), which keeps the macro's caret or selection.

The `Chrome` handle provides `events(since)`, `mark()`, `wait_event(pred, since)`,
`keys_since(mark)`, `state()` (via F10) and `last_state()`.

## Safety guarantees

- **No live-session access.** The harness process imports no `gi` and makes no D-Bus calls.
  Everything in the session goes through `ctl.py`, which refuses to start unless:
  - the session bus address differs from the real one and points into the work dir;
  - XDG_CONFIG/DATA/CACHE/RUNTIME and HOME are all under `/tmp/gd-*`.

  `session.sh` repeats these checks before its first `gsettings` call. The environment is
  whitelisted (PATH, locale, XDG_CURRENT_DESKTOP, IM variables, ...) instead of inherited, so the
  host's `DISPLAY`, `XAUTHORITY`, `GDK_BACKEND`, `ELECTRON_PA_APP_NAME`, `SSH_AUTH_SOCK`,
  `SESSION_MANAGER` and similar never reach the session.
- **Isolated buses.** The session bus socket is `<work>/bus`, from a private dbus config. By
  default there is also a private, empty system bus at `<work>/sysbus`, so gnome-shell cannot
  register with the host's GDM or watch or change the host's logind session.
- **Isolated settings and IBus.** dconf, IBus (address file, cache, `IBUS_COMPONENT_PATH`),
  extensions, keyrings and Chrome/Electron profiles all live in the work dir. Nothing is
  written under `~/.config`, `~/.cache`, `~/.local` or `/usr`, and there are no
  `ibus`/`gsettings` commands outside the session.
- **Guaranteed teardown.** It is owned by `supervise.py`, a separate process in its own
  process session that acts as a child subreaper. It tears down on `stop()`/`__exit__`
  (also on exceptions and KeyboardInterrupt; SIGTERM is turned into SystemExit), when its
  stdin closes because the test process died (including SIGKILL of the test or of its whole
  process group), on SIGTERM/SIGINT, and after `max_seconds`. If the supervisor itself dies,
  `stop()` does the same teardown from the test process.
- **Teardown steps.**
  1. TERM, then KILL, every process that belongs to the session:
     - descendants of the supervisor;
     - processes whose environment carries `GD_WORK=<work>`;
     - Chrome/Electron processes with `--user-data-dir=<work>/`, since they overwrite their environ.

     Never the caller or its ancestors, and never broad `pkill -f`.
  2. `fusermount3 -u` any mount left under the work dir.
  3. Remove the `/tmp/.X<n>-lock` files and `/tmp/.X11-unix/X<n>` sockets of **our** Xwayland,
     and only those: numbers taken from our `shell.log` or lock files holding our gnome-shell PID,
     never files that existed before the session.
  4. Copy the logs if `copy_logs_to` was given.
  5. Remove the work dir.
  6. Check again for leftovers (`Session.leftovers`).
- **Manual recovery.** `/usr/bin/python3 tests/session/supervise.py --cleanup /tmp/gd-XXXXXXXX`
  applies the same rules to one work dir.
- **Test data only.** Only fixture text is typed. Chrome runs with background networking,
  sync and component updates disabled. The rich fixture's link points to `example.invalid`.

## Files

- `harness.py`: the `Session` API. Runs outside the session and imports no `gi`.
- `supervise.py`: owns one session and its teardown; its `--cleanup` mode is for manual recovery.
- `session.sh`: in-session bootstrap. Runs the guards, sets gsettings on the isolated dconf, then starts gnome-shell and ctl.py.
- `ctl.py`: in-session control server (RemoteDesktop keyboard and pointer, spawn, Eval, Screenshot, D-Bus calls).
- `keyseq.py`: pure evdev key sequences for the pl layout.
- `testwin.py`, `fixture.html`: fixtures.
- `smoke.py`: harness self-test.

Nothing here is named `test*.py`, and there is no `__init__.py`, so `unittest discover -s tests`
never imports these files.
