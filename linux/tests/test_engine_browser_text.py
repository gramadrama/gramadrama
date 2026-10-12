import json
import pathlib
import unittest

from gramadrama.engine import browser_text as BT, dictate, keys
from gramadrama.engine.context import Refusal
from gramadrama.engine.session import (APPLYING, IDLE, READY, WAITING, Consume, HideUI, Message, ResetCompose,
                                       StartBackend)
from test_engine_session import CODES, P2, Driver, g

REPO = pathlib.Path(__file__).resolve().parents[2]  # tests/ime_cases.json is written by the Mac
CHROME = 'google-chrome'


def reply(before='', selected='', after='', start=0, short=True, client='7:0:1', **more):
    """The extension's answer to `context` (content.js), as the hub hands it on."""
    location = start + BT.u16(before)
    return dict({'ok': True, 'client': client, 'windowStart': start, 'selectionLocation': location,
                 'selectionLength': BT.u16(selected), 'before': before, 'selected': selected, 'after': after,
                 'shortAfter': short, 'site': 'mail.example'}, **more)


def written(document, edits):
    """The field after the extension's edits, applied in the order given (each one at its own UTF-16 place)."""
    units = document.encode('utf-16-le', 'surrogatepass')
    for edit in edits:
        a, b = 2 * edit['location'], 2 * (edit['location'] + edit['length'])
        units = units[:a] + edit['text'].encode('utf-16-le', 'surrogatepass') + units[b:]
    return units.decode('utf-16-le', 'surrogatepass')


class SharedImeCasesTests(unittest.TestCase):
    """tests/ime_cases.json, as the Mac's ImeScope and ImeEdits decide them for its browser channel. Linux takes the selection
    or the caret's paragraph (PANEL.md), never the whole field or a mail's new message; a candidate the Mac would refuse to
    take ("cut", an answer that does not add up) is the IBus way here (FALLBACK), as it is the usual way there."""

    cases = json.loads((REPO / 'tests' / 'ime_cases.json').read_text(encoding='utf-8'))
    REFUSALS = {'attachments': Refusal('embedded'), 'emptyParagraph': Refusal('empty'), 'noText': BT.FALLBACK,
                'cut': BT.FALLBACK}

    def test_scope(self):
        for case in self.cases['scope']:
            with self.subTest(case['name']):
                answer = dict(case['reply'], ok=True, client='1:0:1')
                answer.setdefault('selectionLength', BT.u16(answer.get('selected', '')))
                answer.setdefault('selectionLocation', answer['windowStart'] + BT.u16(answer['before']))
                got = BT.capture(answer, CHROME)
                if 'refusal' in case:
                    self.assertEqual(got, self.REFUSALS[case['refusal']])
                    continue
                wanted = next(c for c in case['candidates'] if c['scope'] in ('selection', 'paragraph')
                              or '\n' not in c['text'])
                self.assertIsInstance(got, BT.Capture, got)
                self.assertEqual((got.source, got.location, got.length),
                                 (wanted['text'], wanted['location'], BT.u16(wanted['text'])))
                self.assertEqual((got.client, got.browser), ('1:0:1', CHROME))

    def test_edits(self):
        for case in self.cases['edits']:
            original, corrected, base = case['original'], case['corrected'], case['base']
            with self.subTest(case['name']):
                planned = BT.edits(original, corrected, base)
                if case.get('noChange'):
                    self.assertIsNone(planned)
                    continue
                if 'failure' in case:
                    self.assertEqual(planned, Refusal('line_structure'))
                    continue
                changes, expected = planned
                self.assertEqual(expected, case['expected'])
                document = 'x' * base + original + ' koniec'
                self.assertEqual(written(document, changes), 'x' * base + case['expected'] + ' koniec')
                if 'maxEdits' in case:
                    self.assertLessEqual(len(changes), case['maxEdits'])
                for kept in case.get('untouched', ()):
                    at = BT.u16(original[:original.index(kept)]) + base
                    for change in changes:
                        self.assertFalse(change['location'] < at + BT.u16(kept) and at < change['location'] + change['length'],
                                         (kept, change))
                if case.get('noSeparatorInEdits'):
                    self.assertFalse(any(sep in c['text'] for c in changes for sep in ('\n', '\r', '\x0b')), changes)


class CaptureTests(unittest.TestCase):
    def test_a_password_field_is_refused_not_passed_on(self):
        self.assertEqual(BT.capture({'ok': False, 'reason': 'secure_input', 'attempted': False}, CHROME), Refusal('sensitive'))

    def test_no_field_and_no_answer_are_the_ibus_way(self):
        for answer in ({'ok': False, 'reason': 'no_client', 'attempted': False}, {'ok': False, 'reason': 'timeout'},
                       {'ok': False, 'reason': 'disconnected'}, {'ok': False, 'reason': 'no_extension'},
                       {'ok': False, 'reason': 'site_off'}, reply(before='  ')):
            with self.subTest(answer.get('reason')):
                self.assertEqual(BT.capture(answer, CHROME), BT.FALLBACK)

    def test_a_long_selection_is_refused(self):
        self.assertEqual(BT.capture(reply(before='a ', selected='', selectionTooLong=True, selectionLength=9000), CHROME),
                         Refusal('too_long'))
        self.assertEqual(BT.capture(reply(selected='słowo ' * 1001), CHROME), Refusal('too_long'))
        self.assertEqual(BT.capture(reply(before='Akapit ' * 1000, after='koniec.'), CHROME), Refusal('too_long'))

    def test_the_place_is_counted_in_utf16_units(self):
        got = BT.capture(reply(before='😀 Pierwszy.\nDrugi z bled', after='em.', start=40), CHROME)
        self.assertEqual((got.source, got.location, got.length), ('Drugi z bledem.', 40 + BT.u16('😀 Pierwszy.\n'), 15))

    def test_a_selection_with_an_image_is_refused(self):
        self.assertEqual(BT.capture(reply(before='Ala ', selected='ma ￼ kota'), CHROME), Refusal('embedded'))


class EditTests(unittest.TestCase):
    def test_from_the_end_back_so_each_place_holds(self):
        changes, expected = BT.edits('Wysylam poprawiona wersje.', 'Wysyłam poprawioną wersję.', 10)
        self.assertEqual([c['location'] for c in changes], sorted((c['location'] for c in changes), reverse=True))
        self.assertEqual(written('0123456789Wysylam poprawiona wersje.', changes), '0123456789' + expected)

    def test_many_changes_are_one_span(self):
        original = ' '.join('slowo%d' % i for i in range(20))
        corrected = ' '.join('słowo%d' % i for i in range(20))
        changes, _ = BT.edits(original, corrected, 0)
        self.assertEqual(len(changes), 1)
        self.assertEqual(written(original, changes), corrected)

    def test_a_field_with_only_lf_refuses_a_new_line_count_too(self):
        # The extension replaces within one line of a rich field: a new line end could not be written word by word.
        self.assertEqual(BT.edits('Raz dwa', 'Raz\ndwa', 0), Refusal('line_structure'))


class OutcomeTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(BT.outcome({'ok': True, 'attempted': True, 'landedAfterMs': 15}), 'landed')
        for reason in ('changed', 'client_changed', 'not_text', 'expired', 'no_client'):
            self.assertEqual(BT.outcome({'ok': False, 'reason': reason, 'attempted': False}), 'refused', reason)
        self.assertEqual(BT.outcome({'ok': False, 'reason': 'no_extension', 'attempted': False}), 'refused')
        # Written in part, or its answer lost: nobody knows what the field has.
        for answer in ({'ok': False, 'reason': 'not_text', 'attempted': True}, {'ok': False, 'reason': 'not_landed', 'attempted': True},
                       {'ok': False, 'reason': 'extension_error'}, {'ok': False, 'reason': 'timeout'},
                       {'ok': False, 'reason': 'disconnected'}):
            self.assertEqual(BT.outcome(answer), 'unknown', answer)

    def test_the_character_before_the_caret(self):
        self.assertEqual(BT.previous_char(reply(before='Hej.')), '.')
        self.assertIsNone(BT.previous_char(reply(before='')))
        self.assertIsNone(BT.previous_char(reply(before='Hej ', selected='to')))


def requests(effects):
    return [e for e in effects if isinstance(e, BT.BrowserRequest)]


class PageDriver(Driver):
    """The Driver of test_engine_session with Chrome focused and its extension connected: the hotkey asks the page."""

    def __init__(self, **kw):
        kw.setdefault('focus', ('google-chrome.desktop', 'window'))
        super().__init__(**kw)

    def hotkey(self, key=g):
        effects = self.run(self.logic.key(key, CODES[key], keys.CONTROL | keys.MOD1, self.focus, self.disabled, self.modes,
                                          CHROME))
        self.run(self.logic.key(key, CODES[key], keys.CONTROL | keys.MOD1 | keys.RELEASE, self.focus, self.disabled,
                                self.modes, CHROME))
        return effects

    def answer(self, request, answer):
        return self.run(self.logic.browser_answer(request.kind, request.id, answer))


class SessionTests(unittest.TestCase):
    def started(self, page=reply(before='Pierwszy.\nDrugi z bled', after='em.\nTrzeci.'), source='Drugi z bledem.'):
        d = PageDriver(text=None)  # X11 Chrome: IBus has no text at all
        effects = d.hotkey()
        (ask,) = requests(effects)
        self.assertEqual((ask.kind, ask.browser, ask.op, ask.fields), ('hotkey', CHROME, 'context', {}))
        self.assertEqual(effects, [ResetCompose(), ask, Consume()])
        self.assertEqual(d.state, IDLE)  # nothing shown until the page answers, as nothing is for AT-SPI
        effects = d.answer(ask, page)
        self.assertEqual(effects[0], StartBackend(d.logic.session.id, source, 'fix'))
        self.assertEqual(d.state, WAITING)
        return d

    def test_the_page_gives_the_paragraph_and_the_correction_goes_back_word_by_word(self):
        d = self.started()
        self.assertEqual(d.logic.session.panel_view()['scope'], 'paragraph')
        d.result('Drugi z błędem.')
        self.assertEqual(d.state, READY)
        effects = d.press(0xff0d)  # Enter
        (write,) = requests(effects)
        self.assertEqual((write.kind, write.op, write.seconds, write.write), ('apply', 'replace', 2.5, 1.2))
        self.assertEqual(write.fields, {'client': '7:0:1', 'check': {'location': 10, 'length': 15, 'text': 'Drugi z bledem.'},
                                        'edits': [{'location': 18, 'length': 6, 'text': 'błędem'}],
                                        'expected': 'Drugi z błędem.'})
        self.assertEqual(d.state, APPLYING)
        # The extension focuses the field it writes to: IBus sees a focus change, and the session waits on.
        d.run(d.logic.focus_out())
        d.run(d.logic.focus_in('gnome-shell'))
        self.assertEqual(d.state, APPLYING)
        effects = d.answer(write, {'ok': True, 'attempted': True, 'landedAfterMs': 12})
        self.assertEqual(effects, [Message('applied', 1.5)])
        self.assertEqual(d.state, IDLE)
        verified = [l for l in d.logs if l.event == 'verified'][-1]
        self.assertEqual((verified.data['outcome'], verified.data['via']), ('ok', 'extension'))

    def test_a_field_that_changed_meanwhile_is_left_as_it_is(self):
        d = self.started()
        d.result('Drugi z błędem.')
        (write,) = requests(d.press(0xff0d))
        self.assertEqual(d.answer(write, {'ok': False, 'reason': 'changed', 'attempted': False}), [Message('changed', 4)])
        self.assertEqual(d.state, IDLE)

    def test_an_answer_that_never_came_is_no_refusal(self):
        d = self.started()
        d.result('Drugi z błędem.')
        (write,) = requests(d.press(0xff0d))
        self.assertEqual(d.answer(write, {'ok': False, 'reason': 'timeout'}), [Message('verify_mismatch', 4)])

    def test_unchanged_closes(self):
        d = self.started()
        d.result('Drugi z bledem.')
        self.assertEqual(d.press(0xff0d)[0], HideUI())
        self.assertEqual(d.state, IDLE)

    def test_a_changed_line_count_is_refused_in_a_page(self):
        d = self.started(reply(before='Ala ', selected='ma kota i psa', after='.'), 'ma kota i psa')
        d.result('ma kota\ni psa')
        effects = d.press(0xff0d)
        self.assertEqual(requests(effects), [])
        self.assertEqual(effects[0], Message('line_structure', 4))

    def test_the_selection_of_a_page(self):
        d = self.started(reply(before='Ala ', selected='ma kota', after=' i psa'), 'ma kota')
        self.assertEqual(d.logic.session.capture.scope, 'selection')
        self.assertEqual((d.logic.session.capture.location, d.logic.session.capture.length), (4, 7))

    def test_no_field_in_the_page_is_the_ibus_way(self):
        d = PageDriver()  # IBus knows the field (Wayland, a field it has the text of)
        (ask,) = requests(d.hotkey())
        effects = d.answer(ask, {'ok': False, 'reason': 'no_client', 'attempted': False})
        self.assertEqual(effects[0], StartBackend(d.logic.session.id, P2, 'fix'))
        self.assertIn('browser_fallback', [l.event for l in d.logs])
        self.assertFalse(d.logic.session.in_page)

    def test_without_text_in_ibus_either_the_usual_refusal(self):
        d = PageDriver(text=None, client='gtk3-im:google-chrome')
        d.run(d.logic.capabilities(9))
        (ask,) = requests(d.hotkey())
        self.assertEqual(d.answer(ask, {'ok': False, 'reason': 'timeout'}), [Message('no_text', 3)])

    def test_a_password_field_in_the_page_is_refused(self):
        d = PageDriver(text=None)
        (ask,) = requests(d.hotkey())
        self.assertEqual(d.answer(ask, {'ok': False, 'reason': 'secure_input', 'attempted': False}), [Message('sensitive', 3)])

    def test_typing_meanwhile_drops_the_question(self):
        d = PageDriver(text=None)
        (ask,) = requests(d.hotkey())
        d.press(0x61)  # a
        self.assertEqual(d.answer(ask, reply(before='Tekst')), [])
        self.assertEqual(d.state, IDLE)
        self.assertIn('browser_dropped', [l.event for l in d.logs])

    def test_an_old_answer_is_ignored(self):
        d = PageDriver(text=None)
        (first,) = requests(d.hotkey())
        (second,) = requests(d.hotkey())
        self.assertEqual(d.answer(first, reply(before='Tekst')), [])
        self.assertEqual(d.answer(second, reply(before='Tekst'))[0], StartBackend(d.logic.session.id, 'Tekst', 'fix'))

    def test_without_the_extension_nothing_changes(self):
        d = Driver()
        effects = d.hotkey()
        self.assertEqual(requests(effects), [])
        self.assertEqual(effects[1], StartBackend(1, P2, 'fix'))

    def test_the_badge_asks_the_page_too(self):
        d = PageDriver(text=None)
        effects = d.run(d.logic.action(0, 'live:open', CHROME))
        self.assertEqual([r.kind for r in requests(effects)], ['hotkey'])


class DictationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Driver().logic.ctx
        self.dictation = dictate.Dictation(self.ctx)
        self.dictation.key(dictate.CONTROL_R, False, 'google-chrome.desktop')
        self.dictation.armed(self.dictation.hold.gen, True, True, False)
        self.dictation.key(dictate.CONTROL_R, True)

    def result(self, text='Kolejne zdanie', **ev):
        return self.dictation.event(dict({'ev': 'result', 'id': self.dictation.id, 'text': text}, **ev), browser=CHROME)

    def test_the_page_is_asked_then_the_text_goes_in_with_its_space(self):
        (ask,) = requests(self.result())
        self.assertEqual((ask.kind, ask.op), ('dictation', 'context'))
        self.assertTrue(self.dictation.active)
        (insert,) = requests(self.dictation.page_answer('dictation', ask.id, reply(before='Hej.', client='3:0:9')))
        self.assertEqual((insert.kind, insert.op, insert.write), ('insert', 'insert', 1.2))
        self.assertEqual(insert.fields, {'text': ' Kolejne zdanie', 'client': '3:0:9'})
        effects = self.dictation.page_answer('insert', insert.id, {'ok': True, 'attempted': True, 'landedAfterMs': 5})
        self.assertEqual(effects[0], dictate.HIDE)
        self.assertEqual(effects[1].data['outcome'], 'inserted')
        self.assertFalse(self.dictation.active)

    def test_no_field_in_the_page_is_commit_text(self):
        (ask,) = requests(self.result())
        effects = self.dictation.page_answer('dictation', ask.id, {'ok': False, 'reason': 'no_client', 'attempted': False})
        self.assertIsInstance(effects[0], dictate.Insert)

    def test_a_password_field_gets_nothing(self):
        (ask,) = requests(self.result())
        effects = self.dictation.page_answer('dictation', ask.id, {'ok': False, 'reason': 'secure_input', 'attempted': False})
        self.assertEqual(effects[0].view['copy'], 'Kolejne zdanie')
        self.assertFalse(any(isinstance(e, dictate.Insert) for e in effects))

    def test_an_insert_that_may_have_landed_is_not_done_twice(self):
        (ask,) = requests(self.result())
        (insert,) = requests(self.dictation.page_answer('dictation', ask.id, reply(before='')))
        effects = self.dictation.page_answer('insert', insert.id, {'ok': False, 'reason': 'not_landed', 'attempted': True})
        self.assertEqual(effects[0].view['message'], dictate.MESSAGES['uncertain'])
        self.assertEqual(effects[0].view['copy'], 'Kolejne zdanie')
        self.assertFalse(any(isinstance(e, dictate.Insert) for e in effects))

    def test_another_field_in_the_page_gets_the_clipboard(self):
        (ask,) = requests(self.result())
        (insert,) = requests(self.dictation.page_answer('dictation', ask.id, reply(before='')))
        effects = self.dictation.page_answer('insert', insert.id, {'ok': False, 'reason': 'client_changed', 'attempted': False})
        self.assertEqual(effects[0].view['message'], dictate.MESSAGES['appChanged'])

    def test_a_refused_insert_goes_the_ibus_way(self):
        (ask,) = requests(self.result())
        (insert,) = requests(self.dictation.page_answer('dictation', ask.id, reply(before='')))
        effects = self.dictation.page_answer('insert', insert.id, {'ok': False, 'reason': 'exec_failed', 'attempted': False})
        self.assertIsInstance(effects[0], dictate.Insert)

    def test_without_the_extension_commit_text_as_before(self):
        effects = self.dictation.event({'ev': 'result', 'id': self.dictation.id, 'text': 'Tekst'})
        self.assertIsInstance(effects[0], dictate.Insert)


if __name__ == '__main__':
    unittest.main()
