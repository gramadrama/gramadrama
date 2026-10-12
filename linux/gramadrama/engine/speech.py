"""The engine's long-lived children (no gi, no core): the speech service (gramadrama/speech.py,
docs/dictation.md) and the live-check service (gramadrama/liveservice.py, docs/live-check.md).

Each starts when first needed and stays: the speech service keeps the Whisper model in the GPU's memory
(it quits by itself after half an hour without use), the live service keeps the dictionaries.
Both end with the engine (their stdin closes). The speech service runs in the speech environment's Python
(paths.speech_python()) when it uses the built-in Whisper, and in this Python when a transcription server is
selected (it needs no numpy or GPU then); in test mode with GRAMADRAMA_DEBUG_SPEECH it runs
in this Python, without a microphone or a model. Their events reach deliver() on the main loop through post (GLib.idle_add);
{'ev': 'died'} when the process ends, or when it cannot be started."""
import json
import os
import subprocess
import sys
import threading
import time

from .. import paths

LINUX_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODE = 'import sys; sys.path.insert(0, %r); from gramadrama.%s import main; sys.exit(main())'
SETTINGS_SECONDS = 2.0  # settings.json is read at most this often on the key path's main loop


def debug_text():
    """GRAMADRAMA_DEBUG_SPEECH counts only in test mode (a stray variable never replaces dictation)."""
    return os.environ.get('GRAMADRAMA_DEBUG_SPEECH') if os.environ.get('GRAMADRAMA_ENGINE_TEST') == '1' else None


def ready():
    """The service can run. Server configuration errors are reported before it opens the microphone;
    they must not fall back to a local model or appear as missing local speech tools."""
    if debug_text() is not None or uses_server():
        return True
    return paths.speech_python().exists() and (paths.whisper_model() / 'model.bin').exists()


_settings = {'read': 0.0, 'data': {}}


def _data():
    """settings.json read with json alone (the engine never imports core); a missing or broken file is {}.
    Cached for SETTINGS_SECONDS."""
    now = time.monotonic()
    if now - _settings['read'] > SETTINGS_SECONDS:
        try:
            data = json.loads(paths.settings_path().read_bytes())
        except (OSError, ValueError):
            data = {}
        _settings.update(read=now, data=data if isinstance(data, dict) else {})
    return _settings['data']


def setting(name, default=True):
    """A switch of settings.json; a missing or unusable value means the default."""
    value = _data().get(name, default)
    return value if isinstance(value, bool) else default


def remote_url():
    """The saved server address, also retained while the built-in Whisper is selected."""
    value = _data().get('whisper_url')
    return value.strip() if isinstance(value, str) else ''


def uses_server():
    """Same legacy resolution as Settings.whisper_uses_server, without importing core on the key path.
    A malformed backend also uses the system Python so the service can report its configuration error."""
    backend = _data().get('whisper_backend', 'auto')
    return backend != 'local' and (backend != 'auto' or bool(remote_url()))


def enabled():
    return setting('dictation_enabled')


class ChildService:
    def __init__(self, module, python, post, deliver):
        self.module, self.python = module, python
        self.post, self.deliver = post, deliver
        self.process = None
        self.lock = threading.Lock()

    def send(self, command, start=True):
        """One command; start=False drops it when the service is not running (its end was reported)."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                if not start:
                    return
                try:
                    self._spawn()
                except OSError:
                    self.process = None
                    self.post(lambda: self.deliver({'ev': 'died'}) or False)
                    return
            try:
                self.process.stdin.write((json.dumps(command, ensure_ascii=False) + '\n').encode('utf-8'))
                self.process.stdin.flush()
            except OSError:
                pass  # it died meanwhile: its reader reports that

    def _spawn(self):
        self.process = subprocess.Popen([self.python(), '-I', '-c', CODE % (LINUX_DIR, self.module)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        cwd='/', env=dict(os.environ))
        threading.Thread(target=self._read, args=(self.process,), name='gramadrama-' + self.module, daemon=True).start()

    def _read(self, process):
        for line in process.stdout:  # blocking reads release the GIL
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self.post(lambda event=event: self.deliver(event) or False)
        process.wait()
        # Only for the current process: one that quit by itself (idle) must not end the dictation
        # that a new one, started meanwhile, is recording.
        self.post(lambda: process is self.process and self.deliver({'ev': 'died'}) or False)

    def close(self):
        with self.lock:
            process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()  # the service quits at the end of its stdin
                process.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


class SpeechClient(ChildService):
    def __init__(self, post, deliver):
        super().__init__('speech', self._python, post, deliver)
        self.remote = False  # the running service was started for a transcription server

    def _python(self):
        return sys.executable if debug_text() is not None or self.remote else str(paths.speech_python())

    def send(self, cmd, sid, alternate=False):
        if cmd == 'start':
            # Built-in Whisper or a server: the two run in different Pythons, so a change of the setting is a new service
            remote = uses_server()
            if remote != self.remote:
                self.close()
                self.remote = remote
        command = {'cmd': cmd, 'id': sid}
        if alternate:
            command['alternate'] = True  # with 'start': Shift was down before the recording began
        super().send(command, start=cmd == 'start')


class LiveClient(ChildService):
    """The live-check service, in this Python: it needs only the system's Enchant."""

    def __init__(self, post, deliver):
        super().__init__('liveservice', lambda: sys.executable, post, deliver)
