"""Synthetic model-only live tests; no settings, requests or model runtime."""
import dataclasses
import threading
import unittest
from unittest import mock

from gramadrama import core, live as L, model_live as M


class Corrector:
    def __init__(self, corrections=None):
        self.corrections = corrections or {}
        self.calls = []

    def __call__(self, text, cancel):
        self.calls.append(text)
        return self.corrections.get(text, text)


class SplitTests(unittest.TestCase):
    def test_sentence_and_line_spans_preserve_separators_and_unicode(self):
        text = ' \tKot śpi.  „Pies czeka!”\r\n\n🐢 Jeszcze piszę\t'
        spans = M.split_fragments(text)
        self.assertEqual([s.text for s in spans], ['Kot śpi.', '„Pies czeka!”', '🐢 Jeszcze piszę'])
        covered = set()
        for span in spans:
            self.assertEqual(text[span.start:span.end], span.text)
            covered.update(range(span.start, span.end))
        self.assertTrue(all(char.isspace() for index, char in enumerate(text) if index not in covered))

    def test_common_abbreviations_initials_decimals_and_dates(self):
        text = 'Dr. A. Nowak podał np. 3.14 oraz 12. 10. 2026 r. jako dane. Dalej czeka kot.'
        self.assertEqual([s.text for s in M.split_fragments(text)],
                         ['Dr. A. Nowak podał np. 3.14 oraz 12. 10. 2026 r. jako dane.', 'Dalej czeka kot.'])

    def test_stable_later_sentences_and_unfinished_tail(self):
        before = 'Kot śpi. Pies czeka. Jeszcze piszę'
        after = 'Mały kot śpi bardzo długo. Pies czeka. Jeszcze piszę'
        self.assertEqual([s.text for s in M.split_fragments(before)][1:],
                         [s.text for s in M.split_fragments(after)][1:])

    def test_fallback_respects_limit_without_losing_non_whitespace(self):
        for text, limit in [('alpha  beta\tgamma delta epsilon', 12), ('🐢' * 11, 4), ('x' * 650, 320)]:
            with self.subTest(limit=limit):
                spans = M.split_fragments(text, limit)
                self.assertTrue(all(0 < len(s.text) <= limit for s in spans))
                self.assertEqual(''.join(c for c in text if not c.isspace()),
                                 ''.join(c for s in spans for c in s.text if not c.isspace()))

    def test_invalid_limits_and_unicode(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                M.FragmentChecker(max_chars=value)
        with self.assertRaises(core.BackendError):
            M.split_fragments('\ud800')


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.checker = M.FragmentChecker()
        self.correct = Corrector({'Kot ma domm.': 'Kot ma dom.', '🐢 Kot ma domm.': '🐢 Kot ma dom.'})

    def check(self, text, identity=('synthetic-model', M.PROMPT_VERSION, ('pl', 'en'))):
        return self.checker.check(text, self.correct, identity, self.cancel)

    def test_cache_includes_clean_results_and_returns_fresh_issue_lists(self):
        text = 'Kot ma domm.  Pies śpi.\n'
        first, second = self.check(text), self.check(text)
        self.assertEqual((first.model_calls, first.cache_hits, first.fragments), (2, 0, 2))
        self.assertEqual((second.model_calls, second.cache_hits), (0, 2))
        self.assertEqual(first.issues, second.issues)
        self.assertEqual(first.corrected, 'Kot ma dom.  Pies śpi.\n')
        self.assertEqual(first.corrected, second.corrected)
        self.assertEqual([(i.word, i.suggestions, i.source, i.rule) for i in second.issues],
                         [('domm', ('dom',), 'Model', 'MODEL_LIVE')])
        first.issues.clear()
        self.assertEqual(len(self.check(text).issues), 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            second.issues[0].start = 999

    def test_repeated_sentences_and_prefix_insertion_reuse_relative_offsets(self):
        text = '🐢 Kot ma domm. Kot ma domm. Kot ma domm.'
        first = self.check(text)
        self.assertEqual((first.model_calls, first.cache_hits), (2, 1))
        prefix = 'Nowy początek.\n'
        second = self.check(prefix + text)
        self.assertEqual((second.model_calls, second.cache_hits), (1, 3))
        self.assertEqual([(i.start, i.end) for i in second.issues],
                         [(i.start + len(prefix), i.end + len(prefix)) for i in first.issues])
        for issue in second.issues:
            self.assertEqual((prefix + text)[issue.start:issue.end], issue.word)

    def test_completed_sentence_is_not_resent_after_appending_spaces_or_new_sentence(self):
        self.check('To test.')
        for text in ('To test. ', 'To test.\n', ' To test.\r\n '):
            with self.subTest(text=text):
                result = self.check(text)
                self.assertEqual((result.model_calls, result.cache_hits), (0, 1))
        result = self.check('To test. Nowe zdanie.')
        self.assertEqual((result.model_calls, result.cache_hits), (1, 1))
        self.assertEqual(self.correct.calls, ['To test.', 'Nowe zdanie.'])

    def test_same_sentence_continuation_and_one_changed_sentence_are_rechecked(self):
        self.check('Kot ma domm. Pies śpi. Jeszcze piszę')
        result = self.check('Kot ma domm. Pies czeka. Jeszcze piszę dalej')
        self.assertEqual((result.model_calls, result.cache_hits), (2, 1))

    def test_mask_recomputed_for_whole_document_on_cache_hits(self):
        self.correct.corrections['domm'] = 'dom'
        self.assertEqual(len(self.check('domm').issues), 1)
        protected = self.check('`\ndomm\n`')
        self.assertEqual(protected.issues, [])
        self.assertEqual(protected.corrected, '`\ndomm\n`')
        self.assertGreaterEqual(protected.cache_hits, 1)
        self.assertEqual(len(self.check('domm').issues), 1)

    def test_masked_first_result_does_not_poison_unmasked_relative_cache(self):
        self.correct.corrections['domm'] = 'dom'
        self.assertEqual(self.check('`\ndomm\n`').issues, [])
        plain = self.check('domm')
        self.assertEqual((len(plain.issues), plain.model_calls, plain.cache_hits), (1, 0, 1))

    def test_identity_changes_clear_old_results(self):
        self.check('Kot ma domm.', ('model-one', 'prompt-one', ('pl',)))
        for identity in [('model-two', 'prompt-one', ('pl',)), ('model-two', 'prompt-two', ('pl',)),
                         ('model-two', 'prompt-two', ('en',))]:
            with self.subTest(identity=identity):
                self.assertEqual(self.check('Kot ma domm.', identity).model_calls, 1)
        self.checker.clear()
        self.assertEqual(self.check('Kot ma domm.', identity).model_calls, 1)

    def test_prompt_version_and_fragment_limit_are_implicitly_in_identity(self):
        self.check('Kot ma domm.')
        with mock.patch.object(M, 'PROMPT_VERSION', 'synthetic-next-prompt'):
            self.assertEqual(self.check('Kot ma domm.').model_calls, 1)
            self.assertEqual(self.check('Kot ma domm.').model_calls, 0)
            self.checker.max_chars = 300
            self.assertEqual(self.check('Kot ma domm.').model_calls, 1)

    def test_lru_entry_and_byte_limits_and_disabled_cache(self):
        self.checker = M.FragmentChecker(max_entries=2)
        for text in ('Pierwsze.', 'Drugie.', 'Pierwsze.', 'Trzecie.'):
            self.check(text)
        self.assertEqual(self.check('Pierwsze.').model_calls, 0)
        self.assertEqual(self.check('Drugie.').model_calls, 1)
        for options in ({'max_entries': 0}, {'max_cache_bytes': 0}, {'max_cache_bytes': 1}):
            with self.subTest(options=options):
                self.checker = M.FragmentChecker(**options)
                self.check('Kot ma domm.')
                self.assertEqual(self.check('Kot ma domm.').model_calls, 1)
                self.assertEqual(self.checker._bytes, 0)
        # The second equally sized clean entry must evict the first by bytes.
        self.checker = M.FragmentChecker()
        self.check('Pierwsze.')
        self.checker.max_cache_bytes = self.checker._bytes
        self.check('Następne.')
        self.assertLessEqual(self.checker._bytes, self.checker.max_cache_bytes)
        self.assertLessEqual(len(self.checker._cache), 1)

    def test_whitespace_makes_no_requests_and_no_entries(self):
        result = self.check('\n \t')
        self.assertEqual(result, M.CheckResult([], 0, 0, 0, '\n \t'))
        self.assertEqual(self.correct.calls, [])
        self.assertEqual(len(self.checker._cache), 0)

    def test_punctuation_insertion_stays_inside_fragment(self):
        self.correct.corrections['Wiem że działa.'] = 'Wiem, że działa.'
        text = '🐢\nWiem że działa. Dalej czekam.'
        result = self.check(text)
        self.assertEqual([(i.word, i.suggestions) for i in result.issues], [('Wiem', ('Wiem,',))])
        issue = result.issues[0]
        self.assertEqual(text[issue.start:issue.end], 'Wiem')

    def test_full_correction_preserves_separators_and_punctuation_on_cache_hits(self):
        self.correct.corrections['Wiem że działa.'] = 'Wiem, że działa.'
        text = ' \tWiem że działa.  Kot ma domm.\r\n\n🐢\t'
        expected = ' \tWiem, że działa.  Kot ma dom.\r\n\n🐢\t'
        self.assertEqual(self.check(text).corrected, expected)
        cached = self.check(text)
        self.assertEqual((cached.corrected, cached.model_calls), (expected, 0))

    def test_full_cached_result_respects_multiline_code_and_long_link_context(self):
        self.correct.corrections['domm'] = 'dom'
        self.check('domm')
        for text in ('```\ndomm\n```', '`\ndomm\n`'):
            with self.subTest(text=text):
                result = self.check(text)
                self.assertEqual(result.corrected, text)
                self.assertEqual(result.issues, [])
        self.checker = M.FragmentChecker(max_chars=4)
        self.correct.corrections['domm'] = 'dom'
        self.check('domm')
        text = 'https://example.com/domm'
        self.assertEqual(self.check(text).corrected, text)

    def test_full_result_is_not_rebuilt_from_visible_issue_list(self):
        self.correct.corrections['Kot  śpi.'] = 'Kot śpi.'
        result = self.check('Kot  śpi.')
        self.assertEqual(result.issues, [])  # live suggestions intentionally omit whitespace-only edits
        self.assertEqual(result.corrected, 'Kot śpi.')

    def test_suppressed_insertion_at_code_boundary_has_no_live_suggestion(self):
        self.correct.corrections['Wiem`x`'] = 'Wiem, `x`'
        for _ in range(2):
            result = self.check('Wiem`x`')
            self.assertEqual(result.corrected, 'Wiem`x`')
            self.assertEqual(result.issues, [])
        self.assertEqual(self.correct.calls, ['Wiem`x`'])


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.checker = M.FragmentChecker()
        self.cancel = threading.Event()

    def check(self, correct, text='Kot ma domm.'):
        return self.checker.check(text, correct, 'synthetic-identity', self.cancel)

    def test_invalid_response_is_never_cached_as_clean(self):
        for output in (None, 42, '', '  ', 'Tekst nie zawiera błędów.', '[Your name]', '\ud800'):
            with self.subTest(output=repr(output)):
                correct = mock.Mock(return_value=output)
                for _ in range(2):
                    with self.assertRaises(core.BackendError):
                        self.check(correct)
                self.assertEqual(correct.call_count, 2)
                self.assertEqual(len(self.checker._cache), 0)

    def test_backend_errors_propagate_without_caching(self):
        correct = mock.Mock(side_effect=core.BackendError('synthetic failure'))
        for _ in range(2):
            with self.assertRaises(core.BackendError):
                self.check(correct)
        self.assertEqual(correct.call_count, 2)
        self.assertEqual(len(self.checker._cache), 0)

    def test_failure_in_later_fragment_does_not_return_partial_success(self):
        correct = mock.Mock(side_effect=['Kot ma dom.', core.BackendError('synthetic failure')])
        with self.assertRaises(core.BackendError):
            self.check(correct, 'Kot ma domm. Pies śpi.')
        self.assertEqual(correct.call_count, 2)
        # The successful first fragment can remain cached, but no document was returned.
        self.assertEqual(self.check(mock.Mock()).model_calls, 0)

    def test_the_live_check_leaves_only_an_unusable_answers_fragment_unchecked(self):
        # macOS ModelLive: an answer the code cannot use leaves its own sentence unchecked and the others go on; it is
        # not cached, so the next check asks for it again. A correction (no tolerate) still fails as a whole.
        correct = mock.Mock(side_effect=['', 'Pies śpi.'])
        result = self.checker.check('Kot ma domm. Pies spi.', correct, 'synthetic-identity', self.cancel, tolerate=True)
        self.assertEqual((result.rejected, [i.word for i in result.issues]), (1, ['spi']))
        self.assertEqual(result.corrected, 'Kot ma domm. Pies śpi.')
        correct = mock.Mock(return_value='Kot ma dom.')
        again = self.checker.check('Kot ma domm. Pies spi.', correct, 'synthetic-identity', self.cancel, tolerate=True)
        self.assertEqual((correct.call_count, again.rejected, again.cache_hits), (1, 0, 1))
        for kind in sorted(M.REJECTED):
            with self.subTest(kind=kind), self.assertRaises(core.BackendError):
                M.FragmentChecker().check('Kot ma domm.', mock.Mock(side_effect=core.BackendError('x', kind=kind)),
                                          'synthetic-identity', self.cancel)

    def test_a_server_error_ends_even_the_live_check(self):
        for kind in ('timeout', 'refused', 'http_500', 'model_error', 'bad_reply', 'interrupted'):
            correct = mock.Mock(side_effect=['Kot ma dom.', core.BackendError('x', kind=kind)])
            with self.subTest(kind=kind), self.assertRaises(core.BackendError):
                M.FragmentChecker().check('Kot ma domm. Pies spi.', correct, 'synthetic-identity', self.cancel,
                                          tolerate=True)

    def test_bad_diff_ranges_are_not_cached(self):
        bad = L.Issue(0, 999, 'domm', 'synthetic', ('dom',), 'Bielik', 'BIELIK')
        with mock.patch.object(L, 'bielik_issues', return_value=[bad]), self.assertRaises(core.BackendError):
            self.check(lambda text, cancel: 'Kot ma dom.')
        self.assertEqual(len(self.checker._cache), 0)

    def test_cancellation_before_call_and_on_cache_hit(self):
        correct = Corrector()
        self.check(correct)
        self.cancel.set()
        with self.assertRaises(core.Cancelled):
            self.check(correct)
        self.assertEqual(len(correct.calls), 1)
        with self.assertRaises(core.Cancelled):
            self.check(correct, 'Nowe zdanie.')
        self.assertEqual(len(correct.calls), 1)

    def test_cancel_during_model_call_does_not_cache_or_publish(self):
        def correct(text, cancel):
            cancel.set()
            return 'Kot ma dom.'
        with self.assertRaises(core.Cancelled):
            self.check(correct)
        self.assertEqual(len(self.checker._cache), 0)
        self.cancel.clear()
        self.assertEqual(self.check(Corrector()).model_calls, 1)

    def test_clear_from_another_thread_invalidates_inflight_write(self):
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def correct(text, cancel):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Synthetic worker did not resume')
            return 'Kot ma dom.'

        def work():
            try:
                outcomes.append(self.check(correct))
            except Exception as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.checker.clear()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], core.Cancelled)
        self.assertEqual(len(self.checker._cache), 0)
        self.assertEqual(self.check(Corrector()).model_calls, 1)

    def test_clear_during_segmentation_also_invalidates_the_check(self):
        split = M.split_fragments

        def clear_while_splitting(*args):
            result = split(*args)
            self.checker.clear()
            return result

        correct = Corrector()
        with mock.patch.object(M, 'split_fragments', side_effect=clear_while_splitting):
            with self.assertRaises(core.Cancelled):
                self.check(correct)
        self.assertEqual(correct.calls, [])
        self.assertEqual(len(self.checker._cache), 0)


class FragmentRequestTests(unittest.TestCase):
    def test_the_words_the_fragment_uses_come_before_it(self):
        server = mock.Mock()
        server.chat.return_value = 'Bielik pisze.'
        M.correct_fragment(server, 'Bielika pisze.', threading.Event(), words=['Orion', 'Bielik'])
        self.assertEqual(server.chat.call_args.args[0][1]['content'],
                         'Te nazwy i terminy pisz dokładnie tak: Bielik.\n<<<TEKST>>>\nBielika pisze.\n<<<KONIEC TEKSTU>>>')
        self.assertEqual(server.chat.call_args.args[0][0]['content'], M._PROMPT)

    def test_fixed_prompt_and_cancel_pass_through_backend_chat(self):
        cancel = threading.Event()
        server = mock.Mock()
        server.chat.return_value = 'Balon opadł, gdy ucichł wiatr.'
        text = 'Balon opadl gdy ucichł wiatr.'
        self.assertEqual(M.correct_fragment(server, text, cancel), server.chat.return_value)
        args, kwargs = server.chat.call_args
        self.assertEqual(args[1:], (0, text))
        self.assertIs(kwargs['cancel'], cancel)
        self.assertEqual(args[0][1]['content'], '<<<TEKST>>>\n' + text + '\n<<<KONIEC TEKSTU>>>')
        prompt = args[0][0]['content']
        self.assertIn('polskim, angielskim lub niemieckim', prompt)
        self.assertIn('Nie tłumacz', prompt)
        self.assertIn('Nie redaguj stylu', prompt)
        self.assertIn('Poprawny tekst przepisz identycznie', prompt)
        server.correct.assert_not_called()

    def test_changed_protected_token_foreign_script_and_comment_only_fail(self):
        server, cancel = mock.Mock(), threading.Event()
        for original, output in [('Link https://example.com/a', 'Link https://example.com/b'),
                                 ('Kot śpi.', 'Кот śpi.'), ('Kot śpi.', 'Tekst nie zawiera błędów.')]:
            with self.subTest(original=original):
                server.chat.return_value = output
                with self.assertRaises(core.BackendError):
                    M.correct_fragment(server, original, cancel)

    def test_no_change_emoji_and_author_commentary_are_valid(self):
        server, cancel = mock.Mock(), threading.Event()
        for text in ('🐢 Kot śpi.', 'Tekst nie zawiera błędów.', 'The cat is asleep.',
                     'Die Katze schläft.', 'Kot śpi. The cat sleeps. Die Katze schläft.'):
            with self.subTest(text=text):
                server.chat.return_value = text
                self.assertEqual(M.correct_fragment(server, text, cancel), text)

    def test_cancel_after_chat_and_invalid_input_make_no_retry(self):
        server, cancel = mock.Mock(), threading.Event()

        def answer(*args, **kwargs):
            cancel.set()
            return 'Kot śpi.'

        server.chat.side_effect = answer
        with self.assertRaises(core.Cancelled):
            M.correct_fragment(server, 'Kot śpi.', cancel)
        self.assertEqual(server.chat.call_count, 1)
        cancel.clear()
        with self.assertRaises(core.BackendError):
            M.correct_fragment(server, ' ', cancel)
        self.assertEqual(server.chat.call_count, 1)


if __name__ == '__main__':
    unittest.main()
