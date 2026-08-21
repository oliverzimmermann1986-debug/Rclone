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

    private func makeDefaults() -> UserDefaults {
        let suite = "AppModelLifecycleTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }
}

private final class StubAPIClient: APIClientProtocol {
    var configError: Error?
    private(set) var clearedLocalSession = false

    func login(username: String, password: String) async throws {}

    func getOverview() async throws -> OverviewResponse {
        throw APIError.server(status: 503, message: "Overview nicht verfügbar")
    }

    func getStorage(includeSizes: Bool) async throws -> StorageOverview {
        if includeSizes {
            throw URLError(.timedOut)
        }
        return StorageOverview(pairs: [])
    }

    func getConfig() async throws -> ConfigSnapshot {
        if let configError { throw configError }
        return ConfigSnapshot(
            revision: "revision-1",
            backup: BackupConfig(enabled: true, timezone: "Europe/Berlin", defaultSchedule: nil, pairs: [])
        )
    }

    func getJobs(limit: Int) async throws -> JobSearchResponse {
        JobSearchResponse(items: [], total: 0, limit: limit, offset: 0)
    }

    func getJob(id: Int) async throws -> JobRecord { throw APIError.invalidResponse }
    func getJobLog(id: Int) async throws -> JobLogResponse { throw APIError.invalidResponse }
    func getDoctor() async throws -> DoctorResponse { throw APIError.invalidResponse }

    func getProgress() async throws -> BackupProgress {
        BackupProgress(
            running: false,
            jobID: nil,
            startedAt: nil,
            elapsedSeconds: nil,
            pairs: nil,
            totalPairs: nil,
            donePairs: nil,
            last: nil
        )
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
    func logout() async throws { throw URLError(.notConnectedToInternet) }

    func clearLocalSession() {
        clearedLocalSession = true
    }
}
