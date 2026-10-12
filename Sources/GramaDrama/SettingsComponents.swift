import AppKit
import SwiftUI
import GramaCore

// Klocki okna aplikacji. Zasada: podpis pod opcją w jednej linii, bez czasów, limitów i poleceń dla deweloperów —
// szczegóły są w dokumentacji. Znak stanu (✓ / ⚠︎) tylko tam, gdzie coś może nie działać (uprawnienia, usługi).

/// Sekcja z tytułem i najwyżej jednym zdaniem objaśnienia.
struct SettingsSection<Content: View>: View {
    let title: String
    var note: String? = nil
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            if let note {
                Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            content
        }
    }
}

/// Karta z wierszami (tło jak w Ustawieniach systemowych).
struct SettingsCard<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary))
    }
}

/// Znak przed wierszem: stan rzeczy, która może nie działać (nil = sprawdzam / nie dotyczy) albo zwykła ikona.
enum RowMark {
    case status(Bool?)
    case icon(String)
}

struct SettingsRow<Trailing: View>: View {
    let mark: RowMark
    let title: String
    var subtitle: String? = nil
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 12) {
            Group {
                switch mark {
                case .status(let ok): StatusIcon(ok: ok)
                case .icon(let name): Image(systemName: name).foregroundStyle(.secondary)
                }
            }
            .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
    }
}

extension SettingsRow where Trailing == EmptyView {
    init(mark: RowMark, title: String, subtitle: String? = nil) {
        self.init(mark: mark, title: title, subtitle: subtitle) { EmptyView() }
    }
}

/// Przełącznik w wierszu (bez własnej etykiety — tytuł jest w wierszu).
struct RowToggle: View {
    @Binding var isOn: Bool
    var disabled = false

    var body: some View {
        Toggle("", isOn: $isOn).labelsHidden().toggleStyle(.switch).disabled(disabled)
    }
}

struct StatusIcon: View {
    let ok: Bool?

    var body: some View {
        switch ok {
        case true?: Image(systemName: "checkmark").foregroundStyle(Brand.sage).fontWeight(.semibold)
        case false?: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Brand.brick)
        case nil: Image(systemName: "circle.dotted").foregroundStyle(.secondary)
        }
    }
}

/// Komunikat stanu pod formularzem (wynik sprawdzenia serwera, błąd adresu).
struct StatusLine: View {
    enum Kind { case ok, warning, error }
    let kind: Kind
    let text: String

    var body: some View {
        switch kind {
        case .ok: Label(text, systemImage: "checkmark").font(.caption).foregroundStyle(Brand.sage)
        case .warning: Label(text, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Brand.brick)
        case .error: Label(text, systemImage: "xmark.octagon").font(.caption).foregroundStyle(Brand.rose)
        }
    }
}

// MARK: Usługi — ten sam układ dla modelu, LanguageTool i mowy

/// Pierwszy wiersz usługi: gdzie działa — na tym Macu albo na serwerze.
struct LocationRow: View {
    @Binding var selection: ServiceLocation
    let subtitle: String

    var body: some View {
        SettingsRow(mark: .icon(selection == .local ? "laptopcomputer" : "server.rack"), title: tr("Gdzie"), subtitle: subtitle) {
            Picker("", selection: $selection) {
                ForEach(ServiceLocation.allCases) { Text($0.label).tag($0) }
            }
            .labelsHidden().pickerStyle(.segmented).fixedSize()
        }
    }
}

/// Serwer usługi: adres, pola usługi (protokół, model, klucz API), „Zapisz i sprawdź” i błąd adresu.
struct ServerForm<Fields: View>: View {
    let placeholder: String
    @Binding var address: String
    let error: String?
    let save: () -> Void
    @ViewBuilder let fields: Fields

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(tr("Adres serwera"), text: $address, prompt: Text(placeholder)).onSubmit(save)
            fields
            Button(tr("Zapisz i sprawdź"), action: save)
            if let error { StatusLine(kind: .error, text: error) }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }
}

extension ServerForm where Fields == EmptyView {
    init(placeholder: String, address: Binding<String>, error: String?, save: @escaping () -> Void) {
        self.init(placeholder: placeholder, address: address, error: error, save: save) { EmptyView() }
    }
}

/// Adres usługi na tym Macu: puste pole to adres domyślny (podpowiedź w polu), inny musi wskazywać ten Mac.
struct LocalAddressRow: View {
    let title: String
    let placeholder: String
    @Binding var address: String
    let error: String?
    let save: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SettingsRow(mark: .icon("link"), title: title) {
                TextField("", text: $address, prompt: Text(placeholder)).frame(width: 220).onSubmit(save)
                Button(tr("Zapisz"), action: save)
            }
            if let error { StatusLine(kind: .error, text: error).padding(.leading, 44).padding(.bottom, 8) }
        }
    }
}

/// Stan usługi: znak, wynik ostatniego sprawdzenia i „Sprawdź” (przed nim przyciski usługi, np. „Uruchom”).
struct ServiceStatusRow<Actions: View>: View {
    let title: String
    /// nil — jeszcze nie sprawdzono.
    let status: ServiceStatus?
    let checking: Bool
    let check: () -> Void
    @ViewBuilder let actions: Actions

    var body: some View {
        SettingsRow(mark: .status(status.map { if case .ok = $0 { true } else { false } }), title: title,
                    subtitle: status?.text ?? tr("Sprawdzam…")) {
            HStack(spacing: 8) {
                actions
                Button(checking ? tr("Sprawdzam…") : tr("Sprawdź"), action: check).disabled(checking)
            }
        }
    }
}

extension ServiceStatusRow where Actions == EmptyView {
    init(title: String, status: ServiceStatus?, checking: Bool, check: @escaping () -> Void) {
        self.init(title: title, status: status, checking: checking, check: check) { EmptyView() }
    }
}

struct KeyCaps: View {
    let keys: [String]

    var body: some View {
        HStack(spacing: 4) {
            ForEach(Array(keys.enumerated()), id: \.offset) { _, key in
                if key == "…" {
                    Text("…").foregroundStyle(.secondary)
                } else {
                    Text(key).font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: 5).fill(.quaternary))
                }
            }
        }
    }
}

struct AppIconView: View {
    let bundleID: String

    var body: some View {
        let image = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID)
            .map { NSWorkspace.shared.icon(forFile: $0.path) } ?? NSImage(systemSymbolName: "app", accessibilityDescription: nil)!
        Image(nsImage: image).resizable().frame(width: 28, height: 28)
    }
}

/// Wspólne teksty i formaty okna.
enum WindowText {
    /// Język interfejsu wprost — aplikacja nie ma plików .lproj, więc bez tego po polsku byłoby „9 minutes ago”.
    static var locale: Locale { UI.language.locale }

    static func appName(_ bundleID: String) -> String {
        if bundleID == Bundle.main.bundleIdentifier { return tr("GramaDrama (Wypróbuj)") }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else { return bundleID }
        return FileManager.default.displayName(atPath: url.path).replacingOccurrences(of: ".app", with: "")
    }

    static func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = locale
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    static func duration(_ seconds: Double) -> String {
        switch seconds {
        case ..<90: tr("chwili")
        case ..<5400: "\(Int(seconds / 60)) min"
        case ..<172_800: tr("{hours} godz.", ["hours": Int(seconds / 3600)])
        default: tr("{days} dni", ["days": Int(seconds / 86_400)])
        }
    }

    static func seconds(_ milliseconds: Int) -> String { UI.seconds(Double(milliseconds) / 1000) }
}
