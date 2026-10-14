import dataclasses
import datetime
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gramadrama import checks as C, core  # noqa: E402
from gramadrama.checks import BUSY, ERROR, INFO, OK, WARNING, Check  # noqa: E402
from stub_server import Stub, closed_port, stream  # noqa: E402

BIELIK = 'hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M'


def at(hour, minute, days_ago=0):
    moment = datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=days_ago),
                                       datetime.time(hour, minute))
    return moment.timestamp()


class ShortcutTests(unittest.TestCase):
    def test_accelerators_are_parsed_like_gnome_writes_them(self):
        hotkey = (frozenset({'ctrl', 'alt'}), 'g')
        for accel in ('<Control><Alt>g', '<Alt><Control>g', '<Primary><Alt>G', '<ctrl><mod1>g', ' <Control> <Alt>g '):
            self.assertEqual(C.parse_accel(accel), hotkey, accel)
        self.assertEqual(C.parse_accel('<Super>space'), (frozenset({'super'}), 'space'))
        self.assertEqual(C.parse_accel('F5'), (frozenset(), 'f5'))
        for bad in ('', '<Control>', '<Control', '   '):
            self.assertIsNone(C.parse_accel(bad), bad)

    def test_only_exactly_ctrl_alt_g_conflicts(self):
        bindings = [('show-desktop', '<Primary><Alt>d'), ('switch-panels', '<Control><Alt>Tab'),
                    ('Mój skrót', '<Control><Alt>g'), ('shifted', '<Control><Alt><Shift>g'), ('off', '')]
        self.assertEqual(C.hotkey_conflicts(bindings), ['Mój skrót'])

    def test_hotkey_row(self):
        self.assertEqual(C.hotkey_check([]).state, OK)
        conflict = C.hotkey_check(['Mój skrót'])
        self.assertEqual((conflict.state, conflict.action), (ERROR, 'keyboard'))
        self.assertIn('„Mój skrót”', conflict.text)
        self.assertEqual(C.hotkey_check([], verified=True).text, 'Działa — sprawdzone w polu Wypróbuj')
        self.assertEqual(C.hotkey_check([], verified=False).state, ERROR)
        self.assertEqual(C.hotkey_check(['x'], verified=True).state, ERROR)  # a conflict wins


class SystemTests(unittest.TestCase):
    def test_input_source(self):
        sources = [('ibus', 'gramadrama'), ('xkb', 'pl')]
        self.assertEqual(C.source_check(sources, 'gramadrama').state, OK)
        other = C.source_check(sources, 'xkb:pl::pol')
        self.assertEqual((other.state, other.action), (WARNING, 'keyboard'))
        self.assertEqual(C.source_check(sources, None).state, WARNING)
        missing = C.source_check([('xkb', 'pl')], 'xkb:pl::pol')
        self.assertEqual((missing.state, missing.action), (ERROR, 'install'))

    def test_only_configured_source_inactive_needs_activation_guidance(self):
        for sources in ([('ibus', 'gramadrama')], [['ibus', 'gramadrama']]):
            with self.subTest(sources=sources):
                inactive = C.source_check(sources, 'xkb:us::eng')
                self.assertEqual((inactive.state, inactive.action), (WARNING, None))
                self.assertIn('ustawione poprawnie', inactive.text)
                self.assertIn('Kliknij zwykłe pole tekstowe', inactive.text)
                self.assertIn('w polu hasła', inactive.text)
                self.assertIn('Jeśli problem pozostaje', inactive.text)
                self.assertNotIn('Super+Spacja', inactive.text)
                self.assertEqual(C.source_check(sources, 'gramadrama').state, OK)
                unknown = C.source_check(sources, None)
                self.assertEqual((unknown.state, unknown.action), (WARNING, None))
                self.assertIn('Nie udało się odczytać', unknown.text)

    def test_when_is_local_and_relative(self):
        now = at(15, 0)
        self.assertEqual(C.when(at(14, 1), now), 'dziś 14:01')
        self.assertEqual(C.when(at(18, 22, 1), now), 'wczoraj 18:22')
        older = at(9, 5, 3)
        self.assertEqual(C.when(older, now), datetime.datetime.fromtimestamp(older).strftime('%d.%m 09:05'))

    def test_version_text(self):
        self.assertEqual(C.version_text(None), '')
        self.assertEqual(C.version_text({'installed_at': '2026-09-27T14:10:00'}), '')
        today = datetime.date.today().isoformat()
        info = {'commit': '2064c42', 'dirty': True, 'installed_at': today + 'T14:10:00'}
        self.assertEqual(C.version_text(info), ' · wersja 2064c42+zmiany, zainstalowana dziś 14:10')
        self.assertEqual(C.version_text({'commit': 'abc', 'installed_at': 'nonsense'}), ' · wersja abc')
        self.assertEqual(C.version_text({'version': '0.6.0', 'commit': 'abc'}), ' · wersja 0.6.0 (abc)')
        self.assertEqual(C.version_text({'version': '0.6.0'}), ' · wersja 0.6.0')

    def test_version_label_and_the_app_version(self):
        self.assertEqual(C.version_label({'version': '0.6.0', 'commit': '2064c42', 'dirty': True}), '0.6.0 (2064c42+zmiany)')
        self.assertEqual(C.version_label({'commit': '2064c42'}), '2064c42')  # installed before VERSION existed
        self.assertEqual(C.version_label(None), '')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(C, 'engine_dir', return_value=Path(tmp) / 'engine'):
            repository = Path(tmp) / 'repo'
            (repository / 'linux').mkdir(parents=True)
            self.assertEqual(C.app_version_text(None, repository / 'linux'), 'z repozytorium (bez instalacji)')
            (repository / 'VERSION').write_text('0.6.0\n')
            self.assertEqual(C.app_version_text(None, repository / 'linux'), '0.6.0, z repozytorium (bez instalacji)')
            installed = Path(tmp) / 'engine'
            self.assertEqual(C.app_version_text({'version': '0.6.0', 'commit': 'abc'}, installed), '0.6.0 (abc)')
            self.assertEqual(C.app_version_text({}, installed), 'nieznana')

    def engine(self, status=None, registered=True, installed=True, component='current', info=None,
               origin='installed', differs=False, stale=False):
        return C.engine_check(status, registered, installed, component, info, origin, differs, stale)

    def test_engine(self):
        running = {'test': False, 'backend': 'real'}
        today = datetime.date.today().isoformat()
        ok = self.engine(running, info={'commit': 'abc1234', 'installed_at': today + 'T09:00:00'})
        self.assertEqual(ok, Check(OK, 'Działa · wersja abc1234, zainstalowana dziś 09:00'))
        self.assertEqual(self.engine(None, origin=None).text, 'Gotowy — IBus uruchomi go przy pierwszym użyciu')
        repo = self.engine(running, installed=False, component='unknown', origin='repository')
        self.assertEqual(repo, Check(OK, 'Działa z repozytorium (bez instalacji)'))
        newer = self.engine(running, differs=True)
        self.assertEqual((newer.state, newer.action), (INFO, 'install'))

    def test_engine_problems(self):
        self.assertIn('trybie testowym', self.engine({'test': True, 'backend': 'debug'}).text)
        self.assertIn('127.0.0.1', self.engine({'test': True, 'backend': 'real'}).text)
        self.assertEqual(self.engine(None, False, False, 'missing'), Check(ERROR, 'Nie jest zainstalowany. Uruchom instalator.', 'install'))
        self.assertIn('sudo', self.engine(None, False, True, 'missing').text)
        self.assertIn('nie wczytał', self.engine(None, False, True, 'current').text)
        outdated = self.engine({'test': False}, component='outdated')
        self.assertEqual((outdated.state, outdated.action), (WARNING, 'install'))
        # The ibus CLI did not answer: unknown, never "ready", even with nothing installed.
        for installed in (True, False):
            unknown = self.engine(None, registered=None, installed=installed, component='missing', origin=None)
            self.assertEqual(unknown.state, WARNING)
            self.assertIn('IBus', unknown.text)
        stale = self.engine({'test': False}, stale=True)
        self.assertEqual(stale.state, WARNING)
        self.assertIn('starsza wersja', stale.text)

    def test_panel(self):
        active = {'state': 1.0, 'enabled': True}
        self.assertEqual(C.panel_check(active, '3', 3, None, 1000.0), Check(OK, 'Działa · wersja 3'))
        self.assertEqual(C.panel_check({}, None, None, None, None).action, 'install')
        # installed during this session: GNOME looks for extensions only at login
        just_copied = C.panel_check({}, None, 3, None, None)
        self.assertEqual((just_copied.state, just_copied.action), (WARNING, None))
        self.assertIn('po wylogowaniu', just_copied.text)
        self.assertEqual(C.panel_check({'state': 3.0, 'enabled': True, 'error': 'SyntaxError'}, None, 3, None, None),
                         Check(ERROR, 'Błąd dodatku: SyntaxError'))
        off = C.panel_check(active, '3', 3, None, 1000.0, user_disabled=True)
        self.assertEqual((off.state, off.action), (WARNING, None))  # "Włącz" could not help here
        self.assertEqual(C.panel_check({'state': 4.0, 'enabled': True}, None, 3, None, None).state, ERROR)
        self.assertEqual(C.panel_check({'state': 2.0, 'enabled': False}, None, 3, None, None).action, 'enable_panel')
        self.assertIn('zalogowaniu', C.panel_check({'state': 6.0, 'enabled': True}, None, 3, None, None).text)
        # enabled but inactive: the lock screen switches user extensions off until it is unlocked
        self.assertIn('blokady ekranu', C.panel_check({'state': 2.0, 'enabled': True}, None, 3, None, None).text)
        self.assertIn('nie odpowiada', C.panel_check(active, None, 3, None, None).text)

    def test_panel_waits_for_a_new_login(self):
        active = {'state': 1.0, 'enabled': True}
        newer = C.panel_check(active, '3', 4, None, 1000.0)
        self.assertEqual(newer.state, WARNING)
        self.assertIn('teraz działa wersja 3', newer.text)
        self.assertEqual(C.panel_check(active, '3', 3, 1500.0, 1000.0).state, WARNING)  # changed after login
        self.assertEqual(C.panel_check(active, '3', 3, 900.0, 1000.0).state, OK)  # changed, then logged in
        self.assertEqual(C.panel_check(active, '3', 3, 1500.0, None).state, OK)  # login time unknown


def ev(name, t=None, **data):
    return dict(t=time.time() if t is None else t, ev=name, **data)


class LogTests(unittest.TestCase):
    def test_parse_keeps_only_the_events_the_app_reads(self):
        lines = [json.dumps(ev('focus_in', client='gnome-shell'), separators=(',', ':')),
                 json.dumps(ev('start', id=1, scope='paragraph', chars=10, mode='fix'), separators=(',', ':')),
                 '{"t":1,"ev":"start"',  # cut off by a rotation
                 'garbage', '',
                 json.dumps(ev('verified', id=1, outcome='ok'), separators=(',', ':'))]
        events = C.parse_log('\n'.join(lines).encode())
        self.assertEqual([e['ev'] for e in events], ['start', 'verified'])

    def test_last_use(self):
        t = at(14, 1)
        self.assertEqual(C.last_use([]).state, INFO)
        applied = [ev('start', t, id=3, scope='paragraph', chars=55, mode='translate'), ev('result', t + 5, id=3, chars=60),
                   ev('apply', t + 6, id=3), ev('verified', t + 6, id=3, outcome='ok')]
        self.assertEqual(C.last_use(applied, t + 60), Check(OK, 'dziś 14:01 · Tłumacz · akapit · zastosowano'))
        cancelled = [ev('start', t, id=4, scope='selection', mode='fix'), ev('end', t + 1, id=4, state='ready', why='cancel')]
        self.assertEqual(C.last_use(cancelled, t + 60).text, 'dziś 14:01 · Popraw · zaznaczenie · anulowano')
        failed = [ev('start', t, id=5, scope='paragraph', mode='fix'), ev('backend_error', t + 1, id=5),
                  ev('end', t + 2, id=5, state='error', why='closed')]
        self.assertEqual(C.last_use(failed, t + 60), Check(WARNING, 'dziś 14:01 · Popraw · akapit · błąd korekty'))
        copied = [ev('start', t, id=8, scope='paragraph', mode='fix'), ev('result', t + 1, id=8),
                  ev('end', t + 2, id=8, why='copied')]
        self.assertEqual(C.last_use(copied, t + 60), Check(OK, 'dziś 14:01 · Popraw · akapit · skopiowano do schowka'))
        for why in ('key', 'reset', 'hidden', 'sensitive', 'disable', 'something-new'):
            ended = [ev('start', t, id=9, scope='paragraph', mode='fix'), ev('end', t + 1, id=9, why=why)]
            self.assertEqual(C.last_use(ended, t + 60).state, INFO, why)
        crashed = [ev('start', t, id=10, scope='paragraph', mode='fix'), ev('end', t + 1, id=10, why='error')]
        self.assertEqual(C.last_use(crashed, t + 60).state, WARNING)
        waiting = [ev('start', t, id=6, scope='paragraph', mode='fix')]
        self.assertEqual(C.last_use(waiting, t + 1).text, 'dziś 14:01 · Popraw · akapit · w toku')

    def test_last_use_is_the_latest_shortcut_and_ignores_other_sessions(self):
        t = at(10, 0)
        events = [ev('start', t, id=1, scope='paragraph', mode='fix'), ev('verified', t + 1, id=1, outcome='ok'),
                  ev('start', t + 10, id=2, scope='paragraph', mode='fix'),
                  ev('end', t + 11, id=1, why='focus'),  # an older session of another engine instance
                  ev('end', t + 12, id=2, why='unchanged')]
        self.assertEqual(C.last_use(events, t + 60).text, 'dziś 10:00 · Popraw · akapit · bez zmian')
        refused = events + [ev('refused', t + 20, at='hotkey', reason='stale')]
        check = C.last_use(refused, t + 60)
        self.assertEqual(check.state, INFO)  # asking for an arrow key is by design
        self.assertTrue(check.text.startswith('dziś 10:00 · odmowa: Nie widzę aktualnej pozycji kursora.'))
        # an X11 app that shares no text is a documented limit: information, not a problem
        self.assertEqual(C.last_use([ev('refused', t, at='hotkey', reason='no_text')], t + 1).state, INFO)

    def test_last_use_names_the_app(self):
        t = at(18, 48)
        names = {'chatgpt.desktop': 'ChatGPT', 'window:xterm': 'xterm'}
        applied = [ev('start', t, id=1, scope='paragraph', mode='fix', app='chatgpt.desktop', win='wayland'),
                   ev('apply', t + 3, id=1, app='chatgpt.desktop', win='wayland'),
                   ev('verified', t + 3, id=1, outcome='ok', app='chatgpt.desktop', win='wayland')]
        self.assertEqual(C.last_use(applied, t + 60, names).text, 'dziś 18:48 · ChatGPT · Popraw · akapit · zastosowano')
        self.assertEqual(C.last_use(applied, t + 60).text, 'dziś 18:48 · Popraw · akapit · zastosowano')  # names unknown yet
        off = [ev('refused', t, at='hotkey', reason='app_off', app='window:xterm', win='x11')]
        self.assertEqual(C.last_use(off, t + 1, names),
                         Check(INFO, 'dziś 18:48 · xterm · odmowa: GramaDrama jest wyłączona w tej aplikacji.'))
        # no focus window (''), an unknown focus (None), an app without a name or an odd value: no name
        for app in ('', None, 'other.desktop', ['chatgpt.desktop'], 5):
            event = ev('refused', t, at='hotkey', reason='stale', app=app)
            self.assertTrue(C.last_use([event], t + 1, names).text.startswith('dziś 18:48 · odmowa: '), app)

    def test_refused_apply_ends_the_session(self):
        t = at(11, 0)
        events = [ev('start', t, id=7, scope='paragraph', mode='fix'), ev('result', t + 2, id=7),
                  ev('refused', t + 3, at='apply', reason='changed')]
        self.assertEqual(C.last_use(events, t + 60).text, 'dziś 11:00 · Popraw · akapit · nie zastosowano: Tekst zmienił się. Nic nie zmieniono.')

    def test_engine_errors_of_the_last_day(self):
        t = time.time()
        events = [ev('error', t - 90000, where='key'), ev('start', t - 100, id=1, scope='paragraph', mode='fix'),
                  ev('verified', t - 99, id=1, outcome='ok'), ev('error', t - 50, where='ui'), ev('error', t - 40, where='ui')]
        check = C.last_use(events, t)
        self.assertEqual((check.state, check.action), (WARNING, 'log'))
        self.assertTrue(check.text.endswith('· 2 błędy silnika w ciągu doby (szczegóły w logu)'))
        self.assertEqual(C.plural(1, 'błąd', 'błędy', 'błędów'), 'błąd')
        self.assertEqual(C.plural(5, 'błąd', 'błędy', 'błędów'), 'błędów')
        self.assertEqual(C.plural(12, 'błąd', 'błędy', 'błędów'), 'błędów')
        self.assertEqual(C.plural(22, 'błąd', 'błędy', 'błędów'), 'błędy')

    def test_try_field_messages(self):
        state, text = C.try_event(ev('refused', at='hotkey', reason='stale'))
        self.assertEqual(state, WARNING)
        self.assertTrue(text.startswith('Skrót działa, ale'))
        self.assertIn('na końcu tekstu: w lewo', text)  # Right does not move a caret at the end of the text
        self.assertEqual(C.try_event(ev('refused', at='hotkey', reason='no_text'))[0], ERROR)
        self.assertEqual(C.try_event(ev('start', mode='translate')), (BUSY, 'Skrót działa. Model tłumaczy…'))
        self.assertEqual(C.try_event(ev('result', unchanged=True))[0], OK)
        self.assertEqual(C.try_event(ev('backend_error'))[0], ERROR)
        self.assertEqual(C.try_event(ev('verified', outcome='ok'))[0], OK)
        self.assertEqual(C.try_event(ev('verified', outcome='mismatch'))[0], WARNING)
        self.assertEqual(C.try_event(ev('verified', outcome='unverified'))[0], OK)
        self.assertEqual(C.try_event(ev('end', why='cancel'))[0], INFO)
        self.assertIsNone(C.try_event(ev('end', why='hotkey')))  # a new session follows
        self.assertIsNone(C.try_event(ev('focus_in')))

    def test_every_end_of_a_session_has_a_text(self):
        # session.py ends sessions with these reasons; a new one must not leave a spinner behind
        for why in ('cancel', 'unchanged', 'closed', 'copied', 'hidden', 'key', 'focus', 'reset', 'sensitive',
                    'timeout', 'disable', 'error', 'something-new'):
            state, text = C.try_event(ev('end', why=why))
            self.assertNotEqual(state, BUSY, why)
            self.assertTrue(text, why)
        self.assertEqual(C.try_event(ev('end', why='copied'))[0], OK)
        self.assertEqual(C.try_event(ev('end', why='error'))[0], ERROR)

    def test_not_reached_names_the_cause(self):
        text, known = C.not_reached(None, ['Mój'])
        self.assertTrue(known)
        self.assertIn('„Mój”', text)
        source = Check(WARNING, 'Aktywne jest inne źródło.')
        self.assertEqual(C.not_reached(source, []), ('Skrót nie dotarł do GramaDramy. Aktywne jest inne źródło.', True))
        text, known = C.not_reached(Check(OK, ''), [])
        self.assertFalse(known)  # nothing explains it: it stays failed until the engine gets the key
        self.assertIn('log', text)


class ServerTests(unittest.TestCase):
    def test_network_errors_in_polish(self):
        http = urllib.error.HTTPError('u', 502, 'x', {}, None)
        self.addCleanup(http.close)
        self.assertEqual(C.network_error(http), 'HTTP 502')
        self.assertEqual(C.network_error(urllib.error.URLError(socket.timeout())), 'nie odpowiada')
        self.assertEqual(C.network_error(TimeoutError()), 'nie odpowiada')
        self.assertIn('odmowa', C.network_error(urllib.error.URLError(ConnectionRefusedError())))
        self.assertEqual(C.network_error(urllib.error.URLError(socket.gaierror())), 'nieznany adres')
        self.assertIn('zasięgiem', C.network_error(urllib.error.URLError(OSError(113, 'No route to host'))))
        self.assertEqual(C.network_error(ValueError('bad json')), 'nieoczekiwana odpowiedź')

    def test_probe_the_stub(self):
        with Stub() as stub:
            stub.models = ['bielik-test', BIELIK]
            stub.loaded = [BIELIK]
            result = C.probe_server(core.Settings(**stub.settings()))
            self.assertTrue(result['llm_ok'])
            self.assertEqual((result['llm'].state, result['lt'].state), (OK, OK))
            self.assertEqual(result['models'], sorted(['bielik-test', BIELIK]))
            self.assertEqual(result['loaded'], [BIELIK])
            self.assertTrue(result['llm'].text.startswith('Działa · '))
            self.assertIn('Ollama', result['llm'].text)
            self.assertEqual(result['routes'], {'llm': 'direct', 'lt': 'direct'})
            stub.get_status = {'/api/version': 500}
            broken = C.probe_server(core.Settings(**stub.settings()))
            self.assertEqual(broken['llm'], Check(ERROR, 'Niedostępny (HTTP 500)'))
            self.assertIsNone(broken['models'])

    def test_probe_an_openai_style_server_with_a_key(self):
        with Stub() as stub:
            stub.models = ['gpt-test', 'other']
            stub.require_key = 'sk-secret'
            stub.get_status = {'/api/version': 404}  # an OpenAI-style server, not an Ollama
            ok = C.probe_server(core.Settings(**stub.settings(api='openai', key='sk-secret')), languagetool=False)
            self.assertTrue(ok['llm_ok'])
            self.assertEqual((ok['models'], ok['loaded']), (['gpt-test', 'other'], None))  # nothing is "in memory" there
            self.assertIn('OpenAI', ok['llm'].text)
            self.assertEqual({k for p, k in stub.headers_seen if p == '/v1/models'}, {'Bearer sk-secret'})
            wrong = C.probe_server(core.Settings(**stub.settings(api='openai', key='nope')), languagetool=False)
            self.assertEqual(wrong['llm'], Check(ERROR, 'Niedostępny (HTTP 401)'))
            stub.require_key = None
            self.assertEqual(C.model_check('gpt-test', ok['models'], None, 'openai').state, OK)
            self.assertEqual(C.model_check('', ok['models'], None, 'openai').state, WARNING)  # none chosen: nothing to correct with
            self.assertEqual(C.model_check('missing', ok['models'], None, 'openai').state, ERROR)
            self.assertIn('na serwerze', C.model_check('missing', ok['models'], None, 'openai').text)
            self.assertIn('pobierz go poniżej', C.model_check('missing', ok['models'], None, 'ollama', local=True).text)
            # an Ollama answering where an OpenAI-style one was set is told apart by the shape of the answer
            stub.get_status = {}
            odd = C.probe_server(core.Settings(**stub.settings(api='openai')), languagetool=False)
            self.assertTrue(odd['llm_ok'])  # the stub answers both ways, as an Ollama does
            self.assertEqual(odd['llm'].state, WARNING)
            self.assertIn('wybierz protokół „Ollama”', odd['llm'].text)
            stub.get_status = {'/api/version': 404}
            plain = C.probe_server(core.Settings(**stub.settings(api='openai')), languagetool=False)
            self.assertEqual(plain['llm'].state, OK)
            stub.get_status = {'/v1/models': 404}  # no OpenAI API here, but an Ollama: the hint names it
            hinted = C.probe_server(core.Settings(**stub.settings(api='openai')), languagetool=False)
            self.assertFalse(hinted['llm_ok'])
            self.assertEqual(hinted['llm'], Check(WARNING, 'To Ollama — wybierz protokół „Ollama”.'))
            stub.get_status = {'/api/version': 404}  # Ollama's API missing, an OpenAI one there
            hinted = C.probe_server(core.Settings(**stub.settings(api='ollama')), languagetool=False)
            self.assertEqual(hinted['llm'], Check(WARNING, 'To serwer zgodny z OpenAI — wybierz ten protokół.'))

    def test_this_computers_ollama(self):
        """"Ten komputer": Ollama at its default address or another local one; when it does not answer, the row says
        whether it is installed (the program or its systemd unit)."""
        url = 'http://127.0.0.1:%d' % closed_port()
        settings = core.Settings(llm_local_url=url, languagetool_local_url=url)
        for installed, text in ((False, 'Ollama nie jest zainstalowana'), (True, 'zainstalowana, ale nie działa')):
            with self.subTest(installed=installed), mock.patch.object(C, 'ollama_installed', return_value=installed):
                result = C.probe_server(settings)
                self.assertEqual(result['llm'].state, ERROR)
                self.assertIn(text, result['llm'].text)
                self.assertEqual(result['llm'].action, 'ollama_download' if not installed else None)
                self.assertEqual(result['lt'], Check(INFO, 'Nie działa na tym komputerze — korekta działa bez uzasadnień zmian.'))
        with Stub() as stub:
            result = C.probe_server(core.Settings(llm_local_url=stub.url, languagetool_local_url=stub.url))
            self.assertEqual((result['llm'].state, result['lt'].state), (OK, OK))
            self.assertEqual(result['routes'], {'llm': 'direct', 'lt': 'direct'})

    def test_a_model_pulled_into_this_computers_ollama(self):
        """"Pobierz model": the layers' sizes summed while Ollama pulls, and the model listed afterwards."""
        with Stub() as stub:
            seen = []
            C.ollama_pull(stub.url, 'gemma4:e4b', lambda done, total: seen.append((done, total)), threading.Event())
            self.assertEqual(stub.requests[-1], ('/api/pull', {'model': 'gemma4:e4b', 'stream': True}))
            self.assertEqual(seen[-1], (4000, 4000))
            self.assertIn('gemma4:e4b', stub.models)
            stub.pull_lines = [{'status': 'pulling manifest'}, {'error': 'pull model manifest: file does not exist'}]
            with self.assertRaisesRegex(RuntimeError, 'file does not exist'):
                C.ollama_pull(stub.url, 'nie-ma', lambda *_: None, threading.Event())
            stub.pull_lines = [{'status': 'pulling manifest'}]  # the answer ends without "success"
            with self.assertRaisesRegex(RuntimeError, 'Pobieranie przerwane'):
                C.ollama_pull(stub.url, 'urwany', lambda *_: None, threading.Event())
            cancel = threading.Event()
            cancel.set()
            C.ollama_pull(stub.url, 'przerwany', lambda *_: None, cancel)  # cancelled: no error
        with self.assertRaisesRegex(RuntimeError, 'Ollama nie odpowiada'):
            C.ollama_pull('http://127.0.0.1:%d' % closed_port(), 'x', lambda *_: None, threading.Event())

    def test_the_progress_of_a_pull(self):
        progress = C.PullProgress()
        for line in (b'{"status":"pulling a","digest":"a","total":10,"completed":4}', b'not json', b'[1]',
                     b'{"status":"pulling b","digest":"b","total":30}', b'{"status":"success"}'):
            self.assertIsNone(progress.apply(line))
        self.assertEqual((progress.completed, progress.total, progress.finished), (4, 40, True))
        self.assertEqual(progress.apply(b'{"error":"no space left"}'), 'no space left')

    def test_plain_http_outside_the_lan_is_a_warning(self):
        self.assertTrue(C.plain_text('http://model.example.com'))
        self.assertFalse(C.plain_text('https://model.example.com'))
        self.assertFalse(C.plain_text('http://localhost:11434'))  # this computer's network: no warning
        reply = C.Reply(True, 5, {'version': '1'})
        self.assertEqual(C.llm_check(reply, 'ollama', plain=True).state, WARNING)
        self.assertIn('użyj https://', C.llm_check(reply, 'ollama', plain=True).text)
        self.assertIn('przez bramę', C.llm_check(reply, 'ollama', kind='remote').text)

    def test_probe_the_transcription_server(self):
        with Stub() as stub:
            stub.require_key = 'sk-s'
            stub.models = ['whisper-1']
            probe = lambda **fields: C.probe_server(core.Settings(whisper_url=stub.url, **fields), llm=False,
                                                    languagetool=False, whisper=True)['whisper']
            ok = probe(whisper_key='sk-s')
            self.assertEqual(ok, Check(OK, ok.text))
            self.assertIn('API zgodne z OpenAI', ok.text)
            self.assertEqual(stub.headers_seen[-1], ('/v1/models', 'Bearer sk-s'))
            self.assertEqual(probe(whisper_key='nope'), Check(ERROR, 'Niedostępny (HTTP 401)'))
            stub.require_key = None
            other = probe(whisper_model='large-v3')
            self.assertEqual(other.state, WARNING)  # the server lists its models, and not this one
            self.assertIn('nie wymienia modelu „large-v3”', other.text)
            stub.get_status = {'/v1/models': 404, '/health': 404}  # a server with no list of models still answers
            self.assertEqual(probe().state, INFO)
            stub.get_status = {'/v1/models': 404}  # ...unless a whisper.cpp answers there: the hint names it
            self.assertEqual(probe(), Check(WARNING, 'To serwer whisper.cpp — wybierz protokół „whisper.cpp”.'))
            stub.get_status = {'/v1/models': 500}
            self.assertEqual(probe().state, ERROR)
            stub.get_status = {'/v1/models': 302}  # a redirect is not followed
            self.assertIn('przekierowuje', probe().text)
        url = 'http://127.0.0.1:%d' % closed_port()
        down = C.probe_server(core.Settings(whisper_url=url), llm=False, languagetool=False, whisper=True)['whisper']
        self.assertIn('odmowa połączenia', down.text)
        self.assertEqual(C.whisper_check(None).state, BUSY)
        self.assertEqual(C.whisper_unset().state, INFO)
        self.assertIn('nie opuszcza komputera', C.whisper_unset().text)

    def test_the_dictation_row_says_where_the_recording_goes(self):
        server = 'http://127.0.0.1:8000'
        on = C.dictation_check(True, False, [], server)  # no tools installed, but a server: ready, and it says so
        self.assertEqual(on, Check(OK, 'Rozpoznaje serwer mowy 127.0.0.1:8000.'))
        self.assertEqual(C.dictation_check(True, True, [], server).state, OK)  # the server wins over the built-in one
        self.assertEqual(C.dictation_check(False, True, [], server).state, INFO)  # switched off
        nothing = C.dictation_check(True, False, [])
        self.assertEqual(nothing.state, WARNING)
        self.assertIn('serwer mowy w zakładce Usługi', nothing.text)
        self.assertEqual(nothing.action, 'install_tools')
        here = C.dictation_check(True, False, [], '', 'local', services=True)  # the row on Usługi itself
        self.assertEqual((here.state, here.action), (WARNING, 'install_tools'))
        self.assertIn('wybierz „Serwer” powyżej', here.text)
        self.assertIn('wczyta się przy pierwszym dyktowaniu', C.dictation_check(True, True, []).text)  # the built-in Whisper

    def test_explicit_dictation_choice_controls_readiness_and_probe(self):
        with Stub() as stub:
            settings = core.Settings(whisper_backend='local', whisper_url=stub.url)
            self.assertIsNone(C.probe_server(settings, llm=False, languagetool=False, whisper=True)['whisper'])
            self.assertEqual(stub.requests, [])
            self.assertEqual(C.dictation_check(True, False, [], stub.url, 'local').state, WARNING)
            status = C.dictation_check(True, True, [{'ev': 'speech_model', 'state': 'ready', 'device': 'server'}], stub.url, 'local')
            self.assertIn('pierwszym dyktowaniu', status.text)
        for installed in (False, True):
            self.assertEqual(C.dictation_check(True, installed, [], '', 'server'), Check(ERROR, 'Ustaw serwer mowy w zakładce Usługi.'))
        self.assertEqual(C.whisper_unset('server'), Check(ERROR, 'Podaj adres serwera mowy.'))
        self.assertIn('nie opuszcza komputera', C.whisper_unset('local').text)
        missing = C.probe_server(core.Settings(whisper_backend='server'), llm=False, languagetool=False, whisper=True)
        self.assertEqual((missing['whisper'], missing['routes']), (C.whisper_unset('server'), {'speech': 'none'}))

    def test_native_whispercpp_probe_uses_health_and_checks_its_response(self):
        with Stub() as stub:
            settings = core.Settings(whisper_backend='server', whisper_api='whispercpp', whisper_url=stub.url + '/speech/',
                                     whisper_key='sk-synthetic')
            stub.require_key = 'sk-synthetic'
            probe = lambda: C.probe_server(settings, llm=False, languagetool=False, whisper=True)['whisper']
            self.assertEqual(probe().state, OK)
            self.assertIn('whisper.cpp', probe().text)
            self.assertEqual(stub.headers_seen[-1], ('/speech/health', 'Bearer sk-synthetic'))
            self.assertEqual(len(stub.requests), 2)
            stub.get_status = {'/speech/health': 404}
            self.assertEqual(probe().state, ERROR)
            stub.get_status = {'/speech/health': 503}
            self.assertEqual(probe(), Check(WARNING, 'Serwer whisper.cpp wczytuje model — spróbuj za chwilę.'))
            with mock.patch.object(C, 'fetch', return_value=C.Reply(True, 1, {'status': 'loading model'})):
                self.assertEqual(probe().state, ERROR)

    def test_dictation_backend_and_api_settings_are_checked(self):
        for field, choices in (('whisper_backend', ('auto', 'local', 'server')), ('whisper_api', ('openai', 'whispercpp'))):
            for choice in choices:
                self.assertIsNone(C.field_problem(field, choice))
            self.assertIsNotNone(C.field_problem(field, 'unknown'))

    def test_the_prompts_row(self):
        from gramadrama.prompts import Prompts
        plain = C.prompts_check(Prompts.defaults())
        self.assertEqual((plain.state, plain.action), (INFO, 'prompts_open'))
        self.assertIn('Domyślne', plain.text)
        own = C.prompts_check(Prompts({'task-fix.txt': 'x', 'rules.txt': 'y'}))
        self.assertEqual((own.state, own.text), (OK, 'Własne pliki (2): rules.txt, task-fix.txt'))
        many = C.prompts_check(Prompts({name: 'x' for name in ('a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt')}))
        self.assertIn('a.txt, b.txt, c.txt i 2 innych', many.text)
        broken = C.prompts_check(Prompts({'task-fix.txt': 'x'}, [('user.txt', 'brakuje {text}'), ('rules.txt', 'plik jest pusty')]))
        self.assertEqual(broken.state, WARNING)  # a problem wins over the good files: it is what to fix
        self.assertEqual(broken.text, 'Pominięty plik user.txt: brakuje {text} — używam domyślnego (i 1 innych).')

    def test_nothing_is_probed_that_is_not_set(self):
        self.assertEqual(C.probe_server(core.Settings(), llm=False, languagetool=False),
                         {'llm': None, 'llm_ok': False, 'models': None, 'loaded': None, 'lt': None, 'whisper': None, 'routes': {}})
        self.assertEqual(C.llm_unset().state, WARNING)  # without it nothing is corrected: the summary says so
        self.assertEqual(C.languagetool_unset().state, INFO)  # optional

    def test_unreachable_server(self):
        url = 'http://127.0.0.1:%d' % closed_port()
        result = C.probe_server(core.Settings(llm_url=url, llm_api='ollama', languagetool_url=url, model='x'))
        self.assertEqual(result['llm'].state, ERROR)
        self.assertIn('odmowa połączenia', result['llm'].text)
        self.assertEqual(result['lt'].state, WARNING)  # the model works without it
        self.assertNotIn('?.', result['lt'].text)
        self.assertEqual(C.model_check('x', result['models'], result['loaded']).state, INFO)

    def test_something_else_answering(self):
        with Stub() as stub:
            stub.get_status = {'/v2/languages': 404}
            result = C.probe_server(core.Settings(llm_url=stub.url, llm_api='ollama', languagetool_url=stub.url + '/api/tags',
                                                  model='bielik-test'))
            self.assertEqual(result['lt'].state, WARNING)  # /api/tags/v2/languages: 404

    def test_model(self):
        self.assertEqual(C.model_check('a', ['a:latest'], [], 'ollama').state, OK)  # Ollama adds :latest
        self.assertIn('Wczyta się', C.model_check(BIELIK, [BIELIK], [], 'ollama').text)
        self.assertIn('Wczytany do pamięci', C.model_check(BIELIK, [BIELIK], [BIELIK], 'ollama').text)
        self.assertEqual(C.model_check(BIELIK, [BIELIK], None, 'openai'), Check(OK, ''))  # nothing to say of memory
        self.assertEqual(C.model_check('b', ['a'], []).state, ERROR)
        self.assertEqual(C.model_check('bielik', ['bielik:latest'], ['bielik:latest'], 'ollama').text, 'Wczytany do pamięci.')
        self.assertEqual(C.model_check('', [], None, 'ollama', local=True).text, 'Brak modeli — pobierz model poniżej.')
        self.assertEqual(C.model_index(['a:latest', 'bielik:latest'], 'bielik'), 1)  # saved without a tag
        self.assertEqual(C.model_index(['a', 'b'], 'c'), -1)
        self.assertEqual(C.model_label(BIELIK), 'Bielik-11B-v2.3 · Q4_K_M')  # as on macOS, plus the tag
        self.assertEqual(C.model_label('qwen3:8b'), 'qwen3 · 8b')
        self.assertEqual(C.model_label('bielik:latest'), 'bielik')
        self.assertEqual(C.model_label('my/Model-Instruct'), 'Model')
        self.assertEqual(C.model_labels([BIELIK]), ['Bielik-11B-v2.3'])  # one variant: no tag needed
        self.assertEqual(C.model_labels([BIELIK, BIELIK.replace('Q4_K_M', 'Q8_0'), 'qwen3:8b']),
                         ['Bielik-11B-v2.3 · Q4_K_M', 'Bielik-11B-v2.3 · Q8_0', 'qwen3'])
        self.assertEqual(C.full_name('hf.co/a/b:Q4'), 'hf.co/a/b:Q4')
        self.assertEqual(C.full_name('host:5000/model'), 'host:5000/model:latest')

    def test_try_model_against_the_stub(self):
        with Stub() as stub:
            stub.chunks = stream('Dzięki za info, sprawdzę to jutro rano i dam znać, czy działa.', pieces=3)
            outcome = C.try_model(core.Settings(**stub.settings()), threading.Event())
            self.assertEqual(outcome[0], 'Dzięki za info, sprawdzę to jutro rano i dam znać, czy działa.')
            check = C.model_try_check(outcome)
            self.assertEqual(check.state, OK)
            self.assertIn('„Dzięki za info', check.text)
            self.assertEqual(stub.chats()[-1]['messages'][-1]['content'], core.user_prompt(C.SAMPLE, 'pl'))
            self.assertTrue(core.user_prompt(C.SAMPLE, 'pl').endswith('<text>\n%s\n</text>' % C.SAMPLE))
        url = 'http://127.0.0.1:%d' % closed_port()
        try:
            C.try_model(core.Settings(llm_url=url, llm_api='ollama', languagetool_url=url, model='x'), threading.Event())
        except core.BackendError as exc:
            self.assertEqual(C.model_try_check(exc).state, ERROR)
        else:
            self.fail('no error from a closed port')
        self.assertEqual(C.model_try_check(core.Cancelled()).state, INFO)
        self.assertIn('RuntimeError', C.model_try_check(RuntimeError('x')).text)

    def test_dictation_row(self):
        self.assertEqual(C.dictation_check(False, True, []).state, INFO)
        missing = C.dictation_check(True, False, [])
        self.assertEqual((missing.state, missing.action), (WARNING, 'install_tools'))
        self.assertIn('Brak modelu mowy', missing.text)
        self.assertIn('pierwszym dyktowaniu', C.dictation_check(True, True, []).text)
        events = [{'ev': 'speech_model', 'state': 'ready', 'device': 'cuda', 'ms': 918}]
        self.assertEqual(C.dictation_check(True, True, events).text, 'Rozpoznaje Whisper na tym komputerze na karcie graficznej.')
        self.assertIn('na procesorze', C.dictation_check(True, True, [dict(events[0], device='cpu')]).text)
        self.assertEqual(C.dictation_check(True, True, events + [{'ev': 'speech_model', 'state': 'failed'}]).state, ERROR)
        self.assertEqual(C.install_command('/repo/linux', 'tools'), '/usr/bin/python3 /repo/linux/install_engine.py tools')

    def test_the_sample_is_not_a_few_shot_example(self):
        self.assertNotIn(C.SAMPLE, [source for source, _, _ in core.EXAMPLES])

    def test_summary(self):
        self.assertEqual(C.summary([('a', Check(OK, '')), ('b', Check(INFO, ''))]), (OK, 'Wszystko gotowe'))
        self.assertEqual(C.summary([('a', Check(BUSY, '')), ('b', Check(ERROR, ''))]), (BUSY, 'Sprawdzam…'))
        self.assertEqual(C.summary([('silnik', Check(WARNING, '')), ('Bielik', Check(ERROR, '')), ('x', Check(OK, ''))]),
                         (ERROR, 'Wymaga uwagi: silnik, Bielik'))


class PackageInstallTests(unittest.TestCase):
    """The .deb package or the user's own copy: the IBus description (its <exec>) decides which engine is installed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.root = Path(self.tmp.name)
        for patcher in (mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'data'), 'IBUS_COMPONENT_PATH': str(root / 'ibus')}),
                        mock.patch.object(C, 'SYSTEM_DIR', root / 'usr-lib-gramadrama'),
                        mock.patch.object(C, 'SYSTEM_EXTENSIONS', root / 'usr-share-extensions')):
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / 'ibus').mkdir()

    def component(self, command):
        (self.root / 'ibus' / 'gramadrama.xml').write_text(
            '<?xml version="1.0"?><component><name>org.freedesktop.IBus.GramaDrama</name><exec>%s</exec></component>' % command)

    def test_the_description_decides(self):
        user = self.root / 'data' / 'gramadrama-engine'
        self.assertIsNone(C.component_launcher())  # no description yet
        self.assertEqual(C.engine_dir(), user)
        self.component('/usr/bin/python3 %s --ibus' % (user / 'gramadrama-engine'))
        self.assertEqual(C.component_launcher(), user / 'gramadrama-engine')
        self.assertFalse(C.package_installed())
        self.assertEqual(C.engine_dir(), user)
        self.component('/usr/bin/python3 %s --ibus' % (C.SYSTEM_DIR / 'gramadrama-engine'))
        self.assertTrue(C.package_installed())
        self.assertEqual(C.engine_dir(), C.SYSTEM_DIR)  # the user's old copy, if any, is not what IBus starts
        self.component('%s --ibus' % (C.SYSTEM_DIR / 'gramadrama-engine'))  # without the interpreter
        self.assertTrue(C.package_installed())
        self.component('/usr/bin/python3 %s2 --ibus' % (C.SYSTEM_DIR / 'gramadrama-engine'))
        self.assertFalse(C.package_installed())
        (self.root / 'ibus' / 'gramadrama.xml').write_text('<component><exec>')
        self.assertIsNone(C.component_launcher())  # a broken description: not the package

    def test_the_user_extension_wins(self):
        user = self.root / 'data' / 'gnome-shell' / 'extensions' / C.EXTENSION_UUID
        system = C.SYSTEM_EXTENSIONS / C.EXTENSION_UUID
        self.assertEqual(C.extension_dir(), user)
        system.mkdir(parents=True)
        self.assertEqual(C.extension_dir(), system)
        user.mkdir(parents=True)
        self.assertEqual(C.extension_dir(), user)  # as GNOME Shell chooses


class InstalledCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'data'),
                                               'IBUS_COMPONENT_PATH': str(root / 'ibus')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source, self.target, self.ext = root / 'repo', C.engine_dir(), C.extension_dir()
        for base in (self.source, self.target):
            (base / 'gramadrama' / 'engine').mkdir(parents=True)
            (base / 'gramadrama' / '__init__.py').write_text('')
            (base / 'gramadrama' / 'engine' / 'main.py').write_text('v1')
            (base / 'gramadrama-engine').write_text('launcher')
            (base / 'gramadrama-app').write_text('app')
        (self.target / 'gramadrama' / '__pycache__').mkdir()
        (self.target / 'gramadrama' / '__pycache__' / 'x.pyc').write_text('ignored')
        for base in (self.source / 'gnome-extension', self.ext):
            base.mkdir(parents=True)
            (base / 'extension.js').write_text('// v1')
            (base / 'metadata.json').write_text('{"version": 3}')

    def test_the_copy_matches_its_repository(self):
        self.assertFalse(C.copy_differs(self.source))
        (self.source / 'gnome-extension' / 'gschemas.compiled').write_text('build output')
        self.assertFalse(C.copy_differs(self.source))
        self.assertEqual(C.extension_version_on_disk(), 3)

    def test_any_difference_counts(self):
        cases = [lambda: (self.source / 'gramadrama' / 'engine' / 'main.py').write_text('v2'),
                 lambda: (self.target / 'gramadrama' / 'legacy.py').write_text('deleted in the repository'),
                 lambda: (self.source / 'gramadrama-app').write_text('app v2'),
                 lambda: (self.target / 'gramadrama-app').unlink(),
                 lambda: (self.ext / 'extension.js').write_text('// v2')]
        for change in cases:
            with self.subTest(change=change):
                self.setUp()
                change()
                self.assertTrue(C.copy_differs(self.source))

    def test_no_copy_or_no_repository(self):
        self.assertFalse(C.copy_differs(Path(self.tmp.name) / 'nowhere'))

    def test_component_and_install_info(self):
        self.assertEqual(C.component_state(), 'missing')
        ibus = Path(self.tmp.name) / 'ibus'
        ibus.mkdir()
        (ibus / 'gramadrama.xml').write_text('<component/>')
        self.assertEqual(C.component_state(), 'unknown')  # nothing staged to compare with
        (self.target / 'gramadrama.xml').write_text('<component/>')
        self.assertEqual(C.component_state(), 'current')
        (self.target / 'gramadrama.xml').write_text('<component>new</component>')
        self.assertEqual(C.component_state(), 'outdated')
        self.assertIsNone(C.install_info())
        (self.target / 'install.json').write_text('{"commit": "abc", "source": "/repo"}')
        self.assertEqual(C.install_info(), {'commit': 'abc', 'source': '/repo'})
        (self.target / 'install.json').write_text('[1]')
        self.assertIsNone(C.install_info())
        self.assertEqual(C.install_command('/repo/linux'), '/usr/bin/python3 /repo/linux/install_engine.py install')
        self.assertEqual(C.install_command('/my repo/linux'), "/usr/bin/python3 '/my repo/linux/install_engine.py' install")

    def test_relative_or_empty_xdg_dirs_are_ignored(self):
        home = Path.home()
        for value in ('', 'relative/dir'):
            with mock.patch.dict(os.environ, {'XDG_DATA_HOME': value, 'XDG_CACHE_HOME': value}):
                os.environ.pop('GRAMADRAMA_ENGINE_LOG', None)
                self.assertEqual(C.data_home(), home / '.local' / 'share')
                self.assertEqual(C.log_path(), home / '.cache' / 'gramadrama' / 'engine.log')

    def test_settings_the_engine_cannot_use(self):
        config = Path(self.tmp.name) / 'config'
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            path = core.Settings.path()
            self.assertEqual(C.read_settings(), (core.Settings(), {}))  # no file: the defaults, as for the engine
            path.parent.mkdir(parents=True)
            cases = [('{"model": null, "llm_url": "http://h:1"}', {'model': 'zły typ wartości'}),
                     ('{"llm_url": 5, "model": 7}', {'llm_url': 'zły typ wartości', 'model': 'zły typ wartości'}),
                     ('{"llm_url": "nie-adres"}', {'llm_url': 'Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434'}),
                     ('{"languagetool_url": "http://h:port"}', {'languagetool_url': 'Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434'}),
                     ('{"llm_api": "klingon"}', {'llm_api': 'nieznana wartość'}),
                     ('{"whisper_url": "ftp://x"}', {'whisper_url': 'Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434'}),
                     ('{"llm_url": "", "languagetool_url": "", "model": "   "}', {})]  # not set is no problem
            for content, problems in cases:
                with self.subTest(content=content):
                    path.write_text(content)
                    settings, found = C.read_settings()
                    self.assertEqual(found, problems)
                    self.assertIsInstance(settings.model, str)
            for content, reason in (('not json', 'JSON'), ('[1, 2]', 'nie zawiera ustawień')):
                path.write_text(content)
                self.assertIn(reason, C.read_settings()[1][C.FILE])
            path.write_bytes(b'{"model": "\xff"}')
            self.assertIn('UTF-8', C.read_settings()[1][C.FILE])  # never Python's English message
            path.write_text('{"languagetool_url": "nie-adres", "model": "m"}')
            settings, _ = C.read_settings()
            self.assertEqual((settings.languagetool_url, settings.model), ('nie-adres', 'm'))  # shown to be corrected
            path.write_text('{"llm_url": "http://h:1", "llm_api": "openai", "languagetool_url": "http://h:2", "model": "m", "extra": 1}')
            self.assertEqual(C.read_settings(), (core.Settings(llm_url='http://h:1', languagetool_url='http://h:2', model='m'), {}))
            # before 2026-10 the model server was ollama_url and spoke Ollama's own API
            path.write_text('{"ollama_url": "http://h:1", "languagetool_url": "http://h:2", "model": "m"}')
            self.assertEqual(C.read_settings(), (core.Settings(llm_url='http://h:1', llm_api='ollama',
                                                                languagetool_url='http://h:2', model='m'), {}))

    def test_texts_for_refused_settings(self):
        whole = C.settings_check({C.FILE: '/x/settings.json nie jest poprawnym plikiem JSON'})
        self.assertEqual(whole.state, ERROR)
        self.assertIn('wpisz adres', whole.text)
        problems = {'model': 'zły typ wartości', 'llm_api': 'nieznana wartość',
                    'llm_url': 'Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434'}
        self.assertIn('wybierz model z listy', C.settings_check(problems, 'model').text)
        self.assertIn('wybierz protokół z listy', C.settings_check(problems, 'llm_api').text)
        address = C.settings_check(problems, 'llm_url').text
        self.assertIn('Nieprawidłowy adres serwera modelu', address)
        self.assertNotIn('..', address)
        self.assertEqual(C.summary([('ustawienia', whole), ('ustawienia', whole), ('x', Check(OK, ''))]),
                         (ERROR, 'Wymaga uwagi: ustawienia'))

    def test_the_thinking_choice_is_saved_only_when_known(self):
        config = Path(self.tmp.name) / 'config'
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            self.assertEqual(core.Settings().llm_think, 'off')  # an old file without the field: off
            self.assertEqual(C.save_setting('llm_think', 'model'), (None, None))
            self.assertEqual(C.read_settings()[0].llm_think, 'model')
            error, _ = C.save_setting('llm_think', 'klingon')
            self.assertEqual(error, 'Nieznana wartość.')
            self.assertEqual(C.read_settings()[0].llm_think, 'model')  # the refused value was not written
            self.assertNotIn('llm_think', C.FIELD_NAMES)  # the engine never refuses it: no "will not correct" row

    def test_saving_repairs_a_broken_file_and_keeps_it(self):
        config = Path(self.tmp.name) / 'config'
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            path = core.Settings.path()
            self.assertEqual(C.save_setting('llm_url', 'nie-adres')[0][:5], 'Podaj')
            self.assertFalse(path.exists())  # an invalid value is never written
            self.assertEqual(C.save_setting('model', 'm'), (None, None))
            self.assertEqual(json.loads(path.read_text())['model'], 'm')
            broken = '{"model": null, "languagetool_url": "http://lt:1"}'
            path.write_text(broken)
            error, backup = C.save_setting('llm_url', 'http://o:2')
            self.assertIsNone(error)
            self.assertEqual(backup.read_text(), broken)  # kept for the user
            self.assertEqual(json.loads(path.read_text()), dict(dataclasses.asdict(core.Settings()), llm_url='http://o:2',
                                                                languagetool_url='http://lt:1'))
            self.assertEqual(C.read_settings()[1], {})
            # a refused text value that is not the one being saved: it goes back to its default
            path.write_text('{"model": 5, "languagetool_url": "nie-adres", "llm_url": "http://o:2"}')
            self.assertIsNone(C.save_setting('llm_url', 'http://o:3')[0])
            self.assertEqual(C.read_settings(), (core.Settings(llm_url='http://o:3'), {}))  # the refused ones: defaults
            # the model alone can repair the file too
            path.write_text('{"model": 7}')
            self.assertIsNone(C.save_setting('model', 'bielik')[0])
            self.assertEqual(C.read_settings()[0].model, 'bielik')

    def test_a_failed_save_keeps_the_old_file(self):
        config = Path(self.tmp.name) / 'config'
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            path = core.Settings.path()
            path.parent.mkdir(parents=True)
            path.write_text('{"model": null}')
            with mock.patch.object(core.Settings, 'save', side_effect=OSError(28, 'No space left on device')):
                error, backup = C.save_setting('model', 'm')
            self.assertIn('No space left', error)
            self.assertIsNone(backup)
            self.assertEqual(path.read_text(), '{"model": null}')  # never no file: the engine would use defaults

    def test_log_path_follows_the_engine(self):
        with mock.patch.dict(os.environ, {'GRAMADRAMA_ENGINE_LOG': '/x/engine.log'}):
            self.assertEqual(C.log_path(), Path('/x/engine.log'))
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': '/cache'}):
            os.environ.pop('GRAMADRAMA_ENGINE_LOG', None)
            self.assertEqual(C.log_path(), Path('/cache/gramadrama/engine.log'))

    def test_process_start_and_launcher(self):
        started = C.process_start(os.getpid())
        self.assertIsNotNone(started)
        self.assertLess(abs(started - time.time()), 24 * 3600 * 365)
        self.assertLessEqual(started, time.time() + 1)
        self.assertIsNone(C.process_start(0))
        self.assertIsNone(C.launcher_of(os.getpid()))



class LoginRowTests(unittest.TestCase):
    """The texts of the login row and of the servers that want the login (oidc.py)."""

    def settings(self, **fields):
        return core.Settings(auth_issuer='https://id.example.com', auth_client_id='client', **fields)

    def test_not_logged_in(self):
        check = C.login_check(self.settings(), 'out')
        self.assertEqual((check.state, check.text, check.action), (C.INFO, 'Nie zalogowano.', 'login'))

    def test_logged_in_and_out(self):
        check = C.login_check(self.settings(), 'in', 'Jan')
        self.assertEqual((check.state, check.text, check.action), (C.OK, 'Zalogowano jako Jan.', 'logout'))
        self.assertEqual(C.login_check(self.settings(), 'in').text, 'Zalogowano.')
        out = C.login_check(self.settings(), 'out', relevant='offline')
        self.assertEqual((out.state, out.text, out.action), (C.WARNING, 'Nie zalogowano — serwer nie odpowiada w tej sieci.', 'login'))
        required = C.login_check(self.settings(), 'out', relevant='required')
        self.assertEqual((required.state, required.text), (C.WARNING, 'Nie zalogowano — serwer wymaga logowania.'))
        self.assertEqual(C.login_check(self.settings(), 'out').state, C.INFO)  # nothing needs it (yet)
        waiting = C.login_check(self.settings(), 'out', pending=True)
        self.assertEqual((waiting.state, waiting.action), (C.BUSY, 'login'))

    def test_the_servers_say_they_want_the_login(self):
        for error, text in (('offline', C.LOGIN_NEEDED), ('nie zalogowano', C.LOGIN_REQUIRED)):
            reply = C.Reply(False, error=error)
            for check in (C.llm_check(reply), C.whisper_check(reply), C.languagetool_check(reply)):
                self.assertEqual((check.state, check.text, check.action), (C.WARNING, text, 'login'))
        self.assertEqual(C.llm_check(C.Reply(False, error='serwer odrzucił logowanie (HTTP 401)')).state, C.ERROR)



class BrowserRowTests(unittest.TestCase):
    """The row "Wtyczka" of the Przeglądarka group (the same extension as the Mac's)."""

    def test_connected_browsers_with_the_extension_version(self):
        row = C.browser_check({'browsers': {'google-chrome': '0.3.1', 'microsoft-edge': '0.3.0'}}, ['Google Chrome'], '/x')
        self.assertEqual((row.state, row.action), (C.OK, None))
        self.assertEqual(row.text, 'Połączona: Google Chrome (wtyczka 0.3.1) · Microsoft Edge (wtyczka 0.3.0). '
                                   'Stronę wyłączysz ikoną wtyczki.')

    def test_not_connected_says_what_to_do_and_where_the_extension_is(self):
        row = C.browser_check({'browsers': {}}, ['Google Chrome'], '/home/u/.local/share/gramadrama-engine/browser-extension')
        self.assertEqual((row.state, row.action), (C.INFO, 'extension_folder'))
        self.assertIn('chrome://extensions', row.text)  # the button opens the folder and puts its path on the clipboard

    def test_no_host_registered_anywhere_is_a_warning(self):
        row = C.browser_check({'browsers': {}}, [], '/x')
        self.assertEqual((row.state, row.action), (C.WARNING, 'install'))  # the button copies the install command
        self.assertIn('uruchom instalator', row.text)

    def test_an_engine_that_does_not_answer(self):
        self.assertEqual(C.browser_check(None, [], '/x').state, C.INFO)
        self.assertEqual(C.browser_check({}, ['Google Chrome'], '/x').state, C.INFO)  # an old engine knows no browsers

    def test_the_hosts_the_window_finds(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp}):
            for relative, name in (('google-chrome', 'pl.gramadrama.browser'), ('chromium', 'com.other.host')):
                hosts = Path(tmp) / relative / 'NativeMessagingHosts'
                hosts.mkdir(parents=True)
                (hosts / 'pl.gramadrama.browser.json').write_text(json.dumps({'name': name}))
            self.assertEqual(C.registered_browser_hosts(), ['Google Chrome'])

if __name__ == '__main__':
    unittest.main()


class CleanInstallTests(unittest.TestCase):
    """What a clean Ubuntu desktop lacks (the VM test of 2026-10-14): an NVIDIA card, a supported browser, a dictionary."""

    def test_nvidia_gpu(self):
        with tempfile.TemporaryDirectory() as root:
            def device(name, vendor, kind):
                folder = Path(root, name)
                folder.mkdir()
                (folder / 'vendor').write_text(vendor + '\n')
                (folder / 'class').write_text(kind + '\n')
            device('0000:00:02.0', '0x8086', '0x030000')  # an Intel graphics card
            device('0000:01:00.1', '0x10de', '0x040300')  # the HDMI audio of an NVIDIA card is not the card
            self.assertFalse(C.nvidia_gpu(root))
            device('0000:01:00.0', '0x10de', '0x030000')
            self.assertTrue(C.nvidia_gpu(root))
        self.assertTrue(C.nvidia_gpu('/nonexistent-pci'))  # unknown: the local model is not hidden

    def test_dictation_without_nvidia_offers_the_server(self):
        check = C.dictation_check(True, False, [], gpu=False)
        self.assertEqual((check.state, check.action), (WARNING, None))
        self.assertIn('wymaga karty NVIDIA', check.text)
        self.assertIn('w zakładce Usługi', check.text)
        self.assertIn('„Serwer” powyżej', C.dictation_check(True, False, [], services=True, gpu=False).text)
        self.assertEqual(C.dictation_check(True, False, [], gpu=True).action, 'install_tools')
        self.assertEqual(C.dictation_check(True, True, [], gpu=False).state, OK)  # tools already there: they run

    def test_browser_row_without_a_supported_browser(self):
        check = C.browser_check({'browsers': {}}, [], '/x', present=[])
        self.assertEqual((check.state, check.action), (INFO, None))
        self.assertIn('W Firefoksie', check.text)
        self.assertEqual(C.browser_check({'browsers': {}}, [], '/x', present=['Google Chrome']).state, WARNING)
        self.assertEqual(C.browser_check({'browsers': {}}, [], '/x').state, WARNING)  # not known: as before

    def test_missing_dictionaries(self):
        have = {'en_US'}
        self.assertEqual(C.missing_dictionaries(('pl', 'en', 'pl', 'xx'), lambda tag: tag in have), ['pl'])
        self.assertEqual(C.missing_dictionaries(('en',), lambda tag: tag in have), [])
        self.assertIsNone(C.dictionary_check([]))
        check = C.dictionary_check(['pl', 'de'])
        self.assertEqual((check.state, check.action), (WARNING, 'install_dictionaries'))
        self.assertIn('polski, niemiecki', check.text)
        self.assertEqual(C.dictionary_command(['pl', 'de']), 'sudo apt install hunspell-pl hunspell-de-de')
