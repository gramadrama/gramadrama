"""The parts of gramadrama/app.py that need no desktop: LogWatcher (following the engine log, rotation
included) and the helpers of the Aplikacje section."""
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

LINUX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LINUX))
from gramadrama import apps  # noqa: E402
from gramadrama.app import (APP_ICONS, GENERIC_APP_ICON, FLATPAK_HELP, WEBSITE, LogWatcher, app_icon,  # noqa: E402
                            arranged, icon_name, wanted)


def line(**event):
    return (json.dumps(event, separators=(',', ':')) + '\n').encode()


class ArgumentTests(unittest.TestCase):
    def test_a_failed_corrections_button_and_the_logins_way_back_are_taken(self):
        for argument in ('--show=connection', '--show=writing', '--login', 'gramadrama://auth?code=x&state=y'):
            self.assertTrue(wanted(argument), argument)
        for argument in ('--show=nowhere', '--show=', '--selection', '--background', 'https://example.com/'):
            self.assertFalse(wanted(argument), argument)  # an old launcher's options just open the window


class LogWatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'gramadrama' / 'engine.log'
        self.got = []

    def watcher(self):
        watcher = LogWatcher(self.path, self.got.extend)
        self.addCleanup(watcher.close)
        return watcher

    def append(self, data, path=None):
        with open(path or self.path, 'ab') as f:
            f.write(data)

    def test_history_then_new_lines(self):
        self.path.parent.mkdir(parents=True)
        self.append(line(t=1, ev='start', id=1) + line(t=2, ev='focus_in') + b'{"t":3,"ev":"verified","id":1')
        (self.path.parent / 'engine.log.1').write_bytes(line(t=0, ev='refused', at='hotkey', reason='stale'))
        watcher = self.watcher()
        self.assertEqual([e['ev'] for e in watcher.history()], ['refused', 'start'])
        self.append(b',"outcome":"ok"}\n')  # the rest of the line that was being written
        watcher.read()
        self.assertEqual([e['ev'] for e in self.got], ['verified'])
        self.append(line(t=4, ev='end', id=1, why='cancel')[:10])
        watcher.read()
        self.assertEqual(len(self.got), 1)  # half a line waits for its end
        self.append(line(t=4, ev='end', id=1, why='cancel')[10:])
        watcher.read()
        self.assertEqual([e['ev'] for e in self.got], ['verified', 'end'])

    def test_rotation_and_a_missing_log(self):
        watcher = self.watcher()  # no log yet: the directory is created for the monitor
        self.assertEqual(watcher.history(), [])
        self.assertTrue(self.path.parent.is_dir())
        self.assertEqual(oct(self.path.parent.stat().st_mode & 0o777), '0o700')
        self.append(line(t=1, ev='start', id=1))
        watcher.read()
        os.replace(self.path, str(self.path) + '.1')  # the engine rotates at 1 MB
        self.append(line(t=2, ev='result', id=1))
        watcher.read()
        self.assertEqual([e['ev'] for e in self.got], ['start', 'result'])

    def test_lines_written_just_before_a_rotation_are_not_lost(self):
        self.path.parent.mkdir(parents=True)
        self.append(line(t=1, ev='focus_in'))
        watcher = self.watcher()
        watcher.history()
        # the engine appends, rotates and appends again before the app gets to read
        self.append(line(t=2, ev='start', id=1))
        os.replace(self.path, str(self.path) + '.1')
        self.append(line(t=3, ev='result', id=1))
        watcher.read()
        self.assertEqual([e['ev'] for e in self.got], ['start', 'result'])
        self.append(line(t=4, ev='verified', id=1, outcome='ok'))
        watcher.read()
        self.assertEqual([e['ev'] for e in self.got], ['start', 'result', 'verified'])

    def test_close_releases_the_file(self):
        self.path.parent.mkdir(parents=True)
        self.append(line(t=1, ev='start', id=1))
        watcher = self.watcher()
        watcher.history()
        self.assertIsNotNone(watcher.file)
        watcher.close()
        self.assertIsNone(watcher.file)


class AppsSectionTests(unittest.TestCase):
    def test_rows_keep_their_places_while_the_user_works_in_the_list(self):
        self.assertEqual(arranged(['b', 'a', 'c'], ['a', 'b', 'c'], False), ['b', 'a', 'c'])  # as apps.compose sorted them
        # a switch was just toggled: the rows stay where they are, a row that went away goes, a new one comes last
        self.assertEqual(arranged(['b', 'd', 'a'], ['a', 'b', 'c'], True), ['a', 'b', 'd'])
        self.assertEqual(arranged(['x', 'y'], [], True), ['x', 'y'])

    def test_app_icons_as_gio_reads_the_icon_key(self):
        themed = {'firefox', 'org.gnome.TextEditor', 'org.gnome.Ptyxis'}.__contains__
        cases = {'firefox': 'firefox', 'firefox.png': 'firefox', 'org.gnome.TextEditor.svg': 'org.gnome.TextEditor',
                 'org.gnome.Ptyxis': 'org.gnome.Ptyxis',  # a dot before something else than an image suffix stays
                 '/opt/app/icon.png': '/opt/app/icon.png',  # a path is a file
                 '': GENERIC_APP_ICON,  # window:<wm_class>, no .desktop file
                 'not-in-the-theme': GENERIC_APP_ICON}
        for value, expected in cases.items():
            self.assertEqual(icon_name(app_icon(value, themed)), expected, value)
        # never both names: the generic icon of the user's theme would win over an app's icon in hicolor
        self.assertNotIn(GENERIC_APP_ICON, app_icon('firefox', themed).get_names())

    def test_every_state_of_a_row_has_an_icon_and_a_button_label(self):
        """APP_ICONS against apps.row_state over its whole table (docs/apps.md, section 7)."""
        path = Path('/h/.local/bin/x')
        fixes = (None, apps.Fix(apps.WRAPPER, path), apps.Fix(apps.WRAPPER, path, broken=True),
                 apps.Fix(apps.OVERRIDE, Path('/h/.local/share/applications/x.desktop'), pending=True))
        plans = (None, apps.Plan(apps.WRAPPER, path, 'x'), apps.Plan(reason=apps.FLATPAK_REASON), apps.Plan(reason='x'))
        evidence = (None, apps.Evidence(seen=True, ok_count=2, ok_at=1.0, last_ok=True),
                    apps.Evidence(seen=True, refusal='no_text', refusal_win='x11', x11_no_text=True),
                    apps.Evidence(seen=True, refusal='terminal', refusal_win='wayland'))
        kinds = (None,) + tuple(apps.EXPECTATIONS)
        reached = set()
        for enabled, kind, platform, fix, plan, found in itertools.product(
                (True, False), kinds, (None, 'x11', 'wayland'), fixes, plans, evidence):
            state = apps.row_state('X', enabled, kind, platform, fix, plan, found, 2.0)
            self.assertIn(state.icon, APP_ICONS, state)
            self.assertTrue(state.button is None or state.button in apps.BUTTONS, state)
            reached.add(state.state)
        self.assertEqual(reached, set(apps.ORDER))  # every row of the table

    def test_a_partial_row_looks_different_from_a_working_one(self):
        # review finding: the table gives GTK4 apps ◐, not the ✓ of an app without a limitation
        self.assertNotEqual(APP_ICONS['partial'][1], APP_ICONS['ok'][1])
        self.assertEqual(APP_ICONS['partial'][0], APP_ICONS['ok'][0])  # the colour of "works"
        theme = Path('/usr/share/icons/Adwaita')
        if not theme.is_dir():
            self.skipTest('no Adwaita icon theme here')
        for key, (_state, name, _label) in APP_ICONS.items():
            self.assertTrue(list(theme.glob('*/*/%s.*' % name)), '%s: %s is not in Adwaita' % (key, name))

    def test_flatpak_help_is_available_without_access_to_a_repository(self):
        self.assertEqual(WEBSITE, 'https://gramadrama.app')
        self.assertIn('flatpak override --user --socket=wayland APP_ID', FLATPAK_HELP)
        self.assertIn('usuwa wszystkie', FLATPAK_HELP)
        self.assertNotIn('github.com', FLATPAK_HELP)


if __name__ == '__main__':
    unittest.main()
