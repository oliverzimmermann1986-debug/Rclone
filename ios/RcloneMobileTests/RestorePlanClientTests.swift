import Foundation
import XCTest
@testable import RcloneMobile

final class RestorePlanClientTests: XCTestCase {
    private let revision = String(repeating: "a", count: 64)
    private let savedRevision = String(repeating: "b", count: 64)

    func testGetUsesProxyPrefixAndDecodesTheRealHTTPResponse() async throws {
        let fixture = try makeClient()
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        let plan = try await fixture.client.getRestorePlan()
        let recorded = try XCTUnwrap(RestorePlanURLProtocol.requests(host: fixture.host).first)
        XCTAssertEqual(recorded.request.httpMethod, "GET")
        XCTAssertEqual(recorded.request.url?.path, "/sicherpfad/api/recovery/restore-plan")
        XCTAssertNil(recorded.body)
        XCTAssertEqual(plan.revision, revision)
        XCTAssertEqual(plan.timezone, "Europe/Berlin")
        XCTAssertEqual(plan.generatedAt, 1_791_025_200)
        XCTAssertEqual(plan.dataPaths.first?.id, "photos-path")
        XCTAssertEqual(plan.dataPaths.first?.evidenceState, "never")
        XCTAssertTrue(plan.dataPaths.first?.coverageGap == true)
    }

    func testPreviewPostsRevisionBoundSettingsWithCSRFAndOrigin() async throws {
        let fixture = try makeClient()
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        _ = try await fixture.client.previewRestorePlan(update())
        let recorded = try XCTUnwrap(RestorePlanURLProtocol.requests(host: fixture.host).first)
        XCTAssertEqual(recorded.request.httpMethod, "POST")
        XCTAssertEqual(recorded.request.url?.path, "/sicherpfad/api/recovery/restore-plan/preview")
        XCTAssertEqual(recorded.request.value(forHTTPHeaderField: "X-CSRF-Token"), "csrf-test")
        XCTAssertEqual(recorded.request.value(forHTTPHeaderField: "Origin"), "https://\(fixture.host)")
        try assertRequestBody(recorded.body)
    }

    func testSavePutsTheLoadedRevisionAndReturnsTheNewRevision() async throws {
        let fixture = try makeClient(responseRevision: savedRevision)
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        let result = try await fixture.client.saveRestorePlan(update())
        let recorded = try XCTUnwrap(RestorePlanURLProtocol.requests(host: fixture.host).first)
        XCTAssertEqual(recorded.request.httpMethod, "PUT")
        XCTAssertEqual(recorded.request.url?.path, "/sicherpfad/api/recovery/restore-plan")
        XCTAssertEqual(recorded.request.value(forHTTPHeaderField: "X-CSRF-Token"), "csrf-test")
        XCTAssertEqual(recorded.request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        try assertRequestBody(recorded.body)
        XCTAssertEqual(result.revision, savedRevision)
    }

    func testPreviewUnauthorizedResponseClearsSessionAndCSRFCookies() async throws {
        let fixture = try makeClient(status: 401, errorMessage: "Session expired")
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        do {
            _ = try await fixture.client.previewRestorePlan(update())
            XCTFail("An expired session must reject the preview")
        } catch let error as APIError { XCTAssertEqual(error, .unauthenticated) }
        let names = Set(fixture.cookies.cookies(for: fixture.baseURL)?.map(\.name) ?? [])
        XCTAssertFalse(names.contains(APIClient.sessionCookie))
        XCTAssertFalse(names.contains(APIClient.csrfCookie))
        XCTAssertEqual(RestorePlanURLProtocol.requests(host: fixture.host).count, 1)
    }

    func testMissingEndpointIsReportedAsUnsupportedFor404And405() async throws {
        for status in [404, 405] {
            let fixture = try makeClient(status: status, errorMessage: "Not Found")
            defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
            do {
                _ = try await fixture.client.getRestorePlan()
                XCTFail("A missing restore-plan endpoint must not decode as an empty plan")
            } catch let error as APIError {
                XCTAssertEqual(error, .serverFeatureUnavailable(feature: "Restore-Prüfplan"))
            }
            XCTAssertEqual(RestorePlanURLProtocol.requests(host: fixture.host).count, 1)
        }
    }

    func testRevisionConflictRejectsSaveAndRetainsServerReloadMessage() async throws {
        let message = "Konfiguration wurde parallel geändert. Plan neu laden."
        let fixture = try makeClient(status: 409, errorMessage: message)
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        do {
            _ = try await fixture.client.saveRestorePlan(update())
            XCTFail("A stale revision must fail; the client must not retry against a newer revision")
        } catch let error as APIError {
            XCTAssertEqual(error, .server(status: 409, message: message))
        }
        XCTAssertEqual(RestorePlanURLProtocol.requests(host: fixture.host).count, 1)
        XCTAssertTrue(fixture.cookies.cookies(for: fixture.baseURL)?.contains { $0.name == APIClient.sessionCookie } == true)
    }

    func testMissingCSRFBlocksPreviewAndSaveBeforeSendingRequests() async throws {
        let fixture = try makeClient(includeCSRF: false)
        defer { fixture.session.invalidateAndCancel(); RestorePlanURLProtocol.remove(host: fixture.host) }
        do {
            _ = try await fixture.client.previewRestorePlan(update())
            XCTFail("Preview requires CSRF")
        } catch let error as APIError { XCTAssertEqual(error, .missingCSRF) }
        do {
            _ = try await fixture.client.saveRestorePlan(update())
            XCTFail("Save requires CSRF")
        } catch let error as APIError { XCTAssertEqual(error, .missingCSRF) }
        XCTAssertTrue(RestorePlanURLProtocol.requests(host: fixture.host).isEmpty)
    }

    private func update() -> RestorePlanUpdate {
        RestorePlanUpdate(revision: revision,
            settings: RestorePlanSettings(enabled: true, schedule: "15 5 * * 0,3",
                                          sampleFiles: 24, maxTotalMB: 384, maxScanFiles: 37777))
    }

    private func assertRequestBody(_ data: Data?, file: StaticString = #filePath, line: UInt = #line) throws {
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(data)) as? [String: Any])
        let settings = try XCTUnwrap(object["settings"] as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["revision", "settings"], file: file, line: line)
        XCTAssertEqual(object["revision"] as? String, revision, file: file, line: line)
        XCTAssertEqual(Set(settings.keys), ["enabled", "schedule", "sample_files", "max_total_mb", "max_scan_files"], file: file, line: line)
        XCTAssertEqual(settings["enabled"] as? Bool, true, file: file, line: line)
        XCTAssertEqual(settings["schedule"] as? String, "15 5 * * 0,3", file: file, line: line)
        XCTAssertEqual(settings["sample_files"] as? Int, 24, file: file, line: line)
        XCTAssertEqual(settings["max_total_mb"] as? Int, 384, file: file, line: line)
        XCTAssertEqual(settings["max_scan_files"] as? Int, 37777, file: file, line: line)
    }

    private func makeClient(status: Int = 200, errorMessage: String? = nil,
                            responseRevision: String? = nil, includeCSRF: Bool = true) throws -> RestorePlanClientFixture {
        let host = "restore-plan-\(UUID().uuidString.lowercased()).example"
        let baseURL = try XCTUnwrap(URL(string: "https://\(host)/sicherpfad"))
        let cookies = HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "RestorePlanClient-\(UUID().uuidString)")
        for (name, value) in [(APIClient.sessionCookie, "session-test"), (APIClient.csrfCookie, "csrf-test")] {
            if name == APIClient.csrfCookie && !includeCSRF { continue }
            cookies.setCookie(try XCTUnwrap(HTTPCookie(properties: [.name: name, .value: value, .domain: host, .path: "/"])))
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RestorePlanURLProtocol.self]
        configuration.httpCookieStorage = cookies
        let session = URLSession(configuration: configuration)
        let response: [String: Any]
        if let errorMessage { response = ["detail": errorMessage] }
        else {
            response = [
                "revision": responseRevision ?? revision,
                "settings": ["enabled": true, "schedule": "15 5 * * 0,3", "sample_files": 24,
                             "max_total_mb": 384, "max_scan_files": 37777],
                "timezone": "Europe/Berlin", "generated_at": 1_791_025_200,
                "next_runs": [1_791_083_700, 1_791_342_900], "due_now": false,
                "data_paths": [["id": "photos-path", "name": "Fotos", "evidence_state": "never",
                                "valid_until": NSNull(), "coverage_gap": true]],
                "warnings": ["Fotos: Kein Nachweis"]
            ]
        }
        RestorePlanURLProtocol.configure(host: host, status: status, body: try JSONSerialization.data(withJSONObject: response))
        return RestorePlanClientFixture(client: APIClient(baseURL: baseURL, session: session, cookieStorage: cookies),
                                        session: session, cookies: cookies, baseURL: baseURL, host: host)
    }
}

private struct RestorePlanClientFixture {
    let client: APIClient
    let session: URLSession
    let cookies: HTTPCookieStorage
    let baseURL: URL
    let host: String
}

private final class RestorePlanURLProtocol: URLProtocol {
    struct RecordedRequest {
        let request: URLRequest
        let body: Data?
    }
    private struct Scenario {
        let status: Int
        let body: Data
        var requests: [RecordedRequest] = []
    }
    private static let lock = NSLock()
    private static var scenarios: [String: Scenario] = [:]

    static func configure(host: String, status: Int, body: Data) {
        lock.lock(); defer { lock.unlock() }
        scenarios[host] = Scenario(status: status, body: body)
    }
    static func requests(host: String) -> [RecordedRequest] {
        lock.lock(); defer { lock.unlock() }
        return scenarios[host]?.requests ?? []
    }
    static func remove(host: String) {
        lock.lock(); defer { lock.unlock() }
        scenarios.removeValue(forKey: host)
    }
    override class func canInit(with request: URLRequest) -> Bool {
        guard let host = request.url?.host else { return false }
        lock.lock(); defer { lock.unlock() }
        return scenarios[host] != nil
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = request.httpBody ?? request.httpBodyStream.flatMap { stream in
            stream.open(); defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                guard count > 0 else { break }
                data.append(contentsOf: buffer.prefix(count))
            }
            return data
        }
        Self.lock.lock()
        let host = request.url?.host ?? ""
        guard var scenario = Self.scenarios[host] else {
            Self.lock.unlock()
            client?.urlProtocol(self, didFailWithError: URLError(.resourceUnavailable))
            return
        }
        scenario.requests.append(RecordedRequest(request: request, body: body))
        Self.scenarios[host] = scenario
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: scenario.status,
                                       httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: scenario.body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
