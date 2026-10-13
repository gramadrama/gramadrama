import AppKit
import ApplicationServices
import SwiftUI
import GramaCore
import GramaBackend

/// Obserwacja pisania (etap 3b) i plakietka z liczbą błędów (3c).
///
/// Co 0,4 s odczytuje aktywne pole przez AX (tylko odczyt — nic nie kopiuje i nie wkleja). Po każdej
/// zmianie od razu warstwa 0 (słownik macOS, milisekundy), a po chwili bez pisania warstwa 1
/// (LanguageTool na serwerze). Odpytywanie zamiast AXObservera, bo Electron (Slack) zgłasza zmiany
/// wartości niewiarygodnie, a koszt kilku wywołań AX na tick jest pomijalny.
@MainActor
final class LiveMonitor {
    static let shared = LiveMonitor()

    /// Wstrzymanie z zewnątrz (otwarty panel poprawki).
    var isSuspended: () -> Bool = { false }
    /// Kliknięcie plakietki — otwiera panel poprawki dla akapitu z kursorem.
    var onOpen: () -> Void = {}

    static let interval: TimeInterval = 0.4
    /// Pauza w pisaniu, po której pytamy LanguageTool.
    static let languageToolDelay: UInt64 = 800_000_000
    /// Pauza, po której akapit sprawdza Bielik w tle (warstwa 2), i limit długości — dłuższy tekst to
    /// dodatkowego czasu serwera.
    static let bielikDelay: UInt64 = 3_000_000_000
    static let bielikMaxLength = 1500
    /// Role, których nie sprawdzamy: wyszukiwarki i listy rozwijane to nie wiadomości.
    nonisolated private static let skippedRoles: Set<String> = ["AXSearchField", "AXComboBox"]

    private var timer: Timer?
    private var ticking = false
    private let badge = LiveBadge()
    private let underlines = LiveUnderlines()
    private var snapshot: Snapshot?

    private var element: AXUIElement?
    /// Pole strony z wtyczki przeglądarki (zamiast `element`): przeglądarka i token pola.
    private var browserField: BrowserField?
    /// Sprawdzany tekst i jego położenie w polu (długie pola: sam akapit z kursorem).
    private var text = ""
    private var offset = 0
    private var caret: Int?
    private var quickIssues: [LiveIssue] = []
    private var languageToolIssues: [LiveIssue] = []
    private var quickTask: Task<Void, Never>?
    private var languageToolTask: Task<Void, Never>?
    private var bielikIssues: [LiveIssue] = []
    private var bielikTask: Task<Void, Never>?
    /// Zapytania do serwera w toku (jak `checks_in_progress` na Linuksie): póki trwają, plakietka pokazuje lawę, a znana
    /// liczba „oddycha”. Nowe sprawdzanie je czyści, więc spóźnione zakończenie starego zapytania niczego nie zmienia;
    /// wynik z pamięci w ogóle ich nie zapala.
    private var languageToolCheck: UUID?
    private var bielikCheck: UUID?
    /// Po błędzie połączenia Bielik w tle odpoczywa (niedostępny serwer nie może zalewać logów ani sieci).
    private var bielikPausedUntil = Date.distantPast
    /// Tryb „tylko model” (eksperymentalny): jedyna warstwa, gdy jest włączony. `modelOnly` — tryb z ostatniego
    /// sprawdzenia; przełączenie w ustawieniach czyści wyniki obu trybów i sprawdza pole od nowa.
    private var modelOnly = false
    /// Języki tekstu ostatniego sprawdzania — po zmianie w oknie (Pisanie → Języki tekstu) sprawdzamy od nowa.
    private var languages = Settings.shared.textLanguages
    private var modelOnlyIssues: [LiveIssue] = []
    private var modelOnlyTask: Task<Void, Never>?
    /// Fragment czeka na serwer — plakietka pokazuje wtedy „sprawdzam” (nie przy wynikach z pamięci).
    private var modelOnlyWaiting = false
    private var modelOnlyPausedUntil = Date.distantPast
    /// Co plakietka mówi poza błędami: serwer nie odpowiada albo część zdań została niesprawdzona.
    private var modelOnlyNote: String?
    /// „Ignoruj” z karty: to słowo z tą regułą do końca działania aplikacji.
    private var ignored = Set<String>()
    private var applying = false
    /// Położenie słów liczy kolejka AX; nowa prośba w trakcie czeka na koniec poprzedniej.
    private var geometryInFlight = false
    private var geometryPending = false

    func start() {
        underlines.onApply = { [weak self] issue, suggestion in self?.apply(issue, suggestion) }
        underlines.onIgnore = { [weak self] issue in self?.ignore(issue) }
        underlines.onLearn = { [weak self] issue in self?.learn(issue) }
        underlines.onTurnOff = { [weak self] issue in self?.turnOff(issue) }
        badge.onClick = { [weak self] in
            self?.badge.hide()
            self?.onOpen()
        }
        badge.onFix = { [weak self] issue, suggestion in self?.apply(issue, suggestion) }
        timer?.invalidate()
        let timer = Timer(timeInterval: Self.interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        timer.tolerance = 0.1
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    /// Porzuca stan (inna aplikacja, inne pole, wyłączone sprawdzanie).
    /// ⌥⌘G ma pierwszeństwo: przerywa sprawdzanie w tle, żeby Bielik od razu zajął się panelem.
    func cancelModelCheck() {
        bielikTask?.cancel()
        bielikTask = nil
        bielikCheck = nil
        modelOnlyTask?.cancel()
        modelOnlyTask = nil
        modelOnlyWaiting = false
    }

    func reset() {
        quickTask?.cancel()
        languageToolTask?.cancel()
        languageToolCheck = nil
        cancelModelCheck()
        bielikIssues = []
        modelOnlyIssues = []
        modelOnlyNote = nil
        element = nil
        browserField = nil
        text = ""
        caret = nil
        quickIssues = []
        languageToolIssues = []
        snapshot = nil
        badge.hide()
        underlines.hide()
    }

    /// Błędy do pokazania: bez słowa, które właśnie jest pisane (kursor na jego końcu), i bez zignorowanych.
    var visibleIssues: [LiveIssue] {
        let shown = modelOnly ? LiveText.merge(modelOnlyIssues) : LiveText.merge(languageToolIssues, quickIssues, bielikIssues)
        return shown.filter { issue in
            guard !ignored.contains(Self.ignoreKey(issue)) else { return false }
            guard let caret else { return true }
            return !(issue.range.location < caret && caret <= NSMaxRange(issue.range))
        }
    }

    private static let trace = ProcessInfo.processInfo.environment["LIVE_TRACE"] != nil
    private func log(_ message: @autoclosure () -> String) { if Self.trace { print("· \(message())"); fflush(stdout) } }

    // MARK: Poprawianie pojedynczego błędu (karta pod podkreśleniem)

    private static func ignoreKey(_ issue: LiveIssue) -> String { "\(issue.word.lowercased())|\(issue.rule)" }

    /// Podmienia jedno słowo przez te same, sprawdzane ścieżki co panel: w polu natywnym tylko zmieniony
    /// fragment, w webowym (Slack, Outlook) zaznaczenie przez AX i wklejenie. Przed wklejeniem sprawdzamy,
    /// że w polu nadal stoi to słowo; po — że poprawka jest na miejscu.
    func apply(_ issue: LiveIssue, _ suggestion: String) {
        guard !applying, let snapshot else { return }
        let expected = NSRange(location: issue.range.location + snapshot.offset, length: issue.range.length)
        let known = snapshot.value as NSString
        guard NSMaxRange(expected) <= known.length else { return }
        let original = known.substring(with: expected)
        if let browser = snapshot.browser {
            applyInBrowser(issue, suggestion, original: original, expected: expected, snapshot: snapshot, field: browser)
            return
        }
        guard let field = snapshot.field else { return }
        // Pole czytamy jeszcze raz tuż przed podmianą: od ostatniego odczytu (do 0,4 s) tekst mógł się przesunąć,
        // np. po poprzedniej poprawce. Szukamy tego samego słowa najbliżej spodziewanego miejsca.
        guard let current = TextBridge.string(field, kAXValueAttribute).map({ $0 as NSString }),
              let range = Self.locate(original, near: expected.location, in: current) else {
            EventLog.write("live_fix", ["app": snapshot.app.bundleIdentifier ?? "", "outcome": "word_gone"])
            Toast.shared.show(tr("Nie znalazłem już słowa „{word}” w polu — spróbuj jeszcze raz.", ["word": original]))
            showBadge(frame: nil)
            return
        }
        let captured = CapturedText(text: original, scope: .selection, app: snapshot.app, anchor: nil, canApply: true,
                                    element: field, range: CFRange(location: range.location, length: range.length),
                                    source: "live-fix", axText: snapshot.web ? original : nil)
        replaceWord(issue, suggestion, captured: captured, snapshot: snapshot)
    }

    /// Pole strony: świeży tekst z wtyczki, to samo słowo najbliżej spodziewanego miejsca, podmiana przez wtyczkę
    /// (sprawdza, że słowo wciąż tam stoi).
    private func applyInBrowser(_ issue: LiveIssue, _ suggestion: String, original: String, expected: NSRange,
                                snapshot: Snapshot, field: BrowserField) {
        applying = true
        Task {
            let received = await BrowserChannel.live(field.browser)
            applying = false
            guard case .reply(let reply) = received.outcome, reply.ok, reply.client == field.client,
                  let start = reply.windowStart else {
                EventLog.write("live_fix", ["app": snapshot.app.bundleIdentifier ?? "", "outcome": "field_gone", "browser": true])
                Toast.shared.show(tr("Nie widzę już tego pola — spróbuj jeszcze raz."))
                return
            }
            let current = ((reply.before ?? "") + (reply.selected ?? "") + (reply.after ?? "")) as NSString
            // Spodziewane miejsce w nowym oknie tekstu (okno mogło się przesunąć razem z kursorem).
            let near = field.base + expected.location - start
            guard let range = Self.locate(original, near: near, in: current) else {
                EventLog.write("live_fix", ["app": snapshot.app.bundleIdentifier ?? "", "outcome": "word_gone", "browser": true])
                Toast.shared.show(tr("Nie znalazłem już słowa „{word}” w polu — spróbuj jeszcze raz.", ["word": original]))
                showBadge(frame: nil)
                return
            }
            let captured = CapturedText(text: original, scope: .selection, app: snapshot.app, anchor: nil, canApply: true,
                                        source: "live-fix",
                                        browser: BrowserTarget(range: NSRange(location: start + range.location, length: range.length),
                                                               client: field.client, browser: field.browser))
            replaceWord(issue, suggestion, captured: captured, snapshot: snapshot)
        }
    }

    private func replaceWord(_ issue: LiveIssue, _ suggestion: String, captured: CapturedText, snapshot: Snapshot) {
        applying = true
        Task {
            defer { applying = false }
            let outcome = await TextBridge.replace(captured, with: suggestion)
            EventLog.write("live_fix", ["app": snapshot.app.bundleIdentifier ?? "", "source": issue.source.rawValue,
                                        "rule": issue.rule, "outcome": "\(outcome)", "web": snapshot.web])
            switch outcome {
            case .pasted:
                // Znika od razu; reszta przesunie się przy następnym odczycie pola.
                for list in [\LiveMonitor.quickIssues, \LiveMonitor.languageToolIssues, \LiveMonitor.bielikIssues, \LiveMonitor.modelOnlyIssues] {
                    self[keyPath: list].removeAll { $0.range == issue.range }
                }
                showBadge(frame: nil)
            case .fieldChanged, .copiedToClipboard, .busy, .lineBreaksChanged:
                Toast.shared.show(tr("Nie udało się podmienić słowa — „{word}” jest w schowku (⌘V).", ["word": suggestion]))
            case .pasteNotLanded, .uncertain:
                Toast.shared.show(tr("Nie mam pewności, czy poprawka trafiła — sprawdź słowo „{word}”.", ["word": suggestion]))
            case .badResult:
                break
            }
        }
    }

    /// Najbliższe wystąpienie słowa w pobliżu spodziewanego miejsca (całe słowo, nie fragment dłuższego).
    nonisolated static func locate(_ word: String, near location: Int, in text: NSString) -> NSRange? {
        let length = (word as NSString).length
        guard length > 0 else { return nil }
        var best: NSRange?
        var search = NSRange(location: 0, length: text.length)
        while true {
            let found = text.range(of: word, options: [.literal], range: search)
            guard found.location != NSNotFound else { break }
            let before = found.location > 0 ? UnicodeScalar(text.character(at: found.location - 1)) : nil
            let after = NSMaxRange(found) < text.length ? UnicodeScalar(text.character(at: NSMaxRange(found))) : nil
            let wordChar = { (c: UnicodeScalar?) in c.map { CharacterSet.letters.contains($0) } ?? false }
            if !wordChar(before), !wordChar(after),
               best.map({ abs($0.location - location) > abs(found.location - location) }) ?? true {
                best = found
            }
            search = NSRange(location: found.location + 1, length: text.length - found.location - 1)
        }
        guard let best, abs(best.location - location) <= 200 else { return nil }
        return best
    }

    func ignore(_ issue: LiveIssue) {
        ignored.insert(Self.ignoreKey(issue))
        showBadge(frame: nil)
    }

    /// Słownik użytkownika macOS — ten sam, którego używają Notatki, Mail i Safari — i słownik GramaDramy.
    func learn(_ issue: LiveIssue) {
        UserWords.learn(issue.word)
        for list in [\LiveMonitor.quickIssues, \LiveMonitor.languageToolIssues, \LiveMonitor.bielikIssues, \LiveMonitor.modelOnlyIssues] {
            self[keyPath: list].removeAll { $0.word == issue.word && $0.isSpelling }
        }
        showBadge(frame: nil)
        Toast.shared.show(tr("Dodano „{word}” do słownika.", ["word": issue.word]))
    }

    /// „Wyłącz regułę”: jej błędy znikają od razu, a LanguageTool jej nie sprawdza, aż do „Włącz” w Pisaniu.
    func turnOff(_ issue: LiveIssue) {
        guard issue.canTurnOff else { return }
        Settings.shared.turnOffRule(issue.rule, name: issue.ruleDescription)
        languageToolIssues.removeAll { $0.rule == issue.rule }
        showBadge(frame: nil)
        EventLog.write("live_rule_off", ["rule": issue.rule])
        Toast.shared.show(tr("Wyłączono tę regułę. Włączysz ją w aplikacji GramaDrama (Pisanie › Słownik i reguły)."))
    }

    private func tick() {
        guard !ticking, !applying else { log("tick: jeszcze trwa"); return }
        let settings = Settings.shared
        guard settings.liveCheckEnabled, !isSuspended(), !TextBridge.isBusy, TextBridge.isTrusted(prompt: false),
              let app = NSWorkspace.shared.frontmostApplication, app != NSRunningApplication.current,
              !settings.isDisabled(app.bundleIdentifier),
              !(app.bundleIdentifier.map(TextBridge.terminalBundleIDs.contains) ?? false),
              !TextBridge.isTextless(app) else {
            if element != nil || browserField != nil || badge.isVisible { reset() }
            return
        }
        ticking = true
        // Przeglądarka z wtyczką: tekst i położenie słów z DOM, bez AX (tam jest wolne — ax_slow w Edge).
        if let browser = app.bundleIdentifier, BrowserChannel.isConnected(browser) {
            Task {
                defer { ticking = false }
                await tickBrowser(app, browser)
            }
            return
        }
        Task {
            defer { ticking = false }
            // Wszystkie wywołania AX na kolejce AX — zawieszona aplikacja nie zatrzyma głównego wątku.
            let started = Date()
            let read = await AXQueue.run { () -> Snapshot? in
                guard let field = TextBridge.focusedElementBlocking(pid: app.processIdentifier) else { return nil }
                // Aplikacja odpowiada z limitem czasu (zawieszona) — kolejne wywołania czekałyby po 1 s każde,
                // a w kolejce AX może czekać ⌥⌘G. Spróbujemy przy następnym ticku.
                guard Date().timeIntervalSince(started) < AXQueue.slowThreshold else { return nil }
                return Self.read(field, in: app)
            }
            Self.logIfSlow("read", since: started, app: app)
            log("pole: \(read != nil) \(app.bundleIdentifier ?? "")")
            guard var snapshot = read, settings.liveCheckEnabled, !isSuspended() else {
                if element != nil || browserField != nil || badge.isVisible { reset() }
                return
            }
            snapshot.frame = snapshot.axFrame.map(TextBridge.cocoaRect)
            update(snapshot)
        }
    }

    /// Pole strony: przeglądarka i token pola (karta, ramka, element), `base` — początek okna tekstu w polu.
    struct BrowserField: Equatable {
        let browser: String
        let client: String
        let base: Int
    }

    /// Tick w przeglądarce z wtyczką. Fokus poza polem strony (pasek adresu) = brak pola — bez przejścia na AX.
    private func tickBrowser(_ app: NSRunningApplication, _ browser: String) async {
        let received = await BrowserChannel.live(browser)
        guard case .reply(let reply) = received.outcome, reply.ok, let client = reply.client,
              let start = reply.windowStart, Settings.shared.liveCheckEnabled, !isSuspended(),
              !Settings.shared.isSiteDisabled(received.raw["site"] as? String) else {
            if element != nil || browserField != nil || badge.isVisible { reset() }
            return
        }
        let value = (reply.before ?? "") + (reply.selected ?? "") + (reply.after ?? "")
        guard !value.isBlank else {
            if browserField != nil || badge.isVisible { reset() }
            return
        }
        let ns = value as NSString
        let caretInWindow = (reply.before ?? "").utf16.count
        var range = NSRange(location: 0, length: ns.length)
        if ns.length > TextBridge.maxChars {
            range = TextEdits.paragraphRange(in: value, caret: caretInWindow)
            guard range.length <= TextBridge.maxChars, range.length > 0 else { return }
        }
        let caret = (reply.selectionLength ?? 0) == 0 ? caretInWindow - range.location : nil
        let frame = (received.raw["field"] as? [Double]).flatMap { rect in
            rect.count == 4 ? BrowserChannel.screenConverter(received.raw)?(rect) : nil
        }
        let snapshot = Snapshot(app: app, web: true, field: nil, browser: BrowserField(browser: browser, client: client, base: start),
                                value: value, text: ns.substring(with: range), offset: range.location, caret: caret,
                                axFrame: nil, frame: frame)
        update(snapshot)
    }

    private static func logIfSlow(_ step: String, since started: Date, app: NSRunningApplication?) {
        let elapsed = Date().timeIntervalSince(started)
        guard elapsed >= AXQueue.slowThreshold else { return }
        EventLog.write("ax_slow", ["step": step, "ms": Int(elapsed * 1000), "app": app?.bundleIdentifier ?? ""])
    }

    private struct Snapshot {
        let app: NSRunningApplication
        let web: Bool
        /// Pole z AX albo (`browser`) pole strony z wtyczki.
        let field: AXUIElement?
        var browser: BrowserField? = nil
        let value: String
        let text: String
        let offset: Int
        let caret: Int?
        /// Widoczna część pola we współrzędnych AX (z kolejki AX) i w Cocoa (przeliczona na głównym wątku).
        let axFrame: CGRect?
        var frame: NSRect?
    }

    nonisolated private static func read(_ field: AXUIElement, in app: NSRunningApplication) -> Snapshot? {
        guard TextBridge.isEditable(field), !TextBridge.isSecure(field), !TextBridge.isWebTerminal(field),
              !Self.skippedRoles.contains(TextBridge.string(field, kAXRoleAttribute) ?? ""),
              let value = TextBridge.string(field, kAXValueAttribute), !value.isBlank else { return nil }
        let ns = value as NSString
        let selection = TextBridge.selectedRange(field)
        var range = NSRange(location: 0, length: ns.length)
        if ns.length > TextBridge.maxChars {
            // Długi dokument: sam akapit z kursorem.
            guard let selection else { return nil }
            range = TextEdits.paragraphRange(in: value, caret: selection.location)
            guard range.length <= TextBridge.maxChars, range.length > 0 else { return nil }
        }
        let caret = selection.flatMap { $0.length == 0 ? $0.location - range.location : nil }
        return Snapshot(app: app, web: TextBridge.isWebContent(field), field: field, value: value, text: ns.substring(with: range), offset: range.location, caret: caret,
                        axFrame: visibleFrame(of: field), frame: nil)
    }

    private func update(_ snapshot: Snapshot) {
        let sameField: Bool
        if let field = snapshot.field {
            sameField = element.map { CFEqual($0, field) } ?? false
        } else {
            // To samo pole strony i to samo okno tekstu (inny początek okna = inne przesunięcia).
            sameField = browserField != nil && browserField == snapshot.browser
        }
        caret = snapshot.caret
        self.snapshot = snapshot
        // Inne języki tekstu albo przełączenie trybu „tylko model” w ustawieniach: wyniki nie pasują — sprawdzamy od nowa.
        let languagesChanged = Settings.shared.textLanguages != languages
        languages = Settings.shared.textLanguages
        let modeChanged = Settings.shared.liveModelOnly != modelOnly
        if modeChanged {
            modelOnly = Settings.shared.liveModelOnly
            modelOnlyNote = nil
            modelOnlyPausedUntil = .distantPast
        }
        if modeChanged || languagesChanged || !sameField || snapshot.text != text || snapshot.offset != offset {
            if !modeChanged, !languagesChanged, sameField, snapshot.offset == offset {
                // To samo pole: znane błędy przesuwamy, zanim przyjdą nowe wyniki — licznik nie skacze.
                quickIssues = LiveText.shift(quickIssues, from: text, to: snapshot.text)
                languageToolIssues = LiveText.shift(languageToolIssues, from: text, to: snapshot.text)
                bielikIssues = LiveText.shift(bielikIssues, from: text, to: snapshot.text)
                modelOnlyIssues = LiveText.shift(modelOnlyIssues, from: text, to: snapshot.text)
            } else {
                quickIssues = []
                languageToolIssues = []
                bielikIssues = []
                modelOnlyIssues = []
                if !sameField { modelOnlyNote = nil }
            }
            element = snapshot.field
            browserField = snapshot.browser
            text = snapshot.text
            offset = snapshot.offset
            check()
        }
        showBadge(frame: snapshot.frame)
    }

    private func check() {
        quickTask?.cancel()
        languageToolTask?.cancel()
        languageToolCheck = nil
        cancelModelCheck()
        if modelOnly {
            scheduleModelOnly()
            return
        }
        scheduleBielik()
        let live = LiveText(text, languages: Settings.shared.textLanguages)
        let checked = text
        quickTask = Task {
            let issues = await LiveChecker.shared.quick(live)
            log("warstwa 0: \(issues.count), anulowane \(Task.isCancelled), aktualne \(checked == text)")
            guard !Task.isCancelled, checked == text else { return }
            quickIssues = issues
            showBadge(frame: nil)
        }
        guard Settings.shared.liveLanguageToolEnabled else { return }
        languageToolTask = Task {
            try? await Task.sleep(nanoseconds: Self.languageToolDelay)
            guard !Task.isCancelled else { return }
            let token = UUID()
            if !live.present.isEmpty {
                languageToolCheck = token
                showBadge(frame: nil)
            }
            let issues = await LiveChecker.shared.languageTool(live)
            guard !Task.isCancelled else { return }
            if languageToolCheck == token { languageToolCheck = nil }
            if let issues, checked == text { languageToolIssues = issues }
            showBadge(frame: nil)
        }
    }

    private func scheduleBielik() {
        let checked = text
        guard Settings.shared.liveBielikEnabled, (checked as NSString).length <= Self.bielikMaxLength,
              Date() >= bielikPausedUntil else { return }
        bielikTask = Task {
            try? await Task.sleep(nanoseconds: Self.bielikDelay)
            guard !Task.isCancelled, checked == text, !isSuspended() else { return }
            let started = Date()
            let token = UUID()
            let issues = await LiveChecker.shared.bielik(checked) { [weak self] in
                guard let self, !Task.isCancelled, checked == text else { return }
                bielikCheck = token
                showBadge(frame: nil)
            }
            guard !Task.isCancelled else { return }
            if bielikCheck == token { bielikCheck = nil }
            guard let issues else {
                bielikPausedUntil = Date().addingTimeInterval(60)
                EventLog.write("live_bielik_failed", ["chars": checked.count])
                showBadge(frame: nil)
                return
            }
            EventLog.write("live_bielik", ["chars": checked.count, "issues": issues.count,
                                           "ms": Int(Date().timeIntervalSince(started) * 1000)])
            if checked == text { bielikIssues = issues }
            showBadge(frame: nil)
        }
    }

    /// Tryb „tylko model”: po ~0,8 s bez pisania fragmenty od kursora; wynik każdego od razu na plakietce.
    /// Wyniki fragmentu zastępują przesunięte stare błędy z jego zakresu, a po przejściu całego pola znikają
    /// stare błędy spoza fragmentów. Odrzucona odpowiedź modelu dotyczy tylko jej fragmentu; błąd serwera
    /// zostawia dotychczasowe wyniki i wstrzymuje tryb na minutę z komunikatem na plakietce.
    private func scheduleModelOnly() {
        let checked = text, caret = caret
        // W przerwie po błędzie serwera każde pole mówi, dlaczego nic nie podkreśla — inaczej cisza wyglądałaby
        // jak tekst bez błędów.
        guard Date() >= modelOnlyPausedUntil else {
            modelOnlyNote = Self.modelOnlyPausedNote
            return
        }
        modelOnlyTask = Task {
            try? await Task.sleep(nanoseconds: Self.languageToolDelay)
            guard !Task.isCancelled, checked == text, !isSuspended() else { return }
            let started = Date()
            var rejected = 0, computed = 0
            let isCurrent = { [weak self] in !Task.isCancelled && self?.text == checked }
            do {
                let fragments = try await LiveChecker.shared.modelOnly(checked, caret: caret, onWait: { [weak self] waiting in
                    guard let self, isCurrent() || !waiting else { return }
                    if waiting { computed += 1 }
                    self.modelOnlyWaiting = waiting
                    self.showBadge(frame: nil)
                }) { result in
                    guard isCurrent() else { return }
                    if result.rejected { rejected += 1 }
                    modelOnlyIssues.removeAll { NSIntersectionRange($0.range, result.fragment.range).length > 0 }
                    modelOnlyIssues += result.issues
                    showBadge(frame: nil)
                }
                guard isCurrent() else { return }
                modelOnlyIssues.removeAll { issue in
                    !fragments.contains { NSIntersectionRange($0.range, issue.range).length > 0 }
                }
                modelOnlyNote = rejected == 0 ? nil
                    : rejected == 1 ? tr("1 zdanie bez sprawdzenia — model zwrócił niepewną odpowiedź.")
                    : tr("Zdania: {count} bez sprawdzenia — model zwrócił niepewną odpowiedź.", ["count": rejected])
                EventLog.write("live_model", ["chars": checked.count, "fragments": fragments.count, "requests": computed,
                                              "rejected": rejected, "issues": modelOnlyIssues.count,
                                              "ms": Int(Date().timeIntervalSince(started) * 1000)])
            } catch {
                guard !(error is CancellationError), isCurrent() else { return }
                modelOnlyPausedUntil = Date().addingTimeInterval(60)
                modelOnlyNote = Self.modelOnlyPausedNote
                EventLog.write("live_model_failed", ["chars": checked.count])
            }
            modelOnlyWaiting = false
            showBadge(frame: nil)
        }
    }

    private static var modelOnlyPausedNote: String {
        tr("{model} nie odpowiada — sprawdzanie wstrzymane na minutę.", ["model": Settings.shared.modelDisplayName])
    }

    /// Plakietka sama decyduje, co z tym zrobić: bez błędów i sprawdzania lawa zlewa się i znika, a bez lawy plakietka
    /// po prostu się chowa.
    private func showBadge(frame: NSRect?) {
        let issues = visibleIssues
        let checking = modelOnly ? modelOnlyWaiting : languageToolCheck != nil || bielikCheck != nil
        let note = modelOnly ? modelOnlyNote : nil
        badge.show(issues, checking: checking, note: note, in: frame)
        showUnderlines(issues)
    }

    /// Podkreślenia pod słowami — co tick, żeby nadążały za przewijaniem i przesuwaniem okna.
    /// Pozycje słów liczy kolejka AX (w Slacku to przejście po elementach tekstu pola).
    private func showUnderlines(_ issues: [LiveIssue]) {
        guard Settings.shared.liveUnderlinesEnabled, let snapshot, snapshot.frame != nil, !issues.isEmpty else {
            if !issues.isEmpty { log("podkreślenia: brak pola (widoczne \(snapshot?.frame.map { "\($0.integral)" } ?? "nil"))") }
            underlines.hide()
            return
        }
        guard !geometryInFlight else {
            geometryPending = true
            return
        }
        geometryInFlight = true
        if let browser = snapshot.browser {
            showBrowserUnderlines(issues, snapshot: snapshot, field: browser)
            return
        }
        guard let field = snapshot.field else {
            geometryInFlight = false
            return
        }
        let value = snapshot.value
        let app = snapshot.app
        let ranges = issues.map { NSRange(location: $0.range.location + snapshot.offset, length: $0.range.length) }
        Task {
            let started = Date()
            let rects = await AXQueue.run { () -> [[CGRect]]? in
                guard let fieldFrame = TextBridge.axFrame(of: field),
                      Date().timeIntervalSince(started) < AXQueue.slowThreshold else { return nil }
                return TextGeometry.wordRects(for: ranges, in: field, value: value, fieldFrame: fieldFrame)
            }
            Self.logIfSlow("geometry", since: started, app: app)
            geometryInFlight = false
            if geometryPending {
                // Błędy albo pole zmieniły się w trakcie — liczymy od nowa dla aktualnego stanu.
                geometryPending = false
                showUnderlines(visibleIssues)
                return
            }
            // Pole zmieniło się w trakcie (inne pole, nowy tekst, reset): zostawiamy, następny tick policzy od nowa.
            guard let current = self.snapshot, let currentField = current.field, CFEqual(currentField, field),
                  current.value == value else { return }
            guard let rects, let clip = current.frame else {
                log("podkreślenia: brak pozycji pola")
                underlines.hide()
                return
            }
            var marks: [UnderlineMark] = []
            for (issue, words) in zip(issues, rects) {
                for rect in words.map(TextBridge.cocoaRect) where clip.insetBy(dx: -1, dy: -1).contains(NSPoint(x: rect.midX, y: rect.midY)) {
                    marks.append(UnderlineMark(rect: rect, issue: issue))
                }
            }
            log("podkreślenia: \(issues.count) błędów, \(rects.map(\.count)) prostokątów, \(marks.count) znaków, widoczne \(clip.integral), prostokąty \(rects.flatMap { $0 }.prefix(3).map { TextBridge.cocoaRect($0).integral })")
            underlines.show(marks, clip: clip)
        }
    }

    /// Podkreślenia w polu strony: prostokąty słów z wtyczki, przeliczone na ekran przez aplikację.
    private func showBrowserUnderlines(_ issues: [LiveIssue], snapshot: Snapshot, field: BrowserField) {
        let ranges = issues.map { NSRange(location: field.base + snapshot.offset + $0.range.location, length: $0.range.length) }
        let value = snapshot.value
        Task {
            let received = await BrowserChannel.rects(field.browser, client: field.client, ranges: ranges)
            geometryInFlight = false
            if geometryPending {
                geometryPending = false
                showUnderlines(visibleIssues)
                return
            }
            guard let current = self.snapshot, current.browser == field, current.value == value else { return }
            guard case .reply(let reply) = received.outcome, reply.ok,
                  let words = received.raw["words"] as? [[[Double]]], words.count == issues.count,
                  let convert = BrowserChannel.screenConverter(received.raw),
                  let clipRect = received.raw["field"] as? [Double], clipRect.count == 4 else {
                log("podkreślenia: brak pozycji z wtyczki")
                underlines.hide()
                return
            }
            let clip = convert(clipRect)
            var marks: [UnderlineMark] = []
            for (issue, rects) in zip(issues, words) {
                for rect in rects where rect.count == 4 {
                    let screen = convert(rect)
                    if clip.insetBy(dx: -1, dy: -1).contains(NSPoint(x: screen.midX, y: screen.midY)) {
                        marks.append(UnderlineMark(rect: screen, issue: issue))
                    }
                }
            }
            log("podkreślenia (wtyczka): \(issues.count) błędów, \(marks.count) znaków, widoczne \(clip.integral)")
            underlines.show(marks, clip: clip)
        }
    }

    /// Widoczna część pola (współrzędne AX): przycięta do obszaru przewijania i okna (pole w TextEdit jest
    /// wyższe od okna). Na kolejce AX.
    nonisolated private static func visibleFrame(of field: AXUIElement) -> CGRect? {
        guard var frame = TextBridge.axFrame(of: field) else { return nil }
        var current = field
        for _ in 0..<6 {
            var parent: CFTypeRef?
            guard AXUIElementCopyAttributeValue(current, kAXParentAttribute as CFString, &parent) == .success,
                  let parent, CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
            current = parent as! AXUIElement
            let role = TextBridge.string(current, kAXRoleAttribute)
            if role == "AXScrollArea" || role == "AXWindow", let clip = TextBridge.axFrame(of: current) {
                frame = frame.intersection(clip)
            }
            if role == "AXWindow" { break }
        }
        return frame.isEmpty ? nil : frame
    }
}

// MARK: - Plakietka

/// Plakietka w prawym dolnym rogu pola: liczba błędów; po najechaniu nad nią lista, w której sugestia poprawia słowo,
/// a „Popraw…” — tak jak kliknięcie samej plakietki — otwiera panel poprawki (jak plakietka na Linuksie).
/// Póki sprawdzanie trwa, a liczby jeszcze nie ma, trzy lawendowe bańki dryfują jak wosk w lampie; z wynikiem zlewają się
/// w ceglane koło z liczbą, a bez błędów — zlewają się i znikają (`Lava`, PANEL.md; `show()` w `live.js` na Linuksie).
/// Nie zabiera fokusu ani klawiszy — aplikacja docelowa pisze dalej.
@MainActor
final class LiveBadge {
    var onClick: () -> Void = {}
    /// Sugestia z listy: poprawia jedno słowo (`LiveMonitor.apply`, ta sama droga co karta podkreślenia).
    var onFix: (LiveIssue, String) -> Void = { _, _ in }
    private var panel: NSPanel?
    private let model = LiveBadgeModel()
    private var field: NSRect?
    /// Koniec zlewania albo wskakiwania liczby: przestawia stan i zatrzymuje klatki.
    private var settleTask: Task<Void, Never>?

    var isVisible: Bool { panel?.isVisible ?? false }

    /// „Ogranicz ruch” (Dostępność): bańki stoją, wynik od razu, bez zlewania i wskakiwania liczby.
    private var animations: Bool { !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    /// `checking`: zapytanie do serwera w toku (LanguageTool, model w tle, tryb „tylko model”); `note`: komunikat
    /// o stanie sprawdzania (bez błędów plakietka pokazuje wtedy „!”).
    func show(_ issues: [LiveIssue], checking: Bool = false, note: String? = nil, in frame: NSRect?) {
        if let frame { field = frame }
        guard let field else { return }
        let now = LiveBadgeModel.now()
        let lamp = model.lamp.settled(at: now)
            .next(count: issues.count, note: note != nil, checking: checking, animations: animations, now: now)
        guard lamp != .hidden else { return conceal() }
        if model.issues != issues { model.issues = issues }
        if model.checking != checking { model.checking = checking }
        if model.note != note { model.note = note }
        if model.lamp != lamp { model.lamp = lamp }
        sync(now)
        let panel = self.panel ?? makePanel()
        place(panel, in: field)
        if !panel.isVisible { panel.orderFrontRegardless() }
    }

    /// Inne pole, inna aplikacja, kliknięcie: plakietka znika i zapomina pole.
    func hide() {
        conceal()
        field = nil
    }

    /// Nic do pokazania w tym samym polu: plakietka znika, ale pamięta, gdzie stoi — lawa z następnego sprawdzania
    /// pojawi się od razu, a nie dopiero przy kolejnym odczycie pola.
    private func conceal() {
        settleTask?.cancel()
        settleTask = nil
        if model.lamp != .hidden { model.lamp = .hidden }
        if model.animating { model.animating = false }
        if model.expanded { model.expanded = false }
        panel?.orderOut(nil)
    }

    /// Klatki tylko wtedy, gdy coś się rusza; koniec zlewania i wskakiwania liczby przestawia stan sam (koło z liczbą
    /// albo, bez błędów, schowana plakietka).
    private func sync(_ now: Double) {
        settleTask?.cancel()
        settleTask = nil
        let animating = animations && model.lamp.moves(checking: model.checking, at: now)
        if model.animating != animating { model.animating = animating }
        guard let next = model.lamp.change(after: now) else { return }
        settleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, next - now) * 1_000_000) + 5_000_000)
            guard !Task.isCancelled, let self else { return }
            let now = LiveBadgeModel.now()
            let lamp = model.lamp.settled(at: now)
            guard lamp != .hidden else { return conceal() }
            if model.lamp != lamp { model.lamp = lamp }
            sync(now)
        }
    }

    private func makePanel() -> NSPanel {
        let hosting = BadgeHostingView(rootView: LiveBadgeView(
            model: model,
            open: { [weak self] in self?.onClick() },
            fix: { [weak self] issue, suggestion in
                // Jak na Linuksie: po wyborze lista się chowa, plakietka zostaje i pokaże wynik ponownego sprawdzenia.
                guard let self else { return }
                model.expanded = false
                if let panel = self.panel, let field = self.field { place(panel, in: field) }
                onFix(issue, suggestion)
            }))
        hosting.onHover = { [weak self] inside in
            guard let self else { return }
            model.expanded = inside && model.lamp.text != nil
            if let panel = self.panel, let field = self.field { place(panel, in: field) }
        }
        let panel = BadgePanel(contentRect: NSRect(x: 0, y: 0, width: 30, height: 22),
                               styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        panel.contentView = hosting
        panel.isOpaque = false
        panel.backgroundColor = .clear
        // Cień tylko pod listą: cień okna liczy się z kształtu treści, a bańki zmieniają go co klatkę.
        panel.hasShadow = false
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        self.panel = panel
        return panel
    }

    /// Prawy dolny róg pola; rozwinięta lista rośnie w górę i w lewo od tego rogu.
    private func place(_ panel: NSPanel, in field: NSRect) {
        guard let hosting = panel.contentView else { return }
        hosting.layoutSubtreeIfNeeded()
        let size = hosting.fittingSize
        let inset: CGFloat = field.height < 34 ? 3 : 6
        var origin = NSPoint(x: field.maxX - size.width - inset, y: field.minY + inset)
        if let visible = (NSScreen.screens.first { $0.frame.intersects(field) } ?? NSScreen.main)?.visibleFrame {
            origin.x = min(max(origin.x, visible.minX + 4), visible.maxX - size.width - 4)
            origin.y = min(max(origin.y, visible.minY + 4), visible.maxY - size.height - 4)
        }
        let frame = NSRect(origin: origin, size: size)
        if panel.frame != frame { panel.setFrame(frame, display: true) }
        if panel.hasShadow != model.expanded { panel.hasShadow = model.expanded }
        if model.expanded { panel.invalidateShadow() }
    }
}

private final class BadgePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Kliknięcie bez aktywowania GramaDramy (panel bez fokusu, pierwsze kliknięcie od razu trafia do przycisków)
/// i najechanie także wtedy, gdy na wierzchu jest inna aplikacja. Kliknięcia obsługują przyciski widoku.
private final class BadgeHostingView<Content: View>: NSHostingView<Content> {
    var onHover: (Bool) -> Void = { _ in }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.filter { $0.owner === self && $0.userInfo?["badge"] != nil }.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: ["badge": true]))
    }

    override func mouseEntered(with event: NSEvent) { onHover(true) }
    override func mouseExited(with event: NSEvent) { onHover(false) }
}

@MainActor
final class LiveBadgeModel: ObservableObject {
    typealias Lamp = Lava.Lamp

    @Published var issues: [LiveIssue] = []
    @Published var expanded = false
    /// Zapytanie do serwera w toku (LanguageTool, model w tle albo tryb „tylko model”; wynik z pamięci go nie zapala).
    @Published var checking = false
    /// Stan sprawdzania do pokazania: serwer nie odpowiada, część zdań bez sprawdzenia.
    @Published var note: String?
    @Published var lamp = Lamp.hidden
    /// Klatki płyną (bańki, zlewanie, wskakiwanie, „oddech”); inaczej plakietka stoi w stanie końcowym.
    @Published var animating = false
    /// Zamrożony czas (ms) dla zrzutów `--preview-panel`.
    var clock: Double?

    static func now() -> Double { Date().timeIntervalSinceReferenceDate * 1000 }
}

struct LiveBadgeView: View {
    @ObservedObject var model: LiveBadgeModel
    /// Panel poprawki (⌥⌘G): plakietka i „Popraw…”.
    var open: () -> Void = {}
    var fix: (LiveIssue, String) -> Void = { _, _ in }

    /// Rozwinięta: lista nad plakietką, która zostaje na swoim miejscu i dalej otwiera panel (jak na Linuksie).
    var body: some View {
        if model.expanded {
            VStack(alignment: .trailing, spacing: 6) {
                list
                pill
            }
        } else {
            pill
        }
    }

    private var pill: some View {
        Button(action: open) { LavaLamp(model: model) }
            .buttonStyle(.plain)
            .help([model.checking ? tr("Sprawdzam…") : summary, model.note].compactMap { $0 }.joined(separator: "\n"))
            .accessibilityLabel(model.lamp.text == nil ? tr("Sprawdzam…") : summary)
    }

    private var list: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Text(model.checking ? summary + " · " + tr("sprawdzam…") : summary).font(.callout.bold())
                Spacer(minLength: 0)
                // To, co robi kliknięcie plakietki (⌥⌘G, panel); wielokropek mówi, że to otwiera panel,
                // a nie poprawia samo.
                if !model.issues.isEmpty {
                    Button(tr("Popraw…"), action: open).buttonStyle(BrandButtonStyle(compact: true))
                        .help(tr("Popraw w panelu ({shortcut})", ["shortcut": Settings.shared.correctionShortcut.label]))
                }
            }
            if let note = model.note {
                Label(note, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Brand.rose)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(model.issues.prefix(5).enumerated()), id: \.offset) { _, issue in
                VStack(alignment: .leading, spacing: 2) {
                    // Słowo, strzałka i sugestie w jednej linii, dopóki się mieszczą; inaczej sugestie jedna pod drugą.
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 6) {
                            Text(issue.word).waveUnderline().fixedSize()
                            if !issue.suggestions.isEmpty {
                                Image(systemName: "arrow.right").font(.caption2).foregroundStyle(Brand.secondaryInk)
                                HStack(spacing: 6) { fixes(issue) }.fixedSize()
                            }
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(issue.word).waveUnderline()
                            fixes(issue)
                        }
                    }
                    .font(.callout)
                    Text(tr(issue.message)).font(.caption).foregroundStyle(Brand.secondaryInk).lineLimit(2)
                }
            }
            if model.issues.count > 5 {
                Text(tr("i {count} więcej", ["count": model.issues.count - 5])).font(.caption).foregroundStyle(Brand.secondaryInk)
            }
        }
        .padding(12)
        .frame(width: 280, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
        .foregroundStyle(Brand.ink)
        .background(Brand.surface.opacity(0.98), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Brand.line))
    }

    /// Sugestie słowa (do trzech, jak na karcie podkreślenia): wybór poprawia to jedno słowo.
    @ViewBuilder private func fixes(_ issue: LiveIssue) -> some View {
        ForEach(Array(issue.suggestions.prefix(3).enumerated()), id: \.offset) { index, suggestion in
            Button(suggestion) { fix(issue, suggestion) }
                .buttonStyle(SuggestionButtonStyle(first: index == 0))
                .help(tr("Zamień „{word}” na „{suggestion}”", ["word": issue.word, "suggestion": suggestion]))
        }
    }

    private var summary: String {
        UI.count(model.issues.count, ("błąd", "błędy", "błędów"), ("issue", "issues"))
    }
}

/// Lawa na plakietce (`Lava`, PANEL.md): lawendowe bańki z mostkami między bliskimi, zlewanie w koło (pigułkę przy
/// dwóch cyfrach), lawenda przechodząca w cegłę i wskakująca liczba. Klatki tylko wtedy, gdy coś się rusza
/// (`animating`); w stanie końcowym wszystko stoi.
private struct LavaLamp: View {
    @ObservedObject var model: LiveBadgeModel
    @Environment(\.colorScheme) private var scheme

    // Kolory jak na Linuksie (BRAND.md): lawenda, kiedy aplikacja pracuje, cegła dla tego, co znalazła.
    private static let lavender = (light: rgb(0x66579E), dark: rgb(0xBCB0EC))
    private static let brick = rgb(0xAD4E2C)
    private static let brickHover = rgb(0x9A4426)

    private static func rgb(_ hex: Int) -> [Double] {
        [16, 8, 0].map { Double((hex >> $0) & 0xFF) / 255 }
    }

    /// Jedna klatka: bańki albo kształt, ich kolor i krycie, tekst i jego wskakiwanie.
    private struct Frame {
        var width: Double = 24
        var blobs: [Lava.Blob] = []
        var shape: Double?
        var colour: [Double]
        var alpha: Double = 1
        var text: String?
        var pop: Double = 1
    }

    var body: some View {
        TimelineView(.animation(paused: !model.animating || model.clock != nil)) { context in
            let frame = frame(at: model.clock ?? context.date.timeIntervalSinceReferenceDate * 1000)
            ZStack {
                Canvas { context, _ in draw(frame, in: &context) }
                if let text = frame.text {
                    Text(text)
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.white)
                        .scaleEffect(0.5 + 0.5 * Lava.easeOutBack(frame.pop))
                        .opacity(min(1, Lava.easeOutBack(frame.pop)))
                }
            }
            .frame(width: frame.width, height: Lava.height)
            .contentShape(Rectangle())
        }
    }

    /// `_repaint` z `live.js`. Bez klatek (`animating` wyłączone) — stan końcowy, bez względu na zegar.
    private func frame(at now: Double) -> Frame {
        let moving = model.animating || model.clock != nil
        let lavender = scheme == .dark ? Self.lavender.dark : Self.lavender.light
        switch model.lamp {
        case .hidden:
            return Frame(colour: lavender, alpha: 0)
        case .thinking(let began):
            return Frame(blobs: Lava.drift(moving ? now - began : 0), colour: lavender)
        case .merging(let began, let at, let into):
            let shape = Lava.shapeWidth(text: into ?? "1"), width = Lava.areaWidth(text: into ?? "1")
            let elapsed = moving ? now - at : Lava.mergeMS + Lava.fadeMS
            var frame = Frame(width: width, colour: lavender, text: into, pop: (elapsed - Lava.mergeMS) / Lava.numberMS)
            if elapsed < Lava.mergeMS {
                frame.blobs = Lava.merge(Lava.drift(at - began, width: width), progress: elapsed / Lava.mergeMS,
                                         shape: shape, width: width)
            } else {
                frame.shape = shape
                if into != nil {
                    frame.colour = Lava.mix(lavender, Self.brick, (elapsed - Lava.mergeMS) / Lava.tintMS)
                } else {
                    frame.alpha = max(0, 1 - (elapsed - Lava.mergeMS) / Lava.fadeMS)
                }
            }
            return frame
        case .count(let text, let popped):
            // Pod kursorem ciemniejsza cegła; najechanie śledzi panel (`BadgeHostingView`) i ono też rozwija listę.
            var frame = Frame(width: Lava.areaWidth(text: text), shape: Lava.shapeWidth(text: text),
                              colour: model.expanded ? Self.brickHover : Self.brick, text: text)
            if moving, let popped { frame.pop = (now - popped) / Lava.numberMS }
            if moving, model.checking { frame.alpha = Lava.breath(now) }
            return frame
        }
    }

    private func draw(_ frame: Frame, in context: inout GraphicsContext) {
        let c = frame.colour
        let fill = Color(.sRGB, red: c[0], green: c[1], blue: c[2], opacity: frame.alpha)
        for b in frame.blobs {
            context.fill(Path(ellipseIn: CGRect(x: b.x - b.r, y: b.y - b.r, width: 2 * b.r, height: 2 * b.r)), with: .color(fill))
        }
        for b in Lava.bridges(frame.blobs) {
            let point = { (p: (Double, Double)) in CGPoint(x: p.0, y: p.1) }
            var path = Path()
            path.move(to: point(b.p1))
            path.addCurve(to: point(b.p3), control1: point(b.h1), control2: point(b.h3))
            path.addLine(to: point(b.p4))
            path.addCurve(to: point(b.p2), control1: point(b.h4), control2: point(b.h2))
            path.closeSubpath()
            context.fill(path, with: .color(fill))
        }
        if let shape = frame.shape {
            // Koło albo pigułka; jasna obwódka 35% odcina je od ciemnego albo ceglanego tła aplikacji.
            let rect = CGRect(x: (frame.width - shape) / 2, y: 0, width: shape, height: Lava.height)
            context.fill(Path(roundedRect: rect, cornerRadius: Lava.radius), with: .color(fill))
            context.stroke(Path(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), cornerRadius: Lava.radius - 0.5),
                           with: .color(.white.opacity(0.35 * frame.alpha)), lineWidth: 1)
        }
    }
}
