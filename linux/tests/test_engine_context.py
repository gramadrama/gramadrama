import unittest
from gramadrama.engine import keys
from gramadrama.engine.context import FieldContext, Refusal

# Fixture texts of the 2026-09-26 spike (research/2026-09-26-ibus/ibus-spike).
P1 = 'Pierwszy akapit: zażółć gęślą jaźń.'
P2 = 'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.'
P3 = 'Trzeci akapit — koniec.'
TA = '\n'.join((P1, P2, P3))
CE = 'Pierwszy akapit edytora: zażółć gęślą jaźń.\n\n' + P2 + '\n\n' + P3
Control_L, Alt_L, Shift_L = 0xffe3, 0xffe9, 0xffe1


def describe(snapshot):
    if isinstance(snapshot, Refusal):
        return snapshot.reason
    return ((snapshot.cursor, snapshot.anchor) + (('hidden',) if snapshot.maybe_hidden_selection else ())
            + (('unknown',) if snapshot.selection_unknown else ()))


def replay(events, ctx=None):
    """Feed (kind, *args) events; ('check', want) compares the current snapshot."""
    ctx = ctx or FieldContext()
    actions = {'focus': ctx.focus_in, 'blur': ctx.focus_out, 'caps': ctx.set_caps, 'type': ctx.set_content_type,
               'surr': ctx.surrounding, 'reset': ctx.reset, 'key': ctx.key_passed, 'expect': ctx.expect_update}
    for step, (kind, *args) in enumerate(events):
        if kind == 'check':
            yield step, args[0], describe(ctx.snapshot())
        else:
            actions[kind](*args)


# run2/engine.log against run2/gtk4.jsonl (critique C2), times relative to the spike's capture.
# GTK4 over text-input-v3 answers a mouse press with its state from BEFORE the press.
GTK4_MOUSE = [
    ('focus', 'gnome-shell'), ('caps', 41),
    ('key', keys.END, keys.CONTROL), ('surr', P3, 23, 23),  # -4.37 Ctrl+End
    ('check', (23, 23)),
    ('reset',), ('check', 'stale'),                  # -3.602 first click of a double-click
    ('surr', P2, 33, 33), ('check', (33, 33)),       # -3.563 the click's new caret (app 69 = P2[33])
    ('reset',), ('check', 'stale'),                  # -3.499 second click: app selects KOT, never pushed
    ('reset',), ('surr', P2, 31, 34),                # -2.688 drag press; echo of the unseen KOT selection
    ('check', 'stale'),                              # while the app drags [9..23] in P1
    ('reset',), ('surr', P1 + '\nDrugi', 23, 9),     # -1.506 click; echo of the unseen drag selection
    ('check', 'stale'),                              # while the app has a caret at 69
    ('surr', P2, 33, 33), ('check', (33, 33)),       # -1.481 the click's new caret
    ('reset',), ('reset',),                          # -0.730/-0.655 double-click on KOT, never pushed
    ('check', 'stale'),                              # the spike captured P2 here and corrupted the text
]

# run3/engine.log t=103.9-108.4: Chrome 154 pushes the NEW state right after every reset. Right
# after a focus change nothing is trusted yet (GTK would push its state from before the click), so
# clicks and double-clicks there are refused; the later updates of a drag are trusted.
CHROME_MOUSE = [
    ('reset',), ('type', 0, 0), ('reset',), ('blur',), ('focus', 'fake'), ('blur',), ('focus', 'gnome-shell'),
    ('caps', 9), ('type', 8, 6144), ('type', 8, 64), ('type', 0, 64), ('caps', 41),
    ('surr', TA, 61, 61), ('check', 'stale'),  # first update after the focus change
    ('reset',), ('surr', TA, 68, 68), ('check', 'stale'),  # 104.53 click
    ('reset',), ('surr', TA, 70, 67), ('check', 'stale'),  # 104.61 double-click selects KOT
    ('reset',), ('surr', TA, 0, 0), ('surr', TA, 4, 0), ('surr', TA, 8, 0), ('surr', TA, 12, 0),
    ('surr', TA, 15, 0), ('surr', TA, 19, 0), ('check', (19, 0)),  # 105.38 drag
    ('reset',), ('surr', TA, 95, 95), ('check', (95, 95)),  # 106.52 click, engine in sync
    ('reset',), ('reset',), ('type', 0, 0), ('reset',), ('blur',), ('focus', 'fake'), ('blur',),
    ('focus', 'gnome-shell'), ('caps', 9), ('type', 0, 64), ('caps', 41),  # 107.27 contenteditable
    ('surr', CE, 77, 77), ('check', 'stale'),  # click-to-focus
    ('reset',), ('surr', CE, 79, 76), ('check', 'stale'),  # 107.35 double-click
    ('reset',), ('surr', CE, 0, 0), ('surr', CE, 5, 0), ('surr', CE, 10, 0), ('surr', CE, 15, 0),
    ('surr', CE, 20, 0), ('surr', CE, 26, 0), ('check', (26, 0)),  # 108.12 drag
]

# Integration run final4 t=1.23-1.58 plus scenario 11: after a keyboard focus change Chrome sends
# nothing; one arrow key makes it push, and from then on clicks and double-clicks are trusted.
CHROME_MOUSE_AFTER_KEY = [
    ('reset',), ('reset',), ('blur',), ('focus', 'fake'), ('blur',), ('focus', 'gnome-shell'), ('caps', 9),
    ('type', 0, 64), ('check', 'stale'),
    ('key', keys.LEFT, 0), ('caps', 41), ('surr', TA, 110, 110), ('check', (110, 110)),
    ('reset',), ('surr', TA, 68, 68), ('check', (68, 68)),  # click
    ('reset',), ('surr', TA, 70, 67), ('check', (70, 67)),  # double-click selects KOT
]

# Review probe rv-spec/click_results_accept.json: focus on the Entry, TextView caret at offset 2,
# then one click on KOT (app caret 67) or a drag over 'słowo' in the TextView. At focus-in GTK
# pushes the TextView's state from BEFORE the click; GTK3 never pushes the click's caret, GTK4
# never pushes a drag, and a GTK4 click pushes the new caret about 2 ms later.
POINTER_FOCUS = [('reset',), ('reset',), ('reset',), ('blur',), ('focus', 'fake'), ('blur',),
                 ('focus', 'gnome-shell'), ('caps', 9), ('caps', 41), ('surr', P1, 2, 2)]
GTK3_CLICK_FOCUS = POINTER_FOCUS + [('check', 'stale')]
GTK4_DRAG_FOCUS = POINTER_FOCUS + [('check', 'stale')]
GTK4_CLICK_FOCUS = POINTER_FOCUS + [('check', 'stale'), ('surr', P2, 31, 31), ('check', (31, 31, 'unknown'))]

# Review probe rv-spec/gtk3_refocus_results.json: a GTK3 keyboard selection [36, 41] survives a
# switch to another window and back; GTK3 then pushes a collapsed caret (it never reports anchors).
GTK3_REFOCUS = [
    ('focus', 'gnome-shell'), ('caps', 41), ('key', keys.RIGHT, 0), ('surr', P2, 0, 0),
    *[event for i in range(1, 6) for event in (('key', Shift_L, 0), ('key', keys.RIGHT, keys.SHIFT), ('surr', P2, i, i))],
    ('check', (5, 5, 'hidden')),
    ('reset',), ('reset',), ('blur',), ('focus', 'fake'), ('blur',), ('focus', 'gnome-shell'), ('caps', 41),
    ('surr', P2, 5, 5), ('check', 'stale'),
    # Even a trusted update cannot show the selection: only a collapsing key or a reported anchor clears it.
    ('key', 0x61, keys.MOD1), ('surr', P2, 5, 5), ('check', (5, 5, 'unknown')),
    ('key', keys.LEFT, 0), ('surr', P2, 4, 4), ('check', (4, 4)),
]

# run2 GTK3 over text-input-v3 (gtk3.jsonl t=12.68-12.99): Shift+Right moves the caret, the
# anchor is never reported (app selection [67,70], engine 34/34).
GTK3_SHIFT = [
    ('focus', 'gnome-shell'), ('caps', 9), ('type', 8, 6144), ('type', 8, 0), ('type', 0, 0), ('caps', 41),
    ('surr', P3, 23, 23),
    ('key', keys.HOME, keys.CONTROL), ('surr', P1, 0, 0),
    ('key', keys.DOWN, 0), ('surr', P2, 0, 0),
    ('key', keys.HOME, 0), ('check', 'stale'),  # caret already there: no update
    *[event for i in range(1, 32) for event in (('key', keys.RIGHT, 0), ('surr', P2, i, i))],
    ('check', (31, 31)),
    *[event for i in range(32, 35) for event in (('key', Shift_L, 0), ('key', keys.RIGHT, keys.SHIFT),
                                                 ('surr', P2, i, i))],
    ('check', (34, 34, 'hidden')),
    ('key', keys.RIGHT, 0), ('surr', P2, 35, 35), ('check', (35, 35)),  # a plain arrow collapses it
]

# run2 GTK4 with GTK_IM_MODULE=ibus (t=682-690): the IM module re-sends the widget state right
# before each key event, so its update after mouse resets is the current state.
GTK4_IM_MOUSE = [
    ('caps', 41), ('focus', 'gtk4-im:testwin.py'),  # ibus-daemon re-sends no caps: unchanged
    ('surr', P3, 23, 23), ('check', (23, 23, 'unknown')),  # trusted; a selection may exist
    ('key', Control_L, 0), ('key', keys.END, keys.CONTROL), ('reset',), ('reset',),
    ('reset',), ('reset',), ('reset',), ('reset',), ('check', 'stale'),  # mouse double-click on KOT
    ('surr', P2, 31, 34), ('key', Control_L, 0), ('key', Alt_L, keys.CONTROL),
    ('check', (31, 34)),  # the hotkey's Control press refreshed it; the spike captured KOT correctly
]

# run2 t=699.67-699.80: with the IBus module, every caret move is key, reset, new state.
W19 = 'zażółć gęślą jaźń.\n' + P2
W13 = 'gęślą jaźń.\n' + P2
GTK4_IM_KEYS = [
    ('caps', 41), ('focus', 'gtk4-im:testwin.py'), ('surr', P3, 23, 23),
    ('key', Control_L, 0), ('key', keys.HOME, keys.CONTROL), ('reset',), ('reset',), ('surr', P1, 0, 0),
    ('check', (0, 0)),
    ('key', keys.DOWN, 0), ('reset',), ('surr', W19, 19, 19), ('check', (19, 19)),
    ('key', keys.HOME, 0), ('reset',), ('check', 'stale'),
    ('key', keys.RIGHT, 0), ('reset',), ('surr', W13, 13, 13), ('check', (13, 13)),
    *[event for i in range(14, 19) for event in (('key', keys.RIGHT, 0), ('reset',), ('surr', W13, i, i))],
    ('check', (18, 18)),
]


class ReplayTests(unittest.TestCase):
    def check(self, events):
        for step, want, got in replay(events):
            with self.subTest(step=step):
                self.assertEqual(got, want)

    def test_gtk4_mouse_echo_after_reset_is_stale(self):
        self.check(GTK4_MOUSE)

    def test_chrome_mouse_updates_after_reset_are_fresh(self):
        self.check(CHROME_MOUSE)
        self.check(CHROME_MOUSE_AFTER_KEY)

    def test_pointer_focus_never_trusts_the_focus_in_push(self):
        for name, events in (('gtk3 click', GTK3_CLICK_FOCUS), ('gtk4 drag', GTK4_DRAG_FOCUS),
                             ('gtk4 click', GTK4_CLICK_FOCUS)):
            with self.subTest(name):
                self.check(events)

    def test_hidden_selection_survives_a_window_switch(self):
        self.check(GTK3_REFOCUS)

    def test_gtk3_keyboard_selection_is_hidden(self):
        self.check(GTK3_SHIFT)

    def test_gtk_im_module_refreshes_at_key_time(self):
        self.check(GTK4_IM_MOUSE)
        self.check(GTK4_IM_KEYS)

    def test_same_key_reset_pattern_from_text_input_v3_stays_stale(self):
        # Why GTK IM modules are exempt: over text-input-v3 an update following an unanswered
        # reset may be the app's older state, so it is never trusted.
        events = [('focus', 'gnome-shell') if e == ('focus', 'gtk4-im:testwin.py') else e for e in GTK4_IM_KEYS]
        self.assertEqual(list(replay(events))[-1][2], 'stale')


class FreshnessTests(unittest.TestCase):
    def test_table(self):
        base = [('focus', 'gnome-shell'), ('caps', 41), ('surr', P2, 9, 9), ('key', keys.RIGHT, 0), ('surr', P2, 10, 10)]
        cases = [
            ('update answering a key is fresh', [], (10, 10)),
            ('reset with no push (GTK4 double-click)', [('reset',)], 'stale'),
            ('reset then the identical old state (echo)', [('reset',), ('surr', P2, 10, 10)], 'stale'),
            ('reset then a different state (Chrome)', [('reset',), ('surr', P2, 12, 12)], (12, 12)),
            ('echo then a real update', [('reset',), ('surr', P2, 10, 10), ('surr', P2, 12, 12)], (12, 12)),
            ('second reset with no answer: next update untrusted',
             [('reset',), ('reset',), ('surr', P2, 31, 34)], 'stale'),
            ('update after the untrusted one is fresh',
             [('reset',), ('reset',), ('surr', P2, 31, 34), ('surr', P2, 20, 20)], (20, 20)),
            ('typing awaits the update', [('key', 0x61, 0)], 'stale'),
            ('typing then update', [('key', 0x61, 0), ('surr', P2 + 'a', 11, 11)], (11, 11)),
            ('ctrl+a never reported (GTK4)', [('key', 0x61, keys.CONTROL)], 'stale'),
            ('ctrl+a then click: echo of the unseen selection',
             [('key', 0x61, keys.CONTROL), ('reset',), ('surr', P2, 0, len(P2))], 'stale'),
            ('ctrl+c changes nothing', [('key', 0x63, keys.CONTROL)], (10, 10)),
            ('modifiers change nothing', [('key', Shift_L, 0), ('key', Control_L, keys.SHIFT)], (10, 10)),
            ('super chord changes nothing', [('key', keys.LEFT, keys.SUPER | keys.MOD4)], (10, 10)),
            ('shift+arrow without any reported anchor', [('key', keys.RIGHT, keys.SHIFT), ('surr', P2, 11, 11)],
             (11, 11, 'hidden')),
            ('reported anchor: toolkit reports selections', [('surr', P2, 10, 12), ('surr', P2, 10, 10),
                                                            ('key', keys.RIGHT, keys.SHIFT), ('surr', P2, 11, 11)],
             (11, 11)),
            ('focus change drops the text', [('focus', 'gnome-shell')], 'stale'),
            ('first update after a focus change is not trusted', [('focus', 'gnome-shell'), ('surr', P1, 2, 2)], 'stale'),
            ('second update after a focus change: fresh, selection unknown',
             [('focus', 'gnome-shell'), ('surr', P1, 2, 2), ('surr', P2, 31, 31)], (31, 31, 'unknown')),
            ('key after a focus change: its update is trusted', [('focus', 'gnome-shell'), ('key', keys.LEFT, 0),
                                                                  ('surr', P2, 30, 30)], (30, 30)),
            ('reported selection after a focus change', [('focus', 'gnome-shell'), ('surr', P2, 30, 30),
                                                          ('surr', P2, 31, 34)], (31, 34)),
            ('reset after a focus change: next update untrusted', [('focus', 'gnome-shell'), ('reset',),
                                                                    ('surr', P2, 30, 30)], 'stale'),
            ('key before a reset does not vouch for the update after it',
             [('key', keys.LEFT, 0), ('reset',), ('surr', P2, 9, 9)], 'stale'),
            ('IM module client: first update trusted, selection unknown',
             [('focus', 'gtk4-im:testwin.py'), ('surr', P2, 5, 5)], (5, 5, 'unknown')),
            ('text-input-v3 app sends nothing after a caret key', [('focus', 'gnome-shell'), ('caps', 9),
                                                                   ('key', keys.LEFT, 0)], 'no_text'),
            ('direct IBus client without text', [('focus', 'gtk3-im:google-chrome'), ('caps', 9)], 'no_text'),
            ('fake client', [('focus', 'fake'), ('surr', P2, 1, 1)], 'no_field'),
            ('plain focus_in without id', [('focus', None), ('surr', P2, 1, 1)], 'no_field'),
            ('focus out', [('blur',)], 'no_field'),
            ('offsets out of range', [('surr', P2, 99, 99)], 'stale'),
            ('negative anchor', [('surr', P2, 1, -1)], 'stale'),
            ('no text', [('surr', None, 0, 0)], 'stale'),
            ('no text after a key', [('key', keys.LEFT, 0), ('surr', None, 0, 0)], 'no_text'),
            ('engine applied: stale until the echo', [('expect',)], 'stale'),
        ]
        for name, events, want in cases:
            with self.subTest(name):
                got = list(replay(base + events + [('check', None)]))[-1][2]
                self.assertEqual(got, want)


class PolicyTests(unittest.TestCase):
    def test_table(self):
        cases = [
            # (client, caps, purpose, hints, expected)
            ('gnome-shell', 41, 0, 0, (3, 3)),
            (None, 41, 0, 0, 'no_field'),
            ('fake', 41, 0, 0, 'no_field'),
            ('gnome-shell', 41, 8, 0, 'sensitive'),       # PASSWORD
            ('gnome-shell', 41, 9, 0, 'sensitive'),       # PIN
            ('gnome-shell', 41, 0, 2048, 'sensitive'),    # PRIVATE
            ('gnome-shell', 41, 0, 4096, 'sensitive'),    # HIDDEN_TEXT
            ('gnome-shell', 41, 8, 6144, 'sensitive'),    # Chrome password field
            ('gnome-shell', 41, 10, 0, 'terminal'),
            ('gtk3-im:google-chrome', 9, 0, 0, 'no_text'),  # Chrome under XWayland (run3 t=295.8)
            ('xim', 9, 0, 0, 'no_text'),
            ('gnome-shell', 41, 0, 64, (3, 3)),           # hints like LOWERCASE are harmless
        ]
        for client, caps, purpose, hints, want in cases:
            with self.subTest(client=client, caps=caps, purpose=purpose, hints=hints):
                ctx = FieldContext()
                ctx.focus_in(client)
                ctx.set_caps(caps)
                ctx.set_content_type(purpose, hints)
                ctx.key_passed(keys.RIGHT, 0)
                ctx.surrounding('abcdef', 3, 3)
                self.assertEqual(describe(ctx.snapshot()), want)

    def test_password_text_is_dropped_immediately(self):
        secret = 'test-password-123'
        for order in ('type first', 'text first'):
            with self.subTest(order):
                ctx = FieldContext()
                ctx.focus_in('gnome-shell')
                ctx.set_caps(41)
                if order == 'text first':
                    ctx.surrounding(secret, 3, 3)
                    self.assertTrue(ctx.set_content_type(8, 6144))
                else:
                    ctx.set_content_type(8, 6144)
                    ctx.surrounding(secret, 3, 3)
                self.assertIsNone(ctx.text)
                self.assertNotIn(secret, repr(ctx.status()) + repr(vars(ctx)))
                self.assertFalse(ctx.set_content_type(0, 64))  # Chrome's transient types on focus
                self.assertEqual(describe(ctx.snapshot()), 'stale')  # text-input-v3: wait for the next push

    def test_becoming_sensitive_drops_everything_derived_from_the_text(self):
        # Review probe: the text arrived before the purpose (a direct client), then a reset.
        ctx = FieldContext()
        ctx.focus_in('gtk3-im:app')
        ctx.set_caps(41)
        ctx.surrounding('tajneHasło1', 11, 11)
        ctx.reset()
        self.assertTrue(ctx.set_content_type(8, 0))
        self.assertEqual((ctx.text, ctx.before_reset, ctx.cursor, ctx.anchor), (None, None, 0, 0))
        self.assertNotIn('tajne', repr(vars(ctx)))
        self.assertNotIn(11, ctx.status().values())

    def test_caps_and_content_type_survive_focus_changes(self):
        # ibus-daemon (bus/engineproxy.c) only re-sends SetCapabilities/ContentType when they
        # differ from what it last sent this engine, e.g. none on the gtk4-im focus at run2 t=682.
        ctx = FieldContext()
        ctx.focus_in('gnome-shell')
        ctx.set_caps(41)
        ctx.set_content_type(8, 0)
        ctx.focus_out()
        ctx.focus_in('gnome-shell')
        ctx.surrounding('••••', 2, 2)
        self.assertEqual(describe(ctx.snapshot()), 'sensitive')
        self.assertIsNone(ctx.text)

    def test_focus_change_never_reuses_previous_text(self):
        ctx = FieldContext()
        ctx.focus_in('gnome-shell')
        ctx.set_caps(41)
        ctx.surrounding(TA, 5, 5)
        for event in (lambda: ctx.focus_in('gnome-shell'), lambda: ctx.focus_in('fake'), ctx.focus_out,
                      lambda: ctx.focus_in('gtk4-im:x')):
            event()
            self.assertIsNone(ctx.text)
            self.assertIsInstance(ctx.snapshot(), Refusal)

    def test_text_dropped_as_unchanged_is_kept_untrusted(self):
        # 2026-10-07, GNOME Text Editor after Alt+Tab to a window without a text field and back: GTK4 sends its
        # unchanged state, ibus-daemon drops it as identical to what it last sent this engine (bus/engineproxy.c),
        # and only GNOME Shell's caps rise shows that it was sent (js/misc/inputMethod.js, set_surrounding).
        ctx = FieldContext()
        ctx.can_confirm = True
        events = [('focus', 'gnome-shell'), ('caps', 41), ('key', keys.RIGHT, 0), ('surr', TA, 40, 40), ('check', (40, 40)),
                  ('reset',), ('reset',), ('blur',), ('focus', 'fake'), ('caps', 41), ('blur',),
                  ('focus', 'gnome-shell'), ('caps', 9), ('type', 0, 4), ('caps', 41), ('check', 'stale')]
        for step, want, got in replay(events, ctx):
            self.assertEqual(got, want, 'step %d' % step)
        self.assertIsNone(ctx.text)  # the text may still be on its way
        self.assertTrue(ctx.settle())  # ibus-daemon's next message was not the text: it was dropped
        self.assertEqual((ctx.text, ctx.cursor, ctx.anchor, ctx.fresh), (TA, 40, 40, False))
        self.assertTrue(ctx.confirmable())  # AT-SPI may make it current (ConfirmCaret)
        self.assertEqual(ctx.confirm(ctx.version, 40, 40), 'ok')
        self.assertEqual(describe(ctx.snapshot()), (40, 40))
        self.assertFalse(ctx.settle())  # once

    def test_text_kept_as_unchanged_never_makes_a_later_update_trusted(self):
        # Were the text only late, the update that follows is still the first after the focus change.
        ctx = FieldContext()
        events = [('focus', 'gnome-shell'), ('caps', 41), ('key', keys.RIGHT, 0), ('surr', TA, 40, 40),
                  ('blur',), ('focus', 'gnome-shell'), ('caps', 9), ('caps', 41)]
        list(replay(events, ctx))
        self.assertTrue(ctx.settle())
        ctx.surrounding(P2, 3, 3)
        self.assertEqual(describe(ctx.snapshot()), 'stale')

    def test_text_is_not_taken_as_unchanged(self):
        focused = [('focus', 'gnome-shell'), ('caps', 9), ('caps', 41)]
        cases = (
            ('the text came after the caps', focused + [('surr', P3, 2, 2)], P3),
            ('caps raised without being lowered in this focus', [('caps', 9), ('focus', 'gnome-shell'), ('caps', 41)], None),
            ('a focus change before the next message', focused + [('focus', 'gnome-shell')], None),
            ('a password field', [('focus', 'gnome-shell'), ('type', 8, 0), ('caps', 9), ('caps', 41)], None),
        )
        for name, events, text in cases:
            with self.subTest(name):
                ctx = FieldContext()
                list(replay([('focus', 'gnome-shell'), ('caps', 41), ('surr', TA, 5, 5), ('blur',)] + events, ctx))
                self.assertFalse(ctx.settle())
                self.assertEqual(ctx.text, text)
        for name, client, last in (('an in-process client', 'gtk4-im:x', (TA, 5, 5)), ('an empty paragraph', 'gnome-shell', ('', 0, 0)),
                                   ('a secret was the last', 'gnome-shell', None)):
            with self.subTest(name):
                ctx = FieldContext()
                ctx.forwarded = last
                list(replay([('focus', client), ('caps', 9), ('caps', 41)], ctx))
                self.assertFalse(ctx.settle())
                self.assertIsNone(ctx.text)

    def test_a_secret_is_never_kept_as_the_last_text(self):
        ctx = FieldContext()
        list(replay([('focus', 'gnome-shell'), ('caps', 41), ('type', 8, 0), ('surr', 'tajneHasło1', 11, 11)], ctx))
        self.assertIsNone(ctx.forwarded)
        ctx = FieldContext()
        list(replay([('focus', 'gnome-shell'), ('caps', 41), ('surr', 'tajneHasło1', 11, 11), ('type', 8, 0)], ctx))
        self.assertIsNone(ctx.forwarded)
        self.assertNotIn('tajne', repr(vars(ctx)))

    def test_status_has_no_text(self):
        ctx = FieldContext()
        ctx.focus_in('gnome-shell')
        ctx.set_caps(41)
        ctx.surrounding('tajny akapit', 3, 3)
        self.assertNotIn('tajny', repr(ctx.status()))
        self.assertEqual(ctx.status()['text_len'], 12)


if __name__ == '__main__':
    unittest.main()
