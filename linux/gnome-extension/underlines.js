// GramaDrama: the live check's underlines (linux/docs/live-check.md), as macOS LiveUnderlines draws
// them: a wavy line under each mistaken word (brick for every kind; the card's message tells grammar and style apart),
// and on hover a card with the suggestions, "Ignoruj", "Dodaj do słownika" and, for a grammar rule of LanguageTool,
// "Wyłącz regułę" (icons, named under them on hover).
//
// The words' boxes come from the app's accessibility (the engine's live service reads AT-SPI), in the
// app's own window coordinates. GTK4 counts from the window's frame; a browser's page (its extension's
// boxes) from the window's client rect; anything else is placed by the caret, whose stage rectangle
// IBus gives and whose box comes with the marks.
//
// The lines are drawn inside the focused window's actor: they move with the window, other windows and
// the app's own popups cover them, and they never take a click. GNOME Shell sees no pointer motion over
// app windows, so while there are lines the pointer is polled for the card; the card is top chrome,
// reactive but never focusable, like the badge's list.
//
//   hover a word     the card: its suggestions as buttons, the reason, Ignoruj, Dodaj do słownika, Wyłącz regułę
//   click a button   Action(0, 'live:fix:<id>:<n>' | 'live:ignore:<id>' | 'live:learn:<id>' | 'live:rule_off:<id>')
import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';
import {tr, uiLanguage} from './i18n.js';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {WAVE, box, cut, fixButton, paddingOf, stackFixes, wholeText} from './live.js';

const MAX_JSON = 200000;
const MAX_MARKS = 60;
const MAX_RECTS = 60;
const POLL_MS = 80;
const MARGIN = 4; // around the field's box, for the wave at its bottom edge
const CARD_MIN = 220;
const CARD_MAX = 320;
const CARD_WIDE = 480; // the card grows to this for a long suggestion; beyond it the suggestion wraps
const KINDS = new Set(['spelling', 'grammar', 'style']);
// Brick #D07A52 (branding/BRAND.md): what the check found, in any app and either theme. One colour for
// every kind keeps the meaning simple; the wave's shape tells it from the panel's corrections.
const BRICK = [0.816, 0.478, 0.322];
const COLORS = {spelling: BRICK, grammar: BRICK, style: BRICK};

function escape(text) {
    return GLib.markup_escape_text(text, -1);
}

export function intersect(a, b) {
    const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
    const width = Math.min(a.x + a.width, b.x + b.width) - x, height = Math.min(a.y + a.height, b.y + b.height) - y;
    return width > 0 && height > 0 ? {x, y, width, height} : null;
}

function within(rect, x, y, dx = 0, dy = 0) {
    return x >= rect.x - dx && x <= rect.x + rect.width + dx && y >= rect.y - dy && y <= rect.y + rect.height + dy;
}

/** The view of a Marks call, checked; throws with the reason for anything else. */
export function parseMarks(json) {
    if (typeof json !== 'string' || json.length > MAX_JSON)
        throw new Error('not a string, or too long');
    const raw = JSON.parse(json);
    if (!raw || typeof raw !== 'object' || raw.v !== 1 || !Array.isArray(raw.marks))
        throw new Error('unknown view');
    const text = (value, max) => typeof value === 'string' ? value.slice(0, max) : '';
    const marks = [];
    let rects = 0;
    for (const mark of raw.marks.slice(0, MAX_MARKS)) {
        const boxes = (Array.isArray(mark?.rects) ? mark.rects : []).map(box).filter(b => b && b[2] > 0 && b[3] > 0);
        if (!boxes.length || rects + boxes.length > MAX_RECTS)
            continue;
        rects += boxes.length;
        marks.push({
            id: text(mark.id, 200), word: text(mark.word, 200), message: text(mark.message, 300),
            suggestions: (Array.isArray(mark.suggestions) ? mark.suggestions : []).filter(s => typeof s === 'string')
                .slice(0, 3).map(s => s.slice(0, 200)),
            kind: KINDS.has(mark.kind) ? mark.kind : 'grammar', source: text(mark.source, 40), learn: mark.learn === true,
            ruleOff: mark.rule_off === true, rects: boxes,
        });
    }
    const toolkit = Array.isArray(raw.toolkit) ? [text(raw.toolkit[0], 40), text(raw.toolkit[1], 40)] : ['', ''];
    return {ui_language: uiLanguage(raw.ui_language), toolkit, from: raw.from === 'client' ? 'client' : null, caret: box(raw.caret), clip: box(raw.clip), marks};
}

/**
 * Where the app's window coordinates start on the stage. A browser's page (from: 'client') counts from
 * the window's client rect: Chrome's own coordinates start at its surface, its shadow included when it is
 * not maximized (12 px on the left and 10 above on Wayland), and on X11 inside the window manager's frame.
 * GTK4 counts from the frame; anything else is placed by the caret (its stage rectangle minus its box in
 * app coordinates), kept per window relative to the frame, so a window moved since is still right.
 * Returns {x, y, kept}, or null when not known.
 */
export function origin(view, window, caret, kept) {
    const frame = window.get_frame_rect();
    if (view.from === 'client') {
        const client = window.frame_rect_to_client_rect(frame);
        return {x: client.x, y: client.y, kept: null};
    }
    const [name, version] = view.toolkit;
    if (name === 'GTK' && /^4\./.test(version))
        return {x: frame.x, y: frame.y, kept: null};
    if (view.caret && caret) {
        kept = {
            dx: caret.x - view.caret[0] - frame.x,
            dy: caret.y + caret.height / 2 - (view.caret[1] + view.caret[3] / 2) - frame.y,
        };
    }
    return kept ? {x: frame.x + kept.dx, y: frame.y + kept.dy, kept} : null;
}

export class Underlines {
    constructor({onAction}) {
        this._onAction = onAction;
        this._gone = false;
        this._variant = 'dark';
        this._window = null; // the Meta.Window whose actor holds the lines
        this._actor = null;
        this._actorDestroyId = 0;
        this._area = null;
        this._areaAt = {x: 0, y: 0}; // the drawing area's stage position
        this._lines = []; // [{mark, rect}] in stage coordinates
        this._kept = new Map(); // window id -> the caret's calibration
        this._pollId = 0;
        this._cardMark = null;
        this._cardWord = null;
        this.card = new St.BoxLayout({
            name: 'gramadrama-mark-card',
            style_class: 'gd-mark-card gd-dark',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: true, // its buttons take clicks; the pointer over it keeps it open
            can_focus: false,
            track_hover: true,
            visible: false,
        });
        this.card.connect('destroy', () => this._destroyed());
        // The name of an icon button of the card, under it while the pointer is over it (St has no tooltips of its own).
        this._tip = new St.Label({style_class: 'gd-mark-tip gd-dark', visible: false, reactive: false});
        Main.layoutManager.uiGroup.add_child(this._tip);
        this._tip.connect('destroy', () => (this._tip = null)); // with the Shell's own actors at its end
    }

    get window() {
        return this._window;
    }

    setVariant(variant) {
        if (this._gone || variant === this._variant)
            return;
        for (const actor of [this.card, this._tip].filter(Boolean))
            actor.remove_style_class_name(`gd-${this._variant}`);
        this._variant = variant;
        for (const actor of [this.card, this._tip].filter(Boolean))
            actor.add_style_class_name(`gd-${variant}`);
    }

    /** Draws the view's lines in window (the focused Meta.Window); caret: the IBus caret on the stage,
     *  or null. True when a line is on screen. */
    show(view, window, caret) {
        this._uiLanguage = view.ui_language;
        if (this._gone)
            return false;
        const actor = window?.get_compositor_private();
        if (!view.marks.length || !actor) {
            this.hide();
            return false;
        }
        const frame = window.get_frame_rect();
        const id = String(window.get_id());
        const place = origin(view, window, caret, this._kept.get(id));
        if (!place) {
            this.hide();
            return false;
        }
        if (place.kept)
            this._kept.set(id, place.kept);
        let clip = {x: frame.x, y: frame.y, width: frame.width, height: frame.height};
        if (view.clip)
            clip = intersect(clip, {x: place.x + view.clip[0], y: place.y + view.clip[1], width: view.clip[2], height: view.clip[3]});
        const lines = [];
        for (const mark of clip ? view.marks : []) {
            for (const r of mark.rects) {
                const rect = {x: place.x + r[0], y: place.y + r[1], width: r[2], height: r[3]};
                // As macOS: a word whose middle is outside the field's visible box is scrolled away.
                if (within(clip, rect.x + rect.width / 2, rect.y + rect.height / 2, 1, 1))
                    lines.push({mark, rect});
            }
        }
        if (!lines.length) {
            this.hide();
            return false;
        }
        this._lines = lines;
        this._attach(window, actor, clip);
        if (this._cardMark && !view.marks.some(m => m.id === this._cardMark.id))
            this._closeCard();
        if (!this._pollId) {
            this._pollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => {
                this._poll();
                return GLib.SOURCE_CONTINUE;
            });
        }
        return true;
    }

    _attach(window, actor, clip) {
        if (this._actor !== actor || !this._area) {
            this._detach();
            this._area = new St.DrawingArea({name: 'gramadrama-underlines', reactive: false});
            this._area.connect('repaint', area => this._repaint(area));
            actor.add_child(this._area);
            this._window = window;
            this._actor = actor;
            this._actorDestroyId = actor.connect('destroy', () => {
                this._actor = this._area = null; // gone with the window
                this._actorDestroyId = 0;
                this.hide();
            });
        }
        this._areaAt = {x: clip.x - MARGIN, y: clip.y - MARGIN};
        this._area.set_position(Math.round(this._areaAt.x - actor.x), Math.round(this._areaAt.y - actor.y));
        this._area.set_size(Math.round(clip.width + 2 * MARGIN), Math.round(clip.height + 2 * MARGIN));
        this._area.visible = !Main.overview.visible; // the overview's window previews clone the actor
        this._area.queue_repaint();
    }

    _repaint(area) {
        const cr = area.get_context();
        cr.setLineWidth(1.4);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        for (const {mark, rect} of this._lines) {
            const [r, g, b] = COLORS[mark.kind];
            cr.setSourceRGBA(r, g, b, 1);
            // The wave just above the box's bottom edge, 2.5 px steps 1.6 px high (macOS UnderlineCanvas).
            const y = rect.y + rect.height - 1.5 - this._areaAt.y;
            let x = rect.x - this._areaAt.x;
            const end = x + rect.width;
            cr.moveTo(x, y);
            let up = true;
            while (x < end) {
                x = Math.min(x + 2.5, end);
                cr.lineTo(x, up ? y - 1.6 : y);
                up = !up;
            }
            cr.stroke();
        }
        cr.$dispose();
    }

    _poll() {
        if (this._gone || !this._window)
            return;
        if (global.display.focus_window !== this._window || Main.keyboard.visible) {
            this.hide(); // another window, or the on-screen keyboard: the next marks draw again
            return;
        }
        if (this._area)
            this._area.visible = !Main.overview.visible;
        if (Main.overview.visible)
            return;
        if (this.card.visible && this.card.hover)
            return; // over the card: a click on its buttons may be under way
        const [x, y, mods] = global.get_pointer();
        if (!x && !y && !mods)
            return; // no answer while a button is held on shell chrome: not a position
        const pressed = mods & (Clutter.ModifierType.BUTTON1_MASK | Clutter.ModifierType.BUTTON3_MASK);
        if (this.card.visible) {
            const card = {x: this.card.x, y: this.card.y, width: this.card.width, height: this.card.height};
            if (within(card, x, y, 4, 4) || (!pressed && within(this._cardWord, x, y, 4, 4)))
                return; // the word, the gap and the card keep it open; a click beside it closes it
            this._closeCard();
        }
        if (pressed)
            return; // selecting with the mouse: no card
        const line = this._lines.find(l => within(l.rect, x, y, 1, 3));
        if (line)
            this._openCard(line);
    }

    _openCard({mark, rect}) {
        const card = this.card;
        card.destroy_all_children();
        // A spelling mistake's message ("Wykryto prawdopodobny błąd pisowni") says no more than its underline does; a grammar
        // or style issue's says what is wrong.
        if (mark.message && mark.kind !== 'spelling') {
            const message = new St.Label({text: mark.message, style_class: 'gd-mark-message'});
            message.clutter_text.line_wrap = true;
            card.add_child(message);
        }
        const fixes = new St.BoxLayout({style_class: 'gd-mark-fixes'});
        if (mark.suggestions.length) {
            mark.suggestions.forEach((suggestion, n) => {
                const button = fixButton(suggestion, n === 0);
                button.connect('clicked', () => this._action(`live:fix:${mark.id}:${n}`));
                fixes.add_child(button);
            });
        } else {
            const word = wholeText(new St.Label({style_class: `gd-live-word gd-live-${mark.kind}`}));
            word.clutter_text.set_markup(`<span underline="error" underline_color="${WAVE}">${escape(mark.word)}</span>`);
            fixes.add_child(word);
        }
        card.add_child(fixes);
        const actions = new St.BoxLayout({style_class: 'gd-mark-actions'});
        const add = (icon, name, action) => {
            const button = new St.Button({style_class: 'gd-mark-action gd-mark-icon', reactive: true, can_focus: false,
                track_hover: true, accessible_name: name, child: new St.Icon({icon_name: icon, style_class: 'gd-mark-glyph'})});
            button.connect('clicked', () => this._action(action));
            button.connect('notify::hover', () => (button.hover ? this._showTip(button, name) : this._hideTip()));
            actions.add_child(button);
        };
        add('action-unavailable-symbolic', tr('Ignoruj', this._uiLanguage), `live:ignore:${mark.id}`);
        if (mark.learn)
            add('accessories-dictionary-symbolic', tr('Dodaj do słownika', this._uiLanguage), `live:learn:${mark.id}`);
        if (mark.ruleOff) // every issue of this rule, until it is turned on again in the app (Pisanie)
            add('view-conceal-symbolic', tr('Wyłącz regułę', this._uiLanguage), `live:rule_off:${mark.id}`);
        actions.add_child(new St.Widget({x_expand: true}));
        actions.add_child(new St.Label({text: mark.source, style_class: 'gd-mark-source', y_align: Clutter.ActorAlign.CENTER}));
        card.add_child(actions);
        // The suggestions side by side while they fit in the card; else one under the other, each whole (a word one sees
        // two letters of is a word one cannot judge), and the card grows to the longest, up to CARD_WIDE. The width set for
        // the card before goes first: with it the card's natural width is that width, and a card opened after a narrow one
        // (a word without suggestions) squeezed its suggestions to "darm…".
        card.set_width(-1);
        const [, row] = fixes.get_preferred_width(-1);
        const stacked = mark.suggestions.length > 0 && row > CARD_MAX - paddingOf(card);
        if (stacked)
            stackFixes(fixes);
        const [, natural] = card.get_preferred_width(-1);
        const area = Main.layoutManager.getWorkAreaForMonitor(global.display.get_current_monitor());
        const width = Math.min(Math.max(natural, CARD_MIN), stacked ? CARD_WIDE : CARD_MAX, area.width - 8);
        card.set_width(width);
        const [, height] = card.get_preferred_height(width);
        let y = rect.y + rect.height + 4; // under the word, as macOS; above it when there is no room
        if (y + height > area.y + area.height)
            y = rect.y - height - 4;
        const x = Math.min(Math.max(rect.x - 8, area.x + 4), area.x + area.width - width - 4);
        card.set_position(Math.round(x), Math.round(y));
        card.show();
        this._cardMark = mark;
        this._cardWord = rect;
    }

    _showTip(button, text) {
        if (this._gone || !this._tip)
            return;
        const tip = this._tip;
        tip.text = text;
        const [bx, by] = button.get_transformed_position();
        const [bw, bh] = button.get_transformed_size();
        const [, width] = tip.get_preferred_width(-1);
        const area = Main.layoutManager.getWorkAreaForMonitor(global.display.get_current_monitor());
        const x = Math.min(Math.max(bx + bw / 2 - width / 2, area.x + 4), area.x + area.width - width - 4);
        tip.set_position(Math.round(x), Math.round(by + bh + 6));
        tip.show();
    }

    _hideTip() {
        if (!this._gone)
            this._tip?.hide();
    }

    _closeCard() {
        if (this._gone)
            return;
        this._hideTip();
        this.card.hide();
        this._cardMark = this._cardWord = null;
    }

    _action(action) {
        this._closeCard();
        this._onAction(action);
    }

    _detach() {
        if (this._actor && this._actorDestroyId)
            this._actor.disconnect(this._actorDestroyId);
        this._actorDestroyId = 0;
        this._area?.destroy();
        this._area = this._actor = this._window = null;
    }

    hide() {
        if (this._gone)
            return;
        if (this._pollId)
            GLib.source_remove(this._pollId);
        this._pollId = 0;
        this._closeCard();
        this._detach();
        this._lines = [];
    }

    _destroyed() {
        if (this._pollId)
            GLib.source_remove(this._pollId);
        this._pollId = 0;
        this._gone = true;
        this._tip?.destroy();
        this._detach();
        this._lines = [];
    }

    destroy() {
        if (!this._gone) {
            this.hide();
            this.card.destroy(); // the tip goes with it (_destroyed)
        }
    }

    describe() {
        if (this._gone)
            return {visible: false};
        const buttons = [];
        const walk = actor => {
            if (actor instanceof St.Button) {
                const [x, y] = actor.get_transformed_position();
                buttons.push({label: actor.label || actor.accessible_name, ellipsized: cut(actor), x: Math.round(x), y: Math.round(y), width: Math.round(actor.width),
                    height: Math.round(actor.height)});
            }
            actor.get_children().forEach(walk);
        };
        if (this.card.visible)
            walk(this.card);
        return {
            visible: !!this._area?.visible && this._lines.length > 0,
            inWindow: !!this._area && this._area.get_parent() === this._actor,
            window: this._window ? String(this._window.get_id()) : null,
            lines: this._lines.map(({mark, rect}) => ({id: mark.id, kind: mark.kind, x: Math.round(rect.x), y: Math.round(rect.y),
                width: Math.round(rect.width), height: Math.round(rect.height)})),
            card: this.card.visible ? {x: Math.round(this.card.x), y: Math.round(this.card.y), width: Math.round(this.card.width),
                height: Math.round(this.card.height), focusable: this.card.can_focus, id: this._cardMark?.id ?? null, buttons,
                message: this.card.get_children().some(c => c.has_style_class_name?.('gd-mark-message')),
                tip: this._tip?.visible ? this._tip.text : null} : null,
        };
    }
}
