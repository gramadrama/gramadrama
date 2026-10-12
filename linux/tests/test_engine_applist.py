"""apps.json as the engine reads it (gramadrama/engine/applist.py, docs/apps.md section 2)."""
import json
import os
import tempfile
import unittest
from unittest import mock
from gramadrama.engine import applist, fallback_ui
from gramadrama.engine.applist import GRAMADRAMA_APP, default_mode, is_off, load, load_all, parse

try:
    from gramadrama import apps as app_side  # the app's own reading of the file
except ImportError:
    app_side = None


def data(value):
    return json.dumps(value).encode()


# (name, file content, the disabled set or None for a broken file)
FILES = [
    ('the app writes it', data({'v': 1, 'disabled': ['org.gnome.Ptyxis.desktop', 'window:Obsidian'],
                                'added': ['obsidian.desktop']}),
     {'org.gnome.Ptyxis.desktop', 'window:Obsidian'}),
    ('nothing off', data({'v': 1, 'added': ['obsidian.desktop']}), set()),
    ('only the version', data({'v': 1}), set()),
    ('duplicates', data({'v': 1, 'disabled': ['chatgpt.desktop', 'chatgpt.desktop']}), {'chatgpt.desktop'}),
    ('the GramaDrama app is never off', data({'v': 1, 'disabled': [GRAMADRAMA_APP, 'chatgpt.desktop']}),
     {'chatgpt.desktop'}),
    ('non-ASCII IDs', json.dumps({'v': 1, 'disabled': ['zażółć.desktop']}, ensure_ascii=False).encode(),
     {'zażółć.desktop'}),
    ('version 1.0', b'{"v": 1.0, "disabled": ["a.desktop"]}', {'a.desktop'}),
    ('not json', b'{"v": 1, "disabled": [', None),
    ('empty', b'', None),
    ('not utf-8', b'{"v": 1, "disabled": ["\xff"]}', None),
    ('utf-16', json.dumps({'v': 1, 'disabled': ['a.desktop']}).encode('utf-16'), None),
    ('a byte order mark', b'\xef\xbb\xbf' + data({'v': 1, 'disabled': ['a.desktop']}), None),
    ('a list', data(['chatgpt.desktop']), None),
    ('null', b'null', None),
    ('no version', data({'disabled': ['chatgpt.desktop']}), None),
    ('version 2', data({'v': 2, 'disabled': ['chatgpt.desktop']}), None),
    ('version as text', data({'v': '1', 'disabled': []}), None),
    ('version true', data({'v': True, 'disabled': []}), None),
    ('disabled not a list', data({'v': 1, 'disabled': 'chatgpt.desktop'}), None),
    ('disabled an object', data({'v': 1, 'disabled': {'chatgpt.desktop': True}}), None),
    ('an ID that is no string', data({'v': 1, 'disabled': ['chatgpt.desktop', 7]}), None),
    ('an ID that is null', data({'v': 1, 'disabled': [None]}), None),
    ('an empty ID', data({'v': 1, 'disabled': ['', 'chatgpt.desktop']}), None),
    ('added broken', data({'v': 1, 'disabled': ['chatgpt.desktop'], 'added': 'obsidian.desktop'}), None),
    ('modes beside the lists', data({'v': 1, 'disabled': ['a.desktop'], 'modes': {'b.desktop': 'formal'}}), {'a.desktop'}),
    ('modes a list', data({'v': 1, 'modes': ['b.desktop']}), None),
    ('modes null', data({'v': 1, 'modes': None}), None),
    ('a mode that is no string', data({'v': 1, 'modes': {'b.desktop': 5}}), None),
    ('an empty app ID in modes', data({'v': 1, 'modes': {'': 'formal'}}), None),
]


class ParseTests(unittest.TestCase):
    def test_files(self):
        for name, raw, want in FILES:
            with self.subTest(name):
                if want is None:
                    with self.assertRaises(ValueError):
                        parse(raw)
                    self.assertEqual(load(raw), (frozenset(), False))  # fail-open: nothing is off
                else:
                    self.assertEqual(parse(raw), frozenset(want))
                    self.assertEqual(load(raw), (frozenset(want), True))

    def test_limits(self):
        for name, raw in (('too large', data({'v': 1, 'disabled': ['x' * applist.MAX_BYTES]})),
                          ('nested too deep', b'[' * 100000)):
            with self.subTest(name):
                self.assertEqual(load(raw), (frozenset(), False))

    def test_no_file(self):
        self.assertEqual(load(None), (frozenset(), True))  # nothing is off, and nothing is wrong

    @unittest.skipUnless(app_side and hasattr(app_side, 'read_config'), 'the app side reads apps.json elsewhere')
    def test_the_app_agrees_which_files_are_broken(self):
        # The app tells the user that GramaDrama works in every app while the file is broken: the
        # engine must fail open for exactly those files, and follow the list for all others.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'gramadrama'))
            with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp}):
                for name, raw, want in FILES:
                    with self.subTest(name):
                        with open(os.path.join(tmp, 'gramadrama', 'apps.json'), 'wb') as f:
                            f.write(raw)
                        config, problem = app_side.read_config()
                        self.assertEqual(problem is None, want is not None, problem)
                        if want is not None:
                            self.assertEqual(set(config.disabled), want)


class ModesTests(unittest.TestCase):
    def test_the_modes_of_a_file(self):
        raw = data({'v': 1, 'disabled': ['a.desktop'], 'modes': {'b.desktop': 'formal', 'c.desktop': 'no-such-mode',
                                                                  GRAMADRAMA_APP: 'shorter'}})
        disabled, modes, ok = load_all(raw)
        self.assertEqual((disabled, ok), (frozenset({'a.desktop'}), True))
        self.assertEqual(modes, {'b.desktop': 'formal', 'c.desktop': 'no-such-mode', GRAMADRAMA_APP: 'shorter'})
        self.assertEqual(load_all(data({'v': 1})), (frozenset(), {}, True))
        self.assertEqual(load_all(None), (frozenset(), {}, True))  # no file: nothing is off, nothing changes
        self.assertEqual(load_all(b'not json'), (frozenset(), {}, False))  # broken: fail open, and Popraw everywhere
        self.assertEqual(load_all(data({'v': 1, 'disabled': ['a.desktop'], 'modes': []})), (frozenset(), {}, False))

    def test_the_mode_of_an_app(self):
        modes = {'b.desktop': 'formal', 'c.desktop': 'no-such-mode', 'd.desktop': 'translate', 'e.desktop': 'fix',
                 GRAMADRAMA_APP: 'shorter'}
        cases = [('set', 'b.desktop', 'formal'), ('a mode this build does not know', 'c.desktop', 'fix'),
                 ('Tłumacz is no default: it has Ctrl+Alt+J', 'd.desktop', 'fix'), ('Popraw written out', 'e.desktop', 'fix'),
                 ('no entry', 'f.desktop', 'fix'), ('the GramaDrama app', GRAMADRAMA_APP, 'fix'),
                 ('unknown focus', None, 'fix'), ('no focus window', '', 'fix')]
        for name, app, want in cases:
            with self.subTest(name):
                self.assertEqual(default_mode(app, modes), want)
        self.assertEqual(default_mode('b.desktop', {}), 'fix')
        # the choices are the panel's modes without Tłumacz, in the panel's order (the app's list shows them so)
        self.assertEqual(applist.MODE_CHOICES, tuple(key for key, _ in fallback_ui.MODES if key != 'translate'))


class IsOffTests(unittest.TestCase):
    def test_table(self):
        off = frozenset({'chatgpt.desktop', 'window:Obsidian', GRAMADRAMA_APP, ''})
        cases = [
            ('on the list', 'chatgpt.desktop', True),
            ('a window-backed app on the list', 'window:Obsidian', True),
            ('not on the list: allowed', 'google-chrome.desktop', False),
            ('unknown focus (no extension, an old one, the lock screen)', None, False),
            ('no focus window', '', False),
            ('the GramaDrama app, even when listed', GRAMADRAMA_APP, False),
            ('a shell entry', 'org.gnome.Shell', False),
        ]
        for name, app, want in cases:
            with self.subTest(name):
                self.assertEqual(is_off(app, off), want)
        self.assertFalse(is_off('chatgpt.desktop', frozenset()))


if __name__ == '__main__':
    unittest.main()
