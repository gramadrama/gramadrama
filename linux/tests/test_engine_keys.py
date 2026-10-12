import unittest
from gramadrama.engine import keys
from gramadrama.engine.keys import (ACCEPT, CANCEL, CANCEL_AND_PASS, CONTROL, HOTKEY, LOCK, MOD1, MOD2, MOD4,
                                    MOD5, MODE, PASS, RELEASE, SCROLL_DOWN, SCROLL_UP, SHIFT, SUPER, SWALLOW,
                                    TRANSLATE)

a, g, G, j, J, one, six, seven = 0x61, 0x67, 0x47, 0x6a, 0x4a, 0x31, 0x36, 0x37
aogonek, dead_acute = 0x1b1, 0xfe51
Shift_L, Control_R, Alt_L, Super_L, Caps_Lock = 0xffe1, 0xffe4, 0xffe9, 0xffeb, 0xffe5


def classify(keyval, state=0, active=False, keycode=30, tracker=None):
    return keys.classify(keyval, keycode, state, active, tracker or keys.KeyTracker())


class ClassifyTests(unittest.TestCase):
    def test_table(self):
        cases = [
            # hotkey: Control+Alt exactly; Caps/NumLock ignored; AltGr (MOD5) or extra modifiers never match
            ('hotkey', g, CONTROL | MOD1, False, HOTKEY),
            ('hotkey upper', G, CONTROL | MOD1 | SHIFT, False, PASS),
            ('hotkey capslock', G, CONTROL | MOD1 | LOCK, False, HOTKEY),
            ('hotkey numlock', g, CONTROL | MOD1 | MOD2, False, HOTKEY),
            ('hotkey while active', g, CONTROL | MOD1, True, HOTKEY),
            ('altgr g', g, CONTROL | MOD1 | MOD5, False, PASS),
            ('super hotkey', g, CONTROL | MOD1 | SUPER | MOD4, False, PASS),
            ('ctrl g', g, CONTROL, False, PASS),
            ('alt g', g, MOD1, False, PASS),
            # Ctrl+Alt+J: straight into Tłumacz, with the same modifier rules
            ('translate', j, CONTROL | MOD1, False, TRANSLATE),
            ('translate capslock', J, CONTROL | MOD1 | LOCK, False, TRANSLATE),
            ('translate while active', j, CONTROL | MOD1, True, TRANSLATE),
            ('translate upper', J, CONTROL | MOD1 | SHIFT, False, PASS),
            ('altgr j', j, CONTROL | MOD1 | MOD5, False, PASS),
            ('ctrl j', j, CONTROL, False, PASS),
            # idle: everything passes
            ('idle enter', keys.RETURN, 0, False, PASS),
            ('idle esc', keys.ESCAPE, 0, False, PASS),
            ('idle ctrl+1', one, CONTROL, False, PASS),
            ('idle pgdn', keys.PAGE_DOWN, 0, False, PASS),
            # active session
            ('enter', keys.RETURN, 0, True, ACCEPT),
            ('kp enter', keys.KP_ENTER, MOD2, True, ACCEPT),
            ('enter capslock', keys.RETURN, LOCK, True, ACCEPT),
            ('shift enter', keys.RETURN, SHIFT, True, CANCEL_AND_PASS),
            ('ctrl enter', keys.RETURN, CONTROL, True, CANCEL_AND_PASS),
            ('esc', keys.ESCAPE, 0, True, CANCEL),
            ('shift esc', keys.ESCAPE, SHIFT, True, CANCEL_AND_PASS),
            ('ctrl+1', one, CONTROL, True, MODE(0)),
            ('ctrl+6', six, CONTROL, True, MODE(5)),
            ('ctrl+7', seven, CONTROL, True, CANCEL_AND_PASS),
            ('plain 1', one, 0, True, CANCEL_AND_PASS),
            ('ctrl+shift+1', one, CONTROL | SHIFT, True, CANCEL_AND_PASS),
            ('pgup', keys.PAGE_UP, 0, True, SCROLL_UP),
            ('pgdn', keys.PAGE_DOWN, MOD2, True, SCROLL_DOWN),
            ('shift pgdn', keys.PAGE_DOWN, SHIFT, True, CANCEL_AND_PASS),
            ('letter', a, 0, True, CANCEL_AND_PASS),
            ('altgr letter', aogonek, MOD5, True, CANCEL_AND_PASS),
            ('dead key', dead_acute, MOD5, True, CANCEL_AND_PASS),
            ('compose', keys.MULTI_KEY, 0, True, CANCEL_AND_PASS),
            ('arrow', keys.LEFT, 0, True, CANCEL_AND_PASS),
        ]
        for mod in (Shift_L, Control_R, Alt_L, Super_L, Caps_Lock, keys.NUM_LOCK,
                    keys.ISO_LEVEL3_SHIFT, keys.ISO_LEVEL5_SHIFT, keys.MODE_SWITCH):
            cases.append(('modifier %x never cancels' % mod, mod, MOD5, True, PASS))
        for name, keyval, state, active, expected in cases:
            with self.subTest(name):
                self.assertEqual(classify(keyval, state, active), expected)

    def test_releases_of_consumed_presses_are_swallowed_once(self):
        tracker = keys.KeyTracker()
        self.assertEqual(classify(keys.RETURN, RELEASE, True, 36, tracker), PASS)
        tracker.consume(36)
        self.assertEqual(classify(keys.RETURN, RELEASE, False, 36, tracker), SWALLOW)
        self.assertEqual(classify(keys.RETURN, RELEASE, False, 36, tracker), PASS)

    def test_auto_repeat_of_a_consumed_key_is_swallowed(self):
        tracker = keys.KeyTracker()
        tracker.consume(42)
        for active in (False, True):
            with self.subTest(active=active):
                self.assertEqual(classify(g, CONTROL | MOD1, active, 42, tracker), SWALLOW)
                self.assertEqual(classify(keys.RETURN, 0, active, 42, tracker), SWALLOW)
        self.assertEqual(classify(g, CONTROL | MOD1 | RELEASE, True, 42, tracker), SWALLOW)
        self.assertEqual(classify(g, CONTROL | MOD1, True, 42, tracker), HOTKEY)

    def test_a_hotkey_left_to_the_app_is_not_tracked(self):
        # docs/apps.md: where GramaDrama is off, the hotkey's press is not consumed (tracker.consume
        # is never called), so its auto-repeat is a hotkey again and its release reaches the app.
        tracker = keys.KeyTracker()
        self.assertEqual(classify(g, CONTROL | MOD1, False, 42, tracker), HOTKEY)
        self.assertEqual(classify(g, CONTROL | MOD1, True, 42, tracker), HOTKEY)
        self.assertEqual(classify(g, CONTROL | MOD1 | RELEASE, False, 42, tracker), PASS)

    def test_lost_release_after_focus_change_never_eats_the_next_press(self):
        tracker = keys.KeyTracker()
        tracker.consume(36)
        tracker.new_focus()
        self.assertEqual(classify(keys.RETURN, 0, False, 36, tracker), PASS)
        self.assertEqual(classify(keys.RETURN, RELEASE, False, 36, tracker), PASS)
        tracker.consume(9)
        tracker.new_focus()
        self.assertEqual(classify(keys.ESCAPE, RELEASE, False, 9, tracker), SWALLOW)


class PredicateTests(unittest.TestCase):
    def test_table(self):
        A, C, V, X, Y, Z, B = 0x41, 0x63, 0x76, 0x78, 0x79, 0x7a, 0x62
        kp_7, euro_unicode = 0xffb7, 0x10020ac
        # (name, keyval, state, affects_text_or_caret, makes_selection, collapses_selection)
        cases = [
            ('letter', a, 0, True, False, True),
            ('shift letter', A, SHIFT, True, False, True),
            ('altgr ą', aogonek, MOD5, True, False, True),
            ('unicode keysym', euro_unicode, MOD5, True, False, True),
            ('keypad digit', kp_7, MOD2, True, False, True),
            ('space', 0x20, 0, True, False, True),
            ('dead key', dead_acute, MOD5, False, False, False),
            ('modifier', Shift_L, 0, False, False, False),
            ('caps lock', Caps_Lock, 0, False, False, False),
            ('enter', keys.RETURN, 0, True, False, True),
            ('kp enter', keys.KP_ENTER, 0, True, False, True),
            ('backspace', keys.BACKSPACE, 0, True, False, True),
            ('delete', keys.DELETE, 0, True, False, True),
            ('tab', keys.TAB, 0, True, False, True),
            ('left', keys.LEFT, 0, True, False, True),
            ('home', keys.HOME, 0, True, False, True),
            ('pgdn', keys.PAGE_DOWN, 0, True, False, True),
            ('shift right', keys.RIGHT, SHIFT, True, True, False),
            ('shift end', keys.END, SHIFT, True, True, False),
            ('ctrl shift left', keys.LEFT, CONTROL | SHIFT, True, True, False),
            ('shift pgup', keys.PAGE_UP, SHIFT, True, True, False),
            ('ctrl left', keys.LEFT, CONTROL, True, False, True),
            ('alt left', keys.LEFT, MOD1, True, False, False),
            ('ctrl a', a, CONTROL, True, True, False),
            ('ctrl shift A', A, CONTROL | SHIFT, True, True, False),
            ('ctrl x', X, CONTROL, True, False, False),
            ('ctrl v', V, CONTROL, True, False, False),
            ('ctrl z', Z, CONTROL, True, False, False),
            ('ctrl shift Z', 0x5a, CONTROL | SHIFT, True, False, False),
            ('ctrl y', Y, CONTROL, True, False, False),
            ('ctrl c', C, CONTROL, False, False, False),
            ('ctrl b', B, CONTROL, False, False, False),
            ('ctrl 1', one, CONTROL, False, False, False),
            ('ctrl backspace', keys.BACKSPACE, CONTROL, True, False, False),
            ('ctrl enter', keys.RETURN, CONTROL, True, False, False),
            ('alt letter', a, MOD1, True, False, False),
            ('super letter', a, SUPER | MOD4, False, False, False),
            ('super left', keys.LEFT, MOD4, False, False, False),
            ('super shift right', keys.RIGHT, SUPER | SHIFT, False, False, False),
            ('escape', keys.ESCAPE, 0, False, False, False),
        ]
        for name, keyval, state, affects, makes, collapses in cases:
            with self.subTest(name):
                self.assertEqual(keys.affects_text_or_caret(keyval, state), affects)
                self.assertEqual(keys.makes_selection(keyval, state), makes)
                self.assertEqual(keys.collapses_selection(keyval, state), collapses)


if __name__ == '__main__':
    unittest.main()


class ChangesKeyTests(unittest.TestCase):
    def test_ctrl_i_only_while_the_panel_is_open(self):
        from gramadrama.engine import keys
        self.assertEqual(keys.classify(0x69, 31, keys.CONTROL, True, keys.KeyTracker()), keys.CHANGES)
        self.assertEqual(keys.classify(0x49, 31, keys.CONTROL, True, keys.KeyTracker()), keys.CHANGES)
        self.assertEqual(keys.classify(0x69, 31, keys.CONTROL, False, keys.KeyTracker()), keys.PASS)  # italics in the app
        self.assertEqual(keys.classify(0x69, 31, keys.CONTROL, True, keys.KeyTracker(), editing=True), keys.EDIT_KEY)
        self.assertEqual(keys.classify(0x69, 31, keys.CONTROL | keys.SHIFT, True, keys.KeyTracker()),
                         keys.CANCEL_AND_PASS)


class DirectionKeyTests(unittest.TestCase):
    def test_ctrl_d_only_while_translating(self):
        from gramadrama.engine import keys
        tracker = keys.KeyTracker()
        self.assertEqual(keys.classify(0x64, 40, keys.CONTROL, True, tracker, translating=True), keys.DIRECTION)
        self.assertEqual(keys.classify(0x44, 40, keys.CONTROL, True, keys.KeyTracker(), translating=True), keys.DIRECTION)
        self.assertEqual(keys.classify(0x64, 40, keys.CONTROL, True, keys.KeyTracker()), keys.CANCEL_AND_PASS)
        self.assertEqual(keys.classify(0x64, 40, keys.CONTROL, False, keys.KeyTracker(), translating=True), keys.PASS)
        self.assertEqual(keys.classify(0x64, 40, keys.CONTROL | keys.SHIFT, True, keys.KeyTracker(), translating=True),
                         keys.CANCEL_AND_PASS)
