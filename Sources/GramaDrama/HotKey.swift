import Carbon
import GramaCore

/// Globalny skrót przez Carbon — nie wymaga uprawnień do monitorowania wejścia.
final class HotKey {
    private var ref: EventHotKeyRef?
    private static var handlers: [UInt32: () -> Void] = [:]
    private static var handlerInstalled = false
    /// Wynik rejestracji — eventHotKeyExistsErr znaczy, że skrót zajęła inna aplikacja.
    let status: OSStatus

    var isRegistered: Bool { status == noErr }

    init(id: UInt32, keyCode: Int, modifiers: Int, handler: @escaping () -> Void) {
        HotKey.handlers[id] = handler
        HotKey.installHandlerIfNeeded()
        let hotKeyID = EventHotKeyID(signature: OSType(0x4752_4D44), id: id) // 'GRMD'
        status = RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), hotKeyID, GetApplicationEventTarget(), 0, &ref)
    }

    convenience init(id: UInt32, shortcut: GlobalShortcut, handler: @escaping () -> Void) {
        self.init(id: id, keyCode: shortcut.keyCode, modifiers: Self.carbonModifiers(shortcut.modifiers), handler: handler)
    }

    deinit {
        if let ref { UnregisterEventHotKey(ref) }
    }

    static func carbonModifiers(_ modifiers: GlobalShortcut.Modifiers) -> Int {
        [(GlobalShortcut.Modifiers.control, controlKey), (.option, optionKey), (.shift, shiftKey), (.command, cmdKey)]
            .reduce(0) { modifiers.contains($1.0) ? $0 | $1.1 : $0 }
    }

    /// Czy skrót da się zarejestrować: próbna rejestracja, od razu zwolniona. Własne skróty GramaDramy są wtedy
    /// wstrzymane (nagrywanie skrótu), więc nie liczą się jako zajęte. Nie każdy skrót innej aplikacji Carbon zgłasza
    /// jako zajęty — wykrywa tylko te konflikty, które sam odrzuca.
    static func availability(of shortcut: GlobalShortcut) -> OSStatus {
        HotKey(id: 99, shortcut: shortcut) {}.status
    }

    private static func installHandlerIfNeeded() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hotKeyID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKeyID)
            let handler = HotKey.handlers[hotKeyID.id]
            DispatchQueue.main.async { handler?() }
            return noErr
        }, 1, &spec, nil, nil)
    }
}
