"""Model-only live settings and switch behaviour without a desktop session."""

import dataclasses
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from gramadrama import checks, core, i18n
from gramadrama.window import writing


class ModelOnlySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'settings.json'
        patch = mock.patch.object(core.Settings, 'path', return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_new_and_existing_installations_require_explicit_opt_in(self):
        self.assertIs(core.Settings().live_model_only, False)
        self.assertIs(core.Settings.load().live_model_only, False)
        self.path.write_text(json.dumps({'live_check': True, 'live_languagetool': False,
                                         'live_bielik': True, 'model': 'example-model'}))
        loaded = core.Settings.load()
        self.assertIs(loaded.live_model_only, False)
        self.assertIs(loaded.live_languagetool, False)
        self.assertIs(loaded.live_bielik, True)
        self.assertEqual(loaded.model, 'example-model')
        self.assertNotIn('live_model_only', json.loads(self.path.read_text()))

    def test_opt_in_round_trip_preserves_other_settings(self):
        initial = core.Settings(llm_url='https://model.example.com/v1', model='example-model',
                                live_check=True, live_languagetool=False, live_bielik=True,
                                live_underlines=False, correction_shortcut='Ctrl+Alt+H')
        initial.save()
        baseline = dataclasses.asdict(initial)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertEqual(checks.save_setting('live_model_only', enabled), (None, None))
                self.assertEqual(dataclasses.asdict(core.Settings.load()),
                                 dict(baseline, live_model_only=enabled))
                self.assertIs(json.loads(self.path.read_text())['live_model_only'], enabled)

    def test_switch_field_accepts_only_boolean_values(self):
        for value in (True, False):
            self.assertIsNone(checks.field_problem('live_model_only', value))
        for value in ('true', 'false', '', 0, 1, None, [], {}):
            with self.subTest(value=value):
                self.assertIsNotNone(checks.field_problem('live_model_only', value))

    def test_invalid_saved_value_is_shown_disabled_and_reported(self):
        for value in ('true', 1, None):
            self.path.write_text(json.dumps({'live_model_only': value, 'live_bielik': False}))
            with self.subTest(value=value):
                settings, problems = checks.read_settings()
                self.assertIs(settings.live_model_only, False)
                self.assertIs(settings.live_bielik, False)
                self.assertIn('live_model_only', problems)

    def test_invalid_toggle_does_not_modify_file(self):
        core.Settings(live_languagetool=False, live_bielik=False).save()
        before = self.path.read_bytes()
        error, backup = checks.save_setting('live_model_only', 'true')
        self.assertTrue(error)
        self.assertIsNone(backup)
        self.assertEqual(self.path.read_bytes(), before)


class Row:
    """The small GTK interface of the rows build_live and refresh_live use (no desktop: no real widgets)."""

    def __init__(self, *, title='', active=False, use_markup=False, subtitle=''):
        self.title, self.active, self.subtitle = title, active, subtitle
        self.sensitive = self.visible = True
        self.callbacks = []

    def get_active(self):
        return self.active

    def set_active(self, value):
        if value != self.active:
            self.active = value
            for callback in self.callbacks:
                callback(self, None)

    def set_subtitle(self, value):
        self.subtitle = value

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

    def add_prefix(self, widget):
        pass

    def add_suffix(self, widget):
        pass

    def connect(self, signal, callback):
        if signal != 'notify::active':
            raise AssertionError('Unexpected switch signal')
        self.callbacks.append(callback)


class StatusRow(Row):
    """window.common.StatusRow: a row with a check (the Słowniki row)."""

    def __init__(self, title, window):
        super().__init__(title=title)
        self.check = None

    def set_check(self, check):
        self.check, self.subtitle = check, check.text

    def get_visible(self):
        return self.visible

    def get_subtitle(self):
        return self.subtitle


class Toggles:
    """A segmented choice (window.common.toggle_group)."""

    def __init__(self, choices):
        self.names = [name for name, _ in choices]
        self.active = self.names[0]
        self.sensitive = True
        self.callbacks = []

    def get_active_name(self):
        return self.active

    def set_active_name(self, name):
        if name != self.active:
            self.active = name
            for callback in self.callbacks:
                callback(self, None)

    def set_sensitive(self, value):
        self.sensitive = value

    def get_sensitive(self):
        return self.sensitive

    def connect(self, signal, callback):
        if signal != 'notify::active-name':
            raise AssertionError('Unexpected toggle signal')
        self.callbacks.append(callback)


class Image:
    def __init__(self, **_):
        self.icon = None

    def set_from_icon_name(self, name):
        self.icon = name


class PreferencesGroup:
    def __init__(self, **properties):
        self.properties = properties
        self.rows = []

    def add(self, row):
        self.rows.append(row)


class LiveSwitchTests(unittest.TestCase):
    """Pisanie: checking while typing, with one choice of the way it checks (WritingTab.swift on the Mac)."""

    def setUp(self):
        self.addCleanup(i18n.set_language, i18n.get_language())
        i18n.set_language('pl')
        self.missing = []  # the text languages without a dictionary, as checks.missing_dictionaries says
        patcher = mock.patch.object(writing.checks, 'missing_dictionaries', lambda languages: list(self.missing))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, settings):
        owner = SimpleNamespace(write_state=mock.Mock(), toast=mock.Mock(), saved=mock.Mock(), load_settings=lambda: settings,
                                refresh_privacy=mock.Mock())
        owner.refresh_live = lambda current=None: writing.WritingTab.refresh_live(owner, current)
        owner.live_switched = lambda field, value: writing.WritingTab.live_switched(owner, field, value)
        with mock.patch.object(writing.Adw, 'SwitchRow', Row), mock.patch.object(writing.Adw, 'ActionRow', Row), \
                mock.patch.object(writing.Adw, 'PreferencesGroup', PreferencesGroup), \
                mock.patch.object(writing.Gtk, 'Image', Image), mock.patch.object(writing, 'toggle_group', Toggles), \
                mock.patch.object(writing, 'StatusRow', StatusRow):
            group = writing.WritingTab.build_live(owner, settings)
        return owner, group

    def test_the_standard_way_is_the_default_with_its_layers(self):
        owner, group = self.build(core.Settings(model='hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest'))
        self.assertEqual(owner.checking.get_active_name(), 'standard')
        self.assertEqual(owner.checking_row.subtitle, 'Słownik na tym komputerze, potem LanguageTool i model.')
        self.assertEqual([row.title for row in group.rows], ['Sprawdzaj podczas pisania', 'Sposób sprawdzania', 'Słowniki',
                                                             'Gramatyka i interpunkcja', 'Trudniejsze błędy',
                                                             'Podkreślenia pod słowami'])
        self.assertFalse(owner.dictionaries_row.visible)  # every text language has its dictionary
        self.assertTrue(owner.live_rows['live_languagetool'].visible and owner.live_rows['live_bielik'].visible)
        self.assertEqual(owner.live_rows['live_bielik'].subtitle, 'Gemma-4-12B, po dłuższej przerwie.')

    def test_model_only_hides_the_layers_and_names_the_model(self):
        owner, _ = self.build(core.Settings(live_model_only=True, model='qwen3:8b'))
        self.assertEqual(owner.checking.get_active_name(), 'model')
        self.assertIn('Eksperymentalne: tylko Qwen3:8b.', owner.checking_row.subtitle)
        self.assertFalse(owner.live_rows['live_languagetool'].visible or owner.live_rows['live_bielik'].visible)

    def test_sensitivity_and_visibility_follow_the_switch_and_the_way_without_changing_values(self):
        for enabled, model_only, lt, background in itertools.product((False, True), repeat=4):
            with self.subTest(enabled=enabled, model_only=model_only, lt=lt, background=background):
                settings = core.Settings(live_check=enabled, live_model_only=model_only,
                                         live_languagetool=lt, live_bielik=background, live_underlines=False)
                with mock.patch.object(checks, 'save_setting') as save:
                    owner, _ = self.build(settings)
                    for field, row in owner.live_rows.items():
                        self.assertIs(row.active, getattr(settings, field))
                    self.assertTrue(owner.live_rows['live_check'].sensitive)
                    self.assertEqual(owner.checking.sensitive, enabled)
                    self.assertEqual(owner.live_rows['live_underlines'].sensitive, enabled)
                    for field in ('live_languagetool', 'live_bielik'):
                        self.assertEqual(owner.live_rows[field].sensitive, enabled)
                        self.assertEqual(owner.live_rows[field].visible, not model_only)
                    save.assert_not_called()

    def test_the_way_saves_only_its_field_and_keeps_the_layers(self):
        settings = core.Settings(live_languagetool=False, live_bielik=True)
        owner, _ = self.build(settings)
        with mock.patch.object(checks, 'save_setting', return_value=(None, None)) as save:
            owner.checking.set_active_name('model')
            save.assert_called_once_with('live_model_only', True)
            settings.live_model_only = True
            self.assertFalse(owner.live_rows['live_languagetool'].visible)
            self.assertFalse(owner.live_rows['live_languagetool'].active)
            self.assertTrue(owner.live_rows['live_bielik'].active)
            owner.checking.set_active_name('standard')
            self.assertEqual(save.call_args_list, [mock.call('live_model_only', True), mock.call('live_model_only', False)])
            self.assertTrue(owner.live_rows['live_languagetool'].visible)
            self.assertFalse(owner.live_rows['live_languagetool'].active)
            self.assertTrue(owner.live_rows['live_bielik'].active)

    def test_master_toggle_preserves_the_way_and_other_saved_values(self):
        owner, _ = self.build(core.Settings(live_model_only=True, live_languagetool=True, live_bielik=False))
        with mock.patch.object(checks, 'save_setting', return_value=(None, None)) as save:
            owner.live_rows['live_check'].set_active(False)
            self.assertFalse(owner.checking.sensitive)
            self.assertFalse(owner.live_rows['live_underlines'].sensitive)
            self.assertEqual(owner.checking.get_active_name(), 'model')
            self.assertTrue(owner.live_rows['live_languagetool'].active)
            self.assertFalse(owner.live_rows['live_bielik'].active)
            owner.live_rows['live_check'].set_active(True)
            self.assertTrue(owner.checking.sensitive)
            self.assertEqual(save.call_args_list, [mock.call('live_check', False), mock.call('live_check', True)])

    def test_a_missing_dictionary(self):
        """A text language without its spelling dictionary (hunspell-pl on an English system): the Słowniki row says so,
        with the command; not while checking is off or in the model-only way, which uses no dictionary."""
        self.missing = ['pl']
        owner, _ = self.build(core.Settings())
        self.assertTrue(owner.dictionaries_row.visible)
        self.assertIn('Brak słownika: polski', owner.dictionaries_row.subtitle)
        self.assertEqual(owner.dictionaries_row.check.action, 'install_dictionaries')
        owner.checking.set_active_name('model')
        self.assertFalse(owner.dictionaries_row.visible)
        owner.checking.set_active_name('standard')
        owner.live_rows['live_check'].set_active(False)
        self.assertFalse(owner.dictionaries_row.visible)

    def test_english_texts(self):
        i18n.set_language('en')
        owner, _ = self.build(core.Settings(live_model_only=True, model='qwen3:8b'))
        self.assertEqual(owner.checking_row.title, 'Checking')
        self.assertIn('Experimental: only Qwen3:8b.', owner.checking_row.subtitle)


if __name__ == '__main__':
    unittest.main()
