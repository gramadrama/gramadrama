import AppKit
import InputMethodKit
import GramaIMEProtocol

// Metoda wprowadzania „Polski (GramaDrama)”. Najpierw serwer IMK i pętla zdarzeń — pisanie ma działać,
// nawet gdyby gniazdo się nie udało. Zamknięty rozmówca gniazda nie może nas zakończyć sygnałem.
signal(SIGPIPE, SIG_IGN)

let server = IMKServer(name: ImeProtocol.connectionName, bundleIdentifier: Bundle.main.bundleIdentifier)
if server == nil {
    imeLog.error("imk_server_failed")
} else {
    _ = Server.startedAt  // licznik czasu działania od startu procesu, nie od pierwszej prośby
    Server.start()
}
ProcessInfo.processInfo.disableAutomaticTermination("input method")
ProcessInfo.processInfo.disableSuddenTermination()
NSApplication.shared.run()
