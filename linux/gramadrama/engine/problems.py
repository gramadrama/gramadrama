"""What the panel says about a correction that failed (branding/PANEL.md, Errors): the cause's code the backend reports
(core.error_kind, the backend's `why`) gives a group, a title, a sentence and the panel's buttons, the first of them
Enter's. Grouped by what the user can do about it, not by the technical cause:

  1 something must be set up: the button opens the app on the right tab; retrying alone would not help
  2 the server does not answer: retry (it may be starting), the login when away from home
  3 the model slipped up: retry
  4 the text cannot be corrected as it is: no button (the selection is the user's to change)

The error's own message is the sentence where the table has none: a server's or a model's words, or a settings check's.
Pure data and functions: no gi."""
import dataclasses

from ..i18n import tr

RETRY, SERVICES, WRITING, LOGIN = 'retry', 'open:connection', 'open:writing', 'login'
ACTIONS = (RETRY, SERVICES, WRITING, LOGIN)
LABELS = {RETRY: 'Spróbuj ponownie', SERVICES: 'Otwórz Usługi', WRITING: 'Otwórz Pisanie', LOGIN: 'Zaloguj się…'}
# What core.network_kind found, in the sentence of a server that does not answer
NETWORK = {'refused': 'odmowa połączenia', 'timeout': 'serwer nie odpowiedział na czas', 'dns': 'nie znaleziono adresu',
           'tls': 'błąd szyfrowanego połączenia', 'reset': 'połączenie zostało zerwane',
           'unreachable': 'brak drogi do serwera'}
SETTINGS_PREFIX = 'Ustawienia: '  # backend._real puts it before a settings check's message


@dataclasses.dataclass(frozen=True)
class Problem:
    group: int
    title: str  # Polish source text (tr at display time)
    text: str = None  # None: the error's own message
    actions: tuple = ()  # of ACTIONS; the first one is Enter's


TABLE = {
    # 1 · something must be set up. A new install has no model server of its own (docs/open-source-plan.md): these three
    # are told apart by backend._settings_why when no model is chosen for Ollama on this computer.
    'no_ollama': Problem(1, 'Nie ma jeszcze modelu językowego',
                         'Ollama nie jest zainstalowana na tym komputerze. Zainstaluj ją albo podaj adres serwera — oba '
                         'sposoby są w zakładce Usługi.', (SERVICES,)),
    'ollama_down': Problem(1, 'Ollama nie działa',
                           'Ollama jest zainstalowana, ale nie odpowiada. Uruchom jej usługę albo podaj adres serwera w '
                           'zakładce Usługi.', (SERVICES, RETRY)),
    'no_model': Problem(1, 'Nie wybrano modelu',
                        'Ollama działa, ale żaden model nie jest wybrany. Wybierz go albo pobierz w zakładce Usługi.',
                        (SERVICES,)),
    'settings': Problem(1, 'Trzeba dokończyć ustawienia', None, (SERVICES,)),
    'second_language': Problem(1, 'Tłumaczenie wymaga drugiego języka',
                               'Dodaj go w zakładce Pisanie (Języki tekstu).', (WRITING,)),
    'http_404': Problem(1, 'Na serwerze nie ma tego modelu', None, (SERVICES,)),
    'http_401': Problem(1, 'Serwer odmówił dostępu', None, (SERVICES,)),
    'http_403': Problem(1, 'Serwer odmówił dostępu', None, (SERVICES,)),
    'http_400': Problem(1, 'Serwer odrzucił zapytanie', None),  # e.g. a context too small: the message says how to fix it
    'test_mode': Problem(1, 'Tryb testowy', None),
    # 2 · the server does not answer
    'network': Problem(2, 'Serwer nie odpowiada',
                       'Nie można połączyć się z serwerem modelu ({detail}). Sprawdź, czy działa, albo zmień adres w '
                       'zakładce Usługi.', (RETRY, SERVICES)),
    'http': Problem(2, 'Serwer zgłosił błąd', None, (RETRY, SERVICES)),
    'offline': Problem(2, 'Serwer jest poza tą siecią',
                       'Zaloguj się w dostępie zdalnym, żeby łączyć się przez bramę.', (LOGIN, RETRY)),
    'login': Problem(2, 'Trzeba zalogować się ponownie', None, (LOGIN,)),
    'login_unreachable': Problem(2, 'Serwer logowania nie odpowiada', None, (RETRY, SERVICES)),
    # 3 · the model slipped up
    'interrupted': Problem(3, 'Połączenie przerwano przed końcem odpowiedzi', 'Zwykle wystarczy spróbować jeszcze raz.',
                           (RETRY,)),
    'empty_reply': Problem(3, 'Model zwrócił pustą odpowiedź',
                           'Zwykle wystarczy spróbować jeszcze raz albo wybrać inny tryb.', (RETRY,)),
    'bad_reply': Problem(3, 'Serwer odpowiedział w nieznanym formacie',
                         'Sprawdź protokół serwera w zakładce Usługi (Ollama albo zgodny z OpenAI).', (RETRY, SERVICES)),
    'model_error': Problem(3, 'Model zgłosił błąd', None, (RETRY,)),
    'script_drift': Problem(3, 'Wynik odrzucono', None, (RETRY,)),
    'protected_changed': Problem(3, 'Wynik odrzucono', None, (RETRY,)),
    'truncated_reply': Problem(3, 'Model uciął odpowiedź', 'Zaznacz krótszy fragment i naciśnij skrót jeszcze raz.'),
    # 4 · the text cannot be corrected as it is
    'empty': Problem(4, 'Nie ma tekstu do poprawienia', None),
    'too_long': Problem(4, 'Tekst jest za długi', None),
    'embedded': Problem(4, 'Tekst zawiera załącznik', None),
}
UNKNOWN = Problem(2, 'Korekta się nie udała', None, (RETRY,))
TEXT = 4  # the text cannot be corrected as it is: a toast with the refusal's message, no panel
# Slips of the model the engine retries once on its own before it says anything (the panel's "Ponawiam…"): a broken
# stream, an empty answer, an answer that changed what must stay or wrote another script. Not a cut-short answer.
RETRIED = frozenset(('interrupted', 'empty_reply', 'protected_changed', 'script_drift'))


def problem(why):
    """The Problem of a cause's code: one of TABLE's, a network failure, an HTTP status, or UNKNOWN."""
    why = why or ''
    if why in TABLE:
        return TABLE[why]
    if why in NETWORK:
        return TABLE['network']
    if why.startswith('http_'):
        return TABLE['http']
    return UNKNOWN


def describe(why, message):
    """(Problem, its sentence): the table's own sentence, else the error's message (a settings check's without the
    "Ustawienia: " the backend puts before it, the title says as much)."""
    p = problem(why)
    if p.text is not None:
        return p, tr(p.text, detail=tr(NETWORK.get(why, NETWORK['unreachable'])))
    prefix = tr(SETTINGS_PREFIX)
    if why == 'settings' and message.startswith(prefix):
        message = message[len(prefix):]
    return p, message


def view(p, text):
    """The panel's part of the view JSON (docs/architecture.md, View JSON): group, title, sentence and buttons."""
    return {'group': p.group, 'title': tr(p.title), 'text': text,
            'actions': [{'id': action, 'label': tr(LABELS[action])} for action in p.actions]}
