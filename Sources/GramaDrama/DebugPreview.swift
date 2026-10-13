import AppKit
import SwiftUI
import GramaCore

/// `GramaDrama --preview "<tekst>" <katalog>` — otwiera prawdziwy panel z odpowiedzią serwera
/// i co pół sekundy zapisuje zrzut panelu i jego geometrię. Do diagnozowania UI bez zrzutów ekranu.
@MainActor
enum DebugPreview {
    /// `GramaDrama --preview-window <plik.png>` — zrzut okna aplikacji, każda zakładka w całości (po odświeżeniu
    /// stanu): `<plik>-start.png`, `<plik>-writing.png`…
    static func window(to path: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let base = (path as NSString).deletingPathExtension
        let views = MainWindowTab.allCases.map { tab -> (MainWindowTab, NSHostingView<MainWindowView>) in
            let hosting = NSHostingView(rootView: MainWindowView(height: 1700, previewTab: tab))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 1700), styleMask: [.borderless],
                                  backing: .buffered, defer: false)
            window.contentView = hosting
            window.orderFrontRegardless()
            return (tab, hosting)
        }
        Timer.scheduledTimer(withTimeInterval: 6, repeats: false) { _ in
            MainActor.assumeIsolated {
                for (tab, hosting) in views {
                    hosting.layoutSubtreeIfNeeded()
                    let file = "\(base)-\(tab.rawValue).png"
                    if let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) {
                        hosting.cacheDisplay(in: hosting.bounds, to: rep)
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: file))
                    }
                    print("zapisano \(file)")
                }
                exit(0)
            }
        }
        app.run()
    }

    /// `GramaDrama --preview-dictation <plik.png>` — pastylka dyktowania w kilku stanach, bez mikrofonu i modelu:
    /// `<plik>-listening.png`, `<plik>-short.png`, `<plik>-long.png`, `<plik>-recognizing.png`.
    static func dictationHUD(to path: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let base = (path as NSString).deletingPathExtension
        let controller = DictationController.shared
        var short = DictationPreviewText()
        // Syntetyczny tekst do zrzutów, niezwiązany z wiadomościami ani środowiskiem użytkownika.
        short.setPending("Jutro wyślę krótkie podsumowanie, możesz", prompt: "")
        var long = DictationPreviewText()
        long.confirm("Krótki plan wycieczki. Rano pójdziemy przez park do ogrodu botanicznego i obejrzymy wystawę kwiatów. "
                     + "Po spacerze zrobimy przerwę na herbatę, a po południu odwiedzimy małe muzeum przy rynku.",
                     samples: 1, prompt: "")
        long.setPending("Jeżeli pogoda dopisze, wieczorem wybierzemy się nad jezioro i zobaczymy", prompt: "")
        let states: [(String, DictationController.Phase, DictationPreviewText)] = [
            ("listening", .listening, DictationPreviewText()), ("short", .listening, short),
            ("long", .listening, long), ("recognizing", .recognizing, long),
        ]
        let hosting = NSHostingView(rootView: DictationHUDView(controller: controller))
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: DictationHUD.size), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = hosting
        window.backgroundColor = .windowBackgroundColor
        window.orderFrontRegardless()
        func shoot(_ index: Int) {
            guard index < states.count else { exit(0) }
            let (name, phase, preview) = states[index]
            controller.showForScreenshot(phase: phase, language: .polish, level: 0.6, preview: preview)
            // Animacja rozmiaru pastylki trwa 0,15 s.
            Timer.scheduledTimer(withTimeInterval: 0.5, repeats: false) { _ in
                MainActor.assumeIsolated {
                    hosting.layoutSubtreeIfNeeded()
                    let file = "\(base)-\(name).png"
                    if let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) {
                        hosting.cacheDisplay(in: hosting.bounds, to: rep)
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: file))
                    }
                    print("zapisano \(file)")
                    shoot(index + 1)
                }
            }
        }
        shoot(0)
        app.run()
    }

    /// `GramaDrama --preview-panel <plik.png>` — panel korekty (także z listą zmian, w edycji i po błędzie), plakietka bieżącego sprawdzania
    /// z listą i karta podkreślenia, w jasnym i ciemnym motywie, bez serwera: `<plik>-panel-light.png`, `<plik>-badge-dark.png`…
    static func panel(to path: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let base = (path as NSString).deletingPathExtension
        // Syntetyczny tekst do zrzutów, niezwiązany z wiadomościami ani środowiskiem użytkownika.
        let text = "Jutro wysle krótkie podsumowanie spotkania a w piątek omówimy plan na kolejny tydzien."
        let fixed = "Jutro wyślę krótkie podsumowanie spotkania, a w piątek omówimy plan na kolejny tydzień."
        let captured = CapturedText(text: text, scope: .paragraph, app: nil, anchor: .zero, canApply: true)
        let session = CorrectionSession(captured: captured, mode: .fix)
        session.showForScreenshot(fixed, elapsed: 2.9)
        let issues = [
            LiveIssue(range: NSRange(location: 6, length: 5), word: "wysle", message: "Możliwy błąd pisowni.",
                      suggestions: ["wyślę", "wyśle"], source: .spelling, rule: "MORFOLOGIK_RULE_PL_PL"),
            LiveIssue(range: NSRange(location: 79, length: 7), word: "tydzien", message: "Brak polskiego znaku.",
                      suggestions: ["tydzień"], source: .languageTool, rule: "PL_DIACRITICS"),
        ]
        let badge = LiveBadgeModel()
        badge.issues = issues
        badge.lamp = .count("2", popped: nil)
        let expanded = LiveBadgeModel()
        expanded.issues = issues
        expanded.lamp = .count("2", popped: nil)
        expanded.expanded = true
        // Długie sugestie: idą jedna pod drugą.
        let long = LiveBadgeModel()
        long.issues = [LiveIssue(range: NSRange(location: 0, length: 12), word: "konfigurację", message: "Możliwa literówka.",
                                 suggestions: ["konfiguracja serwera", "konfiguracyjny"], source: .languageTool, rule: "TEST")]
        long.lamp = .count("1", popped: nil)
        long.expanded = true
        // Lawa (PANEL.md): bańki dryfują, zlewają się, lawenda przechodzi w cegłę, liczba wskakuje; pigułka, „99+”,
        // „!” (komunikat bez błędów) i liczba, która „oddycha” w trakcie sprawdzania. Zegar zamrożony (ms).
        func lamp(_ lamp: LiveBadgeModel.Lamp, at clock: Double, checking: Bool = false) -> LiveBadgeView {
            let model = LiveBadgeModel()
            model.lamp = lamp
            model.clock = clock
            model.checking = checking
            return LiveBadgeView(model: model)
        }
        let lava = HStack(spacing: 14) {
            lamp(.thinking(began: 0), at: 1234)
            lamp(.thinking(began: 0), at: 2600)
            lamp(.thinking(began: 0), at: 4100)
            lamp(.merging(began: 0, at: 4100, into: "2"), at: 4100 + 260)
            lamp(.merging(began: 0, at: 4100, into: "2"), at: 4100 + 520 + 90)
            lamp(.merging(began: 0, at: 4100, into: "2"), at: 4100 + 520 + 240)
            lamp(.merging(began: 0, at: 4100, into: nil), at: 4100 + 520 + 200)
            lamp(.count("12", popped: nil), at: 0)
            lamp(.count("99+", popped: nil), at: 0)
            lamp(.count("!", popped: nil), at: 0)
            lamp(.count("2", popped: nil), at: 700, checking: true)
        }
        // Lista zmian otwarta (⌘I).
        let changes = CorrectionSession(captured: captured, mode: .fix)
        changes.showForScreenshot(fixed, elapsed: 2.9, changesOpen: true)
        // Edycja wyniku w jego polu: dopisane słowo też na szałwii.
        let editing = CorrectionSession(captured: captured, mode: .fix)
        editing.showForScreenshot(fixed, elapsed: 2.9)
        editing.startEditing()
        editing.draft = fixed.replacingOccurrences(of: "krótkie", with: "krótkie pisemne")
        // Nieudana korekta (PANEL.md, „Błąd”): serwer nie odpowiada, nowa instalacja bez Ollamy, ciche ponowienie.
        let unreachable = CorrectionSession(captured: captured, mode: .fix)
        unreachable.showFailureForScreenshot("refused")
        let setup = CorrectionSession(captured: captured, mode: .fix)
        setup.showFailureForScreenshot("no_ollama")
        let retrying = CorrectionSession(captured: captured, mode: .fix)
        retrying.showFailureForScreenshot("interrupted", retrying: true)
        let shots: [(String, AnyView)] = [
            ("panel", AnyView(PanelView(session: session, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("panel-error", AnyView(PanelView(session: unreachable, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("panel-error-setup", AnyView(PanelView(session: setup, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("panel-retrying", AnyView(PanelView(session: retrying, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("panel-changes", AnyView(PanelView(session: changes, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("panel-edit", AnyView(PanelView(session: editing, onApply: { _ in }, onCopy: { _ in }, onClose: {}))),
            ("badge", AnyView(HStack(alignment: .top, spacing: 16) {
                LiveBadgeView(model: badge)
                LiveBadgeView(model: expanded)
                IssueCardView(issue: issues[0], apply: { _ in }, ignore: {}, learn: {})
                IssueCardView(issue: LiveIssue(range: NSRange(location: 33, length: 8), word: "spotkania", message: "Brak przecinka przed „a”.",
                                               suggestions: ["spotkania,"], source: .languageTool, rule: "PL_BRAK_PRZECINKA",
                                               ruleDescription: "Brak przecinka przed spójnikiem"),
                              apply: { _ in }, ignore: {}, learn: nil, turnOff: {})
                LiveBadgeView(model: long)
            })),
            ("badge-lava", AnyView(lava)),
        ]
        for (name, view) in shots {
            for (variant, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                let hosting = NSHostingView(rootView: view.padding(20).background(Color(nsColor: .windowBackgroundColor)))
                hosting.appearance = NSAppearance(named: appearance)
                hosting.setFrameSize(hosting.fittingSize)
                hosting.layoutSubtreeIfNeeded()
                let file = "\(base)-\(name)-\(variant).png"
                if let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) {
                    hosting.cacheDisplay(in: hosting.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: file))
                }
                print("zapisano \(file)")
            }
        }
        exit(0)
    }

    static func run(text: String, outputDir: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        try? FileManager.default.createDirectory(atPath: outputDir, withIntermediateDirectories: true)

        let controller = PanelController()
        // GD_ANCHOR_Y=20 symuluje pole wiadomości przy dolnej krawędzi ekranu (jak w Slacku).
        let anchorY = Double(ProcessInfo.processInfo.environment["GD_ANCHOR_Y"] ?? "") ?? 500
        let captured = CapturedText(text: text, scope: .selection, app: nil,
                                    anchor: NSRect(x: 200, y: anchorY, width: 500, height: 40), canApply: true)
        print("anchor: y \(anchorY)…\(anchorY + 40)")
        let environment = ProcessInfo.processInfo.environment
        let mode = Mode(rawValue: environment["GD_MODE"] ?? "") ?? .fix
        controller.debugApplyOverride = { text in print("APPLIED: \(text)") }
        controller.show(captured, mode: mode)
        // GD_APPLY_PENDING=1: jak ↩ wciśnięty w trakcie strumieniowania.
        if environment["GD_APPLY_PENDING"] != nil { controller.debugPressApplyDuringWork() }

        var tick = 0
        var doneTicks = 0
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
            MainActor.assumeIsolated {
                tick += 1
                let phase = controller.debugPhase.map { "\($0)" } ?? "nil"
                let info = controller.debugSnapshot(to: "\(outputDir)/tick-\(String(format: "%02d", tick)).png")
                print("[\(tick)] phase=\(phase) \(info)")
                if controller.debugPhase == .done || controller.debugPhase == nil { doneTicks += 1 }
                if doneTicks >= 3 || tick >= 60 { exit(0) }
            }
        }
        app.run()
    }
}
