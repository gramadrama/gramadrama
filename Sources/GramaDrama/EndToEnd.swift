import AppKit
import WebKit
import GramaCore

/// `open -n ~/Applications/GramaDrama.app --args --e2e <plik>` — test pobierania i podmiany w natywnym polu
/// na prawdziwym Accessibility API i prawdziwym ⌘V. Wymaga uprawnienia Dostępność, więc działa tylko
/// z podpisanej aplikacji. Wszystko dzieje się we własnym oknie. Przed każdym pobraniem sprawdzamy, że to
/// okno jest na wierzchu, a po pobraniu — że tekst pochodzi z tego procesu; inaczej test się przerywa
/// i niczego nie wkleja. Schowek użytkownika jest zapamiętany i przywracany na końcu.
@MainActor
enum EndToEnd {
    /// `--e2e-webhost <html> <plik-z-tekstem>` — osobny proces z polem WebKit (jak Outlook/Mail). Test pyta go
    /// przez Accessibility z zewnątrz, tak jak GramaDrama pyta prawdziwe aplikacje; własnego WKWebView proces
    /// przez AX nie widzi (treść strony żyje w procesie WebKit). Co 200 ms zapisuje innerText do pliku.
    static func webHost(html: String, textFile: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        app.mainMenu = AppDelegate.buildEditMenu()
        let window = NSWindow(contentRect: NSRect(x: 280, y: 280, width: 560, height: 320),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "GramaDrama — test WebKit"
        let webView = WKWebView(frame: window.contentLayoutRect)
        window.contentView = webView
        webView.loadHTMLString((try? String(contentsOfFile: html, encoding: .utf8)) ?? "", baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        app.activate()
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            app.activate()
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(webView)
            _ = try? await webView.evaluateJavaScript("""
                document.body.focus();
                const r = document.createRange(); r.setStart(document.body.firstElementChild.firstChild, 3); r.collapse(true);
                const s = getSelection(); s.removeAllRanges(); s.addRange(r); true
                """)
            for _ in 0..<150 {
                if let text = try? await webView.evaluateJavaScript("document.body.innerText") as? String {
                    try? text.write(toFile: textFile, atomically: true, encoding: .utf8)
                }
                try? await Task.sleep(nanoseconds: 200_000_000)
            }
            exit(0)
        }
        app.run()
    }

    static func run(output: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        // ⌘V/⌘C/⌘Z w AppKit działają przez menu Edycja — bez niego pole testowe nie przyjmie wklejenia
        // (prawdziwe aplikacje, np. TextEdit i Notatki, to menu mają).
        app.mainMenu = AppDelegate.buildEditMenu()

        let window = NSWindow(contentRect: NSRect(x: 240, y: 240, width: 560, height: 320),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "GramaDrama — test"
        let scroll = NSTextView.scrollableTextView()
        let textView = scroll.documentView as! NSTextView
        textView.allowsUndo = true
        window.contentView = scroll
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        window.makeFirstResponder(textView)

        var log: [String] = []
        var failures = 0
        func check(_ name: String, _ actual: String, _ expected: String) {
            if actual == expected {
                log.append("✓ \(name)")
            } else {
                failures += 1
                log.append("✗ \(name)\n    wynik:      \(actual.debugDescription)\n    oczekiwano: \(expected.debugDescription)")
            }
        }
        // Schowek użytkownika — przywracamy go na końcu, także po przerwaniu.
        let savedClipboard: [NSPasteboardItem] = (NSPasteboard.general.pasteboardItems ?? []).map { item in
            let copy = NSPasteboardItem()
            for type in item.types { if let data = item.data(forType: type) { copy.setData(data, forType: type) } }
            return copy
        }
        /// Proces pomocniczy z polem WebKit (test 6). Kończymy go w finish(): po exit() bloki defer się nie wykonują,
        /// a żywy proces z tej samej paczki sprawiał, że `open` po teście nie uruchamiał już aplikacji (05.10).
        var helperProcess: Process?
        func finish() {
            if let helper = helperProcess, helper.isRunning {
                helper.terminate()
                for _ in 0..<40 where helper.isRunning { usleep(50_000) }
            }
            NSPasteboard.general.clearContents()
            if !savedClipboard.isEmpty { NSPasteboard.general.writeObjects(savedClipboard) }
            log.append(failures == 0 ? "Wszystko OK" : "Błędy: \(failures)")
            try? log.joined(separator: "\n").write(toFile: output, atomically: true, encoding: .utf8)
            exit(failures == 0 ? 0 : 1)
        }

        /// Proces pomocniczy z polem WebKit (test 6) — jedyny obcy proces, z którego test może pobierać.
        var helperPID: pid_t?
        /// Pobiera tekst tylko z okna testu. nil = przerwać cały test (niczego nie wklejamy).
        func captureFromTestWindow() async -> Result<CapturedText, Error>? {
            let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
            let ours = helperPID == nil ? (front == getpid() && window.isKeyWindow) : (front == helperPID)
            guard ours else {
                log.append("✗ okno testu nie jest na wierzchu — przerwane, nic nie zostało wysłane")
                failures += 1
                return nil
            }
            do {
                let captured = try await TextBridge.capture()
                guard captured.app?.processIdentifier == (helperPID ?? getpid()) else {
                    log.append("✗ tekst pobrany z innej aplikacji — przerwane bez wklejania")
                    failures += 1
                    return nil
                }
                return .success(captured)
            } catch {
                return .failure(error)
            }
        }

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 800_000_000)
            let pasteboard = NSPasteboard.general
            let sentinel = "SCHOWEK-UŻYTKOWNIKA-\(Int.random(in: 1000...9999))"
            pasteboard.clearContents()
            pasteboard.setString(sentinel, forType: .string)

            // 1. Akapit z kursorem → podmiana tylko zmienionych fragmentów.
            let original = "Pierwszy akapit.\nDrugi akapit z bledem i pomysl.\n\nTrzeci."
            textView.string = original
            textView.setSelectedRange(NSRange(location: 25, length: 0))
            guard let first = await captureFromTestWindow() else { return finish() }
            do {
                let captured = try first.get()
                check("zakres: akapit", captured.scope.label, "akapit")
                check("pobrany tekst", captured.text, "Drugi akapit z bledem i pomysl.")
                let outcome = await TextBridge.replace(captured, with: "Drugi akapit z błędem i pomysł.")
                check("wynik podmiany", "\(outcome)", "pasted")
                check("tekst po podmianie", textView.string, "Pierwszy akapit.\nDrugi akapit z błędem i pomysł.\n\nTrzeci.")
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                check("schowek przywrócony", pasteboard.string(forType: .string) ?? "nil", sentinel)
                textView.undoManager?.undo()
                check("⌘Z cofa poprawkę", textView.string, original)
            } catch {
                check("pobranie akapitu", error.localizedDescription, "—")
            }

            // 2. Tekst zmieniony między pobraniem a podmianą → nic nie wklejamy.
            textView.string = "Jeden akapit z bledem."
            textView.setSelectedRange(NSRange(location: 3, length: 0))
            guard let second = await captureFromTestWindow() else { return finish() }
            do {
                let captured = try second.get()
                textView.string = "Użytkownik zdążył coś dopisać. Jeden akapit z bledem."
                let outcome = await TextBridge.replace(captured, with: "Jeden akapit z błędem.")
                check("zmienione pole: wynik", "\(outcome)", "fieldChanged")
                check("zmienione pole: tekst nietknięty", textView.string, "Użytkownik zdążył coś dopisać. Jeden akapit z bledem.")
                check("zmienione pole: poprawka w schowku", pasteboard.string(forType: .string) ?? "nil", "Jeden akapit z błędem.")
            } catch {
                check("pobranie (zmienione pole)", error.localizedDescription, "—")
            }

            // 3. Zaznaczenie → podmiana w zaznaczeniu.
            pasteboard.clearContents()
            pasteboard.setString(sentinel, forType: .string)
            textView.string = "Zostaje. Popraw to zdanie ktore ma blad. Też zostaje."
            textView.setSelectedRange(NSRange(location: 9, length: 31))
            guard let third = await captureFromTestWindow() else { return finish() }
            do {
                let captured = try third.get()
                check("zaznaczenie: zakres", captured.scope.label, "zaznaczenie")
                check("zaznaczenie: tekst", captured.text, "Popraw to zdanie ktore ma blad.")
                _ = await TextBridge.replace(captured, with: "Popraw to zdanie, które ma błąd.")
                check("zaznaczenie: tekst po podmianie", textView.string, "Zostaje. Popraw to zdanie, które ma błąd. Też zostaje.")
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                check("zaznaczenie: schowek przywrócony", pasteboard.string(forType: .string) ?? "nil", sentinel)
            } catch {
                check("pobranie zaznaczenia", error.localizedDescription, "—")
            }

            // 4. Kursor w pustej linii → odmowa.
            textView.string = "Tekst.\n\nDalej."
            textView.setSelectedRange(NSRange(location: 7, length: 0))
            guard let fourth = await captureFromTestWindow() else { return finish() }
            switch fourth {
            case .success: check("pusta linia: odmowa", "pobrano", "emptyParagraph")
            case .failure(let error): check("pusta linia: odmowa", "\(error)", "emptyParagraph")
            }

            // Dyktowanie: wstawienie w miejscu kursora (tylko gdy okno testu jest na wierzchu).
            @MainActor func insertIntoTestWindow(_ text: String) async -> String? {
                guard NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid(), window.isKeyWindow else {
                    log.append("✗ okno testu nie jest na wierzchu — przerwane, nic nie zostało wklejone")
                    failures += 1
                    return nil
                }
                return "\(await TextBridge.insert(text, into: NSRunningApplication.current))"
            }
            pasteboard.clearContents()
            pasteboard.setString(sentinel, forType: .string)
            textView.string = "Hej."
            textView.setSelectedRange(NSRange(location: 4, length: 0))
            guard let inserted = await insertIntoTestWindow("Kolejne zdanie.") else { return finish() }
            check("dyktowanie: wynik", inserted, "inserted")
            check("dyktowanie: spacja po kropce", textView.string, "Hej. Kolejne zdanie.")
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            check("dyktowanie: schowek przywrócony", pasteboard.string(forType: .string) ?? "nil", sentinel)
            textView.string = "Spotkanie w środę o 10."
            textView.setSelectedRange(NSRange(location: 12, length: 5))
            guard await insertIntoTestWindow("czwartek") != nil else { return finish() }
            check("dyktowanie: w miejsce zaznaczenia", textView.string, "Spotkanie w czwartek o 10.")

            // Pole hasła najpierw (w naszym oknie), potem WebKit w procesie pomocniczym.
            // 5. Pole hasła → odmowa bez żadnego ⌘C.
            let secure = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 200, height: 24))
            window.contentView = secure
            window.makeFirstResponder(secure)
            secure.stringValue = "tajne"
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard let fifth = await captureFromTestWindow() else { return finish() }
            switch fifth {
            case .success: check("pole hasła: odmowa", "pobrano", "sensitive")
            case .failure(let error): check("pole hasła: odmowa", "\(error)", "sensitive")
            }
            guard let secureInsert = await insertIntoTestWindow("tekst") else { return finish() }
            check("dyktowanie do pola hasła: odmowa", secureInsert, "secureField")
            check("dyktowanie do pola hasła: pole nietknięte", secure.stringValue, "tajne")

            // 6. Pole WebKit jak w Outlooku/Mailu (osobny proces): odpowiedź z cytowanym mailem → tylko nowa wiadomość.
            let dir = FileManager.default.temporaryDirectory
            let htmlFile = dir.appendingPathComponent("gramadrama-e2e.html").path
            let textFile = dir.appendingPathComponent("gramadrama-e2e.txt").path
            try? FileManager.default.removeItem(atPath: textFile)
            try? """
            <html><body contenteditable="true" style="font: 14px -apple-system">\
            <div>Czesc Aniu,</div><div>wysylam poprawiona wersje raportu.</div><div>Pozdrawiam</div><div><br></div>\
            <div>Od: Anna Nowak &lt;anna@example.com&gt;</div><div>Wysłano: poniedziałek, 28 września 2026 10:12</div>\
            <div>Do: Tomasz</div><div>Temat: Raport</div><div><br></div>\
            <div>Hej, przeslesz raport? To jest stary mail z bledami.</div></body></html>
            """.write(toFile: htmlFile, atomically: true, encoding: .utf8)
            let helper = Process()
            helper.executableURL = Bundle.main.executableURL
            helper.arguments = ["--e2e-webhost", htmlFile, textFile]
            do { try helper.run() } catch {
                check("WebKit: proces pomocniczy", "\(error)", "uruchomiony")
                return finish()
            }
            helperPID = helper.processIdentifier
            helperProcess = helper
            // macOS 14+: aplikacja nie wysunie się sama — aktywna aplikacja (ten test) musi jej to odstąpić.
            for _ in 0..<40 where !FileManager.default.fileExists(atPath: textFile)
                || NSWorkspace.shared.frontmostApplication?.processIdentifier != helper.processIdentifier {
                if let running = NSRunningApplication(processIdentifier: helper.processIdentifier) {
                    NSApp.yieldActivation(to: running)
                    running.activate()
                }
                try? await Task.sleep(nanoseconds: 150_000_000)
            }
            try? await Task.sleep(nanoseconds: 400_000_000)
            log.append("  (WebKit AX: \(await TextBridge.debugDescribeFocus()))")
            pasteboard.clearContents()
            pasteboard.setString(sentinel, forType: .string)
            // WebKit buduje drzewo AX obcego procesu z opóźnieniem: zaraz po instalacji (05.10) pierwsze pobranie
            // widziało puste pole (AXValue 0 zn.) → nothingFound. Do 3 prób co 1 s; pole puste na stałe i tak zostanie błędem.
            func emptyField(_ result: Result<CapturedText, Error>?) -> Bool {
                if case .failure(let error)? = result, case CaptureError.nothingFound? = error as? CaptureError { return true }
                return false
            }
            var attempt = await captureFromTestWindow()
            for _ in 0..<2 where emptyField(attempt) {
                log.append("  (WebKit: pole jeszcze puste, ponawiam za 1 s)")
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                attempt = await captureFromTestWindow()
            }
            guard let sixth = attempt else { return finish() }
            do {
                let captured = try sixth.get()
                check("WebKit: zakres", captured.scope.label, "nowa wiadomość (bez cytatów)")
                check("WebKit: pobrana tylko nowa wiadomość", PostProcess.fold(captured.text),
                      PostProcess.fold("Czesc Aniu, wysylam poprawiona wersje raportu. Pozdrawiam"))
                let outcome = await TextBridge.replace(captured, with: "Cześć Aniu,\nwysyłam poprawioną wersję raportu.\nPozdrawiam")
                check("WebKit: wynik podmiany", "\(outcome)", "pasted")
                try? await Task.sleep(nanoseconds: 600_000_000)
                let body = (try? String(contentsOfFile: textFile, encoding: .utf8)) ?? ""
                check("WebKit: nowa wiadomość poprawiona", body.contains("wysyłam poprawioną wersję raportu.") ? "tak" : body, "tak")
                check("WebKit: cytowany mail nietknięty",
                      body.contains("Hej, przeslesz raport? To jest stary mail z bledami.") && body.contains("Od: Anna Nowak") ? "tak" : body, "tak")
                check("WebKit: schowek przywrócony", pasteboard.string(forType: .string) ?? "nil", sentinel)
            } catch {
                check("WebKit: pobranie", "\(error)", "—")
            }
            finish()
        }
        app.run()
    }
}
