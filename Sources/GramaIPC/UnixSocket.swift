import Darwin
import Foundation

/// Minimalne gniazdo Unix: jedna linia prośby, jedna linia odpowiedzi. Bez SIGPIPE (zamknięty rozmówca
/// nie może zabić procesu — każde wyjście IME macOS liczy jako awarię), z limitem rozmiaru i czasu.
package enum UnixSocket {
    /// Długość `sun_path` w macOS.
    static let maxPathBytes = 104

    /// Katalog na gniazdo: tworzy go z uprawnieniami 0700 i sprawdza, że to katalog (nie dowiązanie),
    /// należący do tego użytkownika.
    package static func prepareDirectory(_ url: URL) -> Bool {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if mkdir(url.path, 0o700) != 0, errno != EEXIST { return false }
        var info = stat()
        guard lstat(url.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid() else { return false }
        return chmod(url.path, 0o700) == 0
    }

    /// Serwer: nowe gniazdo pod `path` (stare usuwa — wołaj tylko z blokadą jednej kopii), uprawnienia 0600.
    package static func listen(at path: String) -> Int32? {
        guard path.utf8.count < maxPathBytes else { return nil }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        noSigPipe(fd)
        unlink(path)
        var address = makeAddress(path)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0, chmod(path, 0o600) == 0, Darwin.listen(fd, 8) == 0 else {
            close(fd)
            return nil
        }
        return fd
    }

    /// Klient: połączenie z `path` albo nil (brak serwera — IME nie działa).
    package static func connect(to path: String) -> Int32? {
        guard path.utf8.count < maxPathBytes else { return nil }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        noSigPipe(fd)
        var address = makeAddress(path)
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard connected == 0 else {
            close(fd)
            return nil
        }
        return fd
    }

    package static func noSigPipe(_ fd: Int32) {
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
    }

    /// Limit czasu pojedynczego zapisu (odczyt ma własny termin w `readLine`).
    package static func setSendTimeout(_ fd: Int32, _ seconds: TimeInterval) {
        var value = timeval(tv_sec: Int(seconds), tv_usec: Int32((seconds - Double(Int(seconds))) * 1_000_000))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &value, socklen_t(MemoryLayout<timeval>.size))
    }

    package static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return true }
            var sent = 0
            while sent < raw.count {
                let n = write(fd, base + sent, raw.count - sent)
                if n > 0 { sent += n; continue }
                if n < 0, errno == EINTR { continue }
                return false
            }
            return true
        }
    }

    /// Czyta do końca linii (bez samego „\n”), najwyżej `maxBytes`, najdłużej do `deadline`
    /// (`ProcessInfo.systemUptime`). nil: przekroczony czas albo rozmiar, zamknięte połączenie.
    package static func readLine(_ fd: Int32, maxBytes: Int, deadline: TimeInterval) -> Data? {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 16 * 1024)
        while true {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { return nil }
            var descriptor = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&descriptor, 1, Int32(max(1, min(remaining * 1000, 60_000))))
            if ready < 0, errno == EINTR { continue }
            guard ready > 0 else { return nil }
            let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            if n < 0, errno == EINTR { continue }
            guard n > 0 else { return nil }
            if let newline = chunk[0..<n].firstIndex(of: 0x0A) {
                buffer.append(contentsOf: chunk[0..<newline])
                return buffer.count <= maxBytes ? buffer : nil
            }
            buffer.append(contentsOf: chunk[0..<n])
            guard buffer.count <= maxBytes else { return nil }
        }
    }

    private static func makeAddress(_ path: String) -> sockaddr_un {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        withUnsafeMutableBytes(of: &address.sun_path) { target in
            for (index, byte) in bytes.prefix(target.count - 1).enumerated() { target[index] = byte }
            target[min(bytes.count, target.count - 1)] = 0
        }
        return address
    }
}

/// Jedna kopia procesu: blokada `flock` na pliku, trzymana do końca życia procesu.
package enum SingleInstance {
    /// Deskryptor z blokadą (trzymaj go otwartego) albo nil, gdy blokadę ma inny proces.
    package static func lock(_ path: String) -> Int32? {
        let fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return nil }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            return nil
        }
        return fd
    }
}
