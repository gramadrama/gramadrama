import SwiftUI
import GramaCore
import GramaBackend

/// Pisanie: języki tekstu, to, jak GramaDrama sprawdza tekst, i słownik z regułami. Serwery i modele są w Usługach, tryb ⌥⌘G dla aplikacji —
/// w Aplikacjach.
struct WritingTab: View {
    @ObservedObject private var settings = Settings.shared

    var body: some View {
        languagesSection
        SettingsSection(title: tr("Sprawdzanie podczas pisania"), note: tr("Plakietka w rogu pola pokazuje błędy; kliknij ją, żeby poprawić tekst.")) {
            SettingsCard {
                SettingsRow(mark: .icon("text.badge.checkmark"), title: tr("Sprawdzaj podczas pisania"),
                            subtitle: tr("We wszystkich aplikacjach poza wyłączonymi.")) {
                    RowToggle(isOn: $settings.liveCheckEnabled)
                }
                Divider()
                SettingsRow(mark: .icon(settings.liveModelOnly ? "flask" : "slider.horizontal.3"), title: tr("Sposób sprawdzania"),
                            subtitle: settings.liveModelOnly ? tr("Eksperymentalne: tylko {model}. Wolniej niż słownik i LanguageTool.", ["model": settings.modelDisplayName])
                                                             : tr("Słownik na Macu, potem LanguageTool i model.")) {
                    Picker("", selection: $settings.liveModelOnly) {
                        Text(tr("Standardowy")).tag(false)
                        Text(tr("Tylko model")).tag(true)
                    }
                    .labelsHidden().pickerStyle(.segmented).fixedSize()
                    .disabled(!settings.liveCheckEnabled)
                }
                if !settings.liveModelOnly {
                    Divider()
                    SettingsRow(mark: .icon("textformat.abc"), title: tr("Gramatyka i interpunkcja"),
                                subtitle: tr("LanguageTool, po chwili bez pisania.")) {
                        RowToggle(isOn: $settings.liveLanguageToolEnabled, disabled: !settings.liveCheckEnabled)
                    }
                    Divider()
                    SettingsRow(mark: .icon("sparkles"), title: tr("Trudniejsze błędy"),
                                subtitle: tr("{model}, po dłuższej przerwie.", ["model": settings.modelDisplayName])) {
                        RowToggle(isOn: $settings.liveBielikEnabled, disabled: !settings.liveCheckEnabled)
                    }
                }
                Divider()
                SettingsRow(mark: .icon("underline"), title: tr("Podkreślenia pod słowami"),
                            subtitle: tr("Najedź na słowo, żeby zobaczyć poprawkę.")) {
                    RowToggle(isOn: $settings.liveUnderlinesEnabled, disabled: !settings.liveCheckEnabled)
                }
            }
        }
        DictionarySection()
    }

    // MARK: Języki tekstu

    /// Jeden albo dwa języki (`build_languages` na Linuksie): korekta, sprawdzanie i dyktowanie w nich, tłumaczenie
    /// między nimi. Interfejs aplikacji zostaje po polsku.
    private var languagesSection: some View {
        SettingsSection(title: tr("Języki tekstu"), note: tr("Korekta, sprawdzanie i dyktowanie w jednym albo dwóch językach; tłumaczenie między nimi."),
                        ring: .textLanguages) {
            SettingsCard {
                SettingsRow(mark: .icon("globe"), title: tr("Język główny")) {
                    Picker("", selection: Binding(get: { settings.textLanguages.primary }, set: selectPrimary)) {
                        ForEach(TextLanguage.supported, id: \.self) { Text($0.label).tag($0) }
                    }
                    .labelsHidden().fixedSize()
                }
                Divider()
                SettingsRow(mark: .icon("character.bubble"), title: tr("Drugi język"), subtitle: secondarySubtitle) {
                    Picker("", selection: Binding(get: { settings.textLanguages.secondary }, set: selectSecondary)) {
                        Text(tr("Brak — jeden język")).tag(TextLanguage?.none)
                        ForEach(TextLanguage.supported.filter { $0 != settings.textLanguages.primary }, id: \.self) {
                            Text($0.label).tag(TextLanguage?.some($0))
                        }
                    }
                    .labelsHidden().fixedSize()
                }
            }
        }
    }

    private var secondarySubtitle: String? {
        var parts: [String] = []
        if !settings.textLanguages.translates { parts.append(tr("Tłumaczenie wyłączone.")) }
        // Własne pliki promptów zostają nad profilem języków — mogą nie pasować do nowego wyboru.
        if !PromptFiles.current().overridden.isEmpty { parts.append(tr("Masz własne prompty — sprawdź, czy pasują do tych języków.")) }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    /// Główny równy drugiemu — zamieniają się miejscami (jak na Linuksie).
    private func selectPrimary(_ primary: TextLanguage) {
        let current = settings.textLanguages
        guard primary != current.primary else { return }
        let secondary = current.secondary == primary ? current.primary : current.secondary
        settings.textLanguages = TextLanguages(primary: primary, secondary: secondary)
    }

    private func selectSecondary(_ secondary: TextLanguage?) {
        guard secondary != settings.textLanguages.secondary else { return }
        settings.textLanguages = TextLanguages(primary: settings.textLanguages.primary, secondary: secondary)
    }
}
