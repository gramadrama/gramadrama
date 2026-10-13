"""Opt-in live model checking: routing, hot switches, cache reuse and stale work."""
import dataclasses
import threading
import time
import unittest
from unittest import mock

from gramadrama import core, live as L, liveservice as S
from stub_server import Stub, stream, sse


class ModelOnlyTests(unittest.TestCase):
    def setUp(self):
        self.current = core.Settings(llm_url='https://example.com', model='test-model',
                                     live_model_only=True, live_bielik=False)
        self.events = []
        self.server = mock.Mock()
        self.checker = mock.Mock()
        self.checker.quick.return_value = []
        self.checker.languagetool.return_value = []
        self.calls = []

        def correct(server, fragment, cancel, words=()):
            self.calls.append(fragment)
            return fragment.replace('domm', 'dom').replace('testt', 'test')

        for patch in (mock.patch.object(S, 'settings', side_effect=lambda: self.current),
                      mock.patch.object(S, 'backend', return_value=self.server),
                      mock.patch.object(S.model_live, 'correct_fragment', side_effect=correct),
                      mock.patch.object(S, 'emit', side_effect=lambda **e: self.events.append(e)),
                      mock.patch.object(S, 'LT_PAUSE', .02),
                      mock.patch.object(S, 'BIELIK_PAUSE', .04),
                      mock.patch.object(S, 'SETTINGS_SECONDS', .01),
                      mock.patch.object(S, 'MARKS_INTERVAL', .01)):
            patch.start()
            self.addCleanup(patch.stop)
        self.monitor = S.Monitor(self.checker, geometry=lambda: False)
        self.addCleanup(self.monitor.stop)

    def check(self, text):
        self.monitor.text_changed(1, text, None)
        self.monitor._layer2(self.monitor.version, 1, text)

    def wait_for(self, condition):
        until = time.monotonic() + 2
        while not condition() and time.monotonic() < until:
            time.sleep(.005)
        self.assertTrue(condition())

    def correction(self, sid, text):
        self.monitor.request_correction(sid, text)
        self.wait_for(lambda: any(e.get('ev') == 'correction' and e['id'] == sid for e in self.events))
        return next(e for e in reversed(self.events) if e.get('ev') == 'correction' and e['id'] == sid)

    def activity(self):
        return [event['checking'] for event in self.events if event['ev'] == 'issues']

    def test_indicator_wraps_real_model_batch_but_not_cached_check(self):
        observed = []

        def checking(server, text, cancel, words=()):
            observed.append(self.activity()[-1])
            return text

        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=checking):
            self.check('Pierwsze zdanie. Drugie zdanie.')
            self.assertEqual(observed, [True, True])
            self.assertEqual(self.activity(), [True, False])
            self.events.clear()
            self.check('Pierwsze zdanie. Drugie zdanie.')
            self.assertNotIn(True, self.activity())

    def test_new_revision_of_identical_text_echoes_activity_without_restarting_check(self):
        self.monitor.text_changed(1, 'To domm.', None, revision=40)
        version = self.monitor.version

        def next_revision(server, text, cancel, words=()):
            self.monitor.text_changed(1, text, None, revision=41)
            self.assertEqual(self.monitor.version, version)
            self.assertFalse(cancel.is_set())
            active = [e for e in self.events if e['ev'] == 'issues'][-1]
            self.assertEqual((active['revision'], active['checking']), (41, True))
            return 'To dom.'

        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=next_revision) as correct:
            self.monitor._layer2(version, 1, 'To domm.')
            self.assertEqual(correct.call_count, 1)
        result = [e for e in self.events if e['ev'] == 'issues'][-1]
        self.assertEqual((result['revision'], result['checking'], result['issues'][0]['word']),
                         (41, False, 'domm'))
        count = len(self.events)
        self.monitor.text_changed(1, 'To domm.', None, revision=42)
        self.assertEqual(len(self.events), count + 1)
        self.assertEqual(self.events[-1]['revision'], 42)
        self.monitor.text_changed(1, 'To domm.', None, revision=42)
        self.assertEqual(len(self.events), count + 1)

    def test_indicator_clears_on_failure_cancellation_and_unexpected_error(self):
        for error in (core.BackendError('synthetic'), core.Cancelled(), RuntimeError('synthetic')):
            with self.subTest(error=type(error).__name__):
                self.monitor.backoff_until = 0
                self.events.clear()
                with mock.patch.object(S.model_live, 'correct_fragment', side_effect=error):
                    if isinstance(error, RuntimeError):
                        with self.assertRaises(RuntimeError):
                            self.check('Nowy tekst.')
                    else:
                        self.check('Nowy tekst.')
                self.assertEqual(self.activity(), [True, False])

    def test_indicator_clears_immediately_on_edit_clear_settings_or_stop(self):
        for operation in ('edit', 'clear', 'settings', 'stop'):
            with self.subTest(operation=operation):
                monitor = S.Monitor(self.checker, geometry=lambda: False)
                self.addCleanup(monitor.stop)
                self.events.clear()
                monitor.text_changed(1, 'To domm.', None)

                def interrupt(server, text, cancel, words=()):
                    self.assertTrue(self.activity()[-1])
                    if operation == 'edit':
                        monitor.text_changed(1, 'Inny tekst.', None)
                    elif operation == 'settings':
                        monitor.refresh_settings(dataclasses.replace(self.current, model='other-model'))
                    else:
                        getattr(monitor, operation)()
                    self.assertFalse(self.activity()[-1])
                    return 'To dom.'

                with mock.patch.object(S.model_live, 'correct_fragment', side_effect=interrupt):
                    monitor._layer2(monitor.version, 1, 'To domm.')
                self.assertFalse(self.activity()[-1])

    def test_manual_reuses_full_results_and_matching_complete_fragments(self):
        self.check(' \tTo domm.  Nowy testt.\r\n')
        self.assertEqual(self.correction(1, ' \tTo domm.  Nowy testt.\r\n')['corrected'],
                         ' \tTo dom.  Nowy test.\r\n')
        self.assertEqual(self.correction(2, 'Nowy testt.')['corrected'], 'Nowy test.')
        self.assertEqual(self.calls, ['To domm.', 'Nowy testt.'])
        result = self.correction(3, 'Nowy testt. Jeszcze domm.')
        self.assertEqual((result['kind'], result['corrected']), ('result', 'Nowy test. Jeszcze dom.'))
        self.assertEqual(self.calls, ['To domm.', 'Nowy testt.', 'Jeszcze domm.'])
        self.assertIn('rows', result)
        self.assertIn('segments', result)
        self.checker.languagetool.assert_not_called()
        self.server.correct.assert_not_called()

    def test_manual_cached_correction_preserves_new_multiline_code_context(self):
        self.check('domm')
        result = self.correction(4, '```\ndomm\n```\nTo domm.')
        self.assertEqual((result['kind'], result['corrected']), ('result', '```\ndomm\n```\nTo dom.'))

    def test_manual_result_populates_cache_for_next_live_check(self):
        self.assertEqual(self.correction(12, 'To domm.')['corrected'], 'To dom.')
        self.events.clear()
        self.check('To domm.')
        self.assertEqual(self.calls, ['To domm.'])
        self.assertNotIn(True, self.activity())
        self.assertEqual(self.monitor.visible[0]['suggestions'], ['dom'])

    def test_manual_failure_is_error_and_ineligible_mode_is_only_fallback(self):
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=core.BackendError('synthetic')):
            self.assertEqual(self.correction(5, 'To domm.')['kind'], 'error')
        self.current = dataclasses.replace(self.current, live_model_only=False)
        self.assertEqual(self.correction(6, 'To domm.')['kind'], 'fallback')
        self.current = dataclasses.replace(self.current, live_model_only=True, live_check=False)
        self.assertEqual(self.correction(7, 'To domm.')['kind'], 'fallback')

    def test_manual_joins_live_call_across_panel_clear_without_duplicate_request(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(server, text, cancel, words=()):
            calls.append(text)
            entered.set()
            self.assertTrue(release.wait(2))
            return text.replace('domm', 'dom')

        self.monitor.text_changed(1, 'To domm.', None)
        worker = threading.Thread(target=self.monitor._layer2, args=(self.monitor.version, 1, 'To domm.'))
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=blocked):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.monitor.request_correction(8, 'To domm.')
                self.monitor.clear()  # opening the manual panel suspends live checking
                self.assertFalse(self.monitor.bielik_cancel.is_set())
                self.assertFalse(self.activity()[-1])
            finally:
                release.set()
                worker.join(2)
            self.wait_for(lambda: any(e.get('ev') == 'correction' and e['id'] == 8 for e in self.events))
        self.assertEqual(calls, ['To domm.'])
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.monitor.visible, [])
        self.assertEqual(next(e for e in self.events if e.get('ev') == 'correction')['corrected'], 'To dom.')

    def test_manual_cancel_or_settings_change_prevents_late_result(self):
        for operation in ('cancel', 'settings'):
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()
                self.events.clear()

                def blocked(server, text, cancel, words=()):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return 'To dom.'

                with mock.patch.object(S.model_live, 'correct_fragment', side_effect=blocked):
                    self.monitor.request_correction(9, 'To domm.')
                    self.assertTrue(entered.wait(2))
                    try:
                        if operation == 'cancel':
                            self.monitor.cancel_correction(9)
                        else:
                            self.current = dataclasses.replace(self.current, model='other-model')
                            self.monitor.refresh_settings(self.current)
                    finally:
                        release.set()
                    self.wait_for(lambda: not self.monitor.model_lock.locked())
                messages = [e for e in self.events if e.get('ev') == 'correction']
                self.assertEqual([e['kind'] for e in messages], [] if operation == 'cancel' else ['error'])
                self.assertFalse(self.monitor.fragments._cache)

    def test_old_remote_completion_does_not_clear_new_version_activity(self):
        self.current = dataclasses.replace(self.current, live_model_only=False,
                                           languagetool_url='https://example.com')
        self.monitor.refresh_settings(self.current)
        first, second, release_first, release_second = (threading.Event() for _ in range(4))

        def blocked(text, server, off=()):
            ready, release = (first, release_first) if text.text == 'Pierwsze.' else (second, release_second)
            ready.set()
            self.assertTrue(release.wait(2))
            return []

        self.checker.languagetool.side_effect = blocked
        self.monitor.text_changed(1, 'Pierwsze.', None)
        older = threading.Thread(target=self.monitor._layer1, args=(self.monitor.version, 1, 'Pierwsze.'))
        older.start()
        newer = None
        try:
            self.assertTrue(first.wait(2))
            self.monitor.text_changed(1, 'Drugie.', None)
            newer = threading.Thread(target=self.monitor._layer1, args=(self.monitor.version, 1, 'Drugie.'))
            newer.start()
            self.assertTrue(second.wait(2))
            self.assertTrue(self.activity()[-1])
            release_first.set()
            older.join(2)
            self.assertTrue(self.activity()[-1])
        finally:
            release_first.set()
            release_second.set()
            older.join(2)
            if newer is not None:
                newer.join(2)
        self.assertFalse(self.activity()[-1])

    def test_scheduler_does_not_debounce_again_after_old_call_finishes(self):
        first, release, second = (threading.Event() for _ in range(3))

        def layer(version, field, text):
            if text == 'Pierwsze.':
                first.set()
                release.wait(2)
            else:
                second.set()
                self.monitor.stop()

        self.monitor.text_changed(1, 'Pierwsze.', None)
        worker = threading.Thread(target=self.monitor._paused, args=(.02, layer))
        worker.start()
        try:
            self.assertTrue(first.wait(2))
            self.monitor.text_changed(1, 'Drugie.', None)
            with self.monitor.lock:
                self.monitor.changed_at = time.monotonic() - 1  # the last edit's pause already elapsed
            real_wait, timed_waits = self.monitor.lock.wait, []

            def wait(timeout=None):
                if timeout is not None:
                    timed_waits.append(timeout)
                return real_wait(timeout)

            with mock.patch.object(self.monitor.lock, 'wait', side_effect=wait):
                release.set()
                self.assertTrue(second.wait(2))
                worker.join(2)
            self.assertEqual(timed_waits, [])
        finally:
            release.set()
            self.monitor.stop()
            worker.join(2)

    def test_joined_live_call_is_cancelled_when_manual_session_ends(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(server, text, cancel, words=()):
            entered.set()
            release.wait(2)
            return 'To dom.'

        self.monitor.text_changed(1, 'To domm.', None)
        worker = threading.Thread(target=self.monitor._layer2, args=(self.monitor.version, 1, 'To domm.'))
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=blocked):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.monitor.request_correction(10, 'To domm.')
                self.monitor.clear()
                self.monitor.cancel_correction(10)
                self.assertTrue(self.monitor.bielik_cancel.is_set())
            finally:
                release.set()
                worker.join(2)
            self.wait_for(lambda: not self.monitor.model_lock.locked())
        self.assertFalse(any(e.get('ev') == 'correction' for e in self.events))
        self.assertFalse(self.monitor.fragments._cache)

    def test_replaced_manual_request_cancels_obsolete_joined_live_work(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(server, text, cancel, words=()):
            if text == 'To domm.':
                entered.set()
                release.wait(2)
            return text.replace('domm', 'dom')

        self.monitor.text_changed(1, 'To domm.', None)
        worker = threading.Thread(target=self.monitor._layer2, args=(self.monitor.version, 1, 'To domm.'))
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=blocked):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.monitor.request_correction(20, 'To domm.')
                self.monitor.clear()
                self.monitor.request_correction(21, 'Inny domm.')
                self.assertTrue(self.monitor.bielik_cancel.is_set())
            finally:
                release.set()
                worker.join(2)
            self.wait_for(lambda: any(e.get('ev') == 'correction' and e['id'] == 21 for e in self.events))
        messages = [e for e in self.events if e.get('ev') == 'correction']
        self.assertEqual([(e['id'], e['corrected']) for e in messages], [(21, 'Inny dom.')])

    def test_settings_change_discovered_at_completion_terminates_manual_request(self):
        def change_settings(server, text, cancel, words=()):
            self.current = dataclasses.replace(self.current, model='other-model')
            return 'To dom.'

        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=change_settings):
            result = self.correction(11, 'To domm.')
        self.assertEqual(result['kind'], 'error')
        self.assertIn('Ustawienia zmieniły', result['message'])
        self.assertFalse(self.monitor.fragments._cache)

    def test_experimental_mode_runs_model_even_with_legacy_model_toggle_off(self):
        self.current = dataclasses.replace(self.current, languagetool_url='invalid unused address',
                                           llm_think='model')
        self.monitor.run()
        self.monitor.text_changed(1, 'To domm.', None)
        self.wait_for(lambda: self.monitor.visible)
        self.assertEqual(self.calls, ['To domm.'])
        self.checker.quick.assert_not_called()
        self.checker.languagetool.assert_not_called()
        self.server.correct.assert_not_called()
        self.assertEqual(self.monitor.visible[0]['source'], 'Model')
        self.assertEqual(self.monitor.visible[0]['suggestions'], ['dom'])
        used = S.backend.call_args.args[0]
        self.assertEqual((used.languagetool_url, used.languagetool_local_url, used.llm_think), ('', '', 'off'))
        self.assertEqual(self.current.llm_think, 'model')

    def test_completed_sentence_stays_cached_when_next_sentence_is_typed(self):
        self.check('To domm.')
        self.check('To domm. Nowy testt.')
        self.assertEqual(self.calls, ['To domm.', 'Nowy testt.'])
        self.assertEqual([i.word for i in self.monitor.bielik], ['domm', 'testt'])
        self.check('To domm. Nowy testt. Jeszcze coś.')
        self.assertEqual(len(self.calls), 3)
        last = next(e for e in reversed(self.events) if e.get('event') == 'live_model')
        self.assertEqual((last['cache_hits'], last['model_calls']), (2, 1))
        self.assertNotIn('domm', str([e for e in self.events if e['ev'] == 'log']))

    def test_continuing_same_sentence_rechecks_it(self):
        self.check('To domm')
        self.check('To domm na wzgórzu.')
        self.assertEqual(self.calls, ['To domm', 'To domm na wzgórzu.'])

    def test_model_change_invalidates_cache_and_unchanged_field_results(self):
        self.check('To domm.')
        self.current = dataclasses.replace(self.current, model='other-test-model')
        version = self.monitor.version
        self.monitor.refresh_settings(self.current)
        self.assertGreater(self.monitor.version, version)
        self.assertEqual(self.monitor.visible, [])
        self.check('To domm.')
        self.assertEqual(len(self.calls), 2)

    def test_mode_switch_cancels_old_work_and_restores_saved_layers(self):
        self.current = dataclasses.replace(self.current, live_model_only=False,
                                           live_languagetool=True, live_bielik=False,
                                           languagetool_url='https://example.com')
        self.monitor.refresh_settings(self.current)
        self.monitor.text_changed(1, 'To domm.', None)
        self.monitor.quick = [L.Issue(3, 7, 'domm', '', ('dom',), 'słownik', 'SPELLING_PL')]
        self.monitor._send()
        pending = self.monitor.bielik_cancel = threading.Event()
        self.current = dataclasses.replace(self.current, live_model_only=True)
        self.monitor.refresh_settings(self.current)
        self.assertTrue(pending.is_set())
        self.assertEqual(self.monitor.visible, [])
        self.monitor._layer1(self.monitor.version, 1, 'To domm.')
        self.checker.languagetool.assert_not_called()
        self.check('To domm.')
        self.current = dataclasses.replace(self.current, live_model_only=False)
        self.monitor.refresh_settings(self.current)
        self.monitor._layer1(self.monitor.version, 1, 'To domm.')
        self.checker.languagetool.assert_called_once()
        self.assertTrue(self.current.live_languagetool)
        self.assertFalse(self.current.live_bielik)
        self.assertEqual(self.monitor.bielik, [])

    def test_settings_poll_switches_mode_without_another_keystroke(self):
        self.current = dataclasses.replace(self.current, live_model_only=False)
        self.monitor.run()
        self.monitor.text_changed(1, 'To domm.', None)
        self.wait_for(lambda: self.checker.quick.called)
        self.current = dataclasses.replace(self.current, live_model_only=True)
        self.wait_for(lambda: bool(self.monitor.visible))
        self.assertEqual(self.monitor.visible[0]['source'], 'Model')
        self.current = dataclasses.replace(self.current, live_check=False)
        self.wait_for(lambda: not self.monitor.visible)
        self.assertFalse(self.monitor.enabled)

    def test_late_model_reply_is_discarded_after_edit_or_mode_change(self):
        for change in ('edit', 'mode'):
            with self.subTest(change=change):
                self.current = dataclasses.replace(self.current, live_model_only=True)
                self.monitor.refresh_settings(self.current)
                self.monitor.text_changed(1, 'To domm.', None)

                def late(*args, **kwargs):
                    if change == 'edit':
                        self.monitor.text_changed(1, 'Nowy tekst.', None)
                    else:
                        self.current = dataclasses.replace(self.current, live_model_only=False)
                    return 'To dom.'

                with mock.patch.object(S.model_live, 'correct_fragment', side_effect=late):
                    self.monitor._layer2(self.monitor.version, 1, 'To domm.')
                self.assertEqual(self.monitor.bielik, [])

    def test_late_languagetool_reply_is_discarded_when_model_only_is_enabled(self):
        self.current = dataclasses.replace(self.current, live_model_only=False,
                                           languagetool_url='https://example.com')
        self.monitor.refresh_settings(self.current)

        def late(*args, **kwargs):
            self.current = dataclasses.replace(self.current, live_model_only=True)
            return [L.Issue(0, 4, 'Test', '', (), 'LanguageTool', 'TEST')]

        self.checker.languagetool.side_effect = late
        self.monitor._layer1(self.monitor.version, 1, 'Test')
        self.assertEqual(self.monitor.lt, [])

    def test_model_failure_has_no_fallback_to_other_layers(self):
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=core.BackendError('synthetic')):
            self.check('To domm.')
        self.monitor._layer1(self.monitor.version, 1, 'To domm.')
        self.checker.quick.assert_not_called()
        self.checker.languagetool.assert_not_called()
        self.assertGreater(self.monitor.backoff_until, time.monotonic())
        self.assertEqual(self.monitor.bielik, [])

    def issues(self):
        return [event for event in self.events if event['ev'] == 'issues']

    def test_a_server_failure_pauses_with_a_note_that_every_field_shows(self):
        # macOS LiveMonitor: the results so far stay, checking pauses for a minute, and every field says why it shows
        # nothing; the badge is then a „!”. A settings change ends the pause and its note.
        paused = 'Test-model nie odpowiada — sprawdzanie wstrzymane na minutę.'
        self.check('To domm.')
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=core.BackendError('x', kind='timeout')):
            self.check('To domm. Pies spi.')
        last = self.issues()[-1]
        self.assertEqual(([i['word'] for i in last['issues']], last.get('note'), last['checking']), (['domm'], paused, False))
        calls = len(self.calls)
        self.monitor.text_changed(2, 'Inny tekst.', None)
        self.assertIsNone(self.monitor.note)  # another field: its own check says it again
        self.monitor._layer2(self.monitor.version, 2, 'Inny tekst.')
        last = self.issues()[-1]
        self.assertEqual((last['field'], last['issues'], last.get('note')), (2, [], paused))
        self.assertEqual(len(self.calls), calls)  # the model is not asked during the pause
        self.current = dataclasses.replace(self.current, model='other-test-model')
        self.monitor.refresh_settings(self.current)
        self.assertNotIn('note', self.issues()[-1])
        self.assertEqual(self.monitor.backoff_until, 0)

    def test_an_unusable_answer_leaves_its_sentence_unchecked_with_a_note(self):
        def correct(server, fragment, cancel, words=()):
            if 'Pies' in fragment:
                raise core.BackendError('x', kind='empty_reply')
            return fragment.replace('domm', 'dom')
        with mock.patch.object(S.model_live, 'correct_fragment', side_effect=correct):
            self.check('To domm. Pies spi.')
        last = self.issues()[-1]
        self.assertEqual(([i['word'] for i in last['issues']], last.get('note')),
                         (['domm'], '1 zdanie bez sprawdzenia — model zwrócił niepewną odpowiedź.'))
        self.assertEqual(self.monitor.backoff_until, 0)  # the server answered: no pause
        self.assertEqual(next(e for e in reversed(self.events) if e.get('event') == 'live_model')['rejected'], 1)
        self.check('To domm. Pies spi.')  # a usable answer now: the note goes
        self.assertNotIn('note', self.issues()[-1])
        self.assertEqual(S.unchecked_note(2), 'Zdania: 2 bez sprawdzenia — model zwrócił niepewną odpowiedź.')

    def test_the_standard_mode_has_no_note(self):
        self.current = dataclasses.replace(self.current, live_model_only=False, live_bielik=True)
        self.monitor.refresh_settings(self.current)
        self.monitor.note = 'x'
        self.monitor.text_changed(1, 'To domm.', None)
        self.assertTrue(all('note' not in e for e in self.issues()))

    def test_old_failure_cannot_back_off_a_new_profile_during_settings_reload(self):
        self.monitor.text_changed(1, 'To domm.', None)
        previous, version = self.current, self.monitor.version
        cancel = self.monitor.bielik_cancel = threading.Event()

        def reload_between_read_and_failure():
            self.current = dataclasses.replace(previous, model='other-test-model')
            self.monitor.refresh_settings(self.current)
            return previous  # a read that completed just before another thread reloaded

        with mock.patch.object(S, 'settings', side_effect=reload_between_read_and_failure):
            self.monitor._model_failed(version, previous, cancel, 'live_model_failed', 'To domm.',
                                      core.BackendError('x', kind='timeout'))
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.monitor.backoff_until, 0)
        self.assertFalse(any(e.get('event') == 'live_model_failed' for e in self.events))


class ModelOnlyProtocolTests(unittest.TestCase):
    def test_both_protocols_return_underlines_and_send_only_changed_sentences(self):
        for protocol in ('ollama', 'openai'):
            with self.subTest(protocol=protocol), Stub() as stub:
                current = core.Settings(**dict(stub.settings(), llm_api=protocol,
                                               live_model_only=True, live_bielik=False))
                sent = []

                def response(request, formatter):
                    content = request['messages'][-1]['content']
                    fragment = content.split('<<<TEKST>>>\n', 1)[1].rsplit('\n<<<KONIEC TEKSTU>>>', 1)[0]
                    sent.append(fragment)
                    return formatter(fragment.replace('domm', 'dom').replace('testt', 'test'))

                stub.reply = lambda request: response(request, stream)
                stub.openai_reply = lambda request: response(request, sse)
                with mock.patch.object(S, 'settings', return_value=current), mock.patch.object(S, 'emit'):
                    checker = mock.Mock()
                    monitor = S.Monitor(checker)
                    try:
                        for text in ('To domm.', 'To domm. Nowy testt.', 'To domm. Nowy testt.'):
                            monitor.text_changed(1, text, None)
                            monitor._layer1(monitor.version, 1, text)
                            monitor._layer2(monitor.version, 1, text)
                        self.assertEqual(sent, ['To domm.', 'Nowy testt.'])
                        self.assertEqual([(i.word, i.suggestions) for i in monitor.bielik],
                                         [('domm', ('dom',)), ('testt', ('test',))])
                        self.assertTrue(all(text[i.start:i.end] == i.word for i in monitor.bielik))
                        self.assertFalse(any(path == '/v2/check' for path, _ in stub.requests))
                        checker.languagetool.assert_not_called()
                        if protocol == 'ollama':
                            self.assertTrue(all(body['think'] is False for path, body in stub.requests
                                                if path == '/api/chat'))
                    finally:
                        monitor.stop()


if __name__ == '__main__':
    unittest.main()
