import json
import shutil
import subprocess
import sys
import tempfile
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
        for script in ('postinst', 'prerm'):
            self.assertTrue((control / script).stat().st_mode & 0o111, script)


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
