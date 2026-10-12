import Foundation
import Network
import GramaCore
import GramaBackend

/// Atrapa serwera HTTP na localhost (losowy port): odpowiedź zależna od żądania i jego numeru, zapis żądań.
/// Do testów trybu „tylko model” (Ollama) i serwera mowy — bez zewnętrznych usług.
final class HTTPStub: @unchecked Sendable {
    struct Request {
        let method: String
        let path: String
        let headers: [String: String]
        let body: Data
        var text: String { String(decoding: body, as: UTF8.self) }
    }

    struct Response {
        var status = 200
        var headers: [String: String] = [:]
        var body = Data()

        init(status: Int = 200, headers: [String: String] = [:], body: String = "") {
            self.status = status
            self.headers = headers
            self.body = Data(body.utf8)
        }

        /// Odpowiedź Ollamy /api/chat w strumieniu NDJSON z całą treścią w jednym kawałku.
        static func ollama(_ content: String, doneReason: String = "stop") -> Response {
            let chunk = String(decoding: try! JSONSerialization.data(withJSONObject: ["message": ["content": content], "done": false]), as: UTF8.self)
            return Response(headers: ["Content-Type": "application/x-ndjson"],
                            body: chunk + "\n" + #"{"message":{"content":""},"done":true,"done_reason":"\#(doneReason)"}"# + "\n")
        }

        /// Odpowiedź `/chat/completions` API zgodnego z OpenAI: zdarzenia SSE z kawałkami, `finish_reason` i `[DONE]`.
        static func openAI(_ parts: [String], finishReason: String = "stop") -> Response {
            func event(_ object: [String: Any]) -> String {
                "data: " + String(decoding: try! JSONSerialization.data(withJSONObject: object), as: UTF8.self)
            }
            let events = parts.map { event(["choices": [["index": 0, "delta": ["content": $0]]]]) }
                + [event(["choices": [["index": 0, "delta": [String: String](), "finish_reason": finishReason]]]), "data: [DONE]"]
            return Response(headers: ["Content-Type": "text/event-stream"], body: events.joined(separator: "\n\n") + "\n\n")
        }
    }

    private let listener: NWListener
    private let lock = NSLock()
    private var log: [Request] = []
    var requests: [Request] { lock.withLock { log } }

    init(_ handler: @escaping @Sendable (Request, Int) -> Response) throws {
        listener = try NWListener(using: .tcp, on: .any)
        listener.newConnectionHandler = { [weak self] connection in
            connection.start(queue: .global())
            Self.read(connection, buffer: Data()) { request in
                guard let self else { return connection.cancel() }
                let index: Int = self.lock.withLock {
                    self.log.append(request)
                    return self.log.count - 1
                }
                let response = handler(request, index)
                var head = "HTTP/1.1 \(response.status) Stub\r\nConnection: close\r\nContent-Length: \(response.body.count)\r\n"
                for (name, value) in response.headers { head += "\(name): \(value)\r\n" }
                connection.send(content: Data((head + "\r\n").utf8) + response.body,
                                completion: .contentProcessed { _ in connection.cancel() })
            }
        }
        listener.start(queue: .global())
    }

    /// Nagłówki i całe ciało (Content-Length), zanim odpowie — inaczej klient dostałby reset.
    private static func read(_ connection: NWConnection, buffer: Data, then respond: @escaping (Request) -> Void) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { data, _, done, _ in
            var buffer = buffer
            if let data { buffer.append(data) }
            if let end = buffer.range(of: Data("\r\n\r\n".utf8)) {
                let head = String(decoding: buffer[..<end.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
                var headers: [String: String] = [:]
                for line in head.dropFirst() {
                    guard let colon = line.firstIndex(of: ":") else { continue }
                    headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
                }
                let length = Int(headers["content-length"] ?? "") ?? 0
                let body = buffer[end.upperBound...]
                if body.count >= length || done {
                    let parts = head.first?.split(separator: " ") ?? []
                    return respond(Request(method: parts.first.map(String.init) ?? "", path: parts.count > 1 ? String(parts[1]) : "",
                                           headers: headers, body: Data(body)))
                }
            } else if done {
                return connection.cancel()
            }
            read(connection, buffer: buffer, then: respond)
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

/// Część `--selftest`: tryb „tylko model” i serwer mowy na atrapach serwerów.
@MainActor
enum ServerSelfTest {
    private static var failures = 0

    private static func check(_ name: String, _ actual: String, _ expected: String) {
        if actual == expected {
            print("  ✓ \(name)")
        } else {
            failures += 1
            print("  ✗ \(name)\n      wynik:     \(actual.debugDescription)\n      oczekiwano: \(expected.debugDescription)")
        }
    }

    static func run() async -> Int {
        failures = 0
        print("Tryb „tylko model” (atrapa Ollamy)")
        await modelOnly()
        print("Serwer modelu: API zgodne z OpenAI, stan i pobieranie modelu (atrapa)")
        await modelServer()
        print("Serwer mowy (atrapa)")
        await speech()
        return failures
    }

    nonisolated private static func json(_ request: HTTPStub.Request) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: request.body) as? [String: Any]) ?? [:]
    }

    private static func modelServer() async {
        do {
            // API zgodne z OpenAI: /v1/chat/completions strumieniem SSE, model i limit odpowiedzi w treści żądania.
            let openAI = try HTTPStub { request, _ in
                switch request.path {
                case "/v1/chat/completions": .openAI(["Jutro ", "piszę ", "raport."])
                case "/v1/models": HTTPStub.Response(body: #"{"object":"list","data":[{"id":"korektor","object":"model"}]}"#)
                case "/v2/languages": HTTPStub.Response(body: #"[{"name":"Polish","code":"pl","longCode":"pl-PL"}]"#)
                default: HTTPStub.Response(status: 404, body: "404 page not found")
                }
            }
            defer { openAI.stop() }
            let server = ModelServerEndpoint(address: (await openAI.baseURL()).absoluteString, api: .openAI)!
            let backend = Backend(model: "korektor", server: server, cache: false)
            let fixed = try await backend.correct("Jutro pisze raport.", mode: .fix) { _ in }
            check("OpenAI: poprawka ze strumienia", fixed, "Jutro piszę raport.")
            let sent = openAI.requests.last.map(json) ?? [:]
            check("OpenAI: ścieżka i treść żądania",
                  "\(openAI.requests.last?.path ?? "") \(sent["model"] ?? "") \(sent["stream"] ?? "") \(sent["max_tokens"] is Int) \(sent["temperature"] ?? "")",
                  "/v1/chat/completions korektor 1 true 0")
            let diagnostics = await backend.diagnostics()
            check("OpenAI: stan i lista modeli", "\(stateLabel(diagnostics.model)) · \(diagnostics.models ?? [])", "ok: Działa · [\"korektor\"]")
            check("LanguageTool: stan", stateLabel(diagnostics.languageTool), "ok: Działa")

            // Serwer mówi błędem 400, którego parametru nie przyjmuje — max_tokens, potem temperature; trzecie żądanie przechodzi.
            let strict = try HTTPStub { _, index in
                switch index {
                case 0: HTTPStub.Response(status: 400, body: #"{"error":{"message":"Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.","type":"invalid_request_error"}}"#)
                case 1: HTTPStub.Response(status: 400, body: #"{"error":{"message":"Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported."}}"#)
                default: .openAI(["Jutro piszę raport."])
                }
            }
            defer { strict.stop() }
            let strictServer = ModelServerEndpoint(address: (await strict.baseURL()).absoluteString, api: .openAI)!
            let adapted = try await Backend(model: "m", server: strictServer, cache: false).correct("Jutro pisze raport.", mode: .fix) { _ in }
            let last = strict.requests.last.map(json) ?? [:]
            check("OpenAI: żądanie dostosowane po dwóch błędach 400",
                  "\(adapted) · \(strict.requests.count) · \(last["max_completion_tokens"] is Int) \(last["max_tokens"] == nil) \(last["temperature"] == nil)",
                  "Jutro piszę raport. · 3 · true true true")
            // Poprawka zapamiętana dla serwera i modelu: następne zapytanie od razu właściwe.
            _ = try await Backend(model: "m", server: strictServer, cache: false).correct("Kot ma ale.", mode: .fix) { _ in }
            let next = strict.requests.last.map(json) ?? [:]
            check("OpenAI: poprawka zapamiętana, jedno zapytanie",
                  "\(strict.requests.count) · \(next["max_completion_tokens"] is Int) \(next["temperature"] == nil)", "4 · true true")

            // Ucięta odpowiedź (finish_reason: length) — odrzucony fragment, jak przy Ollamie; błąd w strumieniu — komunikat.
            let long = try HTTPStub { _, _ in .openAI(["Coś zupełnie innego"], finishReason: "length") }
            defer { long.stop() }
            let (truncated, _) = try await Backend(model: "m", server: ModelServerEndpoint(address: (await long.baseURL()).absoluteString, api: .openAI)!)
                .liveFragment("Kot ma ale.")
            check("OpenAI: ucięta odpowiedź to odrzucony fragment", describe(truncated), "odrzucone: truncated")
            let failing = try HTTPStub { _, _ in
                HTTPStub.Response(headers: ["Content-Type": "text/event-stream"], body: "data: {\"error\":{\"message\":\"out of memory\"}}\n\n")
            }
            defer { failing.stop() }
            do {
                _ = try await Backend(model: "m", server: ModelServerEndpoint(address: (await failing.baseURL()).absoluteString, api: .openAI)!,
                                      cache: false).correct("Kot ma ale.", mode: .fix) { _ in }
                check("OpenAI: błąd w strumieniu", "bez błędu", "błąd")
            } catch {
                check("OpenAI: błąd w strumieniu", "\(error.localizedDescription) · \(errorKind(error))",
                      "Model przerwał odpowiedź: out of memory · model_error")
            }

            // Kod przyczyny nieudanej korekty (`errorKind`, panel: `Problems`) — te same kody co na Linuksie.
            func kind(_ response: @escaping @Sendable (HTTPStub.Request, Int) -> HTTPStub.Response,
                      api: ModelServerAPI = .openAI) async -> String {
                do {
                    let stub = try HTTPStub(response)
                    defer { stub.stop() }
                    let endpoint = ModelServerEndpoint(address: (await stub.baseURL()).absoluteString, api: api)!
                    _ = try await Backend(model: "m", server: endpoint, cache: false).correct("Kot ma ale.", mode: .fix) { _ in }
                    return "bez błędu"
                } catch {
                    return errorKind(error)
                }
            }
            check("przyczyna: nieczytelny strumień", await kind { _, _ in
                HTTPStub.Response(headers: ["Content-Type": "text/event-stream"], body: "data: {nie json\n\n")
            }, "bad_reply")
            check("przyczyna: brak modelu na serwerze", await kind { _, _ in
                HTTPStub.Response(status: 404, body: #"{"error":"model 'm' not found"}"#)
            }, "http_404")
            check("przyczyna: strumień Ollamy bez końca", await kind({ _, _ in
                HTTPStub.Response(headers: ["Content-Type": "application/x-ndjson"], body: #"{"message":{"content":"Kot"},"done":false}"# + "\n")
            }, api: .ollama), "interrupted")
            check("przyczyna: pusta odpowiedź", await kind({ _, _ in .ollama("") }, api: .ollama), "empty_reply")
            check("przyczyna: ucięta odpowiedź", await kind { _, _ in .openAI(["Kot"], finishReason: "length") }, "truncated_reply")
            let closed = try HTTPStub { _, _ in HTTPStub.Response() }
            let closedURL = await closed.baseURL()
            closed.stop()
            do {
                _ = try await Backend(model: "m", server: ModelServerEndpoint(address: closedURL.absoluteString, api: .openAI)!, cache: false)
                    .correct("Kot ma ale.", mode: .fix) { _ in }
                check("przyczyna: serwer nie przyjmuje połączeń", "bez błędu", "refused")
            } catch {
                check("przyczyna: serwer nie przyjmuje połączeń", errorKind(error), "refused")
            }

            // Podpowiedź protokołu: Ollama wybrana jako OpenAI (odpowiada na oba sposoby) i odwrotnie.
            let ollama = try HTTPStub { request, _ in
                switch request.path {
                case "/api/version": HTTPStub.Response(body: #"{"version":"0.12.0"}"#)
                case "/api/tags": HTTPStub.Response(body: #"{"models":[{"name":"gemma4:e4b"},{"name":"bielik:11b"}]}"#)
                case "/api/ps": HTTPStub.Response(body: #"{"models":[{"name":"gemma4:e4b"}]}"#)
                case "/v1/models": HTTPStub.Response(body: #"{"object":"list","data":[{"id":"gemma4:e4b"}]}"#)
                default: HTTPStub.Response(status: 404, body: "404 page not found")
                }
            }
            defer { ollama.stop() }
            let ollamaBase = (await ollama.baseURL()).absoluteString
            let native = await Backend(server: ModelServerEndpoint(address: ollamaBase, api: .ollama)!).diagnostics()
            check("Ollama: wersja, modele i model w pamięci",
                  "\(stateLabel(native.model)) · \(native.models ?? []) · \(native.loadedModels)",
                  "ok: Działa · [\"bielik:11b\", \"gemma4:e4b\"] · [\"gemma4:e4b\"]")
            let asOpenAI = await Backend(server: ModelServerEndpoint(address: ollamaBase, api: .openAI)!).diagnostics()
            check("Ollama wybrana jako OpenAI: podpowiedź", stateLabel(asOpenAI.model),
                  "warning: Działa, ale to Ollama — wybierz protokół „Ollama”, inaczej model wczytuje się od nowa.")
            let openAIBase = (await openAI.baseURL()).absoluteString
            let asOllama = await Backend(server: ModelServerEndpoint(address: openAIBase, api: .ollama)!).diagnostics()
            check("serwer OpenAI wybrany jako Ollama: podpowiedź", stateLabel(asOllama.model),
                  "warning: To serwer zgodny z OpenAI — wybierz ten protokół.")

            // Serwer wymaga klucza.
            let locked = try HTTPStub { _, _ in HTTPStub.Response(status: 401, body: #"{"error":{"message":"Missing API key"}}"#) }
            defer { locked.stop() }
            let lockedState = await Backend(server: ModelServerEndpoint(address: (await locked.baseURL()).absoluteString, api: .openAI)!).diagnostics()
            check("401: prośba o klucz", stateLabel(lockedState.model),
                  "error: Serwer wymaga klucza API (HTTP 401) — wpisz go w polu „Klucz API”.")

            // Pobieranie modelu do Ollamy: postęp z sumy warstw, nazwa w treści żądania; nieznany model — błąd Ollamy.
            let pull = try HTTPStub { request, _ in
                guard request.path == "/api/pull", (json(request)["model"] as? String) == "gemma4:e4b" else {
                    return HTTPStub.Response(status: 500, body: #"{"error":"pull model manifest: file does not exist"}"#)
                }
                return HTTPStub.Response(headers: ["Content-Type": "application/x-ndjson"], body: [
                    #"{"status":"pulling manifest"}"#,
                    #"{"status":"pulling aaa","digest":"sha256:aaa","total":600,"completed":300}"#,
                    #"{"status":"pulling bbb","digest":"sha256:bbb","total":400,"completed":400}"#,
                    #"{"status":"pulling aaa","digest":"sha256:aaa","total":600,"completed":600}"#,
                    #"{"status":"verifying sha256 digest"}"#, #"{"status":"writing manifest"}"#, #"{"status":"success"}"#,
                ].joined(separator: "\n") + "\n")
            }
            defer { pull.stop() }
            var updates: [Double] = []
            var final = OllamaPullProgress()
            try await LocalOllama.pull("gemma4:e4b", base: await pull.baseURL()) { progress in
                updates.append(progress.fraction ?? 0)
                final = progress
            }
            check("pobieranie: postęp i koniec", "\(updates.map { String(format: "%.1f", $0) }.joined(separator: " ")) · \(final.finished)",
                  "0.0 0.5 0.7 1.0 1.0 1.0 1.0 · true")
            do {
                try await LocalOllama.pull("nie-ma-takiego", base: await pull.baseURL()) { _ in }
                check("pobieranie: nieznany model", "bez błędu", "błąd")
            } catch LocalOllama.PullError.ollama(let message) {
                check("pobieranie: nieznany model", message, "pull model manifest: file does not exist")
            }
        } catch {
            check("atrapa serwera modelu", "\(error)", "działa")
        }
    }

    nonisolated private static func temperature(_ request: HTTPStub.Request) -> Double {
        let json = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any]
        return (json?["options"] as? [String: Any])?["temperature"] as? Double ?? -1
    }

    nonisolated private static func userMessage(_ request: HTTPStub.Request) -> String {
        let json = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any]
        return ((json?["messages"] as? [[String: String]])?.last?["content"]) ?? ""
    }

    private static func describe(_ result: LiveFragmentResult) -> String {
        switch result {
        case .corrected(let text): "poprawka: \(text)"
        case .rejected(let reason): "odrzucone: \(reason)"
        }
    }

    private static func modelOnly() async {
        do {
            // Obce pismo przy temperaturze 0, poprawna odpowiedź przy 0,3.
            let drift = try HTTPStub { request, _ in
                temperature(request) == 0 ? .ollama("Jutro pいつze raport.") : .ollama("Jutro piszę raport.")
            }
            defer { drift.stop() }
            let backend = Backend(model: "test-model", server: .init(base: await drift.baseURL(), api: .ollama))
            let (first, _) = try await backend.liveFragment("Jutro pisze raport.")
            check("obce pismo: druga próba z temperaturą 0,3", describe(first), "poprawka: Jutro piszę raport.")
            check("obce pismo: dwa zapytania (0 i 0,3)", drift.requests.map { "\(temperature($0))" }.joined(separator: ","), "0.0,0.3")
            let prompt = drift.requests.first.map(userMessage) ?? ""
            check("prompt fragmentu jak na Linuksie", prompt, "<<<TEKST>>>\nJutro pisze raport.\n<<<KONIEC TEKSTU>>>")

            // Dwa razy obce pismo → odrzucone, a odrzucenie trafia do pamięci (nie męczymy serwera tym samym zdaniem).
            let always = try HTTPStub { _, _ in .ollama("Jutro pいつze raport.") }
            defer { always.stop() }
            let stubborn = Backend(model: "test-model", server: .init(base: await always.baseURL(), api: .ollama))
            let (rejected, _) = try await stubborn.liveFragment("Jutro pisze raport.")
            let (again, source) = try await stubborn.liveFragment("Jutro pisze raport.")
            check("obce pismo dwa razy: odrzucone", describe(rejected), "odrzucone: script")
            check("odrzucony fragment z pamięci", "\(describe(again)) · \(source) · \(always.requests.count) zapytania",
                  "odrzucone: script · cache · 2 zapytania")

            // Za długa odpowiedź (limit) to odrzucony fragment, a nie błąd całego sprawdzania.
            let long = try HTTPStub { _, _ in .ollama("Coś zupełnie innego", doneReason: "length") }
            defer { long.stop() }
            let (truncated, _) = try await Backend(model: "test-model", server: .init(base: await long.baseURL(), api: .ollama)).liveFragment("Kot ma ale.")
            check("ucięta odpowiedź: odrzucony fragment", describe(truncated), "odrzucone: truncated")

            // Błąd serwera przerywa sprawdzanie (rzucony dalej).
            let broken = try HTTPStub { _, _ in HTTPStub.Response(status: 500, body: #"{"error":"out of memory"}"#) }
            defer { broken.stop() }
            do {
                _ = try await Backend(model: "test-model", server: .init(base: await broken.baseURL(), api: .ollama)).liveFragment("Kot ma ale.")
                check("błąd serwera rzucony dalej", "bez błędu", "błąd")
            } catch {
                check("błąd serwera rzucony dalej", error.localizedDescription, "out of memory (HTTP 500)")
            }

            // ⌥⌘G: tekst złożony z poprawionych fragmentów, odstępy i linie nietknięte; fragment z pamięci bez zapytania.
            let fixes = ["Balon opadl gdy ucichł wiatr.": "Balon opadł, gdy ucichł wiatr.",
                         "Potem dlugo lezal na trawie przy samej drodze.": "Potem długo leżał na trawie przy samej drodze."]
            let panel = try HTTPStub { request, _ in
                let message = userMessage(request)
                let fragment = fixes.keys.first { message.contains($0) } ?? ""
                return .ollama(fixes[fragment] ?? fragment)
            }
            defer { panel.stop() }
            let panelBackend = Backend(model: "test-model", server: .init(base: await panel.baseURL(), api: .ollama))
            _ = try await panelBackend.liveFragment("Balon opadl gdy ucichł wiatr.")
            let text = "Balon opadl gdy ucichł wiatr.\n\nPotem dlugo lezal na trawie przy samej drodze. https://example.com/x"
            var partials = 0
            let corrected = try await panelBackend.modelOnlyCorrection(text) { _ in partials += 1 }
            check("⌥⌘G: złożony tekst", corrected.text,
                  "Balon opadł, gdy ucichł wiatr.\n\nPotem długo leżał na trawie przy samej drodze. https://example.com/x")
            check("⌥⌘G: zdanie sprawdzone wcześniej bez zapytania", "\(corrected.fromModel) z modelu, \(panel.requests.count) zapytania",
                  "1 z modelu, 2 zapytania")
            check("⌥⌘G: postęp w trakcie", partials > 0 ? "jest" : "brak", "jest")

            // Cała ścieżka na żywo: od kursora, odrzucone zdanie nie blokuje reszty, błąd serwera przerywa sprawdzanie.
            let opening = "Wczoraj pracowalem do pozna nad raportem dla klienta."
            let middle = "Ten fragment model zawsze psuje obcymi znakami w odpowiedzi."
            let closing = "Jutro rano wysle go do calego zespolu do przejrzenia."
            let live = try HTTPStub { request, _ in
                let message = userMessage(request)
                if message.contains("Wczoraj") { return .ollama("Wczoraj pracowałem do późna nad raportem dla klienta.") }
                if message.contains("Ten fragment") { return .ollama("Ten fragment model zawsze psuje obcymi znakami w odpowiedziい.") }
                if message.contains("Jutro") { return .ollama("Jutro rano wyślę go do całego zespołu do przejrzenia.") }
                return HTTPStub.Response(status: 500, body: #"{"error":"model runner has unexpectedly stopped"}"#)
            }
            defer { live.stop() }
            let liveBackend = Backend(model: "test-model", server: .init(base: await live.baseURL(), api: .ollama))
            let liveText = [opening, middle, closing].joined(separator: " ")
            var order: [String] = [], waits = 0
            let fragments = try await LiveChecker.shared.modelOnly(liveText, caret: (liveText as NSString).length, backend: liveBackend,
                                                                   onWait: { if $0 { waits += 1 } }) { result in
                order.append("\(result.fragment.text.prefix(4))\(result.rejected ? "✗" : "")·\(result.issues.count)")
            }
            check("na żywo: od kursora, odrzucone zdanie osobno", order.joined(separator: " "), "Jutr·3 Ten ✗·0 Wczo·2")
            check("na żywo: fragmenty i zapytania do serwera", "\(fragments.count) fragmenty, \(waits) oczekiwania", "3 fragmenty, 3 oczekiwania")
            var delivered = 0
            do {
                _ = try await LiveChecker.shared.modelOnly(closing + " Zdanie, na którym serwer pada w połowie sprawdzania.", caret: 0,
                                                           backend: liveBackend, onWait: { _ in }) { _ in delivered += 1 }
                check("na żywo: błąd serwera przerywa resztę", "bez błędu", "błąd")
            } catch {
                check("na żywo: błąd serwera przerywa resztę, wcześniejszy wynik zostaje", "\(delivered) · \(error.localizedDescription)",
                      "1 · model runner has unexpectedly stopped (HTTP 500)")
            }

            // Myślenie wyłączone: `think: false` dla każdego modelu, także takiego, który domyślnie nie myśli.
            let thinking = try HTTPStub { _, _ in .ollama("Jutro piszę raport.") }
            defer { thinking.stop() }
            _ = try await Backend(model: "bielik:11b", server: .init(base: await thinking.baseURL(), api: .ollama), cache: false)
                .correct("Jutro pisze raport.", mode: .fix) { _ in }
            check("Ollama: think: false dla każdego modelu", "\(thinking.requests.first.map(json)?["think"] ?? "brak")", "0")
            // Serwer, który odrzuca `think` dla modelu: to samo zapytanie bez niego, potem już bez niego (zapamiętane).
            let refusing = try HTTPStub { request, _ in
                json(request)["think"] != nil
                    ? HTTPStub.Response(status: 400, body: #"{"error":"\"stary-model\" does not support thinking"}"#)
                    : .ollama("Jutro piszę raport.")
            }
            defer { refusing.stop() }
            let refusingServer = ModelServerEndpoint(base: await refusing.baseURL(), api: .ollama)
            let refused = try await Backend(model: "stary-model", server: refusingServer, cache: false)
                .correct("Jutro pisze raport.", mode: .fix) { _ in }
            _ = try await Backend(model: "stary-model", server: refusingServer, cache: false).correct("Kot ma ale.", mode: .fix) { _ in }
            check("Ollama: odrzucone think — ponowienie bez niego, zapamiętane",
                  "\(refused) · \(refusing.requests.map { json($0)["think"] == nil ? "bez" : "think" }.joined(separator: ","))",
                  "Jutro piszę raport. · think,bez,bez")

            // Zwykły Popraw: obce pismo dwa razy → komunikat zamiast wyniku z dziwnymi znakami.
            let regular = try HTTPStub { _, _ in .ollama("Jutro pいつze raport.") }
            defer { regular.stop() }
            do {
                _ = try await Backend(model: "test-model", server: .init(base: await regular.baseURL(), api: .ollama), cache: false)
                    .correct("Jutro pisze raport.", mode: .fix) { _ in }
                check("Popraw: obce pismo dwa razy", "bez błędu", "błąd")
            } catch {
                check("Popraw: obce pismo dwa razy",
                      "\(error.localizedDescription) · \(regular.requests.count) zapytania · \(errorKind(error))",
                      "Model wstawił znaki z obcego pisma (いつ). Wynik odrzucono — spróbuj ponownie. · 2 zapytania · script_drift")
            }
        } catch {
            check("atrapa Ollamy", "\(error)", "działa")
        }
    }

    private static func speech() async {
        let samples = [Float](repeating: 0.1, count: 16_000)
        do {
            // whisper.cpp: /inference, multipart z WAV i polami; segmenty z \n łączone w jedną linię.
            let whisper = try HTTPStub { request, _ in
                switch request.path {
                case "/health": HTTPStub.Response(body: #"{"status":"ok"}"#)
                case "/inference": HTTPStub.Response(headers: ["Content-Type": "application/json"], body: #"{"text":" Raz.\n Dwa.\n"}"#)
                default: HTTPStub.Response(status: 404, body: "File Not Found")
                }
            }
            defer { whisper.stop() }
            let base = await whisper.baseURL()
            let client = SpeechServerClient(endpoint: SpeechServerEndpoint(address: base.absoluteString, api: .whisperCpp)!,
                                            model: "", key: "")
            let text = try await client.transcribe(samples, language: "pl", prompt: "Mówię po polsku.")
            check("whisper.cpp: jedna linia tekstu", text, "Raz. Dwa.")
            let request = whisper.requests.last
            check("whisper.cpp: ścieżka i metoda", "\(request?.method ?? "") \(request?.path ?? "")", "POST /inference")
            let body = request?.text ?? ""
            check("whisper.cpp: multipart z WAV, językiem i podpowiedzią",
                  [request?.headers["content-type"]?.hasPrefix("multipart/form-data; boundary=") == true,
                   body.contains("filename=\"audio.wav\""), body.contains("RIFF"),
                   body.contains("name=\"language\"\r\n\r\npl\r\n"), body.contains("Mówię po polsku.")].map { $0 ? "1" : "0" }.joined(),
                  "11111")
            check("bez klucza i bez bramy: brak nagłówka Authorization", request?.headers["authorization"] ?? "brak", "brak")
            check("whisper.cpp: stan", stateLabel(await client.check()), "ok: Działa")

            // OpenAI: /v1/audio/transcriptions, model i klucz jako Bearer.
            let openAI = try HTTPStub { _, _ in HTTPStub.Response(body: #"{"text":"Hello there."}"#) }
            defer { openAI.stop() }
            let keyed = SpeechServerClient(endpoint: SpeechServerEndpoint(address: (await openAI.baseURL()).absoluteString, api: .openAI)!,
                                           model: "large-v3", key: "test-key")
            _ = try await keyed.transcribe(samples, language: nil, prompt: "")
            let sent = openAI.requests.last
            check("OpenAI: ścieżka /v1/audio/transcriptions", sent?.path ?? "", "/v1/audio/transcriptions")
            check("OpenAI: klucz jako Bearer", sent?.headers["authorization"] ?? "", "Bearer test-key")
            check("OpenAI: model, bez języka przy auto",
                  "\(sent?.text.contains("name=\"model\"\r\n\r\nlarge-v3") == true) \(sent?.text.contains("name=\"language\"") == true)",
                  "true false")
            let odd = stateLabel(await keyed.check())
            check("OpenAI: odpowiedź 200 bez listy modeli to ostrzeżenie, nie „działa”",
                  odd.hasPrefix("warning: Odpowiada") ? "ostrzeżenie" : odd, "ostrzeżenie")

            // Przekierowanie nie jest wykonywane (nagranie i klucz nie idą pod inny adres).
            let redirect = try HTTPStub { _, _ in HTTPStub.Response(status: 302, headers: ["Location": "https://speech.example.com/v1/audio/transcriptions"]) }
            defer { redirect.stop() }
            let redirected = SpeechServerClient(endpoint: SpeechServerEndpoint(address: (await redirect.baseURL()).absoluteString, api: .openAI)!,
                                                model: "", key: "test-key")
            do {
                _ = try await redirected.transcribe(samples, language: "pl", prompt: "")
                check("przekierowanie: błąd z adresem", "bez błędu", "błąd")
            } catch {
                check("przekierowanie: błąd z adresem, jedno żądanie", "\(SpeechRecognition.errorKind(error)) · \(redirect.requests.count)", "redirect · 1")
            }

            // 413 z proxy: komunikat o limicie, nie „sprawdź adres”.
            let tooLarge = try HTTPStub { _, _ in HTTPStub.Response(status: 413, body: "<html>413</html>") }
            defer { tooLarge.stop() }
            do {
                _ = try await SpeechServerClient(endpoint: SpeechServerEndpoint(address: (await tooLarge.baseURL()).absoluteString, api: .openAI)!,
                                                 model: "", key: "").transcribe(samples, language: "pl", prompt: "")
                check("413: komunikat o limicie", "bez błędu", "błąd")
            } catch {
                check("413: komunikat o limicie", "\(error.localizedDescription.contains("client_max_body_size"))", "true")
            }

            // Zły protokół: wybrano OpenAI, a pod adresem jest whisper.cpp (/v1/models → 404, /health → ok).
            let wrong = SpeechServerClient(endpoint: SpeechServerEndpoint(address: base.absoluteString, api: .openAI)!, model: "", key: "")
            check("zły protokół: podpowiedź whisper.cpp", stateLabel(await wrong.check()), "warning: To serwer whisper.cpp — wybierz protokół „whisper.cpp”.")
        } catch {
            check("atrapa serwera mowy", "\(error)", "działa")
        }
    }

    private static func stateLabel(_ status: ServiceStatus) -> String {
        switch status {
        case .ok(let text): "ok: \(text.components(separatedBy: " · ").first ?? "")"
        case .warning(let text): "warning: \(text)"
        case .error(let text): "error: \(text)"
        }
    }
}
