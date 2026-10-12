import dataclasses
import datetime
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gramadrama import apps as A, checks as C, paths  # noqa: E402


def at(hour, minute, days_ago=0):
    moment = datetime.datetime.combine(datetime.date.today() - datetime.timedelta(days=days_ago),
                                       datetime.time(hour, minute))
    return moment.timestamp()


def ev(name, t=None, **data):
    return dict(t=time.time() if t is None else t, ev=name, **data)


def write(path, text='', mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    if mode is not None:
        path.chmod(mode)
    return path


def program(path, *resources):
    """An executable at path, with resources (resources.pak …) in its directory."""
    write(path, '#!/bin/sh\n', 0o755)
    for name in resources:
        write(Path(path).parent / name)
    return Path(path)


def desktop(name='App', exec_line='app %U', more=''):
    return '[Desktop Entry]\nType=Application\nName=%s\nIcon=app-icon\nExec=%s\n%s' % (name, exec_line, more)


def fake_process(proc, pid, argv, exe=None, comm='x', environ=None, started=100):
    """/proc/<pid> as the scans read it; argv: the arguments, or raw cmdline bytes."""
    base = Path(proc) / str(pid)
    base.mkdir(parents=True)
    (base / 'cmdline').write_bytes(argv if isinstance(argv, bytes) else b''.join(a.encode() + b'\0' for a in argv))
    if exe is not None:
        (base / 'exe').symlink_to(exe)
    (base / 'comm').write_text(comm + '\n')
    (base / 'stat').write_text('%d (%s) S 1 %s %d 0 0\n' % (pid, comm, ' '.join(['0'] * 17), started))
    if environ is not None:
        (base / 'environ').write_bytes(b''.join(('%s=%s' % item).encode() + b'\0' for item in environ.items()))
    return base


def write_mo(path, messages, header='Content-Type: text/plain; charset=UTF-8\n'):
    """A gettext catalogue (.mo) with {msgid: msgstr} and a header (UTF-8 by default)."""
    messages = {'': header, **messages}
    ids = [key.encode() for key in sorted(messages)]
    strs = [messages[key].encode() for key in sorted(messages)]
    offset, table = 28 + 16 * len(ids), []
    for item in ids + strs:
        table += [len(item), offset]
        offset += len(item) + 1
    data = struct.pack('<7I', 0x950412de, 0, len(ids), 28, 28 + 8 * len(ids), 0, 0)
    data += struct.pack('<%dI' % len(table), *table) + b''.join(item + b'\0' for item in ids + strs)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)


class TempHome(unittest.TestCase):
    """A temporary HOME with the XDG directories in it, a /usr/bin and PATH = ~/.local/bin:/usr/bin."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / 'home'
        self.data = self.home / '.local' / 'share'
        self.system = self.root / 'usr' / 'share'
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / '.config'),
                                               'XDG_DATA_HOME': str(self.data), 'XDG_DATA_DIRS': str(self.system)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bin = self.root / 'usr' / 'bin'
        self.local_bin = self.home / '.local' / 'bin'
        self.bin.mkdir(parents=True)
        self.places = A.Places((str(self.local_bin), str(self.bin)), self.local_bin, self.data / 'applications',
                               self.root / 'snap')

    def installed(self, places=None):
        return A.installed_apps([self.data, self.system], path=(places or self.places).path, names=[])


class ConfigTests(TempHome):
    def test_no_file_switches_nothing_off(self):
        self.assertEqual(paths.apps_path(), self.home / '.config' / 'gramadrama' / 'apps.json')
        self.assertEqual(A.read_config(), (A.Config(), None))

    def test_the_lists_as_the_engine_uses_them(self):
        path = write(paths.apps_path(), json.dumps({'v': 1, 'disabled': ['a.desktop', 'window:xterm', 'a.desktop', A.SELF],
                                                    'added': ['b.desktop'], 'later': True}))
        self.assertEqual(A.read_config(), (A.Config(['a.desktop', 'window:xterm'], ['b.desktop']), None))
        write(path, '{"v": 1}')
        self.assertEqual(A.read_config(), (A.Config(), None))

    def test_problems_are_reported_as_for_settings(self):
        path = paths.apps_path()
        cases = [('not json', 'nie jest poprawnym plikiem JSON'), ('[1]', 'nie zawiera listy aplikacji'),
                 ('{"disabled": []}', 'nieobsługiwaną wersję'), ('{"v": 2}', 'nieobsługiwaną wersję'),
                 ('{"v": true}', 'nieobsługiwaną wersję'), ('{"v": 1, "disabled": "a.desktop"}', '„disabled” w'),
                 ('{"v": 1, "added": [5]}', '„added” w'), ('{"v": 1, "disabled": [""]}', '„disabled” w'),
                 ('{"v": 1, "modes": ["a.desktop"]}', '„modes” w'), ('{"v": 1, "modes": {"a.desktop": 5}}', '„modes” w'),
                 ('{"v": 1, "modes": {"": "formal"}}', '„modes” w'), ('{"v": 1, "modes": null}', '„modes” w')]
        for content, reason in cases:
            with self.subTest(content=content):
                write(path, content)
                config, problem = A.read_config()
                self.assertEqual(config, A.Config())  # the engine fails open: nothing is off
                self.assertIn(reason, problem)
                self.assertIn(str(path), problem)
        path.write_bytes(b'{"v": 1, "disabled": ["\xff"]}')
        self.assertIn('UTF-8', A.read_config()[1])  # never Python's English message
        path.write_text('{"v": 1}')
        with mock.patch.object(A.json, 'loads', side_effect=RecursionError):  # nesting too deep for the parser
            self.assertIn('nie jest poprawnym plikiem JSON', A.read_config()[1])
        path.write_text(json.dumps({'v': 1, 'disabled': ['app%d.desktop' % i for i in range(5000)]}))
        self.assertEqual(A.read_config(), (A.Config(), '%s jest za duży (ponad 64 KiB)' % path))
        path.unlink()
        path.mkdir()
        self.assertIn('nie można odczytać', A.read_config()[1])

    def test_the_engine_calls_the_same_files_broken(self):
        try:
            from gramadrama.engine import applist
        except ImportError:
            self.skipTest('the engine does not read apps.json here')
        path = paths.apps_path()
        path.parent.mkdir(parents=True)
        samples = [b'{"v": 1, "disabled": ["a.desktop", "a.desktop", "pl.gramadrama.Linux.desktop"], "added": ["b"]}',
                   b'{"v": 1}', b'{"v": 1.0, "disabled": ["window:xterm"]}', b'not json', b'[1]', b'{"disabled": []}',
                   b'{"v": 2}', b'{"v": true}', b'{"v": 1, "disabled": "a"}', b'{"v": 1, "added": [5]}',
                   b'{"v": 1, "disabled": [""]}', b'{"v": 1, "disabled": null}', b'{"v": 1, "disabled": ["\xff"]}',
                   b'\xef\xbb\xbf{"v": 1}', b'[' * 20000 + b']' * 20000, b'[' * 100000 + b']' * 100000,
                   json.dumps({'v': 1, 'disabled': ['app%d.desktop' % i for i in range(5000)]}).encode(),
                   b'{"v": 1, "modes": {"a.desktop": "formal", "pl.gramadrama.Linux.desktop": "shorter"}}',
                   b'{"v": 1, "modes": {"a.desktop": "no-such-mode"}}', b'{"v": 1, "modes": {}}',
                   b'{"v": 1, "modes": []}', b'{"v": 1, "modes": {"a.desktop": 5}}', b'{"v": 1, "modes": {"": "fix"}}',
                   b'{"v": 1, "modes": null}', b'{"v": 1, "modes": "formal"}']
        for data in samples:
            with self.subTest(data=data[:50]):
                path.write_bytes(data)
                config, problem = A.read_config()
                disabled, modes, ok = applist.load_all(data)
                self.assertEqual(problem is None, ok)
                self.assertEqual(frozenset(config.disabled), disabled)
                self.assertEqual(config.modes, {app: mode for app, mode in modes.items() if app != A.SELF})

    def test_a_switch_writes_the_file_at_once(self):
        path = paths.apps_path()
        self.assertEqual(A.set_enabled('a.desktop', False), (None, None))
        self.assertEqual(json.loads(path.read_text()), {'v': 1, 'disabled': ['a.desktop'], 'added': []})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        A.set_enabled('b.desktop', False)
        A.set_enabled('a.desktop', False)  # already off: stays where it is
        self.assertEqual(A.read_config()[0].disabled, ['a.desktop', 'b.desktop'])
        A.set_enabled('a.desktop', True)
        A.set_enabled('c.desktop', True)  # on already
        self.assertEqual(A.read_config()[0].disabled, ['b.desktop'])
        A.set_enabled(A.SELF, False)  # the Wypróbuj field always works
        self.assertEqual(A.read_config()[0].disabled, ['b.desktop'])
        self.assertEqual(os.listdir(path.parent), ['apps.json'])  # no temp file left behind

    def test_the_mode_per_app_is_written_and_read(self):
        path = paths.apps_path()
        write(path, json.dumps({'v': 1, 'modes': {'a.desktop': 'formal', 'old.desktop': 'no-such-mode'}}))
        self.assertEqual(A.read_config(), (A.Config(modes={'a.desktop': 'formal', 'old.desktop': 'no-such-mode'}), None))
        self.assertEqual(A.set_mode('b.desktop', 'shorter'), (None, None))
        self.assertEqual(json.loads(path.read_text())['modes'],
                         {'a.desktop': 'formal', 'old.desktop': 'no-such-mode', 'b.desktop': 'shorter'})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        A.set_mode('a.desktop', 'improve')  # changed
        A.set_mode('old.desktop', 'fix')  # Popraw is no entry: a mode that was written once goes
        A.set_mode('b.desktop', 'translate')  # Tłumacz has its own shortcut: nothing is written, and the entry goes
        A.set_mode('c.desktop', 'no-such-mode')  # never written
        A.set_mode(A.SELF, 'formal')  # the Wypróbuj field always starts in Popraw
        self.assertEqual(A.read_config()[0].modes, {'a.desktop': 'improve'})
        A.set_mode('a.desktop', 'fix')
        self.assertEqual(json.loads(path.read_text()), {'v': 1, 'disabled': [], 'added': []})  # no empty "modes"
        self.assertEqual(os.listdir(path.parent), ['apps.json'])
        # the lists beside it stay as they are
        A.set_enabled('x.desktop', False)
        A.set_mode('y.desktop', 'casual')
        A.set_enabled('x.desktop', True)
        self.assertEqual(A.read_config(), (A.Config(modes={'y.desktop': 'casual'}), None))

    def test_added_apps_come_and_go(self):
        A.add('obsidian.desktop')
        A.add('obsidian.desktop')
        A.add(A.SELF)
        A.set_enabled('obsidian.desktop', False)
        self.assertEqual(A.read_config(), (A.Config(['obsidian.desktop'], ['obsidian.desktop']), None))
        # review finding: removing takes the app out of "added" only, it never switches GramaDrama on
        # there (and a switched-off row has no remove button, see ListTests)
        A.remove('obsidian.desktop')
        self.assertEqual(A.read_config(), (A.Config(['obsidian.desktop']), None))
        A.set_enabled('obsidian.desktop', True)
        self.assertEqual(A.read_config(), (A.Config(), None))

    def test_a_broken_file_is_repaired_and_kept(self):
        path = write(paths.apps_path(), '{"v": 1, "disabled": "oops"}')
        error, backup = A.set_enabled('a.desktop', False)
        self.assertIsNone(error)
        self.assertEqual(backup, path.with_name('apps.json.bak'))
        self.assertEqual(backup.read_text(), '{"v": 1, "disabled": "oops"}')  # kept for the user
        self.assertEqual(A.read_config(), (A.Config(['a.desktop']), None))  # the change alone, as the engine saw it
        self.assertEqual(A.set_enabled('b.desktop', False), (None, None))  # a good file: no backup

    def test_a_failed_save_keeps_the_old_file(self):
        path = write(paths.apps_path(), 'broken')
        with mock.patch.object(A, '_write', side_effect=OSError(28, 'No space left on device')):
            error, backup = A.set_enabled('a.desktop', False)
        self.assertEqual(error, 'Nie udało się zapisać listy aplikacji: No space left on device')
        self.assertIsNone(backup)
        self.assertEqual(path.read_text(), 'broken')
        self.assertFalse(path.with_name('apps.json.bak').exists())

    def test_the_note_about_a_broken_file(self):
        self.assertIsNone(A.config_check(None))
        check = A.config_check('/x/apps.json nie jest poprawnym plikiem JSON')
        self.assertEqual(check.state, C.WARNING)
        self.assertIn('/x/apps.json nie jest poprawnym plikiem JSON', check.text)
        self.assertIn('apps.json.bak', check.text)


class CatalogueTests(unittest.TestCase):
    def test_the_table_of_the_spec(self):
        table = {A.CHROMIUM: ['google-chrome.desktop', 'chromium_chromium.desktop', 'chatgpt.desktop',
                              'com.anthropic.Claude.desktop', 'slack.desktop', 'discord.desktop', 'code.desktop',
                              'obsidian.desktop', 'signal-desktop.desktop'],
                 A.GTK3: ['firefox_firefox.desktop', 'firefox.desktop', 'thunderbird.desktop',
                          'thunderbird_thunderbird.desktop', 'org.gnome.Evolution.desktop', 'org.gnome.Geary.desktop'],
                 A.GTK4: ['org.gnome.TextEditor.desktop', 'org.gnome.Notes.desktop'],
                 A.TERMINAL: ['org.gnome.Ptyxis.desktop', 'org.gnome.Terminal.desktop', 'org.gnome.Console.desktop'],
                 A.OFFICE: ['libreoffice-writer.desktop']}
        for kind, ids in table.items():
            for app_id in ids:
                self.assertEqual(A.catalogue_kind(app_id), kind, app_id)
        for unknown in ('org.gnome.Calculator.desktop', 'window:xterm', A.SELF, 'my-thunderbird.desktop', ''):
            self.assertIsNone(A.catalogue_kind(unknown), unknown)
        self.assertTrue(all(key.endswith('.desktop') for key in A.APP_CATALOGUE))


class ExecTests(unittest.TestCase):
    COMMANDS = ['a b', '"a b" c', "'a b' c", 'a\\ b', '"a\\"b"', '"a\\\\b"', '"a\\$b"', '"a\\`b"', '"a\\xb"',
                'a"b c"d', "''", 'a #b\nc', 'a#b', 'a "#b"', 'a\tb\nc', '"a\\\nb"', 'a\\\nb', 'env A=1 /x %U',
                '/opt/My\\ App/app --flag="x y" %U', 'claude-desktop "claude://code/new?source=desktop_action"']

    def test_arguments_as_glib_reads_them(self):
        from gi.repository import GLib
        for command in self.COMMANDS:
            self.assertEqual(A.exec_argv(command), GLib.shell_parse_argv(command)[1], command)
        for command in ('a\\', '"a', "'a", '"a\\'):
            self.assertIsNone(A.exec_argv(command), command)
            with self.assertRaises(GLib.Error):
                GLib.shell_parse_argv(command)
        self.assertEqual(A.exec_argv('  '), [])
        self.assertEqual(A.split_exec('ab "c d" %U'), [('ab', 2), ('c d', 8), ('%U', 11)])

    def test_key_file_escapes_as_glib_reads_them(self):
        from gi.repository import GLib
        for raw in ('x\\sy', '\\sx', 'a\\\\b', 'tab\\there', 'plain', 'a\\nb\\rc'):
            key_file, text = GLib.KeyFile(), '[Desktop Entry]\nK=%s\n' % raw
            key_file.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
            self.assertEqual(A.unescape(raw)[0], key_file.get_string('Desktop Entry', 'K'), raw)
        self.assertEqual(A.unescape('a\\sb'), ('a b', [0, 1, 3, 4]))
        self.assertEqual(A.unescape('a\\qb\\')[0], 'a\\qb\\')  # GLib refuses these; the backslash stays

    def test_the_program_comes_after_env(self):
        self.assertEqual(A.program_index(['chatgpt', '%U']), 0)
        self.assertEqual(A.program_index(['env', 'BAMF_DESKTOP_FILE_HINT=/x.desktop', 'A_1=', '/snap/bin/chromium']), 3)
        self.assertEqual(A.program_index(['/usr/bin/env', 'X=1', 'code']), 2)
        for argv in ([], ['env'], ['env', 'X=1'], ['env', '-u', 'X', 'app'], ['%U'], [''], ['-x']):
            self.assertIsNone(A.program_index(argv), argv)

    def test_the_flag_goes_right_after_the_program(self):
        hint = 'BAMF_DESKTOP_FILE_HINT=/var/lib/snapd/desktop/applications/chromium_chromium.desktop'
        cases = {'chatgpt %U': 'chatgpt --ozone-platform=wayland %U',
                 '/usr/bin/google-chrome-stable': '/usr/bin/google-chrome-stable --ozone-platform=wayland',
                 'env %s /snap/bin/chromium %%U' % hint: 'env %s /snap/bin/chromium --ozone-platform=wayland %%U' % hint,
                 '"/opt/My App/app" --x': '"/opt/My App/app" --ozone-platform=wayland --x',
                 '\\s/opt/app %U': '\\s/opt/app --ozone-platform=wayland %U',  # an escape before the cut
                 '/opt/My\\sApp/app': '/opt/My --ozone-platform=wayland\\sApp/app',  # \s is a space: two words, as for GLib
                 'app\t--x': 'app --ozone-platform=wayland\t--x',
                 'app --ozone-platform=wayland %U': 'app --ozone-platform=wayland %U',  # already there
                 '"open': '"open', '': '', 'env -i app': 'env -i app'}
        for raw, expected in cases.items():
            self.assertEqual(A.with_flag(raw), expected, raw)


class DesktopTests(TempHome):
    def test_the_first_file_of_an_id_counts(self):
        other = self.root / 'other'
        write(self.data / 'applications' / 'a.desktop', desktop('Mine'))
        write(self.system / 'applications' / 'a.desktop', desktop('System'))
        write(self.system / 'applications' / 'b.desktop', desktop('B system'))
        write(other / 'applications' / 'b.desktop', desktop('B other'))
        write(self.system / 'applications' / 'kde4' / 'c.desktop', desktop('C'))
        write(self.system / 'applications' / 'notes.txt', 'x')
        entries = A.installed_apps([self.data, self.system, other], names=[])
        self.assertEqual(sorted(entries), ['a.desktop', 'b.desktop', 'kde4-c.desktop'])
        self.assertEqual((entries['a.desktop'].name, entries['b.desktop'].name), ('Mine', 'B system'))
        self.assertEqual(entries['kde4-c.desktop'].path, self.system / 'applications' / 'kde4' / 'c.desktop')
        with mock.patch.dict(os.environ, {'XDG_DATA_DIRS': '%s:relative:%s/:%s' % (self.system, other, self.data)}):
            self.assertEqual(A.data_dirs(), [self.data, self.system, other])
        os.environ.pop('XDG_DATA_DIRS')
        self.assertEqual(A.data_dirs(), [self.data, Path('/usr/local/share'), Path('/usr/share')])

    def test_what_hides_an_app(self):
        apps = self.system / 'applications'
        write(self.data / 'applications' / 'deleted.desktop', desktop('Deleted', more='Hidden=true\n'))
        write(apps / 'deleted.desktop', desktop('Deleted'))  # a user's Hidden hides the system's file too
        write(apps / 'quiet.desktop', desktop('Quiet', more='NoDisplay=true\n'))
        write(apps / 'gone.desktop', desktop('Gone', more='TryExec=/nonexistent/program\n'))
        write(apps / 'there.desktop', desktop('There', more='TryExec=tool\n'))
        write(apps / 'link.desktop', '[Desktop Entry]\nType=Link\nName=Link\nURL=https://example.com\n')
        write(apps / 'empty.desktop', '# nothing\n')
        program(self.bin / 'tool')
        entries = self.installed()
        self.assertEqual(sorted(entries), ['quiet.desktop', 'there.desktop'])  # NoDisplay: the log may name it

    def test_name_icon_and_exec(self):
        write(self.system / 'applications' / 'a.desktop',
              '# comment\n[Desktop Entry]\nType = Application\nName=Text Editor\nName[pl]=Edytor\\stekstu\n'
              'Name[de]=Texteditor\nIcon=org.gnome.TextEditor\nExec=gnome-text-editor --new-window %U\n'
              'X-Flatpak=org.gnome.TextEditor\n\n[Desktop Action new]\nName=Nowe okno\nExec=other\n')
        entry = A.installed_apps([self.data, self.system], names=['pl_PL', 'pl'])['a.desktop']
        self.assertEqual((entry.name, entry.icon, entry.exec), ('Edytor tekstu', 'org.gnome.TextEditor',
                                                               'gnome-text-editor --new-window %U'))
        self.assertTrue(entry.flatpak)
        self.assertEqual(A.installed_apps([self.data, self.system], names=[])['a.desktop'].name, 'Text Editor')
        write(self.system / 'applications' / 'nameless.desktop', '[Desktop Entry]\nType=Application\nExec=x\n')
        self.assertEqual(A.installed_apps([self.data, self.system], names=[])['nameless.desktop'].name, 'nameless')

    def test_names_from_the_gettext_domain(self):
        # Ubuntu keeps the translations of its apps' names in gettext catalogues, as GLib reads them
        locale = self.root / 'locale'
        write_mo(locale / 'pl' / 'LC_MESSAGES' / 'gnome-text-editor.mo', {'Text Editor': 'Edytor tekstu'})
        write(self.system / 'applications' / 'org.gnome.TextEditor.desktop',
              desktop('Text Editor', more='X-Ubuntu-Gettext-Domain=gnome-text-editor\n'))
        with mock.patch.object(A, 'LOCALE_DIR', str(locale)):
            for names, name in ((['pl_PL', 'pl'], 'Edytor tekstu'), (['de'], 'Text Editor'), ([], 'Text Editor')):
                entries = A.installed_apps([self.data, self.system], names=names)
                self.assertEqual(entries['org.gnome.TextEditor.desktop'].name, name, names)
        # review finding: any other app's broken catalogue keeps its untranslated name, and nothing else
        # changes (Python's gettext raises LookupError or IndexError for these headers)
        for i, header in enumerate(('Content-Type: text/plain; charset=CHARSET\n', 'Content-Type: text/plain\n',
                                    'Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2;\n')):
            write_mo(locale / 'pl' / 'LC_MESSAGES' / ('broken%d.mo' % i), {'Broken': 'Zepsuta'}, header)
            write(self.system / 'applications' / ('broken%d.desktop' % i),
                  desktop('Broken', more='X-GNOME-Gettext-Domain=broken%d\n' % i))
        with mock.patch.object(A, 'LOCALE_DIR', str(locale)):
            entries = A.installed_apps([self.data, self.system], names=['pl'])
        self.assertEqual([entries['broken%d.desktop' % i].name for i in range(3)], ['Broken'] * 3)
        self.assertEqual(entries['org.gnome.TextEditor.desktop'].name, 'Edytor tekstu')

    def test_a_nul_byte_ends_a_value_as_for_glib(self):
        from gi.repository import GLib
        text = '[Desktop Entry]\nType=Application\nName=Odd\x00 app\nExec=odd\x00program %U\n'
        path = write(self.system / 'applications' / 'odd.desktop', text)
        key_file = GLib.KeyFile()
        key_file.load_from_file(str(path), GLib.KeyFileFlags.NONE)
        entry = self.installed()['odd.desktop']
        self.assertEqual((entry.name, entry.exec), (key_file.get_string('Desktop Entry', 'Name'),
                                                    key_file.get_string('Desktop Entry', 'Exec')))
        self.assertEqual(entry.exec, 'odd')
        self.assertFalse(A.is_ours(self.root / 'a\x00b'))  # a name no file can have: never ours, no error

    def test_languages_in_glib_order(self):
        self.assertEqual(A.languages({'LANG': 'pl_PL.UTF-8'}), ['pl_PL', 'pl'])
        self.assertEqual(A.languages({'LANGUAGE': 'pl:en_GB', 'LANG': 'de_DE.UTF-8'}), ['pl', 'en_GB', 'en'])
        self.assertEqual(A.languages({'LC_ALL': 'sr_RS.UTF-8@latin', 'LANG': 'pl_PL.UTF-8'}),
                         ['sr_RS@latin', 'sr_RS', 'sr@latin', 'sr'])
        self.assertEqual(A.languages({'LANG': 'C.UTF-8'}), [])
        self.assertEqual(A.languages({}), [])

    def test_our_override_stands_aside(self):
        original = write(self.system / 'applications' / 'slack.desktop', desktop('Slack', '/usr/bin/slack %U'))
        ours = write(self.data / 'applications' / 'slack.desktop',
                     A.override_text(original.read_text(), 'Slack', original))
        mine = write(self.data / 'applications' / 'mine.desktop', desktop('Mine'))
        write(self.system / 'applications' / 'mine.desktop', desktop('System'))
        entries = self.installed()
        self.assertEqual((entries['slack.desktop'].path, entries['slack.desktop'].override), (original, ours))
        self.assertEqual(entries['slack.desktop'].exec, '/usr/bin/slack %U')  # the original's
        self.assertEqual((entries['mine.desktop'].path, entries['mine.desktop'].override), (mine, None))
        original.unlink()  # the app is gone, our copy is left
        entry = self.installed()['slack.desktop']
        self.assertEqual((entry.path, entry.override, entry.name), (None, ours, 'Slack'))


class DetectionTests(TempHome):
    def entry(self, exec_line, **more):
        return A.Entry('app.desktop', self.system / 'applications' / 'app.desktop', 'App', exec=exec_line, **more)

    def test_a_bare_name_is_looked_up_without_local_bin(self):
        launcher = program(self.root / 'usr' / 'lib' / 'chatgpt' / 'codex-launcher', 'resources.pak')
        (self.bin / 'chatgpt').symlink_to(launcher)
        program(self.local_bin / 'chatgpt')  # our wrapper must not hide the program
        detection = A.detect(self.entry('chatgpt %U'), self.places)
        self.assertEqual((detection.program, detection.found), ('chatgpt', self.bin / 'chatgpt'))
        self.assertEqual(detection.directory, launcher.parent)  # the realpath's directory holds resources.pak
        self.assertTrue(detection.chromium)
        self.assertEqual(A.which('chatgpt', self.places.path), self.local_bin / 'chatgpt')  # a plain lookup
        dirs = ['relative', str(self.local_bin) + '/', str(self.bin)]
        self.assertEqual(A.which('chatgpt', dirs, skip=self.local_bin), self.bin / 'chatgpt')
        self.assertIsNone(A.which('chatgpt', ['relative'], skip=self.local_bin))

    def test_what_counts_as_chromium(self):
        cases = [(('resources.pak',), True), (('chrome_100_percent.pak', 'icudtl.dat'), True),
                 (('chrome_100_percent.pak',), False), (('icudtl.dat',), False), ((), False)]
        for i, (files, chromium) in enumerate(cases):
            app = program(self.root / 'opt' / str(i) / 'app', *files)
            self.assertEqual(A.detect(self.entry('%s %%U' % app), self.places).chromium, chromium, files)

    def test_the_program_must_be_there(self):
        plain = write(self.root / 'opt' / 'x' / 'app', '')  # not executable
        self.assertEqual(A.detect(self.entry(str(plain)), self.places), A.Detection(str(plain)))
        self.assertEqual(A.detect(self.entry('missing %U'), self.places), A.Detection('missing'))
        self.assertEqual(A.detect(self.entry('"open'), self.places), A.Detection())
        self.assertEqual(A.detect(self.entry('bin/app'), self.places), A.Detection('bin/app'))  # relative

    def test_snaps_keep_their_files_in_the_snap(self):
        snap = self.places.snap
        program(self.bin / 'snap')
        for name in ('chromium', 'deep', 'foo.bar'):
            (snap / 'bin').mkdir(parents=True, exist_ok=True)
            (snap / 'bin' / name).symlink_to(self.bin / 'snap')
        write(snap / 'chromium' / '1234' / 'usr' / 'lib' / 'chromium-browser' / 'resources.pak')  # 3 levels down
        (snap / 'chromium' / 'current').symlink_to('1234')
        write(snap / 'deep' / '1' / 'a' / 'b' / 'c' / 'd' / 'resources.pak')  # 4 levels: too deep
        (snap / 'deep' / 'current').symlink_to('1')
        write(snap / 'foo' / '7' / 'resources.pak')
        (snap / 'foo' / 'current').symlink_to('7')
        hint = 'BAMF_DESKTOP_FILE_HINT=/var/lib/snapd/desktop/applications/chromium_chromium.desktop'
        chromium = A.detect(self.entry('env %s %s %%U' % (hint, snap / 'bin' / 'chromium')), self.places)
        self.assertEqual(chromium.program, str(snap / 'bin' / 'chromium'))
        self.assertEqual(chromium.directory, snap / 'chromium' / '1234' / 'usr' / 'lib' / 'chromium-browser')
        self.assertFalse(A.detect(self.entry(str(snap / 'bin' / 'deep')), self.places).chromium)
        places = dataclasses.replace(self.places, path=(str(snap / 'bin'),))
        foo = A.detect(self.entry('foo.bar'), places)  # a bare name found in /snap/bin; the snap is foo
        self.assertEqual(foo.directory, snap / 'foo' / '7')

    def test_flatpak_keeps_its_files_in_the_sandbox(self):
        run = A.detect(self.entry('/usr/bin/flatpak run --branch=stable --command=obsidian md.obsidian.Obsidian '
                                  '@@u %U @@'), self.places)
        self.assertEqual((run.program, run.flatpak, run.found, run.chromium), ('/usr/bin/flatpak', True, None, False))
        self.assertTrue(A.detect(self.entry('obsidian %U', flatpak=True), self.places).flatpak)  # X-Flatpak
        self.assertFalse(A.detect(self.entry('flatpak-helper run'), self.places).flatpak)

    def test_the_kind(self):
        chromium = A.Detection('x', Path('/x'), Path('/opt/x'))
        self.assertEqual(A.kind_of('org.gnome.Ptyxis.desktop', chromium), A.TERMINAL)  # the catalogue first
        self.assertEqual(A.kind_of('unknown.desktop', chromium), A.CHROMIUM)
        self.assertEqual(A.kind_of('slack.desktop', None), A.CHROMIUM)
        self.assertIsNone(A.kind_of('unknown.desktop', A.Detection('x')))
        self.assertIsNone(A.kind_of('window:xterm', None))


class ProcessTests(TempHome):
    def test_gpu_processes_tell_how_an_app_runs(self):
        proc = self.root / 'proc'
        chat = program(self.root / 'opt' / 'chat' / 'ChatGPT', 'resources.pak')
        chrome = program(self.root / 'opt' / 'chrome' / 'chrome', 'resources.pak')
        claude = program(self.root / 'opt' / 'claude' / 'claude', 'resources.pak')
        updated = program(self.root / 'opt' / 'updated' / 'app', 'resources.pak')
        fake_process(proc, 10, [str(chat), '--type=gpu-process', '--ozone-platform=wayland'], chat)
        # Chromium rewrites its process title: one string with the arguments split by spaces
        title = '%s --type=gpu-process --ozone-platform=x11 --render-node-override=/dev/dri/renderD128' % chrome
        fake_process(proc, 11, title.encode() + b'\0', chrome)
        fake_process(proc, 12, [str(chrome), '--type=gpu-process', '--ozone-platform=wayland'], chrome)  # X11 wins
        fake_process(proc, 13, [str(claude), '--type=renderer', '--ozone-platform=x11'], claude)
        fake_process(proc, 14, [str(claude), '--type=gpu-process'], claude)  # no platform: unknown
        fake_process(proc, 15, ['/bin/bash', '-c', 'x --type=gpu-process --ozone-platform=x11'], '/usr/bin/bash')
        fake_process(proc, 16, [str(updated), '--type=gpu-process', '--ozone-platform=wayland'], '%s (deleted)' % updated)
        fake_process(proc, 17, [str(claude), '--type=gpu-process', '--ozone-platform=x11'])  # its program cannot be read
        (proc / 'self').mkdir()
        self.assertEqual(A.gpu_platforms(proc), {chat.parent: 'wayland', chrome.parent: 'x11', updated.parent: 'wayland'})
        self.assertEqual(A.gpu_platforms(self.root / 'nowhere'), {})

    def test_gnome_shell_path(self):
        proc = self.root / 'proc'
        fake_process(proc, 30, ['/usr/bin/gnome-shell'], comm='gnome-shell', environ={'PATH': '/late:/usr/bin'},
                     started=500)
        fake_process(proc, 20, ['/usr/bin/gnome-shell', '--mode=ubuntu'], comm='gnome-shell',
                     environ={'HOME': '/h', 'PATH': '/home/u/.local/bin:/usr/bin'}, started=100)
        fake_process(proc, 25, ['/usr/libexec/gnome-shell-calendar-server'], comm='gnome-shell-cal',
                     environ={'PATH': '/wrong'}, started=50)
        self.assertEqual(A.shell_path(proc, fallback='/fallback'), '/home/u/.local/bin:/usr/bin')  # the oldest
        with mock.patch.object(os, 'getppid', return_value=30):
            self.assertEqual(A.shell_path(proc, fallback='/fallback'), '/late:/usr/bin')  # the app's parent first
        (proc / '20' / 'environ').write_bytes(b'HOME=/h\0')  # no PATH there: the next one
        self.assertEqual(A.shell_path(proc, fallback='/fallback'), '/late:/usr/bin')
        self.assertEqual(A.shell_path(self.root / 'nowhere', fallback='/fallback'), '/fallback')
        with mock.patch.dict(os.environ, {'PATH': '/mine'}):
            self.assertEqual(A.shell_path(self.root / 'nowhere'), '/mine')

    def test_when_gnome_shell_started(self):
        proc, tick = self.root / 'proc', os.sysconf('SC_CLK_TCK')
        (proc / 'stat').parent.mkdir(parents=True)
        (proc / 'stat').write_text('cpu  10 0 5 100\nbtime 1700000000\nprocesses 99\n')
        self.assertIsNone(A.shell_started(proc))  # no GNOME Shell of ours
        fake_process(proc, 30, ['/usr/bin/gnome-shell'], comm='gnome-shell', started=50 * tick)
        fake_process(proc, 20, ['/usr/bin/gnome-shell'], comm='gnome-shell', started=10 * tick)
        fake_process(proc, 25, ['/usr/libexec/gnome-shell-calendar-server'], comm='gnome-shell-cal', started=1)
        self.assertEqual(A.shell_started(proc), 1700000010)  # the oldest, as for shell_path
        with mock.patch.object(os, 'getppid', return_value=30):
            self.assertEqual(A.shell_started(proc), 1700000050)  # the app's parent first
        (proc / 'stat').write_text('cpu  10 0 5 100\n')
        self.assertIsNone(A.shell_started(proc))

    def test_the_gnome_shell_on_our_session_bus(self):
        # another GNOME Shell of the user (a nested or a test session) is on another bus
        proc, tick = self.root / 'proc', os.sysconf('SC_CLK_TCK')
        (proc / 'stat').parent.mkdir(parents=True)
        (proc / 'stat').write_text('btime 1700000000\n')
        fake_process(proc, 20, ['/usr/bin/gnome-shell'], comm='gnome-shell', started=10 * tick,
                     environ={'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus', 'PATH': '/desktop'})
        fake_process(proc, 30, ['/usr/bin/gnome-shell'], comm='gnome-shell', started=50 * tick,
                     environ={'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/gd-x/bus', 'PATH': '/test'})
        with mock.patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/gd-x/bus'}):
            self.assertEqual((A.shell_path(proc), A.shell_started(proc)), ('/test', 1700000050))
            with mock.patch.object(os, 'getppid', return_value=20):  # the app's parent still comes first
                self.assertEqual((A.shell_path(proc), A.shell_started(proc)), ('/desktop', 1700000010))
        with mock.patch.dict(os.environ):
            os.environ.pop('DBUS_SESSION_BUS_ADDRESS', None)
            self.assertEqual((A.shell_path(proc), A.shell_started(proc)), ('/desktop', 1700000010))  # the oldest
        (proc / '20' / 'environ').chmod(0)  # unreadable: it can still be the oldest, but has no PATH to give
        if not os.access(proc / '20' / 'environ', os.R_OK):
            with mock.patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/elsewhere'}):
                self.assertEqual((A.shell_path(proc), A.shell_started(proc)), ('/test', 1700000010))

    def test_places(self):
        self.assertEqual(A.find_places(path='/a:/b'), A.Places(('/a', '/b'), self.local_bin, self.data / 'applications'))
        with mock.patch.object(A, 'shell_path', return_value='/c') as shell_path:
            self.assertEqual(A.find_places('/p').path, ('/c',))
        shell_path.assert_called_once_with('/p')


class EvidenceTests(unittest.TestCase):
    def test_corrections_and_refusals_per_app(self):
        t = at(14, 1)
        chrome, chat, ptyxis = 'google-chrome.desktop', 'chatgpt.desktop', 'org.gnome.Ptyxis.desktop'
        events = [ev('refused', t, at='hotkey', reason='no_text', app=chat, win='x11'),
                  ev('start', t + 1, id=1, app=chrome, win='wayland', scope='paragraph', chars=12, mode='fix'),
                  ev('apply', t + 2, id=1, app=chrome, win='wayland', scope='paragraph'),
                  ev('verified', t + 3, id=1, outcome='ok', app=chrome, win='wayland'),
                  ev('start', t + 4, id=2, app=chrome, win='wayland', scope='paragraph', chars=5, mode='fix'),
                  ev('verified', t + 5, id=2, outcome='mismatch', app=chrome, win='wayland'),
                  ev('refused', t + 6, at='hotkey', reason='terminal', app=ptyxis, win='wayland')]
        found = A.log_evidence(events)
        self.assertEqual(sorted(found), [chat, chrome, ptyxis])
        self.assertEqual(found[chat], A.Evidence(seen=True, refusal='no_text', refusal_win='x11', x11_no_text=True))
        self.assertEqual((found[chrome].ok_count, found[chrome].ok_at, found[chrome].last_ok), (1, t + 3, False))
        self.assertEqual((found[ptyxis].refusal, found[ptyxis].refusal_win), ('terminal', 'wayland'))

    def test_a_capture_and_a_verified_correction_outdate_refusals(self):
        app = 'chatgpt.desktop'
        events = [ev('refused', 1, at='hotkey', reason='no_text', app=app, win='x11')]
        self.assertTrue(A.log_evidence(events)[app].x11_no_text)
        events.append(ev('start', 2, id=7, app=app, win='wayland'))
        evidence = A.log_evidence(events)[app]
        self.assertEqual((evidence.x11_no_text, evidence.refusal, evidence.last_ok), (False, 'no_text', False))
        events.append(ev('verified', 3, id=7, outcome='ok', app=app, win='wayland'))
        self.assertEqual(A.log_evidence(events)[app].last_ok, True)
        events.append(ev('refused', 4, at='hotkey', reason=A.APP_OFF, app=app, win='wayland'))
        evidence = A.log_evidence(events)[app]
        self.assertEqual((evidence.last_ok, evidence.refusal), (True, 'no_text'))  # off there: says nothing
        events.append(ev('refused', 5, at='hotkey', reason='no_text', app=app, win='x11'))
        evidence = A.log_evidence(events)[app]
        self.assertEqual((evidence.last_ok, evidence.x11_no_text), (False, True))  # X11 again

    def test_a_later_refusal_on_wayland_outdates_one_on_x11(self):
        # review finding: section 7 row 4 asks for the last refusal to be no_text on X11; an app that
        # refused on Wayland since (updated, or started with the flag by hand) no longer runs on X11
        app = 'chatgpt.desktop'
        for reason in ('no_text', 'busy'):
            events = [ev('refused', 1, at='hotkey', reason='no_text', app=app, win='x11'),
                      ev('refused', 2, at='hotkey', reason=reason, app=app, win='wayland')]
            evidence = A.log_evidence(events)[app]
            self.assertEqual((evidence.refusal_win, evidence.x11_no_text), ('wayland', False), reason)
            self.assertEqual(A.row_state('ChatGPT', True, A.CHROMIUM, None, None, StateTests.PLAN, evidence).state,
                             A.UNCONFIRMED, reason)
        # another refusal on X11 keeps it: the app still runs through X11
        events = [ev('refused', 1, at='hotkey', reason='no_text', app=app, win='x11'),
                  ev('refused', 2, at='hotkey', reason='busy', app=app, win='x11')]
        self.assertTrue(A.log_evidence(events)[app].x11_no_text)
        events.append(ev('refused', 3, at='hotkey', reason=A.APP_OFF, app=app, win='wayland'))  # says nothing
        self.assertTrue(A.log_evidence(events)[app].x11_no_text)

    def test_a_verified_event_without_the_app_follows_its_session(self):
        events = [ev('start', 1, id=3, app='slack.desktop', win='wayland'),
                  ev('start', 2, id=4, app='code.desktop', win='wayland'),
                  ev('verified', 3, id=3, outcome='ok'), ev('verified', 4, id=9, outcome='ok')]
        found = A.log_evidence(events)
        self.assertEqual((found['slack.desktop'].ok_count, found['slack.desktop'].last_ok), (1, True))
        self.assertEqual(found['code.desktop'].ok_count, 0)

    def test_what_is_about_no_app(self):
        events = [ev('start', 1, id=1, scope='paragraph'),  # an engine without the focus: no app
                  ev('refused', 2, at='hotkey', reason='no_text', app='', win='none'),  # no focus window
                  ev('refused', 3, at='apply', reason='changed', app='a.desktop', win='wayland'),
                  ev('result', 4, id=1, app='b.desktop'), ev('refused', 5, at='hotkey', reason='stale', app=None),
                  ev('start', 6, id=2, app=5)]
        self.assertEqual(A.log_evidence(events), {})
        self.assertEqual(A.log_evidence(C.parse_log(b'\n'.join(json.dumps(e).encode() for e in events))), {})

    def test_odd_values_in_the_log_break_nothing(self):
        events = [ev('start', 'soon', id=[1], app='a.desktop'),
                  ev('verified', True, id={'x': 1}, outcome='ok', app='a.desktop'),
                  ev('refused', 'later', at='hotkey', reason='no_text', app='b.desktop', win='x11')]
        found = A.log_evidence(events)
        self.assertEqual((found['a.desktop'].ok_count, found['a.desktop'].ok_at), (1, None))
        self.assertEqual((found['b.desktop'].refusal, found['b.desktop'].x11_no_text), ('no_text', True))
        self.assertEqual(A.row_state('A', True, None, None, None, None, found['a.desktop']).text, 'Działa · 1 poprawka')


class FixTests(TempHome):
    SLACK = ('[Desktop Entry]\nName=Slack\nComment=Slack Desktop\nExec={slack} %U\nIcon=slack\nType=Application\n'
             'TryExec={slack}\nActions=new;quiet;\n\n[Desktop Action new]\nName=New Window\n'
             'Exec = {slack} --new-window\n\n[Desktop Action quiet]\nName=Quiet\nExec=env SLACK_QUIET=1 {slack} --quiet\r\n')

    def chat_entry(self, exec_line='chatgpt %U'):
        return A.Entry('chatgpt.desktop', self.system / 'applications' / 'chatgpt.desktop', 'ChatGPT', exec=exec_line)

    def chatgpt(self, link=None, lib='chatgpt'):
        """ChatGPT as its package installs it: the program in usr/lib/<lib> next to Chromium's
        resources, and the name in PATH (link, default usr/bin/chatgpt) a link to it."""
        link = link or self.bin / 'chatgpt'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(program(self.root / 'usr' / 'lib' / lib / 'chatgpt', 'resources.pak'))
        return link

    def plan(self, entry, places=None, kind=A.CHROMIUM):
        places = places or self.places
        return A.plan_fix(entry.id, entry, A.detect(entry, places), kind, places)

    def test_the_wrapper_is_like_the_first_one(self):
        self.assertEqual(A.wrapper_text('ChatGPT', '/usr/bin/chatgpt'),
                         '#!/bin/sh\n'
                         '# GramaDrama: run ChatGPT natively on Wayland, so IBus engines get the field text.\n'
                         '# Delete this file to undo.\n'
                         'exec /usr/bin/chatgpt --ozone-platform=wayland "$@"\n')

    def test_a_wrapper_for_a_bare_name(self):
        target = self.chatgpt()
        entry, wrapper = self.chat_entry(), self.local_bin / 'chatgpt'
        plan = self.plan(entry)
        self.assertEqual(plan, A.Plan(A.WRAPPER, wrapper, A.wrapper_text('ChatGPT', target)))
        self.assertIsNone(A.fix_status(entry, A.detect(entry, self.places), self.places))
        self.assertIsNone(A.apply_fix(plan))
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertEqual(os.listdir(self.local_bin), ['chatgpt'])  # no temp file left behind
        self.assertEqual(A.fix_status(entry, A.detect(entry, self.places), self.places), A.Fix(A.WRAPPER, wrapper))
        self.assertEqual(A.wrapper_target(wrapper), target)
        self.assertEqual(self.plan(entry), plan)  # ours: it may be written again
        self.assertIsNone(A.undo_fix(A.Fix(A.WRAPPER, wrapper)))
        self.assertFalse(wrapper.exists())
        self.assertIsNone(A.undo_fix(A.Fix(A.WRAPPER, wrapper)))  # already gone: nothing to do

    def test_the_wrapper_passes_every_argument(self):
        out = self.root / 'argv'
        target = write(self.root / 'opt' / "it's here" / 'app',
                       '#!/bin/sh\nfor a in "$@"; do printf "%%s\\n" "$a"; done > %s\n' % shlex.quote(str(out)), 0o755)
        wrapper = write(self.local_bin / 'app', A.wrapper_text('App\nX', target), 0o755)
        self.assertIn('# GramaDrama: run App X natively', wrapper.read_text())  # one comment line
        subprocess.run([str(wrapper), 'a b', '$HOME', '--x'], check=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(out.read_text().split('\n')[:-1], [A.FLAG, 'a b', '$HOME', '--x'])
        self.assertEqual(A.wrapper_target(wrapper), target)

    def test_an_override_for_an_absolute_exec(self):
        from gi.repository import GLib
        slack = program(self.root / 'usr' / 'lib' / 'slack' / 'slack', 'resources.pak')
        original = write(self.system / 'applications' / 'slack.desktop', self.SLACK.format(slack=slack))
        entry = self.installed()['slack.desktop']
        detection = A.detect(entry, self.places)
        plan = A.plan_fix('slack.desktop', entry, detection, A.kind_of('slack.desktop', detection), self.places)
        override = self.data / 'applications' / 'slack.desktop'
        self.assertEqual((plan.kind, plan.path), (A.OVERRIDE, override))
        self.assertEqual(plan.text, (
            '# GramaDrama: run Slack natively on Wayland, so IBus engines get the field text.\n'
            '# Delete this file to undo. It is a copy of {original} with --ozone-platform=wayland.\n'
            '[Desktop Entry]\nName=Slack\nComment=Slack Desktop\nExec={slack} --ozone-platform=wayland %U\nIcon=slack\n'
            'Type=Application\nTryExec={slack}\nActions=new;quiet;\n\n[Desktop Action new]\nName=New Window\n'
            'Exec = {slack} --ozone-platform=wayland --new-window\n\n[Desktop Action quiet]\nName=Quiet\n'
            'Exec=env SLACK_QUIET=1 {slack} --ozone-platform=wayland --quiet\r\n').format(original=original, slack=slack))
        self.assertIsNone(A.apply_fix(plan))
        self.assertEqual(stat.S_IMODE(override.stat().st_mode), 0o644)
        self.assertTrue(A.is_ours(override))
        key_file = GLib.KeyFile()  # GLib reads the copy, and every Exec starts the program with the flag
        key_file.load_from_file(str(override), GLib.KeyFileFlags.NONE)
        for group in ('Desktop Entry', 'Desktop Action new', 'Desktop Action quiet'):
            argv = GLib.shell_parse_argv(key_file.get_string(group, 'Exec'))[1]
            self.assertEqual(argv[argv.index(str(slack)) + 1], A.FLAG, group)
        entry = self.installed()['slack.desktop']  # the copy stands aside for the original
        self.assertEqual((entry.path, entry.override), (original, override))
        detection = A.detect(entry, self.places)
        self.assertEqual(A.fix_status(entry, detection, self.places), A.Fix(A.OVERRIDE, override))
        # a package update changes the original: the copy is out of date, and "Napraw" copies it again
        original.write_text(self.SLACK.format(slack=slack).replace('Icon=slack\n', 'Icon=slack\nKeywords=chat;\n'))
        stale = A.fix_status(entry, detection, self.places)
        self.assertEqual(stale, A.Fix(A.OVERRIDE, override, broken=True))
        self.assertIsNone(A.repair_fix(stale, A.plan_fix('slack.desktop', entry, detection, A.CHROMIUM, self.places)))
        self.assertIn('Keywords=chat;', override.read_text())
        self.assertFalse(A.fix_status(entry, detection, self.places).broken)
        # the app is removed: the copy is left over, and "Napraw" takes it away
        original.unlink()
        entry = self.installed()['slack.desktop']
        detection = A.detect(entry, self.places)
        fix = A.fix_status(entry, detection, self.places)
        self.assertEqual(fix, A.Fix(A.OVERRIDE, override, broken=True))
        self.assertIsNone(A.repair_fix(fix, A.plan_fix('slack.desktop', entry, detection, A.CHROMIUM, self.places)))
        self.assertFalse(override.exists())

    def test_an_override_counts_everywhere_after_the_next_login(self):
        # E1: the app grid keeps the command it read when GNOME Shell started, so an override written
        # later is pending until the next login; a wrapper never is (PATH is read at every start)
        slack = program(self.root / 'usr' / 'lib' / 'slack' / 'slack', 'resources.pak')
        write(self.system / 'applications' / 'slack.desktop', desktop('Slack', '%s %%U' % slack))
        entry = self.installed()['slack.desktop']
        self.assertIsNone(A.apply_fix(self.plan(entry)))
        override, now = self.data / 'applications' / 'slack.desktop', time.time()
        entry = self.installed()['slack.desktop']
        detection = A.detect(entry, self.places)
        self.assertEqual(A.fix_status(entry, detection, self.places, now - 600), A.Fix(A.OVERRIDE, override, pending=True))
        self.assertEqual(A.fix_status(entry, detection, self.places, now + 600), A.Fix(A.OVERRIDE, override))  # logged in since
        self.assertEqual(A.fix_status(entry, detection, self.places), A.Fix(A.OVERRIDE, override))  # unknown: not pending
        write(self.system / 'applications' / 'slack.desktop', desktop('Slack', '%s --x %%U' % slack))
        entry = self.installed()['slack.desktop']
        self.assertEqual(A.fix_status(entry, detection, self.places, now - 600),
                         A.Fix(A.OVERRIDE, override, broken=True, pending=True))  # "Napraw" comes first (row 2)
        target = self.chatgpt()
        chat = self.chat_entry()
        self.assertIsNone(A.apply_fix(self.plan(chat)))
        self.assertEqual(A.fix_status(chat, A.detect(chat, self.places), self.places, now - 600),
                         A.Fix(A.WRAPPER, self.local_bin / 'chatgpt'))
        self.assertEqual(A.wrapper_target(self.local_bin / 'chatgpt'), target)

    def test_an_override_of_a_file_with_a_byte_order_mark(self):
        from gi.repository import GLib
        text = A.override_text('\ufeff[Desktop Entry]\nType=Application\nName=X\nExec=/opt/x/x %U', 'X', '/a/x.desktop')
        self.assertNotIn('\ufeff', text)
        self.assertTrue(text.endswith('\nExec=/opt/x/x --ozone-platform=wayland %U\n'))
        key_file = GLib.KeyFile()
        key_file.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
        self.assertEqual(key_file.get_string('Desktop Entry', 'Name'), 'X')

    def test_an_override_when_local_bin_does_not_come_first(self):
        claude = program(self.root / 'usr' / 'lib' / 'claude-desktop' / 'claude-desktop', 'resources.pak')
        (self.bin / 'claude-desktop').symlink_to(claude)
        action = 'claude-desktop "claude://claude.ai/new?surface=chat&source=desktop_action"'
        write(self.system / 'applications' / 'com.anthropic.Claude.desktop',
              desktop('Claude', 'claude-desktop %U', 'Actions=new;\n[Desktop Action new]\nName=New\nExec=%s\n' % action))
        last = dataclasses.replace(self.places, path=(str(self.bin), str(self.local_bin)))
        entry = self.installed(last)['com.anthropic.Claude.desktop']
        plan = self.plan(entry, last)
        self.assertEqual((plan.kind, plan.path), (A.OVERRIDE, self.data / 'applications' / 'com.anthropic.Claude.desktop'))
        self.assertIn('\nExec=claude-desktop --ozone-platform=wayland %U\n', plan.text)
        self.assertIn('\nExec=claude-desktop --ozone-platform=wayland "claude://claude.ai/new?surface=chat&source='
                      'desktop_action"\n', plan.text)
        self.assertEqual(self.plan(entry, dataclasses.replace(self.places, path=(str(self.bin),))).kind, A.OVERRIDE)
        self.assertEqual(self.plan(entry).kind, A.WRAPPER)  # ~/.local/bin first

    def test_when_no_fix_is_possible(self):
        entry = A.Entry('a.desktop', self.system / 'applications' / 'a.desktop', 'A', exec='missing-program %U')
        self.assertEqual(self.plan(entry), A.Plan(reason='nie znaleziono programu missing-program'))
        for kind in (A.GTK3, A.GTK4, A.TERMINAL, A.OFFICE):
            self.assertEqual(self.plan(entry, kind=kind).reason, 'poprawka jest tylko dla aplikacji Chromium i Electron')
        self.assertEqual(self.plan(entry, kind=None).reason, 'nie rozpoznano aplikacji Chromium ani Electron')
        flatpak = dataclasses.replace(entry, flatpak=True)
        self.assertEqual(self.plan(flatpak), A.Plan(reason=A.FLATPAK_REASON))
        self.assertEqual(A.plan_fix('window:xterm', None, None, None, self.places).reason,
                         'nie rozpoznano aplikacji Chromium ani Electron')
        self.assertEqual(A.plan_fix('slack.desktop', None, None, A.CHROMIUM, self.places).reason,
                         'nie znaleziono jej pliku .desktop')
        self.assertEqual(self.plan(dataclasses.replace(entry, exec='"open')).reason,
                         'nie rozpoznano polecenia, które ją uruchamia')
        app = program(self.root / 'opt' / 'app' / 'app', 'resources.pak')
        unreadable = dataclasses.replace(entry, exec=str(app))  # its .desktop file vanished meanwhile
        self.assertEqual(self.plan(unreadable).reason, 'nie można odczytać %s' % entry.path)
        self.assertEqual(A.apply_fix(A.Plan(reason='nie znaleziono programu x')),
                         'Poprawka nie jest możliwa: nie znaleziono programu x.')

    def test_only_the_apps_own_chromium_program_gets_the_fix(self):
        """Review finding: the fix goes to the program Exec starts, so that program has to be Chromium
        or Electron itself, whatever the catalogue says: a wrapper ~/.local/bin/bash would break every
        bash started by name in the session, and the flag after firejail breaks the command."""
        for name in ('bash', 'firejail', 'python3'):
            program(self.bin / name)
        obsidian = self.system / 'applications' / 'obsidian.desktop'  # in the catalogue: CHROMIUM
        cases = {'bash -c "exec $HOME/Apps/Obsidian.AppImage"': 'bash', 'firejail obsidian': 'firejail',
                 '%s /opt/Obsidian/obsidian %%U' % (self.bin / 'firejail'): 'firejail',
                 'env A=1 python3 /opt/notes/main.py': 'python3'}
        for exec_line, name in cases.items():
            write(obsidian, desktop('Obsidian', exec_line))
            entry = self.installed()['obsidian.desktop']
            detection = A.detect(entry, self.places)
            self.assertEqual(A.kind_of(entry.id, detection), A.CHROMIUM)
            plan = A.plan_fix(entry.id, entry, detection, A.CHROMIUM, self.places)
            self.assertEqual(plan, A.Plan(reason='nie rozpoznano Chromium ani Electron w programie %s' % name), exec_line)
            # refused on X11: row 5 with the reason, and no "Włącz"
            state = A.row_state('Obsidian', True, A.CHROMIUM, None, None, plan, StateTests.REFUSED_X11)
            self.assertEqual((state.state, state.button), (A.UNFIXABLE, None), exec_line)
        self.assertFalse(self.local_bin.exists() or (self.data / 'applications').exists())

    def test_a_runtime_that_other_apps_share_gets_no_wrapper(self):
        electron = program(self.root / 'usr' / 'lib' / 'electron30' / 'electron', 'resources.pak')
        (self.bin / 'electron30').symlink_to(electron)
        write(self.system / 'applications' / 'notes.desktop', desktop('Notes', 'electron30 /usr/lib/notes/app.asar %U'))
        write(self.system / 'applications' / 'other.desktop', desktop('Other', 'electron30 /usr/lib/other/app.asar'))
        entry = self.installed()['notes.desktop']
        detection = A.detect(entry, self.places)
        self.assertTrue(detection.chromium)
        plan = A.plan_fix(entry.id, entry, detection, A.kind_of(entry.id, detection), self.places)
        # the override changes this app's Exec lines alone, never what electron30 is for the others
        self.assertEqual((plan.kind, plan.path), (A.OVERRIDE, self.data / 'applications' / 'notes.desktop'))
        self.assertIn('\nExec=electron30 --ozone-platform=wayland /usr/lib/notes/app.asar %U\n', plan.text)
        self.assertIsNone(A.apply_fix(plan))
        other = self.installed()['other.desktop']
        self.assertIsNone(A.fix_status(other, A.detect(other, self.places), self.places))
        for name in ('electron', 'electron30', 'electron30.1', 'nw'):
            self.assertTrue(A.SHARED_RUNTIME.fullmatch(name), name)
        for name in ('chatgpt', 'electron-app', 'nwjs', 'obsidian', 'claude-desktop'):
            self.assertFalse(A.SHARED_RUNTIME.fullmatch(name), name)

    def test_files_that_are_not_ours_are_never_touched(self):
        target = self.chatgpt()
        entry = self.chat_entry()
        plan = self.plan(entry)  # planned while nothing was there
        text = '#!/bin/sh\n# my own launcher\nexec /usr/bin/chatgpt "$@"\n'
        mine = write(self.local_bin / 'chatgpt', text, 0o755)
        self.assertEqual(self.plan(entry),
                         A.Plan(reason='~/.local/bin/chatgpt już istnieje i nie pochodzi od GramaDramy'))
        self.assertEqual(A.apply_fix(plan), '~/.local/bin/chatgpt już istnieje i nie pochodzi od GramaDramy.')
        self.assertEqual(A.undo_fix(A.Fix(A.WRAPPER, mine)),
                         'Nie usunięto ~/.local/bin/chatgpt: to nie jest plik GramaDramy.')
        self.assertIsNone(A.fix_status(entry, A.detect(entry, self.places), self.places))
        self.assertEqual(A.remove_all_fixes(self.local_bin, self.data / 'applications'), [])
        self.assertEqual(mine.read_text(), text)
        # a symlink is never ours, not even one to a file of ours, and it is never followed
        mine.unlink()
        elsewhere = write(self.root / 'elsewhere', A.wrapper_text('ChatGPT', target), 0o755)
        mine.symlink_to(elsewhere)
        self.assertFalse(A.is_ours(mine))
        self.assertIsNone(self.plan(entry).kind)
        self.assertIsNotNone(A.undo_fix(A.Fix(A.WRAPPER, mine)))
        self.assertIsNotNone(A.apply_fix(plan))
        self.assertEqual(A.remove_all_fixes(self.local_bin, self.data / 'applications'), [])
        self.assertTrue(mine.is_symlink())
        self.assertEqual(elsewhere.read_text(), A.wrapper_text('ChatGPT', target))
        # a .desktop file of the user's own with the same ID (as Claude's in ~/.local/share/applications)
        slack = program(self.root / 'opt' / 'slack' / 'slack', 'resources.pak')
        write(self.system / 'applications' / 'slack.desktop', desktop('Slack', str(slack)))
        own = write(self.data / 'applications' / 'slack.desktop', desktop('My Slack', str(slack)))
        entry = self.installed()['slack.desktop']
        self.assertEqual((entry.path, entry.override), (own, None))
        self.assertEqual(self.plan(entry).reason,
                         '~/.local/share/applications/slack.desktop już istnieje i nie pochodzi od GramaDramy')

    def test_a_wrapper_whose_program_moved(self):
        old = self.chatgpt()
        entry, wrapper = self.chat_entry(), self.local_bin / 'chatgpt'
        self.assertIsNone(A.apply_fix(self.plan(entry)))
        old.unlink()  # a package update moved it
        moved = self.chatgpt(self.root / 'usr' / 'local' / 'bin' / 'chatgpt', lib='chatgpt-2')
        places = dataclasses.replace(self.places, path=self.places.path + (str(moved.parent),))
        detection = A.detect(entry, places)
        fix = A.fix_status(entry, detection, places)
        self.assertEqual(fix, A.Fix(A.WRAPPER, wrapper, broken=True))
        self.assertEqual(A.row_state('ChatGPT', True, A.CHROMIUM, None, fix, None, None).state, A.BROKEN)
        self.assertIsNone(A.repair_fix(fix, A.plan_fix(entry.id, entry, detection, A.CHROMIUM, places)))
        self.assertEqual(A.wrapper_target(wrapper), moved)
        self.assertEqual(A.fix_status(entry, detection, places), A.Fix(A.WRAPPER, wrapper))
        moved.unlink()  # uninstalled: nothing to run any more, so "Napraw" takes the wrapper away
        detection = A.detect(entry, places)
        fix = A.fix_status(entry, detection, places)
        self.assertTrue(fix.broken)
        self.assertIsNone(A.repair_fix(fix, A.plan_fix(entry.id, entry, detection, A.CHROMIUM, places)))
        self.assertFalse(wrapper.exists())

    def test_a_repair_moves_the_fix_where_it_works_now(self):
        self.chatgpt()
        entry = write(self.system / 'applications' / 'chatgpt.desktop', desktop('ChatGPT', 'chatgpt %U'))
        entry = self.installed()['chatgpt.desktop']
        self.assertIsNone(A.apply_fix(self.plan(entry)))
        wrapper = self.local_bin / 'chatgpt'
        (self.bin / 'chatgpt').unlink()
        fix = A.fix_status(entry, A.detect(entry, self.places), self.places)
        moved = self.chatgpt(self.root / 'opt' / 'bin' / 'chatgpt', lib='chatgpt-2')
        last = dataclasses.replace(self.places, path=(str(moved.parent), str(self.local_bin)))  # now an override
        self.assertIsNone(A.repair_fix(fix, self.plan(entry, last)))
        self.assertFalse(wrapper.exists())
        self.assertTrue(A.is_ours(self.data / 'applications' / 'chatgpt.desktop'))

    def test_what_counts_as_ours(self):
        cases = [('#!/bin/sh\n# GramaDrama: x\n', True), ('# GramaDrama: x\n[Desktop Entry]\n', True),
                 ('#!/bin/sh\n\n# GramaDrama: x\n', False), ('x\n# GramaDrama: x\n', False), ('# GramaDrama:x\n', False),
                 ('#!/bin/sh', False), ('', False), ('#GramaDrama: x', False), (' # GramaDrama: x', False)]
        for i, (text, ours) in enumerate(cases):
            self.assertEqual(A.is_ours(write(self.root / str(i), text)), ours, text)
        self.assertFalse(A.is_ours(self.root / 'missing'))
        self.assertFalse(A.is_ours(self.root))  # a directory
        os.mkfifo(self.root / 'fifo')
        self.assertFalse(A.is_ours(self.root / 'fifo'))  # never waits for a writer

    def test_removing_every_fix(self):
        wrapper = write(self.local_bin / 'chatgpt', A.wrapper_text('ChatGPT', '/usr/bin/chatgpt'), 0o755)
        override = write(self.data / 'applications' / 'slack.desktop', A.MARKER + 'run Slack\n[Desktop Entry]\n')
        keep = [write(self.local_bin / 'gramadrama', '#!/bin/sh\nexec /usr/bin/python3 /x/gramadrama-app "$@"\n'),
                write(self.data / 'applications' / 'mine.desktop', '[Desktop Entry]\nName=Mine\n'),
                write(self.data / 'applications' / 'notes.txt', A.MARKER + 'not a .desktop file\n'),
                write(self.data / 'applications' / 'sub' / 'x.desktop', A.MARKER + 'never written there\n')]
        (self.local_bin / 'link').symlink_to(wrapper)
        self.assertEqual(A.remove_all_fixes(self.local_bin, self.data / 'applications'), [wrapper, override])
        self.assertFalse(wrapper.exists() or override.exists())
        self.assertTrue(all(path.exists() for path in keep))
        self.assertTrue((self.local_bin / 'link').is_symlink())
        self.assertEqual(A.remove_all_fixes(self.root / 'nowhere', self.root / 'nowhere'), [])


class RealWrapperTests(unittest.TestCase):
    def test_the_hand_made_chatgpt_wrapper_is_ours(self):
        path = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local' / 'bin' / 'chatgpt'
        if not path.is_file() or path.is_symlink():
            self.skipTest('no ~/.local/bin/chatgpt here')
        self.assertTrue(A.is_ours(path))  # read only: the file of 2026-09-27 must stay recognised


class StateTests(unittest.TestCase):
    PLAN = A.Plan(A.WRAPPER, Path('/h/.local/bin/chatgpt'), 'x')
    FOREIGN = A.Plan(reason='~/.local/bin/chatgpt już istnieje i nie pochodzi od GramaDramy')
    FIX = A.Fix(A.WRAPPER, Path('/h/.local/bin/chatgpt'))
    BROKEN = A.Fix(A.WRAPPER, Path('/h/.local/bin/chatgpt'), broken=True)
    REFUSED_X11 = A.Evidence(seen=True, refusal='no_text', refusal_win='x11', x11_no_text=True)

    def state(self, enabled=True, kind=A.CHROMIUM, platform=None, fix=None, plan=PLAN, evidence=None, now=None):
        return A.row_state('ChatGPT', enabled, kind, platform, fix, plan, evidence, now)

    def test_1_switched_off(self):
        for fix in (None, self.BROKEN):
            self.assertEqual(self.state(enabled=False, platform='x11', fix=fix, evidence=self.REFUSED_X11),
                             A.RowState(A.OFF, 'off', 'Wyłączona · skrót korekty trafia do aplikacji'))

    def test_2_a_broken_fix(self):
        self.assertEqual(self.state(fix=self.BROKEN, platform='x11'),
                         A.RowState(A.BROKEN, 'warning', 'Poprawka uruchamiania jest nieaktualna', A.REPAIR))

    def test_3_a_restart_after_the_fix(self):
        self.assertEqual(self.state(fix=self.FIX, platform='x11'),
                         A.RowState(A.RESTART, 'restart', 'Zamknij aplikację ChatGPT (Ctrl+Q) i uruchom ją ponownie, '
                                                          'żeby poprawka zadziałała', A.UNDO))

    def test_4_x11_and_the_fix_is_possible(self):
        fixable = A.RowState(A.FIXABLE, 'tool', 'Działa przez X11, więc nie udostępnia tekstu. Poprawka: uruchamianie '
                                                'natywne', A.APPLY)
        self.assertEqual(self.state(platform='x11'), fixable)  # the GPU process
        self.assertEqual(self.state(evidence=self.REFUSED_X11), fixable)  # not running now: the last refusal

    def test_5_x11_and_no_fix(self):
        self.assertEqual(self.state(platform='x11', plan=self.FOREIGN),
                         A.RowState(A.UNFIXABLE, 'error', 'Działa przez X11 i nie udostępnia tekstu · ~/.local/bin/chatgpt '
                                                          'już istnieje i nie pochodzi od GramaDramy'))
        flatpak = self.state(plan=A.Plan(reason=A.FLATPAK_REASON), evidence=self.REFUSED_X11)
        self.assertEqual(flatpak, A.RowState(A.UNFIXABLE, 'error', 'Działa przez X11 i nie udostępnia tekstu · aplikacja '
                                                                   'z Flatpaka potrzebuje zmiany uprawnień', A.HELP))
        other = self.state(kind=None, plan=A.Plan(reason='nie rozpoznano aplikacji Chromium ani Electron'),
                           evidence=self.REFUSED_X11)  # an app the fix cannot help
        self.assertEqual(other.text, 'Działa przez X11 i nie udostępnia tekstu · nie rozpoznano aplikacji Chromium ani '
                                     'Electron')
        self.assertEqual(self.state(plan=None, platform='x11').text,
                         'Działa przez X11 i nie udostępnia tekstu · poprawka nie jest możliwa')

    def test_6_terminals(self):
        unsupported = A.RowState(A.UNSUPPORTED, 'error', 'Terminal nie udostępnia tekstu')
        self.assertEqual(self.state(kind=A.TERMINAL, plan=None), unsupported)
        refused = A.Evidence(seen=True, refusal='terminal', refusal_win='wayland')
        self.assertEqual(self.state(kind=None, plan=None, evidence=refused), unsupported)
        # VS Code's terminal: an app that corrected text elsewhere is no terminal
        vscode = A.Evidence(seen=True, ok_count=2, ok_at=at(9, 0), refusal='terminal', refusal_win='wayland')
        self.assertEqual(self.state(evidence=vscode, now=at(10, 0)).state, A.WORKS)

    def test_7_works(self):
        now = at(15, 0)
        works = A.Evidence(seen=True, ok_count=3, ok_at=at(14, 1), last_ok=True)
        self.assertEqual(self.state(kind=None, plan=None, evidence=works, now=now),
                         A.RowState(A.WORKS, 'ok', 'Działa · ostatnio dziś 14:01 · 3 poprawki'))
        self.assertEqual(self.state(kind=A.GTK4, plan=None, evidence=works, now=now),
                         A.RowState(A.WORKS, 'partial', 'Działa · ostatnio dziś 14:01 · 3 poprawki · po zaznaczeniu myszą '
                                                        'najpierw naciśnij strzałkę'))
        self.assertEqual(self.state(fix=self.FIX, platform='wayland', evidence=works, now=now),
                         A.RowState(A.WORKS, 'ok', 'Działa · ostatnio dziś 14:01 · 3 poprawki · uruchamiana natywnie '
                                                   '(poprawka GramaDramy)', A.UNDO))
        for n, word in ((1, 'poprawka'), (5, 'poprawek'), (22, 'poprawki')):
            text = self.state(evidence=A.Evidence(ok_count=n, ok_at=at(9, 5, 1)), now=now).text
            self.assertEqual(text, 'Działa · ostatnio wczoraj 09:05 · %d %s' % (n, word))
        self.assertEqual(self.state(evidence=A.Evidence(ok_count=1)).text, 'Działa · 1 poprawka')  # no time logged

    def test_8_not_confirmed(self):
        try_it = A.RowState(A.UNCONFIRMED, 'unknown', 'Niepotwierdzone · wypróbuj skrót korekty w polu tekstowym')
        self.assertEqual(self.state(kind=None, plan=None), try_it)
        self.assertEqual(self.state(kind=A.GTK3, plan=None), try_it)
        self.assertEqual(self.state(), try_it)  # Chromium that does not run now
        native = A.RowState(A.UNCONFIRMED, 'unknown', 'Niepotwierdzone · działa natywnie, powinno działać')
        self.assertEqual(self.state(platform='wayland'), native)
        # restarted with the fix, or not started since: it can be switched off here too
        self.assertEqual(self.state(fix=self.FIX), dataclasses.replace(native, button=A.UNDO))
        self.assertEqual(self.state(fix=self.FIX, platform='wayland'), dataclasses.replace(native, button=A.UNDO))

    def test_newer_evidence_wins(self):
        now = at(15, 0)
        # a correction after the X11 refusal: the refusal is history
        after = A.Evidence(seen=True, ok_count=1, ok_at=at(10, 0), refusal='no_text', refusal_win='x11', last_ok=True)
        self.assertEqual(self.state(evidence=after, now=now).state, A.WORKS)
        # refused on X11 after a correction: it runs through X11 again
        again = A.Evidence(seen=True, ok_count=1, ok_at=at(10, 0), refusal='no_text', refusal_win='x11', x11_no_text=True)
        self.assertEqual(self.state(evidence=again, now=now).state, A.FIXABLE)
        self.assertEqual(self.state(evidence=A.Evidence(x11_no_text=True, last_ok=True)).state, A.UNCONFIRMED)
        # an instance running on Wayland says more than an older refusal
        self.assertEqual(self.state(platform='wayland', evidence=self.REFUSED_X11).text,
                         'Niepotwierdzone · działa natywnie, powinno działać')
        # with the fix in place, only an instance running on X11 asks for a restart
        self.assertEqual(self.state(fix=self.FIX, evidence=self.REFUSED_X11).state, A.UNCONFIRMED)
        self.assertEqual(self.state(fix=self.FIX, platform='x11', evidence=self.REFUSED_X11).state, A.RESTART)
        # a terminal refusal older than a correction does not make a terminal
        old_terminal = A.Evidence(seen=True, refusal='terminal', last_ok=True)
        self.assertEqual(self.state(kind=None, plan=None, evidence=old_terminal).state, A.UNCONFIRMED)

    def test_a_pending_override_asks_for_a_new_login(self):
        now = at(15, 0)
        pending = A.Fix(A.OVERRIDE, Path('/h/.local/share/applications/slack.desktop'), pending=True)
        login = A.RowState(A.RESTART, 'restart', 'Wyloguj się i zaloguj ponownie, żeby poprawka zadziałała', A.UNDO)
        self.assertEqual(self.state(fix=pending, platform='x11'), login)  # a restart from the app grid would not help
        self.assertEqual(self.state(fix=pending, platform='x11', evidence=self.REFUSED_X11), login)
        self.assertEqual(self.state(fix=pending), A.RowState(
            A.UNCONFIRMED, 'unknown', 'Niepotwierdzone · poprawka zadziała po wylogowaniu i ponownym zalogowaniu', A.UNDO))
        # started from the dock or search, it runs natively already
        self.assertEqual(self.state(fix=pending, platform='wayland'),
                         A.RowState(A.UNCONFIRMED, 'unknown', 'Niepotwierdzone · działa natywnie, powinno działać', A.UNDO))
        works = A.Evidence(seen=True, ok_count=1, ok_at=at(14, 1), last_ok=True)
        self.assertEqual(self.state(fix=pending, evidence=works, now=now).state, A.WORKS)
        self.assertEqual(self.state(fix=dataclasses.replace(pending, broken=True), platform='x11').state, A.BROKEN)
        self.assertEqual(self.state(enabled=False, fix=pending, platform='x11').state, A.OFF)
        # after the next login it is a fix like the wrapper: running on X11, the app needs a restart
        active = dataclasses.replace(pending, pending=False)
        self.assertEqual(self.state(fix=active, platform='x11').text,
                         'Zamknij aplikację ChatGPT (Ctrl+Q) i uruchom ją ponownie, żeby poprawka zadziałała')
        self.assertEqual(self.state(fix=active).text, 'Niepotwierdzone · działa natywnie, powinno działać')

    def test_every_state_is_ordered_and_every_button_has_a_label(self):
        self.assertEqual(sorted(A.ORDER), sorted((A.OFF, A.BROKEN, A.RESTART, A.FIXABLE, A.UNFIXABLE, A.UNSUPPORTED,
                                                 A.WORKS, A.UNCONFIRMED)))
        self.assertEqual(sorted(A.BUTTONS), sorted((A.APPLY, A.UNDO, A.REPAIR, A.HELP)))
        self.assertEqual(A.BUTTONS[A.UNDO], 'Wyłącz poprawkę')


class ListTests(unittest.TestCase):
    @staticmethod
    def entry(app_id, name):
        return A.Entry(app_id, Path('/usr/share/applications') / app_id, name, 'icon-' + name, 'x')

    def test_the_sources_of_the_list(self):
        # the list grows from use: installed apps that are known (catalogue) or Chromium are not on it
        evidence = {'window:xterm': A.Evidence(seen=True), A.SHELL: A.Evidence(seen=True),
                    A.SELF: A.Evidence(seen=True, ok_count=1), 'window:pl.gramadrama.Linux': A.Evidence(seen=True),
                    'mail.desktop': A.Evidence(seen=True), 'verified-only.desktop': A.Evidence(ok_count=1)}
        config = A.Config(disabled=['org.gnome.Ptyxis.desktop', 'gone.desktop', A.SELF],
                          added=['obsidian2.desktop', 'mail.desktop', 'window:pl.gramadrama.Linux'])
        self.assertEqual(A.listed(config, evidence, ['slack.desktop', 'obsidian2.desktop']), {
            'mail.desktop': False, 'slack.desktop': False, 'window:xterm': False,  # the log, a fix of ours
            'obsidian2.desktop': False,  # added, but it has a fix too: the remove button would change nothing
            'org.gnome.Ptyxis.desktop': False, 'gone.desktop': False})  # off: it can be switched on again
        self.assertEqual(A.listed(config, evidence), {
            'mail.desktop': False, 'window:xterm': False, 'obsidian2.desktop': True, 'org.gnome.Ptyxis.desktop': False,
            'gone.desktop': False})
        # GNOME Shell's entries (Activities search, the run dialog) only while switched off by hand
        self.assertEqual(A.listed(A.Config(disabled=[A.SHELL]), evidence),
                         {'window:xterm': False, 'mail.desktop': False, A.SHELL: False})
        # review finding: an app added by hand and switched off has no remove button, since removing it
        # would switch it on (spec, sections 4 and 8); switched on again, it can be removed
        self.assertEqual(A.listed(A.Config(disabled=['x.desktop'], added=['x.desktop']), {}), {'x.desktop': False})
        self.assertEqual(A.listed(A.Config(added=['x.desktop']), {}), {'x.desktop': True})
        self.assertEqual(A.listed(A.Config(), {}), {})
        # an app with a mode of its own stays, so that it can be changed back; it is no "removed by hand" row
        self.assertEqual(A.listed(A.Config(added=['x.desktop'], modes={'x.desktop': 'formal', 'z.desktop': 'casual'}), {}),
                         {'x.desktop': False, 'z.desktop': False})
        entries = {e.id: e for e in (self.entry('plasticity.desktop', 'Plasticity'),)}
        self.assertEqual([A.display_name(i, entries.get(i)) for i in ('window:xterm', A.SHELL, 'gone.desktop',
                                                                       'plasticity.desktop', 'window:', A.SELF,
                                                                       'window:pl.gramadrama.Linux')],
                         ['xterm', 'GNOME Shell', 'gone', 'Plasticity', 'window:', 'GramaDrama', 'GramaDrama'])

    def test_rows_by_state_then_name(self):
        now = at(15, 0)
        entries = {e.id: e for e in (
            self.entry('chatgpt.desktop', 'ChatGPT'), self.entry('google-chrome.desktop', 'Google Chrome'),
            self.entry('org.gnome.TextEditor.desktop', 'Edytor tekstu'), self.entry('org.gnome.Ptyxis.desktop', 'Terminal'),
            self.entry('zed.desktop', 'zed'), self.entry('obsidian.desktop', 'Obsidian'),
            self.entry('slack.desktop', 'Slack'))}
        detections = {'chatgpt.desktop': A.Detection('chatgpt', Path('/usr/bin/chatgpt'), Path('/usr/lib/chatgpt')),
                      'google-chrome.desktop': A.Detection('/usr/bin/google-chrome-stable',
                                                           Path('/usr/bin/google-chrome-stable'), Path('/opt/google/chrome')),
                      'obsidian.desktop': A.Detection('obsidian', Path('/usr/bin/obsidian'), Path('/opt/Obsidian')),
                      'slack.desktop': A.Detection('/usr/bin/slack', Path('/usr/bin/slack'), Path('/usr/lib/slack'))}
        platforms = {Path('/usr/lib/chatgpt'): 'x11', Path('/opt/google/chrome'): 'wayland', Path('/opt/other'): 'x11',
                     Path('/opt/Obsidian'): 'x11'}
        plan = A.Plan(A.WRAPPER, Path('/h/.local/bin/chatgpt'), 'x')
        override = A.Fix(A.OVERRIDE, Path('/h/.local/share/applications/slack.desktop'), pending=True)
        fixes = {'chatgpt.desktop': (plan, None), 'slack.desktop': (A.Plan(A.OVERRIDE), override)}
        evidence = {'chatgpt.desktop': A.Evidence(seen=True, refusal='no_text', refusal_win='x11', x11_no_text=True),
                    'google-chrome.desktop': A.Evidence(seen=True, ok_count=2, ok_at=at(14, 0), last_ok=True),
                    'org.gnome.TextEditor.desktop': A.Evidence(seen=True),
                    'window:xterm': A.Evidence(seen=True), 'zed.desktop': A.Evidence(seen=True)}
        config = A.Config(disabled=['org.gnome.Ptyxis.desktop'], added=['zed.desktop'])
        rows = A.compose(config, evidence, entries, detections, fixes, platforms, now)
        # Obsidian runs on X11, but it was never used here: not listed
        self.assertEqual([(row.id, row.state.state) for row in rows], [
            ('chatgpt.desktop', A.FIXABLE), ('google-chrome.desktop', A.WORKS),
            ('org.gnome.TextEditor.desktop', A.UNCONFIRMED), ('slack.desktop', A.UNCONFIRMED),
            ('window:xterm', A.UNCONFIRMED), ('zed.desktop', A.UNCONFIRMED), ('org.gnome.Ptyxis.desktop', A.OFF)])
        chat, chrome, _editor, slack, xterm, zed, ptyxis = rows
        self.assertEqual({row.mode for row in rows}, {'fix'})  # no entry is Popraw
        with_modes = A.compose(A.Config(disabled=config.disabled, added=config.added,
                                        modes={'google-chrome.desktop': 'formal', 'slack.desktop': 'no-such-mode',
                                               'zed.desktop': 'shorter'}),
                               evidence, entries, detections, fixes, platforms, now)
        self.assertEqual({row.id: row.mode for row in with_modes if row.mode != 'fix'},
                         {'google-chrome.desktop': 'formal', 'zed.desktop': 'shorter'})  # an unknown mode shows Popraw
        self.assertEqual((chat.name, chat.icon, chat.enabled, chat.removable, chat.plan, chat.fix),
                         ('ChatGPT', 'icon-ChatGPT', True, False, plan, None))
        self.assertEqual(chrome.state.text, 'Działa · ostatnio dziś 14:00 · 2 poprawki')
        self.assertEqual((slack.fix, slack.state.button), (override, A.UNDO))  # on the list for its fix alone
        self.assertEqual((xterm.name, xterm.icon, xterm.removable), ('xterm', '', False))
        self.assertEqual((zed.removable, ptyxis.enabled), (False, False))  # zed is in the log too

    def test_what_to_expect_in_an_app(self):
        self.assertIn('X11', A.expectation(A.CHROMIUM))
        self.assertEqual(A.expectation(A.GTK4), 'działa · ' + A.GTK4_NOTE)
        self.assertEqual(A.expectation(A.TERMINAL), 'terminal nie udostępnia tekstu')
        self.assertEqual(A.expectation(None), '')
        self.assertEqual(set(A.EXPECTATIONS), set(A.APP_CATALOGUE.values()))


class ScanTests(TempHome):
    def test_from_x11_through_the_fix_to_a_confirmed_correction(self):
        proc = self.root / 'proc'
        chat = program(self.root / 'usr' / 'lib' / 'chatgpt' / 'ChatGPT', 'resources.pak')
        (self.bin / 'chatgpt').symlink_to(program(chat.parent / 'codex-launcher'))
        apps = self.system / 'applications'
        write(apps / 'chatgpt.desktop', desktop('ChatGPT', 'chatgpt %U'))
        write(apps / 'org.gnome.Ptyxis.desktop', desktop('Terminal', 'ptyxis --new-window'))
        write(apps / 'org.gnome.Calculator.desktop', desktop('Kalkulator', 'gnome-calculator'))
        write(apps / (A.SELF), desktop('GramaDrama', '/usr/bin/python3 /x/gramadrama-app'))
        gpu = fake_process(proc, 50, ('%s --type=gpu-process --ozone-platform=x11' % chat).encode() + b'\0', chat)
        events = [ev('refused', at(9, 0), at='hotkey', reason='no_text', app='chatgpt.desktop', win='x11'),
                  ev('refused', at(9, 0), at='hotkey', reason='terminal', app='org.gnome.Ptyxis.desktop', win='wayland'),
                  ev('start', at(9, 1), id=1, app=A.SELF, win='wayland'),
                  ev('verified', at(9, 1), id=1, outcome='ok', app=A.SELF, win='wayland')]

        def rows():
            config = A.read_config()[0]
            rows = A.scan(events, config, at(15, 0), proc, self.places, [self.data, self.system]).rows
            return {row.id: row for row in rows}

        self.assertEqual(sorted(rows()), ['chatgpt.desktop', 'org.gnome.Ptyxis.desktop'])  # not the calculator
        row = rows()['chatgpt.desktop']
        self.assertEqual((row.name, row.state.state, row.state.button, row.plan.kind),
                         ('ChatGPT', A.FIXABLE, A.APPLY, A.WRAPPER))
        self.assertEqual(rows()['org.gnome.Ptyxis.desktop'].state.state, A.UNSUPPORTED)
        self.assertIsNone(A.apply_fix(row.plan))  # "Włącz"
        wrapper = self.local_bin / 'chatgpt'
        self.assertEqual(wrapper.read_text(), A.wrapper_text('ChatGPT', self.bin / 'chatgpt'))
        row = rows()['chatgpt.desktop']
        self.assertEqual((row.state.state, row.state.button, row.fix), (A.RESTART, A.UNDO, A.Fix(A.WRAPPER, wrapper)))
        restarted = '%s --type=gpu-process --ozone-platform=wayland' % chat
        (gpu / 'cmdline').write_bytes(restarted.encode() + b'\0')
        row = rows()['chatgpt.desktop']
        self.assertEqual((row.state.state, row.state.text, row.state.button),
                         (A.UNCONFIRMED, 'Niepotwierdzone · działa natywnie, powinno działać', A.UNDO))
        events += [ev('start', at(14, 1), id=2, app='chatgpt.desktop', win='wayland'),
                   ev('verified', at(14, 1), id=2, outcome='ok', app='chatgpt.desktop', win='wayland')]
        row = rows()['chatgpt.desktop']
        self.assertEqual(row.state, A.RowState(A.WORKS, 'ok', 'Działa · ostatnio dziś 14:01 · 1 poprawka · uruchamiana '
                                                                 'natywnie (poprawka GramaDramy)', A.UNDO))
        self.assertIsNone(A.undo_fix(row.fix))  # "Wyłącz poprawkę"
        self.assertFalse(wrapper.exists())
        self.assertEqual(A.set_enabled('chatgpt.desktop', False), (None, None))
        self.assertEqual(rows()['chatgpt.desktop'].state.state, A.OFF)

    def test_an_override_counts_after_the_next_login(self):
        proc, tick = self.root / 'proc', os.sysconf('SC_CLK_TCK')
        slack = program(self.root / 'usr' / 'lib' / 'slack' / 'slack', 'resources.pak')
        write(self.system / 'applications' / 'slack.desktop', desktop('Slack', '%s %%U' % slack))
        gpu = fake_process(proc, 60, ('%s --type=gpu-process --ozone-platform=x11' % slack).encode() + b'\0', slack)
        shell = fake_process(proc, 40, ['/usr/bin/gnome-shell'], comm='gnome-shell', started=100 * tick)
        boot = int(time.time()) - 1000  # GNOME Shell started 900 s ago
        (proc / 'stat').write_text('cpu  1 2 3\nbtime %d\n' % boot)
        events = [ev('refused', at(9, 0), at='hotkey', reason='no_text', app='slack.desktop', win='x11')]

        def row():
            rows = A.scan(events, A.read_config()[0], at(15, 0), proc, self.places, [self.data, self.system]).rows
            return {r.id: r for r in rows}['slack.desktop']

        fixable = row()
        self.assertEqual((fixable.state.state, fixable.plan.kind), (A.FIXABLE, A.OVERRIDE))
        self.assertIsNone(A.apply_fix(fixable.plan))  # "Włącz"
        override = self.data / 'applications' / 'slack.desktop'
        self.assertEqual((row().state.text, row().fix), ('Wyloguj się i zaloguj ponownie, żeby poprawka zadziałała',
                                                        A.Fix(A.OVERRIDE, override, pending=True)))
        (gpu / 'cmdline').write_bytes(b'')  # quit: the app grid would start it without the flag again
        self.assertEqual(row().state.text, 'Niepotwierdzone · poprawka zadziała po wylogowaniu i ponownym zalogowaniu')
        (shell / 'stat').write_text('40 (gnome-shell) S 1 %s %d 0 0\n' % (' '.join(['0'] * 17), 2000 * tick))  # a new login
        (gpu / 'cmdline').write_bytes(('%s --type=gpu-process --ozone-platform=wayland' % slack).encode() + b'\0')
        self.assertEqual((row().state.text, row().fix), ('Niepotwierdzone · działa natywnie, powinno działać',
                                                        A.Fix(A.OVERRIDE, override)))

    def test_one_scan_for_the_rows_the_names_and_the_hints(self):
        proc = self.root / 'proc'
        proc.mkdir()
        chat = program(self.root / 'usr' / 'lib' / 'chatgpt' / 'ChatGPT', 'resources.pak')
        (self.bin / 'chatgpt').symlink_to(program(chat.parent / 'codex-launcher'))
        cad = program(self.root / 'opt' / 'cad' / 'cad', 'resources.pak')  # Electron, installed, never used here
        apps = self.system / 'applications'
        write(apps / 'chatgpt.desktop', desktop('ChatGPT', 'chatgpt %U'))
        write(apps / 'cad.desktop', desktop('Modeler', '%s %%U' % cad))
        write(apps / 'org.gnome.TextEditor.desktop', desktop('Edytor tekstu', 'gnome-text-editor %U'))
        write(apps / A.SELF, desktop('GramaDrama', '/usr/bin/python3 /x/gramadrama-app'))
        events = [ev('start', at(9, 1), id=1, app=A.SELF, win='wayland'),
                  ev('refused', at(9, 2), at='hotkey', reason='stale', app='org.gnome.TextEditor.desktop', win='wayland'),
                  ev('start', at(9, 3), id=2, app='window:xterm', win='x11')]

        def scan():
            return A.scan(events, A.read_config()[0], at(15, 0), proc, self.places, [self.data, self.system])

        found = scan()
        self.assertEqual([row.id for row in found.rows], ['org.gnome.TextEditor.desktop', 'window:xterm'])
        self.assertEqual(found.names, {A.SELF: 'GramaDrama', 'org.gnome.TextEditor.desktop': 'Edytor tekstu',
                                       'window:xterm': 'xterm'})
        self.assertEqual(found.kinds, {'chatgpt.desktop': A.CHROMIUM, 'cad.desktop': A.CHROMIUM,
                                       'org.gnome.TextEditor.desktop': A.GTK4, A.SELF: None})
        # a fix of ours keeps its app on the list, used here or not, so that it can be seen and undone
        write(self.local_bin / 'chatgpt', A.wrapper_text('ChatGPT', self.bin / 'chatgpt'), 0o755)
        row = scan().rows[0]
        self.assertEqual((row.id, row.removable, row.fix, row.state),
                         ('chatgpt.desktop', False, A.Fix(A.WRAPPER, self.local_bin / 'chatgpt'),
                          A.RowState(A.UNCONFIRMED, 'unknown', 'Niepotwierdzone · działa natywnie, powinno działać', A.UNDO)))
        self.assertIsNone(A.undo_fix(row.fix))
        self.assertEqual([row.id for row in scan().rows], ['org.gnome.TextEditor.desktop', 'window:xterm'])



class SitesTests(unittest.TestCase):
    """apps.json "sites": where the browser extension is off (docs/browser.md). The app writes and reads them as the engine does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_a_site_is_switched_off_and_on_and_the_rest_of_the_file_stays(self):
        A.set_enabled('org.example.One.desktop', False)
        A.set_mode('org.example.Two.desktop', 'formal')
        self.assertEqual(A.set_site('mail.example', True), (None, None))
        A.set_site('mail.example', True)  # twice is once
        A.set_site('a.example', True)
        config, problem = A.read_config()
        self.assertEqual((problem, config.sites, config.disabled, config.modes),
                         (None, ['mail.example', 'a.example'], ['org.example.One.desktop'], {'org.example.Two.desktop': 'formal'}))
        A.set_site('mail.example', False)
        self.assertEqual(A.read_config()[0].sites, ['a.example'])
        A.set_site('a.example', False)
        self.assertNotIn('sites', json.loads(paths.apps_path().read_text()))  # a file that uses no site stays as it was

    def test_the_engine_and_the_app_read_the_same_lists_and_call_the_same_files_broken(self):
        from gramadrama.engine import applist
        for content, expected in (({'v': 1, 'sites': ['a.example', 'b.example']}, ['a.example', 'b.example']),
                                  ({'v': 1, 'disabled': ['x.desktop'], 'sites': []}, []), ({'v': 1}, []),
                                  ({'v': 1, 'sites': 'a.example'}, None), ({'v': 1, 'sites': ['a', 7]}, None),
                                  ({'v': 1, 'sites': ['']}, None)):
            with self.subTest(content=content):
                paths.apps_path().parent.mkdir(parents=True, exist_ok=True)
                paths.apps_path().write_text(json.dumps(content))
                config, problem = A.read_config()
                data = paths.apps_path().read_bytes()
                if expected is None:
                    self.assertIsNotNone(problem)
                    self.assertFalse(applist.load_all(data)[2])  # the engine calls it broken too
                    self.assertEqual(applist.load_sites(data), frozenset())
                else:
                    self.assertIsNone(problem)
                    self.assertTrue(applist.load_all(data)[2])
                    self.assertEqual((config.sites, sorted(applist.load_sites(data))), (expected, expected))

    def test_what_the_engine_writes_the_app_reads(self):
        from gramadrama.engine import applist
        data = applist.with_site(None, 'mail.example', True)
        data = applist.with_site(data, 'a.example', True)
        paths.apps_path().parent.mkdir(parents=True, exist_ok=True)
        paths.apps_path().write_bytes(data)
        config, problem = A.read_config()
        self.assertEqual((problem, config.sites), (None, ['mail.example', 'a.example']))
        with self.assertRaises(ValueError):
            applist.with_site(b'{oops', 'x.example', True)  # a broken file is never written over from the engine
        self.assertEqual(applist.with_site(applist.with_site(data, 'a.example', False), 'mail.example', False),
                         applist.with_site(None, 'x', False))  # the last site off: no "sites" key left

if __name__ == '__main__':
    unittest.main()
