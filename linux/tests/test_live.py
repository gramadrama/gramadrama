"""Live checking's rules against the cases of macOS SelfTest.swift (offsets in code points: the Mac's are
UTF-16, the same for these texts but for the emoji case)."""
import unittest
from gramadrama import live as L
from gramadrama.live import Issue


def issue(start, length, source='słownik', word='', rule='', suggestions=()):
    return Issue(start, start + length, word, '', tuple(suggestions), source, rule)


def spans(issues):
    return ' '.join('%d+%d' % (i.start, i.end - i.start) for i in issues)


class MacCases(unittest.TestCase):
    def test_sentence_languages(self):
        for text, expected in (('Dzięki za PR. I will check it tomorrow morning. Zrobię to jutro.', 'PL EN PL'),
                               ('Zrobiłem przegląd kodu wczoraj wieczorem. Ok, thx!', 'PL PL'),
                               ('Deploy na staging jest gotowy, sprawdź.', 'PL')):
            with self.subTest(text=text):
                found = L.sentences(text)
                self.assertEqual(' '.join(lang.upper() for _, _, lang in found), expected)
                self.assertEqual((found[0][0], found[-1][1]), (0, len(text)))  # they cover the text

    def test_blank_and_masks(self):
        self.assertEqual(L.blank('a😀b\nc', {0, 1, 3}), '  b\nc')  # one space per code point, line ends kept
        text = L.LiveText('Cześć @jan, dzięki. I will check it tomorrow morning.')
        self.assertTrue(7 in text.polish_mask and 7 in text.english_mask)
        self.assertEqual(text.polish.strip(' '), 'Cześć     , dzięki.')
        self.assertTrue(text.has_polish and text.has_english)
        self.assertEqual(len(text.polish), len(text.text))

    def test_shift(self):
        self.assertEqual(spans(L.shift([issue(0, 3), issue(4, 2), issue(12, 5)], 'ala ma kota ktory', 'ala mał kota ktory')),
                         '0+3 13+5')
        self.assertEqual(spans(L.shift([issue(0, 3)], 'abc', 'abc')), '0+3')

    def test_merge(self):
        self.assertEqual([i.source for i in L.merge([issue(0, 6), issue(10, 2)], [issue(0, 6, 'LanguageTool')])],
                         ['LanguageTool', 'słownik'])
        self.assertEqual([i.source for i in L.merge([issue(0, 6)], [issue(0, 10, 'LanguageTool')])],
                         ['słownik', 'LanguageTool'])
        self.assertEqual([i.source for i in L.merge([issue(5, 2, word='ze', rule='PL_DIACRITICS')],
                                                    [issue(5, 5, 'LanguageTool')])], ['LanguageTool'])

    def test_sentence_start(self):
        self.assertEqual([L.sentence_start(t, p) for p, t in ((0, 'Ala'), (4, 'Tak. Ala'), (4, 'tak Ala'))],
                         [True, True, False])

    def test_missing_diacritics(self):
        def found(text):
            return ' '.join('%s→%s' % (i.word, i.suggestions[0]) for i in L.missing_diacritics(text))
        self.assertEqual(found('Cos tez tam jest, wiec zle nie bede'), 'Cos→Coś tez→też wiec→więc zle→źle bede→będę')
        self.assertEqual(found('wiem ze to jest ze mną i ze szkoły, ze względu na to'), 'ze→że')
        self.assertEqual(found('wiem ze'), '')
        self.assertEqual(L.restoring_diacritics('ze to'), 'że to')
        self.assertIsNone(L.restoring_diacritics('ze mną'))

    def test_bielik_issues(self):
        def found(a, b):
            return ' | '.join('%s→%s' % (i.word, i.suggestions[0] if i.suggestions else '') for i in L.bielik_issues(a, b))
        self.assertEqual(found('testuje cos, czego hyba nie ma', 'Testuję coś, czego chyba nie ma'),
                         'testuje→Testuję | cos→coś | hyba→chyba')
        self.assertEqual(found('wiem że działa', 'wiem, że działa'), 'wiem→wiem,')
        self.assertEqual(found('Wszystko gra.', 'Wszystko gra.'), '')
        self.assertTrue(all(i.source == 'Bielik' and i.rule == 'BIELIK' and i.kind == 'style'
                            for i in L.bielik_issues('testuje cos', 'Testuję coś')))

    def test_ranked(self):
        self.assertEqual(L.ranked(['Dziki', 'Dzięki', 'Dzieli'], 'Dzieki'), ['Dzięki', 'Dziki', 'Dzieli'])
        self.assertEqual(L.ranked(['część', 'cześć'], 'Czesc'), ['Część', 'Cześć'])

    def test_locate(self):
        self.assertEqual(L.locate('cos', 9, 'Testuję cos, czego'), 8)
        self.assertEqual(L.locate('ze', 22, 'wiem ze to, a potem ze tamto ze'), 20)
        self.assertIsNone(L.locate('cos', 0, 'cosinus i coś'))

    def test_plausible(self):
        text = 'Zrobię deploy dla PR od Anna, dzieki'
        english = {'deploy'}.__contains__
        results = {w: L.plausible(w, text.index(w), text, english=english) for w in ('deploy', 'PR', 'Anna', 'dzieki')}
        self.assertEqual(results, {'deploy': True, 'PR': True, 'Anna': True, 'dzieki': False})


class Rules(unittest.TestCase):
    def test_kinds_and_summary(self):
        self.assertEqual(issue(0, 1).kind, 'spelling')
        self.assertEqual(issue(0, 1, 'LanguageTool', rule='COMMA').kind, 'grammar')
        self.assertEqual(issue(0, 1, 'LanguageTool', rule='MORFOLOGIK_RULE_PL_PL').kind, 'spelling')
        self.assertEqual(issue(0, 1, 'Bielik', rule='BIELIK').kind, 'style')
        self.assertEqual([L.summary(n) for n in (1, 2, 5, 12, 22, 25, 104, 111)],
                         ['1 błąd', '2 błędy', '5 błędów', '12 błędów', '22 błędy', '25 błędów', '104 błędy', '111 błędów'])

    def test_under_caret(self):
        word = issue(4, 3)
        self.assertEqual([L.under_caret(word, c) for c in (None, 3, 4, 5, 7, 8)], [False, False, False, True, True, False])

    def test_utf16(self):
        text = 'a😀b'
        self.assertEqual([L.utf16_to_index(text, u) for u in (0, 1, 3, 4)], [0, 1, 2, 3])


class ConfiguredLanguages(unittest.TestCase):
    def test_german_and_english_sentences_keep_the_original_offsets(self):
        original = 'Ich schicke die Datei morgen. I will review it tomorrow.'
        text = L.LiveText(original, ('de', 'en'))
        self.assertEqual([code for _, _, code in L.sentences(original, languages=('de', 'en'))], ['de', 'en'])
        self.assertEqual(text.parts['de'].strip(), 'Ich schicke die Datei morgen.')
        self.assertEqual(text.parts['en'].strip(), 'I will review it tomorrow.')
        self.assertTrue(all(len(part) == len(original) for part in text.parts.values()))
        self.assertFalse(text.has_polish)

    def test_single_english_profile_never_builds_a_polish_part(self):
        text = L.LiveText('This is a test @user https://example.com.', ('en',))
        self.assertEqual(set(text.parts), {'en'})
        self.assertFalse(text.has_polish)
        self.assertNotIn('@user', text.parts['en'])
        self.assertNotIn('https://', text.parts['en'])


if __name__ == '__main__':
    unittest.main()
