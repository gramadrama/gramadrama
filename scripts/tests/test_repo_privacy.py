"""Regression tests for the publication guard, including real temporary Git indexes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'check_repo_privacy.py'
SPEC = importlib.util.spec_from_file_location('repo_privacy', SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)

# Assemble intentionally sensitive-looking fixtures so these tests do not need
# exceptions in the real repository. None is a developer's actual endpoint/key.
LAN_IP = '.'.join(('192', '168', '42', '17'))
PERSONAL_HOME = '/home/' + 'person-fixture' + '/project'
TOKEN = 'gh' + 'p_' + 'Ab3' * 12


class ContentTests(unittest.TestCase):
    def rules(self, text, path='fixture.txt'):
        return {found.rule for found in guard.scan_text(path, text)}

    def test_rfc1918_link_local_and_carrier_nat_are_detected(self):
        for octets in ((10, 1, 2, 3), (172, 16, 8, 1), (172, 31, 255, 1), (192, 168, 1, 2),
                       (169, 254, 2, 3), (100, 64, 2, 3), (100, 127, 255, 2)):
            with self.subTest(octets=octets):
                self.assertEqual(self.rules('URL=http://' + '.'.join(map(str, octets))), {'private-ip'})

    def test_ipv6_local_and_mapped_ipv4_are_detected(self):
        for address in ('fd' + '12::5', 'fe' + '80::a%eth0', '::ffff:' + LAN_IP):
            with self.subTest(address=address):
                self.assertEqual(self.rules('http://[' + address + ']:11434'), {'private-ip'})

    def test_neutral_documentation_and_loopback_addresses_are_allowed(self):
        for address in ('127.0.0.1', '127.0.0.2', '::1', 'localhost', 'api.localhost',
                        '192.0.2.10', '198.51.100.4', '203.0.113.8', '2001:db8::1',
                        'example.com', 'api.example.org', 'example.net', '8.8.8.8'):
            with self.subTest(address=address):
                self.assertEqual(self.rules(address), set())

    def test_malformed_ip_and_unrelated_version_number_are_allowed(self):
        self.assertFalse(self.rules('release 192.168.300.1; library v10.3.4.5; 172.15.0.1'))

    def test_personal_unix_windows_and_mac_paths_are_detected(self):
        for path in (PERSONAL_HOME, '/Users/' + 'person-fixture/file',
                     'C:\\Users\\' + 'person-fixture\\file', 'C:/Users/' + 'person-fixture/file'):
            with self.subTest(path=path):
                self.assertEqual(self.rules(path), {'home-path'})

    def test_variable_and_placeholder_paths_are_allowed(self):
        self.assertFalse(self.rules('~/.config $HOME/project /home/user/repo /Users/username/repo '
                                    '/home/<user>/repo /home/${USER}/repo /home/u/test /home/runner/work'))

    def test_local_host_names_need_network_context_for_local_suffix(self):
        for suffix in ('.local', '.lan', '.home', '.home.arpa', '.internal'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.rules('https://fixture-host' + suffix + ':443'), {'local-host'})
        self.assertEqual(self.rules('hostname="fixture-host' + '.local"'), {'local-host'})
        self.assertFalse(self.rules('pl.gramadrama.local org.example.local self.local Path.home() self.home'))

    def test_standalone_lan_and_internal_host_names_are_detected(self):
        for suffix in ('.lan', '.internal', '.home.arpa'):
            self.assertEqual(self.rules('Server: fixture-host' + suffix), {'local-host'})

    def test_private_keys_and_provider_credentials_are_detected(self):
        values = [TOKEN, 'github_' + 'pat_' + 'Ab3_' * 12, 'AK' + 'IA' + 'A' * 16,
                  'sk-' + 'proj-' + 'A1b' * 12, 'xo' + 'xb-' + '1a-' * 12,
                  '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----']
        for value in values:
            with self.subTest(value=value[:12]):
                self.assertEqual(self.rules(value), {'secret'})

    def test_literal_secret_assignments_are_detected_but_placeholders_are_not(self):
        self.assertEqual(self.rules('api_key = "' + 'Ab3_' * 8 + '"'), {'secret'})
        self.assertEqual(self.rules('"client_secret": "' + 'Ab3_' * 8 + '"'), {'secret'})
        for value in ('test-example-value', 'your-api-key-here', '${SERVER_API_KEY}', 'example-password'):
            self.assertFalse(self.rules('api_key = "' + value + '"'))
        self.assertFalse(self.rules("'secret': 'Explanation of the secret rule in plain language'"))

    def test_exception_is_one_line_one_rule_and_requires_a_reason(self):
        allow = ' # privacy: allow[private-ip] synthetic network routing fixture'
        found = guard.scan_text('fixture.py', LAN_IP + allow + '\n' + LAN_IP)
        self.assertEqual([(item.line, item.rule) for item in found], [(2, 'private-ip')])
        self.assertEqual(self.rules(PERSONAL_HOME + ' ' + LAN_IP + allow), {'home-path'})
        self.assertEqual(self.rules(LAN_IP + ' # privacy: allow[private-ip]'), {'private-ip'})
        self.assertEqual(self.rules(LAN_IP + ' # privacy: allow[private-ip] ok'), {'private-ip'})

    def test_secrets_cannot_be_suppressed(self):
        self.assertEqual(self.rules(TOKEN + ' # privacy: allow[secret] synthetic fixture credential'), {'secret'})

    def test_binary_metadata_and_utf16_are_scanned(self):
        for data in (b'\x89PNG\0\xff' + PERSONAL_HOME.encode(), PERSONAL_HOME.encode('utf-16')):
            self.assertEqual({found.rule for found in guard.scan_bytes('image.bin', data)}, {'home-path'})

    def test_large_files_fail_closed(self):
        with mock.patch.object(guard, 'MAX_BYTES', 8):
            self.assertEqual(guard.scan_bytes('large.bin', b'123456789')[0].rule, 'too-large')

    def test_real_credential_files_are_rejected_but_templates_are_allowed(self):
        for path in ('.env', 'config/.env.production', 'config/auth.json', '.aws/credentials', 'cert.p12'):
            self.assertIn('credential-file', {item.rule for item in guard.scan_bytes(path, b'{}')})
        for path in ('.env.example', '.env.sample', '.env.template', 'auth.example.json'):
            self.assertFalse(guard.scan_bytes(path, b'{}'))

    def test_local_private_words_match_at_a_word_start_in_any_case(self):
        words = guard.parse_words('# own names, one per line\n\nZorblax\nfixture-host.sh\n')
        for line in ('Zorblax', 'w ZORBLAXIE', 'see scripts/fixture-host.sh'):
            with self.subTest(line=line):
                self.assertEqual({item.rule for item in guard.scan_text('notes.md', line, words)}, {'private-word'})
        for line in ('prezorblax', 'own names, one per line', 'Zorbla'):
            with self.subTest(line=line):
                self.assertFalse(guard.scan_text('notes.md', line, words))
        # In a binary file only readable strings (metadata) count: compressed bytes spell short words by chance.
        self.assertEqual({item.rule for item in guard.scan_bytes('image.png', b'\x89PNG\0tEXtSoftware\0Zorblax '
                                                                  b'screenshot tool\0\x01', words)}, {'private-word'})
        self.assertFalse(guard.scan_bytes('image.png', b'\x89PNG\0\x93zorblax\x01\xfe', words))

    def test_private_words_cannot_be_suppressed(self):
        words = guard.parse_words('Zorblax')
        line = 'Zorblax  # privacy: allow[private-word] a reason long enough'
        self.assertEqual({item.rule for item in guard.scan_text('notes.md', line, words)}, {'private-word'})

    def test_commit_message_skips_comments_and_the_diff_below_the_scissors(self):
        words = guard.parse_words('Zorblax')
        message = ('Panel: a fix\n\nTested with http://' + LAN_IP + '/ today\n# Zorblax in a comment line\n'
                   '# ------------------------ >8 ------------------------\n+Zorblax in the diff\n')
        self.assertEqual([(item.path, item.line, item.rule) for item in guard.scan_message(message, words)],
                         [('commit message', 3, 'private-ip')])
        self.assertEqual([item.line for item in guard.scan_message('A fix\n\nAfter Zorblax.', words)], [3])


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '--quiet')

    def git(self, *args, input=None):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, input=input).stdout

    def write(self, path, data):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data)
        return file

    def scan(self, **kwargs):
        return guard.scan_repository(self.root, **kwargs)[0]

    def test_default_includes_new_nonignored_files(self):
        self.write('.gitignore', 'private/\n')
        self.write('private/config.txt', LAN_IP)
        self.write('new.txt', PERSONAL_HOME)
        self.git('add', '.gitignore')
        self.assertEqual([item.path for item in self.scan()], ['new.txt'])
        self.assertFalse(self.scan(tracked_only=True))

    def test_force_added_ignored_files_are_still_scanned(self):
        self.write('.gitignore', 'private/\n')
        self.write('private/config.txt', LAN_IP)
        self.git('add', '-f', 'private/config.txt')
        self.assertEqual([item.rule for item in self.scan()], ['private-ip'])

    def test_private_archives_are_blocked_even_when_force_added_without_detectable_content(self):
        self.write('.gitignore', 'local-private/\nbench/lab/\nlinux/research/\n')
        for path in ('local-private/notes.md', 'bench/lab/session.txt', 'linux/research/shot.png'):
            self.write(path, 'text with no technical identifiers')
            self.git('add', '-f', path)
        self.assertEqual([item.rule for item in self.scan(staged=True)], ['private-artifact'] * 3)

    def test_staged_scan_reads_index_not_clean_unstaged_replacement(self):
        self.write('config.txt', LAN_IP)
        self.git('add', 'config.txt')
        self.write('config.txt', 'https://example.com')
        self.assertFalse(self.scan())
        self.assertEqual([item.rule for item in self.scan(staged=True)], ['private-ip'])

    def test_staged_scan_does_not_read_unstaged_private_text(self):
        self.write('config.txt', 'https://example.com')
        self.git('add', 'config.txt')
        self.write('config.txt', LAN_IP)
        self.assertFalse(self.scan(staged=True))
        self.assertEqual([item.rule for item in self.scan()], ['private-ip'])

    def test_working_deletion_is_skipped_while_staged_content_is_checked(self):
        file = self.write('config.txt', LAN_IP)
        self.git('add', 'config.txt')
        file.unlink()
        self.assertFalse(self.scan())
        self.assertEqual(len(self.scan(staged=True)), 1)

    def test_paths_with_spaces_and_non_ascii_are_supported(self):
        path = 'próba plików/a b.txt'
        self.write(path, LAN_IP)
        self.git('add', '--', path)
        self.assertEqual([item.path for item in self.scan(staged=True)], [path])

    def test_symlinks_are_not_followed_outside_repository(self):
        target = self.write('outside/config.txt', TOKEN)
        self.write('.gitignore', 'outside/\n')
        os.symlink(target, self.root / 'safe-link')
        os.symlink(PERSONAL_HOME, self.root / 'personal-link')
        self.git('add', '.gitignore', 'safe-link', 'personal-link')
        self.assertEqual([(item.path, item.rule) for item in self.scan()], [('personal-link', 'home-path')])
        self.assertEqual([(item.path, item.rule) for item in self.scan(staged=True)],
                         [('personal-link', 'home-path')])

    def test_cli_reports_location_and_rule_without_private_values(self):
        self.write('config.txt', TOKEN + '\n' + LAN_IP + '\n' + PERSONAL_HOME)
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('config.txt:1: [secret]', result.stdout)
        for value in (TOKEN, LAN_IP, PERSONAL_HOME):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_clean_repository_has_zero_exit_status(self):
        self.write('README.md', 'Use http://localhost:11434 or https://api.example.com')
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unmerged_index_is_a_failure(self):
        oid = self.git('hash-object', '-w', '--stdin', input=b'conflicted content').decode().strip()
        self.git('update-index', '--index-info', input=('100644 %s 2\tconflict.txt\n'
                                                       '100644 %s 3\tconflict.txt\n' % (oid, oid)).encode())
        self.assertEqual([item.rule for item in self.scan(staged=True)], ['unreadable'])

    def test_submodule_is_not_silently_skipped(self):
        with mock.patch.object(guard, 'entries', return_value=iter([('dependency', '160000', 'a' * 40)])):
            self.assertEqual([item.rule for item in self.scan(staged=True)], ['submodule'])

    def test_directory_symlink_does_not_read_files_outside_repository(self):
        with tempfile.TemporaryDirectory() as outside:
            self.write('tracked/config.txt', 'safe')
            self.git('add', 'tracked/config.txt')
            (self.root / 'tracked/config.txt').unlink()
            (self.root / 'tracked').rmdir()
            Path(outside, 'config.txt').write_text(TOKEN)
            os.symlink(outside, self.root / 'tracked')
            self.assertEqual([item.rule for item in self.scan(tracked_only=True)], ['unreadable'])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root, text=True, capture_output=True)

    def test_the_clones_word_list_is_used_and_a_missing_one_fails_closed(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, 'words.txt').write_text('Zorblax\n')
            self.write('notes.md', 'Ask Zorblax first.')
            self.assertEqual(self.cli().returncode, 0)  # no list set: CI and other clones
            self.git('config', 'privacy.wordsFile', str(Path(outside, 'words.txt')))
            result = self.cli()
            self.assertEqual(result.returncode, 1)
            self.assertIn('notes.md:1: [private-word]', result.stdout)
            self.assertIn('with the local word list', result.stdout)
            self.assertNotIn('Zorblax', result.stdout + result.stderr)
            self.git('config', 'privacy.wordsFile', str(Path(outside, 'missing.txt')))
            result = self.cli()
            self.assertEqual(result.returncode, 2)
            self.assertIn('local word list', result.stderr)

    def test_commit_message_cli_checks_only_the_message(self):
        self.write('notes.md', TOKEN)
        message = self.write('message.txt', 'Docs: the setup page\n\nSee http://' + LAN_IP + '/\n')
        result = self.cli('--message', str(message))
        self.assertEqual(result.returncode, 1)
        self.assertIn('commit message:3: [private-ip]', result.stdout)
        self.assertNotIn('notes.md', result.stdout)
        message.write_text('Docs: the setup page\n')
        self.assertEqual(self.cli('--message', str(message)).returncode, 0)

    def test_hooks_check_the_staged_files_and_the_message(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, 'words.txt').write_text('Zorblax\n')
            hooks = Path(__file__).resolve().parents[2] / '.githooks'
            (self.root / 'scripts').mkdir()
            os.symlink(SCRIPT, self.root / 'scripts' / 'check_repo_privacy.py')
            self.git('config', 'core.hooksPath', str(hooks))
            self.git('config', 'privacy.wordsFile', str(Path(outside, 'words.txt')))
            self.git('config', 'user.name', 'Test')
            self.git('config', 'user.email', 'test@example.com')
            self.write('notes.md', 'A plain note.')
            self.git('add', 'notes.md')
            with self.assertRaises(subprocess.CalledProcessError):
                self.git('commit', '--quiet', '-m', 'Notes from Zorblax')
            self.git('commit', '--quiet', '-m', 'Notes')
            self.write('notes.md', 'A note for Zorblax.')
            self.git('add', 'notes.md')
            with self.assertRaises(subprocess.CalledProcessError):
                self.git('commit', '--quiet', '-m', 'More notes')


if __name__ == '__main__':
    unittest.main()
