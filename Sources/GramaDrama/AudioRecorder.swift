import AVFoundation
import GramaCore

/// Nagrywanie z mikrofonu prosto do formatu Whispera: 16 kHz, mono, Float32.
/// Nowy AVAudioEngine przy każdym nagraniu — po zmianie mikrofonu (AirPods, monitor) stary silnik
/// trzymałby nieaktualny format wejścia.
final class AudioRecorder: @unchecked Sendable {
    static let sampleRate = 16_000.0
    /// Twardy limit (ok. 11 MB próbek) — dłuższe nagranie kończymy sami.
    static let maxSeconds = 180.0

    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var samples: [Float] = []
    private var currentLevel: Float = 0
    private var peakLevel: Float = 0

    /// Bieżąca głośność 0…1 (do wskaźnika w HUD).
    var level: Float { lock.withLock { currentLevel } }

    enum RecorderError: LocalizedError {
        case noInput

        var errorDescription: String? { tr("Nie znalazłem mikrofonu.") }
    }

    func start() throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Self.sampleRate, channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: inputFormat, to: target) else { throw RecorderError.noInput }
        lock.withLock {
            samples = []
            samples.reserveCapacity(Int(Self.sampleRate) * 30)
            currentLevel = 0
            peakLevel = 0
        }
        let ratio = Self.sampleRate / inputFormat.sampleRate
        input.installTap(onBus: 0, bufferSize: 2048, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
            guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
            var consumed = false
            var error: NSError?
            converter.convert(to: output, error: &error) { _, status in
                if consumed {
                    status.pointee = .noDataNow
                    return nil
                }
                consumed = true
                status.pointee = .haveData
                return buffer
            }
            guard error == nil, let channel = output.floatChannelData?[0] else { return }
            self.append(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
    }

    /// Próbki od `start` w trakcie nagrania (do podglądu), wszystkie dotąd nagrane i najgłośniejszy fragment.
    func snapshot(from start: Int) -> (samples: [Float], total: Int, peak: Float) {
        lock.withLock { (start < samples.count ? Array(samples[start...]) : [], samples.count, peakLevel) }
    }

    /// Kończy nagranie i zwraca próbki oraz najgłośniejszy fragment (RMS 0…1) — do odrzucenia ciszy.
    func stop() -> (samples: [Float], peak: Float) {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        return lock.withLock { (samples, peakLevel) }
    }

    private func append(_ chunk: UnsafeBufferPointer<Float>) {
        guard !chunk.isEmpty else { return }
        var sum: Float = 0
        for sample in chunk { sum += sample * sample }
        let rms = (sum / Float(chunk.count)).squareRoot()
        lock.withLock {
            if Double(samples.count) < Self.maxSeconds * Self.sampleRate { samples.append(contentsOf: chunk) }
            peakLevel = max(peakLevel, rms)
            // Skala logarytmiczna: -50 dB → 0, -10 dB → 1.
            let db = 20 * log10(max(rms, 1e-6))
            currentLevel = min(max((db + 50) / 40, 0), 1)
        }
    }
}
