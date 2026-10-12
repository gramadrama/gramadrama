"""Dictation, the engine's side (docs/dictation.md; pure: no gi, no core).

Hold the right Ctrl alone: after ARM_MS it is a dictation, and when the key is let go the text comes to
the caret by commit_text, without the clipboard. The microphone opens at the key-down already: a USB
microphone that PipeWire has suspended takes up to 2 s to wake, and the pill says "Słucham…" only once
the first audio has come. Any other key pressed meanwhile makes it a shortcut (right Ctrl+C): before
ARM_MS the clip is thrown away without a trace, after it the recording is cancelled. The exception is
Shift, held before or during the hold: it makes this dictation go in the other language (macOS, 2026-09-30).
The engine only listens: the right Ctrl and every other key still reach the app.

HoldKey is macOS's HoldKeyMonitor machine. Dictation is the part of DictationController that belongs
to the engine; the speech service (gramadrama/speech.py) records and makes the text. Every input
returns a list of effects for the adapter (main.py), as in session.py."""
import dataclasses
import itertools
from ..i18n import tr

from . import browser_text as BT

CONTROL_R = 0xffe4
SHIFT_KEYS = (0xffe1, 0xffe2)  # Shift_L, Shift_R
ARM_MS = 300  # macOS's right-⌥ delay: the next key of a shortcut comes sooner than this
IDLE, ARMED, RECORDING = 'idle', 'armed', 'recording'
LISTENING, PROCESSING, INSERTING = 'listening', 'processing', 'inserting'  # INSERTING: through a page's extension
OPENING = 'opening'  # the microphone records from the key-down on; ARM_MS has not yet made it a dictation
MESSAGE_SECONDS = 3
WATCH_MS = 120_000  # a result must come this long after the key-up (the service died unnoticed, a hang)
# One numbering for the whole engine process: IBus makes an engine object per input context, and the
# one speech service answers them all, so an id must never repeat across them.
IDS = itertools.count(1)
MESSAGES = {
    'busy_dictation': 'Poprzednie dyktowanie jeszcze trwa.',
    'no_model': 'Dyktowanie potrzebuje modelu mowy: uruchom „install_engine.py tools”.',
    'silence': 'Nic nie usłyszałem — sprawdź, czy mikrofon działa.',
    'empty': 'Nic nie rozpoznałem.',
    'appChanged': 'Aktywne pole się zmieniło — podyktowany tekst jest w schowku (Ctrl+V).',
    'secureField': 'To pole hasła — podyktowany tekst jest w schowku (Ctrl+V).',
    'busy': 'Trwa inna operacja — podyktowany tekst jest w schowku (Ctrl+V).',
    'died': 'Rozpoznawanie mowy przerwało się — spróbuj jeszcze raz.',
    'uncertain': 'Nie wiem, czy tekst trafił do pola — sprawdź; jest też w schowku (Ctrl+V).',
}
# The character before the caret after which no space is added, and the text's first characters
# before which none is needed (macOS TextBridge.leadingSpaceNeeded).
NO_SPACE_AFTER = '([{„"\'“‘/-–—@#'
NO_SPACE_BEFORE = '.,;:!?)]}…”’'


@dataclasses.dataclass(frozen=True)
class ScheduleArm:
    gen: int
    ms: int  # then EngineLogic.dictation_armed(gen, ...)


@dataclasses.dataclass(frozen=True)
class ScheduleOpen:
    gen: int  # at once, but off the key path: then EngineLogic.dictation_open(gen, ...)


@dataclasses.dataclass(frozen=True)
class ScheduleWatch:
    id: int
    ms: int  # then EngineLogic.dictation_watch(id)


@dataclasses.dataclass(frozen=True)
class Speech:
    # for the speech service: 'start' (at the key-down), 'confirm' (ARM_MS made it a dictation), 'stop', 'cancel', or
    # 'alternate' (Shift came down while recording)
    cmd: str
    id: int
    alternate: bool = False  # with 'start': Shift was down before the recording began


@dataclasses.dataclass(frozen=True)
class Hud:
    view: dict  # the extension's pill (docs/dictation.md), or the IBus popup's line without it


@dataclasses.dataclass(frozen=True)
class Insert:
    text: str  # commit_text at the caret, now


@dataclasses.dataclass(frozen=True)
class Note:
    event: str
    data: dict  # a log line: counts, times, outcomes and the app ID only, never text


class HoldKey:
    """idle -down-> armed -elapsed-> recording -up-> idle (stop). Another key: armed -> idle (a
    shortcut), recording -> idle (cancel). Shift is no other key: armed or recording, it makes the dictation
    go in the other language (alternate; while recording the action is 'alternate', once). Every other pair
    changes nothing (a repeated key-down, a stale timer, a key-up while idle)."""

    def __init__(self, arm_ms=ARM_MS):
        self.arm_ms = arm_ms
        self.state = IDLE
        self.gen = 0
        self.alternate = False  # this dictation is in the other language; the next one starts without

    def key_down(self):
        if self.state != IDLE:
            return None
        self.alternate = False
        if not self.arm_ms:
            self.state = RECORDING
            return 'start'
        self.state, self.gen = ARMED, self.gen + 1
        return 'scheduleArm'

    def arm_elapsed(self, gen):
        if self.state != ARMED or gen != self.gen:
            return None
        self.state = RECORDING
        return 'start'

    def key_up(self):
        was, self.state = self.state, IDLE
        return 'stop' if was == RECORDING else None

    def other_key(self):
        was, self.state = self.state, IDLE
        return 'cancel' if was == RECORDING else None

    def shift(self):
        if self.state == ARMED:
            self.alternate = True
            return None
        if self.state == RECORDING and not self.alternate:
            self.alternate = True
            return 'alternate'
        return None

    def reset(self):
        """After a refusal or an end that came from elsewhere: the key-up that follows means nothing."""
        self.state = IDLE


def leading_space(previous, text):
    """' ' + text after a word or a sentence; text alone at the start, after whitespace, an opening
    bracket or quote, @ or #, and before punctuation."""
    if previous is None or not text:
        return text
    if previous.isspace() or previous in NO_SPACE_AFTER or text[0].isspace() or text[0] in NO_SPACE_BEFORE:
        return text
    return ' ' + text


def message(key_or_text, copy=None, seconds=MESSAGE_SECONDS):
    # Service messages are already localized; dictated/user text is never translated.
    text = tr(MESSAGES[key_or_text]) if key_or_text in MESSAGES else key_or_text
    return Hud({'v': 1, 'state': 'message', 'message': text, 'seconds': seconds,
                'copy': copy})


HIDE = Hud({'v': 1, 'state': 'hidden'})


class Dictation:
    """One dictation at a time for one engine: listening, then processing until the service answers.
    ctx is the engine's FieldContext: its focus generation at the key-down is the target, and at the
    end the text goes there, or to the clipboard when the focus has moved."""

    def __init__(self, ctx, arm_ms=ARM_MS, ids=None):
        self.ctx = ctx
        self.hold = HoldKey(arm_ms)
        self.phase = IDLE
        self.ids = IDS if ids is None else ids
        self.id = 0
        self.target = None  # (focus generation, app) at the key-down
        self.seconds = 0.0
        self.language = None  # PL or EN, as the service's levels say
        self.heard = False  # the microphone delivers audio: the pill says "Słucham…", not "Budzę mikrofon…"
        self.failure = None  # the service's error while OPENING: told at ARM_MS, dropped with a tap or a shortcut
        self.sent_alternate = False  # the service records in the other language already
        self.result = None  # (text, note, log) while the page's extension is asked where it goes (INSERTING)

    @property
    def active(self):
        """Anything going on, an open microphone before ARM_MS too: one at a time for the process."""
        return self.phase != IDLE

    @property
    def shown(self):
        """A dictation the user sees (the pill): live checking waits for it. A shortcut with the right Ctrl opens the
        microphone too, and must not clear the badge."""
        return self.phase not in (IDLE, OPENING)

    def key(self, keyval, release, app=None, may_start=True, shift=False):
        """Every key event, before anything else sees it; returns effects and never decides about the key.
        may_start: False while a correction session runs, where the right Ctrl is only a Ctrl (Ctrl+1..6).
        shift: Shift was already down when the right Ctrl came down."""
        if keyval == CONTROL_R:
            if release:
                action = self.hold.key_up()
                return self._discard() if self.phase == OPENING else self._act(action)
            if self.hold.state == IDLE:
                if not may_start:
                    return []
                self.target = (self.ctx.focus_gen, app)
            effects = self._act(self.hold.key_down())
            return effects + self._act(self.hold.shift()) if shift else effects
        if release:
            return []  # letting go of another modifier (Shift up) changes nothing
        if keyval in SHIFT_KEYS:
            return self._act(self.hold.shift())  # the other language, not a shortcut
        action = self.hold.other_key()
        return self._discard() if self.phase == OPENING else self._act(action)

    def opened(self, gen, enabled, ready, app_off, elsewhere=False):
        """Right after the key-down, off the key path: the microphone opens now, so that what is said as the pill
        comes up is recorded. Nothing is shown yet; refusals are armed()'s to tell, if the key is still held then."""
        if self.hold.state != ARMED or gen != self.hold.gen or self.phase != IDLE:
            return []
        if not enabled or app_off or elsewhere or not ready:
            return []
        self.id = next(self.ids)
        self.phase, self.seconds, self.language, self.heard, self.failure = OPENING, 0.0, None, False, None
        self.sent_alternate = self.hold.alternate
        return [Speech('start', self.id, self.hold.alternate)]

    def _discard(self):
        """A tap or a shortcut before ARM_MS, or a refusal at it: the clip goes without a pill or a log line."""
        if self.phase != OPENING:
            return []
        self.phase = IDLE
        return [Speech('cancel', self.id)]

    def drop(self):
        """The right Ctrl is no dictation key now (a correction session started while it was held)."""
        self.hold.reset()
        return self._discard()

    def _listening(self, level=0.0):
        view = {'v': 1, 'state': 'listening', 'level': level, 'seconds': self.seconds}
        if self.language:
            view['language'] = self.language  # PL or EN on the pill, as the service speaks it now
        if not self.heard:
            view['waiting'] = True  # no audio yet: "Budzę mikrofon…"
        return view

    def armed(self, gen, enabled, ready, app_off, elsewhere=False):
        """The hold lasted ARM_MS: start, unless dictation is off or cannot work here. elsewhere: another
        engine object's dictation is still going (one at a time for the whole process, as on macOS)."""
        if self.hold.arm_elapsed(gen) != 'start':
            return []
        opened = self.phase == OPENING
        if not enabled or app_off:
            return self.drop()
        if (self.phase != IDLE and not opened) or elsewhere:
            return self.drop() + [message('busy_dictation')]
        if not ready:
            return self.drop() + [message('no_model', seconds=5)]
        if self.failure is not None:  # the microphone or the configuration failed while the key was held
            failure, self.failure, self.phase = self.failure, None, IDLE
            self.hold.reset()
            return self._failed(failure)
        effects = []
        if opened:
            if self.hold.alternate and not self.sent_alternate:
                effects.append(Speech('alternate', self.id))  # Shift came down after the microphone opened
        else:  # the opening was refused or the service died meanwhile: as before, from now on
            self.id = next(self.ids)
            self.seconds, self.language, self.heard = 0.0, None, False
            effects.append(Speech('start', self.id, self.hold.alternate))
        self.phase = LISTENING
        return effects + [Speech('confirm', self.id), Hud(self._listening())]

    def _act(self, action):
        if action == 'scheduleArm':
            return [ScheduleOpen(self.hold.gen), ScheduleArm(self.hold.gen, self.hold.arm_ms)]
        if action == 'alternate' and self.phase == LISTENING:
            return [Speech('alternate', self.id)]
        if action == 'stop' and self.phase == LISTENING:
            self.phase = PROCESSING
            return [Speech('stop', self.id), ScheduleWatch(self.id, WATCH_MS)]
        if action == 'cancel' and self.phase == LISTENING:
            self.phase = IDLE
            return [Speech('cancel', self.id), HIDE, self._log('cancelled', audio_ms=int(self.seconds * 1000))]
        return []

    def _log(self, outcome, **data):
        return Note('dictation', dict(data, outcome=outcome, app=self.target[1] if self.target else None))

    def cancel(self, why):
        """The engine object goes away (disabled, destroyed): a recording stops, a result is not awaited,
        and a key-down whose ARM_MS is still running starts nothing."""
        if self.phase in (IDLE, OPENING):
            return self.drop()
        effects = [Speech('cancel', self.id), HIDE] if self.phase == LISTENING else [HIDE]
        self.phase, self.result = IDLE, None
        self.hold.reset()
        return effects + [self._log('cancelled', why=why)]

    def event(self, ev, busy=False, browser=None):
        """An event of the speech service (gramadrama/speech.py). busy: a correction is being applied. browser: the
        focused app is a browser whose extension is connected: the result goes into the page through it."""
        kind = ev.get('ev')
        if ev.get('id') != self.id or self.phase == IDLE:
            return []  # an older dictation's, or a model notice
        if kind in ('listening', 'level') and self.phase in (OPENING, LISTENING):
            self.heard = True  # 'listening': the first audio has come
            if kind == 'level':
                self.seconds = float(ev.get('s') or 0.0)
                if isinstance(ev.get('lang'), str):
                    self.language = ev['lang']
            return [Hud(self._listening(float(ev.get('level') or 0.0)))] if self.phase == LISTENING else []
        if self.phase == OPENING:
            if kind == 'error':
                self.failure = ev
            return []
        if kind == 'recognizing':
            effects = [Hud({'v': 1, 'state': 'recognizing'})]
            if ev.get('limit'):
                self.hold.reset()  # 180 s: the key-up that comes later is no stop
            if self.phase == LISTENING:
                effects.append(ScheduleWatch(self.id, WATCH_MS))
            self.phase = PROCESSING
            return effects
        if kind == 'cleaning':
            return [Hud({'v': 1, 'state': 'cleaning'})]
        if kind == 'short':
            self.phase = IDLE
            return [HIDE]
        if kind not in ('silence', 'error', 'result'):
            return []  # a late 'level', and anything newer: nothing to do
        self.phase = IDLE
        self.hold.reset()
        if kind == 'error':
            return self._failed(ev)
        log = dict(ev.get('log') or {})
        log.pop('outcome', None)
        if kind == 'silence':
            return [message('silence'), self._log('silence', **log)]
        return self._insert(str(ev.get('text') or ''), str(ev.get('note') or ''), log, busy, browser)

    def _failed(self, ev):
        log = dict(ev.get('log') or {})
        outcome = log.pop('outcome', 'asr_error')
        return [message(str(ev.get('message') or MESSAGES['died']), seconds=5), self._log(outcome, **log)]

    def focus_changed(self):
        """The field lost the focus while the key was held: its key-up may never come to this engine, so
        the recording ends here; the text then goes to the clipboard (the field changed)."""
        if self.phase == LISTENING:
            self.hold.reset()
            self.phase = PROCESSING
            return [Speech('stop', self.id), ScheduleWatch(self.id, WATCH_MS)]
        if self.hold.state == ARMED:
            self.hold.reset()
        return self._discard()

    def watch(self, sid):
        """WATCH_MS after the key-up without a result: the service is stuck or gone."""
        if sid != self.id or self.phase != PROCESSING:
            return []
        return self.died()

    def died(self):
        """The service is gone (a crash, or it quit): a dictation in progress ends with a message."""
        if self.phase == IDLE:
            return []
        if self.phase == OPENING:  # nothing shown yet: if the key is still held at ARM_MS, armed() starts a new service
            self.phase = IDLE
            return []
        self.phase = IDLE
        self.hold.reset()
        return [message('died'), self._log('asr_error', why='died')]

    def _insert(self, text, note, log, busy, browser=None):
        """Where the text goes, as macOS TextBridge.insert decides, with the field the engine knows. In a browser with
        its extension the page is asked first (insertViaBrowser): the character before the caret (`context`), then
        `insert`; page_answer goes on from there."""
        if not text.strip() or '￼' in text or '\x00' in text:
            return [message('empty'), self._log('empty' if not text.strip() else 'badResult', **log)]
        if browser is not None and not busy and self.target is not None and self.ctx.focus_gen == self.target[0]:
            self.phase, self.result = INSERTING, (text, note, log, browser)
            return [BT.BrowserRequest('dictation', self.id, browser, 'context', {})]
        return self._commit(text, note, log, busy)

    def page_answer(self, kind, rid, answer, busy=False):
        """The extension's answer: to `context` (kind 'dictation') or to `insert` (kind 'insert')."""
        if rid != self.id or self.phase != INSERTING or self.result is None:
            return []
        text, note, log, browser = self.result
        if kind == 'dictation':
            if answer.get('ok') and isinstance(answer.get('client'), str):
                fields = {'text': leading_space(BT.previous_char(answer), text), 'client': answer['client']}
                return [BT.BrowserRequest('insert', self.id, browser, 'insert', fields, BT.WRITE_SECONDS, BT.WRITE_DEADLINE)]
            self.phase, self.result = IDLE, None
            if answer.get('reason') == 'secure_input':
                return [message('secureField', copy=text), self._log('secureField', via='extension', **log)]
            return self._commit(text, note, log, busy)  # no field in the page for it: the IBus way, as before
        self.phase, self.result = IDLE, None
        outcome = BT.outcome(answer)
        if outcome == 'landed':
            return [message(note, seconds=4) if note else HIDE, self._log('inserted', via='extension', **log)]
        if outcome == 'refused' and answer.get('reason') != 'client_changed':
            return self._commit(text, note, log, busy)  # nothing was written: the IBus way (insertViaBrowser's nil)
        outcome = 'appChanged' if outcome == 'refused' else 'uncertain'
        return [message(outcome, copy=text), self._log(outcome, via='extension', **log)]

    def _commit(self, text, note, log, busy):
        """The text at the caret through IBus (commit_text), unless the field is gone or must not get it."""
        ctx = self.ctx
        if busy:
            outcome = 'busy'
        elif self.target is None or ctx.focus_gen != self.target[0] or ctx.client in (None, 'fake'):
            outcome = 'appChanged'
        elif ctx.sensitive:
            outcome = 'secureField'
        else:
            previous = None
            if ctx.text is not None and 0 <= min(ctx.cursor, ctx.anchor) <= len(ctx.text):
                start = min(ctx.cursor, ctx.anchor)
                previous = ctx.text[start - 1] if start else None
            if ctx.text is not None:
                # The engine changed the field itself, as after a correction: it is trusted again when the client
                # echoes the new text. Otherwise a hotkey right after this would capture the text from before.
                # (Without known text there is nothing to distrust, and "press an arrow key" stays the hint.)
                ctx.expect_update()
            effects = [Insert(leading_space(previous, text)), message(note, seconds=4) if note else HIDE]
            return effects + [self._log('inserted', **log)]
        return [message(outcome, copy=text), self._log(outcome, **log)]
