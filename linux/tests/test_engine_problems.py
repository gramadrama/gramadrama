"""engine/problems.py: what the panel says about a failed correction (branding/PANEL.md, Errors)."""
import unittest

from gramadrama import i18n
from gramadrama.engine import problems
from gramadrama.engine.problems import LOGIN, RETRY, SERVICES, WRITING


class ProblemTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language('pl')

    def test_a_server_that_does_not_answer_says_why_and_offers_a_retry(self):
        p, text = problems.describe('refused', 'Serwer modelu zszedł ze sceny — sprawdź połączenie.')
        self.assertEqual((p.group, p.title, p.actions), (2, 'Serwer nie odpowiada', (RETRY, SERVICES)))
        self.assertIn('(odmowa połączenia)', text)
        self.assertIn('(serwer nie odpowiedział na czas)', problems.describe('timeout', '')[1])

    def test_the_message_is_the_sentence_where_the_table_has_none(self):
        for why, title in (('http_404', 'Na serwerze nie ma tego modelu'), ('http_503', 'Serwer zgłosił błąd'),
                           ('model_error', 'Model zgłosił błąd'), (None, 'Korekta się nie udała'),
                           ('crash', 'Korekta się nie udała')):
            with self.subTest(why=why):
                p, text = problems.describe(why, 'Słowa serwera.')
                self.assertEqual((p.title, text), (title, 'Słowa serwera.'))

    def test_a_settings_message_loses_the_prefix_its_title_says_already(self):
        p, text = problems.describe('settings', 'Ustawienia: Podaj protokół serwera modelu: openai albo ollama.')
        self.assertEqual((p.title, text), ('Trzeba dokończyć ustawienia', 'Podaj protokół serwera modelu: openai albo ollama.'))

    def test_a_new_install_is_told_what_to_do_first(self):
        for why, title in (('no_ollama', 'Nie ma jeszcze modelu językowego'), ('ollama_down', 'Ollama nie działa'),
                           ('no_model', 'Nie wybrano modelu')):
            with self.subTest(why=why):
                p, text = problems.describe(why, 'Ustawienia: Wybierz model (aplikacja GramaDrama › Usługi).')
                self.assertEqual((p.group, p.title, p.actions[0]), (1, title, SERVICES))
                self.assertNotIn('Wybierz model (', text)

    def test_the_groups_keep_their_rules(self):
        for why, p in problems.TABLE.items():
            with self.subTest(why=why):
                self.assertTrue(set(p.actions) <= set(problems.ACTIONS))
                if p.group == 1 and p.actions:
                    self.assertNotEqual(p.actions[0], RETRY)  # retrying alone would not help: fix it first
                if p.group == 4:
                    self.assertEqual(p.actions, ())  # the selection is the user's to change
        self.assertEqual(problems.problem('offline').actions[0], LOGIN)
        self.assertEqual(problems.problem('second_language').actions, (WRITING,))
        self.assertEqual(problems.problem('truncated_reply').actions, ())  # a retry would cut it short again

    def test_only_slips_of_the_model_are_retried_quietly(self):
        for why in problems.RETRIED:
            self.assertEqual(problems.problem(why).group, 3, why)
        self.assertNotIn('truncated_reply', problems.RETRIED)  # a retry would cut it short again
        self.assertEqual({w for w, p in problems.TABLE.items() if p.group == problems.TEXT}, {'empty', 'too_long', 'embedded'})

    def test_the_view_is_translated_at_display_time(self):
        p, text = problems.describe('refused', '')
        self.assertEqual(problems.view(p, text)['actions'], [{'id': 'retry', 'label': 'Spróbuj ponownie'},
                                                             {'id': 'open:connection', 'label': 'Otwórz Usługi'}])
        i18n.set_language('en')
        p, text = problems.describe('refused', '')
        view = problems.view(p, text)
        self.assertEqual((view['group'], view['title']), (2, 'The server does not answer'))
        self.assertIn('(connection refused)', view['text'])
        self.assertEqual([a['label'] for a in view['actions']], ['Try again', 'Open Services'])

    def test_every_text_has_an_english_one(self):
        texts = {p.title for p in problems.TABLE.values()} | {p.text for p in problems.TABLE.values() if p.text}
        texts |= set(problems.LABELS.values()) | set(problems.NETWORK.values()) | {problems.UNKNOWN.title,
                                                                                    problems.SETTINGS_PREFIX}
        for text in sorted(texts):
            with self.subTest(text=text):
                self.assertNotEqual(i18n.tr(text, language='en'), text)


if __name__ == '__main__':
    unittest.main()
