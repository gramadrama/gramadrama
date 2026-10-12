import Foundation
import os

/// Język interfejsu aplikacji (`i18n.interface_language` na Linuksie): polski, gdy główny język tekstu jest polski,
/// w każdym innym przypadku angielski. Tekst użytkownika nigdy nie przechodzi przez tłumaczenie.
package enum UILanguage: String, Sendable {
    case polish = "pl", english = "en"

    package init(primary: TextLanguage) { self = primary == .polish ? .polish : .english }

    /// Formaty dat, liczb i rozmiarów w oknie.
    package var locale: Locale { Locale(identifier: self == .polish ? "pl_PL" : "en_US") }
}

/// Teksty interfejsu: polski oryginał jest kluczem (jak na Linuksie), angielska wersja pochodzi z katalogu
/// `Resources/locales/en/*.json`. Brak wpisu — zostaje polski (test `scripts/tests/test_ui_catalog.py` tego pilnuje).
package enum UI {
    private static let state = OSAllocatedUnfairLock(initialState: UILanguage.polish)

    /// Ustawia `Settings` z głównego języka tekstu (także przy jego zmianie).
    package static var language: UILanguage {
        get { state.withLock { $0 } }
        set { state.withLock { $0 = newValue } }
    }

    package static var isEnglish: Bool { language == .english }

    /// Tekst w języku interfejsu; `{nazwa}` zastąpione wartościami już po tłumaczeniu, więc tekst użytkownika
    /// w wartościach (słowo, nazwa aplikacji, adres) nigdy nie jest tłumaczony.
    package static func tr(_ polish: String, _ values: [String: Any] = [:]) -> String {
        var text = language == .polish ? polish : (english[polish] ?? polish)
        for (name, value) in values { text = text.replacingOccurrences(of: "{\(name)}", with: "\(value)") }
        return text
    }

    /// „1 poprawka / 3 poprawki / 5 poprawek” albo „1 correction / 3 corrections”.
    package static func count(_ n: Int, _ polish: (one: String, few: String, many: String),
                              _ english: (one: String, other: String)) -> String {
        if isEnglish { return "\(n) \(n == 1 ? english.one : english.other)" }
        let last = n % 10, lastTwo = n % 100
        let word = n == 1 ? polish.one : (2...4).contains(last) && !(12...14).contains(lastTwo) ? polish.few : polish.many
        return "\(n) \(word)"
    }

    /// „2,9 s” po polsku, „2.9 s” po angielsku.
    package static func seconds(_ value: Double) -> String {
        let text = String(format: "%.1f s", value)
        return isEnglish ? text : text.replacingOccurrences(of: ".", with: ",")
    }

    /// Katalog angielski: pliki z pakietu aplikacji, a w buildzie deweloperskim i testach — z repozytorium.
    package static let english: [String: String] = {
        var result: [String: String] = [:]
        for directory in catalogDirectories {
            guard let files = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { continue }
            for file in files.sorted(by: { $0.path < $1.path }) where file.pathExtension == "json" {
                guard let data = try? Data(contentsOf: file),
                      let entries = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { continue }
                result.merge(entries.filter { !$0.value.isEmpty }) { current, _ in current }
            }
            if !result.isEmpty { break }
        }
        return result
    }()

    static var catalogDirectories: [URL] {
        var directories: [URL] = []
        if let resources = Bundle.main.resourceURL { directories.append(resources.appendingPathComponent("locales/en")) }
        #if DEBUG
        // swift build / swift test: katalog w repozytorium (Sources/GramaCore/UI.swift → Resources/locales/en).
        directories.append(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Resources/locales/en"))
        #endif
        return directories
    }
}

/// Skrót: `tr("Zapisz")`, `tr("Zapisano: {value}.", ["value": label])`.
package func tr(_ polish: String, _ values: [String: Any] = [:]) -> String { UI.tr(polish, values) }
