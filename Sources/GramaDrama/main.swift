import AppKit
import GramaCore

let arguments = Array(CommandLine.arguments.dropFirst())
// Limit czasu AX dla całego procesu — zawieszona aplikacja nie zatrzyma nas na domyślne 6 s.
AXQueue.configure()

if let command = arguments.first, ["--ime-status", "--ime-enable", "--ime-disable", "--ime-uninstall"].contains(command) {
    let code = MainActor.assumeIsolated { ImeSetup.run(command) }
    exit(code)
}

if arguments.first == "--bench" {
    Task {
        await Bench.run(arguments: Array(arguments.dropFirst()))
        exit(0)
    }
    dispatchMain()
}

if arguments.first == "--lint", arguments.count >= 2 {
    Task { @MainActor in
        await LiveChecker.lintCommand(arguments[1], arguments: arguments)
        exit(0)
    }
    dispatchMain()
}

if arguments.first == "--transcribe" {
    Task { @MainActor in
        await DictationCLI.run(arguments: Array(arguments.dropFirst()))
        exit(0)
    }
    dispatchMain()
}

if arguments.first == "--dictation-bench" {
    Task { @MainActor in
        exit(await DictationBench.run(arguments: Array(arguments.dropFirst())))
    }
    dispatchMain()
}

if arguments.first == "--live-preview" {
    // Sam monitor i plakietka (bez skrótów i menu) — test sprawdzania na żywo bez instalowania aplikacji.
    MainActor.assumeIsolated {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let monitor = LiveMonitor.shared
        monitor.onOpen = { print("kliknięto plakietkę") }
        monitor.start()
        var last = ""
        // LIVE_HEARTBEAT=1: najdłuższa blokada głównego wątku (test z zawieszoną aplikacją na wierzchu).
        if ProcessInfo.processInfo.environment["LIVE_HEARTBEAT"] != nil {
            let started = Date()
            var beat = started
            var worst: TimeInterval = 0
            Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
                let lag = Date().timeIntervalSince(beat) - 0.05
                beat = Date()
                if lag > worst { worst = lag; print("blokada głównego wątku: \(Int(lag * 1000)) ms po \(String(format: "%.1f", Date().timeIntervalSince(started))) s"); fflush(stdout) }
            }
        }
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
            MainActor.assumeIsolated {
                let now = monitor.visibleIssues.map { "„\($0.word)” [\($0.source.rawValue)]" }.joined(separator: ", ")
                if now != last { print("\(monitor.visibleIssues.count): \(now)"); fflush(stdout); last = now }
                // LIVE_AUTOFIX=1: jak kliknięcie pierwszej sugestii w karcie (test poprawiania bez myszy).
                if ProcessInfo.processInfo.environment["LIVE_AUTOFIX"] != nil,
                   let issue = monitor.visibleIssues.first(where: { !$0.suggestions.isEmpty }) {
                    print("poprawiam „\(issue.word)” → „\(issue.suggestions[0])”"); fflush(stdout)
                    monitor.apply(issue, issue.suggestions[0])
                }
            }
        }
        app.run()
    }
}

if arguments.first == "--dump-prompts", arguments.count >= 2 {
    Prompts.dumpPrompts(to: arguments[1])
    exit(0)
}

// Własne prompty (linux/docs/prompts.md): `export [katalog]` zapisuje brakujące pliki domyślne, `status [katalog]` mówi,
// które pliki są własne, a które pominięte (kod 1 przy problemie), `list` wypisuje fragmenty.
if arguments.first == "--prompts" {
    let command = arguments.count > 1 ? arguments[1] : ""
    let directory = arguments.count > 2 ? URL(fileURLWithPath: arguments[2]) : PromptFiles.directory
    switch command {
    case "export":
        do {
            let written = try PromptFiles.export(to: directory)
            print("\(directory.path): " + (written.isEmpty ? "nic do zapisania, wszystkie pliki już są"
                                                        : "zapisano \(written.count) plików: \(written.joined(separator: ", "))"))
            exit(0)
        } catch {
            print("Nie udało się zapisać promptów: \(error.localizedDescription)")
            exit(1)
        }
    case "status":
        let files = PromptFiles.load(from: directory)
        print("Własne pliki (\(files.overridden.count)): \(files.overridden.isEmpty ? "—" : files.overridden.joined(separator: ", "))")
        for problem in files.problems { print("Pominięty \(problem.file): \(problem.reason)") }
        exit(files.problems.isEmpty ? 0 : 1)
    case "list":
        PromptFiles.pieces.forEach { print($0.file) }
        exit(0)
    default:
        print("Użycie: GramaDrama --prompts export|status [katalog] | list")
        exit(2)
    }
}

if arguments.first == "--selftest" {
    let dump = arguments.firstIndex(of: "--dump-cases").flatMap { arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil }
    let logic = SelfTest.run(dumpCasesTo: dump)
    Task {
        let stream = await StreamSelfTest.run()
        let servers = await ServerSelfTest.run()
        let ok = logic == 0 && stream == 0 && servers == 0
        print(ok ? "\nWszystko OK (także strumień)" : "\nSą błędy")
        exit(ok ? 0 : 1)
    }
    dispatchMain()
}

if arguments.first == "--e2e-webhost", arguments.count >= 3 {
    MainActor.assumeIsolated { EndToEnd.webHost(html: arguments[1], textFile: arguments[2]) }
}

if arguments.first == "--e2e-ime", arguments.count >= 2 {
    MainActor.assumeIsolated { ImeEndToEnd.run(output: arguments[1]) }
}

if arguments.first == "--e2e", arguments.count >= 2 {
    MainActor.assumeIsolated { EndToEnd.run(output: arguments[1]) }
}

if arguments.first == "--preview-window", arguments.count >= 2 {
    MainActor.assumeIsolated { DebugPreview.window(to: arguments[1]) }
}

if arguments.first == "--preview-panel", arguments.count >= 2 {
    MainActor.assumeIsolated { DebugPreview.panel(to: arguments[1]) }
}

if arguments.first == "--preview-dictation", arguments.count >= 2 {
    MainActor.assumeIsolated { DebugPreview.dictationHUD(to: arguments[1]) }
}

if arguments.first == "--preview", arguments.count >= 3 {
    MainActor.assumeIsolated { DebugPreview.run(text: arguments[1], outputDir: arguments[2]) }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
