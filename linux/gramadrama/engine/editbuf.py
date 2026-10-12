"""The result being edited in the correction panel (branding/PANEL.md, Edycja; pure, no gi).

The GNOME panel never takes the keyboard from the app, so the engine is the editor: it keeps the text, the caret
(cursor) and the other end of the selection (anchor), in code points, and the panel only draws them. Moves and
deletions never split a user-perceived character (plan.is_boundary). Up, Down, Home and End follow the panel's
visual lines when it has reported them (lines: the offsets where they start), else the text's own lines."""
import re
from . import plan

WORD = re.compile(r"[\w'’]")  # as core.TOKEN: letters, digits, '_' and apostrophes
UNDO_STEPS = 100


class EditBuffer:
    def __init__(self, text, cursor=None):
        self.text = text
        self.cursor = self.anchor = len(text) if cursor is None else max(0, min(cursor, len(text)))
        self.lines = None  # where the panel's visual lines start, None until it says
        self.goal = None  # the column Up and Down keep while they are pressed one after another
        self.undo_steps, self.redo_steps = [], []
        self._typing = None  # 'insert' or 'delete' while one undo step grows by single keys

    # ---- state ----
    @property
    def selection(self):
        return min(self.cursor, self.anchor), max(self.cursor, self.anchor)

    @property
    def has_selection(self):
        return self.cursor != self.anchor

    @property
    def typing(self):
        """The last edit was a key typed or a Backspace, and nothing has moved since: a word may be unfinished."""
        return self._typing is not None

    def selected(self):
        start, end = self.selection
        return self.text[start:end]

    def set_lines(self, starts):
        """The panel's visual lines: their start offsets, from 0, rising, inside the text; anything else is ignored."""
        if (isinstance(starts, (list, tuple)) and starts and starts[0] == 0 and all(type(s) is int for s in starts)
                and all(a < b for a, b in zip(starts, starts[1:])) and starts[-1] <= len(self.text)):
            self.lines = tuple(starts)

    # ---- edits ----
    def insert(self, text):
        """Typed or pasted text in place of the selection. Line ends become '\\n'."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if not text and not self.has_selection:
            return False
        single = len(text) == 1 and not self.has_selection
        # a word typed after a space is a new undo step; the space goes with the word before it
        word_starts = single and not text.isspace() and self.cursor > 0 and self.text[self.cursor - 1].isspace()
        self._remember('insert' if single else None, word_starts)
        start, end = self.selection
        self._replace(start, end, text)
        return True

    def delete(self, forward=False, word=False):
        """Backspace (forward=False) or Delete: the selection, else one character or (word) up to the word's edge."""
        if self.has_selection:
            start, end = self.selection
        elif forward:
            start, end = self.cursor, self._word_right(self.cursor) if word else self._next(self.cursor)
        else:
            start, end = self._word_left(self.cursor) if word else self._previous(self.cursor), self.cursor
        if start == end:
            return False
        self._remember('delete' if not self.has_selection and not word else None)
        self._replace(start, end, '')
        return True

    def replace(self, start, end, text):
        """A suggestion put in place of start..end (one undo step), the caret after it."""
        if not 0 <= start <= end <= len(self.text):
            return False
        self._remember(None)
        self._replace(start, end, text)
        return True

    def _replace(self, start, end, text):
        self.text = self.text[:start] + text + self.text[end:]
        self.cursor = self.anchor = start + len(text)
        self.lines = None  # the panel lays the new text out again
        self.goal = None

    def _remember(self, typing, new_step=False):
        """One undo step per edit, except that keys of the same kind (typing, Backspace) in a row are one step."""
        if typing is None or typing != self._typing or new_step:
            self.undo_steps.append((self.text, self.cursor, self.anchor))
            del self.undo_steps[:-UNDO_STEPS]
        self.redo_steps.clear()
        self._typing = typing

    def undo(self):
        return self._restore(self.undo_steps, self.redo_steps)

    def redo(self):
        return self._restore(self.redo_steps, self.undo_steps)

    def _restore(self, source, target):
        if not source:
            return False
        target.append((self.text, self.cursor, self.anchor))
        self.text, self.cursor, self.anchor = source.pop()
        self.lines = self.goal = self._typing = None
        return True

    # ---- moves ----
    def move(self, where, extend=False):
        """where: 'left', 'right', 'word_left', 'word_right', 'home', 'end', 'up', 'down', 'start' or 'finish'.
        extend: Shift held, the selection grows from its anchor. Without it Left and Right collapse a selection
        to its edge, as in GTK."""
        self._typing = None
        if where not in ('up', 'down'):
            self.goal = None
        if not extend and self.has_selection and where in ('left', 'right'):
            start, end = self.selection
            self.cursor = self.anchor = start if where == 'left' else end
            return True
        target = {
            'left': lambda: self._previous(self.cursor),
            'right': lambda: self._next(self.cursor),
            'word_left': lambda: self._word_left(self.cursor),
            'word_right': lambda: self._word_right(self.cursor),
            'home': lambda: self._line(self.cursor)[0],
            'end': lambda: self._line_end(self.cursor),
            'up': lambda: self._vertical(-1),
            'down': lambda: self._vertical(1),
            'start': lambda: 0,
            'finish': lambda: len(self.text),
        }[where]()
        before = (self.cursor, self.anchor)
        self.cursor = target
        if not extend:
            self.anchor = target
        return (self.cursor, self.anchor) != before

    def place(self, offset):
        """A click: the caret there, at the nearest place that does not split a character, nothing selected."""
        self._typing = self.goal = None
        offset = max(0, min(offset, len(self.text)))
        while offset > 0 and not plan.is_boundary(self.text, offset):
            offset -= 1
        changed = (self.cursor, self.anchor) != (offset, offset)
        self.cursor = self.anchor = offset
        return changed

    def select_all(self):
        self._typing = self.goal = None
        changed = (self.anchor, self.cursor) != (0, len(self.text))
        self.anchor, self.cursor = 0, len(self.text)
        return changed

    def _previous(self, i):
        i -= 1
        while i > 0 and not plan.is_boundary(self.text, i):
            i -= 1
        return max(i, 0)

    def _next(self, i):
        i += 1
        while i < len(self.text) and not plan.is_boundary(self.text, i):
            i += 1
        return min(i, len(self.text))

    def _word_left(self, i):
        while i > 0 and not WORD.match(self.text[i - 1]):
            i -= 1
        while i > 0 and WORD.match(self.text[i - 1]):
            i -= 1
        return i

    def _word_right(self, i):
        n = len(self.text)
        while i < n and not WORD.match(self.text[i]):
            i += 1
        while i < n and WORD.match(self.text[i]):
            i += 1
        return i

    def _starts(self):
        if self.lines is not None:
            return self.lines
        return (0,) + tuple(m.end() for m in re.finditer('\n', self.text))

    def _line(self, i):
        """(start, end) of the line i is on: a visual line ends where the next starts, a text line at its '\\n'."""
        starts = self._starts()
        k = max(j for j, start in enumerate(starts) if start <= i)
        return starts[k], starts[k + 1] if k + 1 < len(starts) else len(self.text)

    def _line_end(self, i):
        start, end = self._line(i)
        if end < len(self.text) and end > start and (self.text[end - 1] == '\n' or self.lines is not None):
            return end - 1  # before the line break, or before the space where the line wraps
        return end

    def _vertical(self, step):
        starts = self._starts()
        k = max(j for j, start in enumerate(starts) if start <= self.cursor)
        if self.goal is None:
            self.goal = self.cursor - starts[k]
        k += step
        if k < 0:
            return 0
        if k >= len(starts):
            return len(self.text)
        start = starts[k]
        return min(start + self.goal, self._line_end(start))
