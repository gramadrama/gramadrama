"""Live checking's text rules (docs/live-check.md), as on macOS (LiveCheck.swift). Pure: no gi, no network;
the live service (liveservice.py) and the tests import it, the engine does not.

Offsets are code points of the checked text. Masking replaces one code point with one space, so a
masked text keeps the original's offsets; LanguageTool and LSP count UTF-16 units, converted only at
those calls (utf16_to_index). Python's re has no \\p{L}: [^\\W\\d_] is a letter."""
from .i18n import tr
import dataclasses
import re

from . import core

LETTER = r'[^\W\d_]'
POLISH_LETTERS = frozenset('ąćęłńóśźżĄĆĘŁŃÓŚŹŻ')
# LanguageTool's rules that give false alarms in messages and around masked fragments.
# COMMA_COMPOUND_SENTENCE: the comma before "and" in an English compound sentence is optional (as on macOS).
DISABLED_RULES = ('WHITESPACE_RULE', 'PUNCTUATION_PARAGRAPH_END', 'UPPERCASE_SENTENCE_START',
                  'COMMA_PARENTHESIS_WHITESPACE', 'DOUBLE_PUNCTUATION', 'COMMA_COMPOUND_SENTENCE')
PRIORITY = {'LanguageTool': 3, 'słownik': 1, 'Bielik': 0, 'Model': 0}  # the better reason wins an overlap
MAX_CHARS = 6000
BIELIK_MAX_CHARS = 1500


@dataclasses.dataclass(frozen=True)
class Issue:
    start: int  # code points in the checked text
    end: int
    word: str
    message: str
    suggestions: tuple
    source: str  # 'słownik', 'LanguageTool', 'Bielik' or the experimental 'Model'
    rule: str
    description: str = ''  # LanguageTool's description of the rule, for the list of the rules turned off

    @property
    def spelling(self):
        return (self.source == 'słownik' or self.rule.startswith('MORFOLOGIK') or self.rule == 'SpellCheck'
                or self.rule == 'PL_SIMPLE_REPLACE')

    @property
    def can_turn_off(self):
        """Wyłącz regułę is offered for a LanguageTool rule about grammar, punctuation or style; a spelling one is a word
        to add to the dictionary instead."""
        return self.source == 'LanguageTool' and self.kind == 'grammar' and bool(self.rule)

    @property
    def kind(self):
        """'spelling' (coral), 'grammar' (orange) or 'style' (Bielik, violet), as the macOS underlines."""
        if self.source == 'Bielik':
            return 'style'
        return 'spelling' if self.spelling or self.rule == 'PL_DIACRITICS' else 'grammar'

    def moved(self, delta):
        return dataclasses.replace(self, start=self.start + delta, end=self.end + delta)


# ---- sentences, languages and masks ------------------------------------------------------------
# A sentence ends at .!?… before whitespace (or the end), with the spaces after it, or at a line end.
SENTENCE_END = re.compile(r'[.!?…]+(?=\s|$)[^\S\n]*|\n')


def blank(text, mask):
    """The positions in mask as spaces; line ends stay (LSP counts lines)."""
    if not mask:
        return text
    return ''.join(' ' if i in mask and c not in '\n\r' else c for i, c in enumerate(text))


def language(sentence, fallback, minimum_words, languages=core.DEFAULT_LANGUAGES):
    """'pl' or 'en': a Polish letter decides; with too few words the fallback; else the vote of function
    words (core), a tie to Polish (macOS: NLLanguageRecognizer, P(pl) >= P(en))."""
    if len(languages) == 1:
        return languages[0]
    if 'pl' in languages and not POLISH_LETTERS.isdisjoint(sentence):
        return 'pl'
    if 'de' in languages and not core.GERMAN_LETTERS.isdisjoint(sentence.lower()):
        return 'de'
    words = re.findall(LETTER + '+', sentence)
    if len(words) < minimum_words or not words:
        return fallback
    votes = core.language_votes(sentence, languages)
    return max(languages, key=lambda code: votes[code]) if any(votes.values()) else fallback


def sentences(text, mask=frozenset(), languages=core.DEFAULT_LANGUAGES):
    """[(start, end, 'pl'|'en')] of the text's sentences, together covering all of it; a short one takes
    the language of the whole text."""
    prose = blank(text, mask)
    fallback = language(prose, languages[0], 1, languages)
    result, start = [], 0
    for match in SENTENCE_END.finditer(prose):
        if match.end() > start:
            result.append((start, match.end(), language(prose[start:match.end()], fallback, 3, languages)))
            start = match.end()
    if start < len(prose):
        result.append((start, len(prose), language(prose[start:], fallback, 3, languages)))
    return result


class LiveText:
    """The text ready for checking: each sentence has its language, and the fragments without one
    (links, e-mails, @mentions, code, :emoji:) are masked. The Polish and the English version have the
    other language's sentences blanked, so their offsets are the text's."""

    def __init__(self, text, languages=core.DEFAULT_LANGUAGES):
        self.text, self.languages = text, tuple(languages)
        non_language = set()
        for match in core.NON_LANGUAGE.finditer(text):
            non_language.update(range(match.start(), match.end()))
        self.non_language = non_language
        self.masks = {code: set(non_language) for code in self.languages}
        for start, end, lang in sentences(text, non_language, self.languages):
            for code in self.languages:
                if code != lang:
                    self.masks[code].update(range(start, end))
        self.parts = {code: blank(text, mask) for code, mask in self.masks.items()}
        self.polish_mask = self.masks.get('pl', set(range(len(text))))
        self.english_mask = self.masks.get('en', set(range(len(text))))
        self.german_mask = self.masks.get('de', set(range(len(text))))
        self.polish = blank(text, self.polish_mask)
        self.english = blank(text, self.english_mask)
        self.german = blank(text, self.german_mask)

    @property
    def has_polish(self):
        return len(self.polish_mask) < len(self.text) and bool(self.polish.strip())

    @property
    def has_english(self):
        return len(self.english_mask) < len(self.text) and bool(self.english.strip())


def touches(mask, start, end):
    return any(i in mask for i in range(start, end))


# ---- suggestions ---------------------------------------------------------------------------------
FOLD = str.maketrans('ąćęłńóśźż', 'acelnoszz')


def fold(text):
    return text.lower().translate(FOLD)


def ranked(suggestions, word):
    """The same word with Polish letters first ("Dzieki" -> "Dzięki", not "Dziki"), then the rest in the
    dictionary's order; a capital first letter as in the word; no case-only duplicates."""
    folded = fold(word)
    result = [s for s in suggestions if fold(s) == folded] + [s for s in suggestions if fold(s) != folded]
    if word[:1].isupper():
        result = [s[:1].upper() + s[1:] for s in result]
    seen, unique = set(), []
    for s in result:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    return unique


# ---- missing Polish letters in real words -----------------------------------------------------------
# Words a dictionary accepts ("cos" as in cosinus, "tez" from "teza") that in messages are nearly always
# Polish letters left out. Verbs whose form depends on the person (testuje / testuję) are Bielik's.
DIACRITIC_FORMS = {
    'cos': 'coś', 'tez': 'też', 'wiec': 'więc', 'zle': 'źle', 'az': 'aż', 'bede': 'będę', 'musze': 'muszę',
    'zrobie': 'zrobię', 'mowie': 'mówię', 'moge': 'mogę', 'sprawdze': 'sprawdzę', 'prosze': 'proszę', 'ktos': 'ktoś',
    'gdzies': 'gdzieś', 'zeby': 'żeby', 'juz': 'już', 'moze': 'może', 'sie': 'się', 'dzieki': 'dzięki', 'wlasnie': 'właśnie',
}
WORD = re.compile(r'\b%s+\b' % LETTER)
# "ze" is the preposition before "mną/mnie" and consonant clusters on s/z/ś/ź/ż/w ("ze szkoły", "ze
# względu", "ze wszystkimi"); elsewhere it is usually "że" without its dot.
ZE_PREPOSITION = re.compile(r'^(mn|wszystk|[sśzźż][^aąeęioóuy]|w[^aąeęioóuy]|sob|sw|ś)', re.IGNORECASE)


def missing_diacritics(text, original=None):
    original = text if original is None else original
    matches = list(WORD.finditer(text))
    issues = []
    for index, match in enumerate(matches):
        token = match.group()
        lower = token.lower()
        suggestion = None
        if lower == 'ze':
            following = matches[index + 1].group() if index + 1 < len(matches) else ''
            # Something must follow "ze" to guess: the end of a sentence being typed is no mistake yet.
            if following and not ZE_PREPOSITION.match(following):
                suggestion = 'że'
        else:
            suggestion = DIACRITIC_FORMS.get(lower)
        if suggestion is None:
            continue
        if token[:1].isupper():
            suggestion = suggestion[:1].upper() + suggestion[1:]
        issues.append(Issue(match.start(), match.end(), original[match.start():match.end()], tr('Brak polskiej litery?'),
                            (suggestion,), 'słownik', 'PL_DIACRITICS'))
    return issues


def restoring_diacritics(fragment):
    """The fragment with its Polish letters back ("ze to" -> "że to"), or None when the list finds none."""
    found = [i for i in missing_diacritics(fragment + ' x') if i.end <= len(fragment)]
    if not found:
        return None
    for issue in reversed(found):
        fragment = fragment[:issue.start] + issue.suggestions[0] + fragment[issue.end:]
    return fragment


# ---- false alarms --------------------------------------------------------------------------------
def sentence_start(text, position):
    i = position - 1
    while i >= 0 and text[i] in ' \t ':
        i -= 1
    return i < 0 or text[i] in '.!?\n\r…'


def plausible(word, position, text, learned=lambda w: False, english=lambda w: False, allow_names=True):
    """An "unknown" word that is probably right: an abbreviation (PR, API), a name mid-sentence
    (Anna, Orion), numbers, paths, or an English word in a Polish sentence (deploy, staging).
    learned: the user's own dictionary; english: the English dictionary accepts it."""
    if len(word) <= 1:
        return True
    if learned(word):
        return True
    if any(c.isdigit() or c in '_./' for c in word):
        return True
    if sum(c.isupper() for c in word) >= 2:
        return True
    if allow_names and word[:1].isupper() and not sentence_start(text, position):
        return True
    return english(word)


# ---- merging the layers, and following edits -----------------------------------------------------------
def merge(*lists):
    """The same mistake from two sources (the same range, or two spelling hits on one word): the better
    source stays. Different mistakes in one place ("dziala ale": a comma, "dziala": spelling) both stay.
    Bielik and the list of missing letters only add what nothing else found there."""
    candidates = sorted((issue for issues in lists for issue in issues),
                        key=lambda i: (-PRIORITY[i.source], i.start))
    accepted = []
    for issue in candidates:
        def clash(other):
            overlaps = other.start < issue.end and issue.start < other.end
            return ((other.start, other.end) == (issue.start, issue.end) or (other.spelling and issue.spelling and overlaps)
                    or ((issue.source == 'Bielik' or issue.rule == 'PL_DIACRITICS') and overlaps))
        if not any(clash(other) for other in accepted):
            accepted.append(issue)
    return sorted(accepted, key=lambda i: (i.start, i.end - i.start))


def minimal_span(old, new):
    """(prefix, old_length, replacement): the part of old that new replaced."""
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    return prefix, len(old) - prefix - suffix, new[prefix:len(new) - suffix]


def shift(issues, old, new):
    """Known issues after an edit: the ones before it stay, the ones after it move by the change of length,
    the ones it touched go (they are checked again)."""
    if old == new:
        return list(issues)
    prefix, old_length, replacement = minimal_span(old, new)
    edit_end, delta = prefix + old_length, len(replacement) - old_length
    return [i if i.end < prefix else i.moved(delta) for i in issues if i.end < prefix or i.start > edit_end]


def under_caret(issue, caret):
    """The word being typed: the caret inside it or right at its end (at its start: shown)."""
    return caret is not None and issue.start < caret <= issue.end


def locate(original, near, text, reach=200):
    """Where the word is now: the whole-word occurrence nearest to near (the earlier one on a tie)."""
    best = None
    for match in re.finditer(re.escape(original), text):
        a, b = match.start(), match.end()
        if (a and re.match(LETTER, text[a - 1])) or (b < len(text) and re.match(LETTER, text[b])):
            continue
        distance = abs(a - near)
        if distance <= reach and (best is None or distance < abs(best - near)):
            best = a
    return best


# ---- Bielik's answer as issues ---------------------------------------------------------------------------
def bielik_issues(original, corrected):
    """Each change of the word diff is one issue on the original. Unlike the panel, every unchanged part
    ends a change, so "testuje cos" gives two. An insertion (a comma) is pinned to the word before it."""
    issues, offset, start, old, new = [], 0, 0, '', ''

    def flush():
        nonlocal old, new
        if (old or new) and old.strip() != new.strip():
            a, b = start, start + len(old)
            while a < b and original[a].isspace():
                a += 1
            while b > a and original[b - 1].isspace():
                b -= 1
            insertion = a == b
            if insertion:
                a = min(a, len(original))
                while a > 0 and original[a - 1].isspace():
                    a -= 1
                b = a
                while a > 0 and not original[a - 1].isspace():
                    a -= 1
                if a == b:
                    while b < len(original) and not original[b].isspace():
                        b += 1
            if b > a:
                word = original[a:b]
                span = (core._utf16(original[:start]), max(core._utf16(original[:start + len(old)]), core._utf16(original[:start]) + 1))
                suggestion = (word + new.strip() if a < start else new.strip() + ' ' + word) if insertion else new.strip()
                issues.append(Issue(a, b, word, core._reason(old, new, span, ()), (suggestion,) if suggestion else (),
                                    'Bielik', 'BIELIK'))
        old = new = ''

    for kind, text in core.diff_parts(original, corrected):
        if kind == 'same':
            flush()
            offset += len(text)
        elif kind == 'removed':
            if not old and not new:
                start = offset
            old += text
            offset += len(text)
        else:
            if not old and not new:
                start = offset
            new += text
    flush()
    return issues


def summary(count):
    """"1 błąd", "3 błędy", "5 błędów", "22 błędy", "12 błędów"."""
    if count == 1:
        return tr('1 błąd')
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return tr('%d błędy') % count
    return tr('%d błędów') % count


def utf16_to_index(text, units):
    """The code point index of a UTF-16 offset into text (LanguageTool, LSP)."""
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)
