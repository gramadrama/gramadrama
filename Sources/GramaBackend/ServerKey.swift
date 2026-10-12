import Foundation
import os
import Security

/// Klucz API serwera (modelu albo mowy) w pęku kluczy tego Maca, nie w ustawieniach aplikacji. Po pierwszym odczycie
/// z pamięci: serwer modelu dostaje zapytanie przy każdym fragmencie sprawdzanym podczas pisania.
package struct ServerKey: Sendable {
    package static let model = ServerKey(service: "pl.gramadrama.model")
    package static let speech = ServerKey(service: "pl.gramadrama.speech")

    private let service: String
    private static let cache = OSAllocatedUnfairLock(initialState: [String: String]())

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: "api-key"]
    }

    /// Zapisany klucz albo "" (brak klucza albo pęk kluczy nie dał go odczytać).
    package func load() -> String { read() ?? "" }

    /// Zapisany klucz, "" — brak klucza, nil — nie udało się odczytać (np. odmowa dostępu); wtedy formularz
    /// nie może nadpisać klucza pustym polem.
    package func read() -> String? {
        if let cached = Self.cache.withLock({ $0[service] }) { return cached }
        var result: CFTypeRef?
        var search = query
        search[kSecReturnData as String] = true
        let key: String
        switch SecItemCopyMatching(search as CFDictionary, &result) {
        case errSecSuccess: key = (result as? Data).map { String(decoding: $0, as: UTF8.self) } ?? ""
        case errSecItemNotFound: key = ""
        default: return nil
        }
        Self.cache.withLock { $0[service] = key }
        return key
    }

    /// Pusty klucz usuwa zapisany.
    package func save(_ key: String) {
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        if key.isEmpty {
            SecItemDelete(query as CFDictionary)
        } else {
            let attributes: [String: Any] = [kSecValueData as String: Data(key.utf8),
                                             kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
            if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
                SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil)
            }
        }
        Self.cache.withLock { $0[service] = key }
    }
}
