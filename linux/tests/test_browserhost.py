"""The browser extension's way to the engine (docs/browser.md): the native messaging host (gramadrama/browserhost.py) and the
engine's hub (gramadrama/engine/browser.py), with real Unix sockets and pipes, and no browser: the frames of the browser go
as lines to the engine and back, the host tells the extension whether the engine is there, a request carries its deadline,
and a host that loses the engine ends."""
import json
import os
import select
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest

from gramadrama import browserhost as H
from gramadrama.engine import browser as B


def read_frames(fd, until, timeout=5):
    """The frames the host wrote to its stdout until `until(frames)`; the host runs in a thread."""
    frames, buffer, end = [], b'', time.monotonic() + timeout
    while time.monotonic() < end:
        if until(frames):
            return frames
        if select.select([fd], [], [], 0.05)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buffer += chunk
        while len(buffer) >= 4:
            length = struct.unpack('=I', buffer[:4])[0]
            if len(buffer) < 4 + length:
                break
            frames.append(json.loads(buffer[4:4 + length]))
            buffer = buffer[4 + length:]
    if until(frames):
        return frames
    raise AssertionError('not seen in time: %r' % frames)


def pump(hub, until, timeout=5):
    """What the engine's loop does: accept, read, expire; until `until()`."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if until():
            return True
        readers = [hub.listener] + [c.sock for c in hub.connections]
        ready, _, _ = select.select(readers, [], [], 0.05)
        for sock in ready:
            if sock is hub.listener:
                hub.accept()
            else:
                connection = next((c for c in hub.connections if c.sock is sock), None)
                if connection is not None:
                    hub.readable(connection)
        hub.expire()
    return until()


class FramesTests(unittest.TestCase):
    def test_a_frame_is_the_length_in_native_order_and_the_json(self):
        data = H.frame({'type': 'status', 'app': True})
        self.assertEqual(struct.unpack('=I', data[:4])[0], len(data) - 4)
        self.assertEqual(json.loads(data[4:]), {'type': 'status', 'app': True})
        self.assertEqual(H.line({'a': 'ą\nb'}), b'{"a":"\xc4\x85\\nb"}\n')  # one line whatever the text holds

    def test_which_browser_started_us(self):
        for paths, name in ((['/opt/google/chrome/chrome'], 'google-chrome'), (['/usr/bin/google-chrome-stable'], 'google-chrome'),
                            (['/snap/chromium/3000/usr/lib/chromium-browser/chrome'], 'chromium'),
                            (['/opt/microsoft/msedge/msedge'], 'microsoft-edge'), (['/opt/brave.com/brave/brave'], 'brave-browser'),
                            (['/usr/bin/bash', '/opt/vivaldi/vivaldi-bin'], 'vivaldi'), (['/usr/bin/python3'], 'unknown'), ([], 'unknown')):
            self.assertEqual(H.browser_name(paths), name, paths)

    def test_the_origin_is_checked(self):
        for argv in (['host'], ['host', 'chrome-extension://another/'], ['host', '']):
            with self.subTest(argv=argv):
                self.assertEqual(H.main(argv), 1)

    def test_the_extension_has_the_id_of_its_manifest_key(self):
        """The ID of the unpacked extension comes from the key in its manifest (the same on the Mac): the host lets in that one."""
        import base64
        import hashlib
        import pathlib
        manifest = json.loads((pathlib.Path(__file__).resolve().parents[2] / 'browser' / 'extension' / 'manifest.json').read_text())
        digest = hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
        self.assertEqual(''.join(chr(ord('a') + int(c, 16)) for c in digest), H.EXTENSION_ID)


class HostAndHubTests(unittest.TestCase):
    """The host in a thread (its stdin and stdout are pipes) against a hub on a real socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'gd', 'browser.sock')
        self.events = []
        self.sites = {'old.example'}
        self.hub = B.Hub(self.path, on_change=lambda: self.events.append('change'),
                         on_site=lambda site, disabled: self.events.append(('site', site, disabled)), sites=lambda: self.sites)
        self.assertIsNotNone(self.hub.listen())
        self.addCleanup(self.hub.close)
        self.in_r, self.in_w = os.pipe()    # the browser → the host
        self.out_r, self.out_w = os.pipe()  # the host → the browser
        self.exited = threading.Event()
        self.clock = [1000.0]
        self.host = H.Host('google-chrome', stdin=self.in_r, stdout=self.out_w, socket_path=self.path, retry=0.05,
                           exit_fn=lambda code: self.exited.set(), monotonic=lambda: self.clock[0])
        self.hub.monotonic = lambda: self.clock[0]  # one clock for both, as the processes of one computer share time.monotonic()
        self.thread = threading.Thread(target=self.host.run, daemon=True)
        for fd in (self.in_w, self.out_r):
            self.addCleanup(os.close, fd)

    def start(self):
        self.thread.start()

    def say(self, obj):
        """What the extension says (a frame to the host's stdin)."""
        os.write(self.in_w, H.frame(obj))

    def frames(self, until, timeout=5):
        return read_frames(self.out_r, until, timeout)

    def introduced(self):
        self.start()
        self.say({'type': 'hello', 'version': '0.3.1'})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '0.3.1'}))

    def test_the_host_introduces_itself_then_the_extension_and_hears_the_sites(self):
        self.introduced()
        self.assertEqual(self.hub.status(), {'google-chrome': '0.3.1'})
        frames = self.frames(lambda f: any(x.get('type') == 'sites' for x in f))
        self.assertEqual(frames[0], {'type': 'status', 'app': True})  # first: the engine is there
        self.assertEqual([x for x in frames if x.get('type') == 'sites'], [{'type': 'sites', 'disabled': ['old.example']}])

    def test_the_hello_said_before_the_engine_was_there_is_repeated(self):
        """The host starts first; the extension says hello while it waits for the engine."""
        self.hub.close()
        self.start()
        self.say({'type': 'hello', 'version': '0.3.1'})
        time.sleep(0.3)  # the host cannot connect yet
        self.assertEqual(self.frames(lambda f: True, 1), [])  # and tells nothing
        self.hub = B.Hub(self.path, on_change=lambda: None, sites=lambda: ())
        self.assertIsNotNone(self.hub.listen())
        self.addCleanup(self.hub.close)
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '0.3.1'}, 8))

    def test_a_request_goes_to_the_extension_with_the_time_that_is_left_and_the_answer_comes_back(self):
        self.introduced()
        answers = []
        id_ = self.hub.request('google-chrome', 'context', answers.append, timeout=3, maxBefore=6000)
        self.assertEqual(id_, 'e1')
        request = next(x for x in self.frames(lambda f: any(x.get('op') == 'context' for x in f)) if x.get('op') == 'context')
        self.assertEqual((request['v'], request['id'], request['maxBefore']), (1, 'e1', 6000))
        self.assertNotIn('deadline', request)
        self.assertTrue(0 < request['expiresInMs'] <= 3000, request)
        self.say({'v': 1, 'id': 'e1', 'ok': True, 'before': 'Tekst ', 'selected': '', 'after': ''})
        self.assertTrue(pump(self.hub, lambda: answers))
        self.assertEqual(answers[0]['before'], 'Tekst ')

    def test_the_deadline_is_the_hosts_clock_not_the_extensions(self):
        self.introduced()
        self.hub.monotonic = lambda: 1000.0
        # Set the receiving clock before sending: the host thread may forward immediately.
        self.clock[0] = 1000.5  # simulate half a second in transit; the extension gets what is left
        self.hub.request('google-chrome', 'live', lambda a: None, timeout=2)
        request = next(x for x in self.frames(lambda f: any(x.get('op') == 'live' for x in f)) if x.get('op') == 'live')
        self.assertEqual(request['expiresInMs'], 1500)

    def test_a_write_has_a_shorter_deadline_than_its_wait(self):
        self.introduced()
        self.hub.monotonic = lambda: 1000.0
        self.clock[0] = 1000.0
        self.hub.request('google-chrome', 'replace', lambda a: None, timeout=2.5, write=1.2)
        request = next(x for x in self.frames(lambda f: any(x.get('op') == 'replace' for x in f)) if x.get('op') == 'replace')
        self.assertTrue(1100 <= request['expiresInMs'] <= 1200, request)
        self.assertEqual(self.hub.pending[next(iter(self.hub.pending))][1], 1002.5)  # the engine waits the whole timeout

    def test_a_request_nobody_answers_expires(self):
        self.introduced()
        now = [500.0]
        self.hub.monotonic = lambda: now[0]
        answers = []
        self.hub.request('google-chrome', 'context', answers.append, timeout=1)
        self.hub.expire()
        self.assertEqual(answers, [])
        now[0] = 501.5
        self.assertEqual(self.hub.expire(), None)
        self.assertEqual(answers, [{'ok': False, 'reason': 'timeout'}])
        self.hub.expire()
        self.assertEqual(len(answers), 1)  # once

    def test_the_popup_switches_a_site(self):
        self.introduced()
        self.say({'type': 'setSite', 'site': 'mail.example', 'disabled': True})
        self.assertTrue(pump(self.hub, lambda: ('site', 'mail.example', True) in self.events))
        for bad in ({'type': 'setSite', 'site': '', 'disabled': True}, {'type': 'setSite', 'site': 'x.y', 'disabled': 'yes'},
                    {'type': 'setSite', 'site': 7, 'disabled': True}, {'type': 'setSite', 'disabled': True}):
            self.say(bad)
        self.say({'type': 'setSite', 'site': 'last.example', 'disabled': False})
        self.assertTrue(pump(self.hub, lambda: ('site', 'last.example', False) in self.events))
        self.assertEqual([e for e in self.events if isinstance(e, tuple)], [('site', 'mail.example', True), ('site', 'last.example', False)])
        self.hub.push_sites({'b.example', 'a.example'})
        frames = self.frames(lambda f: [x for x in f if x.get('type') == 'sites'][-1:] == [{'type': 'sites', 'disabled': ['a.example', 'b.example']}])
        self.assertTrue(frames)

    def test_a_host_that_loses_the_engine_tells_the_extension_and_ends(self):
        self.introduced()
        self.hub.close()
        self.assertTrue(self.exited.wait(5), 'the host did not end')
        frames = self.frames(lambda f: {'type': 'status', 'app': False} in f)
        self.assertIn({'type': 'status', 'app': False}, frames)

    def test_the_host_ends_when_the_browser_closes_the_connection(self):
        self.introduced()
        os.close(self.in_w)
        self.in_w = os.open(os.devnull, os.O_WRONLY)  # the cleanup closes it
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_what_is_not_a_message_is_ignored(self):
        self.introduced()
        os.write(self.in_w, struct.pack('=I', 5) + b'{oops')
        os.write(self.in_w, H.frame([1, 2]))
        self.say({'v': 1, 'id': 'nobody-asked', 'ok': True})
        self.say({'type': 'hello', 'version': '0.3.2'})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '0.3.2'}))

    def test_a_frame_longer_than_the_limit_ends_the_hosts_reading(self):
        self.introduced()
        os.write(self.in_w, struct.pack('=I', H.MAX_MESSAGE + 1))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'gd', 'browser.sock')
        self.hub = B.Hub(self.path)
        self.addCleanup(self.hub.close)

    def client(self, *lines):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.path)
        for item in lines:
            sock.sendall(item if isinstance(item, bytes) else B.json.dumps(item).encode() + b'\n')
        self.addCleanup(sock.close)
        return sock

    def accepted(self, sock):
        self.assertTrue(pump(self.hub, lambda: len(self.hub.connections) >= 1))
        return self.hub.connections[-1]

    def test_the_socket_is_private(self):
        self.assertIsNotNone(self.hub.listen())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_interface_language_and_shortcut_reach_existing_browser_without_reconnect(self):
        preferences = {'ui_language': 'en', 'correction_shortcut': 'Ctrl+Shift+K'}
        self.hub.preferences = lambda: preferences
        self.hub.listen()
        sock = self.client({'type': 'host', 'browser': 'google-chrome'}, {'type': 'hello', 'version': '1'})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '1'}))
        sock.settimeout(1)
        reader = sock.makefile('rb')
        self.addCleanup(reader.close)
        self.assertEqual(json.loads(reader.readline())['type'], 'sites')
        self.assertEqual(json.loads(reader.readline()), dict(preferences, type='preferences'))
        preferences.update(ui_language='pl', correction_shortcut='Ctrl+Alt+G')
        self.hub.push_preferences()
        self.assertEqual(json.loads(reader.readline()), dict(preferences, type='preferences'))

    def test_a_directory_that_is_too_open_is_closed(self):
        os.makedirs(os.path.dirname(self.path), mode=0o755)
        self.assertIsNotNone(self.hub.listen())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)

    def test_a_socket_nobody_listens_on_is_replaced_and_one_somebody_listens_on_stays(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(self.path)
        dead.close()  # the file stays, nobody listens: the engine died
        self.assertIsNotNone(self.hub.listen())
        other = B.Hub(self.path)
        self.addCleanup(other.close)
        self.assertIsNone(other.listen())  # ours is alive
        self.client()  # and still ours
        self.assertTrue(pump(self.hub, lambda: self.hub.connections))

    def test_nothing_counts_before_the_host_says_who_it_is(self):
        self.hub.listen()
        sock = self.client({'type': 'hello', 'version': '9'}, {'type': 'setSite', 'site': 'x.y', 'disabled': True})
        conn = self.accepted(sock)
        pump(self.hub, lambda: False, 0.3)
        self.assertEqual((conn.browser, conn.version, self.hub.status()), (None, None, {}))
        self.assertEqual(self.hub.request('google-chrome', 'context', lambda a: None), None)

    def test_a_newer_host_of_a_browser_replaces_the_older(self):
        self.hub.listen()
        first = self.client({'type': 'host', 'browser': 'google-chrome', 'pid': 1}, {'type': 'hello', 'version': '1'})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '1'}))
        answers = []
        self.hub.request('google-chrome', 'context', answers.append)
        second = self.client({'type': 'host', 'browser': 'google-chrome', 'pid': 2}, {'type': 'hello', 'version': '2'})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '2'}))
        self.assertEqual(len(self.hub.connections), 1)
        self.assertEqual(answers, [{'ok': False, 'reason': 'disconnected'}])  # what waited on the old one is told
        edge = self.client({'type': 'host', 'browser': 'microsoft-edge', 'pid': 3}, {'type': 'hello', 'version': '3'})
        self.assertTrue(pump(self.hub, lambda: len(self.hub.status()) == 2))

    def test_an_answer_counts_only_from_the_connection_that_was_asked(self):
        self.hub.listen()
        asked = self.client({'type': 'host', 'browser': 'google-chrome', 'pid': 1}, {'type': 'hello', 'version': '1'})
        other = self.client({'type': 'host', 'browser': 'microsoft-edge', 'pid': 2}, {'type': 'hello', 'version': '1'})
        self.assertTrue(pump(self.hub, lambda: len(self.hub.status()) == 2))
        answers = []
        id_ = self.hub.request('google-chrome', 'context', answers.append)
        other.sendall(B.json.dumps({'id': id_, 'ok': True}).encode() + b'\n')
        pump(self.hub, lambda: False, 0.3)
        self.assertEqual(answers, [])  # a forged answer from another browser's host
        asked.sendall(B.json.dumps({'id': id_, 'ok': True}).encode() + b'\n')
        self.assertTrue(pump(self.hub, lambda: answers))

    def test_a_line_that_is_too_long_ends_the_connection(self):
        self.hub.listen()
        sock = self.client({'type': 'host', 'browser': 'google-chrome', 'pid': 1})
        self.accepted(sock)
        pump(self.hub, lambda: self.hub.connections and self.hub.connections[0].browser)
        sender = threading.Thread(target=lambda: self._send_quietly(sock, b'x' * (B.MAX_MESSAGE + 100)), daemon=True)
        sender.start()  # more than the socket holds: it goes while the hub reads
        self.assertTrue(pump(self.hub, lambda: not self.hub.connections))
        sender.join(5)

    @staticmethod
    def _send_quietly(sock, data):
        try:
            sock.sendall(data)
        except OSError:
            pass  # the hub closed the connection

    def test_garbage_is_ignored(self):
        self.hub.listen()
        sock = self.client(b'not json\n', b'[1,2]\n', {'type': 'host', 'browser': 'google-chrome'}, b'{"type":\n', {'type': 'hello', 'version': 7})
        self.assertTrue(pump(self.hub, lambda: self.hub.status() == {'google-chrome': '7'}))

    def test_a_request_to_a_browser_that_is_not_there_says_so(self):
        self.hub.listen()
        called = []
        self.assertIsNone(self.hub.request('google-chrome', 'context', called.append))
        self.assertEqual(called, [])



class WhichBrowserTests(unittest.TestCase):
    def test_a_focused_windows_application_is_a_browser_or_not(self):
        for app, name in (('google-chrome.desktop', 'google-chrome'), ('chromium_chromium.desktop', 'chromium'),
                          ('org.chromium.Chromium.desktop', 'chromium'), ('brave-browser.desktop', 'brave-browser'),
                          ('microsoft-edge.desktop', 'microsoft-edge'), ('vivaldi-stable.desktop', 'vivaldi'),
                          ('org.gnome.TextEditor.desktop', None), ('', None), (None, None), ('window:Obsidian', None)):
            self.assertEqual(B.browser_for_app(app), name, app)

if __name__ == '__main__':
    unittest.main()
