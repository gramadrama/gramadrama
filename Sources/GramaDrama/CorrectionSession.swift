import AppKit
import GramaCore
import GramaBackend

@MainActor
final class CorrectionSession: ObservableObject {
    enum Phase: Equatable {
        case checking, streaming, done, failed(String)
    }

    @Published private(set) var phase: Phase = .checking
    @Published var mode: Mode { didSet { if mode != oldValue { run() } } }
    /// Kierunek tłumaczenia — automatyczny z języka tekstu, przełączany w panelu.
    @Published var translationTarget: TranslationTarget {
        didSet { if translationTarget != oldValue, mode == .translate { run() } }
    }
    @Published private(set) var corrected = ""
    @Published private(set) var segments: [DiffSegment] = []
    @Published private(set) var changes: [Change] = []
    @Published private(set) var ruleHits = 0
    @Published private(set) var language = ""
    @Published private(set) var elapsed: TimeInterval = 0
    /// Uwaga do wyniku, np. w trybie „tylko model”: które zdania model zostawił bez sprawdzenia.
    @Published private(set) var note: String?
    /// Popraw w eksperymentalnym trybie „tylko model” (`Settings.liveModelOnly`): te same fragmenty co podczas pisania.
    var usesModelOnly: Bool { mode == .fix && Settings.shared.modelOnlyActive }
    /// ↩ wciśnięty w trakcie pracy — zastosujemy wynik, gdy będzie gotowy (PANEL.md: „Zastosuję po zakończeniu”).
    @Published var applyPending = false
    var onReadyToApply: ((String) -> Void)?
    /// Edycja wyniku w jego polu (klik, pisanie albo ⌘E); `draft` trafia do `corrected` przy ⌘E, ⎋ albo Zastąp.
    @Published private(set) var editing = false
    @Published var draft = ""
    /// Lista zmian pod wynikiem, na żądanie (⌘I albo „Pokaż N poprawek”); każdy nowy wynik zaczyna z zamkniętą.
    @Published private(set) var changesOpen = false
    /// Podpowiedzi LanguageTool — uzasadnienia zmian liczone od nowa po edycji i cofnięciu.
    private var hints: [LTMatch] = []

    let captured: CapturedText
    var original: String { captured.text }
    var hasChanges: Bool { corrected != original && !corrected.isBlank }

    private let backend = Backend()
    private var task: Task<Void, Never>?
    private var ltTask: Task<LTResult?, Never>?
    /// Każde run() dostaje nowy numer; wyniki starszych przebiegów są ignorowane
    /// (np. po szybkim przełączeniu trybu stary strumień nie nadpisze nowego).
    private var generation = 0

    init(captured: CapturedText, mode: Mode) {
        self.captured = captured
        self.mode = mode
        self.translationTarget = .automatic(for: captured.text, in: languages)
    }

    /// Języki tekstu z chwili otwarcia panelu: kierunek tłumaczenia i jego etykieta.
    let languages = Settings.shared.textLanguages
    /// „PL → EN”.
    var directionLabel: String { translationTarget.label(in: languages) }
    func reverseDirection() { translationTarget = translationTarget.opposite(in: languages) }

    func run() {
        cancel()
        generation += 1
        let current = generation
        phase = .checking
        corrected = ""
        segments = []
        changes = []
        applyPending = false
        ruleHits = 0
        language = ""
        note = nil
        editing = false
        draft = ""
        changesOpen = false
        hints = []
        guard mode != .translate || languages.translates else {
            phase = .failed(tr("Tłumaczenie wymaga drugiego języka. Dodaj go w aplikacji GramaDrama (Pisanie → Języki tekstu)."))
            return
        }

        let text = original
        let mode = mode
        let target = translationTarget
        let started = Date()
        let backend = backend
        let modelOnly = usesModelOnly
        // LanguageTool leci równolegle, tylko dla uzasadnień zmian na liście (zwykle z pamięci po sprawdzaniu
        // na żywo). Modelowi go nie podajemy — w benchmarku wskazówki pogarszały wyniki. Przy tłumaczeniu
        // reguły nic nie wnoszą — nie pytamy. Tryb „tylko model” z założenia nie korzysta z LanguageTool.
        let lt: Task<LTResult?, Never>? = mode == .translate || modelOnly ? nil : Task { try? await backend.languageTool(text) }
        ltTask = lt
        let ltValue: () async -> LTResult? = { await lt?.value ?? nil }

        task = Task { [weak self] in
            guard let self, self.isCurrent(current) else { return }

            do {
                self.phase = .streaming
                let onPartial: @MainActor (String) -> Void = { [weak self] partial in
                    guard let self, self.isCurrent(current) else { return }
                    self.corrected = partial
                }
                let result: String
                if modelOnly {
                    let outcome = try await backend.modelOnlyCorrection(text, onPartial: onPartial)
                    result = outcome.text
                    if outcome.rejected > 0 {
                        self.note = outcome.rejected == 1
                            ? tr("Jedno zdanie zostało bez zmian — model zwrócił niepewną odpowiedź.")
                            : tr("Zdania bez zmian (niepewna odpowiedź modelu): {count}.", ["count": outcome.rejected])
                    }
                } else {
                    result = try await backend.correct(text, mode: mode, target: target, onPartial: onPartial)
                }
                guard self.isCurrent(current) else { return }
                // Wynik od razu — „Zastosuj" nie czeka na LanguageTool.
                self.finish(result, started: started)

                let ltResult = await ltValue()
                guard self.isCurrent(current), let ltResult else { return }
                self.ruleHits = ltResult.matches.count
                self.language = ltResult.language
                self.hints = ltResult.matches
                // Po edycji albo cofnięciu zmiany lista liczy się już z nowego wyniku (`update`).
                self.changes = Diff.changes(self.segments, original: text, hints: ltResult.matches)
            } catch {
                guard self.isCurrent(current), !(error is CancellationError) else { return }
                // Błąd modelu pozostawia oryginalny tekst; użytkownik może ponowić korektę.
                let unreachable = (error as? URLError).map { Self.connectionErrors.contains($0.code) } ?? false
                EventLog.write("session_failed", ["mode": mode.rawValue, "unreachable": unreachable,
                                                  "app": self.captured.app?.bundleIdentifier ?? ""])
                self.phase = .failed(!unreachable ? error.localizedDescription
                    : Settings.shared.modelLocation == .local ? tr("Ollama na tym Macu nie odpowiada — uruchom ją (okno aplikacji → Usługi).")
                    : tr("Serwer nie odpowiada — sprawdź połączenie."))
            }
        }
    }

    func cancel() {
        task?.cancel()
        ltTask?.cancel()
        task = nil
        ltTask = nil
    }

    private static let connectionErrors: Set<URLError.Code> = [
        .cannotConnectToHost, .cannotFindHost, .timedOut, .networkConnectionLost, .notConnectedToInternet,
        .dnsLookupFailed, .cannotLoadFromNetwork,
    ]

    private func isCurrent(_ run: Int) -> Bool {
        run == generation && !Task.isCancelled
    }

    /// Podgląd `--preview-panel`: gotowy wynik bez serwera i bez wpisu w dzienniku.
    func showForScreenshot(_ result: String, elapsed: TimeInterval, changesOpen: Bool = false) {
        cancel()
        generation += 1
        corrected = result
        segments = mode == .translate ? [] : Diff.segments(from: original, to: result)
        changes = mode == .translate ? [] : Diff.changes(segments, original: original)
        self.elapsed = elapsed
        self.changesOpen = changesOpen
        phase = .done
    }

    // MARK: Edycja wyniku (PANEL.md, „Edycja”)

    /// Lista zmian jest tylko przy gotowej poprawce (nie w Tłumacz i nie w trakcie edycji).
    var canShowChanges: Bool { phase == .done && !editing && mode != .translate && !changes.isEmpty }

    /// ⌘I albo „Pokaż N poprawek ▾” / „Ukryj zmiany ▴”.
    func toggleChanges() {
        guard canShowChanges else { return }
        changesOpen.toggle()
        EventLog.write("panel_changes", ["open": changesOpen, "mode": mode.rawValue])
    }

    /// Co zaznaczyć szałwią w polu wyniku: zmiany poprawki, a w trakcie edycji to, co tekst zmienia względem
    /// oryginału. W Tłumacz nic — tłumaczenie nie jest poprawką oryginału.
    var marks: [NSRange] {
        guard mode != .translate else { return [] }
        return editing ? Diff.marks(from: original, to: draft) : Diff.marks(segments)
    }

    /// Ta jedna zmiana wraca do oryginału, pozostałe poprawki zostają. `learn`: słowo z oryginału trafia do słownika
    /// macOS (ten sam, którego używa sprawdzanie podczas pisania i karta podkreślenia).
    func revert(_ change: Change, learn: Bool) {
        guard phase == .done, !editing else { return }
        if learn { NSSpellChecker.shared.learnWord(change.from) }
        update(Diff.text(segments, reverting: [change.segments]))
        EventLog.write("panel_edit", ["action": learn ? "learn" : "revert", "mode": mode.rawValue])
    }

    func startEditing() {
        guard phase == .done, !editing else { return }
        draft = corrected
        editing = true
    }

    /// ⌘E: zaczyna edycję albo ją kończy.
    func toggleEditing() {
        if editing { finishEditing() } else { startEditing() }
    }

    /// Wynik z pola tekstowego zostaje wynikiem; różnice i lista zmian liczą się od nowa.
    func finishEditing() {
        guard editing else { return }
        editing = false
        if draft != corrected {
            update(draft)
            EventLog.write("panel_edit", ["action": "edit", "mode": mode.rawValue])
        }
    }

    private func update(_ text: String) {
        corrected = text
        segments = mode == .translate ? [] : Diff.segments(from: original, to: text)
        changes = mode == .translate ? [] : Diff.changes(segments, original: original, hints: hints)
    }

    private func finish(_ result: String, started: Date) {
        corrected = result
        // Diff między językami nic nie mówi — panel pokazuje wtedy oryginał i tłumaczenie.
        segments = mode == .translate ? [] : Diff.segments(from: original, to: result)
        changes = mode == .translate ? [] : Diff.changes(segments, original: original)
        elapsed = Date().timeIntervalSince(started)
        phase = .done
        // source: model / cache / joined — ile razy panel dostał wynik bez nowego zapytania (etap 4C).
        EventLog.write("session", ["mode": mode.rawValue, "ms": Int(elapsed * 1000), "source": backend.lastSource,
                                   "changes": changes.count, "chars": original.count, "model_only": usesModelOnly,
                                   "app": captured.app?.bundleIdentifier ?? ""])
        if applyPending {
            applyPending = false
            if hasChanges, captured.canApply { onReadyToApply?(corrected) }
        }
    }
}
