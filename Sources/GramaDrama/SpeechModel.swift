import Foundation
import GramaCore
import GramaBackend
@preconcurrency import WhisperKit

/// Rozpoznawanie mowy na Macu: Whisper large-v3-turbo przez WhisperKit (Core ML, Neural Engine).
/// Model (~1,6 GB) pobiera się raz do Application Support, tylko przyciskiem „Pobierz” (Usługi, Dyktowanie) — sam
/// nigdy. Pobrany wczytuje się w tle, gdy dyktowanie jest włączone. Pierwsze wczytanie po pobraniu
/// (i po aktualizacji macOS) kompiluje go pod Neural Engine — to trwa kilka minut, potem sekundę.
@MainActor
final class SpeechModel: ObservableObject {
    static let shared = SpeechModel()

    enum State: Equatable {
        /// Modelu nie ma na dysku: czeka na „Pobierz”.
        case notDownloaded
        case notLoaded
        case downloading(Double)
        case loading
        case ready
        case failed(String)

        var label: String {
            switch self {
            case .notDownloaded: tr("Model mowy (1,6 GB) nie jest jeszcze pobrany.")
            case .notLoaded: tr("Model mowy nie jest jeszcze wczytany.")
            case .downloading(let fraction): tr("Pobieram model mowy (1,6 GB)… {percent}%", ["percent": Int(fraction * 100)])
            case .loading: tr("Wczytuję model mowy… Za pierwszym razem kilka minut.")
            case .ready: tr("Gotowe — Whisper large-v3-turbo działa na tym Macu.")
            case .failed(let message): tr("Model mowy się nie wczytał: {message}", ["message": message])
            }
        }
    }

    @Published private(set) var state: State = SpeechModel.isDownloaded ? .notLoaded : .notDownloaded {
        didSet { if state != oldValue { loadingSince = state == .loading ? Date() : nil } }
    }
    /// Od kiedy trwa wczytywanie — kilka sekund to zwykły start, minuty to kompilacja pod Neural Engine.
    private(set) var loadingSince: Date?
    private var loaded: LoadedWhisper?
    private var loading: Task<LoadedWhisper, Error>?

    nonisolated static let variant = "openai_whisper-large-v3-v20240930_turbo"
    nonisolated static let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("GramaDrama/Models", isDirectory: true)
    private nonisolated static var modelFolder: URL {
        directory.appendingPathComponent("models/argmaxinc/whisperkit-coreml/\(variant)", isDirectory: true)
    }
    private nonisolated static let requiredFiles = ["AudioEncoder.mlmodelc", "MelSpectrogram.mlmodelc", "TextDecoder.mlmodelc", "config.json"]

    /// Model jest na dysku (wcześniej pobrany): wystarczy go wczytać.
    nonisolated static var isDownloaded: Bool {
        requiredFiles.allSatisfy { FileManager.default.fileExists(atPath: modelFolder.appendingPathComponent($0).path) }
    }

    /// Terminy, które Whisper po polsku zapisuje fonetycznie („stejdżing”, „kod riwju”), jeśli ich nie podpowiemy.
    nonisolated static let defaultVocabulary = ["deploy", "staging", "PR", "merge", "code review", "feedback", "release", "Jira", "Slack"]

    /// Pobiera (tylko raz) i wczytuje model — przycisk „Pobierz” i „Wczytaj teraz”; dyktowanie i włączenie dyktowania
    /// wołają je tylko dla pobranego modelu. Równoczesne wywołania czekają na to samo wczytywanie.
    func prepare() async throws {
        _ = try await model()
    }

    private func model() async throws -> LoadedWhisper {
        if let loaded { return loaded }
        if let loading { return try await loading.value }
        let task = Task { try await load() }
        loading = task
        do {
            let model = try await task.value
            loaded = model
            state = .ready
            loading = nil
            return model
        } catch {
            loading = nil
            state = .failed(error.localizedDescription)
            throw error
        }
    }

    private func load() async throws -> LoadedWhisper {
        let complete = Self.isDownloaded
        if !complete {
            state = .downloading(0)
            _ = try await WhisperKit.download(variant: Self.variant, downloadBase: Self.directory) { progress in
                let fraction = progress.fractionCompleted
                Task { @MainActor in SpeechModel.shared.state = .downloading(fraction) }
            }
        }
        state = .loading
        let started = Date()
        let model = try await LoadedWhisper.load(folder: Self.modelFolder, tokenizerFolder: Self.directory)
        EventLog.write("speech_model_loaded", ["ms": Int(Date().timeIntervalSince(started) * 1000), "downloaded": !complete])
        return model
    }

    /// `samples`: 16 kHz mono. Zwraca transkrypcję bez znaczników specjalnych i bez typowych halucynacji.
    func transcribe(_ samples: [Float], language: DictationLanguage, vocabulary: [String]) async throws -> String {
        let model = try await model()
        await acquire()
        defer { release() }
        let prompt = Self.prompt(vocabulary: vocabulary, language: language)
        var text = try await model.transcribe(samples, language: language, prompt: prompt)
        // Mimo wymuszonego <|pl|> Whisper potrafi oddać polską mowę po angielsku (tłumaczy). Wtedy
        // jedna próba bez podpowiedzi — to ona najczęściej ciągnie go w stronę angielskiego.
        if language == .polish, Self.looksTranslated(text) {
            let retry = try await model.transcribe(samples, language: language, prompt: "")
            let fixed = !Self.looksTranslated(retry)
            EventLog.write("dictation_english_retry", ["fixed": fixed])
            if fixed { text = retry }
        }
        return TranscriptFilter.clean(text, prompt: prompt)
    }

    /// Przebieg do podglądu w trakcie mówienia: bez ponownej próby i bez filtra (filtruje `DictationPreviewText`).
    /// Nil, gdy model nie jest gotowy albo trwa inne rozpoznanie — podgląd nigdy nie czeka ani nie wczytuje
    /// modelu. Przerwany (Task.cancel) kończy się po bieżącym tokenie.
    func previewTranscript(_ samples: [Float], language: DictationLanguage, prompt: String) async throws -> String? {
        guard let loaded, !busy else { return nil }
        busy = true
        defer { release() }
        return try await loaded.transcribe(samples, language: language, prompt: prompt)
    }

    // WhisperKit nie znosi równoległych przebiegów: rozpoznanie po puszczeniu klawisza czeka, aż przerwany
    // przebieg podglądu się skończy. Wszystko na głównym wątku, więc wystarczy flaga i kolejka.
    private var busy = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    private func acquire() async {
        guard busy else {
            busy = true
            return
        }
        await withCheckedContinuation { waiting.append($0) }
    }

    private func release() {
        if waiting.isEmpty { busy = false } else { waiting.removeFirst().resume() }
    }

    /// Cały wynik po angielsku przy dyktowaniu po polsku. Krótkie „OK, thanks” nie liczą się.
    nonisolated static func looksTranslated(_ text: String) -> Bool {
        text.split(whereSeparator: \.isWhitespace).count >= 4 && TextLanguage.detect(text) == .english
    }

    /// Whisper traktuje podpowiedź jak poprzednie zdanie rozmowy i przejmuje jego język. Sama lista
    /// angielskich słów („deploy, staging, PR…”) popychała polskie dyktowanie w angielski, więc przy
    /// polskim słowa idą w polskim zdaniu.
    nonisolated static func prompt(vocabulary: [String], language: DictationLanguage) -> String {
        var words = vocabulary
        if language != .english {
            for word in defaultVocabulary where !words.contains(where: { $0.caseInsensitiveCompare(word) == .orderedSame }) {
                words.append(word)
            }
        }
        let list = words.joined(separator: ", ")
        guard !words.isEmpty else { return list }
        switch language {
        case .polish: return "Mówię po polsku, czasem wtrącam słowa: \(list)."
        case .german: return "Ich spreche Deutsch und verwende manchmal diese Begriffe: \(list)."  // `whisper_prompt` na Linuksie
        default: return list
        }
    }
}

/// WhisperKit nie jest Sendable. Model trzyma tylko ten obiekt, a przebiegi idą po kolei
/// (`SpeechModel.acquire`, także podgląd) — stąd @unchecked.
private final class LoadedWhisper: @unchecked Sendable {
    private let kit: WhisperKit

    private init(_ kit: WhisperKit) { self.kit = kit }

    static func load(folder: URL, tokenizerFolder: URL) async throws -> LoadedWhisper {
        let config = WhisperKitConfig(model: SpeechModel.variant, modelFolder: folder.path, tokenizerFolder: tokenizerFolder,
                                      verbose: false, logLevel: .error, load: true, download: false)
        return LoadedWhisper(try await WhisperKit(config))
    }

    func transcribe(_ samples: [Float], language: DictationLanguage, prompt: String) async throws -> String {
        var options = DecodingOptions(
            verbose: false,
            task: .transcribe,
            language: language.whisperCode,
            temperature: 0,
            // Wymuszony język = prefiks <|pl|><|transcribe|>; bez niego Whisper rozpoznaje język sam.
            usePrefillPrompt: language != .automatic,
            detectLanguage: language == .automatic,
            skipSpecialTokens: true,
            withoutTimestamps: true
        )
        if let tokenizer = kit.tokenizer, !prompt.isEmpty {
            // Podpowiedź pisowni: Whisper traktuje ją jak poprzedni fragment tekstu i przejmuje jego słownictwo.
            options.promptTokens = tokenizer.encode(text: " " + prompt)
                .filter { $0 < tokenizer.specialTokens.specialTokenBegin }
        }
        // WhisperKit zaczyna okno tylko przed ostatnią sekundą nagrania (windowClipTime = 1 s), więc krótkie
        // „OK”, „Tak” (< 1 s) wychodziły puste. Sekunda ciszy na końcu — Whisper i tak dopełnia ciszą do 30 s.
        let padding = [Float](repeating: 0, count: Int(options.windowClipTime * Float(WhisperKit.sampleRate)))
        let sampleRate = Double(WhisperKit.sampleRate)
        let window = Double((kit.featureExtractor.windowSamples ?? Constants.defaultWindowSamples) - padding.count) / sampleRate
        let pieces = DictationChunking.split(samples, sampleRate: sampleRate, window: window)
        guard pieces.count > 1 else {
            return try await kit.transcribe(audioArray: samples + padding, decodeOptions: options).map(\.text).joined(separator: " ")
        }
        // Bez znaczników czasu WhisperKit przesuwa się o całe okno (30 s), więc może pomijać mowę na jego granicy.
        // Znaczniki czasu nie pomagają — turbo zamyka
        // tekst okna jednym segmentem do 29,98 s — a `chunkingStrategy: .vad` ma stały próg głośności (RMS 0,02),
        // więc przy cichym mikrofonie cała mowa jest dla niego ciszą i tnie w środku słowa. Dłuższe nagranie
        // dzielimy więc w pauzach (`DictationChunking.split`), a kawałki WhisperKit rozpoznaje równolegle.
        // Kawałek bez mowy pomijamy: z ciszy Whisper zmyśla „Dziękuję za uwagę.”.
        let spoken = pieces.map { Array(samples[$0]) }
            .filter { DictationChunking.hasSpeech($0, sampleRate: sampleRate, threshold: DictationController.silencePeak) }
        let results = await kit.transcribeWithResults(audioArrays: spoken.map { $0 + padding }, decodeOptions: options)
        return try results.map { try $0.get().map(\.text).joined(separator: " ") }.joined(separator: " ")
    }
}
