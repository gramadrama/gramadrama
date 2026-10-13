"""The adapter's process-wide state for docs/apps.md (gramadrama/engine/main.py): the focused app
that PanelClient keeps from the extension's Focus signal, and apps.json through AppList. No bus is
used: PanelClient gets a fake connection whose replies the tests hand in; AppList reads real files
in a temporary directory, and the GLib main context is iterated by hand."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gi.repository import Gio, GLib  # noqa: E402
from gramadrama.engine import applist, main  # noqa: E402

UNKNOWN_METHOD = 'org.freedesktop.DBus.Error.UnknownMethod'  # GDBus's answer for an object that is not exported
UNKNOWN_OBJECT = 'org.freedesktop.DBus.Error.UnknownObject'


def pump(pred, timeout=3.0):
    """Iterate the default main context until pred() is true; returns pred()."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    with warnings.catch_warnings():
        # PyGObject's asyncio glue asks for the deprecated event loop policy on every iteration.
        warnings.simplefilter('ignore', DeprecationWarning)
        while not pred() and time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)
    return pred()


class FakeApp:
    def __init__(self):
        self.focus = None
        self.apps_off, self.apps_ok, self.apps_modes = frozenset(), True, {}
        self.sites_off, self.browsers = frozenset(), None  # the browser extension's list, and the link that tells the extensions
        self.active = None
        self.logs = []

    def log(self, event, **data):
        self.logs.append((event, data))

    def events(self, name):
        return [data for event, data in self.logs if event == name]


class FakeBrowsers:
    def __init__(self):
        self.pushed = []

    def push_sites(self, sites):
        self.pushed.append(sorted(sites))


class FakeConn:
    """A session bus connection that records calls; call_finish(reply) returns or raises reply."""

    def __init__(self):
        self.calls = []
        self.signals = {}

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.signals[member] = (sender, callback)
        return len(self.signals)

    def call(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable, callback):
        self.calls.append((method, callback))

    def call_finish(self, reply):
        if isinstance(reply, Exception):
            raise reply
        return reply

    def methods(self):
        return [method for method, _ in self.calls]


def dbus_error(name):
    return Gio.DBusError.new_for_dbus_error(name, 'test')


class PanelFocusTests(unittest.TestCase):
    def setUp(self):
        self.app, self.conn = FakeApp(), FakeConn()
        self.panel = self.client()

    def client(self, **kw):
        with mock.patch.object(main.Gio, 'bus_get_sync', return_value=self.conn):
            panel = main.PanelClient(self.app, **kw)
        self.addCleanup(panel._no_focus)  # no timer left behind for the next test
        return panel

    def ping_reply(self, version=None, error=None, call=-1):
        method, callback = self.conn.calls[call]
        self.assertEqual(method, 'Ping')
        callback(self.conn, dbus_error(error) if error else GLib.Variant('(s)', (version,)))

    def subscribe_reply(self, error=None, call=-1):
        method, callback = self.conn.calls[call]
        self.assertEqual(method, 'Subscribe')
        callback(self.conn, dbus_error(error) if error else GLib.Variant('()', ()))

    def sent(self, n):
        """The calls since the first n."""
        return self.conn.methods()[n:]

    def focus(self, app, kind):
        sender, callback = self.conn.signals['Focus']
        self.assertEqual(sender, 'org.gnome.Shell')  # only GNOME Shell may tell the focus
        callback(self.conn, ':1.5', main.PANEL_PATH, main.PANEL_IFACE, 'Focus', GLib.Variant('(ss)', (app, kind)))

    def test_subscribes_from_version_4_and_keeps_the_focus(self):
        self.ping_reply('4')
        self.assertEqual(self.conn.methods(), ['Ping', 'Subscribe'])
        self.subscribe_reply()
        self.focus('google-chrome.desktop', 'wayland')
        self.assertEqual(self.app.focus, ('google-chrome.desktop', 'wayland'))
        self.focus('org.gnome.Shell', 'shell')
        self.focus('', 'none')  # no focus window: known, and allowed
        self.assertEqual(self.app.focus, ('', 'none'))
        self.assertEqual(self.app.logs, [('panel', {'available': True, 'version': '4'}), ('focus', {'subscribed': True})])
        self.assertNotIn('chrome', repr(self.app.logs))  # focus changes are not logged

    def test_an_older_extension_gives_no_focus_and_no_error(self):
        self.ping_reply('3')
        self.assertEqual(self.conn.methods(), ['Ping'])  # no Subscribe
        self.assertIsNone(self.app.focus)
        self.assertEqual(self.app.events('focus'), [])
        n = len(self.conn.calls)
        self.panel.focus_in()  # its panel works: a focus change asks nothing
        self.assertEqual(self.sent(n), [])

    def test_subscribe_errors(self):
        # After a Ping of version 4, UnknownMethod too means the extension went away meanwhile: GDBus
        # answers so for an object that is no longer exported (a version 3 is never asked, above).
        cases = [(main.PANEL_DENIED, True),  # disabled and enabled again during the caller check
                 ('org.freedesktop.DBus.Error.Failed', True),
                 (UNKNOWN_OBJECT, False), (UNKNOWN_METHOD, False)]
        for error, available in cases:
            with self.subTest(error):
                self.panel.ping(force=True)
                self.ping_reply('4')
                self.subscribe_reply()
                self.focus('chatgpt.desktop', 'x11')
                self.panel.ping(force=True)  # e.g. after the extension came back
                self.ping_reply('4')
                self.subscribe_reply(error)
                self.assertIsNone(self.app.focus)
                self.assertEqual(self.panel.available, available)
                self.assertEqual((self.panel.resubscribe, self.panel.returning), (available, not available))
                self.assertEqual(self.app.events('focus')[-1], {'subscribed': False, 'why': error})

    def test_a_subscribe_that_did_not_subscribe_is_asked_for_again(self):
        """Review finding: every way a Subscribe can end without a subscription leads to a new one at
        the next focus change, not only at the next engine start."""
        with mock.patch.object(main, 'FOCUS_PING_SECONDS', 0), mock.patch.object(main, 'FOCUS_WAIT_MS', 30):
            for how in ('gone', 'no Focus', 'denied'):
                with self.subTest(how):
                    self.panel.ping(force=True)
                    self.ping_reply('4')
                    if how == 'gone':  # disabled between the Ping and the Subscribe
                        self.subscribe_reply(UNKNOWN_METHOD)
                    elif how == 'no Focus':  # answered, but disabled during the caller check
                        self.subscribe_reply()
                        self.assertTrue(pump(lambda: self.panel.resubscribe))
                    else:
                        self.subscribe_reply(main.PANEL_DENIED)
                    self.assertIsNone(self.app.focus)
                    n = len(self.conn.calls)
                    self.panel.focus_in()
                    self.assertEqual(self.sent(n), ['Ping'])
                    self.ping_reply('4')
                    self.assertEqual(self.sent(n), ['Ping', 'Subscribe'])
                    self.subscribe_reply()
                    self.focus('obsidian.desktop', 'wayland')
                    self.assertEqual(self.app.focus, ('obsidian.desktop', 'wayland'))
                    self.assertFalse(self.panel.resubscribe or self.panel.returning)
                    n = len(self.conn.calls)
                    self.panel.focus_in()  # subscribed: nothing to ask
                    self.assertEqual(self.sent(n), [])
        # the failures are logged once each, not at every new try
        self.assertEqual(self.app.events('focus'), [
            {'subscribed': False, 'why': UNKNOWN_METHOD}, {'subscribed': True},
            {'subscribed': False, 'why': 'timeout'}, {'subscribed': True},
            {'subscribed': False, 'why': main.PANEL_DENIED}, {'subscribed': True}])

    def test_an_engine_started_by_hand_is_refused_for_good(self):
        panel = self.client(by_ibus=False)
        self.ping_reply('4')
        self.subscribe_reply(main.PANEL_DENIED)
        self.assertTrue(panel.available)  # the panel refuses it as well: it uses the IBus popup
        n = len(self.conn.calls)
        with mock.patch.object(main, 'FOCUS_PING_SECONDS', 0):
            panel.focus_in()
        self.assertEqual(self.sent(n), [])

    def test_the_reply_to_an_older_subscribe_is_ignored(self):
        self.ping_reply('4')
        first = len(self.conn.calls) - 1
        self.panel.ping(force=True)
        self.ping_reply('4')
        self.subscribe_reply()
        self.focus('chatgpt.desktop', 'wayland')
        self.subscribe_reply(main.PANEL_DENIED, call=first)  # the first Subscribe answers last
        self.assertEqual(self.app.focus, ('chatgpt.desktop', 'wayland'))
        self.assertFalse(self.panel.resubscribe)

    def test_no_focus_within_the_wait_means_unknown(self):
        with mock.patch.object(main, 'FOCUS_WAIT_MS', 30):
            self.ping_reply('4')
            self.subscribe_reply()
        self.assertTrue(pump(lambda: self.app.events('focus')))
        self.assertEqual(self.app.events('focus'), [{'subscribed': False, 'why': 'timeout'}])
        self.assertIsNone(self.app.focus)
        # A Focus in time keeps it.
        with mock.patch.object(main, 'FOCUS_WAIT_MS', 30):
            self.panel.ping(force=True)
            self.ping_reply('4')
            self.subscribe_reply()
        self.focus('chatgpt.desktop', 'wayland')
        pump(lambda: False, 0.1)
        self.assertEqual(self.app.focus, ('chatgpt.desktop', 'wayland'))

    def test_the_extension_being_disabled_forgets_the_focus_until_it_is_back(self):
        self.ping_reply('4')
        self.subscribe_reply()
        self.focus('org.gnome.Ptyxis.desktop', 'wayland')
        self.focus('', '')  # disable(): the lock screen switches user extensions off
        self.assertIsNone(self.app.focus)
        self.assertIs(self.panel.available, False)
        self.assertEqual(self.app.events('focus')[-1], {'subscribed': False, 'why': 'disabled'})
        # Review finding: the unlock dialog's field gets focus first, while the extension is still
        # off; the app's field gets it right after the unlock, well within FOCUS_PING_SECONDS.
        n = len(self.conn.calls)
        self.panel.focus_in()
        self.assertEqual(self.sent(n), ['Ping'])
        self.ping_reply(error=UNKNOWN_METHOD)  # still off
        self.panel.focus_in()  # not held back by the Ping that failed
        self.assertEqual(self.sent(n), ['Ping', 'Ping'])
        self.ping_reply('4')  # it is back
        self.subscribe_reply()
        self.focus('org.gnome.Ptyxis.desktop', 'wayland')
        self.assertEqual(self.app.focus, ('org.gnome.Ptyxis.desktop', 'wayland'))
        self.assertTrue(self.panel.available)
        n = len(self.conn.calls)
        self.panel.focus_in()  # back and subscribed: nothing to ask
        self.assertEqual(self.sent(n), [])

    def test_a_panel_that_never_answered_is_asked_at_most_every_two_seconds(self):
        self.ping_reply(error=UNKNOWN_METHOD)  # no extension (it is optional)
        self.assertIs(self.panel.available, False)
        self.assertFalse(self.panel.returning)
        n = len(self.conn.calls)
        with mock.patch.object(main.time, 'monotonic', return_value=self.panel.last_ping + 1):
            self.panel.focus_in()
        self.assertEqual(self.sent(n), [])
        with mock.patch.object(main.time, 'monotonic', return_value=self.panel.last_ping + main.FOCUS_PING_SECONDS):
            self.panel.focus_in()
        self.assertEqual(self.sent(n), ['Ping'])

    def test_a_missing_panel_forgets_the_focus(self):
        self.ping_reply('4')
        self.subscribe_reply()
        self.focus('chatgpt.desktop', 'wayland')
        self.panel._missing(dbus_error(UNKNOWN_OBJECT))  # e.g. a Show that failed
        self.assertIsNone(self.app.focus)
        self.assertIs(self.panel.available, False)
        self.assertTrue(self.panel.returning)  # it answered before: every focus change asks
        n = len(self.conn.calls)
        self.panel.focus_in()
        self.assertEqual(self.sent(n), ['Ping'])

    def test_a_focus_with_another_signature_is_ignored(self):
        _sender, callback = self.conn.signals['Focus']
        callback(self.conn, ':1.5', main.PANEL_PATH, main.PANEL_IFACE, 'Focus', GLib.Variant('(s)', ('x',)))
        self.assertIsNone(self.app.focus)


class ShortcutSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'settings.json'
        self.app = FakeApp()
        self.app.shortcuts = None
        self.watcher = main.ShortcutSettings(self.app, self.path)
        self.addCleanup(self.watcher.close)
        self.assertTrue(pump(lambda: self.app.shortcuts is not None))

    def write(self, value):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(self.path)

    def test_atomic_edits_and_deletion_reload_without_engine_restart(self):
        defaults = main.shortcuts.resolve()
        self.assertEqual(self.app.shortcuts, defaults)
        for letter in ('K', 'L'):
            self.write({'correction_shortcut': 'Ctrl+Shift+' + letter})
            expected = main.shortcuts.resolve({'correction_shortcut': 'Ctrl+Shift+' + letter})
            self.assertTrue(pump(lambda: self.app.shortcuts == expected))
        self.path.unlink()
        self.assertTrue(pump(lambda: self.app.shortcuts == defaults))
        self.assertEqual(self.app.logs, [])  # no settings, tokens or shortcut values in the log

    def test_corrupt_file_and_old_reads_cannot_install_unsafe_shortcuts(self):
        self.write({'correction_shortcut': 'a'})
        expected = main.shortcuts.resolve()
        self.watcher._read(self.watcher.generation)
        self.assertTrue(pump(lambda: self.app.shortcuts == expected))
        old = main.shortcuts.resolve({'correction_shortcut': 'Ctrl+Shift+K'})
        self.watcher._loaded(self.watcher.generation - 1, old)
        self.assertEqual(self.app.shortcuts, expected)

    def test_browser_lookup_only_for_current_exact_shortcuts(self):
        self.app.shortcuts = main.shortcuts.resolve({'correction_shortcut': 'Ctrl+Shift+K'})
        self.app.browser_name = mock.Mock(return_value='chrome')
        engine = type('FakeEngine', (), {'app': self.app})()
        key, mods = ord('K'), main.keys.CONTROL | main.keys.SHIFT
        self.assertEqual(main.Engine._hotkey_browser(engine, key, mods), 'chrome')
        for keyval, state in ((ord('g'), main.keys.CONTROL | main.keys.MOD1),
                              (key, mods | main.keys.MOD5), (key, mods | main.keys.RELEASE)):
            self.assertIsNone(main.Engine._hotkey_browser(engine, keyval, state))
        self.assertEqual(self.app.browser_name.call_count, 1)


class AppListTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / 'gramadrama'
        self.path = self.dir / 'apps.json'
        self.app = FakeApp()

    def watch(self):
        apps = main.AppList(self.app, self.path)
        self.addCleanup(apps.close)
        self.assertTrue(pump(lambda: self.app.events('apps_config')), 'the first read never finished')
        return apps

    def write(self, data):
        """Like the app: a temporary file renamed over apps.json."""
        self.dir.mkdir(exist_ok=True)
        tmp = self.dir / '.apps.json.tmp'
        tmp.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        os.replace(tmp, self.path)

    def logged(self, n, timeout=3.0):
        self.assertTrue(pump(lambda: len(self.app.events('apps_config')) >= n, timeout), self.app.logs)
        return self.app.events('apps_config')

    def test_the_sites_of_the_browser_extension_are_read_and_every_extension_is_told(self):
        self.app.browsers = FakeBrowsers()
        self.dir.mkdir()
        self.watch()
        self.assertEqual(self.app.sites_off, frozenset())
        self.write({'v': 1, 'sites': ['mail.example', 'a.example']})
        self.assertTrue(pump(lambda: self.app.browsers.pushed == [['a.example', 'mail.example']], 3), self.app.browsers.pushed)
        self.assertEqual(self.app.sites_off, frozenset({'mail.example', 'a.example'}))
        self.write({'v': 1, 'disabled': ['x.desktop'], 'sites': ['mail.example', 'a.example']})  # another entry changed: no news
        self.logged(2)  # apps_config is logged when what the engine uses changes: the first read and this one
        self.assertEqual(self.app.browsers.pushed, [['a.example', 'mail.example']])
        self.write({'v': 1, 'sites': 'oops'})  # a broken file switches nothing off
        self.assertTrue(pump(lambda: self.app.browsers.pushed[-1:] == [[]], 3), self.app.browsers.pushed)
        self.assertEqual(self.app.sites_off, frozenset())

    def test_missing_then_written_then_broken_then_removed(self):
        self.dir.mkdir()
        self.watch()
        self.assertEqual((self.app.apps_off, self.app.apps_ok), (frozenset(), True))
        self.assertEqual(self.app.events('apps_config'), [{'ok': True, 'off': 0, 'modes': 0}])
        self.write({'v': 1, 'disabled': ['chatgpt.desktop', 'window:Obsidian', applist.GRAMADRAMA_APP], 'added': []})
        self.assertEqual(self.logged(2)[-1], {'ok': True, 'off': 2, 'modes': 0})
        self.assertEqual(self.app.apps_off, frozenset({'chatgpt.desktop', 'window:Obsidian'}))
        self.write(b'{"v": 1, "disabled": [')  # broken: nothing is off (fail-open)
        self.assertEqual(self.logged(3)[-1], {'ok': False, 'off': 0, 'modes': 0})
        self.assertEqual((self.app.apps_off, self.app.apps_ok), (frozenset(), False))
        self.write(b'{"v": 1, "disabled": [')  # the same broken content again: logged once per change
        pump(lambda: False, 0.6)
        self.assertEqual(len(self.app.events('apps_config')), 3)
        self.write(b'[]')  # broken in another way: a change
        self.assertEqual(self.logged(4)[-1], {'ok': False, 'off': 0, 'modes': 0})
        self.path.unlink()
        self.assertEqual(self.logged(5)[-1], {'ok': True, 'off': 0, 'modes': 0})
        self.assertEqual((self.app.apps_off, self.app.apps_ok), (frozenset(), True))

    def test_the_modes_are_read_and_a_change_of_a_mode_alone_is_logged(self):
        self.dir.mkdir()
        self.watch()
        self.assertEqual(self.app.apps_modes, {})
        self.write({'v': 1, 'modes': {'chatgpt.desktop': 'formal', applist.GRAMADRAMA_APP: 'shorter'}})
        self.assertEqual(self.logged(2)[-1], {'ok': True, 'off': 0, 'modes': 2})
        self.assertEqual(self.app.apps_modes, {'chatgpt.desktop': 'formal', applist.GRAMADRAMA_APP: 'shorter'})
        self.write({'v': 1, 'modes': {'chatgpt.desktop': 'casual', applist.GRAMADRAMA_APP: 'shorter'}})  # only a mode
        self.assertTrue(pump(lambda: self.app.apps_modes.get('chatgpt.desktop') == 'casual'), self.app.logs)
        self.assertEqual(len(self.app.events('apps_config')), 3)  # a change, so logged again
        self.write(b'{"v": 1, "modes": ["chatgpt.desktop"]}')  # broken: no mode changes, nothing is off
        self.assertEqual(self.logged(4)[-1], {'ok': False, 'off': 0, 'modes': 0})
        self.assertEqual((self.app.apps_modes, self.app.apps_ok), ({}, False))

    def test_an_in_place_write_and_a_directory_created_later(self):
        self.watch()  # no directory yet (a first run): GLib watches for it to appear
        self.write({'v': 1, 'disabled': ['org.gnome.Ptyxis.desktop']})
        self.assertEqual(self.logged(2, timeout=10)[-1], {'ok': True, 'off': 1, 'modes': 0})
        with open(self.path, 'w', encoding='utf-8') as f:  # an editor that writes in place
            json.dump({'v': 1, 'disabled': ['org.gnome.Ptyxis.desktop', 'slack.desktop']}, f)
        self.assertTrue(pump(lambda: len(self.app.apps_off) == 2), self.app.logs)
        self.assertEqual(self.app.events('apps_config')[-1], {'ok': True, 'off': 2, 'modes': 0})

    def test_an_older_read_that_finishes_late_is_dropped(self):
        apps = self.watch()
        apps.generation = 7
        apps._loaded(6, frozenset({'chatgpt.desktop'}), {}, True, b'')
        self.assertEqual(self.app.apps_off, frozenset())
        apps._loaded(7, frozenset({'chatgpt.desktop'}), {}, True, b'')
        self.assertEqual(self.app.apps_off, frozenset({'chatgpt.desktop'}))

    def test_large_and_unreadable_files_are_broken(self):
        self.dir.mkdir()
        self.path.mkdir()  # a directory where the file should be
        self.watch()
        self.assertEqual(self.app.events('apps_config'), [{'ok': False, 'off': 0, 'modes': 0}])
        self.path.rmdir()
        self.assertEqual(self.logged(2)[-1], {'ok': True, 'off': 0, 'modes': 0})  # gone: nothing is off
        self.write(b' ' * (applist.MAX_BYTES + 10) + b'{}')  # valid JSON, but too large to parse here
        self.assertEqual(self.logged(3)[-1], {'ok': False, 'off': 0, 'modes': 0})


class SharedCorrectionTests(unittest.TestCase):
    """Transport routing uses synthetic inputs and a fake child, with no IBus connection or model."""

    def setUp(self):
        self.app = main.App(None, mock.Mock())
        self.app.live = mock.Mock()
        self.engine = self.make_engine()
        self.settings = mock.patch.object(main.speech, 'setting', side_effect=lambda name, default=True: True)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.hooks = mock.patch.object(main.backend, 'test_hooks', return_value=False)
        self.hooks.start()
        self.addCleanup(self.hooks.stop)

    def make_engine(self, sid=1):
        engine = SimpleNamespace(backend=mock.Mock(real=True, reply=None), starts={}, start_idle=0,
                                 logic=SimpleNamespace(session=SimpleNamespace(id=sid, state=main.S.WAITING)),
                                 backend_error=mock.Mock(), backend_result=mock.Mock(), _failed=mock.Mock())
        engine.app = self.app
        self.app.engines.append(engine)
        return engine

    def start(self, engine=None, mode='fix'):
        engine = engine or self.engine
        effect = main.S.StartBackend(engine.logic.session.id, 'Tekst testowy.', mode)
        self.assertTrue(self.app.start_correction(engine, effect))
        return self.app.live.send.call_args.args[0]['id']

    def result(self, rid):
        self.app.live_event({'ev': 'correction', 'id': rid, 'kind': 'result', 'corrected': 'Tekst testowy!',
                             'rows': ['Tekst testowy!'], 'segments': [['same', 'Tekst testowy'], ['add', '!']],
                             'changes': [], 'count': 1, 'languages': ['pl', 'en']})

    def test_different_input_contexts_do_not_share_session_ids(self):
        other = self.make_engine()
        first, second = self.start(), self.start(other)
        self.assertNotEqual(first, second)
        self.result(second)
        self.engine.backend_result.assert_not_called()
        other.backend_result.assert_called_once()
        self.assertEqual(other.backend_result.call_args.args[:3], (1, 'Tekst testowy!', ('Tekst testowy!',)))
        self.result(first)
        self.engine.backend_result.assert_called_once()
        self.assertFalse(self.app.corrections)

    def test_cancel_and_destroy_discard_late_results(self):
        first = self.start()
        self.assertTrue(self.app.cancel_correction(self.engine, 1))
        self.app.live.send.assert_called_with({'cmd': 'cancel_correct', 'id': first}, start=False)
        self.result(first)
        second = self.start()
        self.app.engines.remove(self.engine)
        self.result(second)
        self.engine.backend_result.assert_not_called()
        self.engine.backend_error.assert_not_called()

    def test_mode_change_ignores_an_older_manual_answer(self):
        rid = self.start()
        self.engine.logic.session.id += 1
        self.result(rid)
        self.engine.backend_result.assert_not_called()

    def test_service_death_completes_pending_requests_with_error(self):
        self.start()
        self.app.live_event({'ev': 'died'})
        self.engine.backend_error.assert_called_once_with(1, main.backend.FAILED, 'service_died')
        self.assertFalse(self.app.corrections)

    def test_service_error_ends_the_request_and_late_success_is_ignored(self):
        rid = self.start()
        self.app.live_event({'ev': 'correction', 'id': rid, 'kind': 'error', 'message': 'Ustawienia zmieniły się.',
                             'why': 'settings_changed'})
        self.engine.backend_error.assert_called_once_with(1, 'Ustawienia zmieniły się.', 'settings_changed')
        self.result(rid)
        self.engine.backend_result.assert_not_called()
        self.assertFalse(self.app.corrections)

    def test_a_log_event_of_the_service_keeps_numbers_and_a_cause_code_only(self):
        with mock.patch.object(self.app, 'log') as log:
            self.app.live_event({'ev': 'log', 'event': 'live_model_failed', 'chars': 12, 'why': 'http_503'})
            self.app.live_event({'ev': 'log', 'event': 'live_model_failed', 'chars': 12, 'why': 'Błąd modelu: Mysle ze'})
        self.assertEqual(log.call_args_list, [mock.call('live_model_failed', chars=12, why='http_503'),
                                              mock.call('live_model_failed', chars=12)])  # a message is never logged

    def test_service_revalidates_settings_and_can_request_the_standard_backend(self):
        rid = self.start()
        self.app.live_event({'ev': 'correction', 'id': rid, 'kind': 'fallback'})
        self.engine.backend.start.assert_called_once_with(1, 'Tekst testowy.', 'fix', main.GLib.idle_add, None)

    def test_only_real_experimental_fix_is_delegated(self):
        for mode in ('translate', 'improve', 'formal', 'casual', 'shorter'):
            self.assertFalse(self.app.start_correction(self.engine, main.S.StartBackend(1, 'Tekst.', mode)))
        for disabled in ('live_check', 'live_model_only'):
            with mock.patch.object(main.speech, 'setting', side_effect=lambda name, default=True: name != disabled):
                self.assertFalse(self.app.start_correction(self.engine, main.S.StartBackend(1, 'Tekst.', 'fix')))
        self.engine.backend.real = False
        self.assertFalse(self.app.start_correction(self.engine, main.S.StartBackend(1, 'Tekst.', 'fix')))
        self.engine.backend.real, self.engine.backend.reply = True, 'TEST'
        self.assertFalse(self.app.start_correction(self.engine, main.S.StartBackend(1, 'Tekst.', 'fix')))
        self.app.live.send.assert_not_called()

    def test_pending_start_reaches_service_before_the_busy_clear(self):
        self.app.live_owner = self.engine
        self.engine.starts[1] = main.S.StartBackend(1, 'Tekst testowy.', 'fix')
        callbacks = []
        with mock.patch.object(main.GLib, 'idle_add', side_effect=lambda fn: callbacks.append(fn) or len(callbacks)):
            self.app.live_command(self.engine, {'cmd': 'clear'})
            self.app.live.send.assert_not_called()
            main.Engine._start_backends(self.engine)
            callbacks.pop(0)()
        self.assertEqual([call.args[0]['cmd'] for call in self.app.live.send.call_args_list], ['correct', 'clear'])
        self.engine.backend.start.assert_not_called()

    def test_a_word_from_the_panel_starts_the_service_with_the_check_off(self):
        """Zostaw i dodaj do słownika: the live service holds the user's dictionary, also when it is not running."""
        self.app.live = None
        client = mock.Mock()
        with mock.patch.object(main.speech, 'LiveClient', return_value=client):
            self.app.live_command(self.engine, {'cmd': 'clear'})
            self.assertIsNone(self.app.live)  # a clear with nothing started
            self.app.live_command(self.engine, {'cmd': 'learn', 'word': 'Kowalsky', 'rule': ''})
        client.send.assert_called_once_with({'cmd': 'learn', 'word': 'Kowalsky', 'rule': ''}, start=True)

    def test_the_panels_lines_of_an_edited_result(self):
        """Edit(session, 'lines', JSON): only a list of offsets reaches the session; nothing else, never logged."""
        engine = SimpleNamespace(_call=mock.Mock(), logic=SimpleNamespace(edit_lines=mock.Mock()))
        main.Engine.panel_edit(engine, 3, 'lines', '[0, 20, 41]')
        engine._call.assert_called_once_with('edit', engine.logic.edit_lines, 3, [0, 20, 41])
        for kind, data in (('lines', 'not json'), ('lines', '{"a": 1}'), ('paste', '"tekst"'), ('paste', '{"id": "1"}'),
                           ('lines', '[0' + ',1' * 20000 + ']'), ('other', '[0]')):
            engine._call.reset_mock()
            main.Engine.panel_edit(engine, 3, kind, data)
            engine._call.assert_not_called()
        engine.logic.edit_paste = mock.Mock()
        main.Engine.panel_edit(engine, 3, 'paste', '{"id": 7, "text": "Ze schowka."}')
        engine._call.assert_called_once_with('edit', engine.logic.edit_paste, 3, 7, 'Ze schowka.')

    def test_deferred_clear_cannot_erase_newer_text(self):
        self.app.live_owner = self.engine
        self.engine.starts[1] = main.S.StartBackend(1, 'Tekst testowy.', 'fix')
        callbacks = []
        with mock.patch.object(main.GLib, 'idle_add', side_effect=lambda fn: callbacks.append(fn) or len(callbacks)):
            self.app.live_command(self.engine, {'cmd': 'clear'})
            self.engine.starts.clear()
            self.app.live_command(self.engine, {'cmd': 'text', 'field': 2, 'text': 'Nowy tekst.', 'caret': 11})
            callbacks.pop(0)()
        self.assertEqual([call.args[0]['cmd'] for call in self.app.live.send.call_args_list], ['text'])

    def test_queued_checking_from_an_older_text_revision_is_ignored(self):
        self.engine._call = mock.Mock()
        self.engine.logic.live_result = mock.Mock()
        self.engine.logic.live_marks = mock.Mock()
        self.app.live_command(self.engine, {'cmd': 'text', 'field': 2, 'text': 'Pierwszy tekst.', 'caret': 15})
        first = self.app.live.send.call_args.args[0]['revision']
        self.app.live_command(self.engine, {'cmd': 'text', 'field': 2, 'text': 'Nowy tekst.', 'caret': 11})
        current = self.app.live.send.call_args.args[0]['revision']
        self.assertGreater(current, first)
        self.app.live_event({'ev': 'issues', 'field': 2, 'issues': [], 'checking': True, 'revision': first})
        self.app.live_event({'ev': 'marks', 'field': 2, 'view': {}, 'revision': first})
        self.engine._call.assert_not_called()
        self.app.live_event({'ev': 'issues', 'field': 2, 'issues': [], 'checking': True, 'revision': current})
        self.engine._call.assert_called_once_with('live', self.engine.logic.live_result, 2, [], True, None)
        self.engine._call.reset_mock()
        self.app.live_event({'ev': 'issues', 'field': 2, 'issues': [], 'checking': False})
        self.engine._call.assert_called_once_with('live', self.engine.logic.live_result, 2, [], False, None)


if __name__ == '__main__':
    unittest.main()
