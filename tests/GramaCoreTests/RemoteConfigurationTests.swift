import Testing
@testable import GramaCore

@Suite("Konfiguracja dostępu zdalnego")
struct RemoteConfigurationTests {
    @Test func explicitConfigurationRequired() {
        #expect(RemoteServiceConfiguration(address: "", clientID: "") == nil)
        #expect(RemoteServiceConfiguration(address: "https://id.example.com", clientID: " ") == nil)
        #expect(RemoteServiceConfiguration(address: "https://id.example.com", clientID: "test-client") != nil)
    }

    @Test func refusesCredentialsAndUnsafeDestinations() {
        for address in ["http://id.example.com", "https://user:password@id.example.com",
                        "https://id.example.com/path", "https://id.example.com?token=test",
                        "https://id.example.com#fragment", "file:///tmp/config", "https://id.example.com:70000"] {
            #expect(RemoteServiceConfiguration(address: address, clientID: "test-client") == nil)
        }
        #expect(RemoteServiceConfiguration(address: "http://127.0.0.1:1411", clientID: "test-client") != nil)
    }

    @Test func tokenIdentityIncludesServerAndClient() throws {
        let first = try #require(RemoteServiceConfiguration(address: " https://id.example.com/ ", clientID: " a "))
        let same = try #require(RemoteServiceConfiguration(address: "https://id.example.com", clientID: "a"))
        let other = try #require(RemoteServiceConfiguration(address: "https://other.example.com", clientID: "a"))
        let client = try #require(RemoteServiceConfiguration(address: "https://id.example.com", clientID: "b"))
        #expect(first.identity == same.identity)
        #expect(first.identity != other.identity)
        #expect(first.identity != client.identity)
    }
}
