import Foundation

/// User-supplied Pocket ID origin. Credentials belong to this origin and client together.
package struct RemoteServiceConfiguration: Equatable, Sendable {
    package let base: URL
    package let clientID: String

    package init?(address: String, clientID: String) {
        let client = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !client.isEmpty,
              var parts = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/" else { return nil }
        let loopback = ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host.lowercased())
        guard parts.scheme?.lowercased() == "https" || (parts.scheme?.lowercased() == "http" && loopback),
              parts.port.map({ (1...65535).contains($0) }) ?? true else { return nil }
        parts.scheme = parts.scheme?.lowercased()
        parts.host = host.lowercased()
        parts.path = ""
        guard let url = parts.url else { return nil }
        base = url
        self.clientID = client
    }

    package var identity: String { base.absoluteString + "|" + clientID }
}
