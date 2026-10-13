// GramaDrama: the live check's badge (linux/docs/live-check.md), as macOS LiveMonitor draws it: the
// number of issues in a small brick circle, a list of them on hover, and a click that corrects. While a
// check runs with no count yet, three lavender blobs drift like wax in a lava lamp and, with the result,
// merge into the circle (lava.js; branding/PANEL.md).
// macOS puts it at the bottom right of the field. So does this one in a browser's page, whose extension
// tells where the field is (the view's field); elsewhere GNOME Shell knows the caret but not the field,
// so it stands at the focused window's right edge, level with the caret's line (a chat's input box: its end).
//
//   hover the badge     the list: the word with the app's wave, its suggestions as buttons, the reason;
//                       its header's "Popraw…" does what the badge's click does
//   the view's note     Model only (version 18): why checking finds nothing it may (the model does not answer,
//                       sentences unchecked); with no issues the circle says „!”, and the list says the note
//   click a suggestion  Action(0, 'live:fix:<issue id>:<n>'): the engine fixes that word
//   click the badge     Action(0, 'live:open'): Ctrl+Alt+G
//
// Nothing here takes the keyboard focus: buttons are reactive but never focusable, like the panel's.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import {tr, uiLanguage} from './i18n.js';
import * as Lava from './lava.js';

const MAX_ISSUES = 5;
const LIST_WIDTH = 280;
const LIST_WIDE = 460; // the list grows to this for a long suggestion; beyond it the suggestion wraps
const EDGE = 8; // from the window's edge and the work area's
const LEAVE_MS = 350; // the list stays this long after the pointer leaves the badge and the list
const MAX_JSON = 100000;
const KINDS = new Set(['spelling', 'grammar', 'style']);
// The badge's colours (branding/BRAND.md): lavender while the app works, brick for what it found.
const LAVENDER = {dark: Lava.rgb('#BCB0EC'), light: Lava.rgb('#66579E')};
const BRICK = Lava.rgb('#AD4E2C');
const BRICK_HOVER = Lava.rgb('#9A4426');
const BREATH_MS = 1400; // a known count breathes this slowly while a new check runs
// What the circle says: a count, or NOTE, the „!” of a note with no issues (macOS Lava.swift); „!” has the one-digit
// circle's shape.
const NOTE = -1;
const circleText = value => value === NOTE ? '!' : value ? Lava.countText(value) : '';
const circleSize = value => value === NOTE ? 1 : value;

// Brick #D07A52 (branding/BRAND.md): the wave under a word the check found, here and in the underline card, the same
// as the underline drawn in the app (underlines.js).
export const WAVE = '#D07A52';

function escape(text) {
    return GLib.markup_escape_text(text, -1);
}

/** A word the check found: whole, with the wave under it as in the app. */
export function waveWord(word, kind) {
    const label = wholeText(new St.Label({style_class: `gd-live-word gd-live-${kind}`, y_align: Clutter.ActorAlign.CENTER}));
    label.clutter_text.set_markup(`<span underline="error" underline_color="${WAVE}">${escape(word)}</span>`);
    return label;
}

/** A box [x, y, width, height] of numbers, or null. */
export function box(value) {
    return Array.isArray(value) && value.length === 4 && value.every(n => typeof n === 'number' && Number.isFinite(n))
        ? value : null;
}

/** Text that is never cut to an ellipsis: a word one sees two letters of is a word one cannot judge. A word wider than
 * its card wraps, at any letter if it must. */
export function wholeText(label) {
    const text = label?.clutter_text;
    if (text) {
        text.ellipsize = Pango.EllipsizeMode.NONE;
        text.line_wrap = true;
        text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    }
    return label;
}

/** A suggestion's button (the label is the suggestion, shown whole). */
export function fixButton(suggestion, first) {
    const button = new St.Button({label: suggestion, style_class: first ? 'gd-live-fix gd-live-first' : 'gd-live-fix',
        reactive: true, can_focus: false, y_align: Clutter.ActorAlign.CENTER});
    wholeText(button.get_child());
    return button;
}

/** Stack the buttons of fixes one under the other, each at its natural width and at the start. */
export function stackFixes(fixes) {
    fixes.orientation = Clutter.Orientation.VERTICAL;
    for (const button of fixes.get_children())
        button.x_align = Clutter.ActorAlign.START;
}

/** The horizontal padding of an actor's style, 0 before it has one (not on the stage yet). */
export function paddingOf(actor) {
    try {
        return actor.get_theme_node().get_horizontal_padding();
    } catch (e) {
        return 0;
    }
}

/** Whether a button's text is drawn cut (tests: a suggestion must never be): with an ellipsis, or a button laid out narrower
 * than its text (St draws the ellipsis then without the layout saying so). */
export function cut(button) {
    if (button.get_child?.()?.clutter_text?.get_layout?.()?.is_ellipsized?.())
        return true;
    if (!button.has_allocation?.())
        return false;
    const [, natural] = button.get_preferred_width(-1);
    return button.allocation.get_width() + 0.5 < natural;
}

/** The view of a Live call, checked; throws with the reason for anything else. */
export function parseLive(json) {
    if (typeof json !== 'string' || json.length > MAX_JSON)
        throw new Error('not a string, or too long');
    const raw = JSON.parse(json);
    if (!raw || typeof raw !== 'object' || raw.v !== 1 || !Number.isInteger(raw.count) || raw.count < 0)
        throw new Error('unknown view');
    const text = (value, max) => typeof value === 'string' ? value.slice(0, max) : '';
    const issues = (Array.isArray(raw.issues) ? raw.issues : []).slice(0, MAX_ISSUES).map(issue => ({
        id: text(issue?.id, 200),
        word: text(issue?.word, 200),
        message: text(issue?.message, 300),
        kind: KINDS.has(issue?.kind) ? issue.kind : 'grammar',
        source: text(issue?.source, 40),
        suggestions: (Array.isArray(issue?.suggestions) ? issue.suggestions : []).filter(s => typeof s === 'string')
            .slice(0, 3).map(s => s.slice(0, 200)),
    }));
    // field: the field's visible box in the window's client coordinates (from: 'client', a browser's page).
    const field = box(raw.field);
    return {ui_language: uiLanguage(raw.ui_language), count: raw.count, checking: raw.checking === true,
        summary: text(raw.summary, 40), issues, note: text(raw.note, 300).trim(),
        more: Number.isInteger(raw.more) && raw.more > 0 ? raw.more : 0,
        field: field && field[2] > 0 && field[3] > 0 && raw.from === 'client' ? field : null};
}

export class LiveBadge {
    constructor({onAction}) {
        this._onAction = onAction;
        this._variant = 'dark';
        this._gone = false;
        this._leaveTimer = 0;
        this._view = null;
        this._stale = false; // a newer view came while the pointer was on the list
        // What the badge draws: 'hidden', 'thinking' (the blobs), 'merging' (into the result), 'count' (the circle).
        this._mode = 'hidden';
        this._checking = false;
        this._target = 0; // the count the merge ends in (0: the merged blob fades away)
        this._began = 0; // ms: when the blobs started to drift
        this._mergeAt = 0; // ms: when they started to merge
        this._popped = false; // the merge's count has popped in
        this._shown = 0; // the count in the circle
        this.badge = new St.Button({
            name: 'gramadrama-live-badge',
            style_class: 'gd-live-badge',
            reactive: true,
            can_focus: false,
            track_hover: true,
            visible: false,
        });
        const stack = new St.Widget({layout_manager: new Clutter.BinLayout()});
        this._area = new St.DrawingArea({width: Lava.areaWidth(1), height: Lava.HEIGHT});
        this._area.connect('repaint', area => this._repaint(area));
        this._count = new St.Label({style_class: 'gd-live-count', x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER});
        this._count.set_pivot_point(0.5, 0.5);
        stack.add_child(this._area);
        stack.add_child(this._count);
        this.badge.set_child(stack);
        // Frames come from the badge's own frame clock, and only while something moves (_syncTimeline).
        this._timeline = new Clutter.Timeline({actor: this._area, duration: 1000, repeat_count: -1});
        this._timeline.connect('new-frame', () => this._frame());
        this.badge.connect('clicked', () => this._action('live:open'));
        this.badge.connect('notify::hover', () => {
            this._area.queue_repaint(); // a darker circle under the pointer
            this._hoverChanged();
        });
        this.list = new St.BoxLayout({
            name: 'gramadrama-live-list',
            style_class: 'gd-live-list gd-dark',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: true, // hover keeps it open; clicks go to its buttons
            can_focus: false,
            track_hover: true,
            visible: false,
            width: LIST_WIDTH,
        });
        this._listWidth = LIST_WIDTH;
        this.list.connect('notify::hover', () => this._hoverChanged());
        for (const actor of [this.badge, this.list])
            actor.connect('destroy', () => this._destroyed());
    }

    _destroyed() {
        this._gone = true;
        this._stopTimer();
        this._timeline?.stop();
        this._timeline = null;
    }

    setVariant(variant) {
        if (this._gone || variant === this._variant)
            return;
        this.list.remove_style_class_name(`gd-${this._variant}`);
        this._variant = variant;
        this.list.add_style_class_name(`gd-${variant}`);
        this._area.queue_repaint(); // the blobs' lavender follows the theme
    }

    /** The view last shown (its field places the badge when the caret moves). */
    get view() {
        return this._view;
    }

    /** Draws the view at place = {x, y} of the badge's right edge and vertical centre, or {x, bottom}
     *  of its right and bottom edges; true when shown. */
    show(view, place) {
        if (this._gone)
            return false;
        this._view = view;
        const moving = this._animations();
        const value = view.count || (view.note ? NOTE : 0); // what the circle says
        if ((!value && !view.checking) || !place) {
            if (place && moving && this.badge.visible && (this._mode === 'thinking' || this._mode === 'merging')) {
                // a check that found nothing: the blobs merge and fade away; nothing is shown any more
                this._merge(0);
                this._place = place;
                this._position();
                return false;
            }
            this.hide();
            return false;
        }
        this.badge.accessible_name = this._summary() || view.note; // a „!” says its note
        this._checking = view.checking;
        if (view.checking && !view.count) {
            if (this._mode !== 'thinking') {
                // nothing known yet: the blobs drift until the result
                this._mode = 'thinking';
                this._began = this._now();
                this._setCount(0, false);
            }
        } else if (!view.checking && moving && (this._mode === 'thinking' || this._mode === 'merging')) {
            this._merge(value); // the result: the blobs merge into the circle with the count (or „!”)
        } else if (this._mode !== 'merging') {
            // a count to show at once; a new one pops in (a known count stays, breathing, while a check runs)
            const changed = this._mode !== 'count' || this._count.text !== circleText(value);
            this._mode = 'count';
            this._setCount(value, changed && moving);
        }
        this.badge.show();
        this._syncTimeline();
        this._area.queue_repaint();
        this._place = place;
        this._position();
        if (this.list.visible) {
            if (this.list.hover && view.issues.length)
                this._stale = true; // never under the pointer: the suggestion being aimed at would move
            else {
                this._fillList();
                this._positionList();
            }
        }
        return true;
    }

    move(place) {
        if (this._gone || !this.badge.visible || !place)
            return;
        this._place = place;
        this._position();
    }

    _position() {
        const [, width] = this.badge.get_preferred_width(-1);
        const [, height] = this.badge.get_preferred_height(width);
        const top = this._place.bottom !== undefined ? this._place.bottom - height : this._place.y - height / 2;
        this.badge.set_position(Math.round(this._place.x - width), Math.round(top));
        if (this.list.visible)
            this._positionList();
    }

    _positionList() {
        const [, height] = this.list.get_preferred_height(this._listWidth);
        const area = this._place.area;
        let x = this.badge.x + this.badge.width - this._listWidth;
        let y = this.badge.y - height - 6; // above the badge (macOS: the list grows up and left)
        if (area && y < area.y + EDGE)
            y = this.badge.y + this.badge.height + 6;
        if (area)
            x = Math.min(Math.max(x, area.x + EDGE), area.x + area.width - this._listWidth - EDGE);
        this.list.set_position(Math.round(x), Math.round(y));
    }

    _hoverChanged() {
        if (this._gone)
            return;
        if (this._stale && this.list.visible && !this.list.hover) {
            this._fillList();
            this._positionList();
        }
        if (this.badge.hover || this.list.hover) {
            this._stopTimer();
            if (!this.list.visible && this.badge.visible) {
                this._fillList();
                this.list.show();
                this._positionList();
            }
            return;
        }
        this._stopTimer();
        this._leaveTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, LEAVE_MS, () => {
            this._leaveTimer = 0;
            if (!this.badge.hover && !this.list.hover)
                this.list.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    _fillList() {
        this._stale = false;
        this.list.destroy_all_children();
        this._open = null;
        const view = this._view;
        const header = new St.BoxLayout({style_class: 'gd-live-header', x_expand: true});
        this._summaryLabel = new St.Label({text: this._summary(), style_class: 'gd-live-summary', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        header.add_child(this._summaryLabel);
        if (this._summaryLabel.text)
            this.list.add_child(header);
        let note = null;
        if (view.note) {
            // the note under the header, rose with a warning sign, as macOS puts it
            note = new St.BoxLayout({style_class: 'gd-live-note'});
            note.add_child(new St.Icon({icon_name: 'dialog-warning-symbolic', icon_size: 12, style_class: 'gd-live-note-icon',
                y_align: Clutter.ActorAlign.START}));
            const label = new St.Label({text: view.note, style_class: 'gd-live-note-text', x_expand: true});
            label.clutter_text.line_wrap = true;
            note.add_child(label);
            this.list.add_child(note);
        }
        if (!view.count && !view.issues.length) {
            // A hover with no issues (checking, a note) is a short status tooltip, never an empty suggestion list.
            const widest = Math.max(this._summaryLabel.text ? this._summaryLabel.get_preferred_width(-1)[1] : 0,
                note ? note.get_preferred_width(-1)[1] : 0);
            this._listWidth = Math.min(Math.ceil(widest + paddingOf(this.list)), LIST_WIDTH);
            this.list.set_width(this._listWidth);
            return;
        }
        // "Popraw…": the badge's own click (Ctrl+Alt+G, the panel), named where the list is read; the ellipsis says that
        // it opens the panel and fixes nothing by itself
        this._open = new St.Button({label: tr('Popraw…', view.ui_language), style_class: 'gd-live-open', reactive: true,
            can_focus: false, y_align: Clutter.ActorAlign.CENTER});
        this._open.connect('clicked', () => this._action('live:open'));
        header.add_child(this._open);
        const room = LIST_WIDTH - paddingOf(this.list);
        let needed = 0; // the widest line that cannot be wrapped: the list grows to it (up to LIST_WIDE)
        for (const issue of view.issues) {
            const row = new St.BoxLayout({style_class: 'gd-live-row', orientation: Clutter.Orientation.VERTICAL});
            const line = new St.BoxLayout({style_class: 'gd-live-line'});
            line.add_child(waveWord(issue.word, issue.kind));
            const fixes = new St.BoxLayout({style_class: 'gd-live-line'});
            issue.suggestions.forEach((suggestion, n) => {
                const button = fixButton(suggestion, n === 0);
                button.connect('clicked', () => this._action(`live:fix:${issue.id}:${n}`));
                fixes.add_child(button);
            });
            if (issue.suggestions.length) {
                line.add_child(new St.Label({text: '→', style_class: 'gd-live-arrow', y_align: Clutter.ActorAlign.CENTER}));
                line.add_child(fixes);
            }
            row.add_child(line);
            if (issue.message) {
                const message = new St.Label({text: issue.message, style_class: 'gd-live-message'});
                message.clutter_text.line_wrap = true;
                message.clutter_text.ellipsize = Pango.EllipsizeMode.END;
                row.add_child(message);
            }
            this.list.add_child(row);
            // The word, the arrow and the suggestions in one line while they fit; else the suggestions go one under
            // the other, each whole.
            let [, natural] = line.get_preferred_width(-1);
            if (issue.suggestions.length && natural > room) {
                line.remove_child(fixes);
                stackFixes(fixes);
                row.insert_child_at_index(fixes, 1);
                [, natural] = fixes.get_preferred_width(-1);
                needed = Math.max(needed, natural + paddingOf(this.list));
            }
        }
        this._listWidth = Math.min(Math.max(LIST_WIDTH, needed), LIST_WIDE);
        this.list.set_width(this._listWidth);
        if (view.more)
            this.list.add_child(new St.Label({text: tr('i {count} więcej', view.ui_language, {count: view.more}), style_class: 'gd-live-more'}));
    }

    _action(action) {
        this.list.hide();
        if (action === 'live:open')
            this.hide();
        this._onAction(action);
    }

    _summary() {
        const view = this._view;
        if (!view.checking)
            return view.summary;
        const checking = tr('Sprawdzanie…', view.ui_language);
        return view.count && view.summary ? `${checking} · ${view.summary}` : checking;
    }

    _now() {
        return GLib.get_monotonic_time() / 1000;
    }

    /** GNOME's "Animations" setting (Accessibility): off means still blobs and no merge or pop. */
    _animations() {
        return St.Settings.get().enable_animations;
    }

    /** The count in the circle (0: none); `pop` scales it in. The area fits the circle or the pill. */
    _setCount(count, pop) {
        this._count.remove_all_transitions();
        this._shown = count;
        this._count.text = circleText(count);
        const width = Lava.areaWidth(circleSize(count) || 1);
        if (this._area.width !== width) {
            this._area.set_width(width);
            if (this._place)
                this._position();
        }
        if (pop && count) {
            this._count.set_opacity(0);
            this._count.set_scale(0.5, 0.5);
            this._count.ease({opacity: 255, scale_x: 1, scale_y: 1, duration: Lava.NUMBER_MS,
                mode: Clutter.AnimationMode.EASE_OUT_BACK});
        } else {
            this._count.set_opacity(255);
            this._count.set_scale(1, 1);
        }
    }

    _merge(count) {
        if (this._mode === 'merging' && this._target === count)
            return; // the same result again (a page sends its state every 0.35 s): let the merge run on
        if (this._mode !== 'merging')
            this._mergeAt = this._now();
        this._mode = 'merging';
        this._target = count;
        this._popped = false;
        this._checking = false;
        const text = circleText(count);
        this._count.remove_all_transitions();
        this._count.set_opacity(0); // it pops in once the blobs are one
        this._count.text = text;
        if (count && this._area.width !== Lava.areaWidth(circleSize(count))) {
            this._area.set_width(Lava.areaWidth(circleSize(count)));
            if (this._place)
                this._position();
        }
        this._syncTimeline();
        this._area.queue_repaint();
    }

    /** Frames run while the blobs drift, while they merge, and while a known count breathes during a check. */
    _syncTimeline() {
        if (!this._timeline)
            return;
        const moving = this._animations() && this.badge.visible !== false && (this._mode === 'thinking' ||
            this._mode === 'merging' || (this._mode === 'count' && this._checking));
        if (moving && !this._timeline.is_playing())
            this._timeline.start();
        else if (!moving && this._timeline.is_playing())
            this._timeline.stop();
    }

    _frame() {
        if (this._gone)
            return;
        if (this._mode === 'merging') {
            const elapsed = this._now() - this._mergeAt;
            if (this._target && !this._popped && elapsed >= Lava.MERGE_MS) {
                this._popped = true;
                this._setCount(this._target, true);
            }
            if (this._target && elapsed >= Lava.MERGE_MS + Lava.TINT_MS) {
                this._mode = 'count';
                this._shown = this._target;
                this._syncTimeline();
            } else if (!this._target && elapsed >= Lava.MERGE_MS + Lava.FADE_MS) {
                this.hide();
                return;
            }
        }
        this._area.queue_repaint();
    }

    _repaint(area) {
        const cr = area.get_context(); // in logical pixels, as underlines.js draws
        const w = area.width;
        const now = this._now();
        const lavender = LAVENDER[this._variant] ?? LAVENDER.dark;
        let blobs = null, colour = BRICK, alpha = 1, shape = false;
        if (this._mode === 'thinking') {
            blobs = Lava.drift(this._animations() ? now - this._began : 0, w);
            colour = lavender;
        } else if (this._mode === 'merging') {
            const elapsed = now - this._mergeAt;
            const from = Lava.drift(this._mergeAt - this._began, w);
            blobs = Lava.merge(from, elapsed / Lava.MERGE_MS, circleSize(this._target) || 1, w);
            colour = lavender;
            if (elapsed >= Lava.MERGE_MS) {
                shape = true;
                blobs = null;
                if (this._target)
                    colour = Lava.mix(lavender, BRICK, (elapsed - Lava.MERGE_MS) / Lava.TINT_MS);
                else
                    alpha = Math.max(0, 1 - (elapsed - Lava.MERGE_MS) / Lava.FADE_MS);
            }
        } else if (this._mode === 'count') {
            shape = true;
            colour = this.badge.hover ? BRICK_HOVER : BRICK;
            if (this._checking && this._animations())
                alpha = 0.72 + 0.28 * (0.5 + 0.5 * Math.cos(2 * Math.PI * (now % BREATH_MS) / BREATH_MS));
        }
        cr.setSourceRGBA(colour[0], colour[1], colour[2], alpha);
        if (blobs) {
            for (const b of blobs) {
                cr.arc(b.x, b.y, b.r, 0, 2 * Math.PI);
                cr.fill();
            }
            for (const b of Lava.bridges(blobs)) {
                cr.moveTo(...b.p1);
                cr.curveTo(...b.h1, ...b.h3, ...b.p3);
                cr.lineTo(...b.p4);
                cr.curveTo(...b.h4, ...b.h2, ...b.p2);
                cr.closePath();
                cr.fill();
            }
        } else if (shape) {
            // the circle, or a pill for two or more digits
            const sw = Lava.shapeWidth(circleSize(this._mode === 'merging' ? this._target : this._shown) || 1);
            const r = Lava.RADIUS, x0 = (w - sw) / 2 + r, x1 = (w + sw) / 2 - r, y = Lava.HEIGHT / 2;
            cr.arc(x0, y, r, Math.PI / 2, 3 * Math.PI / 2);
            cr.arc(x1, y, r, -Math.PI / 2, Math.PI / 2);
            cr.closePath();
            cr.fillPreserve();
            // a faint light rim, so that the circle stays apart from a dark or brick-coloured app behind it
            cr.setSourceRGBA(1, 1, 1, 0.35 * alpha);
            cr.setLineWidth(1);
            cr.stroke();
        }
        cr.$dispose();
    }

    hide() {
        if (this._gone)
            return;
        this._stopTimer();
        this._mode = 'hidden';
        this._checking = false;
        this._shown = 0;
        this._timeline?.stop();
        this._count.remove_all_transitions();
        this.badge.hide();
        this.list.hide();
    }

    _stopTimer() {
        if (this._leaveTimer) {
            GLib.source_remove(this._leaveTimer);
            this._leaveTimer = 0;
        }
    }

    destroy() {
        if (!this._gone) {
            this.hide();
            this.badge.destroy();
            this.list.destroy();
        }
    }

    describe() {
        if (this._gone)
            return {visible: false};
        const button = actor => ({label: actor.label, x: Math.round(actor.get_transformed_position()[0]),
            y: Math.round(actor.get_transformed_position()[1]), width: Math.round(actor.width),
            height: Math.round(actor.height), ellipsized: cut(actor)});
        const buttons = []; // the suggestions; the header's "Popraw…" is `open`
        const walk = actor => {
            if (actor instanceof St.Button && actor !== this.badge && actor !== this._open)
                buttons.push(button(actor));
            actor.get_children().forEach(walk);
        };
        if (this.list.visible)
            walk(this.list);
        const open = this.list.visible && this._open?.mapped ? button(this._open) : null;
        return {visible: this.badge.visible, count: this._count.text, indicator: this._mode,
            spinner: this.badge.visible && this._checking,
            accessibleName: this.badge.accessible_name, summary: this._summaryLabel?.text ?? '',
            note: this.list.visible ? this.list.get_children().find(c => c.has_style_class_name('gd-live-note'))
                ?.get_children()[1].text ?? null : null,
            x: Math.round(this.badge.x), y: Math.round(this.badge.y),
            width: Math.round(this.badge.width), height: Math.round(this.badge.height), reactive: this.badge.reactive,
            focusable: this.badge.can_focus, list: this.list.visible, buttons, open};
    }
}
