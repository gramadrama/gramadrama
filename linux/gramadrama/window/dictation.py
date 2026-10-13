"""Dyktowanie (DictationTab.swift on the Mac): only how you dictate (docs/dictation.md). The right Ctrl held, the language
and the model's cleanup; where speech is recognized (this computer or a server) is set in Usługi, whose group the first
row's button rings. The own words, which help it recognize names, are in the dictionary of Pisanie, as on the Mac, whose
section the last row's button rings."""
from gi.repository import Adw, Gtk

from .. import checks, core, paths
from ..checks import BUSY, ERROR, WARNING, Check
from ..dictation import alternate_language
from ..i18n import tr
from .common import StatusRow
from .writing import TEXT_LANGUAGES

CLEANUPS = (('off', 'nigdy'), ('auto', 'gdy trzeba'), ('always', 'zawsze'))
# "dictate in …" for the Shift row
IN_LANGUAGE = {'pl': 'po polsku', 'en': 'po angielsku', 'de': 'po niemiecku'}


class DictationTab:
    """The Dyktowanie tab of the window (a mixin of window.main.Window)."""

    def build_dictation(self, settings):
        group = Adw.PreferencesGroup(title=tr('Dyktowanie'),
                                     description=tr('Trzymaj prawy Ctrl, mów i puść — tekst pojawi się w miejscu kursora.'))
        self.dictation_group = group
        self.updating_dictation_language = False
        self.dictation_choices = list(settings.languages) + (['auto'] if len(settings.languages) > 1 else [])
        self.dictation_switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=settings.dictation_enabled is not False)
        self.dictation_switch.update_property([Gtk.AccessibleProperty.LABEL], [tr('Dyktowanie')])
        self.dictation_switch.connect('notify::active', self.dictation_switched)
        services = Gtk.Button(label=tr('Usługi…'), valign=Gtk.Align.CENTER,
                              tooltip_text=tr('Rozpoznawanie mowy ustawisz w zakładce Usługi'))
        services.connect('clicked', lambda *_: self.point_to('speech'))
        self.dictation_services_button = services
        suffix = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        suffix.append(services)
        suffix.append(self.dictation_switch)
        self.dictation_row = StatusRow(tr('Dyktowanie'), self, suffix=suffix)
        self.dictation_key_row = Adw.ActionRow(title=tr('Klawisz'), use_markup=False)
        self.dictation_key_row.add_prefix(Gtk.Image(icon_name='input-keyboard-symbolic', css_classes=['dimmed']))
        self.dictation_key_row.add_suffix(Gtk.Label(label=tr('prawy Ctrl'), css_classes=['heading'], valign=Gtk.Align.CENTER))
        self.language_row = Adw.ComboRow(title=tr('Język mowy'), use_markup=False)
        self.refresh_dictation_languages(settings)
        self.language_row.connect('notify::selected', lambda row, _p:
                                  self.dictation_choice(row, self.dictation_choices, 'dictation_language'))
        self.cleanup_row = Adw.ComboRow(title=tr('Porządkowanie modelem'), use_markup=False, subtitle=(
            tr('Eksperymentalne: usuwa wypełniacze i poprawki w trakcie mówienia; wydłuża czekanie.')),
            model=Gtk.StringList.new([tr(label) for _, label in CLEANUPS]))
        keys = [key for key, _ in CLEANUPS]
        self.cleanup_row.set_selected(keys.index(settings.dictation_cleanup) if settings.dictation_cleanup in keys else 0)
        self.cleanup_row.connect('notify::selected', lambda row, _p: self.dictation_choice(row, keys, 'dictation_cleanup'))
        writing = Gtk.Button(label=tr('Pisanie…'), valign=Gtk.Align.CENTER,
                             tooltip_text=tr('Własne słowa i słowa dodane do słownika ustawisz w zakładce Pisanie'))
        writing.connect('clicked', lambda *_: self.point_to('dictionary'))
        self.dictation_words_button = writing
        self.dictation_words_row = Adw.ActionRow(title=tr('Słownik'), use_markup=False, subtitle=(
            tr('Własne słowa i dodane do słownika pomagają rozpoznać imiona, nazwy i żargon.')))
        self.dictation_words_row.add_prefix(Gtk.Image(icon_name='accessories-dictionary-symbolic', css_classes=['dimmed']))
        self.dictation_words_row.add_suffix(writing)
        for row in (self.dictation_row, self.dictation_key_row, self.language_row, self.cleanup_row, self.dictation_words_row):
            group.add(row)
        return group

    def refresh_dictation_languages(self, settings):
        self.updating_dictation_language = True
        try:
            self.dictation_choices = list(settings.languages) + (['auto'] if len(settings.languages) > 1 else [])
            labels = dict(TEXT_LANGUAGES, auto=tr('Automatycznie'))
            self.language_row.set_model(Gtk.StringList.new([tr(labels[key]) for key in self.dictation_choices]))
            language = settings.dictation_language if settings.dictation_language in self.dictation_choices else settings.languages[0]
            self.language_row.set_selected(self.dictation_choices.index(language))
            if len(settings.languages) > 1:
                other = alternate_language(language, settings.languages)
                self.language_row.set_subtitle(tr('Z Shiftem dyktujesz {value}.', value=tr(IN_LANGUAGE[other])))
                self.dictation_key_row.set_subtitle(tr('Z Shiftem — drugi język.'))
            else:
                self.language_row.set_subtitle(tr('Jeden język tekstu; drugi dodasz w zakładce Pisanie.'))
                self.dictation_key_row.set_subtitle('')
        finally:
            self.updating_dictation_language = False

    def refresh_dictation(self):
        """The first row says what recognizes speech (this computer's Whisper, or the server, whose check it shares while
        that one is busy or wrong); the other rows follow the switch."""
        settings = self.load_settings()
        installed = paths.speech_python().exists() and (paths.whisper_model() / 'model.bin').exists()
        server = core.speech_server(settings)
        on = self.dictation_switch.get_active()
        check = checks.dictation_check(on, installed, self.events, server, settings.whisper_backend)
        if on and settings.whisper_uses_server and server and self.whisper_status_row.check.state in (BUSY, ERROR, WARNING):
            check = self.whisper_status_row.check
        # The fixes (the speech tools, the login) are offered in Usługi, which the row's button opens.
        self.dictation_row.set_check(Check(check.state, check.text))
        self.whisper_local_row.set_check(checks.dictation_check(True, installed, self.events, '', 'local', services=True))
        for row in (self.dictation_key_row, self.language_row, self.cleanup_row):
            row.set_sensitive(on)
        self.write_state()

    def save_dictation(self, field, value, what):
        error, backup = checks.save_setting(field, value)
        if error:
            self.toast(error)
        elif backup is not None:
            self.saved(what, backup)
        else:
            self.toast(tr('Zapisano. Następne dyktowanie użyje {value}.', value=what))
        self.refresh_dictation()
        self.refresh_privacy()

    def dictation_switched(self, switch, _pspec):
        self.save_dictation('dictation_enabled', switch.get_active(), tr('nowego ustawienia'))
        self.update_summary()

    def dictation_choice(self, row, keys, field):
        if self.updating_dictation_language:
            return
        index = row.get_selected()
        if 0 <= index < len(keys) and getattr(self.load_settings(), field) != keys[index]:
            self.save_dictation(field, keys[index], tr('nowego ustawienia'))
            if field == 'dictation_language':
                self.refresh_dictation_languages(self.load_settings())
