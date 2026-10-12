import unittest
from unittest import mock
from gramadrama import core
from gramadrama.engine import applist, fallback_ui, keys, live, plan, session
from gramadrama.engine.fallback_ui import View
from gramadrama.engine.session import (APPLYING, CONFIRM_MS, ERROR, IDLE, IDLE_TIMEOUT_MS, READY, WAITING, Apply,
                                       CancelBackend, ChainUp, ConfirmCaret, Consume, EngineLogic, HideUI, Log, Message,
                                       OpenApp, ResetCompose, ScheduleConfirmTimeout, ScheduleIdleTimeout,
                                       ScheduleVerifyTimeout, ShowUI, StartBackend, revert_segments, word_change, MODE_KEYS)

P1 = 'Pierwszy akapit: zażółć gęślą jaźń.'
P2 = 'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.'
P3 = 'Trzeci akapit — koniec.'
TA = '\n'.join((P1, P2, P3))
IN_P2 = len(P1) + 1 + 10  # caret inside P2
g, j, one, two, six, d, a, aogonek = 0x67, 0x6a, 0x31, 0x32, 0x36, 0x64, 0x61, 0x1b1
d_key = d
Shift_L, Control_L, Alt_L = 0xffe1, 0xffe3, 0xffe9
CODES = {g: 42, j: 44, keys.RETURN: 36, keys.KP_ENTER: 104, keys.ESCAPE: 9, one: 10, two: 11, six: 15, d: 40, a: 38, aogonek: 38,
         keys.PAGE_UP: 112, keys.PAGE_DOWN: 117, Shift_L: 50, Control_L: 37, Alt_L: 64,
         keys.ISO_LEVEL3_SHIFT: 108, keys.RIGHT: 114, keys.LEFT: 113, keys.END: 115, keys.HOME: 110, keys.UP: 111,
         keys.DOWN: 116, keys.BACKSPACE: 22, keys.DELETE: 119, keys.KEY_e: 26, keys.KEY_z: 52, keys.KEY_c: 54,
         keys.KEY_x: 53, keys.KEY_v: 55, keys.KEY_i: 31}
KEYVALS = {'ą': 0x1b1, 'ę': 0x1ea, 'ś': 0x1b6}  # Latin-2 keysyms of the Polish layout (AltGr)
HOTKEY = (g, keys.CONTROL | keys.MOD1)
HOTKEY_PRESS = (g, CODES[g], keys.CONTROL | keys.MOD1)


def plain(effects):
    return [e for e in effects if not isinstance(e, Log)]


def rows(text):
    return tuple(fallback_ui.lines((('added', text),)))


class Driver:
    """Feeds EngineLogic like the adapter does; UI effects are drawn at once (ui_flushed). focus, disabled
    and modes stand for the process's focused app (the extension's Focus) and apps.json."""

    def __init__(self, text=TA, cursor=IN_P2, anchor=None, client='gnome-shell', focus=None, disabled=(), modes=None,
                 panel=False):
        self.logic = EngineLogic()
        self.logs = []
        self.panel = panel  # the session is drawn in the GNOME panel, which can show an edited result
        self.focus, self.disabled, self.modes = focus, frozenset(disabled), dict(modes or {})
        self.text = text
        self.run(self.logic.focus_in(client))
        self.run(self.logic.capabilities(41))
        if text is not None:
            self.tap(keys.RIGHT)  # the update that answers a caret key is trusted
            self.run(self.logic.surrounding(text, cursor, cursor if anchor is None else anchor))

    def run(self, effects, draw=True):
        self.logs += [e for e in effects if isinstance(e, Log)]
        ui = [e for e in effects if isinstance(e, (ShowUI, HideUI, Message))]
        if ui and draw:
            self.logic.ui_flushed(isinstance(ui[-1], ShowUI), panel=self.panel, problems=self.panel)
        return plain(effects)

    def press(self, keyval, state=0):
        return self.run(self.logic.key(keyval, CODES[keyval], state, self.focus, self.disabled, self.modes))

    def release(self, keyval, state=0):
        return self.run(self.logic.key(keyval, CODES[keyval], state | keys.RELEASE, self.focus, self.disabled,
                                       self.modes))

    def tap(self, keyval, state=0):
        effects = self.press(keyval, state)
        self.release(keyval, state)
        return effects

    def type(self, text):
        """Characters as the adapter passes them (IBus.keyval_to_unicode), each pressed and released."""
        effects = []
        for ch in text:
            keyval = KEYVALS.get(ch, ord(ch))
            effects = self.run(self.logic.key(keyval, 38, 0, self.focus, self.disabled, self.modes, char=ch))
            self.run(self.logic.key(keyval, 38, keys.RELEASE, self.focus, self.disabled, self.modes))
        return effects

    def echo(self, ops, **kw):
        """The client applies ops and reports its new state."""
        self.text = self.text[:ops.start] + ops.replacement + self.text[ops.end:]
        cursor = ops.start + len(ops.replacement)
        return self.run(self.logic.surrounding(self.text, cursor, cursor))

    def hotkey(self, key=g):
        """Ctrl+Alt+G, or Ctrl+Alt+J with key=j."""
        self.press(Control_L)
        self.press(Alt_L, keys.CONTROL)
        effects = self.press(key, keys.CONTROL | keys.MOD1)
        self.release(key, keys.CONTROL | keys.MOD1)
        self.release(Alt_L, keys.CONTROL | keys.MOD1)
        self.release(Control_L, keys.CONTROL)
        return effects

    def result(self, corrected, sid=None):
        sid = self.logic.session.id if sid is None else sid
        return self.run(self.logic.backend_result(sid, corrected, rows(corrected)))

    @property
    def state(self):
        return self.logic.session.state


class RefusalTests(unittest.TestCase):
    def test_refusals_show_a_message_and_are_not_modal(self):
        def sensitive(d):
            d.run(d.logic.content_type(8, 6144))

        cases = [
            ('no text yet (text-input-v3)', dict(text=None), None, 'stale'),
            ('no text after a caret key', dict(text=None), lambda d: d.press(keys.LEFT), 'no_text'),
            ('fake client', dict(client='fake'), None, 'no_field'),
            ('chrome x11 caps 9', dict(client='gtk3-im:google-chrome'), lambda d: d.run(d.logic.capabilities(9)), 'no_text'),
            ('stale after a click', {}, lambda d: d.run(d.logic.reset()), 'stale'),
            ('stale after typing', {}, lambda d: d.press(a), 'stale'),
            ('password', {}, sensitive, 'sensitive'),
            ('terminal', {}, lambda d: d.run(d.logic.content_type(10, 0)), 'terminal'),
            ('empty paragraph', dict(text='a\n\nb', cursor=2), None, 'empty'),
            ('hidden selection', {}, lambda d: (d.press(keys.RIGHT, keys.SHIFT),
                                                d.run(d.logic.surrounding(TA, IN_P2 + 1, IN_P2 + 1))), 'hidden_selection'),
            ('selection unknown after a focus change', dict(text=None),
             lambda d: (d.run(d.logic.surrounding(TA, IN_P2, IN_P2)), d.run(d.logic.surrounding(TA, IN_P2 + 1, IN_P2 + 1))),
             'selection_unknown'),
        ]
        for name, setup, action, reason in cases:
            with self.subTest(name):
                d = Driver(**setup)
                if action:
                    action(d)
                self.assertEqual(d.hotkey(), [ResetCompose(), Message(reason, 3), Consume()])
                self.assertEqual(d.state, IDLE)
                self.assertFalse(d.logic.session.is_modal)
                self.assertEqual(d.press(keys.RETURN), [ChainUp()])
                self.assertEqual(d.press(keys.ESCAPE), [ChainUp()])

    def test_gtk4_mouse_drag_regression(self):
        # run2: double-click KOT, drag in P1, then the hotkey (critique C2). Must refuse, never capture.
        d = Driver(text=P3, cursor=23)
        for event in [('reset',), ('surr', P2, 33, 33), ('reset',), ('reset',), ('surr', P2, 31, 34)]:
            d.run(d.logic.reset() if event[0] == 'reset' else d.logic.surrounding(*event[1:]))
        self.assertEqual(d.hotkey(), [ResetCompose(), Message('stale', 3), Consume()])
        d.press(keys.RIGHT)  # an arrow key makes GTK push the real caret again
        d.run(d.logic.surrounding(P1 + '\nDrugi', 24, 24))
        self.assertEqual(d.hotkey()[1], StartBackend(1, P1, 'fix'))

    def test_gtk3_selection_refused_then_caret_paragraph_works(self):
        d = Driver(text=P2, cursor=31)
        for i in range(32, 35):
            d.press(Shift_L)
            d.press(keys.RIGHT, keys.SHIFT)
            d.run(d.logic.surrounding(P2, i, i))
        self.assertEqual(d.hotkey()[1], Message('hidden_selection', 3))
        d.press(keys.RIGHT)
        d.run(d.logic.surrounding(P2, 35, 35))
        self.assertEqual(d.hotkey()[1], StartBackend(1, P2, 'fix'))

    def test_first_focus_never_uses_previous_text(self):
        d = Driver()  # GTK text is fresh
        d.run(d.logic.focus_out())
        d.run(d.logic.focus_in('gnome-shell'))
        d.run(d.logic.capabilities(9))
        self.assertEqual(d.hotkey()[1], Message('stale', 3))  # Chrome sends its text on the first change
        d.run(d.logic.capabilities(41))
        self.assertEqual(d.hotkey()[1], Message('stale', 3))
        d.run(d.logic.surrounding('Tekst z Chrome.', 3, 3))  # e.g. a click-to-focus push: not trusted
        self.assertEqual(d.hotkey()[1], Message('stale', 3))
        d.tap(keys.LEFT)  # as the message says
        d.run(d.logic.surrounding('Tekst z Chrome.', 2, 2))
        self.assertEqual(d.hotkey()[1], StartBackend(1, 'Tekst z Chrome.', 'fix'))

    def test_click_to_focus_is_refused_and_enter_reaches_the_app(self):
        # Review probe (GTK3): the Entry had focus; a click on KOT focuses the TextView. GTK pushes
        # the TextView's caret from before the click (paragraph 1) and never the new one.
        d = Driver(text=P3, cursor=5)
        d.run(d.logic.focus_out())
        d.run(d.logic.focus_in('gnome-shell'))
        d.run(d.logic.surrounding(P1, 2, 2))
        self.assertEqual(d.hotkey(), [ResetCompose(), Message('stale', 3), Consume()])
        self.assertEqual(d.tap(keys.RETURN), [ChainUp()])
        self.assertEqual(d.state, IDLE)

    def test_gtk3_selection_after_a_window_switch_is_refused(self):
        # Review probe: Shift+Right x5 in GTK3, another window, back; GTK3 keeps the selection.
        d = Driver(text=P2, cursor=0)
        for i in range(1, 6):
            d.press(keys.RIGHT, keys.SHIFT)
            d.run(d.logic.surrounding(P2, i, i))
        self.assertEqual(d.hotkey()[1], Message('hidden_selection', 3))
        for event in (d.logic.reset, d.logic.focus_out, lambda: d.logic.focus_in('gnome-shell')):
            d.run(event())
        d.run(d.logic.surrounding(P2, 5, 5))
        self.assertEqual(d.hotkey()[1], Message('stale', 3))
        d.press(0x61, keys.MOD1)  # a key that may leave the selection alone, and its update
        d.run(d.logic.surrounding(P2, 5, 5))
        self.assertEqual(d.hotkey()[1], Message('selection_unknown', 3))
        d.tap(keys.LEFT)  # collapses any selection
        d.run(d.logic.surrounding(P2, 4, 4))
        self.assertEqual(d.hotkey()[1], StartBackend(1, P2, 'fix'))

    def test_password_text_never_reaches_effects(self):
        d = Driver(text=None)
        d.run(d.logic.content_type(8, 6144))
        d.run(d.logic.surrounding('sekret-testowy-123', 3, 3))
        effects = d.logic.key(g, 42, keys.CONTROL | keys.MOD1)
        self.assertIn(Message('sensitive', 3), effects)
        self.assertNotIn('sekret', repr(effects) + repr(d.logic.status()))


class ConfirmCaretTests(unittest.TestCase):
    """A hotkey that doubts only the caret asks AT-SPI (the live service) where it is now, in the text IBus
    has: GTK4 over text-input-v3 after a click whose update came after another click that sent none
    (ibus-daemon drops an unchanged state), 2026-10-02 in GNOME Text Editor. It goes on only when AT-SPI
    sees the state IBus has: mutter deletes around the caret the app last sent."""

    def clicked(self, cursor=5, anchor=None, **kw):
        """A click that left the state as ibus-daemon last sent it: a reset and no update after it, then
        a second click whose update cannot be trusted (before_reset unknown)."""
        d = Driver(**kw)
        d.logic.ctx.can_confirm = True
        d.run(d.logic.reset())
        d.run(d.logic.reset())
        d.run(d.logic.surrounding(d.text, cursor, cursor if anchor is None else anchor))
        return d

    def asked(self, d):
        """The hotkey's question; its id (one numbering for the process)."""
        before = len(d.logs)
        effects = d.hotkey()
        cid = effects[1].id if isinstance(effects[1], ConfirmCaret) else None
        self.assertEqual(effects, [ResetCompose(), ConfirmCaret(cid, TA), ScheduleConfirmTimeout(cid, CONFIRM_MS), Consume()])
        self.assertEqual(d.logs[before:], [])  # nothing refused yet
        return cid

    def test_atspi_sees_the_state_ibus_has(self):
        d = self.clicked()
        cid = self.asked(d)
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5))[0], StartBackend(1, P1, 'fix'))
        self.assertEqual(d.state, WAITING)
        self.assertEqual(d.logs[0], Log('caret_confirmed', {'reason': 'stale', 'app': None}))
        self.assertEqual(d.run(d.logic.confirm_timeout(cid)), [])  # the timer after the answer

    def test_a_selection(self):
        start = len(P1) + 1
        d = self.clicked(start + 5, start)
        cid = self.asked(d)
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, start + 5, start))[0], StartBackend(1, P2[:5], 'fix'))

    def test_a_focus_change_whose_first_update_was_current(self):
        """The first update after a focus change may be the state from before the click that focused the
        field; when AT-SPI sees the same, the click moved nothing, and the selection is known too."""
        d = Driver()
        d.logic.ctx.can_confirm = True
        d.run(d.logic.focus_out())
        d.run(d.logic.focus_in('gnome-shell'))
        d.run(d.logic.surrounding(TA, IN_P2, IN_P2))
        cid = self.asked(d)
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, IN_P2, IN_P2))[0], StartBackend(1, P2, 'fix'))

    def test_a_window_switch_whose_text_ibus_dropped_as_unchanged(self):
        """2026-10-07, GNOME Text Editor: Alt+Tab to a window without a text field and back. The app sends its
        state again, unchanged, and ibus-daemon drops it; the shell's caps rise shows it was sent. Before, the
        engine had no text and refused without asking AT-SPI."""
        d = Driver()
        d.logic.ctx.can_confirm = True
        for event in (d.logic.reset, d.logic.focus_out, lambda: d.logic.focus_in('fake'), lambda: d.logic.capabilities(41),
                      d.logic.focus_out, lambda: d.logic.focus_in('gnome-shell'), lambda: d.logic.capabilities(9),
                      lambda: d.logic.content_type(0, 4), lambda: d.logic.capabilities(41)):
            d.run(event())
        before = len(d.logs)
        effects = d.hotkey()
        cid = effects[1].id if isinstance(effects[1], ConfirmCaret) else None
        self.assertEqual(effects, [ResetCompose(), ConfirmCaret(cid, TA), ScheduleConfirmTimeout(cid, CONFIRM_MS), Consume()])
        self.assertEqual(d.logs[before:], [Log('surrounding_unchanged', {})])
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, IN_P2, IN_P2))[0], StartBackend(1, P2, 'fix'))

    def test_atspi_sees_another_state(self):
        """The app has not sent its state (a drag, Ctrl+A, the pre-click state at a focus change): a
        replacement through mutter would land beside the text meant, so the old refusal stands."""
        start = len(P1) + 1
        for name, (cursor, anchor) in (('another caret', (IN_P2, IN_P2)), ('a selection IBus never saw', (start + 5, start)),
                                       ('no selection where IBus has one', (5, 5))):
            with self.subTest(name):
                d = self.clicked(5, 9 if name == 'no selection where IBus has one' else 5)
                cid = self.asked(d)
                self.assertEqual(d.run(d.logic.caret_confirmed(cid, cursor, anchor)), [Message('stale', 3)])
                self.assertEqual(d.logs[-1].data['atspi'], 'differs')
                self.assertFalse(d.logic.ctx.fresh)

    def test_unknown_or_late_is_the_old_message(self):
        d = self.clicked()
        cid = self.asked(d)
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, None, None)), [Message('stale', 3)])
        self.assertEqual(d.logs[-1].data['atspi'], 'unknown')
        cid = self.asked(d)
        self.assertEqual(d.run(d.logic.confirm_timeout(cid)), [Message('stale', 3)])
        self.assertEqual(d.logs[-1].data['atspi'], 'timeout')
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5)), [])  # after the timeout: nothing
        self.assertEqual(d.state, IDLE)

    def test_moved_on_meanwhile(self):
        cases = (('key', lambda d: d.press(a)),
                 ('reset', lambda d: d.run(d.logic.reset())),
                 ('focus', lambda d: (d.run(d.logic.focus_out()), d.run(d.logic.focus_in('gnome-shell')))),
                 ('hotkey', lambda d: d.hotkey()))
        for why, step in cases:
            with self.subTest(why):
                d = self.clicked()
                cid = self.asked(d)
                step(d)
                self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5)), [])
                self.assertEqual(d.state, IDLE)
                self.assertEqual(d.logs[0], Log('caret_dropped', {'why': why}))  # never silent in the log
        d = self.clicked()
        cid = self.asked(d)
        d.press(Shift_L)  # a modifier alone: still waiting
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5))[0], StartBackend(1, P1, 'fix'))

    def test_an_answer_about_an_older_state_is_void(self):
        """The field changed after the question: AT-SPI read the state before it, so the hotkey goes on with
        what IBus says now, trusted (an update after the click's own) or not (a click meanwhile)."""
        d = self.clicked()
        cid = self.asked(d)
        d.run(d.logic.surrounding(TA, IN_P2, IN_P2))  # trusted: the click's update has used up after_reset
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5))[0], StartBackend(1, P2, 'fix'))
        d = self.clicked()
        cid = self.asked(d)
        d.logic.ctx.reset()  # a click that the session never heard of: the field's state is in doubt again
        self.assertEqual(d.run(d.logic.caret_confirmed(cid, 5, 5)), [Message('stale', 3)])
        self.assertEqual(d.logs[-1].data['atspi'], 'changed')

    def test_never_asked(self):
        cases = (('the adapter cannot ask', dict(), lambda d: setattr(d.logic.ctx, 'can_confirm', False)),
                 ('an in-process GTK3 client', dict(client='gtk3-im:gedit'), None),
                 ('no text yet', dict(text=None), None),
                 ('a terminal', dict(), lambda d: d.run(d.logic.content_type(10, 0))))
        for name, kw, step in cases:
            with self.subTest(name):
                d = Driver(**kw)
                d.logic.ctx.can_confirm = True
                d.run(d.logic.reset())
                d.run(d.logic.reset())
                if kw.get('text', TA) is not None:
                    d.run(d.logic.surrounding(TA, 5, 5))
                if step:
                    step(d)
                effects = d.hotkey()
                self.assertFalse([e for e in effects if isinstance(e, ConfirmCaret)], effects)

    def test_a_password_field_is_never_asked_about(self):
        d = self.clicked()
        d.run(d.logic.content_type(8, 0))
        self.assertEqual(d.hotkey()[1], Message('sensitive', 3))


# macOS DiffRevertTests (tests/GramaCoreTests/DiffRevertTests.swift): the same sentence and the same cases.
NAMES = 'Jutro wysle raport do Kowalsky i w piatek omowimy plan.'
NAMES_FIXED = 'Jutro wyślę raport do Kowalskiego i w piątek omówimy plan.'


def diffed(original, corrected):
    segments = tuple(core.diff_parts(original, corrected))
    return segments, core.changes(segments, original)


def result_of(segments):
    return ''.join(text for kind, text in segments if kind != 'removed')


class RevertSegmentsTests(unittest.TestCase):
    def test_each_change_reverts_on_its_own(self):
        segments, changes = diffed(NAMES, NAMES_FIXED)
        name = next(c for c in changes if c['from'] == 'Kowalsky')
        self.assertTrue(word_change(name))
        self.assertEqual(result_of(revert_segments(segments, {tuple(name['segments'])})),
                         'Jutro wyślę raport do Kowalsky i w piątek omówimy plan.')  # the other corrections stay
        self.assertEqual(result_of(revert_segments(segments, set())), NAMES_FIXED)
        self.assertEqual(result_of(revert_segments(segments, {tuple(c['segments']) for c in changes})), NAMES)

    def test_a_word_with_an_added_comma_is_one_change(self):
        segments, changes = diffed('do Kowalsky a jutro', 'do Kowalskiego, a jutro')
        self.assertEqual((changes[0]['from'], changes[0]['to']), ('Kowalsky', 'Kowalskiego,'))
        self.assertFalse(word_change(changes[0]))
        self.assertEqual(result_of(revert_segments(segments, {tuple(changes[0]['segments'])})), 'do Kowalsky a jutro')

    def test_a_group_across_one_space_reverts_whole(self):
        segments, changes = diffed('w cale nie', 'wcale nie')
        self.assertFalse(word_change(changes[0]))  # two words in the original: Cofnij only
        self.assertEqual(result_of(revert_segments(segments, {tuple(changes[0]['segments'])})), 'w cale nie')

    def test_punctuation_is_no_word_change(self):
        _, changes = diffed('tak a nie', 'tak, a nie')
        self.assertEqual(len(changes), 1)
        self.assertFalse(word_change(changes[0]))


class RevertTests(unittest.TestCase):
    """The panel's Cofnij and Zostaw i dodaj do słownika at a change (branding/PANEL.md, Lista zmian)."""

    def ready(self, original=NAMES, corrected=NAMES_FIXED):
        d = Driver(text=original, cursor=3)
        self.assertEqual(d.hotkey()[1], StartBackend(1, original, 'fix'))
        segments, changes = diffed(original, corrected)
        info = {'segments': [list(s) for s in segments], 'changes': changes, 'count': len(changes)}
        d.run(d.logic.backend_result(1, corrected, tuple(fallback_ui.lines(segments)), info))
        return d, {c['from']: c['segments'][0] for c in changes}

    def test_undo_one_change(self):
        d, ids = self.ready()
        rows = d.logic.session.panel_view()['changes']
        # two words across one space are one change, Cofnij only (as on macOS)
        self.assertEqual([(r['from'], r['id'], r['keep']) for r in rows],
                         [('wysle', ids['wysle'], True), ('Kowalsky', ids['Kowalsky'], True),
                          ('piatek omowimy', ids['piatek omowimy'], False)])
        effects = d.run(d.logic.action(1, 'revert:%d' % ids['Kowalsky']))
        undone = 'Jutro wyślę raport do Kowalsky i w piątek omówimy plan.'
        self.assertEqual(effects, [ShowUI(View(0, 'ready', tuple(fallback_ui.lines(diffed(NAMES, undone)[0]))))])
        self.assertEqual(d.logs[-1], Log('revert', {'id': 1, 'keep': False, 'count': 2}))  # never the word
        view = d.logic.session.panel_view()
        self.assertEqual((view['text'], view['count']), (undone, 2))
        self.assertEqual([r['from'] for r in view['changes']], ['wysle', 'piatek omowimy'])
        self.assertEqual(result_of(view['segments']), undone)
        self.assertEqual(d.run(d.logic.action(1, 'revert:%d' % ids['Kowalsky'])), [])  # a second click: nothing more

    def test_keep_adds_the_word_to_the_dictionary(self):
        d, ids = self.ready()
        effects = d.logic.action(1, 'keep:%d' % ids['Kowalsky'])
        self.assertEqual(effects[-1], live.Command({'cmd': 'learn', 'word': 'Kowalsky', 'rule': ''}))
        self.assertEqual(d.logic.session.result, 'Jutro wyślę raport do Kowalsky i w piątek omówimy plan.')
        self.assertNotIn('Kowalsky', repr([e for e in effects if isinstance(e, Log)]))

    def test_keep_is_only_for_a_word_changed_to_another(self):
        d, ids = self.ready('do Kowalsky a jutro', 'do Kowalskiego, a jutro')
        self.assertFalse(d.logic.session.panel_view()['changes'][0]['keep'])
        self.assertEqual(d.logic.action(1, 'keep:%d' % ids['Kowalsky']), [])
        self.assertEqual(d.logic.session.result, 'do Kowalskiego, a jutro')

    def test_enter_inserts_the_result_with_the_change_undone(self):
        d, ids = self.ready()
        d.run(d.logic.action(1, 'revert:%d' % ids['piatek omowimy']))
        effects = d.press(keys.RETURN)
        self.assertIsInstance(effects[0], Apply)
        self.assertEqual(d.echo(effects[0].ops), [Message('applied', 1.5)])
        self.assertEqual(d.text, 'Jutro wyślę raport do Kowalskiego i w piatek omowimy plan.')

    def test_reasons_after_an_undo_keep_it_undone(self):
        d, ids = self.ready()
        d.run(d.logic.action(1, 'revert:%d' % ids['piatek omowimy']))
        segments, changes = diffed(NAMES, NAMES_FIXED)
        explained = [dict(c, reason='Z LanguageTool') for c in changes]
        d.run(d.logic.backend_changes(1, explained, len(explained)))
        view = d.logic.session.panel_view()
        self.assertEqual([(r['from'], r['reason']) for r in view['changes']],
                         [('wysle', 'Z LanguageTool'), ('Kowalsky', 'Z LanguageTool')])
        self.assertEqual(view['count'], 2)
        self.assertEqual(view['text'], 'Jutro wyślę raport do Kowalskiego i w piatek omowimy plan.')

    def test_all_undone_is_unchanged(self):
        d, ids = self.ready()
        for change in ids.values():
            d.run(d.logic.action(1, 'revert:%d' % change))
        self.assertEqual(d.logic.session.view().status, 'unchanged')
        self.assertEqual((d.logic.session.result, d.logic.session.panel_view()['changes']), (NAMES, []))
        self.assertEqual(d.press(keys.RETURN), [HideUI(), Consume()])  # nothing to insert
        self.assertEqual(d.state, IDLE)

    def test_only_on_a_finished_correction(self):
        d = Driver(text=NAMES, cursor=3)
        d.hotkey()
        self.assertEqual(d.logic.action(1, 'revert:1'), [])  # still waiting for the model
        d, ids = self.ready()
        self.assertEqual(d.logic.action(2, 'revert:%d' % ids['wysle']), [])  # another session's panel
        self.assertEqual(d.logic.action(1, 'revert:x'), [])
        self.assertEqual(d.logic.action(1, 'revert:99'), [])
        d.press(two, keys.CONTROL)  # another mode starts over: nothing undone, no ids until its result
        self.assertEqual(d.logic.session.reverted, frozenset())
        self.assertNotIn('id', d.logic.session.panel_view()['changes'][0] if d.logic.session.changes else {})



class EditingTests(unittest.TestCase):
    """Editing the result in the panel (branding/PANEL.md, Edycja): the engine is the editor, the panel draws it."""

    def ready(self, panel=True):
        d = Driver(text=NAMES, cursor=3, panel=panel)
        d.hotkey()
        segments, changes = diffed(NAMES, NAMES_FIXED)
        info = {'segments': [list(x) for x in segments], 'changes': changes, 'count': len(changes)}
        d.run(d.logic.backend_result(1, NAMES_FIXED, tuple(fallback_ui.lines(segments)), info))
        return d

    def editing(self, d):
        b = d.logic.session.editing
        return None if b is None else (b.text, b.cursor, b.anchor)

    def test_ctrl_e_edits_and_esc_makes_the_diff_again(self):
        d = self.ready()
        self.assertTrue(d.logic.session.panel_view()['editable'])
        effects = d.tap(keys.KEY_e, keys.CONTROL)
        self.assertEqual(effects[-1], Consume())
        end = len(NAMES_FIXED)
        self.assertEqual(self.editing(d), (NAMES_FIXED, end, end))
        view = d.logic.session.panel_view()
        # what the edited text changes against the original is marked, as in the result: wyślę, Kowalskiego, piątek omówimy
        marks = [[6, 11], [22, 33], [38, 44], [45, 52]]
        self.assertEqual((view['editing'], view['segments'], view['changes']),
                         ({'cursor': end, 'anchor': end, 'checking': False, 'marks': marks}, [], []))
        self.assertEqual([NAMES_FIXED[a:b] for a, b in marks], ['wyślę', 'Kowalskiego', 'piątek', 'omówimy'])
        d.tap(keys.LEFT)  # before the full stop
        d.type(' i jutro')
        self.assertEqual(self.editing(d)[0], 'Jutro wyślę raport do Kowalskiego i w piątek omówimy plan i jutro.')
        self.assertEqual(d.tap(keys.ESCAPE)[-1], Consume())  # ends editing, the panel stays
        s = d.logic.session
        self.assertEqual((s.editing, s.state, s.result), (None, READY, 'Jutro wyślę raport do Kowalskiego i w piątek omówimy plan i jutro.'))
        self.assertEqual([c['from'] for c in s.changes], ['wysle', 'Kowalsky', 'piatek omowimy', ''])
        self.assertEqual(s.changes[-1]['to'], 'i jutro')
        self.assertEqual(result_of(s.segments), s.result)
        self.assertIn(Log('edit_done', {'id': 1, 'changed': True}), d.logs)  # never the text
        d.tap(keys.ESCAPE)  # the next Esc closes the panel
        self.assertEqual(d.state, IDLE)

    def test_typing_in_a_result_starts_editing_with_that_key(self):
        d = self.ready()
        d.type('!')
        self.assertEqual(self.editing(d)[0], NAMES_FIXED + '!')
        d = self.ready()
        d.tap(keys.BACKSPACE)
        self.assertEqual(self.editing(d)[0], NAMES_FIXED[:-1])

    def test_enter_inserts_the_edited_text(self):
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.tap(keys.LEFT, keys.CONTROL)  # by word: before "plan"
        d.type('ten ')
        d.type('ąę')  # AltGr letters
        effects = d.press(keys.RETURN)
        self.assertIsInstance(effects[0], Apply)
        self.assertEqual(d.echo(effects[0].ops), [Message('applied', 1.5)])
        self.assertEqual(d.text, 'Jutro wyślę raport do Kowalskiego i w piątek omówimy ten ąęplan.')

    def test_shift_enter_is_a_new_line_and_modes_wait(self):
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.tap(keys.RETURN, keys.SHIFT)
        self.assertEqual(self.editing(d)[0], NAMES_FIXED + '\n')
        self.assertEqual(d.press(two, keys.CONTROL), [Consume()])  # Ulepsz would drop the edits
        self.assertEqual(d.logic.action(1, 'mode:2'), [])
        self.assertEqual(d.state, READY)
        d.run(d.logic.action(1, 'edit'))  # Gotowe
        self.assertIsNone(d.logic.session.editing)
        self.assertEqual(d.logic.session.result, NAMES_FIXED + '\n')

    def test_held_keys_repeat_and_undo(self):
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.press(keys.BACKSPACE)
        d.press(keys.BACKSPACE)  # auto-repeat of the held key
        d.press(keys.BACKSPACE)
        d.release(keys.BACKSPACE)
        self.assertEqual(self.editing(d)[0], NAMES_FIXED[:-3])
        d.tap(keys.KEY_z, keys.CONTROL)
        self.assertEqual(self.editing(d)[0], NAMES_FIXED)

    def test_the_panels_lines_move_up_and_down(self):
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.run(d.logic.edit_lines(1, [0, 22, 41]))
        d.tap(keys.UP)
        self.assertEqual(self.editing(d)[1], 39)  # the end of the text is column 17 of the last line: 22 + 17
        d.tap(keys.HOME)
        self.assertEqual(self.editing(d)[1], 22)
        d.run(d.logic.edit_lines(2, [0, 5]))  # another session's report: ignored
        self.assertEqual(d.logic.session.editing.lines, (0, 22, 41))

    def test_copy_cut_and_paste_go_through_the_panel(self):
        """The engine has no clipboard of its own on Wayland: the panel puts a copy on it and reads it for a paste."""
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.tap(keys.LEFT, keys.CONTROL | keys.SHIFT)  # select "plan."
        d.tap(keys.KEY_c, keys.CONTROL)
        copied = d.logic.session.panel_view()['editing']['clipboard']
        self.assertEqual(copied['text'], 'plan.')
        d.tap(keys.KEY_x, keys.CONTROL)
        cut = d.logic.session.panel_view()['editing']['clipboard']
        self.assertGreater(cut['id'], copied['id'])
        self.assertEqual(self.editing(d)[0], NAMES_FIXED[:-len('plan.')])
        d.tap(keys.HOME, keys.CONTROL)
        d.tap(keys.KEY_v, keys.CONTROL)
        paste = d.logic.session.panel_view()['editing']['paste']
        self.assertTrue(paste)
        self.assertEqual(d.logic.edit_paste(2, paste, 'Obcy: '), [])  # another session's panel
        self.assertEqual(d.run(d.logic.edit_paste(1, paste, 'Plan: ')), [ShowUI(d.logic.session.view())])
        self.assertEqual(self.editing(d)[:2], ('Plan: ' + NAMES_FIXED[:-len('plan.')], 6))
        self.assertNotIn('paste', d.logic.session.panel_view()['editing'])
        self.assertEqual(d.logic.edit_paste(1, paste, 'jeszcze raz'), [])  # answered already
        d.tap(keys.KEY_v, keys.CONTROL)
        too_long = d.logic.session.panel_view()['editing']['paste']
        self.assertEqual(d.logic.edit_paste(1, too_long, 'x' * 20001), [])
        d.tap(keys.ESCAPE)
        self.assertEqual((d.logic.session.clip, d.logic.session.paste), (None, 0))  # nothing waits after editing

    def test_a_click_in_the_result_edits_it_there(self):
        """The panel maps the click to a place in the result (textmap.resultOffset) and sends edit_at:<offset>."""
        d = self.ready()
        d.run(d.logic.action(1, 'edit_at:6'))  # before "wyślę"
        self.assertEqual(self.editing(d), (NAMES_FIXED, 6, 6))
        d.type('x')
        self.assertTrue(self.editing(d)[0].startswith('Jutro xwyślę'))
        d.run(d.logic.action(1, 'edit_at:0'))  # while editing: the caret moves
        self.assertEqual(self.editing(d)[1:], (0, 0))
        self.assertEqual(d.logic.action(1, 'edit_at:x'), [])
        self.assertEqual(d.logic.action(2, 'edit_at:3'), [])  # another session's panel
        d = self.ready(panel=False)
        self.assertEqual(d.logic.action(1, 'edit_at:3'), [])  # the IBus popup has no place to click

    def test_checking_while_editing(self):
        """macOS EditorChecker: the edited text goes to the live service, without the model; its issues are shown in
        the text they were found in, a suggestion replaces the word (Ctrl+Z takes it back), Ignoruj and Dodaj do
        słownika go to the service as from the card, and when editing ends the service lets the text go."""
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.type(' Dzieki')
        typed = NAMES_FIXED + ' Dzieki'
        check = next(e for e in d.logic.live_update(True, False) if isinstance(e, live.Check))
        self.assertEqual((check.text, check.caret, check.panel), (typed, len(typed), True))  # a word may be unfinished
        self.assertEqual(d.logic.live_update(True, False), [])  # nothing new to send
        self.assertEqual(d.logic.live_update(False, False), [])  # live checking off in the settings
        d.tap(keys.LEFT)
        moved = next(e for e in d.logic.live_update(True, False) if isinstance(e, live.Check))
        self.assertEqual((moved.field, moved.caret), (check.field, None))  # moved: the word under the caret may show
        start = len(NAMES_FIXED) + 1
        issue = {'id': '%d:%d:SPELLING_PL' % (start, start + 6), 'start': start, 'end': start + 6, 'word': 'Dzieki',
                 'message': 'Nieznane słowo', 'suggestions': ['Dzięki', 'Dzieci'], 'kind': 'spelling',
                 'rule': 'SPELLING_PL', 'source': 'słownik'}
        self.assertEqual(d.logic.live_result(check.field + 1, [issue]), [])  # another field's answer
        self.assertEqual(d.run(d.logic.live_result(check.field, [issue], checking=True)), [ShowUI(d.logic.session.view())])
        shown = d.logic.session.panel_view()['editing']
        self.assertEqual((shown['issues'][0]['word'], shown['issues'][0]['learn'], shown['checking']), ('Dzieki', True, True))
        d.run(d.logic.action(1, 'edit_fix:%s:0' % issue['id']))
        self.assertEqual(self.editing(d)[0], NAMES_FIXED + ' Dzięki')
        self.assertNotIn('issues', d.logic.session.panel_view()['editing'])  # found in the text before the fix
        self.assertIn(Log('edit_fix', {'id': 1, 'rule': 'SPELLING_PL'}), d.logs)  # never the word
        d.tap(keys.KEY_z, keys.CONTROL)
        self.assertEqual(len(d.logic.session.panel_view()['editing']['issues']), 1)  # the text it was found in again
        effects = d.logic.action(1, 'edit_learn:%s' % issue['id'])
        self.assertEqual(effects[0], live.Command({'cmd': 'learn', 'word': 'Dzieki', 'rule': 'SPELLING_PL'}))
        self.assertEqual(d.logic.session.panel_view()['editing']['issues'], [])
        self.assertEqual(d.logic.action(1, 'edit_ignore:%s' % issue['id']), [])  # gone already
        self.assertEqual(d.logic.action(1, 'edit_rule_off:%s' % issue['id']), [])
        d.tap(keys.KEY_z, keys.CONTROL)  # "Dzięki" back to "Dzieki": its text, without issues yet
        typed = self.editing(d)[0]
        check = next(e for e in d.logic.live_update(True, False) if isinstance(e, live.Check))
        grammar = [dict(issue, id='%d:%d:ZE' % (n, n + 2), start=n, end=n + 2, word='do', kind='grammar', rule='ZE',
                        source='LanguageTool', rule_off=True) for n in (typed.index('do'), typed.index('i w'))]
        d.run(d.logic.live_result(check.field, grammar))
        shown = d.logic.session.panel_view()['editing']['issues']
        self.assertEqual([(i['learn'], i['rule_off']) for i in shown], [(False, True), (False, True)])
        self.assertEqual(d.logic.action(1, 'edit_learn:%s' % grammar[0]['id']), [])  # no word for the dictionary
        effects = d.logic.action(1, 'edit_rule_off:%s' % grammar[0]['id'])
        self.assertEqual(effects[0], live.Command({'cmd': 'rule_off', 'word': 'do', 'rule': 'ZE'}))
        self.assertIsInstance(effects[-1], ShowUI)  # the panel stays: a message would take its place
        self.assertEqual(d.logic.session.panel_view()['editing']['issues'], [])  # every issue of the rule goes
        self.assertIn(live.Clear(), d.tap(keys.ESCAPE))

    def test_not_in_the_ibus_popup(self):
        d = self.ready(panel=False)
        self.assertFalse(d.logic.session.panel_view()['editable'])
        self.assertEqual(d.type('x'), [HideUI(), ChainUp()])  # as before: the key goes to the app
        self.assertEqual(d.state, IDLE)

class ChangeListTests(unittest.TestCase):
    """The change list under a finished correction, on demand (branding/PANEL.md, List of changes): Ctrl+I or the
    panel's Pokaż / Ukryj zmiany; the marks of the edited text are what it changes against the original."""

    def ready(self, key=None):
        d = Driver(text=NAMES, cursor=3, panel=True)
        d.hotkey(*([key] if key else []))
        segments, changes = diffed(NAMES, NAMES_FIXED)
        info = {'segments': [list(x) for x in segments], 'changes': changes, 'count': len(changes)}
        d.run(d.logic.backend_result(1, NAMES_FIXED, tuple(fallback_ui.lines(segments)), info))
        return d

    def test_hidden_until_ctrl_i_or_the_panel_shows_it(self):
        d = self.ready()
        s = d.logic.session
        view = s.panel_view()
        self.assertEqual((view['changes_open'], len(view['changes'])), (False, 3))  # the rows wait in the view
        self.assertEqual(d.tap(keys.KEY_i, keys.CONTROL)[-1], Consume())
        self.assertTrue(s.panel_view()['changes_open'])
        self.assertIn(Log('changes', {'id': 1, 'open': True}), d.logs)
        d.run(d.logic.action(s.id, 'changes'))  # the panel's Ukryj zmiany
        self.assertEqual((s.panel_view()['changes_open'], d.state), (False, READY))  # the panel stays open

    def test_not_while_editing_and_closed_again_for_the_next_correction(self):
        d = self.ready()
        s = d.logic.session
        d.run(d.logic.action(s.id, 'changes'))
        d.tap(keys.KEY_e, keys.CONTROL)
        self.assertEqual(d.logic.action(s.id, 'changes'), [])
        self.assertEqual(s.panel_view()['changes'], [])  # while editing there is no list to show
        d.tap(keys.ESCAPE)
        d.tap(keys.ESCAPE)
        self.assertEqual(d.state, IDLE)
        d.hotkey()
        self.assertFalse(d.logic.session.panel_view()['changes_open'])

    def test_no_list_no_switch(self):
        d = self.ready(key=j)  # Tłumacz: a translation has no change list
        self.assertEqual(d.logic.action(d.logic.session.id, 'changes'), [])
        self.assertFalse(d.logic.session.changes_open)

    def test_marks_follow_the_typing_and_leave_out_translations_and_long_texts(self):
        d = self.ready()
        d.tap(keys.KEY_e, keys.CONTROL)
        d.type(' Tak.')
        marks = d.logic.session.panel_view()['editing']['marks']
        self.assertEqual(marks[-1], [len(NAMES_FIXED), len(NAMES_FIXED) + 5])  # the typed text is a change too
        d.logic.session.mode_index = MODE_KEYS.index('translate')
        self.assertEqual(d.logic.session.panel_view()['editing']['marks'], [])
        d.logic.session.mode_index = 0
        self.assertEqual(d.logic.session._edit_marks('x' * (session.MARKS_MAX + 1)), [])


class ProblemTests(unittest.TestCase):
    """A failed correction in the GNOME panel (branding/PANEL.md, Errors): its title and buttons from the cause's code
    (engine/problems.py); Enter is the first button, which retries or opens the app where the cause is fixed."""

    def failed(self, why, message='Słowa silnika.', panel=True):
        d = Driver(panel=panel)
        d.hotkey()
        d.run(d.logic.backend_error(1, message, why))
        self.assertEqual(d.state, ERROR)
        return d

    def test_the_view_has_the_title_the_sentence_and_the_buttons(self):
        d = self.failed('refused')
        problem = d.logic.session.panel_view()['problem']
        self.assertEqual((problem['group'], problem['title']), (2, 'Serwer nie odpowiada'))
        self.assertIn('odmowa połączenia', problem['text'])
        self.assertEqual([a['id'] for a in problem['actions']], ['retry', 'open:connection'])
        self.assertEqual(d.logic.session.view().error, problem['text'])  # the IBus popup shows the sentence

    def test_enter_retries_the_same_text_in_the_same_mode(self):
        d = self.failed('model_error')
        effects = d.press(keys.RETURN)
        self.assertEqual(effects[0], StartBackend(2, P2, 'fix'))
        self.assertEqual(d.state, WAITING)
        self.assertIn(Log('problem_action', {'id': 1, 'action': 'retry'}), d.logs)
        self.assertNotIn('problem', d.logic.session.panel_view())

    def test_a_button_opens_the_app_on_the_right_tab_and_closes_the_panel(self):
        for why, name, args in (('no_ollama', 'open:connection', ('--show=connection',)),
                                ('second_language', 'open:writing', ('--show=writing',)),
                                ('offline', 'login', ('--login',))):
            with self.subTest(why=why):
                d = self.failed(why)
                self.assertEqual(d.run(d.logic.action(1, 'problem:' + name)), [OpenApp(args), HideUI()])
                self.assertEqual(d.state, IDLE)
                self.assertIn(Log('end', {'id': 1, 'state': ERROR, 'why': 'opened'}), d.logs)

    def test_enter_does_the_first_button(self):
        d = self.failed('no_model')
        self.assertEqual(d.press(keys.RETURN), [OpenApp(('--show=connection',)), HideUI(), Consume()])

    def test_only_the_buttons_the_error_offers(self):
        d = self.failed('refused')
        self.assertEqual(d.run(d.logic.action(1, 'problem:login')), [])
        self.assertEqual(d.run(d.logic.action(1, 'problem:rm -rf')), [])
        self.assertEqual(d.run(d.logic.action(2, 'problem:retry')), [])  # another session's panel
        self.assertEqual(d.state, ERROR)

    def test_no_button_no_enter_action(self):
        d = self.failed('truncated_reply')
        self.assertEqual(d.press(keys.RETURN), [HideUI(), Consume()])  # Enter closes, as before
        self.assertEqual(d.state, IDLE)

    def test_a_slip_of_the_model_is_retried_once_quietly(self):
        d = Driver(panel=True)
        d.hotkey()
        effects = d.run(d.logic.backend_error(1, 'Model zwrócił pustą odpowiedź. Spróbuj ponownie.', 'empty_reply'))
        self.assertEqual(effects[0], StartBackend(2, P2, 'fix'))
        self.assertEqual((d.state, d.logic.session.panel_view()['retrying']), (WAITING, True))  # "Ponawiam…"
        self.assertIn(Log('retry', {'id': 1, 'why': 'empty_reply'}), d.logs)
        d.run(d.logic.backend_error(2, 'Połączenie z serwerem zostało przerwane.', 'interrupted'))
        self.assertEqual(d.state, ERROR)  # the second slip is said
        self.assertEqual(d.logic.session.panel_view()['problem']['title'], 'Połączenie przerwano przed końcem odpowiedzi')
        self.assertFalse(d.logic.session.panel_view()['retrying'])
        d.press(keys.RETURN)  # Spróbuj ponownie: a run of its own, which may be retried quietly again
        self.assertEqual(d.run(d.logic.backend_error(3, 'x', 'script_drift'))[0], StartBackend(4, P2, 'fix'))

    def test_the_second_try_may_succeed_and_enter_while_working_still_applies(self):
        d = Driver(panel=True)
        d.hotkey()
        d.press(keys.RETURN)  # Zastosuję po zakończeniu
        d.run(d.logic.backend_error(1, 'x', 'interrupted'))
        self.assertTrue(d.logic.session.apply_on_ready)
        effects = d.result(P2.upper())
        self.assertIsInstance(effects[0], Apply)

    def test_not_retried_when_another_try_would_not_help(self):
        for why in ('truncated_reply', 'refused', 'http_404', 'no_ollama', 'model_error', None):
            with self.subTest(why=why):
                d = Driver(panel=True)
                d.hotkey()
                d.run(d.logic.backend_error(1, 'x', why))
                self.assertEqual(d.state, ERROR)

    def test_a_text_that_cannot_be_corrected_is_a_toast(self):
        d = Driver(panel=True)
        d.hotkey()
        self.assertEqual(d.run(d.logic.backend_error(1, 'Tekst ma 7000 znaków. Limit to 6000.', 'too_long')),
                         [Message('too_long', 3)])
        self.assertEqual(d.state, IDLE)
        self.assertIn(Log('end', {'id': 1, 'state': ERROR, 'why': 'too_long'}), d.logs)

    def test_the_ibus_popup_and_older_panels_keep_enter_closing(self):
        d = self.failed('refused', panel=False)
        self.assertEqual(d.press(keys.RETURN), [HideUI(), Consume()])
        self.assertEqual(d.state, IDLE)


class CycleTests(unittest.TestCase):
    def test_paragraph_cycle(self):
        d = Driver()
        self.assertEqual(d.hotkey(), [ResetCompose(), StartBackend(1, P2, 'fix'), ShowUI(View(0, 'working')),
                                      ScheduleIdleTimeout(1, IDLE_TIMEOUT_MS), Consume()])
        self.assertTrue(d.logic.session.is_modal)
        self.assertEqual(d.run(d.logic.backend_partial(1, 'DRU', ('DRU',))), [ShowUI(View(0, 'working', ('DRU',)))])
        corrected = P2.replace('błąd', 'błędy')
        segments = (('same', 'Drugi akapit ma '), ('removed', 'błąd'), ('added', 'błędy'), ('same', P2[20:]))
        lines = tuple(fallback_ui.lines(segments))
        self.assertEqual(d.run(d.logic.backend_result(1, corrected, lines)), [ShowUI(View(0, 'ready', lines))])
        effects = d.press(keys.RETURN)
        ops = effects[0].ops
        self.assertEqual(effects, [Apply(ops), ShowUI(View(0, 'applying', lines)), ScheduleVerifyTimeout(1, 300), Consume()])
        offset, n = ops.delete
        self.assertTrue(offset <= 0 <= offset + n)
        self.assertEqual(TA[:IN_P2 + offset] + ops.commit + TA[IN_P2 + offset + n:], TA.replace('błąd', 'błędy'))
        self.assertEqual(d.release(keys.RETURN), [Consume()])  # web apps act on keyup
        self.assertEqual(d.press(keys.RETURN), [Consume()])  # second Enter while applying: ignored
        self.assertEqual(d.release(keys.RETURN), [Consume()])
        self.assertEqual(d.echo(ops), [Message('applied', 1.5)])
        self.assertEqual(d.state, IDLE)
        self.assertEqual(d.press(keys.RETURN), [ChainUp()])
        self.assertEqual(d.release(keys.RETURN), [ChainUp()])

    def test_selection_is_commit_only(self):
        d = Driver(cursor=70, anchor=67)
        self.assertEqual(d.hotkey()[1], StartBackend(1, 'KOT', 'fix'))
        d.result('PIES 🐕')
        effects = d.press(keys.KP_ENTER)
        self.assertEqual(effects[0], Apply(plan.Ops(None, 'PIES 🐕', 67, 70, 'PIES 🐕')))
        self.assertEqual(d.echo(effects[0].ops), [Message('applied', 1.5)])
        self.assertEqual(d.text, TA.replace('KOT', 'PIES 🐕'))

    def test_enter_while_waiting_applies_on_result(self):
        d = Driver()
        d.hotkey()
        self.assertEqual(d.press(keys.RETURN), [ShowUI(View(0, 'apply_pending')), Consume()])
        self.assertEqual(d.state, WAITING)
        effects = d.result(P2.upper())
        self.assertIsInstance(effects[0], Apply)
        self.assertEqual(d.state, APPLYING)

    def test_enter_while_waiting_then_escape(self):
        d = Driver()
        d.hotkey()
        d.press(keys.RETURN)
        self.assertEqual(d.press(keys.ESCAPE), [CancelBackend(1), HideUI(), Consume()])
        self.assertEqual(d.result(P2.upper(), sid=1), [])

    def test_unchanged_result_just_closes(self):
        d = Driver()
        d.hotkey()
        self.assertEqual(d.result(P2)[0].view.status, 'unchanged')
        self.assertEqual(d.press(keys.RETURN), [HideUI(), Consume()])
        self.assertEqual(d.state, IDLE)

    def test_backend_error(self):
        for key in (keys.RETURN, keys.ESCAPE):
            with self.subTest(key=key):
                d = Driver()
                d.hotkey()
                effects = d.run(d.logic.backend_error(1, 'Nie można połączyć się z serwerem.'))
                self.assertEqual(effects, [ShowUI(View(0, 'error', (), 'Nie można połączyć się z serwerem.'))])
                self.assertEqual(d.state, ERROR)
                self.assertEqual(d.press(key), [HideUI(), Consume()])
                self.assertEqual(d.state, IDLE)

    def test_mode_switch_and_stale_ids(self):
        d = Driver()
        d.hotkey()
        self.assertEqual(d.tap(one, keys.CONTROL), [Consume()])  # same mode: nothing
        effects = d.tap(two, keys.CONTROL)
        self.assertEqual(effects[:2], [CancelBackend(1), StartBackend(2, P2, 'improve')])
        self.assertEqual(effects[2].view.mode, 1)
        self.assertEqual(d.result(P2.upper(), sid=1), [])
        self.assertEqual(d.run(d.logic.backend_partial(1, 'x', ())), [])
        self.assertEqual(d.run(d.logic.backend_error(1, 'x')), [])
        self.assertEqual(d.result(P2.swapcase(), sid=2)[0].view.status, 'ready')
        self.assertEqual(d.tap(one, keys.CONTROL)[:1], [StartBackend(3, P2, 'fix')])  # READY: no cancel needed
        d.tap(two, keys.CONTROL)
        d.tap(keys.ESCAPE)
        self.assertEqual(d.hotkey()[1], StartBackend(5, P2, 'fix'))  # Ctrl+Alt+G is always Popraw, as on macOS

    def test_translation_direction_is_reported_and_reversed(self):
        d = Driver()
        d.hotkey()
        effects = d.tap(six, keys.CONTROL)
        self.assertEqual(effects[:2], [CancelBackend(1), StartBackend(2, P2, 'translate', None)])
        self.assertEqual(d.tap(six, keys.CONTROL), [Consume()])  # direction not known yet: nothing
        view = d.run(d.logic.backend_partial(2, 'Second', rows('Second'), {'target': 'en', 'note': ''}))[0].view
        self.assertEqual(view.target, 'en')
        view = d.run(d.logic.backend_result(2, 'Second.', rows('Second.'), {'target': 'en', 'note': 'Bielik · 2,0 s'}))
        self.assertEqual((view[-1].view.target, view[-1].view.note), ('en', 'Bielik · 2,0 s'))
        effects = d.tap(six, keys.CONTROL)  # READY: reverse the direction
        self.assertEqual(effects[0], StartBackend(3, P2, 'translate', 'pl'))
        self.assertEqual((effects[1].view.target, effects[1].view.note), ('pl', ''))
        d.run(d.logic.backend_error(3, 'x'))
        self.assertEqual(d.tap(six, keys.CONTROL)[0], StartBackend(4, P2, 'translate', 'en'))  # ERROR: again
        self.assertEqual(d.tap(one, keys.CONTROL)[:2], [CancelBackend(4), StartBackend(5, P2, 'fix', None)])
        d.run(d.logic.backend_result(5, P2.upper(), rows(P2.upper()), {'target': 'pl', 'note': 'n'}))
        self.assertEqual(d.logic.session.target, 'en')  # a direction reported outside translate is ignored
        self.assertEqual(d.tap(six, keys.CONTROL)[0], StartBackend(6, P2, 'translate', 'en'))  # kept for this text
        d.tap(keys.ESCAPE)
        self.assertEqual(d.hotkey(j)[1], StartBackend(7, P2, 'translate', None))  # a new capture detects again

    def test_german_english_translation_direction_and_single_language(self):
        driver = Driver()
        driver.hotkey(j)
        effects = driver.run(driver.logic.backend_partial(1, 'Hello', rows('Hello'),
                             {'target': 'en', 'languages': ['de', 'en']}))
        self.assertEqual(effects[0].view.source_language, 'de')
        self.assertIn('DE→EN', fallback_ui.render(effects[0].view)[0])
        self.assertEqual(driver.logic.session.panel_view()['source_language'], 'de')
        self.assertEqual(driver.tap(d_key, keys.CONTROL)[:2], [CancelBackend(1), StartBackend(2, P2, 'translate', 'de')])
        self.assertEqual(driver.logic.session.panel_view()['source_language'], 'en')
        driver.run(driver.logic.backend_partial(2, 'Hallo', rows('Hallo'), {'target': 'en', 'languages': ['en']}))
        self.assertEqual(driver.tap(d_key, keys.CONTROL), [Consume()])

    def test_panel_view_follows_the_session(self):
        d = Driver()
        d.hotkey()
        view = d.logic.session.panel_view()
        self.assertEqual((view['status'], view['kind'], view['scope'], view['source'], view['text']),
                         ('checking', 'session', 'paragraph', P2, ''))  # nothing from the model yet
        d.run(d.logic.backend_partial(1, 'Drugi', rows('Drugi'), {'target': None, 'note': ''}))
        self.assertEqual((d.logic.session.panel_view()['status'], d.logic.session.panel_view()['text']), ('working', 'Drugi'))
        segments = [['same', 'Drugi akapit ma '], ['removed', 'błąd'], ['added', 'błędy']]
        found = [{'from': 'błąd', 'to': 'błędy', 'reason': 'Gramatyka / styl'}]
        d.run(d.logic.backend_result(1, P2.replace('błąd', 'błędy'), rows('x'),
                                     {'note': 'Bielik · 2,9 s', 'segments': segments, 'changes': found, 'count': 1}))
        view = d.logic.session.panel_view()
        self.assertEqual((view['status'], view['segments'], view['changes'], view['count'], view['note'], view['target']),
                         ('ready', segments, found, 1, 'Bielik · 2,9 s', None))
        self.assertEqual(view['text'], P2.replace('błąd', 'błędy'))
        d.tap(six, keys.CONTROL)
        view = d.logic.session.panel_view()
        self.assertEqual((view['mode'], view['segments'], view['changes'], view['text']), (5, [], [], ''))  # restarted

    def test_languagetool_reasons_after_the_result(self):
        # The result does not wait for LanguageTool (as on macOS): its reasons update the ready panel.
        d = Driver()
        d.hotkey()
        found = [{'from': 'błąd', 'to': 'błędy', 'reason': 'Gramatyka / styl'}]
        d.run(d.logic.backend_result(1, P2.replace('błąd', 'błędy'), rows('x'), {'changes': found, 'count': 1}))
        better = [dict(found[0], reason='Niezgodność liczby')]
        self.assertEqual(d.run(d.logic.backend_changes(1, better, 1)), [ShowUI(d.logic.session.view())])
        self.assertEqual(d.logic.session.panel_view()['changes'], better)
        self.assertEqual(d.run(d.logic.backend_changes(0, found, 1)), [])  # an older session's
        d.tap(two, keys.CONTROL)  # another mode: a new run, the late reasons of the old one are stale
        self.assertEqual(d.run(d.logic.backend_changes(1, found, 1)), [])
        self.assertEqual(d.logic.session.panel_view()['changes'], [])
        d = Driver()
        d.hotkey()
        d.tap(keys.RETURN)  # applied as soon as ready: the reasons come too late to matter
        effects = d.run(d.logic.backend_result(1, P2.upper(), rows(P2.upper())))
        self.assertIsInstance(effects[0], Apply)
        self.assertEqual(d.run(d.logic.backend_changes(1, found, 1)), [])

    def test_panel_clicks_are_the_same_as_keys(self):
        d = Driver()
        d.hotkey()
        sid = d.logic.session.id
        self.assertEqual(d.run(d.logic.action(sid + 1, 'cancel')), [])  # another session
        self.assertEqual(d.run(d.logic.action(sid, 'copy')), [])  # nothing to copy while working
        self.assertEqual(d.run(d.logic.action(sid, 'mode:1'))[:2], [CancelBackend(1), StartBackend(2, P2, 'improve')])
        self.assertEqual(d.run(d.logic.action(2, 'mode:9')), [])
        self.assertEqual(d.run(d.logic.action(2, 'direction')), [])  # not translating
        d.result(P2.upper(), sid=2)
        self.assertIsInstance(d.run(d.logic.action(2, 'accept'))[0], Apply)
        d = Driver()
        d.hotkey()
        d.logic.ui_flushed(False)  # the panel is not on screen: clicks cannot be real
        self.assertEqual(d.run(d.logic.action(1, 'cancel')), [])
        d.logic.ui_flushed(True)
        self.assertEqual(d.run(d.logic.action(1, 'cancel')), [CancelBackend(1), HideUI()])

    def test_panel_hidden_by_the_shell_ends_the_session(self):
        d = Driver()
        d.hotkey()
        d.result(P2.upper())
        self.assertEqual(d.run(d.logic.action(99, 'hidden')), [])  # another session
        self.assertEqual(d.state, READY)
        self.assertEqual(d.run(d.logic.action(1, 'hidden')), [])  # nothing left to hide or draw
        self.assertEqual(d.state, IDLE)
        self.assertEqual(d.press(keys.RETURN), [ChainUp()])  # Enter reaches the app again
        d = Driver()
        d.hotkey()
        d.logic.ui_flushed(False)  # hidden before the panel was confirmed: still ends the session
        self.assertEqual(d.run(d.logic.action(1, 'hidden')), [CancelBackend(1)])
        self.assertEqual(d.state, IDLE)

    def test_kopiuj_closes_the_panel(self):
        d = Driver()
        d.hotkey()
        d.result(P2.upper())
        self.assertEqual(d.run(d.logic.action(1, 'copy')), [Message('copied', 1.5)])
        self.assertEqual(d.state, IDLE)

    def test_ctrl_d_reverses_the_translation_only(self):
        d = Driver()
        d.hotkey()
        effects = d.tap(0x64, keys.CONTROL)
        self.assertEqual(effects[-1], ChainUp())  # not translating: an ordinary key cancels and passes
        self.assertEqual(d.state, IDLE)
        d.hotkey()
        d.tap(six, keys.CONTROL)
        d.run(d.logic.backend_partial(3, '', (), {'target': 'en', 'note': ''}))
        self.assertEqual(d.tap(0x64, keys.CONTROL)[:2], [CancelBackend(3), StartBackend(4, P2, 'translate', 'pl')])
        self.assertEqual(d.logic.session.panel_view()['target'], 'pl')

    def test_hotkey_restarts_a_running_session(self):
        d = Driver()
        d.hotkey()
        effects = d.hotkey()
        self.assertEqual(effects[:3], [CancelBackend(1), ResetCompose(), StartBackend(2, P2, 'fix')])

    def test_ctrl_alt_j_starts_in_the_translation_mode(self):
        # ⌥⌘T on macOS: straight into Tłumacz; the next Ctrl+Alt+G is Popraw again.
        d = Driver()
        effects = d.hotkey(j)
        self.assertEqual(effects[:2], [ResetCompose(), StartBackend(1, P2, 'translate')])
        self.assertEqual(effects[2].view.mode, 5)
        self.assertEqual((d.logs[-1].event, d.logs[-1].data['mode']), ('start', 'translate'))
        self.assertTrue(d.logic.session.translating)
        self.assertEqual(d.tap(d_key, keys.CONTROL), [Consume()])  # Ctrl+D: the direction is not known yet
        effects = d.hotkey()  # while translating: restart from a fresh capture in Popraw
        self.assertEqual(effects[:3], [CancelBackend(1), ResetCompose(), StartBackend(2, P2, 'fix')])
        self.assertEqual(d.hotkey(j)[:3], [CancelBackend(2), ResetCompose(), StartBackend(3, P2, 'translate')])

    def test_hotkey_auto_repeat_is_swallowed(self):
        d = Driver()
        d.press(Control_L)
        d.press(Alt_L, keys.CONTROL)
        d.press(*HOTKEY)
        for _ in range(3):
            self.assertEqual(d.press(*HOTKEY), [Consume()])
        self.assertEqual((d.state, d.logic.session.id), (WAITING, 1))
        self.assertEqual(d.release(*HOTKEY), [Consume()])
        self.assertEqual(d.release(Alt_L, keys.CONTROL | keys.MOD1), [ChainUp()])

    def test_session_keys(self):
        d = Driver()
        d.hotkey()
        self.assertEqual(d.tap(keys.PAGE_DOWN), [Consume()])  # one page only: nothing to scroll
        for keyval, state in ((Shift_L, 0), (keys.ISO_LEVEL3_SHIFT, 0), (Control_L, 0), (Alt_L, 0)):
            with self.subTest(keyval=hex(keyval)):
                self.assertEqual(d.press(keyval, state), [ChainUp()])
                self.assertEqual(d.release(keyval, state), [ChainUp()])
                self.assertEqual(d.state, WAITING)
        self.assertEqual(d.press(aogonek, keys.MOD5), [CancelBackend(1), HideUI(), ChainUp()])  # AltGr+a
        self.assertEqual(d.release(aogonek, keys.MOD5), [ChainUp()])
        self.assertEqual(d.state, IDLE)
        self.assertEqual(d.hotkey()[1], Message('stale', 3))  # the letter changed the text

    def test_scrolling(self):
        long_paragraph = ' '.join('słowo%03d' % i for i in range(120))
        d = Driver(text=long_paragraph, cursor=5)
        d.hotkey()
        self.assertEqual(d.tap(keys.PAGE_DOWN), [Consume()])  # nothing to show yet
        d.run(d.logic.backend_partial(1, long_paragraph, tuple(fallback_ui.lines((('same', long_paragraph),)))))
        self.assertEqual(d.tap(keys.PAGE_UP), [Consume()])
        self.assertEqual(d.tap(keys.PAGE_DOWN)[0].view.page, 1)
        self.assertEqual(d.run(d.logic.page(1))[0].view.page, 2)
        self.assertEqual(d.run(d.logic.page(1)), [])  # last page
        self.assertEqual(d.press(keys.PAGE_UP)[0].view.page, 1)
        self.assertEqual(d.press(keys.PAGE_UP), [Consume()])  # auto-repeat is swallowed, never scrolls
        self.assertEqual(d.release(keys.PAGE_UP), [Consume()])

    def test_paging_never_wraps_text_on_the_key_path(self):
        d = Driver()
        d.hotkey()
        d.run(d.logic.backend_result(1, P2.upper(), tuple('linia %d' % i for i in range(20))))
        with mock.patch.object(fallback_ui, 'lines', side_effect=AssertionError('wrapped on the key path')):
            self.assertEqual(d.tap(keys.PAGE_DOWN)[0].view.page, 1)
            self.assertEqual(d.tap(keys.PAGE_DOWN)[0].view.page, 2)
            self.assertEqual(fallback_ui.render(d.logic.session.view())[1], ['linia 16', 'linia 17', 'linia 18', 'linia 19'])

    def test_keys_before_the_popup_is_drawn_reach_the_app(self):
        # The UI is drawn from an idle callback; a key dispatched before it must not be swallowed.
        for key in (keys.RETURN, keys.ESCAPE, keys.PAGE_DOWN, a):
            with self.subTest(key=hex(key)):
                d = Driver()
                d.run(d.logic.key(*HOTKEY_PRESS), draw=False)
                self.assertEqual((d.state, d.logic.session.is_modal), (WAITING, False))
                self.assertEqual(d.run(d.logic.key(key, CODES[key], 0), draw=False), [CancelBackend(1), HideUI(), ChainUp()])
                self.assertEqual(d.state, IDLE)
                self.assertEqual(d.release(key), [ChainUp()])
        d = Driver()
        d.run(d.logic.key(*HOTKEY_PRESS), draw=False)
        self.assertEqual(d.run(d.logic.key(Shift_L, CODES[Shift_L], 0), draw=False), [ChainUp()])  # modifiers wait
        self.assertEqual(d.state, WAITING)
        d.logic.ui_flushed(True)
        self.assertEqual(d.press(keys.RETURN), [ShowUI(View(0, 'apply_pending')), Consume()])


class InterruptionTests(unittest.TestCase):
    def reach(self, state):
        d = Driver()
        d.hotkey()
        if state == READY:
            d.result(P2.upper())
        elif state == ERROR:
            d.run(d.logic.backend_error(1, 'błąd'))
        elif state == APPLYING:
            d.result(P2.upper())
            d.press(keys.RETURN)
        self.assertEqual(d.state, state)
        return d

    def test_table(self):
        events = {
            'reset': lambda d: d.logic.reset(),
            'focus in': lambda d: d.logic.focus_in('gnome-shell'),
            'focus out': lambda d: d.logic.focus_out(),
            'sensitive': lambda d: d.logic.content_type(8, 0),
            'idle timeout': lambda d: d.logic.idle_timeout(1),
        }
        for state in (WAITING, READY, ERROR, APPLYING):
            for name, event in events.items():
                with self.subTest(state=state, event=name):
                    d = self.reach(state)
                    effects = d.run(event(d))
                    if state == APPLYING and name.startswith('focus'):
                        # The echo belongs to the old field and the shell closes the popup on focus-out.
                        self.assertEqual((effects, d.state), ([HideUI()], IDLE))
                        self.assertEqual(d.logs[-2].data['outcome'], 'unverified')
                        self.assertEqual(d.press(keys.RETURN), [ChainUp()])
                    elif state == APPLYING:
                        self.assertEqual((effects, d.state), ([], APPLYING))
                    else:
                        want = [CancelBackend(1), HideUI()] if state == WAITING else [HideUI()]
                        self.assertEqual((effects, d.state), (want, IDLE))
                        self.assertEqual(d.press(keys.RETURN), [ChainUp()])

    def test_stale_idle_timeout_ignored(self):
        d = self.reach(WAITING)
        d.press(two, keys.CONTROL)
        self.assertEqual(d.run(d.logic.idle_timeout(1)), [])
        self.assertEqual(d.state, WAITING)

    def test_apply_time_revalidation(self):
        moved = TA.replace('Trzeci', 'Czwarty')
        cases = [
            ('text changed elsewhere', (moved, IN_P2, IN_P2), 'changed'),
            ('caret moved', (TA, IN_P2 + 1, IN_P2 + 1), 'changed'),
            ('selection appeared', (TA, IN_P2, IN_P2 - 2), 'changed'),
            ('text dropped', (None, 0, 0), 'stale'),
        ]
        for name, surrounding, reason in cases:
            with self.subTest(name):
                d = self.reach(READY)
                d.run(d.logic.surrounding(*surrounding))
                self.assertEqual(d.press(keys.RETURN), [Message(reason, 4), Consume()])
                self.assertEqual(d.state, IDLE)

    def test_hotkey_while_applying_waits_for_the_echo(self):
        d = self.reach(APPLYING)
        self.assertEqual(d.hotkey()[1], Message('stale', 3))


class VerifyTests(unittest.TestCase):
    def applying(self):
        d = Driver()
        d.hotkey()
        d.result(P2.replace('błąd', 'błędy'))
        return d, d.press(keys.RETURN)[0].ops

    def test_outcomes(self):
        d, ops = self.applying()
        offset, n = ops.delete
        new = (TA.replace('błąd', 'błędy'), ops.start + len(ops.replacement))
        after_delete = (TA[:IN_P2 + offset] + TA[IN_P2 + offset + n:], IN_P2 + offset)  # GTK echoes this first
        cases = [
            ('echo ok', [new], None, Message('applied', 1.5), 'ok'),
            ('delete echoed before commit (GTK3/GTK4)', [after_delete, new], None, Message('applied', 1.5), 'ok'),
            ('wrong echo', [after_delete], 1, Message('verify_mismatch', 4), 'mismatch'),
            ('no echo', [], 1, HideUI(), 'unverified'),
        ]
        for name, echoes, timeout, final, outcome in cases:
            with self.subTest(name):
                d, _ = self.applying()
                effects = []
                for text, cursor in echoes:
                    effects = d.run(d.logic.surrounding(text, cursor, cursor))
                if timeout:
                    self.assertEqual(d.run(d.logic.verify_timeout(2)), [])  # other id
                    effects = d.run(d.logic.verify_timeout(timeout))
                self.assertEqual(effects, [final])
                self.assertEqual(d.state, IDLE)
                self.assertEqual(d.logs[-1].data['outcome'], outcome)
                self.assertEqual(d.run(d.logic.verify_timeout(1)), [])

    def test_typing_during_applying_abandons_verification(self):
        d, _ = self.applying()
        self.assertEqual(d.press(a), [HideUI(), ChainUp()])
        self.assertEqual(d.state, IDLE)


CHROME = ('google-chrome.desktop', 'wayland')
EDITOR = ('org.gnome.TextEditor.desktop', 'wayland')


class AppTests(unittest.TestCase):
    """docs/apps.md: Ctrl+Alt+G in an app where GramaDrama is off belongs to the app, and every step
    of a correction names the app it was made in."""

    def test_app_off_chains_the_hotkey_up(self):
        d = Driver(focus=CHROME, disabled={'google-chrome.desktop'})
        d.press(Control_L)
        d.press(Alt_L, keys.CONTROL)
        n = len(d.logs)
        effects = d.logic.key(*HOTKEY_PRESS, d.focus, d.disabled)
        d.run(effects)
        # No ResetCompose, no Message, not consumed: the app gets exactly what it would get without us.
        self.assertEqual(effects, [Log('refused', {'at': 'hotkey', 'reason': 'app_off', 'app': 'google-chrome.desktop',
                                                   'win': 'wayland'}), ChainUp()])
        self.assertEqual(d.press(*HOTKEY), [ChainUp()])  # an auto-repeat reaches the app too
        self.assertEqual(d.release(*HOTKEY), [ChainUp()])  # and the release: the press was never consumed
        self.assertEqual(d.release(Alt_L, keys.CONTROL | keys.MOD1), [ChainUp()])
        self.assertEqual((d.state, d.logic.session.id), (IDLE, 0))  # nothing captured, nothing shown
        self.assertEqual([(log.event, log.data['reason']) for log in d.logs[n:]], [('refused', 'app_off')] * 2)
        effects = d.logic.key(j, CODES[j], keys.CONTROL | keys.MOD1, d.focus, d.disabled)  # Ctrl+Alt+J too
        self.assertEqual(effects[-1], ChainUp())
        self.assertEqual((effects[0].event, effects[0].data['reason']), ('refused', 'app_off'))
        self.assertEqual((d.state, d.logic.session.id), (IDLE, 0))
        # The field is still trusted: once the focus is in an app that is on, the hotkey captures.
        d.focus = EDITOR
        self.assertEqual(d.hotkey()[1], StartBackend(1, P2, 'fix'))

    def test_hotkey_starts_in_the_apps_mode(self):
        # macOS Settings.appModes: Ctrl+Alt+G starts in the mode set for the focused app; Popraw otherwise.
        modes = {'google-chrome.desktop': 'formal', 'org.gnome.TextEditor.desktop': 'translate',
                 'slack.desktop': 'no-such-mode', applist.GRAMADRAMA_APP: 'shorter'}
        cases = [
            ('an app with a mode', CHROME, 'formal'),
            ('an app without one', ('chatgpt.desktop', 'wayland'), 'fix'),
            ('Tłumacz has its own shortcut, so it is no default', EDITOR, 'fix'),
            ('a mode this build does not know', ('slack.desktop', 'wayland'), 'fix'),
            ('the GramaDrama app', (applist.GRAMADRAMA_APP, 'wayland'), 'fix'),
            ('unknown focus', None, 'fix'),
            ('no focus window', ('', 'none'), 'fix'),
        ]
        for name, focus, want in cases:
            with self.subTest(name):
                d = Driver(focus=focus, modes=modes)
                self.assertEqual(d.hotkey()[1], StartBackend(1, P2, want))
        d = Driver(focus=CHROME, modes=modes)
        self.assertEqual(d.hotkey(j)[1], StartBackend(1, P2, 'translate', None))  # Ctrl+Alt+J: Tłumacz anywhere
        d.tap(keys.ESCAPE)
        d.logic.live.clear()
        self.assertEqual([e for e in d.run(d.logic.action(0, 'live:open')) if isinstance(e, StartBackend)],
                         [StartBackend(2, P2, 'fix')])  # the badge's click is Popraw in every app
        d.tap(keys.ESCAPE)
        d.modes = {}  # apps.json changed meanwhile: the next Ctrl+Alt+G follows it
        self.assertEqual(d.hotkey()[1], StartBackend(3, P2, 'fix'))

    def test_app_off_leaves_a_running_session_alone(self):
        d = Driver(focus=CHROME)
        d.hotkey()
        d.result(P2.upper())
        d.disabled = frozenset({'google-chrome.desktop'})  # apps.json changed meanwhile
        self.assertEqual(d.press(*HOTKEY), [ChainUp()])
        self.assertEqual(d.release(*HOTKEY), [ChainUp()])
        self.assertEqual((d.state, d.logic.session.is_modal, d.logic.session.id), (READY, True, 1))
        self.assertIsInstance(d.tap(keys.RETURN)[0], Apply)  # the session goes on as before

    def test_unknown_focus_and_no_focus_window_are_allowed(self):
        for focus in (None, ('', 'none'), ('org.gnome.Shell', 'shell')):
            with self.subTest(focus=focus):
                d = Driver(focus=focus, disabled={'google-chrome.desktop', ''})
                self.assertEqual(d.hotkey()[1], StartBackend(1, P2, 'fix'))

    def test_the_gramadrama_app_is_always_allowed(self):
        d = Driver(focus=(applist.GRAMADRAMA_APP, 'wayland'), disabled={applist.GRAMADRAMA_APP, 'chatgpt.desktop'})
        self.assertEqual(d.hotkey()[1], StartBackend(1, P2, 'fix'))
        self.assertEqual(d.logs[-1].data['app'], applist.GRAMADRAMA_APP)

    def test_every_step_names_the_app_of_its_hotkey(self):
        d = Driver(focus=CHROME)
        d.hotkey()
        self.assertEqual(d.logs[-1], Log('start', {'id': 1, 'scope': 'paragraph', 'chars': len(P2), 'mode': 'fix',
                                                   'app': 'google-chrome.desktop', 'win': 'wayland'}))
        d.focus = EDITOR  # only a hotkey reads the focus: the session stays Chrome's
        d.tap(two, keys.CONTROL)  # a restart in another mode
        d.result(P2.upper(), sid=2)
        d.echo(d.press(keys.RETURN)[0].ops)
        steps = [(log.event, log.data.get('id'), log.data.get('app'), log.data.get('win')) for log in d.logs]
        chrome = ('google-chrome.desktop', 'wayland')
        self.assertEqual([s for s in steps if s[0] in ('start', 'apply', 'verified')],
                         [('start', 1) + chrome, ('start', 2) + chrome, ('apply', 2) + chrome, ('verified', 2) + chrome])
        self.assertEqual(d.hotkey()[1], StartBackend(3, P2.upper(), 'fix'))  # a new hotkey: the new app
        self.assertEqual((d.logs[-1].data['app'], d.logs[-1].data['win']), EDITOR)

    def test_verification_after_the_focus_moved_on_names_the_app(self):
        finishes = {'focus moved': lambda d: d.logic.focus_in('gnome-shell'),
                    'no echo': lambda d: d.logic.verify_timeout(1)}
        for name, finish in finishes.items():
            with self.subTest(name):
                d = Driver(focus=('chatgpt.desktop', 'wayland'))
                d.hotkey()
                d.result(P2.upper())
                d.press(keys.RETURN)
                d.focus = CHROME
                d.run(finish(d))
                verified = [log for log in d.logs if log.event == 'verified']
                self.assertEqual(verified, [Log('verified', {'id': 1, 'outcome': 'unverified', 'app': 'chatgpt.desktop',
                                                             'win': 'wayland'})])
        d = Driver(focus=('chatgpt.desktop', 'x11'))
        d.hotkey()
        d.result(P2.upper())
        d.press(keys.RETURN)
        d.run(d.logic.surrounding(TA, 0, 0))  # a wrong echo, then the timeout
        d.run(d.logic.verify_timeout(1))
        self.assertEqual(d.logs[-1], Log('verified', {'id': 1, 'outcome': 'mismatch', 'app': 'chatgpt.desktop',
                                                      'win': 'x11'}))

    def test_refusals_at_the_hotkey_name_the_app(self):
        d = Driver(text=None, focus=('chatgpt.desktop', 'x11'))
        d.run(d.logic.capabilities(9))  # XWayland without surrounding text
        d.press(keys.LEFT)
        self.assertEqual(d.hotkey()[1], Message('no_text', 3))
        self.assertEqual(d.logs[-1], Log('refused', {'at': 'hotkey', 'reason': 'no_text', 'app': 'chatgpt.desktop',
                                                     'win': 'x11'}))
        d = Driver(text=None)  # no extension: the focus is unknown
        d.hotkey()
        self.assertEqual(d.logs[-1].data, {'at': 'hotkey', 'reason': 'stale', 'app': None, 'win': None})

    def test_every_key_reaches_an_app_that_is_off(self):
        d = Driver(focus=CHROME, disabled={'google-chrome.desktop'})
        sequence = [HOTKEY, (keys.RETURN, 0), (Shift_L, 0), (keys.ESCAPE, 0), HOTKEY, (a, 0), (two, keys.CONTROL), HOTKEY]
        for i, (keyval, state) in enumerate(sequence):
            for s in (state, state | keys.RELEASE):
                with self.subTest(step=i, release=bool(s & keys.RELEASE)):
                    effects = d.logic.key(keyval, CODES[keyval], s, d.focus, d.disabled)
                    self.assertEqual([e for e in effects if isinstance(e, (Consume, ChainUp))], [ChainUp()])
                    self.assertIsInstance(effects[-1], ChainUp)
        self.assertEqual((d.state, d.logic.session.id), (IDLE, 0))


class RobustnessTests(unittest.TestCase):
    def test_every_key_has_exactly_one_disposition(self):
        d = Driver()
        sequence = [HOTKEY, (keys.RETURN, 0), (keys.RETURN, 0), (Shift_L, 0), (keys.PAGE_DOWN, 0), (two, keys.CONTROL),
                    (keys.ESCAPE, 0), HOTKEY, (a, 0), (keys.RETURN, 0), (keys.LEFT, keys.SHIFT), HOTKEY, HOTKEY]
        for i, (keyval, state) in enumerate(sequence):
            if i == 3:
                d.result(P2.upper())
            for s in (state, state | keys.RELEASE):
                with self.subTest(step=i, release=bool(s & keys.RELEASE)):
                    effects = d.logic.key(keyval, CODES[keyval], s)
                    dispositions = [e for e in effects if isinstance(e, (Consume, ChainUp))]
                    self.assertEqual(len(dispositions), 1)
                    self.assertIs(effects[-1], dispositions[0])

    def test_fail_and_disable(self):
        d = Driver()
        d.hotkey()
        self.assertEqual(d.run(d.logic.fail()), [CancelBackend(1), HideUI()])
        self.assertEqual(d.hotkey()[1], Message('stale', 3))
        self.assertEqual(d.run(d.logic.fail()), [HideUI()])
        d = Driver()
        d.hotkey()
        self.assertEqual(d.run(d.logic.disable()), [CancelBackend(1), HideUI(), live.Badge({'v': 1, 'count': 0})])
        self.assertEqual(d.hotkey()[1], Message('no_field', 3))

    def test_bad_results_are_never_shown_or_applied(self):
        for result in ('DRUGI \udcff AKAPIT', '\ud83d'):
            with self.subTest(result=result):
                d = Driver()
                d.hotkey()
                self.assertEqual(d.run(d.logic.backend_partial(1, result, (result,))), [])  # cannot be drawn
                effects = d.result(result)
                self.assertEqual(effects, [ShowUI(View(0, 'error', (), fallback_ui.message('bad_result')))])
                self.assertEqual(d.press(keys.RETURN), [HideUI(), Consume()])
                self.assertEqual(d.state, IDLE)
        d = Driver()
        d.hotkey()
        d.run(d.logic.backend_error(1, 'zły \udcff komunikat'))
        self.assertEqual(d.logic.session.view().error, fallback_ui.message('internal'))

    def test_exception_path_delivers_press_and_release_together(self):
        d = Driver()
        d.hotkey()
        d.logic.key(*HOTKEY_PRESS)  # consumed press whose handler then fails in the adapter
        d.logic.forget_key(CODES[g])
        self.assertEqual(d.release(*HOTKEY), [ChainUp()])

    def test_logs_and_status_never_contain_text(self):
        d = Driver()
        d.hotkey()
        d.run(d.logic.backend_partial(1, P2[:10], (P2[:10],)))
        d.result(P2.upper())
        d.echo(d.press(keys.RETURN)[0].ops)
        dump = repr([(log.event, log.data) for log in d.logs]) + repr(d.logic.status())
        self.assertEqual([log.event for log in d.logs], ['start', 'result', 'apply', 'verified'])
        for word in ('Drugi', 'DRUGI', 'akapit', 'KOT', 'Pierwszy'):
            self.assertNotIn(word, dump)


if __name__ == '__main__':
    unittest.main()
