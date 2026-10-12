"""Plain-text view for the IBus candidate popup (aux text + lookup lines) and Polish messages.

GNOME's popup drops IBus text attributes and never wraps, so the diff is drawn with combining
characters and pre-wrapped lines. Wrapping is linear in the text and runs in the backend's worker
(lines()); the main loop only slices pages (pure; no gi)."""
from ..i18n import tr
import dataclasses
from .plan import MAX_CHARS, graphemes

WIDTH = 64
PAGE_LINES = 8
STRIKE, UNDERLINE = '̶', '̲'
MODES = (('fix', 'Popraw'), ('improve', 'Ulepsz'), ('formal', 'Formalnie'),
         ('casual', 'Luźniej'), ('shorter', 'Krócej'), ('translate', 'Tłumacz'))
STATUS = {
    'working': 'Pracuję…',
    'ready': 'Enter zastąp · Esc anuluj',
    'unchanged': 'Bez zmian · Enter zamknij',
    'apply_pending': 'Zastosuję po zakończeniu · Esc anuluj',
    'applying': 'Zastępuję…',
}
MESSAGES = {
    'stale': 'Nie widzę aktualnej pozycji kursora. Naciśnij strzałkę i spróbuj ponownie.',
    'hidden_selection': 'Ta aplikacja nie zgłasza zaznaczenia. Ustaw kursor w akapicie bez zaznaczenia.',
    'selection_unknown': 'Nie wiem, czy coś jest zaznaczone. Naciśnij strzałkę i spróbuj ponownie.',
    # An app that needs the launch fix (docs/apps.md) is first noticed here, so this points to its list.
    'no_text': 'Ta aplikacja nie udostępnia tekstu. Szczegóły: GramaDrama › Aplikacje.',
    'sensitive': 'Pole hasła lub prywatne — GramaDrama go nie czyta.',  # branding/PANEL.md
    'terminal': 'Terminal — pomijam.',
    'changed': 'Tekst zmienił się. Nic nie zmieniono.',
    'truncated': 'Ta aplikacja pokazuje tylko część akapitu. Zaznacz fragment, który chcesz poprawić.',
    'too_long': 'Tekst ma ponad {limit} znaków. Zaznacz krótszy fragment.',
    'empty': 'Brak tekstu do poprawienia. Zaznacz tekst lub ustaw kursor w akapicie.',
    'embedded': 'Tekst zawiera osadzony obiekt. Zaznacz fragment bez załączników.',
    'no_field': 'Brak aktywnego pola tekstowego.',
    'verify_mismatch': 'Nie udało się potwierdzić zmiany. Sprawdź tekst.',
    'bad_result': 'Wynik jest pusty lub nieprawidłowy. Nic nie zmieniono.',
    'line_structure': 'Model zmienił podział na linie, a ta aplikacja ma własne końce linii. Nic nie zmieniono.',
    'applied': 'Zastosowano',
    'copied': 'Skopiowano do schowka.',
    'app_off': 'GramaDrama jest wyłączona w tej aplikacji.',  # only logged: Ctrl+Alt+G goes to the app
    'internal': 'Wystąpił błąd GramaDramy. Nic nie zmieniono.',
    # A click on a live check's suggestion (docs/live-check.md): a stale caret has the hotkey's messages instead.
    'fix_failed': 'Nie udało się podmienić słowa — pole się zmieniło.',
    'fix_selection': 'W polu jest zaznaczenie. Usuń je i kliknij poprawkę ponownie.',
    'learned': 'Dodano do słownika.',
    'rule_off': 'Wyłączono tę regułę. Włączysz ją w aplikacji GramaDrama (Pisanie › Słownik i reguły).',
}
ERROR_CHARS = 120
DIRECTIONS = {'en': 'PL→EN', 'pl': 'EN→PL'}  # Ctrl+6 again reverses the direction


@dataclasses.dataclass(frozen=True)
class View:
    mode: int  # index into MODES
    status: str  # key of STATUS, or 'error'
    rows: tuple = ()  # lines(segments) of the diff, wrapped by the backend's worker
    error: str = ''
    page: int = 0
    note: str = ''  # who produced the result, e.g. 'Bielik 11B · 2,9 s'
    target: str = None  # language code reported by the backend
    source_language: str = None


def message(reason):
    """Polish text for a reason code."""
    return tr(MESSAGES.get(reason, MESSAGES['internal']), limit=MAX_CHARS)


def _cells(kind, text):
    mark = {'removed': STRIKE, 'added': UNDERLINE}.get(kind, '')
    for g in graphemes(text):
        if g in ('\n', '\r\n'):
            if mark:
                yield '¶' + mark, False
            if kind != 'removed':
                yield None, False  # line break
        else:
            yield g + mark, g.isspace()


def lines(segments):
    """Diff segments ((kind, text) with kind 'same', 'removed' or 'added') as lines of at most
    WIDTH visible characters, wrapped after spaces. Linear in the text; not for the main loop."""
    out, row = [], []

    def flush(row):
        while len(row) > WIDTH:
            cut = next((i for i in range(WIDTH, 0, -1) if row[i - 1][1]), WIDTH)
            out.append(''.join(c for c, _ in row[:cut]))
            row = row[cut:]
        out.append(''.join(c for c, _ in row))

    for kind, text in segments:
        for cell in _cells(kind, text):
            if cell[0] is None:
                flush(row)
                row = []
            else:
                row.append(cell)
    if row:
        flush(row)
    return out


def page_count(view):
    return max(1, -(-len(view.rows) // PAGE_LINES))


def render(view):
    """(aux_text, lines of the current page) for update_auxiliary_text / update_lookup_table.
    An empty list means the lookup table is hidden. Cost does not depend on the text length."""
    pages = page_count(view)
    page = min(max(view.page, 0), pages - 1)
    def label(i, key, name):
        name = tr(name)
        if key == 'translate' and i == view.mode and view.target in ('pl', 'en', 'de'):
            direction = ('%s→%s' % (view.source_language.upper(), view.target.upper())
                         if view.source_language in ('pl', 'en', 'de') else DIRECTIONS.get(view.target, view.target.upper()))
            return '%s %s' % (name, direction)
        return name
    chips = ' '.join(('[%d %s]' if i == view.mode else '%d %s') % (i + 1, label(i, key, name))
                     for i, (key, name) in enumerate(MODES))
    if view.status == 'error':
        error = view.error if len(view.error) <= ERROR_CHARS else view.error[:ERROR_CHARS - 1] + '…'
        status = error + tr(' · Enter zamknij')
    else:
        status = tr(STATUS[view.status])
        if view.note and view.status in ('ready', 'unchanged', 'apply_pending'):
            status = view.note + ' · ' + status
    if pages > 1:
        status += ' · PgUp/PgDn %d/%d' % (page + 1, pages)
    return 'GramaDrama · %s · %s' % (chips, status), list(view.rows[page * PAGE_LINES:(page + 1) * PAGE_LINES])
