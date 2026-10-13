#!/usr/bin/env python3
"""Panel suite for the GramaDrama GNOME Shell extension (linux/gnome-extension).

Contract: docs/architecture.md (Panel D-Bus interface, View JSON, Placement and focus).
Design: ../branding/PANEL.md. Every check runs in an isolated headless GNOME Shell session
(tests/session/harness.py) with the extension installed and enabled next to the harness's own,
and drives the extension's D-Bus interface directly with fixture views:

  panel session   GRAMADRAMA_ENGINE_TEST=1 in the session: any caller may Show/Hide/Subscribe, and
                  the extension exposes globalThis.gdGramaDramaPanel.state() for introspection. A
                  GTK4 test window (tests/session/testwin.py) has focus. Covers Ping, every status in
                  the dark and light variants, placement at the IBus caret, button clicks (Action
                  signal, focus unchanged), Kopiuj, Hide, stale sessions, toasts, the vanished-engine
                  rule, invalid views, the focused app (Focus for Chrome on Wayland and Xwayland, the
                  run dialog, no window; unicast; a Subscribe across disable/enable fails), the
                  on-screen keyboard and the disable/enable cycle.
  caller session  no test mode: only a process whose command line ends with
                  ".../gramadrama-engine --ibus" may Show/Hide/Subscribe (a stand-in engine proves
                  both sides).
  --shots DIR     screenshots of every state, dark and light, over gnome-text-editor (and the full
                  screen); --docs also refreshes docs/panel/*.png (English) and the site's panel pictures
                  (site/public/img, Polish).

The stand-in engine (written into the session's work dir) subscribes to the Action and Focus signals
exactly like the engine's PanelClient (sender org.gnome.Shell) and logs them as JSONL. Only a stand-in
that called Subscribe may receive Focus (docs/apps.md, section 1): the others prove it is unicast.

usage: /usr/bin/python3 tests/integration_panel.py [--only NAME[,NAME...]] [--shots DIR] [--docs]
                                                   [--logs DIR] [-v]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'session'))
from harness import HarnessError, Session, wait_until  # noqa: E402

EXTENSION = os.path.join(LINUX, 'gnome-extension')
UUID = 'gramadrama@gramadrama.local'
DOCS = os.path.join(LINUX, 'docs', 'panel')
PANEL = ('org.gnome.Shell', '/pl/gramadrama/Panel', 'pl.gramadrama.Panel1')
DENIED = 'pl.gramadrama.Panel1.Error.AccessDenied'
INVALID = 'pl.gramadrama.Panel1.Error.InvalidArgs'
TEST_ENV = {'GRAMADRAMA_ENGINE_TEST': '1'}
# The caller session's IBus component starts the stand-in engine the way ibus-daemon starts the real
# one: a direct child of ibus-daemon (sh execs python3), at the installed launcher path, with --ibus.
STANDIN_EXEC = '/bin/sh -c \'exec /usr/bin/python3 "$XDG_DATA_HOME/gramadrama-engine/gramadrama-engine" --ibus\''
VERSION = str(json.load(open(os.path.join(EXTENSION, 'metadata.json')))['version'])
PANEL_WIDTH, PAD, GAP, EDGE = 520, 16, 6, 8
HUD_WIDTH, MESSAGE_WIDTH = 230, 420  # hud.js: the pill, and the widest pill of a message
WIN_TITLE = 'GD test GTK4 gtk4'
MODE_NAMES = ['Popraw', 'Ulepsz', 'Formalnie', 'Luźniej', 'Krócej', 'Tłumacz']

# ---- fixture views (docs/architecture.md, View JSON) -----------------------------------------------
SRC = 'Wczoraj poszłem do skelpu, ale zapomniałem portfela wiec musiałem wrócić.'
FIXED = 'Wczoraj poszedłem do sklepu, ale zapomniałem portfela, więc musiałem wrócić.'
SEGMENTS = [['same', 'Wczoraj '], ['removed', 'poszłem'], ['added', 'poszedłem'], ['same', ' do '],
            ['removed', 'skelpu'], ['added', 'sklepu'], ['same', ', ale zapomniałem portfela'], ['added', ','],
            ['same', ' '], ['removed', 'wiec'], ['added', 'więc'], ['same', ' musiałem wrócić.']]
CHANGES = [{'from': 'poszłem', 'to': 'poszedłem', 'reason': 'Gramatyka / styl'},
           {'from': 'skelpu', 'to': 'sklepu', 'reason': 'Literówka'},
           {'from': '', 'to': ',', 'reason': 'Brakujący znak interpunkcyjny'},
           {'from': 'wiec', 'to': 'więc', 'reason': 'Polskie znaki'}]
TR_SRC = ('Dziękuję za szybką odpowiedź. Spotkajmy się w czwartek o 10:00, żeby omówić szczegóły projektu '
          'i harmonogram wdrożenia. Prześlę wcześniej krótką agendę, a po spotkaniu podsumowanie ustaleń '
          'dla całego zespołu, żeby nikt nie musiał niczego zgadywać.')
TR_TEXT = ("Thank you for the quick reply. Let's meet on Thursday at 10:00 to discuss the project details and "
           "the rollout schedule. I'll send a short agenda beforehand, and a summary of what we agree on to the "
           "whole team afterwards, so nobody has to guess.")
# The docs are English, so is the panel in their pictures (--docs): the view the engine sends with an English
# interface, the model of integration_app.py's docs pictures.
EN_SRC = 'Hi Anna I am sending the corected version of the report. Let me know if you have any coments by friday.'
EN_FIXED = 'Hi Anna, I am sending the corrected version of the report. Let me know if you have any comments by Friday.'
EN_SEGMENTS = [['same', 'Hi Anna'], ['added', ','], ['same', ' I am sending the '], ['removed', 'corected'],
               ['added', 'corrected'], ['same', ' version of the report. Let me know if you have any '],
               ['removed', 'coments'], ['added', 'comments'], ['same', ' by '], ['removed', 'friday'],
               ['added', 'Friday'], ['same', '.']]
EN_CHANGES = [{'from': '', 'to': ',', 'reason': 'Missing punctuation'},
              {'from': 'corected', 'to': 'corrected', 'reason': 'Typo'},
              {'from': 'coments', 'to': 'comments', 'reason': 'Typo'},
              {'from': 'friday', 'to': 'Friday', 'reason': 'Capitalization'}]
EN_MODEL = 'gemma-4-e4b-it'
PRIVATE = ('Pole hasła lub prywatne — GramaDrama go nie czyta.', 'Password or private field — GramaDrama does not read it.')
TRICKY = '<b>pogrubienie</b> & <span foreground="red">„cytat”</span> \\n'  # must show literally


def view(session, status='ready', **fields):
    v = {'v': 1, 'session': session, 'kind': 'session', 'status': status, 'mode': 0, 'target': None,
         'scope': 'paragraph', 'source': SRC, 'text': FIXED, 'segments': SEGMENTS, 'changes': CHANGES,
         'count': len(CHANGES), 'note': 'Bielik 11B · 2,9 s', 'error': '', 'message': '',
         'can_apply': True}
    v.update(fields)
    return v


def message(session, text, seconds=None):
    v = {'v': 1, 'session': session, 'kind': 'message', 'status': 'message', 'message': text}
    if seconds is not None:
        v['seconds'] = seconds
    return v


def long_view(session):
    paragraphs = []
    segments = []
    for i in range(9):
        if i:
            segments.append(['same', '\n'])
        segments += [['same', 'Akapit %d. ' % (i + 1)]] + SEGMENTS
        paragraphs.append('Akapit %d. %s' % (i + 1, FIXED))
    changes = [dict(CHANGES[i % len(CHANGES)], reason='%s (%d)' % (CHANGES[i % len(CHANGES)]['reason'], i + 1))
               for i in range(30)]
    return view(session, text='\n'.join(paragraphs), source='\n'.join(p.replace(FIXED, SRC) for p in paragraphs),
                segments=segments, changes=changes, count=36, note='Bielik 11B · 7,4 s')


# status -> (view builder, what the state must show)
UNREACHABLE = 'Nie można połączyć się z serwerem modelu (odmowa połączenia). Sprawdź, czy działa, albo zmień adres w zakładce Usługi.'


def problem_view(session, title='Serwer nie odpowiada', text=UNREACHABLE,
                 actions=(('retry', 'Spróbuj ponownie'), ('open:connection', 'Otwórz Usługi')), group=2):
    """A failed correction as the engine sends it (engine/problems.py): the first action is Enter's."""
    return view(session, 'error', text='', segments=[], changes=[], count=0, note='', error=text,
                problem={'group': group, 'title': title, 'text': text,
                         'actions': [{'id': i, 'label': label} for i, label in actions]})


def state_views(sid):
    partial = FIXED[:44]
    return {
        'checking': view(sid, 'checking', text='', segments=[], changes=[], count=0, note='Bielik 11B'),
        'working': view(sid, 'working', text=partial, segments=[], changes=[], count=0, note='Bielik 11B'),
        'apply_pending': view(sid, 'apply_pending', text=partial, segments=[], changes=[], count=0,
                              note='Bielik 11B'),
        'ready': view(sid, editable=True),
        'ready_list': view(sid, editable=True, changes_open=True),
        'applying': view(sid, 'applying'),
        'unchanged': view(sid, 'unchanged', text=FIXED, source=FIXED, segments=[['same', FIXED]], changes=[],
                          count=0, note='Bielik 11B · 1,8 s'),
        'error': view(sid, 'error', text='', segments=[], changes=[], count=0, note='Bielik 11B',
                      error='Serwer modelu zszedł ze sceny — sprawdź połączenie.'),
        'translate': view(sid, mode=5, target='en', source=TR_SRC, text=TR_TEXT, segments=[], changes=[], count=0,
                          scope='selection', note='Bielik 11B · PL → EN · 3,4 s'),
        'translate_working': view(sid, 'working', mode=5, target='en', source=TR_SRC, text=TR_TEXT[:70],
                                  segments=[], changes=[], count=0, scope='selection', note='Bielik 11B · PL → EN'),
        'translate_pl': view(sid, mode=5, target='pl', source=TR_TEXT, text=TR_SRC, segments=[], changes=[],
                             count=0, note='Bielik 11B · EN → PL · 3,1 s'),
        'readonly': view(sid, can_apply=False),
        'long': long_view(sid),
    }


# ---- stand-in engine -----------------------------------------------------------------------------------
FAKE_ENGINE = r'''#!/usr/bin/python3
"""Stand-in for the GramaDrama IBus engine in panel tests (written by tests/integration_panel.py).

The test starts it with the command line it wants (GD_FAKE_NAME and GD_FAKE_LOG set; its parent is
then the harness's ctl.py), or ibus-daemon starts it from the session's IBus component exactly like
the real engine: then it is named 'ibus', logs next to itself and registers a do-nothing engine
factory, so the engine switch completes. It logs every Action and Focus signal of pl.gramadrama.Panel1
(subscribed like the engine's PanelClient: sender org.gnome.Shell) as JSONL, and forwards
Ping/Show/Hide/Subscribe from its own bus connection on request (UniqueName returns its own name):
pl.gramadrama.TestEngine.<NAME> /pl/gramadrama/TestEngine Call(method, json args) -> json."""
import json
import os
import time

import gi
from gi.repository import Gio, GLib

BY_IBUS = 'GD_FAKE_NAME' not in os.environ
NAME = os.environ.get('GD_FAKE_NAME', 'ibus')
LOG = os.environ.get('GD_FAKE_LOG') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'actions-ibus.jsonl')
FOCUS_LOG = os.path.splitext(LOG)[0] + '-focus.jsonl'
DEST, PATH, IFACE = 'org.gnome.Shell', '/pl/gramadrama/Panel', 'pl.gramadrama.Panel1'
XML = """<node><interface name="pl.gramadrama.TestEngine">
<method name="Call"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>"""
SIGNATURES = {'Ping': (None, '(s)'), 'Show': ('(s)', '(b)'), 'Hide': ('(u)', None), 'Subscribe': (None, None),
              'Hud': ('(s)', '(b)'), 'Live': ('(s)', '(b)'), 'Marks': ('(s)', '(b)')}
conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
log = open(LOG, 'a', buffering=1, encoding='utf-8')
focus_log = open(FOCUS_LOG, 'a', buffering=1, encoding='utf-8')


def on_action(_conn, sender, path, iface, signal, params):
    session, action = params.unpack()
    log.write(json.dumps({'t': time.time(), 'session': session, 'action': action, 'sender': sender}) + '\n')


def on_focus(_conn, sender, path, iface, signal, params):
    app, kind = params.unpack()
    focus_log.write(json.dumps({'t': time.time(), 'app': app, 'kind': kind, 'sender': sender}) + '\n')


conn.signal_subscribe(DEST, IFACE, 'Action', PATH, None, Gio.DBusSignalFlags.NONE, on_action)
conn.signal_subscribe(DEST, IFACE, 'Focus', PATH, None, Gio.DBusSignalFlags.NONE, on_focus)


def on_call(_conn, sender, path, iface, method, params, invocation):
    name, args = params.unpack()
    if name == 'UniqueName':
        invocation.return_value(GLib.Variant('(s)', (json.dumps({'ok': True, 'value': [conn.get_unique_name()]}),)))
        return
    in_sig, out_sig = SIGNATURES[name]

    def done(c, result):
        try:
            value = c.call_finish(result)
            reply = {'ok': True, 'value': list(value.unpack()) if value is not None else []}
        except GLib.Error as exc:
            reply = {'ok': False, 'error': Gio.DBusError.get_remote_error(exc), 'message': exc.message}
        invocation.return_value(GLib.Variant('(s)', (json.dumps(reply),)))
    conn.call(DEST, PATH, IFACE, name, GLib.Variant(in_sig, tuple(json.loads(args))) if in_sig else None,
              GLib.VariantType(out_sig) if out_sig else None, Gio.DBusCallFlags.NO_AUTO_START, 10000, None, done)


conn.register_object('/pl/gramadrama/TestEngine', Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], on_call, None, None)
Gio.bus_own_name_on_connection(conn, 'pl.gramadrama.TestEngine.' + NAME, Gio.BusNameOwnerFlags.NONE, None, None)
if BY_IBUS:
    gi.require_version('IBus', '1.0')
    from gi.repository import IBus
    IBus.init()
    ibus = IBus.Bus()
    factory = IBus.Factory.new(ibus.get_connection())
    factory.add_engine('gramadrama', IBus.Engine.__gtype__)
    ibus.request_name('org.freedesktop.IBus.GramaDrama', 0)
GLib.MainLoop().run()
'''


def write_standin(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(FAKE_ENGINE)


class FakeEngine:
    """A stand-in engine in the session. By default the test starts it (python3 SCRIPT TAIL...; its
    parent is then the harness's ctl.py); spawn=False adopts the one ibus-daemon started."""

    def __init__(self, s, name, tail=('--ibus',), script=None, spawn=True, log=None):
        self.s, self.name = s, name
        folder = os.path.join(s.work, 'fake')
        self.log = log or os.path.join(folder, 'actions-%s.jsonl' % name)
        self.focus_log = os.path.splitext(self.log)[0] + '-focus.jsonl'
        self.bus_name = 'pl.gramadrama.TestEngine.' + name
        self.proc = None
        if spawn:
            os.makedirs(folder, exist_ok=True)
            path = script if script and os.path.isabs(script) else os.path.join(folder, script or 'gramadrama-engine')
            write_standin(path)
            self.proc = s.spawn(['/usr/bin/python3', path] + list(tail),
                                env={'GD_FAKE_NAME': name, 'GD_FAKE_LOG': self.log}, x11=False, name='fake-' + name,
                                log=os.path.join(s.logs, 'fake-%s.log' % name))
        wait_until(lambda: self._try_ping(), 15, what='stand-in engine %s on the bus' % name)

    def _try_ping(self):
        try:
            return self.call('Ping', []) is not None
        except HarnessError:
            return False

    def call(self, method, args):
        r = self.s.call(self.bus_name, '/pl/gramadrama/TestEngine', 'pl.gramadrama.TestEngine', 'Call', '(ss)',
                        [method, json.dumps(args)])
        return json.loads(r[0])

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding='utf-8') as f:
                return [json.loads(line) for line in f if line.endswith('\n')]
        except OSError:
            return []

    def actions(self):
        return self._read(self.log)

    def focus(self):
        """Focus signals received: [{'app', 'kind', 'sender', 't'}]."""
        return self._read(self.focus_log)

    def unique_name(self):
        return self.call('UniqueName', [])['value'][0]

    def kill(self):
        if self.proc:
            self.proc.kill()


# ---- checks -------------------------------------------------------------------------------------------
class Fail(AssertionError):
    pass


def check(cond, msg, *detail):
    if not cond:
        raise Fail(msg + (': ' + ' | '.join(repr(d)[:700] for d in detail) if detail else ''))
    return cond


def client_rect(rig):
    """The focused window's client rect on the stage: where a browser's page counts from (underlines.js origin)."""
    return rig.s.eval('(w => { const c = w.frame_rect_to_client_rect(w.get_frame_rect()); return [c.x, c.y, c.width, '
                      'c.height]; })(global.display.focus_window)')


def plural(n):
    if n == 1:
        return 'poprawka'
    return 'poprawki' if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 'poprawek'


# shell.log: a GLib/GJS message line starts an entry; its stack trace lines belong to it.
LOG_ENTRY = re.compile(r'^\((?:gnome-shell|process):\d+\): |^JS (?:ERROR|WARNING)|^Gjs-')
SEVERE = re.compile(r'CRITICAL|WARNING|JS ERROR')
EXPECTED_LOG = ('GramaDrama: test mode', 'injected enable failure')


def log_entries(text):
    entries, cur = [], []
    for line in text.splitlines():
        if LOG_ENTRY.match(line) or not line.strip():
            if cur:
                entries.append('\n'.join(cur))
            cur = [line] if line.strip() else []
        elif cur:
            cur.append(line)
    if cur:
        entries.append('\n'.join(cur))
    return entries


def extension_problems(text, expected=EXPECTED_LOG):
    """Criticals, warnings and JS errors that concern the extension: the message or its stack trace
    names it (gramadrama@gramadrama.local/extension.js, panel.js, policy.js, or its actors)."""
    return [e for e in log_entries(text) if SEVERE.search(e.split('\n', 1)[0]) and 'gramadrama' in e.lower()
            and not any(x in e for x in expected)]


def note(msg):
    print('    ' + msg, flush=True)


def clamp(value, low, high):
    return max(low, min(value, high))


class Rig:
    """One isolated session plus the handles the tests share."""

    def __init__(self, s, args):
        self.s, self.args = s, args
        self.sid = 100
        self.win = None
        self.listener = None
        self.workarea = None

    def next_id(self):
        self.sid += 1
        return self.sid

    def call(self, method, sig=None, args=None):
        return self.s.call(*PANEL, method, sig, args)

    def ping(self):
        return self.call('Ping')[0]

    def show(self, v):
        return self.call('Show', '(s)', [v if isinstance(v, str) else json.dumps(v)])[0]

    def hide(self, sid=0):
        self.call('Hide', '(u)', [sid])

    def state(self):
        return self.s.eval('gdGramaDramaPanel.state()')

    def settle(self, what='panel', timeout=4):
        """State once the actor is allocated and its geometry is stable (after a frame or two)."""
        last = [None]

        def stable():
            st = self.state()
            rect = st[what]['rect']
            ok = rect is not None and rect == last[0]
            last[0] = rect
            return st if ok else None
        time.sleep(0.05)
        return wait_until(stable, timeout, 0.06, what='%s geometry to settle' % what)

    def scheme(self, variant):
        value = 'prefer-dark' if variant == 'dark' else 'default'
        r = self.s.sh("gsettings set org.gnome.desktop.interface color-scheme '%s'" % value)
        check(r['rc'] == 0, 'gsettings color-scheme', r)
        wait_until(lambda: self.state()['variant'] == variant, 5, what='panel variant %s' % variant)

    def clipboard(self):
        return self.s.eval('new Promise(r => imports.gi.St.Clipboard.get_default().get_text('
                           'imports.gi.St.ClipboardType.CLIPBOARD, (_c, t) => r(t)))')

    def set_clipboard(self, text):
        self.s.eval('imports.gi.St.Clipboard.get_default().set_text(imports.gi.St.ClipboardType.CLIPBOARD, %s); true'
                    % json.dumps(text))

    def caret_to(self, offset):
        """Put the TextView caret at offset and wait until the extension knows that caret. GTK4 sends
        a cursor rectangle over text-input-v3 only after real input, not after a programmatic move,
        so the caret goes next to offset first and one arrow key brings it there."""
        if offset > 0:
            self.win.select('tv', offset - 1, offset - 1)
            self.s.tap('right')
        else:
            self.win.select('tv', 1, 1)
            self.s.tap('left')
        self.win.wait_event(lambda e: e['ev'] == 'sel' and e['w'] == 'tv' and e['sel'] == [offset, offset],
                            timeout=5)
        x, y = self.win.locate(offset)
        seen = [None]

        def moved():
            st = self.state()
            c = seen[0] = st['caret']
            return c if c and abs(c['x'] - x) <= 3 and c['y'] <= y <= c['y'] + c['height'] else None
        try:
            return wait_until(moved, 5)
        except TimeoutError:
            st = self.state()
            raise Fail('caret at offset %d (%d,%d) not seen; extension caret %r, focus %r, IBus focus %r' % (
                offset, x, y, seen[0], st['focusWindow'], st['ibusFocus']))

    def pointer_away(self):
        self.s.pointer_to(1275, 795)

    def shell_errors(self):
        """JS errors and GLib criticals/warnings in the shell log that concern the extension."""
        return extension_problems(self.s.tail('shell.log', 1000000))

    def criticals(self):
        """Every critical in the shell log (mutter's too), except the harness's known noise."""
        return [e for e in log_entries(self.s.tail('shell.log', 1000000))
                if 'CRITICAL' in e.split('\n', 1)[0] and 'login1' not in e]


def check_placement(rig, st, side=None):
    """Below the caret + 6 px (above when there is no room), text column under the caret, clamped."""
    c, p, wa = st['caret'], st['placed'], rig.workarea
    check(c, 'the extension knows the caret', st.get('caret'))
    check(p and p['actor'] == 'panel', 'placed', p)
    width, height = p['width'], p['height']
    ex = clamp(round(c['x']) - PAD, wa['x'] + EDGE, wa['x'] + wa['width'] - EDGE - width)
    check(abs(p['x'] - ex) <= 1, 'panel x = caret x - 16 (clamped)', p, c)
    if side:
        check(p['side'] == side, 'panel side', p['side'], side)
    if p['side'] == 'below':
        check(abs(p['y'] - round(c['y'] + c['height'] + GAP)) <= 1, 'panel 6 px below the caret', p, c)
    else:
        check(abs(p['y'] + height - round(c['y'] - GAP)) <= 1, 'panel 6 px above the caret', p, c)
    check(wa['y'] + EDGE <= p['y'] and p['y'] + height <= wa['y'] + wa['height'] - EDGE + 1, 'inside the work area',
          p, wa)
    rect = st['panel']['rect']
    check(rect and rect[0] == p['x'] and rect[1] == p['y'] and rect[2] == PANEL_WIDTH, 'actor geometry', rect, p)


def check_focus(rig, st, mark, title=WIN_TITLE, ibus_out=None):
    """The app keeps keyboard focus: no focus event in the window, the shell has no key focus."""
    events = [e for e in rig.win.events(mark) if e['ev'] == 'focus']
    check(not events, 'the app saw a focus change', events)
    info = rig.win.info()
    check(info['active'] and info['focus'] == 'tv', 'the TextView keeps focus', info)
    check((st['focusWindow'] or {}).get('title') == title, 'focus window unchanged', st['focusWindow'])
    check(st['stageKeyFocus'] is None, 'no stage key focus', st['stageKeyFocus'])
    check(not st['grab'], 'no Clutter grab', st['grab'])
    check(not st['panel']['hasKeyFocus'], 'the panel has no key focus')
    if ibus_out is not None:
        check(st['ibusFocus']['out'] == ibus_out, 'no IBus focus-out (the input context stayed focused)',
              st['ibusFocus'], ibus_out)


# expectations per status: badge (kind, text), logo, dim text, buttons
def expect_state(name, v, st):
    p = st['panel']
    check(p['visible'] and p['mapped'], '%s: panel on screen' % name, p['visible'], p['mapped'])
    check(p['session'] == v['session'] and p['status'] == v['status'], '%s: view shown' % name, p['session'],
          p['status'])
    b, btn = p['badge'], p['buttons']
    status, translate = v['status'], v.get('mode') == 5
    done = status in ('ready', 'unchanged', 'applying')
    check(p['logo'] == ('filled' if done else 'outline'), '%s: logo' % name, p['logo'])
    expected_badge = {
        'checking': ('dim', 'Czytam scenariusz…', 'edit-find-symbolic', False),
        'working': ('dim', 'Tłumaczę…' if translate else 'Poprawiam…', None, True),
        'apply_pending': ('dim', 'Zastosuję po zakończeniu', None, True),
        'applying': ('dim', 'Zastępuję…', None, True),
        'unchanged': ('ok', 'Zero dramatu', None, False),
        'error': ('error', '', 'dialog-warning-symbolic', False),
    }.get(status)
    if status == 'ready':
        expected_badge = ('dim', '', None, False) if translate else \
            ('count', '%d %s' % (v['count'], plural(v['count'])), None, False)
    kind, text, icon, spin = expected_badge
    check(b['kind'] == kind and b['icon'] == icon and b['spinner'] == spin, '%s: badge' % name, b)
    if text is not None:
        check(b['text'] == text, '%s: badge text' % name, b['text'], text)
    # direction button only in translate mode
    direction = btn['direction']
    if translate:
        label = {'en': 'PL → EN ⇄', 'pl': 'EN → PL ⇄'}[v['target']]
        check(direction['visible'] and direction['label'] == label, '%s: direction button' % name, direction)
    else:
        check(not direction['visible'], '%s: no direction button' % name, direction)
    # modes: the active one is checked (neutral)
    checked = [i for i in range(6) if btn['mode:%d' % i]['checked']]
    check(checked == [v.get('mode', 0)], '%s: active mode' % name, checked)
    check(all(btn['mode:%d' % i]['label'] == MODE_NAMES[i] for i in range(6)), '%s: mode labels' % name)
    # content
    busy = status in ('checking', 'working', 'apply_pending')
    if status == 'error':
        check(p['text'] == v['error'] and p['dim'], '%s: error text, dimmed' % name, p['text'], p['dim'])
    elif translate and status != 'checking':
        check(p['original'] and p['separator'], '%s: original + separator' % name, p['original'])
        check(p['text'] == v['text'] and p['dim'] == busy, '%s: translation text' % name, p['text'], p['dim'])
        check(p['originalLines'] <= 3, '%s: original at most 3 lines' % name, p['originalLines'])
        check(p['original'] == v['source'] or (p['original'].endswith('…') and len(p['original']) < len(v['source'])),
              '%s: original clamped with an ellipsis' % name, p['original'])
    elif done and not translate and v['segments']:
        result = ''.join(t for k, t in v['segments'] if k != 'removed')
        check(not p['dim'], '%s: result not dimmed' % name)
        check(p['text'] == result and 'strikethrough' not in p['markup'], '%s: the result alone, nothing struck'
              % name, p['text'][:200])
        if any(k == 'added' for k, _ in v['segments']):
            check('<span background="' in p['markup'], '%s: what changed on a sage tint' % name, p['markup'][:300])
    else:
        check(p['text'] == (v['text'] or v['source']) and p['dim'] == busy, '%s: streamed text' % name, p['text'],
              p['dim'])
    rows = p['changes']
    if done and not translate:
        check(len(rows) == min(30, len(v['changes'])), '%s: change rows' % name, len(rows))
    else:
        check(rows == [], '%s: no change list' % name, rows)
    # the list is shown on demand (Ctrl+I, Pokaż / Ukryj zmiany in the footer)
    opened = bool(rows) and v.get('changes_open', False)
    check(p['changesShown'] == opened, '%s: change list shown only on demand' % name, p['changesShown'])
    toggle = btn['changes']
    check(toggle['visible'] == bool(rows), '%s: Pokaż / Ukryj zmiany only with a list' % name, toggle)
    if rows:
        check(toggle['label'].startswith('Ukryj zmiany' if opened else 'Pokaż %d ' % v['count'])
              and toggle['reactive'] == (status == 'ready'), '%s: the list switch' % name, toggle)
    # an editable result is a field with what it is above it
    field = v.get('can_apply', True) and v.get('editable', False)
    check(p['field'] == field and p['fieldLabel'] == ('Wynik — możesz go poprawić, zanim wstawisz' if field else None)
          and not p['fieldActive'], '%s: the result as a field' % name, p['field'], p['fieldLabel'])
    check('edit' not in btn, '%s: no Edytuj button (the result is the field)' % name, sorted(btn))
    # footer
    scope = {'paragraph': 'akapit', 'selection': 'zaznaczenie'}[v['scope']]
    check(p['note'] == '%s · %s' % (v['note'], scope), '%s: footer note' % name, p['note'])
    apply_, copy = btn['accept'], btn['copy']
    if v.get('can_apply', True):
        check(apply_['visible'] and apply_['reactive'] == (status == 'ready'), '%s: Zastąp sensitive only when ready'
              % name, apply_)
        check('gd-primary' in apply_['styles'] and 'gd-primary' not in copy['styles'], '%s: Zastąp is the primary (lavender) action' % name)
    else:
        check(not apply_['visible'] and 'gd-primary' in copy['styles'], '%s: read-only: Kopiuj is primary' % name,
              apply_, copy)
    check(copy['reactive'] == (status == 'ready'), '%s: Kopiuj sensitive only when ready' % name, copy)
    check(btn['cancel']['reactive'] and btn['cancel']['visible'], '%s: × (Anuluj) in the header' % name)
    check(all(not b2['rect'] or b2['rect'][2] > 0 for b2 in btn.values()), '%s: button geometry' % name)


# ---- panel session tests ----------------------------------------------------------------------------------
def t_ping(rig):
    check(rig.ping() == VERSION, 'Ping returns the extension version', VERSION)
    st = rig.state()
    check(st['version'] == VERSION and st['ibusHooked'], 'extension hooked to the IBus panel service', st['ibusHooked'])
    check(not st['panel']['visible'] and not st['toast']['visible'], 'nothing on screen at start')


def t_states(rig):
    """Every status renders, in both variants; the panel sits at the caret; the app keeps focus."""
    for variant in ('dark', 'light'):
        rig.scheme(variant)
        time.sleep(0.5)  # the test window re-themes too
        rig.caret_to(20)
        for name, v in state_views(rig.next_id()).items():
            v['session'] = rig.next_id()
            mark = rig.win.mark()
            check(rig.show(v) is True, '%s/%s: Show returns true' % (variant, name))
            st = rig.settle()
            check(st['panel']['variant'] == variant and 'gd-%s' % variant in st['panel']['styles'],
                  '%s: variant style' % name, st['panel']['styles'])
            expect_state('%s/%s' % (variant, name), v, st)
            check_placement(rig, st)
            check_focus(rig, st, mark)
    # long content scrolls: text at most 220 px, change list at most 130 px (plus the box padding)
    v = dict(long_view(rig.next_id()), changes_open=True)
    check(rig.show(v), 'long: Show')
    st = rig.settle()
    p = st['panel']
    check(p['textHeight'] > 220 and p['textScrollHeight'] == 220, 'long text scrolls at 220 px', p['textHeight'],
          p['textScrollHeight'])
    check(p['changesScrollHeight'] == 130 and len(p['changes']) == 30, 'change list scrolls at 130 px, 30 rows',
          p['changesScrollHeight'], len(p['changes']))
    check(p['badge']['text'] == '36 poprawek', 'badge counts all changes', p['badge'])
    rig.hide(0)


def t_plurals(rig):
    expected = {1: 'poprawka', 2: 'poprawki', 3: 'poprawki', 4: 'poprawki', 5: 'poprawek', 11: 'poprawek',
                12: 'poprawek', 13: 'poprawek', 14: 'poprawek', 21: 'poprawek', 22: 'poprawki', 24: 'poprawki',
                25: 'poprawek', 102: 'poprawki', 112: 'poprawek', 122: 'poprawki'}
    got = {}
    for n in expected:
        rig.show(view(rig.next_id(), count=n))
        got[n] = rig.state()['panel']['badge']['text']
    check(all(got[n] == '%d %s' % (n, w) for n, w in expected.items()), 'Polish plurals', got)
    rig.hide(0)


def t_escape(rig):
    """Engine text is never markup: tags, entities and quotes show literally everywhere."""
    sid = rig.next_id()
    v = view(sid, source=TRICKY, text=TRICKY + '!', segments=[['same', TRICKY], ['added', '!'], ['removed', '<i>']],
             changes=[{'from': '<i>', 'to': '&amp;', 'reason': '<b>powód</b>'}], count=1, note='<b>notatka</b> &')
    check(rig.show(v), 'Show tricky text')
    p = rig.settle()['panel']
    check(p['text'] == TRICKY + '!' and '<i>' not in p['text'], 'result text literal, the removed text left out',
          p['text'])
    row = p['changes'][0] if p['changes'] else ''
    check('<i>' in row and '&amp;' in row and '<b>powód</b>' in row, 'change row literal', p['changes'])
    check(p['note'].startswith('<b>notatka</b> &'), 'note literal', p['note'])
    v = view(rig.next_id(), 'error', error=TRICKY)
    rig.show(v)
    check(rig.state()['panel']['text'] == TRICKY, 'error text literal', rig.state()['panel']['text'])
    check(rig.show(message(rig.next_id(), TRICKY)), 'toast with tricky text')
    check(rig.state()['toast']['text'] == TRICKY, 'toast text literal', rig.state()['toast'])
    rig.hide(0)
    check(not rig.shell_errors(), 'no Pango/markup errors in the shell log', rig.shell_errors()[-3:])


def t_updates(rig):
    """The engine calls Show for every streamed update of a session: updated in place, no jumps."""
    rig.caret_to(20)
    sid = rig.next_id()
    places = set()
    for n in range(8, len(FIXED) + 1, 9):
        v = view(sid, 'working' if n < len(FIXED) else 'ready', text=FIXED[:n],
                 segments=[] if n < len(FIXED) else SEGMENTS, changes=[] if n < len(FIXED) else CHANGES)
        check(rig.show(v) is True, 'update %d: Show returns true' % n)
        st = rig.state()
        check(st['panel']['visible'] and st['panel']['text'].startswith(FIXED[:n] if n < len(FIXED) else 'Wczoraj'),
              'update %d rendered' % n, st['panel']['text'])
        places.add((st['placed']['x'], st['placed']['y'], st['placed']['side']))
        time.sleep(0.1)
    check(len(places) == 1, 'the panel did not move while the result streamed in', places)
    rig.hide(0)


def t_caret_follow(rig):
    """Re-placed when the caret moves while shown; a new session is placed at the new caret."""
    rig.caret_to(20)
    check(rig.show(view(rig.next_id())), 'Show')
    st0 = rig.settle()
    check_placement(rig, st0, 'below')
    second = rig.win.text('tv').index('\n') + 5
    rig.caret_to(second)
    st1 = rig.settle()
    check(st1['placed']['y'] > st0['placed']['y'], 'the panel followed the caret down', st0['placed'], st1['placed'])
    check_placement(rig, st1, 'below')
    rig.hide(0)


def t_above(rig):
    """No room below the caret: the panel goes above it (bottom edge 6 px over the caret)."""
    s = rig.s
    text = rig.win.text('tv')
    try:
        s.place_window(WIN_TITLE, 40, 330, 900, 460)
        rig.win.set_text('tv', '\n'.join('Linia %d: tekst do poprawienia.' % i for i in range(1, 16)))
        time.sleep(0.3)
        rig.caret_to(rig.win.text('tv').rindex('\n') + 3)
        check(rig.show(view(rig.next_id())), 'Show')
        st = rig.settle()
        check_placement(rig, st, 'above')
        rig.hide(0)
    finally:
        rig.win.set_text('tv', text)
        s.place_window(WIN_TITLE, 40, 60, 900, 560)
        time.sleep(0.3)


def t_window_fallback(rig):
    """A caret known only for where the window was (it moved, no input since): the panel sits inside
    the focused window at the bottom centre of its frame."""
    s = rig.s
    rig.caret_to(20)
    try:
        frame = s.place_window(WIN_TITLE, 200, 330, 900, 460)
        time.sleep(0.3)
        check(rig.show(view(rig.next_id())), 'Show without a usable caret')
        st = rig.settle()
        p, wa = st['placed'], rig.workarea
        check(st['caret'] is None and p['side'] == 'window', 'the stale caret is not used', st['caret'], p)
        width, height = p['width'], p['height']
        ex = round(clamp(frame[0] + (frame[2] - width) / 2, wa['x'] + EDGE, wa['x'] + wa['width'] - EDGE - width))
        ey = round(clamp(frame[1] + frame[3] - EDGE - height, wa['y'] + EDGE, wa['y'] + wa['height'] - EDGE - height))
        check((p['x'], p['y']) == (ex, ey), 'bottom centre of the window frame', p, frame)
        rig.hide(0)
    finally:
        s.place_window(WIN_TITLE, 40, 60, 900, 560)
        time.sleep(0.3)


def t_problem(rig):
    """A failed correction (branding/PANEL.md, Errors): the title and the sentence on the error tint; its buttons
    instead of Kopiuj and Zastąp, the first one (Enter's) last, lavender and with its key; each emits problem:<id>. An
    engine before the table (no problem in the view) still gets the dim message."""
    rig.caret_to(20)
    for variant in ('dark', 'light'):
        rig.scheme(variant)
        sid = rig.next_id()
        check(rig.show(problem_view(sid)), '%s: Show a failed correction' % variant)
        p = rig.settle()['panel']
        got = p['problem']
        check(got and got['title'] == 'Serwer nie odpowiada' and got['text'] == UNREACHABLE,
              '%s: the title and the sentence' % variant, got)
        check([(a['label'], a['primary']) for a in got['actions']] == [('Otwórz Usługi', False),
                                                                       ('Spróbuj ponownie Enter', True)],
              '%s: the other button first, Enter\'s last with its key' % variant, got['actions'])
        check(not p['buttons']['copy']['visible'] and not p['buttons']['accept']['visible'],
              '%s: no Kopiuj, no Zastąp' % variant, p['buttons']['copy'], p['buttons']['accept'])
        check(p['note'] == 'akapit · tekst bez zmian', '%s: the footer says the text is as it was' % variant,
              p['note'])
        check(p['badge']['kind'] == 'error' and p['textScrollHeight'] == 0, '%s: the error badge, no result field'
              % variant, p['badge'], p['textScrollHeight'])
    for name in ('problem:open:connection', 'problem:retry'):
        click_button(rig, name, sid)
    sid = rig.next_id()
    check(rig.show(problem_view(sid, 'Nie ma jeszcze modelu językowego', 'Ollama nie jest zainstalowana.',
                                (('open:connection', 'Otwórz Usługi'), ('rm', 'x')), group=1)), 'Show one button')
    got = rig.settle()['panel']['problem']
    check([a['label'] for a in got['actions']] == ['Otwórz Usługi Enter'], 'one button, an unknown one dropped',
          got['actions'])
    sid = rig.next_id()
    check(rig.show(problem_view(sid, 'Model uciął odpowiedź', 'Zaznacz krótszy fragment.', ())), 'Show no button')
    p = rig.settle()['panel']
    check(p['problem']['actions'] == [] and not p['buttons']['accept']['visible'], 'no button at all', p['problem'])
    sid = rig.next_id()
    check(rig.show(state_views(sid)['working'] | {'session': sid, 'retrying': True}), 'Show the quiet second try')
    badge = rig.settle()['panel']['badge']
    check(badge['text'] == 'Ponawiam…' and badge['spinner'], 'the second try says so', badge)
    sid = rig.next_id()
    check(rig.show(state_views(sid)['error'] | {'session': sid}), 'Show an older engine\'s error')
    p = rig.settle()['panel']
    check(p['problem'] is None and p['dim'] and p['buttons']['copy']['visible'] and 'zszedł ze sceny' in p['text'],
          'an older engine\'s error: the dim message, as before', p['problem'], p['text'])
    rig.hide(0)


def click_button(rig, name, sid, expect=True, title=WIN_TITLE):
    st = rig.settle()
    b = st['panel']['buttons'][name]
    check(b['visible'] and b['rect'], '%s: button on screen' % name, b)
    x, y, w, h = b['rect']
    mark = rig.win.mark()
    out = st['ibusFocus']['out']
    n = len(rig.listener.actions())
    ext_n = len(st['actions'])
    rig.s.click(x + w // 2, y + h // 2)
    if expect:
        got = wait_until(lambda: [a for a in rig.listener.actions()[n:] if a['action'] == name], 3,
                         what='Action %s on the bus' % name)
        check(got[0]['session'] == sid, '%s: Action carries the session id' % name, got, sid)
        time.sleep(0.15)
        check(len(rig.listener.actions()[n:]) == 1, '%s: exactly one Action' % name, rig.listener.actions()[n:])
    else:
        time.sleep(0.8)
        check(rig.listener.actions()[n:] == [], '%s: an insensitive button emits nothing' % name,
              rig.listener.actions()[n:])
    st = rig.state()
    if expect:
        check(st['actions'][-1] == {'session': sid, 'action': name}, '%s: emitted by the extension' % name,
              st['actions'][ext_n - 20:])
    check(st['panel']['visible'], '%s: the panel stays until the engine hides it' % name)
    check_focus(rig, st, mark, title, out)
    return st


def t_buttons(rig):
    """Every button emits Action(session, name) on the bus; the app never loses keyboard focus."""
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'Show ready')
    for name in ('mode:1', 'mode:5', 'mode:0', 'mode:2', 'mode:3', 'mode:4', 'cancel', 'accept'):
        click_button(rig, name, sid)
    sid = rig.next_id()
    check(rig.show(state_views(sid)['translate'] | {'session': sid}), 'Show translate')
    click_button(rig, 'direction', sid)
    sid = rig.next_id()
    check(rig.show(state_views(sid)['working'] | {'session': sid}), 'Show working')
    st = click_button(rig, 'accept', sid, expect=False)
    check(not st['panel']['buttons']['accept']['reactive'], 'Zastąp insensitive while working')
    click_button(rig, 'copy', sid, expect=False)
    # the app still takes the keyboard: typing lands in the TextView
    rig.win.set_text('tv', 'abc')
    rig.win.select('tv', 3, 3)
    rig.s.type_text('d')
    rig.win.wait_text('tv', 'abcd')
    rig.hide(0)
    rig.win.set_text('tv', SRC + '\nDrugi akapit tekstu do sprawdzenia.\nTrzeci akapit.')


def t_copy(rig):
    """Kopiuj copies view.text to the clipboard itself, confirms on the button, emits 'copy'."""
    rig.caret_to(20)
    rig.set_clipboard('przed testem')
    sid = rig.next_id()
    v = view(sid)
    check(rig.show(v), 'Show ready')
    click_button(rig, 'copy', sid)
    got = wait_until(lambda: (lambda t: t if t == FIXED else None)(rig.clipboard()), 3, what='clipboard')
    check(got == FIXED, 'clipboard holds the result')
    check(rig.state()['panel']['buttons']['copy']['label'] == 'Skopiowano', 'Kopiuj confirms')
    wait_until(lambda: rig.state()['panel']['buttons']['copy']['label'] == 'Kopiuj', 3, what='Kopiuj label back')
    # a translation copies the translation
    sid = rig.next_id()
    check(rig.show(state_views(sid)['translate'] | {'session': sid}), 'Show translate')
    click_button(rig, 'copy', sid)
    wait_until(lambda: rig.clipboard() == TR_TEXT, 3, what='clipboard = translation')
    rig.hide(0)


def t_hide(rig):
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'Show')
    rig.hide(sid + 50)
    check(rig.state()['panel']['visible'], 'Hide(other session) leaves it')
    rig.hide(sid)
    check(not rig.state()['panel']['visible'], 'Hide(session) hides it')
    check(rig.show(view(sid)), 'Show again')
    rig.hide(0)
    check(not rig.state()['panel']['visible'], 'Hide(0) hides it')
    check(rig.show(message(rig.next_id(), 'Pole prywatne — pomijam.')), 'toast')
    rig.hide(0)
    st = rig.state()
    check(not st['toast']['visible'] and not st['toastTimer'], 'Hide(0) hides a toast too', st['toast'])
    time.sleep(0.3)
    check(not [a for a in rig.listener.actions() if a['action'] == 'hidden'],
          "the engine's own Hide never sends Action 'hidden'", rig.listener.actions()[-5:])


def t_stale(rig):
    """A lower session id than the one on screen is ignored; with nothing on screen it is shown."""
    rig.caret_to(20)
    sid = rig.next_id() + 10
    rig.sid = sid
    check(rig.show(view(sid)), 'Show session N')
    check(rig.show(view(sid - 1, 'working', text='STARY')) is False, 'Show(N-1) returns false')
    st = rig.state()
    check(st['panel']['session'] == sid and 'STARY' not in st['panel']['text'], 'session N still shown', st['panel'])
    check(rig.show(message(sid - 2, 'stary komunikat')) is False, 'an older toast is ignored too')
    check(rig.show(view(sid, 'unchanged', text=SRC, source=SRC, segments=[['same', SRC]], changes=[], count=0)),
          'same session updates')
    rig.hide(0)
    check(rig.show(view(sid - 5)), 'with nothing on screen a lower id is shown (another engine instance)')
    rig.hide(0)
    rig.sid = sid + 1


def t_toast(rig):
    """kind message: a compact toast, no buttons, click-through, gone after `seconds` (default 3)."""
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'panel first')
    t0 = time.monotonic()
    check(rig.show(message(sid, 'Zastosowano', 1.2)), 'a toast of the same session replaces the panel')
    st = rig.settle('toast')
    check(st['toast']['visible'] and not st['panel']['visible'], 'toast instead of the panel', st['toast'])
    check(st['toast']['text'] == 'Zastosowano' and not st['toast']['reactive'], 'toast text, click-through',
          st['toast'])
    r = st['toast']['rect']
    c = st['caret']
    check(abs(r[1] - round(c['y'] + c['height'] + GAP)) <= 1, 'toast below the caret', r, c)
    wait_until(lambda: not rig.state()['toast']['visible'], 3, what='toast to hide itself')
    took = time.monotonic() - t0
    check(1.0 <= took <= 2.2, 'seconds honoured (1.2 s)', took)
    sid = rig.next_id()
    t0 = time.monotonic()
    check(rig.show(message(sid, 'Pole prywatne — pomijam.')), 'toast without seconds')
    time.sleep(2.4)
    check(rig.state()['toast']['visible'], 'still visible after 2.4 s (default 3 s)')
    wait_until(lambda: not rig.state()['toast']['visible'], 2.5, what='default toast to hide')
    took = time.monotonic() - t0
    check(2.8 <= took <= 4.0, 'default 3 s', took)
    check(rig.show(message(rig.next_id(), '')) is False, 'an empty message is not shown')
    # a new session panel replaces a toast at once
    check(rig.show(message(rig.next_id(), 'Tekst zmienił się. Nic nie zmieniono.', 4)), 'toast')
    check(rig.show(view(rig.next_id())), 'panel after toast')
    st = rig.state()
    check(st['panel']['visible'] and not st['toast']['visible'] and not st['toastTimer'], 'panel replaced the toast')
    rig.hide(0)
    check(not [a for a in rig.listener.actions() if a['action'] == 'hidden'],
          "a toast replacing the panel sends no Action 'hidden'", rig.listener.actions()[-5:])


def t_hud(rig):
    """Hud (version 5, docs/dictation.md): the dictation pill at the bottom centre of the work area,
    click-through; bars and time while listening, a spinner while recognising; a message puts its copy
    on the clipboard and hides after `seconds`."""
    def hud(**view):
        return rig.call('Hud', '(s)', [json.dumps(dict(view, v=1))])[0]
    rig.set_clipboard('przed testem')
    check(hud(state='listening', level=0.8, seconds=65.4), 'Hud listening')
    st = rig.state()['hud']
    check((st['visible'], st['state'], st['text'], st['time'], st['reactive']) == (True, 'listening', 'Słucham…', '1:05', False),
          'the listening pill', st)
    check(max(st['bars']) == round(4 + 14 * 0.8) and min(st['bars']) == round(4 + 14 * 0.8 * 0.45), 'bars', st['bars'])
    check(st['width'] == 230, 'the macOS width', st['width'])
    check(st['language'] is None, 'no label without a language', st)
    # extension version 7: the language of the dictation (Shift with the key dictates in the other one)
    for given, shown in (('PL', 'PL'), ('EN', 'EN'), ('auto', 'auto'), (7, None), ('English language', None), ('', None)):
        check(hud(state='listening', level=0.5, seconds=1, language=given), 'Hud language %r' % (given,))
        check(rig.state()['hud']['language'] == shown, 'the language label for %r' % (given,), rig.state()['hud'])
    check(hud(state='listening', level=0.5, seconds=1, language='EN') and rig.state()['hud']['language'] == 'EN', 'EN')
    check(hud(state='recognizing', language='EN') and rig.state()['hud']['language'] is None, 'no label while recognising')
    # extension version 14: the microphone opened but no audio yet (a suspended USB microphone wakes in up to 2 s)
    check(hud(state='listening', level=0.8, seconds=0, language='PL', waiting=True), 'Hud waiting')
    st = rig.state()['hud']
    check((st['state'], st['waiting'], st['text'], st['time'], st['language']) == ('listening', True, 'Budzę mikrofon…', None, 'PL'),
          'the waiting pill', st)
    check(st['spinning'], 'a spinner instead of the bars while waiting', st)
    time.sleep(0.3)  # laid out on the next frame
    check(not rig.state()['hud']['cut'], 'the waiting text fits the pill', rig.state()['hud'])

    def shot(name):
        if rig.args.shots:
            os.makedirs(rig.args.shots, exist_ok=True)
            top = rig.state()['hud']['y']
            shot_area(rig, os.path.join(rig.args.shots, name + '.png'), (1280 - 230) // 2 - 20, top - 20, 270, 80)
    shot('hud-waiting')
    check(hud(state='listening', level=0.8, seconds=65.4), 'back to the plain listening pill')
    st = rig.state()['hud']
    check(not st['waiting'] and not st['spinning'] and st['text'] == 'Słucham…', 'audio came', st)
    time.sleep(0.3)
    shot('hud-listening')
    bottom = rig.s.eval('global.stage.height')  # the harness has no bottom panel: the work area ends there
    check(abs(st['y'] + 40 + 48 - bottom) <= 2, '48 px above the bottom of the work area', st['y'], bottom)
    check(hud(state='recognizing'), 'Hud recognizing')
    st = rig.state()['hud']
    check((st['state'], st['text'], st['time'], st['spinning']) == ('recognizing', 'Rozpoznaję…', None, True), 'recognizing', st)
    check(hud(state='cleaning') and rig.state()['hud']['text'] == 'Porządkuję…', 'cleaning')
    t0 = time.monotonic()
    check(hud(state='message', message='Tekst jest w schowku.', copy='Podyktowany tekst.', seconds=1), 'Hud message')
    check(rig.state()['hud']['text'] == 'Tekst jest w schowku.' and not rig.state()['hud']['spinning'], 'the message')
    check(wait_until(lambda: rig.clipboard() == 'Podyktowany tekst.', 3, what='clipboard'), 'copy on the clipboard')
    wait_until(lambda: not rig.state()['hud']['visible'], 3, what='the message to hide itself')
    check(0.8 <= time.monotonic() - t0 <= 2.2, 'seconds honoured', time.monotonic() - t0)
    # Every message of the engine's dictation (engine/dictate.py), in Polish and in English, is shown whole: a long one
    # widens the pill up to the macOS toast's 420 px and wraps, growing upwards from the same bottom.
    sys.path.insert(0, LINUX)
    from gramadrama.engine.dictate import MESSAGES
    with open(os.path.join(LINUX, 'gramadrama', 'locales', 'en', 'engine.json'), encoding='utf-8') as f:
        english = json.load(f)
    for text in list(MESSAGES.values()) + [english[text] for text in MESSAGES.values()]:
        check(hud(state='message', message=text, seconds=5), 'Hud message %r' % text)
        time.sleep(0.3)  # laid out on the next frame
        st = rig.state()['hud']
        check(st['text'] == text and not st['cut'], 'the whole message: %s' % text, st)
        check(HUD_WIDTH <= st['width'] <= MESSAGE_WIDTH and (st['lines'] == 1 or st['width'] == MESSAGE_WIDTH),
              'as wide as its line, up to %d px: %s' % (MESSAGE_WIDTH, text), st)
        check(abs(st['y'] + max(st['height'], 40) + 48 - bottom) <= 2, 'the same bottom: %s' % text, st['y'], st['height'])
    check(hud(state='message', message='Nic nie rozpoznałem.') and rig.state()['hud']['width'] == HUD_WIDTH,
          'a short message keeps the pill', rig.state()['hud'])
    clipboard, before = MESSAGES['uncertain'], rig.state()['variant']
    for variant in ('dark', 'light'):
        rig.scheme(variant)
        check(hud(state='message', message=clipboard, seconds=5), 'the longest message, %s' % variant)
        time.sleep(0.3)
        check(rig.state()['hud']['lines'] == 2, 'on two lines', rig.state()['hud'])
        if rig.args.shots:
            top = rig.state()['hud']['y']
            shot_area(rig, os.path.join(rig.args.shots, 'hud-message-%s.png' % variant), (1280 - MESSAGE_WIDTH) // 2 - 20,
                      top - 20, MESSAGE_WIDTH + 40, rig.state()['hud']['height'] + 40)
    rig.scheme(before)
    check(hud(state='listening', level=0.5, seconds=1) and rig.state()['hud']['width'] == HUD_WIDTH,
          'the listening pill is 230 px again', rig.state()['hud'])
    check(hud(state='listening') and not hud(state='hidden') and not rig.state()['hud']['visible'], 'hidden')
    for bad in ('nie json', '[]', '{"v": 1, "state": "dancing"}', '{"v": 1, "state": "message"}', '{"state": "listening"}',
                '{"v": 1, "state": "message", "message": "x", "copy": 5}'):
        try:
            rig.call('Hud', '(s)', [bad])
            raise Fail('Hud(%r) must fail' % bad)
        except HarnessError as exc:
            check(INVALID in str(exc), 'Hud(%r): InvalidArgs' % bad, str(exc)[-300:])
    check(not rig.state()['hud']['visible'], 'nothing shown by an invalid view')


def t_live(rig):
    """Live (version 5, docs/live-check.md): the badge with the count at the focused window's right edge,
    level with the caret, or inside the bottom right corner of a browser page's field (version 8); count 0
    hides it; bad views are refused."""
    rig.caret_to(20)
    view = {'v': 1, 'count': 3, 'summary': '3 błędy', 'more': 0, 'issues': [
        {'id': '0:5:X', 'word': 'Mysle', 'suggestions': ['Myślę', 'Mydle'], 'message': 'Nieznane słowo', 'kind': 'spelling'}]}
    check(rig.call('Live', '(s)', [json.dumps(view)])[0], 'Live shows the badge')
    st = rig.state()
    badge, window, caret = st['live'], st['focusWindow'], st['caret']
    check(badge['visible'] and badge['count'] == '3' and badge['reactive'] and not badge['focusable'], 'the badge', badge)
    check(caret and abs(badge['y'] + badge['height'] / 2 - (caret['y'] + caret['height'] / 2)) <= 2,
          'level with the caret', badge, caret)
    check(not st['panel']['visible'], 'no panel with it', st['panel'])
    # The list never changes under the pointer: a newer view waits until the pointer leaves the list.
    rig.s.pointer_to(badge['x'] + badge['width'] // 2, badge['y'] + badge['height'] // 2)
    st = wait_until(lambda: (lambda st: st['live']['list'] and st['live']['buttons'] and st)(rig.state()), 3,
                    what='the list on hover')
    first = st['live']['buttons'][0]
    rig.s.pointer_to(first['x'] + first['width'] // 2, first['y'] + first['height'] // 2)
    time.sleep(0.3)
    newer = dict(view, count=1, summary='1 błąd', issues=[dict(view['issues'][0], suggestions=['Inne'])])
    check(rig.call('Live', '(s)', [json.dumps(newer)])[0], 'a newer view while the pointer is on the list')
    live = rig.state()['live']
    check([b['label'] for b in live['buttons']] == ['Myślę', 'Mydle'] and live['count'] == '1',
          'the list under the pointer stays, the count follows', live)
    rig.s.pointer_to(badge['x'] + badge['width'] // 2, badge['y'] + badge['height'] // 2)  # off the list
    wait_until(lambda: [b['label'] for b in rig.state()['live']['buttons']] == ['Inne'], 3,
               what='the newer list once the pointer left it')
    # The list's header names the badge's click: "Popraw…" opens the panel (Ctrl+Alt+G), it fixes nothing by itself.
    live = rig.state()['live']
    open_ = live['open']
    check(open_ and open_['label'] == 'Popraw…' and not open_['ellipsized'], 'the list header has Popraw…', live)
    actions = len(rig.listener.actions())
    rig.s.pointer_to(open_['x'] + open_['width'] // 2, open_['y'] + open_['height'] // 2)
    rig.s.click(open_['x'] + open_['width'] // 2, open_['y'] + open_['height'] // 2)
    got = wait_until(lambda: rig.listener.actions()[actions:], 3, what='Action from Popraw…')
    check(got[-1]['action'] == 'live:open', 'Popraw… sends what the badge click sends', got)
    rig.s.pointer_to(5, 5)
    # A browser's page tells where its field is, in the window's client coordinates (version 8): the badge goes inside
    # the field's bottom right corner, as on macOS, 6 px from its edges, 3 px in a low field.
    client = client_rect(rig)
    for field, inset in (([100, 120, 500, 200], 6), ([100, 120, 300, 30], 3)):
        check(rig.call('Live', '(s)', [json.dumps(dict(newer, field=field, **{'from': 'client'}))])[0], 'Live with a field')
        badge = rig.state()['live']
        right, bottom = client[0] + field[0] + field[2] - inset, client[1] + field[1] + field[3] - inset
        check(abs(badge['x'] + badge['width'] - right) <= 1 and abs(badge['y'] + badge['height'] - bottom) <= 1,
              "inside the field's bottom right corner", badge, field, client)
    check(rig.call('Live', '(s)', [json.dumps(dict(newer, field=[100, 120, 500, 200]))])[0], 'a field without from')
    badge, caret = rig.state()['live'], rig.state()['caret']
    check(abs(badge['y'] + badge['height'] / 2 - (caret['y'] + caret['height'] / 2)) <= 2,
          'a field in unknown coordinates means nothing: level with the caret', badge, caret)
    check(not rig.call('Live', '(s)', [json.dumps({'v': 1, 'count': 0})])[0] and not rig.state()['live']['visible'],
          'count 0 hides it')
    for bad in ('nie json', '[]', '{"v": 1}', '{"v": 1, "count": -1}', '{"v": 2, "count": 1}'):
        try:
            rig.call('Live', '(s)', [bad])
            raise Fail('Live(%r) must fail' % bad)
        except HarnessError as exc:
            check(INVALID in str(exc), 'Live(%r): InvalidArgs' % bad, str(exc)[-300:])


def t_live_checking(rig):
    """A running check is visible without errors yet; previous results remain usable and no focus is taken."""
    rig.caret_to(20)
    rig.pointer_away()
    before = rig.win.mark()

    def show(**fields):
        return rig.call('Live', '(s)', [json.dumps({'v': 1, 'count': 0, **fields})])[0]

    def hover():
        badge = wait_until(lambda: (lambda b: b['visible'] and b['width'] > 0 and b)(rig.state()['live']), 3,
                           what='checking badge laid out')
        rig.s.pointer_to(badge['x'] + badge['width'] // 2, badge['y'] + badge['height'] // 2)
        return wait_until(lambda: (lambda b: b['list'] and b)(rig.state()['live']), 3, what='checking tooltip')

    def shot(name):
        if rig.args.shots:
            os.makedirs(rig.args.shots, exist_ok=True)
            rig.s.shot(os.path.join(rig.args.shots, name + '.png'), cursor=False)

    for value in (False, None, 'true', 1):
        check(not show(checking=value), 'only boolean true starts a checking indicator', value)
        check(not rig.state()['live']['spinner'], 'an invalid or false checking flag cannot spin')
    for language, title in (('pl', 'Sprawdzanie…'), ('en', 'Checking…')):
        check(show(checking=True, ui_language=language), 'checking without a count shows the badge')
        badge = hover()
        check(badge['spinner'] and badge['count'] == '' and not badge['focusable'],
              'spinner alone, no misleading zero or keyboard focus', badge)
        check(badge['accessibleName'] == title and badge['summary'] == title and not badge['buttons'],
              'translated accessible name and nonempty status tooltip', badge)
        if language == 'pl':
            shot('live-checking-only')
        rig.pointer_away()

    issues = [{'id': 'live-checking', 'word': 'Mysle', 'suggestions': ['Myślę'],
               'message': 'Nieznane słowo', 'kind': 'spelling'}]
    check(show(count=3, checking=True, summary='3 błędy', issues=issues), 'checking preserves the previous count')
    badge = hover()
    check(badge['spinner'] and badge['count'] == '3' and badge['accessibleName'] == 'Sprawdzanie… · 3 błędy',
          'spinner beside the previous count', badge)
    check([b['label'] for b in badge['buttons']] == ['Myślę'], 'previous suggestions stay available', badge)
    shot('live-checking-count')
    rig.pointer_away()
    check(show(count=3, checking=False, summary='3 błędy', issues=issues), 'results after the check')
    check(not rig.state()['live']['spinner'] and rig.state()['live']['count'] == '3',
          'finished checking stops the spinner and keeps the results')

    # A checking-only update follows a browser field just as a result badge does.
    client = client_rect(rig)
    for field in ([100, 120, 300, 90], [140, 150, 310, 100]):
        check(show(checking=True, field=field, **{'from': 'client'}), 'checking badge with a field')
        badge = rig.state()['live']
        check(abs(badge['x'] + badge['width'] - (client[0] + field[0] + field[2] - 6)) <= 1
              and abs(badge['y'] + badge['height'] - (client[1] + field[1] + field[3] - 6)) <= 1,
              'checking follows the field position', badge)
    badge = hover()
    actions = len(rig.listener.actions())
    rig.s.click(badge['x'] + badge['width'] // 2, badge['y'] + badge['height'] // 2)
    wait_until(lambda: rig.listener.actions()[actions:], 3, what='checking badge click')
    check(rig.listener.actions()[actions:][-1]['action'] == 'live:open', 'checking badge opens manual correction')
    check(not rig.state()['live']['visible'] and not rig.state()['live']['spinner'], 'click stops and hides spinner')
    check(not [e for e in rig.win.events(before) if e['ev'] == 'focus'], 'checking never takes keyboard focus')

    rig.pointer_away()
    check(show(checking=True), 'new checking indicator')
    check(not show() and not rig.state()['live']['spinner'], 'legacy empty view hides and stops it')


def t_live_lava(rig):
    """The badge's lava indicator (lava.js, branding/PANEL.md): with GNOME's animations on, a check with no count yet
    shows drifting lavender blobs, its result merges them into the brick circle with the count, a check that finds
    nothing merges them and fades away, and a known count stays (breathing) while a new check runs. With animations
    off (the session's default) everything is shown at once."""
    rig.caret_to(20)
    rig.pointer_away()
    issues = [{'id': 'lava', 'word': 'Mysle', 'suggestions': ['Myślę'], 'message': '', 'kind': 'spelling'}]

    def show(**fields):
        return rig.call('Live', '(s)', [json.dumps({'v': 1, 'count': 0, **fields})])[0]

    def live():
        return rig.state()['live']

    def animations(on):
        rig.s.sh('gsettings set org.gnome.desktop.interface enable-animations %s' % ('true' if on else 'false'))
        wait_until(lambda: rig.s.eval('imports.gi.St.Settings.get().enable_animations') == on, 3,
                   what='GNOME animations %s' % ('on' if on else 'off'))

    def crop(name):
        if rig.args.shots:
            os.makedirs(rig.args.shots, exist_ok=True)
            b = live()
            shot_area(rig, os.path.join(rig.args.shots, name + '.png'), b['x'] - 8, b['y'] - 8, b['width'] + 16,
                      b['height'] + 16)

    animations(True)
    try:
        check(show(checking=True), 'a check with no count yet shows the badge')
        b = live()
        check(b['visible'] and b['indicator'] == 'thinking' and b['spinner'] and b['count'] == '',
              'the blobs drift, no count', b)
        crop('live-lava-thinking-1')
        time.sleep(0.35)
        crop('live-lava-thinking-2')
        check(show(count=4, summary='4 błędy', issues=issues), 'the result')
        b = live()
        check(b['indicator'] == 'merging' and not b['spinner'], 'the blobs merge into the result', b)
        time.sleep(0.25)
        crop('live-lava-merging')
        check(show(count=4, summary='4 błędy', issues=issues), 'the same result again during the merge')
        b = wait_until(lambda: (lambda b: b['indicator'] == 'count' and b)(live()), 3, what='the circle with the count')
        check(b['count'] == '4' and b['visible'], 'the circle shows the count', b)
        time.sleep(0.3)
        crop('live-lava-count')
        check(show(count=4, checking=True, summary='4 błędy', issues=issues), 'a new check with the count known')
        b = live()
        check(b['indicator'] == 'count' and b['spinner'] and b['count'] == '4', 'the count stays while it breathes', b)
        check(show(count=12, summary='12 błędów', issues=issues), 'a new result')
        b = live()
        check(b['indicator'] == 'count' and b['count'] == '12' and not b['spinner'], 'the new count, no merge', b)
        time.sleep(0.4)
        crop('live-lava-count-12')
        check(show(count=150, summary='150 błędów', issues=issues), 'a large count')
        check(live()['count'] == '99+', 'above 99 the circle says 99+', live())
        check(show(checking=True), 'a check from nothing again')
        check(live()['indicator'] == 'thinking', 'the blobs again', live())
        check(not show(), 'nothing found: nothing to show')
        b = live()
        check(b['visible'] and b['indicator'] == 'merging' and not b['spinner'], 'the blobs merge before they go', b)
        wait_until(lambda: not live()['visible'], 3, what='the merged blob fades away')
    finally:
        animations(False)
    check(show(checking=True), 'animations off: a check')
    check(live()['indicator'] == 'thinking', 'still blobs', live())
    crop('live-lava-still')
    check(show(count=2, summary='2 błędy', issues=issues), 'animations off: the result')
    b = live()
    check(b['indicator'] == 'count' and b['count'] == '2', 'animations off: the count at once', b)
    check(show(checking=True), 'animations off: a check from the count')
    check(not show(), 'animations off: nothing found')
    check(not live()['visible'], 'animations off: hidden at once', live())

def t_marks(rig):
    """Marks (version 6, docs/live-check.md): the live check's wavy lines inside the focused window's actor,
    under the words (GTK4 counted from the frame, a browser's page from the client rect, anything else placed
    by the caret); hovering a word opens the card with its suggestions, Ignoruj, Dodaj do słownika and (version 15)
    Wyłącz regułę, whose clicks are Actions; no marks, no lines."""
    s, win = rig.s, rig.win
    text = 'Mysle ze to dziala dobrze.'
    win.set_text('tv', text)
    try:
        caret = rig.caret_to(len(text))
        frame = s.focused_window()['frame']

        def word(start, end):  # its box on the screen, from the TextView itself
            height = win.call('Locate', '(si)', ['tv', start])['h']
            (x0, middle), (x1, _) = win.locate(start), win.locate(end)
            return [x0, middle - height / 2, x1 - x0, height]

        words = {'Mysle': word(0, 5), 'dziala': word(12, 18)}

        def view(toolkit, dx, dy, caret_box=None):
            at = lambda r: [r[0] - frame[0] - dx, r[1] - frame[1] - dy, r[2], r[3]]
            return {'v': 1, 'toolkit': toolkit, 'caret': caret_box, 'clip': [-dx, -dy, frame[2], frame[3]], 'marks': [
                {'id': '0:5:SPELLING_PL', 'word': 'Mysle', 'message': 'Nieznane słowo', 'suggestions': ['Myślę', 'Mydle'],
                 'kind': 'spelling', 'source': 'słownik', 'learn': True, 'rects': [at(words['Mysle'])]},
                {'id': '12:18:X', 'word': 'dziala', 'message': 'Coś tu nie gra.', 'suggestions': [], 'kind': 'grammar',
                 'source': 'LanguageTool', 'learn': False, 'rule_off': True, 'rects': [at(words['dziala'])]}]}

        def lines_at_words(what, tolerance):
            u = rig.state()['underlines']
            got = {line['id']: [line['x'], line['y'], line['width'], line['height']] for line in u['lines']}
            for name, ident in (('Mysle', '0:5:SPELLING_PL'), ('dziala', '12:18:X')):
                want = [round(v) for v in words[name]]
                check(ident in got and all(abs(a - b) <= tolerance for a, b in zip(got[ident], want)),
                      '%s: %s underlined at its word' % (what, name), got.get(ident), want)
            return u

        check(rig.call('Marks', '(s)', [json.dumps(view(['GTK', '4.20.1'], 0, 0))])[0], 'Marks draws the lines')
        u = lines_at_words('GTK4', 1)
        check(u['visible'] and u['inWindow'] and u['window'] == rig.state()['focusWindow']['id'],
              'inside the focused window, never above everything', u)
        r = words['Mysle']
        s.pointer_to(r[0] + r[2] / 2, r[1] + r[3] / 2)
        card = wait_until(lambda: rig.state()['underlines']['card'], 3, what='the card on hover')
        check([b['label'] for b in card['buttons']] == ['Myślę', 'Mydle', 'Ignoruj', 'Dodaj do słownika'], 'its buttons', card)
        check(not card['focusable'] and card['y'] >= r[1] + r[3], 'under the word, never focusable', card, r)
        n = len(rig.listener.actions())
        first = card['buttons'][0]
        s.click(first['x'] + first['width'] // 2, first['y'] + first['height'] // 2)
        got = wait_until(lambda: rig.listener.actions()[n:], 3, what='the Action of a suggestion')
        check([a['action'] for a in got] == ['live:fix:0:5:SPELLING_PL:0'], 'a suggestion: live:fix', got)
        check(not rig.state()['underlines']['card'], 'a click closes the card')
        r = words['dziala']
        s.pointer_to(r[0] + r[2] / 2, r[1] + r[3] / 2)
        card = wait_until(lambda: rig.state()['underlines']['card'], 3, what='the grammar card')
        check([b['label'] for b in card['buttons']] == ['Ignoruj', 'Wyłącz regułę'],
              'no suggestion, no learning: Ignoruj, and Wyłącz regułę for a LanguageTool rule', card)
        n = len(rig.listener.actions())
        off = card['buttons'][1]
        s.click(off['x'] + off['width'] // 2, off['y'] + off['height'] // 2)
        got = wait_until(lambda: rig.listener.actions()[n:], 3, what='the Action of Wyłącz regułę')
        check([a['action'] for a in got] == ['live:rule_off:12:18:X'], 'Wyłącz regułę: live:rule_off', got)
        rig.pointer_away()
        s.pointer_to(r[0] + r[2] / 2, r[1] + r[3] / 2)
        card = wait_until(lambda: rig.state()['underlines']['card'], 3, what='the grammar card again')
        n = len(rig.listener.actions())
        ignore = card['buttons'][0]
        s.click(ignore['x'] + ignore['width'] // 2, ignore['y'] + ignore['height'] // 2)
        got = wait_until(lambda: rig.listener.actions()[n:], 3, what='the Action of Ignoruj')
        check([a['action'] for a in got] == ['live:ignore:12:18:X'], 'Ignoruj: live:ignore', got)
        rig.pointer_away()
        # A browser's page counts from the window's client rect (from: 'client', version 8): with the shadow of a window
        # that is not maximized, not from the frame. The toolkit stays the one a Shell before 8 reads.
        client = client_rect(rig)
        check(client[:2] != frame[:2], 'the test window has a shadow around its frame', client, frame)
        page = dict(view(['GTK', '4.0'], client[0] - frame[0], client[1] - frame[1]), **{'from': 'client'})
        check(rig.call('Marks', '(s)', [json.dumps(page)])[0], 'Marks from the client rect')
        lines_at_words('from the client rect', 1)
        # Chromium counts from the page: its boxes are shifted, and the caret places them.
        dx, dy = 37, 55
        box = [caret['x'] - frame[0] - dx, caret['y'] - frame[1] - dy, 0, caret['height']]
        check(rig.call('Marks', '(s)', [json.dumps(view(['Chromium', '154'], dx, dy, box))])[0], 'placed by the caret')
        lines_at_words('by the caret', 2)
        check(not rig.call('Marks', '(s)', [json.dumps({'v': 1, 'marks': []})])[0]
              and not rig.state()['underlines']['visible'], 'no marks: no lines')
        for bad in ('nie json', '[]', '{"v": 1}', '{"v": 2, "marks": []}'):
            try:
                rig.call('Marks', '(s)', [bad])
                raise Fail('Marks(%r) must fail' % bad)
            except HarnessError as exc:
                check(INVALID in str(exc), 'Marks(%r): InvalidArgs' % bad, str(exc)[-300:])
    finally:
        rig.pointer_away()
        rig.call('Marks', '(s)', [json.dumps({'v': 1, 'marks': []})])
        win.set_text('tv', SRC + '\nDrugi akapit tekstu do sprawdzenia.\nTrzeci akapit.')


def t_invalid(rig):
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'Show')
    for bad in ('nie json', '[]', '{"session": -1}', '{"session": 1.5}', '{"kind": "session"}',
                '{"session": 4294967296}'):
        try:
            rig.show(bad)
            raise Fail('Show(%r) must fail' % bad)
        except HarnessError as exc:
            check(INVALID in str(exc), 'Show(%r): InvalidArgs' % bad, str(exc)[-300:])
    st = rig.state()
    check(st['panel']['visible'] and st['panel']['session'] == sid, 'the panel is untouched', st['panel']['session'])
    check(rig.show(json.dumps({'session': rig.next_id()})), 'a minimal view (defaults) shows')
    check(rig.state()['panel']['status'] == 'checking', 'default status checking', rig.state()['panel']['status'])
    rig.hide(0)


def t_vanish(rig):
    """The panel, the dictation pill and the live badge never outlive the engine that showed them."""
    rig.caret_to(20)
    fake = FakeEngine(rig.s, 'vanish')
    r = fake.call('Show', [json.dumps(view(rig.next_id()))])
    check(r == {'ok': True, 'value': [True]}, 'the stand-in engine shows the panel', r)
    r = fake.call('Hud', [json.dumps({'v': 1, 'state': 'recognizing'})])
    check(r == {'ok': True, 'value': [True]}, 'and the pill', r)
    r = fake.call('Live', [json.dumps({'v': 1, 'count': 2, 'checking': True, 'summary': '2 błędy', 'issues': []})])
    check(r == {'ok': True, 'value': [True]}, 'and the badge', r)
    r = fake.call('Marks', [json.dumps({'v': 1, 'toolkit': ['GTK', '4.20.1'], 'caret': None, 'clip': None, 'marks': [
        {'id': 'x', 'word': 'Wczoraj', 'kind': 'spelling', 'rects': [[10, 50, 60, 18]]}]})])
    check(r == {'ok': True, 'value': [True]}, 'and the underlines', r)
    st = rig.state()
    check(st['panel']['visible'] and st['hud']['visible'] and st['live']['visible'] and st['underlines']['visible'],
          'visible', st)
    fake.kill()
    wait_until(lambda: not any(rig.state()[k]['visible'] for k in ('panel', 'hud', 'live', 'underlines')), 3,
               what='the panel, the pill, the badge and the underlines hidden after the engine exited')
    check(not rig.state()['live']['spinner'], 'vanished engine stops the checking spinner')


def t_osk(rig):
    """While the on-screen keyboard is visible the panel hides and Show returns false."""
    s = rig.s
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'Show')
    n = len(rig.listener.actions())
    try:
        s.sh('gsettings set org.gnome.desktop.a11y.applications screen-keyboard-enabled true')
        wait_until(lambda: s.eval('!!gdMain.keyboard.keyboardActor'), 5, what='OSK created')
        s.eval('gdMain.keyboard.open(gdMain.layoutManager.primaryIndex); true')
        wait_until(lambda: s.eval('!!gdMain.keyboard.visible'), 5, what='OSK visible')
        check(not rig.state()['panel']['visible'], 'panel hidden while the OSK is up')
        wait_until(lambda: rig.listener.actions()[n:], 3, what="Action 'hidden'")
        time.sleep(0.3)
        got = rig.listener.actions()[n:]
        check([(a['session'], a['action']) for a in got] == [(sid, 'hidden')],
              "the engine is told once: Action(session, 'hidden')", got)
        check(rig.show(view(rig.next_id())) is False, 'Show returns false while the OSK is up')
        check(not rig.state()['panel']['visible'], 'and stays hidden')
    finally:
        s.eval('gdMain.keyboard.close(); true')
        try:
            wait_until(lambda: not s.eval('!!gdMain.keyboard.visible'), 5, what='OSK closed')
        finally:
            s.sh('gsettings set org.gnome.desktop.a11y.applications screen-keyboard-enabled false')
            wait_until(lambda: not s.eval('!!gdMain.keyboard.keyboardActor'), 5, what='OSK gone')
            time.sleep(0.5)
    rig.win.present()
    wait_until(lambda: rig.win.info()['active'], 5, what='test window active again')
    rig.caret_to(21)
    check(rig.show(view(rig.next_id())), 'Show works again without the OSK')
    rig.hide(0)


# What GNOME Shell's window tracker says about the focused window, read independently of the extension.
TRACKER_JS = """(w => w && (a => ({id: a ? a.get_id() : null, backed: a ? a.is_window_backed() : true,
  wmClass: w.get_wm_class(), x11: w.get_client_type() === 1, title: w.get_title()}))(
  imports.gi.Shell.WindowTracker.get_default().get_window_app(w)))(global.display.focus_window)"""


def window_id(wm_class):
    """policy.js windowId, written again: of a Chromium app window only the browser and the host."""
    name = (wm_class or '')[:100]
    cut = name.find('__')
    if cut > 0 and re.match(r'[A-Za-z][A-Za-z0-9-]*-', name[:cut]):
        name = name[:cut].rstrip('-')
    return 'window:' + name


def tracker_pair(s):
    """[app, kind] docs/apps.md defines for the focused window: the desktop ID, or window:<WM_CLASS>."""
    w = s.eval(TRACKER_JS)
    check(w, 'a focused window')
    return [w['id'] if w['id'] and not w['backed'] else window_id(w['wmClass']), 'x11' if w['x11'] else 'wayland']


def pairs(events):
    return [[e['app'], e['kind']] for e in events]


def in_order(want, got):
    """want is a subsequence of got."""
    rest = iter(got)
    return all(any(g == w for g in rest) for w in want)


def activate_js(title):
    return ('(w => (w.unminimize(), w.activate(global.get_current_time()), true))(global.get_window_actors()'
            '.map(a => a.meta_window).find(w => w.get_title() === %s))' % json.dumps(title))


def wait_focus(fake, n, pair, what):
    """The Focus signals fake received after the first n, once the last one is pair."""
    return wait_until(lambda: (lambda got: got if got and got[-1] == pair else None)(pairs(fake.focus()[n:])), 5,
                      what=what)


def t_focus(rig):
    """Subscribe/Focus (docs/apps.md, section 1): right after Subscribe and on every change, the
    subscriber gets the focused app, and nobody else does (unicast). Wayland and Xwayland windows,
    window-backed apps, shell entries and no focus window are told apart; a vanished subscriber is
    forgotten, a new one replaces the old, and disable() tells the subscriber that nothing follows."""
    s = rig.s
    sub = FakeEngine(s, 'focus')
    name = sub.unique_name()
    check(rig.state()['subscriber'] is None and rig.state()['following'], 'no subscriber yet, focus followed',
          rig.state()['subscriber'])
    check(sub.call('Subscribe', []) == {'ok': True, 'value': []}, 'Subscribe')
    gtk = tracker_pair(s)
    check(gtk == ['window:org.gramadrama.TestWin4', 'wayland'], 'the GTK4 test window: window-backed, WM_CLASS', gtk)
    got = wait_focus(sub, 0, gtk, 'Focus right after Subscribe')
    check(got == [gtk], 'exactly the current pair right after Subscribe', got)
    st = rig.state()
    check(st['subscriber'] == name and st['focus'] == {'app': gtk[0], 'kind': gtk[1]}, 'test state: subscriber and pair',
          st['subscriber'], name, st['focus'])
    chromes = []
    try:
        chromes.append(s.spawn_chrome(s.fixture_url(focus='ta'), name='focuswl'))
        wl = tracker_pair(s)
        raw = s.eval(TRACKER_JS)['wmClass']
        check(raw.startswith('chrome-__') and 'fixture' in raw, 'Chrome --app: its WM_CLASS names the file', raw)
        check(wl == ['window:chrome', 'wayland'], 'Chrome --app on Wayland: a window-backed app, without the path', wl)
        wait_focus(sub, 0, wl, 'Focus for Chrome on Wayland')
        chromes.append(s.spawn_chrome(s.fixture_url(focus='ta'), x11=True, name='focusx11'))
        x11 = tracker_pair(s)
        check(x11 == ['google-chrome.desktop', 'x11'], 'Chrome on Xwayland: its desktop ID', x11)
        wait_focus(sub, 0, x11, 'Focus for Chrome on Xwayland')
        s.eval('gdMain.openRunDialog(); true')
        wait_focus(sub, 0, ['org.gnome.Shell', 'shell'], 'Focus for the run dialog')
        s.tap('esc')
        wait_focus(sub, 0, x11, 'Focus back in Chrome after the run dialog')
        n = len(sub.focus())
        s.eval('global.display.focus_window.activate(global.get_current_time()); true')  # no change: nothing sent
        time.sleep(0.5)
        check(sub.focus()[n:] == [], 'Focus only on a change', sub.focus()[n:])
        got = pairs(sub.focus())
        check(all(a != b for a, b in zip(got, got[1:])), 'no pair twice in a row', got)
        check(got[0] == gtk and in_order([wl, x11, ['org.gnome.Shell', 'shell'], x11], got), 'every change, in order',
              got)
        # Unicast: the listener subscribes to the signal like any process could, and gets nothing.
        check(rig.listener.focus() == [], 'Focus reached a process that did not subscribe', rig.listener.focus()[:3])
        check({f['to'] for f in rig.state()['focusSent']} == {name}, 'every Focus went to the subscriber',
              rig.state()['focusSent'][-5:])
        # A second subscriber replaces the first.
        sub2 = FakeEngine(s, 'focus2')
        check(sub2.call('Subscribe', []) == {'ok': True, 'value': []}, 'Subscribe (second)')
        wait_focus(sub2, 0, x11, 'Focus right after the second Subscribe')
        n = len(sub.focus())
        s.eval('gdMain.openRunDialog(); true')
        wait_focus(sub2, 0, ['org.gnome.Shell', 'shell'], 'the new subscriber gets the changes')
        s.tap('esc')
        wait_focus(sub2, 0, x11, 'and the way back')
        check(sub.focus()[n:] == [], 'the replaced subscriber gets nothing more', sub.focus()[n:])
        sub2.kill()
        wait_until(lambda: rig.state()['subscriber'] is None, 3, what='a vanished subscriber forgotten')
    finally:
        for chrome in chromes:
            chrome.kill()
    # No focus window at all.
    s.eval(activate_js(WIN_TITLE))
    wait_until(lambda: rig.win.info()['active'], 5, what='test window active again')
    n = len(sub.focus())
    check(sub.call('Subscribe', []) == {'ok': True, 'value': []}, 'Subscribe again')
    wait_focus(sub, n, gtk, 'Focus after Subscribe again')
    s.eval('global.display.focus_window.minimize(); true')
    wait_focus(sub, n, ['', 'none'], 'Focus without a focus window')
    s.eval(activate_js(WIN_TITLE))
    wait_focus(sub, n, gtk, 'Focus when the window is back')
    wait_until(lambda: rig.win.info()['active'], 5, what='test window active again')
    # disable(): Focus('', '') to the subscriber, and a fresh start without one after enable().
    n = len(sub.focus())
    s.eval("gdMain.extensionManager.disableExtension('%s')" % UUID)
    wait_focus(sub, n, ['', ''], "Focus('', '') at disable()")
    s.eval("gdMain.extensionManager.enableExtension('%s')" % UUID)
    wait_until(lambda: rig._try_ping(), 5, what='Ping after enable')
    st = rig.state()
    check(st['subscriber'] is None and st['following'] and st['focus'] == {'app': gtk[0], 'kind': gtk[1]},
          'enable(): following the focus, no subscriber', st['subscriber'], st['focus'])
    # Review finding: a Subscribe whose caller check outlives the extension (disabled, and enabled
    # again, as GNOME Shell does when an extension before it is switched off) fails as if the object
    # were gone, never "subscribed" without a subscription; the engine then asks again with Ping.
    s.eval(SUBSCRIBE_ACROSS_ENABLE_JS % json.dumps(UUID))
    got = wait_until(lambda: s.eval('globalThis.gdSubscribed'), 3, what='the Subscribe answered')
    s.eval('delete globalThis.gdSubscribed; true')
    check(got == ['org.freedesktop.DBus.Error.UnknownObject'], 'Subscribe across disable() and enable()', got)
    wait_until(lambda: rig._try_ping(), 5, what='Ping after the direct enable()')
    check(rig.state()['subscriber'] is None, 'nobody subscribed', rig.state()['subscriber'])
    sub.kill()
    check(rig.listener.focus() == [], 'the listener never got Focus', rig.listener.focus()[:3])


# A Subscribe handled while disable() and enable() run, as GNOME Shell calls them directly when it
# re-orders extensions: the caller check (asynchronous, even in test mode) ends after both.
SUBSCRIBE_ACROSS_ENABLE_JS = """(uuid => { const ext = gdMain.extensionManager.lookup(uuid).stateObj;
  globalThis.gdSubscribed = [];
  ext.handleSubscribe(null, {get_sender: () => ':1.4242', return_value: () => gdSubscribed.push('ok'),
                             return_dbus_error: name => gdSubscribed.push(name)});
  ext.disable(); ext.enable(); return true; })(%s)"""


ACTORS = ['gramadrama-hud', 'gramadrama-live-badge', 'gramadrama-live-list', 'gramadrama-mark-card', 'gramadrama-panel',
          'gramadrama-toast']  # sorted names of the extension's actors
ACTORS_JS = """(() => { const out = []; const walk = a => { if ((a.name ?? '').startsWith('gramadrama'))
  out.push(a.name); a.get_children().forEach(walk); }; walk(global.stage); return out.sort(); })()"""


def t_cycle(rig):
    """disable() removes everything (actors, D-Bus object, timers, handlers); enable() restores it."""
    s = rig.s
    check(s.eval(ACTORS_JS) == ACTORS, 'one panel, one toast and one dictation pill',
          s.eval(ACTORS_JS))
    for round_ in range(2):
        rig.caret_to(20 + round_)
        # leave work in flight: a spinning panel, then a toast with a pending timer
        check(rig.show(view(rig.next_id(), 'working', text=FIXED[:30], segments=[], changes=[])), 'working panel')
        check(rig.show(message(rig.next_id(), 'Zastosowano', 1.0)), 'toast with a pending timer')
        check(rig.call('Live', '(s)', [json.dumps({'v': 1, 'count': 0, 'checking': True})])[0],
              'checking spinner before disabling the extension')
        s.eval("gdMain.extensionManager.disableExtension('%s')" % UUID)
        wait_until(lambda: s.eval(ACTORS_JS) == [], 5, what='actors destroyed')
        check(s.eval("typeof globalThis.gdGramaDramaPanel") == 'undefined', 'test hook removed')
        try:
            rig.ping()
            raise Fail('Ping must fail while disabled')
        except HarnessError as exc:
            check('UnknownMethod' in str(exc) or 'UnknownObject' in str(exc) or 'UnknownInterface' in str(exc),
                  'the D-Bus object is gone', str(exc)[-200:])
        # everything the extension listened to fires now: none of it may reach a dead extension
        s.sh("gsettings set org.gnome.desktop.interface color-scheme '%s'" % ('prefer-dark' if round_ else 'default'))
        rig.win.select('tv', 5 + round_, 5 + round_)
        time.sleep(1.5)  # past the toast timer
        s.eval("gdMain.extensionManager.enableExtension('%s')" % UUID)
        wait_until(lambda: rig._try_ping(), 5, what='Ping after enable')
        check(s.eval(ACTORS_JS) == ACTORS, 'fresh actors after enable',
              s.eval(ACTORS_JS))
        rig.caret_to(22 + round_)
        check(rig.show(view(rig.next_id())), 'Show after enable')
        st = rig.settle()
        check_placement(rig, st)
        rig.hide(0)


def t_testmode(rig):
    """Test mode is on only because this is the isolated harness (policy.js), and the log says so."""
    st = rig.state()
    check(st['testMode'], 'test mode reported', st.get('testMode'))
    check('GramaDrama: test mode' in rig.s.tail('shell.log', 1000000), 'test mode announced in the shell log')


def oversized_view(session, n=20000):
    """The review's case: 20000 diff segments, a long text and far too many, far too long changes."""
    words = ['w%05d ' % i for i in range(n)]
    text = ''.join(words)
    segments = [['removed' if i % 2 else 'added', w] for i, w in enumerate(words)]
    changes = [{'from': 'x' * 1000, 'to': 'y' * 1000, 'reason': 'z' * 1000} for _ in range(60)]
    return view(session, source=SRC, text=text, segments=segments, changes=changes, count=len(changes))


def t_oversized(rig):
    """An oversized view is bounded in parseView: under 50 ms in the Show handler, shown as plain text
    (no diff), 30 short change rows; same-kind neighbours merge; a view over 1 MB is refused."""
    rig.caret_to(20)
    v = oversized_view(rig.next_id())
    payload = json.dumps(v)
    check(len(payload) < 1000000, 'the oversized view stays under the 1 MB bound', len(payload))
    t0 = time.monotonic()
    check(rig.show(payload) is True, 'oversized view: Show returns true')
    round_trip = time.monotonic() - t0
    st = rig.settle()
    p = st['panel']
    note('oversized view (%d KB): Show handler %.1f ms, D-Bus round trip %.0f ms'
         % (len(payload) // 1024, st['lastShowUs'] / 1000, round_trip * 1000))
    check(st['lastShowUs'] < 50000, 'Show handler under 50 ms', st['lastShowUs'])
    check(p['plain'] and '<span' not in p['markup'] and not p['dim'], 'shown as plain text', p['markup'][:200])
    check(p['text'] == v['text'][:20000], 'text cut at 20000 characters', len(p['text']))
    check(len(p['changes']) == 30 and all(len(row) < 400 for row in p['changes']), '30 short change rows',
          len(p['changes']), max(map(len, p['changes'])))
    # 5000 segments of one kind merge into one: still a diff
    sid = rig.next_id()
    merged = view(sid, text='a' * 5000 + 'c', segments=[['same', 'a']] * 5000 + [['removed', 'b'], ['added', 'c']])
    check(rig.show(merged), 'mergeable view: Show')
    p = rig.state()['panel']
    check(not p['plain'] and p['markup'].endswith('">c</span>') and '<span background="' in p['markup'],
          'same-kind neighbours merged into a result with its change marked', p['markup'][-200:])
    try:
        rig.show(json.dumps(view(rig.next_id(), text='x' * 1100000)))
        raise Fail('a view over 1 MB must be refused')
    except HarnessError as exc:
        check(INVALID in str(exc), 'view over 1 MB: InvalidArgs', str(exc)[-200:])
    check(rig.state()['panel']['session'] == sid, 'the panel is untouched by the refused view')
    rig.hide(0)


def t_no_monitor(rig):
    """No monitor (index -1): the panel hides with Action 'hidden', Show returns false, and mutter
    is never asked for the work area of monitor -1 (no criticals)."""
    s = rig.s
    rig.caret_to(20)
    sid = rig.next_id()
    check(rig.show(view(sid)), 'Show')
    rig.settle()
    n = len(rig.listener.actions())
    criticals = len(rig.criticals())
    s.eval('globalThis.gdSavedPrimary = gdMain.layoutManager.primaryIndex; gdMain.layoutManager.primaryIndex = -1; '
           'global.display.get_monitor_index_for_rect = () => -1; true')
    try:
        rig.caret_to(22)  # the caret moves: the panel is placed again, and there is no monitor
        wait_until(lambda: not rig.state()['panel']['visible'], 3, what='panel hidden without a monitor')
        got = wait_until(lambda: rig.listener.actions()[n:], 3, what="Action 'hidden'")
        check([(a['session'], a['action']) for a in got] == [(sid, 'hidden')], "exactly Action(session, 'hidden')",
              got)
        check(rig.show(view(rig.next_id())) is False, 'Show without a monitor returns false')
        check(not rig.state()['panel']['visible'], 'and nothing is shown')
    finally:
        s.eval('delete global.display.get_monitor_index_for_rect; '
               'gdMain.layoutManager.primaryIndex = globalThis.gdSavedPrimary; delete globalThis.gdSavedPrimary; true')
    new = rig.criticals()[criticals:]
    check(not new, 'no criticals (e.g. a work area asked for monitor -1)', *new[:2])
    check(rig.show(view(rig.next_id())), 'Show works again with the monitor back')
    rig.hide(0)


def t_enable_failure(rig):
    """enable() that throws half-way undoes itself: no actors, no D-Bus object, no test hook; the next
    enable() works."""
    s = rig.s
    state_js = "gdMain.extensionManager.lookup('%s').state" % UUID
    s.eval("gdMain.extensionManager.disableExtension('%s')" % UUID)
    wait_until(lambda: s.eval(ACTORS_JS) == [], 5, what='extension disabled')
    s.eval("globalThis.gdSavedWrap = imports.gi.Gio.DBusExportedObject.wrapJSObject; "
           "imports.gi.Gio.DBusExportedObject.wrapJSObject = () => { throw new Error('injected enable failure'); }; "
           "true")
    try:
        s.eval("gdMain.extensionManager.enableExtension('%s')" % UUID)
        wait_until(lambda: s.eval(state_js) == 3, 5, what='extension in the ERROR state')  # ExtensionState.ERROR
        check(s.eval(ACTORS_JS) == [], 'no actors left by the failed enable()', s.eval(ACTORS_JS))
        check(s.eval('typeof globalThis.gdGramaDramaPanel') == 'undefined', 'no test hook')
        try:
            rig.ping()
            raise Fail('no D-Bus object may be left by the failed enable()')
        except HarnessError as exc:
            check('Unknown' in str(exc), 'the D-Bus object is gone', str(exc)[-200:])
    finally:
        s.eval("imports.gi.Gio.DBusExportedObject.wrapJSObject = globalThis.gdSavedWrap; "
               "delete globalThis.gdSavedWrap; true")
        # GNOME Shell never enables an extension in the ERROR state again: reload it
        s.eval("gdMain.extensionManager.reloadExtension(gdMain.extensionManager.lookup('%s')).then(() => true)" % UUID)
        wait_until(lambda: rig._try_ping(), 5, what='Ping after reloading the extension')
    check(s.eval(ACTORS_JS) == ACTORS, 'enabled cleanly again', s.eval(ACTORS_JS))
    check(s.eval(state_js) == 1, 'extension ACTIVE again')
    rig.caret_to(20)
    check(rig.show(view(rig.next_id())), 'Show after the recovery')
    rig.hide(0)


POLICY_TEST_JS = r"""
import {focusPair, isEngineProcess, isTestMode, launcherPath, parentPid, parseCmdline, windowId} from '%s';
const failures = [];
const ok = (cond, what) => { if (!cond) failures.push(what); };
const pair = (args, want, what) => ok(JSON.stringify(focusPair(args)) === JSON.stringify(want), what);
const id = (wmClass, want, what) => ok(windowId(wmClass) === want, `${what}: ${windowId(wmClass)}`);
const win = {shellEntry: false, window: true, x11: false};
// A Chromium app window (--app=URL, no .desktop file) keeps its browser and host, never the path.
id('chrome-docs.google.com__document_d_1AbCdEf_edit-Default', 'window:chrome-docs.google.com', 'a web app');
id('chrome-__home_u_Dokumenty_notatki.html-Default', 'window:chrome', 'a local file: no host');
id('chrome-docs.google.com__-Default', 'window:chrome-docs.google.com', 'the root of a site');
id('msedge-teams.microsoft.com__v2_-Profile_1', 'window:msedge-teams.microsoft.com', 'another browser');
id('Obsidian', 'window:Obsidian', 'an ordinary WM_CLASS');
id('steam_app_1234', 'window:steam_app_1234', 'single underscores');
id('__main__', 'window:__main__', 'no browser before the underscores');
id('my__app', 'window:my__app', 'no hyphen before the underscores');
id(null, 'window:', 'no WM_CLASS');
id('x'.repeat(300), `window:${'x'.repeat(100)}`, 'at most 100 characters');
id(`chrome-${'a'.repeat(120)}__secret_path-Default`, `window:chrome-${'a'.repeat(93)}`, 'a path beyond the cut');
pair({...win, appId: 'window:7', windowBacked: true, wmClass: 'chrome-mail.example.com__u_0_inbox-Default'},
    ['window:chrome-mail.example.com', 'wayland'], 'a window-backed app: windowId');
pair({...win, shellEntry: true, appId: 'firefox_firefox.desktop', windowBacked: false, wmClass: 'firefox'},
    ['org.gnome.Shell', 'shell'], 'a shell entry with the keyboard wins over the focus window');
pair({shellEntry: true, window: false}, ['org.gnome.Shell', 'shell'], 'a shell entry without a focus window');
pair({shellEntry: false, window: false}, ['', 'none'], 'no focus window');
pair({...win, appId: 'google-chrome.desktop', windowBacked: false, wmClass: 'google-chrome'},
    ['google-chrome.desktop', 'wayland'], 'a desktop ID on Wayland');
pair({...win, x11: true, appId: 'chatgpt.desktop', windowBacked: false, wmClass: 'ChatGPT'},
    ['chatgpt.desktop', 'x11'], 'a desktop ID on Xwayland');
pair({...win, appId: 'window:12', windowBacked: true, wmClass: 'Obsidian'}, ['window:Obsidian', 'wayland'],
    'window-backed: WM_CLASS, not the per-window ID');
pair({...win, x11: true, appId: null, windowBacked: true, wmClass: null}, ['window:', 'x11'], 'no app, no WM_CLASS');
const env = vars => name => vars[name] ?? null;
const W = '/tmp/gd-abc123';
const BUS = `unix:path=${W}/bus`;
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1'})), 'the debug switch alone');
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1', GD_WORK: W,
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=/run/user/1000/bus'})), 'switch + work dir, real bus');
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1', DBUS_SESSION_BUS_ADDRESS: BUS})), 'no GD_WORK');
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1', GD_WORK: '/home/u/gd-abc',
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=/home/u/gd-abc/bus'})), 'work dir outside /tmp/gd-');
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1', GD_WORK: '/tmp/gd-x/../y',
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=/tmp/gd-x/../y/bus'})), 'work dir with ..');
ok(!isTestMode(env({GD_WORK: W, DBUS_SESSION_BUS_ADDRESS: BUS})), 'harness without the switch');
ok(!isTestMode(env({GRAMADRAMA_ENGINE_TEST: 'yes', GD_WORK: W, DBUS_SESSION_BUS_ADDRESS: BUS})), 'switch must be 1');
ok(isTestMode(env({GRAMADRAMA_ENGINE_TEST: '1', GD_WORK: W, DBUS_SESSION_BUS_ADDRESS: BUS})), 'the harness');
const L = launcherPath('/home/u/.local/share/');
ok(L === '/home/u/.local/share/gramadrama-engine/gramadrama-engine', 'launcher path');
ok(isEngineProcess(['/usr/bin/python3', L, '--ibus'], 4242, L, 4242), 'the engine started by ibus-daemon');
ok(!isEngineProcess(['/usr/bin/python3', '/x/not-the-engine.py', '/nonexistent/gramadrama-engine', '--ibus'],
    4242, L, 4242), 'name suffix elsewhere (the review case)');
ok(!isEngineProcess(['/usr/bin/python3', L, '--ibus'], 4243, L, 4242), 'another parent');
ok(!isEngineProcess(['/usr/bin/python3', L, '--ibus'], 4242, L, null), 'ibus-daemon unknown');
ok(!isEngineProcess(['/usr/bin/python3', L, '--ibus'], 1, L, 1), 'pid 1 as ibus-daemon');
ok(!isEngineProcess(['/usr/bin/python3', L, '--ibus', '--verbose'], 4242, L, 4242), 'extra argument');
ok(!isEngineProcess(['/usr/bin/python3', L], 4242, L, 4242), 'no --ibus');
ok(!isEngineProcess(['/usr/bin/python3', `${L}2`, '--ibus'], 4242, L, 4242), 'another file');
ok(!isEngineProcess(['--ibus'], 4242, L, 4242), 'too short');
ok(parentPid('1234 (python3) S 42 1234 1234 0 -1 4194560') === 42, 'stat');
ok(parentPid('1234 (a) b) (c) S 77 1 1') === 77, 'stat with ) and spaces in the name');
ok(parentPid('garbage') === null, 'bad stat');
const enc = new TextEncoder();
ok(JSON.stringify(parseCmdline(enc.encode('/usr/bin/python3\0/x y/gramadrama-engine\0--ibus\0'))) ===
    JSON.stringify(['/usr/bin/python3', '/x y/gramadrama-engine', '--ibus']), 'cmdline');
print(JSON.stringify(failures));
"""


def t_policy(_rig):
    """policy.js with plain gjs (no session): the caller rule and the test-mode condition, including
    GRAMADRAMA_ENGINE_TEST=1 outside the harness (the production caller check stays on), and the
    [app, kind] of the Focus signal."""
    with tempfile.TemporaryDirectory(prefix='panel-policy-') as tmp:
        script = os.path.join(tmp, 'policy_test.js')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(POLICY_TEST_JS % ('file://' + os.path.join(EXTENSION, 'policy.js')))
        r = subprocess.run(['gjs', '-m', script], capture_output=True, text=True, timeout=30,
                           env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tmp})
    check(r.returncode == 0, 'gjs ran', r.returncode, r.stderr[-500:])
    failures = json.loads(r.stdout.strip().splitlines()[-1])
    check(not failures, 'policy.js', failures)


DESTROY_JS = """gdMain.layoutManager.uiGroup.get_children().filter(a => (a.name ?? '').startsWith('gramadrama'))
  .forEach(a => a.destroy()); true"""


def t_shutdown(rig):
    """GNOME Shell destroying the actors on its own, as at shutdown (no disable()): the engine's name
    vanishing, the Kopiuj and toast timers, caret moves and a colour-scheme switch touch nothing."""
    s = rig.s
    rig.caret_to(20)
    fake = FakeEngine(s, 'shutdown')
    sid = rig.next_id()
    check(fake.call('Show', [json.dumps(view(sid))]) == {'ok': True, 'value': [True]}, 'Show from a stand-in')
    click_button(rig, 'copy', sid)  # the "Skopiowano" timer runs now
    check(rig.state()['watching'], 'the extension watches the engine that showed the panel')
    s.eval(DESTROY_JS)
    wait_until(lambda: s.eval(ACTORS_JS) == [], 3, what='actors destroyed by the shell')
    fake.kill()  # the watched engine goes away
    rig.win.select('tv', 5, 5)
    s.tap('right')  # IBus reports a caret move
    s.sh("gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'")
    time.sleep(2.0)  # past the Kopiuj timer
    st = rig.state()
    check(st['alive'] is False and not st['watching'] and not st['toastTimer'], 'callbacks and timers stopped', st)
    try:
        rig.ping()  # the object is still exported: Ping is harmless
        r = rig.show(view(rig.next_id()))
        check(r is False, 'Show returns false without actors', r)
        late = FakeEngine(s, 'late')  # Subscribe fails as if the object were gone
        r = late.call('Subscribe', [])
        late.kill()
        check(not r['ok'] and r['error'] == 'org.freedesktop.DBus.Error.UnknownObject', 'Subscribe without actors', r)
        check(rig.state()['subscriber'] is None, 'nobody subscribed without actors', rig.state()['subscriber'])
    finally:
        s.eval("gdMain.extensionManager.disableExtension('%s')" % UUID)
        time.sleep(0.3)
        s.eval("gdMain.extensionManager.enableExtension('%s')" % UUID)
        wait_until(lambda: rig._try_ping(), 5, what='Ping after the next enable()')
    check(s.eval(ACTORS_JS) == ACTORS, 'enabled cleanly again', s.eval(ACTORS_JS))
    rig.scheme('light')
    rig.caret_to(21)
    check(rig.show(view(rig.next_id())), 'Show after the recovery')
    rig.hide(0)


def _try_ping(self):
    try:
        return self.ping() == VERSION
    except HarnessError:
        return False


Rig._try_ping = _try_ping


# ---- screenshots -----------------------------------------------------------------------------------------
# Backdrops for the screenshots: the text each state is about, in gnome-text-editor. The caret sits
# at the start of the paragraph (translations: the paragraph selected from the end backwards).
EDITORS = {'correct': ('scenariusz.txt', SRC + '\n\nTu kończy się akapit, który GramaDrama poprawia.\n', False),
           'pl': ('po-polsku.txt', TR_SRC + '\n', True),
           'en': ('in-english.txt', TR_TEXT + '\n', True),
           'correct_en': ('draft.txt', EN_SRC + '\n\nThis is where the paragraph GramaDrama corrects ends.\n', False)}
BACKDROP = {'translate': 'pl', 'translate_working': 'pl', 'translate_pl': 'en'}
# --docs: the English pictures of docs/panel (state -> backdrop) and, from the Polish states, the site's pictures
DOC_VIEWS = {'ready': 'correct_en', 'translate': 'pl', 'message': 'correct_en'}
DOC_SHOTS = {'ready-dark': 'panel-ready-dark.png', 'ready-light': 'panel-ready-light.png',
             'translate-dark': 'panel-translate-dark.png', 'message-dark': 'panel-message-dark.png'}
SITE = os.path.join(os.path.dirname(LINUX), 'site', 'public', 'img')
SITE_SHOTS = {'ready_list-dark': 'panel-ready-dark.png', 'ready_list-light': 'panel-ready-light.png',
              'translate-dark': 'panel-translate-dark.png'}


def docs_views(sid):
    """The views of DOC_VIEWS with an English interface."""
    return {'ready': view(sid, ui_language='en', source=EN_SRC, text=EN_FIXED, segments=EN_SEGMENTS, changes=EN_CHANGES,
                          count=len(EN_CHANGES), note='%s · 2.9 s' % EN_MODEL, editable=True),
            'translate': view(sid, ui_language='en', mode=5, target='en', source=TR_SRC, text=TR_TEXT, segments=[],
                              changes=[], count=0, scope='selection', note='%s · PL → EN · 3.4 s' % EN_MODEL),
            'message': message(sid, PRIVATE[1])}


def shot_area(rig, path, x, y, w, h):
    x, y = max(0, x), max(0, y)
    w, h = min(w, 1280 - x), min(h, 800 - y)
    r = rig.s.call('org.gnome.Shell.Screenshot', '/org/gnome/Shell/Screenshot', 'org.gnome.Shell.Screenshot',
                   'ScreenshotArea', '(iiiibs)', [x, y, w, h, False, path])
    check(r[0], 'screenshot %s' % path, r)


def focus_editor(rig, key):
    """Bring the backdrop editor up with a fresh caret at the start of its paragraph."""
    s = rig.s
    name, _text, select = EDITORS[key]
    s.activate_window(name)
    frame = s.window(name)['frame']
    if select:
        s.combo('ctrl', 'end')
        s.combo('ctrl', 'shift', 'home')
    else:
        s.combo('ctrl', 'end')  # a real move, so the editor reports its caret again
        s.combo('ctrl', 'home')
    wait_until(lambda: (lambda st: st['caret'] and (st['focusWindow'] or {}).get('title', '').startswith(name)
                        and st['caret']['x'] < frame[0] + 40 and st['caret']['y'] < frame[1] + 120)(rig.state()),
               5, what='caret at the start of %s' % name)


def shoot_view(rig, v, path, full=None):
    """The caret's text line and the panel (or toast) of view v with its shadow; the full screen too into full."""
    what = 'toast' if v['kind'] == 'message' else 'panel'
    check(rig.show(v), 'screenshot %s: Show' % os.path.basename(path))
    rig.pointer_away()
    st = rig.settle(what)
    time.sleep(0.35)
    r = st[what]['rect']
    c = st['caret']
    top = min(r[1] - 28, round(c['y']) - 14)
    bottom = max(r[1] + r[3] + 28, round(c['y'] + c['height']) + 14)
    shot_area(rig, path, r[0] - 28, top, r[2] + 56, bottom - top)
    if full:
        rig.s.shot(full, cursor=False)


def t_screenshots(rig):
    """Every state, dark and light, over a real editor (gnome-text-editor follows the scheme). With --docs also
    docs/panel/*.png (English, DOC_VIEWS) and the site's pictures (Polish, SITE_SHOTS)."""
    s, out = rig.s, rig.args.shots
    if not out:
        note('no --shots DIR: skipped')
        return
    os.makedirs(os.path.join(out, 'full'), exist_ok=True)
    os.makedirs(os.path.join(out, 'docs'), exist_ok=True)
    s.eval('gdMain.messageTray.bannerBlocked = true; gdMain.messageTray.getSources().forEach(x => x.destroy()); true')
    editors = []
    for key, (name, text, _select) in EDITORS.items():
        editors.append(s.spawn_text_editor(name, text))
        s.place_window(name, 40, 60, 1000, 640)
    saved = []
    for variant in ('dark', 'light'):
        rig.scheme(variant)
        time.sleep(1.5)  # the editors re-theme too
        backdrop = None
        for name in list(state_views(0)) + ['problem', 'message']:
            if BACKDROP.get(name, 'correct') != backdrop:
                backdrop = BACKDROP.get(name, 'correct')
                rig.hide(0)
                focus_editor(rig, backdrop)
                time.sleep(0.3)
            sid = rig.next_id()
            v = (message(sid, PRIVATE[0]) if name == 'message' else problem_view(sid) if name == 'problem'
                 else state_views(sid)[name])
            path = os.path.join(out, '%s-%s.png' % (name, variant))
            shoot_view(rig, v, path, os.path.join(out, 'full', '%s-%s.png' % (name, variant)))
            saved.append(path)
            key = '%s-%s' % (name, variant)
            if rig.args.docs and key in SITE_SHOTS:
                shutil.copyfile(path, os.path.join(SITE, SITE_SHOTS[key]))
        for name, doc_backdrop in DOC_VIEWS.items() if rig.args.docs else ():
            key = '%s-%s' % (name, variant)
            if key not in DOC_SHOTS:
                continue
            rig.hide(0)
            focus_editor(rig, doc_backdrop)
            time.sleep(0.3)
            path = os.path.join(out, 'docs', DOC_SHOTS[key])
            shoot_view(rig, docs_views(rig.next_id())[name], path)
            saved.append(path)
            os.makedirs(DOCS, exist_ok=True)
            shutil.copyfile(path, os.path.join(DOCS, DOC_SHOTS[key]))
        rig.hide(0)
    note('%d screenshots in %s%s' % (len(saved), out, ' (+ docs/panel, site)' if rig.args.docs else ''))
    for editor in editors:
        editor.kill()


PANEL_TESTS = [('ping', t_ping), ('testmode', t_testmode), ('states', t_states), ('plurals', t_plurals),
               ('escape', t_escape), ('updates', t_updates), ('caret_follow', t_caret_follow), ('above', t_above),
               ('window_fallback', t_window_fallback), ('buttons', t_buttons), ('problem', t_problem), ('copy', t_copy),
               ('hide', t_hide),
               ('stale', t_stale), ('toast', t_toast), ('hud', t_hud), ('live', t_live), ('live_checking', t_live_checking), ('live_lava', t_live_lava),
               ('marks', t_marks),
               ('invalid', t_invalid), ('oversized', t_oversized),
               ('vanish', t_vanish), ('focus', t_focus), ('osk', t_osk), ('no_monitor', t_no_monitor),
               ('enable_failure', t_enable_failure), ('shutdown', t_shutdown), ('cycle', t_cycle),
               ('screenshots', t_screenshots)]


# ---- caller session ------------------------------------------------------------------------------------------
BUTTONS_JS = """(() => { const p = gdMain.layoutManager.uiGroup.get_children().find(a => a.name === 'gramadrama-panel');
  const out = {visible: p.visible, buttons: {}};
  const walk = a => {
    if (a instanceof imports.gi.St.Button && a.mapped) {
      const [x, y] = a.get_transformed_position(); const [w, h] = a.get_transformed_size();
      out.buttons[a.label || a.accessible_name] = [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
    }
    a.get_children().forEach(walk); };
  walk(p); return out; })()"""


def t_caller(rig):
    """Without test mode only the installed engine as ibus-daemon starts it may Show/Hide: exactly
    `<data dir>/gramadrama-engine/gramadrama-engine --ibus` with ibus-daemon as the parent. Ping is
    open to everybody. Impostors, including one with the exact command line, are refused, and their
    Hide(0) cannot touch the engine's panel."""
    s = rig.s
    check(rig.ping() == VERSION, 'anyone may Ping')
    check(s.eval('typeof globalThis.gdGramaDramaPanel') == 'undefined', 'no test hook outside test mode')
    check('GramaDrama: test mode' not in s.tail('shell.log', 1000000), 'no test mode outside the harness condition')
    for method, sig, args in (('Show', '(s)', [json.dumps(view(1))]), ('Hide', '(u)', [0]),
                              ('Hud', '(s)', [json.dumps({'v': 1, 'state': 'message', 'message': 'x', 'copy': 'y'})]),
                              ('Live', '(s)', [json.dumps({'v': 1, 'count': 1, 'issues': []})])):
        try:
            rig.call(method, sig, args)
            raise Fail('%s from the test client must be refused' % method)
        except HarnessError as exc:
            check(DENIED in str(exc), '%s from ctl.py: AccessDenied' % method, str(exc)[-300:])
    check(not s.eval(BUTTONS_JS)['visible'], 'nothing shown')
    win = rig.win
    win.select('tv', 19, 19)
    s.tap('right')  # a real move: GTK reports the caret after input only
    time.sleep(0.4)
    # the engine: ibus-daemon starts the stand-in from the IBus component, like the installed engine
    launcher = os.path.join(s.env['XDG_DATA_HOME'], 'gramadrama-engine', 'gramadrama-engine')
    write_standin(launcher)
    s.sh('timeout 10 ibus engine gramadrama', timeout=20)
    engine = FakeEngine(s, 'ibus', spawn=False, log=os.path.join(os.path.dirname(launcher), 'actions-ibus.jsonl'))
    pids = s.pids(r'^/usr/bin/python3 %s --ibus$' % re.escape(launcher))
    check(len(pids) == 1, 'one stand-in engine started by ibus-daemon', pids)
    ppid = int(open('/proc/%d/stat' % pids[0]).read().rsplit(')', 1)[1].split()[1])
    parent = open('/proc/%d/cmdline' % ppid, 'rb').read().replace(b'\0', b' ').decode()
    check('ibus-daemon' in parent, "the stand-in's parent is ibus-daemon", parent)
    r = engine.call('Show', [json.dumps(view(5))])
    check(r == {'ok': True, 'value': [True]}, 'the engine (ibus-daemon child, launcher path, --ibus) may Show', r)
    last = [None]

    def laid_out():
        st = s.eval(BUTTONS_JS)
        rect = st['buttons'].get('Anuluj')
        ok = st['visible'] and rect and None not in rect and rect[2] > 0 and rect == last[0]
        last[0] = rect
        return st if ok else None
    state = wait_until(laid_out, 3, 0.06, what='panel with its buttons laid out')
    x, y, w, h = state['buttons']['Anuluj']
    mark = win.mark()
    s.click(x + w // 2, y + h // 2)
    got = wait_until(lambda: engine.actions(), 3, what='Action at the engine')
    check(got[0]['session'] == 5 and got[0]['action'] == 'cancel', 'the engine receives Action(5, cancel)', got)
    check(not [e for e in win.events(mark) if e['ev'] == 'focus'], 'no focus change in the app')
    # The engine may subscribe to the focused app (docs/apps.md, section 1).
    r = engine.call('Subscribe', [])
    check(r == {'ok': True, 'value': []}, 'the engine may Subscribe', r)
    gtk = ['window:org.gramadrama.TestWin4', 'wayland']
    got = wait_until(lambda: pairs(engine.focus()), 3, what='Focus at the engine')
    check(got == [gtk], 'the engine gets the focused app right after Subscribe', got)
    phishing = view(9, 'error', error='Twoja sesja wygasła. Wpisz hasło…')
    for name, tail, script in (
            ('samepath', ('--ibus',), launcher),  # the exact command line, but not a child of ibus-daemon
            ('suffix', ('/nonexistent/gramadrama-engine', '--ibus'), 'not-the-engine.py'),  # the review's case
            ('extra', ('--ibus', '--verbose'), 'gramadrama-engine'),
            ('other', ('--ibus',), 'other-engine'),
            ('noibus', ('--ibusx',), 'gramadrama-engine')):
        impostor = FakeEngine(s, name, tail, script)
        what = '%s %s' % (os.path.basename(script), ' '.join(tail))
        r = impostor.call('Show', [json.dumps(phishing)])
        check(not r['ok'] and r['error'] == DENIED, 'Show refused: %s' % what, r)
        r = impostor.call('Hide', [0])
        check(not r['ok'] and r['error'] == DENIED, 'Hide refused: %s' % what, r)
        check(s.eval(BUTTONS_JS)['visible'], "the engine's panel survives the impostor's Hide(0): %s" % what)
        r = impostor.call('Subscribe', [])
        check(not r['ok'] and r['error'] == DENIED, 'Subscribe refused: %s' % what, r)
        impostor.kill()
    r = engine.call('Hide', [5])
    check(r == {'ok': True, 'value': []}, 'the engine may Hide', r)
    check(not s.eval(BUTTONS_JS)['visible'], 'hidden by the engine')
    r = engine.call('Ping', [])
    check(r == {'ok': True, 'value': [VERSION]}, 'Ping from the engine', r)
    # Unicast: a process listening for Focus (without Subscribe, or refused) gets nothing, while the
    # engine still is the subscriber after the impostors' attempts.
    listener = FakeEngine(s, 'eavesdrop', ('--ibus',), 'other-engine')
    check(not listener.call('Subscribe', [])['ok'], 'Subscribe refused: other-engine')
    n = len(engine.focus())
    s.eval('gdMain.openRunDialog(); true')
    wait_until(lambda: pairs(engine.focus()[n:]) == [['org.gnome.Shell', 'shell']], 3, what='Focus: the run dialog')
    s.tap('esc')
    wait_until(lambda: pairs(engine.focus()[n:]) == [['org.gnome.Shell', 'shell'], gtk], 3, what='Focus: back')
    time.sleep(0.3)
    check(listener.focus() == [], 'Focus reached a process that is not the subscriber', listener.focus()[:3])
    listener.kill()


CALLER_TESTS = [('caller', t_caller)]
UNIT_TESTS = [('policy', t_policy)]


# ---- driver ------------------------------------------------------------------------------------------------------
results = []


def record(name, ok, detail=''):
    print('%s %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok or not detail else ': %s' % detail), flush=True)
    results.append((name, ok))


def verify_clean(s, label):
    if s.work is None:
        return
    left = s.leftovers or {}
    record('%s teardown: no leftovers' % label, s.leftovers is not None and not any(left.values()), left)
    record('%s teardown: work dir removed' % label, not os.path.exists(s.work), s.work)


def run_units(tests, args):
    for name, fn in tests:
        t0 = time.monotonic()
        try:
            fn(None)
            record('%s (%.1fs)' % (name, time.monotonic() - t0), True)
        except Exception as exc:
            if args.verbose or not isinstance(exc, Fail):
                traceback.print_exc()
            record(name, False, exc)


def run_session(label, tests, args, env, **session_args):
    if not tests:
        return
    # The logs are always kept until the end: the shell log is checked once more after gnome-shell
    # has shut down (callbacks must not touch the extension's disposed actors then either).
    logs = os.path.join(args.logs, label) if args.logs else tempfile.mkdtemp(prefix='panel-logs-')
    s = Session(sources=[('xkb', 'pl')], env=env, extensions=[EXTENSION], name=label, copy_logs_to=logs,
                verbose=args.verbose, **session_args)
    try:
        with s:
            rig = Rig(s, args)
            rig.workarea = s.eval('(r => ({x: r.x, y: r.y, width: r.width, height: r.height}))'
                                  '(gdMain.layoutManager.getWorkAreaForMonitor(gdMain.layoutManager.primaryIndex))')
            s.eval('gdMain.messageTray.bannerBlocked = true; true')
            rig.win = s.spawn_gtk(4)
            s.place_window(WIN_TITLE, 40, 60, 900, 560)
            rig.win.set_text('tv', SRC + '\nDrugi akapit tekstu do sprawdzenia.\nTrzeci akapit.')
            if env.get('GRAMADRAMA_ENGINE_TEST') == '1':
                rig.listener = FakeEngine(s, 'listener')
            for name, fn in tests:
                t0 = time.monotonic()
                errors = len(rig.shell_errors())
                try:
                    fn(rig)
                    new = rig.shell_errors()[errors:]
                    check(not new, 'shell log: %d new warnings/errors about the extension' % len(new), *new[:3])
                    record('%s (%.1fs)' % (name, time.monotonic() - t0), True)
                except Exception as exc:
                    if args.verbose or not isinstance(exc, (Fail, TimeoutError)):
                        traceback.print_exc()
                    record(name, False, exc)
                    if env:  # test mode: clear the screen for the next test
                        try:
                            rig.hide(0)
                        except Exception:
                            pass
    except Exception as exc:
        traceback.print_exc()
        record('%s session' % label, False, exc)
    finally:
        verify_clean(s, label)
        try:
            with open(os.path.join(logs, 'shell.log'), encoding='utf-8', errors='replace') as f:
                problems = extension_problems(f.read())
            record('%s shell log incl. shutdown: no warnings/errors about the extension' % label, not problems,
                   problems[:3])
        except OSError as exc:
            record('%s shell log incl. shutdown' % label, False, exc)
        if not args.logs:
            shutil.rmtree(logs, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    names = [n for n, _ in UNIT_TESTS + PANEL_TESTS + CALLER_TESTS]
    ap.add_argument('--only', help='comma-separated test names: %s' % ','.join(names))
    ap.add_argument('--shots', help='directory for screenshots of every state (dark and light)')
    ap.add_argument('--docs', action='store_true', help="also refresh docs/panel/*.png and the site's panel pictures (needs --shots)")
    ap.add_argument('--logs', help='copy the session logs here')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    only = set(args.only.split(',')) if args.only else None
    if only and not only <= set(names):
        ap.error('unknown test in --only: %s' % sorted(only - set(names)))
    if args.docs and not args.shots:
        ap.error('--docs needs --shots DIR')
    pick = lambda tests: [(n, f) for n, f in tests if only is None or n in only]  # noqa: E731
    t0 = time.time()
    run_units(pick(UNIT_TESTS), args)
    run_session('panel', pick(PANEL_TESTS), args, TEST_ENV)
    # No test mode here; the IBus component starts the stand-in engine like the installed engine.
    run_session('caller', pick(CALLER_TESTS), args, {}, engine=True, engine_exec=STANDIN_EXEC)
    failed = [n for n, ok in results if not ok]
    print('PANEL %s (%d checks, %d failed, %.0fs)' % ('FAIL' if failed else 'PASS', len(results), len(failed),
                                                      time.time() - t0), flush=True)
    return 1 if failed or not results else 0



if __name__ == '__main__':
    sys.exit(main())
