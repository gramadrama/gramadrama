import Darwin
import Foundation

/// Wtyczka przeglądarki (Edge, Chrome): stałe wspólne dla aplikacji i hosta native messaging.
///
/// Droga: wtyczka ⇄ (native messaging: 4 bajty długości + JSON) ⇄ `GramaBrowserHost` ⇄ (gniazdo Unix, linia JSON
/// na wiadomość) ⇄ aplikacja. Host tylko przekłada ramki na linie; prośby wysyła aplikacja (te same typy co
/// metoda wprowadzania: `ImeProtocol.Request`/`Reply`), wtyczka czyta i zmienia pole w stronie.
package enum BrowserLink {
    /// Nazwa hosta w manifeście native messaging (`chrome.runtime.connectNative`).
    package static let hostName = "pl.gramadrama.browser"
    /// Identyfikator podpisu hosta (sprawdzany przez aplikację po drugiej stronie gniazda).
    package static let hostIdentifier = "pl.gramadrama.browser-host"
    package static let hostExecutable = "GramaBrowserHost"
    /// ID wtyczki wynika z klucza publicznego w `browser/extension/manifest.json` (to samo w Edge i Chrome).
    package static let extensionID = "hlnpnkiancoohmafifihcjndfmhilkfe"
    package static var extensionOrigin: String { "chrome-extension://\(extensionID)/" }
    /// Większy niż limit panelu: odpowiedź `context` niesie tekst przed i za kursorem.
    package static let maxMessageBytes = 1024 * 1024

    package static var directory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/GramaDrama/browser", isDirectory: true)
    }
    package static var socketPath: String { directory.appendingPathComponent("browser.sock").path }
}

/// Czytanie linii z gniazda, które zostaje otwarte (w przeciwieństwie do `UnixSocket.readLine` nie gubi
/// bajtów za pierwszym końcem linii).
package final class LineReader {
    private let fd: Int32
    private let maxBytes: Int
    private var buffer = Data()

    package init(fd: Int32, maxBytes: Int) {
        self.fd = fd
        self.maxBytes = maxBytes
    }

    /// Następna linia (bez „\n”). nil: zamknięte połączenie albo linia dłuższa niż limit.
    package func next() -> Data? {
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            if let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<newline]
                buffer.removeSubrange(buffer.startIndex...newline)
                return Data(line)
            }
            guard buffer.count <= maxBytes else { return nil }
            let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            if n < 0, errno == EINTR { continue }
            guard n > 0 else { return nil }
            buffer.append(contentsOf: chunk[0..<n])
        }
    }
}
