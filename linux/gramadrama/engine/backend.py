"""Corrections in a child process per session: Bielik (Ollama), with LanguageTool giving the reasons for
the changes, or deterministic debug transforms for tests (no gi).

The network wait, the transform, the diff (core.diff_parts) and the line wrapping
(fallback_ui.lines) would otherwise run inside the engine process, where pure-Python work holds
the GIL and delays key events, and a thread cannot be stopped mid-request. A child process per
session does all of it and streams JSON lines back; cancel() kills it. The engine process never
imports core."""
from ..i18n import tr, set_language
import json
import os
import subprocess
import sys
import threading
import time

DEFAULT_DELAY_MS = 300
PARTIAL_INTERVAL = 0.1  # seconds between streamed partials (the popup is redrawn per partial)
ERROR_PREFIX = 'ERROR:'  # a debug reply starting with this simulates a backend error
LINUX_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHILD = ('import sys; sys.path.insert(0, %r); from gramadrama.engine.backend import child_main; child_main()'
         % LINUX_DIR)
WARM_CHILD = ('import sys; sys.path.insert(0, %r); from gramadrama.engine.backend import warm_main; warm_main()'
              % LINUX_DIR)
FAILED = 'Nie udało się przetworzyć tekstu. Spróbuj ponownie.'
UNREACHABLE = 'Serwer modelu zszedł ze sceny — sprawdź połączenie.'  # branding/PANEL.md
DIRECTION_LABELS = {'en': 'PL → EN', 'pl': 'EN → PL'}  # default pair for older callers
SECOND_LANGUAGE_REQUIRED = 'Tłumaczenie wymaga drugiego języka. Dodaj go w GramaDrama → Pisanie → Języki tekstu.'
_ui_language = 'pl'


def _shorter(text):
    words = text.split()
    return ' '.join(words[:(len(words) + 1) // 2])


TRANSFORMS = {
    'fix': str.upper,
    'improve': str.swapcase,
    'formal': str.title,
    'casual': str.lower,
    'shorter': _shorter,
    'translate': lambda text: ' '.join(reversed(text.split())),
}


def test_hooks():
    """GRAMADRAMA_DEBUG_REPLY / GRAMADRAMA_DEBUG_DELAY_MS act only in test mode, so a stray variable
    can never replace a user's corrections."""
    return os.environ.get('GRAMADRAMA_ENGINE_TEST') == '1'


def burn_ms():
    """GRAMADRAMA_DEBUG_BURN_MS (test mode only): the debug child spins the CPU this long before its
    result, standing in for heavy work (a slow diff) to prove it never delays the engine's keys."""
    try:
        return max(0, int(os.environ.get('GRAMADRAMA_DEBUG_BURN_MS', 0)))
    except ValueError:
        return 0


def delay_ms():
    try:
        return max(0, int(os.environ.get('GRAMADRAMA_DEBUG_DELAY_MS', DEFAULT_DELAY_MS)))
    except ValueError:
        return DEFAULT_DELAY_MS


class ProcessBackend:
    """Runs one child per started session and relays what it streams.

    sink provides backend_partial(id, text, rows, info), backend_result(id, corrected, rows, info)
    and backend_error(id, message), where rows are fallback_ui.lines() of the text or of the diff
    and info is {'target': 'en'|'pl'|None, 'note': str, 'segments': [[kind, text], ...] (the diff; empty
    for translations and partials), 'changes': [{'from', 'to', 'reason'}, ...] (at most core.MAX_CHANGES), 'count': int}.
    A result may be followed by backend_changes(id, changes, count): LanguageTool's reasons, when it
    answered after Bielik (the result never waits for it, as on macOS). They run on the main loop,
    handed over through the post(callable) given to start() (GLib.idle_add: the callable returns False).
    real=True corrects with Bielik/LanguageTool from settings.json; otherwise, and whenever a debug
    reply is set (`reply`, or GRAMADRAMA_DEBUG_REPLY in test mode), the child applies TRANSFORMS or
    returns the reply; 'ERROR:<message>' simulates a failure."""

    def __init__(self, sink, delay=None, reply=None, real=False):
        self.sink = sink
        self.delay = delay  # ms; None: GRAMADRAMA_DEBUG_DELAY_MS in test mode, else DEFAULT_DELAY_MS
        self.reply = reply
        self.real = real
        self._jobs = {}  # session id -> [cancel event, child process or None]
        self._lock = threading.Lock()

    def start(self, session_id, text, mode, post, target=None):
        """target: 'en' or 'pl' for mode 'translate'; None detects it from the text.
        Returns the thread that runs the child and relays its output (the child is thread.child)."""
        hooks = test_hooks()
        reply = self.reply if self.reply is not None else os.environ.get('GRAMADRAMA_DEBUG_REPLY') if hooks else None
        delay = self.delay if self.delay is not None else delay_ms() if hooks else DEFAULT_DELAY_MS
        event = threading.Event()
        with self._lock:
            self._jobs[session_id] = [event, None]
        job = {'text': text, 'mode': mode, 'target': target, 'delay': delay, 'reply': reply,
               'real': self.real and reply is None, 'burn': burn_ms() if hooks else 0}
        env = dict(os.environ)  # as of now: the worker spawns the child later, from its own thread
        worker = threading.Thread(target=self._run, args=(session_id, job, post, event, env),
                                  name='gramadrama-backend', daemon=True)
        worker.child = None
        worker.start()
        return worker

    def cancel(self, session_id):
        # The event is set under the lock that _run registers its child under: either _run sees
        # the cancel before it starts talking to the child, or the child is here to be killed.
        with self._lock:
            job = self._jobs.pop(session_id, None)
            if job:
                job[0].set()
        if job and job[1] is not None:
            _kill(job[1])

    def _deliver(self, post, event, name, sid, *args):
        # Runs on the main loop; a cancel() issued after posting still suppresses the call.
        def call():
            if not event.is_set():
                if name in ('backend_result', 'backend_error'):
                    with self._lock:
                        if self._jobs.get(sid, [None])[0] is event:
                            del self._jobs[sid]
                getattr(self.sink, name)(sid, *args)
            return False
        post(call)

    def _run(self, sid, job, post, event, env):
        child = None
        try:
            child = subprocess.Popen([sys.executable, '-I', '-c', CHILD], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd='/', env=env)
            threading.current_thread().child = child
            with self._lock:
                entry = self._jobs.get(sid)
                if entry is not None and entry[0] is event:
                    entry[1] = child
                cancelled = event.is_set()
            if cancelled:
                return  # finally kills the child before it gets the job
            child.stdin.write(json.dumps(job).encode())
            child.stdin.close()
            done = False  # a result came: only LanguageTool's reasons may follow it
            for line in child.stdout:  # blocking reads release the GIL
                if event.is_set():
                    return
                msg = json.loads(line)
                kind = msg.pop('kind')
                info = {'target': msg.get('target'), 'languages': msg.get('languages'), 'note': msg.get('note', ''),
                        'segments': msg.get('segments') or [], 'changes': msg.get('changes') or [],
                        'count': msg.get('count', 0)}
                if done:
                    if kind == 'changes':
                        self._deliver(post, event, 'backend_changes', sid, msg['changes'], msg.get('count', 0))
                    return
                if kind == 'partial':
                    self._deliver(post, event, 'backend_partial', sid, msg['text'], tuple(msg['rows']), info)
                elif kind == 'result':
                    self._deliver(post, event, 'backend_result', sid, msg['corrected'], tuple(msg['rows']), info)
                    if not msg.get('pending'):
                        return
                    done = True
                else:
                    self._deliver(post, event, 'backend_error', sid, msg['message'], str(msg.get('why') or 'unknown')[:40])
                    return
            if not event.is_set() and not done:
                self._deliver(post, event, 'backend_error', sid, FAILED, 'no_result')
        except Exception:
            if not event.is_set():
                self._deliver(post, event, 'backend_error', sid, FAILED, 'crash')
        finally:
            if child is not None:
                _kill(child)
                for f in (child.stdin, child.stdout):
                    try:
                        f.close()
                    except OSError:
                        pass
                child.wait()


DebugBackend = ProcessBackend  # real=False by default


def warm_up():
    """Loads the model into Ollama's memory in a short-lived child (the engine never imports core),
    so the first Ctrl+Alt+G after a start does not wait for gigabytes to load (only Ollama has anything to load). Returns the thread that
    waits for the child; the engine does not."""
    def run():
        try:
            subprocess.run([sys.executable, '-I', '-c', WARM_CHILD], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd='/', timeout=WARM_TIMEOUT + 10)
        except (OSError, subprocess.SubprocessError):
            pass
    thread = threading.Thread(target=run, name='gramadrama-warm-up', daemon=True)
    thread.start()
    return thread


WARM_TIMEOUT = 300  # seconds: loading the model after a restart of the server takes a while


def warm_main():
    """The warm-up child: the same num_ctx as the corrections, or Ollama would load the model again."""
    from .. import core
    try:
        settings = core.Settings.load()
        settings.validate()
    except (ValueError, TypeError, OSError):
        return
    if test_hooks() and not all(_loopback(url) for url in core.addresses(settings)):
        return
    try:
        core.Backend(settings).warm_up(WARM_TIMEOUT)
    except core.BackendError:
        pass


def _kill(child):
    try:
        child.kill()
    except OSError:
        pass


def _emit(**msg):
    if _ui_language != 'pl':
        msg['ui_language'] = _ui_language
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


def child_main():
    """The child: one job as JSON on stdin; JSON lines on stdout (partials, then result or error)."""
    job = json.loads(sys.stdin.read())
    try:
        (_real if job['real'] else _debug)(job)
    except Exception as exc:
        _emit(kind='error', message=tr(FAILED), why=type(exc).__name__)


def _debug(job):
    from .. import core
    from .fallback_ui import lines
    text, mode, reply, step = job['text'], job['mode'], job['reply'], job['delay'] / 1000 / 4
    if reply is not None and reply.startswith(ERROR_PREFIX):
        result, error = None, reply[len(ERROR_PREFIX):].strip() or tr('Testowy błąd backendu.')
    elif reply is not None:
        result, error = reply, None
    elif mode in TRANSFORMS:
        result, error = TRANSFORMS[mode](text), None
    else:
        result, error = None, tr('Nieznany tryb.')
    languages = ('pl', 'en')  # deterministic debug transforms never read the user's configuration
    if mode == 'translate' and len(languages) < 2:
        _emit(kind='error', message=tr(SECOND_LANGUAGE_REQUIRED), why='second_language')
        return
    target = (job['target'] if job['target'] in languages else core.translation_target(text, languages)) if mode == 'translate' else None
    if target:
        _emit(kind='partial', text='', rows=[], target=target, languages=languages)
    for i in range(1, 4):
        time.sleep(step)
        if result is not None:
            partial = result[:len(result) * i // 4]
            _emit(kind='partial', text=partial, rows=lines((('same', partial),)), target=target, languages=languages)
    time.sleep(step)
    spin_until = time.monotonic() + job.get('burn', 0) / 1000
    while time.monotonic() < spin_until:  # test mode: CPU-heavy work in the child
        pass
    if error is not None:
        _emit(kind='error', message=error, why='test')
        return
    _result(core, lines, text, result, mode, target=target, languages=languages, note='test')


def result_payload(core, lines, text, result, mode, hints=(), **info):
    """Build the popup rows, diff and changes without writing to a process's output.
    A diff between two languages says nothing: a translation is shown as plain text."""
    if mode == 'translate':
        segments, found = [], []
        rows = lines((('same', result),))
    else:
        segments = [list(part) for part in core.diff_parts(text, result)]
        found = core.changes(segments, text, hints)
        rows = lines(tuple(map(tuple, segments)))
    return dict(kind='result', corrected=result, rows=rows, segments=segments, changes=found[:core.MAX_CHANGES],
                count=len(found), **info)


def _result(core, lines, text, result, mode, hints=(), **info):
    """Emit a backend result and return its diff (also used for the late rule hints)."""
    payload = result_payload(core, lines, text, result, mode, hints, **info)
    _emit(**payload)
    return payload['segments']


def _note(who, target, start=None, languages=None):
    """The panel's footer before the scope (branding/PANEL.md): model · direction · time."""
    source = next((code for code in languages or () if code != target), None)
    direction = '%s → %s' % (source.upper(), target.upper()) if source and target else DIRECTION_LABELS.get(target, '')
    parts = [who, direction] + ([_seconds(start)] if start is not None else [])
    return ' · '.join(part for part in parts if part)


def _loopback(url):
    import urllib.parse
    return urllib.parse.urlsplit(url).hostname in ('127.0.0.1', 'localhost', '::1')


def _seconds(start):
    """The time in the footer: a decimal comma in the Polish interface, a point in the English one."""
    text = '%.1f s' % (time.monotonic() - start)
    return text.replace('.', ',') if _ui_language == 'pl' else text


def _settings_why(core, settings):
    """The cause's code of a settings check that failed (engine/problems.py). No model chosen for Ollama on this computer
    is how a new install starts (docs/open-source-plan.md): whether Ollama is missing, installed but not answering, or
    answering ('no_ollama', 'ollama_down', 'no_model') says what to do first. Anything else: 'settings'."""
    try:
        settings.validate(llm=False)  # something else is wrong too: the check's own message says what
    except ValueError:
        return 'settings'
    if settings.model.strip() or core.located(settings, 'llm') != 'local':
        return 'settings'
    import urllib.request
    try:
        with urllib.request.urlopen(core.model_server(settings)[0].rstrip('/') + '/api/tags', timeout=1):
            return 'no_model'
    except (OSError, ValueError):
        pass
    from ..checks import ollama_installed
    return 'ollama_down' if ollama_installed() else 'no_ollama'


def _real(job):
    global _ui_language
    from .. import core
    from .fallback_ui import lines
    text, mode = job['text'], job['mode']
    try:
        settings = core.Settings.load()
    except (ValueError, TypeError, OSError):  # unreadable or not JSON
        _emit(kind='error', message=tr('Nie udało się odczytać ustawień (%s).') % core.Settings.path(), why='settings')
        return
    _ui_language = set_language(settings.primary_language)
    try:
        settings.validate()
    except ValueError as exc:  # core's messages are Polish; urllib's port error is not
        _emit(kind='error', message=tr('Ustawienia: %s') % exc, why=_settings_why(core, settings))
        return
    languages = settings.languages
    if mode == 'translate' and len(languages) < 2:
        _emit(kind='error', message=tr(SECOND_LANGUAGE_REQUIRED), why='second_language')
        return
    target = (job['target'] if job['target'] in languages else core.translation_target(text, languages)) if mode == 'translate' else None
    if test_hooks() and not all(_loopback(url) for url in core.addresses(settings)):
        _emit(kind='error', message=tr('Tryb testowy: dozwolony jest tylko serwer na 127.0.0.1.'), why='test_mode')
        return
    backend = core.Backend(settings)
    model = core.display_name(settings.model)  # as the Mac's footer names it (Settings.modelDisplayName)
    start = time.monotonic()
    last = [0.0]
    if target:  # the direction is known now: Ctrl+6 can reverse it before the first token
        _emit(kind='partial', text='', rows=[], target=target, languages=languages, note=_note(model, target, languages=languages))
    hints, checked = [], threading.Event()

    def check():  # LanguageTool in parallel with Bielik, as on macOS: reasons for the change list
        try:
            hints.extend(backend.check(text, timeout=5))
            checked.set()
        except Exception:
            pass
    rules = None if mode == 'translate' else threading.Thread(target=check, daemon=True)
    if rules:
        rules.start()

    def partial(raw):
        now = time.monotonic()
        if now - last[0] < PARTIAL_INTERVAL:
            return
        last[0] = now
        shown = raw.strip()
        if shown.startswith('<text>'):
            shown = shown[len('<text>'):].lstrip()
        _emit(kind='partial', text=shown, rows=lines((('same', shown),)), target=target, languages=languages, note=_note(model, target, languages=languages))

    try:
        corrected = backend.correct(text, mode, target or 'en', partial)
    except core.Unreachable as exc:  # no rules-only answer in any mode, as on macOS since 2026-09-29 (branding/PANEL.md)
        _emit(kind='error', message=tr(UNREACHABLE), why=core.error_kind(exc))
        return
    except core.BackendError as exc:
        _emit(kind='error', message=str(exc), why=core.error_kind(exc))
        return
    except OSError:  # the connection broke while streaming
        _emit(kind='error', message=tr('Połączenie z serwerem zostało przerwane. Spróbuj ponownie.'), why='interrupted')
        return
    note = _note(model, target, start, languages)
    if rules is None or checked.is_set():
        _result(core, lines, text, corrected, mode, hints if checked.is_set() else (), target=target, languages=languages, note=note)
        return
    # The result does not wait for LanguageTool (as on macOS); its reasons follow when it answers.
    segments = _result(core, lines, text, corrected, mode, target=target, languages=languages, note=note, pending=True)
    rules.join(6)  # check() gives up after 5 s
    if checked.is_set() and hints and segments:
        found = core.changes(segments, text, hints)
        _emit(kind='changes', changes=found[:core.MAX_CHANGES], count=len(found))
