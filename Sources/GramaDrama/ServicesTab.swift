import SwiftUI
import GramaCore
import GramaBackend

/// Usługi: wszystko, co liczy się poza samą aplikacją — model językowy, LanguageTool i rozpoznawanie mowy, każde na
/// tym Macu albo na serwerze, oraz opcjonalny dostęp zdalny. Każda usługa ma ten sam układ: gdzie działa, adres
/// serwera (protokół, klucz), stan i „Sprawdź”. Pozostałe zakładki mówią tylko, czego używają.
struct ServicesTab: View {
    @ObservedObject var model: MainWindowModel
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var dictation = DictationSettings.shared
    @ObservedObject private var speech = SpeechModel.shared
    @ObservedObject private var ollama = LocalOllama.shared
    @ObservedObject private var remote = RemoteAccount.shared
    @ObservedObject private var route = Route.shared

    @State private var modelURLDraft = Settings.shared.modelServerURL
    @State private var modelAPIDraft = Settings.shared.modelServerAPI
    @State private var modelKeyDraft = ""
    @State private var modelKeyLoaded = false
    /// Klucz z pęku kluczy przy otwarciu formularza; nil — nie dał się odczytać (wtedy puste pole go nie usuwa).
    @State private var modelKeyStored: String?
    @State private var modelConfigError: String?
    @State private var pullName = ""
    @State private var modelLocalDraft = Settings.shared.modelLocalURL
    @State private var modelLocalError: String?
    @State private var languageToolLocalDraft = Settings.shared.languageToolLocalURL
    @State private var languageToolLocalError: String?
    @State private var promptFiles = PromptFiles.current()
    /// Wynik „Otwórz folder” (zapisane pliki albo błąd zapisu).
    @State private var promptsNote: StatusLine?
    @State private var languageToolURLDraft = Settings.shared.languageToolServerURL
    @State private var languageToolConfigError: String?
    @State private var speechURLDraft = DictationSettings.shared.speechServerURL
    @State private var speechAPIDraft = DictationSettings.shared.speechServerAPI
    @State private var speechModelDraft = DictationSettings.shared.speechServerModel
    @State private var speechKeyDraft = ""
    @State private var speechKeyLoaded = false
    @State private var speechKeyStored: String?
    @State private var speechConfigError: String?
    @State private var remoteAddressDraft = Settings.shared.remoteBaseURL
    @State private var remoteClientDraft = Settings.shared.remoteClientID
    @State private var remoteConfigError: String?
    @State private var remoteExpanded = false

    var body: some View {
        languageModelSection
        languageToolSection
        speechSection
        remoteSection
            .onDisappear(perform: savePendingDrafts)
    }

    private func refreshServices() {
        Task { await model.refreshServices() }
    }

    // MARK: Model językowy

    private var languageModelSection: some View {
        SettingsSection(title: tr("Model językowy"), note: tr("Korekta, tłumaczenie i sprawdzanie modelem."), ring: .languageModel) {
            SettingsCard {
                LocationRow(selection: $settings.modelLocation,
                            subtitle: settings.modelLocation == .local ? tr("Ollama na tym Macu; tekst nie opuszcza komputera.")
                                                                       : tr("Ollama albo serwer zgodny z OpenAI."))
                Divider()
                if settings.modelLocation == .local {
                    LocalAddressRow(title: tr("Adres Ollamy"), placeholder: ModelServerEndpoint.local.base.absoluteString,
                                    address: $modelLocalDraft, error: modelLocalError, save: commitModelLocal)
                    Divider()
                    ollamaRow
                } else {
                    modelServerForm
                    Divider()
                    ServiceStatusRow(title: tr("Serwer"), status: modelServerStatus, checking: model.checkingServices, check: refreshServices)
                }
                Divider()
                modelRow
                if settings.modelLocation == .local, case .running = ollama.state {
                    Divider()
                    pullRow
                }
                Divider()
                SettingsRow(mark: .icon("stopwatch"), title: tr("Próba korekty"),
                            subtitle: model.testResult ?? tr("Wysyła przykładowe zdanie i mierzy czas odpowiedzi.")) {
                    Button(model.testing ? tr("Sprawdzam…") : tr("Sprawdź")) { Task { await model.runTest() } }.disabled(model.testing)
                }
                Divider()
                advancedModel
            }
        }
        .onChange(of: settings.modelLocation) {
            // Lista modeli i stan drugiego miejsca nie mogą zostać na ekranie do końca sprawdzania.
            model.diagnostics = nil
            refreshServices()
        }
    }

    /// Ollama na tym Macu: działa (stan jak serwera), jest wyłączona („Uruchom”) albo jej nie ma („Pobierz Ollamę…”).
    private var ollamaRow: some View {
        let status: ServiceStatus? = switch ollama.state {
        case .unknown: nil
        case .running: model.diagnostics?.model
        case .stopped(nil): .error(tr("Zainstalowana, ale nie działa — uruchom usługę Ollama."))
        case .stopped: .error(tr("Zainstalowana, ale nie działa."))
        case .missing: .error(tr("Nie jest zainstalowana — to bezpłatna aplikacja do modeli językowych na Macu."))
        }
        return ServiceStatusRow(title: "Ollama", status: status, checking: model.checkingServices || ollama.launching,
                                check: refreshServices) {
            switch ollama.state {
            case .stopped(.some):
                Button(ollama.launching ? tr("Uruchamiam…") : tr("Uruchom")) { ollama.launch(then: refreshServices) }
                    .disabled(ollama.launching)
            case .missing where model.firstRun:
                // Nowa instalacja: to jest krok, który zostaje do pierwszej korekty (Pierwsze kroki na Starcie).
                Button(tr("Pobierz Ollamę…")) { NSWorkspace.shared.open(LocalOllama.downloadPage) }.buttonStyle(.borderedProminent)
            case .missing:
                Button(tr("Pobierz Ollamę…")) { NSWorkspace.shared.open(LocalOllama.downloadPage) }
            default:
                EmptyView()
            }
        }
    }

    private func commitModelLocal() {
        if let problem = ServerAddress.localProblem(modelLocalDraft, example: ModelServerEndpoint.local.base.absoluteString) {
            modelLocalError = problem
            return
        }
        modelLocalError = nil
        settings.modelLocalURL = modelLocalDraft.trimmed
        refreshServices()
    }

    // MARK: Zaawansowane (myślenie modelu, prompty)

    /// Zwinięte: ustawienia, których zwykle się nie zmienia — jak „Zaawansowane” na Linuksie.
    private var advancedModel: some View {
        DisclosureGroup {
            VStack(spacing: 0) {
                if usesOllama {
                    SettingsRow(mark: .icon("brain"), title: tr("Myślenie modelu"),
                                subtitle: tr("Bez myślenia korekta jest szybsza; tak ją mierzono.")) {
                        Picker("", selection: $settings.modelThinking) {
                            Text(tr("wyłączone (zalecane)")).tag(false)
                            Text(tr("domyślne ustawienie modelu")).tag(true)
                        }
                        .labelsHidden().fixedSize()
                    }
                    Divider()
                }
                promptsRow
            }
            .padding(.top, 6)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(tr("Zaawansowane"))
                Text(usesOllama ? tr("Myślenie modelu i własne prompty.") : tr("Własne prompty.")).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            // Po powrocie z edytora: pliki mogły się zmienić.
            promptFiles = PromptFiles.current()
        }
    }

    /// Prompty (linux/docs/prompts.md): domyślne, własne pliki albo pominięty plik i powód.
    private var promptsRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            SettingsRow(mark: promptFiles.problems.isEmpty ? .icon("doc.text") : .status(false), title: tr("Prompty"),
                        subtitle: promptsStatus) {
                Button(tr("Otwórz folder"), action: openPrompts)
            }
            if let promptsNote { promptsNote.padding(.leading, 44).padding(.bottom, 8) }
        }
    }

    private var promptsStatus: String {
        if let first = promptFiles.problems.first {
            let more = promptFiles.problems.count > 1 ? " " + tr("(i {count} innych)", ["count": promptFiles.problems.count - 1]) : ""
            return tr("Pominięty plik {file}: {reason} — używam domyślnego{more}.", ["file": first.file, "reason": first.reason, "more": more])
        }
        let own = promptFiles.overridden
        guard !own.isEmpty else { return tr("Domyślne. Własne wersje to pliki w folderze promptów.") }
        let shown = own.prefix(3).joined(separator: ", ") + (own.count > 3 ? " " + tr("i {count} innych", ["count": own.count - 3]) : "")
        return tr("Własne pliki ({count}): {files}", ["count": own.count, "files": shown])
    }

    /// Najpierw zapisuje brakujące domyślne pliki do edycji (żadnego na istniejący), potem otwiera folder.
    private func openPrompts() {
        do {
            let written = try PromptFiles.export()
            promptsNote = written.isEmpty ? nil : StatusLine(kind: .ok, text:
                tr("Zapisano {count} plików z domyślnymi promptami. Zmień ten, który chcesz; usunięcie pliku przywraca domyślny.",
                   ["count": written.count]))
        } catch {
            promptsNote = StatusLine(kind: .error, text: tr("Nie udało się zapisać promptów: {error}", ["error": error.localizedDescription]))
            return
        }
        promptFiles = PromptFiles.current()
        NSWorkspace.shared.open(PromptFiles.directory)
    }

    /// Bez adresu (i bez logowania do bramy) — co wpisać; inaczej wynik sprawdzenia.
    private var modelServerStatus: ServiceStatus? {
        if settings.modelServer == nil, !remote.isSignedIn {
            return .error(ModelServerEndpoint.problem(settings.modelServerURL) ?? tr("Nieprawidłowy adres."))
        }
        return model.diagnostics?.model
    }

    private var modelServerForm: some View {
        ServerForm(placeholder: "http://192.0.2.10:11434", address: $modelURLDraft, error: modelConfigError, save: commitModelServer) {
            Picker(tr("Protokół"), selection: $modelAPIDraft) {
                ForEach(ModelServerAPI.allCases) { Text($0.label).tag($0) }
            }
            .fixedSize()
            SecureField(tr("Klucz API (opcjonalny, w pęku kluczy)"), text: $modelKeyDraft).onSubmit(commitModelServer)
            if modelKeyLoaded, modelKeyStored == nil { unreadableKeyNote }
        }
        .onAppear {
            // Klucz z pęku kluczy dopiero przy otwarciu formularza (SecureField go nie pokazuje).
            guard !modelKeyLoaded else { return }
            modelKeyStored = ServerKey.model.read()
            modelKeyDraft = modelKeyStored ?? ""
            modelKeyLoaded = true
        }
    }

    private var unreadableKeyNote: some View {
        Text(tr("Nie udało się odczytać zapisanego klucza — zostanie, dopóki nie wpiszesz nowego."))
            .font(.caption).foregroundStyle(.secondary)
    }

    /// Zapisuje klucz tylko po zmianie pola; nieodczytany klucz zastępuje dopiero nowo wpisany. Zwraca zapisany stan.
    private func saveKey(_ draft: String, stored: String?, to key: ServerKey) -> String? {
        let draft = draft.trimmed
        if let stored { guard draft != stored else { return stored } } else { guard !draft.isEmpty else { return nil } }
        key.save(draft)
        return draft
    }

    private func commitModelServer() {
        if let problem = ModelServerEndpoint.problem(modelURLDraft) {
            modelConfigError = problem
            return
        }
        modelConfigError = nil
        settings.modelServerURL = modelURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.modelServerAPI = modelAPIDraft
        if modelKeyLoaded { modelKeyStored = saveKey(modelKeyDraft, stored: modelKeyStored, to: .model) }
        Route.shared.forget()
        refreshServices()
    }

    private var modelRow: some View {
        SettingsRow(mark: .status(model.diagnostics?.models.map { $0.contains(settings.model) }), title: "Model",
                    subtitle: modelSubtitle) {
            if !model.models.isEmpty {
                Picker("", selection: $settings.model) {
                    ForEach(modelChoices, id: \.self) { Text(label(for: $0)).tag($0) }
                }
                .labelsHidden().frame(width: 190)
            } else if settings.modelLocation == .server {
                // Serwer bez listy modeli (albo jeszcze bez odpowiedzi) — nazwę wpisujesz.
                TextField("Model", text: $settings.model, prompt: Text(tr("nazwa modelu"))).frame(width: 190)
            }
        }
    }

    private var usesOllama: Bool { settings.modelLocation == .local || settings.modelServerAPI == .ollama }

    private var modelSubtitle: String? {
        let here = settings.modelLocation == .local
        guard let diagnostics = model.diagnostics, diagnostics.modelReachable else {
            return here && ollama.state != .unknown ? tr("Wybierzesz albo pobierzesz go, gdy Ollama będzie działać.") : nil
        }
        if model.models.isEmpty { return here ? tr("Brak modeli — pobierz model poniżej.") : nil }
        if !model.models.contains(settings.model) {
            return here ? tr("Tego modelu nie ma na tym Macu — pobierz go poniżej albo wybierz inny.")
                        : tr("Tego modelu nie ma na serwerze — wybierz inny.")
        }
        guard usesOllama else { return nil }
        return diagnostics.loadedModels.contains(settings.model) ? tr("Wczytany do pamięci.") : tr("Wczyta się przy pierwszej korekcie.")
    }

    private var modelChoices: [String] {
        model.models.contains(settings.model) || settings.model.isEmpty ? model.models : model.models + [settings.model]
    }

    private func label(for name: String) -> String {
        var short = name.split(separator: "/").last.map(String.init) ?? name
        for suffix in ["-GGUF", "-Instruct"] { if let range = short.range(of: suffix) { short = String(short[..<range.lowerBound]) } }
        guard !model.models.contains(name) else { return short }
        return settings.modelLocation == .local ? tr("{model} (brak na tym Macu)", ["model": short])
                                                : tr("{model} (brak na serwerze)", ["model": short])
    }

    /// Pobieranie modelu do Ollamy na tym Macu — z paskiem postępu, jak model mowy.
    private var pullRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Image(systemName: "arrow.down.circle").foregroundStyle(.secondary).frame(width: 18)
                Text(tr("Pobierz model"))
                Spacer(minLength: 8)
                if ollama.download?.active == true {
                    Button(tr("Anuluj")) { ollama.cancelPull() }
                } else {
                    TextField("", text: $pullName, prompt: Text(LocalOllama.suggestedModel))
                        .frame(width: 160).onSubmit(pull)
                    Button(tr("Pobierz"), action: pull).disabled(model.models.contains(pullTarget))
                }
            }
            Group {
                if let download = ollama.download {
                    if let error = download.error {
                        StatusLine(kind: .error, text: error)
                    } else if download.progress.finished {
                        StatusLine(kind: .ok, text: tr("Pobrano {model}.", ["model": download.model]))
                    } else {
                        ProgressView(value: download.progress.fraction ?? 0)
                        Text(pullText(download)).font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Text(tr("Nazwa z biblioteki Ollamy; zalecany {model} (ok. 6,6 GB).", ["model": LocalOllama.suggestedModel]))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.leading, 30)
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
    }

    private func pullText(_ download: LocalOllama.Download) -> String {
        let progress = download.progress
        guard progress.total > 0 else { return tr("Przygotowuję {model}…", ["model": download.model]) }
        let style = ByteCountFormatStyle(style: .file, locale: WindowText.locale)
        return tr("Pobieram {model}: {done} z {total}",
                  ["model": download.model, "done": progress.completed.formatted(style), "total": progress.total.formatted(style)])
    }

    /// Puste pole — podpowiadany model.
    private var pullTarget: String { pullName.trimmed.isEmpty ? LocalOllama.suggestedModel : pullName.trimmed }

    private func pull() {
        guard !model.models.contains(pullTarget) else { return }
        ollama.pull(pullTarget) { name in
            Task {
                await model.refreshServices()
                // Pobrany model od razu w użyciu, jeśli wybranego nie ma na tym Macu.
                if !model.models.contains(settings.model),
                   let pulled = model.models.first(where: { $0 == name || $0 == name + ":latest" }) {
                    settings.model = pulled
                }
            }
        }
    }

    // MARK: LanguageTool

    private var languageToolSection: some View {
        SettingsSection(title: "LanguageTool", note: tr("Gramatyka podczas pisania i uzasadnienia zmian; opcjonalny.")) {
            SettingsCard {
                LocationRow(selection: $settings.languageToolLocation,
                            subtitle: settings.languageToolLocation == .server ? tr("Serwer LanguageTool pod wpisanym adresem.")
                                : settings.languageToolLocalURL.isEmpty ? tr("Serwer LanguageTool na tym Macu (port 8010 albo 8081).")
                                : tr("Serwer LanguageTool na tym Macu pod wpisanym adresem."))
                Divider()
                if settings.languageToolLocation == .local {
                    LocalAddressRow(title: tr("Adres LanguageTool"), placeholder: tr("port 8010 albo 8081"),
                                    address: $languageToolLocalDraft, error: languageToolLocalError, save: commitLanguageToolLocal)
                    Divider()
                }
                if settings.languageToolLocation == .server {
                    ServerForm(placeholder: "http://192.0.2.10:8010", address: $languageToolURLDraft,
                               error: languageToolConfigError, save: commitLanguageTool)
                    Divider()
                }
                ServiceStatusRow(title: "LanguageTool", status: languageToolStatus, checking: model.checkingServices,
                                 check: refreshServices)
            }
        }
        .onChange(of: settings.languageToolLocation) { refreshServices() }
    }

    private var languageToolStatus: ServiceStatus? {
        if settings.languageToolLocation == .server, settings.languageToolServer == nil, !remote.isSignedIn {
            return .error(LanguageToolEndpoint.problem(settings.languageToolServerURL) ?? tr("Nieprawidłowy adres."))
        }
        return model.diagnostics?.languageTool
    }

    private func commitLanguageToolLocal() {
        if let problem = ServerAddress.localProblem(languageToolLocalDraft, example: "http://127.0.0.1:8010") {
            languageToolLocalError = problem
            return
        }
        languageToolLocalError = nil
        settings.languageToolLocalURL = languageToolLocalDraft.trimmed
        refreshServices()
    }

    private func commitLanguageTool() {
        if let problem = LanguageToolEndpoint.problem(languageToolURLDraft) {
            languageToolConfigError = problem
            return
        }
        languageToolConfigError = nil
        settings.languageToolServerURL = languageToolURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        Route.shared.forget()
        refreshServices()
    }

    // MARK: Rozpoznawanie mowy

    private var speechSection: some View {
        SettingsSection(title: tr("Rozpoznawanie mowy"), note: tr("Do dyktowania."), ring: .speechRecognition) {
            SettingsCard {
                LocationRow(selection: $dictation.speechBackend,
                            subtitle: dictation.speechBackend == .local ? tr("Whisper na tym Macu; nagranie nie opuszcza komputera.")
                                                                        : tr("Serwer zgodny z OpenAI albo whisper.cpp."))
                Divider()
                if dictation.speechBackend == .local {
                    whisperRow
                } else {
                    speechServerForm
                    Divider()
                    ServiceStatusRow(title: tr("Serwer mowy"), status: speechServerStatus, checking: model.speechChecking) {
                        Task { await model.checkSpeechServer() }
                    }
                }
            }
        }
    }

    /// Whisper w aplikacji: model (ok. 1,6 GB) pobiera się tylko tym przyciskiem; pobrany wczytuje się sam, gdy dyktowanie
    /// jest włączone, albo od razu przez „Wczytaj teraz”.
    private var whisperRow: some View {
        VStack(alignment: .leading, spacing: 0) {
            SettingsRow(mark: .status(localSpeechOK), title: "Whisper large-v3-turbo", subtitle: speech.state.label) {
                switch speech.state {
                case .notDownloaded: Button(tr("Pobierz (1,6 GB)")) { Task { try? await speech.prepare() } }
                case .notLoaded: Button(tr("Wczytaj teraz")) { Task { try? await speech.prepare() } }
                case .failed: Button(tr("Spróbuj ponownie")) { Task { try? await speech.prepare() } }
                default: EmptyView()
                }
            }
            if case .downloading(let fraction) = speech.state {
                ProgressView(value: fraction).padding(.leading, 44).padding(.trailing, 14).padding(.bottom, 10)
            }
        }
    }

    private var localSpeechOK: Bool? {
        switch speech.state {
        case .ready: true
        case .failed: false
        default: nil
        }
    }

    private var speechServerStatus: ServiceStatus? {
        if dictation.speechEndpoint == nil {
            return .error(SpeechServerEndpoint.problem(dictation.speechServerURL) ?? tr("Nieprawidłowy adres."))
        }
        return model.speechStatus
    }

    /// Adres, protokół, model (tylko OpenAI) i klucz; „Zapisz i sprawdź” sprawdza adres i pyta serwer o stan.
    private var speechServerForm: some View {
        ServerForm(placeholder: "http://192.0.2.10:8080", address: $speechURLDraft, error: speechConfigError, save: commitSpeechServer) {
            HStack {
                Picker(tr("Protokół"), selection: $speechAPIDraft) {
                    ForEach(SpeechServerAPI.allCases) { Text($0.label).tag($0) }
                }
                .fixedSize()
                if speechAPIDraft == .openAI {
                    TextField("Model", text: $speechModelDraft, prompt: Text("whisper-1"))
                }
            }
            SecureField(tr("Klucz API (opcjonalny, w pęku kluczy)"), text: $speechKeyDraft).onSubmit(commitSpeechServer)
            if speechKeyLoaded, speechKeyStored == nil { unreadableKeyNote }
            if RemoteConfig.configuration != nil, let server = dictation.speechEndpoint, !ServerAddress.isLoopback(host: server.host) {
                Text(tr("Gdy serwer nie odpowiada (np. poza domem), nagranie idzie przez bramę dostępu zdalnego (ścieżka /{path}).",
                        ["path": SpeechServerEndpoint.gatewayPath]))
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .onAppear {
            guard !speechKeyLoaded else { return }
            speechKeyStored = ServerKey.speech.read()
            speechKeyDraft = speechKeyStored ?? ""
            speechKeyLoaded = true
        }
    }

    private func commitSpeechServer() {
        let address = speechURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if let problem = SpeechServerEndpoint.problem(address) {
            speechConfigError = problem
            return
        }
        speechConfigError = nil
        dictation.speechServerURL = address
        dictation.speechServerAPI = speechAPIDraft
        dictation.speechServerModel = speechModelDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if speechKeyLoaded { speechKeyStored = saveKey(speechKeyDraft, stored: speechKeyStored, to: .speech) }
        Task { await model.checkSpeechServer() }
    }

    /// Wpisane, ale niezapisane poprawne adresy (i klucze) zapisujemy przy wyjściu z zakładki — bez sprawdzania.
    private func savePendingDrafts() {
        if settings.modelLocation == .server, ModelServerEndpoint.problem(modelURLDraft) == nil {
            settings.modelServerURL = modelURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
            settings.modelServerAPI = modelAPIDraft
            if modelKeyLoaded { modelKeyStored = saveKey(modelKeyDraft, stored: modelKeyStored, to: .model) }
        }
        if settings.languageToolLocation == .server, LanguageToolEndpoint.problem(languageToolURLDraft) == nil {
            settings.languageToolServerURL = languageToolURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if settings.modelLocation == .local, ServerAddress.localProblem(modelLocalDraft, example: "") == nil {
            settings.modelLocalURL = modelLocalDraft.trimmed
        }
        if settings.languageToolLocation == .local, ServerAddress.localProblem(languageToolLocalDraft, example: "") == nil {
            settings.languageToolLocalURL = languageToolLocalDraft.trimmed
        }
        if dictation.speechBackend == .server, SpeechServerEndpoint.problem(speechURLDraft) == nil {
            dictation.speechServerURL = speechURLDraft.trimmingCharacters(in: .whitespacesAndNewlines)
            dictation.speechServerAPI = speechAPIDraft
            dictation.speechServerModel = speechModelDraft.trimmingCharacters(in: .whitespacesAndNewlines)
            if speechKeyLoaded { speechKeyStored = saveKey(speechKeyDraft, stored: speechKeyStored, to: .speech) }
        }
    }

    // MARK: Dostęp zdalny

    private var remoteSection: some View {
        SettingsSection(title: tr("Dostęp zdalny"), note: tr("Opcjonalny: serwery z sieci domowej poza domem, przez bramę z logowaniem OIDC (np. Pocket ID)."),
                        ring: .remoteAccess) {
            SettingsCard {
                if RemoteConfig.configuration == nil && !remoteExpanded {
                    SettingsRow(mark: .icon("person.badge.key"), title: tr("Nie skonfigurowano"),
                                subtitle: tr("Adres logowania i identyfikator klienta dostaniesz od administratora serwera.")) {
                        Button(tr("Skonfiguruj…")) { remoteExpanded = true }
                    }
                } else {
                    remoteForm
                    Divider()
                    remoteAccountRow
                    Divider()
                    connectionRow
                }
            }
        }
    }

    private var remoteForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(tr("Adres logowania"), text: $remoteAddressDraft, prompt: Text("https://id.example.com"))
            TextField(tr("Identyfikator klienta"), text: $remoteClientDraft)
            HStack {
                Button(tr("Zapisz")) { commitRemoteConfiguration() }
                if !settings.remoteBaseURL.isEmpty {
                    Button(tr("Wyłącz dostęp zdalny")) {
                        remoteAddressDraft = ""
                        remoteClientDraft = ""
                        commitRemoteConfiguration()
                        remoteExpanded = false
                    }
                }
            }
            if let remoteConfigError { StatusLine(kind: .error, text: remoteConfigError) }
        }
        .padding(14)
    }

    @ViewBuilder private var remoteAccountRow: some View {
        switch remote.state {
        case .signedIn(let user):
            SettingsRow(mark: .status(true), title: tr("Zalogowano jako {user}", ["user": user]), subtitle: tr("Token w pęku kluczy tego Maca.")) {
                Button(tr("Wyloguj")) { remote.signOut() }
            }
        case .waitingForBrowser:
            SettingsRow(mark: .icon("safari"), title: tr("Czekam na przeglądarkę…"),
                        subtitle: tr("Dokończ logowanie — przeglądarka wróci tu sama.")) {
                HStack(spacing: 8) {
                    Button(tr("Otwórz ponownie")) { AppDelegate.signIn() }
                    Button(tr("Anuluj")) { remote.cancelSignIn() }
                }
            }
        case .signedOut:
            SettingsRow(mark: .icon("person.badge.key"), title: tr("Nie zalogowano")) {
                Button(tr("Zaloguj się…")) { AppDelegate.signIn() }.buttonStyle(.borderedProminent)
                    .disabled(RemoteConfig.configuration == nil)
            }
        }
    }

    /// Usługi, które mogą łączyć się przez bramę: serwer (nie ten Mac) w protokole, który brama obsługuje,
    /// albo serwer bez adresu (sama brama). Nazwa do opisu i host, jeśli jest.
    private var gatewayServices: [(service: Route.Service, name: String, host: String?)] {
        var list: [(service: Route.Service, name: String, host: String?)] = []
        if settings.modelLocation == .server,
           settings.modelServer.map({ $0.api == .ollama && !ServerAddress.isLoopback(host: $0.host) }) ?? true {
            list.append((.model, tr("model"), settings.modelServer?.host))
        }
        if settings.languageToolLocation == .server,
           settings.languageToolServer.map({ !ServerAddress.isLoopback(host: $0.host) }) ?? true {
            list.append((.languageTool, "LanguageTool", settings.languageToolServer?.host))
        }
        if dictation.speechBackend == .server, let server = dictation.speechEndpoint, !ServerAddress.isLoopback(host: server.host) {
            list.append((.speech, tr("mowa"), server.host))
        }
        return list
    }

    /// Jak teraz łączą się usługi, które mogą iść przez bramę — każda sprawdza drogę osobno.
    private var connectionRow: some View {
        let services = gatewayServices
        let known = services.compactMap { item in route.kinds[item.service].map { (item, $0) } }
        let kinds = Set(known.map(\.1))
        let mark: RowMark = services.isEmpty ? .icon("minus.circle") : .status(known.isEmpty ? nil : !kinds.contains(.offline))
        let text: String
        if services.isEmpty {
            text = tr("Nie dotyczy — żadna z wybranych usług nie łączy się przez bramę.")
        } else if known.isEmpty {
            text = tr("Sprawdzam…")
        } else if kinds.contains(.offline) {
            text = tr("Serwer nie odpowiada w tej sieci — zaloguj się, żeby łączyć się przez bramę.")
        } else if kinds == [.home] {
            let hosts = known.compactMap(\.0.host).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
            text = hosts.isEmpty ? tr("Teraz bezpośrednio.") : tr("Teraz bezpośrednio: {hosts}.", ["hosts": hosts.joined(separator: ", ")])
        } else if kinds == [.remote] {
            text = tr("Teraz przez bramę: {host}.", ["host": RemoteConfig.configuration?.base.host ?? ""])
        } else {
            func names(_ kind: Route.Kind) -> String { known.filter { $0.1 == kind }.map(\.0.name).joined(separator: ", ") }
            text = tr("Przez bramę: {remote}; bezpośrednio: {home}.", ["remote": names(.remote), "home": names(.home)])
        }
        return SettingsRow(mark: mark, title: tr("Połączenie"), subtitle: text)
    }

    /// Zmiana adresu albo identyfikatora unieważnia logowanie (token należy do tamtego serwera); ten sam zapis nie.
    private func commitRemoteConfiguration() {
        let address = remoteAddressDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let client = remoteClientDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !(address.isEmpty && client.isEmpty), RemoteServiceConfiguration(address: address, clientID: client) == nil {
            remoteConfigError = tr("Podaj adres HTTPS bez ścieżki i identyfikator klienta. HTTP tylko dla localhost.")
            return
        }
        remoteConfigError = nil
        guard address != settings.remoteBaseURL || client != settings.remoteClientID else { return }
        remote.signOut()
        settings.remoteBaseURL = address
        settings.remoteClientID = client
        Route.shared.forget()
    }
}
