"""The prompts as files (gramadrama/prompts.py, docs/prompts.md): the defaults are the macOS ones, a file of a piece's
name replaces that piece, a broken file is ignored with its reason, and nothing of the user's is ever overwritten."""
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock
from gramadrama import core, dictation, prompts as P
from gramadrama.prompts import Prompts
from stub_server import Stub, sse

REPO = pathlib.Path(__file__).resolve().parents[2]


def golden():
    entries = json.loads((REPO / 'bench' / 'prompts.golden.json').read_text())['entries']
    return [({'english': 'en', 'polish': 'pl', None: 'en'}[e['target']], e) for e in entries]


class Directory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name, 'prompts')
        self.dir.mkdir()

    def write(self, name, content, raw=False):
        path = self.dir / name
        path.write_bytes(content if raw else (content if isinstance(content, str) else json.dumps(content)).encode('utf-8'))

    def load(self):
        return Prompts.load(self.dir)


class DefaultsTests(Directory):
    def test_word_preservation_rules_are_only_for_standard_fix(self):
        fixed = core.messages('To przykład.', 'fix', prompts=Prompts.defaults())[0]['content']
        self.assertIn('never swap a word for a synonym or a different word', fixed)
        self.assertIn('Never invent words.', fixed)
        for mode in ('improve', 'formal', 'casual', 'shorter', 'translate'):
            system = core.messages('To przykład.', mode, prompts=Prompts.defaults())[0]['content']
            self.assertNotIn('never swap a word for a synonym or a different word', system)

    def test_nothing_of_the_users_is_the_macos_prompts_word_for_word(self):
        for target, entry in golden():
            with self.subTest(mode=entry['mode'], input=entry['input']):
                self.assertEqual(core.messages(entry['input'], entry['mode'], target, Prompts.defaults()), entry['messages'])
                self.assertEqual(core.messages(entry['input'], entry['mode'], target, self.load()), entry['messages'])  # an empty dir

    def test_the_defaults_as_files_and_back_change_nothing(self):
        written = P.export(self.dir)
        self.assertEqual(sorted(written), sorted(P.PIECES))
        self.assertEqual(len(P.PIECES), 18)
        loaded = self.load()
        self.assertEqual(loaded.problems, [])
        self.assertEqual(loaded.overridden, [])  # every file is there, and none says anything the default does not
        for file, piece in P.PIECES.items():  # and every one reads back as the default it was made from
            with self.subTest(file=file):
                value = P._parse((self.dir / file).read_text(encoding='utf-8'), piece)
                same = lambda value: json.loads(json.dumps(value))  # a list or a tuple of examples is the same examples
                self.assertEqual(same(value), same(piece.default))
        for target, entry in golden():  # so the exported files make the very same messages
            self.assertEqual(core.messages(entry['input'], entry['mode'], target, loaded), entry['messages'])
        self.assertEqual(dictation.cleanup_messages('yyy no więc test', ['Bielik'], core.detect_language, loaded),
                         dictation.cleanup_messages('yyy no więc test', ['Bielik'], core.detect_language, Prompts.defaults()))

    def test_export_never_overwrites_a_file(self):
        self.write('task-fix.txt', 'My own task.\n')
        written = P.export(self.dir)
        self.assertNotIn('task-fix.txt', written)
        self.assertEqual((self.dir / 'task-fix.txt').read_text(), 'My own task.\n')
        self.assertEqual(P.export(self.dir), [])  # a second time: nothing to write
        P.export(self.dir / 'new' / 'deeper')  # a directory that is not there is made
        self.assertEqual(len(list((self.dir / 'new' / 'deeper').iterdir())), 18)

    def test_the_dictation_cleanup_messages(self):
        messages = dictation.cleanup_messages('Ala ma kota', ['Bielik', 'PR'], core.detect_language, Prompts.defaults())
        self.assertEqual(messages[0]['content'], dictation.CLEANUP_SYSTEM + '\n- Spell these names and terms exactly like this: Bielik, PR.')
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'Clean up this dictated transcript. Language: Polish. Your answer MUST be in '
                                                                  'the same language — do not translate.\n\n<text>\nAla ma kota\n</text>'})
        self.assertEqual(len(messages), 1 + 2 * len(dictation.CLEANUP_EXAMPLES) + 1)
        self.assertNotIn('Spell these names', dictation.cleanup_messages('x', [], core.detect_language, Prompts.defaults())[0]['content'])


class DictionaryWordsTests(unittest.TestCase):
    def test_a_correction_names_only_the_words_its_text_uses_after_the_cached_prefix(self):
        words = ['Bielik', 'GramaDrama', 'Orion']
        plain = core.messages('Bielika i gramadrama.', 'fix', prompts=Prompts.defaults())
        told = core.messages('Bielika i gramadrama.', 'fix', prompts=Prompts.defaults(), words=words)
        self.assertEqual(told[:-1], plain[:-1])  # the system prompt and the examples: the same prefix
        self.assertEqual(told[-1]['content'], 'Spell these names and terms exactly like this: Bielik, GramaDrama.\n\n'
                         + plain[-1]['content'])
        self.assertEqual(core.messages('Ala ma kota.', 'improve', prompts=Prompts.defaults(), words=words),
                         core.messages('Ala ma kota.', 'improve', prompts=Prompts.defaults()))
        translated = core.messages('Bielik pisze.', 'translate', 'en', Prompts.defaults(), ('pl', 'en'), words)
        self.assertNotIn('Spell these names', json.dumps(translated))

    def test_the_backend_sends_the_users_words_but_not_for_a_translation(self):
        settings = core.Settings(llm_url='http://127.0.0.1:9', model='m', dictation_vocabulary='Bielik')
        backend = core.Backend(settings)
        sent = []

        def chat(messages, *args, **kwargs):
            sent.append(messages[-1]['content'])
            return 'Bielik pisze.'
        with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': config}), \
                mock.patch.object(backend, 'chat', side_effect=chat):
            backend.correct('Bielik pisze.', 'fix')
            backend.correct('Bielik pisze.', 'translate', 'en')
        self.assertTrue(sent[0].startswith('Spell these names and terms exactly like this: Bielik.'))
        self.assertNotIn('Spell these names', sent[1])


class OverrideTests(Directory):
    def system(self, mode='improve', prompts=None):
        return core.messages('Zły tekst.', mode, 'en', prompts or self.load())[0]['content']

    def test_a_file_replaces_one_piece_and_nothing_else(self):
        default = self.system('formal', Prompts.defaults())
        self.write('task-formal.txt', 'Rewrite it like a pirate.\n')  # an editor's trailing newline is no part of the prompt
        mine = self.system('formal')
        self.assertEqual(mine, default.replace(core.INSTRUCTIONS['formal'], 'Rewrite it like a pirate.'))
        self.assertIn('\nTask: Rewrite it like a pirate.\nRules:\n- Keep the language', mine)  # the glue is the same
        self.assertEqual(self.system('casual'), self.system('casual', Prompts.defaults()))  # the other modes are not touched
        self.assertEqual(self.load().overridden, ['task-formal.txt'])

    def test_the_other_text_pieces(self):
        self.write('intro.txt', 'You are a careful German editor.')
        self.write('rules.txt', 'Rules:\n- Only fix typos.')
        self.write('last-rule-fix.txt', '- Return the text unchanged when it is right.')
        self.assertEqual(self.system('fix'), 'You are a careful German editor.\nTask: ' + core.INSTRUCTIONS['fix'] +
                         '\nRules:\n- Only fix typos.\n- Return the text unchanged when it is right.')
        self.write('last-rule-rewrite.txt', '- Return all of it.')
        self.assertTrue(self.system('improve').endswith('Rules:\n- Only fix typos.\n- Return all of it.'))

    def test_the_examples_and_the_names_of_the_languages(self):
        self.write('examples.json', [{'text': 'Ich habe keine Ahnung', 'language': 'en', 'output': 'Ich habe keine Ahnung.'}])
        self.write('names.json', {'language': {'pl': 'German', 'en': 'German', 'mixed': 'mixed German and English'},
                                  'target': {'en': 'German', 'pl': 'German'}})
        messages = core.messages('Das ist ein Test', 'fix', 'en', self.load())
        self.assertEqual(len(messages), 1 + 2 + 1)  # one example now, and the text
        self.assertEqual(messages[1]['content'], 'Language of the text: German. Your answer MUST be in the same language — do '
                                                 'not translate.\n\n<text>\nIch habe keine Ahnung\n</text>')
        self.assertTrue(messages[-1]['content'].startswith('Language of the text: mixed German and English.'))  # detected as mixed
        self.assertEqual(core.messages('Hallo', 'translate', 'en', self.load())[-1]['content'], 'Translate into German.\n\n<text>\nHallo\n</text>')

    def test_the_templates_and_a_placeholder_inside_the_text(self):
        self.write('user.txt', '[{language}] {text}')
        self.write('translate.txt', 'Translate to {target}, politely.')
        self.write('translate-user.txt', '{target}: {text}')
        messages = core.messages('Tekst z {language} i {text} w środku.', 'improve', 'en', self.load())
        self.assertEqual(messages[-1]['content'], '[Polish] Tekst z {language} i {text} w środku.')  # the user's text is never expanded
        translation = core.messages('Hej {target}', 'translate', 'pl', self.load())
        self.assertEqual((translation[0]['content'], translation[-1]['content']), ('Translate to Polish, politely.', 'Polish: Hej {target}'))

    def test_the_cleanup_pieces(self):
        self.write('cleanup.txt', 'Clean it, the German way.')
        self.write('cleanup-user.txt', '({language}) {text}')
        self.write('cleanup-examples.json', [{'text': 'ähm ja', 'output': 'Ja.'}])
        messages = dictation.cleanup_messages('äh nein', ['Wort'], core.detect_language, self.load())
        self.assertEqual(messages[0]['content'], 'Clean it, the German way.\n- Spell these names and terms exactly like this: Wort.')
        self.assertEqual(messages[1:3], [{'role': 'user', 'content': '(Polish) ähm ja'}, {'role': 'assistant', 'content': 'Ja.'}])
        self.assertEqual(messages[3], {'role': 'user', 'content': '(mixed Polish and English — keep each part in its own '
                                                                  'language) äh nein'})
        self.assertEqual(len(messages), 4)  # the system prompt, one example, the transcript

    def test_files_that_are_no_pieces_are_left_alone(self):
        self.write('notes.txt', 'my notes')
        self.write('task-fix.txt.bak', 'old')
        (self.dir / 'rules.txt').mkdir()  # a directory with a piece's name
        loaded = self.load()
        self.assertEqual((loaded.overridden, loaded.problems), ([], []))


class BrokenFileTests(Directory):
    def test_a_broken_file_is_ignored_with_its_reason_and_the_rest_still_counts(self):
        self.write('task-fix.txt', 'A good task.')
        cases = [('intro.txt', b'\xff\xfe broken', 'UTF-8'), ('task-improve.txt', '   \n', 'pusty'),
                 ('user.txt', 'no placeholder here', '{text}'), ('translate.txt', 'no target', '{target}'),
                 ('translate-user.txt', '{target} only', '{text}'), ('cleanup-user.txt', 'x', '{text}'),
                 ('examples.json', '{not json', 'JSON'), ('translate-examples.json', '{"a": 1}', 'lista'),
                 ('cleanup-examples.json', [{'text': 'x'}], 'przykład 1'),
                 ('names.json', {'language': {'pl': 'Polish'}, 'target': {}}, 'language'),
                 ('rules.txt', 'x' * (P.MAX_FILE + 1), 'KiB')]
        for name, content, reason in cases:
            self.write(name, content, raw=isinstance(content, bytes))
        loaded = self.load()
        found = dict(loaded.problems)
        for name, _content, reason in cases:
            with self.subTest(name=name):
                self.assertIn(reason, found[name])
                self.assertEqual(loaded.get(name), P.PIECES[name].default)  # the default stays
        self.assertEqual(loaded.overridden, ['task-fix.txt'])  # the good one is used
        self.assertEqual(core.messages('x', 'fix', 'en', loaded)[0]['content'].split('\nTask: ')[1].split('\n')[0], 'A good task.')

    def test_the_shape_of_the_examples(self):
        for content, reason in (([], 'lista'), ([{'text': 't', 'language': 'xx', 'output': 'o'}], 'pl, en, de albo mixed'),
                                ([{'text': 't', 'language': 'pl', 'output': ''}], 'przykład 1'),
                                ([{'text': 't', 'language': 'pl', 'output': 'o'}] * (P.MAX_EXAMPLES + 1), 'lista'),
                                (['text'], 'przykład 1'), ({'text': 't'}, 'lista')):
            with self.subTest(content=str(content)[:50]):
                self.write('examples.json', content)
                self.assertIn(reason, dict(self.load().problems)['examples.json'])
        self.write('examples.json', [{'text': 't', 'language': 'mixed', 'output': 'o'}])
        self.assertEqual(self.load().get('examples.json'), (('t', 'mixed', 'o'),))


class CurrentTests(unittest.TestCase):
    """The user's directory, read again only when something in it changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        Prompts._cache.update(signature=None, prompts=None)
        self.dir = pathlib.Path(self.tmp.name, 'gramadrama', 'prompts')

    def test_changes_are_seen_and_nothing_is_read_twice(self):
        self.assertEqual(Prompts.current().overridden, [])  # no directory: the defaults
        self.dir.mkdir(parents=True)
        (self.dir / 'task-fix.txt').write_text('First.')
        first = Prompts.current()
        self.assertEqual(first.overridden, ['task-fix.txt'])
        self.assertIs(Prompts.current(), first)  # nothing changed: not read again
        time.sleep(0.01)
        (self.dir / 'task-fix.txt').write_text('Second one.')
        self.assertEqual(Prompts.current().get('task-fix.txt'), 'Second one.')
        (self.dir / 'task-fix.txt').unlink()  # reset to the default by deleting the file
        self.assertEqual(Prompts.current().overridden, [])

    def test_the_corrections_use_the_users_files_by_default(self):
        self.dir.mkdir(parents=True)
        (self.dir / 'task-fix.txt').write_text('Only fix commas.')
        self.assertIn('Task: Only fix commas.\n', core.messages('Ala ma kota', 'fix')[0]['content'])


class CommandLineTests(Directory):
    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = P.main(list(argv))
        return code, out.getvalue()

    def test_export_status_and_list(self):
        code, out = self.run_main('export', str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn('zapisano 18 plików', out)
        self.assertIn('nic do zapisania', self.run_main('export', str(self.dir))[1])
        code, out = self.run_main('status', str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn('Własne pliki (0)', out)  # none differs from the default
        self.write('task-fix.txt', 'Mine.')
        self.assertIn('Własne pliki (1): task-fix.txt', self.run_main('status', str(self.dir))[1])
        self.write('user.txt', 'no placeholder')
        code, out = self.run_main('status', str(self.dir))
        self.assertEqual(code, 1)  # a problem: the exit code says so
        self.assertIn('Pominięty user.txt: brakuje {text}', out)
        code, out = self.run_main('list')
        self.assertEqual((code, out.count('\n')), (0, 18))
        self.assertIn('task-fix.txt', out)
        self.assertEqual(self.run_main()[0], 2)


class BackendTests(Directory):
    """A Backend sends the pieces it was given, or the user's files."""

    def setUp(self):
        super().setUp()
        self.stub = Stub().__enter__()
        self.addCleanup(self.stub.__exit__)
        self.stub.events = sse('Dobrze.')

    def settings(self):
        return core.Settings(llm_url=self.stub.url, llm_api='openai', model='m')

    def test_a_backend_with_its_own_prompts_is_what_the_benchmark_measures(self):
        self.write('task-fix.txt', 'Fix NOTHING but the word zly.')
        core.Backend(self.settings(), prompts=self.load()).correct('to jest zly tekst')
        core.Backend(self.settings()).correct('to jest zly tekst')  # no override in the user's own directory
        mine, plain = [r['messages'][0]['content'] for r in self.stub.completions()]
        self.assertIn('Task: Fix NOTHING but the word zly.\n', mine)
        self.assertIn('Task: ' + core.INSTRUCTIONS['fix'], plain)


class LanguageProfileTests(Directory):
    def test_profiles_file_is_current(self):
        """tests/prompt_profiles.json: what macOS PromptFiles.forLanguages must build; regenerated by
        python3 tests/prompt_profiles.py after a change of for_languages."""
        import prompt_profiles
        written = json.loads(prompt_profiles.PATH.read_text(encoding='utf-8'))
        self.assertEqual(written, json.loads(json.dumps(prompt_profiles.document())),
                         'tests/prompt_profiles.json is stale: run python3 tests/prompt_profiles.py from linux/')

    def test_exporting_defaults_does_not_turn_polish_into_a_german_profile_override(self):
        P.export(self.dir)
        prompts = self.load()
        self.assertFalse(prompts.overridden)
        messages = core.messages('Das ist gut.', 'fix', prompts=prompts, languages=('de', 'en'))
        self.assertIn('German and English', messages[0]['content'])
        self.assertNotIn('Polish', str(messages))

    def test_english_only_has_english_examples_and_no_polish_instructions(self):
        messages = core.messages('Your right.', 'fix', prompts=Prompts.defaults(), languages=('en',))
        self.assertNotIn('Polish', str(messages))
        self.assertNotIn('German', str(messages))
        self.assertTrue(messages[-1]['content'].startswith('Language of the text: English.'))
        self.assertIn("You're right", str(messages))
        self.assertIn('Never invent words.', messages[0]['content'])
        self.assertIn('never swap a word for a synonym or a different word', messages[0]['content'])

    def test_german_pair_has_german_rules_examples_and_language_label(self):
        messages = core.messages('Ich schicke die Datei morgen.', 'fix', prompts=Prompts.defaults(), languages=('de', 'en'))
        self.assertIn('German and English', messages[0]['content'])
        self.assertIn('noun capitalization', messages[0]['content'])
        self.assertIn('Ich denke, dass wir die Datei', str(messages))
        self.assertTrue(messages[-1]['content'].startswith('Language of the text: German.'))
        self.assertNotIn('Polish', str(messages))

    def test_translation_examples_use_only_the_selected_pair(self):
        messages = core.messages('Thanks for the message.', 'translate', 'de', Prompts.defaults(), ('de', 'en'))
        self.assertIn('German and English', messages[0]['content'])
        self.assertIn('Translate into German.', messages[-1]['content'])
        self.assertNotIn('Polish', str(messages))
        self.assertIn('Danke für die Nachricht', str(messages))

    def test_custom_overrides_survive_language_profiles_and_german_examples_are_accepted(self):
        self.write('task-formal.txt', 'Keep this custom instruction.')
        self.write('examples.json', [{'text': 'ich komme', 'language': 'de', 'output': 'Ich komme.'}])
        prompts = self.load()
        self.assertFalse(prompts.problems)
        messages = core.messages('Ich komme.', 'formal', prompts=prompts, languages=('de',))
        self.assertIn('Keep this custom instruction.', messages[0]['content'])
        self.assertEqual(prompts.overridden, ['examples.json', 'task-formal.txt'])


if __name__ == '__main__':
    unittest.main()
