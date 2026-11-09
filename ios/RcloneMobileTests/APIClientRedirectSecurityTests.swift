import Foundation
import XCTest
@testable import RcloneMobile

final class APIClientRedirectSecurityTests: XCTestCase {
    func testOriginPolicyIncludesSchemeHostAndEffectivePortButAllowsProxyPaths() throws {
        let policy = APIOriginPolicy(baseURL: try XCTUnwrap(URL(string: "https://backup.example/proxy")))
        for value in [
            "https://backup.example/another/path?query=1",
            "https://BACKUP.EXAMPLE:443/login",
            "https://backup.example:443/proxy/api/config"
        ] {
            XCTAssertTrue(policy.allows(URL(string: value)), value)
        }
        for value in [
            "https://other.example/proxy", "https://backup.example.attacker.test/",
            "https://backup.example:444/", "http://backup.example/",
            "http://backup.example:443/", "https://user@backup.example/",
            "https://user:password@backup.example/", "https://backup.example:0/",
            "https://backup.example:65536/", "/relative/path", "file:///private/fixture"
        ] {
            XCTAssertFalse(policy.allows(URL(string: value)), value)
        }
        XCTAssertFalse(policy.allows(nil))
        let http = APIOriginPolicy(baseURL: try XCTUnwrap(URL(string: "http://backup.local:80")))
        XCTAssertTrue(http.allows(URL(string: "http://BACKUP.local/proxy")))
        XCTAssertFalse(http.allows(URL(string: "https://backup.local:80/proxy")))
    }

    func testRedirectDelegateRejectsForeignRequestBeforePasswordBodyCanBeReplayed() throws {
        let baseURL = try XCTUnwrap(URL(string: "https://backup.example"))
        let policy = APIOriginPolicy(baseURL: baseURL)
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        var original = URLRequest(url: baseURL.appendingPathComponent("api/auth/login"))
        original.httpMethod = "POST"
        original.httpBody = Data("fixture-password".utf8)
        original.setValue("fixture-cookie", forHTTPHeaderField: "Cookie")
        let task = session.dataTask(with: original)
        defer { task.cancel() }
        for status in [307, 308] {
            let response = try XCTUnwrap(HTTPURLResponse(
                url: original.url!, statusCode: status, httpVersion: nil, headerFields: nil
            ))
            for value in ["https://other.example/", "https://backup.example:444/", "http://backup.example/"] {
                var redirected = original
                redirected.url = try XCTUnwrap(URL(string: value))
                var calls = 0
                policy.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: redirected) { accepted in
                    calls += 1
                    XCTAssertNil(accepted, "The password request must never reach \(value)")
                }
                XCTAssertEqual(calls, 1)
            }
            var sameOrigin = original
            sameOrigin.url = URL(string: "https://backup.example:443/proxy/api/auth/login")
            policy.urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: sameOrigin) { accepted in
                XCTAssertEqual(accepted?.url, sameOrigin.url)
                XCTAssertEqual(accepted?.httpBody, original.httpBody)
            }
        }
    }

    func testAnInvalidBaseOriginNeverAuthorizesARequest() throws {
        for value in ["https://user:password@backup.example/", "file:///private/fixture", "/relative"] {
            let policy = APIOriginPolicy(baseURL: try XCTUnwrap(URL(string: value)))
            XCTAssertFalse(policy.allows(URL(string: "https://backup.example/api/config")))
            XCTAssertThrowsError(try policy.validateRequest(URLRequest(url: URL(string: "https://backup.example/")!))) { error in
                XCTAssertEqual(error as? APIError, .invalidServer)
            }
        }
    }

    func testPublicDNSPrefixesAndGlobalIPv6DefaultToHTTPS() throws {
        for host in [
            "features.example.com", "fdstorage.example.com", "fcstorage.example.com",
            "fe8.example.com", "fe9.example.com", "feb.example.com", "10.backup.0.1.2",
            "[2001:db8::1]", "[fec0::1]"
        ] {
            XCTAssertEqual(try APIClient.normalizedServerURL(host).scheme, "https", host)
        }
    }

    func testActualLANAddressesKeepTheirHTTPDefaultAndExplicitSchemesRemainExplicit() throws {
        for host in [
            "localhost", "backup", "backup.local", "10.0.0.2", "127.0.0.1",
            "172.16.0.1", "172.31.255.254", "192.168.1.2", "169.254.1.2",
            "[::1]", "[fc00::1]", "[fd12:3456::1]", "[fe80::1]", "[febf::1]"
        ] {
            XCTAssertEqual(try APIClient.normalizedServerURL(host).scheme, "http", host)
        }
        XCTAssertEqual(try APIClient.normalizedServerURL("http://features.example.com").scheme, "http")
        XCTAssertEqual(try APIClient.normalizedServerURL("https://192.168.1.2").scheme, "https")
        for value in ["https://user@backup.example", "https://backup.example:0", "https://backup.example:65536"] {
            XCTAssertThrowsError(try APIClient.normalizedServerURL(value), value)
        }
    }

    func testInjectedSessionCannotFollowCrossOriginRedirectsForSensitiveAndDownloadRequests() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://redirect.example"))
        for flow in ["general", "login", "registration", "logout", "vault"] {
            RedirectFixtureURLProtocol.visitedForeignOrigin = false
            RedirectFixtureURLProtocol.requests = []
            let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [
                .name: APIClient.csrfCookie, .value: "fixture-csrf",
                .domain: "redirect.example", .path: "/", .secure: "TRUE"
            ])))
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [RedirectFixtureURLProtocol.self]
            configuration.httpCookieStorage = jar
            // The caller's permissive session delegate must never override the
            // per-task policy installed by APIClient, including download tasks.
            let session = URLSession(configuration: configuration, delegate: AllowAllRedirects(), delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            let client = APIClient(baseURL: baseURL, session: session, loginSession: session, cookieStorage: jar)
            do {
                switch flow {
                case "login": try await client.login(username: "fixture", password: "fixture-password")
                case "registration":
                    _ = try await client.createNativeWebAuthnRegistration(
                        method: "passkey", label: "Fixture", appChallenge: String(repeating: "R", count: 43),
                        currentPassword: "fixture-current-password"
                    )
                case "logout": _ = try await client.logout()
                case "vault": _ = try await client.downloadVaultItem(id: "fixture", filename: "fixture.txt")
                default: _ = try await client.getOverview()
                }
            } catch {
                // A blocked redirect can remain a 307 response or cause a
                // transport error; neither may initiate the foreign request.
            }
            XCTAssertFalse(RedirectFixtureURLProtocol.visitedForeignOrigin, flow)
            let endpoint = switch flow {
            case "login": "POST /api/auth/login"
            case "registration": "POST /api/webauthn/native/registration/ticket"
            case "logout": "POST /logout"
            case "vault": "GET /api/vault/library/fixture/download"
            default: "GET /api/diagnostics/overview"
            }
            XCTAssertTrue(RedirectFixtureURLProtocol.requests.contains(endpoint), flow)
        }
    }

    func testInjectedTransportCannotForgeAnAcceptedFinalResponseFromAnotherOrigin() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://redirect.example"))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ForeignFinalResponseURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let client = APIClient(baseURL: baseURL, session: session)
        do {
            _ = try await client.getOverview()
            XCTFail("A foreign final response must be rejected before decoding")
        } catch {
            XCTAssertEqual(error as? APIError, .invalidResponse)
        }
    }
}

private final class AllowAllRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(request)
    }
}

private final class RedirectFixtureURLProtocol: URLProtocol {
    nonisolated(unsafe) static var visitedForeignOrigin = false
    nonisolated(unsafe) static var requests: [String] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append("\(request.httpMethod ?? "GET") \(request.url?.path ?? "")")
        if request.url?.host != "redirect.example" {
            Self.visitedForeignOrigin = true
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        if request.url?.path == "/api/auth/login", request.httpMethod == "GET" {
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(#"{"status":"csrf_ready","login_csrf":"fixture"}"#.utf8))
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        var redirected = request
        redirected.url = URL(string: "https://foreign.example/collect")!
        let response = HTTPURLResponse(url: request.url!, statusCode: 307, httpVersion: nil,
                                       headerFields: ["Location": redirected.url!.absoluteString])!
        client?.urlProtocol(self, wasRedirectedTo: redirected, redirectResponse: response)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private final class ForeignFinalResponseURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: URL(string: "https://foreign.example/forged")!, statusCode: 200,
                                       httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
