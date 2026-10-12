import ApplicationServices
import Foundation

/// Odczyty Accessibility poza głównym wątkiem.
///
/// Każde wywołanie AX czeka synchronicznie na odpowiedź aplikacji docelowej. Zawieszony Slack czy Outlook
/// blokowałby na głównym wątku całą GramaDramę (panel, plakietkę, dyktowanie) — sprawdzanie na żywo pyta
/// o pole co 0,4 s. Jedna seryjna kolejka: odczyty idą po kolei, więc zawieszona aplikacja nie mnoży
/// wątków, a stan używany tylko tutaj (np. `TextGeometry`) nie potrzebuje blokad.
enum AXQueue {
    private static let queue = DispatchQueue(label: "pl.gramadrama.ax", qos: .userInitiated)
    /// Po tym czasie odczyt uznajemy za wolny i zapisujemy w logu (`ax_slow`).
    static let slowThreshold: TimeInterval = 0.5

    /// Limit czasu dla wszystkich wywołań AX w procesie. Musi być ustawiony na obiekcie systemowym: ustawiony
    /// na elemencie aplikacji obejmuje tylko ten jeden element, a pole, jego rodzice i elementy tekstu
    /// czekałyby domyślne 6 s.
    static func configure(timeout: Float = 1.0) {
        AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), timeout)
    }

    static func run<T>(_ work: @escaping () -> T) async -> T {
        await withCheckedContinuation { continuation in
            queue.async { continuation.resume(returning: work()) }
        }
    }
}
