import itertools
import json
import unittest
from pathlib import Path
from unittest import mock
from gramadrama.engine import dictate, keys
from gramadrama.engine.context import FieldContext, Refusal
from gramadrama.engine.dictate import (HIDE, Dictation, HoldKey, Hud, Insert, Note, ScheduleArm, ScheduleOpen, ScheduleWatch,
                                      Speech)
from gramadrama.engine.session import APPLYING, IDLE, EngineLogic

CASES = json.loads((Path(__file__).resolve().parent / 'dictation_cases.json').read_text())
R = dictate.CONTROL_R
a, c = 0x61, 0x63


def field(text='Hej.', cursor=4, client='gnome-shell'):
    ctx = FieldContext()
    ctx.focus_in(client)
    ctx.set_caps(41)
    ctx.surrounding(text, cursor, cursor)
    return ctx


def kinds(effects):
    return [type(e).__name__ for e in effects]


DOWN = [ScheduleOpen(1), ScheduleArm(1, dictate.ARM_MS)]  # the right Ctrl came down: the microphone now, ARM_MS later


def listening(level=0.0, seconds=0.0, **more):
    return Hud(dict({'v': 1, 'state': 'listening', 'level': level, 'seconds': seconds}, **more))


class FreshIds(unittest.TestCase):
    """Dictation ids are process-wide (dictate.IDS): every test numbers its dictations from 1."""

    def setUp(self):
        patcher = mock.patch.object(dictate, 'IDS', itertools.count(1))
        patcher.start()
        self.addCleanup(patcher.stop)


class HoldKeyCases(unittest.TestCase):
    """macOS SelfTest.swift: HoldKeyMonitor's machine, event by event."""

    def test_mac_cases(self):
        for case in CASES['hold_key']:
            with self.subTest(case=case):
                hold = HoldKey(int(case['arm_delay'] * 1000))
                calls = {'keyDown': hold.key_down, 'keyUp': hold.key_up, 'otherKey': hold.other_key,
                         'armElapsed': lambda: hold.arm_elapsed(hold.gen), 'shift': hold.shift}
                actions = [calls[event]() or 'none' for event in case['events']]
                self.assertEqual(actions, case['actions'])
                if 'alternate' in case:  # the flag the machine ends with: the language of the dictation that follows
                    self.assertEqual(hold.alternate, case['alternate'])

    def test_stale_timer(self):
        hold = HoldKey()
        hold.key_down()
        hold.key_up()
        hold.key_down()
        self.assertIsNone(hold.arm_elapsed(1))  # the first press's timer
        self.assertEqual(hold.arm_elapsed(2), 'start')

    def test_leading_space_cases(self):
        for case in CASES['leading_space']:
            with self.subTest(case=case):
                self.assertEqual(dictate.leading_space(case['previous'], case['text']), case['expected'])


class DictationFlow(FreshIds):
    def start(self, ctx=None, ids=None, **armed):
        """Key-down, the microphone opened (its effects first) and ARM_MS: the effects of both."""
        d = Dictation(ctx or field(), ids=ids or itertools.count(1))
        self.assertEqual(d.key(R, False, 'app.desktop'), DOWN)
        args = dict(dict(enabled=True, ready=True, app_off=False), **armed)
        effects = d.opened(1, **args)
        return d, effects + d.armed(1, **args)

    def test_hold_record_release_insert(self):
        d, effects = self.start()
        self.assertEqual(effects, [Speech('start', 1), Speech('confirm', 1), listening(waiting=True)])
        self.assertEqual(d.event({'ev': 'listening', 'id': 1, 'ms': 120}), [listening()])  # audio: "Słucham…"
        self.assertEqual(d.event({'ev': 'something-new', 'id': 1}), [])
        self.assertTrue(d.active)
        self.assertEqual(d.event({'ev': 'level', 'id': 1, 'level': 0.5, 's': 1.25})[0].view['seconds'], 1.25)
        self.assertEqual(d.key(R, True), [Speech('stop', 1), ScheduleWatch(1, dictate.WATCH_MS)])
        self.assertEqual(d.event({'ev': 'recognizing', 'id': 1}), [Hud({'v': 1, 'state': 'recognizing'})])
        log = {'audio_ms': 1250, 'language': 'pl', 'asr_ms': 200, 'raw_chars': 15, 'cleanup': 'off', 'chars': 15}
        effects = d.event({'ev': 'result', 'id': 1, 'text': 'Kolejne zdanie.', 'note': '', 'log': log})
        self.assertEqual(effects[:2], [Insert(' Kolejne zdanie.'), HIDE])  # after "Hej." a space
        self.assertEqual(effects[2], Note('dictation', dict(log, outcome='inserted', app='app.desktop')))
        self.assertFalse(d.active)
        self.assertNotIn('Kolejne', json.dumps(effects[2].data))  # the log never holds text

    def test_short_holds_and_shortcuts_start_nothing(self):
        d = Dictation(field())
        d.key(R, False)
        self.assertEqual(d.opened(1, True, True, False), [Speech('start', 1)])
        self.assertEqual(d.event({'ev': 'level', 'id': 1, 'level': 0.4, 's': 0.05}), [])  # no pill before ARM_MS
        self.assertEqual(d.key(R, True), [Speech('cancel', 1)])  # let go before ARM_MS: the clip goes, no pill, no log
        self.assertFalse(d.active)
        self.assertEqual(d.armed(1, True, True, False), [])  # its timer comes too late
        d.key(R, False)
        self.assertEqual(d.opened(2, True, True, False), [Speech('start', 2)])
        self.assertEqual(d.key(c, False), [Speech('cancel', 2)])  # right Ctrl+C: a shortcut
        self.assertEqual(d.armed(2, True, True, False), [])
        self.assertEqual(d.key(c, True) + d.key(R, True), [])
        d.key(R, False)
        self.assertEqual(d.key(R, True), [])  # a tap quicker than the opening: nothing was opened
        self.assertEqual(d.opened(3, True, True, False), [])  # nor will be

    def test_the_microphone_opens_at_the_key_down(self):
        """A suspended USB microphone takes up to 2 s to wake: it opens off the key path right after the key-down,
        what it hears before ARM_MS is kept, and the pill says "Słucham…" only once audio has come."""
        d = Dictation(field(), ids=itertools.count(1))
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.opened(1, True, True, False), [Speech('start', 1)])
        self.assertTrue(d.active)  # one at a time
        self.assertFalse(d.shown)  # but live checking goes on: right Ctrl+C must not clear the badge
        self.assertEqual(d.event({'ev': 'listening', 'id': 1, 'ms': 150}), [])
        self.assertEqual(d.event({'ev': 'level', 'id': 1, 'level': 0.3, 's': 0.15, 'lang': 'PL'}), [])
        self.assertEqual(d.armed(1, True, True, False), [Speech('confirm', 1), listening(seconds=0.15, language='PL')])
        self.assertTrue(d.shown)
        # a cold microphone: the pill waits for the first audio
        d = Dictation(field(), ids=itertools.count(2))
        d.key(R, False, 'app.desktop')
        d.opened(1, True, True, False)
        self.assertEqual(d.armed(1, True, True, False), [Speech('confirm', 2), listening(waiting=True)])
        self.assertEqual(d.event({'ev': 'listening', 'id': 2, 'ms': 2000}), [listening()])
        self.assertEqual(d.event({'ev': 'level', 'id': 2, 'level': 0.5, 's': 0.05}), [listening(0.5, 0.05)])
        self.assertEqual(d.key(R, True), [Speech('stop', 2), ScheduleWatch(2, dictate.WATCH_MS)])
        # levels without a 'listening' first (an older service) count as audio too
        d = Dictation(field(), ids=itertools.count(3))
        d.key(R, False, 'app.desktop')
        d.opened(1, True, True, False)
        d.armed(1, True, True, False)
        self.assertEqual(d.event({'ev': 'level', 'id': 3, 'level': 0.5, 's': 0.4}), [listening(0.5, 0.4)])

    def test_the_microphone_stays_shut_where_dictation_could_not_start(self):
        for args in (dict(enabled=False), dict(app_off=True), dict(ready=False), dict(elsewhere=True)):
            with self.subTest(args=args):
                d = Dictation(field())
                d.key(R, False, 'app.desktop')
                self.assertEqual(d.opened(1, **dict(dict(enabled=True, ready=True, app_off=False), **args)), [])
                self.assertFalse(d.active)
        d = Dictation(field())
        d.key(R, False)
        self.assertEqual(d.opened(0, True, True, False), [])  # an older key-down's
        d, _ = self.start()
        d.key(R, True)  # processing
        d.key(R, False)
        self.assertEqual(d.opened(2, True, True, False), [])  # one at a time
        self.assertEqual(d.armed(2, True, True, False), [dictate.message('busy_dictation')])
        d = Dictation(field())
        d.key(R, False)
        d.opened(1, True, True, False)
        self.assertEqual(d.armed(1, False, True, False), [Speech('cancel', 1)])  # switched off meanwhile
        self.assertFalse(d.active)

    def test_another_key_while_recording_cancels(self):
        d, _ = self.start()
        d.event({'ev': 'level', 'id': 1, 'level': 0.2, 's': 2.0})
        effects = d.key(a, False)
        self.assertEqual(effects[:2], [Speech('cancel', 1), HIDE])
        self.assertEqual(effects[2].data, {'outcome': 'cancelled', 'audio_ms': 2000, 'app': 'app.desktop'})
        self.assertEqual(d.key(R, True), [])  # the key-up after it stops nothing
        self.assertEqual(d.event({'ev': 'result', 'id': 1, 'text': 'x', 'log': {}}), [])  # nor does a late result

    def test_refusals_at_the_start(self):
        for armed, expected in ((dict(enabled=False), []), (dict(app_off=True), []),
                                (dict(ready=False), [dictate.message('no_model', seconds=5), dictate.PointTo('speech')])):
            with self.subTest(armed=armed):
                d, effects = self.start(**armed)
                self.assertEqual(effects, expected)
                self.assertFalse(d.active)
                self.assertEqual(d.key(R, True), [])

    def test_the_text_goes_to_the_clipboard_when_it_cannot_be_inserted(self):
        ctx = field()
        for change, outcome in ((lambda: (ctx.focus_out(), ctx.focus_in('gnome-shell')), 'appChanged'),
                                (lambda: ctx.set_content_type(8, 0), 'secureField')):
            with self.subTest(outcome=outcome):
                ctx.__init__()
                ctx.focus_in('gnome-shell')
                d, _ = self.start(ctx)
                d.key(R, True)
                change()
                effects = d.event({'ev': 'result', 'id': 1, 'text': 'Tekst.', 'log': {}})
                self.assertEqual(effects[0], dictate.message(outcome, copy='Tekst.'))
                self.assertEqual(effects[1].data['outcome'], outcome)
                self.assertFalse([e for e in effects if isinstance(e, Insert)])
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(d.event({'ev': 'result', 'id': 1, 'text': 'Tekst.', 'log': {}}, busy=True)[0],
                         dictate.message('busy', copy='Tekst.'))

    def test_a_result_is_written_once_and_never_again(self):
        # macOS's rule for every write: after it was sent, nothing writes a second time. A second result with
        # the same id, a late one after the service was given up on, and one of an older dictation write nothing.
        result = {'ev': 'result', 'id': 1, 'text': 'Tekst.', 'log': {}}
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(kinds(d.event(result))[0], 'Insert')
        self.assertEqual(d.event(result), [])  # the same id again
        self.assertEqual(d.watch(1), [])  # the watchdog after the result: nothing to give up on
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(kinds(d.watch(1))[0], 'Hud')  # the service is given up on: "Rozpoznawanie mowy przerwało się"
        self.assertEqual(d.event(result), [])  # its late result is not inserted
        d, _ = self.start(ids=itertools.count(5))
        d.key(R, True)
        self.assertEqual(d.event(dict(result, id=1)), [])  # an older dictation's result

    def test_the_field_is_trusted_again_only_when_the_client_echoes_the_new_text(self):
        # Like after a correction: a hotkey right after the insert would capture the text from before it.
        ctx = field('Hej.', 4)
        ctx.key_passed(keys.RIGHT, 0)  # a caret key: the update that answers it is trusted
        ctx.surrounding('Hej.', 4, 4)
        d, _ = self.start(ctx)
        d.key(R, True)
        self.assertEqual(ctx.snapshot().text, 'Hej.')  # before: the field is known
        d.event({'ev': 'result', 'id': 1, 'text': 'Tak.', 'log': {}})
        self.assertEqual(ctx.snapshot(), Refusal('stale'))
        ctx.surrounding('Hej. Tak.', 9, 9)  # the echo
        self.assertEqual((ctx.snapshot().text, ctx.snapshot().cursor), ('Hej. Tak.', 9))
        # A field whose text is not known (Chrome before its first caret key): nothing is distrusted, so the
        # hint stays "press an arrow key" rather than turning into "this app gives no text".
        ctx = FieldContext()
        ctx.focus_in('gnome-shell')
        ctx.set_caps(41)
        self.assertEqual(ctx.snapshot(), Refusal('stale'))
        d, _ = self.start(ctx)
        d.key(R, True)
        self.assertEqual(kinds(d.event({'ev': 'result', 'id': 1, 'text': 'Tak.', 'log': {}}))[0], 'Insert')
        self.assertEqual(ctx.snapshot(), Refusal('stale'))

    def test_shift_makes_the_dictation_go_in_the_other_language(self):
        # macOS 918d2c4: Shift before or during the hold switches to the other language instead of cancelling.
        d, effects = self.start()  # Shift never came: the setting's language
        self.assertEqual(effects[0], Speech('start', 1))
        d = Dictation(field(), ids=itertools.count(1))
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.opened(1, True, True, False), [Speech('start', 1)])
        self.assertEqual(d.key(0xffe1, False), [])  # Shift_L while armed: no cancel
        self.assertEqual(d.hold.state, dictate.ARMED)
        self.assertEqual(d.armed(1, True, True, False)[:2], [Speech('alternate', 1), Speech('confirm', 1)])  # told at ARM_MS
        self.assertEqual(d.key(0xffe2, False), [])  # Shift_R, already alternate: nothing more
        d.key(R, True)
        d = Dictation(field(), ids=itertools.count(1))
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.key(0xffe1, False), [])  # Shift before the microphone opened: told with the start
        self.assertEqual(d.opened(1, True, True, False), [Speech('start', 1, True)])
        self.assertEqual(d.armed(1, True, True, False)[0], Speech('confirm', 1))
        d.key(R, True)
        d = Dictation(field(), ids=itertools.count(1))
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.key(0xffe1, False), [])
        self.assertEqual(d.armed(1, True, True, False)[0], Speech('start', 1, True))  # never opened: told at the start
        self.assertEqual(d.key(0xffe2, False), [])  # Shift_R, already alternate: nothing more
        self.assertEqual(d.key(0xffe1, True), [])  # letting go of Shift changes nothing
        self.assertEqual(d.key(R, True), [Speech('stop', 1), ScheduleWatch(1, dictate.WATCH_MS)])
        # Shift during the recording: the service is told once
        d, _ = self.start(ids=itertools.count(2))
        self.assertEqual(d.key(0xffe2, False), [Speech('alternate', 2)])
        self.assertEqual(d.key(0xffe1, False), [])
        self.assertEqual(d.key(R, True), [Speech('stop', 2), ScheduleWatch(2, dictate.WATCH_MS)])
        # Shift down before the key: the machine hears it right after the key-down
        d = Dictation(field(), ids=itertools.count(3))
        self.assertEqual(d.key(R, False, 'app.desktop', shift=True), DOWN)
        self.assertEqual(d.opened(1, True, True, False), [Speech('start', 3, True)])
        # Right Ctrl+Shift+T is a shortcut: the other key still ends it, before and after the start
        d = Dictation(field())
        d.key(R, False)
        d.opened(1, True, True, False)
        d.key(0xffe1, False)
        self.assertEqual(d.key(0x74, False), [Speech('cancel', 1)])
        self.assertEqual(d.armed(1, True, True, False), [])
        d, _ = self.start(ids=itertools.count(4))
        d.key(0xffe1, False)
        self.assertEqual(d.key(0x74, False)[0], Speech('cancel', 4))
        # The next dictation goes back to the setting's language
        d = Dictation(field(), ids=itertools.count(5))
        d.key(R, False, 'app.desktop', shift=True)
        d.armed(1, True, True, False)
        d.key(R, True)
        d.event({'ev': 'recognizing', 'id': 5})
        d.event({'ev': 'silence', 'id': 5, 'log': {}})
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.armed(2, True, True, False)[0], Speech('start', 6, False))
        # Shift alone, with no dictation going, does nothing
        d = Dictation(field())
        self.assertEqual((d.key(0xffe1, False), d.hold.state), ([], dictate.IDLE))

    def test_the_pill_shows_the_language_of_the_dictation(self):
        d, _ = self.start()
        view = d.event({'ev': 'level', 'id': 1, 'level': 0.5, 's': 1.0, 'lang': 'EN'})[0].view
        self.assertEqual((view['state'], view['language']), ('listening', 'EN'))
        d, _ = self.start(ids=itertools.count(2))
        view = d.event({'ev': 'level', 'id': 2, 'level': 0.5, 's': 1.2})[0].view  # an older service: no label
        self.assertNotIn('language', view)

    def test_nothing_heard_or_recognised(self):
        for ev, message_key, outcome in (({'ev': 'silence', 'log': {'audio_ms': 900, 'outcome': 'silence'}}, 'silence', 'silence'),
                                         ({'ev': 'result', 'text': '  ', 'log': {}}, 'empty', 'empty'),
                                         ({'ev': 'result', 'text': 'a￼b', 'log': {}}, 'empty', 'badResult')):
            with self.subTest(outcome=outcome):
                d, _ = self.start()
                d.key(R, True)
                effects = d.event(dict(ev, id=1))
                self.assertEqual(effects[0], dictate.message(message_key))
                self.assertEqual(effects[1].data['outcome'], outcome)
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(d.event({'ev': 'short', 'id': 1}), [HIDE])  # a tap: nothing said, nothing logged

    def test_errors_and_a_dead_service(self):
        mic_error = {'ev': 'error', 'id': 1, 'message': 'Nie mogę włączyć mikrofonu: x', 'log': {'outcome': 'mic_error'}}
        d, _ = self.start()
        effects = d.event(mic_error)
        self.assertEqual(effects[0].view['message'], 'Nie mogę włączyć mikrofonu: x')
        self.assertEqual(effects[1].data['outcome'], 'mic_error')
        self.assertEqual(d.key(R, True), [])  # the user still holds the key: its release stops nothing
        d, _ = self.start()
        self.assertEqual(d.died()[0], dictate.message('died'))
        self.assertEqual(d.died(), [])
        # before ARM_MS: an error is told only if the key is still held then, and a dead service is started again
        d = Dictation(field(), ids=itertools.count(1))
        d.key(R, False, 'app.desktop')
        d.opened(1, True, True, False)
        self.assertEqual(d.event(mic_error), [])
        effects = d.armed(1, True, True, False)
        self.assertEqual((effects[0].view['message'], effects[1].data['outcome']), ('Nie mogę włączyć mikrofonu: x', 'mic_error'))
        self.assertFalse(d.active)
        self.assertEqual(d.key(R, True), [])
        d.key(R, False)
        d.opened(2, True, True, False)
        d.event(dict(mic_error, id=2))
        self.assertEqual(d.key(c, False), [Speech('cancel', 2)])  # a shortcut: the error goes with the clip
        d = Dictation(field(), ids=itertools.count(3))
        d.key(R, False, 'app.desktop')
        d.opened(1, True, True, False)
        self.assertEqual(d.died(), [])
        self.assertEqual(d.armed(1, True, True, False), [Speech('start', 4), Speech('confirm', 4), listening(waiting=True)])

    def test_the_time_limit_and_notes(self):
        d, _ = self.start()
        self.assertEqual(d.event({'ev': 'recognizing', 'id': 1, 'limit': True}),  # 180 s, the key still held
                         [Hud({'v': 1, 'state': 'recognizing'}), ScheduleWatch(1, dictate.WATCH_MS)])
        self.assertEqual(d.key(R, True), [])
        effects = d.event({'ev': 'result', 'id': 1, 'text': 'Dłuższy tekst.', 'note': 'Serwer modelu niedostępny — …', 'log': {}})
        self.assertEqual(effects[1], dictate.message('Serwer modelu niedostępny — …', seconds=4))

    def test_ids_are_never_reused_by_another_engine(self):
        """IBus makes an engine object per input context and one speech service answers them all."""
        first, _ = self.start(ids=dictate.IDS)
        second, _ = self.start(ids=dictate.IDS)
        self.assertEqual((first.id, second.id), (1, 2))
        self.assertIs(Dictation(field()).ids, dictate.IDS)  # the default: one numbering for the process
        first.key(R, True)
        self.assertEqual(second.event({'ev': 'result', 'id': 1, 'text': 'x', 'log': {}}), [])  # not its answer
        self.assertTrue(second.active)
        d, effects = self.start(elsewhere=True)  # another engine still dictates: one at a time
        self.assertEqual(effects, [dictate.message('busy_dictation')])
        self.assertFalse(d.active)

    def test_the_watch_ends_a_result_that_never_comes(self):
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(d.watch(1)[0], dictate.message('died'))
        self.assertFalse(d.active)
        d, _ = self.start()
        d.key(R, True)
        self.assertEqual(d.watch(0), [])  # an older dictation's timer
        self.assertTrue(d.active)
        d.event({'ev': 'result', 'id': 1, 'text': 'Tak.', 'log': {}})
        self.assertEqual(d.watch(1), [])  # answered in time

    def test_a_cancel_before_arm_ms_starts_nothing(self):
        """disable() between the key-down and its timer: the timer must not start a recording."""
        d = Dictation(field())
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.cancel('disable'), [])
        self.assertEqual(d.armed(1, True, True, False), [])
        self.assertFalse(d.active)
        d.key(R, False, 'app.desktop')
        d.opened(2, True, True, False)
        self.assertEqual(d.cancel('disable'), [Speech('cancel', 1)])  # the open microphone shuts, no pill, no log
        self.assertEqual(d.armed(2, True, True, False), [])
        self.assertFalse(d.active)

    def test_a_focus_change_ends_the_recording(self):
        """The key-up may reach another engine object: the recording stops here, the text is not lost."""
        ctx = field()
        d, _ = self.start(ctx)
        ctx.focus_out()
        self.assertEqual(d.focus_changed(), [Speech('stop', 1), ScheduleWatch(1, dictate.WATCH_MS)])
        self.assertEqual(d.key(R, True), [])
        ctx.focus_in('gnome-shell')
        effects = d.event({'ev': 'result', 'id': 1, 'text': 'Tekst.', 'log': {}})
        self.assertEqual(effects[0], dictate.message('appChanged', copy='Tekst.'))
        d = Dictation(field())
        d.key(R, False, 'app.desktop')
        self.assertEqual(d.focus_changed(), [])  # before ARM_MS: the new field gets nothing
        self.assertEqual(d.armed(1, True, True, False), [])
        d.key(R, False, 'app.desktop')
        d.opened(2, True, True, False)
        self.assertEqual(d.focus_changed(), [Speech('cancel', 1)])  # nor what the open microphone heard
        self.assertEqual(d.armed(2, True, True, False), [])

    def test_leading_space_follows_the_field(self):
        for text, cursor, anchor, inserted in (('Hej.', 4, 4, ' Tak.'), ('', 0, 0, 'Tak.'), ('(', 1, 1, 'Tak.'),
                                               ('Ala ma kota', 6, 3, ' Tak.'), ('Ala ma kota', 4, 6, 'Tak.'),
                                               ('Ala ma', 0, 0, 'Tak.')):
            with self.subTest(text=text, cursor=cursor):
                ctx = field(text, cursor)
                ctx.surrounding(text, cursor, anchor)
                d, _ = self.start(ctx)
                d.key(R, True)
                self.assertEqual(d.event({'ev': 'result', 'id': 1, 'text': 'Tak.', 'log': {}})[0], Insert(inserted))


class EngineLogicDictation(FreshIds):
    """The right Ctrl through EngineLogic.key: listened to, never consumed."""

    def test_right_ctrl_is_passed_and_heard(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        effects = logic.key(R, 105, 0)
        self.assertEqual(kinds(effects), ['ScheduleOpen', 'ScheduleArm', 'ChainUp'])
        self.assertEqual(logic.dictation_open(1, True, True, ('app.desktop', 'wayland'), frozenset()), [Speech('start', 1)])
        effects = logic.dictation_armed(1, True, True, ('app.desktop', 'wayland'), frozenset())
        self.assertEqual(kinds(effects), ['Speech', 'Hud'])
        self.assertEqual(kinds(logic.key(R, 105, keys.CONTROL | keys.RELEASE)), ['Speech', 'ScheduleWatch', 'ChainUp'])

    def test_shift_in_the_key_state_is_heard(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        self.assertEqual(kinds(logic.key(R, 105, keys.SHIFT)), ['ScheduleOpen', 'ScheduleArm', 'ChainUp'])  # Shift was down first
        effects = logic.dictation_armed(1, True, True, ('app.desktop', 'wayland'), frozenset())
        self.assertEqual(effects[0], Speech('start', 1, True))
        logic.key(R, 105, keys.CONTROL | keys.RELEASE)
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.key(R, 105, 0)
        logic.dictation_armed(1, True, True, ('app.desktop', 'wayland'), frozenset())
        effects = logic.key(0xffe1, 50, keys.CONTROL)  # Shift during the recording
        self.assertEqual([e for e in effects if isinstance(e, Speech)], [Speech('alternate', logic.dictation.id)])  # ids are process-wide
        self.assertEqual(effects[-1].__class__.__name__, 'ChainUp')  # the key still reaches the app

    def test_off_in_a_disabled_app(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.key(R, 105, 0)
        self.assertEqual(logic.dictation_open(1, True, True, ('chat.desktop', 'wayland'), frozenset({'chat.desktop'})), [])
        self.assertEqual(logic.dictation_armed(1, True, True, ('chat.desktop', 'wayland'), frozenset({'chat.desktop'})), [])

    def start_correction(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.capabilities(41)
        logic.key(0xff53, 114, 0)  # a caret key: the next update is trusted
        logic.surrounding('Ala ma kota.', 3, 3)
        logic.key(0xffe3, 37, 0)
        logic.key(0xffe9, 64, keys.CONTROL)
        logic.key(0x67, 42, keys.CONTROL | keys.MOD1)  # Ctrl+Alt+G
        logic.ui_flushed(True)
        self.assertNotEqual(logic.session.state, IDLE)
        for keyval, code, state in ((0x67, 42, keys.CONTROL | keys.MOD1 | keys.RELEASE),
                                    (0xffe9, 64, keys.CONTROL | keys.MOD1 | keys.RELEASE), (0xffe3, 37, keys.CONTROL | keys.RELEASE)):
            logic.key(keyval, code, state)
        return logic

    def test_the_right_ctrl_is_a_ctrl_during_a_correction(self):
        """Holding the right Ctrl before Ctrl+1..6 in the panel must not end the correction."""
        logic = self.start_correction()
        state = logic.session.state
        self.assertNotIn('ScheduleArm', kinds(logic.key(R, 105, 0)))
        self.assertEqual(logic.dictation_open(logic.dictation.hold.gen, True, True), [])
        self.assertEqual(logic.dictation_armed(logic.dictation.hold.gen, True, True), [])
        self.assertEqual(logic.session.state, state)
        logic.key(R, 105, keys.CONTROL | keys.RELEASE)
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.key(R, 105, 0)  # the key went down first, then a correction started before ARM_MS
        logic.dictation_open(1, True, True)
        logic.session.state = APPLYING
        self.assertEqual(logic.dictation_armed(1, True, True), [Speech('cancel', 1)])
        self.assertFalse(logic.dictation.active)
        self.assertEqual(logic.dictation.hold.state, dictate.IDLE)

    def test_disable_cancels_a_recording(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.key(R, 105, 0)
        logic.dictation_armed(1, True, True)
        effects = logic.disable()
        self.assertEqual(effects[:2], [Speech('cancel', 1), HIDE])
        self.assertEqual(logic.speech_event({'ev': 'result', 'id': 1, 'text': 'x', 'log': {}}), [])

    def test_a_result_while_a_correction_is_applied_goes_to_the_clipboard(self):
        logic = EngineLogic()
        logic.focus_in('gnome-shell')
        logic.key(R, 105, 0)
        logic.dictation_armed(1, True, True)
        logic.key(R, 105, keys.CONTROL | keys.RELEASE)
        logic.session.state = APPLYING
        self.assertEqual(logic.speech_event({'ev': 'result', 'id': 1, 'text': 'Tekst.', 'log': {}})[0],
                         dictate.message('busy', copy='Tekst.'))


if __name__ == '__main__':
    unittest.main()
