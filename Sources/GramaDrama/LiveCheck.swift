import AppKit
import NaturalLanguage
import GramaCore
import GramaBackend

/// Silnik sprawdzania na żywo, w wybranych językach tekstu (Pisanie → Języki tekstu). Warstwa 0 (`quick`, milisekundy,
/// na Macu): słownik macOS dla każdego z nich i lista słów bez polskich liter. Warstwa 1 (`languageTool`, po pauzie
/// w pisaniu): LanguageTool na serwerze dla zdań każdego języka. Warstwa 2 (`bielik`): model w tle, tylko akapity
/// w głównym języku.
/// Harpera (angielski na Macu) usunęliśmy 29.09: na benchmarku LanguageTool znalazł wszystko, co on, i więcej
/// („Me and Anna was”, „have went”, „it look”), w ~80 ms — Harper dawał tylko natychmiastowość.
@MainActor
final class LiveChecker {
    static let shared = LiveChecker()
    private let spellTag = NSSpellChecker.uniqueSpellDocumentTag()
    private let backend = Backend()
    private var knownCache: (words: [String], known: UserDictionary.Known) = ([], UserDictionary.Known())

    /// Słownik użytkownika (`Settings.dictionaryWords`), zbudowany od nowa tylko po zmianie słów.
    private var known: UserDictionary.Known {
        let words = Settings.shared.dictionaryWords
        if words != knownCache.words { knownCache = (words, UserDictionary.Known(words)) }
        return knownCache.known
    }

    /// Reguły LanguageTool, które w wiadomościach i przy zamaskowanych fragmentach dają fałszywe alarmy.
    /// COMMA_COMPOUND_SENTENCE: przecinek przed „and” w złożonym zdaniu angielskim jest opcjonalny.
    static let disabledLanguageToolRules = ["WHITESPACE_RULE", "PUNCTUATION_PARAGRAPH_END", "UPPERCASE_SENTENCE_START",
                                            "COMMA_PARENTHESIS_WHITESPACE", "DOUBLE_PUNCTUATION", "COMMA_COMPOUND_SENTENCE"]

    func quick(_ live: LiveText) async -> [LiveIssue] {
        var issues: [LiveIssue] = []
        for language in live.present {
            issues += spelling(live.version(language), language: language.code, mask: live.mask(language), original: live.text)
            if language == .polish { issues += Self.missingDiacritics(live.polish, original: live.text) }
        }
        return LiveText.merge(issues)
    }

    /// nil = LanguageTool nie odpowiedział (offline, za wolno) — zostają wyniki warstwy 0.
    func languageTool(_ live: LiveText) async -> [LiveIssue]? {
        let jobs = live.present.map { (live.version($0), $0.languageToolCode, live.mask($0)) }
        var issues: [LiveIssue] = []
        // Reguły wyłączone kartą („Wyłącz regułę”, Pisanie › Słownik i reguły) LanguageTool też pomija.
        let off = Settings.shared.rulesOffIDs
        let disabled = Self.disabledLanguageToolRules + off.filter { !Self.disabledLanguageToolRules.contains($0) }
        for (text, language, mask) in jobs {
            guard let result = try? await backend.languageTool(text, language: language, disabledRules: disabled)
            else { return nil }
            let ns = live.text as NSString
            for match in result.matches {
                let range = NSRange(location: match.offset, length: match.length)
                guard range.length > 0, NSMaxRange(range) <= ns.length, !off.contains(match.rule.id),
                      !mask.intersects(integersIn: range.location..<NSMaxRange(range)) else { continue }
                let word = ns.substring(with: range)
                if match.rule.id.hasPrefix("MORFOLOGIK_RULE"), isPlausible(word, at: range.location, in: live.text) { continue }
                var suggestions = Self.ranked(match.replacements.prefix(5).map(\.value), for: word)
                // „ze to” → LanguageTool proponuje „z to”; zgubione „że” jest dużo częstsze — dajemy je pierwsze.
                if let restored = Self.restoringDiacritics(word), !suggestions.contains(restored) { suggestions.insert(restored, at: 0) }
                issues.append(LiveIssue(range: range, word: word, message: match.message,
                                        suggestions: Array(suggestions.prefix(3)), source: .languageTool, rule: match.rule.id,
                                        ruleDescription: match.rule.description ?? ""))
            }
        }
        return issues
    }

    // MARK: Warstwa 2: Bielik w tle

    /// Poprawka Bielika jako lista błędów (`LiveText.issues`): każda zmiana z diffu to jeden błąd.
    nonisolated static func issues(from original: String, corrected: String) -> [LiveIssue] {
        LiveText.issues(from: original, corrected: corrected)
    }

    /// nil = model nie odpowiedział albo sprawdzanie przerwano. Tylko zdania w głównym języku tekstu (domyślnie
    /// polskie): pozostałe sprawdza LanguageTool, a model potrafi je „poprawić” tłumaczeniem („I have the plan” →
    /// „Mam plan”). Akapit po akapicie (etap 4B): niezmienione akapity biorą wynik ze wspólnej pamięci, więc literówka
    /// w długim mailu to jedno krótkie zapytanie, a akapit pobrany potem przez ⌥⌘G trafia w gotowy wynik.
    /// Akapit bez zdania w głównym języku w ogóle nie idzie do modelu. `onWait`: akapit idzie do serwera (nie ma go
    /// w pamięci).
    func bielik(_ text: String, onWait: @escaping @MainActor () -> Void = {}) async -> [LiveIssue]? {
        var issues: [LiveIssue] = []
        var location = 0
        let languages = Settings.shared.textLanguages
        for paragraph in text.components(separatedBy: "\n") {
            let length = (paragraph as NSString).length
            defer { location += length + 1 }
            let live = LiveText(paragraph, languages: languages)
            guard !paragraph.isBlank, live.has(languages.primary) else { continue }
            guard let corrected = try? await backend.correct(paragraph, mode: .fix, background: true, onPartial: { _ in },
                                                             onWait: onWait),
                  !Task.isCancelled else { return nil }
            // Maska liczona na akapicie, zakresy przesuwane dopiero po filtrze.
            issues += Self.issues(from: paragraph, corrected: corrected)
                .filter { !live.mask(languages.primary).intersects(integersIn: $0.range.location..<NSMaxRange($0.range)) }
                .map { issue in
                    var moved = issue
                    moved.range.location += location
                    return moved
                }
        }
        return issues
    }

    // MARK: Tryb „tylko model” (eksperymentalny)

    /// Wynik jednego fragmentu: błędy w zakresach całego tekstu albo odrzucona odpowiedź modelu.
    struct ModelOnlyFragment {
        let fragment: ModelLive.Fragment
        let issues: [LiveIssue]
        let rejected: Bool
    }

    /// Fragmenty po kolei, od kursora (`ModelLive.order`); każdy wynik od razu trafia do `onFragment`, więc
    /// podkreślenia przy kursorze pojawiają się, zanim model przejdzie przez resztę pola. Odrzucona odpowiedź
    /// dotyczy tylko swojego fragmentu. Błąd połączenia albo serwera przerywa resztę (rzucony dalej).
    /// `onWait(true)` — fragment idzie do serwera (nie ma go w pamięci), `onWait(false)` — odpowiedź jest.
    /// `backend`: atrapa serwera w `--selftest`.
    func modelOnly(_ text: String, caret: Int?, backend: Backend? = nil, onWait: @escaping (Bool) -> Void,
                   onFragment: (ModelOnlyFragment) -> Void) async throws -> [ModelLive.Fragment] {
        let backend = backend ?? self.backend
        let fragments = ModelLive.fragments(in: text)
        let nonLanguage = LiveText.nonLanguageMask(text)
        for index in ModelLive.order(fragments, caret: caret) {
            try Task.checkCancellation()
            let fragment = fragments[index]
            let result: LiveFragmentResult
            do {
                defer { onWait(false) }
                result = try await backend.liveFragment(fragment.text, onWait: { onWait(true) }).0
            }
            try Task.checkCancellation()
            switch result {
            case .corrected(let corrected):
                onFragment(ModelOnlyFragment(fragment: fragment,
                                             issues: ModelLive.issues(for: fragment, corrected: corrected, nonLanguage: nonLanguage),
                                             rejected: false))
            case .rejected(let reason):
                EventLog.write("live_model_rejected", ["reason": reason, "chars": fragment.text.count])
                onFragment(ModelOnlyFragment(fragment: fragment, issues: [], rejected: true))
            }
        }
        return fragments
    }

    // MARK: Kolejność sugestii

    /// Najpierw to samo słowo z polskimi literami („Dzieki” → „Dzięki”, nie „Dziki”), potem reszta w kolejności
    /// słownika. Wielka litera na początku jak w oryginale.
    nonisolated static func ranked(_ suggestions: [String], for word: String) -> [String] {
        let folded = fold(word)
        let same = suggestions.filter { fold($0) == folded }
        var result = same + suggestions.filter { fold($0) != folded }
        if word.first?.isUppercase == true {
            result = result.map { $0.prefix(1).uppercased() + $0.dropFirst() }
        }
        var seen = Set<String>()
        return result.filter { seen.insert($0.lowercased()).inserted }
    }

    nonisolated static func fold(_ text: String) -> String {
        String(text.lowercased().map { c -> Character in
            switch c {
            case "ą": "a"; case "ć": "c"; case "ę": "e"; case "ł": "l"; case "ń": "n"
            case "ó": "o"; case "ś": "s"; case "ź", "ż": "z"
            default: c
            }
        })
    }

    // MARK: Brak polskich liter w istniejących słowach

    /// Słowa, które słownik przyjmuje („cos” jak cosinus, „tez” od „teza”), a w wiadomościach to prawie zawsze
    /// zgubione polskie litery. Czasowniki zależne od osoby („testuje” / „testuję”) zostają dla Bielika.
    nonisolated static let diacriticForms: [String: String] = [
        "cos": "coś", "tez": "też", "wiec": "więc", "zle": "źle", "az": "aż", "bede": "będę", "musze": "muszę",
        "zrobie": "zrobię", "mowie": "mówię", "moge": "mogę", "sprawdze": "sprawdzę", "prosze": "proszę", "ktos": "ktoś",
        "gdzies": "gdzieś", "zeby": "żeby", "juz": "już", "moze": "może", "sie": "się", "dzieki": "dzięki", "wlasnie": "właśnie",
    ]
    nonisolated private static let word = try! NSRegularExpression(pattern: #"\b\p{L}+\b"#)
    /// „ze” jest przyimkiem przed „mną/mnie” i przed zbitkami spółgłosek na s/z/ś/ź/ż/w („ze szkoły”, „ze względu”,
    /// „ze wszystkimi”); w pozostałych miejscach to zwykle zgubione „że”.
    nonisolated private static let zePreposition = try! NSRegularExpression(
        pattern: #"^(mn|wszystk|[sśzźż][^aąeęioóuy]|w[^aąeęioóuy]|sob|sw|ś)"#, options: [.caseInsensitive])

    /// Fragment z przywróconymi polskimi literami („ze to” → „że to”), jeśli lista coś w nim znajdzie.
    nonisolated static func restoringDiacritics(_ fragment: String) -> String? {
        let found = missingDiacritics(fragment + " x", original: fragment + " x").filter { NSMaxRange($0.range) <= (fragment as NSString).length }
        guard !found.isEmpty else { return nil }
        let result = NSMutableString(string: fragment)
        for issue in found.reversed() { result.replaceCharacters(in: issue.range, with: issue.suggestions[0]) }
        return result as String
    }

    nonisolated static func missingDiacritics(_ text: String, original: String) -> [LiveIssue] {
        let ns = text as NSString
        let matches = word.matches(in: text, range: NSRange(location: 0, length: ns.length))
        var issues: [LiveIssue] = []
        for (index, match) in matches.enumerated() {
            let token = ns.substring(with: match.range)
            let lower = token.lowercased()
            var suggestion: String?
            if lower == "ze" {
                let next = index + 1 < matches.count ? ns.substring(with: matches[index + 1].range) : ""
                // Po „ze” coś musi być, żeby zgadywać (koniec pisanego zdania to jeszcze nie błąd).
                if !next.isEmpty, zePreposition.firstMatch(in: next, range: NSRange(location: 0, length: (next as NSString).length)) == nil {
                    suggestion = "że"
                }
            } else {
                suggestion = diacriticForms[lower]
            }
            guard var suggestion else { continue }
            if token.first?.isUppercase == true { suggestion = suggestion.prefix(1).uppercased() + suggestion.dropFirst() }
            issues.append(LiveIssue(range: match.range, word: (original as NSString).substring(with: match.range),
                                    message: "Brak polskiej litery?", suggestions: [suggestion], source: .spelling, rule: "PL_DIACRITICS"))
        }
        return issues
    }

    // MARK: Słownik macOS

    private func spelling(_ text: String, language: String, mask: IndexSet, original: String) -> [LiveIssue] {
        let checker = NSSpellChecker.shared
        let ns = original as NSString
        var issues: [LiveIssue] = []
        var start = 0
        while start < ns.length {
            let range = checker.checkSpelling(of: text, startingAt: start, language: language, wrap: false,
                                              inSpellDocumentWithTag: spellTag, wordCount: nil)
            guard range.location != NSNotFound, range.length > 0, range.location >= start else { break }
            start = NSMaxRange(range)
            let word = ns.substring(with: range)
            if mask.intersects(integersIn: range.location..<NSMaxRange(range)) || isPlausible(word, at: range.location, in: original) { continue }
            let guesses = checker.guesses(forWordRange: range, in: text, language: language, inSpellDocumentWithTag: spellTag) ?? []
            issues.append(LiveIssue(range: range, word: word, message: "Nieznane słowo", suggestions: Array(Self.ranked(guesses, for: word).prefix(3)),
                                    source: .spelling, rule: "SPELLING_\(language.uppercased())"))
        }
        return issues
    }

    /// Słowo „nieznane”, ale prawdopodobnie poprawne: skrót (PR, API), nazwa w środku zdania (Kowalski, Toruń),
    /// camelCase, liczby, ścieżki, albo angielskie słowo w polskim zdaniu (deploy, staging).
    func isPlausible(_ word: String, at location: Int, in text: String) -> Bool {
        guard word.count > 1 else { return true }
        // „Dodaj do słownika” z karty (słownik użytkownika macOS, wspólny dla wszystkich języków) i własne słowa z Pisania
        // w pisowni, jaką przyjmuje lista słów (jak `Known` na Linuksie).
        if NSSpellChecker.shared.hasLearnedWord(word) || known.contains(word) { return true }
        if word.contains(where: { $0.isNumber || $0 == "_" || $0 == "." || $0 == "/" }) { return true }
        if word.filter(\.isUppercase).count >= 2 { return true }
        if word.first?.isUppercase == true, !Self.isSentenceStart(location, in: text) { return true }
        let english = NSSpellChecker.shared.checkSpelling(of: word, startingAt: 0, language: "en", wrap: false,
                                                          inSpellDocumentWithTag: spellTag, wordCount: nil)
        return english.location == NSNotFound
    }

    nonisolated static func isSentenceStart(_ location: Int, in text: String) -> Bool {
        let ns = text as NSString
        var i = location - 1
        while i >= 0, let scalar = UnicodeScalar(ns.character(at: i)), CharacterSet.whitespaces.contains(scalar) { i -= 1 }
        guard i >= 0 else { return true }
        return [".", "!", "?", "\n", "\r", "…"].contains(Character(UnicodeScalar(ns.character(at: i)) ?? " "))
    }

    // MARK: `--lint`

    /// `GramaDrama --lint "tekst" [--bielik]` — test silnika z linii poleceń: błędy obu warstw i czasy.
    static func lintCommand(_ text: String, arguments: [String] = []) async {
        let checker = LiveChecker.shared
        let t0 = Date()
        let languages = Settings.shared.textLanguages
        let live = LiveText(text, languages: languages)
        let prepared = Date()
        print("Zdania:")
        for segment in LiveText.segments(text, languages: languages) {
            print("  [\(segment.language.shortLabel)] \((text as NSString).substring(with: segment.range).trimmingCharacters(in: .whitespacesAndNewlines))")
        }
        let quick = await checker.quick(live)
        let quickDone = Date()
        // Drugi przebieg warstwy 0 — już z wczytanym słownikiem.
        _ = await checker.quick(live)
        let warm = Date()
        let lt = await checker.languageTool(live)
        let ltDone = Date()
        let bielik = arguments.contains("--bielik") ? await checker.bielik(text) : nil
        let bielikDone = Date()
        func ms(_ a: Date, _ b: Date) -> String { "\(Int(b.timeIntervalSince(a) * 1000)) ms" }
        print("Warstwa 0: \(quick.count) błędów · \(ms(t0, prepared)) przygotowanie · \(ms(prepared, quickDone)) (zimny) · \(ms(quickDone, warm)) (ciepły)")
        print("Warstwa 1: " + (lt.map { "\($0.count) błędów · \(ms(warm, ltDone))" } ?? "LanguageTool nie odpowiada"))
        if arguments.contains("--bielik") {
            print("Warstwa 2: " + (bielik.map { "\($0.count) zmian Bielika · \(ms(ltDone, bielikDone))" } ?? "Bielik nie odpowiada"))
        }
        // --layers: każda warstwa osobno, bez scalania (pomiar, które warstwy się pokrywają). Kolumny rozdzielone tabulatorem.
        if arguments.contains("--layers") {
            for (layer, issues) in [("0", quick), ("1", lt ?? []), ("2", bielik ?? [])] {
                for issue in issues {
                    print(["LAYER", layer, issue.source.rawValue, issue.rule, "\(issue.range.location)", "\(issue.range.length)",
                           issue.word, issue.suggestions.first ?? ""].joined(separator: "\t"))
                }
            }
        }
        for issue in LiveText.merge(quick, lt ?? [], bielik ?? []) {
            let hint = issue.suggestions.isEmpty ? "" : " → \(issue.suggestions.joined(separator: " / "))"
            print("  \(issue.range.location)+\(issue.range.length) „\(issue.word)”\(hint) · \(issue.message) [\(issue.source.rawValue): \(issue.rule)]")
        }
    }
}
