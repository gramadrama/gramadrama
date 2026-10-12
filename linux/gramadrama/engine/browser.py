"""The engine's side of the browser extension (docs/browser.md; pure: no gi, no core).

The extension (browser/extension) is only the app's interface in the browser: when asked it gives the text of the field
in the active tab and makes the replacements. The engine is the one that asks, as the app does on the Mac
(BrowserChannel.swift). The way: extension ⇄ native messaging ⇄ the host (browserhost.py, one per browser) ⇄ a Unix
socket, one line of JSON per message ⇄ this hub.

The hub listens on paths.browser_socket_path() (a directory 0700, the socket 0600, and every peer's uid is checked).
A host introduces itself with {"type": "host", "browser": …}; the extension then says {"type": "hello", "version": …}.
Requests are {"v": 1, "id", "op", "deadline", …} (the ops of ImeProtocol: hello, context, live, rects, replace, insert); the
answer carries the same "id". {"type": "sites", "disabled": […]} tells the extension the sites where GramaDrama is off, and
{"type": "setSite", "site", "disabled"} from the popup of the extension changes that list.

No loop of its own: whoever runs it (main.py: GLib) calls accept() when the listening socket is readable, readable(conn) when
a connection is, and expire() now and then. Everything is non-blocking."""
import json
import os
import socket
import stat
import struct
import time

from .. import paths

MAX_MESSAGE = 1024 * 1024
DEFAULT_TIMEOUT = 3.0  # seconds a request waits for its answer
VERSION = 1


def browser_for_app(app_id):
    """The browser (the name its host reports, browser_name) of a focused window's application ID (a desktop ID such as
    google-chrome.desktop, chromium_chromium.desktop, org.chromium.Chromium.desktop, brave-browser.desktop), or None."""
    low = (app_id or '').lower()
    for part, name in (('google-chrome', 'google-chrome'), ('microsoft-edge', 'microsoft-edge'), ('msedge', 'microsoft-edge'),
                       ('brave', 'brave-browser'), ('vivaldi', 'vivaldi'), ('opera', 'opera'), ('chromium', 'chromium')):
        if part in low:
            return name
    return None


class Connection:
    """One host: the extension of one browser."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b''
        self.browser = None  # set by the host's introduction
        self.pid = None
        self.version = None  # the extension's, from its "hello"

    def fileno(self):
        return self.sock.fileno()


class Hub:
    def __init__(self, path=None, on_change=lambda: None, on_site=lambda site, disabled: None, sites=lambda: (),
                 monotonic=time.monotonic, preferences=lambda: {}):
        self.path = str(path or paths.browser_socket_path())
        self.on_change, self.on_site, self.sites = on_change, on_site, sites  # sites(): the disabled ones, for a new extension
        self.monotonic = monotonic
        self.preferences = preferences
        self.listener = None
        self.connections = []  # all, introduced or not
        self.pending = {}  # id -> (callback, deadline, connection)
        self.counter = 0

    # ---- the socket ----
    def listen(self):
        """Start listening; the fd to watch, or None when the socket cannot be made (a browser extension then has no engine)."""
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            if stat.S_IMODE(os.stat(directory).st_mode) != 0o700 or os.stat(directory).st_uid != os.getuid():
                os.chmod(directory, 0o700)
            self._clear_stale()
        except OSError:
            return None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(self.path)
            sock.listen(8)
            sock.setblocking(False)
        except OSError:
            sock.close()
            return None
        finally:
            os.umask(old)
        self.listener = sock
        return sock.fileno()

    def _clear_stale(self):
        """A socket file nobody listens on (the engine died) is removed; one somebody listens on stays (the bind then fails)."""
        if not os.path.exists(self.path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(self.path)
        except OSError:
            os.unlink(self.path)
        finally:
            probe.close()

    def accept(self):
        """The new connection, or None (nothing to accept, or a peer of another user)."""
        try:
            sock, _ = self.listener.accept()
        except (BlockingIOError, InterruptedError, OSError):
            return None
        try:
            _pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            if uid != os.getuid():
                raise OSError('not ours')
            sock.setblocking(False)
        except OSError:
            sock.close()
            return None
        connection = Connection(sock)
        self.connections.append(connection)
        return connection

    def close(self):
        for connection in list(self.connections):
            self._drop(connection)
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            try:
                os.unlink(self.path)
            except OSError:
                pass

    # ---- what comes in ----
    def readable(self, connection):
        """Read what the connection has and act on its lines. False: it is closed (and gone from the hub)."""
        try:
            chunk = connection.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            chunk = b''
        if not chunk:
            self._drop(connection)
            return False
        connection.buffer += chunk
        while True:
            newline = connection.buffer.find(b'\n')
            if newline < 0:
                break
            data, connection.buffer = connection.buffer[:newline], connection.buffer[newline + 1:]
            self._line(connection, data)
        if len(connection.buffer) > MAX_MESSAGE:
            self._drop(connection)
            return False
        return True

    def _line(self, connection, data):
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get('type')
        if connection.browser is None:
            if kind == 'host' and isinstance(message.get('browser'), str) and message['browser']:
                self._introduce(connection, message)
            return  # nothing counts before the host says who it is
        if kind == 'hello':
            connection.version = str(message.get('version') or '?')[:40]
            self.send(connection, {'type': 'sites', 'disabled': sorted(self.sites())})
            self._send_preferences(connection)
            self.on_change()
        elif kind == 'setSite':
            site, disabled = message.get('site'), message.get('disabled')
            if isinstance(site, str) and site and len(site) <= 253 and isinstance(disabled, bool):
                self.on_site(site, disabled)
        elif isinstance(message.get('id'), str):
            waiting = self.pending.get(message['id'])
            if waiting is not None and waiting[2] is connection:  # an answer from another host does not take its place
                del self.pending[message['id']]
                waiting[0](message)

    def _introduce(self, connection, message):
        """A browser has one host: a newer one replaces the older."""
        for other in list(self.connections):
            if other is not connection and other.browser == message['browser']:
                self._drop(other)
        connection.browser = message['browser'][:60]
        connection.pid = message.get('pid') if isinstance(message.get('pid'), int) else None
        self.on_change()

    def _drop(self, connection):
        if connection in self.connections:
            self.connections.remove(connection)
        try:
            connection.sock.close()
        except OSError:
            pass
        for id_, (callback, _deadline, owner) in list(self.pending.items()):
            if owner is connection:
                del self.pending[id_]
                callback({'ok': False, 'reason': 'disconnected'})  # a write may have landed before
        if connection.browser is not None:
            self.on_change()

    # ---- what goes out ----
    def send(self, connection, message):
        data = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        try:
            connection.sock.sendall(data)
            return True
        except (BlockingIOError, OSError):
            self._drop(connection)  # a host that cannot keep up is gone as far as we are concerned
            return False

    def connection_of(self, browser):
        return next((c for c in self.connections if c.browser == browser and c.version is not None), None)

    def request(self, browser, op, callback, timeout=DEFAULT_TIMEOUT, write=None, **fields):
        """Ask the extension of a browser (its name as the host gave it). callback(answer dict) is called once: with the
        answer, with {"ok": False, "reason": "timeout"} after the timeout (expire()), or {"reason": "disconnected"}.
        write: the seconds the extension has to start a write (its deadline; the timeout by default), shorter than the timeout
        so that a write never lands after the engine has stopped waiting for it.
        Returns the id, or None when that browser has no extension connected (callback is not called then)."""
        connection = self.connection_of(browser)
        if connection is None:
            return None
        self.counter += 1
        id_ = 'e%d' % self.counter
        now = self.monotonic()
        deadline = now + timeout
        self.pending[id_] = (callback, deadline, connection)
        if not self.send(connection, dict(fields, v=VERSION, id=id_, op=op, deadline=now + min(write or timeout, timeout))):
            return None
        return id_

    def expire(self):
        """Answer the requests whose deadline has passed; the seconds until the next one (None: nothing waits)."""
        now = self.monotonic()
        for id_, (callback, deadline, _owner) in list(self.pending.items()):
            if deadline <= now:
                del self.pending[id_]
                callback({'ok': False, 'reason': 'timeout'})  # likewise: no answer is no refusal
        return min((d for _, d, _ in self.pending.values()), default=None)

    def push_sites(self, disabled):
        for connection in list(self.connections):
            if connection.browser is not None and connection.version is not None:
                self.send(connection, {'type': 'sites', 'disabled': sorted(disabled)})

    def _send_preferences(self, connection):
        preferences = self.preferences()
        if preferences:
            self.send(connection, dict(preferences, type='preferences'))

    def push_preferences(self):
        for connection in list(self.connections):
            if connection.browser is not None and connection.version is not None:
                self._send_preferences(connection)

    def status(self):
        """For the engine's diagnostics: {browser: extension version} of the extensions that are connected."""
        return {c.browser: c.version for c in self.connections if c.browser is not None and c.version is not None}
