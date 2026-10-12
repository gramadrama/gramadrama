import Foundation
import NaturalLanguage
import os
import GramaCore

package enum BackendError: LocalizedError {
    case http(Int, String)
    /// Brak adresu usługi (komunikat mówi, gdzie go ustawić).
    case notConfigured(String)
    case stream(String)
    /// Odpowiedź, której nie da się odczytać (np. linia strumienia, która nie jest JSON-em).
    case badReply
    case incomplete
    case truncated
    /// Poza domem bez zalogowania (dostęp zdalny, RemoteAccess.swift).
    case signInRequired
    case remote(String)
    /// Odpowiedź z literami obcego pisma także po ponownej próbie (`PostProcess.foreignLetters`).
    case scriptDrift(String)

    package var errorDescription: String? {
        switch self {
        case .http(let code, let body):
            // Ollama zwraca {"error": "..."}, API zgodne z OpenAI {"error": {"message": "..."}} — pokazujemy samą treść.
            if let message = Self.message(in: body) {
                "\(message) (HTTP \(code))"
            } else {
                "HTTP \(code): \(body.prefix(200))"
            }
        case .notConfigured(let message): message
        case .stream(let message): tr("Model przerwał odpowiedź: {message}", ["message": message])
        case .badReply: tr("Model przerwał odpowiedź: {message}", ["message": tr("nieczytelna odpowiedź serwera")])
        case .incomplete: tr("Połączenie z modelem zerwało się przed końcem odpowiedzi.")
        case .truncated: tr("Tekst jest za długi dla modelu — zaznacz krótszy fragment.")
        case .signInRequired: tr("Zaloguj się do wybranego serwera: okno aplikacji → Usługi → Dostęp zdalny.")
        case .remote(let message): tr("Logowanie: {message}", ["message": message])
        case .scriptDrift(let letters):
            tr("Model wstawił znaki z obcego pisma ({letters}). Wynik odrzucono — spróbuj ponownie.", ["letters": letters])
        }
    }

    /// Komunikat błędu z odpowiedzi serwera (Ollama albo API zgodne z OpenAI) albo nil.
    package static func message(in body: String) -> String? {
        guard let json = try? JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any] else { return nil }
        return json["error"] as? String ?? (json["error"] as? [String: Any])?["message"] as? String
    }
}

/// Wynik trybu „tylko model” dla jednego fragmentu. Odrzucona odpowiedź też trafia do pamięci: przy temperaturze 0
/// model odpowiada tak samo, więc ponowne pytanie o niezmieniony fragment tylko obciążałoby serwer.
package enum LiveFragmentResult: Equatable, Sendable {
    case corrected(String)
    /// Powód bez treści tekstu (`ModelLive.verdict`): empty, script, lines, protected, rewritten, truncated.
    case rejected(String)
}

package final class Backend {
    private let settings = Settings.shared
    private let modelOverride: String?
    /// Serwer podany wprost (atrapa w testach, serwer benchmarku): model i LanguageTool pod tym adresem, bez bramy.
    private let serverOverride: ModelServerEndpoint?
    private let usesCache: Bool
    package private(set) var tokensPerSecond: Double = 0
    /// Surowa odpowiedź modelu przed post-processingiem (benchmark pokazuje ją, gdy się różni).
    package private(set) var lastRawOutput = ""
    /// Skąd był ostatni wynik `correct`: z modelu, z pamięci albo z dołączenia do zapytania w toku.
    package private(set) var lastSource = "model"

    /// Wspólna pamięć odpowiedzi dla całej aplikacji (etap 4A): panel, sprawdzanie w tle, zmiany trybu.
    @MainActor static let corrections = RequestCache<String>(name: "bielik")
    @MainActor static let languageToolResults = RequestCache<LTResult>(name: "languagetool")
    /// Fragmenty trybu „tylko model” — wspólne dla sprawdzania na żywo i ⌥⌘G. Fragment to zdanie (do 320 znaków),
    /// więc mieści się ich więcej niż całych tekstów w `corrections`.
    @MainActor static let liveFragments = RequestCache<LiveFragmentResult>(name: "model_live", capacity: 512)

    /// `model` i `server` nadpisują ustawienia bez zapisywania ich (benchmark, testy z atrapą serwera).
    /// `cache: false` — zawsze pytamy serwer (benchmark mierzy model, nie pamięć).
    package init(model: String? = nil, server: ModelServerEndpoint? = nil, cache: Bool = true) {
        modelOverride = model
        serverOverride = server
        usesCache = cache
    }

    /// Serwer w kluczach pamięci wyników — miejsce, adres, protokół i konfiguracja logowania — oraz to, co zmienia
    /// odpowiedź tego samego modelu: myślenie i własne pliki promptów.
    private var modelServerKey: String {
        (serverOverride.map { "\($0.api.rawValue) \($0.base.absoluteString)" } ?? settings.modelServerIdentity)
            + "|think=\(settings.modelThinking ? 1 : 0)|prompts=\(PromptFiles.current().identity)"
            + "|languages=\(settings.textLanguages.codes.joined(separator: ","))"
    }

    private var languageToolServerKey: String {
        serverOverride.map { ($0.ollamaAtOrigin?.base ?? $0.base).absoluteString } ?? settings.languageToolIdentity
    }

    var model: String { modelOverride ?? settings.model }

    /// Stały rozmiar kontekstu — ten sam w warm-upie i w zapytaniach, inaczej Ollama przeładowuje model.
    /// 8192 mieści prompt (~950 tokenów) + 6000 znaków tekstu na wejściu i wyjściu.
    static let contextSize = 8192
    private static let promptTokens = 1000

    private let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 90
        // Ten Mac i sieć lokalna — bez proxy; serwer w internecie i brama korzystają z ustawień systemowych.
        config.connectionProxyDictionary = [:]
        return URLSession(configuration: config)
    }()
    /// Serwer w internecie i brama: systemowe proxy zostaje, bo w sieci firmowej może być jedyną drogą.
    private let remoteSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 90
        return URLSession(configuration: config)
    }()

    // MARK: Droga do serwera

    /// Serwer modelu na bieżącej drodze (RemoteAccess.swift); atrapa w testach — wprost.
    private func modelRoute() async throws -> Routed<ModelServerEndpoint> {
        if let serverOverride { return Routed(target: serverOverride, bearer: nil, viaGateway: false) }
        return try await Route.shared.model()
    }

    private func languageToolRoute() async throws -> Routed<LanguageToolEndpoint> {
        // Atrapa: LanguageTool pod tym samym hostem i portem (bez ścieżki API, np. `/v1`).
        if let serverOverride {
            let origin = serverOverride.ollamaAtOrigin?.base ?? serverOverride.base
            return Routed(target: LanguageToolEndpoint(base: origin), bearer: nil, viaGateway: false)
        }
        return try await Route.shared.languageTool()
    }

    /// Zapytanie na wybranej drodze. Błąd połączenia: następne zapytanie sprawdzi drogę od nowa (np. wyjście
    /// z domu). Token odrzucony przez bramę (401): raz odświeżamy i ponawiamy.
    private func send<Target, Result>(_ route: () async throws -> Routed<Target>, host: (Target) -> String,
                                      _ build: (Target) throws -> URLRequest,
                                      _ perform: (URLSession, URLRequest) async throws -> (Result, URLResponse))
        async throws -> (Result, URLResponse, Routed<Target>) {
        for attempt in 0..<2 {
            let routed = try await route()
            var request = try build(routed.target)
            if let bearer = routed.bearer { request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
            let session = ServerAddress.isLocalNetwork(host: host(routed.target)) && !routed.viaGateway ? session : remoteSession
            do {
                let (result, response) = try await perform(session, request)
                if routed.viaGateway, (response as? HTTPURLResponse)?.statusCode == 401, attempt == 0 {
                    await RemoteAccount.shared.tokenRejected()
                    continue
                }
                return (result, response, routed)
            } catch let error as URLError {
                // Zerwane połączenie (nie przerwane przez nas) z serwerem: następne zapytanie sprawdzi go od nowa;
                // przez bramę — wszystkie drogi (np. powrót do domu). Usługa na tym Macu nie ma innej drogi.
                let failed = host(routed.target)
                if error.code != .cancelled, !ServerAddress.isLoopback(host: failed) {
                    if routed.viaGateway { await Route.shared.forget() } else { await Route.shared.forget(host: failed) }
                }
                throw error
            }
        }
        throw BackendError.signInRequired
    }

    private func modelData(_ build: (ModelServerEndpoint) throws -> URLRequest) async throws
        -> (Data, URLResponse, Routed<ModelServerEndpoint>) {
        try await send({ try await self.modelRoute() }, host: \.host, build) { try await $0.data(for: $1) }
    }

    private func modelBytes(_ build: (ModelServerEndpoint) throws -> URLRequest) async throws
        -> (URLSession.AsyncBytes, URLResponse, Routed<ModelServerEndpoint>) {
        try await send({ try await self.modelRoute() }, host: \.host, build) { try await $0.bytes(for: $1) }
    }

    private func languageToolData(_ build: (LanguageToolEndpoint) throws -> URLRequest) async throws
        -> (Data, URLResponse, Routed<LanguageToolEndpoint>) {
        try await send({ try await self.languageToolRoute() }, host: \.host, build) { try await $0.data(for: $1) }
    }

    /// Jak długo Ollama trzyma model w pamięci po ostatnim zapytaniu. Serwer — cały dzień (bez zimnego startu);
    /// ten Mac — pół godziny, żeby model nie zajmował pamięci, gdy nie piszesz.
    private func keepAlive(_ endpoint: ModelServerEndpoint) -> String {
        ServerAddress.isLoopback(host: endpoint.host) ? "30m" : "24h"
    }

    /// Poprawki żądania API zgodnego z OpenAI (po błędzie 400) zapamiętane dla serwera i modelu — następne
    /// zapytania od razu właściwe, bez ponownego błędu.
    private static let openAIOptions = OSAllocatedUnfairLock(initialState: [String: OpenAIChat.Options]())
    /// Serwery i modele (`adaptationKey`), które odrzuciły `think` — dostają zapytania bez niego.
    private static let thinkRefused = OSAllocatedUnfairLock(initialState: Set<String>())

    /// Serwer i model w pamięci poprawek żądania.
    private func adaptationKey(_ endpoint: ModelServerEndpoint) -> String {
        "\(endpoint.base.absoluteString)\u{1F}\(model)"
    }

    // MARK: LanguageTool

    package func languageTool(_ text: String) async throws -> LTResult {
        let languages = settings.textLanguages
        let language = TextLanguage.detect(text, among: languages)
        var fields = ["language": language.languageToolCode]
        // preferredVariants jest dozwolone tylko przy language=auto
        if language == .mixed { fields["preferredVariants"] = languages.all.map(\.languageToolCode).joined(separator: ",") }
        return try await languageTool(text, fields: fields, timeout: 8)
    }

    /// Sprawdzanie na żywo: język znany z góry (zdania drugiego języka są zamaskowane spacjami),
    /// krótki limit czasu — pisanie nie może czekać na serwer.
    package func languageTool(_ text: String, language: String, disabledRules: [String]) async throws -> LTResult {
        var fields = ["language": language]
        if !disabledRules.isEmpty { fields["disabledRules"] = disabledRules.joined(separator: ",") }
        return try await languageTool(text, fields: fields, timeout: 3)
    }

    private func languageTool(_ text: String, fields given: [String: String], timeout: TimeInterval) async throws -> LTResult {
        // Język ojczysty to główny język tekstu (jak na Linuksie) — LanguageTool sprawdza wtedy „fałszywych przyjaciół”
        // tego języka. Jest w kluczu pamięci: inny główny język to inne wyniki.
        let extra = ["motherTongue": settings.textLanguages.primary.languageToolCode].merging(given) { $1 }
        guard usesCache else { return try await languageToolRequest(text, fields: extra, timeout: timeout) }
        // Dokładny tekst w kluczu (bez przycinania) — przesunięcia błędów liczą się od jego początku.
        let key = ([languageToolServerKey] + extra.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" } + [text])
            .joined(separator: "\u{1F}")
        return try await Self.languageToolResults.value(for: key, chars: text.count) { _ in
            try await self.languageToolRequest(text, fields: extra, timeout: timeout)
        }.0
    }

    private func languageToolRequest(_ text: String, fields extra: [String: String], timeout: TimeInterval) async throws -> LTResult {
        let fields = ["text": text].merging(extra) { $1 }
        let (data, response, _) = try await languageToolData { endpoint in
            var request = URLRequest(url: endpoint.checkURL)
            request.httpMethod = "POST"
            request.timeoutInterval = timeout
            request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            request.httpBody = formEncode(fields)
            return request
        }
        try check(response, data)
        let decoded = try JSONDecoder().decode(LTResponse.self, from: data)
        return LTResult(matches: decoded.matches,
                        language: decoded.language.detectedLanguage?.code ?? decoded.language.code)
    }

    // MARK: Model językowy

    /// Strumieniuje poprawiony (albo przetłumaczony) tekst; `onPartial` dostaje cały dotychczasowy tekst.
    /// `target` dotyczy tylko trybu .translate (domyślnie kierunek automatyczny).
    /// `background`: sprawdzanie w tle — nie przerywa osieroconych zapytań (robi to dopiero panel).
    /// Ten sam tekst (bez białych znaków na brzegach), tryb, kierunek, wskazówki i model → wynik z pamięci
    /// albo dołączenie do zapytania w toku.
    package func correct(_ text: String, mode: Mode, target: TranslationTarget? = nil, background: Bool = false,
                 onPartial: @escaping @MainActor (String) -> Void) async throws -> String {
        guard usesCache else {
            lastSource = "model"
            return try await correctUncached(text, mode: mode, target: target, onPartial: onPartial)
        }
        let key = [model, modelServerKey, mode.rawValue, target?.rawValue ?? "auto", text.trimmed]
            .joined(separator: "\u{1F}")
        let (core, source) = try await Self.corrections.value(for: key, chars: text.count, background: background,
                                                              onPartial: onPartial) { forward in
            let result = try await self.correctUncached(text, mode: mode, target: target, onPartial: forward)
            return Self.core(result, of: text)
        }
        lastSource = source.rawValue
        return Self.wrap(core, like: text)
    }

    /// Wynik bez białych znaków z brzegów oryginału (PostProcess.clean je dokleja) — w pamięci, żeby ten sam
    /// tekst z inną spacją na końcu też trafiał.
    private static func core(_ result: String, of original: String) -> String {
        let leading = original.prefix { $0.isWhitespace }.count
        let trailing = original.reversed().prefix { $0.isWhitespace }.count
        guard result.count >= leading + trailing else { return result.trimmed }
        return String(result.dropFirst(leading).dropLast(trailing))
    }

    private static func wrap(_ core: String, like original: String) -> String {
        let leading = original.prefix { $0.isWhitespace }
        let trailing = String(original.reversed().prefix { $0.isWhitespace }.reversed())
        return leading + core + trailing
    }

    private func correctUncached(_ text: String, mode: Mode, target: TranslationTarget?,
                                 onPartial: @escaping @MainActor (String) -> Void) async throws -> String {
        let messages = Prompts.messages(for: text, mode: mode, target: target, languages: settings.textLanguages)
        var output = try await chat(messages, temperature: mode.temperature, inputText: text, onPartial: onPartial)
        // Litery obcego pisma (Gemma 4: „piszę” → „pいつze”) przy temperaturze 0 wracają co do znaku, a przy
        // odrobinie losowości nie — jedna ponowna próba; druga taka odpowiedź to błąd, nie poprawka.
        if !PostProcess.foreignLetters(output, original: text).isEmpty {
            EventLog.write("model_script_retry", ["mode": mode.rawValue, "chars": text.count])
            output = try await chat(messages, temperature: max(mode.temperature, 0.3), inputText: text, onPartial: onPartial)
            let letters = PostProcess.foreignLetters(output, original: text)
            if !letters.isEmpty { throw BackendError.scriptDrift(String(letters.prefix(6))) }
        }
        lastRawOutput = output
        // Pusta odpowiedź albo to, czego naprawy nie przywróciły (zmieniony link, zgubiona wzmianka, nowe emoji
        // w Popraw) — błąd zamiast wyniku do wstawienia; jak na Linuksie, bez ponownej próby.
        do {
            return try PostProcess.checked(output, original: text, mode: mode)
        } catch let refusal as ResultRefusal {
            EventLog.write("model_rejected", ["mode": mode.rawValue, "reason": refusal.reason, "chars": text.count])
            throw refusal
        }
    }

    // MARK: Tryb „tylko model” (ModelLive)

    /// Jeden fragment (zdanie) przez krótki prompt minimalnej korekty. Ten sam fragment, model i serwer → wynik
    /// z pamięci albo dołączenie do zapytania w toku (sprawdzanie na żywo i ⌥⌘G pytają o te same fragmenty).
    /// `background`: sprawdzanie na żywo — nie przerywa osieroconych zapytań (robi to dopiero panel).
    /// `onWait`: wyniku nie ma w pamięci — zaraz pytamy serwer (albo dołączamy do zapytania w toku).
    package func liveFragment(_ fragment: String, background: Bool = true,
                              onPartial: @escaping @MainActor (String) -> Void = { _ in },
                              onWait: @escaping @MainActor () -> Void = {}) async throws -> (LiveFragmentResult, String) {
        guard usesCache else {
            await onWait()
            return (try await liveFragmentUncached(fragment, onPartial: onPartial), "model")
        }
        let key = [ModelLive.promptVersion, model, modelServerKey, fragment].joined(separator: "\u{1F}")
        let (result, source) = try await Self.liveFragments.value(for: key, chars: fragment.count, background: background,
                                                                  onPartial: onPartial, onWait: onWait) { forward in
            try await self.liveFragmentUncached(fragment, onPartial: forward)
        }
        return (result, source.rawValue)
    }

    private func liveFragmentUncached(_ fragment: String, onPartial: @escaping @MainActor (String) -> Void) async throws -> LiveFragmentResult {
        let messages = ModelLive.messages(for: fragment)
        for temperature in [0, 0.3] {
            let output: String
            do {
                output = try await chat(messages, temperature: temperature, inputText: fragment, onPartial: onPartial)
            } catch BackendError.truncated {
                // Dłuższa odpowiedź niż fragment z zapasem — model nie poprawia, tylko pisze coś innego.
                return .rejected("truncated")
            }
            switch ModelLive.verdict(output, fragment: fragment) {
            case .corrected(let corrected): return .corrected(corrected)
            case .rejected(let reason): return .rejected(reason)
            case .retry(let reason):
                if temperature > 0 { return .rejected(reason) }
                EventLog.write("model_script_retry", ["mode": "model_live", "chars": fragment.count])
            }
        }
        return .rejected("script")
    }

    /// Wynik ⌥⌘G (Popraw) w trybie „tylko model”: tekst złożony z poprawionych fragmentów; `rejected` — ile
    /// fragmentów zostało bez zmian, bo odpowiedź modelu odrzucono. `onPartial` dostaje cały tekst: poprawiony
    /// do bieżącego miejsca, dalej jeszcze oryginał.
    package func modelOnlyCorrection(_ text: String, onPartial: @escaping @MainActor (String) -> Void) async throws
        -> (text: String, rejected: Int, fromModel: Int) {
        let ns = text as NSString
        var output = "", end = 0
        var rejected = 0, fromModel = 0
        for fragment in ModelLive.fragments(in: text) {
            try Task.checkCancellation()
            output += ns.substring(with: NSRange(location: end, length: fragment.range.location - end))
            let done = output, rest = ns.substring(from: NSMaxRange(fragment.range))
            let (result, source) = try await liveFragment(fragment.text, background: false) { partial in onPartial(done + partial + rest) }
            if source == "model" { fromModel += 1 }
            switch result {
            case .corrected(let corrected): output += corrected
            case .rejected: output += fragment.text; rejected += 1
            }
            end = NSMaxRange(fragment.range)
            let progress = output + ns.substring(from: end)
            await onPartial(progress)
        }
        output += ns.substring(from: end)
        lastSource = fromModel == 0 ? "cache" : "model"
        return (output, rejected, fromModel)
    }

    /// Porządkuje surową transkrypcję dyktowania (wypełniacze, interpunkcja, autokorekty mówcy).
    package func cleanDictation(_ transcript: String, vocabulary: [String]) async throws -> String {
        let messages = Prompts.dictationMessages(for: transcript, vocabulary: vocabulary, languages: settings.textLanguages)
        let output = try await chat(messages, temperature: 0, inputText: transcript) { _ in }
        lastRawOutput = output
        return PostProcess.clean(output, original: transcript)
    }

    /// Przelicza prompt dyktowania na serwerze, zanim skończysz mówić. Ollama trzyma w cache wspólny początek
    /// promptu (instrukcja + przykłady), więc właściwe porządkowanie liczy już tylko samą transkrypcję.
    /// Po ⌥⌘G albo restarcie cache ma inny prompt i samo przeliczenie trwa ~4 s (pomiar 28.09).
    /// Tylko Ollama: inny serwer (np. usługa w chmurze) policzyłby to jak zwykłe zapytanie.
    package func prewarmDictation(vocabulary: [String]) async {
        let messages = Prompts.dictationMessages(for: "…", vocabulary: vocabulary, languages: settings.textLanguages)
        _ = try? await modelData { endpoint in
            guard endpoint.api == .ollama else { throw Skipped.notNeeded }
            var body: [String: Any] = [
                "model": self.model, "stream": false, "keep_alive": self.keepAlive(endpoint), "messages": messages,
                // Ten sam num_ctx co w zapytaniach — inny przeładowałby model.
                "options": ["num_ctx": Self.contextSize, "num_predict": 1, "temperature": 0],
            ]
            if self.sendsThinkingOff(endpoint) { body["think"] = false }
            var request = URLRequest(url: endpoint.chatURL)
            request.httpMethod = "POST"
            request.timeoutInterval = 20
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            return request
        }
    }

    private enum Skipped: Error { case notNeeded }

    /// Strumień odpowiedzi modelu: Ollama `/api/chat` albo `/chat/completions` API zgodnego z OpenAI. Zwraca cały tekst
    /// odpowiedzi (przed post-processingiem); `inputText` wyznacza limit długości odpowiedzi.
    private func chat(_ messages: [[String: String]], temperature: Double, inputText text: String,
                      onPartial: @escaping @MainActor (String) -> Void) async throws -> String {
        // Polski: ~2,3 bajta UTF-8 na token. Odpowiedź ≈ długość tekstu, z zapasem na przeredagowanie.
        let limit = min(Self.contextSize - Self.promptTokens - text.utf8.count / 2, text.utf8.count + 256)
        var adapted: OpenAIChat.Options?
        while true {
            let (bytes, response, routed) = try await modelBytes { endpoint in
                let options = adapted ?? Self.openAIOptions.withLock { $0[self.adaptationKey(endpoint)] } ?? OpenAIChat.Options()
                let body = endpoint.api == .ollama
                    ? self.ollamaChatBody(messages, temperature: temperature, limit: limit, endpoint: endpoint)
                    : OpenAIChat.body(model: self.model, messages: messages, temperature: temperature, limit: limit, options: options)
                var request = URLRequest(url: endpoint.chatURL)
                request.httpMethod = "POST"
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
                return request
            }
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                var errorBody = ""
                for try await line in bytes.lines {
                    errorBody += line
                    if errorBody.utf8.count > 4096 { break }
                }
                let key = adaptationKey(routed.target)
                // Ollama, która nie przyjmuje `think` dla tego modelu („does not support thinking”): to samo zapytanie
                // bez niego, a ten serwer i model nie dostają go już wcale (jak na Linuksie).
                let message = (BackendError.message(in: errorBody) ?? errorBody).lowercased()
                if routed.target.api == .ollama, message.contains("thinking"), sendsThinkingOff(routed.target) {
                    Self.thinkRefused.withLock { _ = $0.insert(key) }
                    EventLog.write("model_think_refused", ["status": http.statusCode])
                    continue
                }
                // API zgodne z OpenAI mówi błędem 400, którego parametru nie przyjmuje — zmieniamy go i pytamy ponownie.
                var options = adapted ?? Self.openAIOptions.withLock { $0[key] } ?? OpenAIChat.Options()
                if routed.target.api == .openAI, http.statusCode == 400,
                   options.adapt(to: BackendError.message(in: errorBody) ?? errorBody) {
                    adapted = options
                    let remembered = options
                    Self.openAIOptions.withLock { $0[key] = remembered }
                    continue
                }
                throw BackendError.http(http.statusCode, errorBody)
            }
            do {
                return routed.target.api == .ollama ? try await ollamaStream(bytes, onPartial: onPartial)
                                                    : try await openAIStream(bytes, onPartial: onPartial)
            } catch let error as URLError where error.code != .cancelled {
                // Połączenie zerwane w trakcie odpowiedzi: urwana odpowiedź (panel ponawia ją sam), a nie serwer,
                // który nie odpowiada — jak `_lines` na Linuksie.
                throw BackendError.incomplete
            }
        }
    }

    private func ollamaChatBody(_ messages: [[String: String]], temperature: Double, limit: Int,
                                endpoint: ModelServerEndpoint) -> [String: Any] {
        var body: [String: Any] = [
            "model": model, "stream": true, "keep_alive": keepAlive(endpoint), "messages": messages,
            "options": ["temperature": temperature, "num_ctx": Self.contextSize, "num_predict": limit],
        ]
        if sendsThinkingOff(endpoint) { body["think"] = false }
        return body
    }

    /// Strumień Ollamy: linie JSON, koniec to `"done": true`.
    private func ollamaStream(_ bytes: URLSession.AsyncBytes, onPartial: @escaping @MainActor (String) -> Void) async throws -> String {
        struct Chunk: Decodable {
            struct Message: Decodable { let content: String }
            let message: Message?
            let done: Bool?
            let done_reason: String?
            let error: String?
            let eval_count: Int?
            let eval_duration: Double?
        }
        var output = ""
        for try await line in bytes.lines {
            try Task.checkCancellation()
            guard let chunk = try? JSONDecoder().decode(Chunk.self, from: Data(line.utf8)) else { continue }
            // Ollama po HTTP 200 zgłasza awarię (np. brak pamięci na serwerze) jako {"error": …} w strumieniu.
            if let error = chunk.error { throw BackendError.stream(error) }
            output += chunk.message?.content ?? ""
            let partial = output
            await onPartial(partial)
            if chunk.done == true {
                if chunk.done_reason == "length" { throw BackendError.truncated }
                if let count = chunk.eval_count, let duration = chunk.eval_duration, duration > 0 {
                    tokensPerSecond = Double(count) / (duration / 1e9)
                }
                return output
            }
        }
        // Bez „done" nie wiemy, czy tekst jest kompletny — nie proponujemy wklejenia urwanej wersji.
        throw BackendError.incomplete
    }

    /// Strumień API zgodnego z OpenAI (server-sent events): koniec to `finish_reason` albo `data: [DONE]`.
    private func openAIStream(_ bytes: URLSession.AsyncBytes, onPartial: @escaping @MainActor (String) -> Void) async throws -> String {
        var output = ""
        var finished = false
        lines: for try await line in bytes.lines {
            try Task.checkCancellation()
            switch OpenAIChat.parse(line) {
            case .skip:
                continue
            case .done:
                finished = true
                break lines
            case .malformed:
                throw BackendError.badReply
            case .error(let message):
                throw BackendError.stream(message)
            case .chunk(let content, let finishReason, let speed):
                if !content.isEmpty {
                    output += content
                    let partial = output
                    await onPartial(partial)
                }
                if let speed { tokensPerSecond = speed }
                if finishReason == "length" { throw BackendError.truncated }
                if finishReason != nil { finished = true }
            }
        }
        guard finished else { throw BackendError.incomplete }
        return output
    }

    /// „Myślenie modelu” wyłączone (domyślnie): Ollama dostaje `think: false` dla każdego modelu, jak na Linuksie.
    /// Myślenie zużywało limit odpowiedzi i spowalniało korektę (Gemma 4, Qwen3); model, który nie myśli, parametr
    /// pomija, a serwer, który go odrzuci, dostaje dalsze zapytania bez niego (`thinkRefused`).
    private func sendsThinkingOff(_ endpoint: ModelServerEndpoint) -> Bool {
        !settings.modelThinking && !Self.thinkRefused.withLock { $0.contains(adaptationKey(endpoint)) }
    }

    /// Ładuje model do pamięci serwera Ollamy, żeby pierwsza poprawka nie czekała na wczytanie. Na tym Macu nie
    /// (poza benchmarkiem): model zająłby pamięć, zanim go użyjesz. Innym serwerom nie wysyłamy nic na zapas.
    package func warmUp() async {
        _ = try? await modelData { endpoint in
            guard endpoint.api == .ollama, self.serverOverride != nil || !ServerAddress.isLoopback(host: endpoint.host) else {
                throw Skipped.notNeeded
            }
            var request = URLRequest(url: endpoint.base.appendingPathComponent("api/generate"))
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "model": self.model, "keep_alive": self.keepAlive(endpoint), "options": ["num_ctx": Self.contextSize],
            ] as [String: Any])
            return request
        }
    }

    // MARK: Stan usług

    package struct Diagnostics {
        /// Stan serwera modelu do pokazania w ustawieniach.
        package var model: ServiceStatus = .error(tr("Nie sprawdzono."))
        /// Serwer modelu odpowiedział (także z ostrzeżeniem, np. o protokole).
        package var modelReachable = false
        /// Modele z serwera (Ollama `/api/tags`, OpenAI `/models`); nil — serwer nie podał listy.
        package var models: [String]?
        /// Modele w pamięci (tylko Ollama, `/api/ps`).
        package var loadedModels: [String] = []
        package var languageTool: ServiceStatus = .error(tr("Nie sprawdzono."))
        package var languageToolReachable = false
    }

    /// Stan usług do ustawień: czasy odpowiedzi, wersja Ollamy, modele i podpowiedź, co poprawić.
    package func diagnostics() async -> Diagnostics {
        var result = Diagnostics()
        async let languageTool = languageToolStatus()
        await checkModel(into: &result)
        (result.languageTool, result.languageToolReachable) = await languageTool
        return result
    }

    /// Do paska menu: czy serwer modelu i LanguageTool odpowiadają.
    package func status() async -> (model: Bool, languageTool: Bool) {
        async let model = try? modelData { endpoint in
            var request = URLRequest(url: endpoint.statusURL)
            request.timeoutInterval = 3
            return request
        }
        async let languageTool = try? languageToolData { endpoint in
            var request = URLRequest(url: endpoint.languagesURL)
            request.timeoutInterval = 3
            return request
        }
        let (modelResponse, languageToolResponse) = await (model?.1, languageTool?.1)
        return ((modelResponse as? HTTPURLResponse)?.statusCode == 200, (languageToolResponse as? HTTPURLResponse)?.statusCode == 200)
    }

    private func checkModel(into result: inout Diagnostics) async {
        let started = Date()
        let data: Data, response: URLResponse, routed: Routed<ModelServerEndpoint>
        do {
            (data, response, routed) = try await modelData { endpoint in
                var request = URLRequest(url: endpoint.statusURL)
                request.timeoutInterval = 4
                return request
            }
        } catch {
            result.model = .error(await modelProblem(error))
            return
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        let endpoint = routed.target
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            result.model = await statusProblem(code, body: data, routed: routed)
            return
        }
        let via = routed.viaGateway ? " · " + tr("przez bramę") : ""
        switch endpoint.api {
        case .ollama:
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard let version = json?["version"] as? String else {
                result.model = .error(tr("Odpowiada, ale to nie wygląda na Ollamę — sprawdź adres i protokół."))
                return
            }
            result.modelReachable = true
            // Na tym Macu wiersz nazywa się „Ollama” — sama wersja.
            let local = serverOverride == nil && settings.modelLocation == .local
            result.model = .ok(tr("Działa · {ms} ms", ["ms": ms]) + " · \(local ? tr("wersja") : "Ollama") \(version)\(via)")
            result.models = await modelNames { $0.modelsURL }
            result.loadedModels = await modelNames { $0.base.appendingPathComponent("api/ps") } ?? []
        case .openAI:
            guard let names = ModelServerEndpoint.modelNames(data) else {
                result.model = .error(tr("Odpowiada, ale nie jak API zgodne z OpenAI (brak listy modeli) — sprawdź adres."))
                return
            }
            result.modelReachable = true
            result.models = names
            // Ollama odpowiada też jak OpenAI, ale bez num_ctx — model wczytuje się wtedy od nowa (kilkanaście sekund).
            if !routed.viaGateway, let ollama = endpoint.ollamaAtOrigin,
               await answers(ollama.statusURL, bearer: routed.bearer, with: "version") {
                result.model = .warning(tr("Działa, ale to Ollama — wybierz protokół „Ollama”, inaczej model wczytuje się od nowa."))
            } else {
                result.model = .ok(tr("Działa · {ms} ms", ["ms": ms]) + " · " + tr("API zgodne z OpenAI") + via)
            }
        }
        if case .ok(let text) = result.model, !endpoint.isEncrypted, !endpoint.isLocalNetwork {
            result.model = .warning(text + " · " + tr("bez szyfrowania poza siecią lokalną — użyj https://"))
        }
    }

    private func modelNames(_ url: (ModelServerEndpoint) -> URL) async -> [String]? {
        guard let (data, response, _) = try? await modelData({ endpoint in
            var request = URLRequest(url: url(endpoint))
            request.timeoutInterval = 4
            return request
        }), (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return ModelServerEndpoint.modelNames(data)
    }

    /// Serwer odpowiedział, ale nie tak, jak trzeba: klucz, logowanie albo protokół (podpowiadamy właściwy).
    private func statusProblem(_ code: Int, body: Data, routed: Routed<ModelServerEndpoint>) async -> ServiceStatus {
        let endpoint = routed.target
        if [401, 403].contains(code) {
            if routed.viaGateway { return .error(tr("Brama nie przyjęła logowania (HTTP {code}) — zaloguj się ponownie.", ["code": code])) }
            return .error(routed.bearer == nil ? tr("Serwer wymaga klucza API (HTTP {code}) — wpisz go w polu „Klucz API”.", ["code": code])
                                               : tr("Serwer nie przyjął klucza API (HTTP {code}) — sprawdź klucz.", ["code": code]))
        }
        if [404, 405].contains(code), !routed.viaGateway {
            switch endpoint.api {
            case .ollama:
                if let other = endpoint.asOpenAI, await answers(other.statusURL, bearer: routed.bearer, with: "data") {
                    return .warning(tr("To serwer zgodny z OpenAI — wybierz ten protokół."))
                }
            case .openAI:
                if let ollama = endpoint.ollamaAtOrigin, await answers(ollama.statusURL, bearer: routed.bearer, with: "version") {
                    return .warning(tr("To Ollama — wybierz protokół „Ollama”."))
                }
            }
            return .error(endpoint.api == .ollama
                ? tr("Pod tym adresem nie ma API Ollamy (HTTP {code}) — sprawdź adres.", ["code": code])
                : tr("Pod tym adresem nie ma API zgodnego z OpenAI (HTTP {code}) — sprawdź adres.", ["code": code]))
        }
        return .error(BackendError.http(code, String(decoding: body.prefix(4096), as: UTF8.self)).localizedDescription)
    }

    /// Krótkie pytanie wprost (podpowiedź protokołu): odpowiedź 200 z obiektem JSON, który ma pole `key`.
    private func answers(_ url: URL, bearer: String?, with key: String) async -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        if let bearer { request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
        let session = ServerAddress.isLocalNetwork(host: url.host ?? "") ? session : remoteSession
        guard let (data, response) = try? await session.data(for: request), (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return json[key] != nil
    }

    /// Dlaczego serwer modelu nie odpowiedział — z podpowiedzią, co zrobić.
    private func modelProblem(_ error: Error) async -> String {
        guard let error = error as? URLError else { return error.localizedDescription }
        if serverOverride == nil, settings.modelLocation == .local { return tr("Ollama na tym Macu nie odpowiada.") }
        if error.code == .timedOut { return tr("Serwer nie odpowiedział w 4 s — sprawdź adres i sieć.") }
        // Serwer, który może zastąpić brama: protokół Ollama, nie ten Mac.
        let viaGateway = settings.modelServer.map { $0.api == .ollama && !ServerAddress.isLoopback(host: $0.host) } ?? false
        let signedIn = await MainActor.run { RemoteAccount.shared.isSignedIn }
        return tr("Nie można połączyć się z serwerem — sprawdź adres i sieć.")
            + (viaGateway && RemoteConfig.configuration != nil && !signedIn ? " " + tr("Poza domem zaloguj się do dostępu zdalnego.") : "")
    }

    private func languageToolStatus() async -> (ServiceStatus, Bool) {
        let local = serverOverride == nil && settings.languageToolLocation == .local
        let started = Date()
        let data: Data, response: URLResponse, routed: Routed<LanguageToolEndpoint>
        do {
            (data, response, routed) = try await languageToolData { endpoint in
                var request = URLRequest(url: endpoint.languagesURL)
                request.timeoutInterval = 4
                return request
            }
        } catch let error as URLError {
            let what = local ? tr("Nie działa na tym Macu") : error.code == .timedOut ? tr("Nie odpowiedział w 4 s") : tr("Nie odpowiada")
            return (.error(tr("{problem} — korekta działa bez uzasadnień zmian.", ["problem": what])), false)
        } catch {
            return (.error(error.localizedDescription), false)
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            if routed.viaGateway, [401, 403].contains(code) {
                return (.error(tr("Brama nie przyjęła logowania (HTTP {code}) — zaloguj się ponownie.", ["code": code])), false)
            }
            return (.error(tr("{problem} — korekta działa bez uzasadnień zmian.", ["problem": "HTTP \(code)"])), false)
        }
        guard (try? JSONSerialization.jsonObject(with: data)) is [Any] else {
            return (.error(tr("Odpowiada, ale to nie wygląda na LanguageTool — sprawdź adres.")), false)
        }
        var text = tr("Działa · {ms} ms", ["ms": Int(Date().timeIntervalSince(started) * 1000)])
        if local, let port = routed.target.port { text += " · port \(port)" }
        if routed.viaGateway { text += " · " + tr("przez bramę") }
        guard routed.target.isEncrypted || routed.target.isLocalNetwork else {
            return (.warning(text + " · " + tr("bez szyfrowania poza siecią lokalną — użyj https://")), true)
        }
        return (.ok(text), true)
    }

    // MARK: Pomocnicze

    private func check(_ response: URLResponse, _ data: Data) throws {
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw BackendError.http(http.statusCode, String(decoding: data, as: UTF8.self))
        }
    }

    private func formEncode(_ fields: [String: String]) -> Data {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        let body = fields.map { key, value in
            "\(key)=\(value.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")"
        }.joined(separator: "&")
        return Data(body.utf8)
    }
}
