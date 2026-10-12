import Foundation
import GramaCore
import GramaBackend

/// `GramaDrama --bench bench/cases.json model1 [model2…] [--server=<adres>] [--api=ollama|openai]`
/// Ten sam prompt i post-processing co w aplikacji — wyniki odpowiadają temu, co zobaczy użytkownik.
/// Benchmark ma osobną domenę ustawień, więc serwer podaje `--server` (bez niego: model z ustawień tej domeny).
enum Bench {
    struct Case: Decodable {
        let lang: String
        let input: String
        /// Oczekiwany wynik (tryb Popraw). Tłumaczenie nie ma jednego wzorca — zamiast tego `keep`.
        let expected: String?
        /// Fragmenty, które muszą przetrwać tłumaczenie (emoji, @wzmianki, URL-e, kod).
        let keep: [String]?
    }

    /// Tłumaczenie jest OK, gdy jest w docelowym języku, różni się od oryginału i zachowało chronione fragmenty.
    static func checkTranslation(_ output: String, _ c: Case) -> (ok: Bool, problems: [String]) {
        let target = TranslationTarget.automatic(for: c.input)
        var problems: [String] = []
        let detected = TextLanguage.detect(output)
        if target == .english, detected == .polish { problems.append("nie po angielsku") }
        if target == .polish, detected == .english { problems.append("nie po polsku") }
        if output.trimmed == c.input.trimmed { problems.append("bez tłumaczenia") }
        let missing = (c.keep ?? []).filter { !output.contains($0) }
        if !missing.isEmpty { problems.append("zgubione: \(missing.joined(separator: " "))") }
        if output.range(of: #"(?i)\b(translation|translated|tłumaczenie|przetłumaczon)"#, options: .regularExpression) != nil,
           c.input.range(of: #"(?i)(translat|tłumacz)"#, options: .regularExpression) == nil {
            problems.append("komentarz modelu")
        }
        return (problems.isEmpty, problems)
    }

    /// Podsumowanie jednego modelu dla `--json=<plik>` (czyta je scripts/release-check.sh).
    struct Summary: Encodable {
        let model: String, mode: String
        let exact: Int, total: Int, falseAlarms: Int, cleanCases: Int, errors: Int
        /// Tryb Popraw, nieudane przypadki z błędem: błąd został (`missed`) albo model zmienił coś innego (`wrongEdits`
        /// — wymyślone słowo, synonim, inne słowo; groźniejsze niż pominięcie).
        let missed: Int, wrongEdits: Int
        let medianSeconds: Double, maxSeconds: Double, tokensPerSecond: Double
    }

    static func run(arguments: [String]) async {
        let flags = Set(arguments.filter { $0.hasPrefix("--") })
        let positional = arguments.filter { !$0.hasPrefix("--") }
        guard let path = positional.first,
              let data = FileManager.default.contents(atPath: path),
              let cases = try? JSONDecoder().decode([Case].self, from: data) else {
            print("Użycie: GramaDrama --bench <cases.json> <model>… [--mode=fix|improve|formal|casual|shorter|translate]")
            return
        }
        // --mode=formal|casual|improve|shorter — tryby przepisujące nie mają „oczekiwanego" wyniku,
        // więc benchmark wypisuje odpowiedzi do przejrzenia (i pilnuje, czy nie są puste lub obcięte).
        let mode = flags.first { $0.hasPrefix("--mode=") }.flatMap { Mode(rawValue: String($0.dropFirst(7))) } ?? .fix
        let models = positional.dropFirst().isEmpty ? [Settings.shared.model] : Array(positional.dropFirst())
        func value(_ flag: String) -> String? { flags.first { $0.hasPrefix(flag) }.map { String($0.dropFirst(flag.count)) } }
        let api = value("--api=").flatMap(ModelServerAPI.init) ?? .ollama
        var server: ModelServerEndpoint?
        if let address = value("--server=") {
            guard let endpoint = ModelServerEndpoint(address: address, api: api) else {
                print("Nieprawidłowy adres serwera: \(ModelServerEndpoint.problem(address) ?? address)")
                return
            }
            server = endpoint
        }
        var summary: [String] = []
        var summaries: [Summary] = []

        for model in models {
            let backend = Backend(model: model, server: server, cache: false)
            print("\n### \(model)")
            await backend.warmUp()

            var exact = 0, falseAlarms = 0, cleanCases = 0, errors = 0, missed = 0, wrongEdits = 0
            var latencies: [Double] = [], speeds: [Double] = []
            for c in cases {
                let started = Date()
                let output: String
                do {
                    output = try await backend.correct(c.input, mode: mode) { _ in }
                } catch {
                    errors += 1
                    print("  ! \(error.localizedDescription)")
                    continue
                }
                latencies.append(Date().timeIntervalSince(started))
                speeds.append(backend.tokensPerSecond)
                let expected = c.expected ?? ""
                var ok = output == expected
                var problems: [String] = []
                if mode == .translate {
                    (ok, problems) = checkTranslation(output, c)
                } else if c.input == expected {
                    cleanCases += 1
                    falseAlarms += output == c.input ? 0 : 1
                } else if mode == .fix, !ok {
                    if output == c.input {
                        missed += 1
                        problems.append("pominięte")
                    } else {
                        wrongEdits += 1
                        problems.append("inna zmiana niż oczekiwana")
                    }
                }
                exact += ok ? 1 : 0
                print(String(format: "  %@ [%@] %4.1fs  %@", ok ? "✓" : "✗", c.lang, latencies.last!,
                             output.replacingOccurrences(of: "\n", with: "⏎")))
                if !ok, !expected.isEmpty { print("        oczekiwano: \(expected)") }
                if !problems.isEmpty { print("        problem:    \(problems.joined(separator: "; "))") }
                let raw = backend.lastRawOutput.trimmingCharacters(in: .whitespacesAndNewlines)
                if flags.contains("--raw"), raw != output.trimmingCharacters(in: .whitespacesAndNewlines) {
                    print("        surowo:     \(raw.debugDescription)")
                }
            }
            latencies.sort()
            let median = latencies.isEmpty ? 0 : latencies[latencies.count / 2]
            let speed = speeds.isEmpty ? 0 : speeds.reduce(0, +) / Double(speeds.count)
            var line = String(format: "%@: dokładne %d/%d · fałszywe alarmy %d/%d · mediana %.1fs · max %.1fs · %.1f tok/s",
                              model, exact, cases.count, falseAlarms, cleanCases, median, latencies.last ?? 0, speed)
            if mode == .fix { line += " · pominięte \(missed) · inne zmiany \(wrongEdits)" }
            if errors > 0 { line += " · BŁĘDY: \(errors)/\(cases.count) (wynik niemiarodajny)" }
            print("== " + line)
            summary.append(line)
            summaries.append(Summary(model: model, mode: mode.rawValue, exact: exact, total: cases.count, falseAlarms: falseAlarms,
                                     cleanCases: cleanCases, errors: errors, missed: missed, wrongEdits: wrongEdits, medianSeconds: median,
                                     maxSeconds: latencies.last ?? 0, tokensPerSecond: speed))
        }
        print("\n=== PODSUMOWANIE\n" + summary.joined(separator: "\n"))
        if let json = flags.first(where: { $0.hasPrefix("--json=") }).map({ String($0.dropFirst(7)) }) {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try? encoder.encode(summaries).write(to: URL(fileURLWithPath: json))
        }
    }
}
