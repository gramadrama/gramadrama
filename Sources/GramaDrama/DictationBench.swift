import CryptoKit
import Foundation
import GramaCore
@preconcurrency import WhisperKit

/// `GramaDrama --dictation-bench bench/dictation.json [--json=<plik>]`
/// Nagrania z syntezatora (`say`) → ta sama ścieżka co przy dyktowaniu (podpowiedź, ponowna próba, filtr,
/// lokalne porządki, bez Bielika) → błąd słów (WER) i język wyniku. Kod wyjścia 1, gdy wymagany przypadek
/// nie przejdzie. Nagrania leżą w cache i powstają na nowo tylko po zmianie tekstu, głosu albo tempa.
@MainActor
enum DictationBench {
    struct Case: Codable {
        let id: String
        let voice: String
        let lang: String
        let text: String
        var rate: Int?
        /// false = tylko do obserwacji, nie blokuje wydania.
        var required: Bool?
        var maxWER: Double?
        var note: String?
    }

    struct Result: Codable {
        let id: String
        let lang: String
        let required: Bool
        let ok: Bool
        let wer: Double
        let language: String
        let asrMs: Int
        let output: String
        let problems: [String]
    }

    struct Report: Codable {
        let passed: Int
        let failedRequired: Int
        let total: Int
        let meanWER: Double
        let medianAsrMs: Int
        let results: [Result]
    }

    static let defaultMaxWER = 0.2
    static let clipDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Caches/pl.gramadrama.app/bench-clips", isDirectory: true)

    static func run(arguments: [String]) async -> Int32 {
        guard let path = arguments.first(where: { !$0.hasPrefix("--") }),
              let data = FileManager.default.contents(atPath: path),
              let cases = try? JSONDecoder().decode([Case].self, from: data) else {
            print("Użycie: GramaDrama --dictation-bench <dictation.json> [--json=<plik>]")
            return 2
        }
        let jsonPath = arguments.first { $0.hasPrefix("--json=") }.map { String($0.dropFirst(7)) }
        do {
            try await SpeechModel.shared.prepare()
        } catch {
            print("! Model mowy: \(error.localizedDescription)")
            return 2
        }

        var results: [Result] = []
        for c in cases {
            guard let language = DictationLanguage(rawValue: c.lang) else { print("! \(c.id): język \(c.lang)"); return 2 }
            let required = c.required ?? true
            let samples: [Float]
            do {
                samples = try AudioProcessor.loadAudioAsFloatArray(fromPath: try clip(for: c).path)
            } catch {
                print("! \(c.id): nagranie: \(error.localizedDescription)")
                results.append(Result(id: c.id, lang: c.lang, required: required, ok: false, wer: 1, language: "-",
                                      asrMs: 0, output: "", problems: ["brak nagrania"]))
                continue
            }
            let started = Date()
            // Bez własnych słów użytkownika — wynik ma nie zależeć od ustawień na tym Macu.
            let raw = (try? await SpeechModel.shared.transcribe(samples, language: language, vocabulary: [])) ?? ""
            let asrMs = Int(Date().timeIntervalSince(started) * 1000)
            let output = DictationTidy.apply(raw)
            let wer = wordErrorRate(reference: c.text, hypothesis: output)
            let detected = TextLanguage.detect(output)
            var problems: [String] = []
            if wer > (c.maxWER ?? defaultMaxWER) { problems.append(String(format: "WER %.0f%%", wer * 100)) }
            // Wystarczy „nie w drugim języku”: polski z żargonem bywa rozpoznany jako mieszany.
            let expected: TextLanguage = TextLanguage.detect(c.text) == .english ? .english : .polish
            if detected != .mixed, detected != expected { problems.append(detected == .english ? "po angielsku" : "po polsku") }
            let result = Result(id: c.id, lang: c.lang, required: required, ok: problems.isEmpty, wer: wer,
                                language: "\(detected)", asrMs: asrMs, output: output, problems: problems)
            results.append(result)
            let mark = result.ok ? "✓" : required ? "✗" : "~"
            print(String(format: "%@ %-18@ [%@] WER %3.0f%% %5d ms  %@", mark, c.id, c.lang, wer * 100, asrMs, output))
            if !result.ok { print("      oczekiwano: \(c.text)\n      problem:    \(problems.joined(separator: "; "))") }
        }

        let failedRequired = results.filter { $0.required && !$0.ok }.count
        let times = results.map(\.asrMs).sorted()
        let report = Report(passed: results.filter(\.ok).count, failedRequired: failedRequired, total: results.count,
                            meanWER: results.isEmpty ? 0 : results.map(\.wer).reduce(0, +) / Double(results.count),
                            medianAsrMs: times.isEmpty ? 0 : times[times.count / 2], results: results)
        print(String(format: "\n== dyktowanie: %d/%d · wymagane nieudane %d · średni WER %.1f%% · mediana %d ms",
                     report.passed, report.total, failedRequired, report.meanWER * 100, report.medianAsrMs))
        if let jsonPath {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try? encoder.encode(report).write(to: URL(fileURLWithPath: jsonPath))
        }
        return failedRequired == 0 ? 0 : 1
    }

    /// Nagranie 16 kHz mono z `say`, w cache pod skrótem głosu, tempa i tekstu.
    static func clip(for c: Case) throws -> URL {
        let key = "\(c.voice)|\(c.rate ?? 0)|\(c.text)"
        let hash = SHA256.hash(data: Data(key.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
        let url = clipDirectory.appendingPathComponent("\(c.id)-\(hash).wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        try FileManager.default.createDirectory(at: clipDirectory, withIntermediateDirectories: true)
        let say = Process()
        say.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        say.arguments = ["-v", c.voice, "-o", url.path, "--data-format=LEI16@16000"]
            + (c.rate.map { ["-r", "\($0)"] } ?? []) + [c.text]
        try say.run()
        say.waitUntilExit()
        guard say.terminationStatus == 0, FileManager.default.fileExists(atPath: url.path) else {
            throw NSError(domain: "say", code: Int(say.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: "say -v \(c.voice) nie zadziałał (brak głosu?)"])
        }
        return url
    }

    /// Odległość edycyjna na słowach / liczba słów wzorca; bez wielkości liter i interpunkcji.
    nonisolated static func wordErrorRate(reference: String, hypothesis: String) -> Double {
        func words(_ text: String) -> [String] {
            text.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init)
        }
        let ref = words(reference), hyp = words(hypothesis)
        guard !ref.isEmpty else { return hyp.isEmpty ? 0 : 1 }
        var previous = Array(0...hyp.count)
        for (i, r) in ref.enumerated() {
            var current = [i + 1] + Array(repeating: 0, count: hyp.count)
            for (j, h) in hyp.enumerated() {
                current[j + 1] = min(previous[j + 1] + 1, current[j] + 1, previous[j] + (r == h ? 0 : 1))
            }
            previous = current
        }
        return Double(previous[hyp.count]) / Double(ref.count)
    }
}
