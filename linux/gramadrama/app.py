"""GramaDrama for Linux: the app in the application grid, for status and settings.

Corrections happen in the IBus engine (gramadrama/engine) and in the panel of the GNOME Shell
extension. This window shows whether all of that works (the correction shortcut, the input source,
the engine, the panel and the services: the model, LanguageTool, speech recognition), edits
settings.json and has a field to try the shortcut in. Its tabs are the Mac window's, one module each
in gramadrama/window. It reads no text from other apps and keeps no process running after it is closed."""
import sys

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import checks, oidc  # noqa: E402
from .window.apps_tab import (APP_ICONS, FLATPAK_HELP, GENERIC_APP_ICON, AppRow, app_icon, arranged,  # noqa: E402,F401
                              icon_name)
from .window.common import APP_DIR, APP_ID, CSS, WEBSITE, LogWatcher  # noqa: E402,F401
from .window.main import POINTS, TABS, Window  # noqa: E402


class Application(Adw.Application):
    def __init__(self):
        # HANDLES_COMMAND_LINE: the desktop hands gramadrama://auth?… (the browser coming back from a login) to a
        # new `gramadrama-app URL`, which this running one gets, whole, as its command line
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.removal = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)
        quit_action = Gio.SimpleAction.new('quit', None)
        quit_action.connect('activate', lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action('app.quit', ['<Control>q'])
        self.set_accels_for_action('win.refresh', ['<Control>r', 'F5'])
        for number, (name, _, _) in enumerate(TABS, 1):
            self.set_accels_for_action('win.tab-' + name, ['<Alt>%d' % number])
        self.set_accels_for_action('window.close', ['<Control>w'])
        if APP_DIR == checks.SYSTEM_DIR:  # the package's app closes when apt removes it
            self.removal = watch_removal(APP_DIR / 'gramadrama-app', self.quit)

    def do_activate(self):
        if self.window is None or self.window.closed:
            self.window = Window(self)
        self.window.present()

    def do_command_line(self, command_line):
        self.activate()
        for argument in command_line.get_arguments()[1:]:
            if oidc.is_callback(argument):
                self.window.login_callback(argument)
            elif argument.startswith('--show='):  # a failed correction's button, the dictation key (engine/)
                self.window.show_for(argument[len('--show='):])
            elif argument == '--login':
                self.window.point_to('login')
                self.window.run_action('login')
        return 0


def watch_removal(launcher, on_removed, delay_ms=1000):
    """Removing the package (apt remove) deletes the launcher this app runs from: on_removed then, so no window goes on
    running from deleted files. An upgrade replaces the file, which is there again when it is looked at a moment later.
    Returns the monitor, to keep."""
    pending = []

    def look():
        pending.clear()
        if not launcher.exists():
            on_removed()
        return False

    def changed(*_):
        if not pending:
            pending.append(GLib.timeout_add(delay_ms, look))
    monitor = Gio.File.new_for_path(str(launcher)).monitor_file(Gio.FileMonitorFlags.NONE, None)
    monitor.connect('changed', changed)
    return monitor


def wanted(argument):
    """The arguments this app takes: the login's way back from the browser, the tab or the login a failed
    correction's button asks for (engine/problems.py), and the group the dictation key points to (engine/dictate.py)."""
    names = [name for name, _, _ in TABS] + list(POINTS)
    return oidc.is_callback(argument) or argument == '--login' or argument in {'--show=' + name for name in names}


def main(argv=None):
    # The options of the old GTK app (--selection, --clipboard, --background) are ignored: an old
    # launcher or shortcut that still passes them just opens this window.
    argv = list(argv or sys.argv)
    return Application().run(argv[:1] + [a for a in argv[1:] if wanted(a)])
