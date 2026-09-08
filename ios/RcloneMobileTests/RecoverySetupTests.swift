import Foundation
import XCTest
@testable import RcloneMobile

final class RecoverySetupTests: XCTestCase {
    private var profile: RecoveryPolicyProfile {
        RecoveryPolicyProfile(id: "photos", name: "Fotos", description: "Kopie",
            pair: ["mode": .string("copy"), "allow_delete": .bool(false)],
            job: ["schedule": .string("0 3 * * *"), "retry_minutes": .number(60)], restore: [:])
    }

    func testSetupPreservesIdentityAndUnknownFieldsWithoutChangingSharedJob() throws {
        let pair = PairConfig(stableID: "stable", name: "Fotos", local: "/source", remote: "cloud:target",
            mode: "sync", allowDelete: true, extras: ["future_option": .string("kept")])
        let updated = try ProtectionSetup.pair(pair, profile: profile, snapshots: true)
        XCTAssertEqual(updated.id, pair.id)
        XCTAssertEqual(updated.local, pair.local)
        XCTAssertEqual(updated.remote, pair.remote)
        XCTAssertFalse(updated.allowDelete)
        XCTAssertEqual(updated.mode, "copy")
        let fields = try JSONDecoder().decode([String: JSONValue].self, from: JSONEncoder().encode(updated))
        XCTAssertEqual(fields["future_option"], .string("kept"))
        XCTAssertEqual(fields["recovery_snapshots"], .bool(true))
        let shared = JobDefinition(id: "shared", name: "Nacht", dataPathIDs: [pair.id, "other"], schedule: "30 4 * * *")
        let jobs = ProtectionSetup.jobs([shared], pair: updated, profile: profile)
        XCTAssertEqual(jobs.count, 1)
        XCTAssertEqual(jobs[0].schedule, shared.schedule)
        XCTAssertEqual(jobs[0].dataPathIDs, shared.dataPathIDs)
    }

    func testSetupCreatesOnlyMissingAssignmentAndRejectsDifferentDirection() throws {
        let pair = PairConfig(stableID: "stable", name: "Fotos", local: "/source", remote: "cloud:target")
        let jobs = ProtectionSetup.jobs([], pair: pair, profile: profile)
        XCTAssertEqual(jobs.count, 1)
        XCTAssertEqual(jobs[0].dataPathIDs, [pair.id])
        XCTAssertEqual(jobs[0].schedule, "0 3 * * *")
        XCTAssertEqual(jobs[0].id.count, 32)
        XCTAssertTrue(jobs[0].id.allSatisfy { $0.isHexDigit })
        let collision = JobDefinition(id: "other", name: "Schutz Fotos", dataPathIDs: ["other"])
        XCTAssertEqual(ProtectionSetup.jobs([collision], pair: pair, profile: profile).last?.name, "Schutz Fotos 2")
        let pull = PairConfig(stableID: "pull", name: "Pull", local: "/source", remote: "cloud:target", direction: "pull")
        XCTAssertThrowsError(try ProtectionSetup.pair(pull, profile: profile, snapshots: false))
    }

    func testOfflineKeysSeparateAccountPortSchemeAndBasePath() {
        let store = RecoveryOfflineStore()
        let original = store.key(server: "http://192.168.1.20:8000", username: "admin")
        for (server, username) in [("http://192.168.1.20:8001", "admin"),
                                    ("https://192.168.1.20:8000", "admin"),
                                    ("http://192.168.1.20:8000/other", "admin"),
                                    ("http://192.168.1.20:8000", "other")] {
            XCTAssertNotEqual(original, store.key(server: server, username: username))
        }
        XCTAssertEqual(original, store.key(server: "http://192.168.1.20:8000/", username: "admin"))
    }

    func testOldUnboundOrExpiredProofIsNotCurrent() throws {
        let old = Data(#"{"state":"passed","checksum_verified":true}"#.utf8)
        XCTAssertFalse(try JSONDecoder().decode(RestoreEvidence.self, from: old).isCurrent)
        let expired = Data(#"{"state":"passed","valid":true,"checksum_verified":true,"valid_until":1}"#.utf8)
        XCTAssertFalse(try JSONDecoder().decode(RestoreEvidence.self, from: expired).isCurrent)
        let bound = Data("{\"state\":\"passed\",\"valid\":true,\"binding\":{\"pair_id\":\"abc\",\"fingerprint\":\"hash\"},\"checksum_verified\":true,\"valid_until\":\(Date().timeIntervalSince1970 + 60)}".utf8)
        XCTAssertTrue(try JSONDecoder().decode(RestoreEvidence.self, from: bound).isCurrent)
        XCTAssertEqual(try JSONDecoder().decode(RecoveryRestoreProof.self, from: bound).binding?["pair_id"], "abc")
    }

    func testOldArchiveNeverDecodesAsFullSnapshot() throws {
        let data = Data(#"{"id":"old","label":"Änderungen","kind":"version"}"#.utf8)
        let old = try JSONDecoder().decode(RecoveryPoint.self, from: data)
        XCTAssertNotEqual(old.complete, true)
        let full = Data(#"{"id":"full-123","label":"Komplett","kind":"full","complete":true,"files":3,"total_bytes":99,"storage":"server"}"#.utf8)
        XCTAssertTrue(try JSONDecoder().decode(RecoveryPoint.self, from: full).complete == true)
    }
}
