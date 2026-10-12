import Foundation
import Testing
@testable import GramaCore

@Suite("Języki tekstu")
struct TextLanguagesTests {
    let germanEnglish = TextLanguages(primary: .german, secondary: .english)

    @Test func theChoiceIsAlwaysUsable() {
        #expect(TextLanguages.standard.all == [.polish, .english])
        #expect(TextLanguages(primary: .german, secondary: .german).all == [.german])  // dwa takie same — jeden
        #expect(TextLanguages(primary: .mixed, secondary: .english).all == [.polish, .english])  // „mieszany” to nie wybór
        #expect(TextLanguages(primary: .english, secondary: nil).translates == false)
        #expect(germanEnglish.other(than: .german) == .english && germanEnglish.other(than: .english) == .german)
        #expect(germanEnglish.pairLabel == "DE ↔ EN")
    }

    @Test func detectionKeepsToTheChosenLanguages() {
        #expect(TextLanguage.detect("Ich denke, dass wir die Datei morgen schicken können.", among: germanEnglish) == .german)
        #expect(TextLanguage.detect("I think we can send the file tomorrow.", among: germanEnglish) == .english)
        // Jeden wybrany język to zawsze on, także dla tekstu w innym.
        #expect(TextLanguage.detect("Myślę, że jutro wyślemy plik.", among: TextLanguages(primary: .english, secondary: nil)) == .english)
        // Para PL+EN jak dotąd.
        #expect(TextLanguage.detect("Myślę, że jutro będzie lepiej, zobaczymy.") == .polish)
    }

    @Test func translationGoesBetweenThePair() {
        #expect(TranslationTarget.automatic(for: "Ich schicke die Datei morgen.", in: germanEnglish) == .english)
        #expect(TranslationTarget.automatic(for: "I will send the file tomorrow.", in: germanEnglish) == .german)
        #expect(TranslationTarget.english.label(in: germanEnglish) == "DE → EN")
        #expect(TranslationTarget.english.opposite(in: germanEnglish) == .german)
        #expect(TranslationTarget.english.label() == "PL → EN")
        // Surowe wartości zostają takie jak w bench/prompts.golden.json.
        #expect(TranslationTarget(rawValue: "polish") == .polish && TranslationTarget.german.code == "de")
    }

    @Test func liveTextHasAVersionPerChosenLanguage() {
        let text = "Ich denke das wir die Datei morgen schicken. I will check it tomorrow."
        let live = LiveText(text, languages: germanEnglish)
        #expect(live.present == [.german, .english])
        #expect(live.version(.german).hasPrefix("Ich denke") && !live.version(.german).contains("check"))
        #expect(live.version(.english).contains("I will check") && !live.version(.english).contains("Datei"))
        #expect(!live.has(.polish) && live.mask(.polish).count == (text as NSString).length)
        // Para PL+EN: polska litera przesądza, jak dotąd.
        #expect(LiveText.segments("Deploy na staging jest gotowy, sprawdź.").map(\.language) == [.polish])
    }
}
