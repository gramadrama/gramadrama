import Foundation
import GramaCore

/// Krótki kod tego, dlaczego zapytanie się nie udało (`core.error_kind` na Linuksie): do dziennika, który nigdy nie
/// trzyma komunikatu (tekst błędu modelu albo serwera może cytować tekst użytkownika), i do tabeli `Problems`.
package func errorKind(_ error: Error) -> String {
    switch error {
    case is CancellationError: "cancelled"
    case let error as BackendError: error.kind
    case let refusal as ResultRefusal: refusal == .empty ? "empty_reply" : "protected_changed"
    case let error as URLError: networkKind(error)
    default: String(String(describing: type(of: error)).prefix(40))
    }
}

/// Dlaczego połączenie się nie udało: timeout, refused, dns, tls, reset albo unreachable (`core.network_kind`).
package func networkKind(_ error: URLError) -> String {
    switch error.code {
    case .cancelled: "cancelled"
    case .timedOut: "timeout"
    case .cannotConnectToHost: "refused"
    case .cannotFindHost, .dnsLookupFailed: "dns"
    case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateNotYetValid,
         .serverCertificateHasUnknownRoot, .clientCertificateRejected, .clientCertificateRequired: "tls"
    case .networkConnectionLost: "reset"
    default: "unreachable"
    }
}

extension BackendError {
    /// Kod przyczyny (`Problems.problem`); te same kody co `kind` błędów na Linuksie.
    package var kind: String {
        switch self {
        case .http(let code, _): "http_\(code)"
        case .notConfigured: "settings"
        case .stream: "model_error"
        case .badReply: "bad_reply"
        case .incomplete: "interrupted"
        case .truncated: "truncated_reply"
        // Poza domem bez zalogowania (albo z tokenem, którego brama już nie przyjmuje).
        case .signInRequired: "offline"
        case .remote: "login"
        case .scriptDrift: "script_drift"
        }
    }
}
