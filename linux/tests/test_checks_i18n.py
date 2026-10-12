"""User-visible diagnostics switch languages without changing protocol/action values."""
import datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from gramadrama import apps, checks, core
from gramadrama.i18n import get_language, set_language


class DiagnosticsLanguageTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_language, get_language())

    def test_statuses_follow_language_after_modules_were_imported(self):
        event = {'ev': 'end', 'why': 'timeout'}
        set_language('pl')
        self.assertEqual(checks.try_event(event),
                         (checks.INFO, 'Wygasło. Użyj skrótu korekty ponownie.'))
        set_language('de')
        self.assertEqual(checks.try_event(event),
                         (checks.INFO, 'Timed out. Use the correction shortcut again.'))
        set_language('pl')
        self.assertEqual(checks.hotkey_check([], verified=True).text,
                         'Działa — sprawdzone w polu Wypróbuj')

    def test_diagnostics_do_not_translate_external_names_or_identifiers(self):
        set_language('en')
        conflict = checks.hotkey_check(['Mój skrót'])
        self.assertIn('Mój skrót', conflict.text)
        self.assertIn('GNOME shortcut', conflict.text)
        self.assertEqual((conflict.state, conflict.action), (checks.ERROR, 'keyboard'))
        self.assertEqual(checks.model_label('custom-model:Q4'), 'custom-model · Q4')
        login = checks.login_check(core.Settings(auth_client_id='app'), 'in', 'Żółw')
        self.assertEqual(login.action, 'logout')
        self.assertIn('Żółw', login.text)

    def test_cached_log_maps_are_translated_at_display_time(self):
        set_language('en')
        now = datetime.datetime(2026, 10, 4, 12).timestamp()
        events = [{'ev': 'start', 't': now, 'mode': 'fix', 'scope': 'selection'},
                  {'ev': 'end', 'why': 'cancel'}]
        self.assertEqual(checks.last_use(events, now).text,
                         'today 12:00 · Correct · selection · cancelled')
        set_language('pl')
        self.assertIn('zaznaczenie · anulowano', checks.last_use(events, now).text)

    def test_inactive_only_source_explains_activation_in_english(self):
        set_language('en')
        result = checks.source_check([('ibus', 'gramadrama')], 'xkb:us::eng')
        self.assertEqual((result.state, result.action), (checks.WARNING, None))
        self.assertIn('configured correctly but is not active in IBus', result.text)
        self.assertIn('Click a regular text field', result.text)
        self.assertIn('If the problem persists', result.text)
        self.assertNotIn('Super+Space', result.text)

    def test_login_error_sentinel_still_selects_sign_in_guidance(self):
        set_language('en')
        for error, text in (('nie zalogowano', 'Sign-in required'), ('offline', 'does not answer on this network')):
            reply = checks.Reply(False, error=error)
            for render in (checks.llm_check, checks.whisper_check, checks.languagetool_check):
                with self.subTest(render=render.__name__, error=error):
                    result = render(reply)
                    self.assertEqual(result.state, checks.WARNING)
                    self.assertIn(text, result.text)
            self.assertEqual(reply.error, error)

    def test_flatpak_help_remains_available_in_english(self):
        plan = apps.Plan(reason=apps.FLATPAK_REASON)
        set_language('en')
        row = apps.row_state('Example', True, apps.CHROMIUM, 'x11', None, plan, None)
        self.assertEqual((row.state, row.button), (apps.UNFIXABLE, apps.HELP))
        self.assertIn('Flatpak app needs a permission change', row.text)
        self.assertIn('Flatpak app needs a permission change', apps.apply_fix(plan))
        self.assertEqual(plan.reason, apps.FLATPAK_REASON)

    def test_counts_and_duration_follow_interface_language(self):
        set_language('en')
        for count in (1, 2, 5, 12, 22):
            evidence = apps.Evidence(ok_count=count)
            row = apps.row_state('Example', True, apps.GTK4, None, None, None, evidence)
            self.assertIn('%d %s' % (count, 'correction' if count == 1 else 'corrections'), row.text)
            self.assertIn('press an arrow key', row.text)
        self.assertEqual(checks.seconds(1.25), '1.2 s')
        set_language('pl')
        self.assertEqual(checks.seconds(1.25), '1,2 s')

    def test_invalid_config_preserves_path_in_english_error(self):
        set_language('en')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text('not json', encoding='utf-8')
            with mock.patch.object(core.Settings, 'path', return_value=path):
                _, problems = checks.read_settings()
            self.assertEqual(problems[checks.FILE], '%s is not valid JSON' % path)
        result = checks.settings_check({'primary_language': 'unknown value'}, 'primary_language')
        self.assertIn('Invalid primary language', result.text)
        self.assertIn('choose a language on the Writing tab', result.text)

    def test_unknown_browser_label_has_english_fallback(self):
        set_language('en')
        result = checks.browser_check({'browsers': {'unknown': '1.2'}}, True, '/example')
        self.assertEqual(result.text, 'Connected: browser (extension 1.2). Turn a site off with the extension icon.')


if __name__ == '__main__':
    unittest.main()
