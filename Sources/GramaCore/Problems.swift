import Foundation

/// Przycisk nieudanej korekty; pierwszy z listy robi ↩ (PANEL.md, „Błąd”).
package enum ProblemAction: String, Sendable, CaseIterable {
    case retry
    case openServices = "open:connection"
    case openWriting = "open:writing"
    case login

    package var label: String {
        switch self {
        case .retry: tr("Spróbuj ponownie")
        case .openServices: tr("Otwórz Usługi")
        case .openWriting: tr("Otwórz Pisanie")
        case .login: tr("Zaloguj się…")
        }
    }
}

/// Co panel mówi o nieudanej korekcie: kod przyczyny (`errorKind` w GramaBackend, `core.error_kind` na Linuksie)
/// daje grupę, tytuł, zdanie i przyciski — ta sama tabela co `linux/gramadrama/engine/problems.py`. Grupy według tego,
/// co użytkownik może zrobić, nie według technicznej przyczyny:
///   1 trzeba coś ustawić: przycisk otwiera aplikację na właściwej zakładce; samo ponowienie by nie pomogło
///   2 serwer nie odpowiada: ponowienie (może się uruchamia), poza domem logowanie
///   3 model się potknął: ponowienie
///   4 tekstu nie da się poprawić w tej postaci: bez przycisku (zaznaczenie należy do użytkownika)
/// Tam, gdzie tabela nie ma zdania, zdaniem jest komunikat samego błędu (serwera, modelu albo sprawdzenia ustawień).
package struct Problem: Equatable, Sendable {
    package let group: Int
    package let title: String
    /// nil — komunikat samego błędu.
    package let text: String?
    /// Najwyżej dwa; pierwszy robi ↩.
    package let actions: [ProblemAction]

    init(_ group: Int, _ title: String, _ text: String? = nil, _ actions: [ProblemAction] = []) {
        self.group = group
        self.title = title
        self.text = text
        self.actions = actions
    }
}

package enum Problems {
    /// Potknięcia modelu, które sesja raz ponawia sama („Ponawiam…”), zanim cokolwiek powie: zerwany strumień, pusta
    /// odpowiedź, odpowiedź, która zmieniła to, co musi zostać, albo wpisała obce pismo. Nie ucięta odpowiedź.
    package static let retried: Set<String> = ["interrupted", "empty_reply", "protected_changed", "script_drift"]

    /// Tekstu nie da się poprawić w tej postaci: komunikat w dymku zamiast panelu.
    package static let textGroup = 4

    /// Co `errorKind` znalazło w połączeniu — w zdaniu o serwerze, który nie odpowiada.
    package static func networkDetail(_ why: String) -> String? {
        switch why {
        case "refused": tr("odmowa połączenia")
        case "timeout": tr("serwer nie odpowiedział na czas")
        case "dns": tr("nie znaleziono adresu")
        case "tls": tr("błąd szyfrowanego połączenia")
        case "reset": tr("połączenie zostało zerwane")
        case "unreachable": tr("brak drogi do serwera")
        default: nil
        }
    }

    /// Problem kodu przyczyny (w języku interfejsu): z tabeli, błąd sieci, status HTTP albo „Korekta się nie udała”.
    package static func problem(_ why: String) -> Problem {
        if networkDetail(why) != nil { return problem("network") }
        switch why {
        // 1 · trzeba coś ustawić. Nowa instalacja nie ma własnego serwera modelu: te trzy rozróżnia Ollama na tym Macu.
        case "no_ollama":
            return Problem(1, tr("Nie ma jeszcze modelu językowego"),
                           tr("Ollama nie jest zainstalowana na tym komputerze. Zainstaluj ją albo podaj adres serwera — oba sposoby są w zakładce Usługi."),
                           [.openServices])
        case "ollama_down":
            return Problem(1, tr("Ollama nie działa"),
                           tr("Ollama jest zainstalowana, ale nie odpowiada. Uruchom jej usługę albo podaj adres serwera w zakładce Usługi."),
                           [.openServices, .retry])
        case "no_model":
            return Problem(1, tr("Nie wybrano modelu"),
                           tr("Ollama działa, ale żaden model nie jest wybrany. Wybierz go albo pobierz w zakładce Usługi."),
                           [.openServices])
        case "settings":
            return Problem(1, tr("Trzeba dokończyć ustawienia"), nil, [.openServices])
        case "second_language":
            return Problem(1, tr("Tłumaczenie wymaga drugiego języka"), tr("Dodaj go w zakładce Pisanie (Języki tekstu)."),
                           [.openWriting])
        case "http_404":
            return Problem(1, tr("Na serwerze nie ma tego modelu"), nil, [.openServices])
        case "http_401", "http_403":
            return Problem(1, tr("Serwer odmówił dostępu"), nil, [.openServices])
        case "http_400":
            // Np. za mały kontekst: komunikat serwera mówi, co zmienić.
            return Problem(1, tr("Serwer odrzucił zapytanie"))
        // 2 · serwer nie odpowiada
        case "network":
            return Problem(2, tr("Serwer nie odpowiada"),
                           tr("Nie można połączyć się z serwerem modelu ({detail}). Sprawdź, czy działa, albo zmień adres w zakładce Usługi."),
                           [.retry, .openServices])
        case "offline":
            return Problem(2, tr("Serwer jest poza tą siecią"), tr("Zaloguj się w dostępie zdalnym, żeby łączyć się przez bramę."),
                           [.login, .retry])
        case "login":
            return Problem(2, tr("Trzeba zalogować się ponownie"), nil, [.login])
        case "login_unreachable":
            return Problem(2, tr("Serwer logowania nie odpowiada"), nil, [.retry, .openServices])
        // 3 · model się potknął
        case "interrupted":
            return Problem(3, tr("Połączenie przerwano przed końcem odpowiedzi"), tr("Zwykle wystarczy spróbować jeszcze raz."), [.retry])
        case "empty_reply":
            return Problem(3, tr("Model zwrócił pustą odpowiedź"),
                           tr("Zwykle wystarczy spróbować jeszcze raz albo wybrać inny tryb."), [.retry])
        case "bad_reply":
            return Problem(3, tr("Serwer odpowiedział w nieznanym formacie"),
                           tr("Sprawdź protokół serwera w zakładce Usługi (Ollama albo zgodny z OpenAI)."), [.retry, .openServices])
        case "model_error":
            return Problem(3, tr("Model zgłosił błąd"), nil, [.retry])
        case "script_drift", "protected_changed":
            return Problem(3, tr("Wynik odrzucono"), nil, [.retry])
        case "truncated_reply":
            return Problem(3, tr("Model uciął odpowiedź"), tr("Zaznacz krótszy fragment i naciśnij skrót jeszcze raz."))
        // 4 · tekstu nie da się poprawić w tej postaci
        case "empty":
            return Problem(4, tr("Nie ma tekstu do poprawienia"))
        case "too_long":
            return Problem(4, tr("Tekst jest za długi"))
        case "embedded":
            return Problem(4, tr("Tekst zawiera załącznik"))
        default:
            if why.hasPrefix("http_") { return Problem(2, tr("Serwer zgłosił błąd"), nil, [.retry, .openServices]) }
            return Problem(2, tr("Korekta się nie udała"), nil, [.retry])
        }
    }

    /// Problem i jego zdanie: zdanie z tabeli (o sieci — z tym, co zawiodło), a gdy tabela go nie ma — komunikat błędu.
    package static func describe(_ why: String, message: String) -> (problem: Problem, text: String) {
        let problem = problem(why)
        guard let text = problem.text else { return (problem, message) }
        let detail = networkDetail(why) ?? networkDetail("unreachable") ?? ""
        return (problem, text.replacingOccurrences(of: "{detail}", with: detail))
    }
}
