"""Where the focused field's words are, from accessibility (AT-SPI), for the live check's underlines
(docs/live-check.md, research/2026-09-28-atspi). IBus gives the caret's rectangle only; AT-SPI gives
the box of every character, in the app's own window coordinates:

- GTK4 counts from the window's frame, and its range extents are the FIRST LETTER only, so a word's
  box is its first letter's joined to its last letter's;
- Chromium counts from the page, and has whole ranges (with ACCESSIBILITY_ENABLED=1 and
  --force-renderer-accessibility only).

The extension places them: GTK4 from the frame, anything else by the caret, whose screen rectangle it
knows from IBus and whose box comes with the marks.

Runs in the live service, in one thread: libatspi is not thread-safe, and every call is a synchronous
D-Bus call into the app, which must never block GNOME Shell. Nothing read here is logged."""
import re

WORD = re.compile(r'\S+')
EMBEDDED = '￼'  # a child object in the text (a paragraph of a rich editor, a link)
MAX_WALK = 4000  # accessibles visited to find the focused field (GTK4 has no Collection)
MAX_DEPTH = 60
MAX_MARKS = 60  # words underlined at most
BLOCK_ROLES = frozenset(('paragraph', 'heading', 'section', 'list item', 'block quote', 'article', 'form',
                         'panel', 'list', 'table', 'table cell', 'footer', 'header'))


def words(text, start, end):
    """(a, b) of every run without whitespace in text[start:end], as macOS TextGeometry.words: an issue
    over a line break gives a box per word, never one over two lines."""
    return [(start + m.start(), start + m.end()) for m in WORD.finditer(text[start:end])]


def align(full, full_caret, sent, sent_caret):
    """Where sent (the text the live service checks: the IBus window or its paragraph) starts in full
    (the field's text as AT-SPI has it), or None when they are not the same field: by the carets when
    both are known, else the one occurrence."""
    if not sent:
        return None
    if sent_caret is not None and full_caret is not None:
        start = full_caret - sent_caret
        return start if start >= 0 and full[start:start + len(sent)] == sent else None
    first = full.find(sent)
    if first < 0 or full.find(sent, first + 1) >= 0:
        return None
    return first


def union(rects):
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    return [x0, y0, x1 - x0, y1 - y0]


def same_line(a, b):
    """The two boxes share a line: they overlap by at least half of the lower one's height."""
    overlap = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return overlap >= min(a[3], b[3]) / 2


def usable(rect):
    """A box a word can have: some width, a text line's height (Chromium answers zeros for text it has
    not laid out)."""
    return rect is not None and rect[2] > 0 and 4 < rect[3] < 120


def word_box(first, last, whole):
    """The box of a word from its first and last letters' boxes and the range's (any may be None). A
    word that wraps (last letter on another line) keeps its first line."""
    boxes = [b for b in (first, whole) if usable(b)]
    if not boxes:
        return None
    if usable(last) and same_line(boxes[0], last):
        boxes.append(last)
    return union(boxes)


def caret_box(before, after):
    """The caret as a zero-width box: at the left edge of the letter after it, or the right edge of the
    one before it (the end of the text)."""
    if usable(after):
        return [after[0], after[1], 0, after[3]]
    if usable(before):
        return [before[0] + before[2], before[1], 0, before[3]]
    return None


class Segment:
    """A piece of the field's text that one accessible holds: [start, start + len(text)) of the whole."""

    def __init__(self, acc, start, text):
        self.acc, self.start, self.text = acc, start, text

    @property
    def end(self):
        return self.start + len(self.text)


def join(root_text, children):
    """The field's text and its segments for a rich editor, whose root holds an EMBEDDED character per
    child: the children's texts in their places, a line break between block children (a paragraph
    each), as the app gives IBus. children: [(acc, role, text)] in order. Returns (text, [(child index,
    start)])."""
    out, starts, child = [], [], 0
    previous_block = False
    for ch in root_text:
        if ch != EMBEDDED:
            out.append(ch)
            previous_block = False
            continue
        if child >= len(children):
            return None
        _acc, role, text = children[child]
        block = role in BLOCK_ROLES
        if block and previous_block:
            out.append('\n')
        starts.append((child, sum(len(x) for x in out)))
        out.append(text)
        previous_block = block
        child += 1
    return ''.join(out), starts


class Atspi:
    """The AT-SPI calls, in one place: the geometry above stays testable with fakes."""

    def __init__(self):
        import gi
        gi.require_version('Atspi', '2.0')
        from gi.repository import Atspi as A
        self.A = A
        if A.init() not in (0, 1):  # 1: already initialized
            raise RuntimeError('AT-SPI is not available')
        A.set_timeout(250, -1)  # a busy app must not hold the underlines for long

    def rect(self, r):
        return None if r is None else [r.x, r.y, r.width, r.height]

    def state(self, acc, name):
        try:
            return acc.get_state_set().contains(getattr(self.A.StateType, name))
        except Exception:
            return False

    def active_windows(self):
        desktop = self.A.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            try:
                n = app.get_child_count()
            except Exception:
                continue
            for j in range(n):
                window = app.get_child_at_index(j)
                if window is not None and self.state(window, 'ACTIVE'):
                    yield app, window

    def focused_in(self, window):
        """The focused accessible with text in window: the Collection interface where the app has it
        (Chromium), else a walk over the showing children (GTK4)."""
        A = self.A
        try:
            collection = window.get_collection_iface()
        except Exception:
            collection = None
        if collection is not None:
            rule = A.MatchRule.new(A.StateSet.new([A.StateType.FOCUSED]), A.CollectionMatchType.ALL, {},
                                   A.CollectionMatchType.ALL, [], A.CollectionMatchType.ALL, [],
                                   A.CollectionMatchType.ALL, False)
            try:
                for match in collection.get_matches(rule, A.CollectionSortOrder.CANONICAL, 5, True):
                    if match.get_text_iface() is not None:
                        return match
            except Exception:
                pass
        seen = [0]

        def walk(acc, depth):
            seen[0] += 1
            if seen[0] > MAX_WALK or depth > MAX_DEPTH:
                return None
            if self.state(acc, 'FOCUSED') and acc.get_text_iface() is not None:
                return acc
            try:
                n = acc.get_child_count()
            except Exception:
                return None
            for i in range(n):
                try:
                    child = acc.get_child_at_index(i)
                except Exception:
                    continue
                if child is None or not (self.state(child, 'SHOWING') or self.state(child, 'FOCUSED')):
                    continue
                found = walk(child, depth + 1)
                if found is not None:
                    return found
            return None
        return walk(window, 0)

    def still_focused(self, acc):
        return self.state(acc, 'FOCUSED') and self.state(acc, 'SHOWING')

    def text(self, acc):
        n = self.A.Text.get_character_count(acc)
        return self.A.Text.get_text(acc, 0, n) if n > 0 else ''

    def caret(self, acc):
        return self.A.Text.get_caret_offset(acc)

    def selection(self, acc):
        """(start, end) of the first selection, None without one."""
        if self.A.Text.get_n_selections(acc) < 1:
            return None
        r = self.A.Text.get_selection(acc, 0)
        return None if r.start_offset == r.end_offset else (r.start_offset, r.end_offset)

    def children(self, acc):
        out = []
        for i in range(acc.get_child_count()):
            child = acc.get_child_at_index(i)
            if child is None or child.get_text_iface() is None:
                out.append((None, '', ''))
                continue
            out.append((child, child.get_role_name(), self.text(child)))
        return out

    def char_box(self, acc, offset):
        return self.rect(self.A.Text.get_character_extents(acc, offset, self.A.CoordType.WINDOW))

    def range_box(self, acc, start, end):
        return self.rect(self.A.Text.get_range_extents(acc, start, end, self.A.CoordType.WINDOW))

    def extents(self, acc):
        try:
            return self.rect(self.A.Component.get_extents(acc, self.A.CoordType.WINDOW))
        except Exception:
            return None

    def toolkit(self, acc):
        try:
            app = acc.get_application()
            return app.get_toolkit_name() or '', app.get_toolkit_version() or ''
        except Exception:
            return '', ''


class Geometry:
    """The focused field as AT-SPI has it, kept between calls (finding it again costs a walk)."""

    def __init__(self, api=None):
        self.api = api if api is not None else Atspi()
        self.field = None  # the focused accessible
        self.window = None

    def find(self):
        api = self.api
        if self.field is not None and api.still_focused(self.field):
            return self.field
        self.field = self.window = None
        for _app, window in api.active_windows():
            found = api.focused_in(window)
            if found is not None:
                self.field, self.window = found, window
                return found
        return None

    def read(self, acc):
        """(text, caret, [Segment]) of the field: its own text, or its children's for a rich editor."""
        api = self.api
        text, caret = api.text(acc), api.caret(acc)
        if EMBEDDED not in text:
            return text, caret, [Segment(acc, 0, text)]
        children = api.children(acc)
        joined = join(text, children)
        if joined is None:
            return None
        full, starts = joined
        segments = [Segment(children[i][0], start, children[i][2]) for i, start in starts if children[i][0] is not None]
        full_caret = None
        for segment in segments:
            local = api.caret(segment.acc)
            if local is not None and local >= 0:
                full_caret = segment.start + local
                break
        if full_caret is None and 0 <= caret <= len(text):  # between two children: after the previous one
            before = text[:caret].count(EMBEDDED)
            done = [s for i, s in enumerate(segments) if i < before]
            full_caret = done[-1].end if done else 0
        return full, full_caret, segments

    def caret(self, sent):
        """(cursor, anchor) in sent's offsets where the focused field's caret and selection are now, or None
        when the field cannot be read, sent is not exactly once in it, or the caret is outside sent. The
        engine trusts its IBus state when this is the same (GTK4 after a click, docs/architecture.md, Text safety)."""
        api = self.api
        acc = self.find()
        if acc is None:
            return None
        text = api.text(acc)
        if EMBEDDED in text:  # a rich editor: a caret per paragraph, never compared here
            return None
        base = align(text, None, sent, None)
        if base is None:
            self.field = None
            return None
        caret, selection = api.caret(acc), api.selection(acc)
        if caret is None or not base <= caret <= base + len(sent):
            return None
        anchor = caret
        if selection is not None:
            start, end = selection
            if caret not in (start, end) or start < base or end > base + len(sent):
                return None
            anchor = end if caret == start else start
        return caret - base, anchor - base

    def marks(self, sent, sent_caret, issues):
        """The boxes of the issues' words for the text the live service checks, or None when the focused
        field cannot be read or is not that text. issues: [{'id', 'start', 'end'}] in sent's offsets.
        Returns {'toolkit': [name, version], 'caret': box or None, 'clip': box or None,
                 'marks': [{'id', 'rects'}]} in the app's window coordinates."""
        api = self.api
        acc = self.find()
        if acc is None:
            return None
        read = self.read(acc)
        if read is None:
            return None
        full, full_caret, segments = read
        base = align(full, full_caret, sent, sent_caret)
        if base is None:
            self.field = None  # another field, or not current yet: look again next time
            return None
        marks, count = [], 0
        for issue in issues:
            rects = []
            for a, b in words(full, base + issue['start'], base + issue['end']):
                segment = next((s for s in segments if s.start <= a and b <= s.end), None)
                if segment is None or count >= MAX_MARKS:
                    continue
                la, lb = a - segment.start, b - segment.start
                box = word_box(api.char_box(segment.acc, la), api.char_box(segment.acc, lb - 1),
                               api.range_box(segment.acc, la, lb))
                if box is not None:
                    rects.append(box)
                    count += 1
            if rects:
                marks.append({'id': issue['id'], 'rects': rects})
        caret = None
        if full_caret is not None:
            segment = next((s for s in segments if s.start <= full_caret <= s.end), None)
            if segment is not None:
                local = full_caret - segment.start
                after = api.char_box(segment.acc, local) if local < len(segment.text) else None
                before = api.char_box(segment.acc, local - 1) if local > 0 else None
                caret = caret_box(before, after)
        return {'toolkit': list(api.toolkit(acc)), 'caret': caret, 'clip': api.extents(acc), 'marks': marks}
