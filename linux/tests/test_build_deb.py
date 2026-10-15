import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_deb as B  # noqa: E402
import install_engine as I  # noqa: E402
from gramadrama import checks  # noqa: E402

LIB = '/usr/lib/gramadrama/'
EXECUTABLES = {'/usr/bin/gramadrama', LIB + 'gramadrama-engine', LIB + 'gramadrama-app', LIB + 'gramadrama-browser-host',
               LIB + 'install_engine.py'}


@unittest.skipUnless(shutil.which('dpkg-deb'), 'needs dpkg-deb')
class PackageBuildTests(unittest.TestCase):
    """One build of the package from this tree, read back with dpkg-deb as apt would see it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.deb = B.build(Path(cls.tmp.name))
        cls.root = Path(cls.tmp.name, 'unpacked')
        subprocess.run(['dpkg-deb', '-x', str(cls.deb), str(cls.root)], check=True)
        listing = subprocess.run(['dpkg-deb', '-c', str(cls.deb)], check=True, capture_output=True, text=True).stdout
        cls.entries = {}
        for line in listing.splitlines():
            mode, owner, _size, _date, _time, name = line.split(None, 5)
            cls.entries['/' + name.lstrip('./').rstrip('/')] = (mode, owner)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def field(self, name):
        return subprocess.run(['dpkg-deb', '-f', str(self.deb), name], check=True, capture_output=True,
                              text=True).stdout.strip()

    def files(self):
        return {name for name, (mode, _) in self.entries.items() if mode.startswith('-')}

    def test_control_describes_an_architecture_independent_package(self):
        self.assertEqual(self.field('Package'), 'gramadrama')
        self.assertEqual(self.field('Architecture'), 'all')
        version = (B.ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertTrue(self.field('Version').startswith(version + '+git'), self.field('Version'))
        self.assertEqual(self.deb.name, 'gramadrama_%s_all.deb' % self.field('Version'))
        depends = self.field('Depends')
        for needed in ('python3-gi', 'gir1.2-ibus-1.0', 'gir1.2-gtk-4.0', 'gir1.2-adw-1', 'ibus', 'gnome-shell'):
            self.assertIn(needed, depends)
        self.assertIn('hunspell-pl', self.field('Recommends'))

    def test_the_package_ships_what_the_installer_and_the_app_look_for(self):
        files = self.files()
        for path in (str(I.SYSTEM_LAUNCHER), LIB + 'gramadrama-app', LIB + 'gramadrama-browser-host',
                     LIB + 'install_engine.py', LIB + 'data/gramadrama.xml.in', LIB + 'icon.svg', LIB + 'install.json',
                     LIB + 'gramadrama.xml', LIB + 'browser-extension/manifest.json', str(I.COMPONENT),
                     str(I.SYSTEM_DESKTOP), str(I.SYSTEM_EXTENSION / 'metadata.json'), '/usr/bin/gramadrama',
                     '/usr/share/icons/hicolor/scalable/apps/%s.svg' % I.APP_ID, '/usr/share/doc/gramadrama/copyright',
                     '/usr/share/doc/gramadrama/changelog.Debian.gz'):
            self.assertIn(path, files)
        self.assertEqual(Path(LIB), checks.SYSTEM_DIR)
        self.assertEqual(I.SYSTEM_EXTENSION.parent, checks.SYSTEM_EXTENSIONS)

    def test_the_python_package_is_the_tracked_code_without_caches(self):
        shipped = {name[len(LIB + 'gramadrama/'):] for name in self.files() if name.startswith(LIB + 'gramadrama/')}
        tracked = {str(p.relative_to(B.LINUX / 'gramadrama')) for p in B.tracked(B.LINUX / 'gramadrama') if p.exists()}
        self.assertEqual(shipped, tracked)
        self.assertFalse([name for name in self.entries if '__pycache__' in name or name.endswith('.pyc')])

    def test_owners_and_modes(self):
        for name, (mode, owner) in self.entries.items():
            self.assertEqual(owner, 'root/root', name)
            if mode.startswith('d'):
                self.assertEqual(mode, 'drwxr-xr-x', name)
            else:
                self.assertEqual(mode, '-rwxr-xr-x' if name in EXECUTABLES else '-rw-r--r--', name)

    def test_the_ibus_description_starts_the_package_engine(self):
        component = self.root / str(I.COMPONENT).lstrip('/')
        self.assertEqual(component.read_text(encoding='utf-8'), I.package_component_xml())
        self.assertEqual((self.root / LIB.lstrip('/') / 'gramadrama.xml').read_bytes(), component.read_bytes())
        self.assertEqual(checks.component_launcher(component), checks.SYSTEM_DIR / 'gramadrama-engine')

    def test_the_panel_accepts_the_package_engine(self):
        extension = self.root / str(I.SYSTEM_EXTENSION).lstrip('/')
        self.assertEqual(json.loads((extension / 'metadata.json').read_text(encoding='utf-8'))['uuid'], I.EXTENSION_ID)
        self.assertIn("SYSTEM_LAUNCHER = '%s'" % I.SYSTEM_LAUNCHER, (extension / 'policy.js').read_text(encoding='utf-8'))

    def test_launchers_point_into_the_package(self):
        desktop = (self.root / str(I.SYSTEM_DESKTOP).lstrip('/')).read_text(encoding='utf-8')
        self.assertIn('Exec="/usr/bin/python3" "%sgramadrama-app" %%u' % LIB, desktop)
        self.assertIn('MimeType=%s;' % I.SCHEME_TYPE, desktop)
        self.assertIn(LIB + 'gramadrama-app', (self.root / 'usr/bin/gramadrama').read_text(encoding='utf-8'))

    def test_install_info_tells_the_app_the_version(self):
        info = json.loads((self.root / LIB.lstrip('/') / 'install.json').read_text(encoding='utf-8'))
        self.assertEqual(info['package'], self.field('Version'))
        self.assertEqual(info['version'], (B.ROOT / 'VERSION').read_text(encoding='utf-8').strip())
        self.assertTrue(checks.version_label(info).startswith(info['version']))

    def test_md5sums_cover_every_file(self):
        control = Path(self.tmp.name, 'control')
        subprocess.run(['dpkg-deb', '-e', str(self.deb), str(control)], check=True)
        listed = {'/' + line.split(None, 1)[1] for line in (control / 'md5sums').read_text().splitlines()}
        self.assertEqual(listed, self.files())
        for script in ('preinst', 'postinst', 'prerm'):
            self.assertTrue((control / script).stat().st_mode & 0o111, script)


class PreinstTests(unittest.TestCase):
    """The package does not go over a copy from the installer that is still there (its IBus description, written by
    the installer's sudo step, would be replaced, and that account's own panel would refuse the package's engine)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.component = self.root / 'component' / 'gramadrama.xml'
        self.component.parent.mkdir()
        self.copy = self.root / 'home' / '.local' / 'share' / 'gramadrama-engine'
        self.copy.mkdir(parents=True)
        (self.copy / 'gramadrama-engine').write_text('the copy\'s engine')
        self.checkout = self.root / 'home' / 'gramadrama' / 'linux'
        self.checkout.mkdir(parents=True)
        (self.checkout / 'install_engine.py').write_text('')
        (self.copy / 'install.json').write_text(json.dumps({'version': '0.5.2', 'source': str(self.checkout)}, indent=2))
        with mock.patch.object(I, 'LAUNCHER', self.copy / 'gramadrama-engine'):
            self.source_install = I.component_xml()

    def preinst(self, argument='install'):
        script = B.PREINST.replace('/usr/share/ibus/component/gramadrama.xml', str(self.component))
        return subprocess.run(['sh', '-c', script, 'preinst', argument], capture_output=True, text=True)

    def test_a_copy_from_the_installer_stops_the_install(self):
        self.component.write_text(self.source_install)
        for argument in ('install', 'upgrade'):
            done = self.preinst(argument)
            self.assertEqual(done.returncode, 1)
            self.assertIn(str(self.copy), done.stderr)
            self.assertIn('/usr/bin/python3 %s/install_engine.py uninstall' % self.checkout, done.stderr)
            self.assertIn('Uninstall that copy first', done.stderr)

    def test_without_the_checkout_the_command_is_the_generic_one(self):
        self.component.write_text(self.source_install)
        (self.copy / 'install.json').unlink()
        done = self.preinst()
        self.assertEqual(done.returncode, 1)
        self.assertIn('/usr/bin/python3 linux/install_engine.py uninstall', done.stderr)

    def test_what_is_no_running_copy_lets_the_package_in(self):
        self.assertEqual(self.preinst().returncode, 0)  # no description at all
        self.component.write_text(I.package_component_xml())  # the package's own (an upgrade)
        self.assertEqual(self.preinst('upgrade').returncode, 0)
        self.component.write_text(self.source_install)
        shutil.rmtree(self.copy)  # uninstalled, the description left behind: the package replaces it
        done = self.preinst()
        self.assertEqual((done.returncode, done.stderr), (0, ''))


class RemovalTests(unittest.TestCase):
    def spin(self, seconds, until=lambda: False):
        from gi.repository import GLib
        context, deadline = GLib.MainContext.default(), time.monotonic() + seconds
        while time.monotonic() < deadline and not until():
            context.iteration(False)
            time.sleep(0.01)

    def test_the_app_closes_when_apt_removes_it(self):
        from gramadrama.app import watch_removal
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp, 'gramadrama-app')
            launcher.write_text('#! old')
            removed = []
            monitor = watch_removal(launcher, lambda: removed.append(True), delay_ms=50)
            self.addCleanup(monitor.cancel)
            new = Path(tmp, 'gramadrama-app.dpkg-new')  # an upgrade: dpkg renames the new file over the old one
            new.write_text('#! new')
            os.rename(new, launcher)
            self.spin(0.5)
            self.assertEqual(removed, [])
            launcher.unlink()  # apt remove
            self.spin(3, lambda: removed)
            self.assertEqual(removed, [True])


class VersionTests(unittest.TestCase):
    def version_with(self, answers):
        def git(*args):
            return answers.get(args[0], '')
        with mock.patch.object(B, 'git', git):
            return B.version()

    def test_version_carries_the_commit_count_and_the_commit(self):
        base = (B.ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        clean = self.version_with({'rev-parse': '3cb73dc', 'rev-list': '38'})
        self.assertEqual(clean, (base + '+git38.3cb73dc', base, '3cb73dc', False))
        dirty = self.version_with({'rev-parse': '3cb73dc', 'rev-list': '38', 'status': ' M linux/x.py'})
        self.assertEqual(dirty[0], base + '+git38.3cb73dc+dirty')
        self.assertEqual(self.version_with({})[0], base)

    def test_a_release_is_its_tagged_version_alone(self):
        base = (B.ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        tagged = {'rev-parse': '3cb73dc', 'rev-list': '38', 'tag': 'latest v%s' % base}
        with mock.patch.object(B, 'git', lambda *args: tagged.get(args[0], '')):
            self.assertEqual(B.version(release=True), (base, base, '3cb73dc', False))
        for answers, why in (({'rev-parse': '3cb73dc', 'tag': 'v0.0.1'}, 'tagu v%s' % base),
                             (dict(tagged, status=' M linux/x.py'), 'niezapisane zmiany')):
            with mock.patch.object(B, 'git', lambda *args, answers=answers: answers.get(args[0], '')):
                with self.assertRaises(SystemExit) as stopped:
                    B.version(release=True)
            self.assertIn(why, str(stopped.exception))

    @unittest.skipUnless(shutil.which('dpkg'), 'needs dpkg')
    def test_a_release_sorts_above_the_builds_before_it_and_below_the_builds_after_it(self):
        def newer(a, b):
            return subprocess.run(['dpkg', '--compare-versions', a, 'gt', b]).returncode == 0
        self.assertTrue(newer('0.6.0', '0.5.2+git41.1197f2c'))
        self.assertTrue(newer('0.6.0+git45.0aaaaaa', '0.6.0'))

    @unittest.skipUnless(shutil.which('dpkg'), 'needs dpkg')
    def test_a_later_commit_sorts_higher_whatever_its_hash(self):
        later = subprocess.run(['dpkg', '--compare-versions', '0.5.2+git39.0000000', 'gt', '0.5.2+git38.fffffff'])
        self.assertEqual(later.returncode, 0)

    def test_without_git_every_file_but_caches_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'src')
            (source / '__pycache__').mkdir(parents=True)
            (source / 'a.py').write_text('a')
            (source / '__pycache__' / 'a.cpython-314.pyc').write_bytes(b'')
            with mock.patch.object(B, 'git', lambda *args: ''):
                self.assertEqual(B.tracked(source), [source / 'a.py'])


if __name__ == '__main__':
    unittest.main()
