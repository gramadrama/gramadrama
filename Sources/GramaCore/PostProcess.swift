import Foundation

/// Sprzątanie odpowiedzi modelu. Zasada: zmieniamy tylko to, co dodał model — nigdy treść,
/// która była już w oryginale (bloki kodu, powtórzone emoji, e-maile, URL-e).
package enum PostProcess {
    /// `strictCommentary`: tryb tłumaczenia — wynik nie dzieli słów z oryginałem, więc komentarz modelu
    /// musi mieć też formę komentarza (nawias, dwukropek na końcu, „Translation:"), a nie tylko pasującą frazę.
    package static func clean(_ output: String, original: String, strictCommentary: Bool = false) -> String {
        let originalTrimmed = original.trimmed
        var result = stripWrappers(output.trimmed, original: originalTrimmed)
        result = stripCommentary(result, original: originalTrimmed, strict: strictCommentary)
        // Model odpowiedział samym komentarzem („tekst nie zawiera błędów") — nic do zmiany.
        if result.isBlank { return original }
        result = stripPlaceholders(result, original: originalTrimmed)
        // Sam „[Your name]” — tak samo nic do zmiany (jak na Linuksie; inaczej kontrola wyniku widziałaby zgubione wzmianki).
        if result.isBlank { return original }
        result = restoreEmoji(result, original: original)
        result = restoreQuotes(result, original: original)
        result = restoreMentions(result, original: original)
        result = unwrapURLs(result, original: original)
        result = restoreProtectedSpacing(result, original: original)
        let leading = original.prefix { $0.isWhitespace }
        let trailing = String(original.reversed().prefix { $0.isWhitespace }.reversed())
        return leading + result + trailing
    }

    // MARK: Opakowania

    /// Zdejmuje ```, <text>…</text> i cudzysłowy wokół całej odpowiedzi — tylko te, które dodał model.
    static func stripWrappers(_ text: String, original: String) -> String {
        var result = text
        var changed = true
        while changed {
            changed = false
            // Płotki kodu parami i tylko, gdy w wyniku jest ich więcej niż w oryginale.
            let extraFences = count("```", in: result) - count("```", in: original)
            if extraFences >= 2, result.count >= 6, result.hasPrefix("```"), result.hasSuffix("```") {
                removeLeadingFence(&result)
                result.removeLast(3)
                changed = true
            } else if extraFences >= 1, result.hasPrefix("```"), !original.hasPrefix("```") {
                removeLeadingFence(&result)
                changed = true
            } else if extraFences >= 1, result.hasSuffix("```"), !original.hasSuffix("```") {
                result.removeLast(3)
                changed = true
            }
            if !original.hasPrefix("<text>"), result.hasPrefix("<text>") {
                result.removeFirst("<text>".count)
                changed = true
            }
            if !original.hasSuffix("</text>"), result.hasSuffix("</text>") {
                result.removeLast("</text>".count)
                changed = true
            }
            // Autor ujął cały tekst w cudzysłów (w dowolnym stylu) — zostawiamy, nawet gdy model zmienił styl.
            let openers: Set<Character> = ["\"", "„", "“"], closers: Set<Character> = ["\"", "”", "“"]
            let originalWrapped = original.first.map(openers.contains) == true && original.last.map(closers.contains) == true
            if !originalWrapped, result.count > 2, let first = result.first, let last = result.last,
               openers.contains(first), closers.contains(last) {
                let inner = String(result.dropFirst().dropLast())
                // „a" i „b" to nie opakowanie — zdejmujemy tylko, gdy w środku nie ma cudzysłowów.
                if !inner.contains(where: { openers.contains($0) || closers.contains($0) }) {
                    result = inner
                    changed = true
                }
            }
            let trimmed = result.trimmed
            if trimmed != result {
                result = trimmed
                changed = true
            }
        }
        return result
    }

    /// „```swift\n" (znacznik języka tylko z nową linią) albo same „```".
    private static func removeLeadingFence(_ text: inout String) {
        if let fence = text.range(of: #"^```[A-Za-z0-9_+\-]*[ \t]*\n"#, options: .regularExpression) {
            text.removeSubrange(fence)
        } else {
            text.removeFirst(3)
        }
    }

    // MARK: Komentarze modelu

    /// Tylko frazy, w których model mówi o samym tekście / swojej pracy. Celowo wąskie: w trybach
    /// przepisujących model legalnie pisze „Please let me know if you need any changes" czy „Oto plan:".
    private static let commentaryPattern = try! NSRegularExpression(pattern: [
        #"\b(the|this|your) (original|provided|given|corrected|revised|input) (text|message|version|sentence)\b"#,
        #"\bhere(?: is|'s| are) (the|your) (corrected|revised|improved|updated|edited|shorter|formal|polished|fixed)\b"#,
        #"\bno (changes|corrections|edits) (were |are |was |is )?(needed|necessary|required|made)\b"#,
        #"\bnothing (to|that needs to be) (fix|correct|change|changed|fixed|corrected)\b"#,
        #"\b(does not|doesn't|did not|didn't) (contain|need|require|have) any (errors|mistakes|changes|corrections)\b"#,
        #"\b(text|message|sentence) (is|was) already (correct|fine|error-free)\b"#,
        #"\b(oryginalny|poprawiony|podany|zmieniony|wejściowy) tekst\b"#,
        #"\btekst (nie zawiera|nie wymaga|jest (już )?poprawny|jest bezbłędny)\b"#,
        #"\bnie (wymaga|wymagał|wymagała) (żadnych )?(zmian|poprawek|korekt)\b|\bnie zawiera (żadnych )?błędów\b"#,
        #"\boto (poprawiony|poprawiona|zmieniony|zmieniona|ulepszony|ulepszona|krótszy|krótsza|skrócona|skrócony) (tekst|wersja|wiadomość)\b"#,
        // Tłumaczenie
        #"\bhere(?: is|'s) (the|your|my) translation\b|\btranslated (text|version)\b|\btranslated from (polish|english)\b"#,
        #"^(the )?translation( into (english|polish)| \((en|pl)\))?:?$"#,
        #"\boto (tłumaczenie|przekład)\b|\bprzetłumaczono z\b|^(tłumaczenie|przekład|przetłumaczony tekst)( na (angielski|polski))?:?$"#,
    ].joined(separator: "|"), options: [.caseInsensitive])

    /// Usuwa wtrącenia modelu: „Oto poprawiony tekst:", „(The original text does not contain any errors…)".
    /// Linia (albo jej doklejony koniec/początek) znika tylko, gdy zawiera frazę o samym tekście
    /// i nie odpowiada niczemu z oryginału.
    static func stripCommentary(_ text: String, original: String, strict: Bool = false) -> String {
        let originalWords = Set(fold(original).split(separator: " ").filter { $0.count >= 3 })
        let foldedOriginal = fold(original)

        /// Linia pochodzi od autora: jest fragmentem oryginału albo używa głównie jego słów.
        func aligns(_ line: String) -> Bool {
            let folded = fold(line)
            guard !folded.isEmpty else { return true }
            if foldedOriginal.contains(folded) { return true }
            let words = folded.split(separator: " ").filter { $0.count >= 3 }
            guard !words.isEmpty else { return similarity(folded, foldedOriginal) >= 0.5 }
            return Double(words.filter(originalWords.contains).count) / Double(words.count) >= 0.6
        }
        func isCommentary(_ fragment: String) -> Bool {
            let t = fragment.trimmed
            guard !t.isEmpty, !aligns(t) else { return false }
            if strict {
                let shaped = (t.hasPrefix("(") && t.hasSuffix(")")) || (t.hasPrefix("[") && t.hasSuffix("]")) || t.hasSuffix(":")
                guard shaped else { return false }
            }
            return commentaryPattern.firstMatch(in: t, range: NSRange(t.startIndex..., in: t)) != nil
        }

        var result = text
        // Komentarz doklejony w tej samej linii: na końcu w nawiasie albo na początku „Oto poprawiony tekst: …".
        if count("(", in: result) > count("(", in: original),
           let range = result.range(of: #"[ \t]*\([^()\n]{12,}\)[ \t]*$"#, options: .regularExpression),
           isCommentary(String(result[range])) {
            result.removeSubrange(range)
        }
        if let range = result.range(of: #"^[^\n:]{8,80}:[ \t]+"#, options: .regularExpression),
           !original.hasPrefix(String(result[range]).trimmed), isCommentary(String(result[range])),
           result[range.upperBound...].contains(where: { !$0.isWhitespace && !$0.isNewline }) {
            result.removeSubrange(range)
        }

        // Całe linie na brzegach (każdy rodzaj końca linii: \n, \r, U+2028).
        func firstLine(_ s: String) -> (line: String, removal: Range<String.Index>) {
            let end = s.firstIndex(where: \.isNewline) ?? s.endIndex
            let next = s[end...].firstIndex { !$0.isNewline } ?? s.endIndex
            return (String(s[..<end]), s.startIndex..<next)
        }
        func lastLine(_ s: String) -> (line: String, removal: Range<String.Index>) {
            let start = s.lastIndex(where: \.isNewline).map(s.index(after:)) ?? s.startIndex
            let previous = s[..<start].lastIndex { !$0.isNewline }.map(s.index(after:)) ?? s.startIndex
            return (String(s[start...]), previous..<s.endIndex)
        }
        while !result.isBlank {
            let first = firstLine(result), last = lastLine(result)
            if first.line.isBlank || isCommentary(first.line) {
                result.removeSubrange(first.removal)
            } else if last.line.isBlank || isCommentary(last.line) {
                result.removeSubrange(last.removal)
            } else {
                break
            }
        }
        // Zostało puste — cała odpowiedź była komentarzem („tekst nie zawiera błędów").
        return result
    }

    /// Małe litery, bez polskich znaków, interpunkcji i nadmiarowych spacji.
    package static func fold(_ text: String) -> String {
        let folded = text.lowercased().folding(options: .diacriticInsensitive, locale: nil)
            .replacingOccurrences(of: "ł", with: "l")
        return folded.split { !$0.isLetter && !$0.isNumber }.joined(separator: " ")
    }

    /// 1 − znormalizowana odległość Levenshteina (na pierwszych 400 znakach — komentarze są krótkie).
    static func similarity(_ a: String, _ b: String) -> Double {
        let x = Array(a.prefix(400)), y = Array(b.prefix(400))
        if x.isEmpty || y.isEmpty { return x.isEmpty && y.isEmpty ? 1 : 0 }
        var previous = Array(0...y.count)
        for i in 1...x.count {
            var current = [i] + Array(repeating: 0, count: y.count)
            for j in 1...y.count {
                current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x[i - 1] == y[j - 1] ? 0 : 1))
            }
            previous = current
        }
        return 1 - Double(previous[y.count]) / Double(max(x.count, y.count))
    }

    // MARK: Obce pismo

    /// Litery z pisma, którego oryginał w ogóle nie używa — usterka modelu, nie poprawka: Gemma 4 zrobiła
    /// z „piszę” „pいつze”, przy temperaturze 0 powtarzalnie (to samo co `foreign_letters` na Linuksie).
    /// Łacina zawsze się liczy jako używana (każda litera poniżej U+0250 to łacina); tekst, który już ma
    /// cyrylicę, może ją zachować.
    package static func foreignLetters(_ text: String, original: String) -> [Character] {
        func script(_ character: Character) -> String? {
            guard character.isLetter, let scalar = character.unicodeScalars.first, scalar.value >= 0x250 else { return nil }
            // LATIN, CYRILLIC, GREEK, HIRAGANA, CJK, ARABIC…
            return scalar.properties.name.map { String($0.prefix { $0 != " " }) } ?? "?"
        }
        let used = Set(original.compactMap(script)).union(["LATIN"])
        var seen = Set<Character>()
        return text.filter { character in
            guard let name = script(character), !used.contains(name) else { return false }
            return seen.insert(character).inserted
        }.map { $0 }
    }

    // MARK: Chronione fragmenty po korekcie

    /// Kontrola po naprawach — to samo co `protected` na Linuksie: bloki i fragmenty kodu, linki, e-maile, wzmianki,
    /// kanały i kody emoji (kod tylko na granicy, więc „9:30:” nim nie jest).
    private static let protectedFragment = try! NSRegularExpression(
        pattern: #"```[\s\S]*?```|`[^`\n]+`|https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.\w+|(?<!\w)[@#][\w.-]+|:[a-zA-Z0-9_+-]+:"#)

    /// Linki, e-maile, wzmianki, kanały, kod, kody i znaki emoji z liczbą wystąpień — po korekcie mają być te same.
    /// Znormalizowane tak, żeby przeszła zwykła poprawka: link bez interpunkcji za nim, wzmianka bez kropki zdania.
    package static func protectedCounts(_ text: String) -> [String: Int] {
        var counts: [String: Int] = [:]
        for character in text where isEmoji(character) { counts[String(character), default: 0] += 1 }
        let ns = text as NSString
        var codeEnd = -1
        for match in protectedFragment.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            var token = ns.substring(with: match.range)
            if token.hasPrefix(":") {
                // Kod emoji na początku, po białym znaku albo zaraz po poprzednim kodzie („:fire::tada:”).
                let start = match.range.location
                if start > 0, start != codeEnd,
                   !(Unicode.Scalar(ns.character(at: start - 1)).map(CharacterSet.whitespacesAndNewlines.contains) ?? false) {
                    continue
                }
                codeEnd = NSMaxRange(match.range)
            } else if token.hasPrefix("http") {
                // Interpunkcja zamykająca zdanie nie należy do linku („…/a_b.”).
                token = dropTrailing(token, ".,;:!?)…\"'”’»]")
            } else if token.hasPrefix("@") || token.hasPrefix("#") {
                token = dropTrailing(token, ".-")
            }
            counts[token, default: 0] += 1
        }
        return counts
    }

    private static func dropTrailing(_ text: String, _ characters: String) -> String {
        var text = text
        while let last = text.last, characters.contains(last) { text.removeLast() }
        return text
    }

    /// Emoji albo kod emoji — tryby przepisujące mogą je dodać; nigdy link ani kod z emoji w środku.
    private static func isEmojiLike(_ token: String) -> Bool {
        (token.count > 2 && token.hasPrefix(":") && token.hasSuffix(":")) || (token.count == 1 && token.first.map(isEmoji) == true)
    }

    /// Odpowiedź gotowa do pokazania w panelu — `clean` na Linuksie: pusta to błąd, potem naprawy (`clean`), a wynik,
    /// w którym nadal różnią się linki, wzmianki, kod albo emoji, jest odrzucany (`refusal`).
    package static func checked(_ output: String, original: String, mode: Mode) throws -> String {
        guard !output.isBlank else { throw ResultRefusal.empty }
        // Tłumaczenie z definicji nie dzieli słów z oryginałem — komentarze modelu rozpoznajemy ostrożniej.
        let result = clean(output, original: original, strictCommentary: mode == .translate)
        if let refusal = refusal(result, original: original, mode: mode) { throw refusal }
        return result
    }

    /// Powód odrzucenia wyniku po naprawach (`clean`), jak na Linuksie, albo nil: linki, wzmianki, kod i emoji mają
    /// zostać te same. Poza trybem Popraw model może dodać emoji (i kody emoji), byle zostały te autora.
    package static func refusal(_ result: String, original: String, mode: Mode) -> ResultRefusal? {
        let before = protectedCounts(original)
        var after = protectedCounts(result)
        if mode != .fix {
            for (token, count) in after where isEmojiLike(token) { after[token] = min(count, before[token] ?? 0) }
            after = after.filter { $0.value > 0 }
        }
        return before == after ? nil : .protected
    }

    // MARK: Emoji i symbole zastępcze

    private static func isEmoji(_ character: Character) -> Bool {
        character.unicodeScalars.contains { $0.properties.isEmojiPresentation }
            || (character.unicodeScalars.first.map { $0.properties.isEmoji && $0.value > 0x238C } ?? false)
    }

    /// Emoji autora zostają nietknięte: jeśli model podmienił 🙂 na 😊 (albo na „;)" / „:)"),
    /// wstawiamy z powrotem oryginał w to samo miejsce.
    static func restoreEmoji(_ text: String, original: String) -> String {
        let originalEmoji = original.filter(isEmoji)
        guard !originalEmoji.isEmpty else { return text }
        var result = text
        for emoji in Set(originalEmoji) {
            let missing = originalEmoji.filter { $0 == emoji }.count - result.filter { $0 == emoji }.count
            guard missing > 0 else { continue }
            for _ in 0..<missing {
                // Emoji, którego autor nie użył, albo emotikon tekstowy, którego nie było w oryginale.
                if let index = result.firstIndex(where: { isEmoji($0) && !originalEmoji.contains($0) }) {
                    result.replaceSubrange(index...index, with: String(emoji))
                } else if let range = result.range(of: #"(?<=\s|^)[;:]-?[)(DPp](?=\s|$)"#, options: .regularExpression),
                          !original.contains(String(result[range])) {
                    result.replaceSubrange(range, with: String(emoji))
                } else {
                    result += " " + String(emoji)
                }
            }
        }
        return result
    }

    /// „[Your name]", „[Twoje imię]" — model dopisał podpis z symbolem zastępczym, którego nie ma w oryginale.
    static func stripPlaceholders(_ text: String, original: String) -> String {
        let pattern = #"(?m)^[ \t]*\[[^\]\n]{2,40}\][ \t]*$\n?"#
        guard !original.contains("["),
              let regex = try? NSRegularExpression(pattern: pattern) else { return text }
        let result = regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")
        return result.trimmed
    }

    // MARK: Cudzysłowy

    /// Modele lubią zamieniać ' na ’ — zostawiamy styl cudzysłowów autora.
    static func restoreQuotes(_ text: String, original: String) -> String {
        var result = text
        if !original.contains(where: { "’‘".contains($0) }) {
            result = result.replacingOccurrences(of: "’", with: "'").replacingOccurrences(of: "‘", with: "'")
        }
        if !original.contains(where: { "„”“".contains($0) }) {
            for quote in ["„", "”", "“"] { result = result.replacingOccurrences(of: quote, with: "\"") }
        }
        return result
    }

    // MARK: Nietykalne fragmenty (@wzmianki, #kanały, :emoji:, URL-e, `kod`)

    private static let protectedToken = try! NSRegularExpression(
        pattern: #"(?<=\s|^)(:[a-z0-9_+\-]+:|@[\p{L}\p{N}._\-]+|#[\p{L}\p{N}_\-]+|https?://\S+|`[^`\n]+`)"#,
        options: [.caseInsensitive])

    /// Fragmenty chronione, które w oryginale stoją po białym znaku (albo na początku).
    private static func protectedTokens(in original: String) -> Set<String> {
        let ns = original as NSString
        return Set(protectedToken.matches(in: original, range: NSRange(location: 0, length: ns.length)).map {
            var token = ns.substring(with: $0.range)
            // „@Łukasz." na końcu zdania — kropka nie należy do wzmianki.
            if token.hasPrefix("@") { token = token.trimmingCharacters(in: CharacterSet(charactersIn: ".-_")) }
            return token
        })
    }

    private static func count(_ needle: String, in haystack: String) -> Int {
        haystack.components(separatedBy: needle).count - 1
    }

    /// URL-e i e-maile — wewnątrz nich nie szukamy imion do oznaczenia.
    private static let linkLike = try! NSRegularExpression(pattern: #"https?://\S+|[\p{L}\p{N}._%+\-]+@[\p{L}\p{N}.\-]+\.[\p{L}]{2,}"#)

    /// Model zgubił „@" przed wzmianką — bez niego Slack nie oznaczy osoby. Wystąpienia imienia
    /// (z „@" albo bez) w oryginale i wyniku dopasowujemy po kolei; „@" wraca tylko w tych miejscach,
    /// które w oryginale były wzmianką. Gdy liczba wystąpień się nie zgadza — nie zgadujemy.
    static func restoreMentions(_ text: String, original: String) -> String {
        var result = text
        for token in protectedTokens(in: original) where token.hasPrefix("@") && token.count > 1 {
            guard count(token, in: original) > count(token, in: result) else { continue }
            let name = NSRegularExpression.escapedPattern(for: String(token.dropFirst()))
            guard let occurrence = try? NSRegularExpression(
                pattern: #"(?<![@#/.=?&~+:%\p{L}\p{N}_\-])@?"# + name + #"(?![\p{L}\p{N}_@\-]|\.[\p{L}\p{N}])"#,
                options: [.caseInsensitive]) else { continue }

            func occurrences(in text: String) -> [NSRange] {
                let full = NSRange(location: 0, length: (text as NSString).length)
                let links = linkLike.matches(in: text, range: full).map(\.range)
                return occurrence.matches(in: text, range: full).map(\.range)
                    .filter { range in !links.contains { NSIntersectionRange($0, range).length > 0 } }
            }
            let originalNS = original as NSString
            let inOriginal = occurrences(in: original)
            let inResult = occurrences(in: result)
            guard inOriginal.count == inResult.count else { continue }

            let resultNS = NSMutableString(string: result)
            for (originalRange, resultRange) in zip(inOriginal, inResult).reversed() {
                let wasMention = originalNS.substring(with: originalRange).hasPrefix("@")
                let isMention = resultNS.substring(with: resultRange).hasPrefix("@")
                if wasMention, !isMention { resultNS.replaceCharacters(in: resultRange, with: token) }
            }
            result = resultNS as String
        }
        return result
    }

    /// „<https://…>", „[https://…]" albo escapowanie jak w markdownie („merge\_requests") psuje link.
    static func unwrapURLs(_ text: String, original: String) -> String {
        var result = text
        for token in protectedTokens(in: original) where token.hasPrefix("http") {
            var escaped = token
            for character in ["_", "*", "~", "[", "]", "(", ")"] {
                escaped = escaped.replacingOccurrences(of: character, with: "\\" + character)
            }
            if escaped != token, !original.contains(escaped) {
                result = result.replacingOccurrences(of: escaped, with: token)
            }
            let pattern = #"[<\[]\s*"# + NSRegularExpression.escapedPattern(for: token) + #"\s*[>\]]"#
            guard original.range(of: pattern, options: .regularExpression) == nil else { continue }
            result = result.replacingOccurrences(of: pattern, with: NSRegularExpression.escapedTemplate(for: token),
                                                 options: .regularExpression)
        }
        return result
    }

    private static let emojiCode = try! NSRegularExpression(pattern: #":[a-z0-9_+\-]+:"#, options: [.caseInsensitive])

    /// Pozycje dwukropków zamykających kody emoji. Kod emoji zaczyna się na początku tekstu,
    /// po białym znaku albo zaraz po poprzednim emoji (":fire::tada:").
    private static func emojiClosingColons(in text: NSString) -> Set<Int> {
        var closing = Set<Int>()
        var lastEnd = -1
        for match in emojiCode.matches(in: text as String, range: NSRange(location: 0, length: text.length)) {
            let start = match.range.location
            let boundary = start == 0 || start == lastEnd
                || text.substring(with: NSRange(location: start - 1, length: 1)).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            guard boundary else { continue }
            lastEnd = start + match.range.length
            closing.insert(lastEnd - 1)
        }
        return closing
    }

    /// Przywraca spację przed chronionym fragmentem, jeśli to model go skleił z poprzednim znakiem.
    /// Sklejenia obecne w oryginale („:fire::fire:", „admin@dev.pl", „(@jan") zostają nietknięte.
    static func restoreProtectedSpacing(_ text: String, original: String) -> String {
        var text = text
        // Model dokleił dwukropek przed kodem emoji („staging: :rocket:" / „staging::rocket:").
        // Dwukropek zamykający poprzednie emoji (":fire::rocket:") zostaje; „9:30:" to nie emoji.
        for token in protectedTokens(in: original) where token.hasPrefix(":") {
            let glued = #":[ ]?"# + NSRegularExpression.escapedPattern(for: token)
            guard original.range(of: glued, options: [.regularExpression, .caseInsensitive]) == nil,
                  let regex = try? NSRegularExpression(pattern: glued, options: [.caseInsensitive]) else { continue }
            let current = text as NSString
            let closing = emojiClosingColons(in: current)
            let mutable = NSMutableString(string: text)
            for match in regex.matches(in: text, range: NSRange(location: 0, length: current.length)).reversed()
            where !closing.contains(match.range.location) {
                mutable.replaceCharacters(in: match.range, with: " " + token)
            }
            text = mutable as String
        }
        let ns = text as NSString
        var insertions = Set<Int>()  // przesunięcia UTF-16, przed którymi wstawiamy spację
        for token in protectedTokens(in: original) {
            var searchRange = NSRange(location: 0, length: ns.length)
            while true {
                let found = ns.range(of: token, options: [], range: searchRange)
                guard found.location != NSNotFound else { break }
                searchRange = NSRange(location: found.location + 1, length: ns.length - found.location - 1)
                guard found.location > 0 else { continue }
                let previous = ns.substring(with: ns.rangeOfComposedCharacterSequence(at: found.location - 1))
                if previous.trimmed.isEmpty || original.contains(previous + token) { continue }
                insertions.insert(found.location)
            }
        }
        let result = NSMutableString(string: text)
        for location in insertions.sorted(by: >) { result.insert(" ", at: location) }
        return result as String
    }
}

/// Odpowiedź modelu, której panel nie pokazuje jako wyniku (te same komunikaty co na Linuksie).
package enum ResultRefusal: LocalizedError, Equatable {
    /// Pusta odpowiedź — to błąd, a nie „Zero dramatu” (sam komentarz modelu nadal znaczy „bez zmian”).
    case empty
    /// Po naprawach nadal zmieniony link, wzmianka, kod albo emoji.
    case protected

    /// Nazwa do dziennika zdarzeń (bez treści tekstu).
    package var reason: String { self == .empty ? "empty" : "protected" }

    package var errorDescription: String? {
        switch self {
        case .empty: tr("Model zwrócił pustą odpowiedź. Spróbuj ponownie.")
        case .protected: tr("Model zmienił link, wzmiankę, kod lub emoji. Wynik odrzucono; spróbuj ponownie.")
        }
    }
}
