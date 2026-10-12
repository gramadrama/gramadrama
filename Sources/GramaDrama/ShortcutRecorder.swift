import AppKit
import Carbon
import SwiftUI
import GramaCore
import GramaBackend

/// Start → Skróty → Zmień skróty: nowy skrót korekty albo tłumaczenia nagrany z klawiatury. Na czas nagrywania
/// własne skróty są wyrejestrowane (`AppState.recordingShortcut`), więc naciśnięcie obecnego skrótu też trafia tutaj.
/// Skrót musi spełniać zasady `GlobalShortcut`, różnić się od drugiego i dać się zarejestrować (próbna rejestracja).
@MainActor
final class ShortcutRecorder: ObservableObject {
    enum Field { case correction, translation }

    @Published private(set) var recording: Field?
    /// Wynik ostatniej próby: zapisany skrót albo powód odmowy.
    @Published private(set) var message: (kind: StatusLine.Kind, text: String)?
    private var monitor: Any?
    private var resignObserver: NSObjectProtocol?

    func start(_ field: Field) {
        stop()
        message = nil
        recording = field
        AppState.shared.recordingShortcut = true
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handle(event)
            return nil  // nagrywany skrót nie trafia do okna (⌘W nie zamknie go w trakcie)
        }
        // Inna aplikacja na wierzchu: klawisze nie trafią już do okna — koniec nagrywania, skróty wracają.
        resignObserver = NotificationCenter.default.addObserver(forName: NSApplication.didResignActiveNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.stop() }
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        if let resignObserver { NotificationCenter.default.removeObserver(resignObserver) }
        monitor = nil
        resignObserver = nil
        guard recording != nil else { return }
        recording = nil
        AppState.shared.recordingShortcut = false
    }

    func reset(_ field: Field) {
        stop()
        save(field == .correction ? .defaultCorrection : .defaultTranslation, for: field)
    }

    private func handle(_ event: NSEvent) {
        guard let field = recording else { return }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if Int(event.keyCode) == kVK_Escape, flags.isDisjoint(with: [.command, .option, .control]) {
            stop()
            return
        }
        let modifiers = [(NSEvent.ModifierFlags.control, GlobalShortcut.Modifiers.control), (.option, .option),
                         (.shift, .shift), (.command, .command)]
            .reduce(into: GlobalShortcut.Modifiers()) { if flags.contains($1.0) { $0.insert($1.1) } }
        // Znak klawisza w bieżącym układzie, bez modyfikatorów (⇧ daje wielką literę — bez znaczenia).
        let letter = event.charactersIgnoringModifiers ?? ""
        if let problem = GlobalShortcut.problem(modifiers: modifiers, letter: letter) {
            message = (.error, problem)  // nagrywanie trwa: można od razu spróbować innego (⎋ kończy)
            return
        }
        // Odmowa (taki sam jak drugi, zajęty) — nagrywanie trwa dalej, jak po złej kombinacji.
        if save(GlobalShortcut(keyCode: Int(event.keyCode), modifiers: modifiers, letter: letter), for: field) { stop() }
    }

    /// Zapisuje skrót albo podaje powód odmowy w `message`; true — zapisany.
    @discardableResult
    private func save(_ shortcut: GlobalShortcut, for field: Field) -> Bool {
        let settings = Settings.shared
        let other = field == .correction ? settings.translationShortcut : settings.correctionShortcut
        guard !shortcut.sameKeys(as: other) else {
            message = (.error, GlobalShortcut.duplicate)
            return false
        }
        let current = field == .correction ? settings.correctionShortcut : settings.translationShortcut
        // Próbna rejestracja: w trakcie nagrywania własne skróty są wyrejestrowane, a przy „Przywróć” nowy skrót nie jest
        // żadnym z nich (drugi odpadł wyżej) — błąd znaczy więc, że skrót zajęła inna aplikacja. Carbon zgłasza tylko
        // część takich konfliktów: druga kopia GramaDramy zarejestrowała ⌥⌘G bez błędu, gdy pierwsza już go miała.
        if !shortcut.sameKeys(as: current) {
            let status = HotKey.availability(of: shortcut)
            guard status == noErr else {
                message = (.error, status == OSStatus(eventHotKeyExistsErr)
                    ? tr("{shortcut} ma już inna aplikacja — wybierz inny.", ["shortcut": shortcut.label])
                    : tr("Nie udało się ustawić {shortcut} (błąd {status}).", ["shortcut": shortcut.label, "status": status]))
                return false
            }
        }
        if field == .correction {
            settings.setShortcuts(correction: shortcut, translation: settings.translationShortcut)
        } else {
            settings.setShortcuts(correction: settings.correctionShortcut, translation: shortcut)
        }
        EventLog.write("shortcut_changed", ["field": field == .correction ? "fix" : "translate", "default": shortcut.sameKeys(
            as: field == .correction ? .defaultCorrection : .defaultTranslation)])
        message = (.ok, tr("Zapisano: {shortcut}.", ["shortcut": shortcut.label]))
        return true
    }
}

/// Zwinięta część „Zmień skróty” w Start → Skróty.
struct ShortcutEditor: View {
    @ObservedObject private var settings = Settings.shared
    @StateObject private var recorder = ShortcutRecorder()

    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 0) {
                row(.correction, title: tr("Popraw tekst"), current: settings.correctionShortcut, fallback: .defaultCorrection)
                row(.translation, title: tr("Przetłumacz"), current: settings.translationShortcut, fallback: .defaultTranslation)
                if let message = recorder.message {
                    StatusLine(kind: message.kind, text: message.text).padding(.top, 4)
                }
            }
            .padding(.top, 4)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(tr("Zmień skróty"))
                Text(tr("Co najmniej dwa z ⌃ ⌥ ⌘ (⇧ dowolnie) i litera A–Z.")).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .onDisappear(perform: recorder.stop)
    }

    private func row(_ field: ShortcutRecorder.Field, title: String, current: GlobalShortcut, fallback: GlobalShortcut) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.callout)
            Spacer()
            if recorder.recording == field {
                Text(tr("Naciśnij nowy skrót…")).font(.callout).foregroundStyle(.secondary)
                Button(tr("Anuluj"), action: recorder.stop).help(tr("Albo ⎋"))
            } else {
                KeyCaps(keys: current.keys)
                Button(tr("Zmień…")) { recorder.start(field) }
                Button { recorder.reset(field) } label: { Image(systemName: "arrow.uturn.backward") }
                    .buttonStyle(.borderless)
                    .help(tr("Przywróć {shortcut}", ["shortcut": fallback.label]))
                    .disabled(current.sameKeys(as: fallback))
            }
        }
        .padding(.vertical, 5)
    }
}
