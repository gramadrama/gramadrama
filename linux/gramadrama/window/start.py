"""Start (StartTab.swift on the Mac): the state in one place, with a button to the tab of the first problem; a field to try
the correction shortcut in; what GNOME must have for GramaDrama (the input source, the IBus engine, the panel); the
shortcuts; and the last use and the version.

The Wypróbuj field is an ordinary text field, corrected by the engine like any other; the window follows the engine's
event log (events, times and app IDs, never text) to say what happened there. A bubble-phase key controller on the window
catches a correction shortcut the input method let through: then the engine did not get it."""
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from .. import checks
from ..checks import BUSY, ERROR, INFO, OK, WARNING, Check
from ..engine import keys as engine_keys, shortcuts
from ..i18n import tr
from .common import APP_DIR, APP_ID, ICON_FILE, ICONS, Status, StatusRow

TRY_TEXT = 'Czesc, przesylam poprawiona wersje raportu. Jesli bedziesz miec uwagi daj znac do piatku.'
WAITING = 'Czekam na skrót korekty…'
# The keys of the correction panel (the Mac's "W panelu poprawki").
PANEL_SHORTCUTS = (('Wstaw poprawkę (także w trakcie)', ('Return',)),
                   ('Anuluj', ('Escape',)),
                   ('Tryby: Popraw … Tłumacz', ('<Control>1', '…', '<Control>6')),
                   ('Odwróć kierunek tłumaczenia', ('<Control>d',)))


def key_caps(accels):
    """The keys of a shortcut as the window shows them; '…' between two, '#name' for a key no accelerator can name."""
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    for accel in accels:
        if accel == '…' or accel.startswith('#'):
            box.append(Gtk.Label(label=tr(accel.lstrip('#')), css_classes=['dimmed' if accel == '…' else 'heading']))
        else:
            box.append(Adw.ShortcutLabel(accelerator=accel))
    return box


class StartTab:
    """The Start tab of the window (a mixin of window.main.Window)."""

    def init_start(self):
        self.conflicts = []
        self.hotkey_verified = None  # True/False once the correction shortcut was tried in the Wypróbuj field
        self.not_reached = False  # a miss waits for fresh checks to name its cause
        self.miss_known = False  # the last miss has a cause that fixing is enough for (not_reached)
        self.source_state = None
        self.source = None  # the repository the installed copy came from (install command)
        self.install = None
        self.try_state, self.try_text = INFO, ''
        self.try_session = None  # engine session started in the Wypróbuj field
        self.problems = []  # (label, check, tab) of what needs attention, first one first
        self.first_run = False  # no model chosen yet: a new install (Pierwsze kroki, the neutral pill)

    def build_start(self, settings):
        return [self.build_hero(), self.build_first_steps(), self.build_try(), self.build_gnome(), self.build_shortcuts(settings),
                self.build_system()]

    def build_hero(self):
        group = Adw.PreferencesGroup()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.CENTER, css_classes=['gd-hero'])
        icon = Gio.FileIcon.new(Gio.File.new_for_path(str(ICON_FILE))) if ICON_FILE.exists() else Gio.ThemedIcon.new(APP_ID)
        hero.append(Gtk.Image(gicon=icon, pixel_size=88, margin_bottom=8, accessible_role=Gtk.AccessibleRole.PRESENTATION))
        hero.append(Gtk.Label(label='GramaDrama', css_classes=['title-1']))
        hero.append(Gtk.Label(label=tr('Pisz bez dramatu.'), css_classes=['dimmed']))
        line = Gtk.Box(spacing=8)
        self.summary_status = Status()
        self.summary_label = Gtk.Label(label=tr('Sprawdzam…'), wrap=True, justify=Gtk.Justification.CENTER)
        line.append(self.summary_status)
        line.append(self.summary_label)
        # A click opens the tab where the first problem is fixed (the Mac's status pill).
        self.summary = Gtk.Button(child=line, halign=Gtk.Align.CENTER, css_classes=['gd-summary', 'flat'])
        self.summary.connect('clicked', lambda *_: self.problems and self.show_for(self.problems[0][2]))
        self.summary_status.set_state(BUSY)
        hero.append(self.summary)
        group.add(hero)
        return group

    def build_first_steps(self):
        """A new install, with no model chosen yet: what is left before the first correction, in order; hidden once a
        model is chosen (update_first_steps)."""
        group = Adw.PreferencesGroup(title=tr('Pierwsze kroki'), visible=False)
        self.first_steps = group
        self.first_gnome_row = StatusRow(tr('Skrót i panel'), self)
        self.first_model_row = StatusRow(tr('Model językowy'), self)
        services = Gtk.Button(label=tr('Otwórz Usługi'), valign=Gtk.Align.CENTER, css_classes=['suggested-action'])
        services.connect('clicked', lambda *_: self.point_to('connection'))
        self.first_model_row.add_suffix(services)
        self.first_try_row = StatusRow(tr('Pierwsza korekta'), self)
        for row in (self.first_gnome_row, self.first_model_row, self.first_try_row):
            group.add(row)
        return group

    def update_first_steps(self):
        """The steps of a new install follow the checks: GNOME's part, then the model (as the Usługi tab says it)."""
        self.first_steps.set_visible(self.first_run)
        if not self.first_run:
            return
        shortcut = self.bindings[0].label  # the correction shortcut as the user has set it
        self.first_steps.set_description(tr('Potem {value} poprawia tekst w każdej aplikacji.', value=shortcut))
        self.first_try_row.set_check(Check(INFO, tr('W polu Wypróbuj poniżej: kursor strzałką i {value}.', value=shortcut)))
        gnome = [self.source_row.check, self.engine_row.check, self.panel_row.check]
        if any(check.state == BUSY for check in gnome):
            self.first_gnome_row.set_check(Check(BUSY, tr('Sprawdzam…')))
        elif all(check.state == OK for check in gnome):
            self.first_gnome_row.set_check(Check(OK, tr('Działają: źródło wprowadzania, silnik IBus i panel przy kursorze.')))
        else:
            self.first_gnome_row.set_check(Check(WARNING, tr('Dokończ to w części „Integracja z GNOME” poniżej.')))
        llm = self.llm_status_row.check
        model = llm if llm.state in checks.PROBLEMS or llm.state == BUSY else self.model_state
        self.first_model_row.set_check(Check(model.state, model.text))  # its own button: Otwórz Usługi

    def build_try(self):
        group = Adw.PreferencesGroup(title=tr('Wypróbuj'))
        self.try_group = group
        reset = Gtk.Button(icon_name='edit-undo-symbolic', tooltip_text=tr('Przywróć przykład'),
                           css_classes=['flat'], valign=Gtk.Align.CENTER)
        reset.connect('clicked', lambda *_: self.reset_try())
        group.set_header_suffix(reset)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.try_sample = self.sample_text()
        self.try_buffer = Gtk.TextBuffer(text=self.try_sample)
        self.try_buffer.connect('changed', lambda *_: self.write_state())
        self.try_view = Gtk.TextView(buffer=self.try_buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                     left_margin=14, right_margin=14, top_margin=12, bottom_margin=12)
        self.try_view.update_property([Gtk.AccessibleProperty.LABEL], [tr('Pole do wypróbowania skrótu korekty')])
        self.try_view.connect('notify::has-focus', lambda *_: self.write_state())
        scroll = Gtk.ScrolledWindow(child=self.try_view, css_classes=['card', 'gd-try'],
                                    hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    min_content_height=72, max_content_height=220)
        box.append(scroll)
        line = Gtk.Box(spacing=8, margin_top=10, margin_start=4)
        self.try_status = Status()
        self.try_label = Gtk.Label(wrap=True, xalign=0, hexpand=True, css_classes=['caption'])
        line.append(self.try_status)
        line.append(self.try_label)
        box.append(line)
        group.add(box)
        self.set_try(INFO, tr(WAITING))
        return group

    def build_gnome(self):
        """What GramaDrama needs from GNOME (the Mac's Uprawnienia): the input source, the IBus engine, the panel."""
        group = Adw.PreferencesGroup(title=tr('Integracja z GNOME'))
        self.source_row = StatusRow(tr('Źródło wprowadzania'), self)
        self.engine_row = StatusRow(tr('Silnik IBus'), self)
        self.panel_row = StatusRow(tr('Panel przy kursorze'), self)
        for row in (self.source_row, self.engine_row, self.panel_row):
            group.add(row)
        return group

    def build_shortcuts(self, settings):
        """The shortcuts (the Mac's Skróty): correction and translation with their state, the dictation key, the keys of the
        panel; changing the two shortcuts is folded away (Linux only: GNOME may already use them)."""
        group = Adw.PreferencesGroup(title=tr('Skróty'))
        self.shortcut_label = Adw.ShortcutLabel(accelerator=self.bindings[0].accelerator, valign=Gtk.Align.CENTER)
        self.hotkey_row = StatusRow(tr('Popraw tekst'), self, suffix=self.shortcut_label)
        self.translation_label = Adw.ShortcutLabel(accelerator=self.bindings[1].accelerator, valign=Gtk.Align.CENTER)
        self.translation_row = Adw.ActionRow(title=tr('Przetłumacz'), use_markup=False)
        self.translation_row.add_prefix(Gtk.Image(icon_name='preferences-desktop-locale-symbolic', css_classes=['dimmed']))
        self.translation_row.add_suffix(self.translation_label)
        dictate = Adw.ActionRow(title=tr('Dyktuj: trzymaj, mów, puść'), use_markup=False)
        dictate.add_prefix(Gtk.Image(icon_name='audio-input-microphone-symbolic', css_classes=['dimmed']))
        dictate.add_suffix(key_caps(('#prawy Ctrl',)))
        self.shortcuts_problem = Adw.ActionRow(use_markup=False, visible=False)
        self.shortcuts_problem.add_prefix(Gtk.Image(icon_name=ICONS[ERROR], css_classes=['gd-error']))
        self.change_shortcuts = Adw.ExpanderRow(title=tr('Zmień skróty'), use_markup=False,
                                                subtitle=tr('Ctrl+Alt, Ctrl+Shift albo Ctrl+Alt+Shift i litera A–Z.'))
        self.shortcut_rows = {}
        self.shortcut_shown = {field: getattr(settings, field) for field in shortcuts.DEFAULTS}
        for field, title in (('correction_shortcut', tr('Popraw')), ('translation_shortcut', tr('Przetłumacz'))):
            row = Adw.EntryRow(title=title, text=getattr(settings, field), use_markup=False, show_apply_button=True)
            row.connect('apply', lambda row, field=field: self.apply_shortcut(row, field))
            restore = Gtk.Button(icon_name='edit-undo-symbolic', valign=Gtk.Align.CENTER, css_classes=['flat'],
                                 tooltip_text=tr('Przywróć ') + shortcuts.DEFAULTS[field])
            restore.connect('clicked', lambda *_, row=row, field=field: self.reset_shortcut(row, field))
            row.add_suffix(restore)
            self.change_shortcuts.add_row(row)
            self.shortcut_rows[field] = row
        panel = Adw.ExpanderRow(title=tr('W panelu poprawki'), use_markup=False)
        for title, accels in PANEL_SHORTCUTS:
            row = Adw.ActionRow(title=tr(title), use_markup=False)
            row.add_suffix(key_caps(accels))
            panel.add_row(row)
        for row in (self.hotkey_row, self.translation_row, dictate, self.shortcuts_problem, self.change_shortcuts, panel):
            group.add(row)
        self.refresh_shortcuts(settings)
        return group

    def build_system(self):
        group = Adw.PreferencesGroup(title=tr('System'))
        self.last_row = StatusRow(tr('Ostatnie użycie'), self)
        self.version_row = Adw.ActionRow(title=tr('Wersja'), use_markup=False)
        self.version_row.add_prefix(Gtk.Image(icon_name='help-about-symbolic', css_classes=['dimmed']))
        group.add(self.last_row)
        group.add(self.version_row)
        return group

    # ---- the Wypróbuj field ----
    def sample_text(self):
        primary = self.load_settings().languages[0]
        return {'pl': TRY_TEXT,
                'en': 'Hi, Im sending the updated report. If you have any comment let me know by friday.',
                'de': 'Hallo, ich sende den aktualisierten bericht. Wenn du anmerkungen hast melde dich bitte bis freitag.'}[primary]

    def set_try(self, state, text):
        self.try_state, self.try_text = state, text
        self.try_status.set_state(state)
        self.try_label.set_label(text)
        self.write_state()

    def reset_try(self):
        self.try_sample = self.sample_text()
        self.try_buffer.set_text(self.try_sample)
        self.try_view.grab_focus()
        self.set_try(INFO, tr(WAITING))

    def try_key(self, _controller, keyval, _keycode, state):
        """The correction shortcut in the Wypróbuj field that the input method let through: the engine did not get it."""
        mods = int(state) & engine_keys.RELEVANT
        if not self.bindings[0].matches(keyval, mods):
            return False
        if not self.try_view.has_focus():
            return False  # no text field: no input method either, nothing to learn
        self.hotkey_verified = False
        self.not_reached = True
        text, self.miss_known = checks.not_reached(self.source_state, self.conflicts)
        self.hotkey_row.set_check(checks.hotkey_check(self.conflicts, False))
        self.set_try(ERROR, text)
        self.update_summary()
        self.refresh_system()  # the input source may have changed since the last check
        return True

    def try_events(self, events):
        """The engine's events of a session started in the Wypróbuj field, in the field's line."""
        for event in events:
            ev = event['ev']
            if ev == 'start' or ev == 'refused' and event.get('at') == 'hotkey':
                mine = self.try_view.has_focus() and self.is_active()
                self.try_session = event.get('id') if mine and ev == 'start' else None
                if mine:  # the engine consumed the correction shortcut in this window's field
                    self.hotkey_verified = True
                    self.not_reached = self.miss_known = False
                    self.hotkey_row.set_check(checks.hotkey_check(self.conflicts, True))
            else:
                mine = self.try_session is not None and event.get('id', self.try_session) == self.try_session
                if mine and ev in ('end', 'verified', 'refused'):
                    self.try_session = None
            if mine:
                update = checks.try_event(event)
                if update is not None:
                    self.set_try(*update)

    # ---- the shortcuts ----
    def apply_shortcut(self, row, field):
        error, backup = checks.save_setting(field, row.get_text().strip())
        if error:
            row.add_css_class('error')
            self.toast(error)
            return
        row.remove_css_class('error')
        self.hotkey_verified = None
        self.not_reached = self.miss_known = False
        self.refresh_shortcuts(self.load_settings(), force=True)
        self.saved(tr('skrótu'), backup)
        self.set_try(INFO, tr('Czekam na {value}…', value=self.bindings[0].label))
        self.refresh_system()

    def reset_shortcut(self, row, field):
        row.set_text(shortcuts.DEFAULTS[field])
        self.apply_shortcut(row, field)

    def refresh_shortcuts(self, settings, force=False):
        bindings = shortcuts.resolve(vars(settings))
        if bindings != self.bindings:
            self.hotkey_verified = None
            self.not_reached = self.miss_known = False
        self.bindings = bindings
        self.shortcut_label.set_accelerator(bindings[0].accelerator)
        self.translation_label.set_accelerator(bindings[1].accelerator)
        self.try_group.set_description(tr('Ustaw kursor w tekście strzałką i naciśnij {value}.', value=bindings[0].label))
        for field, row in self.shortcut_rows.items():
            if force or row.get_text().strip() == self.shortcut_shown[field]:
                row.set_text(getattr(settings, field))
            self.shortcut_shown[field] = getattr(settings, field)
            if field in self.settings_problems:
                row.add_css_class('error')
            else:
                row.remove_css_class('error')
        problems = [self.settings_problems[field] for field in shortcuts.DEFAULTS if field in self.settings_problems]
        self.shortcuts_problem.set_visible(bool(problems))
        self.shortcuts_problem.set_title(tr('Nieprawidłowe skróty — silnik używa domyślnych'))
        self.shortcuts_problem.set_subtitle('; '.join(dict.fromkeys(problems)))
        if problems:
            self.change_shortcuts.set_expanded(True)
        two = len(settings.languages) > 1
        self.translation_row.set_title(tr('Przetłumacz {value}', value=' ↔ '.join(code.upper() for code in settings.languages))
                                       if two else tr('Przetłumacz'))
        self.translation_row.set_subtitle('' if two else tr('Dodaj drugi język w zakładce Pisanie.'))
        self.translation_row.set_sensitive(two)
        self.shortcut_rows['translation_shortcut'].set_sensitive(two)

    # ---- the state of GNOME, the engine and the panel ----
    def refresh_system_soon(self, *_):
        """Coalesces bursts of signals (sources rewritten, an engine restarting) into one check."""
        if not self.system_timer and not self.closed:
            self.system_timer = GLib.timeout_add(300, self._refresh_system_timer)

    def _refresh_system_timer(self):
        self.system_timer = 0
        self.refresh_system()
        return False

    def refresh_system(self):
        self.system_gen += 1
        gen = self.system_gen
        # GSettings are read here, on the main thread; the worker gets plain values.
        try:
            bindings = checks.gnome_bindings()
            active_bindings = self.bindings if len(self.load_settings().languages) > 1 else self.bindings[:1]
            self.conflicts = [binding.label + ': ' + name for binding in active_bindings
                              for name in checks.hotkey_conflicts(bindings, binding.accelerator)]
        except Exception:
            self.conflicts = []
        try:
            sources = checks.input_sources()
        except Exception:
            sources = []
        try:
            user_disabled = checks.user_extensions_disabled()
        except Exception:
            user_disabled = False
        self.hotkey_row.set_check(checks.hotkey_check(self.conflicts, self.hotkey_verified))
        bus = self.bus

        def work():
            try:
                result = checks.probe_system(bus, APP_DIR) if bus is not None else None
            except Exception:
                result = None
            GLib.idle_add(self.system_done, gen, sources, user_disabled, result)
        threading.Thread(target=work, name='gramadrama-system', daemon=True).start()

    def system_done(self, gen, sources, user_disabled, result):
        if gen != self.system_gen or self.closed:
            return False
        if result is None:
            failed = Check(WARNING, tr('Nie udało się sprawdzić (brak sesyjnej magistrali D-Bus?).'))
            for row in (self.source_row, self.engine_row, self.panel_row):
                row.set_check(failed)
            self.update_summary()
            return False
        source = checks.source_check(sources, result['current'])
        self.browser_folder = result.get('browser_folder', '')
        self.browser_row.set_check(checks.browser_check(result['status'], result.get('browser_hosts', []), self.browser_folder))
        self.source_state = source
        self.source, self.install = result['source'], result['install']
        self.version_row.set_subtitle(checks.app_version_text(self.install, APP_DIR))
        if self.not_reached:  # the first checks after a miss name its cause
            self.not_reached = False
            text, self.miss_known = checks.not_reached(source, self.conflicts)
            self.set_try(ERROR, text)
        elif self.hotkey_verified is False and self.miss_known and source.state == OK and not self.conflicts:
            # The cause of the miss is gone. A miss without a known cause stays until the engine
            # really gets the shortcut.
            self.hotkey_verified = None
            if self.try_state == ERROR:
                self.set_try(INFO, tr('Teraz powinno działać — użyj ponownie skrótu korekty.'))
        self.hotkey_row.set_check(checks.hotkey_check(self.conflicts, self.hotkey_verified))
        self.source_row.set_check(source)
        self.engine_row.set_check(checks.engine_check(
            result['status'], result['registered'], result['installed'], result['component'], result['install'],
            result['origin'], result['differs'], result['stale']))
        self.panel_row.set_check(checks.panel_check(
            result['extension'], result['loaded'], result['on_disk'],
            (result['install'] or {}).get('panel_changed_at'), result['shell_started'], user_disabled))
        self.update_summary()
        return False

    def enable_panel(self):
        if self.bus is None:
            return

        def done(bus, result):
            try:
                if not bus.call_finish(result).unpack()[0]:
                    self.toast(tr('GNOME nie włączył panelu. Wyloguj się i zaloguj ponownie.'))
            except GLib.Error as exc:
                self.toast(tr('Nie udało się włączyć panelu: {value}', value=exc.message))
            self.refresh_system_soon()
        self.bus.call(*checks.EXTENSIONS_BUS, 'EnableExtension', GLib.Variant('(s)', (checks.EXTENSION_UUID,)),
                      GLib.VariantType('(b)'), Gio.DBusCallFlags.NONE, 5000, None, done)

    # ---- the summary ----
    def update_summary(self):
        """The pill under the name: everything fine, or what needs attention; a click opens the tab of the first problem
        (MainWindowModel.problems on the Mac). LanguageTool is optional and never in it; "Ostatnie użycie" is history."""
        broken = self.settings_problems
        settings = self.load_settings()
        local = self.llm_where.location() == 'local'
        named = [(tr('skrót'), self.hotkey_row.check, 'start'), (tr('źródło wprowadzania'), self.source_row.check, 'start'),
                 (tr('silnik'), self.engine_row.check, 'start'), ('panel', self.panel_row.check, 'start'),
                 (tr('ustawienia') if broken.keys() & {checks.FILE, 'llm_url', 'llm_api', 'llm_local_url', 'llm_location'}
                  else 'Ollama' if local else tr('serwer modelu'), self.llm_status_row.check, 'connection'),
                 (tr('ustawienia') if 'model' in broken else 'model', self.model_state, 'connection')]
        if settings.dictation_enabled is not False and settings.whisper_uses_server:
            named.append((tr('serwer mowy'), self.whisper_status_row.check, 'connection'))
        # A refused address of any service stops every correction (core.Settings.validate), LanguageTool's too.
        if broken.keys() & {'languagetool_location', 'languagetool_local_url', 'languagetool_url', 'whisper_backend',
                            'whisper_url', 'whisper_api', 'auth_issuer'}:
            named.append((tr('ustawienia'), Check(ERROR, ''), 'connection'))
        if broken.keys() & {'primary_language', 'secondary_language'}:
            named.append((tr('języki'), Check(ERROR, ''), 'writing'))
        if broken.keys() & set(shortcuts.DEFAULTS):
            named.append((tr('skróty'), Check(ERROR, ''), 'start'))
        state, text = checks.summary([(label, check) for label, check, _ in named])
        self.problems = [item for item in named if item[1].state in checks.PROBLEMS]
        self.first_run = not settings.model.strip()
        if self.first_run and self.problems and all(tab == 'connection' for _, _, tab in self.problems):
            state, text = INFO, tr('Jeszcze jeden krok: model językowy')  # a step to take, not a fault (Pierwsze kroki)
        self.update_first_steps()
        for name in list(ICONS) + [BUSY]:
            self.summary.remove_css_class('gd-' + name)
        self.summary.add_css_class('gd-' + state)
        self.summary.set_can_target(bool(self.problems))
        self.summary.set_tooltip_text(tr('Pokaż, co poprawić') if self.problems else None)
        self.summary_status.set_state(state)
        self.summary_label.set_label(text)
        self.write_state()
