import Foundation
import GramaCore
import GramaBackend

/// Gdzie rozpoznajemy mowę: Whisper na tym Macu (WhisperKit) albo serwer z API zgodnym z OpenAI / whisper.cpp —
/// ten sam wybór co dla modelu językowego i LanguageTool.
typealias SpeechBackend = ServiceLocation

/// Klawisz, który trzymasz podczas dyktowania.
enum DictationKey: String, CaseIterable, Identifiable {
    /// Fn / 🌐 — domyślnie, bo w układzie Polski Pro oba ⌥ służą do polskich liter (⌥A = ą).
    case fn
    /// Prawy ⌥ — z opóźnieniem startu, żeby szybkie ⌥+litera (ą, ę, ł) nie włączało mikrofonu.
    case rightOption

    var id: String { rawValue }
    var label: String { self == .fn ? "Fn (🌐)" : tr("prawy ⌥") }
}

/// Język mowy przekazywany Whisperowi: jeden z wybranych języków tekstu (Pisanie → Języki tekstu) albo rozpoznanie
/// automatyczne — `dictation_language` i `effective_language` / `alternate_language` na Linuksie.
enum DictationLanguage: String, CaseIterable, Identifiable {
    /// Polski z angielskimi wtrąceniami. Wymuszony, bo automatyczne rozpoznanie na krótkiej wypowiedzi
    /// potrafi uznać polską mowę za angielską i przetłumaczyć ją zamiast zapisać.
    case polish = "pl"
    case english = "en"
    case german = "de"
    case automatic = "auto"

    var id: String { rawValue }

    init(_ language: TextLanguage) { self = DictationLanguage(rawValue: language.code) ?? .automatic }

    /// Język tekstu; nil — rozpoznaje Whisper.
    var textLanguage: TextLanguage? { self == .automatic ? nil : TextLanguage(rawValue: rawValue) }

    func label(in languages: TextLanguages) -> String {
        switch self {
        case .polish: languages.contains(.english) ? tr("polski (z angielskimi słowami)") : tr("polski")
        case .english: tr("angielski")
        case .german: tr("niemiecki")
        case .automatic: tr("rozpoznaj automatycznie")
        }
    }

    /// Do wyboru: wybrane języki tekstu i, przy dwóch, rozpoznanie automatyczne.
    static func choices(in languages: TextLanguages) -> [DictationLanguage] {
        languages.all.map(DictationLanguage.init) + (languages.translates ? [.automatic] : [])
    }

    /// Jeden język wymusza siebie (także zamiast „automatycznie”); wybór spoza wybranych języków (np. po ich zmianie)
    /// to główny język.
    func effective(in languages: TextLanguages) -> DictationLanguage {
        guard languages.translates else { return DictationLanguage(languages.primary) }
        if self == .automatic { return self }
        return textLanguage.map(languages.contains) == true ? self : DictationLanguage(languages.primary)
    }

    /// Język po dociśnięciu ⇧: drugi z pary względem ustawionego (przy „automatycznie” — drugi język); przy jednym
    /// języku ⇧ nic nie zmienia.
    func alternate(in languages: TextLanguages) -> DictationLanguage {
        guard let secondary = languages.secondary else { return DictationLanguage(languages.primary) }
        let current = effective(in: languages)
        return current == .automatic || current.textLanguage == languages.primary ? DictationLanguage(secondary)
                                                                                    : DictationLanguage(languages.primary)
    }

    var shortLabel: String { textLanguage?.shortLabel ?? "auto" }
    /// Kod dla Whispera; nil = rozpoznaj.
    var whisperCode: String? { self == .automatic ? nil : rawValue }
}

/// Opcjonalne porządkowanie transkrypcji przez model. Domyślnie wyłączone:
/// wydłuża oczekiwanie, a wygenerowany tekst wymaga sprawdzenia.
enum DictationCleanup: String, CaseIterable, Identifiable {
    /// Tylko przy wypełniaczach, poprawkach w trakcie i powtórzeniach (`Prompts.dictationNeedsCleanup`).
    case automatic = "auto"
    case always
    case off

    var id: String { rawValue }
    var label: String {
        switch self {
        case .automatic: tr("gdy trzeba")
        case .always: tr("zawsze")
        case .off: tr("nigdy")
        }
    }
}

/// Ustawienia dyktowania — osobno od `Settings`, w tej samej domenie UserDefaults.
final class DictationSettings: ObservableObject {
    static let shared = DictationSettings()
    private let defaults = UserDefaults.standard

    @Published var enabled: Bool { didSet { defaults.set(enabled, forKey: "dictationEnabled") } }
    @Published var key: DictationKey { didSet { defaults.set(key.rawValue, forKey: "dictationKey") } }
    @Published var language: DictationLanguage { didSet { defaults.set(language.rawValue, forKey: "dictationLanguage") } }
    /// Porządkowanie transkrypcji przez model (wypełniacze, interpunkcja, autokorekty mówcy).
    @Published var cleanup: DictationCleanup { didSet { defaults.set(cleanup.rawValue, forKey: "dictationCleanupMode") } }
    /// Własne słowa (imiona, nazwy projektów, żargon) — po przecinku albo w osobnych liniach.
    @Published var vocabularyText: String { didSet { defaults.set(vocabularyText, forKey: "dictationVocabulary") } }
    /// Rozpoznawanie mowy: domyślnie na tym Macu. Serwer dostaje nagranie (WAV) i podpowiedź słów; klucz API
    /// jest w pęku kluczy (`ServerKey.speech`), nie tutaj. Adres i protokół zostają przy powrocie na „Ten Mac”.
    @Published var speechBackend: SpeechBackend { didSet { defaults.set(speechBackend.rawValue, forKey: "speechBackend") } }
    @Published var speechServerURL: String { didSet { defaults.set(speechServerURL, forKey: "speechServerURL") } }
    @Published var speechServerAPI: SpeechServerAPI { didSet { defaults.set(speechServerAPI.rawValue, forKey: "speechServerAPI") } }
    /// Nazwa modelu dla API zgodnego z OpenAI (whisper.cpp wybiera model w konfiguracji serwera).
    @Published var speechServerModel: String { didSet { defaults.set(speechServerModel, forKey: "speechServerModel") } }

    private init() {
        enabled = defaults.object(forKey: "dictationEnabled") as? Bool ?? true
        key = defaults.string(forKey: "dictationKey").flatMap(DictationKey.init) ?? .fn
        language = defaults.string(forKey: "dictationLanguage").flatMap(DictationLanguage.init) ?? .polish
        cleanup = defaults.string(forKey: "dictationCleanupMode").flatMap(DictationCleanup.init)
            ?? .off
        vocabularyText = defaults.string(forKey: "dictationVocabulary") ?? ""
        speechBackend = defaults.string(forKey: "speechBackend").flatMap(SpeechBackend.init) ?? .local
        speechServerURL = defaults.string(forKey: "speechServerURL") ?? ""
        speechServerAPI = defaults.string(forKey: "speechServerAPI").flatMap(SpeechServerAPI.init) ?? .openAI
        speechServerModel = defaults.string(forKey: "speechServerModel") ?? "whisper-1"
    }

    var vocabulary: [String] { Self.parseVocabulary(vocabularyText) }

    /// Serwer mowy z ustawień (nil = pusty albo nieprawidłowy adres).
    var speechEndpoint: SpeechServerEndpoint? { SpeechServerEndpoint(address: speechServerURL, api: speechServerAPI) }

    /// „GramaDrama, Bielik\nOllama,, staging " → ["GramaDrama", "Bielik", "Ollama", "staging"] (bez powtórzeń).
    static func parseVocabulary(_ text: String) -> [String] {
        var seen = Set<String>()
        return text.split(whereSeparator: { $0 == "," || $0 == ";" || $0.isNewline })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && seen.insert($0.lowercased()).inserted }
    }

    /// Ustawienie klawisza 🌐 w macOS („Naciśnij 🌐, aby…”). Wszystko poza „Nic nie rób” (0) otwiera
    /// po puszczeniu Fn emoji, dyktowanie Apple albo zmienia źródło wprowadzania.
    static var globeKeyDoesNothing: Bool {
        (UserDefaults(suiteName: "com.apple.HIToolbox")?.object(forKey: "AppleFnUsageType") as? Int) == 0
    }
}
