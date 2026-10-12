import AppKit
import Carbon.HIToolbox
import InputMethodKit
import GramaIMEProtocol
import GramaIMEShim

/// Prośby aplikacji, wykonywane na głównym wątku (tam IMK pozwala rozmawiać z klientem). Każde wywołanie
/// klienta jest synchroniczne i krótkie; na nic nie czekamy — opóźnione sprawdzenia idą przez `asyncAfter`.
enum Ops {
    typealias Completion = (ImeProtocol.Reply) -> Void

    static func run(_ request: ImeProtocol.Request, completion: @escaping Completion) {
        if expired(request) { return completion(refuse(request, "expired")) }
        if Registry.socketDisabled { return completion(refuse(request, "disabled")) }
        switch request.op {
        case .hello:
            completion(hello(request))
        case .context:
            completion(context(request))
        case .replace, .insert:
            // Panel ⌥⌘G na chwilę zabiera fokus polu — czekamy (bez blokowania), aż pole wróci.
            waitForClient(request, attemptsLeft: 15) { target, match in
                guard let target else { return completion(refuse(request, request.client == nil ? "no_client" : "client_changed")) }
                let reply: Completion = { var r = $0; r.clientMatch = match; completion(r) }
                if request.op == .replace { replace(request, target, reply) } else { insert(request, target, reply) }
            }
        }
    }

    // MARK: - Pole

    typealias Target = (controller: GramaIMEController, token: String, bundleID: String)

    private static func waitForClient(_ request: ImeProtocol.Request, attemptsLeft: Int, _ found: @escaping (Target?, String) -> Void) {
        if let client = request.client {
            if let target = Registry.shared.activeController(for: client) { return found(target, "token") }
        } else if let target = Registry.shared.active {
            return found(target, "active")
        }
        // Podmiana: po ~0,1 s bez tego samego pola przyjmujemy aktywne pole tej samej aplikacji (nowy kontroler
        // po zamknięciu panelu). Zapis i tak nastąpi tylko, gdy w zakresie `check` jest dokładnie pobrany tekst.
        if request.op == .replace, attemptsLeft <= 10, let app = request.app, let target = Registry.shared.active,
           target.bundleID == app {
            return found(target, "app")
        }
        guard attemptsLeft > 0, !expired(request) else { return found(nil, "none") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.02) {
            waitForClient(request, attemptsLeft: attemptsLeft - 1, found)
        }
    }

    private static func client(of target: Target) -> (IMKTextInput & NSObjectProtocol)? {
        target.controller.client()
    }

    /// Powód odmowy wspólny dla wszystkich próśb o tekst, albo nil.
    private static func refusal(for bundleID: String) -> String? {
        if ImePolicy.terminalBundleIDs.contains(bundleID) { return "terminal" }
        if let until = Registry.shared.slowUntil[bundleID], ProcessInfo.processInfo.systemUptime < until { return "slow_client" }
        if secureInputBlocks(bundleID) { return "secure_input" }
        return nil
    }

    /// Pole hasła: macOS włącza wtedy bezpieczne wprowadzanie, a my nie czytamy aplikacji, która je trzyma.
    /// Gdy trzyma je inna aplikacja (menedżer haseł, Terminal z Secure Keyboard Entry), pole obok można czytać.
    static func secureInputBlocks(_ bundleID: String) -> Bool {
        guard IsSecureEventInputEnabled() else { return false }
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
              let pid = (session["kCGSSessionSecureInputPID"] as? NSNumber)?.int32Value,
              let holder = NSRunningApplication(processIdentifier: pid) else { return true }
        return holder.bundleIdentifier == nil || holder.bundleIdentifier == bundleID
    }

    private static func noteDuration(_ started: TimeInterval, _ bundleID: String) {
        let now = ProcessInfo.processInfo.systemUptime
        // Wolny klient trzyma główny wątek IME, a więc i klawisze innych aplikacji — dajemy mu 10 s przerwy.
        if now - started > 0.5 { Registry.shared.slowUntil[bundleID] = now + 10 }
    }

    // MARK: - hello

    private static func hello(_ request: ImeProtocol.Request) -> ImeProtocol.Reply {
        var reply = ImeProtocol.Reply(id: request.id, ok: true)
        let now = ProcessInfo.processInfo.systemUptime
        if let active = Registry.shared.active {
            reply.app = active.bundleID
            reply.client = active.token
        }
        reply.build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String
        reply.imePid = getpid()
        reply.uptime = now - Server.startedAt
        reply.apps = Registry.shared.activations.mapValues { now - $0 }
        reply.requests = Server.requestCount
        return reply
    }

    // MARK: - context

    private static func context(_ request: ImeProtocol.Request) -> ImeProtocol.Reply {
        guard let target = Registry.shared.active, let client = client(of: target) else { return refuse(request, "no_client") }
        if let reason = refusal(for: target.bundleID) { return refuse(request, reason, app: target.bundleID) }
        let started = ProcessInfo.processInfo.systemUptime
        defer { noteDuration(started, target.bundleID) }

        var selection = NSRange(location: NSNotFound, length: 0)
        var marked = NSRange(location: NSNotFound, length: 0)
        if GDTry({ selection = client.selectedRange(); marked = client.markedRange() }) != nil {
            return refuse(request, "client_error_before", app: target.bundleID)
        }
        guard selection.location != NSNotFound else { return refuse(request, "no_selection", app: target.bundleID) }
        if marked.location != NSNotFound, marked.length > 0 { return refuse(request, "composing", app: target.bundleID) }

        let maxBefore = min(max(request.maxBefore ?? 0, 0), ImeProtocol.maxWindow)
        let maxAfter = min(max(request.maxAfter ?? 0, 0), ImeProtocol.maxWindow)
        let selectionTooLong = selection.length > ImeProtocol.maxWindow
        let selectionEnd = NSMaxRange(selection)

        // Trzy odczyty w granicach dokumentu: NSTextView przez IMK zwraca pustkę dla zakresu, który wychodzi
        // poza koniec tekstu, a Chromium oddaje tylko swój bufor ±100 znaków (wtedy `actualRange` jest krótszy).
        let requestStart = max(0, selection.location - maxBefore)
        guard let beforeRead = read(client, NSRange(location: requestStart, length: selection.location - requestStart)) else {
            return refuse(request, "client_error_before", app: target.bundleID)
        }
        var before = beforeRead.text as NSString
        var windowStart = beforeRead.start ?? requestStart
        // Okno musi kończyć się dokładnie na kursorze, inaczej przesunięcia byłyby błędne.
        guard windowStart >= 0, windowStart + before.length == selection.location else {
            return refuse(request, "not_text", app: target.bundleID)
        }
        var selected = ""
        if selection.length > 0, !selectionTooLong {
            guard let selectedRead = read(client, selection), (selectedRead.text as NSString).length == selection.length else {
                return refuse(request, "not_text", app: target.bundleID)
            }
            selected = selectedRead.text
        }
        guard let after = readAfter(client, from: selectionEnd, max: selectionTooLong ? 0 : maxAfter) else {
            return refuse(request, "client_error_before", app: target.bundleID)
        }
        var afterText = after.text as NSString

        // Brzegi okna na granicy znaku (nie w środku pary zastępczej UTF-16, np. emoji).
        if before.length > 0, UTF16.isTrailSurrogate(before.character(at: 0)) {
            before = before.substring(from: 1) as NSString
            windowStart += 1
        }
        var shortAfter = after.reachedEnd
        if afterText.length > 0, UTF16.isLeadSurrogate(afterText.character(at: afterText.length - 1)) {
            afterText = afterText.substring(to: afterText.length - 1) as NSString
            shortAfter = false
        }

        var reply = ImeProtocol.Reply(id: request.id, ok: true)
        reply.app = target.bundleID
        reply.client = target.token
        reply.windowStart = windowStart
        reply.selectionLocation = selection.location
        reply.selectionLength = selection.length
        reply.before = before as String
        reply.selected = selected
        reply.after = afterText as String
        reply.shortAfter = shortAfter && !selectionTooLong
        reply.selectionTooLong = selectionTooLong

        var caretActual = NSRange(location: NSNotFound, length: 0)
        var rect = NSRect.zero
        if GDTry({ rect = client.firstRect(forCharacterRange: NSRange(location: NSMaxRange(selection), length: 0), actualRange: &caretActual) }) == nil,
           rect.height > 0 {
            reply.caret = [rect.origin.x, rect.origin.y, rect.width, rect.height]
        }
        return reply
    }

    /// Tekst zakresu i początek tego, co aplikacja naprawdę oddała (nil przy wyjątku klienta).
    private static func read(_ client: IMKTextInput & NSObjectProtocol, _ range: NSRange) -> (text: String, start: Int?)? {
        guard range.length > 0 else { return ("", range.location) }
        var actual = NSRange(location: NSNotFound, length: 0)
        var text: String?
        if GDTry({ text = client.string(from: range, actualRange: &actual) }) != nil { return nil }
        return (text ?? "", actual.location == NSNotFound ? (text == nil ? nil : range.location) : actual.location)
    }

    /// Tekst za kursorem, najwyżej `max` znaków, i czy sięga końca dokumentu. Najpierw cały zakres (Chromium,
    /// Office), potem długość dokumentu z `length()`, a gdy aplikacja jej nie podaje — koniec szukany połówkowo.
    private static func readAfter(_ client: IMKTextInput & NSObjectProtocol, from start: Int, max limit: Int) -> (text: String, reachedEnd: Bool)? {
        guard limit > 0 else { return ("", false) }
        guard let full = read(client, NSRange(location: start, length: limit)) else { return nil }
        let fullLength = (full.text as NSString).length
        if fullLength > 0, full.start == start {
            return (full.text, fullLength < limit)
        }
        var documentLength = 0
        if GDTry({ documentLength = client.length() }) != nil { return nil }
        // Kursor na końcu dokumentu (NSTextView podaje długość): za nim nic nie ma.
        if documentLength > 0, documentLength <= start { return ("", true) }
        if documentLength > start {
            let length = min(limit, documentLength - start)
            guard let clamped = read(client, NSRange(location: start, length: length)) else { return nil }
            return (clamped.text, start + (clamped.text as NSString).length >= documentLength)
        }
        // Największa długość, którą aplikacja jeszcze oddaje (kilkanaście krótkich odczytów).
        var low = 0, high = limit
        while low < high {
            let mid = (low + high + 1) / 2
            guard let probe = read(client, NSRange(location: start, length: mid)) else { return nil }
            if (probe.text as NSString).length == mid { low = mid } else { high = mid - 1 }
        }
        guard let found = read(client, NSRange(location: start, length: low)) else { return nil }
        return (found.text, low < limit)
    }

    // MARK: - replace

    private static func replace(_ request: ImeProtocol.Request, _ target: Target, _ completion: @escaping Completion) {
        guard let client = client(of: target) else { return completion(refuse(request, "no_client")) }
        guard let check = request.check, let edits = request.edits, let expected = request.expected, !edits.isEmpty,
              valid(check: check, edits: edits, expected: expected) else { return completion(refuse(request, "bad_request")) }
        if let reason = refusal(for: target.bundleID) { return completion(refuse(request, reason, app: target.bundleID)) }
        let started = ProcessInfo.processInfo.systemUptime
        defer { noteDuration(started, target.bundleID) }

        var selection = NSRange(location: NSNotFound, length: 0)
        var marked = NSRange(location: NSNotFound, length: 0)
        var current: String?
        var actual = NSRange(location: NSNotFound, length: 0)
        var documentAccess = true
        let checkRange = NSRange(location: check.location, length: check.length)
        if GDTry({
            selection = client.selectedRange()
            marked = client.markedRange()
            documentAccess = client.supportsProperty(TSMDocumentPropertyTag(kTSMDocumentSupportDocumentAccessPropertyTag))
            current = client.string(from: checkRange, actualRange: &actual)
        }) != nil { return completion(refuse(request, "client_error_before", app: target.bundleID)) }
        if marked.location != NSNotFound, marked.length > 0 { return completion(refuse(request, "composing", app: target.bundleID)) }
        // Bez dostępu do dokumentu klient ignoruje `replacementRange` i wstawia przy kursorze (duplikat).
        guard documentAccess else { return completion(refuse(request, "no_document_access", app: target.bundleID)) }
        guard let current, actual.location == NSNotFound || actual == checkRange,
              ImeText.comparable(current) == ImeText.comparable(check.text) else {
            return completion(refuse(request, "changed", app: target.bundleID))
        }
        // Ostatnia chwila na rezygnację: aplikacja mogła już przestać czekać.
        if expired(request) { return completion(refuse(request, "expired", app: target.bundleID)) }

        var failed: String?
        for edit in edits {
            failed = GDTry { client.insertText(edit.text, replacementRange: NSRange(location: edit.location, length: edit.length)) }
            if failed != nil { break }
        }
        var reply = ImeProtocol.Reply(id: request.id, ok: false)
        reply.attempted = true
        reply.app = target.bundleID
        reply.caretBefore = selection.location
        if failed != nil {
            reply.reason = "client_error_after"
            return completion(reply)
        }
        let expectedRange = NSRange(location: check.location, length: (expected as NSString).length)
        verify(client, expectedRange, expected, reply, delays: [0, 0.15, 0.5], started: ProcessInfo.processInfo.systemUptime, completion)
    }

    private static func valid(check: ImeProtocol.Edit, edits: [ImeProtocol.Edit], expected: String) -> Bool {
        let limit = ImeProtocol.maxWindow * 2
        guard check.location >= 0, check.length >= 0, check.length <= limit, (check.text as NSString).length == check.length,
              (expected as NSString).length <= limit * 2, !expected.contains("\u{0}") else { return false }
        var previousStart = Int.max
        for edit in edits {
            guard edit.location >= check.location, edit.length >= 0,
                  edit.location + edit.length <= check.location + check.length,
                  edit.location + edit.length <= previousStart,  // od końca, bez nakładania
                  (edit.text as NSString).length <= limit, !edit.text.contains("\u{0}") else { return false }
            previousStart = edit.location
        }
        return true
    }

    // MARK: - insert

    private static func insert(_ request: ImeProtocol.Request, _ target: Target, _ completion: @escaping Completion) {
        guard let client = client(of: target) else { return completion(refuse(request, "no_client")) }
        guard let text = request.text, !text.isEmpty, (text as NSString).length <= ImeProtocol.maxWindow * 2,
              !text.contains("\u{0}") else { return completion(refuse(request, "bad_request")) }
        if let reason = refusal(for: target.bundleID) { return completion(refuse(request, reason, app: target.bundleID)) }
        let started = ProcessInfo.processInfo.systemUptime
        defer { noteDuration(started, target.bundleID) }

        var selection = NSRange(location: NSNotFound, length: 0)
        var marked = NSRange(location: NSNotFound, length: 0)
        var previous: String?
        if GDTry({
            selection = client.selectedRange()
            marked = client.markedRange()
            if selection.location != NSNotFound, selection.location > 0 {
                let from = max(0, selection.location - 2)
                var actual = NSRange(location: NSNotFound, length: 0)
                previous = client.string(from: NSRange(location: from, length: selection.location - from), actualRange: &actual)
            }
        }) != nil { return completion(refuse(request, "client_error_before", app: target.bundleID)) }
        guard selection.location != NSNotFound else { return completion(refuse(request, "no_selection", app: target.bundleID)) }
        if marked.location != NSNotFound, marked.length > 0 { return completion(refuse(request, "composing", app: target.bundleID)) }
        if expired(request) { return completion(refuse(request, "expired", app: target.bundleID)) }

        // Odstęp liczony w tej samej chwili co wstawienie — między dwiema prośbami użytkownik mógłby coś dopisać.
        let spaced = ImeText.leadingSpaceNeeded(before: text, after: previous?.last) ? " " + text : text
        var reply = ImeProtocol.Reply(id: request.id, ok: false)
        reply.attempted = true
        reply.app = target.bundleID
        reply.caretBefore = selection.location
        // NSNotFound: w miejsce zaznaczenia i z kursorem za tekstem (zakres wprost zostawia w Chromium zaznaczenie).
        if GDTry({ client.insertText(spaced, replacementRange: NSRange(location: NSNotFound, length: 0)) }) != nil {
            reply.reason = "client_error_after"
            return completion(reply)
        }
        // Sprawdzamy koniec wstawionego tekstu (tuż przed nowym kursorem): Chromium widzi tylko ±100 znaków
        // wokół kursora, więc początek długiego dyktowania byłby poza jego oknem.
        let spacedNS = spaced as NSString
        var tailStart = max(0, spacedNS.length - 64)
        if tailStart > 0, UTF16.isTrailSurrogate(spacedNS.character(at: tailStart)) { tailStart -= 1 }
        let tail = spacedNS.substring(from: tailStart)
        let expectedRange = NSRange(location: selection.location + tailStart, length: spacedNS.length - tailStart)
        verify(client, expectedRange, tail, reply, delays: [0, 0.15, 0.5], started: ProcessInfo.processInfo.systemUptime, completion)
    }

    // MARK: - Sprawdzenie po zapisie

    /// Czy w `range` jest `expected` — od razu, a potem po chwili (Electron stosuje zmiany asynchronicznie).
    /// Nigdy nie ponawiamy zapisu: spóźnione zastosowanie dałoby tekst dwa razy.
    private static func verify(_ client: IMKTextInput & NSObjectProtocol, _ range: NSRange, _ expected: String,
                               _ base: ImeProtocol.Reply, delays: [Double], started: TimeInterval, _ completion: @escaping Completion) {
        guard let delay = delays.first else {
            var reply = base
            reply.reason = "not_landed"
            return completion(reply)
        }
        let check = {
            var actual = NSRange(location: NSNotFound, length: 0)
            var now: String?
            var selection = NSRange(location: NSNotFound, length: 0)
            let failed = GDTry {
                now = client.string(from: range, actualRange: &actual)
                selection = client.selectedRange()
            }
            if failed == nil, let now, ImeText.comparable(now) == ImeText.comparable(expected) {
                var reply = base
                reply.ok = true
                reply.landedAfterMs = Int((ProcessInfo.processInfo.systemUptime - started) * 1000)
                reply.caretAfter = selection.location == NSNotFound ? nil : selection.location
                return completion(reply)
            }
            verify(client, range, expected, base, delays: Array(delays.dropFirst()), started: started, completion)
        }
        if delay == 0 { check() } else { DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: check) }
    }

    // MARK: - Pomocnicze

    private static func expired(_ request: ImeProtocol.Request) -> Bool {
        guard let deadline = request.deadline else { return false }
        return ProcessInfo.processInfo.systemUptime > deadline
    }

    static func refuse(_ request: ImeProtocol.Request, _ reason: String, app: String? = nil) -> ImeProtocol.Reply {
        var reply = ImeProtocol.Reply(id: request.id, ok: false, reason: reason)
        reply.attempted = false
        reply.app = app
        return reply
    }
}
