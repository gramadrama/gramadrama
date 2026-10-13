"""Live checking in the pages of a browser, through its extension (docs/browser.md; pure: no gi, no core).

The engine knows the text of a field from IBus (engine/live.py); in a browser that is unreliable (X11 gives none, Wayland a
window of it), and there are no word positions from the accessibility tree. The extension is the page's interface: when asked
it gives the whole text of the focused field (`live`) and where its words are (`rects`) and replaces a word (`replace`). So when
the focused window is a browser whose extension is connected, this module is the text source instead of IBus, as BrowserChannel
and LiveMonitor are on the Mac:

- every POLL seconds `live`: the text and caret of the field become a Check for the live service, as engine/live.py makes them
  (the same issues, the same badge); the answers of the service come back to issues();
- when there are issues, `rects` for their words: the boxes are in the page's CSS pixels, the underlines of the GNOME Shell
  extension are in the window's, so every box becomes origin + zoom × box (page_origin: where the page starts in the window,
  from the pointer — the page tells the same position in its own units and in the window's — or from the browser's chrome);
  the Shell draws them from the window's client rect (FROM);
- the badge goes inside the field's bottom right corner, as on the Mac: its view carries the field's visible box, in the same
  coordinates, and is sent again when the field moves in the window (scrolled, the window resized);
- a click on a suggestion: `replace` in the field the text came from, checked by the extension against the word as it is now.

It has no clock and no loop: the glue (main.py BrowserLiveLink) calls tick() every POLL seconds with what the engine knows, and
passes the hub's answers back through the callbacks it gave. `out` is that glue: text(field, text, caret), clear_service(),
fixed(field, start, end), command(dict), badge(view), marks(view), log(event, **data), message(reason, seconds), sites()."""
import json

from . import live as L, plan

POLL = 0.35  # seconds between two reads of the field
REQUEST_SECONDS = 1.5  # an answer of the extension later than that is no answer
MAX_RECTS = 60  # words underlined at most (the Shell takes as many)
# Where the Shell counts the boxes from: the window's client rect, where Chrome's own coordinates start. That is not the frame
# (the window as one sees it): a window that is not maximized has Chrome's shadow around it, 12 px on the left and 10 above
# on Wayland, 22 and 19 on X11, and on X11 with the system title bar the page counts from under the window manager's frame.
FROM = 'client'
# For a Shell before extension 8, which knows no `from`: a GTK 4 window counts from its frame, right for a maximized window.
TOOLKIT = ['GTK', '4.0']


def u16(text):
    """The length of text in UTF-16 units, which is how the page counts (JavaScript strings)."""
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def _numbers(value, length):
    return isinstance(value, list) and len(value) == length and all(isinstance(v, (int, float)) for v in value)


def _origin(mouse, win, inner, zoom):
    """Where the top frame's page starts in the window: from the pointer over it (exact, also with a side bar), else from the
    browser's chrome, which is what the window has more than the page (outer minus inner, the page's size in DIP)."""
    if _numbers(mouse, 4):
        return mouse[0] - mouse[2] * zoom, mouse[1] - mouse[3] * zoom
    if _numbers(win, 6) and _numbers(inner, 2):
        return (win[2] - inner[0] * zoom) / 2, win[3] - inner[1] * zoom
    return None


def page_origin(geometry, zoom):
    """Where the page's (0, 0) is in the window, in the window's units (DIP), or None when it cannot be told. geometry is what the
    extension gives: {"mouse": [x, y, clientX, clientY] or null, "win": [screenX, screenY, outerW, outerH, innerW, innerH],
    "top": bool, "frame": {"origin": [x, y], "inner": [w, h], "mouse": … or null}}. mouse: the last position of the pointer over
    that frame, in the window (screen minus the window's own position) and in the page. A frame inside the page also tells
    where it starts in the top frame (frame.origin, in CSS pixels, asked of its parents) with the top frame's size and pointer:
    that holds without any pointer over the inner frame, and when the top page scrolls after the pointer was there."""
    if not isinstance(geometry, dict):
        return None
    z = zoom if isinstance(zoom, (int, float)) and zoom > 0 else 1.0
    win, frame = geometry.get('win'), geometry.get('frame')
    if not geometry.get('top') and isinstance(frame, dict) and _numbers(frame.get('origin'), 2):
        top = _origin(frame.get('mouse'), win, frame.get('inner'), z)
        if top is not None:
            return top[0] + frame['origin'][0] * z, top[1] + frame['origin'][1] * z
    if _numbers(geometry.get('mouse'), 4):
        return _origin(geometry['mouse'], None, None, z)
    if geometry.get('top') and _numbers(win, 6):
        return _origin(None, win, win[4:], z)
    return None


def scaled(box, origin, zoom):
    x, y = origin
    return [round(x + zoom * box[0], 1), round(y + zoom * box[1], 1), round(zoom * box[2], 1), round(zoom * box[3], 1)]


def field_box(answer):
    """Where the field is in the window (its visible part, whole pixels), from the extension's answer to `live`; None when the
    page cannot tell."""
    field, zoom = answer.get('field'), answer.get('zoom')
    zoom = zoom if isinstance(zoom, (int, float)) and zoom > 0 else 1.0
    origin = page_origin(answer.get('geometry'), zoom)
    if origin is None or not (isinstance(field, list) and len(field) == 4 and all(isinstance(v, (int, float)) for v in field)):
        return None
    return [round(v) for v in scaled(field, origin, zoom)]


class PageField(L.LiveField):
    """The field in the page: LiveField's issues, badge and marks, with the text from the extension instead of IBus."""

    def __init__(self, fields=None):
        super().__init__(None, fields)

    def update_page(self, client, text, caret, base):
        """text is what the live service gets (the field, or its caret's paragraph from base); client the extension's token of
        the field: another field means another number, so that an older answer is ignored."""
        key = (client, base, text, caret)
        if key == self.sent:
            return []
        if self.sent is None or self.sent[0] != client or self.sent[1] != base:
            self.field = next(self.fields)
            self.issues = []
        effects = self.stop_checking() if self.sent is None or self.sent[:3] != key[:3] else []
        self.sent, self.base = key, base
        return effects + [L.Check(self.field, text, caret)]


class BrowserLive:
    def __init__(self, hub, out):
        self.hub, self.out = hub, out
        self.field = PageField()
        self.field.shown = False  # nothing of ours is on screen: a clear must not hide the badge of another source (IBus)
        self.zoom = 1.0  # DIP per CSS pixel of the page last read (Chrome's zoom factor)
        self.browser = None  # the hub's name of the browser whose page is read
        self.pending = {'live': False, 'rects': False}
        self.client = None  # the extension's token of the field
        self.window = ''  # the text as the extension gave it (a window around the caret)
        self.window_start = 0  # where it starts in the field, in UTF-16 units
        self.marks_sent = None  # the JSON of the last marks view
        self.field_box = None  # where the field is in the window, from the last read (field_box)
        self.badge_view = None  # the live service's last badge view, and the field's box it was sent with
        self.badge_box = None
        self.reading = 0  # numbers every read: an answer of an older one is dropped

    # ---- what the engine tells ----
    @property
    def active(self):
        return self.browser is not None and self.field.sent is not None

    def tick(self, browser, enabled, off, busy):
        """Every POLL seconds. browser: the hub's name of the browser whose window has the focus (None: another app, or no extension
        there); enabled: live checking is on; off: not in this app; busy: a correction or a dictation is going on."""
        if browser is None or not enabled or off or busy:
            self.stop()
            return
        if browser != self.browser:
            self.stop()
            self.browser = browser
        if not self.pending['live']:
            self.pending['live'] = True
            self.reading += 1
            reading = self.reading
            if self.hub.request(browser, 'live', lambda answer: self._read(reading, browser, answer), REQUEST_SECONDS) is None:
                self.pending['live'] = False
                self.stop()

    def stop(self):
        """Nothing to check any more: the badge and underlines go, the service forgets the text."""
        self.reading += 1
        self.pending = {'live': False, 'rects': False}
        self.client = None
        self.marks_sent = None
        self.browser = None
        self.field_box = None
        self._run(self.field.clear())

    # ---- the answers of the extension ----
    def _read(self, reading, browser, answer):
        self.pending['live'] = False
        if reading != self.reading or browser != self.browser:
            return
        if not answer.get('ok'):
            if answer.get('reason') in ('timeout', 'disconnected'):
                return  # nothing known: what is shown stays until the next read
            self.client = None
            self._run(self.field.clear())  # no field in the page has the focus, or one that is not checked
            self.marks_sent = None
            return
        if answer.get('site') in self.out.sites() or answer.get('selectionTooLong') or answer.get('selected') is None:
            self.client = None
            self._run(self.field.clear())
            self.marks_sent = None
            return
        before, selected, after = answer.get('before') or '', answer.get('selected') or '', answer.get('after') or ''
        text = before + selected + after
        caret = len(before) if not selected else None
        self.client = answer.get('client')
        self.window, self.window_start = text, int(answer.get('windowStart') or 0)
        self.zoom = answer.get('zoom') if isinstance(answer.get('zoom'), (int, float)) else 1.0
        self.field_box = field_box(answer)
        base, end = 0, len(text)
        # A window of a longer field, or a long text, is checked from the caret's paragraph, as engine/live.py does for IBus.
        if self.window_start > 0 or not answer.get('shortAfter', True) or L.cut(text):
            base, end = L.whole_words(text, *plan.paragraph_bounds(text, len(before)))
            if not text[base:end].strip():
                self._run(self.field.clear())
                return
        sent = text[base:end]
        sent_caret = caret - base if caret is not None and base <= caret <= end else None
        self._run(self.field.update_page(self.client, sent, sent_caret, base))
        if self.field.issues or self.field.checking:
            self._badge_moved()
        if self.field.issues:
            self._ask_rects()

    def issues(self, field, issues, checking=False, note=None):
        """The live service's visible issues for the text it was sent, and its note (live.LiveField.result)."""
        self._run(self.field.result(field, issues, checking, note))
        if self.field.issues:
            self._ask_rects()

    def _ask_rects(self):
        if self.pending['rects'] or self.client is None or self.browser is None or self.field.sent is None:
            return
        ranges, wanted = [], []
        for issue in self.field.issues[:MAX_RECTS]:
            start, end = issue.get('start'), issue.get('end')
            if not (isinstance(start, int) and isinstance(end, int)):
                continue
            a, b = self.field.base + start, self.field.base + end
            if 0 <= a < b <= len(self.window):
                ranges.append([self.window_start + u16(self.window[:a]), u16(self.window[a:b])])
                wanted.append(issue)
        if not ranges:
            self._marks_off()
            return
        self.pending['rects'] = True
        reading, sent = self.reading, self.field.sent
        asked = self.hub.request(self.browser, 'rects', lambda answer: self._rects(reading, sent, wanted, answer),
                                 REQUEST_SECONDS, client=self.client, ranges=ranges)
        if asked is None:
            self.pending['rects'] = False

    def _rects(self, reading, sent, issues, answer):
        self.pending['rects'] = False
        if reading != self.reading or sent != self.field.sent or not answer.get('ok'):
            return  # the field changed meanwhile: the next read asks again
        origin = page_origin(answer.get('geometry'), answer.get('zoom', self.zoom))
        words = answer.get('words')
        if origin is None or not isinstance(words, list) or len(words) != len(issues):
            self._marks_off()
            return
        zoom = answer.get('zoom') if isinstance(answer.get('zoom'), (int, float)) and answer.get('zoom') > 0 else 1.0
        marks = []
        for issue, boxes in zip(issues, words):
            boxes = [scaled(b, origin, zoom) for b in boxes if isinstance(b, list) and len(b) == 4]
            if boxes:
                marks.append({'id': issue.get('id'), 'rects': boxes, 'word': issue.get('word'), 'message': issue.get('message'),
                              'suggestions': issue.get('suggestions'), 'kind': issue.get('kind'), 'source': issue.get('source'),
                              'learn': issue.get('kind') == 'spelling', 'rule_off': issue.get('rule_off') is True})
        field = answer.get('field')
        clip = scaled(field, origin, zoom) if isinstance(field, list) and len(field) == 4 else None
        view = {'toolkit': TOOLKIT, 'from': FROM, 'caret': None, 'clip': clip, 'marks': marks}
        key = json.dumps(view, sort_keys=True)
        if key == self.marks_sent:
            return
        effects = self.field.marks(self.field.field, sent[3], len(sent[2]), view)
        if effects or not marks:
            self.marks_sent = key
            self._run(effects)

    def _marks_off(self):
        if self.marks_sent is not None:
            self.marks_sent = None
            self._run(self.field.marks(self.field.field, self.field.sent[3], len(self.field.sent[2]), {'marks': []})
                      if self.field.sent else [])

    # ---- the clicks on the badge's list and the underline card ----
    def handles(self, name):
        """Whether an action of the Shell (live:fix:…, live:ignore:…, live:learn:…, live:rule_off:…) is this module's: it is
        when it has the field."""
        return self.active and name.startswith(('live:fix:',) + L.CARD_ACTIONS)

    def action(self, name):
        if name.startswith(L.CARD_ACTIONS):
            verb, issue_id = name[len('live:'):].split(':', 1)
            command = self.field.command(verb, issue_id)
            if command is not None:
                self.out.command(command)
                self.out.log('live_' + verb, rule=command['rule'])  # never the word
                if verb in L.CARD_MESSAGES:
                    self.out.message(*L.CARD_MESSAGES[verb])
            return
        issue_id, _, index = name[len('live:fix:'):].rpartition(':')
        if index.isdigit():
            self.fix(issue_id, int(index))

    def fix(self, issue_id, index):
        issue = next((i for i in self.field.issues if i.get('id') == issue_id), None)
        if issue is None or not 0 <= index < len(issue.get('suggestions') or []) or self.client is None:
            return self._fix_failed('gone', {})
        data = {'source': issue.get('source'), 'rule': issue.get('rule')}
        word, suggestion = str(issue.get('word') or ''), str(issue['suggestions'][index])
        start = L.locate(word, self.field.base + int(issue.get('start') or 0), self.window) if word else None
        if start is None:
            return self._fix_failed('word_gone', data, issue_id)
        location, length = self.window_start + u16(self.window[:start]), u16(word)
        field, a, b = self.field.field, int(issue.get('start') or 0), int(issue.get('end') or 0)

        def done(answer):
            if answer.get('ok'):
                self.out.log('live_fix', outcome='sent', via='extension', **{k: v for k, v in data.items() if v is not None})
                self.out.fixed(field, a, b)
                self.marks_sent = None
            else:
                self._fix_failed(str(answer.get('reason') or 'failed'), data, issue_id)
        asked = self.hub.request(self.browser, 'replace', done, 2.0, client=self.client,
                                 check={'location': location, 'length': length, 'text': word},
                                 edits=[{'location': location, 'length': length, 'text': suggestion}], expected=suggestion)
        if asked is None:
            self._fix_failed('no_extension', data)

    def _fix_failed(self, reason, data, issue_id=None):
        """A refused click; when its word is no longer where it was (not found, or the page's check of it failed), the
        suggestion leaves the badge, as through IBus (LiveField.forget)."""
        if issue_id is not None and reason in ('word_gone', 'changed'):
            self._run(self.field.forget(issue_id))
        self.out.log('live_fix', outcome=reason, via='extension', **{k: v for k, v in data.items() if v is not None})
        self.out.message('fix_failed', 3)

    # ---- the badge ----
    def _badge(self, view):
        """The live service's badge, with where the field is when the page tells it: the Shell puts the badge inside the
        field's bottom right corner, as the Mac does, instead of at the window's right edge."""
        self.badge_view = view
        self.badge_box = self.field_box if view.get('count') or view.get('checking') else None
        if self.badge_box is not None:
            view = dict(view, field=self.badge_box)
            view['from'] = FROM
        self.out.badge(view)

    def _badge_moved(self):
        """The field moved in the window (the page scrolled, the window was resized): the badge goes with it."""
        if (self.badge_view is not None and (self.badge_view.get('count') or self.badge_view.get('checking'))
                and self.field_box != self.badge_box):
            self._badge(self.badge_view)

    # ---- effects ----
    def _run(self, effects):
        for effect in effects:
            kind = type(effect)
            if kind is L.Check:
                self.out.text(effect.field, effect.text, effect.caret)
            elif kind is L.Clear:
                self.out.clear_service()
            elif kind is L.Badge:
                self._badge(effect.view)
            elif kind is L.Marks:
                self.out.marks(effect.view)
            elif kind is L.Command:
                self.out.command(effect.command)

    def status(self):
        return {'browser': self.browser, 'issues': len(self.field.issues), 'text': self.field.sent is not None}
