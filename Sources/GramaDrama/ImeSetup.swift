import AppKit
import Carbon
import GramaBackend
import GramaCore
import GramaIMEProtocol
import GramaIPC

/// Włączanie, wyłączanie i stan metody wprowadzania. Z wiersza poleceń (`--ime-status`, `--ime-enable`,
/// `--ime-disable`, `--ime-uninstall`) i z menu (przełącznik klawiatury).
@MainActor
enum ImeSetup {
    // MARK: - TIS

    nonisolated static func identifier(of source: TISInputSource) -> String? {
        guard let raw = TISGetInputSourceProperty(source, kTISPropertyInputSourceID) else { return nil }
        return Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
    }

    nonisolated static func name(of source: TISInputSource) -> String {
        guard let raw = TISGetInputSourceProperty(source, kTISPropertyLocalizedName) else { return "?" }
        return Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
    }

    nonisolated static func flag(_ source: TISInputSource, _ key: CFString) -> Bool {
        guard let raw = TISGetInputSourceProperty(source, key) else { return false }
        return CFBooleanGetValue(Unmanaged<CFBoolean>.fromOpaque(raw).takeUnretainedValue())
    }

    /// Źródło o danym identyfikatorze (także niewłączone).
    static func source(_ id: String) -> TISInputSource? {
        let filter = [kTISPropertyInputSourceID as String: id] as CFDictionary
        let list = TISCreateInputSourceList(filter, true)?.takeRetainedValue() as? [TISInputSource]
        return list?.first
    }

    static var imeSource: TISInputSource? { source(ImeProtocol.inputSourceID) }

    static var isInstalled: Bool { FileManager.default.fileExists(atPath: ImeProtocol.installURL.path) }

    /// Układ, na który wracamy („Polski Pro”): zapisany przy włączaniu, a gdy go brak — domyślny.
    static var fallbackLayoutID: String {
        UserDefaults.standard.string(forKey: ImeProtocol.DefaultsKey.keyboardLayout) ?? ImeProtocol.defaultKeyboardLayout
    }

    static var fallbackLayoutName: String { source(fallbackLayoutID).map(name(of:)) ?? "Polski Pro" }

    // MARK: - Menu

    enum MenuState { case notInstalled, selected, available }

    static var menuState: MenuState {
        guard isInstalled, let ime = imeSource, flag(ime, kTISPropertyInputSourceIsEnabled) else { return .notInstalled }
        return ImeChannel.isSourceSelected() ? .selected : .available
    }

    /// Przełącza klawiaturę między GramaDramą a zwykłym układem (bez okna zgody — oba źródła są włączone).
    static func toggleKeyboard() {
        let target = ImeChannel.isSourceSelected() ? source(fallbackLayoutID) : imeSource
        guard let target else { return }
        TISSelectInputSource(target)
    }

    // MARK: - Stan dla okna

    struct Snapshot {
        var installed = false
        var version: String?
        var enabled = false
        var selected = false
        /// Nazwa wybranego źródła klawiatury (np. „Polish”).
        var current = ""
        var fallbackName = "Polski Pro"
        /// Odpowiedź IME na `hello` (nil: nie działa albo nie odpowiada).
        var hello: ImeProtocol.Reply?
        var restart: [String] = []
        /// Ta kopia aplikacji jest podpisana certyfikatem (bez tego IME z nią nie rozmawia).
        var signed = false
    }

    static func snapshot() async -> Snapshot {
        var snapshot = Snapshot()
        snapshot.installed = isInstalled
        snapshot.version = Bundle(url: ImeProtocol.installURL)?.infoDictionary?["CFBundleVersion"] as? String
        if let ime = imeSource {
            snapshot.enabled = flag(ime, kTISPropertyInputSourceIsEnabled)
            snapshot.selected = flag(ime, kTISPropertyInputSourceIsSelected)
        }
        snapshot.current = TISCopyCurrentKeyboardInputSource().map { name(of: $0.takeRetainedValue()) } ?? ""
        snapshot.fallbackName = fallbackLayoutName
        snapshot.restart = appsNeedingRestart()
        snapshot.signed = PeerCheck.ownLeafHash != nil
        if snapshot.installed, snapshot.signed { snapshot.hello = await ImeChannel.hello() }
        return snapshot
    }

    /// Włączenie z okna: rejestracja, zgoda macOS (okno w Ustawieniach systemowych) i wybór źródła.
    /// Zwraca komunikat dla użytkownika.
    static func enableFromApp() async -> String {
        guard isInstalled else { return tr("Brak pakietu — zbuduj ./scripts/build-app.sh.") }
        _ = TISRegisterInputSource(ImeProtocol.installURL as CFURL)
        if let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(), let id = identifier(of: ascii),
           id != ImeProtocol.inputSourceID {
            UserDefaults.standard.set(id, forKey: ImeProtocol.DefaultsKey.keyboardLayout)
        }
        guard let ime = imeSource else { return tr("macOS nie widzi źródła — wyloguj się i zaloguj ponownie.") }
        if !flag(ime, kTISPropertyInputSourceIsEnabled) {
            TISEnableInputSource(ime)
            for _ in 0..<120 where !(imeSource.map { flag($0, kTISPropertyInputSourceIsEnabled) } ?? false) {
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            guard imeSource.map({ flag($0, kTISPropertyInputSourceIsEnabled) }) == true else {
                return tr("Nie widzę zgody. Dodaj ręcznie: Ustawienia systemowe → Klawiatura → Źródła wprowadzania → Edytuj… → + → Polish → Polski (GramaDrama).")
            }
        }
        if let ime = imeSource { TISSelectInputSource(ime) }
        EventLog.write("ime_setup", ["action": "enable_from_app", "selected": ImeChannel.isSourceSelected()])
        return ImeChannel.isSourceSelected() ? tr("Włączona i wybrana.") : tr("Włączona — wybierz ją w menu klawiatury.")
    }

    // MARK: - Wiersz poleceń

    static func run(_ command: String) -> Int32 {
        switch command {
        case "--ime-status": return status()
        case "--ime-enable": return enable()
        case "--ime-disable": return disable()
        case "--ime-uninstall": return uninstall()
        default: return 2
        }
    }

    private static func status() -> Int32 {
        let installed = isInstalled
        let version = Bundle(url: ImeProtocol.installURL)?.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        print("Pakiet:      \(installed ? "\(ImeProtocol.installURL.path) (\(version))" : "brak — zbuduj ./scripts/build-app.sh")")
        if let ime = imeSource {
            print("Źródło:      zarejestrowane, \(flag(ime, kTISPropertyInputSourceIsEnabled) ? "włączone" : "wyłączone"), "
                  + (flag(ime, kTISPropertyInputSourceIsSelected) ? "wybrane" : "niewybrane"))
        } else {
            print("Źródło:      niezarejestrowane")
        }
        if let current = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue() {
            print("Klawiatura:  \(name(of: current)) (\(identifier(of: current) ?? "?"))")
        }
        if let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue() {
            let id = identifier(of: ascii) ?? "?"
            let warn = id == fallbackLayoutID ? "" : "  ⚠︎ inny niż \(fallbackLayoutID) — po awarii IME klawisze pójdą tym układem"
            print("Układ ASCII: \(name(of: ascii)) (\(id))\(warn)")
        }
        print("Nadpisanie:  \(fallbackLayoutID)")
        print("Kanał:       \(Settings.shared.imeChannelEnabled ? "włączony" : "wyłączony w ustawieniach")"
              + (PeerCheck.ownLeafHash == nil ? " — ta kopia nie jest podpisana certyfikatem, kanał nie zadziała" : ""))
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: ImeProtocol.inputSourceID)
        print("Proces IME:  \(running.isEmpty ? "nie działa (system uruchomi go przy pierwszym użyciu)" : running.map { "pid \($0.processIdentifier)" }.joined(separator: ", "))")

        let semaphore = DispatchSemaphore(value: 0)
        var reply: ImeProtocol.Reply?
        Task.detached {
            reply = await ImeChannel.hello()
            semaphore.signal()
        }
        while semaphore.wait(timeout: .now() + 0.01) == .timedOut { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
        if let reply {
            print("Gniazdo:     odpowiada (IME \(reply.build ?? "?"), pid \(reply.imePid.map(String.init) ?? "?"), działa \(Int(reply.uptime ?? 0)) s, próśb: \(reply.requests ?? 0))")
            print("Aktywne:     \(reply.app ?? "brak pola")")
            let apps = (reply.apps ?? [:]).sorted { $0.value < $1.value }.map { "\($0.key) (\(Int($0.value)) s temu)" }
            print("Aplikacje:   \(apps.isEmpty ? "żadna jeszcze nie aktywowała IME" : apps.joined(separator: ", "))")
        } else {
            print("Gniazdo:     brak odpowiedzi")
        }
        let restart = appsNeedingRestart()
        if !restart.isEmpty { print("Do restartu: \(restart.joined(separator: ", ")) — uruchomione przed instalacją IME") }
        if shortcutsSwitchSources() {
            print("Uwaga:       ⌃Spacja / ⌃⌥Spacja przełączają źródła klawiatury (Ustawienia → Klawiatura → Skróty → Źródła wprowadzania)")
        }
        return 0
    }

    private static func enable() -> Int32 {
        guard isInstalled else {
            print("Brak \(ImeProtocol.installURL.path) — najpierw ./scripts/build-app.sh")
            return 1
        }
        // Zawsze: sama rejestracja w LaunchServices nie wystarcza Ustawieniom (odrzucają prośbę o zgodę
        // jako „Invalid value on inputSourceToBeEnabled”). Powtórna rejestracja niczego nie psuje.
        let status = TISRegisterInputSource(ImeProtocol.installURL as CFURL)
        print("Rejestracja: \(status == noErr ? "OK" : "błąd \(status)")")
        guard let ime = imeSource else {
            print("macOS nie widzi źródła po rejestracji — wyloguj się i zaloguj ponownie, potem powtórz.")
            return 1
        }
        // Układ, który IME ma nadpisywać: obecny układ ASCII (u Ciebie Polski Pro).
        if let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(), let id = identifier(of: ascii),
           id != ImeProtocol.inputSourceID {
            UserDefaults.standard.set(id, forKey: ImeProtocol.DefaultsKey.keyboardLayout)
            print("Układ:       \(name(of: ascii)) (\(id))")
        }
        if !flag(ime, kTISPropertyInputSourceIsEnabled) {
            print("Włączam — macOS pokaże okno zgody, kliknij „Zezwól” / „Allow”…")
            TISEnableInputSource(ime)
            let until = Date().addingTimeInterval(120)
            while Date() < until, !(imeSource.map { flag($0, kTISPropertyInputSourceIsEnabled) } ?? false) {
                RunLoop.current.run(until: Date().addingTimeInterval(0.5))
            }
            guard imeSource.map({ flag($0, kTISPropertyInputSourceIsEnabled) }) == true else {
                print("Źródło nie zostało włączone (brak zgody?).")
                return 1
            }
        }
        if let ime = imeSource { TISSelectInputSource(ime) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        print("Wybrane:     \(ImeChannel.isSourceSelected() ? "Polski (GramaDrama)" : "nie — wybierz w menu klawiatury")")
        EventLog.write("ime_setup", ["action": "enable", "selected": ImeChannel.isSourceSelected()])
        let restart = appsNeedingRestart()
        if !restart.isEmpty {
            print("Te aplikacje działały przed instalacją i mogą nie widzieć IME — zamknij je (⌘Q) i otwórz ponownie")
            print("albo wyloguj się i zaloguj: \(restart.joined(separator: ", "))")
        }
        return 0
    }

    private static func disable() -> Int32 {
        if let layout = source(fallbackLayoutID) { TISSelectInputSource(layout) }
        if let ime = imeSource, flag(ime, kTISPropertyInputSourceIsEnabled) { TISDisableInputSource(ime) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        print("Klawiatura:  \(TISCopyCurrentKeyboardInputSource().map { name(of: $0.takeRetainedValue()) } ?? "?"); GramaDrama wyłączona")
        EventLog.write("ime_setup", ["action": "disable"])
        return 0
    }

    private static func uninstall() -> Int32 {
        _ = disable()
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: ImeProtocol.inputSourceID) { app.terminate() }
        try? FileManager.default.removeItem(at: ImeProtocol.installURL)
        try? FileManager.default.removeItem(at: ImeProtocol.directory)
        print("Usunięto \(ImeProtocol.installURL.path)")
        EventLog.write("ime_setup", ["action": "uninstall"])
        return 0
    }

    // MARK: - Pomocnicze

    private static let firstInstalledKey = "imeFirstInstalledAt"

    /// Aplikacje z oknami uruchomione przed instalacją pakietu IME (mogą jej nie widzieć do restartu).
    static func appsNeedingRestart() -> [String] {
        // Pierwsza instalacja (zapisuje ją scripts/install-ime.sh) — aktualizacje pakietu nie wymagają restartu aplikacji.
        let firstInstalled = UserDefaults.standard.object(forKey: firstInstalledKey) as? Date
        let bundleDate = (try? FileManager.default.attributesOfItem(atPath: ImeProtocol.installURL.path))?[.creationDate] as? Date
        guard let installed = firstInstalled ?? bundleDate else { return [] }
        return NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular && ($0.launchDate ?? .distantFuture) < installed && $0 != .current }
            .compactMap(\.localizedName)
            .sorted()
    }

    /// Czy włączone są systemowe skróty „poprzednie/następne źródło” (⌃Spacja, ⌃⌥Spacja).
    static func shortcutsSwitchSources() -> Bool {
        guard let hotkeys = UserDefaults(suiteName: "com.apple.symbolichotkeys")?.dictionary(forKey: "AppleSymbolicHotKeys") else { return false }
        return ["60", "61"].contains { ((hotkeys[$0] as? [String: Any])?["enabled"] as? Bool) == true }
    }
}
