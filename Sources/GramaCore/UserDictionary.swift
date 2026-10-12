import Foundation

/// Słownik użytkownika — jeden dla sprawdzania, korekty i dyktowania (`dictionary.py` na Linuksie): własne słowa
/// wpisane w Pisaniu i słowa dodane „Dodaj do słownika”. Sprawdzanie ich nie podkreśla, korekta dostaje ich pisownię
/// (te, których tekst używa), a dyktowanie — podpowiedź dla Whispera i listę dla porządkowania. Czysta logika; zapis
/// jest w `Settings`. Przypadki wspólne z Linuksem: tests/dictionary_cases.json.
package enum UserDictionary {
    /// Najwięcej słów, o których korekta się dowiaduje (`MAX_PROMPT_WORDS`).
    package static let maxPromptWords = 20
    /// Słowa dodane do słownika dołączają do własnych w podpowiedzi Whispera do tej długości (`PROMPT_CHARS`).
    package static let promptChars = 300
    /// Dłuższa linia listy słów nie jest słowem (`MAX_WORD`).
    package static let maxWord = 60

    /// Przed tekstem poprawki, w ostatniej wiadomości (`SPELLING_NOTE`): początek rozmowy zostaje ten sam.
    package static func spellingNote(_ used: [String]) -> String {
        used.isEmpty ? "" : "Spell these names and terms exactly like this: \(used.joined(separator: ", ")).\n\n"
    }

    /// Przed fragmentem trybu „tylko model” (`model_live._SPELLING`).
    package static func fragmentNote(_ used: [String]) -> String {
        used.isEmpty ? "" : "Te nazwy i terminy pisz dokładnie tak: \(used.joined(separator: ", ")).\n"
    }

    /// „GramaDrama, Bielik\nOrion,, staging ” → [GramaDrama, Bielik, Orion, staging]: podział na przecinkach,
    /// średnikach i końcach linii, pierwsza pisownia każdego słowa (bez względu na wielkość liter) — `parse_vocabulary`.
    package static func parse(_ text: String) -> [String] {
        let separators: Set<Character> = [",", ";", "\n", "\r", "\r\n", "\u{0B}", "\u{0C}", "\u{85}", "\u{2028}", "\u{2029}"]
        var seen = Set<String>()
        return text.split(whereSeparator: { separators.contains($0) })
            .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \t")) }
            .filter { !$0.isEmpty && seen.insert($0.lowercased()).inserted }
    }

    /// Listy po kolei, każde słowo raz (bez względu na wielkość liter), w pierwszej pisowni — `user_words`.
    package static func merge(_ lists: [String]...) -> [String] {
        var seen = Set<String>()
        return lists.joined().filter { seen.insert($0.lowercased()).inserted }
    }

    /// Słowa listy słów (systemowej albo takiej jak Enchanta): jedno w linii, `#` to komentarz — `_words`.
    package static func listWords(_ text: String) -> [String] {
        let body = text.hasPrefix("\u{FEFF}") ? String(text.dropFirst()) : text
        return body.split(whereSeparator: \.isNewline).compactMap { line in
            let word = line.trimmingCharacters(in: .whitespaces)
            guard !word.isEmpty, !word.hasPrefix("#"), word.unicodeScalars.count <= maxWord, !word.contains("\u{FFFD}"),
                  !word.contains(where: \.isWhitespace) else { return nil }
            return word
        }
    }

    /// Słowa słownika, których tekst używa, po kolei, najwyżej `maxPromptWords`: jako osobne słowo, w dowolnej
    /// wielkości liter („Gramadrama” dla „GramaDrama”), a od czterech liter także z trzema literami więcej, jak dokłada
    /// je polska odmiana („Bielika” dla „Bielik”). Korekta dowiaduje się tylko o nich — nic, czego i tak nie wysyła.
    package static func relevant(_ words: [String], in text: String) -> [String] {
        var found: [String] = []
        let range = NSRange(location: 0, length: (text as NSString).length)
        for word in words {
            let tail = word.unicodeScalars.count >= 4 ? "\\p{L}{0,3}" : ""
            let pattern = "(?<!\\p{L})" + NSRegularExpression.escapedPattern(for: word) + tail + "(?!\\p{L})"
            guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
                  regex.firstMatch(in: text, range: range) != nil else { continue }
            found.append(word)
            if found.count == maxPromptWords { break }
        }
        return found
    }

    /// Słowa do podpowiedzi Whispera i listy pisowni porządkowania: wszystkie własne, potem dodane do słownika
    /// (najnowsze pierwsze), póki lista mieści się w `promptChars` — `prompt_words`. Whisper czyta tylko koniec długiej
    /// podpowiedzi, a żargon i tak idzie po tych słowach.
    package static func promptWords(own: [String], learned: [String]) -> [String] {
        var words = own
        var seen = Set(words.map { $0.lowercased() })
        var length = words.joined(separator: ", ").unicodeScalars.count
        for word in learned where !seen.contains(word.lowercased()) {
            length += word.unicodeScalars.count + (words.isEmpty ? 0 : 2)
            if length > promptChars { break }
            seen.insert(word.lowercased())
            words.append(word)
        }
        return words
    }

    /// Czy słowo należy do słownika w pisowni, jaką przyjmuje lista Enchanta: jak zapisane, wielkimi literami albo
    /// z wielką pierwszą, gdy zapisane jest małymi („Staging” dla „staging”) — `Known`.
    package struct Known: Sendable {
        private let exact: Set<String>
        private let upper: Set<String>

        package init(_ words: [String] = []) {
            exact = Set(words)
            upper = Set(words.map { $0.uppercased() })
        }

        package var isEmpty: Bool { exact.isEmpty }

        package func contains(_ word: String) -> Bool {
            if exact.contains(word) { return true }
            if word == word.uppercased(), word != word.lowercased(), upper.contains(word) { return true }
            guard let first = word.first, first.isUppercase else { return false }
            return exact.contains(first.lowercased() + word.dropFirst())
        }
    }

    /// Identyfikator reguły LanguageTool: COMMA_PARENTHESIS_WHITESPACE, PL_BRAK_PRZECINKA… (`RULE`).
    package static func isRule(_ id: String) -> Bool {
        id.range(of: "^[A-Za-z0-9_.\\-]{1,100}$", options: .regularExpression) != nil
    }
}
