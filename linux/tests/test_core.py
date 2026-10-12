import dataclasses
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from gramadrama.core import (Backend, BackendError, Cancelled, ScriptDrift, Settings, Unreachable, changes, clean, foreign_letters,
                             detect_language, diff_parts, http_error, messages, postprocess, translation_target, validate_text)
from gramadrama import core
from gramadrama.prompts import Prompts
from stub_server import Stub, closed_port, sse, stream

REPO = Path(__file__).resolve().parents[2]  # the macOS files shared with Linux live at the top


class TextTests(unittest.TestCase):
    def test_protected_content(self):
        original = '@Łukasz zobacz `main` 🙂 https://example.com/a_b :rocket:'
        for mode in ('fix', 'improve', 'translate'):
            self.assertEqual(clean(original, original, mode), original)
            for token in ['@Łukasz', '`main`', 'https://example.com/a_b', ':rocket:']:
                with self.subTest(mode=mode, token=token), self.assertRaises(BackendError):
                    clean(original.replace(token, ''), original, mode)
        # A lost emoji of the author comes back (at the end when its place is unknown), as on macOS.
        self.assertTrue(clean(original.replace('🙂', ''), original).endswith(':rocket: 🙂'))

    def test_wrappers_and_whitespace(self):
        self.assertEqual(clean('<text>\nCześć!\n</text>', '  czesc!\n'), '  Cześć!\n')
        self.assertEqual(clean('```text\nCześć!\n```', 'czesc!'), 'Cześć!')
        self.assertEqual(clean('```py\nx=1\n```', '```py\nx=1\n```'), '```py\nx=1\n```')
        with self.assertRaises(BackendError):
            clean('', 'test')

    def test_validation(self):
        for value in ['', ' ', 'x' * 6001, 'text\ufffc']:
            with self.subTest(value=value[:20]), self.assertRaises(BackendError):
                validate_text(value)
        validate_text('ą' * 6000)
        for url in ['ftp://test', 'server', 'http://', 'http://x:bad', 'http://user:password@host']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Settings(llm_url=url, model='m').validate()
        Settings(llm_url='http://[::1]:11434', model='m').validate()

    def test_settings_path_ignores_empty_and_relative_config_dirs(self):
        from pathlib import Path
        from unittest import mock
        default = Path.home() / '.config' / 'gramadrama' / 'settings.json'
        for value, expected in (('', default), ('relative/dir', default), ('.', default),
                                ('/abs/config', Path('/abs/config/gramadrama/settings.json'))):
            with self.subTest(value=value), mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': value}):
                self.assertEqual(Settings.path(), expected)
        with mock.patch.dict('os.environ', clear=True):
            self.assertEqual(Settings.path(), default)

    def test_diff_reconstructs_both_versions(self):
        original, result = 'Hej  swiecie!\n🙂', 'Hej, świecie!\n🙂'
        parts = list(diff_parts(original, result))
        self.assertEqual(''.join(s for kind, s in parts if kind != 'added'), original)
        self.assertEqual(''.join(s for kind, s in parts if kind != 'removed'), result)

    def test_repairs_instead_of_refusing(self):
        # Each of these was refused before, every time (temperature 0).
        cases = [('zobacz https://example.com/a', 'Zobacz https://example.com/a.', 'Zobacz https://example.com/a.'),
                 ('dzieki @Łukasz', 'Dzięki @Łukasz.', 'Dzięki @Łukasz.'),
                 ('@jan napisz', 'jan, napisz', '@jan, napisz'),
                 ('standup o 9:30: :coffee:', 'Standup o 9:30 :coffee:', 'Standup o 9:30 :coffee:'),
                 ('super 🙂', 'Super 😊', 'Super 🙂'),
                 ('super 🙂', 'Super ;)', 'Super 🙂'),
                 ('mr: https://gitlab.com/x/-/merge_requests/1', 'MR: https://gitlab.com/x/-/merge\\_requests/1',
                  'MR: https://gitlab.com/x/-/merge_requests/1'),
                 ('link: https://gitlab.com/x', 'Link: <https://gitlab.com/x>', 'Link: https://gitlab.com/x')]
        for original, output, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(clean(output, original), expected)

    def test_emoji_the_model_adds(self):
        # Outside Popraw the model may add emoji; the author's must stay in every mode.
        self.assertEqual(clean('Super, dzięki! 🎉', 'super dzieki', 'casual'), 'Super, dzięki! 🎉')
        self.assertEqual(clean('Super 🙂 :tada:', 'super 🙂', 'improve'), 'Super 🙂 :tada:')
        for output in ('Super, dzięki! 🎉', 'Super, dzięki! :tada:'):
            with self.subTest(output=output), self.assertRaises(BackendError):
                clean(output, 'super dzieki', 'fix')
        # A foreign emoji in place of the author's is the author's again (restore_emoji), in any mode.
        self.assertEqual(clean('Great, thanks! 🎉', 'super dzieki 🙂', 'translate'), 'Great, thanks! 🙂')

    def test_emoji_outside_the_old_ranges_survive(self):
        # U+2B50 and U+23F0 used to vanish without a trace.
        self.assertEqual(clean('Dzięki!', 'dzieki ⭐'), 'Dzięki! ⭐')
        self.assertEqual(clean('Spotkanie o 10.', 'spotkanie o 10 ⏰'), 'Spotkanie o 10. ⏰')
        self.assertEqual(clean('Działa ❤️', 'dziala ❤'), 'Działa ❤')  # the author's form of the emoji

    def test_commentary_and_fences(self):
        # Commentary instead of a correction, a label before the text and code fences are dropped.
        self.assertEqual(clean('(The original text does not contain any errors.)', 'ok dzięki'), 'ok dzięki')
        self.assertEqual(clean('Oto poprawiony tekst:\nMyślę, że tak.', 'mysle ze tak.'), 'Myślę, że tak.')
        self.assertEqual(clean('Translation: Thanks!', 'Dzięki!', 'translate'), 'Thanks!')
        self.assertEqual(clean('```Popraw to```', 'popraw to'), 'Popraw to')
        self.assertEqual(clean('```Dzięki, jasne.```', 'dzieki, jasne.'), 'Dzięki, jasne.')
        with self.assertRaises(BackendError):
            clean(' \n', 'test')  # nothing at all is an error, unlike a remark

    def test_review_findings(self):
        # A reply of placeholders only changes nothing, like a reply of remarks only.
        self.assertEqual(clean('[Your name]', 'ok'), 'ok')
        self.assertEqual(postprocess('[Imię]\n[Nazwisko]', '  ok  '), '  ok  ')
        # A link or code span with an emoji is no emoji: the rewriting modes may not add one.
        for output in ('Zobacz https://x.pl/🙂', 'Zobacz `🙂`'):
            with self.subTest(output=output), self.assertRaises(BackendError):
                clean(output, 'zobacz', 'improve')
        # Line ends as macOS (ICU) sees them.
        self.assertEqual(postprocess('Dzięki\r\n[Your name]', 'dzieki'), 'Dzięki')
        self.assertEqual(postprocess('Dzięki\u2028[Your name]', 'dzieki'), 'Dzięki')
        # An ellipsis after a link.
        self.assertEqual(clean('Zobacz https://x.pl/a…', 'zobacz https://x.pl/a...'), 'Zobacz https://x.pl/a…')

    def test_restoring_emoji_stays_linear(self):
        import time
        started = time.monotonic()
        self.assertEqual(clean('x', '🙂' * 6000, 'improve').count('🙂'), 6000)
        self.assertEqual(postprocess(':) ' * 2000, '🙂' * 3000).count('🙂'), 3000)
        self.assertLess(time.monotonic() - started, 1.0)  # was 15 s: the text was split again per emoji

    def test_language_modes(self):
        self.assertIn('Never translate', messages('hello', 'fix')[0]['content'])
        self.assertIn('into Polish', messages('hello', 'translate', 'pl')[0]['content'])
        self.assertGreater(len(messages('hello', 'fix')), 2)


class SharedWithMacTests(unittest.TestCase):
    """Files macOS writes for Linux: the same post-processing and the same prompts on both."""

    def test_postprocess_cases(self):
        cases = json.loads((REPO / 'tests' / 'postprocess_cases.json').read_text())['cases']
        self.assertGreaterEqual(len(cases), 67)
        for case in cases:
            with self.subTest(case['name']):
                self.assertEqual(postprocess(case['output'], case['original'], case['strict']), case['expected'])
                # The Linux check after the repairs refuses none of them (Luźniej and Tłumacz may add emoji).
                mode = 'translate' if case['strict'] else 'casual'
                self.assertEqual(clean(case['output'], case['original'], mode), case['expected'])

    def test_result_check_cases(self):
        """tests/result_check_cases.json: the check after the repairs, the same file as macOS PostProcess.checked."""
        cases = json.loads((REPO / 'tests' / 'result_check_cases.json').read_text())['cases']
        self.assertGreaterEqual(len(cases), 30)
        for case in cases:
            with self.subTest(case['name']):
                if case['refusal'] is None:
                    self.assertEqual(clean(case['output'], case['original'], case['mode']), case['result'])
                else:
                    with self.assertRaises(BackendError) as caught:
                        clean(case['output'], case['original'], case['mode'])
                    message = {'empty': 'Model zwrócił pustą odpowiedź. Spróbuj ponownie.',
                               'protected': 'Model zmienił link, wzmiankę, kod lub emoji. Wynik odrzucono; spróbuj ponownie.'}
                    self.assertEqual(str(caught.exception), message[case['refusal']])

    def test_prompts_match_macos(self):
        entries = json.loads((REPO / 'bench' / 'prompts.golden.json').read_text())['entries']
        self.assertEqual(len(entries), 21)
        for entry in entries:
            target = {'english': 'en', 'polish': 'pl', None: 'en'}[entry['target']]
            with self.subTest(mode=entry['mode'], input=entry['input'], target=entry['target']):
                # the defaults, never the files of whoever runs the tests (prompts.py)
                self.assertEqual(messages(entry['input'], entry['mode'], target, Prompts.defaults()), entry['messages'])


class ChangeTests(unittest.TestCase):
    """The change list and its reasons, identical to macOS (Diff.changes; SelfTest.swift cases)."""

    def reasons(self, a, b, hints=()):
        return ' | '.join('%s→%s: %s' % (c['from'], c['to'], c['reason']) for c in changes(list(diff_parts(a, b)), a, hints))

    def test_macos_self_test_cases(self):
        cases = [('ok,dzięki', 'ok, dzięki', '→: Odstęp'),
                 ('nikogo niebyło', 'nikogo nie było', 'niebyło→nie było: Pisownia łączna / rozdzielna'),
                 ('w piatek', 'w piątek', 'piatek→piątek: Polskie znaki'),
                 ('maila ktory', 'maila, który', 'ktory→, który: Polskie znaki i interpunkcja'),
                 ('wogole nie', 'w ogóle nie', 'wogole→w ogóle: Polskie znaki i pisownia łączna / rozdzielna'),
                 ('wiem że', 'wiem, że', '→,: Brakujący znak interpunkcyjny')]
        for a, b, want in cases:
            with self.subTest(a=a):
                self.assertEqual(self.reasons(a, b), want)

    def test_other_categories(self):
        self.assertEqual(self.reasons('To jest ok.', 'To jest ok'), '.→: Zbędny znak interpunkcyjny')
        self.assertEqual(self.reasons('w poniedziałek Jan', 'w Poniedziałek Jan'), 'poniedziałek→Poniedziałek: Wielka / mała litera')
        self.assertEqual(self.reasons('Mam kota', 'Mam kotw'), 'kota→kotw: Literówka')
        self.assertEqual(self.reasons('Poszedłem do sklep', 'Poszedłem do sklepu wczoraj rano'),
                         'sklep→sklepu wczoraj rano: Gramatyka / styl')

    def test_languagetool_message_is_used_for_generic_changes(self):
        text = 'Ala 😀 poszedł do sklep'
        start = len(text[:text.index('poszedł')].encode('utf-16-le')) // 2  # the emoji is 2 code units
        hint = {'offset': start, 'length': 7, 'message': 'Niezgodność rodzaju', 'rule': {'id': 'GRAM_RULE'}}
        spelling = dict(hint, message='Możliwy błąd pisowni', rule={'id': 'MORFOLOGIK_RULE_PL_PL'})
        self.assertEqual(self.reasons(text, 'Ala 😀 poszła do sklepu', [hint]).split(' | ')[0],
                         'poszedł→poszła: Niezgodność rodzaju')
        self.assertEqual(self.reasons(text, 'Ala 😀 poszła do sklepu', [spelling]).split(' | ')[0],
                         'poszedł→poszła: Gramatyka / styl')  # spelling rules are too generic

    def test_dense_rewrites_stay_fast_and_exact(self):
        import time
        sentences = ['Ala ma kota numer %d i psa w lesie nad rzeką.' % i for i in range(120)]
        a = ' '.join(sentences)[:6000]
        b = ' '.join(' '.join(w if j % 2 else w.upper() for j, w in enumerate(s.split(' '))) for s in sentences)[:6000]
        started = time.monotonic()
        parts = list(diff_parts(a, b))
        self.assertLess(time.monotonic() - started, 1.0)  # was ~13 s token by token
        self.assertEqual(''.join(t for k, t in parts if k != 'added'), a)
        self.assertEqual(''.join(t for k, t in parts if k != 'removed'), b)
        self.assertGreater(len(parts), 400)  # still word by word, sentence after sentence

    def test_apostrophes_stay_inside_words(self):
        self.assertEqual(list(diff_parts("It's fine", "It’s fine")), [('removed', "It's"), ('added', 'It’s'), ('same', ' fine')])


class LanguageTests(unittest.TestCase):
    def test_detection(self):
        cases = {
            'myslalem ze to juz wyslane ale okazało sie ze nie': 'pl',
            '@Kasia sprawdzisz dzis ten task w jirze? :pray:': 'pl',
            'Wysyłam feedback do klienta o 14, potem mam call z zespołem.': 'pl',
            'Deploy na staging jutro, PR jest ready': 'pl',
            'spoko, dzięki': 'pl',
            'Your right, we should of tested it more before the release.': 'en',
            'thanks!': 'en',
            'The details are here: https://docs.example.com/projekt_x/specyfikacja': 'en',
            'Hi Łukasz, can you check the deploy on staging?': 'en',
            'Could you please send me the logs when you have a moment? cc @Tom': 'en',
            'Mam call o 14, a potem review PR-a, sorry for the delay': 'mixed',
            'Good to go': 'en',
            'Ten minutes, I am on a call': 'en',
            "Let's go with the second option": 'en',
            'Go ahead and merge it': 'en',
            'CI is red again': 'en',
            'The pod keeps restarting': 'en',
            'ok': 'mixed',
            '': 'mixed',
        }
        for text, language in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), language)

    def test_translation_target_of_short_texts(self):
        # No function words to go by. English goes to Polish; Polish with
        # tech loanwords (rollback, feedback, staging) stays a Polish text.
        for text, target in (('Deploy failed, rollback done.', 'pl'), ('Nice work', 'pl'), ('Happy to help', 'pl'),
                             ('Each works', 'pl'), ('Deploy each branch', 'pl'), ('Rollback zrobiony', 'en'),
                             ('Feedback wyslany', 'en'), ('Staging lezy', 'en'), ('Merge zrobiony', 'en')):
            with self.subTest(text=text):
                self.assertEqual(translation_target(text), target)

    def test_translation_target(self):
        self.assertEqual(translation_target('Dzięki, sprawdzę to jutro.'), 'en')
        self.assertEqual(translation_target('Thanks, I will check it tomorrow.'), 'pl')
        self.assertEqual(translation_target('Mam call, sorry for the delay'), 'pl')  # mixed: majority English
        self.assertEqual(translation_target('Mam call, jutro sorry'), 'en')  # mixed: majority Polish
        self.assertEqual(translation_target('ok'), 'en')  # no evidence: English, as on macOS

    def test_no_backtracking_on_hostile_text(self):
        import time
        started = time.monotonic()
        detect_language('a@' * 3000)
        self.assertLess(time.monotonic() - started, 0.5)


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunks = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                cls.request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.end_headers()
                for chunk in cls.chunks:
                    self.wfile.write((json.dumps(chunk) + '\n').encode())
            def log_message(self, *_):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def backend(self):
        return Backend(Settings(llm_url=f'http://127.0.0.1:{self.server.server_port}', llm_api='ollama', model='m'))

    def test_complete_stream(self):
        type(self).chunks = [{'message': {'content': 'Cze'}}, {'message': {'content': 'ść!'}, 'done': True}]
        partials = []
        self.assertEqual(self.backend().correct('czesc!', partial=partials.append), 'Cześć!')
        self.assertEqual(partials, ['Cze', 'Cześć!'])
        self.assertTrue(type(self).request['stream'])

    def test_rejects_incomplete_error_and_truncated_streams(self):
        for chunks in [[{'message': {'content': 'partial'}}], [{'error': 'out of memory'}],
                       [{'message': {'content': 'cut'}, 'done': True, 'done_reason': 'length'}], [{'done': True}]]:
            type(self).chunks = chunks
            with self.subTest(chunks=chunks), self.assertRaises(BackendError):
                self.backend().correct('test')

    def test_unreachable_server_is_its_own_error(self):
        backend = Backend(Settings(llm_url='http://127.0.0.1:%d' % closed_port(), llm_api='ollama', model='m'))
        with self.assertRaises(Unreachable):
            backend.correct('test')

    def test_cancelled_before_request(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            self.backend().correct('test', cancel=cancel)


class ErrorKindTests(unittest.TestCase):
    """The engine log names why a request failed with a short code, never with the message: a model's error may
    quote the user's text."""

    def test_codes(self):
        import socket
        import urllib.error
        cases = (
            (BackendError('Błąd modelu: Mysle ze', kind='model_error'), 'model_error'),
            (Unreachable('x'), 'unreachable'),
            (Cancelled(), 'cancelled'),
            (BackendError('Mysle ze'), 'backend'),
            (urllib.error.URLError(socket.timeout()), 'timeout'),
            (urllib.error.URLError(ConnectionRefusedError()), 'refused'),
            (urllib.error.URLError(socket.gaierror()), 'dns'),
            (ConnectionResetError(), 'reset'),
            (ValueError('Mysle ze'), 'bad_reply'),
            (KeyError('Mysle ze'), 'KeyError'),
        )
        for exc, code in cases:
            with self.subTest(code):
                self.assertEqual(core.error_kind(exc), code)
        http = urllib.error.HTTPError('http://example.com', 503, 'x', {}, None)
        self.addCleanup(http.close)
        self.assertEqual(core.error_kind(http), 'http_503')

    def test_an_unreachable_server_says_how(self):
        port = closed_port()
        backend = Backend(Settings(llm_url='http://127.0.0.1:%d' % port, model='m'))
        with self.assertRaises(Unreachable) as caught:
            backend.correct('Mysle ze to dziala.')
        self.assertEqual(core.error_kind(caught.exception), 'refused')


class SettingsTests(unittest.TestCase):
    """No default servers, two protocols, the old ollama_url read as it was (docs/open-source-plan.md, G3)."""

    def test_a_new_install_knows_no_server(self):
        settings = Settings()
        self.assertEqual((settings.llm_url, settings.languagetool_url, settings.whisper_url, settings.model), ('', '', '', ''))
        self.assertEqual((settings.llm_api, settings.llm_key, settings.whisper_key), ('openai', '', ''))
        for what in ('example.test', 'Bielik', 'hf.co'):  # nothing of the author's own setup is a default
            self.assertNotIn(what, json.dumps(dataclasses.asdict(settings)))

    def test_dictation_selection_preserves_legacy_settings_and_saved_server(self):
        self.assertEqual((Settings().whisper_backend, Settings().whisper_api), ('auto', 'openai'))
        for fields, remote in (({}, False), ({'whisper_url': 'https://example.com'}, True),
                               ({'whisper_backend': 'local', 'whisper_url': 'https://example.com'}, False),
                               ({'whisper_backend': 'server'}, True)):
            with self.subTest(fields=fields):
                for settings in (Settings(**fields), Settings.from_dict(fields)):
                    self.assertEqual(settings.whisper_uses_server, remote)
                    self.assertEqual(settings.whisper_url, fields.get('whisper_url', ''))
        # Selecting the server before typing its address is saveable; dictation reports what is missing.
        Settings(whisper_backend='server').validate(llm=False)

    def test_validate_says_what_is_missing(self):
        for fields, message in (({'llm_location': 'server'}, 'Podaj adres serwera modelu albo wybierz „Ten komputer”'),
                                ({}, 'Wybierz model'),  # a new install: Ollama on this computer, the model still to choose
                                ({'llm_url': 'http://h:1'}, 'Wybierz model'),
                                ({'llm_url': 'http://h:1', 'model': 'm', 'llm_api': 'klingon'}, 'Podaj protokół')):
            with self.subTest(fields=fields), self.assertRaises(ValueError) as caught:
                Settings(**fields).validate()
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        Settings(llm_url='http://h:1', model='m').validate()
        Settings().validate(llm=False)  # LanguageTool alone, or nothing yet: nothing is missing
        Settings(languagetool_url='http://lt:8010').validate(llm=False)
        for field in ('llm_url', 'languagetool_url', 'whisper_url'):  # an address that is set must be one
            with self.subTest(field=field), self.assertRaises(ValueError):
                Settings(**{field: 'nie-adres'}).validate(llm=False)

    def test_the_old_ollama_url_means_ollamas_own_api(self):
        old = Settings.from_dict({'ollama_url': 'http://h:11434', 'languagetool_url': 'http://h:8010', 'model': 'm'})
        self.assertEqual((old.llm_url, old.llm_api), ('http://h:11434', 'ollama'))
        new = Settings.from_dict({'ollama_url': 'http://old:1', 'llm_url': 'http://h:2', 'llm_api': 'openai'})
        self.assertEqual((new.llm_url, new.llm_api), ('http://h:2', 'openai'))  # the new key wins
        self.assertEqual(Settings.from_dict({'ollama_url': 'http://h:1', 'llm_api': 'openai'}).llm_api, 'openai')

    def test_the_file_with_keys_is_private(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': tmp}):
            Settings(llm_url='http://h:1', model='m', llm_key='sk-secret').save()
            path = Settings.path()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())['llm_key'], 'sk-secret')
            self.assertEqual(os.listdir(path.parent), ['settings.json'])  # no temp file left
            Settings(model='only a model, no server yet').save()  # nothing is required to save: the server may come later

    def test_lan_hosts_skip_a_proxy_and_the_internet_does_not(self):
        from gramadrama.core import lan_host
        for host in ('127.0.0.1', 'localhost', '::1', '192.168.1.5', '10.0.0.2', '172.16.3.4', '169.254.1.1',  # privacy: allow[private-ip] synthetic LAN classification fixtures
                     'server.home.arpa', 'nas', 'box.lan', 'srv.home.arpa', 'a.internal'):  # privacy: allow[local-host] synthetic LAN classification fixtures
            with self.subTest(host=host):
                self.assertTrue(lan_host(host))
        for host in ('api.openai.com', 'api.groq.com', '8.8.8.8', 'example.org', '9.9.9.9'):
            with self.subTest(host=host):
                self.assertFalse(lan_host(host))


class ThinkingTests(unittest.TestCase):
    """Settings.llm_think: Ollama gets think=false unless the model is let to think. A model that thinks by default
    (Gemma 4, Qwen3) answered a short correction in seconds instead of a fraction of one, worse, and ran out of the
    answer's token limit on its thoughts (bench/lab, docs/models.md)."""

    def setUp(self):
        self.stub = Stub()
        self.stub.__enter__()
        self.addCleanup(self.stub.__exit__)
        self.stub.chunks = stream('Cześć!')

    def backend(self, model='gemma-4-12b', **fields):
        return Backend(Settings(llm_url=self.stub.url, llm_api='ollama', model=model, **fields))

    def test_off_is_the_default_and_goes_to_every_model(self):
        for model in ('gemma-4-12b', 'qwen3:8b', 'Bielik-11B'):
            with self.subTest(model=model):
                self.stub.requests.clear()
                self.assertEqual(self.backend(model).correct('czesc!'), 'Cześć!')
                self.assertIs(self.stub.chats()[0]['think'], False)

    def test_the_model_may_think_when_the_user_says_so(self):
        for model in ('gemma-4-12b', 'qwen3:8b'):  # qwen3 was switched off by its name before the setting
            with self.subTest(model=model):
                self.stub.requests.clear()
                self.backend(model, llm_think='model').correct('czesc!')
                self.assertNotIn('think', self.stub.chats()[0])

    def test_an_unknown_value_is_read_as_off(self):
        self.backend(llm_think='klingon').correct('czesc!')
        self.assertIs(self.stub.chats()[0]['think'], False)

    def test_the_warm_up_prompt_is_not_a_thinker_either(self):
        self.stub.chunks = [{'done': True}]
        self.backend().prewarm([{'role': 'user', 'content': 'x'}])
        self.assertIs(self.stub.chats()[0]['think'], False)

    def test_a_server_that_refuses_the_parameter_is_asked_again_without_it(self):
        self.stub.ollama_reject = {'think': '"m" does not support thinking'}
        backend = self.backend()
        self.assertEqual(backend.correct('czesc!'), 'Cześć!')
        self.assertEqual(['think' in request for request in self.stub.chats()], [True, False])
        self.assertEqual(backend.correct('czesc!'), 'Cześć!')  # not sent again by this Backend
        self.assertEqual(['think' in request for request in self.stub.chats()], [True, False, False])

    def test_other_refusals_are_still_errors(self):
        self.stub.ollama_reject = {'think': 'invalid request'}
        with self.assertRaises(BackendError):
            self.backend().correct('czesc!')
        self.stub.ollama_reject = {}
        self.stub.chat_status, self.stub.chat_error = 404, 'model "x" not found'
        with self.assertRaises(BackendError):
            self.backend().correct('czesc!')


class ScriptDriftTests(unittest.TestCase):
    """A glitch such as "pいつze" (Gemma 4 through the model server's Ollama, 4 times in 4 at temperature 0) is not a correction."""

    def test_which_letters_are_foreign(self):
        for result, original, expected in [
                ('pいつze trochę', 'piusze troche', ['い', 'つ']),
                ('Zażółć gęślą jaźń', 'zazolc gesla jazn', []),  # every Polish letter is Latin
                ('Привет, świat', 'Привет, swiat', []),  # a script the text already uses may stay
                ('Привет 你好', 'Привет', ['你', '好']),  # but not another one
                ('5 µs, 1º, ª, naïve café ß', '5 us 1 a naive cafe ss', []),  # signs below U+0250 are no script
                ('Zrób to 今すぐ', 'Zrob to', ['ぐ', 'す', '今'])]:
            with self.subTest(result=result):
                self.assertEqual(foreign_letters(result, original), expected)

    def test_clean_refuses_it(self):
        with self.assertRaises(ScriptDrift) as caught:
            clean('pいつze trochę dłuższy test', 'piusze troche dluzszy test')
        self.assertIn('いつ', str(caught.exception))
        self.assertIsInstance(caught.exception, BackendError)  # every caller that handles BackendError handles it
        self.assertEqual(clean('Привет, świat!', 'Привет, swiat!'), 'Привет, świat!')

    def setUp(self):
        self.stub = Stub()
        self.stub.__enter__()
        self.addCleanup(self.stub.__exit__)

    def backend(self):
        return Backend(Settings(llm_url=self.stub.url, llm_api='ollama', model='m'))

    def test_a_glitch_is_tried_again_with_a_little_randomness(self):
        self.stub.reply = lambda request: stream('pいつze trochę' if request['options']['temperature'] == 0 else 'piszę trochę')
        self.assertEqual(self.backend().correct('piusze troche'), 'piszę trochę')
        self.assertEqual([r['options']['temperature'] for r in self.stub.chats()], [0, 0.3])

    def test_a_mode_that_samples_keeps_its_own_temperature(self):
        self.stub.reply = lambda request: stream('pいつze trochę' if len(self.stub.chats()) == 1 else 'piszę trochę')
        self.assertEqual(self.backend().correct('piusze troche', 'improve'), 'piszę trochę')
        self.assertEqual([r['options']['temperature'] for r in self.stub.chats()], [0.4, 0.4])

    def test_two_glitches_are_an_error(self):
        self.stub.chunks = stream('pいつze trochę')
        with self.assertRaises(ScriptDrift):
            self.backend().correct('piusze troche')
        self.assertEqual(len(self.stub.chats()), 2)  # one more try, not a loop

    def test_a_clean_answer_costs_one_request(self):
        self.stub.chunks = stream('piszę trochę')
        self.assertEqual(self.backend().correct('piusze troche'), 'piszę trochę')
        self.assertEqual(len(self.stub.chats()), 1)


class OpenAIClientTests(unittest.TestCase):
    """The model server spoken to in the OpenAI way (/chat/completions, server-sent events), against the stub."""

    def setUp(self):
        self.stub = Stub()
        self.stub.__enter__()
        self.addCleanup(self.stub.__exit__)

    def backend(self, **fields):
        fields = dict(llm_url=self.stub.url, llm_api='openai', model='some-model', **fields)
        return Backend(Settings(**fields))

    def test_a_stream(self):
        self.stub.events = sse('Cześć!', pieces=2)
        partials = []
        self.assertEqual(self.backend().correct('czesc!', partial=partials.append), 'Cześć!')
        self.assertEqual(partials, ['Cze', 'Cześć!'])
        request = self.stub.completions()[0]
        self.assertEqual((request['model'], request['stream'], request['temperature']), ('some-model', True, 0))
        self.assertEqual(request['messages'][-1]['role'], 'user')
        self.assertIn('<text>\nczesc!\n</text>', request['messages'][-1]['content'])
        self.assertEqual(self.stub.requests[0][0], '/v1/chat/completions')  # the base got its /v1
        self.assertNotIn('options', request)  # num_ctx and keep_alive are Ollama's own

    def test_a_stream_as_llama_cpp_really_sends_it(self):
        """Seen on a real llama-server (2026-10-01): the first chunk has a role and a null content."""
        def chunk(delta, finish=None):
            return 'data: ' + json.dumps({'choices': [{'finish_reason': finish, 'index': 0, 'delta': delta}], 'created': 1,
                                          'id': 'chatcmpl-x', 'model': 'm', 'system_fingerprint': 'b11312-0c1e57098',
                                          'object': 'chat.completion.chunk'})
        self.stub.events = [chunk({'role': 'assistant', 'content': None}), chunk({'content': 'Cze'}), chunk({'content': 'ść!'}),
                            chunk({}, 'stop'), 'data: [DONE]']
        self.assertEqual(self.backend().correct('czesc!'), 'Cześć!')

    def test_a_context_that_is_too_small_says_what_to_do(self):
        """A server started with a small context refuses every correction: the prompt alone is about 1400 tokens."""
        for message in ('request (1370 tokens) exceeds the available context size (1024 tokens), try increasing it',  # llama.cpp
                        "This model's maximum context length is 4096 tokens. However, your messages resulted in 5000 tokens.",
                        'Trying to keep the first 5000 tokens when context overflows. However, the model is loaded with '
                        'context length of only 4096 tokens'):
            text = http_error(400, json.dumps({'error': {'message': message}}).encode())
            self.assertIn(message[:30], text)  # the server's own words stay
            self.assertIn('co najmniej 4096 tokenów (zalecane 8192)', text)
            self.assertIn('llama-server: -c 8192', text)
        other = http_error(400, json.dumps({'error': {'message': 'Unsupported parameter: x'}}).encode())
        self.assertNotIn('kontekst', other)
        self.stub.reject = {'stream': 'request (1370 tokens) exceeds the available context size (1024 tokens), try increasing it'}
        with self.assertRaisesRegex(BackendError, 'za mały kontekst'):
            self.backend().correct('czesc!')

    def test_the_end_of_a_stream_without_done_or_with_odd_chunks(self):
        for name, events, ok in (('no [DONE], a finish reason', sse('Tekst.', done=False), True),
                                 ('[DONE] and no finish reason', sse('Tekst.', finish=None), True),
                                 ('neither: the connection just ended', sse('Tekst.', finish=None, done=False), False),
                                 ('cut by the length limit', sse('Tekst.', finish='length'), False),
                                 ('an error inside the stream', ['data: ' + json.dumps({'error': {'message': 'out of memory'}})], False),
                                 ('not JSON', ['data: {oops'], False)):
            with self.subTest(name):
                self.stub.events = events
                if ok:
                    self.assertEqual(self.backend().correct('tekst'), 'Tekst.')
                else:
                    with self.assertRaises(BackendError):
                        self.backend().correct('tekst')
        # comments, event names and blank lines are not data; a chunk with no content (a role, a usage) adds nothing
        self.stub.events = [': keep-alive', 'event: ping', '', 'data: ' + json.dumps({'choices': [{'delta': {'role': 'assistant'}}]}),
                            *sse('Gotowe.'), 'data: ' + json.dumps({'choices': [], 'usage': {'total_tokens': 5}})]
        self.assertEqual(self.backend().correct('gotowe'), 'Gotowe.')

    def test_the_key_goes_to_the_model_server_and_nowhere_else(self):
        self.stub.events = sse('Dobrze.')
        self.stub.require_key = 'sk-secret'
        backend = self.backend(llm_key='sk-secret', languagetool_url=self.stub.url)
        self.assertEqual(backend.correct('dobrze'), 'Dobrze.')
        backend.check('Zły tekst')
        keys = {path: auth for path, auth in self.stub.headers_seen}
        self.assertEqual(keys['/v1/chat/completions'], 'Bearer sk-secret')
        self.assertIsNone(keys['/v2/check'])  # never to LanguageTool
        with self.assertRaises(BackendError) as caught:
            self.backend(llm_key='wrong').correct('dobrze')
        self.assertIn('klucz API', str(caught.exception))
        self.assertIn('HTTP 401', str(caught.exception))
        self.assertNotIn('wrong', str(caught.exception))  # the key is never in a message
        with self.assertRaises(BackendError):
            self.backend().correct('dobrze')  # none at all

    def test_a_server_that_refuses_a_parameter_gets_it_adapted(self):
        self.stub.events = sse('Dobrze.')
        self.stub.reject = {'max_tokens': "Unsupported parameter: 'max_tokens' is not supported with this model. "
                                          "Use 'max_completion_tokens' instead."}
        self.assertEqual(self.backend().correct('dobrze'), 'Dobrze.')
        first, second = self.stub.completions()
        self.assertIn('max_tokens', first)
        self.assertEqual((second.get('max_tokens'), second['max_completion_tokens']), (None, first['max_tokens']))
        self.stub.requests.clear()
        self.stub.reject = {'temperature': "Unsupported value: 'temperature' does not support 0 with this model. "
                                           "Only the default (1) value is supported."}
        self.assertEqual(self.backend().correct('dobrze'), 'Dobrze.')
        self.assertNotIn('temperature', self.stub.completions()[-1])
        self.stub.requests.clear()
        self.stub.reject = {'max_tokens': 'Something else is wrong with max_tokens'}  # not a hint: shown, not looped
        with self.assertRaises(BackendError):
            self.backend().correct('dobrze')
        self.assertEqual(len(self.stub.completions()), 1)

    def test_the_base_url(self):
        for typed, base in (('http://h:8080', 'http://h:8080/v1'), ('http://h:8080/', 'http://h:8080/v1'),
                            ('http://h:8080/v1', 'http://h:8080/v1'), ('https://api.groq.com/openai/v1/', 'https://api.groq.com/openai/v1'),
                            ('https://openrouter.ai/api/v1', 'https://openrouter.ai/api/v1')):
            with self.subTest(typed):
                self.assertEqual(Backend(Settings(llm_url=typed, model='m')).llm_base(), base)
        self.assertEqual(Backend(Settings(llm_url='http://h:11434', llm_api='ollama', model='m')).llm_base(), 'http://h:11434')
        self.stub.events = sse('Dobrze.')
        self.assertEqual(Backend(Settings(llm_url=self.stub.url + '/openai/v1', model='m')).correct('dobrze'), 'Dobrze.')
        self.assertEqual(self.stub.requests[-1][0], '/openai/v1/chat/completions')

    def test_nothing_is_warmed_up_on_a_server_that_does_not_load_models(self):
        backend = self.backend()
        backend.warm_up()
        backend.prewarm([{'role': 'user', 'content': 'x'}])
        self.assertEqual(self.stub.requests, [])  # a cloud service would bill for a "warm-up"
        ollama = Backend(Settings(llm_url=self.stub.url, llm_api='ollama', model='m'))
        ollama.warm_up()
        self.assertEqual([path for path, _ in self.stub.requests], ['/api/generate'])

    def test_a_model_server_that_is_not_set_is_said_so(self):
        with self.assertRaises(ValueError) as caught:
            Backend(Settings(llm_location='server', model='m'))
        self.assertIn('adres serwera modelu', str(caught.exception))
        lt_only = Backend(Settings(languagetool_url=self.stub.url), llm=False)
        self.assertEqual(lt_only.check('Zły tekst'), [])
        with self.assertRaises(ValueError):
            lt_only.chat([], 0, 'x')  # no model server: a clear refusal, not a request to nowhere
        no_lt = Backend(Settings(llm_url=self.stub.url, model='m', languagetool_location='server'))  # a server without an address
        with self.assertRaises(BackendError) as caught:
            no_lt.check('Zły tekst')
        self.assertIn('LanguageTool', str(caught.exception))
        self.assertEqual([path for path, _ in self.stub.requests], ['/v2/check'])  # and nothing for the model

    def test_the_proxy_of_the_environment_is_for_the_internet_only(self):
        backend = self.backend()
        for url, lan in (('http://127.0.0.1:1/v1', True), ('http://server.home.arpa:1/v1', True), ('https://api.openai.com/v1', False)):  # privacy: allow[local-host] synthetic LAN timeout fixture
            with self.subTest(url), mock.patch.object(backend, 'lan_opener') as lan_opener, \
                    mock.patch.object(backend, 'net_opener') as net_opener:
                backend.request(url + '/models')
                self.assertEqual((lan_opener.open.called, net_opener.open.called), (lan, not lan))


class LanguageProfileTests(unittest.TestCase):
    def test_existing_settings_keep_polish_and_english_and_invalid_pairs_are_rejected(self):
        self.assertEqual(Settings.from_dict({}).languages, ('pl', 'en'))
        for primary, secondary in (('en', ''), ('de', 'en'), ('en', 'de'), ('pl', 'de')):
            settings = Settings(primary_language=primary, secondary_language=secondary)
            settings.validate(llm=False)
            self.assertEqual(settings.languages, tuple(code for code in (primary, secondary) if code))
        for primary, secondary in (('xx', ''), ('de', 'de'), ('en', 'xx')):
            with self.assertRaises(ValueError):
                Settings(primary_language=primary, secondary_language=secondary).validate(llm=False)

    def test_german_english_and_mixed_text_are_recognised_only_within_the_active_pair(self):
        for text, expected in (('Ich schicke den Bericht morgen.', 'de'),
                               ('Kannst du bitte die Datei prüfen?', 'de'),
                               ('Thanks for the message. I will check it tomorrow.', 'en'),
                               ('Das ist gut. I will check it tomorrow.', 'mixed')):
            with self.subTest(text=text):
                self.assertEqual(detect_language(text, ('de', 'en')), expected)
        self.assertEqual(detect_language('Hallo Welt', ('en',)), 'en')
        self.assertEqual(detect_language('Ich schicke den Bericht morgen.', ('pl', 'de')), 'de')
        self.assertEqual(detect_language('Ich bin in Berlin.', ('de', 'en')), 'de')
        self.assertEqual(detect_language('Ich nie ma w domu.', ('pl', 'de')), 'pl')

    def test_translation_directions_follow_the_configured_pair(self):
        self.assertEqual(translation_target('Ich schicke den Bericht morgen.', ('de', 'en')), 'en')
        self.assertEqual(translation_target('I will send it tomorrow.', ('de', 'en')), 'de')
        self.assertEqual(translation_target('Dziękuję za wiadomość.', ('pl', 'de')), 'de')
        self.assertEqual(translation_target('Danke für die Nachricht.', ('pl', 'de')), 'pl')

    def test_single_language_translation_never_calls_a_server(self):
        backend = Backend(Settings(llm_url='http://127.0.0.1:11434', model='test-model',
                                   primary_language='en', secondary_language=''))
        with mock.patch.object(backend, 'chat') as chat:
            with self.assertRaisesRegex(BackendError, 'drugiego języka'):
                backend.correct('Hello', 'translate')
        chat.assert_not_called()

    def test_languagetool_receives_only_selected_languages_and_the_primary_mother_tongue(self):
        import io
        for pair, text, expected in ((('en', ''), 'This are a test.', 'en-US'),
                                     (('de', 'en'), 'Das ist eine gute Idee.', 'de-DE'),
                                     (('de', 'en'), 'Das ist gut. I will check it tomorrow.', 'auto')):
            backend = Backend(Settings(languagetool_url='http://127.0.0.1:8010',
                                       primary_language=pair[0], secondary_language=pair[1]), llm=False)
            with mock.patch.object(backend, 'request', return_value=io.StringIO('{"matches": []}')) as request:
                backend.check(text)
            fields = request.call_args.args[1]
            self.assertEqual(fields['language'], expected)
            self.assertEqual(fields['motherTongue'], 'en-US' if pair[0] == 'en' else 'de-DE')
            if expected == 'auto':
                self.assertEqual(fields['preferredVariants'], 'de-DE,en-US')
            else:
                self.assertNotIn('preferredVariants', fields)
            self.assertNotIn('pl-PL', str(fields))



class DisplayNameTests(unittest.TestCase):
    """The model's name in sentences, as the Mac's Settings.displayName (its SelfTest's cases)."""

    def test_the_macs_cases(self):
        for model, expected in (('hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M', 'Bielik-11B-v2.3'),
                                ('hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest', 'Gemma-4-12B'),
                                ('qwen3:8b', 'Qwen3:8b'), ('gemma3:12b-it-qat', 'Gemma3:12b'), ('llama3.1:Q4_K_M', 'Llama3.1'),
                                ('gemma4-e4b-gramadrama:latest', 'Gemma4-E4B'), ('mistral-7b-v0.3:latest', 'Mistral-7b-v0.3')):
            with self.subTest(model=model):
                self.assertEqual(core.display_name(model), expected)

if __name__ == '__main__':
    unittest.main()
