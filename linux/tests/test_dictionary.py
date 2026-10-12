"""The user's dictionary (gramadrama/dictionary.py): the own words, Enchant's personal lists and the rules turned off."""
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from gramadrama import core, dictionary as D, paths


class Words(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.config.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('ENCHANT_CONFIG_DIR', None)
        self.enchant = pathlib.Path(self.config.name, 'enchant')
        self.enchant.mkdir()

    def write(self, name, text):
        (self.enchant / name).write_text(text, encoding='utf-8')

    def test_the_tags_are_the_cores(self):
        self.assertEqual(D.TAGS, core.DICTIONARY_CODES)

    def test_enchant_lists_follow_its_own_directory(self):
        self.assertEqual(paths.enchant_dir(), self.enchant)
        with mock.patch.dict(os.environ, {'ENCHANT_CONFIG_DIR': '/srv/enchant'}):
            self.assertEqual(paths.enchant_dir(), pathlib.Path('/srv/enchant'))
        with mock.patch.dict(os.environ, {'ENCHANT_CONFIG_DIR': 'relative'}):
            self.assertEqual(paths.enchant_dir(), self.enchant)

    def test_learned_words_latest_first_from_the_lists_of_the_languages(self):
        self.write('pl_PL.dic', '﻿Bielik\n# a comment\n\nOrion\nbielik\n' + 'x' * 61 + '\ntwo words\n')
        self.write('pl.dic', 'Gdańszczanka\nOrion\n')
        self.write('en_US.dic', 'GramaDrama\n')
        self.write('de_DE.dic', 'Straßenbahnhaltestelle\n')
        self.assertEqual(D.learned_words(('pl',)), ['bielik', 'Orion', 'Gdańszczanka'])
        self.assertEqual(D.learned_words(('pl', 'en')), ['bielik', 'Orion', 'Gdańszczanka', 'GramaDrama'])
        self.assertEqual(D.learned_words(('en',)), ['GramaDrama'])

    def test_a_list_too_big_or_missing_gives_nothing(self):
        self.assertEqual(D.learned_words(('pl', 'en', 'de')), [])
        self.write('pl_PL.dic', 'słowo\n' * (D.MAX_LIST // 6 + 1))
        self.assertEqual(D.learned_words(('pl',)), [])

    def test_a_changed_list_is_read_again(self):
        self.write('pl_PL.dic', 'Bielik\n')
        self.assertEqual(D.learned_words(('pl',)), ['Bielik'])
        self.write('pl_PL.dic', 'Bielik\nOrion\n')
        self.assertEqual(D.learned_words(('pl',)), ['Orion', 'Bielik'])

    def test_user_words_are_the_own_then_the_learned(self):
        self.write('pl_PL.dic', 'Orion\nbielik\n')
        settings = core.Settings(dictation_vocabulary='Bielik, GramaDrama', secondary_language='')
        self.assertEqual(D.own_words(settings), ['Bielik', 'GramaDrama'])
        self.assertEqual(D.user_words(settings), ['Bielik', 'GramaDrama', 'Orion'])
        self.assertEqual(D.own_words(core.Settings(dictation_vocabulary=['bad'])), [])

    def test_forget_removes_only_that_word_and_keeps_the_mode(self):
        self.write('pl_PL.dic', 'Bielik\n# comment\nOrion\nOrionek\n')
        self.write('pl.dic', 'Orion\n')
        os.chmod(self.enchant / 'pl_PL.dic', 0o664)
        self.assertEqual(D.learned_words(('pl',)), ['Orionek', 'Orion', 'Bielik'])
        self.assertIsNone(D.forget('Orion', ('pl',)))
        self.assertEqual((self.enchant / 'pl_PL.dic').read_text(), 'Bielik\n# comment\nOrionek\n')
        self.assertEqual((self.enchant / 'pl.dic').read_text(), '')
        self.assertEqual(stat.S_IMODE((self.enchant / 'pl_PL.dic').stat().st_mode), 0o664)
        self.assertEqual(D.learned_words(('pl',)), ['Orionek', 'Bielik'])
        self.assertFalse((self.enchant / 'pl_PL.exc').exists())  # not Enchant's exclusion list
        self.assertIsNone(D.forget('Nieznane', ('pl',)))

    def test_known_accepts_the_forms_enchant_accepts(self):
        known = D.Known(['GramaDrama', 'staging', 'PR'])
        for word in ('GramaDrama', 'GRAMADRAMA', 'staging', 'Staging', 'STAGING', 'PR'):
            self.assertIn(word, known)
        for word in ('gramadrama', 'Gramadrama', 'stagin', 'pr', 'Pr', ''):
            self.assertNotIn(word, known)
        self.assertFalse(D.Known())

    def test_relevant_words_are_the_ones_the_text_uses(self):
        words = ['Bielik', 'GramaDrama', 'PR', 'code review', 'Orion']
        self.assertEqual(D.relevant(words, 'Bielika i gramadrama zrobią code review.'), ['Bielik', 'GramaDrama', 'code review'])
        self.assertEqual(D.relevant(words, 'To prawda, że Orionowie przyjdą.'), [])  # "pr…" is no PR, 5 letters too many
        self.assertEqual(D.relevant(words, 'Wyślij PR do Oriona.'), ['PR', 'Orion'])
        self.assertEqual(D.relevant(['a.b'], 'axb'), [])  # the words are text, not patterns
        many = ['Słowo%d' % n for n in range(30)]
        self.assertEqual(len(D.relevant(many, ' '.join(many))), D.MAX_PROMPT_WORDS)


class Rules(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.config.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_turned_off_and_on_again(self):
        self.assertEqual(D.rules_off(), {})
        self.assertIsNone(D.turn_off_rule('ZE_ZE', '  Że   czy\nze '))
        self.assertIsNone(D.turn_off_rule('PL_COMMA', 'Przecinek'))
        self.assertEqual(D.rules_off(), {'ZE_ZE': 'Że czy ze', 'PL_COMMA': 'Przecinek'})
        self.assertEqual(stat.S_IMODE(paths.rules_path().stat().st_mode), 0o600)
        self.assertIsNone(D.turn_on_rule('ZE_ZE'))
        self.assertEqual(D.rules_off(), {'PL_COMMA': 'Przecinek'})
        self.assertIsNone(D.turn_on_rule('ZE_ZE'))  # already on: nothing to do
        self.assertEqual(json.loads(paths.rules_path().read_text()), {'off': {'PL_COMMA': 'Przecinek'}})

    def test_an_id_that_is_no_rule_is_refused(self):
        for rule in ('', 'A B', '../x', 'x' * 101, None):
            self.assertTrue(D.turn_off_rule(rule, 'x'))
        self.assertFalse(paths.rules_path().exists())

    def test_the_oldest_makes_room(self):
        for n in range(D.MAX_RULES + 1):
            D.turn_off_rule('R%d' % n)
        off = D.rules_off()
        self.assertEqual(len(off), D.MAX_RULES)
        self.assertNotIn('R0', off)
        self.assertIn('R%d' % D.MAX_RULES, off)

    def test_a_broken_file_shows_every_rule_and_is_kept_when_replaced(self):
        path = paths.rules_path()
        path.parent.mkdir(parents=True)
        for content in ('{', '[]', '{"off": []}'):
            path.write_text(content)
            self.assertEqual(D.rules_off(), {})
        path.write_text('{"off": {"OK": "Opis", "bad id": "x", "N": 5}}')
        self.assertEqual(D.rules_off(), {'OK': 'Opis'})
        path.write_text('{')
        self.assertIsNone(D.turn_off_rule('NEW', 'Nowa'))
        self.assertEqual(D.rules_off(), {'NEW': 'Nowa'})
        self.assertEqual(path.with_name('rules.json.bak').read_text(), '{')


if __name__ == '__main__':
    unittest.main()
