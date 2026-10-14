// Who may drive the GramaDrama panel, when the relaxed test mode applies, and what the Focus
// signal reports. Pure functions without GNOME Shell imports, so tests/integration_panel.py checks
// them with plain gjs too.

/** The engine launcher install_engine.py installs and writes into the IBus component. */
export function launcherPath(userDataDir) {
    return `${userDataDir.replace(/\/+$/, '')}/gramadrama-engine/gramadrama-engine`;
}

/** The engine launcher of the .deb package (scripts/build-deb.sh), in a directory only root can write to. */
export const SYSTEM_LAUNCHER = '/usr/lib/gramadrama/gramadrama-engine';

/** The launchers an IBus component may name: the user's own copy and the package's. */
export function launcherPaths(userDataDir) {
    return [launcherPath(userDataDir), SYSTEM_LAUNCHER];
}

/** argv from the bytes of /proc/PID/cmdline (NUL-separated and NUL-terminated). */
export function parseCmdline(bytes) {
    const args = new TextDecoder().decode(bytes).split('\0');
    if (args.length && args[args.length - 1] === '')
        args.pop();
    return args;
}

/** The parent PID from /proc/PID/stat. The command name in parentheses may hold spaces and ')'. */
export function parentPid(stat) {
    const fields = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/);
    const ppid = Number(fields[1]);
    return Number.isInteger(ppid) && ppid > 0 ? ppid : null;
}

/**
 * The real engine: ibus-daemon starts `…python3 <launcher> --ibus` from the IBus component, so its
 * command line ends with exactly an installed launcher (launcherPaths, or one path) and `--ibus`, and its
 * parent is the session's ibus-daemon. A matching name elsewhere, extra arguments, another parent or an
 * unknown ibus-daemon are all refused.
 */
export function isEngineProcess(argv, ppid, launchers, ibusDaemonPid) {
    const n = argv.length;
    const allowed = Array.isArray(launchers) ? launchers : [launchers];
    return n >= 2 && argv[n - 1] === '--ibus' && allowed.includes(argv[n - 2]) &&
        Number.isInteger(ibusDaemonPid) && ibusDaemonPid > 1 && ppid === ibusDaemonPid;
}

/**
 * [app, kind] for the Focus signal (docs/apps.md, section 1). A shell entry with the keyboard
 * (Activities search, the run dialog) is ['org.gnome.Shell', 'shell'], no focus window is
 * ['', 'none']. Otherwise the app is the window tracker's desktop ID, or windowId(WM_CLASS) for a
 * window-backed app: its own ID, `window:<n>`, is new for every window. The kind is 'x11' or
 * 'wayland'.
 */
export function focusPair({shellEntry, window, x11, appId, windowBacked, wmClass}) {
    if (shellEntry)
        return ['org.gnome.Shell', 'shell'];
    if (!window)
        return ['', 'none'];
    return [appId && !windowBacked ? appId : windowId(wmClass), x11 ? 'x11' : 'wayland'];
}

const WINDOW_ID_MAX = 100; // characters of the WM_CLASS in an ID (a client may set any)

/**
 * `window:<WM_CLASS>`, the ID of an app without a .desktop file. A Chromium app window (`--app=URL`)
 * is named after its address, <browser>-<host>_<path with "/" as "_">-<profile>, e.g.
 * chrome-docs.google.com__document_d_…_edit-Default: only the browser and the host are kept
 * (window:chrome-docs.google.com; a local file has no host: window:chrome). The engine logs this ID
 * and apps.json keeps it, and a page's path or a file's name must never get there.
 */
export function windowId(wmClass) {
    let name = (wmClass ?? '').slice(0, WINDOW_ID_MAX);
    const path = name.indexOf('__');
    if (path > 0 && /^[A-Za-z][A-Za-z0-9-]*-/.test(name.slice(0, path)))
        name = name.slice(0, path).replace(/-+$/, '');
    return `window:${name}`;
}

/**
 * Test mode (any caller may drive the panel, and its state is exposed for introspection) needs
 * the engine's debug switch GRAMADRAMA_ENGINE_TEST=1 *and* the isolated test harness: a work dir
 * /tmp/gd-* (GD_WORK) that also holds the session bus socket. The switch alone may be exported by
 * a user debugging the engine; it must never open the panel to every process on the bus.
 */
export function isTestMode(getenv) {
    if (getenv('GRAMADRAMA_ENGINE_TEST') !== '1')
        return false;
    const work = getenv('GD_WORK') ?? '';
    const bus = getenv('DBUS_SESSION_BUS_ADDRESS') ?? '';
    return /^\/tmp\/gd-[A-Za-z0-9_]+$/.test(work) && bus.includes(`${work}/`);
}
