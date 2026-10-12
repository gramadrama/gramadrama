import SwiftUI
import Carbon
import ServiceManagement
import GramaCore
import GramaBackend

/// Start: stan w jednym miejscu, pole do wypróbowania, uprawnienia, skróty i ustawienia systemowe.
struct StartTab: View {
    @ObservedObject var model: MainWindowModel
    @ObservedObject private var appState = AppState.shared
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var dictation = DictationSettings.shared
    @AppStorage(MainWindowTab.defaultsKey) private var tab: MainWindowTab = .start
    @State private var trial = Self.sampleText(for: Settings.shared.textLanguages.primary)

    /// Tekst z błędami w głównym języku tekstu (`sample_text` na Linuksie).
    static func sampleText(for language: TextLanguage) -> String {
        switch language {
        case .english: "Hi, Im sending the updated report. If you have any comment let me know by friday."
        case .german: "Hallo, ich sende den aktualisierten bericht. Wenn du anmerkungen hast melde dich bitte bis freitag."
        default: "Czesc, przesylam poprawiona wersje raportu. Jesli bedziesz miec uwagi daj znac do piatku."
        }
    }

    var body: some View {
        header
        trySection
        permissionsSection
        shortcutsSection
        systemSection
    }

    // MARK: Nagłówek i stan

    private var header: some View {
        VStack(spacing: 6) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 84, height: 84)
            Text("GramaDrama").font(.title.bold())
            Text(tr("Pisz bez dramatu.")).foregroundStyle(.secondary)
            let problems = model.problems
            // Kliknięcie prowadzi do zakładki, w której naprawisz pierwszy problem.
            Button { if let first = problems.first { tab = first.tab } } label: {
                Label(problems.isEmpty ? tr("Wszystko gotowe")
                                       : tr("Wymaga uwagi: {problems}", ["problems": problems.map(\.title).joined(separator: ", ")]),
                      systemImage: problems.isEmpty ? "checkmark" : "exclamationmark.triangle.fill")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(problems.isEmpty ? Brand.sage : Brand.brick)
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .background(problems.isEmpty ? Brand.sageBackground : Brand.wave.opacity(0.16), in: Capsule())
            }
            .buttonStyle(.plain)
            .disabled(problems.isEmpty)
            .opacity(model.diagnostics == nil ? 0.4 : 1)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: Wypróbuj

    private var trySection: some View {
        SettingsSection(title: tr("Wypróbuj"),
                        note: tr("Kliknij w tekst i naciśnij {fix} albo {translate}.",
                                 ["fix": settings.correctionShortcut.label, "translate": settings.translationShortcut.label])) {
            VStack(alignment: .leading, spacing: 8) {
                TextEditor(text: $trial)
                    // Inny język główny: przykład w nowym języku, jeśli w polu stoi jeszcze przykład.
                    .onChange(of: settings.textLanguages.primary) { _, primary in
                        if TextLanguage.supported.map(Self.sampleText).contains(trial) { trial = Self.sampleText(for: primary) }
                    }
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(8)
                    .frame(height: 70)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.quaternary))
                HStack {
                    if let status = model.trialStatus {
                        StatusIcon(ok: status.ok)
                        Text(status.text).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        trial = Self.sampleText(for: settings.textLanguages.primary)
                        model.trialStatus = nil
                    } label: { Image(systemName: "arrow.uturn.backward") }
                        .buttonStyle(.borderless).help(tr("Przywróć przykład"))
                }
            }
        }
    }

    // MARK: Uprawnienia

    private var permissionsSection: some View {
        SettingsSection(title: tr("Uprawnienia")) {
            SettingsCard {
                SettingsRow(mark: .status(model.accessibility), title: tr("Dostępność"),
                            subtitle: model.accessibility ? tr("Odczyt i podmiana tekstu w innych aplikacjach.")
                                                          : tr("Bez niego GramaDrama nie odczyta tekstu.")) {
                    if !model.accessibility {
                        Button(tr("Otwórz ustawienia")) {
                            _ = TextBridge.isTrusted(prompt: true)
                            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
                        }
                    }
                }
                Divider()
                SettingsRow(mark: .status(microphoneOK), title: tr("Mikrofon"), subtitle: microphoneText) {
                    if model.microphone != .authorized {
                        Button(model.microphone == .notDetermined ? tr("Zezwól") : tr("Otwórz ustawienia")) { model.requestMicrophone() }
                    }
                }
            }
        }
    }

    private var microphoneOK: Bool? {
        switch model.microphone {
        case .authorized: true
        case .notDetermined: nil
        default: false
        }
    }

    private var microphoneText: String {
        switch model.microphone {
        case .authorized: tr("Nagrywa tylko, gdy trzymasz klawisz dyktowania.")
        case .notDetermined: tr("macOS zapyta przy pierwszym dyktowaniu.")
        default: tr("Brak dostępu — dyktowanie nie zadziała.")
        }
    }

    // MARK: Skróty

    private var shortcutsSection: some View {
        SettingsSection(title: tr("Skróty")) {
            SettingsCard {
                hotKeyRow("fix", title: tr("Popraw tekst"), keys: settings.correctionShortcut.keys)
                Divider()
                hotKeyRow("translate", title: settings.textLanguages.translates
                                                ? tr("Przetłumacz {pair}", ["pair": settings.textLanguages.pairLabel]) : tr("Przetłumacz"),
                          keys: settings.translationShortcut.keys,
                          note: settings.textLanguages.translates ? nil : tr("Dodaj drugi język w zakładce Pisanie."))
                Divider()
                SettingsRow(mark: .icon("mic"), title: tr("Dyktuj: trzymaj, mów, puść")) {
                    KeyCaps(keys: [dictation.key == .fn ? "fn" : tr("prawy ⌥")])
                }
                Divider()
                ShortcutEditor()
                Divider()
                DisclosureGroup(tr("W panelu poprawki")) {
                    VStack(spacing: 0) {
                        ForEach(Array(Self.panelShortcuts.enumerated()), id: \.offset) { _, item in
                            HStack {
                                Text(item.0).font(.callout)
                                Spacer()
                                KeyCaps(keys: item.1)
                            }
                            .padding(.vertical, 5)
                        }
                    }
                    .padding(.top, 4)
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
            }
        }
    }

    private static var panelShortcuts: [(String, [String])] { [
        (tr("Wstaw poprawkę (także w trakcie)"), ["↩"]),
        (tr("Anuluj"), ["⎋"]),
        (tr("Tryby: Popraw … Tłumacz"), ["⌘", "1", "…", "⌘", "6"]),
        (tr("Odwróć kierunek tłumaczenia"), ["⌘", "D"]),
        (tr("Kopiuj wynik"), ["⌘", "⇧", "C"]),
    ] }

    private func hotKeyRow(_ key: String, title: String, keys: [String], note: String? = nil) -> some View {
        let status = appState.hotKeyStatus[key] ?? nil
        return SettingsRow(mark: .status(appState.suspendedFor != nil ? nil : status.map { $0 == noErr }), title: title,
                           subtitle: note ?? hotKeyDescription(status)) {
            KeyCaps(keys: keys)
        }
    }

    private func hotKeyDescription(_ status: OSStatus?) -> String? {
        if let app = appState.suspendedFor { return tr("Wstrzymany — {app} jest wyłączona w GramaDramie.", ["app": app]) }
        if appState.recordingShortcut { return tr("Wstrzymany na czas nagrywania nowego skrótu.") }
        guard let status, status != noErr else { return nil }
        return status == OSStatus(eventHotKeyExistsErr) ? tr("Zajęty przez inną aplikację.") : tr("Błąd rejestracji {status}.", ["status": status])
    }

    // MARK: System

    private var systemSection: some View {
        SettingsSection(title: "System") {
            SettingsCard {
                SettingsRow(mark: model.loginError != nil || model.loginStatus == .requiresApproval ? .status(false) : .icon("power"),
                            title: tr("Uruchamiaj przy logowaniu"), subtitle: model.loginError ?? loginDescription) {
                    HStack(spacing: 8) {
                        if model.loginStatus == .requiresApproval {
                            Button(tr("Zatwierdź…")) { SMAppService.openSystemSettingsLoginItems() }
                        }
                        Toggle("", isOn: Binding(get: { model.loginStatus == .enabled || model.loginStatus == .requiresApproval },
                                                 set: model.setLaunchAtLogin)).labelsHidden().toggleStyle(.switch)
                    }
                }
                Divider()
                SettingsRow(mark: .status(lastUseOK), title: tr("Ostatnie użycie"), subtitle: lastUseText)
                Divider()
                SettingsRow(mark: .icon("info.circle"), title: tr("Wersja"), subtitle: versionText)
            }
        }
    }

    private var loginDescription: String? {
        switch model.loginStatus {
        case .enabled: tr("Po zalogowaniu działa w tle (ikona masek w pasku menu).")
        case .requiresApproval: tr("Czeka na zatwierdzenie w Ustawieniach systemowych.")
        default: nil
        }
    }

    private var lastUseOK: Bool? {
        model.lastUse.map(\.ok)
    }

    private var lastUseText: String {
        guard let use = model.lastUse else { return tr("Jeszcze nie użyto skrótu.") }
        let mode = Mode(rawValue: use.mode)?.title ?? use.mode
        let today = Calendar.current.isDateInToday(use.date)
        let style = Date.FormatStyle(date: today ? .omitted : .abbreviated, time: .shortened).locale(WindowText.locale)
        let when = (today ? tr("dziś") + " " : "") + use.date.formatted(style)
        return [when, WindowText.appName(use.bundleID), mode, use.scope.map { tr($0) }, use.outcome].compactMap { $0 }.joined(separator: " · ")
    }

    private var versionText: String {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] as? String ?? "dev") (\(info?["CFBundleVersion"] as? String ?? "dev"))"
    }
}
