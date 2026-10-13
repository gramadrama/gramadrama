"""Live checking, the engine's side (engine/live.py) and its single fixes through Session and EngineLogic."""
import itertools
import unittest
from unittest import mock
from gramadrama.engine import keys, live, plan
from gramadrama.engine.context import ConfirmCaret, FieldContext
from gramadrama.engine.live import Badge, Check, Clear
from gramadrama.engine.session import (APPLYING, CONFIRM_MS, IDLE, Apply, EngineLogic, Log, Message,
                                      ScheduleConfirmTimeout, ScheduleVerifyTimeout)

TEXT = 'Mysle ze to dziala.'


def field(text=TEXT, cursor=None, client='gnome-shell'):
    ctx = FieldContext()
    ctx.focus_in(client)
    ctx.set_caps(41)
    ctx.key_passed(0xff53, 0)  # a caret key: the next update is current
    cursor = len(text) if cursor is None else cursor
    ctx.surrounding(text, cursor, cursor)
    return ctx


def LiveField(ctx):
    """Field numbers are process-wide (live.FIELDS): each field of a test numbers its texts from 1."""
    return live.LiveField(ctx, fields=itertools.count(1))


def issue(start, word, suggestions, rule='SPELLING_PL'):
    return {'id': '%d:%d:%s' % (start, start + len(word), rule), 'start': start, 'end': start + len(word), 'word': word,
            'message': 'Nieznane słowo', 'suggestions': suggestions, 'source': 'słownik', 'rule': rule, 'kind': 'spelling'}


class LiveFieldTests(unittest.TestCase):
    def test_checking_without_issues_and_completion(self):
        f = LiveField(field())
        f.update(True, False, False)
        badge, = f.result(1, [], checking=True)
        self.assertEqual((badge.view['count'], badge.view['checking']), (0, True))
        self.assertTrue(f.shown)
        badge, = f.result(1, [], checking=False)
        self.assertEqual((badge.view['count'], badge.view['checking']), (0, False))
        self.assertFalse(f.shown)

    def test_checking_stops_on_edit_or_clear_and_ignores_older_fields(self):
        f = LiveField(field())
        f.update(True, False, False)
        f.result(1, [], checking=True)
        f.ctx.key_passed(ord('a'), 0)
        badge, = f.update(True, False, False)
        self.assertFalse(badge.view['checking'])
        self.assertFalse(f.checking)
        self.assertEqual(f.result(1, [], checking=True), [])  # old check while its input is stale
        f.ctx.surrounding(TEXT + 'a', len(TEXT) + 1, len(TEXT) + 1)
        f.update(True, False, False)
        f.result(1, [], checking=True)
        self.assertIn(Badge({'v': 1, 'count': 0}), f.clear())
        self.assertFalse(f.checking)
        self.assertEqual(f.result(1, [], checking=True), [])

    def test_what_is_sent(self):
        f = LiveField(field())
        self.assertEqual(f.update(True, False, False), [Check(1, TEXT, len(TEXT))])
        self.assertEqual(f.update(True, False, False), [])  # nothing new
        f.ctx.surrounding(TEXT, 3, 3)
        self.assertEqual(f.update(True, False, False), [Check(1, TEXT, 3)])  # the caret moved: the same field
        f.ctx.surrounding(TEXT, 3, 6)
        self.assertEqual(f.update(True, False, False), [Check(1, TEXT, None)])  # a selection: no caret

    def test_when_nothing_is_checked(self):
        for setup, why in ((lambda ctx: ctx.set_content_type(8, 0), 'a password field'),
                           (lambda ctx: ctx.set_content_type(10, 0), 'a terminal')):
            with self.subTest(why):
                f = LiveField(field())
                f.update(True, False, False)
                f.result(1, [])  # nothing found: the badge is hidden
                setup(f.ctx)
                self.assertEqual(f.update(True, False, False), [Clear()])
        f = LiveField(field())
        self.assertEqual(f.update(False, False, False), [Badge({'v': 1, 'count': 0})])  # another engine's may be up
        self.assertEqual(f.update(False, False, False), [])  # off: nothing was sent, nothing shown
        f.update(True, False, False)
        f.result(1, [issue(0, 'Mysle', ['Myślę'])])
        self.assertEqual(f.update(True, True, False), [Clear(), Badge({'v': 1, 'count': 0})])  # the app is off
        f.update(True, False, False)
        self.assertEqual(f.update(True, False, True), [Clear()])  # a correction session or a dictation runs

    def test_a_stale_text_waits(self):
        ctx = field()
        f = LiveField(ctx)
        f.update(True, False, False)
        ctx.reset()  # a click: the text is not current until the next update
        self.assertEqual(f.update(True, False, False), [])

    def test_a_click_ibus_never_reported_asks_atspi(self):
        """GTK4 after a click that left the caret's state as ibus-daemon last sent it, then another click: no
        underlines until a key, unless AT-SPI says where the caret is (2026-10-02, GNOME Text Editor)."""
        ctx = field(cursor=3)
        ctx.can_confirm = True
        f = LiveField(ctx)
        f.update(True, False, False)
        ctx.reset()  # the first click: no update at all
        ctx.reset()  # the second
        ctx.surrounding(TEXT, 9, 9)  # its update, distrusted
        ask, = f.update(True, False, False)
        self.assertEqual(ask, ConfirmCaret(ask.id, TEXT))
        self.assertEqual(f.update(True, False, False), [])  # once per state of the field
        self.assertFalse(f.caret_confirmed(ask.id + 1000, 9, 9))  # another question's answer
        self.assertTrue(f.caret_confirmed(ask.id, 9, 9))
        self.assertEqual(f.update(True, False, False), [Check(1, TEXT, 9)])
        self.assertFalse(f.caret_confirmed(ask.id, 9, 9))  # answered once

    def test_atspi_sees_another_caret(self):
        """The app has not sent its state: the underlines wait for it, as a fix through mutter would land
        beside the word."""
        ctx = field(cursor=3)
        ctx.can_confirm = True
        f = LiveField(ctx)
        f.update(True, False, False)
        ctx.reset()
        ask, = f.update(True, False, False)
        self.assertFalse(f.caret_confirmed(ask.id, 7, 7))
        self.assertFalse(ctx.fresh)  # a fix stays refused as stale too
        self.assertEqual(f.update(True, False, False), [])

    def test_an_answer_about_an_older_state_is_void(self):
        ctx = field()
        ctx.can_confirm = True
        f = LiveField(ctx)
        f.update(True, False, False)
        ctx.reset()
        ask, = f.update(True, False, False)
        ctx.reset()  # clicked again before the answer: AT-SPI may have read the caret before this click
        self.assertFalse(f.caret_confirmed(ask.id, 2, 2))
        self.assertFalse(ctx.fresh)
        again, = f.update(True, False, False)  # a new state: a new question
        self.assertNotEqual(again.id, ask.id)
        self.assertFalse(f.caret_confirmed(again.id, None, None))  # AT-SPI cannot tell: still waiting for a key
        self.assertEqual(f.update(True, False, False), [])

    def test_never_asked(self):
        cases = (('the adapter cannot ask', lambda ctx: setattr(ctx, 'can_confirm', False), (True, False, False)),
                 ('the live check is off', None, (False, False, False)),
                 ('the app is off', None, (True, True, False)),
                 ('a session runs', None, (True, False, True)),
                 ('a key waits for its update', lambda ctx: ctx.key_passed(0x61, 0), (True, False, False)),
                 ('another client', lambda ctx: setattr(ctx, 'client', 'gtk3-im:gedit'), (True, False, False)))
        for name, setup, args in cases:
            with self.subTest(name):
                ctx = field()
                ctx.can_confirm = True
                f = LiveField(ctx)
                f.update(True, False, False)
                ctx.reset()
                if setup:
                    setup(ctx)
                self.assertFalse([e for e in f.update(*args) if isinstance(e, ConfirmCaret)])

    def test_answers_go_to_whoever_asked(self):
        """The live check and a hotkey may both ask about the same click; each answer goes to its question."""
        for first in ('live', 'hotkey'):
            with self.subTest(first):
                logic = EngineLogic()
                logic.ctx.can_confirm = True
                logic.focus_in('gnome-shell')
                logic.capabilities(41)
                logic.ctx.key_passed(0xff53, 0)
                logic.surrounding(TEXT, 3, 3)
                logic.reset()
                logic.reset()
                logic.surrounding(TEXT, 9, 9)
                ask, = logic.live_update(True, False)
                hotkey = next(e for e in logic.session.hotkey('fix') if isinstance(e, ConfirmCaret))
                self.assertNotEqual(ask.id, hotkey.id)
                if first == 'live':
                    self.assertEqual(logic.caret_confirmed(ask.id, 9, 9), [])
                    self.assertTrue(logic.ctx.fresh)
                started = logic.caret_confirmed(hotkey.id, 9, 9)
                self.assertEqual(logic.session.state, 'waiting', started)
                if first == 'hotkey':
                    self.assertEqual(logic.caret_confirmed(ask.id, 9, 9), [])  # late: the field is current already

    def test_a_cut_window_sends_the_caret_paragraph(self):
        text = ('x' * 2000 + '\n') * 2 + 'Mysle ze to dziala. Tak'
        ctx = field(text, len(text) - 7)
        f = LiveField(ctx)
        check, = f.update(True, False, False)
        # Its last word goes: the window may end inside it ("Tak" may be "Takie" in the field).
        self.assertEqual((check.text, check.caret, f.base), ('Mysle ze to dziala.', 16, 4002))
        ctx.surrounding(text, len(text) - 1, len(text) - 1)
        check, = f.update(True, False, False)
        self.assertEqual((check.text, check.caret), ('Mysle ze to dziala.', None))  # the caret's word went with it

    def test_the_words_at_a_cut_windows_edges(self):
        """The reviewer's case: a long field typed at its end, the window starts inside "Przygotowaliśmy"."""
        text = 'rzygotowaliśmy ' + 'sprawozdanie z wczorajszego spotkania, ' * 110 + 'kon'
        ctx = field(text)
        f = LiveField(ctx)
        check, = f.update(True, False, False)
        self.assertEqual((f.base, check.text[:12], check.text[-10:], check.caret), (15, 'sprawozdanie', 'spotkania,', None))
        ctx.surrounding('x' * 3990, 3990, 3990)  # a window of one cut word: nothing to check
        self.assertEqual(f.update(True, False, False), [Clear(), Badge({'v': 1, 'count': 0})])

    def test_a_fix_at_a_cut_windows_edge_is_refused(self):
        text = 'Ala Mysle ' + 'y' * 3990
        ctx = field(text)
        f = LiveField(ctx)
        check, = f.update(True, False, False)
        self.assertEqual((check.text, f.base), ('Mysle', 4))
        f.result(check.field, [issue(0, 'Mysle', ['Myślę'])])
        ctx.surrounding(text[4:] + 'yyyy', len(text), len(text))  # typed on: the window moved past "Ala "
        self.assertEqual(f.fix('0:5:SPELLING_PL', 0)[:2], (None, 'truncated'))  # it may be "…Mysle" in the field
        ctx.surrounding(text, len(text), len(text))
        capture, corrected, _ = f.fix('0:5:SPELLING_PL', 0)
        self.assertEqual((capture.lo, corrected[:6]), (4, 'Myślę '))

    def test_the_badge(self):
        f = LiveField(field())
        f.update(True, False, False)
        self.assertEqual(f.result(0, [issue(0, 'Mysle', ['Myślę'])]), [])  # an older field's answer
        badge, = f.result(1, [issue(0, 'Mysle', ['Myślę'])] + [issue(i, 'x', []) for i in range(6, 12)])
        view = badge.view
        self.assertEqual((view['count'], view['summary'], len(view['issues']), view['more']), (7, '7 błędów', 5, 2))
        self.assertEqual(view['issues'][0], {'id': '0:5:SPELLING_PL', 'word': 'Mysle', 'suggestions': ['Myślę'],
                                             'message': 'Nieznane słowo', 'kind': 'spelling', 'source': 'słownik'})
        self.assertEqual(f.result(1, []), [Badge({'v': 1, 'count': 0, 'summary': '', 'issues': [], 'more': 0,
                                              'checking': False})])
        self.assertEqual(f.result(1, []), [])  # hidden already

    def test_a_note_keeps_the_badge_with_no_issues(self):
        # Model only (extension version 18): the service's note shows the badge's „!” with no issues; it stays when an
        # edit stops the checking, goes with the next result without it, and with another field.
        note = 'Test-model nie odpowiada — sprawdzanie wstrzymane na minutę.'
        f = LiveField(field())
        f.update(True, False, False)
        badge, = f.result(1, [], checking=False, note=note)
        self.assertEqual((badge.view['count'], badge.view['note']), (0, note))
        self.assertTrue(f.shown)
        f.result(1, [], checking=True, note=note)
        f.ctx.key_passed(ord('a'), 0)
        badge, = f.update(True, False, False)
        self.assertEqual((badge.view['checking'], badge.view['note']), (False, note))
        f.ctx.surrounding(TEXT + 'a', len(TEXT) + 1, len(TEXT) + 1)
        f.update(True, False, False)
        badge, = f.result(1, [], checking=False)
        self.assertNotIn('note', badge.view)
        self.assertFalse(f.shown)
        f.result(1, [], note='x' * 400)
        self.assertEqual(len(f.note), live.MAX_NOTE)
        self.assertEqual(f.result(1, [], note='  '), [Badge({'v': 1, 'count': 0, 'summary': '', 'issues': [], 'more': 0,
                                                            'checking': False})])
        f.result(1, [], note=note)
        f.ctx.focus_in('gnome-shell')  # another field
        f.ctx.set_caps(41)
        f.ctx.key_passed(0xff53, 0)
        f.ctx.surrounding('Inny.', 5, 5)
        f.update(True, False, False)
        self.assertIsNone(f.note)

    def test_a_fix_finds_the_word_again(self):
        ctx = field()
        f = LiveField(ctx)
        f.update(True, False, False)
        f.result(1, [issue(6, 'ze', ['że'], 'PL_DIACRITICS')])
        capture, corrected, data = f.fix('6:8:PL_DIACRITICS', 0)
        self.assertEqual((capture.lo, capture.hi, capture.source), (6, len(TEXT), TEXT[6:]))  # up to the caret
        self.assertEqual(corrected, 'że to dziala.')
        self.assertEqual(data, {'source': 'słownik', 'rule': 'PL_DIACRITICS'})
        ops = plan.replace_ops(capture, ctx.snapshot(), corrected)
        self.assertEqual(plan.expected(capture, ops).text, 'Mysle że to dziala.')
        self.assertEqual(ops.delete, (-13, 13))  # mutter deletes only around the caret: up to it
        ctx.surrounding('Ok. ' + TEXT, len(TEXT) + 4, len(TEXT) + 4)  # typed before it meanwhile: found 4 later
        capture, corrected, _ = f.fix('6:8:PL_DIACRITICS', 0)
        self.assertEqual((capture.lo, corrected[:2]), (10, 'że'))
        ctx.surrounding('Inny tekst.', 11, 11)
        self.assertEqual(f.fix('6:8:PL_DIACRITICS', 0)[:2], (None, 'word_gone'))
        self.assertEqual(f.fix('nie-ma', 0)[:2], (None, 'gone'))
        self.assertEqual(f.fix('6:8:PL_DIACRITICS', 5)[:2], (None, 'gone'))  # no such suggestion

    def test_marks_follow_the_text_last_sent(self):
        f = LiveField(field())
        check, = f.update(True, False, False)
        view = {'toolkit': ['GTK', '4.20.1'], 'caret': [164, 40, 0, 18], 'clip': [0, 30, 700, 300],
                'marks': [{'id': '0:5:SPELLING_PL', 'rects': [[12, 40, 40, 18]]}]}
        self.assertEqual(f.marks(check.field, check.caret, len(TEXT), view), [live.Marks(dict(view, v=1))])
        self.assertEqual(f.marks(check.field, 3, len(TEXT), view), [])  # an older caret: the words may have moved
        self.assertEqual(f.marks(check.field, check.caret, len(TEXT) - 1, view), [])  # an older text
        self.assertEqual(f.marks(check.field + 1, check.caret, len(TEXT), view), [])  # another field's
        self.assertEqual(f.marks(check.field, check.caret, len(TEXT), None), [live.Marks({'v': 1, 'marks': []})])
        self.assertEqual(f.marks(check.field, check.caret, len(TEXT), None), [])  # nothing drawn already
        f.marks(check.field, check.caret, len(TEXT), view)
        self.assertIn(live.Marks({'v': 1, 'marks': []}), f.clear())  # the focus went: the lines go too

    def test_field_numbers_are_never_shared_by_two_engines(self):
        """One live service answers every engine object: another's late answer is not this field's."""
        a, b = live.LiveField(field()), live.LiveField(field())
        self.assertIs(a.fields, live.FIELDS)
        check_a, = a.update(True, False, False)
        check_b, = b.update(True, False, False)
        self.assertNotEqual(check_a.field, check_b.field)
        self.assertEqual(b.result(check_a.field, [issue(0, 'Mysle', ['Myślę'])]), [])

    def test_locate(self):
        self.assertEqual(live.locate('cos', 9, 'Testuję cos, czego'), 8)
        self.assertIsNone(live.locate('cos', 0, 'cosinus i coś'))
        self.assertIsNone(live.locate('ze', 0, 'x' * 300 + ' ze'))  # too far


class EngineLogicLive(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(live, 'FIELDS', itertools.count(1))
        patcher.start()
        self.addCleanup(patcher.stop)

    def logic(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.capabilities(41)
        logic.key(0xff53, 114, 0)
        logic.surrounding(TEXT, len(TEXT), len(TEXT))
        check, = logic.live_update(True, False)
        logic.live_result(check.field, [issue(0, 'Mysle', ['Myślę', 'Mydle'])])
        return logic

    def test_a_click_on_a_suggestion_fixes_the_word_quietly(self):
        logic = self.logic()
        effects = logic.action(0, 'live:fix:0:5:SPELLING_PL:0')
        self.assertEqual([type(e) for e in effects], [Apply, ScheduleVerifyTimeout, Log])
        self.assertEqual(effects[0].ops.commit, 'ślę ze to dziala.')  # "My" stays: the change up to the caret
        self.assertEqual(effects[2].data['outcome'], 'sent')
        self.assertEqual(logic.session.state, APPLYING)
        effects = logic.surrounding('Myślę ze to dziala.', len(TEXT), len(TEXT))  # the echo
        self.assertEqual(logic.session.state, IDLE)
        self.assertFalse([e for e in effects if isinstance(e, Message)])  # no "Zastosowano" for a word
        self.assertEqual([(e.event, e.data['outcome']) for e in effects if isinstance(e, Log)], [('verified', 'ok')])

    def test_a_click_on_the_badge_is_the_hotkey(self):
        logic = self.logic()
        effects = logic.action(0, 'live:open')
        self.assertEqual(logic.session.state, 'waiting')
        self.assertIn(Clear(), effects)
        self.assertEqual(logic.live_update(True, False), [])  # a session runs: nothing checked, nothing shown

    def test_the_underline_card_ignores_and_learns(self):
        logic = self.logic()
        self.assertEqual(logic.action(0, 'live:ignore:0:5:SPELLING_PL'),
                         [live.Command({'cmd': 'ignore', 'word': 'Mysle', 'rule': 'SPELLING_PL'}),
                          Log('live_ignore', {'rule': 'SPELLING_PL'})])
        effects = logic.action(0, 'live:learn:0:5:SPELLING_PL')
        self.assertEqual(effects[0], live.Command({'cmd': 'learn', 'word': 'Mysle', 'rule': 'SPELLING_PL'}))
        self.assertEqual(effects[-1], Message('learned', 2))
        self.assertNotIn('Mysle', repr([e for e in effects if isinstance(e, Log)]))  # the log never holds text
        self.assertEqual(logic.action(0, 'live:learn:nie-ma'), [])

    def test_the_underline_card_turns_a_rule_off(self):
        logic = self.logic()
        grammar = dict(issue(6, 'ze', ['że'], rule='ZE_ZE'), source='LanguageTool', kind='grammar', rule_off=True)
        logic.live_result(logic.live.field, [issue(0, 'Mysle', ['Myślę']), grammar])
        effects = logic.action(0, 'live:rule_off:6:8:ZE_ZE')
        self.assertEqual(effects, [live.Command({'cmd': 'rule_off', 'word': 'ze', 'rule': 'ZE_ZE'}),
                                   Log('live_rule_off', {'rule': 'ZE_ZE'}), Message('rule_off', 3)])
        self.assertEqual(logic.action(0, 'live:rule_off:nie-ma'), [])

    def test_refused_fixes_say_so(self):
        logic = self.logic()
        logic.surrounding('Całkiem inny tekst.', 19, 19)
        effects = logic.action(0, 'live:fix:0:5:SPELLING_PL:0')
        self.assertEqual(effects[1], Message('fix_failed', 3))
        self.assertEqual(effects[2].data['outcome'], 'word_gone')
        self.assertEqual(logic.action(0, 'live:fix:bez-numeru'), [])

    def test_a_suggestion_whose_word_has_gone_leaves_the_badge(self):
        """A second click on it could not work either."""
        logic = self.logic()
        logic.surrounding('Całkiem inny tekst.', 19, 19)
        effects = logic.action(0, 'live:fix:0:5:SPELLING_PL:0')
        self.assertEqual(effects[0], live.Command({'cmd': 'fixed', 'field': 1, 'start': 0, 'end': 5}))  # the service drops it
        effects = logic.action(0, 'live:fix:0:5:SPELLING_PL:0')  # a click before the service's new list
        self.assertEqual(effects, [Message('fix_failed', 3), Log('live_fix', {'outcome': 'gone'})])

    def test_a_click_says_what_to_do(self):
        logic = self.logic()
        logic.surrounding(TEXT, 0, 5)
        self.assertEqual(logic.action(0, 'live:fix:0:5:SPELLING_PL:0')[0], Message('fix_selection', 3))
        logic.content_type(8, 0)
        self.assertEqual(logic.action(0, 'live:fix:0:5:SPELLING_PL:0')[0], Message('sensitive', 3))

    def clicked(self):
        """The field after a click that sent no update (GTK4 over text-input-v3, docs/architecture.md): only the caret
        is in doubt, and the badge still holds what the last check found."""
        logic = self.logic()
        logic.ctx.can_confirm = True
        logic.reset()
        logic.reset()
        logic.surrounding(TEXT, len(TEXT), len(TEXT))
        return logic

    def asked(self, logic):
        effects = logic.action(0, 'live:fix:0:5:SPELLING_PL:0')
        cid = effects[0].id if effects and isinstance(effects[0], ConfirmCaret) else None
        self.assertEqual(effects, [ConfirmCaret(cid, TEXT), ScheduleConfirmTimeout(cid, CONFIRM_MS)])
        return cid

    def test_a_click_with_the_caret_in_doubt_asks_atspi_as_the_hotkey_does(self):
        """2026-10-02 in GNOME Text Editor: one suggestion clicked three times in seven seconds, each time refused as stale."""
        logic = self.clicked()
        cid = self.asked(logic)
        effects = logic.caret_confirmed(cid, len(TEXT), len(TEXT))
        self.assertEqual(effects[0], Log('caret_confirmed', {'reason': 'stale', 'at': 'fix', 'app': None}))
        self.assertEqual([type(e) for e in effects[1:]], [Apply, ScheduleVerifyTimeout, Log])
        self.assertEqual(effects[-1].data['outcome'], 'sent')
        self.assertEqual(logic.confirm_timeout(cid), [])  # the timer after the answer

    def test_atspi_sees_another_caret_or_cannot_tell(self):
        for name, answer in (('differs', (3, 3)), ('unknown', (None, None))):
            with self.subTest(name):
                logic = self.clicked()
                cid = self.asked(logic)
                effects = logic.caret_confirmed(cid, *answer)
                self.assertEqual(effects, [Message('stale', 3), Log('live_fix', {
                    'source': 'słownik', 'rule': 'SPELLING_PL', 'atspi': name, 'outcome': 'stale'})])
                self.assertEqual(logic.session.state, IDLE)
                self.assertEqual(len(logic.live.issues), 1)  # the word is still there: an arrow key makes it work

    def test_no_answer_in_time(self):
        logic = self.clicked()
        cid = self.asked(logic)
        effects = logic.confirm_timeout(cid)
        self.assertEqual((effects[0], effects[1].data['atspi']), (Message('stale', 3), 'timeout'))
        self.assertEqual(logic.caret_confirmed(cid, len(TEXT), len(TEXT)), [])  # late: nothing
        self.assertEqual(logic.session.state, IDLE)

    def test_moved_on_meanwhile(self):
        logic = self.clicked()
        cid = self.asked(logic)
        effects = logic.key(ord('a'), 38, 0)
        self.assertIn(Log('caret_dropped', {'why': 'key', 'at': 'fix'}), effects)
        self.assertEqual(logic.caret_confirmed(cid, len(TEXT), len(TEXT)), [])
        logic = self.clicked()
        cid = self.asked(logic)
        logic.focus_out()
        logic.focus_in('gnome-shell')
        self.assertEqual(logic.caret_confirmed(cid, len(TEXT), len(TEXT)), [Log('caret_dropped', {'why': 'focus', 'at': 'fix'})])
        self.assertEqual(logic.session.state, IDLE)
        logic = self.logic()
        logic.key(0x67, 42, keys.CONTROL | keys.MOD1)  # a correction session runs
        self.assertEqual(logic.action(0, 'live:fix:0:5:SPELLING_PL:0')[-1].data['outcome'], 'busy')


if __name__ == '__main__':
    unittest.main()
