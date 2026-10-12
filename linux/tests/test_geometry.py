"""Word boxes from AT-SPI for the live check's underlines (gramadrama/geometry.py), with a fake AT-SPI:
a GTK4-like field (monospace, the range extents of its first letter only) and a Chromium-like rich
editor (a paragraph per child)."""
import unittest
from gramadrama import geometry as G

EMB = G.EMBEDDED


class FakeField:
    def __init__(self, text, caret, role='text', x0=12, y0=40, cw=8, lh=20, first_letter_ranges=True):
        self.text_value, self.caret_value, self.role = text, caret, role
        self.x0, self.y0, self.cw, self.lh = x0, y0, cw, lh
        self.first_letter_ranges = first_letter_ranges
        self.children = []
        self.focused = True
        self.selection_value = None

    def box(self, offset):
        """Monospace: a line per '\\n', 18 px letters on 20 px lines."""
        if not 0 <= offset < len(self.text_value):
            return [0, 0, 0, 0]
        line = self.text_value.count('\n', 0, offset)
        column = offset - (self.text_value.rfind('\n', 0, offset) + 1)
        return [self.x0 + column * self.cw, self.y0 + line * self.lh, self.cw, 18]


class FakeApi:
    def __init__(self, fields, toolkit=('GTK', '4.20.1')):
        self.fields = fields
        self.toolkit_value = toolkit
        self.walks = 0

    def active_windows(self):
        yield 'app', 'window'

    def focused_in(self, window):
        self.walks += 1
        return next((f for f in self.fields if f.focused), None)

    def still_focused(self, acc):
        return acc.focused

    def text(self, acc):
        return acc.text_value

    def caret(self, acc):
        return acc.caret_value

    def selection(self, acc):
        return acc.selection_value

    def children(self, acc):
        return [(c, c.role, c.text_value) for c in acc.children]

    def char_box(self, acc, offset):
        return acc.box(offset)

    def range_box(self, acc, start, end):
        if acc.first_letter_ranges:
            return acc.box(start)  # GTK4
        first, last = acc.box(start), acc.box(end - 1)
        return [first[0], first[1], last[0] + last[2] - first[0], first[3]]

    def extents(self, acc):
        return [0, 30, 700, 300]

    def toolkit(self, acc):
        return self.toolkit_value


def issue(text, word, rule='SPELLING_PL'):
    start = text.index(word)
    return {'id': '%d:%d:%s' % (start, start + len(word), rule), 'start': start, 'end': start + len(word)}


class Helpers(unittest.TestCase):
    def test_words(self):
        self.assertEqual(G.words('ala  ma\nkota', 0, 12), [(0, 3), (5, 7), (8, 12)])
        self.assertEqual(G.words('ala ma', 3, 4), [])  # only whitespace

    def test_align(self):
        full = 'Pierwszy akapit.\nMysle ze to dziala.'
        sent = 'Mysle ze to dziala.'
        self.assertEqual(G.align(full, len(full), sent, len(sent)), 17)
        self.assertIsNone(G.align(full, len(full), 'Inny tekst.', 11))  # another field
        self.assertIsNone(G.align(full, 3, sent, len(sent)))  # the carets disagree: not current
        self.assertEqual(G.align(full, None, sent, None), 17)  # a selection: the one occurrence
        self.assertIsNone(G.align('ab ab', None, 'ab', None))  # two: which one is unknown
        self.assertIsNone(G.align(full, 0, '', 0))

    def test_word_box(self):
        first, last = [12, 40, 8, 18], [44, 40, 8, 18]
        self.assertEqual(G.word_box(first, last, first), [12, 40, 40, 18])  # GTK4: joined to the last letter
        self.assertEqual(G.word_box(first, [12, 60, 8, 18], first), first)  # wrapped: its first line
        self.assertEqual(G.word_box([0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]), None)  # Chromium not laid out
        self.assertEqual(G.word_box(first, last, [12, 40, 40, 18]), [12, 40, 40, 18])

    def test_caret_box(self):
        self.assertEqual(G.caret_box([12, 40, 8, 18], [20, 40, 8, 18]), [20, 40, 0, 18])
        self.assertEqual(G.caret_box([12, 40, 8, 18], [0, 0, 0, 0]), [20, 40, 0, 18])  # at the end
        self.assertIsNone(G.caret_box(None, None))

    def test_join(self):
        children = [(1, 'paragraph', 'Mysle ze,'), (2, 'paragraph', 'a potem.')]
        self.assertEqual(G.join(EMB + EMB, children), ('Mysle ze,\na potem.', [(0, 0), (1, 10)]))
        text, starts = G.join('Zobacz ' + EMB + ' tu', [(1, 'link', 'stronę')])
        self.assertEqual((text, starts), ('Zobacz stronę tu', [(0, 7)]))  # a link stays in its line
        self.assertIsNone(G.join(EMB + EMB, children[:1]))  # fewer children than places


class GeometryMarks(unittest.TestCase):
    TEXT = 'Mysle ze to dziala.'

    def test_gtk4_field(self):
        field = FakeField(self.TEXT, len(self.TEXT))
        api = FakeApi([field])
        geometry = G.Geometry(api)
        found = geometry.marks(self.TEXT, len(self.TEXT), [issue(self.TEXT, 'Mysle'), issue(self.TEXT, 'dziala')])
        self.assertEqual(found['toolkit'], ['GTK', '4.20.1'])
        self.assertEqual(found['marks'], [{'id': '0:5:SPELLING_PL', 'rects': [[12, 40, 40, 18]]},
                                          {'id': '12:18:SPELLING_PL', 'rects': [[108, 40, 48, 18]]}])
        self.assertEqual(found['caret'], [12 + 18 * 8 + 8, 40, 0, 18])  # after the last letter
        geometry.marks(self.TEXT, len(self.TEXT), [])
        self.assertEqual(api.walks, 1)  # found once, kept while focused

    def test_the_checked_text_is_a_part_of_the_field(self):
        full = 'Pierwszy akapit.\n' + self.TEXT
        field = FakeField(full, len(full))
        found = G.Geometry(FakeApi([field])).marks(self.TEXT, len(self.TEXT), [issue(self.TEXT, 'ze')])
        self.assertEqual(found['marks'][0]['rects'], [[12 + 6 * 8, 60, 16, 18]])  # on the second line

    def test_an_issue_over_a_line_break_gets_a_box_per_word(self):
        text = 'Mysle\nze to'
        field = FakeField(text, len(text), first_letter_ranges=False)
        found = G.Geometry(FakeApi([field])).marks(text, len(text), [{'id': 'x', 'start': 0, 'end': 8}])
        self.assertEqual(found['marks'][0]['rects'], [[12, 40, 40, 18], [12, 60, 16, 18]])

    def test_another_field_gives_nothing_and_is_looked_for_again(self):
        field = FakeField('Zupełnie inny tekst.', 20)
        api = FakeApi([field])
        geometry = G.Geometry(api)
        self.assertIsNone(geometry.marks(self.TEXT, len(self.TEXT), [issue(self.TEXT, 'ze')]))
        geometry.marks(self.TEXT, len(self.TEXT), [])
        self.assertEqual(api.walks, 2)
        field.focused = False
        self.assertIsNone(geometry.marks(self.TEXT, len(self.TEXT), []))

    def test_a_rich_editor(self):
        """Chromium's ProseMirror or Lexical box: the root holds a character per paragraph."""
        root = FakeField(EMB + EMB, 2, role='section')
        first = FakeField('Mysle ze to,', -1, role='paragraph', first_letter_ranges=False)
        second = FakeField('a potem dziala.', 15, role='paragraph', y0=60, first_letter_ranges=False)
        root.children = [first, second]
        sent = 'Mysle ze to,\na potem dziala.'
        found = G.Geometry(FakeApi([root], toolkit=('Chromium', '154'))).marks(
            sent, len(sent), [issue(sent, 'ze'), issue(sent, 'dziala')])
        self.assertEqual(found['marks'], [{'id': '6:8:SPELLING_PL', 'rects': [[60, 40, 16, 18]]},
                                          {'id': '21:27:SPELLING_PL', 'rects': [[76, 60, 48, 18]]}])
        self.assertEqual(found['caret'], [12 + 14 * 8 + 8, 60, 0, 18])

    def test_the_number_of_underlined_words_is_capped(self):
        text = ' '.join('slowo%d' % i for i in range(100))
        field = FakeField(text, len(text))
        issues = [{'id': str(i), 'start': m.start(), 'end': m.end()} for i, m in enumerate(G.WORD.finditer(text))]
        found = G.Geometry(FakeApi([field])).marks(text, len(text), issues)
        self.assertEqual(sum(len(m['rects']) for m in found['marks']), G.MAX_MARKS)


if __name__ == '__main__':
    unittest.main()


class Caret(unittest.TestCase):
    """Where the caret is now, in the offsets of the text IBus sent (a hotkey after a GTK4 click)."""

    DOC = 'Pierwszy akapit.\nMysle ze to dziala.\nTrzeci.'
    SENT = 'Mysle ze to dziala.\nTrzeci.'  # the IBus window starts at the second paragraph

    def geometry(self, caret, selection=None, text=DOC):
        field = FakeField(text, caret)
        field.selection_value = selection
        return G.Geometry(FakeApi([field])), field

    def test_the_caret_in_the_sent_text(self):
        geometry, _ = self.geometry(17 + 6)
        self.assertEqual(geometry.caret(self.SENT), (6, 6))

    def test_a_selection_keeps_its_direction(self):
        geometry, _ = self.geometry(17 + 2, (17 + 2, 17 + 8))
        self.assertEqual(geometry.caret(self.SENT), (2, 8))  # the caret at the start: selected backwards
        geometry, _ = self.geometry(17 + 8, (17 + 2, 17 + 8))
        self.assertEqual(geometry.caret(self.SENT), (8, 2))

    def test_refused(self):
        cases = (('the caret outside the sent text', self.geometry(3), self.SENT),
                 ('the text is not in the field', self.geometry(20), 'Inny tekst'),
                 ('the text twice in the field', self.geometry(1, text='ala ala'), 'ala'),
                 ('a selection reaching outside', self.geometry(17 + 2, (5, 17 + 2)), self.SENT),
                 ('the caret inside the selection', self.geometry(17 + 4, (17 + 2, 17 + 8)), self.SENT),
                 ('no caret', self.geometry(-1), self.SENT),
                 ('a rich editor', self.geometry(0, text=EMB + EMB), self.SENT),
                 ('nothing sent', self.geometry(0), ''))
        for name, (geometry, _), sent in cases:
            with self.subTest(name):
                self.assertIsNone(geometry.caret(sent))

    def test_no_focused_field(self):
        geometry, field = self.geometry(20)
        field.focused = False
        self.assertIsNone(geometry.caret(self.SENT))
