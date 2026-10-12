"""Safe, configurable writing shortcuts shared by the settings UI and the IBus engine.

Only ASCII letters with Ctrl+Alt, Ctrl+Shift or Ctrl+Alt+Shift are accepted. Exact modifier
matching keeps ordinary typing, AltGr, and desktop Super shortcuts out of the key path.
"""
import dataclasses
import re
from ..i18n import tr

SHIFT, CONTROL, ALT = 0x1, 0x4, 0x8  # IBus/GDK modifier bits
DEFAULTS = {'correction_shortcut': 'Ctrl+Alt+G', 'translation_shortcut': 'Ctrl+Alt+J'}
ALIASES = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'primary': 'Ctrl', 'alt': 'Alt', 'mod1': 'Alt', 'shift': 'Shift'}
PROBLEM = 'Użyj Ctrl+Alt, Ctrl+Shift lub Ctrl+Alt+Shift oraz jednej litery A–Z (np. Ctrl+Alt+G)'
DUPLICATE = 'Korekta i tłumaczenie muszą mieć różne skróty'


@dataclasses.dataclass(frozen=True)
class Shortcut:
    key: str
    modifiers: int

    @property
    def label(self):
        return '+'.join([name for mask, name in ((CONTROL, 'Ctrl'), (ALT, 'Alt'), (SHIFT, 'Shift'))
                         if self.modifiers & mask] + [self.key.upper()])

    @property
    def accelerator(self):
        return ''.join(name for mask, name in ((CONTROL, '<Control>'), (ALT, '<Alt>'), (SHIFT, '<Shift>'))
                       if self.modifiers & mask) + self.key

    def matches(self, keyval, modifiers):
        return keyval in (ord(self.key), ord(self.key.upper())) and modifiers == self.modifiers


def parse(value):
    if not isinstance(value, str):
        raise ValueError(tr(PROBLEM))
    value = value.strip()
    if value.startswith('<'):
        match = re.fullmatch(r'((?:<[^<>]+>)+)([a-zA-Z])', value)
        if not match:
            raise ValueError(tr(PROBLEM))
        parts = re.findall(r'<([^<>]+)>', match[1]) + [match[2]]
    else:
        parts = [part.strip() for part in value.split('+')]
    mods = [ALIASES.get(part.lower()) for part in parts[:-1]]
    if (len(parts[-1]) != 1 or parts[-1] not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
            or None in mods or len(mods) != len(set(mods))
            or set(mods) not in ({'Ctrl', 'Alt'}, {'Ctrl', 'Shift'}, {'Ctrl', 'Alt', 'Shift'})):
        raise ValueError(tr(PROBLEM))
    bits = sum({'Ctrl': CONTROL, 'Alt': ALT, 'Shift': SHIFT}[mod] for mod in mods)
    return Shortcut(parts[-1].lower(), bits)


def resolve(data=None):
    """A complete pair; a corrupt or conflicting configuration safely restores both defaults."""
    data = data if isinstance(data, dict) else {}
    try:
        result = tuple(parse(data.get(field, value)) for field, value in DEFAULTS.items())
        if result[0] == result[1]:
            raise ValueError(tr(DUPLICATE))
        return result
    except ValueError:
        return tuple(parse(value) for value in DEFAULTS.values())
