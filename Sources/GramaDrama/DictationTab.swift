import SwiftUI
import GramaCore
import GramaBackend

/// Dyktowanie: tylko to, jak dyktujesz. Silnik rozpoznawania mowy (Mac albo serwer) ustawia się w Usługach.
struct DictationTab: View {
    @ObservedObject var model: MainWindowModel
    @ObservedObject private var dictation = DictationSettings.shared
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var speech = SpeechModel.shared
    @AppStorage(MainWindowTab.defaultsKey) private var tab: MainWindowTab = .start

    var body: some View {
        SettingsSection(title: tr("Dyktowanie"), note: tr("Trzymaj klawisz, mów i puść — tekst pojawi się w miejscu kursora.")) {
            SettingsCard {
                SettingsRow(mark: .status(dictation.enabled ? recognizerOK : nil), title: tr("Dyktowanie"),
                            subtitle: dictation.enabled ? recognizerText : tr("Wyłączone.")) {
                    HStack(spacing: 8) {
                        // Dwie drogi: model pobrany na ten Mac albo serwer mowy (Usługi), na którym model już jest.
                        if dictation.enabled, dictation.speechBackend == .local, speech.state == .notDownloaded {
                            Button(tr("Pobierz (1,6 GB)")) { Task { try? await speech.prepare() } }
                                .help(tr("Whisper large-v3-turbo na tym Macu; pobiera się tylko po kliknięciu"))
                        }
                        Button(tr("Usługi…")) { tab = .connection }.help(tr("Silnik rozpoznawania mowy ustawisz w zakładce Usługi"))
                        RowToggle(isOn: $dictation.enabled)
                    }
                    .fixedSize()
                }
                Divider()
                keyRow
                Divider()
                SettingsRow(mark: .icon("character.bubble"), title: tr("Język mowy"), subtitle: languageSubtitle) {
                    Picker("", selection: Binding(get: { dictation.language.effective(in: settings.textLanguages) },
                                                  set: { dictation.language = $0 })) {
                        ForEach(DictationLanguage.choices(in: settings.textLanguages)) { Text($0.label(in: settings.textLanguages)).tag($0) }
                    }
                    .labelsHidden().fixedSize()
                }
                Divider()
                SettingsRow(mark: .icon("wand.and.stars"), title: tr("Porządkowanie modelem"),
                            subtitle: tr("Eksperymentalne: usuwa wypełniacze i poprawki w trakcie mówienia; wydłuża czekanie.")) {
                    Picker("", selection: $dictation.cleanup) {
                        ForEach(DictationCleanup.allCases) { Text($0.label).tag($0) }
                    }
                    .labelsHidden().fixedSize()
                }
                Divider()
                // Słownik jest jeden: własne słowa i dodane do słownika ustawiasz w Pisaniu.
                SettingsRow(mark: .icon("character.book.closed"), title: tr("Słownik"),
                            subtitle: tr("Własne słowa i dodane do słownika pomagają rozpoznać imiona, nazwy i żargon.")) {
                    Button(tr("Pisanie…")) { WindowHighlight.shared.point(to: .dictionary) }
                        .help(tr("Własne słowa i słowa dodane do słownika ustawisz w zakładce Pisanie"))
                }
            }
        }
    }

    /// Fn wymaga w macOS „Naciśnij 🌐, aby: Nic nie rób” — inaczej po puszczeniu otworzą się emoji.
    /// Co robi ⇧ — przy jednym języku tekstu nic.
    private var languageSubtitle: String {
        let languages = settings.textLanguages
        guard languages.translates else { return tr("Jeden język tekstu (Pisanie → Języki tekstu) — ⇧ nic nie zmienia.") }
        let alternate = dictation.language.alternate(in: languages)
        return tr("Z ⇧ dyktujesz {how}.", ["how": alternate.textLanguage?.adverb ?? tr("w języku rozpoznanym automatycznie")])
    }

    private var keyRow: some View {
        let globeMisconfigured = dictation.key == .fn && !DictationSettings.globeKeyDoesNothing
        return SettingsRow(mark: globeMisconfigured ? .status(false) : .icon("keyboard"), title: tr("Klawisz"),
                           subtitle: globeMisconfigured ? tr("W Ustawieniach systemowych → Klawiatura ustaw „Naciśnij 🌐: Nic nie rób”.")
                               : dictation.key == .rightOption ? tr("Startuje po chwili, żeby ⌥ z literą działało jak zwykle.") : nil) {
            HStack(spacing: 8) {
                if globeMisconfigured {
                    Button(tr("Otwórz")) {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension")!)
                    }
                }
                Picker("", selection: $dictation.key) {
                    ForEach(DictationKey.allCases) { Text($0.label).tag($0) }
                }
                .labelsHidden().fixedSize()
            }
        }
    }

    private var recognizerOK: Bool? {
        switch dictation.speechBackend {
        case .local:
            switch speech.state {
            case .ready: return true
            case .failed: return false
            default: return nil
            }
        case .server:
            guard dictation.speechEndpoint != nil else { return false }
            switch model.speechStatus {
            case .ok?: return true
            case .error?: return false
            default: return nil
            }
        }
    }

    private var recognizerText: String {
        switch dictation.speechBackend {
        case .local:
            switch speech.state {
            case .ready: return tr("Rozpoznaje Whisper na tym Macu.")
            case .notDownloaded: return tr("Whisper na tym Macu — pobierz model mowy (1,6 GB) albo wybierz serwer mowy w Usługach.")
            case .notLoaded: return tr("Whisper na tym Macu — wczyta się przy pierwszym dyktowaniu.")
            default: return speech.state.label
            }
        case .server:
            guard let endpoint = dictation.speechEndpoint else { return tr("Ustaw serwer mowy w zakładce Usługi.") }
            return tr("Rozpoznaje serwer mowy {host}.", ["host": endpoint.host])
        }
    }
}
