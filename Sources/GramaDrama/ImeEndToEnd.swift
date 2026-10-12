import AppKit
import Carbon
import GramaCore
import GramaIMEProtocol

/// `open -n -W ~/Applications/GramaDrama.app --args --e2e-ime <plik>` — test metody wprowadzania na prawdziwym
/// IMK i prawdziwym gnieździe, we własnym oknie. Na kilka sekund wybiera źródło „Polski (GramaDrama)”
/// (musi być włączone: `--ime-enable`), a na końcu przywraca poprzednie. Prosi IME wprost (z pominięciem
/// `ImePolicy`, która własne okna GramaDramy kieruje do AX) — odpowiedzi idą z wątku w tle, więc główny
/// wątek odpowiada IME jako klient i nic się nie blokuje.
@MainActor
enum ImeEndToEnd {
    static func run(output: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.mainMenu = AppDelegate.buildEditMenu()

        let window = NSWindow(contentRect: NSRect(x: 240, y: 240, width: 560, height: 360),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "GramaDrama — test metody wprowadzania"
        let stack = NSStackView()
        stack.orientation = .vertical
        let first = NSTextView(frame: NSRect(x: 0, y: 0, width: 540, height: 120))
        let second = NSTextView(frame: NSRect(x: 0, y: 0, width: 540, height: 120))
        let secure = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 540, height: 24))
        for view in [first, second] {
            view.allowsUndo = true
            view.isRichText = false
            view.heightAnchor.constraint(equalToConstant: 120).isActive = true
        }
        [first, second, secure].forEach(stack.addArrangedSubview)
        window.contentView = stack
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        window.makeFirstResponder(first)

        var log: [String] = []
        var failures = 0
        func check(_ name: String, _ ok: Bool, _ detail: String = "") {
            log.append((ok ? "✓ " : "✗ ") + name + (detail.isEmpty ? "" : " — \(detail)"))
            if !ok { failures += 1 }
        }
        let previous = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue()
        func finish() -> Never {
            if let previous { TISSelectInputSource(previous) }
            log.append(failures == 0 ? "\nWszystko OK" : "\nBłędy: \(failures)")
            try? log.joined(separator: "\n").write(toFile: output, atomically: true, encoding: .utf8)
            exit(failures == 0 ? 0 : 1)
        }

        func place(_ view: NSTextView, _ text: String, caret: Int? = nil, selection: NSRange? = nil) async {
            window.makeFirstResponder(view)
            view.string = text
            view.setSelectedRange(selection ?? NSRange(location: caret ?? (text as NSString).length, length: 0))
            try? await Task.sleep(nanoseconds: 150_000_000)
        }

        /// Kontekst, gdy IME już widzi nasze okno (po zmianie fokusu aktywacja przychodzi asynchronicznie).
        var lastContext = ""
        func context() async -> ImeProtocol.Reply? {
            for _ in 0..<20 {
                let outcome = await ImeChannel.context()
                switch outcome {
                case .reply(let reply):
                    if reply.ok, reply.app == ImeProtocol.appBundleID { return reply }
                    lastContext = "ok=\(reply.ok) reason=\(reply.reason ?? "-") app=\(reply.app ?? "-")"
                case .unavailable(let reason): lastContext = "unavailable(\(reason))"
                case .noReply: lastContext = "noReply"
                }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            return nil
        }

        Task { @MainActor in
            for _ in 0..<30 where NSWorkspace.shared.frontmostApplication != .current {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            guard NSWorkspace.shared.frontmostApplication == .current else {
                check("okno testowe na wierzchu", false, "macOS nie dał fokusu — nie dotykaj Maca w trakcie testu")
                finish()
            }
            guard let ime = ImeSetup.imeSource, ImeSetup.flag(ime, kTISPropertyInputSourceIsEnabled) else {
                check("źródło włączone", false, "najpierw --ime-enable")
                finish()
            }
            TISSelectInputSource(ime)
            try? await Task.sleep(nanoseconds: 400_000_000)
            check("źródło wybrane", ImeChannel.isSourceSelected())

            // 1. Odczyt akapitu z kursorem na końcu.
            let original = "Wysylam poprawiona wersje raportu."
            await place(first, original)
            guard let reply = await context() else {
                check("IME odpowiada dla okna testowego", false, lastContext)
                finish()
            }
            check("odczyt: tekst przed kursorem", reply.before == original, "\((reply.before ?? "").count) znaków")
            check("odczyt: koniec dokumentu", reply.shortAfter == true)
            let candidates = (try? ImeScope.candidates(reply, wholeFieldWhenComplete: false)) ?? []
            check("zakres: całe pole", candidates.first?.scope == .wholeField)

            // 2. Podmiana słowami, kursor zostaje na końcu.
            let corrected = "Wysyłam poprawioną wersję raportu."
            if let candidate = candidates.first, let plan = try? ImeEdits.plan(original: candidate.text, corrected: corrected, base: candidate.range.location) {
                let outcome = await ImeChannel.replace(client: reply.client, app: ImeProtocol.appBundleID, check: ImeProtocol.Edit(location: candidate.range.location, length: candidate.range.length, text: candidate.text),
                                                       edits: plan.edits, expected: plan.expected)
                if case .reply(let r) = outcome { check("podmiana: odpowiedź", r.ok, r.reason ?? "landed \(r.landedAfterMs ?? -1) ms") } else { check("podmiana: odpowiedź", false, "\(outcome)") }
                check("podmiana: tekst w polu", first.string == corrected, first.string)
                check("podmiana: kursor na końcu", first.selectedRange().location == (corrected as NSString).length, "\(first.selectedRange())")
                first.undoManager?.undo()
                log.append("  (⌘Z po podmianie: „\(first.string)”)")
            } else {
                check("podmiana: plan", false)
            }

            // 3. Pole zmienione od odczytu — odmowa bez zmian.
            await place(first, original)
            if let stale = await context(), let candidate = try? ImeScope.candidates(stale, wholeFieldWhenComplete: false).first,
               let plan = try? ImeEdits.plan(original: candidate.text, corrected: corrected, base: candidate.range.location) {
                first.string = "Ktoś dopisał coś innego."
                first.setSelectedRange(NSRange(location: (first.string as NSString).length, length: 0))
                let outcome = await ImeChannel.replace(client: stale.client, app: ImeProtocol.appBundleID, check: ImeProtocol.Edit(location: candidate.range.location, length: candidate.range.length, text: candidate.text),
                                                       edits: plan.edits, expected: plan.expected)
                if case .refused(let reason) = ImeChannel.classify(outcome) {
                    check("zmienione pole: odmowa przed zapisem", reason == "changed", reason)
                } else {
                    check("zmienione pole: odmowa przed zapisem", false, "\(outcome)")
                }
                check("zmienione pole: tekst nietknięty", first.string == "Ktoś dopisał coś innego.")
            }

            // 4. Zaznaczenie.
            await place(first, "Ala ma kotta i psa.", selection: NSRange(location: 7, length: 5))
            if let selected = await context() {
                let candidate = try? ImeScope.candidates(selected, wholeFieldWhenComplete: false).first
                check("zaznaczenie: zakres", candidate?.scope == .selection && candidate?.text == "kotta", candidate?.text ?? "-")
            }

            // 5. Wstawianie dyktowania z odstępem.
            await place(first, "Pierwsze zdanie.")
            let hello = await ImeChannel.hello()
            let inserted = await ImeChannel.insert("Drugie zdanie.", client: hello?.client)
            check("wstawienie: odpowiedź", { if case .landed = ImeChannel.classify(inserted) { return true }; return false }(), "\(inserted)")
            check("wstawienie: tekst z odstępem", first.string == "Pierwsze zdanie. Drugie zdanie.", first.string)

            // 6. Inne pole niż na początku dyktowania — odmowa, nic nie wstawione.
            await place(first, "Pole A")
            let helloA = await ImeChannel.hello()
            await place(second, "Pole B")
            _ = await context()
            let moved = await ImeChannel.insert("tekst", client: helloA?.client)
            if case .refused(let reason) = ImeChannel.classify(moved) {
                check("inne pole: odmowa", reason == "client_changed", reason)
            } else {
                check("inne pole: odmowa", false, "\(moved)")
            }
            check("inne pole: bez zmian", first.string == "Pole A" && second.string == "Pole B", "\(first.string) / \(second.string)")

            // 7. Pole hasła: brak tekstu dla aplikacji.
            window.makeFirstResponder(secure)
            secure.stringValue = "tajne-haslo-123"
            try? await Task.sleep(nanoseconds: 300_000_000)
            let secret = await ImeChannel.context()
            if case .reply(let r) = secret {
                let leaked = (r.before ?? "").contains("tajne") || (r.after ?? "").contains("tajne") || (r.selected ?? "").contains("tajne")
                check("pole hasła: bez treści", !leaked, r.ok ? "odczyt z \(r.app ?? "?")" : (r.reason ?? ""))
            } else {
                check("pole hasła: bez treści", true, "\(secret)")
            }
            finish()
        }
        app.run()
    }
}
