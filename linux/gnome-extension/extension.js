// GramaDrama: the correction panel next to the text caret, drawn by GNOME Shell 50.
//
// Contract with the GramaDrama IBus engine: linux/docs/architecture.md (D-Bus interface
// pl.gramadrama.Panel1 at /pl/gramadrama/Panel on gnome-shell's own session-bus connection, the
// view JSON, placement and focus rules). Design: branding/PANEL.md.
//
// Only GNOME Shell can draw a window-less panel that never takes keyboard focus: the app keeps
// focus, its IBus input context stays active, and the engine keeps handling Enter/Esc/Ctrl+1-6
// while the panel is on screen. Clicks on the panel's buttons are sent back as the Action signal.
// No keybindings, no key injection, no clipboard access except the Kopiuj button.
//
// Only GNOME Shell knows which app has focus (every Wayland app reaches IBus as `gnome-shell`), and
// the engine decides about Ctrl+Alt+G synchronously, so the focused app is pushed to it: the Focus
// signal goes to the single subscriber only (unicast), never to the rest of the bus (docs/apps.md).
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Mtk from 'gi://Mtk';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {getIBusManager} from 'resource:///org/gnome/shell/misc/ibusManager.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {Hud, parseHud} from './hud.js';
import {LiveBadge, parseLive} from './live.js';
import {Underlines, intersect, parseMarks} from './underlines.js';
import {Panel, Toast, PANEL_WIDTH, PADDING, TRANSLATE, parseView} from './panel.js';
import {focusPair, isEngineProcess, isTestMode, launcherPath, parentPid, parseCmdline} from './policy.js';

const OBJECT_PATH = '/pl/gramadrama/Panel';
const IFACE = 'pl.gramadrama.Panel1';
const ERROR_DENIED = `${IFACE}.Error.AccessDenied`;
const ERROR_ARGS = `${IFACE}.Error.InvalidArgs`;
// What GDBus answers once the object is no longer exported: the engine then asks again with Ping.
const ERROR_GONE = 'org.freedesktop.DBus.Error.UnknownObject';
const ERROR_FAILED = 'org.freedesktop.DBus.Error.Failed';
const IFACE_XML = `<node>
  <interface name="${IFACE}">
    <method name="Ping">
      <arg type="s" name="version" direction="out"/>
    </method>
    <method name="Show">
      <arg type="s" name="view" direction="in"/>
      <arg type="b" name="shown" direction="out"/>
    </method>
    <method name="Hide">
      <arg type="u" name="session" direction="in"/>
    </method>
    <method name="Subscribe"/>
    <method name="Hud">
      <arg type="s" name="view" direction="in"/>
      <arg type="b" name="shown" direction="out"/>
    </method>
    <method name="Live">
      <arg type="s" name="view" direction="in"/>
      <arg type="b" name="shown" direction="out"/>
    </method>
    <method name="Marks">
      <arg type="s" name="view" direction="in"/>
      <arg type="b" name="shown" direction="out"/>
    </method>
    <signal name="Action">
      <arg type="u" name="session"/>
      <arg type="s" name="action"/>
    </signal>
    <signal name="Focus">
      <arg type="s" name="app"/>
      <arg type="s" name="kind"/>
    </signal>
    <signal name="Edit">
      <arg type="u" name="session"/>
      <arg type="s" name="kind"/>
      <arg type="s" name="data"/>
    </signal>
  </interface>
</node>`;
const NO_FOCUS = ['', '']; // Focus sent when the extension stops: nothing follows until the next Subscribe
// The isolated test harness only (policy.js): any caller may drive the panel, and the panel's
// state is exposed as globalThis.gdGramaDramaPanel for introspection.
const TEST_MODE = isTestMode(name => GLib.getenv(name));
const GAP = 6; // between the caret and the panel
const EDGE = 8; // minimum distance to the work area's edge
const CHANGES_RESERVE = 160; // room kept below the caret for the change list that comes with the result
const MAX_CALLERS = 16;

function dbusCall(connection, dest, path, iface, method, params, replyType, cancellable) {
    return new Promise((resolve, reject) => {
        connection.call(dest, path, iface, method, params, new GLib.VariantType(replyType),
            Gio.DBusCallFlags.NONE, 2000, cancellable, (conn, result) => {
                try {
                    resolve(conn.call_finish(result).deepUnpack());
                } catch (e) {
                    reject(e);
                }
            });
    });
}

function readFile(path, cancellable) {
    return new Promise((resolve, reject) => {
        Gio.File.new_for_path(path).load_contents_async(cancellable, (file, result) => {
            try {
                resolve(file.load_contents_finish(result)[1]);
            } catch (e) {
                reject(e);
            }
        });
    });
}

function clamp(value, low, high) {
    return Math.max(low, Math.min(value, high));
}

/** The object exported with wrapJSObject: only these four methods exist on the bus. */
class PanelService {
    constructor(owner) {
        this._owner = owner;
    }

    Ping() {
        return this._owner.version;
    }

    ShowAsync(params, invocation) {
        return this._owner.handleShow(params, invocation);
    }

    HideAsync(params, invocation) {
        return this._owner.handleHide(params, invocation);
    }

    SubscribeAsync(params, invocation) {
        return this._owner.handleSubscribe(params, invocation);
    }

    HudAsync(params, invocation) {
        return this._owner.handleHud(params, invocation);
    }

    LiveAsync(params, invocation) {
        return this._owner.handleLive(params, invocation);
    }

    MarksAsync(params, invocation) {
        return this._owner.handleMarks(params, invocation);
    }
}

export default class GramaDramaExtension extends Extension {
    get version() {
        return String(this.metadata.version ?? '');
    }

    enable() {
        try {
            this._enable();
        } catch (e) {
            this.disable(); // never leave half an extension behind (actors, handlers, D-Bus)
            throw e;
        }
    }

    _enable() {
        this._alive = false; // true while the actors exist; callbacks do nothing otherwise
        this._cancellable = new Gio.Cancellable();
        this._callers = new Map(); // unique bus name -> Promise<boolean> (caller check)
        this._current = null; // {session, sender, kind} of the last Show that reached the screen
        this._view = null; // the session view on screen (Kopiuj copies its text)
        this._placed = null;
        this._caret = null;
        this._ibusFocus = {in: 0, out: 0};
        this._actions = [];
        this._toastId = 0;
        this._watchId = 0;
        this._watched = null;
        this._overlayWatchId = 0; // the engine showing the pill or the badge
        this._overlaySender = null;
        this._ps = null;
        this._psIds = [];
        this._ibusCaretId = 0;
        this._lastShowUs = 0;
        this._focusIds = []; // [object, handler id] of the focus sources
        this._focus = null; // [app, kind] of the focus now; the subscriber has been sent every change
        this._subscriber = null; // unique bus name of the engine that receives Focus
        this._subscriberWatchId = 0;
        this._focusSent = []; // test mode: the last Focus signals and where they went

        const icon = name => new Gio.FileIcon({file: this.dir.resolve_relative_path(`icons/${name}`)});
        // The neutral two-mask logo per variant: filled once a result is there, an outline while working.
        const logos = Object.fromEntries(['dark', 'light'].map(variant =>
            [variant, {filled: icon(`mask-${variant}.svg`), outline: icon(`mask-${variant}-outline.svg`)}]));
        this._panel = new Panel({logos, onAction: action => this._onAction(action),
            onEdit: (kind, data) => this._onEdit(kind, data)});
        this._toast = new Toast({logos});
        this._hud = new Hud({logos});
        this._live = new LiveBadge({onAction: action => this._emit(0, action)});
        this._underlines = new Underlines({onAction: action => this._emit(0, action)});
        // If GNOME Shell destroys the actors without disable() (shutdown), later callbacks (the
        // engine's name vanishing, timers, IBus) must not touch them.
        for (const actor of [this._panel.actor, this._toast.actor, this._hud.actor, this._live.badge, this._live.list,
            this._underlines.card])
            actor.connect('destroy', () => this._actorsGone());
        this._alive = true;
        // Top chrome: above all windows. Never added to the input region or given key focus.
        Main.layoutManager.addTopChrome(this._panel.actor);
        Main.layoutManager.addTopChrome(this._toast.actor);
        Main.layoutManager.addTopChrome(this._hud.actor);
        Main.layoutManager.addTopChrome(this._live.badge);
        Main.layoutManager.addTopChrome(this._live.list);
        Main.layoutManager.addTopChrome(this._underlines.card);

        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._schemeId = this._interface.connect('changed::color-scheme', () => this._syncVariant());
        this._syncVariant();

        this._keyboardId = Main.keyboard.connect('visibility-changed', () => {
            if (Main.keyboard.visible) {
                this._hideAll(true); // the on-screen keyboard wins; Show returns false meanwhile
                this._live?.hide();
                this._underlines?.hide();
            }
        });
        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => {
            this._reposition();
            this._hud?.reposition();
        });

        this._ibus = getIBusManager();
        this._ibusReadyId = this._ibus.connect('ready', () => {
            this._callers?.clear(); // a new ibus-daemon: check every caller again
            this._hookPanelService();
        });
        this._hookPanelService();

        // The focused app for the engine: a window gets focus, a shell entry takes the keyboard, or
        // the window tracker re-assigns the focused window (a WM_CLASS set late).
        const tracker = Shell.WindowTracker.get_default();
        this._focusIds = [
            [global.display, global.display.connect('notify::focus-window', () => this._focusChanged())],
            [global.stage, global.stage.connect('notify::key-focus', () => this._focusChanged())],
            [tracker, tracker.connect('notify::focus-app', () => this._focusChanged())],
        ];
        this._focus = this._focusPair();

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, new PanelService(this));
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);

        if (TEST_MODE) {
            console.warn('GramaDrama: test mode (isolated test harness): any caller may drive the panel');
            globalThis.gdGramaDramaPanel = {state: () => this._testState(), textPoint: offset => this._panel.textPoint(offset)};
        }
    }

    disable() {
        // Also after a failed enable(): every step copes with what was never set up.
        if (TEST_MODE)
            delete globalThis.gdGramaDramaPanel;
        this._alive = false;
        this._stopFocus(); // before unexport: the subscriber learns that no focus follows
        this._dbus?.unexport();
        this._dbus = null;
        this._cancellable?.cancel();
        this._cancellable = null;
        this._stopToastTimer();
        this._unwatch();
        this._unwatchOverlays();
        this._unhookPanelService();
        if (this._ibusReadyId)
            this._ibus?.disconnect(this._ibusReadyId);
        this._ibusReadyId = 0;
        this._ibus = null;
        if (this._keyboardId)
            Main.keyboard.disconnect(this._keyboardId);
        this._keyboardId = 0;
        if (this._monitorsId)
            Main.layoutManager.disconnect(this._monitorsId);
        this._monitorsId = 0;
        if (this._schemeId)
            this._interface?.disconnect(this._schemeId);
        this._schemeId = 0;
        this._interface = null;
        // Destroying the actors also removes them from the layout manager's tracked chrome.
        this._panel?.destroy();
        this._toast?.destroy();
        this._hud?.destroy();
        this._live?.destroy();
        this._underlines?.destroy();
        this._panel = this._toast = this._hud = this._live = this._underlines = null;
        this._callers = null;
        this._current = this._view = this._placed = this._caret = null;
    }

    _actorsGone() {
        this._alive = false;
        this._stopToastTimer();
        this._unwatch();
        this._unwatchOverlays();
        this._stopFocus();
    }

    // ---- D-Bus -----------------------------------------------------------------------------

    async handleShow([json], invocation) {
        let shown = false;
        try {
            const allowed = await this._authorized(invocation.get_sender());
            if (!this._alive) {
                invocation.return_value(new GLib.Variant('(b)', [false])); // disabled meanwhile
                return;
            }
            if (!allowed) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may show the panel');
                return;
            }
            let view;
            try {
                view = parseView(json);
            } catch (e) {
                invocation.return_dbus_error(ERROR_ARGS, `Invalid view: ${e.message}`);
                return;
            }
            const t0 = GLib.get_monotonic_time();
            shown = this._show(view, invocation.get_sender());
            this._lastShowUs = GLib.get_monotonic_time() - t0;
        } catch (e) {
            logError(e, 'GramaDrama: Show failed');
            this._hideAll(); // never leave a half-updated panel behind a "false"
            shown = false;
        }
        invocation.return_value(new GLib.Variant('(b)', [shown]));
    }

    /** The dictation pill (hud.js), from the same engine only: it can put text on the clipboard. */
    async handleHud([json], invocation) {
        let shown = false;
        try {
            const allowed = await this._authorized(invocation.get_sender());
            if (!this._alive) {
                invocation.return_value(new GLib.Variant('(b)', [false]));
                return;
            }
            if (!allowed) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may show the dictation pill');
                return;
            }
            let view;
            try {
                view = parseHud(json);
            } catch (e) {
                invocation.return_dbus_error(ERROR_ARGS, `Invalid view: ${e.message}`);
                return;
            }
            shown = this._hud.show(view);
            if (shown)
                this._watchOverlays(invocation.get_sender());
        } catch (e) {
            logError(e, 'GramaDrama: Hud failed');
            this._hud?.hide();
            shown = false;
        }
        invocation.return_value(new GLib.Variant('(b)', [shown]));
    }

    /** The live check's badge (live.js), from the same engine only: its clicks correct text. */
    async handleLive([json], invocation) {
        let shown = false;
        try {
            const allowed = await this._authorized(invocation.get_sender());
            if (!this._alive) {
                invocation.return_value(new GLib.Variant('(b)', [false]));
                return;
            }
            if (!allowed) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may show the live badge');
                return;
            }
            let view;
            try {
                view = parseLive(json);
            } catch (e) {
                invocation.return_dbus_error(ERROR_ARGS, `Invalid view: ${e.message}`);
                return;
            }
            shown = !Main.keyboard.visible && this._live.show(view, this._livePlace(view));
            if (shown)
                this._watchOverlays(invocation.get_sender());
        } catch (e) {
            logError(e, 'GramaDrama: Live failed');
            this._live?.hide();
            shown = false;
        }
        invocation.return_value(new GLib.Variant('(b)', [shown]));
    }

    /** The live check's underlines (underlines.js), from the same engine only: its card corrects text. */
    async handleMarks([json], invocation) {
        let shown = false;
        try {
            const allowed = await this._authorized(invocation.get_sender());
            if (!this._alive) {
                invocation.return_value(new GLib.Variant('(b)', [false]));
                return;
            }
            if (!allowed) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may draw the underlines');
                return;
            }
            let view;
            try {
                view = parseMarks(json);
            } catch (e) {
                invocation.return_dbus_error(ERROR_ARGS, `Invalid view: ${e.message}`);
                return;
            }
            shown = !Main.keyboard.visible && this._underlines.show(view, global.display.focus_window, this._caretRect());
            if (shown)
                this._watchOverlays(invocation.get_sender());
        } catch (e) {
            logError(e, 'GramaDrama: Marks failed');
            this._underlines?.hide();
            shown = false;
        }
        invocation.return_value(new GLib.Variant('(b)', [shown]));
    }

    /** Where the badge stands. A browser's page tells where its field is: inside the field's bottom
     *  right corner, as on macOS (3 px from the edges of a low field, 6 px else). Elsewhere the focused
     *  window's right edge, level with the caret's line (its bottom when the caret is unknown); null
     *  without a focused window. */
    _livePlace(view = this._live?.view) {
        const window = global.display.focus_window;
        if (!window)
            return null;
        const frame = window.get_frame_rect();
        const area = Main.layoutManager.getWorkAreaForMonitor(window.get_monitor());
        if (view?.field) { // its visible box in the window's client coordinates (underlines.js origin)
            const client = window.frame_rect_to_client_rect(frame);
            const [x, y, width, height] = view.field;
            let field = intersect({x: client.x + x, y: client.y + y, width, height}, frame);
            field = field && intersect(field, area);
            if (field) {
                const inset = field.height < 34 ? 3 : 6;
                return {x: field.x + field.width - inset, bottom: field.y + field.height - inset, area};
            }
        }
        const caret = this._caretRect();
        let y = caret ? caret.y + caret.height / 2 : frame.y + frame.height - 24;
        y = clamp(y, area.y + 20, area.y + area.height - 20);
        const x = Math.min(frame.x + frame.width, area.x + area.width) - 8;
        return {x, y, area};
    }

    async handleHide([session], invocation) {
        try {
            const allowed = await this._authorized(invocation.get_sender());
            if (!allowed && this._alive) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may hide the panel');
                return;
            }
            if (this._alive && (session === 0 || this._current?.session === session))
                this._hideAll();
        } catch (e) {
            logError(e, 'GramaDrama: Hide failed');
        }
        invocation.return_value(null);
    }

    /**
     * The caller receives Focus from now on, starting right after this reply. There is one
     * subscriber: the last caller that passed the same check as Show/Hide. A reply without an error
     * always means subscribed: when the extension was disabled during the (asynchronous) caller
     * check, perhaps enabled again since, or its actors are gone, the call fails as if the object
     * were gone, so that the engine asks again once a Ping is answered.
     */
    async handleSubscribe(_params, invocation) {
        const sender = invocation.get_sender();
        const callers = this._callers; // a new Map at every enable()
        try {
            const allowed = await this._authorized(sender);
            if (!this._alive || this._callers !== callers) {
                invocation.return_dbus_error(ERROR_GONE, 'The GramaDrama panel was disabled meanwhile');
                return;
            }
            if (!allowed) {
                invocation.return_dbus_error(ERROR_DENIED,
                    'Only the GramaDrama IBus engine started by ibus-daemon may subscribe to the focus');
                return;
            }
            this._setSubscriber(sender);
        } catch (e) {
            logError(e, 'GramaDrama: Subscribe failed');
            invocation.return_dbus_error(ERROR_FAILED, `Subscribe failed: ${e.message}`);
            return;
        }
        invocation.return_value(null);
        if (this._subscriber === sender) {
            this._focus = this._focusPair();
            this._sendFocus(sender, this._focus);
        }
    }

    /** Promise<boolean>: may this bus client drive the panel? Cached per unique name. */
    _authorized(sender) {
        if (TEST_MODE)
            return Promise.resolve(true);
        if (!sender || !this._callers)
            return Promise.resolve(false);
        let check = this._callers.get(sender);
        if (!check) {
            check = this._checkCaller(sender).catch(e => {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`GramaDrama: caller check for ${sender} failed: ${e.message}`);
                this._callers?.delete(sender); // a transient failure is not remembered
                return false;
            });
            this._callers.set(sender, check);
            while (this._callers.size > MAX_CALLERS)
                this._callers.delete(this._callers.keys().next().value);
        }
        return check;
    }

    /**
     * The caller must be the installed engine as ibus-daemon starts it (policy.js): its command
     * line ends with exactly `<user data dir>/gramadrama-engine/gramadrama-engine --ibus`, and its
     * parent is this session's ibus-daemon. Everything is read asynchronously.
     */
    async _checkCaller(sender) {
        const [pid] = await dbusCall(Gio.DBus.session, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetConnectionUnixProcessID', new GLib.Variant('(s)', [sender]), '(u)',
            this._cancellable);
        const [cmdline, stat] = await Promise.all([readFile(`/proc/${pid}/cmdline`, this._cancellable),
            readFile(`/proc/${pid}/stat`, this._cancellable)]);
        return isEngineProcess(parseCmdline(cmdline), parentPid(new TextDecoder().decode(stat)),
            launcherPath(GLib.get_user_data_dir()), this._ibusDaemonPid());
    }

    /** The PID of the ibus-daemon GNOME Shell is connected to (peer credentials of its socket). */
    _ibusDaemonPid() {
        try {
            // IBusManager._ibus is private to GNOME Shell: feature-detected, and null refuses all.
            const socket = this._ibus?._ibus?.get_connection?.()?.get_stream?.()?.get_socket?.();
            const pid = socket?.get_credentials?.()?.get_unix_pid?.();
            return Number.isInteger(pid) && pid > 1 ? pid : null;
        } catch {
            return null;
        }
    }

    _onAction(action) {
        const current = this._current;
        if (!this._alive || !current || current.kind !== 'session' || !this._panel.actor.visible)
            return;
        if (action === 'copy') {
            const text = this._view?.text ?? '';
            if (text) {
                St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
                this._panel.markCopied();
            }
        }
        this._emit(current.session, action);
    }

    /** The panel's report about the result being edited ('lines': JSON of its visual lines' start offsets). */
    _onEdit(kind, data) {
        const current = this._current;
        if (!this._alive || !this._dbus || !current || current.kind !== 'session' || !this._panel.actor.visible)
            return;
        this._dbus.emit_signal('Edit', new GLib.Variant('(uss)', [current.session, kind, data]));
    }

    _emit(session, action) {
        if (!this._dbus)
            return;
        this._dbus.emit_signal('Action', new GLib.Variant('(us)', [session, action]));
        if (TEST_MODE) {
            this._actions.push({session, action});
            this._actions.splice(0, this._actions.length - 50);
        }
    }

    // ---- showing and hiding ----------------------------------------------------------------

    _show(view, sender) {
        if (Main.keyboard.visible) {
            this._hideAll();
            return false;
        }
        const current = this._current;
        const onScreen = this._panel.actor.visible || this._toast.actor.visible;
        const sameEngine = onScreen && current?.sender === sender;
        if (sameEngine && view.session < current.session)
            return false; // an older session than the one on screen
        if (view.kind === 'message' && !view.message)
            return false;
        const sameSession = sameEngine && view.session === current.session;
        const anchor = this._anchor() ?? (sameSession ? this._placed?.anchor : null);
        if (!anchor) {
            this._hideAll(); // no caret and no focused window: nowhere to show it
            return false;
        }

        let actor;
        if (view.kind === 'message') {
            // A toast (refusal, "Zastosowano") replaces the panel, also for the same session.
            this._panel.hide();
            this._view = null;
            this._toast.render(view);
            actor = this._toast.actor;
        } else {
            this._stopToastTimer();
            this._toast.hide();
            this._view = view;
            this._panel.render(view);
            actor = this._panel.actor;
        }
        const keep = sameSession && this._placed?.actor === actor ? this._placed : null;
        if (!this._place(actor, anchor, keep, view)) {
            this._hideAll(); // no monitor to show it on
            return false;
        }
        this._current = {session: view.session, sender, kind: view.kind};
        this._watch(sender);
        if (view.kind === 'message')
            this._startToastTimer(view.seconds);
        if (!actor.visible) {
            actor.show();
            // Like GNOME's own IBus candidate popup: just above the on-screen keyboard's layer, so
            // the screenshot UI and the like stay above the panel.
            Main.layoutManager.uiGroup.set_child_above_sibling(actor, Main.layoutManager.keyboardBox);
        }
        return actor.visible && actor.mapped;
    }

    /**
     * Hide the panel and the toast. `notify`: the extension hides a session panel on its own (the
     * on-screen keyboard came up, no monitor), so the engine gets Action(session, 'hidden') and
     * ends the session instead of keeping an invisible one modal.
     */
    _hideAll(notify = false) {
        if (!this._alive)
            return;
        const current = this._current;
        const hidSession = notify && current?.kind === 'session' && this._panel.actor.visible;
        this._stopToastTimer();
        this._panel.hide();
        this._toast.hide();
        this._view = null;
        this._unwatch();
        if (hidSession)
            this._emit(current.session, 'hidden');
    }

    _startToastTimer(seconds) {
        this._stopToastTimer();
        this._toastId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.round(seconds * 1000), () => {
            this._toastId = 0;
            if (this._alive) {
                this._toast.hide();
                if (!this._panel.actor.visible)
                    this._unwatch();
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _stopToastTimer() {
        if (this._toastId)
            GLib.source_remove(this._toastId);
        this._toastId = 0;
    }

    /** The panel never outlives the engine that drives it (crash, restart, logout). */
    _watch(sender) {
        if (this._watched === sender)
            return;
        this._unwatch();
        this._watched = sender;
        this._watchId = Gio.bus_watch_name_on_connection(Gio.DBus.session, sender,
            Gio.BusNameWatcherFlags.NONE, null, () => {
                if (this._alive && this._watched === sender && this._current?.sender === sender)
                    this._hideAll();
            });
    }

    _unwatch() {
        if (this._watchId)
            Gio.bus_unwatch_name(this._watchId);
        this._watchId = 0;
        this._watched = null;
    }

    /** Nor do the pill and the badge (an engine restarted by an install leaves them behind otherwise). */
    _watchOverlays(sender) {
        if (this._overlaySender === sender)
            return;
        this._unwatchOverlays();
        this._overlaySender = sender;
        this._overlayWatchId = Gio.bus_watch_name_on_connection(Gio.DBus.session, sender,
            Gio.BusNameWatcherFlags.NONE, null, () => {
                if (!this._alive || this._overlaySender !== sender)
                    return;
                this._hud.hide();
                this._live.hide();
                this._underlines.hide();
                this._unwatchOverlays();
            });
    }

    _unwatchOverlays() {
        if (this._overlayWatchId)
            Gio.bus_unwatch_name(this._overlayWatchId);
        this._overlayWatchId = 0;
        this._overlaySender = null;
    }

    _syncVariant() {
        if (!this._alive)
            return;
        const variant = this._interface.get_string('color-scheme') === 'prefer-dark' ? 'dark' : 'light';
        this._variant = variant;
        this._panel.setVariant(variant);
        this._toast.setVariant(variant);
        this._hud.setVariant(variant);
        this._live.setVariant(variant);
        this._underlines.setVariant(variant);
        if (this._panel.actor.visible || this._toast.actor.visible)
            this._reposition(); // heights can differ slightly between the variants' fonts
    }

    // ---- the focused app, pushed to the engine (docs/apps.md, section 1) ----------------------

    /** [app, kind] of what has the keyboard now (policy.js). */
    _focusPair() {
        const window = global.display.focus_window;
        const app = window ? Shell.WindowTracker.get_default().get_window_app(window) : null;
        return focusPair({
            shellEntry: !!global.stage.key_focus,
            window: !!window,
            x11: window?.get_client_type() === Meta.WindowClientType.X11,
            appId: app?.get_id() ?? null,
            windowBacked: app?.is_window_backed() ?? true,
            wmClass: window?.get_wm_class() ?? null,
        });
    }

    _focusChanged() {
        const pair = this._focusPair();
        if (this._focus && pair[0] === this._focus[0] && pair[1] === this._focus[1])
            return;
        this._focus = pair;
        if (this._subscriber)
            this._sendFocus(this._subscriber, pair);
    }

    /** Unicast: the focus is for the subscriber only (Wayland hides it from clients on purpose). */
    _sendFocus(to, pair) {
        try {
            Gio.DBus.session.emit_signal(to, OBJECT_PATH, IFACE, 'Focus', new GLib.Variant('(ss)', pair));
        } catch (e) {
            // the connection is closing (shutdown): nobody is left to tell
            console.debug(`GramaDrama: Focus to ${to} not sent: ${e.message}`);
            return;
        }
        if (TEST_MODE) {
            this._focusSent.push({app: pair[0], kind: pair[1], to});
            this._focusSent.splice(0, this._focusSent.length - 50);
        }
    }

    _setSubscriber(name) {
        if (this._subscriber === name)
            return;
        this._forgetSubscriber();
        this._subscriber = name;
        // Forget it when its connection goes away (engine exit or crash); called at once if the
        // name is already gone.
        this._subscriberWatchId = Gio.bus_watch_name_on_connection(Gio.DBus.session, name,
            Gio.BusNameWatcherFlags.NONE, null, () => {
                if (this._subscriber === name)
                    this._forgetSubscriber();
            });
    }

    _forgetSubscriber() {
        if (this._subscriberWatchId)
            Gio.bus_unwatch_name(this._subscriberWatchId);
        this._subscriberWatchId = 0;
        this._subscriber = null;
    }

    /**
     * Stop following the focus (disable(), or the shell destroyed the actors). The subscriber gets
     * Focus('', ''): no focus information until it subscribes again. The lock screen disables user
     * extensions, and a stale app would keep deciding about Ctrl+Alt+G meanwhile.
     */
    _stopFocus() {
        for (const [object, id] of this._focusIds ?? [])
            object.disconnect(id);
        this._focusIds = [];
        if (this._subscriber)
            this._sendFocus(this._subscriber, NO_FOCUS);
        this._forgetSubscriber();
    }

    // ---- caret tracking: the same IBus panel-service signals GNOME's candidate popup uses ----

    _hookPanelService() {
        // _panelService is private to GNOME Shell: feature-detect it, and fall back to the
        // manager's public (absolute-only) caret signal when it is missing.
        const ps = this._ibus?._panelService ?? null;
        if (ps && ps === this._ps)
            return;
        this._unhookPanelService();
        if (!ps) {
            this._ibusCaretId = this._ibus.connect('set-cursor-location',
                (_manager, rect) => this._onAbsoluteCaret(rect.x, rect.y, rect.width, rect.height));
            return;
        }
        this._ps = ps;
        this._psIds = [
            ps.connect('set-cursor-location', (_ps, x, y, w, h) => this._onAbsoluteCaret(x, y, w, h)),
            ps.connect('focus-in', () => this._ibusFocus.in++),
            ps.connect('focus-out', () => this._ibusFocus.out++),
            // GNOME Shell destroys the service when ibus-daemon goes away (and makes a new one,
            // announced by 'ready'); its handlers go with it, so forget them here instead of
            // disconnecting from a disposed object later.
            ps.connect('destroy', () => {
                if (this._ps === ps) {
                    this._ps = null;
                    this._psIds = [];
                }
            }),
        ];
        try {
            this._psIds.push(ps.connect('set-cursor-location-relative',
                (_ps, x, y, w, h) => this._onRelativeCaret(x, y, w, h)));
        } catch {
            // an older IBus without the relative signal: absolute locations only
        }
    }

    _unhookPanelService() {
        if (this._ps) {
            for (const id of this._psIds) {
                try {
                    this._ps.disconnect(id);
                } catch {
                    // disposed without a 'destroy' emission: nothing left to disconnect
                }
            }
        }
        this._ps = null;
        this._psIds = [];
        if (this._ibusCaretId)
            this._ibus?.disconnect(this._ibusCaretId);
        this._ibusCaretId = 0;
    }

    _onAbsoluteCaret(x, y, w, h) {
        if (!x && !y && !w && !h) {
            this._caret = null; // IBus resets the location on focus changes
            return;
        }
        const window = global.display.focus_window;
        const stageFocus = !!global.stage.key_focus; // a shell entry (run dialog, search)
        let rect = new Mtk.Rectangle({x, y, width: w, height: h});
        if (!stageFocus && window)
            rect = window.protocol_to_stage_rect(rect);
        this._caret = {x: rect.x, y: rect.y, width: rect.width, height: rect.height, source: 'absolute',
            window: stageFocus || !window ? null : String(window.get_id())};
        this._reposition();
        this._live?.move(this._livePlace());
    }

    _onRelativeCaret(x, y, w, h) {
        if (!x && !y && !w && !h) {
            this._caret = null;
            return;
        }
        // Window-relative (Wayland clients talking to IBus directly). Resolved against the focus
        // window only when used: on a window's first focus this signal can arrive while
        // focus_window is still the previous window.
        this._caret = {relative: {x, y, width: w, height: h}, source: 'relative'};
        this._reposition();
        this._live?.move(this._livePlace());
    }

    /** The caret in stage coordinates, or null when unknown or recorded for another window. */
    _caretRect() {
        const caret = this._caret;
        const window = global.display.focus_window;
        if (!caret)
            return null;
        if (caret.relative) {
            const actor = window?.get_compositor_private();
            if (!actor)
                return null;
            const {x, y, width, height} = caret.relative;
            const rect = window.protocol_to_stage_rect(new Mtk.Rectangle({x, y, width, height}));
            const scale = actor.get_resource_scale() || 1;
            return {x: actor.x + rect.x / scale, y: actor.y + rect.y / scale, width: rect.width / scale,
                height: rect.height / scale, source: 'relative'};
        }
        if (caret.window !== null) {
            if (!window || caret.window !== String(window.get_id()))
                return null; // recorded for another window
            // Apps report the caret only after real input, so after a re-layout it can be stale;
            // one outside the window is certainly wrong.
            const b = window.get_buffer_rect();
            if (caret.x < b.x || caret.y < b.y || caret.x > b.x + b.width || caret.y > b.y + b.height)
                return null;
        }
        return {x: caret.x, y: caret.y, width: caret.width, height: caret.height, source: caret.source};
    }

    /** Where to show: the caret, else the bottom centre of the focused window's frame. */
    _anchor() {
        const caret = this._caretRect();
        if (caret)
            return caret;
        const window = global.display.focus_window;
        if (!window)
            return null;
        const f = window.get_frame_rect();
        return {x: f.x, y: f.y, width: f.width, height: f.height, source: 'window', fallback: true};
    }

    /** The work area of the anchor's monitor; null without any monitor (index -1). */
    _workArea(anchor) {
        const rect = new Mtk.Rectangle({x: Math.round(anchor.x), y: Math.round(anchor.y),
            width: Math.max(1, Math.round(anchor.width)), height: Math.max(1, Math.round(anchor.height))});
        let index = global.display.get_monitor_index_for_rect(rect);
        if (index < 0)
            index = Main.layoutManager.primaryIndex;
        return index < 0 ? null : Main.layoutManager.getWorkAreaForMonitor(index);
    }

    /**
     * Below the caret + 6 px, above it when there is no room, clamped to the work area. The side
     * is kept for updates of the same session while the caret stays put (no jumping while the
     * result streams in); when first placing a correction, room is kept for the change list.
     * Returns false when there is no monitor to place it on.
     */
    _place(actor, anchor, keep, view) {
        const wa = this._workArea(anchor);
        if (!wa)
            return false;
        const width = actor === this._panel.actor ? PANEL_WIDTH : actor.get_preferred_width(-1)[1];
        const height = actor.get_preferred_height(width)[1];
        const key = [anchor.x, anchor.y, anchor.width, anchor.height].map(Math.round).join(',') + anchor.source;
        let x, y, side;
        if (anchor.fallback) {
            side = 'window';
            x = anchor.x + (anchor.width - width) / 2;
            y = anchor.y + anchor.height - EDGE - height; // inside the window, at its bottom centre
        } else {
            const below = anchor.y + anchor.height + GAP;
            const roomBelow = wa.y + wa.height - EDGE - below;
            const roomAbove = anchor.y - GAP - (wa.y + EDGE);
            const growing = actor === this._panel.actor && view?.mode !== TRANSLATE &&
                ['checking', 'working', 'apply_pending'].includes(view?.status);
            const reserve = growing ? CHANGES_RESERVE : 0;
            side = keep?.key === key ? keep.side
                : height + reserve <= roomBelow ? 'below'
                    : height <= roomAbove ? 'above'
                        : roomBelow >= roomAbove ? 'below' : 'above';
            x = anchor.x - PADDING; // the panel's text starts under the caret
            y = side === 'below' ? below : anchor.y - GAP - height;
        }
        x = Math.round(clamp(x, wa.x + EDGE, wa.x + wa.width - EDGE - width));
        y = Math.round(clamp(y, wa.y + EDGE, wa.y + wa.height - EDGE - height));
        if (actor.x !== x || actor.y !== y)
            actor.set_position(x, y);
        this._placed = {actor, key, side, x, y, width, height, anchor};
        return true;
    }

    _reposition() {
        if (!this._alive)
            return;
        const actor = this._panel.actor.visible ? this._panel.actor
            : this._toast.actor.visible ? this._toast.actor : null;
        if (!actor)
            return;
        const anchor = this._anchor() ?? this._placed?.anchor;
        if (anchor && !this._place(actor, anchor, this._placed?.actor === actor ? this._placed : null, this._view))
            this._hideAll(true); // the monitor went away under the panel
    }

    // ---- test introspection (test mode only) -------------------------------------------------

    _testState() {
        const focus = {
            focus: this._focus && {app: this._focus[0], kind: this._focus[1]},
            following: this._focusIds.length > 0,
            subscriber: this._subscriber,
            focusSent: this._focusSent.slice(-20),
        };
        if (!this._alive) { // the actors are gone: report without touching them
            return {alive: false, version: this.version, testMode: TEST_MODE, watching: this._watched,
                toastTimer: !!this._toastId, actions: this._actions.slice(-20), ...focus};
        }
        const window = global.display.focus_window;
        const keyFocus = global.stage.key_focus;
        const placed = this._placed;
        return {
            alive: true,
            version: this.version,
            testMode: TEST_MODE,
            variant: this._variant,
            current: this._current,
            panel: this._panel.describe(),
            toast: this._toast.describe(),
            hud: this._hud.describe(),
            underlines: this._underlines.describe(),
            live: this._live.describe(),
            toastTimer: !!this._toastId,
            placed: placed && {x: placed.x, y: placed.y, side: placed.side, width: placed.width,
                height: placed.height, anchor: placed.anchor,
                actor: placed.actor === this._panel.actor ? 'panel' : 'toast'},
            caret: this._caretRect(),
            ibusHooked: !!this._ps,
            ibusDaemonPid: this._ibusDaemonPid(),
            launcher: launcherPath(GLib.get_user_data_dir()),
            ibusFocus: {...this._ibusFocus},
            stageKeyFocus: keyFocus ? `${keyFocus.constructor.name}:${keyFocus.name ?? ''}` : null,
            grab: !!global.stage.get_grab_actor?.(),
            focusWindow: window ? {title: window.get_title(), id: String(window.get_id())} : null,
            watching: this._watched,
            lastShowUs: this._lastShowUs,
            actions: this._actions.slice(-20),
            ...focus,
        };
    }
}
