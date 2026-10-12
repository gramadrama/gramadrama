import AppKit
import AVFoundation
import Combine
import GramaCore
import GramaBackend

/// Dyktowanie w stylu Wispr Flow: trzymasz Fn (albo prawy ⌥), mówisz, puszczasz.
/// Mowa → Whisper (na Macu albo na serwerze mowy z ustawień) → opcjonalnie model porządkuje tekst → wklejenie
/// w miejscu kursora. Nagranie nie jest nigdzie zapisywane; poza Maca wychodzi tylko do wybranego serwera mowy,
/// a do modelu językowego trafia sam tekst transkrypcji.
@MainActor
final class DictationController: ObservableObject {
    static let shared = DictationController()

    enum Phase: Equatable { case idle, listening, recognizing, cleaning }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var level: Float = 0
    @Published private(set) var startedAt = Date()
    /// Język bieżącego dyktowania: z ustawień albo drugi (⇧).
    @Published private(set) var language = DictationLanguage.polish
    /// Rozpoznany dotąd tekst do pastylki — tylko podgląd, wklejamy pełne rozpoznanie po puszczeniu klawisza.
    @Published private(set) var preview = DictationPreviewText()

    /// Pozycja informacyjna w menu paska (stan modelu i klawisz).
    let menuItem = NSMenuItem(title: tr("Dyktowanie"), action: nil, keyEquivalent: "")

    /// Krótsze nagranie to przypadkowe naciśnięcie.
    static let minimumSeconds = 0.4
    /// Najgłośniejszy fragment cichszy niż ok. -50 dBFS = cisza (Whisper wymyśliłby z niej tekst).
    nonisolated static let silencePeak: Float = 0.003
    /// Dłużej nie czekamy na model — wklejamy surową transkrypcję.
    static let cleanupTimeout: TimeInterval = 10
    /// Heurystyka budżetu: 7,8 tokenu/s i 2,3 bajta/token dla polskiej transkrypcji.
    /// Dłuższe dyktowanie i tak nie zmieściłoby się w limicie — wtedy nie każemy czekać.
    static func estimatedCleanupSeconds(_ transcript: String) -> TimeInterval {
        Double(transcript.utf8.count) / 2.3 / 7.8 + 0.5
    }

    private let settings = DictationSettings.shared
    private let monitor = HoldKeyMonitor()
    private let hud = DictationHUD()
    private var recorder: AudioRecorder?
    private var previewer: DictationPreview?
    private var targetApp: NSRunningApplication?
    /// Pole z początku dyktowania według metody wprowadzania — wstawiamy tylko do niego.
    private var imeClient: String?
    private var levelTimer: Timer?
    private var limitTask: Task<Void, Never>?
    private var subscriptions: Set<AnyCancellable> = []
    private var started = false
    /// Na wierzchu jest aplikacja wyłączona w GramaDramie — jak skróty Carbon (AppDelegate.updateHotKeys).
    private var suspended = false

    private struct CleanupTimeout: Error {}

    /// Tylko do zrzutów pastylki (`--preview-dictation`): stan bez nagrywania.
    func showForScreenshot(phase: Phase, language: DictationLanguage, level: Float, preview: DictationPreviewText) {
        self.phase = phase
        self.language = language
        self.level = level
        self.preview = preview
        startedAt = Date().addingTimeInterval(-17)
    }

    func start() {
        started = true
        menuItem.isEnabled = false
        monitor.onStart = { [weak self] in self?.begin() }
        monitor.onStop = { [weak self] in self?.finish() }
        monitor.onCancel = { [weak self] in self?.cancel() }
        monitor.onAlternate = { [weak self] in
            guard let self, self.phase == .listening else { return }
            self.language = self.settings.language.alternate(in: Settings.shared.textLanguages)
            self.previewer?.language = self.language
        }
        // Inne języki tekstu: „(+⇧ = …)” w menu.
        Settings.shared.$textLanguages.dropFirst()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuItem() }
            .store(in: &subscriptions)
        // @Published publikuje przed zapisem — używamy przekazanych wartości, nie odczytu z ustawień.
        settings.$enabled.combineLatest(settings.$key)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] enabled, key in self?.configure(enabled: enabled, key: key) }
            .store(in: &subscriptions)
        // Powrót na „Ten Mac” wczytuje model od razu (jak przy włączaniu dyktowania).
        settings.$speechBackend.dropFirst()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                self.configure(enabled: self.settings.enabled, key: self.settings.key)
            }
            .store(in: &subscriptions)
        SpeechModel.shared.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuItem() }
            .store(in: &subscriptions)
    }

    /// Wyłączona aplikacja: nie słuchamy klawisza w ogóle, więc Fn i ⌥ działają tam jak zwykle.
    func setSuspended(_ suspended: Bool) {
        guard suspended != self.suspended else { return }
        self.suspended = suspended
        if started { configure(enabled: settings.enabled, key: settings.key) }
    }

    private func configure(enabled: Bool, key: DictationKey) {
        if enabled, !suspended {
            monitor.start(key: key)
            // Model wczytujemy od razu, żeby pierwsze dyktowanie nie czekało (albo nie pobierało 1,6 GB).
            // Przy serwerze mowy model na Macu nie jest potrzebny — ani pobierany, ani wczytywany.
            if settings.speechBackend == .local, SpeechModel.shared.state == .notLoaded {
                Task { try? await SpeechModel.shared.prepare() }
            }
        } else {
            monitor.stop()
            if phase == .listening { cancel() }
        }
        updateMenuItem()
    }

    private func updateMenuItem() {
        let key = settings.key.label
        // „(+⇧ = angielski)” / „(+⇧ = English)” — tylko przy dwóch językach tekstu.
        let languages = Settings.shared.textLanguages
        let other = settings.language.alternate(in: languages).textLanguage.map { UI.isEnglish ? $0.label : $0.label.lowercased() }
        let alternate = languages.translates ? " (+⇧ = \(other ?? tr("rozpoznaj")))" : ""
        if settings.enabled, settings.speechBackend == .server {
            menuItem.title = tr("Dyktowanie (serwer mowy): przytrzymaj {key} i mów{alternate}", ["key": key, "alternate": alternate])
            return
        }
        menuItem.title = switch (settings.enabled, SpeechModel.shared.state) {
        case (false, _): tr("Dyktowanie wyłączone")
        case (true, .ready), (true, .notLoaded):
            tr("Dyktowanie: przytrzymaj {key} i mów{alternate}", ["key": key, "alternate": alternate])
        case (true, .downloading(let fraction)): tr("Dyktowanie: pobieram model mowy… {percent}%", ["percent": Int(fraction * 100)])
        case (true, .loading): tr("Dyktowanie: wczytuję model mowy…")
        case (true, .failed): tr("Dyktowanie: błąd modelu mowy (zob. Ustawienia)")
        }
    }

    // MARK: Nagrywanie

    private func begin() {
        guard phase == .idle else {
            monitor.reset()
            Toast.shared.show(tr("Poprzednie dyktowanie jeszcze trwa."))
            return
        }
        let front = NSWorkspace.shared.frontmostApplication
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            break
        case .notDetermined:
            monitor.reset()
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                Task { @MainActor in
                    Toast.shared.show(granted ? tr("Mikrofon włączony — przytrzymaj {key} i mów.", ["key": DictationSettings.shared.key.label])
                                              : tr("Bez mikrofonu dyktowanie nie zadziała."))
                }
            }
            return
        default:
            monitor.reset()
            Toast.shared.show(tr("GramaDrama nie ma dostępu do mikrofonu. Włącz go w Ustawieniach systemowych → Prywatność i ochrona → Mikrofon."),
                              duration: 5)
            return
        }
        if settings.speechBackend == .server {
            // Błędny albo pusty adres serwera: komunikat od razu, zamiast nagrania, którego nie będzie gdzie wysłać.
            do {
                let endpoint = try SpeechRecognition.configuredEndpoint()
                // Droga (dom albo brama) sprawdza się, póki mówisz — po puszczeniu klawisza jest już znana.
                Task { _ = await Route.shared.speechEndpoint(endpoint) }
            } catch {
                monitor.reset()
                Toast.shared.show(error.localizedDescription, duration: 5)
                EventLog.write("dictation", ["app": front?.bundleIdentifier ?? "", "outcome": "asr_error", "asr": "server",
                                             "asr_error": SpeechRecognition.errorKind(error)])
                return
            }
        } else {
            switch SpeechModel.shared.state {
            case .downloading(let fraction):
                monitor.reset()
                Toast.shared.show(tr("Model mowy jeszcze się pobiera ({percent}%).", ["percent": Int(fraction * 100)]))
                return
            case .failed:
                monitor.reset()
                Toast.shared.show(tr("Model mowy się nie wczytał — próbuję ponownie. Szczegóły w Ustawieniach."))
                Task { try? await SpeechModel.shared.prepare() }
                return
            case .loading where Date().timeIntervalSince(SpeechModel.shared.loadingSince ?? Date()) > 5:
                // Pierwsze wczytanie (kompilacja pod Neural Engine) trwa minuty — nie każemy czekać po nagraniu.
                monitor.reset()
                Toast.shared.show(tr("Model mowy przygotowuje się pierwszy raz (kilka minut). Spróbuj za chwilę."))
                return
            default:
                break  // zwykłe wczytywanie (sekunda–dwie) zdąży, zanim skończysz mówić
            }
        }

        let recorder = AudioRecorder()
        do {
            try recorder.start()
        } catch {
            monitor.reset()
            Toast.shared.show(tr("Nie mogę włączyć mikrofonu: {error}", ["error": error.localizedDescription]))
            EventLog.write("dictation", ["app": front?.bundleIdentifier ?? "", "outcome": "mic_error"])
            return
        }
        self.recorder = recorder
        targetApp = front
        imeClient = nil
        if ImeChannel.rule(for: front)?.insert == true {
            Task { @MainActor [weak self] in
                let hello = await ImeChannel.hello()
                if hello?.app == front?.bundleIdentifier { self?.imeClient = hello?.client }
            }
        }
        // Serwer przelicza prompt porządkowania, póki mówisz aby ograniczyć czas oczekiwania po nagraniu.
        if settings.cleanup != .off {
            let vocabulary = settings.vocabulary
            Task.detached { await Backend().prewarmDictation(vocabulary: vocabulary) }
        }
        startedAt = Date()
        let languages = Settings.shared.textLanguages
        language = monitor.alternate ? settings.language.alternate(in: languages) : settings.language.effective(in: languages)
        level = 0
        preview = DictationPreviewText()
        // Podgląd w pastylce liczy Whisper na Macu co sekundę — przy serwerze mowy go nie ma (wysyłanie rosnącego
        // nagrania co sekundę obciążałoby serwer i sieć, a model na Macu nie jest wtedy wczytany).
        if settings.speechBackend == .local {
            let previewer = DictationPreview(language: language, vocabulary: settings.vocabulary)
            previewer.onChange = { [weak self] in self?.preview = $0 }
            previewer.start { [recorder] from in recorder.snapshot(from: from) }
            self.previewer = previewer
        }
        phase = .listening
        hud.show(controller: self)
        levelTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 20, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.level = self?.recorder?.level ?? 0 }
        }
        limitTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(AudioRecorder.maxSeconds * 1_000_000_000))
            guard !Task.isCancelled, let self, self.phase == .listening else { return }
            self.monitor.reset()
            self.finish()
        }
    }

    private func stopRecording() -> (samples: [Float], peak: Float) {
        levelTimer?.invalidate()
        levelTimer = nil
        limitTask?.cancel()
        previewer?.cancel()
        let result = recorder?.stop() ?? ([], 0)
        recorder = nil
        return result
    }

    /// Inny klawisz w trakcie trzymania (np. Fn+←) — to był skrót, nie dyktowanie.
    private func cancel() {
        guard phase == .listening else { return }
        _ = stopRecording()
        previewer = nil
        phase = .idle
        hud.hide()
        EventLog.write("dictation", ["app": targetApp?.bundleIdentifier ?? "", "outcome": "cancelled",
                                     "audio_ms": Int(Date().timeIntervalSince(startedAt) * 1000)])
    }

    private func finish() {
        guard phase == .listening else { return }
        let (samples, peak) = stopRecording()
        let seconds = Double(samples.count) / AudioRecorder.sampleRate
        let app = targetApp
        // Log bez treści: ile przebiegów podglądu i ile średnio trwały.
        let previewFields: [String: Any] = previewer.map {
            ["preview_passes": $0.passes, "preview_ms": $0.passes == 0 ? 0 : Int($0.passSeconds / Double($0.passes) * 1000)]
        } ?? [:]
        previewer = nil
        guard seconds >= Self.minimumSeconds else {
            phase = .idle
            hud.hide()
            return
        }
        guard peak >= Self.silencePeak else {
            phase = .idle
            hud.hide()
            Toast.shared.show(tr("Nic nie usłyszałem — sprawdź, czy mikrofon działa."))
            EventLog.write("dictation", ["app": app?.bundleIdentifier ?? "", "outcome": "silence", "audio_ms": Int(seconds * 1000)])
            return
        }
        phase = .recognizing
        let language = language
        Task { await process(samples, seconds: seconds, language: language, app: app, previewFields: previewFields) }
    }

    // MARK: Rozpoznanie, porządkowanie, wklejenie

    private func process(_ samples: [Float], seconds: Double, language: DictationLanguage, app: NSRunningApplication?,
                         previewFields: [String: Any]) async {
        // Log bez treści: tylko długości, czasy i wynik.
        var fields: [String: Any] = ["app": app?.bundleIdentifier ?? "", "audio_ms": Int(seconds * 1000),
                                     "language": language.rawValue,
                                     "alternate": language != settings.language.effective(in: Settings.shared.textLanguages)]
        fields.merge(previewFields) { $1 }
        defer {
            phase = .idle
            preview = DictationPreviewText()
            hud.hide()
            EventLog.write("dictation", fields)
        }

        let recognitionStarted = Date()
        let raw: String
        fields["asr"] = settings.speechBackend.rawValue
        do {
            var server: SpeechServerClient?
            if settings.speechBackend == .server {
                let (client, viaGateway) = try await SpeechRecognition.serverClient()
                server = client
                fields["asr_route"] = viaGateway ? "gateway" : "direct"
            }
            raw = try await SpeechRecognition.transcribe(samples, language: language, vocabulary: settings.vocabulary, server: server)
        } catch {
            fields["outcome"] = "asr_error"
            fields["asr_error"] = SpeechRecognition.errorKind(error)
            // Zerwane połączenie: następne dyktowanie sprawdzi drogę (dom / brama) od nowa.
            if case SpeechServerError.connection = error { Route.shared.forget() }
            Toast.shared.show(tr("Nie udało się rozpoznać mowy: {error}", ["error": SpeechRecognition.message(for: error)]), duration: 5)
            return
        }
        fields["asr_ms"] = Int(Date().timeIntervalSince(recognitionStarted) * 1000)
        fields["raw_chars"] = raw.count
        guard !raw.isBlank else {
            fields["outcome"] = "empty"
            Toast.shared.show(tr("Nic nie rozpoznałem."))
            return
        }

        // Bez Bielika (albo gdy nie zdąży) wklejamy transkrypcję po lokalnych porządkach.
        var text = DictationTidy.apply(raw)
        var note: String?
        let mode = settings.cleanup
        if mode == .off {
            fields["cleanup"] = "off"
        } else if mode == .automatic, !Prompts.dictationNeedsCleanup(raw) {
            fields["cleanup"] = "not_needed"
        } else if Self.estimatedCleanupSeconds(raw) > Self.cleanupTimeout {
            fields["cleanup"] = "too_long"
            note = tr("Długie dyktowanie — wkleiłem bez porządkowania ({model} może nie zdążyć w {seconds} s).",
                      ["model": Settings.shared.modelDisplayName, "seconds": Int(Self.cleanupTimeout)])
        } else {
            phase = .cleaning
            let cleanupStarted = Date()
            switch await cleanUp(raw) {
            case .success(let cleaned) where Prompts.dictationCleanupLooksSafe(cleaned, transcript: raw):
                text = cleaned
                fields["cleanup"] = "ok"
            case .success:
                // Model odpowiedział na pytanie albo dopisał treść — wklejamy to, co zostało powiedziane.
                fields["cleanup"] = "rejected"
            case .failure(let error):
                fields["cleanup"] = error is CleanupTimeout ? "timeout" : "error"
                note = error is CleanupTimeout ? tr("Model odpowiadał za długo — wkleiłem tekst bez porządkowania.")
                                               : tr("Model niedostępny — wkleiłem tekst bez porządkowania.")
            }
            fields["cleanup_ms"] = Int(Date().timeIntervalSince(cleanupStarted) * 1000)
        }
        fields["chars"] = text.count
        guard !text.isBlank else {
            fields["outcome"] = "empty"
            Toast.shared.show(tr("Nic nie rozpoznałem."))
            return
        }

        hud.hide()
        let outcome = await TextBridge.insert(text, into: app, imeClient: imeClient)
        fields["outcome"] = "\(outcome)"
        switch outcome {
        case .inserted: if let note { Toast.shared.show(note) }
        case .appChanged: Toast.shared.show(tr("Aktywna aplikacja się zmieniła — podyktowany tekst jest w schowku (⌘V)."))
        case .noTextField: Toast.shared.show(tr("Nie widzę pola tekstowego — podyktowany tekst jest w schowku (⌘V)."))
        case .secureField: Toast.shared.show(tr("To pole hasła — podyktowany tekst jest w schowku (⌘V)."))
        case .busy: Toast.shared.show(tr("Trwa inna operacja — podyktowany tekst jest w schowku (⌘V)."))
        case .fieldChanged: Toast.shared.show(tr("Kursor jest w innym polu niż na początku dyktowania — tekst jest w schowku (⌘V)."))
        case .uncertain: Toast.shared.show(tr("Nie mam pewności, czy tekst się wstawił — sprawdź pole; tekst jest też w schowku (⌘V)."))
        case .badResult: Toast.shared.show(tr("Nic nie rozpoznałem."))
        }
    }

    private func cleanUp(_ raw: String) async -> Result<String, Error> {
        let vocabulary = settings.vocabulary
        return await withTaskGroup(of: Result<String, Error>?.self) { group in
            group.addTask {
                do { return .success(try await Backend().cleanDictation(raw, vocabulary: vocabulary)) } catch { return .failure(error) }
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(Self.cleanupTimeout * 1_000_000_000))
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first ?? .failure(CleanupTimeout())
        }
    }
}
