#!/usr/bin/python3
"""Starts Chrome with an unpacked extension loaded: usage chrome_ext.py EXTENSION_DIR CHROME ARGS...

Google Chrome ignores --load-extension ("not allowed in Google Chrome"). What it allows for automation is the DevTools
protocol over a pipe with --enable-unsafe-extension-debugging, as Puppeteer installs an extension: Extensions.loadUnpacked.
This process holds the pipe (Chrome ends when it closes) and ends with Chrome; a signal to it goes to Chrome.

With GD_CDP_DIR set it also serves commands for the test: a file cmd-N.json in that directory ({"op": "eval_sw",
"expression": "…"}: run JavaScript in the extension's service worker, with the page's await and by-value result) is answered
by res-N.json ({"ok": …, "result": …})."""
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time


class Cdp:
    """The DevTools protocol over the two pipes: call() waits for the answer with its id and keeps the rest."""

    def __init__(self, w_in, r_out):
        self.w_in, self.r_out, self.buffer, self.next_id = w_in, r_out, b'', 0

    def call(self, method, params=None, session=None, timeout=10):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        os.write(self.w_in, json.dumps(message).encode() + b'\0')
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while b'\0' in self.buffer:
                data, self.buffer = self.buffer.split(b'\0', 1)
                try:
                    reply = json.loads(data)
                except ValueError:
                    continue
                if reply.get('id') == message['id']:
                    return reply
            if select.select([self.r_out], [], [], 0.1)[0]:
                chunk = os.read(self.r_out, 65536)
                if not chunk:
                    raise EOFError
                self.buffer += chunk
        raise TimeoutError(method)


def service_worker_session(cdp, extension_id):
    targets = cdp.call('Target.getTargets')['result']['targetInfos']
    target = next((t for t in targets if t['type'] == 'service_worker' and t['url'].startswith('chrome-extension://%s/' % extension_id)), None)
    if target is None:
        raise LookupError('no service worker of the extension')
    return cdp.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['result']['sessionId']


def serve(cdp, directory, extension_id, done):
    for name in sorted(os.listdir(directory)):
        if not name.startswith('cmd-') or name in done:
            continue
        done.add(name)
        number = name[4:-5]
        try:
            command = json.load(open(os.path.join(directory, name)))
            if command['op'] != 'eval_sw':
                raise ValueError('unknown op')
            session = service_worker_session(cdp, extension_id)
            reply = cdp.call('Runtime.evaluate', {'expression': command['expression'], 'awaitPromise': True, 'returnByValue': True},
                             session=session)
            outcome = {'ok': 'exceptionDetails' not in reply.get('result', {}), 'result': reply.get('result')}
        except Exception as exc:  # whatever went wrong, the test is told
            outcome = {'ok': False, 'result': '%s: %s' % (type(exc).__name__, exc)}
        temp = os.path.join(directory, 'res-%s.tmp' % number)
        with open(temp, 'w') as f:
            json.dump(outcome, f)
        os.replace(temp, os.path.join(directory, 'res-%s.json' % number))


def main():
    extension, argv = sys.argv[1], sys.argv[2:]

    def high(fd):
        # Out of the way of 3 and 4: dup2(3, 3) would change nothing and leave the descriptor closed on exec
        moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(fd)
        return moved
    r_in, w_in = (high(fd) for fd in os.pipe())    # we write commands, Chrome reads them from fd 3
    r_out, w_out = (high(fd) for fd in os.pipe())  # Chrome writes its answers to fd 4

    def child():
        os.dup2(r_in, 3, inheritable=True)
        os.dup2(w_out, 4, inheritable=True)
    proc = subprocess.Popen(argv + ['--remote-debugging-pipe', '--enable-unsafe-extension-debugging'], preexec_fn=child,
                            close_fds=False)
    os.close(r_in)
    os.close(w_out)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: proc.terminate())
    cdp, extension_id, done = Cdp(w_in, r_out), None, set()
    directory = os.environ.get('GD_CDP_DIR')
    last = 0.0
    while proc.poll() is None:
        try:
            if extension_id is None and time.monotonic() - last > 1.0:
                last = time.monotonic()
                reply = cdp.call('Extensions.loadUnpacked', {'path': extension}, timeout=3)
                if 'result' in reply and 'id' in reply['result']:
                    extension_id = reply['result']['id']
                    sys.stderr.write('chrome_ext: loaded %s\n' % extension_id)
                    sys.stderr.flush()
            elif extension_id is not None and directory:
                serve(cdp, directory, extension_id, done)
                time.sleep(0.1)
            else:
                time.sleep(0.1)
        except (TimeoutError, OSError, EOFError):
            time.sleep(0.2)
    return proc.wait()


sys.exit(main())
