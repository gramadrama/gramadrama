"""The account row of the remote access asks for the login only when a service needs it (window/services.py
refresh_login): a server that does not answer in this network, or one whose check asks for the login."""
from types import SimpleNamespace
import unittest
from unittest import mock

from gramadrama import checks, core
from gramadrama.window import services


class LoginRowTests(unittest.TestCase):
    def owner(self, routes, actions=(None, None, None)):
        rows = [SimpleNamespace(check=checks.Check(checks.OK, '', action)) for action in actions]
        return SimpleNamespace(settings_problems={}, login_starting=False, login_row=mock.Mock(), login_back_row=mock.Mock(),
                               write_state=mock.Mock(), routes=routes, llm_status_row=rows[0], lt_status_row=rows[1],
                               whisper_status_row=rows[2], load_settings=core.Settings)

    def shown(self, owner):
        settings = core.Settings(auth_issuer='https://id.example.com', auth_client_id='example-client')
        with mock.patch.object(services.oidc, 'status', return_value=('out', '')), \
                mock.patch.object(services.oidc, 'pending', return_value=None):
            services.ServicesTab.refresh_login(owner, settings)
        return owner.login_row.set_check.call_args.args[0]

    def test_nothing_needs_it(self):
        self.assertEqual(self.shown(self.owner({'llm': 'home', 'speech': 'direct'})).state, checks.INFO)

    def test_a_server_away_from_home(self):
        shown = self.shown(self.owner({'llm': 'home', 'speech': 'offline'}))
        self.assertEqual((shown.state, shown.text), (checks.WARNING, 'Nie zalogowano — serwer nie odpowiada w tej sieci.'))

    def test_the_gateway_itself(self):
        shown = self.shown(self.owner({'llm': 'gateway'}, actions=('login', None, None)))
        self.assertEqual((shown.state, shown.text), (checks.WARNING, 'Nie zalogowano — serwer wymaga logowania.'))


if __name__ == '__main__':
    unittest.main()
