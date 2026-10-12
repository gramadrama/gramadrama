import unittest
from gramadrama.engine.editbuf import EditBuffer


def typed(buffer, text):
    for ch in text:
        buffer.insert(ch)
    return buffer


class EditBufferTests(unittest.TestCase):
    def test_typing_starts_at_the_end_and_replaces_the_selection(self):
        b = EditBuffer('Ala ma kota.')
        self.assertEqual((b.cursor, b.anchor), (12, 12))
        typed(b, ' Ćma też.')
        self.assertEqual(b.text, 'Ala ma kota. Ćma też.')
        b.cursor, b.anchor = 4, 6  # "ma"
        b.insert('miała')
        self.assertEqual((b.text, b.cursor, b.anchor), ('Ala miała kota. Ćma też.', 9, 9))
        b.insert('\r\n')
        self.assertEqual(b.text, 'Ala miała\n kota. Ćma też.')  # line ends become \n

    def test_backspace_and_delete_never_split_a_character(self):
        b = EditBuffer('a😀b👍🏽c')
        b.cursor = b.anchor = 2  # after the emoji
        self.assertTrue(b.delete())
        self.assertEqual((b.text, b.cursor), ('ab👍🏽c', 1))
        b.cursor = b.anchor = 2
        self.assertTrue(b.delete(forward=True))
        self.assertEqual(b.text, 'abc')  # the thumb with its skin tone is one character
        b.cursor = b.anchor = 0
        self.assertFalse(b.delete())
        b.cursor = b.anchor = 3
        self.assertFalse(b.delete(forward=True))

    def test_word_moves_and_deletions(self):
        b = EditBuffer('Jutro, wyślę raport.')
        b.move('word_left')
        self.assertEqual(b.cursor, 13)  # before "raport"
        b.move('word_left')
        self.assertEqual(b.cursor, 7)  # before "wyślę"
        b.move('word_right')
        self.assertEqual(b.cursor, 12)  # after "wyślę"
        b.delete(word=True)
        self.assertEqual((b.text, b.cursor), ('Jutro,  raport.', 7))
        b.delete(forward=True, word=True)
        self.assertEqual(b.text, 'Jutro, .')

    def test_a_click_places_the_caret_between_characters(self):
        b = EditBuffer('a👍🏽b', cursor=0)
        b.move('right', extend=True)
        self.assertTrue(b.place(2))  # inside the thumb with its skin tone: before it
        self.assertEqual((b.cursor, b.anchor), (1, 1))
        self.assertTrue(b.place(99))
        self.assertEqual(b.cursor, 4)
        self.assertFalse(b.place(4))

    def test_left_and_right_collapse_a_selection(self):
        b = EditBuffer('abcdef', cursor=2)
        b.move('right', extend=True)
        b.move('right', extend=True)
        self.assertEqual((b.selection, b.selected()), ((2, 4), 'cd'))
        b.move('left')
        self.assertEqual((b.cursor, b.anchor), (2, 2))
        b.move('word_right', extend=True)
        b.move('home')
        self.assertEqual((b.cursor, b.anchor), (0, 0))
        self.assertTrue(b.select_all())
        self.assertEqual(b.selected(), 'abcdef')

    def test_lines_of_the_text_without_the_panels_layout(self):
        b = EditBuffer('pierwsza\ndruga linia\n\nczwarta', cursor=3)
        b.move('down')
        self.assertEqual(b.cursor, 12)  # column 3 kept: "dru|ga"
        b.move('down')
        self.assertEqual(b.cursor, 21)  # the empty line
        b.move('down')
        self.assertEqual(b.cursor, 25)  # back to column 3 in "czw|arta"
        b.move('down')
        self.assertEqual(b.cursor, len(b.text))  # below the last line: the end
        b.move('home')
        self.assertEqual(b.cursor, 22)
        b.move('up')
        self.assertEqual(b.cursor, 21)
        b.move('start')
        b.move('up')
        self.assertEqual(b.cursor, 0)
        b.move('end')
        self.assertEqual(b.cursor, 8)  # before the line break

    def test_the_panels_visual_lines(self):
        text = 'Jeden długi akapit, który panel zawija w trzech liniach na ekranie.'
        b = EditBuffer(text, cursor=5)
        b.set_lines([0, 20, 47])  # "Jeden długi akapit, " | "który panel zawija w " | ...
        b.move('end')
        self.assertEqual(b.cursor, 19)  # before the space where the line wraps
        b.move('down')
        self.assertEqual(b.cursor, 39)  # column 19 on the second line
        b.move('home')
        self.assertEqual(b.cursor, 20)
        for wrong in ([1, 20], [0, 20, 10], [0, 999], [], None, [0, 'x']):
            b.set_lines(wrong)
            self.assertEqual(b.lines, (0, 20, 47))  # a report that does not fit this text is ignored
        b.insert('x')
        self.assertIsNone(b.lines)  # the panel lays the new text out again

    def test_undo_takes_back_a_typed_word_at_once(self):
        b = typed(EditBuffer('Ala'), ' ma kota')
        self.assertTrue(b.undo())
        self.assertEqual(b.text, 'Ala ma ')  # a word, the space after it went with the word before
        self.assertTrue(b.undo())
        self.assertEqual(b.text, 'Ala ')
        self.assertTrue(b.redo())
        self.assertEqual((b.text, b.cursor), ('Ala ma ', 7))
        b.delete()
        b.delete()
        self.assertEqual(b.text, 'Ala m')
        b.undo()
        self.assertEqual(b.text, 'Ala ma ')  # Backspaces in a row too
        self.assertFalse(b.redo() and b.redo())
        b.move('left')
        b.insert('X')
        self.assertFalse(b.redo())  # an edit drops what could be redone
