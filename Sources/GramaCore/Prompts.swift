import Foundation

/// Wiadomości do modelu: prompty systemowe, przykłady few-shot i prompty użytkownika dla każdego trybu.
/// Wspólne z wersją na Linuksa przez bench/prompts.golden.json (`--dump-prompts`).
package enum Prompts {
    /// Pełna lista wiadomości wysyłana do modelu (też dla `--dump-prompts` i porównania z Linuksem). `languages`:
    /// wybrane języki tekstu (profil promptów i rozpoznawanie języka); `prompts`: fragmenty z plików użytkownika
    /// (`PromptFiles`), `.defaults` — same prompty macOS. `spelling`: słowa słownika, których tekst używa
    /// (`UserDictionary.relevant`) — notka o ich pisowni stoi przed tekstem w ostatniej wiadomości, więc początek rozmowy
    /// zostaje ten sam; nie w tłumaczeniu, które może je przetłumaczyć.
    package static func messages(for text: String, mode: Mode, target: TranslationTarget? = nil,
                                 languages: TextLanguages = .standard, prompts: PromptFiles = .current(),
                                 spelling: [String] = []) -> [[String: String]] {
        let p = prompts.forLanguages(languages)
        if mode == .translate {
            let target = target ?? .automatic(for: text, in: languages)
            var result = [["role": "system", "content": fill(p.text("translate.txt"), ["target": p.name("target", target.code)])]]
            for example in p.examples("translate-examples.json") {
                result += [["role": "user", "content": translationUserPrompt(example.text, target: example.language, p)],
                           ["role": "assistant", "content": example.output]]
            }
            return result + [["role": "user", "content": translationUserPrompt(text, target: target.code, p)]]
        }
        var result = [["role": "system", "content": systemPrompt(mode, p)]]
        if mode == .fix {
            for example in p.examples("examples.json") {
                result += [["role": "user", "content": userPrompt(example.text, language: example.language, p)],
                           ["role": "assistant", "content": example.output]]
            }
        }
        return result + [["role": "user", "content": UserDictionary.spellingNote(spelling)
                            + userPrompt(text, language: TextLanguage.detect(text, among: languages).code, p)]]
    }

    static func systemPrompt(_ mode: Mode, _ p: PromptFiles) -> String {
        p.text("intro.txt") + "\nTask: " + p.text("task-\(mode.rawValue).txt") + "\n" + p.text("rules.txt") + "\n"
            + p.text(mode == .fix ? "last-rule-fix.txt" : "last-rule-rewrite.txt")
    }

    static func userPrompt(_ text: String, language: String, _ p: PromptFiles) -> String {
        fill(p.text("user.txt"), ["language": p.name("language", language)], text: text)
    }

    static func translationUserPrompt(_ text: String, target: String, _ p: PromptFiles) -> String {
        fill(p.text("translate-user.txt"), ["target": p.name("target", target)], text: text)
    }

    /// Szablon z podstawionymi znacznikami `{nazwa}`; tekst użytkownika na końcu, więc `{znacznik}` wpisany
    /// w samym tekście zostaje tekstem.
    static func fill(_ template: String, _ values: [String: String], text: String? = nil) -> String {
        var result = template
        for (name, value) in values { result = result.replacingOccurrences(of: "{\(name)}", with: value) }
        if let text { result = result.replacingOccurrences(of: "{text}", with: text) }
        return result
    }

    /// `--dump-prompts <plik>`: wiadomości dla stałych wejść w każdym trybie i kierunku — „złoty” plik,
    /// z którym wersja na Linuksa porównuje swoje prompty.
    package static func dumpPrompts(to path: String) {
        let inputs = ["Mysle ze jutro bedzie lepiej, zobaczymy.", "I think their is a problem with the build.",
                      "Zrobiłem merge do main, teraz trzeba zrobić release notes."]
        var entries: [[String: Any]] = []
        for mode in Mode.allCases {
            for input in inputs {
                let targets: [TranslationTarget?] = mode == .translate ? [.english, .polish] : [nil]
                for target in targets {
                    entries.append(["mode": mode.rawValue, "input": input, "target": target?.rawValue ?? NSNull(),
                                    "messages": messages(for: input, mode: mode, target: target, prompts: .defaults)])
                }
            }
        }
        let document: [String: Any] = [
            "description": "Wiadomości do modelu z macOS Sources/GramaCore/Prompts.swift. Generowane: GramaDrama --dump-prompts. Nie edytować ręcznie.",
            "entries": entries,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: document, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path))
            print("Zapisano \(entries.count) zestawów wiadomości do \(path)")
        }
    }
}
