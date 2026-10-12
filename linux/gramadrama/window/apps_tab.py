"""Aplikacje (AppsTab.swift on the Mac): the mode of the correction shortcut and GramaDrama on or off for each app
(docs/apps.md, section 8), the sites the browser extension is off on, and the integrations through which GramaDrama reads
text (the browser extension). The rows of the apps come from apps.scan in a worker; the launch fix a Chromium or Electron
app needs on X11 is written from here (apps.py)."""
import collections
import os
from pathlib import Path
import threading
import traceback

from gi.repository import Adw, Gio, GLib, Gtk

from .. import apps, checks
from ..checks import BUSY, ERROR, INFO, OK, WARNING, Check
from ..i18n import tr
from .common import Status, StatusRow

APPS_TEXT = 'Tryb skrótu korekty i wyłączanie GramaDramy dla każdej aplikacji.'
APPS_EMPTY = 'Aplikacje pojawią się tu po pierwszej korekcie. Możesz też dodać je ręcznie.'
# The icon of a row's state (apps.RowState.icon), as Status.set_state takes it: the state that gives
# its colour, the icon, the accessible name.
APP_ICONS = {'ok': (OK, 'object-select-symbolic', 'Działa'),
             'partial': (OK, 'checkbox-mixed-symbolic', 'Działa z ograniczeniem'),  # the spec's ◐
             'restart': (WARNING, 'view-refresh-symbolic', 'Czeka na ponowne uruchomienie'),
             'tool': (WARNING, 'preferences-other-symbolic', 'Potrzebuje poprawki uruchamiania'),
             'error': (ERROR, 'action-unavailable-symbolic', 'Nie działa'),
             'warning': (WARNING, 'dialog-warning-symbolic', 'Uwaga'),
             'unknown': (INFO, 'dialog-question-symbolic', 'Niepotwierdzone'),
             'off': (INFO, 'system-shutdown-symbolic', 'Wyłączona')}
GENERIC_APP_ICON = 'application-x-executable'  # an app without a .desktop file (window:<wm_class>)
APP_EVENTS = ('start', 'refused', 'verified')  # the events apps.log_evidence reads
KEEP_APP_EVENTS = 20000  # of those: more than the two log files hold
RESCAN_MS = 3000  # while a row waits for its app to restart (docs/apps.md, section 7)
FLATPAK_HELP = ('Sprawdź identyfikator aplikacji poleceniem:\nflatpak list --app\n\n'
                'Pozwól wybranej aplikacji używać Wayland (zastąp APP_ID jej identyfikatorem):\n'
                'flatpak override --user --socket=wayland APP_ID\n\n'
                'Zamknij aplikację i uruchom ją ponownie, a następnie wypróbuj skrót korekty. '
                'Aplikacje Electron mogą dodatkowo wymagać flagi --ozone-platform=wayland; '
                'sposób jej ustawienia znajdziesz w dokumentacji danej aplikacji.\n\n'
                'GramaDrama nie zmienia uprawnień Flatpaka automatycznie. '
                'Własne zmiany dla aplikacji sprawdzisz przez flatpak override --user --show APP_ID. '
                'Ich cofnięcie poleceniem flatpak override --user --reset APP_ID usuwa wszystkie '
                'Twoje nadpisania uprawnień tej aplikacji.')


def app_icon(value, known):
    """The icon of a .desktop Icon value (apps.Entry.icon), read as GIO reads it: a path is a file,
    a name loses a .png, .xpm or .svg someone added to it. The generic icon stands in when there is
    none, or when known(name) (Gtk.IconTheme.has_icon) says the icon theme lacks it. Never both
    names in one icon: GTK tries every name in a theme before the next theme, so the generic icon
    of the user's theme would win over an app's icon in hicolor."""
    if value and os.path.isabs(value):
        return Gio.FileIcon.new(Gio.File.new_for_path(value))
    stem, dot, suffix = (value or '').rpartition('.')
    name = stem if dot and suffix in ('png', 'xpm', 'svg') else value
    return Gio.ThemedIcon.new(name if name and known(name) else GENERIC_APP_ICON)


def icon_name(icon):
    """Tests: the first choice of an icon, a name or a file."""
    if isinstance(icon, Gio.ThemedIcon):
        return icon.get_names()[0]
    return icon.get_file().get_path() if isinstance(icon, Gio.FileIcon) else None


def arranged(ids, shown, keep):
    """The order of the Aplikacje rows. ids: as apps.compose sorted them (state, then name); shown:
    the order on screen now; keep: the user is working in the list, so the rows stay where they are
    (a row that was just switched off would otherwise jump away) and new ones come last."""
    if not keep:
        return list(ids)
    current = set(ids)
    return [app_id for app_id in shown if app_id in current] + [app_id for app_id in ids if app_id not in shown]


class AppRow(Adw.ActionRow):
    """An app in the Aplikacje tab: its state, icon and name, the launch fix's button, a remove
    button for an app added by hand, and the switch (the whole row toggles it, as in Adw.SwitchRow)."""

    def __init__(self, window):
        super().__init__(use_markup=False)
        self.status = Status()
        self.add_prefix(self.status)
        self.icon = Gtk.Image(pixel_size=32, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.add_prefix(self.icon)
        self.button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self.button.connect('clicked', lambda *_: window.app_button(self.app))
        self.add_suffix(self.button)
        self.remove = Gtk.Button(icon_name='list-remove-symbolic', tooltip_text=tr('Usuń z listy'), valign=Gtk.Align.CENTER,
                                 css_classes=['flat'], visible=False)
        self.remove.connect('clicked', lambda *_: window.remove_app(self.app))
        self.add_suffix(self.remove)
        # The mode the correction shortcut starts in here (macOS Aplikacje): Popraw, Ulepsz, Formalnie, Luźniej, Krócej.
        self.mode = Gtk.DropDown.new_from_strings([tr(checks.MODE_NAMES[key]) for key in apps.applist.MODE_CHOICES])
        self.mode.set_valign(Gtk.Align.CENTER)
        self.mode.connect('notify::selected', lambda *_: self.updating or window.app_mode_changed(self))
        self.add_suffix(self.mode)
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect('notify::active', lambda *_: self.updating or window.app_switched(self))
        self.add_suffix(self.switch)
        self.set_activatable_widget(self.switch)
        self.app, self.order, self.updating = None, 0, False

    def set_app(self, app):
        if app != self.app:
            self.app = app
            self.set_title(app.name)
            self.set_subtitle(app.state.text)
            self.status.set_state(*APP_ICONS.get(app.state.icon, APP_ICONS['unknown']))
            self.icon.set_from_gicon(app_icon(app.icon, Gtk.IconTheme.get_for_display(self.get_display()).has_icon))
            if app.enabled:
                self.icon.remove_css_class('dimmed')
            else:
                self.icon.add_css_class('dimmed')
            label = apps.BUTTONS.get(app.state.button)
            self.button.set_visible(label is not None)
            if label is not None:
                self.button.set_label(tr(label))
            self.remove.set_visible(app.removable)
            # the name as it is, after "aplikację": a Polish name cannot be declined automatically
            self.remove.update_property([Gtk.AccessibleProperty.LABEL], [tr('Usuń aplikację {value} z listy', value=app.name)])
            self.switch.update_property([Gtk.AccessibleProperty.LABEL], [tr('GramaDrama w aplikacji {value}', value=app.name)])
            self.mode.set_tooltip_text(tr('Tryb, od którego zaczyna korekta w tej aplikacji'))
            self.mode.update_property([Gtk.AccessibleProperty.LABEL], [tr('Tryb korekty w aplikacji {value}', value=app.name)])
        self.updating = True  # also after a switch the file did not take: it shows the file again
        try:
            self.switch.set_active(app.enabled)
            self.mode.set_selected(apps.applist.MODE_CHOICES.index(app.mode))
            self.mode.set_sensitive(app.enabled)  # where GramaDrama is off, the correction shortcut is the app's
        finally:
            self.updating = False


class AddAppDialog(Adw.Dialog):
    """"Dodaj aplikację…": the apps GNOME shows (Gio.AppInfo), searchable by name, each with what to
    expect there (apps.expectation of kinds, from the last scan). An app already on the list (listed)
    is shown, but cannot be added again. Enter in the search field adds the first match."""

    def __init__(self, window, listed, kinds):
        super().__init__(title=tr('Dodaj aplikację'), content_width=460, content_height=620)
        self.window, self.query, self.rows = window, '', []
        self.search = Gtk.SearchEntry(placeholder_text=tr('Szukaj aplikacji'), hexpand=True)
        self.search.connect('search-changed', self.searched)
        self.search.connect('activate', self.pick_first)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'], valign=Gtk.Align.START)
        self.list.set_placeholder(Gtk.Label(label=tr('Nie znaleziono takiej aplikacji.'), wrap=True, css_classes=['dimmed'],
                                            margin_top=18, margin_bottom=18, margin_start=12, margin_end=12))
        found = []
        for info in Gio.AppInfo.get_all():
            app_id = info.get_id()
            if app_id and app_id not in apps.SELF_IDS and info.should_show():
                found.append((info.get_display_name() or app_id, app_id, info))
        for name, app_id, info in sorted(found, key=lambda item: (item[0].casefold(), item[1])):
            on_list = app_id in listed
            row = Adw.ActionRow(title=name, use_markup=False, activatable=not on_list, sensitive=not on_list,
                                subtitle=tr('już na liście') if on_list else apps.expectation(kinds.get(app_id)))
            row.add_prefix(Gtk.Image(gicon=info.get_icon() or Gio.ThemedIcon.new(GENERIC_APP_ICON), pixel_size=32,
                                     accessible_role=Gtk.AccessibleRole.PRESENTATION))
            row.app_id, row.key = app_id, ('%s %s' % (name, app_id)).casefold()
            row.connect('activated', lambda _row, app_id=app_id, name=name: window.add_app(app_id, name))
            self.list.append(row)
            self.rows.append(row)
        self.list.set_filter_func(lambda row: self.query in row.key)
        search = Gtk.Box(margin_start=12, margin_end=12, margin_bottom=6)
        search.append(self.search)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=Adw.Clamp(child=self.list, margin_top=6, margin_bottom=18, margin_start=12, margin_end=12),
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        view.add_top_bar(Adw.HeaderBar())
        view.add_top_bar(search)
        self.set_child(view)
        self.set_focus(self.search)

    def searched(self, *_):
        self.query = self.search.get_text().strip().casefold()
        self.list.invalidate_filter()
        self.window.write_state()

    def pick_first(self, *_):
        row = next((row for row in self.rows if row.get_child_visible() and row.get_activatable()), None)
        if row is not None:
            self.window.add_app(row.app_id, row.get_title())


class AppsTab:
    """The Aplikacje tab of the window (a mixin of window.main.Window)."""

    def init_apps(self):
        # The rows of the last scan (apps.scan), as apps.compose ordered them, and the order on screen; while the user works
        # in the list, rows keep their places (arranged).
        self.apps_gen = getattr(self, 'apps_gen', 0) + 1
        self.apps_scans = 0
        self.apps_why = collections.Counter()  # tests: why the scans started (refresh_apps)
        self.apps_soon_why = None
        self.apps_timer = self.apps_soon_timer = 0
        self.app_rows, self.app_order, self.app_widgets = [], [], {}
        self.app_names, self.app_kinds = {}, {}  # for "Ostatnie użycie" and the hints of "Dodaj aplikację…"
        self.apps_frozen = False
        self.app_events = []  # the log events apps.log_evidence reads, over both log files
        self.add_dialog = None
        self.browser_folder = ''
        self.site_rows = {}  # site -> its row
        self.sites_shown = []

    def build_apps_tab(self):
        """The groups of the tab: the apps, the sites (shown only while there are some) and the integrations."""
        return [self.build_apps(), self.build_sites(), self.build_integrations()]

    def build_apps(self):
        """A note while apps.json is broken, one row per app, the empty state and "Dodaj aplikację…". The rows are sorted in
        the list itself (AppRow.order), so a row moves without being taken out, and keeps the keyboard focus."""
        group = Adw.PreferencesGroup(title=tr('Aplikacje'), description=tr(APPS_TEXT))
        self.apps_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.apps_list.set_sort_func(lambda a, b: a.order - b.order)
        self.apps_problem = StatusRow(tr('Lista aplikacji'), self)
        self.apps_empty = Adw.ActionRow(title=tr(APPS_EMPTY), use_markup=False, activatable=False)
        self.apps_empty.add_prefix(Gtk.Image(icon_name='dialog-information-symbolic', css_classes=['gd-info']))
        self.add_app_row = Adw.ButtonRow(title=tr('Dodaj aplikację…'), start_icon_name='list-add-symbolic',
                                         action_name='win.add-app')
        for order, row in ((-1, self.apps_problem), (1 << 30, self.apps_empty), ((1 << 30) + 1, self.add_app_row)):
            row.order = order
            self.apps_list.append(row)
        self.apps_problem.set_visible(False)
        self.apps_empty.set_visible(False)  # until the first scan: never "empty" for a list not read yet
        group.add(self.apps_list)
        return group

    def build_sites(self):
        """Wyłączone strony: the sites the browser extension is off on (apps.json "sites"), each with "Włącz"."""
        self.sites_group = Adw.PreferencesGroup(
            title=tr('Wyłączone strony'), visible=False,
            description=tr('Bez sprawdzania podczas pisania; skrót korekty i dyktowanie działają.'))
        return self.sites_group

    def build_integrations(self):
        """Integracje (the Mac's integrationsSection): the browser extension, the same as the Mac's (docs/browser.md)."""
        group = Adw.PreferencesGroup(title=tr('Integracje'))
        self.browser_row = StatusRow(tr('Wtyczka przeglądarki'), self)
        self.browser_row.set_check(Check(BUSY, tr('Sprawdzam…')))
        group.add(self.browser_row)
        return group

    def show_sites(self, sites):
        """One row for each site the extension is off on, in order, with "Włącz"."""
        for site in [s for s in self.site_rows if s not in sites]:
            self.sites_group.remove(self.site_rows.pop(site))
        for site in sorted(sites):
            if site in self.site_rows:
                continue
            row = Adw.ActionRow(title=site, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name='web-browser-symbolic', css_classes=['gd-info']))
            button = Gtk.Button(label=tr('Włącz'), valign=Gtk.Align.CENTER,
                                tooltip_text=tr('Włącz sprawdzanie na tej stronie'))
            button.connect('clicked', lambda *_, site=site: self.site_on(site))
            row.add_suffix(button)
            row.site_button = button
            self.site_rows[site] = row
            self.sites_group.add(row)
        self.sites_group.set_visible(bool(sites))
        self.sites_shown = sorted(sites)
        self.write_state()

    def focus_site_button(self):
        if self.site_rows:
            next(iter(self.site_rows.values())).site_button.grab_focus()
        self.write_state()

    def site_on(self, site):
        error, backup = apps.set_site(site, False)
        self.toast(error or tr('Wtyczka sprawdza znów na {value}.', value=site))
        self.refresh_apps('site')

    def open_extension_folder(self):
        """The folder of the browser extension, to pick in "Załaduj rozpakowane" (and its path on the clipboard)."""
        folder = self.browser_folder
        if not folder or not Path(folder).is_dir():
            self.toast(tr('Folder wtyczki nie istnieje. Uruchom instalator (install_engine.py install).'))
            return
        self.get_clipboard().set(folder)
        self.toast(tr('Ścieżka folderu wtyczki jest w schowku.'))

        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self.toast(tr('Nie udało się otworzyć {value}.', value=folder))
        Gtk.FileLauncher.new(Gio.File.new_for_path(folder)).launch(self, None, done)

    # ---- the rows of the apps ----
    def refresh_apps(self, why='refresh'):
        """apps.json, the .desktop files, PATH and /proc are read in a worker (apps.scan); the log's
        events are the ones this window already follows. why: what asked for the scan, counted for
        the tests ('refresh', 'log', 'seen', 'timer', 'switch', 'button', 'add', 'remove')."""
        self.apps_gen += 1
        self.apps_why[why] += 1
        gen, events = self.apps_gen, list(self.app_events)

        def work():
            try:
                config, problem = apps.read_config()
                result = apps.scan(events, config)
                sites = list(config.sites)
            except Exception:
                traceback.print_exc()
                result = problem = sites = None
            GLib.idle_add(self.apps_done, gen, result, problem, sites)
        threading.Thread(target=work, name='gramadrama-apps', daemon=True).start()

    def refresh_apps_soon(self, why):
        """Coalesces the events of one correction (start, apply, verified…) into one scan; the first
        reason counts."""
        if not self.apps_soon_timer and not self.closed:
            self.apps_soon_why = why
            self.apps_soon_timer = GLib.timeout_add(300, self._refresh_apps_timer)

    def _refresh_apps_timer(self):
        self.apps_soon_timer = 0
        self.refresh_apps(self.apps_soon_why)
        return False

    def apps_done(self, gen, result, problem, sites=None):
        if gen != self.apps_gen or self.closed:
            return False
        self.apps_scans += 1
        if sites is not None:
            self.show_sites(sites)
        if result is None:
            check = Check(WARNING, tr('Nie udało się sprawdzić aplikacji.'))
        else:
            check = apps.config_check(problem)
            self.app_names, self.app_kinds = result.names, result.kinds
            self.show_apps(result.rows)
            self.last_row.set_check(checks.last_use(self.events, names=self.app_names))
        self.apps_problem.set_visible(check is not None)
        if check is not None:
            self.apps_problem.set_check(check)
        waiting = any(row.state.state == apps.RESTART for row in self.app_rows)
        if waiting and not self.apps_timer:
            self.apps_timer = GLib.timeout_add(RESCAN_MS, self._rescan_apps)
        elif not waiting and self.apps_timer:
            GLib.source_remove(self.apps_timer)
            self.apps_timer = 0
        self.write_state()
        return False

    def show_apps(self, rows):
        self.app_rows = rows
        self.app_order = arranged([row.id for row in rows], self.app_order, self.apps_frozen)
        by_id = {row.id: row for row in rows}
        focus = self.get_focus()
        for app_id in [app_id for app_id in self.app_widgets if app_id not in by_id]:
            widget = self.app_widgets.pop(app_id)
            if focus is not None and (focus == widget or focus.is_ancestor(widget)):
                self.add_app_row.grab_focus()  # a removed app's row takes the focus away with it
            self.apps_list.remove(widget)
        for order, app_id in enumerate(self.app_order):
            widget = self.app_widgets.get(app_id)
            if widget is None:
                widget = self.app_widgets[app_id] = AppRow(self)
                self.apps_list.append(widget)
            widget.order = order
            widget.set_app(by_id[app_id])
        self.apps_list.invalidate_sort()
        self.apps_empty.set_visible(not rows and self.apps_scans > 0)
        self.write_state()

    def _rescan_apps(self):
        """Every few seconds while a row waits for its app to restart (apps_done starts and stops it):
        the GPU process of the new instance tells when it runs natively, no correction shortcut needed. Only
        while the window can be seen."""
        if self.get_mapped() and not self.is_suspended():
            self.refresh_apps('timer')
        return True

    def suspended_changed(self, *_):
        """GNOME Shell says whether the window can be seen (it cannot while it is minimized). Seen
        again while a row waits for its app to restart, it rescans at once instead of at the next
        tick."""
        if not self.is_suspended() and self.apps_timer:
            self.refresh_apps_soon('seen')
        self.write_state()

    def apps_saved(self, text, error, backup):
        if error:
            self.toast(error)
        elif backup is not None:
            self.toast(tr('{value}. Poprzedni, nieprawidłowy plik listy aplikacji jest w {value2}.', value=text, value2=backup.name))
        else:
            self.toast(text)

    def app_switched(self, row):
        """The switch writes apps.json at once; the engine follows the file by itself. App names go
        after "aplikacji"/"aplikację" in the texts: Polish would decline the name itself otherwise."""
        app, enabled = row.app, row.switch.get_active()
        self.apps_frozen = True
        error, backup = apps.set_enabled(app.id, enabled)
        self.apps_saved(tr('GramaDrama {value} w aplikacji {value2}', value=tr('włączona') if enabled else tr('wyłączona'), value2=app.name), error,
                        backup)
        self.refresh_apps('switch')

    def app_mode_changed(self, row):
        """The list writes apps.json at once, like the switch; the engine follows the file by itself."""
        app, key = row.app, apps.applist.MODE_CHOICES[row.mode.get_selected()]
        self.apps_frozen = True
        error, backup = apps.set_mode(app.id, key)
        self.apps_saved(tr('Korekta w aplikacji {value} zaczyna od trybu „{value2}”', value=app.name, value2=tr(checks.MODE_NAMES[key])), error,
                        backup)
        self.refresh_apps('mode')

    def app_button(self, app):
        """The launch fix: "Włącz" writes it, "Wyłącz poprawkę" deletes it, "Napraw" writes it anew."""
        self.apps_frozen = True
        button = app.state.button
        if button == apps.HELP:
            dialog = Adw.AlertDialog(heading=tr('Aplikacja z Flatpaka'), body=tr(FLATPAK_HELP), body_use_markup=False)
            dialog.add_response('close', tr('Zamknij'))
            dialog.present(self)
            return
        if button == apps.APPLY and app.plan is not None:
            error, done = apps.apply_fix(app.plan), tr('Poprawka włączona.')
        elif button == apps.UNDO and app.fix is not None:
            error, done = apps.undo_fix(app.fix), tr('Poprawka wyłączona.')
            if app.fix.kind == apps.OVERRIDE and not app.fix.pending:
                # the app grid keeps the command it read at login (docs/apps.md, section 6)
                done += tr(' Z siatki aplikacji {value} uruchomi się bez niej po ponownym zalogowaniu.', value=app.name)
        elif button == apps.REPAIR and app.fix is not None and app.plan is not None:
            error, done = apps.repair_fix(app.fix, app.plan), tr('Poprawka naprawiona.')
        else:
            return
        self.toast(error or done)
        self.refresh_apps('button')

    def remove_app(self, app):
        """Only an app added by hand, and there for no other reason (apps.listed)."""
        self.apps_frozen = True
        error, backup = apps.remove(app.id)
        self.apps_saved(tr('Usunięto aplikację {value} z listy', value=app.name), error, backup)
        self.refresh_apps('remove')

    def add_app_dialog(self):
        if self.add_dialog is not None:
            return
        self.add_dialog = AddAppDialog(self, {row.id for row in self.app_rows}, self.app_kinds)
        self.add_dialog.connect('closed', self.add_dialog_closed)
        self.add_dialog.present(self)
        self.write_state()

    def add_dialog_closed(self, *_):
        self.add_dialog = None
        self.write_state()

    def add_app(self, app_id, name):
        self.apps_frozen = True  # the new row comes last, where it is looked for
        error, backup = apps.add(app_id)
        if self.add_dialog is not None:
            self.add_dialog.close()
        self.apps_saved(tr('Dodano aplikację {value} do listy', value=name), error, backup)
        self.refresh_apps('add')

    def apps_state(self):
        """Tests: the Aplikacje tab as shown, rows in their order on screen."""
        rows = []
        for app_id in self.app_order:
            row = self.app_widgets[app_id]
            app = row.app
            rows.append({'id': app.id, 'name': app.name, 'state': app.state.state, 'icon': app.state.icon,
                         'text': app.state.text, 'button': row.button.get_label() if row.button.get_visible() else None,
                         'enabled': row.switch.get_active(), 'removable': app.removable,
                         'mode': apps.applist.MODE_CHOICES[row.mode.get_selected()],
                         'mode_sensitive': row.mode.get_sensitive(),
                         'status': row.status.shown[0], 'app_icon': icon_name(row.icon.get_gicon()),
                         'fix': str(app.fix.path) if app.fix else None,
                         'geometry': {name: self.bounds(widget) for name, widget in (
                             ('row', row), ('button', row.button), ('remove', row.remove), ('mode', row.mode),
                             ('switch', row.switch))}})
        dialog = None
        if self.add_dialog is not None:
            shown = [row for row in self.add_dialog.rows if row.get_child_visible()]
            dialog = {'query': self.add_dialog.search.get_text(), 'rows': [row.app_id for row in shown],
                      'addable': [row.app_id for row in shown if row.get_activatable() and row.get_sensitive()]}
        problem = self.apps_problem.check.text if self.apps_problem.get_visible() else None
        return {'rows': rows, 'scans': self.apps_scans, 'why': dict(self.apps_why), 'names': self.app_names,
                'frozen': self.apps_frozen, 'problem': problem, 'empty': self.apps_empty.get_visible(),
                'timer': bool(self.apps_timer), 'dialog': dialog}
