"""Network and text logic; independent of GTK, with no third-party dependencies."""
from .i18n import tr
import collections
import contextlib
import dataclasses
import fcntl
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import dictionary, oidc, paths
from .emoji import is_emoji
from .engine.plan import graphemes, is_boundary
from .engine.textdiff import (  # noqa: F401  (core.changes, core.diff_parts, … as before the move)
    DENSE_PAIRS, MAX_CHANGES, SENTENCE, TOKEN, _fold_polish, _levenshtein, _punctuation, _reason, _utf16, changes,
    diff_parts)
# The prompts are the macOS ones word for word (prompts.py has them, and the user's files over them): a test
# compares messages() with bench/prompts.golden.json, which macOS writes with --dump-prompts.
from .prompts import (EXAMPLES, INSTRUCTIONS, LANGUAGE_NAMES, LAST_RULE, RULES, SPELLING_NOTE, TARGET_NAMES,
                      TRANSLATION_EXAMPLES, TRANSLATION_PROMPT, Prompts)

MAX_CHARS = 6000
CONTEXT_SIZE = 8192  # num_ctx: the prompt (~1000 tokens) and 6000 characters in and out; as on macOS
MODES = {'fix': 'Popraw', 'improve': 'Ulepsz', 'formal': 'Formalnie',
         'casual': 'Luźniej', 'shorter': 'Krócej', 'translate': 'Tłumacz'}
class BackendError(Exception):
    """kind: a short code of the cause for the engine log (error_kind); the message may quote a server, so it is never logged."""

    def __init__(self, *args, kind=None):
        super().__init__(*args)
        self.kind = kind

class Cancelled(BackendError):
    pass

class Unreachable(BackendError):
    pass

class ScriptDrift(BackendError):
    """The model wrote letters of a script the text does not use at all (Gemma 4 turned "piszę" into "pいつze")."""


class HttpStatus(Exception):
    """A server's HTTP error, inside Backend.request (which turns it into a BackendError, or tries again)."""

    def __init__(self, code, body):
        super().__init__(code)
        self.code, self.body = code, body


def network_kind(exc):
    """Why a connection failed, as a short code: 'timeout', 'refused', 'dns', 'tls', 'reset' or 'unreachable'."""
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, TimeoutError):  # socket.timeout too
        return 'timeout'
    if isinstance(reason, ConnectionRefusedError):
        return 'refused'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, ConnectionError):
        return 'reset'
    return 'unreachable'


def error_kind(exc):
    """A short code of why a request failed, for the engine log, which never holds the message: a model's or a
    server's error text may quote the user's text."""
    kind = getattr(exc, 'kind', None)
    if isinstance(kind, str) and kind:
        return kind[:40]
    if isinstance(exc, Cancelled):
        return 'cancelled'
    if isinstance(exc, Unreachable):
        return network_kind(exc.__cause__) if exc.__cause__ is not None else 'unreachable'
    if isinstance(exc, urllib.error.HTTPError):
        return 'http_%d' % exc.code
    if isinstance(exc, OSError):  # urllib.error.URLError too
        return network_kind(exc)
    if isinstance(exc, ValueError):
        return 'bad_reply'
    return 'backend' if isinstance(exc, BackendError) else type(exc).__name__[:40]


# Links, e-mails, mentions, code and emoji codes say nothing about the language of a sentence.
NON_LANGUAGE = re.compile(r'https?://\S+|[^\s@]+@[^\s@]+\.\S+|[@#][\w.-]+|`[^`]*`|:[a-z0-9_+-]+:', re.I)
POLISH_LETTERS = frozenset('ąćęłńóśźż')
# Only words that are not also common in the other language or in tech chat: 'a', 'i', 'to',
# 'na', 'no', 'do', 'on', 'my', 'we', 'go', 'ten', 'pod', 'ci', 'co', 'ty' are left out. Polish
# words also match without diacritics, as typed in chats.
POLISH_WORDS = frozenset('''w z nie jest są sa się sie że ze jak ale czy już juz dla mam masz ma
jestem jesteś będzie bedzie był byl była byla było bylo tak też tez tylko jeszcze bo gdzie kiedy
może moze proszę prosze dzięki dzieki jutro dzisiaj dziś dzis teraz potem od przez przy bez nad
oraz albo lub ta te tego tej tym mnie mi jej ich nam wam oni ja jego który ktory która ktora
które ktore czyli więc wiec bardzo trzeba można mozna musimy możesz mozesz'''.split())
ENGLISH_WORDS = frozenset('''the and of is are was were be been being am you your that this these
those for with have has had will would can could should please thanks thank hi hello our they
their it its in at from by about what when where which who how if but not just there here me he
she his her them than then also some any all more get got did does into over only very again
still now today tomorrow yesterday sorry yes yeah sure maybe because soon later good great let
lets ahead need want know think keep keeps'''.split())
GERMAN_LETTERS = frozenset('äöüß')
GERMAN_WORDS = frozenset('''der die das den dem des ein eine einer einen einem eines und oder aber
ist sind war waren wird werden wurde wurden ich du er sie es wir ihr mir mich dich dir uns euch
nicht kein keine keinen auch noch schon bitte danke hallo heute morgen gestern über für mit ohne
weil dass wenn wie was wer wann wo warum kann können muss müssen soll sollen habe hast hat haben
hatte hatten dieser diese dieses jetzt später sehr gut gute guten gerne schicken schicke möchte
bin bist seid sein auf aus bei nach zu zum zur durch gegen in arbeit erledigt willkommen'''.split())
SUPPORTED_LANGUAGES = ('pl', 'en', 'de')
LANGUAGE_LABELS = {'pl': 'Polski', 'en': 'Angielski', 'de': 'Niemiecki'}
DEFAULT_LANGUAGES = ('pl', 'en')
DICTIONARY_CODES = {'pl': 'pl_PL', 'en': 'en_US', 'de': 'de_DE'}


def _votes(text):
    polish = english = 0
    for word in re.findall(r'[^\W\d_]+', NON_LANGUAGE.sub(' ', text)):
        lower = word.lower()
        # Capitalised words with Polish letters are usually names ('Hi Łukasz, ...').
        polish += lower in POLISH_WORDS or (word[0].islower() and not POLISH_LETTERS.isdisjoint(lower))
        english += lower in ENGLISH_WORDS
    return polish, english


def language_votes(text, languages=DEFAULT_LANGUAGES):
    polish, english = _votes(text)
    words = re.findall(r'[^\W\d_]+', NON_LANGUAGE.sub(' ', text))
    german = sum(word.lower() in GERMAN_WORDS or not GERMAN_LETTERS.isdisjoint(word.lower()) for word in words)
    counts = {'pl': polish, 'en': english, 'de': german}
    if 'de' in languages:
        dictionaries = {'pl': POLISH_WORDS, 'en': ENGLISH_WORDS, 'de': GERMAN_WORDS}
        for word in words:
            shared = [code for code in languages if word.lower() in dictionaries[code]]
            if len(shared) > 1:
                for code in shared:
                    counts[code] -= 1  # 'ich' PL/DE and 'was' EN/DE alone say nothing about the language
    return {language: counts[language] for language in languages}


def detect_language(text, languages=DEFAULT_LANGUAGES):
    """Active language or 'mixed'; deterministic function-word heuristic, never a network call."""
    if len(languages) == 1:
        return languages[0]
    votes = language_votes(text, languages)
    total = sum(votes.values())
    for language, count in votes.items():
        if total and count / total >= 0.85:
            return language
    return 'mixed'


LANGUAGETOOL_CODES = {'pl': 'pl-PL', 'en': 'en-US', 'de': 'de-DE', 'mixed': 'auto'}
# A short text without function words ('Nice work', 'Deploy failed, rollback done.'): letter pairs
# and endings typical of one language, and chat words Polish does not borrow. Only for the
# translation direction; the language line of the prompt keeps to the function words.
ENGLISH_SHAPES = re.compile(r'th|wh|ee|oo|pp|ck|gh|(?:ing|ed)$')
POLISH_SHAPES = re.compile(r'sz|cz|rz|dz|(?:ego|ych|ymi|ami|cie|owy|owa|owe|ony|ona|any|ana|ane|zy)$')
ENGLISH_CHAT = frozenset('nice work works happy help done welcome awesome cheers agreed noted fine perfect each many'.split())
# Tech words that are as common in Polish chat (the prompt's own rules say so): no evidence either way.
LOANWORDS = frozenset('''deploy deployment merge merged refactor staging feedback call task rollback review commit build
release hotfix ticket sprint backlog standup daily meeting branch bug fix feature update test testy'''.split())


def translation_target(text, languages=DEFAULT_LANGUAGES):
    """English -> Polish, otherwise -> English; mixed text goes by the majority (a tie: English,
    as on macOS). Ctrl+6 again reverses a wrong guess."""
    if len(languages) == 1:
        return languages[0]
    if set(languages) != {'pl', 'en'}:
        votes = language_votes(text, languages)
        source = max(languages, key=lambda language: votes[language])
        return next(language for language in languages if language != source)
    polish, english = _votes(text)
    if polish == english == 0:
        words = [w.lower() for w in re.findall(r'[^\W\d_]+', NON_LANGUAGE.sub(' ', text))]
        if all(POLISH_LETTERS.isdisjoint(w) for w in words):
            words = [w for w in words if w not in LOANWORDS]
            english = sum(w in ENGLISH_CHAT or bool(ENGLISH_SHAPES.search(w)) for w in words)
            polish = sum(bool(POLISH_SHAPES.search(w)) for w in words)
    return 'pl' if english > polish else 'en'


def model_parts(name):
    """('Bielik-11B-v2.3', 'Q4_K_M') for 'hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M': the
    name as macOS shows it (Settings.modelDisplayName) and the tag, none for 'latest'."""
    base, tag = name, ''
    if ':' in name.rsplit('/', 1)[-1]:
        base, tag = name.rsplit(':', 1)
    short = base.rsplit('/', 1)[-1]
    for suffix in ('-GGUF', '-Instruct'):
        index = short.find(suffix)
        if index > 0:
            short = short[:index]
    return short, '' if tag == 'latest' else tag


def display_name(model):
    """The model's name in the interface's sentences (Settings.displayName on the Mac; the cases are its SelfTest's):
    'hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M' -> 'Bielik-11B-v2.3', 'hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest'
    -> 'Gemma-4-12B', 'qwen3:8b' -> 'Qwen3:8b', 'gemma4-e4b-gramadrama:latest' -> 'Gemma4-E4B'."""
    last = ([part for part in model.split('/') if part] or [model])[-1]
    parts = [part for part in last.split(':', 1) if part]
    name = parts.pop(0) if parts else last

    def variant(token):  # from the first marker of a variant (instruct, format, quantization) it is no longer the name
        return token.lower() in ('instruct', 'it', 'gguf', 'qat') or re.match(r'([qQ]\d|[bB]?[fF](16|32))', token) is not None
    tokens = [token for token in name.split('-') if token]
    cut = next((i for i, token in enumerate(tokens) if variant(token)), None)
    if cut:
        tokens = tokens[:cut]
    # after the size ("12B", "e4b") a word without digits is a variant or the name of an import ("gemma4-e4b-gramadrama");
    # a version with digits ("Bielik-11B-v2.3") stays
    sized = next((i for i, token in enumerate(tokens) if re.fullmatch(r'[eE]?\d+(\.\d+)?[bBmM]', token)), None)
    if sized is not None:
        cut = next((i for i in range(sized + 1, len(tokens)) if not any(c.isdigit() for c in tokens[i])), None)
        if cut is not None:
            tokens = tokens[:cut]
    name = '-'.join(token.upper() if re.fullmatch(r'[eE]\d+[bB]', token) else token for token in tokens)
    # a tag of Ollama's library starts with the size ("qwen3:8b", "gemma3:12b-it-qat"): the size stays; "latest" and a
    # quantization alone ("Q4_K_M") do not
    if parts and parts[0].lower() != 'latest':
        size = []
        for token in [token for token in parts[0].split('-') if token]:
            if variant(token):
                break
            size.append(token)
        if size:
            name += ':' + '-'.join(size)
    return name[:1].upper() + name[1:]


LLM_APIS = ('openai', 'ollama')  # the protocol of the model server: any OpenAI-compatible one, or Ollama's own
LLM_THINKS = ('off', 'model')  # Ollama's "thinking": off (think=false), or the model's own way (Settings.llm_think)
LAN_SUFFIXES = ('.local', '.lan', '.home', '.internal', '.home.arpa', '.localdomain', '.intranet')


def lan_host(host):
    """True for a machine on this computer's own network: loopback, private and link-local addresses, a bare
    name and the usual local suffixes. Those are never reached through an HTTP proxy from the environment
    (a proxy cannot see a LAN); a service on the internet is, as any program of the user's would."""
    import ipaddress
    host = (host or '').lower().rstrip('.')
    if not host or '.' not in host or host.endswith(LAN_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect comes back as its HTTPError (3xx): a request with a recording or a key never goes on to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENERS = {}


def opener_for(url, redirects=True):
    """The urllib opener for url: a LAN host is never behind an HTTP proxy of the environment, one on the internet is
    (see lan_host). Shared by the model server and the speech service; redirects=False for the speech server."""
    lan = lan_host(urllib.parse.urlsplit(url).hostname)
    key = (lan, redirects)
    if key not in _OPENERS:
        handlers = ([urllib.request.ProxyHandler({})] if lan else []) + ([] if redirects else [_NoRedirect()])
        _OPENERS[key] = urllib.request.build_opener(*handlers)
    return _OPENERS[key]


def base_url(url, openai=True):
    """The base of a server's URLs. OpenAI-style: what was typed, and /v1 when it names no path (http://host:8080 is
    http://host:8080/v1; https://api.groq.com/openai/v1 stays as it is)."""
    url = url.strip().rstrip('/')
    if not openai or urllib.parse.urlsplit(url).path:
        return url
    return url + '/v1'


def url_problem(value):
    """Why value is no address of a server (a Polish reason that starts with "Podaj", shown as it is), or None."""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        _ = parsed.port
    except ValueError:
        return tr('Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434.')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        return tr('Podaj pełny adres HTTP(S), np. http://127.0.0.1:11434.')
    return None


# ---- the services: on this computer or on a server (the Usługi tab; Services.swift on the Mac) ----
LOCATIONS = ('local', 'server')
LOCAL_OLLAMA = 'http://127.0.0.1:11434'  # Ollama's own address on this computer
LOCAL_LANGUAGETOOL = 'http://127.0.0.1:8010'  # the port of docs/self-hosting.md
# The remote-access gateway (docs/remote-access.md): the services under the origin of the login address, as on the Mac.
GATEWAY_PATHS = {'llm': '/ollama', 'lt': '/languagetool', 'speech': '/whisper'}
SERVICES = tuple(GATEWAY_PATHS)


def loopback(host):
    """This computer: localhost, *.localhost, 127.0.0.0/8 and ::1 (ServerAddress.isLoopback on the Mac)."""
    import ipaddress
    host = (host or '').lower().strip('[]').rstrip('.')
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_url_problem(value):
    """Why value is no address on this computer ("Ten komputer"), or None. Empty is the default address."""
    if not value.strip():
        return None
    problem = url_problem(value)
    if problem:
        return problem
    if not loopback(urllib.parse.urlsplit(value.strip()).hostname):
        return tr('Podaj adres na tym komputerze, np. http://127.0.0.1:11434.')
    return None


def located(settings, service):
    """Where a service ('llm' or 'lt') runs: 'local' or 'server'. 'auto', the settings of before the Usługi tab: a server
    when its address is set (then it was the only way), else this computer."""
    value, url = ((settings.llm_location, settings.llm_url) if service == 'llm' else
                  (settings.languagetool_location, settings.languagetool_url))
    if value in LOCATIONS:
        return value
    return 'server' if isinstance(url, str) and url.strip() else 'local'


def gateway(settings):
    """The remote-access gateway: the origin of the login address once the login is set up (its address and the client
    id), else ''. The login's token goes to this origin only (oidc.applies)."""
    if not oidc.configured(settings):
        return ''
    parts = urllib.parse.urlsplit(oidc.issuer_of(settings))
    return '%s://%s' % (parts.scheme, parts.netloc)


def model_server(settings):
    """(address, protocol, key) of the model server: Ollama on this computer (at its default address unless another one is
    typed), the server's address, or for a server without one the gateway's Ollama. The address is '' when none is set."""
    if located(settings, 'llm') == 'local':
        return settings.llm_local_url.strip() or LOCAL_OLLAMA, 'ollama', ''
    if settings.llm_url.strip():
        return settings.llm_url.strip(), settings.llm_api, settings.llm_key
    gw = gateway(settings)
    return (gw + GATEWAY_PATHS['llm'], 'ollama', '') if gw else ('', settings.llm_api, settings.llm_key)


def languagetool_server(settings):
    """LanguageTool's address: on this computer, the server's, the gateway's for a server without one, or ''."""
    if located(settings, 'lt') == 'local':
        return settings.languagetool_local_url.strip() or LOCAL_LANGUAGETOOL
    if settings.languagetool_url.strip():
        return settings.languagetool_url.strip()
    gw = gateway(settings)
    return gw + GATEWAY_PATHS['lt'] if gw else ''


def speech_server(settings):
    """The speech server's address when dictation uses one (the gateway's for a server without one), else ''."""
    if not settings.whisper_uses_server:
        return ''
    if settings.whisper_url.strip():
        return settings.whisper_url.strip()
    gw = gateway(settings)
    return gw + GATEWAY_PATHS['speech'] if gw else ''


def service_base(settings, service):
    """The base of a service's URLs at its own address ('' when it has none): /v1 added for an OpenAI protocol (base_url)."""
    if service == 'llm':
        url, api, _ = model_server(settings)
        return base_url(url, openai=api != 'ollama') if url else ''
    if service == 'lt':
        return languagetool_server(settings).rstrip('/')
    url = speech_server(settings)
    return base_url(url, openai=settings.whisper_api != 'whispercpp') if url else ''


def status_path(settings, service):
    """What a service is asked to learn whether it answers in this network (any HTTP answer counts)."""
    if service == 'llm':
        return '/api/version' if model_server(settings)[1] == 'ollama' else '/models'
    if service == 'lt':
        return '/v2/languages'
    return '/health' if settings.whisper_api == 'whispercpp' else '/models'


# ---- the route: at home or away (docs/remote-access.md; Route in Sources/GramaBackend/RemoteAccess.swift on the Mac) ----
# Each service goes its own way. A service on this computer, a server on this computer's own address, the model server's
# OpenAI protocol (the gateway serves Ollama's) and any server while no login is set up: always direct. Another server of
# the settings: direct while it answers in this network; when it does not (away from home), the gateway's path
# (GATEWAY_PATHS) with the login's token, or 'offline' without the login. Asking takes up to PROBE_SECONDS, so each answer
# is remembered in route.json for all processes: "it answers" for HOME_FOR seconds, "it does not" for AWAY_FOR, none when
# the network changed (its default routes are part of the file) or a connection to that server failed (forget_route).
HOME_FOR = 300
AWAY_FOR = 30
PROBE_SECONDS = 1.2
OFFLINE_HINT = ('Serwer jest niedostępny w tej sieci. Zaloguj się w dostępie zdalnym, żeby łączyć się przez bramę '
                '(aplikacja GramaDrama › Usługi).')


def network_fingerprint():
    """The default routes of this computer (interface and gateway): another Wi-Fi, a VPN or a cable gives another one."""
    try:
        with open('/proc/net/route', encoding='ascii') as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return ''
    fields = (line.split() for line in lines)
    return ';'.join(sorted('%s:%s' % (f[0], f[2]) for f in fields if len(f) > 2 and f[1] == '00000000'))


def route_cache_path():
    return paths.cache_dir() / 'route.json'


def _read_answers(net):
    try:
        cached = json.loads(route_cache_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(cached, dict) or cached.get('net') != net or not isinstance(cached.get('answers'), dict):
        return {}
    return {url: entry for url, entry in cached['answers'].items()
            if isinstance(entry, dict) and isinstance(entry.get('at'), (int, float))}


@contextlib.contextmanager
def _route_lock():
    """One read-change-write of route.json at a time: the correction asks the model's and LanguageTool's ways in two
    threads, and the live service and the app ask too, so an answer written meanwhile would be lost."""
    try:
        path = route_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.with_name(path.name + '.lock'), 'a')
    except OSError:
        yield  # no cache directory: the answers are not remembered anyway
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_answers(net, answers):
    try:
        path = route_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.%d' % os.getpid())
        temp.write_text(json.dumps({'net': net, 'answers': answers}), encoding='utf-8')
        temp.replace(path)
    except OSError:
        pass


def forget_route(url=None):
    """Ask again: about every server, or (url) about the one whose connection just failed (e.g. right after leaving home)."""
    if url is None:
        try:
            route_cache_path().unlink()
        except OSError:
            pass
        return
    host = urllib.parse.urlsplit(url).hostname
    net = network_fingerprint()
    with _route_lock():
        answers = _read_answers(net)
        kept = {key: entry for key, entry in answers.items() if urllib.parse.urlsplit(key).hostname != host}
        if kept != answers:
            _write_answers(net, kept)


def server_answers(url):
    """True when url gives any HTTP answer (a 401 is one) within PROBE_SECONDS; no proxy for a server on the LAN."""
    try:
        opener_for(url).open(urllib.request.Request(url, headers={'User-Agent': oidc.USER_AGENT}), timeout=PROBE_SECONDS).close()
        return True
    except urllib.error.HTTPError as exc:
        exc.close()
        return True
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False


def _answers(url):
    net, now = network_fingerprint(), time.time()
    entry = _read_answers(net).get(url)
    if entry is not None and 0 <= now - entry['at'] < (HOME_FOR if entry.get('home') else AWAY_FOR):
        return bool(entry.get('home'))
    home = server_answers(url)  # outside the lock: it takes up to PROBE_SECONDS
    with _route_lock():
        answers = _read_answers(net)  # another thread or process may have written meanwhile
        answers[url] = {'home': home, 'at': now}
        _write_answers(net, answers)
    return home


def route(settings, service):
    """(kind, base) of a request to a service ('llm', 'lt' or 'speech'). kind: 'none' (no address), 'direct', 'gateway'
    (the address is the gateway's own: the login's token goes with it), 'home' (the server answers in this network),
    'remote' (it does not; base is the gateway's path, for the login's token) or 'offline' (it does not, and there is no
    login). GRAMADRAMA_FORCE_REMOTE=1 pretends a server at home does not answer (the gateway, tried from home)."""
    base = service_base(settings, service)
    if not base:
        return 'none', ''
    gw = gateway(settings)
    if (service != 'speech' and located(settings, service) == 'local') or not gw:
        return 'direct', base
    if oidc.applies(settings, base):
        return 'gateway', base
    # GRAMADRAMA_ROUTE_LOOPBACK=1 (tests only): a stub server on 127.0.0.1 stands for a server in the home network.
    here = loopback(urllib.parse.urlsplit(base).hostname) and os.environ.get('GRAMADRAMA_ROUTE_LOOPBACK') != '1'
    if here or (service == 'llm' and model_server(settings)[1] != 'ollama'):
        return 'direct', base
    if os.environ.get('GRAMADRAMA_FORCE_REMOTE') != '1' and _answers(base + status_path(settings, service)):
        return 'home', base
    if oidc.status(settings)[0] != 'in':
        return 'offline', base
    return 'remote', gw + GATEWAY_PATHS[service]


def addresses(settings):
    """Every address a request of these settings may go to: the services' and the gateway's (test mode allows 127.0.0.1 only)."""
    return [url for url in (model_server(settings)[0], languagetool_server(settings), speech_server(settings), gateway(settings))
            if url]


def routed(settings, url, service):
    """(url on the service's route, kind): a request is made with the service's own address (service_base); away from home
    the same request goes to the gateway's path. A URL that is not the service's goes as it is ('direct')."""
    base = service_base(settings, service)
    if base and (url == base or url.startswith(base + '/')):
        kind, target = route(settings, service)
        return target + url[len(base):], kind
    return url, 'direct'


# What servers say when the prompt does not fit their context (llama.cpp: "exceeds the available context size", OpenAI and
# vLLM: "maximum context length", LM Studio: "context overflow"). The prompt alone (instructions and examples) is about 1400
# tokens, so a server started with -c 1024 refuses every correction.
CONTEXT_ERROR = re.compile(r'context (size|length|window)|context overflow|maximum context', re.I)
MIN_SERVER_CONTEXT = 4096


def http_error(code, body, login=False):
    """An HTTP error as a message. A server says what went wrong in {"error": ...} (Ollama: a string,
    OpenAI-style servers: {"message": ...}), e.g. that the model is not found; the hint about the address
    and the model only for 404, as on macOS; and a refused key is said as such."""
    try:
        detail = json.loads(body.decode('utf-8', 'replace')).get('error')
        if isinstance(detail, dict):
            detail = detail.get('message')
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        message = '%s (HTTP %d).' % (detail.strip()[:200], code)
    else:
        message = tr('Serwer zwrócił HTTP %d.') % code
    if code in (401, 403):
        message += (tr(' Zaloguj się ponownie w aplikacji GramaDrama (Usługi › Dostęp zdalny).') if login else
                    tr(' Sprawdź klucz API w aplikacji GramaDrama (Usługi).'))
    if code == 400 and CONTEXT_ERROR.search(message):  # llama.cpp, vLLM, OpenAI, LM Studio say it in English
        message += (tr(' Serwer ma za mały kontekst: GramaDrama potrzebuje co najmniej %d tokenów (zalecane %d). '
                    'W llama-server: -c %d.') % (MIN_SERVER_CONTEXT, CONTEXT_SIZE, CONTEXT_SIZE))
    return message + (tr(' Sprawdź adres i nazwę modelu.') if code == 404 else '')


@dataclasses.dataclass
class Settings:
    # No developer's servers: a new install uses the services on this computer (Ollama and LanguageTool at their default
    # local addresses, the built-in Whisper) until the user gives it a server (docs/open-source-plan.md, G3). Each service
    # is 'local' or 'server' (LOCATIONS); "this computer" may have another local address typed in. The model server is any
    # OpenAI-compatible one, or Ollama's own API.
    llm_location: str = 'auto'  # 'auto' (settings of before the Usługi tab): a server when llm_url is set, else this computer
    llm_local_url: str = ''  # empty: LOCAL_OLLAMA
    llm_url: str = ''
    llm_api: str = 'openai'  # 'openai' or 'ollama' (LLM_APIS)
    llm_key: str = ''  # a Bearer token, for a service that wants one; never logged, never shown
    model: str = ''
    # Ollama only. 'off': think=false on every request. Measured on 193 cases (docs/models.md): a model that thinks by
    # default (Gemma 4) answered a short correction in 3.8 s instead of 0.6 s, made more mistakes, and ran out of its answer
    # limit in 6% of the cases. 'model': send nothing, the model does what it does by default. Not in validate(): a
    # value we do not know is read as 'off'.
    llm_think: str = 'off'
    languagetool_location: str = 'auto'  # as llm_location, with languagetool_url
    languagetool_local_url: str = ''  # empty: LOCAL_LANGUAGETOOL
    languagetool_url: str = ''  # a server without an address: the gateway's, else no LanguageTool
    whisper_backend: str = 'auto'  # legacy: infer from whisper_url; the UI saves 'local' or 'server'
    whisper_api: str = 'openai'  # 'openai' or the native 'whispercpp' /inference API
    whisper_url: str = ''  # retained when the built-in Whisper is selected; empty for a server: the gateway's
    whisper_model: str = ''  # the model name that server wants (OpenAI: whisper-1)
    whisper_key: str = ''
    # Remote access (docs/remote-access.md): logging in through an OpenID Connect provider such as Pocket ID (oidc.py), its
    # address and this app's client id there; the gateway to the servers is on the same origin (gateway()). The tokens are
    # in auth.json.
    auth_issuer: str = ''
    auth_client_id: str = ''
    # Dictation (docs/dictation.md), with the macOS defaults. Not in validate(): a bad value here must
    # never stop a correction; the speech service falls back to the default instead.
    dictation_enabled: bool = True
    dictation_language: str = 'pl'  # 'pl' (with English words), 'en' or 'auto' (Whisper detects it)
    dictation_cleanup: str = 'off'  # the model's cleanup: 'auto' (gdy trzeba), 'always' or 'off'
    dictation_vocabulary: str = ''  # own words, comma separated: the Whisper prompt and the model's spelling list
    # Live checking (docs/live-check.md), with the macOS defaults: on, with LanguageTool and the model.
    live_check: bool = True
    live_model_only: bool = False  # Linux experiment: the selected model replaces the other live layers
    live_languagetool: bool = True  # after a short pause: the field's text to the server
    live_bielik: bool = True  # after a longer pause: up to 1500 characters to the model
    primary_language: str = 'pl'
    secondary_language: str = 'en'  # empty: a single language, no translation/Shift alternation
    correction_shortcut: str = 'Ctrl+Alt+G'
    translation_shortcut: str = 'Ctrl+Alt+J'
    live_underlines: bool = True  # a wavy line under each mistake, where the app tells where its words are

    @property
    def whisper_uses_server(self):
        """Explicit selection wins; old settings keep their address-based behavior."""
        return self.whisper_backend == 'server' or (self.whisper_backend == 'auto' and
                                                   isinstance(self.whisper_url, str) and bool(self.whisper_url.strip()))

    @property
    def languages(self):
        """An ordered, usable language selection, also for reading hand-edited settings safely."""
        primary = self.primary_language if self.primary_language in SUPPORTED_LANGUAGES else 'pl'
        secondary = self.secondary_language if self.secondary_language in SUPPORTED_LANGUAGES else ''
        return (primary, secondary) if secondary and secondary != primary else (primary,)

    @staticmethod
    def path():
        return paths.settings_path()

    @staticmethod
    def migrated(data):
        """The content of a settings.json as the fields of today. Before 2026-10 the model server was ollama_url, and spoke
        Ollama's own API. The remote addresses of before the Usługi tab (llm_remote_url, languagetool_remote_url, 2026-10-06)
        are the gateway's paths now and are not read; the location of a service ('auto') follows its address until it is
        chosen (located)."""
        data = dict(data)
        if 'ollama_url' in data and 'llm_url' not in data:
            data['llm_url'] = data['ollama_url']
            data.setdefault('llm_api', 'ollama')
        return data

    @classmethod
    def from_dict(cls, data):
        data = cls.migrated(data)
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls) if f.name in data})

    @classmethod
    def load(cls):
        path = cls.path()
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text()))

    def validate(self, llm=True):
        """ValueError with a Polish message ("Podaj…" ones are shown as they are) when what a correction
        needs is missing (llm: the model server and its model) or when an address that is set is no address."""
        if self.primary_language not in SUPPORTED_LANGUAGES:
            raise ValueError(tr('Wybierz główny język: polski, angielski albo niemiecki.'))
        if self.secondary_language not in ('',) + SUPPORTED_LANGUAGES or self.secondary_language == self.primary_language:
            raise ValueError(tr('Drugi język musi być inny od głównego albo wyłączony.'))
        for location in (self.llm_location, self.languagetool_location):
            if location not in ('auto',) + LOCATIONS:
                raise ValueError(tr('Wybierz, gdzie działa usługa: na tym komputerze albo na serwerze (aplikacja GramaDrama › Usługi).'))
        if llm:
            if not model_server(self)[0]:
                raise ValueError(tr('Podaj adres serwera modelu albo wybierz „Ten komputer” (aplikacja GramaDrama › Usługi).'))
            if self.llm_api not in LLM_APIS:
                raise ValueError(tr('Podaj protokół serwera modelu: openai albo ollama.'))
            if not self.model.strip():
                raise ValueError(tr('Wybierz model (aplikacja GramaDrama › Usługi).'))
        for url in (self.llm_url, self.languagetool_url, self.whisper_url):
            if url.strip() and url_problem(url):
                raise ValueError(url_problem(url))
        for url in (self.llm_local_url, self.languagetool_local_url):
            if local_url_problem(url):
                raise ValueError(local_url_problem(url))
        if oidc.issuer_problem(self.auth_issuer):
            raise ValueError(oidc.issuer_problem(self.auth_issuer) + '.')

    def save(self):
        self.validate(llm=False)  # an address that is set must be one; the model server may come later
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)  # holds keys: never wider
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(dataclasses.asdict(self), indent=2) + '\n')
        temp.chmod(0o600)
        temp.replace(path)


def validate_text(text):
    if not text.strip():
        raise BackendError(tr('Zaznacz lub wklej tekst do poprawienia.'), kind='empty')
    if len(text) > MAX_CHARS:
        raise BackendError(tr('Tekst ma {count} znaków. Limit to {limit}.', count=len(text), limit=MAX_CHARS), kind='too_long')
    if '\ufffc' in text:
        raise BackendError(tr('Tekst zawiera osadzony obiekt. Zaznacz fragment bez załączników.'), kind='embedded')


def _fill(template, **values):
    """template with its {name} placeholders replaced; the user's text last, so that a {placeholder} written inside
    that text stays as it is."""
    text = values.pop('text', None)
    for name, value in values.items():
        template = template.replace('{%s}' % name, value)
    return template if text is None else template.replace('{text}', text)


def system_prompt(mode, prompts=None):
    p = prompts or Prompts.current()
    return (p.text('intro.txt') + '\nTask: ' + p.text('task-%s.txt' % mode) + '\n' + p.text('rules.txt') + '\n' +
            p.text('last-rule-fix.txt' if mode == 'fix' else 'last-rule-rewrite.txt'))


def user_prompt(text, language, prompts=None):
    p = prompts or Prompts.current()
    return _fill(p.text('user.txt'), language=p.get('names.json')['language'][language], text=text)


def translation_user_prompt(text, target, prompts=None):
    p = prompts or Prompts.current()
    return _fill(p.text('translate-user.txt'), target=p.get('names.json')['target'][target], text=text)


def messages(text, mode, target='en', prompts=None, languages=DEFAULT_LANGUAGES, words=()):
    """The messages for the model, identical to macOS Backend.messages (without the LanguageTool hints, an
    experimental option there that is off by default) when no prompt file is there. prompts: a Prompts, the user's
    files by default (Prompts.defaults() for the macOS ones only). words: the user's dictionary (dictionary.user_words);
    the ones the text uses are named before it (SPELLING_NOTE), not in a translation, which may translate them."""
    p = (prompts or Prompts.current()).for_languages(languages)
    if mode == 'translate':
        result = [{'role': 'system', 'content': _fill(p.text('translate.txt'), target=p.get('names.json')['target'][target])}]
        for source, language, output in p.get('translate-examples.json'):
            result += [{'role': 'user', 'content': translation_user_prompt(source, language, p)},
                       {'role': 'assistant', 'content': output}]
        return result + [{'role': 'user', 'content': translation_user_prompt(text, target, p)}]
    result = [{'role': 'system', 'content': system_prompt(mode, p)}]
    if mode == 'fix':
        for source, language, output in p.get('examples.json'):
            result += [{'role': 'user', 'content': user_prompt(source, language, p)}, {'role': 'assistant', 'content': output}]
    used = dictionary.relevant(words, text) if words else []
    note = SPELLING_NOTE % ', '.join(used) if used else ''
    return result + [{'role': 'user', 'content': note + user_prompt(text, detect_language(text, languages), p)}]


# The model's answer, cleaned as on macOS (PostProcess.swift; the shared cases are
# tests/postprocess_cases.json): only what the model added is changed, never content that was
# already in the original (code blocks, repeated emoji, e-mails, URLs). Then Linux checks that the
# protected fragments survived.

def clean(output, original, mode='fix'):
    """The model's answer ready to apply. After postprocess() repaired what it could, a result whose
    links, mentions, code or emoji still differ from the original's is refused; outside 'fix' the
    model may add emoji, as long as the author's stay."""
    if not output.strip():
        raise BackendError(tr('Model zwrócił pustą odpowiedź. Spróbuj ponownie.'), kind='empty_reply')
    result = postprocess(output, original, strict=mode == 'translate')
    before, after = protected(original), protected(result)
    if mode != 'fix':
        after = {token: min(n, before[token]) if _emojiish(token) else n for token, n in after.items()}
        after = collections.Counter({token: n for token, n in after.items() if n})
    if before != after:
        raise BackendError(tr('Model zmienił link, wzmiankę, kod lub emoji. Wynik odrzucono; spróbuj ponownie.'),
                           kind='protected_changed')
    drift = foreign_letters(result, original)
    if drift:
        raise ScriptDrift(tr('Model wstawił znaki z obcego pisma (%s). Wynik odrzucono; spróbuj ponownie.') % ''.join(drift[:6]),
                          kind='script_drift')
    return result


def _script(letter):
    try:
        return unicodedata.name(letter).split(' ', 1)[0]  # LATIN, CYRILLIC, GREEK, HIRAGANA, CJK, ARABIC…
    except ValueError:
        return ''


def foreign_letters(text, original):
    """The letters of text from a script that original does not use at all (Latin always counts as used: what we correct
    is Polish and English, and every letter below U+0250 is Latin anyway). A model's glitch, not a correction: a corrector
    never has a reason to bring hiragana into a Polish sentence, while a text that already has Cyrillic may keep it."""
    used = {_script(c) for c in original if c.isalpha() and ord(c) >= 0x250} | {'LATIN'}
    return sorted({c for c in text if c.isalpha() and ord(c) >= 0x250 and _script(c) not in used})


def postprocess(output, original, strict=False):
    """macOS PostProcess.clean. strict: the translation mode. A translation shares no words with the
    original, so the model's comment must also look like one (in brackets, with a colon at the end,
    'Translation:'), not only contain a matching phrase."""
    original_trimmed = original.strip()
    result = strip_wrappers(output.strip(), original_trimmed)
    result = strip_commentary(result, original_trimmed, strict)
    if not result.strip():
        return original  # the answer was a comment only ("the text contains no errors"): nothing to change
    result = strip_placeholders(result, original_trimmed)
    if not result.strip():
        return original  # only '[Your name]': macOS gets the same through an empty result (no changes)
    result = restore_emoji(result, original)
    result = restore_quotes(result, original)
    result = restore_mentions(result, original)
    result = unwrap_urls(result, original)
    result = restore_protected_spacing(result, original)
    leading = original[:len(original) - len(original.lstrip())]
    trailing = original[len(original.rstrip()):]
    return leading + result + trailing


QUOTE_OPENERS, QUOTE_CLOSERS = '"„“', '"”“'
LEADING_FENCE = re.compile(r'```[A-Za-z0-9_+\-]*[ \t]*\n')


def strip_wrappers(text, original):
    """Removes ```, <text>…</text> and quotes around the whole answer, only those the model added."""
    result, changed = text, True
    while changed:
        changed = False
        # Code fences in pairs, and only when the answer has more of them than the original.
        extra = result.count('```') - original.count('```')
        if extra >= 2 and len(result) >= 6 and result.startswith('```') and result.endswith('```'):
            result, changed = _remove_leading_fence(result)[:-3], True
        elif extra >= 1 and result.startswith('```') and not original.startswith('```'):
            result, changed = _remove_leading_fence(result), True
        elif extra >= 1 and result.endswith('```') and not original.endswith('```'):
            result, changed = result[:-3], True
        if not original.startswith('<text>') and result.startswith('<text>'):
            result, changed = result[len('<text>'):], True
        if not original.endswith('</text>') and result.endswith('</text>'):
            result, changed = result[:-len('</text>')], True
        # The author quoted the whole text (in any style): it stays, even if the model changed the style.
        wrapped = bool(original) and original[0] in QUOTE_OPENERS and original[-1] in QUOTE_CLOSERS
        if not wrapped and len(result) > 2 and result[0] in QUOTE_OPENERS and result[-1] in QUOTE_CLOSERS:
            inner = result[1:-1]
            # "a" and "b" is no wrapper: only an answer without quotes inside is unwrapped.
            if not any(c in QUOTE_OPENERS or c in QUOTE_CLOSERS for c in inner):
                result, changed = inner, True
        if result.strip() != result:
            result, changed = result.strip(), True
    return result


def _remove_leading_fence(text):
    """'```swift\\n' (a language tag only together with a line break) or just '```': the tag of
    '```Dzięki```' is its first word."""
    match = LEADING_FENCE.match(text)
    return text[match.end():] if match else text[3:]


# Only phrases in which the model talks about the text itself or its work. Narrow on purpose: in the
# rewriting modes the model may legitimately write "Please let me know if you need any changes".
COMMENTARY = re.compile('|'.join([
    r"\b(the|this|your) (original|provided|given|corrected|revised|input) (text|message|version|sentence)\b",
    r"\bhere(?: is|'s| are) (the|your) (corrected|revised|improved|updated|edited|shorter|formal|polished|fixed)\b",
    r"\bno (changes|corrections|edits) (were |are |was |is )?(needed|necessary|required|made)\b",
    r"\bnothing (to|that needs to be) (fix|correct|change|changed|fixed|corrected)\b",
    r"\b(does not|doesn't|did not|didn't) (contain|need|require|have) any (errors|mistakes|changes|corrections)\b",
    r"\b(text|message|sentence) (is|was) already (correct|fine|error-free)\b",
    r"\b(oryginalny|poprawiony|podany|zmieniony|wejściowy) tekst\b",
    r"\btekst (nie zawiera|nie wymaga|jest (już )?poprawny|jest bezbłędny)\b",
    r"\bnie (wymaga|wymagał|wymagała) (żadnych )?(zmian|poprawek|korekt)\b|\bnie zawiera (żadnych )?błędów\b",
    r"\boto (poprawiony|poprawiona|zmieniony|zmieniona|ulepszony|ulepszona|krótszy|krótsza|skrócona|skrócony) (tekst|wersja|wiadomość)\b",
    # Translation
    r"\bhere(?: is|'s) (the|your|my) translation\b|\btranslated (text|version)\b|\btranslated from (polish|english)\b",
    r"^(the )?translation( into (english|polish)| \((en|pl)\))?:?$",
    r"\boto (tłumaczenie|przekład)\b|\bprzetłumaczono z\b|^(tłumaczenie|przekład|przetłumaczony tekst)( na (angielski|polski))?:?$",
]), re.IGNORECASE)
TRAILING_REMARK = re.compile(r'[ \t]*\([^()\n]{12,}\)[ \t]*$')
LEADING_REMARK = re.compile(r'[^\n:]{8,80}:[ \t]+')
NEWLINES = '\n\r\x0b\x0c\x85\u2028\u2029'  # Swift's Character.isNewline


def strip_commentary(text, original, strict=False):
    """Removes the model's remarks: 'Oto poprawiony tekst:', '(The original text does not contain any
    errors…)'. A line (or a remark glued to its end or start) goes only when it contains a phrase
    about the text itself and matches nothing in the original."""
    folded_original = fold(original)
    original_words = {word for word in folded_original.split(' ') if len(word) >= 3}

    def aligns(line):
        """The line is the author's: a part of the original, or mostly its words."""
        folded = fold(line)
        if not folded or folded in folded_original:
            return True
        words = [word for word in folded.split(' ') if len(word) >= 3]
        if not words:
            return similarity(folded, folded_original) >= 0.5
        return sum(word in original_words for word in words) / len(words) >= 0.6

    def is_commentary(fragment):
        t = fragment.strip()
        if not t or aligns(t):
            return False
        if strict and not ((t[0] == '(' and t[-1] == ')') or (t[0] == '[' and t[-1] == ']') or t[-1] == ':'):
            return False
        return COMMENTARY.search(t) is not None

    result = text
    # A remark on the same line: in brackets at the end, or 'Oto poprawiony tekst: …' at the start.
    if result.count('(') > original.count('('):
        match = TRAILING_REMARK.search(result)
        if match and is_commentary(match.group()):
            result = result[:match.start()] + result[match.end():]
    match = LEADING_REMARK.match(result)
    if (match and not original.startswith(match.group().strip()) and is_commentary(match.group())
            and result[match.end():].strip()):
        result = result[match.end():]
    # Whole lines at the edges (any line end: \n, \r, U+2028).
    while result.strip():
        end = next((i for i, c in enumerate(result) if c in NEWLINES), len(result))
        if not result[:end].strip() or is_commentary(result[:end]):
            result = result[next((i for i in range(end, len(result)) if result[i] not in NEWLINES), len(result)):]
            continue
        start = next((i + 1 for i in range(len(result) - 1, -1, -1) if result[i] in NEWLINES), 0)
        if not result[start:].strip() or is_commentary(result[start:]):
            result = result[:next((i + 1 for i in range(start - 1, -1, -1) if result[i] not in NEWLINES), 0)]
            continue
        break
    return result  # empty when the whole answer was a remark


def fold(text):
    """Lower case, without Polish diacritics, punctuation and extra spaces."""
    return ' '.join(re.findall(r'[^\W_]+', _fold_polish(text.lower())))


def similarity(a, b):
    """1 − the normalised Levenshtein distance, on the first 400 characters (remarks are short)."""
    x, y = a[:400], b[:400]
    if not x or not y:
        return 1.0 if not x and not y else 0.0
    return 1 - _levenshtein(x, y) / max(len(x), len(y))


# '^' and '$' as in ICU's multiline mode: any line end counts, as in macOS NSRegularExpression.
PLACEHOLDER = re.compile('(?:(?<=[%s])|\\A)[ \\t]*\\[[^\\]\\n]{2,40}\\][ \\t]*(?=[%s]|\\Z)(?:\\r\\n|[%s])?'
                         % ((NEWLINES,) * 3))


def strip_placeholders(text, original):
    """'[Your name]', '[Twoje imię]': a signature placeholder the model added, which is not in the original."""
    if '[' in original:
        return text
    return PLACEHOLDER.sub('', text).strip()


EMOTICON = re.compile(r'(?:(?<=\s)|^)[;:]-?[)(DPp](?=\s|$)')


def restore_emoji(text, original):
    """The author's emoji stay untouched: when the model replaced 🙂 with 😊 (or with ';)' or ':)'),
    the original goes back into the same place. The places are taken as macOS takes them, one missing
    emoji at a time: an emoji the author did not use, then the first text emoticon while it is not in
    the original, then the end of the text. Found in one pass, not once per emoji."""
    original_emoji = [g for g in graphemes(original) if is_emoji(g)]
    if not original_emoji:
        return text
    authors, parts = set(original_emoji), graphemes(text)
    counts = collections.Counter(parts)
    missing = [e for e in dict.fromkeys(original_emoji) for _ in range(original_emoji.count(e) - counts[e])]
    places, offset = [], 0
    for g in parts:
        if len(places) < len(missing) and is_emoji(g) and g not in authors:
            places.append((offset, offset + len(g)))
        offset += len(g)
    for match in EMOTICON.finditer(text):
        if len(places) == len(missing) or match.group() in original:
            break  # macOS looks at the first emoticon only: one of the author's ends the search
        places.append(match.span())
    pieces, last = [], 0
    for (start, end), emoji in sorted(zip(places, missing)):
        pieces += [text[last:start], emoji]
        last = end
    return ''.join(pieces) + text[last:] + ''.join(' ' + emoji for emoji in missing[len(places):])


def restore_quotes(text, original):
    """Models like to turn ' into ’: the author's style of quotes stays."""
    if not any(c in '’‘' for c in original):
        text = text.replace('’', "'").replace('‘', "'")
    if not any(c in '„”“' for c in original):
        for quote in '„”“':
            text = text.replace(quote, '"')
    return text


# Fragments no model may touch (@mentions, #channels, :emoji:, URLs, `code`), as macOS finds them:
# only after whitespace or at the start.
PROTECTED_TOKEN = re.compile(r'(?:(?<=\s)|^)(:[a-z0-9_+\-]+:|@[\w.\-]+|#[\w\-]+|https?://\S+|`[^`\n]+`)', re.IGNORECASE)
# URLs and e-mails: no name inside them is a mention.
LINK_LIKE = re.compile(r'https?://\S+|[\w.%+\-]+@[\w.\-]+\.[^\W\d_]{2,}')
EMOJI_CODE = re.compile(r':[a-z0-9_+\-]+:', re.IGNORECASE)


def protected_tokens(original):
    """The protected fragments of the original, in order and without repeats."""
    tokens = {}
    for match in PROTECTED_TOKEN.finditer(original):
        token = match.group(1)
        if token.startswith('@'):
            token = token.strip('.-_')  # '@Łukasz.' at the end of a sentence: the dot is not the mention's
        tokens[token] = None
    return list(tokens)


def restore_mentions(text, original):
    """The model lost the '@' of a mention, and without it Slack tags nobody. The occurrences of the
    name (with '@' or without) in the original and in the answer are paired in order, and '@' comes
    back only where the original had a mention. When their numbers differ, nothing is guessed."""
    result = text
    for token in protected_tokens(original):
        if not token.startswith('@') or len(token) < 2 or original.count(token) <= result.count(token):
            continue
        occurrence = re.compile(r'(?<![@#/.=?&~+:%\w\-])@?' + re.escape(token[1:]) + r'(?![\w@\-]|\.[^\W_])',
                                re.IGNORECASE)

        def occurrences(s):
            links = [m.span() for m in LINK_LIKE.finditer(s)]
            return [m.span() for m in occurrence.finditer(s) if not any(a < m.end() and m.start() < b for a, b in links)]
        in_original, in_result = occurrences(original), occurrences(result)
        if len(in_original) != len(in_result):
            continue
        for (a, b), (c, d) in reversed(list(zip(in_original, in_result))):
            if original[a:b].startswith('@') and not result[c:d].startswith('@'):
                result = result[:c] + token + result[d:]
    return result


def unwrap_urls(text, original):
    """'<https://…>', '[https://…]' or markdown escaping ('merge\\_requests') break a link."""
    result = text
    for token in protected_tokens(original):
        if not token.startswith('http'):
            continue
        escaped = token
        for c in '_*~[]()':
            escaped = escaped.replace(c, '\\' + c)
        if escaped != token and escaped not in original:
            result = result.replace(escaped, token)
        wrapped = re.compile(r'[<\[]\s*' + re.escape(token) + r'\s*[>\]]')
        if not wrapped.search(original):
            result = wrapped.sub(lambda _: token, result)
    return result


def _emoji_closing_colons(text):
    """Offsets of the colons that close emoji codes. A code starts at the start of the text, after
    whitespace, or right after the previous code (':fire::tada:')."""
    closing, last_end = set(), -1
    for match in EMOJI_CODE.finditer(text):
        start = match.start()
        if start == 0 or start == last_end or text[start - 1].isspace():
            last_end = match.end()
            closing.add(last_end - 1)
    return closing


def restore_protected_spacing(text, original):
    """Restores the space before a protected fragment that the model glued to the previous
    character. Gluing that was in the original (':fire::fire:', 'admin@dev.pl', '(@jan') stays."""
    tokens = protected_tokens(original)
    # The model added a colon before an emoji code ('staging: :rocket:' or 'staging::rocket:'). A colon
    # that closes the previous code (':fire::rocket:') stays, and '9:30:' is no code.
    for token in tokens:
        glued = re.compile(':[ ]?' + re.escape(token), re.IGNORECASE)
        if not token.startswith(':') or glued.search(original):
            continue
        closing = _emoji_closing_colons(text)
        for match in reversed(list(glued.finditer(text))):
            if match.start() not in closing:
                text = text[:match.start()] + ' ' + token + text[match.end():]
    spaces = set()  # offsets to insert a space before
    for token in tokens:
        found = text.find(token)
        while found >= 0:
            if found:
                start = found - 1
                while start and not is_boundary(text, start):
                    start -= 1
                previous = text[start:found]  # the whole character: an emoji with a skin tone
                if previous.strip() and previous + token not in original:
                    spaces.add(found)
            found = text.find(token, found + 1)
    for offset in sorted(spaces, reverse=True):
        text = text[:offset] + ' ' + text[offset:]
    return text


# The Linux check after the repairs: the protected fragments of the answer and of the original,
# compared as multisets. Normalised so that a correct fix passes: a URL without the punctuation after
# it, a mention without a sentence's dot, an emoji code only at a boundary (so '9:30:' is none).
PROTECTED = re.compile(r'```[\s\S]*?```|`[^`\n]+`|https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.\w+|(?<!\w)[@#][\w.-]+'
                       r'|:[a-zA-Z0-9_+-]+:')


def protected(text):
    """Counter of the protected fragments and emoji of text (each emoji one user-perceived character)."""
    found = collections.Counter(g for g in graphemes(text) if is_emoji(g))
    code_end = -1
    for match in PROTECTED.finditer(text):
        token = match.group()
        if token[0] == ':':
            if match.start() and match.start() != code_end and not text[match.start() - 1].isspace():
                continue
            code_end = match.end()
        elif token.startswith('http'):
            token = token.rstrip('.,;:!?)…"\'”’»]')
        elif token[0] in '@#':
            token = token.rstrip('.-')
        found[token] += 1
    return found


def _emojiish(token):
    """An emoji or an emoji code, which the rewriting modes may add; never a link or code with one."""
    return (token[0] == ':' and len(token) > 2 and token[-1] == ':') or (len(graphemes(token)) == 1 and is_emoji(token))


def _lines(response):
    """The response's lines; a connection dropped mid-stream (e.g. inside an HTTP chunk) is an
    ordinary backend error, not an unexpected exception."""
    try:
        yield from response
    except (OSError, http.client.HTTPException) as exc:
        raise BackendError(tr('Połączenie przerwano przed końcem odpowiedzi. Spróbuj ponownie.'), kind='interrupted') from exc


class Backend:
    """The servers: the model (OpenAI-compatible, or Ollama's own API) and, when its address is set, LanguageTool.
    llm=False: a user of LanguageTool alone (the live check's layer 1) needs no model server."""

    def __init__(self, settings, llm=True, prompts=None):
        settings.validate(llm=llm)
        self.settings = settings
        self.prompts = prompts  # None: the user's prompt files, read when a correction is made
        self.raw = ''  # the model's last answer before clean() (the benchmark shows it)
        self.tokens_per_second = 0.0
        self.think_refused = False  # the server said think=false is not for this model: not sent again by this Backend
        # A server on the LAN is never reached through an HTTP proxy of the environment; one on the internet is.
        self.lan_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.net_opener = urllib.request.build_opener()

    def request(self, url, data=None, timeout=90, form=False, key='', service='llm'):
        """url: with the service's own address (service_base); routed() sends it through the gateway away from home.
        key: a Bearer token the user typed for the model server (never for LanguageTool, never to the gateway). Without
        one, a server on the login's origin (oidc.py) gets the login's token; a 401 to it is answered once with a
        refreshed token. A connection that fails at home asks the route again once: away from home it now goes to the
        gateway. service: 'llm' or 'lt', whose route the request takes."""
        rejected, rerouted = '', False
        for _ in range(3):
            target, kind = routed(self.settings, url, service)
            if kind == 'offline':
                raise BackendError(tr(OFFLINE_HINT), kind='offline')
            if kind == 'remote':
                key = ''
            routed_url = target
            try:
                authorization = oidc.authorization(self.settings, routed_url, key, rejected)
            except oidc.AuthUnreachable as exc:
                raise Unreachable(str(exc), kind='login_unreachable') from exc
            except oidc.AuthError as exc:
                raise BackendError(str(exc), kind='login') from exc
            try:
                return self._open(routed_url, data, timeout, form, authorization)
            except HttpStatus as exc:
                if exc.code == 401 and authorization and not key and not rejected:
                    rejected = authorization.removeprefix('Bearer ')
                    continue
                raise BackendError(http_error(exc.code, exc.body, login=bool(authorization) and not key),
                                   kind='http_%d' % exc.code) from exc.__cause__
            except Unreachable:
                if kind == 'home' and not rerouted:
                    forget_route(url)
                    rerouted = True
                    continue
                raise
        raise Unreachable(tr('Nie można połączyć się z serwerem. Sprawdź sieć i Ustawienia.'))

    def _open(self, url, data, timeout, form, authorization):
        headers = {'User-Agent': oidc.USER_AGENT}
        if authorization:
            headers['Authorization'] = authorization
        if data is None:
            request = urllib.request.Request(url, headers=headers)
        else:
            body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            request = urllib.request.Request(url, data=body, headers=headers)
        opener = self.lan_opener if lan_host(urllib.parse.urlsplit(url).hostname) else self.net_opener
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(4096)
            except (OSError, http.client.HTTPException):
                body = b''
            finally:
                exc.close()
            raise HttpStatus(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Unreachable(tr('Nie można połączyć się z serwerem. Sprawdź sieć i Ustawienia.'), kind=network_kind(exc)) from exc

    # ---- the model server ----
    @property
    def ollama(self):
        return model_server(self.settings)[1] == 'ollama'

    @property
    def llm_key(self):
        return model_server(self.settings)[2]

    def llm_base(self):
        """The base of the model server's URLs (service_base): Ollama's as typed, the OpenAI way with /v1 added."""
        return service_base(self.settings, 'llm')

    def status(self):
        result = {}
        for name, base, path in [('Model', self.llm_base(), '/api/tags' if self.ollama else '/models'),
                                 ('LanguageTool', service_base(self.settings, 'lt'), '/v2/languages')]:
            if not base:
                result[name] = 'nie ustawiono'
                continue
            try:
                with self.request(base + path, timeout=4, key=self.llm_key if name == 'Model' else '',
                                  service='llm' if name == 'Model' else 'lt') as response:
                    data = json.load(response)
                if name == 'Model':
                    names = [m['name'] for m in data['models']] if self.ollama else [m['id'] for m in data['data']]
                    result[name] = tr('gotowy') if self.settings.model in names else tr('brak wybranego modelu')
                else:
                    result[name] = tr('gotowy')
            except Exception:
                result[name] = tr('niedostępny')
        return result

    def correct(self, text, mode='fix', target='en', partial=lambda text: None, cancel=None):
        validate_text(text)
        languages = self.settings.languages
        if mode == 'translate':
            if len(languages) < 2:
                raise BackendError(tr('Tłumaczenie wymaga drugiego języka. Dodaj go w aplikacji GramaDrama (Pisanie › Języki tekstu).'),
                                   kind='second_language')
            if target not in languages:
                target = translation_target(text, languages)
        words = dictionary.user_words(self.settings) if mode != 'translate' else ()
        prompt = messages(text, mode, target, self.prompts, languages, words)
        temperature = 0 if mode == 'fix' else 0.2 if mode == 'translate' else 0.4
        output = self.chat(prompt, temperature, text, partial, cancel)
        try:
            return clean(output, text, mode)
        except ScriptDrift:
            # Greedy decoding (temperature 0) repeats such a glitch exactly: four times in four on Gemma 4 and the model server. A
            # little randomness does not, so one more try, with it; a second glitch is the error ScriptDrift says.
            output = self.chat(prompt, max(temperature, 0.3), text, partial, cancel)
            return clean(output, text, mode)

    def chat(self, chat_messages, temperature, text, partial=lambda text: None, cancel=None, timeout=90):
        """The model's whole answer to chat_messages, streamed (partial gets it so far), before any
        post-processing. text is the user's text: it sets the answer's length limit."""
        self.settings.validate()
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise Cancelled()
        size = len(text.encode())
        limit = min(CONTEXT_SIZE - 1000 - size // 2, size + 256)
        if self.ollama:
            return self._chat_ollama(chat_messages, temperature, limit, partial, cancel, timeout)
        return self._chat_openai(chat_messages, temperature, limit, partial, cancel, timeout)

    def _ollama_chat(self, body, timeout):
        """The open response to Ollama's /api/chat with body. Thinking is switched off (settings.llm_think) because the
        thoughts count against num_predict, which is sized for the answer: a model that thinks by default (Gemma 4, Qwen3)
        lost answers to it, and was slower and worse. A few server/model pairs refuse the parameter ("does not support
        thinking"): the request is then repeated without it, and this Backend does not send it again."""
        url = self.llm_base() + '/api/chat'
        if self.settings.llm_think != 'model' and not self.think_refused:
            body['think'] = False
        try:
            return self.request(url, body, timeout=timeout, key=self.llm_key)
        except BackendError as exc:
            if 'think' not in body or 'thinking' not in str(exc).lower():
                raise
        del body['think']
        self.think_refused = True
        return self.request(url, body, timeout=timeout, key=self.llm_key)

    def _chat_ollama(self, chat_messages, temperature, limit, partial, cancel, timeout):
        body = {'model': self.settings.model, 'messages': chat_messages, 'stream': True, 'keep_alive': '24h',
                'options': {'temperature': temperature, 'num_ctx': CONTEXT_SIZE, 'num_predict': limit}}
        with self._ollama_chat(body, timeout) as response:
            output = ''
            for line in _lines(response):
                if cancel.is_set():
                    raise Cancelled()
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise BackendError(tr('Nieprawidłowa odpowiedź serwera.'), kind='bad_reply') from exc
                if chunk.get('error'):
                    raise BackendError(tr('Błąd modelu: ') + str(chunk['error'])[:200], kind='model_error')
                output += chunk.get('message', {}).get('content', '')
                partial(output)
                if chunk.get('done'):
                    if chunk.get('done_reason') == 'length':
                        raise BackendError(tr('Model uciął odpowiedź. Zaznacz krótszy fragment.'), kind='truncated_reply')
                    if cancel.is_set():
                        raise Cancelled()
                    if chunk.get('eval_count') and chunk.get('eval_duration'):
                        self.tokens_per_second = chunk['eval_count'] / (chunk['eval_duration'] / 1e9)
                    self.raw = output
                    return output
        raise BackendError(tr('Połączenie przerwano przed końcem odpowiedzi. Spróbuj ponownie.'), kind='interrupted')

    def _chat_openai(self, chat_messages, temperature, limit, partial, cancel, timeout):
        """/chat/completions with a stream of server-sent events. Servers differ in two parameters, and say so
        with a 400: newer OpenAI models want max_completion_tokens, and some take no temperature; each is
        adapted once and the request repeated."""
        body = {'model': self.settings.model, 'messages': chat_messages, 'stream': True, 'temperature': temperature,
                'max_tokens': limit}
        for _attempt in range(3):
            try:
                return self._stream_openai(body, partial, cancel, timeout)
            except (Cancelled, Unreachable):
                raise
            except BackendError as exc:
                if not _adapted(body, str(exc)):
                    raise
        return self._stream_openai(body, partial, cancel, timeout)

    def _stream_openai(self, body, partial, cancel, timeout):
        with self.request(self.llm_base() + '/chat/completions', body, timeout=timeout,
                          key=self.llm_key) as response:
            output, finished = '', False
            for raw in _lines(response):
                if cancel.is_set():
                    raise Cancelled()
                line = raw.strip()
                if not line.startswith(b'data:'):
                    continue  # a comment (": keep-alive"), an event name, a blank line
                payload = line[5:].strip()
                if payload == b'[DONE]':
                    finished = True
                    break
                try:
                    chunk = json.loads(payload)
                except (ValueError, UnicodeError) as exc:
                    raise BackendError(tr('Nieprawidłowa odpowiedź serwera.'), kind='bad_reply') from exc
                if not isinstance(chunk, dict):
                    raise BackendError(tr('Nieprawidłowa odpowiedź serwera.'), kind='bad_reply')
                if chunk.get('error'):
                    error = chunk['error']
                    raise BackendError(tr('Błąd modelu: ') + str(error.get('message') if isinstance(error, dict) else error)[:200],
                                       kind='model_error')
                for choice in chunk.get('choices') or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = (choice.get('delta') or {}).get('content')
                    if isinstance(delta, str) and delta:
                        output += delta
                        partial(output)
                    reason = choice.get('finish_reason')
                    if reason == 'length':
                        raise BackendError(tr('Model uciął odpowiedź. Zaznacz krótszy fragment.'), kind='truncated_reply')
                    if reason:
                        finished = True
                timings = chunk.get('timings')  # llama.cpp says how fast it was
                if isinstance(timings, dict) and timings.get('predicted_per_second'):
                    self.tokens_per_second = float(timings['predicted_per_second'])
        if not finished:
            raise BackendError(tr('Połączenie przerwano przed końcem odpowiedzi. Spróbuj ponownie.'), kind='interrupted')
        if cancel.is_set():
            raise Cancelled()
        self.raw = output
        return output

    def prewarm(self, chat_messages, timeout=20):
        """Has Ollama compute the fixed start of a prompt now (it caches it): one token, no stream. Only
        Ollama does that; another server is asked nothing (a cloud service would charge for it)."""
        if not self.ollama:
            return
        body = {'model': self.settings.model, 'messages': chat_messages, 'stream': False, 'keep_alive': '24h',
                'options': {'num_ctx': CONTEXT_SIZE, 'num_predict': 1, 'temperature': 0}}
        with self._ollama_chat(body, timeout) as response:
            response.read()

    def warm_up(self, timeout=300):
        """Loads the model into Ollama's memory, as macOS Backend.warmUp does at start. The same num_ctx as
        the corrections: another one would make Ollama load the model again. Another server: nothing to do."""
        if not self.ollama:
            return
        body = {'model': self.settings.model, 'keep_alive': '24h', 'options': {'num_ctx': CONTEXT_SIZE}}
        with self.request(self.llm_base() + '/api/generate', body, timeout=timeout, key=self.llm_key) as response:
            response.read()

    # ---- LanguageTool ----
    def _languagetool(self):
        base = service_base(self.settings, 'lt')
        if not base:
            raise BackendError(tr('Nie ustawiono adresu LanguageTool.'), kind='config')
        return base + '/v2/check'

    def check(self, text, timeout=8):
        """LanguageTool matches for the text (offsets and lengths in UTF-16 code units), in the
        text's language as on macOS: with 'auto', a short mixed text got its English sentences
        marked as Polish typos. preferredVariants is allowed only with 'auto'."""
        validate_text(text)
        languages = self.settings.languages
        fields = {'text': text, 'motherTongue': LANGUAGETOOL_CODES[languages[0]],
                  'language': LANGUAGETOOL_CODES[detect_language(text, languages)]}
        if fields['language'] == 'auto':
            fields['preferredVariants'] = ','.join(LANGUAGETOOL_CODES[language] for language in languages)
        with self.request(self._languagetool(), fields, timeout=timeout, form=True, service='lt') as response:
            return json.load(response)['matches']

    def check_live(self, text, language, disabled_rules=(), timeout=3):
        """LanguageTool while typing (macOS Backend.languageTool(_:language:disabledRules:)): the language
        is known (the other language's sentences are masked), no preferredVariants, a short timeout."""
        fields = {'text': text, 'motherTongue': LANGUAGETOOL_CODES[self.settings.languages[0]], 'language': language}
        if disabled_rules:
            fields['disabledRules'] = ','.join(disabled_rules)
        url = self._languagetool()
        try:
            with self.request(url, fields, timeout=timeout, form=True, service='lt') as response:
                matches = json.load(response)['matches']
        except (OSError, http.client.HTTPException, ValueError, LookupError, TypeError) as exc:
            raise BackendError(tr('Nieprawidłowa odpowiedź LanguageTool.'), kind='bad_reply') from exc
        if not isinstance(matches, list):
            raise BackendError(tr('Nieprawidłowa odpowiedź LanguageTool.'), kind='bad_reply')
        return [m for m in matches if isinstance(m, dict)]


def _adapted(body, message):
    """Change body the way a 400 says (message is the server's own text): max_tokens -> max_completion_tokens,
    or no temperature. False when the message is about something else."""
    lower = message.lower()
    if 'max_completion_tokens' in lower and 'max_tokens' in body:
        body['max_completion_tokens'] = body.pop('max_tokens')
        return True
    if 'temperature' in lower and 'temperature' in body and any(
            word in lower for word in ('unsupported', 'not support', 'only the default', 'not allowed', 'does not support')):
        del body['temperature']
        return True
    return False
