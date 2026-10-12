import Foundation
import NaturalLanguage

/// Błąd znaleziony podczas pisania (etap 3 roadmapy). Zakres w jednostkach UTF-16, jak w AX.
package struct LiveIssue: Equatable {
    package enum Source: String {
        /// `model`: eksperymentalny tryb „tylko model” (`ModelLive`), zamiast słownika i LanguageTool.
        case spelling = "słownik", languageTool = "LanguageTool", bielik = "Bielik", model = "Model"

        /// Przy nakładaniu się błędów wygrywa źródło z lepszym uzasadnieniem.
        var priority: Int {
            switch self {
            case .languageTool: 3
            case .spelling: 1
            case .bielik, .model: 0
            }
        }
    }

    package var range: NSRange
    package let word: String
    package var isSpelling: Bool { source == .spelling || rule.hasPrefix("MORFOLOGIK") || rule == "SpellCheck" || rule == "PL_SIMPLE_REPLACE" }
    package let message: String
    package let suggestions: [String]
    package let source: Source
    package let rule: String
    /// Opis reguły LanguageTool (do listy wyłączonych reguł w Pisaniu).
    package let ruleDescription: String

    /// „Wyłącz regułę”: reguła LanguageTool o gramatyce, interpunkcji albo stylu; błąd pisowni to słowo
    /// do słownika (`can_turn_off` na Linuksie).
    package var canTurnOff: Bool { source == .languageTool && !isSpelling && !rule.isEmpty }

    package init(range: NSRange, word: String, message: String, suggestions: [String], source: Source, rule: String,
                 ruleDescription: String = "") {
        self.range = range
        self.word = word
        self.message = message
        self.suggestions = suggestions
        self.source = source
        self.rule = rule
        self.ruleDescription = ruleDescription
    }
}

/// Tekst przygotowany do sprawdzania: każde zdanie ma swój język (spośród wybranych języków tekstu), a fragmenty
/// bez języka (linki, e-maile, @wzmianki, kod, :emoji:) są zamaskowane. Wersja każdego języka ma zdania pozostałych
/// języków zamienione na spacje — długość i przesunięcia się nie zmieniają, więc wyniki słownika i LanguageTool
/// trafiają w oryginalny tekst bez przeliczania (`live.LiveText` na Linuksie).
package struct LiveText {
    package let text: String
    package let languages: TextLanguages
    /// Wersja tekstu każdego wybranego języka i jej przesunięcia (UTF-16) zamaskowane.
    package let versions: [TextLanguage: String]
    package let masks: [TextLanguage: IndexSet]
    /// Fragmenty bez języka (linki, e-maile, @wzmianki, kod, :emoji:) — we wszystkich wersjach zamaskowane.
    package let nonLanguage: IndexSet

    package init(_ text: String, languages: TextLanguages = .standard) {
        self.text = text
        self.languages = languages
        let nonLanguage = Self.nonLanguageMask(text)
        self.nonLanguage = nonLanguage
        var masks = Dictionary(uniqueKeysWithValues: languages.all.map { ($0, nonLanguage) })
        for segment in Self.segments(text, masking: nonLanguage, languages: languages) {
            let span = segment.range.location..<NSMaxRange(segment.range)
            for language in languages.all where language != segment.language { masks[language]?.insert(integersIn: span) }
        }
        self.masks = masks
        versions = masks.mapValues { Self.blank(text, $0) }
    }

    /// Wersja tekstu w danym języku; język spoza wybranych — sam tekst zamaskowany w całości.
    package func version(_ language: TextLanguage) -> String {
        versions[language] ?? Self.blank(text, mask(language))
    }

    package func mask(_ language: TextLanguage) -> IndexSet {
        masks[language] ?? IndexSet(integersIn: 0..<(text as NSString).length)
    }

    /// Czy w tekście są zdania w tym języku.
    package func has(_ language: TextLanguage) -> Bool {
        guard let mask = masks[language], let version = versions[language] else { return false }
        return mask.count < (text as NSString).length && !version.isBlank
    }

    /// Wybrane języki, których zdania są w tekście — w kolejności ustawień.
    package var present: [TextLanguage] { languages.all.filter(has) }

    package var polish: String { version(.polish) }
    package var english: String { version(.english) }
    package var polishMask: IndexSet { mask(.polish) }
    package var englishMask: IndexSet { mask(.english) }
    package var hasPolish: Bool { has(.polish) }
    package var hasEnglish: Bool { has(.english) }

    /// Przesunięcia (UTF-16) linków, e-maili, @wzmianek, kodu i :emoji: — bez rozpoznawania języka zdań.
    package static func nonLanguageMask(_ text: String) -> IndexSet {
        var mask = IndexSet()
        for match in TextLanguage.nonLanguage.matches(in: text, range: NSRange(location: 0, length: (text as NSString).length)) {
            mask.insert(integersIn: match.range.location..<NSMaxRange(match.range))
        }
        return mask
    }

    /// Zdania z językiem spośród wybranych. Krótkie zdania („ok, dzięki”, „thx”) dostają język całego tekstu.
    package static func segments(_ text: String, masking nonLanguage: IndexSet = [],
                                 languages: TextLanguages = .standard) -> [(range: NSRange, language: TextLanguage)] {
        let prose = blank(text, nonLanguage)
        let fallback = language(of: prose, fallback: languages.primary, minimumWords: 1, languages: languages)
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = prose
        var result: [(NSRange, TextLanguage)] = []
        tokenizer.enumerateTokens(in: prose.startIndex..<prose.endIndex) { range, _ in
            result.append((NSRange(range, in: prose),
                           language(of: String(prose[range]), fallback: fallback, minimumWords: 3, languages: languages)))
            return true
        }
        return result
    }

    private static let polishLetters = CharacterSet(charactersIn: "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
    private static let germanLetters = CharacterSet(charactersIn: "äöüßÄÖÜẞ")

    /// Język zdania: jeden wybrany język to zawsze on; polska (albo niemiecka) litera przesądza, jeśli ten język
    /// jest wybrany (jak `live.language` na Linuksie); dalej NaturalLanguage w obrębie wybranych języków.
    static func language(of sentence: String, fallback: TextLanguage, minimumWords: Int,
                         languages: TextLanguages = .standard) -> TextLanguage {
        let all = languages.all
        guard all.count > 1 else { return languages.primary }
        if languages.contains(.polish), sentence.rangeOfCharacter(from: polishLetters) != nil { return .polish }
        if languages.contains(.german), sentence.rangeOfCharacter(from: germanLetters) != nil { return .german }
        let words = sentence.split { !$0.isLetter }.count
        guard words >= minimumWords, words > 0 else { return fallback }
        let hypotheses = TextLanguage.hypotheses(sentence, among: all)
        // Remis — pierwszy w kolejności ustawień (dla PL+EN: polski, jak dotąd).
        return all.dropFirst().reduce(all[0]) { best, language in (hypotheses[language] ?? 0) > (hypotheses[best] ?? 0) ? language : best }
    }

    /// Zamienia wskazane jednostki UTF-16 na spacje (końce linii zostają — LSP liczy linie).
    package static func blank(_ text: String, _ mask: IndexSet) -> String {
        guard !mask.isEmpty else { return text }
        var units = Array(text.utf16)
        for i in mask where i < units.count && units[i] != 10 && units[i] != 13 { units[i] = 32 }
        return String(decoding: units, as: UTF16.self)
    }

    /// Przesuwa znane błędy po edycji: te przed zmianą zostają, te za nią przesuwają się o różnicę
    /// długości, a te, których zmiana dotknęła, znikają (sprawdzimy je od nowa).
    package static func shift(_ issues: [LiveIssue], from old: String, to new: String) -> [LiveIssue] {
        guard let span = TextEdits.minimalSpan(from: old, to: new) else { return issues }
        let editEnd = span.prefix + span.oldLength
        let delta = span.replacement.utf16.count - span.oldLength
        return issues.compactMap { issue in
            if NSMaxRange(issue.range) < span.prefix { return issue }
            guard issue.range.location > editEnd else { return nil }
            var moved = issue
            moved.range.location += delta
            return moved
        }
    }

    /// Ten sam błąd z dwóch źródeł (ten sam zakres albo dwa błędy pisowni na tym samym słowie): zostaje ten
    /// z lepszego źródła. Różne błędy w jednym miejscu („dziala ale” → przecinek, „dziala” → pisownia) zostają oba.
    package static func merge(_ lists: [LiveIssue]...) -> [LiveIssue] {
        var accepted: [LiveIssue] = []
        for issue in lists.flatMap({ $0 }).sorted(by: { ($0.source.priority, -$0.range.location) > ($1.source.priority, -$1.range.location) })
        where !accepted.contains(where: { other in
            let overlaps = NSIntersectionRange(other.range, issue.range).length > 0
            // Bielik dokłada tylko to, czego nie znalazły szybsze warstwy (one mają dokładniejsze zakresy i opisy).
            // Lista zgubionych polskich liter to zgadywanie — ustępuje każdemu innemu błędowi w tym miejscu („ze to” z LanguageTool).
            return other.range == issue.range || (other.isSpelling && issue.isSpelling && overlaps)
                || ((issue.source == .bielik || issue.source == .model || issue.rule == "PL_DIACRITICS") && overlaps)
        }) {
            accepted.append(issue)
        }
        return accepted.sorted { ($0.range.location, $0.range.length) < ($1.range.location, $1.range.length) }
    }

    /// Poprawka modelu jako lista błędów: każda zmiana z diffu to jeden błąd na zakresie oryginału.
    /// Samo wstawienie (np. przecinek) przypinamy do słowa przed nim, żeby było co pokazać.
    /// `rule` dostaje opis zmiany (`Diff.reason`) — tryb „tylko model” rozróżnia po nim pisownię i gramatykę.
    package static func issues(from original: String, corrected: String, source: LiveIssue.Source = .bielik,
                               rule: (String) -> String = { _ in "BIELIK" }) -> [LiveIssue] {
        let ns = original as NSString
        var issues: [LiveIssue] = []
        var offset = 0, start = 0
        var from = "", to = ""
        func isSpace(_ unit: unichar) -> Bool {
            UnicodeScalar(unit).map { CharacterSet.whitespacesAndNewlines.contains($0) } ?? false
        }
        func flush() {
            defer { from = ""; to = "" }
            guard !from.isEmpty || !to.isEmpty, from.trimmed != to.trimmed else { return }
            var range = NSRange(location: start, length: (from as NSString).length)
            // Białe znaki na brzegach zmiany nie są częścią błędu.
            while range.length > 0, isSpace(ns.character(at: range.location)) {
                range.location += 1; range.length -= 1
            }
            while range.length > 0, isSpace(ns.character(at: NSMaxRange(range) - 1)) {
                range.length -= 1
            }
            let insertion = range.length == 0
            if insertion {
                // Wstawienie: słowo przed nim (albo po nim na samym początku tekstu).
                var a = min(range.location, ns.length)
                while a > 0, isSpace(ns.character(at: a - 1)) { a -= 1 }
                var b = a
                while a > 0, !isSpace(ns.character(at: a - 1)) { a -= 1 }
                if a == b {
                    while b < ns.length, !isSpace(ns.character(at: b)) { b += 1 }
                }
                range = NSRange(location: a, length: b - a)
            }
            guard range.length > 0 else { return }
            let span = start..<max(start + (from as NSString).length, start + 1)
            let word = ns.substring(with: range)
            // Wstawienie pokazujemy razem ze słowem: „raport → raport,”.
            let suggestion = insertion ? (range.location < start ? word + to.trimmed : to.trimmed + " " + word) : to.trimmed
            let reason = Diff.reason(from, to, at: span)
            issues.append(LiveIssue(range: range, word: word, message: reason,
                                    suggestions: suggestion.isEmpty ? [] : [suggestion], source: source, rule: rule(reason)))
        }
        for segment in Diff.segments(from: original, to: corrected) {
            switch segment {
            case .same(let text):
                // Inaczej niż w panelu: każde słowo osobno, żeby „testuje cos” nie zniknęło razem z „cos”,
                // które znalazł już słownik.
                flush()
                offset += (text as NSString).length
            case .removed(let text):
                if from.isEmpty && to.isEmpty { start = offset }
                from += text
                offset += (text as NSString).length
            case .added(let text):
                if from.isEmpty && to.isEmpty { start = offset }
                to += text
            }
        }
        flush()
        return issues
    }
}
