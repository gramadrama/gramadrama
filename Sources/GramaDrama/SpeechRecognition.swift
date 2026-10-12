import Foundation
import GramaCore
import GramaBackend

/// Rozpoznanie po puszczeniu klawisza: Whisper na tym Macu (`SpeechModel`) albo serwer mowy z ustawień dyktowania.
/// Po obu stronach ta sama podpowiedź słów, ponowna próba bez niej, gdy polska mowa wyszła po angielsku,
/// i ten sam filtr halucynacji. Bez cichego przejścia na drugi silnik: wybrany serwer, który nie odpowiada,
/// to komunikat, a nie niespodziewane wczytanie modelu (1,6 GB) na Macu.
@MainActor
enum SpeechRecognition {
    /// Adres z ustawień albo błąd z powodem (pusty lub nieprawidłowy) — bez sieci, już przy naciśnięciu klawisza.
    static func configuredEndpoint() throws -> SpeechServerEndpoint {
        let settings = DictationSettings.shared
        if let problem = SpeechServerEndpoint.problem(settings.speechServerURL) {
            throw SpeechServerError.notConfigured(tr("Serwer mowy: {problem} Ustawisz go w zakładce Usługi.", ["problem": problem]))
        }
        guard let endpoint = settings.speechEndpoint else {
            throw SpeechServerError.notConfigured(tr("Serwer mowy: nieprawidłowy adres. Ustawisz go w zakładce Usługi."))
        }
        return endpoint
    }

    /// Klient na tę chwilę: serwer wprost albo — poza siecią domową, po zalogowaniu do dostępu zdalnego — przez bramę
    /// (`Route.speechEndpoint`). Przez bramę idzie token logowania, a nie klucz API serwera.
    static func serverClient() async throws -> (client: SpeechServerClient, viaGateway: Bool) {
        let settings = DictationSettings.shared
        let (endpoint, viaGateway) = await Route.shared.speechEndpoint(try configuredEndpoint())
        return (SpeechServerClient(endpoint: endpoint, model: settings.speechServerModel,
                                   key: viaGateway ? "" : ServerKey.speech.load()), viaGateway)
    }

    /// Komunikat błędu z podpowiedzią: serwer mowy nie odpowiada w tej sieci, a brama jest skonfigurowana,
    /// tylko brakuje logowania — poza domem wystarczy się zalogować.
    static func message(for error: Error) -> String {
        let text = error.localizedDescription
        guard case SpeechServerError.connection = error, RemoteConfig.configuration != nil,
              !RemoteAccount.shared.isSignedIn else { return text }
        return text + " " + tr("Poza domem zaloguj się do dostępu zdalnego (menu GramaDramy → Zaloguj się).")
    }

    /// `server`: nil = Whisper na tym Macu.
    static func transcribe(_ samples: [Float], language: DictationLanguage, vocabulary: [String],
                           server: SpeechServerClient?) async throws -> String {
        guard let server else {
            return try await SpeechModel.shared.transcribe(samples, language: language, vocabulary: vocabulary)
        }
        let prompt = SpeechModel.prompt(vocabulary: vocabulary, language: language)
        let started = Date()
        var text = try await server.transcribe(samples, language: language.whisperCode, prompt: prompt)
        // Druga wysyłka nagrania tylko po szybkiej pierwszej — ponowna próba nie może podwoić długiego czekania.
        if language == .polish, SpeechModel.looksTranslated(text), Date().timeIntervalSince(started) < 10 {
            let retry = try await server.transcribe(samples, language: language.whisperCode, prompt: "")
            let fixed = !SpeechModel.looksTranslated(retry)
            EventLog.write("dictation_english_retry", ["fixed": fixed, "asr": "server"])
            if fixed { text = retry }
        }
        return TranscriptFilter.clean(text, prompt: prompt)
    }

    /// Rodzaj błędu do logu (bez adresów i treści).
    static func errorKind(_ error: Error) -> String {
        switch error {
        case SpeechServerError.notConfigured: "not_configured"
        case SpeechServerError.http(let code, _): "http_\(code)"
        case SpeechServerError.redirect: "redirect"
        case SpeechServerError.connection(let code, _, _): code == .timedOut ? "timeout" : "connection"
        case SpeechServerError.badResponse: "bad_response"
        default: "local"
        }
    }
}
