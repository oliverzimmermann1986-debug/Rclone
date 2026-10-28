import Foundation
import Security
import XCTest
@testable import RcloneMobile

final class SessionPersistenceTests: XCTestCase {
    func testNormalLoadRepairsCorruptKeychainJSONBeforeANewConditionalSave() throws {
        let server = try XCTUnwrap(URL(string: "https://corrupt-\(UUID().uuidString.lowercased()).local"))
        let store = SessionCookieStore()
        defer { store.remove(for: server) }
        let corrupt: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "de.sicherpfad.server-session",
            kSecAttrAccount as String: server.absoluteString,
            kSecValueData as String: Data("invalid-json-fixture".utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        XCTAssertEqual(SecItemAdd(corrupt as CFDictionary, nil), errSecSuccess)
        XCTAssertThrowsError(try store.loadChecked(for: server)) { error in
            XCTAssertEqual(error as? SessionCookieStore.StorageError, .invalidCookieData)
        }

        XCTAssertTrue(store.load(for: server).isEmpty)

        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "new-valid-session")
        XCTAssertTrue(store.save(cookies, for: server, replacingSession: nil))
        XCTAssertEqual(try store.loadChecked(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value, "new-valid-session")
    }

    func testClientDiscardsLegacyHTTPKeychainCookiesBeforeRelaunch() throws {
        let server = try XCTUnwrap(URL(string: "http://session-\(UUID().uuidString.lowercased()).local:8001/app"))
        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600))
        let first = SessionCookieStore()
        defer { first.remove(for: server) }

        try first.saveChecked(cookies, for: server)
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        cookies.forEach(jar.setCookie)
        let client = APIClient(baseURL: server, cookieStorage: jar, sessionPersistence: first)

        XCTAssertTrue(try first.loadChecked(for: server).isEmpty)
        XCTAssertTrue((jar.cookies(for: server) ?? []).isEmpty)
        client.setSessionPersistence(true)
        XCTAssertTrue(try first.loadChecked(for: server).isEmpty)
    }

    func testKeychainIsolationIncludesSchemePortAndProxyBasePath() throws {
        let hostname = "isolated-\(UUID().uuidString.lowercased()).local"
        let server = try XCTUnwrap(URL(string: "http://\(hostname):8001/app"))
        let store = SessionCookieStore()
        defer { store.remove(for: server) }
        try store.saveChecked(try sessionCookies(server: server, expires: Date().addingTimeInterval(600)), for: server)

        for other in ["https://\(hostname):8001/app", "http://\(hostname):8002/app", "http://\(hostname):8001/other"] {
            XCTAssertTrue(try store.loadChecked(for: XCTUnwrap(URL(string: other))).isEmpty)
        }
        XCTAssertFalse(try store.loadChecked(for: server).isEmpty)
    }

    func testExpiredKeychainCookiesAreNotRestored() throws {
        let server = try XCTUnwrap(URL(string: "http://expired-\(UUID().uuidString.lowercased()).local"))
        let store = SessionCookieStore()
        defer { store.remove(for: server) }
        try store.saveChecked(try sessionCookies(server: server, expires: Date().addingTimeInterval(-60)), for: server)

        XCTAssertTrue(try SessionCookieStore().loadChecked(for: server).isEmpty)
    }

    func testCookieEncodingPreservesHTTPHTTPSExpiryAndRejectsWrongOrigin() throws {
        for scheme in ["http", "https"] {
            let server = try XCTUnwrap(URL(string: "\(scheme)://codec.local:8001/app"))
            let expiry = Date().addingTimeInterval(600)
            let cookies = try sessionCookies(server: server, expires: expiry)
            let data = try SessionCookieStore.encodeCookies(cookies)
            let decoded = try SessionCookieStore.decodeCookies(data, for: server)

            XCTAssertEqual(Set(decoded.map(\.name)), Set([APIClient.sessionCookie, APIClient.csrfCookie]))
            XCTAssertEqual(decoded.map(\.value), cookies.map(\.value))
            XCTAssertTrue(decoded.allSatisfy { $0.isSecure == (scheme == "https") })
            for cookie in decoded {
                XCTAssertEqual(try XCTUnwrap(cookie.expiresDate).timeIntervalSince1970,
                               expiry.timeIntervalSince1970, accuracy: 1)
            }
            let wrongOrigin = try XCTUnwrap(URL(string: "\(scheme)://other.local:8001/app"))
            XCTAssertTrue(try SessionCookieStore.decodeCookies(data, for: wrongOrigin).isEmpty)
            if scheme == "https" {
                let insecureOrigin = try XCTUnwrap(URL(string: "http://codec.local:8001/app"))
                XCTAssertTrue(try SessionCookieStore.decodeCookies(data, for: insecureOrigin).isEmpty)
            }
        }
    }

    func testCookieEncodingRejectsExpiredAndMalformedRecords() throws {
        let server = try XCTUnwrap(URL(string: "http://codec.local:8001/app"))
        let expired = try sessionCookies(server: server, expires: Date().addingTimeInterval(-60))
        let data = try SessionCookieStore.encodeCookies(expired)
        XCTAssertTrue(try SessionCookieStore.decodeCookies(data, for: server).isEmpty)
        XCTAssertThrowsError(try SessionCookieStore.decodeCookies(Data("invalid".utf8), for: server)) { error in
            XCTAssertEqual(error as? SessionCookieStore.StorageError, .invalidCookieData)
        }
    }

    func testPersistenceOptOutRemovesExistingCredentialWithoutEndingCurrentSession() throws {
        for scheme in ["http", "https"] {
            let server = try XCTUnwrap(URL(string: "\(scheme)://session.local:8001"))
            let store = MemorySessionStore()
            store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(600)), for: server)
            let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            let client = APIClient(baseURL: server, cookieStorage: jar, sessionPersistence: store)
            // HTTP credentials can exist only after a fresh manual login.
            if scheme == "http" {
                try sessionCookies(server: server, expires: Date().addingTimeInterval(600)).forEach(jar.setCookie)
            }

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
        let server = try XCTUnwrap(URL(string: "https://session.local:8001"))
        let otherServer = try XCTUnwrap(URL(string: "https://session.local:8002"))
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

    func testHTTPNeverLoadsOrSavesCookiesAndRemovesStaleRecordsWithoutEndingManualSession() throws {
        let server = try XCTUnwrap(URL(string: "http://session.local:8001/app"))
        let other = try XCTUnwrap(URL(string: "https://session.local:8001/app"))
        let store = MemorySessionStore()
        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600))
        store.save(cookies, for: server)
        store.save(try sessionCookies(server: other, expires: Date().addingTimeInterval(600)), for: other)
        store.loadCount = 0
        store.saveCount = 0
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        let client = APIClient(baseURL: server, cookieStorage: jar, sessionPersistence: store)
        XCTAssertEqual(store.loadCount, 0)
        XCTAssertTrue((jar.cookies(for: server) ?? []).isEmpty)
        XCTAssertTrue(store.load(for: server).isEmpty)

        cookies.forEach(jar.setCookie)
        // Model an old writer recreating the record while a manual session runs.
        store.save(cookies, for: server)
        store.saveCount = 0
        client.setSessionPersistence(true)
        XCTAssertEqual(store.saveCount, 0)
        XCTAssertTrue(store.load(for: server).isEmpty)
        XCTAssertEqual(jar.cookies(for: server)?.count, 2)
        XCTAssertEqual(store.load(for: other).count, 2)

        let relaunchedJar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        _ = APIClient(baseURL: server, cookieStorage: relaunchedJar, sessionPersistence: store)
        XCTAssertTrue((relaunchedJar.cookies(for: server) ?? []).isEmpty)
    }

    func testHTTPSRestoresAndPersistsSessionAndCSRFCookiesOnly() throws {
        let server = try XCTUnwrap(URL(string: "https://session.local:8001/app"))
        let store = MemorySessionStore()
        let cookies = try sessionCookies(server: server, expires: Date().addingTimeInterval(600))
        store.save(cookies, for: server)
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        let client = APIClient(baseURL: server, cookieStorage: jar, sessionPersistence: store)
        XCTAssertEqual(jar.cookies(for: server)?.count, 2)
        jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [
            .domain: "session.local", .path: "/", .name: "unrelated", .value: "fixture"
        ])))

        client.setSessionPersistence(true)

        XCTAssertEqual(Set(store.load(for: server).map(\.name)), Set([APIClient.sessionCookie, APIClient.csrfCookie]))
    }

    func testOlderClientCannotClearOrOverwriteANewerSameOriginSession() async throws {
        for action in ["clear", "401", "logout", "optOut", "lateSave"] {
            let server = try XCTUnwrap(URL(string: "https://owned-session.local:8001"))
            let store = MemorySessionStore()
            store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "old-session"), for: server)
            let oldJar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [RejectedSessionURLProtocol.self]
            configuration.httpCookieStorage = oldJar
            let transport = URLSession(configuration: configuration)
            defer { transport.invalidateAndCancel() }
            let old = APIClient(baseURL: server, session: transport, cookieStorage: oldJar, sessionPersistence: store)
            let newJar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            let current = APIClient(baseURL: server, cookieStorage: newJar, sessionPersistence: store)
            try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "new-session").forEach(newJar.setCookie)
            current.setSessionPersistence(true)

            switch action {
            case "401": _ = try? await old.getConfig()
            case "logout": _ = try? await old.logout()
            case "optOut": old.setSessionPersistence(false)
            case "lateSave": old.setSessionPersistence(true)
            default: old.clearLocalSession()
            }

            XCTAssertEqual(store.load(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value, "new-session", action)
            XCTAssertEqual(newJar.cookies(for: server)?.first(where: { $0.name == APIClient.sessionCookie })?.value, "new-session", action)
            if ["clear", "401", "logout"].contains(action) {
                XCTAssertTrue((oldJar.cookies(for: server) ?? []).isEmpty, action)
            }
        }
    }

    func testClientWithoutAnOwnedSessionCannotDeleteANewerSavedCredential() throws {
        let server = try XCTUnwrap(URL(string: "https://unbound-session.local"))
        let store = MemorySessionStore()
        let unbound = APIClient(baseURL: server, sessionPersistence: store)
        store.save(try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "new-session"), for: server)
        unbound.clearLocalSession()
        unbound.setSessionPersistence(false)
        XCTAssertEqual(store.load(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value, "new-session")
    }

    func testConcreteKeychainStoreConditionallyReplacesAndRemovesOnlyItsOwnedSession() throws {
        let server = try XCTUnwrap(URL(string: "https://owned-\(UUID().uuidString.lowercased()).local"))
        let first = SessionCookieStore()
        let second = SessionCookieStore()
        defer { first.remove(for: server) }
        let old = try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "old-session")
        let new = try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "new-session")
        try first.saveChecked(old, for: server)
        XCTAssertTrue(second.save(new, for: server, replacingSession: "old-session"))
        first.remove(for: server, matchingSession: "old-session")
        XCTAssertFalse(first.save(old, for: server, replacingSession: "old-session"))
        XCTAssertFalse(first.save(old, for: server, replacingSession: nil))
        XCTAssertEqual(try first.loadChecked(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value, "new-session")
        second.remove(for: server, matchingSession: "new-session")
        XCTAssertTrue(try first.loadChecked(for: server).isEmpty)
    }

    func testConcurrentOldRemovalAndNewSaveAcrossKeychainInstancesAlwaysKeepNewSession() throws {
        let server = try XCTUnwrap(URL(string: "https://atomic-\(UUID().uuidString.lowercased()).local"))
        let first = SessionCookieStore()
        let second = SessionCookieStore()
        defer { first.remove(for: server) }
        let old = try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "old-session")
        let new = try sessionCookies(server: server, expires: Date().addingTimeInterval(600), sessionValue: "new-session")
        for _ in 0..<8 {
            try first.saveChecked(old, for: server)
            DispatchQueue.concurrentPerform(iterations: 2) { operation in
                if operation == 0 { first.remove(for: server, matchingSession: "old-session") }
                else { second.save(new, for: server) }
            }
            XCTAssertEqual(try first.loadChecked(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value, "new-session")
        }
    }

    private func sessionCookies(server: URL, expires: Date, sessionValue: String = "fixture-value") throws -> [HTTPCookie] {
        try [APIClient.sessionCookie, APIClient.csrfCookie].map { name in
            var properties: [HTTPCookiePropertyKey: Any] = [
                .domain: try XCTUnwrap(server.host), .path: "/", .name: name,
                .value: name == APIClient.sessionCookie ? sessionValue : "fixture-value", .expires: expires
            ]
            if server.scheme == "https" { properties[.secure] = "TRUE" }
            return try XCTUnwrap(HTTPCookie(properties: properties))
        }
    }
}

private final class MemorySessionStore: SessionPersisting {
    private var records: [String: [HTTPCookie]] = [:]
    var loadCount = 0
    var saveCount = 0

    func load(for server: URL) -> [HTTPCookie] {
        loadCount += 1
        return records[server.absoluteString] ?? []
    }
    func save(_ cookies: [HTTPCookie], for server: URL) {
        saveCount += 1
        records[server.absoluteString] = cookies
    }
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
