"""Synthetic release-check guards; no build, network, application or desktop access.

The extracted functions use syntax shared by bash and zsh. These tests exercise
their decisions under bash; they do not replace a complete macOS/zsh release run.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'release-check.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


def function(name):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{[^\n]*\}$', SOURCE, re.M)
    if match is None:
        match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', SOURCE, re.M | re.S)
    if match is None:
        raise AssertionError('Release helper missing: ' + name)
    return match.group()


@unittest.skipUnless(shutil.which('bash'), 'bash is needed for isolated shell-function tests')
class ReleaseCheckTests(unittest.TestCase):
    def shell(self, body, directory, **extra):
        env = dict(os.environ)
        env.pop('GRAMADRAMA_LEVEL3_GO', None)
        env.update(extra)
        return subprocess.run(['bash', '-c', 'set -uo pipefail\n' + body], cwd=directory,
                              env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=5)

    def test_noninteractive_level_three_without_consent_never_installs_or_opens(self):
        level = SOURCE.split('if (( LEVEL >= 3 )); then', 1)[1].split('\n{\n', 1)[0]
        body = function('step') + '\n' + function('hands_off') + '''
LEVEL=3
FAILED=0
DIR=.
NAMES=(); STATUS=("✅"); TIMES=()
install_app() { echo UNEXPECTED_DESKTOP_ACCESS; exit 99; }
open() { echo UNEXPECTED_DESKTOP_ACCESS; exit 99; }
''' + 'if (( LEVEL >= 3 )); then' + level + '''
[[ $FAILED == 1 && ${STATUS[-1]} == "❌" && ${NAMES[-1]} == hands-off ]]
'''
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell(body, directory)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('UNEXPECTED_DESKTOP_ACCESS', result.stdout + result.stderr)

    def test_consent_file_is_consumed_once_without_desktop_access(self):
        with tempfile.TemporaryDirectory() as directory:
            consent = Path(directory) / 'consent'
            consent.touch()
            result = self.shell(function('hands_off') + '\nhands_off', directory,
                                GRAMADRAMA_LEVEL3_GO=str(consent))
            self.assertFalse(consent.exists())
        self.assertEqual(result.returncode, 0)

    def test_missing_server_or_model_never_attempts_a_network_request(self):
        for server, model in (('', 'synthetic-model'), ('http://192.0.2.10:11434', '')):
            body = function('model_server_up') + '''
SERVER_URL=%s
SERVER_API=ollama
MODEL=%s
defaults() { return 1; }
curl() { echo UNEXPECTED_NETWORK_ACCESS; exit 99; }
if model_server_up; then exit 1; fi
''' % (server, model)
            with self.subTest(server=server, model=model), tempfile.TemporaryDirectory() as directory:
                result = self.shell(body, directory)
                self.assertEqual(result.returncode, 0)
                self.assertNotIn('UNEXPECTED_NETWORK_ACCESS', result.stdout + result.stderr)

    def test_e2e_requires_final_success_line_not_just_process_exit(self):
        for content, expected in (('case: OK\nWszystko OK\n\n', 0),
                                  ('case: OK\n', 1), ('Wszystko OK\ncase: FAIL\n', 1), ('', 1)):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'result.txt').write_text(content, encoding='utf-8')
                result = self.shell(function('e2e_result') + '\ne2e_result result.txt', directory)
                self.assertEqual(result.returncode, expected)


if __name__ == '__main__':
    unittest.main()
