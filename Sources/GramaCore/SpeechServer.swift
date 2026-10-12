import Foundation

/// Rozpoznawanie mowy na serwerze (opcja dyktowania): adres, protokół, nagranie jako WAV w multipart i odpowiedź.
/// Ten sam kontrakt co na Linuksie (`linux/gramadrama/speech.py`): API zgodne z OpenAI (`/audio/transcriptions`)
/// albo serwer whisper.cpp (`/inference`, `/health`). Same dane i reguły, bez sieci — wysyła `SpeechServerClient`.
package enum SpeechServerAPI: String, CaseIterable, Identifiable, Sendable {
    case openAI = "openai"
    case whisperCpp = "whispercpp"

    package var id: String { rawValue }
    package var label: String { self == .openAI ? tr("Zgodny z OpenAI") : "whisper.cpp" }
}

/// Adres serwera mowy po sprawdzeniu: http(s), host, bez danych logowania, zapytania i kotwicy.
package struct SpeechServerEndpoint: Equatable, Sendable {
    package let api: SpeechServerAPI
    /// Adres tak, jak go wpisano (bez końcowego „/”).
    package let address: URL
    /// Baza żądań: przy OpenAI z dopisanym `/v1`, gdy adres nie ma ścieżki (`http://host:8080` → `…/v1`).
    package let base: URL

    package init?(address text: String, api: SpeechServerAPI) {
        guard let address = ServerAddress.url(text) else { return nil }
        self.api = api
        self.address = address
        base = api == .openAI && address.path.isEmpty ? address.appendingPathComponent("v1") : address
    }

    /// Powód, dla którego to nie jest adres serwera (do pokazania w ustawieniach), albo nil.
    package static func problem(_ text: String) -> String? {
        ServerAddress.problem(text, empty: tr("Podaj adres serwera mowy."), example: "https://speech.example.com")
    }

    /// Rozpoznanie: OpenAI `POST {base}/audio/transcriptions`, whisper.cpp `POST {adres}/inference`.
    package var transcriptionURL: URL {
        base.appendingPathComponent(api == .openAI ? "audio/transcriptions" : "inference")
    }

    /// Sprawdzenie w ustawieniach: OpenAI `GET {base}/models`, whisper.cpp `GET {adres}/health`.
    package var healthURL: URL {
        base.appendingPathComponent(api == .openAI ? "models" : "health")
    }

    /// Ten sam adres w drugim protokole — gdy pierwszy nie odpowiada, sprawdzamy, czy to nie pomyłka protokołu.
    package var otherProtocol: SpeechServerEndpoint? {
        SpeechServerEndpoint(address: address.absoluteString, api: api == .openAI ? .whisperCpp : .openAI)
    }

    /// Ścieżka serwera mowy na bramie dostępu zdalnego — obok `ollama` i `languagetool`.
    package static let gatewayPath = "whisper"

    /// Ten sam serwer mowy przez bramę dostępu zdalnego (`<adres logowania>/whisper`), w tym samym protokole.
    package static func gateway(_ base: URL, api: SpeechServerAPI) -> SpeechServerEndpoint? {
        SpeechServerEndpoint(address: base.appendingPathComponent(gatewayPath).absoluteString, api: api)
    }

    package var host: String { address.host ?? "" }
    package var isEncrypted: Bool { address.scheme == "https" }
    package var isLocalNetwork: Bool { ServerAddress.isLocalNetwork(host: host) }

    package static func isLocalNetwork(host: String) -> Bool { ServerAddress.isLocalNetwork(host: host) }
}

/// Nagranie dla serwera: 16-bitowy PCM, mono, little-endian, jak plik WAV z dyktafonu.
package enum WAVEncoder {
    package static func encode(_ samples: [Float], sampleRate: Int = 16_000) -> Data {
        let dataBytes = samples.count * 2
        var data = Data(capacity: 44 + dataBytes)
        func append(_ text: String) { data.append(contentsOf: Array(text.utf8)) }
        func append32(_ value: UInt32) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        func append16(_ value: UInt16) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        append("RIFF"); append32(UInt32(36 + dataBytes)); append("WAVE")
        append("fmt "); append32(16); append16(1); append16(1)
        append32(UInt32(sampleRate)); append32(UInt32(sampleRate * 2)); append16(2); append16(16)
        append("data"); append32(UInt32(dataBytes))
        var pcm = [Int16](repeating: 0, count: samples.count)
        for (index, sample) in samples.enumerated() {
            // Przesterowanie obcinamy, zamiast zawijać (Float spoza -1…1 dawałby trzaski).
            let clamped = sample.isFinite ? min(max(sample, -1), 1) : 0
            pcm[index] = Int16((clamped * 32767).rounded()).littleEndian
        }
        pcm.withUnsafeBytes { data.append(contentsOf: $0) }
        return data
    }
}

/// Treść żądania `multipart/form-data`: pola tekstowe i jeden plik.
package enum Multipart {
    package static func body(fields: [(String, String)], file: (field: String, name: String, type: String, data: Data),
                             boundary: String) -> Data {
        var body = Data()
        func append(_ text: String) { body.append(contentsOf: Array(text.utf8)) }
        for (name, value) in fields {
            append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n")
        }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(file.field)\"; filename=\"\(file.name)\"\r\n")
        append("Content-Type: \(file.type)\r\n\r\n")
        body.append(file.data)
        append("\r\n--\(boundary)--\r\n")
        return body
    }
}

package enum SpeechServer {
    /// Pola żądania. OpenAI: model, język (bez niego serwer rozpoznaje sam) i podpowiedź; whisper.cpp: jeden
    /// zachłanny przebieg, język albo „auto” i podpowiedź — model wybiera konfiguracja serwera.
    package static func fields(api: SpeechServerAPI, model: String, language: String?, prompt: String) -> [(String, String)] {
        var fields = [("response_format", "json"), ("temperature", "0")]
        switch api {
        case .openAI:
            fields.append(("model", model.trimmed.isEmpty ? "whisper-1" : model.trimmed))
            if let language { fields.append(("language", language)) }
            if !prompt.isEmpty { fields.append(("prompt", prompt)) }
        case .whisperCpp:
            fields += [("temperature_inc", "0"), ("best_of", "1"), ("beam_size", "1"), ("translate", "false"),
                       ("language", language ?? "auto"), ("prompt", prompt)]
        }
        return fields
    }

    /// Tekst z odpowiedzi: JSON `{"text": …}` albo sam tekst. whisper.cpp kończy każdy segment znakiem nowej
    /// linii — dyktowanie to jeden ciąg tekstu, więc wszystkie białe znaki zwijamy do pojedynczych spacji
    /// (tak samo łączy segmenty WhisperKit na Macu). nil = odpowiedź, która nie jest transkrypcją.
    package static func transcript(from data: Data) -> String? {
        let text: String
        if let json = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) {
            guard let object = json as? [String: Any], let value = object["text"] as? String else { return nil }
            text = value
        } else {
            guard let plain = String(data: data, encoding: .utf8), !["{", "["].contains(plain.trimmed.first) else { return nil }
            text = plain
        }
        return text.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
    }

    /// Błąd HTTP serwera mowy po polsku; treść odpowiedzi tylko z pola „error”/„message” i skrócona.
    package static func describe(status: Int, body: Data) -> String {
        let detail: String = {
            guard let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return "" }
            let error = json["error"]
            let message = (error as? [String: Any])?["message"] as? String ?? error as? String ?? json["message"] as? String ?? ""
            return message.isEmpty ? "" : " — \(message.prefix(160))"
        }()
        switch status {
        case 401, 403: return tr("Serwer mowy odrzucił dostęp (HTTP {status}) — sprawdź klucz API.", ["status": status]) + detail
        case 404, 405: return tr("Serwer mowy nie ma tej ścieżki (HTTP {status}) — sprawdź adres i protokół.", ["status": status]) + detail
        case 413: return tr("Nagranie jest za duże dla serwera albo jego proxy (HTTP 413) — zwiększ limit (np. client_max_body_size w nginx) albo dyktuj krócej.")
        case 300..<400: return tr("Serwer mowy przekierowuje (HTTP {status}) — wpisz w ustawieniach adres docelowy.", ["status": status])
        default: return tr("Serwer mowy zwrócił błąd HTTP {status}.", ["status": status]) + detail
        }
    }
}
