"""Live checking in the pages of a browser, through its extension (gramadrama/engine/browser_live.py, docs/browser.md): the
field's text from the extension is checked like IBus's, the boxes of the words in the page become the window's coordinates
(zoom, the pointer's pair), a click on a suggestion is a `replace` in that field. With a fake hub and a fake glue: no browser."""
import unittest

from gramadrama.engine import browser_live as B, live as L

ISSUE = {'id': 'i1', 'start': 0, 'end': 5, 'word': 'Mysle', 'suggestions': ['Myślę', 'Mysłe'], 'message': 'Nieznane słowo',
         'kind': 'spelling', 'source': 'Enchant', 'rule': 'SPELLING_PL'}


class FakeHub:
    def __init__(self):
        self.asked = []  # (browser, op, callback, timeout, fields)
        self.connected = {'google-chrome'}

    def request(self, browser, op, callback, timeout=3.0, **fields):
        if browser not in self.connected:
            return None
        self.asked.append((browser, op, callback, timeout, fields))
        return 'e%d' % len(self.asked)

    def last(self, op):
        return next(a for a in reversed(self.asked) if a[1] == op)

    def answer(self, op, reply):
        item = self.last(op)
        self.asked.remove(item)
        item[2](reply)

    def ops(self):
        return [a[1] for a in self.asked]


class FakeOut:
    def __init__(self):
        self.calls = []
        self.off_sites = set()

    def __getattr__(self, name):
        if name in ('text', 'clear_service', 'fixed', 'command', 'badge', 'marks', 'log', 'message'):
            return lambda *a, **k: self.calls.append((name, a, k))
        raise AttributeError(name)

    def sites(self):
        return self.off_sites

    def of(self, name):
        return [(a, k) for n, a, k in self.calls if n == name]


def live_reply(before='Mysle ze to dziala dobrze.', selected='', after='', start=0, short=True, **more):
    return dict({'ok': True, 'client': '5:0:1', 'windowStart': start, 'selectionLocation': start + len(before),
                 'selectionLength': len(selected), 'before': before, 'selected': selected, 'after': after, 'shortAfter': short,
                 'site': 'mail.example', 'zoom': 1}, **more)


# A window that is not maximized, on Wayland: Chrome's shadow is 12 px on the left and 10 above it, so the page starts at
# (12, 97) of the window's client rect (measured in the isolated session: 1024×665 around a frame of 1000×640).
GEOMETRY = {'dpr': 1, 'win': [0, 0, 1024, 665, 1000, 553], 'mouse': [312, 310, 300, 213], 'top': True}


def rects_reply(boxes, mouse=(300, 400, 300, 313), zoom=1, field=(100, 200, 406, 106), top=True):
    return {'ok': True, 'words': boxes, 'field': list(field), 'zoom': zoom,
            'geometry': {'dpr': zoom, 'win': [0, 0, 1280, 768, 1280, 681], 'mouse': list(mouse) if mouse else None, 'top': top}}


class Base(unittest.TestCase):
    def setUp(self):
        self.hub, self.out = FakeHub(), FakeOut()
        self.live = B.BrowserLive(self.hub, self.out)

    def tick(self, browser='google-chrome', enabled=True, off=False, busy=False):
        self.live.tick(browser, enabled, off, busy)

    def read(self, **fields):
        self.tick()
        self.hub.answer('live', live_reply(**fields))

    def field_number(self):
        return self.out.of('text')[-1][0][0]


class OriginTests(unittest.TestCase):
    def test_the_pointer_gives_where_the_page_starts_in_the_window(self):
        # at 100%: the pointer at (300, 400) in the window and at (300, 313) in the page: the browser's bar is 87 high
        self.assertEqual(B.page_origin({'mouse': [300, 400, 300, 313]}, 1), (0, 87))
        # at 150%: the same pointer is at (200, 208.67) in the page, whose pixels are 1.5 DIP
        x, y = B.page_origin({'mouse': [300, 400, 200, 208.6666]}, 1.5)
        self.assertAlmostEqual(x, 0, places=2)
        self.assertAlmostEqual(y, 87, places=2)
        self.assertEqual(B.page_origin({'mouse': [310, 420, 10, 20]}, 1), (300, 400))  # a side bar of 300 and a bar of 400

    def test_without_the_pointer_the_browsers_chrome_does_it_for_the_top_frame(self):
        geometry = {'mouse': None, 'top': True, 'win': [0, 0, 1280, 768, 1280, 681]}
        self.assertEqual(B.page_origin(geometry, 1), (0, 87))
        x, y = B.page_origin({'mouse': None, 'top': True, 'win': [0, 0, 1280, 768, 853, 454]}, 1.5)
        self.assertAlmostEqual(y, 768 - 454 * 1.5)
        self.assertEqual(B.page_origin({'mouse': None, 'top': False, 'win': [0, 0, 1, 1, 1, 1]}, 1), None)  # a frame inside a page

    def test_a_frame_inside_the_page_from_where_its_parents_put_it(self):
        # An editor in an <iframe> at (40, 120) of the top page; no pointer over it ever. The top frame's size gives its start.
        frame = {'origin': [40, 120], 'inner': [1280, 681], 'mouse': None}
        geometry = {'mouse': None, 'top': False, 'win': [0, 0, 1280, 768, 600, 300], 'frame': frame}
        self.assertEqual(B.page_origin(geometry, 1), (40, 207))
        x, y = B.page_origin(dict(geometry, frame=dict(frame, inner=[853, 454])), 1.5)
        self.assertAlmostEqual(y, 768 - 454 * 1.5 + 180)
        # The top frame's pointer is better (a side bar), and the inner frame's own one is old once the top page scrolled.
        top_pointer = dict(frame, mouse=[310, 420, 10, 20])
        self.assertEqual(B.page_origin(dict(geometry, frame=top_pointer, mouse=[999, 999, 1, 1]), 1), (340, 520))
        # Parents that did not answer: the inner frame's pointer, as before, or nothing.
        self.assertEqual(B.page_origin(dict(geometry, frame=None, mouse=[310, 420, 10, 20]), 1), (300, 400))
        self.assertIsNone(B.page_origin(dict(geometry, frame={'origin': [1, 'a']}), 1))

    def test_what_is_not_geometry(self):
        for bad in (None, {}, {'mouse': [1, 2], 'top': False}, {'mouse': ['a', 1, 2, 3]}, 'x', []):
            self.assertIsNone(B.page_origin(bad, 1), bad)

    def test_a_box_is_scaled_and_moved(self):
        self.assertEqual(B.scaled([100, 200, 40, 20], (0, 87), 1.5), [150.0, 387.0, 60.0, 30.0])

    def test_where_the_field_is_in_the_window(self):
        self.assertEqual(B.field_box({'field': [100, 200, 406, 106], 'zoom': 1, 'geometry': GEOMETRY}), [112, 297, 406, 106])
        at_150 = dict(GEOMETRY, mouse=[312, 310, 200, 142])  # the same pointer, in a page whose pixels are 1.5 DIP
        self.assertEqual(B.field_box({'field': [100, 200, 406, 106.4], 'zoom': 1.5, 'geometry': at_150}), [162, 397, 609, 160])
        for bad in ({}, {'field': [1, 2, 3], 'geometry': GEOMETRY}, {'field': ['a', 2, 3, 4], 'geometry': GEOMETRY},
                    {'field': [100, 200, 406, 106], 'geometry': {'mouse': None, 'top': False}}):
            self.assertIsNone(B.field_box(bad), bad)

    def test_utf16(self):
        self.assertEqual((B.u16('abc'), B.u16('ą'), B.u16('😀'), B.u16('a😀b')), (3, 1, 2, 4))


class ReadTests(Base):
    def test_nothing_is_asked_where_no_browser_has_the_focus(self):
        for args in ({'browser': None}, {'enabled': False}, {'off': True}, {'busy': True}):
            self.tick(**args)
        self.assertEqual(self.hub.asked, [])
        self.assertEqual(self.out.calls, [])  # and nothing is hidden that is not ours: no badge of another source is cleared

    def test_the_field_is_read_and_checked_like_ibus_does(self):
        self.read(before='Mysle ze to ', selected='', after='dziala dobrze.')
        (field, text, caret), _ = self.out.of('text')[-1]
        self.assertEqual((text, caret), ('Mysle ze to dziala dobrze.', 12))
        self.assertEqual(self.hub.last('live')[1:2] if self.hub.asked else None, None)  # one read at a time: answered

    def test_a_selection_is_checked_with_no_caret(self):
        self.read(before='Mysle ', selected='ze to', after=' dziala.')
        self.assertEqual(self.out.of('text')[-1][0][1:], ('Mysle ze to dziala.', None))

    def test_one_read_at_a_time_and_again_at_every_tick(self):
        self.tick()
        self.tick()
        self.assertEqual(self.hub.ops(), ['live'])
        self.hub.answer('live', live_reply())
        self.tick()
        self.assertEqual(self.hub.ops(), ['live'])  # the next read

    def test_the_same_text_is_sent_once_and_a_change_again(self):
        self.read()
        self.read()
        self.assertEqual(len(self.out.of('text')), 1)
        self.read(before='Mysle ze to dziala dobrze. Jeszcze')
        self.assertEqual(len(self.out.of('text')), 2)

    def test_a_new_field_is_a_new_number(self):
        self.read()
        first = self.field_number()
        self.tick()
        self.hub.answer('live', live_reply(client='5:0:2', before='Inne pole.'))
        self.assertNotEqual(self.field_number(), first)

    def test_a_window_of_a_long_field_is_checked_from_the_carets_paragraph(self):
        text = 'Pierwszy akapit.\n' + 'Mysle ze to dziala. ' + 'x' * 10
        self.read(before=text[:30], selected='', after=text[30:], start=100, short=False)
        sent = self.out.of('text')[-1][0][1]
        self.assertTrue(sent.startswith('Mysle ze to dziala.'), sent)
        self.assertEqual(self.live.field.base, len('Pierwszy akapit.\n'))

    def test_no_field_with_the_focus_clears_what_is_shown(self):
        self.read()
        self.live.issues(self.field_number(), [ISSUE])
        self.assertEqual(self.out.of('badge')[-1][0][0]['count'], 1)
        self.tick()
        self.hub.answer('live', {'ok': False, 'reason': 'no_client', 'attempted': False})
        self.assertEqual(self.out.of('badge')[-1][0][0], {'v': 1, 'count': 0})
        self.assertEqual(len(self.out.of('clear_service')), 1)

    def test_a_slow_answer_changes_nothing(self):
        self.read()
        self.live.issues(self.field_number(), [ISSUE])
        shown = len(self.out.of('badge'))
        self.tick()
        self.hub.answer('live', {'ok': False, 'reason': 'timeout'})
        self.assertEqual(len(self.out.of('badge')), shown)

    def test_a_site_that_is_off_and_a_secure_field_are_not_checked(self):
        self.out.off_sites = {'mail.example'}
        self.read()
        self.assertEqual(self.out.of('text'), [])
        self.out.off_sites = set()
        self.tick()
        self.hub.answer('live', {'ok': False, 'reason': 'secure_input', 'attempted': False})
        self.assertEqual(self.out.of('text'), [])

    def test_the_selection_too_long_is_not_checked(self):
        self.tick()
        reply = live_reply()
        reply.pop('selected')
        reply['selectionTooLong'] = True
        self.hub.answer('live', reply)
        self.assertEqual(self.out.of('text'), [])

    def test_leaving_the_browser_stops_everything(self):
        self.read()
        self.live.issues(self.field_number(), [ISSUE])
        self.tick(browser=None)
        self.assertEqual(self.out.of('badge')[-1][0][0], {'v': 1, 'count': 0})
        self.assertEqual(len(self.out.of('clear_service')), 1)
        self.assertFalse(self.live.active)

    def test_an_answer_that_comes_after_the_focus_moved_is_dropped(self):
        self.tick()
        self.tick(browser=None)
        self.hub.answer('live', live_reply())
        self.assertEqual(self.out.of('text'), [])

    def test_a_browser_without_an_extension_is_not_asked(self):
        self.hub.connected = set()
        self.tick()
        self.assertEqual(self.hub.asked, [])


class MarksTests(Base):
    def issues_for(self, text, **more):
        self.read(before=text, **more)
        self.live.issues(self.field_number(), [ISSUE])

    def test_the_words_boxes_ask_for_the_ranges_in_utf16(self):
        self.read(before='😀 Mysle ze to.')
        self.live.issues(self.field_number(), [dict(ISSUE, start=2, end=7)])
        # the emoji is two UTF-16 units but one character: the word starts at 3 in the page, not at 2
        self.assertEqual(self.hub.last('rects')[4]['ranges'], [[3, 5]])
        self.assertEqual(self.hub.last('rects')[4]['client'], '5:0:1')

    def test_the_window_start_counts(self):
        """A window of a long field starts anywhere, even inside a word: its first word is not checked, and the rest
        is counted from where the window starts in the field."""
        self.read(before='xxx Mysle ze to dziala.', start=1000, short=False)
        self.live.issues(self.field_number(), [ISSUE])  # start 0 in the text that was sent: after "xxx "
        self.assertEqual(self.hub.last('rects')[4]['ranges'], [[1004, 5]])

    def test_a_whole_field_is_counted_from_its_start(self):
        self.read(before='Mysle ze to dziala.')
        self.live.issues(self.field_number(), [ISSUE])
        self.assertEqual(self.hub.last('rects')[4]['ranges'], [[0, 5]])

    def test_the_marks_are_in_the_windows_coordinates(self):
        self.issues_for('Mysle ze to dziala dobrze.')
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]]))
        view = self.out.of('marks')[-1][0][0]
        self.assertEqual(view['from'], 'client')  # counted from the window's client rect, where Chrome's coordinates start
        self.assertEqual(view['toolkit'], ['GTK', '4.0'])  # a Shell before 8 counts from the frame, as for GTK 4
        self.assertEqual(view['clip'], [100.0, 287.0, 406.0, 106.0])
        mark = view['marks'][0]
        self.assertEqual((mark['id'], mark['rects'], mark['word'], mark['learn'], mark['rule_off'], mark['suggestions']),
                         ('i1', [[100.0, 287.0, 40.0, 20.0]], 'Mysle', True, False, ['Myślę', 'Mysłe']))

    def test_zoom_scales_the_boxes(self):
        self.issues_for('Mysle ze to dziala dobrze.')
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]], mouse=(300, 400, 200, 208.6666), zoom=1.5))
        mark = self.out.of('marks')[-1][0][0]['marks'][0]
        self.assertEqual([round(v) for v in mark['rects'][0]], [150, 387, 60, 30])

    def test_the_same_marks_are_sent_once_and_new_ones_again(self):
        self.issues_for('Mysle ze to dziala dobrze.')
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]]))
        self.tick()
        self.hub.answer('live', live_reply())
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]]))
        self.assertEqual(len(self.out.of('marks')), 1)
        self.tick()
        self.hub.answer('live', live_reply())
        self.hub.answer('rects', rects_reply([[[100, 150, 40, 20]]]))  # the page scrolled
        self.assertEqual(len(self.out.of('marks')), 2)

    def test_boxes_for_a_text_that_changed_meanwhile_are_dropped(self):
        self.issues_for('Mysle ze to dziala dobrze.')
        self.tick()
        self.hub.answer('live', live_reply(before='Mysle ze to dziala dobrze!'))  # typed since
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]]))
        self.assertEqual(self.out.of('marks'), [])

    def test_without_geometry_there_are_no_underlines_but_the_badge_stays(self):
        self.issues_for('Mysle ze to dziala dobrze.')
        reply = rects_reply([[[100, 200, 40, 20]]], mouse=None, top=False)
        self.hub.answer('rects', reply)
        self.assertEqual(self.out.of('marks'), [])
        self.assertEqual(self.out.of('badge')[-1][0][0]['count'], 1)

    def test_a_word_the_page_cannot_place_has_no_mark(self):
        self.read(before='Mysle ze to dziala dobrze.')
        self.live.issues(self.field_number(), [ISSUE, dict(ISSUE, id='i2', start=6, end=8, word='ze')])
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]], []]))
        self.assertEqual([m['id'] for m in self.out.of('marks')[-1][0][0]['marks']], ['i1'])

    def test_the_issues_of_the_service_that_belong_to_another_text_are_ignored(self):
        self.read()
        self.live.issues(self.field_number() + 5, [ISSUE])
        self.assertEqual(self.out.of('badge'), [])
        self.assertNotIn('rects', self.hub.ops())


class BadgeTests(Base):
    """The badge goes inside the field's bottom right corner, as on the Mac: its view tells the Shell where the field is."""

    def test_checking_without_issues_moves_with_the_field_and_stops_on_an_edit(self):
        self.read(field=[100, 200, 406, 106], geometry=GEOMETRY)
        self.live.issues(self.field_number(), [], checking=True)
        view = self.out.of('badge')[-1][0][0]
        self.assertEqual((view['count'], view['checking'], view['field']), (0, True, [112, 297, 406, 106]))
        self.again(field=[100, 150, 406, 106])
        view = self.out.of('badge')[-1][0][0]
        self.assertEqual((view['checking'], view['field']), (True, [112, 247, 406, 106]))
        self.assertNotIn('rects', self.hub.ops())
        self.again(before='Nowy tekst testowy.')
        self.assertFalse(self.out.of('badge')[-1][0][0]['checking'])
        self.assertFalse(self.live.field.checking)

    def test_new_field_drops_checking_and_rejects_its_old_result(self):
        self.read(field=[100, 200, 406, 106], geometry=GEOMETRY)
        previous = self.field_number()
        self.live.issues(previous, [], checking=True)
        self.again(client='5:0:2', field=[100, 400, 406, 106])
        self.assertFalse(self.live.field.checking)
        sent = len(self.out.of('badge'))
        self.live.issues(previous, [], checking=True)
        self.assertEqual(len(self.out.of('badge')), sent)

    def shown(self, **more):
        self.read(**dict({'field': [100, 200, 406, 106], 'geometry': GEOMETRY}, **more))
        self.live.issues(self.field_number(), [ISSUE])
        return self.out.of('badge')[-1][0][0]

    def again(self, **more):
        self.tick()
        self.hub.answer('live', live_reply(**dict({'field': [100, 200, 406, 106], 'geometry': GEOMETRY}, **more)))

    def test_the_badge_tells_where_the_field_is_in_the_window(self):
        view = self.shown()
        self.assertEqual((view['count'], view['field'], view['from']), (1, [112, 297, 406, 106], 'client'))
        self.assertEqual(view['issues'][0]['word'], 'Mysle')

    def test_zoom_scales_the_field(self):
        view = self.shown(zoom=1.5, geometry=dict(GEOMETRY, mouse=[312, 310, 200, 142]))
        self.assertEqual(view['field'], [162, 397, 609, 159])

    def test_the_badge_goes_with_the_field_when_it_moves(self):
        self.shown()
        sent = len(self.out.of('badge'))
        self.again()  # the same place: nothing new
        self.assertEqual(len(self.out.of('badge')), sent)
        self.again(field=[100, 150, 406, 106])  # the page scrolled by 50
        view = self.out.of('badge')[-1][0][0]
        self.assertEqual((len(self.out.of('badge')), view['count'], view['field']), (sent + 1, 1, [112, 247, 406, 106]))

    def test_another_field_does_not_take_the_old_badge_along(self):
        self.shown()
        sent = len(self.out.of('badge'))
        self.again(client='5:0:2', field=[100, 400, 406, 106])  # its own issues bring its badge
        self.assertEqual(len(self.out.of('badge')), sent)

    def test_where_the_page_cannot_tell_the_badge_stands_at_the_windows_edge(self):
        view = self.shown(geometry={'dpr': 1, 'win': [0, 0, 1, 1, 1, 1], 'mouse': None, 'top': False})
        self.assertNotIn('field', view)
        self.assertNotIn('from', view)

    def test_a_badge_that_goes_carries_nothing(self):
        self.shown()
        self.live.stop()
        self.assertEqual(self.out.of('badge')[-1][0][0], {'v': 1, 'count': 0})
        self.assertIsNone(self.live.field_box)


class ClickTests(Base):
    def shown(self):
        self.read()
        self.live.issues(self.field_number(), [ISSUE])
        self.hub.answer('rects', rects_reply([[[100, 200, 40, 20]]]))

    def test_a_click_on_a_suggestion_replaces_the_word_in_the_field_the_text_came_from(self):
        self.shown()
        self.assertTrue(self.live.handles('live:fix:i1:0'))
        self.live.action('live:fix:i1:0')
        browser, op, callback, timeout, fields = self.hub.last('replace')
        self.assertEqual((browser, op, fields['client']), ('google-chrome', 'replace', '5:0:1'))
        self.assertEqual(fields['check'], {'location': 0, 'length': 5, 'text': 'Mysle'})
        self.assertEqual((fields['edits'], fields['expected']), ([{'location': 0, 'length': 5, 'text': 'Myślę'}], 'Myślę'))
        self.hub.answer('replace', {'ok': True, 'attempted': True, 'landedAfterMs': 20})
        self.assertEqual(self.out.of('fixed'), [((self.field_number(), 0, 5), {})])  # the service takes its issues away at once
        log = self.out.of('log')[-1]
        self.assertEqual((log[0][0], log[1]['outcome'], log[1]['via']), ('live_fix', 'sent', 'extension'))
        self.assertNotIn('Mysle', str(log))  # never the word

    def test_the_second_suggestion_and_a_word_in_the_middle_of_a_window(self):
        self.read(before='Pierwsze. Mysle ze to dziala.', start=50)
        self.live.issues(self.field_number(), [dict(ISSUE, start=10, end=15)])
        self.live.action('live:fix:i1:1')
        fields = self.hub.last('replace')[4]
        self.assertEqual((fields['check']['location'], fields['edits'][0]['text']), (60, 'Mysłe'))

    def test_a_refusal_of_the_page_is_said(self):
        self.shown()
        self.live.action('live:fix:i1:0')
        self.hub.answer('replace', {'ok': False, 'reason': 'changed', 'attempted': False})
        self.assertEqual(self.out.of('fixed'), [])
        self.assertEqual(self.out.of('message')[-1][0], ('fix_failed', 3))
        self.assertEqual(self.out.of('log')[-1][1]['outcome'], 'changed')
        # The page's check of the word failed: the suggestion leaves the badge, not offered for another click.
        self.assertEqual(self.out.of('command')[-1][0][0], {'cmd': 'fixed', 'field': self.field_number(), 'start': 0, 'end': 5})
        self.assertEqual(self.live.field.issues, [])

    def test_a_suggestion_that_is_gone_or_a_word_that_moved_away(self):
        self.shown()
        self.live.action('live:fix:nobody:0')
        self.live.action('live:fix:i1:9')
        self.assertEqual([o for o in self.hub.ops() if o == 'replace'], [])
        self.assertEqual(len(self.out.of('message')), 2)
        self.tick()
        self.hub.answer('live', live_reply(before='Zupelnie inny tekst.'))  # the word is not there any more
        self.live.action('live:fix:i1:0')
        self.assertEqual(self.out.of('log')[-1][1]['outcome'], 'word_gone')
        self.assertEqual(self.out.of('command')[-1][0][0]['cmd'], 'fixed')

    def test_ignore_and_learn_go_to_the_service(self):
        self.shown()
        self.live.action('live:ignore:i1')
        self.live.action('live:learn:i1')
        commands = [a[0] for a, _ in self.out.of('command')]
        self.assertEqual(commands, [{'cmd': 'ignore', 'word': 'Mysle', 'rule': 'SPELLING_PL'},
                                    {'cmd': 'learn', 'word': 'Mysle', 'rule': 'SPELLING_PL'}])
        self.assertEqual(self.out.of('message')[-1][0], ('learned', 2))
        logs = [k for _, k in self.out.of('log')]
        self.assertTrue(all('Mysle' not in str(k) for k in logs))

    def test_a_rule_off_goes_to_the_service(self):
        self.shown()
        self.assertTrue(self.live.handles('live:rule_off:i1'))
        self.live.action('live:rule_off:i1')
        self.assertEqual(self.out.of('command')[-1][0][0], {'cmd': 'rule_off', 'word': 'Mysle', 'rule': 'SPELLING_PL'})
        self.assertEqual(self.out.of('message')[-1][0], ('rule_off', 3))

    def test_the_clicks_are_not_ours_when_the_page_is_not_the_source(self):
        self.assertFalse(self.live.handles('live:fix:i1:0'))
        self.shown()
        self.assertFalse(self.live.handles('live:open'))
        self.assertFalse(self.live.handles('accept'))

    def test_a_replace_to_a_browser_that_went_is_a_refusal_not_a_crash(self):
        self.shown()
        self.hub.connected = set()
        self.live.action('live:fix:i1:0')
        self.assertEqual(self.out.of('log')[-1][1]['outcome'], 'no_extension')


if __name__ == '__main__':
    unittest.main()
