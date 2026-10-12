"""Ctrl+Alt+G and dictation in the pages of a browser, through its extension (docs/browser.md; pure: no gi, no core).

In a browser whose extension is connected the extension is the text channel, as BrowserChannel is on the Mac
(TextBridge.captureViaBrowser, replaceViaBrowser, insertViaBrowser): `context` gives the whole field around the caret
(IBus gives none on X11 and a window of it on Wayland), `replace` writes the correction word by word, checked against the
text as it is in the field then, and `insert` puts dictated text at the caret. When the extension has no field (the address
bar, a page of the browser itself) or does not answer, the engine goes the IBus way as before.

Ranges are in UTF-16 units, as the page counts. What is decided here is the Mac's: the shared cases in tests/ime_cases.json
(ImeScope.candidates and ImeEdits.plan), with the Linux rule that Ctrl+Alt+G takes the selection or the caret's paragraph."""
import dataclasses
import difflib
import re

from .context import Refusal
from .plan import MAX_CHARS, split_lines, paragraph_bounds

# Seconds: how long an answer is waited for, and the deadline the extension gets for a write (it gives up writing before
# the engine stops waiting, so a late write never surprises anyone). The Mac's BrowserChannel values.
CONTEXT_SECONDS = 0.8
WRITE_SECONDS = 2.5
WRITE_DEADLINE = 1.2
MAX_EDITS = 12  # more word changes than that go in as one span, from the first change to the last (ImeEdits.plan)
TOKEN = re.compile(r"[\w'’]+|\s+|[^\w\s]")  # core.TOKEN: words, runs of whitespace, single other characters
OBJECT = '￼'
# Answers that say nothing was written; any other failure of a write may have written (the Mac's ImeChannel.classify).
REFUSED = ('changed', 'client_changed', 'no_client', 'secure_input', 'not_text', 'bad_request', 'expired', 'exec_failed')
# The engine's own: no extension to ask, so nothing was sent. The hub's 'timeout' and 'disconnected' are no refusal: a write
# may have landed in the field before its answer was lost.
NOT_SENT = ('no_extension',)
# The page cannot say what to correct: the hotkey goes the IBus way. Never shown, so it has no message (fallback_ui).
FALLBACK = Refusal(reason='fallback')


@dataclasses.dataclass(frozen=True)
class BrowserRequest:
    """Effect: ask the extension of `browser`, then give the answer to EngineLogic.browser_answer(kind, id, answer).
    kind names who waits ('hotkey', 'apply', 'dictation', 'insert'); write: the extension's deadline for a write."""
    kind: str
    id: int
    browser: str
    op: str
    fields: dict
    seconds: float = CONTEXT_SECONDS
    write: float = None


@dataclasses.dataclass(frozen=True)
class Capture:
    """What Ctrl+Alt+G took from a page: the scope and text as plan.Capture has them, and where the text is in the field."""
    scope: str  # 'selection' or 'paragraph'
    source: str
    location: int  # UTF-16, in the field
    length: int
    client: str  # the extension's token of the field: the correction goes back only there
    browser: str


def u16(text):
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def capture(answer, browser):
    """The extension's answer to `context` as what Ctrl+Alt+G corrects: Capture, or Refusal(reason). FALLBACK when
    the page cannot say (no field, a window cut where the paragraph needs it, an answer that does not add up): the IBus way."""
    if not answer.get('ok'):
        return Refusal('sensitive' if answer.get('reason') == 'secure_input' else 'fallback')
    start, location, length = answer.get('windowStart'), answer.get('selectionLocation'), answer.get('selectionLength')
    client = answer.get('client')
    if not all(isinstance(v, int) and v >= 0 for v in (start, location, length)) or not isinstance(client, str):
        return FALLBACK
    if answer.get('selectionTooLong'):
        return Refusal('too_long')
    before, selected, after = (answer.get(k) or '' for k in ('before', 'selected', 'after'))
    if not all(isinstance(v, str) for v in (before, selected, after)) or start + u16(before) != location:
        return FALLBACK
    if length:
        if u16(selected) != length or not selected.strip():
            return FALLBACK
        if len(selected) > MAX_CHARS:
            return Refusal('too_long')
        if OBJECT in selected:
            return Refusal('embedded')
        return Capture('selection', selected, location, length, client, browser)
    window = before + after
    if not window.strip():
        return FALLBACK  # an empty field: the Mac's "noText", the usual way says what is wrong
    lo, hi = paragraph_bounds(window, len(before))
    source = window[lo:hi]
    if not source.strip():
        return Refusal('empty')
    # The paragraph is whole only when its ends are line ends in the window or the field's own ends.
    if (lo == 0 and start > 0) or (hi == len(window) and not answer.get('shortAfter')):
        return FALLBACK
    if len(source) > MAX_CHARS:
        return Refusal('too_long')
    if OBJECT in source:
        return Refusal('embedded')
    return Capture('paragraph', source, start + u16(window[:lo]), u16(source), client, browser)


def _map_line_ends(original, corrected):
    """ImeEdits.mapSeparators: the line ends of original, in order, in corrected; None when the line count differs."""
    wanted = split_lines(original)[1]
    lines, found = split_lines(corrected)
    if wanted == found:
        return corrected
    if len(wanted) != len(found):
        return None
    return ''.join(line + (wanted[i] if i < len(wanted) else '') for i, line in enumerate(lines))


def _word_hunks(original, corrected):
    """(location, length, text) of every changed run of words, in code points of original, from the start."""
    left, right = TOKEN.findall(original), TOKEN.findall(corrected)
    hunks, at = [], 0
    for tag, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        old = ''.join(left[a:b])
        if tag != 'equal':
            hunks.append((at, len(old), ''.join(right[c:d])))
        at += len(old)
    return hunks


def _apply(hunks, text):
    for location, length, new in sorted(hunks, reverse=True):
        text = text[:location] + new + text[location + length:]
    return text


def _span(original, corrected):
    p = 0
    while p < min(len(original), len(corrected)) and original[p] == corrected[p]:
        p += 1
    s = 0
    while s < min(len(original), len(corrected)) - p and original[-1 - s] == corrected[-1 - s]:
        s += 1
    return [(p, len(original) - p - s, corrected[p:len(corrected) - s])]


def edits(original, corrected, base):
    """ImeEdits.plan: the replacements that turn original (at base in the field) into corrected, word by word, from the end
    of the text back (each one leaves the places of the ones before it). (edits, expected), None when nothing changes,
    Refusal('line_structure') when the model changed the line count of a text with line ends: the extension writes a
    paragraph only within one line (a rich field refuses a replacement across a line end). Short replacements keep links,
    mentions and formatting between the corrections as they were."""
    mapped = _map_line_ends(original, corrected)
    if mapped is None:
        return Refusal('line_structure')
    if mapped == original:
        return None
    hunks = _word_hunks(original, mapped)
    if len(hunks) > MAX_EDITS or _apply(hunks, original) != mapped:
        hunks = _span(original, mapped)
    out = []
    for location, length, new in sorted(hunks, reverse=True):
        out.append({'location': base + u16(original[:location]), 'length': u16(original[location:location + length]),
                    'text': new})
    return out, mapped


def outcome(answer):
    """'landed', 'refused' (nothing was written) or 'unknown' (it may have been), as the Mac's ImeChannel.classify."""
    if answer.get('ok'):
        return 'landed'
    if answer.get('attempted') is False and (answer.get('reason') in REFUSED or answer.get('reason') in NOT_SENT):
        return 'refused'
    return 'unknown'


def previous_char(answer):
    """The character right before the caret in a `context` answer, for the space before dictated text (None: none, or a
    selection that the text replaces)."""
    before = answer.get('before') or ''
    if answer.get('selectionLength') or not isinstance(before, str) or not before:
        return None
    return before[-1]
