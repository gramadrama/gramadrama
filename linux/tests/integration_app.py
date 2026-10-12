#!/usr/bin/env python3
"""The GramaDrama app (gramadrama/app.py) in an isolated headless GNOME Shell session.

The session is set up like a real installation, inside its own data dir: the engine, the app and
install.json are copied to $XDG_DATA_HOME/gramadrama-engine, the app's .desktop file is installed
(install_engine.install_app), ibus-daemon starts the engine from there without test mode, the panel
extension is installed and enabled, and settings.json points at a stub model server on 127.0.0.1
(tests/stub_server.py). The app runs from that copy with GRAMADRAMA_APP_STATE, so every check reads
the window's state; its window actions are pressed over D-Bus (org.gtk.Actions). For the Aplikacje
section the session's PATH and XDG_DATA_DIRS start in a tree of fake apps (apps_tree), and its
~/.local/bin points into that tree. Nothing here touches the live desktop, its dconf or its IBus.

  legacy_app       (before the app starts) the old GTK app still running in the background is asked
                   to quit by install_engine.stop_legacy_app() (SIGTERM when it does not answer),
                   then its copy is removed
  startup          every row reaches its state; "Wszystko gotowe"; the Aplikacje section is empty
  tabs             the window opens on Start; Alt+1…6 open the tabs and the last one is remembered; a tab with a
                   warning or an error in one of its rows is marked, and the mark goes when the row is well again
  dictionary       Pisanie › Słownik i reguły: the words of Enchant's list (the latest first) and the rules turned off;
                   the bin removes a word from the list file, Włącz turns a rule on in rules.json, the own words are
                   saved, and the Dyktowanie tab links to them
  try              Ctrl+Alt+G in the Wypróbuj field: the arrow-key refusal, the panel, Enter; the field
                   holds the correction, the shortcut counts as confirmed, "Ostatnie użycie" follows
                   and names the app; the GramaDrama app is never on its own list
  apps_list        the Aplikacje rows for a simulated log (written while another window is active) and
                   the fake installs: states, texts, icons and order; an installed Chromium app that was
                   never used is not listed; "Ostatnie użycie" names the app
  apps_fix         "Włącz" writes the wrapper; the row waits for a restart (a fake GPU process on X11),
                   sees the new instance run natively (the GPU process on Wayland, found by the 3 s
                   rescan, no Ctrl+Alt+G; no rescans while the window is minimized, one at once when
                   it is seen again) and works after a simulated correction
  apps_switch      the switch (and the rest of the row) writes apps.json at once and says so; rows keep
                   their places while the user works in the list; a broken apps.json is shown and
                   repaired by the next switch; a write that fails shows its error
  apps_mode        the list beside an app sets the mode Ctrl+Alt+G starts in: apps.json at once and a toast,
                   Popraw takes the entry away, and the mode stays in the file while the app is off
  apps_add         "Dodaj aplikację…": an app on the list cannot be added again, search, Enter adds the
                   first match, a switched-off row has no remove button, the remove button takes it off
                   again; an app without a .desktop file (window:) gets the generic icon
  apps_undo        "Wyłącz poprawkę" deletes the wrapper
  apps_override    "Włącz" for an absolute Exec writes an override, which asks for a new login until
                   GNOME Shell started after it; its undo says when the app grid follows
  apps_repair      a wrapper whose program moved: "Napraw" writes it anew
  cancel           Esc in the panel: the text stays as it was
  address_edit     an address being typed survives a refresh; nothing is saved until it is applied
  login            Pocket ID (oidc.py): "Zaloguj" gives the browser's address, the browser's way back is the real
                   gramadrama:// handler (gio open, registered as the installer does), the servers behind the login
                   then answer; "Wyloguj"; a forged way back is refused; the address pasted in when the handler is missing
  route            at home or away (core.route): the row Połączenie says which way; away without the login it asks for
                   it, with it the servers are the remote ones, at home no token goes to the server; the remote
                   addresses are typed into the rows of the model and LanguageTool and saved
  settings_broken  a settings.json the engine cannot use: the rows say so instead of checking defaults
  other_source     plain "Polski" active (the app notices the switch by itself): Ctrl+Alt+G reaches the
                   app, which names the cause; switching back clears it
  sources_removed  GramaDrama gone from the input sources: the row follows the setting by itself
  conflict         a GNOME custom shortcut on Ctrl+Alt+G is reported by its name
  legacy_shortcut  install_engine.remove_legacy_shortcut() on the session's own dconf
  panel_disabled   the panel extension disabled and enabled again: the row follows the shell's signal
  panel_login      install.json says the panel changed after this login: "log in again"
  copy_differs     the installed copy differs from its repository: the install command is offered
  server           Ollama unreachable, a missing model, the model test (window action try-model)
  show_args        gramadrama-app --show=<tab> as a failed correction's button starts it: the tab, the group ringed
  shots            with --shots DIR: the window in light and dark (with the apps of the apps_* scenarios)
                   and the panel over the Wypróbuj field
  docs_shots       with --docs (and --shots DIR), last of all: the pictures of docs/app/*.png, in the English
                   interface the documentation is written in, with the services of a typical setup

  e1               (only with --e1, in a session of its own) experiment E1 of docs/apps.md: which
                   command GNOME Shell starts after the launch fix appears; see run_e1

usage: /usr/bin/python3 tests/integration_app.py [--only NAME[,NAME...]] [--shots DIR] [--docs] [--logs DIR] [-v]
       /usr/bin/python3 tests/integration_app.py --e1 [--logs DIR] [-v]
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'session'))
sys.path.insert(0, HERE)
sys.path.insert(0, LINUX)
from harness import Session, wait_until  # noqa: E402
from stub_server import CLIENT_ID, Stub, closed_port, sse, stream  # noqa: E402
from gramadrama import apps, checks, core  # noqa: E402  (plain Python, no gi: the texts and scans the app uses)

EXTENSION = os.path.join(LINUX, 'gnome-extension')
UUID = 'gramadrama@gramadrama.local'
DOCS = os.path.join(LINUX, 'docs', 'app')
# ibus-daemon starts the copied engine like the installed one: a direct child (sh execs python3)
# at <data dir>/gramadrama-engine/gramadrama-engine with --ibus, so the panel accepts it.
ENGINE_EXEC = '/bin/sh -c \'exec /usr/bin/python3 "$XDG_DATA_HOME/gramadrama-engine/gramadrama-engine" --ibus\''
TRY_TEXT = 'Czesc, przesylam poprawiona wersje raportu. Jesli bedziesz miec uwagi daj znac do piatku.'
FIXED = 'Cześć, przesyłam poprawioną wersję raportu. Jeśli będziesz mieć uwagi, daj znać do piątku.'
SAMPLE_FIXED = 'Dzięki za info, sprawdzę to jutro rano i dam znać, czy działa.'
BIELIK = 'hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M'
COMMIT = 'test123'
MEDIA = 'org.gnome.settings-daemon.plugins.media-keys'
CUSTOM = MEDIA + '.custom-keybinding'
CUSTOM_ROOT = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
EXTENSIONS = ('org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell.Extensions')
WIDTH, HEIGHT = 640, 2240
SHOT_HEIGHT = 960  # the window of the screenshots: a tab fits in it (the tests' tall window is mostly empty)
SIZE = '1400x2310'  # the virtual monitor: the whole window fits, for the pointer and the screenshots
SELF = 'pl.gramadrama.Linux.desktop'
CHAT, MAIL, MODELER = 'org.gramadrama.Apps.Komunikator.desktop', 'org.gramadrama.Apps.Poczta.desktop', \
    'org.gramadrama.Apps.Modeler.desktop'
# The fake apps of the apps_* scenarios (apps_tree): desktop ID: (Polish name, English name, icon, program, started by
# a bare name). The scenarios assume a Polish session (the system apps' names too) and see the Polish names;
# docs_shots starts the app in English and shows the English ones.
FAKE_APPS = {CHAT: ('Komunikator', 'Messenger', 'internet-chat', 'komunikator', True),
             MAIL: ('Poczta', 'Mail', 'internet-mail', 'poczta', False),
             MODELER: ('Modeler 3D', '3D Modeler', 'applications-graphics', 'modeler', False)}  # installed, never used
# Installed apps of the system, which the simulated log names too
CHROME, EDITOR, TERMINAL, CALCULATOR = ('google-chrome.desktop', 'org.gnome.TextEditor.desktop',
                                        'org.gnome.Ptyxis.desktop', 'org.gnome.Calculator.desktop')
RESCAN = 3  # seconds between the app's rescans while a row waits for a restart (app.py RESCAN_MS)
PANEL_RECT_JS = """(p => p && p.visible ? [...p.get_transformed_position(), ...p.get_transformed_size()].map(Math.round) : null)(
  gdMain.layoutManager.uiGroup.get_children().find(a => a.name === 'gramadrama-panel'))"""

# The tab (window/main.py TABS) each widget of the window's state is on: a click on it opens that tab first
TAB_OF = {'try_view': 'start', 'summary': 'start', 'change_shortcuts': 'start', 'add_app': 'apps', 'site_button': 'apps',
          'primary_language_row': 'writing', 'secondary_language_row': 'writing', 'checking': 'writing',
          'vocabulary_row': 'writing', 'learned_row': 'writing', 'rules_row': 'writing', 'forget': 'writing',
          'rule_on': 'writing', 'dictation_words': 'dictation'}
TAB_OF.update({name: 'connection' for name in (
    'llm_where', 'llm_entry', 'llm_local_entry', 'llm_api_row', 'llm_model_row', 'model_row', 'llm_key_row', 'clear_key',
    'think_row', 'llm_advanced', 'lt_where', 'lt_entry', 'lt_local_entry', 'speech_where', 'whisper_entry', 'whisper_api_row',
    'whisper_model_row', 'whisper_key_row', 'clear_whisper_key', 'remote_setup', 'login_entry', 'login_client_row',
    'login_back_row', 'login_button', 'remote_off', 'pull_entry')})
TAB_NAMES = ('start', 'writing', 'dictation', 'apps', 'connection', 'privacy')  # as the Mac window's tabs

results = []


class Fail(AssertionError):
    pass


def apps_tree(tree):
    """The system side of the apps_* scenarios, installed as apps in /usr and /opt are. Each fake app
    has Chromium's resources.pak next to its launcher. Komunikator starts by a bare name (bin/, a link
    to its launcher, like /usr/bin/chatgpt), so its launch fix is a wrapper; the others start by an
    absolute path. Komunikator's directory also holds a copy of python3: its fake GPU process runs
    from there, as /proc/PID/exe shows it. The session's ~/.local/bin will point to localbin/."""
    for d in ('bin', 'localbin', 'share/applications'):
        os.makedirs(os.path.join(tree, d))
    for app_id, (name, english, icon, program, bare) in FAKE_APPS.items():
        directory = os.path.join(tree, 'opt', program)
        os.makedirs(directory)
        launcher = os.path.join(directory, program + '-launcher')
        with open(launcher, 'w') as f:
            f.write('#!/bin/sh\n# a fake app of tests/integration_app.py: it is never started\nexit 1\n')
        os.chmod(launcher, 0o755)
        open(os.path.join(directory, 'resources.pak'), 'w').close()
        command = launcher
        if bare:
            command = 'gd-' + program
            os.symlink(launcher, os.path.join(tree, 'bin', command))
        with open(os.path.join(tree, 'share', 'applications', app_id), 'w') as f:
            f.write('[Desktop Entry]\nType=Application\nName=%s\nName[pl]=%s\nExec=%s %%U\nIcon=%s\n'
                    % (english, name, command, icon))
    shutil.copy('/usr/bin/python3', os.path.join(tree, 'opt', 'komunikator', 'komunikator'))


def corrected(sid, t, app, win='wayland'):
    """One correction as the engine logs it (session.py): start, result, apply and verified ok."""
    return [{'t': t, 'ev': 'start', 'id': sid, 'scope': 'paragraph', 'chars': 88, 'mode': 'fix', 'app': app, 'win': win},
            {'t': t + 2.1, 'ev': 'result', 'id': sid, 'chars': 90, 'unchanged': False},
            {'t': t + 3.4, 'ev': 'apply', 'id': sid, 'scope': 'paragraph', 'delete': 88, 'commit': 90, 'app': app,
             'win': win},
            {'t': t + 3.5, 'ev': 'verified', 'id': sid, 'outcome': 'ok', 'app': app, 'win': win}]


def refused(t, app, reason, win):
    return {'t': t, 'ev': 'refused', 'at': 'hotkey', 'reason': reason, 'app': app, 'win': win}


def check(cond, msg, *detail):
    if not cond:
        raise Fail(msg + (': ' + ' | '.join(repr(d)[:700] for d in detail) if detail else ''))
    return cond


def note(msg):
    print('    ' + msg, flush=True)


def record(name, ok, detail=''):
    results.append((name, ok))
    print('%s %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok else ': %s' % detail), flush=True)


class Rig:
    def __init__(self, s, stub, args, tree):
        self.s, self.stub, self.args, self.tree = s, stub, args, tree
        self.data = s.env['XDG_DATA_HOME']
        self.target = os.path.join(self.data, 'gramadrama-engine')
        self.settings = os.path.join(s.env['XDG_CONFIG_HOME'], 'gramadrama', 'settings.json')
        self.apps_json = os.path.join(s.env['XDG_CONFIG_HOME'], 'gramadrama', 'apps.json')
        self.state_path = os.path.join(s.work, 'app-state.json')
        self.app = None
        self.other = None  # a window of another app (apps_*): the log's events come while it is active
        self.gpu = None  # the fake GPU process of Komunikator
        self.logged = {}  # the times of the simulated log (apps_setup), once it is written

    # ---- setup ----
    def install(self):
        """What install_engine.py install leaves behind, in the session's own data dir."""
        os.makedirs(self.target)
        shutil.copytree(os.path.join(LINUX, 'gramadrama'), os.path.join(self.target, 'gramadrama'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('gramadrama-engine', 'gramadrama-app', 'icon.svg'):
            shutil.copy2(os.path.join(LINUX, name), os.path.join(self.target, name))
        shutil.copy(os.path.join(self.s.env['IBUS_COMPONENT_PATH'], 'gramadrama.xml'),
                    os.path.join(self.target, 'gramadrama.xml'))  # the staged copy matches IBus's
        self.write_info()
        # ~/.local/bin, where the launch fix writes its wrappers, is the first directory of the session's PATH
        os.makedirs(os.path.join(self.s.env['HOME'], '.local'), exist_ok=True)
        os.symlink(os.path.join(self.tree, 'localbin'), os.path.join(self.s.env['HOME'], '.local', 'bin'))
        # The app's .desktop file, as install_engine.py install writes it: GNOME Shell then knows the
        # window as pl.gramadrama.Linux.desktop, and the engine logs that ID for the Wypróbuj field.
        self.installer('I.install_app()')

    def write_info(self, **extra):
        info = {'commit': COMMIT, 'installed_at': datetime.datetime.now().isoformat(timespec='seconds'),
                'source': LINUX}
        info.update(extra)
        with open(os.path.join(self.target, 'install.json'), 'w') as f:
            json.dump(info, f)

    def write_settings(self, **overrides):
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        data = self.stub.settings(model=BIELIK)
        data.update(overrides)
        with open(self.settings, 'w') as f:
            json.dump(data, f)

    def start_app(self, env=None):
        wait_until(lambda: self.s.eval('imports.gi.Shell.AppSystem.get_default().lookup_app(%s) !== null'
                                       % json.dumps(SELF)), 30, what='GNOME Shell to know the app')
        self.app = self.s.spawn_app(['/usr/bin/python3', os.path.join(self.target, 'gramadrama-app')],
                                    env={'GRAMADRAMA_APP_STATE': self.state_path, 'GRAMADRAMA_ROUTE_LOOPBACK': '1',
                                         **(env or {})}, name='app')
        # 'Checking…' in English (docs_shots); the window can be built before GNOME Shell lists it
        wait_until(lambda: self.state().get('summary') not in (None, '', 'Sprawdzam…', 'Checking…'), 30,
                   what='the first checks')
        wait_until(lambda: self.s.window('GramaDrama'), 10, what='the window on screen')
        self.s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)
        wait_until(lambda: self.state().get('active') and self.state()['try']['focus'], 10,
                   what='the app active with the Wypróbuj field focused')

    # ---- the app ----
    def state(self):
        try:
            with open(self.state_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def row(self, name):
        return self.state()['rows'][name]

    def wait_row(self, name, state, text=None, timeout=10):
        def ok():
            row = self.state().get('rows', {}).get(name)
            return row and row['state'] == state and (text is None or text in row['text']) and row
        try:
            return wait_until(ok, timeout, what='%s: %s %r' % (name, state, text))
        except TimeoutError:
            raise Fail('%s is not %s %r' % (name, state, text or ''), self.state().get('rows', {}).get(name))

    def wait_try(self, state, text, timeout=10):
        def ok():
            t = self.state().get('try', {})
            return t.get('state') == state and text in t.get('text', '') and t
        try:
            return wait_until(ok, timeout, what='Wypróbuj: %s %r' % (state, text))
        except TimeoutError:
            raise Fail('Wypróbuj is not %s %r' % (state, text), self.state().get('try'))

    def refresh(self):
        """Ctrl+R in the app, and wait for the answer from the stub."""
        before = len(self.stub.requests)
        self.s.combo('ctrl', 'r')
        wait_until(lambda: len(self.stub.requests) > before, 5, what='a new check at the stub')
        time.sleep(0.8)

    def refresh_quietly(self):
        """Ctrl+R when the app will not ask any server (nobody is logged in): nothing arrives at the stub to wait for."""
        self.s.combo('ctrl', 'r')
        time.sleep(1.5)

    def action(self, name):
        return self.s.call('pl.gramadrama.Linux', '/pl/gramadrama/Linux/window/1', 'org.gtk.Actions', 'Activate',
                           '(sava{sv})', [name, [], {}])

    def geometry(self, get, what):
        """Where a widget is, from a state the app writes on request (window action write-state): after
        the layout of the last change, so twice the same in a row."""
        seen = []

        def settled():
            time.sleep(0.1)
            self.action('write-state')
            seen.append(get(self.state()))
            return seen[-1] is not None and seen[-2:] == [seen[-1]] * 2 and seen[-1]
        return wait_until(settled, 5, what='%s on screen' % what)

    def show_tab(self, tab):
        """The window's tab (app.py TABS) open, by its action: the widgets of the others are not on screen."""
        if self.state().get('tab') != tab:
            self.action('tab-' + tab)
            wait_until(lambda: self.state().get('tab') == tab, 5, what='the %s tab' % tab)
            time.sleep(0.3)

    def click_widget(self, name, dx=None, dy=None):
        """Click a widget the app reports in its state (window coordinates + the window's frame); its tab opens first."""
        if name in TAB_OF:
            self.show_tab(TAB_OF[name])
        frame = self.s.window('GramaDrama')['frame']
        x, y, w, h = self.geometry(lambda state: state.get('geometry', {}).get(name), name)
        self.s.click(frame[0] + x + (w // 2 if dx is None else dx), frame[1] + y + (h // 2 if dy is None else dy))

    def toggle(self, name, index, count=2):
        """Click the index-th choice of a segmented choice (a toggle group: "Gdzie", "Sposób sprawdzania")."""
        if name in TAB_OF:
            self.show_tab(TAB_OF[name])
        frame = self.s.window('GramaDrama')['frame']
        x, y, w, h = self.geometry(lambda state: state.get('geometry', {}).get(name), name)
        self.s.click(frame[0] + x + w * (2 * index + 1) // (2 * count), frame[1] + y + h // 2)

    def choose(self, name, index):
        """A combo row's popover: open it, Home, down to the entry, Enter (as for the Aplikacje modes)."""
        self.click_widget(name)
        time.sleep(0.5)
        self.s.tap('home')
        time.sleep(0.25)
        for _ in range(index):
            self.s.tap('down')
            time.sleep(0.25)
        self.s.tap('ret')

    def type_into(self, name, text, enter=True):
        self.click_widget(name, dx=60)
        self.s.combo('ctrl', 'a')
        if text:
            self.s.type_text(text, mode='keysyms')
        else:
            self.s.tap('backspace')
        if enter:
            self.s.tap('ret')

    def saved_settings(self):
        with open(self.settings) as f:
            return json.load(f)

    def focus_try(self):
        self.click_widget('try_view', dx=30)
        wait_until(lambda: self.state()['try']['focus'] and self.state()['active'], 5,
                   what='the Wypróbuj field focused')

    def owner(self, name):
        return self.s.call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner',
                           '(s)', [name])[0]

    def installer(self, code):
        """install_engine.py functions inside the session (its dconf, its bus, its data dir)."""
        r = self.s.sh('/usr/bin/python3 -c "import sys; sys.path.insert(0, \'%s\'); import install_engine as I; %s"'
                      % (LINUX, code))
        check(r['rc'] == 0, 'install_engine in the session', r)
        return r['out'].strip()

    def nudge(self):
        """A real caret move: the engine trusts the field's state only after one (docs/architecture.md, Text safety)."""
        self.s.tap('left')
        self.s.tap('right')
        # The app's text reaches the engine late in this 640x2240 software-rendered window: under 0.05 s with
        # the page as it was, 0.3-0.6 s once the Podczas pisania section gained a switch (measured). The
        # engine shows no freshness outside test mode, so there is nothing to wait on but the time.
        time.sleep(1.0)

    def gsettings(self, *args):
        r = self.s.sh('gsettings ' + ' '.join("'%s'" % a.replace("'", "'\\''") for a in args))
        check(r['rc'] == 0, 'gsettings %s' % ' '.join(args), r['err'])
        return r['out'].strip()

    def activate_source(self, index):
        self.s.eval('gdKbd.getInputSourceManager().inputSources[%d].activate(true); true' % index)
        want = 'gramadrama' if index == 0 else None
        wait_until(lambda: (self.s.ibus_engine_status()['engine'] == 'gramadrama') == (want is not None), 10,
                   what='input source %d active' % index)

    def shot_window(self, path, with_panel=False):
        """The window; with_panel: its top part and the GramaDrama panel over it."""
        x, y, w, h = self.s.window('GramaDrama')['frame']
        if with_panel:
            panel = self.s.eval(PANEL_RECT_JS)
            check(panel, 'the panel is on screen')
            px, py, pw, ph = panel
            w, h = max(x + w, px + pw + 16) - x, py + ph + 24 - y
        r = self.s.call('org.gnome.Shell.Screenshot', '/org/gnome/Shell/Screenshot', 'org.gnome.Shell.Screenshot',
                        'ScreenshotArea', '(iiiibs)', [x, y, w, h, False, path])
        check(r[0], 'screenshot %s' % path, r)

    # ---- the Aplikacje section ----
    def apps(self):
        return self.state().get('apps') or {}

    def apps_file(self):
        """apps.json as it is, or None."""
        try:
            with open(self.apps_json, encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return None

    def app_row(self, app_id):
        return next((row for row in self.apps().get('rows', []) if row['id'] == app_id), None)

    def order(self):
        return [row['id'] for row in self.apps().get('rows', [])]

    def wait_app(self, app_id, state, text=None, timeout=10):
        """The row of app_id in state (apps.RowState.state), its subtitle holding text."""
        def ok():
            row = self.app_row(app_id)
            return row and row['state'] == state and (text is None or text in row['text']) and row
        try:
            return wait_until(ok, timeout, what='%s: %s %r' % (app_id, state, text))
        except TimeoutError:
            raise Fail('%s is not %s %r' % (app_id, state, text or ''), self.app_row(app_id), self.order())

    def click_app(self, app_id, part):
        """Click a part of an app's row: 'row' (its middle, the name), 'button', 'remove' or 'switch'."""
        def get(state):
            row = next((r for r in (state.get('apps') or {}).get('rows', []) if r['id'] == app_id), None)
            return row and row['geometry'][part]
        self.show_tab('apps')
        frame = self.s.window('GramaDrama')['frame']
        x, y, w, h = self.geometry(get, 'the %s of %s' % (part, app_id))
        self.s.click(frame[0] + x + w // 2, frame[1] + y + h // 2)

    def log(self, *events):
        """Engine events appended to the session's engine log as the engine writes them: a line each,
        in one write. The app follows the log."""
        data = ''.join(json.dumps(e, separators=(',', ':'), ensure_ascii=False) + '\n' for e in events).encode()
        fd = os.open(self.s.env['GRAMADRAMA_ENGINE_LOG'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def elsewhere(self):
        """Another app's window active, as when Ctrl+Alt+G is used there; placed beside the app's
        window, so that both stay in view."""
        if self.other is None or not self.other.alive():
            self.other = self.s.spawn_gtk(4, name='other')
            self.s.place_window(self.other.title, WIDTH + 80, 30, 600, 480)
        self.s.activate_window(self.other.title)
        wait_until(lambda: self.state().get('active') is False, 5, what='the app inactive')

    def here(self):
        self.s.activate_window('GramaDrama')
        wait_until(lambda: self.state().get('active'), 5, what='the app active')

    def minimize(self, minimized=True):
        """The app's window minimized: GNOME Shell then tells it that it cannot be seen (GTK's
        suspended state). minimized=False shows it again without activating it, so that the app's
        refresh on activation plays no part; here() brings it back active."""
        js = ('(w => w ? (w.%s(), true) : false)(global.get_window_actors().map(a => a.meta_window)'
              '.find(w => (w.get_title() || "").includes("GramaDrama")))' % ('minimize' if minimized else 'unminimize'))
        check(self.s.eval(js), 'the window %s' % ('minimized' if minimized else 'shown again'))
        wait_until(lambda: self.state().get('suspended') is minimized, 10,
                   what='the window %s' % ('suspended' if minimized else 'no longer suspended'))

    def back(self):
        """The app active with the Wypróbuj field focused, as the other scenarios expect it."""
        self.here()
        self.focus_try()

    def gpu_process(self, platform):
        """Komunikator's GPU process as Chromium starts one: from the app's directory, with the platform
        in its arguments (apps.gpu_platforms reads both from /proc). A running one quits first."""
        if self.gpu is not None:
            self.gpu.kill()
        program = os.path.join(self.tree, 'opt', 'komunikator', 'komunikator')
        self.gpu = self.s.spawn([program, '-c', 'import time; time.sleep(3600)', '--type=gpu-process',
                                 '--ozone-platform=' + platform], name='gpu-' + platform, x11=False)
        directory = Path(self.tree, 'opt', 'komunikator')
        wait_until(lambda: apps.gpu_platforms().get(directory) == platform, 5, what='the GPU process on ' + platform)

    def apps_setup(self):
        """The simulated past of the apps_* scenarios, written once while another window is active, as
        in real use: corrections in Google Chrome and the text editor, a refusal in the terminal and X11
        refusals in the two fake Chromium apps, Komunikator still running on X11 (its GPU process)."""
        if self.logged:
            return
        self.elsewhere()
        self.gpu_process('x11')
        now, events, sid = time.time(), [], 90000
        for app, count, last in ((CHROME, 12, now - 1500), (EDITOR, 3, now - 2400)):
            for n in range(count):
                sid += 1
                events += corrected(sid, last - 60 * (count - 1 - n), app)
        events += [refused(now - 900, TERMINAL, 'terminal', 'wayland'), refused(now - 600, MAIL, 'no_text', 'x11'),
                   refused(now - 300, CHAT, 'no_text', 'x11')]
        self.log(*events)
        self.logged = {'chrome_ok': now - 1500 + 3.5, 'editor_ok': now - 2400 + 3.5, 'chat': now - 300}


# ---- tests ------------------------------------------------------------------------------------------------
# What the old GTK app does that matters here: Gio.Application with the app's id, held without a
# window, with an app.quit action, started as `python3 -m gramadrama` inside its copy.
LEGACY_MAIN = '''import gi
from gi.repository import Gio
app = Gio.Application(application_id='pl.gramadrama.Linux', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
def startup(a):
    a.hold()
    quit = Gio.SimpleAction.new('quit', None)
    quit.connect('activate', lambda *_: a.quit())
    a.add_action(quit)
app.connect('startup', startup)
app.connect('activate', lambda a: None)
app.run([])
'''


def t_legacy_app(rig):
    """Runs before the app starts: the old app would own pl.gramadrama.Linux and take its place."""
    legacy = os.path.join(rig.data, 'gramadrama')
    os.makedirs(os.path.join(legacy, 'gramadrama'))
    for name, text in (('__init__.py', ''), ('__main__.py', LEGACY_MAIN), ('app.py', '# the old GTK app\n')):
        with open(os.path.join(legacy, 'gramadrama', name), 'w') as f:
            f.write(text)
    proc = rig.s.spawn(['/bin/sh', '-c', 'cd "$XDG_DATA_HOME/gramadrama" && exec /usr/bin/python3 -m gramadrama'],
                       name='legacy')
    wait_until(lambda: rig.owner('pl.gramadrama.Linux'), 10, what='the old app on the bus')
    check(rig.installer('print(I.stop_legacy_app(), I.remove_legacy_copy())') == 'True True', 'stopped and removed')
    wait_until(lambda: not proc.alive(), 5, what='the old app gone')
    check(not rig.owner('pl.gramadrama.Linux'), 'the name is free for the new app')
    check(not os.path.exists(legacy), 'its copy is gone')
    check(rig.installer('print(I.stop_legacy_app())') == 'None', 'nothing to stop the second time')
    # an old app that does not answer the quit action is stopped with SIGTERM
    os.makedirs(os.path.join(legacy, 'gramadrama'))
    for name, text in (('__init__.py', ''), ('__main__.py', LEGACY_MAIN.replace("'quit'", "'nothing'"))):
        with open(os.path.join(legacy, 'gramadrama', name), 'w') as f:
            f.write(text)
    proc = rig.s.spawn(['/bin/sh', '-c', 'cd "$XDG_DATA_HOME/gramadrama" && exec /usr/bin/python3 -m gramadrama'],
                       name='legacy-deaf')
    wait_until(lambda: rig.owner('pl.gramadrama.Linux'), 10, what='the deaf old app on the bus')
    check(rig.installer('print(I.stop_legacy_app())') == 'True', 'stopped by SIGTERM')
    wait_until(lambda: not proc.alive(), 5, what='the deaf old app gone')
    shutil.rmtree(legacy)


def t_startup(rig):
    rows = {name: rig.row(name) for name in rig.state()['rows']}
    check(rows['hotkey'] == {'state': 'ok', 'text': 'Wolny — żaden skrót GNOME go nie przechwytuje'}, 'hotkey', rows['hotkey'])
    check(rows['source'] == {'state': 'ok', 'text': 'GramaDrama — aktywne'}, 'source', rows['source'])
    engine = rig.wait_row('engine', 'ok', 'Działa · wersja %s, zainstalowana dziś ' % COMMIT)
    note('engine: %s' % engine['text'])
    version = json.load(open(os.path.join(EXTENSION, 'metadata.json')))['version']
    rig.wait_row('panel', 'ok', 'Działa · wersja %s' % version)
    check(rows['last']['state'] == 'info', 'no use yet', rows['last'])
    rig.wait_row('llm', 'ok', 'Działa · ')
    rig.wait_row('lt', 'ok', 'Działa · ')
    model = rig.state()['model']
    check(model['state'] == 'ok' and 'Wczyta się' in model['text'] and model['names'] == [BIELIK], 'model', model)
    check(rig.state()['summary'] == 'Wszystko gotowe', 'summary', rig.state()['summary'])
    check(rig.state()['try'] == {'state': 'info', 'text': 'Czekam na skrót korekty…', 'buffer': TRY_TEXT, 'focus': True},
          'the Wypróbuj field', rig.state()['try'])
    # no app used yet: the Aplikacje section says where its rows will come from
    wait_until(lambda: rig.apps().get('scans'), 10, what='the first scan of the apps')
    section = rig.apps()
    check(section['rows'] == [] and section['empty'] and section['problem'] is None and section['dialog'] is None,
          'the empty Aplikacje section', section)
    rig.show_tab('apps')
    check(rig.state()['geometry']['add_app'], '"Dodaj aplikację…" on screen')
    rig.show_tab('start')
    errors = [line for line in rig.app.read_log().splitlines() if 'WARNING' in line or 'CRITICAL' in line or 'Traceback' in line]
    check(not errors, 'no GTK warnings or tracebacks from the app', *errors[:3])



def t_languages_shortcuts(rig):
    """Single language and DE/EN settings affect the UI; custom correction works without engine restart."""
    s = rig.s
    def refresh():
        before = len(rig.stub.requests)
        rig.action('refresh')
        wait_until(lambda: len(rig.stub.requests) > before, 5, what='settings refresh')
        time.sleep(0.8)
    def choose(name, index):
        rig.click_widget(name)
        time.sleep(0.3)
        s.tap('home')
        for _ in range(index):
            s.tap('down')
        s.tap('ret')
    try:
        check(rig.state()['ui_language'] == 'pl' and rig.state()['tab_titles']['writing'] == 'Pisanie',
              'default interface is Polish', rig.state()['tab_titles'])
        rig.show_tab('writing')
        if rig.args.shots:
            rig.shot_window(os.path.join(rig.args.shots, 'writing-pl.png'))
        rig.write_settings(primary_language='de', secondary_language='en', correction_shortcut='Ctrl+Shift+K')
        refresh()
        state = rig.state()
        check(state['languages'] == ['de', 'en'] and state['translation_enabled'], 'DE/EN is active', state)
        check(state['ui_language'] == 'en' and list(state['tab_titles'].values()) ==
              ['Home', 'Writing', 'Dictation', 'Apps', 'Services', 'Privacy'],
              'German primary uses the complete English interface', state['tab_titles'])
        check(state['tab'] == 'writing', 'language refresh preserves the active tab', state['tab'])
        check(state['dictation_languages'] == ['de', 'en', 'auto'], 'dictation follows the selected pair', state)
        check(state['shortcut_labels'][0] == 'Ctrl+Shift+K', 'custom shortcut shown', state['shortcut_labels'])
        rig.show_tab('writing')
        if rig.args.shots:
            rig.shot_window(os.path.join(rig.args.shots, 'writing-de-en.png'))
            rig.show_tab('dictation')
            rig.shot_window(os.path.join(rig.args.shots, 'dictation-de-en.png'))
            for tab in ('start', 'apps', 'connection', 'privacy'):
                rig.show_tab(tab)
                rig.shot_window(os.path.join(rig.args.shots, tab + '-en.png'))
            rig.show_tab('writing')
        # Drafts must survive a DE -> PL rebuild in the same window.
        draft, draft_url = 'Synthetic user draft for language switching.', 'http://192.0.2.44:8080'
        def address_typed():
            rig.action('write-state')  # an entry writes no state as it changes
            return rig.state()['entries']['llm'] == draft_url
        # The typed keys reach the app through the compositor and the input method after type_text returns, the tab
        # actions over D-Bus at once: each draft is in its field before another tab is opened and the language changed.
        rig.focus_try()
        s.combo('ctrl', 'a')
        s.type_text(draft, mode='keysyms')
        wait_until(lambda: rig.state()['try']['buffer'] == draft, 5, what='the draft typed in Try it')
        rig.type_into('llm_entry', draft_url, enter=False)
        wait_until(address_typed, 5, what='the draft address typed')
        choose('primary_language_row', 0)
        wait_until(lambda: rig.state()['ui_language'] == 'pl' and rig.state()['languages'] == ['pl', 'en'],
                   5, what='Polish interface restored immediately')
        state = rig.state()
        check(state['tab'] == 'writing' and state['tab_titles']['writing'] == 'Pisanie',
              'PL switch preserves the tab', state['tab'])
        check(state['entries']['llm'] == draft_url and state['try']['buffer'] == draft,
              'an unsaved address and user text survive UI rebuild', state['entries'], state['try'])
        check(json.load(open(rig.settings))['llm_url'] == rig.stub.url, 'the draft address has not been saved')
        # EN primary + PL secondary is English too; then return to DE/EN for the pair swap below.
        choose('primary_language_row', 1)
        wait_until(lambda: rig.state()['ui_language'] == 'en' and rig.state()['languages'] == ['en', 'pl'],
                   5, what='English primary with Polish secondary')
        check(rig.state()['tab_titles']['writing'] == 'Writing', 'EN/PL uses English')
        rig.write_settings(primary_language='de', secondary_language='en', correction_shortcut='Ctrl+Shift+K')
        refresh()
        # Select EN as primary: it swaps with DE, retaining a valid distinct pair.
        choose('primary_language_row', 1)
        wait_until(lambda: rig.state()['languages'] == ['en', 'de'], 5, what='primary language saved')
        choose('secondary_language_row', 0)
        wait_until(lambda: rig.state()['languages'] == ['en'] and not rig.state()['translation_enabled']
                   and rig.state()['dictation_languages'] == ['en'], 5, what='single English applied')
        state = rig.state()
        check(state['ui_language'] == 'en' and not state['translation_enabled'] and state['dictation_languages'] == ['en'],
              'single language disables translation and the alternative dictation language', state)
        if rig.args.shots:
            rig.shot_window(os.path.join(rig.args.shots, 'writing-en-only.png'))
        # Restore the default text pair for the synthetic Polish sentence, retaining the custom shortcut.
        rig.write_settings(correction_shortcut='Ctrl+Shift+K')
        refresh()
        rig.show_tab('start')
        rig.focus_try()
        s.combo('ctrl', 'a')
        s.type_text(TRY_TEXT, mode='keysyms')
        rig.nudge()
        rig.stub.chunks = stream(FIXED, pieces=4)
        s.combo('ctrl', 'shift', 'k')
        rig.wait_try('ok', 'Gotowe. Enter wstawi poprawkę', timeout=15)
        s.tap('esc')
        errors = [line for line in rig.app.read_log().splitlines() if 'CRITICAL' in line or 'Traceback' in line]
        check(not errors, 'no UI failures while changing languages and shortcuts', errors)
    finally:
        rig.write_settings()
        refresh()
        rig.type_into('llm_entry', rig.stub.url)  # the draft goes: the saved address again
        rig.show_tab('start')

def t_dictionary(rig):
    config = rig.s.env['XDG_CONFIG_HOME']
    words_file = os.path.join(config, 'enchant', 'pl_PL.dic')  # Enchant's personal list, which the panel's card adds to
    rules_file = os.path.join(config, 'gramadrama', 'rules.json')
    os.makedirs(os.path.dirname(words_file), exist_ok=True)
    with open(words_file, 'w', encoding='utf-8') as f:
        f.write('Bielik\nOrion\n')
    with open(rules_file, 'w', encoding='utf-8') as f:
        json.dump({'off': {'ZE_ZE': 'Że czy ze'}}, f, ensure_ascii=False)

    def dictionary():
        rig.action('write-state')
        return rig.state().get('dictionary') or {}
    try:
        before = len(rig.stub.requests)
        rig.action('refresh')
        wait_until(lambda: len(rig.stub.requests) > before, 5, what='settings refresh')
        rig.show_tab('writing')
        wait_until(lambda: dictionary().get('words') == ['Orion', 'Bielik'], 5, what='the words listed, the latest first')
        check(dictionary()['rules'] == ['Że czy ze'], 'the rule turned off is listed by its description', dictionary())
        for name in ('learned_row', 'rules_row'):
            check(rig.geometry(lambda state: state['geometry'].get(name), name)[3] < 80, 'closed at first: %s' % name)
            rig.click_widget(name, dy=24)  # the header of the expander row: open it
        rig.geometry(lambda state: state['geometry'].get('forget'), 'the bin of the first word')  # the lists open
        rig.geometry(lambda state: state['geometry'].get('rule_on'), 'Włącz of the first rule')
        if rig.args.shots:
            time.sleep(0.5)  # the expander's animation
            rig.shot_window(os.path.join(rig.args.shots, 'dictionary.png'))
        rig.click_widget('forget')
        wait_until(lambda: open(words_file, encoding='utf-8').read() == 'Bielik\n', 5, what='Orion out of the list file')
        wait_until(lambda: dictionary().get('words') == ['Bielik'], 5, what='the list without Orion')
        rig.geometry(lambda state: state['geometry'].get('forget'), 'the list still open')
        check(rig.state()['toast'] == 'Usunięto ze słownika: Orion', 'the removal is said', rig.state()['toast'])
        rig.click_widget('rule_on')
        wait_until(lambda: json.load(open(rules_file, encoding='utf-8')) == {'off': {}}, 5, what='the rule on in rules.json')
        wait_until(lambda: dictionary().get('rules') == [], 5, what='the list without the rule')
        check(dictionary()['rules_subtitle'].startswith('Brak.'), 'no rules turned off', dictionary())
        rig.type_into('vocabulary_row', 'GramaDrama, Kasia')
        wait_until(lambda: rig.saved_settings().get('dictation_vocabulary') == 'GramaDrama, Kasia', 5,
                   what='the own words saved')
        check(rig.state()['toast'] == 'Zapisano. Sprawdzanie, korekta i dyktowanie użyją tych słów.',
              'the own words serve everything', rig.state()['toast'])
        rig.show_tab('dictation')
        rig.click_widget('dictation_words')
        wait_until(lambda: rig.state().get('tab') == 'writing', 5, what='Dyktowanie leads to the dictionary')
    finally:
        for path in (words_file, rules_file):
            if os.path.exists(path):
                os.remove(path)
        rig.write_settings()


def t_tabs(rig):
    """The window's tabs (app.py TABS, those of the Mac window): it opens on Start, Alt+1…6 open the tabs in the order of
    the bar and the last one is remembered, a tab with a warning or an error in one of its rows is marked, and the mark
    goes when the row is well again."""
    s = rig.s
    state = rig.state()
    check(state['tab'] == 'start', 'the window opens on Start', state['tab'])
    check(tuple(state['attention']) == TAB_NAMES, 'the six tabs', state['attention'])
    for key, tab in (('2', 'writing'), ('3', 'dictation'), ('4', 'apps'), ('5', 'connection'), ('6', 'privacy'), ('1', 'start')):
        s.combo('alt', key)
        wait_until(lambda: rig.state()['tab'] == tab, 5, what='Alt+%s opens %s' % (key, tab))
    window_json = os.path.join(s.env['XDG_STATE_HOME'], 'gramadrama', 'window.json')  # the tab the window is left on
    s.combo('alt', '4')
    wait_until(lambda: rig.state()['tab'] == 'apps', 5, what='Alt+4')
    wait_until(lambda: os.path.exists(window_json) and json.load(open(window_json)).get('tab') == 'apps', 5,
               what='the last tab remembered')
    s.combo('alt', '1')
    wait_until(lambda: json.load(open(window_json)).get('tab') == 'start', 5, what='the last tab remembered again')
    rig.refresh()
    check(not rig.state()['attention']['connection'], 'the servers are well: the tab is not marked', rig.state()['attention'])
    try:
        rig.write_settings(llm_url='http://127.0.0.1:%d' % closed_port())  # nothing listens there
        rig.refresh_quietly()
        wait_until(lambda: rig.state()['attention']['connection'], 10, what='the Usługi tab marked')
        check(rig.state()['rows']['llm']['state'] == 'error', 'the row it is for', rig.state()['rows']['llm'])
        check(not rig.state()['attention']['writing'] and not rig.state()['attention']['privacy'], 'only that tab',
              rig.state()['attention'])
    finally:
        rig.write_settings()
        rig.refresh()
    wait_until(lambda: not rig.state()['attention']['connection'], 10, what='the mark gone')


def t_try(rig):
    s = rig.s
    rig.action('focus-try')  # the focus moved into the field without a click, as at the start: the caret is not known yet
    wait_until(lambda: rig.state()['try']['focus'], 5, what='the Wypróbuj field focused')
    time.sleep(0.5)
    s.combo('ctrl', 'alt', 'g')  # right after the field got focus: the engine asks for an arrow key
    rig.wait_try('warning', 'Skrót działa, ale GramaDrama nie zna jeszcze pozycji kursora')
    rig.wait_row('hotkey', 'ok', 'sprawdzone w polu Wypróbuj')
    rig.stub.chunks, rig.stub.chunk_delay = stream(FIXED, pieces=4), 0.25
    chats = len(rig.stub.chats())
    rig.nudge()
    s.combo('ctrl', 'alt', 'g')
    rig.wait_try('busy', 'Skrót działa. Model poprawia tekst')
    rig.wait_try('ok', 'Gotowe. Enter wstawi poprawkę', timeout=15)
    check(len(rig.stub.chats()) == chats + 1, 'one request to Bielik', len(rig.stub.chats()))
    check(TRY_TEXT in rig.stub.chats()[-1]['messages'][-1]['content'], 'the field text was sent')
    if rig.args.shots:
        time.sleep(0.5)
        rig.shot_window(os.path.join(rig.args.shots, 'try-panel-light.png'), with_panel=True)
    s.tap('ret')
    rig.wait_try('ok', 'Zastosowano. Skrót, silnik i serwer działają.')
    check(rig.state()['try']['buffer'] == FIXED, 'the field holds the correction', rig.state()['try']['buffer'])
    rig.wait_row('last', 'ok', 'GramaDrama · Popraw · akapit · zastosowano')  # the app of the log's start
    check(rig.state()['summary'] == 'Wszystko gotowe', 'summary', rig.state()['summary'])
    # the engine logged the app (the extension's Focus): GramaDrama itself, which is never on its own list
    with open(s.env['GRAMADRAMA_ENGINE_LOG'], 'rb') as f:
        started = [e for e in checks.parse_log(f.read()) if e['ev'] == 'start']
    check((started[-1].get('app'), started[-1].get('win')) == (SELF, 'wayland'), 'the app in the log', started[-1])
    check(rig.apps()['rows'] == [] and rig.apps()['empty'], 'the list stays empty', rig.apps())


def sorted_as_the_spec_says(rows):
    """By state (apps.ORDER), then name: what the list shows when the user is not working in it."""
    return [r['id'] for r in sorted(rows, key=lambda r: (apps.ORDER.index(r['state']), r['name'].casefold(), r['id']))]


def t_apps_list(rig):
    rig.apps_setup()
    # the live desktop's Chrome is on this machine too: its GPU process tells how it runs (on X11, the
    # Chrome row asks for the fix even after a correction)
    chrome = 'fixable' if apps.gpu_platforms().get(Path('/opt/google/chrome')) == 'x11' else 'works'
    for app_id, state in ((CHAT, 'fixable'), (MAIL, 'fixable'), (CHROME, chrome), (EDITOR, 'works'),
                          (TERMINAL, 'unsupported')):
        rig.wait_app(app_id, state, timeout=15)
    section = rig.apps()
    rows = {row['id']: row for row in section['rows']}
    check(sorted(rows) == sorted((CHAT, MAIL, CHROME, EDITOR, TERMINAL)),
          'the apps of the log only: not Modeler 3D (installed Chromium, never used), not GramaDrama', sorted(rows))
    check(rig.order() == sorted_as_the_spec_says(section['rows']), 'by state, then name', rig.order())
    x11 = 'Działa przez X11, więc nie udostępnia tekstu. Poprawka: uruchamianie natywne'
    for app_id, name, icon in ((CHAT, 'Komunikator', 'internet-chat'), (MAIL, 'Poczta', 'internet-mail')):
        row = rows[app_id]
        check((row['name'], row['text'], row['button'], row['status'], row['enabled'], row['removable'], row['fix'])
              == (name, x11, 'Włącz', 'warning', True, False, None) and icon in row['app_icon'], name, row)
    works = 'Działa · ostatnio %s · ' % checks.when(rig.logged['chrome_ok'])
    if chrome == 'works':
        check(rows[CHROME]['text'] == works + '12 poprawek' and rows[CHROME]['status'] == 'ok'
              and 'google-chrome' in rows[CHROME]['app_icon'], 'Chrome', rows[CHROME])
    else:
        note('the live Chrome runs on X11: its row asks for the fix')
    editor = rows[EDITOR]
    check(editor['text'] == 'Działa · ostatnio %s · 3 poprawki · po zaznaczeniu myszą najpierw naciśnij strzałkę'
          % checks.when(rig.logged['editor_ok']) and editor['icon'] == 'partial' and editor['button'] is None,
          'the text editor works, with its limitation', editor)
    check((rows[TERMINAL]['text'], rows[TERMINAL]['status']) == ('Terminal nie udostępnia tekstu', 'error'),
          'the terminal', rows[TERMINAL])
    check(not section['empty'] and section['problem'] is None, 'no empty state, no problem', section)
    # the last Ctrl+Alt+G, with its app's name and the no_text message that points to this list
    rig.wait_row('last', 'info', '%s · Komunikator · odmowa: Ta aplikacja nie udostępnia tekstu. Szczegóły: '
                                 'GramaDrama › Aplikacje.' % checks.when(rig.logged['chat']))
    check(rig.state()['summary'] == 'Wszystko gotowe', 'an app that needs a fix is no problem of GramaDrama',
          rig.state()['summary'])
    rig.back()


def t_apps_fix(rig):
    rig.apps_setup()
    rig.here()
    rig.wait_app(CHAT, 'fixable', timeout=15)
    wrapper = os.path.join(rig.tree, 'localbin', 'gd-komunikator')  # the session's ~/.local/bin/gd-komunikator
    check(not os.path.lexists(wrapper), 'no wrapper yet')
    rig.click_app(CHAT, 'button')  # "Włącz"
    row = rig.wait_app(CHAT, 'restart', 'Zamknij aplikację Komunikator (Ctrl+Q) i uruchom ją ponownie, żeby poprawka '
                                        'zadziałała')
    check((row['button'], row['status'], row['fix']) == ('Wyłącz poprawkę', 'warning',
                                                        os.path.join(rig.s.env['HOME'], '.local', 'bin', 'gd-komunikator')),
          'the row waits for the restart', row)
    with open(wrapper, encoding='utf-8') as f:
        text = f.read()
    check(text == apps.wrapper_text('Komunikator', os.path.join(rig.tree, 'bin', 'gd-komunikator')), 'the wrapper', text)
    check(os.stat(wrapper).st_mode & 0o777 == 0o755, 'an executable wrapper', oct(os.stat(wrapper).st_mode))
    check(rig.state()['toast'] == 'Poprawka włączona.', 'toast', rig.state()['toast'])
    check(rig.apps()['timer'], 'the app rescans while a row waits for a restart', rig.apps())
    wait_until(lambda: rig.apps()['why'].get('timer'), RESCAN + 2, what='a rescan by the timer')
    # ... but only while its window can be seen; seen again, it rescans at once: within a second, for
    # that reason (not the timer's next tick; shown without activation, so not the activation's refresh)
    rig.minimize()
    scans = rig.apps()['scans']
    time.sleep(RESCAN + 1)
    check(rig.apps()['scans'] == scans and rig.apps()['timer'], 'no rescans while the window is minimized', rig.apps())
    seen = rig.apps()['why'].get('seen', 0)
    rig.minimize(False)
    wait_until(lambda: rig.apps()['why'].get('seen', 0) > seen and rig.apps()['scans'] > scans, 1.5,
               what='a rescan as soon as the window is seen again')
    rig.here()
    # the user restarts Komunikator: the X11 instance quits, the new one runs natively; the rescan sees
    # its GPU process, no Ctrl+Alt+G needed
    rig.elsewhere()
    rig.gpu_process('wayland')
    rig.wait_app(CHAT, 'unconfirmed', 'Niepotwierdzone · działa natywnie, powinno działać', timeout=10)
    wait_until(lambda: not rig.apps()['timer'], 8, what='the rescans to stop')
    # the first correction there confirms it
    t = time.time()
    rig.log(*corrected(91001, t, CHAT))
    row = rig.wait_app(CHAT, 'works', 'Działa · ostatnio %s · 1 poprawka · uruchamiana natywnie (poprawka GramaDramy)'
                       % checks.when(t + 3.5))
    check((row['button'], row['status']) == ('Wyłącz poprawkę', 'ok'), 'the fix can be switched off', row)
    rig.wait_row('last', 'ok', 'Komunikator · Popraw · akapit · zastosowano')
    rig.back()


def t_apps_switch(rig):
    rig.apps_setup()
    rig.here()
    rig.wait_app(TERMINAL, 'unsupported', timeout=15)
    before = rig.order()
    # off: the file at once, a toast, and the row stays where it is while the user works in the list
    rig.click_app(CHROME, 'switch')
    row = rig.wait_app(CHROME, 'off', 'Wyłączona · skrót korekty trafia do aplikacji')
    check(not row['enabled'] and row['status'] == 'info' and row['button'] is None, 'Chrome off', row)
    with open(rig.apps_json) as f:
        check(json.load(f) == {'v': 1, 'disabled': [CHROME], 'added': []}, 'apps.json', open(rig.apps_json).read())
    check(os.stat(rig.apps_json).st_mode & 0o777 == 0o600, 'apps.json is private', oct(os.stat(rig.apps_json).st_mode))
    check(rig.state()['toast'] == 'GramaDrama wyłączona w aplikacji Google Chrome', 'toast', rig.state()['toast'])
    check(rig.order() == before and rig.apps()['frozen'], 'the rows keep their places', rig.order())
    rig.elsewhere()  # the user leaves the window: the list is sorted again
    # 'active' false can come from another state write (the toast) before the app's own handler runs
    wait_until(lambda: not rig.apps()['frozen'], 5, what='the list sorted again')
    check(rig.order()[-1] == CHROME and rig.order() == sorted_as_the_spec_says(rig.apps()['rows']),
          'an app that is off comes last', [(r['id'], r['state']) for r in rig.apps()['rows']], rig.apps()['frozen'],
          rig.state()['active'])
    rig.here()
    rig.click_app(CHROME, 'row')  # the whole row toggles the switch, not only the switch itself
    wait_until(lambda: rig.app_row(CHROME)['state'] != 'off', 10, what='Chrome on again')
    with open(rig.apps_json) as f:
        check(json.load(f)['disabled'] == [], 'apps.json', open(rig.apps_json).read())
    check(rig.app_row(CHROME)['enabled'], 'the switch on again', rig.app_row(CHROME))
    check(rig.state()['toast'] == 'GramaDrama włączona w aplikacji Google Chrome', 'toast', rig.state()['toast'])
    # a broken apps.json switches nothing off (the engine fails open) and the section says so
    broken = '{"v": 1, "disabled": "oops"}'
    with open(rig.apps_json, 'w') as f:
        f.write(broken)
    rig.action('refresh')
    problem = wait_until(lambda: rig.apps()['problem'], 10, what='the note about the broken list')
    check(problem.startswith('Lista aplikacji jest nieprawidłowa: ') and 'apps.json.bak' in problem, 'the note', problem)
    wait_until(lambda: rig.state()['summary'] == 'Wszystko gotowe', 10, what='the list never makes the summary a problem')
    # the next switch repairs it and keeps the broken file
    rig.click_app(TERMINAL, 'switch')
    rig.wait_app(TERMINAL, 'off')
    wait_until(lambda: rig.apps()['problem'] is None, 10, what='the note gone')
    with open(rig.apps_json) as f:
        check(json.load(f) == {'v': 1, 'disabled': [TERMINAL], 'added': []}, 'the repaired apps.json',
              open(rig.apps_json).read())
    with open(rig.apps_json + '.bak') as f:
        check(f.read() == broken, 'the broken file is kept')
    check(rig.state()['toast'] == 'GramaDrama wyłączona w aplikacji Terminal. Poprzedni, nieprawidłowy plik listy '
                                  'aplikacji jest w apps.json.bak.', 'toast', rig.state()['toast'])
    # a write that fails (the config directory read-only): its error instead, and the switch shows the
    # file again
    folder = os.path.dirname(rig.apps_json)
    mode = os.stat(folder).st_mode & 0o777
    os.chmod(folder, 0o500)
    try:
        rig.click_app(TERMINAL, 'switch')
        error = wait_until(lambda: (lambda t: t.startswith('Nie udało się zapisać listy aplikacji: ') and t)(
            rig.state()['toast'] or ''), 5, what='the error of the write')
        note(error)
        wait_until(lambda: (lambda r: r['state'] == 'off' and not r['enabled'])(rig.app_row(TERMINAL)), 5,
                   what='the switch off again, as in the file')
    finally:
        os.chmod(folder, mode)
    with open(rig.apps_json) as f:
        check(json.load(f)['disabled'] == [TERMINAL], 'apps.json unchanged', open(rig.apps_json).read())
    rig.back()


def t_apps_mode(rig):
    rig.apps_setup()
    rig.here()
    wait_until(lambda: rig.app_row(CHROME) and rig.app_row(EDITOR), 15, what='the rows of Chrome and the editor')
    # the earlier scenarios leave their apps.json: this one changes the modes alone and puts them back
    base = json.loads(rig.apps_file() or '{"v": 1, "disabled": [], "added": []}')
    check('modes' not in base, 'no mode yet', base)

    def file_with(modes):
        return dict(base, modes=modes) if modes else base

    def choose(app_id, key):  # the list is a popover: open it, move from the entry shown, Enter
        order = ['fix', 'improve', 'formal', 'casual', 'shorter']
        rig.click_app(app_id, 'mode')
        time.sleep(0.5)
        # The list opens with the entry under the pointer's last position highlighted, wherever an earlier
        # scenario left it: Home first, then down from Popraw.
        rig.s.tap('home')
        time.sleep(0.25)
        for _ in range(order.index(key)):
            rig.s.tap('down')
            time.sleep(0.25)  # a key right after the last one is lost while the popover's list scrolls
        rig.s.tap('ret')
        try:
            wait_until(lambda: rig.app_row(app_id)['mode'] == key, 5, what='%s in the list of %s' % (key, app_id))
        except TimeoutError:
            raise Fail('the list of %s did not change to %s' % (app_id, key), rig.app_row(app_id),
                       rig.s.window('GramaDrama')['frame'], rig.state().get('active'))
    check(rig.app_row(CHROME)['mode'] == 'fix', 'Popraw while the file has no entry', rig.app_row(CHROME))
    choose(CHROME, 'formal')
    with open(rig.apps_json) as f:
        check(json.load(f) == file_with({CHROME: 'formal'}), 'apps.json', open(rig.apps_json).read())
    check(os.stat(rig.apps_json).st_mode & 0o777 == 0o600, 'apps.json is private', oct(os.stat(rig.apps_json).st_mode))
    check(rig.state()['toast'] == 'Korekta w aplikacji Google Chrome zaczyna od trybu „Formalnie”', 'toast',
          rig.state()['toast'])
    # a second app, then back to Popraw: the entry goes, and so does the whole "modes" when it is empty
    choose(EDITOR, 'shorter')
    check(json.loads(rig.apps_file()) == file_with({CHROME: 'formal', EDITOR: 'shorter'}), 'both entries', rig.apps_file())
    choose(CHROME, 'fix')
    check(json.loads(rig.apps_file()) == file_with({EDITOR: 'shorter'}), 'Popraw takes the entry away', rig.apps_file())
    choose(EDITOR, 'fix')
    check(json.loads(rig.apps_file()) == base, 'no empty modes', rig.apps_file())
    # where GramaDrama is off, Ctrl+Alt+G is the app's: the list is greyed out, and the file keeps the mode
    choose(CHROME, 'casual')
    rig.click_app(CHROME, 'switch')
    rig.wait_app(CHROME, 'off', 'Wyłączona · skrót korekty trafia do aplikacji')
    check(json.loads(rig.apps_file()) == dict(base, disabled=base['disabled'] + [CHROME], modes={CHROME: 'casual'}),
          'the mode stays while the app is off', rig.apps_file())
    check(rig.app_row(CHROME)['mode'] == 'casual' and not rig.app_row(CHROME)['mode_sensitive'], 'a greyed-out list',
          rig.app_row(CHROME))
    rig.click_app(CHROME, 'switch')  # on again, and Popraw again: the file is as it was found
    wait_until(lambda: rig.app_row(CHROME)['enabled'] and rig.app_row(CHROME)['mode_sensitive'], 10, what='Chrome on')
    choose(CHROME, 'fix')
    check(json.loads(rig.apps_file()) == base, 'the file as it was', rig.apps_file())


def t_apps_add(rig):
    rig.apps_setup()
    rig.here()
    rig.action('add-app')
    dialog = wait_until(lambda: rig.apps().get('dialog'), 5, what='the dialog')
    check(CALCULATOR in dialog['rows'] and CHROME in dialog['rows'] and MODELER in dialog['rows']
          and SELF not in dialog['rows'], 'the installed apps, never GramaDrama', dialog)
    check(CALCULATOR in dialog['addable'] and CHROME not in dialog['addable'],
          'an app already on the list is shown, but cannot be added again', dialog['addable'])
    # Enter adds the first match that can be added: none for an app that is on the list already
    before, toast = rig.apps_file(), rig.state()['toast']
    rig.s.type_text('google chrome', mode='keysyms')
    wait_until(lambda: (rig.apps().get('dialog') or {}).get('rows') == [CHROME], 5, what='the search for Chrome')
    rig.s.tap('ret')
    time.sleep(1)
    check(rig.apps()['dialog'] and rig.state()['toast'] == toast, 'nothing added', rig.apps()['dialog'],
          rig.state()['toast'])
    check(rig.apps_file() == before, 'apps.json unchanged', rig.apps_file())
    rig.s.combo('ctrl', 'a')
    rig.s.type_text('kalkul', mode='keysyms')
    wait_until(lambda: (rig.apps().get('dialog') or {}).get('rows') == [CALCULATOR], 5, what='the search')
    rig.s.tap('ret')  # adds the first match
    row = rig.wait_app(CALCULATOR, 'unconfirmed', 'Niepotwierdzone · wypróbuj skrót korekty w polu tekstowym')
    check(rig.apps()['dialog'] is None, 'the dialog closed', rig.apps()['dialog'])
    check(row['removable'] and row['name'] == 'Kalkulator' and row['enabled'], 'an app added by hand', row)
    check(rig.order()[-1] == CALCULATOR, 'a new row comes last while the user works in the list', rig.order())
    with open(rig.apps_json) as f:
        check(json.load(f)['added'] == [CALCULATOR], 'apps.json', open(rig.apps_json).read())
    check(rig.state()['toast'] == 'Dodano aplikację Kalkulator do listy', 'toast', rig.state()['toast'])
    # switched off, it keeps its row and has no remove button: removing it would switch it on again
    rig.click_app(CALCULATOR, 'switch')
    row = rig.wait_app(CALCULATOR, 'off')
    check(not row['removable'] and not row['geometry']['remove'], 'no remove button while it is off', row)
    rig.click_app(CALCULATOR, 'switch')
    row = rig.wait_app(CALCULATOR, 'unconfirmed')
    check(row['removable'], 'the remove button back', row)
    rig.click_app(CALCULATOR, 'remove')
    wait_until(lambda: rig.app_row(CALCULATOR) is None, 10, what='the row gone')
    with open(rig.apps_json) as f:
        check(json.load(f)['added'] == [], 'apps.json', open(rig.apps_json).read())
    check(rig.state()['toast'] == 'Usunięto aplikację Kalkulator z listy', 'toast', rig.state()['toast'])
    # an app without a .desktop file: named by its window's class, with the generic icon
    rig.elsewhere()
    t = time.time()
    rig.log(refused(t, 'window:gd-notes', 'stale', 'wayland'))
    row = rig.wait_app('window:gd-notes', 'unconfirmed', 'Niepotwierdzone · wypróbuj skrót korekty w polu tekstowym')
    check(row['name'] == 'gd-notes' and 'application-x-executable' in row['app_icon'] and not row['removable'],
          'a window-backed app', row)
    rig.wait_row('last', 'info', '%s · gd-notes · odmowa: Nie widzę aktualnej pozycji kursora.' % checks.when(t))
    rig.back()


def t_apps_undo(rig):
    rig.apps_setup()
    rig.here()
    wrapper = os.path.join(rig.tree, 'localbin', 'gd-komunikator')
    if not os.path.exists(wrapper):  # alone (--only): "Włącz" first
        rig.click_app(CHAT, 'button')
        wait_until(lambda: os.path.exists(wrapper), 5, what='the wrapper')
    wait_until(lambda: (rig.app_row(CHAT) or {}).get('button') == 'Wyłącz poprawkę', 10, what='"Wyłącz poprawkę"')
    rig.click_app(CHAT, 'button')
    wait_until(lambda: not os.path.lexists(wrapper), 5, what='the wrapper deleted')
    wait_until(lambda: rig.app_row(CHAT)['fix'] is None, 10, what='the row without the fix')
    row = rig.app_row(CHAT)
    check(row['button'] != 'Wyłącz poprawkę' and 'poprawka GramaDramy' not in row['text'], 'no fix any more', row)
    check(rig.state()['toast'] == 'Poprawka wyłączona.', 'toast', rig.state()['toast'])
    rig.back()


def t_apps_override(rig):
    """Poczta starts by an absolute path, so "Włącz" writes an override .desktop file. One written
    during this login counts everywhere only after the next one (docs/apps.md, section 6): the row
    says so, and "Wyłącz poprawkę" of an override from before this login says when the app grid
    follows. Poczta does not run, so the rows are 8, not 3."""
    rig.apps_setup()
    rig.here()
    rig.wait_app(MAIL, 'fixable', timeout=15)
    original = os.path.join(rig.tree, 'share', 'applications', MAIL)
    override = os.path.join(rig.data, 'applications', MAIL)
    with open(original, encoding='utf-8') as f:
        expected = apps.override_text(f.read(), 'Poczta', original)
    for login in (False, True):
        check(not os.path.lexists(override), 'no override yet')
        rig.click_app(MAIL, 'button')  # "Włącz"
        row = rig.wait_app(MAIL, 'unconfirmed', 'Niepotwierdzone · poprawka zadziała po wylogowaniu i ponownym zalogowaniu')
        check((row['button'], row['fix']) == ('Wyłącz poprawkę', override), 'pending until the next login', row)
        with open(override, encoding='utf-8') as f:
            text = f.read()
        check(text == expected and '--ozone-platform=wayland' in text, 'a copy of the original with the flag', text)
        check(rig.state()['toast'] == 'Poprawka włączona.', 'toast', rig.state()['toast'])
        toast = 'Poprawka wyłączona.'
        if login:  # a new login: GNOME Shell started after the override was written
            old = time.time() - 86400
            os.utime(override, (old, old))
            rig.action('refresh')
            rig.wait_app(MAIL, 'unconfirmed', 'Niepotwierdzone · działa natywnie, powinno działać')
            toast += ' Z siatki aplikacji Poczta uruchomi się bez niej po ponownym zalogowaniu.'
        rig.click_app(MAIL, 'button')  # "Wyłącz poprawkę"
        rig.wait_app(MAIL, 'fixable')
        check(not os.path.lexists(override), 'the override deleted')
        check(rig.state()['toast'] == toast, 'toast', rig.state()['toast'])
    rig.back()


def t_apps_repair(rig):
    """A wrapper whose program has moved since it was written (a package update): the row says so,
    and "Napraw" writes the wrapper anew for the program where it is now."""
    rig.apps_setup()
    rig.here()
    wrapper = os.path.join(rig.tree, 'localbin', 'gd-komunikator')
    with open(wrapper, 'w', encoding='utf-8') as f:
        f.write(apps.wrapper_text('Komunikator', os.path.join(rig.tree, 'opt', 'komunikator-1.0', 'gd-komunikator')))
    os.chmod(wrapper, 0o755)
    rig.action('refresh')
    row = rig.wait_app(CHAT, 'broken', 'Poprawka uruchamiania jest nieaktualna')
    check((row['button'], row['status']) == ('Napraw', 'warning'), 'the repair is offered', row)
    rig.click_app(CHAT, 'button')  # "Napraw"
    row = wait_until(lambda: (lambda r: r and r['state'] != 'broken' and r)(rig.app_row(CHAT)), 10, what='the row repaired')
    with open(wrapper, encoding='utf-8') as f:
        text = f.read()
    check(text == apps.wrapper_text('Komunikator', os.path.join(rig.tree, 'bin', 'gd-komunikator')), 'the wrapper', text)
    check(row['button'] == 'Wyłącz poprawkę' and rig.state()['toast'] == 'Poprawka naprawiona.', 'repaired', row,
          rig.state()['toast'])
    rig.back()


def t_cancel(rig):
    s = rig.s
    before = rig.state()['try']['buffer']
    rig.stub.chunks, rig.stub.chunk_delay = stream(TRY_TEXT, pieces=2), 0.1  # any other text: a real change
    rig.nudge()
    s.combo('ctrl', 'alt', 'g')
    rig.wait_try('ok', 'Gotowe', timeout=15)
    s.tap('esc')
    rig.wait_try('info', 'Anulowano — tekst bez zmian.')
    time.sleep(0.3)
    check(rig.state()['try']['buffer'] == before, 'the text stays', rig.state()['try']['buffer'])
    rig.wait_row('last', 'info', 'Popraw · akapit · anulowano')


def t_address_edit(rig):
    s = rig.s
    saved = rig.stub.url
    rig.click_widget('llm_entry')
    # a refresh must not touch the saved address somebody is about to edit: the caret stays where it was put
    # (set_text of the same text empties the entry and fills it again, the caret at the end)
    s.tap('home')
    for _ in range(4):
        s.tap('right')
    rig.action('refresh')
    time.sleep(1.0)
    s.type_text('X', mode='keysyms')
    wait_until(lambda: rig.state()['entries']['llm'] != saved, 5, what='the typed letter')
    check(rig.state()['entries']['llm'] == saved[:4] + 'X' + saved[4:], 'the caret stayed in the saved address '
          'through a refresh', rig.state()['entries'])
    s.combo('ctrl', 'a')
    s.type_text('http://192.0.2.7:1', mode='keysyms')
    wait_until(lambda: rig.state()['entries']['llm'] == 'http://192.0.2.7:1', 5, what='the typed address')
    rig.action('refresh')  # e.g. the window became active again
    time.sleep(1.5)
    check(rig.state()['entries']['llm'] == 'http://192.0.2.7:1', 'the address being typed stays', rig.state()['entries'])
    check(json.load(open(rig.settings))['llm_url'] == saved, 'nothing saved before it is applied')
    s.combo('ctrl', 'a')
    s.type_text('nie-adres', mode='keysyms')
    s.tap('ret')
    time.sleep(0.8)
    check(json.load(open(rig.settings))['llm_url'] == saved, 'an invalid address is never saved')
    s.combo('ctrl', 'a')
    s.type_text(saved, mode='keysyms')
    s.tap('ret')
    rig.wait_row('llm', 'ok', 'Działa')
    check(rig.state()['entries']['llm'] == saved, 'the saved address shown', rig.state()['entries'])


def t_settings_broken(rig):
    try:
        with open(rig.settings, 'w') as f:
            f.write('{"llm_url": ')  # not JSON: nothing can be checked
        rig.action('refresh')
        row = rig.wait_row('llm', 'error', 'Plik ustawień jest nieprawidłowy')
        note(row['text'])
        state = rig.state()
        check(state['summary'] == 'Wymaga uwagi: ustawienia', 'summary', state['summary'],
              {name: row['state'] for name, row in state['rows'].items()}, state['model'], state['problems'])
        check(state['rows']['lt']['state'] == 'info' and state['model']['state'] == 'info',
              'nothing checked with defaults', state['rows']['lt'], state['model'])
        # only the model is refused: the server is still checked, so a model can be picked from its list
        rig.write_settings(model=5)  # not text: refused (an empty model is just not chosen yet)
        rig.refresh()
        wait_until(lambda: rig.state()['model']['state'] == 'error', 5, what='the refused model')
        state = rig.state()
        check('Nieprawidłowy model' in state['model']['text'] and state['model']['names'] == [None, BIELIK]
              and state['model']['selected'] == 0, 'a model to pick, the placeholder selected', state['model'])
        rig.wait_row('llm', 'ok')
        # only the LanguageTool address is refused: its row says so
        rig.write_settings(languagetool_url='nie-adres')
        rig.action('refresh')
        rig.wait_row('lt', 'error', 'Nieprawidłowy adres LanguageTool w ustawieniach')
        # the model server is still checked meanwhile: the summary waits for it, then names the settings
        wait_until(lambda: rig.state()['summary'] == 'Wymaga uwagi: ustawienia', 10, what='the summary')
        check(rig.state()['problems'] == [['ustawienia', 'connection']], 'the pill leads to Usługi', rig.state()['problems'])
    finally:
        rig.write_settings()  # repaired, also for the next tests
        rig.refresh()
    rig.wait_row('llm', 'ok')
    rig.wait_row('lt', 'ok')
    check(rig.state()['settings_problems'] == {}, 'repaired', rig.state()['settings_problems'])
    check(rig.state()['model']['names'] == [BIELIK], 'no placeholder any more', rig.state()['model'])


def t_server_settings(rig):
    """The model server's rows in Usługi: the protocol (with the hint when an Ollama is set as OpenAI), the thinking row of
    Zaawansowane (Ollama only), the model's name typed by hand while the server gives no list, and the key, which is saved
    0600 and never stays on the screen."""
    s, stub = rig.s, rig.stub
    secret = 'test-api-key-0123456789'
    try:
        rig.refresh()
        rig.show_tab('connection')
        state = rig.state()
        check(state['services']['locations']['llm'] == 'server' and state['services']['llm_status_title'] == 'Serwer',
              'an address of before the Usługi tab is a server', state['services'])
        check(state['server']['api'] == 'ollama', 'the stub settings speak Ollama', state['server'])
        # thinking (Ollama only), folded under Zaawansowane: off for a file that has no such field, a choice that is saved
        rig.click_widget('llm_advanced', dy=24)
        wait_until(lambda: rig.state()['geometry']['think_row'], 5, what='Zaawansowane open')
        time.sleep(0.5)  # the expander's animation
        check(rig.state()['server']['think'] == 'off' and rig.state()['server']['think_visible'],
              'the thinking row: off, shown for Ollama', rig.state()['server'])
        rig.choose('think_row', 1)  # "domyślne ustawienie modelu"
        wait_until(lambda: rig.saved_settings().get('llm_think') == 'model', 5, what='the thinking choice saved')
        rig.choose('think_row', 0)
        wait_until(lambda: rig.saved_settings().get('llm_think') == 'off', 5, what='the thinking choice saved again')
        rig.choose('llm_api_row', 0)  # OpenAI
        wait_until(lambda: rig.saved_settings()['llm_api'] == 'openai', 5, what='the protocol saved')
        wait_until(lambda: not rig.state()['server']['think_visible'], 5, what='the thinking row gone for OpenAI')
        row = rig.wait_row('llm', 'warning', 'to Ollama')
        check('wybierz protokół „Ollama”' in row['text'], 'the hint', row)
        rig.choose('llm_api_row', 1)  # and back
        wait_until(lambda: rig.saved_settings()['llm_api'] == 'ollama', 5, what='the protocol saved again')
        wait_until(lambda: rig.state()['server']['think_visible'], 5, what='the thinking row back for Ollama')
        rig.wait_row('llm', 'ok', 'Ollama')
        # a model the server does not have
        rig.write_settings(model='inny-model')
        rig.refresh()
        wait_until(lambda: rig.state()['model']['state'] == 'error', 5, what='a model the server does not have')
        check('Tego modelu nie ma na serwerze' in rig.state()['model']['text'], 'the missing model', rig.state()['model'])
        # a server without a list of models: the name is typed
        stub.get_status = {'/api/tags': 404}
        rig.write_settings()
        rig.refresh()
        wait_until(lambda: rig.state()['services']['visible']['model_entry'], 5, what='the entry for the name')
        check(not rig.state()['services']['visible']['model'], 'no list to pick from', rig.state()['services']['visible'])
        rig.type_into('llm_model_row', 'inny-model')
        wait_until(lambda: rig.saved_settings()['model'] == 'inny-model', 5, what='the model name saved')
        rig.type_into('llm_model_row', BIELIK)
        wait_until(lambda: rig.saved_settings()['model'] == BIELIK, 5, what='the model back')
        stub.get_status = {}
        rig.refresh()
        wait_until(lambda: rig.state()['services']['visible']['model'], 5, what='the list back')
        # the key: saved, 0600, never shown again, removed by the button
        rig.type_into('llm_key_row', secret)
        wait_until(lambda: rig.saved_settings().get('llm_key') == secret, 5, what='the key saved')
        check(os.stat(rig.settings).st_mode & 0o777 == 0o600, 'settings.json is private', oct(os.stat(rig.settings).st_mode))
        wait_until(lambda: rig.state()['server']['key_button'], 5, what='the remove button')
        state = rig.state()
        check(state['server']['key_text'] == '' and 'zapisany' in state['server']['key_row'], 'the key left the entry', state['server'])
        check(secret not in json.dumps(state), 'the key is nowhere in what the window shows')
        check(state['toast'] == 'Zapisano klucz API.' and secret not in state['toast'], 'toast', state['toast'])
        rig.click_widget('clear_key')
        wait_until(lambda: rig.saved_settings().get('llm_key') == '', 5, what='the key removed')
        wait_until(lambda: not rig.state()['server']['key_button'], 5, what='the button gone')
        check('opcjonalny' in rig.state()['server']['key_row'], 'the entry asks for a key again', rig.state()['server'])
        # GNOME keeps the plain source while a password field has the focus; a click into a text field brings
        # GramaDrama back (the next scenarios start from there)
        rig.click_widget('llm_entry', dx=60)
        rig.wait_row('source', 'ok', 'aktywne')
    finally:
        stub.get_status = {}
        rig.write_settings()  # repaired, also for the next tests
        stub.models = [BIELIK]
        rig.refresh()
    rig.wait_row('llm', 'ok')
    rig.wait_row('lt', 'ok')


def t_whisper_server(rig):
    """Speech recognition in Usługi: this computer's Whisper (the recording stays here), or a server whose address,
    protocol, model and key are saved like the model server's, the key never staying on the screen; the Dyktowanie row
    says what recognizes speech."""
    s, stub = rig.s, rig.stub
    secret = 'test-speech-key-9876543210'

    def choose_backend(index):
        rig.toggle('speech_where', index)
        wait_until(lambda: rig.saved_settings().get('whisper_backend') == ('server' if index else 'local'),
                   5, what='the speech backend saved')
        wait_until(lambda: rig.state()['server']['whisper_backend'] == ('server' if index else 'local')
                   and rig.state()['services']['visible']['whisper_url'] == bool(index),
                   5, what='the speech backend shown')
    try:
        stub.models = [BIELIK, 'whisper-test']
        rig.refresh()
        check(rig.state()['server']['whisper_backend'] == 'local', 'empty legacy settings use this computer')
        check(not rig.state()['services']['visible']['whisper_url'] and rig.state()['services']['visible']['whisper_local'],
              'server settings hidden while local', rig.state()['services']['visible'])
        choose_backend(1)
        rig.wait_row('whisper', 'error', 'Podaj adres serwera mowy')
        rig.wait_row('dictation', 'error', 'Ustaw serwer mowy')
        rig.type_into('whisper_entry', 'nie-adres')
        time.sleep(0.8)
        check(rig.saved_settings().get('whisper_url', '') == '', 'an invalid address is never saved')
        rig.type_into('whisper_entry', stub.url)
        wait_until(lambda: rig.saved_settings().get('whisper_url') == stub.url, 5, what='the address saved')
        row = rig.wait_row('whisper', 'warning', 'nie wymienia modelu „whisper-1”')  # the server lists other models
        rig.type_into('whisper_model_row', 'whisper-test')
        wait_until(lambda: rig.saved_settings().get('whisper_model') == 'whisper-test', 5, what='the model name saved')
        row = rig.wait_row('whisper', 'ok', 'Działa')
        check('API zgodne z OpenAI' in row['text'], 'the protocol said', row)
        rig.wait_row('dictation', 'ok', 'Rozpoznaje serwer mowy')
        rig.type_into('whisper_key_row', secret)
        wait_until(lambda: rig.saved_settings().get('whisper_key') == secret, 5, what='the key saved')
        check(os.stat(rig.settings).st_mode & 0o777 == 0o600, 'settings.json is private', oct(os.stat(rig.settings).st_mode))
        wait_until(lambda: rig.state()['server']['whisper_key_button'], 5, what='the remove button')
        state = rig.state()
        check(state['server']['whisper_key_text'] == '' and 'zapisany' in state['server']['whisper_key_row'],
              'the key left the entry', state['server'])
        check(secret not in json.dumps(state), 'the key is nowhere in what the window shows')
        rig.click_widget('clear_whisper_key')
        wait_until(lambda: rig.saved_settings().get('whisper_key') == '', 5, what='the key removed')
        # a password field keeps GNOME on the plain source; a click into a text field brings GramaDrama back
        rig.click_widget('whisper_model_row', dx=60)
        rig.wait_row('source', 'ok', 'aktywne')
        # A local choice keeps the server configuration for switching back.
        choose_backend(0)
        check(rig.saved_settings()['whisper_url'] == stub.url and rig.saved_settings()['whisper_model'] == 'whisper-test',
              'local selection preserves the remote address and model')
        choose_backend(1)
        rig.wait_row('whisper', 'ok', 'Działa')
        # An empty address in explicit server mode stays a configuration error, never a local fallback.
        rig.type_into('whisper_entry', '')
        wait_until(lambda: rig.saved_settings().get('whisper_url') == '', 5, what='the address removed')
        rig.wait_row('whisper', 'error', 'Podaj adres serwera mowy')
        rig.wait_row('dictation', 'error', 'Ustaw serwer mowy')
        choose_backend(0)
    finally:
        stub.models = [BIELIK]
        rig.write_settings()  # repaired, also for the next tests
        rig.refresh()
    rig.wait_row('llm', 'ok')


def t_whisper_target(rig):
    """Legacy server settings and explicit choices survive refresh without losing saved connection fields."""
    s, stub = rig.s, rig.stub
    secret = 'test-speech-choice-key'

    def shot(name):
        if rig.args.shots:
            os.makedirs(rig.args.shots, exist_ok=True)
            s.place_window('GramaDrama', 40, 30, WIDTH, SHOT_HEIGHT)
            time.sleep(0.6)
            rig.shot_window(os.path.join(rig.args.shots, name + '.png'))
            s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)

    try:
        stub.models = [BIELIK, 'example-speech-model']
        rig.write_settings(whisper_url=stub.url, whisper_model='example-speech-model', whisper_key=secret)
        rig.refresh()
        rig.show_tab('connection')
        rig.wait_row('whisper', 'ok', 'Działa')
        check(rig.state()['server']['whisper_backend'] == 'server', 'legacy address still selects the server')
        check('whisper_backend' not in rig.saved_settings(), 'reading legacy settings does not rewrite the file')
        rig.toggle('speech_where', 0)
        wait_until(lambda: rig.saved_settings().get('whisper_backend') == 'local', 5, what='local speech selected')
        rig.refresh()
        check(not rig.state()['services']['visible']['whisper_url'], 'local selection survives refresh')
        check(rig.saved_settings()['whisper_url'] == stub.url and rig.saved_settings()['whisper_key'] == secret
              and rig.saved_settings()['whisper_model'] == 'example-speech-model', 'saved server configuration preserved')
        check(secret not in json.dumps(rig.state()), 'saved key stays off screen')
        shot('speech-local')
        rig.toggle('speech_where', 1)
        wait_until(lambda: rig.saved_settings().get('whisper_backend') == 'server', 5, what='server speech selected')
        rig.wait_row('whisper', 'ok', 'Działa')
        rig.choose('whisper_api_row', 1)
        wait_until(lambda: rig.saved_settings().get('whisper_api') == 'whispercpp', 5, what='native whisper.cpp selected')
        wait_until(lambda: rig.state()['server']['whisper_api'] == 'whispercpp', 5, what='native protocol shown')
        row = rig.wait_row('whisper', 'ok', 'whisper.cpp')
        check(not rig.state()['services']['visible']['whisper_model'], 'no model name for whisper.cpp', rig.state()['services'])
        check(rig.saved_settings()['whisper_key'] == secret and rig.saved_settings()['whisper_model'] == 'example-speech-model',
              'protocol change preserves the OpenAI model and key')
        shot('speech-server')
        rig.choose('whisper_api_row', 0)
        wait_until(lambda: rig.saved_settings().get('whisper_api') == 'openai', 5, what='OpenAI protocol restored')
        rig.wait_row('whisper', 'ok', 'Działa')
        check(rig.state()['entries']['whisper_model'] == 'example-speech-model', 'saved model restored on screen')
        # a whisper.cpp server set as OpenAI: the check suggests the right protocol
        stub.get_status = {'/v1/models': 404}
        rig.refresh()
        rig.wait_row('whisper', 'warning', 'To serwer whisper.cpp')
    finally:
        stub.get_status = {}
        stub.models = [BIELIK]
        rig.write_settings()
        rig.refresh()


def t_login(rig):
    """The remote access (oidc.py, docs/remote-access.md): folded away until "Skonfiguruj…"; the stub is both the provider
    and the gateway (it wants the provider's token), and the model server is the gateway itself. The harness plays the
    browser: it opens the URL the app made (the provider's page and the passkey are the user's) and brings the address
    back the way the desktop does it, through the handler of gramadrama:// that the installer registers."""
    s, stub = rig.s, rig.stub
    auth_file = os.path.join(os.path.dirname(rig.settings), 'auth.json')

    def browser_returns(url):
        r = s.sh('gio open %s' % shlex.quote(url))
        check(r['rc'] == 0, 'the desktop hands gramadrama:// to the app', r)

    def start():
        rig.click_widget('login_button')
        wait_until(lambda: rig.state()['server']['login_url'], 10, what='the address for the browser')
        return rig.state()['server']['login_url']
    try:
        rig.installer('assert I.register_scheme()')  # as `install_engine.py install` does
        rig.write_settings()
        rig.refresh()
        visible = rig.state()['services']['visible']
        check(visible['remote_setup'] and not visible['login_entry'] and not visible['login'] and not visible['route'],
              'nothing set: only "Skonfiguruj…"', visible)
        rig.click_widget('remote_setup')
        wait_until(lambda: rig.state()['server']['focus_in'] == 'issuer', 5, what='the address field focused')
        stub.require_login = True
        s.type_text(stub.url, mode='keysyms')
        s.tap('ret')
        wait_until(lambda: rig.saved_settings().get('auth_issuer') == stub.url, 5, what='the address saved')
        check(not rig.state()['services']['visible']['login'], 'no account without the client id', rig.state()['services'])
        rig.type_into('login_client_row', CLIENT_ID)
        wait_until(lambda: rig.saved_settings().get('auth_client_id') == CLIENT_ID, 5, what='the client id saved')
        wait_until(lambda: rig.state()['services']['visible']['login'], 5, what='the account row')
        row = rig.wait_row('llm', 'warning', 'Wymaga logowania')  # the model server is behind the login
        check(rig.state()['server']['login_button'] == 'Zaloguj się…', 'the button to log in', rig.state()['server'])
        url = start()
        check(url.startswith(stub.url + '/authorize?') and 'code_challenge_method=S256' in url,
              'the provider\'s page, with PKCE', url)
        row = rig.state()['rows']['login']
        check(row['state'] == 'busy' and rig.state()['server']['login_back_visible'], 'waiting for the browser; the field '
              'for the address the browser came back to', row)
        # a link on a web page cannot log anyone in: its state is not the one of the login that waits
        browser_returns('gramadrama://auth?code=forged&state=forged')
        wait_until(lambda: 'nie pasuje' in (rig.state().get('toast') or ''), 10, what='the refusal')
        check(rig.state()['rows']['login']['state'] == 'busy' and not os.path.exists(auth_file), 'still waiting, nothing saved')
        # the real way back
        browser_returns(stub.authorize(url))
        rig.wait_row('login', 'ok', 'Zalogowano jako Jan Testowy', timeout=20)
        check(rig.state()['server']['login_button'] == 'Wyloguj', 'the button to log out', rig.state()['server'])
        check(os.stat(auth_file).st_mode & 0o777 == 0o600, 'the tokens are private', oct(os.stat(auth_file).st_mode))
        saved = json.load(open(auth_file))
        check(saved['access_token'] in stub.access_tokens, 'the tokens are the provider\'s', saved.keys())
        check(saved['access_token'] not in json.dumps(rig.state()) and saved['access_token'] not in open(rig.settings).read(),
              'the token is nowhere in the window or in settings.json')
        rig.wait_row('llm', 'ok', 'Działa')  # the servers answer to the token (the stub refuses requests without it)
        check(any(a == 'Bearer ' + saved['access_token'] for p, a in stub.headers_seen if p == '/api/tags'),
              'the model server was asked with the token', stub.headers_seen[-5:])
        # logging out
        rig.click_widget('login_button')
        rig.wait_row('login', 'warning', 'Nie zalogowano', timeout=20)
        check(not os.path.exists(auth_file), 'the tokens are gone')
        rig.wait_row('llm', 'warning', 'Wymaga logowania')
        # the handler is missing (or the browser asks and the user says no): the address is pasted in
        url = start()
        callback = stub.authorize(url)
        wait_until(lambda: rig.state()['server']['focus_in'] == 'back', 5, what='the field for the address focused (and scrolled to)')
        s.type_text(callback, mode='keysyms')  # what a user does: paste it
        wait_until(lambda: rig.state()['server']['login_back_text'] == callback, 10, what='the address typed')
        s.tap('ret')
        rig.wait_row('login', 'ok', 'Zalogowano jako Jan Testowy', timeout=20)
        rig.wait_row('llm', 'ok', 'Działa')
        # the provider ended the login behind our back: the next check says so and offers the login again
        stub.refresh_tokens.clear()
        stub.access_tokens.clear()
        rig.refresh_quietly()
        rig.wait_row('login', 'warning', 'Nie zalogowano', timeout=20)
        check(not os.path.exists(auth_file), 'a login the provider ended is forgotten')
        # "Wyłącz dostęp zdalny": the address and the client id go, folded away again
        rig.click_widget('remote_off')
        wait_until(lambda: (rig.saved_settings().get('auth_issuer'), rig.saved_settings().get('auth_client_id')) == ('', ''),
                   5, what='the remote access off')
        wait_until(lambda: rig.state()['services']['visible']['remote_setup'], 5, what='"Skonfiguruj…" again')
    finally:
        stub.require_login = False
        rig.installer('I.unregister_scheme()')
        if os.path.exists(auth_file):
            os.remove(auth_file)
        rig.write_settings()  # repaired, also for the next tests
        rig.refresh()
    rig.wait_row('llm', 'ok')


def t_route(rig):
    """At home or away (core.route, as the Mac's Route), each service on its own: with the remote access set up, a server
    that answers in this network is asked directly, and one that does not goes through the gateway (the login address's
    origin, /ollama and /languagetool) with the login's token; without the login the row Połączenie asks for it."""
    s, stub = rig.s, rig.stub
    auth_file = os.path.join(os.path.dirname(rig.settings), 'auth.json')
    route_file = os.path.join(s.env['XDG_CACHE_HOME'], 'gramadrama', 'route.json')

    def forget():  # the answer "it does not answer here" is kept for 30 s
        if os.path.exists(route_file):
            os.remove(route_file)
    nothing = 'http://127.0.0.1:%d' % closed_port()
    at_home = 'http://localhost:%d' % stub.server.server_port  # another origin than the gateway's: the token is the gateway's
    gateway = stub.url.split('//')[1]
    try:
        # at home: the servers answer at their own address, and no token goes there
        rig.write_settings(llm_url=at_home, languagetool_url=at_home, auth_issuer=stub.url, auth_client_id=CLIENT_ID)
        forget()
        asked = len(stub.headers_seen)
        rig.refresh()
        row = rig.wait_row('route', 'ok', 'Teraz bezpośrednio')
        check('localhost' in row['text'], 'the row names the server at home', row)
        check(not any(a for _, a in stub.headers_seen[asked:]), 'no token to a server at home', stub.headers_seen[asked:])
        # away from home: nothing answers at the servers' own addresses; the gateway wants the login
        stub.require_login = True
        rig.write_settings(llm_url=nothing, languagetool_url=nothing, auth_issuer=stub.url, auth_client_id=CLIENT_ID)
        forget()
        rig.refresh_quietly()
        rig.wait_row('route', 'warning', 'nie odpowiada w tej sieci')
        check(rig.state()['server']['login_button'] == 'Zaloguj się…' and 'zaloguj' in rig.state()['rows']['llm']['text'],
              'away and not logged in: the login is asked for', rig.state()['rows']['llm'])
        rig.wait_row('login', 'warning', 'nie odpowiada w tej sieci')
        # logged in (the file the login leaves): the gateway answers
        fd = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(stub.login(name='Jan Testowy'), f)
        forget()
        rig.refresh()
        row = rig.wait_row('route', 'ok', 'Teraz przez bramę')
        check(gateway in row['text'], 'the row names the gateway', row)
        rig.wait_row('llm', 'ok', 'przez bramę')
        rig.wait_row('lt', 'ok', 'przez bramę')
        check('/ollama/api/version' in stub.gateway_seen and '/languagetool/v2/languages' in stub.gateway_seen,
              'the gateway\'s paths', stub.gateway_seen[-6:])
        # LanguageTool at home, the model away: each says its own way
        stub.require_login = False
        rig.write_settings(llm_url=nothing, languagetool_url=at_home, auth_issuer=stub.url, auth_client_id=CLIENT_ID)
        forget()
        rig.refresh()
        rig.wait_row('route', 'ok', 'Przez bramę: model; bezpośrednio: LanguageTool')
    finally:
        stub.require_login = False
        if os.path.exists(auth_file):
            os.remove(auth_file)
        forget()
        rig.write_settings()  # repaired, also for the next tests
        rig.refresh()
    rig.wait_row('llm', 'ok')


def t_browser(rig):
    """The Przeglądarka group of the Aplikacje tab (the Mac window's browserSection, the same extension): without the extension the
    row says what to do and where it is, with it in a real Chrome (loaded as chrome_ext.py does) it names the browser and the
    version, a site the extension switched off (apps.json "sites") is a row with "Włącz", and the button puts the site back
    (the extension hears of it from the engine)."""
    s = rig.s
    extension = os.path.join(os.path.dirname(LINUX), 'browser', 'extension')
    version = json.load(open(os.path.join(extension, 'manifest.json')))['version']
    os.makedirs(os.path.join(s.env['XDG_CONFIG_HOME'], 'google-chrome'), exist_ok=True)  # a Chrome that was run once
    chrome = None
    try:
        rig.refresh_quietly()
        row = rig.wait_row('browser', 'warning', 'Nie połączona — uruchom instalator')
        rig.installer("assert I.register_browser_hosts() == ['Google Chrome']")  # as `install_engine.py install` does
        rig.refresh_quietly()
        row = rig.wait_row('browser', 'info', 'Nie połączona — przeglądarka zamknięta')
        check('chrome://extensions' in row['text'], 'the way to load the extension', row)
        chrome = s.spawn_chrome(url='about:blank', app=False, wait=False, extension=extension, name='chromeapp')
        row = rig.wait_row('browser', 'ok', 'Połączona: Google Chrome (wtyczka %s)' % version, timeout=40)
        check(not rig.state()['attention']['apps'], 'a connected extension marks no tab', rig.state()['attention'])
        # the popup of the extension switches a site off: the engine saves it, the window lists it
        chrome.eval_extension("port.postMessage({type: 'setSite', site: 'mail.example', disabled: true}); true")
        wait_until(lambda: rig.state()['sites'] == ['mail.example'], 15, what='the site listed')
        s.activate_window('GramaDrama')  # Chrome's window (1200x760 at 0,0) is on top of it: bring the app's back
        rig.show_tab('apps')
        rig.action('focus-site-button')  # the page may be long (the apps of the other scenarios): scroll to the button
        time.sleep(0.5)
        rig.click_widget('site_button')
        wait_until(lambda: rig.state()['sites'] == [], 10, what='the site back on')
        wait_until(lambda: chrome.eval_extension('[...disabledSites]') == [], 10, what='the extension heard of it')
        check('sites' not in json.load(open(rig.apps_json)), 'apps.json after the switch back', open(rig.apps_json).read())
    finally:
        if chrome is not None:
            chrome.kill()
    row = rig.wait_row('browser', 'info', 'Nie połączona', timeout=20)  # the browser is gone: the row follows


def t_prompts(rig):
    """The Prompty row (docs/prompts.md): the defaults say so, a file of the user's that differs from its default is
    counted, one that is broken is named with its reason and the default is used, and a file that says what the
    default says is no change."""
    directory = os.path.join(os.path.dirname(rig.settings), 'prompts')
    os.makedirs(directory, exist_ok=True)
    task, user = os.path.join(directory, 'task-fix.txt'), os.path.join(directory, 'user.txt')
    try:
        rig.refresh()
        row = rig.wait_row('prompts', 'info', 'Domyślne')
        check('folderze promptów' in row['text'], 'where the files go', row)
        with open(task, 'w', encoding='utf-8') as f:
            f.write('Only fix the commas.\n')
        rig.refresh()
        rig.wait_row('prompts', 'ok', 'Własne pliki (1): task-fix.txt')
        with open(user, 'w', encoding='utf-8') as f:
            f.write('no placeholder')
        rig.refresh()
        row = rig.wait_row('prompts', 'warning', 'Pominięty plik user.txt: brakuje {text}')
        check('używam domyślnego' in row['text'], 'the default is used', row)
        os.remove(user)
        os.remove(task)
        with open(task, 'w', encoding='utf-8') as f:  # the default's own words: no change
            f.write(core.INSTRUCTIONS['fix'] + '\n')
        rig.refresh()
        rig.wait_row('prompts', 'info', 'Domyślne')
    finally:
        for path in (task, user):
            try:
                os.remove(path)
            except OSError:
                pass
        rig.refresh()


def t_other_source(rig):
    s = rig.s
    rig.focus_try()
    rig.activate_source(1)  # plain "Polski"; no Ctrl+R: the app follows mru-sources
    rig.wait_row('source', 'warning', 'Aktywne jest inne źródło')
    rig.nudge()
    s.combo('ctrl', 'alt', 'g')
    rig.wait_try('error', 'Skrót nie dotarł do GramaDramy. Aktywne jest inne źródło.')
    rig.wait_row('hotkey', 'error', 'Nie dotarł do GramaDramy')
    check('skrót' in rig.state()['summary'], 'summary names the shortcut', rig.state()['summary'])
    check(rig.state()['try']['buffer'] == FIXED, 'Ctrl+Alt+G typed nothing into the field', rig.state()['try']['buffer'])
    rig.activate_source(0)
    rig.wait_row('hotkey', 'ok', 'Wolny')
    rig.wait_try('info', 'Teraz powinno działać')
    rig.wait_row('source', 'ok')


def t_sources_removed(rig):
    rig.s.set_sources([('xkb', 'pl')])
    rig.wait_row('source', 'error', 'Brak źródła GramaDrama')  # no Ctrl+R: the app follows the setting
    rig.s.set_sources([('ibus', 'gramadrama'), ('xkb', 'pl')])
    wait_until(lambda: rig.s.ibus_engine_status()['engine'] == 'gramadrama', 10, what='the engine back')
    rig.wait_row('source', 'ok')


def t_conflict(rig):
    path = CUSTOM_ROOT + 'custom0/'
    rig.gsettings('set', MEDIA, 'custom-keybindings', "['%s']" % path)
    for key, value in (('name', 'Mój skrót'), ('command', 'true'), ('binding', '<Control><Alt>g')):
        rig.gsettings('set', '%s:%s' % (CUSTOM, path), key, value)
    rig.refresh()
    row = rig.wait_row('hotkey', 'error', 'Zajęty przez skrót GNOME „Ctrl+Alt+G: Mój skrót”')
    note(row['text'])
    rig.gsettings('set', MEDIA, 'custom-keybindings', '[]')
    rig.refresh()
    rig.wait_row('hotkey', 'ok')


def t_legacy_shortcut(rig):
    """The installer removes the old GTK app's shortcut entry, and only that one."""
    legacy, other = CUSTOM_ROOT + 'gramadrama/', CUSTOM_ROOT + 'custom1/'
    rig.gsettings('set', MEDIA, 'custom-keybindings', "['%s', '%s']" % (legacy, other))
    # GVariant text: the old app's command starts with a quote, so it is written as a quoted string.
    for path, name, command in ((legacy, 'GramaDrama — aktywne pole tekstowe', '"\'/home/u/.local/bin/gramadrama\' --selection"'),
                                (other, 'Terminal', 'kgx')):
        for key, value in (('name', name), ('command', command), ('binding', '')):
            rig.gsettings('set', '%s:%s' % (CUSTOM, path), key, value)
    run = ('/usr/bin/python3 -c "import sys; sys.path.insert(0, \'%s\'); import install_engine as I; '
           'print(I.remove_legacy_shortcut())"' % LINUX)
    r = rig.s.sh(run)
    check(r['rc'] == 0 and r['out'].strip() == 'True', 'removed', r)
    check(rig.gsettings('get', MEDIA, 'custom-keybindings') == "['%s']" % other, 'only the old entry went',
          rig.gsettings('get', MEDIA, 'custom-keybindings'))
    check(rig.gsettings('get', '%s:%s' % (CUSTOM, legacy), 'command') == "''", 'its keys are reset')
    r = rig.s.sh(run)
    check(r['out'].strip() == 'False', 'nothing left to remove', r)
    rig.gsettings('set', '%s:%s' % (CUSTOM, other), 'command', 'gramadrama-something --selection')
    rig.gsettings('set', MEDIA, 'custom-keybindings', "['%s']" % other)
    check(rig.s.sh(run)['out'].strip() == 'False', 'another path is never touched')
    rig.gsettings('set', MEDIA, 'custom-keybindings', '[]')


def t_panel_disabled(rig):
    s = rig.s
    check(s.call(*EXTENSIONS, 'DisableExtension', '(s)', [UUID])[0], 'disabled')
    row = rig.wait_row('panel', 'warning', 'Wyłączony')  # no Ctrl+R: ExtensionStateChanged
    note(row['text'])
    check(s.call(*EXTENSIONS, 'EnableExtension', '(s)', [UUID])[0], 'enabled')
    rig.wait_row('panel', 'ok', 'Działa')


def t_panel_login(rig):
    rig.write_info(panel_changed_at=time.time())
    rig.refresh()
    rig.wait_row('panel', 'warning', 'Nowa wersja wczyta się po wylogowaniu i ponownym zalogowaniu')
    check('panel' in rig.state()['summary'], 'summary', rig.state()['summary'])
    rig.write_info(panel_changed_at=time.time() - 86400)  # before this login
    rig.refresh()
    rig.wait_row('panel', 'ok')


def t_copy_differs(rig):
    path = os.path.join(rig.target, 'gramadrama', 'checks.py')
    original = open(path, encoding='utf-8').read()
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('# an older copy\n')
        rig.refresh()
        row = rig.wait_row('engine', 'info', 'Repozytorium ma nowszą wersję')
        check(rig.state()['summary'] == 'Wszystko gotowe', 'a newer repository is not a problem', rig.state()['summary'])
        note(row['text'])
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    rig.refresh()
    rig.wait_row('engine', 'ok')


def t_server(rig):
    stub = rig.stub
    rig.write_settings(llm_url='http://127.0.0.1:%d' % closed_port())
    rig.refresh()
    rig.wait_row('llm', 'error', 'Niedostępny (odmowa połączenia')
    state = rig.state()
    check(state['model']['state'] == 'info', 'no model list without Ollama', state['model'])
    wait_until(lambda: rig.state()['summary'] == 'Wymaga uwagi: serwer modelu', 10, what='the summary')
    rig.write_settings()
    stub.models = ['inny-model']
    rig.refresh()
    wait_until(lambda: rig.state()['model']['state'] == 'error', 5, what='the missing model')
    model = rig.state()['model']
    check('Tego modelu nie ma na serwerze' in model['text'], 'missing model', model)
    check(model['names'] == ['inny-model', BIELIK] and model['selected'] == 1, 'the saved model stays listed', model)
    stub.models, stub.loaded = [BIELIK], [BIELIK]
    rig.refresh()
    wait_until(lambda: 'Wczytany do pamięci' in rig.state()['model']['text'], 5, what='the model in memory')
    stub.chunks, stub.chunk_delay = stream(SAMPLE_FIXED, pieces=3), 0.1
    chats = len(stub.chats())
    rig.action('try-model')
    row = rig.wait_row('model_try', 'ok', 'Model odpowiedział w', timeout=15)
    check('„%s”' % SAMPLE_FIXED in row['text'], 'the answer is shown', row)
    check(len(stub.chats()) == chats + 1 and 'Dzieki za info' in stub.chats()[-1]['messages'][-1]['content'],
          'the sample went to the server')
    wait_until(lambda: rig.state()['summary'] == 'Wszystko gotowe', 10, what='the summary')


def t_services_local(rig):
    """"Ten komputer" (core.LOCATIONS): a first run uses the services on this computer, at their default addresses, which
    can be changed; with nothing there the rows say what to do (and LanguageTool, optional, marks nothing). The choice
    between this computer and a server keeps the server's address for the way back."""
    s, stub = rig.s, rig.stub
    nothing = 'http://127.0.0.1:%d' % closed_port()
    try:
        # a first run: no file at all (the default addresses would be this machine's own Ollama and LanguageTool: a test
        # must never ask them, so the addresses on this computer are a port with nothing on it)
        with open(rig.settings, 'w') as f:
            json.dump({'llm_local_url': nothing, 'languagetool_local_url': nothing}, f)
        rig.refresh_quietly()
        rig.show_tab('connection')
        state = rig.state()
        check(state['services']['locations'] == {'llm': 'local', 'lt': 'local', 'speech': 'local'},
              'everything on this computer', state['services']['locations'])
        check(state['services']['visible']['llm_local'] and not state['services']['visible']['llm_url']
              and not state['services']['visible']['llm_api'] and state['services']['llm_status_title'] == 'Ollama',
              'the address of Ollama, no protocol or key', state['services'])
        check(state['entries']['llm_local'] == nothing, 'the address on this computer shown', state['entries'])
        row = rig.wait_row('llm', 'error', 'Ollama')
        note(row['text'])
        lt = rig.wait_row('lt', 'info', 'Nie działa na tym komputerze')
        check(rig.state()['model']['state'] == 'info' and 'gdy Ollama będzie działać' in rig.state()['model']['text'],
              'the model waits for Ollama', rig.state()['model'])
        # a new install (no model chosen): a step to take, not a fault; LanguageTool is optional and not in it
        check(rig.state()['summary'] == 'Jeszcze jeden krok: model językowy', 'the neutral pill of a first run',
              rig.state()['summary'], lt)
        check(rig.state()['problems'] == [['Ollama', 'connection']], 'the pill leads to Usługi', rig.state()['problems'])
        steps = rig.state()['first_steps']
        check(steps and steps['gnome']['state'] == 'ok' and steps['model'] == rig.state()['rows']['llm']
              and 'Ctrl+Alt+G' in steps['try']['text'], 'Pierwsze kroki: GNOME done, the model as Usługi says it', steps)
        check(not rig.state()['attention']['dictation'], 'no mark on the optional Dyktowanie', rig.state()['attention'])
        if rig.args.shots:
            first_run_shots(rig)
        # the pill opens the tab of the first problem
        rig.show_tab('start')
        rig.click_widget('summary')
        wait_until(lambda: rig.state()['tab'] == 'connection', 5, what='the pill opens Usługi')
        check(rig.state()['highlight'] == 'connection', 'and rings the language model', rig.state()['highlight'])
        # the address on this computer: the default comes back when it is cleared; another computer's is refused
        rig.type_into('llm_local_entry', 'http://192.0.2.10:11434')
        time.sleep(0.8)
        check(rig.saved_settings().get('llm_local_url') == nothing, 'an address of another computer is refused', rig.saved_settings())
        check('na tym komputerze' in (rig.state()['toast'] or ''), 'and said why', rig.state()['toast'])
        rig.type_into('llm_local_entry', 'http://127.0.0.1:11434')
        wait_until(lambda: rig.saved_settings().get('llm_local_url') == '', 5, what='the default saved as empty')
        check(rig.state()['entries']['llm_local'] == 'http://127.0.0.1:11434', 'and shown', rig.state()['entries'])
        # this computer's Ollama is the stub at another port: it works, with its list of models
        rig.type_into('llm_local_entry', stub.url)
        wait_until(lambda: rig.saved_settings().get('llm_local_url') == stub.url, 5, what='another local address saved')
        rig.wait_row('llm', 'ok', 'Ollama')
        wait_until(lambda: rig.state()['services']['visible']['pull'], 5, what='the row to pull a model while Ollama runs')
        # pulling a model: its progress, and it is chosen, as none was
        rig.stub.chunk_delay = 0.2
        rig.type_into('pull_entry', 'gemma4:e4b')
        wait_until(lambda: (rig.state()['services']['pull'] or [None] * 5)[4], 15, what='the pull done')
        rig.stub.chunk_delay = 0
        wait_until(lambda: rig.saved_settings().get('model') == 'gemma4:e4b', 5, what='the pulled model chosen')
        wait_until(lambda: rig.state()['model']['state'] == 'ok', 10, what='the model there')
        wait_until(lambda: rig.state()['first_steps'] is None, 5, what='Pierwsze kroki gone with a model chosen')
        # a server: its rows, and the way back keeps the local address and the server's
        rig.toggle('llm_where', 1)
        wait_until(lambda: rig.saved_settings().get('llm_location') == 'server', 5, what='the server chosen')
        wait_until(lambda: rig.state()['services']['visible']['llm_url'] and not rig.state()['services']['visible']['llm_local'],
                   5, what='the server rows')
        rig.wait_row('llm', 'warning', 'Podaj adres serwera modelu')
        rig.type_into('llm_entry', stub.url)
        wait_until(lambda: rig.saved_settings().get('llm_url') == stub.url, 5, what='the server address saved')
        rig.wait_row('llm', 'warning', 'to Ollama')  # a first run's protocol is OpenAI's, and the stub is an Ollama
        rig.choose('llm_api_row', 1)
        rig.wait_row('llm', 'ok', 'Działa')
        rig.toggle('llm_where', 0)
        wait_until(lambda: rig.saved_settings().get('llm_location') == 'local', 5, what='this computer again')
        check(rig.saved_settings().get('llm_url') == stub.url and rig.saved_settings().get('llm_local_url') == stub.url,
              'both addresses kept', rig.saved_settings())
        # LanguageTool on a server, and back
        rig.toggle('lt_where', 1)
        wait_until(lambda: rig.saved_settings().get('languagetool_location') == 'server', 5, what='LanguageTool on a server')
        rig.wait_row('lt', 'info', 'opcjonalny')
        rig.type_into('lt_entry', stub.url)
        rig.wait_row('lt', 'ok', 'Działa')
        privacy = dict(rig.state()['privacy'])
        check('tym komputerze (Ollama)' in privacy['Korekta i tłumaczenie'],
              'Prywatność names where the text goes', privacy)
        check('LanguageTool na serwerze 127.0.0.1' in privacy['Sprawdzanie podczas pisania'], 'and LanguageTool\'s', privacy)
        if rig.args.shots:
            rig.s.place_window('GramaDrama', 40, 30, WIDTH, SHOT_HEIGHT)
            time.sleep(0.6)
            rig.shot_window(os.path.join(rig.args.shots, 'services-local.png'))
            rig.s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)
    finally:
        rig.write_settings()
        rig.refresh()
    rig.wait_row('llm', 'ok')


def t_show_args(rig):
    """gramadrama-app --show=<tab> and --login, as a failed correction's button starts it (engine/problems.py): the
    running window opens that tab and rings the group the cause is in for a few seconds; another tab just opens."""
    launcher = shlex.quote(os.path.join(rig.target, 'gramadrama-app'))
    for args, tab, highlight in (('--show=connection', 'connection', 'connection'),
                                 ('--show=writing', 'writing', 'writing'), ('--show=privacy', 'privacy', None)):
        r = rig.s.sh('/usr/bin/python3 %s %s' % (launcher, args))
        check(r['rc'] == 0, '%s: the second start hands over and ends' % args, r)
        wait_until(lambda: rig.state().get('tab') == tab, 5, what='%s: the %s tab' % (args, tab))
        check(rig.state().get('highlight') == highlight, '%s: the group pointed to' % args, rig.state().get('highlight'))
        if highlight == 'connection' and rig.args.shots:
            rig.s.place_window('GramaDrama', 40, 30, WIDTH, SHOT_HEIGHT)
            rig.action('unfocus')
            time.sleep(0.8)
            rig.shot_window(os.path.join(rig.args.shots, 'point-to-model.png'))
            rig.s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)
    rig.s.sh('/usr/bin/python3 %s --show=connection' % launcher)
    wait_until(lambda: rig.state().get('highlight') == 'connection', 5, what='the ring again')
    wait_until(lambda: rig.state().get('highlight') is None, 10, what='the ring gone after a few seconds')
    rig.show_tab('start')


def first_run_shots(rig):
    """A first run with nothing on this computer, as a new user meets it (with --shots): Start with its pill, the panel
    with the error over Wypróbuj, and Usługi (first-run-*.png, the starting point of the error flow in the design)."""
    s, out = rig.s, rig.args.shots
    s.place_window('GramaDrama', 40, 30, WIDTH, SHOT_HEIGHT)
    time.sleep(0.6)
    rig.show_tab('start')
    rig.action('unfocus')
    s.pointer_to(WIDTH + 400, HEIGHT - 40)
    time.sleep(0.5)
    rig.shot_window(os.path.join(out, 'first-run-start.png'))
    rig.focus_try()
    rig.nudge()
    s.combo('ctrl', 'alt', 'g')
    rig.wait_try('error', 'Korekta się nie udała', timeout=15)
    s.pointer_to(WIDTH + 400, HEIGHT - 40)
    time.sleep(0.5)
    rig.shot_window(os.path.join(out, 'first-run-panel.png'), with_panel=True)
    s.tap('esc')
    rig.show_tab('connection')
    rig.action('unfocus')
    time.sleep(0.5)
    rig.shot_window(os.path.join(out, 'first-run-services.png'))
    s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)


def shoot_tabs(rig, out):
    """Every tab in light and dark, in the window's size for pictures; the first tab is app-light.png / app-dark.png."""
    rig.s.place_window('GramaDrama', 40, 30, WIDTH, SHOT_HEIGHT)
    time.sleep(1)
    for variant in ('light', 'dark'):
        if variant == 'dark':
            rig.gsettings('set', 'org.gnome.desktop.interface', 'color-scheme', 'prefer-dark')
            wait_until(lambda: rig.state().get('dark'), 5, what='the dark variant')
            time.sleep(0.8)
        for tab in TAB_NAMES:
            rig.show_tab(tab)
            rig.action('unfocus')  # a page shows the entry focused in it before: no caret or selection in the pictures
            time.sleep(0.5)
            rig.shot_window(os.path.join(out, 'app-%s.png' % variant if tab == 'start' else 'app-%s-%s.png' % (tab, variant)))
    rig.show_tab('start')
    rig.gsettings('set', 'org.gnome.desktop.interface', 'color-scheme', 'default')
    wait_until(lambda: not rig.state().get('dark'), 5, what='the light variant')
    rig.s.place_window('GramaDrama', 40, 30, WIDTH, HEIGHT)


def t_shots(rig):
    out = rig.args.shots
    if not out:
        note('skipped (no --shots)')
        return
    if rig.apps().get('frozen'):
        rig.elsewhere()  # the list sorted, as it is when the user comes back to the window
        rig.back()
    rig.action('focus-try')  # no entry of another tab focused (and selected) in the pictures
    rig.s.pointer_to(WIDTH + 400, HEIGHT - 40)  # no hover highlight in the window
    wait_until(lambda: rig.state()['summary'] == 'Wszystko gotowe', 10, what='the checks done')
    # apps_mode leaves a queue of toasts (one at a time, 4 s each)
    wait_until(lambda: rig.state()['toasts'] == 0, 60, what="the app's toasts gone")
    time.sleep(2)  # the panel's "Zastosowano" toast is gone, and the last toast's fade-out
    shoot_tabs(rig, out)
    note('screenshots in %s' % out)


# The pictures of the documentation (docs_shots): the docs are English, so is the interface in them. The services are
# those of a typical setup on their usual local ports (any free port when one is taken): an OpenAI-compatible model
# server, LanguageTool and a speech server; no made-up version of a real program is shown.
DOCS_PORTS = (('llm', 8080), ('lt', 8081), ('speech', 8000))
# Every window but the app's minimized (Chrome stays connected): only the desktop beside the panel in try-panel-light.png
DOCS_MINIMIZE_JS = '''global.get_window_actors().map(a => a.meta_window)
  .filter(w => !(w.get_title() || '').startsWith('GramaDrama')).forEach(w => w.minimize()); true'''
DOCS_MODEL = 'gemma-4-e4b-it'
DOCS_TRY = 'Hi Anna, I am sending the corected version of the report. Let me know if you have any coments by Friday.'
DOCS_FIXED = 'Hi Anna, I am sending the corrected version of the report. Let me know if you have any comments by Friday.'


def english(message):
    """The app's English text for a Polish one (gramadrama/locales/en), as the docs pictures show it."""
    folder = os.path.join(LINUX, 'gramadrama', 'locales', 'en')
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), encoding='utf-8') as f:
            text = json.load(f).get(message)
        if text:
            return text
    raise Fail('no English text for %r' % message)


def docs_stub(port):
    try:
        return Stub(port).__enter__()
    except OSError:  # the port is taken on this computer
        return Stub().__enter__()


def t_docs_shots(rig):
    """docs/app/*.png: the app started again in English (its language and the apps' names), the services of DOCS_PORTS,
    the extension connected in a Chrome of the session, the repository's commit as the engine's version, and an
    English sentence corrected in Try it with the panel over it. Last of all, so nothing after it sees the change."""
    if not rig.args.docs:
        note('skipped (no --docs)')
        return
    s, out = rig.s, os.path.join(rig.args.shots, 'docs')
    os.makedirs(out, exist_ok=True)
    stubs, chrome = {}, None
    try:
        for name, port in DOCS_PORTS:
            stubs[name] = docs_stub(port)
        llm, lt, speech = stubs['llm'], stubs['lt'], stubs['speech']
        llm.models, llm.get_status['/api/version'] = [DOCS_MODEL], 404  # an OpenAI-compatible server, no Ollama
        llm.events, llm.chunk_delay = sse(DOCS_FIXED, pieces=4), 0.25
        speech.models = ['whisper-1']
        commit = subprocess.run(['git', '-C', LINUX, 'rev-parse', '--short', 'HEAD'], capture_output=True,
                                text=True).stdout.strip() or COMMIT
        rig.write_info(commit=commit)
        rig.write_settings(llm_location='server', llm_url=llm.url, llm_api='openai', model=DOCS_MODEL,
                           languagetool_location='server', languagetool_url=lt.url,
                           whisper_backend='server', whisper_api='openai', whisper_url=speech.url,
                           primary_language='en', secondary_language='pl')
        rig.installer('I.register_browser_hosts()')
        extension = os.path.join(os.path.dirname(LINUX), 'browser', 'extension')
        chrome = s.spawn_chrome(url='about:blank', app=False, wait=False, extension=extension, name='chromedocs')
        rig.app.kill()
        wait_until(lambda: s.window('GramaDrama') is None, 10, what='the window closed')
        os.unlink(rig.state_path)  # start_app waits on the state: the new app's, not the one it left
        rig.start_app(env={'LANGUAGE': 'en'})
        wait_until(lambda: rig.state().get('ui_language') == 'en', 10, what='the English interface')
        for row in ('llm', 'lt', 'whisper', 'dictation'):
            rig.wait_row(row, 'ok', None, timeout=20)
        rig.wait_row('browser', 'ok', None, timeout=40)
        s.eval(DOCS_MINIMIZE_JS)
        s.activate_window('GramaDrama')
        # Try it: an English sentence, the panel with the proposal, then applied (the shortcut is confirmed)
        rig.show_tab('start')
        rig.focus_try()
        s.combo('ctrl', 'a')  # the field may hold the app's sample text
        s.tap('backspace')
        s.type_text(DOCS_TRY, mode='keysyms')
        try:
            wait_until(lambda: rig.state()['try']['buffer'] == DOCS_TRY, 10, what='the sentence typed')
        except TimeoutError:
            raise Fail('Try it does not hold the sentence', rig.state()['try'].get('buffer'))
        rig.nudge()
        asked = len(llm.completions())  # the OpenAI protocol: /v1/chat/completions
        s.combo('ctrl', 'alt', 'g')
        rig.wait_try('ok', english('Gotowe. Enter wstawi poprawkę, Esc anuluje.'), timeout=15)
        check(len(llm.completions()) == asked + 1 and DOCS_TRY in llm.completions()[-1]['messages'][-1]['content'],
              'the sentence went to the model server', len(llm.completions()))
        time.sleep(0.5)
        rig.shot_window(os.path.join(out, 'try-panel-light.png'), with_panel=True)
        s.tap('ret')
        rig.wait_try('ok', english('Zastosowano. Skrót, silnik i serwer działają.'))
        check(rig.state()['try']['buffer'] == DOCS_FIXED, 'the field holds the correction', rig.state()['try']['buffer'])
        rig.action('focus-try')
        s.pointer_to(WIDTH + 400, HEIGHT - 40)
        wait_until(lambda: not any(rig.state()['attention'].values()), 20, what='no tab marked')
        check(rig.state()['summary'] == english('Wszystko gotowe'), 'summary', rig.state()['summary'])
        wait_until(lambda: rig.state()['toasts'] == 0, 60, what="the app's toasts gone")
        time.sleep(2)
        shoot_tabs(rig, out)
    finally:
        if chrome is not None:
            chrome.kill()
        for stub in stubs.values():
            stub.__exit__(None, None, None)
    os.makedirs(DOCS, exist_ok=True)
    for name in sorted(os.listdir(out)):
        shutil.copy(os.path.join(out, name), os.path.join(DOCS, name))
    note('docs pictures in %s and docs/app (ports %s)' % (out, ', '.join(
        '%s %s' % (name, stub.server.server_port) for name, stub in stubs.items())))


# shots after try and the apps it takes to fill the Aplikacje section: the window then shows a
# confirmed shortcut, nothing to fix, and a list of apps as it looks after some use
TESTS = [('startup', t_startup), ('languages_shortcuts', t_languages_shortcuts), ('dictionary', t_dictionary), ('tabs', t_tabs), ('try', t_try), ('apps_list', t_apps_list), ('apps_fix', t_apps_fix),
         ('apps_switch', t_apps_switch), ('apps_mode', t_apps_mode), ('shots', t_shots), ('apps_add', t_apps_add), ('apps_undo', t_apps_undo),
         ('apps_override', t_apps_override), ('apps_repair', t_apps_repair), ('cancel', t_cancel),
         ('address_edit', t_address_edit), ('server_settings', t_server_settings), ('services_local', t_services_local), ('whisper_server', t_whisper_server),
         ('whisper_target', t_whisper_target), ('login', t_login), ('route', t_route), ('browser', t_browser), ('prompts', t_prompts), ('settings_broken', t_settings_broken), ('other_source', t_other_source),
         ('sources_removed', t_sources_removed), ('conflict', t_conflict), ('legacy_shortcut', t_legacy_shortcut),
         ('panel_disabled', t_panel_disabled), ('panel_login', t_panel_login), ('copy_differs', t_copy_differs),
         ('server', t_server), ('show_args', t_show_args), ('docs_shots', t_docs_shots)]  # docs_shots last: it starts the app again in English


# ---- E1: which command GNOME Shell starts after the launch fix appears (docs/apps.md, sections 6 and 9) ------
# A session of its own (--e1): the original .desktop files have to be in a system data dir, and one
# override has to be there before GNOME Shell starts. A test app writes down the arguments it was
# started with; the launch fix is planned and written by gramadrama/apps.py inside the session. Every
# round starts its app through a launcher: search (lookup_app, as a search result does), the favorites
# (Ubuntu Dock when it is installed, else the overview's dash) or the app grid. Rounds:
#   running  an override written while the app runs; the app quit and started again
#   stopped  an override written while the app does not run (it ran before); then written again
#   login    an override that was there when GNOME Shell started, then deleted (the undo)
#   wrapper  a wrapper in the first PATH directory, written while the app runs
#   hidden   the override first written with NoDisplay=true, then without it (its outcome is a note,
#            not a check: apps.py does not use it)
E1_DOCK = '/usr/share/gnome-shell/extensions/ubuntu-dock@ubuntu.com'
E1_FLAG = '--ozone-platform=wayland'
E1_PREFIX = 'org.gramadrama.E1.'
E1_LAUNCHERS = ('search', 'favorites', 'grid')
E1_APP = r'''#!/usr/bin/python3
"""E1 test app: writes down how it was started, then shows a window until it is closed."""
import json, os, sys, time
ID = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--e1-id=')), 'org.gramadrama.E1.unknown')
with open(%(log)r, 'a') as f:
    f.write(json.dumps({'id': ID, 'pid': os.getpid(), 'argv': sys.argv[1:], 't': time.time(),
                        'desktop_file': os.environ.get('GIO_LAUNCHED_DESKTOP_FILE')}) + '\n')
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, Gtk
app = Gtk.Application(application_id=ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect('activate', lambda a: Gtk.ApplicationWindow(application=a, title='E1 ' + ID).present())
sys.exit(app.run(sys.argv[:1]))  # the flag is not GApplication's
'''
# Runs inside the session: plans the fix as the app does and writes it (mode 'hidden': with NoDisplay=true),
# then reads it back as the app would.
E1_FIX = r'''import dataclasses, json, os, sys
from pathlib import Path
sys.path.insert(0, %(linux)r)
from gramadrama import apps as A, checks
app_id, mode, bin_dir = sys.argv[1:4]
places = A.Places(tuple(os.environ['PATH'].split(':')), Path(bin_dir), checks.data_home() / 'applications')
entry = A.installed_apps(path=places.path)[app_id]
detection = A.detect(entry, places)
plan = A.plan_fix(app_id, entry, detection, A.kind_of(app_id, detection), places)
assert plan.kind is None or str(plan.path).startswith((bin_dir + '/', os.environ['XDG_DATA_HOME'] + '/')), plan.path
if mode == 'hidden':
    plan = dataclasses.replace(plan, text=plan.text.replace('[Desktop Entry]\n', '[Desktop Entry]\nNoDisplay=true\n', 1))
error = A.apply_fix(plan) if plan.kind else plan.reason
# as the app sees it now: pending while GNOME Shell started before the fix, and this session's GNOME Shell
entry = A.installed_apps(path=places.path)[app_id]
fix = A.fix_status(entry, A.detect(entry, places), places, A.shell_started())
with open(os.path.join(os.environ['GD_WORK'], 'shell.pid')) as f:
    shell = checks.process_start(int(f.read()))
print(json.dumps({'kind': plan.kind, 'path': str(plan.path), 'error': error, 'pending': fix.pending if fix else None,
                  'this_shell': shell is not None and abs(A.shell_started() - shell) < 1}))
'''
E1_WATCH_JS = ('globalThis.gdE1 = {changed: 0}; imports.gi.Shell.AppSystem.get_default().connect("installed-changed", '
               '() => gdE1.changed++); true')
E1_STATE_JS = '''(id => { const Shell = imports.gi.Shell, t = Shell.WindowTracker.get_default();
  const grid = gdMain.overview._overview._controls._appDisplay._items.get(id);
  return {changed: gdE1.changed, grid: grid ? grid.app.get_app_info().get_filename() : null,
          running: Shell.AppSystem.get_default().get_running().some(a => a.get_id() === id),
          windows: global.get_window_actors().filter(a => (t.get_window_app(a.meta_window) || {get_id: () => ''}).get_id() === id).length};
})(%s)'''
# Each returns the .desktop file of the app it starts (null: no icon), and starts it.
E1_LAUNCH_JS = {
    'search': '''(id => { const app = imports.gi.Shell.AppSystem.get_default().lookup_app(id);
        const file = app.get_app_info().get_filename(); app.activate(); return file; })(%s)''',
    'favorites': '''(id => { const dock = gdMain.layoutManager._trackedActors.map(t => t.actor).find(a => a.dash && a.dash._box);
        const icon = (dock ? dock.dash : gdMain.overview.dash)._box.get_children().map(c => c.child && c.child._delegate)
            .find(i => i && i.app && i.app.get_id() === id);
        if (!icon) return null; const file = icon.app.get_app_info().get_filename(); icon.activate(1); return file; })(%s)''',
    'grid': '''(id => { const icon = gdMain.overview._overview._controls._appDisplay._items.get(id);
        if (!icon) return null; const file = icon.app.get_app_info().get_filename(); icon.activate(1); return file; })(%s)''',
}
E1_QUIT_JS = '''(id => { const t = imports.gi.Shell.WindowTracker.get_default();
  const windows = global.get_window_actors().map(a => a.meta_window).filter(w => (t.get_window_app(w) || {get_id: () => ''}).get_id() === id);
  windows.forEach(w => w.delete(global.get_current_time())); return windows.length; })(%s)'''
E1_APPS = {  # name: (program: absolute path or bare name, a favorite)
    'running_search': ('abs', False), 'running_favorites': ('abs', True), 'running_grid': ('abs', False),
    'stopped': ('abs', False), 'login': ('abs', False), 'hidden': ('abs', False),
    'wrapper_search': ('bare', False), 'wrapper_favorites': ('bare', True), 'wrapper_grid': ('bare', False)}


class E1Rig:
    def __init__(self, s, tree, dock):
        self.s, self.tree, self.dock = s, tree, dock
        self.log = os.path.join(tree, 'argv.jsonl')

    @staticmethod
    def app_id(name):
        return E1_PREFIX + name + '.desktop'

    def records(self, name):
        try:
            with open(self.log, encoding='utf-8') as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        return [r for r in map(json.loads, filter(None, lines)) if r['id'] == E1_PREFIX + name]

    def where(self, path):
        """The .desktop file a launch came from (GIO_LAUNCHED_DESKTOP_FILE), shortened."""
        path = path or '?'
        for base, label in ((self.s.env['XDG_DATA_HOME'], '~/.local/share'), (self.tree, 'system')):
            if path.startswith(base + '/'):
                return label + path[len(base):]
        return path

    def state(self, name):
        return self.s.eval(E1_STATE_JS % json.dumps(self.app_id(name)))

    def changed(self):
        return self.s.eval('gdE1.changed')

    def wait_changed(self, before, what):
        """installed-changed after a .desktop file changed (the shell waits about 5 s for more changes)."""
        started = time.monotonic()
        wait_until(lambda: self.changed() > before, 30, what='installed-changed after %s' % what)
        return time.monotonic() - started

    def fix(self, name, mode='write'):
        """The launch fix, planned and written by gramadrama/apps.py in the session. An override is
        waited for until GNOME Shell has read it; a wrapper needs no waiting."""
        before = self.changed()
        r = self.s.sh('/usr/bin/python3 %s %s %s %s' % (os.path.join(self.tree, 'fix.py'), self.app_id(name), mode,
                                                         os.path.join(self.tree, 'localbin')))
        check(r['rc'] == 0, 'apps.py planned and wrote the fix', r)
        out = json.loads(r['out'])
        check(out['error'] is None, 'the fix written', out)
        check(out['this_shell'], "apps.shell_started() is this session's GNOME Shell, not the desktop's", out)
        check(out['pending'] is (out['kind'] == 'override'), 'apps.fix_status: a new override is pending, a wrapper never',
              out)
        if out['kind'] == 'override':
            note('%s: override written; installed-changed after %.1fs' % (name, self.wait_changed(before, name)))
        return out

    def show(self, launcher):
        """What a user opens first: the app grid, or for the favorites the overview (the dock needs nothing)."""
        if launcher == 'grid':
            self.s.eval('gdMain.overview.showApps(); true')
        elif launcher == 'favorites' and not self.dock:
            self.s.eval('gdMain.overview.show(); true')
        else:
            return
        wait_until(lambda: self.s.eval('gdMain.overview.visible && !gdMain.overview.animationInProgress'), 10,
                   what='the overview')
        time.sleep(1)  # the views redisplay once they are mapped (Main.queueDeferredWork)

    def launch(self, launcher, name):
        """Start an app through a launcher: (the .desktop file the launcher holds, the app's record)."""
        before = len(self.records(name))
        self.show(launcher)
        # null until the launcher has an icon for the app (a dock redisplays when GNOME Shell reread the files)
        used = wait_until(lambda: self.s.eval(E1_LAUNCH_JS[launcher] % json.dumps(self.app_id(name))), 5,
                          what='%s to show %s' % (launcher, name))
        record = wait_until(lambda: (lambda r: len(r) > before and r[before])(self.records(name)), 15,
                            what='%s started through %s' % (name, launcher))
        wait_until(lambda: self.state(name)['windows'], 15, what='the window of %s' % name)
        self.s.eval('gdMain.overview.hide(); true')
        return used, record

    def quit(self, name, record):
        """Close its window, as the user would; the app then exits."""
        self.s.eval(E1_QUIT_JS % json.dumps(self.app_id(name)))
        pid = record['pid']
        try:
            wait_until(lambda: not os.path.exists('/proc/%d' % pid), 10, what='%s to exit' % name)
        except TimeoutError:
            self.s.kill_pids([pid], signal.SIGTERM)
            wait_until(lambda: not os.path.exists('/proc/%d' % pid), 5, what='%s to exit' % name)
        wait_until(lambda: not self.state(name)['running'], 10, what='%s stopped for GNOME Shell' % name)

    def start(self, launcher, name, flag, label):
        """Start, note and check whether the app got the flag, quit."""
        used, record = self.launch(launcher, name)
        got = E1_FLAG in record['argv']
        note('%-8s %-9s %-7s  %s' % (label, launcher, 'flag' if got else 'no flag', self.where(record['desktop_file'])))
        self.quit(name, record)
        check(got == flag, '%s, %s: %s' % (label, launcher, 'the flag expected' if flag else 'no flag expected'),
              record['argv'], used)
        return got


def e1_running(rig):
    """An override written while the app runs: search and the favorites start it after the next start,
    the app grid does not."""
    for launcher in E1_LAUNCHERS:
        name = 'running_' + launcher
        _used, record = rig.launch(launcher, name)
        check(E1_FLAG not in record['argv'], 'no flag before the fix', record)
        check(rig.fix(name)['kind'] == 'override', 'an absolute Exec gets an override')
        rig.quit(name, record)
        rig.start(launcher, name, launcher != 'grid', 'running')


def e1_stopped(rig):
    """An override written while the app does not run, and written again: the app grid still starts the
    command it read at login."""
    rig.start('grid', 'stopped', False, 'before')
    rig.fix('stopped')
    rig.start('grid', 'stopped', False, 'stopped')
    rig.start('search', 'stopped', True, 'stopped')
    rig.fix('stopped')  # "Napraw" or "Włącz" again: the same text once more
    rig.start('grid', 'stopped', False, 'rewrite')


def e1_login(rig):
    """An override that was there when GNOME Shell started: every launcher starts it with the flag. The
    undo: deleted, search starts the original at once, the app grid keeps the flag until the next login."""
    rig.start('grid', 'login', True, 'login')
    rig.start('search', 'login', True, 'login')
    before = rig.changed()
    os.unlink(os.path.join(rig.tree, 'login', 'applications', rig.app_id('login')))
    note('login: override deleted; installed-changed after %.1fs' % rig.wait_changed(before, 'the undo'))
    rig.start('search', 'login', False, 'undo')
    rig.start('grid', 'login', True, 'undo')


def e1_wrapper(rig):
    """A wrapper written while the app runs: every launcher starts it with the flag after the next start;
    nothing waits for GNOME Shell."""
    for launcher in E1_LAUNCHERS:
        name = 'wrapper_' + launcher
        _used, record = rig.launch(launcher, name)
        check(rig.fix(name)['kind'] == 'wrapper', 'a bare name with ~/.local/bin first in PATH gets a wrapper')
        rig.quit(name, record)
        rig.start(launcher, name, True, 'wrapper')


def e1_hidden(rig):
    """A note only: can the app grid be made to read an override without a new login, by hiding the
    app from it for one redisplay (NoDisplay=true, then the override without it)?"""
    rig.start('grid', 'hidden', False, 'before')
    rig.fix('hidden', 'hidden')
    started = time.monotonic()
    try:
        wait_until(lambda: rig.state('hidden')['grid'] is None, 40, what='the icon to leave the app grid')
    except TimeoutError:
        note('hidden: the app grid kept its icon for 40 s')
        return
    note('hidden: the icon left the app grid %.1fs after installed-changed' % (time.monotonic() - started))
    rig.fix('hidden')
    started = time.monotonic()
    wait_until(lambda: rig.state('hidden')['grid'], 40, what='the icon back in the app grid')
    note('hidden: the icon came back %.1fs after installed-changed' % (time.monotonic() - started))
    used, record = rig.launch('grid', 'hidden')
    note('hidden: the app grid started it %s' % ('with the flag' if E1_FLAG in record['argv'] else 'WITHOUT the flag'))
    rig.quit('hidden', record)


E1_ROUNDS = [('running', e1_running), ('stopped', e1_stopped), ('login', e1_login), ('wrapper', e1_wrapper),
             ('hidden', e1_hidden)]


def e1_tree(tree):
    """The system side of E1: the test program (a Chromium-like directory, for apps.detect), bare names in
    bin/ for the wrappers, the original .desktop files in share/, and the override that is there at login
    in login/ (a data dir before share/, as ~/.local/share is)."""
    sys.path.insert(0, LINUX)
    from gramadrama import apps
    program = os.path.join(tree, 'opt', 'e1', 'e1app')
    for d in ('opt/e1', 'bin', 'localbin', 'share/applications', 'login/applications'):
        os.makedirs(os.path.join(tree, d))
    with open(program, 'w') as f:
        f.write(E1_APP % {'log': os.path.join(tree, 'argv.jsonl')})
    os.chmod(program, 0o755)
    open(os.path.join(tree, 'opt', 'e1', 'resources.pak'), 'w').close()
    with open(os.path.join(tree, 'fix.py'), 'w') as f:
        f.write(E1_FIX % {'linux': LINUX})
    favorites = []
    for name, (kind, favorite) in E1_APPS.items():
        command = program
        if kind == 'bare':
            command = 'e1-' + name.replace('_', '-')
            os.symlink(program, os.path.join(tree, 'bin', command))
        app_id = E1Rig.app_id(name)
        path = os.path.join(tree, 'share', 'applications', app_id)
        with open(path, 'w') as f:
            f.write('[Desktop Entry]\nType=Application\nName=E1 %s\nExec=%s --e1-id=%s %%U\nIcon=application-x-executable\n'
                    'StartupWMClass=%s\n' % (name, command, E1_PREFIX + name, E1_PREFIX + name))
        if name == 'login':
            with open(path, encoding='utf-8') as f:
                text = apps.override_text(f.read(), 'E1 login', path)
            with open(os.path.join(tree, 'login', 'applications', app_id), 'w') as f:
                f.write(text)
        if favorite:
            favorites.append(app_id)
    return favorites


def run_e1(args):
    """Experiment E1 in a session of its own; prints what each launcher started."""
    t0 = time.time()
    tree = tempfile.mkdtemp(prefix='gramadrama-e1-')
    logs = args.logs or tempfile.mkdtemp(prefix='e1-logs-')
    dock = os.path.isdir(E1_DOCK)
    try:
        favorites = e1_tree(tree)
        dirs = os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share'
        env = {'PATH': '%s/localbin:%s/bin:/usr/local/bin:/usr/bin:/bin' % (tree, tree),
               'XDG_DATA_DIRS': '%s/login:%s/share:%s' % (tree, tree, dirs)}
        s = Session(env=env, extensions=[E1_DOCK] if dock else [], name='e1', copy_logs_to=logs, verbose=args.verbose)
        try:
            with s:
                rig = E1Rig(s, tree, dock)
                s.eval(E1_WATCH_JS)
                r = s.sh('gsettings set org.gnome.shell favorite-apps "%s"' % str(favorites))
                check(r['rc'] == 0, 'favorites set', r)
                note('favorites: %s' % ('Ubuntu Dock' if dock else "the overview's dash"))
                for name, fn in E1_ROUNDS:
                    started = time.monotonic()
                    try:
                        fn(rig)
                        record('e1 %s (%.1fs)' % (name, time.monotonic() - started), True)
                    except Exception as exc:
                        if args.verbose or not isinstance(exc, (Fail, TimeoutError)):
                            traceback.print_exc()
                        record('e1 ' + name, False, exc)
        except Exception as exc:
            traceback.print_exc()
            record('e1 session', False, exc)
        finally:
            leftovers = getattr(s, 'leftovers', None) or {}
            dirty = {k: v for k, v in leftovers.items() if v}
            record('e1 clean teardown', not dirty, dirty)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
        if not args.logs:
            shutil.rmtree(logs, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print('E1 %s (%d checks, %d failed, %.0fs)' % ('FAIL' if failed else 'PASS', len(results), len(failed),
                                                   time.time() - t0), flush=True)
    return 1 if failed or not results else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--only', help='comma-separated test names: %s (legacy_app and startup always run)'
                    % ','.join(n for n, _ in TESTS))
    ap.add_argument('--shots', help='directory for screenshots of the window')
    ap.add_argument('--docs', action='store_true', help='run docs_shots: refresh docs/app/*.png (needs --shots)')
    ap.add_argument('--logs', help='copy the session logs here')
    ap.add_argument('--e1', action='store_true', help='only experiment E1 of docs/apps.md, in a session of its own')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    if args.e1:
        return run_e1(args)
    only = set(args.only.split(',')) if args.only else None
    if only and not only <= {n for n, _ in TESTS}:
        ap.error('unknown test in --only: %s' % sorted(only - {n for n, _ in TESTS}))
    if args.docs and not args.shots:
        ap.error('--docs needs --shots DIR')
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)
    t0 = time.time()
    logs = args.logs or tempfile.mkdtemp(prefix='app-logs-')
    # The fake apps (apps_tree) come first in the session's PATH and XDG_DATA_DIRS; the PATH leaves out
    # the real ~/.local/bin, whose wrappers must never count in the session.
    tree = os.path.realpath(tempfile.mkdtemp(prefix='gramadrama-apps-'))
    apps_tree(tree)
    env = {'PATH': '%s/localbin:%s/bin:/usr/local/bin:/usr/bin:/bin' % (tree, tree),
           'XDG_DATA_DIRS': '%s/share:%s' % (tree, os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share')}
    with Stub() as stub:
        stub.models = [BIELIK]
        s = Session(sources=[('xkb', 'pl')], engine=True, engine_exec=ENGINE_EXEC, env=env, extensions=[EXTENSION],
                    name='app', size=SIZE, copy_logs_to=logs, verbose=args.verbose)
        try:
            with s:
                rig = Rig(s, stub, args, tree)
                rig.install()
                rig.write_settings()  # use the stub server instead of relying on user configuration
                s.eval('gdMain.messageTray.bannerBlocked = true; true')
                s.set_sources([('ibus', 'gramadrama'), ('xkb', 'pl')])
                wait_until(lambda: s.ibus_engine_status()['engine'] == 'gramadrama', 15, what='the engine active')
                try:
                    t_legacy_app(rig)
                    record('legacy_app', True)
                except Exception as exc:
                    traceback.print_exc()
                    record('legacy_app', False, exc)
                rig.start_app()
                for name, fn in TESTS:
                    if only is not None and name not in only and name != 'startup':
                        continue
                    started = time.monotonic()
                    try:
                        rig.show_tab('start')  # every scenario starts where the window opens
                        fn(rig)
                        record('%s (%.1fs)' % (name, time.monotonic() - started), True)
                    except Exception as exc:
                        if args.verbose or not isinstance(exc, (Fail, TimeoutError)):
                            traceback.print_exc()
                        record(name, False, exc)
                launcher = os.path.join(rig.target, 'gramadrama-engine')
                record('the engine ran from the copied launcher, without test mode',
                       bool(s.pids(r'^/usr/bin/python3 %s --ibus$' % launcher.replace('.', r'\.'))))
        except Exception as exc:
            traceback.print_exc()
            record('session', False, exc)
        finally:
            leftovers = getattr(s, 'leftovers', None) or {}
            dirty = {k: v for k, v in leftovers.items() if v}
            record('clean teardown', not dirty, dirty)
            if not args.logs:
                shutil.rmtree(logs, ignore_errors=True)
            shutil.rmtree(tree, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print('APP %s (%d checks, %d failed, %.0fs)' % ('FAIL' if failed else 'PASS', len(results), len(failed),
                                                    time.time() - t0), flush=True)
    return 1 if failed or not results else 0


if __name__ == '__main__':
    sys.exit(main())
