import Foundation

/// Gdzie działa usługa — model językowy, LanguageTool albo rozpoznawanie mowy: na tym Macu albo na serwerze pod
/// wpisanym adresem. Wszystkie usługi w zakładce Usługi mają ten sam wybór; adres serwera zostaje zapisany po
/// powrocie na „Ten Mac”.
package enum ServiceLocation: String, CaseIterable, Identifiable, Sendable {
    case local, server

    package var id: String { rawValue }
    package var label: String { self == .local ? tr("Ten Mac") : tr("Serwer") }
}

/// Wynik sprawdzenia usługi do pokazania w ustawieniach.
package enum ServiceStatus: Equatable, Sendable {
    case ok(String)
    case warning(String)
    case error(String)

    package var text: String {
        switch self {
        case .ok(let text), .warning(let text), .error(let text): text
        }
    }

    /// Ten sam stan z dopiskiem (np. „ · przez bramę”).
    package func appending(_ suffix: String) -> ServiceStatus {
        switch self {
        case .ok(let text): .ok(text + suffix)
        case .warning(let text): .warning(text + suffix)
        case .error(let text): .error(text + suffix)
        }
    }
}

/// Adres serwera wpisany w ustawieniach: sprawdzenie i rodzaj sieci — wspólne dla modelu, LanguageTool i serwera mowy.
package enum ServerAddress {
    /// Powód, dla którego tekst nie jest adresem serwera (do pokazania w ustawieniach), albo nil.
    /// `empty`: komunikat dla pustego pola; `example`: adres w podpowiedzi.
    package static func problem(_ text: String, empty: String, example: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return empty }
        guard let parts = URLComponents(string: trimmed), let scheme = parts.scheme?.lowercased(),
              ["http", "https"].contains(scheme), let host = parts.host, !host.isEmpty else {
            return tr("Podaj pełny adres z http:// albo https://, np. {example}.", ["example": example])
        }
        if parts.user != nil || parts.password != nil { return tr("Adres nie może zawierać nazwy użytkownika ani hasła.") }
        if parts.query != nil || parts.fragment != nil { return tr("Adres nie może zawierać „?” ani „#”.") }
        if let port = parts.port, !(1...65535).contains(port) { return tr("Nieprawidłowy port.") }
        return nil
    }

    /// Adres usługi na tym Macu („Ten Mac”): pusty — domyślny; inny musi wskazywać ten Mac (`isLoopback`), jak
    /// `local_url_problem` na Linuksie. Powód albo nil.
    package static func localProblem(_ text: String, example: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if let problem = problem(trimmed, empty: "", example: example) { return problem }
        guard let host = URLComponents(string: trimmed)?.host, isLoopback(host: host) else {
            return tr("Podaj adres na tym Macu, np. {example}.", ["example": example])
        }
        return nil
    }

    /// Adres bez końcowych „/”, ze schematem małymi literami; nil — to nie adres serwera.
    package static func url(_ text: String) -> URL? {
        guard problem(text, empty: "", example: "") == nil,
              var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        parts.scheme = parts.scheme?.lowercased()
        while parts.path.hasSuffix("/") { parts.path.removeLast() }
        return parts.url
    }

    /// Komputer w tej samej sieci albo ten sam Mac: loopback, adresy prywatne i lokalne łącza (IPv4 i IPv6),
    /// zakres 100.64/10 (np. sieci VPN typu mesh), nazwy bez kropki i lokalne końcówki nazw — te same co `lan_host`
    /// na Linuksie. Taki serwer łączy się bez systemowego proxy (proxy nie widzi sieci lokalnej).
    package static func isLocalNetwork(host: String) -> Bool {
        let host = bare(host)
        guard !host.isEmpty else { return false }
        if isLoopback(host: host) { return true }
        for suffix in [".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".intranet"]
            where host.hasSuffix(suffix) { return true }
        if let octets = ipv4(host) {
            let a = octets[0], b = octets[1]
            return a == 10 || (a == 172 && (16...31).contains(b)) || (a == 192 && b == 168)
                || (a == 169 && b == 254) || (a == 100 && (64...127).contains(b))
        }
        if host.contains(":") {
            return ["fe8", "fe9", "fea", "feb", "fc", "fd"].contains { host.hasPrefix($0) }
        }
        // Nazwa bez kropki (np. nazwa komputera w sieci domowej).
        return !host.contains(".")
    }

    /// Ten sam Mac: localhost, 127.0.0.0/8 i ::1.
    package static func isLoopback(host: String) -> Bool {
        let host = bare(host)
        return host == "localhost" || host.hasSuffix(".localhost") || host == "::1" || ipv4(host)?.first == 127
    }

    /// Ten sam schemat, host i port — token logowania idzie tylko na adres bramy.
    package static func sameOrigin(_ a: URL, _ b: URL) -> Bool {
        func port(_ url: URL) -> Int? { url.port ?? (url.scheme == "https" ? 443 : url.scheme == "http" ? 80 : nil) }
        return a.scheme?.lowercased() == b.scheme?.lowercased() && a.host?.lowercased() == b.host?.lowercased() && port(a) == port(b)
    }

    private static func bare(_ host: String) -> String {
        host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]."))
    }

    private static func ipv4(_ host: String) -> [Int]? {
        let octets = host.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        guard octets.count == 4, octets.allSatisfy({ $0.map { (0...255).contains($0) } ?? false }) else { return nil }
        return octets.map { $0! }
    }
}

// MARK: - Model językowy

/// Protokół serwera modelu — jak `llm_api` na Linuksie: natywne API Ollamy albo API zgodne z OpenAI.
package enum ModelServerAPI: String, CaseIterable, Identifiable, Sendable {
    case ollama
    case openAI = "openai"

    package var id: String { rawValue }
    package var label: String { self == .ollama ? "Ollama" : tr("Zgodny z OpenAI") }
}

/// Serwer modelu po sprawdzeniu adresu. Ollama: adres tak, jak go wpisano (`…/api/chat`). API zgodne z OpenAI:
/// z dopisanym `/v1`, gdy adres nie ma ścieżki (`http://host:8080` → `…/v1/chat/completions`), jak na Linuksie.
package struct ModelServerEndpoint: Equatable, Sendable {
    package let api: ModelServerAPI
    package let base: URL

    /// Baza podana wprost (Ollama na tym Macu, brama, atrapa w testach).
    package init(base: URL, api: ModelServerAPI) {
        self.base = base
        self.api = api
    }

    package init?(address text: String, api: ModelServerAPI) {
        guard let address = ServerAddress.url(text) else { return nil }
        self.api = api
        base = api == .openAI && address.path.isEmpty ? address.appendingPathComponent("v1") : address
    }

    package static func problem(_ text: String) -> String? {
        ServerAddress.problem(text, empty: tr("Podaj adres serwera modelu."), example: "http://192.0.2.10:11434")
    }

    /// Ollama na tym Macu — domyślny adres, pod którym nasłuchuje aplikacja Ollamy.
    package static let local = ModelServerEndpoint(base: URL(string: "http://127.0.0.1:11434")!, api: .ollama)

    /// Ollama na tym Macu pod wpisanym adresem; pusty albo błędny (także spoza tego Maca) — domyślny.
    package static func local(address: String) -> ModelServerEndpoint {
        guard ServerAddress.localProblem(address, example: "") == nil,
              let endpoint = ModelServerEndpoint(address: address, api: .ollama) else { return local }
        return endpoint
    }

    /// Ścieżka Ollamy na bramie dostępu zdalnego — obok `languagetool` i `whisper`.
    package static let gatewayPath = "ollama"

    package static func gateway(_ base: URL) -> ModelServerEndpoint {
        ModelServerEndpoint(base: base.appendingPathComponent(gatewayPath), api: .ollama)
    }

    /// Odpowiedź strumieniem: Ollama `POST /api/chat`, OpenAI `POST /chat/completions`.
    package var chatURL: URL { base.appendingPathComponent(api == .ollama ? "api/chat" : "chat/completions") }
    /// Stan: Ollama `GET /api/version`, OpenAI `GET /models` (ta sama odpowiedź to lista modeli).
    package var statusURL: URL { base.appendingPathComponent(api == .ollama ? "api/version" : "models") }
    /// Lista modeli: Ollama `GET /api/tags`, OpenAI `GET /models`.
    package var modelsURL: URL { base.appendingPathComponent(api == .ollama ? "api/tags" : "models") }

    package var host: String { base.host ?? "" }
    package var isEncrypted: Bool { base.scheme == "https" }
    package var isLocalNetwork: Bool { ServerAddress.isLocalNetwork(host: host) }

    /// Ten sam serwer w API zgodnym z OpenAI (Ollama wpisana jako adres bez ścieżki → `…/v1`).
    package var asOpenAI: ModelServerEndpoint? {
        api == .openAI ? self : ModelServerEndpoint(address: base.absoluteString, api: .openAI)
    }

    /// Ollama pod tym samym hostem i portem — Ollama odpowiada też jak OpenAI (`/v1`), ale bez `num_ctx`.
    package var ollamaAtOrigin: ModelServerEndpoint? {
        guard var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        parts.path = ""
        return parts.url.map { ModelServerEndpoint(base: $0, api: .ollama) }
    }

    /// Nazwy modeli z odpowiedzi: Ollama `{"models": [{"name"}]}`, OpenAI `{"data": [{"id"}]}`; nil — to nie lista.
    package static func modelNames(_ data: Data) -> [String]? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let models = json["models"] as? [[String: Any]] { return models.compactMap { $0["name"] as? String }.sorted() }
        if let models = json["data"] as? [[String: Any]] { return models.compactMap { $0["id"] as? String }.sorted() }
        return nil
    }
}

/// API zgodne z OpenAI: treść żądania `/chat/completions`, linie strumienia (server-sent events) i poprawka żądania
/// po HTTP 400 — to samo co `_chat_openai`, `_stream_openai` i `_adapted` na Linuksie.
package enum OpenAIChat {
    /// Serwery różnią się dwoma parametrami i mówią o tym błędem 400: nowsze modele OpenAI chcą
    /// `max_completion_tokens`, a niektóre nie przyjmują `temperature`. Każdy zmieniamy najwyżej raz.
    package struct Options: Equatable, Sendable {
        package var limitField = "max_tokens"
        package var sendsTemperature = true

        package init() {}

        /// Zmienia opcje tak, jak mówi komunikat serwera; false — komunikat dotyczy czegoś innego.
        package mutating func adapt(to message: String) -> Bool {
            let lower = message.lowercased()
            if lower.contains("max_completion_tokens"), limitField == "max_tokens" {
                limitField = "max_completion_tokens"
                return true
            }
            let refused = ["unsupported", "not support", "only the default", "not allowed"].contains { lower.contains($0) }
            if lower.contains("temperature"), refused, sendsTemperature {
                sendsTemperature = false
                return true
            }
            return false
        }
    }

    package static func body(model: String, messages: [[String: String]], temperature: Double, limit: Int,
                             options: Options) -> [String: Any] {
        var body: [String: Any] = ["model": model, "messages": messages, "stream": true, options.limitField: limit]
        if options.sendsTemperature { body["temperature"] = temperature }
        return body
    }

    package enum Line: Equatable {
        /// Komentarz („: keep-alive”), nazwa zdarzenia, pusta linia.
        case skip
        /// `data: [DONE]` — koniec strumienia.
        case done
        /// Fragment odpowiedzi; `finishReason` („stop”, „length”) w ostatnim; `tokensPerSecond` podaje llama.cpp.
        case chunk(content: String, finishReason: String?, tokensPerSecond: Double?)
        case error(String)
        case malformed
    }

    package static func parse(_ line: String) -> Line {
        let line = line.trimmingCharacters(in: .whitespaces)
        guard line.hasPrefix("data:") else { return .skip }
        let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        if payload == "[DONE]" { return .done }
        guard let json = try? JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any] else { return .malformed }
        if let error = json["error"], !(error is NSNull) {
            let message = (error as? [String: Any])?["message"] as? String ?? (error as? String) ?? "\(error)"
            return .error(String(message.prefix(200)))
        }
        var content = ""
        var finishReason: String?
        for choice in json["choices"] as? [[String: Any]] ?? [] {
            if let delta = (choice["delta"] as? [String: Any])?["content"] as? String { content += delta }
            if let reason = choice["finish_reason"] as? String, !reason.isEmpty { finishReason = reason }
        }
        let speed = ((json["timings"] as? [String: Any])?["predicted_per_second"] as? NSNumber)?.doubleValue
        return .chunk(content: content, finishReason: finishReason, tokensPerSecond: speed)
    }
}

/// Postęp pobierania modelu przez Ollamę (`POST /api/pull`, linie JSON): status, a dla warstw digest, total
/// i completed. Procent liczymy z sumy warstw — Ollama pobiera kilka naraz.
package struct OllamaPullProgress: Equatable, Sendable {
    package private(set) var status = ""
    package private(set) var finished = false
    private var layers: [String: Layer] = [:]

    private struct Layer: Equatable, Sendable {
        var total: Int64
        var completed: Int64
    }

    package init() {}

    package var total: Int64 { layers.values.reduce(0) { $0 + $1.total } }
    package var completed: Int64 { layers.values.reduce(0) { $0 + min($1.completed, $1.total) } }
    package var fraction: Double? { total > 0 ? Double(completed) / Double(total) : nil }

    /// Jedna linia odpowiedzi; zwraca komunikat błędu (`{"error": …}`) albo nil.
    package mutating func apply(_ line: String) -> String? {
        guard let json = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { return nil }
        if let error = json["error"] as? String { return error }
        if let status = json["status"] as? String {
            self.status = status
            if status == "success" { finished = true }
        }
        if let digest = json["digest"] as? String, let total = (json["total"] as? NSNumber)?.int64Value, total > 0 {
            let completed = (json["completed"] as? NSNumber)?.int64Value ?? layers[digest]?.completed ?? 0
            layers[digest] = Layer(total: total, completed: completed)
        }
        return nil
    }
}

// MARK: - LanguageTool

/// Serwer LanguageTool: sprawdzanie `POST {adres}/v2/check`, stan `GET {adres}/v2/languages`.
package struct LanguageToolEndpoint: Equatable, Sendable {
    package let base: URL

    package init(base: URL) { self.base = base }

    package init?(address text: String) {
        guard let base = ServerAddress.url(text) else { return nil }
        self.base = base
    }

    package static func problem(_ text: String) -> String? {
        ServerAddress.problem(text, empty: tr("Podaj adres serwera LanguageTool."), example: "http://192.0.2.10:8010")
    }

    /// Na tym Macu: port z instrukcji i obrazu Dockera (8010), potem domyślny port LanguageTool i usługi Homebrew (8081).
    package static let localPorts = [8010, 8081]

    package static func local(port: Int) -> LanguageToolEndpoint {
        LanguageToolEndpoint(base: URL(string: "http://127.0.0.1:\(port)")!)
    }

    /// LanguageTool na tym Macu pod wpisanym adresem; nil — pusty albo błędny, wtedy szukamy go na `localPorts`.
    package static func local(address: String) -> LanguageToolEndpoint? {
        guard !address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              ServerAddress.localProblem(address, example: "") == nil else { return nil }
        return LanguageToolEndpoint(address: address)
    }

    /// Ścieżka LanguageTool na bramie dostępu zdalnego.
    package static let gatewayPath = "languagetool"

    package static func gateway(_ base: URL) -> LanguageToolEndpoint {
        LanguageToolEndpoint(base: base.appendingPathComponent(gatewayPath))
    }

    package var checkURL: URL { base.appendingPathComponent("v2/check") }
    package var languagesURL: URL { base.appendingPathComponent("v2/languages") }
    package var host: String { base.host ?? "" }
    package var port: Int? { base.port }
    package var isEncrypted: Bool { base.scheme == "https" }
    package var isLocalNetwork: Bool { ServerAddress.isLocalNetwork(host: host) }
}

// MARK: - Ustawienia sprzed wyboru „Ten Mac / Serwer”

/// Do wersji 0.5.2 jedno pole „adres serwera” (sam host): Ollama na porcie 11434, LanguageTool na 8010.
/// Nowa wersja zamienia je raz na adresy usług — bez zmiany działania.
package enum LegacyServerHost {
    package struct Services: Equatable, Sendable {
        package var modelLocation: ServiceLocation
        package var modelServerURL: String
        package var languageToolLocation: ServiceLocation
        package var languageToolServerURL: String
    }

    /// Pusty albo błędny adres (nowa instalacja) — obie usługi na tym Macu; przy skonfigurowanej bramie dostępu zdalnego
    /// — serwer bez adresu, czyli sama brama (tak działało to wcześniej). Ten Mac (127.0.0.1, localhost, ::1) — też
    /// „Ten Mac”, a dotychczasowe adresy zostają w formularzu serwera. Inny host — serwer z dotychczasowymi portami.
    package static func migrate(_ serverHost: String, gatewayConfigured: Bool = false) -> Services {
        guard let host = normalize(serverHost) else {
            var location = ServiceLocation.local
            if gatewayConfigured { location = .server }
            return Services(modelLocation: location, modelServerURL: "", languageToolLocation: location, languageToolServerURL: "")
        }
        let name = host.contains(":") ? "[\(host)]" : host
        let location: ServiceLocation = ServerAddress.isLoopback(host: host) ? .local : .server
        return Services(modelLocation: location, modelServerURL: "http://\(name):11434",
                        languageToolLocation: location, languageToolServerURL: "http://\(name):8010")
    }

    /// Sam host z tego, co wpisał użytkownik: „127.0.0.1”, „server.example.com:11434”, „http://server.example.com/”
    /// i „::1” dają poprawny host; błędny adres daje nil.
    package static func normalize(_ serverHost: String) -> String? {
        var input = serverHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty else { return nil }
        let isBareIPv6 = input.filter { $0 == ":" }.count >= 2 && !input.contains("[") && !input.contains("://")
        if !isBareIPv6 {
            if !input.contains("://") { input = "http://" + input }
            guard let host = URLComponents(string: input)?.host else { return nil }
            input = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        }
        let allowed = isBareIPv6 || input.contains(":") ? #"^[0-9A-Fa-f:.]+$"# : #"^[A-Za-z0-9.\-]+$"#
        return input.range(of: allowed, options: .regularExpression) == nil ? nil : input
    }
}
