import AppKit
import GramaCore
import GramaBackend

/// Ollama na tym Macu („Ten Mac” w modelu językowym): czy działa i czy jest zainstalowana, uruchomienie aplikacji
/// i pobieranie modelu z postępem (`POST /api/pull`). Aplikacja Ollamy sama uruchamia serwer na 127.0.0.1:11434
/// (inny adres tego Maca można wpisać w Usługach — `Settings.localModelServer`);
/// instalacja z Homebrew działa jako usługa, której aplikacja nie uruchamia.
@MainActor
final class LocalOllama: ObservableObject {
    static let shared = LocalOllama()

    enum State: Equatable {
        case unknown
        case running(version: String)
        /// Zainstalowana, ale serwer nie odpowiada. `app` nil — sam program (np. z Homebrew).
        case stopped(app: URL?)
        case missing
    }

    struct Download: Equatable {
        let model: String
        var progress = OllamaPullProgress()
        var error: String?
        var active: Bool { error == nil && !progress.finished }
    }

    enum PullError: Error {
        case ollama(String)
        case incomplete
    }

    @Published private(set) var state: State = .unknown
    @Published private(set) var launching = false
    @Published private(set) var download: Download?
    private var downloadTask: Task<Void, Never>?

    static let downloadPage = URL(string: "https://ollama.com/download")!
    /// Model zalecany do pobrania na tym Macu: domyślny model nowej instalacji (`Settings.defaultModel`).
    static let suggestedModel = Settings.defaultModel

    nonisolated private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        // Pobieranie trwa minuty, ale Ollama co chwilę podaje postęp — długa cisza to zerwane połączenie.
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 6 * 3600
        return URLSession(configuration: config)
    }()

    func refresh() async {
        if let version = await Self.version() {
            state = .running(version: version)
        } else if let app = Self.installedApp() {
            state = .stopped(app: app)
        } else {
            state = Self.installedTool() ? .stopped(app: nil) : .missing
        }
    }

    nonisolated static func version(base: URL = Settings.shared.localModelServer.base) async -> String? {
        var request = URLRequest(url: base.appendingPathComponent("api/version"))
        request.timeoutInterval = 1.5
        guard let (data, response) = try? await session.data(for: request), (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return json["version"] as? String
    }

    private static func installedApp() -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return [URL(fileURLWithPath: "/Applications/Ollama.app"), home.appendingPathComponent("Applications/Ollama.app")]
            .first { FileManager.default.fileExists(atPath: $0.path) }
    }

    /// Sam program `ollama` (Homebrew na Apple Silicon i Intelu).
    private static func installedTool() -> Bool {
        ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"].contains { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// Uruchamia aplikację Ollamy w tle i czeka (do 15 s), aż serwer odpowie; potem `done` (odświeżenie stanu usług).
    func launch(then done: @escaping @MainActor () -> Void) {
        guard case .stopped(let app?) = state, !launching else { return }
        launching = true
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        NSWorkspace.shared.openApplication(at: app, configuration: configuration) { _, _ in }
        Task {
            for _ in 0..<30 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if let version = await Self.version() {
                    state = .running(version: version)
                    break
                }
            }
            launching = false
            if case .running = state {} else { await refresh() }
            done()
        }
    }

    /// Pobiera model do Ollamy na tym Macu; po sukcesie `done` z nazwą (odświeżenie listy, wybór modelu).
    func pull(_ name: String, then done: @escaping @MainActor (String) -> Void) {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, download?.active != true else { return }
        download = Download(model: name)
        downloadTask = Task {
            var shown = Date.distantPast
            do {
                try await Self.pull(name, base: Settings.shared.localModelServer.base) { progress in
                    // Ollama podaje postęp wiele razy na sekundę — okno odświeżamy najwyżej pięć razy.
                    guard Date().timeIntervalSince(shown) > 0.2 || progress.finished else { return }
                    shown = Date()
                    self.download?.progress = progress
                }
                EventLog.write("ollama_pull", ["outcome": "ok"])
                done(name)
            } catch {
                guard !Task.isCancelled else { return }
                download?.error = Self.message(for: error)
                EventLog.write("ollama_pull", ["outcome": "error"])
            }
        }
    }

    func cancelPull() {
        downloadTask?.cancel()
        downloadTask = nil
        download = nil
    }

    /// `POST {base}/api/pull` strumieniem; `onProgress` po każdej linii. Błąd Ollamy (`{"error": …}`) to `PullError.ollama`.
    static func pull(_ name: String, base: URL, onProgress: (OllamaPullProgress) -> Void) async throws {
        var request = URLRequest(url: base.appendingPathComponent("api/pull"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["model": name, "stream": true])
        let (bytes, response) = try await session.bytes(for: request)
        var progress = OllamaPullProgress()
        for try await line in bytes.lines {
            try Task.checkCancellation()
            if let error = progress.apply(line) { throw PullError.ollama(error) }
            onProgress(progress)
        }
        if let http = response as? HTTPURLResponse, http.statusCode != 200 { throw PullError.ollama("HTTP \(http.statusCode)") }
        guard progress.finished else { throw PullError.incomplete }
    }

    private static func message(for error: Error) -> String {
        switch error {
        case PullError.ollama(let text) where text.contains("file does not exist") || text.contains("not found"):
            tr("Nie ma takiego modelu w bibliotece Ollamy — sprawdź nazwę, np. {model}.", ["model": suggestedModel])
        case PullError.ollama(let text):
            "Ollama: \(text)"
        case PullError.incomplete:
            tr("Pobieranie przerwało się — spróbuj ponownie, Ollama dokończy od miejsca przerwania.")
        case let error as URLError where error.code == .cannotConnectToHost:
            tr("Ollama na tym Macu nie odpowiada.")
        default:
            error.localizedDescription
        }
    }
}
