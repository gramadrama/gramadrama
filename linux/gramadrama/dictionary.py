"""The user's dictionary and the rules they turned off (docs/live-check.md, docs/dictation.md).

One dictionary for everything GramaDrama does. It holds the own words typed in Pisanie (Settings.dictation_vocabulary,
named when they served dictation only) and the words added with "Dodaj do słownika", which Enchant keeps in its personal
word lists (paths.enchant_dir(), shared with the GNOME apps). Checking while typing accepts both, a correction is told
how to spell the ones that are in its text, and dictation puts them in the Whisper prompt and the cleanup's spelling list.

The LanguageTool rules turned off from the underline card ("Wyłącz regułę") are kept in rules.json with the rule's own
description, never the text it was found in, until they are turned on again in Pisanie.

Pure: no gi, no network, no core (core and the speech service import it)."""
import json
import os
import re

from . import paths
from .dictation import parse_vocabulary
from .i18n import tr

# The Enchant dictionary of each text language, as core.DICTIONARY_CODES (a test compares them). The personal list of the
# bare language (pl.dic) is read too: the GNOME apps that ask Enchant for 'pl' add their words there.
TAGS = {'pl': 'pl_PL', 'en': 'en_US', 'de': 'de_DE'}
MAX_LIST = 256 * 1024  # a larger word list is not read
MAX_WORD = 60  # a longer line of a word list is no word
MAX_PROMPT_WORDS = 20  # the words a correction is told about at most
LETTER = r'[^\W\d_]'
RULE = re.compile(r'[A-Za-z0-9_.\-]{1,100}')  # LanguageTool's rule ids: COMMA_PARENTHESIS_WHITESPACE, PL_BRAK_PRZECINKA…
MAX_RULES = 200
MAX_NAME = 200

_read = {}  # path -> ((mtime_ns, size), value): a file is parsed again only when it changed


def signature(path):
    """(mtime, size) of the file, or None when there is none: whether it changed since it was read."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _cached(path, parse):
    """parse(bytes) of the file, from the cache while the file is the same: parse(None) when there is no such file,
    parse(False) when it is too big or cannot be read."""
    current = signature(path)
    cached = _read.get(path)
    if cached is not None and cached[0] == current:
        return cached[1]
    data = None if current is None else False
    if current is not None and current[1] <= MAX_LIST:
        try:
            data = path.read_bytes()
        except OSError:
            pass
    value = parse(data)
    _read[path] = (current, value)
    return value


def _replace(path, text):
    """Atomically, as apps.json: a temp file, then a rename; the mode of the file it replaces (Enchant's 0664), else 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(temp, mode)  # a temp file left over from a crash keeps its old mode through O_CREAT
    os.replace(temp, path)


# ---- the words ------------------------------------------------------------------------------------
def _words(data):
    """The words of an Enchant personal list, in the order they were added: one per line, UTF-8, '#' a comment."""
    if not data:
        return []
    words = []
    for line in data.decode('utf-8-sig', 'replace').splitlines():
        word = line.strip()
        if word and not word.startswith('#') and len(word) <= MAX_WORD and '�' not in word \
                and not any(c.isspace() for c in word):
            words.append(word)
    return words


def list_paths(languages):
    """The personal lists of these languages, for each in order: pl_PL.dic, then pl.dic."""
    directory = paths.enchant_dir()
    return [directory / (name + '.dic') for code in languages if code in TAGS for name in (TAGS[code], code)]


def learned_words(languages):
    """The words added to the dictionary in these languages, the latest of each list first, each once
    (case-insensitively)."""
    seen, words = set(), []
    for path in list_paths(languages):
        for word in reversed(_cached(path, _words)):
            if word.lower() not in seen:
                seen.add(word.lower())
                words.append(word)
    return words


def own_words(settings):
    """The words typed in Pisanie, in their order."""
    value = settings.dictation_vocabulary
    return parse_vocabulary(value if isinstance(value, str) else '')


def user_words(settings, languages=None):
    """The whole dictionary: the own words, then the learned ones (the latest first), each once."""
    words = own_words(settings)
    seen = {word.lower() for word in words}
    for word in learned_words(settings.languages if languages is None else languages):
        if word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


def forget(word, languages):
    """Removes word from the personal lists of these languages (the file is written again, without it): None, or why it
    could not be. Not Enchant's own remove, which also puts the word on its exclusion list, so that a correct word kept in
    the panel would be underlined from then on in every GNOME app."""
    for path in list_paths(languages):
        if word not in _cached(path, _words):
            continue
        try:
            lines = path.read_bytes().decode('utf-8-sig', 'replace').splitlines()
            _replace(path, ''.join(line + '\n' for line in lines if line.strip() != word))
        except OSError as exc:
            return tr('Nie udało się zapisać słownika: {value}', value=exc.strerror or exc)
    return None


class Known:
    """Whether a word is one of the user's own, spelt as Enchant accepts a word of its personal list: as written, in
    capitals, or with a capital first letter when it is written in lower case ("Staging" for "staging")."""

    def __init__(self, words=()):
        self.exact = frozenset(words)
        self.upper = frozenset(word.upper() for word in words)

    def __contains__(self, word):
        return (word in self.exact or (word.isupper() and word in self.upper)
                or (word[:1].isupper() and word[:1].lower() + word[1:] in self.exact))

    def __bool__(self):
        return bool(self.exact)


def relevant(words, text):
    """The words of the dictionary that the text uses, in their order, at most MAX_PROMPT_WORDS: as a word of its own, in
    any case ("Gramadrama" for "GramaDrama"), and from four letters on with up to three more, as Polish declension adds
    them ("Bielika" for "Bielik"). A correction is told only about these: nothing it is not already sent."""
    found = []
    for word in words:
        tail = LETTER + '{0,3}' if len(word) >= 4 else ''
        if re.search(r'(?<!%s)%s%s(?!%s)' % (LETTER, re.escape(word), tail, LETTER), text, re.IGNORECASE):
            found.append(word)
            if len(found) == MAX_PROMPT_WORDS:
                break
    return found


# ---- the rules turned off -------------------------------------------------------------------------
def _rules(data):
    """{rule id: description} of rules.json ({"off": {...}}); None when the file is there but broken."""
    if data is None:
        return {}
    if data is False:
        return None
    try:
        off = json.loads(data.decode('utf-8')).get('off')
    except (ValueError, UnicodeError, AttributeError):
        return None
    if not isinstance(off, dict):
        return None
    return {rule: ' '.join(name.split())[:MAX_NAME] for rule, name in list(off.items())[:MAX_RULES]
            if isinstance(rule, str) and RULE.fullmatch(rule) and isinstance(name, str)}


def rules_off():
    """{rule id: description} of the LanguageTool rules turned off, in the order they were; {} when there are none or
    rules.json is broken (every rule is shown then, as before)."""
    return _cached(paths.rules_path(), _rules) or {}


def _save_rules(change):
    """Applies change(off) to rules.json: None, or why it could not be saved. A broken file is kept as rules.json.bak."""
    path = paths.rules_path()
    current = _cached(path, _rules)
    off = dict(current or {})
    change(off)
    try:
        if current is None and os.path.lexists(path):
            os.replace(path, path.with_name(path.name + '.bak'))
        _replace(path, json.dumps({'off': off}, indent=2, ensure_ascii=False) + '\n')
    except OSError as exc:
        return tr('Nie udało się zapisać wyłączonych reguł: {value}', value=exc.strerror or exc)
    return None


def turn_off_rule(rule, name=''):
    """The rule is not shown from now on (name: its description, for the list in Pisanie)."""
    if not RULE.fullmatch(rule or ''):
        return tr('Nieznana reguła.')

    def change(off):
        if rule not in off and len(off) >= MAX_RULES:
            off.pop(next(iter(off)))  # the oldest makes room
        off[rule] = ' '.join(str(name or '').split())[:MAX_NAME]
    return _save_rules(change)


def turn_on_rule(rule):
    return _save_rules(lambda off: off.pop(rule, None))
