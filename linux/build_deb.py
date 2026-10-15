#!/usr/bin/python3
"""Build the Debian package of GramaDrama for Linux from this repository (Architecture: all, nothing to compile).

    /usr/bin/python3 linux/build_deb.py [--out DIR]     → DIR/gramadrama_<version>_all.deb (default: build/)

The package installs what install_engine.py copies into a user's data directory into the system instead:
/usr/lib/gramadrama (the engine, the app, the browser host and install_engine.py itself, for an account's setup and
the speech tools), the IBus description with one engine per keyboard layout (install_engine.package_component_xml),
the panel among the system's GNOME Shell extensions, the desktop entry, the icon and /usr/bin/gramadrama. apt cannot
reach the accounts: each one switches GramaDrama on from the app (install_engine.py setup) and logs in again once.
The version is VERSION with the commit, e.g. 0.5.2+git38.3cb73dc (see version). Needs git and dpkg-deb.
"""
import argparse
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

LINUX = Path(__file__).resolve().parent
ROOT = LINUX.parent
sys.path.insert(0, str(LINUX))
import install_engine as I  # noqa: E402

PACKAGE = 'gramadrama'
LIB = Path('usr/lib/gramadrama')
MAINTAINER = 'GramaDrama <info@gramadrama.app>'
DEPENDS = ('python3 (>= 3.12), python3-gi, gir1.2-glib-2.0, gir1.2-ibus-1.0, gir1.2-gtk-4.0, gir1.2-adw-1, '
           'gir1.2-atspi-2.0, ibus, gnome-shell (>= 50), libenchant-2-2, libglib2.0-bin, procps')
RECOMMENDS = 'hunspell-pl, hunspell-en-us, pipewire-bin'
SUGGESTS = 'hunspell-de-de'
DESCRIPTION = '''text correction, translation and dictation in GNOME applications
 GramaDrama corrects and translates text in the text fields of GNOME applications
 through an IBus input method, shows the proposal in a panel at the cursor, checks
 spelling while you type and lets you dictate. The language model runs at an
 address you choose: Ollama on this computer, or a server with the Ollama or the
 OpenAI-compatible protocol.
 .
 After installing, start GramaDrama and switch it on for your account, then log
 out and in once so that GNOME loads the panel.'''
# The maintainer scripts. preinst: a copy from the installer (install_engine.py install, from the repository) wrote the
# IBus description by hand; the package would replace it, and that account's own panel, which GNOME prefers to the
# package's, would refuse the package's engine. The package waits until the copy is uninstalled. A description whose
# engine is gone (a copy uninstalled before) is replaced. postinst and prerm: __pycache__ is made at configure time and
# removed before dpkg removes the files, so no directory of the package is left behind; IBus reads the new description
# at the next login (or the account's setup).
PREINST = r'''#!/bin/sh
set -e
component=/usr/share/ibus/component/gramadrama.xml
if [ "$1" = install ] || [ "$1" = upgrade ]; then
    if [ -f "$component" ] && ! grep -q '/usr/lib/gramadrama/gramadrama-engine' "$component"; then
        launcher=$(sed -n 's:.*<exec>/usr/bin/python3 \(.*\) --ibus</exec>.*:\1:p' "$component" | tr -d "'")
        if [ -n "$launcher" ] && [ -e "$launcher" ]; then
            copy=$(dirname "$launcher")
            source=$(sed -n 's/.*"source": *"\([^"]*\)".*/\1/p' "$copy/install.json" 2>/dev/null || true)
            if [ -n "$source" ] && [ -e "$source/install_engine.py" ]; then
                command="/usr/bin/python3 $source/install_engine.py uninstall"
            else
                command="/usr/bin/python3 linux/install_engine.py uninstall"
            fi
            cat >&2 <<EOF

GramaDrama jest już zainstalowana z repozytorium (instalatorem): $copy
Najpierw odinstaluj tę kopię w terminalu sesji GNOME jej konta, potem zainstaluj paczkę ponownie:

  $command

GramaDrama is already installed from the repository (by its installer): $copy
Uninstall that copy first, in a terminal of that account's GNOME session (the command above),
then install the package again.

EOF
            exit 1
        fi
    fi
fi
exit 0
'''
POSTINST = '''#!/bin/sh
set -e
if [ "$1" = configure ]; then
    /usr/bin/python3 -m compileall -q /usr/lib/gramadrama/gramadrama >/dev/null 2>&1 || true
fi
exit 0
'''
PRERM = '''#!/bin/sh
set -e
find /usr/lib/gramadrama -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
'''
COPYRIGHT = '''Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: GramaDrama
Upstream-Contact: info@gramadrama.app
Source: https://github.com/gramadrama/gramadrama

Files: *
Copyright: the GramaDrama authors
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 On Debian systems, the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.

Files: usr/lib/gramadrama/gramadrama/emoji.py
Copyright: Unicode, Inc.
License: Unicode-3.0
 See /usr/share/doc/gramadrama/THIRD_PARTY.md, section "Unicode licence notice".
'''


def git(*args):
    out = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ''


def version():
    """(package version, VERSION, commit, dirty). The package version is VERSION with the number of commits and the commit,
    so a later build of the same VERSION sorts higher for apt: 0.5.2+git38.3cb73dc; +dirty while linux/ or browser/ have
    uncommitted changes."""
    base = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    commit, count = git('rev-parse', '--short', 'HEAD'), git('rev-list', '--count', 'HEAD')
    dirty = bool(git('status', '--porcelain', '--untracked-files=no', '--', 'linux', 'browser', 'VERSION'))
    full = base + ('+git%s.%s' % (count, commit) if commit else '') + ('+dirty' if dirty else '')
    return full, base, commit, dirty


def tracked(source):
    """The files of a directory the repository tracks (a build ships no caches or files left lying around); every file
    but caches outside git (a copy of the tree without .git)."""
    listed = git('ls-files', '-z', '--', str(source))  # nothing outside the repository
    if listed:
        return [ROOT / name for name in listed.split('\0') if name]
    return [p for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']


def copy_tree(source, target):
    for path in tracked(source):
        if path.exists():  # a file deleted but not yet committed stays out
            (target / path.relative_to(source)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.relative_to(source))


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(mode)


def stage(root, full_version, base, commit, dirty):
    """The package's files under root (the future /), with modes; returns the files (relative paths)."""
    lib = root / LIB
    copy_tree(LINUX / 'gramadrama', lib / 'gramadrama')
    for name in ('gramadrama-engine', 'gramadrama-app', 'gramadrama-browser-host', 'install_engine.py'):
        shutil.copy2(LINUX / name, lib / name)
        (lib / name).chmod(0o755)
    copy_tree(LINUX / 'data', lib / 'data')
    shutil.copy2(LINUX / 'icon.svg', lib / 'icon.svg')
    shutil.copy2(ROOT / 'VERSION', lib / 'VERSION')
    if (ROOT / 'browser' / 'extension').is_dir():
        copy_tree(ROOT / 'browser' / 'extension', lib / 'browser-extension')
    component = I.package_component_xml()
    write(root / 'usr/share/ibus/component/gramadrama.xml', component)
    write(lib / 'gramadrama.xml', component)  # the app compares the two (checks.component_state)
    write(lib / 'install.json', json.dumps({'version': base, 'commit': commit or None, 'dirty': dirty,
                                            'package': full_version}, indent=2) + '\n')  # read by the app (checks)
    copy_tree(LINUX / 'gnome-extension', root / 'usr/share/gnome-shell/extensions' / I.EXTENSION_ID)
    write(root / 'usr/share/applications' / (I.APP_ID + '.desktop'), I.desktop_entry(Path('/') / LIB / 'gramadrama-app'))
    icon = root / 'usr/share/icons/hicolor/scalable/apps' / (I.APP_ID + '.svg')
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LINUX / 'icon.svg', icon)
    write(root / 'usr/bin/gramadrama', I.bin_script(Path('/') / LIB / 'gramadrama-app'), 0o755)
    doc = root / 'usr/share/doc' / PACKAGE
    write(doc / 'copyright', COPYRIGHT)
    shutil.copy2(ROOT / 'THIRD_PARTY.md', doc / 'THIRD_PARTY.md')
    date = datetime.datetime.now(datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    changelog = '%s (%s) unstable; urgency=medium\n\n  * Built from %s.\n\n -- %s  %s\n' % (
        PACKAGE, full_version, 'commit %s' % commit if commit else 'a tree without git', MAINTAINER, date)
    doc.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(doc / 'changelog.Debian.gz', 'wb', mtime=0) as f:
        f.write(changelog.encode('utf-8'))
    for path in [root, *root.rglob('*')]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file() and not os.access(path, os.X_OK):
            path.chmod(0o644)
    return sorted(p.relative_to(root) for p in root.rglob('*') if p.is_file())


def control(root, full_version):
    size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) // 1024 + 1
    return ('Package: %s\nVersion: %s\nArchitecture: all\nMaintainer: %s\nInstalled-Size: %d\nDepends: %s\n'
            'Recommends: %s\nSuggests: %s\nSection: utils\nPriority: optional\nHomepage: https://gramadrama.app\n'
            'Description: %s\n') % (PACKAGE, full_version, MAINTAINER, size, DEPENDS, RECOMMENDS, SUGGESTS, DESCRIPTION)


def build(out):
    full_version, base, commit, dirty = version()
    work = out / 'deb'
    if work.exists():
        shutil.rmtree(work)
    root = work / 'root'
    files = stage(root, full_version, base, commit, dirty)
    debian = root / 'DEBIAN'
    write(debian / 'control', control(root, full_version))
    write(debian / 'preinst', PREINST, 0o755)
    write(debian / 'postinst', POSTINST, 0o755)
    write(debian / 'prerm', PRERM, 0o755)
    sums = ''.join('%s  %s\n' % (hashlib.md5((root / f).read_bytes()).hexdigest(), f) for f in files)
    write(debian / 'md5sums', sums)
    package = out / ('%s_%s_all.deb' % (PACKAGE, full_version))
    subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(root), str(package)], check=True,
                   stdout=subprocess.DEVNULL)
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=ROOT / 'build', help='where the package goes (default: build/)')
    args = parser.parse_args()
    if not shutil.which('dpkg-deb'):
        sys.exit('Brak dpkg-deb (pakiet dpkg).')
    args.out.mkdir(parents=True, exist_ok=True)
    print(build(args.out.resolve()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
