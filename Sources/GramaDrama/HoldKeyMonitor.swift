import AppKit
import Carbon.HIToolbox

/// Logika „trzymaj klawisz, żeby dyktować” bez zdarzeń systemu (testowana w `--selftest`).
/// Inny klawisz w trakcie trzymania znaczy, że to skrót (Fn+←, ⌥+a = ą), a nie dyktowanie.
/// Wyjątek: ⇧ (wciśnięty przed albo w trakcie) przełącza to dyktowanie na drugi język.
struct HoldKeyMachine {
    enum State: Equatable { case idle, armed, recording }
    enum Event { case keyDown, keyUp, otherKey, armElapsed, shift }
    enum Action: Equatable { case none, scheduleArm, start, stop, cancel, alternate }

    private(set) var state = State.idle
    /// To dyktowanie w drugim języku (⇧).
    private(set) var alternate = false
    /// Czas trzymania samego klawisza, po którym startuje nagrywanie (0 = od razu).
    let armDelay: TimeInterval

    mutating func handle(_ event: Event) -> Action {
        switch (state, event) {
        case (.idle, .keyDown):
            alternate = false
            if armDelay > 0 {
                state = .armed
                return .scheduleArm
            }
            state = .recording
            return .start
        case (.armed, .armElapsed):
            state = .recording
            return .start
        case (.armed, .keyUp), (.armed, .otherKey):
            state = .idle
            return .none
        case (.recording, .keyUp):
            state = .idle
            return .stop
        case (.recording, .otherKey):
            state = .idle
            return .cancel
        case (.armed, .shift):
            alternate = true
            return .none
        case (.recording, .shift):
            guard !alternate else { return .none }
            alternate = true
            return .alternate
        default:
            return .none
        }
    }

    /// Nagrywanie skończyło się samo (limit czasu, błąd) — kolejne puszczenie klawisza nic nie robi.
    mutating func reset() { state = .idle }
}

/// Nasłuch trzymanego Fn albo prawego ⌥ w całym systemie. Globalny monitor NSEvent działa dzięki
/// uprawnieniu Dostępność (to samo, którego GramaDrama używa do tekstu). Zdarzenia tylko czytamy,
/// więc Fn i ⌥ działają w aplikacjach jak zwykle.
@MainActor
final class HoldKeyMonitor {
    var onStart: (() -> Void)?
    var onStop: (() -> Void)?
    var onCancel: (() -> Void)?
    /// ⇧ w trakcie nagrywania — dyktowanie w drugim języku.
    var onAlternate: (() -> Void)?
    /// Bieżące (albo właśnie rozpoczynane) dyktowanie jest w drugim języku.
    var alternate: Bool { machine.alternate }

    private var monitors: [Any] = []
    private var machine = HoldKeyMachine(armDelay: 0)
    private var key = DictationKey.fn
    private var armTask: Task<Void, Never>?
    /// Ostatni znany stan naszego klawisza — zmiana stanu to wciśnięcie albo puszczenie.
    private var keyIsDown = false

    func start(key: DictationKey) {
        stop()
        self.key = key
        keyIsDown = false
        // Prawy ⌥ w układzie Polski Pro to polskie litery — ⌥+a przychodzi zwykle w < 200 ms.
        machine = HoldKeyMachine(armDelay: key == .rightOption ? 0.3 : 0)
        let mask: NSEvent.EventTypeMask = [.flagsChanged, .keyDown]
        if let global = NSEvent.addGlobalMonitorForEvents(matching: mask, handler: { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
        }) {
            monitors.append(global)
        }
        // Globalny monitor nie widzi zdarzeń własnych okien (ustawienia, panel).
        if let local = NSEvent.addLocalMonitorForEvents(matching: mask, handler: { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
            return event
        }) {
            monitors.append(local)
        }
    }

    func stop() {
        monitors.forEach(NSEvent.removeMonitor)
        monitors = []
        armTask?.cancel()
        machine.reset()
    }

    func reset() {
        armTask?.cancel()
        machine.reset()
    }

    private func handle(_ event: NSEvent) {
        let input: HoldKeyMachine.Event
        // Zmiana stanu naszego klawisza zamiast samego kodu klawisza: 🌐 nie na każdej klawiaturze
        // ma kod kVK_Function, a flaga .function zmienia się zawsze.
        if event.type == .flagsChanged, isPressed(event) != keyIsDown, key == .fn || event.keyCode == UInt16(kVK_RightOption) {
            keyIsDown.toggle()
            perform(machine.handle(keyIsDown ? .keyDown : .keyUp))
            // ⇧ trzymany już przed naszym klawiszem.
            if keyIsDown, event.modifierFlags.contains(.shift) { perform(machine.handle(.shift)) }
            return
        } else if event.type == .flagsChanged, [kVK_Shift, kVK_RightShift].contains(Int(event.keyCode)),
                  event.modifierFlags.contains(.shift) {
            input = .shift
        } else if event.type == .keyDown || isPressed(anyModifierIn: event) {
            input = .otherKey
        } else {
            return  // puszczenie innego modyfikatora
        }
        perform(machine.handle(input))
    }

    private func perform(_ action: HoldKeyMachine.Action) {
        switch action {
        case .none: break
        case .scheduleArm:
            armTask?.cancel()
            let delay = machine.armDelay
            armTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                guard !Task.isCancelled, let self else { return }
                self.perform(self.machine.handle(.armElapsed))
            }
        case .start: onStart?()
        case .stop: onStop?()
        case .cancel: onCancel?()
        case .alternate: onAlternate?()
        }
    }

    private func isPressed(_ event: NSEvent) -> Bool {
        switch key {
        case .fn: event.modifierFlags.contains(.function)
        // Bit urządzenia NX_DEVICERALTKEYMASK — odróżnia prawy ⌥ od lewego.
        case .rightOption: event.modifierFlags.rawValue & 0x40 != 0
        }
    }

    /// Inny modyfikator (⌘, ⇧, ⌃, lewy ⌥) właśnie wciśnięty.
    private func isPressed(anyModifierIn event: NSEvent) -> Bool {
        guard event.type == .flagsChanged else { return false }
        let flags = event.modifierFlags
        switch Int(event.keyCode) {
        case kVK_Command, kVK_RightCommand: return flags.contains(.command)
        case kVK_Shift, kVK_RightShift: return flags.contains(.shift)
        case kVK_Control, kVK_RightControl: return flags.contains(.control)
        case kVK_Option, kVK_RightOption: return flags.contains(.option)
        case kVK_Function: return flags.contains(.function)
        default: return false
        }
    }
}
