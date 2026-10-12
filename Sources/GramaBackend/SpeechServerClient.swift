import Foundation
import GramaCore

/// Rozpoznawanie mowy na serwerze wybranym w ustawieniach dyktowania (API zgodne z OpenAI albo whisper.cpp).
/// Nagranie idzie jako WAV 16 kHz w jednym żądaniu; przekierowań nie wykonujemy (żądanie z nagraniem i kluczem
/// nie może trafić pod inny adres niż wpisany), a w sieci lokalnej łączymy się bez systemowego proxy.
package struct SpeechServerClient: Sendable {
    package let endpoint: SpeechServerEndpoint
    package let model: String
    private let key: String

    package init(endpoint: SpeechServerEndpoint, model: String, key: String) {
        self.endpoint = endpoint
        self.model = model
        self.key = key.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Czas na całe rozpoznanie: wysłanie, obliczenia serwera i odpowiedź. Dłuższe nagranie dostaje więcej czasu.
    package static func deadline(forSeconds seconds: Double) -> TimeInterval {
        min(120, 30 + seconds)
    }

    /// `samples`: 16 kHz mono. `language`: kod Whispera albo nil (serwer rozpoznaje sam). Zwraca jedną linię tekstu.
    package func transcribe(_ samples: [Float], language: String?, prompt: String) async throws -> String {
        let boundary = "GramaDrama-\(UUID().uuidString)"
        let body = Multipart.body(fields: SpeechServer.fields(api: endpoint.api, model: model, language: language, prompt: prompt),
                                  file: ("file", "audio.wav", "audio/wav", WAVEncoder.encode(samples)), boundary: boundary)
        var request = URLRequest(url: endpoint.transcriptionURL)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let data = try await send(request, body: body, deadline: Self.deadline(forSeconds: Double(samples.count) / 16_000))
        guard let text = SpeechServer.transcript(from: data) else { throw SpeechServerError.badResponse }
        return text
    }

    /// Stan serwera do ustawień: odpowiada, wymaga klucza, a może wybrano zły protokół (whisper.cpp nie ma
    /// `/v1/models`, serwer OpenAI nie ma `/health`) — wtedy podpowiadamy właściwy zamiast „działa”.
    package func check() async -> ServiceStatus {
        let started = Date()
        let status: ServiceStatus
        do {
            let data = try await send(URLRequest(url: endpoint.healthURL), deadline: 5)
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            switch endpoint.api {
            case .whisperCpp:
                status = (json?["status"] as? String) == "ok"
                    ? .ok(tr("Działa · {ms} ms", ["ms": ms]) + " · whisper.cpp")
                    : .error(tr("Odpowiada, ale to nie wygląda na gotowy serwer whisper.cpp."))
            case .openAI:
                let wanted = model.trimmed.isEmpty ? "whisper-1" : model.trimmed
                if let list = json?["data"] as? [[String: Any]] {
                    let ids = list.compactMap { $0["id"] as? String }
                    status = !ids.isEmpty && !ids.contains(wanted)
                        ? .warning(tr("Działa ({ms} ms), ale serwer nie wymienia modelu „{model}” — sprawdź nazwę.", ["ms": ms, "model": wanted]))
                        : .ok(tr("Działa · {ms} ms", ["ms": ms]) + " · " + tr("API zgodne z OpenAI"))
                } else if await otherProtocolAnswers() {
                    status = .warning(tr("To serwer whisper.cpp — wybierz protokół „whisper.cpp”."))
                } else {
                    // Coś odpowiada 200, ale nie listą modeli — „działa” byłoby obietnicą bez pokrycia.
                    status = .warning(tr("Odpowiada ({ms} ms), ale nie jak API zgodne z OpenAI — sprawdź adres krótkim dyktowaniem.", ["ms": ms]))
                }
            }
        } catch SpeechServerError.http(let code, let body) where [404, 405, 503].contains(code) {
            if code == 503, endpoint.api == .whisperCpp {
                status = .warning(tr("Serwer whisper.cpp wczytuje model — spróbuj za chwilę."))
            } else if code != 503, await otherProtocolAnswers() {
                status = .warning(endpoint.api == .openAI
                    ? tr("To serwer whisper.cpp — wybierz protokół „whisper.cpp”.")
                    : tr("To serwer zgodny z OpenAI — wybierz ten protokół."))
            } else {
                status = .error(SpeechServer.describe(status: code, body: body))
            }
        } catch {
            status = .error(error.localizedDescription)
        }
        guard !endpoint.isEncrypted, !endpoint.isLocalNetwork, case .ok(let text) = status else { return status }
        return .warning(text + " · " + tr("bez szyfrowania poza siecią lokalną — użyj https://"))
    }

    private func otherProtocolAnswers() async -> Bool {
        guard let other = endpoint.otherProtocol,
              let data = try? await send(URLRequest(url: other.healthURL), deadline: 5) else { return false }
        let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        return other.api == .whisperCpp ? (json?["status"] as? String) == "ok" : json?["data"] is [Any]
    }

    /// Żądanie z kluczem (Bearer) albo — bez klucza, gdy adres należy do skonfigurowanej bramy dostępu zdalnego —
    /// z tokenem logowania; po 401 jedna próba ze świeżym tokenem. Kody inne niż 2xx to `SpeechServerError.http`.
    private func send(_ request: URLRequest, body: Data? = nil, deadline: TimeInterval) async throws -> Data {
        let config = URLSessionConfiguration.ephemeral
        // Serwer liczy rozpoznanie bez wysyłania czegokolwiek — limit bezczynności równy całemu limitowi.
        config.timeoutIntervalForRequest = deadline
        config.timeoutIntervalForResource = deadline
        if endpoint.isLocalNetwork { config.connectionProxyDictionary = [:] }
        let session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }

        for attempt in 0..<2 {
            var request = request
            let gateway = key.isEmpty ? await gatewayToken() : nil
            if let bearer = key.isEmpty ? gateway : key {
                request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
            }
            let data: Data, response: URLResponse
            do {
                if let body {
                    (data, response) = try await session.upload(for: request, from: body)
                } else {
                    (data, response) = try await session.data(for: request)
                }
            } catch let error as URLError {
                if error.code == .cancelled, Task.isCancelled { throw CancellationError() }
                throw SpeechServerError.connection(error.code, host: endpoint.host, timeout: deadline)
            }
            guard let http = response as? HTTPURLResponse else { throw SpeechServerError.badResponse }
            if http.statusCode == 401, gateway != nil, attempt == 0 {
                await RemoteAccount.shared.tokenRejected()
                continue
            }
            if (300..<400).contains(http.statusCode) {
                throw SpeechServerError.redirect(http.statusCode, location: http.value(forHTTPHeaderField: "Location"))
            }
            guard (200..<300).contains(http.statusCode) else { throw SpeechServerError.http(http.statusCode, data.prefix(4096)) }
            return data
        }
        throw SpeechServerError.http(401, Data())
    }

    /// Token bramy tylko dla adresu z tym samym schematem, hostem i portem co skonfigurowana brama.
    private func gatewayToken() async -> String? {
        guard let gateway = RemoteConfig.configuration, ServerAddress.sameOrigin(endpoint.address, gateway.base),
              await RemoteAccount.shared.isSignedIn else { return nil }
        return try? await RemoteAccount.shared.accessToken()
    }
}

/// Przekierowanie wraca do nas jako odpowiedź 3xx (`SpeechServerError.redirect`), zamiast wysyłać nagranie dalej.
private final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

package enum SpeechServerError: LocalizedError {
    case notConfigured(String)
    case http(Int, Data)
    case redirect(Int, location: String?)
    case connection(URLError.Code, host: String, timeout: TimeInterval)
    case badResponse

    package var errorDescription: String? {
        switch self {
        case .notConfigured(let problem): problem
        case .http(let code, let body): SpeechServer.describe(status: code, body: body)
        case .redirect(let code, let location):
            location.map { tr("Serwer mowy przekierowuje (HTTP {code}) na {address} — wpisz w ustawieniach adres docelowy.",
                              ["code": code, "address": $0]) }
                ?? tr("Serwer mowy przekierowuje (HTTP {code}) — wpisz w ustawieniach adres docelowy.", ["code": code])
        case .connection(let code, let host, let timeout):
            code == .timedOut
                ? tr("Serwer mowy ({host}) nie odpowiedział w {seconds} s.", ["host": host, "seconds": Int(timeout)])
                : tr("Nie można połączyć się z serwerem mowy ({host}) — sprawdź adres i sieć.", ["host": host])
        case .badResponse: tr("Serwer mowy odpowiedział nie tak, jak oczekiwano — sprawdź protokół.")
        }
    }
}
