import Foundation
import GramaIMEProtocol

/// Czyste operacje na tekście pola: akapit z kursorem, najkrótsza podmiana, spacja przed wstawieniem.
package enum TextEdits {
    /// Spacja między istniejącym tekstem a podyktowanym: „Hej.|” + „Kolejne” → „Hej. Kolejne”.
    /// Ta sama reguła działa w metodzie wprowadzania (wstawianie dyktowania), stąd wspólny moduł.
    package static func leadingSpaceNeeded(before text: String, after previous: Character?) -> Bool {
        ImeText.leadingSpaceNeeded(before: text, after: previous)
    }
    /// Akapit z kursorem: od poprzedniego do następnego końca linii, bez samego znaku końca linii.
    package static func paragraphRange(in text: String, caret: Int) -> NSRange {
        let ns = text as NSString
        var paragraph = ns.paragraphRange(for: NSRange(location: min(max(caret, 0), ns.length), length: 0))
        while paragraph.length > 0,
              let scalar = UnicodeScalar(ns.character(at: paragraph.location + paragraph.length - 1)),
              CharacterSet.newlines.contains(scalar) {
            paragraph.length -= 1
        }
        return paragraph
    }
    /// Najkrótszy zmieniony fragment (na granicach znaków widzianych przez użytkownika).
    package static func minimalSpan(from old: String, to new: String) -> (prefix: Int, oldLength: Int, replacement: String)? {
        let a = Array(old), b = Array(new)
        var p = 0
        while p < a.count, p < b.count, a[p] == b[p] { p += 1 }
        if p == a.count, p == b.count { return nil }
        var s = 0
        while s < a.count - p, s < b.count - p, a[a.count - 1 - s] == b[b.count - 1 - s] { s += 1 }
        return (String(a[..<p]).utf16.count, String(a[p..<(a.count - s)]).utf16.count, String(b[p..<(b.count - s)]))
    }
}

package extension String {
    var isBlank: Bool { trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
}
