#!/usr/bin/env python3
"""Control server that lives INSIDE the isolated dbus-run-session (started by session.sh).

Holds the long-lived Mutter RemoteDesktop session (injection needs one D-Bus connection)
and serves one JSON request per connection on a Unix socket (see harness.Session.ctl):
  {"op":"ping"}                                      liveness + in-session facts
  {"op":"sh","cmd":"...","timeout":20}               shell command in the session env
  {"op":"spawn","argv":[..],"env":{},"unset":[],"log":path,"x11":true} / {"op":"poll","pid":N}
  {"op":"warmup"}                                    RemoteDesktop session + warm-up Shift tap
  {"op":"keys","seq":[["code",30,1],["sym",65293,0],["sleep",50]],"gap_ms":8}
  {"op":"type","text":"zażółć","delay_ms":30}        keysym typing (Gdk.unicode_to_keyval)
  {"op":"pointer","dx":1.0,"dy":2.0} / {"op":"button","button":272,"pressed":true}
  {"op":"pointer_to","x":10,"y":20} / {"op":"click","n":2} / {"op":"drag","from":[x,y],"to":[x,y]}
  {"op":"eval","js":"..."} / {"op":"shot","path":"/abs.png"}
  {"op":"call","dest":..,"path":..,"iface":..,"method":..,"sig":"(s)","args":[..]}
  {"op":"x11"}                                       DISPLAY/XAUTHORITY of this session's Xwayland
  {"op":"quit"}
"""
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time

import gi
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib  # noqa: E402

REAL = os.environ.get('GD_REAL_DBUS', '')
WORK = os.environ.get('GD_WORK', '')
ADDR = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
if not WORK.startswith('/tmp/gd-') or not ADDR or ADDR == REAL or WORK + '/' not in ADDR:
    sys.exit('ctl: refusing to run outside the isolated session bus')
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'HOME'):
    if not os.environ.get(var, '').startswith(WORK + '/'):
        sys.exit('ctl: %s is not isolated' % var)

SOCK = sys.argv[1]
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
children = []
children_lock = threading.Lock()
RD = 'org.gnome.Mutter.RemoteDesktop'
rd_session = None
x11_env = None


def call(dest, path, iface, method, sig=None, args=None, timeout=30000):
    params = GLib.Variant(sig, tuple(args)) if sig else None
    res = bus.call_sync(dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE, timeout, None)
    return res.unpack() if res is not None else None


def rd():
    global rd_session
    if rd_session is None:
        path = call(RD, '/org/gnome/Mutter/RemoteDesktop', RD, 'CreateSession')[0]
        call(RD, path, RD + '.Session', 'Start')
        rd_session = path
        # The first event after Start is dropped: warm up with a Shift tap.
        time.sleep(0.1)
        for pressed in (True, False):
            call(RD, path, RD + '.Session', 'NotifyKeyboardKeycode', '(ub)', [42, pressed])
        time.sleep(0.1)
    return rd_session


def rdcall(method, sig, args):
    return call(RD, rd(), RD + '.Session', method, sig, args)


def key(kind, val, pressed):
    rdcall('NotifyKeyboardKeycode' if kind == 'code' else 'NotifyKeyboardKeysym', '(ub)', [val, bool(pressed)])


def pointer(dx, dy):
    rdcall('NotifyPointerMotionRelative', '(dd)', [float(dx), float(dy)])


def button(b, pressed):
    rdcall('NotifyPointerButton', '(ib)', [int(b), bool(pressed)])


def pointer_to(x, y):
    # Absolute motion needs a screencast stream; slam into the top-left corner instead.
    pointer(-10000, -10000)
    time.sleep(0.05)
    pointer(x, y)
    time.sleep(0.05)


def shell_eval(js):
    ok, res = call('org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell', 'Eval', '(s)', [js])
    return ok, res


def find_x11():
    """DISPLAY/XAUTHORITY of this session's Xwayland, only if its lock file names our gnome-shell."""
    global x11_env
    if x11_env is None:
        ok, res = shell_eval('[imports.gi.GLib.getenv("DISPLAY"), imports.gi.GLib.getenv("XAUTHORITY")]')
        display, xauth = json.loads(res) if ok else (None, None)
        if not display:
            return None
        shell_pid = open(os.path.join(WORK, 'shell.pid')).read().strip()
        num = display.lstrip(':').split('.')[0]
        try:
            owner = open('/tmp/.X%s-lock' % num).read().strip()
        except OSError:
            owner = None
        if owner != shell_pid:
            raise RuntimeError('X display %s is not owned by this session (lock pid %s)' % (display, owner))
        if not xauth:
            auths = glob.glob(os.path.join(os.environ['XDG_RUNTIME_DIR'], '.mutter-Xwaylandauth.*'))
            xauth = auths[0] if auths else None
        if xauth and not xauth.startswith(WORK + '/'):
            raise RuntimeError('XAUTHORITY outside the session: ' + xauth)
        x11_env = {'DISPLAY': display, 'XAUTHORITY': xauth or ''}
    return x11_env


INPUT_OPS = {'warmup', 'keys', 'type', 'pointer', 'pointer_to', 'button', 'click', 'drag'}
input_lock = threading.Lock()


def handle(req):
    if req['op'] in INPUT_OPS:
        with input_lock:  # never interleave two injected sequences
            return _handle(req)
    return _handle(req)


def _handle(req):
    op = req['op']
    if op == 'ping':
        return {'pid': os.getpid(), 'bus': ADDR, 'wayland': os.environ.get('WAYLAND_DISPLAY'),
                'system_bus': os.environ.get('DBUS_SYSTEM_BUS_ADDRESS')}
    if op == 'sh':
        env = dict(os.environ, **req.get('env', {}))
        p = subprocess.run(['bash', '-c', req['cmd']], capture_output=True, text=True, env=env,
                           timeout=req.get('timeout', 30))
        return {'rc': p.returncode, 'out': p.stdout[-20000:], 'err': p.stderr[-8000:]}
    if op == 'spawn':
        env = dict(os.environ)
        if req.get('x11', True):
            env.update(find_x11() or {})
        env.update(req.get('env', {}))
        for k in req.get('unset', []):
            env.pop(k, None)
        logf = open(req.get('log') or os.devnull, 'ab')
        p = subprocess.Popen(req['argv'], stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                             env=env, cwd=req.get('cwd') or env['HOME'], start_new_session=True)
        logf.close()
        with children_lock:
            children.append(p)
        return {'pid': p.pid}
    if op == 'poll':
        with children_lock:
            p = next((c for c in children if c.pid == req['pid']), None)
        return {'running': p is not None and p.poll() is None, 'rc': p and p.returncode}
    if op == 'warmup':
        rd()
        return {'session': rd_session}
    if op == 'keys':
        rd()
        stamps = []
        for item in req['seq']:
            if item[0] == 'sleep':
                time.sleep(item[1] / 1000)
                continue
            stamps.append(time.time())
            key(item[0], item[1], item[2])
            time.sleep(req.get('gap_ms', 8) / 1000)
        return {'stamps': stamps}
    if op == 'type':
        stamps = []
        for ch in req['text']:
            ks = {'\n': 0xff0d, '\t': 0xff09}.get(ch) or Gdk.unicode_to_keyval(ord(ch))
            stamps.append(time.time())
            key('sym', ks, 1)
            key('sym', ks, 0)
            time.sleep(req.get('delay_ms', 30) / 1000)
        return {'stamps': stamps}
    if op == 'pointer':
        pointer(req['dx'], req['dy'])
        return {}
    if op == 'pointer_to':
        pointer_to(req['x'], req['y'])
        return {}
    if op == 'button':
        button(req.get('button', 272), req['pressed'])
        return {}
    if op == 'click':
        for _ in range(req.get('n', 1)):
            button(req.get('button', 272), True)
            button(req.get('button', 272), False)
            time.sleep(0.03)
        return {}
    if op == 'drag':
        (x0, y0), (x1, y1), b, steps = req['from'], req['to'], req.get('button', 272), req.get('steps', 5)
        pointer_to(x0, y0)
        button(b, True)
        time.sleep(0.05)
        for _ in range(steps):
            pointer((x1 - x0) / steps, (y1 - y0) / steps)
            time.sleep(0.03)
        button(b, False)
        time.sleep(0.05)
        return {}
    if op == 'eval':
        ok, res = shell_eval(req['js'])
        return {'ok': ok, 'res': res}
    if op == 'shot':
        return {'res': call('org.gnome.Shell.Screenshot', '/org/gnome/Shell/Screenshot', 'org.gnome.Shell.Screenshot',
                            'Screenshot', '(bbs)', [bool(req.get('cursor', True)), False, req['path']])}
    if op == 'call':
        return {'res': call(req['dest'], req['path'], req['iface'], req['method'], req.get('sig'),
                            req.get('args'), req.get('timeout', 30000))}
    if op == 'x11':
        return {'env': find_x11()}
    if op == 'quit':
        GLib.idle_add(loop.quit)
        return {'bye': True}
    raise ValueError('bad op %r' % op)


def serve_one(c):
    with c:
        c.settimeout(600)
        data = b''
        while not data.endswith(b'\n'):
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            resp = {'ok': True, 'r': handle(json.loads(data))}
        except Exception as exc:
            resp = {'ok': False, 'error': repr(exc)}
        c.sendall((json.dumps(resp, ensure_ascii=False) + '\n').encode())


def serve():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv.bind(SOCK + '.tmp')
    srv.listen(8)
    os.rename(SOCK + '.tmp', SOCK)
    while True:
        c, _ = srv.accept()
        # A vanished or misbehaving client must never kill the server.
        threading.Thread(target=_guard, args=(c,), daemon=True).start()


def _guard(c):
    try:
        serve_one(c)
    except Exception as exc:
        sys.stderr.write('ctl: client error %r\n' % exc)


loop = GLib.MainLoop()
threading.Thread(target=serve, daemon=True).start()
GLib.timeout_add_seconds(int(os.environ.get('GD_MAX_SECONDS', '3600')), loop.quit)
loop.run()
for p in children:
    try:
        os.killpg(p.pid, 9)
    except OSError:
        pass
