import Foundation
import Testing
@testable import GramaCore

@Suite("Prompty z plików")
struct PromptFilesTests {
    private func folder() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("gramadrama-prompts-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func write(_ text: String, _ file: String, in folder: URL) throws {
        try Data(text.utf8).write(to: folder.appendingPathComponent(file))
    }

    /// Wyeksportowane domyślne pliki nic nie zmieniają: to nie są „własne” fragmenty.
    @Test func exportedDefaultsAreNoOverrides() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        let written = try PromptFiles.export(to: dir)
        #expect(written.count == PromptFiles.pieces.count)
        let files = PromptFiles.load(from: dir)
        #expect(files.overridden.isEmpty)
        #expect(files.problems.isEmpty)
        #expect(try PromptFiles.export(to: dir).isEmpty)
    }

    @Test func anEditedFileReplacesItsPieceOnly() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try write("Fix only typos.\n", "task-fix.txt", in: dir)
        let files = PromptFiles.load(from: dir)
        #expect(files.overridden == ["task-fix.txt"])
        let system = Prompts.messages(for: "Ala ma kota", mode: .fix, prompts: files)[0]["content"] ?? ""
        #expect(system.contains("\nTask: Fix only typos.\nRules:"))
        #expect(Prompts.messages(for: "Ala ma kota", mode: .improve, prompts: files)
                == Prompts.messages(for: "Ala ma kota", mode: .improve, prompts: .defaults))
    }

    @Test func aBrokenFileIsSkippedWithItsReason() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try write("Language: {language}", "user.txt", in: dir)
        try write("   \n", "intro.txt", in: dir)
        try write("[{\"text\": \"a\"}]", "examples.json", in: dir)
        try write("{", "names.json", in: dir)
        try Data([0xFF, 0xFE]).write(to: dir.appendingPathComponent("rules.txt"))
        let files = PromptFiles.load(from: dir)
        #expect(files.overridden.isEmpty)
        let reasons = Dictionary(uniqueKeysWithValues: files.problems.map { ($0.file, $0.reason) })
        #expect(reasons["user.txt"] == "brakuje {text}")
        #expect(reasons["intro.txt"] == "plik jest pusty")
        #expect(reasons["examples.json"] == "przykład 1: potrzebne pola tekstowe text, language, output")
        #expect(reasons["names.json"] == "to nie jest poprawny JSON")
        #expect(reasons["rules.txt"] == "plik nie jest zapisany w UTF-8")
        #expect(Prompts.messages(for: "Ala ma kota", mode: .fix, prompts: files)
                == Prompts.messages(for: "Ala ma kota", mode: .fix, prompts: .defaults))
    }

    /// `{text}` wpisany przez użytkownika zostaje tekstem — tekst podstawiamy na końcu.
    @Test func placeholdersInTheUsersTextStay() {
        let message = Prompts.messages(for: "Napisz {language} i {target}", mode: .fix, prompts: .defaults).last?["content"] ?? ""
        #expect(message.contains("<text>\nNapisz {language} i {target}\n</text>"))
    }

    @Test func dictationUsesItsPieces() throws {
        let dir = try folder()
        defer { try? FileManager.default.removeItem(at: dir) }
        try write("[{\"text\": \"yyy dobra\", \"output\": \"Dobra.\"}]", "cleanup-examples.json", in: dir)
        let files = PromptFiles.load(from: dir)
        let messages = Prompts.dictationMessages(for: "no to tak", vocabulary: ["GramaDrama"], prompts: files)
        #expect(messages.count == 4)
        #expect(messages[0]["content"]?.hasSuffix("\n- Spell these names and terms exactly like this: GramaDrama.") == true)
        #expect(messages[2]["content"] == "Dobra.")
    }
}
