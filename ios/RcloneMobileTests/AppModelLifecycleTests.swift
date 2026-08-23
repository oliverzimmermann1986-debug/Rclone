import Foundation
import XCTest
@testable import RcloneMobile

@MainActor
final class AppModelLifecycleTests: XCTestCase {
    func testRestoreSessionKeepsConnectionErrorForLoginUI() async {
        let defaults = makeDefaults()
        defaults.set("https://backup.example.de", forKey: "serverAddress")
        let client = StubAPIClient()
        client.configError = URLError(.timedOut)
        let model = AppModel(defaults: defaults) { _ in client }

        await model.restoreSession()

        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(model.errorMessage?.contains("Zeitlimits") == true)
    }

    func testRefreshPublishesSuccessfulEndpointsWhenOthersFail() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let model = AppModel(defaults: defaults) { _ in client }

        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        XCTAssertEqual(model.phase, .signedIn)
        XCTAssertEqual(model.config?.revision, "revision-1")
        XCTAssertNotNil(model.storage)
        XCTAssertNotNil(model.progress)
        XCTAssertNotNil(model.pbs)
        XCTAssertNotNil(model.errorMessage, "The independent overview failure should remain visible")
        XCTAssertEqual(model.overviewState, .failed("Overview nicht verfügbar"))
        XCTAssertEqual(model.storageState, .loaded)
        XCTAssertEqual(model.configState, .loaded)
        XCTAssertEqual(model.jobsState, .loaded)
        XCTAssertFalse(model.isRefreshing)
    }

    func testPBSFailurePublishesRetryableLoadState() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.pbsError = APIError.server(status: 503, message: "PBS nicht erreichbar")
        let model = AppModel(defaults: defaults) { _ in client }

        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        XCTAssertNil(model.pbs)
        XCTAssertEqual(model.pbsState, .failed("PBS nicht erreichbar"))
    }

    func testStartupRestoreCanBeCancelledWithoutWaitingForNetworkTimeout() {
        let defaults = makeDefaults()
        defaults.set("https://backup.example.de", forKey: "serverAddress")
        let model = AppModel(defaults: defaults) { _ in StubAPIClient() }

        model.cancelSessionRestore()

        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(model.errorMessage?.contains("abgebrochen") == true)
    }

    func testPartialGlobalLogoutRemainsVisibleAfterImmediateLocalSignOut() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.logoutResult = LogoutResult(
            globalRevocation: false,
            localSessionCleared: true,
            detail: "Andere Sitzungen konnten nicht widerrufen werden."
        )
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        await model.logout()

        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(client.clearedLocalSession)
        XCTAssertEqual(model.errorMessage, "Andere Sitzungen konnten nicht widerrufen werden.")
    }

    func testOfflinePushRevocationIsPersistedAndRetriedOnNextLogin() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        await model.registerPushDevice(token: String(repeating: "ab", count: 32), environment: "production")
        client.unregisterPushError = URLError(.notConnectedToInternet)

        await model.logout()

        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(model.errorMessage?.contains("Push-Registrierung") == true)
        XCTAssertNotNil(defaults.data(forKey: "pendingPushRevocations"))
        XCTAssertEqual(client.unregisterPushCallCount, 1)

        client.unregisterPushError = nil
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        XCTAssertEqual(client.unregisterPushCallCount, 2)
        XCTAssertNil(defaults.data(forKey: "pendingPushRevocations"))
    }

    func testProgressBecomesStaleAndCompletionRefreshesJobsWithoutFirstRowHeuristic() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.progressResults = [
            .success(.fixture(running: true)),
            .failure(URLError(.timedOut)),
            .failure(URLError(.timedOut)),
            .failure(URLError(.timedOut)),
            .success(.fixture(running: false))
        ]
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        XCTAssertEqual(client.jobsCallCount, 1)

        await model.refreshProgress()
        await model.refreshProgress()
        await model.refreshProgress()

        XCTAssertEqual(model.progressConsecutiveFailures, 3)
        XCTAssertTrue(model.progressIsStale)
        XCTAssertTrue(model.progress?.running == true)
        XCTAssertEqual(client.jobsCallCount, 1)

        await model.refreshProgress()

        XCTAssertFalse(model.progressIsStale)
        XCTAssertTrue(model.progress?.running == false)
        XCTAssertEqual(client.jobsCallCount, 2)
    }

    func testDoctorRecordsLastCheckedAndSupportsRepeatedChecks() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        await model.refreshDoctor()
        let first = model.doctorLastCheckedAt
        await model.refreshDoctor()

        XCTAssertNotNil(first)
        XCTAssertNotNil(model.doctor)
        XCTAssertEqual(client.doctorCallCount, 2)
        XCTAssertFalse(model.doctorIsRefreshing)
    }

    func testConfigurationSavePublishesCanonicalServerSnapshot() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        let pair = PairConfig(
            stableID: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            name: "Fotos", local: "/mnt/fotos", remote: "cloud:Fotos"
        )
        let definition = JobDefinition(
            id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            name: "Fotos täglich",
            enabled: true,
            dataPathIDs: [pair.id],
            schedule: "0 3 * * *",
            executionMode: "sequential",
            maxParallel: 1,
            retryMinutes: 60
        )

        let saved = await model.saveConfiguration(pairs: [pair], definitions: [definition])

        XCTAssertTrue(saved)
        XCTAssertEqual(client.updatedConfig?.revision, "revision-1")
        XCTAssertEqual(client.updatedConfig?.backup.jobs.first?.id, definition.id)
        XCTAssertEqual(model.jobDefinitions.first?.id, definition.id)
        XCTAssertNil(model.configSaveIssue)
    }

    func testConfigurationConflictIsExposedWithoutReplacingDraftBase() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.updateConfigError = APIError.configConflict(
            message: "Parallel geändert",
            currentRevision: "revision-2"
        )
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        let saved = await model.saveConfiguration(pairs: [], definitions: [])

        XCTAssertFalse(saved)
        XCTAssertEqual(model.configSaveIssue, .conflict("Parallel geändert"))
        XCTAssertEqual(model.config?.revision, "revision-1")
    }

    func testDirtyRevisionOneDraftCannotSaveAfterBackgroundRefreshPublishesRevisionTwo() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        let draftBaseRevision = try? XCTUnwrap(model.config?.revision)
        client.baseConfig = ConfigSnapshot(
            revision: "revision-2",
            backup: BackupConfig(enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: [])
        )

        await model.reloadConfiguration()
        let saved = await model.saveConfiguration(
            pairs: [], definitions: [], baseRevision: draftBaseRevision
        )

        XCTAssertFalse(saved)
        XCTAssertNil(client.updatedConfig)
        XCTAssertEqual(model.config?.revision, "revision-2")
        XCTAssertEqual(
            model.configSaveIssue,
            .conflict("Die Serverkonfiguration wurde geändert, während dieser Entwurf offen war. Lade den Serverstand neu und übernimm deine Änderungen erneut.")
        )
    }

    func testPasswordChangeImmediatelyClearsSessionAndRequestsFreshLogin() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.passwordChangeResponse = PasswordChangeResponse(
            ok: true,
            message: "Passwort geändert",
            reauthenticate: true
        )
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        let changed = await model.changePassword(current: "secret", new: "a-new-long-password")

        XCTAssertTrue(changed)
        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(client.clearedLocalSession)
        XCTAssertTrue(model.errorMessage?.contains("erneut") == true)
    }

    func testRunAllDefinitionsUsesDedicatedCanonicalClientCall() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.runAllResponse = ActionResponse(ok: true, jobID: 91, error: nil)
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        let started = await model.runAllJobDefinitions()

        XCTAssertTrue(started)
        XCTAssertEqual(client.runAllCallCount, 1)
        XCTAssertEqual(model.actionMessage, "Alle aktiven Jobs wurden gestartet.")
        await model.logout()
    }

    func testBatchResponseIsPublishedAndVisibleSourcesRefreshImmediately() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let started = BatchDefinitionState(
            definitionID: "daily", definitionName: "Täglich", state: "started", jobID: 91
        )
        let queued = BatchDefinitionState(
            definitionID: "weekly", definitionName: "Wöchentlich", state: "queued", jobID: nil
        )
        client.runAllResponse = ActionResponse(
            ok: true, jobID: 91, error: nil,
            startedDefinitions: [started], queuedDefinitions: [queued],
            definitions: [started, queued]
        )
        let model = AppModel(defaults: defaults, runPollInterval: .milliseconds(10)) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        let callsBeforeRun = client.jobsCallCount
        client.progressResults = [.success(.fixture(running: true)), .success(.fixture(running: false))]
        client.jobResults = [
            JobSearchResponse(
                items: [.fixture(id: 91, status: "running", definitionID: "daily")],
                total: 1, limit: 100, offset: 0
            ),
            JobSearchResponse(
                items: [
                    .fixture(id: 91, status: "ok", definitionID: "daily"),
                    .fixture(id: 92, status: "ok", definitionID: "weekly")
                ],
                total: 2, limit: 100, offset: 0
            )
        ]

        let startedBatch = await model.runAllJobDefinitions()

        XCTAssertTrue(startedBatch)
        XCTAssertEqual(model.batchDefinitions.map(\.definitionID), ["daily", "weekly"])
        XCTAssertGreaterThan(client.jobsCallCount, callsBeforeRun)
        XCTAssertNotNil(model.storage)
        var attempts = 0
        while model.batchIsRunning && attempts < 50 {
            try? await Task.sleep(for: .milliseconds(10))
            attempts += 1
        }
        XCTAssertFalse(model.batchIsRunning)
        XCTAssertEqual(model.batchDefinitions.map(\.state), ["ok", "ok"])
        await model.logout()
    }

    func testAuthenticatedAction401EndsSessionCentrally() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.runAllError = APIError.unauthenticated
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        let startedBatch = await model.runAllJobDefinitions()

        XCTAssertFalse(startedBatch)
        XCTAssertEqual(model.phase, .signedOut)
        XCTAssertTrue(client.clearedLocalSession)
    }

    func testStoragePartialAndTimeoutStatesKeepLastUsefulTimestamp() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.detailedStorage = StorageOverview(
            pairs: [],
            measurement: StorageMeasurementSummary(
                state: "partial", total: 4, loaded: 2, failed: 2, stale: 0,
                measurementError: "2 von 4 Messungen nutzbar", measuredAt: 1_720_000_000
            )
        )
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        XCTAssertEqual(model.storageSizeState.status, .partial)
        XCTAssertEqual(model.storageSizeState.message, "2 von 4 Messungen nutzbar")
        let lastUpdated = model.storageSizeState.lastUpdated

        client.detailedStorage = nil
        client.detailedStorageError = URLError(.timedOut)
        await model.refreshStorageSizes()

        XCTAssertEqual(model.storageSizeState.status, .stale)
        XCTAssertEqual(model.storageSizeState.lastUpdated, lastUpdated)
        XCTAssertTrue(model.storageSizeState.message?.contains("Zeitlimits") == true)
    }

    func testPushRotationAndLogoutRevokeEveryKnownTokenAfterInflightRegistration() async throws {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.pushDelay = .milliseconds(40)
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")
        let first = String(repeating: "a", count: 64)
        let second = String(repeating: "b", count: 64)

        let firstRegistration = Task { await model.registerPushDevice(token: first, environment: "production") }
        try await Task.sleep(for: .milliseconds(5))
        let secondRegistration = Task { await model.registerPushDevice(token: second, environment: "production") }
        try await Task.sleep(for: .milliseconds(5))
        let logout = Task { await model.logout() }
        await Task.yield()
        XCTAssertEqual(model.phase, .signedOut, "Die lokale Ansicht muss nicht auf Netzwerk-Widerrufe warten")
        await firstRegistration.value
        await secondRegistration.value
        await logout.value

        XCTAssertFalse(client.registeredPushTokens.isEmpty)
        XCTAssertTrue(Set(client.registeredPushTokens).isSubset(of: Set([first, second])))
        XCTAssertTrue(Set(client.registeredPushTokens).isSubset(of: Set(client.unregisteredPushTokens)))
        XCTAssertNil(defaults.data(forKey: "pendingPushRevocations"))
    }

    func testRetryJobUsesDedicatedClientCallAndKeepsPushNavigationTarget() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        client.retryResponse = ActionResponse(ok: true, jobID: 43, error: nil)
        let model = AppModel(defaults: defaults) { _ in client }
        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        model.requestRunNavigation(id: 42)
        let started = await model.retryJob(id: 42)

        XCTAssertTrue(started)
        XCTAssertEqual(client.retriedJobIDs, [42])
        XCTAssertEqual(model.actionMessage, "Job wurde erneut gestartet.")
        XCTAssertEqual(model.requestedRunID, 42)
        model.consumeRequestedRun(id: 42)
        XCTAssertNil(model.requestedRunID)
    }

    func testJobDefinitionsAreAcceptedOnlyFromRevisionBoundConfigSnapshot() async {
        let defaults = makeDefaults()
        let client = StubAPIClient()
        let definition = JobDefinition(
            id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            name: "Revisionstreu",
            enabled: true,
            dataPathIDs: ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
            schedule: "manual",
            executionMode: "sequential",
            maxParallel: 1,
            retryMinutes: 60
        )
        client.baseConfig = ConfigSnapshot(
            revision: "revision-bound",
            backup: BackupConfig(
                enabled: true,
                timezone: "Europe/Berlin",
                defaultSchedule: nil,
                pairs: [],
                jobs: [definition]
            )
        )
        let model = AppModel(defaults: defaults) { _ in client }

        await model.login(server: "https://backup.example.de", username: "admin", password: "secret")

        XCTAssertEqual(model.jobDefinitions.first?.name, "Revisionstreu")
        XCTAssertEqual(client.definitionsCallCount, 0)
    }

    private func makeDefaults() -> UserDefaults {
        let suite = "AppModelLifecycleTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }
}

private final class StubAPIClient: APIClientProtocol {
    var configError: Error?
    var logoutResult = LogoutResult(
        globalRevocation: true,
        localSessionCleared: true,
        detail: nil
    )
    var progressResults: [Result<BackupProgress, Error>] = []
    var jobResults: [JobSearchResponse] = []
    var updateConfigError: Error?
    var passwordChangeResponse: PasswordChangeResponse?
    var runAllResponse: ActionResponse?
    var runAllError: Error?
    var retryResponse: ActionResponse?
    var pbsError: Error?
    var unregisterPushError: Error?
    var detailedStorage: StorageOverview?
    var detailedStorageError: Error?
    var pushDelay: Duration?
    var baseConfig: ConfigSnapshot?
    private(set) var updatedConfig: ConfigSnapshot?
    private(set) var clearedLocalSession = false
    private(set) var jobsCallCount = 0
    private(set) var doctorCallCount = 0
    private(set) var runAllCallCount = 0
    private(set) var retriedJobIDs: [Int] = []
    private(set) var definitionsCallCount = 0
    private(set) var unregisterPushCallCount = 0
    private(set) var registeredPushTokens: [String] = []
    private(set) var unregisteredPushTokens: [String] = []

    func login(username: String, password: String) async throws {}

    func getOverview() async throws -> OverviewResponse {
        throw APIError.server(status: 503, message: "Overview nicht verfügbar")
    }

    func getStorage(includeSizes: Bool, forceRefresh: Bool) async throws -> StorageOverview {
        if includeSizes {
            if let detailedStorageError { throw detailedStorageError }
            if let detailedStorage { return detailedStorage }
            throw URLError(.timedOut)
        }
        return StorageOverview(pairs: [])
    }

    func getConfig() async throws -> ConfigSnapshot {
        if let configError { throw configError }
        if let updatedConfig { return updatedConfig }
        if let baseConfig { return baseConfig }
        return ConfigSnapshot(
            revision: "revision-1",
            backup: BackupConfig(enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: [])
        )
    }

    func getJobDefinitions() async throws -> [JobDefinition] {
        definitionsCallCount += 1
        return updatedConfig?.backup.jobs ?? []
    }

    func updateConfig(
        _ config: ConfigSnapshot,
        currentPassword: String?
    ) async throws -> ConfigSaveResponse {
        if let updateConfigError { throw updateConfigError }
        updatedConfig = config
        return ConfigSaveResponse(ok: true, warnings: [], config: config)
    }

    func getJobDefinitionPlan(id: String, dryRun: Bool) async throws -> JobPlan {
        throw APIError.invalidResponse
    }

    func runJobDefinition(id: String, dryRun: Bool) async throws -> ActionResponse {
        throw APIError.invalidResponse
    }
    func runAllJobDefinitions(dryRun: Bool) async throws -> ActionResponse {
        runAllCallCount += 1
        if let runAllError { throw runAllError }
        guard let runAllResponse else { throw APIError.invalidResponse }
        return runAllResponse
    }

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
    func changePassword(current: String, new: String) async throws -> PasswordChangeResponse {
        guard let passwordChangeResponse else { throw APIError.invalidResponse }
        return passwordChangeResponse
    }
    func downloadSupportBundle() async throws -> URL { throw APIError.invalidResponse }

    func getJobs(limit: Int) async throws -> JobSearchResponse {
        jobsCallCount += 1
        if !jobResults.isEmpty { return jobResults.removeFirst() }
        return JobSearchResponse(items: [], total: 0, limit: limit, offset: 0)
    }
    func searchJobs(kind: String?, status: String?, query: String, limit: Int, offset: Int) async throws -> JobSearchResponse {
        try await getJobs(limit: limit)
    }
    func downloadJobsCSV(kind: String?, status: String?, query: String) async throws -> URL { throw APIError.invalidResponse }

    func getJob(id: Int) async throws -> JobRecord { throw APIError.invalidResponse }
    func getJobLog(id: Int) async throws -> JobLogResponse { throw APIError.invalidResponse }
    func downloadJobLog(id: Int) async throws -> URL { throw APIError.invalidResponse }
    func retryJob(id: Int, dryRun: Bool) async throws -> ActionResponse {
        retriedJobIDs.append(id)
        guard let retryResponse else { throw APIError.invalidResponse }
        return retryResponse
    }
    func getDoctor() async throws -> DoctorResponse {
        doctorCallCount += 1
        return DoctorResponse(ok: true, level: "ok", checks: [], generatedAt: 1_720_000_000)
    }

    func getProgress() async throws -> BackupProgress {
        if !progressResults.isEmpty {
            return try progressResults.removeFirst().get()
        }
        return .fixture(running: false)
    }

    func getPBSStatus() async throws -> PBSStatus {
        if let pbsError { throw pbsError }
        return PBSStatus(
            enabled: false,
            clientAvailable: false,
            repository: "",
            namespace: "",
            running: false,
            runningJob: nil,
            targets: []
        )
    }

    func runBackup(pair: String?, dryRun: Bool) async throws -> ActionResponse { throw APIError.invalidResponse }
    func cancelBackup() async throws -> ActionResponse { throw APIError.invalidResponse }
    func runPBS(target: String?) async throws -> ActionResponse { throw APIError.invalidResponse }
    func cancelPBS() async throws -> ActionResponse { throw APIError.invalidResponse }
    func pauseScheduler(minutes: Int) async throws -> SchedulerControl { throw APIError.invalidResponse }
    func resumeScheduler() async throws -> SchedulerControl { throw APIError.invalidResponse }
    func registerPushDevice(token: String, environment: String, appVersion: String) async throws -> PushRegistrationResponse {
        if let pushDelay { try await Task.sleep(for: pushDelay) }
        registeredPushTokens.append(token)
        return PushRegistrationResponse(ok: true)
    }
    func unregisterPushDevice(token: String) async throws -> PushRegistrationResponse {
        unregisterPushCallCount += 1
        if let unregisterPushError { throw unregisterPushError }
        unregisteredPushTokens.append(token)
        return PushRegistrationResponse(ok: true)
    }
    func logout() async throws -> LogoutResult { logoutResult }

    func clearLocalSession() {
        clearedLocalSession = true
    }
}

private extension BackupProgress {
    static func fixture(running: Bool) -> BackupProgress {
        BackupProgress(
            running: running,
            jobID: running ? 42 : nil,
            startedAt: running ? 1_720_000_000 : nil,
            elapsedSeconds: running ? 30 : nil,
            pairs: nil,
            totalPairs: running ? 1 : nil,
            donePairs: running ? 0 : nil,
            last: nil
        )
    }
}

private extension JobRecord {
    static func fixture(id: Int, status: String, definitionID: String) -> JobRecord {
        JobRecord(
            id: id,
            kind: "backup",
            status: status,
            startedAt: Date().timeIntervalSince1970,
            endedAt: status == "running" ? nil : Date().timeIntervalSince1970,
            logFile: nil,
            definitionID: definitionID,
            definitionName: definitionID,
            configRevision: "revision-1"
        )
    }
}
