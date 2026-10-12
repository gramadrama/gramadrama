import Foundation
import GramaCore
import GramaBackend
@preconcurrency import WhisperKit

/// `GramaDrama --transcribe <plik audio>… [--lang=pl|en|auto] [--no-clean] [--always-clean] [--words="A, B"]
/// [--cold] [--prewarm] [--stream] [--speech-server=<adres> [--speech-api=openai|whispercpp] [--speech-model=<nazwa>]]`.
/// `--cold` najpierw wysyła korektę ⌥⌘G (inny prompt w cache modelu), `--prewarm`
/// przelicza prompt dyktowania przed porządkowaniem — jak aplikacja, gdy trzymasz Fn. `--stream` odtwarza
/// nagranie w czasie rzeczywistym z podglądem jak w pastylce; czas Whispera liczy się wtedy od końca nagrania.
/// `--speech-server`: rozpoznanie na serwerze mowy zamiast na Macu (klucz API ze zmiennej GRAMADRAMA_SPEECH_KEY).
/// Cała ścieżka dyktowania bez mikrofonu i bez wklejania: ten sam model, podpowiedź i porządkowanie
/// co w aplikacji. Wypisuje surową transkrypcję, wynik po porządkowaniu i czasy.
@MainActor
enum DictationCLI {
    static func run(arguments: [String]) async {
        let files = arguments.filter { !$0.hasPrefix("--") }
        guard !files.isEmpty else {
            print("Użycie: GramaDrama --transcribe <plik.wav|m4a>… [--lang=pl|en|auto] [--no-clean] [--always-clean] [--words=\"A, B\"] [--stream]"
                  + " [--speech-server=<adres> --speech-api=openai|whispercpp --speech-model=<nazwa>]")
            return
        }
        let language = arguments.first { $0.hasPrefix("--lang=") }.flatMap { DictationLanguage(rawValue: String($0.dropFirst(7))) }
            ?? DictationSettings.shared.language
        let vocabulary = arguments.first { $0.hasPrefix("--words=") }.map { DictationSettings.parseVocabulary(String($0.dropFirst(8))) }
            ?? DictationSettings.shared.vocabulary
        let clean = !arguments.contains("--no-clean") && DictationSettings.shared.cleanup != .off

        var server: SpeechServerClient?
        if let address = arguments.first(where: { $0.hasPrefix("--speech-server=") }).map({ String($0.dropFirst(16)) }) {
            let api = arguments.first { $0.hasPrefix("--speech-api=") }.flatMap { SpeechServerAPI(rawValue: String($0.dropFirst(13))) } ?? .openAI
            let model = arguments.first { $0.hasPrefix("--speech-model=") }.map { String($0.dropFirst(15)) } ?? "whisper-1"
            guard let endpoint = SpeechServerEndpoint(address: address, api: api) else {
                print("! Serwer mowy: \(SpeechServerEndpoint.problem(address) ?? "nieprawidłowy adres")")
                return
            }
            let client = SpeechServerClient(endpoint: endpoint, model: model,
                                            key: ProcessInfo.processInfo.environment["GRAMADRAMA_SPEECH_KEY"] ?? "")
            print("Serwer mowy: \(endpoint.transcriptionURL.absoluteString) (\(api.label)) · stan: \(await client.check())")
            server = client
        } else {
            let loadStarted = Date()
            do {
                try await SpeechModel.shared.prepare()
            } catch {
                print("! Model mowy: \(error.localizedDescription)")
                return
            }
            print(String(format: "Model %@ wczytany w %.1f s", SpeechModel.variant, Date().timeIntervalSince(loadStarted)))
        }
        print("Język: \(language.rawValue) · podpowiedź: \(SpeechModel.prompt(vocabulary: vocabulary, language: language))\n")

        for file in files {
            let samples: [Float]
            do {
                samples = try AudioProcessor.loadAudioAsFloatArray(fromPath: file)
            } catch {
                print("! \(file): \(error.localizedDescription)")
                continue
            }
            let seconds = Double(samples.count) / AudioRecorder.sampleRate
            if arguments.contains("--stream"), server == nil {
                print("## \((file as NSString).lastPathComponent) — podgląd (‹niepewne›)")
                await stream(samples, language: language, vocabulary: vocabulary)
            }
            let started = Date()
            let raw: String
            do {
                raw = try await SpeechRecognition.transcribe(samples, language: language, vocabulary: vocabulary, server: server)
            } catch {
                print("! \(file): \(error.localizedDescription)")
                continue
            }
            let asr = Date().timeIntervalSince(started)
            print(String(format: "## %@ (%.1f s nagrania)\n  %@ %.2f s: %@", (file as NSString).lastPathComponent, seconds,
                         server == nil ? "Whisper" : "Serwer", asr, raw))
            let tidied = DictationTidy.apply(raw)
            if tidied != raw { print("  Lokalnie:     \(tidied)") }
            guard clean, !raw.isBlank else { continue }
            guard Prompts.dictationNeedsCleanup(raw) || arguments.contains("--always-clean")
                    || DictationSettings.shared.cleanup == .always else {
                print("  Bielik  pominięty — bez wypełniaczy i autokorekt (--always-clean wymusza)")
                continue
            }
            if arguments.contains("--cold") {
                _ = try? await Backend().correct("To jest zdanie ktore trzeba poprawic.", mode: .fix) { _ in }
            }
            if arguments.contains("--prewarm") {
                let started = Date()
                await Backend().prewarmDictation(vocabulary: vocabulary)
                print(String(format: "  (przeliczenie promptu %.2f s — w aplikacji w trakcie mówienia)", Date().timeIntervalSince(started)))
            }
            let cleanStarted = Date()
            do {
                let cleaned = try await Backend().cleanDictation(raw, vocabulary: vocabulary)
                let safe = Prompts.dictationCleanupLooksSafe(cleaned, transcript: raw)
                print(String(format: "  Bielik  %.2f s: %@%@", Date().timeIntervalSince(cleanStarted), cleaned,
                             safe ? "" : "  [ODRZUCONE — za duża zmiana, wkleiłbym surowy tekst]"))
            } catch {
                print("  Bielik: ! \(error.localizedDescription)")
            }
        }
    }

    /// Plik „nagrywa się” w czasie rzeczywistym, a podgląd liczy jak przy trzymanym klawiszu i na koniec zostaje
    /// przerwany — jak po puszczeniu klawisza.
    private static func stream(_ samples: [Float], language: DictationLanguage, vocabulary: [String]) async {
        let started = Date()
        let preview = DictationPreview(language: language, vocabulary: vocabulary)
        preview.onChange = { text in
            print(String(format: "  %5.1f s  %@ ‹%@›", Date().timeIntervalSince(started), text.confirmed, text.pending))
        }
        preview.start { from in
            let total = min(samples.count, Int(Date().timeIntervalSince(started) * AudioRecorder.sampleRate))
            return (from < total ? Array(samples[from..<total]) : [], total, 1)
        }
        try? await Task.sleep(nanoseconds: UInt64(Double(samples.count) / AudioRecorder.sampleRate * 1_000_000_000))
        preview.cancel()
        print(String(format: "  podgląd: %d przebiegów, średnio %.2f s", preview.passes,
                     preview.passes == 0 ? 0 : preview.passSeconds / Double(preview.passes)))
    }
}
