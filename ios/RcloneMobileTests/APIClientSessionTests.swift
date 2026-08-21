import Foundation
import XCTest
@testable import RcloneMobile

final class APIClientSessionTests: XCTestCase {
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
    }
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
        let body = Data((valid ? #"{"ok":true,"job_id":91}"# : #"{"detail":"wrong route"}"#).utf8)
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
