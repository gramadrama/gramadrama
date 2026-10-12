import Foundation

/// Rozmowa aplikacji z metodą wprowadzania „Polski (GramaDrama)” (`GramaIME`) przez gniazdo Unix.
/// Jedna linia JSON na wiadomość (koder zamienia końce linii w tekście na `\n`), jedno połączenie na prośbę.
/// Zakresy w jednostkach UTF-16 dokumentu, tak jak w IMK, AX i NSString.
///
/// Wszystkie pola poza `v`/`id`/`op` (prośba) i `v`/`id`/`ok` (odpowiedź) są opcjonalne: nowsza strona może
/// dodać pole, a starsza je pominie (syntezowany Decodable czyta opcjonalne pola przez `decodeIfPresent`).
/// Niezgodna zmiana znaczenia pól = nowa `version`; aplikacja nie rozmawia z IME w innej wersji.
package enum ImeProtocol {
    package static let version = 1
    package static let inputSourceID = "pl.gramadrama.inputmethod.GramaDrama"
    /// macOS szuka usługi metody wprowadzania pod nazwą „<bundle id>_Connection”.
    package static let connectionName = inputSourceID + "_Connection"
    package static let appBundleID = "pl.gramadrama.app"
    package static let bundleName = "GramaDramaIME.app"
    package static let executableName = "GramaIME"
    /// Ile tekstu prosimy po każdej stronie kursora (jak limit panelu).
    package static let maxWindow = 6000
    package static let maxMessageBytes = 512 * 1024

    /// Katalog 0700 na gniazdo i blokadę jednej kopii. Uprawnienia chronią przed innymi użytkownikami;
    /// przed innymi procesami tego samego użytkownika chroni sprawdzanie podpisu po drugiej stronie gniazda.
    package static var directory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/GramaDrama/ime", isDirectory: true)
    }
    package static var socketPath: String { directory.appendingPathComponent("ime.sock").path }
    package static var lockPath: String { directory.appendingPathComponent("ime.lock").path }
    package static var installURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Input Methods/\(bundleName)", isDirectory: true)
    }

    /// Klucze w ustawieniach aplikacji (`pl.gramadrama.app`), które czyta też IME.
    package enum DefaultsKey {
        /// Układ klawiatury, który IME nadpisuje przy każdej aktywacji (zapisywany przez `--ime-enable`).
        package static let keyboardLayout = "imeKeyboardLayout"
        /// Wyłącznik: IME tylko przepuszcza klawisze i nie odpowiada na prośby o tekst.
        package static let socketDisabled = "imeSocketDisabled"
    }
    package static let defaultKeyboardLayout = "com.apple.keylayout.PolishPro"

    package enum Op: String, Codable, Sendable {
        /// Stan IME: wersja, aktywne pole (token), aplikacje, które ją aktywowały.
        case hello
        /// Tekst przed kursorem, zaznaczenie i tekst za kursorem w aktywnym polu.
        case context
        /// Podmiany w polu `client`, jeśli w zakresie `check` wciąż jest `check.text`.
        case replace
        /// Wstawienie `text` w miejsce zaznaczenia albo przy kursorze (z odstępem, gdy trzeba).
        case insert
    }

    /// Powody odmowy, przy których IME na pewno niczego w polu nie zmieniła — aplikacja może wtedy spróbować
    /// zwykłej drogi (AX, schowek). Każdy inny wynik prośby zmieniającej tekst znaczy „nie wiadomo”.
    package static let preWriteReasons: Set<String> = [
        "no_client", "client_changed", "composing", "secure_input", "terminal", "expired", "changed",
        "bad_request", "no_selection", "not_text", "no_document_access", "slow_client", "disabled", "client_error_before",
    ]

    package struct Edit: Codable, Sendable, Equatable {
        package var location: Int
        package var length: Int
        package var text: String

        package init(location: Int, length: Int, text: String) {
            self.location = location
            self.length = length
            self.text = text
        }
    }

    package struct Request: Codable, Sendable {
        package var v = ImeProtocol.version
        package var id: String
        package var op: Op
        /// Po tej chwili (`ProcessInfo.systemUptime` — wspólny zegar procesów tego Maca) IME odrzuca prośbę
        /// bez zapisu. IME sprawdza termin na początku i tuż przed pierwszą zmianą w polu.
        package var deadline: Double?
        package var maxBefore: Int?
        package var maxAfter: Int?
        /// Token pola z `context`/`hello`: podmiana i wstawienie idą tylko do tego samego pola.
        package var client: String?
        /// Aplikacja pola. Po zamknięciu panelu ⌥⌘G macOS aktywuje pole jako nowy kontroler (inny token) —
        /// podmiana przyjmuje wtedy aktywne pole tej aplikacji, bo i tak sprawdza tekst zakresu `check`.
        package var app: String?
        /// Zakres i tekst, które muszą być w polu przed zapisem (cały pobrany fragment, nie tylko zmiana).
        package var check: Edit?
        /// Podmiany od końca dokumentu (IME stosuje je w tej kolejności, więc wcześniejsze przesunięcia są ważne).
        package var edits: [Edit]?
        /// Treść zakresu `check` po zapisie — do sprawdzenia, że poprawka jest na miejscu.
        package var expected: String?
        /// Tekst do wstawienia (dyktowanie).
        package var text: String?

        package init(op: Op) {
            self.id = UUID().uuidString
            self.op = op
        }
    }

    package struct Reply: Codable, Sendable {
        package var v = ImeProtocol.version
        package var id: String
        package var ok: Bool
        /// Krótki kod do logu, gdy `ok == false`.
        package var reason: String?
        /// Czy IME zaczęła zmieniać tekst w polu. Po `true` aplikacja nigdy nie próbuje drugi raz (ani ⌘V).
        package var attempted: Bool?
        /// Aplikacja, której pole jest aktywne w IME.
        package var app: String?
        package var client: String?
        /// Początek `before` w dokumencie (z `actualRange` aplikacji).
        package var windowStart: Int?
        package var selectionLocation: Int?
        package var selectionLength: Int?
        package var before: String?
        package var selected: String?
        package var after: String?
        /// Aplikacja oddała za kursorem mniej tekstu, niż prosiliśmy — poza Chromium znaczy to koniec dokumentu.
        package var shortAfter: Bool?
        /// Zaznaczenie dłuższe niż `maxWindow` — tekstu zaznaczenia nie ma w odpowiedzi.
        package var selectionTooLong: Bool?
        /// Ramka kursora w układzie Cocoa (lewy dolny róg ekranu głównego): x, y, szerokość, wysokość.
        package var caret: [Double]?
        /// Po podmianie albo wstawieniu: po ilu ms tekst był na miejscu (Electron stosuje zmiany asynchronicznie).
        package var landedAfterMs: Int?
        /// Jak znaleziono pole podmiany: „token” (to samo pole) albo „app” (aktywne pole tej samej aplikacji).
        package var clientMatch: String?
        /// Kursor przed zmianą i po niej (do przywrócenia kursora w aplikacjach, które go przesuwają).
        package var caretBefore: Int?
        package var caretAfter: Int?
        // hello
        package var build: String?
        package var imePid: Int32?
        package var uptime: Double?
        /// Aplikacje, które aktywowały IME w tym procesie: bundle ID → ile sekund temu ostatnio.
        package var apps: [String: Double]?
        package var requests: Int?

        package init(id: String, ok: Bool, reason: String? = nil) {
            self.id = id
            self.ok = ok
            self.reason = reason
        }
    }

    package static func encode<T: Encodable>(_ value: T) -> Data? {
        guard var data = try? JSONEncoder().encode(value) else { return nil }
        data.append(0x0A)
        return data
    }

    package static func decode<T: Decodable>(_ type: T.Type, from line: Data) -> T? {
        try? JSONDecoder().decode(type, from: line)
    }
}

/// Które aplikacje i w czym obsługuje metoda wprowadzania. Reszta idzie zwykłą drogą (AX, schowek).
package enum ImePolicy {
    /// Terminale: `insertText` to wpisane polecenie (koniec linii je wykonuje) — tam nigdy przez IME.
    package static let terminalBundleIDs: Set<String> = [
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "net.kovidgoyal.kitty",
        "org.alacritty", "io.alacritty", "com.github.wez.wezterm", "co.zeit.hyper", "com.mitchellh.ghostty",
    ]
    /// Chromium i Electron: metoda wprowadzania widzi tylko zaznaczenie ±100 znaków (bufor przeglądarki), więc
    /// ⌥⌘G zostaje na AX, a IME tylko wstawia dyktowanie przy kursorze. Aplikacja rozpoznaje też Electron po
    /// frameworku w pakiecie — ta lista to znane przypadki.
    package static let chromiumBundleIDs: Set<String> = [
        "com.tinyspeck.slackmacgap", "com.anthropic.claudefordesktop", "com.google.Chrome", "com.microsoft.edgemac",
        "com.brave.Browser", "company.thebrowser.Browser", "com.microsoft.teams2", "com.microsoft.VSCode",
        "com.hnc.Discord", "notion.id", "com.figma.Desktop", "org.whispersystems.signal-desktop", "md.obsidian",
        "com.vivaldi.Vivaldi", "com.operasoftware.Opera",
    ]
    /// WebKit z mailem (nowy Outlook, Mail): przy komplecie tekstu poprawiamy całą wiadomość, jak dziś,
    /// a kursor po podmianie wraca na miejsce (WebKit przesuwa go na poprawione słowo).
    package static let mailBundleIDs: Set<String> = ["com.microsoft.Outlook", "com.apple.mail"]

    package struct Rule: Equatable, Sendable {
        /// ⌥⌘G: odczyt i podmiana przez IME.
        package var capture: Bool
        /// Dyktowanie: wstawienie przez IME.
        package var insert: Bool
        package var wholeFieldWhenComplete: Bool
        package var restoreCaret: Bool
    }

    /// nil = ta aplikacja nigdy przez IME (terminale, sama GramaDrama — jej pola obsługuje AX, a czekanie na IME,
    /// która pyta o tekst nasz własny proces, zablokowałoby oba).
    package static func rule(for bundleID: String?, chromium: Bool) -> Rule? {
        guard let bundleID, bundleID != ImeProtocol.appBundleID, !terminalBundleIDs.contains(bundleID) else { return nil }
        if chromium || chromiumBundleIDs.contains(bundleID) {
            return Rule(capture: false, insert: true, wholeFieldWhenComplete: false, restoreCaret: false)
        }
        let mail = mailBundleIDs.contains(bundleID)
        return Rule(capture: true, insert: true, wholeFieldWhenComplete: mail, restoreCaret: mail)
    }
}

/// Drobne reguły tekstu wspólne dla aplikacji i IME.
package enum ImeText {
    /// Spacja między istniejącym tekstem a podyktowanym: „Hej.|” + „Kolejne” → „Hej. Kolejne”.
    package static func leadingSpaceNeeded(before text: String, after previous: Character?) -> Bool {
        guard let previous, let first = text.first else { return false }
        if previous.isWhitespace || "([{„\"'“‘/-–—@#".contains(previous) { return false }
        if first.isWhitespace || ".,;:!?)]}…”’".contains(first) { return false }
        return true
    }

    /// Do porównań tekstu z pola: twarda spacja jak zwykła (Chromium i WebKit różnie ją oddają).
    package static func comparable(_ text: String) -> String {
        text.replacingOccurrences(of: "\u{00A0}", with: " ")
    }
}
