import Foundation
import XCTest
@testable import RcloneMobile

@MainActor
final class ServerOverviewTests: XCTestCase {
    func testStoredBasePathIsNotReinterpretedAsAPastedLoginPage() throws {
        let stored = "https://backup.example/proxy/login"
        XCTAssertEqual(try ServerLoginTarget(address: stored, persistedAddress: stored).address, stored)
        XCTAssertEqual(try ServerLoginTarget(address: stored + "/login", persistedAddress: stored).address, stored)
        XCTAssertEqual(try ServerLoginTarget(address: "https://backup.example/other/login", persistedAddress: stored).address,
            "https://backup.example/other")
    }

    func testResultsAndFailuresRemainBoundToTheirServer() async throws {
        let first = try client(hostname: "NAS zuhause")
        let second = try client(hostname: "NAS extern")
        let failed = StubAPIClient()
        failed.overviewResult = .failure(URLError(.timedOut))
        let clients = ["https://home.example": first, "https://external.example": second, "https://failed.example": failed]
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { clients[$0.absoluteString]! }, hasStoredSession: { _ in true })

        await refresh(store, profiles: clients.keys.sorted().map { profile($0) })

        XCTAssertEqual(store.entries.count, 3)
        XCTAssertEqual(store.entries.first { $0.id == "https://home.example" }?.snapshot?.overview.system.hostname, "NAS zuhause")
        XCTAssertEqual(store.entries.first { $0.id == "https://external.example" }?.snapshot?.overview.system.hostname, "NAS extern")
        let unavailable = try XCTUnwrap(store.entries.first { $0.id == "https://failed.example" })
        XCTAssertNil(unavailable.snapshot)
        XCTAssertFalse(unavailable.isCurrent(at: Date()))
        XCTAssertFalse(store.isRefreshing)
    }

    func testCurrentHTTPClientIsReusedWithoutLoadingStoredCredentials() async throws {
        let active = try client()
        let address = "http://192.168.1.20:8001/app"
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in
            XCTFail("An active session must reuse its own transport")
            return StubAPIClient()
        }, hasStoredSession: { _ in XCTFail("Do not load credentials for active HTTP"); return false })

        await store.refresh(profiles: [profile(address)], activeAddress: address, activeUsername: "admin", activeClient: active)

        XCTAssertEqual(store.entries.first?.state, .loaded)
        XCTAssertEqual(active.overviewCallCount, 1)
    }

    func testHTTPMissingAndSessionOnlyCredentialsRequireLoginWithoutRequests() async {
        let defaults = defaults()
        defaults.set(["https://session-only.example"], forKey: "sessionOnlyServers")
        var requested = [String]()
        let store = ServerOverviewModel(defaults: defaults, clientFactory: {
            requested.append($0.absoluteString)
            return StubAPIClient()
        }, hasStoredSession: { _ in false })

        await refresh(store, profiles: [profile("http://192.168.1.20"), profile("https://missing.example"), profile("https://session-only.example")])

        XCTAssertTrue(requested.isEmpty)
        XCTAssertTrue(store.entries.allSatisfy { $0.state == .needsLogin && $0.snapshot == nil })
    }

    func testExpiredSessionDiscardsItsSnapshotAndDoesNotAffectAnotherServer() async throws {
        let first = try client()
        let second = try client(hostname: "Andere Sicherung")
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { $0.host == "first.example" ? first : second }, hasStoredSession: { _ in true })
        let profiles = [profile("https://first.example"), profile("https://second.example")]
        await refresh(store, profiles: profiles)
        first.overviewResult = .failure(APIError.unauthenticated)

        await refresh(store, profiles: profiles)

        XCTAssertEqual(store.entries[0].state, .needsLogin)
        XCTAssertNil(store.entries[0].snapshot)
        XCTAssertEqual(store.entries[1].state, .loaded)
        XCTAssertEqual(store.entries[1].snapshot?.overview.system.hostname, "Andere Sicherung")
    }

    func testConnectionFailureRetainsOnlyAnExplicitlyOutdatedSnapshot() async throws {
        let client = try client()
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client }, hasStoredSession: { _ in true })
        let profiles = [profile("https://backup.example")]
        await refresh(store, profiles: profiles)
        client.overviewResult = .failure(URLError(.cannotConnectToHost))

        await refresh(store, profiles: profiles)

        let entry = try XCTUnwrap(store.entries.first)
        XCTAssertNotNil(entry.snapshot)
        XCTAssertFalse(entry.isCurrent(at: Date()))
        XCTAssertEqual(entry.status(at: Date()), "Status nicht verfügbar")
    }

    func testMissingRunStatusIsPartialAndNeverReportedAsZeroRunningJobs() async throws {
        let client = try client()
        client.progressResults = [.failure(URLError(.timedOut))]
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client }, hasStoredSession: { _ in true })

        await refresh(store, profiles: [profile("https://backup.example")])

        let snapshot = try XCTUnwrap(store.entries.first?.snapshot)
        XCTAssertTrue(snapshot.missing.contains("Laufstatus"))
        XCTAssertNil(snapshot.runningCount)
        XCTAssertNotEqual(store.entries.first?.status(at: Date()), "Schutz aktiv")
    }

    func testSnapshotsExpireWithoutARefreshAndAfterClockMovesBackwards() async throws {
        let fixture = try StorePreviewData.load(bundle: Bundle(for: Self.self))
        let now = Date()
        let snapshot = ServerOverviewSnapshot(overview: fixture.overview, storage: fixture.storage,
            progress: fixture.progress, pbs: fixture.pbs, lockdown: nil, receivedAt: now, missing: [])
        let entry = ServerOverviewEntry(profile: profile("https://backup.example"), state: .loaded, snapshot: snapshot)

        XCTAssertTrue(entry.isCurrent(at: now.addingTimeInterval(74)))
        XCTAssertFalse(entry.isCurrent(at: now.addingTimeInterval(75)))
        XCTAssertFalse(entry.isCurrent(at: now.addingTimeInterval(-1)))
        XCTAssertEqual(entry.status(at: now.addingTimeInterval(75)), "Stand veraltet")
    }

    func testLateRefreshCannotReplaceANewerServerSnapshot() async throws {
        let old = try client(hostname: "Alter Stand")
        old.suspendOverview = true
        let new = try client(hostname: "Neuer Stand")
        let store = ServerOverviewModel(defaults: defaults())
        let address = "https://backup.example"
        let profiles = [profile(address)]
        let previous = Task {
            await store.refresh(profiles: profiles, activeAddress: address, activeUsername: "admin", activeClient: old)
        }
        for _ in 0..<100 where old.overviewCallCount == 0 { await Task.yield() }
        XCTAssertEqual(old.overviewCallCount, 1)

        await store.refresh(profiles: profiles, activeAddress: address, activeUsername: "admin", activeClient: new)
        old.resumeOverview()
        await previous.value

        XCTAssertEqual(store.entries.first?.snapshot?.overview.system.hostname, "Neuer Stand")
        XCTAssertFalse(store.isRefreshing)
    }

    func testResetPreventsSuspendedResultsFromReappearing() async throws {
        let client = try client()
        client.suspendOverview = true
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client }, hasStoredSession: { _ in true })
        let refresh = Task { await self.refresh(store, profiles: [self.profile("https://backup.example")]) }
        for _ in 0..<100 where client.overviewCallCount == 0 { await Task.yield() }

        store.reset()
        client.resumeOverview()
        await refresh.value

        XCTAssertTrue(store.entries.isEmpty)
        XCTAssertFalse(store.isRefreshing)
    }

    func testAnotherAccountCannotKeepThePreviousAccountsSnapshot() async throws {
        let client = try client()
        var available = true
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client }, hasStoredSession: { _ in available })
        let address = "https://backup.example"
        await refresh(store, profiles: [profile(address)])
        available = false

        await refresh(store, profiles: [profile(address, username: "anderes-konto")])

        XCTAssertNil(store.entries.first?.snapshot)
        XCTAssertEqual(store.entries.first?.state, .needsLogin)
    }

    func testConnectingAnotherServerPreservesTheActiveAppConnection() async throws {
        let defaults = defaults()
        let activeClient = try client(hostname: "Aktiver Server")
        let app = AppModel(defaults: defaults) { _ in activeClient }
        await app.login(server: "https://active.example", username: "admin", password: "fixture")
        let monitoredClient = try client(hostname: "Weiterer Server")
        let store = ServerOverviewModel(defaults: defaults, clientFactory: { _ in monitoredClient }, hasStoredSession: { _ in false })
        let target = try ServerLoginTarget(address: "https://other.example/base/login")

        let connected = await store.connect(target: target, username: "other", password: "fixture",
            rememberSession: false, insecureTransportConfirmed: false, activeAddress: app.serverAddress) { url, username, remember in
                app.rememberOverviewServer(url: url, username: username, rememberSession: remember)
            }
        await refresh(store, profiles: app.savedServerProfiles.filter { $0.address == target.address })

        XCTAssertTrue(connected)
        XCTAssertEqual(app.serverAddress, "https://active.example")
        XCTAssertEqual(app.savedUsername, "admin")
        XCTAssertTrue(app.client === activeClient)
        XCTAssertEqual(app.phase, .signedIn)
        XCTAssertEqual(monitoredClient.persistenceChoices, [false])
        XCTAssertEqual(monitoredClient.persistenceDeferrals, 1)
        XCTAssertEqual(store.entries.first?.state, .loaded, "The session-only connection remains usable in memory")
        XCTAssertEqual(store.entries.first?.snapshot?.overview.system.hostname, "Weiterer Server")
    }

    func testUnconfirmedHTTPAndActiveServerCannotCreateAConnection() async throws {
        var factories = 0
        var accepted = false
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in factories += 1; return StubAPIClient() })
        let http = try ServerLoginTarget(address: "http://192.168.1.20")
        let active = try ServerLoginTarget(address: "https://active.example")
        for target in [http, active] {
            let connected = await store.connect(target: target, username: "admin", password: "fixture",
                rememberSession: true, insecureTransportConfirmed: false, activeAddress: active.address) { _, _, _ in accepted = true }
            XCTAssertFalse(connected)
        }
        XCTAssertEqual(factories, 0)
        XCTAssertFalse(accepted)
    }

    func testPasskeyProfileUsesTheAuthenticatedServerAccount() async throws {
        let client = try client()
        client.baseConfig = ConfigSnapshot(revision: "fixture", backup: BackupConfig(
            enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: []),
            extraSections: ["web": .object(["username": .string("backup-admin")])])
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client },
            hasStoredSession: { _ in false }, browserFactory: { ServerOverviewBrowserStub() })
        let target = try ServerLoginTarget(address: "https://backup.example")
        var account: String?

        let connected = await store.connect(target: target, username: "ungeprüfte-eingabe", password: nil,
            passkeyMethod: "passkey", rememberSession: false, insecureTransportConfirmed: false,
            activeAddress: "https://active.example") { _, username, _ in account = username }
        await refresh(store, profiles: [profile(target.address, username: "backup-admin")])

        XCTAssertTrue(connected)
        XCTAssertEqual(account, "backup-admin")
        XCTAssertEqual(store.entries.first?.state, .loaded)
        XCTAssertEqual(client.configCallCount, 1)
    }

    func testPasskeyWithoutAConfirmedAccountCannotPersistTheSession() async throws {
        let client = try client()
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client },
            browserFactory: { ServerOverviewBrowserStub() })
        let target = try ServerLoginTarget(address: "https://backup.example")
        var accepted = false

        let connected = await store.connect(target: target, username: "admin", password: nil,
            passkeyMethod: "passkey", rememberSession: true, insecureTransportConfirmed: false,
            activeAddress: "https://active.example") { _, _, _ in accepted = true }

        XCTAssertFalse(connected)
        XCTAssertFalse(accepted)
        XCTAssertTrue(client.persistenceChoices.isEmpty)
    }

    func testCancelledAuthenticationCannotStoreAProfileOrSession() async throws {
        let client = try client()
        client.suspendOverview = true
        let store = ServerOverviewModel(defaults: defaults(), clientFactory: { _ in client })
        var accepted = false
        let target = try ServerLoginTarget(address: "https://backup.example")
        let connection = Task {
            await store.connect(target: target, username: "admin", password: "fixture", rememberSession: true,
                insecureTransportConfirmed: false, activeAddress: "https://active.example") { _, _, _ in accepted = true }
        }
        for _ in 0..<100 where client.overviewCallCount == 0 { await Task.yield() }

        store.cancelConnection()
        client.resumeOverview()
        let connected = await connection.value

        XCTAssertFalse(connected)
        XCTAssertFalse(accepted)
        XCTAssertTrue(client.persistenceChoices.isEmpty)
        XCTAssertFalse(store.isConnecting)
    }

    private func refresh(_ store: ServerOverviewModel, profiles: [SavedServerProfile]) async {
        await store.refresh(profiles: profiles, activeAddress: "", activeUsername: "", activeClient: nil)
    }

    private func profile(_ address: String, username: String = "admin") -> SavedServerProfile {
        SavedServerProfile(name: address, address: address, username: username, lastUsedAt: 0)
    }

    private func defaults() -> UserDefaults {
        UserDefaults(suiteName: "ServerOverviewTests-\(UUID().uuidString)")!
    }

    private func client(hostname: String = "Sicherpfad") throws -> StubAPIClient {
        let fixture = try StorePreviewData.load(bundle: Bundle(for: Self.self))
        let old = fixture.overview.system
        let system = SystemSnapshot(hostname: hostname, platform: old.platform, kernel: old.kernel,
            python: old.python, virtualization: old.virtualization, addresses: old.addresses,
            uptimeSeconds: old.uptimeSeconds, cpu: old.cpu, memory: old.memory, pids: old.pids, dataDisk: old.dataDisk)
        let overview = OverviewResponse(app: fixture.overview.app, system: system, services: fixture.overview.services,
            pairs: fixture.overview.pairs, jobs: fixture.overview.jobs, alerts: fixture.overview.alerts, generatedAt: Date().timeIntervalSince1970)
        let client = StubAPIClient()
        client.overviewResult = .success(overview)
        client.baseStorage = fixture.storage
        return client
    }
}

@MainActor
private final class ServerOverviewBrowserStub: WebAuthnBrowserSessionProtocol {
    func authenticate(baseURL: URL, method: String) async throws -> WebAuthnExchange {
        WebAuthnExchange(token: "fixture-exchange", verifier: "fixture-verifier")
    }
    func makeRegistrationBinding() throws -> WebAuthnRegistrationBinding { throw APIError.invalidResponse }
    func register(baseURL: URL, method: String, token: String, binding: WebAuthnRegistrationBinding) async throws {
        throw APIError.invalidResponse
    }
    func cancel() {}
}
