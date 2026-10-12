import AppKit
import ApplicationServices
import Carbon.HIToolbox
import os
import GramaCore
import GramaBackend
import GramaIMEProtocol

struct CapturedText {
    enum Scope {
        case selection, paragraph, newMessage, wholeField

        var label: String {
            switch self {
            case .selection: "zaznaczenie"
            case .paragraph: "akapit"
            case .newMessage: "nowa wiadomość (bez cytatów)"
            case .wholeField: "całe pole"
            }
        }

        /// Etykieta w stopce panelu — `label` zostaje po polsku, bo trafia do dziennika zdarzeń.
        var title: String { tr(label) }
    }
    let text: String
    let scope: Scope
    let app: NSRunningApplication?
    /// Ramka kursora tekstu albo pola w układzie współrzędnych Cocoa (do pozycjonowania panelu).
    let anchor: NSRect?
    /// false, gdy tekst pochodzi z miejsca tylko do odczytu (historia Slacka, podgląd maila, terminal)
    /// albo zawiera załączniki — wtedy panel proponuje tylko „Kopiuj".
    let canApply: Bool
    /// Pole natywne (AX podaje pełny tekst): gdzie w polu leży `text` (jednostki UTF-16, jak w AX).
    /// Dzięki temu podmieniamy tylko zmieniony fragment i sprawdzamy, czy pole się w międzyczasie nie zmieniło.
    var element: AXUIElement? = nil
    var range: CFRange? = nil
    /// Skąd tekst (do logu zdarzeń): ax-selection, copy-selection, ax-paragraph, ax-field, web-region, select-all.
    var source = ""
    /// Pole webowe (Outlook, Mail): fragment wyznaczony w tekście z AX (`range`), a skopiowany przez aplikację.
    /// `axText` to ten fragment w wersji AX — do sprawdzenia przed podmianą, że pole się nie zmieniło.
    var axText: String? = nil
    /// Pobrane przez metodę wprowadzania: podmiana idzie tą samą drogą (nigdy AX ani ⌘V — inne przesunięcia).
    var ime: ImeTarget? = nil
    /// Pobrane przez wtyczkę przeglądarki: podmiana wraca przez wtyczkę (zakres w jej układzie, UTF-16 pola).
    var browser: BrowserTarget? = nil
}

/// Fragment pobrany przez wtyczkę przeglądarki: zakres w tekście pola i token pola (karta, ramka, element).
struct BrowserTarget {
    let range: NSRange
    let client: String?
    let browser: String
}

/// Fragment pobrany przez metodę wprowadzania: zakres w dokumencie (UTF-16, układ IMK) i token pola.
struct ImeTarget {
    let range: NSRange
    let client: String?
    /// WebKit (Outlook, Mail) przesuwa kursor na poprawione słowo — po podmianie wracamy nim na miejsce.
    let restoreCaret: Bool
}

enum CaptureError: LocalizedError {
    /// Krótki kod do logu zdarzeń.
    var code: String {
        switch self {
        case .notTrusted: "no_permission"
        case .nothingFound: "nothing_found"
        case .tooLong: "too_long"
        case .attachments: "attachments"
        case .busy: "busy"
        case .sensitive: "password_field"
        case .emptyParagraph: "empty_line"
        case .selectFragment: "quoted_thread"
        case .selectText: "select_text"
        }
    }

    case notTrusted, nothingFound, tooLong(Int), attachments, busy, sensitive, emptyParagraph, selectFragment, selectText

    var errorDescription: String? {
        switch self {
        case .notTrusted: tr("GramaDrama potrzebuje uprawnienia Dostępność (Ustawienia → Prywatność i ochrona).")
        case .nothingFound: tr("Nie znalazłem tekstu. Kliknij w pole tekstowe albo zaznacz fragment.")
        case .tooLong(let n): tr("Tekst ma {count} znaków — zaznacz krótszy fragment (maks. {limit}).", ["count": n, "limit": TextBridge.maxChars])
        case .attachments: tr("To pole zawiera obrazy lub załączniki — zaznacz sam tekst, żeby ich nie skasować.")
        case .busy: tr("Poprzednia operacja jeszcze trwa.")
        case .sensitive: tr("To pole hasła — GramaDrama go nie czyta.")
        case .emptyParagraph: tr("Kursor stoi w pustej linii — kliknij w akapit z tekstem albo zaznacz fragment.")
        case .selectFragment: tr("To pole zawiera cytowaną korespondencję — zaznacz fragment, który mam poprawić.")
        case .selectText: tr("Ta aplikacja nie pokazuje tekstu pola — zaznacz fragment, który mam poprawić.")
        }
    }
}

/// Pobieranie i podmiana tekstu w dowolnej aplikacji.
/// Najpierw Accessibility API, a gdy aplikacja go nie wspiera — schowek (⌘C / ⌘A / ⌘V).
/// Wszystko na głównym wątku i jedna operacja naraz: dwie równoległe operacje na schowku
/// nadpisywałyby sobie nawzajem jego zawartość.
@MainActor
enum TextBridge {
    nonisolated static let maxChars = 6000
    private static var operationInProgress = false
    /// Strażnicy spóźnionego ⌘C (watchForLateCopy) — dopóki działają, nie ruszamy schowka.
    private static var lateCopyWatchers = 0
    static var isBusy: Bool { operationInProgress || lateCopyWatchers > 0 }

    nonisolated private static let singleLineRoles: Set<String> = ["AXTextField", "AXComboBox", "AXSearchField"]
    /// Terminale wystawiają bufor jako AXTextArea — całe pole to historia poleceń, nie wiadomość.
    static let terminalBundleIDs = ImePolicy.terminalBundleIDs
    /// Aplikacje, które nie wystawiają tekstu przez AX: PowerPoint podaje jako fokus cały panel slajdu
    /// (AXLayoutArea bez dzieci, z pozornym zaznaczeniem 0+0) także w trakcie edycji pola. Tam pomijamy AX:
    /// ⌘C zaznaczenia i ⌘V, bez całego pola i bez sprawdzania na żywo.
    nonisolated static let textlessBundleIDs: Set<String> = ["com.microsoft.Powerpoint"]

    nonisolated static func isTextless(_ app: NSRunningApplication?) -> Bool {
        app?.bundleIdentifier.map(textlessBundleIDs.contains) ?? false
    }

    /// Aplikacje (pid), w których już włączyliśmy AXManualAccessibility (`manual`), i te, które go przyjęły,
    /// czyli Electron/Chromium (`electron`). Pod blokadą — czytane także z kolejki AX.
    nonisolated private static let accessibilityPIDs = OSAllocatedUnfairLock(
        initialState: (manual: Set<pid_t>(), electron: Set<pid_t>()))

    nonisolated static func isTrusted(prompt: Bool) -> Bool {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    // MARK: - Pobieranie

    static func capture() async throws -> CapturedText {
        let bundleID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""
        do {
            let captured = try await captureText()
            EventLog.write("capture", ["app": bundleID, "source": captured.source, "scope": captured.scope.label,
                                       "chars": captured.text.count, "canApply": captured.canApply])
            return captured
        } catch let error as CaptureError {
            EventLog.write("capture_refused", ["app": bundleID, "reason": error.code])
            throw error
        }
    }

    private static func captureText() async throws -> CapturedText {
        guard !isBusy else { throw CaptureError.busy }
        operationInProgress = true
        defer { operationInProgress = false }
        guard isTrusted(prompt: false) else { throw CaptureError.notTrusted }

        let app = NSWorkspace.shared.frontmostApplication
        // 0. Wtyczka przeglądarki: pełny tekst pola strony prosto z DOM, bez AX i bez ⌘C.
        if let captured = try await captureViaBrowser(app) { return captured }
        // 0. Metoda wprowadzania GramaDrama: tekst pola bez AX i bez ⌘C (także PowerPoint, którego AX nie pokazuje).
        if let captured = try await captureViaIme(app) { return captured }
        await waitForModifiersRelease()

        let element = await app.asyncMap { await focusedElement(in: $0) } ?? nil
        // Pole hasła: nie wysyłamy nawet ⌘C (niektóre aplikacje kopiują zamaskowaną wartość).
        if let element, isSecure(element) { throw CaptureError.sensitive }
        // Kotwica przy kursorze tekstu (albo zaznaczeniu), a gdy AX jej nie poda — przy całym polu.
        let fieldFrame = element.flatMap(frame)
        let anchor = element.flatMap { caretFrame(of: $0, within: fieldFrame) } ?? fieldFrame
        let terminal = (app?.bundleIdentifier.map(terminalBundleIDs.contains) ?? false)
            || (element.map(isWebTerminal) ?? false)
        let editable = element.map(isEditable) ?? false
        // Pola webowe (Slack, Outlook, Electron, contenteditable): AX gubi emoji-obrazki i fragmenty
        // poza ekranem, więc tekst bierzemy z kopiowania samej aplikacji. AX tylko mówi, czy jest zaznaczenie.
        let preferClipboard = element.map(isWebContent) ?? true

        func result(_ text: String, _ scope: CapturedText.Scope, canApply: Bool, attachments: Bool = false,
                    range: CFRange? = nil, source: String) throws -> CapturedText {
            guard text.count <= maxChars else { throw CaptureError.tooLong(text.count) }
            // Obraz/załącznik (U+FFFC albo obrazek w kopii) — wklejenie zwykłego tekstu by go skasowało.
            let hasAttachments = attachments || text.contains("\u{FFFC}")
            if hasAttachments, scope != .selection { throw CaptureError.attachments }
            return CapturedText(text: text, scope: scope, app: app, anchor: anchor,
                                canApply: canApply && !hasAttachments,
                                element: range == nil ? nil : element, range: range, source: source)
        }
        // Bez elementu AX nie wiemy, czy miejsce jest edytowalne — ufamy użytkownikowi, który zaznaczył tekst.
        let selectionCanApply = !terminal && (element == nil || editable)

        // 1. Zaznaczenie z Accessibility API (tylko natywne pola — tam AX podaje pełny tekst).
        if !preferClipboard, let element, let selected = string(element, kAXSelectedTextAttribute), !selected.isBlank {
            // Zakres zaznaczenia zapamiętujemy tylko, gdy AX jest spójne (wartość w zakresie = zaznaczony tekst).
            let range = selectedRange(element).flatMap { r -> CFRange? in
                guard let value = string(element, kAXValueAttribute) as NSString?, r.location + r.length <= value.length,
                      value.substring(with: NSRange(location: r.location, length: r.length)) == selected else { return nil }
                return r
            }
            return try result(selected, .selection, canApply: selectionCanApply && editable, range: editable ? range : nil,
                              source: "ax-selection")
        }

        // 2. ⌘C, chyba że AX potwierdza brak zaznaczenia (zakres o długości 0) — wtedy ⌘C tylko by
        //    zapiszczało, a w VS Code skopiowało całą linię.
        var copyTimedOut = false
        let noSelectionConfirmed = element.flatMap(selectedRange)?.length == 0
        if !noSelectionConfirmed {
            switch await copyViaClipboard() {
            case .copied(let text, let attachments) where !text.isBlank:
                // PowerPoint dokłada do kopii tekstu jego obraz (PNG/TIFF) — to nie załącznik. Obraz na slajdzie
                // to osobny kształt, a w tekście pola i tak zostałby U+FFFC.
                return try result(text, .selection, canApply: selectionCanApply, attachments: attachments && !isTextless(app),
                                  source: "copy-selection")
            case .timedOut:
                copyTimedOut = true
            default:
                break
            }
        }

        // 3. Całe pole — tylko w polu edycyjnym i nigdy w terminalu.
        if isTextless(app) { throw CaptureError.selectText }
        guard let element, editable, !terminal else { throw CaptureError.nothingFound }
        if !preferClipboard, let value = string(element, kAXValueAttribute) {
            // Pole jest czytelne i puste — nie ma czego poprawiać (i nie robimy ⌘A w ciemno).
            guard !value.isBlank else { throw CaptureError.nothingFound }
            let ns = value as NSString
            // Akapit z kursorem (jak na Linuksie), a nie całe pole — długa notatka czy mail nie trafiają
            // w całości do modelu i nie są w całości podmieniane.
            if let caret = selectedRange(element), caret.length == 0, caret.location <= ns.length {
                let paragraph = TextEdits.paragraphRange(in: value, caret: caret.location)
                let text = ns.substring(with: paragraph)
                guard !text.isBlank else { throw CaptureError.emptyParagraph }
                let whole = paragraph.location == 0 && paragraph.length == ns.length
                return try result(text, whole ? .wholeField : .paragraph, canApply: true,
                                  range: CFRange(location: paragraph.location, length: paragraph.length), source: "ax-paragraph")
            }
            return try result(value, .wholeField, canApply: true, range: CFRange(location: 0, length: ns.length), source: "ax-field")
        }

        // 3b. Pole webowe z cytowaną korespondencją (Outlook, Mail) albo bardzo długie: nie całe pole, tylko
        //     nowa wiadomość (od początku do cytatu), a gdy kursor stoi w cytacie — akapit z kursorem.
        guard !copyTimedOut else { throw CaptureError.nothingFound }
        if let value = string(element, kAXValueAttribute), let caret = selectedRange(element), caret.length == 0 {
            let ns = value as NSString
            let newMessage = ReplyBoundary.newMessageRange(in: value)
            if newMessage != nil || ns.length > maxChars {
                var candidates: [(NSRange, CapturedText.Scope)] = []
                if let newMessage, caret.location <= newMessage.location + newMessage.length {
                    candidates.append((newMessage, .newMessage))
                }
                let paragraph = TextEdits.paragraphRange(in: value, caret: caret.location)
                if !ns.substring(with: paragraph).isBlank, !candidates.contains(where: { $0.0 == paragraph }) {
                    candidates.append((paragraph, .paragraph))
                }
                for (range, scope) in candidates {
                    if let captured = try await captureWebRegion(
                        element, range: range, axText: ns.substring(with: range), scope: scope, restoring: caret,
                        result: { text, scope, canApply, attachments, range, source in
                            try result(text, scope, canApply: canApply, attachments: attachments, range: range, source: source)
                        }) {
                        return captured
                    }
                }
                throw candidates.isEmpty ? CaptureError.emptyParagraph : CaptureError.selectFragment
            }
        }

        // 4. ⌘A + ⌘C.
        let savedRange = selectedRange(element)
        sendShortcut("a")
        try? await Task.sleep(nanoseconds: 80_000_000)
        let copied = await copyViaClipboard()
        // Nie zostawiamy całego pola zaznaczonego — następny wpisany znak skasowałby wiadomość.
        restoreSelection(element, savedRange)
        if case .copied(let text, let attachments) = copied, !text.isBlank {
            return try result(text, .wholeField, canApply: true, attachments: attachments, source: "select-all")
        }
        throw CaptureError.nothingFound
    }

    /// Zaznacza fragment przez AX, kopiuje go kopiowaniem samej aplikacji (emoji, wykrywanie obrazków)
    /// i przywraca kursor. nil, gdy aplikacja nie przyjęła zaznaczenia, kopia się nie zgadza albo fragment
    /// zawiera obrazki (wtedy próbujemy mniejszego fragmentu).
    private static func captureWebRegion(
        _ element: AXUIElement, range: NSRange, axText: String, scope: CapturedText.Scope, restoring caret: CFRange,
        result: (String, CapturedText.Scope, Bool, Bool, CFRange?, String) throws -> CapturedText
    ) async throws -> CapturedText? {
        guard !axText.isBlank else { return nil }
        let cfRange = CFRange(location: range.location, length: range.length)
        guard setSelectedRange(element, cfRange) else { return nil }
        try? await Task.sleep(nanoseconds: 60_000_000)
        let copied = await copyViaClipboard()
        restoreSelection(element, caret)
        guard case .copied(let text, let attachments) = copied, !text.isBlank,
              PostProcess.fold(text) == PostProcess.fold(axText), !attachments else { return nil }
        var captured = try result(text, scope, true, false, cfRange, "web-region")
        captured.axText = axText
        return captured
    }

    /// Diagnostyka dla --e2e: co AX mówi o elemencie z fokusem w aplikacji na wierzchu (bez treści pola).
    static func debugDescribeFocus() async -> String {
        guard let app = NSWorkspace.shared.frontmostApplication else { return "brak aplikacji na wierzchu" }
        guard let element = await focusedElement(in: app) else { return "brak elementu z fokusem" }
        var names: CFArray?
        AXUIElementCopyAttributeNames(element, &names)
        let value = string(element, kAXValueAttribute)
        let range = selectedRange(element)
        return "rola=\(string(element, kAXRoleAttribute) ?? "?") web=\(isWebContent(element)) edytowalne=\(isEditable(element)) "
            + "AXValue=\(value.map { "\(($0 as NSString).length) zn." } ?? "brak") zakres=\(range.map { "\($0.location)+\($0.length)" } ?? "brak") "
            + "atrybuty=\((names as? [String])?.joined(separator: ",") ?? "?")"
    }

    // MARK: - Podmiana

    enum ReplaceOutcome {
        case pasted, copiedToClipboard, busy
        /// Poprawka zmienia podział na linie, a metoda wprowadzania nie odtworzy ⇧↩/↩ aplikacji — tekst w schowku.
        case lineBreaksChanged
        /// Tekst w polu zmienił się od pobrania — nie wklejamy, poprawka zostaje w schowku.
        case fieldChanged
        /// ⌘V nie dotarło: fragment jest nadal zaznaczony, a w schowku leży dokładnie jego poprawka.
        case pasteNotLanded
        /// Po wklejeniu pole wygląda inaczej, niż oczekiwaliśmy — nie wiemy, co się stało.
        case uncertain
        /// Wynik nie nadaje się do wklejenia (pusty, z obiektem U+FFFC albo NUL).
        case badResult
    }

    /// Wkleja poprawiony tekst w miejsce oryginału. Wklejanie (zamiast AXValue) działa w Electronie
    /// i zostawia normalne ⌘Z w aplikacji docelowej. W polach natywnych podmieniamy tylko zmieniony
    /// fragment — mniejszy krok cofania i formatowanie poza zmianą zostaje.
    static func replace(_ captured: CapturedText, with text: String) async -> ReplaceOutcome {
        let outcome = await replaceText(captured, with: text)
        EventLog.write("replace", ["app": captured.app?.bundleIdentifier ?? "", "outcome": "\(outcome)",
                                   "native": captured.range != nil, "scope": captured.scope.label])
        return outcome
    }

    private static func replaceText(_ captured: CapturedText, with text: String) async -> ReplaceOutcome {
        guard !operationInProgress else {
            setClipboard(text)
            return .busy
        }
        // Strażnik spóźnionego ⌘C z pobierania nie może cofnąć naszego wklejenia — sami robimy kopię schowka.
        cancelLateCopyWatchers()
        operationInProgress = true
        defer { operationInProgress = false }

        guard !text.isBlank, !text.contains("\u{FFFC}"), !text.contains("\u{0}") else { return .badResult }
        // Użytkownik przełączył się do innej aplikacji — nie wklejamy w ciemno, zostawiamy tekst w schowku.
        guard captured.canApply,
              captured.app == nil || NSWorkspace.shared.frontmostApplication?.processIdentifier
                == captured.app?.processIdentifier else {
            setClipboard(text)
            return .copiedToClipboard
        }

        // Tekst z wtyczki albo metody wprowadzania wraca tą samą drogą — ⌘V wkleiłoby go obok oryginału.
        if let browser = captured.browser {
            return await replaceViaBrowser(captured, browser, with: text)
        }
        if let ime = captured.ime {
            return await replaceViaIme(captured, ime, with: text)
        }
        if let element = captured.element, let range = captured.range, let axText = captured.axText {
            return await replaceWebRegion(element, range: range, axText: axText, with: text)
        }
        if let element = captured.element, let range = captured.range {
            return await replaceInNativeField(element, range: range, original: captured.text, with: text)
        }

        // Pola webowe: zaznaczenie, które zniknęło (np. po utracie fokusu), dałoby dopisanie zamiast podmiany.
        if captured.scope == .selection, let app = captured.app, !isTextless(app),
           let element = focusedElement(of: AXUIElementCreateApplication(app.processIdentifier)),
           selectedRange(element)?.length == 0 {
            setClipboard(text)
            return .fieldChanged
        }
        await paste(text, selectAllFirst: captured.scope == .wholeField)
        return .pasted
    }

    private static func replaceInNativeField(_ element: AXUIElement, range: CFRange, original: String,
                                             with text: String) async -> ReplaceOutcome {
        // Kontrola przed wklejeniem: w polu nadal jest dokładnie to, co pobraliśmy.
        guard let value = string(element, kAXValueAttribute) as NSString?,
              range.location + range.length <= value.length,
              value.substring(with: NSRange(location: range.location, length: range.length)) == original else {
            setClipboard(text)
            return .fieldChanged
        }
        guard let span = TextEdits.minimalSpan(from: original, to: text) else { return .pasted }
        let target = CFRange(location: range.location + span.prefix, length: span.oldLength)
        guard setSelectedRange(element, target) else {
            setClipboard(text)
            return .fieldChanged
        }
        if span.replacement.isEmpty {
            sendKey(CGKeyCode(kVK_Delete), flags: [])
            try? await Task.sleep(nanoseconds: 150_000_000)
        } else {
            await paste(span.replacement, selectAllFirst: false, restoreAfter: false)
        }
        // Kontrola po wklejeniu: poprawka naprawdę jest w polu.
        let expected = span.replacement as NSString
        let expectedLength = value.length - span.oldLength + expected.length
        func landed() -> Bool {
            guard let after = string(element, kAXValueAttribute) as NSString?, after.length == expectedLength else { return false }
            return after.substring(with: NSRange(location: target.location, length: expected.length)) == span.replacement
        }
        for _ in 0..<10 {
            if landed() {
                restorePendingPaste()
                return .pasted
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        // Aplikacja mogła jeszcze nie obsłużyć ⌘V (zajęty wątek). W schowku leży sam poprawiony fragment,
        // który pasuje do zaznaczenia — nigdy cały akapit, bo spóźnione ⌘V wkleiłoby go w środek zdania.
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        if landed() {
            restorePendingPaste()
            return .pasted
        }
        let oldSpan = (original as NSString).substring(with: NSRange(location: span.prefix, length: span.oldLength))
        let stillOriginal = (string(element, kAXValueAttribute) as NSString?).map {
            target.location + target.length <= $0.length
                && $0.substring(with: NSRange(location: target.location, length: target.length)) == oldSpan
        } ?? false
        let stillSelected = selectedRange(element).map { $0.location == target.location && $0.length == target.length } ?? false
        if stillOriginal, stillSelected, !span.replacement.isEmpty {
            // Wklejenie nie dotarło — fragment dalej zaznaczony, a ⌘V użytkownika podmieni dokładnie go.
            discardPendingPaste()
            return .pasteNotLanded
        }
        // Stan nieznany (pole zmieniło się w trakcie) — oddajemy użytkownikowi jego schowek.
        restorePendingPaste()
        return .uncertain
    }

    // MARK: - Wstawianie (dyktowanie)

    enum InsertOutcome {
        case inserted
        /// Tekst został w schowku: inna aplikacja na wierzchu, brak pola tekstowego albo pole hasła.
        case appChanged, noTextField, secureField, busy
        /// Tekst w schowku: kursor jest w innym polu tej samej aplikacji niż na początku dyktowania.
        case fieldChanged
        /// Metoda wprowadzania mogła wstawić tekst (albo zrobi to za chwilę) — nie wklejamy drugi raz; tekst też w schowku.
        case uncertain
        case badResult
    }

    /// Wkleja podyktowany tekst w miejscu kursora (albo w miejsce zaznaczenia), bez ⌘A.
    /// Gdy tuż przed kursorem stoi słowo, dokleja spację.
    static func insert(_ text: String, into app: NSRunningApplication?, imeClient: String? = nil) async -> InsertOutcome {
        guard !operationInProgress else {
            setClipboard(text)
            return .busy
        }
        cancelLateCopyWatchers()
        operationInProgress = true
        defer { operationInProgress = false }

        guard !text.isBlank, !text.contains("\u{FFFC}"), !text.contains("\u{0}") else { return .badResult }
        let front = NSWorkspace.shared.frontmostApplication
        // Użytkownik przełączył się w trakcie rozpoznawania — nie wklejamy w ciemno.
        guard let front, app == nil || front.processIdentifier == app?.processIdentifier else {
            setClipboard(text)
            return .appChanged
        }
        if let outcome = await insertViaBrowser(text, into: front) { return outcome }
        if let outcome = await insertViaIme(text, into: front, client: imeClient) { return outcome }
        let element = await focusedElement(in: front)
        if let element, isSecure(element) {
            setClipboard(text)
            return .secureField
        }
        // Fokus na przycisku albo liście: ⌘V zrobiłoby tam coś innego (np. w Finderze).
        // Bez elementu AX (aplikacja go nie wystawia) ufamy użytkownikowi, jak przy pobieraniu.
        if let element, !isTextElement(element) {
            setClipboard(text)
            return .noTextField
        }
        let previous = element.flatMap(characterBeforeCaret)
        let spaced = TextEdits.leadingSpaceNeeded(before: text, after: previous) ? " " + text : text
        await paste(spaced, selectAllFirst: false)
        return .inserted
    }

    // MARK: - Metoda wprowadzania

    /// ⌥⌘G przez metodę wprowadzania, gdy jest wybrana, a aplikacja jest w `ImePolicy`.
    /// nil = zwykła droga (IME niedostępna, aplikacja nie oddała tekstu, akapit ucięty, emoji w fragmencie).
    private static func captureViaIme(_ app: NSRunningApplication?) async throws -> CapturedText? {
        guard let app, let rule = ImeChannel.rule(for: app), rule.capture else { return nil }
        let started = Date()
        let outcome = await ImeChannel.context()
        ImeChannel.log("context", outcome, app: app, started: started)
        guard case .reply(let reply) = outcome else { return nil }
        if reply.reason == "secure_input" { throw CaptureError.sensitive }
        guard reply.ok, reply.app == app.bundleIdentifier else { return nil }
        guard let pick = try pickCandidate(reply, wholeFieldWhenComplete: rule.wholeFieldWhenComplete, app: app,
                                           fallbackEvent: "ime_fallback") else { return nil }
        // Obraz albo emoji w zaznaczeniu: podmiana zwykłym tekstem by je skasowała — tylko „Kopiuj”.
        let hasAttachments = pick.text.contains("\u{FFFC}")
        return CapturedText(text: pick.text, scope: capturedScope(pick.scope), app: app,
                            anchor: ImeChannel.anchor(reply.caret, app: app),
                            canApply: !hasAttachments, source: "ime-\(pick.scope.rawValue)",
                            ime: ImeTarget(range: pick.range, client: reply.client, restoreCaret: rule.restoreCaret))
    }

    /// Najlepszy fragment z odpowiedzi IME albo wtyczki. nil = zwykła droga (powód w `fallbackEvent`).
    private static func pickCandidate(_ reply: ImeProtocol.Reply, wholeFieldWhenComplete: Bool, app: NSRunningApplication,
                                      fallbackEvent: String) throws -> ImeScope.Candidate? {
        let candidates: [ImeScope.Candidate]
        do {
            candidates = try ImeScope.candidates(reply, wholeFieldWhenComplete: wholeFieldWhenComplete)
        } catch ImeScope.Refusal.emptyParagraph {
            throw CaptureError.emptyParagraph
        } catch ImeScope.Refusal.tooLong(let length) {
            throw CaptureError.tooLong(length)
        } catch {
            EventLog.write(fallbackEvent, ["app": app.bundleIdentifier ?? "", "reason": "\(error)"])
            return nil
        }
        guard let pick = candidates.first(where: { $0.text.count <= maxChars }) else {
            throw CaptureError.tooLong(candidates.first?.text.count ?? 0)
        }
        return pick
    }

    private static func capturedScope(_ scope: ImeScope) -> CapturedText.Scope {
        switch scope {
        case .selection: .selection
        case .newMessage: .newMessage
        case .paragraph: .paragraph
        case .wholeField: .wholeField
        }
    }

    // MARK: - Wtyczka przeglądarki

    /// ⌥⌘G przez wtyczkę, gdy ta przeglądarka ma ją połączoną. nil = zwykła droga (fokus poza polem strony,
    /// np. w pasku adresu, strona bez wtyczki, brak odpowiedzi).
    private static func captureViaBrowser(_ app: NSRunningApplication?) async throws -> CapturedText? {
        guard let app, let browser = app.bundleIdentifier, BrowserChannel.isConnected(browser) else { return nil }
        let started = Date()
        let outcome = await BrowserChannel.context(browser)
        ImeChannel.log("context", outcome, app: app, started: started, event: "browser_request")
        guard case .reply(let reply) = outcome else { return nil }
        if reply.reason == "secure_input" { throw CaptureError.sensitive }
        guard reply.ok, let pick = try pickCandidate(reply, wholeFieldWhenComplete: false, app: app,
                                                     fallbackEvent: "browser_fallback") else { return nil }
        let hasAttachments = pick.text.contains("\u{FFFC}")
        return CapturedText(text: pick.text, scope: capturedScope(pick.scope), app: app,
                            anchor: ImeChannel.anchor(reply.caret, app: app),
                            canApply: !hasAttachments, source: "browser-\(pick.scope.rawValue)",
                            browser: BrowserTarget(range: pick.range, client: reply.client, browser: browser))
    }

    /// Podmiana przez wtyczkę: słowami, z kontrolą, że fragment w polu się nie zmienił (jak przez IME).
    private static func replaceViaBrowser(_ captured: CapturedText, _ target: BrowserTarget, with text: String) async -> ReplaceOutcome {
        let plan: (edits: [ImeProtocol.Edit], expected: String)
        do {
            guard let planned = try ImeEdits.plan(original: captured.text, corrected: text, base: target.range.location) else { return .pasted }
            plan = planned
        } catch {
            setClipboard(text)
            return .lineBreaksChanged
        }
        let started = Date()
        let check = ImeProtocol.Edit(location: target.range.location, length: target.range.length, text: captured.text)
        let outcome = await BrowserChannel.replace(target.browser, client: target.client, check: check,
                                                   edits: plan.edits, expected: plan.expected)
        ImeChannel.log("replace", outcome, app: captured.app, started: started,
                       extra: ["edits": plan.edits.count], event: "browser_request")
        switch ImeChannel.classify(outcome) {
        case .landed:
            return .pasted
        case .refused:
            setClipboard(text)
            return .fieldChanged
        case .unknown:
            setClipboard(text)
            return .uncertain
        }
    }

    /// Dyktowanie przez wtyczkę: znak przed kursorem z `context` (odstęp liczy aplikacja), potem wstawienie.
    /// nil = zwykła droga, tylko gdy wtyczka na pewno nic nie wstawiła.
    private static func insertViaBrowser(_ text: String, into app: NSRunningApplication) async -> InsertOutcome? {
        guard let browser = app.bundleIdentifier, BrowserChannel.isConnected(browser) else { return nil }
        let started = Date()
        let context = await BrowserChannel.context(browser)
        ImeChannel.log("context", context, app: app, started: started, event: "browser_request")
        guard case .reply(let reply) = context else { return nil }
        if reply.reason == "secure_input" {
            setClipboard(text)
            return .secureField
        }
        guard reply.ok else { return nil }
        let previous = (reply.selectionLength ?? 0) == 0 ? reply.before?.last : nil
        let spaced = ImeText.leadingSpaceNeeded(before: text, after: previous) ? " " + text : text
        let outcome = await BrowserChannel.insert(browser, text: spaced, client: reply.client)
        ImeChannel.log("insert", outcome, app: app, started: started, event: "browser_request")
        switch ImeChannel.classify(outcome) {
        case .landed:
            return .inserted
        case .refused(let reason):
            if reason == "client_changed" {
                setClipboard(text)
                return .fieldChanged
            }
            return nil
        case .unknown:
            setClipboard(text)
            return .uncertain
        }
    }

    /// Podmiana przez metodę wprowadzania: słowami, z kontrolą, że fragment w polu się nie zmienił.
    private static func replaceViaIme(_ captured: CapturedText, _ ime: ImeTarget, with text: String) async -> ReplaceOutcome {
        let plan: (edits: [ImeProtocol.Edit], expected: String)
        do {
            guard let planned = try ImeEdits.plan(original: captured.text, corrected: text, base: ime.range.location) else { return .pasted }
            plan = planned
        } catch {
            // Model zmienił podział na linie — nie wiemy, jak przenieść ⇧↩/↩ aplikacji. Użytkownik wklei sam.
            setClipboard(text)
            return .lineBreaksChanged
        }
        guard ImeChannel.isSourceSelected() else {
            setClipboard(text)
            return .copiedToClipboard
        }
        let started = Date()
        let check = ImeProtocol.Edit(location: ime.range.location, length: ime.range.length, text: captured.text)
        let outcome = await ImeChannel.replace(client: ime.client, app: captured.app?.bundleIdentifier, check: check,
                                               edits: plan.edits, expected: plan.expected)
        ImeChannel.log("replace", outcome, app: captured.app, started: started, extra: ["edits": plan.edits.count])
        switch ImeChannel.classify(outcome) {
        case .landed:
            if ime.restoreCaret, case .reply(let reply) = outcome, let app = captured.app {
                await restoreCaret(after: reply, check: check, newLength: (plan.expected as NSString).length, in: app)
            }
            return .pasted
        case .refused:
            setClipboard(text)
            return .fieldChanged
        case .unknown:
            setClipboard(text)
            return .uncertain
        }
    }

    /// WebKit po podmianie stawia kursor na poprawionym fragmencie. Jeśli użytkownik pisał dalej za nim,
    /// przesuwamy kursor przez AX o różnicę — przesunięcie jest względne, więc nie mieszamy układów AX i IMK.
    private static func restoreCaret(after reply: ImeProtocol.Reply, check: ImeProtocol.Edit, newLength: Int,
                                     in app: NSRunningApplication) async {
        guard let before = reply.caretBefore, let after = reply.caretAfter else { return }
        let end = check.location + check.length
        let wanted: Int
        if before >= end { wanted = before + newLength - check.length } else if before < check.location { wanted = before } else { return }
        guard wanted != after, let element = await focusedElement(in: app), let current = selectedRange(element) else { return }
        let target = current.location + (wanted - after)
        guard target >= 0 else { return }
        let moved = await setSelectedRangeWaiting(element, CFRange(location: target, length: 0))
        EventLog.write("ime_caret", ["app": app.bundleIdentifier ?? "", "moved": moved, "shift": wanted - after])
    }

    /// Dyktowanie przez metodę wprowadzania: jedna operacja (odstęp liczony w IME tuż przed wstawieniem).
    /// nil = zwykła droga (⌘V), tylko gdy IME na pewno nic nie wstawiła.
    private static func insertViaIme(_ text: String, into app: NSRunningApplication, client: String?) async -> InsertOutcome? {
        guard let rule = ImeChannel.rule(for: app), rule.insert else { return nil }
        let started = Date()
        let outcome = await ImeChannel.insert(text, client: client)
        ImeChannel.log("insert", outcome, app: app, started: started)
        switch ImeChannel.classify(outcome) {
        case .landed:
            return .inserted
        case .refused(let reason):
            switch reason {
            case "secure_input":
                setClipboard(text)
                return .secureField
            case "client_changed":
                setClipboard(text)
                return .fieldChanged
            default:
                return nil
            }
        case .unknown:
            setClipboard(text)
            return .uncertain
        }
    }

    /// Znak tuż przed kursorem (nil na początku pola albo gdy AX nie podaje spójnej wartości).
    private static func characterBeforeCaret(_ element: AXUIElement) -> Character? {
        guard let value = string(element, kAXValueAttribute) as NSString?, let range = selectedRange(element),
              range.location > 0, range.location <= value.length else { return nil }
        let previous = value.rangeOfComposedCharacterSequence(at: range.location - 1)
        return value.substring(with: previous).first
    }

    /// Pole webowe: zaznaczamy przez AX dokładnie pobrany fragment (reszta pola, np. cytowane maile,
    /// zostaje nietknięta) i wklejamy poprawkę w jego miejsce.
    private static func replaceWebRegion(_ element: AXUIElement, range: CFRange, axText: String,
                                         with text: String) async -> ReplaceOutcome {
        // Powód odmowy trafia do logu (bez tekstu) — w Slacku i Outlooku każdy z nich wygląda dla użytkownika tak samo.
        func refuse(_ reason: String) -> ReplaceOutcome {
            EventLog.write("replace_refused", ["reason": reason])
            setClipboard(text)
            return .fieldChanged
        }
        guard let value = string(element, kAXValueAttribute) as NSString? else { return refuse("no_value") }
        guard range.location + range.length <= value.length else { return refuse("out_of_range") }
        guard value.substring(with: NSRange(location: range.location, length: range.length)) == axText else { return refuse("text_differs") }
        guard await setSelectedRangeWaiting(element, range) else { return refuse("select_failed") }
        await paste(text, selectAllFirst: false)
        // Sprawdzenie po wklejeniu: od początku fragmentu stoi teraz poprawka. Dokładnie dla jednej linii
        // (porównanie bez polskich liter zgłosiłoby sukces przy „cos” → „coś”, gdy nic się nie wkleiło);
        // dla dłuższych fragmentów bez interpunkcji i odstępów — edytor może inaczej zapisać końce linii.
        var after: NSString?
        for _ in 0..<10 {
            after = string(element, kAXValueAttribute) as NSString?
            if let after, after != value { break }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        guard let after, range.location <= after.length else { return .uncertain }
        let window = after.substring(with: NSRange(location: range.location,
                                                   length: min(after.length - range.location, (text as NSString).length + 64)))
        if !text.contains(where: \.isNewline) { return window.hasPrefix(text) ? .pasted : .uncertain }
        return PostProcess.fold(window).hasPrefix(PostProcess.fold(text)) ? .pasted : .uncertain
    }

    /// Schowek sprzed ostatniego wklejenia, gdy przywrócenie czeka na weryfikację.
    private static var pendingRestore: (items: [NSPasteboardItem], written: Int)?

    private static func paste(_ text: String, selectAllFirst: Bool, restoreAfter: Bool = true) async {
        let pasteboard = NSPasteboard.general
        let saved = snapshotPasteboard()
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        pasteboard.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.TransientType"))
        let written = pasteboard.changeCount
        if selectAllFirst {
            sendShortcut("a")
            try? await Task.sleep(nanoseconds: 80_000_000)
        }
        sendShortcut("v")
        guard restoreAfter else {
            pendingRestore = (saved, written)
            try? await Task.sleep(nanoseconds: 150_000_000)
            return
        }
        // Aplikacja czyta schowek asynchronicznie (Slack po IPC) — dajemy jej czas. Tekst jest już
        // wklejony, więc użytkownik tej chwili nie odczuwa.
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        // Przywracamy tylko, jeśli nikt w międzyczasie nie skopiował czegoś nowego.
        if pasteboard.changeCount == written { restorePasteboard(saved) }
    }

    private static func restorePendingPaste() {
        guard let pending = pendingRestore else { return }
        pendingRestore = nil
        if NSPasteboard.general.changeCount == pending.written { restorePasteboard(pending.items) }
    }

    private static func discardPendingPaste() {
        pendingRestore = nil
    }

    // MARK: - Accessibility

    /// Element z fokusem — odczyt na kolejce AX, żeby zawieszona aplikacja nie blokowała głównego wątku.
    static func focusedElement(in app: NSRunningApplication) async -> AXUIElement? {
        guard !isTextless(app) else { return nil }
        let pid = app.processIdentifier
        return await AXQueue.run { focusedElementBlocking(pid: pid) }
    }

    /// Jak `focusedElement(in:)`, ale synchronicznie — tylko na kolejce AX (czeka do ~1,2 s na drzewo AX).
    nonisolated static func focusedElementBlocking(pid: pid_t) -> AXUIElement? {
        let axApp = AXUIElementCreateApplication(pid)
        var justEnabled = false
        if !accessibilityPIDs.withLock({ $0.manual.contains(pid) }) {
            // Aplikacje Electron (Slack, Teams…) budują drzewo AX dopiero na wyraźną prośbę.
            let status = AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
            justEnabled = status == .success
            let accepted = justEnabled
            accessibilityPIDs.withLock { state in
                state.manual.insert(pid)
                if accepted { state.electron.insert(pid) }
            }
        }

        var (element, error) = focusedElementResult(of: axApp)
        // Zawieszona aplikacja: każda kolejna próba to znowu pełny limit czasu.
        guard error != .cannotComplete else { return nil }
        // WebKit (Outlook, Mail) — podobnie jak Electron — buduje drzewo AX dopiero po pierwszym zapytaniu.
        for _ in 0..<12 where element == nil {
            usleep(50_000)
            (element, error) = focusedElementResult(of: axApp)
            if error == .cannotComplete { return nil }
        }
        if justEnabled {
            // Chromium buduje drzewo asynchronicznie — czekamy (maks. ~600 ms), aż fokus trafi na pole tekstowe.
            for _ in 0..<12 where !(element.map(isTextElement) ?? false) {
                usleep(50_000)
                element = focusedElement(of: axApp)
            }
        }
        return element
    }

    nonisolated private static func focusedElement(of axApp: AXUIElement) -> AXUIElement? {
        focusedElementResult(of: axApp).element
    }

    /// Z kodem błędu: `.cannotComplete` znaczy, że aplikacja nie odpowiedziała w limicie czasu (zawieszona).
    nonisolated private static func focusedElementResult(of axApp: AXUIElement) -> (element: AXUIElement?, error: AXError) {
        var focused: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(axApp, kAXFocusedUIElementAttribute as CFString, &focused)
        guard error == .success, let focused, CFGetTypeID(focused) == AXUIElementGetTypeID() else { return (nil, error) }
        return ((focused as! AXUIElement), error)
    }

    nonisolated static func string(_ element: AXUIElement, _ attribute: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
        return value as? String
    }

    nonisolated private static func hasAttribute(_ element: AXUIElement, _ attribute: String) -> Bool {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success && value != nil
    }

    nonisolated static func isSecure(_ element: AXUIElement) -> Bool {
        string(element, kAXRoleAttribute) == "AXSecureTextField"
            || string(element, kAXSubroleAttribute) == (kAXSecureTextFieldSubrole as String)
    }

    /// Treść webowa: element w contenteditable (Chromium/WebKit wystawiają AXEditableAncestor)
    /// albo aplikacja Electron (przyjęła AXManualAccessibility).
    nonisolated static func isWebContent(_ element: AXUIElement) -> Bool {
        if hasAttribute(element, "AXEditableAncestor") { return true }
        var pid: pid_t = 0
        if AXUIElementGetPid(element, &pid) == .success {
            let owner = pid
            if accessibilityPIDs.withLock({ $0.electron.contains(owner) }) { return true }
        }
        // WebKit (nowy Outlook, Mail, Safari): korzeń contenteditable wymienia AXEditableAncestor
        // (z pustą wartością) albo leży w AXWebArea. Natywne NSTextView nie robi żadnego z tych.
        var names: CFArray?
        if AXUIElementCopyAttributeNames(element, &names) == .success,
           let list = names as? [String], list.contains("AXEditableAncestor") { return true }
        var current = element
        for _ in 0..<6 {
            var parent: CFTypeRef?
            guard AXUIElementCopyAttributeValue(current, kAXParentAttribute as CFString, &parent) == .success,
                  let parent, CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
            current = parent as! AXUIElement
            if string(current, kAXRoleAttribute) == "AXWebArea" { return true }
        }
        return false
    }

    /// Terminal w przeglądarce/Electronie (VS Code, Hyper): xterm.js łapie klawisze w ukrytym textarea.
    nonisolated static func isWebTerminal(_ element: AXUIElement) -> Bool {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, "AXDOMClassList" as CFString, &value) == .success,
              let classes = value as? [String] else { return false }
        return classes.contains { $0.contains("xterm") }
    }

    nonisolated private static func isTextElement(_ element: AXUIElement) -> Bool {
        isEditable(element) || string(element, kAXSelectedTextAttribute) != nil
    }

    /// Edytowalne = da się ustawić wartość, element leży w contenteditable (Chromium/WebKit wystawiają
    /// AXEditableAncestor) albo to jednowierszowe pole. Sama rola AXTextArea nie wystarcza —
    /// tak wygląda też bufor Terminala czy podgląd logów.
    nonisolated static func isEditable(_ element: AXUIElement) -> Bool {
        var settable: DarwinBoolean = false
        if AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable) == .success,
           settable.boolValue { return true }
        if hasAttribute(element, "AXEditableAncestor") { return true }
        if let role = string(element, kAXRoleAttribute), singleLineRoles.contains(role) { return true }
        return false
    }

    nonisolated static func selectedRange(_ element: AXUIElement) -> CFRange? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var range = CFRange()
        return AXValueGetValue(value as! AXValue, .cfRange, &range) ? range : nil
    }

    /// Ustawia zaznaczenie przez AX i sprawdza, że aplikacja je przyjęła.
    /// Jak `setSelectedRange`, ale czeka (do ~0,4 s), aż aplikacja przyjmie zaznaczenie. Chromium (Slack)
    /// przyjmuje je asynchronicznie — natychmiastowy odczyt podaje jeszcze stary kursor.
    private static func setSelectedRangeWaiting(_ element: AXUIElement, _ range: CFRange) async -> Bool {
        var target = range
        guard let value = AXValueCreate(.cfRange, &target),
              AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value) == .success else { return false }
        for _ in 0..<8 {
            if let now = selectedRange(element), now.location == range.location, now.length == range.length { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return false
    }

    private static func setSelectedRange(_ element: AXUIElement, _ range: CFRange) -> Bool {
        var range = range
        guard let value = AXValueCreate(.cfRange, &range),
              AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value) == .success,
              let now = selectedRange(element) else { return false }
        return now.location == range.location && now.length == range.length
    }

    private static func restoreSelection(_ element: AXUIElement, _ range: CFRange?) {
        if var range, let value = AXValueCreate(.cfRange, &range),
           AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value) == .success {
            return
        }
        sendKey(CGKeyCode(kVK_RightArrow), flags: [])  // zwija zaznaczenie na koniec
    }

    /// Prostokąt kursora tekstu / zaznaczenia (kAXBoundsForRange). Odrzucamy wartości bez sensu
    /// (zerowe, ogromne, poza polem) — Electron potrafi zwrócić śmieci.
    private static func caretFrame(of element: AXUIElement, within field: NSRect?) -> NSRect? {
        guard let selection = selectedRange(element) else { return nil }
        let candidates = [CFRange(location: selection.location, length: max(selection.length, 1)),
                          CFRange(location: max(selection.location - 1, 0), length: 1)]
        for var range in candidates {
            guard let value = AXValueCreate(.cfRange, &range) else { continue }
            var result: CFTypeRef?
            guard AXUIElementCopyParameterizedAttributeValue(element, kAXBoundsForRangeParameterizedAttribute as CFString,
                                                             value, &result) == .success,
                  let result, CFGetTypeID(result) == AXValueGetTypeID() else { continue }
            var rect = CGRect.zero
            guard AXValueGetValue(result as! AXValue, .cgRect, &rect), rect.height > 0, rect.height < 200 else { continue }
            let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
            let cocoa = NSRect(x: rect.minX, y: primaryHeight - rect.maxY, width: max(rect.width, 1), height: rect.height)
            if let field, !field.insetBy(dx: -20, dy: -20).intersects(cocoa) { continue }
            guard NSScreen.screens.contains(where: { $0.frame.intersects(cocoa) }) else { continue }
            return cocoa
        }
        return nil
    }

    static func frame(of element: AXUIElement) -> NSRect? {
        axFrame(of: element).map(cocoaRect)
    }

    /// AX liczy od lewego górnego rogu głównego ekranu, Cocoa od lewego dolnego.
    static func cocoaRect(_ rect: CGRect) -> NSRect {
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return NSRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }

    /// Położenie elementu we współrzędnych AX — bez NSScreen, więc także na kolejce AX.
    nonisolated static func axFrame(of element: AXUIElement) -> CGRect? {
        var positionRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &positionRef) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeRef) == .success,
              let positionRef, let sizeRef else { return nil }
        var position = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(positionRef as! AXValue, .cgPoint, &position)
        AXValueGetValue(sizeRef as! AXValue, .cgSize, &size)
        guard size.width > 0, size.height > 0 else { return nil }
        return CGRect(origin: position, size: size)
    }

    // MARK: - Klawiatura

    private static func waitForModifiersRelease() async {
        for _ in 0..<25 {
            if NSEvent.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty { return }
            try? await Task.sleep(nanoseconds: 40_000_000)
        }
    }

    /// ⌘ + litera wg bieżącego układu klawiatury (na AZERTY „a" leży pod innym klawiszem niż na QWERTY —
    /// stały kod kVK_ANSI_A wysłałby tam ⌘Q i zamknął aplikację).
    private static func sendShortcut(_ character: Character) {
        let fallback: [Character: Int] = ["a": kVK_ANSI_A, "c": kVK_ANSI_C, "v": kVK_ANSI_V]
        guard let code = KeyboardLayout.keyCode(for: character) ?? fallback[character].map({ CGKeyCode($0) }) else { return }
        sendKey(code, flags: .maskCommand)
    }

    private static func sendKey(_ keyCode: CGKeyCode, flags: CGEventFlags) {
        // Prywatne źródło — fizycznie wciśnięte modyfikatory nie mieszają się z naszymi.
        let source = CGEventSource(stateID: .privateState)
        for keyDown in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: keyDown)
            event?.flags = flags
            event?.post(tap: .cghidEventTap)
        }
    }

    // MARK: - Schowek

    private enum CopyResult { case copied(String, attachments: Bool), timedOut }

    private static func copyViaClipboard() async -> CopyResult {
        let pasteboard = NSPasteboard.general
        let saved = snapshotPasteboard()
        let before = pasteboard.changeCount
        sendShortcut("c")
        for _ in 0..<12 where pasteboard.changeCount == before {
            try? await Task.sleep(nanoseconds: 40_000_000)
        }
        guard pasteboard.changeCount != before else {
            watchForLateCopy(since: before, restoring: saved)
            return .timedOut
        }
        let copied = pasteboard.string(forType: .string) ?? ""
        let attachments = clipboardHasAttachments(pasteboard)
        restorePasteboard(saved)
        return .copied(copied, attachments: attachments)
    }

    /// Obrazki i załączniki w kopii (poza emoji — Slack wstawia je jako <img> z kodem emoji).
    private static func clipboardHasAttachments(_ pasteboard: NSPasteboard) -> Bool {
        let types = Set(pasteboard.types ?? [])
        if !types.isDisjoint(with: [.png, .tiff, .fileURL, NSPasteboard.PasteboardType("public.jpeg")]) { return true }
        if let html = pasteboard.string(forType: .html),
           let images = try? NSRegularExpression(pattern: #"<img\b[^>]*>"#, options: [.caseInsensitive]) {
            let ns = html as NSString
            let hasRealImage = images.matches(in: html, range: NSRange(location: 0, length: ns.length)).contains { match in
                let tag = ns.substring(with: match.range).lowercased()
                return !(tag.contains("emoji") || tag.range(of: #"alt="\s*:[a-z0-9_+\-]+:\s*""#, options: .regularExpression) != nil)
            }
            if hasRealImage { return true }
        }
        if let rtfd = pasteboard.data(forType: .rtfd),
           let attributed = NSAttributedString(rtfd: rtfd, documentAttributes: nil) {
            var found = false
            attributed.enumerateAttribute(.attachment, in: NSRange(location: 0, length: attributed.length)) { value, _, stop in
                if value != nil { found = true; stop.pointee = true }
            }
            if found { return true }
        }
        return false
    }

    /// Zajęta aplikacja może obsłużyć ⌘C z opóźnieniem i nadpisać schowek, gdy już nie patrzymy.
    /// Przez 2 s pilnujemy tego i przywracamy poprzednią zawartość; do tego czasu blokujemy nowe pobieranie.
    /// Własny zapis GramaDramy (Kopiuj, wklejanie) unieważnia strażników — nie cofną naszej poprawki.
    private static var watcherGeneration = 0

    private static func watchForLateCopy(since changeCount: Int, restoring saved: [NSPasteboardItem]) {
        lateCopyWatchers += 1
        let generation = watcherGeneration
        Task { @MainActor in
            defer { lateCopyWatchers = max(0, lateCopyWatchers - 1) }
            for _ in 0..<50 {
                try? await Task.sleep(nanoseconds: 40_000_000)
                guard generation == watcherGeneration else { return }
                if NSPasteboard.general.changeCount != changeCount {
                    restorePasteboard(saved)
                    return
                }
            }
        }
    }

    private static func cancelLateCopyWatchers() {
        watcherGeneration += 1
        lateCopyWatchers = 0
    }

    /// Zapis do schowka z panelu (Kopiuj) — przez TextBridge, żeby strażnik spóźnionego ⌘C go nie cofnął.
    static func setClipboard(_ text: String) {
        cancelLateCopyWatchers()
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private static func snapshotPasteboard() -> [NSPasteboardItem] {
        (NSPasteboard.general.pasteboardItems ?? []).map { item in
            let copy = NSPasteboardItem()
            for type in item.types {
                if let data = item.data(forType: type) { copy.setData(data, forType: type) }
            }
            return copy
        }
    }

    private static func restorePasteboard(_ items: [NSPasteboardItem]) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        if !items.isEmpty { pasteboard.writeObjects(items) }
    }
}

/// Mapowanie znak → kod klawisza w bieżącym układzie (ASCII-capable, bo tego używa macOS dla skrótów z ⌘).
enum KeyboardLayout {
    static func keyCode(for character: Character) -> CGKeyCode? {
        guard let source = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(),
              let pointer = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) else { return nil }
        let data = Unmanaged<CFData>.fromOpaque(pointer).takeUnretainedValue() as Data
        let target = String(character).lowercased()
        return data.withUnsafeBytes { raw -> CGKeyCode? in
            guard let layout = raw.baseAddress?.assumingMemoryBound(to: UCKeyboardLayout.self) else { return nil }
            for code in 0..<128 {
                var deadKeyState: UInt32 = 0
                var characters = [UniChar](repeating: 0, count: 4)
                var length = 0
                // Ze stanem ⌘ — tak aplikacja czyta skrót (np. „Dvorak – QWERTY ⌘" przełącza się wtedy na QWERTY).
                let commandState = UInt32((cmdKey >> 8) & 0xFF)
                let status = UCKeyTranslate(layout, UInt16(code), UInt16(kUCKeyActionDown), commandState, UInt32(LMGetKbdType()),
                                            OptionBits(kUCKeyTranslateNoDeadKeysMask), &deadKeyState,
                                            characters.count, &length, &characters)
                if status == noErr, length == 1, String(utf16CodeUnits: characters, count: 1).lowercased() == target {
                    return CGKeyCode(code)
                }
            }
            return nil
        }
    }
}

private extension Optional {
    func asyncMap<T>(_ transform: (Wrapped) async -> T) async -> T? {
        guard let self else { return nil }
        return await transform(self)
    }
}
