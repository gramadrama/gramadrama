"""The native messaging host of the browser extension (browser/extension, docs/browser.md).

The browser (Chrome, Chromium, Edge, Brave…) starts it for as long as the extension holds its connection
(`chrome.runtime.connectNative('pl.gramadrama.browser')`), with the extension's origin as the first argument. It understands
nothing: the frames of the browser (4 bytes of length, native order, and JSON) go as lines of JSON to the engine's socket
(paths.browser_socket_path()) and back, as the Mac's GramaBrowserHost does for the app. While the engine is not there it
waits for it and tells the extension that it is not (`{"type": "status", "app": false}`). When the engine's connection ends
(the engine was restarted or replaced) the host ends too, and the extension starts a new one at once.

The requests of the engine carry their deadline as time.monotonic() seconds (the clock all the processes of this computer
share), and the extension gets how many ms are left (`expiresInMs`).

Pure: standard library only, no gi."""
import json
import os
import socket
import struct
import sys
import threading
import time

from . import paths

EXTENSION_ID = 'hlnpnkiancoohmafifihcjndfmhilkfe'  # from the key in browser/extension/manifest.json: the same in every Chromium browser
EXTENSION_ORIGIN = 'chrome-extension://%s/' % EXTENSION_ID
HOST_NAME = 'pl.gramadrama.browser'
MAX_MESSAGE = 1024 * 1024  # larger than the panel's limit: the answer to `context` carries the text around the caret
RETRY_SECONDS = 2
# (name, the browser's configuration directory under ~/.config): where its NativeMessagingHosts/ is; shared by the installer, which
# registers the host there, and the app's window, which says whether it is. A browser in a snap or a Flatpak is confined and
# cannot start a host outside its sandbox: those are not covered.
BROWSER_CONFIGS = (('Google Chrome', 'google-chrome'), ('Chromium', 'chromium'), ('Microsoft Edge', 'microsoft-edge'),
                   ('Brave', 'BraveSoftware/Brave-Browser'), ('Vivaldi', 'vivaldi'))
# The names the host reports (browser_name) and how the window shows them.
BROWSER_TITLES = {'google-chrome': 'Google Chrome', 'chromium': 'Chromium', 'microsoft-edge': 'Microsoft Edge',
                  'brave-browser': 'Brave', 'vivaldi': 'Vivaldi', 'opera': 'Opera', 'unknown': 'przeglądarka'}
# What the executable of an ancestor process says about the browser: (a part of its path, the browser's name), first match wins.
BROWSERS = (('google/chrome', 'google-chrome'), ('google-chrome', 'google-chrome'), ('microsoft-edge', 'microsoft-edge'),
            ('msedge', 'microsoft-edge'), ('brave', 'brave-browser'), ('vivaldi', 'vivaldi'), ('opera', 'opera'),
            ('chromium', 'chromium'))


def browser_name(paths_of_ancestors):
    for path in paths_of_ancestors:
        low = path.lower()
        for part, name in BROWSERS:
            if part in low:
                return name
    return 'unknown'


def ancestor_paths(pid=None, depth=6):
    """The executables (and first arguments) of the processes above this one, nearest first."""
    pid = pid or os.getppid()
    found = []
    for _ in range(depth):
        if pid <= 1:
            break
        try:
            found.append(os.path.realpath('/proc/%d/exe' % pid))
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                found.append(f.read().split(b'\0')[0].decode('utf-8', 'replace'))
            with open('/proc/%d/stat' % pid, encoding='utf-8', errors='replace') as f:
                pid = int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return found


def frame(obj):
    """A message for the browser: 4 bytes of length (native order) and the JSON."""
    body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return struct.pack('=I', len(body)) + body


def line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'


def read_exactly(fd, count):
    data = b''
    while len(data) < count:
        try:
            chunk = os.read(fd, count - len(data))
        except InterruptedError:
            continue
        if not chunk:
            return None
        data += chunk
    return data


class LineReader:
    """Lines of a socket that stays open; a line longer than the limit ends the connection."""

    def __init__(self, sock, max_bytes=MAX_MESSAGE):
        self.sock, self.max_bytes, self.buffer = sock, max_bytes, b''

    def next(self):
        while True:
            newline = self.buffer.find(b'\n')
            if newline >= 0:
                data, self.buffer = self.buffer[:newline], self.buffer[newline + 1:]
                return data
            if len(self.buffer) > self.max_bytes:
                return None
            try:
                chunk = self.sock.recv(65536)
            except (InterruptedError, socket.timeout):
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self.buffer += chunk


def connect(path):
    """The engine's socket, when it is there and is the engine of this user; else None."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
        _pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != os.getuid():
            raise OSError('not ours')
        sock.settimeout(1)  # a send that takes longer ends the connection; reads retry (LineReader)
        return sock
    except OSError:
        sock.close()
        return None


class Host:
    def __init__(self, browser, stdin=0, stdout=1, socket_path=None, retry=RETRY_SECONDS, exit_fn=os._exit,
                 monotonic=time.monotonic):
        self.browser, self.stdin, self.stdout = browser, stdin, stdout
        self.socket_path = socket_path or paths.browser_socket_path()
        self.retry, self.exit_fn, self.monotonic = retry, exit_fn, monotonic
        self.lock = threading.Lock()  # for the writes to the browser and to the engine
        self.app = None  # the engine's connection (None: it is not there)
        self.hello = None  # the extension's last "hello", repeated after every (re)connection to the engine

    # ---- to the browser ----
    def write_frame(self, obj):
        data = frame(obj)
        with self.lock:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self.stdout, view):]
            except OSError:
                self.exit_fn(0)  # the browser is gone

    def status(self, connected):
        self.write_frame({'type': 'status', 'app': connected})

    # ---- the engine → the browser ----
    def engine_loop(self):
        while True:
            sock = connect(self.socket_path)
            if sock is None:
                time.sleep(self.retry)
                continue
            with self.lock:
                self.app = sock
                try:
                    sock.sendall(line({'type': 'host', 'browser': self.browser, 'pid': os.getpid()}))
                    if self.hello:
                        sock.sendall(self.hello)
                except OSError:
                    pass
            self.status(True)
            reader = LineReader(sock)
            while True:
                data = reader.next()
                if data is None:
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get('deadline'), (int, float)):
                    obj['expiresInMs'] = max(0, int((obj.pop('deadline') - self.monotonic()) * 1000))
                self.write_frame(obj)
            with self.lock:
                self.app = None
            sock.close()
            self.status(False)
            self.exit_fn(0)  # a host that lost its engine ends: the extension starts a new one
            return

    # ---- the browser → the engine ----
    def browser_loop(self):
        """The frames of the browser until it closes the connection (the end of stdin)."""
        while True:
            header = read_exactly(self.stdin, 4)
            if header is None:
                return
            length = struct.unpack('=I', header)[0]
            if not 0 < length <= MAX_MESSAGE:
                return
            body = read_exactly(self.stdin, length)
            if body is None:
                return
            try:
                obj = json.loads(body)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            data = line(obj)  # one line, whatever spaces the JSON of the browser had
            with self.lock:
                if obj.get('type') == 'hello':
                    self.hello = data
                if self.app is not None:
                    try:
                        self.app.sendall(data)
                    except OSError:
                        try:
                            self.app.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

    def run(self):
        threading.Thread(target=self.engine_loop, name='engine', daemon=True).start()
        self.browser_loop()


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    # The browser gives the origin of the extension; the manifest of the host lets in only ours, this is the second check.
    if len(argv) < 2 or argv[1] != EXTENSION_ORIGIN:
        sys.stderr.write('gramadrama-browser-host: unknown origin %s\n' % (argv[1] if len(argv) > 1 else '-'))
        return 1
    Host(browser_name(ancestor_paths())).run()
    return 0
