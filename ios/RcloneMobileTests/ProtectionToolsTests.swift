import Foundation
import XCTest
@testable import RcloneMobile

final class ProtectionToolsTests: XCTestCase {
    func testVersionSearchEncodesExactQueryIdentityAndCursor() throws {
        let request = ProtectionToolRequest.search(query: "Fotos + Urlaub & März.pdf", identity: "path/id?&", cursor: "opaque+cursor/=")
        let components = try XCTUnwrap(URLComponents(string: request.path))
        let query = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value ?? "") })
        XCTAssertEqual(components.path, "/api/recovery/files/search")
        XCTAssertEqual(query["query"], "Fotos + Urlaub & März.pdf")
        XCTAssertEqual(query["identity"], "path/id?&")
        XCTAssertEqual(query["cursor"], "opaque+cursor/=")
        XCTAssertFalse(try XCTUnwrap(components.percentEncodedQuery).contains("+"))
        XCTAssertNil(request.body)
    }

    func testSensitiveRequestsBindPasswordAndRevisionWithoutPuttingThemInURL() throws {
        let password = "fixture private credential"
        let requests: [ProtectionToolRequest] = [
            .switchMigration(id: "plan/../?token", revision: "revision-current", password: password),
            .runMigration(id: "plan", password: password),
            .saveOperations(revision: "revision-current", heartbeat: ["enabled": .bool(true)], report: nil, password: password),
            .sendReport(id: 7, password: password),
            .testHeartbeat(password: password)
        ]
        for request in requests {
            XCTAssertNotEqual(request.method, "GET")
            XCTAssertFalse(request.path.contains(password))
            XCTAssertFalse(request.path.contains("revision-current"))
            let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request.body)) as? [String: Any]
            XCTAssertEqual(object?["current_password"] as? String, password)
        }
        XCTAssertFalse(requests[0].path.contains("/../"))
        let switchBody = try XCTUnwrap(requests[0].body)
        XCTAssertEqual(switchBody["expected_revision"], .string("revision-current"))
    }

    func testUnknownConfigurationFieldsSurviveForecastOnlyEdit() throws {
        let original = Data(#"{"_revision":"r1","backup":{"future_guard":{"active":true},"pairs":[],"storage_forecast":{"warning_days":30}},"future_section":{"version":8}}"#.utf8)
        var document = try JSONDecoder().decode(ProtectionToolDocument.self, from: original)
        var backup = document.object("backup")
        var settings = backup.object("storage_forecast")
        settings.values["warning_days"] = .number(45)
        backup.values["storage_forecast"] = .object(settings.values)
        document.values["backup"] = .object(backup.values)
        let body = ProtectionToolRequest.saveConfiguration(document, password: "fixture").body
        let encoded = try JSONEncoder().encode(body)
        let roundTrip = try JSONDecoder().decode(ProtectionToolDocument.self, from: encoded).object("config")
        XCTAssertEqual(roundTrip.object("backup").object("future_guard").bool("active"), true)
        XCTAssertEqual(roundTrip.object("future_section").integer("version"), 8)
        XCTAssertEqual(roundTrip.string("_revision"), "r1")
        XCTAssertEqual(roundTrip.object("backup").object("storage_forecast").integer("warning_days"), 45)
    }

    func testCapacityRemovalIsExplicitNullAndNumericHelpersRejectOversizedCounts() throws {
        let request = ProtectionToolRequest.setCapacity(identity: "path:remote", capacityBytes: nil, revision: "r2")
        XCTAssertEqual(request.method, "PUT")
        XCTAssertEqual(request.body?["capacity_bytes"], .null)
        XCTAssertEqual(request.body?["expected_revision"], .string("r2"))
        let large = ProtectionToolRequest.setCapacity(identity: "path:remote", capacityBytes: 1 << 60, revision: "r2")
        let data = try XCTUnwrap(large.encodedBody())
        XCTAssertTrue(String(decoding: data, as: UTF8.self).contains("1152921504606846976"))
        let cleared = try XCTUnwrap(request.encodedBody())
        XCTAssertTrue(String(decoding: cleared, as: UTF8.self).contains("\"capacity_bytes\":null"))
        let value = ProtectionToolDocument(["count": .number(1e40), "fraction": .number(1.5)])
        XCTAssertNil(value.integer("count"))
        XCTAssertNil(value.integer("fraction"))
        XCTAssertEqual(ProtectionToolLabels.bytes(1e40), "–")
    }

    func testNewToolsReuseAuthenticatedCSRFAndSessionErrorTransport() async throws {
        let fixture = try makeClient()
        defer { fixture.session.invalidateAndCancel() }
        ProtectionToolsURLProtocol.reset()
        let running = try await fixture.client.getCurrentJobs()
        XCTAssertEqual(running.migration?.id, 47)
        XCTAssertEqual(running.backupScopeJobs.first?.kindLabel, "Zielwechsel")
        _ = try await fixture.client.protectionTool(.forecasts)
        _ = try await fixture.client.protectionTool(.testHeartbeat(password: "fixture"))
        let request = try XCTUnwrap(ProtectionToolsURLProtocol.requests.last)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-CSRF-Token"), "fixture-csrf")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "https://tools.example")
        XCTAssertEqual(request.url?.path, "/api/operations/heartbeat/test")
        ProtectionToolsURLProtocol.status = 401
        do {
            _ = try await fixture.client.protectionTool(.weeklyHistory)
            XCTFail("An expired session must fail through the existing transport")
        } catch {
            XCTAssertEqual(error as? APIError, .unauthenticated)
        }
        XCTAssertFalse((fixture.cookies.cookies(for: fixture.baseURL) ?? []).contains { $0.name == APIClient.sessionCookie })
    }

    func testReportPDFUsesProtectedOwnedExportAndIsRemovedOnLogout() async throws {
        let fixture = try makeClient()
        defer { fixture.session.invalidateAndCancel() }
        ProtectionToolsURLProtocol.reset()
        let file = try await fixture.client.downloadProtectionReport(id: 7)
        XCTAssertTrue(try Data(contentsOf: file).starts(with: Data("%PDF-1.4".utf8)))
        XCTAssertTrue(file.deletingLastPathComponent().lastPathComponent.hasPrefix("sicherpfad-export-"))
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        fixture.client.clearLocalSession()
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
    }

    func testToolJSONAndPDFRejectForgedForeignFinalResponses() async throws {
        let fixture = try makeClient()
        defer { fixture.session.invalidateAndCancel() }
        ProtectionToolsURLProtocol.reset()
        ProtectionToolsURLProtocol.foreignResponse = true
        do {
            _ = try await fixture.client.protectionTool(.forecasts)
            XCTFail("Foreign tool JSON must be rejected")
        } catch { XCTAssertEqual(error as? APIError, .invalidResponse) }
        do {
            _ = try await fixture.client.downloadProtectionReport(id: 7)
            XCTFail("Foreign PDF responses must be rejected before export")
        } catch { XCTAssertEqual(error as? APIError, .invalidResponse) }
    }

    private func makeClient() throws -> (client: APIClient, session: URLSession, cookies: HTTPCookieStorage, baseURL: URL) {
        let baseURL = try XCTUnwrap(URL(string: "https://tools.example"))
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        for (name, value) in [(APIClient.csrfCookie, "fixture-csrf"), (APIClient.sessionCookie, "fixture-session")] {
            jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [.domain: "tools.example", .path: "/", .name: name, .value: value])))
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProtectionToolsURLProtocol.self]
        configuration.httpCookieStorage = jar
        let session = URLSession(configuration: configuration)
        return (APIClient(baseURL: baseURL, session: session, cookieStorage: jar), session, jar, baseURL)
    }
}

private final class ProtectionToolsURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var status = 200
    nonisolated(unsafe) static var foreignResponse = false
    static func reset() { requests = []; status = 200; foreignResponse = false }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        let pdf = request.url?.path.hasSuffix("/pdf") == true
        let url = Self.foreignResponse ? URL(string: "https://foreign.example/forged")! : request.url!
        let response = HTTPURLResponse(url: url, statusCode: Self.status, httpVersion: nil, headerFields: ["Content-Type": pdf ? "application/pdf" : "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        let document = request.url?.path == "/api/jobs/status/current"
            ? #"{"migration":{"id":47,"kind":"migration","status":"running","started_at":1720000000}}"#
            : #"{"ok":true,"items":[]}"#
        client?.urlProtocol(self, didLoad: pdf ? Data("%PDF-1.4\nfixture".utf8) : Data(document.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
