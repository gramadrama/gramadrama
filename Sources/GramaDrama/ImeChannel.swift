import AppKit
import Carbon
import GramaBackend
import GramaIMEProtocol
import GramaIPC

/// Kanał tekstu przez metodę wprowadzania „Polski (GramaDrama)”: odczyt pola, podmiana w miejscu i wstawianie
/// dyktowania bez AX i bez schowka. Używany tylko wtedy, gdy to źródło klawiatury jest wybrane, a aplikacja
/// jest w `ImePolicy`; wszystko inne idzie dotychczasową drogą.
///
/// Zasada przejścia na zwykłą drogę: wolno, dopóki IME na pewno niczego nie zmieniła (`preWriteReasons`,
/// brak połączenia). Gdy prośba zmieniająca tekst mogła już zadziałać, wynik to „nie wiadomo” — nigdy drugi zapis.
enum ImeChannel {
    enum Outcome {
        case reply(ImeProtocol.Reply)
        /// Prośba nie wyszła (brak IME, obcy proces na gnieździe) — w polu na pewno nic się nie zmieniło.
        case unavailable(String)
        /// Prośba wyszła, odpowiedzi brak — IME mogła ją wykonać.
        case noReply
    }

    enum WriteResult {
        case landed
        /// IME niczego nie zmieniła; powód do logu.
        case refused(String)
        /// Zmiana mogła się odbyć (albo odbyć później) — tylko schowek i komunikat.
        case unknown(String)
    }

    private static let queue = DispatchQueue(label: "gramadrama.ime.channel", qos: .userInitiated)

    // MARK: - Czy używać

    /// Czy wybrane źródło klawiatury to GramaDrama (TIS — główny wątek).
    @MainActor static func isSourceSelected() -> Bool {
        guard let source = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue() else { return false }
        return ImeSetup.identifier(of: source) == ImeProtocol.inputSourceID
    }

    /// Reguła dla aplikacji albo nil (kanał wyłączony, inne źródło klawiatury, aplikacja poza tabelą).
    @MainActor static func rule(for app: NSRunningApplication?) -> ImePolicy.Rule? {
        guard let app, Settings.shared.imeChannelEnabled, PeerCheck.ownLeafHash != nil, isSourceSelected() else { return nil }
        return ImePolicy.rule(for: app.bundleIdentifier, chromium: isChromium(app))
    }

    @MainActor private static var chromiumCache: [String: Bool] = [:]

    /// Chromium albo Electron: po znanym bundle ID albo frameworku w pakiecie (Electron Framework, Chrome Framework…).
    @MainActor static func isChromium(_ app: NSRunningApplication) -> Bool {
        if let id = app.bundleIdentifier, ImePolicy.chromiumBundleIDs.contains(id) { return true }
        guard let url = app.bundleURL else { return false }
        if let cached = chromiumCache[url.path] { return cached }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: url.appendingPathComponent("Contents/Frameworks").path)) ?? []
        let engines = ["Electron", "Chrome", "Chromium", "Edge", "Brave", "Vivaldi", "Opera"]
        let result = names.contains { name in name.hasSuffix("Framework.framework") && engines.contains { name.contains($0) } }
        chromiumCache[url.path] = result
        return result
    }

    // MARK: - Prośby

    static func hello() async -> ImeProtocol.Reply? {
        let request = ImeProtocol.Request(op: .hello)
        if case .reply(let reply) = await send(request, timeout: 0.5), reply.ok { return reply }
        return nil
    }

    static func context() async -> Outcome {
        var request = ImeProtocol.Request(op: .context)
        request.maxBefore = ImeProtocol.maxWindow
        request.maxAfter = ImeProtocol.maxWindow
        return await send(request, timeout: 0.8)
    }

    static func replace(client: String?, app: String?, check: ImeProtocol.Edit, edits: [ImeProtocol.Edit], expected: String) async -> Outcome {
        var request = ImeProtocol.Request(op: .replace)
        request.client = client
        request.app = app
        request.check = check
        request.edits = edits
        request.expected = expected
        return await send(request, timeout: 2.5, writeDeadline: 1.2)
    }

    static func insert(_ text: String, client: String?) async -> Outcome {
        var request = ImeProtocol.Request(op: .insert)
        request.client = client
        request.text = text
        return await send(request, timeout: 2.5, writeDeadline: 1.2)
    }

    static func classify(_ outcome: Outcome) -> WriteResult {
        switch outcome {
        case .unavailable(let reason):
            return .refused(reason)
        case .noReply:
            return .unknown("no_reply")
        case .reply(let reply):
            if reply.ok { return .landed }
            let reason = reply.reason ?? "unknown"
            if reply.attempted == false, ImeProtocol.preWriteReasons.contains(reason) { return .refused(reason) }
            return .unknown(reason)
        }
    }

    /// `writeDeadline`: po ilu sekundach IME ma zrezygnować z zapisu, zanim zacznie (krócej niż nasze czekanie,
    /// żeby zapis nigdy nie przyszedł po tym, jak przestaniemy czekać).
    private static func send(_ request: ImeProtocol.Request, timeout: TimeInterval, writeDeadline: TimeInterval? = nil) async -> Outcome {
        var request = request
        let now = ProcessInfo.processInfo.systemUptime
        request.deadline = now + (writeDeadline ?? timeout)
        let prepared = request
        return await withCheckedContinuation { continuation in
            queue.async { continuation.resume(returning: sendBlocking(prepared, deadline: now + timeout)) }
        }
    }

    private static func sendBlocking(_ request: ImeProtocol.Request, deadline: TimeInterval) -> Outcome {
        guard let fd = UnixSocket.connect(to: ImeProtocol.socketPath) else { return .unavailable("no_ime") }
        defer { close(fd) }
        UnixSocket.setSendTimeout(fd, 1)
        guard PeerCheck.verify(fd: fd, identifier: ImeProtocol.inputSourceID) else { return .unavailable("peer_rejected") }
        guard let data = ImeProtocol.encode(request) else { return .unavailable("encode") }
        guard UnixSocket.writeAll(fd, data) else { return .unavailable("write_failed") }
        guard let line = UnixSocket.readLine(fd, maxBytes: ImeProtocol.maxMessageBytes, deadline: deadline),
              let reply = ImeProtocol.decode(ImeProtocol.Reply.self, from: line) else { return .noReply }
        // Inna wersja IME odrzuca prośbę przed wykonaniem (sprawdza `v`), więc w polu nic się nie zmieniło.
        guard reply.v == ImeProtocol.version else { return .unavailable("version") }
        guard reply.id == request.id || reply.reason == "bad_request" else { return .noReply }
        return .reply(reply)
    }

    // MARK: - Log i kursor

    /// Zdarzenie `ime_request` (albo `browser_request`) bez tekstu: operacja, wynik, powód, czas, długości.
    static func log(_ op: String, _ outcome: Outcome, app: NSRunningApplication?, started: Date, extra: [String: Any] = [:],
                    event: String = "ime_request") {
        var fields = extra
        fields["op"] = op
        fields["app"] = app?.bundleIdentifier ?? ""
        fields["ms"] = Int(Date().timeIntervalSince(started) * 1000)
        switch outcome {
        case .unavailable(let reason):
            fields["ok"] = false
            fields["reason"] = reason
        case .noReply:
            fields["ok"] = false
            fields["reason"] = "no_reply"
        case .reply(let reply):
            fields["ok"] = reply.ok
            if let reason = reply.reason { fields["reason"] = reason }
            if let attempted = reply.attempted { fields["attempted"] = attempted }
            if let landed = reply.landedAfterMs { fields["landed_ms"] = landed }
            if let match = reply.clientMatch { fields["client_match"] = match }
            if op == "context" {
                fields["before"] = (reply.before ?? "").utf16.count
                fields["after"] = (reply.after ?? "").utf16.count
                fields["selected"] = reply.selectionLength ?? 0
                fields["short_after"] = reply.shortAfter ?? false
            }
        }
        EventLog.write(event, fields)
    }

    /// Kursor z IME jako kotwica panelu — tylko gdy wygląda na prawdziwy: rozsądna wysokość i wewnątrz okna aplikacji.
    @MainActor static func anchor(_ caret: [Double]?, app: NSRunningApplication) -> NSRect? {
        guard let caret, caret.count == 4 else { return nil }
        let rect = NSRect(x: caret[0], y: caret[1], width: max(caret[2], 1), height: caret[3])
        guard rect.height >= 4, rect.height <= 200 else { return nil }
        guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]],
              let primaryHeight = NSScreen.screens.first?.frame.maxY else { return nil }
        for window in windows {
            guard (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == app.processIdentifier,
                  (window[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let boundsInfo = window[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsInfo) else { continue }
            // Okna mają początek w lewym górnym rogu ekranu głównego, Cocoa — w lewym dolnym.
            let cocoa = NSRect(x: bounds.minX, y: primaryHeight - bounds.maxY, width: bounds.width, height: bounds.height)
            if cocoa.intersects(rect) { return rect }
        }
        return nil
    }
}
