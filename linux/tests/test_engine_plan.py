import ast
import json
import pathlib
import unittest
from gramadrama import core
from gramadrama.engine import plan
from gramadrama.engine.context import Refusal, Snapshot

REPO = pathlib.Path(__file__).resolve().parents[2]  # tests/ime_cases.json is written by the Mac

P1 = 'Pierwszy akapit: zażółć gęślą jaźń.'
P2 = 'Drugi akapit ma błąd 😀 i słowo KOT w środku zdania.'
P3 = 'Trzeci akapit — koniec.'
TA = '\n'.join((P1, P2, P3))
CE = 'Pierwszy akapit edytora: zażółć gęślą jaźń.\n\n' + P2 + '\n\n' + P3
LONG = '\n'.join('Akapit %d: zażółć gęślą jaźń 😀 — to jest dość długi akapit testowy numer %d.' % (i, i)
                 for i in range(60))  # fixture.html long textarea


def chrome_window(full, cursor, anchor):
    """Chromium 154 TrimSurroundingTextForStandard: 4000 UTF-8 bytes centred on the selection."""
    data = full.encode()
    cb, ab = len(full[:cursor].encode()), len(full[:anchor].encode())
    lo, hi = min(cb, ab), max(cb, ab)
    if len(data) <= 4000:
        return full, cursor, anchor
    centre = lo + (hi - lo) // 2
    start = 0 if centre <= 2000 else len(data) - 4000 if len(data) - centre < 2000 else centre - 2000
    end = start + 4000
    while start < len(data) and data[start] & 0xc0 == 0x80:
        start += 1
    while end < len(data) and data[end] & 0xc0 == 0x80:
        end -= 1
    shift = len(data[:start].decode())
    return data[start:end].decode(), cursor - shift, anchor - shift


def snap(text, cursor, anchor=None, hidden=False, client='gnome-shell', unknown=False):
    return Snapshot(text, cursor, cursor if anchor is None else anchor, client, anchor is not None, hidden, unknown)


def simulate(text, cursor, anchor, ops):
    """Apply ops the way text-input-v3 clients do: delete around the caret, then commit, which
    replaces the selection."""
    if ops.delete:
        offset, n = ops.delete
        assert offset <= 0 <= offset + n, 'mutter refuses a delete range without the cursor'
        text, cursor = text[:cursor + offset] + text[cursor + offset + n:], cursor + offset
        anchor = cursor
    lo, hi = sorted((cursor, anchor))
    commit = ops.commit or ''
    return text[:lo] + commit + text[hi:], lo + len(commit)


class ModuleTests(unittest.TestCase):
    def test_max_chars_matches_core_without_importing_it(self):
        self.assertEqual(plan.MAX_CHARS, core.MAX_CHARS)
        tree = ast.parse(pathlib.Path(plan.__file__).read_text())
        names = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                 for alias in node.names} | {node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse({'core', 'gi'} & names, names)


class CaptureTests(unittest.TestCase):
    def test_table(self):
        kot_ta, kot_ce = TA.index('KOT'), CE.index('KOT')
        gap = CE.index('\n\n') + 1
        cases = [
            # (name, snapshot, (scope, source) or refusal reason)
            ('gtk4 selection, emoji counts once', snap(P2, 34, 31), ('selection', 'KOT')),
            ('chrome textarea selection', snap(TA, 70, 67), ('selection', 'KOT')),
            ('backwards selection', snap(TA, kot_ta, kot_ta + 3), ('selection', 'KOT')),
            ('chrome contenteditable selection', snap(CE, 79, 76), ('selection', 'KOT')),
            ('selection wins over hidden flag', snap(P2, 34, 31, hidden=True), ('selection', 'KOT')),
            ('caret paragraph', snap(TA, kot_ta), ('paragraph', P2)),
            ('caret at paragraph start', snap(TA, len(P1) + 1), ('paragraph', P2)),
            ('caret at paragraph end', snap(TA, len(P1)), ('paragraph', P1)),
            ('contenteditable block', snap(CE, kot_ce), ('paragraph', P2)),
            ('empty line between blocks', snap(CE, gap), 'empty'),
            ('whitespace-only paragraph', snap('abc\n  \t\ndef', 5), 'empty'),
            ('whitespace-only selection', snap('abc   def', 4, 6), 'empty'),
            ('hidden selection (GTK3)', snap(P2, 34, hidden=True), 'hidden_selection'),
            ('selection unknown after a focus change', snap(P2, 34, unknown=True), 'selection_unknown'),
            ('reported selection despite the unknown flag', snap(P2, 34, 31, unknown=True), ('selection', 'KOT')),
            ('too long', snap('ab ' * 2001, 5), 'too_long'),
            ('embedded object', snap('Zdjęcie ￼ tutaj', 3), 'embedded'),
            ('refusal passes through', Refusal('stale'), 'stale'),
        ]
        for name, snapshot, want in cases:
            with self.subTest(name):
                got = plan.capture(snapshot)
                self.assertEqual(got.reason if isinstance(got, Refusal) else (got.scope, got.source), want)
                if not isinstance(got, Refusal):
                    self.assertEqual(got.text[got.lo:got.hi], got.source)

    def test_offsets_are_code_points_not_utf16(self):
        got = plan.capture(snap(TA, 70, 67))
        self.assertEqual((got.lo, got.hi), (67, 70))
        self.assertEqual(len(TA[:67].encode('utf-16-le')) // 2, 68)  # the page's own offsets

    def test_chrome_4000_byte_window_edges(self):
        text, cursor, _ = chrome_window(LONG, LONG.index('Akapit 30:') + 20, LONG.index('Akapit 30:') + 20)
        self.assertEqual((len(text), len(text.encode()), cursor), (3272, 4000, 1638))  # as logged in run3
        self.assertTrue(text.startswith('8.\nAkapit 9:'))
        first_full = text.index('Akapit 9:')
        last_nl = text.rindex('\n')
        cases = [
            ('caret paragraph inside the window', snap(text, cursor), ('paragraph', 'Akapit 30')),
            ('caret in the cut first paragraph', snap(text, 1), 'truncated'),
            ('caret in the cut last paragraph', snap(text, last_nl + 3), 'truncated'),
            ('caret in the first full paragraph', snap(text, first_full + 3), ('paragraph', 'Akapit 9:')),
            ('selection touching the window start', snap(text, 0, 2), 'truncated'),
            ('selection touching the window end', snap(text, len(text) - 2, len(text)), 'truncated'),
            ('selection inside the window', snap(text, cursor, cursor + 5), ('selection', None)),
            ('short text may touch its edges', snap(P2, 3), ('paragraph', 'Drugi')),
            ('short selection at index 0', snap(TA, 0, 8), ('selection', 'Pierwszy')),
        ]
        for name, snapshot, want in cases:
            with self.subTest(name):
                got = plan.capture(snapshot)
                if isinstance(want, str):
                    self.assertEqual(getattr(got, 'reason', got), want)
                else:
                    self.assertEqual(got.scope, want[0])
                    self.assertTrue(want[1] is None or got.source.startswith(want[1]), got.source[:20])


class ReplaceOpsTests(unittest.TestCase):
    def ops(self, text, cursor, corrected, anchor=None):
        capture = plan.capture(snap(text, cursor, anchor))
        return capture, plan.replace_ops(capture, snap(text, cursor, anchor), corrected)

    def test_apply_time_revalidation(self):
        capture = plan.capture(snap(TA, 60))
        cases = [
            ('field refused now', Refusal('stale'), 'stale'),
            ('text changed', snap(TA.replace('KOT', 'PSA'), 60), 'changed'),
            ('text outside the paragraph changed', snap(TA + '!', 60), 'changed'),
            ('cursor moved', snap(TA, 61), 'changed'),
            ('selection appeared', snap(TA, 60, 58), 'changed'),
            ('other client', snap(TA, 60, client='gtk4-im:x'), 'changed'),
        ]
        for name, now, want in cases:
            with self.subTest(name):
                self.assertEqual(plan.replace_ops(capture, now, P2.upper()), Refusal(want))

    def test_nothing_and_bad_results(self):
        capture = plan.capture(snap(TA, 60))
        self.assertTrue(plan.replace_ops(capture, snap(TA, 60), P2).nothing)
        # a lone surrogate (e.g. from surrogateescape) cannot become IBus.Text: never delete for it
        for bad in ('', '  \n', 'a￼b', 'a\x00', P2 + '\udcff', '\ud83d' + P2):
            with self.subTest(bad=bad):
                self.assertEqual(plan.replace_ops(capture, snap(TA, 60), bad), Refusal('bad_result'))

    def test_selection_is_commit_only(self):
        for corrected in ('kot', 'KOTY', 'K', 'Kot 🐈'):
            with self.subTest(corrected=corrected):
                capture, ops = self.ops(TA, 70, corrected, anchor=67)
                self.assertIsNone(ops.delete)
                self.assertEqual(ops.commit, corrected)
                self.assertEqual(simulate(TA, 70, 67, ops)[0], TA.replace('KOT', corrected))

    def test_minimal_span_contains_the_cursor(self):
        text = 'A\nAla ma kota.\nB'
        lo = 2
        cases = [
            # (name, cursor, corrected paragraph, expected (delete, commit))
            ('cursor before the change', lo, 'Ala ma psa.', ((0, 10), 'Ala ma ps')),  # 'kot' -> 'ps'
            ('cursor inside the change', lo + 9, 'Ala ma psa.', ((-2, 3), 'ps')),
            ('cursor right after the change', lo + 11, 'Ala ma psa.', ((-4, 4), 'psa')),
            ('cursor at paragraph end', lo + 12, 'Ala ma psa.', ((-5, 5), 'psa.')),
            ('insertion at the cursor', lo + 3, 'Ala, ma kota.', (None, ',')),
            ('insertion after the cursor', lo, 'Ala ma kota!.', ((0, 11), 'Ala ma kota!')),
            ('deletion only', lo + 12, 'Ala ma.', ((-6, 6), '.')),
            ('deletion ending at the cursor', lo + 11, 'Ala ma.', ((-5, 5), None)),
        ]
        for name, cursor, corrected, want in cases:
            with self.subTest(name):
                capture, ops = self.ops(text, cursor, corrected)
                self.assertEqual((ops.delete, ops.commit), want)
                self.assertEqual(simulate(text, cursor, cursor, ops)[0], text.replace('Ala ma kota.', corrected))

    def test_minimal_span_never_splits_graphemes(self):
        family, family2 = '👨‍👩‍👧', '👨‍👩‍👦'
        cases = [
            # (name, source, corrected, replaced source part)
            ('combining mark', 'Café ok', 'Cafè ok', 'é'),
            ('combining mark removed', 'née', 'nee', 'é'),
            ('two combining marks', 'é̂x', 'êx', 'é̂'),
            ('zwj family', family + ' hej', family2 + ' hej', family),
            ('skin tone', '👍🏻 ok', '👍🏽 ok', '👍🏻'),
            ('variation selector', '❤️ ok', '❤ ok', '❤️'),
            ('second flag', '🇵🇱🇩🇪', '🇵🇱🇫🇷', '🇩🇪'),
            ('first flag', '🇵🇱🇩🇪', '🇵🇸🇩🇪', '🇵🇱'),
            ('keycap', '1️⃣ i', '2️⃣ i', '1'),
            ('tag sequence', '🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f!',
             '🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f!',
             '🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f'),
        ]
        for name, source, corrected, part in cases:
            text = 'X\n' + source + '\nY'
            for cursor in range(2, 2 + len(source) + 1):
                if not plan.is_boundary(text, cursor):
                    continue  # clients keep the caret between graphemes
                with self.subTest(name, cursor=cursor):
                    capture, ops = self.ops(text, cursor, corrected)
                    self.assertEqual(simulate(text, cursor, cursor, ops)[0], 'X\n' + corrected + '\nY')
                    self.assertTrue(plan.is_boundary(text, ops.start) and plan.is_boundary(text, ops.end))
                    self.assertIn(part, text[ops.start:ops.end])

    def test_invariants_for_every_cursor(self):
        pairs = [(P2, P2.replace('błąd', 'błędy')), (P2, P2.upper()), (P2, 'x'), ('a b c', 'a  b c'),
                 ('zażółć', 'zazolc'), ('abc', 'abcabc'), ('abcabc', 'abc'), ('😀😀', '😀'), ('aa', 'aaa')]
        for source, corrected in pairs:
            text = P1 + '\n' + source + '\n\n' + P3
            lo = len(P1) + 1
            for cursor in range(lo, lo + len(source) + 1):
                with self.subTest(source=source, corrected=corrected, cursor=cursor):
                    capture, ops = self.ops(text, cursor, corrected)
                    new, new_cursor = simulate(text, cursor, cursor, ops)
                    self.assertEqual(new, text[:lo] + corrected + text[lo + len(source):])
                    want = plan.expected(capture, ops)
                    self.assertEqual((want.text, want.cursor), (new, new_cursor))
                    self.assertEqual(plan.verify(want, new, new_cursor), 'ok')
                    self.assertLessEqual(ops.end - ops.start, len(source))


def apply_in_app(full, cursor, ops):
    """The client applies ops to its whole text (the window is only what it reported)."""
    if ops.delete:
        offset, n = ops.delete
        full, cursor = full[:cursor + offset] + full[cursor + offset + n:], cursor + offset
    commit = ops.commit or ''
    return full[:cursor] + commit + full[cursor:], cursor + len(commit)


def window_start(full, window, cursor_full, cursor_window):
    return cursor_full - cursor_window


class RecentredWindowTests(unittest.TestCase):
    """Chrome re-centres its 4000-byte window on the new caret after an apply (review finding)."""

    def roundtrip(self, full, cursor, corrected_paragraph):
        window, wcursor, _ = chrome_window(full, cursor, cursor)
        capture = plan.capture(snap(window, wcursor))
        self.assertIsInstance(capture, plan.Capture)
        ops = plan.replace_ops(capture, snap(window, wcursor), corrected_paragraph)
        new_full, new_cursor = apply_in_app(full, cursor, ops)
        lo = full.rfind('\n', 0, cursor) + 1
        hi = full.find('\n', cursor)
        self.assertEqual(new_full, full[:lo] + corrected_paragraph + full[hi:])
        echo, echo_cursor, _ = chrome_window(new_full, new_cursor, new_cursor)
        shift = (new_cursor - echo_cursor) - (cursor - wcursor)
        return plan.expected(capture, ops), echo, echo_cursor, shift

    def test_length_changing_replacement_shifts_the_window(self):
        cursor = LONG.index('Akapit 30:') + 20
        para = LONG.split('\n')[30]
        for corrected in (para.replace('zażółć', 'ZAŻÓŁĆ, ZAŻÓŁĆ I JESZCZE RAZ ZAŻÓŁĆ'), para.replace(' dość długi', ''),
                          para.upper() + ' DOPISANE ZDANIE NA KOŃCU AKAPITU.'):
            with self.subTest(corrected=corrected[:30]):
                want, echo, echo_cursor, shift = self.roundtrip(LONG, cursor, corrected)
                self.assertNotEqual(shift, 0)  # the window really moved
                self.assertEqual(plan.verify(want, echo, echo_cursor), 'ok')
                wrong = echo[:echo_cursor - 1] + '#' + echo[echo_cursor:]
                self.assertEqual(plan.verify(want, wrong, echo_cursor), 'mismatch')

    def test_long_paragraph_cut_by_the_recentred_window(self):
        # A ~3700-byte paragraph fits the capture window around a caret in its middle; after a change
        # near its end the window centres on the new caret and cuts the paragraph's start.
        words = ' '.join('słowo%03d' % i for i in range(360))
        par = 'Długi akapit: ' + words + ' koniec'
        full = '\n'.join(['Krótki akapit %d.' % i for i in range(40)] + [par] + ['Następny akapit %d.' % i for i in range(150)])
        cursor = full.index(par) + len(par) // 2
        self.assertGreater(len(par.encode()), 3500)
        corrected = par[:-6] + 'KONIEC!'
        want, echo, echo_cursor, shift = self.roundtrip(full, cursor, corrected)
        self.assertGreater(abs(shift), 1000)
        self.assertEqual(plan.paragraph_bounds(echo, echo_cursor)[0], 0)  # paragraph start not visible
        self.assertEqual(plan.verify(want, echo, echo_cursor), 'ok')
        wrong = echo.replace('słowo359', 'słowoXXX')
        self.assertNotEqual(wrong, echo)
        self.assertEqual(plan.verify(want, wrong, echo_cursor), 'mismatch')
        short = echo[:echo_cursor - 3] + echo[echo_cursor:]  # the replacement did not land
        self.assertEqual(plan.verify(want, short, echo_cursor - 3), 'mismatch')


class VerifyTests(unittest.TestCase):
    def expected(self, text, cursor, corrected, anchor=None):
        capture = plan.capture(snap(text, cursor, anchor))
        ops = plan.replace_ops(capture, snap(text, cursor, anchor), corrected)
        return plan.expected(capture, ops), ops

    def test_table(self):
        para, _ = self.expected(TA, 60, P2.replace('błąd', 'błędy'))
        new_ta = TA.replace('błąd', 'błędy')
        sel, _ = self.expected(TA, 70, 'PIES 🐕', anchor=67)
        long_cursor = LONG.index('Akapit 30:') + 20
        window, cursor, _ = chrome_window(LONG, long_cursor, long_cursor)
        long_para, long_ops = self.expected(window, cursor, 'Akapit 30: ZAŻÓŁĆ gęślą jaźń 😀 — to jest dość długi akapit testowy numer 30.')
        new_long = LONG.replace('Akapit 30: zażółć', 'Akapit 30: ZAŻÓŁĆ')
        echo_long, echo_cursor, _ = chrome_window(new_long, long_cursor, long_cursor)
        gtk_para = self.expected(P2, 20, P2.replace('błąd', 'błędy'))[0]
        cases = [
            ('exact echo', para, new_ta, para.cursor, 'ok'),
            ('selection echo', sel, TA.replace('KOT', 'PIES 🐕'), 67 + 6, 'ok'),
            ('chrome window, same length', long_para, echo_long, echo_cursor, 'ok'),
            ('gtk4 paragraph plus three words', gtk_para, 'gęślą jaźń.\n' + P2.replace('błąd', 'błędy'),
             12 + gtk_para.cursor, 'ok'),
            ('gtk3 echo after the delete only', para, TA[:len(P1) + 1] + TA[len(P1) + 1 + P2.index('błąd'):],
             len(P1) + 1 + P2.index('błąd'), 'mismatch'),
            ('caret elsewhere', para, new_ta, para.cursor - 1, 'mismatch'),
            ('text unchanged', para, TA, para.cursor, 'mismatch'),
            ('no echo text', para, None, 0, 'mismatch'),
            ('cursor out of range', para, new_ta, len(new_ta) + 5, 'mismatch'),
            # run2 +0.611: the spike's stale paragraph replace, as echoed by GTK4
            ('corrupted echo', self.expected(P2, 33, 'PIES 🐕')[0],
             'Pierwszy akapit: zażółć gęślą jaźńPIES 🐕a.\nTrzeci akapit', 40, 'mismatch'),
        ]
        for name, want, echo, echo_cursor_, result in cases:
            with self.subTest(name):
                self.assertEqual(plan.verify(want, echo, echo_cursor_), result)


class LineEndTests(unittest.TestCase):
    """Line ends that apps store differently while the model answers with LF (macOS ImeEdits.mapSeparators)."""

    def test_split_lines(self):
        self.assertEqual(plan.split_lines('a'), (['a'], []))
        self.assertEqual(plan.split_lines('a\r\nb\nc\rd\x0be\u2028f\u2029g'),
                         (list('abcdefg'), ['\r\n', '\n', '\r', '\x0b', '\u2028', '\u2029']))
        self.assertEqual(plan.split_lines('\n\n'), (['', '', ''], ['\n', '\n']))

    def test_map_line_ends(self):
        self.assertEqual(plan.map_line_ends('Raz\x0bDwa\rTrzy', 'Raz\nDwa\nTrzy'), 'Raz\x0bDwa\rTrzy')
        self.assertEqual(plan.map_line_ends('a\r\nb', 'A\nB'), 'A\r\nB')
        self.assertEqual(plan.map_line_ends('a\rb', 'a\rb'), 'a\rb')
        self.assertIsNone(plan.map_line_ends('a\rb', 'a b'))  # a line gone
        self.assertIsNone(plan.map_line_ends('a\rb', 'a\nb\nc'))  # a line more

    def test_only_apps_with_line_ends_of_their_own_are_held_to_them(self):
        # PowerPoint (CR and VT): the break the model moved cannot be placed, so nothing is written.
        capture = plan.capture(snap('Raz\x0bDwa z bledem', 15, 0))
        self.assertEqual(plan.replace_ops(capture, snap('Raz\x0bDwa z bledem', 15, 0), 'Raz Dwa z błędem'),
                         Refusal('line_structure'))
        # A field that uses only LF: a rewrite may change the line count, as it always could on Linux.
        capture = plan.capture(snap('Raz Dwa', 7))
        ops = plan.replace_ops(capture, snap('Raz Dwa', 7), 'Raz\nDwa')
        self.assertEqual(simulate('Raz Dwa', 7, 7, ops)[0], 'Raz\nDwa')


class SharedImeCasesTests(unittest.TestCase):
    """tests/ime_cases.json, written by the macOS input-method channel: what both ports must agree on.

    Compared by result, not by the Mac's list of edits. Not checked, and why:
    - scope "cut" and the inconsistent reply: the Mac's window of an input method with its UTF-16 document
      offsets; IBus gives the surrounding text itself and the engine's own rule is `truncated` (EDGE_BYTES).
    - newMessage and wholeField: Linux always takes the caret's paragraph or the selection (PANEL.md).
    - "untouched" and "maxEdits": the Mac replaces words one by one; commit_text always writes at the
      caret, so a text between two changes cannot be skipped and Linux replaces from the first change to
      the last.
    """

    cases = json.loads((REPO / 'tests' / 'ime_cases.json').read_text(encoding='utf-8'))
    REFUSALS = {'attachments': 'embedded', 'emptyParagraph': 'empty', 'noText': 'empty'}

    def test_fixture_is_there(self):
        self.assertEqual((len(self.cases['scope']), len(self.cases['edits'])), (13, 8))

    def test_scope(self):
        for case in self.cases['scope']:
            reply = case['reply']
            if case.get('refusal') == 'cut' or 'selectionLocation' in reply:
                continue
            with self.subTest(case['name']):
                before, selected, after = reply['before'], reply.get('selected', ''), reply['after']
                cursor = len(before) + len(selected)
                got = plan.capture(snap(before + selected + after, cursor, len(before) if selected else None))
                if 'refusal' in case:
                    self.assertEqual(got, Refusal(self.REFUSALS[case['refusal']]))
                    continue
                wanted = next(c['text'] for c in case['candidates']
                              if c['scope'] in ('selection', 'paragraph') or '\n' not in c['text'])
                self.assertIsInstance(got, plan.Capture, got)
                self.assertEqual(got.source, wanted)

    def test_edits(self):
        for case in self.cases['edits']:
            original, corrected = case['original'], case['corrected']
            with self.subTest(case['name']):
                lines_end = len(plan.split_lines(original)[1]) > 0
                end = len(original)
                cursor, anchor = (end, 0) if lines_end else (end, None)  # a selection holds several lines
                capture = plan.capture(snap(original, cursor, anchor))
                self.assertIsInstance(capture, plan.Capture, capture)
                ops = plan.replace_ops(capture, snap(original, cursor, anchor), corrected)
                if case.get('noChange'):
                    self.assertTrue(ops.nothing)
                elif 'failure' in case:
                    if any(e in original for e in plan.FOREIGN_LINE_ENDS):
                        self.assertEqual(ops, Refusal('line_structure'))
                    else:  # the Mac writes this one to its clipboard; Linux has one way, and LF goes in as it is
                        self.assertEqual(simulate(original, cursor, cursor if anchor is None else anchor, ops)[0],
                                         corrected)
                else:
                    self.assertIsInstance(ops, plan.Ops, ops)
                    self.assertEqual(simulate(original, cursor, cursor if anchor is None else anchor, ops)[0],
                                     case['expected'])


if __name__ == '__main__':
    unittest.main()
