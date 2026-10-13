"""IBus adapter: the only module of the engine package that imports gi.

It feeds IBus vfuncs into the pure EngineLogic (session.py) and executes the effects it returns,
in order. Every vfunc is wrapped: an exception is logged by type only, the session ends and the
key is chained up to IBus.EngineSimple, so typing behaves exactly as with the stock engine (unless
the key had already applied a correction: then it is consumed). The main loop does no file I/O:
log lines go to a writer thread, apps.json is read by a thread, and UI updates are drawn from
idle callbacks."""
from ..i18n import tr, set_language, interface_language
import collections
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback

import gi

gi.require_version('IBus', '1.0')
from gi.repository import Gio, GLib, IBus  # noqa: E402

from .. import paths  # noqa: E402  (never core: install_engine.engine_digest)
from . import (applist, backend, browser, browser_live, browser_text as BT, dictate, fallback_ui, keys, live,  # noqa: E402
               session as S, shortcuts, speech)

ENGINE_NAME = 'gramadrama'
BUS_NAME = 'org.freedesktop.IBus.GramaDrama'
DIAG_NAME = 'pl.gramadrama.Engine'
DIAG_PATH = '/pl/gramadrama/Engine'
LOG_MAX_BYTES = 1 << 20
LOG_QUEUE = 1000  # lines waiting for the writer thread; more are dropped
MAX_KEY_DELAY = 30.0
TEST = os.environ.get('GRAMADRAMA_ENGINE_TEST') == '1'
# Outside tests the engine always corrects with Bielik/LanguageTool; tests use the debug
# transforms unless they ask for the real backend (against a stub server).
REAL_BACKEND = not TEST or os.environ.get('GRAMADRAMA_BACKEND') == 'real'
# Per-update events (surrounding text, resets, key dispositions) only in tests or when debugging.
VERBOSE = TEST or os.environ.get('GRAMADRAMA_ENGINE_DEBUG') == '1'
LINUX_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DIAG_XML = """<node><interface name="pl.gramadrama.Engine">
<method name="Status"><arg type="s" direction="out"/></method>%s
</interface></node>"""
TEST_XML = """
<method name="SetDebugReply"><arg type="s" direction="in"/></method>
<method name="SetDebugDelay"><arg type="i" direction="in"/></method>
<method name="SetKeyDelay"><arg type="d" direction="in"/></method>
<method name="Snapshot"><arg type="s" direction="out"/></method>"""
UI_EFFECTS = (S.ShowUI, S.HideUI, S.Message)
CARET_ASKED = 16  # ConfirmCaret questions waiting for the live service at most
# The only text a live service's log event may carry: a cause's code (core.error_kind), never words of a message.
WHY = re.compile(r'[A-Za-z0-9_]{1,40}')
# The GNOME Shell extension's panel (docs/architecture.md, Panel D-Bus interface); without it: the IBus candidate popup.
PANEL_NAME, PANEL_PATH, PANEL_IFACE = 'org.gnome.Shell', '/pl/gramadrama/Panel', 'pl.gramadrama.Panel1'
PANEL_MISSING = frozenset(('org.freedesktop.DBus.Error.UnknownMethod', 'org.freedesktop.DBus.Error.UnknownObject',
                           'org.freedesktop.DBus.Error.UnknownInterface', 'org.freedesktop.DBus.Error.ServiceUnknown',
                           'org.freedesktop.DBus.Error.NameHasNoOwner'))
PANEL_DENIED = PANEL_IFACE + '.Error.AccessDenied'  # the caller check (docs/architecture.md, Caller check)
PANEL_PING_SECONDS = 10  # while the panel is missing, ask again at most this often
WARM_INTERVAL = 15 * 60  # seconds between warm-ups; Ollama keeps the model loaded 24 h after each
FOCUS_PING_SECONDS = 2  # the same when a field gets focus: the extension may be back (after the lock screen)
FOCUS_VERSION = 4  # the first extension with Subscribe and the Focus signal (docs/apps.md)
HUD_VERSION = 5  # the first extension with the dictation pill, Hud, and the live badge, Live
MARKS_VERSION = 6  # the first with the live check's underlines, Marks
EDIT_VERSION = 12  # the first that draws a result being edited and reports its lines (the Edit signal)
PROBLEM_VERSION = 16  # the first that shows a failed correction's title and buttons (view.problem)
EDIT_LINES = 10000  # visual lines of an edited result the engine takes from the panel at most
LIVE_SEND_MS = 100  # the live service gets the field's text at most this often
# Live checking is on by default; in test mode off unless settings.json turns it on, so that scenarios
# of other features never get a badge in their way.
LIVE_DEFAULT = not TEST
FOCUS_WAIT_MS = 2000  # no Focus this long after a Subscribe: the focus is unknown
APPS_RELOAD_MS = 200  # apps.json changed: read it once the writer is done (a burst of events)


class PanelClient:
    """Talks to the extension's pl.gramadrama.Panel1. Every call is asynchronous (never from the key
    path's critical section, never waiting); clicks arrive as the Action signal and go to the
    engine that has focus. From extension version 4 on it also subscribes to the focused app
    (docs/apps.md): the Focus signal, sent to this connection only, keeps app.focus current. While
    the panel or its focus is missing, the next focus change asks again (focus_in)."""

    def __init__(self, app, by_ibus=True):
        self.app = app
        self.by_ibus = by_ibus  # started by ibus-daemon: the only engine the extension lets Subscribe
        self.available = None  # None: not known yet
        self.version = 0  # the extension's, from its Ping
        self.last_ping = 0.0
        # The extension went away after it had answered (Focus('', ''): the lock screen disables it).
        # Until it answers again, every focus change asks: any of them may come after the unlock.
        self.returning = False
        # The extension is there but its focus is not (a Subscribe failed, or no Focus came): the next
        # focus change subscribes again.
        self.resubscribe = False
        self.subscribe_id = 0  # of the newest Subscribe: the reply to an older one is ignored
        self.focus_timer = 0
        self.focus_seen = False  # a Focus arrived since the last Subscribe
        self.following = None  # the subscription as the log last said (it gets the changes only)
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self.conn = None
            return
        self.conn.signal_subscribe(PANEL_NAME, PANEL_IFACE, 'Action', PANEL_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._action)
        self.conn.signal_subscribe(PANEL_NAME, PANEL_IFACE, 'Focus', PANEL_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._focus)
        self.conn.signal_subscribe(PANEL_NAME, PANEL_IFACE, 'Edit', PANEL_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._edit)
        self.ping(force=True)

    def ping(self, force=False, interval=PANEL_PING_SECONDS):
        if not self.conn or (not force and time.monotonic() - self.last_ping < interval):
            return
        self.last_ping = time.monotonic()
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Ping', None, GLib.VariantType('(s)'),
                       Gio.DBusCallFlags.NO_AUTO_START, 2000, None, self._pinged)

    def focus_in(self):
        """A field got focus (Engine.do_focus_in_id). The extension's focus is needed before the next
        Ctrl+Alt+G, not only at the next session: a missing panel or focus is asked for again, at
        most every FOCUS_PING_SECONDS. While the extension is returning, every focus change asks at
        once: a Ping answered while it was still off (the unlock dialog's own field) must not hold
        back the one after the unlock."""
        if self.returning:
            self.ping(force=True)
        elif self.available is False or self.resubscribe:
            self.ping(interval=FOCUS_PING_SECONDS)

    def _pinged(self, conn, result):
        try:
            version = conn.call_finish(result).unpack()[0]
        except GLib.Error as exc:
            self._missing(exc)
            return
        if not self.available:
            self.app.log('panel', available=True, version=version)
        self.available, self.returning = True, False
        self.version = int(version) if version.isdigit() else 0
        if version.isdigit() and int(version) >= FOCUS_VERSION:
            self._subscribe()
        else:
            self.resubscribe = False  # an older extension has no focus to give

    def _missing(self, exc):
        name = Gio.DBusError.get_remote_error(exc)
        if self.available is not False:
            self.app.log('panel', available=False, error=name or 'error')
        if name in PANEL_MISSING or not name:
            if self.available:
                self.returning = True  # it answered before: disabled for a while, most likely (the lock screen)
            self.available, self.resubscribe = False, False
            self._no_focus()  # the extension is gone, and its focus with it

    # ---- the focused app (docs/apps.md, section 1) ----
    def _subscribe(self):
        self.subscribe_id += 1
        sid = self.subscribe_id
        self.focus_seen = False
        if self.focus_timer:
            GLib.source_remove(self.focus_timer)
        self.focus_timer = GLib.timeout_add(FOCUS_WAIT_MS, self._focus_waited)

        def reply(conn, result):
            self._subscribed(conn, result, sid)
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Subscribe', None, None,
                       Gio.DBusCallFlags.NO_AUTO_START, 2000, None, reply)

    def _subscribed(self, conn, result, sid):
        try:
            conn.call_finish(result)
        except GLib.Error as exc:
            if sid != self.subscribe_id:
                return  # a newer Subscribe is on its way, and decides
            name = Gio.DBusError.get_remote_error(exc)
            self._no_focus(name or 'error')
            if name in PANEL_MISSING or not name:
                # After a Ping of version 4 even UnknownMethod means that the extension went away
                # meanwhile (GDBus answers so for an object that is no longer exported).
                self._missing(exc)
            else:
                # AccessDenied: disabled and enabled again during the caller check, or GNOME Shell did
                # not know ibus-daemon yet; ask again at the next focus change. An engine started by
                # hand is refused for good (docs/architecture.md, Caller check): it uses the IBus popup anyway.
                self.resubscribe = self.by_ibus or name != PANEL_DENIED

    def _focus_waited(self):
        self.focus_timer = 0
        if not self.focus_seen:
            self._no_focus('timeout')
            self.resubscribe = True  # e.g. disabled during the caller check: ask again at the next focus change
        return False

    def _focus(self, conn, sender, path, iface, signal, params):
        if params.get_type_string() != '(ss)':
            return
        app_id, kind = params.unpack()
        if not kind:
            # Focus('', ''): the extension is being disabled (the lock screen does that). Nothing
            # follows until it is back: every focus change asks for it (focus_in), and so does the
            # next session.
            self._no_focus('disabled')
            self.available, self.returning, self.resubscribe = False, True, False
            self.last_ping = 0.0
            return
        if self.following is not True:
            self.following = True
            self.app.log('focus', subscribed=True)  # the app IDs themselves go to the log only with a hotkey
        self.focus_seen, self.resubscribe = True, False
        self.app.focus = (app_id, kind)

    def _no_focus(self, why=None):
        """The focus is unknown from now on. why: the subscription failed or ended, logged once until
        a Focus arrives again; None when the panel's own log line says why."""
        if self.focus_timer:
            GLib.source_remove(self.focus_timer)
            self.focus_timer = 0
        self.focus_seen = False
        self.app.focus = None
        if why is None:
            self.following = None
        elif self.following is not False:
            self.following = False
            self.app.log('focus', subscribed=False, why=why)

    def _view_json(self, view):
        # One locale travels with every view; user/model text is copied unchanged.
        language = getattr(self.app, 'ui_language', 'pl')
        return json.dumps(dict(view, ui_language=language))

    def show(self, view, done):
        """done(shown) runs on the main loop: True once the panel is on screen."""
        def reply(conn, result):
            try:
                shown = conn.call_finish(result).unpack()[0]
            except GLib.Error as exc:
                self._missing(exc)
                shown = False
            done(bool(shown))
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Show', GLib.Variant('(s)', (self._view_json(view),)),
                       GLib.VariantType('(b)'), Gio.DBusCallFlags.NO_AUTO_START, 3000, None, reply)

    def hud(self, view, done):
        """The dictation pill (extension version 5); done(shown) on the main loop."""
        def reply(conn, result):
            try:
                shown = conn.call_finish(result).unpack()[0]
            except GLib.Error as exc:
                self._missing(exc)
                shown = False
            done(bool(shown))
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Hud', GLib.Variant('(s)', (self._view_json(view),)),
                       GLib.VariantType('(b)'), Gio.DBusCallFlags.NO_AUTO_START, 3000, None, reply)

    def live(self, view):
        """The live check's badge (extension version 5); fire and forget."""
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Live', GLib.Variant('(s)', (self._view_json(view),)),
                       GLib.VariantType('(b)'), Gio.DBusCallFlags.NO_AUTO_START, 3000, None, None)

    def marks(self, view):
        """The live check's underlines (extension version 6); fire and forget."""
        self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Marks', GLib.Variant('(s)', (self._view_json(view),)),
                       GLib.VariantType('(b)'), Gio.DBusCallFlags.NO_AUTO_START, 3000, None, None)

    def hide(self, sid=0):
        if self.conn and self.available:
            self.conn.call(PANEL_NAME, PANEL_PATH, PANEL_IFACE, 'Hide', GLib.Variant('(u)', (sid,)), None,
                           Gio.DBusCallFlags.NO_AUTO_START, 3000, None, None)

    def _action(self, conn, sender, path, iface, signal, params):
        sid, name = params.unpack()
        engine = self.app.active
        if engine is not None:
            engine.panel_action(sid, name)

    def _edit(self, conn, sender, path, iface, signal, params):
        """Edit(session, kind, data) about the result being edited; data may hold the user's text: never logged."""
        if params.get_type_string() != '(uss)':
            return
        sid, kind, data = params.unpack()
        engine = self.app.active
        if engine is not None:
            engine.panel_edit(sid, kind, data)

# Status() outside test mode: no text lengths, offsets, freshness flags, counters or key timings,
# which change with every keystroke and would let any process watch the user type.
COARSE_STATUS = ('client', 'caps', 'purpose', 'hints', 'sensitive', 'session', 'mode', 'modal', 'ui', 'message')


class Logger:
    """JSONL event log (never text). A writer thread does the file I/O, so a slow disk can never
    delay a key; when it falls LOG_QUEUE lines behind, lines are dropped."""

    def __init__(self, path):
        self.path = path
        self.queue = queue.Queue(LOG_QUEUE)
        self.thread = threading.Thread(target=self._writer, name='gramadrama-log', daemon=True)
        self.thread.start()

    def __call__(self, event, **data):
        try:
            self.queue.put_nowait(json.dumps(dict(t=round(time.time(), 6), ev=event, **data), separators=(',', ':')))
        except queue.Full:
            pass

    def close(self, timeout=2.0):
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self.thread.join(timeout)

    def _writer(self):
        while True:
            lines = [self.queue.get()]
            while lines[-1] is not None and len(lines) < LOG_QUEUE:
                try:
                    lines.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            done = lines[-1] is None
            self._write([line for line in lines if line is not None])
            if done:
                return

    def _write(self, lines):
        if not lines or not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + '.1')
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
        except OSError:
            pass


def log_path():
    return os.environ.get('GRAMADRAMA_ENGINE_LOG') or os.path.join(GLib.get_user_cache_dir(), 'gramadrama', 'engine.log')


class AppList:
    """apps.json (docs/apps.md, section 2): the apps where GramaDrama is off, in app.apps_off, and the mode
    Ctrl+Alt+G starts in per app, in app.apps_modes.
    A Gio.FileMonitor reports changes (the app writes a temp file and renames it), so no restart is
    needed; a thread reads the file, at most applist.MAX_BYTES of it (the main loop does no file
    I/O), and a missing or broken file disables nothing and changes no mode. The log gets apps_config (ok,
    how many apps are off, how many have a mode) once per change."""

    def __init__(self, app, path=None):
        self.app = app
        self.path = str(path or paths.apps_path())
        self.reload_id = 0
        self.generation = 0  # of the newest read: an older read that finishes later is dropped
        self.logged = None
        try:
            self.monitor = Gio.File.new_for_path(self.path).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect('changed', self._changed)
        except GLib.Error as exc:
            self.monitor = None  # no live reload: a change needs an engine restart
            app.log('error', where='apps_monitor', type=type(exc).__name__, at=[])
        self.load()

    def _changed(self, monitor, file, other, event):
        if not self.reload_id:
            self.reload_id = GLib.timeout_add(APPS_RELOAD_MS, self._reload)

    def _reload(self):
        self.reload_id = 0
        self.load()
        return False

    def load(self):
        self.generation += 1
        threading.Thread(target=self._read, args=(self.generation,), name='gramadrama-apps', daemon=True).start()

    def _read(self, generation):
        try:
            with open(self.path, 'rb') as f:
                data = f.read(applist.MAX_BYTES + 1)
        except FileNotFoundError:
            data = None  # no file: nothing is off
        except OSError:
            data = b''  # unreadable (a directory, no permission): broken
        try:
            disabled, modes, ok = applist.load_all(data)
            sites = applist.load_sites(data)
        except Exception:  # never a silent end of this thread: whatever went wrong, nothing is off
            disabled, modes, ok, sites = frozenset(), {}, False, frozenset()
        GLib.idle_add(self._loaded, generation, disabled, modes, ok, data, sites)

    def _loaded(self, generation, disabled, modes, ok, data, sites=frozenset()):
        if generation == self.generation:
            self.app.apps_off, self.app.apps_modes, self.app.apps_ok, self.app.apps_read = disabled, modes, ok, True
            if sites != self.app.sites_off:
                self.app.sites_off = sites
                if self.app.browsers is not None:
                    self.app.browsers.push_sites(sites)  # every extension hears of a change, from the popup or from the app
            state = (ok, disabled, sorted(modes.items())) if ok else (ok, data)  # a broken file: once per content
            if state != self.logged:
                self.logged = state
                self.app.log('apps_config', ok=ok, off=len(disabled), modes=len(modes))
        return False

    def close(self):
        if self.reload_id:
            GLib.source_remove(self.reload_id)
            self.reload_id = 0
        if self.monitor is not None:
            self.monitor.cancel()


class ShortcutSettings(AppList):
    """Watch settings.json off the key path, including atomic saves and stale read protection."""

    def __init__(self, app, path=None):
        super().__init__(app, path or paths.settings_path())

    def _read(self, generation):
        try:
            with open(self.path, 'rb') as f:
                data = f.read(applist.MAX_BYTES + 1)
            data = json.loads(data) if len(data) <= applist.MAX_BYTES else None
        except (OSError, ValueError, RecursionError):
            data = None
        primary = data.get('primary_language', 'pl') if isinstance(data, dict) else 'pl'
        GLib.idle_add(self._loaded, generation, shortcuts.resolve(data), primary)

    def _loaded(self, generation, bindings, primary='pl'):
        if generation == self.generation:
            self.app.shortcuts = bindings
            self.app.ui_language = set_language(primary)
            browsers = getattr(self.app, 'browsers', None)
            if browsers is not None and hasattr(browsers, 'hub'):
                browsers.hub.push_preferences()
        return False


class BrowserLink:
    """engine/browser.Hub in the GLib loop: the extensions of the browsers (docs/browser.md). The log gets browser_connected
    and browser_disconnected with the browser's name and the extension's version, never text."""

    def __init__(self, app):
        self.app = app
        self.hub = browser.Hub(on_change=self._changed, on_site=self._site, sites=lambda: app.sites_off,
                               preferences=lambda: {'ui_language': getattr(app, 'ui_language', 'pl'),
                                                    'correction_shortcut': getattr(app, 'shortcuts', shortcuts.resolve())[0].label})
        self.watches = {}  # Connection -> the id of its GLib source
        self.known = {}
        fd = self.hub.listen()
        self.timer = 0
        if fd is None:
            app.log('browser_socket', ok=False)
            return
        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._accept)
        self.timer = GLib.timeout_add(250, self._expire)

    def _accept(self, fd, condition):
        connection = self.hub.accept()
        if connection is not None:
            self.watches[connection] = GLib.io_add_watch(connection.fileno(), GLib.PRIORITY_DEFAULT,
                                                         GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._readable, connection)
        return True

    def _readable(self, fd, condition, connection):
        if self.hub.readable(connection):
            return True
        self.watches.pop(connection, None)
        return False

    def _expire(self):
        self.hub.expire()
        return True

    def _changed(self):
        now = self.hub.status()
        for name, version in now.items():
            if self.known.get(name) != version:
                self.app.log('browser_connected', browser=name, extension=version)
        for name in set(self.known) - set(now):
            self.app.log('browser_disconnected', browser=name)
        self.known = now

    def _site(self, site, disabled):
        """The popup of an extension switched a site: apps.json is changed in a thread (the main loop does no file I/O); its
        monitor then reloads it and the new list goes to every extension (AppList._loaded)."""
        threading.Thread(target=self._write_site, args=(site, disabled), name='gramadrama-site', daemon=True).start()

    def _write_site(self, site, disabled):
        path = str(paths.apps_path())
        try:
            try:
                with open(path, 'rb') as f:
                    data = f.read(applist.MAX_BYTES + 1)
            except FileNotFoundError:
                data = None
            changed = applist.with_site(data, site, disabled)
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            temp = path + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(changed)
            os.replace(temp, path)
            GLib.idle_add(self._site_saved, disabled)
        except (OSError, ValueError):
            GLib.idle_add(self._site_failed)  # a broken apps.json is the app's to repair; the extension's popup keeps its copy

    def _site_saved(self, disabled):
        self.app.log('browser_site', disabled=disabled)  # the site itself is never in the log
        return False

    def _site_failed(self):
        self.app.log('browser_site_failed')
        self.push_sites(self.app.sites_off)  # the extension shows what is really saved
        return False

    def push_sites(self, sites):
        self.hub.push_sites(sites)

    def close(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        for source in self.watches.values():
            GLib.source_remove(source)
        self.watches.clear()
        self.hub.close()


class BrowserLiveLink:
    """engine/browser_live.BrowserLive in the GLib loop (docs/browser.md): a timer reads the field of the focused browser's page
    through the extension, the live service's issues for it come back here (App.live_event), and the badge and underlines go to
    the Shell's panel. While a browser with a connected extension has the focus IBus is not the source of the live check."""

    def __init__(self, app, hub):
        self.app, self.hub = app, hub
        self.logic = browser_live.BrowserLive(hub, self)
        self.timer = GLib.timeout_add(int(browser_live.POLL * 1000), self._tick)

    # what BrowserLive calls
    def text(self, field, text, caret):
        self.app.live_command(self, {'cmd': 'text', 'field': field, 'text': text, 'caret': caret})

    def clear_service(self):
        self.app.live_command(self, {'cmd': 'clear'})

    def fixed(self, field, start, end):
        self.app.live_command(self, {'cmd': 'fixed', 'field': field, 'start': start, 'end': end})

    def command(self, command):
        self.app.live_command(self, command)

    def badge(self, view):
        panel = self.app.panel
        if panel is not None and panel.version >= HUD_VERSION:
            panel.live(view)

    def marks(self, view):
        panel = self.app.panel
        if panel is not None and panel.version >= MARKS_VERSION:
            panel.marks(view)

    def log(self, event, **data):
        self.app.log(event, **data)

    def message(self, reason, seconds):
        engine = self.app.active
        if engine is not None:
            engine._message(reason, seconds)

    def sites(self):
        return self.app.sites_off

    # the engine's side
    def browser_for(self, app_id):
        """The hub's name of the browser an application ID is, when its extension is connected; else None."""
        name = browser.browser_for_app(app_id)
        return name if name is not None and self.hub.connection_of(name) is not None else None

    def owns(self, app_id):
        """A browser with a connected extension has the focus: IBus does not check the field then."""
        return self.browser_for(app_id) is not None

    def _tick(self):
        app = self.app
        app_id = (app.focus or (None, None))[0]
        panel = app.panel
        off = (panel is None or panel.version < HUD_VERSION or not app_id or not app.apps_read or not app.apps_ok
               or applist.is_off(app_id, app.apps_off) or app_id == applist.GRAMADRAMA_APP)
        busy = any(e.logic.session.state != S.IDLE or e.logic.dictation.shown for e in app.engines)
        try:
            enabled = speech.setting('live_check', LIVE_DEFAULT)
            self.logic.tick(self.browser_for(app_id), enabled, off, busy)
        except Exception as exc:  # the loop of the engine must never end here
            app.log('error', where='browser_live', type=type(exc).__name__, at=[])
        return True

    def live_event(self, event):
        """From the live service, on the main loop (App.live_event). Its marks are the AT-SPI's: a page has none."""
        if event.get('ev') == 'died':
            self.logic._run(self.logic.field.clear())  # hide a spinner; the next read starts a new service
        elif event.get('ev') == 'issues' and isinstance(event.get('issues'), list):
            self.logic.issues(event.get('field'), event['issues'], event.get('checking') is True)

    def close(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0


class App:
    """Process-wide state: engines, key timings, diagnostics and test hooks."""

    def __init__(self, bus, log):
        self.bus, self.log = bus, log
        self.panel = None  # PanelClient, created once the main loop is about to run
        self.apps = None  # AppList, likewise
        self.shortcuts = shortcuts.resolve()
        self.ui_language = 'pl'
        # (app, kind) of the focused window as GNOME Shell pushes it (PanelClient), None when
        # unknown; and the app IDs where GramaDrama is off (apps.json). EngineLogic.key gets both.
        self.focus = None
        self.apps_off, self.apps_ok = frozenset(), True
        self.apps_modes = {}  # {app ID: the mode Ctrl+Alt+G starts in}; an app that is not here starts in Popraw
        self.apps_read = False  # apps.json has been read once (live checking waits for it)
        self.sites_off = frozenset()  # the sites where the browser extension is off (apps.json "sites")
        self.browsers = None  # BrowserLink: the extensions of the browsers (docs/browser.md), likewise
        self.browser_live = None  # BrowserLiveLink: live checking in their pages
        self.engines = []
        self.active = None
        self.key_us = {'passed': collections.deque(maxlen=4096), 'consumed': collections.deque(maxlen=512)}
        self.n_keys = 0
        self.key_delay = 0.0
        self.debug_reply = self.debug_delay = None
        self.warmed = None  # time.monotonic() of the last warm-up
        self.speech = None  # speech.SpeechClient, from the first dictation on
        self.dictating = None  # (id, Engine) of the newest dictation: the speech service's events go there
        self.live = None  # speech.LiveClient, from the first text to check on
        self.live_owner = None  # the Engine whose field the live service checks
        self.live_revision = 0  # a deferred clear must not erase newer text from the same owner
        self.correction_serial = 0  # session ids are per input context; transport ids must be process-wide
        self.corrections = {}  # request id -> (Engine, StartBackend)
        self.caret_asked = {}  # ConfirmCaret id -> the Engine its answer goes to

    def key_timed(self, seconds, consumed):
        self.n_keys += 1
        self.key_us['consumed' if consumed else 'passed'].append(seconds * 1e6)

    def key_stats(self):
        st = {'n': self.n_keys}
        for kind, values in self.key_us.items():
            us = sorted(values)
            if us:
                st[kind] = {'n': len(us), 'median_us': round(us[len(us) // 2], 1),
                            'p95_us': round(us[int(len(us) * 0.95)], 1), 'max_us': round(us[-1], 1)}
        return st

    def status(self):
        st = {'pid': os.getpid(), 'engines': len(self.engines), 'test': TEST,
              'backend': 'real' if REAL_BACKEND else 'debug'}
        if self.browsers is not None:
            st['browsers'] = self.browsers.hub.status()  # {browser: extension version}
        if self.browser_live is not None:
            st['browser_live'] = self.browser_live.logic.status()
        if TEST:
            st['keys'] = self.key_stats()
            # Never outside tests: any process on the bus could follow the focused app here.
            st['focus'] = list(self.focus) if self.focus else None
            st['apps'] = {'off': sorted(self.apps_off), 'ok': self.apps_ok, 'modes': dict(self.apps_modes)}
        if self.active is not None:
            st.update(self.active.status())
        return st

    def warm_up(self):
        """An engine was enabled (the start, a new input context): load the model on the model server, at
        most every WARM_INTERVAL, so the first Ctrl+Alt+G does not wait for it."""
        now = time.monotonic()
        if not REAL_BACKEND or (self.warmed is not None and now - self.warmed < WARM_INTERVAL):
            return
        self.warmed = now
        backend.warm_up()
        self.log('warm_up')

    def browser_name(self):
        """The browser whose window has the focus, when its extension is connected (the hub's name); else None: the
        hotkey and dictation then go the IBus way."""
        link = self.browser_live
        return link.browser_for((self.focus or (None, None))[0]) if link is not None else None

    def browser_request(self, engine, effect):
        """A BrowserRequest of an engine: asked through the hub, answered to that engine on the main loop, always once and
        never inside the effect that asked (a host that is gone answers at once). The log gets the op and outcome, no text."""
        started = time.monotonic()
        answered = []

        def done(answer):
            if answered:
                return
            answered.append(True)
            if effect.op == 'context' and answer.get('ok') and answer.get('site') in self.sites_off:
                answer = {'ok': False, 'reason': 'site_off'}  # GramaDrama is off on this site: the page is not used
            self.log('browser_request', kind=effect.kind, op=effect.op, ok=bool(answer.get('ok')),
                     reason=str(answer.get('reason') or '')[:40] or None, ms=round((time.monotonic() - started) * 1000))
            GLib.idle_add(lambda: engine in self.engines and engine._call(
                'browser', engine.logic.browser_answer, effect.kind, effect.id, answer) and False)

        hub = self.browsers.hub if self.browsers is not None else None
        asked = hub.request(effect.browser, effect.op, done, effect.seconds, effect.write, **effect.fields) if hub else None
        if asked is None:
            done({'ok': False, 'reason': 'no_extension', 'attempted': False})

    def dictating_elsewhere(self, engine):
        """Another engine object (input context) still dictates: one dictation at a time for the process."""
        return any(other is not engine and other.logic.dictation.active for other in self.engines)

    def speech_command(self, engine, cmd, sid, alternate=False):
        if cmd == 'start':
            self.dictating = (sid, engine)
        if self.speech is None:
            self.speech = speech.SpeechClient(GLib.idle_add, self.speech_event)
        self.speech.send(cmd, sid, alternate)

    def live_command(self, engine, command):
        if command.get('cmd') == 'text':
            self.live_owner = engine
            self.live_revision += 1
            command = dict(command, revision=self.live_revision)
        elif self.live is None and command.get('cmd') != 'learn':
            return  # a clear with nothing started; a word from the panel goes to the dictionary with the check off too
        if command.get('cmd') == 'clear':
            if engine is not self.live_owner:
                return
            if any(e.starts for e in self.engines):
                # StartBackend runs off the key path. Send its correct request before a busy panel's
                # clear, so the service keeps the matching live request alive for the manual consumer.
                revision = self.live_revision
                def clear_later():
                    if revision == self.live_revision and self.live_owner is engine:
                        self.live_command(engine, command)
                    return False
                GLib.idle_add(clear_later)
                return
        if self.live is None:
            self.live = speech.LiveClient(GLib.idle_add, self.live_event)
        self.live.send(command, start=command.get('cmd') in ('text', 'learn'))

    def start_correction(self, engine, effect):
        """Experimental Popraw uses the live service's exact result. Called only off the key path."""
        if (effect.mode != 'fix' or not engine.backend.real or engine.backend.reply is not None
                or (backend.test_hooks() and os.environ.get('GRAMADRAMA_DEBUG_REPLY') is not None)
                or not speech.setting('live_check', LIVE_DEFAULT) or not speech.setting('live_model_only', False)):
            return False
        self.correction_serial += 1
        rid = self.correction_serial
        self.corrections[rid] = (engine, effect)
        if self.live is None:
            self.live = speech.LiveClient(GLib.idle_add, self.live_event)
        try:
            self.live.send({'cmd': 'correct', 'id': rid, 'text': effect.text}, start=True)
        except Exception as exc:
            self.corrections.pop(rid, None)
            engine.backend_error(effect.id, tr(backend.FAILED), type(exc).__name__[:40])
        return True

    def cancel_correction(self, engine, sid=None):
        """Detach a consumer before its late reply can touch a canceled or destroyed session."""
        found = False
        for rid, (owner, effect) in list(self.corrections.items()):
            if owner is engine and (sid is None or effect.id == sid):
                del self.corrections[rid]
                found = True
                if self.live is not None:
                    self.live.send({'cmd': 'cancel_correct', 'id': rid}, start=False)
        return found

    def correction_event(self, event):
        entry = self.corrections.pop(event.get('id'), None)
        if entry is None:
            return
        engine, effect = entry
        if (engine not in self.engines or engine.logic.session.id != effect.id
                or engine.logic.session.state != S.WAITING):
            return
        kind = event.get('kind')
        if kind == 'fallback':  # settings changed since the engine selected the shared path
            try:
                engine.backend.start(effect.id, effect.text, effect.mode, GLib.idle_add, effect.target)
            except Exception as exc:
                engine.backend_error(effect.id, tr(backend.FAILED), type(exc).__name__[:40])
        elif kind == 'result' and isinstance(event.get('corrected'), str) and isinstance(event.get('rows'), (tuple, list)):
            info = {key: event[key] for key in ('target', 'languages', 'note', 'segments', 'changes', 'count') if key in event}
            engine.backend_result(effect.id, event['corrected'], tuple(event['rows']), info)
        else:
            engine.backend_error(effect.id, event.get('message') or tr(backend.FAILED), str(event.get('why') or 'unknown')[:40])

    def caret_command(self, engine, cid, text):
        """Where the caret is in text (ConfirmCaret): the live service asks AT-SPI, with the live check off too."""
        self.caret_asked[cid] = engine
        while len(self.caret_asked) > CARET_ASKED:  # answers that never came (a service that died)
            self.caret_asked.pop(next(iter(self.caret_asked)))
        if self.live is None:
            self.live = speech.LiveClient(GLib.idle_add, self.live_event)
        self.live.send({'cmd': 'caret', 'id': cid, 'text': text}, start=True)

    def live_event(self, event):
        """From the live service, on the main loop."""
        kind = event.get('ev')
        if kind in ('issues', 'marks') and 'revision' in event and event['revision'] != self.live_revision:
            return  # an old check can already be queued in the pipe when the user edits the field
        if kind == 'correction':
            self.correction_event(event)
            return
        if kind == 'caret':
            engine = self.caret_asked.pop(event.get('id'), None)
            if engine in self.engines:
                engine._call('caret', engine.logic.caret_confirmed, event['id'], event.get('cursor'), event.get('anchor'))
                engine._live_soon()
            return
        if kind == 'died':
            self.caret_asked.clear()  # never answered: a hotkey's question ends at its timeout
            pending, self.corrections = self.corrections, {}
            for engine, effect in pending.values():
                if engine in self.engines:
                    engine.backend_error(effect.id, tr(backend.FAILED), 'service_died')
        if kind == 'log':
            data = {k: v for k, v in event.items() if k not in ('ev', 'event') and isinstance(v, (int, float, bool))}
            if isinstance(event.get('why'), str) and WHY.fullmatch(event['why']):
                data['why'] = event['why']
            self.log(str(event.get('event')), **data)
            return
        engine = self.live_owner
        if isinstance(engine, BrowserLiveLink):
            engine.live_event(event)
            return
        if engine is None or engine not in self.engines:
            return
        if kind == 'died':
            self.log('live_died')
            engine._run(engine.logic.live.clear())  # stops loading too; the next change restarts checking
        elif kind == 'issues' and isinstance(event.get('issues'), list):
            engine._call('live', engine.logic.live_result, event.get('field'), event['issues'], event.get('checking') is True)
        elif kind == 'marks':
            engine._call('live', engine.logic.live_marks, event.get('field'), event.get('caret'), event.get('length'),
                         event.get('view'))

    def speech_event(self, event):
        """From the speech service, on the main loop: to the engine whose dictation it is."""
        if event.get('ev') == 'model':
            self.log('speech_model', state=event.get('state'), device=event.get('device'), ms=event.get('ms'))
            return
        sid, engine = self.dictating or (None, None)
        if VERBOSE and event.get('ev') != 'level':
            self.log('speech_event', kind=event.get('ev'), id=event.get('id'), routed=engine is not None and engine in self.engines)
        if engine is None or engine not in self.engines:
            return
        if event.get('ev') == 'died':
            engine._call('speech', engine.logic.speech_died)
        elif event.get('id') == sid:  # an older dictation's late answer goes nowhere
            engine._call('speech', engine.logic.speech_event, event, self.browser_name())

    def set_debug(self):
        for engine in self.engines:
            engine.backend.reply, engine.backend.delay = self.debug_reply, self.debug_delay

    # ---- diagnostics on the session bus ----
    def export(self):
        info = Gio.DBusNodeInfo.new_for_xml(DIAG_XML % (TEST_XML if TEST else ''))

        def acquired(conn, name):
            conn.register_object(DIAG_PATH, info.interfaces[0], self.on_call, None, None)

        flags = Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE
        Gio.bus_own_name(Gio.BusType.SESSION, DIAG_NAME, flags, acquired, None, None)

    def on_call(self, conn, sender, path, iface, method, params, inv):
        try:
            args = params.unpack()
            if method == 'Status':
                return inv.return_value(GLib.Variant('(s)', (json.dumps(self.status()),)))
            if not TEST:
                raise ValueError(method)
            if method == 'SetDebugReply':
                self.debug_reply = args[0] or None
                self.set_debug()
            elif method == 'SetDebugDelay':
                self.debug_delay = args[0] if args[0] >= 0 else None
                self.set_debug()
            elif method == 'SetKeyDelay':
                self.key_delay = min(max(args[0], 0.0), MAX_KEY_DELAY)
            elif method == 'Snapshot':  # test-only: contains the field's text
                e = self.active
                snap = {'text': e.logic.ctx.text, 'cursor': e.logic.ctx.cursor, 'anchor': e.logic.ctx.anchor,
                        'client': e.logic.ctx.client} if e else None
                return inv.return_value(GLib.Variant('(s)', (json.dumps(snap),)))
            else:
                raise ValueError(method)
            inv.return_value(None)
        except Exception as exc:
            self.log('diag_error', method=method, type=type(exc).__name__)
            inv.return_dbus_error('pl.gramadrama.Engine.Error', type(exc).__name__)


class Engine(IBus.EngineSimple):
    __gtype_name__ = 'GramaDramaEngine'

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.logic = S.EngineLogic()
        self.logic.ctx.can_confirm = True  # through the live service (App.caret_command)
        self.backend = backend.ProcessBackend(self, delay=app.debug_delay, reply=app.debug_reply, real=REAL_BACKEND)
        self.timers = set()
        self.message_timer = 0
        self.ui = 'hidden'  # 'session', 'panel', 'message' or 'hidden'
        self.ui_seq = 0  # bumped by every drawn UI change: a late panel reply for an older one is ignored
        self.panel_sid = 0  # session whose panel GNOME Shell has confirmed on screen
        self.ui_queue = []
        self.ui_idle = 0
        self.starts = {}  # session id -> StartBackend waiting for the idle callback
        self.start_idle = 0
        self.counts = collections.Counter()
        self.key_applied = False  # the key being handled has sent an Apply
        self.hud_timer = 0  # hides a dictation message in the IBus popup (no extension version 5)
        self.live_check = None  # the newest live.Check not sent yet
        self.live_timer = self.live_idle = 0

    # ---- effects ----
    def _run(self, effects, key=None):
        handled = False
        for e in effects:
            kind = type(e)
            if kind is S.Consume:
                handled = True
            elif kind is S.ChainUp:
                handled = IBus.EngineSimple.do_process_key_event(self, *key)
            elif kind in UI_EFFECTS:
                self._queue_ui(e)
            elif kind is S.StartBackend:
                # Creating the worker thread costs ~0.5 ms: do it right after the key returns.
                self.starts[e.id] = e
                if not self.start_idle:
                    self.start_idle = GLib.idle_add(self._start_backends)
            elif kind is S.CancelBackend:
                if self.starts.pop(e.id, None) is None:
                    if not self.app.cancel_correction(self, e.id):
                        self.backend.cancel(e.id)
            elif kind is S.Apply:
                # Everything that can fail comes before the first signal: never a delete alone.
                commit = IBus.Text.new_from_string(e.ops.commit) if e.ops.commit is not None else None
                self.key_applied = key is not None
                if e.ops.delete:
                    self.delete_surrounding_text(*e.ops.delete)
                if commit is not None:
                    self.commit_text(commit)
            elif kind is S.ScheduleVerifyTimeout:
                self._timer(e.ms, self.logic.verify_timeout, e.id)
            elif kind is S.ConfirmCaret:
                self.app.caret_command(self, e.id, e.text)
            elif kind is BT.BrowserRequest:
                self.app.browser_request(self, e)
            elif kind is S.ScheduleConfirmTimeout:
                self._timer(e.ms, self.logic.confirm_timeout, e.id)
            elif kind is S.ScheduleIdleTimeout:
                self._timer(e.ms, self.logic.idle_timeout, e.id)
            elif kind is S.ResetCompose:
                IBus.EngineSimple.do_reset(self)
            elif kind is S.Log:
                self.app.log(e.event, **e.data)
            elif kind is S.OpenApp:
                self._open_app(e.args)
            elif kind is dictate.PointTo:
                self._open_app(('--show=' + e.target,))
            elif kind is dictate.ScheduleOpen:
                self._timer(0, self._open, e.gen)
            elif kind is dictate.ScheduleArm:
                self._timer(e.ms, self._armed, e.gen)
            elif kind is dictate.ScheduleWatch:
                self._timer(e.ms, self.logic.dictation_watch, e.id)
            elif kind is dictate.Speech:
                self.app.speech_command(self, e.cmd, e.id, e.alternate)
            elif kind is dictate.Hud:
                self._hud(e.view)
            elif kind is dictate.Insert:
                self.commit_text(IBus.Text.new_from_string(e.text))
            elif kind is dictate.Note:
                self.app.log(e.event, **e.data)
            elif kind is live.Check:
                self.live_check = e  # sent by _send_live, at most every LIVE_SEND_MS
                if not self.live_timer:
                    self.live_timer = GLib.timeout_add(LIVE_SEND_MS, self._send_live)
            elif kind is live.Clear:
                self.live_check = None
                self.app.live_command(self, {'cmd': 'clear'})
            elif kind is live.Badge:
                panel = self._panel()
                if panel is not None and panel.version >= HUD_VERSION:
                    panel.live(e.view)
            elif kind is live.Marks:
                panel = self._panel()
                if panel is not None and panel.version >= MARKS_VERSION:
                    panel.marks(e.view)
            elif kind is live.Command:
                self.app.live_command(self, e.command)
        return handled

    def _open_app(self, args):
        """The GramaDrama app beside this engine (gramadrama-app, as its .desktop file starts it), on the tab a failed
        correction's button names or the group the dictation key points to; a running app gets the arguments as its
        command line."""
        launcher = os.path.join(LINUX_DIR, 'gramadrama-app')
        try:
            Gio.Subprocess.new(['/usr/bin/python3', launcher] + list(args), Gio.SubprocessFlags.STDOUT_SILENCE
                               | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as exc:  # the session is over already: only say so in the log
            self.app.log('error', where='open_app', type=type(exc).__name__)

    # ---- live checking (docs/live-check.md) ----
    def _live_soon(self):
        """The field changed: what to check is decided off the key path, once per main loop turn."""
        if not self.live_idle:
            self.live_idle = GLib.idle_add(self._live)

    def _live(self):
        self.live_idle = 0
        app = (self.app.focus or (None, None))[0]
        panel = self._panel()
        # Text leaves only for a badge that can show what is found (extension version 5), in a known app
        # that is not off (a broken apps.json may have lost the user's list), and never in the GramaDrama
        # app itself (its Wypróbuj field), as on macOS.
        off = (panel is None or panel.version < HUD_VERSION or not app or not self.app.apps_read
               or not self.app.apps_ok or applist.is_off(app, self.app.apps_off) or app == applist.GRAMADRAMA_APP)
        if self.logic.session.editing is None and self.app.browser_live is not None and self.app.browser_live.owns(app):
            return False  # the page's text comes from the browser's extension (BrowserLiveLink), not from IBus
        self._call('live', self.logic.live_update, speech.setting('live_check', LIVE_DEFAULT), off)
        return False

    def _send_live(self):
        self.live_timer = 0
        check, self.live_check = self.live_check, None
        if check is not None:
            command = {'cmd': 'text', 'field': check.field, 'text': check.text, 'caret': check.caret}
            self.app.live_command(self, dict(command, panel=True) if check.panel else command)
        return False

    def _open(self, gen):
        # The right Ctrl came down alone: the microphone opens on the next turn of the main loop, never on the key path.
        return self.logic.dictation_open(gen, speech.enabled(), speech.ready(), self.app.focus, self.app.apps_off,
                                         self.app.dictating_elsewhere(self))

    def _armed(self, gen):
        # The right Ctrl has been held alone: settings and tools are read here, never on the key path.
        return self.logic.dictation_armed(gen, speech.enabled(), speech.ready(), self.app.focus, self.app.apps_off,
                                          self.app.dictating_elsewhere(self))

    # ---- the dictation pill: the extension's, or a line in the IBus popup ----
    def _hud(self, view):
        panel = self._panel()
        if panel is not None and panel.version >= HUD_VERSION:
            panel.hud(view, lambda shown: shown or self._hud_line(view))
        else:
            self._hud_line(view)

    def _hud_line(self, view):
        if self.hud_timer:
            GLib.source_remove(self.hud_timer)
            self.hud_timer = 0
        state = view.get('state')
        if state == 'hidden':
            if self.ui == 'hidden':
                self.hide_auxiliary_text()
            return
        if state == 'listening':
            if view.get('waiting'):  # no audio yet: a suspended USB microphone takes up to 2 s to wake
                line = tr('GramaDrama · Budzę mikrofon…')
            else:
                line = tr('GramaDrama · Słucham… %d:%02d') % divmod(int(view.get('seconds') or 0), 60)
            if view.get('language'):  # PL or EN: Shift with the key dictates in the other language
                line += ' · ' + str(view['language'])
        elif state in ('recognizing', 'cleaning'):
            line = 'GramaDrama · ' + (tr('Rozpoznaję…') if state == 'recognizing' else tr('Porządkuję…'))
        else:
            line = view.get('message') or ''
            self.hud_timer = GLib.timeout_add(int(float(view.get('seconds') or 3) * 1000), self._hud_line_done)
        self.update_auxiliary_text(IBus.Text.new_from_string(line), True)

    def _hud_line_done(self):
        self.hud_timer = 0
        if self.ui == 'hidden':
            self.hide_auxiliary_text()
        return False

    def _call(self, where, fn, *args):
        try:
            self._run(fn(*args))
        except Exception as exc:
            self._failed(where, exc)

    def _failed(self, where, exc, key=None):
        frames = traceback.extract_tb(exc.__traceback__)[-4:]
        self.app.log('error', where=where, type=type(exc).__name__,
                     at=['%s:%d' % (os.path.basename(f.filename), f.lineno) for f in frames])
        try:
            self._run([e for e in self.logic.fail() if not isinstance(e, (S.Consume, S.ChainUp))])
        except Exception:
            pass
        try:
            self.ui_queue.clear()
            self._hide_ui()
        except Exception:
            pass
        if key is None:
            return False
        if self.key_applied:
            return True  # its correction was (partly) sent: delivering the key too would add to it
        try:
            if not key[2] & keys.RELEASE:
                self.logic.forget_key(key[1])  # the app gets this press, so it gets the release too
            return IBus.EngineSimple.do_process_key_event(self, *key)
        except Exception:
            return False

    def _start_backends(self):
        self.start_idle = 0
        starts, self.starts = self.starts, {}
        for e in starts.values():
            try:
                if not self.app.start_correction(self, e):
                    self.backend.start(e.id, e.text, e.mode, GLib.idle_add, e.target)
            except Exception as exc:
                self._failed('backend', exc)
        return False

    def _timer(self, ms, fn, sid):
        def fire():
            self.timers.discard(source)
            self._call('timer', fn, sid)
            return False
        source = GLib.timeout_add(ms, fire)
        self.timers.add(source)

    # ---- UI (IBus auxiliary text + lookup table), applied from an idle callback ----
    def _queue_ui(self, effect):
        self.ui_queue.append(effect)
        if not self.ui_idle:
            self.ui_idle = GLib.idle_add(self._flush_ui)

    def _panel(self):
        panel = self.app.panel
        if panel is None or panel.conn is None:
            return None
        if not panel.available:
            panel.ping()  # the extension may have been enabled since
        return panel if panel.available else None

    def _flush_panel(self, panel, effects):
        """Draw the last UI effect in the GNOME Shell panel; the session becomes modal only when the
        panel reports it is on screen (a refusal or confirmation toast never makes it modal)."""
        e = effects[-1]
        self.ui_seq += 1
        seq, sid = self.ui_seq, self.logic.session.id
        if not (isinstance(e, S.ShowUI) and sid == self.panel_sid):
            # A new panel: modal only once it is on screen. Updates of a panel already on screen
            # (streaming, mode switch) keep the session modal: an Enter typed between an update and
            # its reply must never reach the app.
            self.logic.ui_flushed(False)
            self.panel_sid = 0
        self._cancel_message()
        self.hide_auxiliary_text()
        self.hide_lookup_table()
        if isinstance(e, S.HideUI):
            panel.hide(0)
            self.panel_sid = 0
            self.ui = 'hidden'
            return
        if isinstance(e, S.Message):
            view = {'v': 1, 'session': sid, 'kind': 'message', 'status': 'message',
                    'message': fallback_ui.message(e.reason), 'seconds': e.seconds}
            self.ui = 'message:' + e.reason
        else:
            view = self.logic.session.panel_view()
            self.ui = 'panel'

        def done(shown):
            if seq != self.ui_seq:
                return  # an older update: a newer one is on its way
            try:
                if isinstance(e, S.ShowUI):
                    if shown:
                        self.panel_sid = sid
                    else:  # the panel could not be shown (no caret, on-screen keyboard): the IBus popup
                        self.panel_sid = 0
                        self._show(e.view)
                    self.logic.ui_flushed(True, panel=shown and panel.version >= EDIT_VERSION,
                                          problems=shown and panel.version >= PROBLEM_VERSION)
                elif not shown:
                    self._message(e.reason, e.seconds)
            except Exception as exc:
                self._failed('ui', exc)
        panel.show(view, done)

    def panel_edit(self, sid, kind, data):
        """The panel's Edit signal: 'lines', the start offsets of the edited result's visual lines (JSON), or 'paste',
        {"id", "text"} with the clipboard's text for a paste. Never logged: the text is the user's."""
        if kind not in ('lines', 'paste') or len(data) > (12 * EDIT_LINES if kind == 'lines' else 8 * S.PASTE_MAX):
            return
        try:
            value = json.loads(data)
        except ValueError:
            return
        if kind == 'lines' and isinstance(value, list) and len(value) <= EDIT_LINES:
            self._call('edit', self.logic.edit_lines, sid, value)
        elif kind == 'paste' and isinstance(value, dict) and type(value.get('id')) is int:
            self._call('edit', self.logic.edit_paste, sid, value['id'], value.get('text'))

    def panel_action(self, sid, name):
        self.app.log('panel_action', id=sid, action=name)
        link = self.app.browser_live
        if link is not None and link.logic.handles(name):  # a click in the list or card of a page's check: the extension fixes it
            link.logic.action(name)
            return
        if name == 'hidden' and sid == self.panel_sid:
            self.panel_sid = 0
            self.ui = 'hidden'
        self._call('action', self.logic.action, sid, name, self.app.browser_name())

    def _flush_ui(self):
        self.ui_idle = 0
        self._live_soon()  # a session started or ended: the badge goes or comes back
        effects, self.ui_queue = self.ui_queue, []
        panel = self._panel() if effects else None
        if panel is not None:
            try:
                self._flush_panel(panel, effects)
            except Exception as exc:
                self._failed('ui', exc)
            return False
        try:
            for i, e in enumerate(effects):
                if isinstance(e, S.ShowUI) and i + 1 < len(effects):
                    continue  # superseded by a later UI effect
                if isinstance(e, S.ShowUI):
                    self._show(e.view)
                elif isinstance(e, S.Message):
                    self._message(e.reason, e.seconds)
                else:
                    self._hide_ui()
            # Only now is the session modal: Enter or Esc typed before this reached the app.
            self.logic.ui_flushed(bool(effects) and isinstance(effects[-1], S.ShowUI))
        except Exception as exc:
            self._failed('ui', exc)
        return False

    def _cancel_message(self):
        if self.message_timer:
            GLib.source_remove(self.message_timer)
            self.message_timer = 0

    def _show(self, view):
        self._cancel_message()
        aux, lines = fallback_ui.render(view)
        self.update_auxiliary_text(IBus.Text.new_from_string(aux), True)
        if lines:
            table = IBus.LookupTable.new(fallback_ui.PAGE_LINES, 0, False, False)
            for i, line in enumerate(lines):
                table.append_candidate(IBus.Text.new_from_string(line))
                table.set_label(i, IBus.Text.new_from_string(' '))
            self.update_lookup_table(table, True)
        else:
            self.hide_lookup_table()
        self.ui = 'session'

    def _message(self, reason, seconds):
        self._cancel_message()
        self.hide_lookup_table()
        self.update_auxiliary_text(IBus.Text.new_from_string(fallback_ui.message(reason)), True)
        self.message_timer = GLib.timeout_add(int(seconds * 1000), self._message_done)
        self.ui = 'message:' + reason

    def _message_done(self):
        self.message_timer = 0
        try:
            self._hide_ui()
        except Exception as exc:
            self._failed('ui', exc)
        return False

    def _hide_ui(self):
        self.logic.ui_flushed(False)
        self._cancel_message()
        self.hide_auxiliary_text()
        self.hide_lookup_table()
        if self.ui != 'hidden' and self.app.panel is not None and self.app.panel.available:
            self.ui_seq += 1
            self.panel_sid = 0
            self.app.panel.hide(0)
        self.ui = 'hidden'

    def _drop_message(self):
        # A refusal message belongs to the field it was shown in.
        if self.message_timer or self.ui.startswith('message'):
            self._queue_ui(S.HideUI())

    # ---- backend sink (called on the main loop through GLib.idle_add) ----
    def backend_partial(self, sid, text, rows, info=None):
        self._call('backend', self.logic.backend_partial, sid, text, rows, info)

    def backend_result(self, sid, corrected, rows, info=None):
        self._call('backend', self.logic.backend_result, sid, corrected, rows, info)

    def backend_changes(self, sid, changes, count):
        self._call('backend', self.logic.backend_changes, sid, changes, count)

    def backend_error(self, sid, message, why=None):
        self._call('backend', self.logic.backend_error, sid, message, why)

    # ---- diagnostics ----
    def status(self):
        st = dict(self.logic.status(), path=self.get_object_path(), ui=self.ui.split(':')[0],
                  message=self.ui[8:] if self.ui.startswith('message:') else None)
        if TEST:
            return dict(st, counts=dict(self.counts))
        return {k: st[k] for k in COARSE_STATUS}

    # ---- vfuncs ----
    def do_process_key_event(self, keyval, keycode, state):
        t0 = time.perf_counter()
        key = (keyval, keycode, state)
        self.key_applied = False
        try:
            if self.app.key_delay:
                time.sleep(self.app.key_delay)  # test-only fault injection (SetKeyDelay)
            handled = self._run(self.logic.key(keyval, keycode, state, self.app.focus, self.app.apps_off,
                                               self.app.apps_modes, self._hotkey_browser(keyval, state), self.app.shortcuts,
                                               self._typed(keyval, state)), key)
        except Exception as exc:
            handled = self._failed('key', exc, key)
        self.app.key_timed(time.perf_counter() - t0, handled)
        self._live_soon()  # a hotkey starts a session, a key may make the text current again
        return bool(handled)

    def _typed(self, keyval, state):
        """The character a press types, only while a session is modal (the panel's editor), else None."""
        if state & keys.RELEASE or not self.logic.session.is_modal:
            return None
        char = IBus.keyval_to_unicode(keyval)
        return char if char and char.isprintable() else None

    def _hotkey_browser(self, keyval, state):
        """The browser to ask for the text, for a press that may be the hotkey only (the key path stays short)."""
        if state & keys.RELEASE or not any(binding.matches(keyval, state & keys.RELEVANT)
                                           for binding in self.app.shortcuts):
            return None
        return self.app.browser_name()

    def do_focus_in_id(self, path, client):
        self._chain('focus_in_id', IBus.EngineSimple.do_focus_in_id, self, path, client)
        self.app.active = self
        self.counts['focus_in'] += 1
        self.app.log('focus_in', client=client)
        self._drop_message()
        self._call('focus_in', self.logic.focus_in, client)
        self._chain('focus_in_id', self.get_surrounding_text)  # RequireSurroundingText
        if self.app.panel is not None:
            self._chain('focus_in_id', self.app.panel.focus_in)  # the extension may be back (after the lock screen)
        self._live_soon()

    def do_focus_in(self):
        # With has-focus-id ibus-daemon still sends one plain FocusIn after CreateEngine.
        self._chain('focus_in', IBus.EngineSimple.do_focus_in, self)
        self.app.active = self
        self._call('focus_in', self.logic.focus_in, None)

    def do_focus_out_id(self, path):
        self.app.log('focus_out')
        self._drop_message()
        self._call('focus_out', self.logic.focus_out)
        self._chain('focus_out_id', IBus.EngineSimple.do_focus_out_id, self, path)
        self._live_soon()

    def do_focus_out(self):
        self._call('focus_out', self.logic.focus_out)
        self._chain('focus_out', IBus.EngineSimple.do_focus_out, self)

    def do_set_surrounding_text(self, text, cursor, anchor):
        # MUST chain up: the base class keeps the text for get_surrounding_text(). A password
        # field's text is not handed on (ibus-daemon still holds its own copy; see the spec, L4).
        if self.logic.ctx.sensitive:
            self._chain('surrounding', IBus.EngineSimple.do_set_surrounding_text, self, IBus.Text.new_from_string(''), 0, 0)
        else:
            self._chain('surrounding', IBus.EngineSimple.do_set_surrounding_text, self, text, cursor, anchor)
        self.counts['surrounding'] += 1
        try:
            value = text.get_text() if text is not None else None
            if VERBOSE and not self.logic.ctx.sensitive:  # not even the length of a password
                self.app.log('surrounding', len=None if value is None else len(value), cursor=cursor, anchor=anchor)
            self._run(self.logic.surrounding(value, cursor, anchor))
        except Exception as exc:
            self._failed('surrounding', exc)
        self._live_soon()

    def do_set_content_type(self, purpose, hints):
        self._chain('content_type', IBus.EngineSimple.do_set_content_type, self, purpose, hints)
        self.app.log('content_type', purpose=int(purpose), hints=int(hints))
        self._call('content_type', self.logic.content_type, int(purpose), int(hints))
        if self.logic.ctx.sensitive:  # text that came before the purpose leaves the base class too
            self._chain('surrounding', IBus.EngineSimple.do_set_surrounding_text, self, IBus.Text.new_from_string(''), 0, 0)
        self._live_soon()

    def do_set_capabilities(self, caps):
        self._chain('capabilities', IBus.EngineSimple.do_set_capabilities, self, caps)
        self.app.log('capabilities', caps=int(caps))
        self._call('capabilities', self.logic.capabilities, int(caps))

    def do_reset(self):
        self._chain('reset', IBus.EngineSimple.do_reset, self)
        self.counts['reset'] += 1
        if VERBOSE:
            self.app.log('reset')
        self._call('reset', self.logic.reset)
        self._live_soon()

    def do_enable(self):
        self._chain('enable', IBus.EngineSimple.do_enable, self)
        self.app.log('enable')
        self._chain('enable', self.get_surrounding_text)
        self._chain('enable', self.app.warm_up)

    def do_disable(self):
        self.app.log('disable')
        self._call('disable', self.logic.disable)
        self._chain('disable', IBus.EngineSimple.do_disable, self)

    def do_destroy(self):
        self.app.log('destroy')
        self._call('destroy', self.logic.disable)
        self.app.cancel_correction(self)
        for source in list(self.timers) + [self.message_timer, self.ui_idle, self.start_idle, self.hud_timer,
                                            self.live_timer, self.live_idle]:
            if source:
                GLib.source_remove(source)
        self.timers.clear()
        self.starts.clear()
        self.message_timer = self.ui_idle = self.start_idle = self.hud_timer = self.live_timer = self.live_idle = 0
        if self.app.dictating and self.app.dictating[1] is self:
            self.app.dictating = None
        for cid in [cid for cid, engine in self.app.caret_asked.items() if engine is self]:
            del self.app.caret_asked[cid]
        if self.app.live_owner is self:
            self.app.live_command(self, {'cmd': 'clear'})
            self.app.live_owner = None
        if self in self.app.engines:
            self.app.engines.remove(self)
        if self.app.active is self:
            self.app.active = None
        self._chain('destroy', IBus.EngineSimple.do_destroy, self)

    def do_page_up(self):
        self.counts['page_up'] += 1
        if self.logic.session.state == S.IDLE:
            return self._chain('page_up', IBus.EngineSimple.do_page_up, self)
        self._call('page_up', self.logic.page, -1)

    def do_page_down(self):
        self.counts['page_down'] += 1
        if self.logic.session.state == S.IDLE:
            return self._chain('page_down', IBus.EngineSimple.do_page_down, self)
        self._call('page_down', self.logic.page, 1)

    def do_candidate_clicked(self, index, button, state):
        # Lines of our popup are not candidates: a click must never insert anything.
        self.counts['candidate_clicked'] += 1
        if self.ui == 'hidden':
            self._chain('candidate_clicked', IBus.EngineSimple.do_candidate_clicked, self, index, button, state)

    def _chain(self, where, fn, *args):
        try:
            fn(*args)
        except Exception as exc:
            self._failed(where, exc)


class Factory:
    """Creates engines through 'create-engine' so construct-only properties can be set:
    has_focus_id gives the client id in focus_in_id, active_surrounding_text makes ibus-daemon
    ask the client for surrounding text on every focus change."""

    def __init__(self, app):
        self.app = app
        self.n = 0
        self.factory = IBus.Factory.new(app.bus.get_connection())
        self.factory.connect('create-engine', self.create)

    def create(self, factory, name):
        if name != ENGINE_NAME:
            return None
        self.n += 1
        path = '/org/freedesktop/IBus/Engine/GramaDrama/%d' % self.n
        engine = Engine(self.app, engine_name=name, object_path=path, connection=self.app.bus.get_connection(),
                        has_focus_id=True, active_surrounding_text=True)
        self.app.engines.append(engine)
        self.app.log('create_engine', path=path)
        return engine


def dev_component():
    """Component for a hand-started engine (development only): the template with this launcher."""
    exec_line = '%s %s --ibus' % (sys.executable, os.path.join(LINUX_DIR, 'gramadrama-engine'))
    with open(os.path.join(LINUX_DIR, 'data', 'gramadrama.xml.in'), encoding='utf-8') as f:
        xml = f.read().replace('@EXEC@', exec_line.replace('&', '&amp;').replace('<', '&lt;'))
    with tempfile.NamedTemporaryFile('w', suffix='.xml', encoding='utf-8') as f:
        f.write(xml)
        f.flush()
        return IBus.Component.new_from_file(f.name)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    log = Logger(log_path())
    IBus.init()
    bus = IBus.Bus()
    if not bus.is_connected():
        log('fatal', why='no_ibus_daemon')
        log.close()
        return 1
    app = App(bus, log)
    # Threads (backend relays, log writer) hand the GIL back to the key path quickly.
    sys.setswitchinterval(0.0005)
    loop = GLib.MainLoop()
    bus.connect('disconnected', lambda *_: (log('ibus_disconnected'), loop.quit()))
    factory = Factory(app)  # noqa: F841 (must stay alive)
    if '--ibus' in argv:
        bus.request_name(BUS_NAME, 0)
    else:
        bus.register_component(dev_component())
    app.export()
    app.panel = PanelClient(app, by_ibus='--ibus' in argv)
    log('started', pid=os.getpid(), exec_by_ibus='--ibus' in argv, test=TEST,
        ibus='%d.%d.%d' % (IBus.MAJOR_VERSION, IBus.MINOR_VERSION, IBus.MICRO_VERSION))
    app.apps = AppList(app)
    shortcut_settings = ShortcutSettings(app)
    app.browsers = BrowserLink(app)
    if app.browsers.hub.listener is not None:
        app.browser_live = BrowserLiveLink(app, app.browsers.hub)
    try:
        loop.run()
    finally:
        app.apps.close()
        shortcut_settings.close()
        if app.browser_live is not None:
            app.browser_live.close()
        app.browsers.close()
        if app.speech is not None:
            app.speech.close()
        log('exit')
        log.close()
    return 0
