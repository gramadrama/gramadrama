"""The word diff between a text and its correction, and the change list with reasons, as on macOS (Diff.swift).
Pure (no gi, no network): core uses it for the backend's results, and the engine recomputes a diff after the result
was edited in the panel without importing core (install_engine.engine_digest)."""
import difflib
import re
import unicodedata
from ..i18n import tr

# Word tokens as on macOS (Diff.tokenize): letters, digits, '_' and apostrophes form words;
# whitespace runs are one token; every other character is its own token.
TOKEN = re.compile(r"[\w'’]+|\s+|[^\w\s]")
MAX_CHANGES = 30


# A sentence with its closing punctuation and the whitespace after it (a line break ends one too).
SENTENCE = re.compile(r'[^.!?…\n]*(?:[.!?…]+|\n|$)[^\S\n]*\n?')
# difflib is quadratic in the worst case: a dense rewrite of 6000 characters took 13 s token by
# token. Sentences are aligned first and diffed one pair at a time; a block larger than this many
# token pairs is diffed with autojunk (fast, coarser: frequent tokens like spaces cannot anchor).
DENSE_PAIRS = 400_000


def diff_parts(original, result):
    """Word-level diff as (kind, text) with kind 'same', 'removed' or 'added'."""
    left = [s for s in SENTENCE.findall(original) if s]
    right = [s for s in SENTENCE.findall(result) if s]
    if len(left) <= 1 or len(right) <= 1:
        yield from _token_diff(original, result)
        return
    for tag, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if tag == 'equal':
            yield 'same', ''.join(right[c:d])
        elif b - a == d - c:  # the same number of sentences: a rewrite, pair them up
            for i in range(b - a):
                yield from _token_diff(left[a + i], right[c + i])
        else:
            yield from _token_diff(''.join(left[a:b]), ''.join(right[c:d]))


def _token_diff(original, result):
    left, right = TOKEN.findall(original), TOKEN.findall(result)
    dense = len(left) * len(right) > DENSE_PAIRS
    for tag, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=dense).get_opcodes():
        if tag == 'equal':
            yield 'same', ''.join(right[c:d])
        else:
            if a != b:
                yield 'removed', ''.join(left[a:b])
            if c != d:
                yield 'added', ''.join(right[c:d])


def _utf16(text):
    return len(text.encode('utf-16-le')) // 2


def changes(segments, original='', hints=()):
    """Adjacent removals and additions grouped into {'from', 'to', 'reason', 'segments'}, as on macOS
    (Diff.changes); a single space between two changes does not split the group. 'segments' is
    [first, last): the segments the change was made of, to undo it alone (macOS Change.segments).
    hints are LanguageTool matches (UTF-16 offsets into the original)."""
    result, offset, start = [], 0, 0
    first = last = 0
    old = new = ''

    def flush():
        nonlocal old, new
        if old or new:
            span = (start, max(start + _utf16(old), start + 1))
            result.append({'from': old.strip(), 'to': new.strip(), 'reason': _reason(old, new, span, hints),
                           'segments': [first, last]})
        old = new = ''

    for index, (kind, text) in enumerate(segments):
        if kind == 'same':
            if (old or new) and len(text) == 1 and text.isspace():
                old += text
                new += text
            else:
                flush()
            offset += _utf16(text)
        elif kind == 'removed':
            if not old and not new:
                start, first = offset, index
            old += text
            offset += _utf16(text)
            last = index + 1
        else:
            if not old and not new:
                start, first = offset, index
            new += text
            last = index + 1
    flush()
    return result


def _fold_polish(text):
    decomposed = unicodedata.normalize('NFD', text)
    return ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn').replace('ł', 'l').replace('Ł', 'L')


def _punctuation(c):
    return unicodedata.category(c).startswith('P')


def _levenshtein(a, b):
    a, b = a.lower(), b.lower()
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _reason(old, new, span, hints):
    # Specific categories from the comparison first: they are more accurate than LanguageTool's
    # generic "possible spelling mistake".
    a, b = old.strip(), new.strip()
    if not a and not b:
        return tr('Odstęp')
    aw, bw = ''.join(c for c in a if not c.isspace()), ''.join(c for c in b if not c.isspace())
    spacing_changed = len(a.split()) != len(b.split())
    if aw == bw:
        return tr('Pisownia łączna / rozdzielna') if any(c.isalpha() for c in a) else tr('Odstęp')
    if not a and all(_punctuation(c) for c in b):
        return tr('Brakujący znak interpunkcyjny')
    if not b and all(_punctuation(c) for c in a):
        return tr('Zbędny znak interpunkcyjny')
    if a.lower() == b.lower():
        return tr('Wielka / mała litera')
    if _fold_polish(aw) == _fold_polish(bw):
        return tr('Polskie znaki i pisownia łączna / rozdzielna') if spacing_changed else tr('Polskie znaki')
    al, bl = ''.join(c for c in aw if not _punctuation(c)), ''.join(c for c in bw if not _punctuation(c))
    if al == bl:
        return tr('Interpunkcja')
    if _fold_polish(al) == _fold_polish(bl):
        return tr('Polskie znaki i interpunkcja')
    for hint in hints:
        start = hint.get('offset', 0)
        end = start + max(hint.get('length', 0), 1)
        rule = (hint.get('rule') or {}).get('id', '')
        if start < span[1] and span[0] < end and not rule.startswith('MORFOLOGIK') and hint.get('message'):
            return hint['message']
    if ' ' not in a and ' ' not in b and _levenshtein(a, b) <= 2:
        return tr('Literówka')
    return tr('Gramatyka / styl')
