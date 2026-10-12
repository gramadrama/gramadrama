import Testing
@testable import GramaCore

@Suite("Skróty globalne")
struct GlobalShortcutTests {
    @Test func defaultsAndLabels() {
        #expect(GlobalShortcut.defaultCorrection.label == "⌥⌘G")
        #expect(GlobalShortcut.defaultTranslation.keys == ["⌥", "⌘", "T"])
        let all = GlobalShortcut(keyCode: 40, modifiers: [.command, .shift, .option, .control], letter: "k")
        #expect(all.label == "⌃⌥⇧⌘K")  // kolejność jak w menu macOS
    }

    @Test func rawValueRoundTrip() {
        let shortcut = GlobalShortcut(keyCode: 16, modifiers: [.control, .option], letter: "z")  // QWERTZ: klawisz Y
        #expect(shortcut.rawValue == "⌃⌥Z@16")
        #expect(GlobalShortcut(rawValue: shortcut.rawValue) == shortcut)
        for broken in ["", "⌥⌘G", "⌥⌘G@", "⌥⌘G@x", "⌥⌘G@500", "⌥⌘GG@5", "⌘G@5", "⌥G@5", "⌥⌘1@18", "⌥⌘ż@5"] {
            #expect(GlobalShortcut(rawValue: broken) == nil, "\(broken)")
        }
    }

    /// Nie przechwytuje pisania ani skrótów edycji: co najmniej dwa z ⌃ ⌥ ⌘, litera A–Z.
    @Test func onlySafeCombinations() {
        for allowed: GlobalShortcut.Modifiers in [[.option, .command], [.control, .command], [.control, .option],
                                                   [.control, .option, .command], [.shift, .option, .command]] {
            #expect(GlobalShortcut.problem(modifiers: allowed, letter: "k") == nil)
        }
        for refused: GlobalShortcut.Modifiers in [[], [.command], [.option], [.control], [.shift, .command], [.shift, .option],
                                                   [.shift, .control], [.shift]] {
            #expect(GlobalShortcut.problem(modifiers: refused, letter: "k") == GlobalShortcut.rule)
        }
        for letter in ["1", "ż", "", "kk", " ", "/"] {
            #expect(GlobalShortcut.problem(modifiers: [.option, .command], letter: letter) == GlobalShortcut.rule, "\(letter)")
        }
        #expect(GlobalShortcut.problem(modifiers: [.option, .command], letter: "K") == nil)
        #expect(GlobalShortcut.problem(modifiers: [.control, .command], letter: "q") == "⌃⌘Q ma już macOS (Zablokuj ekran) — wybierz inny.")
    }

    /// Uszkodzony zapis albo dwa takie same skróty — oba domyślne, jak `resolve` na Linuksie.
    @Test func resolveFallsBackToBothDefaults() {
        let custom = GlobalShortcut(keyCode: 40, modifiers: [.control, .option], letter: "k")
        #expect(GlobalShortcut.resolve(correction: nil, translation: nil) == (.defaultCorrection, .defaultTranslation))
        #expect(GlobalShortcut.resolve(correction: custom.rawValue, translation: nil) == (custom, .defaultTranslation))
        #expect(GlobalShortcut.resolve(correction: "zepsute", translation: custom.rawValue) == (.defaultCorrection, .defaultTranslation))
        #expect(GlobalShortcut.resolve(correction: nil, translation: GlobalShortcut.defaultCorrection.rawValue)
                == (.defaultCorrection, .defaultTranslation))
        // Zamienione skróty to dalej dwa różne.
        #expect(GlobalShortcut.resolve(correction: GlobalShortcut.defaultTranslation.rawValue, translation: GlobalShortcut.defaultCorrection.rawValue)
                == (.defaultTranslation, .defaultCorrection))
    }

    @Test func sameKeysIgnoresTheDisplayedLetter() {
        #expect(GlobalShortcut(keyCode: 5, modifiers: [.option, .command], letter: "x").sameKeys(as: .defaultCorrection))
        #expect(!GlobalShortcut(keyCode: 5, modifiers: [.control, .command], letter: "g").sameKeys(as: .defaultCorrection))
    }
}
