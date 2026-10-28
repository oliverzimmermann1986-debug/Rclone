import Foundation
import XCTest
@testable import RcloneMobile

@MainActor
final class PushOriginSecurityTests: XCTestCase {
    private let serverA = String(repeating: "a", count: 64)
    private let serverB = String(repeating: "b", count: 64)

    func testRegistrationResponseKeepsOlderServersCompatibleButUnbound() throws {
        let legacy = try JSONDecoder().decode(PushRegistrationResponse.self, from: Data(#"{"ok":true}"#.utf8))
        XCTAssertTrue(legacy.ok)
        XCTAssertNil(legacy.serverID)
        let current = try JSONDecoder().decode(
            PushRegistrationResponse.self,
            from: Data("{\"ok\":true,\"server_id\":\"\(serverA)\"}".utf8)
        )
        XCTAssertEqual(current.serverID, serverA)
        XCTAssertNil(PushRegistrationResponse(ok: true).serverID)
    }

    func testPushBindingRequiresIdentityExactOriginAndCurrentSession() throws {
        let binding = try XCTUnwrap(PushServerBinding(serverID: serverA, origin: "https://a.example/base", session: 1))
        XCTAssertTrue(binding.matches(serverID: serverA, origin: "https://a.example/base", session: 1))
        XCTAssertFalse(binding.matches(serverID: serverB, origin: "https://a.example/base", session: 1))
        XCTAssertFalse(binding.matches(serverID: nil, origin: "https://a.example/base", session: 1))
        XCTAssertFalse(binding.matches(serverID: serverA, origin: "http://a.example/base", session: 1))
        XCTAssertFalse(binding.matches(serverID: serverA, origin: "https://a.example/other", session: 1))
        XCTAssertFalse(binding.matches(serverID: serverA, origin: "https://a.example/base", session: 2))
        XCTAssertNil(PushServerBinding(serverID: "old-message", origin: "https://a.example", session: 1))
    }

    func testQueuedPushRecoveryNavigationCannotMoveToAnotherProfile() {
        var pending = PendingAppNavigation()
        pending.request(.recovery, serverID: serverA)
        XCTAssertNil(pending.consume(isSignedIn: true, currentServerID: serverB))
        XCTAssertNil(pending.destination)
        pending.request(.recovery, serverID: serverA)
        XCTAssertEqual(pending.consume(isSignedIn: true, currentServerID: serverA), .pushRecoveryNavigationRequested)
    }

    func testDeferredPauseCannotUseClientSelectedAfterTheClick() async throws {
        let first = PushOriginClient(serverID: serverA)
        let second = PushOriginClient(serverID: serverB)
        let model = makeModel(first: first, second: second)
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        await model.registerPushDevice(token: "fixture-token", environment: "production")
        XCTAssertTrue(model.canHandlePush(serverID: serverA))

        let pause = try XCTUnwrap(model.pauseSchedulerFromPush(serverID: serverA))
        // login invalidates the captured generation before it reaches its first await.
        await model.login(server: "https://b.example", username: "admin", password: "fixture")
        await model.registerPushDevice(token: "fixture-token", environment: "production")
        await pause.value

        XCTAssertLessThanOrEqual(first.pauseCalls, 1)
        XCTAssertEqual(second.pauseCalls, 0)
        XCTAssertFalse(model.canHandlePush(serverID: serverA))
        XCTAssertNil(model.pauseSchedulerFromPush(serverID: serverA))
        XCTAssertNil(model.pauseSchedulerFromPush(serverID: nil))
        let currentPause = try XCTUnwrap(model.pauseSchedulerFromPush(serverID: serverB))
        await currentPause.value
        XCTAssertEqual(second.pauseCalls, 1)
        XCTAssertLessThanOrEqual(first.pauseCalls, 1)
        await model.logout()
        XCTAssertNil(model.currentPushServerID)
    }

    func testLateRegistrationFromOldSessionCannotBindNewServer() async {
        let first = PushOriginClient(serverID: serverA)
        first.holdRegistration = true
        let second = PushOriginClient(serverID: serverB)
        let model = makeModel(first: first, second: second)
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        let registration = Task { await model.registerPushDevice(token: "fixture-token", environment: "production") }
        for _ in 0..<200 where !first.registrationStarted { await Task.yield() }
        XCTAssertTrue(first.registrationStarted)

        await model.login(server: "https://b.example", username: "admin", password: "fixture")
        first.releaseRegistration()
        await registration.value

        XCTAssertNil(model.currentPushServerID)
        XCTAssertFalse(model.canHandlePush(serverID: serverA))
        await model.registerPushDevice(token: "fixture-token", environment: "production")
        XCTAssertEqual(model.currentPushServerID, serverB)
        XCTAssertFalse(model.canHandlePush(serverID: serverA))
    }

    func testOldLogoutCompletionPreservesNewServerPushRegistration() async throws {
        let first = PushOriginClient(serverID: serverA)
        let second = PushOriginClient(serverID: serverB)
        let model = makeModel(first: first, second: second)
        await model.login(server: "https://a.example", username: "admin", password: "fixture")
        await model.registerPushDevice(token: "old-token", environment: "production")
        first.holdLogout = true
        let logout = Task { await model.logout() }
        for _ in 0..<100 where !first.logoutStarted { await Task.yield() }
        XCTAssertTrue(first.logoutStarted)
        XCTAssertEqual(model.phase, .signedOut)

        await model.login(server: "https://b.example", username: "admin", password: "fixture")
        await model.registerPushDevice(token: "new-token", environment: "production")
        model.errorMessage = "new-session-message"
        first.releaseLogout()
        await logout.value

        XCTAssertEqual(model.phase, .signedIn)
        XCTAssertEqual(model.errorMessage, "new-session-message")
        XCTAssertEqual(model.currentPushServerID, serverB)
        await model.registerPushDevice(token: "new-token", environment: "production")
        XCTAssertEqual(second.registrationCalls, 1, "An old logout must not clear the new registered token")
    }

    private func makeModel(first: PushOriginClient, second: PushOriginClient) -> AppModel {
        let defaults = UserDefaults(suiteName: "PushOriginSecurityTests-\(UUID().uuidString)")!
        return AppModel(defaults: defaults) { url in
            url.host == "a.example" ? first : second
        }
    }
}

@MainActor
final class PushOriginClient: APIClientProtocol {
    let serverID: String
    var pauseCalls = 0
    var loginCalls = 0
    var holdRegistration = false
    var registrationStarted = false
    var registrationCalls = 0
    var holdLogout = false
    var logoutStarted = false
    var holdTicket = false
    var ticketStarted = false
    var holdUnregister = false
    var unregisterCalls = 0
    var unregisteredTokens: [String] = []
    var persistenceDeferrals = 0
    var persistenceChoices: [Bool] = []
    private var registration: CheckedContinuation<Void, Never>?
    private var logoutContinuation: CheckedContinuation<Void, Never>?
    private var ticketContinuation: CheckedContinuation<Void, Never>?
    private var unregisterContinuation: CheckedContinuation<Void, Never>?

    init(serverID: String) { self.serverID = serverID }
    func login(username: String, password: String) async throws { loginCalls += 1 }
    func exchangeWebAuthnToken(_ token: String, verifier: String) async throws {}
    func deferSessionPersistenceUntilAccepted() { persistenceDeferrals += 1 }
    func setSessionPersistence(_ enabled: Bool) { persistenceChoices.append(enabled) }
    nonisolated func clearLocalSession() {}
    func getConfig() async throws -> ConfigSnapshot {
        ConfigSnapshot(revision: "fixture", backup: BackupConfig(enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: []))
    }
    func getOverview() async throws -> OverviewResponse { throw APIError.invalidResponse }
    func getStorage(includeSizes: Bool, forceRefresh: Bool) async throws -> StorageOverview { throw APIError.invalidResponse }
    func getJobDefinitions() async throws -> [JobDefinition] { [] }
    func updateConfig(_ config: ConfigSnapshot, currentPassword: String?) async throws -> ConfigSaveResponse { throw APIError.invalidResponse }
    func getJobDefinitionPlan(id: String, dryRun: Bool) async throws -> JobPlan { throw APIError.invalidResponse }
    func runJobDefinition(id: String, dryRun: Bool) async throws -> ActionResponse { throw APIError.invalidResponse }
    func runAllJobDefinitions(dryRun: Bool) async throws -> ActionResponse { throw APIError.invalidResponse }
    func runQuickSync(_ request: QuickSyncRequest) async throws -> ActionResponse { throw APIError.invalidResponse }
    func checkPair(name: String) async throws -> ActionResponse { throw APIError.invalidResponse }
    func runRestoreTest(pair: String?) async throws -> ActionResponse { throw APIError.invalidResponse }
    func browseLocal(path: String) async throws -> BrowseResponse { throw APIError.invalidResponse }
    func browseRemote(path: String) async throws -> BrowseResponse { throw APIError.invalidResponse }
    func getAuditEvents(limit: Int) async throws -> AuditResponse { throw APIError.invalidResponse }
    func getMaintenanceLogs(limit: Int) async throws -> MaintenanceLogsResponse { throw APIError.invalidResponse }
    func getDatabaseStatus() async throws -> DatabaseStatus { throw APIError.invalidResponse }
    func pruneDatabase(days: Int, keepLatest: Int) async throws -> DatabasePruneResponse { throw APIError.invalidResponse }
    func getConfigSnapshots() async throws -> SnapshotListResponse { throw APIError.invalidResponse }
    func createConfigSnapshot() async throws -> SnapshotCreateResponse { throw APIError.invalidResponse }
    func restoreConfigSnapshot(_ request: SnapshotRestoreRequest) async throws -> SnapshotRestoreResponse { throw APIError.invalidResponse }
    func getFilterFile() async throws -> FilterFile { throw APIError.invalidResponse }
    func saveFilterFile(_ request: FilterFileSaveRequest) async throws -> FilterFileSaveResponse { throw APIError.invalidResponse }
    func changePassword(current: String, new: String) async throws -> PasswordChangeResponse { throw APIError.invalidResponse }
    func downloadSupportBundle() async throws -> URL { throw APIError.invalidResponse }
    func getJobs(limit: Int) async throws -> JobSearchResponse { throw APIError.invalidResponse }
    func searchJobs(kind: String?, status: String?, query: String, limit: Int, offset: Int) async throws -> JobSearchResponse { throw APIError.invalidResponse }
    func downloadJobsCSV(kind: String?, status: String?, query: String) async throws -> URL { throw APIError.invalidResponse }
    func getJob(id: Int) async throws -> JobRecord { throw APIError.invalidResponse }
    func getJobLog(id: Int) async throws -> JobLogResponse { throw APIError.invalidResponse }
    func downloadJobLog(id: Int) async throws -> URL { throw APIError.invalidResponse }
    func retryJob(id: Int, dryRun: Bool) async throws -> ActionResponse { throw APIError.invalidResponse }
    func getDoctor() async throws -> DoctorResponse { throw APIError.invalidResponse }
    func getProgress() async throws -> BackupProgress { throw APIError.invalidResponse }
    func getPBSStatus() async throws -> PBSStatus { throw APIError.invalidResponse }
    func runBackup(pair: String?, dryRun: Bool) async throws -> ActionResponse { throw APIError.invalidResponse }
    func cancelBackup() async throws -> ActionResponse { throw APIError.invalidResponse }
    func runPBS(target: String?) async throws -> ActionResponse { throw APIError.invalidResponse }
    func cancelPBS() async throws -> ActionResponse { throw APIError.invalidResponse }
    func pauseScheduler(minutes: Int) async throws -> SchedulerControl {
        pauseCalls += 1
        return SchedulerControl(paused: true, until: nil, reason: nil, actor: "fixture")
    }
    func resumeScheduler() async throws -> SchedulerControl { throw APIError.invalidResponse }
    func registerPushDevice(token: String, environment: String, appVersion: String) async throws -> PushRegistrationResponse {
        registrationCalls += 1
        registrationStarted = true
        if holdRegistration { await withCheckedContinuation { registration = $0 } }
        return PushRegistrationResponse(ok: true, serverID: serverID)
    }
    func unregisterPushDevice(token: String) async throws -> PushRegistrationResponse {
        unregisterCalls += 1
        if holdUnregister { await withCheckedContinuation { unregisterContinuation = $0 } }
        unregisteredTokens.append(token)
        return PushRegistrationResponse(ok: true)
    }
    func releaseUnregister() {
        holdUnregister = false
        unregisterContinuation?.resume()
        unregisterContinuation = nil
    }
    func logout() async throws -> LogoutResult {
        logoutStarted = true
        if holdLogout { await withCheckedContinuation { logoutContinuation = $0 } }
        return LogoutResult(globalRevocation: true, localSessionCleared: true, detail: nil)
    }
    func createNativeWebAuthnRegistration(
        method: String, label: String, appChallenge: String, currentPassword: String
    ) async throws -> NativeWebAuthnRegistrationTicket {
        ticketStarted = true
        if holdTicket { await withCheckedContinuation { ticketContinuation = $0 } }
        return try JSONDecoder().decode(
            NativeWebAuthnRegistrationTicket.self,
            from: Data(#"{"registration_token":"fixture-ticket","expires_in_seconds":300}"#.utf8)
        )
    }
    func releaseTicket() {
        holdTicket = false
        ticketContinuation?.resume()
        ticketContinuation = nil
    }
    func releaseLogout() {
        holdLogout = false
        logoutContinuation?.resume()
        logoutContinuation = nil
    }
    func releaseRegistration() {
        holdRegistration = false
        registration?.resume()
        registration = nil
    }
}
