"""The apps where GramaDrama is off, and the mode Ctrl+Alt+G starts in per app: the engine's reading of
apps.json (docs/apps.md, section 2; pure; no gi, and no core: only json).

The GramaDrama app writes the file. The engine reads only its "disabled" list and its "modes" object, and
every doubt means allowed and Popraw: a missing or broken file disables nothing (fail-open, as for apps that are not on
the list), and so does an unknown focus. A file is broken exactly when the app's own reading
(gramadrama/apps.py, read_config) says so, since the app then tells the user that GramaDrama works
in every app until the file is repaired; the engine only adds a size limit."""
import json

GRAMADRAMA_APP = 'pl.gramadrama.Linux.desktop'  # never off: its Wypróbuj field must always work
VERSION = 1
# The modes an app can start in (macOS Settings.defaultModeChoices): Popraw is the default and Tłumacz has
# its own shortcut, Ctrl+Alt+J, so it never replaces Ctrl+Alt+G.
DEFAULT_MODE = 'fix'
MODE_CHOICES = ('fix', 'improve', 'formal', 'casual', 'shorter')
# A larger apps.json is broken: parsing it holds the GIL, which the key path needs.
MAX_BYTES = 64 * 1024


def _ids(config, key):
    ids = config.get(key, [])
    if not isinstance(ids, list) or not all(isinstance(app, str) and app for app in ids):
        raise ValueError('"%s" is not a list of app IDs' % key)
    return ids


def _modes(config):
    modes = config.get('modes', {})
    if not isinstance(modes, dict) or not all(app and isinstance(mode, str) for app, mode in modes.items()):
        raise ValueError('"modes" is not an object of app ID: mode')
    return modes


def parse_all(data):
    """(disabled, modes) of apps.json's bytes: the app IDs in its "disabled" list, as a frozenset without
    the GramaDrama app, and {app ID: mode key} of its "modes" (a mode this build does not know stays in
    the dict and means Popraw, see default_mode). Raises ValueError for a broken file: too large, not
    UTF-8 JSON, not an object, a "v" other than 1, a "disabled" or "added" that is not a list of
    non-empty strings, or a "modes" that is not an object of strings."""
    if len(data) > MAX_BYTES:
        raise ValueError('apps.json is larger than %d bytes' % MAX_BYTES)
    try:
        config = json.loads(data.decode('utf-8'))  # UnicodeDecodeError and JSONDecodeError are ValueErrors
    except RecursionError:
        raise ValueError('apps.json nests too deep') from None
    if not isinstance(config, dict):
        raise ValueError('apps.json is not an object')
    version = config.get('v')
    if version != VERSION or isinstance(version, bool):
        raise ValueError('unknown apps.json version')
    _ids(config, 'added')  # not the engine's, but a file the app calls broken disables nothing
    _ids(config, 'sites')  # the extension's list: the same, and the app reads it the same way
    return frozenset(_ids(config, 'disabled')) - {GRAMADRAMA_APP}, dict(_modes(config))


def parse(data):
    """The app IDs in apps.json's "disabled" list (see parse_all, which also says when the file is broken)."""
    return parse_all(data)[0]


def load_all(data):
    """(disabled, modes, ok) for apps.json's bytes, or data None when there is no file: ok is False only
    for a broken file, which disables nothing and changes no mode."""
    if data is None:
        return frozenset(), {}, True
    try:
        return (*parse_all(data), True)
    except ValueError:
        return frozenset(), {}, False


def load(data):
    """(disabled, ok) for apps.json's bytes, or data None when there is no file: ok is False only
    for a broken file, which disables nothing."""
    if data is None:
        return frozenset(), True
    try:
        return parse(data), True
    except ValueError:
        return frozenset(), False


def load_sites(data):
    """The sites where the browser extension is off (host names), from apps.json's "sites" list: empty when there is no
    file, when it is broken (as the engine reads it: parse_all) and when "sites" is not a list of non-empty strings."""
    if data is None:
        return frozenset()
    try:
        parse_all(data)
        sites = json.loads(data.decode('utf-8')).get('sites', [])
    except (ValueError, RecursionError):
        return frozenset()
    if not isinstance(sites, list) or not all(isinstance(site, str) and site for site in sites):
        return frozenset()
    return frozenset(sites)


def with_site(data, site, disabled):
    """apps.json's bytes with the site added to or taken off the "sites" list (the file as it is otherwise: the app's
    own entries stay), for the toggle of the extension's popup. data None: a new file. ValueError for a broken file,
    which is never overwritten from here (the app says it is broken and repairs it)."""
    if data is None:
        config = {'v': VERSION}
    else:
        parse_all(data)
        config = json.loads(data.decode('utf-8'))
    sites = config.get('sites', [])
    if not isinstance(sites, list) or not all(isinstance(s, str) and s for s in sites):
        raise ValueError('"sites" is not a list of sites')
    sites = [s for s in dict.fromkeys(sites) if s != site]
    if disabled:
        sites.append(site)
    if sites:
        config['sites'] = sites
    else:
        config.pop('sites', None)
    return (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def default_mode(app, modes):
    """The mode Ctrl+Alt+G starts in for the focused app: its entry in apps.json when this build has that
    mode, else Popraw. An unknown app (None), no focus window ('') and the GramaDrama app get Popraw."""
    mode = modes.get(app) if app and app != GRAMADRAMA_APP and modes else None
    return mode if mode in MODE_CHOICES else DEFAULT_MODE


def is_off(app, disabled):
    """Ctrl+Alt+G belongs to the focused app: GramaDrama is off there. An unknown app (None: no
    extension, an old one, the lock screen) or no focus window ('') is allowed, and so is the
    GramaDrama app."""
    return bool(app) and app != GRAMADRAMA_APP and app in disabled
