import Foundation
import GramaIMEProtocol

/// Co poprawić, gdy tekst przyszedł z metody wprowadzania: zaznaczenie, nowa wiadomość (bez cytatów),
/// akapit z kursorem albo całe pole. Aplikacja oddaje tylko okno wokół kursora, więc granicę akapitu
/// uznajemy wyłącznie wtedy, gdy ją widać (znak końca linii w oknie, początek dokumentu, koniec dokumentu).
package enum ImeScope: String, Equatable, Sendable {
    case selection, newMessage, paragraph, wholeField

    package enum Refusal: Error, Equatable {
        /// IME nie widzi tekstu albo odpowiedź jest niespójna — spróbuj zwykłej drogi (AX, schowek).
        case noText
        /// Akapit sięga poza okno, które oddała aplikacja — spróbuj zwykłej drogi.
        case cut
        /// Każdy fragment zawiera obraz albo emoji (U+FFFC) — spróbuj zwykłej drogi.
        case attachments
        /// Kursor w pustej linii obok tekstu — to odpowiedź dla użytkownika, nie powód do innej drogi.
        case emptyParagraph
        /// Zaznaczenie dłuższe niż limit (liczba znaków UTF-16).
        case tooLong(Int)
    }

    package struct Candidate: Equatable, Sendable {
        /// Zakres w dokumencie (UTF-16).
        package let range: NSRange
        package let scope: ImeScope
        package let text: String
    }

    /// Fragmenty do poprawy, od najlepszego. Zawierające U+FFFC są pomijane (podmiana skasowałaby obraz),
    /// poza zaznaczeniem — tam decyduje reguła panelu (tylko „Kopiuj”).
    package static func candidates(_ reply: ImeProtocol.Reply, wholeFieldWhenComplete: Bool) throws -> [Candidate] {
        guard let selection = reply.selectionLocation, let selectionLength = reply.selectionLength,
              selection >= 0, selectionLength >= 0, selection != NSNotFound, let start = reply.windowStart, start >= 0
        else { throw Refusal.noText }
        if reply.selectionTooLong == true { throw Refusal.tooLong(selectionLength) }
        let before = reply.before ?? "", selected = reply.selected ?? "", after = reply.after ?? ""
        let caret = (before as NSString).length
        guard start + caret == selection else { throw Refusal.noText }

        if selectionLength > 0 {
            guard (selected as NSString).length == selectionLength, !selected.isBlank else { throw Refusal.noText }
            return [Candidate(range: NSRange(location: selection, length: selectionLength), scope: .selection, text: selected)]
        }

        let window = before + after
        guard !window.isBlank else { throw Refusal.noText }
        let ns = window as NSString
        let fromStart = start == 0
        let toEnd = reply.shortAfter == true
        var found: [Candidate] = []
        func add(_ local: NSRange, _ scope: ImeScope) {
            let range = NSRange(location: start + local.location, length: local.length)
            guard local.length > 0, !found.contains(where: { $0.range == range }) else { return }
            found.append(Candidate(range: range, scope: scope, text: ns.substring(with: local)))
        }

        // Odpowiedź w mailu: nowa wiadomość od początku do cytatu (granicę wyznacza cytat, nie koniec pola).
        if fromStart, let message = ReplyBoundary.newMessageRange(in: window), caret <= message.location + message.length {
            add(message, .newMessage)
        } else if fromStart, toEnd, wholeFieldWhenComplete {
            add(NSRange(location: 0, length: ns.length), .wholeField)
        }

        let paragraph = TextEdits.paragraphRange(in: window, caret: caret)
        if ns.substring(with: paragraph).isBlank {
            if found.isEmpty { throw Refusal.emptyParagraph }
        } else {
            // Granica widoczna: znak końca linii w oknie albo brzeg dokumentu.
            let startVisible = paragraph.location > 0 || fromStart
            let endVisible = paragraph.location + paragraph.length < ns.length || toEnd
            if startVisible, endVisible {
                let whole = fromStart && toEnd && paragraph.location == 0 && paragraph.length == ns.length
                add(paragraph, whole ? .wholeField : .paragraph)
            }
        }

        let clean = found.filter { !$0.text.contains("\u{FFFC}") }
        if clean.isEmpty { throw found.isEmpty ? Refusal.cut : Refusal.attachments }
        return clean
    }
}

/// Podmiany dla metody wprowadzania: słowami (z `Diff`), od końca fragmentu, z separatorami linii oryginału.
/// Krótkie podmiany zostawiają nietknięte formatowanie, linki i wzmianki między poprawkami — jeden długi
/// fragment wstawiony przez `insertText` przejąłby atrybuty pierwszego znaku.
package enum ImeEdits {
    package enum Failure: Error, Equatable {
        /// Model zmienił podział na linie — nie da się przenieść separatorów oryginału (PowerPoint: ⇧↩ vs ↩).
        case lineStructureChanged
    }

    /// Znaki końca linii, które aplikacje zapisują różnie (CR i VT w PowerPoincie, CRLF, LS/PS), a model zwraca „\n”.
    static let separators: [String] = ["\r\n", "\n", "\r", "\u{0B}", "\u{2028}", "\u{2029}"]

    /// nil = poprawka nic nie zmienia. `base` — początek fragmentu w dokumencie. `expected` — treść fragmentu
    /// po zapisie (z separatorami oryginału), do sprawdzenia po stronie IME.
    package static func plan(original: String, corrected: String, base: Int, maxHunks: Int = 12) throws
        -> (edits: [ImeProtocol.Edit], expected: String)? {
        let mapped = try mapSeparators(original: original, corrected: corrected)
        guard mapped != original else { return nil }

        var hunks = wordHunks(from: original, to: mapped)
        if hunks.count > maxHunks || apply(hunks, to: original) != mapped {
            // Wiele drobnych zmian albo niespójny diff — jeden fragment od pierwszej do ostatniej zmiany.
            guard let span = TextEdits.minimalSpan(from: original, to: mapped) else { return nil }
            hunks = [ImeProtocol.Edit(location: span.prefix, length: span.oldLength, text: span.replacement)]
        }
        let edits = hunks.sorted { $0.location > $1.location }
            .map { ImeProtocol.Edit(location: base + $0.location, length: $0.length, text: $0.text) }
        return (edits, mapped)
    }

    /// Zastępuje separatory w poprawce separatorami oryginału, po kolei. Inna liczba linii = odmowa.
    static func mapSeparators(original: String, corrected: String) throws -> String {
        let originalSeparators = split(original).1
        let (correctedLines, correctedSeparators) = split(corrected)
        if originalSeparators == correctedSeparators { return corrected }
        guard originalSeparators.count == correctedSeparators.count else { throw Failure.lineStructureChanged }
        var result = ""
        for (index, line) in correctedLines.enumerated() {
            result += line
            if index < originalSeparators.count { result += originalSeparators[index] }
        }
        return result
    }

    /// Linie i separatory między nimi (UTF-16, żeby „\r\n” był jednym separatorem).
    static func split(_ text: String) -> ([String], [String]) {
        let ns = text as NSString
        var lines: [String] = [], found: [String] = []
        var lineStart = 0, index = 0
        while index < ns.length {
            let separator = separators.first { candidate in
                let length = (candidate as NSString).length
                return index + length <= ns.length && ns.substring(with: NSRange(location: index, length: length)) == candidate
            }
            if let separator {
                lines.append(ns.substring(with: NSRange(location: lineStart, length: index - lineStart)))
                found.append(separator)
                index += (separator as NSString).length
                lineStart = index
            } else {
                index += 1
            }
        }
        lines.append(ns.substring(from: lineStart))
        return (lines, found)
    }

    /// Zmiany słowami, w układzie fragmentu (UTF-16), w kolejności od początku.
    static func wordHunks(from original: String, to corrected: String) -> [ImeProtocol.Edit] {
        var hunks: [ImeProtocol.Edit] = []
        var offset = 0
        var current: (location: Int, old: String, new: String)?
        func close() {
            if let c = current { hunks.append(ImeProtocol.Edit(location: c.location, length: (c.old as NSString).length, text: c.new)) }
            current = nil
        }
        for segment in Diff.segments(from: original, to: corrected) {
            switch segment {
            case .same(let text):
                close()
                offset += (text as NSString).length
            case .removed(let text):
                if current == nil { current = (offset, "", "") }
                current?.old += text
                offset += (text as NSString).length
            case .added(let text):
                if current == nil { current = (offset, "", "") }
                current?.new += text
            }
        }
        close()
        return hunks
    }

    /// Zastosowanie podmian (w układzie fragmentu) — do sprawdzenia planu przed wysłaniem.
    static func apply(_ hunks: [ImeProtocol.Edit], to text: String) -> String {
        let result = NSMutableString(string: text)
        for hunk in hunks.sorted(by: { $0.location > $1.location }) {
            guard hunk.location >= 0, hunk.location + hunk.length <= result.length else { return "\u{0}" }
            result.replaceCharacters(in: NSRange(location: hunk.location, length: hunk.length), with: hunk.text)
        }
        return result as String
    }
}
