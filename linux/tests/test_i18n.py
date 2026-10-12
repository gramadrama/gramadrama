"""Language fallback, safe formatting and the complete shipped UI catalogues."""
import ast
import json
from pathlib import Path
import re
import string
import tempfile
import unittest
from unittest import mock

from gramadrama import i18n, oidc

PACKAGE = Path(i18n.__file__).parent


def placeholders(message):
    named = sorted(field for _, field, _, _ in string.Formatter().parse(message) if field is not None)
    percent = re.findall(r'%(?:\([^)]+\))?[-+#0 ]*\d*(?:\.\d+)?[sdrif]', message)
    return named, percent


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, i18n.get_language())

    def test_primary_language_selects_ui_with_english_fallback(self):
        for primary, expected in [('pl', 'pl'), ('PL', 'pl'), ('pl-PL', 'pl'), ('pl_PL', 'pl'),
                                  ('en', 'en'), ('de', 'en'), ('fr', 'en'), ('', 'en'), ('../../pl', 'en')]:
            with self.subTest(primary=primary):
                self.assertEqual(i18n.set_language(primary), expected)
                self.assertEqual(i18n.get_language(), expected)

    def test_runtime_and_explicit_language_do_not_freeze_imports(self):
        message = 'Podaj adres logowania i identyfikator klienta.'
        i18n.set_language('pl')
        self.assertEqual(i18n.tr(message), message)
        i18n.set_language('de')
        self.assertEqual(i18n.tr(message), 'Enter the sign-in address and client ID.')
        self.assertEqual(i18n.tr(message, language='pl'), message)
        self.assertEqual(i18n.get_language(), 'en')

    def test_sign_in_errors_follow_interface_language(self):
        i18n.set_language('de')
        settings = mock.Mock(auth_issuer='', auth_client_id='')
        with self.assertRaisesRegex(oidc.AuthError, 'Enter the sign-in address and client ID'):
            oidc.start_login(settings)

    def test_missing_community_messages_and_broken_templates_fall_back_to_english(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(i18n, 'LOCALES', Path(directory)):
            for language, messages in [('en', {'Cześć {name}': 'Hello {name}', 'Gotowe': 'Done'}),
                                        ('fr', {'Cześć {name}': 'Bonjour {wrong}'})]:
                target = Path(directory, language)
                target.mkdir()
                (target / 'app.json').write_text(json.dumps(messages))
            i18n.catalogue.cache_clear()
            try:
                self.assertEqual(i18n.interface_language('fr'), 'fr')
                self.assertEqual(i18n.tr('Gotowe', language='fr'), 'Done')
                self.assertEqual(i18n.tr('Cześć {name}', language='fr', name='Example'), 'Hello Example')
                self.assertEqual(i18n.tr('Cześć {name}', language='en'), 'Hello {name}')
                self.assertEqual(i18n.tr('User {literal}', language='en'), 'User {literal}')
            finally:
                i18n.catalogue.cache_clear()

    def test_catalogues_keep_parameters_and_have_no_conflicting_duplicates(self):
        merged = {}
        for path in sorted((PACKAGE / 'locales' / 'en').glob('*.json')):
            entries = json.loads(path.read_text(), object_pairs_hook=list)
            keys = set()
            for source, target in entries:
                with self.subTest(file=path.name, message=source):
                    self.assertNotIn(source, keys, 'duplicate key in one catalogue')
                    keys.add(source)
                    self.assertIsInstance(target, str)
                    self.assertTrue(target.strip())
                    self.assertEqual(placeholders(source), placeholders(target))
                    if source in merged:
                        self.assertEqual(merged[source], target, 'conflicting translations between components')
                    merged[source] = target

    def test_every_literal_translation_call_has_an_english_entry(self):
        entries = i18n.catalogue('en')
        for path in sorted(PACKAGE.rglob('*.py')):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr' and node.args:
                    source = node.args[0]
                    if isinstance(source, ast.Constant) and isinstance(source.value, str):
                        with self.subTest(file=str(path.relative_to(PACKAGE)), line=node.lineno):
                            self.assertIn(source.value, entries)


if __name__ == '__main__':
    unittest.main()
