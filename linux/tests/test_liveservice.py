"""The live-check service (gramadrama/liveservice.py): the monitor's layers against the stub server, and
layer 0 with the real dictionaries."""
import dataclasses
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from gramadrama import core, live as L, liveservice as S, paths
from stub_server import Stub, sse, stream


class FakeChecker:
    """Layer 0 as a list per text; LanguageTool through the real code against the stub."""

    def __init__(self, quick=None):
        self.quick_issues = quick or {}
        self.checked = []
        real = S.Checker.__new__(S.Checker)
        real.pl = real.en = None
        real.suggestions = {}
        self.real = real

    def quick(self, text):
        self.checked.append(text.text)
        return self.quick_issues.get(text.text, [])

    def languagetool(self, text, server, off=()):
        return self.real.languagetool(text, server, off)

    def refresh(self, current):
        pass

    def learned(self, word):
        return False


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.stub = Stub().__enter__()
        self.addCleanup(self.stub.__exit__)
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.parent.mkdir()
        path.write_text(json.dumps(self.stub.settings()))
        self.events = []
        for patcher in (mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.config.name}),
                        mock.patch.object(S, 'TEST', True), mock.patch.object(S, 'LT_PAUSE', 0.2),
                        mock.patch.object(S, 'BIELIK_PAUSE', 0.4),
                        mock.patch.object(S, 'emit', lambda **e: self.events.append(e)),
                        mock.patch.object(S, 'real_geometry', self.no_atspi)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def no_atspi():
        raise RuntimeError('unit tests never read AT-SPI')

    def issues(self, timeout=3.0, where=lambda issues: True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [e for e in self.events if e['ev'] == 'issues']
            if found and where(found[-1]['issues']):
                return found[-1]
            time.sleep(0.02)
        self.fail('no issues matching in %s' % self.events)

    def test_layers_arrive_in_order_and_merge(self):
        text = 'Mysle ze to jest ok'
        quick = {text: [L.Issue(0, 5, 'Mysle', 'Nieznane słowo', ('Myślę',), 'słownik', 'SPELLING_PL')]}
        self.stub.lt_matches = [{'offset': 6, 'length': 2, 'message': 'Czy chodziło o „że”?', 'rule': {'id': 'ZE_ZE'},
                                 'replacements': [{'value': 'z'}]}]
        self.stub.chunks = stream('Myślę, że to jest OK')
        monitor = S.Monitor(FakeChecker(quick))
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(1, text, None)
        first = self.issues(where=lambda i: len(i) == 1)
        self.assertEqual(first['issues'][0]['word'], 'Mysle')
        second = self.issues(where=lambda i: any(x['source'] == 'LanguageTool' for x in i))
        lt = next(x for x in second['issues'] if x['source'] == 'LanguageTool')
        self.assertEqual(lt['suggestions'], ['że', 'z'])  # "że" first: the missing letter is far more likely
        form = next(body for path, body in self.stub.requests if path == '/v2/check')
        self.assertEqual((form['language'], form['disabledRules']), ('pl-PL', ','.join(L.DISABLED_RULES)))
        third = self.issues(where=lambda i: any(x['source'] == 'Bielik' for x in i))
        bielik = [x for x in third['issues'] if x['source'] == 'Bielik']
        self.assertEqual([x['word'] for x in bielik], ['ok'])  # what the faster layers did not find
        self.assertEqual(bielik[0]['kind'], 'style')
        logs = [e for e in self.events if e['ev'] == 'log']
        self.assertEqual(logs[-1]['event'], 'live_bielik')
        self.assertNotIn('Mysle', json.dumps(logs))

    def test_a_rule_turned_off_goes_at_once_and_is_not_asked_for_again(self):
        """Wyłącz regułę (the card's rule_off, for a LanguageTool grammar rule only): its issues go, rules.json keeps the
        rule with LanguageTool's description (never the text), and the next check asks the server not to check it."""
        text = 'Mysle ze to jest ok'
        quick = {text: [L.Issue(0, 5, 'Mysle', 'Nieznane słowo', ('Myślę',), 'słownik', 'SPELLING_PL')]}
        self.stub.lt_matches = [{'offset': 6, 'length': 2, 'message': 'Czy chodziło o „że”?', 'replacements': [],
                                 'rule': {'id': 'ZE_ZE', 'description': 'Że czy ze'}}]
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_bielik=False)))
        monitor = S.Monitor(FakeChecker(quick))
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(1, text, None)
        found = self.issues(where=lambda i: any(x['source'] == 'LanguageTool' for x in i))['issues']
        self.assertEqual({x['word']: x['rule_off'] for x in found}, {'Mysle': False, 'ze': True})
        monitor.turn_off('SPELLING_PL')  # not a LanguageTool grammar rule: nothing happens
        monitor.turn_off('ZE_ZE')
        self.assertEqual([x['word'] for x in self.issues(where=lambda i: len(i) == 1)['issues']], ['Mysle'])
        saved = json.loads(paths.rules_path().read_text())
        self.assertEqual(saved, {'off': {'ZE_ZE': 'Że czy ze'}})
        self.assertNotIn(text, paths.rules_path().read_text())
        self.stub.requests.clear()
        monitor.text_changed(1, text + ' tak', None)
        deadline = time.monotonic() + 3
        while not [b for p, b in self.stub.requests if p == '/v2/check'] and time.monotonic() < deadline:
            time.sleep(0.02)
        form = next(body for path, body in self.stub.requests if path == '/v2/check')
        self.assertEqual(form['disabledRules'].split(','), list(L.DISABLED_RULES) + ['ZE_ZE'])
        time.sleep(0.1)
        self.assertNotIn('ze', [x['word'] for x in self.events[-1].get('issues', [])])

    def test_english_sentences_go_to_languagetool_as_en_us(self):
        # Harper is gone (as on macOS since 2026-09-29): English grammar is layer 1's, the dictionary has the spelling.
        text = 'Me and Anna was working on this yesterday.'
        self.stub.lt_matches = [{'offset': 0, 'length': 11, 'message': 'Subject-verb agreement',
                                 'rule': {'id': 'AGREEMENT'}, 'replacements': [{'value': 'Anna and I'}]}]
        monitor = S.Monitor(FakeChecker())
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(1, text, None)
        found = self.issues(where=lambda i: any(x['source'] == 'LanguageTool' for x in i))
        self.assertEqual([x['word'] for x in found['issues']], ['Me and Anna'])
        form = next(body for path, body in self.stub.requests if path == '/v2/check')
        self.assertEqual(form['language'], 'en-US')
        self.assertIn('COMMA_COMPOUND_SENTENCE', form['disabledRules'].split(','))  # an optional comma is no mistake

    def test_the_panels_text_has_no_model_also_in_the_model_only_mode(self):
        """The result edited in the correction panel ("panel": true) came from the model: the dictionary and
        LanguageTool check it, the model never does, and no AT-SPI underlines are looked for."""
        text = 'Mysle ze to jest ok'
        quick = {text: [L.Issue(0, 5, 'Mysle', 'Nieznane słowo', ('Myślę',), 'słownik', 'SPELLING_PL')]}
        self.stub.lt_matches = [{'offset': 6, 'length': 2, 'message': 'Czy chodziło o „że”?', 'rule': {'id': 'ZE_ZE'},
                                 'replacements': [{'value': 'że'}]}]
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_model_only=True)))
        monitor = S.Monitor(FakeChecker(quick))
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(7, text, None, panel=True)
        found = self.issues(where=lambda i: {x['source'] for x in i} == {'słownik', 'LanguageTool'})
        self.assertEqual([x['word'] for x in found['issues']], ['Mysle', 'ze'])
        time.sleep(0.6)  # past the model's pause
        self.assertFalse([path for path, _ in self.stub.requests if path in ('/api/chat', '/v1/chat/completions')])
        self.assertFalse([e for e in self.events if e['ev'] == 'marks' and e.get('view')])

    def test_the_word_being_typed_is_hidden(self):
        text = 'napisz tu cos'
        quick = {text: [L.Issue(10, 13, 'cos', 'Brak polskiej litery?', ('coś',), 'słownik', 'PL_DIACRITICS')]}
        monitor = S.Monitor(FakeChecker(quick))
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(1, text, 13)  # the caret right after "cos": still being typed
        time.sleep(0.15)
        self.assertEqual([e['issues'] for e in self.events if e['ev'] == 'issues'][-1:], [[]])
        monitor.text_changed(1, text, 0)  # the caret moved away: shown
        self.assertEqual(self.issues(where=bool)['issues'][0]['word'], 'cos')

    def test_edits_shift_known_issues_and_fixes_remove_them(self):
        old, new = 'ala ma kota ktory', 'Ola ala ma kota ktory'
        quick = {old: [L.Issue(12, 17, 'ktory', '', ('który',), 'słownik', 'SPELLING_PL')]}
        checker = FakeChecker(quick)
        monitor = S.Monitor(checker)
        with mock.patch.object(S.Monitor, 'run', lambda self: None):
            monitor.text_changed(1, old, None)
            monitor.quick = quick[old]
            monitor.text_changed(1, new, None)
        self.assertEqual([(i.start, i.end) for i in monitor.quick], [(16, 21)])  # moved by the 4 characters typed before it
        monitor.fixed(1, 16, 21)
        self.assertEqual(monitor.quick, [])
        monitor.text_changed(2, 'inne pole', None)
        self.assertEqual(monitor.quick, [])  # another field: nothing carried over
        monitor.clear()
        self.assertEqual(self.events[-1], {'ev': 'issues', 'field': None, 'issues': [], 'checking': False})

    def test_a_bad_answer_or_a_bug_never_ends_a_layer(self):
        """A 200 that is not LanguageTool's JSON (a captive portal), then an exception in the layer itself:
        the next text is checked all the same."""
        text = 'Mysle ze'
        backend = core.Backend(core.Settings(**self.stub.settings()))
        for body in (b'<html>captive portal</html>', b'{"matches": 3}', b'[]'):
            with self.subTest(body=body):
                self.stub.lt_body = body
                with self.assertRaises(core.BackendError):
                    backend.check_live(text, 'pl-PL')
        self.stub.lt_body = json.dumps({'matches': ['x', {'offset': 0, 'length': 5, 'message': 'm'}]}).encode()
        self.assertEqual(len(backend.check_live(text, 'pl-PL')), 1)  # only the matches that are objects
        self.stub.lt_body = b'<html>captive portal</html>'
        checker = FakeChecker()
        monitor = S.Monitor(checker)
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_bielik=False)))
        monitor.run()
        monitor.text_changed(1, text, None)
        deadline = time.monotonic() + 3
        while not self.stub.requests and time.monotonic() < deadline:
            time.sleep(0.02)
        self.stub.lt_body = None
        self.stub.lt_matches = [{'offset': 6, 'length': 2, 'message': 'm', 'rule': {'id': 'R'}, 'replacements': []}]
        with mock.patch.object(checker, 'languagetool', side_effect=RuntimeError('a bug')):
            monitor.text_changed(1, text + ' to', None)
            deadline = time.monotonic() + 3
            while not any(e.get('event') == 'live_layer_failed' for e in self.events) and time.monotonic() < deadline:
                time.sleep(0.02)
        monitor.text_changed(1, text + ' tak', None)
        self.assertEqual(self.issues(where=bool)['issues'][0]['source'], 'LanguageTool')
        self.assertEqual([e for e in self.events if e.get('event') == 'live_layer_failed'],
                         [{'ev': 'log', 'event': 'live_layer_failed', 'layer': 1}])

    def marks(self, timeout=3.0, where=lambda view: True):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [e for e in self.events if e['ev'] == 'marks']
            if found and where(found[-1]['view']):
                return found[-1]
            time.sleep(0.02)
        self.fail('no marks matching in %s' % [e for e in self.events if e['ev'] == 'marks'])

    def test_underlines_ignore_and_learn(self):
        """The marker draws the visible issues (a fake AT-SPI); "Ignoruj" and "Dodaj do słownika" in the
        card take theirs away."""
        text = 'Mysle ze to'
        quick = {text: [L.Issue(0, 5, 'Mysle', 'Nieznane słowo', ('Myślę',), 'słownik', 'SPELLING_PL'),
                        L.Issue(6, 8, 'ze', 'Brak polskiej litery?', ('że',), 'słownik', 'PL_DIACRITICS')]}
        asked = []

        class FakeGeometry:
            def marks(self, sent, caret, issues):
                asked.append((sent, caret, [i['id'] for i in issues]))
                return {'toolkit': ['GTK', '4.20.1'], 'caret': [100, 40, 0, 18], 'clip': [0, 30, 700, 300],
                        'marks': [{'id': i['id'], 'rects': [[12 + 8 * i['start'], 40, 8 * (i['end'] - i['start']), 18]]}
                                  for i in issues]}
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_languagetool=False, live_bielik=False)))
        monitor = S.Monitor(FakeChecker(quick), geometry=FakeGeometry)
        self.addCleanup(monitor.stop)  # before the patches go: never the real server
        monitor.run()
        monitor.text_changed(1, text, None)
        event = self.marks(where=lambda view: view and len(view['marks']) == 2)
        self.assertEqual((event['field'], event['caret'], event['length']), (1, None, len(text)))
        first = event['view']['marks'][0]
        self.assertEqual(first, {'id': '0:5:SPELLING_PL', 'rects': [[12, 40, 40, 18]], 'word': 'Mysle',
                                 'message': 'Nieznane słowo', 'suggestions': ['Myślę'], 'kind': 'spelling',
                                 'source': 'słownik', 'rule_off': False, 'learn': True})
        self.assertEqual(asked[-1][:2], (text, None))
        monitor.ignore('MYSLE', 'SPELLING_PL')  # the key is the word in lower case, as on macOS
        self.assertEqual([i['word'] for i in self.issues(where=lambda i: len(i) == 1)['issues']], ['ze'])
        self.marks(where=lambda view: view and [m['word'] for m in view['marks']] == ['ze'])
        monitor.learned('ze')
        self.issues(where=lambda i: not i)
        self.marks(where=lambda view: view is None)  # nothing to draw: the extension clears the lines
        monitor.text_changed(1, text + ' tak', None)  # typed on: "Mysle" stays ignored
        time.sleep(0.3)
        self.assertFalse([i for i in self.events[-1].get('issues', []) if i['word'] == 'Mysle'])

    def test_no_underlines_when_switched_off_or_without_atspi(self):
        text = 'Mysle ze to'
        quick = {text: [L.Issue(0, 5, 'Mysle', 'Nieznane słowo', ('Myślę',), 'słownik', 'SPELLING_PL')]}
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_underlines=False, live_languagetool=False,
                                        live_bielik=False)))
        called = []
        monitor = S.Monitor(FakeChecker(quick), geometry=lambda: called.append(1))
        self.addCleanup(monitor.stop)
        monitor.run()
        monitor.text_changed(1, text, None)
        self.issues(where=bool)
        time.sleep(0.5)
        self.assertEqual(called, [])  # switched off: AT-SPI is not even opened
        self.assertTrue(all(e['view'] is None for e in self.events if e['ev'] == 'marks'))
        monitor = S.Monitor(FakeChecker(quick))  # the default in unit tests: no AT-SPI at all
        self.assertIsNone(monitor._marks(text, None, [{'id': 'x', 'start': 0, 'end': 5}]))
        self.assertIs(monitor.geometry, False)
        self.assertEqual(self.events[-1], {'ev': 'log', 'event': 'live_marks_unavailable'})  # once, not a failure

    def test_the_caret_for_a_hotkey(self):
        """The engine asks where the caret is now (a GTK4 click it could not trust): the marker thread reads
        AT-SPI at once, with the live check off too, and answers null when it cannot tell."""
        asked = []

        class FakeGeometry:
            def caret(self, sent):
                asked.append(sent)
                return (3, 7) if sent == 'Ala ma kota' else None
        monitor = S.Monitor(FakeChecker({}), geometry=FakeGeometry)
        self.addCleanup(monitor.stop)
        monitor.run()
        started = time.monotonic()
        monitor.ask_caret(5, 'Ala ma kota')
        monitor.ask_caret(6, 'Inny')
        deadline = time.monotonic() + 2
        while len([e for e in self.events if e['ev'] == 'caret']) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertLess(time.monotonic() - started, S.MARKS_INTERVAL)  # never after a round of the marker
        self.assertEqual([e for e in self.events if e['ev'] == 'caret'],
                         [{'ev': 'caret', 'id': 5, 'cursor': 3, 'anchor': 7},
                          {'ev': 'caret', 'id': 6, 'cursor': None, 'anchor': None}])
        self.assertEqual(asked, ['Ala ma kota', 'Inny'])
        monitor = S.Monitor(FakeChecker({}))  # no AT-SPI: an answer all the same
        monitor._caret(7, 'Ala')
        self.assertEqual(self.events[-1], {'ev': 'caret', 'id': 7, 'cursor': None, 'anchor': None})

    def test_a_layer_whose_server_is_not_set_asks_nobody(self):
        # no default servers: a new install has no LanguageTool and no model until the user gives their addresses
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        text = 'Mysle ze to dziala'
        for fields, layer in (({'llm_url': self.stub.url, 'llm_api': 'ollama', 'model': 'm'}, '_layer1'),
                              ({'languagetool_url': self.stub.url}, '_layer2'), ({}, '_layer1'), ({}, '_layer2')):
            with self.subTest(fields=fields, layer=layer):
                path.write_text(json.dumps(fields))
                monitor = S.Monitor(FakeChecker())
                getattr(monitor, layer)(monitor.version, 1, text)
                self.assertEqual(self.stub.requests, [])
        # LanguageTool alone is enough for layer 1, and the model alone for layer 2
        path.write_text(json.dumps({'languagetool_url': self.stub.url}))
        monitor = S.Monitor(FakeChecker())
        monitor._layer1(monitor.version, 1, text)
        self.assertEqual([p for p, _ in self.stub.requests], ['/v2/check'])
        self.stub.requests.clear()
        self.stub.events = sse('Mysle ze to działa')
        path.write_text(json.dumps({'llm_url': self.stub.url, 'llm_api': 'openai', 'model': 'm'}))
        monitor = S.Monitor(FakeChecker())
        monitor._layer2(monitor.version, 1, text)
        self.assertEqual([p for p, _ in self.stub.requests], ['/v1/chat/completions'])

    def test_bielik_caches_and_backs_off(self):
        text = 'Testuje to'
        self.stub.chunks = stream('Testuję to')
        monitor = S.Monitor(FakeChecker())
        monitor._layer2(monitor.version, 1, text)  # not the current text: computed and cached, not shown
        self.assertEqual(len(self.stub.chats()), 1)
        monitor._layer2(monitor.version, 1, text)
        self.assertEqual(len(self.stub.chats()), 1)  # from the cache
        self.stub.chat_status = 500
        monitor._layer2(monitor.version, 1, 'Inny tekst')
        self.assertEqual(self.events[-1], {'ev': 'log', 'event': 'live_bielik_failed', 'chars': 10, 'why': 'http_500'})
        self.stub.chat_status = 200
        monitor._layer2(monitor.version, 1, 'Jeszcze inny')
        self.assertEqual(len(self.stub.chats()), 2)  # the minute's back-off: not asked

    def test_switches_in_settings(self):
        path = pathlib.Path(self.config.name, 'gramadrama', 'settings.json')
        path.write_text(json.dumps(dict(self.stub.settings(), live_languagetool=False, live_bielik=False)))
        monitor = S.Monitor(FakeChecker())
        monitor._layer1(monitor.version, 1, 'Mysle ze')
        monitor._layer2(monitor.version, 1, 'Mysle ze')
        self.assertEqual(self.stub.requests, [])


@unittest.skipUnless(shutil.which('enchant-2') or os.path.exists('/usr/share/hunspell/pl_PL.dic'), 'no Polish dictionary')
class RealLayerZero(unittest.TestCase):
    """Layer 0 with Enchant (pl_PL, en_US)."""

    @classmethod
    def setUpClass(cls):
        cls.checker = S.Checker()

    def found(self, text):
        return {(i.word, i.suggestions[0] if i.suggestions else '') for i in self.checker.quick(L.LiveText(text))}

    def test_polish(self):
        found = self.found('Mysle ze dzieki tez to zrobie, Anna wysle PR na staging.')
        for pair in (('Mysle', 'Myślę'), ('ze', 'że'), ('dzieki', 'dzięki'), ('tez', 'też'), ('zrobie', 'zrobię')):
            self.assertIn(pair, found)
        words = {w for w, _ in found}
        self.assertFalse(words & {'Anna', 'PR', 'staging'}, found)  # a name, an abbreviation, an English word

    @unittest.skipUnless(os.path.exists('/usr/share/hunspell/en_US.dic'), 'no English dictionary')
    def test_english_spelling_comes_from_the_dictionary(self):
        issues = {issue.word: issue for issue in self.checker.quick(L.LiveText('I has a eror in teh code.'))}
        self.assertEqual(set(issues), {'teh', 'eror'})  # grammar ("has") belongs to LanguageTool
        for word, correction in (('teh', 'the'), ('eror', 'error')):
            # System dictionary versions rank valid alternatives differently. The intended
            # correction must still be offered among the live check's three suggestions.
            self.assertIn(correction, issues[word].suggestions)
            self.assertEqual((issues[word].source, issues[word].rule), ('słownik', 'SPELLING_EN'))


class LanguageProfileTests(unittest.TestCase):
    def test_german_dictionary_checks_capitalised_nouns_and_inactive_dictionary_is_not_used(self):
        class Dictionary:
            def __init__(self, tag, words):
                self.tag, self.words, self.checked = tag, words, []

            def check(self, word):
                self.checked.append(word)
                return word in self.words

            def suggest(self, word):
                return ['Datei']

            def learned(self, word):
                return False
        german = Dictionary('de_DE', {'Ich', 'schicke', 'die'})
        polish = Dictionary('pl_PL', set())
        checker = S.Checker({'de_DE': german, 'pl_PL': polish})
        issues = checker.quick(L.LiveText('Ich schicke die Dateii.', ('de',)))
        self.assertEqual([(item.word, item.rule, item.suggestions) for item in issues],
                         [('Dateii', 'SPELLING_DE', ('Datei',))])
        self.assertFalse(polish.checked)

    def test_languagetool_checks_both_active_parts_with_their_original_offsets(self):
        checker = S.Checker({})
        server = mock.Mock()
        server.check_live.return_value = []
        text = 'Das ist eine gute Idee. I will check it tomorrow.'
        checker.languagetool(L.LiveText(text, ('de', 'en')), server)
        calls = server.check_live.call_args_list
        self.assertEqual([call.args[1] for call in calls], ['de-DE', 'en-US'])
        self.assertEqual([len(call.args[0]) for call in calls], [len(text), len(text)])

    def test_languagetool_leaves_out_the_rules_turned_off_and_keeps_the_description(self):
        checker = S.Checker({})
        server = mock.Mock()
        server.check_live.return_value = [
            {'offset': 3, 'length': 2, 'message': 'm', 'rule': {'id': 'OFF', 'description': 'Off'}},
            {'offset': 6, 'length': 2, 'message': 'm', 'rule': {'id': 'ON', 'description': 'Opis reguły'}}]
        issues = checker.languagetool(L.LiveText('Ala ma kota.', ('pl',)), server, ('OFF', 'WHITESPACE_RULE'))
        self.assertEqual(server.check_live.call_args.args[2], L.DISABLED_RULES + ('OFF',))
        self.assertEqual([(i.rule, i.description, i.can_turn_off) for i in issues], [('ON', 'Opis reguły', True)])
        spelling = L.Issue(0, 1, 'x', '', (), 'LanguageTool', 'MORFOLOGIK_RULE_PL_PL')
        self.assertFalse(spelling.can_turn_off)  # a word for the dictionary, not a rule to turn off
        self.assertFalse(L.Issue(0, 1, 'x', '', (), 'Bielik', 'BIELIK').can_turn_off)

    def test_the_users_words_pass_the_spelling(self):
        """The own words of the settings, in the forms Enchant accepts for its own lists, are no mistakes."""
        class Dictionary:
            tag = 'pl_PL'

            def check(self, word):
                return word in {'Piszę', 'do'}

            def suggest(self, word):
                return []

            def learned(self, word):
                return False
        checker = S.Checker({'pl_PL': Dictionary()})
        with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': config}):
            checker.refresh(core.Settings(dictation_vocabulary='GramaDrama, kubernetes', secondary_language=''))
        issues = checker.quick(L.LiveText('Piszę do GramaDrama Kubernetes GRAMADRAMA gramadrama.', ('pl',)))
        self.assertEqual([i.word for i in issues], ['gramadrama'])

    def test_a_personal_list_changed_elsewhere_opens_the_dictionary_again(self):
        """Enchant reads its personal list only when the dictionary is opened: a word removed in Pisanie (or added in a
        GNOME app) reopens it; a word this checker adds itself does not."""
        opened = []

        class Speller:
            def __init__(self, tag):
                self.tag = tag
                opened.append(tag)

            def add(self, word):
                with open(os.path.join(os.environ['XDG_CONFIG_HOME'], 'enchant', self.tag + '.dic'), 'a') as f:
                    f.write(word + '\n')
        with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': config}):
            os.mkdir(os.path.join(config, 'enchant'))
            with mock.patch('gramadrama.spell.Speller', Speller):
                checker = S.Checker({'pl_PL': Speller('pl_PL')})
                settings = core.Settings(secondary_language='')
                checker.refresh(settings)
                checker.learn('Bielik', 'pl')  # its own change: known already
                checker.refresh(settings)
                self.assertEqual(opened, ['pl_PL'])
                path = pathlib.Path(config, 'enchant', 'pl_PL.dic')
                path.write_text('Orion\n')  # removed in Pisanie, another one there
                os.utime(path, ns=(1, 1))
                checker.refresh(settings)
                self.assertEqual(opened, ['pl_PL', 'pl_PL'])
                self.assertIsNot(checker.pl, None)
                self.assertIn('Orion', checker.known)

    def test_single_english_does_not_restore_polish_letters(self):
        checker = S.Checker({})
        self.assertEqual(checker.quick(L.LiveText('ze to jest', ('en',))), [])

    def test_switching_languages_clears_issues_and_cancels_pending_work(self):
        with mock.patch.object(S, 'settings', return_value=core.Settings()), mock.patch.object(S, 'emit'):
            monitor = S.Monitor(S.Checker({}))
            monitor.text_changed(1, 'Test', None)
            monitor.quick = [L.Issue(0, 4, 'Test', '', ('test',), 'słownik', 'SPELLING_PL')]
            monitor.cache[('pl', 'en'), 'Test'] = monitor.quick
            monitor.bielik_cancel = threading.Event()
            before = monitor.version
            monitor.refresh_languages(('de', 'en'))
        self.assertEqual(monitor.languages, ('de', 'en'))
        self.assertEqual(monitor.version, before + 1)
        self.assertTrue(monitor.pending and monitor.bielik_cancel.is_set())
        self.assertEqual((monitor.quick, monitor.lt, monitor.bielik, monitor.cache), ([], [], [], {}))

    def test_model_cache_is_per_language_profile_and_english_model_issues_are_kept(self):
        selected = [core.Settings(llm_url='http://127.0.0.1:11434', model='test-model',
                                  primary_language='en', secondary_language='')]
        server = mock.Mock()
        server.correct.return_value = 'This is a test.'
        with mock.patch.object(S, 'settings', side_effect=lambda: selected[0]), \
                mock.patch.object(S, 'backend', return_value=server), mock.patch.object(S, 'emit'):
            monitor = S.Monitor(S.Checker({}))
            monitor._layer2(monitor.version, 1, 'This are a test.')
            self.assertTrue(monitor.bielik)
            monitor._layer2(monitor.version, 1, 'This are a test.')
            self.assertEqual(server.correct.call_count, 1)
            selected[0] = dataclasses.replace(selected[0], primary_language='de', secondary_language='en')
            monitor._layer2(monitor.version, 1, 'This are a test.')
            self.assertEqual(server.correct.call_count, 2)

    def test_result_of_old_language_profile_is_discarded_after_settings_change(self):
        for layer in ('model', 'languagetool'):
            selected = [core.Settings(llm_url='http://127.0.0.1:11434', model='test-model',
                                      languagetool_url='http://127.0.0.1:8010')]
            server = mock.Mock()
            checker = mock.Mock()

            def switch(*args, **kwargs):
                selected[0] = dataclasses.replace(selected[0], primary_language='de', secondary_language='en')
                return 'Test.' if layer == 'model' else [L.Issue(0, 4, 'Test', '', (), 'LanguageTool', 'TEST')]
            server.correct.side_effect = switch
            checker.languagetool.side_effect = switch
            with self.subTest(layer=layer), mock.patch.object(S, 'settings', side_effect=lambda: selected[0]), \
                    mock.patch.object(S, 'backend', return_value=server), mock.patch.object(S, 'emit'):
                monitor = S.Monitor(checker)
                method = monitor._layer2 if layer == 'model' else monitor._layer1
                method(monitor.version, 1, 'Test')
                self.assertEqual((monitor.lt, monitor.bielik, monitor.cache), ([], [], {}))


if __name__ == '__main__':
    unittest.main()


class DictionaryRetryTests(unittest.TestCase):
    """A dictionary installed after the service started (hunspell-pl on an English system) is opened later, no restart."""

    def test_a_dictionary_installed_later(self):
        installed = {'en_US'}

        class Speller:
            def __init__(self, tag):
                if tag not in installed:
                    raise LookupError(tag)
                self.tag = tag
        with mock.patch('gramadrama.spell.Speller', Speller):
            checker = S.Checker()
            self.assertIsNone(checker.pl)
            checker.retry_missing(now=100.0)
            self.assertIsNone(checker.pl)
            installed.add('pl_PL')
            checker.retry_missing(now=100.0 + S.DICTIONARY_RETRY / 2)  # not so often
            self.assertIsNone(checker.pl)
            checker.retry_missing(now=100.0 + S.DICTIONARY_RETRY + 1)
            self.assertEqual(checker.pl.tag, 'pl_PL')
            self.assertEqual(checker.en.tag, 'en_US')
        given = S.Checker({tag: None for tag in core.DICTIONARY_CODES.values()})
        given.retry_missing(now=10 ** 9)  # spellers given by a test stay as they are
        self.assertIsNone(given.pl)

