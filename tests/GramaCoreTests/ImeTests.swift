import Foundation
import Testing
@testable import GramaCore
@testable import GramaIMEProtocol

@Suite("Metoda wprowadzania: zakres")
struct ImeScopeTests {
    /// Odpowiedź `context` z oknem zaczynającym się w `start` dokumentu.
    private func reply(before: String, selected: String = "", after: String = "", start: Int = 0,
                       shortAfter: Bool = true) -> ImeProtocol.Reply {
        var reply = ImeProtocol.Reply(id: "t", ok: true)
        reply.windowStart = start
        reply.before = before
        reply.selected = selected
        reply.after = after
        reply.selectionLocation = start + (before as NSString).length
        reply.selectionLength = (selected as NSString).length
        reply.shortAfter = shortAfter
        return reply
    }

    private func first(_ reply: ImeProtocol.Reply, whole: Bool = false) throws -> (String, ImeScope) {
        let candidate = try #require(try ImeScope.candidates(reply, wholeFieldWhenComplete: whole).first)
        return (candidate.text, candidate.scope)
    }

    @Test func selectionWins() throws {
        #expect(try first(reply(before: "Ala ", selected: "ma kota", after: " i psa")) == ("ma kota", .selection))
        #expect(throws: ImeScope.Refusal.noText) { try ImeScope.candidates(reply(before: "a", selected: "  "), wholeFieldWhenComplete: false) }
    }

    @Test func paragraphWithCaret() throws {
        #expect(try first(reply(before: "Pierwszy.\nDrugi z bled", after: "em.\nTrzeci.")) == ("Drugi z bledem.", .paragraph))
        #expect(try first(reply(before: "Jedno zdanie", after: " w polu.")) == ("Jedno zdanie w polu.", .wholeField))
        #expect(throws: ImeScope.Refusal.emptyParagraph) {
            try ImeScope.candidates(reply(before: "Tekst.\n", after: "\nDalej."), wholeFieldWhenComplete: false)
        }
    }

    @Test func documentRangeInScrolledWindow() throws {
        // Okno od 100 znaku dokumentu, akapit ograniczony widocznymi końcami linii.
        let r = reply(before: "koniec.\nAkapit z kur", after: "sorem.\nNastępny.", start: 100, shortAfter: false)
        let candidate = try #require(try ImeScope.candidates(r, wholeFieldWhenComplete: false).first)
        #expect(candidate.scope == .paragraph)
        #expect(candidate.range == NSRange(location: 108, length: 18))
        #expect(candidate.text == "Akapit z kursorem.")
    }

    @Test func chromiumLikeWindowIsNotTheWholeDocument() {
        // Aplikacja oddała mniej, niż prosiliśmy, ale bez końca dokumentu (shortAfter == false): akapit ucięty.
        let cut = reply(before: String(repeating: "słowo ", count: 10), after: "dalej bez końca", start: 0, shortAfter: false)
        #expect(throws: ImeScope.Refusal.cut) { try ImeScope.candidates(cut, wholeFieldWhenComplete: false) }
        // Okno zaczyna się w środku akapitu (brak końca linii przed kursorem i start > 0): też ucięty.
        let cutStart = reply(before: "środek akapitu", after: " koniec.\n", start: 50)
        #expect(throws: ImeScope.Refusal.cut) { try ImeScope.candidates(cutStart, wholeFieldWhenComplete: false) }
    }

    @Test func outlookReplyOnlyNewMessage() throws {
        let body = "Cześć Aniu,\nwysyłam poprawki.\n\nOd: Anna Nowak <anna@example.com>\nWysłano: poniedziałek\nDo: Tomasz\nTemat: Raport\n\nHej?"
        let caret = ("Cześć Aniu,\nwysy" as NSString).length
        let ns = body as NSString
        let r = reply(before: ns.substring(to: caret), after: ns.substring(from: caret))
        #expect(try first(r) == ("Cześć Aniu,\nwysyłam poprawki.", .newMessage))
    }

    @Test func mailWithoutQuoteIsTheWholeMessage() throws {
        let r = reply(before: "Cześć,\nwysyłam raport", after: ".\nPozdrawiam")
        #expect(try first(r, whole: true) == ("Cześć,\nwysyłam raport.\nPozdrawiam", .wholeField))
        #expect(try first(r, whole: false) == ("wysyłam raport.", .paragraph))
    }

    @Test func emojiFragmentsAreSkipped() throws {
        // Nowa wiadomość z obrazkiem (U+FFFC) — zostaje akapit z kursorem bez obrazka.
        let r = reply(before: "Hej \u{FFFC}\nDruga linia z bled", after: "em.\n\nOd: A <a@b.pl>\nWysłano: dziś\nDo: B\nTemat: X\n\nstare")
        let candidates = try ImeScope.candidates(r, wholeFieldWhenComplete: true)
        #expect(candidates.map(\.scope) == [.paragraph])
        #expect(candidates.first?.text == "Druga linia z bledem.")
        #expect(throws: ImeScope.Refusal.attachments) {
            try ImeScope.candidates(reply(before: "Tylko \u{FFFC} tu"), wholeFieldWhenComplete: false)
        }
    }

    @Test func inconsistentRepliesFallBack() {
        var wrongStart = reply(before: "abc")
        wrongStart.windowStart = 5  // start + before ≠ zaznaczenie
        #expect(throws: ImeScope.Refusal.noText) { try ImeScope.candidates(wrongStart, wholeFieldWhenComplete: false) }
        var wrongSelection = reply(before: "Ala ", selected: "ma")
        wrongSelection.selectionLength = 50
        #expect(throws: ImeScope.Refusal.noText) { try ImeScope.candidates(wrongSelection, wholeFieldWhenComplete: false) }
        var tooLong = reply(before: "a", selected: "b")
        tooLong.selectionTooLong = true
        tooLong.selectionLength = 9000
        #expect(throws: ImeScope.Refusal.tooLong(9000)) { try ImeScope.candidates(tooLong, wholeFieldWhenComplete: false) }
        #expect(throws: ImeScope.Refusal.noText) { try ImeScope.candidates(reply(before: "  "), wholeFieldWhenComplete: false) }
    }
}

@Suite("Metoda wprowadzania: podmiany")
struct ImeEditsTests {
    private func apply(_ edits: [ImeProtocol.Edit], to text: String, base: Int = 0) -> String {
        let result = NSMutableString(string: text)
        for edit in edits {  // już od końca
            result.replaceCharacters(in: NSRange(location: edit.location - base, length: edit.length), with: edit.text)
        }
        return result as String
    }

    @Test func wordHunksFromTheEnd() throws {
        let original = "Wysylam poprawiona wersje raportu, zobacz tutaj: https://example.com i daj znac."
        let corrected = "Wysyłam poprawioną wersję raportu, zobacz tutaj: https://example.com i daj znać."
        let plan = try #require(try ImeEdits.plan(original: original, corrected: corrected, base: 40))
        #expect(plan.expected == corrected)
        #expect(plan.edits.count == 4)
        #expect(plan.edits.map(\.location) == plan.edits.map(\.location).sorted(by: >))
        // Link między poprawkami zostaje nietknięty.
        #expect(!plan.edits.contains { $0.text.contains("https") })
        #expect(apply(plan.edits, to: original, base: 40) == corrected)
    }

    @Test func noChangeIsNil() throws {
        #expect(try ImeEdits.plan(original: "Bez zmian.", corrected: "Bez zmian.", base: 0) == nil)
    }

    @Test func deletionsAndInsertions() throws {
        for (a, b) in [("Wiem że tak", "Wiem, że tak"), ("ze zdjęciem", "z zdjęciem"), ("a  b", "a b")] {
            let plan = try #require(try ImeEdits.plan(original: a, corrected: b, base: 7))
            #expect(apply(plan.edits, to: a, base: 7) == b)
        }
    }

    @Test func powerPointSeparatorsAreKept() throws {
        // PowerPoint: ⇧↩ to U+000B, ↩ to CR. Model zwraca „\n”.
        let original = "Raz\u{0B}Dwa z bledem\rTrzy"
        let plan = try #require(try ImeEdits.plan(original: original, corrected: "Raz\nDwa z błędem\nTrzy", base: 0))
        #expect(plan.expected == "Raz\u{0B}Dwa z błędem\rTrzy")
        #expect(plan.edits.count == 1)
        #expect(plan.edits.allSatisfy { !$0.text.contains("\n") && !$0.text.contains("\u{0B}") })
        let crlf = try #require(try ImeEdits.plan(original: "a\r\nbledem", corrected: "a\nbłędem", base: 0))
        #expect(crlf.expected == "a\r\nbłędem")
    }

    @Test func changedLineStructureIsRefused() {
        #expect(throws: ImeEdits.Failure.lineStructureChanged) {
            try ImeEdits.plan(original: "Jedna linia i druga", corrected: "Jedna linia\ni druga", base: 0)
        }
    }

    @Test func manyChangesBecomeOneSpan() throws {
        let original = (1...20).map { "slowo\($0)" }.joined(separator: " ")
        let corrected = (1...20).map { "słowo\($0)" }.joined(separator: " ")
        let plan = try #require(try ImeEdits.plan(original: original, corrected: corrected, base: 0))
        #expect(plan.edits.count == 1)
        #expect(apply(plan.edits, to: original) == corrected)
    }

    @Test func emojiAndCombiningMarks() throws {
        // UTF-16: emoji to dwie jednostki, „ę” rozłożone to dwa znaki Unicode — przesunięcia muszą się zgadzać.
        let original = "Hej 😀 dzie\u{0301}kuje bardzo"
        let corrected = "Hej 😀 dziękuję bardzo"
        let plan = try #require(try ImeEdits.plan(original: original, corrected: corrected, base: 3))
        #expect(apply(plan.edits, to: original, base: 3) == corrected)
    }
}

@Suite("Metoda wprowadzania: protokół i tabela aplikacji")
struct ImeProtocolTests {
    @Test func roundTripKeepsNewlinesOnOneLine() throws {
        var request = ImeProtocol.Request(op: .replace)
        request.edits = [ImeProtocol.Edit(location: 3, length: 4, text: "zażółć\ngęślą\u{2028}jaźń")]
        let line = try #require(ImeProtocol.encode(request))
        #expect(line.last == 0x0A)
        #expect(!line.dropLast().contains(0x0A))
        let decoded = try #require(ImeProtocol.decode(ImeProtocol.Request.self, from: line.dropLast()))
        #expect(decoded.edits == request.edits)
        #expect(decoded.id == request.id)
    }

    @Test func missingOptionalFieldsStillDecode() throws {
        // Starsza albo nowsza strona może pominąć pola — dekodujemy, dopóki są v, id i op/ok.
        let request = try #require(ImeProtocol.decode(ImeProtocol.Request.self, from: Data(#"{"v":1,"id":"a","op":"context"}"#.utf8)))
        #expect(request.maxBefore == nil)
        let reply = try #require(ImeProtocol.decode(ImeProtocol.Reply.self, from: Data(#"{"v":1,"id":"a","ok":false,"reason":"x","future":1}"#.utf8)))
        #expect(reply.reason == "x")
        #expect(ImeProtocol.decode(ImeProtocol.Request.self, from: Data(#"{"v":1,"id":"a","op":"push"}"#.utf8)) == nil)
    }

    @Test func policy() {
        #expect(ImePolicy.rule(for: "com.microsoft.Powerpoint", chromium: false)?.capture == true)
        #expect(ImePolicy.rule(for: "com.microsoft.Outlook", chromium: false)?.wholeFieldWhenComplete == true)
        #expect(ImePolicy.rule(for: "com.microsoft.Outlook", chromium: false)?.restoreCaret == true)
        let slack = ImePolicy.rule(for: "com.tinyspeck.slackmacgap", chromium: false)
        #expect(slack?.capture == false && slack?.insert == true)
        #expect(ImePolicy.rule(for: "com.example.electron", chromium: true)?.capture == false)
        #expect(ImePolicy.rule(for: "com.apple.Terminal", chromium: false) == nil)
        #expect(ImePolicy.rule(for: ImeProtocol.appBundleID, chromium: false) == nil)
        #expect(ImePolicy.rule(for: nil, chromium: false) == nil)
    }

    @Test func leadingSpace() {
        #expect(ImeText.leadingSpaceNeeded(before: "Kolejne", after: "."))
        #expect(!ImeText.leadingSpaceNeeded(before: "Kolejne", after: " "))
        #expect(!ImeText.leadingSpaceNeeded(before: ", dalej", after: "a"))
        #expect(!ImeText.leadingSpaceNeeded(before: "x", after: nil))
    }
}
