import Foundation
import Darwin
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
        for flow in ["general", "login", "registration", "logout", "vault"] {
            let foreign = try RedirectLoopbackHTTPServer { _ in
                RedirectLoopbackHTTPServer.json("{}")
            }
            defer { foreign.stop() }
            let location = foreign.baseURL.appendingPathComponent("collect").absoluteString
            let origin = try RedirectLoopbackHTTPServer { request in
                if request.hasPrefix("GET /api/auth/login ") {
                    return RedirectLoopbackHTTPServer.json(#"{"status":"csrf_ready","login_csrf":"fixture"}"#)
                }
                return "HTTP/1.1 307 Temporary Redirect\r\nLocation: \(location)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            }
            defer { origin.stop() }
            let baseURL = origin.baseURL
            let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
            jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [
                .name: APIClient.csrfCookie, .value: "fixture-csrf",
                .domain: "127.0.0.1", .path: "/"
            ])))
            let configuration = URLSessionConfiguration.ephemeral
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
            XCTAssertTrue(foreign.requests.isEmpty, "The different-port origin received \(flow): \(foreign.requests)")
            let endpoint = switch flow {
            case "login": "POST /api/auth/login"
            case "registration": "POST /api/webauthn/native/registration/ticket"
            case "logout": "POST /logout"
            case "vault": "GET /api/vault/library/fixture/download"
            default: "GET /api/diagnostics/overview"
            }
            XCTAssertTrue(origin.requests.contains(endpoint), flow)
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

/// Real HTTP redirects exercise CFNetwork, including the per-task delegate on
/// injected sessions. Synthetic URLProtocol redirects cannot model that lifecycle.
private final class RedirectLoopbackHTTPServer: @unchecked Sendable {
    let baseURL: URL
    private let descriptor: Int32
    private let response: @Sendable (String) -> String
    private let lock = NSLock()
    private var running = true
    private var received: [String] = []

    var requests: [String] {
        lock.lock()
        defer { lock.unlock() }
        return received
    }

    init(response: @escaping @Sendable (String) -> String) throws {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw URLError(.cannotConnectToHost) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.getsockname(fd, $0, &length)
            }
        }
        guard bound == 0, named == 0, Darwin.listen(fd, 8) == 0,
              Darwin.fcntl(fd, F_SETFL, O_NONBLOCK) == 0 else {
            Darwin.close(fd)
            throw URLError(.cannotConnectToHost)
        }
        descriptor = fd
        baseURL = URL(string: "http://127.0.0.1:\(UInt16(bigEndian: address.sin_port))")!
        self.response = response
        DispatchQueue(label: "fixture.redirect.\(fd)").async { self.serve() }
    }

    func stop() {
        lock.lock()
        running = false
        lock.unlock()
    }

    private func serve() {
        defer { Darwin.close(descriptor) }
        while true {
            lock.lock()
            let active = running
            lock.unlock()
            guard active else { return }
            var readiness = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            guard Darwin.poll(&readiness, 1, 100) > 0 else { continue }
            let connection = Darwin.accept(descriptor, nil, nil)
            guard connection >= 0 else { continue }
            handle(connection)
            Darwin.close(connection)
        }
    }

    private func handle(_ connection: Int32) {
        _ = Darwin.fcntl(connection, F_SETFL, 0)
        var enabled: Int32 = 1
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(connection, SOL_SOCKET, SO_NOSIGPIPE, &enabled, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var bytes = Data()
        while bytes.count < 64 * 1024 {
            var chunk = [UInt8](repeating: 0, count: 4096)
            let count = chunk.withUnsafeMutableBytes { Darwin.recv(connection, $0.baseAddress, $0.count, 0) }
            guard count > 0 else { return }
            bytes.append(contentsOf: chunk.prefix(count))
            if let end = bytes.range(of: Data("\r\n\r\n".utf8)) {
                let headers = String(decoding: bytes[..<end.lowerBound], as: UTF8.self)
                let line = headers.components(separatedBy: "\r\n").first { $0.lowercased().hasPrefix("content-length:") }
                let size = line.flatMap { Int($0.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)[1].trimmingCharacters(in: .whitespaces)) } ?? 0
                if bytes.count >= end.upperBound + size { break }
            }
        }
        let request = String(decoding: bytes, as: UTF8.self)
        let first = request.components(separatedBy: "\r\n")[0].split(separator: " ").prefix(2).joined(separator: " ")
        lock.lock()
        received.append(first)
        lock.unlock()
        let reply = Data(response(request).utf8)
        reply.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let count = Darwin.send(connection, raw.baseAddress!.advanced(by: offset), raw.count - offset, 0)
                guard count > 0 else { return }
                offset += count
            }
        }
    }

    static func json(_ body: String) -> String {
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
    }
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
