"""The speech service for dictation (docs/dictation.md): it records the microphone while the dictation
key is held and turns the recording into text with Whisper, then cleans it up as macOS does (dictation.py).
Whisper is large-v3-turbo on this computer's GPU (faster-whisper) in its own Python (paths.speech_python():
the environment with the CUDA libraries), or the selected OpenAI-style or whisper.cpp transcription
server (RemoteWhisper: then the recording goes there, as a WAV, and the service
runs in the system Python). The engine starts it and talks to it over stdin/stdout, one JSON object per
line. Nothing it hears or writes goes to a log; the transcript leaves the machine only for the model's
optional cleanup, on the user's own server.

Commands:  {"cmd": "start", "id": n}     the right Ctrl came down: record (a tap or a shortcut cancels it)
           {"cmd": "confirm", "id": n}   held for ARM_MS: a dictation, the model's cleanup warms up
           {"cmd": "alternate", "id": n} {"cmd": "stop", "id": n}   {"cmd": "cancel", "id": n}
Events:    {"ev": "listening", "id", "ms"}              the first audio has come, ms after the start (a USB
                                                        microphone that PipeWire suspended wakes in up to 2 s)
           {"ev": "level", "id", "level", "s"}          20 times a second (0..1 for the bars, seconds so far)
           {"ev": "short", "id"}                        under MIN_SECONDS: dropped without a word
           {"ev": "silence", "id", "log"}               nothing loud enough was heard
           {"ev": "recognizing", "id", "limit"}         Whisper runs (limit: MAX_SECONDS reached, no key-up)
           {"ev": "cleaning", "id"}                     Bielik tidies the transcript
           {"ev": "result", "id", "text", "note", "log"}  the text to insert, '' when nothing was recognised
           {"ev": "error", "id", "message", "log"}
           {"ev": "model", "state": "ready"|"failed", "device", "ms"}

The service reads settings.json at every start (language, cleanup, own words). It quits when stdin
closes (the engine is gone) or after IDLE_SECONDS without a command, which frees the GPU memory.

Command line (install_engine.py tools runs the first):
    --self-test                       load the model on the GPU and time a transcription
    --transcribe FILE [...]           the whole path without the microphone, with its timings
       [--lang=pl|en|auto] [--words="A, B"] [--clean|--no-clean]

Test mode (GRAMADRAMA_ENGINE_TEST=1) with GRAMADRAMA_DEBUG_SPEECH set: no microphone and no Whisper.
The recording is a steady tone and the transcript is that variable's value; 'SILENCE' records silence
and 'ERROR:<message>' fails the recognition. GRAMADRAMA_DEBUG_MIC_MS delays the tone's start like a waking microphone."""
from .i18n import tr, set_language
import array
import dataclasses
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse

from . import dictation as D, paths

CHUNK = D.SAMPLE_RATE // 20  # samples per level reading: 50 ms
IDLE_SECONDS = 30 * 60
REMOTE_SECONDS = 60  # a transcription server may take this long to answer
TEST = os.environ.get('GRAMADRAMA_ENGINE_TEST') == '1'
DEBUG_TEXT = os.environ.get('GRAMADRAMA_DEBUG_SPEECH') if TEST else None
DEBUG_MIC_MS = int(os.environ.get('GRAMADRAMA_DEBUG_MIC_MS') or 0) if DEBUG_TEXT is not None else 0
NOTES = {  # shown after the text is inserted (macOS DictationController)
    'too_long': 'Długie dyktowanie — wstawiłem bez porządkowania (model nie zdąży w 10 s).',
    'timeout': 'Serwer modelu odpowiadał za długo — wstawiłem tekst bez porządkowania.',
    'error': 'Serwer modelu niedostępny — wstawiłem tekst bez porządkowania.',
}
_write = threading.Lock()
_ui_language = 'pl'


def emit(**event):
    if _ui_language != 'pl':
        event.setdefault('ui_language', _ui_language)
    with _write:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def load_cuda():
    """ctranslate2 finds cuBLAS and cuDNN from the pip wheels only when they are loaded first:
    LD_LIBRARY_PATH is read when the process starts, so setting it here would come too late."""
    import ctypes
    import glob
    import site
    for base in site.getsitepackages():
        for pattern in ('nvidia/cublas/lib/libcublasLt.so.*', 'nvidia/cublas/lib/libcublas.so.*',
                        'nvidia/cudnn/lib/libcudnn*.so.*'):
            for lib in sorted(glob.glob(os.path.join(base, pattern))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


class Whisper:
    """faster-whisper large-v3-turbo, loaded once in the background; the GPU when there is one."""

    def __init__(self):
        self.model = self.error = None
        self.device, self.ms = None, 0
        self.ready = threading.Event()

    def load(self):
        started = time.monotonic()
        try:
            load_cuda()
            from faster_whisper import WhisperModel
            try:
                self.model, self.device = WhisperModel(str(paths.whisper_model()), device='cuda', compute_type='float16'), 'cuda'
            except (RuntimeError, ValueError):  # no CUDA device or driver: slower, but it works
                self.model, self.device = WhisperModel(str(paths.whisper_model()), device='cpu', compute_type='int8'), 'cpu'
        except Exception as exc:  # a missing model or package: said at the next stop, and in the log
            self.error = (str(exc) or type(exc).__name__)[:200]
        self.ms = int((time.monotonic() - started) * 1000)
        self.ready.set()

    def transcribe(self, samples, language, prompt):
        """samples: floats in -1..1 at 16 kHz. As WhisperKit on macOS: temperature 0, no timestamps,
        the language forced unless 'auto', the prompt as the previous text."""
        self.ready.wait()
        if self.model is None:
            raise SpeechError(tr('Model mowy się nie wczytał: %s') % self.error, 'model_load')
        segments, _info = self.model.transcribe(samples, language=language, task='transcribe', temperature=0.0,
                                                beam_size=5, without_timestamps=True, initial_prompt=prompt or None,
                                                condition_on_previous_text=False, vad_filter=False)
        return ' '.join(segment.text.strip() for segment in segments)


class SpeechError(RuntimeError):
    """kind: the cause's short code for the engine log (failure_kind); the message may name a server or quote its answer."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def failure_kind(exc):
    """The short code of a failed recognition for the engine log, never its message."""
    kind = getattr(exc, 'kind', None)
    return kind[:40] if isinstance(kind, str) and kind else type(exc).__name__[:40]


def configuration_problem(settings):
    """Dictation-only configuration errors, checked before recording or loading a local model."""
    from . import core
    if settings.whisper_backend not in ('auto', 'local', 'server'):
        return tr('Wybierz rozpoznawanie mowy na tym komputerze albo na serwerze (aplikacja GramaDrama › Usługi).')
    if not settings.whisper_uses_server:
        return None
    if not isinstance(settings.whisper_url, str):
        return tr('Podaj adres serwera mowy (aplikacja GramaDrama › Usługi).')
    if settings.whisper_url.strip() and core.url_problem(settings.whisper_url):
        return core.url_problem(settings.whisper_url)
    if not core.speech_server(settings):  # a server without an address is the gateway's, when there is one
        return tr('Podaj adres serwera mowy (aplikacja GramaDrama › Usługi).')
    if settings.whisper_api not in ('openai', 'whispercpp'):
        return tr('Wybierz protokół serwera mowy: OpenAI albo whisper.cpp.')
    return None


def transcript(data):
    """The text of a transcription server's answer: JSON {"text": …} or plain text; None for anything else. whisper.cpp ends
    every segment with a new line, but a dictation is one run of text: all whitespace becomes single spaces, as the
    built-in Whisper joins its segments (SpeechServer.transcript on the Mac)."""
    try:
        value = json.loads(data.decode('utf-8'))
        text = value.get('text') if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        text = data.decode('utf-8', 'replace')
        if text.lstrip()[:1] in ('{', '['):
            return None
    return ' '.join(text.split()) if isinstance(text, str) else None


def describe_http(code, body, location='', login=False):
    """A speech server's HTTP error in the user's words (SpeechServer.describe on the Mac); of the answer only its
    "error"/"message", shortened."""
    try:
        data = json.loads(body.decode('utf-8', 'replace'))
        error = data.get('error')
        detail = (error.get('message') if isinstance(error, dict) else error) or data.get('message') or ''
    except (ValueError, AttributeError):
        detail = ''
    detail = ' — ' + detail.strip()[:160] if isinstance(detail, str) and detail.strip() else ''
    if 300 <= code < 400:
        return tr('Serwer mowy przekierowuje (HTTP {code:d}){where} — wpisz w ustawieniach adres docelowy.', code=code,
                  where=tr(' na {value}', value=location) if location else '')
    if code in (401, 403):
        if login:
            return tr('Serwer mowy odrzucił logowanie (HTTP {code:d}) — zaloguj się ponownie w zakładce Usługi.', code=code)
        return tr('Serwer mowy odrzucił dostęp (HTTP {code:d}) — sprawdź klucz API.', code=code) + detail
    if code in (404, 405):
        return tr('Serwer mowy nie ma tej ścieżki (HTTP {code:d}) — sprawdź adres i protokół.', code=code) + detail
    if code == 413:
        return tr('Nagranie jest za duże dla serwera albo jego proxy (HTTP 413) — zwiększ limit (np. client_max_body_size w nginx) albo dyktuj krócej.')
    return tr('Serwer mowy zwrócił błąd HTTP {code:d}.', code=code) + detail


def deadline(samples):
    """Seconds for the whole recognition (upload, the server's work, the answer): a longer recording gets more."""
    return min(120, 30 + len(samples) / D.SAMPLE_RATE)


class RemoteWhisper:
    """An OpenAI-style or native whisper.cpp transcription server: one the user runs, on this computer or another, or a
    cloud service; away from home the gateway's /whisper (core.route), with the login's token instead of the key. The
    recording goes there as a WAV, with the language and the prompt; nothing else does. Redirects are not followed: the
    recording and the key never go to an address other than the one typed. It needs neither numpy nor the speech
    environment."""
    device, ms = 'server', 0

    def __init__(self, settings):
        from . import core, oidc
        # This adapter may be constructed directly, independently of the selected local/server mode.
        settings = dataclasses.replace(settings, whisper_backend='server')
        problem = configuration_problem(settings)
        if problem:
            raise SpeechError(problem, 'config')
        self.core, self.oidc, self.settings = core, oidc, settings
        self.native = settings.whisper_api == 'whispercpp'
        self.url = core.service_base(settings, 'speech') + ('/inference' if self.native else '/audio/transcriptions')
        self.model = '' if self.native else (settings.whisper_model.strip() or 'whisper-1')
        self.key = settings.whisper_key
        self.ready = threading.Event()
        self.ready.set()

    def transcribe(self, samples, language, prompt):
        """samples: int16 at 16 kHz, as recorded."""
        import urllib.error
        import urllib.request
        fields = [('response_format', 'json'), ('temperature', '0')]
        if self.native:
            # Match the single-pass decoder used in the synthetic benchmark. Model selection belongs
            # to whisper-server; its defaults for language/prompt/translation must not override ours.
            fields.extend([('temperature_inc', '0'), ('best_of', '1'), ('beam_size', '1'),
                           ('translate', 'false'), ('language', language or 'auto'), ('prompt', prompt)])
        else:
            fields.append(('model', self.model))
            if language:
                fields.append(('language', language))
            if prompt:
                fields.append(('prompt', prompt))
        body, boundary = multipart(fields, ('file', 'audio.wav', 'audio/wav', wav_bytes(samples)))
        seconds = deadline(samples)
        rejected, rerouted = '', False
        for _ in range(3):
            url, kind = self.core.routed(self.settings, self.url, 'speech')
            if kind == 'offline':
                raise SpeechError(tr(self.core.OFFLINE_HINT), 'offline')
            key = '' if kind == 'remote' else self.key  # the key is the home server's, never the gateway's
            host = urllib.parse.urlsplit(url).netloc
            headers = {'Content-Type': 'multipart/form-data; boundary=' + boundary, 'User-Agent': self.oidc.USER_AGENT}
            try:
                authorization = self.oidc.authorization(self.settings, url, key, rejected)
            except self.oidc.AuthError as exc:
                raise SpeechError(str(exc), 'login') from None
            if authorization:
                headers['Authorization'] = authorization
            try:
                with self.core.opener_for(url, redirects=False).open(urllib.request.Request(url, data=body, headers=headers),
                                                                     timeout=seconds) as response:
                    data = response.read(2_000_000)
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(4096)
                except OSError:
                    detail = b''
                finally:
                    exc.close()
                if exc.code == 401 and authorization and not key and not rejected:
                    rejected = authorization.removeprefix('Bearer ')
                    continue
                raise SpeechError(describe_http(exc.code, detail, exc.headers.get('Location', '') if exc.headers else '',
                                                login=bool(authorization) and not key), 'http_%d' % exc.code) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if kind == 'home' and not rerouted:  # just left home: the gateway now
                    self.core.forget_route(self.url)
                    rerouted = True
                    continue
                if isinstance(getattr(exc, 'reason', exc), TimeoutError):
                    raise SpeechError(tr('Serwer mowy ({host}) nie odpowiedział w {value:d} s.', host=host, value=int(seconds)),
                                      'timeout') from None
                raise SpeechError(tr('Nie można połączyć się z serwerem mowy ({host}). Sprawdź adres i sieć.', host=host),
                                  self.core.network_kind(exc)) from None
        else:
            raise SpeechError(tr('Serwer mowy odrzucił logowanie (HTTP {code:d}) — zaloguj się ponownie w zakładce Usługi.', code=401),
                              'http_401')
        text = transcript(data)
        if text is None:
            raise SpeechError(tr('Serwer mowy odpowiedział nie tak, jak oczekiwano.'), 'bad_reply')
        return text


def wav_bytes(samples):
    """A 16 kHz mono 16-bit WAV of the recording: int16 samples (array 'h'), or floats in -1..1."""
    import io
    import wave
    if isinstance(samples, array.array) and samples.typecode == 'h':
        pcm = array.array('h', samples)
    else:
        pcm = array.array('h', (max(-32768, min(32767, int(x * 32767))) for x in samples))
    if sys.byteorder == 'big':
        pcm.byteswap()
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(D.SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return out.getvalue()


def multipart(fields, file):
    """(body, boundary) of a multipart/form-data request: text fields and one file (name, filename, type, bytes)."""
    import uuid
    boundary = 'gramadrama' + uuid.uuid4().hex
    parts = [('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode('utf-8')
             for name, value in fields]
    name, filename, kind, data = file
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                  % (boundary, name, filename, kind)).encode('utf-8') + data + b'\r\n')
    parts.append(('--%s--\r\n' % boundary).encode('utf-8'))
    return b''.join(parts), boundary


class DebugWhisper:
    """Test mode: the transcript is GRAMADRAMA_DEBUG_SPEECH; 'ERROR:<message>' fails."""
    device, ms = 'debug', 0

    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def load(self):
        pass

    def transcribe(self, samples, language, prompt):
        time.sleep(0.05)
        if DEBUG_TEXT.startswith('ERROR:'):
            raise SpeechError(DEBUG_TEXT[len('ERROR:'):].strip() or tr('Testowy błąd rozpoznawania.'), 'test')
        return DEBUG_TEXT


class Recorder:
    """The microphone through PipeWire (pw-record, raw 16 kHz mono s16 on its stdout), read in 50 ms
    chunks: each one gives a level for the HUD, and the loudest one decides about silence. The first one
    calls on_ready with the milliseconds it took: pw-record starts in ~0.15 s, a suspended USB webcam's
    microphone in ~2 s, and nothing said before it is recorded."""

    def __init__(self, on_level, on_limit, on_ready=None):
        self.on_level, self.on_limit, self.on_ready = on_level, on_limit, on_ready
        self.samples = array.array('h')
        self.peak = 0.0
        self.opened = self.closed = time.monotonic()
        self.ready_ms = None  # until the first audio
        self.failed = None  # the reason when the microphone could not be opened
        self.process = self.thread = None

    def command(self):
        return ['pw-record', '--raw', '--rate', str(D.SAMPLE_RATE), '--channels', '1', '--format', 's16',
                '--latency', '50ms', '-P', json.dumps({'application.name': 'GramaDrama', 'media.name': 'Dyktowanie'}), '-']

    def start(self):
        try:
            self.process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.failed = exc.strerror or str(exc)
            return False
        self.thread = threading.Thread(target=self._read, name='gramadrama-recorder', daemon=True)
        self.thread.start()
        return True

    def _read(self):
        limit = int(D.MAX_SECONDS * D.SAMPLE_RATE)
        while True:
            data = self.process.stdout.read(CHUNK * 2)
            if not data:
                break
            chunk = array.array('h')
            chunk.frombytes(data[:len(data) // 2 * 2])
            self.add(chunk)
            if len(self.samples) >= limit:
                self.on_limit()
                break
        if self.process.poll() not in (None, 0) and not self.samples:
            self.failed = tr('Nie znalazłem mikrofonu.')

    def add(self, chunk):
        if self.ready_ms is None:
            self.ready_ms = int((time.monotonic() - self.opened) * 1000)
            if self.on_ready is not None:
                self.on_ready(self.ready_ms)
        room = int(D.MAX_SECONDS * D.SAMPLE_RATE) - len(self.samples)
        self.samples.extend(chunk[:room])
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk)) / 32768 if chunk else 0.0
        self.peak = max(self.peak, rms)
        self.on_level(rms, len(self.samples) / D.SAMPLE_RATE)

    def held(self):
        """Seconds from the start to the stop: how long the key was held, audio or not."""
        return self.closed - self.opened

    def stop(self):
        """The recorded samples (array of int16)."""
        self.closed = time.monotonic()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.thread is not None:
            self.thread.join(2)
        return self.samples


class DebugRecorder(Recorder):
    """Test mode: a steady tone (or silence for 'SILENCE') instead of the microphone, in real time."""

    def start(self):
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._tone, name='gramadrama-recorder', daemon=True)
        self.thread.start()
        return True

    def _tone(self):
        amplitude = 0 if DEBUG_TEXT == 'SILENCE' else 3000
        chunk = array.array('h', [amplitude if i % 2 else -amplitude for i in range(CHUNK)])
        if self.stopped.wait(DEBUG_MIC_MS / 1000):
            return
        while not self.stopped.wait(CHUNK / D.SAMPLE_RATE):
            self.add(chunk)
            if len(self.samples) >= int(D.MAX_SECONDS * D.SAMPLE_RATE):
                self.on_limit()
                return

    def stop(self):
        self.closed = time.monotonic()
        self.stopped.set()
        self.thread.join(2)
        return self.samples


def read_settings():
    global _ui_language
    """(settings, language, cleanup mode, words) from settings.json; defaults for anything unusable. The words: the own
    ones and those added to the dictionary in the text languages (dictionary.py), as D.prompt_words limits them."""
    from . import core, dictionary
    try:
        settings = core.Settings.load()
    except (ValueError, TypeError, OSError):
        settings = core.Settings()
    _ui_language = set_language(settings.primary_language)
    language = D.effective_language(settings.dictation_language, settings.languages)
    cleanup = settings.dictation_cleanup if settings.dictation_cleanup in ('auto', 'always', 'off') else 'off'
    vocabulary = D.prompt_words(dictionary.own_words(settings), dictionary.learned_words(settings.languages))
    return settings, language, cleanup, vocabulary


def _backend(settings):
    """core.Backend for the model, or None (none set, or in test mode a server that is not on 127.0.0.1)."""
    from . import core
    import urllib.parse
    if TEST and not all(urllib.parse.urlsplit(url).hostname in ('127.0.0.1', 'localhost', '::1') for url in core.addresses(settings)):
        return None
    try:
        return core.Backend(settings)
    except ValueError:
        return None


def prewarm(settings, vocabulary):
    """Ollama keeps the fixed start of the cleanup prompt cached; without this the first cleanup
    takes ~7.5 s on a slow server. In the background at the start of a recording; errors do not matter."""
    from . import core
    backend = _backend(settings)
    if backend is None:
        return
    try:
        backend.prewarm(D.cleanup_messages('…', vocabulary, lambda text: core.detect_language(text, settings.languages),
                                           languages=settings.languages))
    except core.BackendError:
        pass


def cleanup(raw, settings, mode, vocabulary, on_cleaning, language=None):
    """(text or None, outcome, milliseconds or None) of Bielik's cleanup of the raw transcript, as on
    macOS: only when asked and needed, never longer than CLEANUP_SECONDS, and only a result made of the
    transcript's own words is used."""
    from . import core
    if mode == 'off':
        return None, 'off', None
    if mode == 'auto' and not D.needs_cleanup(raw, language or core.detect_language(raw, settings.languages)):
        return None, 'not_needed', None
    if D.cleanup_too_long(raw):
        return None, 'too_long', None
    backend = _backend(settings)
    if backend is None:
        return None, 'error', None
    on_cleaning()
    started, cancel, box = time.monotonic(), threading.Event(), {}

    def run():
        try:
            box['output'] = backend.chat(D.cleanup_messages(raw, vocabulary,
                                         lambda text: core.detect_language(text, settings.languages),
                                         languages=settings.languages), 0, raw, cancel=cancel,
                                         timeout=D.CLEANUP_SECONDS + 1)
        except Exception as exc:  # BackendError, Cancelled or anything else: the transcript stays
            box['error'] = exc
    worker = threading.Thread(target=run, name='gramadrama-cleanup', daemon=True)
    worker.start()
    worker.join(D.CLEANUP_SECONDS)
    ms = int((time.monotonic() - started) * 1000)
    if worker.is_alive():
        cancel.set()
        return None, 'timeout', ms
    if 'output' not in box:
        return None, 'error', ms
    cleaned = core.postprocess(box['output'], raw)
    safe = D.cleanup_looks_safe(cleaned, raw) and not core.foreign_letters(cleaned, raw)
    return (cleaned, 'ok', ms) if safe else (None, 'rejected', ms)


class Service:
    def __init__(self):
        self.local = DebugWhisper() if DEBUG_TEXT is not None else Whisper()  # loaded when a dictation needs it
        self.local_started = False
        self.recorder = None
        self.id = None
        self.started = 0.0
        self.settings = None
        self.alternate = False  # Shift was held with the key: this dictation is in the other language
        self.last_command = time.monotonic()
        self.busy = False  # recording or processing: the idle timer waits
        self.lock = threading.Lock()

    def serve(self):
        threading.Thread(target=self._load, name='gramadrama-whisper', daemon=True).start()
        threading.Thread(target=self._idle, name='gramadrama-idle', daemon=True).start()
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except ValueError:
                continue
            self.last_command = time.monotonic()
            name, sid = command.get('cmd'), command.get('id')
            if name == 'start':
                self.start(sid, alternate=command.get('alternate') is True)
            elif name == 'confirm':
                self.confirm(sid)
            elif name == 'alternate':
                self.make_alternate(sid)
            elif name == 'stop':
                self.stop(sid, limit=False)
            elif name == 'cancel':
                self.cancel(sid)
            elif name == 'quit':
                break
        self.cancel(self.id)

    def _load(self):
        """At the start: only load a local model when the selected backend is local."""
        settings = read_settings()[0]
        problem = configuration_problem(settings)
        if problem or settings.whisper_uses_server:
            emit(ev='model', state='failed' if problem else 'ready', device='server', ms=0)
        else:
            self._load_local()

    def _load_local(self):
        with self.lock:
            if self.local_started:
                return
            self.local_started = True
        self.local.load()
        emit(ev='model', state='ready' if getattr(self.local, 'error', None) is None else 'failed',
             device=self.local.device, ms=self.local.ms)

    def whisper_for(self, settings):
        """Whisper of this dictation: the server of settings.json, or the built-in model (loaded now if need be)."""
        problem = configuration_problem(settings)
        if problem:
            raise SpeechError(problem, 'config')
        if settings.whisper_uses_server:
            from . import core
            if TEST and not all(urllib.parse.urlsplit(url).hostname in ('127.0.0.1', 'localhost', '::1') for url in core.addresses(settings)):
                raise SpeechError(tr('Tryb testowy: dozwolony jest tylko serwer na 127.0.0.1.'), 'test_mode')
            return RemoteWhisper(settings)
        self._load_local()
        return self.local

    def _idle(self):
        while True:
            time.sleep(60)
            if not self.busy and time.monotonic() - self.last_command > IDLE_SECONDS:
                os._exit(0)  # frees the GPU; the engine starts the service again when needed

    def spoken(self):
        """The language of this dictation: the setting's, or the other one when Shift was held (macOS)."""
        languages = self.settings.languages if self.settings is not None else ('pl', 'en')
        return (D.alternate_language(self.language, languages) if self.alternate
                else D.effective_language(self.language, languages))

    def make_alternate(self, sid):
        """Shift came down while recording: this dictation goes in the other language. The pill shows it with
        the next level."""
        with self.lock:
            if self.recorder is not None and sid == self.id and len(self.settings.languages) > 1:
                self.alternate = True

    def start(self, sid, alternate=False):
        with self.lock:
            if self.recorder is not None:  # the engine never overlaps dictations; a stray start ends the old one
                self._drop()
            self.settings, self.language, self.mode, self.vocabulary = read_settings()
            self.id, self.started, self.busy = sid, time.monotonic(), True
            problem = configuration_problem(self.settings)
            if problem:
                self.busy = False
                emit(ev='error', id=sid, message=problem, log={'outcome': 'asr_error', 'why': 'config'})
                return
            self.alternate = alternate and len(self.settings.languages) > 1
            kind = DebugRecorder if DEBUG_TEXT is not None else Recorder
            self.recorder = kind(lambda rms, s: emit(ev='level', id=sid, level=round(D.level(rms), 3), s=round(s, 2),
                                                     lang=D.LABELS[self.spoken()]),
                                 lambda: self.stop(sid, limit=True),
                                 lambda ms: emit(ev='listening', id=sid, ms=ms))
            if not self.recorder.start():
                emit(ev='error', id=sid, message=tr('Nie mogę włączyć mikrofonu: %s') % self.recorder.failed,
                     log={'outcome': 'mic_error'})
                self.recorder, self.busy = None, False

    def confirm(self, sid):
        """The key was held for ARM_MS: a dictation, so the model's cleanup warms up (never for a tap or a shortcut)."""
        with self.lock:
            if self.recorder is None or sid != self.id or self.mode == 'off':
                return
            settings, vocabulary = self.settings, self.vocabulary
        threading.Thread(target=prewarm, args=(settings, vocabulary), daemon=True).start()

    def _drop(self):
        recorder, self.recorder = self.recorder, None
        if recorder is not None:
            recorder.stop()

    def cancel(self, sid):
        with self.lock:
            if self.recorder is not None and sid == self.id:
                self._drop()
                self.busy = False

    def stop(self, sid, limit):
        with self.lock:
            if self.recorder is None or sid != self.id:
                return
            recorder, self.recorder = self.recorder, None
        threading.Thread(target=self._finish, args=(sid, recorder, limit), name='gramadrama-finish', daemon=True).start()

    def _finish(self, sid, recorder, limit):
        try:
            samples = recorder.stop()
            seconds = len(samples) / D.SAMPLE_RATE
            language = self.spoken()
            log = {'audio_ms': int(seconds * 1000), 'language': language, 'alternate': self.alternate,
                   'mic_ms': recorder.ready_ms}
            if recorder.failed and not samples:
                emit(ev='error', id=sid, message=tr('Nie mogę włączyć mikrofonu: %s') % recorder.failed, log={'outcome': 'mic_error'})
                return
            if recorder.ready_ms is None and recorder.held() >= D.MIN_SECONDS:  # let go during "Budzę mikrofon…"
                emit(ev='error', id=sid, message=tr('Mikrofon jeszcze się nie włączył — poczekaj na „Słucham…”.'),
                     log=dict(log, outcome='mic_error', why='not_ready'))
                return
            if seconds < D.MIN_SECONDS:
                emit(ev='short', id=sid)
                return
            if recorder.peak < D.SILENCE_PEAK:
                emit(ev='silence', id=sid, log=dict(log, outcome='silence'))
                return
            emit(ev='recognizing', id=sid, limit=limit)
            try:
                whisper = self.whisper_for(self.settings)
            except Exception as exc:
                emit(ev='error', id=sid, message=tr('Nie udało się rozpoznać mowy: %s') % (str(exc)[:200] or type(exc).__name__),
                     log=dict(log, outcome='asr_error', why=failure_kind(exc)))
                return
            emit_result(sid, whisper, samples, self.settings, language, self.mode, self.vocabulary, log)
        finally:
            self.busy = False


def floats(samples):
    """int16 samples (array) as numpy float32 in -1..1, what faster-whisper takes."""
    import numpy
    if isinstance(samples, array.array):
        return numpy.frombuffer(samples.tobytes(), dtype=numpy.int16).astype(numpy.float32) / 32768
    return samples


def emit_result(sid, whisper, samples, settings, language, mode, vocabulary, log, send=None):
    """Whisper, the filters, the tidy-up and the optional cleanup; one 'result' or 'error' event."""
    send = send or emit
    prompt = D.whisper_prompt(vocabulary, language)
    started = time.monotonic()
    try:
        audio = samples if isinstance(whisper, (DebugWhisper, RemoteWhisper)) else floats(samples)
        text = whisper.transcribe(audio, D.LANGUAGES[language], prompt)
        retry = None
        if language == 'pl':
            from . import core
            if D.looks_translated(D.filter_transcript(text, prompt), core.detect_language):
                # Forced Polish, yet an English sentence: Whisper translated. The prompt is what pulls it that way
                # most often, so one more try without it (macOS dictation_english_retry).
                again = whisper.transcribe(audio, D.LANGUAGES[language], '')
                retry = not D.looks_translated(D.filter_transcript(again, ''), core.detect_language)
                if retry:
                    text, prompt = again, ''
    except Exception as exc:
        send(ev='error', id=sid, message=tr('Nie udało się rozpoznać mowy: %s') % (str(exc)[:200] or type(exc).__name__),
             log=dict(log, outcome='asr_error', why=failure_kind(exc)))
        return
    log['asr_ms'] = int((time.monotonic() - started) * 1000)
    log['asr'] = 'server' if isinstance(whisper, RemoteWhisper) else 'local'
    if retry is not None:
        log['english_retry'] = 'fixed' if retry else 'kept'  # whether the second try came out Polish
    raw = D.filter_transcript(text, prompt)
    log['raw_chars'] = len(raw)
    if not raw.strip():
        send(ev='result', id=sid, text='', note='', log=dict(log, cleanup='off', chars=0))
        return
    from . import core
    text_language = core.detect_language(raw, settings.languages) if language == 'auto' else language
    text = D.tidy(raw, text_language)
    cleaned, outcome, ms = cleanup(raw, settings, mode, vocabulary, lambda: send(ev='cleaning', id=sid),
                                   language=text_language)
    if cleaned is not None:
        text = cleaned  # used as it is, as on macOS
    log['cleanup'] = outcome
    if ms is not None:
        log['cleanup_ms'] = ms
    log['chars'] = len(text)
    send(ev='result', id=sid, text=text, note=tr(NOTES.get(outcome, ''), language=settings.primary_language), log=log)


def self_test():
    whisper = Whisper()
    whisper.load()
    if whisper.model is None:
        print(tr('Model mowy się nie wczytał: %s') % whisper.error)
        return 1
    import numpy
    tone = numpy.sin(numpy.arange(D.SAMPLE_RATE * 2) * 2 * numpy.pi * 220 / D.SAMPLE_RATE).astype(numpy.float32) * 0.01
    started = time.monotonic()
    whisper.transcribe(tone, 'pl', D.whisper_prompt([], 'pl'))
    print('Whisper large-v3-turbo: %s, wczytanie %d ms, 2 s nagrania w %d ms' % (
        whisper.device, whisper.ms, (time.monotonic() - started) * 1000))
    return 0


def transcribe_files(paths_, language, words, clean):
    """The whole path without the microphone (macOS --transcribe), with the timings."""
    from faster_whisper import decode_audio
    settings, _language, mode, vocabulary = read_settings()
    language = language or _language
    vocabulary = D.parse_vocabulary(words) if words is not None else vocabulary
    mode = {True: 'always', False: 'off', None: mode}[clean]
    whisper = Whisper()
    whisper.load()
    for path in paths_:
        samples = decode_audio(path, sampling_rate=D.SAMPLE_RATE)
        peak = max((math.sqrt(float((samples[i:i + CHUNK] ** 2).mean())) for i in range(0, len(samples), CHUNK)), default=0)
        print('%s: %.1f s, szczyt %.4f' % (path, len(samples) / D.SAMPLE_RATE, peak))
        events = []
        emit_result(0, whisper, samples, settings, language, mode, vocabulary, {'audio_ms': int(len(samples) / 16)},
                    send=lambda **event: events.append(event))
        for event in events:
            if event['ev'] == 'result':
                print('  → %s' % (event['text'] or '(nic)'))
                print('  %s' % json.dumps(event['log'], ensure_ascii=False))
                if event['note']:
                    print('  %s' % event['note'])
            elif event['ev'] == 'error':
                print('  ! %s' % event['message'])
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    if '--transcribe' in argv:
        options = {a.split('=', 1)[0]: a.split('=', 1)[1] for a in argv if a.startswith('--') and '=' in a}
        files = [a for a in argv if not a.startswith('--')]
        clean = True if '--clean' in argv else False if '--no-clean' in argv else None
        return transcribe_files(files, options.get('--lang'), options.get('--words'), clean)
    Service().serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
