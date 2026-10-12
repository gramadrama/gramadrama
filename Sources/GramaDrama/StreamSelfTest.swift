import Foundation
import Network
import GramaBackend

/// Atrapa serwera Ollamy na localhost (losowy port) — testy strumienia bez zewnętrznego serwera.
private final class StubServer: @unchecked Sendable {
    private let listener: NWListener
    private let reply: Data
    private let lock = NSLock()
    private var count = 0
    /// Liczba połączeń — test pamięci wyników sprawdza, ile zapytań naprawdę doszło do „serwera”.
    var requests: Int { lock.withLock { count } }

    init(reply: String) throws {
        self.reply = Data(reply.utf8)
        listener = try NWListener(using: .tcp, on: .any)
        listener.newConnectionHandler = { [reply = self.reply, weak self] connection in
            self?.lock.withLock { self?.count += 1 }
            connection.start(queue: .global())
            StubServer.readRequest(connection, buffer: Data()) {
                connection.send(content: reply, completion: .contentProcessed { _ in connection.cancel() })
            }
        }
        listener.start(queue: .global())
    }

    /// Czyta całe żądanie (nagłówki + Content-Length), zanim odpowie — inaczej klient dostałby reset.
    private static func readRequest(_ connection: NWConnection, buffer: Data, then respond: @escaping () -> Void) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { data, _, done, _ in
            var buffer = buffer
            if let data { buffer.append(data) }
            let text = String(decoding: buffer, as: UTF8.self)
            if let headerEnd = text.range(of: "\r\n\r\n") {
                let headers = text[..<headerEnd.lowerBound].lowercased()
                let length = headers.split(separator: "\r\n").first { $0.hasPrefix("content-length:") }
                    .flatMap { Int($0.split(separator: ":")[1].trimmingCharacters(in: .whitespaces)) } ?? 0
                let bodyBytes = buffer.count - text[..<headerEnd.upperBound].utf8.count
                if bodyBytes >= length || done { return respond() }
            } else if done {
                return respond()
            }
            readRequest(connection, buffer: buffer, then: respond)
        }
    }

    func baseURL() async -> URL {
        for _ in 0..<100 {
            if let port = listener.port, port.rawValue != 0 { return URL(string: "http://127.0.0.1:\(port.rawValue)")! }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return URL(string: "http://127.0.0.1:1")!
    }

    func stop() { listener.cancel() }
}

/// Testy obsługi strumienia Ollamy (część `--selftest`).
enum StreamSelfTest {
    static func run() async -> Int {
        print("Strumień Ollamy (atrapa serwera)")
        var failures = 0
        func ndjson(_ lines: [String]) -> String {
            "HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nConnection: close\r\n\r\n" + lines.joined(separator: "\n") + "\n"
        }
        let cases: [(name: String, reply: String, expected: String)] = [
            ("poprawna odpowiedź",
             ndjson([#"{"message":{"content":"<text>\nPopraw "},"done":false}"#,
                     #"{"message":{"content":"to.\n</text>"},"done":false}"#,
                     #"{"message":{"content":""},"done":true,"done_reason":"stop","eval_count":4,"eval_duration":1000000000}"#]),
             "ok: Popraw to."),
            ("błąd w trakcie strumienia",
             ndjson([#"{"message":{"content":"Pop"},"done":false}"#, #"{"error":"model runner has unexpectedly stopped"}"#]),
             "błąd: Model przerwał odpowiedź: model runner has unexpectedly stopped"),
            ("brak zakończenia (done)",
             ndjson([#"{"message":{"content":"Popraw"},"done":false}"#]),
             "błąd: Połączenie z modelem zerwało się przed końcem odpowiedzi."),
            ("ucięcie limitem długości",
             ndjson([#"{"message":{"content":"Popraw"},"done":false}"#, #"{"message":{"content":""},"done":true,"done_reason":"length"}"#]),
             "błąd: Tekst jest za długi dla modelu — zaznacz krótszy fragment."),
            ("HTTP 404 z treścią błędu",
             "HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n" + #"{"error":"model 'bielik' not found"}"#,
             "błąd: model 'bielik' not found (HTTP 404)"),
        ]
        for c in cases {
            let actual: String
            do {
                let server = try StubServer(reply: c.reply)
                defer { server.stop() }
                let backend = Backend(model: "test", server: .init(base: await server.baseURL(), api: .ollama), cache: false)
                do {
                    let result = try await backend.correct("popraw to", mode: .fix) { _ in }
                    actual = "ok: \(result)"
                } catch {
                    actual = "błąd: \(error.localizedDescription)"
                }
            } catch {
                actual = "atrapa nie wystartowała: \(error)"
            }
            if actual == c.expected {
                print("  ✓ \(c.name)")
            } else {
                failures += 1
                print("  ✗ \(c.name)\n      wynik:     \(actual.debugDescription)\n      oczekiwano: \(c.expected.debugDescription)")
            }
        }

        // Porządkowanie dyktowania idzie tą samą ścieżką strumienia i post-processingu.
        let dictation: String
        do {
            let server = try StubServer(reply: ndjson([
                #"{"message":{"content":"<text>\nSpotkanie przesuwamy na czwartek."},"done":false}"#,
                #"{"message":{"content":"\n</text>"},"done":false}"#,
                #"{"message":{"content":""},"done":true,"done_reason":"stop"}"#]))
            defer { server.stop() }
            let backend = Backend(model: "test", server: .init(base: await server.baseURL(), api: .ollama), cache: false)
            dictation = (try? await backend.cleanDictation("spotkanie przesuwamy na środę znaczy nie na czwartek", vocabulary: [])) ?? "błąd"
        } catch {
            dictation = "atrapa nie wystartowała: \(error)"
        }
        if dictation == "Spotkanie przesuwamy na czwartek." {
            print("  ✓ porządkowanie dyktowania")
        } else {
            failures += 1
            print("  ✗ porządkowanie dyktowania\n      wynik:     \(dictation.debugDescription)")
        }
        failures += await cacheTests(ndjson)
        return failures
    }

    /// Wspólna pamięć wyników (etap 4A): trafienie, dołączenie do zapytania w toku, przerywanie.
    @MainActor
    private static func cacheTests(_ ndjson: ([String]) -> String) async -> Int {
        print("Pamięć wyników (etap 4)")
        var failures = 0
        func check(_ name: String, _ actual: String, _ expected: String) {
            if actual == expected {
                print("  ✓ \(name)")
            } else {
                failures += 1
                print("  ✗ \(name)\n      wynik:     \(actual.debugDescription)\n      oczekiwano: \(expected.debugDescription)")
            }
        }
        @Sendable func slow(_ value: String, _ ms: UInt64 = 200, partial: String? = nil)
            -> (@escaping @MainActor (String) -> Void) async throws -> String {
            { forward in
                if let partial {
                    try await Task.sleep(nanoseconds: 50_000_000)
                    await forward(partial)
                }
                try await Task.sleep(nanoseconds: ms * 1_000_000)
                return value
            }
        }

        let cache = RequestCache<String>(name: "test", grace: 0.15)
        let first = try? await cache.value(for: "a", chars: 1, log: false, compute: slow("A", 10))
        let second = try? await cache.value(for: "a", chars: 1, log: false, compute: slow("X", 10))
        check("drugie zapytanie z pamięci", "\(first?.1.rawValue ?? "błąd") \(second?.0 ?? "") \(second?.1.rawValue ?? "błąd")",
              "model A cache")

        // Dwa jednoczesne zapytania o to samo: jedno liczenie, drugie dołącza i dostaje też strumień.
        var streamed = ""
        async let one = try? cache.value(for: "b", chars: 1, log: false, compute: slow("B", 300, partial: "B…"))
        try? await Task.sleep(nanoseconds: 100_000_000)
        async let two = try? cache.value(for: "b", chars: 1, log: false, onPartial: { streamed = $0 }, compute: slow("Y"))
        let (r1, r2) = await (one, two)
        check("dołączenie do zapytania w toku", "\(r1?.1.rawValue ?? "błąd") \(r2?.1.rawValue ?? "błąd") \(r2?.0 ?? "") \(streamed)",
              "model joined B B…")

        // Przerwany jeden z dwóch czekających: liczenie trwa dalej dla drugiego.
        let cancelled = Task { try await cache.value(for: "c", chars: 1, log: false, compute: slow("C", 300)) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        async let survivor = try? cache.value(for: "c", chars: 1, log: false, compute: slow("Z"))
        try? await Task.sleep(nanoseconds: 50_000_000)
        cancelled.cancel()
        let cancelledResult = await cancelled.result
        let survived = await survivor
        check("przerwany czekający nie przerywa liczenia dla innych",
              "\((try? cancelledResult.get()) == nil ? "przerwane" : "wynik") \(survived?.0 ?? "błąd")", "przerwane C")

        // Nikt nie czeka: po chwili zapytanie jest przerywane; w tej chwili ⌥⌘G może jeszcze dołączyć.
        let computedBefore = cache.computed
        let orphan = Task { try await cache.value(for: "d", chars: 1, log: false, compute: slow("D", 400)) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        orphan.cancel()
        try? await Task.sleep(nanoseconds: 50_000_000)
        let rejoined = try? await cache.value(for: "d", chars: 1, log: false, compute: slow("W"))
        check("panel dołącza do przerwanego przed chwilą sprawdzania",
              "\(rejoined?.0 ?? "błąd") \(rejoined?.1.rawValue ?? "") liczeń: \(cache.computed - computedBefore)", "D joined liczeń: 1")

        let lonely = Task { try await cache.value(for: "e", chars: 1, log: false, compute: slow("E", 400)) }
        try? await Task.sleep(nanoseconds: 50_000_000)
        lonely.cancel()
        try? await Task.sleep(nanoseconds: 300_000_000)  // dłużej niż grace
        let fresh = try? await cache.value(for: "e", chars: 1, log: false, compute: slow("E2", 10))
        check("po zwłoce osierocone zapytanie przerwane, następne liczy od nowa", fresh?.0 ?? "błąd", "E2")

        // Zapytanie z panelu przerywa osierocone sprawdzanie w tle o inny tekst od razu.
        let patient = RequestCache<String>(name: "test", grace: 5)
        var backgroundOutcome = "trwa"
        let background = Task { try await patient.value(for: "tło", chars: 1, background: true, log: false) { _ in
            do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { backgroundOutcome = "przerwane"; throw error }
            backgroundOutcome = "dokończone"
            return "T"
        } }
        try? await Task.sleep(nanoseconds: 50_000_000)
        background.cancel()
        try? await Task.sleep(nanoseconds: 50_000_000)
        _ = try? await patient.value(for: "panel", chars: 1, log: false, compute: slow("P", 10))
        try? await Task.sleep(nanoseconds: 50_000_000)
        check("panel przerywa osierocone sprawdzanie w tle", backgroundOutcome, "przerwane")

        struct Failure: Error {}
        _ = try? await cache.value(for: "f", chars: 1, log: false) { _ in throw Failure() }
        let retried = try? await cache.value(for: "f", chars: 1, log: false, compute: slow("F", 10))
        check("błąd nie trafia do pamięci", retried.map { "\($0.0) \($0.1.rawValue)" } ?? "błąd", "F model")

        // Backend: ten sam tekst (także z inną spacją na końcu) → jedno zapytanie do serwera.
        do {
            let server = try StubServer(reply: ndjson([
                #"{"message":{"content":"Popraw to."},"done":false}"#,
                #"{"message":{"content":""},"done":true,"done_reason":"stop"}"#]))
            defer { server.stop() }
            let url = await server.baseURL()
            let a = try await Backend(model: "cache-test", server: .init(base: url, api: .ollama)).correct("popraw to", mode: .fix) { _ in }
            let b = try await Backend(model: "cache-test", server: .init(base: url, api: .ollama)).correct("popraw to\n", mode: .fix) { _ in }
            check("Backend: ten sam tekst raz do serwera, spacje z brzegów zachowane",
                  "\(a.debugDescription) \(b.debugDescription) zapytań: \(server.requests)", #""Popraw to." "Popraw to.\n" zapytań: 1"#)
        } catch {
            check("Backend: pamięć", "\(error)", "—")
        }
        return failures
    }
}
