#!/usr/bin/env python3
"""Scenario suite for the GramaDrama IBus engine (docs/architecture.md, Tests): captures and refusals, the real
backend against a stub model server on 127.0.0.1 (tests/stub_server.py), and (with docs/apps.md) the
GNOME Shell extension's panel and the focused app it pushes, with the session's own apps.json.

Every scenario runs in an isolated headless GNOME session (tests/session/harness.py) whose input
source is the real engine, [('ibus', 'gramadrama')]. Text ground truth comes from the fixtures
only (testwin JSONL/D-Bus, the Chrome fixture's console log); the engine's JSONL log gives its
decisions (refusal reasons, verification) and is grepped for fixture text after every session.

usage: integration_engine.py [--only NAME[,NAME...]] [--logs DIR] [--repeat N] [-v]
"""
import argparse
import functools
import http.server
import json
import os
import shutil
import signal
import statistics
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # the repository: linux/ is in it, and browser/ (the extension the Mac shares)
sys.path.insert(0, os.path.join(HERE, 'session'))
sys.path.insert(0, os.path.dirname(HERE))
import keyseq as K  # noqa: E402
from harness import Chrome, HarnessError, Session, wait_until  # noqa: E402
from gramadrama.engine import fallback_ui  # noqa: E402  (pure module, no gi)
from stub_server import Stub, closed_port, stream  # noqa: E402

ENGINE = [('ibus', 'gramadrama')]
STOCK = [('xkb', 'pl')]
# The speech service in test mode (gramadrama/speech.py): no microphone, no Whisper, this transcript.
DICTATED = 'Dyktowany tekst.'
ENV = {'GRAMADRAMA_ENGINE_TEST': '1', 'GRAMADRAMA_DEBUG_SPEECH': DICTATED}
PANEL_MIC_MS = 600  # the panel session's test microphone wakes this late (panel_dictation_pill)
# Fixture texts (tests/session/testwin.py and fixture.html; checked against the fixtures at start).
TV = ('Pierwszy akapit: zażółć gęślą jaźń.\n'
      'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.\n'
      'Trzeci akapit — koniec.')
P2 = 36  # start of the second TextView/textarea paragraph
SLOWO, KOT = P2 + 25, P2 + 31
ENTRY = 'Pole jednoliniowe: źdźbło 😀 i słowo PIES tutaj.'
PW = 'tajneHasło1'
CE = ['Pierwszy akapit edytora: zażółć gęślą jaźń.', 'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.',
      'Trzeci akapit — koniec.']
RICH = ['Zwykły akapit z pogrubieniem i linkiem, a słowo KOT stoi na końcu.', 'Drugi akapit bogatego edytora.']
BOLD, LINK = '<b>pogrubieniem</b>', '<a href="https://example.invalid/">linkiem</a>'
LONG = '\n'.join('Akapit %d: zażółć gęślą jaźń 😀 — to jest dość długi akapit testowy numer %d.' % (i, i)
                 for i in range(60))  # fixture.html Shift+F9
LONG_REPLY = LONG.split('\n')[30].replace('zażółć', 'ZAŻÓŁĆ, ZAŻÓŁĆ I JESZCZE RAZ ZAŻÓŁĆ')  # longer: the window moves
TYPED = 'Zażółć gęślą jaźń ĄŚĆ é č'
GTK_OTHER = 'Tekst z okna GTK.\nAkapit GTK, który nie istnieje w przeglądarce ani w innym polu.\nKoniec.'
POPUP_PAR = ' '.join('słowo%02d' % i for i in range(70))  # several pages of popup lines
DIFF_PAR = ' '.join(['ala ma kota i psa'] * 150)  # 2699 chars
BURN_MS = 8000  # the latency-engine session's backend child spins the CPU this long per correction
CRASH_BEFORE, CRASH_AFTER, DELAYED = 'przed awarią ', 'po awarii ąęź', 'żółw '
BAD_REPLY = 'ZŁY WYNIK'  # set while Enter follows a refusal: a correction started by mistake would write it
LATENCY_KEYS = 50
LATENCY_MARGIN_MS = 1.0
DIFF_TAPS, DIFF_P95_MARGIN_MS = 30, 5.0
PLACES = {4: (0, 40, 620, 600), 3: (650, 40, 620, 600)}  # side by side: a click never lands in the other
POPUP = 'gdKbd.getInputSourceManager()._ibusManager._candidatePopup'
POPUP_JS = """(p => ({visible: p.visible, aux: p._auxText.visible ? p._auxText.text : null,
  lines: p._candidateArea.visible ? p._candidateArea._candidateBoxes.filter(b => b.visible)
         .map(b => b._candidateLabel.text) : []}))(%s)""" % POPUP
LINE_JS = """(b => { const [x, y] = b.get_transformed_position(); const [w, h] = b.get_transformed_size();
  return [x, y, w, h]; })(%s._candidateArea._candidateBoxes[%%d])""" % POPUP
EMIT_JS = POPUP + "._candidateArea.emit('%s'); true"
CHATGPT = '/usr/lib/chatgpt/ChatGPT'


class Fail(AssertionError):
    pass


def check(cond, msg, *detail):
    if not cond:
        raise Fail(msg + (': ' + ' | '.join(repr(d)[:600] for d in detail) if detail else ''))


def note(msg):
    print('    ' + msg, flush=True)


def swap_word(text, old, new):
    check(text.count(old) >= 1, 'fixture word missing', old)
    return text.replace(old, new, 1)


def upper_line(text, i):
    lines = text.split('\n')
    lines[i] = lines[i].upper()
    return '\n'.join(lines)


class Engine:
    """The engine's D-Bus diagnostics and its JSONL log (never text)."""

    def __init__(self, s):
        self.s = s
        self.path = s.env['GRAMADRAMA_ENGINE_LOG']

    def call(self, method, sig=None, args=None):
        return self.s.call('pl.gramadrama.Engine', '/pl/gramadrama/Engine', 'pl.gramadrama.Engine', method, sig, args)

    def status(self):
        return json.loads(self.call('Status')[0])

    def snapshot(self):
        return json.loads(self.call('Snapshot')[0])

    def reply(self, text):
        self.call('SetDebugReply', '(s)', [text or ''])

    def delay(self, ms):
        self.call('SetDebugDelay', '(i)', [-1 if ms is None else ms])

    def events(self, since=0):
        try:
            with open(self.path, encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            return []
        return [json.loads(l) for l in lines[since:] if l.endswith('\n')]

    def mark(self):
        return len(self.events())

    def wait(self, pred, since=0, timeout=5, what='engine event'):
        return wait_until(lambda: next((e for e in self.events(since) if pred(e)), None), timeout, what=what)

    def wait_status(self, pred, timeout=5, what='engine status'):
        return wait_until(lambda: (lambda st: st if pred(st) else None)(self.status()), timeout, what=what)

    def synced(self, timeout=2):
        """Wait until the engine trusts its copy of the field and no session runs."""
        return self.wait_status(lambda st: st.get('fresh') and not st.get('awaiting_update') and st.get('session') == 'idle',
                                timeout, 'engine in sync')


class Run:
    """One isolated session plus the fixture windows the scenarios share."""

    def __init__(self, s):
        self.s, self.e = s, Engine(s)
        self.gtk4 = self.gtk3 = self.chrome = None
        self.offset = None  # Chrome page client coordinates -> screen
        self.nudges = 0  # extra arrow-key pairs gtk_field needed to get the engine in sync

    # ---- engine decisions ----
    def hotkey(self, timeout=5):
        """Ctrl+Alt+G; returns the engine's 'start' or 'refused' event."""
        n = self.e.mark()
        self.s.combo('ctrl', 'alt', 'g')
        return self.e.wait(lambda e: e['ev'] in ('start', 'refused'), n, timeout, 'hotkey decision')

    def start(self, scope=None, chars=None, mode=0):
        ev = self.hotkey()
        check(ev['ev'] == 'start', 'hotkey refused', ev, self.e.status())
        if scope:
            check(ev['scope'] == scope, 'wrong capture scope', ev)
        if chars is not None:
            check(ev['chars'] == chars, 'wrong capture length', ev, chars)
        self.drawn()
        if self.e.status()['mode'] != fallback_ui.MODES[mode][0]:
            n = self.e.mark()
            self.s.combo('ctrl', str(mode + 1))
            self.e.wait(lambda e: e['ev'] == 'start', n, what='restart in mode %d' % mode)
        return ev

    def drawn(self):
        """Wait until the session popup is on screen: only then are Enter and Esc consumed."""
        self.e.wait_status(lambda st: st['modal'], 3, 'session popup drawn')
        wait_until(lambda: (self.popup() or {}).get('visible'), 3, what='popup visible in the shell')

    def refused(self, reasons, app=None, ev=None):
        """Hotkey -> refusal with its message, never modal. With app (a TestWin or Chrome), Enter is
        pressed next: it must reach the app as usual and start nothing, although a debug reply is
        set that any correction would write. The caller checks the app's own Enter afterwards.
        ev: the hotkey's event when the caller has pressed it already."""
        n = self.e.mark()
        ev = ev or self.hotkey()
        check(ev['ev'] == 'refused' and ev['reason'] in reasons, 'expected refusal %s' % (reasons,), ev, self.e.status())
        pop = self.popup()
        check(pop and pop['aux'] == fallback_ui.message(ev['reason']), 'refusal message not shown', pop)
        check(self.e.status()['modal'] is False, 'refusal must not be modal')
        if app is not None:
            self.e.reply(BAD_REPLY)
            try:
                m = app.mark()
                self.s.tap('ret')
                if isinstance(app, Chrome):
                    app.wait_event(lambda e: e['ev'] == 'keyup' and e['key'] == 'Enter', m)
                    got = app.keys_since(m)
                    check(('keydown', 'Enter') in got, 'Enter after a refusal did not reach the page', got)
                else:
                    app.wait_event(lambda e: e['ev'] == 'key' and e['type'] == 'release' and e['key'] == 'Return', m)
                    got = app.keys_since(m)
                    check(('press', 'Return') in got, 'Enter after a refusal did not reach the window', got)
                time.sleep(0.3)
                bad = [e for e in self.e.events(n) if e['ev'] in ('start', 'apply')]
                check(not bad, 'Enter after a refusal started a correction', bad)
            finally:
                self.e.reply(None)
        return ev['reason']

    def ready(self, timeout=5):
        return self.e.wait_status(lambda st: st['session'] == 'ready', timeout, 'result ready')

    def accept(self, outcomes=('ok',), timeout=5):
        """Enter in READY, with the result on screen; returns the verification outcome."""
        wait_until(lambda: (lambda p: p and p['visible'] and fallback_ui.STATUS['ready'] in (p['aux'] or '') and p['lines'])(
            self.popup()), 3, what='visible popup with the result before Enter')
        n = self.e.mark()
        self.s.tap('ret')
        ev = self.e.wait(lambda e: e['ev'] in ('verified', 'refused') or (e['ev'] == 'end' and e['why'] == 'unchanged'),
                         n, timeout, 'apply outcome')
        check(ev['ev'] == 'verified' and ev['outcome'] in outcomes, 'apply did not verify', ev)
        return ev['outcome']

    def cancel(self):
        n = self.e.mark()
        self.s.tap('esc')
        self.e.wait(lambda e: e['ev'] == 'end' and e['why'] == 'cancel', n, what='cancel')

    def idle(self):
        """Make sure no session is open (Esc is consumed only by a modal session)."""
        if self.e.status()['session'] != 'idle':
            self.s.tap('esc')
            self.e.wait_status(lambda st: st['session'] == 'idle', what='session closed')

    def popup(self):
        try:
            return self.s.eval(POPUP_JS)
        except HarnessError:
            return None

    def popup_with(self, text, what):
        """The popup once its aux line contains text: the engine draws from an idle callback, after
        the state that Status() reports has already changed."""
        return wait_until(lambda: (lambda p: p if p and p['visible'] and text in (p['aux'] or '') else None)(
            self.popup()), 3, what=what)

    # ---- key ground truth, read after a sentinel so that a late event cannot be missed ----
    def gtk_keys(self, win, mark):
        """Keys that reached the window since mark (both phases), up to a Shift tap sentinel: a key
        event that arrives late is logged before the sentinel's press."""
        m = win.mark()
        self.s.tap('lshift')
        win.wait_event(lambda e: e['ev'] == 'key' and e['type'] == 'release' and e['key'] == 'Shift_L', m)
        evs = win.events(mark)
        end = next(i for i, e in enumerate(evs) if i >= m - mark and e['ev'] == 'key' and e['key'] == 'Shift_L')
        return [(e['type'], e['key']) for e in evs[:end] if e['ev'] == 'key']

    def chrome_keys(self, c, mark):
        """keydown/keyup that reached the page since mark, up to the F10 dump sentinel."""
        m = c.mark()
        c.state()
        evs = c.events(mark)
        end = next(i for i, e in enumerate(evs) if i >= m - mark and e['ev'] == 'keydown' and e['key'] == 'F10')
        return [(e['ev'], e['key']) for e in evs[:end] if e['ev'] in ('keydown', 'keyup')]

    # ---- GTK fixture ----
    def gtk(self, version=4):
        attr = 'gtk%d' % version
        win = getattr(self, attr)
        if win is None:
            win = self.s.spawn_gtk(version)
            check(win.text('tv') == TV and win.text('entry') == ENTRY, 'testwin fixture text drifted', win.text('tv'))
            self.s.place_window(win.title, *PLACES[version])
            setattr(self, attr, win)
        else:
            self.s.activate_window(win.title)
        return win

    def gtk_field(self, win, widget, text, caret, select=0):
        """Put text into widget and place the caret (then select `select` chars with Shift+Right)
        with keyboard moves: the update that answers a key is the only one the engine trusts."""
        self.idle()
        win.focus(widget)
        win.set_text(widget, text)
        near = caret - 1 if caret > 0 else caret + 1
        win.select(widget, near, near)
        self.s.tap('right' if caret > 0 else 'left')
        for _ in range(select):
            self.s.tap('right', ['lshift'])
        # (GtkEntry reports [anchor, end] where GtkTextView reports [insert, bound].)
        want = sorted([caret + select, caret])
        wait_until(lambda: sorted(win.get(widget)['sel']) == want, 3, what='%s selection %s' % (widget, want))
        try:
            return self.e.synced(0.5)
        except TimeoutError:
            # ibus-daemon drops a surrounding text equal to the last one it sent: when the field is
            # back in a state the engine saw before a key it could not follow (e.g. Ctrl+A), the
            # engine rightly stays stale. A user would press an arrow key, as the message says.
            self.nudges += 1
            st = self.e.status()
            note('nudge: %s caret %d select %d; engine fresh=%s awaiting=%s after_reset=%s text_len=%s cursor=%s' % (
                widget, caret, select, st['fresh'], st['awaiting_update'], st['after_reset'], st['text_len'], st['cursor']))
            mods = ['lshift'] if select else []
            self.s.tap('left' if select or caret > 0 else 'right', mods)
            self.s.tap('right' if select or caret > 0 else 'left', mods)
            wait_until(lambda: sorted(win.get(widget)['sel']) == want, 3, what='%s selection %s' % (widget, want))
            return self.e.synced()

    # ---- Chrome fixture ----
    def browser(self):
        if self.chrome is None:
            self.chrome = self.s.spawn_chrome(self.s.fixture_url(focus='ta'), name='chromewl')
            st = self.chrome.state()
            check(st['ta']['text'] == TV and st['in']['text'] == ENTRY and st['ce']['text'] == '\n'.join(CE),
                  'fixture.html text drifted', st['ta']['text'])
            # Chrome sends no surrounding text on focus, only on the first caret or text change.
            check(self.e.status()['text_len'] is None, 'text before any change?', self.e.status())
            self.macro('f3')
        else:
            self.s.activate_window('GD fixture')
        return self.chrome

    def macro(self, key, shift=False, sync=True):
        c = self.chrome
        name = ('S-' if shift else '') + key.upper()
        n = c.mark()
        self.s.tap(key, ['lshift'] if shift else [])
        ev = c.wait_event(lambda e: e['ev'] == 'macro' and e['macro'] == name, n)
        if sync:
            try:
                self.e.synced(0.7)
            except TimeoutError:
                self.nudge_page(ev['state'], name)
        return ev['state']

    def nudge_page(self, st, name):
        """The first update after a focus change is never trusted, and Chrome re-creates its text
        input (focus out and in) when a script replaces the selection in a contenteditable; when
        nothing changed, it pushes nothing. A key there and one back make it push the very caret or
        selection the macro set, and the update that answers a key is trusted."""
        self.nudges += 1
        field = st.get(st.get('active') or '') or {}
        sel, text = (field.get('sel') or [0, 0])[:2], field.get('text') or ''
        mods = ['lshift'] if sel[0] != sel[1] else []
        there, back = ('left', 'right') if max(sel) >= len(text) else ('right', 'left')
        note('nudge: macro %s (%s%s, %s)' % (name, '+'.join(mods + [there]), '', back))
        self.s.tap(there, mods)
        self.s.tap(back, mods)
        self.e.synced()

    def calibrate(self):
        """Screen offset of the page: click the empty page background and read mousedown."""
        fx, fy, fw, fh = self.s.window('GD fixture')['frame']
        x, y = fx + fw // 2, fy + fh - 30
        n = self.chrome.mark()
        self.s.click(x, y)
        ev = self.chrome.wait_event(lambda e: e['ev'] == 'mousedown', n)
        check(ev['target'] in (None, ''), 'calibration click hit a field', ev)
        self.offset = (x - ev['x'], y - ev['y'])

    def geometry(self):
        n = self.chrome.mark()
        self.s.tap('f7', ['lshift'])
        return self.chrome.wait_event(lambda e: e['ev'] == 'geom', n)['geom']

    def screen(self, r, fx=0.5):
        return int(round(self.offset[0] + r['x'] + r['w'] * fx)), int(round(self.offset[1] + r['y'] + r['h'] / 2))

    def chrome_text(self, field, timeout=5):
        return self.chrome.state(timeout)[field]


def ce_off(par, i):
    return sum(len(p) + 1 for p in CE[:par]) + i


def tv_changes(win, mark):
    return [e['text'] for e in win.events(mark) if e['ev'] == 'changed' and e['w'] == 'tv']


# ---------------------------------------------------------------------------------------------
# scenarios (main session: GTK 4, GTK 3 and Chrome on Wayland, plus Chrome on XWayland at the end)
# ---------------------------------------------------------------------------------------------
def typing_passthrough(r):
    win = r.gtk(4)
    r.idle()
    win.focus('tv')
    win.set_text('tv', '')
    keys = r.e.status()['keys']['n']
    r.s.type_text(TYPED)
    win.wait_text('tv', TYPED, timeout=10)
    check(r.e.status()['keys']['n'] > keys + 2 * len(TYPED), 'keys did not go through the engine', r.e.status()['keys'])
    check(r.s.ibus_engine_status()['engine'] == 'gramadrama', 'engine not active')


RCTRL = 97  # evdev code of the right Ctrl: held alone, it dictates (docs/dictation.md)


def dictation_hold(r):
    """Hold the right Ctrl alone: 'Słucham…' while held (the IBus popup's line without the extension),
    and at the key-up the service's text at the caret, by commit, with no clipboard."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1]])
    try:
        r.popup_with('Słucham…', 'the recording shown in the popup')
        time.sleep(0.6)  # speak (the test service records a tone)
    finally:
        r.s.keys([['code', RCTRL, 0]])
    ev = r.e.wait(lambda e: e['ev'] == 'dictation', n, 10, 'dictation outcome')
    check(ev['outcome'] == 'inserted' and ev['chars'] == len(DICTATED), 'dictation not inserted', ev)
    check(ev['audio_ms'] >= 500 and 'Dyktowany' not in json.dumps(ev), 'dictation log', ev)
    win.wait_text('tv', TV[:KOT] + DICTATED + TV[KOT:])  # after a space: no space added


def dictation_shortcuts_and_cancel(r):
    """The right Ctrl stays a Ctrl: with another key it is a shortcut (nothing records), a short tap
    records nothing, and another key during a recording throws it away and reaches the app."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    m = win.mark()
    r.s.keys([['code', RCTRL, 1], ['code', 30, 1], ['code', 30, 0], ['code', RCTRL, 0]])  # right Ctrl+A
    r.s.keys([['code', RCTRL, 1], ['sleep', 100], ['code', RCTRL, 0]])  # a tap
    time.sleep(0.6)
    check(not [e for e in r.e.events(n) if e['ev'] == 'dictation'], 'a shortcut or a tap dictated', r.e.events(n))
    keys = r.gtk_keys(win, m)
    check(('press', 'Control_R') in keys and ('press', 'a') in keys, 'the right Ctrl and A reached the app', keys)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1], ['sleep', 700], ['code', 106, 1], ['code', 106, 0], ['code', RCTRL, 0]])  # Ctrl+Right
    ev = r.e.wait(lambda e: e['ev'] == 'dictation', n, 5, 'dictation outcome')
    check(ev['outcome'] == 'cancelled', 'another key did not cancel', ev)
    time.sleep(0.5)
    check(win.text('tv') == TV, 'a cancelled dictation changed the text', win.text('tv'))


def dictation_shift(r):
    """Shift with the right Ctrl dictates in the other language instead of cancelling (macOS 918d2c4): down
    before the key, or during the recording (the popup's line says EN), and the next dictation is back to the
    setting's language. Right Ctrl+Shift+T is still a shortcut."""
    LSHIFT, KEY_T = 42, 20
    win = r.gtk(4)

    def dictation(n):
        return r.e.wait(lambda e: e['ev'] == 'dictation', n, 10, 'dictation outcome')
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', LSHIFT, 1], ['code', RCTRL, 1], ['sleep', 700], ['code', RCTRL, 0], ['code', LSHIFT, 0]])
    ev = dictation(n)
    check((ev['outcome'], ev['language'], ev['alternate']) == ('inserted', 'en', True), 'Shift before the key', ev)
    check('Dyktowany' not in json.dumps(ev), 'the log holds text', ev)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1]])
    try:
        r.popup_with('Słucham…', 'the recording shown in the popup')
        check('· EN' not in ((r.popup() or {}).get('aux') or ''), 'English before Shift', r.popup())
        r.s.keys([['code', LSHIFT, 1], ['sleep', 150], ['code', LSHIFT, 0]])
        r.popup_with('· EN', 'the popup says the dictation is now English')
        time.sleep(0.3)
    finally:
        r.s.keys([['code', RCTRL, 0]])
    ev = dictation(n)
    check((ev['outcome'], ev['language'], ev['alternate']) == ('inserted', 'en', True), 'Shift during the recording', ev)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1], ['sleep', 700], ['code', RCTRL, 0]])
    ev = dictation(n)
    check((ev['outcome'], ev['language'], ev['alternate']) == ('inserted', 'pl', False), 'the next one is Polish again', ev)
    # right Ctrl+Shift+T: a shortcut, nothing records and T reaches the app
    r.gtk_field(win, 'tv', TV, KOT)
    n, m = r.e.mark(), win.mark()
    r.s.keys([['code', RCTRL, 1], ['code', LSHIFT, 1], ['code', KEY_T, 1], ['code', KEY_T, 0], ['code', LSHIFT, 0],
              ['code', RCTRL, 0]])
    time.sleep(0.8)
    check(not [e for e in r.e.events(n) if e['ev'] == 'dictation'], 'Ctrl+Shift+T dictated', r.e.events(n))
    keys = r.gtk_keys(win, m)
    check(('press', 'T') in keys or ('press', 't') in keys, 'T reached the app', keys)


def translate_hotkey(r):
    """Ctrl+Alt+J (⌥⌘T on macOS): straight into Tłumacz, its keys consumed; the next Ctrl+Alt+G is
    Popraw again, whatever mode the last session ended in."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    m = win.mark()
    n = r.e.mark()
    r.s.combo('ctrl', 'alt', 'j')
    ev = r.e.wait(lambda e: e['ev'] in ('start', 'refused'), n, 5, 'Ctrl+Alt+J decision')
    check((ev['ev'], ev.get('mode'), ev.get('scope')) == ('start', 'translate', 'paragraph'), 'no Tłumacz', ev)
    r.drawn()
    r.popup_with('[6 Tłumacz', 'Tłumacz active in the popup')
    r.cancel()
    ev = r.hotkey()
    check((ev['ev'], ev.get('mode')) == ('start', 'fix'), 'Ctrl+Alt+G after Tłumacz is not Popraw', ev)
    r.drawn()
    r.popup_with('[1 Popraw]', 'Popraw active in the popup')
    r.cancel()
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] in ('j', 'J', 'g', 'G', 'Escape')], 'consumed keys reached GTK4', keys)
    check(win.text('tv') == TV, 'text changed', win.text('tv'))


def hotkey_consumed(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    m = win.mark()
    r.start('paragraph')
    r.cancel()
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] in ('g', 'G', 'Escape')], 'consumed keys reached GTK4', keys)
    check(('press', 'Control_L') in keys, 'no ground truth for the modifiers', keys)
    check(win.text('tv') == TV, 'text changed', win.text('tv'))
    c = r.browser()
    r.macro('f9', sync=False)
    r.macro('f3')
    m = c.mark()
    r.start('paragraph')
    r.cancel()
    keys = r.chrome_keys(c, m)
    check(not [k for k in keys if k[1] in ('g', 'G', 'Escape', 'Process')], 'consumed keys reached Chrome', keys)
    check(('keydown', 'Control') in keys, 'no ground truth for the modifiers', keys)
    check(r.chrome_text('ta')['text'] == TV, 'textarea changed')


def gtk4_selection(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, SLOWO, select=5)
    m = win.mark()
    r.start('selection', 5)
    r.ready()
    r.accept()
    want = swap_word(TV, 'słowo', 'SŁOWO')
    win.wait_text('tv', want)
    check(win.get('tv')['sel'] == [SLOWO + 5, SLOWO + 5], 'caret not after the replacement', win.get('tv'))
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] in ('Return', 'KP_Enter', 'g')], 'Enter/hotkey reached the app', keys)


def gtk4_paragraph_min_span(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    para = TV.split('\n')[1]
    r.e.reply(swap_word(para, 'KOT', 'PSA'))
    try:
        r.start('paragraph', len(para))
        r.ready()
        m = win.mark()
        r.accept()
    finally:
        r.e.reply(None)
    want = swap_word(TV, 'KOT', 'PSA')
    win.wait_text('tv', want)
    # Ground truth of the minimal span: the app saw 'KOT' deleted, then 'PSA' committed.
    check(tv_changes(win, m) == [TV[:KOT] + TV[KOT + 3:], want], 'replace span is not minimal', tv_changes(win, m))


def gtk4_entry_field(r):
    win = r.gtk(4)
    r.gtk_field(win, 'entry', ENTRY, len(ENTRY))
    r.start('paragraph', len(ENTRY))
    r.ready()
    r.accept()
    win.wait_text('entry', ENTRY.upper())
    # A keyboard selection in the Entry: GtkEntry reports it, so it is replaced exactly.
    pies = ENTRY.index('PIES')
    r.gtk_field(win, 'entry', ENTRY, pies, select=4)
    r.e.reply('pies')
    try:
        r.start('selection', 4)
        r.ready()
        r.accept()
    finally:
        r.e.reply(None)
    win.wait_text('entry', swap_word(ENTRY, 'PIES', 'pies'))


def gtk4_mouse_drag_refused(r):
    win = r.gtk(4)
    # A drag in a TextView the engine is in sync with: GTK4 sends only a reset for the press.
    r.gtk_field(win, 'tv', TV, 2)
    r.s.drag(win.locate(SLOWO), win.locate(SLOWO + 5))
    wait_until(lambda: sorted(win.get('tv')['sel']) == [SLOWO, SLOWO + 5], 3, what='drag selection')
    time.sleep(0.3)
    check(win.text('tv') == TV, 'text changed by the drag')
    reason = r.refused(('stale',), app=win)
    after_enter = TV[:SLOWO] + '\n' + TV[SLOWO + 5:]
    win.wait_text('tv', after_enter)  # the app's own Enter replaced its selection; nothing else changed
    # One arrow key later the hotkey works (the caret sits at the start of ' KOT w środku zdania.').
    r.s.tap('right')
    wait_until(lambda: win.get('tv')['sel'] == [SLOWO + 2] * 2, 3, what='caret moved')
    r.e.synced()
    r.start('paragraph', len(after_enter.split('\n')[2]))
    r.ready()
    r.accept()
    win.wait_text('tv', upper_line(after_enter, 2))
    note('drag refused with %s; one arrow key later the hotkey works' % reason)
    # Critique C2: double-click KOT (never pushed), then drag over 'słowo'. GTK4 answers the drag's
    # press with a reset and the KOT selection the engine never saw; it never pushes the drag.
    r.gtk_field(win, 'tv', TV, 2)
    r.s.click(*win.locate(KOT + 1), n=2)
    wait_until(lambda: sorted(win.get('tv')['sel']) == [KOT, KOT + 3], 3, what='double-click selection')
    time.sleep(0.3)
    m = r.e.mark()
    r.s.drag(win.locate(SLOWO), win.locate(SLOWO + 5))
    wait_until(lambda: sorted(win.get('tv')['sel']) == [SLOWO, SLOWO + 5], 3, what='drag selection')
    time.sleep(0.3)
    evs = r.e.events(m)
    first_reset = next((i for i, e in enumerate(evs) if e['ev'] == 'reset'), len(evs))
    echo = [e for e in evs[first_reset:] if e['ev'] == 'surrounding' and e['anchor'] != e['cursor']]
    check(echo, 'precondition: no echo of the unseen double-click selection after the drag\'s reset', evs)
    r.refused(('stale',), app=win)
    win.wait_text('tv', after_enter)
    note('C2 sequence: the echo %s after the drag\'s reset was not trusted' % ([echo[0]['cursor'], echo[0]['anchor']],))


def gtk4_ctrl_a_refused(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.s.combo('ctrl', 'a')
    wait_until(lambda: sorted(win.get('tv')['sel']) == [0, len(TV)], 3, what='select all')
    time.sleep(0.3)
    reason = r.refused(('stale', 'hidden_selection'), app=win)
    win.wait_text('tv', '\n')  # the app's own Enter replaced everything it had selected
    note('ctrl+a refused with %s' % reason)
    # Ctrl+A, then a click: GTK4 answers the click's press with the select-all it never pushed.
    # Only the paragraph at the real caret may be captured, or nothing.
    r.gtk_field(win, 'tv', TV, 2)
    r.s.combo('ctrl', 'a')
    wait_until(lambda: sorted(win.get('tv')['sel']) == [0, len(TV)], 3, what='select all')
    time.sleep(0.3)
    r.s.click(*win.locate(KOT))
    wait_until(lambda: win.get('tv')['sel'] == [KOT, KOT], 3, what='caret after the click')
    time.sleep(0.3)
    ev = r.hotkey()
    if ev['ev'] == 'start':
        check((ev['scope'], ev['chars']) == ('paragraph', len(TV.split('\n')[1])), 'captured the wrong text', ev)
        r.drawn()
        r.ready()
        r.accept()
        win.wait_text('tv', upper_line(TV, 1))
        note('ctrl+a then click: the paragraph at the click was corrected')
    else:
        check(ev['reason'] in ('stale', 'selection_unknown'), 'unexpected refusal', ev)
        check(win.text('tv') == TV, 'text changed after the refusal')
        note('ctrl+a then click: refused %s' % ev['reason'])


def pointer_focus_refused(r):
    """Review finding (C2 through focus): a click or a drag that moves the keyboard focus into a
    TextView, from another field or from another window. At focus-in GTK pushes the TextView's state
    from before the click; GTK3 never pushes the click's caret and GTK4 never pushes a drag. The
    hotkey must refuse and change nothing, and Enter then reaches the app as usual."""
    for version in (4, 3):
        for how in ('click', 'drag'):
            for source in ('entry', 'window'):
                win = r.gtk(version)
                r.gtk_field(win, 'tv', TV, 2)  # engine in sync, caret in paragraph 1
                n = r.e.mark()
                if source == 'entry':
                    win.focus('entry')
                else:
                    r.gtk(7 - version)  # the other GTK window takes the focus
                r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] == 'gnome-shell', n, what='focus left the TextView')
                time.sleep(0.3)
                n = r.e.mark()
                if how == 'click':
                    r.s.click(*win.locate(KOT))
                    sel, after_enter = [KOT, KOT], TV[:KOT] + '\n' + TV[KOT:]
                else:
                    r.s.drag(win.locate(SLOWO), win.locate(SLOWO + 5))
                    sel, after_enter = [SLOWO, SLOWO + 5], TV[:SLOWO] + '\n' + TV[SLOWO + 5:]
                wait_until(lambda: sorted(win.get('tv')['sel']) == sel and (lambda i: i['focus'] == 'tv' and i['active'])(
                    win.info()), 3, what='gtk%d %s from the %s' % (version, how, source))
                r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] == 'gnome-shell', n, what='focus in the TextView')
                time.sleep(0.3)  # every update GTK sends for the click has arrived
                check(win.text('tv') == TV, 'text changed by the pointer')
                ev = r.hotkey()
                if ev['ev'] == 'start':
                    # GTK sent the click's state after the one from before it, and AT-SPI saw that state
                    # (docs/architecture.md, Text safety): only the text at the pointer, and it applies.
                    lo, hi = (P2, P2 + len(TV.split('\n')[1])) if how == 'click' else (SLOWO, SLOWO + 5)
                    check((ev['scope'], ev['chars']) == ('paragraph' if how == 'click' else 'selection', hi - lo),
                          'captured the wrong text', ev)
                    r.drawn()
                    r.ready()
                    r.accept()
                    win.wait_text('tv', TV[:lo] + TV[lo:hi].upper() + TV[hi:])
                    note('gtk%d %s from the %s: the text at the pointer was corrected' % (version, how, source))
                    continue
                reason = r.refused(('stale', 'selection_unknown'), app=win, ev=ev)
                win.wait_text('tv', after_enter)  # the app's own Enter; nothing else changed
                note('gtk%d %s from the %s: refused %s' % (version, how, source, reason))


def gtk3_selection_window_switch_refused(r):
    """Review finding (H1 across focus): GTK3 keeps a keyboard selection it never reports while its
    window is inactive; after switching back the hotkey must not correct the paragraph around it."""
    win = r.gtk(3)
    r.gtk_field(win, 'tv', TV, P2)
    for _ in range(5):
        r.s.tap('right', ['lshift'])
    wait_until(lambda: sorted(win.get('tv')['sel']) == [P2, P2 + 5], 3, what='keyboard selection')
    time.sleep(0.3)
    r.refused(('hidden_selection',))
    r.gtk(4)
    n = r.e.mark()
    r.gtk(3)
    r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] == 'gnome-shell', n, what='focus back in GTK3')
    wait_until(lambda: sorted(win.get('tv')['sel']) == [P2, P2 + 5], 3, what='GTK3 kept its selection')
    time.sleep(0.3)
    reason = r.refused(('stale', 'hidden_selection', 'selection_unknown'), app=win)
    win.wait_text('tv', TV[:P2] + '\n' + TV[P2 + 5:])  # the app's own Enter replaced its selection
    note('after the window switch: refused %s' % reason)


def session_keys(r):
    win = r.gtk(4)
    lines = TV.split('\n')
    # AltGr alone never cancels; an AltGr letter cancels and reaches the app.
    r.gtk_field(win, 'tv', TV, KOT)
    r.start('paragraph')
    r.s.tap('ralt')
    time.sleep(0.3)
    st = r.e.status()
    check(st['session'] in ('waiting', 'ready') and st['modal'], 'AltGr cancelled the session', st)
    r.s.type_text('ą')
    win.wait_text('tv', TV[:KOT] + 'ą' + TV[KOT:])
    check(r.e.status()['session'] == 'idle', 'letter did not cancel')
    # A plain letter cancels and reaches the app.
    r.gtk_field(win, 'tv', TV, KOT)
    r.start('paragraph')
    r.ready()
    r.s.tap('x')
    win.wait_text('tv', TV[:KOT] + 'x' + TV[KOT:])
    check(r.e.status()['session'] == 'idle', 'letter did not cancel')
    # Critique L1: a dead key during a session cancels it and composes as usual.
    r.gtk_field(win, 'tv', TV, KOT)
    r.start('paragraph')
    r.ready()
    r.s.keys(K.tap(*K.DEAD['acute']))
    r.s.tap('e')
    win.wait_text('tv', TV[:KOT] + 'é' + TV[KOT:])
    check(r.e.status()['session'] == 'idle', 'dead key did not cancel')
    # Critique L1: the hotkey in the middle of a compose sequence resets it: Esc, then 'e' is plain.
    r.gtk_field(win, 'tv', TV, KOT)
    r.s.keys(K.tap(*K.DEAD['acute']))
    r.start('paragraph')
    r.cancel()
    r.s.tap('e')
    win.wait_text('tv', TV[:KOT] + 'e' + TV[KOT:])
    # Esc cancels: text unchanged, Esc never delivered.
    r.gtk_field(win, 'tv', TV, KOT)
    m = win.mark()
    r.start('paragraph')
    r.ready()
    r.cancel()
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] == 'Escape'], 'Esc reached the app', keys)
    check(win.text('tv') == TV, 'Esc changed the text')
    # Ctrl+2 switches to Ulepsz (swapcase in the debug backend).
    r.gtk_field(win, 'tv', TV, KOT)
    r.start('paragraph')
    n = r.e.mark()
    r.s.combo('ctrl', '2')
    ev = r.e.wait(lambda e: e['ev'] == 'start', n, what='restart in mode 2')
    check(ev['mode'] == 'improve', 'Ctrl+2 did not switch mode', ev)
    r.ready()
    r.accept()
    win.wait_text('tv', '\n'.join([lines[0], lines[1].swapcase(), lines[2]]))
    # Back to Popraw for the next scenarios (the last mode is remembered).
    r.gtk_field(win, 'tv', TV, KOT)
    r.start('paragraph', mode=0)
    r.cancel()
    # Enter during WAITING applies when the result arrives.
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.delay(1500)
    try:
        r.start('paragraph')
        check(r.e.status()['session'] == 'waiting', 'backend finished too early')
        n = r.e.mark()
        r.s.tap('ret')
        time.sleep(0.3)
        st = r.e.status()
        check(st['session'] == 'waiting' and win.text('tv') == TV, 'Enter during WAITING did not wait', st)
        pop = r.popup()
        check(pop and pop['aux'] and fallback_ui.STATUS['apply_pending'] in pop['aux'], 'no "apply when ready" status', pop)
        ev = r.e.wait(lambda e: e['ev'] in ('verified', 'refused'), n, timeout=5, what='apply after result')
        check(ev['ev'] == 'verified' and ev['outcome'] == 'ok', 'deferred apply failed', ev)
    finally:
        r.e.delay(None)
    win.wait_text('tv', '\n'.join([lines[0], lines[1].upper(), lines[2]]))


def popup_clicks_and_paging(r):
    """Adapter guards: a click on a popup line never inserts anything; the popup's page signals
    scroll a running session and are chained up to EngineSimple without one."""
    win = r.gtk(4)
    text = 'Krótki akapit.\n' + POPUP_PAR + '\nKoniec.'
    r.gtk_field(win, 'tv', text, len('Krótki akapit.\n') + 3)
    r.start('paragraph', len(POPUP_PAR))
    r.ready()
    pop = wait_until(lambda: (lambda p: p if p and 'PgUp/PgDn 1/' in (p['aux'] or '') and len(p['lines']) == 8 else None)(
        r.popup()), 3, what='a result with several pages')
    counts = r.e.status()['counts']
    x, y, w, h = r.s.eval(LINE_JS % 0)
    r.s.click(int(x + w / 2), int(y + h / 2))
    r.e.wait_status(lambda st: st['counts'].get('candidate_clicked', 0) > counts.get('candidate_clicked', 0), 3,
                    'the click reached the engine')
    time.sleep(0.3)
    check(win.text('tv') == text and r.e.status()['session'] == 'ready', 'a click on a popup line changed something')
    r.s.eval(EMIT_JS % 'next-page')
    wait_until(lambda: 'PgUp/PgDn 2/' in ((r.popup() or {}).get('aux') or ''), 3, what='popup page 2')
    r.s.eval(EMIT_JS % 'previous-page')
    wait_until(lambda: 'PgUp/PgDn 1/' in ((r.popup() or {}).get('aux') or ''), 3, what='popup page 1')
    r.cancel()
    n = r.e.mark()
    counts = r.e.status()['counts']
    r.s.eval(EMIT_JS % 'next-page')
    r.s.eval(EMIT_JS % 'previous-page')
    r.e.wait_status(lambda st: st['counts'].get('page_up', 0) > counts.get('page_up', 0), 3, 'page signals reached the engine')
    time.sleep(0.3)
    check(not [e for e in r.e.events(n) if e['ev'] in ('error', 'start')], 'page signals without a session', r.e.events(n))
    check(win.text('tv') == text, 'page signals changed the text')
    note('popup "%s": line click and page signals verified' % pop['aux'].split(' · ')[-1])


def password_refused(r):
    win = r.gtk(4)
    r.idle()
    n = r.e.mark()
    before = r.e.status()['counts'].get('surrounding', 0)
    win.focus('pw')
    r.e.wait_status(lambda st: st['purpose'] == 8, what='password purpose')
    # The field's text does reach the engine (GNOME Shell sets SURROUNDING_TEXT only after the app
    # sent it) and is dropped on arrival: nothing kept, nothing logged.
    r.e.wait_status(lambda st: st['caps'] & 32 and st['counts'].get('surrounding', 0) > before, what='password text arrived')
    r.refused(('sensitive',), app=win)
    check(r.e.snapshot()['text'] is None, 'password text kept by the engine')
    check(win.text('pw') == PW, 'password field changed')
    win.focus('tv')
    c = r.browser()
    r.macro('f9', sync=False)
    before = r.e.status()['counts'].get('surrounding', 0)
    r.macro('f6', sync=False)
    r.e.wait_status(lambda st: st['purpose'] == 8, what='password purpose')
    r.e.wait_status(lambda st: st['caps'] & 32 and st['counts'].get('surrounding', 0) > before, what='password text arrived')
    time.sleep(0.2)
    r.refused(('sensitive',), app=c)
    check(r.e.snapshot()['text'] is None, 'password text kept by the engine')
    check(r.chrome_text('pw')['text'] == PW, 'password field changed')
    r.macro('f4', shift=True)  # back to the textarea
    # The engine keeps purpose and hints across focus changes; while they say sensitive, no update
    # is logged at all (Chrome also sends a transient PASSWORD type when a textarea gets focus).
    sensitive, logged = False, []
    for e in r.e.events(n):
        if e['ev'] == 'content_type':
            sensitive = e['purpose'] in (8, 9) or bool(e['hints'] & (2048 | 4096))
        elif e['ev'] == 'surrounding' and sensitive:
            logged.append(e)
    check(not logged, 'a password field update was logged', logged)
    leaks = log_leaks(r.e.path, [PW])
    check(not leaks, 'password in the engine log', leaks)


def first_focus_no_stale_reuse(r):
    c = r.browser()
    r.macro('f9', sync=False)
    r.macro('f4', shift=True)  # caret at the end of ta: paragraph 3
    win = r.gtk(4)
    r.gtk_field(win, 'tv', GTK_OTHER, GTK_OTHER.index('GTK,'))
    gtk_para = GTK_OTHER.split('\n')[1]
    r.start('paragraph', len(gtk_para))
    r.cancel()
    check(r.e.snapshot()['text'] and gtk_para in r.e.snapshot()['text'], 'engine never saw the GTK text')
    # Switch to Chrome and press the hotkey at once: never the GTK text. Chrome sends its text
    # only on the first caret or text change, so the engine refuses until then.
    r.s.activate_window('GD fixture')
    seen = [r.e.snapshot()['text']]
    ev = r.hotkey()
    for _ in range(10):
        seen.append(r.e.snapshot()['text'])
        time.sleep(0.02)
    check(not [t for t in seen if t and gtk_para in t], 'GTK text reused in Chrome', seen)
    if ev['ev'] == 'start':
        check(ev['chars'] == len(TV.split('\n')[2]), 'first capture in Chrome is not Chrome\'s text', ev)
        r.cancel()
        note('first hotkey in Chrome: captured Chrome\'s own paragraph')
    else:
        check(ev['reason'] in ('no_field', 'no_text', 'stale'), 'unexpected refusal', ev)
        note('first hotkey in Chrome: refused %s' % ev['reason'])
    r.s.tap('left')  # as the refusal message says
    r.e.synced()
    check(r.e.snapshot()['text'] == TV, 'engine does not hold Chrome\'s text')
    r.start('paragraph', len(TV.split('\n')[2]))
    r.cancel()
    # Page background focused (no editable): no capture, nothing reused. Without an enabled
    # text input the key may not reach IBus at all.
    r.calibrate()
    time.sleep(0.3)
    n = r.e.mark()
    r.s.combo('ctrl', 'alt', 'g')
    time.sleep(0.7)
    evs = [e for e in r.e.events(n) if e['ev'] in ('start', 'refused')]
    check(not [e for e in evs if e['ev'] == 'start'], 'capture without a focused field', evs)
    note('hotkey with no focused field: %s' % (evs[0]['reason'] if evs else 'not seen by the engine'))
    check(r.chrome_text('ta')['text'] == TV and win.text('tv') == GTK_OTHER, 'text changed')
    r.macro('f4', shift=True)  # focus back in the textarea


def browser_extension_connects(r):
    """The browser extension (browser/extension, the same one the Mac uses) in a real Chrome: it starts the native messaging
    host (gramadrama-browser-host), which finds the engine's socket, and the engine knows the extension (browser and version,
    in the log and in its status; no text, no site anywhere in the log). The popup's switch of a site goes through the host
    into apps.json "sites" and comes back to the extension from the engine; a change of apps.json by somebody else reaches
    it too; a broken apps.json is never overwritten from here; and closing the browser ends the connection."""
    extension = os.path.join(REPO, 'browser', 'extension')
    version = json.load(open(os.path.join(extension, 'manifest.json')))['version']
    apps_json = os.path.join(r.s.env['XDG_CONFIG_HOME'], 'gramadrama', 'apps.json')
    n = r.e.mark()
    chrome = r.s.spawn_chrome(url='about:blank', app=False, wait=False, extension=extension, name='chromeext')
    try:
        ev = r.e.wait(lambda e: e['ev'] == 'browser_connected', n, 40, 'the extension connects to the engine')
        check((ev['browser'], ev['extension']) == ('google-chrome', version), 'who connected', ev)
        check(r.e.status()['browsers'] == {'google-chrome': version}, 'the engine does not know the extension', r.e.status())
        wait_until(lambda: chrome.eval_extension('appConnected') is True, 10, what='the extension knows the engine is there')

        def sites():
            return chrome.eval_extension('[...disabledSites].sort()')
        check(sites() == [], 'no site is off yet', sites())
        # the popup's switch: "setSite" through the host; the engine writes apps.json and tells the extension
        n = r.e.mark()
        chrome.eval_extension("port.postMessage({type: 'setSite', site: 'mail.example', disabled: true}); true")
        r.e.wait(lambda e: e['ev'] == 'browser_site' and e['disabled'] is True, n, 10, 'the site saved')
        data = json.load(open(apps_json))
        check(data['v'] == 1 and data['sites'] == ['mail.example'], 'apps.json', data)  # earlier scenarios may have left other entries
        check(oct(os.stat(apps_json).st_mode & 0o777) == '0o600', 'apps.json is not private')
        wait_until(lambda: sites() == ['mail.example'], 10, what='the engine told the extension')
        # somebody else (the app) changes the file: every extension hears of it
        with open(apps_json, 'w') as f:
            json.dump({'v': 1, 'disabled': [], 'added': [], 'sites': ['mail.example', 'a.example']}, f)
        wait_until(lambda: sites() == ['a.example', 'mail.example'], 10, what='a change of apps.json reaches the extension')
        chrome.eval_extension("port.postMessage({type: 'setSite', site: 'mail.example', disabled: false}); true")
        wait_until(lambda: sites() == ['a.example'], 10, what='a site switched on again')
        check(json.load(open(apps_json))['sites'] == ['a.example'], 'apps.json after the switch back')
        # a broken apps.json is the app's to repair: the engine does not write over it
        broken = '{"v": 1, "sites": [oops'
        with open(apps_json, 'w') as f:
            f.write(broken)
        n = r.e.mark()
        chrome.eval_extension("port.postMessage({type: 'setSite', site: 'b.example', disabled: true}); true")
        r.e.wait(lambda e: e['ev'] == 'browser_site_failed', n, 10, 'the refusal')
        check(open(apps_json).read() == broken, 'a broken apps.json was overwritten')
        check('example' not in open(os.environ.get('GRAMADRAMA_ENGINE_LOG') or r.s.env['GRAMADRAMA_ENGINE_LOG']).read(),
              'a site is in the engine log')
    finally:
        chrome.kill()
    r.e.wait(lambda e: e['ev'] == 'browser_disconnected', 0, 15, 'the connection ends with the browser')
    check(r.e.status()['browsers'] == {}, 'the extension is still known', r.e.status())


def chrome_textarea_input_contenteditable(r):
    r.browser()
    lines = TV.split('\n')
    cases = [  # field, selection macro, reply, expected; paragraph macro, expected
        ('ta', ('f2', False), 'kot', swap_word(TV, 'KOT', 'kot'), ('f3', False),
         '\n'.join([lines[0], lines[1].upper(), lines[2]])),
        ('in', ('f4', False), 'pies', swap_word(ENTRY, 'PIES', 'pies'), ('f5', False), ENTRY.upper()),
        ('ce', ('f7', False), 'kot', '\n'.join([CE[0], swap_word(CE[1], 'KOT', 'kot'), CE[2]]), ('f8', False),
         '\n'.join([CE[0], CE[1].upper(), CE[2]])),
    ]
    for field, sel_macro, reply, want_sel, par_macro, want_par in cases:
        r.macro('f9', sync=False)
        r.macro(*sel_macro)
        r.e.reply(reply)
        try:
            r.start('selection', len(reply))
            r.ready()
            m = r.chrome.mark()
            r.accept()
        finally:
            r.e.reply(None)
        got = r.chrome_text(field)
        check(got['text'] == want_sel, '%s selection replace' % field, got['text'])
        keys = r.chrome_keys(r.chrome, m)
        check(not [k for k in keys if k[1] == 'Enter'], 'Enter (or its keyup) reached the page', keys)
        r.macro('f9', sync=False)
        r.macro(*par_macro)
        r.start('paragraph')
        r.ready()
        m = r.chrome.mark()
        r.accept()
        got = r.chrome_text(field)
        check(got['text'] == want_par, '%s paragraph replace' % field, got['text'])
        keys = r.chrome_keys(r.chrome, m)
        check(not [k for k in keys if k[1] == 'Enter'], 'Enter (or its keyup) reached the page', keys)
        if field == 'ce':
            check(got['html'].count('<p>') == 3, 'contenteditable blocks changed', got['html'])


def chrome_rich_paragraph_preserves_markup(r):
    c = r.browser()
    r.macro('f9', sync=False)
    r.macro('f3', shift=True)  # caret before 'słowo' in the rich paragraph
    want = swap_word(RICH[0], 'słowo', 'wyraz')
    r.e.reply(want)
    try:
        r.start('paragraph', len(RICH[0]))
        r.ready()
        m = c.mark()
        r.accept()
    finally:
        r.e.reply(None)
    got = r.chrome_text('rich')
    check(got['text'] == '\n'.join([want, RICH[1]]), 'rich text', got['text'])
    check(BOLD in got['html'] and LINK in got['html'], 'markup lost', got['html'])
    # Ground truth of the minimal span: the page saw exactly 'słowo' deleted, then 'wyraz' inserted
    # (until the insert, Chrome shows the space before the gap as a no-break space).
    inputs = [e['state']['rich']['text'] for e in c.events(m) if e['ev'] == 'input' and e['id'] == 'rich']
    i = RICH[0].index('słowo')
    check([t.replace('\xa0', ' ') for t in inputs[:1]] == ['\n'.join([RICH[0][:i] + RICH[0][i + 5:], RICH[1]])]
          and inputs[-1] == got['text'], 'replace span is not minimal', inputs)


def chrome_mouse_selection_ok(r):
    c = r.browser()
    r.macro('f9', sync=False)
    r.calibrate()
    g = r.geometry()
    # Click into the contenteditable: the update that comes with a focus change is never trusted
    # (GTK sends its state from before the click), so a double-click right away is refused.
    r.s.click(*r.screen(g['ce']['Pierwszy']))
    r.e.wait_status(lambda st: st['client'] == 'gnome-shell' and st['text_len'], what='focus in ce')
    time.sleep(0.3)
    r.s.click(*r.screen(g['ce']['KOT']), n=2)
    lo = ce_off(1, 31)
    wait_until(lambda: sorted(r.chrome_text('ce')['sel']) == [lo, lo + 3], 3, what='double-click selection')
    time.sleep(0.3)
    r.refused(('stale',))
    # One arrow key later, mouse selections are trusted.
    r.s.tap('right')
    r.e.synced()
    r.s.click(*r.screen(g['ce']['KOT']), n=2)
    wait_until(lambda: sorted(r.chrome_text('ce')['sel']) == [lo, lo + 3], 3, what='double-click selection')
    r.e.synced()
    r.e.reply('kot')
    try:
        r.start('selection', 3)
        r.ready()
        r.accept()
    finally:
        r.e.reply(None)
    want = '\n'.join([CE[0], swap_word(CE[1], 'KOT', 'kot'), CE[2]])
    check(r.chrome_text('ce')['text'] == want, 'double-click selection replace', r.chrome_text('ce'))
    # Drag-select a word in the same field.
    x0, y0 = r.screen(g['ce']['słowo'], 0.0)
    x1, _ = r.screen(g['ce']['słowo'], 1.0)
    r.s.drag((x0 + 1, y0), (x1 - 1, y0))
    lo = ce_off(1, 25)
    wait_until(lambda: sorted(r.chrome_text('ce')['sel']) == [lo, lo + 5], 3, what='drag selection')
    r.e.synced()
    r.start('selection', 5)
    r.ready()
    r.accept()
    want = '\n'.join([CE[0], swap_word(swap_word(CE[1], 'KOT', 'kot'), 'słowo', 'SŁOWO'), CE[2]])
    check(r.chrome_text('ce')['text'] == want, 'drag selection replace', r.chrome_text('ce'))
    # Textarea: click in, one arrow key, then double-click a word.
    r.macro('f9', sync=False)
    g = r.geometry()
    r.s.click(*r.screen(g['ta']['Pierwszy']))
    r.e.wait_status(lambda st: st['client'] == 'gnome-shell' and st['text_len'], what='focus in ta')
    r.s.tap('right')
    r.e.synced()
    r.s.click(*r.screen(g['ta']['KOT']), n=2)
    wait_until(lambda: r.chrome_text('ta')['sel'][:2] == [KOT, KOT + 3], 3, what='textarea double-click')
    r.e.synced()
    r.e.reply('kot')
    try:
        r.start('selection', 3)
        r.ready()
        r.accept()
    finally:
        r.e.reply(None)
    check(r.chrome_text('ta')['text'] == swap_word(TV, 'KOT', 'kot'), 'textarea selection replace')
    r.macro('f4', shift=True)


def chrome_long_text_window(r):
    """Critique L2: Chrome sends 4000 bytes around the caret. A paragraph inside the window is
    replaced exactly and verified although the window re-centres on the new caret; the cut first
    and last paragraphs of a window are refused as truncated."""
    c = r.browser()
    r.macro('f9', sync=False)
    r.macro('f9', shift=True)  # 60 paragraphs in ta, caret in paragraph 30
    st = r.e.status()
    check(st['text_len'] and st['text_len'] < len(LONG), 'Chrome did not trim the text', st)
    lines = LONG.split('\n')
    r.e.reply(LONG_REPLY)
    try:
        r.start('paragraph', len(lines[30]))
        r.ready()
        r.accept()
    finally:
        r.e.reply(None)
    want = '\n'.join(lines[:30] + [LONG_REPLY] + lines[31:])
    check(r.chrome_text('ta')['text'] == want, 'only paragraph 30 may change', r.chrome_text('ta')['text'][:200])
    for name, combo in (('first', ('ctrl', 'home')), ('last', ('ctrl', 'end'))):
        r.s.combo(*combo)
        r.e.synced()
        r.refused(('truncated',), app=c)
        note('%s paragraph of the window: refused truncated' % name)
    got = r.chrome_text('ta')['text']
    check(got == '\n' + want + '\n', 'only the app\'s own Enter may change the text', got[:80], got[-80:])
    r.macro('f9')


def gtk3_selection_refused_paragraph_ok(r):
    win = r.gtk(3)
    r.gtk_field(win, 'tv', TV, P2)
    for _ in range(5):
        r.s.tap('right', ['lshift'])
    wait_until(lambda: sorted(win.get('tv')['sel']) == [P2, P2 + 5], 3, what='keyboard selection')
    time.sleep(0.3)
    r.refused(('hidden_selection',))
    time.sleep(0.3)
    check(win.text('tv') == TV and sorted(win.get('tv')['sel']) == [P2, P2 + 5], 'text changed after refusal')
    # Right only collapses the selection: GTK3 then pushes the very state the engine already has,
    # which ibus-daemon drops, so the engine cannot tell it from an unreported change. A second
    # arrow key moves the caret and is pushed.
    r.s.tap('right')
    r.s.tap('right')
    wait_until(lambda: win.get('tv')['sel'] == [P2 + 6] * 2, 3, what='caret in the paragraph')
    r.e.synced()
    para = TV.split('\n')[1]
    r.start('paragraph', len(para))
    r.ready()
    r.accept()
    win.wait_text('tv', upper_line(TV, 1))


def text_editor_ctrl_a_refused(r):
    """Critique C2's second fixture: gnome-text-editor (GTK4, GtkSourceView) never reports Ctrl+A."""
    r.idle()
    ed = r.s.spawn_text_editor('gd-fixture.txt', TV)
    try:
        r.s.combo('ctrl', 'home')  # keys that move the caret wherever the editor put it
        r.s.tap('down')
        r.e.synced()
        r.s.combo('ctrl', 'a')
        time.sleep(0.4)
        reason = r.refused(('stale', 'hidden_selection', 'selection_unknown'))
        r.s.tap('right')
        # GtkSourceView saves with an implicit trailing newline; anything else would be a change.
        check(ed.save() in (TV, TV + '\n'), 'the document changed', ed.read()[:200])
        note('gnome-text-editor ctrl+a refused with %s' % reason)
    finally:
        ed.kill()


def text_editor_window_switch(r):
    """2026-10-07: back in gnome-text-editor from a window that sends no text (Chrome before its first change),
    GTK4 sends its unchanged state and ibus-daemon drops it as identical to what it last sent the engine. The
    hotkey used to be refused as stale; the engine now keeps that text untrusted and AT-SPI confirms the caret."""
    r.idle()
    r.browser()
    ed = r.s.spawn_text_editor('gd-switch.txt', TV)
    try:
        r.s.combo('ctrl', 'home')
        r.s.tap('down')  # the caret in the second paragraph, the engine in sync with it
        r.e.synced()
        r.s.activate_window('GD fixture')
        time.sleep(0.5)
        n = r.e.mark()
        r.s.activate_window('gd-switch.txt')
        time.sleep(0.5)
        ev = r.hotkey()
        check(ev['ev'] == 'start' and ev['chars'] == len(TV.split('\n')[1]), 'hotkey after the window switch', ev,
              r.e.status())
        check([e for e in r.e.events(n) if e['ev'] == 'surrounding_unchanged'],
              'no dropped update: this scenario no longer covers it', r.e.events(n))
        r.cancel()
        check(ed.save() in (TV, TV + '\n'), 'the document changed', ed.read()[:200])
    finally:
        ed.kill()


def x11_no_text_refused(r):
    if r.chrome is not None:
        r.chrome.kill()
        r.chrome = None
    n = r.e.mark()
    c = r.s.spawn_chrome(r.s.fixture_url(focus='ta'), x11=True, name='chromex11')
    r.chrome = c
    focus = r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] not in ('fake', 'gnome-shell'), n, 10, 'X11 focus')
    time.sleep(0.5)
    st = r.e.status()
    note('XWayland Chrome: client %s, caps %s' % (st['client'], st['caps']))
    check(not st['caps'] & 32, 'XWayland client claims surrounding text', st)
    check(focus['client'].startswith(('gtk3-im:', 'xim')), 'unexpected X11 client', focus)
    m = c.mark()
    r.refused(('no_text',), app=c)
    keys = r.chrome_keys(c, m)
    check(not [k for k in keys if k[1] in ('g', 'G')], 'hotkey reached the page', keys)
    check(c.state()['ta']['text'] == TV + '\n', 'only the app\'s own Enter may change the text')


# ---------------------------------------------------------------------------------------------
# isolated sessions
# ---------------------------------------------------------------------------------------------
def crash_typing_continues(s):
    e = Engine(s)
    win = s.spawn_gtk(4)
    win.set_text('tv', '')
    s.type_text(CRASH_BEFORE)
    win.wait_text('tv', CRASH_BEFORE)
    # Fault injection (test-only SetKeyDelay): a slow key handler delays keys, never drops or reorders them.
    e.call('SetKeyDelay', '(d)', [0.05])
    try:
        s.type_text(DELAYED)
        win.wait_text('tv', CRASH_BEFORE + DELAYED, timeout=10)
        slowest = e.status()['keys']['passed']['max_us']
        check(slowest >= 50000, 'SetKeyDelay had no effect', slowest)
    finally:
        e.call('SetKeyDelay', '(d)', [0.0])
    pids = s.engine_pids()
    check(pids, 'no engine process', s.pids())
    check(e.status()['pid'] in pids, 'diagnostics do not come from the engine process')
    s.kill_pids(pids, signal.SIGKILL)
    wait_until(lambda: not s.engine_pids(), 5, what='engine gone')
    time.sleep(0.5)
    s.type_text(CRASH_AFTER)
    win.wait_text('tv', CRASH_BEFORE + DELAYED + CRASH_AFTER, timeout=10)
    note('after kill -9: ibus engine = %s' % s.ibus_engine_status()['out'])


def latency_window(s):
    win = s.spawn_gtk(4)
    win.set_text('tv', '')
    s.type_text('rozgrzewka ')  # warm-up
    win.wait_text('tv', 'rozgrzewka ')
    time.sleep(0.5)
    return win


def measure_latency(s, win):
    """Injection -> GTK4 buffer change, in ms, for LATENCY_KEYS plain letters."""
    s.activate_window(win.title)
    win.set_text('tv', '')
    time.sleep(0.2)
    letters = ('zazolcgeslajazn' * 4)[:LATENCY_KEYS]
    seq = []
    for ch in letters:
        seq += K.tap(ch) + [['sleep', 40]]
    m = win.mark()
    presses = s.keys(seq, gap_ms=5)[0::2]
    changes = wait_until(lambda: (lambda c: c if len(c) >= LATENCY_KEYS else None)(
        [e['t'] for e in win.events(m) if e['ev'] == 'changed' and e['w'] == 'tv' and e['text']]), 10,
        what='all keys typed')
    check(win.text('tv') == letters, 'typed text differs', win.text('tv'))
    return [(c - p) * 1000 for p, c in zip(presses, changes)]


def shift_latency(s, win, n=DIFF_TAPS):
    """Injection -> the window's capture-phase Shift_L press, in ms (a modifier never ends a session)."""
    seq = []
    for _ in range(n):
        seq += K.tap('lshift') + [['sleep', 30]]
    m = win.mark()
    presses = s.keys(seq, gap_ms=5)[0::2]
    seen = wait_until(lambda: (lambda t: t if len(t) >= n else None)(
        [e['t'] for e in win.events(m) if e['ev'] == 'key' and e['phase'] == 'capture' and e['type'] == 'press'
         and e['key'] == 'Shift_L']), 10, what='all Shift presses')
    return [(t - p) * 1000 for p, t in zip(presses, seen)]


def latency_stats(lat):
    lat = sorted(lat)
    return statistics.median(lat), lat[int(len(lat) * 0.95)], lat[-1]


def latency_during_diff(s, win):
    """Review finding: the debug backend's pure-Python diff ran in the engine process, held the GIL
    and delayed keys by 10-75 ms, and a restarted session left the old diff running. Shift taps are
    timed while the backend child spins the CPU (GRAMADRAMA_DEBUG_BURN_MS: the diff itself is fast
    now) after a mode switch, against taps with no session."""
    r = Run(s)
    s.activate_window(win.title)
    win.focus('tv')
    win.set_text('tv', DIFF_PAR)
    win.select('tv', 9, 9)
    s.tap('right')
    r.e.synced()
    base = shift_latency(s, win)
    r.start('paragraph', len(DIFF_PAR))
    time.sleep(0.45)  # the first diff has started
    n = r.e.mark()
    s.combo('ctrl', '2')
    r.e.wait(lambda e: e['ev'] == 'start' and e['mode'] == 'improve', n, what='restart in mode 2')
    time.sleep(0.4)  # the second diff has started
    keys = r.e.status()['keys']['n']
    loaded = shift_latency(s, win)
    st = r.e.status()
    r.cancel()
    check(st['session'] == 'waiting', 'the diff finished before the taps: nothing measured', st['session'])
    check(st['keys']['n'] >= keys + 2 * DIFF_TAPS, 'the taps did not go through the engine', st['keys'])
    check(win.text('tv') == DIFF_PAR, 'text changed')
    return base, loaded


def electron(s, found):
    """A separate ChatGPT instance with an isolated profile, never the user's. Without an account
    the app shows a login screen: its second button opens the local "OpenAI API key" form, whose
    field is the only text input. Nothing is ever typed there (a key field may be checked over the
    network); only the engine's view of the field and the hotkey's refusal are recorded."""
    e = Engine(s)
    for label, extra in (('default', []), ('wayland', ['--ozone-platform=wayland'])):
        name = 'chatgpt' + label[:3]
        n = e.mark()
        # --password-store=basic: no Secret Service probe (it opens GNOME's keyring prompt).
        p = s.spawn_app([CHATGPT, '--user-data-dir=%s/%s' % (s.work, name), '--password-store=basic',
                         '--no-first-run'] + extra, name=name)
        f = found[label] = {}
        try:
            win = wait_until(lambda: next((w for w in s.windows() if 'chatgpt' in (w['wm_class'] or '').lower()), None),
                             40, what='ChatGPT window')
            f['window'] = 'XWayland' if win['x11'] else 'Wayland'
            time.sleep(8)  # login screen
            s.shot(os.path.join(s.logs, 'chatgpt-%s-login.png' % label))
            fx, fy, fw, fh = s.window(win['title'])['frame']  # after the app's own resize
            s.click(fx + fw // 2, fy + fh // 2 + 53)  # second login button: API key form
            try:
                focus = e.wait(lambda ev: ev['ev'] == 'focus_in' and ev['client'] not in ('fake',), n, 8, 'field focus')
            except TimeoutError:
                s.shot(os.path.join(s.logs, 'chatgpt-%s-nofield.png' % label))
                f['field'] = 'no text field focused'
                continue
            time.sleep(0.5)
            st = e.status()
            f.update(client=focus['client'], caps=st['caps'], purpose=st['purpose'],
                     surrounding_text=st['text_len'] is not None,
                     focused_wm_class=(s.focused_window() or {}).get('wm_class'))
            ev = Run(s).hotkey()
            f['hotkey'] = ev['ev'] + ':' + ev.get('reason', '')
            f['client_at_hotkey'] = e.status()['client']
            s.shot(os.path.join(s.logs, 'chatgpt-%s-hotkey.png' % label))
        finally:
            p.kill(signal.SIGTERM)
            time.sleep(1)
    return found


# ---------------------------------------------------------------------------------------------
# ---- the real backend (a child per correction) against a stub model server ----
# GRAMADRAMA_ROUTE_LOOPBACK: the stub servers on 127.0.0.1 stand for servers in the home network (core.route), so that the
# way through the gateway can be tried
BIELIK_ENV = dict(ENV, GRAMADRAMA_BACKEND='real', GRAMADRAMA_ROUTE_LOOPBACK='1')
P2_TEXT = TV.split('\n')[1]
BIELIK_FIX = P2_TEXT.replace('KOT', 'kot').replace('błąd', 'błędy')  # keeps the protected emoji
EN_ENTRY = 'Single-line field: a blade 😀 and the word PIES here.'
PL_ENTRY = 'Pole jednoliniowe: źdźbło 😀 oraz słowo PIES tutaj.'
SWITCHES = 40  # password <-> text field round trips with two input sources


def write_settings(s, data):
    """settings.json in the session's own XDG_CONFIG_HOME (the engine's child reads it per job)."""
    path = os.path.join(s.env['XDG_CONFIG_HOME'], 'gramadrama', 'settings.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f)


def bielik_stream_apply(r, stub):
    check(r.e.status().get('backend') == 'real', 'the engine does not use the real backend', r.e.status())
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    stub.chunks, stub.chunk_delay = stream(BIELIK_FIX, pieces=6), 0.25
    n = len(stub.chats())
    try:
        r.start('paragraph', len(P2_TEXT))
        wait_until(lambda: (lambda p: p and p['visible'] and p['lines'] and
                            fallback_ui.STATUS['working'] in (p['aux'] or ''))(r.popup()), 5, what='streamed text in the popup')
        r.ready(timeout=10)
    finally:
        stub.chunk_delay = 0
    r.popup_with('Bielik-test · ', 'result source in the popup')  # the model's name, as on macOS
    chats = stub.chats()[n:]
    check(len(chats) == 1, 'expected one request to the model', len(chats))
    sent = chats[0]['messages'][-1]['content']
    others = [t for i, t in enumerate(TV.split('\n')) if i != 1]
    check(P2_TEXT in sent and not any(t in sent for t in others), 'more than the paragraph was sent', sent)
    r.accept()
    win.wait_text('tv', TV.replace(P2_TEXT, BIELIK_FIX))


def bielik_unreachable(r, stub):
    # No LanguageTool-only answer, as on macOS since 2026-09-29: the model's server is gone, so it says so.
    write_settings(r.s, stub.settings(ollama='http://127.0.0.1:%d' % closed_port()))
    try:
        win = r.gtk(4)
        r.gtk_field(win, 'tv', TV, KOT)
        off = len(P2_TEXT[:P2_TEXT.index('KOT')].encode('utf-16-le')) // 2  # LanguageTool counts UTF-16
        stub.lt_matches = [{'offset': off, 'length': 3, 'message': 'x', 'replacements': [{'value': 'kot'}]}]
        r.start('paragraph', len(P2_TEXT))
        r.e.wait_status(lambda st: st['session'] == 'error', 10, 'error with the model unreachable')
        r.popup_with('Nie można połączyć się z serwerem modelu (odmowa połączenia)', 'unreachable shown in the popup')
        r.cancel()
        check(win.text('tv') == TV, 'text changed')
    finally:
        stub.lt_matches = []
        write_settings(r.s, stub.settings())


def backend_killed_mid_stream(r, stub):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    stub.chunks, stub.chunk_delay = stream(BIELIK_FIX, pieces=10), 1.0
    engine = r.e.status()['pid']
    n = len(stub.chats())
    try:
        r.start('paragraph', len(P2_TEXT))
        wait_until(lambda: len(stub.chats()) > n, 5, what='request at the stub')
        children = r.s.pids(r'engine\.backend import child_main')
        check(len(children) == 1, 'expected one backend child', children)
        r.s.kill_pids(children, signal.SIGKILL)
        r.e.wait_status(lambda st: st['session'] == 'error', 5, 'error after the child died')
    finally:
        stub.chunk_delay = 0
    r.popup_with('Nie udało się przetworzyć tekstu', 'error shown in the popup')
    r.cancel()
    check(r.e.status()['pid'] == engine, 'the engine process restarted')
    check(win.text('tv') == TV, 'text changed')
    r.s.type_text(DELAYED)  # typing is unaffected; the caret is still before KOT
    win.wait_text('tv', TV[:KOT] + DELAYED + TV[KOT:])


def dictation_server(r, stub):
    """An address of a transcription server in settings.json replaces the built-in Whisper (G3): the recording goes
    there as a WAV, and the server's text, not the test service's, is inserted at the caret. The log says where it
    was recognised and holds no text."""
    stub.transcript = 'Tekst z serwera mowy.'
    write_settings(r.s, dict(stub.settings(), whisper_url=stub.url, whisper_model='whisper-test'))
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        win = r.gtk(4)
        r.gtk_field(win, 'tv', TV, KOT)
        n = r.e.mark()
        r.s.keys([['code', RCTRL, 1], ['sleep', 1500], ['code', RCTRL, 0]])  # the first dictation starts the service: 0.4 s before it listens
        ev = r.e.wait(lambda e: e['ev'] == 'dictation', n, 15, 'dictation outcome')
        check((ev['outcome'], ev['asr']) == ('inserted', 'server'), 'not recognised by the server', ev)
        check('serwera' not in json.dumps(ev) and DICTATED not in json.dumps(ev), 'the log holds text', ev)
        win.wait_text('tv', TV[:KOT] + 'Tekst z serwera mowy.' + TV[KOT:])
        request = stub.transcriptions()[-1]
        check((request['model'], request['file'][0], request['file'][1][:4]) == ('whisper-test', 'audio.wav', b'RIFF'),
              'the request to the server', request['model'], request['file'][0])
        check(request['prompt'].startswith('Mówię po polsku, czasem wtrącam słowa:'), 'the Polish prompt', request['prompt'])
    finally:
        write_settings(r.s, stub.settings())
        time.sleep(2.2)


def prompts_override(r, stub):
    """A prompt file in ~/.config/gramadrama/prompts replaces its piece for the next correction, with no restart
    (the backend child reads the files for each one); a broken file is ignored; deleting the file brings the
    default back (G5)."""
    directory = os.path.join(r.s.env['XDG_CONFIG_HOME'], 'gramadrama', 'prompts')
    task = os.path.join(directory, 'task-fix.txt')
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    stub.chunks = stream(BIELIK_FIX)
    pid = r.e.status()['pid']

    def system_of_next_correction():
        n = len(stub.chats())
        r.start('paragraph', len(P2_TEXT))
        r.ready(timeout=10)
        r.cancel()
        return stub.chats()[n]['messages'][0]['content']
    try:
        os.makedirs(directory, exist_ok=True)
        with open(task, 'w', encoding='utf-8') as f:
            f.write('Fix only the word KOT. MARKER-G5\n')
        system = system_of_next_correction()
        check('\nTask: Fix only the word KOT. MARKER-G5\nRules:\n' in system, 'the file was not used', system[:400])
        check(system.startswith('You are GramaDrama, a meticulous proofreader'), 'the rest of the prompt changed', system[:100])
        with open(os.path.join(directory, 'rules.txt'), 'w', encoding='utf-8') as f:
            f.write('   \n')  # an empty piece is no prompt: ignored, the default rules stay
        system = system_of_next_correction()
        check('MARKER-G5' in system and 'Rules:\n- Keep the language of the original' in system, 'a broken file was used', system[:600])
        os.remove(task)
        os.remove(os.path.join(directory, 'rules.txt'))
        system = system_of_next_correction()
        check('MARKER-G5' not in system and 'Task: Fix ONLY objective errors' in system, 'the default did not come back', system[:300])
        check(r.e.status()['pid'] == pid, 'the engine restarted', pid, r.e.status()['pid'])
        check(win.text('tv') == TV, 'a correction that was cancelled changed the text', win.text('tv'))
    finally:
        for name in ('task-fix.txt', 'rules.txt'):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def remote_login(r, stub):
    """A model server behind the login of a provider (oidc.py, docs/remote-access.md), as one out of the home network is:
    the stub wants the provider's token. The login is in auth.json with an access token that has run out: the
    engine's backend child refreshes it, once, and the correction goes through with the new token; the model and
    LanguageTool ask at the same time. Without a login nothing is sent and the popup says so."""
    auth = os.path.join(r.s.env['XDG_CONFIG_HOME'], 'gramadrama', 'auth.json')
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    stub.chunks = stream(BIELIK_FIX)
    stub.require_login = True
    old = stub.login(ttl=-5)
    write_settings(r.s, stub.settings(login=True))
    fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(old, f)
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        n, refreshes = len(stub.chats()), len(stub.token_requests)
        r.start('paragraph', len(P2_TEXT))
        r.ready(timeout=15)
        new = json.load(open(auth))
        check(new['access_token'] != old['access_token'] and new['refresh_token'] != old['refresh_token'],
              'the tokens were not renewed', list(new))
        check(len(stub.token_requests) == refreshes + 1,
              'the model and LanguageTool, asking together, refreshed more than once', stub.token_requests[refreshes:])
        check(os.stat(auth).st_mode & 0o777 == 0o600, 'auth.json is not private', oct(os.stat(auth).st_mode))
        check(len(stub.chats()) == n + 1, 'expected one request to the model', len(stub.chats()) - n)
        asked = [a for p, a in stub.headers_seen if p == '/api/chat'][-1]
        check(asked == 'Bearer ' + new['access_token'], 'the model was not asked with the new token', asked)
        check(all(a == 'Bearer ' + new['access_token'] for p, a in stub.headers_seen if p == '/v2/check' and a),
              'LanguageTool was asked with another token', [a for p, a in stub.headers_seen if p == '/v2/check'])
        check(new['access_token'] not in open(os.path.join(r.s.env['HOME'], '.cache', 'gramadrama', 'engine.log')).read()
              if os.path.exists(os.path.join(r.s.env['HOME'], '.cache', 'gramadrama', 'engine.log')) else True,
              'the token is in the engine log')
        r.accept()
        win.wait_text('tv', TV.replace(P2_TEXT, BIELIK_FIX))
        # logged out: not a single request to the model, and the popup says what to do
        os.remove(auth)
        win2 = r.gtk(4)
        r.gtk_field(win2, 'tv', TV, KOT)
        n = len(stub.chats())
        r.start('paragraph', len(P2_TEXT))
        r.e.wait_status(lambda st: st['session'] == 'error', 10, 'error without a login')
        r.popup_with('Nie zalogowano', 'the popup says to log in')
        r.cancel()
        check(len(stub.chats()) == n, 'a request reached the model without a login')
        check(win2.text('tv') == TV, 'the text changed')
    finally:
        stub.require_login = False
        try:
            os.remove(auth)
        except OSError:
            pass
        write_settings(r.s, stub.settings())
        time.sleep(2.2)


def remote_route(r, stub):
    """Away from home (core.route): nothing answers at the servers' own addresses, so a correction goes through the engine to
    the gateway (the login address's origin, /ollama) with the login's token (it had run out: refreshed once); LanguageTool
    goes its own way the same. With the login gone nothing is sent and the popup says what to do."""
    config = os.path.join(r.s.env['XDG_CONFIG_HOME'], 'gramadrama')
    auth = os.path.join(config, 'auth.json')
    route_file = os.path.join(r.s.env['XDG_CACHE_HOME'], 'gramadrama', 'route.json')
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    stub.chunks = stream(BIELIK_FIX)
    stub.require_login = True
    old = stub.login(ttl=-5)
    nothing = 'http://127.0.0.1:%d' % closed_port()
    write_settings(r.s, dict(stub.settings(login=True), llm_url=nothing, languagetool_url=nothing))
    fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(old, f)
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        n, refreshes = len(stub.chats()), len(stub.token_requests)
        r.start('paragraph', len(P2_TEXT))
        r.ready(timeout=20)
        check(len(stub.chats()) == n + 1, 'expected one request to the remote model', len(stub.chats()) - n)
        new = json.load(open(auth))
        check(len(stub.token_requests) == refreshes + 1 and new['access_token'] != old['access_token'], 'the token was not renewed once')
        asked = [a for p, a in stub.headers_seen if p == '/api/chat'][-1]
        check(asked == 'Bearer ' + new['access_token'], 'the remote model was not asked with the token', asked)
        check('/ollama/api/chat' in stub.gateway_seen, 'the model was not asked through the gateway', stub.gateway_seen[-3:])
        check(json.load(open(route_file))['answers'][nothing + '/api/version']['home'] is False, 'the route was not remembered',
              open(route_file).read())
        r.accept()
        win.wait_text('tv', TV.replace(P2_TEXT, BIELIK_FIX))
        # the login gone: away from home nothing can be asked, and the popup says what to do
        os.remove(auth)
        win2 = r.gtk(4)
        r.gtk_field(win2, 'tv', TV, KOT)
        n = len(stub.chats())
        r.start('paragraph', len(P2_TEXT))
        r.e.wait_status(lambda st: st['session'] == 'error', 10, 'error without a login away from home')
        r.popup_with('Zaloguj się w dostępie zdalnym', 'the popup says to log in')
        r.cancel()
        check(len(stub.chats()) == n, 'a request reached the model without a login')
    finally:
        stub.require_login = False
        for path in (auth, route_file):
            try:
                os.remove(path)
            except OSError:
                pass
        write_settings(r.s, stub.settings())
        time.sleep(2.2)


def translate_direction(r, stub):
    stub.reply = lambda req: stream(EN_ENTRY if req['messages'][-1]['content'].startswith('Translate into English')
                                    else PL_ENTRY)
    try:
        win = r.gtk(4)
        r.gtk_field(win, 'entry', ENTRY, len(ENTRY))
        r.start('paragraph', len(ENTRY), mode=5)
        r.ready(timeout=10)
        r.popup_with('[6 Tłumacz PL→EN]', 'direction shown in the popup')
        check(stub.chats()[-1]['messages'][-1]['content'].startswith('Translate into English.'), 'wrong direction')
        n = r.e.mark()
        r.s.combo('ctrl', '6')  # again: reverse the direction
        r.e.wait(lambda e: e['ev'] == 'start', n, what='restart in the other direction')
        r.ready(timeout=10)
        r.popup_with('[6 Tłumacz EN→PL]', 'reversed direction shown in the popup')
        check(stub.chats()[-1]['messages'][-1]['content'].startswith('Translate into Polish.'), 'not reversed')
        r.accept()
        win.wait_text('entry', PL_ENTRY)
    finally:
        stub.reply = None


def two_sources_password_switching(s):
    """[GramaDrama, Polski]: GNOME switches to the plain XKB source in every password field and back,
    re-creating the engine each time (the ibus#2957 crash path). Typing must survive every switch."""
    e = Engine(s)
    win = s.spawn_gtk(4)
    win.focus('tv')
    s.wait_ibus_engine('gramadrama')
    daemon = s.pids(r'(^|/)ibus-daemon( |$)')
    engine = s.engine_pids()
    check(len(daemon) == 1 and len(engine) == 1, 'unexpected processes', daemon, engine)
    win.set_text('tv', '')
    typed = ''
    for i in range(SWITCHES):
        win.focus('pw')
        s.type_text('x')
        win.focus('tv')
        s.type_text('ą' if i % 2 else 'ż')
        typed += 'ą' if i % 2 else 'ż'
    win.wait_text('tv', typed, timeout=10)
    created = sum(1 for ev in e.events() if ev['ev'] == 'create_engine')
    note('password round trips: %d; engine objects created: %d; ibus engine now: %s'
         % (SWITCHES, created, s.ibus_engine_status()['out']))
    check(created > SWITCHES, 'GNOME did not switch sources in the password field', created)
    check(s.pids(r'(^|/)ibus-daemon( |$)') == daemon, 'ibus-daemon crashed or restarted', daemon)
    check(s.engine_pids() == engine, 'the engine process restarted', engine, s.engine_pids())
    check(s.ibus_engine_status()['engine'] == 'gramadrama', 'GramaDrama is not active in the text field')
    check(e.status()['pid'] == engine[0], 'diagnostics do not answer')


BIELIK = [bielik_stream_apply, bielik_unreachable, backend_killed_mid_stream, translate_direction, dictation_server,
          prompts_override, remote_login, remote_route]

# ---- the real engine with the real GramaDrama panel (GNOME Shell extension) ----
PANEL_EXTENSION = os.path.join(os.path.dirname(HERE), 'gnome-extension')


def panel_state(r):
    return r.s.eval('gdGramaDramaPanel.state()')


def wait_panel(r, pred, what, timeout=5):
    return wait_until(lambda: (lambda st: st if pred(st) else None)(panel_state(r)), timeout, what=what)


def panel_session(r, want='ready', timeout=5):
    """Hotkey -> the panel (not the IBus popup) is on screen and the session is modal."""
    ev = r.hotkey()
    check(ev['ev'] == 'start', 'hotkey refused', ev, r.e.status())
    r.e.wait_status(lambda st: st['modal'] and st['ui'] == 'panel', timeout, 'modal session in the panel')
    st = wait_panel(r, lambda st: st['panel']['visible'] and st['panel']['status'] == want, 'panel %s' % want, timeout)
    check(not (r.popup() or {}).get('visible'), 'the IBus popup is shown next to the panel')
    check(st['stageKeyFocus'] is None and not st['grab'] and not st['panel']['hasKeyFocus'], 'the panel took focus', st)
    return st


def click(r, st, name):
    """Click a panel button once its geometry is stable: the panel grows (change list) and may move
    right after a result arrives, and a click at a stale position lands elsewhere."""
    last = [None]

    def stable():
        b = panel_state(r)['panel']['buttons'][name]
        ok = b['visible'] and b['reactive'] and b['rect'] and b['rect'] == last[0]
        last[0] = b['rect']
        return b if ok else None
    b = wait_until(stable, 3, 0.08, what='button %s settled' % name)
    x, y, w, h = b['rect']
    r.s.click(x + w // 2, y + h // 2)


def panel_enter_apply(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.reply(swap_word(P2_TEXT, 'KOT', 'PSA'))
    try:
        st = panel_session(r)
    finally:
        r.e.reply(None)
    check(st['panel']['badge']['text'] == '1 poprawka', 'badge', st['panel']['badge'])
    check(any('KOT' in row and 'PSA' in row for row in st['panel']['changes']), 'change list', st['panel']['changes'])
    check('~' not in st['panel']['text'] and 'PSA' in st['panel']['text'], 'diff text', st['panel']['text'])
    m = win.mark()
    r.s.tap('ret')
    win.wait_text('tv', swap_word(TV, 'KOT', 'PSA'))
    wait_panel(r, lambda st: st['toast']['visible'] and st['toast']['text'] == fallback_ui.message('applied')
               and not st['panel']['visible'], 'Zastosowano toast')
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] in ('Return', 'KP_Enter')], 'Enter reached the app', keys)


def panel_change_revert(r):
    """Cofnij and Zostaw i dodaj do słownika at a change of the panel's list (branding/PANEL.md, List of changes): the list
    is shown on demand (Ctrl+I), only that change goes back to the original, the other correction is inserted with Enter."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.reply(swap_word(swap_word(P2_TEXT, 'błąd', 'błędy'), 'KOT', 'PSA'))
    try:
        st = panel_session(r)
    finally:
        r.e.reply(None)
    check(not st['panel']['changesShown'] and st['panel']['buttons']['changes']['visible'],
          'the list waits behind Pokaż poprawki', st['panel']['buttons'].get('changes'))
    r.s.combo('ctrl', 'i')
    st = wait_panel(r, lambda st: st['panel']['changesShown'], 'the list shown with Ctrl+I')
    check(st['panel']['buttons']['changes']['label'].startswith('Ukryj zmiany'), 'the switch says Ukryj',
          st['panel']['buttons']['changes'])
    buttons = st['panel']['buttons']
    ids = sorted(int(name.split(':')[1]) for name in buttons if name.startswith('revert:'))
    check(len(ids) == 2 and all('keep:%d' % i in buttons for i in ids), 'Cofnij and the dictionary at both changes',
          sorted(buttons))
    if os.environ.get('GD_SHOTS'):  # to look at it: GD_SHOTS=DIR tests/integration_engine.py --only panel_change_revert
        r.s.shot(os.path.join(os.environ['GD_SHOTS'], 'panel-change-revert.png'))
    n = r.e.mark()
    click(r, st, 'keep:%d' % ids[0])  # błąd → błędy: the original word stays
    st = wait_panel(r, lambda st: len(st['panel']['changes']) == 1, 'one change left')
    check('PSA' in st['panel']['changes'][0] and 'błędy' not in st['panel']['text'], 'the change undone', st['panel'])
    check(st['panel']['badge']['text'] == '1 poprawka', 'count', st['panel']['badge'])
    reverted = [e for e in r.e.events(n) if e['ev'] == 'revert']
    check(len(reverted) == 1 and reverted[0]['keep'] is True and 'błąd' not in json.dumps(reverted), 'revert log', reverted)
    r.s.tap('ret')
    win.wait_text('tv', swap_word(TV, 'KOT', 'PSA'))


def panel_edit_result(r):
    """Editing the result in the panel (branding/PANEL.md, Edycja): Ctrl+E, typing with Polish letters, Up along the
    panel's own lines, Shift+Enter, Esc back to the result, Enter into the field. The result is shown as a field (no
    Edytuj), what the edited text changes stays marked; the panel never takes the keyboard, and no key of the editing
    reaches the app."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.reply(swap_word(P2_TEXT, 'KOT', 'PSA'))
    try:
        st = panel_session(r)
    finally:
        r.e.reply(None)
    check(st['panel']['field'] and st['panel']['fieldLabel'] and 'edit' not in st['panel']['buttons'],
          'the result is a field, no Edytuj', st['panel']['fieldLabel'], sorted(st['panel']['buttons']))
    m = win.mark()
    r.s.combo('ctrl', 'e')
    st = wait_panel(r, lambda st: st['panel']['editing'] and st['panel']['caret']['visible'], 'editing with a caret')
    check(st['stageKeyFocus'] is None and not st['panel']['hasKeyFocus'], 'the panel took the keyboard', st)
    added = ' Dziękuję, że czekasz na odpowiedź, zażółć gęślą jaźń i do jutra.'
    r.s.type_text(added)
    edited = swap_word(P2_TEXT, 'KOT', 'PSA') + added
    st = wait_panel(r, lambda st: st['panel']['text'] == edited, 'the typed text in the panel')
    marks = [edited[a:b] for a, b in st['panel']['editing']['marks']]
    check('PSA' in marks and st['panel']['fieldActive'], 'the correction and the typing marked, the field active',
          marks, st['panel']['fieldActive'])
    end = st['panel']['caret']['rect']
    text_left = st['panel']['textRect'][0]
    r.s.tap('up')  # one of the panel's lines up, in the same column: the engine has the panel's lines
    st = wait_panel(r, lambda st: (st['panel']['caret']['rect'] or end)[1] < end[1], 'the caret a line up')
    check(st['panel']['caret']['rect'][0] > text_left + 40, 'Up went to the start, not along the line', st['panel']['caret'])
    r.s.combo('ctrl', 'end')
    r.s.tap('ret', ['lshift'])
    r.s.type_text('Koniec')
    edited += '\nKoniec'
    wait_panel(r, lambda st: st['panel']['text'] == edited, 'a new line in the panel')
    if os.environ.get('GD_SHOTS'):  # to look at it: GD_SHOTS=DIR tests/integration_engine.py --only panel_edit_result
        r.s.shot(os.path.join(os.environ['GD_SHOTS'], 'panel-edit-result.png'))
    r.s.tap('esc')  # editing ends, the panel stays with the edited result and its changes against the original
    st = wait_panel(r, lambda st: st['panel']['editing'] is None and st['panel']['status'] == 'ready', 'the result again')
    check(any('PSA' in row for row in st['panel']['changes']) and len(st['panel']['changes']) >= 2, 'change list',
          st['panel']['changes'])
    r.s.tap('ret')
    win.wait_text('tv', TV.replace(P2_TEXT, edited))
    typed = [k for k in r.gtk_keys(win, m) if k[1] not in ('Control_L', 'Shift_L', 'ISO_Level3_Shift')]
    check(not typed, 'keys of the editing reached the app', typed)


def panel_edit_clipboard(r):
    """Ctrl+X and Ctrl+V in the panel's editor: the panel puts the cut text on the clipboard and reads it back for the
    paste (the engine has no clipboard on Wayland); Enter inserts the result."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.reply(swap_word(P2_TEXT, 'KOT', 'PSA'))
    try:
        panel_session(r)
    finally:
        r.e.reply(None)
    r.s.combo('ctrl', 'e')
    wait_panel(r, lambda st: st['panel']['editing'], 'editing')
    r.s.combo('ctrl', 'lshift', 'left')  # "zdania." is selected
    r.s.combo('ctrl', 'x')
    fixed = swap_word(P2_TEXT, 'KOT', 'PSA')
    rest = fixed[:-len('zdania.')]
    wait_panel(r, lambda st: st['panel']['text'] == rest, 'the word cut')
    got = r.s.eval('new Promise(r => imports.gi.St.Clipboard.get_default().get_text('
                   'imports.gi.St.ClipboardType.CLIPBOARD, (_c, t) => r(t)))')
    check(got == 'zdania.', 'the clipboard', got)
    r.s.combo('ctrl', 'home')
    r.s.combo('ctrl', 'v')
    edited = 'zdania.' + rest
    wait_panel(r, lambda st: st['panel']['text'] == edited, 'the word pasted at the start')
    r.s.tap('ret')
    win.wait_text('tv', TV.replace(P2_TEXT, edited))


def panel_click_into_result(r):
    """A click in the shown result starts editing there (macOS): the caret goes where the click was (the panel shows the
    result itself), a click while editing moves the caret. The pointer never takes the keyboard from the app."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.reply(swap_word(P2_TEXT, 'KOT', 'PSA'))
    try:
        st = panel_session(r)
    finally:
        r.e.reply(None)
    fixed = swap_word(P2_TEXT, 'KOT', 'PSA')
    m = win.mark()
    check(st['panel']['text'] == fixed, 'the result itself on screen', st['panel']['text'])
    r.s.click(*r.s.eval('gdGramaDramaPanel.textPoint(%d)' % (fixed.index('PSA') + 1)))  # on the "S"
    st = wait_panel(r, lambda st: st['panel']['editing'], 'editing after a click')
    check(st['panel']['editing']['cursor'] == fixed.index('PSA') + 1, 'the caret where the click was',
          st['panel']['editing'])
    check(st['stageKeyFocus'] is None and not st['grab'], 'the click took the keyboard', st)
    r.s.click(*r.s.eval('gdGramaDramaPanel.textPoint(%d)' % fixed.index('środku')))
    st = wait_panel(r, lambda st: st['panel']['editing']['cursor'] == fixed.index('środku'), 'the caret moved by a click')
    r.s.type_text('naprawdę ')
    edited = fixed.replace('środku', 'naprawdę środku')
    wait_panel(r, lambda st: st['panel']['text'] == edited, 'typed where the click was')
    r.s.tap('ret')
    win.wait_text('tv', TV.replace(P2_TEXT, edited))
    typed = [k for k in r.gtk_keys(win, m) if k[1] not in ('Shift_L', 'ISO_Level3_Shift')]
    check(not typed, 'keys of the editing reached the app', typed)


def panel_edit_check(r):
    """Checking while editing in the panel (macOS EditorChecker): the dictionary marks a word typed without its Polish
    letters with the brick wave, the line under the text shows it with suggestions once the caret is on it, and the
    first suggestion replaces it. Layer 0 only (the session's Enchant dictionaries; no server in test mode)."""
    write_settings(r.s, {'live_check': True})
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        win = r.gtk(4)
        r.gtk_field(win, 'tv', TV, KOT)
        r.e.reply(swap_word(P2_TEXT, 'KOT', 'PSA'))
        try:
            panel_session(r)
        finally:
            r.e.reply(None)
        r.s.combo('ctrl', 'e')
        wait_panel(r, lambda st: st['panel']['editing'], 'editing')
        r.s.type_text(' Dziekuje bardzo')
        st = wait_panel(r, lambda st: 'underline="error"' in st['panel']['markup'], 'the wave under the word', 10)
        check(st['panel']['checkBar'] and 'Do sprawdzenia' in st['panel']['checkBar'][0], 'the count under the text',
              st['panel']['checkBar'])
        r.s.combo('ctrl', 'left')
        r.s.combo('ctrl', 'left')  # the caret on "Dziekuje"
        st = wait_panel(r, lambda st: any(n.startswith('edit_fix:') for n in st['panel']['buttons']), 'its suggestions', 10)
        if os.environ.get('GD_SHOTS'):  # to look at it: GD_SHOTS=DIR tests/integration_engine.py --only panel_edit_check
            r.s.shot(os.path.join(os.environ['GD_SHOTS'], 'panel-edit-check.png'))
        first = next(n for n in st['panel']['buttons'] if n.startswith('edit_fix:') and n.endswith(':0'))
        suggestion = st['panel']['buttons'][first]['label']
        check(suggestion.startswith('Dzięk'), 'the first suggestion', st['panel']['buttons'][first])  # Enchant's order
        click(r, st, first)
        edited = swap_word(P2_TEXT, 'KOT', 'PSA') + ' %s bardzo' % suggestion
        wait_panel(r, lambda st: st['panel']['text'] == edited, 'the suggestion in the text')
        r.s.tap('ret')
        win.wait_text('tv', TV.replace(P2_TEXT, edited))
    finally:
        write_settings(r.s, {'live_check': False})
        time.sleep(2.2)


def panel_dictation_pill(r):
    """With the extension (version 5): the pill at the bottom while the right Ctrl is held, its bars
    moving with the level; then 'Rozpoznaję…', and gone once the text is at the caret. The panel session's
    test microphone wakes in PANEL_MIC_MS like a suspended USB one (version 14): until then the pill says
    'Budzę mikrofon…', and the microphone opened at the key-down, so the audio counts from then."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1]])
    try:
        st = wait_panel(r, lambda st: st['hud']['visible'] and st['hud']['state'] == 'listening', 'the pill')
        check(st['hud']['waiting'] and st['hud']['text'] == 'Budzę mikrofon…' and st['hud']['time'] is None,
              'the pill waits for the microphone', st['hud'])
        st = wait_panel(r, lambda st: st['hud']['visible'] and st['hud']['state'] == 'listening' and not st['hud']['waiting']
                        and st['hud']['time'] is not None and max(st['hud']['bars']) > 4, 'the pill listening')
        check(st['hud']['text'] == 'Słucham…' and not st['hud']['reactive'], 'the pill', st['hud'])
        check(not st['panel']['visible'], 'the correction panel came up', st['panel'])
        time.sleep(0.5)
    finally:
        r.s.keys([['code', RCTRL, 0]])
    ev = r.e.wait(lambda e: e['ev'] == 'dictation', n, 10, 'dictation outcome')
    check(ev['outcome'] == 'inserted', 'dictation not inserted', ev)
    check(ev['mic_ms'] >= PANEL_MIC_MS - 50, 'the microphone time in the log', ev)
    win.wait_text('tv', TV[:KOT] + DICTATED + TV[KOT:])
    wait_panel(r, lambda st: not st['hud']['visible'], 'the pill gone')
    check(not (r.popup() or {}).get('visible'), 'the IBus popup came up although the pill was there', r.popup())



def dictation_no_speech_model(r, state_path):
    """Without the speech tools and without a speech server (a session without GRAMADRAMA_DEBUG_SPEECH, whose
    XDG_DATA_HOME has no gramadrama-tools): holding the right Ctrl records nothing, the pill says „Brak modelu mowy.”
    whole, and the app opens on Usługi with the speech recognition group ringed, as on macOS (engine/dictate.py
    PointTo); the text of the field stays as it was."""
    write_settings(r.s, {'llm_local_url': 'http://127.0.0.1:%d' % closed_port(),
                         'languagetool_local_url': 'http://127.0.0.1:%d' % closed_port()})  # never the computer's own
    time.sleep(2.2)  # the engine reads settings.json at most every 2 s
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    n = r.e.mark()
    r.s.keys([['code', RCTRL, 1]])
    try:
        st = wait_panel(r, lambda st: st['hud']['visible'] and st['hud']['state'] == 'message', 'the pill')
        check(st['hud']['text'] == 'Brak modelu mowy.', 'the pill says why', st['hud'])
        time.sleep(0.3)  # laid out on the next frame
        check(not panel_state(r)['hud']['cut'], 'the message fits the pill', panel_state(r)['hud'])
    finally:
        r.s.keys([['code', RCTRL, 0]])

    def app():
        try:
            with open(state_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    state = wait_until(lambda: (lambda st: st if st and st.get('highlight') == 'speech' else None)(app()), 20,
                       what='the app on the speech recognition group')
    check(state['tab'] == 'connection', 'the app on Usługi', state['tab'])
    wait_until(lambda: r.s.window('GramaDrama'), 10, what='the app window on screen')
    # The group is below the fold of the window that has just opened: it scrolls up with its ring in view. The state is
    # written on request (the window action write-state), after the layout of the scroll.

    def in_view():
        r.s.call('pl.gramadrama.Linux', '/pl/gramadrama/Linux/window/1', 'org.gtk.Actions', 'Activate', '(sava{sv})',
                 ['write-state', [], {}])
        time.sleep(0.1)
        return (app() or {}).get('ring_in_view')
    wait_until(in_view, 5, what='the group and its ring scrolled into view')
    check(not [e for e in r.e.events(n) if e['ev'] == 'dictation'], 'something was recorded', r.e.events(n))
    check(win.text('tv') == TV, 'the field changed', win.text('tv'))


def panel_live_badge(r):
    """Live checking (docs/live-check.md): a text with mistakes brings the badge with their number, at the
    window's right edge; in its list the first suggestion fixes that word in place, and the count follows.
    Layer 0 only (the session's Enchant dictionaries; no server in test mode)."""
    write_settings(r.s, {'live_check': True})
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        win = r.gtk(4)
        text = 'Mysle ze to dziala dobrze.'
        r.gtk_field(win, 'tv', text, len(text))
        st = wait_panel(r, lambda st: st['live']['visible'] and int(st['live']['count'] or 0) >= 3
                        and st['live']['x'] is not None, 'the badge', 10)  # x is None until it is laid out
        badge = st['live']
        check(badge['reactive'] and not badge['focusable'], 'the badge takes clicks, never the focus', badge)
        window = st['focusWindow']
        r.s.pointer_to(badge['x'] + badge['width'] // 2, badge['y'] + badge['height'] // 2)
        st = wait_panel(r, lambda st: st['live']['list'] and st['live']['buttons']
                        and all(b['x'] is not None for b in st['live']['buttons']), 'the list on hover')
        first = next(b for b in st['live']['buttons'] if b['label'] == 'Myślę')
        count = int(st['live']['count'])
        r.s.click(first['x'] + first['width'] // 2, first['y'] + first['height'] // 2)
        win.wait_text('tv', 'Myślę ze to dziala dobrze.')
        wait_panel(r, lambda st: not st['live']['visible'] or int(st['live']['count'] or 0) < count, 'the count down')
        check(panel_state(r)['focusWindow'] == window, 'the app kept the focus', panel_state(r)['focusWindow'])
        ev = r.e.wait(lambda e: e['ev'] == 'live_fix' and e.get('outcome') == 'sent', 0, 5, 'live_fix logged')
        check('Mysle' not in json.dumps(ev), 'no text in the log', ev)
        r.e.wait(lambda e: e['ev'] == 'verified' and e.get('outcome') == 'ok', 0, 5, 'the fix verified')
    finally:
        write_settings(r.s, {'live_check': False})
        time.sleep(2.2)


def panel_live_underlines(r):
    """Live underlines (docs/live-check.md): in a GTK4 field the live service finds the words through AT-SPI,
    and the extension draws a wavy line under each mistake, inside the window, at the word. The card that
    opens on hover fixes the word, ignores one, and adds one to the dictionary (the session's Enchant list)."""
    write_settings(r.s, {'live_check': True})
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        win = r.gtk(4)
        text = 'Mysle ze to dziala dobrze.'
        r.gtk_field(win, 'tv', text, len(text))
        st = wait_panel(r, lambda st: len(st['underlines']['lines']) >= 3, 'the underlines', 10)
        u = st['underlines']
        check(u['inWindow'] and u['window'] == st['focusWindow']['id'], 'drawn inside the focused window', u)

        def line_at(start):
            return next((l for l in panel_state(r)['underlines']['lines'] if l['id'].startswith('%d:' % start)), None)

        (x0, middle), (x1, _) = win.locate(0), win.locate(5)
        line = line_at(0)
        check(line and abs(line['x'] - x0) <= 2 and abs(line['x'] + line['width'] - x1) <= 2
              and line['y'] <= middle <= line['y'] + line['height'], 'the line under "Mysle" is at the word', line, (x0, x1, middle))

        def card_for(start):
            line = wait_until(lambda: line_at(start), 5, what='the line at %d' % start)
            r.s.pointer_to(line['x'] + line['width'] // 2, line['y'] + line['height'] // 2)
            def placed(card):  # a button's position is None until the card is laid out
                return (card or {}).get('id', '').startswith('%d:' % start) and card.get('buttons') \
                    and all(b['x'] is not None for b in card['buttons'])
            return wait_panel(r, lambda st: placed(st['underlines']['card']),
                              'the card of the word at %d' % start)['underlines']['card']

        def press(card, label):
            button = next(b for b in card['buttons'] if b['label'] == label)
            r.s.click(button['x'] + button['width'] // 2, button['y'] + button['height'] // 2)

        press(card_for(0), 'Myślę')
        win.wait_text('tv', 'Myślę ze to dziala dobrze.')
        r.e.wait(lambda e: e['ev'] == 'live_fix' and e.get('outcome') == 'sent', 0, 5, 'live_fix logged')
        wait_panel(r, lambda st: not line_at(0), 'no line under the fixed word')
        check(panel_state(r)['focusWindow']['id'] == u['window'], 'the app kept the focus')
        card = card_for(12)  # "dziala"
        check('Dodaj do słownika' in [b['label'] for b in card['buttons']], 'a spelling card offers the dictionary', card)
        press(card, 'Ignoruj')
        r.e.wait(lambda e: e['ev'] == 'live_ignore', 0, 5, 'live_ignore logged')
        wait_panel(r, lambda st: not line_at(12), 'no line under the ignored word')
        press(card_for(6), 'Dodaj do słownika')  # "ze"
        ev = r.e.wait(lambda e: e['ev'] == 'live_learn', 0, 5, 'live_learn logged')
        check('ze' not in json.dumps(ev), 'no word in the log', ev)
        wait_panel(r, lambda st: not st['underlines']['lines'], 'no lines left')
        config = r.s.env.get('XDG_CONFIG_HOME') or os.path.join(r.s.env['HOME'], '.config')
        words = open(os.path.join(config, 'enchant', 'pl_PL.dic'), encoding='utf-8').read().split()
        check('ze' in words, "the word went into the session's own Enchant list", words)
    finally:
        r.s.pointer_to(1275, 795)
        write_settings(r.s, {'live_check': False})
        time.sleep(2.2)


def panel_live_long_words(r):
    """A long word with long suggestions (the card under the word and the badge's list): every suggestion is shown whole.
    Side by side they were squeezed to two or three letters and an ellipsis, and a word one sees the start of is a word
    one cannot judge. The suggestions then stand one under the other, the card grows, and a click on the second one fixes."""
    write_settings(r.s, {'live_check': True})
    suggestions = ['nieporozumienia', 'nieporozumienie', 'nieporozumiewanie']  # Enchant's, for "nieporozumiena"
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        win = r.gtk(4)
        text = 'Mam nieporozumiena w sprawie.'
        r.gtk_field(win, 'tv', text, len(text))
        st = wait_panel(r, lambda st: any(l['id'].startswith('4:') for l in st['underlines']['lines']), 'the underline', 10)
        line = next(l for l in st['underlines']['lines'] if l['id'].startswith('4:'))
        r.s.pointer_to(line['x'] + line['width'] // 2, line['y'] + line['height'] // 2)

        def placed(card):
            return (card or {}).get('id', '').startswith('4:') and card.get('buttons') \
                and all(b['x'] is not None for b in card['buttons'])
        card = wait_panel(r, lambda st: placed(st['underlines']['card']), 'the card of the long word')['underlines']['card']
        shots = os.environ.get('GD_SHOTS')  # to look at it: GD_SHOTS=DIR tests/integration_engine.py --only panel_live_long_words
        if shots:
            time.sleep(0.5)
            r.s.shot(os.path.join(shots, 'live-card-long-word.png'))
        fixes = [b for b in card['buttons'] if b['label'] in suggestions]
        check([b['label'] for b in fixes] == suggestions, 'the card offers the three suggestions', card['buttons'])
        check(not any(b['ellipsized'] for b in card['buttons']), 'a suggestion is cut to an ellipsis', card['buttons'])
        check(len({b['x'] for b in fixes}) == 1 and fixes[0]['y'] < fixes[1]['y'] < fixes[2]['y'],
              'too wide for one line: one under the other', fixes)
        check(card['width'] <= 480 and card['x'] >= 0, 'the card stays inside the screen and below its largest width', card)
        r.s.pointer_to(1275, 795)  # off the word: the card closes
        wait_panel(r, lambda st: not st['underlines']['card'], 'the card closed')
        # the badge's list: the same, for every issue it shows
        st = wait_panel(r, lambda st: st['live']['visible'] and st['live']['x'] is not None, 'the badge')
        r.s.pointer_to(st['live']['x'] + st['live']['width'] // 2, st['live']['y'] + st['live']['height'] // 2)
        st = wait_panel(r, lambda st: st['live']['list'] and st['live']['buttons']
                        and all(b['x'] is not None for b in st['live']['buttons']), 'the list on hover')
        if shots:
            time.sleep(0.5)
            r.s.shot(os.path.join(shots, 'live-list-long-word.png'))
        listed = [b for b in st['live']['buttons'] if b['label'] in suggestions]
        check([b['label'] for b in listed] == suggestions, 'the list offers the three suggestions', st['live']['buttons'])
        check(not any(b['ellipsized'] for b in st['live']['buttons']), 'a suggestion of the list is cut', st['live']['buttons'])
        # the second suggestion, not the first: a click on a stacked button fixes the word
        second = listed[1]
        r.s.click(second['x'] + second['width'] // 2, second['y'] + second['height'] // 2)
        win.wait_text('tv', 'Mam nieporozumienie w sprawie.')
        r.e.wait(lambda e: e['ev'] == 'live_fix' and e.get('outcome') == 'sent', 0, 5, 'live_fix logged')
    finally:
        r.s.pointer_to(1275, 795)
        write_settings(r.s, {'live_check': False})
        time.sleep(2.2)


def panel_card_after_a_narrow_one(r):
    """The card of a word with three suggestions, opened after the card of a word with none: each suggestion whole. The
    second card kept the first one's width (its natural width was the width set before) and showed "darm…" three times
    (the user's Gmail, 2026-10-02). The marks are put straight into the Shell's underlines: the layout is what is tested."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.s.pointer_to(1275, 795)
    js = """(() => {
      const u = Main.extensionManager.lookup('gramadrama@gramadrama.local').stateObj._underlines;
      const view = {toolkit: ['GTK', '4.0'], caret: null, clip: null, marks: [
        {id: '0:3:Y', rects: [[200, 60, 40, 24]], word: 'abc', message: '', suggestions: [], kind: 'grammar', source: 'Enchant', learn: false},
        {id: '0:9:X', rects: [[40, 60, 110, 24]], word: 'darmoszke', message: 'Wykryto prawdopodobny błąd pisowni',
         suggestions: ['darmoszkę', 'darmoszce', 'darmoszka'], kind: 'spelling', source: 'LanguageTool', learn: true}]};
      u.show(view, global.display.focus_window, null);
      u._openCard(u._lines.find(l => l.mark.id === '0:3:Y'));
      u._closeCard();
      u._openCard(u._lines.find(l => l.mark.id === '0:9:X'));
      return true;
    })()"""
    try:
        r.s.eval(js)
        card = wait_panel(r, lambda st: (st['underlines']['card'] or {}).get('id') == '0:9:X' and all(
            b['x'] is not None for b in st['underlines']['card']['buttons']), 'the second card laid out')['underlines']['card']
        cut = [b['label'] for b in card['buttons'] if b['ellipsized']]
        check(not cut, 'cut in the second card', cut, card)
        check(all(b['x'] + b['width'] <= card['x'] + card['width'] for b in card['buttons']), 'a button out of the card', card)
        check(not card['message'], "a spelling card repeats what its underline says", card)
        # Ignoruj and Dodaj do słownika are icons: their names come under them on hover
        for name in ('Ignoruj', 'Dodaj do słownika'):
            b = next(b for b in card['buttons'] if b['label'] == name)
            r.s.pointer_to(b['x'] + b['width'] // 2, b['y'] + b['height'] // 2)
            wait_panel(r, lambda st: (st['underlines']['card'] or {}).get('tip') == name, 'the name of %s on hover' % name)
            check(b['width'] < 40, 'an icon, not a word', b)
    finally:
        r.s.pointer_to(1275, 795)
        r.s.eval("(() => { const u = Main.extensionManager.lookup('gramadrama@gramadrama.local').stateObj._underlines;"
                 " u.hide(); return true; })()")


PAGE = '''<!doctype html><meta charset="utf-8"><title>GD browser live</title>
<body style="margin:0;background:#fff">
<div id="marker" style="position:absolute;left:10px;top:10px;width:24px;height:24px;background:#00ff00"></div>
<div id="ce" contenteditable="true" spellcheck="false" style="position:absolute;left:100px;top:120px;width:700px;height:60px;font:28px sans-serif;border:1px solid #888;padding:4px">Wysylam wiadomosc dzisiaj.</div>
'''  # words no other scenario ignores or adds to the dictionary of the live service


def colour_box(image, wanted, region=None):
    """The bounding box (x0, y0, x1, y1) of the pixels of a screenshot that `wanted(r, g, b)` accepts, within region; None if none."""
    width, height = image.size
    x0, y0, x1, y1 = region or (0, 0, width, height)
    pixels = image.load()
    box = None
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            r_, g_, b_ = pixels[x, y][:3]
            if wanted(r_, g_, b_):
                box = (x, y, x, y) if box is None else (min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y))
    return box


def panel_live_browser(r):
    """Live checking in a page of Chrome, through its extension (docs/browser.md): the text of a contenteditable comes from the
    extension, the live service finds the mistakes, the badge counts them and the Shell draws an underline under each word
    where the page has it: the page's origin in the screenshot is found by a green marker the page itself draws, so that the
    expected place does not depend on the engine's arithmetic. First in a window that is not maximized, whose shadow is
    between its frame and where Chrome counts from, then maximized. The badge stands inside the field's bottom right corner.
    Then a click on the suggestion of the card under a word replaces it in the page, through the extension (the page's own
    text is read back)."""
    from PIL import Image
    directory = os.path.join(r.s.work, 'browserpage')
    os.makedirs(directory)
    with open(os.path.join(directory, 'page.html'), 'w', encoding='utf-8') as f:
        f.write(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    write_settings(r.s, {'live_check': True})
    chrome = None
    try:
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        n = r.e.mark()
        chrome = r.s.spawn_chrome(url='http://127.0.0.1:%d/page.html' % server.server_port, app=False, wait=False,
                                  extension=os.path.join(REPO, 'browser', 'extension'), name='chromelive')
        r.e.wait(lambda e: e['ev'] == 'browser_connected', n, 40, 'the extension connects to the engine')
        wait_until(lambda: any('GD browser live' in (w['title'] or '') for w in r.s.windows()), 30, what='the page')
        r.s.activate_window('GD browser live')
        # Not maximized, as a window one has moved: Chrome's shadow is around its frame, and Chrome counts from the window's
        # client rect, shadow included (the Shell's `from: 'client'`). Mutter maximizes a window this large when it opens.
        find = 'global.get_window_actors().map(a => a.meta_window).find(w => (w.get_title() || "").includes("GD browser live"))'
        r.s.eval('(w => { w.unmaximize(); return true; })(%s)' % find)
        time.sleep(1)
        r.s.place_window('GD browser live', 60, 50, 1100, 700)
        window = r.s.window('GD browser live')
        check(window['buffer'][:2] != window['frame'][:2], 'a shadow around the window', window)
        time.sleep(1.5)
        shot = os.path.join(r.s.work, 'browser-live.png')

        def screenshot():
            r.s.shot(shot, cursor=False)
            return Image.open(shot).convert('RGB')

        def page_origin():
            """Where the page's (0, 0) is on the stage: its green marker is at (10, 10)."""
            green = colour_box(screenshot(), lambda R, G, B: G > 200 and R < 60 and B < 60)
            check(green is not None and green[2] - green[0] >= 20, 'the page is on screen', green)
            return green[0] - 10, green[1] - 10
        origin = page_origin()
        tab = chrome.eval_extension("(async () => (await chrome.tabs.query({})).find(t => (t.url || '').includes('page.html')).id)()")

        def page(expression):
            return chrome.eval_extension("(async () => (await chrome.scripting.executeScript({target: {tabId: %d}, func: () => { %s }}))[0].result)()"
                                         % (tab, expression))
        words = page("const node = document.getElementById('ce').firstChild, out = {};"
                     "for (const [w, a] of [['Wysylam', 0], ['wiadomosc', 8]]) { const q = document.createRange(); q.setStart(node, a);"
                     "q.setEnd(node, a + w.length); const b = q.getBoundingClientRect(); out[w] = [b.left, b.top, b.width, b.height]; } return out;")
        field = page("const b = document.getElementById('ce').getBoundingClientRect(); return [b.left, b.top, b.width, b.height];")

        def underlined(origin, what):
            """Each wrong word has its underline under it on the screen (pixels), and the badge is inside the field's corner."""
            want = {word: [origin[0] + box[0], origin[1] + box[1], box[2], box[3]] for word, box in words.items()}
            wait_panel(r, lambda st: len(st['underlines']['lines']) >= 2 and all(
                any(abs(line['x'] - x0) <= 2 and abs(line['y'] - y0) <= 2 for line in st['underlines']['lines'])
                for x0, y0, _, _ in want.values()), 'the underlines at the words (%s)' % what, 20)
            time.sleep(0.5)
            image = screenshot()
            for word, (x0, y0, width, height) in want.items():
                region = (int(x0) - 4, int(y0), int(x0 + width) + 4, int(y0 + height) + 14)  # not the next word's underline
                # brick #D07A52 (208, 122, 82), allowing for the wave's anti-aliasing
                brick = colour_box(image, lambda R, G, B: R > 170 and 80 < G < 150 and 40 < B < 120 and R - B > 80, region)
                check(brick is not None, 'no underline near %s (%s)' % (word, what), region)
                check(abs(brick[0] - x0) <= 4 and abs(brick[2] - (x0 + width)) <= 4 and y0 + height - 10 <= brick[3] <= y0 + height + 12,
                      'the underline of %s is not under it (%s)' % (word, what), (brick, (x0, y0, width, height)))
            right, bottom = origin[0] + field[0] + field[2] - 6, origin[1] + field[1] + field[3] - 6
            badge = wait_panel(r, lambda st: abs(st['live']['x'] + st['live']['width'] - right) <= 2
                               and abs(st['live']['y'] + st['live']['height'] - bottom) <= 2,
                               "the badge inside the field's bottom right corner (%s)" % what, 10)['live']
            check(badge['visible'] and int(badge['count']) >= 2, 'the badge (%s)' % what, badge)
        # the user's pointer in the page, then the field gets the focus (a click, as a user's)
        r.s.pointer_to(origin[0] + 600, origin[1] + 150)
        time.sleep(0.3)
        r.s.click(origin[0] + 600, origin[1] + 150)
        st = wait_panel(r, lambda st: st['live']['visible'] and int(st['live']['count'] or 0) >= 2, 'the badge for the page', 20)
        # two words are wrong in it: Wysylam and wiadomosc
        underlined(origin, 'not maximized')
        # maximized: no shadow, the page moves in the window; the pointer over the page again tells where it is now
        r.s.eval('(w => { w.maximize(); return true; })(%s)' % find)
        wait_until(lambda: (lambda w: w and w['buffer'] == w['frame'])(r.s.window('GD browser live')), 5, what='maximized')
        time.sleep(1)
        origin = page_origin()
        r.s.pointer_to(origin[0] + 600, origin[1] + 260)
        time.sleep(0.2)
        r.s.pointer_to(origin[0] + 610, origin[1] + 265)
        underlined(origin, 'maximized')
        # a click on the first suggestion in the card under the word: the extension replaces it in the page
        word = words['Wysylam']
        r.s.pointer_to(int(origin[0] + word[0] + word[2] / 2), int(origin[1] + word[1] + word[3] / 2))
        st = wait_panel(r, lambda st: (st['underlines']['card'] or {}).get('buttons')
                        and all(b['x'] is not None for b in st['underlines']['card']['buttons']), 'the card of the word', 10)
        button = next(b for b in st['underlines']['card']['buttons'] if b['label'] == 'Wysyłam')
        if os.environ.get('GD_SHOTS'):  # to look at it: GD_SHOTS=DIR
            time.sleep(0.5)
            r.s.shot(os.path.join(os.environ['GD_SHOTS'], 'live-browser-card.png'), cursor=True)
        n = r.e.mark()
        r.s.click(button['x'] + button['width'] // 2, button['y'] + button['height'] // 2)
        ev = r.e.wait(lambda e: e['ev'] == 'live_fix', n, 10, 'live_fix logged')
        check((ev['outcome'], ev.get('via')) == ('sent', 'extension'), 'the fix was not made through the extension', ev)
        wait_until(lambda: page("return document.getElementById('ce').innerText;") == 'Wysyłam wiadomosc dzisiaj.', 10,
                   what='the corrected text in the page')
        check('Wysylam' not in json.dumps(ev), 'a word is in the log', ev)
    finally:
        if chrome is not None:
            chrome.kill()
        server.shutdown()
        r.s.pointer_to(1275, 795)
        write_settings(r.s, {'live_check': False})
        time.sleep(2.2)


PAGE_TOP = """<!doctype html><meta charset="utf-8"><title>GD browser page</title>
<body style="margin:0;background:#fff;font:24px sans-serif">
<div id="marker" style="position:absolute;left:10px;top:10px;width:24px;height:24px;background:#00ff00"></div>
<textarea id="ta" spellcheck="false" style="position:absolute;left:100px;top:60px;width:700px;height:110px;font:20px sans-serif"></textarea>
<div id="ce" contenteditable="true" spellcheck="false" style="position:absolute;left:100px;top:200px;width:700px;height:40px;border:1px solid #888"></div>
<iframe id="fr" src="%s" style="position:absolute;left:130px;top:290px;width:640px;height:110px;border:7px solid #ccc;padding:5px"></iframe>
"""
PAGE_INNER = """<!doctype html><meta charset="utf-8"><title>inner</title>
<body contenteditable="true" spellcheck="false" style="margin:8px;font:28px sans-serif">Zrobilem to w ramce.</body>
"""  # a word no other scenario ignores, learns or fixes in the live service


def browser_page_hotkey_dictation(r):
    """Ctrl+Alt+G and dictation in a page of Chrome on Xwayland, where IBus gives no text at all (x11_no_text_refused): with
    the extension connected the page gives the caret's paragraph (`context`), and the correction goes back word by word
    (`replace`), keeping a link between two corrected words; dictated text goes in through `insert` with its space. Then live
    checking in a field inside a cross-origin <iframe> that the pointer never touched: its frame asks its parent where it is,
    and the underline lies under the word (docs/browser.md)."""
    directory = os.path.join(r.s.work, 'browserpage2')
    os.makedirs(directory)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_port
    with open(os.path.join(directory, 'inner.html'), 'w', encoding='utf-8') as f:
        f.write(PAGE_INNER)
    with open(os.path.join(directory, 'page.html'), 'w', encoding='utf-8') as f:
        f.write(PAGE_TOP % ('http://localhost:%d/inner.html' % port))  # another origin than the page's 127.0.0.1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if r.chrome is not None:
        r.chrome.kill()
        r.chrome = None
    chrome = None
    r.s.pointer_to(1275, 795)  # never over the inner frame
    try:
        n = r.e.mark()
        chrome = r.s.spawn_chrome(url='http://127.0.0.1:%d/page.html' % port, x11=True, app=False, wait=False,
                                  extension=os.path.join(REPO, 'browser', 'extension'), name='chromepage')
        r.e.wait(lambda e: e['ev'] == 'browser_connected', n, 40, 'the extension connects to the engine')
        wait_until(lambda: any('GD browser page' in (w['title'] or '') for w in r.s.windows()), 30, what='the page')
        r.s.activate_window('GD browser page')
        tab = chrome.eval_extension("(async () => (await chrome.tabs.query({})).find(t => (t.url || '').includes('page.html')).id)()")

        def page(expression, inner=False):
            target = '{tabId: %d, allFrames: true}' % tab if inner else '{tabId: %d}' % tab
            return chrome.eval_extension(
                "(async () => (await chrome.scripting.executeScript({target: %s, func: () => { %s }}))"
                ".map(r => r.result).find(v => v !== null && v !== undefined))()" % (target, expression))

        # 1. a textarea: the caret's paragraph, through the extension
        page("const t = document.getElementById('ta'); t.value = 'Pierwszy.\\nDrugi z bledem.\\nTrzeci.'; t.focus();"
             "t.setSelectionRange(14, 14); return true;")
        r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] not in ('fake', 'gnome-shell'), n, 15, 'X11 focus')
        app, _kind = engine_focus(r, 'x11')
        check(app == 'google-chrome.desktop', 'Chrome on Xwayland: its desktop ID', app)
        time.sleep(0.3)
        n = r.e.mark()
        r.e.reply('Drugi z błędem.')
        try:
            panel_session(r)
        finally:
            r.e.reply(None)
        start = r.e.wait(lambda e: e['ev'] == 'start', n, 2, 'start')
        check((start['scope'], start['chars']) == ('paragraph', len('Drugi z bledem.')), 'the paragraph of the page', start)
        asked = r.e.wait(lambda e: e['ev'] == 'browser_request' and e['kind'] == 'hotkey', n, 2, 'the page asked')
        check(asked['ok'] is True, 'the page did not answer', asked)
        n = r.e.mark()
        r.s.tap('ret')
        ev = r.e.wait(lambda e: e['ev'] == 'verified', n, 5, 'the correction written')
        check((ev['outcome'], ev.get('via')) == ('ok', 'extension'), 'not written through the extension', ev)
        check(page("return document.getElementById('ta').value;") == 'Pierwszy.\nDrugi z błędem.\nTrzeci.', 'the textarea',
              page("return document.getElementById('ta').value;"))
        wait_panel(r, lambda st: st['toast']['visible'] and st['toast']['text'] == fallback_ui.message('applied'), 'Zastosowano')

        # 2. a rich field: the link between two corrected words stays a link
        page("const c = document.getElementById('ce'); c.innerHTML = 'Wysylam <a href=\"#x\">raport</a> z bledami.'; c.focus();"
             "const s = getSelection(); s.collapse(c.lastChild, 3); return true;")
        time.sleep(0.3)
        r.e.reply('Wysyłam raport z błędami.')
        try:
            panel_session(r)
        finally:
            r.e.reply(None)
        n = r.e.mark()
        r.s.tap('ret')
        ev = r.e.wait(lambda e: e['ev'] == 'verified', n, 5, 'the correction written')
        check(ev['outcome'] == 'ok', 'the rich field', ev)
        html = page("return document.getElementById('ce').innerHTML;")
        check(html == 'Wysyłam <a href="#x">raport</a> z błędami.', 'the link or the words', html)

        # 3. dictation at the end of the textarea: after "Trzeci." with a space, through `insert`
        page("const t = document.getElementById('ta'); t.focus(); t.setSelectionRange(t.value.length, t.value.length); return true;")
        time.sleep(0.3)
        n = r.e.mark()
        r.s.keys([['code', RCTRL, 1], ['sleep', 1500], ['code', RCTRL, 0]])
        ev = r.e.wait(lambda e: e['ev'] == 'dictation', n, 15, 'dictation outcome')
        check((ev['outcome'], ev.get('via')) == ('inserted', 'extension'), 'not inserted through the extension', ev)
        wait_until(lambda: page("return document.getElementById('ta').value;") == 'Pierwszy.\nDrugi z błędem.\nTrzeci. ' + DICTATED,
                   5, what='the dictated text in the page')

        # 4. live checking inside the cross-origin frame, the pointer never over it
        write_settings(r.s, {'live_check': True})
        time.sleep(2.2)  # the engine reads settings.json at most every 2 s
        page("if (!location.href.includes('inner.html')) return null; document.body.focus();"
             "const t = document.body.firstChild; getSelection().collapse(t, t.length); return true;", inner=True)
        word = page("if (!location.href.includes('inner.html')) return null; const q = document.createRange();"
                    "q.setStart(document.body.firstChild, 0); q.setEnd(document.body.firstChild, 8);"
                    "const b = q.getBoundingClientRect(); return [b.left, b.top, b.width, b.height];", inner=True)
        frame = page("const f = document.getElementById('fr'), b = f.getBoundingClientRect(), s = getComputedStyle(f);"
                     "return [b.left + f.clientLeft + parseFloat(s.paddingLeft), b.top + f.clientTop + parseFloat(s.paddingTop)];")
        from PIL import Image
        shot = os.path.join(r.s.work, 'browser-page.png')
        r.s.shot(shot, cursor=False)
        green = colour_box(Image.open(shot).convert('RGB'), lambda R, G, B: G > 200 and R < 60 and B < 60)
        check(green is not None, 'the page is on screen')
        x0, y0 = green[0] - 10 + frame[0] + word[0], green[1] - 10 + frame[1] + word[1]
        # The pointer over the top page, never over the frame: the frame's place comes from its parents, the top page's from
        # the pointer (without any pointer over the page, a window that is not maximized puts the page lower by its shadow).
        r.s.pointer_to(green[0] + 400, green[1] + 20)
        time.sleep(0.2)
        r.s.pointer_to(green[0] + 410, green[1] + 25)
        wait_panel(r, lambda st: any(abs(line['x'] - x0) <= 3 and abs(line['y'] - y0) <= 3 for line in st['underlines']['lines']),
                   'the underline under the word in the frame', 20)
    finally:
        write_settings(r.s, {'live_check': False})
        if chrome is not None:
            chrome.kill()
        server.shutdown()
        r.s.pointer_to(1275, 795)
        time.sleep(2.2)


def panel_click_zastosuj_keeps_focus(r):
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    st = panel_session(r)
    window, ibus_out = st['focusWindow'], st['ibusFocus']['out']
    click(r, st, 'accept')
    lines = TV.split('\n')
    win.wait_text('tv', '\n'.join([lines[0], lines[1].upper(), lines[2]]))
    st = wait_panel(r, lambda st: not st['panel']['visible'], 'panel closed after the click')
    check(st['focusWindow'] == window and st['ibusFocus']['out'] == ibus_out, 'the click moved the focus', st)
    r.e.synced()  # the field is still the engine's focused, trusted field


def panel_click_kopiuj(r):
    win = r.gtk(4)
    r.gtk_field(win, 'entry', ENTRY, len(ENTRY))
    st = panel_session(r)
    click(r, st, 'copy')
    wait_panel(r, lambda st: st['toast']['visible'] and st['toast']['text'] == fallback_ui.message('copied'),
               'Skopiowano toast')
    got = r.s.eval('new Promise(r => imports.gi.St.Clipboard.get_default().get_text('
                   'imports.gi.St.ClipboardType.CLIPBOARD, (_c, t) => r(t)))')
    check(got == ENTRY.upper(), 'clipboard', got)
    check(win.text('entry') == ENTRY, 'Kopiuj changed the field')
    r.e.wait_status(lambda st: st['session'] == 'idle' and not st['modal'], what='session closed')


def panel_refusal_toast(r):
    win = r.gtk(4)
    win.focus('pw')
    time.sleep(0.5)
    n = r.e.mark()
    r.s.combo('ctrl', 'alt', 'g')
    ev = r.e.wait(lambda e: e['ev'] in ('start', 'refused'), n, what='hotkey decision')
    check(ev['ev'] == 'refused' and ev['reason'] == 'sensitive', 'password field', ev)
    wait_panel(r, lambda st: st['toast']['visible'] and st['toast']['text'] == fallback_ui.message('sensitive'),
               'refusal toast')
    check(not r.e.status()['modal'], 'a refusal is never modal')


def panel_translate_ctrl_d(r):
    win = r.gtk(4)
    r.gtk_field(win, 'entry', ENTRY, len(ENTRY))
    panel_session(r)
    n = r.e.mark()
    r.s.combo('ctrl', '6')
    r.e.wait(lambda e: e['ev'] == 'start' and e['mode'] == 'translate', n, what='translate')
    st = wait_panel(r, lambda st: st['panel']['status'] == 'ready' and st['panel']['buttons']['direction']['visible']
                    and 'PL → EN' in (st['panel']['buttons']['direction']['label'] or ''), 'PL → EN in the panel')
    n = r.e.mark()
    r.s.combo('ctrl', 'd')
    r.e.wait(lambda e: e['ev'] == 'start' and e['mode'] == 'translate', n, what='reversed by Ctrl+D')
    wait_panel(r, lambda st: 'EN → PL' in (st['panel']['buttons']['direction']['label'] or ''), 'EN → PL')
    n = r.e.mark()
    r.s.combo('ctrl', '1')  # back to Popraw while the panel is open (the mode is kept for the next session)
    r.e.wait(lambda e: e['ev'] == 'start' and e['mode'] == 'fix', n, what='back to Popraw')
    wait_panel(r, lambda st: not st['panel']['buttons']['direction']['visible'], 'direction button gone')
    r.cancel()
    check(win.text('entry') == ENTRY, 'text changed')


def panel_enter_while_streaming(r):
    """Enter while the panel streams: applied when ready; never delivered to the app, although every
    streamed update re-sends the panel (modality must not flicker between updates)."""
    win = r.gtk(4)
    r.gtk_field(win, 'tv', TV, KOT)
    r.e.delay(1500)
    try:
        panel_session(r, want='checking')
        check(r.e.status()['mode'] == 'fix', 'not in Popraw', r.e.status()['mode'])
        m = win.mark()
        r.s.tap('ret')
        wait_panel(r, lambda st: st['panel']['status'] == 'apply_pending', 'Zastosuję po zakończeniu')
        lines = TV.split('\n')
        win.wait_text('tv', '\n'.join([lines[0], lines[1].upper(), lines[2]]), timeout=6)
    finally:
        r.e.delay(None)
    keys = r.gtk_keys(win, m)
    check(not [k for k in keys if k[1] in ('Return', 'KP_Enter')], 'Enter reached the app', keys)


# ---- docs/apps.md: the focused app from the extension, apps.json and log attribution ----
OTHER_APP = 'org.gnome.Ptyxis.desktop'


def write_apps(s, disabled=(), raw=None, modes=None):
    """apps.json in the session's own XDG_CONFIG_HOME, written the way the app writes it: a temp file
    renamed over the old one."""
    path = os.path.join(s.env['XDG_CONFIG_HOME'], 'gramadrama', 'apps.json')
    check(path.startswith(s.work + '/'), 'apps.json outside the session', path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'wb') as f:
        content = {'v': 1, 'disabled': list(disabled), 'added': []}
        if modes:
            content['modes'] = modes
        f.write(raw if raw is not None else json.dumps(content).encode())
    os.replace(path + '.tmp', path)


def engine_apps(r, off, ok=True, timeout=10, modes=None):
    """Wait until the engine has read apps.json through its file monitor (a directory created after
    the engine started is noticed within a few seconds)."""
    want = {'off': sorted(off), 'ok': ok, 'modes': dict(modes or {})}
    return r.e.wait_status(lambda st: st['apps'] == want, timeout, 'apps.json read: %s' % want)


def engine_focus(r, kind, timeout=5):
    """(app, kind) of the focused window, once the engine has it: the extension's Focus signal travels
    apart from IBus's focus-in."""
    def same():
        focus = panel_state(r)['focus'] or {}
        want = [focus.get('app'), focus.get('kind')]
        return tuple(want) if want[1] == kind and r.e.status()['focus'] == want else None
    return wait_until(same, timeout, what='the engine knows the %s focus' % kind)


def apps_attribution(r):
    """start, apply and verified name the app and its window kind as GNOME Shell reported them at the
    hotkey (docs/apps.md, section 3). Chrome --app on Wayland is a window-backed app."""
    check(r.e.status()['pid'] in r.s.engine_pids(), 'the engine is not the repo launcher', r.s.engine_pids())
    r.browser()
    app, kind = engine_focus(r, 'wayland')
    # window-backed, named by its WM_CLASS without the fixture's file path (policy.js windowId)
    check(app == 'window:chrome', 'Chrome --app on Wayland: window-backed, named by the browser alone', app)
    r.macro('f9', sync=False)
    r.macro('f3')
    n = r.e.mark()
    panel_session(r)
    r.s.tap('ret')
    ev = r.e.wait(lambda e: e['ev'] == 'verified', n, what='verified')
    check(ev['outcome'] == 'ok', 'apply not verified', ev)
    steps = [(e['ev'], e.get('app'), e.get('win')) for e in r.e.events(n) if e['ev'] in ('start', 'apply', 'verified')]
    check(steps == [(step, app, 'wayland') for step in ('start', 'apply', 'verified')], 'attribution', steps)
    check(r.chrome_text('ta')['text'] == upper_line(TV, 1), 'textarea', r.chrome_text('ta')['text'])
    note('Chrome on Wayland: %s' % app)


def apps_app_off(r):
    """Chrome's ID in apps.json (docs/apps.md, section 2): Ctrl+Alt+G reaches the page, press and
    release (the fixture logs them), no session starts, nothing is shown, and the engine logs refused
    app_off. Editing apps.json takes effect without an engine restart; a broken file disables nothing."""
    c = r.browser()
    app, _kind = engine_focus(r, 'wayland')
    pid = r.e.status()['pid']
    try:
        write_apps(r.s, [app, OTHER_APP])
        engine_apps(r, [app, OTHER_APP])
        r.macro('f9', sync=False)
        r.macro('f3')
        n, m = r.e.mark(), c.mark()
        r.s.combo('ctrl', 'alt', 'g')
        ev = r.e.wait(lambda e: e['ev'] in ('start', 'refused'), n, what='hotkey decision')
        check(ev['ev'] == 'refused' and (ev['at'], ev['reason'], ev['app'], ev['win']) == ('hotkey', 'app_off', app, 'wayland'),
              'expected refused app_off', ev)
        keys = r.chrome_keys(c, m)
        check(('keydown', 'g') in keys and ('keyup', 'g') in keys, 'Ctrl+Alt+G did not reach the page', keys)
        st, shown = r.e.status(), panel_state(r)
        check(st['session'] == 'idle' and not st['modal'], 'a session started', st)
        check(not shown['panel']['visible'] and not shown['toast']['visible'], 'something was shown',
              shown['panel']['visible'], shown['toast'])
        check(not [e for e in r.e.events(n) if e['ev'] == 'start'], 'a correction started')
        check(r.chrome_text('ta')['text'] == TV, 'the text changed')
        # Chrome back on: an edit of apps.json, the same engine process.
        write_apps(r.s, [OTHER_APP])
        engine_apps(r, [OTHER_APP])
        n = r.e.mark()
        panel_session(r)
        r.cancel()
        start = [e for e in r.e.events(n) if e['ev'] == 'start']
        check(start and start[0]['app'] == app, 'Chrome is on again', start)
        # Off again, then the file breaks: fail-open, Chrome is allowed.
        write_apps(r.s, [app])
        engine_apps(r, [app])
        n = r.e.mark()
        write_apps(r.s, raw=b'{"v": 1, "disabled": ["%s"' % app.encode())
        engine_apps(r, [], ok=False)
        r.e.wait(lambda e: e['ev'] == 'apps_config' and e['ok'] is False, n, what='apps_config ok false')
        panel_session(r)  # a broken apps.json disables nothing
        r.cancel()
        check(r.e.status()['pid'] == pid, 'the engine restarted', pid, r.e.status()['pid'])
        check(len([e for e in r.e.events(n) if e['ev'] == 'apps_config']) == 1, 'apps_config once per change',
              [e for e in r.e.events(n) if e['ev'] == 'apps_config'])
    finally:
        write_apps(r.s, [])
        engine_apps(r, [])


def apps_default_mode(r):
    """apps.json's "modes" (docs/apps.md, section 2): Ctrl+Alt+G starts in the mode set for the focused app,
    Popraw when there is none; Ctrl+Alt+J is Tłumacz in every app. A mode this build does not know and
    Tłumacz in the file mean Popraw, and a broken "modes" breaks the file (nothing changes). Editing the
    file takes effect without an engine restart."""
    r.browser()
    app, _kind = engine_focus(r, 'wayland')
    pid = r.e.status()['pid']

    def started(shortcut='g'):
        n = r.e.mark()
        r.s.combo('ctrl', 'alt', shortcut)
        r.e.wait(lambda e: e['ev'] == 'start', n, what='start with Ctrl+Alt+%s' % shortcut.upper())
        r.e.wait_status(lambda st: st['modal'] and st['ui'] == 'panel', 5, 'modal session in the panel')
        r.cancel()
        return [e for e in r.e.events(n) if e['ev'] == 'start'][0]
    try:
        r.macro('f9', sync=False)
        r.macro('f3')
        engine_apps(r, [])
        ev = started()
        check((ev['mode'], ev['app']) == ('fix', app), 'no entry: Popraw', ev)
        modes = {app: 'formal', OTHER_APP: 'shorter'}
        write_apps(r.s, modes=modes)
        engine_apps(r, [], modes=modes)
        ev = started()
        check((ev['mode'], ev['app']) == ('formal', app), 'Ctrl+Alt+G starts in the mode of the app', ev)
        ev = started('j')
        check(ev['mode'] == 'translate', 'Ctrl+Alt+J is Tłumacz whatever the mode of the app', ev)
        for name, content in (('a mode this build does not know', 'no-such-mode'), ('Tłumacz in the file', 'translate')):
            write_apps(r.s, modes={app: content})
            engine_apps(r, [], modes={app: content})
            check(started()['mode'] == 'fix', '%s means Popraw' % name)
        write_apps(r.s, raw=b'{"v": 1, "modes": ["%s"]}' % app.encode())  # "modes" is no object: the file is broken
        engine_apps(r, [], ok=False)
        check(started()['mode'] == 'fix', 'a broken file changes no mode')
        check(r.e.status()['pid'] == pid, 'the engine restarted', pid, r.e.status()['pid'])
    finally:
        write_apps(r.s, [])
        engine_apps(r, [])


def apps_x11_no_text(r):
    """Chrome on Xwayland shares no text: its refusal names Chrome's desktop ID and win x11, the
    evidence the app needs to offer the launch fix (docs/apps.md, sections 3 and 5)."""
    if r.chrome is not None:
        r.chrome.kill()
        r.chrome = None
    n = r.e.mark()
    c = r.s.spawn_chrome(r.s.fixture_url(focus='ta'), x11=True, name='appsx11')
    try:
        r.e.wait(lambda e: e['ev'] == 'focus_in' and e['client'] not in ('fake', 'gnome-shell'), n, 10, 'X11 focus')
        app, _kind = engine_focus(r, 'x11')
        check(app == 'google-chrome.desktop', 'Chrome on Xwayland: its desktop ID', app)
        time.sleep(0.5)
        ev = r.hotkey()
        check(ev['ev'] == 'refused' and (ev['reason'], ev['app'], ev['win']) == ('no_text', app, 'x11'),
              'expected refused no_text in x11', ev)
        wait_panel(r, lambda st: st['toast']['visible'] and st['toast']['text'] == fallback_ui.message('no_text'),
                   'no_text toast')
    finally:
        c.kill()


def apps_unknown_focus_allowed(r):
    """Without the extension (the main session has none) the focused app is unknown, which means
    allowed (docs/apps.md, section 2), even with the window's would-be ID in apps.json; the log says
    so with app and win null."""
    win = r.gtk(4)
    try:
        write_apps(r.s, ['window:org.gramadrama.TestWin4'])
        engine_apps(r, ['window:org.gramadrama.TestWin4'])
        check(r.e.status()['focus'] is None, 'a focus without the extension', r.e.status()['focus'])
        r.gtk_field(win, 'tv', TV, KOT)
        ev = r.start('paragraph')
        check((ev['app'], ev['win']) == (None, None), 'attribution without the extension', ev)
        r.cancel()
    finally:
        write_apps(r.s, [])
        engine_apps(r, [])


PANEL = [panel_enter_apply, panel_click_zastosuj_keeps_focus, panel_click_kopiuj, panel_refusal_toast,
         panel_translate_ctrl_d, panel_enter_while_streaming, apps_attribution, apps_app_off, apps_default_mode,
         apps_x11_no_text,
         panel_dictation_pill, panel_live_badge, panel_live_underlines, panel_live_long_words, panel_live_browser,
         browser_page_hotkey_dictation, panel_card_after_a_narrow_one, panel_change_revert, panel_edit_result,
         panel_edit_clipboard, panel_click_into_result,
         panel_edit_check]

MAIN = [typing_passthrough, gtk4_selection, gtk4_paragraph_min_span, gtk4_entry_field, gtk4_mouse_drag_refused,
        gtk4_ctrl_a_refused, session_keys, popup_clicks_and_paging, pointer_focus_refused,
        gtk3_selection_window_switch_refused, hotkey_consumed, translate_hotkey, dictation_hold, dictation_shift,
        dictation_shortcuts_and_cancel, first_focus_no_stale_reuse,
        chrome_textarea_input_contenteditable, chrome_rich_paragraph_preserves_markup, chrome_mouse_selection_ok,
        chrome_long_text_window, password_refused, gtk3_selection_refused_paragraph_ok, text_editor_ctrl_a_refused,
        text_editor_window_switch, x11_no_text_refused, apps_unknown_focus_allowed, browser_extension_connects]
ORDER = ['typing_passthrough', 'hotkey_consumed', 'gtk4_selection', 'gtk4_paragraph_min_span', 'gtk4_entry_field',
         'gtk4_mouse_drag_refused', 'gtk4_ctrl_a_refused', 'gtk3_selection_refused_paragraph_ok',
         'chrome_textarea_input_contenteditable', 'chrome_rich_paragraph_preserves_markup', 'chrome_mouse_selection_ok',
         'password_refused', 'session_keys', 'x11_no_text_refused', 'first_focus_no_stale_reuse',
         'crash_typing_continues', 'latency', 'electron_chatgpt',
         # more captures and refusals
         'pointer_focus_refused', 'gtk3_selection_window_switch_refused', 'popup_clicks_and_paging',
         'chrome_long_text_window', 'text_editor_ctrl_a_refused', 'latency_during_diff',
         'text_editor_window_switch',
         # the real backend against the stub server
         'bielik_stream_apply', 'bielik_unreachable', 'backend_killed_mid_stream', 'translate_direction', 'dictation_server', 'prompts_override', 'remote_login', 'remote_route',
         'two_sources_password_switching',
         # the GNOME Shell panel
         'panel_enter_apply', 'panel_click_zastosuj_keeps_focus', 'panel_click_kopiuj', 'panel_refusal_toast',
         'panel_translate_ctrl_d', 'panel_enter_while_streaming',
         # docs/apps.md
         'apps_attribution', 'apps_app_off', 'apps_default_mode', 'apps_x11_no_text', 'apps_unknown_focus_allowed', 'browser_extension_connects',
         # the translation shortcut (Ctrl+Alt+J)
         'translate_hotkey',
         # docs/dictation.md
         'dictation_hold', 'dictation_shift', 'dictation_shortcuts_and_cancel', 'panel_dictation_pill',
         'dictation_no_speech_model',
         # docs/live-check.md
         'panel_live_badge', 'panel_live_underlines', 'panel_live_long_words', 'panel_card_after_a_narrow_one', 'panel_live_browser', 'panel_change_revert', 'panel_edit_result', 'panel_edit_clipboard', 'panel_click_into_result', 'panel_edit_check',
         # docs/browser.md
         'browser_page_hotkey_dictation']
results = []


def report(name, status, detail=''):
    print('%s %s%s' % (status, name, (': ' + detail) if detail else ''), flush=True)
    results.append((name, status))


def fixture_strings():
    words = set()
    for text in [TV, ENTRY, PW, TYPED, GTK_OTHER, CRASH_BEFORE, CRASH_AFTER, DELAYED, LONG, LONG_REPLY, POPUP_PAR,
                 DIFF_PAR, BAD_REPLY, BIELIK_FIX, EN_ENTRY, PL_ENTRY] + CE + RICH:
        for variant in (text, text.upper(), text.swapcase()):
            words.update(w.strip('.,:—!?') for w in variant.split() if len(w.strip('.,:—!?')) >= 4)
    letters = 'zazolcgeslajazn'
    return sorted(words | {PW, 'KOT', 'kot', 'PSA', 'PIES', 'pies', 'wyraz', 'ąęź', 'ZŁY', 'rozgrzewka',
                           letters, (letters * 4)[:LATENCY_KEYS]})


def log_leaks(path, strings=None):
    """Fixture strings found in an engine log: raw, JSON-escaped, or in any decoded JSON string."""
    strings = strings or fixture_strings()
    leaks = set()
    for p in (path, path + '.1'):
        try:
            raw = open(p, encoding='utf-8').read()
        except OSError:
            continue
        for w in strings:
            if w in raw or json.dumps(w)[1:-1] in raw:
                leaks.add(w)
        for line in raw.splitlines():
            try:
                blob = json.dumps(json.loads(line), ensure_ascii=False)
            except ValueError:
                leaks.add('<not JSON: %d chars>' % len(line))
                continue
            leaks.update(w for w in strings if w in blob)
    return sorted(leaks)


def privacy(s, label):
    path = s.env['GRAMADRAMA_ENGINE_LOG']
    leaks = log_leaks(path)
    ok = os.path.exists(path) and not leaks
    report('log_privacy[%s]' % label, 'PASS' if ok else 'FAIL', '' if ok else 'fixture text in %s: %s' % (path, leaks))


def clean(s, label):
    left = s.leftovers or {}
    ok = s.leftovers is not None and not any(left.values()) and not os.path.exists(s.work)
    report('teardown[%s]' % label, 'PASS' if ok else 'FAIL', '' if ok else repr(left))


def session(args, label, sources=ENGINE):
    return Session(sources=sources, env=ENV, name=label[:10],
                   copy_logs_to=args.logs and os.path.join(args.logs, label), verbose=args.verbose)


def scenarios(args, s, r, todo, *extra):
    for fn in todo:
        t0 = time.time()
        try:
            fn(r, *extra)
            report(fn.__name__, 'PASS', '%.1fs' % (time.time() - t0))
        except Exception as exc:
            report(fn.__name__, 'FAIL', '%s: %s' % (type(exc).__name__, exc))
            if args.verbose:
                traceback.print_exc()
            try:
                s.shot(os.path.join(s.logs, 'fail-%s.png' % fn.__name__))
                note('engine status: %s' % json.dumps(r.e.status()))
                r.idle()
            except Exception:
                pass


def run_main(args, wanted):
    todo = [fn for fn in MAIN if fn.__name__ in wanted]
    if not todo:
        return
    s = session(args, 'main')
    try:
        with s:
            r = Run(s)
            scenarios(args, s, r, todo)
            note('gtk_field/macro needed %d extra caret nudge(s)' % r.nudges)
            note('engine key handler: %s' % json.dumps(r.e.status()['keys']))
            privacy(s, 'main')
    finally:
        clean(s, 'main')


def run_bielik(args, wanted):
    todo = [fn for fn in BIELIK if fn.__name__ in wanted]
    if not todo:
        return
    with Stub() as stub:
        s = Session(sources=ENGINE, env=BIELIK_ENV, name='bielik', verbose=args.verbose,
                    copy_logs_to=args.logs and os.path.join(args.logs, 'bielik'))
        try:
            with s:
                write_settings(s, stub.settings())  # use the stub server instead of relying on user configuration
                r = Run(s)
                scenarios(args, s, r, todo, stub)
                note('stub requests: %d' % len(stub.requests))
                privacy(s, 'bielik')
        finally:
            clean(s, 'bielik')


def run_panel(args, wanted):
    todo = [fn for fn in PANEL if fn.__name__ in wanted]
    if not todo:
        return
    s = Session(sources=ENGINE, env=dict(ENV, GRAMADRAMA_DEBUG_MIC_MS=str(PANEL_MIC_MS)), name='panel',
                extensions=[PANEL_EXTENSION], verbose=args.verbose,
                copy_logs_to=args.logs and os.path.join(args.logs, 'panel'))
    try:
        with s:
            r = Run(s)
            wait_until(lambda: r.s.eval('typeof gdGramaDramaPanel') == 'object', 10, what='panel extension enabled')
            scenarios(args, s, r, todo)
            privacy(s, 'panel')
    finally:
        clean(s, 'panel')


def run_two_sources(args):
    try:
        run_single(args, 'twosources', two_sources_password_switching, sources=[('ibus', 'gramadrama'), ('xkb', 'pl')])
        report('two_sources_password_switching', 'PASS')
    except Exception as exc:
        report('two_sources_password_switching', 'FAIL', '%s: %s' % (type(exc).__name__, exc))


def run_single(args, label, body, sources=ENGINE):
    s = session(args, label, sources)
    try:
        with s:
            out = body(s)
            if ('ibus', 'gramadrama') in sources:
                privacy(s, label)
            return out
    finally:
        clean(s, label)


def run_crash(args):
    try:
        run_single(args, 'crash', crash_typing_continues)
        report('crash_typing_continues', 'PASS')
    except Exception as exc:
        report('crash_typing_continues', 'FAIL', '%s: %s' % (type(exc).__name__, exc))



def run_no_speech_model(args):
    """dictation_no_speech_model in a session of its own: there the engine looks for the real speech tools, and the app
    it starts writes its state where the scenario reads it."""
    state_dir = tempfile.mkdtemp(prefix='gramadrama-app-state-')
    env = dict({k: v for k, v in ENV.items() if k != 'GRAMADRAMA_DEBUG_SPEECH'},
               GRAMADRAMA_APP_STATE=os.path.join(state_dir, 'state.json'))
    s = Session(sources=ENGINE, env=env, name='nospeech', extensions=[PANEL_EXTENSION], verbose=args.verbose,
                copy_logs_to=args.logs and os.path.join(args.logs, 'nospeech'))
    try:
        with s:
            r = Run(s)
            wait_until(lambda: r.s.eval('typeof gdGramaDramaPanel') == 'object', 10, what='panel extension enabled')
            scenarios(args, s, r, [dictation_no_speech_model], env['GRAMADRAMA_APP_STATE'])
            privacy(s, 'nospeech')
    finally:
        clean(s, 'nospeech')
        shutil.rmtree(state_dir, ignore_errors=True)

def run_latency(args, wanted, rounds=3):
    """Engine and stock sessions side by side, measured in alternating rounds of LATENCY_KEYS keys,
    so drift of the machine affects both alike; then keys during a long diff in the engine session."""
    eng_s = Session(sources=ENGINE, env=dict(ENV, GRAMADRAMA_DEBUG_BURN_MS=str(BURN_MS)), name='latency-en',
                    copy_logs_to=args.logs and os.path.join(args.logs, 'latency-engine'), verbose=args.verbose)
    stock_s = session(args, 'latency-stock', STOCK)
    try:
        with eng_s, stock_s:
            e = Engine(eng_s)
            wins = {'engine': (eng_s, latency_window(eng_s)), 'stock': (stock_s, latency_window(stock_s))}
            pid = e.status()['pid']
            if 'latency' in wanted:
                lat = {'engine': [], 'stock': []}
                for i in range(rounds):
                    for label in ('engine', 'stock'):
                        s, win = wins[label]
                        n = e.status()['keys']['n']
                        one = measure_latency(s, win)
                        lat[label] += one
                        if label == 'engine':  # the measured keys really went through the engine
                            st = e.status()
                            check(st['keys']['n'] >= n + 2 * LATENCY_KEYS, 'keys did not go through the engine', st['keys'])
                            check(st['pid'] == pid and pid in eng_s.engine_pids(), 'engine restarted', st['pid'], pid)
                        note('round %d %s: median %.2f ms, p95 %.2f ms, max %.2f ms' % ((i + 1, label) + latency_stats(one)))
                eng, stock = latency_stats(lat['engine'])[0], latency_stats(lat['stock'])[0]
                ok = eng <= stock + LATENCY_MARGIN_MS
                report('latency', 'PASS' if ok else 'FAIL', 'median over %d keys each: engine %.2f ms vs stock '
                       'xkb:pl::pol %.2f ms (limit +%.1f ms)' % (len(lat['engine']), eng, stock, LATENCY_MARGIN_MS))
            if 'latency_during_diff' in wanted:
                try:
                    base, loaded = latency_during_diff(eng_s, wins['engine'][1])
                    (bm, bp, bx), (lm, lp, lx) = latency_stats(base), latency_stats(loaded)
                    ok = lm <= bm + LATENCY_MARGIN_MS and lp <= bp + DIFF_P95_MARGIN_MS
                    report('latency_during_diff', 'PASS' if ok else 'FAIL', 'Shift press, %d taps each: no session median '
                           '%.2f / p95 %.2f / max %.2f ms; during the diff %.2f / %.2f / %.2f ms (limits +%.1f / +%.1f ms)'
                           % (len(base), bm, bp, bx, lm, lp, lx, LATENCY_MARGIN_MS, DIFF_P95_MARGIN_MS))
                except Exception as exc:
                    report('latency_during_diff', 'FAIL', '%s: %s' % (type(exc).__name__, exc))
            privacy(eng_s, 'latency-engine')
    except Exception as exc:
        report('latency', 'FAIL', '%s: %s' % (type(exc).__name__, exc))
    finally:
        clean(eng_s, 'latency-engine')
        clean(stock_s, 'latency-stock')


def run_electron(args):
    if not os.access(CHATGPT, os.X_OK):
        report('electron_chatgpt', 'SKIP', '%s is not installed' % CHATGPT)
        return
    found = {}
    try:
        run_single(args, 'electron', lambda s: electron(s, found))
    except Exception as exc:
        report('electron_chatgpt', 'SKIP', 'could not complete (%s: %s); partial findings %s'
               % (type(exc).__name__, exc, json.dumps(found)))
        return
    for label, f in sorted(found.items()):
        note('ChatGPT %s: %s' % (label, json.dumps(f)))
    d, w = found.get('default', {}), found.get('wayland', {})
    if 'client' not in d or 'client' not in w:
        report('electron_chatgpt', 'SKIP', 'no focusable text field without an account: %s' % json.dumps(found))
        return
    problems = []
    if not (d['window'] == 'XWayland' and d['client'].startswith('gtk3-im:') and d['hotkey'] == 'refused:no_text'
            and 'chatgpt' in (d['focused_wm_class'] or '').lower()):
        problems.append('default launch: expected an XWayland ChatGPT window, a gtk3-im client and no_text')
    if not (w['window'] == 'Wayland' and w['client'] == w['client_at_hotkey'] == 'gnome-shell'
            and 'chatgpt' in (w['focused_wm_class'] or '').lower() and w['hotkey'].startswith('refused:')):
        problems.append('Wayland launch: expected ChatGPT focused over text-input-v3 and a refusal')
    if problems:
        report('electron_chatgpt', 'FAIL', '; '.join(problems) + ': ' + json.dumps(found))
    elif not w['surrounding_text']:
        # Safe outcomes hold, but the question the scenario exists for (does text arrive?) stays open.
        report('electron_chatgpt', 'SKIP', 'safe refusals confirmed (default: no_text over XWayland; Wayland: %s over '
               'text-input-v3), but no surrounding text arrived: nothing may be typed into the API-key field' % w['hotkey'])
    else:
        report('electron_chatgpt', 'PASS', json.dumps(found))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='comma-separated scenario names')
    ap.add_argument('--logs', help='copy session logs here (they contain fixture text)')
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    wanted = set(args.only.split(',')) if args.only else set(ORDER)
    unknown = wanted - set(ORDER)
    if unknown:
        ap.error('unknown scenario(s): %s' % ', '.join(sorted(unknown)))
    for i in range(args.repeat):
        t0 = time.time()
        run_main(args, wanted)
        run_bielik(args, wanted)
        run_panel(args, wanted)
        if 'two_sources_password_switching' in wanted:
            run_two_sources(args)
        if 'crash_typing_continues' in wanted:
            run_crash(args)
        if 'dictation_no_speech_model' in wanted:
            run_no_speech_model(args)
        if wanted & {'latency', 'latency_during_diff'}:
            run_latency(args, wanted)
        if 'electron_chatgpt' in wanted:
            run_electron(args)
        print('run %d finished in %.0fs' % (i + 1, time.time() - t0), flush=True)
    failed = [n for n, st in results if st == 'FAIL']
    print('INTEGRATION %s (%d results: %d passed, %d skipped, %d failed)' % (
        'FAIL' if failed else 'PASS', len(results), sum(st == 'PASS' for _, st in results),
        sum(st == 'SKIP' for _, st in results), len(failed)))
    if failed:
        print('failed: ' + ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
