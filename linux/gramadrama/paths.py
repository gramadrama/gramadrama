"""Where GramaDrama keeps its configuration and its downloaded tools. Shared by the engine process, its
children and the app; the engine process imports this module but never core.py (install_engine.engine_digest)."""
import os
from pathlib import Path


def config_dir():
    # Only an absolute XDG_CONFIG_HOME counts (the XDG spec ignores empty and relative ones): a
    # relative one would point the engine's backend (cwd /) and the app at different files.
    config = os.environ.get('XDG_CONFIG_HOME') or ''
    return (Path(config) if os.path.isabs(config) else Path.home() / '.config') / 'gramadrama'


def settings_path():
    """settings.json: the server addresses and the model (core.Settings)."""
    return config_dir() / 'settings.json'


def auth_path():
    """auth.json: the tokens of the login to the servers (oidc.py). Apart from settings.json: the engine, its
    children and the app refresh them, and the app's settings must never be rewritten by that."""
    return config_dir() / 'auth.json'


def prompts_dir():
    """The prompts as files (prompts.py, docs/prompts.md): a file of a piece's name replaces its default."""
    return config_dir() / 'prompts'


def apps_path():
    """apps.json: the apps where GramaDrama is off, and the ones added by hand (docs/apps.md)."""
    return config_dir() / 'apps.json'


def rules_path():
    """rules.json: the LanguageTool rules turned off from the underline card (dictionary.py, docs/live-check.md)."""
    return config_dir() / 'rules.json'


def enchant_dir():
    """Where Enchant 2 keeps the personal word lists that "Dodaj do słownika" adds to (<tag>.dic, shared with the GNOME
    apps): $ENCHANT_CONFIG_DIR when it is absolute, else $XDG_CONFIG_HOME/enchant, as Enchant itself decides."""
    custom = os.environ.get('ENCHANT_CONFIG_DIR') or ''
    if os.path.isabs(custom):
        return Path(custom)
    return config_dir().parent / 'enchant'


def runtime_dir():
    """The directory of the sockets of GramaDrama, made 0700 by whoever listens: $XDG_RUNTIME_DIR/gramadrama (the user's
    /run/user/UID), else /tmp/gramadrama-UID."""
    runtime = os.environ.get('XDG_RUNTIME_DIR') or ''
    if os.path.isabs(runtime):
        return Path(runtime) / 'gramadrama'
    return Path('/tmp') / ('gramadrama-%d' % os.getuid())


def browser_socket_path():
    """Where the engine listens for the hosts of the browser extension (browserhost.py, docs/browser.md)."""
    return runtime_dir() / 'browser.sock'


def cache_dir():
    """What can be thrown away and made again (the route's answer, route.json): ~/.cache/gramadrama, where the engine's log is."""
    cache = os.environ.get('XDG_CACHE_HOME') or ''
    return (Path(cache) if os.path.isabs(cache) else Path.home() / '.cache') / 'gramadrama'


def state_dir():
    """What the app remembers between runs, not a setting (the window's last tab): ~/.local/state/gramadrama."""
    state = os.environ.get('XDG_STATE_HOME') or ''
    return (Path(state) if os.path.isabs(state) else Path.home() / '.local' / 'state') / 'gramadrama'


def data_dir():
    data = os.environ.get('XDG_DATA_HOME') or ''
    return Path(data) if os.path.isabs(data) else Path.home() / '.local' / 'share'


def tools_dir():
    """Downloaded tools, apart from the engine's copy (the installer replaces that one): the speech
    environment with faster-whisper and CUDA libraries and the Whisper model."""
    return data_dir() / 'gramadrama-tools'


def speech_python():
    """The Python of the speech environment (uv, Python 3.13: faster-whisper has no 3.14 wheels)."""
    return tools_dir() / 'speech' / 'bin' / 'python'


def whisper_model():
    """faster-whisper large-v3-turbo (CTranslate2, float16), as mobiuslabsgmbh publishes it."""
    return tools_dir() / 'models' / 'faster-whisper-large-v3-turbo'

