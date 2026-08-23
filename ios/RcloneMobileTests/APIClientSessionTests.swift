import Foundation
import XCTest
@testable import RcloneMobile

final class APIClientSessionTests: XCTestCase {
    func testUserFacingConnectionErrorsAreRecoveryOriented() {
        XCTAssertEqual(
            APIError.invalidResponse.errorDescription,
            "Die Serverantwort konnte nicht geprüft werden. Prüfe, ob die Adresse zu Rclone Sync gehört, und versuche es erneut."
        )
        XCTAssertEqual(
            APIError.incompatibleResponse(resource: "Anmeldung").errorDescription,
            "Anmeldung konnte nicht gelesen werden. Aktualisiere Server oder App und versuche es erneut."
        )
        XCTAssertFalse(APIError.invalidResponse.errorDescription?.contains("HTTP") == true)
    }

    func testSharedNativeLoginContractFixturesDecode() throws {
        let url = try XCTUnwrap(
            Bundle(for: Self.self).url(
                forResource: "native_login_contract",
                withExtension: "json"
            )
        )
        let root = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        )
        let challengeFixture = try XCTUnwrap(root["challenge"] as? [String: Any])
        let challengeBody = try XCTUnwrap(challengeFixture["body"])
        let challengeData = try JSONSerialization.data(withJSONObject: challengeBody)
        let challenge = try JSONDecoder().decode(NativeLoginChallenge.self, from: challengeData)
        XCTAssertEqual(challenge.status, "csrf_ready")
        XCTAssertFalse(challenge.loginCSRF.isEmpty)

        let outcomes = try XCTUnwrap(root["outcomes"] as? [[String: Any]])
        XCTAssertEqual(Set(outcomes.compactMap { $0["name"] as? String }), Set([
            "success", "invalid_credentials", "rate_limited", "csrf_failed", "origin_failed"
        ]))
        for fixture in outcomes {
            let body = try XCTUnwrap(fixture["body"])
            let data = try JSONSerialization.data(withJSONObject: body)
            let decoded = try JSONDecoder().decode(NativeLoginResponse.self, from: data)
            XCTAssertEqual(decoded.status, fixture["name"] as? String)
        }
    }

    func testLoginUsesBoundedRequestsWithoutBlockingOnConfigRefresh() async throws {
        let baseURL = try XCTUnwrap(URL(string: "http://192.168.1.67:8001"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientLoginTimeoutTests-\(UUID().uuidString)"
        )
        RecordingLoginURLProtocol.reset(cookieStorage: cookieStorage)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RecordingLoginURLProtocol.self]
        configuration.httpCookieStorage = cookieStorage
        configuration.httpShouldSetCookies = true
        let loginSession = URLSession(configuration: configuration)
        let client = APIClient(
            baseURL: baseURL,
            session: loginSession,
            loginSession: loginSession,
            cookieStorage: cookieStorage
        )

        try await client.login(username: "admin", password: "secret")

        XCTAssertEqual(
            RecordingLoginURLProtocol.requests,
            ["GET /api/auth/login", "POST /api/auth/login"]
        )
        XCTAssertEqual(RecordingLoginURLProtocol.timeouts.count, 2)
        XCTAssertTrue(RecordingLoginURLProtocol.timeouts.allSatisfy { $0 <= 8.1 })
    }

    func testLoginFallsBackToCSRFProtectedWebContractOnOlderServer() async throws {
        let baseURL = try XCTUnwrap(URL(string: "http://192.168.1.67"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientLegacyLoginTests-\(UUID().uuidString)"
        )
        LegacyLoginURLProtocol.reset(cookieStorage: cookieStorage)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [LegacyLoginURLProtocol.self]
        configuration.httpCookieStorage = cookieStorage
        configuration.httpShouldSetCookies = true
        let loginSession = URLSession(configuration: configuration)
        let client = APIClient(
            baseURL: baseURL,
            session: loginSession,
            loginSession: loginSession,
            cookieStorage: cookieStorage
        )

        try await client.login(username: "admin", password: "sicher & geheim")

        XCTAssertEqual(
            LegacyLoginURLProtocol.requests,
            ["GET /api/auth/login", "GET /login", "POST /login"]
        )
        XCTAssertEqual(LegacyLoginURLProtocol.timeouts.count, 3)
        XCTAssertTrue(LegacyLoginURLProtocol.timeouts.allSatisfy { $0 <= 8.1 })
        XCTAssertTrue(LegacyLoginURLProtocol.postBody?.contains("login_csrf=legacy-login-csrf-token") == true)
        XCTAssertTrue(LegacyLoginURLProtocol.postBody?.contains("password=sicher%20%26%20geheim") == true)
    }

    func testLogoutClearsOnlyServerCookiesWhenRequestFails() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://backup.example.de"))
        let unrelatedURL = try XCTUnwrap(URL(string: "https://notbackup.example.de"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientSessionTests-\(UUID().uuidString)"
        )
        let sessionCookie = try XCTUnwrap(cookie(named: APIClient.sessionCookie, value: "session", domain: "backup.example.de"))
        let csrfCookie = try XCTUnwrap(cookie(named: APIClient.csrfCookie, value: "csrf", domain: "backup.example.de"))
        let unrelatedCookie = try XCTUnwrap(cookie(named: APIClient.sessionCookie, value: "other", domain: "notbackup.example.de"))
        cookieStorage.setCookie(sessionCookie)
        cookieStorage.setCookie(csrfCookie)
        cookieStorage.setCookie(unrelatedCookie)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FailingURLProtocol.self]
        let client = APIClient(
            baseURL: baseURL,
            session: URLSession(configuration: configuration),
            cookieStorage: cookieStorage
        )

        do {
            try await client.logout()
            XCTFail("Logout should surface the network failure")
        } catch {
            XCTAssertEqual((error as? URLError)?.code, .cannotConnectToHost)
        }

        let remainingServerNames = cookieStorage.cookies(for: baseURL)?.map(\.name) ?? []
        XCTAssertFalse(remainingServerNames.contains(APIClient.sessionCookie))
        XCTAssertFalse(remainingServerNames.contains(APIClient.csrfCookie))
        XCTAssertEqual(
            cookieStorage.cookies(for: unrelatedURL)?.first { $0.name == APIClient.sessionCookie }?.value,
            "other"
        )
    }

    private func cookie(named name: String, value: String, domain: String) -> HTTPCookie? {
        HTTPCookie(properties: [
            .domain: domain,
            .path: "/",
            .name: name,
            .value: value,
            .secure: "TRUE"
        ])
    }

    func testPartialLogoutResponseIsReturnedWhileLocalCookiesAreCleared() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://backup.example.de"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientPartialLogoutTests-\(UUID().uuidString)"
        )
        cookieStorage.setCookie(try XCTUnwrap(cookie(
            named: APIClient.sessionCookie,
            value: "session",
            domain: "backup.example.de"
        )))
        cookieStorage.setCookie(try XCTUnwrap(cookie(
            named: APIClient.csrfCookie,
            value: "csrf",
            domain: "backup.example.de"
        )))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [PartialLogoutURLProtocol.self]
        let client = APIClient(
            baseURL: baseURL,
            session: URLSession(configuration: configuration),
            cookieStorage: cookieStorage
        )

        let result = try await client.logout()

        XCTAssertFalse(result.globalRevocation)
        XCTAssertTrue(result.localSessionCleared)
        XCTAssertEqual(result.detail, "Andere Sitzungen bleiben möglicherweise aktiv.")
        let remainingNames = cookieStorage.cookies(for: baseURL)?.map(\.name) ?? []
        XCTAssertFalse(remainingNames.contains(APIClient.sessionCookie))
        XCTAssertFalse(remainingNames.contains(APIClient.csrfCookie))
    }

    func testConfigWriteMapsConflictRevisionPasswordAndValidationPrecisely() async throws {
        let cases: [(String, APIError)] = [
            ("conflict.example", .configConflict(message: "Parallel geändert", currentRevision: "r2")),
            ("revision.example", .configRevisionRequired(message: "Revision fehlt", currentRevision: "r2")),
            ("password.example", .configReauthenticationRequired(message: "Passwort nötig")),
            ("validation.example", .configValidation(errors: ["Name fehlt"]))
        ]
        for (host, expected) in cases {
            let baseURL = try XCTUnwrap(URL(string: "https://\(host)"))
            let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
                forGroupContainerIdentifier: "APIClientConfigTests-\(UUID().uuidString)"
            )
            cookieStorage.setCookie(try XCTUnwrap(cookie(
                named: APIClient.csrfCookie,
                value: "csrf",
                domain: host
            )))
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [ConfigErrorURLProtocol.self]
            let client = APIClient(
                baseURL: baseURL,
                session: URLSession(configuration: configuration),
                cookieStorage: cookieStorage
            )
            let snapshot = ConfigSnapshot(
                revision: "r1",
                backup: BackupConfig(
                    enabled: true,
                    timezone: "Europe/Berlin",
                    defaultSchedule: "manual",
                    pairs: []
                )
            )

            do {
                _ = try await client.updateConfig(snapshot, currentPassword: nil)
                XCTFail("\(host) should fail")
            } catch let error as APIError {
                XCTAssertEqual(error, expected)
            }
        }
    }

    func testRouteSpecificRevisionAndPasswordErrorsRemainActionable() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://operations.example"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientOperationTests-\(UUID().uuidString)"
        )
        cookieStorage.setCookie(try XCTUnwrap(cookie(
            named: APIClient.csrfCookie,
            value: "csrf",
            domain: "operations.example"
        )))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [OperationErrorURLProtocol.self]
        let client = APIClient(
            baseURL: baseURL,
            session: URLSession(configuration: configuration),
            cookieStorage: cookieStorage
        )

        do {
            _ = try await client.saveFilterFile(FilterFileSaveRequest(content: "- *.tmp", revision: "old"))
            XCTFail("Filter conflict expected")
        } catch let error as APIError {
            XCTAssertEqual(error, .revisionConflict(message: "Filter parallel geändert", currentRevision: "new"))
        }

        do {
            _ = try await client.changePassword(current: "wrong", new: "a-new-password")
            XCTFail("Password error expected")
        } catch let error as APIError {
            XCTAssertEqual(error, .reauthenticationRequired(message: "Aktuelles Passwort falsch"))
        }
    }

    func testRunAllDefinitionsUsesCanonicalRouteAndDryRunQuery() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://run-all.example"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientRunAllTests-\(UUID().uuidString)"
        )
        cookieStorage.setCookie(try XCTUnwrap(cookie(
            named: APIClient.csrfCookie,
            value: "csrf",
            domain: "run-all.example"
        )))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RunAllURLProtocol.self]
        let client = APIClient(
            baseURL: baseURL,
            session: URLSession(configuration: configuration),
            cookieStorage: cookieStorage
        )

        let result = try await client.runAllJobDefinitions(dryRun: false)

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.jobID, 91)
        XCTAssertEqual(result.startedDefinitions.map(\.definitionID), ["daily"])
        XCTAssertEqual(result.queuedDefinitions.map(\.definitionID), ["weekly"])
        XCTAssertEqual(result.definitions.map(\.state), ["started", "queued"])
    }

    func testRetryJobUsesCanonicalRevisionSafeRoute() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://retry.example"))
        let cookieStorage = HTTPCookieStorage.sharedCookieStorage(
            forGroupContainerIdentifier: "APIClientRetryTests-\(UUID().uuidString)"
        )
        cookieStorage.setCookie(try XCTUnwrap(cookie(
            named: APIClient.csrfCookie,
            value: "csrf",
            domain: "retry.example"
        )))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RetryJobURLProtocol.self]
        let client = APIClient(
            baseURL: baseURL,
            session: URLSession(configuration: configuration),
            cookieStorage: cookieStorage
        )

        let result = try await client.retryJob(id: 42, dryRun: false)

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.jobID, 43)
    }

    func testEveryStorageSizeRequestUsesServerCompatibleTimeout() async throws {
        StorageTimeoutURLProtocol.timeout = nil
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StorageTimeoutURLProtocol.self]
        let client = APIClient(
            baseURL: try XCTUnwrap(URL(string: "https://storage.example")),
            session: URLSession(configuration: configuration)
        )

        let result = try await client.getStorage(includeSizes: true, forceRefresh: false)

        XCTAssertEqual(result.pairs.count, 0)
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(StorageTimeoutURLProtocol.timeout), 85)
    }

    func testMalformedStorageResponseNamesTheAffectedArea() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MalformedStorageURLProtocol.self]
        let client = APIClient(
            baseURL: try XCTUnwrap(URL(string: "https://storage.example")),
            session: URLSession(configuration: configuration)
        )

        do {
            _ = try await client.getStorage(includeSizes: false, forceRefresh: false)
            XCTFail("Malformed storage response should fail")
        } catch let error as APIError {
            XCTAssertEqual(
                error,
                .incompatibleResponse(resource: "Dateizahlen und Größen")
            )
        }
    }

    func testRemoteBrowserUsesCanonicalRcloneRouteAndPreservesPath() async throws {
        BrowseURLProtocol.requestURL = nil
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BrowseURLProtocol.self]
        let client = APIClient(
            baseURL: try XCTUnwrap(URL(string: "https://browse.example")),
            session: URLSession(configuration: configuration)
        )

        let result = try await client.browseRemote(path: "pcloud:/Fotos & Familie")

        XCTAssertEqual(result.path, "pcloud:/Fotos & Familie")
        let components = try XCTUnwrap(
            BrowseURLProtocol.requestURL.flatMap {
                URLComponents(url: $0, resolvingAgainstBaseURL: false)
            }
        )
        XCTAssertEqual(components.path, "/api/browse/rclone")
        XCTAssertEqual(components.queryItems?.first(where: { $0.name == "path" })?.value, "pcloud:/Fotos & Familie")
    }

    func testReverseProxyBasePathIsNormalizedAndKeptForEveryEndpoint() async throws {
        BasePathURLProtocol.requestURL = nil
        let normalized = try APIClient.normalizedServerURL(
            "https://backup.example.de/rclone/app///?discarded=true#fragment"
        )
        XCTAssertEqual(normalized.absoluteString, "https://backup.example.de/rclone/app")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BasePathURLProtocol.self]
        let client = APIClient(baseURL: normalized, session: URLSession(configuration: configuration))

        _ = try await client.getStorage(includeSizes: false, forceRefresh: false)

        XCTAssertEqual(BasePathURLProtocol.requestURL?.path, "/rclone/app/api/storage/overview")
        XCTAssertEqual(
            URLComponents(url: try XCTUnwrap(BasePathURLProtocol.requestURL), resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "include_remote" })?.value,
            "false"
        )
    }
}

private final class LegacyLoginURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requests: [String] = []
    nonisolated(unsafe) static var timeouts: [TimeInterval] = []
    nonisolated(unsafe) static var postBody: String?
    nonisolated(unsafe) static var cookieStorage: HTTPCookieStorage?

    static func reset(cookieStorage: HTTPCookieStorage) {
        requests = []
        timeouts = []
        postBody = nil
        self.cookieStorage = cookieStorage
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let method = request.httpMethod ?? "GET"
        let path = request.url?.path ?? ""
        Self.requests.append("\(method) \(path)")
        Self.timeouts.append(request.timeoutInterval)

        let status: Int
        let type: String
        let body: Data
        if path == "/api/auth/login" {
            status = 404
            type = "application/json"
            body = Data(#"{"detail":"Not Found"}"#.utf8)
        } else if method == "GET" {
            status = 200
            type = "text/html"
            body = Data(
                #"<form><input type="hidden" name="login_csrf" value="legacy-login-csrf-token"></form>"#.utf8
            )
        } else {
            status = 200
            type = "text/html"
            Self.postBody = Self.bodyData(from: request).flatMap { String(data: $0, encoding: .utf8) }
            if let cookie = HTTPCookie(properties: [
                .domain: request.url?.host ?? "192.168.1.67",
                .path: "/",
                .name: APIClient.sessionCookie,
                .value: "session"
            ]) {
                Self.cookieStorage?.setCookie(cookie)
            }
            body = Data("ok".utf8)
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: ["Content-Type": type]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func bodyData(from request: URLRequest) -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }

        stream.open()
        defer { stream.close() }
        var body = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            body.append(contentsOf: buffer.prefix(count))
        }
        return body
    }
}

private final class RecordingLoginURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requests: [String] = []
    nonisolated(unsafe) static var timeouts: [TimeInterval] = []
    nonisolated(unsafe) static var cookieStorage: HTTPCookieStorage?

    static func reset(cookieStorage: HTTPCookieStorage) {
        requests = []
        timeouts = []
        self.cookieStorage = cookieStorage
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let method = request.httpMethod ?? "GET"
        Self.requests.append("\(method) \(request.url?.path ?? "")")
        Self.timeouts.append(request.timeoutInterval)

        let body: Data
        if method == "POST" {
            if let cookie = HTTPCookie(properties: [
                .domain: request.url?.host ?? "192.168.1.67",
                .path: "/",
                .name: APIClient.sessionCookie,
                .value: "session"
            ]) {
                Self.cookieStorage?.setCookie(cookie)
            }
            body = Data(#"{"status":"success"}"#.utf8)
        } else {
            body = Data(#"{"status":"csrf_ready","login_csrf":"native-login-csrf-token"}"#.utf8)
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class StorageTimeoutURLProtocol: URLProtocol {
    nonisolated(unsafe) static var timeout: TimeInterval?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.timeout = request.timeoutInterval
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"pairs":[]}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class BasePathURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestURL: URL?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requestURL = request.url
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"pairs":[],"measurement":{"state":"loading","total":0,"loaded":0,"failed":0,"stale":0,"measurement_error":null,"measured_at":null}}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class MalformedStorageURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"pairs":"not-an-array"}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class BrowseURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestURL: URL?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requestURL = request.url
        let path = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == "path" })?.value ?? ""
        let body = try! JSONSerialization.data(withJSONObject: [
            "path": path,
            "parent": "pcloud:",
            "is_root": false,
            "entries": [],
            "truncated": false
        ])
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class FailingURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
    }

    override func stopLoading() {}
}

private final class PartialLogoutURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = Data(#"{"ok":false,"partial":true,"global_revocation":false,"local_session_cleared":true,"detail":"Andere Sitzungen bleiben möglicherweise aktiv."}"#.utf8)
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 503,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class ConfigErrorURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let host = request.url?.host ?? ""
        let response: (Int, String)
        switch host {
        case "conflict.example":
            response = (409, #"{"detail":{"message":"Parallel geändert","reload_required":true,"current_revision":"r2"}}"#)
        case "revision.example":
            response = (428, #"{"detail":{"message":"Revision fehlt","reload_required":true,"current_revision":"r2"}}"#)
        case "password.example":
            response = (403, #"{"detail":{"message":"Passwort nötig","reauth_required":true}}"#)
        default:
            response = (422, #"{"detail":{"message":"Ungültig","errors":["Name fehlt"]}}"#)
        }
        let http = HTTPURLResponse(
            url: request.url!,
            statusCode: response.0,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(response.1.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class OperationErrorURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        let response: (Int, String) = path.hasSuffix("/filter-file")
            ? (409, #"{"detail":{"message":"Filter parallel geändert","reload_required":true,"current_revision":"new"}}"#)
            : (403, #"{"detail":"Aktuelles Passwort falsch"}"#)
        let http = HTTPURLResponse(
            url: request.url!, statusCode: response.0, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(response.1.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class RunAllURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }
        let valid = request.httpMethod == "POST"
            && request.url?.path == "/api/jobs/definitions/run-all"
            && components?.queryItems?.first(where: { $0.name == "dry_run" })?.value == "false"
        let status = valid ? 200 : 404
        let body = Data((valid ? #"{"ok":true,"job_id":91,"started_definitions":[{"definition_id":"daily","definition_name":"Täglich","state":"started","job_id":91}],"queued_definitions":[{"definition_id":"weekly","definition_name":"Wöchentlich","state":"queued","job_id":null}],"failed_definitions":[],"definitions":[{"definition_id":"daily","definition_name":"Täglich","state":"started","job_id":91},{"definition_id":"weekly","definition_name":"Wöchentlich","state":"queued","job_id":null}]}"# : #"{"detail":"wrong route"}"#).utf8)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class RetryJobURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }
        let valid = request.httpMethod == "POST"
            && request.url?.path == "/api/jobs/42/retry"
            && components?.queryItems?.first(where: { $0.name == "dry_run" })?.value == "false"
        let status = valid ? 200 : 404
        let body = Data((valid ? #"{"ok":true,"job_id":43}"# : #"{"detail":"wrong route"}"#).utf8)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
