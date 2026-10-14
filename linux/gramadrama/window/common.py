"""Building blocks shared by the tabs (SettingsComponents.swift on the Mac): the state icon, a row for a check, an address
row that keeps what is being typed, the "Gdzie" row of a service, and the follower of the engine's log."""
import os
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import checks  # noqa: E402
from ..checks import BUSY, ERROR, INFO, OK, WARNING, Check  # noqa: E402
from ..i18n import tr  # noqa: E402

APP_ID = 'pl.gramadrama.Linux'
APP_DIR = Path(__file__).resolve().parent.parent.parent  # linux/ in the repository, or the installed copy
ICON_FILE = APP_DIR / 'icon.svg'
WEBSITE = 'https://gramadrama.app'
STATE_FILE = os.environ.get('GRAMADRAMA_APP_STATE')  # tests: the window's state as JSON
LOG_READ_MAX = 4 << 20  # bytes of the engine log read at once (it rotates at 1 MB)
ICONS = {OK: 'object-select-symbolic', WARNING: 'dialog-warning-symbolic', ERROR: 'dialog-error-symbolic',
         INFO: 'dialog-information-symbolic'}
STATE_NAMES = {OK: 'Działa', WARNING: 'Uwaga', ERROR: 'Błąd', INFO: 'Informacja', BUSY: 'Sprawdzam'}
# The fix a check offers (Check.action) and the label of its button.
ACTIONS = {'enable_panel': 'Włącz', 'keyboard': 'Ustawienia', 'install': 'Kopiuj polecenie', 'log': 'Pokaż log',
           'try_model': 'Sprawdź', 'cancel_model': 'Przerwij', 'install_tools': 'Kopiuj polecenie',
           'install_dictionaries': 'Kopiuj polecenie',
           'prompts_open': 'Otwórz folder', 'login': 'Zaloguj się…', 'logout': 'Wyloguj',
           'extension_folder': 'Otwórz folder', 'ollama_download': 'Pobierz Ollamę…'}
# Fixes that are a new install's first step (Pierwsze kroki): their button is the suggested action
NEXT_STEPS = frozenset(('ollama_download',))
# Where a service runs (core.LOCATIONS), as the Mac's LocationRow: the choice and its icon.
LOCATIONS = (('local', 'Ten komputer', 'computer-symbolic'), ('server', 'Serwer', 'network-server-symbolic'))
# Brand colours (branding/BRAND.md): lavender accent (the app's actions), sage for "works", brick
# for a warning, rose for errors; the light variants are darker so that small icons and text stay
# readable on a light background. The standalone accent (--accent-color: the dot on a tab that needs
# attention, focus rings, links) is set too: Ubuntu's libadwaita sets its own (orange) instead of
# deriving it from --accent-bg-color; in dark it is the lighter lavender, as on the panel.
CSS = '''
:root { --accent-bg-color: #66579e; --accent-fg-color: #fffefc; --accent-color: #66579e; }
.gd-dark { --accent-color: #bcb0ec; }
preferencesgroup.gd-highlight list.boxed-list { outline: 2px solid var(--accent-color); outline-offset: 4px; }
.gd-hero { margin-bottom: 4px; }
button.gd-summary { margin-top: 10px; padding: 5px 14px 5px 10px; border-radius: 999px; font-weight: bold; min-height: 0; }
.gd-ok { color: #536b60; }
.gd-warning { color: #ad4e2c; }
.gd-error { color: #9e3446; }
.gd-info, .gd-busy { color: alpha(currentColor, 0.6); }
.gd-dark .gd-ok { color: #adc8b5; }
.gd-dark .gd-warning { color: #eba27f; }
.gd-dark .gd-error { color: #f2a7ae; }
button.gd-summary.gd-ok { background-color: alpha(#78927f, 0.18); }
button.gd-summary.gd-warning { background-color: alpha(#d07a52, 0.16); }
button.gd-summary.gd-error { background-color: alpha(#c4566a, 0.16); }
button.gd-summary.gd-busy, button.gd-summary.gd-info { background-color: alpha(currentColor, 0.08); }
.gd-try textview, .gd-try textview text { background-color: transparent; }
.gd-try textview { font-size: 11pt; }
'''


def not_checked():
    return Check(INFO, tr('Nie sprawdzono — najpierw popraw ustawienia.'))


class Status(Gtk.Stack):
    """The state of a check: a coloured icon, or a spinner while it runs."""

    def __init__(self, size=16):
        super().__init__(valign=Gtk.Align.CENTER, hhomogeneous=True, vhomogeneous=True)
        self.icon = Gtk.Image(pixel_size=size)
        self.add_named(self.icon, 'icon')
        self.add_named(Adw.Spinner(width_request=size, height_request=size), 'busy')
        self.state = self.shown = None

    def set_state(self, state, icon=None, label=None):
        """state: OK … BUSY, which also gives the colour; icon and label: an icon and an accessible
        name other than the state's own (the rows of the Aplikacje tab)."""
        if (state, icon, label) == self.shown:
            return
        for name in ICONS:
            self.icon.remove_css_class('gd-' + name)
        self.state, self.shown = state, (state, icon, label)
        if state == BUSY:
            self.set_visible_child_name('busy')
        else:
            self.icon.set_from_icon_name(icon or ICONS.get(state, ICONS[INFO]))
            self.icon.add_css_class('gd-' + state)
            self.set_visible_child_name('icon')
        self.update_property([Gtk.AccessibleProperty.LABEL], [tr(label or STATE_NAMES.get(state, ''))])


class StatusRow(Adw.ActionRow):
    """A row for one check: state icon, title, the check's text and an optional fix button. check: a button "Sprawdź"
    after the fix button (ServiceStatusRow on the Mac), which calls it."""

    def __init__(self, title, window, suffix=None, check=None):
        super().__init__(title=title, use_markup=False)
        self.status = Status()
        self.add_prefix(self.status)
        if suffix is not None:
            self.add_suffix(suffix)
        self.button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self.button.connect('clicked', lambda *_: window.run_action(self.check.action))
        self.add_suffix(self.button)
        self.check_button = None
        if check is not None:
            self.check_button = Gtk.Button(label=tr('Sprawdź'), valign=Gtk.Align.CENTER)
            self.check_button.connect('clicked', lambda *_: check())
            self.add_suffix(self.check_button)
        self.check = None
        self.set_check(Check(BUSY, tr('Sprawdzam…')))

    def set_check(self, check):
        self.check = check
        self.set_subtitle(check.text)
        self.status.set_state(check.state)
        label = ACTIONS.get(check.action)
        self.button.set_visible(label is not None)
        if label is not None:
            self.button.set_label(tr(label))
        if check.action in NEXT_STEPS:  # the one thing to do first: the accent, as GNOME's suggested action
            self.button.add_css_class('suggested-action')
        else:
            self.button.remove_css_class('suggested-action')
        if self.check_button is not None:
            self.check_button.set_sensitive(check.state != BUSY)


def show_text(entry, text):
    """The text in an entry, left alone when it is already there: set_text of the same text still empties the entry
    and fills it again (two "changed" signals, the caret at the end), and the caret of somebody about to edit the
    saved address jumped with every refresh."""
    if entry.get_text() != text:
        entry.set_text(text)


class AddressRow(Adw.EntryRow):
    """An address (or another text) of settings.json, applied with Enter or the apply button. The saved text follows the file
    while nobody edits it: an address being typed (the entry differs from the last saved one) survives a refresh, since
    keyboard focus is inside the EntryRow's own text widget and cannot tell. default: what an empty value means (the
    address on this computer), shown in its place."""

    def __init__(self, title, text, on_apply, default=''):
        super().__init__(title=title, show_apply_button=True, use_markup=False, input_purpose=Gtk.InputPurpose.URL)
        self.default = default
        self.saved = text or default
        self.set_text(self.saved)
        self.connect('apply', lambda *_: on_apply(self))

    def set_saved(self, text):
        text = text or self.default
        if self.get_text().strip() in ('', self.saved):
            show_text(self, text)
        self.saved = text

    def value(self):
        """The text to save: '' for the default."""
        text = self.get_text().strip()
        return '' if text == self.default else text


def toggle_group(choices):
    """A segmented choice (the Mac's segmented Picker): (name, label) each. Labels never ellipsize, so the choice keeps
    its width beside a long subtitle, which wraps instead."""
    group = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
    for name, label in choices:
        group.add(Adw.Toggle(name=name, tooltip=tr(label), child=Gtk.Label(label=tr(label))))
    return group


class LocationRow(Adw.ActionRow):
    """The first row of a service (LocationRow on the Mac): where it runs, this computer or a server, with what that
    means below the title."""

    def __init__(self, on_change):
        super().__init__(title=tr('Gdzie'), use_markup=False)
        self.icon = Gtk.Image(icon_name=LOCATIONS[0][2], css_classes=['dimmed'])
        self.add_prefix(self.icon)
        self.toggles = toggle_group([(name, label) for name, label, _ in LOCATIONS])
        self.add_suffix(self.toggles)
        self.updating = False
        self.toggles.connect('notify::active-name', lambda *_: self.updating or on_change(self.toggles.get_active_name()))

    def set_location(self, name, subtitle):
        self.updating = True
        try:
            self.toggles.set_active_name(name)
        finally:
            self.updating = False
        self.icon.set_from_icon_name(dict((key, icon) for key, _, icon in LOCATIONS)[name])
        self.set_subtitle(subtitle)

    def location(self):
        return self.toggles.get_active_name()


class LogWatcher:
    """Follows the engine's JSONL log and hands the events the app reads (checks.LOG_EVENTS) to
    on_events, on the main loop. The log it follows stays open: when the engine rotates it
    (engine.log -> engine.log.1), what is left of the old file is read before the new one."""

    def __init__(self, path, on_events):
        self.path = Path(path)
        self.on_events = on_events
        self.file = None
        self.rest = b''
        self.monitor = None
        try:
            # The engine creates this directory too (mode 0700); a monitor needs it to exist.
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.monitor = Gio.File.new_for_path(str(self.path.parent)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.connect('changed', self._changed)
        except (OSError, GLib.Error):
            pass

    def history(self):
        """Events of the rotated and the current log (once, at start); the current one is followed
        from the end of its last whole line."""
        events = []
        try:
            with open(self.path.with_name(self.path.name + '.1'), 'rb') as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - LOG_READ_MAX))
                events += checks.parse_log(f.read(LOG_READ_MAX))
        except OSError:
            pass
        if self._open():
            start = max(0, os.fstat(self.file.fileno()).st_size - LOG_READ_MAX)
            self.file.seek(start)
            data = self.file.read(LOG_READ_MAX)
            end = data.rfind(b'\n') + 1
            self.file.seek(start + end)
            events += checks.parse_log(data[:end])
        return events

    def _open(self):
        try:
            self.file = open(self.path, 'rb')
        except OSError:
            self.file = None
        self.rest = b''
        return self.file is not None

    def _changed(self, _monitor, file, other, _event):
        names = {file.get_basename(), other.get_basename() if other else None}
        if self.path.name in names:
            self.read()

    def read(self):
        if self.file is not None:
            self._consume(self.file.read(LOG_READ_MAX))  # also after the file was renamed away
        try:
            st = os.stat(self.path)
        except OSError:
            return  # rotated, and the first line of the new log is not written yet
        if self.file is None or st.st_ino != os.fstat(self.file.fileno()).st_ino or st.st_size < self.file.tell():
            if self.file is not None:
                self.file.close()
            if self._open():
                self._consume(self.file.read(LOG_READ_MAX))

    def _consume(self, data):
        if not data:
            return
        head, newline, self.rest = (self.rest + data).rpartition(b'\n')
        events = checks.parse_log(head) if newline else []
        if events:
            self.on_events(events)

    def close(self):
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None
        if self.file is not None:
            self.file.close()
            self.file = None
