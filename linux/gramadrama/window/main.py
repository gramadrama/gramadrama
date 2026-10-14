"""The window (MainWindowView.swift on the Mac): the tabs Start, Pisanie, Dyktowanie, Aplikacje, Usługi and Prywatność in a
bar at the top, each a page of groups built by its mixin. A tab with a warning or an error in one of its rows has a dot,
and the last tab is remembered (paths.state_dir()/window.json). The window keeps no process running after it is closed and
reads no text from other apps."""
import json
import os
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .. import checks, paths
from ..checks import ERROR, WARNING, Check
from ..engine import shortcuts
from ..i18n import interface_language, set_language, tr
from .apps_tab import APP_EVENTS, KEEP_APP_EVENTS, AppsTab
from .common import APP_DIR, APP_ID, STATE_FILE, WEBSITE, LogWatcher
from .dictation import CLEANUPS, DictationTab
from .privacy import PrivacyTab
from .services import PROTOCOLS, THINKS, ServicesTab
from .start import StartTab
from .writing import WritingTab

REFRESH_AFTER = 5.0  # seconds: an activated window checks again at most this often
KEEP_EVENTS = 2000
# The tabs (name, title, icon), as the Mac window has them (MainWindowTab); Alt+1…6 open them. 'connection' is Usługi, as
# the Mac keeps that name for the tab it remembers.
TABS = (('start', 'Start', 'go-home-symbolic'),
        ('writing', 'Pisanie', 'document-edit-symbolic'),
        ('dictation', 'Dyktowanie', 'audio-input-microphone-symbolic'),
        ('apps', 'Aplikacje', 'view-app-grid-symbolic'),
        ('connection', 'Usługi', 'network-server-symbolic'),
        ('privacy', 'Prywatność', 'channel-secure-symbolic'))


# Where a failed correction's button, the dictation key without a speech model or a button of the window points
# (engine/problems.py, engine/dictate.py, Window.point_to): the tab and the group the cause is in (WindowHighlight.Target
# on the Mac).
POINTS = {'connection': ('connection', 'llm_group'), 'speech': ('connection', 'speech_group'),
          'writing': ('writing', 'languages_group'), 'dictionary': ('writing', 'dictionary_group'),
          'login': ('connection', 'remote_group')}
HIGHLIGHT_MS = 6000  # how long the ring stays round that group
RING_MARGIN = 12  # px kept in view round the group: its ring is 2 px wide, 4 px out (common.CSS)


class Window(StartTab, WritingTab, DictationTab, AppsTab, ServicesTab, PrivacyTab, Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='GramaDrama', default_width=640, default_height=860)
        self.rebuilding = False  # rebuild_language: no state is written until the drafts are back
        self._initialize()
        self.connect('notify::is-active', self.activated)
        self.connect('notify::suspended', self.suspended_changed)
        self.connect('close-request', self.close_requested)

    def _initialize(self):
        settings, _ = checks.read_settings()
        self.ui_language = set_language(settings.primary_language)
        self.closed = False
        self.built = False  # write_state waits for the whole window
        self.highlighted, self.highlight_timer = None, 0  # point_to's target and the timer that clears its ring
        self.ring_watch = None  # (adjustment, handler): the ringed group kept in view while the view changes size
        self.system_gen = getattr(self, 'system_gen', 0) + 1
        self.system_timer = 0
        self.last_refresh = 0.0
        self.events = []
        self.last_toast, self.toasts_shown = None, 0  # tests: the last toast's text, the toasts on screen
        self.init_start()
        self.init_apps()
        self.init_services()
        self.bus = None
        self.subscriptions = []
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            pass
        settings = self.load_settings()
        self.bindings = shortcuts.resolve(vars(settings))

        style = Adw.StyleManager.get_default()
        self.subscriptions.append(('style', style.connect('notify::dark', self.dark_changed)))
        self.dark_changed(style)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView(content=self.toasts)
        self.stack = Adw.ViewStack()
        self.tab_pages = {}
        for name, title, icon in TABS:
            self.tab_pages[name] = Adw.PreferencesPage()
            self.stack.add_titled_with_icon(self.tab_pages[name], name, tr(title), icon)
        # The tabs at the top, an icon over each name, as in the Mac window.
        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.NARROW))
        menu = Gio.Menu()
        menu.append(tr('Otwórz log silnika'), 'win.open-log')
        if checks.package_installed():  # the package: apt cannot reach the accounts, so this goes first
            menu.append(tr('Wyłącz GramaDramę na tym koncie…'), 'win.teardown')
        menu.append(tr('O GramaDramie'), 'win.about')
        menu.append(tr('Zakończ'), 'app.quit')
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu, primary=True,
                                       tooltip_text=tr('Menu główne')))
        self.refresh_button = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text=tr('Odśwież stan (Ctrl+R)'),
                                         action_name='win.refresh')
        header.pack_start(self.refresh_button)
        view.add_top_bar(header)
        self.stack.set_visible_child_name(self.remembered_tab())
        self.stack.connect('notify::visible-child-name', self.remember_tab)
        self.toasts.set_child(self.stack)
        self.set_content(view)

        for name, groups in (('start', self.build_start(settings)), ('writing', self.build_writing(settings)),
                             ('dictation', [self.build_dictation(settings)]), ('apps', self.build_apps_tab()),
                             ('connection', self.build_services(settings)), ('privacy', self.build_privacy(settings))):
            for group in groups:
                self.tab_pages[name].add(group)

        # Window actions are on D-Bus too (org.gtk.Actions), which is how the harness presses them.
        actions = [('refresh', lambda *_: self.refresh()), ('open-log', lambda *_: self.open_log()),
                   ('about', lambda *_: self.about()), ('try-model', lambda *_: self.try_model()),
                   ('add-app', lambda *_: self.add_app_dialog()), ('teardown', lambda *_: self.teardown_dialog())]
        actions += [('tab-' + name, lambda *_, tab=name: self.show_tab(tab)) for name, _, _ in TABS]
        if STATE_FILE:  # tests: the state written now, e.g. for where widgets are after a change of layout
            actions.append(('write-state', lambda *_: self.write_state()))
            actions.append(('focus-site-button', lambda *_: self.focus_site_button()))  # scrolls a long page to it
            actions.append(('focus-try', lambda *_: self.focus_try_fresh()))
            actions.append(('unfocus', lambda *_: (self.set_focus(None), self.write_state())))  # screenshots: no focused entry
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
        # Bubble phase on the window: the input method (the compositor over text-input-v3, or the
        # in-process IBus module) sees every key first, so only a correction shortcut it let through ends up here.
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.try_key)
        self.key_controller = keys
        self.add_controller(keys)

        self.watch()
        self.log = LogWatcher(checks.log_path(), self.log_events)
        history = self.log.history()
        self.events = history[-KEEP_EVENTS:]
        self.app_events = [e for e in history if e['ev'] in APP_EVENTS][-KEEP_APP_EVENTS:]
        self.last_row.set_check(checks.last_use(self.events))
        self.built = True
        self.refresh()
        self.try_view.grab_focus()

    def rebuild_language(self, settings):
        """Rebuild translatable widgets in the same window, keeping drafts and the current tab."""
        if interface_language(settings.primary_language) == self.ui_language:
            return False
        tab = self.stack.get_visible_child_name()
        # Server addresses, keys being typed, vocabulary, model names and shortcuts are user data.
        entries = {name: widget.get_text() for name, widget in vars(self).items()
                   if isinstance(widget, (Adw.EntryRow, Adw.PasswordEntryRow))}
        shortcut_drafts = {name: row.get_text() for name, row in self.shortcut_rows.items()}
        sample = self.try_buffer.get_property('text')
        custom_sample = sample != self.try_sample
        remote_expanded = self.remote_expanded
        self.close_requested()
        self.built = False
        self.remove_controller(self.key_controller)
        for name in self.list_actions():
            self.remove_action(name)
        # _initialize refreshes the new widgets before they get the drafts back: a state written then would show the new
        # language with the saved texts (the screen is drawn only after this callback, the state file at once).
        self.rebuilding = True
        try:
            self._initialize()
            self.remote_expanded = remote_expanded
            for name, text in entries.items():
                getattr(self, name).set_text(text)
            for name, text in shortcut_drafts.items():
                self.shortcut_rows[name].set_text(text)
            if custom_sample:
                self.try_buffer.set_text(sample)
            self.show_tab(tab)
            self.refresh_remote(self.load_settings())
        finally:
            self.rebuilding = False
        self.write_state()
        return True

    # ---- state ----
    def dark_changed(self, style, *_):
        if style.get_dark():
            self.add_css_class('gd-dark')
        else:
            self.remove_css_class('gd-dark')
        self.write_state()

    def toast(self, text):
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        toast.connect('dismissed', self.toast_dismissed)
        self.toasts.add_toast(toast)
        self.last_toast, self.toasts_shown = text, self.toasts_shown + 1
        self.write_state()

    def toast_dismissed(self, *_):
        self.toasts_shown = max(0, self.toasts_shown - 1)
        self.write_state()

    def load_settings(self):
        """settings.json as the engine reads it; self.settings_problems: what the engine refuses in it."""
        settings, self.settings_problems = checks.read_settings()
        return settings

    def remembered_tab(self):
        """The tab the window was left on (the Mac window does the same); the first one when none is known."""
        try:
            name = json.loads((paths.state_dir() / 'window.json').read_text(encoding='utf-8')).get('tab')
        except (OSError, ValueError, AttributeError):
            return TABS[0][0]
        return name if name in self.tab_pages else TABS[0][0]

    def remember_tab(self, *_):
        try:
            directory = paths.state_dir()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'window.json').write_text(json.dumps({'tab': self.stack.get_visible_child_name()}), encoding='utf-8')
        except OSError:
            pass

    def show_tab(self, name):
        self.stack.set_visible_child_name(name)
        self.write_state()

    def show_for(self, name):
        """The tab or the group a failed correction's button or the dictation key names (gramadrama-app --show=<name>,
        engine/problems.py, engine/dictate.py): a group of POINTS is pointed to on its tab, another tab just opens."""
        if name in POINTS:
            self.point_to(name)
        else:
            self.clear_highlight()
            self.show_tab(name)

    @staticmethod
    def tab_of(name):
        """The tab of a tab's or a group's name (show_for)."""
        return POINTS[name][0] if name in POINTS else name

    def point_to(self, target):
        """Open the tab of target and ring the group the cause is in, scrolled into view, for HIGHLIGHT_MS
        ('connection': the language model, 'speech': speech recognition, 'writing': the text languages, 'dictionary':
        the dictionary and the rules, 'login': remote access)."""
        tab, attribute = POINTS[target]
        group = getattr(self, attribute)
        self.show_tab(tab)
        self.clear_highlight()
        group.add_css_class('gd-highlight')
        self.highlighted = target
        GLib.idle_add(self.scroll_into_view, group)
        self.highlight_timer = GLib.timeout_add(HIGHLIGHT_MS, self.highlight_done)
        self.write_state()

    def scroll_into_view(self, group, tries=40):
        """Scroll the group's tab so that the whole group and its ring are in view, with the least scrolling (its top
        first when it is taller than the view). A window that has only just opened is laid out first: then this tries
        again, for up to 2 s."""
        if self.highlighted is None or getattr(self, POINTS[self.highlighted][1]) is not group:
            return False  # the ring has gone or moved to another group meanwhile
        viewport = group.get_ancestor(Gtk.Viewport)
        adjustment = viewport.get_vadjustment() if viewport is not None else None
        ok, bounds = (group.compute_bounds(viewport) if adjustment is not None and group.get_mapped()
                      and group.get_height() > 0 and adjustment.get_page_size() > 0 else (False, None))
        if not ok:
            if tries > 0 and not self.closed:
                GLib.timeout_add(50, self.scroll_into_view, group, tries - 1)
            return False
        top, bottom = bounds.get_y() - RING_MARGIN, bounds.get_y() + bounds.get_height() + RING_MARGIN
        page = adjustment.get_page_size()
        if top < 0 or bottom - top > page:
            adjustment.set_value(adjustment.get_value() + top)
        elif bottom > page:
            adjustment.set_value(adjustment.get_value() + bottom - page)
        if self.ring_watch is None:
            # A window that has just opened may still be made smaller to fit the screen: the group is scrolled again.
            self.ring_watch = (adjustment, adjustment.connect('changed', lambda *_: GLib.idle_add(self.scroll_into_view,
                                                                                                group, 0)))
        return False

    def ring_in_view(self):
        """Tests (write_state): the ringed group and its ring wholly in the view of its tab; None when nothing is ringed."""
        if self.highlighted is None:
            return None
        group = getattr(self, POINTS[self.highlighted][1])
        viewport = group.get_ancestor(Gtk.Viewport)
        ok, bounds = group.compute_bounds(viewport) if viewport is not None else (False, None)
        return bool(ok) and bounds.get_y() - RING_MARGIN >= -0.5 and (
            bounds.get_y() + bounds.get_height() + RING_MARGIN <= viewport.get_vadjustment().get_page_size() + 0.5)

    def highlight_done(self):
        self.highlight_timer = 0
        if not self.closed:
            self.clear_highlight()
            self.write_state()
        return False

    def clear_highlight(self):
        if self.highlight_timer:
            GLib.source_remove(self.highlight_timer)
            self.highlight_timer = 0
        if self.ring_watch is not None:
            adjustment, handler = self.ring_watch
            adjustment.disconnect(handler)
            self.ring_watch = None
        for _, attribute in POINTS.values():
            getattr(self, attribute).remove_css_class('gd-highlight')
        self.highlighted = None

    def tab_checks(self):
        """The checks of the rows of each tab that can say something is wrong."""
        writing = [Check(ERROR, '')] if self.settings_problems.keys() & {'primary_language', 'secondary_language'} else []
        writing += [self.dictionaries_row.check] if self.dictionaries_row.get_visible() else []
        shortcut = [Check(ERROR, '')] if self.settings_problems.keys() & set(shortcuts.DEFAULTS) else []
        speech = [self.whisper_status_row.check] if self.whisper_status_row.get_visible() else []
        return {'start': [self.hotkey_row.check, self.source_row.check, self.engine_row.check, self.panel_row.check] + shortcut,
                'connection': [self.llm_status_row.check, self.lt_status_row.check, self.model_state, self.prompts_row.check]
                + speech + ([self.login_row.check, self.route_row.check] if self.login_row.get_visible() else []),
                'apps': ([self.apps_problem.check] if self.apps_problem.get_visible() else []) + [self.browser_row.check],
                'dictation': [] if self.first_run else [self.dictation_row.check],  # optional: not a first step
                'writing': writing}

    def update_tabs(self):
        """A tab with a warning or an error among its rows is marked in the bar: where to look."""
        if not self.built:
            return
        for name, checks_of_tab in self.tab_checks().items():
            self.stack.get_page(self.tab_pages[name]).set_needs_attention(
                any(check.state in (WARNING, ERROR) for check in checks_of_tab))

    def write_state(self):
        """Called whenever something shown changes: the marks of the tabs follow the rows. Tests only
        (GRAMADRAMA_APP_STATE): the window's state for the harness to read."""
        self.update_tabs()
        if not STATE_FILE or not self.built or self.rebuilding:
            return
        rows = {'hotkey': self.hotkey_row, 'source': self.source_row, 'engine': self.engine_row,
                'panel': self.panel_row, 'last': self.last_row, 'llm': self.llm_status_row, 'lt': self.lt_status_row,
                'whisper': self.whisper_status_row, 'whisper_local': self.whisper_local_row, 'prompts': self.prompts_row,
                'login': self.login_row, 'route': self.route_row, 'browser': self.browser_row,
                'model_try': self.model_try_row, 'dictation': self.dictation_row}
        focus = self.get_focus()
        state = {'rows': {name: {'state': row.check.state, 'text': row.check.text} for name, row in rows.items()},
                 'model': {'state': self.model_state.state, 'text': self.model_state.text, 'names': self.model_names,
                           'selected': self.model_row.get_selected()},
                 'summary': self.summary_label.get_label(), 'problems': [[label, name] for label, _, name in self.problems],
                 'settings_problems': self.settings_problems, 'ui_language': self.ui_language,
                 'tab_titles': {name: self.stack.get_page(page).get_title() for name, page in self.tab_pages.items()},
                 'languages': list(self.load_settings().languages),
                 'shortcut_labels': [binding.label for binding in self.bindings],
                 'translation_enabled': self.translation_row.get_sensitive(),
                 'dictation_languages': self.dictation_choices,
                 'tab': self.stack.get_visible_child_name(), 'sites': list(self.sites_shown), 'highlight': self.highlighted,
                 'ring_in_view': self.ring_in_view(),
                 'first_steps': {name: {'state': row.check.state, 'text': row.check.text} for name, row in (
                     ('gnome', self.first_gnome_row), ('model', self.first_model_row), ('try', self.first_try_row))}
                 if self.first_steps.get_visible() else None,
                 'attention': {name: self.stack.get_page(page).get_needs_attention() for name, page in self.tab_pages.items()},
                 'entries': {name: row.get_text() for name, row in (
                     ('llm', self.llm_url_entry), ('llm_local', self.llm_local_entry), ('lt', self.lt_url_entry),
                     ('lt_local', self.lt_local_entry), ('whisper', self.whisper_url_entry), ('login', self.login_entry),
                     ('login_client', self.login_client_row), ('model', self.llm_model_row), ('whisper_model', self.whisper_model_row),
                     ('pull', self.pull_entry))},
                 'server': {'api': PROTOCOLS[self.llm_api_row.get_selected()][0], 'key_row': self.llm_key_row.get_title(),
                            'think': THINKS[self.think_row.get_selected()][0], 'think_visible': self.think_row.get_visible(),
                            'key_button': self.clear_key_button.get_visible(), 'key_text': self.llm_key_row.get_text(),
                            'whisper_key_row': self.whisper_key_row.get_title(), 'whisper_key_text': self.whisper_key_row.get_text(),
                            'whisper_key_button': self.clear_whisper_key.get_visible(),
                            'whisper_backend': self.speech_where.location(),
                            'whisper_api': 'whispercpp' if self.whisper_api_row.get_selected() == 1 else 'openai',
                            'login_button': self.login_row.button.get_label() if self.login_row.button.get_visible() else None,
                            'login_back_visible': self.login_back_row.get_visible(),
                            'login_back_text': self.login_back_row.get_text(),
                            'focus_in': next((name for name, row in (('issuer', self.login_entry), ('client', self.login_client_row),
                                                                      ('back', self.login_back_row))
                                              if focus and (focus is row or focus.is_ancestor(row))), None),
                            'login_url': self.login_url},
                 'services': self.services_state(), 'live': self.live_state(), 'dictionary': self.dictionary_state(), 'privacy': self.privacy_state(),
                 'cleanup': CLEANUPS[self.cleanup_row.get_selected()][0] if self.cleanup_row.get_selected() < len(CLEANUPS) else None,
                 'version': self.version_row.get_subtitle(),
                 'try': {'state': self.try_state, 'text': self.try_text,
                         'buffer': self.try_buffer.get_property('text'), 'focus': self.try_view.has_focus()},
                 'geometry': {name: self.bounds(widget) for name, widget in (
                     ('primary_language_row', self.primary_language_row), ('secondary_language_row', self.secondary_language_row),
                     ('try_view', self.try_view), ('summary', self.summary), ('llm_where', self.llm_where.toggles),
                     ('llm_entry', self.llm_url_entry), ('llm_local_entry', self.llm_local_entry),
                     ('llm_api_row', self.llm_api_row), ('llm_model_row', self.llm_model_row), ('model_row', self.model_row),
                     ('llm_key_row', self.llm_key_row), ('clear_key', self.clear_key_button), ('think_row', self.think_row),
                     ('llm_advanced', self.llm_advanced), ('lt_where', self.lt_where.toggles), ('lt_entry', self.lt_url_entry),
                     ('lt_local_entry', self.lt_local_entry), ('speech_where', self.speech_where.toggles),
                     ('whisper_entry', self.whisper_url_entry), ('whisper_api_row', self.whisper_api_row),
                     ('whisper_model_row', self.whisper_model_row), ('whisper_key_row', self.whisper_key_row),
                     ('clear_whisper_key', self.clear_whisper_key), ('remote_setup', self.remote_setup_row),
                     ('login_entry', self.login_entry), ('login_client_row', self.login_client_row),
                     ('login_back_row', self.login_back_row), ('login_button', self.login_row.button),
                     ('remote_off', self.remote_off_row), ('checking', self.checking), ('change_shortcuts', self.change_shortcuts),
                     ('pull_entry', self.pull_entry), ('vocabulary_row', self.vocabulary_row),
                     ('learned_row', self.learned_row), ('rules_row', self.rules_row),
                     ('dictation_words', self.dictation_words_button), ('dictation_services', self.dictation_services_button),
                     ('add_app', self.add_app_row))},
                 'apps': self.apps_state(), 'toast': self.last_toast, 'toasts': self.toasts_shown,
                 'active': self.is_active(), 'suspended': self.is_suspended(), 'dark': self.has_css_class('gd-dark'),
                 't': time.time()}
        state['geometry']['site_button'] = self.bounds(next(iter(self.site_rows.values())).site_button) if self.site_rows else None
        for name, buttons in self.dictionary_buttons.items():  # the first row's: forget, rule_on
            state['geometry'][name] = self.bounds(buttons[0]) if buttons else None
        try:
            with open(STATE_FILE + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(STATE_FILE + '.tmp', STATE_FILE)
        except OSError:
            pass

    def focus_try_fresh(self):
        """Tests: the Wypróbuj field gets the focus back from another widget, without a click and with its text
        unchanged, so ibus-daemon drops the app's update and the engine takes the text it last handed on."""
        self.show_tab('start')
        self.refresh_button.grab_focus()
        self.try_view.grab_focus()
        self.write_state()

    def bounds(self, widget):
        """Tests: where a widget is, in window coordinates (the harness adds the window's frame)."""
        ok, rect = widget.compute_bounds(self)
        return [round(rect.origin.x), round(rect.origin.y), round(rect.size.width), round(rect.size.height)] \
            if ok and widget.get_mapped() else None

    # ---- checks ----
    def refresh(self):
        self.last_refresh = time.monotonic()
        settings = self.load_settings()
        if self.rebuild_language(settings):
            return
        self.refresh_languages(settings)
        self.refresh_dictation_languages(settings)
        self.refresh_shortcuts(settings)
        self.refresh_live(settings)
        self.refresh_dictionary(settings)
        self.refresh_system()
        self.refresh_server()
        self.refresh_apps()
        self.refresh_dictation()
        self.refresh_privacy(settings)

    def activated(self, *_):
        if not self.built:
            return
        if not self.is_active() and self.apps_frozen:
            self.apps_frozen = False
            self.show_apps(self.app_rows)  # sorted again while the user is elsewhere
        self.write_state()
        if self.is_active() and time.monotonic() - self.last_refresh > REFRESH_AFTER:
            self.refresh()

    def log_events(self, events):
        self.events = (self.events + events)[-KEEP_EVENTS:]
        if any(e['ev'] == 'speech_model' for e in events):
            self.refresh_dictation()
        if any(e['ev'] in ('browser_connected', 'browser_disconnected', 'browser_site') for e in events):
            self.refresh_system_soon()  # the Integracje group: an extension came or went
            self.refresh_apps_soon('browser')  # and the sites it is off on
        used = [e for e in events if e['ev'] in APP_EVENTS]
        if used:  # the correction shortcut in an app: a new row, or new evidence for one
            self.app_events = (self.app_events + used)[-KEEP_APP_EVENTS:]
            self.refresh_apps_soon('log')
        self.try_events(events)
        self.last_row.set_check(checks.last_use(self.events, names=self.app_names))
        self.update_summary()

    # ---- actions ----
    def run_action(self, action):
        if action == 'enable_panel':
            self.enable_panel()
        elif action == 'keyboard':
            try:
                Gio.Subprocess.new(['gnome-control-center', 'keyboard'], Gio.SubprocessFlags.NONE)
            except GLib.Error:
                self.toast(tr('Nie znaleziono Ustawień GNOME (gnome-control-center).'))
        elif action in ('install', 'install_tools'):
            command = checks.install_command(self.source, 'tools' if action == 'install_tools' else 'install')
            self.get_clipboard().set(command)
            self.toast(tr('Skopiowano: {value} — wklej w terminalu.', value=command))
        elif action == 'setup':
            self.run_account('setup', tr('Włączam GramaDramę na tym koncie…'), tr(
                'GramaDrama jest włączona na tym koncie. Panel pojawi się po wylogowaniu i ponownym zalogowaniu.'))
        elif action == 'install_dictionaries':
            command = checks.dictionary_command(checks.missing_dictionaries(self.load_settings().languages))
            self.get_clipboard().set(command)
            self.toast(tr('Skopiowano: {value} — wklej w terminalu.', value=command))
        elif action == 'log':
            self.open_log()
        elif action == 'prompts_open':
            self.open_prompts()
        elif action == 'extension_folder':
            self.open_extension_folder()
        elif action == 'login':
            self.login()
        elif action == 'logout':
            self.logout()
        elif action == 'try_model':
            self.try_model()
        elif action == 'cancel_model':
            self.cancel_model()
        elif action == 'ollama_download':
            Gtk.UriLauncher.new(checks.OLLAMA_DOWNLOAD).launch(self, None, None)

    def run_account(self, command, busy, success):
        """The package (.deb): install_engine.py setup or teardown on this account, without root. A failure shows the
        installer's last line (it speaks Polish only)."""
        script = checks.SYSTEM_DIR / 'install_engine.py'
        if not script.exists():
            self.toast(tr('Brak plików z paczki — zainstaluj ją ponownie: sudo apt install --reinstall gramadrama.'))
            return
        self.toast(busy)
        try:
            process = Gio.Subprocess.new(['/usr/bin/python3', str(script), command],
                                         Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self.toast(exc.message)
            return

        def done(process, result):
            try:
                _ok, out, _err = process.communicate_utf8_finish(result)
                ok = process.get_successful()
            except GLib.Error as exc:
                out, ok = exc.message, False
            lines = [line for line in (out or '').splitlines() if line.strip()]
            self.toast(success if ok else tr('Nie udało się: {value}', value=lines[-1] if lines else '?'))
            self.refresh()
        process.communicate_utf8_async(None, None, done)

    def teardown_dialog(self):
        dialog = Adw.AlertDialog(heading=tr('Wyłączyć GramaDramę na tym koncie?'), body=tr(
            'Źródło wprowadzania, panel, skrót, host przeglądarki i adres gramadrama:// wrócą do stanu sprzed włączenia. '
            'Ustawienia zostają. Potem paczkę usuniesz poleceniem: sudo apt remove gramadrama — wcześniej wyłącz '
            'GramaDramę tak samo na każdym koncie, na którym ją włączono.'), body_use_markup=False)
        dialog.add_response('cancel', tr('Anuluj'))
        dialog.add_response('teardown', tr('Wyłącz'))
        dialog.set_response_appearance('teardown', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.connect('response', lambda _d, response: response == 'teardown' and self.run_account(
            'teardown', tr('Wyłączam GramaDramę na tym koncie…'),
            tr('Wyłączono GramaDramę na tym koncie. Paczkę usuniesz poleceniem: sudo apt remove gramadrama')))
        dialog.present(self)

    def open_log(self):
        path = checks.log_path()
        if not path.exists():
            self.toast(tr('Log silnika jeszcze nie istnieje ({value}).', value=path))
            return

        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self.toast(tr('Nie udało się otworzyć {value}.', value=path))
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(self, None, done)

    def about(self):
        dialog = Adw.AboutDialog(application_name='GramaDrama', application_icon=APP_ID,
                                 version=checks.app_version_text(self.install, APP_DIR),
                                 comments=tr('Korekta, tłumaczenie i dyktowanie w obsługiwanych aplikacjach na GNOME. Pisz bez dramatu.'),
                                 website=WEBSITE)
        dialog.present(self)

    # ---- live updates ----
    def watch(self):
        if checks.has_schema(checks.INPUT_SOURCES):
            # Switching the source (top bar, Super+Space) rewrites mru-sources.
            self.input_settings = Gio.Settings.new(checks.INPUT_SOURCES)
            for key in ('sources', 'mru-sources'):
                self.subscriptions.append(('input', self.input_settings.connect('changed::' + key, self.refresh_system_soon)))
        if checks.has_schema(checks.SHELL):
            self.shell_settings = Gio.Settings.new(checks.SHELL)
            for key in ('disable-user-extensions', 'enabled-extensions'):
                self.subscriptions.append(('shell', self.shell_settings.connect('changed::' + key, self.refresh_system_soon)))
        if self.bus is None:
            return
        self.subscriptions.append(('watch', Gio.bus_watch_name_on_connection(
            self.bus, checks.ENGINE_BUS[0], Gio.BusNameWatcherFlags.NONE, self.refresh_system_soon,
            self.refresh_system_soon)))
        self.subscriptions.append(('signal', self.bus.signal_subscribe(
            checks.EXTENSIONS_BUS[0], checks.EXTENSIONS_BUS[2], 'ExtensionStateChanged', checks.EXTENSIONS_BUS[1],
            checks.EXTENSION_UUID, Gio.DBusSignalFlags.NONE, self.refresh_system_soon)))

    def close_requested(self, *_):
        self.closed = True
        for cancel in (self.model_cancel, self.pull_cancel):
            if cancel is not None:
                cancel.set()
        for timer in (self.system_timer, self.apps_timer, self.apps_soon_timer):
            if timer:
                GLib.source_remove(timer)
        self.system_timer = self.apps_timer = self.apps_soon_timer = 0
        self.log.close()
        for kind, handle in self.subscriptions:
            if kind == 'watch':
                Gio.bus_unwatch_name(handle)
            elif kind == 'input':
                self.input_settings.disconnect(handle)
            elif kind == 'shell':
                self.shell_settings.disconnect(handle)
            elif kind == 'style':
                Adw.StyleManager.get_default().disconnect(handle)
            else:
                self.bus.signal_unsubscribe(handle)
        self.subscriptions = []
        return False
