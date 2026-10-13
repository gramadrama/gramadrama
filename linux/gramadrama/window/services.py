"""Usługi (ServicesTab.swift on the Mac): everything that runs outside the app itself. The language model, LanguageTool and
speech recognition, each on this computer or on a server, have the same layout: where it runs, the address (on this
computer a default one that can be changed; for a server the protocol and key too), its state with "Sprawdź". Then the
optional remote access: the login address and client id of an OpenID Connect provider (Pocket ID), the account, and how
the services connect now (core.route: directly, or through the gateway away from home). The other tabs only say what
they use. The model has a folded "Zaawansowane" part: the model's thinking and the prompt files."""
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from .. import checks, core, oidc
from ..checks import BUSY, ERROR, INFO, OK, Check
from ..i18n import tr
from .common import STATE_FILE, AddressRow, LocationRow, Status, StatusRow, not_checked, show_text

# The model server's protocol (core.LLM_APIS): what Ollama, llama.cpp, LM Studio, vLLM and cloud services speak.
PROTOCOLS = (('openai', 'Zgodny z OpenAI'), ('ollama', 'Ollama'))
SPEECH_PROTOCOLS = (('openai', 'Zgodny z OpenAI'), ('whispercpp', 'whisper.cpp'))
# Ollama's "thinking" (core.LLM_THINKS): off is what the corrections were measured with (docs/models.md).
THINKS = (('off', 'wyłączone (zalecane)'), ('model', 'domyślne ustawienie modelu'))
# The recommended model, pulled into Ollama on this computer when none is typed (LocalOllama.suggestedModel on the Mac,
# where it is also the default model of a new install).
SUGGESTED_MODEL = 'gemma4:e4b'
LLM_FIELDS = (checks.FILE, 'llm_location', 'llm_local_url', 'llm_url', 'llm_api')
LT_FIELDS = ('languagetool_location', 'languagetool_local_url', 'languagetool_url')
SPEECH_FIELDS = ('whisper_backend', 'whisper_url', 'whisper_api')


def size(value):
    return GLib.format_size(value)


class ServicesTab:
    """The Usługi tab of the window (a mixin of window.main.Window)."""

    def init_services(self):
        self.server_gen = getattr(self, 'server_gen', 0) + 1
        self.model_names = []
        self.model_state = Check(BUSY, tr('Sprawdzam…'))
        self.models_known = False  # the server said which models it has
        self.model_names_of_server = []
        self.llm_ok = False  # the model server answered at the last check
        self.routes = {}  # core.route's kind of each service at the last check
        self.updating = False  # a combo or a toggle is being set from the file, not by the user
        self.model_cancel = None
        self.pull_cancel = None
        self.pull_state = None  # (model, completed, total, error or None, done) of the last pull
        self.login_starting = False
        self.login_url = None
        self.remote_expanded = False

    def build_services(self, settings):
        return [self.build_model(settings), self.build_languagetool(settings), self.build_speech(settings), self.build_remote(settings)]

    # ---- the language model ----
    def build_model(self, settings):
        group = Adw.PreferencesGroup(title=tr('Model językowy'), description=tr('Korekta, tłumaczenie i sprawdzanie modelem.'))
        self.llm_group = group
        self.llm_where = LocationRow(lambda name: self.location_selected('llm_location', name))
        self.llm_local_entry = AddressRow(tr('Adres Ollamy'), settings.llm_local_url,
                                          lambda row: self.apply_address(row, 'llm_local_url'), default=core.LOCAL_OLLAMA)
        self.llm_url_entry = AddressRow(tr('Adres serwera'), settings.llm_url, lambda row: self.apply_address(row, 'llm_url'))
        self.llm_api_row = Adw.ComboRow(title=tr('Protokół'), use_markup=False,
                                        model=Gtk.StringList.new([tr(label) for _, label in PROTOCOLS]))
        self.llm_api_row.connect('notify::selected', lambda *_: self.choice_selected(self.llm_api_row, PROTOCOLS, 'llm_api', tr('protokołu')))
        self.llm_key_row, self.clear_key_button = self.key_row('llm_key')
        self.llm_status_row = StatusRow(tr('Ollama'), self, check=self.check_services)
        self.model_row = Adw.ComboRow(title=tr('Model'), use_markup=False)
        self.model_status = Status()
        self.model_row.add_prefix(self.model_status)
        self.model_list = Gtk.StringList()
        self.model_row.set_model(self.model_list)
        self.model_row.connect('notify::selected', self.model_selected)
        self.llm_model_row = Adw.EntryRow(title=tr('Model (nazwa na serwerze)'), show_apply_button=True, use_markup=False,
                                          text=settings.model)
        self.llm_model_row.connect('apply', lambda row: self.apply_text(row, 'model', tr('modelu')))
        self.shown = {'model': settings.model, 'whisper_model': settings.whisper_model, 'auth_client_id': settings.auth_client_id}
        self.pull_entry = Adw.EntryRow(title=tr('Pobierz model; zalecany {value} (ok. 6,6 GB)', value=SUGGESTED_MODEL),
                                       show_apply_button=True, use_markup=False)
        self.pull_entry.connect('apply', lambda *_: self.pull_model())
        self.pull_row = Adw.ActionRow(use_markup=False, visible=False)
        self.pull_status = Status()
        self.pull_row.add_prefix(self.pull_status)
        self.pull_bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
        self.pull_row.add_suffix(self.pull_bar)
        self.pull_cancel_button = Gtk.Button(label=tr('Anuluj'), valign=Gtk.Align.CENTER)
        self.pull_cancel_button.connect('clicked', lambda *_: self.cancel_pull())
        self.pull_row.add_suffix(self.pull_cancel_button)
        self.model_try_row = StatusRow(tr('Próba korekty'), self)
        self.model_try_row.set_check(Check(INFO, tr('Wysyła przykładowe zdanie i mierzy czas odpowiedzi.'), 'try_model'))
        self.llm_advanced = Adw.ExpanderRow(title=tr('Zaawansowane'), subtitle=tr('Myślenie modelu i własne prompty.'), use_markup=False)
        self.think_row = Adw.ComboRow(title=tr('Myślenie modelu'), use_markup=False,
                                      subtitle=tr('Wyłączone skraca korektę; dotyczy Ollamy.'),
                                      model=Gtk.StringList.new([tr(label) for _, label in THINKS]))
        self.think_row.connect('notify::selected', lambda *_: self.choice_selected(self.think_row, THINKS, 'llm_think', tr('myślenia modelu')))
        self.prompts_row = StatusRow(tr('Prompty'), self)
        self.llm_advanced.add_row(self.think_row)
        self.llm_advanced.add_row(self.prompts_row)
        for row in (self.llm_where, self.llm_local_entry, self.llm_url_entry, self.llm_api_row, self.llm_key_row, self.llm_status_row,
                    self.model_row, self.llm_model_row, self.pull_entry, self.pull_row, self.model_try_row, self.llm_advanced):
            group.add(row)
        self.set_models(settings.model, None)
        return group

    def key_row(self, field):
        """An API key: typed, applied, then never shown again (settings.json has mode 0600); "Usuń klucz" removes it."""
        row = Adw.PasswordEntryRow(title=tr('Klucz API (opcjonalny)'), show_apply_button=True)
        row.connect('apply', lambda row: self.apply_key(row, field))
        clear = Gtk.Button(label=tr('Usuń klucz'), valign=Gtk.Align.CENTER, visible=False)
        clear.connect('clicked', lambda *_: self.clear_key(field))
        row.add_suffix(clear)
        return row, clear

    # ---- LanguageTool ----
    def build_languagetool(self, settings):
        group = Adw.PreferencesGroup(title='LanguageTool', description=tr('Gramatyka podczas pisania i uzasadnienia zmian; opcjonalny.'))
        self.lt_where = LocationRow(lambda name: self.location_selected('languagetool_location', name))
        self.lt_local_entry = AddressRow(tr('Adres LanguageTool'), settings.languagetool_local_url,
                                         lambda row: self.apply_address(row, 'languagetool_local_url'), default=core.LOCAL_LANGUAGETOOL)
        self.lt_url_entry = AddressRow(tr('Adres serwera'), settings.languagetool_url,
                                       lambda row: self.apply_address(row, 'languagetool_url'))
        self.lt_status_row = StatusRow('LanguageTool', self, check=self.check_services)
        for row in (self.lt_where, self.lt_local_entry, self.lt_url_entry, self.lt_status_row):
            group.add(row)
        return group

    # ---- speech recognition ----
    def build_speech(self, settings):
        group = Adw.PreferencesGroup(title=tr('Rozpoznawanie mowy'), description=tr('Do dyktowania.'))
        self.speech_group = group
        self.speech_where = LocationRow(self.speech_location_selected)
        self.whisper_local_row = StatusRow('Whisper large-v3-turbo', self)
        self.whisper_url_entry = AddressRow(tr('Adres serwera'), settings.whisper_url, lambda row: self.apply_address(row, 'whisper_url'))
        self.whisper_api_row = Adw.ComboRow(title=tr('Protokół'), use_markup=False,
                                            model=Gtk.StringList.new([tr(label) for _, label in SPEECH_PROTOCOLS]))
        self.whisper_api_row.connect('notify::selected', lambda *_: self.choice_selected(
            self.whisper_api_row, SPEECH_PROTOCOLS, 'whisper_api', tr('protokołu serwera mowy')))
        self.whisper_model_row = Adw.EntryRow(title=tr('Model (np. whisper-1)'), show_apply_button=True, use_markup=False,
                                              text=settings.whisper_model)
        self.whisper_model_row.connect('apply', lambda row: self.apply_text(row, 'whisper_model', tr('modelu mowy')))
        self.whisper_key_row, self.clear_whisper_key = self.key_row('whisper_key')
        self.whisper_status_row = StatusRow(tr('Serwer mowy'), self, check=self.check_services)
        for row in (self.speech_where, self.whisper_local_row, self.whisper_url_entry, self.whisper_api_row, self.whisper_model_row,
                    self.whisper_key_row, self.whisper_status_row):
            group.add(row)
        return group

    # ---- remote access ----
    def build_remote(self, settings):
        group = Adw.PreferencesGroup(title=tr('Dostęp zdalny'), description=tr(
            'Opcjonalny: serwery z sieci domowej poza domem, przez bramę z logowaniem OIDC (np. Pocket ID).'))
        self.remote_group = group
        self.remote_setup_row = Adw.ActionRow(title=tr('Nie skonfigurowano'), use_markup=False, subtitle=tr(
            'Adres logowania i identyfikator klienta dostaniesz od administratora serwera.'))
        self.remote_setup_row.add_prefix(Gtk.Image(icon_name='dialog-password-symbolic', css_classes=['dimmed']))
        setup = Gtk.Button(label=tr('Skonfiguruj…'), valign=Gtk.Align.CENTER)
        setup.connect('clicked', lambda *_: self.configure_remote())
        self.remote_setup_row.add_suffix(setup)
        self.remote_setup_row.set_activatable_widget(setup)
        self.login_entry = AddressRow(tr('Adres logowania (np. https://id.example.com)'), settings.auth_issuer,
                                      lambda row: self.apply_remote())
        self.login_client_row = Adw.EntryRow(title=tr('Identyfikator klienta'), show_apply_button=True, use_markup=False,
                                             text=settings.auth_client_id)
        self.login_client_row.connect('apply', lambda *_: self.apply_remote())
        self.login_row = StatusRow(tr('Konto'), self)
        self.login_back_row = Adw.EntryRow(title=tr('Adres, na który wróciła przeglądarka (gramadrama://auth?…)'),
                                           show_apply_button=True, use_markup=False, visible=False)
        self.login_back_row.connect('apply', lambda row: self.login_callback(row.get_text().strip()))
        self.route_row = StatusRow(tr('Połączenie'), self)
        self.remote_off_row = Adw.ButtonRow(title=tr('Wyłącz dostęp zdalny'), css_classes=['destructive-action'])
        self.remote_off_row.connect('activated', lambda *_: self.remote_off())
        for row in (self.remote_setup_row, self.login_entry, self.login_client_row, self.login_row, self.login_back_row,
                    self.route_row, self.remote_off_row):
            group.add(row)
        return group

    # ---- what the file says, on the rows ----
    def refresh_locations(self, settings):
        """The rows of each service for where it runs; texts being typed stay (AddressRow, the entries' saved text)."""
        llm_local = core.located(settings, 'llm') == 'local'
        lt_local = core.located(settings, 'lt') == 'local'
        speech_server = settings.whisper_uses_server
        self.llm_where.set_location('local' if llm_local else 'server', tr('Ollama na tym komputerze; tekst nie opuszcza komputera.')
                                    if llm_local else tr('Ollama albo serwer zgodny z OpenAI.'))
        self.lt_where.set_location('local' if lt_local else 'server', tr('LanguageTool na tym komputerze.') if lt_local
                                   else tr('Serwer LanguageTool pod wpisanym adresem.'))
        self.speech_where.set_location('server' if speech_server else 'local', tr('Serwer zgodny z OpenAI albo whisper.cpp.')
                                       if speech_server else tr('Whisper na tym komputerze; nagranie nie opuszcza komputera.'))
        for row, visible in ((self.llm_local_entry, llm_local), (self.llm_url_entry, not llm_local), (self.llm_api_row, not llm_local),
                             (self.llm_key_row, not llm_local), (self.lt_local_entry, lt_local), (self.lt_url_entry, not lt_local),
                             (self.whisper_local_row, not speech_server), (self.whisper_url_entry, speech_server),
                             (self.whisper_api_row, speech_server), (self.whisper_key_row, speech_server),
                             (self.whisper_status_row, speech_server),
                             (self.whisper_model_row, speech_server and settings.whisper_api != 'whispercpp')):
            row.set_visible(visible)
        self.llm_status_row.set_title(tr('Ollama') if llm_local else tr('Serwer'))
        for row, field in ((self.llm_local_entry, 'llm_local_url'), (self.llm_url_entry, 'llm_url'),
                           (self.lt_local_entry, 'languagetool_local_url'), (self.lt_url_entry, 'languagetool_url'),
                           (self.whisper_url_entry, 'whisper_url'), (self.login_entry, 'auth_issuer')):
            row.set_saved(getattr(settings, field))
            if field in self.settings_problems:
                row.add_css_class('error')
            else:
                row.remove_css_class('error')
        for entry, field in ((self.llm_model_row, 'model'), (self.whisper_model_row, 'whisper_model'),
                             (self.login_client_row, 'auth_client_id')):
            if entry.get_text().strip() in ('', self.shown[field]):  # one being typed stays
                show_text(entry, getattr(settings, field))
            self.shown[field] = getattr(settings, field)
        self.updating = True
        try:
            self.llm_api_row.set_selected([key for key, _ in PROTOCOLS].index(settings.llm_api) if settings.llm_api in dict(PROTOCOLS) else 0)
            self.whisper_api_row.set_selected(1 if settings.whisper_api == 'whispercpp' else 0)
            self.think_row.set_selected([key for key, _ in THINKS].index(settings.llm_think) if settings.llm_think in dict(THINKS) else 0)
        finally:
            self.updating = False
        self.think_row.set_visible(core.model_server(settings)[1] == 'ollama')
        for row, clear, field in ((self.llm_key_row, self.clear_key_button, 'llm_key'),
                                  (self.whisper_key_row, self.clear_whisper_key, 'whisper_key')):
            row.set_title(tr('Klucz API: zapisany (wpisz nowy, żeby go zmienić)') if getattr(settings, field) else tr('Klucz API (opcjonalny)'))
            clear.set_visible(bool(getattr(settings, field)))

    def refresh_model_rows(self, settings):
        """The model's name is picked from the server's list; a server without a list (or not answering) gets it typed. On this
        computer a model is pulled into Ollama while it runs."""
        local = core.located(settings, 'llm') == 'local'
        self.llm_model_row.set_visible(not local and not self.models_known)
        self.model_row.set_visible(local or self.models_known)
        self.pull_entry.set_visible(local and self.llm_ok and self.pull_cancel is None)
        self.pull_row.set_visible(local and self.pull_state is not None)

    # ---- the checks ----
    def check_services(self):
        """"Sprawdź": every service asked again, its route too."""
        core.forget_route()
        self.refresh_server()

    def refresh_server(self):
        self.server_gen += 1
        gen = self.server_gen
        self.refresh_prompts()
        settings = self.load_settings()
        problems = self.settings_problems
        self.refresh_locations(settings)
        self.refresh_remote(settings)
        self.refresh_login(settings)
        # What the engine refuses is shown where it can be corrected; nothing is ever checked with defaults in its place.
        whole = checks.FILE in problems
        probe = {}
        for name, row, fields in (('llm', self.llm_status_row, LLM_FIELDS), ('lt', self.lt_status_row, LT_FIELDS),
                                  ('speech', self.whisper_status_row, SPEECH_FIELDS)):
            bad = next((field for field in fields if field in problems), None)
            if bad is not None:
                row.set_check(checks.settings_check(problems, bad))
            elif whole:
                row.set_check(not_checked())
            probe[name] = bad is None and not whole
        probe['speech'] = probe['speech'] and settings.whisper_uses_server
        if not settings.whisper_uses_server:
            self.whisper_status_row.set_check(checks.whisper_unset('local'))
        if not probe['llm']:
            self.model_state = checks.settings_check(problems, 'model') if 'model' in problems else not_checked()
            self.models_known = self.llm_ok = False
            self.set_models(settings.model, None)
        else:
            self.model_state = Check(BUSY, tr('Sprawdzam…'))
            self.set_models(settings.model, self.model_names_of_server if self.models_known else None)
        for name, row in (('llm', self.llm_status_row), ('lt', self.lt_status_row), ('speech', self.whisper_status_row)):
            if probe[name]:
                row.set_check(Check(BUSY, tr('Sprawdzam…')))
        if not any(probe.values()):
            self.route_row.set_check(checks.route_check(settings, {}))
        self.refresh_button.set_sensitive(not any(probe.values()))
        self.update_summary()  # before anything writes the state: the rows and the summary agree in every state written
        self.refresh_model_rows(settings)
        self.refresh_dictation()
        self.refresh_privacy()
        if not any(probe.values()):
            return

        def work():
            try:
                result = checks.probe_server(settings, probe['llm'], probe['lt'], probe['speech'])
            except Exception:
                failed = Check(ERROR, tr('Nie udało się sprawdzić.'))
                result = {'llm': failed if probe['llm'] else None, 'llm_ok': False, 'models': None, 'loaded': None,
                          'lt': failed if probe['lt'] else None, 'whisper': failed if probe['speech'] else None, 'routes': {}}
            GLib.idle_add(self.server_done, gen, settings, problems, result)
        threading.Thread(target=work, name='gramadrama-server', daemon=True).start()

    def server_done(self, gen, settings, problems, result):
        if gen != self.server_gen or self.closed:
            return False
        self.refresh_button.set_sensitive(True)
        self.routes = result['routes']
        self.route_row.set_check(checks.route_check(settings, self.routes))
        if result['lt'] is not None:
            self.lt_status_row.set_check(result['lt'])
        if result['whisper'] is not None:
            self.whisper_status_row.set_check(result['whisper'])
        if result['llm'] is not None:
            self.llm_status_row.set_check(result['llm'])
            self.llm_ok = result['llm_ok']
            self.models_known = result['models'] is not None
            self.model_names_of_server = result['models'] or []
            local = core.located(settings, 'llm') == 'local'
            if 'model' in problems:
                self.model_state = checks.settings_check(problems, 'model')
            elif local and not self.llm_ok:
                self.model_state = Check(INFO, tr('Wybierzesz albo pobierzesz go, gdy Ollama będzie działać.'))
            else:
                self.model_state = checks.model_check(settings.model, result['models'], result['loaded'],
                                                      core.model_server(settings)[1], local=local)
            self.set_models(settings.model, result['models'])
        self.update_summary()
        self.refresh_model_rows(settings)
        self.refresh_login(settings)  # a check that found the login ended (or refreshed it) says so
        self.refresh_dictation()
        return False

    def set_models(self, current, models):
        """The model list from the server; a valid saved model always stays in it (marked when the
        server does not have it), selected. A refused one is not listed: the list starts with a
        placeholder (a combo row cannot show no selection), and picking a model saves it."""
        valid = bool(current.strip()) and 'model' not in self.settings_problems
        names = list(models or [])
        known = valid and checks.model_index(names, current) >= 0
        if valid and not known:
            names.append(current)
        labels = checks.model_labels(names)
        if valid and not known and models is not None:
            labels[-1] += tr(' (brak na tym komputerze)') if self.llm_where.location() == 'local' else tr(' (brak na serwerze)')
        if not valid:
            names, labels = [None] + names, [tr('— wybierz model —')] + labels
        self.updating = True
        try:
            if names != self.model_names:
                self.model_list.splice(0, self.model_list.get_n_items(), labels)
                self.model_names = names
            self.model_row.set_selected(checks.model_index(names, current) if valid else 0)
        finally:
            self.updating = False
        self.model_row.set_tooltip_text(current or None)
        self.model_row.set_subtitle(self.model_state.text)
        self.model_status.set_state(self.model_state.state)

    # ---- saving ----
    def saved(self, what, backup):
        if backup is not None:
            self.toast(tr('Zapisano. Poprzedni, nieprawidłowy plik ustawień jest w {value}.', value=backup.name))
        else:
            self.toast(tr('Zapisano. Następna korekta użyje {value}.', value=what))

    def services_changed(self):
        """After a change of a service: its route is asked again, and every tab that names it follows."""
        core.forget_route()
        self.refresh_server()
        self.refresh_live()

    def location_selected(self, field, name):
        settings = self.load_settings()
        if getattr(settings, field) == name and field not in self.settings_problems:
            return
        error, backup = checks.save_setting(field, name)
        if error:
            self.toast(error)
        else:
            self.saved(tr('wybranej usługi'), backup)
        self.services_changed()

    def speech_location_selected(self, name):
        if self.load_settings().whisper_backend == name:
            return
        error, backup = checks.save_setting('whisper_backend', name)
        if error:
            self.toast(error)
        elif backup is not None:
            self.saved(tr('rozpoznawania mowy'), backup)
        else:
            self.toast(tr('Zapisano. Zmiana dotyczy następnego dyktowania.'))
        self.services_changed()

    def apply_address(self, row, field):
        value = row.value()
        error, backup = checks.save_setting(field, value)
        if error:
            row.add_css_class('error')
            self.toast(error)
            return
        row.remove_css_class('error')
        row.saved = ''  # the entry shows what the file says now
        row.set_saved(value)
        self.saved(tr('nowego adresu'), backup)
        self.services_changed()

    def choice_selected(self, row, choices, field, what):
        if self.updating:
            return
        index = row.get_selected()
        if not 0 <= index < len(choices):
            return
        key = choices[index][0]
        if key == getattr(self.load_settings(), field) and field not in self.settings_problems:
            return
        error, backup = checks.save_setting(field, key)
        if error:
            self.toast(error)
        else:
            self.saved(what, backup)
        self.services_changed()

    def apply_text(self, row, field, what):
        """A text of settings.json from its entry (a model's name): saved, shown as saved."""
        value = row.get_text().strip()
        error, backup = checks.save_setting(field, value)
        if error:
            row.add_css_class('error')
            self.toast(error)
            return
        row.remove_css_class('error')
        show_text(row, value)
        self.shown[field] = value
        self.saved(what, backup)
        self.refresh_server()
        self.refresh_live()

    def apply_key(self, row, field):
        """A key goes to settings.json (mode 0600) and leaves the screen: it is never shown again."""
        value = row.get_text().strip()
        row.set_text('')
        if not value:
            return  # removing is its own button
        error, backup = checks.save_setting(field, value)
        self.toast(error or tr('Zapisano klucz API.'))
        self.refresh_server()

    def clear_key(self, field):
        error, backup = checks.save_setting(field, '')
        self.toast(error or tr('Usunięto klucz API.'))
        self.refresh_server()

    def model_selected(self, *_):
        index = self.model_row.get_selected()
        if self.updating or not 0 <= index < len(self.model_names):
            return
        name = self.model_names[index]
        if name is None:
            return  # the placeholder
        settings = self.load_settings()
        if name == settings.model and not self.settings_problems:
            return
        error, backup = checks.save_setting('model', name)
        if error:
            self.toast(error)
        else:
            self.saved(tr('modelu {value}', value=checks.model_label(name)), backup)
        self.refresh_server()
        self.refresh_live()

    # ---- pulling a model into Ollama on this computer ----
    def pull_model(self):
        if self.pull_cancel is not None:
            return
        name = self.pull_entry.get_text().strip() or SUGGESTED_MODEL
        settings = self.load_settings()
        base = core.service_base(settings, 'llm')
        cancel = threading.Event()
        self.pull_cancel = cancel
        self.pull_state = (name, 0, 0, None, False)
        self.show_pull()

        def progress(completed, total):
            GLib.idle_add(self.pull_progress, cancel, name, completed, total)

        def work():
            try:
                checks.ollama_pull(base, name, progress, cancel)
                error = None
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            GLib.idle_add(self.pull_done, cancel, name, error)
        threading.Thread(target=work, name='gramadrama-pull', daemon=True).start()

    def pull_progress(self, cancel, name, completed, total):
        if cancel is self.pull_cancel:
            self.pull_state = (name, completed, total, None, False)
            self.show_pull()
        return False

    def pull_done(self, cancel, name, error):
        if cancel is not self.pull_cancel:
            return False
        self.pull_cancel = None
        if cancel.is_set():
            self.pull_state = None
        else:
            self.pull_state = (name, 0, 0, error, error is None)
        self.show_pull()
        if error is None and not cancel.is_set():
            self.pull_entry.set_text('')
            settings = self.load_settings()
            if checks.model_index(self.model_names_of_server if self.models_known else [], settings.model) < 0:
                checks.save_setting('model', name)  # a pulled model is used at once when the chosen one is not here
            self.refresh_server()
        return False

    def cancel_pull(self):
        if self.pull_cancel is not None:
            self.pull_cancel.set()
            self.pull_cancel = None
            self.pull_state = None
            self.show_pull()

    def show_pull(self):
        state = self.pull_state
        if state is not None:
            name, completed, total, error, done = state
            if error:
                self.pull_row.set_title(tr('Nie pobrano {value}', value=name))
                self.pull_row.set_subtitle(error)
                self.pull_status.set_state(ERROR)
            elif done:
                self.pull_row.set_title(tr('Pobrano {value}.', value=name))
                self.pull_row.set_subtitle('')
                self.pull_status.set_state(OK)
            else:
                self.pull_row.set_title(tr('Pobieram {value}', value=name))
                self.pull_row.set_subtitle(tr('{value} z {value2}', value=size(completed), value2=size(total)) if total else tr('Przygotowuję…'))
                self.pull_status.set_state(BUSY)
                self.pull_bar.set_fraction(completed / total if total else 0)
            self.pull_bar.set_visible(not error and not done)
            self.pull_cancel_button.set_visible(not error and not done)
        self.refresh_model_rows(self.load_settings())
        self.write_state()

    # ---- the test of the model ----
    def try_model(self):
        if self.model_cancel is not None:
            return
        settings = self.load_settings()
        if self.settings_problems:  # the engine refuses every correction until they are fixed
            self.model_try_row.set_check(Check(ERROR, tr('Najpierw popraw ustawienia.'), 'try_model'))
            return
        cancel = threading.Event()
        self.model_cancel = cancel
        self.model_try_row.set_check(Check(BUSY, tr('Czekam na model… Wczytanie modelu może potrwać kilkanaście sekund.'), 'cancel_model'))
        self.update_summary()

        def work():
            try:
                outcome = checks.try_model(settings, cancel)
            except Exception as exc:
                outcome = exc
            GLib.idle_add(self.model_done, cancel, outcome)
        threading.Thread(target=work, name='gramadrama-model', daemon=True).start()

    def cancel_model(self):
        """At once: a request still waiting for the server's first byte cannot be interrupted, so its
        late answer is ignored (model_done) instead of keeping the button busy."""
        if self.model_cancel is None:
            return
        self.model_cancel.set()
        self.model_cancel = None
        self.model_try_row.set_check(Check(INFO, tr('Przerwano.'), 'try_model'))
        self.update_summary()

    def model_done(self, cancel, outcome):
        if self.model_cancel is cancel:
            self.model_cancel = None
        if self.closed or cancel.is_set():
            return False
        check = checks.model_try_check(outcome)
        self.model_try_row.set_check(Check(check.state, check.text, 'try_model'))
        self.update_summary()
        if check.state == OK:
            self.refresh_server()  # the model is in memory now
        return False

    # ---- the prompts (docs/prompts.md) ----
    def refresh_prompts(self):
        from .. import prompts
        self.prompts_row.set_check(checks.prompts_check(prompts.Prompts.load()))
        self.write_state()

    def open_prompts(self):
        """The prompts folder: the defaults are written there first as files to edit, none over a file that is there, and
        the folder opens."""
        from .. import paths, prompts
        try:
            written = prompts.export()
        except OSError as exc:
            self.toast(tr('Nie udało się zapisać promptów: {value}', value=exc.strerror or exc))
            return
        if written:
            self.toast(tr('Zapisano {value:d} plików z domyślnymi promptami. Zmień ten, który chcesz; usunięcie pliku przywraca domyślny.', value=len(written)))
        self.refresh_prompts()

        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self.toast(tr('Nie udało się otworzyć {value}.', value=paths.prompts_dir()))
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(paths.prompts_dir()))).launch(self, None, done)

    # ---- the remote access (oidc.py, docs/remote-access.md) ----
    def refresh_remote(self, settings):
        """Folded away until it is set up: "Skonfiguruj…" opens the form; the account and the connection once it is set."""
        typed = bool(settings.auth_issuer.strip() or settings.auth_client_id.strip())
        form = typed or self.remote_expanded
        configured = oidc.configured(settings)
        self.remote_setup_row.set_visible(not form)
        for row in (self.login_entry, self.login_client_row):
            row.set_visible(form)
        self.remote_off_row.set_visible(typed)
        self.login_row.set_visible(configured)
        self.route_row.set_visible(configured)
        if not configured:
            self.login_back_row.set_visible(False)

    def configure_remote(self):
        self.remote_expanded = True
        self.refresh_remote(self.load_settings())
        self.login_entry.grab_focus()
        self.write_state()

    def apply_remote(self):
        """The login address and the client id are saved together. Another address or client id ends the login: its token
        belongs to that provider (the same values saved again do not)."""
        settings = self.load_settings()
        issuer, client = self.login_entry.get_text().strip(), self.login_client_row.get_text().strip()
        if (issuer, client) == (settings.auth_issuer, settings.auth_client_id) and not self.settings_problems.keys() & {'auth_issuer'}:
            return
        error, backup = checks.save_settings({'auth_issuer': issuer, 'auth_client_id': client})
        if error:
            self.login_entry.add_css_class('error')
            self.toast(error)
            return
        self.login_entry.remove_css_class('error')
        if (settings.auth_issuer, settings.auth_client_id) != ('', '') and oidc.status(settings)[0] == 'in':
            oidc.logout()
        self.shown['auth_client_id'] = client
        self.login_entry.saved = ''
        self.login_entry.set_saved(issuer)
        self.saved(tr('dostępu zdalnego'), backup)
        self.services_changed()

    def remote_off(self):
        """"Wyłącz dostęp zdalny": logged out, the address and the client id removed."""
        oidc.logout()
        error, backup = checks.save_settings({'auth_issuer': '', 'auth_client_id': ''})
        self.login_url = None
        self.remote_expanded = False
        show_text(self.login_entry, '')
        show_text(self.login_client_row, '')
        self.toast(error or tr('Dostęp zdalny wyłączony.'))
        self.services_changed()

    def refresh_login(self, settings=None):
        """The account row: who is logged in, whether a login waits for the browser, and whether a server that does not
        answer here needs it."""
        settings = settings or self.load_settings()
        if 'auth_issuer' in self.settings_problems or checks.FILE in self.settings_problems:
            check = checks.settings_check(self.settings_problems, 'auth_issuer') if 'auth_issuer' in self.settings_problems \
                else not_checked()
        else:
            status, name = oidc.status(settings)
            rows = (self.llm_status_row, self.lt_status_row, self.whisper_status_row)
            relevant = 'offline' if 'offline' in self.routes.values() else \
                'required' if any(row.check.action == 'login' for row in rows) else None
            check = checks.login_check(settings, status, name, pending=self.login_starting or oidc.pending(), relevant=relevant)
        self.login_row.set_check(check)
        self.login_back_row.set_visible(check.state == BUSY and oidc.configured(settings))
        self.write_state()

    def login(self):
        """Open the browser at the provider's login page (a worker looks the page up); the browser comes back to
        gramadrama://auth, which the desktop hands to this app (do_command_line), or the user pastes its address."""
        if self.login_starting or not self.login_ready():
            return
        settings = self.load_settings()
        self.login_starting = True
        self.refresh_login(settings)

        def work():
            try:
                url, error = oidc.start_login(settings), None
            except oidc.AuthError as exc:
                url, error = None, str(exc)
            GLib.idle_add(self.login_started, url, error)
        threading.Thread(target=work, name='gramadrama-login', daemon=True).start()

    def login_ready(self):
        """What is typed in the form is saved as the button is pressed; what is still missing is asked for, in its own
        field. True: log in now."""
        settings = self.load_settings()
        if (self.login_entry.get_text().strip(), self.login_client_row.get_text().strip()) != \
                (settings.auth_issuer, settings.auth_client_id):
            self.apply_remote()
            settings = self.load_settings()
        missing = [(self.login_entry, tr('Wpisz adres logowania (np. https://id.example.com).')) if not settings.auth_issuer.strip() else None,
                   (self.login_client_row, tr('Wpisz identyfikator klienta.')) if not settings.auth_client_id.strip() else None]
        for item in missing:
            if item:
                self.remote_expanded = True
                self.refresh_remote(settings)
                item[0].grab_focus()
                self.toast(item[1])
                return False
        return True

    def login_started(self, url, error):
        self.login_starting = False
        if error:
            self.toast(error)
            self.refresh_login()
            return False
        self.login_url = url  # tests: what the browser was sent to
        self.refresh_login()
        GLib.idle_add(self.focus_back_row)  # scrolled into view too: the page is long
        if STATE_FILE:
            return False  # tests (GRAMADRAMA_APP_STATE): the harness plays the browser; no real one is started

        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self.get_clipboard().set(url)
                self.toast(tr('Nie udało się otworzyć przeglądarki. Adres logowania jest w schowku: wklej go w przeglądarce.'))
        Gtk.UriLauncher.new(url).launch(self, None, done)
        return False

    def focus_back_row(self):
        self.login_back_row.grab_focus()
        self.write_state()
        return False

    def login_callback(self, uri):
        """The address the browser came back to: the login is finished in a worker."""
        def work():
            try:
                name, error = oidc.finish_login(uri), None
            except oidc.AuthError as exc:
                name, error = '', str(exc)
            GLib.idle_add(self.login_finished, name, error)
        threading.Thread(target=work, name='gramadrama-login', daemon=True).start()

    def login_finished(self, name, error):
        self.show_tab('connection')  # the browser may have come back while another tab was open
        if error:
            self.toast(error)
            self.refresh_login()
            return False
        self.login_url = None
        show_text(self.login_back_row, '')
        self.toast(tr('Zalogowano{value}.', value=tr(' jako ') + name if name else ''))
        self.services_changed()
        return False

    def logout(self):
        oidc.logout()
        self.login_url = None
        self.toast(tr('Wylogowano.'))
        self.services_changed()

    def services_state(self):
        """Tests: the Usługi tab as shown."""
        def visible(row):
            return row.get_visible()
        return {'locations': {'llm': self.llm_where.location(), 'lt': self.lt_where.location(), 'speech': self.speech_where.location()},
                'visible': {name: visible(row) for name, row in (
                    ('llm_local', self.llm_local_entry), ('llm_url', self.llm_url_entry), ('llm_api', self.llm_api_row),
                    ('llm_key', self.llm_key_row), ('model', self.model_row), ('model_entry', self.llm_model_row),
                    ('pull', self.pull_entry), ('pull_row', self.pull_row), ('lt_local', self.lt_local_entry),
                    ('lt_url', self.lt_url_entry), ('whisper_local', self.whisper_local_row), ('whisper_url', self.whisper_url_entry),
                    ('whisper_api', self.whisper_api_row), ('whisper_model', self.whisper_model_row),
                    ('whisper_key', self.whisper_key_row), ('whisper_status', self.whisper_status_row),
                    ('remote_setup', self.remote_setup_row), ('login_entry', self.login_entry), ('login_client', self.login_client_row),
                    ('login', self.login_row), ('login_back', self.login_back_row), ('route', self.route_row),
                    ('remote_off', self.remote_off_row), ('think', self.think_row))},
                'llm_status_title': self.llm_status_row.get_title(), 'routes': self.routes,
                'pull': list(self.pull_state) if self.pull_state else None}
