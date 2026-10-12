"""The services and their route (gramadrama/core.py, docs/remote-access.md), as the Mac has them (Services.swift,
RemoteAccess.swift): each service on this computer or on a server; a server of the settings is asked directly while it
answers in this network, and away from home through the gateway of the remote access (the login address's origin with
/ollama, /languagetool and /whisper) with the login's token. Against stub servers: a "home" one and a "remote" one that
is the gateway and the login provider and wants the login. The stubs listen on 127.0.0.1, which counts as a server in the
home network here (GRAMADRAMA_ROUTE_LOOPBACK); a real one on this computer always goes directly."""
import array
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from gramadrama import checks as C, core, paths, speech
from stub_server import CLIENT_ID, Stub, closed_port, stream


class Route(unittest.TestCase):
    def setUp(self):
        self.home = Stub().__enter__()
        self.away = Stub().__enter__()  # the gateway, behind the provider's login (the stub is the provider too)
        self.addCleanup(self.home.__exit__)
        self.addCleanup(self.away.__exit__)
        self.away.require_login = True
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name + '/config', 'XDG_CACHE_HOME': self.tmp.name + '/cache',
                                           'GRAMADRAMA_ROUTE_LOOPBACK': '1'})
        env.start()
        self.addCleanup(env.stop)
        for stub in (self.home, self.away):
            stub.chunks = stream('Cześć!')

    def settings(self, at_home=True, login=True, **fields):
        """Servers at home (or a port with nothing on it), and the gateway with its login."""
        nothing = 'http://127.0.0.1:%d' % closed_port()
        data = dict(llm_location='server', llm_url=self.home.url if at_home else nothing, llm_api='ollama',
                    languagetool_location='server', languagetool_url=self.home.url if at_home else nothing,
                    model='bielik-test')
        if login:
            data.update(auth_issuer=self.away.url, auth_client_id=CLIENT_ID)
        data.update(fields)
        return core.Settings(**data)

    def login(self):
        paths.auth_path().parent.mkdir(parents=True, exist_ok=True)
        paths.auth_path().write_text(json.dumps(self.away.login()))


class ServiceTests(unittest.TestCase):
    """Where each service is (pure: nothing is asked)."""

    def test_a_new_install_uses_this_computer(self):
        settings = core.Settings()
        self.assertEqual(core.model_server(settings), (core.LOCAL_OLLAMA, 'ollama', ''))
        self.assertEqual(core.languagetool_server(settings), core.LOCAL_LANGUAGETOOL)
        self.assertEqual(core.speech_server(settings), '')  # the built-in Whisper
        self.assertEqual(core.route(settings, 'llm'), ('direct', core.LOCAL_OLLAMA))

    def test_settings_of_before_the_services_tab_keep_their_servers(self):
        """'auto': an address was the server; without one the model is this computer's Ollama now."""
        settings = core.Settings.from_dict({'llm_url': 'http://192.0.2.10:11434', 'llm_api': 'ollama', 'llm_key': 'k',
                                            'languagetool_url': '', 'llm_remote_url': 'https://gw.example.com/ollama'})
        self.assertEqual((core.located(settings, 'llm'), core.located(settings, 'lt')), ('server', 'local'))
        self.assertEqual(core.model_server(settings), ('http://192.0.2.10:11434', 'ollama', 'k'))
        self.assertFalse(hasattr(settings, 'llm_remote_url'))  # the gateway's path now

    def test_this_computer_may_have_another_address(self):
        settings = core.Settings(llm_location='local', llm_local_url='http://127.0.0.1:11435', llm_url='http://192.0.2.10:8080',
                                 llm_api='openai', llm_key='k', languagetool_location='local', languagetool_local_url='http://localhost:8081')
        self.assertEqual(core.model_server(settings), ('http://127.0.0.1:11435', 'ollama', ''))  # the server's key stays out
        self.assertEqual(core.languagetool_server(settings), 'http://localhost:8081')
        self.assertEqual(core.service_base(settings, 'lt'), 'http://localhost:8081')

    def test_an_address_on_this_computer_must_be_one(self):
        self.assertIsNone(core.local_url_problem(''))
        self.assertIsNone(core.local_url_problem('http://[::1]:11434'))
        self.assertIn('na tym komputerze', core.local_url_problem('http://192.0.2.10:11434'))
        self.assertIn('Podaj pełny adres', core.local_url_problem('nie-adres'))
        self.assertIn('na tym komputerze', C.field_problem('llm_local_url', 'http://192.0.2.10:11434'))
        with self.assertRaises(ValueError):
            core.Settings(languagetool_local_url='http://192.0.2.10:8010').validate(llm=False)

    def test_a_server_without_an_address_is_the_gateways(self):
        settings = core.Settings(llm_location='server', languagetool_location='server', whisper_backend='server',
                                 whisper_api='whispercpp', auth_issuer='https://id.example.com/', auth_client_id=CLIENT_ID)
        self.assertEqual(core.gateway(settings), 'https://id.example.com')
        self.assertEqual(core.model_server(settings), ('https://id.example.com/ollama', 'ollama', ''))
        self.assertEqual(core.languagetool_server(settings), 'https://id.example.com/languagetool')
        self.assertEqual(core.speech_server(settings), 'https://id.example.com/whisper')
        self.assertEqual(core.route(settings, 'llm')[0], 'gateway')  # on the gateway's origin: the login's token
        without = core.Settings(llm_location='server', languagetool_location='server', model='m')
        self.assertEqual((core.model_server(without)[0], core.languagetool_server(without)), ('', ''))
        with self.assertRaisesRegex(ValueError, 'Ten komputer'):
            without.validate()
        self.assertEqual(core.route(without, 'lt'), ('none', ''))

    def test_the_gateway_needs_the_client_id_too(self):
        self.assertEqual(core.gateway(core.Settings(auth_issuer='https://id.example.com')), '')

    def test_what_is_asked_whether_a_server_answers(self):
        settings = core.Settings(llm_url='http://192.0.2.10:8080', llm_api='openai', whisper_api='whispercpp')
        self.assertEqual(core.service_base(settings, 'llm') + core.status_path(settings, 'llm'), 'http://192.0.2.10:8080/v1/models')
        self.assertEqual(core.status_path(settings, 'lt'), '/v2/languages')
        self.assertEqual(core.status_path(settings, 'speech'), '/health')

    def test_test_mode_sees_every_address(self):
        settings = core.Settings(llm_url='http://127.0.0.1:1', whisper_backend='server', whisper_url='http://127.0.0.1:2',
                                 auth_issuer='https://id.example.com', auth_client_id=CLIENT_ID)
        self.assertEqual(core.addresses(settings), ['http://127.0.0.1:1', core.LOCAL_LANGUAGETOOL, 'http://127.0.0.1:2',
                                                    'https://id.example.com'])


class RouteChoiceTests(Route):
    def test_without_the_login_set_up_nobody_is_asked(self):
        settings = self.settings(login=False)
        self.assertEqual(core.route(settings, 'llm'), ('direct', self.home.url))
        self.assertEqual(self.home.requests, [])
        self.assertFalse(core.route_cache_path().exists())

    def test_this_computer_and_a_server_on_it_always_go_directly(self):
        local = self.settings(llm_location='local', languagetool_location='local')
        self.assertEqual(core.route(local, 'llm'), ('direct', core.LOCAL_OLLAMA))
        self.assertEqual(core.route(local, 'lt'), ('direct', core.LOCAL_LANGUAGETOOL))
        with mock.patch.dict(os.environ, {'GRAMADRAMA_ROUTE_LOOPBACK': ''}):
            self.assertEqual(core.route(self.settings(at_home=False), 'llm')[0], 'direct')  # 127.0.0.1 is this computer
        self.assertEqual(self.home.requests, [])

    def test_the_openai_protocol_goes_directly(self):
        """The gateway serves Ollama's API only."""
        self.assertEqual(core.route(self.settings(at_home=False, llm_api='openai'), 'llm')[0], 'direct')

    def test_at_home_the_server_answers(self):
        settings = self.settings()
        self.assertEqual(core.route(settings, 'llm'), ('home', self.home.url))
        self.assertEqual([p for p, _ in self.home.requests], ['/api/version'])  # the model server's status page
        cached = json.loads(core.route_cache_path().read_text())
        self.assertTrue(cached['answers'][self.home.url + '/api/version']['home'])

    def test_each_service_is_asked_on_its_own(self):
        settings = self.settings(languagetool_url='http://127.0.0.1:%d' % closed_port())
        self.login()
        self.assertEqual(core.route(settings, 'llm')[0], 'home')
        self.assertEqual(core.route(settings, 'lt'), ('remote', self.away.url + '/languagetool'))

    def test_any_http_answer_is_an_answer(self):
        self.home.get_status = {'/api/version': 401}  # a server that wants a key is there all the same
        self.assertEqual(core.route(self.settings(), 'llm')[0], 'home')

    def test_away_with_the_login_the_gateway(self):
        self.login()
        settings = self.settings(at_home=False)
        self.assertEqual(core.route(settings, 'llm'), ('remote', self.away.url + '/ollama'))
        self.assertEqual(core.route(settings, 'lt'), ('remote', self.away.url + '/languagetool'))

    def test_away_without_the_login_offline(self):
        self.assertEqual(core.route(self.settings(at_home=False), 'llm')[0], 'offline')

    def test_the_answer_is_remembered_for_five_minutes_and_a_miss_for_thirty_seconds(self):
        settings = self.settings()
        start = time.time()
        with mock.patch.object(core.time, 'time', return_value=start):
            self.assertEqual(core.route(settings, 'llm')[0], 'home')
        for later, asked in ((100, 1), (299, 1), (301, 2)):
            with mock.patch.object(core.time, 'time', return_value=start + later):
                core.route(settings, 'llm')
            self.assertEqual(len(self.home.requests), asked, later)
        self.login()
        away = self.settings(at_home=False)
        start = time.time() + 1000
        with mock.patch.object(core.time, 'time', return_value=start):
            self.assertEqual(core.route(away, 'llm')[0], 'remote')
        for later in (10, 29):
            with mock.patch.object(core.time, 'time', return_value=start + later), \
                    mock.patch.object(core, 'server_answers', return_value=False) as asked:
                core.route(away, 'llm')
            self.assertEqual(asked.call_count, 0)
        with mock.patch.object(core.time, 'time', return_value=start + 31), \
                mock.patch.object(core, 'server_answers', return_value=False) as asked:
            core.route(away, 'llm')
        self.assertEqual(asked.call_count, 1)  # after thirty seconds it asks again

    def test_another_network_asks_again(self):
        settings = self.settings()
        with mock.patch.object(core, 'network_fingerprint', return_value='wlan0:0101A8C0'):
            core.route(settings, 'llm')
            core.route(settings, 'llm')
        self.assertEqual(len(self.home.requests), 1)
        with mock.patch.object(core, 'network_fingerprint', return_value='tun0:0100000A'):  # a VPN: another default route
            core.route(settings, 'llm')
        self.assertEqual(len(self.home.requests), 2)

    def test_a_failed_connection_forgets_only_that_server(self):
        settings = self.settings()
        core.route(settings, 'llm')
        answers = json.loads(core.route_cache_path().read_text())['answers']
        answers['http://192.0.2.10:8010/v2/languages'] = {'home': True, 'at': time.time()}
        core._write_answers(core.network_fingerprint(), answers)
        core.forget_route(self.home.url + '/api/chat')
        self.assertEqual(list(json.loads(core.route_cache_path().read_text())['answers']), ['http://192.0.2.10:8010/v2/languages'])
        core.forget_route()
        self.assertFalse(core.route_cache_path().exists())

    def test_two_answers_at_once_are_both_remembered(self):
        """The model's and LanguageTool's ways are asked at the same time (LanguageTool runs alongside a correction,
        and the live service may ask too): neither answer may write over the other's."""
        write = core._write_answers

        def slow(net, answers):
            time.sleep(0.1)  # the other thread reads meanwhile
            write(net, answers)
        urls = ['http://192.0.2.10:11434/api/version', 'http://192.0.2.10:8010/v2/languages']
        with mock.patch.object(core, 'server_answers', return_value=False), mock.patch.object(core, '_write_answers', slow):
            threads = [threading.Thread(target=core._answers, args=(url,)) for url in urls]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(json.loads(core.route_cache_path().read_text())['answers']), sorted(urls))

    def test_the_fingerprint_is_the_default_routes(self):
        table = ('Iface\tDestination\tGateway\tFlags\n'
                 'wlan0\t00000000\t0101A8C0\t0003\n'
                 'wlan0\t0001A8C0\t00000000\t0001\n'
                 'tun0\t00000000\t0100000A\t0003\n')
        with mock.patch('builtins.open', mock.mock_open(read_data=table)):
            self.assertEqual(core.network_fingerprint(), 'tun0:0100000A;wlan0:0101A8C0')
        with mock.patch('builtins.open', side_effect=OSError):
            self.assertEqual(core.network_fingerprint(), '')

    def test_forcing_the_gateway_from_home(self):
        self.login()
        with mock.patch.dict(os.environ, {'GRAMADRAMA_FORCE_REMOTE': '1'}):
            self.assertEqual(core.route(self.settings(), 'llm')[0], 'remote')
        self.assertEqual(self.home.requests, [])  # the server at home was not even asked


class RoutedRequestsTests(Route):
    def backend(self, settings):
        return core.Backend(settings)

    def test_at_home_a_correction_goes_to_the_home_server_without_a_token(self):
        self.login()
        self.assertEqual(self.backend(self.settings()).correct('czesc!'), 'Cześć!')
        self.assertEqual(len(self.home.chats()), 1)
        self.assertEqual(self.away.chats(), [])
        self.assertEqual([a for p, a in self.home.headers_seen if p == '/api/chat'], [None])  # the token stays out of the LAN

    def test_away_it_goes_to_the_gateway_with_the_token_and_never_the_key(self):
        self.login()
        tokens = json.loads(paths.auth_path().read_text())
        settings = self.settings(at_home=False, llm_key='sk-home')
        self.assertEqual(self.backend(settings).correct('czesc!'), 'Cześć!')
        self.assertEqual(len(self.away.chats()), 1)
        self.assertIn('/ollama/api/chat', self.away.gateway_seen)
        self.assertEqual([a for p, a in self.away.headers_seen if p == '/api/chat'], ['Bearer ' + tokens['access_token']])

    def test_languagetool_follows_its_own_route(self):
        self.login()
        self.backend(self.settings(at_home=False)).check_live('Test.', 'pl-PL')
        self.assertIn('/languagetool/v2/check', self.away.gateway_seen)
        self.backend(self.settings()).check_live('Test.', 'pl-PL')
        self.assertEqual([p for p, _ in self.home.requests if p == '/v2/check'], ['/v2/check'])

    def test_away_without_the_login_says_what_to_do_and_asks_nobody(self):
        with self.assertRaisesRegex(core.BackendError, 'Zaloguj się w dostępie zdalnym'):
            self.backend(self.settings(at_home=False)).correct('czesc!')
        self.assertEqual((self.home.chats(), self.away.chats()), ([], []))

    def test_a_connection_that_fails_at_home_is_tried_again_by_the_new_route(self):
        """The route said "home" five minutes ago and the user left: the first request finds out, forgets the answer and goes
        through the gateway at once."""
        self.login()
        settings = self.settings()
        self.assertEqual(core.route(settings, 'llm')[0], 'home')
        self.home.__exit__()  # the server at home is gone, the answer is still remembered
        self.assertEqual(self.backend(settings).correct('czesc!'), 'Cześć!')
        self.assertEqual(len(self.away.chats()), 1)
        answer = json.loads(core.route_cache_path().read_text())['answers'][settings.llm_url + '/api/version']
        self.assertFalse(answer['home'])  # and the new answer is remembered

    def test_a_server_without_an_address_is_the_gateway_alone(self):
        self.login()
        settings = self.settings(llm_url='', languagetool_url='')
        self.assertEqual(self.backend(settings).correct('czesc!'), 'Cześć!')
        self.assertIn('/ollama/api/chat', self.away.gateway_seen)


class SpeechRouteTests(Route):
    """Dictation on a speech server takes the same way: away from home /whisper on the gateway, with the token."""

    def setUp(self):
        super().setUp()
        self.recording = array.array('h', [0] * 16000)
        for stub in (self.home, self.away):
            stub.transcript = 'Dzień dobry.'

    def test_away_the_recording_goes_to_the_gateway(self):
        self.login()
        tokens = json.loads(paths.auth_path().read_text())
        settings = self.settings(at_home=False, whisper_backend='server', whisper_api='whispercpp',
                                 whisper_url='http://127.0.0.1:%d' % closed_port(), whisper_key='sk-home')
        self.assertEqual(speech.RemoteWhisper(settings).transcribe(self.recording, 'pl', ''), 'Dzień dobry.')
        self.assertIn('/whisper/inference', self.away.gateway_seen)
        self.assertEqual([a for p, a in self.away.headers_seen if p == '/inference'], ['Bearer ' + tokens['access_token']])

    def test_at_home_directly(self):
        self.login()
        settings = self.settings(whisper_backend='server', whisper_api='whispercpp', whisper_url=self.home.url)
        speech.RemoteWhisper(settings).transcribe(self.recording, 'pl', '')
        self.assertEqual([p for p, _ in self.home.requests if p == '/inference'], ['/inference'])
        self.assertEqual(self.away.gateway_seen, [])

    def test_away_without_the_login(self):
        settings = self.settings(at_home=False, whisper_backend='server', whisper_api='whispercpp',
                                 whisper_url='http://127.0.0.1:%d' % closed_port())
        with self.assertRaisesRegex(RuntimeError, 'Zaloguj się w dostępie zdalnym'):
            speech.RemoteWhisper(settings).transcribe(self.recording, 'pl', '')


class ProbeTests(Route):
    def test_the_status_probe_of_the_app_follows_each_route(self):
        self.login()
        away = self.settings(at_home=False)
        result = C.probe_server(away)
        self.assertEqual(result['routes'], {'llm': 'remote', 'lt': 'remote'})
        self.assertEqual((result['llm'].state, result['lt'].state), (C.OK, C.OK))
        self.assertIn('przez bramę', result['llm'].text)
        self.assertEqual(result['models'], ['bielik-test'])
        home = C.probe_server(self.settings())
        self.assertEqual((home['routes']['llm'], home['llm'].state), ('home', C.OK))
        self.assertNotIn('przez bramę', home['llm'].text)
        paths.auth_path().unlink()
        core.forget_route()
        offline = C.probe_server(away)
        self.assertEqual(offline['routes'], {'llm': 'offline', 'lt': 'offline'})
        self.assertEqual((offline['llm'].action, offline['lt'].action), ('login', 'login'))

    def test_the_row_says_which_way(self):
        settings = self.settings()
        self.assertEqual(C.route_check(settings, {'llm': 'direct', 'lt': 'direct'}).state, C.INFO)
        home = C.route_check(settings, {'llm': 'home', 'lt': 'home'})
        self.assertEqual((home.state, home.text), (C.OK, 'Teraz bezpośrednio: %s.' % self.home.url.split('//')[1]))
        remote = C.route_check(settings, {'llm': 'remote', 'lt': 'remote'})
        self.assertEqual((remote.state, remote.text), (C.OK, 'Teraz przez bramę: %s.' % self.away.url.split('//')[1]))
        mixed = C.route_check(settings, {'llm': 'home', 'lt': 'remote', 'speech': 'remote'})
        self.assertEqual(mixed.text, 'Przez bramę: LanguageTool, mowa; bezpośrednio: model.')
        offline = C.route_check(settings, {'llm': 'offline', 'lt': 'home'})
        self.assertEqual((offline.state, offline.action), (C.WARNING, 'login'))


if __name__ == '__main__':
    unittest.main()
