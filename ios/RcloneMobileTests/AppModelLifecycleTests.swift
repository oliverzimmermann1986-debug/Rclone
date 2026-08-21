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
        XCTAssertTrue(model.errorMessage?.contains("30 Sekunden") == true)
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
    private(set) var updatedConfig: ConfigSnapshot?
    private(set) var clearedLocalSession = false
    private(set) var jobsCallCount = 0
    private(set) var doctorCallCount = 0

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
        return ConfigSnapshot(
            revision: "revision-1",
            backup: BackupConfig(enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: [])
        )
    }

    func getJobDefinitions() async throws -> [JobDefinition] {
        updatedConfig?.backup.jobs ?? []
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

    func getJobs(limit: Int) async throws -> JobSearchResponse {
        jobsCallCount += 1
        JobSearchResponse(items: [], total: 0, limit: limit, offset: 0)
    }

    func getJob(id: Int) async throws -> JobRecord { throw APIError.invalidResponse }
    func getJobLog(id: Int) async throws -> JobLogResponse { throw APIError.invalidResponse }
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
        PBSStatus(
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
