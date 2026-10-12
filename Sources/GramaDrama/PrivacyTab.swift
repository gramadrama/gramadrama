import SwiftUI
import GramaCore
import GramaBackend

/// Prywatność: co i dokąd trafia — z bieżących ustawień, zamiast ogólnego opisu wszystkich możliwości.
struct PrivacyTab: View {
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var dictation = DictationSettings.shared

    var body: some View {
        SettingsSection(title: tr("Co i dokąd trafia"), note: tr("Według bieżących ustawień. Aplikacja nie wysyła telemetrii.")) {
            SettingsCard {
                SettingsRow(mark: .icon("text.cursor"), title: tr("Korekta i tłumaczenie ({fix}, {translate})",
                                     ["fix": settings.correctionShortcut.label, "translate": settings.translationShortcut.label]),
                            subtitle: tr("Pobrany tekst trafia do modelu na {place}.", ["place": modelPlace]))
                Divider()
                SettingsRow(mark: .icon("text.badge.checkmark"), title: tr("Sprawdzanie podczas pisania"), subtitle: liveText)
                Divider()
                SettingsRow(mark: .icon("mic"), title: tr("Dyktowanie"), subtitle: dictationText)
                if let gateway = RemoteConfig.configuration?.base.host {
                    Divider()
                    SettingsRow(mark: .icon("globe"), title: tr("Dostęp zdalny"),
                                subtitle: tr("Poza domem przez {gateway}; operator bramy może widzieć przesyłane dane.", ["gateway": gateway]))
                }
                Divider()
                SettingsRow(mark: .icon("doc.on.clipboard"), title: tr("Schowek i pola haseł"),
                            subtitle: tr("Schowek tylko na chwilę przy pobieraniu i wklejaniu; pola haseł są pomijane."))
                Divider()
                SettingsRow(mark: .icon("list.bullet.rectangle"), title: tr("Log zdarzeń"),
                            subtitle: tr("Zdarzenia, czasy i długości — nigdy tekst.")) {
                    Button(tr("Pokaż")) { NSWorkspace.shared.activateFileViewerSelecting([EventLog.file]) }
                }
            }
        }
    }

    /// „tym Macu” albo „serwerze host” — dokąd trafia tekst w bieżących ustawieniach.
    private var modelPlace: String {
        settings.modelLocation == .local ? tr("tym Macu (Ollama)") : serverPlace(settings.modelServer?.host)
    }

    private var languageToolPlace: String {
        settings.languageToolLocation == .local ? tr("tym Macu") : serverPlace(settings.languageToolServer?.host)
    }

    /// Serwer bez adresu to sama brama dostępu zdalnego.
    private func serverPlace(_ host: String?) -> String {
        if let host { return tr("serwerze {host}", ["host": host]) }
        if let gateway = RemoteConfig.configuration?.base.host { return tr("serwerze za bramą {gateway}", ["gateway": gateway]) }
        return tr("serwerze z ustawień")
    }

    private var liveText: String {
        guard settings.liveCheckEnabled else { return tr("Wyłączone.") }
        if settings.liveModelOnly { return tr("Po przerwie w pisaniu tekst pola trafia do modelu na {place}.", ["place": modelPlace]) }
        let languageTool = settings.liveLanguageToolEnabled, model = settings.liveBielikEnabled
        guard languageTool || model else { return tr("Tylko słownik na tym Macu.") }
        let local = (!languageTool || settings.languageToolLocation == .local) && (!model || settings.modelLocation == .local)
        if local { return tr("Wszystko na tym Macu — tekst nie opuszcza komputera.") }
        let targets = (languageTool ? [tr("LanguageTool na {place}", ["place": languageToolPlace])] : [])
            + (model ? [tr("modelu na {place}", ["place": modelPlace])] : [])
        return tr("Słownik na tym Macu; po przerwie tekst pola trafia do {targets}.", ["targets": targets.joined(separator: tr(" i "))])
    }

    private var dictationText: String {
        guard dictation.enabled else { return tr("Wyłączone.") }
        let recording = dictation.speechBackend == .local || dictation.speechEndpoint == nil
            ? tr("Nagranie rozpoznaje Whisper na tym Macu i nie jest zapisywane.")
            : tr("Nagranie (WAV) i podpowiedź słów trafiają do {host}; nie jest zapisywane.",
                 ["host": dictation.speechEndpoint?.host ?? tr("serwera mowy")])
        return dictation.cleanup == .off ? recording
            : recording + " " + tr("Tekst do porządkowania trafia do modelu na {place}.", ["place": modelPlace])
    }
}
