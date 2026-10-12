import AppKit
import GramaBackend

/// „Dodaj do słownika” i usuwanie słowa: słownik macOS (ten sam, którego używają Notatki, Mail i Safari — sprawdzanie
/// podczas pisania go czyta) i lista GramaDramy w `Settings`, z której korekta i dyktowanie biorą pisownię (macOS nie
/// podaje listy nauczonych słów).
@MainActor
enum UserWords {
    static func learn(_ word: String) {
        NSSpellChecker.shared.learnWord(word)
        Settings.shared.recordLearned(word)
    }

    /// Usunięte w Pisaniu: także ze słownika macOS, bez listy wyjątków — poprawne słowo nie będzie potem podkreślane.
    static func forget(_ word: String) {
        NSSpellChecker.shared.unlearnWord(word)
        Settings.shared.forgetLearned(word)
        SystemWordList.hide(word)
    }
}
