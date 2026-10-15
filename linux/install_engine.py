#!/usr/bin/python3
"""Install the GramaDrama IBus engine for the current user (GNOME).

    install_engine.py status      show what is installed and active (changes nothing)
    install_engine.py install     copy the engine, the app and the panel extension, register the
                                  engine, make it the first input source
    install_engine.py uninstall   restore the previous input sources, remove the engine, the app,
                                  the panel (only its entry in the enabled extensions) and the app's
                                  launch fixes
    install_engine.py setup       the .deb package (/usr/lib/gramadrama): GramaDrama on this account, everything
                                  `install` does besides copying files and the sudo step; the app runs it
    install_engine.py teardown    the package: GramaDrama off on this account (before `apt remove`)
    install_engine.py tools       download what dictation needs into
                                  ~/.local/share/gramadrama-tools (about 4.3 GB): a Python 3.13
                                  environment with faster-whisper and CUDA 12 libraries (uv) and the
                                  Whisper large-v3-turbo model; then a test on the GPU

The engine is copied to ~/.local/share/gramadrama-engine, so edits in the repository never
affect the running engine; run `install` again to update. One step needs root: ibus-daemon
reads engine descriptions only from /usr/share/ibus/component, so `install` prints a single
`sudo install ...` command for that file and asks you to run `install` again afterwards.
The previous input sources are saved and restored by `uninstall`, which takes only GramaDrama's own
entry out of the enabled GNOME extensions. The GramaDrama panel is a GNOME Shell extension: GNOME loads a newly installed or
updated extension only after logging out and in; until then the engine shows its results in the
plain IBus popup. GNOME applies the keyboard layout itself. Choose it with `install --layout pl|us|de`
(default: the previous installation's layout, initially pl). Text languages are configured separately
in the app. Other input sources remain available with Super+Space or the top bar.
The app ("GramaDrama" in the application grid: status and settings) runs from the same copy. It
replaces the GTK app of the first Linux client: `install` stops it if it still runs, and removes
its copy and its Ctrl+Alt+G entry among the GNOME custom shortcuts. The launch fixes the app
writes on request (docs/apps.md: wrappers in ~/.local/bin, .desktop overrides) are left alone by
`install` and removed by `uninstall`; the app list apps.json stays, like settings.json.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parent
HOME = Path.home()


def xdg(name, default):
    """An XDG base directory: only an absolute value counts (the spec ignores empty and relative ones)."""
    value = os.environ.get(name) or ''
    return Path(value) if os.path.isabs(value) else default


DATA = xdg('XDG_DATA_HOME', HOME / '.local/share')
CONFIG = xdg('XDG_CONFIG_HOME', HOME / '.config')
TARGET = DATA / 'gramadrama-engine'
LAUNCHER = TARGET / 'gramadrama-engine'
STAGED_XML = TARGET / 'gramadrama.xml'
COMPONENT = Path('/usr/share/ibus/component/gramadrama.xml')
SYSTEM_LAUNCHER = Path('/usr/lib/gramadrama/gramadrama-engine')  # the .deb package's engine (scripts/build-deb.sh)
SYSTEM_DIR = SYSTEM_LAUNCHER.parent
SYSTEM_DESKTOP = Path('/usr/share/applications') / 'pl.gramadrama.Linux.desktop'
SYSTEM_EXTENSION = Path('/usr/share/gnome-shell/extensions') / 'gramadrama@gramadrama.local'
BACKUP = CONFIG / 'gramadrama' / 'engine-install-backup.json'
IBUS_UNIT = 'org.freedesktop.IBus.session.GNOME.service'
PACKAGE_ENGINES = {'pl': 'gramadrama', 'us': 'gramadrama-us', 'de': 'gramadrama-de'}  # layout -> engine (package)
ENGINE_SOURCES = {('ibus', name) for name in PACKAGE_ENGINES.values()}  # every engine of ours, either install
KEYBOARD_LAYOUTS = {'pl': ('pl', 'Polski (GramaDrama)'),
                    'us': ('en', 'English (GramaDrama)'),
                    'de': ('de', 'Deutsch (GramaDrama)')}
SOURCES_SCHEMA = 'org.gnome.desktop.input-sources'
KEYBINDING_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
OLD_SHORTCUT = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/gramadrama/'
HOTKEYS = ('<control><alt>g', '<alt><control>g', '<primary><alt>g')
EXTENSION_ID = 'gramadrama@gramadrama.local'
EXTENSION_SOURCE = SOURCE / 'gnome-extension'
EXTENSION = DATA / 'gnome-shell' / 'extensions' / EXTENSION_ID
# The browser extension is the one of browser/extension, shared with the Mac (one code, one ID, one protocol): installing copies
# that directory to a stable path to load unpacked from, and registers its native messaging host in the browsers found.
BROWSER_EXTENSION_SOURCE = SOURCE.parent / 'browser' / 'extension'
BROWSER_EXTENSION_TARGET = TARGET / 'browser-extension'
HOST_LAUNCHER = TARGET / 'gramadrama-browser-host'
HOST_NAME = 'pl.gramadrama.browser'
INSTALL_INFO = TARGET / 'install.json'  # version and change times of the engine and the panel, read by the app
VERSION_FILE = SOURCE.parent / 'VERSION'  # the version shared with the Mac (scripts/build-app.sh)
APP_ID = 'pl.gramadrama.Linux'
APP_LAUNCHER = TARGET / 'gramadrama-app'
DESKTOP = DATA / 'applications' / (APP_ID + '.desktop')
ICON = DATA / 'icons' / 'hicolor' / 'scalable' / 'apps' / (APP_ID + '.svg')
BIN = HOME / '.local' / 'bin' / 'gramadrama'
LEGACY_APP = DATA / 'gramadrama'  # the first Linux client's GTK app (its install.py), replaced by the app
LOCAL_BIN = HOME / '.local' / 'bin'  # the app's launch fixes: wrappers here, overrides in APPLICATIONS
APPLICATIONS = DATA / 'applications'
SCHEME_TYPE = 'x-scheme-handler/gramadrama'  # the login's way back from the browser (gramadrama/oidc.py)
MIMEAPPS = CONFIG / 'mimeapps.list'
# Downloaded tools (gramadrama/paths.py, tools_dir): kept apart from TARGET, which `install` replaces,
# and left in place by `uninstall` (4 GB are not downloaded again on a whim).
TOOLS = DATA / 'gramadrama-tools'
SPEECH = TOOLS / 'speech'
SPEECH_PYTHON = SPEECH / 'bin' / 'python'
SPEECH_PACKAGES = ('faster-whisper==1.2.1', 'ctranslate2==4.8.2', 'nvidia-cublas-cu12==12.9.2.10',
                   'nvidia-cudnn-cu12==9.26.0.51')  # pinned together for the optional local CUDA speech runtime
WHISPER_REPO = 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
WHISPER = TOOLS / 'models' / 'faster-whisper-large-v3-turbo'


def with_engine(sources, layout='pl', engine='gramadrama'):
    """GramaDrama first; the other sources keep their order (the plain layout stays as a fallback)."""
    rest = [tuple(s) for s in sources if tuple(s) not in ENGINE_SOURCES]
    if not any(kind == 'xkb' for kind, _ in rest):
        rest.append(('xkb', layout))  # keep a plain layout available if the engine cannot start
    return [('ibus', engine)] + rest


def without_engine(sources, layout='pl'):
    return [tuple(s) for s in sources if tuple(s) not in ENGINE_SOURCES] or [('xkb', layout)]


def component_xml(layout='pl'):
    if layout not in KEYBOARD_LAYOUTS:
        raise ValueError('Unsupported keyboard layout')
    template = (SOURCE / 'data' / 'gramadrama.xml.in').read_text(encoding='utf-8')
    command = '/usr/bin/python3 %s --ibus' % shlex.quote(str(LAUNCHER))
    language, name = KEYBOARD_LAYOUTS[layout]
    return (template.replace('@EXEC@', command.replace('&', '&amp;').replace('<', '&lt;'))
            .replace('@LAYOUT@', layout).replace('@LANGUAGE@', language).replace('@LONGNAME@', name))


def package_component_xml():
    """The package's IBus description: one engine per layout of KEYBOARD_LAYOUTS (PACKAGE_ENGINES), all served by the
    package's launcher. One description serves every account, so each account picks its engine (setup)."""
    template = (SOURCE / 'data' / 'gramadrama.xml.in').read_text(encoding='utf-8')
    start, end = template.index('    <engine>'), template.index('</engine>') + len('</engine>\n')
    engines = ''
    for layout, engine in PACKAGE_ENGINES.items():
        language, name = KEYBOARD_LAYOUTS[layout]
        engines += (template[start:end].replace('<name>gramadrama</name>', '<name>%s</name>' % engine)
                    .replace('@LAYOUT@', layout).replace('@LANGUAGE@', language).replace('@LONGNAME@', name))
    command = '/usr/bin/python3 %s --ibus' % shlex.quote(str(SYSTEM_LAUNCHER))
    return template[:start].replace('@EXEC@', command) + engines + template[end:]


def account_engine(sources):
    """The package's engine for an account: the one with the layout the account types in (its first plain layout)."""
    layout = next((name.split('+')[0] for kind, name in sources if kind == 'xkb'), 'pl')
    return PACKAGE_ENGINES.get(layout, PACKAGE_ENGINES['us'])

# ---- the live system (gi is imported lazily so the pure helpers stay testable anywhere) ----
def settings(schema, path=None):
    from gi.repository import Gio
    return Gio.Settings.new_with_path(schema, path) if path else Gio.Settings.new(schema)


def get_sources():
    return [tuple(s) for s in settings(SOURCES_SCHEMA).get_value('sources').unpack()]


def set_sources(sources):
    from gi.repository import GLib, Gio
    s = settings(SOURCES_SCHEMA)
    s.set_value('sources', GLib.Variant('a(ss)', sources))
    Gio.Settings.sync()


def enabled_extensions():
    return list(settings('org.gnome.shell').get_strv('enabled-extensions'))


def set_enabled_extensions(uuids):
    from gi.repository import Gio
    settings('org.gnome.shell').set_strv('enabled-extensions', uuids)
    Gio.Settings.sync()


def extension_files(root):
    """{relative path: content} of an extension as it is installed (no caches, no build output)."""
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.relative_to(root).parts and not p.name.endswith('.compiled')}


def copy_extension():
    """Returns True when the installed extension changed (GNOME loads it at the next login). The
    same files are left alone: GNOME Shell re-reads them when it enables the panel again (after the
    lock screen, for one)."""
    if not (EXTENSION_SOURCE / 'metadata.json').exists():
        return False
    if EXTENSION.exists() and extension_files(EXTENSION) == extension_files(EXTENSION_SOURCE):
        return False
    if EXTENSION.exists():
        shutil.rmtree(EXTENSION)
    shutil.copytree(EXTENSION_SOURCE, EXTENSION, ignore=shutil.ignore_patterns('__pycache__', '*.compiled'))
    return True


def old_binding():
    from gi.repository import Gio
    parent = Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys')
    if OLD_SHORTCUT not in parent.get_strv('custom-keybindings'):
        return None
    return settings(KEYBINDING_SCHEMA, OLD_SHORTCUT).get_string('binding')


def set_old_binding(value):
    from gi.repository import Gio
    settings(KEYBINDING_SCHEMA, OLD_SHORTCUT).set_string('binding', value)
    Gio.Settings.sync()


# The ibus CLI runs in a fresh process each time: libibus keeps one bus object per process, and
# without a main loop it never reconnects to a restarted ibus-daemon.
def ibus_cli(*args):
    try:
        out = subprocess.run(['ibus', *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def registered(engine='gramadrama'):
    return engine in (ibus_cli('list-engine', '--name-only') or '').split()


def global_engine():
    return (ibus_cli('engine') or '').strip() or None


def engine_pids():
    out = subprocess.run(['pgrep', '-f', '^/usr/bin/python3 %s' % LAUNCHER], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def app_pids():
    """The app's windows started from the installed copy."""
    out = subprocess.run(['pgrep', '-f', '^/usr/bin/python3 %s' % APP_LAUNCHER], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def process_start(pid):
    """When a process started, in seconds since the epoch (/proc), or None."""
    try:
        stat = Path('/proc/%d/stat' % pid).read_text()
        ticks = int(stat[stat.rindex(')') + 2:].split()[19])  # field 22, starttime
        with open('/proc/stat') as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith('btime '))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / os.sysconf('SC_CLK_TCK')


def stale_engines(pids, changed_at):
    """Engine processes started before the installer last changed the engine's code: also when an
    earlier `install` copied the code and stopped before the restart (the sudo step, Ctrl+C)."""
    if not changed_at:
        return []
    return [pid for pid in pids if (process_start(pid) or 0) < changed_at]


# The engine's long-lived children (engine/speech.py): the speech and live-check services and what they
# import. They run as long as the engine does, so a change there needs its restart too.
SERVICE_MODULES = ('speech.py', 'dictation.py', 'dictionary.py', 'liveservice.py', 'model_live.py', 'live.py', 'spell.py',
                   'core.py', 'emoji.py', 'geometry.py', 'prompts.py', 'oidc.py', 'i18n.py')


def engine_digest(root):
    """The code the running engine process and its long-lived children hold: when it changes, the engine
    must restart. The backend children import core.py afresh for every correction, but the live service
    keeps it loaded, so it counts. The app alone never restarts the engine."""
    digest = hashlib.sha256()
    files = [root / 'gramadrama-engine', root / 'gramadrama' / '__init__.py', root / 'gramadrama' / 'paths.py']
    files += [root / 'gramadrama' / name for name in SERVICE_MODULES]
    files += sorted((root / 'gramadrama' / 'engine').glob('*.py'))
    files += sorted((root / 'gramadrama' / 'locales').glob('*/*.json'))
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes() if path.is_file() else b'-')
    return digest.hexdigest()


def copy_engine(layout=None):
    """Mirror the package (a module deleted in the repository goes here too) and the launchers.
    Returns True when the engine's own code changed."""
    layout = layout or read_install_info().get('keyboard_layout', 'pl')
    xml = component_xml(layout)  # validate before changing any installed files
    TARGET.mkdir(parents=True, exist_ok=True)
    before = engine_digest(TARGET)
    fresh, old = TARGET / 'gramadrama.new', TARGET / 'gramadrama.old'
    for stale in (fresh, old):
        if stale.exists():
            shutil.rmtree(stale)
    shutil.copytree(SOURCE / 'gramadrama', fresh, ignore=shutil.ignore_patterns('__pycache__'))
    if (TARGET / 'gramadrama').exists():
        os.rename(TARGET / 'gramadrama', old)
    os.rename(fresh, TARGET / 'gramadrama')  # the backend's next child imports the new code
    if old.exists():
        shutil.rmtree(old)
    for name, target in (('gramadrama-engine', LAUNCHER), ('gramadrama-app', APP_LAUNCHER),
                         ('gramadrama-browser-host', HOST_LAUNCHER)):
        shutil.copy2(SOURCE / name, target)
        target.chmod(0o755)
    copy_browser_extension()
    shutil.copy2(SOURCE / 'icon.svg', TARGET / 'icon.svg')
    STAGED_XML.write_text(xml, encoding='utf-8')
    changed = engine_digest(TARGET) != before
    version = source_version()
    write_install_info(installed_at=datetime.datetime.now().isoformat(timespec='seconds'), source=str(SOURCE), keyboard_layout=layout,
                       version=version.get('version'), commit=version.get('commit'), dirty=version.get('dirty'),
                       **({'engine_changed_at': time.time()} if changed else {}))
    return changed


def copy_browser_extension():
    """The extension's directory (browser/extension, the Mac's too) to BROWSER_EXTENSION_TARGET, the path to load unpacked from;
    it is mirrored, so a file deleted in the repository goes here too. Nothing when the repository has no browser/ (a copy of
    linux/ alone)."""
    if not BROWSER_EXTENSION_SOURCE.is_dir():
        return False
    fresh = BROWSER_EXTENSION_TARGET.with_name(BROWSER_EXTENSION_TARGET.name + '.new')
    if fresh.exists():
        shutil.rmtree(fresh)
    shutil.copytree(BROWSER_EXTENSION_SOURCE, fresh)
    if BROWSER_EXTENSION_TARGET.exists():
        shutil.rmtree(BROWSER_EXTENSION_TARGET)
    os.rename(fresh, BROWSER_EXTENSION_TARGET)
    return True


def browserhost_module():
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from gramadrama import browserhost
    return browserhost


def host_manifest(launcher=None):
    """The manifest of the native messaging host: only the extension of GramaDrama (its ID comes from the key in its manifest)
    may start it. launcher: the user's copy's host, or the package's."""
    browserhost = browserhost_module()
    return {'name': HOST_NAME, 'description': 'GramaDrama: the browser extension and the engine',
            'path': str(launcher or HOST_LAUNCHER), 'type': 'stdio', 'allowed_origins': [browserhost.EXTENSION_ORIGIN]}


def register_browser_hosts(launcher=None):
    """The host's manifest in every browser of browserhost.BROWSER_CONFIGS that has a configuration directory (one that was never run has
    none). Returns the names of those browsers."""
    registered = []
    manifest = json.dumps(host_manifest(launcher), indent=2) + '\n'
    for name, relative in browserhost_module().BROWSER_CONFIGS:
        directory = CONFIG / relative
        if not directory.is_dir():
            continue
        hosts = directory / 'NativeMessagingHosts'
        try:
            hosts.mkdir(exist_ok=True)
            (hosts / (HOST_NAME + '.json')).write_text(manifest, encoding='utf-8')
        except OSError:
            continue
        registered.append(name)
    return registered


def unregister_browser_hosts(root=None):
    """Take our manifests away (only ours, pointing into root: a file of that name that points elsewhere is somebody else's)."""
    root = root or TARGET
    removed = []
    for name, relative in browserhost_module().BROWSER_CONFIGS:
        path = CONFIG / relative / 'NativeMessagingHosts' / (HOST_NAME + '.json')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('name') == HOST_NAME and str(data.get('path', '')).startswith(str(root)):
            path.unlink(missing_ok=True)
            removed.append(name)
    return removed


def source_version():
    """The version in VERSION (shared with the Mac), the repository's commit and whether linux/ has uncommitted changes;
    each key only when known (no commit without git, no version in a copy of linux/ alone)."""
    def git(*args):
        try:
            out = subprocess.run(['git', '-C', str(SOURCE), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 else None
    info = {}
    try:
        info['version'] = VERSION_FILE.read_text(encoding='utf-8').strip() or None
    except OSError:
        pass
    commit = (git('rev-parse', '--short', 'HEAD') or '').strip()
    if commit:
        info.update(commit=commit, dirty=bool((git('status', '--porcelain', '--', '.') or '').strip()))
    return {k: v for k, v in info.items() if v is not None}


def read_install_info():
    try:
        info = json.loads(INSTALL_INFO.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def write_install_info(**changes):
    """Merge into install.json; a None value removes the key."""
    info = read_install_info()
    info.update(changes)
    info = {k: v for k, v in info.items() if v is not None}
    INSTALL_INFO.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')


# ---- the app in the application grid ----
def desktop_exec(*argv):
    """An Exec value (Desktop Entry spec): every argument in double quotes with ", `, $ and \\
    escaped, '%' doubled; then the value's own string escaping doubles every backslash."""
    def quote(arg):
        arg = arg.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        return '"%s"' % arg.replace('%', '%%')
    return ' '.join(quote(str(a)) for a in argv).replace('\\', '\\\\')


def desktop_entry(launcher=None):
    return ('''[Desktop Entry]
Type=Application
Name=GramaDrama
GenericName=Korekta tekstu
Comment=Korekta, tłumaczenie i dyktowanie: języki, skróty i serwery
Exec=%s
Icon=%s
Terminal=false
Categories=Utility;TextTools;
Keywords=korekta;gramatyka;pisownia;tłumaczenie;IBus;grammar;spelling;dictation;
StartupNotify=true
MimeType=%s;
''' % (desktop_exec('/usr/bin/python3', launcher or APP_LAUNCHER) + ' %u', APP_ID, SCHEME_TYPE))


def without_scheme(text):
    """mimeapps.list's text without our app as a handler of gramadrama:// (the user's other lines stay as they are)."""
    lines = []
    for line in text.splitlines():
        key, _, value = line.partition('=')
        if key == SCHEME_TYPE:
            others = [name for name in value.split(';') if name and name != DESKTOP.name]
            if not others:
                continue
            line = '%s=%s;' % (key, ';'.join(others))
        lines.append(line)
    return '\n'.join(lines) + ('\n' if lines else '')


def register_scheme(package=False):
    """gramadrama://auth, where the browser comes back from a login (gramadrama/oidc.py), goes to the app: the desktop
    entry says it handles the scheme and it becomes the default for it (a line in ~/.config/mimeapps.list). False
    when that did not work; the login still works, the user pastes the address the browser came back to. package:
    the package's desktop entry, which the package registered already."""
    if package:
        if not SYSTEM_DESKTOP.exists():
            return False
    elif not DESKTOP.exists() or not ours(DESKTOP, str(APP_LAUNCHER)):
        return False
    try:
        if not package:
            subprocess.run(['update-desktop-database', str(APPLICATIONS)], capture_output=True, timeout=30)
        return subprocess.run(['gio', 'mime', SCHEME_TYPE, DESKTOP.name], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def unregister_scheme():
    try:
        text = MIMEAPPS.read_text(encoding='utf-8')
    except OSError:
        return
    if SCHEME_TYPE in text:
        MIMEAPPS.write_text(without_scheme(text), encoding='utf-8')


def bin_script(launcher=None):
    return '#!/bin/sh\nexec /usr/bin/python3 %s "$@"\n' % shlex.quote(str(launcher or APP_LAUNCHER))


def ours(path, *markers):
    """True when the file does not exist or mentions one of the markers (never someone else's file)."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return any(marker in text for marker in markers)


def app_markers():
    """Text only our launchers contain: the app's launcher, or what the old GTK app's install.py
    wrote (~/.local/bin/gramadrama ran `cd <its copy>`, and its desktop entry ran that script)."""
    return str(APP_LAUNCHER), 'cd %s\n' % shlex.quote(str(LEGACY_APP)), 'Exec="%s"' % BIN


def install_app():
    """The launcher in the application grid (same file name as the old GTK app's, so a pinned
    GramaDrama in the dash keeps working), its icon and `gramadrama` in ~/.local/bin."""
    if ours(DESKTOP, *app_markers()):
        DESKTOP.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP.write_text(desktop_entry(), encoding='utf-8')
    ICON.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE / 'icon.svg', ICON)
    os.utime(ICON.parents[2])  # the icon theme rescans a directory whose time changed
    if ours(BIN, *app_markers()):
        BIN.parent.mkdir(parents=True, exist_ok=True)
        BIN.write_text(bin_script(), encoding='utf-8')
        BIN.chmod(0o755)


def legacy_process(pid):
    """The old GTK app: `python3 -m gramadrama`, which its launcher starts inside its copy."""
    try:
        cwd = os.readlink('/proc/%d/cwd' % pid)
        argv = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
    except OSError:
        return False
    copies = {str(LEGACY_APP), os.path.realpath(LEGACY_APP)}  # the kernel shows the resolved path
    return cwd.removesuffix(' (deleted)') in copies and b'-m' in argv and b'gramadrama' in argv


def stop_legacy_app():
    """The old GTK app keeps running after its window is closed (it holds the application) and owns
    the app's D-Bus name, so the new app would only bring the old window back until the next login.
    Ask it to quit (its app.quit action), and stop it if it does not. Returns None when it was not
    running, True when it stopped, False when it is still running."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        pid = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                            'GetConnectionUnixProcessID', GLib.Variant('(s)', (APP_ID,)), GLib.VariantType('(u)'),
                            Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
    except GLib.Error:
        return None  # nobody owns the name: not running
    if not legacy_process(pid):
        return None  # the new app, or something else: never touched
    gone = lambda: not Path('/proc/%d' % pid).exists() or not legacy_process(pid)  # noqa: E731
    try:
        bus.call_sync(APP_ID, '/' + APP_ID.replace('.', '/'), 'org.gtk.Actions', 'Activate',
                      GLib.Variant('(sava{sv})', ('quit', [], {})), None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        pass
    if not wait(gone, 5):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass  # it has just quit
    return wait(gone, 5)


def remove_legacy_copy():
    """The first Linux client's GTK app (its install.py): its copy under ~/.local/share/gramadrama.
    Its launcher, icon and ~/.local/bin/gramadrama now belong to the app. True when removed."""
    if LEGACY_APP.is_symlink():
        return False  # not what install.py made
    try:
        legacy = (LEGACY_APP / 'gramadrama' / 'app.py').exists() and set(os.listdir(LEGACY_APP)) <= {'gramadrama'}
    except OSError:
        return False
    if legacy:
        shutil.rmtree(LEGACY_APP)
    return legacy


def remove_legacy_shortcut():
    """Its (disabled) Ctrl+Alt+G entry among the GNOME custom shortcuts, which ran the old app with
    --selection. True when removed; `uninstall` then has no old binding to restore."""
    from gi.repository import Gio
    parent = Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys')
    paths = list(parent.get_strv('custom-keybindings'))
    if OLD_SHORTCUT not in paths:
        return False
    entry = settings(KEYBINDING_SCHEMA, OLD_SHORTCUT)
    command = entry.get_string('command')
    if 'gramadrama' not in command or '--selection' not in command:
        return False  # not the old app's entry: leave it alone
    parent.set_strv('custom-keybindings', [p for p in paths if p != OLD_SHORTCUT])
    for key in ('name', 'command', 'binding'):
        entry.reset(key)
    Gio.Settings.sync()
    return True


def uninstall_app():
    """Only the app's own launchers; an old GTK app that was never replaced keeps its launcher and icon."""
    for path in (DESKTOP, BIN):
        if path.exists() and ours(path, str(APP_LAUNCHER)):
            path.unlink()
    if not DESKTOP.exists():
        ICON.unlink(missing_ok=True)


def remove_launch_fixes():
    """The launch fixes the app wrote for its Aplikacje list (docs/apps.md, section 6): every file in
    ~/.local/bin and every .desktop file in ~/.local/share/applications that starts with the fix's
    marker line (gramadrama/apps.py). Returns the removed files."""
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from gramadrama import apps
    return apps.remove_all_fixes(LOCAL_BIN, APPLICATIONS)


def wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def status():
    installed = COMPONENT.exists() and COMPONENT.read_text(encoding='utf-8') == (
        STAGED_XML.read_text(encoding='utf-8') if STAGED_XML.exists() else component_xml())
    print('Kopia silnika:        %s' % (TARGET if LAUNCHER.exists() else 'brak'))
    print('Opis dla IBus:        %s' % ('aktualny' if installed else 'obecny, ale inny' if COMPONENT.exists() else 'brak'))
    print('Zarejestrowany w IBus: %s' % ('tak' if registered() else 'nie'))
    print('Źródła wprowadzania:  %s' % get_sources())
    print('Aktywny silnik IBus:  %s' % global_engine())
    print('Proces silnika:       %s' % (engine_pids() or 'nie działa'))
    print('Stary skrót Ctrl+Alt+G: %r' % old_binding())
    print('Dodatek panelu:       %s' % ('zainstalowany, %s' % ('włączony' if EXTENSION_ID in enabled_extensions() else 'wyłączony')
                                        if EXTENSION.exists() else 'brak'))
    print('Aplikacja:            %s' % (DESKTOP if DESKTOP.exists() and ours(DESKTOP, str(APP_LAUNCHER)) else
                                        'stara aplikacja GTK' if LEGACY_APP.exists() else 'brak'))
    info = read_install_info()
    commit = '%s%s' % (info['commit'], '+zmiany' if info.get('dirty') else '') if info.get('commit') else ''
    label = ' '.join(filter(None, (info.get('version'), '(%s)' % commit if commit else '')))
    print('Wersja:               %s' % ('%s, %s' % (label, info.get('installed_at', '?')) if label else 'nieznana'))
    print('Kopia zapasowa:       %s' % (BACKUP if BACKUP.exists() else 'brak'))
    print('Log (bez tekstu):     %s' % (xdg('XDG_CACHE_HOME', HOME / '.cache') / 'gramadrama' / 'engine.log'))


def install(layout=None):
    if SYSTEM_LAUNCHER.exists():  # one IBus description, one engine: the package's file would be overwritten
        sys.exit('GramaDrama jest zainstalowana z paczki (%s). Instalacja ze źródeł by się z nią gryzła: '
                 'zostań przy paczce albo najpierw ją usuń (sudo apt remove gramadrama).' % SYSTEM_LAUNCHER.parent)
    if 'GNOME' not in os.environ.get('XDG_CURRENT_DESKTOP', ''):
        sys.exit('Potrzebna jest sesja GNOME.')
    stopped = stop_legacy_app()
    if stopped:
        print('Zamknięto starą aplikację GramaDrama, która działała w tle.')
    elif stopped is False:
        print('Uwaga: stara aplikacja GramaDrama nadal działa w tle; nowa uruchomi się po ponownym zalogowaniu.')
    copy_engine(layout)
    layout = read_install_info().get('keyboard_layout', 'pl')
    install_app()  # the app guides through the rest, the sudo step included
    browsers = register_browser_hosts()
    if browsers and BROWSER_EXTENSION_TARGET.is_dir():
        print('Wtyczka do przeglądarki (%s): jeden raz wgraj ją ręcznie: chrome://extensions → Tryb dewelopera → '
              '„Załaduj rozpakowane” → %s' % (', '.join(browsers), BROWSER_EXTENSION_TARGET))
    if not register_scheme():
        print('Uwaga: nie udało się zarejestrować adresu gramadrama:// (powrót z logowania do serwera); '
              'przy logowaniu wklej adres, na który wróciła przeglądarka.')
    component = STAGED_XML.read_text(encoding='utf-8')
    component_digest = hashlib.sha256(component.encode('utf-8')).hexdigest()
    if not COMPONENT.exists() or COMPONENT.read_text(encoding='utf-8') != component:
        # The daemon may still know the engine name while caching an obsolete launcher.
        # Keep the reload pending across the sudo step, even if this XML was loaded before.
        write_install_info(registered_component_digest=None)
        print('Jeden krok wymaga uprawnień administratora (opis silnika dla IBus). Uruchom:\n')
        print('  sudo install -m 644 %s %s\n' % (shlex.quote(str(STAGED_XML)), COMPONENT))
        print('a potem ponownie: /usr/bin/python3 %s install' % shlex.quote(str(Path(__file__).resolve())))
        return 2
    if (not registered() or read_install_info().get('registered_layout', 'pl') != layout
            or read_install_info().get('registered_component_digest') != component_digest):
        print('Uruchamiam ponownie IBus, żeby wczytał GramaDramę (pisanie zatrzyma się na ~1 s)…')
        subprocess.run(['systemctl', '--user', 'restart', IBUS_UNIT], check=True)
        if not wait(registered, 15):
            sys.exit('IBus nie widzi silnika GramaDrama. Niczego więcej nie zmieniono.')
    write_install_info(registered_layout=layout, registered_component_digest=component_digest)
    panel_changed = enable_on_account(layout)
    print('Gotowe. Model i inne usługi ustawisz w aplikacji GramaDrama → Usługi, skróty w Start, języki tekstu w Pisanie.'
          ' Powrót: /usr/bin/python3 %s uninstall' % shlex.quote(str(Path(__file__).resolve())))
    if panel_changed:
        print('Panel GramaDramy (dodatek GNOME) pojawi się po wylogowaniu i ponownym zalogowaniu.'
              ' Do tego czasu wyniki są w zwykłym okienku IBus.')
    return 0


def enable_on_account(layout, package=False, engine='gramadrama'):
    """What install and setup share: the previous sources and shortcut saved (once), the old Ctrl+Alt+G shortcut out of
    the way, GramaDrama the first input source and active, the panel enabled (the user's copy refreshed first; the
    package's is the system's), the first client's leftovers removed. True when the panel needs the next login."""
    backup = json.loads(BACKUP.read_text()) if BACKUP.exists() else {'sources': get_sources(), 'binding': old_binding()}
    if 'extensions' not in backup:
        backup['extensions'] = enabled_extensions()
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(backup, indent=2))
    binding = old_binding()
    if binding is not None and binding.lower() in HOTKEYS:
        set_old_binding('')  # GNOME would take Ctrl+Alt+G before the input method sees it
    if not package:
        stale = stale_engines(engine_pids(), read_install_info().get('engine_changed_at'))
        if stale:  # an older engine than the copy: the next activation starts the new code
            for pid in stale:
                os.kill(pid, 15)
            wait(lambda: not set(stale) & set(engine_pids()), 5)
    set_sources(with_engine(get_sources(), layout, engine))
    if not wait(lambda: global_engine() == engine, 5):
        ibus_cli('engine', engine)  # GNOME normally activates the first source itself
    if not wait(lambda: global_engine() == engine, 10):
        print('Uwaga: GramaDrama nie jest jeszcze aktywna. Wybierz „%s” na górnym pasku.'
              % KEYBOARD_LAYOUTS.get(layout, (None, 'GramaDrama'))[1])
    if package:
        panel_changed, present = False, SYSTEM_EXTENSION.exists() or EXTENSION.exists()
    else:
        panel_changed = copy_extension()
        if panel_changed:  # the app tells "log in again" apart from "already running" by this time
            write_install_info(panel_changed_at=time.time())
        present = EXTENSION.exists()
    if present and EXTENSION_ID not in enabled_extensions():
        set_enabled_extensions(enabled_extensions() + [EXTENSION_ID])
        panel_changed = True
    if remove_legacy_copy():
        print('Usunięto starą aplikację GTK: %s' % LEGACY_APP)
    if remove_legacy_shortcut():
        print('Usunięto stary skrót GNOME „GramaDrama — aktywne pole tekstowe”.')
    return panel_changed


def package_engine_pids():
    """This user's engines started from the package (other users' run under their own accounts)."""
    out = subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', '^/usr/bin/python3 %s' % SYSTEM_LAUNCHER],
                         capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def remove_user_copy():
    """The package's setup on an account installed from the repository before (install): that copy's engine, panel and
    launchers go, so that GNOME loads the package's panel (it prefers the user's, which may refuse the package's engine)
    and the application grid opens the package's app. The backup of the sources, the settings and the downloaded tools
    stay. True when there was a copy."""
    if not (TARGET.exists() or EXTENSION.exists()):
        return False
    caller = os.getppid()  # the window that runs this setup stays open, whichever copy it came from
    for pid in engine_pids() + [pid for pid in app_pids() if pid != caller]:
        os.kill(pid, 15)
    if EXTENSION.exists():
        shutil.rmtree(EXTENSION)
    unregister_browser_hosts()  # the copy's hosts; setup registers the package's after this
    uninstall_app()
    if TARGET.exists():
        shutil.rmtree(TARGET)
    return True


def setup():
    """The package (.deb): GramaDrama on this account. The package installed the files and the IBus description; this does
    the rest, which only the user can (their GSettings, their browsers' hosts, their default for gramadrama://). The app's
    first step runs it; no root needed."""
    if not SYSTEM_LAUNCHER.exists():
        sys.exit('Paczka GramaDramy nie jest zainstalowana (brak %s).' % SYSTEM_LAUNCHER)
    if 'GNOME' not in os.environ.get('XDG_CURRENT_DESKTOP', ''):
        sys.exit('Potrzebna jest sesja GNOME.')
    engine = account_engine(get_sources())
    layout = next(layout for layout, name in PACKAGE_ENGINES.items() if name == engine)
    replaced = remove_user_copy()
    if replaced:
        print('Usunięto z tego konta kopię GramaDramy z instalatora (z repozytorium); ustawienia zostają.')
    register_browser_hosts(SYSTEM_DIR / 'gramadrama-browser-host')
    if not register_scheme(package=True):
        print('Uwaga: nie udało się zarejestrować adresu gramadrama:// (powrót z logowania do serwera); '
              'przy logowaniu wklej adres, na który wróciła przeglądarka.')
    # ibus-daemon read its descriptions before the package was installed: it does not know the engine, or it starts
    # the removed copy's
    if replaced or not registered(engine):
        print('Uruchamiam ponownie IBus, żeby wczytał GramaDramę (pisanie zatrzyma się na ~1 s)…')
        subprocess.run(['systemctl', '--user', 'restart', IBUS_UNIT], check=True)
        if not wait(lambda: registered(engine), 15):
            sys.exit('IBus nie widzi silnika GramaDrama. Niczego więcej nie zmieniono.')
    panel_changed = enable_on_account(layout, package=True, engine=engine) or replaced
    print('Gotowe: GramaDrama jest włączona na tym koncie.')
    if panel_changed:
        print('Panel GramaDramy pojawi się po wylogowaniu i ponownym zalogowaniu. Do tego czasu wyniki są w zwykłym okienku IBus.')
    return 0


def teardown():
    """The package: GramaDrama off on this account, before `sudo apt remove gramadrama` (apt cannot reach the accounts):
    the input sources and the old shortcut as before, the panel's entry out of the enabled extensions, this user's
    engine stopped, the browser hosts, the gramadrama:// default and the launch fixes removed. The package's files,
    the settings and apps.json stay."""
    backup = json.loads(BACKUP.read_text()) if BACKUP.exists() else {}
    sources = [tuple(s) for s in backup.get('sources') or []]
    set_sources(without_engine(sources or get_sources()))
    current = enabled_extensions()
    if EXTENSION_ID in current:
        set_enabled_extensions([uuid for uuid in current if uuid != EXTENSION_ID])
    if backup.get('binding') is not None and old_binding() == '':
        set_old_binding(backup['binding'])
    for pid in package_engine_pids():
        os.kill(pid, 15)
    unregister_scheme()
    unregister_browser_hosts(SYSTEM_DIR)
    for path in remove_launch_fixes():
        print('Usunięto poprawkę uruchamiania: %s' % path)
    BACKUP.unlink(missing_ok=True)
    print('Wyłączono GramaDramę na tym koncie. Paczkę usuniesz poleceniem: sudo apt remove gramadrama')
    return 0

def uninstall():
    backup = json.loads(BACKUP.read_text()) if BACKUP.exists() else {}
    sources = [tuple(s) for s in backup.get('sources') or []]
    set_sources(without_engine(sources or get_sources(), read_install_info().get('keyboard_layout', 'pl')))
    current = enabled_extensions()
    if EXTENSION_ID in current:  # only our entry goes; the user's other extensions stay as they are
        set_enabled_extensions([uuid for uuid in current if uuid != EXTENSION_ID])
    if EXTENSION.exists():
        shutil.rmtree(EXTENSION)
    if backup.get('binding') is not None and old_binding() == '':
        set_old_binding(backup['binding'])
    for pid in engine_pids():
        os.kill(pid, 15)
    windows = app_pids()
    for pid in windows:  # an open window would go on running from the copy removed below
        os.kill(pid, 15)
    if windows:
        print('Zamknięto okno GramaDramy.')
    unregister_scheme()
    unregister_browser_hosts()
    uninstall_app()
    for path in remove_launch_fixes():  # apps.json stays, like settings.json
        print('Usunięto poprawkę uruchamiania: %s' % path)
    if TARGET.exists():
        shutil.rmtree(TARGET)
    BACKUP.unlink(missing_ok=True)
    print('Przywrócono źródła wprowadzania: %s' % get_sources())
    if COMPONENT.exists() and str(LAUNCHER) in COMPONENT.read_text(encoding='utf-8', errors='replace'):
        # only this copy's description: one that starts the package's engine (or another account's) is not ours to remove
        print('Aby usunąć też opis silnika z IBus, uruchom:\n\n  sudo rm %s\n' % COMPONENT)
        print('i zrestartuj IBus: systemctl --user restart %s' % IBUS_UNIT)
    return 0


def uv_binary():
    found = shutil.which('uv') or HOME / '.local' / 'bin' / 'uv'
    return Path(found) if Path(found).exists() else None


def install_tools():
    """Idempotent: each part is fetched only when missing or of another version."""
    uv = uv_binary()
    if uv is None:
        print('Brak uv (https://docs.astral.sh/uv/). Zainstaluj go i ponów `tools`.')
        return 1
    if not SPEECH_PYTHON.exists():
        print('Tworzę środowisko Pythona 3.13 dla rozpoznawania mowy…')
        subprocess.run([str(uv), 'venv', '--python', '3.13', str(SPEECH)], check=True)
    print('Instaluję faster-whisper i biblioteki CUDA…')
    subprocess.run([str(uv), 'pip', 'install', '--quiet', '--python', str(SPEECH_PYTHON), *SPEECH_PACKAGES], check=True)
    if not (WHISPER / 'model.bin').exists():
        print('Pobieram model Whisper large-v3-turbo (1,6 GB)…')
        subprocess.run([str(SPEECH_PYTHON), '-c', 'import sys; from huggingface_hub import snapshot_download; '
                        'snapshot_download(sys.argv[1], local_dir=sys.argv[2])', WHISPER_REPO, str(WHISPER)], check=True)
    print('Test rozpoznawania mowy na GPU…')
    test = subprocess.run([str(SPEECH_PYTHON), '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); '
                           'from gramadrama.speech import main; sys.exit(main(["--self-test"]))', str(SOURCE)])
    return test.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['status', 'install', 'uninstall', 'setup', 'teardown', 'tools'])
    parser.add_argument('--layout', choices=KEYBOARD_LAYOUTS, help='układ klawiatury dla install; niezależny od języków tekstu')
    args = parser.parse_args()
    if args.layout and args.command != 'install':
        parser.error('--layout dotyczy tylko polecenia install')
    return {'status': lambda: status() or 0, 'install': lambda: install(args.layout), 'uninstall': uninstall,
            'setup': setup, 'teardown': teardown, 'tools': install_tools}[args.command]()


if __name__ == '__main__':
    sys.exit(main())
