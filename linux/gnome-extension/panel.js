// The GramaDrama correction panel and message toast, built from St widgets.
// Design: branding/PANEL.md (shared with macOS: Sources/GramaDrama/PanelView.swift).
// Data: the view JSON (linux/docs/architecture.md, View JSON), already normalised by parseView().
// Nothing here can take keyboard focus: every button is reactive but can_focus: false.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import {tr, uiLanguage} from './i18n.js';
import {byteIndex, codeOffset, lineStarts} from './textmap.js';

import * as Animation from 'resource:///org/gnome/shell/ui/animation.js';

export const PANEL_WIDTH = 520;
export const PADDING = 16;
const BORDER = 1;
const CONTENT_WIDTH = PANEL_WIDTH - 2 * (PADDING + BORDER);
export const MODES = ['Popraw', 'Ulepsz', 'Formalnie', 'Luźniej', 'Krócej', 'Tłumacz'];
export const TRANSLATE = 5;
export const MAX_CHANGES = 30;
const ORIGINAL_LINES = 3;
const SCOPE_WORDS = {paragraph: 'akapit', selection: 'zaznaczenie', field: 'całe pole'};
const DIRECTIONS = {en: 'PL → EN ⇄', pl: 'EN → PL ⇄'};
const DONE = new Set(['ready', 'unchanged', 'applying']);
const COPIED_MS = 1500;
const STATUSES = new Set(['checking', 'working', 'ready', 'unchanged', 'error', 'applying', 'apply_pending']);
// The buttons a failed correction can offer (engine/problems.py); the first one is Enter's
const PROBLEM_ACTIONS = new Set(['retry', 'open:connection', 'open:writing', 'login']);
// Bounds that keep the Show handler cheap whatever arrives (the engine captures at most 6000
// characters; a Pango layout of the whole text runs synchronously while the panel is placed).
const MAX_JSON = 1000000; // characters of the view JSON
const MAX_TEXT = 20000; // source, text
const MAX_SEGMENTS = 2000; // diff segments after merging same-kind neighbours
const MAX_DIFF_CHARS = 20000; // above either bound the result is shown as plain text
const MAX_ORIGINAL = 1000; // characters of the translation's original measured for its 3 lines

// Pango markup colours per variant (CSS does not reach markup spans), from branding/BRAND.md:
// what the correction changed sits on a sage tint in the result; in the change list the new text is
// sage, removed text is struck in the secondary text colour, and a lone punctuation change gets a
// chip (sage for added, the subtle surface for removed).
const INK = {
    dark: {removed: '#B9BDB8', removedBg: '#303631', added: '#ADC8B5', addedBg: '#34483C', dim: '#B9BDB8',
        selection: '#433B63', brick: '#D07A52'},
    light: {removed: '#6B6870', removedBg: '#F0EDE7', added: '#536B60', addedBg: '#E9F0E9', dim: '#6B6870',
        selection: '#E2DCF2', brick: '#D07A52'},
};
const THIN = '\u2009'; // around a lone punctuation change in a change row, so its chip is wide enough to see

/** Punctuation or spaces only ("," "—" "␣␣"): too small to see without a tinted chip. */
function isMark(text) {
    return text !== '' && !/[\p{L}\p{N}]/u.test(text);
}

export function esc(text) {
    return GLib.markup_escape_text(String(text ?? ''), -1);
}

/**
 * The result being edited as markup: the selection [start, end) (code points) on the lavender tint, what it changes
 * against the original on the sage tint as in the result (marks: [start, end) in code points), and what the check found
 * in it with the live check's brick wave (issues: {start, end} in code points).
 */
export function editMarkup(text, cursor, anchor, ink, issues = [], marks = []) {
    const chars = Array.from(text);
    const start = Math.min(cursor, anchor), end = Math.max(cursor, anchor);
    const cuts = new Set([0, chars.length, start, end, ...issues.flatMap(i => [i.start, i.end]), ...marks.flat()]);
    const points = [...cuts].filter(p => p >= 0 && p <= chars.length).sort((a, b) => a - b);
    let out = '';
    for (let k = 0; k + 1 < points.length; k++) {
        const a = points[k], b = points[k + 1];
        let piece = esc(chars.slice(a, b).join(''));
        if (issues.some(i => i.start <= a && b <= i.end))
            piece = `<span underline="error" underline_color="${ink.brick}">${piece}</span>`;
        // one background: the selection over a mark, so selecting stays visible in corrected words
        if (start < end && start <= a && b <= end)
            piece = `<span background="${ink.selection}">${piece}</span>`;
        else if (marks.some(([from, to]) => from <= a && b <= to))
            piece = `<span background="${ink.addedBg}">${piece}</span>`;
        out += piece;
    }
    return out;
}

/** Polish plural of "poprawka" (1 poprawka, 2-4 poprawki, 5+ poprawek, 12-14 poprawek, 22 poprawki). */
export function pluralChanges(n, language = 'pl') {
    if (uiLanguage(language) !== 'pl')
        return tr(n === 1 ? 'poprawka' : 'poprawek', language);
    if (n === 1)
        return 'poprawka';
    const last = n % 10, lastTwo = n % 100;
    return last >= 2 && last <= 4 && !(lastTwo >= 12 && lastTwo <= 14) ? 'poprawki' : 'poprawek';
}

/** Diff segments merged with same-kind neighbours; [] (plain text instead) when too big to draw. */
function boundedSegments(raw) {
    if (!Array.isArray(raw))
        return [];
    const out = [];
    let chars = 0;
    for (const s of raw) {
        if (!Array.isArray(s) || typeof s[0] !== 'string' || typeof s[1] !== 'string' || !s[1])
            continue;
        chars += s[1].length;
        if (chars > MAX_DIFF_CHARS)
            return [];
        const last = out[out.length - 1];
        if (last && last[0] === s[0])
            last[1] += s[1];
        else if (out.push([s[0], s[1]]) > MAX_SEGMENTS)
            return [];
    }
    return out;
}

/**
 * The view JSON from the engine, validated, bounded and with defaults (architecture.md, View JSON). Throws
 * on a view that cannot be shown at all (too large, not an object, no uint32 session).
 */
export function parseView(json) {
    if (json.length > MAX_JSON)
        throw new Error(`the view is larger than ${MAX_JSON} characters`);
    const raw = JSON.parse(json);
    if (!raw || typeof raw !== 'object' || Array.isArray(raw))
        throw new Error('the view must be a JSON object');
    const {session} = raw;
    if (!Number.isInteger(session) || session < 0 || session > 0xffffffff)
        throw new Error('view.session must be an unsigned 32-bit integer');
    const str = (value, max = MAX_TEXT) => typeof value === 'string' ? value.slice(0, max) : '';
    const segments = boundedSegments(raw.segments);
    const changes = Array.isArray(raw.changes)
        ? raw.changes.slice(0, MAX_CHANGES).filter(c => c && typeof c === 'object')
            .map(c => ({from: str(c.from, 200), to: str(c.to, 200), reason: str(c.reason, 300),
                id: Number.isInteger(c.id) && c.id >= 0 ? c.id : null, keep: c.keep === true}))
        : [];
    const mode = Number.isInteger(raw.mode) && raw.mode >= 0 && raw.mode < MODES.length ? raw.mode : 0;
    const text = str(raw.text);
    const length = Array.from(text).length;
    const offset = value => Number.isInteger(value) && value >= 0 && value <= length ? value : length;
    const id = value => Number.isInteger(value) && value > 0 ? value : 0;
    const clip = raw.editing?.clipboard;
    const issue = i => i && typeof i === 'object' && typeof i.id === 'string' && Number.isInteger(i.start)
        && Number.isInteger(i.end) && i.start >= 0 && i.start <= i.end && i.end <= length;
    const issues = Array.isArray(raw.editing?.issues) ? raw.editing.issues.filter(issue).slice(0, 200).map(i => ({
        id: i.id.slice(0, 200), start: i.start, end: i.end, word: str(i.word, 200), message: str(i.message, 300),
        suggestions: Array.isArray(i.suggestions) ? i.suggestions.filter(x => typeof x === 'string').slice(0, 3)
            .map(x => x.slice(0, 200)) : [],
        learn: i.learn === true,
        ruleOff: i.rule_off === true,
    })) : null;
    const mark = m => Array.isArray(m) && Number.isInteger(m[0]) && Number.isInteger(m[1]) && m[0] >= 0 && m[0] < m[1]
        && m[1] <= length;
    const marks = Array.isArray(raw.editing?.marks) ? raw.editing.marks.filter(mark).slice(0, MAX_SEGMENTS)
        .map(m => [m[0], m[1]]) : [];
    const editing = raw.editing && typeof raw.editing === 'object' && text.length < MAX_TEXT
        ? {cursor: offset(raw.editing.cursor), anchor: offset(raw.editing.anchor), paste: id(raw.editing.paste),
            clipboard: clip && id(clip.id) && typeof clip.text === 'string' ? {id: clip.id, text: clip.text} : null,
            issues, checking: raw.editing.checking === true, marks}
        : null;
    const seconds = typeof raw.seconds === 'number' && raw.seconds > 0 ? Math.min(raw.seconds, 60) : 3;
    const p = raw.status === 'error' && raw.problem && typeof raw.problem === 'object' ? raw.problem : null;
    const problem = p && typeof p.title === 'string' && p.title
        ? {group: [1, 2, 3, 4].includes(p.group) ? p.group : 2, title: str(p.title, 200), text: str(p.text, 2000),
            actions: (Array.isArray(p.actions) ? p.actions : [])
                .filter(a => a && PROBLEM_ACTIONS.has(a.id) && typeof a.label === 'string' && a.label)
                .slice(0, 2).map(a => ({id: a.id, label: str(a.label, 60)}))}
        : null;
    return {
        v: Number.isInteger(raw.v) ? raw.v : 1,
        ui_language: uiLanguage(raw.ui_language),
        session,
        kind: raw.kind === 'message' ? 'message' : 'session',
        status: STATUSES.has(raw.status) ? raw.status : raw.kind === 'message' ? 'message' : 'checking',
        mode,
        target: ['pl', 'en', 'de'].includes(raw.target) ? raw.target : null,
        source_language: ['pl', 'en', 'de'].includes(raw.source_language) ? raw.source_language : null,
        scope: str(raw.scope, 40),
        source: str(raw.source),
        text,
        editing,
        editable: raw.editable === true,
        segments,
        plain: Array.isArray(raw.segments) && raw.segments.length > 0 && segments.length === 0,
        changes,
        changesOpen: raw.changes_open === true,
        retrying: raw.retrying === true,
        count: Number.isInteger(raw.count) && raw.count >= 0 ? raw.count : changes.length,
        note: str(raw.note, 300),
        error: str(raw.error, 2000),
        problem,
        message: str(raw.message, 2000),
        seconds,
        canApply: raw.can_apply !== false,
    };
}

/**
 * The result of a correction as Pango markup: the corrected text alone, what the correction changed on the sage tint
 * (branding/PANEL.md, Content). Removed text is left out (the change list, on demand, says what went), so the label
 * shows exactly the result and a place in it is a place in the result.
 */
export function resultMarkup(segments, ink) {
    let out = '';
    for (const [kind, text] of segments) {
        if (kind === 'added')
            out += `<span background="${ink.addedBg}">${esc(text)}</span>`;
        else if (kind !== 'removed')
            out += esc(text);
    }
    return out;
}

function shorten(text, max) {
    const chars = Array.from(text);
    return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : text;
}

function visible(text) {
    // whitespace-only changes would be invisible: show them as open boxes
    const t = text.replace(/\n/g, '¶');
    return /^\s+$/.test(t) ? t.replace(/\s/g, '␣') : t;
}

/** One change-list row: from (struck, dimmed) → to (sage, bold) · reason (small, dim). */
export function changeMarkup(change, ink) {
    const from = shorten(visible(change.from), 80);
    const to = shorten(visible(change.to), 80);
    const parts = [];
    if (!from && !to)
        parts.push(`<span foreground="${ink.added}" weight="bold">␣</span>`);
    // a lone "," or "—" gets a tinted chip, as in the diff; it would be invisible otherwise
    if (from) {
        const chip = isMark(from) ? ` background="${ink.removedBg}"` : '';
        const pad = chip ? THIN : '';
        parts.push(`<span foreground="${ink.removed}" strikethrough="true" strikethrough_color="${ink.removed}"${chip}>${
            pad}${esc(from)}${pad}</span>`);
    }
    if (to) {
        parts.push(`<span foreground="${ink.dim}">→</span>`);
        parts.push(isMark(to)
            ? `<span foreground="${ink.added}" weight="bold" background="${ink.addedBg}">${THIN}${esc(to)}${THIN}</span>`
            : `<span foreground="${ink.added}" weight="bold">${esc(to)}</span>`);
    }
    if (change.reason)
        parts.push(`<span size="small" foreground="${ink.dim}">· ${esc(shorten(change.reason, 140))}</span>`);
    return parts.join(' ');
}

function wrapLabel(styleClass, text = '') {
    const label = new St.Label({style_class: styleClass, text, x_expand: true});
    const ct = label.clutter_text;
    ct.line_wrap = true;
    ct.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    ct.ellipsize = Pango.EllipsizeMode.NONE;
    return label;
}

function rectOf(actor) {
    if (!actor || !actor.mapped)
        return null;
    const [x, y] = actor.get_transformed_position();
    const [w, h] = actor.get_transformed_size();
    const rect = [x, y, w, h].map(Math.round);
    return rect.every(Number.isFinite) ? rect : null; // not allocated yet
}

/**
 * Text cut to at most `lines` wrapped lines of `width` px in the label's font, with '…'.
 * Measured with Pango, so it matches what the label will draw.
 */
function clampLines(label, text, lines, width) {
    let head = '', n = 0;
    for (const ch of text) { // by code points
        if (n++ === MAX_ORIGINAL)
            break;
        head += ch;
    }
    text = head;
    label.ensure_style();
    const layout = label.clutter_text.create_pango_layout(null);
    // The font St gives the label. Never Clutter.Text.get_font_description(): its typelib says
    // transfer full, so GJS would free the actor's own font description (heap corruption).
    layout.set_font_description(label.get_theme_node().get_font());
    layout.set_width(width * Pango.SCALE);
    layout.set_wrap(Pango.WrapMode.WORD_CHAR);
    layout.set_text(text, -1);
    if (layout.get_line_count() <= lines)
        return text;
    const line = layout.get_line_readonly(lines - 1);
    const bytes = new TextEncoder().encode(text);
    let chars = Array.from(new TextDecoder().decode(bytes.slice(0, line.start_index + line.length)).trimEnd());
    for (let i = 0; i < 60 && chars.length; i++) {
        layout.set_text(`${chars.join('')}…`, -1);
        if (layout.get_line_count() <= lines)
            break;
        chars = chars.slice(0, -2);
    }
    return `${chars.join('').trimEnd()}…`;
}

/**
 * Destroy a direct child of Main.layoutManager.uiGroup. uiGroup is a no-layout actor, so its
 * children are queued as their own relayout roots, and the stage allocates a queued root at the
 * next update even after it was destroyed (St criticals, "Spurious clutter_actor_allocate").
 * get_allocation_box() runs the pending relayouts now, while the actor is still on the stage.
 */
function settleAndDestroy(actor) {
    if (actor.get_stage())
        actor.get_allocation_box();
    actor.destroy();
}

export class Panel {
    constructor({logos, onAction, onEdit = () => {}}) {
        this._logos = logos; // {dark|light: {filled, outline}}
        this._onAction = onAction;
        this._onEdit = onEdit; // (kind, data): the visual lines of the result being edited, or a paste, for the engine
        this._linesKey = null;
        this._clipId = this._pasteId = 0; // the editor's last copy and paste handled (ids grow in the engine)
        this._variant = 'dark';
        this._view = null;
        this._changesKey = null;
        this._copiedId = 0;
        this._buttons = {};
        this._gone = false;

        this.actor = new St.BoxLayout({
            name: 'gramadrama-panel',
            style_class: 'gd-panel gd-dark',
            orientation: Clutter.Orientation.VERTICAL,
            // A pick target, so a click on the panel's background never falls through to the app
            // underneath. It is never focusable and never grabs: the app keeps keyboard focus.
            reactive: true,
            can_focus: false,
            track_hover: false,
            visible: false,
            width: PANEL_WIDTH,
        });
        this._buildHeader();
        this._buildModes();
        this._buildContent();
        this._buildFooter();
        // GNOME Shell may destroy the actor on its own (at shutdown): stop the timer, touch nothing.
        this.actor.connect('destroy', () => {
            this._gone = true;
            if (this._copiedId)
                GLib.source_remove(this._copiedId);
            this._copiedId = 0;
            this._spinning = false;
        });
    }

    _button(label, styleClass, action) {
        const button = new St.Button({
            ...label === null ? {} : {label},
            style_class: styleClass,
            reactive: true,
            can_focus: false,
            track_hover: true,
        });
        button.connect('clicked', () => this._onAction(action));
        this._buttons[action] = button;
        return button;
    }

    _buildHeader() {
        const header = new St.BoxLayout({style_class: 'gd-header', x_expand: true});
        this._logo = new St.Icon({
            gicon: this._logos[this._variant].outline, icon_size: 20, style_class: 'gd-logo',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._logoKind = 'outline';
        header.add_child(this._logo);
        header.add_child(new St.Label({text: 'GramaDrama', style_class: 'gd-title', y_align: Clutter.ActorAlign.CENTER}));
        header.add_child(new St.Widget({x_expand: true}));

        this._direction = this._button(DIRECTIONS.en, 'gd-direction', 'direction');
        this._direction.y_align = Clutter.ActorAlign.CENTER;
        this._direction.visible = false;
        header.add_child(this._direction);

        this._badge = new St.BoxLayout({style_class: 'gd-badge', y_align: Clutter.ActorAlign.CENTER, visible: false});
        this._badgeIcon = new St.Icon({style_class: 'gd-badge-icon', icon_size: 14, y_align: Clutter.ActorAlign.CENTER,
            visible: false});
        this._spinner = new Animation.Spinner(14, {hideOnStop: true});
        this._spinner.add_style_class_name('gd-spinner');
        this._spinner.y_align = Clutter.ActorAlign.CENTER;
        this._badgeLabel = new St.Label({style_class: 'gd-badge-label', y_align: Clutter.ActorAlign.CENTER});
        this._badge.add_child(this._badgeIcon);
        this._badge.add_child(this._spinner);
        this._badge.add_child(this._badgeLabel);
        this._badgeKind = null;
        header.add_child(this._badge);

        // Esc for the keyboard, × for the pointer: the footer keeps only the actions on the text
        this._cancel = this._button(null, 'gd-close', 'cancel');
        this._cancel.child = new St.Icon({icon_name: 'window-close-symbolic', icon_size: 16});
        this._cancel.accessible_name = tr('Anuluj');
        this._cancel.y_align = Clutter.ActorAlign.CENTER;
        header.add_child(this._cancel);
        this.actor.add_child(header);
    }

    _buildModes() {
        this._modes = new St.BoxLayout({style_class: 'gd-modes', x_expand: true});
        this._modes.layout_manager.homogeneous = true;
        this._modeButtons = MODES.map((name, i) => {
            const button = this._button(name, 'gd-mode', `mode:${i}`);
            button.x_expand = true;
            this._modes.add_child(button);
            return button;
        });
        this.actor.add_child(this._modes);
    }

    _scroll(styleClass, child) {
        return new St.ScrollView({
            style_class: styleClass,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
            reactive: true,
            x_expand: true,
            child,
        });
    }

    _buildContent() {
        // translate: the original, small and dimmed (max 3 lines), then a separator line
        this._original = wrapLabel('gd-original');
        this._original.visible = false;
        this.actor.add_child(this._original);
        this._separator = new St.Widget({style_class: 'gd-separator', x_expand: true, visible: false});
        this.actor.add_child(this._separator);

        // a failed correction (branding/PANEL.md, Errors): what happened in the error colour, then what to do
        this._problem = new St.BoxLayout({style_class: 'gd-problem', orientation: Clutter.Orientation.VERTICAL,
            x_expand: true, visible: false});
        this._problemTitle = wrapLabel('gd-problem-title');
        this._problemText = wrapLabel('gd-problem-text');
        this._problem.add_child(this._problemTitle);
        this._problem.add_child(this._problemText);
        this.actor.add_child(this._problem);

        // over an editable result: it is a field, typing or a click edits it (no Edytuj)
        this._fieldLabel = new St.BoxLayout({style_class: 'gd-field-label', x_expand: true, visible: false});
        this._fieldLabel.add_child(new St.Icon({icon_name: 'document-edit-symbolic', icon_size: 12,
            y_align: Clutter.ActorAlign.CENTER}));
        this._fieldText = new St.Label({y_align: Clutter.ActorAlign.CENTER});
        this._fieldLabel.add_child(this._fieldText);
        const fieldBox = new St.BoxLayout({style_class: 'gd-field-box', orientation: Clutter.Orientation.VERTICAL,
            x_expand: true});
        fieldBox.add_child(this._fieldLabel);
        this.actor.add_child(fieldBox);

        // the result (the editable field), translation, streamed text or error; scrolls above 220 px
        this._text = wrapLabel('gd-text');
        const textBox = new St.BoxLayout({style_class: 'gd-text-box', orientation: Clutter.Orientation.VERTICAL,
            x_expand: true});
        // the result being edited: the caret is drawn over the text (the panel never has the keyboard)
        const stack = new St.Widget({layout_manager: new Clutter.BinLayout(), x_expand: true});
        this._caret = new St.Widget({style_class: 'gd-caret', opacity: 0, x_expand: false, y_expand: false,
            x_align: Clutter.ActorAlign.START, y_align: Clutter.ActorAlign.START, width: 2, height: 1});
        stack.add_child(this._text);
        stack.add_child(this._caret);
        textBox.add_child(stack);
        this._caretIdle = 0;
        this._text.clutter_text.connect('notify::allocation', () => this._queueCaret());
        // A view drawn while the panel was hidden (a new session after Hide) is placed once the text is on screen: its
        // allocation may not change at all when the text takes the same space as the one before.
        this._text.connect('notify::mapped', () => this._queueCaret());
        this._text.reactive = true; // a click in the result edits it there (never key focus: only the pointer)
        this._text.connect('button-press-event', (_actor, event) => this._onTextClick(event));
        this._textScroll = this._scroll('gd-text-scroll', textBox);
        fieldBox.add_child(this._textScroll);

        // while editing: what the check found under the caret (suggestions, Ignoruj, Dodaj do słownika), or how much
        // it found; two lines high whatever it shows, so the panel does not move with the caret (macOS issueBar)
        this._checkBar = new St.BoxLayout({style_class: 'gd-check-bar', orientation: Clutter.Orientation.VERTICAL,
            x_expand: true, visible: false});
        this._checkKey = null;
        this.actor.add_child(this._checkBar);

        // change list: a subtle box, scrolls above 130 px, up to 30 rows
        this._changes = new St.BoxLayout({style_class: 'gd-changes-list', orientation: Clutter.Orientation.VERTICAL,
            x_expand: true});
        this._changesScroll = this._scroll('gd-changes-scroll', this._changes);
        this._changesBox = new St.Bin({style_class: 'gd-changes', x_expand: true, child: this._changesScroll,
            visible: false});
        this.actor.add_child(this._changesBox);
    }

    _buildFooter() {
        const footer = new St.BoxLayout({style_class: 'gd-footer', x_expand: true});
        this._note = new St.Label({style_class: 'gd-note', x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._note.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        footer.add_child(this._note);
        // the buttons of a failed correction, instead of Kopiuj and Zastąp
        this._problemActions = new St.BoxLayout({style_class: 'gd-problem-actions', visible: false});
        this._problemKey = null;
        footer.add_child(this._problemActions);
        // the change list on demand (Ctrl+I): quiet, as it only shows more
        this._changesToggle = this._button('', 'gd-changes-toggle', 'changes');
        this._changesToggle.visible = false;
        this._copy = this._button('Kopiuj', 'gd-button', 'copy');
        this._apply = this._button('Zastąp', 'gd-button gd-primary', 'accept');
        for (const button of [this._changesToggle, this._copy, this._apply]) {
            button.y_align = Clutter.ActorAlign.CENTER;
            footer.add_child(button);
        }
        this.actor.add_child(footer);
    }

    get variant() {
        return this._variant;
    }

    setVariant(variant) {
        if (this._gone || variant === this._variant)
            return;
        this.actor.remove_style_class_name(`gd-${this._variant}`);
        this._variant = variant;
        this.actor.add_style_class_name(`gd-${variant}`);
        this._logo.gicon = this._logos[variant][this._logoKind];
        if (this._view)
            this.render(this._view, true); // the markup colours depend on the variant
    }

    /** Update the panel in place (streaming updates arrive up to 10 times a second). */
    render(view, force = false) {
        if (this._gone)
            return;
        const previous = this._view;
        const sameSession = previous && previous.session === view.session;
        this._view = view;
        this._cancel.accessible_name = tr('Anuluj', view.ui_language);
        this._copy.label = tr(this._copiedId ? 'Skopiowano' : 'Kopiuj', view.ui_language);
        this._apply.label = tr('Zastąp', view.ui_language);
        const ink = INK[this._variant];
        const {status} = view;
        const translate = view.mode === TRANSLATE;
        const done = DONE.has(status);
        const busy = !done && status !== 'error';

        // header: the logo is filled once the result is there, an outline while working
        const logoKind = done ? 'filled' : 'outline';
        if (logoKind !== this._logoKind) {
            this._logo.gicon = this._logos[this._variant][logoKind];
            this._logoKind = logoKind;
        }
        const direction = translate && view.target
            ? (view.source_language ? `${view.source_language.toUpperCase()} → ${view.target.toUpperCase()} ⇄`
                : DIRECTIONS[view.target]) : undefined;
        this._direction.visible = !!direction;
        if (direction && this._direction.label !== direction)
            this._direction.label = direction;
        const editing = view.editing;
        // modes and the direction would compute the result again and drop the edits
        this._direction.reactive = status !== 'applying' && !editing;
        this._renderBadge(view, translate);

        this._modeButtons.forEach((button, i) => {
            button.label = tr(MODES[i], view.ui_language);
            button.checked = i === view.mode;
            button.reactive = status !== 'applying' && !editing;
        });

        // content
        const problem = status === 'error' && !editing ? view.problem : null;
        this._problem.visible = !!problem;
        this._textScroll.visible = !problem;
        if (problem) {
            if (this._problemTitle.text !== problem.title)
                this._problemTitle.text = problem.title;
            this._problemText.visible = !!problem.text;
            if (this._problemText.text !== problem.text)
                this._problemText.text = problem.text;
        }
        let original = null, markup, dim = false, changes = [];
        if (editing) {
            original = translate ? view.source : null;
            markup = editMarkup(view.text, editing.cursor, editing.anchor, ink, editing.issues ?? [], editing.marks);
        } else if (problem) {
            markup = '';
        } else if (status === 'error') {
            markup = esc(view.error || tr('Coś poszło nie tak. Spróbuj ponownie.', view.ui_language));
            dim = true;
        } else if (translate && view.text && status !== 'checking') {
            original = view.source;
            markup = esc(view.text);
            dim = busy;
        } else if (done && !translate && view.segments.length) {
            markup = resultMarkup(view.segments, ink);
        } else {
            // streamed so far, the text being read, or a result too large for a diff
            markup = esc(view.text || view.source);
            dim = busy;
        }
        if (done && !translate && !editing)
            changes = view.changes;
        this._original.visible = this._separator.visible = original !== null;
        if (original !== null && (force || !sameSession || original !== this._originalSource)) {
            this._originalSource = original;
            this._original.text = clampLines(this._original, original, ORIGINAL_LINES, CONTENT_WIDTH);
        }
        if (force || markup !== this._markup) {
            this._markup = markup;
            this._text.clutter_text.set_markup(markup);
        }
        this._placeCaret();
        if (editing)
            this._clipboard(editing);
        if (dim)
            this._text.add_style_class_name('gd-dim');
        else
            this._text.remove_style_class_name('gd-dim');
        const undone = sameSession && previous.status === 'ready' && status === 'ready';  // Cofnij in this result
        this._renderChanges(changes, ink, force, status === 'ready' ? view.ui_language : null, undone);
        this._changesBox.visible = changes.length > 0 && view.changesOpen;
        // an editable result is a field: framed, with what it is above it, and lavender while it is being edited
        const field = view.canApply && (view.editable || !!editing);
        this._fieldLabel.visible = field;
        this._fieldText.text = tr('Wynik — możesz go poprawić, zanim wstawisz', view.ui_language);
        for (const [name, on] of [['gd-field', field], ['gd-field-active', !!editing]]) {
            if (on)
                this._textScroll.add_style_class_name(name);
            else
                this._textScroll.remove_style_class_name(name);
        }
        this._renderCheckBar(editing, view.ui_language);
        if (!sameSession) {
            this._textScroll.vadjustment.value = 0;
            this._resetCopied();
        }

        // footer: engine note + scope word; Zastąp only for a finished result with changes
        const note = editing ? tr('⇧↩ nowa linia · Esc kończy edycję', view.ui_language)  // ↩ is Zastąp, next to it
            : [view.note, tr(SCOPE_WORDS[view.scope] ?? '', view.ui_language),
                problem ? tr('tekst bez zmian', view.ui_language) : ''].filter(Boolean).join(' · ');
        if (this._note.text !== note)
            this._note.text = note;
        const ready = status === 'ready' || !!editing;
        this._changesToggle.visible = changes.length > 0;
        this._changesToggle.reactive = status === 'ready';
        this._changesToggle.label = view.changesOpen ? tr('Ukryj zmiany ▴', view.ui_language)
            : tr('Pokaż {count} ▾', view.ui_language, {count: `${view.count} ${pluralChanges(view.count, view.ui_language)}`});
        this._renderProblemActions(problem, view.ui_language);
        this._copy.visible = !problem;
        this._apply.visible = view.canApply && !problem;
        this._apply.reactive = ready;
        this._copy.reactive = ready || (!view.canApply && status === 'unchanged');
        if (view.canApply)
            this._copy.remove_style_class_name('gd-primary');
        else
            this._copy.add_style_class_name('gd-primary');
    }

    /**
     * The caret of the result being edited, over the text where ClutterText lays that character out, kept in
     * view; and the visual lines' start offsets for the engine's Up, Down, Home and End (Edit 'lines').
     */
    _placeCaret() {
        const editing = this._view?.editing;
        const layout = editing && this._text.mapped ? this._wrappedLayout() : null;
        if (!layout) {
            this._caret.opacity = 0; // hidden but laid out: its place in the stack stays known
            if (!editing)
                this._linesKey = null;
            return;
        }
        const text = this._view.text;
        const [strong] = layout.get_cursor_pos(byteIndex(text, editing.cursor));
        const box = this._text.clutter_text.get_allocation_box();
        const label = this._text.get_allocation_box(), base = this._caret.get_allocation_box();
        const left = label.x1 + box.x1 + strong.x / Pango.SCALE;
        const top = label.y1 + box.y1 + strong.y / Pango.SCALE;
        const height = Math.max(1, Math.round(strong.height / Pango.SCALE));
        this._caret.set_size(2, height);
        this._caret.set_translation(Math.round(left - base.x1), Math.round(top - base.y1), 0);
        this._caret.opacity = 255;
        const adjustment = this._textScroll.vadjustment;
        if (top < adjustment.value)
            adjustment.value = top;
        else if (top + height > adjustment.value + adjustment.page_size)
            adjustment.value = top + height - adjustment.page_size;

        const starts = lineStarts(text, layout.get_lines_readonly().map(line => line.start_index));
        const key = `${this._view.session}\n${text}\n${starts.join(',')}`;
        if (key !== this._linesKey && starts.length && starts[0] === 0) {
            this._linesKey = key;
            this._onEdit('lines', JSON.stringify(starts));
        }
    }

    /**
     * The buttons of a failed correction: the others outlined, the first one (Enter's) lavender and last, with its key
     * (branding/PANEL.md, Errors). Rebuilt only when they change.
     */
    _renderProblemActions(problem, language) {
        const actions = problem?.actions ?? [];
        const key = JSON.stringify([language, actions]);
        if (key === this._problemKey)
            return;
        this._problemKey = key;
        for (const name of Object.keys(this._buttons).filter(n => n.startsWith('problem:')))
            delete this._buttons[name];
        this._problemActions.destroy_all_children();
        this._problemActions.visible = actions.length > 0;
        for (const action of [...actions.slice(1), ...actions.slice(0, 1)]) {
            const primary = action === actions[0];
            const button = this._button(null, primary ? 'gd-button gd-primary' : 'gd-button', `problem:${action.id}`);
            button.y_align = Clutter.ActorAlign.CENTER;
            button.accessible_name = action.label;
            const box = new St.BoxLayout({style_class: 'gd-button-box'});
            const labels = [new St.Label({text: action.label, y_align: Clutter.ActorAlign.CENTER})];
            if (primary)
                labels.push(new St.Label({text: 'Enter', style_class: 'gd-keycap', y_align: Clutter.ActorAlign.CENTER}));
            for (const label of labels) {
                label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE; // the note gives way, never a button
                box.add_child(label);
            }
            button.child = box;
            this._problemActions.add_child(button);
        }
    }

    _renderCheckBar(editing, language) {
        const issues = editing?.issues ?? [];
        const issue = editing && issues.find(i => i.start <= editing.cursor && editing.cursor <= i.end);
        const key = editing ? JSON.stringify([language, issue ?? null, issues.length, editing.checking]) : null;
        if (key === this._checkKey)
            return;
        this._checkKey = key;
        for (const name of Object.keys(this._buttons).filter(n => /^edit_(fix|ignore|learn|rule_off):/.test(n)))
            delete this._buttons[name];
        this._checkBar.destroy_all_children();
        this._checkBar.visible = !!editing;
        if (!editing)
            return;
        if (!issue) {
            const text = issues.length ? tr('Do sprawdzenia: {count} — kliknij podkreślone słowo, żeby zobaczyć poprawki.',
                language, {count: issues.length}) : tr(editing.checking ? 'Sprawdzam…' : 'Bez uwag.', language);
            const label = new St.Label({text, style_class: issues.length ? 'gd-check-count gd-check-found' : 'gd-check-count'});
            label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
            this._checkBar.add_child(label);
            return;
        }
        const line = new St.BoxLayout({style_class: 'gd-live-line'});
        const word = new St.Label({style_class: 'gd-live-word', y_align: Clutter.ActorAlign.CENTER});
        word.clutter_text.set_markup(`<span underline="error" underline_color="${INK[this._variant].brick}">${esc(issue.word)}</span>`);
        line.add_child(word);
        if (issue.suggestions.length) {
            line.add_child(new St.Label({text: '→', style_class: 'gd-live-arrow', y_align: Clutter.ActorAlign.CENTER}));
            issue.suggestions.forEach((suggestion, i) => {
                const button = this._button(suggestion, i === 0 ? 'gd-live-fix gd-live-first' : 'gd-live-fix',
                    `edit_fix:${issue.id}:${i}`);
                button.y_align = Clutter.ActorAlign.CENTER;
                line.add_child(button);
            });
        }
        this._checkBar.add_child(line);
        const actions = new St.BoxLayout({style_class: 'gd-mark-actions'});
        const message = new St.Label({text: issue.message, style_class: 'gd-mark-message', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        message.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        actions.add_child(message);
        actions.add_child(this._button(tr('Ignoruj', language), 'gd-mark-action', `edit_ignore:${issue.id}`));
        if (issue.learn)
            actions.add_child(this._button(tr('Dodaj do słownika', language), 'gd-mark-action', `edit_learn:${issue.id}`));
        if (issue.ruleOff)
            actions.add_child(this._button(tr('Wyłącz regułę', language), 'gd-mark-action', `edit_rule_off:${issue.id}`));
        this._checkBar.add_child(actions);
    }

    /** ClutterText lays new text out only at its next allocation: a copy of its layout wrapped at the text's width. */
    _wrappedLayout() {
        const ct = this._text.clutter_text;
        const width = ct.get_allocation_box().get_width();
        if (width <= 0)
            return null;
        const layout = ct.get_layout().copy();
        layout.set_width(Math.round(width * Pango.SCALE));
        layout.set_wrap(Pango.WrapMode.WORD_CHAR);
        return layout;
    }

    /**
     * A click in the result: editing starts with the caret there (macOS); while editing, the caret moves there. The
     * label shows the result itself (resultMarkup), so the place clicked is the place in the result: edit_at:<offset>.
     */
    _onTextClick(event) {
        const view = this._view;
        if (!view || (!view.editing && !view.editable) || event.get_button() !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;
        const [stageX, stageY] = event.get_coords();
        const [ok, x, y] = this._text.clutter_text.transform_stage_point(stageX, stageY);
        const layout = ok ? this._wrappedLayout() : null;
        if (!layout)
            return Clutter.EVENT_PROPAGATE;
        const [, index, trailing] = layout.xy_to_index(Math.round(x * Pango.SCALE), Math.round(y * Pango.SCALE));
        this._onAction(`edit_at:${codeOffset(layout.get_text(), index) + trailing}`);
        return Clutter.EVENT_STOP;
    }

    /** Test introspection: a screen point in the leading quarter of the shown character at offset (code points). */
    textPoint(offset) {
        const layout = this._wrappedLayout();
        if (!layout || !this._text.mapped)
            return null;
        const rect = layout.index_to_pos(byteIndex(layout.get_text(), offset));
        const [x, y] = this._text.clutter_text.get_transformed_position();
        return [Math.round(x + (rect.x + rect.width / 4) / Pango.SCALE), Math.round(y + (rect.y + rect.height / 2) / Pango.SCALE)];
    }

    /** Ctrl+C or Ctrl+X in the editor: the selection to the clipboard; Ctrl+V: the clipboard's text to the engine. */
    _clipboard(editing) {
        const clipboard = St.Clipboard.get_default();
        if (editing.clipboard && editing.clipboard.id > this._clipId) {
            this._clipId = editing.clipboard.id;
            clipboard.set_text(St.ClipboardType.CLIPBOARD, editing.clipboard.text);
        }
        if (editing.paste > this._pasteId) {
            const id = this._pasteId = editing.paste;
            clipboard.get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => {
                if (!this._gone && this._view?.editing)
                    this._onEdit('paste', JSON.stringify({id, text: text ?? ''}));
            });
        }
    }

    /** After a new allocation (the panel laid out again) or once the text is on screen, outside the allocation itself. */
    _queueCaret() {
        if (this._caretIdle || !this._view?.editing)
            return;
        this._caretIdle = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._caretIdle = 0;
            if (!this._gone)
                this._placeCaret();
            return GLib.SOURCE_REMOVE;
        });
    }

    _renderBadge(view, translate) {
        let icon = null, spin = false, text = '', kind = 'dim';
        switch (view.status) {
        case 'checking':
        case 'working':
            if (view.retrying) { // the quiet second try after a slip of the model (branding/PANEL.md, Errors)
                spin = true;
                text = tr('Ponawiam…', view.ui_language);
            } else if (view.status === 'checking') {
                icon = 'edit-find-symbolic';
                text = tr('Czytam scenariusz…', view.ui_language);
            } else {
                spin = true;
                text = tr(translate ? 'Tłumaczę…' : 'Poprawiam…', view.ui_language);
            }
            break;
        case 'apply_pending':
            spin = true;
            text = tr('Zastosuję po zakończeniu', view.ui_language);
            break;
        case 'applying':
            spin = true;
            text = tr('Zastępuję…', view.ui_language);
            break;
        case 'ready':
            if (view.editing) {
                icon = 'document-edit-symbolic';
                text = tr('Edycja', view.ui_language);
            } else if (!translate && view.count > 0) {
                text = `${view.count} ${pluralChanges(view.count, view.ui_language)}`;
                kind = 'count';
            }
            break;
        case 'unchanged':
            if (view.editing) {
                icon = 'document-edit-symbolic';
                text = tr('Edycja', view.ui_language);
                break;
            }
            text = tr('Zero dramatu', view.ui_language);
            kind = 'ok';
            break;
        case 'error':
            icon = 'dialog-warning-symbolic';
            kind = 'error';
            break;
        }
        if (kind !== this._badgeKind) {
            if (this._badgeKind)
                this._badge.remove_style_class_name(`gd-badge-${this._badgeKind}`);
            this._badge.add_style_class_name(`gd-badge-${kind}`);
            this._badgeKind = kind;
        }
        this._badgeIcon.visible = !!icon;
        if (icon && this._badgeIcon.icon_name !== icon) {
            this._badgeIcon.icon_name = icon;
            this._badgeIcon.icon_size = kind === 'error' ? 16 : 14;
        }
        if (spin && !this._spinning) {
            this._spinner.play();
            this._spinning = true;
        } else if (!spin && this._spinning) {
            this._spinner.stop();
            this._spinning = false;
        }
        this._badgeLabel.visible = !!text;
        if (this._badgeLabel.text !== text)
            this._badgeLabel.text = text;
        this._badge.visible = !!(icon || spin || text);
    }

    /**
     * The change list. language: the UI language while the result is ready, then a change with an id has
     * Cofnij (revert:<id>, only that change goes back to the original) and, for one word changed to another,
     * Zostaw i dodaj do słownika (keep:<id>); null while anything else is on screen.
     */
    _renderChanges(changes, ink, force, language = null, keepScroll = false) {
        const key = `${this._variant}\n${language}\n${JSON.stringify(changes)}`;
        if (!force && key === this._changesKey)
            return;
        const scrolled = this._changesScroll.vadjustment.value;
        this._changesKey = key;
        for (const name of Object.keys(this._buttons).filter(n => n.startsWith('revert:') || n.startsWith('keep:')))
            delete this._buttons[name];
        this._changes.destroy_all_children();
        for (const change of changes.slice(0, MAX_CHANGES)) {
            const row = new St.BoxLayout({style_class: 'gd-change-row', x_expand: true});
            row.label = wrapLabel('gd-change');
            row.label.clutter_text.set_markup(changeMarkup(change, ink));
            row.add_child(row.label);
            if (language !== null && change.id !== null) {
                const word = {word: change.from};
                if (change.keep)
                    row.add_child(this._changeButton(`keep:${change.id}`, 'accessories-dictionary-symbolic',
                        tr('Zostaw „{word}” i dodaj do słownika', language, word)));
                row.add_child(this._changeButton(`revert:${change.id}`, 'edit-undo-symbolic', change.from
                    ? tr('Cofnij: zostaw „{word}”', language, word) : tr('Cofnij tę zmianę', language)));
            }
            this._changes.add_child(row);
        }
        // a change undone keeps the list where it was; a new result starts at the top
        this._changesScroll.vadjustment.value = keepScroll ? scrolled : 0;
    }

    _changeButton(action, icon, name) {
        const button = this._button(null, 'gd-change-button', action);
        button.child = new St.Icon({icon_name: icon, icon_size: 14});
        button.accessible_name = name;
        button.y_align = Clutter.ActorAlign.START;
        return button;
    }

    markCopied() {
        if (this._gone)
            return;
        this._resetCopied();
        this._copy.label = tr('Skopiowano', this._view?.ui_language);
        this._copiedId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, COPIED_MS, () => {
            this._copiedId = 0;
            this._copy.label = tr('Kopiuj', this._view?.ui_language);
            return GLib.SOURCE_REMOVE;
        });
    }

    _resetCopied() {
        if (this._copiedId) {
            GLib.source_remove(this._copiedId);
            this._copiedId = 0;
        }
        if (this._copy.label !== tr('Kopiuj', this._view?.ui_language))
            this._copy.label = tr('Kopiuj', this._view?.ui_language);
    }

    hide() {
        if (this._gone)
            return;
        if (this._spinning) {
            this._spinner.stop();
            this._spinning = false;
        }
        this._resetCopied();
        this.actor.hide();
        this._view = null; // the next Show renders from scratch (scroll to top, fresh copy label)
    }

    destroy() {
        if (this._caretIdle) {
            GLib.source_remove(this._caretIdle);
            this._caretIdle = 0;
        }
        if (!this._gone) {
            this.hide();
            settleAndDestroy(this.actor);
        }
        this.actor = null;
    }

    /** Test introspection (GRAMADRAMA_ENGINE_TEST=1 only): geometry and what is on screen. */
    describe() {
        const button = b => ({rect: rectOf(b), visible: b.visible && b.mapped, reactive: b.reactive,
            label: b.label, checked: b.checked, styles: b.get_style_class_name(),
            pseudo: b.get_style_pseudo_class() ?? ''});
        const buttons = {};
        for (const [name, b] of Object.entries(this._buttons))
            buttons[name] = button(b);
        return {
            visible: this.actor.visible,
            mapped: this.actor.mapped,
            rect: rectOf(this.actor),
            variant: this._variant,
            styles: this.actor.get_style_class_name(),
            hasKeyFocus: this.actor.has_key_focus() || Object.values(this._buttons).some(b => b.has_key_focus()),
            session: this._view?.session ?? null,
            status: this._view?.status ?? null,
            logo: this._logoKind,
            badge: {visible: this._badge.visible && this._badge.mapped, kind: this._badgeKind,
                text: this._badgeLabel.visible ? this._badgeLabel.text : '',
                icon: this._badgeIcon.visible ? this._badgeIcon.icon_name : null,
                spinner: !!this._spinning, rect: rectOf(this._badge)},
            text: this._text.clutter_text.get_text(),
            markup: this._markup ?? '',
            plain: !!this._view?.plain,
            dim: this._text.has_style_class_name('gd-dim'),
            original: this._original.visible ? this._original.text : null,
            originalLines: this._original.mapped ? this._original.clutter_text.get_layout().get_line_count() : 0,
            separator: this._separator.visible,
            changes: this._changes.get_children().map(row => row.label.clutter_text.get_text()),
            changesShown: this._changesBox.visible && this._changesBox.mapped,
            field: this._textScroll.has_style_class_name('gd-field'),
            fieldActive: this._textScroll.has_style_class_name('gd-field-active'),
            fieldLabel: this._fieldLabel.visible ? this._fieldText.text : null,
            changesRect: rectOf(this._changesBox),
            changesScrollHeight: this._changesScroll.mapped ? Math.round(this._changesScroll.height) : 0,
            textRect: rectOf(this._textScroll),
            textScrollHeight: this._textScroll.mapped ? Math.round(this._textScroll.height) : 0,
            textHeight: Math.round(this._text.get_preferred_height(CONTENT_WIDTH)[1]),
            note: this._note.text,
            problem: this._problem.visible ? {title: this._problemTitle.text,
                text: this._problemText.visible ? this._problemText.text : '', rect: rectOf(this._problem),
                actions: this._problemActions.get_children().map(b => ({
                    label: b.child.get_children().map(c => c.text).join(' '),
                    primary: b.has_style_class_name('gd-primary')}))} : null,
            editing: this._view?.editing ?? null,
            checkBar: this._checkBar.visible ? this._checkBar.get_children().map(row => row instanceof St.Label
                ? row.text : row.get_children().map(c => c.label ?? c.text ?? '').join(' ')) : null,
            caret: {visible: this._caret.opacity > 0 && this._caret.mapped, rect: rectOf(this._caret)},
            buttons,
        };
    }
}

export class Toast {
    constructor({logos}) {
        this._logos = logos;
        this._variant = 'dark';
        this._gone = false;
        this.actor = new St.BoxLayout({
            name: 'gramadrama-toast',
            style_class: 'gd-toast gd-dark',
            reactive: false, // click-through: a toast never intercepts the pointer or keys
            can_focus: false,
            visible: false,
        });
        this._logo = new St.Icon({gicon: logos.dark.filled, icon_size: 20, style_class: 'gd-logo',
            y_align: Clutter.ActorAlign.CENTER});
        this.actor.add_child(this._logo);
        this._label = wrapLabel('gd-toast-text');
        this._label.y_align = Clutter.ActorAlign.CENTER;
        this.actor.add_child(this._label);
        this.actor.connect('destroy', () => (this._gone = true));
    }

    setVariant(variant) {
        if (this._gone || variant === this._variant)
            return;
        this.actor.remove_style_class_name(`gd-${this._variant}`);
        this._variant = variant;
        this.actor.add_style_class_name(`gd-${variant}`);
        this._logo.gicon = this._logos[variant].filled;
    }

    render(view) {
        if (this._gone)
            return;
        if (this._label.text !== view.message)
            this._label.text = view.message;
    }

    hide() {
        if (!this._gone)
            this.actor.hide();
    }

    destroy() {
        if (!this._gone) {
            this.hide();
            settleAndDestroy(this.actor);
        }
        this.actor = null;
    }

    describe() {
        return {visible: this.actor.visible, mapped: this.actor.mapped, rect: rectOf(this.actor),
            text: this._label.text, variant: this._variant, reactive: this.actor.reactive};
    }
}
