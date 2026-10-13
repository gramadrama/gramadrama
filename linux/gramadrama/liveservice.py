"""The live-check service (docs/live-check.md): checks the focused field's text while the user types, as
macOS LiveMonitor and LiveChecker do. A child of the engine in the system Python; one JSON object per line
on stdin and stdout. Layer 0 at once (Enchant, for both languages), layer 1
after LT_PAUSE without changes (LanguageTool on the server, Polish and English), layer 2 after BIELIK_PAUSE (Bielik, the
Popraw call, only for Polish sentences). Nothing it reads ever goes to a log.

Commands:  {"cmd": "text", "field": f, "text": T, "caret": c or null}   the text to check (the engine's
           window or its caret paragraph); caret None with a selection;
           "panel": true for the result edited in the correction panel: no model (the text came from it) and
           no AT-SPI underlines (the panel draws its own), also in the model-only mode
           {"cmd": "clear"}                                          nothing to check (focus gone, panel open…)
           {"cmd": "fixed", "field": f, "start": a, "end": b}        a fix went in there: its issues go at once
           {"cmd": "learn", "word": w}                               into the user's dictionary
           {"cmd": "ignore", "word": w, "rule": r}                   not shown again while the service runs
           {"cmd": "rule_off", "rule": r}                            Wyłącz regułę: off until turned on in Pisanie
           {"cmd": "caret", "id": n, "text": T}                      where the focused field's caret is now in T
           {"cmd": "correct", "id": n, "text": T}                    manual correction through the model-only cache
           {"cmd": "cancel_correct", "id": n}                        cancel that manual request
Events:    {"ev": "issues", "field": f, "issues": [...],
            "checking": bool}                                   visible issues and active server checks; an issue's
                                                                learn and rule_off: what its card offers
           {"ev": "correction", "id": n, "kind": k, ...}            manual result/error or mode fallback
           {"ev": "marks", "field": f, "caret": c, "length": n,     where their words are (gramadrama/geometry.py),
            "view": {...} or null}                                  for the underlines; null: none to draw
           {"ev": "caret", "id": n, "cursor": c, "anchor": a}       the answer, both null when AT-SPI cannot tell
           {"ev": "log", "event": name, ...}                        counts and times only

Test mode (GRAMADRAMA_ENGINE_TEST=1): only a server on 127.0.0.1 is asked, as the backend children do;
LIVE_PAUSES (seconds, 'lt,bielik') shortens the pauses for the tests."""
import dataclasses
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse

from . import core, dictionary, live as L, model_live, paths
from .i18n import tr, set_language, interface_language

TEST = os.environ.get('GRAMADRAMA_ENGINE_TEST') == '1'
LT_PAUSE, BIELIK_PAUSE = 0.8, 3.0
if TEST and os.environ.get('LIVE_PAUSES'):
    LT_PAUSE, BIELIK_PAUSE = (float(x) for x in os.environ['LIVE_PAUSES'].split(','))
BACKOFF = 60.0  # after a failed Bielik check
CACHE = 20  # Bielik's answers, first in first out
MARKS_INTERVAL = 0.4  # macOS re-reads the field this often: the underlines follow scrolling
SETTINGS_SECONDS = 2.0
_write = threading.Lock()


def emit(**event):
    with _write:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def settings():
    try:
        current = core.Settings.load()
    except (ValueError, TypeError, OSError):
        current = core.Settings()
    set_language(current.primary_language)
    return current


def real_geometry():
    """AT-SPI of the desktop the service runs in. In test mode only inside the isolated session (a work
    dir /tmp/gd-* that also holds the session bus, as the extension's policy.isTestMode): never the
    desktop the tests were started from, whose focused field it would read."""
    if TEST:
        work = os.environ.get('GD_WORK', '')
        if not re.fullmatch(r'/tmp/gd-[A-Za-z0-9_]+', work) or work + '/' not in os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''):
            raise RuntimeError('no AT-SPI outside the isolated session')
    from . import geometry
    return geometry.Geometry()


def backend(current, llm=True):
    """core.Backend for the servers, or None (an address that is no address, or no model server when llm; in test
    mode anything but 127.0.0.1). llm=False: LanguageTool alone, which needs no model server."""
    urls = core.addresses(current)
    if TEST and not all(urllib.parse.urlsplit(u).hostname in ('127.0.0.1', 'localhost', '::1') for u in urls):
        return None
    try:
        return core.Backend(current, llm=llm)
    except ValueError:
        return None


def paused_note(current):
    """Model only, after a failure of the server (BACKOFF): why nothing is checked for a minute (macOS
    LiveMonitor.modelOnlyPausedNote)."""
    return tr('{model} nie odpowiada — sprawdzanie wstrzymane na minutę.', model=core.display_name(current.model))


def unchecked_note(rejected):
    """Model only: how many sentences got an unusable answer (model_live.REJECTED) and stay unchecked; None for none."""
    if not rejected:
        return None
    if rejected == 1:
        return tr('1 zdanie bez sprawdzenia — model zwrócił niepewną odpowiedź.')
    return tr('Zdania: {count} bez sprawdzenia — model zwrócił niepewną odpowiedź.', count=rejected)


def profile(current):
    """Opaque RAM cache identity; never log endpoints, credentials or text."""
    fields = ('live_check', 'live_model_only', 'live_languagetool', 'live_bielik', 'dictation_vocabulary',
              'llm_location', 'llm_local_url', 'llm_url', 'llm_api', 'llm_key', 'model', 'llm_think',
              'auth_issuer', 'auth_client_id', 'languagetool_location', 'languagetool_local_url', 'languagetool_url')
    values = [current.languages] + [getattr(current, name) for name in fields]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).digest()


class Checker:
    """The three layers for one text (macOS LiveChecker); the spellers stay loaded."""

    def __init__(self, spellers=None):
        from . import spell
        self.spell = spell
        if spellers is None:
            spellers = {}
            for tag in core.DICTIONARY_CODES.values():
                try:
                    spellers[tag] = spell.Speller(tag)
                except LookupError:
                    spellers[tag] = None
        self.pl, self.en, self.de = (spellers.get(core.DICTIONARY_CODES[code]) for code in core.SUPPORTED_LANGUAGES)
        self.suggestions = {}  # (tag, word) -> suggestions: a word typed on stays cheap to check
        self.known = dictionary.Known()  # the user's words (dictionary.py), as refresh found them
        self.lists = {}  # tag -> the signature of its personal list when its speller was opened

    def speller(self, language):
        return getattr(self, language, None)

    def _list_signature(self, tag):
        return dictionary.signature(paths.enchant_dir() / (tag + '.dic'))

    def refresh(self, current):
        """The user's words, before a check: the own ones of the settings and the personal lists. Enchant reads a personal
        list only when it opens the dictionary, so a list that someone else changed (a word removed in Pisanie, one added in
        a GNOME app) opens it again, in about 40 ms. The old one is not freed: a layer's thread may still be using it."""
        self.known = dictionary.Known(dictionary.user_words(current))
        for code in core.SUPPORTED_LANGUAGES:
            speller = self.speller(code)
            if not isinstance(speller, self.spell.Speller):
                continue
            signature = self._list_signature(speller.tag)
            if self.lists.setdefault(speller.tag, signature) == signature:
                continue
            try:
                setattr(self, code, self.spell.Speller(speller.tag))
            except LookupError:
                continue
            self.lists[speller.tag] = signature
            self.suggestions = {}

    def learn(self, word, language):
        """Dodaj do słownika: into the personal list of the language, which this speller already knows the new state of."""
        speller = self.speller(language)
        if speller is None:
            return
        speller.add(word)
        if isinstance(speller, self.spell.Speller):
            self.lists[speller.tag] = self._list_signature(speller.tag)

    def learned(self, word, languages=core.DEFAULT_LANGUAGES):
        return word in self.known or any(s is not None and s.learned(word) for s in (self.speller(code) for code in languages))

    def _cached(self, speller):
        checker = self

        class Cached:
            def check(self, word):
                return speller.check(word)

            def suggest(self, word):
                key = (speller.tag, word)
                if key not in checker.suggestions:
                    if len(checker.suggestions) > 5000:
                        checker.suggestions.clear()
                    checker.suggestions[key] = speller.suggest(word)
                return checker.suggestions[key]
        return Cached()

    def quick(self, text):
        """Layer 0: milliseconds, on this machine."""
        issues = []
        for language, masked in text.parts.items():
            speller = self.speller(language)
            english = self.en if language != 'en' and 'en' in text.languages else None
            if masked.strip() and speller is not None:
                issues += self.spell.spelling_issues(masked, text.text, text.masks[language], self._cached(speller),
                                                     english, lambda word: self.learned(word, text.languages),
                                                     'SPELLING_' + language.upper(), allow_names=language != 'de')
            if language == 'pl':
                issues += L.missing_diacritics(masked, text.text)
        return L.merge(issues)

    def languagetool(self, text, server, off=()):
        """Layer 1, or None when LanguageTool did not answer (the known list stays). off: the rules the user turned off,
        which the server is asked not to check."""
        jobs = [(masked, core.LANGUAGETOOL_CODES[code], text.masks[code])
                for code, masked in text.parts.items() if masked.strip()]
        disabled = L.DISABLED_RULES + tuple(rule for rule in off if rule not in L.DISABLED_RULES)
        issues = []
        for masked, language, mask in jobs:
            try:
                matches = server.check_live(masked, language, disabled)
            except core.BackendError:
                return None
            for match in matches:
                start = L.utf16_to_index(masked, match.get('offset', 0))
                end = L.utf16_to_index(masked, match.get('offset', 0) + match.get('length', 0))
                if end <= start or end > len(text.text) or L.touches(mask, start, end):
                    continue
                word, rule = text.text[start:end], (match.get('rule') or {}).get('id', '')
                if rule in off:
                    continue  # a server that does not know the rule's id checks it anyway
                if rule.startswith('MORFOLOGIK_RULE') and L.plausible(
                        word, start, text.text, lambda word: self.learned(word, text.languages),
                        self.en.check if self.en and 'en' in text.languages else lambda w: False):
                    continue
                suggestions = L.ranked([r.get('value', '') for r in (match.get('replacements') or [])[:5]], word)
                restored = L.restoring_diacritics(word) if language == 'pl-PL' else None
                if restored and restored not in suggestions:  # "ze to": "że to" before LanguageTool's "z to"
                    suggestions.insert(0, restored)
                description = (match.get('rule') or {}).get('description')
                issues.append(L.Issue(start, end, word, match.get('message', ''), tuple(suggestions[:3]),
                                      'LanguageTool', rule, description if isinstance(description, str) else ''))
        return issues


@dataclasses.dataclass
class CorrectionRequest:
    id: int
    text: str
    identity: bytes
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event)


class Monitor:
    """macOS LiveMonitor for one field at a time: the lists follow edits, the layers run after their pauses,
    and every change of the visible issues is sent."""

    def __init__(self, checker, geometry=None):
        self.checker = checker
        self.make_geometry = geometry or real_geometry
        self.lock = threading.Condition()
        self.field = self.text = None
        self.revision = None  # optional engine snapshot id, echoed to reject already queued stale events
        self.caret = None
        self.version = 0
        self.changed_at = time.monotonic()
        self.quick = self.lt = self.bielik = []
        self.pending = False  # a new text for layer 0
        self.sent = None
        self.cache = {}  # text -> Bielik's issues
        self.fragments = model_live.FragmentChecker()
        self.backoff_until = 0.0
        self.note = None  # Model only: why the field shows no issues it may have (paused, sentences unchecked; macOS)
        self.bielik_cancel = None
        self.checks_in_progress = set()  # request tokens for the current text version only
        self.model_lock = threading.Lock()  # live and manual calls share one serial model worker
        self.active_model = None  # (profile, full text, cancellation, live version)
        self.correction = self.correction_pending = self.correction_thread = None
        self.stopped = False
        self.failed = set()  # the layers whose failure has been logged (once: a bug would repeat per key)
        self.visible = []  # what _send sent last: the underlines are drawn for these
        self.ignored = set()  # "word|rule" (macOS LiveMonitor.ignoreKey): "Ignoruj" in the underline card
        self.rules_off = dictionary.rules_off()  # {rule: description}: "Wyłącz regułę", kept in rules.json
        self.panel = False  # the text is the result edited in the correction panel (the text command's "panel")
        self.geometry = None  # made by the marker thread, which alone may call AT-SPI; False: unavailable
        self.carets = []  # (id, text) the engine asks about (a hotkey that doubts the caret), for the marker thread
        current = settings()
        self.languages = current.languages
        self.profile = profile(current)
        self.model_only = current.live_model_only is True
        self.enabled = current.live_check is not False

    def _reset_locked(self):
        self.version += 1
        self.changed_at = time.monotonic()
        self.checks_in_progress.clear()
        self.quick = self.lt = self.bielik = []
        self.cache.clear()
        self.backoff_until = 0.0
        self.note = None
        self.pending = self.text is not None
        if self.bielik_cancel is not None:
            self.bielik_cancel.set()
        if self.correction is not None:
            self.correction.cancel.set()
            emit(ev='correction', id=self.correction.id, kind='error', why='settings_changed',
                 message=tr('Ustawienia zmieniły się podczas korekty. Użyj skrótu ponownie.'))
            self.correction = self.correction_pending = None
        self.fragments.clear()
        self.lock.notify_all()

    def refresh_settings(self, current):
        """Switch modes and model routes without waiting for another edit in the field."""
        identity = profile(current)
        with self.lock:
            if identity == self.profile:
                return
            self.profile = identity
            self.languages = current.languages
            self.model_only = current.live_model_only is True
            self.enabled = current.live_check is not False
            self._reset_locked()
        self._send()

    def refresh_languages(self, languages):
        """Drop results made with the old profile, even if the field text has not changed."""
        with self.lock:
            if self.languages == languages:
                return
            self.languages = languages
            self._reset_locked()
        self._send()

    # ---- commands (the main thread) ----
    def text_changed(self, field, text, caret, revision=None, panel=False):
        current = settings()
        self.refresh_settings(current)
        self.checker.refresh(current)
        self.rules_off = dictionary.rules_off()  # turned on again in Pisanie, or off from another field's card
        with self.lock:
            was_checking = bool(self.checks_in_progress)
            if field != self.field or panel != self.panel:
                self.panel = panel
                self.quick = self.lt = self.bielik = []
                self.note = None  # another field: a pause still in force says so again at its check
                self.sent = None
            elif text != self.text:
                self.quick, self.lt, self.bielik = (L.shift(x, self.text, text) for x in (self.quick, self.lt, self.bielik))
            elif caret == self.caret and revision == self.revision:
                return
            changed = field != self.field or text != self.text
            revision_changed = revision != self.revision
            self.field, self.text, self.caret = field, text, caret
            self.revision = revision
            if changed:
                self.version += 1
                self.changed_at = time.monotonic()
                self.checks_in_progress.clear()
                self.pending = True
                self._cancel_live_locked()
            self.lock.notify_all()
        if not changed or was_checking or revision_changed:
            self._send()  # clear stale activity immediately; a caret move may reveal a known issue

    def clear(self):
        with self.lock:
            self.field = self.text = self.caret = None
            self.revision = None
            self.quick = self.lt = self.bielik = []
            self.note = None
            self.version += 1
            self.changed_at = time.monotonic()
            self.checks_in_progress.clear()
            self.pending = False
            self._cancel_live_locked()
            self.lock.notify_all()
        self._send()

    def stop(self):
        """The layers' threads end (and a running Bielik check is cancelled)."""
        with self.lock:
            self.stopped = True
            self.checks_in_progress.clear()
            if self.bielik_cancel is not None:
                self.bielik_cancel.set()
            if self.correction is not None:
                self.correction.cancel.set()
                self.correction = self.correction_pending = None
            self.lock.notify_all()
        self._send()

    def _joined_locked(self):
        request, active = self.correction, self.active_model
        return bool(request is not None and not request.cancel.is_set() and active is not None
                    and request.identity == active[0] and request.text == active[1])

    def _cancel_live_locked(self):
        # Opening the manual panel clears background live text. A manual request already
        # waiting for this exact check keeps it alive, but can never publish old live issues.
        if self.bielik_cancel is not None and not self._joined_locked():
            self.bielik_cancel.set()

    def request_correction(self, sid, text):
        current = settings()
        self.refresh_settings(current)
        with self.lock:
            if self.stopped:
                return
            if self.correction is not None:
                self.correction.cancel.set()
            self.correction = self.correction_pending = None
            if not current.live_check or current.live_model_only is not True:
                emit(ev='correction', id=sid, kind='fallback')
                return
            request = CorrectionRequest(sid, text, profile(current))
            self.correction = self.correction_pending = request
            if self.active_model is not None and self.active_model[3] != self.version:
                self._cancel_live_locked()  # a replacement may no longer need the joined old text
            if self.correction_thread is None:
                self.correction_thread = threading.Thread(target=self._corrections,
                                                         name='gramadrama-live-correction', daemon=True)
                self.correction_thread.start()
            self.lock.notify_all()

    def cancel_correction(self, sid):
        with self.lock:
            if self.correction is None or self.correction.id != sid:
                return
            self.correction.cancel.set()
            self.correction = self.correction_pending = None
            if self.active_model is not None and self.active_model[3] != self.version:
                self._cancel_live_locked()
            self.lock.notify_all()

    def _corrections(self):
        while True:
            with self.lock:
                while self.correction_pending is None and not self.stopped:
                    self.lock.wait()
                if self.stopped:
                    return
                request, self.correction_pending = self.correction_pending, None
            self._correct(request)

    def _correction_result(self, request, payload):
        current = settings()
        self.refresh_settings(current)
        with self.lock:
            if (self.stopped or self.correction is not request or request.cancel.is_set()
                    or request.identity != profile(current)):
                return
            self.correction = None
            emit(ev='correction', id=request.id, **payload)

    def _correct(self, request):
        from .engine import backend as engine_backend, fallback_ui
        try:
            with self.model_lock:
                current = settings()
                self.refresh_settings(current)
                if request.cancel.is_set() or request.identity != profile(current):
                    return
                core.validate_text(request.text)
                server = backend(dataclasses.replace(current, llm_think='off',
                                                     languagetool_url='', languagetool_local_url=''))
                if server is None:
                    raise core.BackendError(tr(engine_backend.FAILED), kind='no_backend')
                words = dictionary.user_words(current)
                result = self.fragments.check(request.text, lambda fragment, event: model_live.correct_fragment(
                    server, fragment, event, words=words), request.identity, request.cancel)
            payload = engine_backend.result_payload(core, fallback_ui.lines, request.text, result.corrected,
                                                    'fix', languages=list(current.languages))
        except core.Cancelled:
            return
        except core.BackendError as exc:
            payload = {'kind': 'error', 'message': str(exc), 'why': core.error_kind(exc)}
        except Exception as exc:
            payload = {'kind': 'error', 'message': tr(engine_backend.FAILED), 'why': type(exc).__name__}
        self._correction_result(request, payload)

    def ignore(self, word, rule):
        with self.lock:
            self.ignored.add('%s|%s' % (word.lower(), rule))
        self._send()

    def learned(self, word):
        """The word went into the user's dictionary: its spelling issues go at once (macOS learn), and the model's results,
        made without it in the prompt, are asked for again."""
        with self.lock:
            self.quick, self.lt, self.bielik = ([i for i in x if not (i.word == word and i.kind == 'spelling')]
                                                for x in (self.quick, self.lt, self.bielik))
            self.cache.clear()
            self.fragments.clear()
        self._send()

    def turn_off(self, rule):
        """Wyłącz regułę: the rule's issues go at once, and it stays off (rules.json, with LanguageTool's description of
        it) until it is turned on in Pisanie; while the service runs even when rules.json cannot be written."""
        with self.lock:
            issue = next((i for i in self.lt if i.rule == rule and i.can_turn_off), None)
        if issue is None:
            return
        problem = dictionary.turn_off_rule(rule, issue.description)
        with self.lock:
            self.rules_off = dict(self.rules_off, **{rule: issue.description})
        if problem:
            emit(ev='log', event='live_rule_off_unsaved', rule=rule)
        self._send()

    def fixed(self, field, start, end):
        with self.lock:
            if field != self.field:
                return
            self.quick, self.lt, self.bielik = ([i for i in x if (i.start, i.end) != (start, end)]
                                                for x in (self.quick, self.lt, self.bielik))
        self._send()

    def ask_caret(self, cid, text):
        with self.lock:
            self.carets.append((cid, text))
            self.lock.notify_all()

    # ---- the visible list ----
    def _start_checking(self, version):
        with self.lock:
            if version != self.version or self.stopped or not self.enabled:
                return None
            token = (version, object())
            self.checks_in_progress.add(token)
        self._send()
        return token

    def _finish_checking(self, token):
        with self.lock:
            if token not in self.checks_in_progress:
                return  # an edit/reset already removed it; a newer request owns its own token
            self.checks_in_progress.remove(token)
        self._send()

    def _send(self):
        # Under the lock: a slower layer's older list can never be sent after a newer one.
        with self.lock:
            field, text, caret = self.field, self.text, self.caret
            layers = (self.lt, self.quick) if self.panel else (self.bielik,) if self.model_only else (self.lt, self.quick, self.bielik)
            issues = [] if text is None or not self.enabled else [i for i in L.merge(*layers)
                                              if not L.under_caret(i, caret)
                                              and '%s|%s' % (i.word.lower(), i.rule) not in self.ignored
                                              and not (i.source == 'LanguageTool' and i.rule in self.rules_off)]
            visible = [{'id': '%d:%d:%s' % (i.start, i.end, i.rule), 'start': i.start, 'end': i.end, 'word': i.word,
                        'message': i.message, 'suggestions': list(i.suggestions),
                        'source': tr('słownik') if i.source == 'słownik' else i.source, 'rule': i.rule,
                        'kind': i.kind, 'rule_off': i.can_turn_off} for i in issues]
            checking = bool(self.checks_in_progress) and text is not None and self.enabled and not self.stopped
            note = self.note if self.model_only and not self.panel and text is not None and self.enabled else None
            key = (field, self.revision, json.dumps(visible, sort_keys=True), checking, note)
            if key != self.sent:
                self.sent = key
                self.visible = visible
                locale = interface_language(self.languages[0])
                emit(ev='issues', field=field, issues=visible, checking=checking,
                     **({'note': note} if note else {}),
                     **({'revision': self.revision} if self.revision is not None else {}),
                     **({'ui_language': locale} if locale != 'pl' else {}))
                self.lock.notify_all()  # the marker draws them

    # ---- the layers (their own threads) ----
    def run(self):
        threading.Thread(target=self._layer0, name='gramadrama-live0', daemon=True).start()
        threading.Thread(target=self._marker, name='gramadrama-marks', daemon=True).start()
        threading.Thread(target=self._paused, args=(LT_PAUSE, self._layer1), name='gramadrama-live1', daemon=True).start()
        threading.Thread(target=self._paused, args=(BIELIK_PAUSE, self._layer2), name='gramadrama-live2', daemon=True).start()

    def _layer0(self):
        while True:
            with self.lock:
                while not self.pending and not self.stopped:
                    self.lock.wait()
                if self.stopped:
                    return
                self.pending = False
                version, text, languages, panel = self.version, self.text, self.languages, self.panel
            if text is None:
                continue
            current = settings()
            try:
                found = ([] if (current.live_model_only is True and not panel) or not current.live_check else
                         self.checker.quick(L.LiveText(text, languages)))
            except Exception:
                self._failed(0)
                found = []
            if profile(current) != profile(settings()):
                continue
            with self.lock:
                if version != self.version:
                    continue  # a newer text is waiting
                self.quick = found
            self._send()

    def _paused(self, pause, layer):
        """Runs layer once the text has not changed for pause seconds, once per text."""
        done = None
        while True:
            with self.lock:
                while (self.text is None or self.version == done) and not self.stopped:
                    self.lock.wait()
                version, changed_at = self.version, self.changed_at
                delay = LT_PAUSE if self.model_only and layer == self._layer2 else pause
                while self.version == version and time.monotonic() - changed_at < delay and not self.stopped:
                    self.lock.wait(max(0, delay - (time.monotonic() - changed_at)))
                if self.stopped:
                    return
                if self.version != version:
                    continue
                text, field = self.text, self.field
            done = version
            try:
                layer(version, field, text)
            except Exception:  # the thread must live on: the next text is checked again
                self._failed(1 if layer == self._layer1 else 2)

    def _marker(self):
        """Where the visible issues' words are (AT-SPI, gramadrama/geometry.py): after every change, and
        every MARKS_INTERVAL while there are some, as macOS re-reads the field. Sent when it changes."""
        last, enabled, read_at = None, True, 0.0
        while True:
            with self.lock:
                if not self.stopped and not self.carets:
                    self.lock.wait(MARKS_INTERVAL)
                if self.stopped:
                    return
                field, text, caret, visible = self.field, self.text, self.caret, list(self.visible)
                panel = self.panel
                carets, self.carets = self.carets, []
            for cid, sent in carets:  # a hotkey waits for these: before the underlines
                self._caret(cid, sent)
            if time.monotonic() - read_at > SETTINGS_SECONDS:
                current = settings()
                enabled, read_at = current.live_underlines, time.monotonic()
                self.refresh_settings(current)
                with self.lock:
                    visible = list(self.visible)
            view = None
            if enabled and text is not None and visible and not panel:
                try:
                    view = self._marks(text, caret, visible)
                except Exception:  # never the end of this thread: the next round tries again
                    self._failed(3)
            key = (field, caret, len(text or ''), json.dumps(view, sort_keys=True))
            if key != last:
                last = key
                emit(ev='marks', field=field, caret=caret, length=len(text or ''), view=view)

    def _geometry(self):
        if self.geometry is None:
            try:
                self.geometry = self.make_geometry()
            except Exception:
                self.geometry = False  # no AT-SPI here: the badge alone, as before
                emit(ev='log', event='live_marks_unavailable')
        return self.geometry

    def _caret(self, cid, sent):
        found = None
        try:
            if self._geometry():
                found = self.geometry.caret(sent)
        except Exception:
            self._failed(4)
        cursor, anchor = found or (None, None)
        emit(ev='caret', id=cid, cursor=cursor, anchor=anchor)

    def _marks(self, text, caret, visible):
        if not self._geometry():
            return None
        found = self.geometry.marks(text, caret, visible)
        if not found or not found['marks']:
            return None
        details = {i['id']: i for i in visible}
        found['marks'] = [dict(mark, **{k: details[mark['id']][k] for k in ('word', 'message', 'suggestions', 'kind', 'source',
                                                                             'rule_off')},
                               learn=details[mark['id']]['kind'] == 'spelling')
                          for mark in found['marks']]
        return found

    def _failed(self, layer):
        if layer not in self.failed:
            self.failed.add(layer)
            emit(ev='log', event='live_layer_failed', layer=layer)

    def _layer1(self, version, field, text):
        current = settings()
        if (not current.live_check or (current.live_model_only is True and not self.panel) or not current.live_languagetool
                or not core.languagetool_server(current)):
            return  # no LanguageTool set: layer 0 and the model are all there is
        server = backend(current, llm=False)
        if server is None:
            return
        prepared = L.LiveText(text, current.languages)
        token = None
        if any(part.strip() for part in prepared.parts.values()):
            token = self._start_checking(version)
            if token is None:
                return
        try:
            found = self.checker.languagetool(prepared, server, tuple(self.rules_off))
            if profile(current) != profile(settings()):
                return
            with self.lock:
                if found is None or version != self.version or self.stopped:
                    return
                self.lt = found
        finally:
            self._finish_checking(token)
        self._send()

    def _layer2(self, version, field, text):
        current = settings()
        if not current.live_check or self.panel:  # the panel's text came from the model
            return
        if current.live_model_only is True:
            self._model_only(version, field, text, current)
            return
        if not current.live_bielik:
            return
        cache_key = (profile(current), text)
        cached = self.cache.get(cache_key)
        if cached is None:
            if (not current.live_bielik or not core.model_server(current)[0] or len(text) > L.BIELIK_MAX_CHARS
                    or not text.strip() or '\ufffc' in text or time.monotonic() < self.backoff_until):
                return
            server = backend(current)
            if server is None:
                return
            cancel = threading.Event()
            with self.lock:
                if version != self.version:
                    return
                self.bielik_cancel = cancel
            token = self._start_checking(version)
            if token is None:
                return
            started = time.monotonic()
            try:
                try:
                    corrected = server.correct(text, 'fix', cancel=cancel)
                except core.Cancelled:
                    return  # the text changed: no log, no back-off
                except core.BackendError as exc:
                    self._model_failed(version, current, cancel, 'live_bielik_failed', text, exc)
                    return
                if cancel.is_set() or profile(current) != profile(settings()):
                    return
                protected = L.LiveText(text, current.languages).non_language
                cached = [i for i in L.bielik_issues(text, corrected) if not L.touches(protected, i.start, i.end)]
                with self.lock:
                    if cancel.is_set() or version != self.version or self.stopped:
                        return
                    if len(self.cache) >= CACHE:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[cache_key] = cached
                    self.bielik = cached
                emit(ev='log', event='live_bielik', chars=len(text), issues=len(cached),
                     ms=int((time.monotonic() - started) * 1000))
            finally:
                self._finish_checking(token)
        with self.lock:
            if version != self.version or self.stopped:
                return
            self.bielik = cached
        self._send()

    def _model_failed(self, version, current, cancel, event, text, exc):
        """A model check failed: no model checks for BACKOFF. True when this failure started that pause."""
        identity = profile(current)
        if identity != profile(settings()):
            return False
        with self.lock:
            # A failed request from the previous mode must not back off its replacement.
            if cancel.is_set() or version != self.version or self.stopped or self.profile != identity:
                return False
            self.backoff_until = time.monotonic() + BACKOFF
            emit(ev='log', event=event, chars=len(text), why=core.error_kind(exc))
        return True

    def _noted(self, version, note, send=True):
        """Model only: the field's note (paused_note, unchecked_note), shown with its issues; send=False when the end
        of a running check sends it anyway."""
        with self.lock:
            if version != self.version or self.stopped:
                return
            self.note = note
        if send:
            self._send()

    def _model_only(self, version, field, text, current):
        if not core.model_server(current)[0] or not text.strip() or '\ufffc' in text:
            return
        if time.monotonic() < self.backoff_until:
            self._noted(version, paused_note(current))  # every field in the pause says why it shows nothing (macOS)
            return
        # This route has no dependency on LT, including validation of its unused address.
        server = backend(dataclasses.replace(current, llm_think='off',
                                             languagetool_url='', languagetool_local_url=''))
        if server is None:
            return
        cancel = threading.Event()
        identity = profile(current)
        words = dictionary.user_words(current)
        started = time.monotonic()
        token = None

        def correct(fragment, event):
            nonlocal token
            if event.is_set():
                raise core.Cancelled()
            if token is None:
                token = self._start_checking(version)
                if token is None:
                    with self.lock:
                        if not self._joined_locked():
                            raise core.Cancelled()
            return model_live.correct_fragment(server, fragment, event, words=words)

        try:
            try:
                with self.model_lock:
                    with self.lock:
                        if version != self.version or self.stopped:
                            return
                        self.bielik_cancel = cancel
                        self.active_model = (identity, text, cancel, version)
                    try:
                        result = self.fragments.check(text, correct, identity, cancel, tolerate=True)
                    finally:
                        with self.lock:
                            self.active_model = None
            except core.Cancelled:
                return
            except core.BackendError as exc:
                if self._model_failed(version, current, cancel, 'live_model_failed', text, exc):
                    # the issues found before stay, as on macOS; the check's end (finally) sends the note
                    self._noted(version, paused_note(current), send=token is None)
                return
            if cancel.is_set() or identity != profile(settings()):
                return
            with self.lock:
                if version != self.version or self.stopped:
                    return
                self.bielik = result.issues
                self.note = unchecked_note(result.rejected)
            emit(ev='log', event='live_model', chars=len(text), issues=len(result.issues),
                 fragments=result.fragments, cache_hits=result.cache_hits, model_calls=result.model_calls,
                 rejected=result.rejected, ms=int((time.monotonic() - started) * 1000))
        finally:
            self._finish_checking(token)
        self._send()


def serve():
    checker = Checker()
    monitor = Monitor(checker)
    monitor.run()
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except ValueError:
            continue
        name = command.get('cmd')
        if name == 'text' and isinstance(command.get('text'), str):
            text = command['text'][:L.MAX_CHARS]
            caret = command.get('caret')
            revision = command.get('revision')
            monitor.text_changed(command.get('field'), text, caret if isinstance(caret, int) else None,
                                 revision if type(revision) is int else None, panel=command.get('panel') is True)
        elif name == 'correct' and type(command.get('id')) is int and isinstance(command.get('text'), str):
            monitor.request_correction(command['id'], command['text'])
        elif name == 'cancel_correct' and type(command.get('id')) is int:
            monitor.cancel_correction(command['id'])
        elif name == 'clear':
            monitor.clear()
        elif name == 'fixed':
            monitor.fixed(command.get('field'), command.get('start'), command.get('end'))
        elif name == 'learn' and isinstance(command.get('word'), str) and command['word'].strip():
            language = str(command.get('rule', '')).rsplit('_', 1)[-1].lower()
            if language not in monitor.languages:
                language = core.detect_language(command['word'], monitor.languages)
                if language == 'mixed':
                    language = monitor.languages[0]
            checker.learn(command['word'], language)
            monitor.learned(command['word'])
        elif name == 'ignore' and isinstance(command.get('word'), str) and isinstance(command.get('rule'), str):
            monitor.ignore(command['word'], command['rule'])
        elif name == 'rule_off' and isinstance(command.get('rule'), str):
            monitor.turn_off(command['rule'])
        elif name == 'caret' and isinstance(command.get('id'), int) and isinstance(command.get('text'), str):
            monitor.ask_caret(command['id'], command['text'])


def lint(text, with_bielik=False):
    """python3 -m gramadrama.liveservice --lint "tekst" [--bielik]: the layers from the command line, with times
    (macOS --lint)."""
    started = time.monotonic()
    checker = Checker()
    current = settings()
    checker.refresh(current)
    live_text = L.LiveText(text, current.languages)
    prepared = time.monotonic()
    print('Zdania:')
    for a, b, lang in L.sentences(text, languages=current.languages):
        print('  [%s] %s' % (lang.upper(), text[a:b].strip()))
    quick = checker.quick(live_text)
    cold = time.monotonic()
    checker.quick(live_text)
    warm = time.monotonic()
    server = backend(settings(), llm=False)
    lt = checker.languagetool(live_text, server, tuple(dictionary.rules_off())) if server is not None else None
    lt_done = time.monotonic()
    bielik = None
    model_server = backend(settings()) if with_bielik else None
    if model_server is not None:
        try:
            bielik = L.bielik_issues(text, model_server.correct(text, 'fix'))
        except core.BackendError:
            bielik = None
    done = time.monotonic()

    def ms(a, b):
        return '%d ms' % ((b - a) * 1000)
    print('Warstwa 0: %d błędów · %s przygotowanie · %s (zimny) · %s (ciepły)' % (len(quick), ms(started, prepared),
                                                                               ms(prepared, cold), ms(cold, warm)))
    print('Warstwa 1: ' + ('%d błędów · %s' % (len(lt), ms(warm, lt_done)) if lt is not None else 'LanguageTool nie odpowiada'))
    if with_bielik:
        print('Warstwa 2: ' + ('%d zmian Bielika · %s' % (len(bielik), ms(lt_done, done)) if bielik is not None
                               else 'Bielik nie odpowiada'))
    for issue in L.merge(quick, lt or [], bielik or []):
        hint = ' → ' + ' / '.join(issue.suggestions) if issue.suggestions else ''
        print('  %d+%d „%s”%s · %s [%s: %s]' % (issue.start, issue.end - issue.start, issue.word, hint, issue.message,
                                             issue.source, issue.rule))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if '--lint' in argv:
        rest = [a for a in argv if not a.startswith('--')]
        lint(' '.join(rest), '--bielik' in argv)
        return 0
    serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
