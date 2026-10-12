import Foundation
import Testing
@testable import GramaCore

/// Język interfejsu (`UI`). Testy nie przełączają `UI.language` — to stan całego procesu, a pozostałe testy
/// sprawdzają polskie komunikaty; angielski katalog sprawdzamy wprost.
@Suite("Język interfejsu")
struct InterfaceLanguageTests {
    @Test func followsThePrimaryTextLanguage() {
        #expect(UILanguage(primary: .polish) == .polish)
        #expect(UILanguage(primary: .english) == .english)
        #expect(UILanguage(primary: .german) == .english)  // jak na Linuksie: interfejs tylko po polsku albo po angielsku
    }

    @Test func englishCatalogueLoadsFromTheRepository() {
        #expect(UI.english["Zapisz"] == "Save")
        #expect(UI.english["Odstęp"] == "Spacing")  // dynamic.json: powód zmiany tłumaczony przy pokazaniu
        #expect(UI.english.values.allSatisfy { !$0.isEmpty })
    }

    @Test func polishPluralForms() {
        let forms = ("poprawka", "poprawki", "poprawek")
        let english = ("change", "changes")
        #expect([1, 2, 4, 5, 12, 14, 22, 25, 101, 112].map { UI.count($0, forms, english) }
                == ["1 poprawka", "2 poprawki", "4 poprawki", "5 poprawek", "12 poprawek", "14 poprawek", "22 poprawki",
                    "25 poprawek", "101 poprawek", "112 poprawek"])
    }

    @Test func valuesAreInsertedAfterTranslation() {
        // Po polsku klucz jest tekstem; wartość (np. słowo użytkownika) nie przechodzi przez katalog.
        #expect(tr("Zapisano: {shortcut}.", ["shortcut": "⌥⌘G"]) == "Zapisano: ⌥⌘G.")
        #expect(tr("Zapisz") == "Zapisz")
    }
}
