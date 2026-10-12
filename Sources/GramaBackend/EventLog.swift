import Foundation
import GramaCore

/// Log zdarzeń bez treści (jak na Linuksie): co się stało, w której aplikacji, długości i czasy —
/// nigdy sam tekst. `~/Library/Logs/GramaDrama/events.jsonl`, rotacja po 1 MB.
/// Z niego powstaje lista „Aplikacje" w ustawieniach.
package enum EventLog {
    static let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/GramaDrama", isDirectory: true)
    package static let file = directory.appendingPathComponent("events.jsonl")
    private static let queue = DispatchQueue(label: "gramadrama.eventlog")
    private static let maxBytes = 1_000_000

    /// Powiadomienie o każdym zdarzeniu (w tym procesie) — okno GramaDramy pokazuje na nim, co się dzieje.
    package static let didWrite = Notification.Name("GramaDramaEvent")

    /// `fields` mogą zawierać tylko liczby, wartości logiczne i krótkie etykiety — nigdy tekst użytkownika.
    package static func write(_ event: String, _ fields: [String: Any] = [:]) {
        var entry = fields
        entry["event"] = event
        entry["ts"] = ISO8601DateFormatter().string(from: Date())
        guard JSONSerialization.isValidJSONObject(entry),
              let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else { return }
        let posted = entry
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: didWrite, object: nil, userInfo: posted)
        }
        queue.async {
            let fm = FileManager.default
            try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
            if let size = (try? fm.attributesOfItem(atPath: file.path))?[.size] as? Int, size > maxBytes {
                let old = directory.appendingPathComponent("events.1.jsonl")
                try? fm.removeItem(at: old)
                try? fm.moveItem(at: file, to: old)
            }
            if !fm.fileExists(atPath: file.path) { fm.createFile(atPath: file.path, contents: nil) }
            guard let handle = try? FileHandle(forWritingTo: file) else { return }
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data + Data("\n".utf8))
        }
    }

    package struct AppUsage: Identifiable {
        package let bundleID: String
        package var uses: Int
        package var lastUsed: Date
        package var lastOutcome: String
        package var id: String { bundleID }
    }

    /// Czy w logu (także w poprzednim pliku) jest takie zdarzenie, np. udana korekta sprzed zapisania jej daty.
    package static func contains(_ event: String) -> Bool {
        entries().contains { $0["event"] as? String == event }
    }

    private static func entries() -> [[String: Any]] {
        let rotated = directory.appendingPathComponent("events.1.jsonl")
        let content = queue.sync {
            [rotated, file].compactMap { try? String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        }
        return content.split(separator: "\n").compactMap {
            $0.data(using: .utf8).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        }
    }

    package struct LastUse {
        package let date: Date
        package let bundleID: String
        package let mode: String
        /// Zakres z logu (`TextBridge.Scope.label`, po polsku) — tłumaczony przy pokazaniu.
        package let scope: String?
        /// Ostatnie zdarzenie po skrócie (`capture`, `session`, `replace`…) i jego `reason` albo `outcome`.
        let event: String?
        let detail: String?

        /// Jak się skończyło, w języku interfejsu.
        package var outcome: String {
            switch event {
            case "capture": tr("pokazano panel")
            case "capture_refused": tr("odmowa: {reason}", ["reason": tr(EventLog.reasonText(detail))])
            case "session": tr("wynik gotowy")
            case "session_failed": tr("model nie odpowiedział")
            case "replace": detail == "pasted" ? tr("zastosowano") : tr("nie wklejono ({outcome})", ["outcome": detail ?? "?"])
            default: tr("bez wyniku (panel zamknięty)")
            }
        }

        /// Skrót zadziałał: panel się pokazał, wynik jest gotowy albo zmiana trafiła do aplikacji.
        package var ok: Bool { event == "capture" || event == "session" || (event == "replace" && detail == "pasted") }
    }

    /// Ostatnie użycie skrótu: kiedy, w której aplikacji, tryb, zakres i jak się skończyło.
    package static func lastUse() -> LastUse? {
        let all = entries()
        guard let start = all.lastIndex(where: { $0["event"] as? String == "hotkey" }) else { return nil }
        let hotkey = all[start]
        guard let date = (hotkey["ts"] as? String).flatMap(ISO8601DateFormatter().date) else { return nil }
        var scope: String?
        var event: String?, detail: String?
        for entry in all[(start + 1)...] {
            switch entry["event"] as? String {
            case "capture": scope = entry["scope"] as? String; (event, detail) = ("capture", nil)
            case "capture_refused": (event, detail) = ("capture_refused", entry["reason"] as? String)
            case "session", "session_failed": (event, detail) = (entry["event"] as? String, nil)
            case "replace": (event, detail) = ("replace", entry["outcome"] as? String)
            default: continue
            }
        }
        return LastUse(date: date, bundleID: hotkey["app"] as? String ?? "", mode: hotkey["mode"] as? String ?? "",
                       scope: scope, event: event, detail: detail)
    }

    /// Powód odmowy po polsku (dane — tłumaczy go miejsce, które go pokazuje; wpisy w `dynamic.json`).
    package static func reasonText(_ code: String?) -> String {
        switch code {
        case "nothing_found": "nie znalazłem tekstu"
        case "empty_line": "kursor w pustej linii"
        case "password_field": "pole hasła"
        case "too_long": "tekst za długi"
        case "attachments": "pole z obrazami"
        case "busy": "poprzednia operacja trwała"
        case "no_permission": "brak uprawnienia Dostępność"
        default: code ?? "?"
        }
    }

    /// Aplikacje, w których użyto skrótu w ostatnich 30 dniach (najświeższe pierwsze).
    package static func recentApps(days: Int = 30) -> [AppUsage] {
        // Także zarotowany plik — inaczej po każdej rotacji lista „Aplikacje" byłaby pusta.
        let rotated = directory.appendingPathComponent("events.1.jsonl")
        let content = queue.sync {
            [rotated, file].compactMap { try? String(contentsOf: $0, encoding: .utf8) }.joined(separator: "\n")
        }
        guard !content.isEmpty else { return [] }
        let formatter = ISO8601DateFormatter()
        let cutoff = Date().addingTimeInterval(-Double(days) * 86_400)
        var apps: [String: AppUsage] = [:]
        for line in content.split(separator: "\n") {
            guard let data = line.data(using: .utf8),
                  let entry = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let event = entry["event"] as? String, event.hasPrefix("capture"),
                  let bundleID = entry["app"] as? String, !bundleID.isEmpty,
                  let date = (entry["ts"] as? String).flatMap(formatter.date), date >= cutoff else { continue }
            let outcome = event == "capture" ? "działa" : reasonText(entry["reason"] as? String)
            var usage = apps[bundleID] ?? AppUsage(bundleID: bundleID, uses: 0, lastUsed: date, lastOutcome: outcome)
            usage.uses += 1
            if date >= usage.lastUsed { usage.lastUsed = date; usage.lastOutcome = outcome }
            apps[bundleID] = usage
        }
        return apps.values.sorted { $0.lastUsed > $1.lastUsed }
    }
}
