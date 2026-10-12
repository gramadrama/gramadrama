import Foundation
import Testing
@testable import GramaCore

@Suite("Usługi: adresy")
struct ServiceAddressTests {
    @Test func ollamaKeepsTheAddressAndOpenAIAddsV1OnlyWithoutAPath() throws {
        let ollama = try #require(ModelServerEndpoint(address: " http://192.0.2.10:11434/ ", api: .ollama))
        #expect(ollama.chatURL.absoluteString == "http://192.0.2.10:11434/api/chat")
        #expect(ollama.statusURL.absoluteString == "http://192.0.2.10:11434/api/version")
        #expect(ollama.modelsURL.absoluteString == "http://192.0.2.10:11434/api/tags")
        let openAI = try #require(ModelServerEndpoint(address: "http://192.0.2.10:8080", api: .openAI))
        #expect(openAI.chatURL.absoluteString == "http://192.0.2.10:8080/v1/chat/completions")
        #expect(openAI.statusURL.absoluteString == "http://192.0.2.10:8080/v1/models")
        let custom = try #require(ModelServerEndpoint(address: "HTTPS://llm.example.com/openai/v1/", api: .openAI))
        #expect(custom.chatURL.absoluteString == "https://llm.example.com/openai/v1/chat/completions")
        #expect(custom.isEncrypted && !custom.isLocalNetwork)
    }

    /// „Sprawdź” podpowiada protokół: Ollama wpisana jako OpenAI (i odwrotnie) — ten sam host i port.
    @Test func theOtherProtocolOfTheSameServer() throws {
        let ollama = try #require(ModelServerEndpoint(address: "http://192.0.2.10:8080", api: .ollama))
        #expect(ollama.asOpenAI?.statusURL.absoluteString == "http://192.0.2.10:8080/v1/models")
        let openAI = try #require(ModelServerEndpoint(address: "http://192.0.2.10:11434", api: .openAI))
        #expect(openAI.ollamaAtOrigin?.statusURL.absoluteString == "http://192.0.2.10:11434/api/version")
    }

    @Test func localAndGatewayAddresses() throws {
        #expect(ModelServerEndpoint.local.chatURL.absoluteString == "http://127.0.0.1:11434/api/chat")
        let base = try #require(URL(string: "https://id.example.com"))
        #expect(ModelServerEndpoint.gateway(base).chatURL.absoluteString == "https://id.example.com/ollama/api/chat")
        #expect(LanguageToolEndpoint.gateway(base).checkURL.absoluteString == "https://id.example.com/languagetool/v2/check")
        #expect(LanguageToolEndpoint.localPorts.map { LanguageToolEndpoint.local(port: $0).languagesURL.absoluteString }
                == ["http://127.0.0.1:8010/v2/languages", "http://127.0.0.1:8081/v2/languages"])
        let languageTool = try #require(LanguageToolEndpoint(address: "http://192.0.2.10:8010/"))
        #expect(languageTool.checkURL.absoluteString == "http://192.0.2.10:8010/v2/check")
    }

    @Test func refusesWhatIsNotAServerAddress() {
        for address in ["", "  ", "192.0.2.10:11434", "ftp://llm.example.com", "http://user:secret@llm.example.com",
                        "https://llm.example.com/?key=x", "https://llm.example.com/#x"] {
            #expect(ModelServerEndpoint.problem(address) != nil, "\(address)")
            #expect(ModelServerEndpoint(address: address, api: .ollama) == nil, "\(address)")
            #expect(LanguageToolEndpoint(address: address) == nil, "\(address)")
        }
        #expect(ModelServerEndpoint.problem("") == "Podaj adres serwera modelu.")
        #expect(LanguageToolEndpoint.problem("") == "Podaj adres serwera LanguageTool.")
        #expect(ModelServerEndpoint.problem("https://llm.example.com:8443/v1") == nil)
    }

    /// Ten Mac nigdy nie idzie przez bramę; sieć lokalna (bez proxy) — te same końcówki nazw co na Linuksie.
    @Test func thisMacAndTheLocalNetwork() {
        for host in ["localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "app.localhost"] {
            #expect(ServerAddress.isLoopback(host: host), "\(host)")
            #expect(ServerAddress.isLocalNetwork(host: host), "\(host)")
        }
        for host in ["nas", "192.168.1.20", "printer.local", "nas.home", "nas.localdomain", "wiki.intranet"] { // privacy: allow[private-ip,local-host] synthetic LAN hosts for route tests
            #expect(ServerAddress.isLocalNetwork(host: host) && !ServerAddress.isLoopback(host: host), "\(host)")
        }
        for host in ["llm.example.com", "192.0.2.10", "127.example.com"] {
            #expect(!ServerAddress.isLocalNetwork(host: host) && !ServerAddress.isLoopback(host: host), "\(host)")
        }
    }

    @Test func sameOriginComparesSchemeHostAndPort() {
        let gateway = URL(string: "https://id.example.com")!
        #expect(ServerAddress.sameOrigin(URL(string: "https://ID.example.com:443/ollama")!, gateway))
        #expect(!ServerAddress.sameOrigin(URL(string: "http://id.example.com/ollama")!, gateway))
        #expect(!ServerAddress.sameOrigin(URL(string: "https://id.example.com:8443")!, gateway))
    }

    @Test func modelNamesFromBothLists() {
        #expect(ModelServerEndpoint.modelNames(Data(#"{"models":[{"name":"b:latest"},{"name":"a:1b"}]}"#.utf8)) == ["a:1b", "b:latest"])
        #expect(ModelServerEndpoint.modelNames(Data(#"{"object":"list","data":[{"id":"korektor"}]}"#.utf8)) == ["korektor"])
        #expect(ModelServerEndpoint.modelNames(Data(#"{"version":"0.12.0"}"#.utf8)) == nil)
        #expect(ModelServerEndpoint.modelNames(Data("<html>".utf8)) == nil)
    }
}

@Suite("Usługi: ustawienia sprzed wyboru „Ten Mac / Serwer”")
struct LegacyServerHostTests {
    @Test func emptyMeansThisMac() {
        for host in ["", "   ", "http://"] {
            #expect(LegacyServerHost.migrate(host) == .init(modelLocation: .local, modelServerURL: "",
                                                           languageToolLocation: .local, languageToolServerURL: ""), "\(host)")
        }
    }

    /// Bez adresu, ale z bramą — dotąd wszystko szło przez bramę; zostaje serwer bez adresu (sama brama).
    @Test func emptyWithAGatewayStaysOnTheGateway() {
        #expect(LegacyServerHost.migrate("", gatewayConfigured: true) == .init(modelLocation: .server, modelServerURL: "",
                                                                              languageToolLocation: .server, languageToolServerURL: ""))
        #expect(LegacyServerHost.migrate("server.example.com", gatewayConfigured: true).modelServerURL == "http://server.example.com:11434")
    }

    @Test func aServerKeepsItsPorts() {
        for host in ["server.example.com", "http://server.example.com:11434/", " server.example.com:8010 "] {
            #expect(LegacyServerHost.migrate(host) == .init(modelLocation: .server, modelServerURL: "http://server.example.com:11434",
                                                           languageToolLocation: .server,
                                                           languageToolServerURL: "http://server.example.com:8010"), "\(host)")
        }
        #expect(LegacyServerHost.migrate("2001:db8::5").modelServerURL == "http://[2001:db8::5]:11434")
    }

    /// Ten Mac zapisany jako serwer — teraz „Ten Mac”, a adresy zostają w formularzu serwera.
    @Test func loopbackBecomesThisMac() {
        let migrated = LegacyServerHost.migrate("127.0.0.1")
        #expect(migrated.modelLocation == .local && migrated.languageToolLocation == .local)
        #expect(migrated.modelServerURL == "http://127.0.0.1:11434" && migrated.languageToolServerURL == "http://127.0.0.1:8010")
        #expect(LegacyServerHost.migrate("::1").modelServerURL == "http://[::1]:11434")
        #expect(LegacyServerHost.migrate("localhost").modelLocation == .local)
    }
}

@Suite("Usługi: API zgodne z OpenAI")
struct OpenAIChatTests {
    @Test func bodyWithTheAdaptedLimitAndTemperature() {
        var options = OpenAIChat.Options()
        var body = OpenAIChat.body(model: "m", messages: [["role": "user", "content": "x"]], temperature: 0, limit: 300, options: options)
        #expect(body["max_tokens"] as? Int == 300 && body["temperature"] as? Double == 0 && body["stream"] as? Bool == true)
        let limit = options.adapt(to: "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")
        let temperature = options.adapt(to: "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.")
        #expect(limit && temperature)
        body = OpenAIChat.body(model: "m", messages: [], temperature: 0, limit: 300, options: options)
        #expect(body["max_tokens"] == nil && body["max_completion_tokens"] as? Int == 300 && body["temperature"] == nil)
        // Każda zmiana najwyżej raz, a inny błąd 400 to błąd.
        let again = [options.adapt(to: "Use 'max_completion_tokens' instead."), options.adapt(to: "Unsupported value: 'temperature'")]
        var fresh = OpenAIChat.Options()
        let other = fresh.adapt(to: "model 'x' not found")
        #expect(again == [false, false] && !other)
    }

    @Test func streamLines() {
        #expect(OpenAIChat.parse(": keep-alive") == .skip)
        #expect(OpenAIChat.parse("") == .skip)
        #expect(OpenAIChat.parse("event: message") == .skip)
        #expect(OpenAIChat.parse("data: [DONE]") == .done)
        #expect(OpenAIChat.parse(#"data: {"choices":[{"delta":{"content":"Kot "}}]}"#)
                == .chunk(content: "Kot ", finishReason: nil, tokensPerSecond: nil))
        #expect(OpenAIChat.parse(#"data:{"choices":[{"delta":{},"finish_reason":"length"}]}"#)
                == .chunk(content: "", finishReason: "length", tokensPerSecond: nil))
        #expect(OpenAIChat.parse(#"data: {"choices":[{"delta":{"content":"."},"finish_reason":"stop"}],"timings":{"predicted_per_second":41.5}}"#)
                == .chunk(content: ".", finishReason: "stop", tokensPerSecond: 41.5))
        #expect(OpenAIChat.parse(#"data: {"error":{"message":"out of memory"}}"#) == .error("out of memory"))
        #expect(OpenAIChat.parse(#"data: {"error":"busy"}"#) == .error("busy"))
        #expect(OpenAIChat.parse(#"data: {"choices":[{"delta":{"content":"x"}}],"error":null}"#)
                == .chunk(content: "x", finishReason: nil, tokensPerSecond: nil))
        #expect(OpenAIChat.parse("data: {nie json") == .malformed)
    }
}

@Suite("Usługi: pobieranie modelu przez Ollamę")
struct OllamaPullTests {
    @Test func progressSumsTheLayers() {
        var progress = OllamaPullProgress()
        #expect(progress.fraction == nil)
        for line in [#"{"status":"pulling manifest"}"#,
                     #"{"status":"pulling aaa","digest":"sha256:aaa","total":300}"#,
                     #"{"status":"pulling aaa","digest":"sha256:aaa","total":300,"completed":150}"#,
                     #"{"status":"pulling bbb","digest":"sha256:bbb","total":100,"completed":100}"#] {
            #expect(progress.apply(line) == nil)
        }
        #expect(progress.total == 400 && progress.completed == 250 && progress.fraction == 0.625)
        #expect(progress.status == "pulling bbb" && !progress.finished)
        _ = progress.apply(#"{"status":"verifying sha256 digest"}"#)
        _ = progress.apply(#"{"status":"success"}"#)
        #expect(progress.finished && progress.fraction == 0.625)
    }

    @Test func errorLine() {
        var progress = OllamaPullProgress()
        #expect(progress.apply(#"{"error":"pull model manifest: file does not exist"}"#) == "pull model manifest: file does not exist")
        #expect(progress.apply("nie json") == nil)
        #expect(!progress.finished)
    }
}

@Suite("Usługi: adres na tym Macu")
struct LocalAddressTests {
    @Test func emptyIsTheDefaultAndOnlyThisMacIsAllowed() {
        #expect(ServerAddress.localProblem("  ", example: "http://127.0.0.1:11434") == nil)
        #expect(ServerAddress.localProblem("http://127.0.0.1:11500", example: "") == nil)
        #expect(ServerAddress.localProblem("http://localhost:8081/", example: "") == nil)
        #expect(ServerAddress.localProblem("http://[::1]:8010", example: "") == nil)
        #expect(ServerAddress.localProblem("http://192.0.2.10:11434", example: "http://127.0.0.1:11434")
                == "Podaj adres na tym Macu, np. http://127.0.0.1:11434.")
        #expect(ServerAddress.localProblem("127.0.0.1:11434", example: "http://127.0.0.1:11434") != nil)
    }

    @Test func endpointsOfThisMac() {
        #expect(ModelServerEndpoint.local(address: "") == .local)
        #expect(ModelServerEndpoint.local(address: "http://192.0.2.10:11434") == .local)
        #expect(ModelServerEndpoint.local(address: "http://127.0.0.1:11500/").chatURL.absoluteString == "http://127.0.0.1:11500/api/chat")
        #expect(LanguageToolEndpoint.local(address: "") == nil)
        #expect(LanguageToolEndpoint.local(address: "http://192.0.2.10:8010") == nil)
        #expect(LanguageToolEndpoint.local(address: "http://localhost:8081")?.checkURL.absoluteString == "http://localhost:8081/v2/check")
    }
}
