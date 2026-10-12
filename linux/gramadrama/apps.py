"""The Aplikacje list of the GramaDrama app (docs/apps.md): the apps where GramaDrama is off
(apps.json), the known and the installed apps, whether a Chromium app shows its windows through
X11 or Wayland, what the engine log says about each app, and the launch fix that starts a Chromium
or Electron app natively on Wayland, so that the engine gets the text of its fields.

Plain Python without GTK, like checks.py: the decisions (the state of a row, the text of a fix)
are pure functions, and the scans of .desktop files, PATH and /proc are kept apart for the app's
worker threads. The engine never imports this module; it reads apps.json itself (paths.py).
Every file the launch fix writes carries MARKER, and no other file is ever overwritten or deleted."""
import dataclasses
import fnmatch
import gettext
import json
import os
from pathlib import Path
import re
import shlex
import stat

from .i18n import tr
from . import checks, paths
from .engine import applist

SELF = 'pl.gramadrama.Linux.desktop'  # the GramaDrama app: its Wypróbuj field always works
# The GramaDrama app is never on the list: installed (SELF), or run from the repository without its
# .desktop file, when GNOME Shell reports its window by the window's class.
SELF_IDS = frozenset((SELF, 'window:pl.gramadrama.Linux'))
SHELL = 'org.gnome.Shell'  # the key focus in the shell: Activities search, the run dialog
VERSION = 1
MAX_BYTES = 64 * 1024  # a larger apps.json is broken, for the engine too (engine/applist.py)
APP_OFF = 'app_off'  # the engine's refusal in an app that is switched off


# ---- apps.json -------------------------------------------------------------------------------------
@dataclasses.dataclass
class Config:
    """apps.json: disabled holds the apps where GramaDrama is off (desktop IDs or window:<wm_class>),
    added the ones the user put on the list by hand ("Dodaj aplikację…"), modes the mode Ctrl+Alt+G starts
    in per app ({app ID: mode key}; no entry is Popraw, a mode this build does not know is kept and means
    Popraw), sites the sites (host names, lower case, without www.) where the browser extension is off
    (docs/browser.md)."""
    disabled: list = dataclasses.field(default_factory=list)
    added: list = dataclasses.field(default_factory=list)
    modes: dict = dataclasses.field(default_factory=dict)
    sites: list = dataclasses.field(default_factory=list)


def _ids(value):
    """App IDs from apps.json, without duplicates and without SELF; None when it is not a list of
    non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        return None
    return [v for v in dict.fromkeys(value) if v != SELF]


def read_config():
    """(config, problem): apps.json as the engine uses it, and why it cannot (a Polish reason, as
    checks.read_settings gives them) or None. A missing file switches nothing off; so does a broken
    one, since the engine then allows every app (fail-open) until the next change repairs it. The
    engine calls exactly the same files broken (engine/applist.py)."""
    path = paths.apps_path()
    try:
        with open(path, 'rb') as f:
            data = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return Config(), None
    except OSError as exc:
        return Config(), tr('nie można odczytać {path} ({value2})', path=path, value2=exc.strerror or exc)
    if len(data) > MAX_BYTES:
        return Config(), tr('{path} jest za duży (ponad {value2:d} KiB)', path=path, value2=MAX_BYTES // 1024)
    try:
        data = json.loads(data.decode('utf-8'))
    except UnicodeError:
        return Config(), tr('{path} nie jest zapisany w UTF-8', path=path)
    except (ValueError, RecursionError):  # deep nesting overflows the parser
        return Config(), tr('{path} nie jest poprawnym plikiem JSON', path=path)
    if not isinstance(data, dict):
        return Config(), tr('{path} nie zawiera listy aplikacji', path=path)
    if data.get('v') != VERSION or isinstance(data.get('v'), bool):
        return Config(), tr('{path} ma nieobsługiwaną wersję (v)', path=path)
    lists = {key: _ids(data.get(key, [])) for key in ('disabled', 'added')}
    bad = [key for key, value in lists.items() if value is None]
    if bad:
        return Config(), tr('„{value}” w {path} nie jest listą identyfikatorów aplikacji', value=bad[0], path=path)
    modes = data.get('modes', {})
    if not isinstance(modes, dict) or not all(app and isinstance(mode, str) for app, mode in modes.items()):
        return Config(), tr('„modes” w {path} nie jest obiektem „aplikacja: tryb”', path=path)
    sites = data.get('sites', [])
    if not isinstance(sites, list) or not all(isinstance(site, str) and site for site in sites):
        return Config(), tr('„sites” w {path} nie jest listą stron', path=path)
    return Config(modes={app: mode for app, mode in modes.items() if app != SELF}, sites=list(dict.fromkeys(sites)), **lists), None


def config_check(problem):
    """The note at the top of the section while apps.json is broken; None when it is fine."""
    if not problem:
        return None
    return checks.Check(checks.WARNING, tr('Lista aplikacji jest nieprawidłowa: {problem}. Do czasu naprawy GramaDrama działa we wszystkich aplikacjach. Przełączenie dowolnej aplikacji zapisze nową listę, a stary plik zostanie zachowany jako apps.json.bak.', problem=problem))


def _write(config, path):
    """Atomically, as core.Settings.save: a temp file with mode 0600, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        data = {'v': VERSION, 'disabled': config.disabled, 'added': config.added}
        if config.modes:  # a file that uses no mode stays as it was
            data['modes'] = config.modes
        if config.sites:  # nor does one that switches off no site
            data['sites'] = config.sites
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.chmod(temp, 0o600)  # a temp file left over from a crash keeps its old mode through O_CREAT
    os.replace(temp, path)


def _change(update):
    """Apply update(config) to apps.json; (error, backup) as checks.save_setting returns them. A
    broken file is kept as apps.json.bak (backup) and replaced by the change alone: while it was
    broken the engine switched nothing off, and neither did the list."""
    config, problem = read_config()
    update(config)
    path, backup = paths.apps_path(), None
    try:
        if problem and os.path.lexists(path):
            backup = path.with_name(path.name + '.bak')
            os.replace(path, backup)
        _write(config, path)
    except OSError as exc:
        if backup is not None and not os.path.lexists(path):
            try:
                os.replace(backup, path)  # the user's file stays where it was
            except OSError:
                pass
            backup = None
        return tr('Nie udało się zapisać listy aplikacji: {value}', value=exc.strerror or exc), backup
    return None, backup


def set_enabled(app_id, enabled):
    """Switch GramaDrama on or off in an app; the engine reloads apps.json when it changes."""
    def update(config):
        if enabled:
            config.disabled = [a for a in config.disabled if a != app_id]
        elif app_id != SELF and app_id not in config.disabled:
            config.disabled.append(app_id)
    return _change(update)


def set_site(site, disabled):
    """Switch the browser extension off or on on a site (a host name as the extension gives it); the engine tells the
    extensions when apps.json changes."""
    def update(config):
        if disabled:
            if site not in config.sites:
                config.sites.append(site)
        else:
            config.sites = [s for s in config.sites if s != site]
    return _change(update)


def set_mode(app_id, mode):
    """The mode Ctrl+Alt+G starts in for an app. Popraw, the default, takes the entry away; a mode that
    cannot be a default (Tłumacz has Ctrl+Alt+J, and an unknown one) is not written. The engine reloads
    apps.json when it changes."""
    def update(config):
        if app_id == SELF:
            return
        if mode in applist.MODE_CHOICES and mode != applist.DEFAULT_MODE:
            config.modes[app_id] = mode
        else:
            config.modes.pop(app_id, None)
    return _change(update)


def add(app_id):
    """Put an app on the list by hand ("Dodaj aplikację…")."""
    def update(config):
        if app_id != SELF and app_id not in config.added:
            config.added.append(app_id)
    return _change(update)


def remove(app_id):
    """Take an app the user added off the list: out of "added" only. Whether GramaDrama works there
    stays as it is; a switched-off app keeps its row, without a remove button (listed)."""
    def update(config):
        config.added = [a for a in config.added if a != app_id]
    return _change(update)


# ---- the catalogue ------------------------------------------------------------------------------------
CHROMIUM, GTK3, GTK4, TERMINAL, OFFICE = 'chromium', 'gtk3', 'gtk4', 'terminal', 'office'
# Known apps and what to expect there before the log has any evidence (docs/apps.md, section 4).
# The Snap and Flatpak IDs of the same apps are here too: a Flatpak app is known only from here,
# its files are in the sandbox (section 5). A key may be a glob pattern.
APP_CATALOGUE = {
    # works on Wayland, needs the launch fix on X11
    'google-chrome.desktop': CHROMIUM, 'com.google.Chrome.desktop': CHROMIUM,
    'chromium_chromium.desktop': CHROMIUM, 'chromium.desktop': CHROMIUM, 'chromium-browser.desktop': CHROMIUM,
    'org.chromium.Chromium.desktop': CHROMIUM,
    'chatgpt.desktop': CHROMIUM,
    'com.anthropic.Claude.desktop': CHROMIUM,
    'slack.desktop': CHROMIUM, 'slack_slack.desktop': CHROMIUM, 'com.slack.Slack.desktop': CHROMIUM,
    'discord.desktop': CHROMIUM, 'discord_discord.desktop': CHROMIUM, 'com.discordapp.Discord.desktop': CHROMIUM,
    'code.desktop': CHROMIUM, 'code_code.desktop': CHROMIUM, 'com.visualstudio.code.desktop': CHROMIUM,
    'obsidian.desktop': CHROMIUM, 'obsidian_obsidian.desktop': CHROMIUM, 'md.obsidian.Obsidian.desktop': CHROMIUM,
    'signal-desktop.desktop': CHROMIUM, 'signal-desktop_signal-desktop.desktop': CHROMIUM,
    'org.signal.Signal.desktop': CHROMIUM,
    # not confirmed (never tested)
    'firefox_firefox.desktop': GTK3, 'firefox.desktop': GTK3, 'org.mozilla.firefox.desktop': GTK3,
    'thunderbird*.desktop': GTK3, 'org.mozilla.Thunderbird.desktop': GTK3, 'net.thunderbird.Thunderbird.desktop': GTK3,
    'org.gnome.Evolution.desktop': GTK3, 'org.gnome.Geary.desktop': GTK3,
    # works; after a mouse selection, press an arrow key first (docs/architecture.md, Text safety)
    'org.gnome.TextEditor.desktop': GTK4, 'org.gnome.Notes.desktop': GTK4,
    # not supported: the engine refuses terminals
    'org.gnome.Ptyxis.desktop': TERMINAL, 'org.gnome.Terminal.desktop': TERMINAL, 'org.gnome.Console.desktop': TERMINAL,
    # not confirmed
    'libreoffice-writer.desktop': OFFICE,
}


def catalogue_kind(app_id):
    """The kind of a known app, or None."""
    if app_id in APP_CATALOGUE:
        return APP_CATALOGUE[app_id]
    return next((kind for pattern, kind in APP_CATALOGUE.items()
                 if '*' in pattern and fnmatch.fnmatchcase(app_id, pattern)), None)


# What to expect in an app of each kind, as a hint in "Dodaj aplikację…" (the catalogue's table).
EXPECTATIONS = {
    CHROMIUM: 'działa natywnie na Wayland, przez X11 potrzebuje poprawki uruchamiania',
    GTK3: 'niepotwierdzone',
    GTK4: 'działa · po zaznaczeniu myszą najpierw naciśnij strzałkę',
    TERMINAL: 'terminal nie udostępnia tekstu',
    OFFICE: 'niepotwierdzone',
}


def expectation(kind):
    """The hint for an app of this kind (kind_of()); '' when nothing is known."""
    return tr(EXPECTATIONS.get(kind, ''))


# ---- installed apps (.desktop files) ------------------------------------------------------------------
ESCAPES = {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}
DESKTOP_MAX = 1 << 20  # bytes of a .desktop file read at most
GETTEXT_DOMAINS = ('X-GNOME-Gettext-Domain', 'X-Ubuntu-Gettext-Domain')
LOCALE_DIR = None  # where the gettext catalogues are: None is gettext's own default (/usr/share/locale)


@dataclasses.dataclass(frozen=True)
class Entry:
    """The .desktop file that counts for a desktop ID."""
    id: str
    path: Path  # None when only our override of it is left
    name: str
    icon: str = ''  # an icon name or a path
    exec: str = ''  # Exec after the key file's own escapes
    flatpak: bool = False  # exported by Flatpak (X-Flatpak)
    override: Path = None  # our launch fix standing in for path in ~/.local/share/applications


def data_dirs():
    """XDG_DATA_HOME, then XDG_DATA_DIRS (only absolute entries count)."""
    value = os.environ.get('XDG_DATA_DIRS') or '/usr/local/share/:/usr/share/'
    return list(dict.fromkeys([checks.data_home()] + [Path(d) for d in value.split(':') if os.path.isabs(d)]))


def languages(environ=None):
    """Locale names for localized keys (Name[pl_PL], then Name[pl]), in GLib's order."""
    environ = os.environ if environ is None else environ
    value = next((environ[v] for v in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG') if environ.get(v)), '')
    names = []
    for locale in value.split(':'):
        match = re.fullmatch(r'([^_.@]+)(_[^.@]+)?(\.[^@]*)?(@.+)?', locale)
        if match:
            lang, country, _encoding, modifier = match.groups(default='')
            names += [lang + country + modifier, lang + country, lang + modifier, lang]
    return [n for n in dict.fromkeys(names) if n not in ('C', 'POSIX')]


def unescape(raw):
    """(value, where): a key file string with its escapes (\\s \\n \\t \\r \\\\) processed, and where
    in raw each character of value starts (one more index for the end). GLib refuses a value with
    another escape; here the backslash just stays."""
    value, where, i = [], [], 0
    while i < len(raw):
        where.append(i)
        if raw[i] == '\\' and raw[i + 1:i + 2] in ESCAPES:
            value.append(ESCAPES[raw[i + 1]])
            i += 2
        else:
            value.append(raw[i])
            i += 1
    where.append(len(raw))
    return ''.join(value), where


def parse_desktop(text):
    """{group: {key: raw value}} of a key file; comments and broken lines are skipped, and a
    repeated key keeps its last value. Localized keys stay as they are ('Name[pl]')."""
    groups, group = {}, None
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            group = groups.setdefault(line[1:-1], {})
        elif group is not None and '=' in line:
            key, value = line.split('=', 1)
            group[key.strip()] = value.strip().split('\0', 1)[0]  # GLib reads a value up to a NUL byte
    return groups


def _true(value):
    return (value or '').strip() in ('true', '1')


def _name(group, names):
    """Name in the user's language, as GLib gives it: Name[pl_PL] or Name[pl], else Name translated
    in the file's gettext domain (Ubuntu keeps the translations of its apps there), else Name."""
    localized = next((group['Name[%s]' % n] for n in names if 'Name[%s]' % n in group), None)
    if localized is not None:
        return unescape(localized)[0]
    name = unescape(group.get('Name', ''))[0]
    domain = next((group[key] for key in GETTEXT_DOMAINS if group.get(key)), None)
    if name and domain and names:
        try:
            name = gettext.translation(domain, LOCALE_DIR, languages=list(names), fallback=True).gettext(name)
        except Exception:  # any app's broken catalogue (charset=CHARSET, a header cut short…): the name as it is
            pass
    return name


def _executable(path):
    return path is not None and os.path.isfile(path) and os.access(path, os.X_OK)


def read_entry(app_id, file, names=(), path=()):
    """The Entry of a .desktop file; None when it is no application to list: not Type=Application,
    Hidden (a user's way to delete it), or its TryExec program is missing (path: the PATH to look
    it up in, as GLib does). NoDisplay apps are read too: the log or apps.json may name them."""
    try:
        with open(file, 'rb') as f:
            text = f.read(DESKTOP_MAX).decode('utf-8', 'replace')
    except OSError:
        return None
    group = parse_desktop(text).get('Desktop Entry')
    if not group or group.get('Type') != 'Application' or _true(group.get('Hidden')):
        return None
    try_exec = unescape(group.get('TryExec', ''))[0]
    if try_exec and not (_executable(try_exec) if '/' in try_exec else which(try_exec, path)):
        return None
    return Entry(app_id, Path(file), _name(group, names) or app_id.removesuffix('.desktop'),
                 unescape(group.get('Icon', ''))[0], unescape(group.get('Exec', ''))[0], 'X-Flatpak' in group)


def _desktop_files(directory):
    """(desktop ID, file) under an applications directory; a file in a subdirectory has it in its
    ID (kde4/a.desktop is kde4-a.desktop)."""
    found = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        rel = os.path.relpath(root, directory)
        prefix = '' if rel == '.' else rel.replace(os.sep, '-') + '-'
        found += [(prefix + name, Path(root) / name) for name in sorted(files) if name.endswith('.desktop')]
    return found


def installed_apps(dirs=None, path=None, names=None):
    """{desktop ID: Entry} of the apps in the XDG data dirs (data_dirs()). As in GLib, the first file
    of an ID counts, and one that is not an application to list hides the ID. Our override in the
    first directory (XDG_DATA_HOME) stands aside for the file it copies. path: the PATH for TryExec;
    names: languages()."""
    dirs = data_dirs() if dirs is None else [Path(d) for d in dirs]
    path = os.environ.get('PATH', '').split(':') if path is None else path
    names = languages() if names is None else names
    entries, seen, overrides = {}, set(), {}
    for index, base in enumerate(dirs):
        for app_id, file in _desktop_files(base / 'applications'):
            if app_id in seen:
                continue
            if index == 0 and is_ours(file):
                overrides[app_id] = file
                continue
            seen.add(app_id)
            entry = read_entry(app_id, file, names, path)
            if entry is not None:
                entries[app_id] = dataclasses.replace(entry, override=overrides.get(app_id))
    for app_id, file in overrides.items():
        entry = read_entry(app_id, file, names, path) if app_id not in seen else None
        if entry is not None:  # the app it copies is gone
            entries[app_id] = dataclasses.replace(entry, path=None, override=file)
    return entries


# ---- the program behind an app (docs/apps.md, section 5) ------------------------------------------
ENV_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
FIELD_CODES = frozenset('%' + c for c in 'fFuUickdDnNvm')
SNAP_DEPTH = 3


def split_exec(command):
    """[(argument, end)] of an Exec value (after the key file's escapes), quoted as the Desktop Entry
    spec says and read as GLib's g_shell_parse_argv reads it; end is the index in command just
    after the argument. None when GLib refuses it (an open quote, a final backslash)."""
    args, i, n = [], 0, len(command)
    while True:
        while i < n and command[i] in ' \t\n':
            i += 1
        if i < n and command[i] == '#':  # a word that starts with # starts a comment, to the end of the line
            i = command.find('\n', i)
            i = n if i < 0 else i
            continue
        if i >= n:
            return args
        arg = []
        while i < n and command[i] not in ' \t\n':
            c = command[i]
            if c == "'":
                end = command.find("'", i + 1)
                if end < 0:
                    return None
                arg.append(command[i + 1:end])
                i = end + 1
            elif c == '"':
                i += 1
                while i < n and command[i] != '"':
                    if command[i] == '\\' and command[i + 1:i + 2] in ('"', '\\', '`', '$', '\n'):
                        i += 1
                    arg.append(command[i])
                    i += 1
                if i >= n:
                    return None
                i += 1
            elif c == '\\':
                if i + 1 >= n:
                    return None
                if command[i + 1] != '\n':  # a backslash before a newline joins the lines
                    arg.append(command[i + 1])
                i += 2
            else:
                arg.append(c)
                i += 1
        args.append((''.join(arg), i))


def exec_argv(command):
    """The arguments of an Exec value, or None when it cannot be parsed."""
    args = split_exec(command)
    return None if args is None else [arg for arg, _end in args]


def program_index(argv):
    """Where the program is among an Exec's arguments: argv[0], or the first one after `env` and its
    NAME=value assignments (snaps start so). None when there is none or env has options."""
    index = 0
    if argv and os.path.basename(argv[0]) == 'env':
        index = 1
        while index < len(argv) and ENV_ASSIGNMENT.match(argv[index]):
            index += 1
    if index >= len(argv) or not argv[index] or argv[index].startswith('-') or argv[index] in FIELD_CODES:
        return None
    return index


def _same_dir(a, b):
    a, b = os.path.normpath(str(a)), os.path.normpath(str(b))
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def which(name, dirs, skip=None):
    """The program a bare name runs: the first executable file of that name in dirs (PATH entries),
    leaving out relative entries and skip (~/.local/bin: our wrapper must not hide the program)."""
    for directory in dirs:
        if not os.path.isabs(directory) or skip is not None and _same_dir(directory, skip):
            continue
        candidate = os.path.join(directory, name)
        if _executable(candidate):
            return Path(candidate)
    return None


def _position(dirs, directory):
    return next((i for i, d in enumerate(dirs) if os.path.isabs(d) and _same_dir(d, directory)), None)


def is_chromium_dir(directory):
    """A directory with Chromium's resources: resources.pak, or chrome_100_percent.pak with icudtl.dat."""
    def has(name):
        return os.path.isfile(os.path.join(directory, name))
    return has('resources.pak') or has('chrome_100_percent.pak') and has('icudtl.dat')


def _subdirs(directory):
    try:
        with os.scandir(directory) as it:
            return sorted(e.path for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def snap_dir(root, depth=SNAP_DEPTH):
    """The directory with Chromium's resources in a snap (root: /snap/<name>/current), at most depth
    levels below root, breadth first; its realpath, or None."""
    level = [str(root)]
    for down in range(depth + 1):
        found = next((d for d in level if is_chromium_dir(d)), None)
        if found is not None:
            return Path(os.path.realpath(found))
        if down < depth:
            level = [sub for d in level for sub in _subdirs(d)]
    return None


@dataclasses.dataclass(frozen=True)
class Places:
    """Where the launch fix looks and writes."""
    path: tuple  # GNOME Shell's PATH entries: where it finds a bare Exec name
    bin_dir: Path  # ~/.local/bin: the wrappers
    applications: Path  # $XDG_DATA_HOME/applications: the overrides
    snap: Path = Path('/snap')


def _shells(proc='/proc'):
    """[(start in clock ticks after boot, environ or None)] of this user's gnome-shell processes, the
    one that started the app first: its parent, then one on its session bus (another gnome-shell of
    the user, such as a nested or a test session, is on another bus), then from the oldest."""
    uid, parent, found = os.getuid(), os.getppid(), []
    bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    try:
        pids = [int(n) for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        base = os.path.join(proc, str(pid))
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, 'comm'), 'rb') as f:
                if f.read().strip() != b'gnome-shell':
                    continue
            with open(os.path.join(base, 'stat'), 'rb') as f:
                data = f.read()
            started = int(data[data.rindex(b')') + 2:].split()[19])  # field 22, starttime
        except (OSError, ValueError, IndexError):
            continue
        try:
            with open(os.path.join(base, 'environ'), 'rb') as f:
                environ = f.read().split(b'\0')
        except OSError:
            environ = None
        same_bus = bool(bus) and environ is not None and os.fsencode('DBUS_SESSION_BUS_ADDRESS=' + bus) in environ
        found.append((pid != parent, not same_bus, started, pid, environ))
    return [(started, environ) for _other, _bus, started, _pid, environ in sorted(found, key=lambda s: s[:4])]


def shell_path(proc='/proc', fallback=None):
    """GNOME Shell's PATH, which it starts apps with (/proc/PID/environ): of the gnome-shell that
    started the app (_shells). fallback (default: our own PATH) when there is none to read."""
    for _started, environ in _shells(proc):
        value = next((item[5:] for item in environ or () if item.startswith(b'PATH=')), None)
        if value is not None:
            return os.fsdecode(value)
    return os.environ.get('PATH', '') if fallback is None else fallback


def shell_started(proc='/proc'):
    """When the gnome-shell that started the app (_shells) started, in seconds since the epoch; None
    when unknown. It reads .desktop files for its app grid only then (Fix.pending)."""
    shells = _shells(proc)
    if not shells:
        return None
    try:
        with open(os.path.join(proc, 'stat'), 'rb') as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith(b'btime '))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + shells[0][0] / os.sysconf('SC_CLK_TCK')


def find_places(proc='/proc', path=None):
    """Places of this user; path: a PATH instead of GNOME Shell's (shell_path)."""
    path = shell_path(proc) if path is None else path
    return Places(tuple(path.split(':')), Path.home() / '.local' / 'bin', checks.data_home() / 'applications')


@dataclasses.dataclass(frozen=True)
class Detection:
    """The program an app's Exec starts."""
    program: str = None  # as written: 'chatgpt', '/usr/bin/slack'; None when Exec cannot be read
    found: Path = None  # the file: a bare name looked up in PATH without ~/.local/bin; None if missing
    directory: Path = None  # the directory with Chromium's resources (realpath): a Chromium or Electron app
    flatpak: bool = False  # `flatpak run` or exported by Flatpak: no launch fix in v1

    @property
    def chromium(self):
        return self.directory is not None


def detect(entry, places):
    """What an entry starts, and whether it is Chromium or Electron: the directory of the launcher's
    realpath, or for a snap the snap's files, hold Chromium's resources (blocking: a few stats, a
    walk of a snap)."""
    argv = exec_argv(entry.exec)
    index = None if argv is None else program_index(argv)
    if index is None:
        return Detection(flatpak=entry.flatpak)
    program = argv[index]
    if entry.flatpak or os.path.basename(program) == 'flatpak' and 'run' in argv[index + 1:]:
        return Detection(program, flatpak=True)  # its files are in the sandbox
    if '/' not in program:
        found = which(program, places.path, skip=places.bin_dir)
    else:
        found = Path(program) if os.path.isabs(program) and _executable(program) else None
    if found is None:
        return Detection(program)
    if found.parent == places.snap / 'bin':  # a link to /usr/bin/snap: /snap/bin/<snap>[.<app>]
        return Detection(program, found, snap_dir(places.snap / found.name.split('.')[0] / 'current'))
    directory = os.path.dirname(os.path.realpath(found))
    return Detection(program, found, Path(directory) if is_chromium_dir(directory) else None)


def kind_of(app_id, detection):
    """The catalogue's kind, else CHROMIUM when detected, else None."""
    return catalogue_kind(app_id) or (CHROMIUM if detection is not None and detection.chromium else None)


# ---- X11 or Wayland: the GPU process ------------------------------------------------------------------
def gpu_platforms(proc='/proc'):
    """{directory: 'x11' or 'wayland'}: how this user's running Chromium and Electron apps show their
    windows. The browser process passes its platform to its GPU process (--ozone-platform); the
    directory is the one of the GPU process's program (realpath), as Detection.directory. When
    instances of one program differ, X11 wins."""
    uid, found = os.getuid(), {}
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return found
    for pid in pids:
        base = os.path.join(proc, pid)
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, 'cmdline'), 'rb') as f:
                argv = f.read().rstrip(b'\0').split(b'\0')
            if len(argv) == 1:  # Chromium rewrites its process title: one string, the arguments split by spaces
                argv = argv[0].split(b' ')
            if b'--type=gpu-process' not in argv:
                continue
            exe = os.readlink(os.path.join(base, 'exe'))
        except OSError:
            continue
        platform = next((a[len(b'--ozone-platform='):] for a in reversed(argv) if a.startswith(b'--ozone-platform=')), b'')
        if platform in (b'x11', b'wayland'):
            directory = Path(os.path.realpath(os.path.dirname(exe.removesuffix(' (deleted)'))))
            if found.get(directory) != 'x11':
                found[directory] = platform.decode()
    return found


# ---- evidence from the engine log ---------------------------------------------------------------------
@dataclasses.dataclass
class Evidence:
    """What the engine log says about one app (docs/apps.md, section 5)."""
    seen: bool = False  # Ctrl+Alt+G was pressed there (a start or a refusal at the hotkey)
    ok_count: int = 0  # verified corrections
    ok_at: float = None  # the last one
    refusal: str = None  # the reason of the last refusal at the hotkey (app_off aside)
    refusal_win: str = None  # the window then: 'wayland', 'x11' or 'shell'
    x11_no_text: bool = False  # a no_text refusal on X11, and no capture or refusal on Wayland there since
    last_ok: bool = False  # the last Ctrl+Alt+G there ended in a verified correction


def log_evidence(events):
    """{app ID: Evidence} from the engine's events (checks.parse_log). The engine writes the focused
    app into start, refused at the hotkey, apply and verified (docs/apps.md, section 3); a verified
    event without it is matched to its session's start. The GramaDrama app is kept too."""
    found, starts, pending = {}, {}, {}
    for event in events:
        ev, app, sid, t = event.get('ev'), event.get('app'), event.get('id'), event.get('t')
        sid = sid if isinstance(sid, (int, str)) else None
        t = t if isinstance(t, (int, float)) and not isinstance(t, bool) else None
        if ev == 'verified' and not app:
            app = starts.get(sid)
        if ev not in ('start', 'refused', 'verified') or not isinstance(app, str) or not app:
            continue
        if ev == 'refused' and event.get('at') != 'hotkey':
            continue  # at 'apply': a session that had started
        evidence = found.setdefault(app, Evidence())
        if ev == 'start':
            evidence.seen, evidence.last_ok, evidence.x11_no_text = True, False, False  # the app gave its text
            starts[sid], pending[app] = app, sid
        elif ev == 'refused':
            evidence.seen = True
            if event.get('reason') == APP_OFF:
                continue  # off there: says nothing about the app
            evidence.refusal, evidence.refusal_win = event.get('reason'), event.get('win')
            evidence.last_ok = False
            pending.pop(app, None)
            if evidence.refusal_win == 'wayland':
                evidence.x11_no_text = False  # it runs natively now: an older refusal on X11 is history
            elif evidence.refusal == 'no_text' and evidence.refusal_win == 'x11':
                evidence.x11_no_text = True
        elif event.get('outcome') == 'ok':
            evidence.ok_count += 1
            evidence.ok_at = t
            if app in pending and pending[app] == sid:
                evidence.last_ok = True
    return found


# ---- the launch fix (docs/apps.md, section 6) -----------------------------------------------------------
# Experiment E1 (tests/integration_app.py --e1) on GNOME Shell 50: a wrapper counts at the next start
# from any launcher, since the bare name is looked up in PATH at every start. An override counts about
# 5 s after it is written for search and the favorites (dash, Ubuntu Dock), but the app grid keeps the
# command it read at login, whether the app was running or not, and whatever is rewritten. So an
# override written during this login counts everywhere only after the next one (Fix.pending).
MARKER = '# GramaDrama: '
FLAG = '--ozone-platform=wayland'
WRAPPER, OVERRIDE = 'wrapper', 'override'
EXEC_LINE = re.compile(r'([ \t]*Exec[ \t]*=[ \t]*)(.*?)(\r?)')
FLATPAK_REASON = 'aplikacja z Flatpaka potrzebuje zmiany uprawnień'
# Runtimes that start other apps' code (electron30 /usr/lib/notes/app.asar): a wrapper takes a name
# for the whole session, so such a program gets no wrapper, only the override of the one app.
SHARED_RUNTIME = re.compile(r'(electron|nw)[0-9.]*')


def is_ours(path):
    """True when path is a regular file (never a symlink) the launch fix wrote: its first line, or
    the second one after #!, starts with MARKER."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):  # ValueError: a NUL byte in the name
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        lines = os.read(fd, 4096).split(b'\n', 2)
    except OSError:
        return False
    finally:
        os.close(fd)
    first = lines[1] if lines[0].startswith(b'#!') and len(lines) > 1 else lines[0]
    return first.startswith(MARKER.encode())


def _one_line(text):
    return ' '.join(str(text).split())


def _tilde(path):
    home, text = str(Path.home()), str(path)
    return '~' + text[len(home):] if text.startswith(home + '/') else text


def _read_text(path):
    """A file's text as it is, bytes that are not UTF-8 included (written back the same way)."""
    with open(path, 'rb') as f:
        return f.read(DESKTOP_MAX).decode('utf-8', 'surrogateescape')


def wrapper_text(name, target):
    """~/.local/bin/<name> for a bare Exec name, as the first one (ChatGPT, 2026-09-27)."""
    return ('#!/bin/sh\n'
            '%srun %s natively on Wayland, so IBus engines get the field text.\n'
            '# Delete this file to undo.\n'
            'exec %s %s "$@"\n' % (MARKER, _one_line(name), shlex.quote(str(target)), FLAG))


def wrapper_target(path):
    """The program our wrapper runs (its exec line), or None."""
    try:
        text = _read_text(path)
    except OSError:
        return None
    for line in text.split('\n'):
        if line.startswith('exec '):
            try:
                argv = shlex.split(line)
            except ValueError:
                return None
            return Path(argv[1]) if len(argv) > 1 else None
    return None


def with_flag(raw):
    """An Exec value as written in the file, with FLAG after its program (after env's assignments);
    unchanged when it cannot be parsed or already has FLAG there."""
    value, where = unescape(raw)
    args = split_exec(value)
    index = program_index([arg for arg, _end in args]) if args else None
    if index is None or index + 1 < len(args) and args[index + 1][0] == FLAG:
        return raw
    cut = where[args[index][1]]
    return raw[:cut] + ' ' + FLAG + raw[cut:]


def override_text(original, name, source):
    """A copy of a .desktop file with FLAG in every Exec line, [Desktop Action] groups included."""
    lines = ['%srun %s natively on Wayland, so IBus engines get the field text.' % (MARKER, _one_line(name)),
             '# Delete this file to undo. It is a copy of %s with %s.' % (_one_line(source), FLAG)]
    for line in original.removeprefix('\ufeff').split('\n'):  # a BOM below our lines would break the group line
        match = EXEC_LINE.fullmatch(line)
        lines.append(match.group(1) + with_flag(match.group(2)) + match.group(3) if match else line)
    text = '\n'.join(lines)
    return text if text.endswith('\n') else text + '\n'


def _body(text):
    """An override without its two comment lines: what depends on the original only."""
    return text.split('\n', 2)[2] if text.count('\n') >= 2 else text


@dataclasses.dataclass(frozen=True)
class Plan:
    """The launch fix for an app: a WRAPPER or an OVERRIDE, its file and text; kind None when no fix
    is possible, with the reason (Polish, for the row)."""
    kind: str = None
    path: Path = None
    text: str = ''
    reason: str = ''


def plan_fix(app_id, entry, detection, kind, places):
    """What the launch fix writes for an app: a wrapper in ~/.local/bin for a bare Exec name when
    that directory comes before the program's in GNOME Shell's PATH, else an override .desktop file
    (it counts everywhere only after the next login, see Fix.pending). Only when the program Exec
    starts is itself Chromium or Electron (detect), whatever the catalogue says: after a shell, an
    interpreter or a launcher such as firejail the flag would break the command, and a wrapper
    named after one would take that name from every program of the session. No wrapper for a
    shared runtime (SHARED_RUNTIME), nothing for Flatpak, and never over a file that is not ours
    (blocking: reads the .desktop file)."""
    if entry is not None and entry.flatpak or detection is not None and detection.flatpak:
        return Plan(reason=FLATPAK_REASON)
    if kind != CHROMIUM:
        return Plan(reason=tr('nie rozpoznano aplikacji Chromium ani Electron') if kind is None else
                    tr('poprawka jest tylko dla aplikacji Chromium i Electron'))
    if entry is None or entry.path is None:
        return Plan(reason=tr('nie znaleziono jej pliku .desktop'))
    if detection is None or detection.program is None:
        return Plan(reason=tr('nie rozpoznano polecenia, które ją uruchamia'))
    if detection.found is None:
        return Plan(reason=tr('nie znaleziono programu {value}', value=_one_line(detection.program)))
    if not detection.chromium:
        return Plan(reason=tr('nie rozpoznano Chromium ani Electron w programie {value}', value=_one_line(os.path.basename(detection.program))))
    bin_at, found_at = _position(places.path, places.bin_dir), _position(places.path, detection.found.parent)
    own_name = '/' not in detection.program and not SHARED_RUNTIME.fullmatch(detection.program)
    if own_name and bin_at is not None and found_at is not None and bin_at < found_at:
        kind, path = WRAPPER, places.bin_dir / detection.program
        text = wrapper_text(entry.name, detection.found)
    else:
        kind, path = OVERRIDE, places.applications / app_id
        try:
            text = override_text(_read_text(entry.path), entry.name, entry.path)
        except OSError:
            return Plan(reason=tr('nie można odczytać {value}', value=_tilde(entry.path)))
    if os.path.lexists(path) and not is_ours(path):
        return Plan(reason=tr('{value} już istnieje i nie pochodzi od GramaDramy', value=_tilde(path)))
    return Plan(kind, path, text)


@dataclasses.dataclass(frozen=True)
class Fix:
    """A launch fix of ours that is there, and whether it is out of date."""
    kind: str
    path: Path
    broken: bool = False
    pending: bool = False  # an override written after GNOME Shell started: the app grid ignores it until the next login


def _written_after(path, moment):
    try:
        return moment is not None and os.lstat(path).st_mtime > moment
    except OSError:
        return False


def fix_status(entry, detection, places, started=None):
    """Our fix of an app, or None. A wrapper is broken when its program is gone (a package update
    moved it), an override when the file it copies is gone or has changed since. started:
    shell_started(); an override written after it is pending."""
    fixes = []
    program = detection.program if detection is not None else None
    if program and '/' not in program and is_ours(places.bin_dir / program):
        wrapper = places.bin_dir / program
        fixes.append(Fix(WRAPPER, wrapper, not _executable(wrapper_target(wrapper))))
    if entry is not None and entry.override is not None:
        try:
            broken = entry.path is None or _body(_read_text(entry.override)) != _body(
                override_text(_read_text(entry.path), entry.name, entry.path))
        except OSError:
            broken = True
        fixes.append(Fix(OVERRIDE, entry.override, broken, _written_after(entry.override, started)))
    return next((fix for fix in fixes if fix.broken), fixes[0] if fixes else None)


def apply_fix(plan):
    """Write a planned fix (the button "Włącz"); a Polish error or None. A file that is not ours is
    never overwritten."""
    if plan.kind is None:
        return tr('Poprawka nie jest możliwa: {reason}.', reason=tr(plan.reason))
    if os.path.lexists(plan.path) and not is_ours(plan.path):
        return tr('{value} już istnieje i nie pochodzi od GramaDramy.', value=_tilde(plan.path))
    temp = plan.path.with_name('.%s.gramadrama-tmp' % plan.path.name)  # not *.desktop, not the program's name
    try:
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(plan.text.encode('utf-8', 'surrogateescape'))
        os.chmod(temp, 0o755 if plan.kind == WRAPPER else 0o644)
        os.replace(temp, plan.path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        return tr('Nie udało się zapisać {value}: {value2}', value=_tilde(plan.path), value2=exc.strerror or exc)
    return None


def undo_fix(fix):
    """Delete our fix (the button "Wyłącz poprawkę"); a Polish error or None. Only a file that is
    ours goes, and a symlink never counts as ours."""
    if not is_ours(fix.path):
        if os.path.lexists(fix.path):
            return tr('Nie usunięto {value}: to nie jest plik GramaDramy.', value=_tilde(fix.path))
        return None  # already gone
    try:
        os.unlink(fix.path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return tr('Nie udało się usunąć {value}: {value2}', value=_tilde(fix.path), value2=exc.strerror or exc)
    return None


def repair_fix(fix, plan):
    """"Napraw": write the fix again as plan_fix plans it now, in place of the old one; when no fix
    is possible any more (the program or its .desktop file is gone), just delete ours."""
    if plan.kind is not None and plan.path == fix.path:
        return apply_fix(plan)
    error = undo_fix(fix)
    if error is None and plan.kind is not None:
        return apply_fix(plan)
    return error


def remove_all_fixes(bin_dir, applications):
    """Delete every launch fix of ours (install_engine.py uninstall): the files in bin_dir and the
    .desktop files in applications that carry MARKER. Returns the deleted ones."""
    removed = []
    for directory, suffix in ((Path(bin_dir), ''), (Path(applications), '.desktop')):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = directory / name
            if name.endswith(suffix) and is_ours(path):
                try:
                    os.unlink(path)
                except OSError:
                    continue
                removed.append(path)
    return removed


# ---- the state of a row (docs/apps.md, section 7) --------------------------------------------------------
OFF, BROKEN, RESTART, FIXABLE, UNFIXABLE, UNSUPPORTED, WORKS, UNCONFIRMED = (
    'off', 'broken', 'restart', 'fixable', 'unfixable', 'unsupported', 'works', 'unconfirmed')
# The rows that ask for a click come first, apps where GramaDrama is off last.
ORDER = (BROKEN, RESTART, FIXABLE, UNFIXABLE, WORKS, UNCONFIRMED, UNSUPPORTED, OFF)
APPLY, UNDO, REPAIR, HELP = 'apply', 'undo', 'repair', 'help'
BUTTONS = {APPLY: 'Włącz', UNDO: 'Wyłącz poprawkę', REPAIR: 'Napraw', HELP: 'Jak to zrobić?'}
GTK4_NOTE = 'po zaznaczeniu myszą najpierw naciśnij strzałkę'


@dataclasses.dataclass(frozen=True)
class RowState:
    state: str  # OFF … UNCONFIRMED
    icon: str  # 'off' (dimmed), 'warning', 'restart', 'tool', 'error', 'ok', 'partial' or 'unknown'
    text: str  # the subtitle
    button: str = None  # APPLY, UNDO, REPAIR, HELP (the Flatpak part of the README) or None; the switch is apart


def row_state(name, enabled, kind, platform, fix, plan, evidence, now=None):
    """The state of an app's row; the first matching row of the table wins. kind: kind_of();
    platform: 'x11' or 'wayland' from gpu_platforms(), None when it does not run (or is no Chromium
    app); fix: fix_status(); plan: plan_fix(); evidence: log_evidence()'s for the app or None.

    Beyond the table: a running instance says more than an older refusal (on Wayland now, it is not
    "on X11"); with a fix in place only a running instance on X11 counts; an app that refused on X11
    and cannot get the fix (no Chromium, Flatpak, a foreign file) is row 5; a terminal refusal marks
    an app as a terminal only while nothing was ever corrected there (VS Code's terminal); and a fix
    in place can be switched off from rows 6 to 8 too. A pending override (Fix.pending) asks for a new
    login instead of a restart: the app grid would start the app without it until then."""
    evidence = evidence or Evidence()
    refusal = None if evidence.last_ok else evidence.refusal  # older than a verified correction: no longer true
    if not enabled:
        return RowState(OFF, 'off', tr('Wyłączona · skrót korekty trafia do aplikacji'))
    if fix is not None and fix.broken:
        return RowState(BROKEN, 'warning', tr('Poprawka uruchamiania jest nieaktualna'), REPAIR)
    if fix is not None and platform == 'x11':
        if fix.pending:
            return RowState(RESTART, 'restart', tr('Wyloguj się i zaloguj ponownie, żeby poprawka zadziałała'), UNDO)
        return RowState(RESTART, 'restart', tr('Zamknij aplikację {name} (Ctrl+Q) i uruchom ją ponownie, żeby poprawka zadziałała', name=name), UNDO)
    x11 = fix is None and (platform == 'x11' or platform is None and evidence.x11_no_text and not evidence.last_ok)
    if x11 and kind == CHROMIUM and plan is not None and plan.kind is not None:
        return RowState(FIXABLE, 'tool', tr('Działa przez X11, więc nie udostępnia tekstu. Poprawka: uruchamianie natywne'),
                        APPLY)
    if x11:
        reason = plan.reason if plan is not None and plan.reason else tr('poprawka nie jest możliwa')
        return RowState(UNFIXABLE, 'error', tr('Działa przez X11 i nie udostępnia tekstu · {reason}', reason=tr(reason)),
                        HELP if reason == FLATPAK_REASON else None)
    button = UNDO if fix is not None else None
    if kind == TERMINAL or refusal == 'terminal' and not evidence.ok_count:
        return RowState(UNSUPPORTED, 'error', tr('Terminal nie udostępnia tekstu'), button)
    if evidence.ok_count:
        parts = [tr('Działa')]
        if evidence.ok_at is not None:
            parts.append(tr('ostatnio ') + checks.when(evidence.ok_at, now))
        parts.append('%d %s' % (evidence.ok_count, checks.plural(evidence.ok_count, tr('poprawka'), tr('poprawki'), tr('poprawek'))))
        if kind == GTK4:
            parts.append(tr(GTK4_NOTE))
        elif fix is not None:
            parts.append(tr('uruchamiana natywnie (poprawka GramaDramy)'))
        return RowState(WORKS, 'partial' if kind == GTK4 else 'ok', ' · '.join(parts), button)
    native = kind == CHROMIUM and (platform == 'wayland' or fix is not None)
    hint = tr('działa natywnie, powinno działać') if native else tr('wypróbuj skrót korekty w polu tekstowym')
    if fix is not None and fix.pending and platform != 'wayland':
        hint = tr('poprawka zadziała po wylogowaniu i ponownym zalogowaniu')
    return RowState(UNCONFIRMED, 'unknown', tr('Niepotwierdzone · ') + hint, button)


# ---- the list (docs/apps.md, section 4) ------------------------------------------------------------------
NAMES = {SHELL: 'GNOME Shell', **{app_id: 'GramaDrama' for app_id in SELF_IDS}}


@dataclasses.dataclass(frozen=True)
class App:
    """A row of the Aplikacje section."""
    id: str
    name: str
    icon: str  # the .desktop Icon (a name or a path); '' for a generic icon
    enabled: bool
    removable: bool  # added by hand and on no other source: the row has a remove button
    state: RowState
    fix: Fix = None  # for UNDO and REPAIR
    plan: Plan = None  # for APPLY and REPAIR
    mode: str = applist.DEFAULT_MODE  # the mode Ctrl+Alt+G starts in here (a key of MODE_CHOICES)


def display_name(app_id, entry):
    if entry is not None:
        return entry.name
    if app_id in NAMES:
        return NAMES[app_id]
    if app_id.startswith('window:'):
        return app_id[len('window:'):] or app_id
    return app_id.removesuffix('.desktop')


def listed(config, evidence, fixed=()):
    """{desktop ID: removable} of the apps the section lists. The list grows from use, never from
    what is installed: the apps seen in the log, the switched-off ones (so that they can be switched
    on again), the ones with a mode of their own (so that it can be changed back), the ones with a launch
    fix of ours (fixed: so that it can be seen and undone) and the ones added by hand. A row added by hand, and there for no other reason, can be removed; a
    switched-off one cannot (remove keeps it off, and so on the list). Never the GramaDrama app, and
    GNOME Shell's own entries only while they are switched off."""
    used = {app for app, e in evidence.items() if e.seen} | set(fixed)
    other = used | set(config.disabled) | set(config.modes)  # reasons besides "added"
    ids = sorted(used) + config.added + config.disabled + list(config.modes)
    return {app: app in config.added and app not in other for app in dict.fromkeys(ids)
            if app and app not in SELF_IDS and (app != SHELL or app in config.disabled)}


def compose(config, evidence, entries, detections, fixes, platforms, now=None):
    """The rows of the section, ordered by state (ORDER), then name (pure). detections:
    {ID: detect()} of the installed apps; fixes: {ID: (plan_fix(), fix_status())} of the listed
    ones, and of every app with a fix of ours; platforms: gpu_platforms()."""
    rows = []
    fixed = [app_id for app_id, (_plan, fix) in fixes.items() if fix is not None]
    for app_id, removable in listed(config, evidence, fixed).items():
        entry, detection = entries.get(app_id), detections.get(app_id)
        plan, fix = fixes.get(app_id, (None, None))
        platform = platforms.get(detection.directory) if detection is not None and detection.chromium else None
        name, enabled = display_name(app_id, entry), app_id not in config.disabled
        state = row_state(name, enabled, kind_of(app_id, detection), platform, fix, plan, evidence.get(app_id), now)
        mode = config.modes.get(app_id)
        rows.append(App(app_id, name, entry.icon if entry is not None else '', enabled, removable, state, fix, plan,
                        mode if mode in applist.MODE_CHOICES else applist.DEFAULT_MODE))
    rows.sort(key=lambda row: (ORDER.index(row.state.state), row.name.casefold(), row.id))
    return rows


@dataclasses.dataclass(frozen=True)
class Scan:
    """What the app shows about apps, from one scan."""
    rows: list  # App, as compose() orders them
    names: dict  # {app ID: name} of every app in the log ("Ostatnie użycie")
    kinds: dict  # {desktop ID: kind_of()} of the installed apps (the hints in "Dodaj aplikację…")


def scan(events, config, now=None, proc='/proc', places=None, dirs=None):
    """Everything the section shows (blocking: .desktop files, PATH, /proc; for a worker thread).
    events: the engine log (checks.parse_log); config: read_config()'s."""
    places = find_places(proc) if places is None else places
    entries = installed_apps(dirs, path=places.path)
    evidence = log_evidence(events)
    detections = {app_id: detect(entry, places) for app_id, entry in entries.items()}
    started = shell_started(proc)
    present = {app_id: fix_status(entry, detections[app_id], places, started) for app_id, entry in entries.items()}
    fixes = {}
    for app_id in listed(config, evidence, [app_id for app_id, fix in present.items() if fix is not None]):
        entry, detection = entries.get(app_id), detections.get(app_id)
        fixes[app_id] = (plan_fix(app_id, entry, detection, kind_of(app_id, detection), places), present.get(app_id))
    rows = compose(config, evidence, entries, detections, fixes, gpu_platforms(proc), now)
    return Scan(rows, {app_id: display_name(app_id, entries.get(app_id)) for app_id in evidence},
                {app_id: kind_of(app_id, detection) for app_id, detection in detections.items()})
