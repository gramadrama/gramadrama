#!/usr/bin/env python3
"""Harness self-test.

1. stock: sources [('xkb', 'pl')] (IBus engine xkb:pl::pol, no GramaDrama engine). GTK4 test
   window: Polish text via AltGr keycodes (+ dead keys) and via keysyms; Chrome (Wayland)
   fixture: typing into the textarea, verified from the console log.
2. component: sources [('ibus', 'gramadrama')] with the real component (data/gramadrama.xml.in,
   exec gramadrama-engine --ibus): proves IBUS_COMPONENT_PATH registration (engine listed and
   active per 'ibus engine' inside the session), that typing passes through and that the
   engine's diagnostics answer.
After each session: no leftover processes, mounts, /tmp/gd-* dir or X lock files.

usage: smoke.py [--only stock|component] [--repeat N] [--logs DIR] [-v]
"""
import argparse
import glob
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import LINUX, Session  # noqa: E402

PL = 'Zażółć gęślą jaźń ĄŚĆ'
DEAD = ' é č'
KEYSYMS = 'źdźbło ABC? ąść 1!'  # keysym mode: layout levels 1-3 only (see README)
results = []


def check(name, ok, detail=''):
    print('%s %s%s' % ('PASS' if ok else 'FAIL', name, '' if ok or not detail else ': %s' % (detail,)), flush=True)
    results.append((name, bool(ok)))
    return ok


def attempt(name, fn):
    try:
        return fn()
    except Exception as exc:
        check(name, False, repr(exc))
        return None


def typing_gtk4(s, label):
    win = s.spawn_gtk(4)
    win.set_text('tv', '')
    win.focus('tv')
    mark = win.mark()
    s.type_text(PL + DEAD)
    try:
        win.wait_text('tv', PL + DEAD, timeout=10)
        ok = True
    except TimeoutError:
        ok = False
    check('%s gtk4 keycodes (AltGr, dead keys)' % label, ok, repr(win.text('tv')))
    keys = win.keys_since(mark)
    check('%s gtk4 ground truth has press+release' % label,
          ('press', 'a') in keys and ('release', 'a') in keys and ('press', 'ISO_Level3_Shift') in keys, keys[:12])
    return win


def stock(args):
    s = Session(sources=[('xkb', 'pl')], name='smoke', copy_logs_to=args.logs and os.path.join(args.logs, 'stock'),
                verbose=args.verbose)
    try:
        with s:
            stock_body(s)
    finally:
        verify_clean(s, 'stock')


def stock_body(s):
    st = s.ibus_engine_status()
    check('stock ibus engine is xkb:pl::pol', st['engine'] == 'xkb:pl::pol', st)
    check('stock input source is xkb pl', (s.input_source() or {}).get('id') == 'pl', s.input_source())
    win = attempt('stock gtk4', lambda: typing_gtk4(s, 'stock'))
    if win:
        win.set_text('entry', '')
        win.focus('entry')
        s.type_text(KEYSYMS, mode='keysyms')
        try:
            win.wait_text('entry', KEYSYMS, timeout=10)
            ok = True
        except TimeoutError:
            ok = False
        check('stock gtk4 keysyms', ok, repr(win.text('entry')))
    chrome = attempt('stock chrome start', lambda: s.spawn_chrome(s.fixture_url(focus='ta')))
    if chrome:
        mark = chrome.mark()
        s.type_text(' ' + PL)
        try:
            ev = chrome.wait_event(lambda e: e['ev'] == 'input' and e['state']['ta']['text'].endswith(' ' + PL),
                                   mark, timeout=10)
            ok = ev['state']['active'] == 'ta'
        except TimeoutError:
            ok = False
        check('chrome wayland textarea via console log', ok, (chrome.last_state() or {}).get('ta'))
        keys = chrome.keys_since(mark)
        check('chrome console log has keydown/keyup', ('keydown', 'ż') in keys and ('keyup', 'ż') in keys, keys[:12])
        st = chrome.state()
        check('chrome fixture dump (rich editor markup)', '<b>pogrubieniem</b>' in st['rich']['html']
              and '<a href=' in st['rich']['html'] and st['pw']['text'], st['rich'])
    s.shot(os.path.join(s.logs, 'stock.png'))


def component(args):
    s = Session(sources=[('ibus', 'gramadrama')], name='smokeeng', env={'GRAMADRAMA_ENGINE_TEST': '1'},
                copy_logs_to=args.logs and os.path.join(args.logs, 'component'), verbose=args.verbose)
    try:
        with s:
            component_body(s)
    finally:
        verify_clean(s, 'component')


def component_body(s):
    st = s.ibus_engine_status()
    check('component ibus engine is gramadrama', st['engine'] == 'gramadrama', st)
    engines = s.ibus_engines()
    check('component engine listed by ibus', 'gramadrama' in engines, engines[-5:])
    src = s.input_source() or {}
    check('component input source ibus/gramadrama with layout pl',
          src.get('id') == 'gramadrama' and src.get('xkbId') == 'pl', src)
    check('component engine process in session', bool(s.engine_pids()), s.pids('engine'))
    attempt('component gtk4', lambda: typing_gtk4(s, 'component'))
    status = attempt('component engine_call', s.engine_status)
    check('component engine_call reaches the engine', status and status.get('client') == 'gnome-shell'
          and status.get('pid') in s.engine_pids(), status)


def verify_clean(s, label):
    if s.work is None:
        return
    left = s.leftovers or {}
    check('%s teardown: no leftovers' % label, s.leftovers is not None and not any(left.values()), left)
    if s.tmp_new:
        print('WARN %s: new Chrome temp entries in /tmp (ours, or the live Chrome): %s' % (label, s.tmp_new[:5]))
    stale = [f for f in sorted(set(glob.glob('/tmp/.X*-lock')) - set(s.x_before)) if not alive_owner(f)]
    check('%s teardown: no stale X lock files' % label, not stale and not left.get('x_files'), stale)
    check('%s teardown: work dir removed' % label, not os.path.exists(s.work), s.work)
    procs = []
    for pid in os.listdir('/proc'):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                cmd = open('/proc/%s/cmdline' % pid, 'rb').read().decode('utf-8', 'replace')
            except OSError:
                continue
            if s.work in cmd or s.display in cmd:
                procs.append((pid, cmd[:100]))
    check('%s teardown: no process names the session' % label, not procs, procs)
    mounts = [l for l in open('/proc/self/mountinfo') if s.work in l]
    check('%s teardown: no mounts' % label, not mounts, mounts)


def alive_owner(lockfile):
    try:
        return os.path.exists('/proc/%d' % int(open(lockfile).read().strip()))
    except (OSError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', choices=['stock', 'component'])
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--logs')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    for i in range(args.repeat):
        t0 = time.time()
        for name, fn in (('stock', stock), ('component', component)):
            if args.only in (None, name):
                try:
                    fn(args)
                except Exception:
                    traceback.print_exc()
                    check('%s session' % name, False)
        print('run %d finished in %.1fs' % (i + 1, time.time() - t0), flush=True)
    failed = [n for n, ok in results if not ok]
    print('SMOKE %s (%d checks, %d failed)' % ('FAIL' if failed else 'PASS', len(results), len(failed)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
