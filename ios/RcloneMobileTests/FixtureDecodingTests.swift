import Foundation
import XCTest
@testable import RcloneMobile

final class FixtureDecodingTests: XCTestCase {
    func testOverviewDecodesSharedBackendContract() throws {
        let overview: OverviewResponse = try decode("diagnostics_overview")

        XCTAssertEqual(overview.app.version, "1.7.1")
        XCTAssertEqual(overview.system.hostname, "backup")
        XCTAssertEqual(overview.pairs.enabled, 1)
        XCTAssertEqual(overview.pairs.health.first?.jobs.first?.id, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        XCTAssertEqual(overview.pairs.health.first?.schedule, "0 3 * * *")
    }

    func testStorageWithoutAndWithSizesDecodeSharedContract() throws {
        let base: StorageOverview = try decode("storage_without_sizes")
        XCTAssertNil(base.pairs.first?.sourceSize)
        XCTAssertNil(base.pairs.first?.targetSize)
        XCTAssertEqual(base.pairs.first?.lastTransferred, "2 KiB")

        let detailed: StorageOverview = try decode("storage_with_sizes")
        XCTAssertEqual(detailed.pairs.count, 4)
        XCTAssertEqual(
            detailed.pairs.compactMap(\.sourceSize?.measurementStatus),
            ["fresh", "cached", "stale", "failed"]
        )
        XCTAssertEqual(detailed.pairs[0].sourceSize?.count, 12)
        XCTAssertEqual(detailed.pairs[1].targetSize?.measuredAt, 1_719_999_900)
        XCTAssertEqual(detailed.pairs[2].sourceSize?.measurementError, "Timeout")
        XCTAssertNil(detailed.pairs[3].sourceSize?.bytes)
    }

    func testConfigAndJobReadModelsDecodeSharedContract() throws {
        let config: ConfigSnapshot = try decode("config")
        XCTAssertEqual(config.revision.count, 64)
        XCTAssertEqual(config.backup.pairs.first?.name, "Fotos")
        XCTAssertEqual(config.backup.jobs.first?.dataPathIDs, ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])

        let list: [JobRecord] = try decode("jobs_list")
        let search: JobSearchResponse = try decode("jobs_search")
        let detail: JobRecord = try decode("job_detail")
        let log: JobLogResponse = try decode("job_log")
        let current: CurrentJobsContract = try decode("jobs_current")

        XCTAssertEqual(list.first?.status, "running")
        XCTAssertEqual(search.total, 1)
        XCTAssertEqual(detail.id, 41)
        XCTAssertTrue(log.log.contains("Transferred"))
        XCTAssertEqual(current.backup?.id, 42)
        XCTAssertNil(current.restoretest)
    }

    func testOperationalModelsDecodeSharedContract() throws {
        let progress: BackupProgress = try decode("backup_progress")
        let pbs: PBSStatus = try decode("pbs_status")
        let scheduler: SchedulerStateContract = try decode("scheduler_state")
        let doctor: DoctorResponse = try decode("doctor")

        XCTAssertTrue(progress.running)
        XCTAssertEqual(progress.pairs?.first?.percent, 50)
        XCTAssertEqual(pbs.targets.first?.name, "config")
        XCTAssertTrue(scheduler.paused)
        XCTAssertEqual(scheduler.timezone, "Europe/Berlin")
        XCTAssertTrue(doctor.ok)
        XCTAssertEqual(doctor.checks.first?.name, "Konfiguration")
    }

    func testJobDefinitionsDecodeSharedContract() throws {
        let definitions: [JobDefinition] = try decode("job_definitions")

        XCTAssertEqual(definitions.first?.id, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        XCTAssertEqual(definitions.first?.dataPathIDs, ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
        XCTAssertEqual(definitions.first?.executionMode, "sequential")
    }

    func testMaintenanceAndBrowseModelsDecodeSharedContract() throws {
        let browse: BrowseResponse = try decode("browse_local")
        let audit: AuditResponse = try decode("maintenance_audit")
        let logs: MaintenanceLogsResponse = try decode("maintenance_logs")
        let database: DatabaseStatus = try decode("maintenance_database")
        let snapshots: SnapshotListResponse = try decode("config_snapshots")
        let filter: FilterFile = try decode("filter_file")

        XCTAssertEqual(browse.entries.first?.path, "/mnt")
        XCTAssertEqual(audit.events.first?.eventType, "config_saved")
        XCTAssertEqual(logs.logs.first?.size, 2_048)
        XCTAssertTrue(database.integrity.ok)
        XCTAssertEqual(snapshots.maxSnapshots, 30)
        XCTAssertEqual(filter.content, "- *.tmp\n")
    }

    func testEverySharedEndpointHasVersionedGetMetadata() throws {
        let root = try contractRoot()
        XCTAssertEqual(root["contract_version"] as? Int, 1)
        let endpoints = try XCTUnwrap(root["endpoints"] as? [String: [String: Any]])
        XCTAssertEqual(endpoints.count, 20)
        for endpoint in endpoints.values {
            XCTAssertEqual(endpoint["method"] as? String, "GET")
            XCTAssertTrue((endpoint["path"] as? String)?.hasPrefix("/api/") == true)
        }
    }

    func testServerURLAddsHTTPS() throws {
        let url = try APIClient.normalizedServerURL("backup.example.de")
        XCTAssertEqual(url.absoluteString, "https://backup.example.de")
    }

    func testLocalIPv4UsesHTTPReverseProxyPort() throws {
        let url = try APIClient.normalizedServerURL("192.168.1.67")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.67")
    }

    func testLocalIPv4KeepsExplicitPort() throws {
        let url = try APIClient.normalizedServerURL("192.168.1.97:9000")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.97:9000")
    }

    func testExplicitLocalHTTPKeepsDefaultPort() throws {
        let url = try APIClient.normalizedServerURL("http://192.168.1.67")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.67")
    }

    func testExplicitHTTPSIsNotRewritten() throws {
        let url = try APIClient.normalizedServerURL("https://192.168.1.97")
        XCTAssertEqual(url.absoluteString, "https://192.168.1.97")
    }

    func testServerURLRejectsEmbeddedCredentials() {
        XCTAssertThrowsError(try APIClient.normalizedServerURL("https://admin:secret@backup.example.de")) { error in
            XCTAssertEqual(error as? APIError, .invalidServer)
        }
        XCTAssertThrowsError(try APIClient.normalizedServerURL("admin:secret@192.168.1.67")) { error in
            XCTAssertEqual(error as? APIError, .invalidServer)
        }
    }

    private func decode<Value: Decodable>(_ endpoint: String) throws -> Value {
        let root = try contractRoot()
        let endpoints = try XCTUnwrap(root["endpoints"] as? [String: [String: Any]])
        let fixture = try XCTUnwrap(endpoints[endpoint])
        let body = try XCTUnwrap(fixture["body"])
        let data = try JSONSerialization.data(withJSONObject: body)
        return try JSONDecoder().decode(Value.self, from: data)
    }

    private func contractRoot() throws -> [String: Any] {
        let url = try XCTUnwrap(
            Bundle(for: Self.self).url(
                forResource: "native_read_contract_v1",
                withExtension: "json"
            )
        )
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        )
    }
}

private struct CurrentJobsContract: Decodable {
    let backup: JobRecord?
    let check: JobRecord?
    let quicksync: JobRecord?
    let restoretest: JobRecord?
    let pbs: JobRecord?
}

private struct SchedulerStateContract: Decodable {
    let paused: Bool
    let until: Double?
    let remainingSeconds: Int?
    let reason: String?
    let actor: String?
    let updatedAt: Double?
    let enabled: Bool
    let timezone: String

    enum CodingKeys: String, CodingKey {
        case paused, until, reason, actor, enabled, timezone
        case remainingSeconds = "remaining_seconds"
        case updatedAt = "updated_at"
    }
}
