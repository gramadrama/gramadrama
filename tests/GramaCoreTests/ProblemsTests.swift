import Testing
@testable import GramaCore

/// Ta sama tabela co `linux/gramadrama/engine/problems.py` (grupy i przyciski według kodu przyczyny).
@Suite("Panel: nieudana korekta według przyczyny")
struct ProblemsTests {
    @Test func groupsAndButtonsFollowTheLinuxTable() {
        let expected: [String: (Int, [ProblemAction])] = [
            "no_ollama": (1, [.openServices]), "ollama_down": (1, [.openServices, .retry]), "no_model": (1, [.openServices]),
            "settings": (1, [.openServices]), "second_language": (1, [.openWriting]), "http_404": (1, [.openServices]),
            "http_401": (1, [.openServices]), "http_403": (1, [.openServices]), "http_400": (1, []),
            "network": (2, [.retry, .openServices]), "http_500": (2, [.retry, .openServices]), "offline": (2, [.login, .retry]),
            "login": (2, [.login]), "login_unreachable": (2, [.retry, .openServices]),
            "interrupted": (3, [.retry]), "empty_reply": (3, [.retry]), "bad_reply": (3, [.retry, .openServices]),
            "model_error": (3, [.retry]), "script_drift": (3, [.retry]), "protected_changed": (3, [.retry]),
            "truncated_reply": (3, []), "empty": (4, []), "too_long": (4, []), "embedded": (4, []),
            "SomethingUnexpected": (2, [.retry]),
        ]
        for (why, (group, actions)) in expected {
            let problem = Problems.problem(why)
            #expect(problem.group == group, "\(why)")
            #expect(problem.actions == actions, "\(why)")
        }
    }

    @Test func aNetworkFailureSaysWhatFailed() {
        for why in ["refused", "timeout", "dns", "tls", "reset", "unreachable"] {
            #expect(Problems.problem(why) == Problems.problem("network"), "\(why)")
        }
        let (problem, text) = Problems.describe("refused", message: "Could not connect to the server.")
        #expect(problem.title == "Serwer nie odpowiada")
        #expect(text == "Nie można połączyć się z serwerem modelu (odmowa połączenia). Sprawdź, czy działa, albo zmień adres w zakładce Usługi.")
    }

    @Test func withoutASentenceTheErrorsOwnMessageIsShown() {
        let (problem, text) = Problems.describe("http_404", message: "model 'x' not found (HTTP 404)")
        #expect(problem.title == "Na serwerze nie ma tego modelu")
        #expect(text == "model 'x' not found (HTTP 404)")
    }

    @Test func onlySlipsOfTheModelAreRetriedQuietly() {
        #expect(Problems.retried == ["interrupted", "empty_reply", "protected_changed", "script_drift"])
        // A cut-short answer is not retried: only a shorter selection helps.
        #expect(!Problems.retried.contains("truncated_reply"))
        #expect(Problems.retried.allSatisfy { Problems.problem($0).group == 3 })
    }
}
