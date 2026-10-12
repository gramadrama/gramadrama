import json
import unittest
from pathlib import Path
from gramadrama import core, dictation as D

CASES = json.loads((Path(__file__).resolve().parent / 'dictation_cases.json').read_text())


class MacCases(unittest.TestCase):
    """The dictation rules of macOS SelfTest.swift, case by case."""

    def test_transcript_filter(self):
        for case in CASES['transcript_filter']:
            with self.subTest(case['text']):
                self.assertEqual(D.filter_transcript(case['text'], case['prompt']), case['expected'])

    def test_tidy(self):
        for case in CASES['tidy']:
            with self.subTest(case['input']):
                self.assertEqual(D.tidy(case['input']), case['expected'])

    def test_needs_cleanup(self):
        for case in CASES['needs_cleanup']:
            with self.subTest(case['input']):
                self.assertEqual(D.needs_cleanup(case['input']), case['expected'])

    def test_cleanup_looks_safe(self):
        for case in CASES['cleanup_looks_safe']:
            with self.subTest(case['cleaned']):
                self.assertEqual(D.cleanup_looks_safe(case['cleaned'], case['transcript']), case['expected'])

    def test_vocabulary_and_prompt(self):
        for case in CASES['parse_vocabulary']:
            self.assertEqual(D.parse_vocabulary(case['input']), case['expected'])
        for case in CASES['whisper_prompt']:
            with self.subTest(case['language']):
                self.assertEqual(D.whisper_prompt(case['vocabulary'], case['language']), case['expected'])


    def test_the_prompt_takes_the_latest_learned_words_that_fit(self):
        own = ['GramaDrama', 'Bielik']
        self.assertEqual(D.prompt_words(own, ['bielik', 'Orion', 'Kasia']), ['GramaDrama', 'Bielik', 'Orion', 'Kasia'])
        learned = ['Słowo%03d' % n for n in range(100)]
        words = D.prompt_words(own, learned)
        self.assertEqual(words[:3], ['GramaDrama', 'Bielik', 'Słowo000'])  # the own ones first, then the latest
        self.assertLessEqual(len(', '.join(words)), D.PROMPT_CHARS)
        self.assertGreater(len(', '.join(words + ['Słowo999'])), D.PROMPT_CHARS)
        many = ['x%d' % n for n in range(200)]
        self.assertEqual(D.prompt_words(many, ['Orion']), many)  # the own words are never dropped


class Language(unittest.TestCase):
    def test_the_other_language_and_the_pill_labels(self):
        for case in CASES['alternate_language']:
            with self.subTest(case['language']):
                self.assertEqual(D.alternate_language(case['language']), case['expected'])
        self.assertEqual({k: D.LABELS[k] for k in ('pl', 'en', 'auto')}, {'pl': 'PL', 'en': 'EN', 'auto': 'auto'})
        self.assertEqual(set(D.LANGUAGES), set(D.ALTERNATE))

    def test_a_whole_english_result_of_polish_dictation(self):
        from gramadrama import core
        for case in CASES['looks_translated']:
            with self.subTest(case['text']):
                self.assertEqual(D.looks_translated(case['text'], core.detect_language), case['expected'])


class Rules(unittest.TestCase):
    def test_levels_and_limits(self):
        self.assertEqual(D.level(10 ** (-50 / 20)), 0.0)
        self.assertAlmostEqual(D.level(10 ** (-10 / 20)), 1.0)
        self.assertEqual(D.level(0), 0.0)
        self.assertLess(20 * __import__('math').log10(D.SILENCE_PEAK), -50)  # -50.46 dBFS
        self.assertFalse(D.cleanup_too_long('a' * 170))
        self.assertTrue(D.cleanup_too_long('a' * 172))  # ~171 UTF-8 bytes is the limit

    def test_tidy_keeps_text_without_fillers(self):
        text = 'Zrobiłem  deploy ,ok'  # no filler: untouched but for the times
        self.assertEqual(D.tidy(text), text)
        self.assertEqual(D.tidy('Eee, eee, dobra.'), 'Dobra.')

    def test_cleanup_messages(self):
        messages = D.cleanup_messages('yyy zrobiłem deploy', ['Bielik', 'GramaDrama'], core.detect_language)
        self.assertEqual(len(messages), 1 + 2 * len(D.CLEANUP_EXAMPLES) + 1)
        self.assertTrue(messages[0]['content'].endswith('- Spell these names and terms exactly like this: Bielik, GramaDrama.'))
        self.assertEqual(messages[-1]['content'], 'Clean up this dictated transcript. Language: Polish. Your answer MUST be '
                                                  'in the same language — do not translate.\n\n<text>\nyyy zrobiłem deploy\n</text>')
        self.assertIn('Language: English.', messages[-2 - 1]['content'])  # the English example
        self.assertNotIn('Spell these', D.cleanup_messages('x', [], core.detect_language)[0]['content'])


class ConfiguredLanguages(unittest.TestCase):
    def test_single_language_disables_shift_and_whisper_auto(self):
        for language in ('en', 'de'):
            for selected in ('pl', 'en', 'de', 'auto'):
                self.assertEqual(D.effective_language(selected, (language,)), language)
                self.assertEqual(D.alternate_language(selected, (language,)), language)

    def test_german_english_pair_alternates_in_both_directions(self):
        self.assertEqual(D.alternate_language('de', ('de', 'en')), 'en')
        self.assertEqual(D.alternate_language('en', ('de', 'en')), 'de')
        self.assertEqual(D.alternate_language('auto', ('de', 'en')), 'en')
        self.assertEqual(D.effective_language('pl', ('de', 'en')), 'de')

    def test_german_whisper_prompt_and_cleanup_stay_german(self):
        from gramadrama import core
        prompt = D.whisper_prompt(['GramaDrama'], 'de')
        self.assertTrue(prompt.startswith('Ich spreche Deutsch'))
        self.assertIn('GramaDrama', prompt)
        self.assertTrue(D.needs_cleanup('Ähm, ich meine am Dienstag.'))
        self.assertEqual(D.tidy('Ähm, ich schicke den Bericht.'), 'Ich schicke den Bericht.')
        messages = D.cleanup_messages('Ähm, ich schicke den Bericht.', [],
                                      lambda text: core.detect_language(text, ('de', 'en')), languages=('de', 'en'))
        self.assertIn('Language: German.', messages[-1]['content'])
        self.assertNotIn('Polish', str(messages))
        self.assertIn('Ich schicke die Datei am Dienstag.', str(messages))

    def test_german_um_is_a_preposition_and_not_an_english_filler(self):
        text = 'Wir treffen uns um zehn Uhr.'
        self.assertEqual(D.tidy(text, 'de'), text)
        self.assertFalse(D.needs_cleanup(text, 'de'))
        self.assertEqual(D.tidy('Ähm, wir treffen uns um zehn Uhr.', 'de'), text)
        self.assertTrue(D.needs_cleanup('Ähm, wir treffen uns um zehn Uhr.', 'de'))


if __name__ == '__main__':
    unittest.main()
