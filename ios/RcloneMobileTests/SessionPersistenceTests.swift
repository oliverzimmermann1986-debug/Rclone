import Foundation
import XCTest
@testable import RcloneMobile

final class SessionPersistenceTests: XCTestCase {
    func testKeychainHTTPRoundTripKeepsInsecureCookieUsableAfterRelaunch() throws {
        let server = try XCTUnwrap(URL(string: "http://session-\(UUID().uuidString.lowercased()).local:8001/app"))
        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600))
        let first = SessionCookieStore()
        defer { first.remove(for: server) }

        first.save(cookies, for: server)
        let loaded = SessionCookieStore().load(for: server)

        XCTAssertEqual(Set(loaded.map(\.name)), Set([APIClient.sessionCookie, APIClient.csrfCookie]))
        XCTAssertTrue(loaded.allSatisfy { !$0.isSecure }, "An HTTP session must remain usable over the explicitly approved HTTP origin")
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        loaded.forEach(jar.setCookie)
        XCTAssertEqual(jar.cookies(for: server)?.count, 2)
    }

    func testKeychainIsolationIncludesSchemePortAndProxyBasePath() throws {
        let hostname = "isolated-\(UUID().uuidString.lowercased()).local"
        let server = try XCTUnwrap(URL(string: "http://\(hostname):8001/app"))
        let store = SessionCookieStore()
        defer { store.remove(for: server) }
        store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(600)), for: server)

        for other in ["https://\(hostname):8001/app", "http://\(hostname):8002/app", "http://\(hostname):8001/other"] {
            XCTAssertTrue(store.load(for: try XCTUnwrap(URL(string: other))).isEmpty)
        }
        XCTAssertFalse(store.load(for: server).isEmpty)
    }

    func testExpiredKeychainCookiesAreNotRestored() throws {
        let server = try XCTUnwrap(URL(string: "http://expired-\(UUID().uuidString.lowercased()).local"))
        let store = SessionCookieStore()
        defer { store.remove(for: server) }
        store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(-60)), for: server)

        XCTAssertTrue(SessionCookieStore().load(for: server).isEmpty)
    }

    func testPersistenceOptOutRemovesExistingCredentialWithoutEndingCurrentSession() throws {
        for scheme in ["http", "https"] {
            let server = try XCTUnwrap(URL(string: "\(scheme)://session.local:8001"))
            let store = MemorySessionStore()
            store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(600)), for: server)
            let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            let client = APIClient(baseURL: server, cookieStorage: jar, sessionPersistence: store)

            client.setSessionPersistence(false)

            XCTAssertTrue(store.load(for: server).isEmpty)
            XCTAssertEqual(jar.cookies(for: server)?.count, 2)
            let relaunchedJar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            _ = APIClient(baseURL: server, cookieStorage: relaunchedJar, sessionPersistence: store)
            XCTAssertTrue((relaunchedJar.cookies(for: server) ?? []).isEmpty)
            client.clearLocalSession()
        }
    }

    func testServer401ClearsKeychainAndOnlyCurrentCookieJar() async throws {
        let server = try XCTUnwrap(URL(string: "http://session.local:8001"))
        let otherServer = try XCTUnwrap(URL(string: "http://session.local:8002"))
        let store = MemorySessionStore()
        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600))
        store.save(cookies, for: server)
        store.save(cookies, for: otherServer)
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RejectedSessionURLProtocol.self]
        config.httpCookieStorage = jar
        let client = APIClient(baseURL: server, session: URLSession(configuration: config), cookieStorage: jar, sessionPersistence: store)

        do {
            _ = try await client.getConfig()
            XCTFail("Rejected sessions must require a fresh login")
        } catch let error as APIError {
            XCTAssertEqual(error, .unauthenticated)
        }

        XCTAssertTrue(store.load(for: server).isEmpty)
        XCTAssertTrue((jar.cookies(for: server) ?? []).isEmpty)
        XCTAssertEqual(store.load(for: otherServer).count, 2)
    }

    private func sessionCookies(server: URL, expires: Date) throws -> [HTTPCookie] {
        try [APIClient.sessionCookie, APIClient.csrfCookie].map { name in
            var properties: [HTTPCookiePropertyKey: Any] = [
                .domain: try XCTUnwrap(server.host), .path: "/", .name: name,
                .value: "fixture-value", .expires: expires
            ]
            if server.scheme == "https" { properties[.secure] = "TRUE" }
            return try XCTUnwrap(HTTPCookie(properties: properties))
        }
    }
}

private final class MemorySessionStore: SessionPersisting {
    private var records: [String: [HTTPCookie]] = [:]

    func load(for server: URL) -> [HTTPCookie] { records[server.absoluteString] ?? [] }
    func save(_ cookies: [HTTPCookie], for server: URL) { records[server.absoluteString] = cookies }
    func remove(for server: URL) { records.removeValue(forKey: server.absoluteString) }
}

private final class RejectedSessionURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"detail":"Login required"}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
