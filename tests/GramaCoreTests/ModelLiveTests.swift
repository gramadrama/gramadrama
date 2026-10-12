import Foundation
import Testing
@testable import GramaCore

@Suite("Tryb „tylko model”: fragmenty")
struct ModelLiveFragmentTests {
    private func texts(_ text: String) -> [String] { ModelLive.fragments(in: text).map(\.text) }

    @Test func longSentencesStandAlone() {
        let first = "Wczoraj wieczorem długo pracowałem nad raportem kwartalnym."
        let second = "Dzisiaj rano wysłałem go do całego zespołu."
        #expect(texts(first + " " + second) == [first, second])
    }

    @Test func rangesAreUTF16AndMatchTheText() {
        let text = "  Zażółć gęślą jaźń 🙂, a potem napisz drugie zdanie po polsku.\nI jeszcze trzecie zdanie w nowej linii, też długie."
        let ns = text as NSString
        let fragments = ModelLive.fragments(in: text)
        #expect(fragments.count == 2)
        for fragment in fragments {
            #expect(ns.substring(with: fragment.range) == fragment.text)
            #expect(fragment.text == fragment.text.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    @Test func shortSentencesJoinTheNextOne() {
        #expect(texts("Ok. Dzięki! Wyślę raport jutro rano, jak tylko skończę poprawki.")
                == ["Ok. Dzięki! Wyślę raport jutro rano, jak tylko skończę poprawki."])
    }

    /// Zmiana dalszego zdania nie przesuwa granic wcześniejszych — te fragmenty dalej trafiają w pamięć wyników.
    @Test func editingALaterSentenceKeepsEarlierFragments() {
        let first = "Pierwsze zdanie jest wystarczająco długie, żeby stać osobno."
        let a = ModelLive.fragments(in: first + " Drugie zdanie też ma odpowiednią długość tekstu.")
        let b = ModelLive.fragments(in: first + " Drugie zdanie zmieniło się teraz całkiem mocno, i jest dłuższe.")
        #expect(a.first == b.first)
    }

    @Test func neverAcrossLineBreaks() {
        #expect(texts("Pierwsza linia bez kropki\r\nDruga linia\u{2028}Trzecia\u{0B}Czwarta") ==
                ["Pierwsza linia bez kropki", "Druga linia", "Trzecia", "Czwarta"])
    }

    @Test func abbreviationsInitialsAndNumbersDoNotEndASentence() {
        func sentences(_ text: String) -> [String] {
            let ns = text as NSString
            return ModelLive.sentences(in: ns, line: NSRange(location: 0, length: ns.length)).map(ns.substring(with:))
        }
        #expect(sentences("To jest m.in. ważne. Dalej.") == ["To jest m.in. ważne.", "Dalej."])
        #expect(sentences("Przyjdę np. jutro. Potem.") == ["Przyjdę np. jutro.", "Potem."])
        #expect(sentences("Jan K. przyjdzie. Na pewno?") == ["Jan K. przyjdzie.", "Na pewno?"])
        #expect(sentences("Wersja 1.5 działa. Kolejne.") == ["Wersja 1.5 działa.", "Kolejne."])
        #expect(sentences("Co? Naprawdę!!! Tak… Koniec") == ["Co?", "Naprawdę!!!", "Tak…", "Koniec"])
        #expect(sentences("Powiedział „tak.” Potem wyszedł.") == ["Powiedział „tak.”", "Potem wyszedł."])
    }

    @Test func longSentenceIsSplitAtSpaces() {
        let words = Array(repeating: "słowo", count: 120).joined(separator: " ")
        let fragments = ModelLive.fragments(in: words + ".")
        #expect(fragments.count > 1)
        #expect(fragments.allSatisfy { $0.range.length <= ModelLive.maxFragmentLength })
        #expect(fragments.map(\.text).joined(separator: " ") == words + ".")
    }

    @Test func wordWithoutSpacesIsCutOnACharacterBoundary() {
        let text = String(repeating: "a", count: 319) + "🙂" + String(repeating: "b", count: 100)
        let fragments = ModelLive.fragments(in: text)
        #expect(fragments.count == 2)
        #expect(fragments.map(\.text).joined() == text)
        #expect(fragments.allSatisfy { $0.text.utf16.count == $0.range.length && $0.range.length <= ModelLive.maxFragmentLength })
    }

    @Test func fragmentsWithoutWordsAreSkipped() {
        #expect(texts("https://example.com/a_b\n:tada: 🎉\nTo jest tekst do sprawdzenia, całkiem zwyczajny.")
                == ["To jest tekst do sprawdzenia, całkiem zwyczajny."])
    }

    @Test func orderStartsAtTheCaret() {
        let fragments = [NSRange(location: 0, length: 10), NSRange(location: 12, length: 8), NSRange(location: 22, length: 8)]
            .map { ModelLive.Fragment(range: $0, text: "") }
        #expect(ModelLive.order(fragments, caret: 25) == [2, 1, 0])
        #expect(ModelLive.order(fragments, caret: nil) == [0, 1, 2])
        #expect(ModelLive.order(fragments, caret: 11) == [0, 1, 2])
        #expect(ModelLive.order(fragments, caret: 15) == [1, 0, 2])
    }
}

@Suite("Tryb „tylko model”: odpowiedź modelu")
struct ModelLiveVerdictTests {
    @Test func minimalCorrectionIsAccepted() {
        #expect(ModelLive.verdict("Balon opadł, gdy ucichł wiatr.", fragment: "Balon opadl gdy ucichł wiatr.")
                == .corrected("Balon opadł, gdy ucichł wiatr."))
        #expect(ModelLive.verdict("Dzięki za pomoc, jutro się odezwę.", fragment: "Dzieki za pomoc, jutro sie odezwe.")
                == .corrected("Dzięki za pomoc, jutro się odezwę."))
    }

    @Test func echoedMarkersAreRemoved() {
        #expect(ModelLive.verdict("<<<TEKST>>>\nKot ma Alę.\n<<<KONIEC TEKSTU>>>", fragment: "Kot ma ale.") == .corrected("Kot ma Alę."))
    }

    @Test func emptyOrCommentaryIsNotAClean() {
        #expect(ModelLive.verdict("  ", fragment: "Kot ma Alę.") == .rejected("empty"))
        #expect(ModelLive.verdict("Tekst nie zawiera błędów.", fragment: "Jutro wyślę raport do zespołu.") == .rejected("empty"))
    }

    @Test func foreignScriptAsksForAnotherTry() {
        #expect(ModelLive.verdict("Jutro pいつze raport.", fragment: "Jutro piszę raport.") == .retry("script"))
    }

    @Test func otherRejections() {
        #expect(ModelLive.verdict("Kot ma\nAlę.", fragment: "Kot ma ale.") == .rejected("lines"))
        #expect(ModelLive.verdict("Zobacz https://example.com/ab teraz.", fragment: "Zobacz https://example.com/a_b teraz.")
                == .rejected("protected"))
        #expect(ModelLive.verdict("I will send the report tomorrow morning.", fragment: "Wyślę raport jutro rano.")
                == .rejected("rewritten"))
    }

    /// Zgubione „@” przed wzmianką PostProcess przywraca — to naprawiona odpowiedź, nie odrzucona.
    @Test func repairedMentionIsAccepted() {
        #expect(ModelLive.verdict("Kasia, sprawdzisz to dziś?", fragment: "@Kasia sprawdzisz to dzis?") == .corrected("@Kasia, sprawdzisz to dziś?"))
    }
}

@Suite("Tryb „tylko model”: wyniki")
struct ModelLiveResultTests {
    @Test func issuesUseDocumentRangesAndCategories() {
        let fragment = ModelLive.Fragment(range: NSRange(location: 10, length: 29), text: "Balon opadl gdy ucichł wiatr.")
        let issues = ModelLive.issues(for: fragment, corrected: "Balon opadł, gdy ucichł wiatr.", nonLanguage: [])
        #expect(issues.count == 1)
        #expect(issues.first?.range == NSRange(location: 16, length: 5))
        #expect(issues.first?.suggestions == ["opadł,"])
        #expect(issues.first?.source == .model)

        let spelling = ModelLive.issues(for: ModelLive.Fragment(range: NSRange(location: 0, length: 14), text: "Dzieki wielkie"),
                                        corrected: "Dzięki wielkie", nonLanguage: [])
        #expect(spelling.map(\.rule) == ["MODEL_SPELLING"])
    }

    @Test func issuesOnLinksAreDropped() {
        let text = "Tu masz https://example.com/a link."
        let fragment = ModelLive.Fragment(range: NSRange(location: 0, length: (text as NSString).length), text: text)
        let issues = ModelLive.issues(for: fragment, corrected: "Tu masz https://example.com/A link.",
                                      nonLanguage: LiveText.nonLanguageMask(text))
        #expect(issues.isEmpty)
    }

    @Test func assembleReplacesOnlyCorrectedFragments() {
        let text = "Ala ma kota.  Kot ma ale."
        let fragments = [ModelLive.Fragment(range: NSRange(location: 0, length: 12), text: "Ala ma kota."),
                         ModelLive.Fragment(range: NSRange(location: 14, length: 11), text: "Kot ma ale.")]
        #expect(ModelLive.assemble(text, fragments: fragments, corrections: [nil, "Kot ma Alę."]) == "Ala ma kota.  Kot ma Alę.")
    }
}

@Suite("Odpowiedź modelu: obce pismo i chronione fragmenty")
struct ForeignLettersTests {
    @Test func lettersOfAnUnusedScript() {
        #expect(PostProcess.foreignLetters("pいつze", original: "piszę") == ["い", "つ"])
        #expect(PostProcess.foreignLetters("Привет, świecie", original: "Привет świecie").isEmpty)
        #expect(PostProcess.foreignLetters("Zażółć", original: "Zazolc").isEmpty)
        #expect(PostProcess.foreignLetters("ɐ", original: "a").isEmpty)
    }

    @Test func protectedCountsIgnoreSentencePunctuationAfterLinks() {
        let counts = PostProcess.protectedCounts("Zobacz https://example.com/a. @Kasia :tada: 🙂")
        #expect(counts["https://example.com/a"] == 1)
        #expect(counts["@Kasia"] == 1)
        #expect(counts[":tada:"] == 1)
        #expect(counts["🙂"] == 1)
    }
}
