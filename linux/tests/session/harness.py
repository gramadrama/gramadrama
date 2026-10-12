"""Isolated headless GNOME session for IBus engine tests.

    import sys; sys.path.insert(0, '<linux>/tests/session')
    from harness import Session
    with Session(sources=[('xkb', 'pl')]) as s:
        win = s.spawn_gtk(4)
        s.type_text('Zażółć')
        s.wait_until(lambda: win.text('tv').startswith('Zażółć'))

Everything runs under its own dbus-run-session with XDG_* dirs and HOME in a fresh
/tmp/gd-XXXXXXXX directory, a private (empty) system bus by default, and a headless
gnome-shell with its own Wayland display and Xwayland. The live desktop is never touched:
the harness process itself imports no gi and makes no D-Bus calls; every in-session action
goes through ctl.py over a Unix socket in the work dir. Teardown is owned by supervise.py
(see there). No gi import here: this module is safe to import anywhere.
"""
import glob
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape as xml_escape

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import keyseq as K  # noqa: E402
import supervise  # noqa: E402

DEFAULT_TEMPLATE = os.path.join(LINUX, 'data', 'gramadrama.xml.in')
DEFAULT_EXEC = '/usr/bin/python3 %s --ibus' % os.path.join(LINUX, 'gramadrama-engine')
FIXTURE = os.path.join(HERE, 'fixture.html')
EXTENSION = 'gd-harness@local'
KEEP_ENV = ('PATH', 'LANG', 'LANGUAGE', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'TZ', 'XDG_CONFIG_DIRS',
            'XDG_DATA_DIRS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'XDG_SESSION_TYPE', 'XDG_MENU_PREFIX',
            'DESKTOP_SESSION', 'GDMSESSION', 'QT_IM_MODULE', 'QT_IM_MODULES', 'XMODIFIERS', 'GTK_IM_MODULE',
            'GTK_MODULES')
SYSTEM_COMPONENTS = '/usr/share/ibus/component'
TMP_LITTER = ('/tmp/.org.chromium.Chromium.*', '/tmp/com.google.Chrome.*', '/tmp/org.chromium.Chromium.*')


def tmp_litter():
    return {p for pattern in TMP_LITTER for p in glob.glob(pattern)}


PROTECTED = re.compile(r'^(TMPDIR|XDG_(CONFIG|DATA|CACHE|STATE)_HOME|XDG_RUNTIME_DIR|HOME|DBUS_.*|DISPLAY|WAYLAND_DISPLAY|'
                       r'XAUTHORITY|IBUS_ADDRESS|IBUS_COMPONENT_PATH|GD_(WORK|REAL_DBUS|WL|HERE|SOURCES|EXTENSION|'
                       r'LOGDIR|SIZE|MAX_SECONDS|SYSBUS_CONF|SHELL_ARGS|ENABLED_EXTENSIONS))$')
EXTENSION_UUID = re.compile(r'^[A-Za-z0-9._@-]+$')
CHROME_FLAGS = ['--no-first-run', '--no-default-browser-check', '--password-store=basic', '--test-type',
                '--disable-background-networking', '--disable-component-update', '--disable-sync',
                '--disable-default-apps', '--disable-extensions', '--disable-domain-reliability', '--no-pings',
                '--disable-breakpad', '--disable-features=MediaRouter,OptimizationHints,Translate',
                '--enable-logging=stderr', '--v=0', '--window-position=0,0', '--window-size=1200,760']
EXTENSION_JS = """import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';
export default class GdHarness extends Extension {
    enable() {
        global.context.unsafe_mode = true;  // org.gnome.Shell.Eval / Screenshot for the test driver
        globalThis.gdMain = Main;
        globalThis.gdKbd = Keyboard;
    }
    disable() {}
}
"""
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <keep_umask/>
  <listen>unix:path=%s</listen>
  <auth>EXTERNAL</auth>
  %s
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
  <limit name="service_start_timeout">120000</limit>
  <limit name="auth_timeout">240000</limit>
</busconfig>
"""
WINDOWS_JS = """global.get_window_actors().map(a => { const w = a.meta_window;
  const b = w.get_buffer_rect(), f = w.get_frame_rect();
  return {title: w.get_title(), wm_class: w.get_wm_class(), pid: w.get_pid(), focus: w.has_focus(),
          x11: w.get_client_type() === 1, buffer: [b.x, b.y, b.width, b.height], frame: [f.x, f.y, f.width, f.height]}; })"""
BUTTONS = {'left': 272, 'right': 273, 'middle': 274}


class HarnessError(RuntimeError):
    pass


def wait_until(pred, timeout=5.0, interval=0.05, what=None):
    """Poll pred() until it returns a truthy value (returned) or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError('timed out after %.1fs waiting for %s' % (timeout, what or getattr(pred, '__name__', 'condition')))
        time.sleep(interval)


def expected_engine(source):
    kind, sid = source
    if kind == 'ibus':
        return sid
    name, _, variant = sid.partition('+')
    return 'xkb:%s:%s:' % (name, variant)


class Proc:
    """A process spawned inside the session (by ctl.py, in its own process group)."""

    def __init__(self, session, pid, name, log):
        self.session, self.pid, self.name, self.log = session, pid, name, log

    def alive(self):
        return os.path.exists('/proc/%d' % self.pid) and not supervise.is_zombie(self.pid)

    def kill(self, sig=signal.SIGTERM, group=True, timeout=5):
        """Signal the process (and its process group); verified to belong to the session."""
        if not self.alive():
            return
        if not supervise.belongs(self.pid, self.session.work) and self.pid not in self.session.pids():
            raise HarnessError('pid %d is not part of the session' % self.pid)
        try:
            os.killpg(self.pid, sig) if group else os.kill(self.pid, sig)
        except ProcessLookupError:
            pass
        wait_until(lambda: not self.alive(), timeout, what='%s to exit' % self.name)

    def read_log(self):
        try:
            return open(self.log, encoding='utf-8', errors='replace').read()
        except OSError:
            return ''


class TestWin(Proc):
    """GTK 3/4 fixture window: D-Bus control + JSONL ground truth."""

    def call(self, method, sig=None, args=None, timeout=10000):
        r = self.session.call(self.bus_name, '/org/gramadrama/TestWin', 'org.gramadrama.TestWin',
                              method, sig, args or [], timeout)
        return json.loads(r[0]) if r else None

    def focus(self, widget):
        return self.call('Focus', '(s)', [widget])

    def select(self, widget, start, end):
        return self.call('Select', '(sii)', [widget, start, end])

    def set_text(self, widget, text):
        return self.call('SetText', '(ss)', [widget, text])

    def get(self, widget):
        return self.call('Get', '(s)', [widget])

    def text(self, widget):
        return self.get(widget)['text']

    def info(self):
        return self.call('Info')

    def present(self):
        return self.call('Present')

    def window(self):
        return self.session.window(self.title)

    def locate(self, offset, widget='tv'):
        """Screen coordinates (x, y) of the character at offset in the TextView."""
        p = self.call('Locate', '(si)', [widget, offset])
        bx, by = self.window()['buffer'][:2]
        return bx + p['x'], by + p['y']

    def rect(self, widget):
        r = self.call('Rect', '(s)', [widget])
        bx, by = self.window()['buffer'][:2]
        return bx + r['x'], by + r['y'], r['w'], r['h']

    def events(self, since=0):
        try:
            with open(self.log, encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            return []
        return [json.loads(l) for l in lines[since:] if l.endswith('\n') and l.strip()]

    def mark(self):
        return len(self.events())

    def keys_since(self, mark, phase='capture'):
        return [(e['type'], e['key']) for e in self.events(mark) if e['ev'] == 'key' and e['phase'] == phase]

    def wait_event(self, pred, since=0, timeout=5):
        return wait_until(lambda: next((e for e in self.events(since) if pred(e)), None), timeout, what='testwin event')

    def wait_text(self, widget, text, timeout=5):
        wait_until(lambda: self.text(widget) == text, timeout, what='%s text == %r' % (widget, text))


class Chrome(Proc):
    """Chrome instance with an isolated profile; ground truth from the fixture's console log."""
    RX = re.compile(r'"GDLOG (.*)", source: ')
    cdp_dir = None
    _commands = 0

    def eval_extension(self, expression, timeout=15):
        """JavaScript run in the service worker of the loaded extension (chrome_ext.py); its value (JSON)."""
        Chrome._commands += 1
        number = Chrome._commands
        with open(os.path.join(self.cdp_dir, 'cmd-%d.json' % number), 'w') as f:
            json.dump({'op': 'eval_sw', 'expression': expression}, f)
        path = os.path.join(self.cdp_dir, 'res-%d.json' % number)
        wait_until(lambda: os.path.exists(path), timeout, what='the extension answer')
        outcome = json.load(open(path))
        if not outcome['ok']:
            raise HarnessError('extension eval failed: %r' % (outcome['result'],))
        return outcome['result'].get('result', {}).get('value')

    def events(self, since=0):
        out = []
        for line in self.read_log().splitlines():
            m = self.RX.search(line)
            if m:
                try:
                    out.append(json.loads(m.group(1)))
                except ValueError:
                    pass
        return out[since:]

    def mark(self):
        return len(self.events())

    def wait_event(self, pred, since=0, timeout=10):
        return wait_until(lambda: next((e for e in self.events(since) if pred(e)), None), timeout, what='page event')

    def keys_since(self, mark):
        return [(e['ev'], e['key']) for e in self.events(mark) if e['ev'] in ('keydown', 'keyup')]

    def state(self, timeout=5):
        """Fresh state of all fixture fields (F10 macro)."""
        n = self.mark()
        self.session.tap('f10')
        return self.wait_event(lambda e: e['ev'] == 'dump', n, timeout)['state']

    def last_state(self):
        return next((e['state'] for e in reversed(self.events()) if 'state' in e), None)


class TextEditor(Proc):
    def read(self):
        return open(self.path, encoding='utf-8').read()

    def save(self, timeout=5):
        before = os.stat(self.path).st_mtime_ns
        self.session.combo('ctrl', 's')
        wait_until(lambda: os.stat(self.path).st_mtime_ns != before, timeout, what='file save')
        time.sleep(0.1)
        return self.read()


class Session:
    """Context manager for one isolated session. See README.md for the full API."""

    def __init__(self, sources=(('xkb', 'pl'),), engine=None, component_template=None, engine_exec=None,
                 env=None, name=None, logs_dir=None, copy_logs_to=None, size='1280x800', max_seconds=1800,
                 system_bus=False, xwayland=True, start_timeout=90, verbose=False, extensions=()):
        self.sources = [tuple(s) for s in sources]
        if component_template is None and (engine or (engine is None and ('ibus', 'gramadrama') in self.sources)):
            component_template = DEFAULT_TEMPLATE
        if component_template and not os.path.exists(component_template):
            raise HarnessError('component template %s does not exist' % component_template)
        self.component_template = component_template
        self.engine_exec = engine_exec or DEFAULT_EXEC
        self.user_env = dict(env or {})
        bad = [k for k in self.user_env if PROTECTED.match(k)]
        if bad:
            raise HarnessError('env may not override %s' % bad)
        self.name = re.sub(r'[^a-z0-9]', '', (name or '').lower())[:12]
        self.logs_arg, self.copy_logs_to = logs_dir, copy_logs_to
        self.size, self.max_seconds, self.system_bus, self.xwayland = size, max_seconds, system_bus, xwayland
        self.start_timeout, self.verbose = start_timeout, verbose
        self.extensions = self._extension_dirs(extensions)
        self.work = self.sup = None
        self.leftovers = self.report = None
        self.tmp_before, self.tmp_new = set(), []
        self.procs = []
        self._counter = 0
        self._old_sigterm = None

    @staticmethod
    def _extension_dirs(extensions):
        """[(uuid, dir)] of extra GNOME Shell extensions to install into the session and enable next
        to the harness's own; each dir needs a metadata.json with a plain uuid."""
        out = []
        for path in extensions:
            src = os.path.realpath(path)
            try:
                uuid = json.load(open(os.path.join(src, 'metadata.json'), encoding='utf-8'))['uuid']
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise HarnessError('extension %s: no usable metadata.json (%r)' % (path, exc))
            if not isinstance(uuid, str) or not EXTENSION_UUID.match(uuid) or uuid == EXTENSION or \
                    uuid in (u for u, _ in out):
                raise HarnessError('extension %s: bad or duplicate uuid %r' % (path, uuid))
            out.append((uuid, src))
        return out

    # ---- lifecycle ------------------------------------------------------------------
    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def log(self, msg):
        if self.verbose:
            sys.stderr.write('[harness] %s\n' % msg)

    def start(self):
        self.real_bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
        self.x_before = sorted(glob.glob('/tmp/.X*-lock'))
        w = os.path.realpath(tempfile.mkdtemp(prefix='gd-', dir='/tmp'))
        self.logs = self.logs_arg or os.path.join(w, 'logs')
        self.display = 'gdwl-%s%s' % (self.name + '-' if self.name else '', w[len('/tmp/gd-'):])
        self.work = w
        # TMPDIR keeps Chrome/Electron temp files (hundreds per run) out of the shared tmpfs /tmp.
        xdg = {'XDG_CONFIG_HOME': w + '/config', 'XDG_DATA_HOME': w + '/data', 'XDG_CACHE_HOME': w + '/cache',
               'XDG_STATE_HOME': w + '/state', 'XDG_RUNTIME_DIR': w + '/rt', 'HOME': w + '/home',
               'TMPDIR': w + '/tmp'}
        self.tmp_before = tmp_litter()
        for d in list(xdg.values()) + [self.logs]:
            os.makedirs(d, exist_ok=True)
        os.chmod(xdg['XDG_RUNTIME_DIR'], 0o700)
        ext = os.path.join(xdg['XDG_DATA_HOME'], 'gnome-shell', 'extensions', EXTENSION)
        os.makedirs(ext)
        with open(os.path.join(ext, 'metadata.json'), 'w') as f:
            json.dump({'uuid': EXTENSION, 'name': 'GD harness', 'description': 'test only',
                       'shell-version': ['50']}, f)
        with open(os.path.join(ext, 'extension.js'), 'w') as f:
            f.write(EXTENSION_JS)
        enabled = [EXTENSION]
        for uuid, src in self.extensions:
            # A copy (not a symlink): the session never sees edits made while it runs.
            shutil.copytree(src, os.path.join(xdg['XDG_DATA_HOME'], 'gnome-shell', 'extensions', uuid),
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            enabled.append(uuid)
        with open(os.path.join(w, 'bus.conf'), 'w') as f:
            f.write(BUS_CONF % (w + '/bus', '<standard_session_servicedirs/>'))
        env = {k: v for k, v in os.environ.items() if k in KEEP_ENV or k.startswith('LC_')}
        env.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
        env.setdefault('XDG_SESSION_TYPE', 'wayland')
        env.setdefault('XDG_CURRENT_DESKTOP', 'GNOME')
        env.update(xdg)
        env.update(GD_WORK=w, GD_REAL_DBUS=self.real_bus, GD_WL=self.display, GD_HERE=HERE,
                   GD_SOURCES='[%s]' % ', '.join("('%s', '%s')" % s for s in self.sources),
                   GD_EXTENSION=EXTENSION, GD_ENABLED_EXTENSIONS='[%s]' % ', '.join("'%s'" % u for u in enabled),
                   GD_LOGDIR=self.logs, GD_SIZE=self.size,
                   GD_MAX_SECONDS=str(self.max_seconds + 60), GVFS_DISABLE_FUSE='1',
                   GD_SHELL_ARGS='' if self.xwayland else '--no-x11')
        if not self.system_bus:
            with open(os.path.join(w, 'sysbus.conf'), 'w') as f:
                f.write(BUS_CONF.replace('<type>session</type>', '<type>system</type>') % (w + '/sysbus', ''))
            env['GD_SYSBUS_CONF'] = w + '/sysbus.conf'
            env['DBUS_SYSTEM_BUS_ADDRESS'] = 'unix:path=%s/sysbus' % w
        # The system's IBus components, minus an installed GramaDrama (/usr/share/ibus/component/
        # gramadrama.xml after a live install): it would win over this session's engine, and the
        # tests would run the installed copy instead of the code under test.
        comp = os.path.join(w, 'component')
        os.makedirs(comp)
        for path in glob.glob(os.path.join(SYSTEM_COMPONENTS, '*.xml')):
            if os.path.basename(path) != 'gramadrama.xml':
                os.symlink(path, os.path.join(comp, os.path.basename(path)))
        if self.component_template:
            xml = open(self.component_template, encoding='utf-8').read().replace('@EXEC@', xml_escape(self.engine_exec))
            # Desktop fixtures use the Polish keymap independently of the text-language profile.
            xml = xml.replace('@LAYOUT@', 'pl').replace('@LANGUAGE@', 'pl').replace('@LONGNAME@', 'Polski (GramaDrama)')
            with open(os.path.join(comp, 'gramadrama.xml'), 'w', encoding='utf-8') as f:
                f.write(xml)
        env['IBUS_COMPONENT_PATH'] = comp
        env.setdefault('GRAMADRAMA_ENGINE_LOG', os.path.join(self.logs, 'engine.log'))
        env.update(self.user_env)
        self.env = env
        cfg = {'work': w, 'env': env, 'bus_conf': w + '/bus.conf', 'session_sh': os.path.join(HERE, 'session.sh'),
               'logs': self.logs, 'max_seconds': self.max_seconds, 'copy_logs_to': self.copy_logs_to,
               'x_before': self.x_before}
        with open(os.path.join(w, 'config.json'), 'w') as f:
            json.dump(cfg, f)
        if threading_main() and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
            self._old_sigterm = signal.signal(signal.SIGTERM, _sigterm)
        self.sup = subprocess.Popen(['/usr/bin/python3', os.path.join(HERE, 'supervise.py'), w + '/config.json'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(self.logs + '/supervise.err', 'ab'),
                                    text=True, start_new_session=True, cwd=w)
        self.log('work=%s display=%s supervisor=%d' % (w, self.display, self.sup.pid))
        self._wait_ready()

    def _wait_ready(self):
        t0 = time.monotonic()
        deadline = t0 + self.start_timeout
        sock = os.path.join(self.work, 'ctl.sock')

        def alive_check():
            if self.sup.poll() is not None:
                raise HarnessError('supervisor exited early:\n' + self.tail('session.log'))
            if time.monotonic() > deadline:
                raise HarnessError('session did not start in %ds:\n%s' % (self.start_timeout, self.tail('session.log')))

        while True:
            alive_check()
            if os.path.exists(sock):
                try:
                    info = self.ctl({'op': 'ping'}, timeout=5)
                    break
                except (OSError, HarnessError):
                    pass
            tail = self.tail('session.log')
            if 'ABORT' in tail or 'ctl exited' in tail:
                raise HarnessError('session aborted:\n%s\n%s' % (tail, self.tail('ctl.log')))
            time.sleep(0.1)
        if not info['bus'] or info['bus'] == self.real_bus or self.work + '/' not in info['bus']:
            raise HarnessError('in-session bus is not isolated: %r' % info['bus'])
        self.bus_address = info['bus']
        while True:
            alive_check()
            r = self._try(lambda: self.eval('!!globalThis.gdMain && !gdMain.layoutManager._startingUp', raw=True))
            if r and r[0] and r[1] == 'true':
                break
            time.sleep(0.2)
        self.eval('gdMain.overview.hide(); true', raw=True)
        want = expected_engine(self.sources[0])
        while True:
            # With Xwayland, gnome-shell restarts ibus-daemon with --xim once X is up: wait for it.
            alive_check()
            eng = None
            if not self.xwayland or (self.pids(r'^\S*Xwayland ') and self.pids(r'^ibus-daemon .*--xim')):
                eng = (self._try(self.ibus_engine_status) or {}).get('engine')
            if eng and eng.startswith(want):
                break
            time.sleep(0.2)
        # RemoteDesktop session + virtual keyboard now: windows get keyboard focus (is-active) at map.
        self.ctl({'op': 'warmup'})
        self.log('ready in %.1fs (engine %s)' % (time.monotonic() - t0, eng))

    def stop(self):
        """Tear everything down (idempotent); sets self.report and self.leftovers."""
        if self.work is None or self.leftovers is not None:
            return
        report = None
        sup, self.sup = self.sup, None
        if sup is not None:
            try:
                if sup.poll() is None:
                    sup.stdin.write('stop\n')
                    sup.stdin.flush()
                report = self._read_report(sup, timeout=90)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    sup.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    sup.kill()
                    sup.wait(timeout=5)
                for f in (sup.stdin, sup.stdout):
                    try:
                        f.close()
                    except OSError:
                        pass
        if self._old_sigterm is not None:
            signal.signal(signal.SIGTERM, self._old_sigterm)
            self._old_sigterm = None
        if report is None or os.path.exists(self.work):
            # The supervisor died, never started or did not finish: clean up from here.
            left = supervise.teardown(self.work, None, self.copy_logs_to, set(self.x_before), self.log, self.logs)
            report = {'leftovers': left, 'reason': 'harness-fallback'}
        self.report = report
        self.leftovers = self.check_leftovers()
        if any(self.leftovers.values()):
            sys.stderr.write('[harness] LEFTOVERS after teardown of %s: %s\n' % (self.work, self.leftovers))

    @staticmethod
    def _read_report(sup, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([sup.stdout], [], [], 0.5)
            if r:
                line = sup.stdout.readline()
                if not line:
                    return None
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if 'leftovers' in msg:
                    return msg
            elif sup.poll() is not None:
                return None
        return None

    def check_leftovers(self):
        """Independent post-teardown check: processes, mounts, work dir, X files."""
        skip = supervise.ancestors()
        procs = {}
        for p in supervise.ppids():
            if p in skip:
                continue
            cmd = supervise.cmdline(p)
            if supervise.belongs(p, self.work) or self.work in cmd or self.display in cmd:
                if not supervise.is_zombie(p):
                    procs[p] = cmd[:160]
        new_x = sorted(set(glob.glob('/tmp/.X*-lock')) - set(self.x_before))
        ours = []
        for f in new_x:
            try:
                pid = int(open(f).read().strip())
            except (OSError, ValueError):
                continue
            if not os.path.exists('/proc/%d' % pid) or supervise.belongs(pid, self.work):
                ours.append(f)
        # Reported separately: the user's own Chrome may create such entries at the same time.
        self.tmp_new = sorted(tmp_litter() - self.tmp_before)
        return {'processes': procs, 'mounts': supervise.mounts_under(self.work),
                'work_dir': os.path.exists(self.work), 'x_files': ours}

    def tail(self, logname, n=40):
        try:
            return ''.join(open(os.path.join(self.logs, logname), errors='replace').readlines()[-n:])
        except OSError:
            return ''

    # ---- raw control -------------------------------------------------------------------
    def ctl(self, req, timeout=120):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        try:
            c.connect(os.path.join(self.work, 'ctl.sock'))
            c.sendall((json.dumps(req) + '\n').encode())
            data = b''
            while not data.endswith(b'\n'):
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError as exc:
            raise HarnessError('ctl %s: %r (is the session still running?)' % (req.get('op'), exc))
        finally:
            c.close()
        if not data:
            raise HarnessError('ctl %s: no reply (ctl.py died?)' % req.get('op'))
        resp = json.loads(data)
        if not resp.get('ok'):
            raise HarnessError('ctl %s failed: %s' % (req.get('op'), resp.get('error')))
        return resp['r']

    def sh(self, cmd, timeout=30, env=None):
        """Run a shell command INSIDE the session; returns {'rc', 'out', 'err'}."""
        return self.ctl({'op': 'sh', 'cmd': cmd, 'timeout': timeout, 'env': env or {}}, timeout=timeout + 10)

    def call(self, dest, path, iface, method, sig=None, args=None, timeout=30000):
        """Generic D-Bus method call on the session bus; returns the unpacked reply tuple."""
        return self.ctl({'op': 'call', 'dest': dest, 'path': path, 'iface': iface, 'method': method,
                         'sig': sig, 'args': list(args or []), 'timeout': timeout}, timeout=timeout / 1000 + 10)['res']

    engine_call = call

    def engine_status(self):
        """Diagnostics of the GramaDrama engine (pl.gramadrama.Engine Status, JSON)."""
        return json.loads(self.call('pl.gramadrama.Engine', '/pl/gramadrama/Engine', 'pl.gramadrama.Engine', 'Status')[0])

    def eval(self, js, raw=False):
        """org.gnome.Shell.Eval (the shell JSON-encodes the value). raw: (ok, str); else the decoded
        value (raises on failure)."""
        r = self.ctl({'op': 'eval', 'js': js})
        if raw:
            return r['ok'], r['res']
        if not r['ok']:
            raise HarnessError('Eval failed: %s' % r['res'])
        try:
            return json.loads(r['res']) if r['res'] else None
        except ValueError:
            return r['res']

    def shot(self, path, cursor=True):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return self.ctl({'op': 'shot', 'path': path, 'cursor': cursor})['res']

    wait_until = staticmethod(wait_until)

    # ---- IBus ---------------------------------------------------------------------------------
    def ibus_engine_status(self):
        """'ibus engine' run inside the session: {'engine': name|None, 'rc', 'out'}."""
        r = self.sh('LC_ALL=C timeout 5 ibus engine', timeout=10)
        out = r['out'].strip()
        return {'engine': out if r['rc'] == 0 and out and ' ' not in out else None, 'rc': r['rc'],
                'out': (out + r['err']).strip()}

    def ibus_engines(self):
        r = self.sh('LC_ALL=C timeout 10 ibus list-engine --name-only', timeout=15)
        return [l.strip() for l in r['out'].splitlines() if l.strip()]

    def input_source(self):
        return self.eval('(s => s && {type: s.type, id: s.id, xkbId: s.xkbId, name: s.displayName})'
                         '(gdKbd.getInputSourceManager().currentSource)')

    def set_sources(self, sources):
        """Change the input sources inside the session (gsettings on the isolated dconf)."""
        val = '[%s]' % ', '.join("('%s', '%s')" % tuple(s) for s in sources)
        r = self.sh('gsettings set org.gnome.desktop.input-sources sources "%s"' % val)
        if r['rc']:
            raise HarnessError(r['err'])

    # ---- keyboard / pointer ------------------------------------------------------------------
    def keys(self, seq, gap_ms=8):
        """Inject a raw sequence (see keys.py); returns the press/release timestamps."""
        return self.ctl({'op': 'keys', 'seq': seq, 'gap_ms': gap_ms})['stamps']

    def tap(self, key, mods=(), gap_ms=8):
        return self.keys(K.tap(key, mods), gap_ms)

    def combo(self, *names, gap_ms=8):
        return self.keys(K.combo(*names), gap_ms)

    def type_text(self, text, mode='keycodes', gap_ms=8, delay_ms=30):
        """keycodes (default): real evdev keys on the pl layout: Shift, AltGr letters, dead keys
        for é/č (see keyseq.py). keysyms: NotifyKeyboardKeysym per character; mutter only maps
        keysyms of layout levels 1-3 (so not Ą/Ś/Ł... which need Shift+AltGr, nor é or emoji)."""
        if mode == 'keysyms':
            return self.ctl({'op': 'type', 'text': text, 'delay_ms': delay_ms})['stamps']
        return self.keys(K.text_seq(text), gap_ms)

    def pointer_to(self, x, y):
        self.ctl({'op': 'pointer_to', 'x': x, 'y': y})

    def pointer_move(self, dx, dy):
        self.ctl({'op': 'pointer', 'dx': dx, 'dy': dy})

    def button(self, pressed, button='left'):
        self.ctl({'op': 'button', 'button': BUTTONS.get(button, button), 'pressed': pressed})

    def click(self, x=None, y=None, n=1, button='left'):
        if x is not None:
            self.pointer_to(x, y)
        self.ctl({'op': 'click', 'n': n, 'button': BUTTONS.get(button, button)})

    def drag(self, start, end, button='left', steps=5):
        self.ctl({'op': 'drag', 'from': list(start), 'to': list(end), 'button': BUTTONS.get(button, button),
                  'steps': steps})

    # ---- windows ----------------------------------------------------------------------------------
    def windows(self):
        return self.eval(WINDOWS_JS)

    def window(self, title):
        return next((w for w in self.windows() if w['title'] and title in w['title']), None)

    def focused_window(self):
        return next((w for w in self.windows() if w['focus']), None)

    def place_window(self, title, x, y, width, height, timeout=5):
        """Move and resize a window (frame rect, screen coordinates), e.g. so that pointer tests never
        hit an overlapping window. Returns the frame mutter settled on."""
        js = ('(w => w ? (w.move_resize_frame(true, %d, %d, %d, %d), true) : false)(global.get_window_actors()'
              '.map(a => a.meta_window).find(w => (w.get_title() || "").includes(%s)))' % (x, y, width, height, json.dumps(title)))
        if not self.eval(js):
            raise HarnessError('no window titled %r' % title)
        return wait_until(lambda: (lambda w: w and w['frame'][2] == width and w['frame'][0] == x and w['frame'])(
            self.window(title)), timeout, what='%r placed at %d,%d %dx%d' % (title, x, y, width, height))

    def activate_window(self, title, timeout=5):
        js = ('(w => w ? (w.activate(global.get_current_time()), true) : false)(global.get_window_actors()'
              '.map(a => a.meta_window).find(w => (w.get_title() || "").includes(%s)))' % json.dumps(title))
        if not self.eval(js):
            raise HarnessError('no window titled %r' % title)
        wait_until(lambda: (self.focused_window() or {}).get('title', '').find(title) >= 0, timeout,
                   what='focus on %r' % title)

    # ---- processes -----------------------------------------------------------------------------------
    def _logname(self, base):
        self._counter += 1
        return os.path.join(self.logs, '%s-%d.log' % (base, self._counter))

    def spawn(self, argv, env=None, unset=(), log=None, x11=True, name=None, cls=Proc):
        """Start argv inside the session (session env + DISPLAY/XAUTHORITY of our Xwayland when
        x11, like a real GNOME session + env overrides). Output goes to log."""
        name = name or os.path.basename(argv[0])
        log = log or self._logname(name)
        pid = self.ctl({'op': 'spawn', 'argv': list(argv), 'env': env or {}, 'unset': list(unset),
                        'log': log, 'x11': x11})['pid']
        p = cls(self, pid, name, log)
        self.procs.append(p)
        self.log('spawned %s pid=%d log=%s' % (name, pid, log))
        return p

    def spawn_app(self, argv, env=None, name=None, unset=()):
        return self.spawn(argv, env=env, name=name, unset=unset)

    def spawn_gtk(self, version=4, env=None, name=None, wait=True, timeout=20):
        name = name or 'gtk%d' % version
        log = os.path.join(self.logs, 'testwin-%s.jsonl' % name)
        p = self.spawn(['/usr/bin/python3', os.path.join(HERE, 'testwin.py'), str(version), log, name], env=env,
                       log=os.path.join(self.logs, 'testwin-%s.out' % name), name=name, cls=TestWin)
        p.log, p.bus_name, p.title = log, 'org.gramadrama.TestWin.' + name, 'GD test GTK%d %s' % (version, name)
        if wait:
            p.wait_event(lambda e: e['ev'] == 'ready', timeout=timeout)
            wait_until(lambda: self._try(lambda: (lambda i: i['active'] and i['focus'] == 'tv')(p.info())),
                       timeout, what='%s active with the TextView focused' % name)
        return p

    def spawn_text_editor(self, path, text=None, env=None, wait=True, timeout=20):
        """gnome-text-editor on a file INSIDE the session (a relative path is taken under
        <work>/home); text, if given, is written to it first. Ground truth: TextEditor.save()."""
        path = os.path.realpath(os.path.join(self.env['HOME'], path))
        if not path.startswith(self.work + '/'):
            raise HarnessError('spawn_text_editor: %s is outside the session work dir' % path)
        if text is not None:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        p = self.spawn(['gnome-text-editor', '--standalone', path], env=env, name='gnome-text-editor', cls=TextEditor)
        p.path = path
        if wait:
            wait_until(lambda: self._try(lambda: any(w['focus'] and os.path.basename(path) in (w['title'] or '')
                                                     for w in self.windows())), timeout, what='text editor window')
            time.sleep(1.0)  # the source view takes keyboard focus after the title is set
        return p

    def fixture_url(self, focus=None):
        return 'file://%s%s' % (FIXTURE, '#focus=' + focus if focus else '')

    def spawn_chrome(self, url=None, x11=False, extra_args=(), env=None, name=None, app=True, wait=True, timeout=30,
                     extension=None):
        """extension: the directory of an unpacked extension to load (chrome_ext.py), together with the native messaging host
        of GramaDrama (browser/extension, linux/gramadrama-browser-host), registered in this Chrome's own profile directory
        as a user would register it in ~/.config/google-chrome."""
        name = name or 'chrome%d' % (self._counter + 1)
        url = url or self.fixture_url()
        profile = '%s/chrome-%s' % (self.work, name)
        flags = list(CHROME_FLAGS)
        if extension:
            flags = [f for f in flags if f != '--disable-extensions']  # it would keep every extension off
            hosts = os.path.join(profile, 'NativeMessagingHosts')
            os.makedirs(hosts, exist_ok=True)
            with open(os.path.join(hosts, 'pl.gramadrama.browser.json'), 'w') as f:
                json.dump({'name': 'pl.gramadrama.browser', 'description': 'GramaDrama', 'type': 'stdio',
                           'path': os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                                'gramadrama-browser-host'),
                           'allowed_origins': ['chrome-extension://hlnpnkiancoohmafifihcjndfmhilkfe/']}, f)
        argv = ['/usr/bin/google-chrome', '--user-data-dir=' + profile,
                '--ozone-platform=' + ('x11' if x11 else 'wayland')] + flags + list(extra_args)
        if extension:  # Chrome ignores --load-extension: tests/session/chrome_ext.py loads it through the DevTools pipe
            argv = ['/usr/bin/python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome_ext.py'), extension] + argv
            env = dict(env or {}, GD_CDP_DIR=os.path.join(profile, 'cdp'))
            os.makedirs(env['GD_CDP_DIR'], exist_ok=True)
        argv.append('--app=' + url if app else url)
        p = self.spawn(argv, env=env, name=name, cls=Chrome)
        p.cdp_dir = env['GD_CDP_DIR'] if extension else None
        p.x11 = x11
        if wait and url.startswith('file://' + FIXTURE):
            p.wait_event(lambda e: e['ev'] == 'loaded', timeout=timeout)
            wait_until(lambda: self._try(lambda: (self.focused_window() or {}).get('title') == 'GD fixture'),
                       timeout, what='chrome window focused')
        return p

    def pids(self, pattern=None):
        """PIDs of this session's processes, optionally filtered by a regex on the command line."""
        rx = re.compile(pattern) if pattern else None
        out = supervise.session_pids(self.work, self.sup.pid if self.sup else None)
        return sorted(p for p in out if not rx or rx.search(supervise.cmdline(p)))

    def engine_pids(self):
        """PIDs of the engine process started from engine_exec (e.g. for crash tests)."""
        script = next((a for a in self.engine_exec.split() if a.startswith('/') and not a.endswith('python3')), None)
        return self.pids(re.escape(script)) if script else []

    def wait_ibus_engine(self, name, timeout=10):
        """Wait until 'ibus engine' inside the session reports name (prefix match)."""
        return wait_until(lambda: (lambda e: e if e and e.startswith(name) else None)(
            (self._try(self.ibus_engine_status) or {}).get('engine')), timeout, what='ibus engine %s' % name)

    def kill_pids(self, pids, sig=signal.SIGKILL):
        mine = set(self.pids())
        for p in pids:
            if p not in mine:
                raise HarnessError('pid %d is not part of the session' % p)
            os.kill(p, sig)

    def x11_env(self):
        return self.ctl({'op': 'x11'})['env']

    @staticmethod
    def _try(fn):
        try:
            return fn()
        except (HarnessError, KeyError, TypeError, ValueError, OSError):
            return None


def threading_main():
    import threading
    return threading.current_thread() is threading.main_thread()


def _sigterm(signum, frame):
    raise SystemExit(128 + signum)
