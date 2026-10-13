"""Correction session state machine and the EngineLogic facade used by the IBus adapter.

All decisions live here and in the other pure modules; the adapter (main.py) only feeds vfunc
events into EngineLogic and executes the returned effects in order (pure; no gi)."""
import dataclasses
import itertools
from . import applist, browser_text as BT, dictate, fallback_ui, keys, live, plan, problems, textdiff
from .editbuf import EditBuffer
from .context import CONFIRMS, ConfirmCaret, FieldContext, Refusal

IDLE, WAITING, READY, ERROR, APPLYING = 'idle', 'waiting', 'ready', 'error', 'applying'
VERIFY_MS = 300
# A hotkey that doubts only the caret asks AT-SPI through the live service (ConfirmCaret, docs/architecture.md, Text safety);
# its answer comes in milliseconds, a service that has to start first takes about 0.1 s more.
CONFIRM_MS = 800
CONFIRMABLE = ('stale', 'selection_unknown')
# A refused click on a live check's suggestion says what to do, as a refused hotkey does (fallback_ui.MESSAGES);
# any other reason means the field has changed under the suggestion.
FIX_MESSAGES = {'stale': 'stale', 'selection_unknown': 'selection_unknown', 'selection': 'fix_selection',
                'no_field': 'no_field', 'sensitive': 'sensitive', 'terminal': 'terminal', 'no_text': 'no_text'}
IDLE_TIMEOUT_MS = 5 * 60 * 1000
MODE_KEYS = tuple(key for key, _ in fallback_ui.MODES)
CLIPS = itertools.count(1)  # copy and paste requests of the panel's editor, one numbering for the process
PASTE_MAX = 20000  # characters a paste into the edited result takes at most
SUGGESTIONS = 3  # of an issue in the edited text, under it (macOS issueBar)
# The edited text is diffed against the original after every key to mark what it changes (the panel's sage marks):
# above this many characters the marks are left out rather than slow the typing down.
MARKS_MAX = 3000
ASKS = itertools.count(1)  # the ids of a hotkey's question to a browser's extension, one numbering for the process


# Effects: plain data, executed in list order by the adapter.
@dataclasses.dataclass(frozen=True)
class StartBackend:
    id: int
    text: str
    mode: str  # 'fix', 'improve', 'formal', 'casual', 'shorter' or 'translate'
    target: str = None  # 'translate' only: 'en' or 'pl'; None lets the backend detect it


@dataclasses.dataclass(frozen=True)
class CancelBackend:
    id: int


@dataclasses.dataclass(frozen=True)
class ShowUI:
    view: fallback_ui.View  # aux, lines = fallback_ui.render(view); then EngineLogic.ui_flushed(True)


@dataclasses.dataclass(frozen=True)
class HideUI:
    pass


@dataclasses.dataclass(frozen=True)
class Message:
    reason: str  # show fallback_ui.message(reason) as aux text, no lookup table
    seconds: float


@dataclasses.dataclass(frozen=True)
class Apply:
    ops: plan.Ops  # delete_surrounding_text(*ops.delete) then commit_text(ops.commit), same turn


@dataclasses.dataclass(frozen=True)
class ScheduleConfirmTimeout:
    id: int
    ms: int


@dataclasses.dataclass(frozen=True)
class ScheduleVerifyTimeout:
    id: int
    ms: int


@dataclasses.dataclass(frozen=True)
class ScheduleIdleTimeout:
    id: int
    ms: int


@dataclasses.dataclass(frozen=True)
class ResetCompose:
    pass  # chain up IBus.EngineSimple.do_reset: drop a half-typed dead key or compose sequence


@dataclasses.dataclass(frozen=True)
class ChainUp:
    pass  # return IBus.EngineSimple.do_process_key_event(...) for the current key


@dataclasses.dataclass(frozen=True)
class Consume:
    pass  # return True for the current key


@dataclasses.dataclass(frozen=True)
class OpenApp:
    args: tuple  # the GramaDrama app's command line after its name: '--show=<tab>' or '--login' (gramadrama/app.py)


@dataclasses.dataclass(frozen=True)
class Log:
    event: str
    data: dict  # counts, offsets, ids, reason codes and app IDs only; never text


class Session:
    """One correction at a time: IDLE -> WAITING -> READY/ERROR -> APPLYING -> IDLE.

    Every input returns a list of effects. Session ids grow monotonically and backend results
    or timers carrying another id are ignored."""

    def __init__(self, ctx, mode_index=0):
        self.ctx = ctx
        self.mode_index = mode_index  # the session's mode; every hotkey sets its own
        self.state = IDLE
        self.id = 0
        self.popup_visible = False  # reported by the adapter once it has drawn or hidden the popup
        self.in_panel = False  # the GNOME panel shows the session and can draw an edited result (EDIT_VERSION)
        self.problem_ui = False  # the GNOME panel shows an error's buttons (PROBLEM_VERSION): Enter does the first one
        self.confirming = None  # (id, mode, reason, ctx version, focus_gen) of a hotkey waiting for AT-SPI
        self.asking = None  # (id, mode, focus_gen, browser) of a hotkey waiting for the text of a page (browser_text)
        self._reset_fields()

    def _reset_fields(self):
        self.capture = self.result = self.expected = None
        self.rows = ()
        self.error = self.note = ''
        self.problem = None  # (problems.Problem, its sentence) of an ERROR: the panel's title and buttons
        self.retrying = False  # this run is the quiet second try after a slip of the model (problems.RETRIED)
        self.page = 0
        self.apply_on_ready = self.echo_mismatch = False
        self.quiet = False  # a live check's single fix: no panel, no "Zastosowano"
        self.translation_languages = ('pl', 'en')
        self.target = None  # translation direction: chosen by the user (Ctrl+6 again) or reported by the backend
        # The app and window kind of the capture (ctx at the hotkey): start, apply and verified
        # carry them, so a verification after the focus has moved on still names the right app.
        self.app = self.win = None
        self._reset_result()

    def _reset_result(self):
        self.text = ''  # the model's text so far, then the result (the panel shows it)
        self.segments, self.changes, self.count = (), (), 0
        # The backend's diff and change list, and the changes undone in the panel (their [first, last) segment
        # ranges): segments, changes, count, text and result are made from them (revert).
        self.base_segments, self.base_changes, self.base_count = (), (), 0
        self.reverted = frozenset()
        self.editing = None  # an EditBuffer while the result is edited in the panel (Edytuj)
        self.clip = None  # (id, text) the panel puts on the clipboard (Ctrl+C, Ctrl+X)
        self.paste = 0  # the id of a paste the panel is asked for (it reads the clipboard), 0 for none
        # Checking while editing (macOS EditorChecker): the editor's field for the live service, the (text, caret) sent
        # last, and the issues it found with the text they are about.
        self.edit_field, self.edit_sent, self.edit_issues, self.edit_checked, self.edit_checking = 0, None, (), None, False
        self.changes_open = False  # the change list under the result, shown on demand (Ctrl+I, the panel's Pokaż / Ukryj)
        self._marks = (None, [])  # the edited text and what it changes against the original (_edit_marks)

    @property
    def is_modal(self):
        """Only a running session whose popup is on screen consumes Enter, Esc, Ctrl+1..6 and
        PgUp/PgDn; until the adapter reports it drawn, keys behave as without GramaDrama."""
        return self.state != IDLE and self.popup_visible

    @property
    def translating(self):
        return MODE_KEYS[self.mode_index] == 'translate'

    @property
    def source_language(self):
        return next((code for code in self.translation_languages if code != self.target), None) if self.target else None

    def panel_view(self):
        """The panel's state as JSON-able data (docs/architecture.md, View JSON); only while a session runs."""
        status = self.view().status
        if status == 'working' and not self.text:
            status = 'checking'  # nothing from the model yet
        view = {'v': 1, 'session': self.id, 'kind': 'session', 'status': status, 'mode': self.mode_index,
                'target': self.target if self.translating else None, 'source_language': self.source_language, 'scope': self.capture.scope,
                'source': self.capture.source, 'text': self.text, 'segments': [list(s) for s in self.segments],
                'changes': [self._panel_change(c) for c in self.changes], 'count': self.count,
                'note': self.note, 'error': self.error, 'message': '', 'can_apply': True,
                'editable': self.state == READY and self.in_panel, 'changes_open': self.changes_open,
                'retrying': self.state == WAITING and self.retrying}
        if self.state == ERROR and self.problem is not None:
            view['problem'] = problems.view(*self.problem)
        if self.editing is not None:  # the edited text with its caret instead of the diff (Kopiuj copies it)
            b = self.editing
            view.update(text=b.text, segments=[], changes=[],
                        editing={'cursor': b.cursor, 'anchor': b.anchor, 'marks': self._edit_marks(b.text)})
            if self.clip is not None:
                view['editing']['clipboard'] = {'id': self.clip[0], 'text': self.clip[1]}
            if self.paste:
                view['editing']['paste'] = self.paste
            if self.edit_checked == b.text:  # issues found in another text would stand under the wrong words
                view['editing']['issues'] = [
                    {key: i.get(key) for key in ('id', 'start', 'end', 'word', 'message', 'kind')}
                    | {'suggestions': list(i.get('suggestions') or [])[:SUGGESTIONS], 'learn': _learnable(i),
                       'rule_off': i.get('rule_off') is True}
                    for i in self.edit_issues]
            view['editing']['checking'] = self.edit_checking
        return view

    def _panel_change(self, change):
        """A change-list row for the panel: from, to, reason, and in a finished correction the id that Cofnij
        (revert:<id>) sends back, with keep when Zostaw i dodaj do słownika (keep:<id>) is offered too."""
        row = {key: change.get(key, '') for key in ('from', 'to', 'reason')}
        span = _range(change)
        if span is not None and self.state == READY and not self.translating:
            row.update(id=span[0], keep=word_change(change))
        return row

    def view(self):
        if self.state == WAITING:
            status = 'apply_pending' if self.apply_on_ready else 'working'
        elif self.state == READY:
            status = 'unchanged' if self.result == self.capture.source else 'ready'
        else:
            status = {ERROR: 'error', APPLYING: 'applying'}.get(self.state, 'working')
        return fallback_ui.View(self.mode_index, status, self.rows, self.error, self.page, self.note, self.target, self.source_language)

    def _end(self, effects, why=None):
        if why:
            effects = effects + [Log('end', {'id': self.id, 'state': self.state, 'why': why})]
        if self.state == WAITING:
            effects = [CancelBackend(self.id)] + effects
        self.state = IDLE
        self._reset_fields()
        return effects

    def _start(self, retry=False):
        """A new run of the backend on the capture; retry: the quiet second try after a slip of the model."""
        self.id += 1
        self.state = WAITING
        self.retrying = retry
        self.result, self.error, self.note, self.page = None, '', '', 0
        self.rows = ()  # the backend's first partial brings the lines
        self.apply_on_ready = False
        self._reset_result()
        target = self.target if MODE_KEYS[self.mode_index] == 'translate' else None
        return [StartBackend(self.id, self.capture.source, MODE_KEYS[self.mode_index], target), ShowUI(self.view()),
                ScheduleIdleTimeout(self.id, IDLE_TIMEOUT_MS),
                Log('start', {'id': self.id, 'scope': self.capture.scope, 'chars': len(self.capture.source),
                              'mode': MODE_KEYS[self.mode_index], 'app': self.app, 'win': self.win})]

    @property
    def in_page(self):
        """The text of this session came from a page, through the browser's extension: it goes back the same way."""
        return isinstance(self.capture, BT.Capture)

    def hotkey(self, mode='fix', browser=None):
        """Ctrl+Alt+G (the app's mode, 'fix' unless apps.json says otherwise) or Ctrl+Alt+J ('translate'):
        restart from a fresh capture in that mode, as ⌥⌘G and ⌥⌘T on macOS, or show a refusal (never modal).
        browser: the focused window is a browser whose extension is connected; the page is asked for its text first, as
        on the Mac (page_text), and IBus is the way only when the page has no field for it."""
        effects = [ResetCompose()]
        if self.state != IDLE:
            effects = self._end(effects, 'hotkey')
        effects += self.drop_confirm('hotkey')
        if browser is not None:
            aid = next(ASKS)
            self.asking = (aid, mode, self.ctx.focus_gen, browser)
            return effects + [BT.BrowserRequest('hotkey', aid, browser, 'context', {})]
        return effects + self._capture(mode)

    def _capture(self, mode):
        """The hotkey the IBus way: the field as IBus has it, or AT-SPI asked about its caret, or a refusal."""
        captured = plan.capture(self.ctx.snapshot())
        if isinstance(captured, Refusal):
            if captured.reason in CONFIRMABLE and self.ctx.confirmable():
                cid = next(CONFIRMS)
                self.confirming = (cid, mode, captured.reason, self.ctx.version, self.ctx.focus_gen)
                return [ConfirmCaret(cid, self.ctx.text), ScheduleConfirmTimeout(cid, CONFIRM_MS)]
            return self._refused(captured.reason)
        return self._begin(captured, mode)

    def drop_confirm(self, why):
        """The user moved on while AT-SPI or the page was asked: that hotkey is over, without a message."""
        effects = []
        if self.asking is not None:
            self.asking = None
            effects.append(Log('browser_dropped', {'why': why}))
        if self.confirming is not None:
            self.confirming = None
            effects.append(Log('caret_dropped', {'why': why}))
        return effects

    def page_text(self, aid, answer):
        """The extension's answer to the hotkey's `context`: the session starts on the page's text, or the hotkey goes the
        IBus way (no field in the page, no answer), or it is refused (a password field, nothing to correct)."""
        pending = self.asking
        if pending is None or pending[0] != aid:
            return []
        self.asking = None
        if self.state != IDLE:  # a live check's fix went in meanwhile
            return [Log('browser_dropped', {'why': 'busy'})]
        if pending[2] != self.ctx.focus_gen:
            return [Log('browser_dropped', {'why': 'focus'})]
        _, mode, _, browser = pending
        captured = BT.capture(answer, browser)
        if captured == BT.FALLBACK:
            why = answer.get('reason') if not answer.get('ok') else 'scope'
            return [Log('browser_fallback', {'reason': str(why or 'unknown')[:40], 'app': self.ctx.app})] + self._capture(mode)
        if isinstance(captured, Refusal):
            return self._refused(captured.reason, via='extension')
        return self._begin(captured, mode)

    def _refused(self, reason, **data):
        return [Message(reason, 3),
                Log('refused', dict({'at': 'hotkey', 'reason': reason, 'app': self.ctx.app, 'win': self.ctx.app_kind},
                                    **data))]

    def _begin(self, captured, mode):
        self.capture, self.target = captured, None  # a new text: detect its direction again
        self.app, self.win = self.ctx.app, self.ctx.app_kind
        self.mode_index = MODE_KEYS.index(mode)
        return self._start()

    def caret_confirmed(self, cid, cursor, anchor):
        """The live service's answer to ConfirmCaret: where AT-SPI sees the caret in the text sent, or None.
        The hotkey goes on only when that is the state IBus has, in the same field with nothing changed in
        it meanwhile (FieldContext.confirm); otherwise it is refused as it was."""
        pending = self.confirming
        if pending is None or pending[0] != cid:
            return []
        if self.state != IDLE:  # a live check's fix went in meanwhile
            return self.drop_confirm('busy')
        if pending[4] != self.ctx.focus_gen:
            return self.drop_confirm('focus')
        self.confirming = None
        _, mode, reason, version, _ = pending
        outcome = self.ctx.confirm(version, cursor, anchor)
        captured = plan.capture(self.ctx.snapshot())
        if isinstance(captured, Refusal):
            return self._refused(captured.reason, atspi=outcome)
        return [Log('caret_confirmed', {'reason': reason, 'app': self.ctx.app})] + self._begin(captured, mode)

    def confirm_timeout(self, cid):
        pending = self.confirming
        if pending is None or pending[0] != cid:
            return []
        if self.state != IDLE:
            return self.drop_confirm('busy')
        self.confirming = None
        return self._refused(pending[2], atspi='timeout')

    def accept(self):
        if self.editing is not None:
            return self._apply_edit()
        if self.state == WAITING:
            self.apply_on_ready = True
            return [ShowUI(self.view())]
        if self.state == READY:
            return self._apply()
        if self.state == ERROR:
            if self.problem_ui and self.problem is not None and self.problem[0].actions:
                return self.problem_action(self.problem[0].actions[0])  # the panel shows it as Enter's
            return self._end([HideUI()], 'closed')
        return []  # APPLYING: a second Enter is ignored

    def problem_action(self, action):
        """A button of a failed correction (problems.ACTIONS; Enter is the first one): retry it, or open the app where
        the cause can be fixed and close the panel. Only the buttons the error offers."""
        if self.state != ERROR or self.problem is None or action not in self.problem[0].actions:
            return []
        log = Log('problem_action', {'id': self.id, 'action': action})
        if action == problems.RETRY:
            return [log] + self._start()
        args = ('--login',) if action == problems.LOGIN else ('--show=' + action[len('open:'):],)
        return [log, OpenApp(args)] + self._end([HideUI()], 'opened')

    def _apply(self):
        if self.in_page:
            return self._apply_page()
        ops = plan.replace_ops(self.capture, self.ctx.snapshot(), self.result)
        if isinstance(ops, Refusal):
            return self._end([Message(ops.reason, 4), Log('refused', {'at': 'apply', 'reason': ops.reason})])
        if ops.nothing:
            return self._end([HideUI()], 'unchanged')
        self.expected = plan.expected(self.capture, ops)
        self.state = APPLYING
        self.ctx.expect_update()
        return [Apply(ops), ShowUI(self.view()), ScheduleVerifyTimeout(self.id, VERIFY_MS),
                Log('apply', {'id': self.id, 'scope': self.capture.scope,
                              'delete': ops.delete[1] if ops.delete else 0,
                              'commit': len(ops.commit) if ops.commit else 0, 'app': self.app, 'win': self.win})]

    def _apply_page(self):
        """The correction goes back through the extension, word by word, into the field the text came from; the extension
        checks that the text there is still what was taken, and says whether the result landed (page_written)."""
        c = self.capture
        if plan.bad_result(self.result):
            return self._end([Message('bad_result', 4), Log('refused', {'at': 'apply', 'reason': 'bad_result'})])
        planned = BT.edits(c.source, self.result, c.location)
        if isinstance(planned, Refusal):
            return self._end([Message(planned.reason, 4), Log('refused', {'at': 'apply', 'reason': planned.reason})])
        if planned is None:
            return self._end([HideUI()], 'unchanged')
        changes, expected = planned
        self.state = APPLYING
        self.ctx.expect_update()  # what IBus knew of the field is old now
        fields = {'client': c.client, 'check': {'location': c.location, 'length': c.length, 'text': c.source},
                  'edits': changes, 'expected': expected}
        return [BT.BrowserRequest('apply', self.id, c.browser, 'replace', fields, BT.WRITE_SECONDS, BT.WRITE_DEADLINE),
                ShowUI(self.view()),
                Log('apply', {'id': self.id, 'scope': c.scope, 'edits': len(changes), 'via': 'extension', 'app': self.app,
                              'win': self.win})]

    def page_written(self, sid, answer):
        """The extension's answer to the correction's `replace`."""
        if sid != self.id or self.state != APPLYING or not self.in_page:
            return []
        outcome = BT.outcome(answer)
        data = {'id': self.id, 'outcome': {'landed': 'ok', 'refused': 'refused'}.get(outcome, 'unverified'),
                'via': 'extension', 'app': self.app, 'win': self.win}
        if outcome != 'landed':
            data['reason'] = str(answer.get('reason') or 'unknown')[:40]
        message = {'landed': Message('applied', 1.5), 'refused': Message('changed', 4)}.get(outcome,
                                                                                         Message('verify_mismatch', 4))
        return self._end([message, Log('verified', data)])

    def fix(self, capture, corrected, data):
        """A live check's single fix (docs/live-check.md): straight to APPLYING, without the panel, and
        verified by the echo like a correction."""
        if self.state != IDLE:
            return [Log('live_fix', dict(data, outcome='busy'))]
        ops = plan.replace_ops(capture, self.ctx.snapshot(), corrected)
        if isinstance(ops, Refusal):
            return [Message('fix_failed', 3), Log('live_fix', dict(data, outcome=ops.reason))]
        if ops.nothing:
            return []
        self.id += 1
        self.capture, self.result, self.quiet = capture, corrected, True
        self.app, self.win = self.ctx.app, self.ctx.app_kind
        self.expected = plan.expected(capture, ops)
        self.state = APPLYING
        self.ctx.expect_update()
        return [Apply(ops), ScheduleVerifyTimeout(self.id, VERIFY_MS),
                Log('live_fix', dict(data, outcome='sent', delete=ops.delete[1] if ops.delete else 0, app=self.app))]

    def cancel(self):
        return self._end([HideUI()], 'cancel')

    def panel_hidden(self):
        """Nothing of this session is on screen any more: it must not keep capturing Enter/Esc."""
        if self.state == IDLE:
            return []
        return self._end([], 'hidden')

    def copied(self):
        """The panel's Kopiuj button put the result on the clipboard: close, as on macOS."""
        if self.state not in (READY, ERROR):
            return []
        return self._end([Message('copied', 1.5)], 'copied')

    def cancel_and_pass(self):
        return self._end([HideUI()], 'key') + [ChainUp()]

    def mode(self, n):
        """Ctrl+1..6: restart the backend on the same capture in another mode. Ctrl+6 while already
        translating reverses the direction (PL -> EN or EN -> PL). Not while editing: the edits would go."""
        if self.state not in (WAITING, READY, ERROR) or self.editing is not None:
            return []
        if n == self.mode_index:
            if MODE_KEYS[n] != 'translate' or self.target is None:
                return []  # the direction is known once the backend has reported it
            if len(self.translation_languages) < 2 or self.target not in self.translation_languages:
                return []
            self.target = self.source_language
        effects = [CancelBackend(self.id)] if self.state == WAITING else []
        self.mode_index = n
        return effects + self._start()

    def direction(self):
        """Ctrl+D (⌘D on macOS) or the panel's direction button: Ctrl+6 again."""
        return self.mode(self.mode_index) if self.translating else []

    def scroll(self, delta):
        if self.state == IDLE:
            return []
        page = min(max(self.page + delta, 0), fallback_ui.page_count(self.view()) - 1)  # O(1)
        if page == self.page:
            return []
        self.page = page
        return [ShowUI(self.view())]

    def _info(self, info):
        info = info or {}
        languages = info.get('languages')
        if (isinstance(languages, (list, tuple)) and 1 <= len(languages) <= 2
                and all(code in ('pl', 'en', 'de') for code in languages) and len(set(languages)) == len(languages)):
            self.translation_languages = tuple(languages)
        if info.get('target') in self.translation_languages and MODE_KEYS[self.mode_index] == 'translate':
            self.target = info['target']
        note = info.get('note') or ''
        self.note = note if _encodable(note) else ''

    def backend_partial(self, sid, text, rows, info=None):
        """rows: fallback_ui.lines() of what to show so far, wrapped by the backend's worker.
        info: {'target': 'en'|'pl'|None, 'note': str} (see backend.ProcessBackend)."""
        if sid != self.id or self.state != WAITING or not _encodable(*rows):
            return []
        self.rows = tuple(rows)
        self.text = text
        self._info(info)
        return [ShowUI(self.view())]

    def backend_result(self, sid, corrected, rows, info=None):
        """rows: fallback_ui.lines() of the diff between the source and corrected."""
        if sid != self.id or self.state != WAITING:
            return []
        if not _encodable(corrected, *rows):  # could never be shown or sent to the client
            return self.backend_error(sid, fallback_ui.message('bad_result'), 'bad_result')
        self.result, self.rows, self.page = corrected, tuple(rows), 0
        self._info(info)
        info = info or {}
        self.text = corrected
        self.base_segments = self.segments = tuple(tuple(s) for s in info.get('segments') or ())
        self.base_changes = tuple(info.get('changes') or ())
        self.base_count = info.get('count') or len(self.base_changes)
        self.changes, self.count = self._current_changes()
        self.state = READY
        log = Log('result', {'id': sid, 'chars': len(corrected), 'unchanged': corrected == self.capture.source})
        if self.apply_on_ready:
            return [log] + self._apply()
        return [log, ShowUI(self.view())]

    def backend_changes(self, sid, changes, count):
        """LanguageTool's reasons for the change list, when it answered after the result."""
        if sid != self.id or self.state != READY:
            return []
        self.base_changes, self.base_count = tuple(changes), count or len(changes)
        self.changes, self.count = self._current_changes()  # a change undone meanwhile stays undone
        return [ShowUI(self.view())]

    def revert(self, change_id, keep=False):
        """The panel's Cofnij on a change (its id: the first segment of its range): only that change goes back to
        the original, the other corrections stay (macOS Diff.text(reverting:)). keep: Zostaw i dodaj do słownika,
        offered for a change of one word to another: the original word is also added to the user's dictionary."""
        if self.state != READY or self.translating:
            return []
        change = next((c for c in self.base_changes if _range(c) and _range(c)[0] == change_id), None)
        if change is None or _range(change) in self.reverted or (keep and not word_change(change)):
            return []
        self.reverted = self.reverted | {_range(change)}
        self.segments = revert_segments(self.base_segments, self.reverted)
        self.result = self.text = ''.join(text for kind, text in self.segments if kind != 'removed')
        self.rows = tuple(fallback_ui.lines(self.segments))
        self.changes, self.count = self._current_changes()
        effects = [ShowUI(self.view()), Log('revert', {'id': self.id, 'keep': keep, 'count': self.count})]  # never the word
        if keep:
            effects.append(live.Command({'cmd': 'learn', 'word': change['from'], 'rule': ''}))
        return effects

    def toggle_changes(self):
        """Ctrl+I or the panel's Pokaż / Ukryj zmiany: the change list under a finished correction, on demand."""
        if self.state != READY or not self.is_modal or self.editing is not None or self.translating or not self.changes:
            return []
        self.changes_open = not self.changes_open
        return [ShowUI(self.view()), Log('changes', {'id': self.id, 'open': self.changes_open})]

    def _edit_marks(self, text):
        """What the edited text changes against the original as [start, end) code point ranges, for the panel's sage
        marks while editing: the corrections that stay and what the user typed. None in Tłumacz (a translation is not
        a correction of its original) and none for a text too long to diff after every key."""
        if self.translating or len(text) > MARKS_MAX:
            return []
        if self._marks[0] != text:
            marks, at = [], 0
            for kind, part in textdiff.diff_parts(self.capture.source, text):
                if kind == 'removed':
                    continue
                if kind == 'added':
                    marks.append([at, at + len(part)])
                at += len(part)
            self._marks = (text, marks)
        return [list(m) for m in self._marks[1]]

    # ---- editing the result in the panel (branding/PANEL.md, Edycja) ----
    @property
    def editable(self):
        """A finished result in the panel, not being edited: Ctrl+E, Edytuj, a letter or Backspace start editing."""
        return self.state == READY and self.in_panel and self.editing is None and self.is_modal

    def edit_toggle(self):
        """Ctrl+E or the panel's Edytuj / Gotowe: start editing with the caret at the end, or end it."""
        if self.editing is not None:
            return self._finish_edit()
        if not self.editable:
            return []
        return self._begin_edit()

    def _begin_edit(self, offset=None):
        self.editing = EditBuffer(self.result)
        if offset is not None:
            self.editing.place(offset)
        self.edit_field = next(live.FIELDS)  # one numbering with the fields of the app: no answer passes for another's
        self.edit_sent, self.edit_issues, self.edit_checked, self.edit_checking = None, (), None, False
        return [ShowUI(self.view()), Log('edit', {'id': self.id})]

    def edit_at(self, offset):
        """A click in the result (the panel's edit_at:<offset in the result>): editing starts with the caret there, as
        on macOS, or the caret of the result being edited moves there."""
        if self.editing is not None:
            return [ShowUI(self.view())] if self.editing.place(offset) else []
        if not self.editable:
            return []
        return self._begin_edit(offset)

    def edit_key(self, keyval, state, char=None):
        """A key for the editor (keys.EDIT_KEY): typing, moves, deletions, Shift+Enter a new line, Enter inserts the
        result, Esc ends editing. In a finished result a letter or Backspace starts editing first (macOS).
        char: the character the key types, from the adapter (IBus.keyval_to_unicode), or None."""
        started = []
        if self.editing is None:
            started = [e for e in self.edit_toggle() if isinstance(e, Log)]  # the view follows with the key in it
            if self.editing is None:
                return []
        b, mods = self.editing, state & keys.RELEVANT
        ctrl, shift = bool(mods & keys.CONTROL), bool(mods & keys.SHIFT)
        if keyval in (keys.RETURN, keys.KP_ENTER) and not ctrl:
            if not shift:
                return started + self._apply_edit()
            changed = b.insert('\n')
        elif keyval == keys.ESCAPE:
            return started + self._finish_edit()
        elif keyval in MOVES:
            where = MOVES[keyval]
            if ctrl:
                where = {'left': 'word_left', 'right': 'word_right', 'home': 'start', 'end': 'finish'}.get(where, where)
            changed = b.move(where, extend=shift)
        elif keyval == keys.BACKSPACE:
            changed = b.delete(word=ctrl)
        elif keyval in (keys.DELETE, keys.KP_DELETE) and not (shift and not ctrl):  # Shift+Delete cuts, below
            changed = b.delete(forward=True, word=ctrl)
        elif ctrl and keyval in (keys.KEY_a, keys.KEY_A):
            changed = b.select_all()
        elif ctrl and keyval in (keys.KEY_z, keys.KEY_Z):
            changed = b.redo() if shift else b.undo()
        elif ctrl and keyval in (keys.KEY_y, keys.KEY_Y):
            changed = b.redo()
        elif (ctrl and keyval in (keys.KEY_c, keys.KEY_C, keys.KEY_x, keys.KEY_X)) or (keyval in (keys.INSERT, keys.KP_INSERT)
                                                                                          and ctrl and not shift):
            changed = b.has_selection  # Ctrl+C, Ctrl+X, Ctrl+Insert: the panel puts the selection on the clipboard
            if changed:
                self.clip = (next(CLIPS), b.selected())
                if keyval in (keys.KEY_x, keys.KEY_X):
                    b.delete()
        elif shift and not ctrl and keyval in (keys.DELETE, keys.KP_DELETE):  # Shift+Delete: cut
            changed = b.has_selection
            if changed:
                self.clip = (next(CLIPS), b.selected())
                b.delete()
        elif (ctrl and keyval in (keys.KEY_v, keys.KEY_V)) or (shift and not ctrl and keyval in (keys.INSERT, keys.KP_INSERT)):
            self.paste = next(CLIPS)  # the panel reads the clipboard and answers (edit_paste)
            changed = True
        elif char and not mods & (keys.CONTROL | keys.MOD1 | keys.NO_TEXT_MODS) and char.isprintable():
            changed = b.insert(char)
        else:
            changed = False  # consumed: Tab, PgUp, Alt chords and the like do nothing here
        return started + ([ShowUI(self.view())] if changed or started else [])

    def edit_paste(self, paste_id, text):
        """The panel's answer to a paste: the clipboard's text, put in place of the selection."""
        if self.editing is None or paste_id != self.paste or not isinstance(text, str):
            return []
        self.paste = 0
        if not text or len(text) > PASTE_MAX or not _encodable(text):
            return []
        self.editing.insert(text)
        return [ShowUI(self.view())]

    def edit_check(self, enabled):
        """The edited text for the live service (macOS EditorChecker): the dictionary at once, LanguageTool after a
        pause, never the model (the text came from it). The caret goes with it only while a word is being typed, so
        that word is not marked yet; after a move or a click the word under the caret shows its issue."""
        b = self.editing
        if b is None or not enabled:
            return []
        caret = b.cursor if b.typing and not b.has_selection else None
        if (b.text, caret) == self.edit_sent:
            return []
        self.edit_sent = (b.text, caret)
        return [live.Check(self.edit_field, b.text, caret, panel=True)]

    def edit_found(self, issues, checking):
        """The live service's issues in the edited text, as it was sent last."""
        if self.editing is None or self.edit_sent is None:
            return []
        found = tuple(i for i in issues if isinstance(i, dict) and type(i.get('start')) is int and type(i.get('end')) is int)
        state = (found, self.edit_sent[0], checking is True)
        if state == (self.edit_issues, self.edit_checked, self.edit_checking):
            return []
        self.edit_issues, self.edit_checked, self.edit_checking = state
        return [ShowUI(self.view())]

    def _edit_issue(self, issue_id):
        """An issue of the edited text as it is now (the text changed since: none)."""
        if self.editing is None or self.edit_checked != self.editing.text:
            return None
        issue = next((i for i in self.edit_issues if i.get('id') == issue_id), None)
        if issue is None or self.editing.text[issue['start']:issue['end']] != issue.get('word'):
            return None
        return issue

    def edit_fix(self, issue_id, index):
        """A suggestion clicked under the edited text: it replaces the word (one undo step, Ctrl+Z takes it back)."""
        issue = self._edit_issue(issue_id)
        suggestions = issue.get('suggestions') if issue else None
        if not isinstance(suggestions, list) or not 0 <= index < len(suggestions) or not isinstance(suggestions[index], str):
            return []
        self.editing.replace(issue['start'], issue['end'], suggestions[index])
        return [ShowUI(self.view()), Log('edit_fix', {'id': self.id, 'rule': str(issue.get('rule') or '')})]  # never the word

    def edit_dismiss(self, issue_id, verb='ignore'):
        """Ignoruj, Dodaj do słownika ('learn', _learnable) or Wyłącz regułę ('rule_off', for every issue of the rule): the
        live service's commands, as from the card. No message: it would take the panel's place."""
        issue = self._edit_issue(issue_id)
        offered = {'ignore': True, 'learn': issue is not None and _learnable(issue),
                   'rule_off': issue is not None and issue.get('rule_off') is True}
        if issue is None or not offered.get(verb):
            return []
        rule = str(issue.get('rule') or '')
        self.edit_issues = tuple(i for i in self.edit_issues if i is not issue
                                 and not (verb == 'rule_off' and str(i.get('rule') or '') == rule))
        return [live.Command({'cmd': verb, 'word': issue['word'], 'rule': rule}), ShowUI(self.view())]

    def edit_lines(self, starts):
        """The panel's visual lines of the edited text (their start offsets), for Up, Down, Home and End."""
        if self.editing is not None:
            self.editing.set_lines(starts)
        return []

    def _finish_edit(self):
        """End editing: the edited text is the result, its diff and change list are made again from the original
        (the reasons of changes that stayed the same are kept, LanguageTool's among them)."""
        text, self.editing = self.editing.text, None
        self.clip, self.paste = None, 0
        checked, self.edit_sent, self.edit_issues, self.edit_checked = self.edit_sent, None, (), None
        if text != self.result:
            if self.translating:
                self.base_segments, self.base_changes = (), ()
                self.rows = tuple(fallback_ui.lines((('same', text),)))
            else:
                segments = tuple(textdiff.diff_parts(self.capture.source, text))
                found = textdiff.changes(segments, self.capture.source)
                reasons = {(c.get('from'), c.get('to')): c.get('reason') for c in self.base_changes}
                for change in found:
                    change['reason'] = reasons.get((change['from'], change['to'])) or change['reason']
                self.base_segments, self.base_changes = segments, tuple(found[:textdiff.MAX_CHANGES])
                self.rows = tuple(fallback_ui.lines(segments))
                self.base_count = len(found)
            self.reverted = frozenset()
            self.segments = self.base_segments
            self.result = self.text = text
            self.changes, self.count = self._current_changes()
        cleared = [live.Clear()] if checked is not None else []  # the live service lets the editor's text go
        return cleared + [ShowUI(self.view()), Log('edit_done', {'id': self.id, 'changed': text != self.capture.source})]

    def _apply_edit(self):
        """Enter or Zastąp while editing: the edited text goes into the field."""
        effects = [e for e in self._finish_edit() if isinstance(e, Log)]
        return effects + self._apply()

    def _current_changes(self):
        """The change list without the changes undone, and the count with them gone."""
        kept = tuple(c for c in self.base_changes if _range(c) not in self.reverted)
        undone = sum(1 for c in self.base_changes if _range(c) in self.reverted)
        return kept, max(self.base_count - undone, len(kept))

    def backend_error(self, sid, message, why=None):
        """why: the cause's short code (core.error_kind), for the log; the message is never logged."""
        if sid != self.id or self.state != WAITING:
            return []
        log = Log('backend_error', dict({'id': sid}, **({'why': why} if why else {})))
        if why in problems.RETRIED and not self.retrying:  # a slip of the model: once more, quietly ("Ponawiam…")
            pending = self.apply_on_ready  # Enter while working still applies the result of the second try
            # only 'retry' in the log: the app's Wypróbuj line reads backend_error as the end of it
            effects = [Log('retry', {'id': sid, 'why': why})] + self._start(retry=True)
            self.apply_on_ready = pending
            return effects
        if problems.problem(why).group == problems.TEXT:  # nothing went wrong in the app: a toast, as a refusal
            self.state = ERROR  # the backend is done: nothing to cancel
            return [log] + self._end([Message(why, 3)], why)
        self.state, self.apply_on_ready = ERROR, False
        message = message if _encodable(message) else fallback_ui.message('internal')
        self.problem = problems.describe(why, message)
        self.error = self.problem[1]  # the IBus popup shows the sentence alone
        return [ShowUI(self.view()), log]

    def _interrupt(self, why):
        # An APPLYING session keeps waiting for its echo: our own commit may cause these events.
        if self.state in (WAITING, READY, ERROR):
            return self._end([HideUI()], why)
        return []

    def _verified(self, outcome):
        return Log('verified', {'id': self.id, 'outcome': outcome, 'app': self.app, 'win': self.win})

    def focus_changed(self):
        dropped = self.drop_confirm('focus')  # the caret asked about was in the field that has gone
        # The echo of an apply belongs to the field it was sent to, and GNOME Shell closes the popup
        # on focus-out: an APPLYING session ends unverified instead of staying modal elsewhere.
        if self.state == APPLYING and self.in_page:
            return dropped  # the page's answer ends it: the extension focuses the field it writes to, IBus sees that
        if self.state == APPLYING:
            return dropped + self._end([HideUI(), self._verified('unverified')], 'focus')
        return dropped + self._interrupt('focus')

    def reset(self):
        return self.drop_confirm('reset') + self._interrupt('reset')  # a click meanwhile: the user has moved on

    def became_sensitive(self):
        return self.drop_confirm('sensitive') + self._interrupt('sensitive')

    def surrounding_updated(self):
        if self.state != APPLYING or self.ctx.text is None or self.in_page:
            return []
        if plan.verify(self.expected, self.ctx.text, self.ctx.cursor) == 'ok':
            return self._end(([] if self.quiet else [Message('applied', 1.5)]) + [self._verified('ok')])
        # GTK echoes delete and commit separately; only the last echo before the timeout counts.
        self.echo_mismatch = True
        return []

    def verify_timeout(self, sid):
        if sid != self.id or self.state != APPLYING:
            return []
        if self.echo_mismatch:
            return self._end([Message('verify_mismatch', 4), self._verified('mismatch')])
        return self._end([HideUI(), self._verified('unverified')])

    def idle_timeout(self, sid):
        if sid != self.id:
            return []
        return self._interrupt('timeout')

    def abort(self, why):
        dropped = self.drop_confirm(why)
        if self.state == IDLE:
            return dropped + [HideUI()]
        return dropped + self._end([HideUI()], why)


def _encodable(*texts):
    """False for text with lone surrogates: IBus.Text (UTF-8) cannot carry it."""
    try:
        for text in texts:
            text.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


# Moves of the editor (EditBuffer.move) by key; with Ctrl, Left and Right go by word, Home and End to the text's edges.
MOVES = {keys.LEFT: 'left', keys.RIGHT: 'right', keys.UP: 'up', keys.DOWN: 'down', keys.HOME: 'home', keys.END: 'end',
         0xff96: 'left', 0xff98: 'right', 0xff97: 'up', 0xff99: 'down', 0xff95: 'home', 0xff9c: 'end'}  # and the keypad's


def _range(change):
    """A change's [first, last) segment range (core.changes) as a tuple, None without a valid one."""
    span = change.get('segments') if isinstance(change, dict) else None
    if (isinstance(span, (list, tuple)) and len(span) == 2 and all(type(i) is int for i in span)
            and 0 <= span[0] < span[1]):
        return tuple(span)
    return None


def revert_segments(segments, ranges):
    """The diff with the changes of ranges undone (macOS Diff.text(reverting:)): there the original stays
    (removed text is kept, added text goes), elsewhere every correction stays."""
    out = []
    for i, (kind, text) in enumerate(segments):
        back = any(first <= i < last for first, last in ranges)
        if back and kind == 'added':
            continue
        out.append(('same', text) if back and kind == 'removed' else (kind, text))
    return tuple(out)


def _learnable(issue):
    """Dodaj do słownika is offered for a spelling issue and a word without Polish letters (macOS issueBar)."""
    return issue.get('kind') == 'spelling' or issue.get('rule') == 'PL_DIACRITICS'


def word_change(change):
    """One word changed to another (spelling, Polish letters): it can be kept and added to the dictionary
    (macOS Change.isWordChange). A comma added after the word belongs to the change, which then is no word change."""
    words = (change.get('from'), change.get('to'))
    return all(isinstance(w, str) and len(w) > 1 and all(c.isalpha() or c in "-'’" for c in w) for w in words)


class EngineLogic:
    """Pure core of the IBus engine. One instance per engine object.

    Feed every vfunc into the matching method and execute the returned effects in order:
      StartBackend(id, text, mode, target)  backend.start(id, text, mode, GLib.idle_add, target)
      CancelBackend(id)             backend.cancel(id)
      ShowUI(view)                  aux, lines = fallback_ui.render(view): aux text shown, lines in
                                    a lookup table (page size 8, labels ' ', cursor hidden; hidden
                                    when lines is empty); cancels a pending Message timer
      HideUI()                      hide aux text and lookup table, cancel a Message timer
      Message(reason, seconds)      aux text fallback_ui.message(reason), lookup table hidden,
                                    hide after `seconds` unless replaced
      Apply(ops)                    delete_surrounding_text(*ops.delete) if ops.delete, then
                                    commit_text(ops.commit) if ops.commit is not None; build the
                                    commit's IBus.Text first, so nothing fails between the two
      ScheduleVerifyTimeout(id, ms) GLib.timeout_add(ms, -> verify_timeout(id))
      ConfirmCaret(id, text)        ask the live service where the caret is in text -> caret_confirmed(id, …)
      ScheduleConfirmTimeout(id, ms) GLib.timeout_add(ms, -> confirm_timeout(id))
      ScheduleIdleTimeout(id, ms)   GLib.timeout_add(ms, -> idle_timeout(id))
      ResetCompose()                IBus.EngineSimple.do_reset(self)
      Log(event, data)              one JSONL line; data never contains text (app IDs are no text)
      Consume() / ChainUp()         only from key(): return True / return the result of
                                    IBus.EngineSimple.do_process_key_event(self, keyval, keycode, state)
    Backend callbacks and timers arrive on the main loop and return effects the same way. After
    drawing UI effects, call ui_flushed(visible): True when the last one drawn was ShowUI.
    On any exception in a vfunc: log the exception type and execute fail(). A key is then chained
    up (after forget_key(keycode), so its release reaches the app too), unless an Apply of this
    key was already sent: then it is consumed, never delivered on top of a partial replace."""

    def __init__(self, mode_index=0):
        self.ctx = FieldContext()
        self.tracker = keys.KeyTracker()
        self.session = Session(self.ctx, mode_index)
        self.dictation = dictate.Dictation(self.ctx)
        self.live = live.LiveField(self.ctx)
        self.fixing = None  # (id, issue id, suggestion index, reason, ctx version, focus_gen) of a click waiting for AT-SPI

    def key(self, keyval, keycode, state, focus=None, disabled=frozenset(), modes=None, browser=None, shortcuts=None,
            char=None):
        """do_process_key_event. The last effect is always Consume() or ChainUp(), and only there.
        focus: (app, kind) of the focused window as GNOME Shell last reported it, None when unknown;
        disabled: the app IDs where GramaDrama is off, modes: {app ID: the mode Ctrl+Alt+G starts in} (apps.json).
        The process keeps all three. browser: the focused app is a browser whose extension is connected (its name).
        char: the character the key types (IBus.keyval_to_unicode), for the result being edited in the panel.
        Dictation sees every key first (the right Ctrl held alone) and never decides about it."""
        settled = self._settled()
        heard = self.dictation.key(keyval, bool(state & keys.RELEASE), (focus or (None, None))[0],
                                   may_start=self.session.state == IDLE, shift=bool(state & keys.SHIFT))
        return settled + heard + self._key(keyval, keycode, state, focus, disabled, modes, browser, shortcuts, char)

    def _settled(self):
        """First for every message from ibus-daemon but the text and focus changes: the app's text that
        ibus-daemon dropped as unchanged is the field's (FieldContext.settle). Never from a timer or another
        process's answer: only ibus-daemon's own next message shows that no text was on its way."""
        if not self.ctx.settle():
            return []
        return [Log('surrounding_unchanged', {})] + self.session.surrounding_updated()

    def _key(self, keyval, keycode, state, focus, disabled, modes, browser, shortcuts, char=None):
        action = keys.classify(keyval, keycode, state, self.session.is_modal, self.tracker,
                               self.session.state != IDLE and self.session.translating, shortcuts,
                               editing=self.session.editing is not None and self.session.is_modal,
                               editable=self.session.editable)
        if action == keys.SWALLOW:
            return [Consume()]
        if action == keys.PASS:
            if state & keys.RELEASE:
                return [ChainUp()]
            self.ctx.key_passed(keyval, state)
            # Typed on while AT-SPI was asked: that hotkey, or that click on a suggestion, is over.
            dropped = [] if keys.is_modifier(keyval) else self.session.drop_confirm('key') + self._drop_fix('key')
            if self.session.state != IDLE and not self.session.is_modal and not keys.is_modifier(keyval):
                # A key before the popup is on screen goes to the app, and the user has moved on.
                return dropped + self.session.cancel_and_pass()
            return dropped + [ChainUp()]
        if action == keys.CANCEL_AND_PASS:
            self.ctx.key_passed(keyval, state)
            return self.session.drop_confirm('key') + self._drop_fix('key') + self.session.cancel_and_pass()
        if action in (keys.HOTKEY, keys.TRANSLATE):
            self.ctx.app, self.ctx.app_kind = focus or (None, None)
            if applist.is_off(self.ctx.app, disabled):
                # GramaDrama is off in this app: Ctrl+Alt+G is the app's, its release too (never
                # consumed), nothing is captured or shown, and a running session stays as it is.
                self.ctx.key_passed(keyval, state)
                return [Log('refused', {'at': 'hotkey', 'reason': 'app_off', 'app': self.ctx.app,
                                        'win': self.ctx.app_kind}), ChainUp()]
        self.tracker.consume(keycode)
        if action in (keys.HOTKEY, keys.TRANSLATE):
            # Ctrl+Alt+J is always Tłumacz; Ctrl+Alt+G starts in the app's mode (Popraw unless apps.json says
            # otherwise). The badge's click is Popraw whatever the app (live:open), as on macOS.
            effects = self.session.hotkey('translate' if action == keys.TRANSLATE
                                          else applist.default_mode(self.ctx.app, modes or {}), browser)
        elif action == keys.EDIT_TOGGLE:
            effects = self.session.edit_toggle()
        elif action == keys.EDIT_KEY:
            effects = self.session.edit_key(keyval, state, char)
        elif action == keys.ACCEPT:
            effects = self.session.accept()
        elif action == keys.CANCEL:
            effects = self.session.cancel()
        elif action.kind == 'mode':
            effects = self.session.mode(action.mode)
        elif action == keys.DIRECTION:
            effects = self.session.direction()
        elif action == keys.CHANGES:
            effects = self.session.toggle_changes()
        else:
            effects = self.session.scroll(-1 if action == keys.SCROLL_UP else 1)
        return effects + [Consume()]

    def focus_in(self, client):
        """do_focus_in_id(path, client); plain do_focus_in passes client=None."""
        self.ctx.focus_in(client)
        self.tracker.new_focus()
        return self.dictation.focus_changed() + self.session.focus_changed()

    def focus_out(self):
        """do_focus_out_id / do_focus_out."""
        self.ctx.focus_out()
        self.tracker.new_focus()
        return self.dictation.focus_changed() + self.session.focus_changed()

    def surrounding(self, text, cursor, anchor):
        """do_set_surrounding_text after chaining up; text is IBus.Text.get_text() (or None)."""
        self.ctx.surrounding(text, cursor, anchor)
        return self.session.surrounding_updated()

    def content_type(self, purpose, hints):
        settled = self._settled()
        if self.ctx.set_content_type(purpose, hints):
            return settled + self.session.became_sensitive()
        return settled

    def capabilities(self, caps):
        settled = self._settled()
        self.ctx.set_caps(caps)
        return settled

    def reset(self):
        """do_reset (mouse click or programmatic change in the client)."""
        settled = self._settled()
        self.ctx.reset()
        return settled + self.session.reset()

    def disable(self):
        """do_disable / do_destroy: the engine stops serving this context."""
        self.ctx.focus_out()
        self.tracker.new_focus()
        return self.dictation.cancel('disable') + self.session.abort('disable') + self.live.clear()

    def page(self, delta):
        """do_page_up (-1) / do_page_down (+1) from the popup's arrows."""
        return self._settled() + self.session.scroll(delta)

    def ui_flushed(self, visible, panel=False, problems=False):
        """The adapter has drawn the queued UI effects; visible: the session popup is on screen; panel: it is the
        GNOME panel, one that can draw an edited result (the IBus popup cannot); problems: one that shows an error's
        buttons."""
        self.session.popup_visible = visible
        self.session.in_panel = visible and panel
        self.session.problem_ui = visible and problems
        return []

    def edit_lines(self, sid, starts):
        """The panel's visual lines of the result being edited (its Edit signal)."""
        return self.session.edit_lines(starts) if sid == self.session.id else []

    def edit_paste(self, sid, paste_id, text):
        """The clipboard's text for a paste into the result being edited (the panel's Edit signal)."""
        return self.session.edit_paste(paste_id, text) if sid == self.session.id else []

    def forget_key(self, keycode):
        """A consumed press is being delivered after all (exception path): deliver its release too."""
        self.tracker.release(keycode)

    def backend_partial(self, sid, text, rows, info=None):
        return self.session.backend_partial(sid, text, rows, info)

    def backend_result(self, sid, corrected, rows, info=None):
        return self.session.backend_result(sid, corrected, rows, info)

    def backend_changes(self, sid, changes, count):
        return self.session.backend_changes(sid, changes, count)

    def backend_error(self, sid, message, why=None):
        return self.session.backend_error(sid, message, why)

    def verify_timeout(self, sid):
        return self.session.verify_timeout(sid)

    def caret_confirmed(self, cid, cursor, anchor):
        """The live service's answer to ConfirmCaret(cid), the hotkey's or the live check's. After it the
        adapter updates the live check (live_update): the field may be current again."""
        if self.session.confirming is not None and self.session.confirming[0] == cid:
            return self.session.caret_confirmed(cid, cursor, anchor)
        if self.fixing is not None and self.fixing[0] == cid:
            return self._fix_confirmed(cursor, anchor)
        self.live.caret_confirmed(cid, cursor, anchor)
        return []

    def confirm_timeout(self, cid):
        if self.fixing is not None and self.fixing[0] == cid:
            _, issue_id, index, _, _, focus_gen = self.fixing
            self.fixing = None
            if focus_gen != self.ctx.focus_gen:
                return [Log('caret_dropped', {'why': 'focus', 'at': 'fix'})]
            return self._live_fix(issue_id, index, atspi='timeout')
        return self.session.confirm_timeout(cid)

    def _live_fix(self, issue_id, index, atspi=None):
        """A click on a live check's suggestion. When only the caret is in doubt (GTK4 after a click that sent no
        update), AT-SPI is asked first, as for the hotkey, and the click goes on with its answer. A refused click
        says what to do, and a suggestion whose word has gone leaves the badge: a second click could not work."""
        capture, corrected, data = self.live.fix(issue_id, index)
        if capture is not None:
            return self.session.fix(capture, corrected, data)
        reason = corrected
        if atspi is None and reason in CONFIRMABLE and self.session.state == IDLE and self.ctx.confirmable():
            cid = next(CONFIRMS)
            self.fixing = (cid, issue_id, index, reason, self.ctx.version, self.ctx.focus_gen)
            return [ConfirmCaret(cid, self.ctx.text), ScheduleConfirmTimeout(cid, CONFIRM_MS)]
        if atspi is not None:
            data = dict(data, atspi=atspi)
        forgotten = self.live.forget(issue_id) if reason == 'word_gone' else []
        return forgotten + [Message(FIX_MESSAGES.get(reason, 'fix_failed'), 3), Log('live_fix', dict(data, outcome=reason))]

    def _fix_confirmed(self, cursor, anchor):
        _, issue_id, index, reason, version, focus_gen = self.fixing
        self.fixing = None
        if focus_gen != self.ctx.focus_gen:
            return [Log('caret_dropped', {'why': 'focus', 'at': 'fix'})]
        outcome = self.ctx.confirm(version, cursor, anchor)
        confirmed = [Log('caret_confirmed', {'reason': reason, 'at': 'fix', 'app': self.ctx.app})] if outcome == 'ok' else []
        return confirmed + self._live_fix(issue_id, index, atspi=outcome)

    def _drop_fix(self, why):
        if self.fixing is None:
            return []
        self.fixing = None
        return [Log('caret_dropped', {'why': why, 'at': 'fix'})]

    def dictation_open(self, gen, enabled, ready, focus=None, disabled=frozenset(), elsewhere=False):
        """Right after the right Ctrl came down alone, off the key path: the microphone opens unless dictation could
        not start here (the arguments as for dictation_armed)."""
        if self.session.state != IDLE:
            return []
        app = (focus or (None, None))[0]
        return self.dictation.opened(gen, enabled, ready, applist.is_off(app, disabled), elsewhere)

    def dictation_armed(self, gen, enabled, ready, focus=None, disabled=frozenset(), elsewhere=False):
        """The right Ctrl has been held alone for dictate.ARM_MS. enabled: dictation is on in
        settings.json; ready: the speech tools are installed; elsewhere: another engine object dictates.
        A correction session that started meanwhile wins: the right Ctrl was a Ctrl there."""
        if self.session.state != IDLE:
            return self.dictation.drop()
        app = (focus or (None, None))[0]
        return self.dictation.armed(gen, enabled, ready, applist.is_off(app, disabled), elsewhere)

    def dictation_watch(self, sid):
        return self.dictation.watch(sid)

    def live_update(self, enabled, app_off):
        """After a change of the field, or of whether it may be checked: the text for the live service. While the
        result is edited in the panel, the edited text is checked instead (the field's check is off in a session)."""
        busy = self.session.state != IDLE or self.dictation.shown
        return self.live.update(enabled, app_off, busy=busy) + self.session.edit_check(enabled)

    def live_result(self, field, issues, checking=False, note=None):
        if self.session.editing is not None and field == self.session.edit_field:
            return self.session.edit_found(issues, checking)
        return self.live.result(field, issues, checking, note)

    def live_marks(self, field, caret, length, view):
        return self.live.marks(field, caret, length, view)

    def speech_event(self, ev, browser=None):
        """An event of the speech service for this engine's dictation. browser: as for key(), at the result: the text
        goes through the page's extension then."""
        return self.dictation.event(ev, busy=self.session.state == APPLYING, browser=browser)

    def browser_answer(self, kind, rid, answer):
        """The extension's answer to a BrowserRequest (browser_text), on the main loop."""
        if kind == 'hotkey':
            return self.session.page_text(rid, answer)
        if kind == 'apply':
            return self.session.page_written(rid, answer)
        return self.dictation.page_answer(kind, rid, answer, busy=self.session.state == APPLYING)

    def speech_died(self):
        return self.dictation.died()

    def action(self, sid, name, browser=None):
        """A click in the GNOME Shell panel (its Action signal): the same as the matching key, and
        only for the session whose panel is on screen. browser: as for key()."""
        s = self.session
        if name == 'hidden':  # GNOME Shell hid the panel itself (e.g. the on-screen keyboard came up)
            return s.panel_hidden() if sid == s.id else []
        if name == 'live:open':  # a click on the badge: Ctrl+Alt+G
            # Queue the correction before clearing live state, so its service can join an in-flight check.
            return s.hotkey('fix', browser) + self.live.clear() if s.state == IDLE else []
        if name.startswith(live.CARD_ACTIONS):  # the underline card
            verb, issue_id = name[len('live:'):].split(':', 1)
            command = self.live.command(verb, issue_id)
            if command is None:
                return []
            effects = [live.Command(command), Log('live_' + verb, {'rule': command['rule']})]  # never the word
            return effects + ([Message(*live.CARD_MESSAGES[verb])] if verb in live.CARD_MESSAGES else [])
        if name.startswith('live:fix:'):  # a click on a suggestion in the badge's list or the card
            issue_id, _, index = name[len('live:fix:'):].rpartition(':')
            if not index.isdigit():
                return []
            return self._drop_fix('click') + self._live_fix(issue_id, int(index))
        if sid != s.id or not s.is_modal:
            return []
        if name == 'accept':
            return s.accept()
        if name == 'cancel':
            return s.cancel()
        if name == 'direction':
            return s.direction()
        if name == 'copy':
            return s.copied()
        if name == 'edit':  # Edytuj / Gotowe (older panels; the result is an editable field now)
            return s.edit_toggle()
        if name == 'changes':  # Pokaż / Ukryj zmiany under the result
            return s.toggle_changes()
        if name.startswith('problem:'):  # a button of a failed correction: problem:retry, problem:open:connection …
            return s.problem_action(name[len('problem:'):])
        if name.startswith('edit_at:') and name[len('edit_at:'):].isdigit():  # a click in the result
            return s.edit_at(int(name[len('edit_at:'):]))
        if name.startswith('edit_fix:'):  # a suggestion under the edited text: edit_fix:<issue id>:<index>
            issue_id, _, index = name[len('edit_fix:'):].rpartition(':')
            return s.edit_fix(issue_id, int(index)) if index.isdigit() else []
        if name.startswith(('edit_ignore:', 'edit_learn:', 'edit_rule_off:')):
            verb, _, issue_id = name.partition(':')
            return s.edit_dismiss(issue_id, verb[len('edit_'):])
        if name.startswith(('revert:', 'keep:')):  # Cofnij / Zostaw i dodaj do słownika at a change
            verb, _, change = name.partition(':')
            return s.revert(int(change), keep=verb == 'keep') if change.isdigit() else []
        if name.startswith('mode:') and name[5:].isdigit() and int(name[5:]) < len(MODE_KEYS):
            return s.mode(int(name[5:]))
        return []

    def idle_timeout(self, sid):
        return self.session.idle_timeout(sid)

    def fail(self):
        """After an exception: end the session and distrust the field until the next update."""
        self.ctx.fresh = False
        return self.session.abort('error')

    def status(self):
        """Diagnostics for the D-Bus Status() method; contains no text."""
        return dict(self.ctx.status(), session=self.session.state, session_id=self.session.id,
                    mode=MODE_KEYS[self.session.mode_index], modal=self.session.is_modal)
