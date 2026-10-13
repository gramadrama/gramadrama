// GramaDrama: the dictation pill (linux/docs/dictation.md), as macOS DictationHUD draws it: a 230x40
// capsule at the bottom centre of the monitor with the pointer, 48 px above the work area's bottom.
// It never takes the keyboard or the pointer. The engine sends its state with Hud(view).
//
//   listening    outlined masks, five level bars, "Słucham…", the language (PL, EN: `language`, as macOS shows it;
//                Shift with the key dictates in the other one) and the time "m:ss"; with `waiting` (no audio yet:
//                a suspended USB microphone wakes in up to 2 s) a spinner and "Budzę mikrofon…" instead
//   recognizing  filled masks, a spinner, "Rozpoznaję…" ("Porządkuję…" while Bielik cleans up)
//   message      a text for `seconds`, after putting `copy` on the clipboard when given; a long one is never cut:
//                the pill widens up to MESSAGE_WIDTH (the macOS toast's width) and wraps, growing upwards
//   hidden
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Graphene from 'gi://Graphene';
import Pango from 'gi://Pango';
import St from 'gi://St';
import {tr, uiLanguage} from './i18n.js';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

export const HUD_WIDTH = 230;
export const HUD_HEIGHT = 40;
export const MESSAGE_WIDTH = 420; // the widest message pill: the toast's maxWidth on macOS (Toast.swift)
const HUD_BOTTOM = 48; // above the bottom of the work area
const BAR_WEIGHTS = [0.45, 0.75, 1, 0.75, 0.45];
const TEXTS = {listening: 'Słucham…', waiting: 'Budzę mikrofon…', recognizing: 'Rozpoznaję…', cleaning: 'Porządkuję…'};
const STATES = new Set(['listening', 'recognizing', 'cleaning', 'message', 'hidden']);
const MAX_JSON = 100000;
const MAX_MESSAGE = 300;
const MAX_COPY = 50000; // characters: three minutes of speech is far less
// Without news from the engine for this long, a pill in progress goes (the engine stuck): while listening
// the levels come several times a second, and the engine ends recognizing itself after two minutes.
const STALE_MS = {listening: 30000, recognizing: 200000, cleaning: 200000};

/** The view of a Hud call, checked; throws with the reason for anything else. */
export function parseHud(json) {
    if (typeof json !== 'string' || json.length > MAX_JSON)
        throw new Error('not a string, or too long');
    const raw = JSON.parse(json);
    if (!raw || typeof raw !== 'object' || raw.v !== 1 || !STATES.has(raw.state))
        throw new Error('unknown view');
    const number = (value, low, high) =>
        typeof value === 'number' && Number.isFinite(value) ? Math.min(Math.max(value, low), high) : low;
    const view = {ui_language: uiLanguage(raw.ui_language), state: raw.state, level: number(raw.level, 0, 1), seconds: number(raw.seconds, 0, 3600),
        waiting: raw.state === 'listening' && raw.waiting === true};
    // The language of this dictation (PL, EN, auto): anything else is no label, never an error.
    view.language = typeof raw.language === 'string' && /^[A-Za-z]{2,4}$/.test(raw.language) ? raw.language : null;
    if (raw.state === 'message') {
        if (typeof raw.message !== 'string' || !raw.message)
            throw new Error('a message without text');
        view.message = raw.message.length > MAX_MESSAGE ? `${raw.message.slice(0, MAX_MESSAGE - 1)}…` : raw.message;
        if (raw.copy !== undefined && raw.copy !== null && (typeof raw.copy !== 'string' || raw.copy.length > MAX_COPY))
            throw new Error('copy is not a text');
        view.copy = raw.copy || null;
    }
    return view;
}

export class Hud {
    constructor({logos}) {
        this._logos = logos; // {dark|light: {filled, outline}}
        this._logoKind = 'outline';
        this._variant = 'dark';
        this._gone = false;
        this._timer = 0;
        this._state = 'hidden';
        this._width = HUD_WIDTH; // the pill's width: HUD_WIDTH, or a message's (_messageWidth)
        this.actor = new St.BoxLayout({
            name: 'gramadrama-hud',
            style_class: 'gd-hud gd-dark',
            reactive: false, // click-through, like the toast
            can_focus: false,
            visible: false,
            width: HUD_WIDTH,
        });
        this._logo = new St.Icon({gicon: logos.dark.outline, icon_size: 18, style_class: 'gd-logo',
            y_align: Clutter.ActorAlign.CENTER});
        this._bars = new St.BoxLayout({style_class: 'gd-hud-bars', y_align: Clutter.ActorAlign.CENTER});
        this._barActors = BAR_WEIGHTS.map(() => {
            const bar = new St.Widget({style_class: 'gd-hud-bar', width: 3, height: 4, y_align: Clutter.ActorAlign.CENTER});
            this._bars.add_child(bar);
            return bar;
        });
        this._spinner = new St.Icon({icon_name: 'process-working-symbolic', icon_size: 14, style_class: 'gd-hud-spinner',
            y_align: Clutter.ActorAlign.CENTER, pivot_point: new Graphene.Point({x: 0.5, y: 0.5})});
        this._label = new St.Label({style_class: 'gd-hud-label', x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR; // a message's long word breaks too
        this._lang = new St.Label({style_class: 'gd-hud-lang', y_align: Clutter.ActorAlign.CENTER});
        this._time = new St.Label({style_class: 'gd-hud-time', y_align: Clutter.ActorAlign.CENTER});
        for (const child of [this._logo, this._bars, this._spinner, this._label, this._lang, this._time])
            this.actor.add_child(child);
        this.actor.connect('destroy', () => {
            this._gone = true;
            this._stopTimer();
        });
    }

    setVariant(variant) {
        if (this._gone || variant === this._variant)
            return;
        this.actor.remove_style_class_name(`gd-${this._variant}`);
        this._variant = variant;
        this.actor.add_style_class_name(`gd-${variant}`);
        this._logo.gicon = this._logos[variant][this._logoKind];
    }

    /** Draws the view; true when it is on screen (a hidden view: false). */
    show(view) {
        if (this._gone)
            return false;
        this._stopTimer();
        if (view.state === 'hidden') {
            this.hide();
            return false;
        }
        if (view.state === 'message' && view.copy)
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, view.copy);
        const listening = view.state === 'listening';
        const working = view.state === 'recognizing' || view.state === 'cleaning' || view.waiting;
        this._logoKind = listening ? 'outline' : 'filled';
        this._logo.gicon = this._logos[this._variant][this._logoKind];
        this._bars.visible = listening && !view.waiting;
        this._time.visible = listening && !view.waiting;
        this._lang.visible = listening && !!view.language;
        if (this._lang.visible) {
            this._lang.text = view.language;
            if (view.language === 'EN')
                this._lang.add_style_class_name('gd-hud-lang-en'); // brick, as macOS marks English in coral
            else
                this._lang.remove_style_class_name('gd-hud-lang-en');
        }
        this._spinner.visible = working;
        if (working && !this._spinning) {
            this._spinning = true;
            this._spinner.rotation_angle_z = 0;
            this._spinner.ease({rotation_angle_z: 360, duration: 1000, mode: Clutter.AnimationMode.LINEAR,
                repeatCount: -1});
        } else if (!working && this._spinning) {
            this._spinning = false;
            this._spinner.remove_all_transitions();
        }
        const message = view.state === 'message';
        this._label.text = message ? view.message : tr(TEXTS[view.waiting ? 'waiting' : view.state], view.ui_language);
        // A message is said whole (the clipboard ones say where the dictated text went): it wraps, in a pill as wide as
        // its line up to MESSAGE_WIDTH. The other states keep the 230 px pill of macOS.
        this._label.clutter_text.line_wrap = message;
        this._label.clutter_text.ellipsize = message ? Pango.EllipsizeMode.NONE : Pango.EllipsizeMode.END;
        if (message)
            this.actor.add_style_class_name('gd-hud-message');
        else
            this.actor.remove_style_class_name('gd-hud-message');
        this._width = message ? this._messageWidth() : HUD_WIDTH;
        this.actor.width = this._width;
        if (listening) {
            BAR_WEIGHTS.forEach((weight, i) => (this._barActors[i].height = Math.round(4 + 14 * view.level * weight)));
            const seconds = Math.floor(view.seconds);
            this._time.text = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
        }
        this._state = view.state;
        this._waiting = view.waiting;
        this.actor.show();
        this._place();
        const ms = view.state === 'message' ? Math.round((view.seconds || 3) * 1000) : STALE_MS[view.state];
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timer = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
        return true;
    }

    /** The width of a message's pill: its text on one line, between the pill's 230 px and MESSAGE_WIDTH. */
    _messageWidth() {
        this.actor.width = -1; // the natural width again, not the last fixed one
        const [, natural] = this.actor.get_preferred_width(-1);
        return Math.min(Math.max(Math.ceil(natural), HUD_WIDTH), MESSAGE_WIDTH);
    }

    _place() {
        const index = global.display.get_current_monitor(); // the monitor with the pointer, as on macOS
        const area = Main.layoutManager.getWorkAreaForMonitor(index);
        // The width chosen in show(), never read back from the actor: reading this.actor.width while a relayout is
        // queued left the bars at their old height in the engine's session (the levels came, the bars stayed at 4 px).
        const width = this._width;
        const [, height] = this.actor.get_preferred_height(width);
        // The bottom stays where it is: a message of more lines grows upwards.
        this.actor.set_position(Math.round(area.x + (area.width - width) / 2),
            Math.round(area.y + area.height - HUD_BOTTOM - Math.max(height, HUD_HEIGHT)));
    }

    reposition() {
        if (!this._gone && this.actor.visible)
            this._place();
    }

    hide() {
        if (this._gone)
            return;
        this._stopTimer();
        if (this._spinning) {
            this._spinning = false;
            this._spinner.remove_all_transitions();
        }
        this._state = 'hidden';
        this.actor.hide();
    }

    _stopTimer() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
    }

    destroy() {
        if (!this._gone) {
            this.hide();
            this.actor.destroy();
        }
        this.actor = null;
    }

    describe() {
        if (this._gone)
            return {visible: false};
        const layout = this._label.clutter_text.get_layout();
        return {visible: this.actor.visible, state: this._state, waiting: this._state === 'listening' && !!this._waiting,
            text: this._label.text,
            language: this._lang.visible ? this._lang.text : null,
            time: this._time.visible ? this._time.text : null, bars: this._barActors.map(bar => bar.height),
            spinning: !!this._spinning, reactive: this.actor.reactive, width: Math.round(this.actor.width),
            height: Math.round(this.actor.height), lines: layout.get_line_count(),
            cut: this._label.has_allocation() && layout.is_ellipsized(),
            y: Math.round(this.actor.y)};
    }
}
