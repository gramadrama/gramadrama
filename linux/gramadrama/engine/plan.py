"""Capture scope, minimal replace operations and echo verification (pure; no gi, no core)."""
import dataclasses
import unicodedata
from .context import Refusal

MAX_CHARS = 6000  # must equal core.MAX_CHARS (unit-tested; core is not imported here)
EDGE_BYTES = 3900  # text-input-v3 sends at most 4000 bytes around the caret


@dataclasses.dataclass(frozen=True)
class Capture:
    scope: str  # 'selection' or 'paragraph'
    lo: int
    hi: int
    source: str
    text: str
    cursor: int
    anchor: int
    client: str


@dataclasses.dataclass(frozen=True)
class Ops:
    """delete_surrounding_text(*delete) then commit_text(commit); None parts are skipped.
    start/end/replacement describe the same edit as a range of Capture.text."""
    delete: tuple = None  # (offset relative to the cursor, nchars), code points
    commit: str = None
    start: int = 0
    end: int = 0
    replacement: str = ''

    @property
    def nothing(self):
        return self.delete is None and self.commit is None


NOTHING = Ops()


@dataclasses.dataclass(frozen=True)
class Expected:
    text: str
    cursor: int
    replacement: str


def _extends(ch):
    o = ord(ch)
    if o < 0x300:
        return False
    return (o == 0x200d or 0xfe00 <= o <= 0xfe0f or 0x1f3fb <= o <= 0x1f3ff or 0xe0020 <= o <= 0xe007f
            or 0xe0100 <= o <= 0xe01ef or unicodedata.category(ch) in ('Mn', 'Me', 'Mc'))


def _regional(ch):
    return 0x1f1e6 <= ord(ch) <= 0x1f1ff


def is_boundary(s, i):
    """True when index i of s does not split a user-perceived character (conservative UAX #29:
    combining marks, ZWJ sequences, variation selectors, skin tones, tags, flags, CR LF)."""
    if i <= 0 or i >= len(s):
        return True
    before, ch = s[i - 1], s[i]
    if _extends(ch) or before == '‍' or (before == '\r' and ch == '\n'):
        return False
    if _regional(before) and _regional(ch):
        run = 0
        while i - run - 1 >= 0 and _regional(s[i - run - 1]):
            run += 1
        return run % 2 == 0
    return True


def graphemes(s):
    """User-perceived characters of s, split exactly where is_boundary() is True, in one pass (a
    rescan per code point is quadratic on a run of flags)."""
    out = []
    prev, flags = '', 0  # flags: regional indicators in a row right before ch
    for ch in s:
        regional = _regional(ch)
        if out and (_extends(ch) or prev == '‍' or (prev == '\r' and ch == '\n') or (regional and flags % 2)):
            out[-1] += ch
        else:
            out.append(ch)
        flags = flags + 1 if regional else 0
        prev = ch
    return out


def paragraph_bounds(text, cursor):
    lo = text.rfind('\n', 0, cursor) + 1
    hi = text.find('\n', cursor)
    return lo, len(text) if hi < 0 else hi


def capture(snapshot):
    """Choose what the hotkey corrects: the selection, else the caret's paragraph."""
    if isinstance(snapshot, Refusal):
        return snapshot
    text, cursor, anchor = snapshot.text, snapshot.cursor, snapshot.anchor
    if anchor != cursor:
        scope, (lo, hi) = 'selection', sorted((cursor, anchor))
    elif snapshot.maybe_hidden_selection:
        return Refusal('hidden_selection')
    elif snapshot.selection_unknown:
        return Refusal('selection_unknown')
    else:
        scope, (lo, hi) = 'paragraph', paragraph_bounds(text, cursor)
    source = text[lo:hi]
    if not source.strip():
        return Refusal('empty')
    if hi - lo > MAX_CHARS:
        return Refusal('too_long')
    if '￼' in source:
        return Refusal('embedded')
    # A range that runs into the edge of a trimmed window may continue beyond it.
    if (lo == 0 or hi == len(text)) and len(text.encode('utf-8')) >= EDGE_BYTES:
        return Refusal('truncated')
    return Capture(scope, lo, hi, source, text, cursor, anchor, snapshot.client)


def _common_prefix(a, b):
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _common_suffix(a, b):
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[len(a) - mid:] == b[len(b) - mid:]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def bad_result(corrected):
    """A result that must never be applied: empty or whitespace, an embedded object, NUL, or text
    that is not valid Unicode (a lone surrogate cannot be sent to the client)."""
    if not corrected.strip() or '￼' in corrected or '\x00' in corrected:
        return True
    try:
        corrected.encode('utf-8')
    except UnicodeEncodeError:
        return True
    return False


# Line ends that apps store differently (CR and VT in PowerPoint, CRLF, LS and PS) while the model answers
# with LF: the same list, order and rule as macOS ImeEdits (tests/ime_cases.json).
LINE_ENDS = ('\r\n', '\n', '\r', '\x0b', ' ', ' ')
FOREIGN_LINE_ENDS = ('\r', '\x0b', ' ', ' ')


def split_lines(text):
    """(lines, line ends): text cut at every line end, CRLF counting as one."""
    lines, found, start, i = [], [], 0, 0
    while i < len(text):
        end = next((e for e in LINE_ENDS if text.startswith(e, i)), None)
        if end is None:
            i += 1
            continue
        lines.append(text[start:i])
        found.append(end)
        i += len(end)
        start = i
    lines.append(text[start:])
    return lines, found


def map_line_ends(original, corrected):
    """corrected with the line ends of original, in order; None when the line count differs, since then it
    is not known which line end the app wants for a new break."""
    wanted = split_lines(original)[1]
    lines, found = split_lines(corrected)
    if wanted == found:
        return corrected
    if len(wanted) != len(found):
        return None
    return ''.join(line + (wanted[i] if i < len(wanted) else '') for i, line in enumerate(lines))


def replace_ops(capture, now, corrected):
    """Operations that turn capture.source into corrected, validated against the field as it is
    now (call at apply time, never earlier). Returns Ops, NOTHING or Refusal."""
    if isinstance(now, Refusal):
        return now
    if (now.client, now.text, now.cursor, now.anchor) != (capture.client, capture.text, capture.cursor, capture.anchor):
        return Refusal('changed')
    source = capture.source
    if any(end in source for end in FOREIGN_LINE_ENDS):
        # An app with line ends of its own (PowerPoint's Shift+Enter and Enter): keep them. Where the field
        # uses only LF the model's LF goes in as it is, which also lets a rewrite change the line count.
        corrected = map_line_ends(source, corrected)
        if corrected is None:
            return Refusal('line_structure')
    if corrected == source:
        return NOTHING
    if bad_result(corrected):
        return Refusal('bad_result')
    if capture.scope == 'selection':
        # Clients replace the selection on commit; never delete while a selection exists.
        return Ops(None, corrected, capture.lo, capture.hi, corrected)
    p = _common_prefix(source, corrected)
    s = _common_suffix(source[p:], corrected[p:])
    while p and not (is_boundary(source, p) and is_boundary(corrected, p)):
        p -= 1
    while s and not (is_boundary(source, len(source) - s) and is_boundary(corrected, len(corrected) - s)):
        s -= 1
    # mutter only accepts a delete range that contains the cursor.
    lo, hi, cursor = capture.lo, capture.hi, capture.cursor
    a, b = min(lo + p, cursor), max(hi - s, cursor)
    replacement = corrected[a - lo:len(corrected) - (hi - b)]
    return Ops((a - cursor, b - a) if b > a else None, replacement or None, a, b, replacement)


def expected(capture, ops):
    """Text and cursor the client should report once ops are applied."""
    text = capture.text[:ops.start] + ops.replacement + capture.text[ops.end:]
    return Expected(text, ops.start + len(ops.replacement), ops.replacement)


def verify(expected, echo_text, echo_cursor):
    """'ok' when the echo shows the expected paragraph around the cursor, with the replacement right
    before it. Otherwise 'mismatch'. text-input-v3 re-centres its 4000-byte window on the new caret, so
    a paragraph that runs into the edge of a trimmed echo is compared only as far as it is visible."""
    if echo_text is None or not 0 <= echo_cursor <= len(echo_text):
        return 'mismatch'
    trimmed = len(echo_text.encode('utf-8', 'surrogatepass')) >= EDGE_BYTES
    lo, hi = paragraph_bounds(echo_text, echo_cursor)
    elo, ehi = paragraph_bounds(expected.text, expected.cursor)
    before, after = expected.text[elo:expected.cursor], expected.text[expected.cursor:ehi]
    left, right = echo_text[lo:echo_cursor], echo_text[echo_cursor:hi]
    cut_left, cut_right = trimmed and lo == 0, trimmed and hi == len(echo_text)
    if not (before.endswith(left) if cut_left else before == left):
        return 'mismatch'
    if not (after.startswith(right) if cut_right else after == right):
        return 'mismatch'
    shown = expected.replacement
    if cut_left and len(shown) > echo_cursor:
        shown = shown[len(shown) - echo_cursor:]
    if len(shown) > echo_cursor:
        return 'mismatch'
    return 'ok' if echo_text[echo_cursor - len(shown):echo_cursor] == shown else 'mismatch'
