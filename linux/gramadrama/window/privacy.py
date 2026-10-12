"""Prywatność (PrivacyTab.swift on the Mac): what goes where, from the current settings, instead of a general description
of everything that is possible. The texts are pure functions of the settings (privacy_rows), so tests need no desktop."""
import urllib.parse

from gi.repository import Adw, Gtk

from .. import core
from ..i18n import tr


def host(url):
    return urllib.parse.urlsplit(url).netloc or url


def model_place(settings):
    """"tym komputerze (Ollama)", "serwerze host" or "serwerze za bramą host": where the text for the model goes."""
    if core.located(settings, 'llm') == 'local':
        return tr('tym komputerze (Ollama)')
    if settings.llm_url.strip():
        return tr('serwerze {value}', value=host(settings.llm_url.strip()))
    if core.gateway(settings):
        return tr('serwerze za bramą {value}', value=host(core.gateway(settings)))
    return tr('serwerze z ustawień')


def languagetool_place(settings):
    if core.located(settings, 'lt') == 'local':
        return tr('tym komputerze')
    url = core.languagetool_server(settings)
    return tr('serwerze {value}', value=host(url)) if url else tr('serwerze z ustawień')


def privacy_rows(settings):
    """(icon, title, text) of each row, as the settings are now."""
    rows = [('document-edit-symbolic', tr('Korekta i tłumaczenie'), tr('Pobrany tekst trafia do modelu na {value}.', value=model_place(settings)))]
    if settings.live_check is False:
        live = tr('Wyłączone.')
    elif settings.live_model_only is True:
        live = tr('Po przerwie w pisaniu tekst pola trafia do modelu na {value}.', value=model_place(settings))
    else:
        languagetool, model = settings.live_languagetool is not False, settings.live_bielik is not False
        here = ((not languagetool or core.located(settings, 'lt') == 'local')
                and (not model or core.located(settings, 'llm') == 'local'))
        targets = ([tr('LanguageTool na {value}', value=languagetool_place(settings))] if languagetool else []) + \
                  ([tr('modelu na {value}', value=model_place(settings))] if model else [])
        if not targets:
            live = tr('Tylko słownik na tym komputerze.')
        elif here:
            live = tr('Wszystko na tym komputerze — tekst nie opuszcza komputera.')
        else:
            live = tr('Słownik na tym komputerze; po przerwie tekst pola trafia do {value}.', value=tr(' i ').join(targets))
    rows.append(('view-list-bullet-symbolic', tr('Sprawdzanie podczas pisania'), live))
    if settings.dictation_enabled is False:
        dictation = tr('Wyłączone.')
    else:
        server = core.speech_server(settings)
        dictation = (tr('Nagranie (WAV) i podpowiedź słów ze słownika trafiają do {value}; nagranie nie jest zapisywane.', value=host(server)) if server
                     else tr('Nagranie rozpoznaje Whisper na tym komputerze i nie jest zapisywane.'))
        if settings.dictation_cleanup in ('auto', 'always'):
            dictation += ' ' + tr('Tekst do porządkowania trafia do modelu na {value}.', value=model_place(settings))
    rows.append(('audio-input-microphone-symbolic', tr('Dyktowanie'), dictation))
    if core.gateway(settings):
        rows.append(('network-server-symbolic', tr('Dostęp zdalny'),
                     tr('Poza domem przez {value}; operator bramy może widzieć przesyłane dane.', value=host(core.gateway(settings)))))
    rows.append(('dialog-password-symbolic', tr('Pola haseł i terminale'),
                 tr('Pola haseł, terminale i wyłączone aplikacje są pomijane.')))
    rows.append(('channel-secure-symbolic', tr('Klucze i logowanie'),
                 tr('Klucze API i token logowania są w plikach dostępnych tylko dla Twojego konta.')))
    return rows


class PrivacyTab:
    """The Prywatność tab of the window (a mixin of window.main.Window)."""

    def build_privacy(self, settings):
        group = Adw.PreferencesGroup(title=tr('Co i dokąd trafia'),
                                     description=tr('Według bieżących ustawień. Aplikacja nie wysyła telemetrii.'))
        self.privacy_group = group
        self.privacy_rows = []
        self.log_row = Adw.ActionRow(title=tr('Log zdarzeń'), subtitle=tr('Zdarzenia, czasy i długości — nigdy tekst.'), use_markup=False)
        self.log_row.add_prefix(Gtk.Image(icon_name='text-x-generic-symbolic', css_classes=['dimmed']))
        show = Gtk.Button(label=tr('Pokaż'), valign=Gtk.Align.CENTER)
        show.connect('clicked', lambda *_: self.open_log())
        self.log_row.add_suffix(show)
        group.add(self.log_row)
        self.refresh_privacy(settings)
        return [group]

    def refresh_privacy(self, settings=None):
        """The rows follow the settings; the log's row stays last."""
        settings = settings or self.load_settings()
        rows = privacy_rows(settings)
        if [(row.icon_name, row.get_title()) for row in self.privacy_rows] != [(icon, title) for icon, title, _ in rows]:
            for row in self.privacy_rows:
                self.privacy_group.remove(row)
            self.privacy_group.remove(self.log_row)
            self.privacy_rows = []
            for icon, title, _ in rows:
                row = Adw.ActionRow(title=title, use_markup=False)
                row.icon_name = icon
                row.add_prefix(Gtk.Image(icon_name=icon, css_classes=['dimmed']))
                self.privacy_group.add(row)
                self.privacy_rows.append(row)
            self.privacy_group.add(self.log_row)
        for row, (_, _, text) in zip(self.privacy_rows, rows):
            row.set_subtitle(text)
        self.write_state()

    def privacy_state(self):
        """Tests: the rows as shown."""
        return [(row.get_title(), row.get_subtitle()) for row in self.privacy_rows]
