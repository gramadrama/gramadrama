#!/usr/bin/env python3
"""GTK 3/4 test window with JSONL ground truth.

usage: testwin.py 3|4 LOGFILE [NAME]

Widgets: multi-paragraph TextView 'tv', Entry 'entry', password entry 'pw' (GTK4
PasswordEntry; GTK3 invisible Entry with purpose PASSWORD). LOGFILE receives one JSON
object per line: 'ready', buffer/selection changes ('changed'/'sel' with the full text and
[cursor, bound]), focus changes, and every key event that reaches the window: phase
'capture' (before the focused widget sees it) and 'bubble' (not handled by the widget or its
input method), type 'press'/'release'. D-Bus control: org.gramadrama.TestWin.NAME at
/org/gramadrama/TestWin (Focus, Select, SetText, Get, Info, Locate, Rect, Present, Quit).
"""
import json
import os
import sys
import time

import gi

GTKV, LOG = sys.argv[1], sys.argv[2]
NAME = sys.argv[3] if len(sys.argv) > 3 else 'gtk' + GTKV
gi.require_version('Gtk', '4.0' if GTKV == '4' else '3.0')
gi.require_version('Gdk', '4.0' if GTKV == '4' else '3.0')
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

TV_TEXT = ('Pierwszy akapit: zażółć gęślą jaźń.\n'
           'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.\n'
           'Trzeci akapit — koniec.')
ENTRY_TEXT = 'Pole jednoliniowe: źdźbło 😀 i słowo PIES tutaj.'
PW_TEXT = 'tajneHasło1'
BUS_NAME = 'org.gramadrama.TestWin.' + NAME
IFACE = """<node><interface name="org.gramadrama.TestWin">
<method name="Focus"><arg type="s" direction="in"/></method>
<method name="Select"><arg type="s" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SetText"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="Get"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Info"><arg type="s" direction="out"/></method>
<method name="Locate"><arg type="s" direction="in"/><arg type="i" direction="in"/><arg type="s" direction="out"/></method>
<method name="Rect"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Present"/>
<method name="Quit"/>
</interface></node>"""

fh = open(LOG, 'a', buffering=1, encoding='utf-8')


def log(**kw):
    kw['t'] = round(time.time(), 6)
    kw['gtk'] = GTKV
    fh.write(json.dumps(kw, ensure_ascii=False) + '\n')


class Win:
    def __init__(self, app):
        self.win = Gtk.ApplicationWindow(application=app, title='GD test GTK%s %s' % (GTKV, NAME))
        self.win.set_default_size(900, 600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.tv = Gtk.TextView()
        self.tv.set_wrap_mode(Gtk.WrapMode.WORD)
        self.tv.set_size_request(-1, 300)
        self.buf = self.tv.get_buffer()
        self.buf.set_text(TV_TEXT)
        self.buf.place_cursor(self.buf.get_start_iter())
        self.entry = Gtk.Entry()
        self.entry.set_text(ENTRY_TEXT)
        if GTKV == '4':
            self.pw = Gtk.PasswordEntry()
        else:
            self.pw = Gtk.Entry()
            self.pw.set_visibility(False)
            self.pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.pw.set_text(PW_TEXT)
        self.widgets = {'tv': self.tv, 'entry': self.entry, 'pw': self.pw}
        for w in (self.tv, self.entry, self.pw):
            if GTKV == '4':
                box.append(w)
            else:
                box.pack_start(w, w is self.tv, True, 0)
        (self.win.set_child if GTKV == '4' else self.win.add)(box)

        self.buf.connect('changed', lambda b: self.log_state('tv', 'changed'))
        self.buf.connect('mark-set', self.on_mark)
        for name in ('entry', 'pw'):
            w = self.widgets[name]
            w.connect('changed', lambda _w, n=name: self.log_state(n, 'changed'))
            w.connect('notify::cursor-position', lambda _w, _p, n=name: self.log_state(n, 'sel'))
            w.connect('notify::selection-bound', lambda _w, _p, n=name: self.log_state(n, 'sel'))
        if GTKV == '4':
            for phase, name in ((Gtk.PropagationPhase.CAPTURE, 'capture'), (Gtk.PropagationPhase.BUBBLE, 'bubble')):
                kc = Gtk.EventControllerKey()
                kc.set_propagation_phase(phase)
                kc.connect('key-pressed', lambda c, kv, code, st, p=name: self.on_key(p, 'press', kv, code, st))
                kc.connect('key-released', lambda c, kv, code, st, p=name: self.on_key(p, 'release', kv, code, st))
                self.win.add_controller(kc)
            self.win.connect('notify::focus-widget', lambda *_: self.log_focus())
            self.win.connect('notify::is-active', lambda *_: self.log_focus())
            self.win.present()
        else:
            # GTK3 delivers key events to the toplevel first: handlers connected before the
            # default handler see everything, connect_after sees what nobody handled.
            for sig, typ in (('key-press-event', 'press'), ('key-release-event', 'release')):
                self.win.connect(sig, lambda w, ev, t=typ: self.on_key('capture', t, ev.keyval, ev.hardware_keycode, ev.state))
                self.win.connect_after(sig, lambda w, ev, t=typ: self.on_key('bubble', t, ev.keyval, ev.hardware_keycode, ev.state))
            self.win.connect('set-focus', lambda *_: GLib.idle_add(self.log_focus))
            self.win.connect('notify::is-active', lambda *_: self.log_focus())
            self.win.show_all()
        self.tv.grab_focus()
        log(ev='ready', name=NAME, pid=os.getpid())

    def on_key(self, phase, typ, keyval, keycode, state):
        log(ev='key', phase=phase, type=typ, key=Gdk.keyval_name(keyval), keyval=int(keyval),
            keycode=int(keycode), state=int(state))
        return False

    def on_mark(self, buf, it, mark):
        if mark.get_name() in ('insert', 'selection_bound'):
            self.log_state('tv', 'sel')

    def focused(self):
        focus = self.win.get_focus()
        return next((n for n, w in self.widgets.items()
                     if focus is not None and (w is focus or focus.is_ancestor(w))), None)

    def log_focus(self):
        log(ev='focus', w=self.focused(), active=self.win.is_active())
        return False

    def text_of(self, name):
        if name == 'tv':
            b = self.buf
            text = b.get_text(b.get_start_iter(), b.get_end_iter(), True)
            ins = b.get_iter_at_mark(b.get_insert()).get_offset()
            sb = b.get_iter_at_mark(b.get_selection_bound()).get_offset()
            return text, [ins, sb]
        w = self.widgets[name]
        return w.get_text(), [w.get_property('cursor-position'), w.get_property('selection-bound')]

    def log_state(self, name, what):
        text, sel = self.text_of(name)
        log(ev=what, w=name, text=text, sel=sel)

    def to_surface(self, widget, x, y):
        """Widget coordinates -> coordinates in the toplevel surface (incl. CSD shadow)."""
        wx, wy = widget.translate_coordinates(self.win, x, y)[-2:]
        if GTKV == '4':
            sx, sy = self.win.get_surface_transform()
            return wx + sx, wy + sy
        return wx, wy

    def locate(self, name, offset):
        if name != 'tv':
            raise ValueError('Locate supports tv only; use Rect for %s' % name)
        it = self.buf.get_iter_at_offset(offset)
        loc = self.tv.get_iter_location(it)
        x, y = self.tv.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, loc.x, loc.y + loc.height // 2)
        sx, sy = self.to_surface(self.tv, x, y)
        return {'x': sx, 'y': sy, 'h': loc.height}

    def rect(self, name):
        w = self.widgets[name]
        a = w.get_allocation()
        x, y = self.to_surface(w, 0, 0)
        return {'x': x, 'y': y, 'w': a.width, 'h': a.height}

    def on_call(self, conn, sender, path, iface, method, params, inv):
        a = params.unpack()
        ret = None
        try:
            if method == 'Focus':
                self.widgets[a[0]].grab_focus()
            elif method == 'Select':
                name, s, e = a
                if name == 'tv':
                    self.buf.select_range(self.buf.get_iter_at_offset(e), self.buf.get_iter_at_offset(s))
                else:
                    self.widgets[name].select_region(s, e)
            elif method == 'SetText':
                name, t = a
                (self.buf if name == 'tv' else self.widgets[name]).set_text(t)
            elif method == 'Get':
                text, sel = self.text_of(a[0])
                ret = {'text': text, 'sel': sel}
            elif method == 'Info':
                im = Gtk.Settings.get_default().get_property('gtk-im-module')
                ret = {'focus': self.focused(), 'active': self.win.is_active(), 'gtk_im_module_setting': im,
                       'title': self.win.get_title()}
            elif method == 'Locate':
                ret = self.locate(*a)
            elif method == 'Rect':
                ret = self.rect(a[0])
            elif method == 'Present':
                self.win.present()
            elif method == 'Quit':
                GLib.idle_add(self.win.get_application().quit)
            inv.return_value(None if ret is None else GLib.Variant('(s)', (json.dumps(ret, ensure_ascii=False),)))
        except Exception as exc:
            inv.return_dbus_error('org.gramadrama.TestWin.Error', repr(exc))


def main():
    app = Gtk.Application(application_id='org.gramadrama.TestWin%s' % GTKV, flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder = {}

    def activate(app):
        w = holder['w'] = Win(app)
        conn = app.get_dbus_connection()
        info = Gio.DBusNodeInfo.new_for_xml(IFACE)
        conn.register_object('/org/gramadrama/TestWin', info.interfaces[0], w.on_call, None, None)
        Gio.bus_own_name_on_connection(conn, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

    app.connect('activate', activate)
    app.run([sys.argv[0]])


if __name__ == '__main__':
    main()
