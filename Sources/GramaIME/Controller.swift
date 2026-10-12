import AppKit
import InputMethodKit
import GramaIMEProtocol
import GramaIMEShim

/// Kontroler jednego pola (sesji IMK). Przepuszcza wszystkie klawisze — pisanie idzie dokładnie tak jak
/// w Polski Pro, bo przy każdej aktywacji nadpisujemy układ klawiatury. Tekst pola czytamy i zmieniamy
/// tylko na prośbę aplikacji (`Ops`), nigdy przy klawiszu.
@objc(GramaIMEController)
final class GramaIMEController: IMKInputController {
    /// Numer nadany przy utworzeniu: token pola dla aplikacji (adres obiektu mógłby się powtórzyć).
    let serial = Registry.nextSerial()

    override func activateServer(_ sender: Any!) {
        let client = sender as? IMKTextInput
        var bundleID: String?
        var uniqueID: String?
        let layout = Registry.keyboardLayout
        _ = GDTry {
            client?.overrideKeyboard(withKeyboardNamed: layout)
            bundleID = client?.bundleIdentifier()
            uniqueID = (client as? NSObject)?.responds(to: #selector(IMKTextInput.uniqueClientIdentifierString)) == true
                ? client?.uniqueClientIdentifierString() : nil
        }
        Registry.shared.activate(self, bundleID: bundleID ?? "?", uniqueID: uniqueID ?? "")
        imeLog.notice("activate serial=\(self.serial, privacy: .public) app=\(bundleID ?? "?", privacy: .public) uid=\((uniqueID ?? "").count, privacy: .public)")
    }

    override func deactivateServer(_ sender: Any!) {
        Registry.shared.deactivate(self)
        imeLog.notice("deactivate serial=\(self.serial, privacy: .public)")
    }

    /// Ta sama ścieżka klawiszy co w sondzie (sprawdzona): IMK pyta o klawisze, a my żadnego nie zabieramy.
    override func recognizedEvents(_ sender: Any!) -> Int {
        Int(NSEvent.EventTypeMask.keyDown.rawValue)
    }

    override func handle(_ event: NSEvent!, client sender: Any!) -> Bool {
        false
    }
}

/// Które pola są aktywne. Tylko główny wątek (tam IMK woła kontrolery i tam wykonujemy prośby).
final class Registry {
    static let shared = Registry()

    struct Entry {
        weak var controller: GramaIMEController?
        let bundleID: String
        let uniqueID: String
        var active: Bool
        var activatedAt: TimeInterval
    }

    private static let serialLock = NSLock()
    private static var lastSerial = 0
    static func nextSerial() -> Int {
        serialLock.lock()
        defer { serialLock.unlock() }
        lastSerial += 1
        return lastSerial
    }

    /// Ustawienia aplikacji (ta sama domena co GramaDrama).
    static var defaults: UserDefaults? { UserDefaults(suiteName: ImeProtocol.appBundleID) }
    static var keyboardLayout: String {
        defaults?.string(forKey: ImeProtocol.DefaultsKey.keyboardLayout) ?? ImeProtocol.defaultKeyboardLayout
    }
    static var socketDisabled: Bool { defaults?.bool(forKey: ImeProtocol.DefaultsKey.socketDisabled) ?? false }

    private(set) var entries: [Int: Entry] = [:]
    private(set) var activeSerial: Int?
    /// bundle ID → chwila ostatniej aktywacji (`systemUptime`), do `hello` i stanu w aplikacji.
    private(set) var activations: [String: TimeInterval] = [:]
    /// Aplikacje, których pole ostatnio odpowiadało wolno — chwilowo bez próśb o tekst (klawisze działają dalej).
    var slowUntil: [String: TimeInterval] = [:]

    func activate(_ controller: GramaIMEController, bundleID: String, uniqueID: String) {
        let now = ProcessInfo.processInfo.systemUptime
        entries = entries.filter { $0.value.controller != nil }
        entries[controller.serial] = Entry(controller: controller, bundleID: bundleID, uniqueID: uniqueID, active: true, activatedAt: now)
        activeSerial = controller.serial
        activations[bundleID] = now
    }

    func deactivate(_ controller: GramaIMEController) {
        entries[controller.serial]?.active = false
        // Nowe pole bywa aktywowane przed dezaktywacją starego — czyścimy tylko, gdy to wciąż ono.
        if activeSerial == controller.serial { activeSerial = nil }
    }

    /// Aktywne pole: kontroler, jego token i aplikacja.
    var active: (controller: GramaIMEController, token: String, bundleID: String)? {
        guard let serial = activeSerial, let entry = entries[serial], entry.active, let controller = entry.controller else { return nil }
        return (controller, Self.token(serial, entry), entry.bundleID)
    }

    /// Pole o danym tokenie, jeśli jest teraz aktywne. Ten sam klient pod nowym kontrolerem (IMK potrafi
    /// utworzyć nowy po powrocie fokusu) rozpoznajemy po jego identyfikatorze.
    func activeController(for token: String) -> (controller: GramaIMEController, token: String, bundleID: String)? {
        guard let current = active else { return nil }
        if current.token == token { return current }
        let parts = token.split(separator: "|", maxSplits: 1).map(String.init)
        guard parts.count == 2, !parts[1].isEmpty, let serial = activeSerial, let entry = entries[serial],
              entry.uniqueID == parts[1] else { return nil }
        return current
    }

    static func token(_ serial: Int, _ entry: Entry) -> String { "\(serial)|\(entry.uniqueID)" }
}
