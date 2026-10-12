"""Logging in to servers through an OpenID Connect provider (gramadrama/oidc.py, docs/remote-access.md): the browser
login with PKCE, the tokens in auth.json, the refresh (once, under a lock, for every process), the rule that a token
goes only to the provider's own origin, and the requests of the model, LanguageTool, the checks and the speech
service carrying it. Against the stub provider: no real login, nothing leaves the machine."""
import array
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from gramadrama import checks as C, core, oidc, paths, speech
from stub_server import CLIENT_ID, Stub, sse

HERE = Path(__file__).resolve().parent


class Login(unittest.TestCase):
    """A provider and a config directory of its own for every test."""

    def setUp(self):
        self.stub = Stub().__enter__()
        self.addCleanup(self.stub.__exit__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.settings = core.Settings(**self.stub.settings(login=True))

    def save_login(self, **changes):
        tokens = dict(self.stub.login(), **changes)
        paths.auth_path().parent.mkdir(parents=True, exist_ok=True)
        paths.auth_path().write_text(json.dumps(tokens))
        return tokens

    def saved(self):
        return json.loads(paths.auth_path().read_text())

    def log_in(self):
        return oidc.finish_login(self.stub.authorize(oidc.start_login(self.settings)))


class PkceTests(unittest.TestCase):
    def test_the_challenge_is_the_one_of_rfc_7636(self):
        self.assertEqual(oidc.pkce_challenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),
                         'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')


class BrowserLoginTests(Login):
    def test_the_url_for_the_browser(self):
        url = oidc.start_login(self.settings)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts._replace(query='').geturl(), self.stub.url + '/authorize')
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.assertEqual({k: query[k] for k in ('response_type', 'client_id', 'redirect_uri', 'scope', 'code_challenge_method')},
                         {'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': 'gramadrama://auth',
                          'scope': 'openid profile offline_access', 'code_challenge_method': 'S256'})
        self.assertTrue(oidc.pending())
        pending = json.loads(oidc._pending_path().read_text())
        self.assertEqual(query['state'], pending['state'])
        self.assertEqual(query['code_challenge'], oidc.pkce_challenge(pending['verifier']))
        self.assertNotIn(pending['verifier'], url)  # the verifier never travels through the browser
        self.assertEqual(stat.S_IMODE(oidc._pending_path().stat().st_mode), 0o600)

    def test_coming_back_saves_the_login_for_the_user_alone(self):
        self.assertEqual(self.log_in(), 'Jan Testowy')
        tokens = self.saved()
        self.assertEqual((tokens['issuer'], tokens['client_id'], tokens['name']), (self.stub.url, CLIENT_ID, 'Jan Testowy'))
        self.assertIn(tokens['access_token'], self.stub.access_tokens)
        self.assertTrue(tokens['refresh_token'])
        self.assertGreater(tokens['expires_at'], time.time() + 3000)
        self.assertEqual(stat.S_IMODE(paths.auth_path().stat().st_mode), 0o600)
        self.assertFalse(oidc.pending())
        self.assertEqual(oidc.status(self.settings), ('in', 'Jan Testowy'))
        form, = self.stub.token_requests
        self.assertEqual((form['grant_type'], form['redirect_uri'], form['client_id']),
                         ('authorization_code', 'gramadrama://auth', CLIENT_ID))
        self.assertIn('code_verifier', form)  # the provider checked it against the challenge

    def test_a_code_of_another_login_is_refused(self):
        url = self.stub.authorize(oidc.start_login(self.settings))
        other = self.stub.authorize(oidc.start_login(self.settings))  # a second login: the first one's verifier is gone
        with self.assertRaisesRegex(oidc.AuthError, 'odmówił'):
            oidc.finish_login(url.split('&state=')[0] + '&state=' + urllib.parse.parse_qs(urllib.parse.urlsplit(other).query)['state'][0])
        self.assertFalse(paths.auth_path().exists())

    def test_nobody_is_waiting(self):
        with self.assertRaisesRegex(oidc.AuthError, 'Nie czekam'):
            oidc.finish_login('gramadrama://auth?code=x&state=y')

    def test_another_state_is_refused_and_the_login_goes_on(self):
        url = self.stub.authorize(oidc.start_login(self.settings))
        with self.assertRaisesRegex(oidc.AuthError, 'nie pasuje'):
            oidc.finish_login('gramadrama://auth?code=x&state=forged')  # e.g. a link on a web page
        self.assertTrue(oidc.pending())
        self.assertEqual(oidc.finish_login(url), 'Jan Testowy')

    def test_a_login_that_took_too_long_is_refused(self):
        url = self.stub.authorize(oidc.start_login(self.settings))
        with mock.patch.object(time, 'time', return_value=time.time() + oidc.PENDING_SECONDS + 5):
            with self.assertRaisesRegex(oidc.AuthError, 'Nie czekam'):
                oidc.finish_login(url)

    def test_the_user_says_no(self):
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(oidc.start_login(self.settings)).query)['state'][0]
        with self.assertRaisesRegex(oidc.AuthError, 'przerwane: The user denied'):
            oidc.finish_login('gramadrama://auth?error=access_denied&error_description=The+user+denied&state=' + state)
        self.assertFalse(oidc.pending())

    def test_no_code(self):
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(oidc.start_login(self.settings)).query)['state'][0]
        with self.assertRaisesRegex(oidc.AuthError, 'nie ma kodu'):
            oidc.finish_login('gramadrama://auth?state=' + state)

    def test_the_provider_is_down_while_coming_back(self):
        url = self.stub.authorize(oidc.start_login(self.settings))
        self.stub.token_status = 500
        with self.assertRaises(oidc.AuthError):
            oidc.finish_login(url)
        self.assertFalse(paths.auth_path().exists())

    def test_what_is_needed_to_start(self):
        with self.assertRaisesRegex(oidc.AuthError, 'identyfikator'):
            oidc.start_login(core.Settings(auth_issuer=self.stub.url))
        with self.assertRaisesRegex(oidc.AuthError, 'Pod tym adresem nie ma serwera logowania'):
            oidc.start_login(core.Settings(auth_issuer=self.stub.url + '/nothing-here', auth_client_id='x'))

    def test_an_address_that_is_no_provider(self):
        with mock.patch.object(core, 'opener_for', side_effect=AssertionError('no request without an address')):
            with self.assertRaises(oidc.AuthError):
                oidc.discover('')

    def test_the_callback_is_recognised(self):
        self.assertTrue(oidc.is_callback('gramadrama://auth?code=a&state=b'))
        self.assertFalse(oidc.is_callback('https://example.com/?x=gramadrama://auth'))
        self.assertFalse(oidc.is_callback('gramadrama://elsewhere'))


class IssuerTests(unittest.TestCase):
    def test_https_unless_the_provider_is_on_this_network(self):
        self.assertIsNone(oidc.issuer_problem(''))
        self.assertIsNone(oidc.issuer_problem('https://id.example.com'))
        for lan in ('http://127.0.0.1:1411', 'http://192.168.1.5', 'http://server.home.arpa:1411'):  # privacy: allow[private-ip,local-host] synthetic LAN issuer fixtures
            self.assertIsNone(oidc.issuer_problem(lan), lan)
        self.assertIn('https', oidc.issuer_problem('http://id.example.com'))
        self.assertIn('https', oidc.issuer_problem('http://8.8.8.8'))
        self.assertIn('Podaj pełny adres', oidc.issuer_problem('not an address'))

    def test_it_is_a_field_of_the_settings(self):
        self.assertIsNone(C.field_problem('auth_issuer', 'https://id.example.com'))
        self.assertIn('https', C.field_problem('auth_issuer', 'http://id.example.com'))
        self.assertIsNone(C.field_problem('auth_client_id', 'anything'))
        with self.assertRaisesRegex(ValueError, 'https'):
            core.Settings(auth_issuer='http://id.example.com').validate(llm=False)
        self.assertEqual(core.Settings.from_dict({'auth_issuer': 'https://a.b', 'auth_client_id': 'c'}).auth_client_id, 'c')


class RefreshTests(Login):
    def test_a_token_with_time_left_is_used_as_it_is(self):
        tokens = self.save_login()
        self.assertEqual(oidc.access_token(self.settings), tokens['access_token'])
        self.assertEqual(self.stub.token_requests, [])

    def test_an_expired_token_is_refreshed_and_the_new_refresh_token_kept(self):
        old = self.save_login(expires_at=time.time() - 5)
        token = oidc.access_token(self.settings)
        self.assertNotEqual(token, old['access_token'])
        form, = self.stub.token_requests
        self.assertEqual((form['grant_type'], form['refresh_token'], form['client_id']),
                         ('refresh_token', old['refresh_token'], CLIENT_ID))
        saved = self.saved()
        self.assertEqual((saved['access_token'], saved['name'], saved['issuer']), (token, '', self.stub.url))
        self.assertNotEqual(saved['refresh_token'], old['refresh_token'])  # rotated
        self.assertIn(saved['refresh_token'], self.stub.refresh_tokens)
        self.assertEqual(stat.S_IMODE(paths.auth_path().stat().st_mode), 0o600)
        self.assertEqual(oidc.access_token(self.settings), token)  # and then no more requests
        self.assertEqual(len(self.stub.token_requests), 1)

    def test_a_token_about_to_expire_is_refreshed_before_it_runs_out(self):
        self.save_login(expires_at=time.time() + oidc.EXPIRY_MARGIN - 10)
        oidc.access_token(self.settings)
        self.assertEqual(len(self.stub.token_requests), 1)

    def test_a_provider_that_does_not_rotate_keeps_the_refresh_token(self):
        self.stub.rotate = False
        old = self.save_login(expires_at=time.time() - 5)
        oidc.access_token(self.settings)
        self.assertEqual(self.saved()['refresh_token'], old['refresh_token'])

    def test_the_provider_ended_the_login(self):
        self.save_login(expires_at=time.time() - 5)
        self.stub.refresh_tokens.clear()
        with self.assertRaisesRegex(oidc.LoggedOut, 'Logowanie wygasło'):
            oidc.access_token(self.settings)
        self.assertFalse(paths.auth_path().exists())
        self.assertEqual(oidc.status(self.settings), ('out', ''))

    def test_a_provider_that_is_down_costs_nothing_while_the_token_still_works(self):
        tokens = self.save_login(expires_at=time.time() + 30)  # inside the margin, not yet expired
        self.stub.token_status = 503
        self.assertEqual(oidc.access_token(self.settings), tokens['access_token'])
        self.assertTrue(paths.auth_path().exists())  # a refresh that failed for a reason of the network ends nothing

    def test_a_provider_that_is_down_and_a_token_that_ran_out(self):
        self.save_login(expires_at=time.time() - 5)
        self.stub.token_status = 503
        with self.assertRaises(oidc.AuthUnreachable):
            oidc.access_token(self.settings)
        self.assertTrue(paths.auth_path().exists())  # the login is fine: try again when the provider is back
        self.stub.token_status = 200
        oidc.access_token(self.settings)

    def test_a_rejected_token_is_replaced_even_though_it_looks_valid(self):
        old = self.save_login()
        token = oidc.access_token(self.settings, rejected=old['access_token'])
        self.assertNotEqual(token, old['access_token'])
        self.assertEqual(len(self.stub.token_requests), 1)

    def test_a_rejected_token_another_process_has_replaced_already_is_not_replaced_again(self):
        self.save_login()
        newer = self.save_login()
        self.assertEqual(oidc.access_token(self.settings, rejected='an-older-one'), newer['access_token'])
        self.assertEqual(self.stub.token_requests, [])

    def test_a_login_of_another_provider_or_client_is_none(self):
        self.save_login()
        for other in (core.Settings(**dict(self.stub.settings(login=True), auth_client_id='another-client')),
                      core.Settings(**dict(self.stub.settings(login=True), auth_issuer=self.stub.url + '/')),  # same
                      core.Settings(**dict(self.stub.settings(login=True), auth_issuer='http://127.0.0.1:9'))):
            if other.auth_issuer.rstrip('/') == self.stub.url and other.auth_client_id == CLIENT_ID:
                self.assertTrue(oidc.access_token(other))  # a trailing slash is the same provider
            else:
                with self.assertRaises(oidc.LoggedOut):
                    oidc.access_token(other)

    def test_logging_out(self):
        self.save_login()
        oidc.start_login(self.settings)
        oidc.logout()
        self.assertEqual((paths.auth_path().exists(), oidc.pending(), oidc.status(self.settings)), (False, False, ('out', '')))
        oidc.logout()  # nothing to forget is no error
        self.assertEqual(oidc.status(core.Settings()), ('off', ''))

    def test_threads_that_find_the_token_expired_refresh_it_once(self):
        self.stub.token_delay = 0.3
        self.save_login(expires_at=time.time() - 5)
        results = []
        threads = [threading.Thread(target=lambda: results.append(oidc.access_token(self.settings))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.stub.token_requests), 1)
        self.assertEqual(len(set(results)), 1)

    def test_processes_that_find_the_token_expired_refresh_it_once(self):
        """The engine, its children and the app are processes of their own. A provider that rotates its refresh tokens
        ends the login of the one that uses an old one a second time: only one may refresh."""
        self.stub.token_delay = 0.5
        self.save_login(expires_at=time.time() - 5)
        script = ('import json, sys\nfrom gramadrama import core, oidc\n'
                  'print(oidc.access_token(core.Settings(**json.loads(sys.argv[1]))))')
        env = dict(os.environ, PYTHONPATH=str(HERE.parent), XDG_CONFIG_HOME=self.tmp.name)
        procs = [subprocess.Popen([sys.executable, '-c', script, json.dumps(self.stub.settings(login=True))], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [p.communicate(timeout=60) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 4, outputs)
        self.assertEqual(len({out.strip() for out, _ in outputs}), 1)
        self.assertEqual(len(self.stub.token_requests), 1)
        self.assertEqual(outputs[0][0].strip(), self.saved()['access_token'])


class OriginTests(Login):
    def test_which_requests_carry_the_login(self):
        for url, expected in ((self.stub.url + '/ollama/api/chat', True), (self.stub.url, True),
                              ('http://127.0.0.1:1/api/chat', False),  # another port: another server
                              ('http://localhost:%d/api/chat' % self.stub.server.server_port, False),  # another name
                              ('https://example.com/', False)):
            self.assertEqual(oidc.applies(self.settings, url), expected, url)
        self.assertFalse(oidc.applies(core.Settings(), self.stub.url))  # no provider set

    def test_the_header(self):
        tokens = self.save_login()
        self.assertEqual(oidc.authorization(self.settings, self.stub.url + '/x'), 'Bearer ' + tokens['access_token'])
        self.assertEqual(oidc.authorization(self.settings, self.stub.url + '/x', key='sk-typed'), 'Bearer sk-typed')  # wins
        self.assertEqual(oidc.authorization(self.settings, 'https://example.com/x'), '')  # never beyond the provider
        self.assertEqual(oidc.authorization(core.Settings(), self.stub.url + '/x'), '')

    def test_not_logged_in(self):
        with self.assertRaisesRegex(oidc.LoggedOut, 'Nie zalogowano. Zaloguj się'):
            oidc.authorization(self.settings, self.stub.url + '/x')
        self.assertEqual(oidc.authorization(self.settings, 'https://example.com/x'), '')  # a server elsewhere needs none


class BackendTests(Login):
    """Every request of the model server and of LanguageTool carries the login, when they are on the provider's origin."""

    def setUp(self):
        super().setUp()
        self.stub.require_login = True
        self.stub.chunks = __import__('stub_server').stream('Cześć!')
        self.stub.events = sse('Cześć!')

    def backend(self, api='ollama'):
        self.settings.llm_api = api
        return core.Backend(self.settings)

    def chat_token(self):
        return next(a for p, a in reversed(self.stub.headers_seen) if p in ('/api/chat', '/v1/chat/completions'))

    def test_a_correction_over_ollamas_api(self):
        tokens = self.save_login()
        self.assertEqual(self.backend('ollama').correct('czesc!'), 'Cześć!')
        self.assertEqual(self.chat_token(), 'Bearer ' + tokens['access_token'])

    def test_a_correction_over_the_openai_api(self):
        tokens = self.save_login()
        self.assertEqual(self.backend('openai').correct('czesc!'), 'Cześć!')
        self.assertEqual(self.chat_token(), 'Bearer ' + tokens['access_token'])

    def test_languagetool_on_the_same_origin_carries_it_too(self):
        tokens = self.save_login()
        self.backend().check_live('Test.', 'pl-PL')
        self.assertEqual(self.stub.headers_seen[-1], ('/v2/check', 'Bearer ' + tokens['access_token']))

    def test_an_expired_token_is_refreshed_before_the_request(self):
        self.save_login(expires_at=time.time() - 5)
        self.assertEqual(self.backend().correct('czesc!'), 'Cześć!')
        self.assertEqual(len(self.stub.token_requests), 1)
        self.assertEqual(self.chat_token(), 'Bearer ' + self.saved()['access_token'])

    def test_a_401_to_a_token_that_looked_valid_is_answered_with_a_new_one(self):
        self.save_login()
        self.stub.access_tokens.clear()  # the provider's session ended behind our back; its refresh token still lives
        self.assertEqual(self.backend().correct('czesc!'), 'Cześć!')
        self.assertEqual(len(self.stub.chats()), 1)  # the first, refused one never reached the model
        self.assertEqual([p for p, _ in self.stub.headers_seen if p == '/api/chat'], ['/api/chat', '/api/chat'])
        self.assertEqual(len(self.stub.token_requests), 1)

    def test_a_server_that_refuses_the_new_token_too(self):
        self.save_login()
        self.stub.access_tokens.clear()
        with mock.patch.object(self.stub, 'new_access_token', return_value='not-valid-on-the-server'):
            with self.assertRaisesRegex(core.BackendError, 'Zaloguj się ponownie'):
                self.backend().correct('czesc!')
        self.assertEqual([p for p, _ in self.stub.headers_seen if p == '/api/chat'], ['/api/chat', '/api/chat'])  # twice, not more

    def test_the_login_ended(self):
        self.save_login(expires_at=time.time() - 5)
        self.stub.refresh_tokens.clear()
        with self.assertRaisesRegex(core.BackendError, 'Logowanie wygasło') as caught:
            self.backend().correct('czesc!')
        self.assertEqual(self.stub.chats(), [])
        self.assertNotIn('Bearer', str(caught.exception))

    def test_nobody_is_logged_in(self):
        with self.assertRaisesRegex(core.BackendError, 'Nie zalogowano. Zaloguj się'):
            self.backend().correct('czesc!')
        self.assertEqual([p for p, _ in self.stub.headers_seen if p == '/api/chat'], [])  # nothing was sent

    def test_the_provider_is_down_while_the_token_ran_out(self):
        self.save_login(expires_at=time.time() - 5)
        self.stub.token_status = 503
        with self.assertRaises(core.Unreachable):
            self.backend().correct('czesc!')

    def test_a_key_the_user_typed_wins_over_the_login(self):
        self.save_login()
        self.stub.require_login = False
        self.stub.require_key = 'sk-typed'
        self.settings.llm_key = 'sk-typed'
        self.assertEqual(self.backend().correct('czesc!'), 'Cześć!')
        self.assertEqual(self.chat_token(), 'Bearer sk-typed')

    def test_a_server_elsewhere_never_sees_the_token(self):
        tokens = self.save_login()
        with Stub() as elsewhere:
            self.settings.languagetool_url = elsewhere.url  # another port: another origin
            self.backend().check_live('Test.', 'pl-PL')
            self.assertEqual(elsewhere.headers_seen, [('/v2/check', None)])
            self.settings.llm_url = elsewhere.url
            elsewhere.chunks = __import__('stub_server').stream('Cześć!')
            self.assertEqual(self.backend().correct('czesc!'), 'Cześć!')
        self.assertTrue(all(a is None for _, a in elsewhere.headers_seen), elsewhere.headers_seen)
        self.assertTrue(tokens['access_token'])

    def test_the_tokens_are_in_no_error(self):
        tokens = self.save_login()
        self.stub.chat_status = 500
        with self.assertRaises(core.BackendError) as caught:
            self.backend().correct('czesc!')
        for secret in (tokens['access_token'], tokens['refresh_token']):
            self.assertNotIn(secret, str(caught.exception))

    def test_every_request_says_who_it_is(self):
        """A proxy in front of a server (Cloudflare) may turn Python's own user agent away: every request is ours."""
        self.stub.require_login = False
        self.save_login()
        self.backend().correct('czesc!')
        self.backend().check_live('Test.', 'pl-PL')
        C.probe_server(self.settings)
        oidc.discover(self.stub.url)
        speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url)).transcribe(array.array('h', [0] * 1600), None, '')
        paths_seen = {path for path, _ in self.stub.agents}
        self.assertTrue({'/api/chat', '/v2/check', '/api/tags', '/.well-known/openid-configuration',
                         '/v1/audio/transcriptions'} <= paths_seen, paths_seen)
        self.assertEqual({agent for _, agent in self.stub.agents}, {'GramaDrama-Linux'})


class ChecksTests(Login):
    def test_the_probe_of_the_server_behind_the_login(self):
        self.stub.require_login = True
        self.save_login()
        result = C.probe_server(self.settings)
        self.assertEqual((result['llm'].state, result['lt'].state), (C.OK, C.OK), result)
        self.assertEqual(result['models'], ['bielik-test'])
        self.assertEqual(result['routes'], {'llm': 'gateway', 'lt': 'gateway'})  # the servers are the gateway itself

    def test_the_probe_says_when_nobody_is_logged_in(self):
        self.stub.require_login = True
        result = C.probe_server(self.settings)
        self.assertEqual(result['llm'], C.Check(C.WARNING, C.LOGIN_REQUIRED, 'login'))
        self.assertEqual(result['lt'], C.Check(C.WARNING, C.LOGIN_REQUIRED, 'login'))

    def test_the_probe_when_the_server_refuses_the_login(self):
        self.stub.require_login = True
        self.save_login()
        self.stub.access_tokens.clear()
        self.stub.refresh_tokens.clear()  # no way to a new token: the refresh ends the login
        result = C.probe_server(self.settings, languagetool=False)
        self.assertFalse(result['llm_ok'])
        self.assertEqual(result['llm'], C.Check(C.WARNING, C.LOGIN_REQUIRED, 'login'))

    def test_the_probe_when_the_new_token_is_refused_too(self):
        self.stub.require_login = True
        self.save_login()
        self.stub.access_tokens.clear()
        with mock.patch.object(self.stub, 'new_access_token', return_value='not-valid-on-the-server'):
            result = C.probe_server(self.settings, languagetool=False)
        self.assertEqual(result['llm'], C.Check(C.ERROR, 'Niedostępny (serwer odrzucił logowanie (HTTP 401))'))


class SpeechTests(Login):
    def test_a_transcription_server_behind_the_login(self):
        self.stub.require_login = True
        tokens = self.save_login()
        recording = array.array('h', [int(8000 * math.sin(i / 20)) for i in range(16000)])
        text = speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url, **{
            'auth_issuer': self.stub.url, 'auth_client_id': CLIENT_ID})).transcribe(recording, 'pl', '')
        self.assertEqual(text, 'Dyktowany tekst.')
        self.assertEqual(self.stub.headers_seen[-1], ('/v1/audio/transcriptions', 'Bearer ' + tokens['access_token']))

    def test_a_401_is_answered_with_a_new_token(self):
        self.stub.require_login = True
        self.save_login()
        self.stub.access_tokens.clear()
        recording = array.array('h', [0] * 1600)
        whisper = speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url, auth_issuer=self.stub.url,
                                                     auth_client_id=CLIENT_ID))
        self.assertEqual(whisper.transcribe(recording, None, ''), 'Dyktowany tekst.')

    def test_not_logged_in(self):
        self.stub.require_login = True
        whisper = speech.RemoteWhisper(core.Settings(whisper_url=self.stub.url, auth_issuer=self.stub.url,
                                                     auth_client_id=CLIENT_ID))
        with self.assertRaisesRegex(RuntimeError, 'Nie zalogowano'):
            whisper.transcribe(array.array('h', [0] * 1600), None, '')
        self.assertEqual(self.stub.transcriptions(), [])


if __name__ == '__main__':
    unittest.main()
