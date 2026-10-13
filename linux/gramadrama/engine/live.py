"""Live checking, the engine's side (docs/live-check.md; pure: no gi, no core).

The engine knows the focused field's text from IBus (surrounding text), so it has nothing to poll: after
a change it sends the text to the live service (gramadrama/liveservice.py), and the issues that come back
go to the extension's badge. A click on a suggestion fixes that one word through the same checked path as
a correction (plan.replace_ops, then the echo verified); a click on the badge is Ctrl+Alt+G. Where the app
tells where its words are (AT-SPI, gramadrama/geometry.py), the service also sends their boxes, and the
extension underlines them; "Ignoruj", "Dodaj do słownika" and "Wyłącz regułę" in the underline card go back to the
service."""
from ..i18n import tr
import dataclasses
import itertools
import re

from . import plan
from .context import CONFIRMS, TERMINAL, ConfirmCaret

MAX_SHOWN = 5  # issues in the badge's list (macOS: the first five, then "i N więcej")
MAX_NOTE = 300  # characters of the service's note on its checking (the extension cuts it there too)
REACH = 200  # how far a word may have moved since it was checked
# One numbering for the whole engine process, as dictate.IDS: the one live service answers every engine
# object (input context), so a late answer about another object's field must never pass for this one's.
FIELDS = itertools.count(1)
# The underline card's actions (live:<verb>:<issue id>) that go to the live service as its commands, and the message the
# pill shows after them (fallback_ui.MESSAGES, seconds).
CARD_ACTIONS = ('live:ignore:', 'live:learn:', 'live:rule_off:')
CARD_MESSAGES = {'learn': ('learned', 2), 'rule_off': ('rule_off', 3)}


@dataclasses.dataclass(frozen=True)
class Check:
    field: int
    text: str
    caret: int  # None with a selection
    panel: bool = False  # the result edited in the correction panel: no model, no AT-SPI underlines


@dataclasses.dataclass(frozen=True)
class Clear:
    pass


@dataclasses.dataclass(frozen=True)
class Badge:
    view: dict  # {'v': 1, 'count': 0} hides it


@dataclasses.dataclass(frozen=True)
class Marks:
    view: dict  # the underlines (extension version 6); {'v': 1, 'marks': []} takes them away


@dataclasses.dataclass(frozen=True)
class Command:
    command: dict  # to the live service: 'ignore', 'learn' or 'rule_off', from the underline card


def summary(count):
    if count == 1:
        return tr('1 błąd')
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return tr('%d błędy') % count
    return tr('%d błędów') % count


def cut(text):
    """The IBus window may be a part of the field: text-input-v3 sends at most 4000 bytes around the caret."""
    return len(text.encode('utf-8', 'surrogatepass')) >= plan.EDGE_BYTES


def whole_words(text, lo, hi):
    """lo..hi of a cut window without the words at its edges: the window may start or end inside a word,
    even at the caret (GTK sends the field's first or last 4000 bytes, or 4000 around the caret), and a
    part of a word ("rzygotowaliśmy") is no word."""
    if lo == 0:
        match = re.search(r'\s', text[lo:hi])
        lo = lo + match.end() if match else hi
    if hi == len(text):
        match = re.search(r'\s(?=\S*$)', text[lo:hi])
        hi = lo + match.start() if match else lo
    return lo, hi


def locate(word, near, text):
    """The whole-word occurrence of word nearest to near (the earlier one on a tie), at most REACH away."""
    best = None
    for match in re.finditer(re.escape(word), text):
        a, b = match.span()
        if (a and text[a - 1].isalpha()) or (b < len(text) and text[b].isalpha()):
            continue
        if abs(a - near) <= REACH and (best is None or abs(a - near) < abs(best - near)):
            best = a
    return best


class LiveField:
    """What the live service knows of the focused field: the text sent (the IBus window, or the caret's
    paragraph when the window is cut) and where it starts in the window."""

    def __init__(self, ctx, fields=None):
        self.ctx = ctx
        self.fields = FIELDS if fields is None else fields
        self.field = 0  # a new number for every new text source: an older answer is ignored
        self.sent = None  # (focus generation, base, text, caret) last sent
        self.base = 0
        self.issues = []
        self.shown = True  # not known at first: another engine object's badge may be up
        self.marked = False  # underlines are drawn
        self.checking = False  # the service is working, including when there are no issues yet
        self.note = None  # Model only: the service's note on its checking (paused, sentences unchecked), with the issues
        self.asked = None  # the field's version AT-SPI was last asked about: once per doubted state
        self.confirming = None  # (ConfirmCaret id, field version) of that question

    def update(self, enabled, app_off, busy):
        """After a change of the field or of what may be checked. busy: a correction session or a
        dictation runs (the panel is the user's now)."""
        ctx = self.ctx
        if (not enabled or app_off or busy or ctx.client in (None, 'fake') or ctx.sensitive or ctx.purpose == TERMINAL
                or ctx.text is None or not ctx.fresh or ctx.awaiting_update):
            if busy or not enabled or app_off or ctx.sensitive or ctx.client in (None, 'fake') or ctx.purpose == TERMINAL:
                return self.clear()
            return self.stop_checking() + self._ask()  # keep issues while awaiting text, stop an old request's spinner
        text, base, end = ctx.text, 0, len(ctx.text)
        if cut(text):  # the caret's paragraph, without a word the window's edge may have cut
            base, end = whole_words(text, *plan.paragraph_bounds(text, ctx.cursor))
            if not text[base:end].strip():
                return self.clear()
            text = text[base:end]
        caret = ctx.cursor - base if ctx.cursor == ctx.anchor and base <= ctx.cursor <= end else None
        key = (ctx.focus_gen, base, text, caret)
        if key == self.sent:
            return []
        if self.sent is None or self.sent[0] != ctx.focus_gen or self.sent[1] != base:
            self.field = next(self.fields)  # another field, or the paragraph moved: the old issues mean nothing
            self.issues, self.note = [], None
        effects = self.stop_checking() if self.sent is None or self.sent[:3] != key[:3] else []
        self.sent, self.base = key, base
        return effects + [Check(self.field, text, caret)]

    def _ask(self):
        """Only the caret in doubt (GTK4 after a click, docs/architecture.md, Text safety): AT-SPI may tell where it is."""
        ctx = self.ctx
        if ctx.fresh or not ctx.confirmable() or self.asked == ctx.version:
            return []
        self.asked = ctx.version
        self.confirming = (next(CONFIRMS), ctx.version)
        return [ConfirmCaret(self.confirming[0], ctx.text)]

    def caret_confirmed(self, cid, cursor, anchor):
        """The live service's answer to this field's ConfirmCaret: True when the field is current again,
        and the adapter then updates the check."""
        if self.confirming is None or self.confirming[0] != cid:
            return False
        version, self.confirming = self.confirming[1], None
        return self.ctx.confirm(version, cursor, anchor) == 'ok'

    def clear(self):
        self.asked = self.confirming = None
        effects = []
        if self.sent is not None:
            effects.append(Clear())
        if self.shown:
            effects.append(Badge({'v': 1, 'count': 0}))
        if self.marked:
            effects.append(Marks({'v': 1, 'marks': []}))
        self.sent, self.issues, self.shown, self.marked = None, [], False, False
        self.checking, self.note = False, None
        return effects

    def stop_checking(self):
        """An edit invalidates the old in-flight status before the new service reply arrives."""
        if not self.checking:
            return []
        return self.result(self.field, self.issues, checking=False, note=self.note)

    def result(self, field, issues, checking=False, note=None):
        """The service's visible issues for the text it was sent. note: Model only, why checking finds nothing it may
        (the badge's "!" without issues, extension version 18)."""
        if field != self.field or self.sent is None:
            return []
        self.issues = [i for i in issues if isinstance(i, dict)]
        self.checking = checking is True and (self.ctx is None or (self.ctx.fresh and not self.ctx.awaiting_update))
        self.note = note[:MAX_NOTE] if isinstance(note, str) and note.strip() else None
        if not self.issues and not self.shown and not self.checking and not self.note:
            return []
        self.shown = bool(self.issues) or self.checking or bool(self.note)
        view = {'v': 1, 'count': len(self.issues), 'summary': summary(len(self.issues)) if self.issues else '',
                'issues': [{k: i.get(k) for k in ('id', 'word', 'suggestions', 'message', 'kind', 'source')}
                           for i in self.issues[:MAX_SHOWN]], 'more': max(0, len(self.issues) - MAX_SHOWN)}
        view['checking'] = self.checking
        if self.note:
            view['note'] = self.note
        return [Badge(view)]

    def marks(self, field, caret, length, view):
        """Where the issues' words are, from the service: for the text last sent only (the caret and the
        length tell it), so an underline never stands under a word that has moved."""
        if field != self.field or self.sent is None or caret != self.sent[3] or length != len(self.sent[2]):
            return []
        marks = view.get('marks') if isinstance(view, dict) else None
        if not marks:
            if not self.marked:
                return []
            self.marked = False
            return [Marks({'v': 1, 'marks': []})]
        self.marked = True
        out = {'v': 1, 'toolkit': view.get('toolkit'), 'caret': view.get('caret'), 'clip': view.get('clip'), 'marks': marks}
        if view.get('from'):
            out['from'] = view['from']  # where a browser's page counts from (engine/browser_live.py)
        return [Marks(out)]

    def command(self, verb, issue_id):
        """The service command for "Ignoruj" (verb 'ignore'), "Dodaj do słownika" ('learn') or "Wyłącz regułę"
        ('rule_off'), or None."""
        issue = next((i for i in self.issues if i.get('id') == issue_id), None)
        if issue is None or not isinstance(issue.get('word'), str) or not issue['word']:
            return None
        return {'cmd': verb, 'word': issue['word'], 'rule': str(issue.get('rule') or '')}

    def forget(self, issue_id):
        """A suggestion whose word is no longer where it was: the service drops it as after a fix ('fixed'),
        and its badge and underlines follow, so a click that cannot work is not offered again."""
        issue = next((i for i in self.issues if i.get('id') == issue_id), None)
        if issue is None:
            return []
        self.issues = [i for i in self.issues if i is not issue]
        return [Command({'cmd': 'fixed', 'field': self.field, 'start': int(issue.get('start') or 0),
                         'end': int(issue.get('end') or 0)})]

    def fix(self, issue_id, index):
        """(capture, corrected, data) for a click on suggestion index of issue_id, or (None, reason, data):
        the word is found again in the field as it is now, near where it was."""
        issue = next((i for i in self.issues if i.get('id') == issue_id), None)
        if issue is None or not 0 <= index < len(issue.get('suggestions') or []):
            return None, 'gone', {}
        data = {'source': issue.get('source'), 'rule': issue.get('rule')}
        snapshot = self.ctx.snapshot()
        if isinstance(snapshot, plan.Refusal):
            return None, snapshot.reason, data
        if snapshot.cursor != snapshot.anchor:
            return None, 'selection', data  # a commit would replace the selection
        word, suggestion = str(issue.get('word') or ''), str(issue['suggestions'][index])
        start = locate(word, self.base + int(issue.get('start') or 0), snapshot.text) if word else None
        if start is None:
            return None, 'word_gone', data
        end = start + len(word)
        if cut(snapshot.text) and whole_words(snapshot.text, start, end) != (start, end):
            return None, 'truncated', data  # at the window's edge: the word may go on beyond it
        # The range must hold the caret (mutter deletes only around it): from the word to the caret,
        # the text between committed again unchanged, as a paragraph correction does.
        lo, hi = min(start, snapshot.cursor), max(end, snapshot.cursor)
        source = snapshot.text[lo:hi]
        corrected = source[:start - lo] + suggestion + source[end - lo:]
        capture = plan.Capture('paragraph', lo, hi, source, snapshot.text, snapshot.cursor, snapshot.anchor, snapshot.client)
        return capture, corrected, data
