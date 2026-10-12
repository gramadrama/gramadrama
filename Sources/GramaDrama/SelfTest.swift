import Foundation
import GramaCore
import GramaBackend

/// `GramaDrama --selftest [--dump-cases <plik.json>]` — testy logiki bez GUI i bez serwera.
/// Command Line Tools nie mają XCTest, więc proste asercje w samej aplikacji.
/// `--dump-cases` zapisuje przypadki post-processingu do wspólnego pliku JSON (tests/postprocess_cases.json),
/// który czyta też wersja na Linuksa — jedno źródło, żeby implementacje się nie rozjechały.
enum SelfTest {
    static func run(dumpCasesTo dumpPath: String? = nil) -> Int32 {
        var failures = 0
        var recorded: [[String: Any]] = []
        var pendingClean: [String: Any]?
        func check(_ name: String, _ actual: String, _ expected: String) {
            if actual == expected {
                print("  ✓ \(name)")
            } else {
                failures += 1
                print("  ✗ \(name)\n      wynik:     \(actual.debugDescription)\n      oczekiwano: \(expected.debugDescription)")
            }
            if var entry = pendingClean {
                entry["name"] = name
                entry["expected"] = expected
                recorded.append(entry)
            }
            pendingClean = nil
        }
        func clean(_ output: String, _ original: String) -> String {
            pendingClean = ["output": output, "original": original, "strict": false]
            return PostProcess.clean(output, original: original)
        }

        print("Komentarze modelu")
        let colon = "symbol : daje error : cos tam z :"
        check("sam komentarz → bez zmian",
              clean("(The original text does not contain any errors that need to be fixed.)", colon), colon)
        check("komentarz po polsku → bez zmian", clean("Tekst nie zawiera błędów.", "ok dzięki"), "ok dzięki")
        check("tekst + komentarz w nawiasie w nowej linii",
              clean("symbol: daje error: coś tam z:\n\n(The original text does not contain any errors.)", colon),
              "symbol: daje error: coś tam z:")
        check("wstęp „Oto poprawiony tekst:”",
              clean("Oto poprawiony tekst:\nMyślę, że jutro.", "Mysle ze jutro."), "Myślę, że jutro.")
        check("wstęp „Here is the corrected text:”",
              clean("Here is the corrected text:\n\nI think so.", "i thnk so."), "I think so.")
        check("komentarz doklejony na końcu",
              clean("Dzięki, jasne. (No changes were needed in this text.)", "Dzięki, jasne."), "Dzięki, jasne.")
        check("prawdziwy nawias autora zostaje",
              clean("Spotkanie jutro (w sali B).", "Spotkanie jutro (w sali b)."), "Spotkanie jutro (w sali B).")
        check("zdanie autora o tekście zostaje",
              clean("Ten tekst jest poprawny, ale długi.", "Ten tekst jest poprawny ale długi."),
              "Ten tekst jest poprawny, ale długi.")
        check("„Uwaga:” autora zostaje",
              clean("Uwaga: jutro nie ma daily.", "Uwaga : jutro nie ma daily."), "Uwaga: jutro nie ma daily.")

        check("„There is” to nie komentarz",
              clean("There is an error in the deployment script, can you check?", "there is an eror in teh deploiment script, can u chek?"),
              "There is an error in the deployment script, can you check?")
        check("„Where are” to nie komentarz", clean("Where are the release notes?", "wher are teh release notes?"),
              "Where are the release notes?")
        check("pierwsza linia z dwukropkiem autora zostaje",
              clean("@Kasia poniżej lista rzeczy na jutro:\n- deploy\n- testy", "@Kasia ponizej lista rzeczy na jutro:\n- deploy\n- testy"),
              "@Kasia poniżej lista rzeczy na jutro:\n- deploy\n- testy")
        check("„there are two options:” zostaje", clean("There are two options:\n1. revert\n2. hotfix", "there are two options:\n1. revert\n2. hotfix"),
              "There are two options:\n1. revert\n2. hotfix")
        check("nawias autora z poprawką zostaje", clean("Wysłałem poprawki (tekst jest w załączniku)", "wyslalem poprawki (tekst jest w zalaczniku)"),
              "Wysłałem poprawki (tekst jest w załączniku)")
        check("jednolinijkowiec z „oryginalną” zostaje", clean("Wysłałem oryginalną wersję umowy.", "wyslalem oryginalna wersje umowy."),
              "Wysłałem oryginalną wersję umowy.")

        check("Formalnie: treść pod powitaniem zostaje",
              clean("Hi John,\n\nThe report will be ready on Friday. Please let me know if you need any changes.",
                    "hi john report on friday, tell me if u need smth changed"),
              "Hi John,\n\nThe report will be ready on Friday. Please let me know if you need any changes.")
        check("Popraw: nagłówek z dwukropkiem w nowej linii zostaje",
              clean("Hej, mam pytanie:\nczy ktoś wie, jak odpalić testy?", "Hej, mam pytnie: czy ktos wie jak odpalic testy?"),
              "Hej, mam pytanie:\nczy ktoś wie, jak odpalić testy?")
        check("Ulepsz: lista z nagłówkiem zostaje", clean("Jutro planujemy:\n- deploy,\n- testy,\n- review.", "jutro robimy deploy, testy i review"),
              "Jutro planujemy:\n- deploy,\n- testy,\n- review.")
        check("Formalnie: „Note:” modelu jako treść zostaje",
              clean("Dear John,\nThe report is attached.\nNote: the office will be closed on Monday.", "john report attached, office closed monday"),
              "Dear John,\nThe report is attached.\nNote: the office will be closed on Monday.")
        check("Luźniej: „message looks good” to nie komentarz", clean("Your message looks good — go ahead and send it! 🚀", "msg looks ok, send it"),
              "Your message looks good — go ahead and send it! 🚀")
        let three = "ok, thanks!\nI'll check it tomorrow\nand let you know"
        check("wielolinijkowy komentarz → bez zmian", clean("The original text does not contain any errors.\n\nNo corrections were necessary.", three), three)
        check("komentarz w tej samej linii → poprawka zostaje",
              clean("symbol: daje error: coś tam z: (The original text does not contain any errors.)", colon), "symbol: daje error: coś tam z:")
        check("„Here's a shorter version:” zdjęte", clean("Here's the shorter version:\n\nRelease moves to Friday.", "the release is moving to friday because qa needs a day"),
              "Release moves to Friday.")
        check("wstęp w tej samej linii zdjęty", clean("Oto poprawiony tekst: Myślę, że tak.", "mysle ze tak."), "Myślę, że tak.")
        check("nagłówek przy U+2028 zostaje",
              clean("Lista zmian w release:\n- fix logowania", "Lista zmian w relase:\u{2028}- fix logowania"), "Lista zmian w release:\n- fix logowania")

        print("Tłumaczenie (ostrzejszy filtr komentarzy)")
        func translated(_ output: String, _ original: String) -> String {
            pendingClean = ["output": output, "original": original, "strict": true]
            return PostProcess.clean(output, original: original, strictCommentary: true)
        }
        check("„the original text” w tłumaczeniu zostaje",
              translated("Please send me the original text of the contract.", "Wyślij mi proszę oryginalny tekst umowy."),
              "Please send me the original text of the contract.")
        check("„Translation:” w tej samej linii zdjęte", translated("Translation: Can you check this PR today?", "Sprawdzisz dziś ten PR?"),
              "Can you check this PR today?")
        check("„Here is the translation:” zdjęte", translated("Here is the translation:\nThanks, I'll check it tomorrow.", "Dzięki, sprawdzę jutro."),
              "Thanks, I'll check it tomorrow.")
        check("„(Translated from Polish)” zdjęte", translated("See you tomorrow!\n(Translated from Polish)", "Do jutra!"), "See you tomorrow!")
        check("„Tłumaczenie:” zdjęte", translated("Tłumaczenie:\nDzięki za szybką poprawkę!", "Thanks for the quick fix!"), "Dzięki za szybką poprawkę!")

        check("język bez linków: angielski z polskim URL-em",
              "\(TextLanguage.detect("The details are here: https://docs.example.com/projekt_x/specyfikacja"))", "english")
        check("kierunek: polski → EN", TranslationTarget.automatic(for: "Sprawdzisz dziś ten PR? @Bartosz").label(), "PL → EN")
        check("kierunek: angielski → PL", TranslationTarget.automatic(for: "Could you check this PR today? @Anna").label(), "EN → PL")

        print("Wydajność i ustawienia")
        // Dostęp zdalny: nazwa z id_token (JWT bez sprawdzania podpisu — tylko do wyświetlenia), base64url.
        let payload = Data(#"{"sub":"u1","preferred_username":"tomek"}"#.utf8).base64URLEncoded
        check("dostęp zdalny: nazwa z id_token", RemoteAccount.userName(fromIDToken: "e30.\(payload).sig") ?? "brak", "tomek")
        check("dostęp zdalny: zły id_token", RemoteAccount.userName(fromIDToken: "zepsuty") ?? "brak", "brak")
        check("dostęp zdalny: base64url tam i z powrotem",
              String(decoding: Data(base64URLEncoded: Data([0xfb, 0xff, 0x01]).base64URLEncoded) ?? Data(), as: UTF8.self)
                == String(decoding: Data([0xfb, 0xff, 0x01]), as: UTF8.self) ? "ok" : "źle", "ok")
        let modes = ["com.microsoft.Outlook": "formal", "com.tinyspeck.slackmacgap": "translate", "x": "bogus"]
        check("tryb aplikacji: ustawiony", Settings.defaultMode(for: "com.microsoft.Outlook", in: modes).rawValue, "formal")
        check("tryb aplikacji: brak wpisu → Popraw", Settings.defaultMode(for: "com.apple.TextEdit", in: modes).rawValue, "fix")
        check("tryb aplikacji: Tłumacz nie zastępuje ⌥⌘G", Settings.defaultMode(for: "com.tinyspeck.slackmacgap", in: modes).rawValue, "fix")
        check("tryb aplikacji: nieznany → Popraw", Settings.defaultMode(for: "x", in: modes).rawValue, "fix")
        check("tryb aplikacji: bez aplikacji → Popraw", Settings.defaultMode(for: nil, in: modes).rawValue, "fix")
        // Identyfikator z logu zdarzeń PowerPointa — z małym „p", inaczej niż Outlook.
        check("PowerPoint bez tekstu w AX", "\(TextBridge.textlessBundleIDs.contains("com.microsoft.Powerpoint"))", "true")
        check("Outlook czytany przez AX", "\(TextBridge.textlessBundleIDs.contains("com.microsoft.Outlook"))", "false")
        let hostile = String(repeating: "a@", count: 3000)
        let started = Date()
        _ = TextLanguage.detect(hostile)
        let seconds = Date().timeIntervalSince(started)
        check("wykrywanie języka na 6000 × „a@” < 0,5 s", seconds < 0.5 ? "ok" : String(format: "%.2f s", seconds), "ok")
        for (name, text) in [("a@", hostile), ("a.", String(repeating: "a.", count: 3000) + "@b"),
                             ("@a", String(repeating: "@a ", count: 2000)), (":a", String(repeating: ":a", count: 3000)),
                             ("http", String(repeating: "https://a", count: 600))] {
            let t0 = Date()
            _ = PostProcess.clean(text + " x", original: text)
            let dt = Date().timeIntervalSince(t0)
            check("post-processing na wrogim tekście „\(name)…” < 1 s", dt < 1 ? "ok" : String(format: "%.2f s", dt), "ok")
        }

        print("Akapit i najkrótsza podmiana (pola natywne)")
        func paragraph(_ text: String, _ caret: Int) -> String {
            (text as NSString).substring(with: TextEdits.paragraphRange(in: text, caret: caret))
        }
        let note = "Pierwszy akapit.\nDrugi akapit z bledem.\n\nTrzeci."
        check("akapit z kursorem w środku", paragraph(note, 20), "Drugi akapit z bledem.")
        check("akapit z kursorem na końcu linii", paragraph(note, 39), "Drugi akapit z bledem.")
        check("pusta linia", paragraph(note, 40), "")
        check("ostatni akapit bez końca linii", paragraph(note, (note as NSString).length), "Trzeci.")
        check("CRLF", paragraph("a\r\nbłąd\r\nc", 4), "błąd")
        func span(_ a: String, _ b: String) -> String {
            guard let s = TextEdits.minimalSpan(from: a, to: b) else { return "brak zmian" }
            return "\(s.prefix)+\(s.oldLength)→\(s.replacement)"
        }
        check("najkrótsza podmiana: polska litera", span("dobry pomysl, ale", "dobry pomysł, ale"), "11+1→ł")
        check("najkrótsza podmiana: dodany przecinek", span("wiem że", "wiem, że"), "4+0→,")
        check("najkrótsza podmiana: usunięcie", span("the the report", "the report"), "4+4→")
        check("najkrótsza podmiana: emoji (UTF-16)", span("🙂 dzieki", "🙂 dzięki"), "6+1→ę")
        check("najkrótsza podmiana: bez zmian", span("ok", "ok"), "brak zmian")

        print("Granica cytowanej korespondencji (Outlook, Mail)")
        func newPart(_ text: String) -> String {
            ReplyBoundary.newMessageRange(in: text).map { (text as NSString).substring(with: $0) } ?? "∅"
        }
        let outlookPL = "Cześć Aniu,\nwysyłam poprawki.\nPozdrawiam\n\nOd: Anna Nowak <anna@example.com>\nWysłano: poniedziałek, 28 września 2026 10:12\nDo: Tomasz\nTemat: Raport\n\nHej, prześlesz raport?"
        check("Outlook PL: Od/Wysłano/Do/Temat", newPart(outlookPL), "Cześć Aniu,\nwysyłam poprawki.\nPozdrawiam")
        let outlookEN = "Hi Tom, see below.\n\n________________________________\nFrom: Tom <tom@x.com>\nSent: Monday\nTo: Me\nSubject: Re: X\n\nOld text"
        check("Outlook EN: linia podkreślników", newPart(outlookEN), "Hi Tom, see below.")
        check("Outlook bez linii: From/Sent/To", newPart("Thanks!\nFrom: Tom\nSent: Monday\nTo: Me\nOld"), "Thanks!")
        check("Original Message", newPart("Ok.\n-----Original Message-----\nOld"), "Ok.")
        check("Wiadomość oryginalna", newPart("Dzięki.\n\n-----Wiadomość oryginalna-----\nStary"), "Dzięki.")
        check("W dniu … pisze:", newPart("Zgoda.\n\nW dniu 28.09.2026 o 10:12 Anna Nowak <a@b.pl> pisze:\n> stary tekst"), "Zgoda.")
        check("On … wrote:", newPart("Sounds good.\n\nOn Mon, Sep 28, 2026 at 10:12 AM Tom <t@x.com> wrote:\n> old"), "Sounds good.")
        check("linie z >", newPart("Odpowiedź.\n> pierwsza\n> druga"), "Odpowiedź.")
        check("bez cytatu", newPart("Zwykła wiadomość.\nOd: poniedziałku pracuję zdalnie."), "∅")
        check("samo „Od:” w treści to nie nagłówek", newPart("Plan:\nOd: 9:00\nDo: 17:00\nPrzerwa o 12."), "∅")
        check("cytat na samym początku", newPart("From: Tom\nSent: Monday\nTo: Me\nOld"), "∅")

        print("Opakowania")
        check("blok kodu na końcu zostaje", clean("Odpal to:\n```npm run build```", "Odpal to:\n```npm run build```"),
              "Odpal to:\n```npm run build```")
        check("kod na początku zostaje", clean("```git pull``` i potem deploy", "```git pull``` i potem deploy"),
              "```git pull``` i potem deploy")
        check("dodane ```markdown zdjęte", clean("```markdown\nPopraw to\n```", "popraw to"), "Popraw to")
        check("zagnieżdżone ``` + <text> zdjęte", clean("```\n<text>\nDzięki!\n</text>\n```", "dzieki!"), "Dzięki!")
        check("dodane cudzysłowy zdjęte", clean("\"Myślę, że tak.\"", "mysle ze tak."), "Myślę, że tak.")
        check("```inline``` bez znacznika języka", clean("```Popraw to```", "popraw to"), "Popraw to")
        check("```Dzięki``` z polską literą", clean("```Dzięki, jasne.```", "dzieki, jasne."), "Dzięki, jasne.")
        check("blok kodu autora + opakowanie modelu", clean("```\nOdpal to:\n```npm run build```\n```", "Odpal to:\n```npm run build```"),
              "Odpal to:\n```npm run build```")
        check("cudzysłów autora w innym stylu zostaje", clean("„Zrobię to jutro”", "\"zrobie to jutro\""), "\"Zrobię to jutro\"")
        check("cytaty w środku zostają", clean("\"a\" i \"b\"", "\"a\" i \"b\""), "\"a\" i \"b\"")

        print("Nietykalne fragmenty")
        check("powtórzone emoji bez spacji", clean("Brawo zespół :fire::fire::fire:", "Brawo zespół :fire::fire::fire:"),
              "Brawo zespół :fire::fire::fire:")
        check("sklejone emoji rozklejone", clean("deploy na staging:rocket:", "deploy na staging :rocket:"),
              "deploy na staging :rocket:")
        check("dopisany dwukropek przed emoji usunięty", clean("deploy na staging: :rocket:", "deploy na staging :rocket:"),
              "deploy na staging :rocket:")
        check("dwukropek autora przed emoji zostaje", clean("Wynik: :tada:", "Wynik: :tada:"), "Wynik: :tada:")
        check("zamykający dwukropek emoji zostaje (sklejone)", clean("Brawo :fire::tada:", "brawo :fire: :tada:"), "Brawo :fire: :tada:")
        check("zamykający dwukropek emoji zostaje (spacja)", clean("Brawo :fire: :tada:", "Brawo :fire:  :tada:"), "Brawo :fire: :tada:")
        check("trzy emoji sklejone przez model", clean("Brawo :fire::fire::fire:", "brawo :fire: :fire: :fire:"), "Brawo :fire: :fire: :fire:")
        check("dwukropek po godzinie przed emoji", clean("Standup o 9:30: :coffee:", "standup o 9:30 :coffee:"), "Standup o 9:30 :coffee:")
        check("emoji Unicode zostają", clean("Działa to całkiem sprawnie 🙂", "dziala to calkiem sprawnie 🙂"), "Działa to całkiem sprawnie 🙂")
        check("podmienione emoji wraca", clean("Przepraszam za opóźnienie, wysyłam ofertę 😊", "sorki za opoznienie, wysylam oferte 🙂"),
              "Przepraszam za opóźnienie, wysyłam ofertę 🙂")
        check("emotikon zamiast emoji wraca", clean("Sorki za opóźnienie ;)", "sorki za opoznienie 🙂"), "Sorki za opóźnienie 🙂")
        check("emoji dodane przez model zostaje, gdy oryginał nie miał", clean("Brawo! 🎉", "brawo!"), "Brawo! 🎉")
        check("symbol zastępczy podpisu zdjęty", clean("Hello John,\n\nThe report is ready.\n\nBest regards,\n[Your name]", "hi john report ready"),
              "Hello John,\n\nThe report is ready.\n\nBest regards,")
        check("nawias kwadratowy autora zostaje", clean("[WIP] Poprawka logowania", "[WIP] poprawka logowania"), "[WIP] Poprawka logowania")
        check("e-mail nietknięty", clean("@dev sprawdźcie logi, pisz na admin@dev.example.com",
                                         "@dev sprawdźcie logi, pisz na admin@dev.example.com"),
              "@dev sprawdźcie logi, pisz na admin@dev.example.com")
        check("URL z @ nietknięty", clean("@jan zobacz https://youtube.com/@jan/videos", "@jan zobacz https://youtube.com/@jan/videos"),
              "@jan zobacz https://youtube.com/@jan/videos")
        check("(@jan) nietknięte", clean("Pytanie do @jan (@jan wie)", "Pytanie do @jan (@jan wie)"), "Pytanie do @jan (@jan wie)")
        check("zgubione @ wraca", clean("jan, napisz do jan@example.com", "@jan napisz do jan@example.com"),
              "@jan, napisz do jan@example.com")
        check("zgubione @ nie trafia do URL-a", clean("devops, sprawdźcie https://devops.example.com/status",
                                                      "@devops sprawdźcie https://devops.example.com/status"),
              "@devops, sprawdźcie https://devops.example.com/status")
        check("zgubione @ nie trafia do #kanału", clean("ola, sprawdź #ola-team", "@ola sprawdź #ola-team"),
              "@ola, sprawdź #ola-team")
        check("@ nie trafia do URL-a z imieniem", clean("https://example.com/profil?user=jan — jan, zobacz", "https://example.com/profil?user=jan — @jan zobacz"),
              "https://example.com/profil?user=jan — @jan, zobacz")
        check("@ wraca we właściwe miejsce", clean("Jak pisał jan, jan to sprawdzi.", "jak pisal jan @jan to sprawdzi"),
              "Jak pisał jan, @jan to sprawdzi.")
        check("@Łukasz. na końcu zdania", clean("Dzięki, Łukasz.", "Dzięki @Łukasz."), "Dzięki, @Łukasz.")
        check("URL w < > rozpakowany", clean("Link: < https://gitlab.com/x/-/merge_requests/12>", "Link: https://gitlab.com/x/-/merge_requests/12"),
              "Link: https://gitlab.com/x/-/merge_requests/12")
        check("escapowany URL naprawiony", clean("Link: https://gitlab.com/x/-/merge\\_requests/12", "Link: https://gitlab.com/x/-/merge_requests/12"),
              "Link: https://gitlab.com/x/-/merge_requests/12")
        check("typograficzne apostrofy → proste", clean("It’s fine, let’s go.", "Its fine, lets go."), "It's fine, let's go.")
        check("białe znaki z brzegów zachowane", clean("Dzięki!", "  dzieki!\n"), "  Dzięki!\n")

        print("Uzasadnienia zmian")
        func reasons(_ a: String, _ b: String) -> String {
            Diff.changes(Diff.segments(from: a, to: b), original: a).map { "\($0.from)→\($0.to): \($0.reason)" }
                .joined(separator: " | ")
        }
        check("dodana spacja", reasons("ok,dzięki", "ok, dzięki"), "→: Odstęp")
        check("pisownia rozdzielna", reasons("nikogo niebyło", "nikogo nie było"), "niebyło→nie było: Pisownia łączna / rozdzielna")
        check("polskie znaki", reasons("w piatek", "w piątek"), "piatek→piątek: Polskie znaki")
        check("znaki + przecinek", reasons("maila ktory", "maila, który"), "ktory→, który: Polskie znaki i interpunkcja")
        check("pisownia łączna + polskie znaki", reasons("wogole nie", "w ogóle nie"), "wogole→w ogóle: Polskie znaki i pisownia łączna / rozdzielna")
        check("brakujący przecinek", reasons("wiem że", "wiem, że"), "→,: Brakujący znak interpunkcyjny")

        print("Sprawdzanie na żywo")
        func languages(_ text: String) -> String {
            LiveText.segments(text).map { $0.language == .english ? "EN" : "PL" }.joined(separator: " ")
        }
        check("zdania według języka", languages("Dzięki za PR. I will check it tomorrow morning. Zrobię to jutro."), "PL EN PL")
        check("krótkie zdanie dostaje język tekstu", languages("Zrobiłem przegląd kodu wczoraj wieczorem. Ok, thx!"), "PL PL")
        check("polska litera przesądza", languages("Deploy na staging jest gotowy, sprawdź."), "PL")
        let masked = LiveText.blank("a😀b\nc", IndexSet(integersIn: 0..<3))
        check("maska zachowuje długość UTF-16 i końce linii", "\((masked as NSString).length) \(masked.debugDescription)", "6 \"   b\\nc\"")
        let live = LiveText("Cześć @jan, dzięki. I will check it tomorrow morning.")
        check("wzmianka zamaskowana w obu wersjach", "\(live.polishMask.contains(7)) \(live.englishMask.contains(7))", "true true")
        check("angielskie zdanie zamaskowane w polskiej wersji", live.polish.trimmingCharacters(in: .whitespaces), "Cześć     , dzięki.")
        func issue(_ location: Int, _ length: Int, _ source: LiveIssue.Source = .spelling) -> LiveIssue {
            LiveIssue(range: NSRange(location: location, length: length), word: "", message: "", suggestions: [], source: source, rule: "")
        }
        func ranges(_ issues: [LiveIssue]) -> String { issues.map { "\($0.range.location)+\($0.range.length)" }.joined(separator: " ") }
        // „ala ma kota ktory” → wpisane „ł” w środku: błąd przed zostaje, dotknięty znika, dalszy się przesuwa.
        check("przesunięcie błędów po edycji",
              ranges(LiveText.shift([issue(0, 3), issue(4, 2), issue(12, 5)], from: "ala ma kota ktory", to: "ala mał kota ktory")),
              "0+3 13+5")
        check("przesunięcie bez zmian", ranges(LiveText.shift([issue(0, 3)], from: "abc", to: "abc")), "0+3")
        check("ten sam błąd z dwóch źródeł: LanguageTool wygrywa ze słownikiem",
              LiveText.merge([issue(0, 6, .spelling), issue(10, 2, .spelling)], [issue(0, 6, .languageTool)]).map(\.source.rawValue).joined(separator: " "),
              "LanguageTool słownik")
        check("początek zdania", "\(LiveChecker.isSentenceStart(0, in: "Ala")) \(LiveChecker.isSentenceStart(4, in: "Tak. Ala")) \(LiveChecker.isSentenceStart(4, in: "tak Ala"))",
              "true true false")
        let plausibleText = "Zrobię deploy dla PR od Tomek, dzieki"
        let plausible = MainActor.assumeIsolated {
            ["deploy", "PR", "Tomek", "dzieki"].map { word in
                "\(word)=\(LiveChecker.shared.isPlausible(word, at: (plausibleText as NSString).range(of: word).location, in: plausibleText))"
            }.joined(separator: " ")
        }
        func diacritics(_ text: String) -> String {
            LiveChecker.missingDiacritics(text, original: text).map { "\($0.word)→\($0.suggestions[0])" }.joined(separator: " ")
        }
        check("zgubione polskie litery w istniejących słowach", diacritics("Cos tez tam jest, wiec zle nie bede"),
              "Cos→Coś tez→też wiec→więc zle→źle bede→będę")
        check("„ze” → „że”, ale nie przed przyimkiem", diacritics("wiem ze to jest ze mną i ze szkoły, ze względu na to"),
              "ze→że")
        check("„ze” na końcu pisanego zdania jeszcze nie jest błędem", diacritics("wiem ze"), "")
        check("błąd pisowni nie znika pod błędem przecinka",
              LiveText.merge([issue(0, 6, .spelling)], [issue(0, 10, .languageTool)]).map(\.source.rawValue).joined(separator: " "),
              "słownik LanguageTool")
        func fromBielik(_ a: String, _ b: String) -> String {
            LiveChecker.issues(from: a, corrected: b).map { "\($0.word)→\($0.suggestions.first ?? "")" }.joined(separator: " | ")
        }
        check("poprawka Bielika jako błędy", fromBielik("testuje cos, czego hyba nie ma", "Testuję coś, czego chyba nie ma"),
              "testuje→Testuję | cos→coś | hyba→chyba")
        check("dodany przecinek przypięty do słowa przed nim", fromBielik("wiem że działa", "wiem, że działa"), "wiem→wiem,")
        check("bez zmian — bez błędów", fromBielik("Wszystko gra.", "Wszystko gra."), "")
        let ze = LiveIssue(range: NSRange(location: 5, length: 2), word: "ze", message: "", suggestions: [], source: .spelling, rule: "PL_DIACRITICS")
        check("„ze” z listy ustępuje „ze to” z LanguageTool",
              LiveText.merge([ze], [issue(5, 5, .languageTool)]).map(\.source.rawValue).joined(separator: " "), "LanguageTool")
        check("słowo po słowie w zakresie błędu", TextGeometry.words(in: NSRange(location: 0, length: 11), of: "dziala  ale" as NSString)
              .map { "\($0.location)+\($0.length)" }.joined(separator: " "), "0+6 8+3")
        check("sugestia z polskimi literami pierwsza", LiveChecker.ranked(["Dziki", "Dzięki", "Dzieli"], for: "Dzieki").joined(separator: " "),
              "Dzięki Dziki Dzieli")
        check("wielka litera jak w oryginale", LiveChecker.ranked(["część", "cześć"], for: "Czesc").joined(separator: " "), "Część Cześć")
        func locate(_ word: String, _ near: Int, _ text: String) -> String {
            LiveMonitor.locate(word, near: near, in: text as NSString).map { "\($0.location)" } ?? "brak"
        }
        check("słowo przesunięte po wcześniejszej poprawce", locate("cos", 9, "Testuję cos, czego"), "8")
        check("najbliższe z kilku wystąpień", locate("ze", 22, "wiem ze to, a potem ze tamto ze"), "20")
        check("nie fragment dłuższego słowa", locate("cos", 0, "cosinus i coś"), "brak")
        check("„ze to” → „że to”", LiveChecker.restoringDiacritics("ze to") ?? "brak", "że to")
        check("„ze mną” zostaje", LiveChecker.restoringDiacritics("ze mną") ?? "brak", "brak")
        check("słowa, których nie zgłaszamy", plausible, "deploy=true PR=true Tomek=true dzieki=false")

        print("Model: nazwa")
        check("nazwa: Bielik", Settings.displayName(ofModel: "hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF:Q4_K_M"), "Bielik-11B-v2.3")
        check("nazwa: Gemma 4", Settings.displayName(ofModel: "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest"), "Gemma-4-12B")
        check("nazwa: rozmiar z biblioteki Ollamy", Settings.displayName(ofModel: "qwen3:8b"), "Qwen3:8b")
        check("nazwa: wariant w tagu znika, rozmiar zostaje", Settings.displayName(ofModel: "gemma3:12b-it-qat"), "Gemma3:12b")
        check("nazwa: sama kwantyzacja w tagu znika", Settings.displayName(ofModel: "llama3.1:Q4_K_M"), "Llama3.1")
        check("nazwa: własny import za rozmiarem znika", Settings.displayName(ofModel: "gemma4-e4b-gramadrama:latest"), "Gemma4-E4B")
        check("nazwa: wersja za rozmiarem zostaje", Settings.displayName(ofModel: "mistral-7b-v0.3:latest"), "Mistral-7b-v0.3")

        print("Dyktowanie: klawisz")
        func hold(_ delay: TimeInterval, _ events: [HoldKeyMachine.Event]) -> String {
            var machine = HoldKeyMachine(armDelay: delay)
            return events.map { "\(machine.handle($0))" }.joined(separator: " ")
        }
        check("Fn: trzymaj i puść", hold(0, [.keyDown, .keyUp]), "start stop")
        check("Fn+← to skrót, nie dyktowanie", hold(0, [.keyDown, .otherKey, .keyUp]), "start cancel none")
        check("prawy ⌥ + a (ą) nie włącza mikrofonu", hold(0.3, [.keyDown, .otherKey, .armElapsed, .keyUp]),
              "scheduleArm none none none")
        check("prawy ⌥ trzymany dłużej", hold(0.3, [.keyDown, .armElapsed, .keyUp]), "scheduleArm start stop")
        check("krótkie stuknięcie prawym ⌥", hold(0.3, [.keyDown, .keyUp, .armElapsed]), "scheduleArm none none")
        check("powtórzone keyDown w trakcie", hold(0, [.keyDown, .keyDown, .keyUp]), "start none stop")
        check("⇧ w trakcie: drugi język, nie anulowanie", hold(0, [.keyDown, .shift, .shift, .keyUp]), "start alternate none stop")
        check("prawy ⌥ + ⇧ przed startem", hold(0.3, [.keyDown, .shift, .armElapsed, .keyUp]), "scheduleArm none start stop")
        check("⇧⌥a (Ą) nie włącza mikrofonu", hold(0.3, [.keyDown, .shift, .otherKey, .armElapsed]), "scheduleArm none none none")
        do {
            var machine = HoldKeyMachine(armDelay: 0)
            _ = [.keyDown, .shift, .keyUp, .keyDown].map { machine.handle($0) }
            check("następne dyktowanie znowu w języku z ustawień", machine.alternate ? "drugi" : "z ustawień", "z ustawień")
        }
        check("WER: interpunkcja i wielkość liter nie liczą się",
              "\(DictationBench.wordErrorRate(reference: "Dzięki, super.", hypothesis: "dzięki super!"))", "0.0")
        check("WER: jedno słowo z czterech", "\(DictationBench.wordErrorRate(reference: "daj znać do jutra", hypothesis: "daj znać od jutra"))", "0.25")
        // Te same przypadki co `test_dictation.py` na Linuksie (`alternate_language`, `effective_language`).
        check("⇧ przy polskim = angielski", DictationLanguage.polish.alternate(in: .standard).rawValue, "en")
        check("⇧ przy angielskim = polski", DictationLanguage.english.alternate(in: .standard).rawValue, "pl")
        check("⇧ przy automatycznym = angielski", DictationLanguage.automatic.alternate(in: .standard).rawValue, "en")
        let germanEnglish = TextLanguages(primary: .german, secondary: .english)
        check("DE+EN: ⇧ w obie strony i automatycznie",
              [DictationLanguage.german, .english, .automatic].map { $0.alternate(in: germanEnglish).rawValue }.joined(separator: " "), "en de en")
        check("DE+EN: polski z ustawień to główny", DictationLanguage.polish.effective(in: germanEnglish).rawValue, "de")
        for language in [TextLanguage.english, .german] {
            let single = TextLanguages(primary: language, secondary: nil)
            check("jeden język (\(language.code)): wymuszony, ⇧ nic nie zmienia",
                  DictationLanguage.allCases.map { "\($0.effective(in: single).rawValue)\($0.alternate(in: single).rawValue)" }.joined(separator: " "),
                  Array(repeating: language.code + language.code, count: DictationLanguage.allCases.count).joined(separator: " "))
        }
        check("podpowiedź Whispera po niemiecku", SpeechModel.prompt(vocabulary: ["GramaDrama"], language: .german),
              "Ich spreche Deutsch und verwende manchmal diese Begriffe: GramaDrama, deploy, staging, PR, merge, code review, feedback, release, Jira, Slack.")

        print("Dyktowanie: tekst")
        func spaced(_ previous: Character?, _ text: String) -> String {
            TextEdits.leadingSpaceNeeded(before: text, after: previous) ? " " + text : text
        }
        check("spacja po kropce", spaced(".", "Kolejne zdanie."), " Kolejne zdanie.")
        check("spacja po słowie", spaced("o", "jutro"), " jutro")
        check("bez spacji na początku pola", spaced(nil, "Hej"), "Hej")
        check("bez spacji po spacji", spaced(" ", "Hej"), "Hej")
        check("bez spacji po nowej linii", spaced("\n", "Hej"), "Hej")
        check("bez spacji po @", spaced("@", "Kasia"), "Kasia")
        check("bez spacji przed przecinkiem", spaced("a", ", dzięki"), ", dzięki")
        check("słownik: przecinki, linie, powtórzenia",
              DictationSettings.parseVocabulary("GramaDrama, Bielik\nOllama,, bielik ; staging ").joined(separator: "|"),
              "GramaDrama|Bielik|Ollama|staging")
        check("podpowiedź: własne słowa + żargon bez powtórzeń",
              SpeechModel.prompt(vocabulary: ["Bielik", "PR"], language: .polish),
              "Mówię po polsku, czasem wtrącam słowa: Bielik, PR, deploy, staging, merge, code review, feedback, release, Jira, Slack.")
        check("podpowiedź automatyczna: sama lista", SpeechModel.prompt(vocabulary: ["Bielik"], language: .automatic).hasPrefix("Bielik, deploy") ? "tak" : "nie", "tak")
        check("angielski wynik przy polskim = tłumaczenie", SpeechModel.looksTranslated("I noticed a mistake in the dictation.") ? "tak" : "nie", "tak")
        check("polski z wtrąceniem to nie tłumaczenie", SpeechModel.looksTranslated("Zrobiłem deploy na staging, sprawdź PR.") ? "tak" : "nie", "nie")
        check("krótkie „OK, thanks” to nie tłumaczenie", SpeechModel.looksTranslated("OK, thanks!") ? "tak" : "nie", "nie")
        check("podpowiedź po angielsku: tylko własne słowa", SpeechModel.prompt(vocabulary: ["Bielik"], language: .english), "Bielik")
        check("halucynacja z ciszy", TranscriptFilter.clean("Napisy stworzone przez społeczność Amara.org", prompt: ""), "")
        check("„Dzięki za obejrzenie!” z ciszy", TranscriptFilter.clean(" Dzięki za obejrzenie! ", prompt: ""), "")
        check("krótkie „Dziękuję.” zostaje", TranscriptFilter.clean("Dziękuję.", prompt: ""), "Dziękuję.")
        check("adnotacje w nawiasach znikają", TranscriptFilter.clean("[MUZYKA] Hej, co słychać? (śmiech)", prompt: ""), "Hej, co słychać?")
        check("sama podpowiedź zamiast mowy", TranscriptFilter.clean("Bielik, PR, deploy.", prompt: "Bielik, PR, deploy"), "")
        check("wypełniacz w środku", DictationTidy.apply("No więc, eee, myślę, że tak."), "No więc, myślę, że tak.")
        check("wypełniacz na początku zdania", DictationTidy.apply("Dobra. Eee, zrobię to jutro."), "Dobra. Zrobię to jutro.")
        check("wypełniacz przed kropką", DictationTidy.apply("Zrobię to hmm."), "Zrobię to.")
        check("same wypełniacze → nic", DictationTidy.apply("Yyy, hmm."), "")
        check("„umowa”, „erm…” to nie wypełniacze", DictationTidy.apply("Umowa jest w termie."), "Umowa jest w termie.")
        check("godzina z kropką", DictationTidy.apply("Spotkanie o 10.00, potem od 14.30 do 15.00."),
              "Spotkanie o 10:00, potem od 14:30 do 15:00.")
        check("cena i wersja zostają", DictationTidy.apply("Kosztuje po 2.50 zł, wersja 1.10 działa."),
              "Kosztuje po 2.50 zł, wersja 1.10 działa.")
        func needs(_ text: String) -> String { Prompts.dictationNeedsCleanup(text) ? "Bielik" : "od razu" }
        check("czysty tekst: bez Bielika", needs("Hej, zrobiłem deploy na staging, możesz sprawdzić ten PR do jutra?"), "od razu")
        check("czysty angielski: bez Bielika", needs("Thanks for the update. I will review the pull request tomorrow."), "od razu")
        check("„yyy”: Bielik", needs("No i yyy zrobiłem to"), "Bielik")
        check("„eee,”: Bielik", needs("Myślę, eee, że tak."), "Bielik")
        check("„znaczy nie”: Bielik", needs("Na środę, znaczy nie, na czwartek."), "Bielik")
        check("„sorry”: Bielik", needs("At five, sorry, at six."), "Bielik")
        check("powtórzone słowo: Bielik", needs("Myślę, że że to działa."), "Bielik")
        check("„to totalnie” to nie powtórzenie", needs("To totalnie działa."), "od razu")
        check("„albo nie, cofnijmy to”: Bielik",
              needs("Myślałem, że zrobi to Andrzej, albo nie, cofnijmy to, myślałem, że zrobi to Tomek."), "Bielik")
        check("zdanie od nowa bez słowa-klucza: Bielik",
              needs("Myślałem, że zrobi to Andrzej. Myślałem, że zrobi to Tomek."), "Bielik")
        check("„a właściwie”: Bielik", needs("Będę o piątej, a właściwie o szóstej."), "Bielik")
        check("„cofam”: Bielik", needs("Wyślę jutro, cofam, wyślę dziś."), "Bielik")
        check("„actually”: Bielik", needs("Let's meet on Monday, actually Tuesday."), "Bielik")
        check("„w tym momencie” to nie „moment”", needs("W tym momencie nie mogę rozmawiać."), "od razu")
        check("„przemyślenie” to nie „mm”", needs("Dzięki za przemyślenie tematu."), "od razu")
        func safe(_ cleaned: String, _ raw: String) -> String {
            Prompts.dictationCleanupLooksSafe(cleaned, transcript: raw) ? "wklej" : "surowy"
        }
        check("porządkowanie: wypełniacze", safe("Zrobiłem deploy, możesz sprawdzić?", "yyy zrobiłem eee deploy możesz sprawdzić"), "wklej")
        check("porządkowanie: autokorekta mówcy", safe("Spotkanie przesuwamy na czwartek o 10:00.",
                                                     "Spotkanie przesuwamy na środę, znaczy nie, na czwartek o dziesiątej."), "wklej")
        check("porządkowanie: odpowiedź zamiast pytania", safe("Paryż.", "jaka jest stolica francji"), "surowy")
        check("porządkowanie: poprawka wycina większość zdania",
              safe("Myślałem, że zrobi to Tomek.", "Myślałem że zrobi to Andrzej albo nie cofnijmy to myślałem że zrobi to Tomek"), "wklej")
        check("porządkowanie: dopisana treść",
              safe("Cześć Janku, w nawiązaniu do rozmowy przesyłam ofertę, którą przygotowaliśmy dla Waszej firmy w tym tygodniu.",
                   "napisz maila do janka"), "surowy")

        print(failures == 0 ? "\nWszystko OK" : "\nBłędy: \(failures)")
        if let dumpPath {
            let document: [String: Any] = [
                "description": "Przypadki PostProcess.clean(output, original, strict) z macOS SelfTest.swift. Generowane: GramaDrama --selftest --dump-cases. Nie edytować ręcznie.",
                "cases": recorded,
            ]
            if let data = try? JSONSerialization.data(withJSONObject: document, options: [.prettyPrinted, .sortedKeys]) {
                try? data.write(to: URL(fileURLWithPath: dumpPath))
                print("Zapisano \(recorded.count) przypadków do \(dumpPath)")
            }
        }
        return failures == 0 ? 0 : 1
    }
}
