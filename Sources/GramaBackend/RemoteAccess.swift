import CryptoKit
import Foundation
import GramaCore
import Network
import Security

/// A token POST must never be forwarded to a redirect destination.
private final class TokenRedirectPolicy: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

/// Optional Pocket ID gateway configured by the user. No built-in deployment or account.
package enum RemoteConfig {
    package static var configuration: RemoteServiceConfiguration? {
        RemoteServiceConfiguration(address: Settings.shared.remoteBaseURL, clientID: Settings.shared.remoteClientID)
    }
    package static let redirectURI = "gramadrama://auth"
    static let scopes = "openid profile offline_access"
}

/// Dokąd wysłać zapytanie do usługi: adres, nagłówek autoryzacji (klucz API albo token logowania) i czy przez bramę.
package struct Routed<Target: Sendable>: Sendable {
    package let target: Target
    package let bearer: String?
    package let viaGateway: Bool
}

// MARK: - Wybór drogi

/// Droga do każdej usługi osobno. Usługa na tym Macu — zawsze wprost. Serwer z ustawień — wprost, gdy odpowiada,
/// a gdy nie (np. poza domem) i trwa logowanie do dostępu zdalnego — przez bramę (`/ollama` dla protokołu Ollama,
/// `/languagetool`, `/whisper`), jak wcześniej dla jedynego adresu serwera. Odpowiedź serwera pamiętamy 5 min
/// („jest”) albo 30 s („nie ma”); zmiana sieci i błąd połączenia z serwerem ją zapominają.
@MainActor
package final class Route: ObservableObject {
    package static let shared = Route()

    package enum Service: Hashable, Sendable { case model, languageTool, speech }
    package enum Kind: Equatable, Sendable { case home, remote, offline }
    /// Ostatnia droga każdej usługi, która może iść przez bramę (brak wpisu — usługa jej nie używa albo jeszcze
    /// nie pytała). `home` — wprost, `remote` — przez bramę, `offline` — serwer nie odpowiada, a logowania nie ma.
    @Published package private(set) var kinds: [Service: Kind] = [:]

    private var answers: [URL: (reachable: Bool, checkedAt: Date)] = [:]
    private var probes: [URL: Task<Bool, Never>] = [:]
    /// Zmienia się przy zapomnieniu dróg: odpowiedź sondy sprzed zmiany sieci nie trafia do pamięci.
    private var generation = 0
    private let monitor = NWPathMonitor()

    /// Sondy: krótki limit; serwer w sieci lokalnej bez proxy, w internecie przez systemowe proxy (jak zapytania).
    private static func probeSession(proxy: Bool) -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 1.2
        if !proxy { config.connectionProxyDictionary = [:] }
        return URLSession(configuration: config)
    }
    private let localProbes = Route.probeSession(proxy: false)
    private let internetProbes = Route.probeSession(proxy: true)

    private init() {
        monitor.pathUpdateHandler = { [weak self] _ in
            Task { @MainActor in self?.forget() }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    /// Serwer modelu z ustawień. Ten Mac — Ollama pod adresem tego Maca (domyślnie 127.0.0.1:11434). Przez bramę tylko protokół Ollama (brama prowadzi
    /// do Ollamy). Serwer bez adresu, ale z logowaniem — sama brama (np. dostęp wyłącznie z zewnątrz).
    package func model() async throws -> Routed<ModelServerEndpoint> {
        let settings = Settings.shared
        guard settings.modelLocation == .server else {
            kinds[.model] = nil
            return Routed(target: settings.localModelServer, bearer: nil, viaGateway: false)
        }
        guard let direct = settings.modelServer else {
            if let gateway = try await gatewayOnly(.model) {
                return Routed(target: .gateway(gateway.base), bearer: gateway.token, viaGateway: true)
            }
            throw BackendError.notConfigured(tr("Ustaw adres serwera modelu w zakładce Usługi albo wybierz „Ten Mac”."))
        }
        return try await route(.model, direct, address: direct.base, probe: direct.statusURL, key: ServerKey.model.load(),
                               gateway: direct.api == .ollama ? ModelServerEndpoint.gateway : nil)
    }

    /// Serwer LanguageTool z ustawień; na tym Macu — wpisany adres albo pierwszy port, na którym odpowiada (8010, potem 8081).
    package func languageTool() async throws -> Routed<LanguageToolEndpoint> {
        let settings = Settings.shared
        guard settings.languageToolLocation == .server else {
            kinds[.languageTool] = nil
            return Routed(target: await localLanguageTool(), bearer: nil, viaGateway: false)
        }
        guard let direct = settings.languageToolServer else {
            if let gateway = try await gatewayOnly(.languageTool) {
                return Routed(target: .gateway(gateway.base), bearer: gateway.token, viaGateway: true)
            }
            throw BackendError.notConfigured(tr("Ustaw adres serwera LanguageTool w zakładce Usługi albo wybierz „Ten Mac”."))
        }
        return try await route(.languageTool, direct, address: direct.base, probe: direct.languagesURL, key: "",
                               gateway: LanguageToolEndpoint.gateway)
    }

    /// Serwer mowy z ustawień dyktowania wprost, a gdy nie odpowiada i trwa logowanie do dostępu zdalnego — przez
    /// bramę (`<adres logowania>/whisper`, ten sam protokół). Token zamiast klucza API dokłada `SpeechServerClient`
    /// (adres bramy). Serwer na tym Macu — zawsze wprost.
    package func speechEndpoint(_ direct: SpeechServerEndpoint) async -> (endpoint: SpeechServerEndpoint, viaGateway: Bool) {
        guard !ServerAddress.isLoopback(host: direct.host), let config = RemoteConfig.configuration else {
            kinds[.speech] = nil
            return (direct, false)
        }
        if ServerAddress.sameOrigin(direct.address, config.base) {
            // Adres samej bramy: token zamiast klucza dokłada `SpeechServerClient`, jeśli klucza nie wpisano.
            kinds[.speech] = .remote
            return (direct, false)
        }
        if await isReachable(direct.healthURL) {
            kinds[.speech] = .home
            return (direct, false)
        }
        guard RemoteAccount.shared.isSignedIn, let remote = SpeechServerEndpoint.gateway(config.base, api: direct.api) else {
            kinds[.speech] = .offline
            return (direct, false)
        }
        kinds[.speech] = .remote
        return (remote, true)
    }

    /// Po zmianie sieci, ustawień albo logowania — następne zapytania sprawdzą drogi od nowa. Trwające sondy
    /// kończą się same (przerwana sonda wysłałaby czekające zapytanie przez bramę także w domu).
    package func forget() {
        generation += 1
        answers = [:]
        probes = [:]
    }

    /// Błąd połączenia z tym serwerem — następne zapytanie sprawdzi go od nowa (np. tuż po wyjściu z domu).
    package func forget(host: String) {
        let host = host.lowercased()
        answers = answers.filter { $0.key.host?.lowercased() != host }
    }

    // MARK: Pomocnicze

    /// Wspólna zasada dla serwerów z ustawień. Adres samej bramy wpisany wprost dostaje token logowania zamiast klucza;
    /// `gateway` nil — usługi nie ma na bramie (np. serwer zgodny z OpenAI), więc zawsze wprost.
    private func route<Target: Sendable>(_ service: Service, _ direct: Target, address: URL, probe: URL, key: String,
                                         gateway: ((URL) -> Target)?) async throws -> Routed<Target> {
        let key = key.isEmpty ? nil : key
        guard let config = RemoteConfig.configuration else {
            kinds[service] = nil
            return Routed(target: direct, bearer: key, viaGateway: false)
        }
        if key == nil, ServerAddress.sameOrigin(address, config.base) {
            // Bez logowania nie prosimy o token: `accessToken()` wylogowałby i przerwał trwające logowanie.
            guard RemoteAccount.shared.isSignedIn else { throw BackendError.signInRequired }
            kinds[service] = .remote
            return Routed(target: direct, bearer: try await token(config), viaGateway: true)
        }
        guard let gateway, !ServerAddress.isLoopback(host: address.host ?? "") else {
            kinds[service] = nil
            return Routed(target: direct, bearer: key, viaGateway: false)
        }
        if await isReachable(probe) {
            kinds[service] = .home
            return Routed(target: direct, bearer: key, viaGateway: false)
        }
        guard RemoteAccount.shared.isSignedIn else {
            kinds[service] = .offline
            return Routed(target: direct, bearer: key, viaGateway: false)
        }
        let token = try await token(config)
        kinds[service] = .remote
        return Routed(target: gateway(config.base), bearer: token, viaGateway: true)
    }

    /// Brama z tokenem, gdy usługa nie ma adresu, a logowanie trwa; nil — bez logowania.
    private func gatewayOnly(_ service: Service) async throws -> (base: URL, token: String)? {
        guard let config = RemoteConfig.configuration, RemoteAccount.shared.isSignedIn else { return nil }
        let token = try await token(config)
        kinds[service] = .remote
        return (config.base, token)
    }

    private func token(_ config: RemoteServiceConfiguration) async throws -> String {
        let token = try await RemoteAccount.shared.accessToken()
        guard config == RemoteConfig.configuration else { throw BackendError.signInRequired }
        return token
    }

    private func localLanguageTool() async -> LanguageToolEndpoint {
        if let typed = Settings.shared.localLanguageToolServer { return typed }
        for port in LanguageToolEndpoint.localPorts {
            let candidate = LanguageToolEndpoint.local(port: port)
            // Na tym Macu port bywa zajęty przez coś innego — liczy się tylko odpowiedź 200 LanguageTool.
            if await isReachable(candidate.languagesURL, requireOK: true) { return candidate }
        }
        return .local(port: LanguageToolEndpoint.localPorts[0])
    }

    /// `GRAMADRAMA_FORCE_REMOTE=1`: serwer z ustawień „nie odpowiada” — test drogi przez bramę z domu.
    private static let forceRemote = ProcessInfo.processInfo.environment["GRAMADRAMA_FORCE_REMOTE"] == "1"

    /// Każda odpowiedź HTTP (także 401 albo 404) znaczy, że serwer jest w tej sieci; `requireOK` — tylko 200.
    private func isReachable(_ url: URL, requireOK: Bool = false) async -> Bool {
        if Self.forceRemote, !requireOK { return false }
        if let answer = answers[url], Date().timeIntervalSince(answer.checkedAt) < (answer.reachable ? 300 : 30) {
            return answer.reachable
        }
        if let probe = probes[url] { return await probe.value }
        let session = ServerAddress.isLocalNetwork(host: url.host ?? "") ? localProbes : internetProbes
        let task = Task { () -> Bool in
            guard let (_, response) = try? await session.data(from: url), let http = response as? HTTPURLResponse else { return false }
            return !requireOK || http.statusCode == 200
        }
        let started = generation
        probes[url] = task
        let reachable = await task.value
        if probes[url] == task { probes[url] = nil }
        if generation == started { answers[url] = (reachable, Date()) }
        return reachable
    }
}

// MARK: - Konto (Pocket ID)

@MainActor
package final class RemoteAccount: ObservableObject {
    package static let shared = RemoteAccount()

    package enum State: Equatable {
        case signedOut
        case waitingForBrowser
        case signedIn(user: String)
    }

    @Published package private(set) var state: State = .signedOut
    package var isSignedIn: Bool { if case .signedIn = state { true } else { false } }

    private struct Tokens: Codable {
        var access: String
        var refresh: String?
        var expiresAt: Date
        var user: String
        var configuration: String?
    }

    private var tokens: Tokens?
    private var pending: (verifier: String, state: String, config: RemoteServiceConfiguration, generation: UInt64)?
    private var generation: UInt64 = 0
    private var refreshing: Task<String, Error>?

    private let session: URLSession = {
        // Przez internet: systemowe proxy (np. firmowe) zostaje — inaczej niż przy połączeniu bezpośrednim.
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        return URLSession(configuration: config, delegate: TokenRedirectPolicy(), delegateQueue: nil)
    }()

    /// Token należy do konkretnej bramy: przy innej skonfigurowanej bramie (albo tokenie sprzed zapisywania bramy)
    /// usuwamy go. Bez skonfigurowanej bramy nie zaglądamy do pęku kluczy wcale — przypadkowo wyczyszczone pole
    /// ani program uruchomiony spoza aplikacji (CLI, podgląd okna) nie kasuje logowania. „Wyłącz dostęp zdalny”
    /// wylogowuje jawnie (`signOut`).
    private init() {
        guard let config = RemoteConfig.configuration else { return }
        if let saved = Keychain.load(), saved.configuration == config.identity {
            tokens = saved
            state = .signedIn(user: saved.user)
        } else {
            Keychain.delete()
        }
    }

    /// Adres strony logowania — aplikacja otwiera go w domyślnej przeglądarce użytkownika.
    package func signInURL() throws -> URL {
        guard let config = RemoteConfig.configuration else {
            throw BackendError.remote(tr("Najpierw ustaw adres logowania i identyfikator klienta w zakładce Usługi."))
        }
        generation &+= 1
        let verifier = Self.randomString()
        let stateValue = Self.randomString()
        pending = (verifier, stateValue, config, generation)
        state = .waitingForBrowser
        let challenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncoded
        var components = URLComponents(url: config.base.appendingPathComponent("authorize"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            .init(name: "response_type", value: "code"),
            .init(name: "client_id", value: config.clientID),
            .init(name: "redirect_uri", value: RemoteConfig.redirectURI),
            .init(name: "scope", value: RemoteConfig.scopes),
            .init(name: "code_challenge", value: challenge),
            .init(name: "code_challenge_method", value: "S256"),
            .init(name: "state", value: stateValue),
        ]
        return components.url!
    }

    /// Powrót z przeglądarki: `gramadrama://auth?code=…&state=…`. Zwraca nazwę zalogowanego użytkownika.
    package func handle(_ url: URL) async throws -> String {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func item(_ name: String) -> String? { items.first { $0.name == name }?.value }
        defer { if !isSignedIn { state = .signedOut } }
        if let error = item("error") { throw BackendError.remote(item("error_description") ?? error) }
        guard let pending, item("state") == pending.state, let code = item("code"),
              pending.config == RemoteConfig.configuration else {
            throw BackendError.remote(tr("Nieoczekiwany powrót z logowania — spróbuj jeszcze raz."))
        }
        self.pending = nil
        let response = try await tokenRequest([
            "grant_type": "authorization_code", "code": code, "redirect_uri": RemoteConfig.redirectURI,
            "client_id": pending.config.clientID, "code_verifier": pending.verifier,
        ], config: pending.config)
        guard pending.generation == generation, pending.config == RemoteConfig.configuration else {
            throw BackendError.signInRequired
        }
        let user = Self.userName(fromIDToken: response.id_token) ?? "użytkownik"
        store(response, user: user, config: pending.config)
        EventLog.write("remote_sign_in", ["refresh": response.refresh_token != nil])
        return user
    }

    /// Ważny token dostępu; odświeża go minutę przed wygaśnięciem. Jedno odświeżanie naraz.
    package func accessToken() async throws -> String {
        guard let tokens, let config = RemoteConfig.configuration,
              tokens.configuration == config.identity else {
            signOut()
            throw BackendError.signInRequired
        }
        let started = generation
        if tokens.expiresAt.timeIntervalSinceNow > 60 { return tokens.access }
        if let refreshing { return try await refreshing.value }
        guard let refresh = tokens.refresh else {
            signOut()
            throw BackendError.signInRequired
        }
        let task = Task { () throws -> String in
            do {
                let response = try await self.tokenRequest([
                    "grant_type": "refresh_token", "refresh_token": refresh, "client_id": config.clientID,
                ], config: config)
                guard started == self.generation, config == RemoteConfig.configuration else {
                    throw BackendError.signInRequired
                }
                self.store(response, user: tokens.user, config: config)
                return response.access_token
            } catch BackendError.remote {
                // Token odświeżania unieważniony (wylogowanie w Pocket ID, usunięte konto) — trzeba zalogować się od nowa.
                if started == self.generation { self.signOut() }
                throw BackendError.signInRequired
            }
        }
        refreshing = task
        defer { if started == generation { refreshing = nil } }
        return try await task.value
    }

    /// nginx odrzucił token (401) — przy następnym zapytaniu odświeżymy go.
    package func tokenRejected() {
        tokens?.expiresAt = .distantPast
    }

    package func signOut() {
        generation &+= 1
        refreshing?.cancel()
        refreshing = nil
        tokens = nil
        pending = nil
        Keychain.delete()
        state = .signedOut
        Route.shared.forget()
    }

    package func cancelSignIn() {
        generation &+= 1
        pending = nil
        if !isSignedIn { state = .signedOut }
    }

    // MARK: Pomocnicze

    private struct TokenResponse: Decodable {
        let access_token: String
        let expires_in: Double?
        let refresh_token: String?
        let id_token: String?
    }

    private func tokenRequest(_ fields: [String: String], config: RemoteServiceConfiguration) async throws -> TokenResponse {
        var request = URLRequest(url: config.base.appendingPathComponent("api/oidc/token"))
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(fields.map { "\($0.key)=\(Self.formEscape($0.value))" }.joined(separator: "&").utf8)
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw BackendError.remote(json?["error_description"] as? String ?? json?["error"] as? String
                ?? tr("Pocket ID odpowiedział HTTP {code}", ["code": (response as? HTTPURLResponse)?.statusCode ?? 0]))
        }
        return try JSONDecoder().decode(TokenResponse.self, from: data)
    }

    private func store(_ response: TokenResponse, user: String, config: RemoteServiceConfiguration) {
        let stored = Tokens(access: response.access_token, refresh: response.refresh_token ?? tokens?.refresh,
                            expiresAt: Date().addingTimeInterval(response.expires_in ?? 3600), user: user,
                            configuration: config.identity)
        tokens = stored
        Keychain.save(stored)
        state = .signedIn(user: user)
    }

    /// Nazwa z `id_token` (JWT) — tylko do wyświetlenia; podpis sprawdza serwer przy każdym zapytaniu.
    nonisolated package static func userName(fromIDToken token: String?) -> String? {
        let parts = token?.split(separator: ".") ?? []
        guard parts.count >= 2, let payload = Data(base64URLEncoded: String(parts[1])),
              let claims = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else { return nil }
        return (claims["preferred_username"] ?? claims["name"] ?? claims["sub"]) as? String
    }

    private static func randomString() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64URLEncoded
    }

    private static func formEscape(_ value: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    /// Tokeny w pęku kluczy macOS (tylko to urządzenie, dostęp po odblokowaniu Maca).
    private enum Keychain {
        static let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "pl.gramadrama.remote",
            kSecAttrAccount as String: "pocket-id",
        ]

        static func load() -> Tokens? {
            var result: CFTypeRef?
            var search = query
            search[kSecReturnData as String] = true
            guard SecItemCopyMatching(search as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
            return try? JSONDecoder().decode(Tokens.self, from: data)
        }

        static func save(_ tokens: Tokens) {
            guard let data = try? JSONEncoder().encode(tokens) else { return }
            let attributes: [String: Any] = [kSecValueData as String: data,
                                             kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
            if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
                SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil)
            }
        }

        static func delete() { SecItemDelete(query as CFDictionary) }
    }
}

package extension Data {
    var base64URLEncoded: String {
        base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    init?(base64URLEncoded string: String) {
        var base64 = string.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        self.init(base64Encoded: base64)
    }
}
