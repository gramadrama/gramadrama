import Foundation
import GramaCore

/// Podgląd w pastylce, póki trzymasz klawisz: po każdej sekundzie nowego nagrania Whisper rozpoznaje końcówkę
/// od ostatniego pewnego miejsca (`DictationPreviewText`, cięcie w pauzach: `DictationChunking`). Tylko do
/// oglądania — po puszczeniu klawisza kontroler przerywa podgląd, a wklejamy pełne rozpoznanie całego nagrania.
@MainActor
final class DictationPreview {
    /// Próbki od podanego miejsca, liczba wszystkich dotąd i najgłośniejszy fragment — z mikrofonu albo z pliku (CLI).
    typealias Source = @Sendable (Int) -> (samples: [Float], total: Int, peak: Float)

    /// Nowy przebieg dopiero po tylu sekundach nowego nagrania.
    static let interval = 1.0

    private(set) var text = DictationPreviewText()
    private(set) var passes = 0
    private(set) var passSeconds: TimeInterval = 0
    /// Zmienia się w trakcie, gdy dociśniesz ⇧.
    var language: DictationLanguage
    var onChange: ((DictationPreviewText) -> Void)?
    private let vocabulary: [String]
    private var task: Task<Void, Never>?

    init(language: DictationLanguage, vocabulary: [String]) {
        self.language = language
        self.vocabulary = vocabulary
    }

    func start(_ source: @escaping Source) {
        task = Task { [weak self] in
            var lastTotal = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 100_000_000)
                guard let self else { return }
                var (samples, total, peak) = source(self.text.confirmedSamples)
                // Przed pierwszym słowem nie ma czego pokazać, a z ciszy Whisper zmyśliłby tekst.
                guard Double(total - lastTotal) >= Self.interval * AudioRecorder.sampleRate,
                      peak >= DictationController.silencePeak else { continue }
                lastTotal = total
                let previous = self.text
                let prompt = SpeechModel.prompt(vocabulary: self.vocabulary, language: self.language)
                // Długa niepewna część: kawałek do pauzy rozpoznajemy raz i odkładamy, dalej liczymy tylko resztę.
                if let cut = DictationChunking.cut(in: samples, sampleRate: AudioRecorder.sampleRate) {
                    guard let chunk = await self.pass(Array(samples[..<cut]), prompt: prompt) else { continue }
                    self.text.confirm(chunk, samples: cut, prompt: prompt)
                    samples.removeFirst(cut)
                }
                if DictationChunking.hasSpeech(samples, sampleRate: AudioRecorder.sampleRate, threshold: DictationController.silencePeak),
                   let rest = await self.pass(samples, prompt: prompt) {
                    self.text.setPending(rest, prompt: prompt)
                }
                if self.text != previous { self.onChange?(self.text) }
            }
        }
    }

    /// Nil, gdy podgląd przerwano albo model jest zajęty — wtedy nic nie zmieniamy.
    private func pass(_ samples: [Float], prompt: String) async -> String? {
        let started = Date()
        guard let text = try? await SpeechModel.shared.previewTranscript(samples, language: language, prompt: prompt),
              !Task.isCancelled else { return nil }
        passes += 1
        passSeconds += Date().timeIntervalSince(started)
        return text
    }

    /// Przerywa bieżący przebieg po najbliższym tokenie; rozpoznanie całości czeka na to w `SpeechModel`.
    func cancel() {
        task?.cancel()
        task = nil
    }
}
