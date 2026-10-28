import Foundation

/// Gdzie w treści maila kończy się nowa wiadomość, a zaczyna cytowana korespondencja.
/// Pole odpowiedzi w Outlooku potrafi zawierać kilkanaście wcześniejszych maili — poprawiamy tylko to,
/// co użytkownik właśnie pisze.
package enum ReplyBoundary {
    /// Nagłówek cytowanego maila: „Od: … / Wysłano: … / Do: … / Temat: …" (Outlook) w różnych językach.
    private static let fromLine = try! NSRegularExpression(
        pattern: #"^\s*\*?(From|Od|Von|De|Van|Da)\s*:\*?\s+\S"#, options: [.caseInsensitive])
    private static let headerLine = try! NSRegularExpression(
        pattern: #"^\s*\*?(Sent|Date|Wysłano|Wysłane|Data|Gesendet|Datum|Envoyé|Enviado|To|Do|An|À|Subject|Temat|Betreff|Objet|Asunto|Cc|DW|UDW)\s*:"#,
        options: [.caseInsensitive])
    /// Separatory i wstępy cytatu.
    private static let separatorLine = try! NSRegularExpression(pattern: [
        #"^\s*-{2,}\s*(Original Message|Wiadomość oryginalna|Oryginalna wiadomość|Ursprüngliche Nachricht|Message d'origine|Forwarded message|Wiadomość przekazana dalej|Przekazana wiadomość)\s*-{2,}\s*$"#,
        #"^\s*_{16,}\s*$"#,
        #"^\s*On .{4,200}(wrote|napisał|napisała|napisał\(a\)):\s*$"#,
        #"^\s*W dniu .{4,200}(pisze|napisał|napisała|napisał\(a\)):\s*$"#,
        #"^\s*.{2,120} (napisał|napisała|napisał\(a\)|wrote):\s*$"#,
        #"^\s*Am .{4,200} schrieb .{1,120}:\s*$"#,
        #"^\s*Le .{4,200} a écrit\s*:\s*$"#,
    ].joined(separator: "|"), options: [.caseInsensitive])

    /// Przesunięcie (UTF-16) początku cytowanej korespondencji albo nil, gdy jej nie ma.
    static func find(in text: String) -> Int? {
        let ns = text as NSString
        var lines: [NSRange] = []
        ns.enumerateSubstrings(in: NSRange(location: 0, length: ns.length), options: [.byLines, .substringNotRequired]) { _, range, _, _ in
            lines.append(range)
        }
        func matches(_ regex: NSRegularExpression, _ range: NSRange) -> Bool {
            regex.firstMatch(in: text, options: [.anchored], range: range) != nil
        }
        for (index, line) in lines.enumerated() {
            guard line.length > 0 else { continue }
            if matches(separatorLine, line) { return line.location }
            // Linie z „>" (klasyczny cytat), co najmniej dwie pod rząd.
            if ns.substring(with: line).hasPrefix(">"), index + 1 < lines.count,
               ns.substring(with: lines[index + 1]).hasPrefix(">") {
                return line.location
            }
            // „Od: …", a w kolejnych kilku liniach co najmniej dwa inne pola nagłówka.
            if matches(fromLine, line) {
                let following = lines[(index + 1)..<min(index + 7, lines.count)]
                if following.filter({ matches(headerLine, $0) }).count >= 2 { return line.location }
            }
        }
        return nil
    }

    /// Odpowiedź nad cytatem: `message` od początku do cytatu, bez końcowych pustych linii (pusty, gdy nad
    /// cytatem nic nie napisano), i `quote` — początek cytatu. nil, gdy cytatu nie ma.
    /// Kursor należy do nowej wiadomości aż do `quote`: także za spacją i w pustej linii pod tekstem.
    package static func split(in text: String) -> (message: NSRange, quote: Int)? {
        guard let boundary = find(in: text) else { return nil }
        let ns = text as NSString
        var end = boundary
        while end > 0, let scalar = UnicodeScalar(ns.character(at: end - 1)),
              CharacterSet.whitespacesAndNewlines.contains(scalar) {
            end -= 1
        }
        return (NSRange(location: 0, length: end), boundary)
    }

    /// Nowa wiadomość (od początku do cytatu), bez końcowych pustych linii. nil, gdy nie ma cytatu albo nad nim nic nie ma.
    package static func newMessageRange(in text: String) -> NSRange? {
        guard let message = split(in: text)?.message, message.length > 0 else { return nil }
        return message
    }
}
