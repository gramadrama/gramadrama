import Foundation

package enum DiffSegment: Equatable {
    case same(String)
    case removed(String)
    case added(String)
}

package struct Change: Identifiable {
    package let id = UUID()
    package let from: String
    package let to: String
    package let reason: String
    /// Segmenty różnic (`Diff.segments`), z których powstała ta zmiana — do cofnięcia jej osobno (`Diff.text`).
    package var segments: Range<Int> = 0..<0

    /// Pojedyncze słowo zmienione na inne (pisownia, polskie litery) — można je zostawić i dodać do słownika.
    package var isWordChange: Bool {
        !from.isEmpty && !to.isEmpty && [from, to].allSatisfy { word in
            word.count > 1 && word.allSatisfy { $0.isLetter || $0 == "-" || $0 == "'" || $0 == "’" }
        }
    }
}

package enum Diff {
    /// Diff na poziomie słów (tokeny: słowa, białe znaki, pojedyncze znaki interpunkcyjne).
    package static func segments(from old: String, to new: String) -> [DiffSegment] {
        let a = tokenize(old)
        let b = tokenize(new)
        let difference = b.difference(from: a)
        var removed = Set<Int>()
        var inserted = Set<Int>()
        for change in difference {
            switch change {
            case .remove(let offset, _, _): removed.insert(offset)
            case .insert(let offset, _, _): inserted.insert(offset)
            }
        }

        var result: [DiffSegment] = []
        func append(_ segment: DiffSegment) {
            switch (result.last, segment) {
            case (.same(let x)?, .same(let y)): result[result.count - 1] = .same(x + y)
            case (.removed(let x)?, .removed(let y)): result[result.count - 1] = .removed(x + y)
            case (.added(let x)?, .added(let y)): result[result.count - 1] = .added(x + y)
            default: result.append(segment)
            }
        }

        var i = 0, j = 0
        while i < a.count || j < b.count {
            if i < a.count, removed.contains(i) {
                append(.removed(a[i])); i += 1
            } else if j < b.count, inserted.contains(j) {
                append(.added(b[j])); j += 1
            } else {
                if i < a.count { append(.same(a[i])) }
                i += 1; j += 1
            }
        }
        return result
    }

    /// Grupuje sąsiednie usunięcia/dodania w zmiany z krótkim wyjaśnieniem.
    package static func changes(_ segments: [DiffSegment], original: String, hints: [LTMatch] = []) -> [Change] {
        var changes: [Change] = []
        var offset = 0  // UTF-16 w oryginale
        var from = "", to = "", start = 0
        var first = 0, last = 0  // segmenty bieżącej zmiany
        func flush() {
            guard !from.isEmpty || !to.isEmpty else { return }
            let range = start..<max(start + (from as NSString).length, start + 1)
            changes.append(Change(from: from.trimmed, to: to.trimmed, reason: reason(from, to, at: range, hints: hints),
                                  segments: first..<last))
            from = ""; to = ""
        }
        for (index, segment) in segments.enumerated() {
            switch segment {
            case .same(let text):
                // Pojedyncza spacja między dwiema zmianami nie rozbija grupy.
                if text.isBlank, !(from.isEmpty && to.isEmpty), text.count == 1 {
                    from += text; to += text
                } else {
                    flush()
                }
                offset += (text as NSString).length
            case .removed(let text):
                if from.isEmpty && to.isEmpty { start = offset; first = index }
                from += text
                offset += (text as NSString).length
                last = index + 1
            case .added(let text):
                if from.isEmpty && to.isEmpty { start = offset; first = index }
                to += text
                last = index + 1
            }
        }
        flush()
        return changes
    }

    /// Co poprawka zmieniła w wyniku: zakresy (UTF-16) dodanego tekstu, które panel kładzie na tle szałwii
    /// (PANEL.md, „Treść”). Usuniętego tekstu w wyniku nie ma — mówi o nim lista zmian — więc miejsce w polu
    /// to miejsce w wyniku.
    ///
    /// Zmiany rozdzielone samymi spacjami to jedno zaznaczenie, bez spacji na jego brzegach („wyślę cały”, nie
    /// „wyślę” i „cały ”). Diff słów (`CollectionDifference`) może przypisać wspólną spację raz do jednej, raz do
    /// drugiej strony — zależnie od wersji Swifta — więc dopiero to scalenie daje ten sam wygląd w każdym buildzie.
    /// Sama dodana spacja (np. „w cale”) zostaje zaznaczona: to też poprawka.
    package static func marks(_ segments: [DiffSegment]) -> [NSRange] {
        var raw: [NSRange] = []
        var result = ""
        for segment in segments {
            switch segment {
            case .same(let text):
                result += text
            case .added(let text):
                raw.append(NSRange(location: (result as NSString).length, length: (text as NSString).length))
                result += text
            case .removed:
                break
            }
        }
        let text = result as NSString
        func spacesOnly(_ range: NSRange) -> Bool {
            text.substring(with: range).allSatisfy { $0 == " " || $0 == "\t" }
        }
        var merged: [NSRange] = []
        for range in raw where range.length > 0 {
            if let last = merged.last, spacesOnly(NSRange(location: NSMaxRange(last), length: range.location - NSMaxRange(last))) {
                merged[merged.count - 1] = NSRange(location: last.location, length: NSMaxRange(range) - last.location)
            } else {
                merged.append(range)
            }
        }
        return merged.map { range in
            guard !spacesOnly(range) else { return range }
            var start = range.location, end = NSMaxRange(range)
            while start < end, text.character(at: start) == 0x20 || text.character(at: start) == 0x09 { start += 1 }
            while end > start, text.character(at: end - 1) == 0x20 || text.character(at: end - 1) == 0x09 { end -= 1 }
            return NSRange(location: start, length: end - start)
        }
    }

    /// Dłuższego tekstu edycja nie porównuje z oryginałem po każdym klawiszu (`MARKS_MAX` na Linuksie).
    package static let editMarksLimit = 3000

    /// W trakcie edycji: co edytowany tekst zmienia względem oryginału — poprawki, które zostały, i to, co dopisano.
    /// Powyżej `editMarksLimit` znaków nic, żeby nie spowalniać pisania.
    package static func marks(from original: String, to edited: String) -> [NSRange] {
        edited.count > editMarksLimit ? [] : marks(segments(from: original, to: edited))
    }

    /// Wynik z cofniętymi wybranymi zmianami: w ich segmentach zostaje oryginał (usunięte wraca, dodane znika),
    /// reszta poprawek zostaje. Bez cofniętych — sam wynik.
    package static func text(_ segments: [DiffSegment], reverting reverted: [Range<Int>] = []) -> String {
        var result = ""
        for (index, segment) in segments.enumerated() {
            let back = reverted.contains { $0.contains(index) }
            switch segment {
            case .same(let text): result += text
            case .removed(let text): if back { result += text }
            case .added(let text): if !back { result += text }
            }
        }
        return result
    }

    package static func reason(_ from: String, _ to: String, at range: Range<Int>, hints: [LTMatch] = []) -> String {
        let a = from.trimmed, b = to.trimmed
        // Najpierw konkretne kategorie z samego porównania — są trafniejsze niż ogólne
        // „prawdopodobny błąd pisowni" z LanguageTool.
        if a.isEmpty && b.isEmpty { return "Odstęp" }
        let aw = a.filter { !$0.isWhitespace }, bw = b.filter { !$0.isWhitespace }
        let spacingChanged = a.split(whereSeparator: \.isWhitespace).count != b.split(whereSeparator: \.isWhitespace).count
        if aw == bw { return a.contains(where: \.isLetter) ? "Pisownia łączna / rozdzielna" : "Odstęp" }
        if a.isEmpty && b.allSatisfy({ $0.isPunctuation }) { return "Brakujący znak interpunkcyjny" }
        if b.isEmpty && a.allSatisfy({ $0.isPunctuation }) { return "Zbędny znak interpunkcyjny" }
        if a.lowercased() == b.lowercased() { return "Wielka / mała litera" }
        if foldPolish(aw) == foldPolish(bw) {
            return spacingChanged ? "Polskie znaki i pisownia łączna / rozdzielna" : "Polskie znaki"
        }
        let al = aw.filter { !$0.isPunctuation }, bl = bw.filter { !$0.isPunctuation }
        if al == bl { return "Interpunkcja" }
        if foldPolish(al) == foldPolish(bl) { return "Polskie znaki i interpunkcja" }
        if let hint = hints.first(where: { range.overlaps($0.offset..<($0.offset + max($0.length, 1))) }),
           !hint.rule.id.hasPrefix("MORFOLOGIK") {
            return hint.message
        }
        if !a.contains(" "), !b.contains(" "), levenshtein(a, b) <= 2 { return "Literówka" }
        return "Gramatyka / styl"
    }

    /// Sprowadza polskie litery do łacińskich (ł nie ma rozkładu Unicode, więc osobno).
    private static func foldPolish(_ text: String) -> String {
        text.folding(options: .diacriticInsensitive, locale: nil)
            .replacingOccurrences(of: "ł", with: "l").replacingOccurrences(of: "Ł", with: "L")
    }

    private static func tokenize(_ text: String) -> [String] {
        enum Kind { case word, space, other }
        func kind(_ c: Character) -> Kind {
            if c.isLetter || c.isNumber || c == "_" || c == "'" || c == "’" { return .word }
            if c.isWhitespace { return .space }
            return .other
        }
        var tokens: [String] = []
        var current = ""
        var currentKind: Kind?
        for c in text {
            let k = kind(c)
            if k == currentKind, k != .other {
                current.append(c)
            } else {
                if !current.isEmpty { tokens.append(current) }
                current = String(c)
                currentKind = k
            }
        }
        if !current.isEmpty { tokens.append(current) }
        return tokens
    }

    private static func levenshtein(_ a: String, _ b: String) -> Int {
        let a = Array(a.lowercased()), b = Array(b.lowercased())
        if a.isEmpty || b.isEmpty { return max(a.count, b.count) }
        var previous = Array(0...b.count)
        for i in 1...a.count {
            var current = [i] + Array(repeating: 0, count: b.count)
            for j in 1...b.count {
                current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a[i - 1] == b[j - 1] ? 0 : 1))
            }
            previous = current
        }
        return previous[b.count]
    }
}

package extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}
