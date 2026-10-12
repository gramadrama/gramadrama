import Foundation
import Testing
@testable import GramaCore

@Suite("Tekst pola")
struct TextEditsTests {
    private func paragraph(_ text: String, _ caret: Int) -> String {
        (text as NSString).substring(with: TextEdits.paragraphRange(in: text, caret: caret))
    }

    @Test func paragraphWithCaret() {
        let note = "Pierwszy akapit.\nDrugi akapit z bledem.\n\nTrzeci."
        #expect(paragraph(note, 20) == "Drugi akapit z bledem.")
        #expect(paragraph(note, 40) == "")
        #expect(paragraph(note, (note as NSString).length) == "Trzeci.")
        #expect(paragraph("a\r\nbłąd\r\nc", 4) == "błąd")
    }

    @Test func minimalSpanCountsUTF16() throws {
        let span = try #require(TextEdits.minimalSpan(from: "🙂 dzieki", to: "🙂 dzięki"))
        #expect(span.prefix == 6 && span.oldLength == 1 && span.replacement == "ę")
        #expect(TextEdits.minimalSpan(from: "ok", to: "ok") == nil)
    }

    @Test func leadingSpace() {
        #expect(TextEdits.leadingSpaceNeeded(before: "Kolejne", after: "."))
        #expect(!TextEdits.leadingSpaceNeeded(before: ", dalej", after: "a"))
        #expect(!TextEdits.leadingSpaceNeeded(before: "tekst", after: " "))
    }
}

@Suite("Język i granica cytatu")
struct LanguageTests {
    @Test func linksDoNotDecideLanguage() {
        #expect(TextLanguage.detect("The details are here: https://docs.example.com/projekt_x/specyfikacja") == .english)
        #expect(TextLanguage.detect("Myślę, że jutro będzie lepiej, zobaczymy.") == .polish)
    }

    /// Regex e-maila miał kiedyś czas sześcienny: 6000 znaków „a@” blokowało aplikację na minuty.
    @Test func hostileTextIsFast() {
        let hostile = String(repeating: "a@", count: 3000)
        let started = Date()
        _ = TextLanguage.detect(hostile)
        _ = PostProcess.clean(hostile + " x", original: hostile)
        #expect(Date().timeIntervalSince(started) < 1)
    }

    @Test func replyKeepsOnlyTheNewMessage() throws {
        let text = "Dzieki, sprawdze jutro.\n\nOd: Jan Kowalski\nWysłano: poniedziałek\nDo: Ja\n\nCzesc, czy mozesz?"
        let range = try #require(ReplyBoundary.newMessageRange(in: text))
        #expect((text as NSString).substring(with: range).trimmed == "Dzieki, sprawdze jutro.")
    }
}

@Suite("Podgląd dyktowania")
struct DictationPreviewTests {
    @Test func confirmedChunksAndPendingRest() {
        var preview = DictationPreviewText()
        preview.setPending(" Hej, zrobiłem", prompt: "")
        #expect(preview.confirmed == "" && preview.pending == "Hej, zrobiłem" && preview.confirmedSamples == 0)

        preview.confirm(" Hej, zrobiłem deploy.", samples: 48_000, prompt: "")
        #expect(preview.confirmed == "Hej, zrobiłem deploy." && preview.pending == "" && preview.confirmedSamples == 48_000)

        preview.setPending("Możesz sprawdzić?", prompt: "")
        preview.confirm("Możesz sprawdzić ten PR?", samples: 32_000, prompt: "")
        preview.setPending("Do jutra", prompt: "")
        #expect(preview.text == "Hej, zrobiłem deploy. Możesz sprawdzić ten PR? Do jutra" && preview.confirmedSamples == 80_000)
        #expect(preview.wordCount == 9)
    }

    @Test func fillersAndHallucinationsAreHidden() {
        var preview = DictationPreviewText()
        preview.confirm("Yyy, spotkanie o 10.00", samples: 1, prompt: "")
        preview.setPending("Dzięki za obejrzenie.", prompt: "")
        #expect(preview.text == "Spotkanie o 10:00")
    }

    @Test func tailStartsAtAWord() {
        var preview = DictationPreviewText()
        preview.confirm("Pierwsze zdanie jest pewne.", samples: 1, prompt: "")
        preview.setPending("Drugie jeszcze nie.", prompt: "")
        #expect(preview.tail(maxCharacters: 100) == ("Pierwsze zdanie jest pewne.", "Drugie jeszcze nie.", false))
        #expect(preview.tail(maxCharacters: 30) == ("pewne.", "Drugie jeszcze nie.", true))
        #expect(preview.tail(maxCharacters: 18) == ("", "jeszcze nie.", true))
        #expect(DictationPreviewText().tail(maxCharacters: 10) == ("", "", false))

        var link = DictationPreviewText()
        link.setPending("https://example.com/bardzo/dlugi/adres", prompt: "")
        #expect(link.tail(maxCharacters: 10) == ("", "lugi/adres", true))
    }

    @Test func polishWordCount() {
        #expect(DictationPreviewText.wordCountLabel(1) == "1 słowo")
        #expect(DictationPreviewText.wordCountLabel(3) == "3 słowa")
        #expect(DictationPreviewText.wordCountLabel(5) == "5 słów")
        #expect(DictationPreviewText.wordCountLabel(12) == "12 słów")
        #expect(DictationPreviewText.wordCountLabel(22) == "22 słowa")
        #expect(DictationPreviewText.wordCountLabel(0) == "0 słów")
    }
}

@Suite("Cięcie nagrania w pauzach")
struct DictationChunkingTests {
    private static let rate = 16_000.0

    /// „Mowa” (szum o danej głośności) i cisza na przemian: [(sekundy, głośność)].
    private func audio(_ parts: [(Double, Float)]) -> [Float] {
        var generator = SystemRandomNumberGenerator()
        return parts.flatMap { seconds, level in
            (0..<Int(seconds * Self.rate)).map { _ in level == 0 ? 0 : Float.random(in: -level...level, using: &generator) }
        }
    }

    /// Miejsce cięcia w sekundach (bez tablicy próbek w komunikacie, gdy test nie przejdzie).
    private func cut(_ parts: [(Double, Float)]) -> Double? {
        DictationChunking.cut(in: audio(parts), sampleRate: Self.rate).map { Double($0) / Self.rate }
    }

    /// Granice kawałków w sekundach przy oknie Whispera bez sekundy ciszy (29 s).
    private func split(_ parts: [(Double, Float)]) -> [ClosedRange<Double>] {
        DictationChunking.split(audio(parts), sampleRate: Self.rate, window: 29)
            .map { Double($0.lowerBound) / Self.rate...Double($0.upperBound) / Self.rate }
    }

    @Test func shortRecordingIsNotCut() {
        #expect(cut([(3, 0.1), (0.5, 0), (1, 0.1)]) == nil)
    }

    @Test func cutsInTheMiddleOfTheLastPause() throws {
        // Pauzy 1,0–1,4 s (za wcześnie), 3,0–3,5 s i 5,5–5,9 s; cięcie w środku ostatniej.
        let seconds = try #require(cut([(1, 0.1), (0.4, 0), (1.6, 0.1), (0.5, 0), (2, 0.1), (0.4, 0), (1, 0.1)]))
        #expect(abs(seconds - 5.7) < 0.03)
    }

    @Test func quietBackgroundStillCountsAsPause() throws {
        // Szum tła 26 dB ciszej niż mowa to nadal pauza, choć głośniejszy od ciszy bezwzględnej.
        let seconds = try #require(cut([(3, 0.1), (0.5, 0.005), (3, 0.1)]))
        #expect(abs(seconds - 3.25) < 0.03)
    }

    @Test func pauseRightAtTheEndIsTooLate() {
        // Pauza kończy się 0,2 s przed końcem — za nią nie byłoby czego rozpoznać.
        #expect(cut([(6, 0.1), (0.4, 0), (0.2, 0.1)]) == nil)
    }

    @Test func speechWithoutPausesIsCutAtTheQuietestPoint() throws {
        #expect(cut([(15, 0.1)]) == nil)
        // 21 s bez pauzy, tylko krótkie (0,1 s) ściszenie w 12. sekundzie.
        let seconds = try #require(cut([(12, 0.1), (0.1, 0.02), (8.9, 0.1)]))
        #expect(abs(seconds - 12.05) < 0.05)
    }

    @Test func silenceIsNotSpeech() {
        #expect(!DictationChunking.hasSpeech(audio([(2, 0)]), sampleRate: Self.rate, threshold: 0.003))
        #expect(DictationChunking.hasSpeech(audio([(1, 0), (0.1, 0.1)]), sampleRate: Self.rate, threshold: 0.003))
    }

    @Test func recordingWithinTheWindowIsOnePiece() {
        let pieces = split([(20, 0.1), (0.5, 0), (8, 0.1)])
        #expect(pieces.count == 1)
        #expect(abs(pieces[0].upperBound - 28.5) < 0.001)
    }

    @Test func longRecordingIsSplitAtTheLastPauseBeforeTheWindowEnds() throws {
        // 37 s z pauzami w 12,0–12,5 s i 22,5–23,0 s: pierwszy kawałek do środka drugiej, reszta (14 s) w jednym.
        let pieces = split([(12, 0.1), (0.5, 0), (10, 0.1), (0.5, 0), (14, 0.1)])
        try #require(pieces.count == 2)
        #expect(pieces[0].lowerBound == 0)
        #expect(abs(pieces[0].upperBound - 22.75) < 0.03)
        #expect(pieces[1].lowerBound == pieces[0].upperBound)
        #expect(abs(pieces[1].upperBound - 37) < 0.001)
    }

    @Test func speechWithoutPausesIsSplitAtTheQuietestPoint() throws {
        // 40 s bez pauzy, krótkie ściszenie w 25. sekundzie.
        let pieces = split([(25, 0.1), (0.1, 0.02), (14.9, 0.1)])
        try #require(pieces.count == 2)
        #expect(abs(pieces[0].upperBound - 25.05) < 0.05)
    }

    @Test func everyPieceFitsInTheWindowAndNothingIsLost() {
        // 3 minuty (najdłuższe nagranie): 6,5 s mowy i 0,5 s pauzy na przemian.
        let pieces = split(Array(repeating: [(6.5, Float(0.1)), (0.5, Float(0))], count: 26).flatMap { $0 })
        #expect(pieces.count >= 7)
        #expect(pieces.first?.lowerBound == 0)
        #expect(abs((pieces.last?.upperBound ?? 0) - 182) < 0.001)
        for (previous, next) in zip(pieces, pieces.dropFirst()) { #expect(previous.upperBound == next.lowerBound) }
        #expect(pieces.allSatisfy { $0.upperBound - $0.lowerBound <= 29 })
    }
}
