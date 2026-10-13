import Foundation

/// Geometria „lawy” na plakietce sprawdzania podczas pisania (`linux/gnome-extension/lava.js`, PANEL.md): póki
/// sprawdzanie trwa, trzy lawendowe bańki dryfują i przelewają się w siebie jak wosk w lampie; z wynikiem zlewają się
/// w jeden kształt — koło (pigułkę przy dwóch cyfrach), które robi się ceglane i pokazuje liczbę. Czyste funkcje;
/// rysuje je `LavaBadge`. Współrzędne w punktach od lewego górnego rogu obszaru.
package enum Lava {
    package static let height: Double = 18
    /// Promień końcowego koła i końców pigułki.
    package static let radius = height / 2
    /// Bańki się zlewają.
    package static let mergeMS: Double = 520
    /// Lawenda przechodzi w cegłę.
    package static let tintMS: Double = 200
    /// Liczba wskakuje.
    package static let numberMS: Double = 240
    /// Bez błędów: zlana bańka znika.
    package static let fadeMS: Double = 450
    /// Znana liczba „oddycha” w tym rytmie, póki trwa nowe sprawdzanie.
    package static let breathMS: Double = 1400
    /// Radiany na sekundę najwolniejszego ruchu baniek.
    private static let driftSpeed = 1.15
    private static let blobCount = 3

    package struct Blob: Equatable, Sendable {
        package var x, y, r: Double

        package init(x: Double, y: Double, r: Double) {
            self.x = x
            self.y = y
            self.r = r
        }
    }

    /// Mostek, przez który dwie bliskie bańki przelewają się w siebie (kontur „metaball” 2D): cztery punkty na okręgach
    /// i cztery uchwyty Béziera. Rysowany p1 →(h1, h3)→ p3 → p4 →(h4, h2)→ p2 i domknięty, wypełniany razem z kołami.
    package struct Bridge: Sendable {
        package let p1, p2, p3, p4, h1, h2, h3, h4: (Double, Double)
    }

    /// Tekst w plakietce: liczba, powyżej 99 — „99+”.
    package static func countText(_ count: Int) -> String { count > 99 ? "99+" : String(count) }

    /// Szerokość końcowego kształtu: koło dla jednej cyfry, pigułka dla więcej.
    package static func shapeWidth(_ count: Int) -> Double { shapeWidth(text: countText(count)) }

    /// Szerokość kształtu dla tekstu w nim: liczby albo „!” (na Macu komunikat bez błędów).
    package static func shapeWidth(text: String) -> Double {
        text.count <= 1 ? height : height + 6 * Double(text.count - 1)
    }

    /// Szerokość obszaru: miejsce na dryfujące bańki i na końcowy kształt.
    package static func areaWidth(_ count: Int) -> Double { areaWidth(text: countText(count)) }

    package static func areaWidth(text: String) -> Double { max(24, shapeWidth(text: text)) }

    /// Bańki po `ms` od początku sprawdzania; zawsze wewnątrz obszaru `width` × `height`.
    package static func drift(_ ms: Double, width: Double = 24) -> [Blob] {
        let t = ms / 1000 * driftSpeed
        return (0..<blobCount).map { index in
            let i = Double(index), p = t + i * 2.1
            return Blob(x: width / 2 + 6.2 * sin(p * 1.3 + i), y: height / 2 + 2.6 * sin(p * 1.9 + i * 1.7),
                        r: 2.5 + 0.7 * sin(p * 1.1 + i * 2.4))
        }
    }

    package static func easeInOut(_ p: Double) -> Double {
        let p = min(1, max(0, p))
        return p < 0.5 ? 2 * p * p : 1 - pow(-2 * p + 2, 2) / 2
    }

    /// Bańki w części drogi (`progress` 0…1) od `blobs` do końcowego kształtu dla `count`: środkowa do środka,
    /// skrajne do końców pigułki, każdy promień do `radius`.
    package static func merge(_ blobs: [Blob], progress: Double, count: Int, width: Double = 24) -> [Blob] {
        merge(blobs, progress: progress, shape: shapeWidth(count), width: width)
    }

    /// To samo dla kształtu szerokości `shape` (`shapeWidth(text:)`).
    package static func merge(_ blobs: [Blob], progress: Double, shape: Double, width: Double = 24) -> [Blob] {
        let e = easeInOut(progress)
        let half = (shape - height) / 2
        let order = blobs.indices.sorted { blobs[$0].x < blobs[$1].x }
        return blobs.indices.map { i in
            let b = blobs[i]
            let slot = Double((order.firstIndex(of: i) ?? 1) - 1)
            let x = width / 2 + slot * half, y = height / 2
            return Blob(x: b.x + (x - b.x) * e, y: b.y + (y - b.y) * e, r: b.r + (radius - b.r) * e)
        }
    }

    private static func vec(_ x: Double, _ y: Double, _ angle: Double, _ r: Double) -> (Double, Double) {
        (x + r * cos(angle), y + r * sin(angle))
    }

    /// Mostek między dwiema bańkami albo nil, gdy są za daleko albo jedna mieści w sobie drugą.
    package static func bridge(_ a: Blob, _ b: Blob, spread: Double = 0.5, handle: Double = 2.4) -> Bridge? {
        let d = hypot(b.x - a.x, b.y - a.y)
        if a.r <= 0 || b.r <= 0 || d > a.r + b.r * 2.5 || d <= abs(a.r - b.r) { return nil }
        var u1 = 0.0, u2 = 0.0
        if d < a.r + b.r {
            u1 = acos((a.r * a.r + d * d - b.r * b.r) / (2 * a.r * d))
            u2 = acos((b.r * b.r + d * d - a.r * a.r) / (2 * b.r * d))
        }
        let between = atan2(b.y - a.y, b.x - a.x)
        let maxAngle = acos((a.r - b.r) / d)
        let a1 = between + u1 + (maxAngle - u1) * spread, a2 = between - u1 - (maxAngle - u1) * spread
        let a3 = between + .pi - u2 - (.pi - u2 - maxAngle) * spread
        let a4 = between - .pi + u2 + (.pi - u2 - maxAngle) * spread
        let p1 = vec(a.x, a.y, a1, a.r), p2 = vec(a.x, a.y, a2, a.r)
        let p3 = vec(b.x, b.y, a3, b.r), p4 = vec(b.x, b.y, a4, b.r)
        let total = a.r + b.r
        let k = min(spread * handle, hypot(p1.0 - p3.0, p1.1 - p3.1) / total) * min(1, d * 2 / total)
        return Bridge(p1: p1, p2: p2, p3: p3, p4: p4,
                      h1: vec(p1.0, p1.1, a1 - .pi / 2, a.r * k), h2: vec(p2.0, p2.1, a2 + .pi / 2, a.r * k),
                      h3: vec(p3.0, p3.1, a3 + .pi / 2, b.r * k), h4: vec(p4.0, p4.1, a4 - .pi / 2, b.r * k))
    }

    /// Wszystkie mostki między dość bliskimi bańkami.
    package static func bridges(_ blobs: [Blob]) -> [Bridge] {
        var out: [Bridge] = []
        for i in blobs.indices {
            for j in blobs.indices where j > i {
                if let b = bridge(blobs[i], blobs[j]) { out.append(b) }
            }
        }
        return out
    }

    /// Kolor `f` (0…1) drogi od `a` do `b`, oba [r, g, b] w 0…1.
    package static func mix(_ a: [Double], _ b: [Double], _ f: Double) -> [Double] {
        let f = min(1, max(0, f))
        return zip(a, b).map { $0 + ($1 - $0) * f }
    }

    /// Jasność znanej liczby, która „oddycha” w trakcie nowego sprawdzania (0,72…1).
    package static func breath(_ ms: Double) -> Double {
        0.72 + 0.28 * (0.5 + 0.5 * cos(2 * .pi * ms.truncatingRemainder(dividingBy: breathMS) / breathMS))
    }

    /// Co rysuje plakietka (czasy w ms) i jak przechodzi z wyniku w wynik — `show()` z `live.js`.
    package enum Lamp: Equatable, Sendable {
        case hidden
        /// Bańki dryfują od `began`.
        case thinking(began: Double)
        /// Od `at` bańki zlewają się w kształt z tekstem `into`; nil — bez błędów, zlana bańka znika.
        case merging(began: Double, at: Double, into: String?)
        /// Koło (pigułka) z liczbą albo „!”; `popped` — kiedy tekst wskoczył (nil: od razu, bez animacji).
        case count(String, popped: Double?)

        package var isLava: Bool {
            switch self {
            case .thinking, .merging: true
            case .hidden, .count: false
            }
        }

        /// Tekst w kształcie (także ten, w który bańki właśnie się zlewają).
        package var text: String? {
            switch self {
            case .merging(_, _, let into): into
            case .count(let text, _): text
            case .hidden, .thinking: nil
            }
        }

        /// Stan po nowym wyniku: `count` błędów, `note` — komunikat o sprawdzaniu (bez błędów: „!”), `checking` —
        /// zapytanie do serwera w toku, `animations` — bez „Ogranicz ruch”.
        package func next(count: Int, note: Bool = false, checking: Bool, animations: Bool, now: Double) -> Lamp {
            let text = count > 0 ? countText(count) : note ? "!" : nil
            guard let text else {
                // Sprawdzanie bez błędów: bańki zlewają się i znikają; bez lawy plakietka po prostu się chowa.
                if checking { return thinking(now) }
                return animations && isLava ? merged(into: nil, at: now) : .hidden
            }
            // Nic jeszcze nie wiadomo: bańki dryfują aż do wyniku.
            if checking, count == 0 { return thinking(now) }
            if !checking, animations, isLava { return merged(into: text, at: now) }
            // Zlewanie trwa dalej; wynik z nowego sprawdzenia pokaże jego koniec.
            if case .merging = self { return self }
            // Liczba od razu; nowa wskakuje, a znana zostaje i „oddycha”, póki trwa nowe sprawdzanie.
            return self.text == text ? self : .count(text, popped: animations ? now : nil)
        }

        private func thinking(_ now: Double) -> Lamp {
            if case .thinking = self { return self }
            return .thinking(began: now)
        }

        /// Ten sam wynik w trakcie zlewania (strona przysyła stan co chwilę) nie zaczyna go od nowa.
        private func merged(into text: String?, at now: Double) -> Lamp {
            switch self {
            case .merging(let began, let at, _): .merging(began: began, at: at, into: text)
            case .thinking(let began): .merging(began: began, at: now, into: text)
            case .hidden, .count: .merging(began: now, at: now, into: text)
            }
        }

        /// Po skończonym zlewaniu: koło z liczbą (która właśnie wskoczyła) albo, bez błędów, nic.
        package func settled(at now: Double) -> Lamp {
            guard case .merging(_, let at, let into) = self else { return self }
            if let into { return now >= at + mergeMS + tintMS ? .count(into, popped: at + mergeMS) : self }
            return now >= at + mergeMS + fadeMS ? .hidden : self
        }

        /// Kiedy stan sam się zmieni (koniec zlewania, koniec wskakiwania liczby); nil — nie zmieni się.
        package func change(after now: Double) -> Double? {
            switch self {
            case .merging(_, let at, let into): at + mergeMS + (into == nil ? fadeMS : tintMS)
            case .count(_, let popped?) where now < popped + numberMS: popped + numberMS
            default: nil
            }
        }

        /// Czy potrzebne są klatki (`_syncTimeline` na Linuksie): bańki, zlewanie, wskakująca liczba i liczba, która
        /// „oddycha” w trakcie sprawdzania.
        package func moves(checking: Bool, at now: Double) -> Bool {
            isLava || change(after: now) != nil || (text != nil && checking)
        }
    }

    /// Wskakiwanie liczby (`EASE_OUT_BACK` w GNOME): 0 → 1 z lekkim przestrzeleniem.
    package static func easeOutBack(_ p: Double) -> Double {
        let p = min(1, max(0, p)), c1 = 1.70158, c3 = c1 + 1
        return 1 + c3 * pow(p - 1, 3) + c1 * pow(p - 1, 2)
    }
}
