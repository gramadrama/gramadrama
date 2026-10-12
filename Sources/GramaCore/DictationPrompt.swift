import Foundation

/// Prompt trybu „Czyszczenie dyktowania” (model porządkuje transkrypcję z Whispera).
package extension Prompts {
    /// Instrukcja (`cleanup.txt`) z listą nazw i terminów do zapisania dokładnie tak, jak je podano.
    static func dictationSystemPrompt(vocabulary: [String], _ p: PromptFiles) -> String {
        var prompt = p.text("cleanup.txt")
        if !vocabulary.isEmpty {
            prompt += "\n- Spell these names and terms exactly like this: \(vocabulary.joined(separator: ", "))."
        }
        return prompt
    }

    /// Język podajemy wprost, jak w trybie Popraw — bez tego Bielik potrafi przetłumaczyć angielskie dyktowanie.
    static func dictationUserPrompt(_ transcript: String, _ p: PromptFiles, languages: TextLanguages) -> String {
        let language = TextLanguage.detect(transcript, among: languages)
        return p.text("cleanup-user.txt").replacingOccurrences(of: "{language}", with: p.name("language", language.code))
            .replacingOccurrences(of: "{text}", with: transcript)
    }

    /// Szybka ścieżka: Whisper sam stawia interpunkcję i wielkie litery, więc model jest potrzebny tylko,
    /// gdy w transkrypcji są wypełniacze, zająknięcia albo poprawki w trakcie. Oszczędza 3–5 s.
    /// Fałszywe trafienie kosztuje tylko czas (Bielik zostawi zdanie bez zmian), przeoczenie — obie wersje w tekście.
    nonisolated static func dictationNeedsCleanup(_ transcript: String) -> Bool {
        let lower = transcript.lowercased()
        let patterns = [
            // wypełniacze i zająknięcia
            #"(?<![\p{L}])(y{2,}|e{2,}|m{2,}|hm+|yhm|ym+|um+|uh+|uhm|er+m?)(?![\p{L}])"#,
            // poprawki w trakcie po polsku
            #"(?<![\p{L}])(znaczy|to znaczy|czyli nie|nie,? nie|a nie|albo nie|nie,? jednak|jednak nie|chociaż nie|a właściwie|właściwie nie|albo lepiej|czy raczej|czekaj|poczekaj|poprawka|pomyłka|wróć|cofam|cofnij|cofnijmy|skreśl|skreślmy|zmieniam zdanie|źle powiedział\p{L}*|przepraszam|sorki|sorry)(?![\p{L}])"#,
            // poprawki w trakcie po angielsku
            #"(?<![\p{L}])(i mean|no wait|wait|actually|actually no|or rather|let me rephrase|scratch that|strike that)(?![\p{L}])"#,
            // „no więc”, „jakby”, „you know” na wtrącenie
            #"(?<![\p{L}])(no więc|no to|jakby|you know|kind of like)(?![\p{L}])"#,
            // powtórzone słowo: „że że”, „the the”
            #"(?<![\p{L}])(\p{L}+)[ ,]+\1(?![\p{L}])"#,
        ]
        if patterns.contains(where: { lower.range(of: $0, options: .regularExpression) != nil }) { return true }
        return restartsSentence(lower)
    }

    /// Zdanie zaczęte od nowa: te same trzy słowa po sobie drugi raz („myślałem, że zrobi… myślałem, że zrobi…”).
    nonisolated static func restartsSentence(_ text: String) -> Bool {
        let words = text.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        guard words.count >= 6 else { return false }
        var seen = Set<String>()
        for index in 0...(words.count - 3) {
            let trigram = words[index..<index + 3].joined(separator: " ")
            if !seen.insert(trigram).inserted { return true }
        }
        return false
    }

    /// Przykłady (`cleanup-examples.json`): wypełniacze, autokorekta mówcy, polecenie i pytanie zapisane zamiast
    /// wykonane, angielski.
    static func dictationMessages(for transcript: String, vocabulary: [String], languages: TextLanguages = .standard,
                                  prompts: PromptFiles = .current()) -> [[String: String]] {
        let p = prompts.forLanguages(languages)
        var messages = [["role": "system", "content": dictationSystemPrompt(vocabulary: vocabulary, p)]]
        for example in p.examples("cleanup-examples.json") {
            messages += [["role": "user", "content": dictationUserPrompt(example.text, p, languages: languages)],
                         ["role": "assistant", "content": example.output]]
        }
        return messages + [["role": "user", "content": dictationUserPrompt(transcript, p, languages: languages)]]
    }

    /// Siatka bezpieczeństwa: model czasem odpowiada na podyktowane pytanie albo dopisuje treść.
    /// Wynik musi składać się głównie ze słów z transkrypcji i mieć podobną długość — inaczej
    /// wklejamy surową transkrypcję.
    nonisolated static func dictationCleanupLooksSafe(_ cleaned: String, transcript: String) -> Bool {
        func words(_ text: String) -> [String] {
            text.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        }
        let source = words(transcript), result = words(cleaned)
        guard !result.isEmpty else { return false }
        guard !source.isEmpty else { return true }
        let ratio = Double(result.count) / Double(source.count)
        // Dolna granica nisko: poprawka w trakcie potrafi wyciąć większość zdania. Przed odpowiedzią
        // na pytanie („Paryż.”) chroni i tak test słów poniżej.
        guard ratio >= 0.15, ratio <= 1.4 else { return false }
        // Słowo „pasuje”, gdy występuje w transkrypcji albo ma z którymś wspólny początek (odmiana,
        // poprawione znaki: „stejdżing” → „staging” nie pasuje, ale to pojedyncze słowa, nie większość).
        let known = Set(source)
        let stems = Set(source.map { String($0.prefix(4)) })
        let matching = result.filter { known.contains($0) || stems.contains(String($0.prefix(4))) }.count
        return Double(matching) / Double(result.count) >= 0.6
    }
}
