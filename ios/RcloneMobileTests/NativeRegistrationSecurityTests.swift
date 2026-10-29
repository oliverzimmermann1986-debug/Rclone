import Foundation
import XCTest
@testable import RcloneMobile

final class NativeRegistrationSecurityTests: XCTestCase {
    func testNativeTicketRequestCarriesCurrentPasswordWithCSRF() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://registration.example"))
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [
            .name: APIClient.csrfCookie,
            .value: "fixture-csrf",
            .domain: "registration.example",
            .path: "/",
            .secure: "TRUE"
        ])))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = jar
        configuration.protocolClasses = [NativeRegistrationURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        NativeRegistrationURLProtocol.receivedRequest = nil
        NativeRegistrationURLProtocol.receivedBody = nil
        let client = APIClient(
            baseURL: baseURL,
            session: session,
            cookieStorage: jar,
            sessionPersistence: RegistrationSessionStore()
        )

        let ticket = try await client.createNativeWebAuthnRegistration(
            method: "passkey",
            label: "Fixture iPhone",
            appChallenge: String(repeating: "R", count: 43),
            currentPassword: "fixture-current-password"
        )

        let request = try XCTUnwrap(NativeRegistrationURLProtocol.receivedRequest)
        XCTAssertEqual(request.url?.path, "/api/webauthn/native/registration/ticket")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-CSRF-Token"), "fixture-csrf")
        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: XCTUnwrap(NativeRegistrationURLProtocol.receivedBody)) as? [String: String]
        )
        XCTAssertEqual(body["current_password"], "fixture-current-password")
        XCTAssertEqual(body["app_challenge"], String(repeating: "R", count: 43))
        XCTAssertEqual(body["method"], "passkey")
        XCTAssertEqual(ticket.registrationToken, "fixture-registration-token")
    }
}

@MainActor
final class NativeRegistrationLifecycleTests: XCTestCase {
    func testProfileChangeWhileTicketWaitsCannotOpenOldRegistrationBrowser() async throws {
        let first = PushOriginClient(serverID: String(repeating: "a", count: 64))
        first.holdTicket = true
        let second = PushOriginClient(serverID: String(repeating: "b", count: 64))
        let browser = RegistrationBrowserStub()
        let model = makeModel(first: first, second: second, browsers: [browser])
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        let registration = Task {
            try await model.registerWebAuthnCredential(method: "passkey", label: "Fixture", currentPassword: "fixture")
        }
        for _ in 0..<100 where !first.ticketStarted { await Task.yield() }
        XCTAssertTrue(first.ticketStarted)

        await model.login(server: "https://b.example", username: "admin", password: "fixture")
        first.releaseTicket()
        await assertCancelled(registration)

        XCTAssertEqual(browser.cancelCalls, 1)
        XCTAssertFalse(browser.registrationStarted)
        XCTAssertEqual(model.serverAddress, "https://b.example")
    }

    func testLateOldBrowserResultCannotSucceedOrClearNewBrowserOwner() async throws {
        let first = PushOriginClient(serverID: String(repeating: "a", count: 64))
        let second = PushOriginClient(serverID: String(repeating: "b", count: 64))
        let oldBrowser = RegistrationBrowserStub()
        let newBrowser = RegistrationBrowserStub()
        let model = makeModel(first: first, second: second, browsers: [oldBrowser, newBrowser])
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        let oldRegistration = Task {
            try await model.registerWebAuthnCredential(method: "passkey", label: "Old", currentPassword: "fixture")
        }
        for _ in 0..<100 where !oldBrowser.registrationStarted { await Task.yield() }
        XCTAssertTrue(oldBrowser.registrationStarted)

        await model.login(server: "https://b.example", username: "admin", password: "fixture")
        let newRegistration = Task {
            try await model.registerWebAuthnCredential(method: "passkey", label: "New", currentPassword: "fixture")
        }
        for _ in 0..<100 where !newBrowser.registrationStarted { await Task.yield() }
        XCTAssertTrue(newBrowser.registrationStarted)
        oldBrowser.releaseRegistration()
        await assertCancelled(oldRegistration)
        XCTAssertEqual(newBrowser.cancelCalls, 0)

        await model.logout()
        XCTAssertEqual(newBrowser.cancelCalls, 1, "The old task must leave the new browser tracked for cancellation")
        newBrowser.releaseRegistration()
        await assertCancelled(newRegistration)
    }

    func testSupersededTicketInSameSessionCannotReplaceNewBrowser() async throws {
        let client = PushOriginClient(serverID: String(repeating: "a", count: 64))
        let oldBrowser = RegistrationBrowserStub()
        let newBrowser = RegistrationBrowserStub()
        let model = makeModel(first: client, second: client, browsers: [oldBrowser, newBrowser])
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        client.holdTicket = true
        let oldRegistration = Task {
            try await model.registerWebAuthnCredential(method: "passkey", label: "Old", currentPassword: "fixture")
        }
        for _ in 0..<100 where !client.ticketStarted { await Task.yield() }
        XCTAssertTrue(client.ticketStarted)
        client.holdTicket = false
        let newRegistration = Task {
            try await model.registerWebAuthnCredential(method: "passkey", label: "New", currentPassword: "fixture")
        }
        for _ in 0..<100 where !newBrowser.registrationStarted { await Task.yield() }
        XCTAssertTrue(newBrowser.registrationStarted)

        client.releaseTicket()
        await assertCancelled(oldRegistration)
        XCTAssertFalse(oldBrowser.registrationStarted)
        XCTAssertEqual(newBrowser.cancelCalls, 0)
        await model.logout()
        XCTAssertEqual(newBrowser.cancelCalls, 1)
        newBrowser.releaseRegistration()
        await assertCancelled(newRegistration)
    }

    func testWebAuthnLoginPersistsOnlyAfterRevocationAndCurrentSessionAcceptance() async throws {
        for cancel in [false, true] {
            let client = PushOriginClient(serverID: String(repeating: "a", count: 64))
            client.holdUnregister = true
            let browser = RegistrationBrowserStub()
            let defaults = UserDefaults(suiteName: "NativeRegistrationLifecycle-\(UUID().uuidString)")!
            defaults.set(try JSONSerialization.data(withJSONObject: [
                ["server": "https://a.example", "token": "old-token"]
            ]), forKey: "pendingPushRevocations")
            let model = AppModel(defaults: defaults, webAuthnBrowserFactory: { browser }) { _ in client }
            let login = Task { await model.loginWithWebAuthn(server: "https://a.example", method: "passkey") }
            for _ in 0..<100 where client.unregisterCalls == 0 { await Task.yield() }
            XCTAssertEqual(client.unregisterCalls, 1)
            XCTAssertEqual(client.persistenceDeferrals, 1)
            XCTAssertTrue(client.persistenceChoices.isEmpty)
            XCTAssertNil(model.client)

            if cancel { login.cancel() }
            client.releaseUnregister()
            await login.value

            XCTAssertEqual(client.persistenceChoices, cancel ? [] : [true])
            XCTAssertEqual(model.phase == .signedIn, !cancel)
        }
    }

    func testSameServerActivationWaitsForLogoutBeforeClientCreationWhileOtherServerProceeds() async throws {
        for activation in ["password", "webauthn", "restore"] {
            let first = PushOriginClient(serverID: String(repeating: "a", count: 64))
            let replacement = PushOriginClient(serverID: String(repeating: "a", count: 64))
            let other = PushOriginClient(serverID: String(repeating: "b", count: 64))
            let browser = RegistrationBrowserStub()
            let defaults = UserDefaults(suiteName: "NativeLogoutBarrier-\(UUID().uuidString)")!
            var serverAClients = 0
            let model = AppModel(defaults: defaults, webAuthnBrowserFactory: { browser }) { url in
                guard url.host?.lowercased() == "a.example" else { return other }
                serverAClients += 1
                return serverAClients == 1 ? first : replacement
            }
            await model.login(server: "https://A.example:443/", username: "admin", password: "fixture")
            await model.registerPushDevice(token: "same-device-token", environment: "production")
            first.holdUnregister = true
            let logout = Task { await model.logout() }
            for _ in 0..<100 where first.unregisterCalls == 0 { await Task.yield() }
            XCTAssertEqual(first.unregisterCalls, 1)
            XCTAssertTrue(first.unregisteredTokens.isEmpty)

            await model.login(server: "https://b.example", username: "admin", password: "fixture")
            XCTAssertEqual(model.serverAddress, "https://b.example")
            XCTAssertEqual(model.phase, .signedIn, "Another server must remain usable during A cleanup")
            if activation == "restore" { defaults.set("https://a.example", forKey: "serverAddress") }
            let activate = Task {
                switch activation {
                case "restore": await model.restoreSession()
                case "webauthn": await model.loginWithWebAuthn(server: "https://a.example", method: "passkey")
                default: await model.login(server: "https://a.example", username: "admin", password: "fixture")
                }
            }
            for _ in 0..<100 where !model.isRefreshing { await Task.yield() }
            XCTAssertTrue(model.isRefreshing)
            XCTAssertEqual(serverAClients, 1, "A client must not load persisted cookies until the old logout finishes")
            XCTAssertEqual(replacement.loginCalls, 0)
            XCTAssertTrue(replacement.persistenceChoices.isEmpty)

            first.releaseUnregister()
            await logout.value
            await activate.value
            XCTAssertTrue(first.logoutStarted)
            XCTAssertEqual(first.unregisteredTokens, ["same-device-token"])
            XCTAssertEqual(serverAClients, 2)
            XCTAssertEqual(model.phase, .signedIn)
            await model.registerPushDevice(token: "same-device-token", environment: "production")
            XCTAssertEqual(replacement.registrationCalls, 1)
        }
    }

    private func assertCancelled(_ task: Task<Void, Error>, file: StaticString = #filePath, line: UInt = #line) async {
        do {
            try await task.value
            XCTFail("Superseded registration must not succeed", file: file, line: line)
        } catch {
            XCTAssertTrue(error is CancellationError, file: file, line: line)
        }
    }

    private func makeModel(
        first: PushOriginClient, second: PushOriginClient, browsers: [RegistrationBrowserStub]
    ) -> AppModel {
        let defaults = UserDefaults(suiteName: "NativeRegistrationLifecycle-\(UUID().uuidString)")!
        var remaining = browsers
        return AppModel(defaults: defaults, webAuthnBrowserFactory: { remaining.removeFirst() }) { url in
            url.host == "a.example" ? first : second
        }
    }
}

@MainActor
private final class RegistrationBrowserStub: WebAuthnBrowserSessionProtocol {
    var registrationStarted = false
    var cancelCalls = 0
    private var registrationContinuation: CheckedContinuation<Void, Never>?

    func makeRegistrationBinding() throws -> WebAuthnRegistrationBinding {
        WebAuthnRegistrationBinding(verifier: "fixture-verifier", appChallenge: "fixture-challenge")
    }

    func authenticate(baseURL: URL, method: String) async throws -> WebAuthnExchange {
        WebAuthnExchange(token: "fixture-token", verifier: "fixture-verifier")
    }

    func register(baseURL: URL, method: String, token: String, binding: WebAuthnRegistrationBinding) async throws {
        registrationStarted = true
        await withCheckedContinuation { registrationContinuation = $0 }
    }

    func cancel() { cancelCalls += 1 }

    func releaseRegistration() {
        registrationContinuation?.resume()
        registrationContinuation = nil
    }
}

private final class RegistrationSessionStore: SessionPersisting {
    func load(for server: URL) -> [HTTPCookie] { [] }
    func save(_ cookies: [HTTPCookie], for server: URL) {}
    func remove(for server: URL) {}
}

private final class NativeRegistrationURLProtocol: URLProtocol {
    nonisolated(unsafe) static var receivedRequest: URLRequest?
    nonisolated(unsafe) static var receivedBody: Data?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.receivedRequest = request
        if let body = request.httpBody {
            Self.receivedBody = body
        } else if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var result = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                result.append(contentsOf: buffer.prefix(count))
            }
            Self.receivedBody = result
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(
            self,
            didLoad: Data(#"{"registration_token":"fixture-registration-token","expires_in_seconds":300}"#.utf8)
        )
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
