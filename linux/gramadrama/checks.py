"""What the GramaDrama app checks: the shortcut, the input source, the engine, the panel, the
server and the engine's event log.

The decisions (parsing, summaries, the Polish texts) are pure functions, so the tests cover them
without a desktop. The probes that ask the system (GSettings, the ibus CLI, D-Bus, /proc, HTTP) are
kept apart, and the app runs the blocking ones in worker threads. Nothing here reads text from other
apps: the engine log holds events, lengths and times only."""
import dataclasses
import datetime
import errno
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import tr, get_language
from . import core, oidc
from .engine import fallback_ui, shortcuts

OK, WARNING, ERROR, INFO, BUSY = 'ok', 'warning', 'error', 'info', 'busy'
PROBLEMS = (ERROR, WARNING)
ENGINE_NAME = 'gramadrama'
ENGINE_SOURCE = ('ibus', ENGINE_NAME)
EXTENSION_UUID = 'gramadrama@gramadrama.local'
ENGINE_BUS = ('pl.gramadrama.Engine', '/pl/gramadrama/Engine', 'pl.gramadrama.Engine')
PANEL_BUS = ('org.gnome.Shell', '/pl/gramadrama/Panel', 'pl.gramadrama.Panel1')
# GNOME Shell's own export (the org.gnome.Shell.Extensions bus name is the activatable Extensions app).
EXTENSIONS_BUS = ('org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell.Extensions')
HOTKEY = (frozenset({'ctrl', 'alt'}), 'g')  # engine/keys.py: HOTKEY_MODS with KEY_g
KEYBINDING_SCHEMAS = ('org.gnome.desktop.wm.keybindings', 'org.gnome.shell.keybindings',
                      'org.gnome.mutter.keybindings', 'org.gnome.mutter.wayland.keybindings',
                      'org.gnome.settings-daemon.plugins.media-keys')
MEDIA_KEYS = 'org.gnome.settings-daemon.plugins.media-keys'
INPUT_SOURCES = 'org.gnome.desktop.input-sources'
SHELL = 'org.gnome.shell'
CUSTOM_KEYBINDING = MEDIA_KEYS + '.custom-keybinding'
# The server check sends this sentence; it is not among the model's few-shot examples (core.EXAMPLES).
SAMPLE = 'Dzieki za info, sprawdze to jutro rano i dam znac czy dziala.'
MODE_NAMES = dict(fallback_ui.MODES)
SCOPES = {'paragraph': 'akapit', 'selection': 'zaznaczenie'}
# Refusals that follow from what the user did (clicked, selected with the mouse, a password field):
# the engine works as designed, so in the Wypróbuj field they are warnings, not errors.
EXPECTED_REFUSALS = ('stale', 'selection_unknown', 'hidden_selection', 'no_field', 'sensitive', 'terminal',
                     'empty', 'too_long', 'truncated', 'embedded', 'changed')
# Log events the app reads; the log is mostly focus and content-type events, which it skips unparsed.
LOG_EVENTS = ('start', 'refused', 'result', 'backend_error', 'apply', 'verified', 'end', 'error',
              'browser_connected', 'browser_disconnected', 'browser_site')  # the last three: the Przeglądarka group follows them
LOG_MARKERS = tuple(('"ev":"%s"' % name).encode() for name in LOG_EVENTS)
ERROR_WINDOW = 24 * 3600  # engine errors of the last day count
# How a session ended (session.py: the 'why' of 'end'), in the Wypróbuj field and in "Ostatnie użycie".
ENDS = {
    'cancel': (INFO, 'Anulowano — tekst bez zmian.', 'anulowano'),
    'unchanged': (OK, 'Zero dramatu — tekst bez zmian.', 'bez zmian'),
    'closed': (INFO, 'Zamknięto.', 'zamknięto'),
    'copied': (OK, 'Skopiowano poprawkę do schowka.', 'skopiowano do schowka'),
    'hidden': (INFO, 'Panel zamknięty.', 'panel zamknięty'),
    'key': (INFO, 'Przerwano: wciśnięto inny klawisz.', 'przerwano klawiszem'),
    'focus': (INFO, 'Przerwano: fokus przeszedł gdzie indziej.', 'przerwano — zmiana fokusu'),
    'reset': (INFO, 'Przerwano: aplikacja zresetowała pole (np. po kliknięciu).', 'przerwano — reset pola'),
    'sensitive': (INFO, 'Przerwano: pole stało się prywatne.', 'przerwano — pole prywatne'),
    'timeout': (INFO, 'Wygasło. Użyj skrótu korekty ponownie.', 'wygasło'),
    'disable': (INFO, 'Przerwano: zmieniono źródło wprowadzania.', 'przerwano — zmiana źródła'),
    'error': (ERROR, 'Wystąpił błąd GramaDramy. Nic nie zmieniono.', 'błąd GramaDramy'),
}
END_DEFAULT = (INFO, 'Zamknięto.', 'przerwano')


@dataclasses.dataclass(frozen=True)
class Check:
    state: str  # OK, WARNING, ERROR, INFO or BUSY
    text: str
    action: str = None  # a fix the app offers: 'enable_panel', 'keyboard', 'install', 'install_tools', 'log', 'login', 'logout' or 'extension_folder'


# ---- where things are (as in install_engine.py and the engine) ----------------------------------------
def _xdg(name, default):
    """An XDG base directory: only an absolute value counts (an empty or relative one is ignored)."""
    value = os.environ.get(name) or ''
    return Path(value) if os.path.isabs(value) else default


def data_home():
    return _xdg('XDG_DATA_HOME', Path.home() / '.local' / 'share')


# The .deb package (scripts/build-deb.sh): the engine, the app and the browser host here, the panel among the system's
# GNOME Shell extensions; a copy from install_engine.py lives in the user's data directory instead.
SYSTEM_DIR = Path('/usr/lib/gramadrama')
SYSTEM_EXTENSIONS = Path('/usr/share/gnome-shell/extensions')


def component_file():
    """The engine's IBus description (component XML) ibus-daemon reads, or None."""
    dirs = [d for d in os.environ.get('IBUS_COMPONENT_PATH', '/usr/share/ibus/component').split(':') if d]
    return next((Path(d) / 'gramadrama.xml' for d in dirs if (Path(d) / 'gramadrama.xml').exists()), None)


def component_launcher(path=None):
    """The engine launcher the IBus description makes ibus-daemon start (in its <exec>), or None."""
    import xml.etree.ElementTree as ET
    path = path or component_file()
    if path is None:
        return None
    try:
        command = ET.parse(path).getroot().findtext('.//exec') or ''
        args = shlex.split(command)
    except (OSError, ET.ParseError, ValueError):
        return None
    return next((Path(arg) for arg in args if arg.endswith('/gramadrama-engine')), None)


def package_installed():
    """GramaDrama from the package: IBus starts the engine in SYSTEM_DIR. A user's old copy left behind does not count:
    the description decides which engine runs."""
    return component_launcher() == SYSTEM_DIR / 'gramadrama-engine'


def engine_dir():
    """Where the installed engine is: the package's directory, or the user's copy (install_engine.py)."""
    return SYSTEM_DIR if package_installed() else data_home() / 'gramadrama-engine'


def extension_dir():
    """The panel's files: the user's own extension directory wins, as in GNOME Shell; else the package's."""
    user = data_home() / 'gnome-shell' / 'extensions' / EXTENSION_UUID
    system = SYSTEM_EXTENSIONS / EXTENSION_UUID
    return system if not user.exists() and system.exists() else user


def config_home():
    return _xdg('XDG_CONFIG_HOME', Path.home() / '.config')


def browser_extension_dir(app_dir=None):
    """The directory of the browser extension to load unpacked: the installed copy (install_engine.py), else the repository's own
    (browser/extension, the Mac's too) when the app runs from there."""
    installed = engine_dir() / 'browser-extension'
    if installed.is_dir():
        return installed
    if app_dir is not None and (Path(app_dir).parent / 'browser' / 'extension').is_dir():
        return Path(app_dir).parent / 'browser' / 'extension'
    return installed


def registered_browser_hosts():
    """The browsers (names as the window shows them) that have the manifest of GramaDrama's native messaging host."""
    from . import browserhost
    found = []
    for name, relative in browserhost.BROWSER_CONFIGS:
        try:
            data = json.loads((config_home() / relative / 'NativeMessagingHosts' / (browserhost.HOST_NAME + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('name') == browserhost.HOST_NAME:
            found.append(name)
    return found


def log_path():
    """The engine's event log (engine/main.py log_path)."""
    if os.environ.get('GRAMADRAMA_ENGINE_LOG'):
        return Path(os.environ['GRAMADRAMA_ENGINE_LOG'])
    return _xdg('XDG_CACHE_HOME', Path.home() / '.cache') / 'gramadrama' / 'engine.log'


def install_info():
    """install.json written by install_engine.py: version, commit, dirty, installed_at, source,
    engine_changed_at, panel_changed_at."""
    try:
        data = json.loads((engine_dir() / 'install.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def version_label(info):
    """'0.5.2 (2064c42+zmiany)' from install.json: the version (VERSION in the repository) and the commit it was installed
    from; the commit alone for a copy installed before there was a VERSION file; '' when neither is known."""
    info = info or {}
    commit = (info.get('commit') or '')[:12]
    if commit and info.get('dirty'):
        commit += tr('+zmiany')
    version = info.get('version') or ''
    return '%s (%s)' % (version, commit) if version and commit else version or commit


def repository_version(app_dir):
    """VERSION next to linux/ when the app runs from the repository; '' without it."""
    try:
        return (Path(app_dir).parent / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def app_version_text(install, app_dir):
    """The "Wersja" row and the About dialog: the installed version, or that the app runs from the repository."""
    if Path(app_dir) != engine_dir():
        version = repository_version(app_dir)
        where = tr('z repozytorium (bez instalacji)')
        return '%s, %s' % (version, where) if version else where
    return version_label(install) or tr('nieznana')


def install_command(source, command='install'):
    if not source:
        return '/usr/bin/python3 linux/install_engine.py %s' % command
    return '/usr/bin/python3 %s %s' % (shlex.quote(str(Path(source) / 'install_engine.py')), command)


def nvidia_gpu(root='/sys/bus/pci/devices'):
    """Whether this computer has an NVIDIA graphics card (a PCI display controller of vendor 0x10de), with its driver or
    not: the local Whisper needs one (docs/dictation.md). Unknown (no PCI list) counts as yes, so nothing is hidden."""
    try:
        devices = list(Path(root).iterdir())
    except OSError:
        return True
    for device in devices:
        try:
            if ((device / 'vendor').read_text().strip() == '0x10de'
                    and (device / 'class').read_text().strip().startswith('0x03')):
                return True
        except OSError:
            continue
    return False


def dictation_check(enabled, installed, events, server='', backend='auto', services=False, gpu=True):
    """The Dyktowanie row (docs/dictation.md): off, a speech server (the recording goes there, which it says; server is its
    address, core.speech_server), the speech tools missing, or what the engine's log says about the speech model (its
    last 'speech_model' event; there is no text in it). services: the row of the Usługi tab itself, where the choice
    of a server is right above it. gpu: nvidia_gpu(); without one the speech tools are not offered, a server is."""
    if not enabled:
        return Check(INFO, tr('Wyłączone — prawy Ctrl działa jak zwykle.'))
    if backend not in ('auto', 'local', 'server'):
        return Check(ERROR, tr('Wybierz rozpoznawanie mowy w zakładce Usługi.'))
    if backend == 'server' and not server.strip():
        return Check(ERROR, tr('Ustaw serwer mowy w zakładce Usługi.'))
    if backend != 'local' and server.strip():
        host = urllib.parse.urlsplit(server.strip()).netloc or server.strip()
        return Check(OK, tr('Rozpoznaje serwer mowy {host}.', host=host))
    if not installed and not gpu:
        return Check(WARNING, tr('Lokalny Whisper wymaga karty NVIDIA, a tu jej nie ma — wybierz „Serwer” powyżej.') if services
                     else tr('Lokalny Whisper wymaga karty NVIDIA, a tu jej nie ma — wybierz serwer mowy w zakładce Usługi.'))
    if not installed:
        return Check(WARNING, tr('Brak modelu mowy (ok. 4 GB) — zainstaluj go albo wybierz „Serwer” powyżej.') if services
                     else tr('Brak modelu mowy (ok. 4 GB) — zainstaluj go albo wybierz serwer mowy w zakładce Usługi.'),
                     'install_tools')
    model = next((e for e in reversed(events) if e.get('ev') == 'speech_model' and e.get('device') != 'server'), None)
    if model is None:
        return Check(OK, tr('Whisper na tym komputerze — wczyta się przy pierwszym dyktowaniu.'))
    if model.get('state') != 'ready':
        return Check(ERROR, tr('Model mowy się nie wczytał — szczegóły w logu silnika.'), 'log')
    where = {'cuda': tr('na karcie graficznej'), 'cpu': tr('na procesorze (wolniej)')}.get(model.get('device'), '')
    return Check(OK, (tr('Rozpoznaje Whisper na tym komputerze {where}.', where=where)).replace(' .', '.'))


FILE = 'file'  # read_settings: a problem of the whole file, not of one field
FIELD_NAMES = {'llm_location': 'miejsce modelu', 'llm_local_url': 'adres modelu na tym komputerze',
               'llm_url': 'adres serwera modelu', 'llm_api': 'protokół serwera modelu', 'model': 'model',
               'languagetool_location': 'miejsce LanguageTool', 'languagetool_local_url': 'adres LanguageTool na tym komputerze',
               'languagetool_url': 'adres LanguageTool', 'whisper_url': 'adres serwera mowy',
               'whisper_backend': 'miejsce rozpoznawania mowy', 'whisper_api': 'protokół serwera mowy',
               'auth_issuer': 'adres logowania',
               'correction_shortcut': 'skrót korekty', 'translation_shortcut': 'skrót tłumaczenia',
               'primary_language': 'język główny', 'secondary_language': 'drugi język'}
URL_FIELDS = ('llm_url', 'languagetool_url', 'whisper_url')  # empty is allowed: not set (the engine says what is missing)
LOCAL_URL_FIELDS = ('llm_local_url', 'languagetool_local_url')  # empty: the default address on this computer
# Known choices for the settings UI. Language/cleanup fall back to defaults; invalid speech backend/API
# selections are errors before recording. llm_think is also saved only when known (not in FIELD_NAMES).
DICTATION_CHOICES = {'dictation_language': ('pl', 'en', 'de', 'auto'),
                     'whisper_backend': ('auto', 'local', 'server'), 'whisper_api': ('openai', 'whispercpp'),
                     'primary_language': ('pl', 'en', 'de'), 'secondary_language': ('', 'pl', 'en', 'de'), 'dictation_cleanup': ('auto', 'always', 'off'),
                     'llm_api': core.LLM_APIS, 'llm_think': core.LLM_THINKS,
                     'llm_location': ('auto',) + core.LOCATIONS, 'languagetool_location': ('auto',) + core.LOCATIONS}
BOOL_FIELDS = ('dictation_enabled', 'live_check', 'live_model_only', 'live_languagetool', 'live_bielik', 'live_underlines')


def field_problem(field, value):
    """Why the engine would refuse this value of settings.json, or None."""
    if field in shortcuts.DEFAULTS:
        try:
            shortcuts.parse(value)
        except ValueError as exc:
            return str(exc)
        return None
    if field in BOOL_FIELDS:
        return None if isinstance(value, bool) else tr('zły typ wartości')
    if field in DICTATION_CHOICES:
        return None if value in DICTATION_CHOICES[field] else tr('nieznana wartość')
    if not isinstance(value, str):
        return tr('zły typ wartości')
    if field in URL_FIELDS:
        problem = core.url_problem(value) if value.strip() else None
        return problem.rstrip('.') if problem else None
    if field in LOCAL_URL_FIELDS:
        problem = core.local_url_problem(value)
        return problem.rstrip('.') if problem else None
    if field == 'auth_issuer':
        return oidc.issuer_problem(value)
    return None  # the model, the keys, the vocabulary: any text


def read_settings():
    """(settings, problems): settings.json as the engine reads it (core.Settings), and why the engine
    cannot use it: {} when it can; {field: reason} for the fields it refuses; {FILE: reason} when the
    file cannot be read at all. The settings hold every text value of the file (a refused one too,
    to show it where it can be corrected) over the defaults."""
    path = core.Settings.path()
    fields = [f.name for f in dataclasses.fields(core.Settings)]
    try:
        data = json.loads(path.read_bytes().decode('utf-8')) if path.exists() else {}
    except OSError as exc:
        return core.Settings(), {FILE: tr('nie można odczytać {path} ({value2})', path=path, value2=exc.strerror or exc)}
    except UnicodeError:
        return core.Settings(), {FILE: tr('{path} nie jest zapisany w UTF-8', path=path)}
    except ValueError:
        return core.Settings(), {FILE: tr('{path} nie jest poprawnym plikiem JSON', path=path)}
    if not isinstance(data, dict):
        return core.Settings(), {FILE: tr('{path} nie zawiera ustawień', path=path)}
    data = core.Settings.migrated(data)  # before 2026-10: ollama_url, Ollama's own API
    problems = {k: field_problem(k, data[k]) for k in fields if k in data}
    settings = core.Settings(**{k: v for k, v in data.items() if k in fields and _shown(k, v)})
    if not any(problems.get(field) for field in shortcuts.DEFAULTS):
        if shortcuts.parse(settings.correction_shortcut) == shortcuts.parse(settings.translation_shortcut):
            problems.update({field: tr(shortcuts.DUPLICATE) for field in shortcuts.DEFAULTS})
    if settings.primary_language == settings.secondary_language:
        problems['secondary_language'] = tr('drugi język musi być różny od głównego')
    return settings, {k: v for k, v in problems.items() if v}


def _shown(field, value):
    """A value read_settings keeps: any text (a refused address too, to be corrected where it is
    shown), and for dictation only a value of the right kind (the others show their default)."""
    if field in BOOL_FIELDS:
        return isinstance(value, bool)
    if field in DICTATION_CHOICES:
        return value in DICTATION_CHOICES[field]
    return isinstance(value, str)


def save_setting(field, value):
    """Save one validated setting; returns (error, backup)."""
    return save_settings({field: value})


def save_settings(values):
    """Save related values atomically, preserving unrelated valid settings and backing up a broken file."""
    settings, problems = read_settings()
    for bad in problems:
        if bad in FIELD_NAMES and bad not in values:
            setattr(settings, bad, getattr(core.Settings(), bad))
    for field, value in values.items():
        problem = field_problem(field, value)
        if problem:
            return problem[0].upper() + problem[1:] + '.', None
        if field in shortcuts.DEFAULTS:
            value = shortcuts.parse(value).label
        setattr(settings, field, value)
    if shortcuts.parse(settings.correction_shortcut) == shortcuts.parse(settings.translation_shortcut):
        return tr(shortcuts.DUPLICATE) + '.', None
    if settings.primary_language == settings.secondary_language:
        return tr('Drugi język musi być różny od głównego.'), None

    path, backup = core.Settings.path(), None
    try:
        if problems and path.exists():
            backup = path.with_name(path.name + '.bak')
            os.replace(path, backup)
        settings.save()
    except OSError as exc:
        if backup is not None and not path.exists():
            try:
                os.replace(backup, path)  # never leave no file: the engine would use the defaults silently
            except OSError:
                pass
            backup = None
        return tr('Nie udało się zapisać ustawień: {value}', value=exc.strerror or exc), backup
    return None, backup


def settings_check(problems, field=FILE):
    """The row of a setting the engine refuses (FILE: the whole file): it corrects nothing then."""
    if field == FILE:
        return Check(ERROR, tr('Plik ustawień jest nieprawidłowy: {value}. Silnik nie poprawi tekstu, dopóki go nie naprawisz — wpisz adres poniżej i zatwierdź.', value=problems[FILE]))
    how = {'model': tr('wybierz model z listy'), 'llm_api': tr('wybierz protokół z listy'),
           'primary_language': tr('wybierz język w zakładce Pisanie'),
           'secondary_language': tr('wybierz język w zakładce Pisanie'),
           'correction_shortcut': tr('wpisz poprawny skrót w zakładce Start'),
           'translation_shortcut': tr('wpisz poprawny skrót w zakładce Start'),
           'llm_location': tr('wybierz, gdzie działa model'), 'languagetool_location': tr('wybierz, gdzie działa LanguageTool'),
           'whisper_backend': tr('wybierz, gdzie działa rozpoznawanie mowy'), 'whisper_api': tr('wybierz protokół z listy')}.get(
               field, tr('wpisz adres poniżej i zatwierdź'))
    return Check(ERROR, tr('Nieprawidłowy {value} w ustawieniach: {value2}. Silnik nie poprawi tekstu, dopóki go nie poprawisz — {how}.', value=tr(FIELD_NAMES[field]), value2=problems[field], how=how))


# ---- the shortcut --------------------------------------------------------------------------------
MODIFIERS = {'control': 'ctrl', 'ctrl': 'ctrl', 'primary': 'ctrl', 'alt': 'alt', 'mod1': 'alt',
             'shift': 'shift', 'super': 'super', 'mod4': 'super', 'meta': 'meta', 'hyper': 'hyper'}


def parse_accel(accel):
    """'<Control><Alt>g' -> (frozenset({'ctrl', 'alt'}), 'g'); None when it is not an accelerator.
    The key is compared in lower case: GNOME may bind 'G' and 'g' to the same key."""
    rest, mods = accel.strip(), set()
    while rest.startswith('<'):
        end = rest.find('>')
        if end < 0:
            return None
        name = rest[1:end].strip().lower()
        mods.add(MODIFIERS.get(name, name))
        rest = rest[end + 1:].lstrip()
    rest = rest.strip()
    return (frozenset(mods), rest.lower()) if rest else None


def hotkey_conflicts(bindings, accelerator='<Control><Alt>g'):
    """Labels of GNOME bindings that intercept the selected shortcut."""
    wanted = parse_accel(accelerator)
    return [label for label, accel in bindings if parse_accel(accel) == wanted]


def hotkey_check(conflicts, verified=None):
    """conflicts: from hotkey_conflicts; verified: True when Ctrl+Alt+G in the app's test field
    reached the engine, False when it reached the app instead, None when not tried yet."""
    if conflicts:
        return Check(ERROR, tr('Zajęty przez skrót GNOME {value} — zmień go niżej albo w ustawieniach GNOME.', value=', '.join('„%s”' % c for c in conflicts)), 'keyboard')
    if verified is False:
        return Check(ERROR, tr('Nie dotarł do GramaDramy (sprawdzone w polu Wypróbuj).'))
    if verified:
        return Check(OK, tr('Działa — sprawdzone w polu Wypróbuj'))
    return Check(OK, tr('Wolny — żaden skrót GNOME go nie przechwytuje'))


def not_reached(source, conflicts):
    """(text, cause_known) for the Wypróbuj field when Ctrl+Alt+G reached the app instead of the
    engine. cause_known: a GNOME shortcut or another input source explains it, so fixing that is
    enough; otherwise the shortcut stays failed until the engine really gets it."""
    if conflicts:
        return (tr('Skrót nie dotarł do GramaDramy: przechwytuje go skrót GNOME {value}.', value=', '.join('„%s”' % c for c in conflicts))), True
    if source is not None and source.state != OK:
        return tr('Skrót nie dotarł do GramaDramy. ') + source.text, True
    return (tr('Skrót nie dotarł do GramaDramy, choć źródło GramaDrama jest aktywne i żaden skrót GNOME go nie zajmuje. Szczegóły mogą być w logu silnika (menu → Otwórz log silnika).')), False


# ---- input source, engine, panel --------------------------------------------------------------------
def source_check(sources, current):
    """sources: GNOME's input sources [(type, id)]; current: `ibus engine` (None: unknown)."""
    sources = [tuple(s) for s in sources]
    if ENGINE_SOURCE not in sources:
        return Check(ERROR, tr('Brak źródła GramaDrama wśród źródeł wprowadzania. Uruchom instalator.'),
                     'install')
    if current == ENGINE_NAME:
        return Check(OK, tr('GramaDrama — aktywne'))
    if current is None:
        return Check(WARNING, tr('Nie udało się odczytać aktywnego źródła (ibus engine).'))
    if all(source == ENGINE_SOURCE for source in sources):
        # There is no other configured source to switch from. GNOME may use plain XKB
        # temporarily in a password field, or when the selected engine cannot start.
        return Check(WARNING, tr('Źródło GramaDrama jest ustawione poprawnie, ale nie jest aktywne w IBus. Kliknij zwykłe pole tekstowe — w polu hasła to normalne. Jeśli problem pozostaje, silnik mógł się nie uruchomić. Uruchom wtedy instalator.'))
    return Check(WARNING, tr('Aktywne jest inne źródło. Wybierz źródło GramaDrama na górnym pasku albo naciśnij Super+Spacja.'), 'keyboard')


def when(t, now=None):
    """'dziś 14:01', 'wczoraj 18:22' or '27.09 14:01' in local time."""
    moment = datetime.datetime.fromtimestamp(t)
    today = datetime.datetime.fromtimestamp(time.time() if now is None else now).date()
    if moment.date() == today:
        return tr('dziś ') + moment.strftime('%H:%M')
    if moment.date() == today - datetime.timedelta(days=1):
        return tr('wczoraj ') + moment.strftime('%H:%M')
    return moment.strftime('%d.%m %H:%M')


def version_text(info, now=None):
    """' · wersja 0.5.2 (2064c42), zainstalowana dziś 14:10' from install.json (version_label); '' when unknown."""
    label = version_label(info)
    if not label:
        return ''
    text = tr(' · wersja {version}', version=label)
    try:
        text += tr(', zainstalowana ') + when(datetime.datetime.fromisoformat(info['installed_at']).timestamp(), now)
    except (KeyError, TypeError, ValueError):
        pass
    return text


def engine_check(status, registered, installed, component, info, origin, differs, stale=False):
    """status: the engine's Status() or None when it is not running; registered: IBus lists the
    engine (None: the ibus CLI did not answer); installed: the copy in ~/.local/share/gramadrama-engine
    exists; component: 'current', 'outdated', 'missing' or 'unknown' (the description IBus reads);
    info: install_info(); origin: 'installed', 'repository' or None (where the running engine was
    started from); differs: the installed copy differs from its repository; stale: the running
    engine started before the installer last changed its code."""
    if status is not None:
        if status.get('test'):
            return Check(WARNING, tr('Działa w trybie testowym (GRAMADRAMA_ENGINE_TEST=1) — {value}.', value=tr('tylko z serwerem na 127.0.0.1') if status.get('backend') == 'real' else tr('poprawki nie trafiają do modelu')))
        if stale:
            # Switching the input source only recreates engine objects in the same process.
            return Check(WARNING, tr('Działa starsza wersja niż zainstalowana. Uruchom instalator ponownie (zrestartuje silnik) albo wyloguj się i zaloguj.'), 'install')
        if origin == 'repository':
            return Check(OK, tr('Działa z repozytorium (bez instalacji)'))
        text = tr('Działa') + version_text(info)
    else:
        if registered is None:
            return Check(WARNING, tr('Nie udało się zapytać IBus (ibus-daemon nie odpowiada?).'))
        if not installed:
            return Check(ERROR, tr('Nie jest zainstalowany. Uruchom instalator.'), 'install')
        if registered is False:
            if component == 'missing':
                return Check(ERROR, tr('Brak opisu silnika dla IBus. Uruchom instalator (jedno polecenie sudo).'),
                             'install')
            return Check(ERROR, tr('IBus nie wczytał silnika. Uruchom instalator ponownie.'), 'install')
        text = tr('Gotowy — IBus uruchomi go przy pierwszym użyciu') + version_text(info)
    if installed and component == 'outdated':
        return Check(WARNING, tr('Opis silnika dla IBus jest nieaktualny. Uruchom instalator (jedno polecenie sudo).'),
                     'install')
    if differs:
        return Check(INFO, text + tr('. Repozytorium ma nowszą wersję — uruchom instalator.'), 'install')
    return Check(OK, text)


EXT_ACTIVE, EXT_INACTIVE, EXT_ERROR, EXT_OUT_OF_DATE, EXT_INITIALIZED = 1, 2, 3, 4, 6


def panel_check(info, loaded, on_disk, changed_at, shell_started, user_disabled=False):
    """info: GetExtensionInfo of the panel ({} when GNOME does not know it); loaded: the version
    its Ping reports (None: no answer); on_disk: the version in the installed metadata.json (None:
    not installed); changed_at: when the installer last changed the panel's files; shell_started:
    when GNOME Shell started (it looks for extensions and loads their code only then);
    user_disabled: org.gnome.shell disable-user-extensions."""
    if not info:
        if on_disk is not None:  # copied during this session: GNOME has not seen it yet
            return Check(WARNING, tr('Zainstalowany — wczyta się po wylogowaniu i ponownym zalogowaniu.'))
        return Check(WARNING, tr('Nie jest zainstalowany — wyniki pokaże zwykłe okienko IBus. Uruchom instalator.'),
                     'install')
    if user_disabled:
        return Check(WARNING, tr('Dodatki użytkownika są wyłączone w GNOME — włącz je w aplikacji Rozszerzenia.'))
    state = int(info.get('state') or 0)
    if state == EXT_ERROR:
        return Check(ERROR, tr('Błąd dodatku: {value}', value=info.get('error') or tr('nieznany')))
    if state == EXT_OUT_OF_DATE:
        return Check(ERROR, tr('Dodatek nie pasuje do tej wersji GNOME Shell.'))
    if not info.get('enabled', state == EXT_ACTIVE):
        return Check(WARNING, tr('Wyłączony — wyniki pokaże zwykłe okienko IBus.'), 'enable_panel')
    if state == EXT_INACTIVE:  # enabled, but switched off for now: GNOME does that on the lock screen
        return Check(WARNING, tr('Nieaktywny — GNOME go wyłączył, np. na czas blokady ekranu. Jeśli to nie minie, wyloguj się i zaloguj ponownie.'))
    if state != EXT_ACTIVE:  # enabled, but its code is not running yet (installed during this session)
        return Check(WARNING, tr('Wczyta się po wylogowaniu i ponownym zalogowaniu.'))
    if loaded is None:
        return Check(WARNING, tr('Włączony, ale nie odpowiada. Wyloguj się i zaloguj ponownie.'))
    newer = on_disk is not None and str(on_disk) != str(loaded)
    if newer or (changed_at and shell_started and changed_at > shell_started):
        return Check(WARNING, tr('Nowa wersja wczyta się po wylogowaniu i ponownym zalogowaniu (teraz działa wersja {loaded}).', loaded=loaded))
    return Check(OK, tr('Działa · wersja {loaded}', loaded=loaded))


# ---- the engine log ------------------------------------------------------------------------------
def parse_log(data):
    """The events the app reads from JSONL bytes; other and broken lines are skipped."""
    events = []
    for line in data.split(b'\n'):
        if not any(marker in line for marker in LOG_MARKERS):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('ev') in LOG_EVENTS:
            events.append(event)
    return events


def _outcome(events):
    """(state, text) for how a session ended, from its events after 'start'."""
    outcome = (INFO, tr('w toku'))
    for event in events:
        ev = event['ev']
        if ev == 'result':
            outcome = (OK, tr('bez zmian') if event.get('unchanged') else tr('wynik gotowy'))
        elif ev == 'backend_error':
            outcome = (WARNING, tr('błąd korekty'))
        elif ev == 'apply':
            outcome = (INFO, tr('wstawianie'))
        elif ev == 'verified':
            outcome = {'ok': (OK, tr('zastosowano')), 'mismatch': (WARNING, tr('nie udało się potwierdzić zmiany'))}.get(
                event.get('outcome'), (OK, tr('zastosowano (bez potwierdzenia)')))
        elif ev == 'refused':
            outcome = (INFO, tr('nie zastosowano: {value}', value=fallback_ui.message(event.get('reason'))))
        elif ev == 'end':
            why = event.get('why')
            if why == 'closed' and outcome[0] == WARNING:
                continue  # closed after an error: the error is the story
            if why == 'hotkey':
                outcome = (INFO, tr('ponowiono skrót'))
            else:
                state, _text, short = ENDS.get(why, END_DEFAULT)
                outcome = (state if state != ERROR else WARNING, tr(short))
    return outcome


def last_use(events, now=None, names=None):
    """Check for the last Ctrl+Alt+G in the log: when, in which app (names: {app ID: name}, the
    apps the app knows by name), the mode and scope, how it ended, and whether the engine hit
    internal errors during the last day. A refusal is information (the engine works as designed),
    not a problem."""
    now = time.time() if now is None else now
    errors = sum(1 for e in events if e['ev'] == 'error' and now - e.get('t', 0) < ERROR_WINDOW)
    last = next((i for i in range(len(events) - 1, -1, -1)
                 if events[i]['ev'] == 'start' or events[i]['ev'] == 'refused' and events[i].get('at') == 'hotkey'),
                None)
    if last is None:
        check = Check(INFO, tr('Jeszcze nie użyto — spróbuj w polu Wypróbuj.'))
    else:
        event = events[last]
        app = event.get('app')
        head = [when(event.get('t', now), now)]
        if isinstance(app, str) and (names or {}).get(app):
            head.append(names[app])
        if event['ev'] == 'refused':
            check = Check(INFO, tr('{value} · odmowa: {value2}', value=' · '.join(head), value2=fallback_ui.message(event.get('reason'))))
        else:
            sid = event.get('id')
            rest = [e for e in events[last + 1:] if e['ev'] != 'refused' or e.get('at') == 'apply']
            rest = [e for e in rest if e.get('id', sid) == sid]
            state, outcome = _outcome(rest)
            parts = head + [tr(MODE_NAMES.get(event.get('mode'), event.get('mode') or '?'))]
            if event.get('scope') in SCOPES:
                parts.append(tr(SCOPES[event['scope']]))
            check = Check(state, ' · '.join(parts + [outcome]))
    if errors:
        text = tr('{text} · {errors:d} {value3} silnika w ciągu doby (szczegóły w logu)', text=check.text, errors=errors, value3=plural(errors, tr('błąd'), tr('błędy'), tr('błędów')))
        return Check(WARNING, text, 'log')
    return check


def plural(n, one, few, many):
    if n == 1:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def try_event(event):
    """(state, text) for the Wypróbuj field after an engine event from that field; None when the
    event changes nothing there."""
    ev = event.get('ev')
    if ev == 'refused':
        reason = event.get('reason')
        if event.get('at') == 'hotkey' and reason == 'stale':
            return WARNING, (tr('Skrót działa, ale GramaDrama nie zna jeszcze pozycji kursora (tak jest po kliknięciu). Przesuń kursor strzałką (na końcu tekstu: w lewo) i ponów korektę.'))
        return (WARNING if reason in EXPECTED_REFUSALS else ERROR), fallback_ui.message(reason)
    if ev == 'start':
        what = tr('tłumaczy') if event.get('mode') == 'translate' else tr('poprawia tekst')
        return BUSY, tr('Skrót działa. Model {what}…', what=what)
    if ev == 'result':
        if event.get('unchanged'):
            return OK, tr('Zero dramatu — tekst jest poprawny. Enter zamknie panel.')
        return OK, tr('Gotowe. Enter wstawi poprawkę, Esc anuluje.')
    if ev == 'backend_error':
        return ERROR, tr('Korekta się nie udała — powód jest w panelu. Sprawdź też zakładkę Usługi.')
    if ev == 'apply':
        return BUSY, tr('Wstawiam poprawkę…')
    if ev == 'verified':
        if event.get('outcome') == 'mismatch':
            return WARNING, fallback_ui.message('verify_mismatch')
        if event.get('outcome') == 'ok':
            return OK, tr('Zastosowano. Skrót, silnik i serwer działają.')
        return OK, tr('Zastosowano (bez potwierdzenia od aplikacji).')
    if ev == 'end':
        if event.get('why') == 'hotkey':
            return None  # a new session follows
        state, text, _short = ENDS.get(event.get('why'), END_DEFAULT)
        return state, tr(text)
    return None


# ---- the server ----------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Reply:
    ok: bool
    ms: int = None
    data: object = None
    error: str = ''


def network_error(exc):
    """A short reason for a failed request in the current interface language."""
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP %d' % exc.code
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return tr('nie odpowiada')
    if isinstance(reason, ConnectionRefusedError):
        return tr('odmowa połączenia — czy usługa działa?')
    if isinstance(reason, socket.gaierror):
        return tr('nieznany adres')
    if isinstance(reason, OSError) and reason.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
        return tr('serwer poza zasięgiem sieci')
    if isinstance(exc, ValueError):
        return tr('nieoczekiwana odpowiedź')
    return tr('brak połączenia')


def fetch(opener, url, timeout=3.0, key='', settings=None):
    """GET url as JSON. A server on the login's origin (oidc.py, with the settings given) is asked with the login's
    token, and a 401 to it once more with a refreshed one."""
    start = time.monotonic()
    rejected = ''
    for attempt in (0, 1):
        try:
            authorization = oidc.authorization(settings, url, key, rejected) if settings is not None else (
                'Bearer ' + key if key else '')
        except oidc.LoggedOut:
            return Reply(False, error='nie zalogowano')
        except oidc.AuthUnreachable:
            return Reply(False, error=tr('serwer logowania nie odpowiada'))
        except oidc.AuthError:
            return Reply(False, error=tr('logowanie nie działa'))
        headers = {'User-Agent': oidc.USER_AGENT}
        if authorization:
            headers['Authorization'] = authorization
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401 and authorization and not key and attempt == 0:
                rejected = authorization.removeprefix('Bearer ')
                continue
            if exc.code in (401, 403) and authorization and not key:
                return Reply(False, error=tr('serwer odrzucił logowanie (HTTP {code:d})', code=exc.code))
            return Reply(False, error=network_error(exc))
        except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
            return Reply(False, error=network_error(exc))
        return Reply(True, max(1, round((time.monotonic() - start) * 1000)), data)


def full_name(model):
    """Ollama adds ':latest' to a name without a tag."""
    return model if ':' in model.rsplit('/', 1)[-1] else model + ':latest'


def model_index(names, current):
    """Where the saved model is in the model list, matching 'bielik' with 'bielik:latest'; -1 if absent."""
    wanted = full_name(current)
    return next((i for i, name in enumerate(names) if full_name(name) == wanted), -1)


# The services of the Usługi tab (Services.swift and Backend.diagnostics on the Mac): each on its route (core.route), with the
# Mac's texts. A route through the gateway is said after the state; a server outside the LAN without https is a warning.
SERVICE_NAMES = {'llm': 'model', 'lt': 'LanguageTool', 'speech': 'mowa'}
OLLAMA_DOWNLOAD = 'https://ollama.com/download'


def ollama_installed():
    """Ollama on this computer: its program on PATH or its systemd unit (the official install script adds both)."""
    import shutil
    units = [Path(d, 'ollama.service') for d in ('/etc/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system')]
    return bool(shutil.which('ollama')) or any(unit.exists() for unit in units + [config_home() / 'systemd' / 'user' / 'ollama.service'])


class PullProgress:
    """The progress of a model pulled into Ollama (OllamaPullProgress on the Mac): the sizes of its layers summed;
    the status "success" ends it."""

    def __init__(self):
        self.status, self.finished, self.layers = '', False, {}

    @property
    def total(self):
        return sum(total for total, _ in self.layers.values())

    @property
    def completed(self):
        return sum(min(done, total) for total, done in self.layers.values())

    def apply(self, line):
        """One line of the answer; Ollama's error ({"error": …}) or None."""
        try:
            data = json.loads(line)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        if isinstance(data.get('error'), str):
            return data['error']
        if isinstance(data.get('status'), str):
            self.status = data['status']
            self.finished = self.finished or self.status == 'success'
        digest, total = data.get('digest'), data.get('total')
        if isinstance(digest, str) and isinstance(total, int) and total > 0:
            done = data.get('completed')
            self.layers[digest] = (total, done if isinstance(done, int) else self.layers.get(digest, (0, 0))[1])
        return None


def ollama_pull(base, name, progress, cancel, timeout=120):
    """POST {base}/api/pull as a stream (LocalOllama.pull on the Mac): progress(completed, total) as it goes, at most five
    times a second. RuntimeError with Ollama's own message, or when the answer ends before "success"; nothing when
    cancel (a threading.Event) is set. Blocking, for a worker thread."""
    request = urllib.request.Request(base + '/api/pull', data=json.dumps({'model': name, 'stream': True}).encode(),
                                     headers={'Content-Type': 'application/json', 'User-Agent': oidc.USER_AGENT})
    state, shown = PullProgress(), 0.0
    try:
        with core.opener_for(base).open(request, timeout=timeout) as response:
            for line in response:
                if cancel.is_set():
                    return
                error = state.apply(line)
                if error:
                    raise RuntimeError(error)
                if time.monotonic() - shown > 0.2 or state.finished:
                    shown = time.monotonic()
                    progress(state.completed, state.total)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError('HTTP %d' % exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(tr('Ollama nie odpowiada ({error}).', error=tr(network_error(exc)))) from None
    if not state.finished and not cancel.is_set():
        raise RuntimeError(tr('Pobieranie przerwane — spróbuj jeszcze raz.'))


def plain_text(url):
    """True for http:// to a server outside this computer's network: the text would travel unencrypted."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == 'http' and not core.lan_host(parts.hostname)


def _answers_with(url, key, settings, field):
    """The protocol hint: a quick GET answered with a JSON object holding field (Ollama: version, OpenAI: data)."""
    reply = fetch(core.opener_for(url), url, timeout=1.5, key=key, settings=settings)
    return reply.ok and isinstance(reply.data, dict) and field in reply.data


def probe_server(settings, llm=True, languagetool=True, whisper=False):
    """The model server, LanguageTool and the speech server, each asked on its route (core.route: direct, at home, or
    through the gateway away from home), at the same time; blocking, for a worker thread. Each service asked has its
    Check in the result (None: not asked), result['routes'] the route of each, and for the model its lists: 'models'
    (None: not said) and, for Ollama, 'loaded'; 'llm_ok' when it answered."""
    import concurrent.futures
    result = {'llm': None, 'llm_ok': False, 'models': None, 'loaded': None, 'lt': None, 'whisper': None, 'routes': {}}
    jobs = [job for job, wanted in ((_probe_model, llm), (_probe_languagetool, languagetool),
                                    (_probe_speech, whisper and settings.whisper_uses_server)) if wanted]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for future in [pool.submit(job, settings) for job in jobs]:
            update, (service, kind) = future.result()
            result.update(update)
            result['routes'][service] = kind
    return result


def _probe_model(settings):
    kind, base = core.route(settings, 'llm')
    url, api, key = core.model_server(settings)
    key = '' if kind == 'remote' else key
    local = core.located(settings, 'llm') == 'local'
    out = {'llm_ok': False}
    if kind in ('none', 'offline'):
        out['llm'] = llm_check(Reply(False, error='nie ustawiono' if kind == 'none' else 'offline'), api, local=local)
        return out, ('llm', kind)
    hint, installed, ollama_here = None, None, False
    if api == 'ollama':
        reply = fetch(core.opener_for(base), base + '/api/version', key=key, settings=settings)
        if reply.ok and not (isinstance(reply.data, dict) and 'version' in reply.data):
            reply = Reply(False, error=tr('to nie wygląda na Ollamę'))
        if reply.ok:
            out['models'] = _names(fetch(core.opener_for(base), base + '/api/tags', key=key, settings=settings))
            out['loaded'] = _names(fetch(core.opener_for(base), base + '/api/ps', key=key, settings=settings))
        elif reply.error in ('HTTP 404', 'HTTP 405') and kind not in ('remote', 'gateway') \
                and _answers_with(core.base_url(url) + '/models', key, settings, 'data'):
            hint = 'openai'
        elif local:
            installed = ollama_installed()
    else:
        reply = fetch(core.opener_for(base), base + '/models', key=key, settings=settings)
        if reply.ok and not (isinstance(reply.data, dict) and isinstance(reply.data.get('data'), list)):
            reply = Reply(False, error=tr('to nie wygląda na serwer zgodny z OpenAI'))
        parts = urllib.parse.urlsplit(base)
        origin = '%s://%s' % (parts.scheme, parts.netloc)
        if reply.ok:
            out['models'] = _names(reply)
            # An Ollama answers the OpenAI way too, but without num_ctx: it loads the model anew (16 s on a real one).
            ollama_here = _answers_with(origin + '/api/version', key, settings, 'version')
        elif reply.error in ('HTTP 404', 'HTTP 405') and _answers_with(origin + '/api/version', key, settings, 'version'):
            hint = 'ollama'
    out['llm_ok'] = reply.ok
    out['llm'] = llm_check(reply, api, ollama_here, kind=kind, local=local, installed=installed, hint=hint,
                           plain=plain_text(base) and kind != 'remote')
    return out, ('llm', kind)


def _probe_languagetool(settings):
    kind, base = core.route(settings, 'lt')
    if kind in ('none', 'offline'):
        return {'lt': languagetool_unset() if kind == 'none' else languagetool_check(Reply(False, error='offline'))}, ('lt', kind)
    reply = fetch(core.opener_for(base), base + '/v2/languages', settings=settings)
    if reply.ok and not isinstance(reply.data, list):
        reply = Reply(False, error=tr('to nie wygląda na LanguageTool'))
    return {'lt': languagetool_check(reply, kind=kind, local=core.located(settings, 'lt') == 'local',
                                     plain=plain_text(base) and kind != 'remote')}, ('lt', kind)


def _probe_speech(settings):
    """As SpeechServerClient.check on the Mac: a wrong protocol is suggested instead of "works"."""
    kind, base = core.route(settings, 'speech')
    native = settings.whisper_api == 'whispercpp'
    if kind in ('none', 'offline'):
        return {'whisper': whisper_unset('server') if kind == 'none' else whisper_check(Reply(False, error='offline'))}, ('speech', kind)
    key = '' if kind == 'remote' else settings.whisper_key
    reply = fetch(core.opener_for(base, redirects=False), base + ('/health' if native else '/models'), timeout=5, key=key,
                  settings=settings)
    hint = None
    url = core.speech_server(settings)
    if kind not in ('remote', 'gateway') and (
            (not reply.ok and reply.error in ('HTTP 404', 'HTTP 405'))
            or (reply.ok and not native and not (isinstance(reply.data, dict) and isinstance(reply.data.get('data'), list)))):
        other = core.base_url(url, openai=native) + ('/models' if native else '/health')
        found = fetch(core.opener_for(other, redirects=False), other, timeout=1.5, key=key, settings=settings)
        if found.ok and isinstance(found.data, dict) and (
                isinstance(found.data.get('data'), list) if native else found.data.get('status') == 'ok'):
            hint = 'openai' if native else 'whispercpp'
    return {'whisper': whisper_check(reply, settings.whisper_api, kind=kind, hint=hint, model=settings.whisper_model,
                                     plain=plain_text(base) and kind != 'remote')}, ('speech', kind)


def _names(reply):
    """Model names of a /api/tags ({"models": [{"name"}]}) or a /models ({"data": [{"id"}]}) reply, or None."""
    if not reply.ok or not isinstance(reply.data, dict):
        return None
    listed = reply.data.get('models') if isinstance(reply.data.get('models'), list) else reply.data.get('data')
    return sorted(m.get('name') or m.get('id') for m in listed or [] if isinstance(m, dict) and (m.get('name') or m.get('id')))


# The two sentinels of a Reply's error that need the login: 'offline' (core.route: the server does not answer in this
# network, and the gateway needs the login) and 'nie zalogowano' (fetch: the address is the gateway's own).
LOGIN_NEEDED = 'Serwer nie odpowiada w tej sieci — zaloguj się w dostępie zdalnym, żeby łączyć się przez bramę.'
LOGIN_REQUIRED = 'Wymaga logowania — zaloguj się w dostępie zdalnym.'
PLAIN = ' · bez szyfrowania poza siecią lokalną — użyj https://'


def login_problem(reply):
    """The Check of a reply that needs the login, or None."""
    if reply.error == 'offline':
        return Check(WARNING, tr(LOGIN_NEEDED), 'login')
    if reply.error == 'nie zalogowano':
        return Check(WARNING, tr(LOGIN_REQUIRED), 'login')
    return None


def _via(kind):
    return tr(' · przez bramę') if kind == 'remote' else ''


def _plain(check, plain):
    """A working server outside the LAN over plain http: a warning with the same text and the advice."""
    return Check(WARNING, check.text + tr(PLAIN), check.action) if plain and check.state == OK else check


def llm_check(reply, api='openai', ollama_here=False, kind='direct', local=False, installed=None, hint=None, plain=False):
    """The model server's row. local: Ollama on this computer (installed: whether its program is there, asked when it does
    not answer); hint: the protocol that answers at this address instead ('openai' or 'ollama'); ollama_here: an Ollama
    answers at this address although the OpenAI way is set."""
    if reply is None:
        return Check(BUSY, tr('Sprawdzam…'))
    if not reply.ok:
        if login_problem(reply):
            return login_problem(reply)
        if reply.error == 'nie ustawiono':
            return llm_unset()
        if hint == 'openai':
            return Check(WARNING, tr('To serwer zgodny z OpenAI — wybierz ten protokół.'))
        if hint == 'ollama':
            return Check(WARNING, tr('To Ollama — wybierz protokół „Ollama”.'))
        if local and installed is False:
            return Check(ERROR, tr('Ollama nie jest zainstalowana — to bezpłatny program do modeli językowych.'), 'ollama_download')
        if local and installed:
            return Check(ERROR, tr('Ollama jest zainstalowana, ale nie działa — uruchom jej usługę (systemctl start ollama).'))
        if local:
            return Check(ERROR, tr('Ollama na tym komputerze nie odpowiada ({error}).', error=tr(reply.error)))
        return Check(ERROR, tr('Niedostępny ({error})', error=tr(reply.error)))
    if api == 'ollama':
        return _plain(Check(OK, tr('Działa · {ms:d} ms · Ollama {value2}', ms=reply.ms, value2=reply.data.get('version', '?')) + _via(kind)), plain)
    if ollama_here:
        return Check(WARNING, tr('Działa, ale to Ollama — wybierz protokół „Ollama”, inaczej model wczytuje się od nowa.'))
    return _plain(Check(OK, tr('Działa · {ms:d} ms · API zgodne z OpenAI', ms=reply.ms) + _via(kind)), plain)


def llm_unset():
    return Check(WARNING, tr('Podaj adres serwera modelu albo wybierz „Ten komputer”.'))


def languagetool_check(reply, kind='direct', local=False, plain=False):
    """LanguageTool is optional: on this computer a server that is not running is only said (no warning, as on the Mac,
    where it is not among the problems); a server of the settings that does not answer is a warning."""
    if reply is None:
        return Check(BUSY, tr('Sprawdzam…'))
    if not reply.ok:
        if login_problem(reply):
            return login_problem(reply)
        if local:
            return Check(INFO, tr('Nie działa na tym komputerze — korekta działa bez uzasadnień zmian.'))
        return Check(WARNING, tr('Niedostępny ({error}) — korekta działa bez uzasadnień zmian.', error=tr(reply.error)))
    return _plain(Check(OK, tr('Działa · {ms:d} ms', ms=reply.ms) + _via(kind)), plain)


def languagetool_unset():
    return Check(INFO, tr('Nie ustawiono — LanguageTool jest opcjonalny.'))


def whisper_check(reply, api='openai', kind='direct', hint=None, model='', plain=False):
    """The speech server's row (SpeechServerClient.check on the Mac). hint: the protocol that answers at this address
    instead. An OpenAI-style server need not list its models (404/405 is then only said)."""
    if reply is None:
        return Check(BUSY, tr('Sprawdzam…'))
    if hint == 'openai':
        return Check(WARNING, tr('To serwer zgodny z OpenAI — wybierz ten protokół.'))
    if hint == 'whispercpp':
        return Check(WARNING, tr('To serwer whisper.cpp — wybierz protokół „whisper.cpp”.'))
    if not reply.ok:
        if login_problem(reply):
            return login_problem(reply)
        if reply.error == 'HTTP 503' and api == 'whispercpp':
            return Check(WARNING, tr('Serwer whisper.cpp wczytuje model — spróbuj za chwilę.'))
        if api == 'openai' and reply.error in ('HTTP 404', 'HTTP 405'):
            return Check(INFO, tr('Odpowiada, ale nie podaje listy modeli — sprawdź go krótkim dyktowaniem.'))
        if reply.error.startswith('HTTP 3'):
            return Check(ERROR, tr('Serwer mowy przekierowuje ({error}) — wpisz adres docelowy.', error=reply.error))
        return Check(ERROR, tr('Niedostępny ({error})', error=tr(reply.error)))
    data = reply.data if isinstance(reply.data, dict) else {}
    if api == 'whispercpp':
        if data.get('status') != 'ok':
            return Check(ERROR, tr('Odpowiada, ale to nie wygląda na gotowy serwer whisper.cpp.'))
        return _plain(Check(OK, tr('Działa · {ms:d} ms · whisper.cpp', ms=reply.ms) + _via(kind)), plain)
    listed = data.get('data')
    if not isinstance(listed, list):
        return Check(WARNING, tr('Odpowiada ({ms:d} ms), ale nie jak API zgodne z OpenAI — sprawdź adres krótkim dyktowaniem.', ms=reply.ms))
    wanted = model.strip() or 'whisper-1'
    ids = [item.get('id') for item in listed if isinstance(item, dict)]
    if ids and wanted not in ids:
        return Check(WARNING, tr('Działa ({ms:d} ms), ale serwer nie wymienia modelu „{value}” — sprawdź nazwę.', ms=reply.ms, value=wanted))
    return _plain(Check(OK, tr('Działa · {ms:d} ms · API zgodne z OpenAI', ms=reply.ms) + _via(kind)), plain)


def whisper_unset(backend='auto'):
    if backend == 'server':
        return Check(ERROR, tr('Podaj adres serwera mowy.'))
    return Check(INFO, tr('Whisper na tym komputerze; nagranie nie opuszcza komputera.'))


def login_check(settings, status, name='', pending=False, relevant=None):
    """The account row of the remote access (oidc.py; RemoteAccount on the Mac): status is oidc.status(settings), name the
    account, pending a login waiting for the browser, relevant why a service needs it now: 'offline' (a server does not
    answer in this network) or 'required' (the gateway wants it)."""
    if status == 'in':
        return Check(OK, tr('Zalogowano{value}.', value=tr(' jako ') + name if name else ''), 'logout')
    if pending:
        return Check(BUSY, tr('Czekam na przeglądarkę — dokończ w niej logowanie. Jeśli aplikacja się nie otworzy, wklej niżej adres, na który wróciła.'), 'login')
    if relevant == 'offline':
        return Check(WARNING, tr('Nie zalogowano — serwer nie odpowiada w tej sieci.'), 'login')
    if relevant:
        return Check(WARNING, tr('Nie zalogowano — serwer wymaga logowania.'), 'login')
    return Check(INFO, tr('Nie zalogowano.'), 'login')


def route_check(settings, routes):
    """The row "Połączenie" of the remote access (connectionRow on the Mac): how the services that can use the gateway
    connect now. routes: core.route's kind of each service asked."""
    eligible = [(service, kind) for service, kind in routes.items() if kind in ('home', 'remote', 'gateway', 'offline')]
    if not eligible:
        return Check(INFO, tr('Nie dotyczy — żadna z wybranych usług nie łączy się przez bramę.'))
    kinds = {kind for _, kind in eligible}
    if 'offline' in kinds:
        return Check(WARNING, tr('Serwer nie odpowiada w tej sieci — zaloguj się, żeby łączyć się przez bramę.'), 'login')
    if kinds == {'home'}:
        hosts = list(dict.fromkeys(urllib.parse.urlsplit(core.service_base(settings, service)).netloc for service, _ in eligible))
        return Check(OK, tr('Teraz bezpośrednio: {value}.', value=', '.join(hosts)))
    if 'home' not in kinds:
        return Check(OK, tr('Teraz przez bramę: {value}.', value=urllib.parse.urlsplit(core.gateway(settings)).netloc))
    names = {kind: ', '.join(tr(SERVICE_NAMES[service]) for service, k in eligible if (k == 'home') == (kind == 'home'))
             for kind in ('home', 'remote')}
    return Check(OK, tr('Przez bramę: {value}; bezpośrednio: {value2}.', value=names['remote'], value2=names['home']))


# The spelling dictionaries of the text languages (Hunspell, through Enchant) and their Ubuntu packages; a fresh English
# system has the English one only.
DICTIONARIES = {'pl': ('polski', 'hunspell-pl'), 'en': ('angielski', 'hunspell-en-us'), 'de': ('niemiecki', 'hunspell-de-de')}


def missing_dictionaries(languages, available=None):
    """The text languages (codes) whose spelling dictionary Enchant does not find: checking while typing does not check their
    spelling (the live service tries again every minute, liveservice.Layers.refresh). available: spell.available."""
    if available is None:
        from .spell import available
    return [code for code in dict.fromkeys(languages) if code in DICTIONARIES
            and not available(core.DICTIONARY_CODES[code])]


def dictionary_command(missing):
    return 'sudo apt install ' + ' '.join(DICTIONARIES[code][1] for code in missing)


def dictionary_check(missing):
    """The Słowniki row of checking while typing: None (no row) while every text language has its dictionary."""
    if not missing:
        return None
    names = ', '.join(tr(DICTIONARIES[code][0]) for code in missing)
    return Check(WARNING, tr('Brak słownika: {value} — pisownia nie jest sprawdzana. Zainstaluj go poleceniem spod przycisku, '
                             'a sprawdzanie użyje go w ciągu minuty.', value=names), 'install_dictionaries')


def browsers_present():
    """The browsers of browserhost.BROWSER_CONFIGS that have a configuration directory (were run once), as the installer
    registers the host only there."""
    from . import browserhost
    return [name for name, relative in browserhost.BROWSER_CONFIGS if (config_home() / relative).is_dir()]


def browser_check(status, registered, folder, present=None):
    """The row "Wtyczka" of the Przeglądarka group (the Mac's browserSection): the extensions connected to the engine (status is
    its Status JSON, None when it does not answer), else what is missing: a registered host, the extension loaded. folder:
    where the extension to load is. present: browsers_present(); with none of them (Ubuntu comes with Firefox only) there
    is nothing to fix, so the row only says where the extension works."""
    from . import browserhost
    connected = (status or {}).get('browsers') or {}
    if connected:
        names = ' · '.join(tr('{value} (wtyczka {version})', value=tr(browserhost.BROWSER_TITLES.get(name, name)), version=version)
                           for name, version in sorted(connected.items()))
        return Check(OK, tr('Połączona: ') + names + tr('. Stronę wyłączysz ikoną wtyczki.'))
    if status is None:
        return Check(INFO, tr('Silnik GramaDramy nie działa, więc wtyczka nie ma z kim się łączyć.'))
    if not registered and present is not None and not present:
        return Check(INFO, tr('Wtyczka działa w Chrome, Chromium, Edge, Brave i Vivaldi — tu żadnej z nich nie ma. W Firefoksie '
                              'skrót korekty działa bez niej, jak w innych aplikacjach.'))
    if not registered:
        return Check(WARNING, tr('Nie połączona — uruchom instalator, a potem przeglądarkę (Chrome, Chromium, Edge albo Brave).'), 'install')
    return Check(INFO, tr('Nie połączona — przeglądarka zamknięta albo wtyczka niewgrana (chrome://extensions → Tryb dewelopera → „Załaduj rozpakowane”).'), 'extension_folder')


def prompts_check(prompts):
    """The Prompty row (docs/prompts.md): the defaults, the files of the user's that differ from them, or a file that
    was ignored and why."""
    if prompts.problems:
        file, reason = prompts.problems[0]
        more = tr(' (i {value:d} innych)', value=len(prompts.problems) - 1) if len(prompts.problems) > 1 else ''
        return Check(WARNING, tr('Pominięty plik {file}: {reason} — używam domyślnego{more}.', file=file, reason=reason, more=more), 'prompts_open')
    if prompts.overridden:
        names = prompts.overridden
        shown = ', '.join(names[:3]) + (tr(' i {value:d} innych', value=len(names) - 3) if len(names) > 3 else '')
        return Check(OK, tr('Własne pliki ({value:d}): {shown}', value=len(names), shown=shown), 'prompts_open')
    return Check(INFO, tr('Domyślne. Własne wersje to pliki w folderze promptów.'), 'prompts_open')




def model_check(model, models, loaded, api='openai', local=False):
    """models, loaded: names from the server's model list and, for Ollama, /api/ps (None: it did not say). local: Ollama on
    this computer, where a missing model can be pulled."""
    if not model.strip():
        return Check(WARNING, tr('Nie wybrano modelu — wybierz go z listy.') if models else
                     tr('Brak modeli — pobierz model poniżej.') if local and models is not None else tr('Nie wybrano modelu — wpisz jego nazwę.'))
    if models is None:
        return Check(INFO, tr('Lista modeli będzie dostępna, gdy serwer odpowie.'))
    if model_index(models, model) < 0:
        return Check(ERROR, tr('Tego modelu nie ma na tym komputerze — pobierz go poniżej albo wybierz inny.') if local
                     else tr('Tego modelu nie ma na serwerze — wybierz inny.'))
    if api != 'ollama' or loaded is None:
        return Check(OK, '')
    if model_index(loaded, model) >= 0:
        return Check(OK, tr('Wczytany do pamięci.'))
    return Check(OK, tr('Wczyta się przy pierwszej korekcie.'))


def model_label(name):
    """'hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M' -> 'Bielik-11B-v2.3 · Q4_K_M'
    (the name as on macOS, Settings.modelDisplayName, with the quantization tag)."""
    short, tag = core.model_parts(name)
    return '%s · %s' % (short, tag) if tag else short


def model_labels(names):
    """Labels for a model list: the short name as on macOS, and the tag only where two models share
    that name (the list shows the selected model in a narrow space)."""
    parts = [core.model_parts(name) for name in names]
    shorts = [short for short, _ in parts]
    return [model_label(name) if shorts.count(short) > 1 else short for name, (short, _) in zip(names, parts)]


def seconds(value):
    text = '%.1f s' % value
    return text.replace('.', ',') if get_language() == 'pl' else text


def try_model(settings, cancel):
    """Correct SAMPLE with the model: (result, seconds to the first token or None, total seconds).
    Raises core.BackendError (Polish message) or core.Cancelled."""
    backend = core.Backend(settings)
    start, first = time.monotonic(), []

    def partial(_text):
        if not first:
            first.append(time.monotonic() - start)
    result = backend.correct(SAMPLE, 'fix', partial=partial, cancel=cancel)
    return result, (first[0] if first else None), time.monotonic() - start


def model_try_check(outcome):
    """Check for try_model's outcome: (result, first, total) or an exception."""
    if isinstance(outcome, core.Cancelled):
        return Check(INFO, tr('Przerwano.'))
    if isinstance(outcome, Exception):
        message = str(outcome) if isinstance(outcome, core.BackendError) else tr('Nie udało się: {__name__}', __name__=type(outcome).__name__)
        return Check(ERROR, message)
    result, first, total = outcome
    timing = tr('Model odpowiedział w {value}', value=seconds(total))
    if first is not None and total - first >= 1:
        timing += tr(' (pierwsze słowo po {value})', value=seconds(first))
    return Check(OK, '%s: „%s”' % (timing, result))


# ---- the summary at the top of the window ------------------------------------------------------
def summary(named):
    """named: (short label, Check) pairs -> (state, text)."""
    if any(check.state == BUSY for _, check in named):
        return BUSY, tr('Sprawdzam…')
    problems = list(dict.fromkeys(label for label, check in named if check.state in PROBLEMS))
    if not problems:
        return OK, tr('Wszystko gotowe')
    state = ERROR if any(check.state == ERROR for _, check in named) else WARNING
    return state, tr('Wymaga uwagi: ') + ', '.join(problems)


# ---- probes of the live system (blocking; gi imported lazily) -----------------------------------
def ibus_cli(*args):
    """The ibus CLI in a fresh process each time: libibus keeps one bus object per process, and
    without a main loop it never reconnects to a restarted ibus-daemon (install_engine.py)."""
    try:
        out = subprocess.run(['ibus', *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def has_schema(schema_id):
    """Gio.Settings.new() aborts the process on a missing schema, so look first."""
    from gi.repository import Gio
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


def gnome_bindings():
    """(label, accelerator) of the GNOME keybindings that take a key before the input method sees
    it: window manager, shell, mutter, media keys and custom shortcuts. A broken custom entry
    is skipped on its own and never hides the others."""
    from gi.repository import Gio
    found = []
    for schema_id in KEYBINDING_SCHEMAS:
        if not has_schema(schema_id):
            continue
        settings = Gio.Settings.new(schema_id)
        for key in settings.props.settings_schema.list_keys():
            value = settings.get_value(key)
            if value.get_type_string() == 'as':
                found += [(key, accel) for accel in value.unpack()]
    if has_schema(MEDIA_KEYS) and has_schema(CUSTOM_KEYBINDING):
        for path in Gio.Settings.new(MEDIA_KEYS).get_strv('custom-keybindings'):
            if not (path.startswith('/') and path.endswith('/') and '//' not in path):
                continue  # not a valid settings path (GLib would log a critical and refuse it)
            try:
                custom = Gio.Settings.new_with_path(CUSTOM_KEYBINDING, path)
            except (TypeError, ValueError):
                continue
            found.append((custom.get_string('name') or path, custom.get_string('binding')))
    return found


def input_sources():
    from gi.repository import Gio
    if not has_schema(INPUT_SOURCES):
        return []
    return [tuple(s) for s in Gio.Settings.new(INPUT_SOURCES).get_value('sources').unpack()]


def user_extensions_disabled():
    from gi.repository import Gio
    return has_schema(SHELL) and Gio.Settings.new(SHELL).get_boolean('disable-user-extensions')


def dbus(conn, dest, path, iface, method, args=None, reply='(s)', timeout=2000):
    """A synchronous call (worker threads only); the unpacked reply, or None on any error."""
    from gi.repository import Gio, GLib
    try:
        result = conn.call_sync(dest, path, iface, method, args, GLib.VariantType(reply) if reply else None,
                                Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
    except GLib.Error:
        return None
    return result.unpack() if result is not None else ()


def owner_pid(conn, name):
    from gi.repository import GLib
    reply = dbus(conn, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                 'GetConnectionUnixProcessID', GLib.Variant('(s)', (name,)), '(u)')
    return reply[0] if reply else None


def process_start(pid):
    """When a process started, in seconds since the epoch (/proc)."""
    try:
        stat = Path('/proc/%d/stat' % pid).read_text()
        ticks = int(stat[stat.rindex(')') + 2:].split()[19])  # field 22, starttime
        with open('/proc/stat') as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith('btime '))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / os.sysconf('SC_CLK_TCK')


def launcher_of(pid):
    """The engine launcher a process runs (its command line), or None."""
    try:
        argv = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
    except OSError:
        return None
    for arg in argv:
        if arg.endswith(b'/gramadrama-engine'):
            return Path(os.fsdecode(arg))
    return None


def component_state():
    """The engine's IBus description (component XML) against the installer's staged copy (the package ships both)."""
    found = component_file()
    staged = engine_dir() / 'gramadrama.xml'
    if found is None:
        return 'missing'
    if not staged.exists():
        return 'unknown'
    try:
        return 'current' if found.read_bytes() == staged.read_bytes() else 'outdated'
    except OSError:
        return 'unknown'


def _files(root, exclude=('__pycache__',), suffixes=None):
    if not root.is_dir():
        return {}
    files = {}
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if path.is_file() and not any(part in exclude for part in rel.parts) and not path.name.endswith('.compiled') \
                and (suffixes is None or path.suffix in suffixes):
            files[rel] = path
    return files


def _single(root, name):
    return {name: root / name} if (root / name).is_file() else {}


def _differ(a, b):
    if a.keys() != b.keys():
        return True
    try:
        return any(a[rel].read_bytes() != b[rel].read_bytes() for rel in a)
    except OSError:
        return True


def copy_differs(source):
    """True when the installed engine, app or panel differs from the repository at source."""
    source, target = Path(source), engine_dir()
    if not (source / 'gramadrama').is_dir() or not target.is_dir():
        return False
    pairs = [(_files(source / 'gramadrama', suffixes=('.py',)), _files(target / 'gramadrama', suffixes=('.py',)))]
    pairs += [(_single(source, name), _single(target, name)) for name in ('gramadrama-engine', 'gramadrama-app')]
    if (source / 'gnome-extension').is_dir():
        pairs.append((_files(source / 'gnome-extension'), _files(extension_dir())))
    return any(_differ(a, b) for a, b in pairs)


def extension_version_on_disk():
    try:
        return json.loads((extension_dir() / 'metadata.json').read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError, AttributeError):
        return None


def probe_system(conn, app_dir):
    """Everything about the engine and the panel that needs the ibus CLI, D-Bus or /proc (blocking).
    app_dir: the linux/ directory the app runs from (the repository or the installed copy)."""
    from gi.repository import GLib
    current = ibus_cli('engine')
    engines = ibus_cli('list-engine', '--name-only')
    status = dbus(conn, *ENGINE_BUS, 'Status')
    try:
        status = json.loads(status[0]) if status else None
    except ValueError:
        status = {}
    install = install_info()
    origin = stale = None
    if status is not None:
        pid = owner_pid(conn, ENGINE_BUS[0]) or 0
        launcher = launcher_of(pid)
        if launcher is not None:
            origin = 'installed' if launcher == engine_dir() / 'gramadrama-engine' else 'repository'
        changed, started = (install or {}).get('engine_changed_at'), process_start(pid)
        stale = origin == 'installed' and bool(changed and started) and started < changed
    info = dbus(conn, *EXTENSIONS_BUS, 'GetExtensionInfo', GLib.Variant('(s)', (EXTENSION_UUID,)), '(a{sv})')
    ping = dbus(conn, *PANEL_BUS, 'Ping')
    shell = owner_pid(conn, PANEL_BUS[0])
    installed_here = Path(app_dir).resolve() == engine_dir().resolve()
    source = (install or {}).get('source') if installed_here else str(app_dir)
    return {
        'current': (current.strip() or None) if current is not None else None,
        'registered': None if engines is None else ENGINE_NAME in engines.split(),
        'status': status, 'origin': origin, 'stale': bool(stale),
        'extension': info[0] if info else {}, 'loaded': ping[0] if ping else None,
        'on_disk': extension_version_on_disk(), 'shell_started': process_start(shell) if shell else None,
        'installed': (engine_dir() / 'gramadrama-engine').exists(), 'component': component_state(),
        'install': install, 'source': source,
        'differs': bool(source) and copy_differs(source),
        'browser_hosts': registered_browser_hosts(), 'browsers_present': browsers_present(),
        'browser_folder': str(browser_extension_dir(app_dir)),
    }
