import Foundation
import Testing
@testable import GramaCore

/// Te same sprawdzenia co `linux/tests/lava.test.mjs` (geometria w `lava.js`).
@Suite("Plakietka: lawa")
struct LavaTests {
    @Test func countAndFinalShape() {
        #expect(Lava.countText(4) == "4")
        #expect(Lava.countText(100) == "99+")
        #expect(Lava.shapeWidth(4) == Lava.height)
        #expect(Lava.shapeWidth(12) > Lava.shapeWidth(4) && Lava.shapeWidth(150) > Lava.shapeWidth(12))
        for n in [1, 9, 12, 99, 150] {
            #expect(Lava.areaWidth(n) >= Lava.shapeWidth(n) && Lava.areaWidth(n) >= 24)
        }
        #expect(Lava.shapeWidth(text: "!") == Lava.height && Lava.areaWidth(text: "99+") == Lava.areaWidth(150))
    }

    @Test func blobsStayInsideForALongCheck() {
        for width in [24, Lava.areaWidth(12)] {
            for ms in stride(from: 0.0, to: 120_000, by: 37) {
                for b in Lava.drift(ms, width: width) {
                    #expect(b.r > 1.5)
                    #expect(b.x - b.r >= 0 && b.x + b.r <= width)
                    #expect(b.y - b.r >= 0 && b.y + b.r <= Lava.height)
                }
            }
        }
    }

    @Test func bridgesComeAndGo() {
        var seen = Set<Int>()
        for ms in stride(from: 0.0, to: 20_000, by: 50) { seen.insert(Lava.bridges(Lava.drift(ms)).count) }
        #expect(seen.contains(0) && seen.contains { $0 >= 1 })
    }

    @Test func bridgeOnlyBetweenCloseBlobs() throws {
        #expect(Lava.bridge(.init(x: 0, y: 0, r: 3), .init(x: 30, y: 0, r: 3)) == nil)
        #expect(Lava.bridge(.init(x: 0, y: 0, r: 6), .init(x: 1, y: 0, r: 2)) == nil)
        let b = try #require(Lava.bridge(.init(x: 0, y: 0, r: 3), .init(x: 7, y: 0, r: 3)))
        for p in [b.p1, b.p2, b.p3, b.p4, b.h1, b.h2, b.h3, b.h4] { #expect(p.0.isFinite && p.1.isFinite) }
    }

    @Test func mergingEndsInTheFinalShape() {
        let start = Lava.drift(1234)
        for b in Lava.merge(start, progress: 1, count: 4) {
            #expect(abs(b.x - 12) < 1e-9 && abs(b.y - Lava.height / 2) < 1e-9 && abs(b.r - Lava.radius) < 1e-9)
        }
        let width = Lava.areaWidth(12)
        let xs = Lava.merge(Lava.drift(1234, width: width), progress: 1, count: 12, width: width).map(\.x).sorted()
        let half = (Lava.shapeWidth(12) - Lava.height) / 2
        #expect(zip(xs, [width / 2 - half, width / 2, width / 2 + half]).allSatisfy { abs($0 - $1) < 1e-9 })
        #expect(Lava.merge(start, progress: 0, count: 4) == start)
        #expect(Lava.easeInOut(-1) == 0 && Lava.easeInOut(2) == 1)
    }

    @Test func lavenderToBrick() {
        let lavender = [0x66, 0x57, 0x9E].map { Double($0) / 255 }, brick = [0xAD, 0x4E, 0x2C].map { Double($0) / 255 }
        #expect(Lava.mix(lavender, brick, 0) == lavender)
        #expect(Lava.mix(lavender, brick, 1) == brick)
        #expect(Lava.mix(lavender, brick, 5) == brick)
        #expect(abs(Lava.breath(0) - 1) < 1e-9 && abs(Lava.breath(Lava.breathMS / 2) - 0.72) < 1e-9)
        #expect(abs(Lava.easeOutBack(1) - 1) < 1e-9 && abs(Lava.easeOutBack(0)) < 1e-9)
    }

    @Test func blobsUntilTheResultThenTheCircle() {
        // LanguageTool odpowiada: bańki zlewają się w koło z liczbą, która wskakuje pod koniec zlewania.
        var lamp = Lava.Lamp.hidden.next(count: 0, checking: true, animations: true, now: 100)
        #expect(lamp == .thinking(began: 100))
        #expect(lamp.next(count: 0, checking: true, animations: true, now: 900) == lamp)
        lamp = lamp.next(count: 2, checking: false, animations: true, now: 1000)
        #expect(lamp == .merging(began: 100, at: 1000, into: "2"))
        #expect(lamp.next(count: 2, checking: false, animations: true, now: 1200) == lamp)
        #expect(lamp.next(count: 2, checking: true, animations: true, now: 1200) == lamp)
        #expect(lamp.settled(at: 1000 + Lava.mergeMS) == lamp)
        #expect(lamp.change(after: 1100) == 1000 + Lava.mergeMS + Lava.tintMS)
        lamp = lamp.settled(at: 1000 + Lava.mergeMS + Lava.tintMS)
        #expect(lamp == .count("2", popped: 1000 + Lava.mergeMS))
        #expect(lamp.change(after: 1000 + Lava.mergeMS + Lava.tintMS) == 1000 + Lava.mergeMS + Lava.numberMS)
        #expect(lamp.change(after: 2000) == nil && !lamp.moves(checking: false, at: 2000))
        // Nowe sprawdzanie przy znanej liczbie: koło zostaje i „oddycha”; nowa liczba wskakuje bez zlewania.
        #expect(lamp.next(count: 2, checking: true, animations: true, now: 3000) == lamp)
        #expect(lamp.moves(checking: true, at: 3000))
        #expect(lamp.next(count: 3, checking: false, animations: true, now: 3500) == .count("3", popped: 3500))
        #expect(Lava.Lamp.count("12", popped: nil).next(count: 150, checking: false, animations: true, now: 1).text == "99+")
    }

    @Test func checkWithoutIssuesFadesAway() {
        let thinking = Lava.Lamp.thinking(began: 0)
        let fading = thinking.next(count: 0, checking: false, animations: true, now: 800)
        #expect(fading == .merging(began: 0, at: 800, into: nil))
        #expect(fading.next(count: 0, checking: false, animations: true, now: 900) == fading)
        #expect(fading.settled(at: 800 + Lava.mergeMS + Lava.fadeMS - 1) == fading)
        #expect(fading.settled(at: 800 + Lava.mergeMS + Lava.fadeMS) == .hidden)
        // Bez lawy (sama warstwa 0, koło z liczbą): nic do pokazania — plakietka się chowa.
        #expect(Lava.Lamp.count("1", popped: nil).next(count: 0, checking: false, animations: true, now: 5) == .hidden)
        #expect(Lava.Lamp.hidden.next(count: 0, checking: false, animations: true, now: 5) == .hidden)
        // Wszystko poprawione, a sprawdzanie trwa: znów bańki.
        #expect(Lava.Lamp.count("1", popped: nil).next(count: 0, checking: true, animations: true, now: 5) == .thinking(began: 5))
    }

    @Test func noteWithoutIssuesAndReducedMotion() {
        #expect(Lava.Lamp.hidden.next(count: 0, note: true, checking: false, animations: true, now: 7) == .count("!", popped: 7))
        #expect(Lava.Lamp.thinking(began: 0).next(count: 0, note: true, checking: true, animations: true, now: 7) == .thinking(began: 0))
        // „Ogranicz ruch”: wynik od razu, bez zlewania i wskakiwania; bez błędów — od razu nic.
        let still = Lava.Lamp.thinking(began: 0)
        #expect(still.next(count: 4, checking: false, animations: false, now: 9) == .count("4", popped: nil))
        #expect(still.next(count: 0, checking: false, animations: false, now: 9) == .hidden)
    }
}
