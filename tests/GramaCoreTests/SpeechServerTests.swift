import Foundation
import Testing
@testable import GramaCore

@Suite("Serwer mowy: adres")
struct SpeechServerEndpointTests {
    @Test func openAIAddsV1OnlyWithoutAPath() throws {
        let plain = try #require(SpeechServerEndpoint(address: " http://192.0.2.10:8080/ ", api: .openAI))
        #expect(plain.transcriptionURL.absoluteString == "http://192.0.2.10:8080/v1/audio/transcriptions")
        #expect(plain.healthURL.absoluteString == "http://192.0.2.10:8080/v1/models")
        let custom = try #require(SpeechServerEndpoint(address: "https://speech.example.com/whisper/", api: .openAI))
        #expect(custom.transcriptionURL.absoluteString == "https://speech.example.com/whisper/audio/transcriptions")
    }

    @Test func whisperCppUsesInferenceAndHealth() throws {
        let endpoint = try #require(SpeechServerEndpoint(address: "http://192.0.2.10:8080", api: .whisperCpp))
        #expect(endpoint.transcriptionURL.absoluteString == "http://192.0.2.10:8080/inference")
        #expect(endpoint.healthURL.absoluteString == "http://192.0.2.10:8080/health")
        #expect(endpoint.otherProtocol?.healthURL.absoluteString == "http://192.0.2.10:8080/v1/models")
        let openAI = try #require(SpeechServerEndpoint(address: "http://192.0.2.10:8080", api: .openAI))
        #expect(openAI.otherProtocol?.healthURL.absoluteString == "http://192.0.2.10:8080/health")
    }

    /// Poza domem ten sam serwer przez bramę dostępu zdalnego, w tym samym protokole.
    @Test func gatewayPathKeepsTheProtocol() throws {
        let base = try #require(URL(string: "https://id.example.com"))
        #expect(SpeechServerEndpoint.gateway(base, api: .whisperCpp)?.transcriptionURL.absoluteString
                == "https://id.example.com/whisper/inference")
        #expect(SpeechServerEndpoint.gateway(base, api: .whisperCpp)?.healthURL.absoluteString == "https://id.example.com/whisper/health")
        #expect(SpeechServerEndpoint.gateway(base, api: .openAI)?.transcriptionURL.absoluteString
                == "https://id.example.com/whisper/audio/transcriptions")
        #expect(SpeechServerEndpoint.gateway(base, api: .whisperCpp)?.isLocalNetwork == false)
    }

    @Test func refusesWhatIsNotAServerAddress() {
        for address in ["", "ftp://speech.example.com", "speech.example.com", "http://user:secret@speech.example.com",
                        "https://speech.example.com/?model=x", "https://speech.example.com/#x"] {
            #expect(SpeechServerEndpoint.problem(address) != nil, "\(address)")
            #expect(SpeechServerEndpoint(address: address, api: .openAI) == nil, "\(address)")
        }
        #expect(SpeechServerEndpoint.problem("https://speech.example.com:8443/asr") == nil)
    }

    @Test func localNetworkHosts() {
        for host in ["localhost", "127.0.0.1", "::1", "[::1]", "nas",
                     "192.168.1.20", "10.1.2.3", "172.16.0.5", "100.64.1.1", "fe80::1", "fd00::1", // privacy: allow[private-ip] synthetic private addresses for LAN detection tests
                     "printer.local", "nas.home.arpa"] { // privacy: allow[local-host] synthetic local names for LAN detection tests
            #expect(SpeechServerEndpoint.isLocalNetwork(host: host), "\(host)")
        }
        for host in ["speech.example.com", "192.0.2.10", "198.51.100.7", "172.32.0.1", "100.128.0.1", "2001:db8::1"] {
            #expect(!SpeechServerEndpoint.isLocalNetwork(host: host), "\(host)")
        }
    }
}

@Suite("Serwer mowy: żądanie i odpowiedź")
struct SpeechServerPayloadTests {
    @Test func wavHeaderAndClampedSamples() {
        let data = WAVEncoder.encode([0, 1, -1, 0.5, 2, -2, .nan])
        #expect(data.count == 44 + 14)
        func text(_ range: Range<Int>) -> String { String(decoding: data[range], as: UTF8.self) }
        func uint32(_ offset: Int) -> UInt32 { data[offset..<offset + 4].reversed().reduce(0) { $0 << 8 | UInt32($1) } }
        func int16(_ offset: Int) -> Int16 { Int16(bitPattern: UInt16(data[offset]) | UInt16(data[offset + 1]) << 8) }
        #expect(text(0..<4) == "RIFF" && text(8..<12) == "WAVE" && text(12..<16) == "fmt " && text(36..<40) == "data")
        #expect(uint32(4) == 36 + 14 && uint32(24) == 16_000 && uint32(28) == 32_000 && uint32(40) == 14)
        #expect((0..<7).map { int16(44 + $0 * 2) } == [0, 32767, -32767, 16384, 32767, -32767, 0])
    }

    @Test func multipartBody() {
        let body = Multipart.body(fields: [("model", "whisper-1")], file: ("file", "audio.wav", "audio/wav", Data("RIFF".utf8)),
                                  boundary: "B")
        #expect(String(decoding: body, as: UTF8.self) == "--B\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
                + "--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\nRIFF\r\n--B--\r\n")
    }

    @Test func fieldsPerProtocol() {
        let openAI = SpeechServer.fields(api: .openAI, model: " ", language: nil, prompt: "")
        #expect(openAI.map(\.0) == ["response_format", "temperature", "model"])
        #expect(openAI.last?.1 == "whisper-1")
        let polish = SpeechServer.fields(api: .openAI, model: "large-v3", language: "pl", prompt: "Słowa: deploy.")
        #expect(polish.map(\.0) == ["response_format", "temperature", "model", "language", "prompt"])
        let whisperCpp = Dictionary(uniqueKeysWithValues: SpeechServer.fields(api: .whisperCpp, model: "x", language: nil, prompt: ""))
        #expect(whisperCpp["language"] == "auto" && whisperCpp["model"] == nil && whisperCpp["prompt"] == "")
    }

    /// whisper.cpp kończy każdy segment nową linią — do pola trafia jedna linia tekstu.
    @Test func transcriptIsOneLine() {
        #expect(SpeechServer.transcript(from: Data(#"{"text":" Pierwsze zdanie.\n Drugie zdanie.\n"}"#.utf8)) == "Pierwsze zdanie. Drugie zdanie.")
        #expect(SpeechServer.transcript(from: Data("Ala ma kota\r\n".utf8)) == "Ala ma kota")
        #expect(SpeechServer.transcript(from: Data(#"{"text":""}"#.utf8)) == "")
        #expect(SpeechServer.transcript(from: Data(#"{"error":"busy"}"#.utf8)) == nil)
        #expect(SpeechServer.transcript(from: Data("[1, 2]".utf8)) == nil)
        #expect(SpeechServer.transcript(from: Data("{zepsuty".utf8)) == nil)
    }

    @Test func httpErrorsSayWhatToDo() {
        #expect(SpeechServer.describe(status: 413, body: Data()).contains("client_max_body_size"))
        #expect(SpeechServer.describe(status: 401, body: Data()).contains("klucz"))
        #expect(SpeechServer.describe(status: 500, body: Data(#"{"error":{"message":"model not loaded"}}"#.utf8)).contains("model not loaded"))
    }
}
