import CryptoKit
import Darwin
import Foundation
import Security

/// Kto jest po drugiej stronie gniazda: proces o danym identyfikatorze, podpisany tym samym certyfikatem co my.
/// Proces sprawdzamy przez token audytu gniazda (LOCAL_PEERTOKEN) — w przeciwieństwie do pid nie da się go
/// podmienić ponownie użytym numerem procesu.
///
/// Uwaga: to nie jest granica wobec kodu tego samego użytkownika, który ma dostęp do klucza podpisu
/// (przegląd 2026-09-29, F15). Chroni przed przypadkowymi i cudzymi procesami, nie przed złośliwym kodem na koncie.
package enum PeerCheck {
    /// SHA-1 certyfikatu liścia, którym podpisany jest ten proces. nil: podpis ad hoc albo brak podpisu —
    /// wtedy kanał jest wyłączony (nie ma czego porównać).
    package static let ownLeafHash: String? = {
        var code: SecCode?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code else { return nil }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else { return nil }
        var info: CFDictionary?
        guard SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
              let dictionary = info as? [String: Any],
              let certificates = dictionary[kSecCodeInfoCertificates as String] as? [SecCertificate],
              let leaf = certificates.first else { return nil }
        let data = SecCertificateCopyData(leaf) as Data
        return Insecure.SHA1.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }()

    /// true, gdy proces po drugiej stronie `fd` ma identyfikator `identifier` i ten sam certyfikat liścia.
    package static func verify(fd: Int32, identifier: String) -> Bool {
        guard let leaf = ownLeafHash else { return false }
        var token = audit_token_t()
        var length = socklen_t(MemoryLayout<audit_token_t>.size)
        guard getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &length) == 0 else { return false }
        let tokenData = withUnsafeBytes(of: &token) { Data($0) }
        var guest: SecCode?
        let attributes = [kSecGuestAttributeAudit: tokenData] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &guest) == errSecSuccess, let guest else { return false }
        var requirement: SecRequirement?
        let text = "identifier \"\(identifier)\" and certificate leaf = H\"\(leaf)\""
        guard SecRequirementCreateWithString(text as CFString, [], &requirement) == errSecSuccess, let requirement else { return false }
        return SecCodeCheckValidity(guest, [], requirement) == errSecSuccess
    }
}
