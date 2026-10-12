import Foundation
import NaturalLanguage

/// Język tekstu: polski, angielski albo niemiecki (Pisanie → Języki tekstu); rozpoznany tekst może być też mieszany.
/// Kody jak na Linuksie (`core.SUPPORTED_LANGUAGES`): `pl`, `en`, `de`, `mixed`.
package enum TextLanguage: String, Sendable {
    case polish = "pl", english = "en", german = "de", mixed

    /// Języki do wyboru w ustawieniach.
    package static let supported: [TextLanguage] = [.polish, .english, .german]

    /// Kod w plikach promptów, ustawieniach i dla Whispera.
    package var code: String { rawValue }

    /// Nazwa w oknie aplikacji.
    package var label: String {
        switch self {
        case .polish: tr("Polski")
        case .english: tr("Angielski")
        case .german: tr("Niemiecki")
        case .mixed: tr("Mieszany")
        }
    }

    /// „PL”, „EN”, „DE” — kierunek tłumaczenia, pastylka dyktowania.
    package var shortLabel: String { self == .mixed ? "PL/EN" : rawValue.uppercased() }

    /// „po polsku”, „po angielsku”, „po niemiecku”.
    package var adverb: String {
        switch self {
        case .polish: tr("po polsku")
        case .english: tr("po angielsku")
        case .german: tr("po niemiecku")
        case .mixed: tr("w kilku językach")
        }
    }

    var nlLanguage: NLLanguage? {
        switch self {
        case .polish: .polish
        case .english: .english
        case .german: .german
        case .mixed: nil
        }
    }

    package var languageToolCode: String {
        switch self {
        case .polish: "pl-PL"
        case .english: "en-US"
        case .german: "de-DE"
        case .mixed: "auto"
        }
    }

    /// Rozpoznawanie języka na Macu (NaturalLanguage) — natychmiast, bez sieci.
    /// Linki, e-maile, @wzmianki, #kanały, kod i kody emoji nie mówią nic o języku zdania
    /// (np. „…/projekt_x/specyfikacja" w angielskim tekście), więc je pomijamy.
    static let nonLanguage = try! NSRegularExpression(
        // E-mail jako [^\s@]+@[^\s@]+ — wersja \S+@\S+ miała czas sześcienny (6000 × „a@" to minuty).
        pattern: #"https?://\S+|[^\s@]+@[^\s@]+\.\S+|[@#][\p{L}\p{N}._\-]+|`[^`]*`|:[a-z0-9_+\-]+:"#, options: [.caseInsensitive])

    /// Język tekstu spośród wybranych (jak `detect_language` na Linuksie): jeden wybrany język to zawsze on,
    /// przy dwóch — ten, który ma co najmniej 85% pewności, inaczej tekst mieszany.
    package static func detect(_ text: String, among languages: TextLanguages = .standard) -> TextLanguage {
        let all = languages.all
        guard all.count > 1 else { return languages.primary }
        let hypotheses = Self.hypotheses(text, among: all)
        return all.first { (hypotheses[$0] ?? 0) >= 0.85 } ?? .mixed
    }

    /// Pewność każdego z języków dla tekstu bez linków, wzmianek i kodu.
    static func hypotheses(_ text: String, among languages: [TextLanguage]) -> [TextLanguage: Double] {
        let prose = nonLanguage.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: " ")
        let recognizer = NLLanguageRecognizer()
        recognizer.languageConstraints = languages.compactMap(\.nlLanguage)
        recognizer.processString(prose)
        let found = recognizer.languageHypotheses(withMaximum: languages.count)
        return Dictionary(uniqueKeysWithValues: languages.map { ($0, $0.nlLanguage.flatMap { found[$0] } ?? 0) })
    }
}

/// Wybrane języki tekstu: główny i opcjonalnie drugi, różny od głównego — `Settings.languages` na Linuksie.
/// Kolejność ma znaczenie: główny jest pierwszy (LanguageTool `motherTongue`, profil promptów).
package struct TextLanguages: Hashable, Sendable {
    package let primary: TextLanguage
    package let secondary: TextLanguage?

    package init(primary: TextLanguage, secondary: TextLanguage?) {
        let primary = TextLanguage.supported.contains(primary) ? primary : .polish
        self.primary = primary
        self.secondary = secondary.flatMap { TextLanguage.supported.contains($0) && $0 != primary ? $0 : nil }
    }

    /// Polski + angielski: domyślne i wspólne z Linuksem wzorce promptów (bench/prompts.golden.json).
    package static let standard = TextLanguages(primary: .polish, secondary: .english)

    package var all: [TextLanguage] { [primary] + (secondary.map { [$0] } ?? []) }
    package var codes: [String] { all.map(\.code) }
    package func contains(_ language: TextLanguage) -> Bool { all.contains(language) }
    /// Tłumaczenie działa między dwoma wybranymi językami; przy jednym jest wyłączone.
    package var translates: Bool { secondary != nil }

    /// Drugi język pary względem podanego (tłumaczenie, ⇧ przy dyktowaniu); przy jednym języku — ten jeden.
    package func other(than language: TextLanguage) -> TextLanguage {
        guard let secondary else { return primary }
        return language == primary ? secondary : primary
    }

    /// „PL ↔ EN”, przy jednym języku samo „PL”.
    package var pairLabel: String { all.map(\.shortLabel).joined(separator: " ↔ ") }
}

/// Kierunek tłumaczenia: język docelowy spośród wybranej pary. Surowe wartości jak w bench/prompts.golden.json.
package enum TranslationTarget: String, Sendable {
    case english, polish, german

    package init(_ language: TextLanguage) {
        switch language {
        case .polish: self = .polish
        case .german: self = .german
        default: self = .english
        }
    }

    package var language: TextLanguage {
        switch self {
        case .english: .english
        case .polish: .polish
        case .german: .german
        }
    }

    package var code: String { language.code }

    /// „PL → EN”: z drugiego języka pary na ten.
    package func label(in languages: TextLanguages = .standard) -> String {
        "\(languages.other(than: language).shortLabel) → \(language.shortLabel)"
    }

    package func opposite(in languages: TextLanguages = .standard) -> TranslationTarget {
        TranslationTarget(languages.other(than: language))
    }

    /// Para polski + angielski: angielski → polski, polski i mieszany → angielski (jak dotąd na Macu). Inna para:
    /// na drugi język względem rozpoznanego, a w tekście mieszanym — względem tego, którego jest więcej.
    package static func automatic(for text: String, in languages: TextLanguages = .standard) -> TranslationTarget {
        let all = languages.all
        guard all.count > 1 else { return TranslationTarget(languages.primary) }
        if Set(all) == [.polish, .english] {
            return TextLanguage.detect(text, among: languages) == .english ? .polish : .english
        }
        var source = TextLanguage.detect(text, among: languages)
        if source == .mixed {
            let hypotheses = TextLanguage.hypotheses(text, among: all)
            source = all.max { (hypotheses[$0] ?? 0) < (hypotheses[$1] ?? 0) } ?? languages.primary
        }
        return TranslationTarget(languages.other(than: source))
    }
}

package enum Mode: String, CaseIterable, Identifiable {
    case fix, improve, formal, casual, shorter, translate

    package var id: String { rawValue }

    package var title: String {
        switch self {
        case .fix: tr("Popraw")
        case .improve: tr("Ulepsz")
        case .formal: tr("Formalnie")
        case .casual: tr("Luźniej")
        case .shorter: tr("Krócej")
        case .translate: tr("Tłumacz")
        }
    }

    package var instruction: String {
        switch self {
        case .fix:
            "Fix ONLY objective errors: spelling, typos, grammar, punctuation (in Polish especially commas before że, który, czy, jak, ponieważ, żeby, bo, gdy, jeśli, ale) and missing Polish diacritics (ą ć ę ł ń ó ś ź ż). Do not rephrase correct sentences, do not replace words with synonyms, do not add optional commas, do not change tone."
        case .improve:
            "Fix all errors and improve clarity and flow like a good editor. Keep the meaning, tone, level of formality and roughly the same length."
        case .formal:
            "Fix all errors and rewrite in a polite, professional business tone suitable for email to a client."
        case .casual:
            "Fix all errors and rewrite in a friendly, relaxed tone suitable for a Slack message to colleagues."
        case .shorter:
            "Fix all errors and make the text noticeably shorter and more direct without losing important information."
        case .translate:
            ""  // tłumaczenie ma własny prompt (Prompts.translationPrompt)
        }
    }

    package var temperature: Double {
        switch self {
        case .fix: 0
        case .translate: 0.2
        default: 0.4
        }
    }
}
