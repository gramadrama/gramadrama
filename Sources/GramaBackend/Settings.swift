import Foundation
import os
import GramaCore

package final class Settings: ObservableObject {
    package static let shared = Settings()
    private let defaults = UserDefaults.standard

    /// Model językowy: Ollama na tym Macu albo serwer (Ollama albo API zgodne z OpenAI) pod wpisanym adresem.
    /// Klucz API serwera jest w pęku kluczy (`ServerKey.model`), nie tutaj. Adres zostaje po powrocie na „Ten Mac”.
    @Published package var modelLocation: ServiceLocation { didSet { defaults.set(modelLocation.rawValue, forKey: "modelLocation") } }
    @Published package var modelServerURL: String { didSet { defaults.set(modelServerURL, forKey: "modelServerURL") } }
    @Published package var modelServerAPI: ModelServerAPI { didSet { defaults.set(modelServerAPI.rawValue, forKey: "modelServerAPI") } }
    /// Adres Ollamy na tym Macu; pusty — domyślny (`ModelServerEndpoint.local`).
    @Published package var modelLocalURL: String { didSet { defaults.set(modelLocalURL, forKey: "modelLocalURL") } }
    /// „Myślenie modelu”: false (zalecane) — Ollama dostaje `think: false` (serwer, który go odrzuci, zapytanie bez
    /// niego); true — model robi to po swojemu. Korekty mierzono bez myślenia.
    @Published package var modelThinking: Bool { didSet { defaults.set(modelThinking, forKey: "modelThinking") } }
    /// LanguageTool: serwer na tym Macu (port 8010 albo 8081) albo pod wpisanym adresem.
    @Published package var languageToolLocation: ServiceLocation {
        didSet { defaults.set(languageToolLocation.rawValue, forKey: "languageToolLocation") }
    }
    @Published package var languageToolServerURL: String { didSet { defaults.set(languageToolServerURL, forKey: "languageToolServerURL") } }
    /// Adres LanguageTool na tym Macu; pusty — pierwszy port, na którym odpowiada (8010, potem 8081).
    @Published package var languageToolLocalURL: String { didSet { defaults.set(languageToolLocalURL, forKey: "languageToolLocalURL") } }
    @Published package var remoteBaseURL: String { didSet { defaults.set(remoteBaseURL, forKey: "remoteBaseURL") } }
    @Published package var remoteClientID: String { didSet { defaults.set(remoteClientID, forKey: "remoteClientID") } }
    @Published package var model: String { didSet { defaults.set(model, forKey: "model") } }
    /// Aplikacje, w których GramaDrama jest wyłączona — ⌥⌘G/⌥⌘T trafiają tam bez zmian.
    @Published package var disabledBundleIDs: [String] { didSet { defaults.set(disabledBundleIDs, forKey: "disabledBundleIDs") } }
    /// Sprawdzanie podczas pisania (plakietka z liczbą błędów). Słownik macOS działa na Macu.
    @Published package var liveCheckEnabled: Bool { didSet { defaults.set(liveCheckEnabled, forKey: "liveCheck") } }
    /// …a LanguageTool dostaje akapit po chwili bez pisania.
    @Published package var liveLanguageToolEnabled: Bool { didSet { defaults.set(liveLanguageToolEnabled, forKey: "liveLanguageTool") } }
    /// Faliste podkreślenia pod błędnymi słowami (tam, gdzie AX podaje pozycje słów).
    @Published package var liveUnderlinesEnabled: Bool { didSet { defaults.set(liveUnderlinesEnabled, forKey: "liveUnderlines") } }
    /// …a po dłuższej pauzie model sprawdza tekst pola w tle — trudne błędy (osoba czasownika, styl).
    @Published package var liveBielikEnabled: Bool { didSet { defaults.set(liveBielikEnabled, forKey: "liveBielik") } }
    /// Eksperyment: podczas pisania sprawdza tylko model (zdanie po zdaniu, `ModelLive`) zamiast słownika
    /// i LanguageTool, a ⌥⌘G w trybie Popraw korzysta z tych samych wyników. Przełączniki warstw zostają
    /// zapisane na powrót do zwykłego trybu.
    @Published package var liveModelOnly: Bool { didSet { defaults.set(liveModelOnly, forKey: "liveModelOnly") } }
    /// Aplikacje dodane ręcznie do listy (widoczne, nawet jeśli skrótu jeszcze w nich nie użyto).
    /// Tryb, w którym ⌥⌘G otwiera panel w danej aplikacji (bundle ID → `Mode.rawValue`). Brak wpisu = Popraw.
    @Published package var appModes: [String: String] { didSet { defaults.set(appModes, forKey: "appModes") } }
    @Published package var manualBundleIDs: [String] { didSet { defaults.set(manualBundleIDs, forKey: "manualBundleIDs") } }
    /// ⌥⌘G i dyktowanie przez metodę wprowadzania „Polski (GramaDrama)”, gdy jest wybrana (bez AX i schowka).
    @Published package var imeChannelEnabled: Bool { didSet { defaults.set(imeChannelEnabled, forKey: "imeChannel") } }
    /// Języki tekstu (Pisanie → Języki tekstu): główny i opcjonalnie drugi; domyślnie polski + angielski.
    /// Korekta, sprawdzanie i dyktowanie w tych językach, tłumaczenie między nimi (jak `primary_language`
    /// i `secondary_language` na Linuksie).
    @Published package var textLanguages: TextLanguages {
        didSet {
            defaults.set(textLanguages.primary.code, forKey: "primaryLanguage")
            defaults.set(textLanguages.secondary?.code ?? "", forKey: "secondaryLanguage")
            // Interfejs po polsku przy polskim głównym języku, inaczej po angielsku (jak na Linuksie).
            UI.language = UILanguage(primary: textLanguages.primary)
        }
    }
    /// Globalne skróty korekty i tłumaczenia (Start → Skróty → Zmień skróty); domyślnie ⌥⌘G i ⌥⌘T.
    @Published package private(set) var correctionShortcut: GlobalShortcut
    @Published package private(set) var translationShortcut: GlobalShortcut
    /// Strony (host bez „www.”), na których wtyczka przeglądarki nie sprawdza tekstu podczas pisania.
    /// ⌥⌘G i dyktowanie działają tam dalej — to polecenie użytkownika, nie sprawdzanie w tle.
    @Published package var disabledSites: [String] { didSet { defaults.set(disabledSites, forKey: "disabledSites") } }
    /// Własne słowa (Pisanie › Słownik i reguły): imiona, nazwy i żargon, po przecinku. Ten sam klucz co dawniej słowa
    /// dyktowania — słownik jest teraz jeden dla sprawdzania, korekty i dyktowania (`dictation_vocabulary` na Linuksie).
    @Published package var ownWordsText: String { didSet { defaults.set(ownWordsText, forKey: "dictationVocabulary") } }
    /// Słowa dodane „Dodaj do słownika” w GramaDramie, w kolejności dodania: macOS uczy ich swój słownik, ale nie podaje
    /// listy nauczonych słów, a korekta i dyktowanie potrzebują ich pisowni.
    @Published package private(set) var learnedWords: [String] { didSet { defaults.set(learnedWords, forKey: "learnedWords") } }
    /// Reguły LanguageTool wyłączone kartą („Wyłącz regułę”), w kolejności wyłączenia: identyfikator i opis LanguageTool,
    /// nigdy tekst, w którym ją znalazł (`rules.json` na Linuksie).
    @Published package private(set) var rulesOff: [RuleOff] {
        didSet { defaults.set(rulesOff.map { ["id": $0.id, "name": $0.name] }, forKey: "languageToolRulesOff") }
    }

    private init() {
        // No deployment-specific destinations: a new installation uses the services on this Mac until a server is set.
        // Do wersji 0.5.2 jedno pole „serverHost” (Ollama :11434, LanguageTool :8010) — przenosimy je raz, bez zmiany
        // działania. Stary klucz zostaje, więc poprzednia wersja aplikacji dalej znajdzie swój serwer.
        // Bez zapisu: dopóki nie zmienisz usług w aplikacji, ich ustawienia wynikają ze starego pola.
        let gateway = RemoteServiceConfiguration(address: defaults.string(forKey: "remoteBaseURL") ?? "",
                                                 clientID: defaults.string(forKey: "remoteClientID") ?? "") != nil
        let legacy = LegacyServerHost.migrate(defaults.string(forKey: "serverHost") ?? "", gatewayConfigured: gateway)
        modelLocation = defaults.string(forKey: "modelLocation").flatMap(ServiceLocation.init) ?? legacy.modelLocation
        modelServerURL = defaults.string(forKey: "modelServerURL") ?? legacy.modelServerURL
        modelServerAPI = defaults.string(forKey: "modelServerAPI").flatMap(ModelServerAPI.init) ?? .ollama
        modelLocalURL = defaults.string(forKey: "modelLocalURL") ?? ""
        modelThinking = defaults.object(forKey: "modelThinking") as? Bool ?? false
        languageToolLocation = defaults.string(forKey: "languageToolLocation").flatMap(ServiceLocation.init) ?? legacy.languageToolLocation
        languageToolServerURL = defaults.string(forKey: "languageToolServerURL") ?? legacy.languageToolServerURL
        languageToolLocalURL = defaults.string(forKey: "languageToolLocalURL") ?? ""
        remoteBaseURL = defaults.string(forKey: "remoteBaseURL") ?? ""
        remoteClientID = defaults.string(forKey: "remoteClientID") ?? ""
        model = defaults.string(forKey: "model") ?? "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest"
        disabledBundleIDs = defaults.stringArray(forKey: "disabledBundleIDs") ?? []
        manualBundleIDs = defaults.stringArray(forKey: "manualBundleIDs") ?? []
        appModes = defaults.dictionary(forKey: "appModes") as? [String: String] ?? [:]
        liveCheckEnabled = defaults.object(forKey: "liveCheck") as? Bool ?? true
        liveLanguageToolEnabled = defaults.object(forKey: "liveLanguageTool") as? Bool ?? true
        liveBielikEnabled = defaults.object(forKey: "liveBielik") as? Bool ?? true
        liveModelOnly = defaults.object(forKey: "liveModelOnly") as? Bool ?? false
        liveUnderlinesEnabled = defaults.object(forKey: "liveUnderlines") as? Bool ?? true
        imeChannelEnabled = defaults.object(forKey: "imeChannel") as? Bool ?? true
        disabledSites = defaults.stringArray(forKey: "disabledSites") ?? []
        ownWordsText = defaults.string(forKey: "dictationVocabulary") ?? ""
        learnedWords = defaults.stringArray(forKey: "learnedWords") ?? []
        rulesOff = (defaults.array(forKey: "languageToolRulesOff") as? [[String: String]] ?? []).compactMap { record in
            guard let id = record["id"], UserDictionary.isRule(id) else { return nil }
            return RuleOff(id: id, name: record["name"] ?? "")
        }
        // Pusty drugi język („”) to jeden język; brak wpisu — wartości domyślne.
        let secondary = defaults.string(forKey: "secondaryLanguage").map { TextLanguage(rawValue: $0) } ?? .english
        let languages = TextLanguages(primary: defaults.string(forKey: "primaryLanguage").flatMap(TextLanguage.init) ?? .polish,
                                      secondary: secondary)
        textLanguages = languages
        UI.language = UILanguage(primary: languages.primary)
        (correctionShortcut, translationShortcut) = GlobalShortcut.resolve(correction: defaults.string(forKey: "correctionShortcut"),
                                                                           translation: defaults.string(forKey: "translationShortcut"))
    }

    /// Zapisuje oba skróty naraz (po sprawdzeniu w oknie: zasady, różne od siebie, wolne w systemie). Domyślny skrót
    /// nie zostaje w ustawieniach — zmiana wartości domyślnej w nowej wersji obejmie także tę instalację.
    package func setShortcuts(correction: GlobalShortcut, translation: GlobalShortcut) {
        guard !correction.sameKeys(as: translation) else { return }
        for (key, value, fallback) in [("correctionShortcut", correction, GlobalShortcut.defaultCorrection),
                                       ("translationShortcut", translation, GlobalShortcut.defaultTranslation)] {
            if value.sameKeys(as: fallback) { defaults.removeObject(forKey: key) } else { defaults.set(value.rawValue, forKey: key) }
        }
        correctionShortcut = correction
        translationShortcut = translation
    }

    package var modelDisplayName: String { Self.displayName(ofModel: model) }

    // MARK: Słownik i reguły (dictionary.py na Linuksie)

    package var ownWords: [String] { UserDictionary.parse(ownWordsText) }

    /// Dodane do słownika, najnowsze pierwsze: w GramaDramie, potem z listy systemu (słowa nauczone w innych aplikacjach).
    package var learnedLatestFirst: [String] { UserDictionary.merge(learnedWords.reversed(), SystemWordList.words.reversed()) }

    /// Cały słownik: własne słowa, potem dodane do słownika (najnowsze pierwsze), każde raz.
    package var dictionaryWords: [String] { UserDictionary.merge(ownWords, learnedLatestFirst) }

    /// Słowo dodane do słownika (jego naukę w macOS robi `UserWords.learn`); najstarsze ustępują miejsca po 2000.
    package func recordLearned(_ word: String) {
        let word = word.trimmingCharacters(in: .whitespaces)
        guard !word.isEmpty, word.unicodeScalars.count <= UserDictionary.maxWord else { return }
        var words = learnedWords.filter { $0 != word }
        words.append(word)
        learnedWords = Array(words.suffix(2000))
    }

    package func forgetLearned(_ word: String) { learnedWords.removeAll { $0 == word } }

    package var rulesOffIDs: [String] { rulesOff.map(\.id) }

    /// „Wyłącz regułę”: LanguageTool jej nie sprawdza, aż do „Włącz” w Pisaniu; najstarsza ustępuje miejsca po 200.
    package func turnOffRule(_ id: String, name: String) {
        guard UserDictionary.isRule(id) else { return }
        let name = String(name.split(whereSeparator: \.isWhitespace).joined(separator: " ").prefix(200))
        var rules = rulesOff
        if let index = rules.firstIndex(where: { $0.id == id }) {
            rules[index] = RuleOff(id: id, name: name)
        } else {
            rules.append(RuleOff(id: id, name: name))
        }
        rulesOff = Array(rules.suffix(200))
    }

    package func turnOnRule(_ id: String) { rulesOff.removeAll { $0.id == id } }

    /// Tryb „tylko model” działa, gdy włączone jest też sprawdzanie podczas pisania.
    package var modelOnlyActive: Bool { liveCheckEnabled && liveModelOnly }

    /// „hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M" → „Bielik-11B-v2.3",
    /// „hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest" → „Gemma-4-12B", „qwen3:8b" → „Qwen3:8b".
    package static func displayName(ofModel model: String) -> String {
        let last = model.split(separator: "/").last.map(String.init) ?? model
        var parts = last.split(separator: ":", maxSplits: 1).map(String.init)
        var name = parts.isEmpty ? last : parts.removeFirst()
        // Od pierwszego znacznika wariantu (instrukcyjny, format, kwantyzacja) to już nie nazwa modelu.
        func variant(_ token: String) -> Bool {
            ["instruct", "it", "gguf", "qat"].contains(token.lowercased())
                || token.range(of: #"^([qQ]\d|[bB]?[fF](16|32))"#, options: .regularExpression) != nil
        }
        var tokens = name.split(separator: "-").map(String.init)
        if let cut = tokens.firstIndex(where: variant), cut > 0 { tokens = Array(tokens[..<cut]) }
        // Za rozmiarem („12B”, „e4b”) słowo bez cyfr to już wariant albo własna nazwa importu
        // („gemma4-e4b-gramadrama” → „Gemma4-E4B”); wersja z cyframi („Bielik-11B-v2.3”) zostaje.
        func size(_ token: String) -> Bool { token.range(of: #"^[eE]?\d+(\.\d+)?[bBmM]$"#, options: .regularExpression) != nil }
        if let sized = tokens.firstIndex(where: size),
           let cut = tokens[(sized + 1)...].firstIndex(where: { !$0.contains(where: \.isNumber) }) {
            tokens = Array(tokens[..<cut])
        }
        // Gemma „E4B” (efektywne 4B) pisana wielkimi literami jak w nazwie modelu.
        tokens = tokens.map { $0.range(of: #"^[eE]\d+[bB]$"#, options: .regularExpression) != nil ? $0.uppercased() : $0 }
        name = tokens.joined(separator: "-")
        // Tag z biblioteki Ollamy zaczyna się od rozmiaru („qwen3:8b”, „gemma3:12b-it-qat”) — rozmiar zostaje;
        // „latest” i sama kwantyzacja („Q4_K_M”) nie.
        if let tag = parts.first, tag.lowercased() != "latest" {
            let size = tag.split(separator: "-").map(String.init).prefix { !variant($0) }
            if !size.isEmpty { name += ":" + size.joined(separator: "-") }
        }
        return name.prefix(1).uppercased() + name.dropFirst()
    }

    /// Wpisany serwer modelu (nil — pusty albo błędny adres). Na tym Macu to `localModelServer`.
    package var modelServer: ModelServerEndpoint? { ModelServerEndpoint(address: modelServerURL, api: modelServerAPI) }
    /// Ollama na tym Macu: wpisany adres albo domyślny.
    package var localModelServer: ModelServerEndpoint { .local(address: modelLocalURL) }
    /// LanguageTool na tym Macu pod wpisanym adresem; nil — szukamy go na portach 8010 i 8081.
    package var localLanguageToolServer: LanguageToolEndpoint? { .local(address: languageToolLocalURL) }
    /// Wpisany serwer LanguageTool (nil — pusty albo błędny adres).
    package var languageToolServer: LanguageToolEndpoint? { LanguageToolEndpoint(address: languageToolServerURL) }

    /// Serwer modelu w kluczach pamięci wyników: inne miejsce, adres, protokół albo brama dają inne odpowiedzi.
    package var modelServerIdentity: String {
        let server = modelLocation == .server ? "\(modelServerAPI.rawValue) \(modelServerURL)" : modelLocalURL
        return [modelLocation.rawValue, server, remoteBaseURL, remoteClientID].joined(separator: "|")
    }

    package var languageToolIdentity: String {
        let server = languageToolLocation == .server ? languageToolServerURL : languageToolLocalURL
        return [languageToolLocation.rawValue, server, remoteBaseURL, remoteClientID].joined(separator: "|")
    }

    /// Tryby do wyboru jako domyślne — Tłumacz ma własny skrót (⌥⌘T), więc nie zastępuje ⌥⌘G.
    package static let defaultModeChoices = Mode.allCases.filter { $0 != .translate }

    package func defaultMode(for bundleID: String?) -> Mode { Self.defaultMode(for: bundleID, in: appModes) }

    package static func defaultMode(for bundleID: String?, in appModes: [String: String]) -> Mode {
        guard let mode = bundleID.flatMap({ appModes[$0] }).flatMap(Mode.init(rawValue:)),
              defaultModeChoices.contains(mode) else { return .fix }
        return mode
    }

    package func setDefaultMode(_ mode: Mode, for bundleID: String) {
        appModes[bundleID] = mode == .fix ? nil : mode.rawValue
    }

    package func isDisabled(_ bundleID: String?) -> Bool {
        bundleID.map(disabledBundleIDs.contains) ?? false
    }

    package func setDisabled(_ bundleID: String, _ disabled: Bool) {
        disabledBundleIDs.removeAll { $0 == bundleID }
        if disabled { disabledBundleIDs.append(bundleID) }
    }

    /// „WWW.Example.com.” → „example.com”. Dokładny host: wyłączenie mail.google.com nie dotyczy docs.google.com.
    package static func site(_ host: String) -> String {
        var site = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ". "))
        if site.hasPrefix("www.") { site.removeFirst(4) }
        return site
    }

    package func isSiteDisabled(_ host: String?) -> Bool {
        guard let host, !host.isEmpty else { return false }
        return disabledSites.contains(Self.site(host))
    }

    package func setSiteDisabled(_ host: String, _ disabled: Bool) {
        let site = Self.site(host)
        guard !site.isEmpty, disabled != disabledSites.contains(site) else { return }
        // Jedno przypisanie = jedno powiadomienie (lista idzie wtedy raz do przeglądarek).
        disabledSites = disabled ? disabledSites + [site] : disabledSites.filter { $0 != site }
    }
}

/// Reguła LanguageTool wyłączona przez użytkownika: identyfikator i opis LanguageTool (do listy w Pisaniu).
package struct RuleOff: Equatable, Sendable {
    package let id: String
    package let name: String
}

/// Słowa nauczone w słowniku systemu („Naucz się pisowni” także w innych aplikacjach). macOS nie ma API do ich listy;
/// leży w ~/Library/Spelling/LocalDictionary (jedno słowo w linii). Plik czytamy tylko, gdy się zmienił.
package enum SystemWordList {
    private struct State {
        var stamp = ""
        var words: [String] = []
        /// Usunięte w Pisaniu: system może zapisać plik z opóźnieniem.
        var hidden: Set<String> = []
    }

    private static let state = OSAllocatedUnfairLock(initialState: State())
    private static let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Spelling/LocalDictionary")

    package static var words: [String] {
        let attributes = (try? FileManager.default.attributesOfItem(atPath: file.path)) ?? [:]
        let size = attributes[.size] as? Int ?? 0
        let stamp = "\((attributes[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0) \(size)"
        return state.withLock { current in
            if current.stamp != stamp {
                current.stamp = stamp
                let text = size > 0 && size <= 256 * 1024 ? (try? String(contentsOf: file, encoding: .utf8)) : nil
                current.words = UserDictionary.listWords(text ?? "")
            }
            return current.words.filter { !current.hidden.contains($0) }
        }
    }

    package static func hide(_ word: String) {
        state.withLock { _ = $0.hidden.insert(word) }
    }
}
