#!/usr/bin/env python3
"""Owns one isolated session and guarantees its teardown.

Started by harness.Session in its own process session (so a process-group kill of the test
does not hit it). It becomes a child subreaper, runs dbus-run-session + session.sh, and tears
everything down when told 'stop' on stdin, when stdin closes (the test died), on SIGTERM/SIGINT
or after max_seconds. Teardown: TERM then KILL every process of the session, unmount FUSE
leftovers, remove the X lock files/sockets of our Xwayland, copy logs, remove the work dir.
Only processes proven to belong to the session are touched (see belongs()): descendants of
this supervisor, processes whose environment carries GD_WORK=<work>, and Chrome/Electron
processes running with --user-data-dir inside the work dir. Never this process or its ancestors.

usage: supervise.py WORK/config.json      |      supervise.py --cleanup /tmp/gd-XXXX
"""
import ctypes
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36


def ancestors(pid=None):
    out, pid = set(), pid or os.getpid()
    while pid > 1:
        out.add(pid)
        try:
            pid = int(open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def ppids():
    out = {}
    for d in glob.glob('/proc/[0-9]*'):
        try:
            out[int(d[6:])] = int(open(d + '/stat').read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            pass
    return out


def descendants(root, table=None):
    table = table or ppids()
    kids, out, todo = {}, set(), [root]
    for pid, ppid in table.items():
        kids.setdefault(ppid, []).append(pid)
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in out:
                out.add(k)
                todo.append(k)
    return out


def cmdline(pid):
    try:
        return open('/proc/%d/cmdline' % pid, 'rb').read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
    except OSError:
        return ''


def belongs(pid, work):
    """True if pid carries our GD_WORK marker, or its environment is unreadable/overwritten
    (Chrome/Electron rewrite that memory for their process titles) and its command line names
    the work dir, or it runs with --user-data-dir inside the work dir."""
    try:
        env = open('/proc/%d/environ' % pid, 'rb').read().split(b'\0')
    except PermissionError:
        env = []
    except OSError:
        return False
    if ('GD_WORK=%s' % work).encode() in env:
        return True
    cmd = cmdline(pid)
    if '--user-data-dir=%s/' % work in cmd:
        return True
    return (work + '/') in cmd and not any(re.match(rb'[A-Za-z_][A-Za-z0-9_]*=', e) for e in env)


def session_pids(work, root=None):
    """PIDs of the session: marker/cmdline matches plus descendants of root."""
    skip = ancestors()
    table = ppids()
    found = {p for p in table if p not in skip and belongs(p, work)}
    if root:
        found |= descendants(root, table) - skip
    return {p for p in found if os.path.exists('/proc/%d' % p) and not is_zombie(p)}


def is_zombie(pid):
    try:
        return open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()[0] == 'Z'
    except (OSError, IndexError):
        return True


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return


def mounts_under(work):
    out = []
    try:
        for line in open('/proc/self/mountinfo'):
            mp = line.split()[4].replace('\\040', ' ')
            if mp == work or mp.startswith(work + '/'):
                out.append(mp)
    except OSError:
        pass
    return out


def x_displays(logs, shell_pid, before):
    """Display numbers whose lock files our gnome-shell created."""
    nums = set()
    try:
        log = open(os.path.join(logs, 'shell.log'), errors='replace').read()
        for m in re.finditer(r'X11 display :(\d+).*?/tmp/\.X11-unix/X(\d+)', log):
            nums |= {int(m.group(1)), int(m.group(2))}
    except OSError:
        pass
    for f in glob.glob('/tmp/.X*-lock'):
        try:
            if shell_pid and open(f).read().strip() == str(shell_pid):
                nums.add(int(f[7:-5]))
        except (OSError, ValueError):
            pass
    return {n for n in nums if '/tmp/.X%d-lock' % n not in before}


def remove_x_files(nums, shell_pid, log):
    for n in sorted(nums):
        lockf, sock = '/tmp/.X%d-lock' % n, '/tmp/.X11-unix/X%d' % n
        try:
            owner = int(open(lockf).read().strip())
        except (OSError, ValueError):
            owner = None
        if owner is not None and owner != shell_pid and os.path.exists('/proc/%d' % owner):
            log('keep %s: owned by live pid %d' % (lockf, owner))
            continue
        for f in (lockf, sock):
            if os.path.lexists(f):
                try:
                    if os.lstat(f).st_uid == os.getuid():
                        os.unlink(f)
                        log('removed ' + f)
                except OSError as exc:
                    log('cannot remove %s: %r' % (f, exc))


def teardown(work, root=None, copy_logs_to=None, before=(), log=print, logs=None):
    if not re.fullmatch(r'/tmp/gd-[A-Za-z0-9_]+', work) or os.path.islink(work):
        raise ValueError('refusing to clean %r' % work)
    try:
        shell_pid = int(open(os.path.join(work, 'shell.pid')).read())
    except (OSError, ValueError):
        shell_pid = None
    logs = logs or os.path.join(work, 'logs')
    xnums = x_displays(logs, shell_pid, before)
    for rnd in range(8):
        reap()
        pids = session_pids(work, root)
        if not pids:
            break
        sig = signal.SIGTERM if rnd == 0 else signal.SIGKILL
        log('round %d: %s %s' % (rnd, sig.name, sorted(pids)))
        for p in pids:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        deadline = time.time() + (3 if rnd == 0 else 1)
        while time.time() < deadline:
            reap()
            if not session_pids(work, root):
                break
            time.sleep(0.1)
    reap()
    for mp in sorted(mounts_under(work), key=len, reverse=True):
        rc = subprocess.run(['fusermount3', '-u', mp], capture_output=True, timeout=10).returncode
        if rc:
            subprocess.run(['fusermount3', '-uz', mp], capture_output=True, timeout=10)
        log('unmounted %s' % mp)
    remove_x_files(xnums, shell_pid, log)
    if copy_logs_to and os.path.isdir(logs):
        try:
            shutil.copytree(logs, copy_logs_to, dirs_exist_ok=True)
        except OSError as exc:
            log('copy logs failed: %r' % exc)
    if not mounts_under(work):
        shutil.rmtree(work, ignore_errors=True)
    return {'processes': {p: cmdline(p)[:160] for p in session_pids(work, root)},
            'mounts': mounts_under(work), 'work_dir': os.path.exists(work),
            'x_files': [f for n in sorted(xnums) for f in ('/tmp/.X%d-lock' % n, '/tmp/.X11-unix/X%d' % n)
                        if os.path.lexists(f)]}


def main():
    if sys.argv[1] == '--cleanup':
        left = teardown(os.path.realpath(sys.argv[2]))
        print(json.dumps(left))
        return 0 if not any(left.values()) else 1
    cfg = json.load(open(sys.argv[1]))
    work = cfg['work']
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    stop = []
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda *_: stop.append('signal'))
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    logf = open(os.path.join(cfg['logs'], 'session.log'), 'ab')
    child = subprocess.Popen(['dbus-run-session', '--config-file=' + cfg['bus_conf'], '--',
                              'bash', cfg['session_sh']], stdin=subprocess.DEVNULL, stdout=logf,
                             stderr=subprocess.STDOUT, env=cfg['env'], cwd=work)
    logf.close()
    out = sys.stdout
    out.write(json.dumps({'started': child.pid}) + '\n')
    out.flush()
    deadline = time.time() + cfg['max_seconds']
    reason = None
    while reason is None:
        if stop:
            reason = stop[0]
            break
        if time.time() > deadline:
            reason = 'max_seconds'
            break
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
        except InterruptedError:
            continue
        if r:
            line = sys.stdin.readline()
            reason = 'stop' if line.strip() == 'stop' else ('eof' if not line else None)
        reap()
    msgs = []
    left = teardown(work, os.getpid(), cfg.get('copy_logs_to'), set(cfg.get('x_before', [])), msgs.append,
                    cfg['logs'])
    try:
        out.write(json.dumps({'reason': reason, 'log': msgs, 'leftovers': left}) + '\n')
        out.flush()
    except (BrokenPipeError, OSError):
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
