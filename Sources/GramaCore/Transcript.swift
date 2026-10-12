import Foundation

/// Sprzątanie transkrypcji z typowych „halucynacji” Whispera: przy ciszy albo szumie potrafi wypisać
/// napisy z filmów, na których się uczył, albo powtórzyć samą podpowiedź.
package enum TranscriptFilter {
    /// Całe wypowiedzi, które Whisper produkuje z ciszy (porównanie po normalizacji). Tylko zwroty z napisów
    /// do filmów — krótkie „Dziękuję.” czy „Thank you.” mogą być prawdziwą odpowiedzią, a ciszę odrzuca
    /// wcześniej próg głośności.
    static let hallucinations: Set<String> = [
        "dzięki za obejrzenie", "dziękuję za obejrzenie", "do zobaczenia w następnym odcinku",
        "zapraszam na kolejny odcinek", "thank you for watching", "thanks for watching",
    ]

    package static func clean(_ text: String, prompt: String) -> String {
        var result = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // Adnotacje w nawiasach: [MUZYKA], (śmiech), *kaszel*.
        result = result.replacingOccurrences(of: #"\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*"#, with: " ",
                                             options: .regularExpression, range: nil)
        // Linie z „amara.org” to zawsze napisy z danych treningowych.
        result = result.split(separator: "\n").filter { !$0.lowercased().contains("amara.org") }.joined(separator: "\n")
        result = result.replacingOccurrences(of: #"[ \t]{2,}"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = normalize(result)
        if hallucinations.contains(normalized) { return "" }
        // Sama podpowiedź (lista słów) zamiast mowy.
        if !prompt.isEmpty, normalized == normalize(prompt) { return "" }
        return result
    }

    private static func normalize(_ text: String) -> String {
        text.lowercased()
            .replacingOccurrences(of: #"[.!?…,]+$"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// Porządki bez modelu, natychmiast i przewidywalnie: same wypełniacze („yyy”, „eee”, „hmm”) i godziny
/// z kropką („o 10.00” → „o 10:00”). Poprawek w trakcie („albo nie, cofam…”) tu nie ruszamy — to robi
/// eksperymentalne porządkowanie Bielikiem.
package enum DictationTidy {
    private static let filler = try! NSRegularExpression(
        pattern: #"(?<![\p{L}])(?:y{2,}|e{2,}|m{2,}|hm+|ehm|yhm|um+|uh+|uhm|erm)(?![\p{L}]),?\s*"#, options: [.caseInsensitive])
    /// Godzina po typowym słowie („o”, „od”, „do”, „około”…), ale nie cena ani wersja („po 2.50 zł”, „wersja 1.10”).
    private static let time = try! NSRegularExpression(
        pattern: #"(?<![\p{L}])(o|od|do|około|ok\.|godz\.|godzina|godzinie|przed|po|między|a|na)\s+([01]?\d|2[0-3])\.([0-5]\d)(?!\d|[.,]\d)(?!\s*(?:zł|pln|%|€|eur|usd|\$|gb|mb|kg|km|cm|m\b))"#,
        options: [.caseInsensitive])

    package static func apply(_ text: String) -> String {
        var result = removeFillers(text)
        result = time.stringByReplacingMatches(in: result, range: NSRange(location: 0, length: (result as NSString).length),
                                               withTemplate: "$1 $2:$3")
        // Same wypełniacze („Hmm.”) — nie ma czego wklejać.
        return result.contains(where: { $0.isLetter || $0.isNumber }) ? result : ""
    }

    private static func removeFillers(_ text: String) -> String {
        let ns = text as NSString
        let matches = filler.matches(in: text, range: NSRange(location: 0, length: ns.length))
        guard !matches.isEmpty else { return text }
        var result = ""
        var cursor = 0
        var capitalizeNext = false
        func append(_ fragment: String) {
            guard capitalizeNext, let first = fragment.first else {
                result += fragment
                return
            }
            result += first.uppercased() + fragment.dropFirst()
            capitalizeNext = false
        }
        for match in matches {
            append(ns.substring(with: NSRange(location: cursor, length: match.range.location - cursor)))
            // „Eee, zrobię to” — wypełniacz zaczynał zdanie, więc teraz zaczyna je następne słowo.
            if ns.substring(with: match.range).first?.isUppercase == true { capitalizeNext = true }
            cursor = match.range.location + match.range.length
        }
        append(ns.substring(from: cursor))
        for (pattern, template) in [(#"\s+([,.!?…])"#, "$1"), (#",\s*,"#, ","), (#",([.!?…])"#, "$1"),
                                    (#"^[,\s]+"#, ""), (#"[ \t]{2,}"#, " ")] {
            result = result.replacingOccurrences(of: pattern, with: template, options: .regularExpression)
        }
        return result.trimmingCharacters(in: .whitespaces)
    }
}

/// Podgląd dyktowania w pastylce: Whisper co sekundę rozpoznaje niepewną końcówkę nagrania. Gdy robi się
/// długa, kawałek do pauzy rozpoznajemy raz i odkładamy jako pewny (`DictationChunking`), więc każdy przebieg
/// liczy tylko kilka ostatnich sekund. Tylko do oglądania: wklejamy osobne rozpoznanie całego nagrania.
package struct DictationPreviewText: Equatable {
    package private(set) var confirmed = ""
    package private(set) var pending = ""
    /// Od której próbki nagrania liczy następny przebieg.
    package private(set) var confirmedSamples = 0

    package init() {}

    package var text: String { Self.join([confirmed, pending]) }
    package var isEmpty: Bool { confirmed.isEmpty && pending.isEmpty }
    package var wordCount: Int { text.split(whereSeparator: \.isWhitespace).count }

    /// Kawałek `samples` próbek od `confirmedSamples` rozpoznany do końca — już się nie zmieni.
    package mutating func confirm(_ transcript: String, samples: Int, prompt: String) {
        confirmed = Self.join([confirmed, Self.clean(transcript, prompt: prompt)])
        confirmedSamples += samples
        pending = ""
    }

    /// Rozpoznanie reszty, od `confirmedSamples` do teraz.
    package mutating func setPending(_ transcript: String, prompt: String) {
        pending = Self.clean(transcript, prompt: prompt)
    }

    /// Koniec tekstu do pastylki: najwyżej `maxCharacters` znaków od granicy słowa, z podziałem na pewne
    /// i niepewne (te pastylka pokazuje szarzej).
    package func tail(maxCharacters: Int) -> (confirmed: String, pending: String, truncated: Bool) {
        let full = Array(text)
        guard full.count > maxCharacters else { return (confirmed, pending, false) }
        var cut = full.count - maxCharacters
        while cut < full.count, !full[cut - 1].isWhitespace { cut += 1 }
        // Jedno długie słowo bez spacji (link) — tniemy w środku.
        if cut == full.count { cut = full.count - maxCharacters }
        let confirmedCount = confirmed.count
        if cut < confirmedCount {
            return (String(Array(confirmed)[cut...]), pending, true)
        }
        // Za pewnym tekstem w `text` stoi jedna spacja.
        let start = min(pending.count, max(0, cut - confirmedCount - (confirmed.isEmpty ? 0 : 1)))
        return ("", String(Array(pending)[start...]), true)
    }

    /// „1 słowo”, „3 słowa”, „12 słów”, „22 słowa”.
    package static func wordCountLabel(_ count: Int) -> String {
        UI.count(count, ("słowo", "słowa", "słów"), ("word", "words"))
    }

    private static func clean(_ transcript: String, prompt: String) -> String {
        DictationTidy.apply(TranscriptFilter.clean(transcript, prompt: prompt))
    }

    private static func join(_ parts: [String]) -> String {
        parts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.joined(separator: " ")
    }
}

/// Gdzie podgląd może uciąć niepewną część nagrania: w pauzie (Whisper dostaje wtedy całe zdania, a tekst
/// przed nią już się nie zmieni). Przy mowie bez pauz w najcichszym miejscu, żeby przebieg nie rósł bez końca.
/// Tak samo dzielimy nagranie dłuższe niż okno Whispera przed pełnym rozpoznaniem (`split`).
package enum DictationChunking {
    /// Krótszej części nie tniemy — jeden przebieg i tak trwa ok. sekundy.
    static let minimumWindow = 6.0
    /// Kawałek do odłożenia ma co najmniej tyle sekund, a za cięciem zostaje co najmniej `tailGuard`.
    static let minimumChunk = 2.0
    static let tailGuard = 0.5
    /// Pauza: tyle sekund ciszej niż 1/10 głośnej mowy (-20 dB).
    static let minimumPause = 0.3
    /// Bez pauzy przez tyle sekund tniemy w najcichszym miejscu.
    static let forcedWindow = 20.0
    static let frameSeconds = 0.02
    /// Cisza bezwzględna (ok. -54 dBFS) — przy nagraniu z redukcją szumów mowa bywa cicha, a tło zerowe.
    static let silenceFloor: Float = 0.002

    /// Miejsce cięcia (indeks próbki) albo nil, gdy jeszcze nie warto.
    package static func cut(in samples: [Float], sampleRate: Double) -> Int? {
        guard Double(samples.count) >= minimumWindow * sampleRate else { return nil }
        let frame = Int(frameSeconds * sampleRate)
        let levels = rms(samples, frame: frame)
        guard !levels.isEmpty else { return nil }
        let loud = levels.sorted()[levels.count * 9 / 10]
        let quiet = max(silenceFloor, loud / 10)
        let earliest = Int(minimumChunk * sampleRate), latest = samples.count - Int(tailGuard * sampleRate)
        let pauseFrames = Int((minimumPause / frameSeconds).rounded())

        // Ostatnia pauza w dozwolonym zakresie, cięcie w jej środku.
        var best: Int?
        var runStart: Int?
        for index in 0...levels.count {
            if index < levels.count, levels[index] < quiet {
                if runStart == nil { runStart = index }
                continue
            }
            if let start = runStart, index - start >= pauseFrames {
                let middle = (start + index) / 2 * frame
                if middle >= earliest, middle <= latest { best = middle }
            }
            runStart = nil
        }
        if let best { return best }

        guard Double(samples.count) >= forcedWindow * sampleRate else { return nil }
        // Najcichsze 100 ms między słowami, ale z dala od końca, gdzie słowo może trwać.
        let span = 5
        let first = earliest / frame, last = (samples.count - Int(2 * sampleRate)) / frame - span
        guard last > first else { return nil }
        var quietest = first, quietestSum = Float.infinity
        for index in first...last {
            let sum = levels[index..<(index + span)].reduce(0, +)
            if sum < quietestSum { (quietest, quietestSum) = (index, sum) }
        }
        return (quietest + span / 2) * frame
    }

    /// Kawałki najwyżej `window` sekund do pełnego rozpoznania, każdy do ostatniej pauzy przed końcem okna.
    /// Krótsze nagranie to jeden kawałek.
    package static func split(_ samples: [Float], sampleRate: Double, window: Double) -> [Range<Int>] {
        let limit = Int(window * sampleRate)
        var pieces: [Range<Int>] = []
        var start = 0
        while samples.count - start > limit {
            // Przy oknie ≥ `forcedWindow` `cut` zawsze coś znajdzie; twarde cięcie tylko na wszelki wypadek.
            let cut = cut(in: Array(samples[start..<(start + limit)]), sampleRate: sampleRate) ?? limit
            pieces.append(start..<(start + cut))
            start += cut
        }
        pieces.append(start..<samples.count)
        return pieces
    }

    /// Czy w nagraniu jest coś głośniejszego od ciszy — z samej ciszy Whisper zmyśliłby tekst.
    package static func hasSpeech(_ samples: [Float], sampleRate: Double, threshold: Float) -> Bool {
        rms(samples, frame: Int(frameSeconds * sampleRate)).contains { $0 >= threshold }
    }

    private static func rms(_ samples: [Float], frame: Int) -> [Float] {
        guard frame > 0 else { return [] }
        return stride(from: 0, to: samples.count - frame + 1, by: frame).map { start in
            var sum: Float = 0
            for sample in samples[start..<(start + frame)] { sum += sample * sample }
            return (sum / Float(frame)).squareRoot()
        }
    }
}
