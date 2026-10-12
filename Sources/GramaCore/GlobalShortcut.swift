import Foundation

/// Globalny skrót korekty albo tłumaczenia (Start → Skróty → Zmień skróty) — odpowiednik `engine/shortcuts.py`
/// na Linuksie. Dozwolona jest tylko litera A–Z z co najmniej dwoma z ⌃ ⌥ ⌘ (⇧ dowolnie): ⌥ albo ⇧⌥ z literą pisze
/// znak (©, ∂), ⌃ z literą to skróty edycji w polach tekstowych (⌃A, ⌃E), a ⇧⌘ z literą — polecenia aplikacji
/// (⇧⌘Z, ⇧⌘T). Taki skrót nie przechwytuje więc zwykłego pisania.
package struct GlobalShortcut: Equatable, Sendable {
    package struct Modifiers: OptionSet, Hashable, Sendable {
        package let rawValue: Int
        package init(rawValue: Int) { self.rawValue = rawValue }

        package static let control = Modifiers(rawValue: 1)
        package static let option = Modifiers(rawValue: 2)
        package static let shift = Modifiers(rawValue: 4)
        package static let command = Modifiers(rawValue: 8)

        /// Kolejność znaków jak w menu macOS: ⌃ ⌥ ⇧ ⌘.
        static let symbols: [(Modifiers, String)] = [(.control, "⌃"), (.option, "⌥"), (.shift, "⇧"), (.command, "⌘")]
    }

    /// Wirtualny kod klawisza (`kVK_ANSI_*`): położenie klawisza, tak jak go rejestruje Carbon.
    package let keyCode: Int
    package let modifiers: Modifiers
    /// Litera tego klawisza w układzie z chwili nagrania (QWERTZ: klawisz Y to „z”) — tylko do wyświetlenia.
    package let letter: String

    package init(keyCode: Int, modifiers: Modifiers, letter: String) {
        self.keyCode = keyCode
        self.modifiers = modifiers
        self.letter = letter.uppercased()
    }

    /// Domyślne: ⌥⌘G (Popraw) i ⌥⌘T (Przetłumacz) — kVK_ANSI_G = 5, kVK_ANSI_T = 17.
    package static let defaultCorrection = GlobalShortcut(keyCode: 5, modifiers: [.option, .command], letter: "G")
    package static let defaultTranslation = GlobalShortcut(keyCode: 17, modifiers: [.option, .command], letter: "T")

    /// Klawisze do pokazania osobno („⌥”, „⌘”, „G”).
    package var keys: [String] {
        Modifiers.symbols.filter { modifiers.contains($0.0) }.map(\.1) + [letter]
    }

    /// „⌥⌘G”.
    package var label: String { keys.joined() }

    /// Zapis w ustawieniach: „⌥⌘G@5” — etykieta i kod klawisza.
    package var rawValue: String { "\(label)@\(keyCode)" }

    package init?(rawValue: String) {
        guard let at = rawValue.lastIndex(of: "@"), let keyCode = Int(rawValue[rawValue.index(after: at)...]),
              (0...127).contains(keyCode) else { return nil }
        var label = Substring(rawValue[..<at])
        var modifiers: Modifiers = []
        for (modifier, symbol) in Modifiers.symbols where label.hasPrefix(symbol) {
            modifiers.insert(modifier)
            label = label.dropFirst(symbol.count)
        }
        guard label.count == 1 else { return nil }
        self.init(keyCode: keyCode, modifiers: modifiers, letter: String(label))
        guard Self.problem(modifiers: modifiers, letter: letter) == nil else { return nil }
    }

    // MARK: Zasady

    package static var rule: String { tr("Użyj co najmniej dwóch z ⌃ ⌥ ⌘ (⇧ dowolnie) i jednej litery A–Z, np. ⌥⌘G.") }
    package static var duplicate: String { tr("Korekta i tłumaczenie muszą mieć różne skróty.") }

    /// Skróty, które macOS albo każda aplikacja już mają — przechwycenie ich zepsułoby coś ważnego. Nazwy po polsku,
    /// tłumaczone przy pokazaniu (`Resources/locales/en/dynamic.json`).
    static let reserved: [String: String] = [
        "⌃⌘Q": "Zablokuj ekran", "⌃⌘F": "Pełny ekran", "⌃⌘D": "Definicja słowa",
        "⌥⌘D": "Pokaż lub ukryj Dock", "⌥⌘H": "Ukryj pozostałe", "⌥⌘M": "Zminimalizuj wszystkie",
    ]

    /// Powód, dla którego to nie może być skrót, albo nil. `letter`: znak klawisza bez modyfikatorów.
    package static func problem(modifiers: Modifiers, letter: String) -> String? {
        let letter = letter.lowercased()
        guard letter.count == 1, let scalar = letter.unicodeScalars.first, ("a"..."z").contains(scalar) else { return rule }
        guard modifiers.intersection([.control, .option, .command]).rawValue.nonzeroBitCount >= 2 else { return rule }
        let shortcut = GlobalShortcut(keyCode: 0, modifiers: modifiers, letter: letter)
        if let name = reserved[shortcut.label] {
            return tr("{shortcut} ma już macOS ({name}) — wybierz inny.", ["shortcut": shortcut.label, "name": tr(name)])
        }
        return nil
    }

    /// Para z ustawień; zapis uszkodzony albo dwa takie same skróty — obie wartości domyślne, jak `resolve` na Linuksie.
    package static func resolve(correction: String?, translation: String?) -> (correction: GlobalShortcut, translation: GlobalShortcut) {
        let fix = correction.map { GlobalShortcut(rawValue: $0) } ?? defaultCorrection
        let translate = translation.map { GlobalShortcut(rawValue: $0) } ?? defaultTranslation
        guard let fix, let translate, !fix.sameKeys(as: translate) else { return (defaultCorrection, defaultTranslation) }
        return (fix, translate)
    }

    /// Ten sam klawisz z tymi samymi modyfikatorami (litera do wyświetlenia się nie liczy).
    package func sameKeys(as other: GlobalShortcut) -> Bool {
        keyCode == other.keyCode && modifiers == other.modifiers
    }
}
