import Foundation
import os
import GramaIMEProtocol
import GramaIPC

/// Log systemowy IME — tylko kody i liczby, nigdy tekst (`/usr/bin/log show --predicate 'subsystem == "pl.gramadrama.inputmethod"'`).
let imeLog = Logger(subsystem: "pl.gramadrama.inputmethod", category: "ime")

/// Serwer gniazda na własnym wątku. Główny wątek IME obsługuje klawisze wszystkich aplikacji, więc nie może
/// czekać na gniazdo: tu przyjmujemy połączenie, sprawdzamy podpis rozmówcy, czytamy prośbę, przekazujemy ją
/// na główny wątek i czekamy na wynik (tutaj, nie tam). Jedna prośba naraz — kolejne czekają w kolejce gniazda.
enum Server {
    static let startedAt = ProcessInfo.processInfo.systemUptime
    private(set) static var requestCount = 0
    private static var lockFD: Int32 = -1

    static func start() {
        let thread = Thread { run() }
        thread.name = "gramadrama.ime.socket"
        thread.qualityOfService = .userInitiated
        thread.start()
    }

    private static func run() {
        guard UnixSocket.prepareDirectory(ImeProtocol.directory) else {
            imeLog.error("socket_dir_failed")
            return
        }
        // Jedna kopia: nowsza (np. uruchomiona przez system, gdy starsza się zawiesiła) czeka, aż starsza zniknie.
        var waited = false
        while true {
            if let fd = SingleInstance.lock(ImeProtocol.lockPath) { lockFD = fd; break }
            if !waited { imeLog.notice("waiting_for_lock"); waited = true }
            sleep(2)
        }
        guard let listener = UnixSocket.listen(at: ImeProtocol.socketPath) else {
            imeLog.error("listen_failed errno=\(errno, privacy: .public)")
            return
        }
        imeLog.notice("listening")
        while true {
            let connection = accept(listener, nil, nil)
            if connection < 0 {
                if errno != EINTR { usleep(100_000) }
                continue
            }
            handle(connection)
            close(connection)
        }
    }

    private static func handle(_ fd: Int32) {
        UnixSocket.noSigPipe(fd)
        UnixSocket.setSendTimeout(fd, 1)
        guard PeerCheck.verify(fd: fd, identifier: ImeProtocol.appBundleID) else {
            imeLog.notice("peer_rejected")
            return
        }
        guard let line = UnixSocket.readLine(fd, maxBytes: ImeProtocol.maxMessageBytes,
                                             deadline: ProcessInfo.processInfo.systemUptime + 1) else { return }
        requestCount += 1
        let reply: ImeProtocol.Reply
        if let request = ImeProtocol.decode(ImeProtocol.Request.self, from: line), request.v == ImeProtocol.version {
            reply = perform(request)
        } else {
            var refused = ImeProtocol.Reply(id: "", ok: false, reason: "bad_request")
            refused.attempted = false
            reply = refused
        }
        if let data = ImeProtocol.encode(reply) { _ = UnixSocket.writeAll(fd, data) }
        imeLog.debug("op done ok=\(reply.ok, privacy: .public) reason=\(reply.reason ?? "-", privacy: .public)")
    }

    private static func perform(_ request: ImeProtocol.Request) -> ImeProtocol.Reply {
        let done = DispatchSemaphore(value: 0)
        let box = ReplyBox()
        DispatchQueue.main.async {
            Ops.run(request) { reply in
                box.set(reply)
                done.signal()
            }
        }
        // Najdłuższa prośba: ~0,3 s czekania na pole + ~0,65 s sprawdzania po zapisie.
        if done.wait(timeout: .now() + 3) == .timedOut {
            imeLog.error("op_timeout op=\(request.op.rawValue, privacy: .public)")
            var reply = ImeProtocol.Reply(id: request.id, ok: false, reason: "timeout")
            // Nie wiemy, czy zapis się zaczął — aplikacja potraktuje to jako „nie wiadomo”.
            reply.attempted = request.op == .replace || request.op == .insert ? nil : false
            return reply
        }
        return box.get() ?? ImeProtocol.Reply(id: request.id, ok: false, reason: "timeout")
    }
}

/// Wynik z głównego wątku do wątku gniazda.
private final class ReplyBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: ImeProtocol.Reply?
    func set(_ reply: ImeProtocol.Reply) { lock.lock(); if value == nil { value = reply }; lock.unlock() }
    func get() -> ImeProtocol.Reply? { lock.lock(); defer { lock.unlock() }; return value }
}
