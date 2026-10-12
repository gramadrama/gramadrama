"""Keyboard safety and persisted shortcut changes, without a live desktop or model service."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from gramadrama import checks, core
from gramadrama.engine import keys, session, shortcuts


class ShortcutTests(unittest.TestCase):
    def test_parse_and_normalize_safe_chords(self):
        for value, expected in ((' Alt + CTRL + g ', 'Ctrl+Alt+G'),
                                ('<Primary><Shift>x', 'Ctrl+Shift+X'),
                                ('Ctrl+Alt+Shift+J', 'Ctrl+Alt+Shift+J')):
            with self.subTest(value=value):
                binding = shortcuts.parse(value)
                self.assertEqual(binding.label, expected)
                self.assertEqual(shortcuts.parse(binding.accelerator), binding)

    def test_unsafe_or_ambiguous_shortcuts_are_refused(self):
        for value in ('g', 'Shift+G', 'Alt+G', 'Ctrl+G', 'Super+G', 'Ctrl+Alt+Alt+G',
                      'Ctrl+Alt+Return', 'Ctrl+Alt+ą', 'Ctrl+Alt+1', 'Ctrl+Alt+F1',
                      'Ctrl+Alt+g+H', '<Control><Alt>gg', 'Ctrl+Alt+Space', '', None, [], 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                shortcuts.parse(value)

    def test_invalid_or_duplicate_files_restore_a_complete_default_pair(self):
        for data in ({'correction_shortcut': 'a'}, {'translation_shortcut': None},
                     {'correction_shortcut': 'Ctrl+Alt+J'},
                     {'correction_shortcut': '<Alt><Control>J'}, [], None):
            with self.subTest(data=data):
                self.assertEqual(shortcuts.resolve(data), shortcuts.resolve())

    def test_custom_keys_replace_old_shortcuts_without_intercepting_typing(self):
        pair = shortcuts.resolve({'correction_shortcut': 'Ctrl+Shift+K',
                                  'translation_shortcut': 'Ctrl+Alt+Shift+L'})
        tracker = keys.KeyTracker()

        def classify(key, mods):
            return keys.classify(ord(key), 40, mods, False, tracker, bindings=pair)
        self.assertEqual(classify('k', keys.CONTROL | keys.SHIFT), keys.HOTKEY)
        self.assertEqual(classify('K', keys.CONTROL | keys.SHIFT | keys.LOCK | keys.MOD2), keys.HOTKEY)
        self.assertEqual(classify('L', keys.CONTROL | keys.MOD1 | keys.SHIFT), keys.TRANSLATE)
        self.assertEqual(classify('g', keys.CONTROL | keys.MOD1), keys.PASS)
        self.assertEqual(classify('j', keys.CONTROL | keys.MOD1), keys.PASS)
        for mods in (0, keys.SHIFT, keys.CONTROL, keys.MOD1, keys.MOD5,
                     keys.CONTROL | keys.SHIFT | keys.MOD5,
                     keys.CONTROL | keys.SHIFT | keys.SUPER):
            self.assertEqual(classify('k', mods), keys.PASS)
        tracker.consume(40)
        self.assertEqual(classify('k', keys.CONTROL | keys.SHIFT), keys.SWALLOW)
        self.assertEqual(classify('k', keys.RELEASE), keys.SWALLOW)
        self.assertEqual(classify('k', keys.RELEASE), keys.PASS)

    def test_session_uses_custom_binding_and_keeps_disabled_apps_untouched(self):
        pair = shortcuts.resolve({'correction_shortcut': 'Ctrl+Shift+K'})
        logic = session.EngineLogic()
        effects = logic.key(ord('K'), 40, keys.CONTROL | keys.SHIFT,
                            focus=('editor.desktop', 'wayland'), disabled={'editor.desktop'}, shortcuts=pair)
        self.assertEqual(effects[-1], session.ChainUp())
        self.assertEqual(effects[-2].data['reason'], 'app_off')
        release = logic.key(ord('K'), 40, keys.RELEASE, shortcuts=pair)
        self.assertEqual(release[-1], session.ChainUp())
        active = session.EngineLogic()
        effects = active.key(ord('K'), 40, keys.CONTROL | keys.SHIFT, shortcuts=pair)
        self.assertEqual(effects[-1], session.Consume())
        self.assertEqual(active.key(ord('K'), 40, keys.RELEASE, shortcuts=pair)[-1], session.Consume())

    def test_gnome_conflict_check_uses_the_selected_accelerator(self):
        bindings = [('terminal', '<Control><Alt>t'), ('other', '<Shift><Primary>k')]
        self.assertEqual(checks.hotkey_conflicts(bindings, shortcuts.parse('Ctrl+Shift+K').accelerator), ['other'])
        self.assertEqual(checks.hotkey_conflicts(bindings), [])


class SavedShortcutsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'settings.json'
        patch = mock.patch.object(core.Settings, 'path', return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_save_canonical_value_and_preserve_other_settings(self):
        core.Settings(model='example-model').save()
        self.assertEqual(checks.save_setting('correction_shortcut', 'ctrl + shift + k'), (None, None))
        settings = core.Settings.load()
        self.assertEqual(settings.correction_shortcut, 'Ctrl+Shift+K')
        self.assertEqual(settings.model, 'example-model')
        self.assertEqual(checks.read_settings()[1], {})

    def test_invalid_or_duplicate_value_does_not_change_saved_settings(self):
        core.Settings().save()
        before = self.path.read_bytes()
        for value in ('g', 'Ctrl+Alt+J', '<Primary><Alt>j'):
            with self.subTest(value=value):
                error, backup = checks.save_setting('correction_shortcut', value)
                self.assertTrue(error)
                self.assertIsNone(backup)
                self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_hand_edit_is_reported_and_can_be_repaired(self):
        self.path.write_text(json.dumps({'correction_shortcut': 'Ctrl+Shift+K',
                                         'translation_shortcut': '<Control><Shift>k'}))
        self.assertEqual(set(checks.read_settings()[1]), set(shortcuts.DEFAULTS))
        error, backup = checks.save_setting('translation_shortcut', 'Ctrl+Alt+J')
        self.assertIsNone(error)
        self.assertTrue(backup.is_file())
        self.assertEqual(checks.read_settings()[1], {})


class LanguageSettingsTests(unittest.TestCase):
    setUp = SavedShortcutsTests.setUp

    def test_switch_pair_atomically_then_choose_single_language(self):
        core.Settings(model='example-model').save()
        self.assertEqual(checks.save_settings({'primary_language': 'en', 'secondary_language': 'de'}), (None, None))
        self.assertEqual(core.Settings.load().languages, ('en', 'de'))
        self.assertEqual(checks.save_setting('secondary_language', ''), (None, None))
        self.assertEqual(core.Settings.load().languages, ('en',))
        self.assertEqual(core.Settings.load().model, 'example-model')

    def test_invalid_or_duplicate_languages_do_not_overwrite_settings(self):
        core.Settings().save()
        before = self.path.read_bytes()
        for values in ({'primary_language': 'fr'}, {'secondary_language': 'pl'}, {'secondary_language': False}):
            self.assertTrue(checks.save_settings(values)[0])
            self.assertEqual(self.path.read_bytes(), before)
