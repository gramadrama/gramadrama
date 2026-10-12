"""The engine's side of dictation's speech service (gramadrama/engine/speech.py): when dictation can run, and which
Python the service runs in, built-in Whisper or a transcription server (docs/dictation.md)."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock
from gramadrama import paths
from gramadrama.engine import speech


class Settings(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        patcher = mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': self.config.name, 'GRAMADRAMA_ENGINE_TEST': '',
                                                  'GRAMADRAMA_DEBUG_SPEECH': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(paths, 'speech_python', return_value=pathlib.Path('/nonexistent/python')),
                        mock.patch.dict(speech._settings, {'read': 0.0, 'data': {}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, data):
        speech._settings['read'] = 0.0  # the cache is for SETTINGS_SECONDS only
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_the_address_of_the_transcription_server(self):
        self.assertEqual(speech.remote_url(), '')  # no file
        for content, want in (({'whisper_url': ' http://127.0.0.1:8000 '}, 'http://127.0.0.1:8000'), ({'whisper_url': ''}, ''),
                              ({'whisper_url': 5}, ''), ({'whisper_url': None}, ''), ('not json', ''), ('[1]', ''),
                              ({'dictation_enabled': True}, '')):
            with self.subTest(content=content):
                self.write(content)
                self.assertEqual(speech.remote_url(), want)

    def test_dictation_can_run_with_an_address_or_with_the_tools(self):
        self.write({})
        self.assertFalse(speech.ready())  # no model, no server: nothing to run
        self.write({'whisper_url': 'http://127.0.0.1:8000'})
        self.assertTrue(speech.ready())  # an address is enough: no tools to install
        with mock.patch.object(paths, 'speech_python', return_value=pathlib.Path(self.config.name, 'python')), \
                mock.patch.object(paths, 'whisper_model', return_value=pathlib.Path(self.config.name, 'model')):
            self.write({})
            self.assertFalse(speech.ready())
            pathlib.Path(self.config.name, 'python').touch()
            pathlib.Path(self.config.name, 'model').mkdir()
            pathlib.Path(self.config.name, 'model', 'model.bin').touch()
            self.assertTrue(speech.ready())  # the built-in Whisper, as before

    def test_the_service_runs_in_the_python_that_fits_its_whisper(self):
        client = speech.SpeechClient(lambda f: f(), lambda event: None)
        self.assertEqual(client._python(), '/nonexistent/python')  # built-in: the speech environment
        client.remote = True
        self.assertEqual(client._python(), speech.sys.executable)  # a server: this Python, no numpy needed

    def test_explicit_local_keeps_the_address_but_requires_local_tools(self):
        self.write({'whisper_backend': 'local', 'whisper_url': 'https://example.com'})
        self.assertEqual(speech.remote_url(), 'https://example.com')
        self.assertFalse(speech.uses_server())
        self.assertFalse(speech.ready())

    def test_server_without_address_can_report_its_error_without_local_tools(self):
        self.write({'whisper_backend': 'server'})
        self.assertTrue(speech.uses_server())
        self.assertTrue(speech.ready())
        client = speech.SpeechClient(lambda f: f(), lambda event: None)
        with mock.patch.object(speech.ChildService, 'send'):
            client.send('start', 1)
        self.assertTrue(client.remote)
        self.assertEqual(client._python(), speech.sys.executable)

    def test_explicit_selection_restarts_service_without_changing_saved_address(self):
        client = speech.SpeechClient(lambda f: f(), lambda event: None)
        fields = {'whisper_url': 'https://example.com', 'whisper_backend': 'server'}
        with mock.patch.object(speech.ChildService, 'send'), mock.patch.object(speech.ChildService, 'close') as close:
            self.write(fields)
            client.send('start', 1)
            self.assertEqual((client.remote, close.call_count), (True, 1))
            self.write(dict(fields, whisper_backend='local'))
            client.send('start', 2)
            self.assertEqual((client.remote, close.call_count), (False, 2))
            self.assertEqual(client._python(), '/nonexistent/python')

    def test_a_change_of_the_setting_is_a_new_service(self):
        client = speech.SpeechClient(lambda f: f(), lambda event: None)
        with mock.patch.object(speech.ChildService, 'send') as send, mock.patch.object(speech.ChildService, 'close') as close:
            self.write({})
            client.send('start', 1)
            self.assertEqual((client.remote, close.call_count), (False, 0))  # nothing changed: the service stays
            self.write({'whisper_url': 'http://127.0.0.1:8000'})
            client.send('start', 2)
            self.assertEqual((client.remote, close.call_count), (True, 1))  # a server now: restarted for this Python
            client.send('start', 3)
            client.send('stop', 3)
            client.send('alternate', 3)
            self.assertEqual(close.call_count, 1)  # only a start looks, and only a change restarts
            self.write({})
            client.send('start', 4)
            self.assertEqual((client.remote, close.call_count), (False, 2))
            self.assertEqual(send.call_count, 6)


if __name__ == '__main__':
    unittest.main()
