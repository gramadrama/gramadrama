"""Key constants and key classification for the engine's key path (pure; no gi)."""
import collections
from . import shortcuts

DEFAULT_SHORTCUTS = shortcuts.resolve()

SHIFT, LOCK, CONTROL, MOD1, MOD2, MOD4, MOD5 = 0x1, 0x2, 0x4, 0x8, 0x10, 0x40, 0x80
SUPER, HYPER, META, RELEASE = 0x4000000, 0x8000000, 0x10000000, 0x40000000
# LOCK (Caps) and MOD2 (NumLock) never change the meaning of a shortcut.
RELEVANT = SHIFT | CONTROL | MOD1 | MOD4 | MOD5 | SUPER | HYPER | META
HOTKEY_MODS = CONTROL | MOD1
NO_TEXT_MODS = SUPER | MOD4  # Super chords belong to the shell

KEY_g, KEY_G, KEY_a, KEY_A, KEY_d, KEY_D, KEY_1, KEY_6 = 0x67, 0x47, 0x61, 0x41, 0x64, 0x44, 0x31, 0x36
KEY_j, KEY_J, KEY_i, KEY_I = 0x6a, 0x4a, 0x69, 0x49
KEY_e, KEY_E, KEY_y, KEY_Y, KEY_z, KEY_Z = 0x65, 0x45, 0x79, 0x59, 0x7a, 0x5a
KEY_c, KEY_C, KEY_v, KEY_V, KEY_x, KEY_X = 0x63, 0x43, 0x76, 0x56, 0x78, 0x58
RETURN, KP_ENTER, ESCAPE, TAB, KP_TAB, ISO_LEFT_TAB = 0xff0d, 0xff8d, 0xff1b, 0xff09, 0xff89, 0xfe20
BACKSPACE, DELETE, KP_DELETE, INSERT, KP_INSERT = 0xff08, 0xffff, 0xff9f, 0xff63, 0xff9e
HOME, LEFT, UP, RIGHT, DOWN, PAGE_UP, PAGE_DOWN, END = 0xff50, 0xff51, 0xff52, 0xff53, 0xff54, 0xff55, 0xff56, 0xff57
ISO_LEVEL3_SHIFT, ISO_LEVEL5_SHIFT, MULTI_KEY, NUM_LOCK = 0xfe03, 0xfe11, 0xff20, 0xff7f
SHIFT_L, HYPER_R, MODE_SWITCH = 0xffe1, 0xffee, 0xff7e  # Shift_L..Hyper_R include Caps_Lock

# KP_Home..KP_End (0xff95..0xff9c) move the caret like their main-block twins.
NAV = frozenset({HOME, END, LEFT, UP, RIGHT, DOWN, PAGE_UP, PAGE_DOWN, *range(0xff95, 0xff9d)})
EDIT = frozenset({RETURN, KP_ENTER, BACKSPACE, DELETE, KP_DELETE, TAB, KP_TAB, ISO_LEFT_TAB})
CTRL_EDITS = frozenset(map(ord, 'aAxXvVzZyY'))

Action = collections.namedtuple('Action', 'kind mode', defaults=(None,))
PASS, HOTKEY, ACCEPT, CANCEL = Action('pass'), Action('hotkey'), Action('accept'), Action('cancel')
# Ctrl+Alt+J: the hotkey straight into Tłumacz (⌥⌘T on macOS; Ctrl+Alt+T opens GNOME's terminal).
TRANSLATE = Action('translate')
SCROLL_UP, SCROLL_DOWN, SWALLOW = Action('scroll_up'), Action('scroll_down'), Action('swallow')
CANCEL_AND_PASS, DIRECTION = Action('cancel_and_pass'), Action('direction')
# Ctrl+I: the change list under a finished correction, shown and hidden on demand (branding/PANEL.md).
CHANGES = Action('changes')
# Editing the result in the panel (branding/PANEL.md, Edycja): Ctrl+E starts and ends it, EDIT_KEY is a key for
# the editor (Session.edit_key), also the letter or Backspace that starts editing a finished result.
EDIT_TOGGLE, EDIT_KEY = Action('edit_toggle'), Action('edit_key')
REPEATING_EDITS = frozenset({BACKSPACE, DELETE, KP_DELETE}) | NAV  # held down, they act again while editing


def MODE(n):
    return Action('mode', n)


class KeyTracker:
    """Keycodes whose press was consumed: their auto-repeats and releases must not reach the app."""

    def __init__(self):
        self._consumed = {}  # keycode -> focus generation of the consumed press
        self._focus = 0

    def consume(self, keycode):
        self._consumed[keycode] = self._focus

    def new_focus(self):
        """Call on every focus change: a later press of a key whose release went to another
        window is a new press, not an auto-repeat (its release is still swallowed)."""
        self._focus += 1

    def release(self, keycode):
        return self._consumed.pop(keycode, None) is not None

    def repeating(self, keycode):
        focus = self._consumed.get(keycode)
        if focus is None:
            return False
        if focus == self._focus:
            return True
        del self._consumed[keycode]
        return False


def is_modifier(keyval):
    return (SHIFT_L <= keyval <= HYPER_R or 0xfe01 <= keyval <= 0xfe0f or 0xfe11 <= keyval <= 0xfe13
            or keyval in (NUM_LOCK, MODE_SWITCH))


def is_printable(keyval):
    # Latin/legacy and Unicode keysyms and the keypad's character keys; dead keys (0xfe50..) are not.
    return (0x20 <= keyval < 0xfd00 or 0x1000100 <= keyval <= 0x110ffff
            or 0xffaa <= keyval <= 0xffb9 or keyval in (0xff80, 0xffbd))


def classify(keyval, keycode, state, session_active, tracker, translating=False, bindings=None, editing=False,
             editable=False):
    """Decide what one key event means. session_active is Session.is_modal; translating: the
    open session is in the translation mode (Ctrl+D reverses the direction, as ⌘D on macOS).
    editing: the result is being edited in the panel, every key but a modifier is the editor's;
    editable: a finished result in the panel, where Ctrl+E, a letter or Backspace starts editing."""
    if state & RELEASE:
        return SWALLOW if tracker.release(keycode) else PASS
    mods = state & RELEVANT
    if tracker.repeating(keycode):
        held_edit = keyval in REPEATING_EDITS or (is_printable(keyval) and not mods & (CONTROL | MOD1))
        return EDIT_KEY if editing and held_edit else SWALLOW
    correction, translation = bindings or DEFAULT_SHORTCUTS
    if correction.matches(keyval, mods):
        return HOTKEY
    if translation.matches(keyval, mods):
        return TRANSLATE
    if not session_active:
        return PASS
    if editing:
        if is_modifier(keyval):
            return PASS
        return EDIT_TOGGLE if keyval in (KEY_e, KEY_E) and mods == CONTROL else EDIT_KEY
    if keyval in (RETURN, KP_ENTER) and not mods:
        return ACCEPT
    if keyval == ESCAPE and not mods:
        return CANCEL
    if KEY_1 <= keyval <= KEY_6 and mods == CONTROL:
        return MODE(keyval - KEY_1)
    if translating and keyval in (KEY_d, KEY_D) and mods == CONTROL:
        return DIRECTION
    if keyval in (KEY_i, KEY_I) and mods == CONTROL:
        return CHANGES
    if keyval in (PAGE_UP, PAGE_DOWN) and not mods:
        return SCROLL_UP if keyval == PAGE_UP else SCROLL_DOWN
    if is_modifier(keyval):
        return PASS
    if editable and keyval in (KEY_e, KEY_E) and mods == CONTROL:
        return EDIT_TOGGLE
    if editable and ((is_printable(keyval) and not mods & (CONTROL | MOD1 | NO_TEXT_MODS)) or (keyval == BACKSPACE and not mods)):
        return EDIT_KEY  # typing in a finished result edits it (macOS), with that key at the end
    return CANCEL_AND_PASS


def affects_text_or_caret(keyval, state):
    """True when the app may change its text, caret or selection because of this press.

    With Control only Ctrl+A/X/V/Z/Y count among character keys (Ctrl+C and other chords do
    not); navigation and editing keys count with any modifier except Super, because Ctrl+arrows,
    Ctrl+BackSpace or Ctrl+Return still move the caret or edit."""
    if state & NO_TEXT_MODS:
        return False
    if keyval in NAV or keyval in EDIT or keyval in (INSERT, KP_INSERT):
        return True
    if state & CONTROL:
        return keyval in CTRL_EDITS
    return is_printable(keyval)


def makes_selection(keyval, state):
    if state & NO_TEXT_MODS:
        return False
    return bool(state & SHIFT and keyval in NAV) or bool(state & CONTROL and keyval in (KEY_a, KEY_A))


def collapses_selection(keyval, state):
    """True only for keys that collapse a selection in every toolkit; unsure cases stay False."""
    if state & (NO_TEXT_MODS | MOD1):
        return False
    if keyval in NAV:
        return not state & SHIFT
    return not state & CONTROL and (is_printable(keyval) or keyval in EDIT)
