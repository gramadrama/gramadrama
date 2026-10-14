import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install_engine as I  # noqa: E402


class SourceTests(unittest.TestCase):
    def test_engine_goes_first_and_a_layout_always_stays(self):
        self.assertEqual(I.with_engine([('xkb', 'pl')]), [('ibus', 'gramadrama'), ('xkb', 'pl')])
        self.assertEqual(I.with_engine([('ibus', 'gramadrama'), ('xkb', 'pl')]), [('ibus', 'gramadrama'), ('xkb', 'pl')])
        self.assertEqual(I.with_engine([('xkb', 'pl'), ('xkb', 'us'), ('ibus', 'gramadrama')]),
                         [('ibus', 'gramadrama'), ('xkb', 'pl'), ('xkb', 'us')])
        self.assertEqual(I.with_engine([]), [('ibus', 'gramadrama'), ('xkb', 'pl')])
        self.assertEqual(I.with_engine([('ibus', 'anthy')]), [('ibus', 'gramadrama'), ('ibus', 'anthy'), ('xkb', 'pl')])

    def test_uninstall_restores_without_the_engine(self):
        self.assertEqual(I.without_engine([('ibus', 'gramadrama'), ('xkb', 'pl')]), [('xkb', 'pl')])
        self.assertEqual(I.without_engine([('ibus', 'gramadrama')]), [('xkb', 'pl')])
        self.assertEqual(I.without_engine([('xkb', 'de')]), [('xkb', 'de')])

    def test_keyboard_fallback_matches_selected_layout_without_removing_others(self):
        self.assertEqual(I.with_engine([], 'de'), [('ibus', 'gramadrama'), ('xkb', 'de')])
        self.assertEqual(I.without_engine([('ibus', 'gramadrama')], 'us'), [('xkb', 'us')])
        self.assertEqual(I.with_engine([('xkb', 'us'), ('xkb', 'de')], 'de'),
                         [('ibus', 'gramadrama'), ('xkb', 'us'), ('xkb', 'de')])


class ExtensionCopyTests(unittest.TestCase):
    def test_copy_reports_whether_the_panel_changed(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp, 'src'), Path(tmp, 'data', 'gnome-shell', 'extensions', I.EXTENSION_ID)
            (source / 'icons').mkdir(parents=True)
            (source / 'metadata.json').write_text('{"uuid": "%s"}' % I.EXTENSION_ID)
            (source / 'extension.js').write_text('// v1')
            (source / 'icons' / 'gramadrama.svg').write_text('<svg/>')
            (source / 'schemas.compiled').write_text('x')  # build output is never installed
            with mock.patch.object(I, 'EXTENSION_SOURCE', source), mock.patch.object(I, 'EXTENSION', target):
                self.assertTrue(I.copy_extension())
                self.assertEqual((target / 'icons' / 'gramadrama.svg').read_text(), '<svg/>')
                self.assertFalse((target / 'schemas.compiled').exists())
                inode = (target / 'extension.js').stat().st_ino  # copy2 keeps mtimes: a new file shows as a new inode
                self.assertFalse(I.copy_extension())  # identical: no new login needed
                self.assertEqual((target / 'extension.js').stat().st_ino, inode)  # and nothing rewritten
                (source / 'extension.js').write_text('// v2')
                self.assertTrue(I.copy_extension())
                (source / 'metadata.json').unlink()
                self.assertFalse(I.copy_extension())  # no extension in the repository: nothing to do


class ComponentTests(unittest.TestCase):
    def test_component_uses_the_selected_keyboard_without_changing_engine_identity(self):
        for layout, language in (('pl', 'pl'), ('us', 'en'), ('de', 'de')):
            with self.subTest(layout=layout):
                root = ET.fromstring(I.component_xml(layout))
                self.assertEqual(root.findtext('engines/engine/name'), 'gramadrama')
                self.assertEqual(root.findtext('engines/engine/layout'), layout)
                self.assertEqual(root.findtext('engines/engine/language'), language)
                self.assertNotIn('@LAYOUT@', I.component_xml(layout))
        with self.assertRaises(ValueError):
            I.component_xml('invalid')

    def test_component_exec_handles_install_paths_with_spaces(self):
        from unittest import mock
        from gi.repository import GLib
        path = Path('/tmp/example directory/engine')
        with mock.patch.object(I, 'LAUNCHER', path):
            command = ET.fromstring(I.component_xml('de')).findtext('exec')
        self.assertEqual(GLib.shell_parse_argv(command)[1], ['/usr/bin/python3', str(path), '--ibus'])

    def test_component_points_to_the_installed_copy(self):
        root = ET.fromstring(I.component_xml())
        self.assertEqual(root.findtext('exec'), '/usr/bin/python3 %s --ibus' % I.LAUNCHER)
        self.assertTrue(root.findtext('exec').startswith('/'))
        engine = root.find('engines/engine')
        self.assertEqual(engine.findtext('name'), 'gramadrama')
        self.assertEqual(engine.findtext('layout'), 'pl')  # GNOME ignores layout variants
        self.assertIsNone(engine.find('layout_variant'))



class AppInstallTests(unittest.TestCase):
    """The app's launcher, icon, version file and the removal of the old GTK app, in a temp dir."""

    def setUp(self):
        import tempfile
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        target = root / 'data' / 'gramadrama-engine'
        paths = {'TARGET': target, 'LAUNCHER': target / 'gramadrama-engine', 'APP_LAUNCHER': target / 'gramadrama-app',
                 'HOST_LAUNCHER': target / 'gramadrama-browser-host', 'BROWSER_EXTENSION_TARGET': target / 'browser-extension',
                 'CONFIG': root / 'config', 'MIMEAPPS': root / 'config' / 'mimeapps.list',
                 'STAGED_XML': target / 'gramadrama.xml', 'INSTALL_INFO': target / 'install.json',
                 'DESKTOP': root / 'data' / 'applications' / 'pl.gramadrama.Linux.desktop',
                 'ICON': root / 'data' / 'icons' / 'hicolor' / 'scalable' / 'apps' / 'pl.gramadrama.Linux.svg',
                 'BIN': root / 'bin' / 'gramadrama', 'LEGACY_APP': root / 'data' / 'gramadrama',
                 'LOCAL_BIN': root / 'bin', 'APPLICATIONS': root / 'data' / 'applications'}
        for name, value in paths.items():
            patcher = mock.patch.object(I, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_exec_line_round_trips_through_glib(self):
        from gi.repository import GLib
        odd = '/tmp/a b/$x"q\\z%/app`'
        value = I.desktop_exec('/usr/bin/python3', odd)
        key_file, text = GLib.KeyFile(), '[Desktop Entry]\nExec=%s\n' % value
        key_file.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
        command = key_file.get_string('Desktop Entry', 'Exec').replace('%%', '%')
        self.assertEqual(GLib.shell_parse_argv(command)[1], ['/usr/bin/python3', odd])

    def test_reinstall_preserves_explicit_keyboard_layout(self):
        I.copy_engine('de')
        self.assertEqual(ET.fromstring(I.STAGED_XML.read_text()).findtext('engines/engine/layout'), 'de')
        I.copy_engine()
        self.assertEqual(ET.fromstring(I.STAGED_XML.read_text()).findtext('engines/engine/layout'), 'de')
        I.copy_engine('us')
        self.assertEqual(json.loads(I.INSTALL_INFO.read_text())['keyboard_layout'], 'us')

    def test_copy_mirrors_the_package_and_records_the_version(self):
        stale = I.TARGET / 'gramadrama' / 'capture.py'
        stale.parent.mkdir(parents=True)
        stale.write_text('# the old GTK app')
        I.write_install_info(panel_changed_at=123.0, commit='old')
        self.assertTrue(I.copy_engine())  # a first copy: the engine changed
        changed_at = json.loads(I.INSTALL_INFO.read_text())['engine_changed_at']
        self.assertFalse(I.copy_engine())  # the same code again: the running engine may stay
        self.assertEqual(json.loads(I.INSTALL_INFO.read_text())['engine_changed_at'], changed_at)
        (I.TARGET / 'gramadrama' / 'app.py').write_text('# an older app')
        self.assertFalse(I.copy_engine())  # the app alone is not the engine
        (I.TARGET / 'gramadrama' / 'core.py').write_text('# an older core')
        self.assertTrue(I.copy_engine())  # the live service, a child as long-lived as the engine, keeps it
        (I.TARGET / 'gramadrama' / 'liveservice.py').write_text('# an older live service')
        self.assertTrue(I.copy_engine())
        (I.TARGET / 'gramadrama' / 'engine' / 'keys.py').write_text('# an older engine')
        self.assertTrue(I.copy_engine())
        catalogue = I.TARGET / 'gramadrama' / 'locales' / 'en' / 'sign-in.json'
        self.assertTrue(catalogue.exists())
        catalogue.write_text('{}')
        self.assertTrue(I.copy_engine())  # persistent children must reload updated translations
        self.assertFalse(stale.exists())  # deleted in the repository: gone from the copy too
        self.assertTrue((I.TARGET / 'gramadrama' / 'app.py').exists())
        self.assertFalse((I.TARGET / 'gramadrama.new').exists() or (I.TARGET / 'gramadrama.old').exists())
        for launcher in (I.LAUNCHER, I.APP_LAUNCHER):
            self.assertTrue(launcher.stat().st_mode & 0o100)
        self.assertTrue((I.TARGET / 'icon.svg').exists())
        info = json.loads(I.INSTALL_INFO.read_text())
        self.assertEqual(info['source'], str(I.SOURCE))
        self.assertEqual(info['panel_changed_at'], 123.0)  # kept until the panel changes again
        self.assertIn('installed_at', info)
        self.assertNotEqual(info.get('commit'), 'old')

    def test_source_version_reads_the_shared_version_file(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / 'VERSION'
            version.write_text('0.6.0\n')
            with mock.patch.object(I, 'SOURCE', Path(tmp) / 'linux'), mock.patch.object(I, 'VERSION_FILE', version):
                self.assertEqual(I.source_version(), {'version': '0.6.0'})  # no git here: no commit
                version.unlink()
                self.assertEqual(I.source_version(), {})
        self.assertEqual(I.VERSION_FILE.read_text().strip(), I.source_version()['version'])  # the repository's own

    def test_install_info_merges_and_drops_none(self):
        I.TARGET.mkdir(parents=True)
        I.write_install_info(commit='abc', dirty=True)
        I.write_install_info(dirty=None, source='/repo')
        self.assertEqual(json.loads(I.INSTALL_INFO.read_text()), {'commit': 'abc', 'source': '/repo'})
        I.INSTALL_INFO.write_text('not json')
        I.write_install_info(commit='def')
        self.assertEqual(json.loads(I.INSTALL_INFO.read_text()), {'commit': 'def'})

    def test_model_live_change_alone_requires_engine_reload(self):
        import shutil
        from unittest import mock
        source = self.root / 'source'
        shutil.copytree(I.SOURCE / 'gramadrama', source / 'gramadrama',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (source / 'data').mkdir()
        shutil.copy2(I.SOURCE / 'data' / 'gramadrama.xml.in', source / 'data' / 'gramadrama.xml.in')
        for name in ('gramadrama-engine', 'gramadrama-app', 'gramadrama-browser-host', 'icon.svg'):
            shutil.copy2(I.SOURCE / name, source / name)
        model_live = source / 'gramadrama' / 'model_live.py'
        model_live.write_text('# model checker version 1\n')
        with mock.patch.object(I, 'SOURCE', source), mock.patch.object(I, 'source_version', return_value={}), \
                mock.patch.object(I, 'copy_browser_extension', return_value=False):
            with mock.patch.object(I.time, 'time', return_value=100.0):
                self.assertTrue(I.copy_engine())
            self.assertFalse(I.copy_engine())
            model_live.write_text('# model checker version 2\n')
            with mock.patch.object(I.time, 'time', return_value=200.0):
                self.assertTrue(I.copy_engine())
            self.assertEqual((I.TARGET / 'gramadrama' / 'model_live.py').read_text(), model_live.read_text())
            self.assertEqual(I.read_install_info()['engine_changed_at'], 200.0)
            self.assertFalse(I.copy_engine())

    def test_app_launchers_replace_the_old_app_but_never_foreign_files(self):
        # exactly what the old install.py wrote
        I.DESKTOP.parent.mkdir(parents=True)
        I.DESKTOP.write_text('[Desktop Entry]\nType=Application\nName=GramaDrama\nComment=Pisz bez dramatu — korekta '
                             'i tłumaczenie PL/EN\nExec="%s"\nIcon=pl.gramadrama.Linux\nTerminal=false\n'
                             'Categories=Office;Utility;\nStartupNotify=true\n' % I.BIN)
        I.BIN.parent.mkdir(parents=True)
        I.BIN.write_text('#!/bin/sh\nset -eu\ncd %s\nexec /usr/bin/python3 -m gramadrama "$@"\n' % I.LEGACY_APP)
        I.install_app()
        self.assertIn(str(I.APP_LAUNCHER), I.DESKTOP.read_text())
        self.assertIn('Icon=pl.gramadrama.Linux', I.DESKTOP.read_text())
        self.assertEqual(I.BIN.read_text(), I.bin_script())
        self.assertEqual(I.ICON.read_bytes(), (I.SOURCE / 'icon.svg').read_bytes())
        I.install_app()  # again: our own files stay ours
        self.assertIn(str(I.APP_LAUNCHER), I.DESKTOP.read_text())
        I.BIN.write_text('#!/bin/sh\necho my own script\n')
        I.install_app()
        self.assertEqual(I.BIN.read_text(), '#!/bin/sh\necho my own script\n')
        I.uninstall_app()
        self.assertTrue(I.BIN.exists())  # not ours
        self.assertFalse(I.DESKTOP.exists() or I.ICON.exists())
        other = '[Desktop Entry]\nName=Something\nExec=%s-engine/x\n' % I.LEGACY_APP  # a path that merely starts alike
        I.DESKTOP.write_text(other)
        I.install_app()
        I.uninstall_app()
        self.assertEqual(I.DESKTOP.read_text(), other)

    def test_the_app_takes_over_the_address_the_browser_comes_back_to_from_a_login(self):
        from gi.repository import GLib
        entry = I.desktop_entry()
        key_file = GLib.KeyFile()
        key_file.load_from_data(entry, len(entry.encode()), GLib.KeyFileFlags.NONE)
        self.assertEqual(key_file.get_string_list('Desktop Entry', 'MimeType'), ['x-scheme-handler/gramadrama'])
        command = key_file.get_string('Desktop Entry', 'Exec')
        self.assertTrue(command.endswith(' %u'), command)  # the address is the one argument: gramadrama-app URL
        self.assertEqual(GLib.shell_parse_argv(command.replace('%u', ''))[1], ['/usr/bin/python3', str(I.APP_LAUNCHER)])

    def test_uninstall_takes_only_our_line_out_of_mimeapps(self):
        text = ('[Default Applications]\nx-scheme-handler/gramadrama=pl.gramadrama.Linux.desktop;\ntext/plain=a.desktop;\n'
                '[Added Associations]\nx-scheme-handler/gramadrama=other.desktop;pl.gramadrama.Linux.desktop;\n')
        self.assertEqual(I.without_scheme(text), '[Default Applications]\ntext/plain=a.desktop;\n[Added Associations]\n'
                                                  'x-scheme-handler/gramadrama=other.desktop;\n')
        self.assertEqual(I.without_scheme('text/plain=a.desktop;\n'), 'text/plain=a.desktop;\n')
        from unittest import mock
        mimeapps = I.DESKTOP.parent / 'mimeapps.list'
        mimeapps.parent.mkdir(parents=True, exist_ok=True)
        mimeapps.write_text(text)
        with mock.patch.object(I, 'MIMEAPPS', mimeapps):
            I.unregister_scheme()
            self.assertNotIn('pl.gramadrama.Linux', mimeapps.read_text())
            mimeapps.unlink()
            I.unregister_scheme()  # nothing to take out is no error

    def test_uninstall_leaves_an_old_app_that_was_never_replaced(self):
        I.DESKTOP.parent.mkdir(parents=True)
        legacy = '[Desktop Entry]\nExec="%s"\nIcon=pl.gramadrama.Linux\n' % I.BIN
        I.DESKTOP.write_text(legacy)
        I.ICON.parent.mkdir(parents=True)
        I.ICON.write_text('<svg/>')
        I.uninstall_app()
        self.assertEqual(I.DESKTOP.read_text(), legacy)
        self.assertTrue(I.ICON.exists())

    def test_engines_older_than_their_code_restart(self):
        import os
        import time
        me = os.getpid()
        started = I.process_start(me)
        self.assertIsNotNone(started)
        self.assertEqual(I.stale_engines([me], None), [])  # nothing recorded: nothing to restart
        self.assertEqual(I.stale_engines([me], started - 60), [])  # started after the change
        self.assertEqual(I.stale_engines([me], time.time()), [me])  # e.g. install stopped at the sudo step
        self.assertFalse(I.legacy_process(me))

    def test_xdg_dirs_must_be_absolute(self):
        from unittest import mock
        with mock.patch.dict('os.environ', {'XDG_DATA_HOME': 'relative'}):
            self.assertEqual(I.xdg('XDG_DATA_HOME', Path('/default')), Path('/default'))
        with mock.patch.dict('os.environ', {'XDG_DATA_HOME': ''}):
            self.assertEqual(I.xdg('XDG_DATA_HOME', Path('/default')), Path('/default'))
        with mock.patch.dict('os.environ', {'XDG_DATA_HOME': '/abs'}):
            self.assertEqual(I.xdg('XDG_DATA_HOME', Path('/default')), Path('/abs'))

    def test_a_symlinked_old_copy_is_left_alone(self):
        real = self.root / 'elsewhere'
        (real / 'gramadrama').mkdir(parents=True)
        (real / 'gramadrama' / 'app.py').write_text('# old')
        I.LEGACY_APP.parent.mkdir(parents=True, exist_ok=True)
        I.LEGACY_APP.symlink_to(real)
        self.assertFalse(I.remove_legacy_copy())
        self.assertTrue((real / 'gramadrama' / 'app.py').exists())

    def test_the_old_copy_goes_only_when_it_is_the_old_app(self):
        self.assertFalse(I.remove_legacy_copy())
        (I.LEGACY_APP / 'gramadrama').mkdir(parents=True)
        (I.LEGACY_APP / 'gramadrama' / 'app.py').write_text('# old')
        (I.LEGACY_APP / 'notes.txt').write_text('something of the user')
        self.assertFalse(I.remove_legacy_copy())
        (I.LEGACY_APP / 'notes.txt').unlink()
        self.assertTrue(I.remove_legacy_copy())
        self.assertFalse(I.LEGACY_APP.exists())

    def launch_fixes(self):
        """The app's launch fixes (docs/apps.md) and files that only look alike: {path: text}."""
        wrapper = ('#!/bin/sh\n# GramaDrama: run ChatGPT natively on Wayland, so IBus engines get the field text.\n'
                   '# Delete this file to undo.\nexec /usr/bin/chatgpt --ozone-platform=wayland "$@"\n')
        override = ('# GramaDrama: run Slack natively on Wayland, so IBus engines get the field text.\n'
                    '# Delete this file to undo. It is a copy of /usr/share/applications/slack.desktop with '
                    '--ozone-platform=wayland.\n[Desktop Entry]\nType=Application\nName=Slack\n'
                    'Exec=/usr/bin/slack --ozone-platform=wayland %U\n')
        files = {I.LOCAL_BIN / 'chatgpt': wrapper, I.APPLICATIONS / 'slack.desktop': override,
                 I.LOCAL_BIN / 'mine': '#!/bin/sh\n# my own GramaDrama: notes\nexec /usr/bin/true\n',
                 I.APPLICATIONS / 'com.anthropic.Claude.desktop': '[Desktop Entry]\nName=Claude\nExec=claude-desktop %U\n',
                 I.APPLICATIONS / 'notes.txt': '# GramaDrama: not a .desktop file\n'}
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (I.LOCAL_BIN / 'link').symlink_to(I.LOCAL_BIN / 'chatgpt')  # a symlink is never ours
        return files

    def test_only_marked_launch_fixes_are_removed(self):
        files = self.launch_fixes()
        ours = [I.LOCAL_BIN / 'chatgpt', I.APPLICATIONS / 'slack.desktop']
        self.assertEqual(I.remove_launch_fixes(), ours)
        for path, text in files.items():
            if path in ours:
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_text(), text)
        self.assertTrue((I.LOCAL_BIN / 'link').is_symlink())
        self.assertEqual(I.remove_launch_fixes(), [])

    def test_install_keeps_the_launch_fixes_and_uninstall_removes_them(self):
        import contextlib
        import io
        from unittest import mock
        files = self.launch_fixes()
        apps_json = self.root / 'config' / 'gramadrama' / 'apps.json'
        apps_json.parent.mkdir(parents=True)
        apps_json.write_text('{"v": 1, "disabled": ["org.gnome.Ptyxis.desktop"], "added": []}')
        live = {'get_sources': mock.Mock(return_value=[('xkb', 'pl')]), 'set_sources': mock.Mock(),
                'enabled_extensions': mock.Mock(return_value=[]), 'set_enabled_extensions': mock.Mock(),
                'old_binding': mock.Mock(return_value=None), 'set_old_binding': mock.Mock(),
                'engine_pids': mock.Mock(return_value=[]), 'app_pids': mock.Mock(return_value=[]),
                'registered': mock.Mock(return_value=True),
                'global_engine': mock.Mock(return_value='gramadrama'), 'ibus_cli': mock.Mock(return_value=None),
                'stop_legacy_app': mock.Mock(return_value=None), 'remove_legacy_shortcut': mock.Mock(return_value=False),
                'register_scheme': mock.Mock(return_value=True),
                'settings': mock.Mock(side_effect=AssertionError('live GSettings')),
                'COMPONENT': self.root / 'ibus' / 'gramadrama.xml', 'BACKUP': apps_json.with_name('engine-install-backup.json'),
                'SYSTEM_LAUNCHER': self.root / 'no-package' / 'gramadrama-engine',
                'EXTENSION': self.root / 'data' / 'gnome-shell' / 'extensions' / I.EXTENSION_ID,
                'EXTENSION_SOURCE': self.root / 'no-extension'}
        for name, value in live.items():
            patcher = mock.patch.object(I, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        I.COMPONENT.parent.mkdir()
        I.COMPONENT.write_text(I.component_xml(), encoding='utf-8')  # the sudo step is done
        with mock.patch('os.kill', side_effect=AssertionError('a signal')), \
                mock.patch.object(I, 'source_version', return_value={}), \
                mock.patch.object(I.subprocess, 'run') as run, \
                mock.patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(I.install(), 0)
        run.assert_called_once_with(['systemctl', '--user', 'restart', I.IBUS_UNIT], check=True)
        for path, text in files.items():
            self.assertEqual(path.read_text(), text)  # install never touches them
        I.app_pids.return_value = [4242]  # the app's window is open
        with mock.patch('os.kill') as kill, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(I.uninstall(), 0)
        kill.assert_called_once_with(4242, 15)
        self.assertIn('Zamknięto okno GramaDramy.', out.getvalue())
        self.assertFalse((I.LOCAL_BIN / 'chatgpt').exists() or (I.APPLICATIONS / 'slack.desktop').exists())
        self.assertIn('Usunięto poprawkę uruchamiania: %s' % (I.LOCAL_BIN / 'chatgpt'), out.getvalue())
        for path in (I.LOCAL_BIN / 'mine', I.APPLICATIONS / 'com.anthropic.Claude.desktop', I.APPLICATIONS / 'notes.txt'):
            self.assertEqual(path.read_text(), files[path])
        self.assertEqual(json.loads(apps_json.read_text())['disabled'], ['org.gnome.Ptyxis.desktop'])  # the list stays
        self.assertFalse(I.TARGET.exists() or I.BIN.exists() or I.DESKTOP.exists() or I.BACKUP.exists())



class ComponentReloadTests(unittest.TestCase):
    """Synthetic install/resume runs: no desktop session, subprocesses or signals."""

    def setUp(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        paths = {'TARGET': root, 'STAGED_XML': root / 'staged.xml', 'COMPONENT': root / 'system.xml',
                 'INSTALL_INFO': root / 'install.json', 'BACKUP': root / 'backup.json',
                 'LAUNCHER': root / 'gramadrama-engine', 'EXTENSION': root / 'no-extension',
                 'SYSTEM_LAUNCHER': root / 'no-package' / 'gramadrama-engine'}
        for name, value in paths.items():
            stack.enter_context(mock.patch.object(I, name, value))
        live = {'stop_legacy_app': None, 'install_app': None, 'register_browser_hosts': [],
                'register_scheme': True, 'registered': True, 'get_sources': [('xkb', 'pl')],
                'set_sources': None, 'old_binding': None, 'enabled_extensions': [],
                'engine_pids': [], 'global_engine': 'gramadrama', 'copy_extension': False,
                'remove_legacy_copy': False, 'remove_legacy_shortcut': False}
        self.live = {name: stack.enter_context(mock.patch.object(I, name, return_value=value))
                     for name, value in live.items()}
        for name in ('settings', 'set_old_binding', 'set_enabled_extensions', 'ibus_cli'):
            stack.enter_context(mock.patch.object(I, name, side_effect=AssertionError('unexpected live call')))
        stack.enter_context(mock.patch.object(I.os, 'kill', side_effect=AssertionError('a signal')))
        self.run = stack.enter_context(mock.patch.object(I.subprocess, 'run'))
        stack.enter_context(mock.patch.object(I, 'wait', side_effect=lambda predicate, seconds: predicate()))
        stack.enter_context(mock.patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'GNOME'}))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

        def stage(layout=None):
            layout = layout or I.read_install_info().get('keyboard_layout', 'pl')
            I.STAGED_XML.write_text(I.component_xml(layout), encoding='utf-8')
            I.write_install_info(keyboard_layout=layout)
        stack.enter_context(mock.patch.object(I, 'copy_engine', side_effect=stage))
        stage()
        I.COMPONENT.write_text(I.STAGED_XML.read_text(encoding='utf-8'), encoding='utf-8')

    def test_unknown_cached_component_reloads_once_and_preserves_keyboard(self):
        self.assertEqual(I.install(), 0)
        self.run.assert_called_once_with(['systemctl', '--user', 'restart', I.IBUS_UNIT], check=True)
        self.assertTrue(I.read_install_info().get('registered_component_digest'))
        self.run.reset_mock()
        self.assertEqual(I.install(), 0)
        self.run.assert_not_called()
        self.assertEqual(I.read_install_info()['keyboard_layout'], 'pl')

    def test_sudo_resume_reloads_even_if_the_engine_name_and_layout_are_registered(self):
        self.assertEqual(I.install(), 0)
        self.run.reset_mock()
        I.COMPONENT.write_text('<component><exec>/tmp/obsolete-engine</exec></component>', encoding='utf-8')
        self.assertEqual(I.install(), 2)
        self.run.assert_not_called()
        self.assertNotIn('registered_component_digest', I.read_install_info())
        # Simulate only the administrator's XML copy; IBus still advertises its cached engine.
        I.COMPONENT.write_text(I.STAGED_XML.read_text(encoding='utf-8'), encoding='utf-8')
        self.assertEqual(I.install(), 0)
        self.run.assert_called_once_with(['systemctl', '--user', 'restart', I.IBUS_UNIT], check=True)
        self.run.reset_mock()
        self.assertEqual(I.install(), 0)
        self.run.assert_not_called()

    def test_failed_restart_does_not_record_component_as_loaded(self):
        import subprocess
        self.run.side_effect = subprocess.CalledProcessError(1, ['systemctl'])
        with self.assertRaises(subprocess.CalledProcessError):
            I.install()
        self.assertNotIn('registered_component_digest', I.read_install_info())
        self.live['set_sources'].assert_not_called()
        self.run.side_effect = None
        self.assertEqual(I.install(), 0)
        self.assertEqual(self.run.call_count, 2)

    def test_failed_registration_does_not_record_component_as_loaded(self):
        self.live['registered'].return_value = False
        with self.assertRaises(SystemExit):
            I.install()
        self.assertNotIn('registered_component_digest', I.read_install_info())
        self.live['set_sources'].assert_not_called()
        self.live['registered'].return_value = True
        self.assertEqual(I.install(), 0)
        self.assertEqual(self.run.call_count, 2)


class BrowserExtensionTests(unittest.TestCase):
    """The browser extension's side of the installer: one extension for the Mac and Linux, a host registered per browser."""

    def setUp(self):
        import tempfile
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in {'CONFIG': root / 'config', 'TARGET': root / 'engine', 'BROWSER_EXTENSION_TARGET': root / 'engine' / 'browser-extension',
                            'HOST_LAUNCHER': root / 'engine' / 'gramadrama-browser-host'}.items():
            patcher = mock.patch.object(I, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_there_is_one_extension_the_macs_too(self):
        """No copy of the extension in linux/: the installer takes browser/extension, which the Mac loads as it is."""
        repo = Path(I.__file__).resolve().parent.parent
        self.assertEqual(I.BROWSER_EXTENSION_SOURCE, repo / 'browser' / 'extension')
        self.assertTrue((I.BROWSER_EXTENSION_SOURCE / 'manifest.json').is_file())
        copies = [p for p in (repo / 'linux').rglob('*') if p.name in ('content.js', 'background.js', 'popup.js')
                  and 'gnome-extension' not in p.parts and 'research' not in p.parts]
        self.assertEqual(copies, [], 'a copy of the browser extension in linux/')

    def test_a_host_is_registered_only_in_the_browsers_that_are_there(self):
        for relative in ('google-chrome', 'BraveSoftware/Brave-Browser'):
            (I.CONFIG / relative).mkdir(parents=True)
        self.assertEqual(I.register_browser_hosts(), ['Google Chrome', 'Brave'])
        manifest = json.loads((I.CONFIG / 'google-chrome' / 'NativeMessagingHosts' / 'pl.gramadrama.browser.json').read_text())
        self.assertEqual(manifest, {'name': 'pl.gramadrama.browser', 'description': manifest['description'],
                                    'path': str(I.HOST_LAUNCHER), 'type': 'stdio',
                                    'allowed_origins': ['chrome-extension://hlnpnkiancoohmafifihcjndfmhilkfe/']})
        self.assertFalse((I.CONFIG / 'chromium').exists())  # a browser never run gets nothing made for it
        self.assertFalse((I.CONFIG / 'microsoft-edge').exists())

    def test_the_extension_is_copied_whole_and_mirrored(self):
        self.assertTrue(I.copy_browser_extension())
        self.assertEqual(json.loads((I.BROWSER_EXTENSION_TARGET / 'manifest.json').read_text())['name'], 'GramaDrama')
        (I.BROWSER_EXTENSION_TARGET / 'stale.js').write_text('// deleted in the repository')
        self.assertTrue(I.copy_browser_extension())
        self.assertFalse((I.BROWSER_EXTENSION_TARGET / 'stale.js').exists())
        self.assertTrue((I.BROWSER_EXTENSION_TARGET / 'content.js').is_file())

    def test_uninstall_takes_only_our_manifests(self):
        for relative in ('google-chrome', 'chromium'):
            (I.CONFIG / relative).mkdir(parents=True)
        I.register_browser_hosts()
        other = I.CONFIG / 'chromium' / 'NativeMessagingHosts' / 'pl.gramadrama.browser.json'
        other.write_text(json.dumps({'name': 'pl.gramadrama.browser', 'path': '/opt/somebody/else', 'type': 'stdio'}))
        self.assertEqual(I.unregister_browser_hosts(), ['Google Chrome'])
        self.assertTrue(other.exists())  # another path: not ours
        self.assertEqual(I.unregister_browser_hosts(), [])


if __name__ == '__main__':
    unittest.main()


class AppPidsTests(unittest.TestCase):
    def test_only_the_installed_app(self):
        from unittest import mock
        with mock.patch.object(I.subprocess, 'run', return_value=mock.Mock(stdout='12 34\n')) as run:
            self.assertEqual(I.app_pids(), [12, 34])
        self.assertEqual(run.call_args[0][0], ['pgrep', '-f', '^/usr/bin/python3 %s' % I.APP_LAUNCHER])


class PackageTests(unittest.TestCase):
    def test_the_source_install_gives_way_to_the_package(self):
        """One IBus description, one engine: with the .deb installed the source install changes nothing and says why."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / 'gramadrama-engine'
            launcher.write_text('the package')
            with mock.patch.object(I, 'SYSTEM_LAUNCHER', launcher), \
                    mock.patch.object(I, 'copy_engine', side_effect=AssertionError('a file copied')), \
                    self.assertRaises(SystemExit) as stopped:
                I.install()
        self.assertIn('sudo apt remove gramadrama', str(stopped.exception))


class AccountTests(unittest.TestCase):
    """The package (.deb): setup and teardown on one account, synthetic (no session, subprocesses or signals)."""

    def setUp(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = self.root = Path(temporary.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        (root / 'lib').mkdir()
        (root / 'lib' / 'gramadrama-engine').write_text('the package engine')
        (root / 'extension').mkdir()
        paths = {'SYSTEM_LAUNCHER': root / 'lib' / 'gramadrama-engine', 'SYSTEM_DIR': root / 'lib',
                 'SYSTEM_EXTENSION': root / 'extension', 'COMPONENT': root / 'component.xml',
                 'BACKUP': root / 'config' / 'backup.json', 'EXTENSION': root / 'no-user-extension'}
        for name, value in paths.items():
            stack.enter_context(mock.patch.object(I, name, value))
        self.sources = [('xkb', 'gb')]
        self.enabled = ['other@example.com']
        live = {'get_sources': mock.Mock(side_effect=lambda: list(self.sources)),
                'set_sources': mock.Mock(side_effect=lambda sources: setattr(self, 'sources', list(sources))),
                'enabled_extensions': mock.Mock(side_effect=lambda: list(self.enabled)),
                'set_enabled_extensions': mock.Mock(side_effect=lambda uuids: setattr(self, 'enabled', list(uuids))),
                'old_binding': mock.Mock(return_value=None), 'set_old_binding': mock.Mock(),
                'registered': mock.Mock(return_value=True),
                'global_engine': mock.Mock(side_effect=lambda: next((n for k, n in self.sources if k == 'ibus'), None)),
                'ibus_cli': mock.Mock(), 'wait': mock.Mock(side_effect=lambda predicate, seconds: predicate()),
                'register_browser_hosts': mock.Mock(return_value=[]), 'register_scheme': mock.Mock(return_value=True),
                'remove_legacy_copy': mock.Mock(return_value=False), 'remove_legacy_shortcut': mock.Mock(return_value=False),
                'copy_extension': mock.Mock(side_effect=AssertionError('the package has its own panel')),
                'package_engine_pids': mock.Mock(return_value=[4242]), 'unregister_scheme': mock.Mock(),
                'unregister_browser_hosts': mock.Mock(return_value=[]), 'remove_launch_fixes': mock.Mock(return_value=[])}
        self.live = {name: stack.enter_context(mock.patch.object(I, name, value)) for name, value in live.items()}
        self.run = stack.enter_context(mock.patch.object(I.subprocess, 'run'))
        self.kill = stack.enter_context(mock.patch.object(I.os, 'kill'))
        stack.enter_context(mock.patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}))
        self.out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_setup_needs_the_package(self):
        (self.root / 'lib' / 'gramadrama-engine').unlink()
        with self.assertRaises(SystemExit) as stopped:
            I.setup()
        self.assertIn('nie jest zainstalowana', str(stopped.exception))
        self.live['set_sources'].assert_not_called()

    def test_setup_on_this_account(self):
        self.assertEqual(I.setup(), 0)
        self.live['register_browser_hosts'].assert_called_once_with(self.root / 'lib' / 'gramadrama-browser-host')
        self.live['register_scheme'].assert_called_once_with(package=True)
        self.run.assert_not_called()  # IBus knew the engine: no restart
        # a British layout: the package's US engine, the user's own layout stays as the fallback
        self.assertEqual(self.sources, [('ibus', 'gramadrama-us'), ('xkb', 'gb')])
        self.live['registered'].assert_called_with('gramadrama-us')
        self.assertEqual(self.enabled, ['other@example.com', I.EXTENSION_ID])
        self.assertIn('po wylogowaniu', self.out.getvalue())
        self.assertTrue(I.BACKUP.exists())

    def test_setup_restarts_an_ibus_that_does_not_know_the_engine(self):
        answers = iter([False, True])
        self.live['registered'].side_effect = lambda engine: next(answers)
        self.sources = [('xkb', 'de+nodeadkeys')]
        self.assertEqual(I.setup(), 0)
        self.run.assert_called_once_with(['systemctl', '--user', 'restart', I.IBUS_UNIT], check=True)
        self.assertEqual(self.sources, [('ibus', 'gramadrama-de'), ('xkb', 'de+nodeadkeys')])

    def test_one_description_one_engine_per_layout(self):
        import xml.etree.ElementTree as ET
        with mock_patch(I, 'SYSTEM_LAUNCHER', Path('/usr/lib/gramadrama/gramadrama-engine')):
            root = ET.fromstring(I.package_component_xml())
        self.assertEqual(root.findtext('exec'), '/usr/bin/python3 /usr/lib/gramadrama/gramadrama-engine --ibus')
        self.assertEqual([(e.findtext('name'), e.findtext('layout')) for e in root.iter('engine')],
                         [('gramadrama', 'pl'), ('gramadrama-us', 'us'), ('gramadrama-de', 'de')])
        self.assertEqual(I.account_engine([('xkb', 'pl')]), 'gramadrama')
        self.assertEqual(I.account_engine([('ibus', 'gramadrama'), ('xkb', 'fr')]), 'gramadrama-us')  # others: US
        self.assertEqual(I.account_engine([]), 'gramadrama')
        # one engine of ours at a time: switching engines replaces it
        self.assertEqual(I.with_engine([('ibus', 'gramadrama'), ('xkb', 'de')], 'de', 'gramadrama-de'),
                         [('ibus', 'gramadrama-de'), ('xkb', 'de')])
        self.assertEqual(I.without_engine([('ibus', 'gramadrama-us'), ('xkb', 'us')]), [('xkb', 'us')])

    def test_teardown_restores_the_account(self):
        I.setup()
        self.live['old_binding'].return_value = ''
        I.teardown()
        self.assertEqual(self.sources, [('xkb', 'gb')])
        self.assertEqual(self.enabled, ['other@example.com'])
        self.kill.assert_called_once_with(4242, 15)
        self.live['unregister_browser_hosts'].assert_called_once_with(self.root / 'lib')
        self.live['unregister_scheme'].assert_called_once_with()
        self.assertFalse(I.BACKUP.exists())
        self.assertIn('sudo apt remove gramadrama', self.out.getvalue())


def mock_patch(target, name, value):
    from unittest import mock
    return mock.patch.object(target, name, value)

