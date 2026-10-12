import Foundation
import Testing
@testable import GramaCore
@testable import GramaIMEProtocol

/// Wspólne pliki z wersją na Linuksa: te same przypadki sprawdzają obie implementacje.
/// Generuje je aplikacja (`--selftest --dump-cases`, `--dump-prompts`); tu sprawdzamy, że Core je spełnia.
enum Fixtures {
    static let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

    static func json(_ path: String) throws -> [String: Any] {
        let data = try Data(contentsOf: root.appendingPathComponent(path))
        return try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    struct CleanCase: CustomTestStringConvertible, Sendable {
        let name, output, original, expected: String
        let strict: Bool
        var testDescription: String { name }
    }

    static let cleanCases: [CleanCase] = {
        guard let document = try? json("tests/postprocess_cases.json"),
              let cases = document["cases"] as? [[String: Any]] else { return [] }
        return cases.compactMap { c in
            guard let name = c["name"] as? String, let output = c["output"] as? String,
                  let original = c["original"] as? String, let expected = c["expected"] as? String else { return nil }
            return CleanCase(name: name, output: output, original: original, expected: expected,
                             strict: c["strict"] as? Bool ?? false)
        }
    }()

    struct PromptCase: CustomTestStringConvertible, Sendable {
        let mode: Mode
        let input: String
        let target: TranslationTarget?
        let messages: [[String: String]]
        var testDescription: String { "\(mode.rawValue)\(target.map { " → \($0.rawValue)" } ?? ""): \(input.prefix(30))" }
    }

    static let promptCases: [PromptCase] = {
        guard let document = try? json("bench/prompts.golden.json"),
              let entries = document["entries"] as? [[String: Any]] else { return [] }
        return entries.compactMap { e in
            guard let mode = (e["mode"] as? String).flatMap(Mode.init(rawValue:)), let input = e["input"] as? String,
                  let messages = e["messages"] as? [[String: String]] else { return nil }
            return PromptCase(mode: mode, input: input, target: (e["target"] as? String).flatMap(TranslationTarget.init(rawValue:)),
                              messages: messages)
        }
    }()
}

@Suite("Wspólne przypadki z Linuksem")
struct SharedFixturesTests {
    @Test("pliki z przypadkami są wczytane")
    func loaded() {
        #expect(Fixtures.cleanCases.count >= 60)
        #expect(Fixtures.promptCases.count >= 20)
    }

    @Test("PostProcess.clean", arguments: Fixtures.cleanCases)
    func clean(_ c: Fixtures.CleanCase) {
        #expect(PostProcess.clean(c.output, original: c.original, strictCommentary: c.strict) == c.expected)
    }

    @Test("prompty zgodne z bench/prompts.golden.json", arguments: Fixtures.promptCases)
    func prompts(_ c: Fixtures.PromptCase) {
        #expect(Prompts.messages(for: c.input, mode: c.mode, target: c.target, prompts: .defaults) == c.messages)
    }
}

/// `tests/ime_cases.json` — kanał tekstu przez metodę wprowadzania: wybór fragmentu i podmiany słowami.
/// Ten sam plik czyta silnik IBus na Linuksie. Podmiany sprawdzamy po wyniku (a nie po dokładnej liście
/// fragmentów), bo diff słów może się różnić między implementacjami.
enum ImeFixtures {
    struct ScopeCase: CustomTestStringConvertible, Sendable {
        let name: String
        let raw: [String: String]
        let numbers: [String: Int]
        let flags: [String: Bool]
        let wholeFieldWhenComplete: Bool
        let candidates: [[String: String]]
        let refusal: String?
        var testDescription: String { name }

        var reply: ImeProtocol.Reply {
            var reply = ImeProtocol.Reply(id: "fixture", ok: true)
            reply.windowStart = numbers["windowStart"] ?? 0
            reply.before = raw["before"] ?? ""
            reply.selected = raw["selected"] ?? ""
            reply.after = raw["after"] ?? ""
            reply.selectionLocation = numbers["selectionLocation"] ?? ((reply.windowStart ?? 0) + ((reply.before ?? "") as NSString).length)
            reply.selectionLength = numbers["selectionLength"] ?? ((reply.selected ?? "") as NSString).length
            reply.shortAfter = flags["shortAfter"]
            reply.selectionTooLong = flags["selectionTooLong"]
            return reply
        }
    }

    struct EditCase: CustomTestStringConvertible, Sendable {
        let name, original, corrected: String
        let base: Int
        let expected: String?
        let failure: String?
        let noChange: Bool
        let untouched: [String]
        let maxEdits: Int?
        let noSeparatorInEdits: Bool
        var testDescription: String { name }
    }

    static let document: [String: Any] = (try? Fixtures.json("tests/ime_cases.json")) ?? [:]

    static let scopeCases: [ScopeCase] = ((document["scope"] as? [[String: Any]]) ?? []).compactMap { c in
        guard let name = c["name"] as? String, let reply = c["reply"] as? [String: Any] else { return nil }
        let candidates = ((c["candidates"] as? [[String: Any]]) ?? []).map { candidate in
            candidate.mapValues { "\($0)" }
        }
        return ScopeCase(name: name, raw: reply.compactMapValues { $0 as? String }, numbers: reply.compactMapValues { $0 as? Int },
                         flags: reply.compactMapValues { $0 as? Bool }, wholeFieldWhenComplete: c["wholeFieldWhenComplete"] as? Bool ?? false,
                         candidates: candidates, refusal: c["refusal"] as? String)
    }

    static let editCases: [EditCase] = ((document["edits"] as? [[String: Any]]) ?? []).compactMap { c in
        guard let name = c["name"] as? String, let original = c["original"] as? String, let corrected = c["corrected"] as? String else { return nil }
        return EditCase(name: name, original: original, corrected: corrected, base: c["base"] as? Int ?? 0,
                        expected: c["expected"] as? String, failure: c["failure"] as? String, noChange: c["noChange"] as? Bool ?? false,
                        untouched: c["untouched"] as? [String] ?? [], maxEdits: c["maxEdits"] as? Int,
                        noSeparatorInEdits: c["noSeparatorInEdits"] as? Bool ?? false)
    }
}

@Suite("Wspólne przypadki metody wprowadzania")
struct ImeFixturesTests {
    @Test("plik tests/ime_cases.json jest wczytany")
    func loaded() {
        #expect(ImeFixtures.scopeCases.count >= 10)
        #expect(ImeFixtures.editCases.count >= 6)
    }

    @Test("wybór fragmentu", arguments: ImeFixtures.scopeCases)
    func scope(_ c: ImeFixtures.ScopeCase) {
        do {
            let found = try ImeScope.candidates(c.reply, wholeFieldWhenComplete: c.wholeFieldWhenComplete)
            #expect(c.refusal == nil, "oczekiwana odmowa \(c.refusal ?? "")")
            let actual = found.map { ["scope": $0.scope.rawValue, "text": $0.text, "location": "\($0.range.location)"] }
            #expect(actual == c.candidates)
        } catch let refusal as ImeScope.Refusal {
            let name: String = switch refusal {
            case .noText: "noText"
            case .cut: "cut"
            case .attachments: "attachments"
            case .emptyParagraph: "emptyParagraph"
            case .tooLong: "tooLong"
            }
            #expect(name == c.refusal)
        } catch {
            Issue.record("nieoczekiwany błąd \(error)")
        }
    }

    @Test("podmiany słowami", arguments: ImeFixtures.editCases)
    func edits(_ c: ImeFixtures.EditCase) throws {
        do {
            let plan = try ImeEdits.plan(original: c.original, corrected: c.corrected, base: c.base)
            #expect(c.failure == nil)
            guard let plan else { return #expect(c.noChange) }
            #expect(!c.noChange)
            #expect(plan.expected == c.expected)
            // Od końca, bez nakładania; po zastosowaniu — dokładnie oczekiwany tekst.
            #expect(zip(plan.edits, plan.edits.dropFirst()).allSatisfy { $0.location >= $1.location + $1.length })
            let result = NSMutableString(string: c.original)
            for edit in plan.edits {
                result.replaceCharacters(in: NSRange(location: edit.location - c.base, length: edit.length), with: edit.text)
            }
            #expect(result as String == c.expected)
            if let maxEdits = c.maxEdits { #expect(plan.edits.count <= maxEdits) }
            if c.noSeparatorInEdits {
                #expect(plan.edits.allSatisfy { edit in !["\n", "\r", "\u{0B}", "\u{2028}", "\u{2029}"].contains { edit.text.contains($0) } })
            }
            for kept in c.untouched {
                let range = (c.original as NSString).range(of: kept)
                #expect(range.location != NSNotFound)
                #expect(!plan.edits.contains { NSIntersectionRange(NSRange(location: $0.location - c.base, length: $0.length), range).length > 0 },
                        "„\(kept)” objęte podmianą")
            }
        } catch let failure as ImeEdits.Failure {
            #expect(c.failure == "\(failure)")
        }
    }
}

/// `tests/result_check_cases.json` — kontrola wyniku po naprawach; ten sam plik czyta `core.clean` na Linuksie.
@Suite("Wspólne przypadki z Linuksem: kontrola wyniku")
struct ResultCheckTests {
    struct Case: CustomTestStringConvertible, Sendable {
        let name: String
        let original: String
        let output: String
        let mode: Mode
        let refusal: String?
        let result: String?
        var testDescription: String { name }
    }

    static let cases: [Case] = {
        guard let document = try? Fixtures.json("tests/result_check_cases.json"),
              let entries = document["cases"] as? [[String: Any]] else { return [] }
        return entries.compactMap { e in
            guard let name = e["name"] as? String, let original = e["original"] as? String, let output = e["output"] as? String,
                  let mode = (e["mode"] as? String).flatMap(Mode.init(rawValue:)) else { return nil }
            return Case(name: name, original: original, output: output, mode: mode, refusal: e["refusal"] as? String,
                        result: e["result"] as? String)
        }
    }()

    @Test func loaded() { #expect(Self.cases.count >= 30) }

    @Test("PostProcess.checked", arguments: cases)
    func check(_ c: Case) {
        do {
            let result = try PostProcess.checked(c.output, original: c.original, mode: c.mode)
            #expect(c.refusal == nil)
            #expect(result == c.result)
        } catch let refusal as ResultRefusal {
            #expect(refusal.reason == c.refusal)
        } catch {
            Issue.record(error)
        }
    }

    /// Żadnego przypadku sprzątania nie odrzucamy (Luźniej i Tłumacz mogą dodać emoji) — jak test na Linuksie.
    @Test("przypadki sprzątania przechodzą kontrolę", arguments: Fixtures.cleanCases)
    func cleanCasesPass(_ c: Fixtures.CleanCase) throws {
        #expect(try PostProcess.checked(c.output, original: c.original, mode: c.strict ? .translate : .casual) == c.expected)
    }
}

/// `tests/prompt_profiles.json` — profile języków innych niż polski + angielski, zapisane przez Linuksa
/// (`linux/tests/prompt_profiles.py`): Mac buduje te same początki zapytań (bez tekstu użytkownika).
@Suite("Wspólne przypadki z Linuksem: profile języków")
struct PromptProfileTests {
    struct Case: CustomTestStringConvertible, Sendable {
        let languages: TextLanguages
        let mode: String
        let target: TranslationTarget?
        let messages: [[String: String]]
        var testDescription: String { "\(languages.codes.joined(separator: "+")) \(mode)\(target.map { " → \($0.code)" } ?? "")" }
    }

    static let cases: [Case] = {
        guard let document = try? Fixtures.json("tests/prompt_profiles.json"),
              let entries = document["entries"] as? [[String: Any]] else { return [] }
        return entries.compactMap { e in
            guard let codes = e["languages"] as? [String], let first = codes.first.flatMap(TextLanguage.init(rawValue:)),
                  let mode = e["mode"] as? String, let messages = e["messages"] as? [[String: String]] else { return nil }
            let languages = TextLanguages(primary: first, secondary: codes.dropFirst().first.flatMap(TextLanguage.init(rawValue:)))
            let target = (e["target"] as? String).flatMap(TextLanguage.init(rawValue:)).map(TranslationTarget.init)
            return Case(languages: languages, mode: mode, target: target, messages: messages)
        }
    }()

    @Test func loaded() { #expect(Self.cases.count >= 40) }

    @Test("ten sam początek zapytania co na Linuksie", arguments: cases)
    func profile(_ c: Case) {
        guard c.mode == "cleanup" else {
            let built = Prompts.messages(for: "x", mode: Mode(rawValue: c.mode)!, target: c.target, languages: c.languages, prompts: .defaults)
            #expect(Array(built.dropLast()) == c.messages)
            return
        }
        // Przykłady porządkowania dyktowania mają język rozpoznany w chwili budowania, a rozpoznawanie celowo się
        // różni (Linux: słowa funkcyjne, macOS: NaturalLanguage — „ähm ich schicke die Datei…” to dla Linuksa tekst
        // mieszany). Porównujemy wszystko poza nazwą rozpoznanego języka.
        func withoutLanguage(_ messages: [[String: String]]) -> [[String: String]] {
            messages.map { message in
                message.mapValues { $0.replacingOccurrences(of: #"Language: [^.]+\. Your answer"#, with: "Language: …. Your answer",
                                                            options: .regularExpression) }
            }
        }
        let built = Prompts.dictationMessages(for: "x", vocabulary: [], languages: c.languages, prompts: .defaults)
        #expect(withoutLanguage(Array(built.dropLast())) == withoutLanguage(c.messages))
    }
}
