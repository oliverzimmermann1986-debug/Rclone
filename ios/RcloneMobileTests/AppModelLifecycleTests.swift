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
    var updateConfigError: Error?
    var passwordChangeResponse: PasswordChangeResponse?
    var runAllResponse: ActionResponse?
    var pbsError: Error?
    var baseConfig: ConfigSnapshot?
    private(set) var updatedConfig: ConfigSnapshot?
    private(set) var clearedLocalSession = false
    private(set) var jobsCallCount = 0
    private(set) var doctorCallCount = 0
    private(set) var runAllCallCount = 0
    private(set) var definitionsCallCount = 0

    func login(username: String, password: String) async throws {}

    func getOverview() async throws -> OverviewResponse {
        throw APIError.server(status: 503, message: "Overview nicht verfügbar")
    }

    func getStorage(includeSizes: Bool, forceRefresh: Bool) async throws -> StorageOverview {
        if includeSizes {
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
        return JobSearchResponse(items: [], total: 0, limit: limit, offset: 0)
    }
    func searchJobs(kind: String?, status: String?, query: String, limit: Int, offset: Int) async throws -> JobSearchResponse {
        try await getJobs(limit: limit)
    }
    func downloadJobsCSV(kind: String?, status: String?, query: String) async throws -> URL { throw APIError.invalidResponse }

    func getJob(id: Int) async throws -> JobRecord { throw APIError.invalidResponse }
    func getJobLog(id: Int) async throws -> JobLogResponse { throw APIError.invalidResponse }
    func downloadJobLog(id: Int) async throws -> URL { throw APIError.invalidResponse }
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
