import AppKit
import Carbon.HIToolbox
import Combine
import ServiceManagement
import SwiftUI
import GramaCore
import GramaBackend

/// Stan widoczny w ustawieniach: czy skróty się zarejestrowały i czy są wstrzymane dla aplikacji na wierzchu.
@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()
    /// nil = wstrzymane (wyłączona aplikacja na wierzchu); inaczej wynik RegisterEventHotKey.
    /// Klucze: `fix` i `translate`.
    @Published var hotKeyStatus: [String: OSStatus?] = [:]
    @Published var suspendedFor: String?
    /// Start → Skróty → Zmień skróty nagrywa nowy skrót: własne skróty są wtedy wyrejestrowane, żeby ich naciśnięcie
    /// trafiło do nagrywania, a nie otworzyło panel.
    @Published var recordingShortcut = false
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
    private var statusItem: NSStatusItem!
    private var hotKeys: [HotKey] = []
    private let panel = PanelController()
    private var settingsWindow: NSWindow?
    private let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let appToggle = NSMenuItem(title: "", action: #selector(toggleFrontmostApp), keyEquivalent: "")
    private let liveToggle = NSMenuItem(title: "", action: #selector(toggleLiveCheck), keyEquivalent: "")
    /// Przełącznik klawiatury: GramaDrama ↔ zwykły układ (bez Ustawień systemowych, bez okna zgody).
    private let keyboardItem = NSMenuItem(title: "", action: #selector(toggleKeyboard), keyEquivalent: "")
    private var lastSourceWasIme: Bool?
    /// Aplikacja, która była na wierzchu, gdy otwarto menu (menu paska nie aktywuje GramaDramy).
    private var menuTargetApp: NSRunningApplication?
    private var observers: [NSObjectProtocol] = []
    private var settingsSubscription: AnyCancellable?
    private var shortcutSubscriptions: [AnyCancellable] = []
    private var languageSubscription: AnyCancellable?
    private var fixItem: NSMenuItem?
    private var translateItem: NSMenuItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Język interfejsu (`UI.language`) ustawiają ustawienia z głównego języka tekstu — przed pierwszym tekstem menu.
        _ = Settings.shared
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let icon = NSImage(systemSymbolName: "theatermasks", accessibilityDescription: "GramaDrama")
        icon?.isTemplate = true
        statusItem.button?.image = icon
        rebuildMenus()
        // Język interfejsu idzie za głównym językiem tekstu — menu od nowa, już z nowym językiem (`@Published` woła
        // przed zapisem).
        languageSubscription = Settings.shared.$textLanguages.dropFirst().sink { [weak self] _ in
            DispatchQueue.main.async { self?.rebuildMenus() }
        }

        updateHotKeys(for: NSWorkspace.shared.frontmostApplication)
        // W wyłączonej aplikacji skróty są wyrejestrowane, więc ⌥⌘G trafia do niej bez zmian.
        observers.append(NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            MainActor.assumeIsolated { self?.updateHotKeys(for: app) }
        })
        settingsSubscription = Settings.shared.$disabledBundleIDs.dropFirst().sink { [weak self] _ in
            DispatchQueue.main.async { self?.updateHotKeys(for: NSWorkspace.shared.frontmostApplication) }
        }
        // Nowy skrót z okna (Start → Skróty) i koniec nagrywania: rejestracja od nowa. `@Published` woła przed zapisem —
        // rejestrujemy w następnym obiegu pętli, już z nowymi wartościami.
        shortcutSubscriptions = [
            Settings.shared.$correctionShortcut.dropFirst().map { _ in () }.eraseToAnyPublisher(),
            Settings.shared.$translationShortcut.dropFirst().map { _ in () }.eraseToAnyPublisher(),
            AppState.shared.$recordingShortcut.dropFirst().map { _ in () }.eraseToAnyPublisher(),
        ].map { publisher in
            publisher.sink { [weak self] in
                DispatchQueue.main.async { self?.reloadHotKeys() }
            }
        }

        // Okno przy ręcznym uruchomieniu (Launchpad, Finder, Spotlight), ale nie przy logowaniu
        // ani z `--background` (skrypty budowania) — wtedy tylko ikona w pasku menu.
        let reason = CommandLine.arguments.contains("--background") ? "background_flag" : Self.launchReason
        EventLog.write("launch", ["reason": reason, "uptime_s": Int(ProcessInfo.processInfo.systemUptime)])
        let background = reason != "manual"
        if !TextBridge.isTrusted(prompt: true) || !background {
            openMainWindow()
        }
        Task { await Backend().warmUp() }
        DictationController.shared.start()
        // Wtyczka przeglądarki: host zarejestrowany w Edge i Chrome, gniazdo dla jego połączeń.
        BrowserChannel.installHostManifests()
        BrowserChannel.start()

        // Zmiana źródła klawiatury z/na GramaDramę (także przypadkowe ⌃Spacja) — do logu, bez treści.
        lastSourceWasIme = ImeChannel.isSourceSelected()
        observers.append(DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name(kTISNotifySelectedKeyboardInputSourceChanged as String), object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                let now = ImeChannel.isSourceSelected()
                guard let self, now != self.lastSourceWasIme else { return }
                self.lastSourceWasIme = now
                EventLog.write("input_source", ["ime": now, "secure_input": IsSecureEventInputEnabled()])
            }
        })

        LiveMonitor.shared.isSuspended = { [weak self] in self?.panel.isOpen ?? false }
        LiveMonitor.shared.onOpen = { [weak self] in self?.correct(mode: .fix) }
        LiveMonitor.shared.start()
    }

    // MARK: Dostęp zdalny (logowanie passkey w Pocket ID)

    /// „Zaloguj się…” / „Zalogowano: …” — stan w menu, kliknięcie loguje albo otwiera okno z kontem.
    private let remoteItem = NSMenuItem(title: "", action: #selector(remoteFromMenu), keyEquivalent: "")

    private func updateRemoteItem() {
        // Bez skonfigurowanej bramy logowanie nie ma sensu — pozycja znika z menu.
        remoteItem.isHidden = RemoteConfig.configuration == nil && !RemoteAccount.shared.isSignedIn
        remoteItem.title = switch RemoteAccount.shared.state {
        case .signedIn(let user): tr("Dostęp zdalny: zalogowano jako {user}", ["user": user])
        case .waitingForBrowser: tr("Dostęp zdalny: czekam na przeglądarkę…")
        case .signedOut: tr("Zaloguj się (dostęp zdalny)…")
        }
    }

    @objc private func remoteFromMenu() {
        if RemoteAccount.shared.isSignedIn {
            MainWindowTab.select(.connection)
            openMainWindow()
        } else {
            Self.signIn()
        }
    }

    /// Przycisk nieudanej korekty, który prowadzi poza panel: zakładka okna aplikacji z tym, co trzeba ustawić,
    /// albo logowanie w dostępie zdalnym (PANEL.md, „Błąd”).
    static func perform(_ action: ProblemAction) {
        switch action {
        case .openServices: openMainWindow(at: .connection)
        case .openWriting: openMainWindow(at: .writing)
        case .login: signIn()
        case .retry: break
        }
    }

    static func openMainWindow(at tab: MainWindowTab) {
        MainWindowTab.select(tab)
        (NSApp.delegate as? AppDelegate)?.openMainWindow()
    }

    /// Strona logowania w domyślnej przeglądarce — tam działa menedżer haseł z passkey (np. Bitwarden).
    static func signIn() {
        do {
            NSWorkspace.shared.open(try RemoteAccount.shared.signInURL())
        } catch {
            MainWindowTab.select(.connection)
            (NSApp.delegate as? AppDelegate)?.openMainWindow()
            Toast.shared.show(error.localizedDescription)
        }
    }

    /// Powrót z przeglądarki: gramadrama://auth?code=…
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where url.scheme == "gramadrama" && url.host == "auth" {
            Task {
                do {
                    let user = try await RemoteAccount.shared.handle(url)
                    Route.shared.forget()
                    Toast.shared.show(tr("Zalogowano jako {user}. Dostęp zdalny jest gotowy.", ["user": user]))
                } catch {
                    Toast.shared.show(error.localizedDescription)
                }
                MainWindowTab.select(.connection)
                openMainWindow()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
    }

    /// Ponowne uruchomienie działającej aplikacji (Launchpad, Finder, Dock) otwiera jej okno.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openMainWindow()
        return true
    }

    /// Start z logowania: znacznik w zdarzeniu „otwórz aplikację” albo — gdy system go nie poda (rzeczy otwierane
    /// przy logowaniu przez SMAppService nie zawsze go ustawiają) — włączony start przy logowaniu i świeżo
    /// uruchomiony system. Ręczne uruchomienie tuż po starcie Maca też wtedy nie otworzy okna — wystarczy
    /// uruchomić ponownie albo wybrać „Otwórz GramaDramę…” z menu.
    private static var launchReason: String {
        if let event = NSAppleEventManager.shared().currentAppleEvent, event.eventID == kAEOpenApplication,
           event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem {
            return "login_event"
        }
        if SMAppService.mainApp.status == .enabled, ProcessInfo.processInfo.systemUptime < 300 { return "login_uptime" }
        return "manual"
    }

    /// Skróty od nowa: zmienione w oknie albo po nagrywaniu (wtedy były wyrejestrowane).
    private func reloadHotKeys() {
        hotKeys = []  // deinit wyrejestrowuje
        updateHotKeys(for: NSWorkspace.shared.frontmostApplication)
        updateMenuShortcuts()
    }

    private func updateHotKeys(for app: NSRunningApplication?) {
        let state = AppState.shared
        if let app, app != NSRunningApplication.current, Settings.shared.isDisabled(app.bundleIdentifier) {
            hotKeys = []  // deinit wyrejestrowuje
            DictationController.shared.setSuspended(true)
            state.suspendedFor = app.localizedName ?? app.bundleIdentifier
            state.hotKeyStatus = ["fix": nil, "translate": nil]
            return
        }
        DictationController.shared.setSuspended(false)
        state.suspendedFor = nil
        if state.recordingShortcut {
            hotKeys = []
            state.hotKeyStatus = ["fix": nil, "translate": nil]
            return
        }
        guard hotKeys.isEmpty else { return }
        let settings = Settings.shared
        hotKeys = [
            HotKey(id: 1, shortcut: settings.correctionShortcut) { [weak self] in self?.correct(mode: .fix, appDefault: true) },
            HotKey(id: 2, shortcut: settings.translationShortcut) { [weak self] in self?.correct(mode: .translate) },
        ]
        state.hotKeyStatus = ["fix": hotKeys[0].status, "translate": hotKeys[1].status]
        for (name, hotKey) in zip(["fix", "translate"], hotKeys) where !hotKey.isRegistered {
            EventLog.write("hotkey_failed", ["hotkey": name, "status": Int(hotKey.status)])
        }
    }

    @objc private func toggleLiveCheck() {
        Settings.shared.liveCheckEnabled.toggle()
        if !Settings.shared.liveCheckEnabled { LiveMonitor.shared.reset() }
    }

    @objc private func toggleFrontmostApp() {
        guard let app = menuTargetApp, let bundleID = app.bundleIdentifier else { return }
        Settings.shared.setDisabled(bundleID, !Settings.shared.isDisabled(bundleID))
        let name = app.localizedName ?? bundleID
        Toast.shared.show(Settings.shared.isDisabled(bundleID)
            ? tr("GramaDrama wyłączona w: {name}. Skróty trafiają tam bez zmian.", ["name": name])
            : tr("GramaDrama znów działa w: {name}.", ["name": name]))
    }

    /// Menu w pasku i niewidoczne menu główne — przy starcie i po zmianie języka interfejsu. Stałe pozycje (stan,
    /// przełączniki) przechodzą do nowego menu, więc najpierw wychodzą ze starego.
    private func rebuildMenus() {
        statusItem.menu?.removeAllItems()
        statusItem.menu = buildMenu()
        NSApp.mainMenu = Self.buildEditMenu()
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        let fix = NSMenuItem(title: tr("Popraw tekst"), action: #selector(fixFromMenu), keyEquivalent: "")
        menu.addItem(fix)
        // Tytuł z parą języków ustawia `menuWillOpen`.
        let translate = NSMenuItem(title: "", action: #selector(translateFromMenu), keyEquivalent: "")
        menu.addItem(translate)
        fixItem = fix
        translateItem = translate
        updateMenuShortcuts()
        menu.addItem(DictationController.shared.menuItem)
        menu.addItem(.separator())
        liveToggle.title = tr("Sprawdzaj podczas pisania")
        liveToggle.target = self
        menu.addItem(liveToggle)
        menu.addItem(appToggle)
        menu.addItem(keyboardItem)
        statusLine.title = tr("Sprawdzam usługi…")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        remoteItem.target = self
        menu.addItem(remoteItem)
        menu.addItem(NSMenuItem(title: tr("Otwórz GramaDramę…"), action: #selector(openMainWindow), keyEquivalent: ","))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: tr("Zakończ GramaDramę"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        for item in menu.items where item.action != #selector(NSApplication.terminate(_:)) { item.target = self }
        return menu
    }

    /// Skróty przy pozycjach menu — tylko do pokazania (działają skróty globalne), te same co w Start → Skróty.
    private func updateMenuShortcuts() {
        for (item, shortcut) in [(fixItem, Settings.shared.correctionShortcut), (translateItem, Settings.shared.translationShortcut)] {
            guard let item else { continue }
            item.keyEquivalent = shortcut.letter.lowercased()
            item.keyEquivalentModifierMask = [(GlobalShortcut.Modifiers.control, NSEvent.ModifierFlags.control), (.option, .option),
                                              (.shift, .shift), (.command, .command)]
                .reduce(into: NSEvent.ModifierFlags()) { if shortcut.modifiers.contains($1.0) { $0.insert($1.1) } }
        }
    }

    /// Niewidoczne menu główne — bez niego ⌘C/⌘V/⌘A/⌘Z nie działają w polach ustawień
    /// (w AppKit te skróty idą przez pozycje menu Edycja).
    static func buildEditMenu() -> NSMenu {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            let item = NSMenuItem()
            item.submenu = menu
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector?, _ key: String, _ modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            return item
        }
        // Menu aplikacji (widoczne, gdy otwarte jest okno GramaDramy i aplikacja ma ikonę w Docku).
        submenu("GramaDrama", [
            item(tr("O GramaDramie"), #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item(tr("Ukryj GramaDramę"), #selector(NSApplication.hide(_:)), "h"),
            .separator(),
            item(tr("Zakończ GramaDramę"), #selector(NSApplication.terminate(_:)), "q"),
        ])
        // ⌘C/⌘V/⌘A/⌘Z działają w AppKit przez menu Edycja — także w polach okna i w panelu.
        submenu(tr("Edycja"), [
            item(tr("Cofnij"), Selector(("undo:")), "z"),
            item(tr("Ponów"), Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item(tr("Wytnij"), #selector(NSText.cut(_:)), "x"),
            item(tr("Kopiuj"), #selector(NSText.copy(_:)), "c"),
            item(tr("Wklej"), #selector(NSText.paste(_:)), "v"),
            item(tr("Zaznacz wszystko"), #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu(tr("Okno"), [
            item(tr("Zamknij"), #selector(NSWindow.performClose(_:)), "w"),
            item(tr("Minimalizuj"), #selector(NSWindow.performMiniaturize(_:)), "m"),
        ])
        return main
    }

    nonisolated func menuWillOpen(_ menu: NSMenu) {
        MainActor.assumeIsolated {
            updateRemoteItem()
            // Tłumaczenie między wybranymi językami tekstu; przy jednym — wyłączone.
            let languages = Settings.shared.textLanguages
            translateItem?.title = languages.translates ? tr("Przetłumacz tekst ({pair})", ["pair": languages.pairLabel])
                                                        : tr("Przetłumacz tekst (jeden język)")
            translateItem?.isEnabled = languages.translates
            let front = NSWorkspace.shared.frontmostApplication
            let usable = front != nil && front != NSRunningApplication.current && front?.bundleIdentifier != nil
            menuTargetApp = usable ? front : nil
            liveToggle.state = Settings.shared.liveCheckEnabled ? .on : .off
            switch ImeSetup.menuState {
            case .notInstalled: keyboardItem.isHidden = true
            case .selected:
                keyboardItem.isHidden = false
                keyboardItem.title = tr("Przełącz klawiaturę na {layout}", ["layout": ImeSetup.fallbackLayoutName])
            case .available:
                keyboardItem.isHidden = false
                keyboardItem.title = tr("Przełącz klawiaturę na {layout}", ["layout": "Polski (GramaDrama)"])
            }
            appToggle.isHidden = !usable
            if let front, usable {
                let name = front.localizedName ?? front.bundleIdentifier ?? ""
                appToggle.title = Settings.shared.isDisabled(front.bundleIdentifier)
                    ? tr("Włącz GramaDramę w: {name}", ["name": name]) : tr("Wyłącz GramaDramę w: {name}", ["name": name])
            }
        }
        Task { @MainActor in
            let status = await Backend().status()
            let model = Settings.shared.modelDisplayName
            let local = Settings.shared.modelLocation == .local
            let remote = !local && Route.shared.kinds[.model] == .remote ? " · " + tr("zdalnie") : ""
            statusLine.title = switch (status.model, status.languageTool) {
            case (true, true): "● \(local ? "Ollama" : tr("Serwer")) OK · \(model)\(remote)"
            case (true, false): "● \(model) · LanguageTool offline\(remote)"
            case (false, _): local ? tr("○ Ollama nie działa") : tr("○ Serwer niedostępny")
            }
        }
    }

    @objc private func toggleKeyboard() { ImeSetup.toggleKeyboard() }

    @objc private func fixFromMenu() { correctFromMenu(mode: .fix, appDefault: true) }
    @objc private func translateFromMenu() { correctFromMenu(mode: .translate) }

    private func correctFromMenu(mode: Mode, appDefault: Bool = false) {
        // Menu zabrało fokus — oddajemy go, zanim pobierzemy tekst.
        NSApp.hide(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.correct(mode: mode, appDefault: appDefault) }
    }

    /// `appDefault`: ⌥⌘G i „Popraw tekst” otwierają panel w trybie ustawionym dla aplikacji (Ustawienia → Aplikacje);
    /// plakietka z błędami zawsze w trybie Popraw.
    private func correct(mode: Mode, appDefault: Bool = false) {
        // Drugie ⌥⌘G przy otwartym panelu go zamyka. Ponowne pobieranie tekstu nie ma sensu (panel ma
        // fokus, więc ⌘C trafiłoby do niego, a nie do Slacka) — zaznaczenie w aplikacji się nie zmieniło.
        if panel.isOpen {
            panel.close()
            return
        }
        LiveMonitor.shared.cancelModelCheck()
        EventLog.write("hotkey", ["mode": mode.rawValue, "app": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""])
        // Operacja na schowku jeszcze trwa — kolejna by go nadpisała.
        guard !TextBridge.isBusy else {
            Toast.shared.show(CaptureError.busy.localizedDescription)
            return
        }
        Task {
            do {
                let captured = try await TextBridge.capture()
                panel.show(captured, mode: appDefault ? Settings.shared.defaultMode(for: captured.app?.bundleIdentifier) : mode)
            } catch CaptureError.busy {
                Toast.shared.show(CaptureError.busy.localizedDescription)
            } catch CaptureError.notTrusted {
                notify(CaptureError.notTrusted.localizedDescription)
                openMainWindow()
            } catch {
                // Odmowy (pole hasła, brak tekstu, za długi tekst) — krótki komunikat, bez zabierania fokusu.
                Toast.shared.show(error.localizedDescription)
            }
        }
    }

    /// Modalny alert — tylko przy nadawaniu uprawnień, gdzie użytkownik i tak przechodzi do Ustawień.
    private func notify(_ message: String) {
        let previous = NSWorkspace.shared.frontmostApplication
        let alert = NSAlert()
        alert.messageText = "GramaDrama"
        alert.informativeText = message
        alert.alertStyle = .informational
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        // Alert zabrał fokus — oddajemy go aplikacji, w której użytkownik pisał.
        if let previous, previous != NSRunningApplication.current { previous.activate(options: []) }
    }

    /// Okno aplikacji (stan, Wypróbuj, skróty, aplikacje, serwer). Gdy jest otwarte, GramaDrama ma ikonę
    /// w Docku i w ⌘Tab; po zamknięciu wraca do samego paska menu i działa dalej.
    @objc private func openMainWindow() {
        if settingsWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: MainWindowView()))
            window.title = "GramaDrama"
            window.styleMask = [.titled, .closable, .miniaturizable, .fullSizeContentView]
            window.titlebarAppearsTransparent = true
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            settingsWindow = window
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        guard (notification.object as? NSWindow) === settingsWindow else { return }
        NSApp.setActivationPolicy(.accessory)
    }
}
