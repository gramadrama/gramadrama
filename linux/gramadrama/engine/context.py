"""What the engine knows about the focused field: freshness, policy and snapshots (pure; no gi)."""
import dataclasses
import itertools
from . import keys

PASSWORD, PIN, TERMINAL = 8, 9, 10
PRIVATE, HIDDEN_TEXT = 2048, 4096
SURROUNDING_TEXT = 32
# In-process IBus IM modules re-send the widget's state right before every key event they
# forward, so an update after a reset or a focus change is never an echo of an older state.
KEY_TIME_CLIENTS = ('gtk3-im:', 'gtk4-im:')
# Wayland apps reach IBus through GNOME Shell's single input context (text-input-v3). The shell
# reports SURROUNDING_TEXT only once the app has sent its text since the focus change, and Chrome
# sends it only on the first caret or text change: until then the text is unknown, not missing.
TEXT_INPUT_V3 = 'gnome-shell'
# ConfirmCaret ids, one numbering for the process: the one live service answers every engine object.
CONFIRMS = itertools.count(1)


@dataclasses.dataclass(frozen=True)
class Refusal:
    reason: str


@dataclasses.dataclass(frozen=True)
class ConfirmCaret:
    """Effect (of the session's hotkey and of the live check): ask the live service where AT-SPI sees the
    caret in text, the field's text as IBus has it; the answer goes to EngineLogic.caret_confirmed."""
    id: int
    text: str


@dataclasses.dataclass(frozen=True)
class Snapshot:
    text: str
    cursor: int
    anchor: int
    client: str
    seen_anchor: bool
    maybe_hidden_selection: bool
    selection_unknown: bool = False


class FieldContext:
    """State of the focused input context, fed by the engine's vfuncs.

    Offsets are code points. The single global engine instance moves between input contexts,
    so text state is dropped on every focus change. caps, purpose and hints are kept: ibus-daemon
    re-sends them on focus only when they differ from the values it last sent this engine."""

    def __init__(self):
        self.caps = self.purpose = self.hints = 0
        self.client = None
        # The app with focus and its window kind at the last hotkey, as GNOME Shell reported them
        # (docs/apps.md): Wayland apps all reach IBus as the one client 'gnome-shell'. None: unknown.
        self.app = self.app_kind = None
        self.focus_gen = 0  # grows with every focus change: dictation inserts only where it started
        self.can_confirm = False  # the adapter can ask AT-SPI where the caret is (ConfirmCaret)
        self.version = 0  # grows with every event that may change the field: an older AT-SPI answer is void
        # The (text, cursor, anchor) ibus-daemon last handed this engine object, kept across focus changes as
        # ibus-daemon keeps it (bus/engineproxy.c): it drops an update identical to it, so an app that sends
        # the same state again after a focus change sends nothing. None: unknown (a secret is never kept).
        self.forwarded = ('', 0, 0)
        self.surrounding_lowered = False  # this focus has had caps without SURROUNDING_TEXT (set_caps)
        self._clear()

    def _clear(self):
        """Forget the field's text. Until a key is typed, the first update afterwards is stored but
        not trusted: GTK over text-input-v3 pushes the widget's state from before the click that
        focused it, and GTK3 never pushes the click's caret. Whether text is selected is unknown
        until an update reports a selection or a key collapses it (GTK3 keeps a selection it never
        reports across window switches)."""
        self.version += 1
        self.text = None
        self.cursor = self.anchor = 0
        self.fresh = self.awaiting_update = False
        self.after_reset = not self._key_time_client()
        self.before_reset = None
        self.seen_anchor = self.maybe_hidden_selection = False
        self.selection_unknown = True
        self.dropped = None  # the state ibus-daemon has just dropped as unchanged, until the next message (settle)

    @property
    def sensitive(self):
        return self.purpose in (PASSWORD, PIN) or bool(self.hints & (PRIVATE | HIDDEN_TEXT))

    def focus_in(self, client):
        self.client = client
        self.focus_gen += 1
        self.surrounding_lowered = False
        self._clear()

    def focus_out(self):
        self.client = None
        self.focus_gen += 1
        self.surrounding_lowered = False
        self._clear()

    def set_caps(self, caps):
        """GNOME Shell lowers SURROUNDING_TEXT at a focus change and raises it in the call that sends the app's
        text, right after the caps (js/misc/inputMethod.js, set_surrounding), and ibus-daemon hands both on
        in order. So a rise in this focus while no text has come means the text follows as the next message,
        or ibus-daemon dropped it as identical to `forwarded`; settle() tells which at the next message."""
        rose = not self.caps & SURROUNDING_TEXT and caps & SURROUNDING_TEXT
        self.caps = caps
        if not caps & SURROUNDING_TEXT:
            self.surrounding_lowered = True
        elif (rose and self.surrounding_lowered and self.client == TEXT_INPUT_V3 and self.text is None
              and not self.sensitive and self.forwarded is not None and self.forwarded[0]):
            # An empty text is not kept: AT-SPI could never confirm it, and an empty field is refused anyway.
            self.dropped = self.forwarded

    def settle(self):
        """Call before handling any message from ibus-daemon except SetSurroundingText and focus changes. When
        SURROUNDING_TEXT has just risen (set_caps) and this message is not the text, ibus-daemon dropped the
        text as unchanged: the field holds `forwarded`. It is taken untrusted, as any first update after a
        focus change, and the next update is still distrusted as such a first one: only AT-SPI (confirm) or
        an update answering a key makes the field current. True when it was taken."""
        dropped, self.dropped = self.dropped, None
        if dropped is None or self.sensitive or self.text is not None:
            return False
        text, cursor, anchor = dropped
        if not (0 <= cursor <= len(text) and 0 <= anchor <= len(text)):
            return False
        self.version += 1
        self.text, self.cursor, self.anchor, self.fresh = text, cursor, anchor, False
        if anchor != cursor:  # the app reported this selection, as in surrounding()
            self.seen_anchor = True
            self.maybe_hidden_selection = self.selection_unknown = False
        return True

    def set_content_type(self, purpose, hints):
        """Returns True when the field has just become sensitive; its text is dropped at once."""
        was = self.sensitive
        self.purpose, self.hints = purpose, hints
        if self.sensitive:
            self._clear()  # nothing derived from a secret stays: text, offsets, before_reset
            self.forwarded = None
        return self.sensitive and not was

    def reset(self):
        # before_reset=None means "the app's state before this reset is unknown": a toolkit
        # that answers a reset with its pre-click state (GTK4 over text-input-v3) may then push
        # a state we never saw, so the first update after the reset cannot be trusted.
        known = (self.fresh and not self.awaiting_update) or self._key_time_client()
        self.before_reset = (self.text, self.cursor, self.anchor) if known else None
        self.after_reset = True
        self.fresh = False
        self.version += 1

    def surrounding(self, text, cursor, anchor):
        self.version += 1
        self.dropped = None
        self.forwarded = None if self.sensitive or text is None else (text, cursor, anchor)
        if self.sensitive or text is None or not (0 <= cursor <= len(text) and 0 <= anchor <= len(text)):
            self.text, self.fresh = None, False
            self.cursor = self.anchor = 0
            return
        echo = self.after_reset and self.before_reset in (None, (text, cursor, anchor))
        self.text, self.cursor, self.anchor = text, cursor, anchor
        self.fresh = not echo
        self.after_reset, self.before_reset, self.awaiting_update = False, None, False
        if anchor != cursor:  # this toolkit reports selections
            self.seen_anchor = True
            self.maybe_hidden_selection = self.selection_unknown = False

    def key_passed(self, keyval, state):
        """Call for every key press that reaches the app (never for releases or consumed keys)."""
        if not keys.is_modifier(keyval):  # Shift or Ctrl alone changes nothing in the field
            self.version += 1
        if keys.affects_text_or_caret(keyval, state):
            # The next update answers this key, so it shows the current state whatever came before.
            self.awaiting_update = True
            self.after_reset, self.before_reset = False, None
        if keys.makes_selection(keyval, state) and not self.seen_anchor:
            self.maybe_hidden_selection = True
        if keys.collapses_selection(keyval, state):
            self.maybe_hidden_selection = self.selection_unknown = False

    def expect_update(self):
        """The engine changed the text itself: nothing is fresh until the client echoes it."""
        self.awaiting_update = True
        self.version += 1

    def confirmable(self):
        """Only the caret is in doubt: a text-input-v3 field whose text came, while no key waits for its
        update. Accessibility (AT-SPI) may then say where the caret is in that text (confirm)."""
        return (self.can_confirm and self.client == TEXT_INPUT_V3 and self.text is not None
                and bool(self.caps & SURROUNDING_TEXT) and not self.sensitive and self.purpose != TERMINAL
                and not self.awaiting_update)

    def confirm(self, version, cursor, anchor):
        """AT-SPI saw the caret at cursor and the selection's other end at anchor in the text of version (the
        version when it was asked). 'ok' when that is the state IBus has: the app sent it (GTK4 after a click
        whose update came after another click that sent none, ibus-daemon dropping it as unchanged), so the
        field is current. Then the selection is known, and after_reset stays: an echo still distrusts.
        'differs' when the app has not sent its state (a drag, Ctrl+A, the pre-click state at a focus
        change): mutter turns a delete_surrounding_text into bytes around the caret the app last sent, so a
        replacement there would land beside the text meant, and the field stays stale.
        'changed' when something happened since the question, 'unknown' when AT-SPI could not tell."""
        if not isinstance(cursor, int) or not isinstance(anchor, int):
            return 'unknown'
        if version != self.version or not self.confirmable():
            return 'changed'
        if (cursor, anchor) != (self.cursor, self.anchor):
            return 'differs'
        self.fresh = True
        self.maybe_hidden_selection = self.selection_unknown = False  # AT-SPI reports the selection
        return 'ok'

    def _key_time_client(self):
        return bool(self.client) and self.client.startswith(KEY_TIME_CLIENTS)

    def snapshot(self):
        """Snapshot of the field, or Refusal(reason) when it must not be read."""
        if self.client in (None, 'fake'):
            return Refusal('no_field')
        if self.sensitive:
            return Refusal('sensitive')
        if self.purpose == TERMINAL:
            return Refusal('terminal')
        if not self.caps & SURROUNDING_TEXT or self.text is None:
            if self.client == TEXT_INPUT_V3 and not self.awaiting_update:
                return Refusal('stale')  # a caret key makes the app send it
            return Refusal('no_text')
        if not self.fresh or self.awaiting_update:
            return Refusal('stale')
        return Snapshot(self.text, self.cursor, self.anchor, self.client,
                        self.seen_anchor, self.maybe_hidden_selection, self.selection_unknown)

    def status(self):
        """Diagnostics without any text."""
        return {'client': self.client, 'caps': self.caps, 'purpose': self.purpose, 'hints': self.hints,
                'sensitive': self.sensitive, 'text_len': None if self.text is None else len(self.text),
                'cursor': self.cursor, 'anchor': self.anchor, 'fresh': self.fresh,
                'after_reset': self.after_reset, 'awaiting_update': self.awaiting_update,
                'seen_anchor': self.seen_anchor, 'maybe_hidden_selection': self.maybe_hidden_selection,
                'selection_unknown': self.selection_unknown}
