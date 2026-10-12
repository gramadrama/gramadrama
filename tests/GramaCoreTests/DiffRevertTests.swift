import Foundation
import Testing
@testable import GramaCore

@Suite("Panel: cofanie pojedynczych zmian")
struct DiffRevertTests {
    let original = "Jutro wysle raport do Kowalsky i w piatek omowimy plan."
    let corrected = "Jutro wyślę raport do Kowalskiego i w piątek omówimy plan."

    @Test func eachChangeRevertsOnItsOwn() throws {
        let segments = Diff.segments(from: original, to: corrected)
        let changes = Diff.changes(segments, original: original)
        let name = try #require(changes.first { $0.from == "Kowalsky" })
        #expect(name.isWordChange)
        // Nazwisko zostaje jak w oryginale, reszta poprawek też zostaje.
        #expect(Diff.text(segments, reverting: [name.segments]) == "Jutro wyślę raport do Kowalsky i w piątek omówimy plan.")
        #expect(Diff.text(segments) == corrected)
        #expect(Diff.text(segments, reverting: changes.map(\.segments)) == original)
    }

    /// Przecinek dopisany tuż za słowem należy do tej samej zmiany (grupowanie jak `core.changes` na Linuksie):
    /// „Cofnij” cofa oba, a to już nie jest sama zmiana słowa.
    @Test func aWordWithAnAddedCommaIsOneChange() throws {
        let original = "do Kowalsky a jutro", corrected = "do Kowalskiego, a jutro"
        let segments = Diff.segments(from: original, to: corrected)
        let change = try #require(Diff.changes(segments, original: original).first)
        #expect(change.from == "Kowalsky" && change.to == "Kowalskiego," && !change.isWordChange)
        #expect(Diff.text(segments, reverting: [change.segments]) == original)
    }

    @Test func aGroupAcrossOneSpaceRevertsWhole() throws {
        let segments = Diff.segments(from: "w cale nie", to: "wcale nie")
        let changes = Diff.changes(segments, original: "w cale nie")
        let change = try #require(changes.first)
        #expect(!change.isWordChange)  // dwa słowa w oryginale — tylko „Cofnij”
        #expect(Diff.text(segments, reverting: [change.segments]) == "w cale nie")
    }

    @Test func punctuationIsNoWordChange() throws {
        let changes = Diff.changes(Diff.segments(from: "tak a nie", to: "tak, a nie"), original: "tak a nie")
        #expect(changes.count == 1 && !changes[0].isWordChange)
    }
}

@Suite("Panel: zmiany na tle szałwii w wyniku")
struct DiffMarksTests {
    @Test func marksAreTheAddedTextInTheResult() {
        let result = "Jutro wyślę raport, a w piątek plan."
        let segments = Diff.segments(from: "Jutro wysle raport a w piatek plan.", to: result)
        let marked = Diff.marks(segments).map { (result as NSString).substring(with: $0) }
        // Usunięte „wysle” i „piatek” nie zajmują miejsca: zakresy wskazują sam wynik.
        #expect(marked == ["wyślę", ",", "piątek"])
    }

    /// Ta sama poprawka w dwóch kształtach, które daje diff słów zależnie od wersji Swifta (6.3 lokalnie, macos-15
    /// w CI): wspólna spacja po jednej albo po drugiej stronie. Zaznaczenie ma być w obu takie samo.
    @Test func marksDoNotDependOnWhereTheDiffPutsTheSpace() {
        let result = "Jutro wyślę cały raport."
        let joined: [DiffSegment] = [.same("Jutro "), .removed("wysle"), .added("wyślę cały"), .same(" raport.")]
        let split: [DiffSegment] = [.same("Jutro "), .removed("wysle"), .added("wyślę"), .same(" "), .added("cały "), .same("raport.")]
        for segments in [joined, split] {
            #expect(Diff.marks(segments).map { (result as NSString).substring(with: $0) } == ["wyślę cały"])
        }
    }

    @Test func anAddedSpaceAloneStaysMarkedAndLinesAreNotJoined() {
        // „wcale” → „w cale”: sama spacja to poprawka, więc zostaje zaznaczona.
        #expect(Diff.marks([.same("w"), .added(" "), .same("cale")]) == [NSRange(location: 1, length: 1)])
        // Zmiany w dwóch liniach zostają osobno.
        #expect(Diff.marks([.added("Raz"), .same("\n"), .added("Dwa")]) == [NSRange(location: 0, length: 3), NSRange(location: 4, length: 3)])
    }

    @Test func editMarksFollowTheEditedText() {
        let original = "Jutro wysle raport."
        let edited = "Jutro wyślę cały raport."
        let marked = Diff.marks(from: original, to: edited).map { (edited as NSString).substring(with: $0) }
        // Poprawione słowo i dopisane tuż za nim to jeden ciąg zmian.
        #expect(marked == ["wyślę cały"])
        // Nic poza oryginałem — nic na szałwii; za długi tekst — bez porównania po każdym klawiszu.
        #expect(Diff.marks(from: original, to: original).isEmpty)
        let long = String(repeating: "a ", count: Diff.editMarksLimit)
        #expect(Diff.marks(from: "b", to: long).isEmpty)
    }
}
