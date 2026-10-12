"""Small, dependency-free UI catalogues, independent of text/model languages.

Polish source messages are message IDs. English is the fallback for any primary
language without a catalogue. Contributors can add locales/<code>/*.json; no
generated files or gettext tools are needed. User text is never passed through
this module. Call tr at display time, not while importing a module.
"""
from functools import lru_cache
import json
from pathlib import Path
import re

LOCALES = Path(__file__).with_name('locales')
_language = 'pl'


def interface_language(primary):
    """Use the primary language when translated, English otherwise."""
    code = str(primary or '').lower().replace('_', '-').split('-')[0]
    if code == 'pl':
        return 'pl'
    if re.fullmatch(r'[a-z]{2,3}', code) and code != 'en' and (LOCALES / code).is_dir():
        return code
    return 'en'


def set_language(primary):
    global _language
    _language = interface_language(primary)
    return _language


def get_language():
    return _language


@lru_cache(maxsize=16)
def catalogue(language):
    """Load packaged JSON only; a damaged optional file cannot stop typing."""
    result = {}
    for path in sorted((LOCALES / language).glob('*.json')):
        try:
            messages = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(messages, dict):
            result.update({key: value for key, value in messages.items()
                           if isinstance(key, str) and isinstance(value, str) and value})
    return result


def tr(message, *, language=None, **values):
    """Translate a UI template; interpolate named values only when supplied.

    Explicit language is useful for asynchronous work bound to a settings
    snapshot. Without values, %-style templates remain intact for callers.
    """
    locale = _language if language is None else interface_language(language)
    translated = message
    if locale != 'pl':
        translated = catalogue(locale).get(message, catalogue('en').get(message, message))
    if values:
        try:
            return translated.format(**values)
        except (KeyError, ValueError, IndexError):
            # An incomplete community translation must not break a user action.
            fallback = catalogue('en').get(message, message) if locale != 'pl' else message
            try:
                return fallback.format(**values)
            except (KeyError, ValueError, IndexError):
                return message.format(**values)
    return translated
