import SwiftUI
import UniformTypeIdentifiers
import GramaCore
import GramaBackend

/// Aplikacje: ustawienia dla każdej aplikacji i strony oraz integracje, przez które GramaDrama czyta tekst.
struct AppsTab: View {
    @ObservedObject var model: MainWindowModel
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var browserStatus = BrowserStatus.shared

    var body: some View {
        appsSection
        if !settings.disabledSites.isEmpty { sitesSection }
        integrationsSection
    }

    // MARK: Aplikacje

    private var appsSection: some View {
        SettingsSection(title: tr("Aplikacje"), note: tr("Tryb {shortcut} i wyłączanie GramaDramy dla każdej aplikacji.", ["shortcut": settings.correctionShortcut.label])) {
            SettingsCard {
                ForEach(appRows, id: \.self) { bundleID in
                    appRow(bundleID)
                    Divider()
                }
                Button { addApp() } label: { Label(tr("Dodaj aplikację…"), systemImage: "plus").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderless)
                    .padding(.vertical, 10)
            }
        }
    }

    private func appRow(_ bundleID: String) -> some View {
        let usage = model.apps.first { $0.bundleID == bundleID }
        let disabled = settings.isDisabled(bundleID)
        return HStack(spacing: 12) {
            AppIconView(bundleID: bundleID)
            VStack(alignment: .leading, spacing: 2) {
                Text(WindowText.appName(bundleID))
                Text(disabled ? tr("Wyłączona — skróty trafiają do aplikacji bez zmian.")
                     : usage.map { ($0.lastOutcome == "działa" ? tr("Działa") : tr("Ostatnio: {outcome}", ["outcome": tr($0.lastOutcome)]))
                                   + " · \(WindowText.relative($0.lastUsed)) · \($0.uses)×" }
                     ?? tr("Dodana ręcznie"))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Picker("", selection: Binding(get: { settings.defaultMode(for: bundleID) },
                                          set: { settings.setDefaultMode($0, for: bundleID) })) {
                ForEach(Settings.defaultModeChoices) { Text($0.title).tag($0) }
            }
            .labelsHidden().pickerStyle(.menu).fixedSize()
            .disabled(disabled)
            .help(tr("Tryb, w którym {shortcut} otwiera panel w tej aplikacji", ["shortcut": settings.correctionShortcut.label]))
            Toggle("", isOn: Binding(get: { !disabled }, set: { settings.setDisabled(bundleID, !$0) }))
                .labelsHidden().toggleStyle(.switch)
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
    }

    private var appRows: [String] {
        var rows = model.apps.map(\.bundleID).filter { $0 != Bundle.main.bundleIdentifier }
        for id in settings.manualBundleIDs + settings.disabledBundleIDs where !rows.contains(id) { rows.append(id) }
        return rows
    }

    private func addApp() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.application]
        panel.directoryURL = URL(fileURLWithPath: "/Applications")
        panel.prompt = tr("Dodaj")
        guard panel.runModal() == .OK, let url = panel.url,
              let bundleID = Bundle(url: url)?.bundleIdentifier, bundleID != Bundle.main.bundleIdentifier else { return }
        if !settings.manualBundleIDs.contains(bundleID) { settings.manualBundleIDs.append(bundleID) }
    }

    // MARK: Strony

    private var sitesSection: some View {
        SettingsSection(title: tr("Wyłączone strony"), note: tr("Bez sprawdzania podczas pisania; {shortcut} i dyktowanie działają.", ["shortcut": settings.correctionShortcut.label])) {
            SettingsCard {
                ForEach(Array(settings.disabledSites.sorted().enumerated()), id: \.element) { index, site in
                    if index > 0 { Divider() }
                    SettingsRow(mark: .icon("globe"), title: site) {
                        Toggle("", isOn: Binding(get: { false }, set: { if $0 { settings.setSiteDisabled(site, false) } }))
                            .labelsHidden().toggleStyle(.switch).help(tr("Włącz sprawdzanie na tej stronie"))
                    }
                }
            }
        }
    }

    // MARK: Integracje

    private var integrationsSection: some View {
        SettingsSection(title: tr("Integracje")) {
            SettingsCard {
                let connected = browserStatus.connected
                SettingsRow(mark: .status(connected.isEmpty ? nil : true), title: tr("Wtyczka przeglądarki"),
                            subtitle: connected.isEmpty
                                ? tr("Nie połączona — zainstaluj wtyczkę GramaDramy w Edge albo Chrome.")
                                : tr("Połączona: {browsers}. Stronę wyłączysz ikoną wtyczki.", ["browsers": connected.sorted { $0.key < $1.key }
                                    .map { "\(WindowText.appName($0.key)) (\($0.value))" }.joined(separator: ", ")]))
                Divider()
                if let ime = model.ime {
                    ImeKeyboardRow(model: model)
                    if let message = model.imeMessage {
                        Divider()
                        SettingsRow(mark: .icon("info.circle"), title: tr("Włączanie"), subtitle: message)
                    }
                    if ime.installed {
                        Divider()
                        SettingsRow(mark: .icon("keyboard"), title: tr("Używaj jej do {shortcut} i dyktowania", ["shortcut": settings.correctionShortcut.label]),
                                    subtitle: settings.imeChannelEnabled ? tr("Bez schowka; podmienia tylko zmienione słowa.")
                                                                         : tr("{shortcut} i dyktowanie przez Dostępność i schowek.", ["shortcut": settings.correctionShortcut.label])) {
                            RowToggle(isOn: $settings.imeChannelEnabled)
                        }
                    }
                    if !ime.restart.isEmpty {
                        Divider()
                        SettingsRow(mark: .status(false), title: tr("Do ponownego uruchomienia"),
                                    subtitle: tr("{apps} — działały przed instalacją klawiatury i mogą jej nie widzieć.", ["apps": ime.restart.joined(separator: ", ")]))
                    }
                } else {
                    ImeKeyboardRow(model: model)
                }
            }
        }
    }

}
