"""Spelling for live checking (docs/live-check.md): Enchant through ctypes, with the system's Hunspell
dictionaries (pl_PL, en_US) and the user's own word list, which Enchant keeps in ~/.config/enchant and
shares with GNOME apps ("Dodaj do słownika", as NSSpellChecker's learnWord on macOS)."""
from .i18n import tr
import ctypes
import re

from . import live as L

LIBRARY = 'libenchant-2.so.2'
# Words: letters, and apostrophes inside English ones ("don't").
WORD = re.compile(r"%s+(?:['’]%s+)*" % (L.LETTER, L.LETTER))



def available(tag):
    """Whether Enchant has a dictionary for tag (e.g. 'pl_PL'), asked anew each time (a package installed meanwhile counts);
    False without Enchant."""
    try:
        return Speller.exists(tag)
    except LookupError:
        return False

class Speller:
    """One dictionary, e.g. 'pl_PL'. Raises LookupError when Enchant or the dictionary is missing."""

    _lib = None
    _broker = None

    @classmethod
    def _load(cls):
        if cls._lib is None:
            try:
                lib = ctypes.CDLL(LIBRARY)
            except OSError as exc:
                raise LookupError('Enchant: %s' % exc) from exc
            lib.enchant_broker_init.restype = ctypes.c_void_p
            lib.enchant_broker_request_dict.restype = ctypes.c_void_p
            lib.enchant_broker_request_dict.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            for name in ('enchant_dict_check', 'enchant_dict_is_added'):
                getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ssize_t]
            lib.enchant_dict_add.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ssize_t]
            lib.enchant_dict_suggest.restype = ctypes.POINTER(ctypes.c_char_p)
            lib.enchant_dict_suggest.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ssize_t,
                                                 ctypes.POINTER(ctypes.c_size_t)]
            lib.enchant_dict_free_string_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
            cls._broker = lib.enchant_broker_init()
            cls._lib = lib
        return cls._lib

    @classmethod
    def exists(cls, tag):
        lib = cls._load()
        lib.enchant_broker_dict_exists.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        return bool(lib.enchant_broker_dict_exists(cls._broker, tag.encode()))

    def __init__(self, tag):
        self.lib = self._load()
        self.dict = self.lib.enchant_broker_request_dict(self._broker, tag.encode())
        if not self.dict:
            raise LookupError('no %s dictionary' % tag)
        self.tag = tag

    def check(self, word):
        data = word.encode('utf-8')
        return self.lib.enchant_dict_check(self.dict, data, len(data)) == 0

    def suggest(self, word):
        data = word.encode('utf-8')
        count = ctypes.c_size_t()
        found = self.lib.enchant_dict_suggest(self.dict, data, len(data), ctypes.byref(count))
        if not found:
            return []
        try:
            return [found[i].decode('utf-8', 'replace') for i in range(count.value)]
        finally:
            self.lib.enchant_dict_free_string_list(self.dict, found)

    def add(self, word):
        """Into the user's own list: accepted from now on, in every app that uses Enchant."""
        data = word.encode('utf-8')
        self.lib.enchant_dict_add(self.dict, data, len(data))

    def learned(self, word):
        data = word.encode('utf-8')
        return self.lib.enchant_dict_is_added(self.dict, data, len(data)) != 0


def spelling_issues(masked, original, mask, speller, english=None, learned=lambda w: False, rule='SPELLING_PL',
                    allow_names=True):
    """macOS LiveChecker.spelling: every unknown word of the masked text that is not plausible
    (live.plausible), with up to 3 ranked suggestions. english: the English speller, for English words
    in Polish sentences."""
    issues = []
    for match in WORD.finditer(masked):
        start, end = match.span()
        word = original[start:end]
        if speller.check(word) or L.touches(mask, start, end):
            continue
        if L.plausible(word, start, original, learned, english.check if english is not None else lambda w: False,
                       allow_names=allow_names):
            continue
        issues.append(L.Issue(start, end, word, tr('Nieznane słowo'), tuple(L.ranked(speller.suggest(word), word)[:3]),
                              'słownik', rule))
    return issues
