import AppKit
import Combine
import os
import GramaBackend
import GramaCore
import GramaIMEProtocol
import GramaIPC

/// Kanał tekstu przez wtyczkę przeglądarki (Edge, Chrome — jedna wtyczka, bo jeden silnik). Wtyczka jest tylko
/// interfejsem: na prośbę aplikacji podaje tekst aktywnego pola strony i wykonuje podmiany. Zakres, poprawki
/// słowami i kontrola przed zapisem są tu, te same co dla metody wprowadzania (`ImeScope`, `ImeEdits`),
/// bo prośby i odpowiedzi mają typy `ImeProtocol` — tyle że z pełnym tekstem pola zamiast ±100 znaków.
///
/// Aplikacja jest serwerem gniazda `BrowserLink.socketPath`. Przeglądarka uruchamia `GramaBrowserHost` na czas
/// połączenia wtyczki; host łączy się z nami i trzyma połączenie (jedno na przeglądarkę), więc „połączony”
/// znaczy: ta przeglądarka ma działającą wtyczkę.
/// Połączone przeglądarki dla okna aplikacji (odświeża się samo przy połączeniu i rozłączeniu wtyczki).
@MainActor final class BrowserStatus: ObservableObject {
    static let shared = BrowserStatus()
    /// Bundle ID przeglądarki → wersja wtyczki.
    @Published private(set) var connected: [String: String] = [:]

    func refresh() {
        let now = BrowserChannel.connectedBrowsers()
        if now != connected { connected = now }
    }
}

enum BrowserChannel {
    private final class Connection: @unchecked Sendable {
        let fd: Int32
        let browser: String
        var extensionVersion: String?
        init(fd: Int32, browser: String) {
            self.fd = fd
            self.browser = browser
        }
    }

    private struct State {
        var connections: [String: Connection] = [:]
        var pending: [String: CheckedContinuation<Received, Never>] = [:]
    }

    /// Odpowiedź i jej surowy JSON (pola spoza `ImeProtocol.Reply`: położenie pola i słów, dane o oknie).
    struct Received: @unchecked Sendable {
        let outcome: ImeChannel.Outcome
        let raw: [String: Any]
    }

    private static let state = OSAllocatedUnfairLock(initialState: State())
    nonisolated(unsafe) private static var lockFD: Int32 = -1
    nonisolated(unsafe) private static var sitesSubscription: AnyCancellable?
    nonisolated(unsafe) private static var preferencesSubscription: AnyCancellable?

    // MARK: - Serwer

    /// Start z aplikacji (nie z trybów wiersza poleceń). Druga kopia (np. `--e2e` przez `open -n`) nie przejmie gniazda.
    static func start() {
        guard PeerCheck.ownLeafHash != nil, UnixSocket.prepareDirectory(BrowserLink.directory),
              let lock = SingleInstance.lock(BrowserLink.directory.appendingPathComponent("browser.lock").path),
              let listener = UnixSocket.listen(at: BrowserLink.socketPath) else {
            EventLog.write("browser_channel", ["ok": false])
            return
        }
        lockFD = lock
        // Zmiana listy wyłączonych stron (z wtyczki albo z okna aplikacji) trafia do wszystkich przeglądarek.
        // `@Published` woła przed zapisem — wysyłamy w następnym obiegu pętli, już z nową listą.
        sitesSubscription = Settings.shared.$disabledSites.dropFirst().sink { _ in
            DispatchQueue.main.async { pushSites() }
        }
        // Nowy skrót korekty (okienko wtyczki go pokazuje: „… i dyktowanie działają dalej”) albo język interfejsu.
        preferencesSubscription = Settings.shared.$correctionShortcut.dropFirst().map { _ in () }
            .merge(with: Settings.shared.$textLanguages.dropFirst().map { _ in () })
            .sink { _ in DispatchQueue.main.async { pushPreferences() } }
        Thread.detachNewThread {
            while true {
                let fd = accept(listener, nil, nil)
                if fd < 0 {
                    if errno == EINTR || errno == ECONNABORTED { continue }
                    return
                }
                UnixSocket.noSigPipe(fd)
                UnixSocket.setSendTimeout(fd, 1)
                guard PeerCheck.verify(fd: fd, identifier: BrowserLink.hostIdentifier) else {
                    EventLog.write("browser_peer_rejected", [:])
                    close(fd)
                    continue
                }
                Thread.detachNewThread { serve(fd) }
            }
        }
    }

    /// Jedno połączenie z hostem: najpierw `{"type":"host"}` z przeglądarką, potem odpowiedzi i zdarzenia wtyczki.
    private static func serve(_ fd: Int32) {
        let reader = LineReader(fd: fd, maxBytes: BrowserLink.maxMessageBytes)
        guard let first = reader.next(),
              let hello = (try? JSONSerialization.jsonObject(with: first)) as? [String: Any],
              hello["type"] as? String == "host", let browser = hello["browser"] as? String else {
            close(fd)
            return
        }
        let connection = Connection(fd: fd, browser: browser)
        let previous = state.withLock { state -> Connection? in
            defer { state.connections[browser] = connection }
            return state.connections[browser]
        }
        if let previous { shutdown(previous.fd, SHUT_RDWR) }

        while let line = reader.next() {
            let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
            if object?["type"] as? String == "hello" {
                let version = object?["version"] as? String
                state.withLock { _ in connection.extensionVersion = version }
                EventLog.write("browser_connected", ["browser": browser, "extension": version ?? "?"])
                DispatchQueue.main.async {
                    pushSites()
                    pushPreferences()
                    BrowserStatus.shared.refresh()
                }
                continue
            }
            // Przełącznik „Sprawdzaj na tej stronie” w okienku wtyczki: zapis w ustawieniach aplikacji.
            if object?["type"] as? String == "setSite", let site = object?["site"] as? String,
               let disabled = object?["disabled"] as? Bool {
                // Nową listę rozsyła subskrypcja `disabledSites` (także do tej przeglądarki).
                DispatchQueue.main.async {
                    Settings.shared.setSiteDisabled(site, disabled)
                    EventLog.write("browser_site", ["disabled": disabled])
                }
                continue
            }
            guard var reply = ImeProtocol.decode(ImeProtocol.Reply.self, from: line) else { continue }
            // Wtyczka podaje kursor w układzie ekranu z lewym górnym rogiem (jak okna w CGWindowList).
            if let rect = object?["caretTopLeft"] as? [Double], rect.count == 4 { reply.caret = cocoa(rect) }
            let id = reply.id, received = Received(outcome: .reply(reply), raw: object ?? [:])
            let waiting = state.withLock { $0.pending.removeValue(forKey: id) }
            waiting?.resume(returning: received)
        }
        let removed = state.withLock { state -> Bool in
            guard state.connections[browser] === connection else { return false }
            state.connections[browser] = nil
            return true
        }
        if removed {
            EventLog.write("browser_disconnected", ["browser": browser])
            DispatchQueue.main.async { BrowserStatus.shared.refresh() }
        }
        close(fd)
    }

    private static func cocoa(_ rect: [Double]) -> [Double] {
        // Ekran główny (z paskiem menu) — CG, bo jesteśmy poza głównym wątkiem.
        let primaryHeight = Double(CGDisplayBounds(CGMainDisplayID()).height)
        return [rect[0], primaryHeight - rect[1] - rect[3], rect[2], rect[3]]
    }

    /// Lista wyłączonych stron do wszystkich połączonych wtyczek (znaczek „off” i przełącznik w okienku).
    @MainActor static func pushSites() {
        broadcast(["type": "sites", "disabled": Settings.shared.disabledSites])
    }

    /// Język interfejsu i skrót korekty do okienka wtyczki — ta sama wiadomość `preferences`, którą wysyła host
    /// na Linuksie.
    @MainActor static func pushPreferences() {
        broadcast(["type": "preferences", "ui_language": UI.language.rawValue,
                   "correction_shortcut": Settings.shared.correctionShortcut.label])
    }

    @MainActor private static func broadcast(_ object: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return }
        data.append(0x0A)
        let descriptors = state.withLock { $0.connections.values.map(\.fd) }
        for fd in descriptors { _ = UnixSocket.writeAll(fd, data) }
    }

    // MARK: - Prośby

    /// Czy ta aplikacja (przeglądarka) ma teraz działającą wtyczkę.
    static func isConnected(_ bundleID: String?) -> Bool {
        guard let bundleID else { return false }
        return state.withLock { $0.connections[bundleID]?.extensionVersion != nil }
    }

    static func connectedBrowsers() -> [String: String] {
        state.withLock { state in state.connections.compactMapValues(\.extensionVersion) }
    }

    static func context(_ browser: String) async -> ImeChannel.Outcome {
        var request = ImeProtocol.Request(op: .context)
        request.maxBefore = ImeProtocol.maxWindow
        request.maxAfter = ImeProtocol.maxWindow
        return await send(request, to: browser, timeout: 0.8)
    }

    static func replace(_ browser: String, client: String?, check: ImeProtocol.Edit, edits: [ImeProtocol.Edit],
                        expected: String) async -> ImeChannel.Outcome {
        var request = ImeProtocol.Request(op: .replace)
        request.client = client
        request.check = check
        request.edits = edits
        request.expected = expected
        return await send(request, to: browser, timeout: 2.5, writeDeadline: 1.2)
    }

    static func insert(_ browser: String, text: String, client: String?) async -> ImeChannel.Outcome {
        var request = ImeProtocol.Request(op: .insert)
        request.client = client
        request.text = text
        return await send(request, to: browser, timeout: 2.5, writeDeadline: 1.2)
    }

    /// Sprawdzanie na żywo: tekst pola (jak `context`), widoczna część pola i dane o oknie.
    static func live(_ browser: String) async -> Received {
        await sendRaw(["op": "live", "maxBefore": ImeProtocol.maxWindow, "maxAfter": ImeProtocol.maxWindow],
                      to: browser, timeout: 0.35)
    }

    /// Prostokąty słów (zakresy w tekście pola) w układzie strony, z danymi o oknie do przeliczenia na ekran.
    static func rects(_ browser: String, client: String, ranges: [NSRange]) async -> Received {
        await sendRaw(["op": "rects", "client": client, "ranges": ranges.map { [$0.location, $0.length] }],
                      to: browser, timeout: 0.35)
    }

    /// `writeDeadline` jak w `ImeChannel`: wtyczka rezygnuje z zapisu, zanim przestaniemy czekać na odpowiedź.
    private static func send(_ request: ImeProtocol.Request, to browser: String, timeout: TimeInterval,
                             writeDeadline: TimeInterval? = nil) async -> ImeChannel.Outcome {
        var request = request
        request.deadline = ProcessInfo.processInfo.systemUptime + (writeDeadline ?? timeout)
        guard let data = ImeProtocol.encode(request),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return .unavailable("encode") }
        return await sendRaw(object, to: browser, timeout: timeout).outcome
    }

    /// Prośba jako słownik — także operacje, których nie zna `ImeProtocol.Op` (`live`, `rects`).
    private static func sendRaw(_ object: [String: Any], to browser: String, timeout: TimeInterval) async -> Received {
        var object = object
        let id = object["id"] as? String ?? UUID().uuidString
        object["id"] = id
        object["v"] = ImeProtocol.version
        if object["deadline"] == nil { object["deadline"] = ProcessInfo.processInfo.systemUptime + timeout }
        let failed = { (reason: String) in Received(outcome: .unavailable(reason), raw: [:]) }
        guard let fd = state.withLock({ $0.connections[browser]?.fd }) else { return failed("no_extension") }
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return failed("encode") }
        data.append(0x0A)
        let message = data
        return await withCheckedContinuation { continuation in
            state.withLock { $0.pending[id] = continuation }
            guard UnixSocket.writeAll(fd, message) else {
                state.withLock { $0.pending.removeValue(forKey: id) }?.resume(returning: failed("write_failed"))
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                state.withLock { $0.pending.removeValue(forKey: id) }?.resume(returning: Received(outcome: .noReply, raw: [:]))
            }
        }
    }

    // MARK: - Położenie na ekranie

    /// Przelicza prostokąty strony (px CSS, względem ramki) na ekran w układzie Cocoa. Powiększenie strony =
    /// devicePixelRatio / skala ekranu (zna ją tylko aplikacja). Początek strony na ekranie: z ostatniej pozycji
    /// myszy nad ramką (dokładnie, także z paskiem bocznym i kartami pionowymi), inaczej — tylko w głównej ramce —
    /// z założenia, że pasek narzędzi jest nad stroną, a z lewej nic nie ma. nil: nie da się (ramka bez myszy).
    @MainActor static func screenConverter(_ raw: [String: Any]) -> ((_ rect: [Double]) -> NSRect)? {
        guard let geometry = raw["geometry"] as? [String: Any], let dpr = geometry["dpr"] as? Double,
              let win = geometry["win"] as? [Double], win.count == 6 else { return nil }
        let (screenX, screenY, _, outerHeight, _, innerHeight) = (win[0], win[1], win[2], win[3], win[4], win[5])
        let primaryHeight = NSScreen.screens.first?.frame.maxY ?? 0
        // Ekran okna przeglądarki (Cocoa: y od dołu ekranu głównego).
        let point = NSPoint(x: screenX + 10, y: primaryHeight - screenY - 10)
        let backing = NSScreen.screens.first { $0.frame.contains(point) }?.backingScaleFactor
            ?? NSScreen.main?.backingScaleFactor ?? 2
        let zoom = (dpr / backing * 100).rounded() / 100
        guard zoom > 0.2, zoom < 6 else { return nil }
        let originX: Double, originY: Double
        if let mouse = geometry["mouse"] as? [Double], mouse.count == 4 {
            originX = screenX + mouse[0] - mouse[2] * zoom
            originY = screenY + mouse[1] - mouse[3] * zoom
        } else if geometry["top"] as? Bool == true {
            originX = screenX
            originY = screenY + outerHeight - innerHeight * zoom
        } else {
            return nil
        }
        return { rect in
            let width = rect[2] * zoom, height = rect[3] * zoom
            return NSRect(x: originX + rect[0] * zoom, y: primaryHeight - (originY + rect[1] * zoom) - height,
                          width: width, height: height)
        }
    }

    // MARK: - Rejestracja hosta

    /// Przeglądarki na Chromium: bundle ID → katalog profilu w Application Support.
    static let browsers: [(id: String, directory: String)] = [
        ("com.microsoft.edgemac", "Microsoft Edge"),
        ("com.google.Chrome", "Google/Chrome"),
    ]

    /// Manifest hosta native messaging w każdej zainstalowanej przeglądarce — tylko z zainstalowanej aplikacji
    /// (ścieżka hosta wskazuje ten pakiet) i tylko gdy treść się zmieniła.
    static func installHostManifests() {
        let bundle = Bundle.main.bundleURL
        let host = bundle.appendingPathComponent("Contents/MacOS/\(BrowserLink.hostExecutable)")
        guard bundle.pathExtension == "app", !bundle.path.contains("/build.noindex/"),
              FileManager.default.isExecutableFile(atPath: host.path) else { return }
        let manifest: [String: Any] = [
            "name": BrowserLink.hostName,
            "description": "GramaDrama",
            "path": host.path,
            "type": "stdio",
            "allowed_origins": [BrowserLink.extensionOrigin],
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]) else { return }
        let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        for browser in browsers {
            let profile = support.appendingPathComponent(browser.directory)
            guard FileManager.default.fileExists(atPath: profile.path) else { continue }
            let directory = profile.appendingPathComponent("NativeMessagingHosts")
            let file = directory.appendingPathComponent("\(BrowserLink.hostName).json")
            guard (try? Data(contentsOf: file)) != data else { continue }
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let written = (try? data.write(to: file, options: .atomic)) != nil
            EventLog.write("browser_host_manifest", ["browser": browser.id, "ok": written])
        }
    }
}
