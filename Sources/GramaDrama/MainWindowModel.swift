import AVFoundation
import ServiceManagement
import SwiftUI
import GramaCore
import GramaBackend

/// Stan okna wspólny dla zakładek: usługi, uprawnienia, metoda wprowadzania, ostatnie użycie. Start pokazuje
/// z tego podsumowanie i problemy, a zakładki — szczegóły.
@MainActor
final class MainWindowModel: ObservableObject {
    @Published var diagnostics: Backend.Diagnostics?
    @Published var checkingServices = false
    @Published var accessibility = TextBridge.isTrusted(prompt: false)
    @Published var microphone = AVCaptureDevice.authorizationStatus(for: .audio)
    @Published var loginStatus = SMAppService.mainApp.status
    @Published var loginError: String?
    @Published var ime: ImeSetup.Snapshot?
    @Published var imeMessage: String?
    @Published var imeBusy = false
    @Published var apps: [EventLog.AppUsage] = []
    @Published var lastUse: EventLog.LastUse?
    /// Pole Wypróbuj: co się dzieje ze skrótem użytym w tym oknie.
    @Published var trialStatus: (ok: Bool?, text: String)?
    @Published var speechStatus: ServiceStatus?
    @Published var speechChecking = false
    @Published var testResult: String?
    @Published var testing = false
    /// Była już udana korekta (data pierwszej w ustawieniach, a sprzed jej zapisywania — zdarzenie w logu).
    @Published var hasCorrected = MainWindowModel.correctedBefore()
    private var refreshGeneration = 0

    /// Data pierwszej udanej korekty (`CorrectionSession`): od niej okno nie pokazuje już Pierwszych kroków.
    static let firstCorrectionKey = "firstCorrectionAt"

    private static func correctedBefore() -> Bool {
        UserDefaults.standard.object(forKey: firstCorrectionKey) != nil || EventLog.contains("session")
    }

    /// Nowa instalacja, w której model jeszcze nie działa (na Linuksie: nie wybrano modelu; na Macu model jest wybrany
    /// od początku, więc liczy się, czy była już udana korekta). Start pokazuje wtedy Pierwsze kroki, a pigułka mówi
    /// „Jeszcze jeden krok”, nie „Wymaga uwagi” — to krok do zrobienia, nie awaria.
    var firstRun: Bool {
        !hasCorrected && (diagnostics == nil || problems.contains { $0 == .modelServer || $0 == .model })
    }

    /// Modele z serwera modelu (na tym Macu — pobrane do Ollamy).
    var models: [String] { diagnostics?.models ?? [] }

    /// Coś, co wymaga uwagi — z zakładką, w której to naprawisz.
    enum Problem: CaseIterable {
        case permission, microphone, hotKey, modelServer, model, speechServer

        var title: String {
            switch self {
            case .permission: tr("uprawnienie")
            case .microphone: tr("mikrofon")
            case .hotKey: tr("skrót")
            case .modelServer: Settings.shared.modelLocation == .local ? "Ollama" : tr("serwer modelu")
            case .model: tr("model")
            case .speechServer: tr("serwer mowy")
            }
        }

        var tab: MainWindowTab {
            switch self {
            case .permission, .microphone, .hotKey: .start
            case .modelServer, .model, .speechServer: .connection
            }
        }
    }

    var problems: [Problem] {
        var list: [Problem] = []
        if !accessibility { list.append(.permission) }
        if DictationSettings.shared.enabled, [.denied, .restricted].contains(microphone) { list.append(.microphone) }
        if AppState.shared.hotKeyStatus.values.contains(where: { $0 != nil && $0 != noErr }) { list.append(.hotKey) }
        if let diagnostics {
            if !diagnostics.modelReachable { list.append(.modelServer) }
            // Ollama bez żadnego modelu to też brak modelu (pusta lista, nie „serwer nie podał listy”).
            else if let models = diagnostics.models, !models.contains(Settings.shared.model) { list.append(.model) }
        }
        if DictationSettings.shared.enabled, DictationSettings.shared.speechBackend == .server {
            if DictationSettings.shared.speechEndpoint == nil { list.append(.speechServer) }
            else if case .error = speechStatus { list.append(.speechServer) }
        }
        return list
    }

    // MARK: Odświeżanie

    func refresh() async {
        await refreshServices()
        refreshSystem()
        apps = EventLog.recentApps()
        lastUse = EventLog.lastUse()
        ime = await ImeSetup.snapshot()
    }

    /// Model językowy (i Ollama na tym Macu), LanguageTool i serwer mowy — przy otwarciu okna, po zmianie
    /// ustawień usług i po „Sprawdź”.
    func refreshServices() async {
        refreshGeneration += 1
        let current = refreshGeneration
        checkingServices = true
        await LocalOllama.shared.refresh()
        let newDiagnostics = await Backend().diagnostics()
        guard current == refreshGeneration else { return }
        diagnostics = newDiagnostics
        checkingServices = false
        await checkSpeechServer()
    }

    /// Po powrocie z Ustawień systemowych (uprawnienia, rzeczy otwierane przy logowaniu).
    func refreshSystem() {
        loginStatus = SMAppService.mainApp.status
        accessibility = TextBridge.isTrusted(prompt: false)
        microphone = AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Stan serwera mowy tą samą drogą co dyktowanie: w domu wprost, poza domem przez bramę.
    func checkSpeechServer() async {
        guard DictationSettings.shared.speechBackend == .server,
              let resolved = try? await SpeechRecognition.serverClient() else {
            speechStatus = nil
            return
        }
        let (client, viaGateway) = resolved
        speechChecking = true
        defer { speechChecking = false }
        let status = await client.check()
        guard viaGateway else {
            speechStatus = status
            return
        }
        speechStatus = status.appending(" · " + tr("przez bramę") + " \(client.endpoint.host)")
    }

    // MARK: Akcje

    /// Test połączenia — zawsze prawdziwe zapytanie, nie wynik z pamięci.
    func runTest() async {
        testing = true
        defer { testing = false }
        let started = Date()
        do {
            // Zdanie z błędami w głównym języku tekstu.
            let sample = switch Settings.shared.textLanguages.primary {
            case .english: "I think its a good idea."
            case .german: "Ich denke das ist eine gute idee."
            default: "Mysle ze to jest dobry pomysl."
            }
            let result = try await Backend(cache: false).correct(sample, mode: .fix) { _ in }
            testResult = tr("{time} — „{result}”", ["time": WindowText.seconds(Int(Date().timeIntervalSince(started) * 1000)), "result": result])
        } catch let error as URLError where Settings.shared.modelLocation == .local {
            testResult = error.code == .timedOut ? tr("Ollama nie odpowiedziała na czas.") : tr("Ollama na tym Macu nie odpowiada.")
        } catch {
            testResult = error.localizedDescription
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginError = nil
        } catch {
            loginError = tr("Nie udało się: {error}", ["error": error.localizedDescription])
        }
        loginStatus = SMAppService.mainApp.status
        EventLog.write("login_item", ["enabled": enabled, "status": Self.loginStatusName(loginStatus), "error": loginError != nil])
    }

    func requestMicrophone() {
        if microphone == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in self.microphone = AVCaptureDevice.authorizationStatus(for: .audio) }
            }
        } else {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
        }
    }

    func enableIME() {
        imeBusy = true
        imeMessage = tr("Kliknij „Zezwól” / „Allow” w oknie Ustawień systemowych.")
        Task {
            imeMessage = await ImeSetup.enableFromApp()
            imeBusy = false
            ime = await ImeSetup.snapshot()
        }
    }

    func toggleKeyboard() {
        ImeSetup.toggleKeyboard()
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            ime = await ImeSetup.snapshot()
        }
    }

    /// Zdarzenia z logu (bez treści): stan pola Wypróbuj, ostatnie użycie, lista aplikacji, metoda wprowadzania.
    func handleEvent(_ entry: [String: Any]) {
        guard let event = entry["event"] as? String else { return }
        if ["input_source", "ime_setup"].contains(event) { Task { ime = await ImeSetup.snapshot() } }
        if ["hotkey", "capture", "capture_refused", "replace"].contains(event) { lastUse = EventLog.lastUse() }
        if event.hasPrefix("capture") || event == "replace" { apps = EventLog.recentApps() }
        // Pole Wypróbuj: tylko zdarzenia z tego okna.
        guard (entry["app"] as? String) == Bundle.main.bundleIdentifier else { return }
        switch event {
        case "hotkey":
            trialStatus = (nil, tr("Skrót dotarł — pobieram tekst…"))
        case "capture":
            trialStatus = (nil, tr("Pobrano {scope} — czekam na model…", ["scope": tr(entry["scope"] as? String ?? "tekst")]))
        case "capture_refused":
            trialStatus = (false, tr("Skrót dotarł, ale: {reason}.", ["reason": tr(EventLog.reasonText(entry["reason"] as? String))]))
        case "session":
            hasCorrected = true
            let changes = entry["changes"] as? Int ?? 0
            let count = changes == 0 ? tr("bez zmian") : UI.count(changes, ("poprawka", "poprawki", "poprawek"), ("change", "changes"))
            trialStatus = (true, tr("Gotowe w {time}: {changes} — ↩ wstawi, ⎋ anuluje.",
                                    ["time": WindowText.seconds(entry["ms"] as? Int ?? 0), "changes": count]))
        case "session_failed":
            trialStatus = (false, tr("Model nie odpowiedział — sprawdź zakładkę Usługi."))
        case "replace":
            let outcome = entry["outcome"] as? String ?? ""
            trialStatus = outcome == "pasted"
                ? (true, tr("Zastosowano. Skrót, aplikacja i serwer działają."))
                : (false, tr("Nie wklejono ({outcome}) — poprawka jest w schowku.", ["outcome": outcome]))
        case "dictation":
            let outcome = entry["outcome"] as? String ?? ""
            let cleanup = entry["cleanup"] as? String ?? "off"
            let detail = switch cleanup {
            case "ok": tr(", porządkowanie {time}.", ["time": WindowText.seconds(entry["cleanup_ms"] as? Int ?? 0)])
            case "not_needed", "off": "."
            case "rejected": tr(" — model za bardzo zmienił tekst, wkleiłem surowy.")
            default: tr(" — bez porządkowania ({reason}).", ["reason": cleanup])
            }
            trialStatus = outcome == "inserted"
                ? (["ok", "not_needed", "off"].contains(cleanup),
                   tr("Podyktowano: rozpoznanie {time}", ["time": WindowText.seconds(entry["asr_ms"] as? Int ?? 0)]) + detail)
                : (false, tr("Dyktowanie: {outcome}.", ["outcome": outcome]))
        default:
            break
        }
    }

    static func loginStatusName(_ status: SMAppService.Status) -> String {
        switch status {
        case .enabled: "enabled"
        case .requiresApproval: "requires_approval"
        case .notRegistered: "not_registered"
        case .notFound: "not_found"
        @unknown default: "unknown"
        }
    }
}
