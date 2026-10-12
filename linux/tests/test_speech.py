"""The speech service (gramadrama/speech.py) in test mode: its protocol, the silence and length rules and
Bielik's cleanup against the stub server. No microphone, no Whisper, no GPU."""
import array
import io
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from unittest import mock
from gramadrama import core, speech
from gramadrama import dictation as D
from stub_server import Stub, closed_port, stream

LINUX = pathlib.Path(__file__).resolve().parent.parent


class Service:
    """The service as the engine starts it (engine/speech.py), talked to line by line."""

    def __init__(self, text, config, **extra_env):
        env = dict(os.environ, GRAMADRAMA_ENGINE_TEST='1', GRAMADRAMA_DEBUG_SPEECH=text, XDG_CONFIG_HOME=config, **extra_env)
        code = 'import sys; sys.path.insert(0, %r); from gramadrama.speech import main; sys.exit(main())' % str(LINUX)
        self.process = subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        env=env, text=True)

    def send(self, cmd, sid, **extra):
        self.process.stdin.write(json.dumps(dict({'cmd': cmd, 'id': sid}, **extra)) + '\n')
        self.process.stdin.flush()

    def until(self, kinds, timeout=15):
        events, deadline = [], time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            events.append(event)
            if event['ev'] in kinds:
                return events
        raise AssertionError('no %s in %s' % (kinds, events))

    def dictate(self, seconds=0.6, sid=1, **start):
        self.send('start', sid, **start)
        time.sleep(seconds)
        self.send('stop', sid)
        return self.until(('result', 'error', 'short', 'silence'))

    def close(self):
        self.process.stdin.close()
        self.process.wait(5)
        self.process.stdout.close()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)

    def service(self, text, **env):
        s = Service(text, self.config.name, **env)
        self.addCleanup(s.close)
        return s

    def settings(self, **fields):
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(fields))

    def test_a_dictation(self):
        s = self.service('yyy no więc spotkanie o 10.00.')
        events = s.dictate()
        kinds = [e['ev'] for e in events]
        self.assertEqual([k for k in kinds if k not in ('level', 'model')][-3:], ['listening', 'recognizing', 'result'])  # the model's news comes when it comes
        self.assertGreaterEqual(kinds.count('level'), 5)  # 20 a second
        levels = [e for e in events if e['ev'] == 'level']
        self.assertTrue(all(0 < e['level'] <= 1 for e in levels) and levels[-1]['s'] > levels[0]['s'])
        result = events[-1]
        self.assertEqual(result['text'], 'no więc spotkanie o 10:00.')  # the filler gone, the time fixed
        self.assertEqual(result['log']['cleanup'], 'off')
        self.assertGreaterEqual(result['log']['audio_ms'], 500)
        self.assertNotIn('spotkanie', json.dumps(result['log']))  # the log never holds text

    def test_short_silent_cancelled_and_failed(self):
        s = self.service('Coś.')
        self.assertEqual(s.dictate(0.1)[-1], {'ev': 'short', 'id': 1})
        s.send('start', 2)
        time.sleep(0.3)
        s.send('cancel', 2)
        s.send('stop', 2)  # after a cancel: nothing to stop
        self.assertEqual(s.dictate(0.6, sid=3)[-1]['text'], 'Coś.')  # and the next one works
        s = self.service('SILENCE')
        event = s.dictate()[-1]
        self.assertEqual((event['ev'], event['log']['outcome']), ('silence', 'silence'))
        s = self.service('ERROR:brak modelu')
        event = s.dictate()[-1]
        self.assertEqual((event['ev'], event['log']['outcome']), ('error', 'asr_error'))
        self.assertEqual(event['message'], 'Nie udało się rozpoznać mowy: brak modelu')

    def test_listening_comes_with_the_first_audio(self):
        """A USB microphone that PipeWire suspended wakes in up to 2 s: 'listening' (and the pill's "Słucham…") waits
        for the first audio, the log says how long it took, and a key let go before it gets a message, not silence."""
        s = self.service('Obudzony mikrofon.', GRAMADRAMA_DEBUG_MIC_MS='900')
        started = time.monotonic()
        s.send('start', 1)
        listening = s.until(('listening',))[-1]
        self.assertGreaterEqual(time.monotonic() - started, 0.85)
        self.assertGreaterEqual(listening['ms'], 850)
        time.sleep(0.5)
        s.send('stop', 1)
        result = s.until(('result',))[-1]
        self.assertEqual(result['text'], 'Obudzony mikrofon.')
        self.assertGreaterEqual(result['log']['mic_ms'], 850)
        s.send('start', 2)
        time.sleep(0.55)  # past MIN_SECONDS (shorter is a tap), before the microphone
        s.send('stop', 2)
        events = s.until(('result', 'error', 'short', 'silence'))
        self.assertNotIn('listening', [e['ev'] for e in events])
        self.assertEqual((events[-1]['ev'], events[-1]['log']['outcome'], events[-1]['log']['why']),
                         ('error', 'mic_error', 'not_ready'))
        self.assertEqual(events[-1]['message'], 'Mikrofon jeszcze się nie włączył — poczekaj na „Słucham…”.')

    def test_the_cleanup_warms_up_only_for_a_dictation(self):
        """The microphone opens at every key-down of the right Ctrl, shortcuts too: the model is asked only once ARM_MS
        has made it a dictation ('confirm')."""
        with Stub() as stub:
            self.settings(**dict(stub.settings(), dictation_cleanup='auto'))
            s = self.service('Coś.')
            s.send('start', 1)
            time.sleep(0.2)
            s.send('cancel', 1)  # a shortcut
            s.send('confirm', 1)  # too late: nothing records
            time.sleep(0.5)
            self.assertEqual(stub.chats(), [])
            s.send('start', 2)
            s.send('confirm', 2)
            deadline = time.monotonic() + 5
            while not stub.chats() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(stub.chats()[0]['options']['num_predict'], 1)  # the prewarm
            s.send('cancel', 2)

    def test_whisper_hallucinations_are_nothing(self):
        s = self.service('Dzięki za obejrzenie!')
        event = s.dictate()[-1]
        self.assertEqual((event['ev'], event['text'], event['log']['chars']), ('result', '', 0))

    def test_shift_dictates_in_the_other_language(self):
        # macOS 918d2c4: Shift with the key: English when the setting is Polish, Polish when English, English for auto.
        for setting, heard, other in (('pl', 'pl', 'en'), ('en', 'en', 'pl'), ('auto', 'auto', 'en')):
            with self.subTest(setting):
                self.settings(dictation_language=setting)
                s = self.service('Thanks.')
                events = s.dictate(alternate=True)  # Shift was down before the recording began
                self.assertEqual((events[-1]['log']['language'], events[-1]['log']['alternate']), (other, True))
                self.assertEqual({e['lang'] for e in events if e['ev'] == 'level'}, {D.LABELS[other]})
                events = s.dictate(sid=2)  # the next one is back to the setting
                self.assertEqual((events[-1]['log']['language'], events[-1]['log']['alternate']), (heard, False))
                self.assertEqual({e['lang'] for e in events if e['ev'] == 'level'}, {D.LABELS[heard]})

    def test_shift_during_the_recording_switches_it(self):
        s = self.service('Hello there.')
        s.send('start', 1)
        time.sleep(0.4)
        s.send('alternate', 1)
        time.sleep(0.4)
        s.send('stop', 1)
        events = s.until(('result',))
        labels = [e['lang'] for e in events if e['ev'] == 'level']
        self.assertEqual((labels[0], labels[-1]), ('PL', 'EN'))  # the pill follows
        self.assertEqual((events[-1]['log']['language'], events[-1]['log']['alternate']), ('en', True))
        s.send('alternate', 7)  # no recording of that id: nothing happens
        self.assertEqual(s.dictate(sid=2)[-1]['log']['language'], 'pl')

    def test_a_transcription_server_replaces_the_built_in_whisper(self):
        stub = Stub().__enter__()
        self.addCleanup(stub.__exit__)
        stub.transcript = 'Tekst z serwera mowy.'
        stub.require_key = 'sk-speech'
        self.settings(whisper_url=stub.url, whisper_key='sk-speech', whisper_model='m', dictation_language='pl')
        s = self.service('Ten tekst z mikrofonu testowego nie jest używany.')  # only the tone is recorded here
        events = s.dictate()
        kinds = [e['ev'] for e in events]
        self.assertEqual([k for k in kinds if k not in ('level', 'model')][-2:], ['recognizing', 'result'])
        result = events[-1]
        self.assertEqual((result['text'], result['log']['asr']), ('Tekst z serwera mowy.', 'server'))
        self.assertNotIn('serwera', json.dumps(result['log']))  # the log never holds text
        request, = stub.transcriptions()
        self.assertEqual((request['model'], request['language']), ('m', 'pl'))
        self.assertTrue(request['prompt'].startswith('Mówię po polsku, czasem wtrącam słowa:'))
        wave_data = request['file'][1]
        self.assertEqual(wave_data[:4], b'RIFF')  # the tone of the test recorder, as a WAV
        # the built-in model was never asked to load: the only news of a model is the server's
        self.assertEqual({e['device'] for e in events if e['ev'] == 'model'} - {'server'}, set())
        # an unreachable server is an error of recognition, said so
        self.settings(whisper_url='http://127.0.0.1:%d' % closed_port(), dictation_language='pl')
        event = s.dictate(sid=2)[-1]
        self.assertEqual((event['ev'], event['log']['outcome']), ('error', 'asr_error'))
        self.assertIn('Nie można połączyć się z serwerem mowy', event['message'])
        # and in test mode only a server on this machine is ever asked
        self.settings(whisper_url='http://192.0.2.1:9000', dictation_language='pl')
        event = s.dictate(sid=3)[-1]
        self.assertEqual(event['ev'], 'error')
        self.assertIn('Tryb testowy', event['message'])
        self.settings(dictation_language='pl')  # the address gone: the (test) built-in Whisper again
        self.assertEqual(s.dictate(sid=4)[-1]['log']['asr'], 'local')

    def test_settings_choose_the_language(self):
        self.settings(dictation_language='en')
        s = self.service('Thanks.')
        self.assertEqual(s.dictate()[-1]['log']['language'], 'en')
        self.settings(dictation_language='klingon')  # unusable: the default
        s = self.service('Dzięki.')
        self.assertEqual(s.dictate()[-1]['log']['language'], 'pl')

    def test_local_choice_keeps_a_server_address_without_sending_recording(self):
        with Stub() as stub:
            self.settings(whisper_backend='local', whisper_url=stub.url)
            result = self.service('Lokalne rozpoznawanie.').dictate()[-1]
            self.assertEqual((result['text'], result['log']['asr']), ('Lokalne rozpoznawanie.', 'local'))
            self.assertEqual(stub.requests, [])

    def test_selected_server_without_address_fails_before_recording(self):
        self.settings(whisper_backend='server')
        service = self.service('Nie wolno użyć modelu lokalnego.')
        service.send('start', 1)
        events = service.until(('error',))
        self.assertEqual(events[-1]['message'], 'Podaj adres serwera mowy (aplikacja GramaDrama › Usługi).')
        self.assertEqual({event['ev'] for event in events} - {'model', 'error'}, set())
        self.assertTrue(all(event.get('device') == 'server' for event in events if event['ev'] == 'model'))
        # A subsequent explicit local choice still works; the failed start did not leave a busy recording.
        self.settings(whisper_backend='local')
        self.assertEqual(service.dictate(sid=2)[-1]['log']['asr'], 'local')

    def test_one_language_ignores_shift_and_stale_polish_dictation_setting(self):
        self.settings(primary_language='en', secondary_language='', dictation_language='pl')
        service = self.service('This is a test.')
        event = service.dictate(alternate=True)[-1]
        self.assertEqual((event['log']['language'], event['log']['alternate']), ('en', False))
        self.assertEqual(event['text'], 'This is a test.')

    def test_german_pair_reloads_and_shift_uses_its_other_language(self):
        self.settings(primary_language='de', secondary_language='en', dictation_language='de')
        service = self.service('Das ist ein Test.')
        self.assertEqual(service.dictate()[-1]['log']['language'], 'de')
        self.assertEqual(service.dictate(sid=2, alternate=True)[-1]['log']['language'], 'en')
        self.settings(primary_language='de', secondary_language='', dictation_language='auto')
        event = service.dictate(sid=3, alternate=True)[-1]
        self.assertEqual((event['log']['language'], event['log']['alternate']), ('de', False))

    def test_german_dictation_preserves_um_before_the_time(self):
        self.settings(primary_language='de', secondary_language='', dictation_language='de', dictation_cleanup='auto')
        event = self.service('Ähm, wir treffen uns um zehn Uhr.').dictate()[-1]
        self.assertEqual(event['text'], 'Wir treffen uns um zehn Uhr.')


class SettingsTests(unittest.TestCase):
    def test_the_words_are_the_own_and_the_ones_added_to_the_dictionary(self):
        with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': config}):
            os.environ.pop('ENCHANT_CONFIG_DIR', None)
            pathlib.Path(config, 'gramadrama').mkdir()
            pathlib.Path(config, 'gramadrama', 'settings.json').write_text(json.dumps(
                {'dictation_vocabulary': 'GramaDrama, Bielik', 'secondary_language': 'en'}))
            pathlib.Path(config, 'enchant').mkdir()
            pathlib.Path(config, 'enchant', 'pl_PL.dic').write_text('Orion\nbielik\n')
            pathlib.Path(config, 'enchant', 'en_US.dic').write_text('Kubernetes\n')
            pathlib.Path(config, 'enchant', 'de_DE.dic').write_text('Haltestelle\n')  # German is no text language here
            _settings, _language, _cleanup, words = speech.read_settings()
        self.assertEqual(words, ['GramaDrama', 'Bielik', 'Orion', 'Kubernetes'])


class Transcript(speech.DebugWhisper):
    """Whisper as emit_result sees it: the text depends on the prompt it is given (a DebugWhisper, so that no
    numpy is needed)."""
    def __init__(self, with_prompt, without_prompt=None):
        super().__init__()
        self.answers = (with_prompt, with_prompt if without_prompt is None else without_prompt)
        self.prompts = []

    def transcribe(self, audio, language, prompt):
        self.prompts.append((language, prompt))
        return self.answers[0] if prompt else self.answers[1]


class EnglishRetryTests(unittest.TestCase):
    """macOS e795d38: forced Polish, a whole English answer: one more try without the prompt."""
    ENGLISH = 'I noticed a mistake in the dictation.'
    POLISH = 'Zauważyłem błąd w dyktowaniu.'

    def run_result(self, whisper, language='pl', vocabulary=('Bielik',)):
        events = []
        with mock.patch.object(speech.D, 'tidy', side_effect=lambda text, language=None: text):
            speech.emit_result(1, whisper, [0.0] * 8, core.Settings(), language, 'off', list(vocabulary), {},
                               send=lambda **event: events.append(event))
        self.assertEqual(len(events), 1, events)
        return events[0]

    def test_a_translated_answer_is_tried_again_without_the_prompt(self):
        whisper = Transcript(self.ENGLISH, self.POLISH)
        event = self.run_result(whisper)
        self.assertEqual((event['text'], event['log']['english_retry']), (self.POLISH, 'fixed'))
        self.assertEqual([prompt != '' for _, prompt in whisper.prompts], [True, False])
        self.assertEqual(whisper.prompts[0][0], 'pl')  # still forced Polish

    def test_a_retry_that_does_not_help_changes_nothing(self):
        whisper = Transcript(self.ENGLISH)
        event = self.run_result(whisper)
        self.assertEqual((event['text'], event['log']['english_retry']), (self.ENGLISH, 'kept'))
        self.assertEqual(len(whisper.prompts), 2)  # one retry, not more

    def test_no_retry_when_nothing_looks_translated(self):
        for name, text, language in (('Polish', self.POLISH, 'pl'), ('a short English answer', 'OK, thanks!', 'pl'),
                                     ('Polish with loanwords', 'Zrobiłem deploy na staging, sprawdź PR.', 'pl'),
                                     ('English was asked for', self.ENGLISH, 'en'), ('detected language', self.ENGLISH, 'auto')):
            with self.subTest(name):
                whisper = Transcript(text)
                event = self.run_result(whisper, language)
                self.assertNotIn('english_retry', event['log'])
                self.assertEqual(len(whisper.prompts), 1)

    def test_the_polish_prompt_is_a_sentence(self):
        whisper = Transcript(self.POLISH)
        self.run_result(whisper, vocabulary=('Bielik', 'PR'))
        self.assertTrue(whisper.prompts[0][1].startswith('Mówię po polsku, czasem wtrącam słowa: Bielik, PR, deploy'))
        self.assertTrue(whisper.prompts[0][1].endswith('Slack.'))


class RemoteWhisperTests(unittest.TestCase):
    """An OpenAI-style transcription server instead of the built-in Whisper (docs/open-source-plan.md, G3)."""

    def setUp(self):
        self.stub = Stub().__enter__()
        self.addCleanup(self.stub.__exit__)
        self.recording = array.array('h', [int(8000 * math.sin(i / 20)) for i in range(16000)])  # one second

    def whisper(self, **fields):
        return speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url, **fields))

    def test_the_recording_goes_as_a_wav_with_its_language_and_prompt(self):
        self.stub.transcript = ' Dzień dobry. '
        text = self.whisper(whisper_model='small', whisper_key='sk-k').transcribe(self.recording, 'pl', 'Mówię po polsku.')
        self.assertEqual(text, 'Dzień dobry.')
        request, = self.stub.transcriptions()
        self.assertEqual({k: request[k] for k in ('model', 'language', 'prompt', 'response_format', 'temperature')},
                         {'model': 'small', 'language': 'pl', 'prompt': 'Mówię po polsku.', 'response_format': 'json',
                          'temperature': '0'})
        filename, data = request['file']
        self.assertEqual(filename, 'audio.wav')
        with wave.open(io.BytesIO(data)) as wav:  # 16 kHz, mono, 16 bit: what Whisper wants
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()),
                             (16000, 1, 2, 16000))
            self.assertEqual(array.array('h', wav.readframes(16000)), self.recording)
        self.assertEqual(self.stub.requests[0][0], '/v1/audio/transcriptions')  # the base got its /v1
        self.assertEqual(self.stub.headers_seen[-1], ('/v1/audio/transcriptions', 'Bearer sk-k'))

    def test_the_fields_that_are_not_set_are_not_sent(self):
        self.whisper().transcribe(self.recording, None, '')  # auto language, no prompt, no model name
        request, = self.stub.transcriptions()
        self.assertEqual(request['model'], 'whisper-1')  # the usual placeholder
        self.assertNotIn('language', request)
        self.assertNotIn('prompt', request)
        self.assertIsNone(self.stub.headers_seen[-1][1])  # and no key

    def test_floats_are_accepted_too(self):  # the command line gives them
        self.whisper().transcribe([0.0, 0.5, -0.5, 1.5] * 100, 'pl', '')
        with wave.open(io.BytesIO(self.stub.transcriptions()[0]['file'][1])) as wav:
            self.assertEqual(array.array('h', wav.readframes(4)).tolist(), [0, 16383, -16383, 32767])  # clipped

    def test_errors_are_said_in_polish_and_never_hold_the_key(self):
        self.stub.require_key = 'sk-right'
        with self.assertRaises(RuntimeError) as caught:
            self.whisper(whisper_key='sk-wrong').transcribe(self.recording, 'pl', '')
        self.assertIn('klucz API', str(caught.exception))
        self.assertNotIn('sk-wrong', str(caught.exception))
        self.assertEqual(speech.failure_kind(caught.exception), 'http_401')  # the log's code
        self.stub.require_key, self.stub.stt_status = None, 404
        with self.assertRaises(RuntimeError) as caught:
            self.whisper().transcribe(self.recording, 'pl', '')
        self.assertIn('HTTP 404', str(caught.exception))
        self.assertEqual(speech.failure_kind(caught.exception), 'http_404')
        with self.assertRaises(RuntimeError) as caught:
            speech.RemoteWhisper(core.Settings(whisper_url='http://127.0.0.1:%d' % closed_port())).transcribe(self.recording, 'pl', '')
        self.assertIn('Nie można połączyć się z serwerem mowy', str(caught.exception))
        self.assertEqual(speech.failure_kind(caught.exception), 'refused')
        self.stub.stt_status, self.stub.stt_body = 200, b'{"unexpected": 1}'
        with self.assertRaises(RuntimeError) as caught:
            self.whisper().transcribe(self.recording, 'pl', '')
        self.assertEqual(speech.failure_kind(caught.exception), 'bad_reply')
        self.assertEqual(speech.failure_kind(ValueError('Mysle ze')), 'ValueError')  # never the message
        self.stub.stt_body = b'Zwykly tekst'  # a server that answers plain text
        self.assertEqual(self.whisper().transcribe(self.recording, 'pl', ''), 'Zwykly tekst')
        self.stub.stt_body = b'<html>captive portal</html>'
        self.assertEqual(self.whisper().transcribe(self.recording, 'pl', ''), '<html>captive portal</html>'.strip())

    def test_the_url_may_name_a_path(self):
        self.stub.transcript = 'Ok.'
        whisper = speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url + '/openai/v1/'))
        self.assertEqual(whisper.transcribe(self.recording, 'pl', ''), 'Ok.')
        self.assertEqual(self.stub.requests[0][0], '/openai/v1/audio/transcriptions')

    def test_native_whispercpp_uses_inference_without_an_openai_model(self):
        self.stub.transcript = 'Dzień dobry.'
        self.stub.require_key = 'sk-synthetic'
        text = self.whisper(whisper_api='whispercpp', whisper_model='ignored', whisper_key='sk-synthetic').transcribe(
            self.recording, 'pl', 'Mówię po polsku.')
        self.assertEqual(text, 'Dzień dobry.')
        request, = self.stub.transcriptions()
        self.assertEqual({key: value for key, value in request.items() if key != 'file'},
                         {'response_format': 'json', 'temperature': '0', 'temperature_inc': '0', 'best_of': '1',
                          'beam_size': '1', 'translate': 'false', 'language': 'pl', 'prompt': 'Mówię po polsku.'})
        self.assertEqual(self.stub.headers_seen[-1], ('/inference', 'Bearer sk-synthetic'))
        with wave.open(io.BytesIO(request['file'][1])) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))

    def test_native_auto_language_and_empty_prompt_override_server_defaults(self):
        whisper = speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url + '/speech/', whisper_api='whispercpp'))
        whisper.transcribe(self.recording, None, '')
        request, = self.stub.transcriptions()
        self.assertEqual(self.stub.requests[0][0], '/speech/inference')
        self.assertEqual((request['language'], request['prompt']), ('auto', ''))

    def test_bad_server_configuration_never_loads_a_local_model(self):
        service = speech.Service()
        with mock.patch.object(service, '_load_local') as local, mock.patch.object(speech, 'emit') as emit:
            for fields in ({'whisper_backend': 'server'}, {'whisper_backend': 'unknown'},
                           {'whisper_backend': 'server', 'whisper_url': self.stub.url, 'whisper_api': 'unknown'}):
                settings = core.Settings(**fields)
                with self.subTest(fields=fields), mock.patch.object(speech, 'read_settings', return_value=(settings, 'pl', 'off', ())):
                    service._load()
                    with self.assertRaises(RuntimeError):
                        service.whisper_for(settings)
                    with mock.patch.object(speech, 'Recorder') as recorder:
                        service.start(1)
                        recorder.assert_not_called()
                    self.assertEqual(emit.call_args.kwargs['ev'], 'error')
                    self.assertFalse(service.busy)
            local.assert_not_called()
        self.assertEqual(self.stub.requests, [])


class CleanupTests(unittest.TestCase):
    """Bielik's cleanup (speech.cleanup) against the stub, in the calling process."""

    def setUp(self):
        self.stub = Stub().__enter__()
        self.addCleanup(self.stub.__exit__)
        patcher = mock.patch.object(speech, 'TEST', True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = core.Settings(**self.stub.settings())
        self.cleaning = []

    def cleanup(self, raw, mode='auto', vocabulary=()):
        return speech.cleanup(raw, self.settings, mode, list(vocabulary), lambda: self.cleaning.append(True))

    def test_when_the_model_is_asked(self):
        self.assertEqual(self.cleanup('Zrobiłem deploy.', 'off'), (None, 'off', None))
        self.assertEqual(self.cleanup('Zrobiłem deploy.'), (None, 'not_needed', None))
        self.assertEqual(self.cleanup('yyy ' + 'słowo ' * 40), (None, 'too_long', None))
        self.assertEqual(self.stub.chats(), [])
        self.assertEqual(self.cleaning, [])

    def test_a_safe_cleanup_is_used(self):
        self.stub.chunks = stream('Spotkanie przesuwamy na czwartek o 10:00.')
        text, outcome, ms = self.cleanup('Spotkanie przesuwamy na środę, znaczy nie, na czwartek o dziesiątej.', 'auto', ['Orion'])
        self.assertEqual((text, outcome), ('Spotkanie przesuwamy na czwartek o 10:00.', 'ok'))
        self.assertEqual(self.cleaning, [True])
        request = self.stub.chats()[-1]
        self.assertTrue(request['messages'][0]['content'].endswith('exactly like this: Orion.'))
        self.assertEqual(request['options']['temperature'], 0)

    def test_an_answer_instead_of_the_transcript_is_rejected(self):
        self.stub.chunks = stream('Paryż.')
        self.assertEqual(self.cleanup('jaka jest stolica francji', 'always')[:2], (None, 'rejected'))

    def test_a_slow_or_absent_server(self):
        self.stub.chunks, self.stub.chunk_delay = stream('Yyy, dobra.', pieces=3), 0.5
        with mock.patch.object(speech.D, 'CLEANUP_SECONDS', 0.3), mock.patch.object(speech.D, 'cleanup_too_long', lambda raw: False):
            self.assertEqual(self.cleanup('yyy dobra', 'always')[:2], (None, 'timeout'))
        self.stub.chunk_delay = 0
        self.settings = core.Settings(**self.stub.settings(ollama='http://127.0.0.1:%d' % closed_port()))
        self.assertEqual(self.cleanup('yyy dobra', 'always')[:2], (None, 'error'))

    def test_test_mode_asks_no_real_server(self):
        self.settings = core.Settings(llm_url='http://192.0.2.1:11434', model='m')
        self.assertEqual(self.cleanup('yyy dobra', 'always')[:2], (None, 'error'))
        self.assertEqual(self.stub.requests, [])

    def test_prewarm(self):
        speech.prewarm(self.settings, ['Bielik'])
        request = self.stub.chats()[-1]
        self.assertEqual((request['stream'], request['options']['num_predict']), (False, 1))
        self.assertIn('<text>\n…\n</text>', request['messages'][-1]['content'])


class RecorderTests(unittest.TestCase):
    def test_the_microphone_command(self):
        command = speech.Recorder(lambda *a: None, lambda: None).command()
        self.assertEqual(command[:9], ['pw-record', '--raw', '--rate', '16000', '--channels', '1', '--format', 's16',
                                       '--latency'])
        self.assertEqual(command[-1], '-')  # the samples on stdout

    def test_levels_and_the_limit(self):
        import array
        levels, limits = [], []
        recorder = speech.Recorder(lambda rms, s: levels.append((rms, s)), lambda: limits.append(True))
        recorder.add(array.array('h', [1000, -1000] * 400))
        self.assertAlmostEqual(levels[-1][0], 1000 / 32768)
        self.assertEqual(levels[-1][1], 0.05)
        with mock.patch.object(speech.D, 'MAX_SECONDS', 0.1):
            recorder.add(array.array('h', [0] * 4000))
        self.assertEqual(len(recorder.samples), 1600)  # cut at the limit

    def test_the_first_audio_is_announced_once(self):
        import array
        ready = []
        recorder = speech.Recorder(lambda rms, s: None, lambda: None, ready.append)
        self.assertIsNone(recorder.ready_ms)
        recorder.opened -= 1.5  # pw-record started 1.5 s ago: a suspended webcam microphone
        recorder.add(array.array('h', [0] * 800))
        recorder.add(array.array('h', [0] * 800))
        self.assertEqual(len(ready), 1)
        self.assertGreaterEqual(ready[0], 1500)
        self.assertEqual(recorder.ready_ms, ready[0])


if __name__ == '__main__':
    unittest.main()
