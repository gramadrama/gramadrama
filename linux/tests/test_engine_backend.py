import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from gramadrama import core
from gramadrama.engine import backend, fallback_ui
from gramadrama.engine.backend import DebugBackend, ProcessBackend
from stub_server import Stub, closed_port, sse, stream

LINUX = pathlib.Path(__file__).resolve().parent.parent
TEXT = 'ala ma Kota i psa'


class ResultPayloadTests(unittest.TestCase):
    def test_shared_payload_matches_emitted_backend_result_without_output(self):
        with mock.patch.object(backend, '_emit') as emit:
            payload = backend.result_payload(core, fallback_ui.lines, TEXT, 'Ala ma kota i psa.', 'fix', languages=['pl', 'en'])
            emit.assert_not_called()
            segments = backend._result(core, fallback_ui.lines, TEXT, 'Ala ma kota i psa.', 'fix', languages=['pl', 'en'])
        emit.assert_called_once_with(**payload)
        self.assertEqual(segments, payload['segments'])
        self.assertTrue(payload['changes'])
        self.assertGreater(payload['count'], 0)

    def test_footer_time_has_the_decimal_mark_of_the_interface(self):
        start = time.monotonic() - 1.34
        for language, expected in (('pl', '1,3 s'), ('en', '1.3 s')):
            with self.subTest(language=language), mock.patch.object(backend, '_ui_language', language):
                self.assertEqual(backend._seconds(start), expected)


class Calls(list):
    """A sink's calls; whys: the cause codes of its errors in order, which go to the log only."""


class Sink:
    def __init__(self):
        self.calls = Calls()
        self.calls.whys = []

    def backend_partial(self, sid, text, rows, info=None):
        self.calls.append(('partial', sid, text, rows, info))

    def backend_result(self, sid, corrected, rows, info=None):
        self.calls.append(('result', sid, corrected, rows, info))

    def backend_changes(self, sid, changes, count):
        self.calls.append(('changes', sid, changes, count))

    def backend_error(self, sid, message, why=None):
        self.calls.append(('error', sid, message))
        self.calls.whys.append(why)


class Loop:
    """Stands in for GLib.idle_add: callables queued by the worker run on the test thread."""

    def __init__(self):
        self.queue = queue.Queue()

    def post(self, fn):
        self.queue.put(fn)

    def drain(self, test):
        while not self.queue.empty():
            test.assertIs(self.queue.get()(), False)


def run(test, mode='fix', text=TEXT, delay=0, reply=None, sid=7):
    sink, loop = Sink(), Loop()
    engine = DebugBackend(sink, delay=delay, reply=reply)
    engine.start(sid, text, mode, loop.post).join(5)
    loop.drain(test)
    return sink.calls


class DebugBackendTests(unittest.TestCase):
    def test_transforms(self):
        cases = [
            ('fix', 'ALA MA KOTA I PSA'),
            ('improve', 'ALA MA kOTA I PSA'),
            ('formal', 'Ala Ma Kota I Psa'),
            ('casual', 'ala ma kota i psa'),
            ('shorter', 'ala ma Kota'),
            ('translate', 'psa i Kota ma ala'),
        ]
        for mode, want in cases:
            with self.subTest(mode):
                self.assertEqual(run(self, mode)[-1][:3], ('result', 7, want))
        self.assertEqual(backend.TRANSFORMS['shorter']('jedno'), 'jedno')
        self.assertEqual(backend.TRANSFORMS['formal']('zażółć gęślą'), 'Zażółć Gęślą')

    def test_streams_three_partials_then_the_result(self):
        calls = run(self, delay=20)
        self.assertEqual([c[0] for c in calls], ['partial', 'partial', 'partial', 'result'])
        partials = [c[2] for c in calls[:3]]
        self.assertEqual(partials, sorted(partials, key=len))
        self.assertTrue(all('ALA MA KOTA I PSA'.startswith(p) for p in partials))
        self.assertEqual([c[3] for c in calls[:3]], [tuple(fallback_ui.lines((('same', p),))) for p in partials])
        # the diff is wrapped by the worker: the main loop only slices pages
        self.assertEqual(calls[-1][3], tuple(fallback_ui.lines(core.diff_parts(TEXT, TEXT.upper()))))

    def test_cancel_stops_the_worker_during_its_sleep(self):
        sink, loop = Sink(), Loop()
        engine = DebugBackend(sink, delay=4000)
        started = time.monotonic()
        worker = engine.start(1, TEXT, 'fix', loop.post)
        engine.cancel(1)
        worker.join(5)
        self.assertLess(time.monotonic() - started, 1)
        loop.drain(self)
        self.assertEqual(sink.calls, [])

    def test_cancel_after_posting_suppresses_delivery(self):
        sink, loop = Sink(), Loop()
        engine = DebugBackend(sink, delay=0)
        engine.start(3, TEXT, 'fix', loop.post).join(5)
        engine.cancel(3)
        loop.drain(self)
        self.assertEqual(sink.calls, [])
        engine.cancel(99)  # unknown ids are ignored

    def test_reply_overrides(self):
        self.assertEqual(run(self, reply='Dokładnie to.')[-1][2], 'Dokładnie to.')
        with mock.patch.dict(os.environ, {'GRAMADRAMA_DEBUG_REPLY': 'Z env.', 'GRAMADRAMA_ENGINE_TEST': '1'}):
            self.assertEqual(run(self)[-1][2], 'Z env.')
            self.assertEqual(run(self, reply='Hook wygrywa.')[-1][2], 'Hook wygrywa.')

    def test_environment_hooks_only_in_test_mode(self):
        env = {k: v for k, v in os.environ.items() if k != 'GRAMADRAMA_ENGINE_TEST'}
        env.update(GRAMADRAMA_DEBUG_REPLY='Podmieniony wynik.', GRAMADRAMA_DEBUG_DELAY_MS='0')
        with mock.patch.dict(os.environ, env, clear=True):
            sink, loop = Sink(), Loop()
            started = time.monotonic()
            DebugBackend(sink).start(1, TEXT, 'fix', loop.post).join(10)
            elapsed = time.monotonic() - started
            loop.drain(self)
        self.assertEqual(sink.calls[-1][2], TEXT.upper())  # the stray reply is ignored
        self.assertGreaterEqual(elapsed, backend.DEFAULT_DELAY_MS / 1000 * 0.9)  # and the delay too

    def test_work_runs_in_a_child_process_that_cancel_kills(self):
        # The work (model wait, diff) must neither hold the engine's GIL nor outlive a cancel. The
        # child is kept busy by the debug delay (the diff itself is fast since sentences are aligned).
        text = ('Drugi akapit ma błąd 😀 i słowo KOT w środku zdania. ' * 200)[:6000]
        sink, loop = Sink(), Loop()
        engine = DebugBackend(sink, delay=8000)
        worker = engine.start(5, text, 'fix', loop.post)
        deadline = time.monotonic() + 5
        while worker.child is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(worker.child)
        self.assertNotEqual(worker.child.pid, os.getpid())
        time.sleep(0.5)  # the child is diffing now
        self.assertIsNone(worker.child.poll())
        started = time.monotonic()
        engine.cancel(5)
        worker.join(5)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNotNone(worker.child.returncode)
        loop.drain(self)
        self.assertEqual(sink.calls, [])

    def test_errors(self):
        cases = [
            ('simulated', dict(reply='ERROR: Serwer nie odpowiada.'), 'Serwer nie odpowiada.'),
            ('simulated without text', dict(reply='ERROR:'), 'Testowy błąd backendu.'),
            ('unknown mode', dict(mode='poetry'), 'Nieznany tryb.'),
        ]
        for name, kw, message in cases:
            with self.subTest(name):
                calls = run(self, **kw)
                self.assertEqual(calls, [('error', 7, message)])
                self.assertEqual(calls.whys, ['test'])

    def test_delay_from_environment(self):
        cases = [('25', 25), ('0', 0), ('-5', 0), ('abc', 300), (None, 300)]
        for value, want in cases:
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != 'GRAMADRAMA_DEBUG_DELAY_MS'}
                if value is not None:
                    env['GRAMADRAMA_DEBUG_DELAY_MS'] = value
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(backend.delay_ms(), want)


class RealBackendTests(unittest.TestCase):
    """The real child against a stub Ollama/LanguageTool on 127.0.0.1 (settings.json in a temp dir)."""

    def setUp(self):
        self.stub = Stub().__enter__()
        self.config = tempfile.TemporaryDirectory()
        self.write_settings(self.stub.settings())
        # For the whole test: a child must never see the user's settings (the real server), and
        # test mode keeps the loopback guard on even if a test writes an incomplete settings.json.
        self.env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.config.name, 'GRAMADRAMA_ENGINE_TEST': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.stub.__exit__()
        self.config.cleanup()

    def write_settings(self, data):
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data) if isinstance(data, dict) else data)

    def run_real(self, text, mode='fix', target=None, sid=7):
        sink, loop = Sink(), Loop()
        ProcessBackend(sink, real=True).start(sid, text, mode, loop.post, target).join(20)
        loop.drain(self)
        return sink.calls

    def final(self, calls):
        """The result's info as the panel ends up showing it: LanguageTool's reasons may come after
        the result when the stub's Bielik answers first."""
        index = next(i for i, c in reversed(list(enumerate(calls))) if c[0] == 'result')
        info = dict(calls[index][4])
        for call in calls[index + 1:]:
            info['changes'], info['count'] = call[2], call[3]
        return info

    def test_streams_and_returns_the_bielik_result(self):
        self.stub.chunks = stream('Ala ma kota, a kot ma Alę.', pieces=4)
        self.stub.chunk_delay = 0.12  # slower than the partial interval, so partials get through
        calls = self.run_real('ala ma kota a kot ma ale')
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds[-1], 'result', calls)
        self.assertGreaterEqual(kinds.count('partial'), 2)
        self.assertEqual(calls[-1][2], 'Ala ma kota, a kot ma Alę.')
        self.assertRegex(calls[-1][4]['note'], r'^Bielik-test · \d+,\d s$')  # model · time, as on macOS (its display name)
        self.assertEqual(calls[0][4]['note'], 'Bielik-test')  # while streaming: the model alone
        self.assertIsNone(calls[-1][4]['target'])
        self.assertEqual(calls[-1][3], tuple(fallback_ui.lines(core.diff_parts('ala ma kota a kot ma ale', 'Ala ma kota, a kot ma Alę.'))))
        request = self.stub.chats()[-1]
        self.assertEqual(request['model'], 'bielik-test')
        self.assertIn('ala ma kota a kot ma ale', request['messages'][-1]['content'])

    def test_result_carries_the_diff_and_the_change_list(self):
        text = 'Ala 😀 poszedł do sklep'
        start = len(text[:text.index('poszedł')].encode('utf-16-le')) // 2
        self.stub.lt_matches = [{'offset': start, 'length': 7, 'message': 'Niezgodność rodzaju',
                                 'rule': {'id': 'GRAM'}, 'replacements': []}]
        self.stub.chunks = stream('Ala 😀 poszła do sklepu')
        info = self.final(self.run_real(text))
        self.assertEqual(info['segments'], [list(p) for p in core.diff_parts(text, 'Ala 😀 poszła do sklepu')])
        self.assertEqual(info['changes'][0], {'from': 'poszedł', 'to': 'poszła', 'reason': 'Niezgodność rodzaju', 'segments': [1, 3]})
        self.assertEqual(info['count'], len(info['changes']))
        self.assertIn('/v2/check', [path for path, _ in self.stub.requests])  # LanguageTool ran alongside

    def test_change_reasons_without_languagetool(self):
        self.write_settings(self.stub.settings(languagetool='http://127.0.0.1:%d' % closed_port()))
        self.stub.chunks = stream('w piątek')
        info = self.run_real('w piatek')[-1][4]
        self.assertEqual(info['changes'], [{'from': 'piatek', 'to': 'piątek', 'reason': 'Polskie znaki', 'segments': [1, 3]}])

    def test_translations_have_no_diff(self):
        self.stub.chunks = stream('Thanks.')
        info = self.run_real('Dzięki.', mode='translate')[-1][4]
        self.assertEqual((info['segments'], info['changes'], info['count']), ([], [], 0))
        self.assertNotIn('/v2/check', [path for path, _ in self.stub.requests])

    def test_partials_are_throttled(self):
        self.stub.chunks = stream('x' * 200, pieces=100)  # 100 chunks at once
        calls = self.run_real('y' * 200, mode='improve')
        self.assertLessEqual([c[0] for c in calls].count('partial'), 3)

    def test_unreachable_bielik_is_an_error_in_every_mode(self):
        # No LanguageTool-only answer in Popraw either, as on macOS since 2026-09-29 (branding/PANEL.md).
        self.write_settings(self.stub.settings(ollama='http://127.0.0.1:%d' % closed_port()))
        self.stub.lt_matches = [{'offset': 4, 'length': 3, 'message': 'Literówka', 'replacements': [{'value': 'zły'}]}]
        for mode in ('fix', 'improve', 'formal', 'translate'):
            with self.subTest(mode):
                calls = self.run_real('Ten zly tekst.', mode=mode)  # translate announces its direction first
                self.assertEqual(calls[-1], ('error', 7, backend.UNREACHABLE))
                self.assertEqual(calls.whys, ['refused'])  # the log's cause: nothing listens on the port
                self.assertNotIn('result', [c[0] for c in calls])

    def test_nothing_reachable_is_an_error(self):
        port = closed_port()
        self.write_settings(self.stub.settings(ollama='http://127.0.0.1:%d' % port, languagetool='http://127.0.0.1:%d' % port))
        calls = self.run_real('Ten zly tekst.')
        self.assertEqual(calls, [('error', 7, 'Serwer modelu zszedł ze sceny — sprawdź połączenie.')])
        self.assertEqual(calls.whys, ['refused'])

    def test_model_errors_are_reported(self):
        cases = [
            ('changed link', stream('Zobacz https://example.com/b'), 'Zobacz https://example.com/a',
             'Model zmienił link, wzmiankę, kod lub emoji. Wynik odrzucono; spróbuj ponownie.', 'protected_changed'),
            ('truncated', [{'message': {'content': 'uci'}, 'done': True, 'done_reason': 'length'}], 'tekst',
             'Model uciął odpowiedź. Zaznacz krótszy fragment.', 'truncated_reply'),
            ('model error', [{'error': 'model not found'}], 'tekst', 'Błąd modelu: model not found', 'model_error'),
        ]
        for name, chunks, text, message, why in cases:
            with self.subTest(name):
                self.stub.chunks = chunks
                calls = self.run_real(text)
                self.assertEqual(calls[-1], ('error', 7, message))
                self.assertEqual(calls.whys, [why])  # the code, never the server's words
                self.assertNotIn('result', [c[0] for c in calls])

    def test_connection_dropped_inside_a_chunk(self):
        self.stub.chunks = stream('Ala ma kota i psa, a pies ma kość.', pieces=5)
        self.stub.drop_after = 2  # Ollama streams chunked HTTP/1.1; the drop lands mid-chunk
        calls = self.run_real('ala ma kota')
        self.assertEqual(calls[-1], ('error', 7, 'Połączenie przerwano przed końcem odpowiedzi. Spróbuj ponownie.'))
        self.assertEqual(calls.whys, ['interrupted'])
        self.stub.drop_after = 5  # the same framing without a drop still works
        self.assertEqual(self.run_real('ala ma kota')[-1][:3], ('result', 7, 'Ala ma kota i psa, a pies ma kość.'))

    def test_translation_direction_is_announced_before_the_model_answers(self):
        self.stub.chunks, self.stub.chunk_delay = stream('Thanks.'), 0.5
        calls = self.run_real('Dzięki.', mode='translate')
        self.assertEqual(calls[0][:4], ('partial', 7, '', ()))
        self.assertEqual(calls[0][4]['target'], 'en')
        self.stub.chunk_delay = 0

    def test_cancel_between_spawn_and_registration_kills_the_child(self):
        # The race the review found: cancel() lands after Popen, before _run registers the child.
        sink, loop = Sink(), Loop()
        engine = ProcessBackend(sink, real=True)
        spawned = []
        real_popen = backend.subprocess.Popen

        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            spawned.append(child)
            engine.cancel(4)
            return child
        with mock.patch.object(backend.subprocess, 'Popen', popen):
            worker = engine.start(4, 'tekst', 'fix', loop.post)
            worker.join(5)
        self.assertIsNotNone(spawned[0].returncode)  # killed and reaped, not left running
        loop.drain(self)
        self.assertEqual(sink.calls, [])
        self.assertEqual(self.stub.requests, [])  # it never got the job

    def test_http_error_is_reported_without_fallback(self):
        self.stub.chat_status = 404
        calls = self.run_real('tekst')
        self.assertEqual(calls, [('error', 7, 'Serwer zwrócił HTTP 404. Sprawdź adres i nazwę modelu.')])
        self.assertEqual(calls.whys, ['http_404'])

    def test_ollama_says_what_went_wrong(self):
        # The reason from Ollama's JSON; the hint only for 404.
        self.stub.chat_status, self.stub.chat_error = 404, 'model "bielik-test" not found, try pulling it first'
        self.assertEqual(self.run_real('tekst'), [('error', 7, 'model "bielik-test" not found, try pulling it first '
                                                   '(HTTP 404). Sprawdź adres i nazwę modelu.')])
        self.stub.chat_status, self.stub.chat_error = 500, 'llama runner process has terminated'
        self.assertEqual(self.run_real('tekst'), [('error', 7, 'llama runner process has terminated (HTTP 500).')])

    def test_the_result_does_not_wait_for_languagetool(self):
        # The result right after the model's answer; the reasons follow.
        text = 'Ala 😀 poszedł do sklep'
        start = len(text[:text.index('poszedł')].encode('utf-16-le')) // 2
        self.stub.lt_matches = [{'offset': start, 'length': 7, 'message': 'Niezgodność rodzaju',
                                 'rule': {'id': 'GRAM'}, 'replacements': []}]
        self.stub.lt_delay = 1.5
        self.stub.chunks = stream('Ala 😀 poszła do sklepu')
        sink, loop = Sink(), Loop()
        engine = ProcessBackend(sink, real=True)
        started = time.monotonic()
        worker = engine.start(7, text, 'fix', loop.post)
        while not self._has_result(loop, sink):
            self.assertLess(time.monotonic() - started, 10, 'no result')
            time.sleep(0.02)
        self.assertLess(time.monotonic() - started, 1.4)  # before LanguageTool answered
        self.assertEqual(sink.calls[-1][4]['changes'][0]['reason'], 'Gramatyka / styl')
        worker.join(20)
        loop.drain(self)
        self.assertEqual(sink.calls[-1][0], 'changes')
        self.assertEqual(sink.calls[-1][2][0], {'from': 'poszedł', 'to': 'poszła', 'reason': 'Niezgodność rodzaju', 'segments': [1, 3]})
        self.assertEqual(sink.calls[-1][3], len(sink.calls[-2][4]['changes']))

    def _has_result(self, loop, sink):
        loop.drain(self)
        return any(c[0] == 'result' for c in sink.calls)

    def test_languagetool_gets_the_text_language(self):
        # 'auto' only for mixed text (and preferredVariants only then).
        self.stub.chunks = stream('x')
        for text, language in (('Myślę, że jutro będzie lepiej.', 'pl-PL'), ('I think it will be better tomorrow.', 'en-US'),
                               ('Mam call o 14, a potem review PR-a, sorry for the delay', 'auto')):
            with self.subTest(language=language):
                self.stub.requests.clear()
                self.run_real(text)
                form = next(body for path, body in self.stub.requests if path == '/v2/check')
                self.assertEqual(form['language'], language)
                self.assertEqual('preferredVariants' in form, language == 'auto')
                self.assertEqual(form['motherTongue'], 'pl-PL')

    def test_translation_note_names_the_direction(self):
        self.stub.chunks = stream('Thanks.')
        calls = self.run_real('Dzięki.', mode='translate')
        self.assertEqual(calls[0][4]['note'], 'Bielik-test · PL → EN')
        self.assertRegex(calls[-1][4]['note'], r'^Bielik-test · PL → EN · \d+,\d s$')

    def test_warm_up_loads_the_model_with_the_same_context(self):
        # Another num_ctx would make Ollama load the model again.
        backend.warm_main()
        path, body = self.stub.requests[-1]
        self.assertEqual(path, '/api/generate')
        self.assertEqual(body, {'model': 'bielik-test', 'keep_alive': '24h', 'options': {'num_ctx': core.CONTEXT_SIZE}})
        self.stub.chunks = stream('Ala.')
        self.run_real('ala')
        self.assertEqual(self.stub.chats()[-1]['options']['num_ctx'], core.CONTEXT_SIZE)
        self.stub.requests.clear()
        self.write_settings({'ollama_url': 'http://192.0.2.1:11434', 'languagetool_url': self.stub.url, 'model': 'm'})
        backend.warm_main()  # test mode: never a real server
        self.assertEqual(self.stub.requests, [])

    def test_translation_direction(self):
        self.stub.chunks = stream('Thanks, I will check it tomorrow.')
        calls = self.run_real('Dzięki, sprawdzę to jutro.', mode='translate')
        self.assertEqual(calls[-1][4]['target'], 'en')
        self.assertTrue(self.stub.chats()[-1]['messages'][-1]['content'].startswith('Translate into English.'))
        self.assertEqual(calls[-1][3], tuple(fallback_ui.lines((('same', 'Thanks, I will check it tomorrow.'),))))
        self.stub.chunks = stream('Dzięki, sprawdzę to jutro.')
        calls = self.run_real('Thanks, I will check it tomorrow.', mode='translate')
        self.assertEqual(calls[-1][4]['target'], 'pl')
        calls = self.run_real('Dzięki, sprawdzę to jutro.', mode='translate', target='pl')  # reversed by the user
        self.assertEqual(calls[-1][4]['target'], 'pl')
        self.assertTrue(self.stub.chats()[-1]['messages'][-1]['content'].startswith('Translate into Polish.'))

    def test_environment_is_taken_when_the_session_starts(self):
        # The child is spawned later by the worker thread; a later change of os.environ must not
        # reach it (this once sent a test text to the user's real server).
        self.stub.chunks = stream('Wynik.')
        sink, loop = Sink(), Loop()
        engine = ProcessBackend(sink, real=True)
        with mock.patch.object(backend.subprocess, 'Popen', wraps=backend.subprocess.Popen) as popen:
            worker = engine.start(8, 'wynik', 'fix', loop.post)
            with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': '/nonexistent'}):
                worker.join(20)
        loop.drain(self)
        self.assertEqual(popen.call_args.kwargs['env']['XDG_CONFIG_HOME'], self.config.name)
        self.assertEqual(sink.calls[-1][:3], ('result', 8, 'Wynik.'))

    def test_test_mode_never_contacts_a_real_server(self):
        for settings in ({'llm_url': 'http://192.0.2.1:11434', 'languagetool_url': self.stub.url, 'model': 'm'},
                         {'llm_url': self.stub.url, 'languagetool_url': 'http://192.0.2.1:8010', 'model': 'm'}):
            with self.subTest(settings=settings):
                self.write_settings(settings)
                calls = self.run_real('tekst')
                self.assertEqual(calls, [('error', 7, 'Tryb testowy: dozwolony jest tylko serwer na 127.0.0.1.')])
                self.assertEqual(self.stub.requests, [])

    def test_the_model_server_may_speak_the_openai_way(self):
        # any OpenAI-compatible server, with a key: the whole path through the backend child
        self.write_settings(self.stub.settings(api='openai', key='sk-k'))
        self.stub.require_key = 'sk-k'
        self.stub.events = sse('Ten zły tekst.')
        calls = self.run_real('Ten zly tekst.')
        self.assertEqual(next(c for c in calls if c[0] == 'result')[2], 'Ten zły tekst.')
        info = self.final(calls)
        self.assertEqual(info['changes'][0]['to'], 'zły')
        self.assertEqual((len(self.stub.completions()), self.stub.chats()), (1, []))  # not Ollama's /api/chat
        self.assertEqual({a for p, a in self.stub.headers_seen if p.endswith('/chat/completions')}, {'Bearer sk-k'})
        self.assertNotIn('/api/generate', [p for p, _ in self.stub.requests])  # no warm-up for such a server
        self.stub.requests.clear()
        self.write_settings(self.stub.settings(api='openai', ollama='http://127.0.0.1:%d' % closed_port(), key='sk-k'))
        self.assertEqual(self.run_real('Ten tekst.')[-1], ('error', 7, backend.UNREACHABLE))  # the same words as for Ollama
        self.write_settings(self.stub.settings(api='openai', key='sk-wrong'))
        error = self.run_real('Ten tekst.')[-1]
        self.assertEqual(error[0], 'error')
        self.assertIn('klucz API', error[2])
        self.assertNotIn('sk-wrong', error[2])

    def test_selected_languages_reach_translation_and_single_language_never_sends_text(self):
        settings = dict(self.stub.settings(), primary_language='de', secondary_language='en')
        self.write_settings(settings)
        self.stub.chunks = stream('Hello!')
        calls = self.run_real('Hallo!', mode='translate')
        info = self.final(calls)
        self.assertEqual((info['target'], info['languages']), ('en', ['de', 'en']))
        self.assertIn('DE → EN', info['note'])
        self.stub.requests.clear()
        self.write_settings(dict(settings, primary_language='en', secondary_language=''))
        self.assertEqual(self.run_real('Hallo!', mode='translate'),
                         [('error', 7, 'Translation requires a second language. '
                           'Add one in GramaDrama → Writing → Text languages.')])
        self.assertEqual(self.stub.requests, [])

    def test_a_new_install_is_told_what_is_missing(self):
        # no developer's servers (G3): nothing is asked of anyone until the model is chosen (this computer's Ollama by
        # default, here a port with nothing on it: a test never asks the Ollama of the computer it runs on)
        nothing = 'http://127.0.0.1:%d' % closed_port()
        for settings, message, whys in (
                ({'llm_local_url': nothing}, 'Ustawienia: Wybierz model (aplikacja GramaDrama › Usługi).',
                 ('no_ollama', 'ollama_down')),  # which one: whether Ollama is installed where the test runs
                ({'llm_location': 'server', 'model': 'm'},
                 'Ustawienia: Podaj adres serwera modelu albo wybierz „Ten komputer” (aplikacja GramaDrama › Usługi).',
                 ('settings',)),
                ({'llm_url': self.stub.url}, 'Ustawienia: Wybierz model (aplikacja GramaDrama › Usługi).', ('settings',)),
                ({'llm_url': self.stub.url, 'model': 'm', 'llm_api': 'klingon'},
                 'Ustawienia: Podaj protokół serwera modelu: openai albo ollama.', ('settings',))):
            with self.subTest(settings=settings):
                self.write_settings(settings)
                calls = self.run_real('tekst')
                self.assertEqual(calls, [('error', 7, message)])
                self.assertIn(calls.whys[0], whys)
                self.assertEqual(self.stub.requests, [])

    def test_bad_settings_are_reported(self):
        self.write_settings({'llm_url': 'ftp://x', 'languagetool_url': self.stub.url, 'model': 'm'})
        calls = self.run_real('tekst')
        self.assertEqual(calls[0][0], 'error')
        self.assertTrue(calls[0][2].startswith('Ustawienia: '), calls)
        self.write_settings('{not json')
        calls = self.run_real('tekst')
        self.assertTrue(calls[0][2].startswith('Nie udało się odczytać ustawień'), calls)
        self.assertEqual(self.stub.requests, [])

    def test_cancel_mid_stream_kills_the_child(self):
        self.stub.chunks = stream('bardzo długa odpowiedź ' * 20, pieces=40)
        self.stub.chunk_delay = 0.2  # 8 s in total
        sink, loop = Sink(), Loop()
        engine = ProcessBackend(sink, real=True)
        worker = engine.start(3, 'tekst', 'improve', loop.post)
        deadline = time.monotonic() + 5
        while not self.stub.chats() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.stub.chats(), 'the child did not reach the stub')
        time.sleep(0.5)  # streaming now
        started = time.monotonic()
        engine.cancel(3)
        worker.join(5)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNotNone(worker.child.returncode)
        loop.drain(self)
        self.assertNotIn('result', [c[0] for c in sink.calls])
        deadline = time.monotonic() + 5  # the stub notices the closed connection on its next write
        while not self.stub.disconnects and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.stub.disconnects, 1)


class PurityTests(unittest.TestCase):
    def test_engine_modules_import_neither_gi_nor_core(self):
        # Even after a whole correction: core (and its diff) is imported only by the child process.
        code = ('import sys\n'
                'from gramadrama.engine import applist, backend, context, fallback_ui, keys, plan, session\n'
                'calls = []\n'
                'class Sink:\n'
                '    def __getattr__(self, name):\n'
                '        return lambda *args: calls.append(name)\n'
                'posted = []\n'
                'backend.DebugBackend(Sink(), delay=0).start(1, "ala ma kota", "fix", posted.append).join(10)\n'
                '[fn() for fn in posted]\n'
                'assert calls[-1] == "backend_result", calls\n'
                'bad = sorted(m for m in sys.modules if m == "gi" or m.startswith("gi.") or m == "gramadrama.core")\n'
                'assert not bad, bad\n')
        env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
        result = subprocess.run([sys.executable, '-c', code], cwd=LINUX, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_long_lived_services_load_only_what_the_digest_covers(self):
        # The speech and live-check services live as long as the engine (engine/speech.py): a module
        # they hold that install_engine.engine_digest missed would go stale in them.
        sys.path.insert(0, str(LINUX))
        try:
            import install_engine
        finally:
            sys.path.remove(str(LINUX))
        code = ('import sys\n'
                'import gramadrama.speech, gramadrama.liveservice, gramadrama.spell, gramadrama.core\n'
                'import gramadrama.geometry\n'
                'print(" ".join(m for m in sys.modules if m.split(".")[0] == "gramadrama"))\n')
        env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')}
        result = subprocess.run([sys.executable, '-c', code], cwd=LINUX, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        covered = {'gramadrama', 'gramadrama.paths'} | {'gramadrama.' + name[:-3] for name in install_engine.SERVICE_MODULES}
        loaded = {m for m in result.stdout.split() if m.split('.')[:2] != ['gramadrama', 'engine']}  # engine/*.py: covered
        self.assertLessEqual(loaded, covered)

    def test_the_engine_process_loads_only_what_its_digest_covers(self):
        # install_engine.engine_digest restarts the engine when these change: the launcher,
        # gramadrama/__init__.py, paths.py (apps.json), i18n.py, catalogues and engine/*.py. Anything else it imported
        # (core above all) would go stale in a running engine.
        code = ('import sys\n'
                'import gramadrama.engine.main\n'
                'mods = sorted(m for m in sys.modules if m.split(".")[0] == "gramadrama" and m.split(".")[:2] != ["gramadrama", "engine"])\n'
                'assert mods == ["gramadrama", "gramadrama.i18n", "gramadrama.paths"], mods\n')
        env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')}
        result = subprocess.run([sys.executable, '-c', code], cwd=LINUX, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class SettingsWhyTests(unittest.TestCase):
    """backend._settings_why: on a new install (no model for Ollama on this computer) whether Ollama is missing, does
    not answer or answers (engine/problems.py: what the panel says to do first)."""

    def settings(self, **fields):
        settings = core.Settings()
        for name, value in fields.items():
            setattr(settings, name, value)
        return settings

    def why(self, settings, installed=False):
        with mock.patch('gramadrama.checks.ollama_installed', return_value=installed):
            return backend._settings_why(core, settings)

    def test_ollama_missing_or_not_answering(self):
        settings = self.settings(llm_local_url='http://127.0.0.1:%d' % closed_port())
        self.assertEqual(self.why(settings), 'no_ollama')
        self.assertEqual(self.why(settings, installed=True), 'ollama_down')

    def test_ollama_answering_has_no_model_chosen(self):
        with Stub() as stub:
            self.assertEqual(self.why(self.settings(llm_local_url=stub.url)), 'no_model')
            self.assertEqual([path for path, _ in stub.requests], ['/api/tags'])

    def test_anything_else_is_a_settings_problem(self):
        closed = 'http://127.0.0.1:%d' % closed_port()
        for fields in ({'llm_location': 'server', 'model': ''},  # a server without its address
                       {'llm_local_url': closed, 'model': 'gemma4:e4b'},
                       {'llm_local_url': closed, 'primary_language': 'xx'}):  # a check before the model's fails
            with self.subTest(fields=fields):
                self.assertEqual(self.why(self.settings(**fields)), 'settings')


if __name__ == '__main__':
    unittest.main()
