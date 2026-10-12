import Foundation
import Testing
@testable import GramaCore

/// Słownik użytkownika jak na Linuksie: tests/dictionary_cases.json (oczekiwane wartości liczy kod Linuksa).
@Suite("Słownik użytkownika: przypadki wspólne z Linuksem")
struct UserDictionaryTests {
    let cases: [String: Any] = (try? Fixtures.json("tests/dictionary_cases.json")) ?? [:]

    private func list(_ key: String) -> [[String: Any]] { cases[key] as? [[String: Any]] ?? [] }

    @Test func notesAreTheLinuxOnes() throws {
        let fix = try #require(cases["spelling_note"] as? String)
        let fragment = try #require(cases["fragment_note"] as? String)
        #expect(UserDictionary.spellingNote(["A", "B"]) == fix.replacingOccurrences(of: "%s", with: "A, B"))
        #expect(UserDictionary.fragmentNote(["A", "B"]) == fragment.replacingOccurrences(of: "%s", with: "A, B"))
        #expect(UserDictionary.spellingNote([]).isEmpty)
    }

    @Test func relevantWords() {
        #expect(!list("relevant").isEmpty)
        for c in list("relevant") {
            let words = c["words"] as? [String] ?? [], text = c["text"] as? String ?? ""
            #expect(UserDictionary.relevant(words, in: text) == c["expected"] as? [String], "\(text)")
        }
    }

    @Test func knownSpellings() {
        #expect(!list("known").isEmpty)
        for c in list("known") {
            let word = c["word"] as? String ?? ""
            #expect(UserDictionary.Known(c["words"] as? [String] ?? []).contains(word) == c["expected"] as? Bool, "\(word)")
        }
    }

    @Test func promptWords() {
        #expect(!list("prompt_words").isEmpty)
        for c in list("prompt_words") {
            #expect(UserDictionary.promptWords(own: c["own"] as? [String] ?? [], learned: c["learned"] as? [String] ?? [])
                    == c["expected"] as? [String])
        }
    }

    @Test func parsedWords() {
        #expect(!list("parse").isEmpty)
        for c in list("parse") {
            let text = c["text"] as? String ?? ""
            #expect(UserDictionary.parse(text) == c["expected"] as? [String], "\(text.debugDescription)")
        }
    }

    @Test func wordListsAndRules() {
        #expect(UserDictionary.listWords("\u{FEFF}Kowalski\n# komentarz\n\nz spacją\nOrion\r\n") == ["Kowalski", "Orion"])
        #expect(UserDictionary.isRule("PL_BRAK_PRZECINKA") && UserDictionary.isRule("COMMA_PARENTHESIS_WHITESPACE"))
        #expect(!UserDictionary.isRule("") && !UserDictionary.isRule("a b") && !UserDictionary.isRule(String(repeating: "A", count: 101)))
    }

    /// Notka przed tekstem w ostatniej wiadomości: początek rozmowy (system i przykłady) zostaje ten sam,
    /// więc serwer dalej trafia w pamięć prefiksu; w Tłumacz nie ma notki.
    @Test func aCorrectionIsToldTheSpellingBeforeItsText() {
        let text = "Napisz do bielika jutro."
        let plain = Prompts.messages(for: text, mode: .fix, prompts: .defaults)
        let told = Prompts.messages(for: text, mode: .fix, prompts: .defaults, spelling: ["Bielik"])
        #expect(Array(told.dropLast()) == Array(plain.dropLast()))
        #expect(told.last?["content"] == UserDictionary.spellingNote(["Bielik"]) + (plain.last?["content"] ?? ""))
        let translated = Prompts.messages(for: text, mode: .translate, prompts: .defaults, spelling: ["Bielik"])
        #expect(translated.last?["content"]?.contains("Spell these") == false)
        let fragment = ModelLive.messages(for: text, spelling: ["Bielik"])
        #expect(fragment.last?["content"]?.hasPrefix(UserDictionary.fragmentNote(["Bielik"])) == true)
        #expect(ModelLive.messages(for: text) == ModelLive.messages(for: text, spelling: []))
    }

    @Test func onlyLanguageToolGrammarRulesCanBeTurnedOff() {
        func issue(_ source: LiveIssue.Source, _ rule: String) -> LiveIssue {
            LiveIssue(range: NSRange(location: 0, length: 1), word: "a", message: "", suggestions: [], source: source, rule: rule)
        }
        #expect(issue(.languageTool, "PL_BRAK_PRZECINKA").canTurnOff)
        #expect(!issue(.languageTool, "MORFOLOGIK_RULE_PL_PL").canTurnOff)
        #expect(!issue(.languageTool, "PL_SIMPLE_REPLACE").canTurnOff)
        #expect(!issue(.spelling, "SPELLING_PL").canTurnOff)
        #expect(!issue(.bielik, "BIELIK").canTurnOff)
    }
}
