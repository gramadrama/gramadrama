"""Pisanie (WritingTab.swift on the Mac): only how GramaDrama checks text. The languages of the text (Linux: one or two of
Polish, English and German), checking while typing (docs/live-check.md), with one choice of the way it checks,
standard or model only, instead of switches that switch each other, and the user's dictionary with the rules turned off
(dictionary.py; Linux only for now). Servers and models are in Usługi; the mode of the correction shortcut for an app is in
Aplikacje."""
from gi.repository import Adw, GLib, Gtk

from .. import checks, core, dictionary
from ..dictation import parse_vocabulary
from ..i18n import tr
from .common import show_text, toggle_group

TEXT_LANGUAGES = (('pl', 'Polski'), ('en', 'Angielski'), ('de', 'Niemiecki'))
# The ways of checking while typing (Settings.live_model_only), as the Mac's "Sposób sprawdzania".
CHECKING = (('standard', 'Standardowy'), ('model', 'Tylko model'))
# The switches of the standard way: (field, title, subtitle); {model} is the model's name.
LIVE_LAYERS = (('live_languagetool', 'Gramatyka i interpunkcja', 'LanguageTool, po chwili bez pisania.'),
               ('live_bielik', 'Trudniejsze błędy', '{model}, po dłuższej przerwie.'))
MAX_WORDS_SHOWN = 100  # the latest words added to the dictionary in its list; Enchant's lists may hold many more


def expandable(row, enabled):
    """An expander row that can open only with something in it. Adw.ExpanderRow opens itself when its expansion is
    enabled: it stays as the user left it instead (closed at first, open after a word is removed from the open list)."""
    expanded = row.get_expanded()
    row.set_enable_expansion(enabled)
    row.set_expanded(expanded and enabled)


def model_name(settings):
    """The model's name as the windows show it ("Gemma-4-12B"), or "model" when none is chosen."""
    return core.display_name(settings.model) if settings.model.strip() else tr('model')


class WritingTab:
    """The Pisanie tab of the window (a mixin of window.main.Window)."""

    def build_writing(self, settings):
        return [self.build_languages(settings), self.build_live(settings), self.build_dictionary(settings)]

    def build_languages(self, settings):
        group = Adw.PreferencesGroup(title=tr('Języki tekstu'), description=(
            tr('Korekta, sprawdzanie i dyktowanie w jednym albo dwóch językach; tłumaczenie między nimi.')))
        self.languages_group = group
        self.updating_languages = False
        self.primary_language_row = Adw.ComboRow(title=tr('Język główny'), use_markup=False,
                                                 subtitle=tr('Polski włącza polski interfejs, inne — angielski.'),
                                                 model=Gtk.StringList.new([tr(name) for _, name in TEXT_LANGUAGES]))
        self.secondary_language_row = Adw.ComboRow(title=tr('Drugi język'), use_markup=False)
        self.languages_problem = Adw.ActionRow(title=tr('Nieprawidłowe ustawienia języków'), use_markup=False, visible=False)
        self.refresh_languages(settings)
        self.primary_language_row.connect('notify::selected', lambda *_: self.language_selected(True))
        self.secondary_language_row.connect('notify::selected', lambda *_: self.language_selected(False))
        group.add(self.primary_language_row)
        group.add(self.secondary_language_row)
        group.add(self.languages_problem)
        return group

    def refresh_languages(self, settings):
        problems = [self.settings_problems[field] for field in ('primary_language', 'secondary_language')
                    if field in self.settings_problems]
        self.languages_problem.set_visible(bool(problems))
        self.languages_problem.set_subtitle('; '.join(problems))
        self.updating_languages = True
        try:
            primary = settings.languages[0]
            codes = [key for key, _ in TEXT_LANGUAGES]
            self.primary_language_row.set_selected(codes.index(primary))
            self.secondary_choices = [''] + [key for key in codes if key != primary]
            self.secondary_language_row.set_model(Gtk.StringList.new(
                [tr('Brak — jeden język')] + [tr(dict(TEXT_LANGUAGES)[key]) for key in self.secondary_choices[1:]]))
            second = settings.languages[1] if len(settings.languages) > 1 else ''
            self.secondary_language_row.set_selected(self.secondary_choices.index(second))
            self.secondary_language_row.set_subtitle('' if second else tr('Bez tłumaczenia i bez drugiego języka dyktowania.'))
        finally:
            self.updating_languages = False

    def language_selected(self, primary_changed):
        if not self.updating_languages:
            GLib.idle_add(self._language_selected, primary_changed)

    def _language_selected(self, primary_changed):
        settings = self.load_settings()
        primary = TEXT_LANGUAGES[self.primary_language_row.get_selected()][0]
        secondary = self.secondary_choices[self.secondary_language_row.get_selected()]
        if primary_changed and primary == secondary:
            secondary = settings.languages[0]
        if (primary, secondary) == (settings.primary_language, settings.secondary_language):
            return False
        error, backup = checks.save_settings({'primary_language': primary, 'secondary_language': secondary})
        if error:
            self.toast(error)
        else:
            self.saved(tr('wybranych języków'), backup)
        settings = self.load_settings()
        if self.rebuild_language(settings):
            return False
        self.refresh_languages(settings)
        self.refresh_dictation_languages(settings)
        self.refresh_shortcuts(settings)
        self.refresh_system()
        self.write_state()
        return False

    # ---- checking while typing ----
    def build_live(self, settings):
        group = Adw.PreferencesGroup(title=tr('Sprawdzanie podczas pisania'),
                                     description=tr('Plakietka przy polu pokazuje błędy; kliknij ją, żeby poprawić tekst.'))
        self.live_rows = {}
        live = Adw.SwitchRow(title=tr('Sprawdzaj podczas pisania'), use_markup=False, active=settings.live_check is not False)
        live.connect('notify::active', lambda row, _p: self.live_switched('live_check', row.get_active()))
        self.live_rows['live_check'] = live
        group.add(live)
        self.checking_row = Adw.ActionRow(title=tr('Sposób sprawdzania'), use_markup=False)
        self.checking_icon = Gtk.Image(css_classes=['dimmed'])
        self.checking_row.add_prefix(self.checking_icon)
        self.checking = toggle_group(CHECKING)
        self.checking.set_active_name('model' if settings.live_model_only is True else 'standard')
        self.checking.connect('notify::active-name', lambda *_: self.updating_live or self.live_switched(
            'live_model_only', self.checking.get_active_name() == 'model'))
        self.checking_row.add_suffix(self.checking)
        group.add(self.checking_row)
        for field, title, _subtitle in LIVE_LAYERS:
            row = Adw.SwitchRow(title=tr(title), use_markup=False, active=getattr(settings, field) is not False)
            row.connect('notify::active', lambda row, _p, field=field: self.updating_live or self.live_switched(field, row.get_active()))
            self.live_rows[field] = row
            group.add(row)
        underlines = Adw.SwitchRow(title=tr('Podkreślenia pod słowami'), use_markup=False,
                                   subtitle=tr('Najedź na słowo, żeby zobaczyć poprawkę.'),
                                   active=settings.live_underlines is not False)
        underlines.connect('notify::active', lambda row, _p: self.updating_live or self.live_switched('live_underlines', row.get_active()))
        self.live_rows['live_underlines'] = underlines
        group.add(underlines)
        self.updating_live = False
        self.refresh_live(settings)
        return group

    def refresh_live(self, settings=None):
        """The rows follow the switch and the way of checking: the layers of the standard way only with it, nothing while
        checking is off. The model's name is the one chosen in Usługi."""
        settings = settings or self.load_settings()
        on = self.live_rows['live_check'].get_active()
        model_only = self.checking.get_active_name() == 'model'
        name = model_name(settings)
        self.live_rows['live_check'].set_subtitle(tr('We wszystkich aplikacjach poza wyłączonymi.') if on else
                                                  tr('Wyłączone — błędy tylko po skrócie korekty.'))
        self.checking_row.set_subtitle(tr('Eksperymentalne: tylko {value}. Wolniej niż słownik i LanguageTool.', value=name)
                                       if model_only else tr('Słownik na tym komputerze, potem LanguageTool i model.'))
        self.checking_icon.set_from_icon_name('applications-science-symbolic' if model_only else 'view-list-bullet-symbolic')
        self.checking.set_sensitive(on)
        for field, _title, subtitle in LIVE_LAYERS:
            row = self.live_rows[field]
            row.set_visible(not model_only)
            row.set_sensitive(on)
            row.set_subtitle(tr(subtitle, model=name) if '{model}' in subtitle else tr(subtitle))
        self.live_rows['live_underlines'].set_sensitive(on)
        self.write_state()

    def live_switched(self, field, value):
        error, backup = checks.save_setting(field, value)
        if error:
            self.toast(error)
        elif backup is not None:
            self.saved(tr('nowego ustawienia'), backup)
        else:
            self.toast(tr('Zapisano. Sprawdzanie podczas pisania użyje nowego ustawienia.'))
        self.refresh_live()
        self.refresh_privacy()  # what goes where follows the layers

    def live_state(self):
        """Tests: the switches and the way of checking as shown."""
        return {'checking': self.checking.get_active_name(), 'checking_sensitive': self.checking.get_sensitive(),
                'rows': {field: {'active': row.get_active(), 'visible': row.get_visible(), 'sensitive': row.get_sensitive(),
                                 'subtitle': row.get_subtitle()} for field, row in self.live_rows.items()}}

    # ---- the dictionary and the rules turned off ----
    def build_dictionary(self, settings):
        group = Adw.PreferencesGroup(title=tr('Słownik i reguły'), description=tr(
            'Twoich słów sprawdzanie nie podkreśla, korekta zostawia ich pisownię, a dyktowanie lepiej je rozpoznaje.'))
        self.vocabulary_row = Adw.EntryRow(title=tr('Własne słowa: imiona, nazwy i żargon, po przecinku'), use_markup=False,
                                           text=settings.dictation_vocabulary if isinstance(settings.dictation_vocabulary, str) else '',
                                           show_apply_button=True)
        self.vocabulary_row.connect('apply', self.vocabulary_applied)
        self.learned_row = Adw.ExpanderRow(title=tr('Dodane do słownika'), use_markup=False)
        self.learned_row.add_prefix(Gtk.Image(icon_name='accessories-dictionary-symbolic', css_classes=['dimmed']))
        self.rules_row = Adw.ExpanderRow(title=tr('Wyłączone reguły'), use_markup=False)
        self.rules_row.add_prefix(Gtk.Image(icon_name='view-conceal-symbolic', css_classes=['dimmed']))
        self.dictionary_rows, self.dictionary_shown = [], None
        self.dictionary_buttons = {'forget': [], 'rule_on': []}  # tests: the buttons of the rows, in their order
        for row in (self.vocabulary_row, self.learned_row, self.rules_row):
            group.add(row)
        self.refresh_dictionary(settings)
        return group

    def refresh_dictionary(self, settings=None):
        """The words added with "Dodaj do słownika" (Enchant's lists, also from the GNOME apps), the latest first, each
        with a button that removes it, and the rules turned off from the underline card, each with Włącz. Built again only
        when they changed: an open list keeps its place."""
        settings = settings or self.load_settings()
        words, rules = dictionary.learned_words(settings.languages), dictionary.rules_off()
        shown = (tuple(words[:MAX_WORDS_SHOWN]), len(words), tuple(rules.items()), settings.languages)
        if shown == self.dictionary_shown:
            return
        self.dictionary_shown = shown
        for expander, row in self.dictionary_rows:
            expander.remove(row)
        self.dictionary_rows, self.dictionary_buttons = [], {'forget': [], 'rule_on': []}
        for word in words[:MAX_WORDS_SHOWN]:
            row = Adw.ActionRow(title=word, use_markup=False)
            button = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.CENTER, css_classes=['flat'],
                                tooltip_text=tr('Usuń ze słownika'))
            button.update_property([Gtk.AccessibleProperty.LABEL], [tr('Usuń ze słownika: {value}', value=word)])
            button.connect('clicked', lambda _button, word=word: self.forget_word(word))
            row.add_suffix(button)
            self.dictionary_buttons['forget'].append(button)
            self.learned_row.add_row(row)
            self.dictionary_rows.append((self.learned_row, row))
        if len(words) > MAX_WORDS_SHOWN:
            more = Adw.ActionRow(title=tr('Starszych słów: {count}', count=len(words) - MAX_WORDS_SHOWN), use_markup=False,
                                 css_classes=['dim-label'])
            self.learned_row.add_row(more)
            self.dictionary_rows.append((self.learned_row, more))
        self.learned_row.set_subtitle(tr('Słów: {count}. Wspólne z aplikacjami GNOME, które używają słownika systemu.',
                                         count=len(words)) if words else
                                      tr('Brak. Słowo dodasz kartą pod podkreślonym słowem albo w panelu korekty.'))
        expandable(self.learned_row, bool(words))
        for rule, name in rules.items():
            row = Adw.ActionRow(title=name or rule, subtitle=rule if name else '', use_markup=False)
            button = Gtk.Button(label=tr('Włącz'), valign=Gtk.Align.CENTER)
            button.update_property([Gtk.AccessibleProperty.LABEL], [tr('Włącz regułę: {value}', value=name or rule)])
            button.connect('clicked', lambda _button, rule=rule: self.turn_rule_on(rule))
            row.add_suffix(button)
            self.dictionary_buttons['rule_on'].append(button)
            self.rules_row.add_row(row)
            self.dictionary_rows.append((self.rules_row, row))
        self.rules_row.set_subtitle(tr('Reguł: {count}. LanguageTool ich nie sprawdza.', count=len(rules)) if rules else
                                    tr('Brak. Regułę LanguageTool wyłączysz kartą pod podkreślonym słowem.'))
        expandable(self.rules_row, bool(rules))
        self.write_state()

    def vocabulary_applied(self, row):
        words = ', '.join(parse_vocabulary(row.get_text()))
        show_text(row, words)
        error, backup = checks.save_setting('dictation_vocabulary', words)
        if error:
            self.toast(error)
        elif backup is not None:
            self.saved(tr('tych słów'), backup)
        else:
            self.toast(tr('Zapisano. Sprawdzanie, korekta i dyktowanie użyją tych słów.'))
        self.refresh_privacy()

    def forget_word(self, word):
        error = dictionary.forget(word, self.load_settings().languages)
        self.toast(error or tr('Usunięto ze słownika: {value}', value=word))
        self.refresh_dictionary()

    def turn_rule_on(self, rule):
        error = dictionary.turn_on_rule(rule)
        self.toast(error or tr('Włączono regułę. Sprawdzanie znów ją pokaże.'))
        self.refresh_dictionary()

    def dictionary_state(self):
        """Tests: the dictionary's rows as shown."""
        rows = {}
        for expander, row in self.dictionary_rows:
            rows.setdefault('words' if expander is self.learned_row else 'rules', []).append(row.get_title())
        return {'vocabulary': self.vocabulary_row.get_text(), 'words': rows.get('words', []), 'rules': rows.get('rules', []),
                'words_subtitle': self.learned_row.get_subtitle(), 'rules_subtitle': self.rules_row.get_subtitle()}
