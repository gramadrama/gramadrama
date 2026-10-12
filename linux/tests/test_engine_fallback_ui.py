import pathlib
import re
import time
import unittest
from unittest import mock
from gramadrama import core
from gramadrama.engine import fallback_ui
from gramadrama.engine.fallback_ui import STRIKE, UNDERLINE, View, render
from gramadrama.engine.plan import graphemes


def visible(line):
    return len([g for g in graphemes(line) if g not in (STRIKE, UNDERLINE)])


def plain(line):
    return line.replace(STRIKE, '').replace(UNDERLINE, '')


class RenderTests(unittest.TestCase):
    def test_modes_match_core(self):
        self.assertEqual(dict(fallback_ui.MODES), core.MODES)

    def test_aux_text(self):
        chips = '[1 Popraw] 2 Ulepsz 3 Formalnie 4 Luźniej 5 Krócej 6 Tłumacz'
        cases = [
            (View(0, 'working'), 'GramaDrama · %s · Pracuję…' % chips),
            (View(0, 'ready'), 'GramaDrama · %s · Enter zastąp · Esc anuluj' % chips),
            (View(0, 'unchanged'), 'GramaDrama · %s · Bez zmian · Enter zamknij' % chips),
            (View(0, 'apply_pending'), 'GramaDrama · %s · Zastosuję po zakończeniu · Esc anuluj' % chips),
            (View(0, 'applying'), 'GramaDrama · %s · Zastępuję…' % chips),
            (View(0, 'error', error='Serwer zwrócił HTTP 500.'),
             'GramaDrama · %s · Serwer zwrócił HTTP 500. · Enter zamknij' % chips),
            (View(5, 'working'), 'GramaDrama · 1 Popraw 2 Ulepsz 3 Formalnie 4 Luźniej 5 Krócej [6 Tłumacz] · Pracuję…'),
        ]
        for view, aux in cases:
            with self.subTest(view=view):
                self.assertEqual(fallback_ui.render(view)[0], aux)
        long_error = fallback_ui.render(View(0, 'error', error='x' * 500))[0]
        self.assertLess(len(long_error), 250)
        self.assertNotIn('\n', long_error)

    def test_diff_marks(self):
        segments = (('same', 'Ala ma '), ('removed', 'kota'), ('added', 'psa'), ('same', '.'))
        self.assertEqual(fallback_ui.lines(segments),
                         ['Ala ma ' + ''.join(c + STRIKE for c in 'kota') + ''.join(c + UNDERLINE for c in 'psa') + '.'])
        emoji = fallback_ui.lines((('removed', '👍🏽é'),))
        self.assertEqual(emoji, ['👍🏽' + STRIKE + 'é' + STRIKE])  # one mark per user-visible character

    def test_wrapping_at_64_visible_characters(self):
        words = ' '.join('słowo%02d' % i for i in range(40))
        cases = [
            ('plain', (('same', words),)),
            ('marked', (('same', words[:100]), ('removed', words[100:200]), ('added', words[200:]))),
            ('one long word', (('added', 'ż' * 150),)),
            ('emoji', (('same', '😀 ' * 70),)),
        ]
        for name, segments in cases:
            with self.subTest(name):
                rows = fallback_ui.lines(segments)
                self.assertTrue(all(visible(row) <= fallback_ui.WIDTH for row in rows), [visible(r) for r in rows])
                self.assertEqual(''.join(plain(row) for row in rows), ''.join(text for _, text in segments))
                self.assertGreater(len(rows), 1)
                if name != 'one long word':
                    self.assertTrue(all(plain(row).endswith(' ') for row in rows[:-1]), rows)  # break after spaces

    def test_newlines(self):
        rows = fallback_ui.lines((('same', 'raz\n\ndwa'), ('removed', '\n'), ('added', 'x\ny')))
        self.assertEqual(rows, ['raz', '', 'dwa¶' + STRIKE + 'x' + UNDERLINE + '¶' + UNDERLINE, 'y' + UNDERLINE])

    def test_pages_of_eight_lines(self):
        wrapped = tuple(fallback_ui.lines((('same', '\n'.join('linia %d' % i for i in range(20))),)))
        self.assertEqual(fallback_ui.page_count(View(0, 'ready', wrapped)), 3)
        cases = [(0, 'linia 0', 8, '1/3'), (1, 'linia 8', 8, '2/3'), (2, 'linia 16', 4, '3/3'),
                 (9, 'linia 16', 4, '3/3'), (-3, 'linia 0', 8, '1/3')]
        for page, first, count, marker in cases:
            with self.subTest(page=page):
                aux, rows = fallback_ui.render(View(0, 'ready', wrapped, page=page))
                self.assertEqual((rows[0], len(rows)), (first, count))
                self.assertTrue(aux.endswith('PgUp/PgDn ' + marker), aux)
        aux, rows = fallback_ui.render(View(0, 'ready', ('krótko',)))
        self.assertNotIn('PgUp', aux)
        self.assertEqual(fallback_ui.render(View(0, 'working'))[1], [])

    def test_render_only_slices_the_current_page(self):
        # Wrapping happens once, in the backend's worker; drawing a page must not depend on the length.
        huge = tuple('linia %d' % i for i in range(200000))
        with mock.patch.object(fallback_ui, 'lines', side_effect=AssertionError('wrapped while drawing')):
            aux, rows = fallback_ui.render(View(0, 'ready', huge, page=12345))
        self.assertEqual(rows[0], 'linia %d' % (12345 * 8))
        self.assertIn('12346/25000', aux)

    def test_flags_wrap_in_linear_time(self):
        # A run of flag emoji used to cost a rescan of the run per code point (quadratic).
        flags = '🇵🇱' * 20000
        started = time.perf_counter()
        rows = fallback_ui.lines((('same', flags),))
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(''.join(rows), flags)
        self.assertEqual({len(graphemes(r)) for r in rows[:-1]}, {fallback_ui.WIDTH})


class NoteAndDirectionTests(unittest.TestCase):
    def test_result_source_and_translation_direction(self):
        aux, _ = render(View(5, 'ready', ('x',), '', 0, 'Bielik · 2,9 s', 'en'))
        self.assertIn('[6 Tłumacz PL→EN]', aux)
        self.assertIn('Bielik · 2,9 s · Enter zastąp', aux)
        aux, _ = render(View(5, 'working', (), '', 0, '', 'pl'))
        self.assertIn('[6 Tłumacz EN→PL]', aux)
        aux, _ = render(View(0, 'working', (), '', 0, 'Bielik · 2,9 s', 'en'))  # not translating; not done
        self.assertNotIn('→', aux)
        self.assertNotIn('Bielik', aux)
        aux, _ = render(View(0, 'unchanged', (), '', 0, 'LanguageTool (Bielik niedostępny)'))
        self.assertIn('LanguageTool (Bielik niedostępny) · Bez zmian', aux)


class MessageTests(unittest.TestCase):
    def test_every_reason_has_a_polish_message(self):
        spec = {
            'stale': 'Nie widzę aktualnej pozycji kursora. Naciśnij strzałkę i spróbuj ponownie.',
            'hidden_selection': 'Ta aplikacja nie zgłasza zaznaczenia. Ustaw kursor w akapicie bez zaznaczenia.',
            'no_text': 'Ta aplikacja nie udostępnia tekstu. Szczegóły: GramaDrama › Aplikacje.',
            'sensitive': 'Pole hasła lub prywatne — GramaDrama go nie czyta.',
            'terminal': 'Terminal — pomijam.',
            'changed': 'Tekst zmienił się. Nic nie zmieniono.',
            'truncated': 'Ta aplikacja pokazuje tylko część akapitu. Zaznacz fragment, który chcesz poprawić.',
            'verify_mismatch': 'Nie udało się potwierdzić zmiany. Sprawdź tekst.',
            'applied': 'Zastosowano',
        }
        for reason, text in spec.items():
            with self.subTest(reason):
                self.assertEqual(fallback_ui.message(reason), text)
        reasons = ['too_long', 'empty', 'embedded', 'no_field', 'bad_result', 'internal']
        for reason in reasons:
            with self.subTest(reason):
                text = fallback_ui.message(reason)
                self.assertTrue(text and text != fallback_ui.message('internal') or reason == 'internal')
        self.assertIn('6000', fallback_ui.message('too_long'))
        self.assertEqual(fallback_ui.message('nieznany_kod'), fallback_ui.message('internal'))

    def test_every_emitted_reason_code_has_its_own_message(self):
        source = ''.join((pathlib.Path(fallback_ui.__file__).parent / name).read_text()
                         for name in ('context.py', 'plan.py', 'session.py', 'browser_text.py'))
        # Also the reasons that are only logged (app_off): the app shows every refusal's message.
        emitted = set(re.findall(r"(?:Refusal|Message)\('(\w+)'|'reason': '(\w+)'", source))
        emitted = {code for pair in emitted for code in pair if code}
        self.assertGreaterEqual(len(emitted), 15)
        self.assertIn('app_off', emitted)
        self.assertEqual(emitted - set(fallback_ui.MESSAGES), set())


if __name__ == '__main__':
    unittest.main()
